"""Voice interface: local speech-to-text + text-to-speech feeding the
existing FRIDAY session, not a second brain.

    microphone -> capture.record_utterance (local VAD)
               -> stt.SttEngine.transcribe   (faster-whisper, local)
               -> SESSION.handle(text, actor="voice")   <- same path as text
               -> tts.TtsEngine.speak        (pyttsx3 / SAPI, local)
               -> speaker

`build_voice_session()` is the single-activation entry point (Ctrl+Alt+V).
`friday.voice.conversation.build_conversation_loop()` (Phase 8) builds on top
of it for wake-word + follow-up conversation mode — see that module for the
full picture. Everything else in this package is importable directly for
testing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from friday.config import CFG
from friday.log import get
from friday.registry import SkillResult
from friday.voice.capture import record_utterance, warm_up_microphone
from friday.voice.session import CaptureTuning, StateCallback, VoiceSession
from friday.voice.stt import SttEngine
from friday.voice.tts import TtsEngine
from friday.voice.types import CaptureResult

log = get(__name__)

__all__ = ["VoiceSession", "SttEngine", "TtsEngine", "build_voice_session", "warm_up"]


def build_voice_session(
    *,
    handle_text: Callable[[str], SkillResult],
    on_state: StateCallback | None = None,
) -> VoiceSession:
    """Wire a VoiceSession from config.yaml's `voice:` block.

    Nothing here touches a microphone, loads a model, or opens the TTS engine
    — those all happen lazily on first use (see SttEngine/TtsEngine), so
    building this at startup can't fail because a dependency or device isn't
    available. `handle_text` should already carry the voice actor, e.g.
    `lambda text: backend.ask(text, actor="voice")`. Call `warm_up()`
    separately (on a background thread) to pay the microphone's PortAudio
    stream-open cost ahead of the first real activation instead of during it.
    """
    voice = CFG.voice
    stt = SttEngine(
        model_size=voice.stt.model,
        language=voice.stt.language,
        device=voice.stt.device,
        compute_type=voice.stt.compute_type,
        beam_size=voice.stt.beam_size,
        vad_filter=voice.stt.vad_filter,
        initial_prompt=voice.stt.initial_prompt,
        condition_on_previous_text=voice.stt.condition_on_previous_text,
        vocabulary=voice.stt.vocabulary,
    )
    tts = TtsEngine(voice_id=voice.tts.voice_id, rate=voice.tts.rate, volume=voice.tts.volume)
    threading.Thread(target=stt.warm_up, daemon=True, name="voice-stt-warmup").start()

    tuning = CaptureTuning()

    def capture(max_duration_s: float | None = None) -> CaptureResult:
        # Consume-once: a wake-sound-preceded listen sets this right before
        # calling into the session (see ConversationLoop._play_wake_sound),
        # every other capture (hotkey, follow-up, confirmation answer) sees 0.
        warmup_ignore_s, tuning.warmup_ignore_s = tuning.warmup_ignore_s, 0.0
        preroll_since, tuning.preroll_since = tuning.preroll_since, None
        return record_utterance(
            silence_timeout_s=voice.stt.silence_timeout_s,
            max_duration_s=max_duration_s if max_duration_s is not None else voice.stt.max_recording_s,
            rms_threshold=voice.stt.silence_rms_threshold,
            min_speech_s=voice.stt.min_speech_s,
            pre_roll_ms=voice.stt.pre_roll_ms,
            warmup_ignore_s=warmup_ignore_s,
            vad_enabled=voice.stt.vad_enabled,
            vad_threshold=voice.stt.vad_threshold,
            vad_sustain_threshold=voice.stt.vad_sustain_threshold,
            device=voice.stt.input_device,
            preroll_since=preroll_since,
        )

    return VoiceSession(
        capture=capture,
        transcribe=stt.transcribe,
        handle_text=handle_text,
        speak=tts.speak,
        stop_speech=tts.stop_current_speech,
        tts_enabled=lambda: CFG.voice.tts.enabled,
        max_speech_chars=voice.max_speech_chars,
        on_state=on_state,
        capture_tuning=tuning,
    )


def warm_up() -> None:
    """Best-effort pre-warm of the microphone stream, run on a background
    thread from friday.gui.app right after the voice hotkey is registered.
    Never blocks the caller and never raises — a failure here just means the
    first real Ctrl+Alt+V pays the (normally sub-second) mic-open cost
    instead, exactly like before Phase 7P.
    """
    threading.Thread(
        target=warm_up_microphone,
        kwargs={"device": CFG.voice.stt.input_device},
        daemon=True,
        name="voice-mic-warmup",
    ).start()

    if CFG.voice.stt.vad_enabled:
        from friday.voice.vad_model import warm_up as warm_up_vad_model

        threading.Thread(
            target=warm_up_vad_model, daemon=True, name="voice-vad-warmup",
        ).start()
