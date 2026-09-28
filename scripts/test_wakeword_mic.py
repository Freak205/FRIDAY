"""Real-microphone, human-in-the-loop wake-word reliability test (Phase
10.X.6). Like scripts/voice_mic_latency_test.py, this cannot be run
unattended — it needs a person actually saying "Hey Jarvis" (and ordinary
sentences, and staying quiet) into the real microphone.

Design: audio for every trial (wake attempts, silence, ordinary speech) is
recorded ONCE from the real mic, then the real openWakeWord model's raw
per-frame score sequence for that clip is computed ONCE. Everything after
that — trying different `threshold`/`persist_frames`/`window_s` combinations
via WakeScoreWindow, the exact same rolling-window decision logic
ConversationLoop uses live (friday/voice/conversation.py) — is free, offline
replay against the saved scores. That means the threshold sweep in the
Phase 10.X.6 brief (0.30 through 0.60) doesn't need the phrase list repeated
seven times; you say each phrase once and every candidate threshold gets
evaluated against that same recording. Mirrors the
record-once/replay-many-configs approach scripts/benchmark_stt.py already
uses for STT.

Usage:
    python scripts/test_wakeword_mic.py
    python scripts/test_wakeword_mic.py --thresholds 0.35,0.45,0.55
    python scripts/test_wakeword_mic.py --save-samples
    python scripts/test_wakeword_mic.py --load-dir data/wakeword_test_samples/20260913 --no-record

Pass --debug to also print every attempt's raw per-frame score trace, useful
for eyeballing exactly where a missed "Hey Jarvis" peaked.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Empty

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import BLOCK_SIZE, SAMPLE_RATE, get_mic_stream, warm_up_microphone  # noqa: E402
from friday.voice.types import MicrophoneUnavailable, WakeWordBackendError  # noqa: E402
from friday.voice.wakeword import FRAME_S, WakeScoreWindow, WakeWordDetector  # noqa: E402

# Phase 10.X.6 brief's exact REAL HUMAN TEST PHRASES list.
WAKE_PHRASES = [
    "Hey Jarvis",
    "Hey Jarvis",
    "Hey Jarvis, open VS Code",
    "Hey Jarvis, open Chrome",
    "Hey Jarvis, read what's on my screen",
    "Hey Jarvis, go to WhatsApp",
    "Hey Jarvis",
    "Hey Jarvis, what is my status?",
    "Hey Jarvis",
    "Hey Jarvis, open Notepad",
]

NORMAL_SPEECH_PHRASES = [
    "open VS Code",
    "what is the status",
    "bro open Chrome",
    "I was just talking about something completely unrelated to all this",
    "let's grab lunch later and sort the rest out tomorrow morning",
]

DEFAULT_THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]


@dataclass
class Attempt:
    label: str
    kind: str  # "wake" | "silence" | "speech"
    audio: np.ndarray = field(repr=False)
    frame_scores: list[float] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"label": self.label, "kind": self.kind, "frame_scores": self.frame_scores}


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def countdown(seconds: int, label: str) -> None:
    for n in range(seconds, 0, -1):
        print(f"    {label} in {n}...", end="\r", flush=True)
        time.sleep(1)
    print(f"    {label} NOW!            ")


def record_fixed(stream, duration_s: float) -> np.ndarray:
    """Records exactly `duration_s` seconds from an already-open MicStream —
    unlike VAD-gated record_utterance(), this keeps everything (leading
    silence, the wake phrase, trailing speech) so it can be re-scored offline
    at different thresholds later.
    """
    q = stream.open_session()
    blocks: list[np.ndarray] = []
    deadline = time.perf_counter() + duration_s
    try:
        while time.perf_counter() < deadline:
            try:
                blocks.append(q.get(timeout=0.1))
            except Empty:
                continue
    finally:
        stream.close_session(q)
    return np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)


def score_clip(detector: WakeWordDetector, audio: np.ndarray) -> list[float]:
    """Feeds `audio` through `detector` at the real production block size
    (480 samples/30ms, matching MicStream), after resetting the detector's
    own state first — so scoring one saved clip can never be contaminated by
    whatever clip was scored right before it in this same process (exactly
    the cross-contamination ConversationLoop.reset() now also guards against
    live — see friday/voice/wakeword.py's WakeWordDetector.reset()).
    """
    detector.reset()
    scores: list[float] = []
    for i in range(0, len(audio), BLOCK_SIZE):
        block = audio[i : i + BLOCK_SIZE]
        if block.size == 0:
            continue
        score = detector.feed(block)
        if score is not None:
            scores.append(score)
    return scores


def evaluate(
    scores: list[float], *, threshold: float, window_frames: int, persist_frames: int
) -> tuple[bool, float | None, float]:
    """Replays a saved per-frame score sequence through WakeScoreWindow — the
    exact same rolling-window decision ConversationLoop makes live — and
    returns (triggered, latency_s to the first trigger, peak score seen).
    """
    window = WakeScoreWindow(threshold=threshold, window_frames=window_frames, persist_frames=persist_frames)
    peak_overall = 0.0
    for i, score in enumerate(scores):
        peak_overall = max(peak_overall, score)
        _, triggered = window.push(score)
        if triggered:
            return True, i * FRAME_S, peak_overall
    return False, None, peak_overall


def save_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())


def load_attempts(load_dir: Path) -> list[Attempt]:
    manifest = json.loads((load_dir / "manifest.json").read_text(encoding="utf-8"))
    attempts = []
    for entry in manifest["attempts"]:
        with wave.open(str(load_dir / entry["wav"]), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        attempts.append(Attempt(label=entry["label"], kind=entry["kind"], audio=audio))
    return attempts


def record_all(*, record_seconds: float, silence_trials: int, speech_trials: int) -> list[Attempt]:
    device = CFG.voice.stt.input_device
    print("\nWarming up the microphone...")
    warm_up_microphone(device=device)
    stream = get_mic_stream(device)
    try:
        stream.ensure_started()
    except MicrophoneUnavailable as exc:
        print(f"FAILED: microphone unavailable ({exc})")
        sys.exit(1)

    attempts: list[Attempt] = []

    print(
        f"\n--- Wake phrases ({len(WAKE_PHRASES)}) ---\n"
        "For each one: press Enter, wait for the countdown, then say the phrase "
        "at a normal conversational volume.\n"
    )
    for i, phrase in enumerate(WAKE_PHRASES, 1):
        ask(f'\n[{i}/{len(WAKE_PHRASES)}] Press Enter, then say: "{phrase}"')
        countdown(2, "recording")
        audio = record_fixed(stream, record_seconds)
        attempts.append(Attempt(label=phrase, kind="wake", audio=audio))
        print("    captured.")

    print(
        f"\n--- Silence trials ({silence_trials}) ---\n"
        "Stay quiet each time (ambient room noise only, no speech) — these check "
        "for false triggers on nothing at all.\n"
    )
    for i in range(1, silence_trials + 1):
        ask(f"\n[{i}/{silence_trials}] Press Enter, then stay quiet")
        countdown(1, "recording")
        audio = record_fixed(stream, record_seconds)
        attempts.append(Attempt(label=f"silence #{i}", kind="silence", audio=audio))
        print("    captured.")

    print(
        f"\n--- Ordinary speech trials ({len(NORMAL_SPEECH_PHRASES[:speech_trials])}) ---\n"
        "Say each phrase normally — NONE of these contain the wake word, so a "
        "trigger on any of them is a false positive.\n"
    )
    for i, phrase in enumerate(NORMAL_SPEECH_PHRASES[:speech_trials], 1):
        ask(f'\n[{i}/{speech_trials}] Press Enter, then say: "{phrase}"')
        countdown(2, "recording")
        audio = record_fixed(stream, record_seconds)
        attempts.append(Attempt(label=phrase, kind="speech", audio=audio))
        print("    captured.")

    return attempts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS))
    parser.add_argument("--window-s", type=float, default=CFG.voice.wakeword.window_s)
    parser.add_argument("--persist-frames", type=int, default=CFG.voice.wakeword.persist_frames)
    parser.add_argument("--record-seconds", type=float, default=3.5)
    parser.add_argument("--silence-trials", type=int, default=5)
    parser.add_argument("--speech-trials", type=int, default=len(NORMAL_SPEECH_PHRASES))
    parser.add_argument("--save-samples", action="store_true", help="Save recorded WAVs + a manifest for reanalysis later.")
    parser.add_argument("--samples-dir", default=str(paths.DATA / "wakeword_test_samples" / time.strftime("%Y%m%d_%H%M%S")))
    parser.add_argument("--load-dir", default=None, help="Reanalyze previously saved recordings instead of using the mic.")
    parser.add_argument("--no-record", action="store_true", help="Synonym for requiring --load-dir; skips the mic entirely.")
    parser.add_argument("--debug", action="store_true", help="Print each attempt's raw per-frame score trace.")
    parser.add_argument("--report", default=str(paths.DATA / "wakeword_test_report.json"))
    args = parser.parse_args()

    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    window_frames = max(1, round(args.window_s / FRAME_S))

    if args.load_dir or args.no_record:
        if not args.load_dir:
            print("FAILED: --no-record requires --load-dir")
            return 1
        attempts = load_attempts(Path(args.load_dir))
        print(f"Loaded {len(attempts)} saved attempts from {args.load_dir}")
    else:
        attempts = record_all(
            record_seconds=args.record_seconds,
            silence_trials=args.silence_trials,
            speech_trials=args.speech_trials,
        )
        if args.save_samples:
            samples_dir = Path(args.samples_dir)
            manifest = {"attempts": []}
            for i, att in enumerate(attempts, 1):
                wav_name = f"{i:02d}_{att.kind}.wav"
                save_wav(samples_dir / wav_name, att.audio)
                manifest["attempts"].append({"label": att.label, "kind": att.kind, "wav": wav_name})
            (samples_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"\nSaved recordings + manifest to {samples_dir}")

    print("\nLoading the real openWakeWord model and scoring every recording...")
    detector = WakeWordDetector(
        model_name=CFG.voice.wakeword.model,
        models_dir=Path(CFG.voice.wakeword.models_dir) if CFG.voice.wakeword.models_dir else None,
    )
    try:
        detector.warm_up()
    except WakeWordBackendError as exc:
        print(f"FAILED: wake-word model unavailable ({exc})")
        return 1

    for att in attempts:
        att.frame_scores = score_clip(detector, att.audio)
        if args.debug:
            trace = " ".join(f"{s:.2f}" for s in att.frame_scores)
            print(f"  [{att.kind}] {att.label!r}: {trace}")

    wake_attempts = [a for a in attempts if a.kind == "wake"]
    silence_attempts = [a for a in attempts if a.kind == "silence"]
    speech_attempts = [a for a in attempts if a.kind == "speech"]

    print("\n" + "=" * 78)
    print(f"{'threshold':>9} {'detected':>10} {'rate':>7} {'avg latency':>12} "
          f"{'silence FP':>11} {'speech FP':>10} {'peak(med)':>10}")
    print("-" * 78)

    results = []
    for threshold in thresholds:
        wake_results = [
            evaluate(a.frame_scores, threshold=threshold, window_frames=window_frames, persist_frames=args.persist_frames)
            for a in wake_attempts
        ]
        detected = sum(1 for triggered, _, _ in wake_results if triggered)
        latencies = [lat for triggered, lat, _ in wake_results if triggered and lat is not None]
        peaks = [peak for _, _, peak in wake_results]
        silence_fp = sum(
            1
            for a in silence_attempts
            if evaluate(a.frame_scores, threshold=threshold, window_frames=window_frames, persist_frames=args.persist_frames)[0]
        )
        speech_fp = sum(
            1
            for a in speech_attempts
            if evaluate(a.frame_scores, threshold=threshold, window_frames=window_frames, persist_frames=args.persist_frames)[0]
        )
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        median_peak = statistics.median(peaks) if peaks else 0.0
        results.append({
            "threshold": threshold, "detected": detected, "total": len(wake_attempts),
            "avg_latency_s": avg_latency, "silence_false_triggers": silence_fp,
            "speech_false_triggers": speech_fp, "median_peak": median_peak,
        })
        rate = f"{100 * detected / max(1, len(wake_attempts)):.0f}%"
        lat_s = f"{avg_latency:.2f}s" if avg_latency is not None else "n/a"
        print(f"{threshold:>9.2f} {detected:>7}/{len(wake_attempts):<3}{rate:>6} {lat_s:>12} "
              f"{silence_fp:>6}/{len(silence_attempts):<4}{speech_fp:>5}/{len(speech_attempts):<4} {median_peak:>10.3f}")

    print("=" * 78)

    # -- headline summary for the currently-configured threshold, in the
    # exact format the Phase 10.X.6 brief asked for -----------------------
    configured = CFG.voice.wakeword.threshold
    match = min(results, key=lambda r: abs(r["threshold"] - configured)) if results else None
    if match is not None:
        all_peaks = [peak for a in wake_attempts for _, _, peak in [evaluate(
            a.frame_scores, threshold=configured, window_frames=window_frames, persist_frames=args.persist_frames
        )]]
        print(f"\nWake-word test (threshold={configured}, window_s={args.window_s}, persist_frames={args.persist_frames})")
        print("-------------")
        print(f"Attempts: {match['total']}")
        print(f"Detected: {match['detected']}/{match['total']}")
        print(f"Detection rate: {100 * match['detected'] / max(1, match['total']):.0f}%")
        print(f"Average detection latency: {match['avg_latency_s']:.2f} sec" if match["avg_latency_s"] is not None else "Average detection latency: n/a")
        print(f"Peak score: {max(all_peaks):.3f}" if all_peaks else "Peak score: n/a")
        print(f"Median peak score: {statistics.median(all_peaks):.3f}" if all_peaks else "Median peak score: n/a")
        print(f"False triggers during silence: {match['silence_false_triggers']}")
        print(f"False triggers during normal speech: {match['speech_false_triggers']}")

    report = {
        "config": {
            "model": CFG.voice.wakeword.model, "window_s": args.window_s,
            "persist_frames": args.persist_frames, "record_seconds": args.record_seconds,
        },
        "sweep": results,
        "attempts": [a.to_json() for a in attempts],
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nFull report (including every raw score trace) written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
