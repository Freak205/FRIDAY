"""The conversational listening state machine — pure decision logic, no I/O.

Mirrors the split `friday.voice.capture` already uses between `VadSession`
(a pure state machine fed blocks one at a time) and `MicStream`/
`record_utterance` (the real PortAudio I/O): `ConversationFSM` here owns only
the *decision* of which state comes next given an event, so it's fully
unit-testable with zero microphone/model/thread involvement (see
scripts/smoke_voice_conversation.py). `friday.voice.conversation.
ConversationLoop` is the hardware-touching driver that feeds it real events.

States (PLAN.md Phase 8B):
    IDLE                  waiting for the wake word (or Ctrl+Alt+V)
    WAKE_DETECTED         wake phrase just matched, about to start listening
    LISTENING             VAD capturing the command
    PROCESSING            Whisper transcribing the captured audio
    EXECUTING             SESSION.handle() running (brain + skill + confirm)
    SPEAKING              TTS speaking the result
    WAITING_FOR_FOLLOWUP  short window where a new command needs no wake word
    STOPPED               shut down; terminal, no further transitions
    ERROR                 something in the pipeline failed; recovers to IDLE

Transitions are deliberately forgiving rather than exception-raising: an
event that doesn't apply to the current state (e.g. a stray wake-word score
arriving while SPEAKING, because the feed loop raced the state change by a
few milliseconds) is logged and ignored instead of crashing a background
thread. Tests assert this explicitly — see "ignored-transition" cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from friday.log import get

log = get(__name__)


class State(str, Enum):
    IDLE = "idle"
    WAKE_DETECTED = "wake_detected"
    LISTENING = "listening"
    PROCESSING = "processing"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    WAITING_FOR_FOLLOWUP = "waiting_for_followup"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(slots=True)
class Transition:
    """One state change, for tests/logging to assert against."""

    frm: State
    to: State
    event: str
    applied: bool = True  # False = the event was ignored (invalid from `frm`)


@dataclass(slots=True)
class ConversationFSM:
    state: State = State.IDLE
    history: list[Transition] = field(default_factory=list)

    # -- internal ---------------------------------------------------------

    def _go(self, event: str, to: State, *, valid_from: tuple[State, ...]) -> State:
        if self.state not in valid_from:
            log.debug(
                "conversation fsm: ignoring %r in state %s (valid from %s)",
                event, self.state, valid_from,
            )
            self.history.append(Transition(self.state, self.state, event, applied=False))
            return self.state
        frm = self.state
        self.state = to
        self.history.append(Transition(frm, to, event))
        return self.state

    # -- events -------------------------------------------------------------

    def wake_detected(self) -> State:
        """Wake phrase score crossed threshold while idle."""
        return self._go("wake_detected", State.WAKE_DETECTED, valid_from=(State.IDLE,))

    def listening_started(self) -> State:
        """Capture actually began (VAD session opened) after a wake, or a
        hotkey press — hotkey presses skip straight from IDLE, since there's
        no wake-detection step to report first.
        """
        return self._go(
            "listening_started", State.LISTENING,
            valid_from=(State.WAKE_DETECTED, State.IDLE),
        )

    def followup_speech_detected(self) -> State:
        """Speech started during the follow-up window — no wake word needed."""
        return self._go(
            "followup_speech_detected", State.LISTENING,
            valid_from=(State.WAITING_FOR_FOLLOWUP,),
        )

    def capture_complete(self) -> State:
        """VAD finished (silence after speech, or the time cap) with real
        audio to transcribe.
        """
        return self._go("capture_complete", State.PROCESSING, valid_from=(State.LISTENING,))

    def transcribed(self) -> State:
        """STT finished; handing the text to SESSION.handle()."""
        return self._go("transcribed", State.EXECUTING, valid_from=(State.PROCESSING,))

    def result_ready(self) -> State:
        """SESSION.handle() returned; about to speak the result."""
        return self._go("result_ready", State.SPEAKING, valid_from=(State.EXECUTING,))

    def speaking_done(self) -> State:
        """TTS finished (or was interrupted) — open the follow-up window."""
        return self._go(
            "speaking_done", State.WAITING_FOR_FOLLOWUP,
            valid_from=(State.SPEAKING,),
        )

    def barge_in_detected(self) -> State:
        """Wake phrase heard while FRIDAY was mid-SPEAKING (Phase 10.X.3).

        Jumps straight to LISTENING, skipping WAKE_DETECTED and
        WAITING_FOR_FOLLOWUP entirely — the caller is responsible for
        stopping TTS playback before/around calling this. Only valid from
        SPEAKING on purpose: if `speaking_done` already fired first (a normal
        TTS completion racing the detection by a few milliseconds), this is
        ignored rather than forced, since there's nothing left to barge into.
        """
        return self._go("barge_in_detected", State.LISTENING, valid_from=(State.SPEAKING,))

    def no_speech(self) -> State:
        """Capture ended with nothing said (silence, or too short to count).
        From a wake-triggered listen this goes back to idle (nothing to
        process); from a follow-up listen it's the normal timeout path,
        also idle.
        """
        return self._go(
            "no_speech", State.IDLE,
            valid_from=(State.LISTENING, State.WAITING_FOR_FOLLOWUP),
        )

    def followup_timeout(self) -> State:
        """No follow-up speech within the configured window."""
        return self._go("followup_timeout", State.IDLE, valid_from=(State.WAITING_FOR_FOLLOWUP,))

    def cancelled(self) -> State:
        """Esc pressed during capture or speech. Returns to the follow-up
        window rather than all the way to idle (Phase 8E: interruption
        should let the user immediately say something else, not force
        another wake word) — except while still idle/wake-detected, where
        there's nothing to interrupt yet.
        """
        if self.state in (State.IDLE, State.WAKE_DETECTED):
            return self._go("cancelled", State.IDLE, valid_from=(State.IDLE, State.WAKE_DETECTED))
        return self._go(
            "cancelled", State.WAITING_FOR_FOLLOWUP,
            valid_from=(
                State.LISTENING, State.PROCESSING, State.EXECUTING,
                State.SPEAKING, State.WAITING_FOR_FOLLOWUP,
            ),
        )

    def error(self, message: str = "") -> State:
        """Something in the pipeline raised (mic gone, STT/TTS backend
        error). Logged by the caller; the FSM just records that it happened.
        """
        log.warning("conversation fsm: error (%s) from state %s", message, self.state)
        frm = self.state
        self.state = State.ERROR
        self.history.append(Transition(frm, State.ERROR, f"error:{message}"))
        return self.state

    def recovered(self) -> State:
        """Only valid from ERROR — resume wake-word listening."""
        return self._go("recovered", State.IDLE, valid_from=(State.ERROR,))

    def stop(self) -> State:
        """Shut down. Terminal — no further transitions are valid."""
        frm = self.state
        self.state = State.STOPPED
        self.history.append(Transition(frm, State.STOPPED, "stop"))
        return self.state
