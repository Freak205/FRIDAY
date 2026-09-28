"""Explicit goal representation.

A `Goal` is FRIDAY's structured record of "what the user wants," persisted in
SQLite (see `friday.store`'s `goals` table) so it survives past a single
`plan.run` call and can be inspected, resumed, or linked to a follow-up or a
correction. This module also classifies a raw utterance into one of the
request shapes the brief distinguishes: a simple one-skill request, an
objective, a multi-step goal, a follow-up to an existing goal, or a
correction of one — deterministically, by cheap heuristics, not a model call.

Deliberately not overengineered: FRIDAY doesn't persist a `Goal` row for
every single skill invocation (that's what `friday.audit` already records,
per-call). A `Goal` row exists for multi-step work — today, every
`plan.run` invocation — where a lifecycle (pending -> running -> succeeded/
failed), a parent/follow-up relationship, and a linked correction are
actually meaningful.

Phase 11.2 extends this same `Goal` (never a second/parallel goal class)
with a bounded `Subgoal` breakdown — see `Subgoal` below and
`Orchestrator.decompose_goal`/`Orchestrator.run_goal` in
`friday/orchestrator.py`, which is what actually produces and advances
these during adaptive execution. `Goal` itself only stores and exposes the
breakdown; it doesn't decide when a subgoal is done.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum

from friday import store
from friday.log import get

log = get(__name__)


class GoalStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    # Phase 17.0: revived, not new — previously declared but never set by any
    # code path. Now means specifically "paused pending a same-goal
    # clarification answer" (see friday.session's "goal_clarify" Pending
    # kind and friday.skills.plan.run's clarification_required branch).
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Phase 17.0: some subgoals/evidence genuinely succeeded but the goal as
    # a whole didn't reach a clean "done" — distinct from FAILED (nothing
    # useful happened) so a partially-useful investigation/plan isn't
    # reported the same way as a total failure.
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class GoalKind(str, Enum):
    """How a raw utterance relates to FRIDAY's notion of a goal."""

    SIMPLE_REQUEST = "simple_request"
    OBJECTIVE = "objective"
    MULTI_STEP = "multi_step"
    FOLLOW_UP = "follow_up"
    CORRECTION = "correction"


class GoalMode(str, Enum):
    """Phase 17.0: the EPISTEMIC TASK TYPE of a goal — orthogonal to
    `GoalKind` above, which is about utterance SHAPE (simple/objective/
    multi-step/follow-up/correction). A `MULTI_STEP` goal can independently
    also be `DIAGNOSTIC`. See `friday.intelligence.discovery.classify_mode`.

    "" (the `Goal.from_row` default for a legacy row) means unclassified —
    treated as `DIRECT_ACTION` everywhere it's read.
    """

    DIRECT_ACTION = "direct_action"
    OPEN_ENDED = "open_ended"
    DIAGNOSTIC = "diagnostic"
    INVESTIGATIVE = "investigative"
    INFORMATION_SEEKING = "information_seeking"


class SubgoalKind(str, Enum):
    """Phase 23.0 — does this subgoal need its OWN new evidence (ACQUISITION: a read,
    observation or action), or does it answer/explain something from evidence a PRIOR
    subgoal already gathered (ANSWER)? See
    `friday.intelligence.discovery.classify_requirement_kind`, the deterministic
    classifier `Orchestrator.decompose_goal` uses to assign this for every LLM-proposed
    subgoal — never guessed by the model itself, and never a reason to demand a fresh
    tool call for what is really just interpretation of evidence already in hand
    (PLAN.md Phase 23.0, "Evidence-Grounded Goal Completion"). "" (a legacy row
    persisted before this phase) is treated as ACQUISITION everywhere it's read — the
    exact pre-Phase-23 behavior, since no subgoal ever skipped a tool call on its own
    before this."""

    ACQUISITION = "acquisition"
    ANSWER = "answer"


class SubgoalStatus(str, Enum):
    """Lifecycle of one bounded subgoal within a decomposed `Goal`.

    Deliberately small and linear — pending -> active -> succeeded/failed —
    not a second copy of `GoalStatus`. A subgoal never has its own
    waiting-for-confirmation/blocked states: confirmation and permission are
    handled at the tool-call level by `friday.permissions.EXECUTOR` exactly
    as for any other call (see Orchestrator.run_goal); a subgoal only ever
    settles as succeeded or failed once that's resolved.
    """

    PENDING = "pending"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(slots=True)
class Subgoal:
    """One bounded stage of a decomposed `Goal` — *what* must be achieved,
    not *how*. The adaptive loop in `Orchestrator.run_goal` still chooses
    each tool call one at a time from real observations; a `Subgoal` only
    tracks progress against that loop, it is never itself a fixed sequence
    of actions.
    """

    id: str
    description: str
    rationale: str = ""
    success_evidence: str = ""
    # Phase 23.0: see SubgoalKind above.
    kind: str = SubgoalKind.ACQUISITION.value
    status: SubgoalStatus = SubgoalStatus.PENDING
    recovery_attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "rationale": self.rationale,
            "success_evidence": self.success_evidence,
            "kind": self.kind,
            "status": self.status.value,
            "recovery_attempts": self.recovery_attempts,
        }

    @staticmethod
    def from_dict(d: dict) -> "Subgoal":
        try:
            status = SubgoalStatus(d.get("status", "pending"))
        except ValueError:
            status = SubgoalStatus.PENDING
        kind = d.get("kind") or SubgoalKind.ACQUISITION.value
        if kind not in (SubgoalKind.ACQUISITION.value, SubgoalKind.ANSWER.value):
            kind = SubgoalKind.ACQUISITION.value
        return Subgoal(
            id=str(d.get("id") or uuid.uuid4()),
            description=str(d.get("description", "")),
            rationale=str(d.get("rationale", "")),
            success_evidence=str(d.get("success_evidence", "")),
            kind=kind,
            status=status,
            recovery_attempts=int(d.get("recovery_attempts", 0) or 0),
        )


@dataclass(slots=True)
class GoalContract:
    """Phase 17.0 — the smallest additive extension representing what/why a
    goal is, never how: `intent`/`desired_outcome` are FRIDAY's own bounded
    restatement of what the user is trying to achieve, `unknowns` is what
    discovery still needs to resolve, `success_conditions`/`stop_conditions`
    are evidence-derived (never invented), and `risk_level` reuses the
    existing L0-L3 tier vocabulary rather than a new scale. The orchestrator
    still decides *how* — this is never a plan, only a record of intent.

    `final_verdict` reuses `friday.intelligence.evaluator.Verdict` (never a
    parallel confidence concept) once discovery/execution settles on one.
    `pending_question` is set only while the owning `Goal.status` is
    `GoalStatus.BLOCKED` (a same-goal clarification is outstanding).
    """

    intent: str = ""
    desired_outcome: str = ""
    mode: str = ""  # GoalMode value; "" = unclassified
    known_constraints: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    success_conditions: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    risk_level: str = "L0"
    final_verdict: str = ""
    pending_question: str = ""
    # Phase 20.0: which ACTION CLASSES the user's own request authorizes
    # (`friday.intent.GoalScope.to_dict()`): e.g. read-only for "inspect my
    # project", modify for "fix ...". Derived from the goal text alone — never
    # from context, experience or a proactive event — and consulted by the
    # orchestrator before any planner decision can reach the permission layer.
    # {} = not derived yet (legacy row); `plan.run` derives it on first use.
    action_scope: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action_scope": dict(self.action_scope),
            "intent": self.intent,
            "desired_outcome": self.desired_outcome,
            "mode": self.mode,
            "known_constraints": list(self.known_constraints),
            "unknowns": list(self.unknowns),
            "success_conditions": list(self.success_conditions),
            "stop_conditions": list(self.stop_conditions),
            "risk_level": self.risk_level,
            "final_verdict": self.final_verdict,
            "pending_question": self.pending_question,
        }

    @staticmethod
    def from_dict(d: dict) -> "GoalContract":
        def _strlist(v: object) -> list[str]:
            return [str(x) for x in v] if isinstance(v, list) else []

        return GoalContract(
            intent=str(d.get("intent", "")),
            desired_outcome=str(d.get("desired_outcome", "")),
            mode=str(d.get("mode", "")),
            known_constraints=_strlist(d.get("known_constraints")),
            unknowns=_strlist(d.get("unknowns")),
            success_conditions=_strlist(d.get("success_conditions")),
            stop_conditions=_strlist(d.get("stop_conditions")),
            risk_level=str(d.get("risk_level", "L0") or "L0"),
            final_verdict=str(d.get("final_verdict", "")),
            pending_question=str(d.get("pending_question", "")),
            action_scope=dict(d.get("action_scope") or {}),
        )


@dataclass(slots=True)
class Goal:
    id: str
    created_at: str
    updated_at: str
    original_request: str
    objective: str
    status: GoalStatus
    parent_goal_id: str | None = None
    current_step: int = 0
    success_criteria: str = ""
    failure_reason: str = ""
    # Phase 11.2: bounded subgoal breakdown (see Subgoal above). Empty for
    # every goal that didn't need decomposition — the common case, since
    # `friday.skills.plan.run` only decomposes goals that look like they
    # actually have more than one distinct stage (see `looks_decomposable`).
    subgoals: list[Subgoal] = field(default_factory=list)
    current_subgoal_index: int = 0
    # Phase 17.0: additive — see GoalContract above. Empty/default for every
    # goal classified DIRECT_ACTION (the common case) or persisted before
    # this phase (old rows have no `goal_contract` column value).
    contract: GoalContract = field(default_factory=GoalContract)

    @staticmethod
    def from_row(row) -> "Goal":
        raw_subgoals = row["subgoals"] if "subgoals" in row.keys() else None
        subgoals = [Subgoal.from_dict(d) for d in json.loads(raw_subgoals)] if raw_subgoals else []
        current_subgoal_index = row["current_subgoal_index"] if "current_subgoal_index" in row.keys() else 0
        raw_contract = row["goal_contract"] if "goal_contract" in row.keys() else None
        contract = GoalContract.from_dict(json.loads(raw_contract)) if raw_contract else GoalContract()
        return Goal(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            original_request=row["original_request"],
            objective=row["objective"],
            status=GoalStatus(row["status"]),
            parent_goal_id=row["parent_goal_id"],
            current_step=row["current_step"],
            success_criteria=row["success_criteria"] or "",
            failure_reason=row["failure_reason"] or "",
            subgoals=subgoals,
            current_subgoal_index=current_subgoal_index or 0,
            contract=contract,
        )

    # -- bounded subgoal accessors (Phase 11.2) ------------------------------

    def current_subgoal(self) -> Subgoal | None:
        if 0 <= self.current_subgoal_index < len(self.subgoals):
            return self.subgoals[self.current_subgoal_index]
        return None

    def completed_subgoals(self) -> list[Subgoal]:
        return [s for s in self.subgoals if s.status == SubgoalStatus.SUCCEEDED]

    def failed_subgoals(self) -> list[Subgoal]:
        return [s for s in self.subgoals if s.status == SubgoalStatus.FAILED]

    def remaining_subgoals(self) -> list[Subgoal]:
        return [s for s in self.subgoals if s.status in (SubgoalStatus.PENDING, SubgoalStatus.ACTIVE)]


def create(
    original_request: str,
    objective: str = "",
    *,
    success_criteria: str = "",
    parent_goal_id: str | None = None,
) -> Goal:
    """Persist a new goal in PENDING status and return it."""
    goal_id = str(uuid.uuid4())
    now = store.now()
    objective = objective.strip() or original_request.strip()
    c = store.conn()
    c.execute(
        "INSERT INTO goals (id, created_at, updated_at, original_request, objective, "
        "status, parent_goal_id, current_step, success_criteria, failure_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            goal_id, now, now, original_request.strip(), objective,
            GoalStatus.PENDING.value, parent_goal_id, 0, success_criteria, "",
        ),
    )
    c.commit()
    log.info("goal created: %s (%r)", goal_id, objective[:60])
    return Goal(
        id=goal_id, created_at=now, updated_at=now, original_request=original_request.strip(),
        objective=objective, status=GoalStatus.PENDING, parent_goal_id=parent_goal_id,
        success_criteria=success_criteria,
    )


def update_status(
    goal_id: str,
    status: GoalStatus,
    *,
    failure_reason: str = "",
    current_step: int | None = None,
) -> None:
    c = store.conn()
    if current_step is None:
        c.execute(
            "UPDATE goals SET status=?, updated_at=?, failure_reason=? WHERE id=?",
            (status.value, store.now(), failure_reason, goal_id),
        )
    else:
        c.execute(
            "UPDATE goals SET status=?, updated_at=?, failure_reason=?, current_step=? WHERE id=?",
            (status.value, store.now(), failure_reason, current_step, goal_id),
        )
    c.commit()


def set_subgoals(goal_id: str | None, subgoals: list[Subgoal], *, current_index: int = 0) -> None:
    """Persist the bounded subgoal breakdown for a goal (Phase 11.2).

    Called once after decomposition and again once execution finishes, so
    the row reflects the final status/recovery_attempts of every subgoal —
    not a resumable checkpoint format; see `friday.orchestrator.Orchestrator`
    for the code that actually mutates these `Subgoal` objects while a goal
    runs. Best-effort by convention with the rest of this module's write
    path — callers (`friday.skills.plan`) already wrap goal bookkeeping in
    try/except so a storage hiccup here never breaks the plan itself.
    """
    if not goal_id:
        return
    c = store.conn()
    c.execute(
        "UPDATE goals SET subgoals=?, current_subgoal_index=?, updated_at=? WHERE id=?",
        (
            json.dumps([s.to_dict() for s in subgoals], ensure_ascii=False),
            current_index, store.now(), goal_id,
        ),
    )
    c.commit()


def set_contract(goal_id: str | None, contract: GoalContract) -> None:
    """Persist the Goal Contract (Phase 17.0) — best-effort, same convention
    as `set_subgoals`: callers already wrap goal bookkeeping in try/except,
    so a storage hiccup here never breaks the plan itself."""
    if not goal_id:
        return
    c = store.conn()
    c.execute(
        "UPDATE goals SET goal_contract=?, updated_at=? WHERE id=?",
        (json.dumps(contract.to_dict(), ensure_ascii=False), store.now(), goal_id),
    )
    c.commit()


def get(goal_id: str) -> Goal | None:
    row = store.conn().execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    return Goal.from_row(row) if row else None


def recent(limit: int = 20, *, status: GoalStatus | None = None) -> list[Goal]:
    sql = "SELECT * FROM goals"
    params: list[object] = []
    if status is not None:
        sql += " WHERE status=?"
        params.append(status.value)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = store.conn().execute(sql, params).fetchall()
    return [Goal.from_row(r) for r in rows]


def most_recent_active() -> Goal | None:
    """The most recent goal still open enough to be corrected/followed-up on."""
    active = {
        GoalStatus.PENDING.value, GoalStatus.RUNNING.value,
        GoalStatus.WAITING_FOR_CONFIRMATION.value, GoalStatus.BLOCKED.value,
    }
    rows = store.conn().execute(
        "SELECT * FROM goals ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    for row in rows:
        if row["status"] in active:
            return Goal.from_row(row)
    # Nothing still open — the most recent goal at all is still a reasonable
    # anchor for "no, I meant..." arriving just after it finished.
    return Goal.from_row(rows[0]) if rows else None


# -- classification ---------------------------------------------------------

# Deliberately broad and biased toward catching real corrections; false
# positives here only cost an extra row in the `corrections` table, never a
# behavior change (see friday.session — recording a correction never blocks
# or rewrites normal intent handling).
_CORRECTION_MARKERS = (
    "no i meant", "no, i meant", "not what i meant", "not what i asked",
    "thats not what i asked", "that's not what i asked", "i meant",
    "actually i meant", "you misunderstood", "thats wrong", "that's wrong",
    "wrong one", "wrong project", "not that one", "not that", "incorrect",
)

# Phase 15.0: "No, search for dogs instead." (this project's own brief §9
# example) is a real, common correction shape none of the markers above
# catch — "instead" never appears, "no i meant" never appears. A plain
# substring for "instead" would be too broad (any unrelated "X instead of Y"
# sentence contains it); anchoring it to a leading "no," is the same bar the
# rest of this list already applies (an explicit rejection cue), just for a
# different sentence shape. Same bias as _CORRECTION_MARKERS: a false
# positive here only ever costs one extra `corrections` row.
_CORRECTION_NO_INSTEAD = re.compile(r"^\s*no,?\s+.+\binstead\b", re.IGNORECASE)

_MULTI_STEP_MARKERS = (
    " and then ", " then ", " after that ", " once you", " and also ",
    "; then", "and tell me", "and let me know",
)


def looks_like_correction(text: str) -> bool:
    low = f" {text.lower().strip()} "
    if any(marker in low for marker in _CORRECTION_MARKERS):
        return True
    return bool(_CORRECTION_NO_INSTEAD.match(text.strip()))


def looks_multi_step(text: str) -> bool:
    low = f" {text.lower().strip()} "
    if any(marker in low for marker in _MULTI_STEP_MARKERS):
        return True
    return len(text.split()) > 25


# Deliberately separate from `looks_multi_step`/`classify()` above: those
# route session-level intent (correction/follow-up/simple/objective) and use
# a high bar (an explicit "and then"/"then" marker, or >25 words) tuned for
# that. This one only decides whether `friday.skills.plan.run` should pay
# for one bounded upfront decomposition call before its adaptive loop starts
# — see Phase 11.2's PLAN.md section. A short connector like a bare "and"
# already means two distinct stages worth tracking ("open Chrome and search
# weather"), well below `looks_multi_step`'s threshold. Biased the same way
# `looks_multi_step` is: a false positive here only costs one extra bounded
# LLM call that safely no-ops back to plain adaptive execution when the goal
# turns out not to split usefully (see `Orchestrator.decompose_goal` and
# `friday.skills.plan._maybe_decompose`) — never a behavior change.
_DECOMPOSABLE_MARKERS = (" and ", " then ", ";", " after ", " once ")


def looks_decomposable(text: str) -> bool:
    """Whether `text` plausibly names more than one distinct stage, i.e.
    whether it's worth the cost of an upfront bounded subgoal breakdown
    before adaptive execution starts. A simple one-action command should
    never pay for this — see the fast path in `friday.skills.plan.run`."""
    if looks_multi_step(text):
        return True
    low = f" {text.lower().strip()} "
    return any(marker in low for marker in _DECOMPOSABLE_MARKERS)


def classify(text: str, *, has_recent_goal: bool = False) -> GoalKind:
    """Best-effort classification of a raw utterance's relationship to goals.

    Order matters: a correction phrasing wins even if it's also short (a
    follow-up would otherwise claim it), and an explicit multi-step marker
    wins over the plain word-count heuristic used for a bare follow-up.
    """
    if looks_like_correction(text):
        return GoalKind.CORRECTION
    if looks_multi_step(text):
        return GoalKind.MULTI_STEP
    if has_recent_goal and len(text.split()) <= 6:
        return GoalKind.FOLLOW_UP
    if len(text.split()) <= 5:
        return GoalKind.SIMPLE_REQUEST
    return GoalKind.OBJECTIVE
