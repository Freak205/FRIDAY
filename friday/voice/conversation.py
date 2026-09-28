"""Phase 8 (+ Phase 10.X.2 strict gating): persistent conversational voice mode.

    microphone -> WakeWordDetector (lightweight, always running while idle)
               -> wake detected ("Hey Jarvis")
               -> VoiceSession.run_cycle()   <- same listen/STT/handle/speak
                                                 cycle Ctrl+Alt+V already uses
               -> follow_up_enabled? short VAD-only listen window (no wake
                  word) for one more turn : straight back to wake standby
               -> ... back to idle, waiting for "Hey Jarvis" again

This adds no second brain and no second confirmation system: every turn,
wake-triggered or follow-up, goes through the exact same
`VoiceSession.run_cycle()` (Phase 7) which calls the exact same
`SESSION.handle(text, actor="voice")` (see friday/voice/session.py and
friday/session.py). What's new here is purely *when* a cycle starts —
on a wake-word score crossing threshold, or (only if `follow_up_enabled`)
during a short post-response window — instead of only on Ctrl+Alt+V.

Phase 10.X.2: `follow_up_enabled` defaults to False (see
`friday.config.VoiceConfig`) so every independent command — wake-triggered
or Ctrl+Alt+V-triggered — requires its own fresh activation. The wake
detector (Phase A, below) is the only gate into command processing (Phase
B); nothing here sends ordinary background speech to STT/the brain while
`fsm.state is State.IDLE`. The confirmation side-channel (`_on_awaiting_
confirm`) is a separate, still-active mechanism — answering a pending
"...should I go ahead?" is part of the interaction that already unlocked,
not a new unwake-gated command path, so it is untouched by this flag.

`ConversationLoop` is the hardware-touching driver: one background thread
owns wake-word scanning and sequences cycles/follow-ups; a small async BUS
handler (`_on_awaiting_confirm`) is the one addition needed to make a
consequential-action confirmation ("...should I go ahead?") work by voice
without a second wake-up — see its docstring for exactly how that avoids
bypassing `Session`/`Executor`'s confirmation gate. The actual state
decisions live in `friday.voice.fsm.ConversationFSM`, kept separate so they
can be unit-tested without a microphone (scripts/smoke_voice_conversation.py).

Phase 10.X.3 (barge-in): the wake-scanning thread above is synchronous —
once it detects a wake word it calls straight into
`VoiceSession.try_run_cycle_locked()` and blocks there (listening through
speaking) for the whole interaction, which is *why* "Hey Jarvis" couldn't
interrupt FRIDAY mid-sentence before this phase: nothing was left scanning
the microphone while that thread sat inside the blocking TTS call. The fix
is a second, short-lived thread (`_speaking_watcher_loop`) spun up only for
the SPEAKING window, on its own mic-stream session, sharing the same
`WakeWordDetector` instance (safe without extra locking beyond `_wake_lock`,
since the two scanners are never active at the same time — the main thread
never calls `_wake.feed()` again until the whole interaction, watcher
included, is over). A trigger there calls `TtsEngine.stop_current_speech()`
(new in friday/voice/tts.py — sets a flag `_speak_now`'s loop already polls,
same shape as the existing Esc `cancel_check`) and moves the FSM straight
from SPEAKING to LISTENING (`ConversationFSM.barge_in_detected`), skipping
WAKE_DETECTED/WAITING_FOR_FOLLOWUP entirely. `_run_cycle_with_bargein` then
notices the interruption and starts a fresh command listen immediately
instead of falling through to the follow-up window/cooldown — see that
method's docstring. A short activation chime (`friday.voice.sound.
WakeSoundPlayer`) plays on every such detection, wake- or barge-in-triggered
alike, and its own tail is defended against by handing the next capture a
`warmup_ignore_s` window (`VoiceSession.set_pending_warmup`) rather than by
delaying when that capture starts.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from friday.bus import BUS, Event
from friday.config import CFG
from friday.log import get
from friday.voice.capture import get_mic_stream
from friday.voice.fsm import ConversationFSM, State
from friday.voice.keys import esc_pressed
from friday.voice.session import StateCallback, VoiceSession
from friday.voice.sound import WakeSoundPlayer
from friday.voice.types import MicrophoneUnavailable, TtsBackendError, WakeWordBackendError
from friday.voice.wakeword import FRAME_S, WakeScoreWindow, WakeWordDetector

log = get(__name__)

# Maps VoiceSession's own on_state names (see friday/voice/session.py) onto
# ConversationFSM transitions. "listening" is intentionally absent: both the
# wake path (wake_detected -> listening_started) and the follow-up path
# (waiting_for_followup -> followup_speech_detected) drive that specific
# transition themselves, since the *valid source state* differs and only
# the caller knows which path this cycle came from.
_TERMINAL_EVENTS = {
    "processing": "capture_complete",
    "executing": "transcribed",
    "speaking": "result_ready",
    "done": "speaking_done",
    "cancelled": "cancelled",
    "empty": "no_speech",
    "error": "error",
}


class ConversationLoop:
    """Owns wake-word scanning + follow-up sequencing on top of an existing
    `VoiceSession`. Safe-by-construction against double-activation: every
    cycle it starts goes through `VoiceSession.try_run_cycle_locked()`,
    which shares the same busy-guard `activate()` (the Ctrl+Alt+V path)
    uses — only one cycle, wake- or hotkey-triggered, ever runs at a time.
    """

    def __init__(
        self,
        *,
        session: VoiceSession,
        wake_detector: WakeWordDetector,
        mic_device: int | None,
        wake_threshold: float,
        wake_cooldown_s: float,
        follow_up_enabled: bool,
        follow_up_timeout_s: float,
        confirm_listen_timeout_s: float,
        post_tts_cooldown_s: float,
        on_state: StateCallback | None = None,
        wake_sound: WakeSoundPlayer | None = None,
        wake_sound_warmup_s: float = 0.0,
        bargein_grace_s: float = 0.15,
        wake_window_s: float = 1.0,
        wake_persist_frames: int = 2,
        wake_debug: bool = False,
    ) -> None:
        self._session = session
        self._wake = wake_detector
        self._mic_device = mic_device
        self._wake_threshold = wake_threshold
        self._wake_cooldown_s = wake_cooldown_s
        self._follow_up_enabled = follow_up_enabled
        self._follow_up_timeout_s = follow_up_timeout_s
        self._confirm_listen_timeout_s = confirm_listen_timeout_s
        self._post_tts_cooldown_s = post_tts_cooldown_s
        self._external_on_state = on_state or (lambda state, **data: None)
        self._wake_sound = wake_sound
        self._wake_sound_warmup_s = wake_sound_warmup_s
        # Phase 10.X.3: how long the barge-in watcher ignores wake scores
        # right after SPEAKING starts, so TTS's own startup click/pop can't
        # immediately false-trigger it.
        self._bargein_grace_s = bargein_grace_s
        self._wake_debug = wake_debug
        window_frames = max(1, round(wake_window_s / FRAME_S))
        # Phase 10.X.6: rolling-window wake decision (see WakeScoreWindow) —
        # one window for the main IDLE scan, a separate one for the barge-in
        # speaking-watcher since that's a distinct, shorter-lived scan
        # session with its own start/stop lifecycle.
        self._score_window = WakeScoreWindow(
            threshold=wake_threshold, window_frames=window_frames, persist_frames=wake_persist_frames,
        )
        self._bargein_score_window = WakeScoreWindow(
            threshold=wake_threshold, window_frames=window_frames, persist_frames=wake_persist_frames,
        )
        self._last_detection_at: float | None = None
        # perf_counter()-based twin of _last_detection_at, set alongside it at
        # every detection (wake or barge-in) — see MicStream._callback's
        # docstring note for why this needs perf_counter()'s resolution
        # instead of monotonic()'s.
        self._last_detection_preroll: float | None = None

        self.fsm = ConversationFSM()
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake_disabled = False  # set True if the detector fails to load
        # Phase 10.X.9: a WakeWordBackendError used to disable wake-word
        # listening for the rest of the process's life — but real logs from
        # this machine (2026-09-14) show it firing from a transient cause
        # (a Windows Application Control policy momentarily blocking a
        # native DLL the openwakeword/scipy stack loads, e.g. cython_blas)
        # that cleared itself on the very next process launch a couple of
        # minutes later. `_ensure_model()` doesn't cache a failed load, so a
        # later retry can succeed on its own — the bug was that nothing
        # asked it to. `_wake_retry_at`/`_wake_retry_backoff_s` implement
        # exponential backoff (5s, 10s, 20s, 40s, capped at 60s) instead of
        # a one-shot permanent disable, so a transient failure self-heals
        # within about a minute instead of requiring the user to notice
        # wake-word has gone silent and restart the app. See
        # `_wake_retry_ready()`/`_record_wake_failure()`/`_record_wake_success()`.
        self._wake_retry_at: float = 0.0
        self._wake_retry_backoff_s: float = 5.0
        # Guards every self._wake.feed()/.reset() call — the main wake-scan
        # thread and the barge-in speaking-watcher thread share one detector
        # instance but are never active at the same time (see module
        # docstring); this is belt-and-suspenders around that invariant.
        self._wake_lock = threading.Lock()
        self._barge_in_event = threading.Event()
        self._speaking_watcher_thread: threading.Thread | None = None
        self._speaking_watcher_stop: threading.Event | None = None
        # Phase 10.X.6: the main wake-scan queue, stashed here (not just a
        # local in `_wake_loop`) so `_enter_idle_cooldown` can drain it from
        # either the wake thread or the hotkey thread — see that method's
        # docstring for the backlog bug this fixes.
        self._wake_queue: "queue.Queue | None" = None

        # Re-wire the session's on_state to pass through here first so this
        # loop's FSM sees every cycle regardless of what triggered it
        # (wake word or the Ctrl+Alt+V hotkey) — see `_on_voice_state`.
        session.set_on_state(self._on_voice_state)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Non-blocking. Safe to call even if the wake-word model can't load
        or the microphone isn't available yet — failures are logged and
        this loop simply never detects a wake word; Ctrl+Alt+V (wired
        separately, see `activate_from_hotkey`) is unaffected either way.
        """
        if self._thread is not None:
            return
        self._running = True
        BUS.subscribe("session.awaiting_confirm", self._on_awaiting_confirm)
        self._thread = threading.Thread(target=self._wake_loop, daemon=True, name="voice-conversation")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        BUS.unsubscribe("session.awaiting_confirm", self._on_awaiting_confirm)
        self.fsm.stop()

    def activate_from_hotkey(self) -> None:
        """Bind this to the Ctrl+Alt+V hotkey instead of `session.activate`
        directly (see friday/desktop.py) so a hotkey-triggered command also
        gets the Phase 8 follow-up window afterward. Non-blocking — starts a
        background thread and returns immediately, same contract as
        `VoiceSession.activate()`.
        """
        log.info("HOTKEY RECEIVED (%s)", CFG.voice.activation_hotkey)
        threading.Thread(target=self._hotkey_cycle, daemon=True, name="voice-hotkey").start()

    def _hotkey_cycle(self) -> None:
        log.info("COMMAND LISTENING STARTED (hotkey activation, manual override)")
        self._run_cycle_with_bargein()
        self._run_followup_loop()
        self._enter_idle_cooldown()

    # -- wake-word scanning ---------------------------------------------------

    def _wake_loop(self) -> None:
        try:
            stream = get_mic_stream(self._mic_device)
            stream.ensure_started()
        except MicrophoneUnavailable as exc:
            log.warning("conversation loop: microphone unavailable (%s); wake-word listening disabled", exc)
            self._wake_disabled = True
            self.fsm.error(str(exc))
            self.fsm.recovered()
            self._emit_fsm()
            return

        self._wake.warm_up()
        q = stream.open_session()
        self._wake_queue = q
        last_state = self.fsm.state
        self._emit_fsm()
        log.info(
            "WAKE LISTENER STARTED (model=%s, threshold=%.2f, window_frames=%d, persist_frames=%d, "
            "follow_up_enabled=%s)",
            self._wake.model_name, self._wake_threshold, self._score_window.window_frames,
            self._score_window.persist_frames, self._follow_up_enabled,
        )
        log.info("WAITING FOR WAKE WORD")
        try:
            while self._running:
                last_state = self._tick(q, last_state)
        finally:
            self._wake_queue = None
            stream.close_session(q)

    def _tick(self, q: "queue.Queue", last_state: "State") -> "State":
        """One iteration of the wake-scanning loop. Split out from
        `_wake_loop` so it's directly unit-testable with a real `queue.Queue`
        and fake session/detector, no microphone thread or timing involved
        (see scripts/smoke_voice_conversation.py). Returns the state to diff
        against on the next call.
        """
        if self.fsm.state != last_state:
            last_state = self.fsm.state
            self._emit_fsm()

        if self.fsm.state is not State.IDLE:
            # A cycle (wake- or hotkey-triggered) or a follow-up window is in
            # progress — don't score audio, just drain this queue so it
            # doesn't fill (Phase 8D: suppress wake detection while
            # executing/speaking).
            self._drain_quietly(q)
            time.sleep(0.05)
            return last_state

        block = self._next_block(q)
        if block is None:
            return last_state
        if self._wake_disabled and not self._wake_retry_ready():
            return last_state

        try:
            with self._wake_lock:
                score = self._wake.feed(block)
        except WakeWordBackendError as exc:
            self._record_wake_failure(exc)
            return last_state
        self._record_wake_success()

        if score is not None:
            peak, triggered = self._score_window.push(score)
            self._log_wake_diagnostic(score, peak)
            if triggered:
                log.info(
                    "WAKE WORD DETECTED (model=%s, score=%.3f, rolling_peak=%.3f >= threshold=%.2f, "
                    "persist_frames=%d)",
                    self._wake.model_name, score, peak, self._wake_threshold, self._score_window.persist_frames,
                )
                self._last_detection_at = time.monotonic()
                self._last_detection_preroll = time.perf_counter()
                self._score_window.reset()
                self._on_wake_detected(self._last_detection_preroll)
                last_state = self.fsm.state
            else:
                # Not the ALL-CAPS lifecycle markers on purpose — this fires
                # roughly every 80ms while idle (one score per completed
                # openWakeWord frame), so it stays at DEBUG to avoid spamming
                # logs with every rejected frame while still being available
                # for diagnosing missed/false wakes.
                log.debug(
                    "wake score below threshold (model=%s, score=%.3f, rolling_peak=%.3f < %.2f)",
                    self._wake.model_name, score, peak, self._wake_threshold,
                )
        return last_state

    def _wake_retry_ready(self) -> bool:
        """True once a backed-off retry is due — see `_wake_retry_at`'s
        docstring in `__init__` for why this replaced a permanent disable.
        """
        return time.monotonic() >= self._wake_retry_at

    def _record_wake_failure(self, exc: WakeWordBackendError) -> None:
        was_active = not self._wake_disabled
        log.warning(
            "wake-word detector unavailable (%s); retrying in %.0fs (Ctrl+Alt+V is unaffected)",
            exc, self._wake_retry_backoff_s,
        )
        self._wake_disabled = True
        self._wake_retry_at = time.monotonic() + self._wake_retry_backoff_s
        self._wake_retry_backoff_s = min(self._wake_retry_backoff_s * 2, 60.0)
        if was_active:
            # GUI's wakeword status was otherwise only ever set once at
            # startup (see friday/gui/app.py's _start_voice) from whether
            # the scan *thread* launched, not whether the model actually
            # loaded — so a mid-run failure like this used to leave the
            # telemetry panel showing "active" while wake-word was silently
            # dead. StateHub.on_voice_state below turns this into a live
            # subsystem-status update.
            self._external_on_state("conversation.wake_unavailable")

    def _record_wake_success(self) -> None:
        if self._wake_disabled:
            log.info("wake-word detector recovered after a transient failure")
            self._external_on_state("conversation.wake_recovered")
        self._wake_disabled = False
        self._wake_retry_backoff_s = 5.0

    def _log_wake_diagnostic(self, score: float, peak: float) -> None:
        """Verbose per-frame diagnostics (voice.wakeword.debug) — score, the
        rolling peak, mic RMS, total frames scored, and time since the last
        detection, all at INFO so it's easy to watch live without cranking
        the whole app to DEBUG (see scripts/test_wakeword_mic.py --debug).
        """
        if not self._wake_debug:
            return
        since_last = (
            f"{time.monotonic() - self._last_detection_at:.1f}s" if self._last_detection_at else "n/a"
        )
        log.info(
            "WAKE DEBUG: score=%.3f peak=%.3f threshold=%.2f rms=%.4f frames=%d state=%s since_last=%s",
            score, peak, self._wake_threshold, getattr(self._wake, "last_rms", 0.0),
            getattr(self._wake, "frames_processed", 0), self.fsm.state.value, since_last,
        )

    def _enter_idle_cooldown(self) -> None:
        """Called after every full interaction ends (wake- or hotkey-
        triggered), regardless of how it ended, so a false trigger followed
        by silence gets the same cooldown as a real conversation. Drops any
        audio buffered in the wake detector while it was suppressed and
        waits `wake_cooldown_s` before the wake loop resumes scoring, so
        FRIDAY's own voice tail or room echo can't immediately re-trigger it
        (Phase 8D).

        Phase 10.X.6: also discards whatever piled up in the main wake-scan
        queue during the interaction. Root cause this fixes: a wake-
        triggered interaction runs `_on_wake_detected()` synchronously
        *inside* `_tick()` (see that method), so the wake thread's own while
        loop — the only thing that ever reads `self._wake_queue` — is
        blocked for the whole interaction (listening, STT, executing,
        speaking, chained barge-ins). The persistent MicStream keeps pushing
        fresh 30ms blocks into that queue the entire time regardless, so by
        the time control returns here the queue can hold many seconds of
        stale audio (the wake phrase itself, the chime, the command, FRIDAY's
        own TTS picked up by the mic). Without this drain, the very next
        `_tick()` calls would burn through that whole backlog through the
        wake detector before ever reaching live audio again — delaying real
        detection of the user's next "Hey Jarvis" and risking a spurious
        re-trigger from replayed TTS/chime audio. (The hotkey path doesn't
        hit this: its interaction runs on a separate thread, so this same
        wake thread keeps calling `_tick()` -> `_drain_quietly` throughout,
        continuously, and never lets a backlog form.)
        """
        with self._wake_lock:
            self._wake.reset()
        self._score_window.reset()
        log.info("RETURNING TO WAKE STANDBY (cooldown=%.1fs)", self._wake_cooldown_s)
        time.sleep(self._wake_cooldown_s)
        self._drain_wake_queue()

    def _drain_wake_queue(self) -> None:
        q = self._wake_queue
        if q is None:
            return
        dropped = 0
        try:
            while True:
                q.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        if dropped:
            log.debug(
                "wake queue: discarded %d stale block(s) buffered during the last interaction/cooldown",
                dropped,
            )

    def _next_block(self, q: "queue.Queue"):
        try:
            return q.get(timeout=0.2)
        except queue.Empty:
            return None

    def _drain_quietly(self, q: "queue.Queue") -> None:
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            return

    def _on_wake_detected(self, detected_at: float) -> None:
        self.fsm.wake_detected()
        self._emit_fsm()
        self._play_wake_sound(detected_at)
        log.info("COMMAND LISTENING STARTED (wake-triggered)")
        self._run_cycle_with_bargein()
        self._run_followup_loop()
        self._enter_idle_cooldown()

    def _run_cycle_with_bargein(self) -> None:
        """Runs one full cycle. If "Hey Jarvis" barges in on its SPEAKING
        phase (Phase 10.X.3 — see `_speaking_watcher_loop`), the interrupting
        wake word already counts as the next turn's activation, so this
        immediately starts a fresh command listen instead of falling through
        to the follow-up window/idle cooldown. Loops rather than recursing so
        a chain of barge-ins (interrupting the *next* response too) is
        handled the same way for as long as it keeps happening.
        """
        while True:
            self._barge_in_event.clear()
            self._session.try_run_cycle_locked()
            if not self._barge_in_event.is_set():
                return
            self._barge_in_event.clear()
            log.info("BARGE-IN: starting a fresh command listen")
            self._play_wake_sound(self._last_detection_preroll or time.perf_counter())

    def _play_wake_sound(self, detected_at: float) -> None:
        """Plays the activation chime (if configured/enabled), arms the
        *next* capture's warmup-ignore window so the chime's own tail can't
        be mistaken by VAD for the start of the command that follows it, and
        (Phase 10.X.7) seeds that same capture with whatever raw audio the
        mic stream already buffered since `detected_at` (a `time.perf_counter()`
        timestamp) — see
        VoiceSession.set_pending_preroll and MicStream.open_session for why:
        without this, "Hey Jarvis, open VS Code" said in one breath can lose
        "open VS Code" to the gap between detection firing and the new
        listen session actually opening. Called for every wake detection,
        wake- or barge-in-triggered alike.
        """
        self._session.set_pending_preroll(detected_at)
        if self._wake_sound is not None and self._wake_sound.enabled:
            self._wake_sound.play()
            self._session.set_pending_warmup(self._wake_sound_warmup_s)
        else:
            self._session.set_pending_warmup(0.0)

    # -- barge-in: wake-word scanning that survives TTS (Phase 10.X.3) ------

    def _start_speaking_watcher(self) -> None:
        if self._wake_disabled and not self._wake_retry_ready():
            return
        stop_event = threading.Event()
        self._speaking_watcher_stop = stop_event
        thread = threading.Thread(
            target=self._speaking_watcher_loop, args=(stop_event,),
            daemon=True, name="voice-bargein",
        )
        self._speaking_watcher_thread = thread
        thread.start()

    def _stop_speaking_watcher(self) -> None:
        stop_event = self._speaking_watcher_stop
        if stop_event is not None:
            stop_event.set()
        thread = self._speaking_watcher_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._speaking_watcher_thread = None
        self._speaking_watcher_stop = None

    def _speaking_watcher_loop(self, stop_event: threading.Event) -> None:
        """Runs concurrently with TTS, on its own mic-stream session, for as
        long as FRIDAY is SPEAKING — the main wake-scan thread
        (`_wake_loop`/`_tick`) is blocked inside this same cycle's TTS call
        for that whole window (see module docstring), so without this,
        nothing would be listening for a barge-in "Hey Jarvis" at all.
        Triggers at most once per SPEAKING window (stops itself right after).
        """
        try:
            stream = get_mic_stream(self._mic_device)
        except MicrophoneUnavailable:
            return
        with self._wake_lock:
            self._wake.reset()
        self._bargein_score_window.reset()
        q = stream.open_session()
        grace_until = time.monotonic() + self._bargein_grace_s
        try:
            while not stop_event.is_set():
                try:
                    block = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if time.monotonic() < grace_until:
                    continue
                with self._wake_lock:
                    try:
                        score = self._wake.feed(block)
                    except WakeWordBackendError as exc:
                        self._record_wake_failure(exc)
                        return
                    self._record_wake_success()
                if score is None:
                    continue
                peak, triggered = self._bargein_score_window.push(score)
                if self._wake_debug:
                    log.info(
                        "WAKE DEBUG (barge-in): score=%.3f peak=%.3f threshold=%.2f rms=%.4f",
                        score, peak, self._wake_threshold, getattr(self._wake, "last_rms", 0.0),
                    )
                if triggered:
                    log.info(
                        "BARGE-IN: wake word detected while SPEAKING (model=%s, score=%.3f, "
                        "rolling_peak=%.3f >= threshold=%.2f) -- interrupting TTS",
                        self._wake.model_name, score, peak, self._wake_threshold,
                    )
                    self._last_detection_at = time.monotonic()
                    self._last_detection_preroll = time.perf_counter()
                    self._barge_in_event.set()
                    stop_speech = self._session.stop_speech_fn
                    if stop_speech is not None:
                        stop_speech()
                    self.fsm.barge_in_detected()
                    self._emit_fsm()
                    return
        finally:
            stream.close_session(q)

    # -- follow-up window -------------------------------------------------

    def _run_followup_loop(self) -> None:
        if self.fsm.state is not State.WAITING_FOR_FOLLOWUP:
            return
        if not self._follow_up_enabled:
            # Phase 10.X.2: strict gating — every independent command needs
            # its own wake word (or hotkey press). Reuses the FSM's existing
            # WAITING_FOR_FOLLOWUP -> IDLE transition (`followup_timeout`)
            # rather than adding a new state; no VAD listen happens at all.
            log.info("follow-up window disabled (strict wake-word mode) -- returning to wake standby")
            self.fsm.followup_timeout()
            self._emit_fsm()
            return
        time.sleep(self._post_tts_cooldown_s)
        while self.fsm.state is State.WAITING_FOR_FOLLOWUP:
            self._emit_fsm()
            if self._session.busy:
                # Another activation (e.g. Ctrl+Alt+V) won the race for this
                # turn — wait briefly rather than spin; its own completion
                # will drive the FSM to whatever comes next.
                time.sleep(0.1)
                continue
            self._session.try_run_cycle_locked(
                max_duration_s=self._follow_up_timeout_s, speak_on_no_speech=False,
            )

    # -- bridging VoiceSession's per-cycle states onto the FSM ---------------

    def _on_voice_state(self, state: str, **data: object) -> None:
        if state == "listening":
            if self.fsm.state in (State.WAKE_DETECTED, State.IDLE):
                # WAKE_DETECTED: normal wake path. IDLE: a hotkey press
                # starts a cycle with no wake step to report first.
                self.fsm.listening_started()
            elif self.fsm.state is State.WAITING_FOR_FOLLOWUP:
                self.fsm.followup_speech_detected()
        elif state == "speaking":
            event = _TERMINAL_EVENTS.get(state)
            if event is not None:
                getattr(self.fsm, event)()
            # Phase 10.X.3: start scanning for a barge-in "Hey Jarvis" for as
            # long as this SPEAKING phase lasts — see _speaking_watcher_loop.
            self._start_speaking_watcher()
        elif state in ("done", "error"):
            # Stop the barge-in watcher before applying the terminal
            # transition below — if it already fired (fsm.state is LISTENING
            # via barge_in_detected()), the transitions here are naturally
            # ignored/no-ops rather than fighting that (ConversationFSM
            # transitions are forgiving-by-design, see fsm.py).
            self._stop_speaking_watcher()
            event = _TERMINAL_EVENTS.get(state)
            if event == "error":
                self.fsm.error(str(data.get("message", "")))
                self.fsm.recovered()
            elif event is not None:
                getattr(self.fsm, event)()
        else:
            event = _TERMINAL_EVENTS.get(state)
            if event is not None:
                getattr(self.fsm, event)()
        self._emit_fsm()
        self._external_on_state(state, **data)

    def _emit_fsm(self) -> None:
        self._external_on_state(f"conversation.{self.fsm.state.value}")

    # -- confirmation side-channel --------------------------------------------

    async def _on_awaiting_confirm(self, event: Event) -> None:
        """Makes an L2/L3 confirmation ("...should I go ahead?") work by
        voice without a second wake word.

        `Session._confirm` (friday/session.py) parks the pending decision on
        a future and *awaits* it — the original `SESSION.handle()` call
        (still running, deep inside `EXECUTOR.run`) stays suspended there
        until something resolves it. This handler is that something: it
        speaks the prompt, listens for a short answer, and feeds the answer
        through `SESSION.handle()` again — the exact mechanism a second
        utterance already uses to resolve `Session.pending` (see
        `Session._resolve_pending`). Nothing here touches the confirm gate
        itself, adds a new decision path, or lets a "yes" skip
        `Executor.run`'s policy check; it's just how the "next utterance"
        that satisfies a pending confirmation gets captured when there's no
        text prompt for the user to type into.

        Runs as a BUS handler, which `EventBus.publish` awaits inline as
        part of the same coroutine that's about to `await` the confirm
        future — so this all completes *before* that await even starts,
        and `SESSION.handle(answer, ...)` below can be awaited directly
        (same event loop, no cross-thread dispatch needed, unlike the
        `backend.ask()` used everywhere else in friday/desktop.py).

        Only reacts to confirmations raised by a voice-actor call (Phase 8G:
        a WhatsApp-send-style command must still pause for confirmation, but
        a confirmation raised by a *typed* command must not suddenly start
        talking over the user) — text/CLI confirmations are untouched.
        """
        if event.data.get("actor") != "voice":
            return

        speech = str(event.data.get("speech") or "Should I go ahead?")
        await asyncio.to_thread(self._speak_prompt, speech)

        capture = self._session.capture_fn
        transcribe = self._session.transcribe_fn
        capture_result = await asyncio.to_thread(capture, self._confirm_listen_timeout_s)
        if not capture_result.has_audio:
            log.info("confirmation answer: no speech heard within %.1fs; original 60s timeout still applies", self._confirm_listen_timeout_s)
            return

        transcription = await asyncio.to_thread(transcribe, capture_result.audio)
        text = transcription.text.strip() if transcription.ok else ""
        if not text:
            log.info("confirmation answer: transcription empty/failed")
            return

        log.info("confirmation answer heard: %r", text)
        from friday.session import SESSION

        await SESSION.handle(text, actor="voice")  # resolves Session.pending; result intentionally unspoken

    def _speak_prompt(self, text: str) -> None:
        speak = self._session.speak_fn
        if speak is None or not self._session.tts_enabled_fn():
            return
        try:
            speak(text, cancel_check=esc_pressed)
        except TtsBackendError:
            log.exception("tts failed while speaking a confirmation prompt")


def build_conversation_loop(
    *,
    handle_text: Callable[[str], object],
    on_state: StateCallback | None = None,
) -> ConversationLoop:
    """Wire a ConversationLoop from config.yaml's `voice:` block, on top of
    the same VoiceSession Ctrl+Alt+V uses (built via `build_voice_session`)
    so STT/TTS models are loaded exactly once regardless of how many
    activation paths exist. See friday/desktop.py for the caller.
    """
    from friday.voice import build_voice_session

    voice = CFG.voice
    session = build_voice_session(handle_text=handle_text, on_state=on_state)
    wake = WakeWordDetector(
        model_name=voice.wakeword.model,
        models_dir=Path(voice.wakeword.models_dir) if voice.wakeword.models_dir else None,
        verifier_model_path=voice.wakeword.verifier_model_path or None,
        verifier_threshold=voice.wakeword.verifier_threshold,
    )
    wake_sound = WakeSoundPlayer(
        enabled=voice.wake_sound_enabled,
        path=voice.wake_sound_path or None,
    )
    return ConversationLoop(
        session=session,
        wake_detector=wake,
        mic_device=voice.stt.input_device,
        wake_threshold=voice.wakeword.threshold,
        wake_cooldown_s=voice.wakeword.cooldown_s,
        follow_up_enabled=voice.follow_up_enabled,
        follow_up_timeout_s=voice.follow_up_timeout_s,
        confirm_listen_timeout_s=voice.confirm_listen_timeout_s,
        post_tts_cooldown_s=voice.post_tts_cooldown_s,
        on_state=on_state,
        wake_sound=wake_sound,
        wake_sound_warmup_s=voice.wake_sound_warmup_s,
        wake_window_s=voice.wakeword.window_s,
        wake_persist_frames=voice.wakeword.persist_frames,
        wake_debug=voice.wakeword.debug,
    )
