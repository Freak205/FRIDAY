"""Central, bounded intelligence state.

One process-wide snapshot of "what is FRIDAY working on right now" — the
current request, goal, plan progress, environment, and a short bounded
history of recent actions/results/failures. Every list here is a `deque`
with a fixed `maxlen`, never a plain list: this state must never grow
without bound no matter how long the process has been running or how much
a goal has done.

This is deliberately a thin, mutable record, not a database — durable
per-goal history lives in `friday.intelligence.goals` (SQLite) and durable
experience lives in `friday.intelligence.episodes` (SQLite). `IntelligenceState`
is the fast, in-memory "what's happening this instant" view that the planner
context, the voice layer, and a future debug surface can all read cheaply.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Phase 14.0: same marker list friday.intelligence.episodes._SECRET_KEY_MARKERS
# and friday.intelligence.context_memory._SECRET_KEY_MARKERS already use —
# this codebase's convention is a small local check per module rather than
# one shared redaction helper (see those modules' own docstrings on this).
_SECRET_KEY_MARKERS = (
    "password", "passwd", "token", "secret", "otp", "pin", "api_key",
    "apikey", "auth", "credential", "cookie", "session_id",
)


def _summarize_args(args: dict[str, Any]) -> str:
    """Bounded, redacted one-line summary of a real call's arguments, for
    the action-continuity log (never the full raw payload — brief §15:
    no secrets, no large payloads just because they're available)."""
    parts = []
    for key, value in args.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            parts.append(f"{key}=[redacted]")
            continue
        text = str(value)
        if len(text) > 80:
            text = text[:80] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)[:300]


def _bounds() -> tuple[int, int, int, int]:
    """(max_recent_actions, max_recent_results, max_failures, max_conversation_turns).

    Read from config lazily (not at import time) so tests can override
    `CFG.intelligence` before the first state mutation.
    """
    from friday.config import CFG

    c = CFG.intelligence
    return (
        max(1, c.max_recent_actions),
        max(1, c.max_recent_results),
        max(1, c.max_failures),
        max(1, c.max_conversation_turns),
    )


@dataclass(slots=True)
class IntelligenceState:
    session_id: str
    created_at: str
    updated_at: str

    current_request: str = ""
    current_goal_id: str | None = None
    goal_status: str = ""
    current_plan: list[str] = field(default_factory=list)
    current_step: int = 0
    execution_status: str = "idle"  # idle | planning | executing | waiting_confirmation | failed
    environment_summary: str = ""
    relevant_memories: list[str] = field(default_factory=list)

    recent_actions: deque = field(default_factory=lambda: deque(maxlen=20))
    recent_results: deque = field(default_factory=lambda: deque(maxlen=20))
    failures: deque = field(default_factory=lambda: deque(maxlen=10))
    conversation_context: deque = field(default_factory=lambda: deque(maxlen=10))
    # Phase 14.0: structured counterpart to `recent_actions` (which stays a
    # plain string deque — working_memory's prompt block and
    # context_resolver.resolve_temporal_repeat's non-empty gate both already
    # read it and neither needed to change). Each entry is a small, bounded,
    # redacted dict recording what actually executed — see `record_action`.
    action_log: deque = field(default_factory=lambda: deque(maxlen=20))

    pending_confirmation: dict[str, Any] | None = None

    # Phase 16.0: a cooperative stop signal for the goal currently running.
    # Deliberately a single in-memory flag, not a new state machine — the
    # daemon (concurrent HTTP requests on one event loop, see friday.daemon's
    # `/cancel`) or any other out-of-band caller sets it; `Orchestrator.run_goal`
    # polls it once per loop iteration (see its `cancel_check` parameter) and
    # stops cleanly with `stopped="cancelled"` instead of the blunter
    # asyncio-task-cancellation path. Reset whenever a new goal starts so a
    # stale request from a finished goal can never cancel the next one.
    # Phase 28.0: it is also only ever ARMED while a goal is running and is
    # cleared when that goal ends (see `request_cancel` / `end_goal`), so the
    # one stop signal every layer polls can never be left set by accident.
    cancel_requested: bool = False


class IntelligenceStateManager:
    """Owns the single process-wide `IntelligenceState` and every mutation to it."""

    def __init__(self) -> None:
        self.state = self._fresh()

    def _fresh(self) -> IntelligenceState:
        max_actions, max_results, max_failures, max_turns = _bounds()
        now = _now()
        return IntelligenceState(
            session_id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            recent_actions=deque(maxlen=max_actions),
            recent_results=deque(maxlen=max_results),
            failures=deque(maxlen=max_failures),
            conversation_context=deque(maxlen=max_turns),
            action_log=deque(maxlen=max_actions),
        )

    def _touch(self) -> None:
        self.state.updated_at = _now()

    # -- conversation -----------------------------------------------------

    def record_turn(self, actor: str, text: str) -> None:
        self.state.conversation_context.append({"actor": actor, "text": text[:300]})
        self.state.current_request = text
        self._touch()

    # -- goal lifecycle -----------------------------------------------------

    def start_goal(self, request: str, goal_id: str, *, plan: list[str] | None = None) -> None:
        self.state.current_request = request
        self.state.current_goal_id = goal_id
        self.state.goal_status = "running"
        self.state.current_plan = list(plan or [])
        self.state.current_step = 0
        self.state.execution_status = "executing"
        self.state.cancel_requested = False
        self._touch()

    def end_goal(self, *, ok: bool, status: str = "") -> None:
        self.state.goal_status = status or ("succeeded" if ok else "failed")
        self.state.execution_status = "idle" if ok else "failed"
        self.state.cancel_requested = False  # consumed: the goal it targeted is over
        self._touch()

    # -- cooperative cancellation (Phase 16.0) -------------------------------

    @property
    def goal_running(self) -> bool:
        return self.state.goal_status == "running"

    def request_cancel(self) -> bool:
        """Ask whatever goal is currently running to stop: no further step
        starts, and an interruptible step in flight is interrupted (see
        `Orchestrator._run_step`). Returns True when a running goal was
        signalled, False when there was nothing to stop — in which case
        NOTHING is armed (Phase 28.0), so a request made while idle can never
        cancel a later, unrelated goal. Idempotent and thread-safe (one bool
        write), so a GUI/hotkey thread may call it directly."""
        if not self.goal_running:
            return False
        self.state.cancel_requested = True
        self._touch()
        return True

    def is_cancel_requested(self) -> bool:
        return self.state.cancel_requested

    # -- execution bookkeeping ----------------------------------------------

    def record_action(
        self,
        tool: str,
        args_summary: str = "",
        *,
        args: dict[str, Any] | None = None,
        status: str = "unknown",
        goal_id: str | None = None,
        subgoal: str = "",
        actor: str = "text",
    ) -> None:
        """Record one real execution attempt's outcome.

        `args_summary` (positional, unchanged from before Phase 14.0) is a
        caller-prebuilt string; pass raw `args` instead and this method
        builds a bounded, redacted summary itself (`_summarize_args`) — the
        two are mutually exclusive, callers use whichever they have. `status`
        should reflect what actually happened (e.g. "success", "failed",
        "permission_denied", "confirmation_declined", "error"), never just
        "the planner picked this tool" — see `friday.permissions.Executor
        .run`, the one production caller of this method (brief §3: intent
        is not execution).
        """
        if args is not None:
            args_summary = _summarize_args(args)
        entry = f"{tool}({args_summary})" if args_summary else tool
        self.state.recent_actions.append(entry)
        self.state.current_step += 1
        self.state.action_log.append({
            "at": _now(),
            "tool": tool,
            "args": args_summary,
            "status": status,
            "goal_id": goal_id,
            "subgoal": subgoal,
            "actor": actor,
        })
        self._touch()

    def last_action(self) -> dict[str, Any] | None:
        """The most recently recorded action's full record, or `None` — the
        structured counterpart to `resolve_temporal_repeat`'s referent."""
        return self.state.action_log[-1] if self.state.action_log else None

    def record_result(self, tool: str, ok: bool, speech: str) -> None:
        self.state.recent_results.append({"tool": tool, "ok": ok, "speech": speech[:200]})
        if not ok:
            self.state.failures.append({"tool": tool, "reason": speech[:200]})
        self._touch()

    def set_environment_summary(self, summary: str) -> None:
        self.state.environment_summary = summary[:500]
        self._touch()

    def set_relevant_memories(self, memories: list[str]) -> None:
        self.state.relevant_memories = list(memories)[:10]
        self._touch()

    # -- confirmation ---------------------------------------------------------

    def set_pending_confirmation(self, skill: str, preview: str) -> None:
        self.state.pending_confirmation = {"skill": skill, "preview": preview[:300]}
        self.state.execution_status = "waiting_confirmation"
        self._touch()

    def clear_pending_confirmation(self) -> None:
        self.state.pending_confirmation = None
        self._touch()

    # -- introspection --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        return {
            "session_id": s.session_id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "current_request": s.current_request,
            "current_goal_id": s.current_goal_id,
            "goal_status": s.goal_status,
            "current_plan": list(s.current_plan),
            "current_step": s.current_step,
            "execution_status": s.execution_status,
            "environment_summary": s.environment_summary,
            "relevant_memories": list(s.relevant_memories),
            "recent_actions": list(s.recent_actions),
            "action_log": list(s.action_log),
            "recent_results": list(s.recent_results),
            "failures": list(s.failures),
            "conversation_context": list(s.conversation_context),
            "pending_confirmation": s.pending_confirmation,
        }

    def reset(self) -> None:
        """Start a fresh state (new session id). Tests use this between cases."""
        self.state = self._fresh()


# Module-level singleton, same pattern as friday.session.SESSION.
INTEL = IntelligenceStateManager()
