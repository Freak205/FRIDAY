"""STT model/decode-parameter benchmark for Phase 7P (voice latency optimization).

Answers two questions with real, repeatable measurements instead of intuition:

  1. Which faster-whisper model size (tiny/base/small) is the smallest one
     that still transcribes normal English commands reliably on this machine?
  2. How much does beam_size affect latency, and does it cost accuracy?

Real human speech samples aren't available to an automated script, so this
uses a deterministic proxy that this project already validated once before
(see PLAN.md Phase 7 "LIVE-VERIFIED ON THIS MACHINE" — TTS->STT round trip):
synthesize each test phrase with the same local pyttsx3/SAPI engine FRIDAY
uses for its own voice, then feed that audio into faster-whisper. This is
NOT a substitute for a real microphone — it has none of a real room's noise,
mic frequency response, or human articulation variance — but it gives a
reproducible way to compare model sizes and decode settings against each
other on identical audio, and to catch a model that's simply too weak to
transcribe clear English at all. Real-microphone validation is a separate,
human-run step: see scripts/voice_mic_latency_test.py.

Usage:
    python scripts/voice_benchmark.py
    python scripts/voice_benchmark.py --models tiny,base,small --beam-sizes 1,5
"""

from __future__ import annotations

import argparse
import difflib
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.voice.stt import SttEngine  # noqa: E402

SR = 16000

# Covers: the exact phrases from the task brief, punctuation/capitalization
# variants, short/medium/long lengths, and a mid-sentence pause (a comma
# forces SAPI to insert a natural breath, exercising VAD tolerance when this
# same audio is used for capture-side testing).
TEST_PHRASES = [
    "Open Chrome.",
    "open chrome",
    "OPEN CHROME",
    "Open Notepad.",
    "Open VS Code.",
    "Inspect my FRIDAY project.",
    "What is my upcoming exam schedule?",
    "Open Chrome and search for weather.",
    "Read what's on my screen.",
    "Stop.",
    "No.",
    "Set a timer for ten minutes.",
    "What's the weather like today, and do I need an umbrella?",
    "Take a screenshot, save it to my desktop, and then open it in the photo viewer.",
    "Open Chrome, and then search for the weather.",  # mid-sentence pause
]


def _normalize(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum() or ch.isspace()).split()


def _similarity(expected: str, actual: str) -> float:
    a, b = _normalize(expected), _normalize(actual)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def synthesize(phrases: list[str], cache_dir: Path) -> dict[str, np.ndarray]:
    """Synthesizes each phrase once via pyttsx3/SAPI and resamples to 16kHz
    mono float32 — the format SttEngine.transcribe() expects from the real
    microphone path. Cached to disk so re-running the benchmark with
    different model/beam_size combos doesn't re-synthesize every time.
    """
    import pyttsx3

    cache_dir.mkdir(parents=True, exist_ok=True)
    audio: dict[str, np.ndarray] = {}
    for phrase in phrases:
        cache_path = cache_dir / f"{abs(hash(phrase))}.wav"
        if not cache_path.exists():
            # A fresh engine per phrase — reusing one pyttsx3/SAPI engine
            # across repeated save_to_file()+runAndWait() calls in a tight
            # loop reliably wedges the SAPI COM voice on this machine after
            # the first couple of calls (observed hang during this benchmark).
            engine = pyttsx3.init()
            engine.save_to_file(phrase, str(cache_path))
            engine.runAndWait()
            del engine
        with wave.open(str(cache_path), "rb") as wf:
            assert wf.getsampwidth() == 2, "expected 16-bit PCM from SAPI"
            src_sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if src_sr != SR:
            duration_s = pcm.size / src_sr
            n_out = int(round(duration_s * SR))
            src_t = np.linspace(0, duration_s, num=pcm.size, endpoint=False)
            dst_t = np.linspace(0, duration_s, num=n_out, endpoint=False)
            pcm = np.interp(dst_t, src_t, pcm).astype(np.float32)
        # Half a second of silence padding on each end mimics VAD's own
        # leading/trailing silence, closer to what SttEngine sees in production.
        pad = np.zeros(int(0.3 * SR), dtype=np.float32)
        audio[phrase] = np.concatenate([pad, pcm, pad])
    return audio


@dataclass
class Row:
    model: str
    beam_size: int
    phrase: str
    transcript: str
    similarity: float
    latency_s: float


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="tiny,base,small")
    parser.add_argument("--beam-sizes", default="1,5")
    parser.add_argument(
        "--cache-dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "cache" / "voice_benchmark"),
    )
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    beam_sizes = [int(b.strip()) for b in args.beam_sizes.split(",") if b.strip()]

    print(f"\nSynthesizing {len(TEST_PHRASES)} test phrases via pyttsx3/SAPI...")
    audio = synthesize(TEST_PHRASES, Path(args.cache_dir))
    print("Done.\n")

    rows: list[Row] = []
    load_times: dict[str, float] = {}

    for model_size in models:
        t0 = time.perf_counter()
        engine = SttEngine(model_size=model_size, device="cpu", compute_type="int8", beam_size=1)
        engine._ensure_model()  # force load now so it's excluded from per-phrase timing
        load_times[model_size] = time.perf_counter() - t0
        print(f"[{model_size}] model loaded in {load_times[model_size]:.2f}s")

        for beam_size in beam_sizes:
            engine.beam_size = beam_size
            for phrase, pcm in audio.items():
                result = engine.transcribe(pcm)
                sim = _similarity(phrase, result.text) if result.ok else 0.0
                rows.append(Row(model_size, beam_size, phrase, result.text, sim, result.latency_s))

    # -- report -----------------------------------------------------------
    print("\n" + "=" * 100)
    print(f"{'model':<8} {'beam':<5} {'avg_sim':<9} {'min_sim':<9} {'avg_lat_s':<11} {'max_lat_s':<11} {'load_s':<8}")
    print("-" * 100)
    summary = []
    for model_size in models:
        for beam_size in beam_sizes:
            subset = [r for r in rows if r.model == model_size and r.beam_size == beam_size]
            avg_sim = sum(r.similarity for r in subset) / len(subset)
            min_sim = min(r.similarity for r in subset)
            avg_lat = sum(r.latency_s for r in subset) / len(subset)
            max_lat = max(r.latency_s for r in subset)
            print(f"{model_size:<8} {beam_size:<5} {avg_sim:<9.3f} {min_sim:<9.3f} "
                  f"{avg_lat:<11.3f} {max_lat:<11.3f} {load_times[model_size]:<8.2f}")
            summary.append((model_size, beam_size, avg_sim, min_sim, avg_lat, max_lat))
    print("=" * 100)

    worst = sorted(rows, key=lambda r: r.similarity)[:8]
    print("\nWorst 8 transcriptions (lowest similarity to what was spoken):")
    for r in worst:
        print(f"  [{r.model} beam={r.beam_size}] {r.similarity:.2f}  {r.phrase!r} -> {r.transcript!r}")

    print("\nNote: audio is TTS-synthesized, not real human speech — this measures relative\n"
          "model speed/robustness on clear, clean English, not real-world mic accuracy.\n"
          "Real-microphone validation: scripts/voice_mic_latency_test.py (human-run).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
