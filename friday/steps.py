"""Phase 53 -- where a multi-step goal actually stands, step by step, from evidence FRIDAY already holds.

A goal's run used to end in ONE flat string (every step's own words, or the planner's `done` summary) and one boolean. It could
not say that step 2 failed while step 3 -- which the planner chose anyway -- succeeded; that step 4 was abandoned and may still
happen; that step 5 never ran. Found by driving real `plan.run` goals: a failed write followed by a "send it" step ended as
``ok=True``, "Created the folder, wrote the file and sent it." This module gives every executed step one honest state and builds the
report from them. It stores nothing of its own: live runs read their `Observation`s, and a goal that no process is running any more
is read back from the audit rows (`audit.goal_id`, owned by a session -- Phase 52), the one source of truth for what was started.

Step states (what the user hears as Completed / Verified / Failed / Unknown / Not attempted / Needs input):

  verified      the tool succeeded AND reading the real state back confirmed it (`friday.verify`)
  completed     the tool reported success; nothing was read back (a read, or an effect that cannot be checked)
  failed        the tool reported failure, or the read-back contradicted its success
  unknown       the step started, was abandoned (timeout / stop) and may still take effect -- never retried, never "failed"
  needs_input   the step did not run because it waits on the user (a declined or unavailable confirmation)
  not_attempted never ran (a subgoal the run stopped before)

A failed step is *recovered* only if a LATER step of the same tool, or of the same kind of action (open->open), succeeded: that is
the Phase 11.2 "fail once, adapt, finish" case. Anything else leaves the goal partial, and steps that ran after it are marked as such
rather than vouched for -- FRIDAY cannot know which later step depended on which, only that something it needed did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from friday.log import get

log = get(__name__)

_NOTE_CHARS = 90


class StepState(str, Enum):
    VERIFIED = "verified"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NEEDS_INPUT = "needs_input"
    NOT_ATTEMPTED = "not_attempted"


_OK_STATES = (StepState.VERIFIED, StepState.COMPLETED)


@dataclass(slots=True)
class StepRecord:
    n: int
    tool: str
    state: StepState
    note: str = ""              # the step's own (already redacted) words, short; empty for a success
    recovered: bool = False     # a failed step a later step made good
    after_failure: bool = False  # a success that ran after an unrecovered failure: not vouched for

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "tool": self.tool, "state": self.state.value,
            **({"recovered": True} if self.recovered else {}),
            **({"after_failure": True} if self.after_failure else {}),
        }


@dataclass(slots=True)
class GoalReport:
    outcome: str                 # complete | complete_unverified | partial | failed | unknown | needs_input | stopped
    steps: list[StepRecord] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)   # subgoals / parts of the request that never ran
    next_action: str = ""

    @property
    def needs_account(self) -> bool:
        """Is a flat "done"/tool-words reply NOT enough -- i.e. something failed, is unknown, waits on the user, or never ran?"""
        return self.outcome in ("partial", "unknown", "needs_input")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "steps": [s.to_dict() for s in self.steps],
            **({"remaining": self.remaining} if self.remaining else {}),
            **({"next": self.next_action} if self.next_action else {}),
        }

    def tail(self) -> str:
        """The plain-language account: what is done, what failed, what is unknown, what did not run, what to do next.
        Names tools, never arguments or content."""
        def names(states: tuple[StepState, ...], pred=lambda s: True) -> str:
            return ", ".join(dict.fromkeys(f"'{s.tool}'" for s in self.steps if s.state in states and pred(s)))

        parts: list[str] = []
        done = names(_OK_STATES, lambda s: not s.after_failure)
        if done:
            confirmed = names((StepState.VERIFIED,))
            parts.append(f"Done: {done}" + (f" (confirmed: {confirmed})" if confirmed and confirmed != done else "") + ".")
        failed = [s for s in self.steps if s.state is StepState.FAILED and not s.recovered]
        if failed:
            first = failed[0]
            parts.append(
                f"Failed: {names((StepState.FAILED,), lambda s: not s.recovered)}"
                + (f" -- {first.note}" if first.note else "") + "."
            )
        late = names(_OK_STATES, lambda s: s.after_failure)
        if late:
            parts.append(f"Ran after that failure, so I can't vouch for it: {late}.")
        unknown = names((StepState.UNKNOWN,))
        if unknown:
            parts.append(f"Outcome unknown: {unknown}.")
        waiting = names((StepState.NEEDS_INPUT,))
        if waiting:
            parts.append(f"Waiting on you: {waiting} did not run.")
        if self.remaining:
            parts.append("Not attempted: " + "; ".join(self.remaining[:3]) + ".")
        if self.next_action:
            parts.append(self.next_action)
        return " ".join(parts)


def _tier_of(tool: str) -> str:
    from friday.registry import REGISTRY

    sk = REGISTRY.get(tool)
    return sk.tier if sk is not None else ""


def _changes_state(tool: str, args: dict[str, Any] | None) -> bool:
    from friday import intent

    try:
        return not intent.is_passive_call(tool, args or {}, tier_hint=_tier_of(tool))
    except Exception:
        return True  # unsure => treat as able to change state: the cautious reading


def _action_kind(tool: str) -> str:
    from friday import intent

    try:
        return intent.action_label(tool, tier_hint=_tier_of(tool))
    except Exception:
        return tool


def _note(speech: str) -> str:
    s = " ".join((speech or "").split()).rstrip(".")
    return s if len(s) <= _NOTE_CHARS else s[: _NOTE_CHARS - 1].rstrip() + "…"


def classify(obs: Any) -> StepState | None:
    """The state of one executed step, or None for an observation that is not a step (a blocked repeat, a rejected mismatch --
    a message to the planner -- or a step a stop request prevented from starting)."""
    from friday.orchestrator import NOT_EXECUTED_ERRORS

    error = obs.error or ""
    if error in NOT_EXECUTED_ERRORS or error == "cancelled_before_start":
        return None
    if error == "confirmation_declined":
        return StepState.NEEDS_INPUT
    if error == "interrupted":
        return StepState.UNKNOWN
    if error == "timeout":
        # a read that ran out of time changed nothing; anything that can change state may still do so
        return StepState.UNKNOWN if _changes_state(obs.step.tool, obs.step.args) else StepState.FAILED
    if obs.ok:
        v = getattr(obs, "verification", None)
        status = getattr(getattr(v, "status", None), "value", None)
        return StepState.VERIFIED if status == "verified" else StepState.COMPLETED
    return StepState.FAILED


def build_report(
    observations: list[Any], *, stopped: str, subgoals: list[Any] | None = None, extra_unknown: list[str] | None = None,
) -> GoalReport:
    """The step-by-step account of a run. `stopped` is the orchestrator's stop reason; `subgoals` (when the run was decomposed)
    supplies what was never reached; `extra_unknown` names steps known only from the audit log (one that was in flight when the
    whole run was cut off, so no Observation exists for it)."""
    steps: list[StepRecord] = []
    for o in observations:
        state = classify(o)
        if state is None:
            continue
        note = "" if state in _OK_STATES else _note(o.speech)
        steps.append(StepRecord(len(steps) + 1, o.step.tool, state, note))
    for tool in extra_unknown or []:
        steps.append(StepRecord(len(steps) + 1, tool, StepState.UNKNOWN))

    # recovery: a failure made good by a later success of the same tool or the same kind of action
    first_unrecovered: int | None = None
    for i, s in enumerate(steps):
        if s.state is not StepState.FAILED:
            continue
        later = [t for t in steps[i + 1:] if t.state in _OK_STATES]
        if any(t.tool == s.tool or _action_kind(t.tool) == _action_kind(s.tool) for t in later):
            s.recovered = True
        elif first_unrecovered is None:
            first_unrecovered = i
    if first_unrecovered is not None:
        for t in steps[first_unrecovered + 1:]:
            if t.state in _OK_STATES and _changes_state(t.tool, None):
                t.after_failure = True

    remaining: list[str] = []
    for sg in subgoals or []:
        status = getattr(getattr(sg, "status", None), "value", "")
        if status == "pending":  # never started; a failed or active one was attempted and is in `steps`
            remaining.append(_note(getattr(sg, "description", "")))

    unrecovered = [s for s in steps if s.state is StepState.FAILED and not s.recovered]
    ok_steps = [s for s in steps if s.state in _OK_STATES]
    unknown = [s for s in steps if s.state is StepState.UNKNOWN]
    waiting = [s for s in steps if s.state is StepState.NEEDS_INPUT]

    if unknown:
        outcome = "unknown"
    elif waiting:
        outcome = "needs_input"
    elif stopped == "cancelled":
        outcome = "stopped"
    elif unrecovered or stopped != "completed":
        outcome = "partial" if ok_steps else "failed"
    elif not steps:
        outcome = "complete_unverified"
    elif all(s.state is StepState.VERIFIED or not _changes_state(s.tool, None) for s in steps):
        outcome = "complete"
    else:
        outcome = "complete_unverified"

    next_action = ""
    if unknown:
        next_action = (
            f"Check whether {', '.join(dict.fromkeys(repr(s.tool) for s in unknown))} took effect before asking me to do it again; "
            "I won't repeat it on my own."
        )
    elif waiting:
        next_action = f"Tell me whether to go ahead with {', '.join(dict.fromkeys(repr(s.tool) for s in waiting))}."
    elif outcome in ("partial", "failed") and unrecovered:
        next_action = "I can try the failed step again if you ask."
    if outcome in ("partial", "unknown", "needs_input", "failed") and stopped not in ("completed", "cancelled") and not remaining:
        remaining = ["the rest of the request"]
    return GoalReport(outcome, steps, remaining, next_action)


# -- the durable view: what the audit log says a goal did ---------------------------------------------------------------------

_AUDIT_STATE = {
    "succeeded": StepState.COMPLETED,
    "failed": StepState.FAILED,
    "blocked": StepState.FAILED,
    "denied": StepState.NEEDS_INPUT,
    "outcome_unknown": StepState.UNKNOWN,
    "interrupted": StepState.UNKNOWN,
    "unfinished": StepState.UNKNOWN,
}


def ledger_for_goal(goal_id: str) -> list[StepRecord]:
    """The steps a goal started, in order, from the audit log (never from memory): which it recorded, which failed, and which
    have no recorded outcome. `unfinished` is reported as unknown: nothing says it finished. The goal's own `plan.run` row is
    not a step."""
    from friday import audit, store

    rows = store.conn().execute(
        "SELECT * FROM audit WHERE goal_id=? AND skill != 'plan.run' ORDER BY id", (goal_id,)
    ).fetchall()
    out: list[StepRecord] = []
    for r in rows:
        row = dict(r)
        state = _AUDIT_STATE.get(audit.status_of(row), StepState.UNKNOWN)
        out.append(StepRecord(len(out) + 1, row["skill"], state))
    return out


def settle_interrupted_goals(probe: Any = None) -> int:
    """A goal still marked `running` whose steps were started by a process that provably no longer exists was cut off: nothing
    is running it. Mark it `partial` (some step recorded a success) or `failed`, with the step account as the reason, so a later
    "what happened with that?" is answered from evidence instead of a goal that looks alive forever. Nothing is resumed, retried
    or undone. A goal with no recorded step has no known owner and is left alone; so is one whose last step's owner is alive or
    cannot be checked. The update repeats `status='running'`, so a goal that finished meanwhile is never overwritten."""
    from friday import owner, store
    from friday.intelligence import goals as goals_mod

    probe = probe or owner.probe
    c = store.conn()
    running = [r["id"] for r in c.execute("SELECT id FROM goals WHERE status=?", (goals_mod.GoalStatus.RUNNING.value,))]
    settled = 0
    for gid in running:
        last = c.execute(
            "SELECT s.id, s.pid, s.proc_start, s.host FROM audit a JOIN runtime_sessions s ON s.id = a.session_id "
            "WHERE a.goal_id=? AND a.skill != 'plan.run' ORDER BY a.id DESC LIMIT 1", (gid,),
        ).fetchone()
        if last is None or last["id"] == owner.identity().id:
            continue
        try:
            verdict = probe(last["pid"], last["proc_start"], last["host"])
        except Exception:  # noqa: BLE001
            continue
        if verdict != owner.DEAD:
            continue
        steps = ledger_for_goal(gid)
        report = GoalReport(
            "unknown" if any(s.state is StepState.UNKNOWN for s in steps) else "partial", steps,
            ["the rest of the request"],
        )
        did = sum(1 for s in steps if s.state in _OK_STATES)
        status = goals_mod.GoalStatus.PARTIAL if did else goals_mod.GoalStatus.FAILED
        reason = ("interrupted: the process running this goal ended before it finished; nothing was retried or resumed. "
                  + report.tail())[:600]
        cur = c.execute(
            "UPDATE goals SET status=?, updated_at=?, failure_reason=?, current_step=? WHERE id=? AND status=?",
            (status.value, store.now(), reason, len(steps), gid, goals_mod.GoalStatus.RUNNING.value),
        )
        c.commit()
        settled += int(cur.rowcount or 0)
    if settled:
        log.warning("%d goal(s) left running by a process that no longer exists are marked interrupted", settled)
    return settled
