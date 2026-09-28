"""Deterministic Phase 8 tests — no microphone, no real wake-word/STT/TTS
model. Mirrors scripts/smoke_voice_pipeline.py's approach: hardware and
models are injected fakes; the brain/executor/session/confirmation path is
deliberately real, because the point of Phase 8 is that a wake-word/
follow-up conversation adds no second brain and no second confirmation
system — every turn still ends at the exact same `SESSION.handle()`.

For a real microphone + real wake-word model + real speakers, see
scripts/smoke_conversation.py instead.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from queue import Queue

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.voice.conversation import ConversationLoop  # noqa: E402
from friday.voice.fsm import ConversationFSM, State  # noqa: E402
from friday.voice.session import VoiceSession  # noqa: E402
from friday.voice.types import CaptureResult, TranscriptionResult  # noqa: E402
from friday.voice.wakeword import WakeWordDetector  # noqa: E402

SR = 16000


class FakeMicStream:
    """Stands in for friday.voice.capture.MicStream so tests never touch a
    real PortAudio device. Phase 10.X.3's barge-in watcher opens a mic
    session the instant SPEAKING starts (see ConversationLoop.
    _speaking_watcher_loop) — without this, every existing "speaking" state
    in this test file would otherwise try to open a real microphone stream
    as an unwanted side effect of wiring session.set_on_state(loop.
    _on_voice_state). One instance is fresh per _fake_get_mic_stream() call
    (i.e. per watcher start), matching the real get_mic_stream()'s per-device
    caching closely enough for these tests' purposes.
    """

    def __init__(self) -> None:
        self.sessions: list[Queue] = []

    def open_session(self) -> "Queue":
        q: Queue = Queue()
        self.sessions.append(q)
        return q

    def close_session(self, q: "Queue") -> None:
        pass


def _fake_get_mic_stream(_device: int | None) -> FakeMicStream:
    return FakeMicStream()


def speech_audio(seconds: float = 0.1) -> np.ndarray:
    return np.ones(int(SR * seconds), dtype=np.float32) * 0.5


class ScriptedWake:
    """Fake WakeWordDetector: hands back a scripted score per feed() call."""

    def __init__(self, scores: list[float | None]) -> None:
        self.model_name = "fake_wake"
        self._scores = list(scores)
        self.feed_calls: list[np.ndarray] = []
        self.reset_calls = 0
        self.warm_up_calls = 0

    def warm_up(self) -> None:
        self.warm_up_calls += 1

    def reset(self) -> None:
        self.reset_calls += 1

    def feed(self, block: np.ndarray) -> float | None:
        self.feed_calls.append(block)
        return self._scores.pop(0) if self._scores else None


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    """Same shape as smoke_voice_pipeline.py's fake — returns whatever score
    map is scripted for the current call.
    """

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, x: np.ndarray) -> dict[str, float]:
        self.calls += 1
        return {"fake_v0.1": 0.0}


def make_loop(
    *,
    capture,
    transcribe=lambda audio: TranscriptionResult(text="open chrome", ok=True),
    handle_text=lambda text: __import__("friday.registry", fromlist=["SkillResult"]).SkillResult(speech="Chrome is open."),
    speak=None,
    stop_speech=None,
    wake_scores: list[float | None] | None = None,
    wake_threshold: float = 0.5,
    follow_up_enabled: bool = True,
    wake_sound=None,
    wake_sound_warmup_s: float = 0.0,
    bargein_grace_s: float = 0.0,
    # Phase 10.X.6: default to 1 (old single-frame-trigger semantics) so
    # every pre-existing test in this file — none of which are testing the
    # new rolling-window/persistence feature itself — keeps triggering on
    # exactly one scripted score, matching what it asserts. The dedicated
    # rolling-window tests below pass a higher value explicitly.
    wake_window_s: float = 1.0,
    wake_persist_frames: int = 1,
) -> tuple[ConversationLoop, list[str]]:
    spoken: list[str] = []

    def default_speak(text: str, **_: object) -> bool:
        spoken.append(text)
        return True

    session = VoiceSession(
        capture=capture,
        transcribe=transcribe,
        handle_text=handle_text,
        speak=speak or default_speak,
        stop_speech=stop_speech,
    )
    wake = ScriptedWake(wake_scores or [])
    loop = ConversationLoop(
        session=session,
        wake_detector=wake,
        mic_device=None,
        wake_threshold=wake_threshold,
        wake_cooldown_s=0.0,
        follow_up_enabled=follow_up_enabled,
        follow_up_timeout_s=1.0,
        confirm_listen_timeout_s=1.0,
        post_tts_cooldown_s=0.0,
        wake_sound=wake_sound,
        wake_sound_warmup_s=wake_sound_warmup_s,
        # 0.0 by default so tests are deterministic and don't race real wall
        # time; the real production default (build_conversation_loop) is
        # 0.15s — see the dedicated grace-period test below for that path.
        bargein_grace_s=bargein_grace_s,
        wake_window_s=wake_window_s,
        wake_persist_frames=wake_persist_frames,
    )
    # Patch out real sleeping, and hardware mic access, so tests run
    # instantly/deterministically (Phase 10.X.3: the barge-in watcher opens a
    # mic session on every "speaking" state — see FakeMicStream above).
    import friday.voice.conversation as conv_mod

    conv_mod.time.sleep = lambda _s: None
    conv_mod.get_mic_stream = _fake_get_mic_stream
    return loop, spoken


def main() -> bool:
    overall = True
    overall &= _fsm_checks()
    overall &= _wakeword_detector_checks()
    overall &= _wake_score_window_checks()
    overall &= _voice_session_checks()
    overall &= _conversation_loop_checks()
    overall &= _rolling_window_integration_checks()
    overall &= _wake_queue_drain_checks()
    overall &= _strict_wake_word_checks()
    overall &= _barge_in_checks()

    store.init()
    overall &= asyncio.run(_async_checks())

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    return overall


def _fsm_checks() -> bool:
    print("\n--- ConversationFSM: deterministic state transitions ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    # 1. Full happy path, exactly as PLAN.md Phase 8B describes it.
    fsm = ConversationFSM()
    check("starts IDLE", fsm.state is State.IDLE)
    check("IDLE -> wake_detected -> WAKE_DETECTED", fsm.wake_detected() is State.WAKE_DETECTED)
    check("WAKE_DETECTED -> listening_started -> LISTENING", fsm.listening_started() is State.LISTENING)
    check("LISTENING -> capture_complete -> PROCESSING", fsm.capture_complete() is State.PROCESSING)
    check("PROCESSING -> transcribed -> EXECUTING", fsm.transcribed() is State.EXECUTING)
    check("EXECUTING -> result_ready -> SPEAKING", fsm.result_ready() is State.SPEAKING)
    check("SPEAKING -> speaking_done -> WAITING_FOR_FOLLOWUP", fsm.speaking_done() is State.WAITING_FOR_FOLLOWUP)
    check("WAITING_FOR_FOLLOWUP -> followup_timeout -> IDLE", fsm.followup_timeout() is State.IDLE)

    # 2. Follow-up speech skips the wake step entirely.
    fsm = ConversationFSM()
    fsm.state = State.WAITING_FOR_FOLLOWUP
    check(
        "WAITING_FOR_FOLLOWUP -> followup_speech_detected -> LISTENING (no wake needed)",
        fsm.followup_speech_detected() is State.LISTENING,
    )

    # 3. Hotkey path: straight from IDLE to LISTENING, no wake step.
    fsm = ConversationFSM()
    check("IDLE -> listening_started -> LISTENING (hotkey path)", fsm.listening_started() is State.LISTENING)

    # 4. Esc/cancellation: mid-cycle returns to the follow-up window, not all
    #    the way to idle; from idle/wake there's nothing to interrupt.
    fsm = ConversationFSM()
    fsm.state = State.SPEAKING
    check("cancelled() during SPEAKING -> WAITING_FOR_FOLLOWUP (TTS interruption)", fsm.cancelled() is State.WAITING_FOR_FOLLOWUP)
    fsm = ConversationFSM()
    fsm.state = State.LISTENING
    check("cancelled() during LISTENING -> WAITING_FOR_FOLLOWUP", fsm.cancelled() is State.WAITING_FOR_FOLLOWUP)
    fsm = ConversationFSM()
    check("cancelled() while IDLE -> IDLE (nothing to interrupt)", fsm.cancelled() is State.IDLE)

    # 5. No speech captured, from either entry point, goes to idle.
    fsm = ConversationFSM()
    fsm.state = State.LISTENING
    check("no_speech() from LISTENING -> IDLE", fsm.no_speech() is State.IDLE)
    fsm = ConversationFSM()
    fsm.state = State.WAITING_FOR_FOLLOWUP
    check("no_speech() from WAITING_FOR_FOLLOWUP -> IDLE (silent follow-up timeout)", fsm.no_speech() is State.IDLE)

    # 6. Error recovers back to idle rather than getting stuck.
    fsm = ConversationFSM()
    fsm.state = State.PROCESSING
    fsm.error("stt backend exploded")
    check("error() -> ERROR", fsm.state is State.ERROR)
    check("recovered() -> IDLE", fsm.recovered() is State.IDLE)

    # 7. Invalid transitions are ignored, not raised, and recorded as such.
    fsm = ConversationFSM()
    result = fsm.capture_complete()  # invalid from IDLE
    check("invalid transition (capture_complete from IDLE) is a no-op", result is State.IDLE)
    check("invalid transition recorded with applied=False", fsm.history[-1].applied is False)

    # 8. stop() is terminal.
    fsm = ConversationFSM()
    fsm.stop()
    check("stop() -> STOPPED", fsm.state is State.STOPPED)
    check("wake_detected() after stop() is ignored (terminal)", fsm.wake_detected() is State.STOPPED)

    # 9. Phase 10.X.3 barge-in: SPEAKING -> LISTENING directly, skipping
    #    WAKE_DETECTED/WAITING_FOR_FOLLOWUP entirely.
    fsm = ConversationFSM()
    fsm.state = State.SPEAKING
    check("barge_in_detected() during SPEAKING -> LISTENING directly", fsm.barge_in_detected() is State.LISTENING)
    for state in (State.IDLE, State.WAKE_DETECTED, State.LISTENING, State.PROCESSING, State.EXECUTING, State.WAITING_FOR_FOLLOWUP):
        fsm = ConversationFSM()
        fsm.state = state
        result = fsm.barge_in_detected()
        check(f"barge_in_detected() from {state} is ignored (only valid from SPEAKING)", result is state)

    return overall


def _wakeword_detector_checks() -> bool:
    print("\n--- WakeWordDetector: buffering + threshold scoring ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    class ScoreOnceModel:
        def __init__(self) -> None:
            self.models = {"fake_v0.1": None}
            self.predict_calls = 0

        def predict(self, x: np.ndarray) -> dict[str, float]:
            self.predict_calls += 1
            return {"fake_v0.1": 0.77}

    model = ScoreOnceModel()
    detector = WakeWordDetector(model_name="fake", model=model)

    # openWakeWord's native frame is 1280 samples; the persistent mic stream
    # delivers 480-sample blocks, so 2 blocks (960 samples) shouldn't trigger
    # a prediction yet, and the 3rd (>= 1280 total) should.
    r1 = detector.feed(np.zeros(480, dtype=np.float32))
    r2 = detector.feed(np.zeros(480, dtype=np.float32))
    check("no prediction yet below the 1280-sample frame size", r1 is None and r2 is None and model.predict_calls == 0)
    r3 = detector.feed(np.zeros(480, dtype=np.float32))
    check("prediction fires once >= 1280 samples buffered", model.predict_calls == 1 and r3 == 0.77)

    detector.reset()
    r4 = detector.feed(np.zeros(480, dtype=np.float32))
    check("reset() drops buffered audio (still short of a full frame after 1 block)", r4 is None)

    # Phase 10.X.6: diagnostics — RMS and a running frame count, both read by
    # ConversationLoop's debug logging.
    detector2 = WakeWordDetector(model_name="fake", model=ScoreOnceModel())
    loud = np.ones(1280, dtype=np.float32) * 0.5
    detector2.feed(loud)
    check("last_rms reflects the fed block's energy (not left at 0)", detector2.last_rms > 0.4)
    check("frames_processed increments once per completed 1280-sample frame", detector2.frames_processed == 1)
    detector2.feed(loud)
    check("frames_processed keeps counting across calls", detector2.frames_processed == 2)

    # Phase 10.X.6: reset() must also clear the underlying model's own
    # buffers (openWakeWord's Model.reset()), not just this adapter's
    # pre-frame sample buffer — see WakeWordDetector.reset()'s docstring for
    # why stale model-level context (up to ~10s) could otherwise survive a
    # reset and poison the next detection.
    class ResettableModel(ScoreOnceModel):
        def __init__(self) -> None:
            super().__init__()
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    resettable = ResettableModel()
    detector3 = WakeWordDetector(model_name="fake", model=resettable)
    detector3.reset()
    check("reset() propagates to the underlying model's own reset()", resettable.reset_calls == 1)

    # A model with no reset() (e.g. this file's own ScoreOnceModel/ScriptedWake
    # fakes elsewhere) must not crash the adapter's reset() — it's a
    # best-effort call, not a hard requirement of WakeModelLike.
    detector4 = WakeWordDetector(model_name="fake", model=ScoreOnceModel())
    try:
        detector4.reset()
        no_crash = True
    except Exception:
        no_crash = False
    check("reset() tolerates a model with no reset() method", no_crash)

    return overall


def _wake_score_window_checks() -> bool:
    """Phase 10.X.6: WakeScoreWindow — the rolling-window/persistence
    decision that replaces "one single 80ms frame must cross threshold".
    Pure logic, fed a scripted score sequence — no mic, no model, no
    ConversationLoop involved.
    """
    print("\n--- WakeScoreWindow: rolling-window + persistence decision ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    from friday.voice.wakeword import WakeScoreWindow

    # 1. Score below threshold never triggers, regardless of persistence.
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=1)
    _, triggered = w.push(0.3)
    check("a score below threshold does not trigger", not triggered)

    # 2. A single spike above threshold DOES trigger when persist_frames=1
    #    (matches the old, single-frame-trigger behavior).
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=1)
    _, triggered = w.push(0.9)
    check("a single score spike triggers with persist_frames=1", triggered)

    # 3. The same single spike does NOT trigger when persist_frames=2 —
    #    this is the false-positive guard a lower threshold leans on.
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=2)
    _, triggered = w.push(0.9)
    check("a single score spike does NOT trigger with persist_frames=2 (needs sustained evidence)", not triggered)

    # 4. Sustained evidence across multiple frames (as a real multi-frame
    #    "hey jarvis" utterance produces) does trigger once enough frames
    #    have crossed threshold.
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=2)
    peak1, triggered1 = w.push(0.6)
    peak2, triggered2 = w.push(0.7)
    check("sustained wake evidence (2nd qualifying frame) triggers", not triggered1 and triggered2)
    check("rolling peak reflects the highest score seen in the window", peak2 == 0.7)

    # 5. Rolling-window detection: evidence doesn't have to be contiguous —
    #    a dip below threshold between two qualifying frames still counts,
    #    as long as both are within the window.
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=2)
    w.push(0.6)   # frame 1: qualifies
    w.push(0.2)   # frame 2: a mid-utterance dip, doesn't reset progress
    _, triggered = w.push(0.55)  # frame 3: qualifies -> 2 total -> trigger
    check("rolling-window detection tolerates a non-qualifying frame in between", triggered)

    # 6. Once evidence ages out of the window (maxlen), it stops counting —
    #    a window_frames=2 window with persist_frames=2 needs the 2
    #    qualifying scores to both still be present.
    w = WakeScoreWindow(threshold=0.5, window_frames=2, persist_frames=2)
    w.push(0.6)                      # frame 1: qualifies
    w.push(0.1)                      # frame 2: doesn't qualify; frame 1 still in window
    _, triggered = w.push(0.1)       # frame 3: window is now [0.1, 0.1] -- frame 1 aged out
    check("evidence that ages out of the window stops counting toward persistence", not triggered)

    # 7. Threshold is configurable and actually changes the decision.
    w_strict = WakeScoreWindow(threshold=0.8, window_frames=10, persist_frames=1)
    _, triggered_strict = w_strict.push(0.6)
    w_loose = WakeScoreWindow(threshold=0.4, window_frames=10, persist_frames=1)
    _, triggered_loose = w_loose.push(0.6)
    check(
        "threshold is configurable: the same 0.6 score triggers at threshold=0.4 but not threshold=0.8",
        triggered_loose and not triggered_strict,
    )

    # 8. reset() clears all remembered evidence — a previous detection's
    #    scores must never contribute to the next one.
    w = WakeScoreWindow(threshold=0.5, window_frames=10, persist_frames=2)
    w.push(0.9)
    w.reset()
    _, triggered = w.push(0.9)
    check("reset() clears prior evidence (a single post-reset spike alone doesn't trigger at persist_frames=2)", not triggered)

    return overall


def _voice_session_checks() -> bool:
    print("\n--- VoiceSession: Phase 8 additions (max_duration override, silent timeout, locked cycles) ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    # max_duration_s is forwarded to the capture callable.
    seen_durations: list[float | None] = []

    def capture(max_duration_s: float | None = None) -> CaptureResult:
        seen_durations.append(max_duration_s)
        return CaptureResult(outcome="no_speech")

    vs = VoiceSession(
        capture=capture,
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
    )
    vs.run_cycle(max_duration_s=3.5)
    check("run_cycle(max_duration_s=3.5) reaches capture() unchanged", seen_durations == [3.5])

    # speak_on_no_speech=False suppresses the "I didn't catch that" TTS call.
    spoken: list[str] = []
    vs2 = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
        speak=lambda text, **_: spoken.append(text) or True,
    )
    result = vs2.run_cycle(speak_on_no_speech=False)
    check("speak_on_no_speech=False: no TTS on silent timeout", result is None and spoken == [])
    result2 = vs2.run_cycle(speak_on_no_speech=True)
    check("speak_on_no_speech=True (default): speaks 'I didn't catch that'", spoken == ["I didn't catch that."])

    # try_run_cycle_locked shares the busy-guard with activate()/_busy.
    vs3 = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
    )
    vs3._busy = True  # simulate a cycle already in progress (e.g. from activate())
    blocked = vs3.try_run_cycle_locked()
    check("try_run_cycle_locked() is skipped while another cycle is busy", blocked is None and vs3.busy is True)
    vs3._busy = False
    unblocked = vs3.try_run_cycle_locked()
    check("try_run_cycle_locked() runs normally once free", unblocked is None and vs3.busy is False)

    return overall


def _conversation_loop_checks() -> bool:
    print("\n--- ConversationLoop: wake gating, suppression, follow-up sequencing (legacy follow_up_enabled=True) ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    # -- suppression: while a cycle/follow-up is active, _tick must not feed
    #    the wake detector at all (Phase 8D) --------------------------------
    loop, _ = make_loop(capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"))
    loop.fsm.state = State.SPEAKING
    q: Queue = Queue()
    q.put(speech_audio())
    last = loop._tick(q, State.SPEAKING)
    check(
        "wake detector is never fed while state != IDLE (suppression)",
        loop._wake.feed_calls == [] and last is State.SPEAKING,
    )

    # -- false-wake / below-threshold suppression --------------------------
    loop, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        wake_scores=[0.1, 0.2],
        wake_threshold=0.5,
    )
    q = Queue()
    q.put(speech_audio())
    last = loop._tick(q, State.IDLE)
    check("a below-threshold score does not trigger a cycle", loop.fsm.state is State.IDLE)

    # -- wake trigger runs exactly one cycle, then cools down --------------
    calls = {"handle": 0}

    def handle_text(text: str):
        from friday.registry import SkillResult

        calls["handle"] += 1
        return SkillResult(speech="Chrome is open.")

    capture_calls: list[float | None] = []

    def capture(max_duration_s: float | None = None) -> CaptureResult:
        capture_calls.append(max_duration_s)
        if len(capture_calls) == 1:
            return CaptureResult(outcome="ok", audio=speech_audio())
        return CaptureResult(outcome="no_speech")  # follow-up: silence -> idle

    loop, spoken = make_loop(
        capture=capture,
        transcribe=lambda audio: TranscriptionResult(text="open chrome", ok=True),
        handle_text=handle_text,
        wake_scores=[0.9],
        wake_threshold=0.5,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check("wake score above threshold runs exactly one command", calls["handle"] == 1)
    check("the wake-triggered turn used the default (unoverridden) max duration", capture_calls[0] is None)
    check("the follow-up turn used follow_up_timeout_s, not the default", capture_calls[1] == 1.0)
    check("follow-up silence is quiet (no 'I didn't catch that')", spoken == ["Chrome is open."])
    check("back to IDLE after the whole interaction", loop.fsm.state is State.IDLE)
    # 2 resets, not 1: Phase 10.X.3's barge-in watcher also resets the buffer
    # when it starts scanning at the top of the one SPEAKING phase this
    # interaction has ("Chrome is open."), on top of the existing
    # end-of-interaction cooldown reset.
    check("wake buffer reset (cooldown + barge-in watcher start)", loop._wake.reset_calls == 2)

    # -- a real follow-up command (no wake word) also reaches handle_text --
    calls2 = {"handle": 0, "texts": []}

    def handle_text2(text: str):
        from friday.registry import SkillResult

        calls2["handle"] += 1
        calls2["texts"].append(text)
        return SkillResult(speech=f"did {text}")

    capture_calls2: list[float | None] = []
    transcripts = iter(["open chrome", "search for weather"])

    def capture2(max_duration_s: float | None = None) -> CaptureResult:
        capture_calls2.append(max_duration_s)
        if len(capture_calls2) <= 2:
            return CaptureResult(outcome="ok", audio=speech_audio())
        return CaptureResult(outcome="no_speech")

    loop2, spoken2 = make_loop(
        capture=capture2,
        transcribe=lambda audio: TranscriptionResult(text=next(transcripts, ""), ok=True),
        handle_text=handle_text2,
        wake_scores=[0.9],
        wake_threshold=0.5,
    )
    q2: Queue = Queue()
    q2.put(speech_audio())
    loop2._tick(q2, State.IDLE)
    check(
        "a follow-up command with no wake word still reaches handle_text (real SESSION path in prod)",
        calls2["texts"] == ["open chrome", "search for weather"],
    )
    check("3rd capture (silence) ends the conversation", len(capture_calls2) == 3)
    check("back to IDLE after two real turns", loop2.fsm.state is State.IDLE)

    # -- hotkey path also gets a follow-up window --------------------------
    hotkey_calls = {"n": 0}

    def hotkey_capture(max_duration_s: float | None = None):
        hotkey_calls["n"] += 1
        if hotkey_calls["n"] == 1:
            return CaptureResult(outcome="ok", audio=speech_audio())
        return CaptureResult(outcome="no_speech")

    loop3, spoken3 = make_loop(
        capture=hotkey_capture,
        transcribe=lambda audio: TranscriptionResult(text="open notepad", ok=True),
        handle_text=lambda text: __import__("friday.registry", fromlist=["SkillResult"]).SkillResult(speech="Notepad is open."),
    )
    loop3._hotkey_cycle()
    check(
        "Ctrl+Alt+V (hotkey) still works and also opens a follow-up window",
        spoken3 == ["Notepad is open."] and hotkey_calls["n"] == 2 and loop3.fsm.state is State.IDLE,
    )

    return overall


def _rolling_window_integration_checks() -> bool:
    """Phase 10.X.6: the rolling-window/persistence decision wired into
    ConversationLoop._tick(), not just WakeScoreWindow in isolation. Each
    `_tick()` call here delivers exactly one scripted score (ScriptedWake
    pops one per feed(), regardless of block size) — three separate ticks
    stand in for three consecutive ~80ms openWakeWord frames.
    """
    print("\n--- ConversationLoop wiring: rolling-window persistence gates the actual trigger ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    calls = {"n": 0}
    loop, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
        handle_text=lambda text: (calls.__setitem__("n", calls["n"] + 1) or __import__(
            "friday.registry", fromlist=["SkillResult"]).SkillResult(speech="done")),
        # frame 1 qualifies, frame 2 is a mid-utterance dip, frame 3 qualifies
        # again -- 2 qualifying frames total, satisfying persist_frames=2.
        wake_scores=[0.6, 0.2, 0.65],
        wake_threshold=0.5,
        wake_window_s=1.0,
        wake_persist_frames=2,
        follow_up_enabled=False,
    )

    state = State.IDLE
    for _ in range(2):
        q: Queue = Queue()
        q.put(speech_audio())
        state = loop._tick(q, state)
    check("one qualifying frame, then a dip, doesn't trigger yet (persist_frames=2 not met)", loop.fsm.state is State.IDLE and calls["n"] == 0)

    q = Queue()
    q.put(speech_audio())
    state = loop._tick(q, state)
    check("the 2nd qualifying frame (3rd tick overall) fires the trigger", calls["n"] == 1)
    check("back to IDLE after the interaction", loop.fsm.state is State.IDLE)

    # A lone spike below the persistence bar (persist_frames=2) never fires,
    # even though it would have under the old single-frame-trigger behavior.
    calls2 = {"n": 0}
    loop2, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
        handle_text=lambda text: (calls2.__setitem__("n", calls2["n"] + 1) or __import__(
            "friday.registry", fromlist=["SkillResult"]).SkillResult(speech="done")),
        wake_scores=[0.95],
        wake_threshold=0.5,
        wake_persist_frames=2,
        follow_up_enabled=False,
    )
    q2: Queue = Queue()
    q2.put(speech_audio())
    loop2._tick(q2, State.IDLE)
    check(
        "false-positive protection: a single noise spike doesn't trigger with persist_frames=2",
        calls2["n"] == 0 and loop2.fsm.state is State.IDLE,
    )

    return overall


def _wake_queue_drain_checks() -> bool:
    """Phase 10.X.6: `_enter_idle_cooldown` must discard whatever piled up in
    the main wake-scan queue during the interaction it just finished — the
    backlog bug described in that method's docstring (a wake-triggered
    interaction blocks the only thread that would otherwise drain this queue,
    so stale audio — the wake phrase, the chime, the command, FRIDAY's own
    TTS — accumulates there for the whole interaction+cooldown and would
    otherwise be replayed through the detector the instant scanning resumes).
    """
    print("\n--- Wake queue drain: no stale-audio replay after an interaction ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    loop, _ = make_loop(capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"))
    stale_q: Queue = Queue()
    for _ in range(25):
        stale_q.put(speech_audio())
    loop._wake_queue = stale_q
    check("queue has stale blocks before draining", stale_q.qsize() == 25)
    loop._drain_wake_queue()
    check("_drain_wake_queue() empties the queue", stale_q.empty())

    # _enter_idle_cooldown() (called at the end of every interaction) drains
    # it too, as part of returning to standby.
    loop._wake_queue = stale_q
    for _ in range(10):
        stale_q.put(speech_audio())
    loop._enter_idle_cooldown()
    check("_enter_idle_cooldown() also drains the wake queue before resuming scanning", stale_q.empty())

    # A wake-triggered interaction end-to-end: blocks that arrive WHILE the
    # interaction is running (simulating the mic stream continuing to push
    # audio during listen/STT/execute/speak) must not survive into the next
    # scan once the whole thing finishes.
    def capture_that_fills_the_queue(max_duration_s: float | None = None) -> CaptureResult:
        # Stands in for "the mic kept receiving audio during this capture" --
        # in production this happens because MicStream fans every block out
        # to every open session, including the wake loop's own.
        for _ in range(5):
            stale_q.put(speech_audio())
        return CaptureResult(outcome="no_speech")

    loop2, _ = make_loop(
        capture=capture_that_fills_the_queue,
        wake_scores=[0.9], wake_threshold=0.5, follow_up_enabled=False,
    )
    loop2._wake_queue = stale_q
    q: Queue = Queue()
    q.put(speech_audio())
    loop2._tick(q, State.IDLE)
    check(
        "audio buffered during a full wake-triggered interaction is discarded, not replayed",
        stale_q.empty(),
    )

    # A missing/never-started queue (e.g. wake-word disabled or mic
    # unavailable) is a safe no-op, not a crash.
    loop3, _ = make_loop(capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"))
    loop3._wake_queue = None
    try:
        loop3._drain_wake_queue()
        no_crash = True
    except Exception:
        no_crash = False
    check("_drain_wake_queue() with no queue set is a safe no-op", no_crash)

    return overall


def _strict_wake_word_checks() -> bool:
    """Phase 10.X.2: strict "Hey Jarvis" gating (follow_up_enabled=False,
    the production default). Mirrors the 10 scenarios from the phase spec.
    """
    print("\n--- Strict wake-word gating: follow_up_enabled=False (production default) ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    # 1/2/3. Normal speech / random phrase / wrong wake phrase while asleep:
    # none of them ever score above threshold, so the wake detector never
    # fires and SESSION.handle() (here: handle_text) is never called at all
    # -- the gate is the wake detector, nothing downstream ever sees the audio.
    for label, scores in (
        ("normal speech (\"open VS Code\") ignored while asleep", [0.05]),
        ("random phrase (\"hello\") ignored while asleep", [0.1]),
        ("wrong wake phrase (\"hey Friday\") ignored while asleep", [0.3]),
    ):
        calls = {"n": 0}
        loop, _ = make_loop(
            capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
            handle_text=lambda text: (calls.__setitem__("n", calls["n"] + 1) or __import__(
                "friday.registry", fromlist=["SkillResult"]).SkillResult(speech="done")),
            wake_scores=scores,
            wake_threshold=0.6,
            follow_up_enabled=False,
        )
        q: Queue = Queue()
        q.put(speech_audio())
        loop._tick(q, State.IDLE)
        check(label + " (SESSION.handle not called)", calls["n"] == 0 and loop.fsm.state is State.IDLE)

    # 4. Correct wake phrase -> wake detected, command listening starts.
    loop, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
        transcribe=lambda audio: TranscriptionResult(text="open vs code", ok=True),
        wake_scores=[0.9],
        wake_threshold=0.6,
        follow_up_enabled=False,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check("\"hey jarvis\" (score >= threshold) triggers wake_detected + a command cycle",
          loop.fsm.history[0].event == "wake_detected" and loop.fsm.history[0].applied)

    # 5. "Hey Jarvis, open VS Code" as one utterance: the wake detector only
    # ever reports a score, never text, so whatever the fresh post-wake
    # capture transcribes (here scripted as "open vs code") is exactly what
    # reaches handle_text -- the literal wake phrase never leaks through.
    seen_texts: list[str] = []

    def handle_text_capture(text: str):
        from friday.registry import SkillResult

        seen_texts.append(text)
        return SkillResult(speech="Opening VS Code.")

    loop, spoken = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
        transcribe=lambda audio: TranscriptionResult(text="open vs code", ok=True),
        handle_text=handle_text_capture,
        wake_scores=[0.95],
        wake_threshold=0.6,
        follow_up_enabled=False,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check(
        "\"Hey Jarvis, open VS Code\" in one breath: handle_text receives only \"open vs code\", never the wake phrase",
        seen_texts == ["open vs code"],
    )

    # 6/7/10. Strict mode: after a wake-triggered command finishes, NO
    # automatic follow-up listen happens (capture() is called exactly once,
    # not twice) -- a second command with no wake word must not execute --
    # and repeated independent "Hey Jarvis" activations each work on their own.
    capture_calls: list[int] = []

    def capture_counting(max_duration_s: float | None = None) -> CaptureResult:
        capture_calls.append(1)
        return CaptureResult(outcome="ok", audio=speech_audio())

    handled: list[str] = []
    transcripts = iter(["open vs code", "open chrome", "open notepad"])

    def handle_text_seq(text: str):
        from friday.registry import SkillResult

        handled.append(text)
        return SkillResult(speech=f"did {text}")

    loop, spoken = make_loop(
        capture=capture_counting,
        transcribe=lambda audio: TranscriptionResult(text=next(transcripts, ""), ok=True),
        handle_text=handle_text_seq,
        wake_scores=[0.9],
        wake_threshold=0.6,
        follow_up_enabled=False,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)  # "Hey Jarvis, open VS Code"
    check(
        "after one wake-triggered command, no follow-up capture happens (exactly 1 capture, not 2)",
        len(capture_calls) == 1 and handled == ["open vs code"] and loop.fsm.state is State.IDLE,
    )

    # 6. A second command spoken with NO wake word does nothing: without a
    # fresh wake score, _tick never calls capture()/handle_text at all.
    q = Queue()
    q.put(speech_audio())
    last = loop._tick(q, State.IDLE)  # no wake score queued -> nothing to feed a trigger
    check(
        "a second command spoken without \"Hey Jarvis\" is never captured or executed",
        len(capture_calls) == 1 and handled == ["open vs code"],
    )

    # 7. "Hey Jarvis, open Chrome" -- a fresh, independent activation -- works.
    loop._wake._scores = [0.9]  # queue the next wake score for the 2nd activation
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check(
        "\"Hey Jarvis, open Chrome\" as a fresh activation executes independently",
        handled == ["open vs code", "open chrome"] and loop.fsm.state is State.IDLE,
    )

    # 10. A third independent activation also works on its own.
    loop._wake._scores = [0.9]
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check(
        "a third independent \"Hey Jarvis\" activation also works (multiple consecutive interactions)",
        handled == ["open vs code", "open chrome", "open notepad"]
        and len(capture_calls) == 3
        and loop.fsm.state is State.IDLE,
    )

    # 8. Command timeout (silence during the wake-triggered listen itself,
    # not a follow-up) -> back to wake-word standby, nothing executed.
    timeout_handled = {"n": 0}
    loop, spoken = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        handle_text=lambda text: (timeout_handled.__setitem__("n", timeout_handled["n"] + 1) or __import__(
            "friday.registry", fromlist=["SkillResult"]).SkillResult(speech="done")),
        wake_scores=[0.9],
        wake_threshold=0.6,
        follow_up_enabled=False,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check(
        "command timeout (silence after wake) returns to wake standby without executing anything",
        timeout_handled["n"] == 0 and loop.fsm.state is State.IDLE,
    )

    # FSM-level: WAITING_FOR_FOLLOWUP -> IDLE immediately when follow-up is
    # disabled, via the existing followup_timeout() transition, with no VAD
    # listen (no capture() call) in between.
    capture_probe = {"calls": 0}

    def capture_should_not_be_called(max_duration_s: float | None = None) -> CaptureResult:
        capture_probe["calls"] += 1
        return CaptureResult(outcome="ok", audio=speech_audio())

    loop2, _ = make_loop(capture=capture_should_not_be_called, follow_up_enabled=False)
    loop2.fsm.state = State.WAITING_FOR_FOLLOWUP
    loop2._run_followup_loop()
    check(
        "_run_followup_loop() with follow_up_enabled=False goes straight WAITING_FOR_FOLLOWUP -> IDLE, no capture",
        loop2.fsm.state is State.IDLE and capture_probe["calls"] == 0,
    )

    # Ctrl+Alt+V (hotkey) also gets no follow-up window in strict mode.
    hotkey_calls = {"n": 0}

    def hotkey_capture_once(max_duration_s: float | None = None) -> CaptureResult:
        hotkey_calls["n"] += 1
        return CaptureResult(outcome="ok", audio=speech_audio())

    loop3, spoken3 = make_loop(
        capture=hotkey_capture_once,
        transcribe=lambda audio: TranscriptionResult(text="open notepad", ok=True),
        handle_text=lambda text: __import__("friday.registry", fromlist=["SkillResult"]).SkillResult(speech="Notepad is open."),
        follow_up_enabled=False,
    )
    loop3._hotkey_cycle()
    check(
        "hotkey activation in strict mode also gets no automatic follow-up (exactly 1 capture)",
        hotkey_calls["n"] == 1 and loop3.fsm.state is State.IDLE,
    )

    return overall


class CountingWakeSound:
    """Fake WakeSoundPlayer: counts .play() calls without touching real
    audio hardware — used to verify "exactly one chime per wake event"
    (regular and barge-in-triggered) without actually making noise on every
    test run.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.play_count = 0

    def play(self) -> None:
        self.play_count += 1


def _barge_in_checks() -> bool:
    """Phase 10.X.3: "Hey Jarvis" interrupts FRIDAY mid-SPEAKING. Combines
    deterministic, no-threading checks of the individual pieces
    (_run_cycle_with_bargein's loop-back, _speaking_watcher_loop called
    directly with a scripted queue) with one real-thread, real-timing
    end-to-end check that the whole wiring (ConversationLoop + VoiceSession +
    the actual _start_speaking_watcher/_stop_speaking_watcher thread
    lifecycle) produces the behavior described in the phase brief: FRIDAY
    stops speaking, and the very next command executes without a second
    wake word. Generous timeouts throughout — outcome doesn't depend on
    precise timing, only on the barge-in landing before the fake
    "speaking" call's own (short) natural-completion budget runs out.
    """
    print("\n--- Phase 10.X.3 barge-in: TTS interruption, wake sound, re-activation ---\n")
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    # 1. Wake sound plays exactly once for a normal (no barge-in) wake event.
    wake_sound = CountingWakeSound()
    loop, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=speech_audio()),
        transcribe=lambda audio: TranscriptionResult(text="open chrome", ok=True),
        wake_scores=[0.9],
        wake_threshold=0.6,
        follow_up_enabled=False,
        wake_sound=wake_sound,
    )
    q = Queue()
    q.put(speech_audio())
    loop._tick(q, State.IDLE)
    check("wake sound plays exactly once for a normal wake event", wake_sound.play_count == 1)
    check("normal wake event still ends back at IDLE", loop.fsm.state is State.IDLE)

    # 2. _run_cycle_with_bargein loops again (and re-plays the chime) for
    #    every cycle that ends with the barge-in flag set, and stops as soon
    #    as one doesn't.
    wake_sound2 = CountingWakeSound()
    call_count = {"n": 0}
    loop2, _ = make_loop(capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"), wake_sound=wake_sound2)

    def fake_try_run_cycle_locked(*, max_duration_s=None, speak_on_no_speech=True):
        call_count["n"] += 1
        if call_count["n"] <= 2:  # two chained barge-ins, then a normal end
            loop2._barge_in_event.set()
        return None

    loop2._session.try_run_cycle_locked = fake_try_run_cycle_locked  # type: ignore[method-assign]
    loop2._run_cycle_with_bargein()
    check("two chained barge-ins run three cycles total", call_count["n"] == 3)
    check("the chime replays once per barge-in re-activation (not the 3rd, non-barge-in cycle)", wake_sound2.play_count == 2)

    # 3. _speaking_watcher_loop, called directly (deterministic, no real
    #    thread): a queued wake-triggering block interrupts TTS and jumps
    #    the FSM straight SPEAKING -> LISTENING.
    stop_calls: list[int] = []
    loop3, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        wake_scores=[0.95], wake_threshold=0.6,
        stop_speech=lambda: stop_calls.append(1),
    )
    q3: Queue = Queue()
    q3.put(speech_audio())

    class OneQueueStream:
        def open_session(self) -> "Queue":
            return q3

        def close_session(self, _q: "Queue") -> None:
            pass

    import friday.voice.conversation as conv_mod

    conv_mod.get_mic_stream = lambda device: OneQueueStream()
    loop3.fsm.state = State.SPEAKING
    loop3._speaking_watcher_loop(threading.Event())
    check("barge-in watcher sets the barge-in event", loop3._barge_in_event.is_set())
    check("barge-in watcher calls the session's stop_speech_fn (interrupts TTS)", stop_calls == [1])
    check("barge-in watcher moves SPEAKING -> LISTENING directly (skips WAITING_FOR_FOLLOWUP)", loop3.fsm.state is State.LISTENING)

    # 4. Grace period: a score that arrives before bargein_grace_s elapses is
    #    ignored (guards against TTS's own startup pop/click false-triggering).
    q4: Queue = Queue()
    q4.put(speech_audio())  # arrives immediately -- well inside the grace window

    class GraceStream:
        def open_session(self) -> "Queue":
            return q4

        def close_session(self, _q: "Queue") -> None:
            pass

    loop4, _ = make_loop(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        wake_scores=[0.95], wake_threshold=0.6, bargein_grace_s=0.2,
    )
    conv_mod.get_mic_stream = lambda device: GraceStream()
    loop4.fsm.state = State.SPEAKING
    grace_stop = threading.Event()

    def _stop_after(delay_s: float, ev: threading.Event) -> None:
        threading.Event().wait(delay_s)  # a real wait, unaffected by the time.sleep patch above
        ev.set()

    threading.Thread(target=_stop_after, args=(0.12, grace_stop), daemon=True).start()
    loop4._speaking_watcher_loop(grace_stop)
    check("a wake score inside the grace window never triggers a barge-in", not loop4._barge_in_event.is_set())
    check("fsm stays SPEAKING (nothing scored the ignored block)", loop4.fsm.state is State.SPEAKING)

    # 5. End-to-end, real threads: "Hey Jarvis" barges into an in-progress
    #    TTS response; FRIDAY stops speaking and immediately captures +
    #    executes the next command with no second wake word.
    wake_sound5 = CountingWakeSound()
    capture_calls5: list[int] = []
    transcripts5 = iter(["open vs code", "open chrome"])
    handled5: list[str] = []

    def capture5(max_duration_s: float | None = None) -> CaptureResult:
        capture_calls5.append(1)
        return CaptureResult(outcome="ok", audio=speech_audio())

    def handle_text5(text: str):
        from friday.registry import SkillResult

        handled5.append(text)
        return SkillResult(speech=f"did {text}")

    speak_calls5 = {"n": 0}
    stop_flags5: list[threading.Event] = []

    def slow_speak5(text: str, **_: object) -> bool:
        speak_calls5["n"] += 1
        stop_ev = threading.Event()
        stop_flags5.append(stop_ev)
        interrupted = stop_ev.wait(0.3)  # "speaking"; a barge-in should land well inside this
        return not interrupted

    def stop_speech5() -> None:
        if stop_flags5:
            stop_flags5[-1].set()

    loop5, _ = make_loop(
        capture=capture5,
        transcribe=lambda audio: TranscriptionResult(text=next(transcripts5, ""), ok=True),
        handle_text=handle_text5,
        speak=slow_speak5,
        stop_speech=stop_speech5,
        wake_scores=[0.9],  # only the initial "Hey Jarvis" -- the barge-in score is injected below
        wake_threshold=0.6,
        follow_up_enabled=False,
        wake_sound=wake_sound5,
    )

    created_streams5: list[FakeMicStream] = []

    def _tracking_get_mic_stream(_device: int | None) -> FakeMicStream:
        stream = FakeMicStream()
        created_streams5.append(stream)
        return stream

    conv_mod.get_mic_stream = _tracking_get_mic_stream

    def _inject_bargein() -> None:
        for _ in range(200):
            if created_streams5 and created_streams5[-1].sessions:
                loop5._wake._scores = [0.95]  # the barge-in "Hey Jarvis" score
                created_streams5[-1].sessions[-1].put(speech_audio())
                return
            threading.Event().wait(0.01)

    threading.Thread(target=_inject_bargein, daemon=True).start()

    q5: Queue = Queue()
    q5.put(speech_audio())  # the initial "Hey Jarvis"
    loop5._tick(q5, State.IDLE)

    check(
        "the initial command AND the barge-in-triggered command both executed",
        handled5 == ["open vs code", "open chrome"],
    )
    check("TTS was actually interrupted mid-response (speak() called for both responses)", speak_calls5["n"] == 2)
    check(
        "wake sound played twice (initial 'Hey Jarvis' + the barge-in re-activation)",
        wake_sound5.play_count == 2,
    )
    check("back to wake-word standby (IDLE) after the whole interaction", loop5.fsm.state is State.IDLE)

    return overall


async def _async_checks() -> bool:
    """Confirmation-by-voice, without bypassing Session/Executor — and proof
    that a text-actor confirmation is untouched by the voice side-channel.
    """
    from friday.brain import BRAIN
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY, SkillResult, skill
    from friday.session import SESSION

    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    REGISTRY.discover()
    await asyncio.to_thread(BRAIN.warm)
    EXECUTOR.set_confirm_handler(SESSION._confirm)  # ensure the real channel, regardless of prior state

    print("\n--- confirmation by voice: no second wake word, no bypass ---\n")

    executed = {"ran": False}

    @skill(
        name="test.voice_conversation_confirm",
        tier="L3",
        description="test-only consequential action for the conversation-loop confirm side-channel",
    )
    def _consequential() -> SkillResult:
        executed["ran"] = True
        return SkillResult(speech="risky thing done")

    def make_confirm_loop(answer: str):
        spoken: list[str] = []
        captured: list[float | None] = []

        def capture(max_duration_s: float | None = None) -> CaptureResult:
            captured.append(max_duration_s)
            return CaptureResult(outcome="ok", audio=speech_audio())

        session = VoiceSession(
            capture=capture,
            transcribe=lambda audio: TranscriptionResult(text=answer, ok=True),
            handle_text=lambda text: SkillResult(speech="unused"),
            speak=lambda text, **_: spoken.append(text) or True,
        )
        loop = ConversationLoop(
            session=session, wake_detector=ScriptedWake([]), mic_device=None,
            wake_threshold=0.5, wake_cooldown_s=0.0, follow_up_enabled=False,
            follow_up_timeout_s=1.0, confirm_listen_timeout_s=1.0, post_tts_cooldown_s=0.0,
        )
        return loop, spoken, captured

    # -- approve: the still-suspended EXECUTOR.run call actually executes --
    loop, spoken, captured = make_confirm_loop("yes")
    from friday.bus import BUS

    BUS.subscribe("session.awaiting_confirm", loop._on_awaiting_confirm)
    try:
        result = await SESSION._run("test.voice_conversation_confirm", {}, actor="voice")
    finally:
        BUS.unsubscribe("session.awaiting_confirm", loop._on_awaiting_confirm)

    check(
        "voice answers 'yes' -> the ORIGINAL suspended call executes the skill",
        result.ok and executed["ran"] and "should i go ahead" in (spoken[0].lower() if spoken else ""),
    )
    check("the confirm answer was captured with confirm_listen_timeout_s, not the default", captured == [1.0])

    from friday import audit

    last = audit.recent(1)[0]
    check(
        "audit records actor=voice, decision=confirmed for the approved call",
        last["actor"] == "voice" and last["decision"] == "confirmed",
    )

    # -- decline: a spoken "no" cancels it, skill body never runs ----------
    executed["ran"] = False
    loop, spoken, _ = make_confirm_loop("no")
    BUS.subscribe("session.awaiting_confirm", loop._on_awaiting_confirm)
    try:
        result = await SESSION._run("test.voice_conversation_confirm", {}, actor="voice")
    finally:
        BUS.unsubscribe("session.awaiting_confirm", loop._on_awaiting_confirm)
    check("voice answers 'no' -> Cancelled, skill body never ran", not result.ok and not executed["ran"])

    # -- a "yes" doesn't ALSO get spoken as a separate, unrelated reply ----
    # (regression check for the fallthrough-to-brain-reparse bug this phase
    # fixed in friday/session.py's Session._resolve_pending)
    executed["ran"] = False
    loop, spoken, _ = make_confirm_loop("yes")
    BUS.subscribe("session.awaiting_confirm", loop._on_awaiting_confirm)
    try:
        await SESSION._run("test.voice_conversation_confirm", {}, actor="voice")
    finally:
        BUS.unsubscribe("session.awaiting_confirm", loop._on_awaiting_confirm)
    check(
        "exactly one thing was spoken (the confirm prompt) — no second, unrelated reply to 'yes'",
        len(spoken) == 1,
    )

    # -- text-mode confirmations are completely untouched -------------------
    print("\n--- text mode is unaffected: a typed command's confirmation doesn't wake voice ---\n")

    from friday.bus import Event

    loop, spoken, captured = make_confirm_loop("yes")
    text_event = Event(
        topic="session.awaiting_confirm",
        data={"actor": "text", "skill": "test.voice_conversation_confirm", "tier": "L3",
              "preview": "Do the thing", "speech": "Do the thing. Should I go ahead?"},
    )
    await loop._on_awaiting_confirm(text_event)
    check(
        "a text-actor confirmation event never triggers the voice capture/speak side-channel",
        spoken == [] and captured == [],
    )
    # (The inverse — a voice-actor event *does* react — is exactly what the
    # approve/decline/regression checks above already proved end to end;
    # not repeated here as a synthetic Event with no real Session.pending
    # behind it, which would fall through to a real, unbounded
    # BRAIN.understand("yes") against the live production skill set instead
    # of resolving anything — unsafe to do in an automated test.)

    return overall


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
