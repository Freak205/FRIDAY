"""Typed results and errors for the voice pipeline.

Expected outcomes (no speech, cancelled, hit the time cap) are plain data so
callers can branch on them without exception handling. Real failures
(microphone unavailable, the STT/TTS backend itself breaking) are exceptions,
mirroring how friday.llm splits ProviderUnavailable/ModelUnavailable from a
normal "no answer" response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

CaptureOutcome = Literal["ok", "max_duration", "cancelled", "no_speech"]


@dataclass(slots=True)
class CaptureResult:
    """What one listen-and-record cycle produced."""

    outcome: CaptureOutcome
    audio: np.ndarray | None = None  # float32 mono PCM @ 16kHz, present for ok/max_duration
    duration_s: float = 0.0
    # perf_counter() timestamps for diagnostics/latency measurement, keyed by
    # stage name: stream_ready, first_audio, speech_start, speech_end.
    # Populated on a best-effort basis — absent stages just mean that stage
    # never happened (e.g. no speech was ever detected).
    timings: dict[str, float] = field(default_factory=dict)
    # Phase 10.X.4: why VadSession actually stopped this capture — one of
    # "silence_timeout", "max_duration", "no_speech (too_short)",
    # "no_speech (max_duration, empty)", "cancelled", or
    # "block_source_exhausted" (the block source ended without the session
    # declaring a stop — only happens driving scripted/finite block lists,
    # never the real microphone). See friday.voice.capture.VadSession.feed.
    stop_reason: str = ""

    @property
    def has_audio(self) -> bool:
        return self.audio is not None and self.audio.size > 0


@dataclass(slots=True)
class TranscriptionResult:
    """What the STT engine produced for one utterance."""

    text: str
    ok: bool
    error: str | None = None
    latency_s: float = 0.0


class VoiceError(Exception):
    """Base class for voice-pipeline failures that aren't just 'no speech'."""


class MicrophoneUnavailable(VoiceError):
    """The configured input device couldn't be opened or read from."""


class SttBackendError(VoiceError):
    """The speech-to-text engine failed to load or to run."""


class TtsBackendError(VoiceError):
    """The text-to-speech engine failed to load or to run."""


class WakeWordBackendError(VoiceError):
    """The wake-word engine failed to load, download, or run. Never fatal to
    the app — see friday.gui.app._start_voice, which logs this and leaves
    Ctrl+Alt+V as the working fallback.
    """
