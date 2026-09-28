"""Phase 11.5: proactive situational intelligence.

    EVENT -> CONTEXT -> RELEVANCE -> DECISION -> OPTIONAL SUGGESTION -> USER DECIDES

Deliberately *not* a second FSM, goal system, memory system, or scheduler.
This module only:

- listens to BUS topics other subsystems already publish (`friday.triggers`'
  desktop-change polling, `friday.orchestrator`'s completion events,
  `friday.jobs`' scheduled-task completion) and turns a meaningful one into a
  bounded `SituationalEvent`;
- evaluates that event with cheap, deterministic checks against the existing
  goal system (`friday.intelligence.goals.most_recent_active`) and self-state
  (`friday.intelligence.self_state.SELF_STATE`) — never an LLM call per event;
- and, only if relevant, cooldown-safe, and not currently interrupting the
  user, produces a `ProactiveOutput` (INFORM/SUGGEST/ASK) that is *only ever
  text* — it is published to `BUS` and optionally surfaced via
  `friday.notify.send`. It never calls `friday.permissions.EXECUTOR` or any
  skill; a suggestion is not an authorization, and the user must still act
  through the normal reactive path (typed/spoken command, or the existing
  confirm panel) for anything consequential.

See PLAN.md "Phase 11.5" for the full design and PLAN.md "Phase 11.4" for the
context-memory precedent this follows (own config switch, in-process bounded
state, no new persistence).
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from friday.bus import BUS, Event
from friday.config import CFG
from friday.log import get

log = get(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Relevance(str, Enum):
    RELEVANT = "relevant"
    NOT_RELEVANT = "not_relevant"
    UNCERTAIN = "uncertain"


class ProactiveAction(str, Enum):
    INFORM = "inform"
    SUGGEST = "suggest"
    ASK = "ask"
    WAIT = "wait"


@dataclass(slots=True)
class SituationalEvent:
    """A bounded, privacy-safe description of something that happened.

    `entity` and `summary` are the only free-text fields, and both are
    already-bounded strings the caller controls (a window title, a process
    name, a goal/job name) — never OCR text, screenshot paths, or raw
    browser content (brief §14).
    """

    event_type: str
    source: str
    summary: str
    timestamp: str = field(default_factory=lambda: _now().isoformat())
    entity: str = ""
    relevant_context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    goal_id: str | None = None


@dataclass(slots=True)
class ProactiveOutput:
    action: ProactiveAction
    text: str
    event: SituationalEvent
    at: str = field(default_factory=lambda: _now().isoformat())


# -- deterministic relevance vocabulary --------------------------------------
#
# These are cheap heuristics, not a classifier — the brief is explicit that
# an LLM must never be invoked per desktop event (§4, §19). They are small
# and meant to be extended, not exhaustive.

_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "my", "your", "on", "in", "at",
    "and", "or", "is", "are", "this", "that", "please", "go", "open", "up",
    "it", "me", "i", "with",
}


def _tokenize(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


# Maps a process name to the intents it typically serves, so "send Rahul the
# update" can relate to WhatsApp opening even though no literal word overlaps
# (brief §6 Example D). Deliberately short; extend only with clear-cut cases.
_APP_KEYWORDS: dict[str, set[str]] = {
    "whatsapp.exe": {"whatsapp", "message", "text", "send", "chat", "contact"},
    "code.exe": {"code", "vscode", "project", "dev", "develop", "build", "implement", "program", "coding"},
    "chrome.exe": {"browse", "search", "website", "web", "youtube", "google"},
    "msedge.exe": {"browse", "search", "website", "web", "youtube", "google"},
    "outlook.exe": {"email", "mail", "send", "message"},
    "explorer.exe": {"file", "folder", "files"},
}

# Utility apps that are almost never relevant to a goal unless named directly
# (brief §6 Example C: Calculator opening should stay silent).
_LOW_SIGNAL_PROCESSES = {"calculator.exe", "calc.exe", "snippingtool.exe", "mspaint.exe"}

# Event types that describe FRIDAY's own work (a goal/job/task it was asked
# to do) rather than incidental desktop activity — relevant unconditionally,
# since the user is the one who created the goal/schedule behind them.
_SYSTEM_EVENT_TYPES = {"task_completed", "task_failed", "scheduled_task_due", "goal_state_changed"}


def assess_relevance(event: SituationalEvent, *, goal: Any) -> tuple[Relevance, str]:
    """Deterministic, cheap relevance check. `goal` is whatever
    `friday.intelligence.goals.most_recent_active()` returned (or None)."""
    et = event.event_type

    if et in _SYSTEM_EVENT_TYPES:
        return Relevance.RELEVANT, "system-originated event"

    if et == "confirmation_required":
        # Already surfaced via the existing confirm panel / session.awaiting_confirm
        # flow (see friday/permissions.py, friday/gui/confirm_panel.py) — never
        # re-announce it here, that would duplicate the real prompt (brief §11).
        return Relevance.NOT_RELEVANT, "already handled by the existing confirmation flow"

    # Everything else (desktop/app activity) needs an active goal to judge
    # relevance against — no goal means nothing to be relevant *to* (Example C).
    if goal is None:
        return Relevance.NOT_RELEVANT, "no active goal"

    goal_tokens = _tokenize(getattr(goal, "objective", "")) | _tokenize(getattr(goal, "original_request", ""))
    if not goal_tokens:
        return Relevance.UNCERTAIN, "active goal has no usable keywords"

    entity_lower = event.entity.lower()
    process_key = entity_lower if entity_lower.endswith(".exe") else ""

    if process_key in _LOW_SIGNAL_PROCESSES:
        return Relevance.NOT_RELEVANT, "low-signal utility app"

    event_tokens = _tokenize(event.entity) | _tokenize(event.summary)
    event_tokens |= _APP_KEYWORDS.get(process_key, set())

    overlap = goal_tokens & event_tokens
    if overlap:
        return Relevance.RELEVANT, f"keyword overlap: {sorted(overlap)}"

    if process_key and process_key not in _APP_KEYWORDS:
        # A named process we have no mapping for, plus an active goal, but no
        # textual evidence either way — don't guess (brief §4/§17 test C).
        return Relevance.UNCERTAIN, "no known relationship to the active goal"

    return Relevance.NOT_RELEVANT, "no relationship to the active goal"


def decide_action(relevance: Relevance, event: SituationalEvent) -> ProactiveAction:
    if relevance != Relevance.RELEVANT:
        return ProactiveAction.WAIT
    if event.event_type in ("task_completed", "task_failed", "scheduled_task_due", "goal_state_changed"):
        return ProactiveAction.INFORM
    if event.relevant_context.get("offer"):
        return ProactiveAction.ASK
    return ProactiveAction.SUGGEST


def _render_text(event: SituationalEvent, goal: Any) -> str:
    goal_desc = ""
    if goal is not None:
        goal_desc = getattr(goal, "objective", "") or getattr(goal, "original_request", "")

    if event.event_type in ("task_completed", "task_failed", "scheduled_task_due", "goal_state_changed"):
        return event.summary

    if event.event_type == "application_opened" and goal_desc:
        return f"{event.entity} opened. Your current task is: {goal_desc}."
    if event.event_type == "active_window_changed" and goal_desc:
        return f"{event.entity} is active — this may relate to: {goal_desc}."
    return event.summary


class ProactiveEngine:
    """Owns the relevance -> decision -> dedup/cooldown/rate-limit pipeline.

    All state here is in-process and bounded (a couple of dicts/deques) —
    matching the same convention `IntelligenceState`/`ContextMemory` already
    use, not a new persisted store (brief §17/§14).
    """

    def __init__(self) -> None:
        self._wired = False
        self._cooldowns: dict[str, datetime] = {}
        self._notify_times: deque[datetime] = deque()
        self._queue: deque[SituationalEvent] = deque(maxlen=10)
        self._last_goal_seen: tuple[str, str] | None = None

    # -- config -----------------------------------------------------------

    def enabled(self) -> bool:
        return bool(CFG.intelligence.proactive_enabled)

    # -- fingerprint / cooldown / rate-limit -------------------------------

    @staticmethod
    def fingerprint(event: SituationalEvent) -> str:
        return f"{event.event_type}:{event.entity}:{event.goal_id or ''}"

    def _cooldown_ok(self, fp: str) -> bool:
        last = self._cooldowns.get(fp)
        if last is None:
            return True
        return (_now() - last).total_seconds() >= CFG.intelligence.proactive_cooldown_seconds

    def _rate_limit_ok(self) -> bool:
        window_s = CFG.intelligence.proactive_window_minutes * 60
        now = _now()
        while self._notify_times and (now - self._notify_times[0]).total_seconds() > window_s:
            self._notify_times.popleft()
        return len(self._notify_times) < CFG.intelligence.proactive_max_notifications

    def _record_notification(self, fp: str) -> None:
        now = _now()
        self._cooldowns[fp] = now
        self._notify_times.append(now)

    # -- active-interaction gate --------------------------------------------

    def _user_busy(self) -> bool:
        """The three synchronous "don't interrupt" signals already in the
        codebase (brief §8): a pending confirmation, an in-flight
        reactive turn (SELF_STATE), or a parked Session confirmation."""
        from friday.intelligence.self_state import SELF_STATE, SelfStatus

        if SELF_STATE.snapshot().status in (
            SelfStatus.THINKING, SelfStatus.EXECUTING,
            SelfStatus.WAITING_CONFIRMATION, SelfStatus.SPEAKING,
        ):
            return True
        try:
            from friday.session import SESSION
            if SESSION.pending is not None:
                return True
        except Exception:
            log.debug("could not read SESSION.pending", exc_info=True)
        return False

    # -- core pipeline --------------------------------------------------------

    async def handle(self, event: SituationalEvent) -> ProactiveOutput | None:
        """EVENT -> CONTEXT -> RELEVANCE -> DECISION -> optional output.

        Cheap and safe to call from a BUS handler or directly from a test.
        Returns None for every silent outcome (disabled, not relevant,
        uncertain, suppressed by an active interaction, cooldown, or rate
        limit) — silence is the default, per the brief's north star.
        """
        if not self.enabled():
            return None

        from friday.intelligence import goals as goals_mod
        goal = None
        try:
            goal = goals_mod.most_recent_active()
        except Exception:
            log.debug("goal lookup failed for proactive relevance", exc_info=True)
        if event.goal_id is None and goal is not None:
            event.goal_id = goal.id

        relevance, reason = assess_relevance(event, goal=goal)
        action = decide_action(relevance, event)
        if action is ProactiveAction.WAIT:
            log.debug("proactive: %s -> WAIT (%s)", event.event_type, reason)
            return None

        if self._user_busy():
            if event not in self._queue:
                self._queue.append(event)
            log.debug("proactive: queued %s (user busy)", event.event_type)
            return None

        fp = self.fingerprint(event)
        if not self._cooldown_ok(fp):
            log.debug("proactive: cooldown active for %s", fp)
            return None
        if not self._rate_limit_ok():
            log.debug("proactive: rate limit hit, dropping %s", fp)
            return None

        text = _render_text(event, goal)
        if not text:
            return None

        output = ProactiveOutput(action=action, text=text, event=event)
        self._record_notification(fp)
        await self._dispatch(output)
        return output

    async def _dispatch(self, output: ProactiveOutput) -> None:
        # Publish first — cheap, in-process, lets any subscriber (a future
        # GUI surface, tests) observe it regardless of the notify channel.
        await BUS.publish(
            "proactive.notice",
            action=output.action.value, text=output.text,
            event_type=output.event.event_type, at=output.at,
        )
        # scheduled_task_due already goes through jobs.run_job's own
        # notify.send call (see friday/jobs.py) — never double-notify the
        # same event through two channels (brief §11/§12).
        if output.event.event_type == "scheduled_task_due":
            return
        try:
            from friday import notify
            from friday.config import CFG as _CFG
            notify.send(_CFG.identity.name, output.text)
        except Exception:
            log.debug("proactive notify.send failed", exc_info=True)

    async def flush_queue(self) -> list[ProactiveOutput]:
        """Re-attempt queued events now that the user may be free (brief §8:
        'after the interaction ends, only surface still-relevant events')."""
        if self._user_busy():
            return []
        pending = list(self._queue)
        self._queue.clear()
        results = []
        for ev in pending:
            out = await self.handle(ev)
            if out is not None:
                results.append(out)
        return results

    # -- goal transition synthesis (no edits to goals.py needed) -------------

    def _check_goal_transition(self) -> SituationalEvent | None:
        from friday.intelligence import goals as goals_mod
        try:
            goal = goals_mod.most_recent_active()
        except Exception:
            return None
        if goal is None:
            return None
        status = goal.status.value if hasattr(goal.status, "value") else str(goal.status)
        key = (goal.id, status)
        previous = self._last_goal_seen
        self._last_goal_seen = key
        if previous is None or previous[0] != key[0] or previous[1] == key[1]:
            return None
        goal_desc = goal.objective or goal.original_request
        return SituationalEvent(
            event_type="goal_state_changed", source="goals",
            summary=f"Goal '{goal_desc}' is now {status}.",
            entity=goal.id, goal_id=goal.id,
        )

    async def _after_activity(self) -> None:
        """Called after any wired BUS event: check for a goal transition,
        then try to drain the suppression queue. Both are cheap/bounded —
        no polling, no timers, only piggybacked on events that already
        fired (brief §19)."""
        try:
            ev = self._check_goal_transition()
            if ev is not None:
                await self.handle(ev)
        except Exception:
            log.debug("proactive goal-transition check failed", exc_info=True)
        try:
            await self.flush_queue()
        except Exception:
            log.debug("proactive queue flush failed", exc_info=True)

    # -- BUS wiring -----------------------------------------------------------

    def wire(self) -> None:
        """Subscribe to the bus. Idempotent — safe to call from every entry
        point, same pattern as `SelfStateTracker.wire()`."""
        if self._wired:
            return
        self._wired = True
        BUS.subscribe("trigger.window.changed", self._on_window_changed)
        BUS.subscribe("trigger.process.started", self._on_process_started)
        BUS.subscribe("trigger.process.stopped", self._on_process_stopped)
        BUS.subscribe("orchestrator.done", self._on_orchestrator_done)
        BUS.subscribe("job.done", self._on_job_done)

    async def _on_window_changed(self, event: Event) -> None:
        title = str(event.data.get("title", ""))[:160]
        if not title:
            return
        await self.handle(SituationalEvent(
            event_type="active_window_changed", source="desktop_observer",
            summary=f"Active window changed to '{title}'.", entity=title,
        ))
        await self._after_activity()

    async def _on_process_started(self, event: Event) -> None:
        process = str(event.data.get("process", ""))[:80]
        if not process:
            return
        await self.handle(SituationalEvent(
            event_type="application_opened", source="desktop_observer",
            summary=f"{process} opened.", entity=process,
        ))
        await self._after_activity()

    async def _on_process_stopped(self, event: Event) -> None:
        process = str(event.data.get("process", ""))[:80]
        if not process:
            return
        await self.handle(SituationalEvent(
            event_type="application_closed", source="desktop_observer",
            summary=f"{process} closed.", entity=process,
        ))
        await self._after_activity()

    async def _on_orchestrator_done(self, event: Event) -> None:
        # A direct text/voice turn already delivers its own spoken/typed
        # response through the normal reactive path (friday/session.py) —
        # announcing it again here would duplicate that response (brief
        # §11/§16). Only background/unattended orchestrator runs (actor
        # "scheduler"/"trigger", see friday/permissions.py UNATTENDED) reach
        # this notification path.
        actor = str(event.data.get("actor", "text"))
        if actor in ("text", "voice"):
            return
        ok = bool(event.data.get("ok"))
        goal_text = str(event.data.get("goal", ""))[:160]
        if not goal_text:
            return
        await self.handle(SituationalEvent(
            event_type="task_completed" if ok else "task_failed",
            source="orchestrator",
            summary=f"{goal_text} completed successfully." if ok else f"{goal_text} failed.",
            entity=goal_text,
        ))
        await self._after_activity()

    async def _on_job_done(self, event: Event) -> None:
        name = str(event.data.get("job", ""))[:120]
        if not name:
            return
        ok = bool(event.data.get("ok"))
        await self.handle(SituationalEvent(
            event_type="scheduled_task_due", source="scheduler",
            summary=f"Scheduled task '{name}' {'completed' if ok else 'failed'}.",
            entity=name,
        ))
        await self._after_activity()


PROACTIVE = ProactiveEngine()
