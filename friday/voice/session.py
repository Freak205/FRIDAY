"""VoiceSession: one hotkey press -> one listen/transcribe/act/speak cycle.

This is deliberately thin. It calls out to `capture`, `transcribe`, and
`speak` (all injected, so it's testable without a microphone or TTS engine —
see scripts/smoke_voice_pipeline.py) and hands the transcript to
`handle_text`, which callers wire to the *existing* FRIDAY session
(`friday.session.SESSION.handle`) — this module adds no second brain, no
separate intent matching, and no separate confirmation system. Confirmation,
permission tiers, and audit logging all come along for free because the
voice path and the text command bar both end up calling the same
`SESSION.handle(..., actor=...)`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from friday.log import get
from friday.registry import SkillResult
from friday.voice.keys import esc_pressed
from friday.voice.summarize import for_speech
from friday.voice.types import CaptureResult, MicrophoneUnavailable, TranscriptionResult, TtsBackendError

log = get(__name__)

StateCallback = Callable[..., None]  # on_state(state: str, **data) -> None


class CaptureTuning:
    """Mutable, consume-once tuning applied to the *next* `capture()` call
    only (Phase 10.X.3). Exists so `friday.voice.conversation.ConversationLoop`
    can tell the real microphone capture "ignore the first N seconds of audio
    as a wake-sound warmup window" without changing `VoiceSession`'s generic
    `capture_fn` signature (`Callable[[float | None], CaptureResult]`), which
    every test fake and the hotkey path both rely on staying exactly that
    shape. See `friday.voice.__init__.build_voice_session` for where this is
    actually read, and `VoiceSession.set_pending_warmup` for how it's set.
    """

    def __init__(self) -> None:
        self.warmup_ignore_s: float = 0.0
        # Phase 10.X.7: see VoiceSession.set_pending_preroll.
        self.preroll_since: float | None = None

# Named stages logged by _log_timings, in the order they happen. Not every
# stage fires on every cycle (e.g. speech_start/speech_end are absent if no
# speech was ever detected) — missing ones are just skipped.
_TIMING_STAGES = [
    "listening", "stream_ready", "first_audio", "speech_start", "speech_end",
    "capture_done", "stt_start", "stt_end", "handle_start", "handle_end",
    "tts_start", "tts_end",
]


class VoiceSession:
    def __init__(
        self,
        *,
        capture: Callable[[float | None], CaptureResult],
        transcribe: Callable[..., TranscriptionResult],
        handle_text: Callable[[str], SkillResult],
        speak: Callable[..., bool] | None = None,
        stop_speech: Callable[[], None] | None = None,
        tts_enabled: Callable[[], bool] = lambda: True,
        max_speech_chars: int = 200,
        on_state: StateCallback | None = None,
        capture_tuning: CaptureTuning | None = None,
    ) -> None:
        self._capture = capture
        self._transcribe = transcribe
        self._handle_text = handle_text
        self._speak = speak
        self._stop_speech = stop_speech
        self._tts_enabled = tts_enabled
        self._max_speech_chars = max_speech_chars
        self._on_state = on_state or (lambda state, **data: None)
        self._capture_tuning = capture_tuning
        self._guard = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    # -- accessors for friday.voice.conversation.ConversationLoop -----------
    # It reuses these exact callables (never duplicates STT/TTS model
    # instances) for the follow-up window and confirmation-answer side
    # listen, which need to call capture/transcribe/speak directly rather
    # than through a full `run_cycle()`.

    @property
    def capture_fn(self) -> Callable[[float | None], CaptureResult]:
        return self._capture

    @property
    def transcribe_fn(self) -> Callable[..., TranscriptionResult]:
        return self._transcribe

    @property
    def speak_fn(self) -> Callable[..., bool] | None:
        return self._speak

    @property
    def stop_speech_fn(self) -> Callable[[], None] | None:
        return self._stop_speech

    @property
    def tts_enabled_fn(self) -> Callable[[], bool]:
        return self._tts_enabled

    def set_pending_warmup(self, seconds: float) -> None:
        """Tell the *next* `capture()` call to ignore up to `seconds` of
        leading audio before it can count as the start of speech (Phase
        10.X.3) — used right before a wake-sound-preceded listen so the
        chime itself can't be mistaken for the start (or the whole body) of
        the command that follows it. A no-op if this session wasn't built
        with a `CaptureTuning` (e.g. every existing test fake).
        """
        if self._capture_tuning is not None:
            self._capture_tuning.warmup_ignore_s = seconds

    def set_pending_preroll(self, since: float | None) -> None:
        """Tell the *next* `capture()` call to seed itself with whatever raw
        audio the persistent mic stream already buffered from `since` (a
        `time.perf_counter()` timestamp) onward (Phase 10.X.7) — used right
        after a wake-word/barge-in detection fires so the command-listen
        session that follows doesn't lose anything said in the gap between
        detection and this session actually opening. A no-op if this session
        wasn't built with a `CaptureTuning`.
        """
        if self._capture_tuning is not None:
            self._capture_tuning.preroll_since = since

    def set_on_state(self, on_state: StateCallback) -> None:
        """Replace the state callback after construction — used by
        `friday.voice.conversation.ConversationLoop` to interpose its own
        higher-level state machine between this session's per-cycle states
        and whatever the original caller (e.g. friday.gui's state hub)
        passed in, so both hotkey- and wake-triggered cycles feed the same
        conversation state machine.
        """
        self._on_state = on_state

    def _try_acquire(self) -> bool:
        with self._guard:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release(self) -> None:
        with self._guard:
            self._busy = False

    def activate(self, *, max_duration_s: float | None = None) -> None:
        """Start one cycle on a background thread.

        Safe to call from the hotkey thread (friday.hotkey's message-pump
        thread) — it never blocks, and a press while a cycle is already
        running is ignored rather than queued.
        """
        if not self._try_acquire():
            log.info(
                "VOICE CYCLE BUSY - SKIPPED (voice activation ignored — "
                "a cycle is already running)"
            )
            return
        threading.Thread(
            target=self._run, args=(max_duration_s,), daemon=True, name="voice-session"
        ).start()

    def _run(self, max_duration_s: float | None = None) -> None:
        try:
            self.run_cycle(max_duration_s=max_duration_s)
        except Exception:
            log.exception("voice cycle crashed")
            self._emit("error", message="internal error")
        finally:
            self._release()

    def try_run_cycle_locked(
        self, *, max_duration_s: float | None = None, speak_on_no_speech: bool = True
    ) -> SkillResult | None:
        """Like `activate()`, but synchronous (runs on the caller's own
        thread) and returns the result instead of firing a new one.

        For `friday.voice.conversation.ConversationLoop`, which already owns
        a dedicated background thread driving wake-word listening and wants
        to chain follow-up logic immediately after a cycle ends. Shares the
        same busy-guard as `activate()` so a Ctrl+Alt+V press that lands
        mid-conversation is safely ignored (rather than two threads racing
        the same microphone/TTS engine) and vice versa — a wake word heard
        while a hotkey-triggered cycle is running is likewise dropped.

        Returns None both when nothing was executed and when a concurrent
        cycle was already in progress; check `.busy` first if the caller
        needs to tell those apart.
        """
        if not self._try_acquire():
            log.info(
                "VOICE CYCLE BUSY - SKIPPED (conversation cycle skipped — "
                "a voice cycle is already running)"
            )
            return None
        try:
            return self.run_cycle(
                max_duration_s=max_duration_s, speak_on_no_speech=speak_on_no_speech
            )
        finally:
            self._release()

    def run_cycle(
        self, *, max_duration_s: float | None = None, speak_on_no_speech: bool = True
    ) -> SkillResult | None:
        """The full listen -> transcribe -> act -> speak cycle.

        `max_duration_s` overrides the configured max recording length for
        just this call — used for the Phase 8 follow-up/confirm-answer
        listens, which should give up much sooner than a fresh command
        would. `speak_on_no_speech` suppresses the "I didn't catch that"
        response for those same silent-timeout cases (Phase 8C: a follow-up
        window that times out should return to idle quietly, not nag).

        Returns the SkillResult that was acted on, or None if nothing was
        executed (cancelled, silence, or an STT failure).
        """
        log.info("VOICE CYCLE STARTED")
        timings: dict[str, float] = {"activate": time.perf_counter()}
        self._emit("listening")
        log.info("LISTENING STARTED")
        timings["listening"] = time.perf_counter()
        try:
            capture = self._capture(max_duration_s)
        except MicrophoneUnavailable as exc:
            log.info("CAPTURE RESULT: microphone unavailable (%s)", exc)
            self._emit("error", message=str(exc))
            self._speak_safe(f"I can't reach the microphone. {exc}")
            self._log_timings(timings)
            return None
        timings["capture_done"] = time.perf_counter()
        timings.update(capture.timings)
        log.info(
            "CAPTURE RESULT: outcome=%s has_audio=%s duration_s=%.2f",
            capture.outcome, capture.has_audio, capture.duration_s,
        )

        if capture.outcome == "cancelled":
            self._emit("cancelled")
            self._log_timings(timings)
            return None
        if not capture.has_audio:
            self._emit("empty")
            if speak_on_no_speech:
                self._speak_safe("I didn't catch that.")
            self._log_timings(timings)
            return None

        self._emit("processing")
        timings["stt_start"] = time.perf_counter()
        transcription = self._transcribe(capture.audio)
        timings["stt_end"] = time.perf_counter()
        log.info(
            "STT RESULT: ok=%s chars=%d latency_s=%.2f",
            transcription.ok, len(transcription.text or ""), transcription.latency_s,
        )
        if not transcription.ok:
            self._emit("error", message=transcription.error)
            self._speak_safe("Sorry, I couldn't understand the audio.")
            self._log_timings(timings)
            return None

        text = transcription.text.strip()
        if not text:
            self._emit("empty")
            if speak_on_no_speech:
                self._speak_safe("I didn't catch that.")
            self._log_timings(timings)
            return None

        self._emit("executing", text=text)
        log.info("COMMAND EXECUTION: %r", text)
        timings["handle_start"] = time.perf_counter()
        result = self._handle_text(text)
        timings["handle_end"] = time.perf_counter()
        log.info("SESSION RESULT: ok=%s", result.ok)

        speech = for_speech(result.speech or "Done.", max_chars=self._max_speech_chars)
        self._emit("speaking", text=speech, ok=result.ok)
        timings["tts_start"] = time.perf_counter()
        self._speak_safe(speech)
        timings["tts_end"] = time.perf_counter()
        self._emit("done", ok=result.ok)
        self._log_timings(timings)
        return result

    def _log_timings(self, timings: dict[str, float]) -> None:
        """Logs one line: per-stage deltas since the previous named stage,
        plus the cycle total. See PLAN.md Phase 7P for how this was used to
        find the pipeline's actual latency bottlenecks.
        """
        t0 = timings.get("activate")
        if t0 is None:
            return
        parts = []
        last = t0
        for key in _TIMING_STAGES:
            ts = timings.get(key)
            if ts is None:
                continue
            parts.append(f"{key}=+{ts - last:.3f}s")
            last = ts
        log.info("voice cycle timing: %s total=%.3fs", " ".join(parts), last - t0)

    def _speak_safe(self, text: str) -> None:
        if self._speak is None or not self._tts_enabled():
            return
        try:
            self._speak(text, cancel_check=esc_pressed)
        except TtsBackendError:
            log.exception("tts failed")
            self._emit("error", message="speech output failed")

    def _emit(self, state: str, **data: object) -> None:
        log.info("voice state -> %s %s", state, data if data else "")
        try:
            self._on_state(state, **data)
        except Exception:
            log.exception("voice on_state handler failed for %s", state)
