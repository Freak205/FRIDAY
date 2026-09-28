"""Bridges the existing backend (BUS events + polled snapshots) into Qt
signals, and folds everything into one presentational `CoreState` for the
animated core widget to render.

This is deliberately a *view projection*, not a new state machine:
`friday.voice.fsm.ConversationFSM` remains the authority for voice-pipeline
state, `friday.intelligence.self_state.SELF_STATE` remains the authority for
any-actor activity, `friday.intelligence.state.INTEL` remains the authority
for task/goal progress. `GuiStateHub` only reads those and re-emits what it
sees — it never decides anything the backend didn't already decide.

Thread safety: `GuiStateHub` is a `QObject` constructed on the Qt main
thread. Its BUS handlers run on the backend's asyncio-loop thread (every
publisher lives there) and its `Backend.observe()` done-callbacks likewise —
both do nothing but call `.emit()` on a signal. Because the receiving
`QObject` (this one) was created on the Qt thread, Qt's default
`AutoConnection` resolves to a `QueuedConnection` for any cross-thread emit,
which marshals the slot call onto the Qt thread automatically. No manual
locking is needed for that reason alone.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from friday.bus import BUS, Event
from friday.config import CFG
from friday.intelligence.self_state import SELF_STATE, SelfStatus
from friday.log import get

from .backend import Backend

log = get(__name__)


class CoreState(str, Enum):
    BOOTING = "booting"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    AWAITING_CONFIRM = "awaiting_confirm"
    SPEAKING = "speaking"
    ERROR = "error"


# friday.voice's own on_state()/FSM string states that map straight onto a
# CoreState without needing SELF_STATE at all — see friday/voice/conversation.py
# (`_TERMINAL_EVENTS`, `_emit_fsm`) and friday/voice/session.py for the full
# vocabulary of strings this callback can receive.
_VOICE_STATE_MAP: dict[str, CoreState] = {
    "listening": CoreState.LISTENING,
    "conversation.wake_detected": CoreState.LISTENING,
    "conversation.listening": CoreState.LISTENING,
    "processing": CoreState.THINKING,
    "executing": CoreState.EXECUTING,
    "speaking": CoreState.SPEAKING,
    "error": CoreState.ERROR,
}

_SELF_STATUS_MAP: dict[SelfStatus, CoreState] = {
    SelfStatus.IDLE: CoreState.IDLE,
    SelfStatus.THINKING: CoreState.THINKING,
    SelfStatus.EXECUTING: CoreState.EXECUTING,
    SelfStatus.WAITING_CONFIRMATION: CoreState.AWAITING_CONFIRM,
    SelfStatus.SPEAKING: CoreState.SPEAKING,
    SelfStatus.FAILED: CoreState.ERROR,
}


class GuiStateHub(QObject):
    coreStateChanged = Signal(str)          # CoreState.value
    selfStateChanged = Signal(dict)         # {status, current_task, detail, updated_at}
    taskStateChanged = Signal(dict)         # INTEL.snapshot()
    orchestratorEvent = Signal(str, dict)   # (topic, event.data) — ticker feed
    confirmRequested = Signal(dict)         # {skill, tier, preview, speech, actor}
    confirmResolved = Signal(str, str)      # (skill, outcome) outcome: approved|declined
    telemetryUpdated = Signal(dict)         # {cpu_pct, ram_pct, ram_used_mb, ram_total_mb}
    desktopContextUpdated = Signal(dict)    # DesktopObservation.to_dict()
    voiceRawState = Signal(str, dict)       # passthrough of on_state(state, **data)
    backendReady = Signal(bool)             # True=ready, False=error
    subsystemStatusChanged = Signal(dict)   # {core, brain, ollama, voice, microphone, wakeword, desktop}

    def __init__(self, backend: Backend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._voice_core_state: CoreState | None = None
        self._confirm_pending_skill: str | None = None
        self._observe_inflight = False
        self._ollama_inflight = False
        self._psutil_primed = False
        self._subsystem = {
            "core": "booting", "brain": "booting", "ollama": "checking",
            "voice": "checking", "microphone": "checking", "wakeword": "checking",
            "desktop": "idle",
        }

        self._timers: list[QTimer] = []
        self._error_hold_timer = QTimer(self)
        self._error_hold_timer.setSingleShot(True)
        self._error_hold_timer.timeout.connect(self._clear_error_hold)
        self._error_held = False

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        BUS.subscribe("session.awaiting_confirm", self._on_awaiting_confirm)
        BUS.subscribe("skill.start", self._on_skill_start)
        BUS.subscribe("skill.done", self._on_skill_done)
        BUS.subscribe("skill.error", self._on_skill_error)
        BUS.subscribe("permission.declined", self._on_permission_declined)
        BUS.subscribe("orchestrator.start", self._on_orchestrator_event)
        BUS.subscribe("orchestrator.step", self._on_orchestrator_event)
        BUS.subscribe("orchestrator.observation", self._on_orchestrator_event)
        BUS.subscribe("orchestrator.replan", self._on_orchestrator_event)
        BUS.subscribe("orchestrator.done", self._on_orchestrator_event)

        self._start_timer(CFG.gui.self_state_poll_ms, self._poll_self_state)
        self._start_timer(CFG.gui.task_state_poll_ms, self._poll_task_state)
        self._start_timer(CFG.gui.telemetry_poll_ms, self._poll_telemetry)
        self._start_timer(CFG.gui.telemetry_poll_ms, self._poll_ollama)
        self._start_timer(CFG.gui.desktop_context_poll_ms, self._poll_desktop_context)
        self._set_subsystem(desktop="observing")

    def stop(self) -> None:
        BUS.unsubscribe("session.awaiting_confirm", self._on_awaiting_confirm)
        BUS.unsubscribe("skill.start", self._on_skill_start)
        BUS.unsubscribe("skill.done", self._on_skill_done)
        BUS.unsubscribe("skill.error", self._on_skill_error)
        BUS.unsubscribe("permission.declined", self._on_permission_declined)
        BUS.unsubscribe("orchestrator.start", self._on_orchestrator_event)
        BUS.unsubscribe("orchestrator.step", self._on_orchestrator_event)
        BUS.unsubscribe("orchestrator.observation", self._on_orchestrator_event)
        BUS.unsubscribe("orchestrator.replan", self._on_orchestrator_event)
        BUS.unsubscribe("orchestrator.done", self._on_orchestrator_event)
        for timer in self._timers:
            timer.stop()

    def _start_timer(self, interval_ms: int, slot: Any) -> None:
        timer = QTimer(self)
        timer.timeout.connect(slot)
        timer.start(max(50, interval_ms))
        self._timers.append(timer)

    # -- externally-pushed subsystem status (called once from app.py) -----------

    def _set_subsystem(self, **kwargs: str) -> None:
        self._subsystem.update(kwargs)
        self.subsystemStatusChanged.emit(dict(self._subsystem))

    def set_backend_ready(self, ready: bool) -> None:
        self._set_subsystem(core="online" if ready else "offline",
                             brain="online" if ready else "offline")

    def set_voice_status(self, *, enabled: bool, built: bool, mic_ok: bool, wakeword_active: bool) -> None:
        if not enabled:
            self._set_subsystem(voice="disabled", microphone="disabled", wakeword="disabled")
            return
        self._set_subsystem(
            voice="ready" if built else "unavailable",
            microphone="ready" if mic_ok else "unavailable",
            wakeword="active" if wakeword_active else "disabled",
        )

    def _poll_ollama(self) -> None:
        if self._ollama_inflight:
            return
        self._ollama_inflight = True
        future = self._backend.ping_llm()
        future.add_done_callback(self._on_ollama_done)

    def _on_ollama_done(self, future: Any) -> None:
        self._ollama_inflight = False
        try:
            reachable = bool(future.result())
        except Exception:
            reachable = False
        self._set_subsystem(ollama="connected" if reachable else "offline")

    # -- the one entry point voice's on_state callback drives --------------------

    def on_voice_state(self, state: str, **data: object) -> None:
        """Pass this as `on_state=` into `build_conversation_loop`/
        `build_voice_session` — the exact hook `friday/desktop.py` used.
        Runs on whichever thread the voice pipeline calls it from (a
        background thread, never the Qt thread), so this only emits.
        """
        self.voiceRawState.emit(state, dict(data))
        mapped = _VOICE_STATE_MAP.get(state)
        if mapped is not None:
            self._voice_core_state = mapped
        elif state in ("done", "cancelled", "empty", "conversation.idle",
                       "conversation.waiting_for_followup"):
            self._voice_core_state = None  # let SELF_STATE/idle take over
        elif state == "conversation.wake_unavailable":
            # ConversationLoop hit a WakeWordBackendError mid-run (e.g. a
            # transient native-DLL load failure) and is now retrying with
            # backoff instead of permanently giving up — see
            # friday/voice/conversation.py's _record_wake_failure. Without
            # this, the panel stayed on the "active" it was set to once at
            # startup regardless of what happened afterward.
            self._set_subsystem(wakeword="disabled")
        elif state == "conversation.wake_recovered":
            self._set_subsystem(wakeword="active")
        self._recompute_core_state()

    # -- BUS handlers (backend loop thread; emit only) ---------------------------

    async def _on_awaiting_confirm(self, event: Event) -> None:
        self._confirm_pending_skill = str(event.data.get("skill") or "")
        self.confirmRequested.emit(dict(event.data))
        self._recompute_core_state()

    async def _on_skill_start(self, event: Event) -> None:
        skill = str(event.data.get("skill") or "")
        if skill == self._confirm_pending_skill:
            self._confirm_pending_skill = None
            self.confirmResolved.emit(skill, "approved")
            self._recompute_core_state()

    async def _on_skill_done(self, event: Event) -> None:
        pass

    async def _on_skill_error(self, event: Event) -> None:
        self._hold_error()

    async def _on_permission_declined(self, event: Event) -> None:
        # `Session` tracks at most one `Pending` at a time, so a decline
        # always resolves whichever confirmation is currently outstanding
        # (explicit "no", the 60s timeout, or a stray follow-up command that
        # dropped the pending — see Session._resolve_pending).
        if self._confirm_pending_skill is None:
            return
        skill = str(event.data.get("skill") or self._confirm_pending_skill)
        self._confirm_pending_skill = None
        self.confirmResolved.emit(skill, "declined")
        self._recompute_core_state()

    async def _on_orchestrator_event(self, event: Event) -> None:
        self.orchestratorEvent.emit(event.topic, dict(event.data))

    # -- polling (Qt thread) -----------------------------------------------------

    def _poll_self_state(self) -> None:
        state = SELF_STATE.snapshot()
        self.selfStateChanged.emit({
            "status": state.status.value,
            "current_task": state.current_task,
            "detail": state.detail,
            "updated_at": state.updated_at,
        })
        if state.status is SelfStatus.FAILED:
            self._hold_error()
        self._recompute_core_state()

    def _poll_task_state(self) -> None:
        from friday.intelligence.state import INTEL

        try:
            self.taskStateChanged.emit(INTEL.snapshot())
        except Exception:
            log.exception("task state poll failed (non-fatal)")

    def _poll_telemetry(self) -> None:
        try:
            import psutil
        except Exception:
            return
        if not self._psutil_primed:
            psutil.cpu_percent(interval=None)  # first call is always 0.0; prime it
            self._psutil_primed = True
            return
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            self.telemetryUpdated.emit({
                "cpu_pct": cpu_pct,
                "ram_pct": mem.percent,
                "ram_used_mb": (mem.total - mem.available) / (1024 * 1024),
                "ram_total_mb": mem.total / (1024 * 1024),
            })
        except Exception:
            log.exception("telemetry poll failed (non-fatal)")

    def _poll_desktop_context(self) -> None:
        if self._observe_inflight:
            return
        self._observe_inflight = True
        future = self._backend.observe(
            include_screenshot=False,
            include_ocr=CFG.gui.desktop_context_include_ocr,
        )
        future.add_done_callback(self._on_observe_done)

    def _on_observe_done(self, future: Any) -> None:
        self._observe_inflight = False
        try:
            observation = future.result()
        except Exception:
            log.exception("desktop observe poll failed (non-fatal)")
            return
        self.desktopContextUpdated.emit(observation.to_dict())

    # -- CoreState projection -----------------------------------------------------

    def _hold_error(self) -> None:
        self._error_held = True
        self._error_hold_timer.start(int(CFG.gui.error_hold_s * 1000))
        self._recompute_core_state()

    def _clear_error_hold(self) -> None:
        self._error_held = False
        self._recompute_core_state()

    def _recompute_core_state(self) -> None:
        if self._confirm_pending_skill is not None:
            state = CoreState.AWAITING_CONFIRM
        elif self._error_held:
            state = CoreState.ERROR
        elif self._voice_core_state is not None:
            state = self._voice_core_state
        else:
            state = _SELF_STATUS_MAP.get(SELF_STATE.snapshot().status, CoreState.IDLE)
        self.coreStateChanged.emit(state.value)
