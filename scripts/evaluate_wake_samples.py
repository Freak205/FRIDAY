"""Phase 10.X.7 Step 6 (+ Step 4 strategy comparison) — offline, deterministic
scoring of a real-microphone wake-word corpus (recorded by
scripts/record_wake_samples.py) through the actual production
`WakeWordDetector`, and through five different rolling-decision strategies
so the choice of algorithm is picked from real recordings, not synthetic
unit tests or intuition:

    A. raw       — single frame >= threshold triggers immediately
    B. peak      — rolling max over a window >= threshold (reported
                   separately from A because they're asked for separately in
                   the brief, even though on real score sequences they
                   necessarily fire on the same frame as A — a max over a
                   window can only cross a threshold at the same instant one
                   of its members does)
    C. persist   — production default: `friday.voice.wakeword.WakeScoreWindow`
                   — >=N frames in the last window above threshold
    D. ema       — exponential moving average of the score itself crosses
                   threshold (smooths a single spike out, rewards sustained
                   moderate confidence a raw/persist strategy might miss)
    E. hysteresis — two thresholds: score must first cross a high
                   "enter" threshold, then stay at/above a lower "sustain"
                   threshold for `sustain_frames` consecutive frames (dips
                   allowed as long as they don't go below the lower bound)

Because scoring is 100% deterministic given a saved WAV, every strategy x
threshold combination is evaluated for free against the same recordings —
nobody has to repeat "Hey Jarvis" once per candidate config.

Usage:
    python scripts/evaluate_wake_samples.py --samples-dir data/wakeword_samples/20260913_120000
    python scripts/evaluate_wake_samples.py --samples-dir ... --thresholds 0.2,0.3,0.4,0.5,0.6
    python scripts/evaluate_wake_samples.py --samples-dir ... --verifier-model data/models/wake_verifier.pkl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import BLOCK_SIZE, SAMPLE_RATE  # noqa: E402
from friday.voice.types import WakeWordBackendError  # noqa: E402
from friday.voice.wakeword import FRAME_S, WakeScoreWindow, WakeWordDetector  # noqa: E402

DEFAULT_THRESHOLDS = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]
POSITIVE_KINDS = {"wake_normal", "wake_quiet", "wake_loud", "wake_conversational", "wake_distance"}
NEGATIVE_KINDS = {"silence", "speech", "noise"}


@dataclass
class Clip:
    label: str
    kind: str
    audio: np.ndarray = field(repr=False)
    frame_scores: list[float] = field(default_factory=list)


def load_manifest(samples_dir: Path) -> list[Clip]:
    manifest = json.loads((samples_dir / "manifest.json").read_text(encoding="utf-8"))
    clips = []
    for entry in manifest["attempts"]:
        with wave.open(str(samples_dir / entry["wav"]), "rb") as wf:
            assert wf.getframerate() == SAMPLE_RATE, f"{entry['wav']}: unexpected sample rate {wf.getframerate()}"
            raw = wf.readframes(wf.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        clips.append(Clip(label=entry["label"], kind=entry["kind"], audio=audio))
    return clips


def score_clip(detector: WakeWordDetector, audio: np.ndarray) -> list[float]:
    """Resets the detector's internal state first so one clip's trailing
    context can never leak into the next (mirrors WakeWordDetector.reset()'s
    use in production between interactions).
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


# -- strategies --------------------------------------------------------------


def strategy_raw(scores: list[float], *, threshold: float, **_kw) -> tuple[bool, int | None]:
    for i, s in enumerate(scores):
        if s >= threshold:
            return True, i
    return False, None


def strategy_peak(scores: list[float], *, threshold: float, window_frames: int, **_kw) -> tuple[bool, int | None]:
    window: list[float] = []
    for i, s in enumerate(scores):
        window.append(s)
        if len(window) > window_frames:
            window.pop(0)
        if max(window) >= threshold:
            return True, i
    return False, None


def strategy_persist(
    scores: list[float], *, threshold: float, window_frames: int, persist_frames: int, **_kw
) -> tuple[bool, int | None]:
    win = WakeScoreWindow(threshold=threshold, window_frames=window_frames, persist_frames=persist_frames)
    for i, s in enumerate(scores):
        _, triggered = win.push(s)
        if triggered:
            return True, i
    return False, None


def strategy_ema(scores: list[float], *, threshold: float, alpha: float = 0.3, **_kw) -> tuple[bool, int | None]:
    ema = 0.0
    for i, s in enumerate(scores):
        ema = alpha * s + (1 - alpha) * ema
        if ema >= threshold:
            return True, i
    return False, None


def strategy_hysteresis(
    scores: list[float], *, threshold: float, sustain_ratio: float = 0.6, sustain_frames: int = 3, **_kw
) -> tuple[bool, int | None]:
    """`threshold` is the high "enter" bar; the low "sustain" bar is
    `threshold * sustain_ratio`. Once the high bar is crossed, the next
    `sustain_frames` consecutive frames (including the triggering one) must
    all stay at/above the low bar or the attempt resets and has to cross the
    high bar again.
    """
    low = threshold * sustain_ratio
    armed_at: int | None = None
    run = 0
    for i, s in enumerate(scores):
        if armed_at is None:
            if s >= threshold:
                armed_at = i
                run = 1
        else:
            if s >= low:
                run += 1
            else:
                armed_at = None
                run = 0
                continue
        if armed_at is not None and run >= sustain_frames:
            return True, i
    return False, None


STRATEGIES = {
    "raw": strategy_raw,
    "peak": strategy_peak,
    "persist": strategy_persist,
    "ema": strategy_ema,
    "hysteresis": strategy_hysteresis,
}


def evaluate_all(
    clips: list[Clip], *, strategy: str, threshold: float, window_frames: int, persist_frames: int,
) -> dict:
    fn = STRATEGIES[strategy]
    positives = [c for c in clips if c.kind in POSITIVE_KINDS]
    negatives = [c for c in clips if c.kind in NEGATIVE_KINDS]

    pos_results = [
        fn(c.frame_scores, threshold=threshold, window_frames=window_frames, persist_frames=persist_frames)
        for c in positives
    ]
    detected = sum(1 for triggered, _ in pos_results if triggered)
    latencies = [idx * FRAME_S for triggered, idx in pos_results if triggered and idx is not None]

    by_kind: dict[str, tuple[int, int]] = {}
    for c, (triggered, _) in zip(positives, pos_results):
        n_ok, n_total = by_kind.get(c.kind, (0, 0))
        by_kind[c.kind] = (n_ok + (1 if triggered else 0), n_total + 1)

    fp_by_kind: dict[str, tuple[int, int]] = {}
    for c in negatives:
        triggered, _ = fn(c.frame_scores, threshold=threshold, window_frames=window_frames, persist_frames=persist_frames)
        n_fp, n_total = fp_by_kind.get(c.kind, (0, 0))
        fp_by_kind[c.kind] = (n_fp + (1 if triggered else 0), n_total + 1)

    return {
        "strategy": strategy, "threshold": threshold,
        "window_frames": window_frames, "persist_frames": persist_frames,
        "detected": detected, "total": len(positives),
        "detection_rate": detected / max(1, len(positives)),
        "avg_latency_s": statistics.mean(latencies) if latencies else None,
        "median_latency_s": statistics.median(latencies) if latencies else None,
        "by_kind": {k: {"detected": v[0], "total": v[1]} for k, v in by_kind.items()},
        "false_positives_by_kind": {k: {"triggered": v[0], "total": v[1]} for k, v in fp_by_kind.items()},
        "total_false_positives": sum(v[0] for v in fp_by_kind.values()),
        "total_negatives": len(negatives),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples-dir", required=True)
    parser.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS))
    parser.add_argument("--window-s", type=float, default=CFG.voice.wakeword.window_s)
    parser.add_argument("--persist-frames", type=int, default=CFG.voice.wakeword.persist_frames)
    parser.add_argument("--strategies", default="raw,peak,persist,ema,hysteresis")
    parser.add_argument("--model", default=CFG.voice.wakeword.model)
    parser.add_argument("--verifier-model", default=None, help="Path to a trained verifier .pkl (see scripts/train_wake_verifier.py).")
    parser.add_argument("--verifier-threshold", type=float, default=CFG.voice.wakeword.verifier_threshold)
    parser.add_argument("--report", default=str(paths.DATA / "wakeword_evaluation_report.json"))
    parser.add_argument("--debug", action="store_true", help="Print each clip's raw per-frame score trace.")
    args = parser.parse_args()

    samples_dir = Path(args.samples_dir)
    clips = load_manifest(samples_dir)
    print(f"Loaded {len(clips)} clips from {samples_dir}")

    detector = WakeWordDetector(
        model_name=args.model,
        verifier_model_path=args.verifier_model,
        verifier_threshold=args.verifier_threshold,
    )
    try:
        detector.warm_up()
    except WakeWordBackendError as exc:
        print(f"FAILED: wake-word model unavailable ({exc})")
        return 1

    for c in clips:
        c.frame_scores = score_clip(detector, c.audio)
        if args.debug:
            trace = " ".join(f"{s:.2f}" for s in c.frame_scores)
            print(f"  [{c.kind}] {c.label!r}: {trace}")

    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    window_frames = max(1, round(args.window_s / FRAME_S))

    all_results = []
    print("\n" + "=" * 100)
    print(f"{'strategy':>10} {'threshold':>9} {'detected':>10} {'rate':>6} "
          f"{'avg_lat':>8} {'FP(silence)':>12} {'FP(speech)':>11} {'FP(noise)':>10}")
    print("-" * 100)
    for strategy in strategies:
        for threshold in thresholds:
            result = evaluate_all(
                clips, strategy=strategy, threshold=threshold,
                window_frames=window_frames, persist_frames=args.persist_frames,
            )
            all_results.append(result)
            fp = result["false_positives_by_kind"]
            silence_fp = fp.get("silence", {}).get("triggered", 0)
            silence_n = fp.get("silence", {}).get("total", 0)
            speech_fp = fp.get("speech", {}).get("triggered", 0)
            speech_n = fp.get("speech", {}).get("total", 0)
            noise_fp = fp.get("noise", {}).get("triggered", 0)
            noise_n = fp.get("noise", {}).get("total", 0)
            lat = f"{result['avg_latency_s']:.2f}s" if result["avg_latency_s"] is not None else "n/a"
            rate = f"{100 * result['detection_rate']:.0f}%"
            print(
                f"{strategy:>10} {threshold:>9.2f} {result['detected']:>4}/{result['total']:<3}{rate:>5} "
                f"{lat:>8} {silence_fp:>5}/{silence_n:<5}{speech_fp:>4}/{speech_n:<5}{noise_fp:>4}/{noise_n:<4}"
            )
    print("=" * 100)

    # Headline: production config (persist strategy, configured threshold)
    configured_threshold = CFG.voice.wakeword.threshold
    headline = evaluate_all(
        clips, strategy="persist", threshold=configured_threshold,
        window_frames=window_frames, persist_frames=args.persist_frames,
    )
    print(
        f"\n--- Currently configured production strategy (persist, threshold={configured_threshold}) ---"
    )
    print(f"Detection rate: {headline['detected']}/{headline['total']} ({100*headline['detection_rate']:.0f}%)")
    print("By kind:")
    for kind, d in headline["by_kind"].items():
        print(f"  {kind:>22}: {d['detected']}/{d['total']}")
    print(f"Avg detection latency: {headline['avg_latency_s']:.3f}s" if headline["avg_latency_s"] else "Avg detection latency: n/a")
    print(f"False positives: {headline['total_false_positives']}/{headline['total_negatives']}")

    report = {
        "samples_dir": str(samples_dir),
        "model": args.model,
        "verifier_model": args.verifier_model,
        "sweep": all_results,
        "headline_production_config": headline,
        "clips": [
            {"label": c.label, "kind": c.kind, "frame_scores": c.frame_scores, "peak_score": max(c.frame_scores) if c.frame_scores else 0.0}
            for c in clips
        ],
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nFull report (every strategy x threshold, every raw score trace) written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
