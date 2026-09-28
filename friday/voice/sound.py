"""Short activation chime played the instant "Hey Jarvis" is heard (Phase
10.X.3) — including mid-barge-in, when FRIDAY is interrupted while speaking.

Deliberately not a new audio dependency: playback reuses `sounddevice`
(already required for microphone capture, see friday/voice/capture.py) and
WAV decoding uses the stdlib `wave` module. The asset itself
(friday/assets/audio/wake_chime.wav) is procedurally synthesized —
see scripts/generate_wake_sound.py — not a copyrighted sample.

`WakeSoundPlayer.play()` is fire-and-forget: `sounddevice.play()` starts
playback on the output device and returns immediately, so it never delays
the capture that follows it (see ConversationLoop._play_wake_sound, which
also arms VadSession's `warmup_ignore_s` so the chime's own tail — likely
picked up faintly by the mic — can't be mistaken for the start of the next
command).
"""

from __future__ import annotations

import threading
import wave
from pathlib import Path

import numpy as np

from friday import paths
from friday.log import get

log = get(__name__)

DEFAULT_ASSET_PATH = paths.ROOT / "friday" / "assets" / "audio" / "wake_chime.wav"


class WakeSoundPlayer:
    """Lazily loads a short WAV and plays it on demand. Never raises —
    a missing/corrupt asset just means silence, never a crashed voice
    pipeline (mirrors WakeWordDetector/SttEngine/TtsEngine's own
    best-effort-load posture in this package).
    """

    def __init__(self, *, enabled: bool = True, path: str | Path | None = None) -> None:
        self.enabled = enabled
        self.path = Path(path) if path else DEFAULT_ASSET_PATH
        self._samples: np.ndarray | None = None
        self._sample_rate: int = 0
        self._load_failed = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> bool:
        if self._samples is not None:
            return True
        if self._load_failed:
            return False
        with self._lock:
            if self._samples is not None:
                return True
            if self._load_failed:
                return False
            try:
                with wave.open(str(self.path), "rb") as wf:
                    sample_rate = wf.getframerate()
                    channels = wf.getnchannels()
                    sample_width = wf.getsampwidth()
                    raw = wf.readframes(wf.getnframes())
                if sample_width != 2:
                    raise ValueError(f"expected 16-bit PCM, got {sample_width * 8}-bit")
                pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1:
                    pcm = pcm.reshape(-1, channels)
                self._samples = pcm
                self._sample_rate = sample_rate
            except Exception as exc:
                log.warning("wake sound asset unavailable at %s (%s) — playback disabled", self.path, exc)
                self._load_failed = True
                return False
        return True

    @property
    def duration_s(self) -> float:
        if not self._ensure_loaded() or self._sample_rate == 0:
            return 0.0
        return float(len(self._samples)) / self._sample_rate

    def play(self) -> None:
        """Fire-and-forget: starts playback and returns immediately without
        waiting for it to finish. Safe to call even if `enabled` is False or
        the asset failed to load — it's just a no-op then.
        """
        if not self.enabled or not self._ensure_loaded():
            return
        try:
            import sounddevice as sd

            sd.play(self._samples, self._sample_rate)
        except Exception:
            log.exception("wake sound playback failed")
