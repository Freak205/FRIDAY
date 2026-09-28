"""Local speech-to-text via faster-whisper (CTranslate2) — no cloud, no PyTorch.

Two things worth knowing before touching this file:

1. faster-whisper's package `__init__.py` unconditionally imports PyAV (`av`)
   to decode arbitrary audio files. We never hit that path — FRIDAY always
   hands it a float32 numpy array straight from the microphone, never a file
   path, so `decode_audio()` is never called. On this dev machine PyAV's
   native DLL is sometimes refused by the local Application Control policy
   (observed: "An Application Control policy has blocked this file" loading
   av's audio-stream extension). Rather than touch that policy, `_ensure_av()`
   below tries the real import first and only installs a harmless stub module
   if it fails, so faster-whisper's top-level import succeeds either way.
2. CUDA is *detected* by ctranslate2 on this machine (an RTX 2050), but
   actually running on it needs the CUDA Toolkit's cuBLAS/cuDNN DLLs
   installed system-wide — confirmed missing here (`cublas64_12.dll is not
   found`). So the safe default is CPU (`device="cpu"`, `compute_type="int8"`);
   `transcribe()` falls back to CPU automatically if a GPU attempt fails.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any, Protocol

import numpy as np

from friday import paths
from friday.log import get
from friday.voice.normalize import normalize_transcript
from friday.voice.types import SttBackendError, TranscriptionResult
from friday.voice.vocabulary import build_hotwords

log = get(__name__)


def _ensure_av_importable() -> None:
    if "av" in sys.modules:
        return
    try:
        import av  # noqa: F401
    except Exception as exc:
        log.warning("PyAV unavailable (%s) — stubbing it; not needed for array input", exc)
        sys.modules["av"] = types.ModuleType("av")


class WhisperModelLike(Protocol):
    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> Any: ...


class SttEngine:
    """Lazily loads a faster-whisper model and transcribes numpy PCM arrays."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        language: str = "en",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        vad_filter: bool = False,
        initial_prompt: str = "",
        condition_on_previous_text: bool = True,
        vocabulary: list[str] | tuple[str, ...] | None = None,
        model: WhisperModelLike | None = None,
    ) -> None:
        self.model_size = model_size
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.initial_prompt = initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        # See friday/voice/vocabulary.py: a decode-time bias toward known
        # FRIDAY entity names, not a post-hoc text replacer. "" = no hint.
        self.hotwords = build_hotwords(vocabulary)
        self._model: WhisperModelLike | None = model
        self._lock = threading.Lock()

    def _load(self, device: str, compute_type: str) -> WhisperModelLike:
        _ensure_av_importable()
        from faster_whisper import WhisperModel

        download_root = str(paths.MODELS / "whisper")
        return WhisperModel(
            self.model_size, device=device, compute_type=compute_type,
            download_root=download_root,
        )

    def _ensure_model(self) -> WhisperModelLike:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                try:
                    self._model = self._load(self.device, self.compute_type)
                except Exception as exc:
                    raise SttBackendError(
                        f"couldn't load the '{self.model_size}' Whisper model: {exc}"
                    ) from exc
            return self._model

    def warm_up(self) -> None:
        """Best-effort model pre-load. Meant to run on a background thread
        right after the voice session is built (see build_voice_session) so
        the first real activation doesn't pay Whisper's load time (measured
        0.7-1.5s warm / much longer on first-ever download — see PLAN.md
        Phase 7P) on top of everything else. `transcribe()` calls
        `_ensure_model()` itself regardless, so a failure here just means
        the first real command pays the cost instead and surfaces the same
        SttBackendError it always would.
        """
        try:
            self._ensure_model()
        except SttBackendError as exc:
            log.warning("STT warm-up failed, will retry on first transcription: %s", exc)
            return
        log.info("STT: model=%s device=%s warmed up", self.model_size, self.device)

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe one utterance. Never raises — failures come back as
        `TranscriptionResult(ok=False, error=...)` so a voice session can
        speak an actionable message instead of crashing.
        """
        try:
            model = self._ensure_model()
        except SttBackendError as exc:
            return TranscriptionResult(text="", ok=False, error=str(exc))

        try:
            return self._run(model, audio)
        except Exception as exc:
            if self.device != "cpu":
                log.warning("STT failed on device=%s (%s); retrying on CPU", self.device, exc)
                try:
                    with self._lock:
                        self._model = self._load("cpu", "int8")
                    self.device, self.compute_type = "cpu", "int8"
                    return self._run(self._model, audio)
                except Exception as retry_exc:
                    return TranscriptionResult(text="", ok=False, error=str(retry_exc))
            return TranscriptionResult(text="", ok=False, error=str(exc))

    def _run(self, model: WhisperModelLike, audio: np.ndarray) -> TranscriptionResult:
        t0 = time.perf_counter()
        segments, _info = model.transcribe(
            audio,
            language=self.language or None,
            vad_filter=self.vad_filter,
            beam_size=self.beam_size,
            initial_prompt=self.initial_prompt or None,
            condition_on_previous_text=self.condition_on_previous_text,
            hotwords=self.hotwords or None,
        )
        raw_text = " ".join(seg.text.strip() for seg in segments).strip()
        text = normalize_transcript(raw_text)
        latency_s = time.perf_counter() - t0
        log.info(
            "stt inference: model=%s beam_size=%d vad_filter=%s hotwords=%r took %.3fs -> %r%s",
            self.model_size, self.beam_size, self.vad_filter, self.hotwords, latency_s, text,
            "" if text == raw_text else f" (raw: {raw_text!r})",
        )
        return TranscriptionResult(text=text, ok=True, latency_s=latency_s)
