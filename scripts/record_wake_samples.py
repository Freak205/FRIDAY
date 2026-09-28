"""Phase 10.X.7 Step 5 — record a real, labelled wake-word evaluation corpus
from the actual microphone. Human-in-the-loop by necessity (a person has to
actually say "Hey Jarvis") — see scripts/voice_mic_latency_test.py and
scripts/test_wakeword_mic.py for the same constraint noted in earlier phases.

Records, as individual 16kHz mono WAV files plus a manifest.json describing
each one:

    20x "Hey Jarvis" at a normal conversational volume/distance
    10x "Hey Jarvis" spoken quietly
    10x "Hey Jarvis" spoken slightly louder than normal
    10x "Hey Jarvis" as part of a natural sentence ("Hey Jarvis, open Chrome")
    10x "Hey Jarvis" from a different distance than the previous ones
    30s of silence (ambient room noise only)
    30s of ordinary speech containing no wake word
    30s of keyboard/mouse/environment noise

Everything is kept as raw WAV (nothing scored here) so scripts/
evaluate_wake_samples.py can replay the exact same recordings through as
many detector configurations as needed without asking anyone to repeat a
single phrase.

Usage:
    python scripts/record_wake_samples.py
    python scripts/record_wake_samples.py --out-dir data/wakeword_samples/round1
    python scripts/record_wake_samples.py --skip-negatives   # positives only, faster re-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from queue import Empty

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import SAMPLE_RATE, get_mic_stream, warm_up_microphone  # noqa: E402
from friday.voice.types import MicrophoneUnavailable  # noqa: E402


@dataclass
class Plan:
    kind: str
    label: str
    duration_s: float


def build_plan() -> list[Plan]:
    plan: list[Plan] = []
    plan += [Plan("wake_normal", "Hey Jarvis", 3.0) for _ in range(20)]
    plan += [Plan("wake_quiet", "Hey Jarvis (quiet)", 3.0) for _ in range(10)]
    plan += [Plan("wake_loud", "Hey Jarvis (louder than normal)", 3.0) for _ in range(10)]
    conversational = [
        "Hey Jarvis, open VS Code",
        "Hey Jarvis, open Chrome",
        "Hey Jarvis, what's my status",
        "Hey Jarvis, read what's on my screen",
        "Hey Jarvis, go to WhatsApp",
        "Hey Jarvis, send him a message",
        "Hey Jarvis, cancel that",
        "Hey Jarvis, open Notepad",
        "Hey Jarvis, inspect my FRIDAY project",
        "Hey Jarvis, what time is it",
    ]
    plan += [Plan("wake_conversational", text, 3.5) for text in conversational]
    plan += [
        Plan("wake_distance", "Hey Jarvis (arm's length from laptop)", 3.0),
        Plan("wake_distance", "Hey Jarvis (across the desk)", 3.0),
        Plan("wake_distance", "Hey Jarvis (across the room)", 3.0),
        Plan("wake_distance", "Hey Jarvis (from another room / doorway)", 3.0),
        Plan("wake_distance", "Hey Jarvis (facing away from the laptop)", 3.0),
    ]
    plan += [Plan("wake_distance", "Hey Jarvis (repeat any distance you like)", 3.0) for _ in range(5)]
    return plan


NEGATIVES = [
    Plan("silence", "30s silence (ambient room noise, stay quiet)", 30.0),
    Plan(
        "speech",
        "30s ordinary speech — talk normally about anything, just never say "
        '"Hey Jarvis" or "Jarvis"',
        30.0,
    ),
    Plan("noise", "30s keyboard/mouse/environment noise (type, click, move things around)", 30.0),
]


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


def save_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--out-dir",
        default=str(paths.DATA / "wakeword_samples" / time.strftime("%Y%m%d_%H%M%S")),
    )
    parser.add_argument("--skip-negatives", action="store_true", help="Skip the silence/speech/noise clips.")
    parser.add_argument("--skip-positives", action="store_true", help="Skip the Hey Jarvis clips (negatives only).")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    device = CFG.voice.stt.input_device

    print("Warming up the microphone...")
    warm_up_microphone(device=device)
    stream = get_mic_stream(device)
    try:
        stream.ensure_started()
    except MicrophoneUnavailable as exc:
        print(f"FAILED: microphone unavailable ({exc})")
        return 1

    manifest: dict[str, list[dict]] = {"attempts": []}
    plan = (build_plan() if not args.skip_positives else []) + (NEGATIVES if not args.skip_negatives else [])
    total = len(plan)

    print(
        f"\nRecording {total} clips into {out_dir}\n"
        "For each: press Enter, wait for the countdown, then do exactly what's asked.\n"
        "Press Ctrl+C at any point to stop early — everything recorded so far is kept.\n"
    )

    for i, item in enumerate(plan, 1):
        try:
            if item.duration_s >= 10:
                ask(f'\n[{i}/{total}] Press Enter, then: {item.label} (~{item.duration_s:.0f}s)')
                countdown(2, "recording")
            else:
                ask(f'\n[{i}/{total}] Press Enter, then say: "{item.label}"')
                countdown(2, "recording")
        except KeyboardInterrupt:
            print("\nStopped early by user.")
            break

        audio = record_fixed(stream, item.duration_s)
        idx = len(manifest["attempts"]) + 1
        wav_name = f"{idx:03d}_{item.kind}.wav"
        save_wav(out_dir / wav_name, audio)
        manifest["attempts"].append({
            "index": idx, "kind": item.kind, "label": item.label,
            "wav": wav_name, "duration_s": item.duration_s,
        })
        print(f"    captured -> {wav_name}")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSaved {len(manifest['attempts'])} clips + manifest.json to {out_dir}")
    print(f"\nNext: python scripts/evaluate_wake_samples.py --samples-dir \"{out_dir}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
