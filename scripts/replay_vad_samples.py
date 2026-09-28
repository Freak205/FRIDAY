"""Replays saved real-mic recordings (data/voice_test_samples/, written by
scripts/voice_mic_latency_test.py --save-samples) through the ACTUAL
production capture path — friday.voice.capture.drive() + VadSession, with
the real friday.voice.vad_model.SileroVad backend — to check VAD
start/stop behavior without needing a live microphone.

Phase 10.X.4: this is how the RMS-noise-floor bug (captures running to the
15s max_duration cap because this room's background noise sits too close to
the 0.012 RMS threshold — see friday/voice/vad_model.py's docstring) was
diagnosed and the Silero-VAD fix validated, entirely offline against the
recordings from the failing 20-utterance test. Re-run this any time
config.yaml's voice.stt VAD settings change, or after recording a fresh
batch of samples, to sanity-check the fix without a human re-running the
full mic test.

Usage:
    python scripts/replay_vad_samples.py
    python scripts/replay_vad_samples.py --samples-dir data/voice_test_samples
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import paths  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import BLOCK_SIZE, SAMPLE_RATE, drive  # noqa: E402


def load_blocks(wav_path: Path) -> list[np.ndarray]:
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"{wav_path} is not {SAMPLE_RATE}Hz"
        raw = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n_blocks = len(pcm) // BLOCK_SIZE
    return [pcm[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE] for i in range(n_blocks)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples-dir", default=str(paths.DATA / "voice_test_samples"))
    args = parser.parse_args()

    voice = CFG.voice.stt
    model = None
    speech_prob_fn = None
    vad_backend = "rms_only"
    if voice.vad_enabled:
        from friday.voice.vad_model import get_shared

        model = get_shared()
        if model is not None:
            speech_prob_fn = model.predict
            vad_backend = "silero"

    print(
        f"backend={vad_backend} vad_threshold={voice.vad_threshold} "
        f"rms_threshold={voice.silence_rms_threshold} silence_timeout_s={voice.silence_timeout_s} "
        f"max_recording_s={voice.max_recording_s} min_speech_s={voice.min_speech_s} "
        f"pre_roll_ms={voice.pre_roll_ms}\n"
    )

    samples_dir = Path(args.samples_dir)
    wavs = sorted(samples_dir.glob("*.wav"))
    if not wavs:
        print(f"no .wav files found in {samples_dir}")
        return 1

    pre_roll_blocks = max(0, round((voice.pre_roll_ms / 1000.0) / (BLOCK_SIZE / SAMPLE_RATE)))
    n_max_duration = 0
    durations: list[float] = []

    for wav_path in wavs:
        txt_path = wav_path.with_suffix(".txt")
        expected = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else ""
        blocks = load_blocks(wav_path)
        if model is not None:
            model.reset()  # fresh LSTM state per utterance
        result = drive(
            blocks,
            silence_timeout_s=voice.silence_timeout_s,
            max_duration_s=voice.max_recording_s,
            rms_threshold=voice.silence_rms_threshold,
            min_speech_s=voice.min_speech_s,
            pre_roll_blocks=pre_roll_blocks,
            speech_prob_fn=speech_prob_fn,
            vad_threshold=voice.vad_threshold,
            vad_sustain_threshold=voice.vad_sustain_threshold,
        )
        durations.append(result.duration_s)
        if result.outcome == "max_duration":
            n_max_duration += 1
        flag = " <-- max_duration" if result.outcome == "max_duration" else ""
        print(
            f"{wav_path.name:10s} {expected[:45]:45s} outcome={result.outcome:12s} "
            f"stop_reason={result.stop_reason:28s} duration_s={result.duration_s:5.2f}{flag}"
        )

    avg = sum(durations) / len(durations)
    print(f"\n{len(wavs)} samples: max_duration hit {n_max_duration}/{len(wavs)} times, "
          f"avg capture duration {avg:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
