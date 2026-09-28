"""Phase 10.X.7 — live wake-word instrumentation.

Continuously prints/logs exactly what the wake-detection path sees, frame by
frame, straight from the real microphone: timestamp, RMS, peak amplitude,
chunk size, sample rate, wake score, rolling peak, rolling average, positive
frame count, detector state, time since last detection. This is deliberately
*not* the production ConversationLoop/FSM — it's the smallest thing that
still touches real hardware, so a problem seen here can only be the
microphone, the model, or the rolling-window decision logic, never the FSM,
STT, TTS, GUI, or orchestrator.

Two modes:

  --live               Run until Ctrl+C, streaming one line per completed
                        openWakeWord frame (~every 80ms) plus a full JSONL
                        record per frame to --log for later offline analysis.
                        Use this to watch scores while doing whatever you
                        want at the mic (silence, talking, "Hey Jarvis").

  --trials N           Interactive: walks you through N labelled attempts
                        (you choose the label each time — "Hey Jarvis",
                        "silence", ordinary speech, ...) and reports, per
                        attempt: peak_score, median_score, positive_frame_count,
                        detection (bool, against the configured threshold/
                        window/persist_frames), detection_latency_s. Results
                        are also written to --report as JSON.

Bypasses ConversationFSM/VoiceSession/STT/TTS/GUI/orchestrator entirely —
only microphone -> WakeWordDetector -> WakeScoreWindow, the same objects
production uses, wired directly.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from queue import Empty

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.config import CFG  # noqa: E402
from friday.voice.capture import SAMPLE_RATE, get_mic_stream, warm_up_microphone  # noqa: E402
from friday.voice.types import MicrophoneUnavailable, WakeWordBackendError  # noqa: E402
from friday.voice.wakeword import CHUNK_SAMPLES, FRAME_S, WakeScoreWindow, WakeWordDetector  # noqa: E402


def build_detector() -> WakeWordDetector:
    wake = CFG.voice.wakeword
    detector = WakeWordDetector(
        model_name=wake.model,
        models_dir=Path(wake.models_dir) if wake.models_dir else None,
        verifier_model_path=wake.verifier_model_path or None,
        verifier_threshold=wake.verifier_threshold,
    )
    detector.warm_up()
    return detector


def run_live(args: argparse.Namespace) -> int:
    device = CFG.voice.stt.input_device
    wake_cfg = CFG.voice.wakeword
    window_frames = max(1, round(wake_cfg.window_s / FRAME_S))
    window = WakeScoreWindow(
        threshold=wake_cfg.threshold, window_frames=window_frames, persist_frames=wake_cfg.persist_frames,
    )

    print("Warming up microphone + wake-word model...")
    warm_up_microphone(device=device)
    try:
        detector = build_detector()
    except WakeWordBackendError as exc:
        print(f"FAILED: {exc}")
        return 1

    stream = get_mic_stream(device)
    try:
        stream.ensure_started()
    except MicrophoneUnavailable as exc:
        print(f"FAILED: microphone unavailable ({exc})")
        return 1

    print(
        f"model={wake_cfg.model} threshold={wake_cfg.threshold} window_s={wake_cfg.window_s} "
        f"persist_frames={wake_cfg.persist_frames} sample_rate={SAMPLE_RATE} chunk_samples={CHUNK_SAMPLES} "
        f"({FRAME_S * 1000:.0f}ms/frame) verifier={'ON: ' + wake_cfg.verifier_model_path if wake_cfg.verifier_model_path else 'off'}"
    )
    print("Streaming live (Ctrl+C to stop)... talk, stay quiet, say \"Hey Jarvis\" — whatever you want to observe.\n")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")

    q = stream.open_session()
    positive_frames = 0
    total_frames = 0
    last_detection_at: float | None = None
    peaks_window: deque[float] = deque(maxlen=window_frames)
    t_start = time.perf_counter()

    try:
        while True:
            try:
                block = q.get(timeout=0.2)
            except Empty:
                continue
            rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
            peak_amp = float(np.max(np.abs(block))) if block.size else 0.0
            try:
                score = detector.feed(block)
            except WakeWordBackendError as exc:
                print(f"\nFAILED: wake-word model error mid-stream: {exc}")
                return 1
            if score is None:
                continue
            total_frames += 1
            peaks_window.append(score)
            rolling_peak, triggered = window.push(score)
            rolling_avg = statistics.fmean(peaks_window)
            is_positive = score >= wake_cfg.threshold
            if is_positive:
                positive_frames += 1
            now = time.perf_counter()
            since_last = f"{now - last_detection_at:.2f}s" if last_detection_at is not None else "n/a"
            state = "DETECTED!" if triggered else ("above-threshold" if is_positive else "scanning")
            if triggered:
                last_detection_at = now

            record = {
                "t": round(now - t_start, 3),
                "rms": round(rms, 5),
                "peak_amplitude": round(peak_amp, 5),
                "chunk_samples": CHUNK_SAMPLES,
                "sample_rate": SAMPLE_RATE,
                "score": round(score, 4),
                "rolling_peak": round(rolling_peak, 4),
                "rolling_avg": round(rolling_avg, 4),
                "positive_frame_count": positive_frames,
                "detector_state": state,
                "since_last_detection_s": since_last,
                "triggered": triggered,
            }
            log_file.write(json.dumps(record) + "\n")
            log_file.flush()

            print(
                f"\rt={record['t']:7.2f}s rms={rms:.4f} peak={peak_amp:.4f} score={score:.3f} "
                f"roll_peak={rolling_peak:.3f} roll_avg={rolling_avg:.3f} pos_frames={positive_frames:5d} "
                f"state={state:12s} since_last={since_last:>7s}   ",
                end="",
                flush=True,
            )
            if triggered:
                print()  # keep the DETECTED line instead of overwriting it
    except KeyboardInterrupt:
        pass
    finally:
        stream.close_session(q)
        log_file.close()

    elapsed = time.perf_counter() - t_start
    print(f"\n\nStopped after {elapsed:.1f}s, {total_frames} frames scored, {positive_frames} above threshold.")
    print(f"Full per-frame log written to {log_path}")
    return 0


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def countdown(seconds: int) -> None:
    for n in range(seconds, 0, -1):
        print(f"    starting in {n}...", end="\r", flush=True)
        time.sleep(1)
    print("    GO!                  ")


def run_trials(args: argparse.Namespace) -> int:
    device = CFG.voice.stt.input_device
    wake_cfg = CFG.voice.wakeword
    window_frames = max(1, round(wake_cfg.window_s / FRAME_S))

    print("Warming up microphone + wake-word model...")
    warm_up_microphone(device=device)
    try:
        detector = build_detector()
    except WakeWordBackendError as exc:
        print(f"FAILED: {exc}")
        return 1
    stream = get_mic_stream(device)
    try:
        stream.ensure_started()
    except MicrophoneUnavailable as exc:
        print(f"FAILED: microphone unavailable ({exc})")
        return 1

    results = []
    for i in range(1, args.trials + 1):
        label = ask(
            f'\n[{i}/{args.trials}] Label for this attempt (e.g. "Hey Jarvis", "silence", '
            f'"ordinary speech") [default: Hey Jarvis]: '
        ) or "Hey Jarvis"
        countdown(2)

        window = WakeScoreWindow(
            threshold=wake_cfg.threshold, window_frames=window_frames, persist_frames=wake_cfg.persist_frames,
        )
        detector.reset()
        q = stream.open_session()
        scores: list[float] = []
        detected = False
        detection_latency: float | None = None
        t0 = time.perf_counter()
        deadline = t0 + args.record_seconds
        try:
            while time.perf_counter() < deadline:
                try:
                    block = q.get(timeout=0.1)
                except Empty:
                    continue
                score = detector.feed(block)
                if score is None:
                    continue
                scores.append(score)
                _, triggered = window.push(score)
                if triggered and not detected:
                    detected = True
                    detection_latency = time.perf_counter() - t0
        finally:
            stream.close_session(q)

        peak_score = max(scores) if scores else 0.0
        median_score = statistics.median(scores) if scores else 0.0
        positive_frame_count = sum(1 for s in scores if s >= wake_cfg.threshold)
        result = {
            "attempt": i,
            "label": label,
            "peak_score": round(peak_score, 4),
            "median_score": round(median_score, 4),
            "positive_frame_count": positive_frame_count,
            "total_frames": len(scores),
            "detection": detected,
            "detection_latency_s": round(detection_latency, 3) if detection_latency is not None else None,
        }
        results.append(result)
        print(
            f"    -> peak={peak_score:.3f} median={median_score:.3f} "
            f"positive_frames={positive_frame_count}/{len(scores)} "
            f"detection={'YES' if detected else 'no'} "
            f"latency={result['detection_latency_s']}s"
        )

    wake_results = [r for r in results if "jarvis" in r["label"].lower()]
    if wake_results:
        detected_n = sum(1 for r in wake_results if r["detection"])
        print(
            f"\n=== Summary over {len(wake_results)} attempt(s) labelled with 'jarvis' ===\n"
            f"Detected: {detected_n}/{len(wake_results)} "
            f"({100 * detected_n / len(wake_results):.0f}%)"
        )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "config": {
                    "model": wake_cfg.model, "threshold": wake_cfg.threshold,
                    "window_s": wake_cfg.window_s, "persist_frames": wake_cfg.persist_frames,
                    "verifier_model_path": wake_cfg.verifier_model_path or None,
                },
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Stream diagnostics continuously until Ctrl+C.")
    parser.add_argument("--trials", type=int, default=0, help="Run this many interactive labelled attempts instead.")
    parser.add_argument("--record-seconds", type=float, default=3.0, help="Recording window per trial (--trials mode).")
    parser.add_argument("--log", default="data/wakeword_diagnostic_log.jsonl", help="Per-frame JSONL log (--live mode).")
    parser.add_argument("--report", default="data/wakeword_diagnostic_trials.json", help="Summary JSON (--trials mode).")
    args = parser.parse_args()

    if args.trials > 0:
        return run_trials(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
