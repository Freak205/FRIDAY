"""Synthesizes FRIDAY's original activation chime (Phase 10.X.3) — no
copyrighted movie/game audio, entirely procedural (numpy sine synthesis +
envelopes), written once to friday/assets/audio/wake_chime.wav.

Design (~0.7s total, three short layers, each windowed to avoid clicks):
  1. A soft low tone rising from 196Hz to 262Hz over the first ~180ms —
     "system waking".
  2. A clean two-note rising harmonic (E5 -> A5, a perfect fourth) starting
     around 140ms — "confirmation".
  3. A very quiet high shimmer (A6 + E7) fading in under the second note and
     decaying through the tail — "ready to listen".

Re-run this script any time the design should change; friday/voice/sound.py
just loads whatever WAV is at the configured path (voice.wake_sound_path,
blank = this file) at runtime, so nothing else needs to change.

Usage:
    python scripts/generate_wake_sound.py
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SR = 44100
OUT_PATH = Path(__file__).resolve().parent.parent / "friday" / "assets" / "audio" / "wake_chime.wav"


def _fade(n: int, fade_in: int, fade_out: int) -> np.ndarray:
    """Linear in/out envelope, 1.0 in the middle, avoiding clicks at the
    start/end of a synthesized tone."""
    env = np.ones(n, dtype=np.float32)
    fade_in = min(fade_in, n)
    fade_out = min(fade_out, n)
    if fade_in > 0:
        env[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out > 0:
        env[n - fade_out :] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    return env


def _tone(freq0: float, freq1: float, duration_s: float, *, sr: int = SR) -> np.ndarray:
    """A short sine sweep from freq0 to freq1 (freq0==freq1 for a flat tone),
    with its own fade-in/out envelope baked in.
    """
    n = int(sr * duration_s)
    t = np.linspace(0.0, duration_s, n, endpoint=False, dtype=np.float64)
    freq = np.linspace(freq0, freq1, n)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    wave_ = np.sin(phase).astype(np.float32)
    fade_n = max(1, int(sr * min(0.03, duration_s / 4)))
    return wave_ * _fade(n, fade_n, fade_n)


def _place(canvas: np.ndarray, layer: np.ndarray, start_s: float, *, sr: int = SR) -> None:
    start = int(sr * start_s)
    end = min(len(canvas), start + len(layer))
    if start >= len(canvas):
        return
    canvas[start:end] += layer[: end - start]


def build_chime() -> np.ndarray:
    total_s = 0.72
    canvas = np.zeros(int(SR * total_s), dtype=np.float32)

    # Layer 1: low rising warm tone — "waking".
    low = _tone(196.0, 262.0, 0.20) * 0.55
    _place(canvas, low, 0.0)

    # Layer 2: clean two-note confirmation motif, E5 -> A5 (perfect fourth).
    note1 = _tone(659.25, 659.25, 0.16) * 0.42
    note2 = _tone(880.00, 880.00, 0.22) * 0.40
    _place(canvas, note1, 0.14)
    _place(canvas, note2, 0.30)

    # Layer 3: very quiet high shimmer tail — "ready", decaying to silence.
    shimmer_n = int(SR * 0.30)
    t = np.linspace(0.0, 0.30, shimmer_n, endpoint=False)
    decay = np.exp(-t * 9.0).astype(np.float32)
    shimmer = (
        np.sin(2 * np.pi * 1760.0 * t) * 0.14
        + np.sin(2 * np.pi * 2637.0 * t) * 0.08
    ).astype(np.float32) * decay
    shimmer *= _fade(shimmer_n, int(SR * 0.01), int(SR * 0.05))
    _place(canvas, shimmer, 0.38)

    # Overall safety fade-out so the file never ends on a hard edge.
    canvas *= _fade(len(canvas), 0, int(SR * 0.05))

    peak = float(np.max(np.abs(canvas))) or 1.0
    canvas = (canvas / peak) * 0.85  # headroom, avoid clipping
    return canvas


def main() -> int:
    chime = build_chime()
    pcm16 = np.clip(chime * 32767.0, -32768, 32767).astype(np.int16)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT_PATH), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm16.tobytes())
    duration_s = len(pcm16) / SR
    print(f"wrote {OUT_PATH} ({duration_s:.3f}s, {SR}Hz mono 16-bit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
