"""FRIDAY's self-state: what it's doing right now.

Deliberately *not* a second state machine. `friday.voice.fsm.ConversationFSM`
already owns the authoritative listening/speaking state transitions for the
voice pipeline, driven by real hardware events — duplicating that here would
create two sources of truth. Instead, `SelfStateTracker` is a passive
listener on the same `friday.bus.BUS` every subsystem already publishes to
(session/brain/skill/permission/orchestrator events), so it reflects FRIDAY's
current activity for *any* actor (voice, text, scheduler) without adding a
new state machine or a new event source. The voice layer (or a future debug
surface) can read `SELF_STATE.snapshot()` to phrase a response; nothing here
drives voice transitions itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from friday.bus import BUS, Event
from friday.log import get

log = get(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SelfStatus(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING_CONFIRMATION = "waiting_for_confirmation"
    SPEAKING = "speaking"
    FAILED = "failed"


@dataclass(slots=True)
class SelfState:
    status: SelfStatus = SelfStatus.IDLE
    current_task: str = ""
    detail: str = ""
    updated_at: str = ""


class SelfStateTracker:
    def __init__(self) -> None:
        self.state = SelfState(updated_at=_now())
        self._wired = False

    def _set(self, status: SelfStatus, *, task: str | None = None, detail: str = "") -> None:
        self.state = SelfState(
            status=status,
            current_task=self.state.current_task if task is None else task,
            detail=detail,
            updated_at=_now(),
        )

    def snapshot(self) -> SelfState:
        return self.state

    # -- wiring -------------------------------------------------------------

    def wire(self) -> None:
        """Subscribe to the bus. Idempotent — safe to call from every entry
        point (daemon, CLI, tests) without double-registering handlers."""
        if self._wired:
            return
        self._wired = True
        BUS.subscribe("session.heard", self._on_heard)
        BUS.subscribe("brain.understood", self._on_understood)
        BUS.subscribe("skill.start", self._on_skill_start)
        BUS.subscribe("skill.done", self._on_skill_done)
        BUS.subscribe("skill.error", self._on_skill_error)
        BUS.subscribe("permission.confirm_requested", self._on_confirm_requested)
        BUS.subscribe("permission.declined", self._on_confirm_resolved)
        BUS.subscribe("session.awaiting_confirm", self._on_confirm_requested)
        BUS.subscribe("orchestrator.step", self._on_orchestrator_step)
        BUS.subscribe("orchestrator.replan", self._on_orchestrator_replan)
        BUS.subscribe("orchestrator.done", self._on_orchestrator_done)

    # -- handlers -------------------------------------------------------------

    async def _on_heard(self, event: Event) -> None:
        self._set(SelfStatus.THINKING, task=str(event.data.get("text", ""))[:120])

    async def _on_understood(self, event: Event) -> None:
        skill = event.data.get("skill")
        if skill:
            self._set(SelfStatus.EXECUTING, task=str(skill))

    async def _on_skill_start(self, event: Event) -> None:
        self._set(SelfStatus.EXECUTING, task=str(event.data.get("skill", "")))

    async def _on_skill_done(self, event: Event) -> None:
        self._set(SelfStatus.IDLE, detail=str(event.data.get("speech", ""))[:160])

    async def _on_skill_error(self, event: Event) -> None:
        self._set(SelfStatus.FAILED, detail=str(event.data.get("error", ""))[:200])

    async def _on_confirm_requested(self, event: Event) -> None:
        detail = str(event.data.get("preview") or event.data.get("speech") or "")
        self._set(SelfStatus.WAITING_CONFIRMATION, detail=detail[:200])

    async def _on_confirm_resolved(self, event: Event) -> None:
        self._set(SelfStatus.EXECUTING)

    async def _on_orchestrator_step(self, event: Event) -> None:
        self._set(SelfStatus.EXECUTING, task=str(event.data.get("tool", "")))

    async def _on_orchestrator_replan(self, event: Event) -> None:
        self._set(SelfStatus.THINKING, detail=f"replanning (attempt {event.data.get('attempt')})")

    async def _on_orchestrator_done(self, event: Event) -> None:
        self._set(SelfStatus.IDLE if event.data.get("ok") else SelfStatus.FAILED)


SELF_STATE = SelfStateTracker()
