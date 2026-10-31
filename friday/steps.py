"""Where a multi-step goal actually stands, step by step -- live (Phase 53) and after a restart (Phase 54).

A goal's run used to end in ONE flat string and one boolean. Every executed step now has one state, and the account of the goal is
built from them:

  verified      the tool succeeded AND reading the real state back confirmed it (`friday.verify`)
  completed     the tool reported success; nothing was read back (a read, or an effect that cannot be checked)
  failed        the tool reported failure, or the read-back contradicted its success
  running       the step's audit row is open and its owning process is demonstrably alive (only ever seen after a restart)
  unknown       the step started, was abandoned or cut off, and may still take effect -- never retried, never "failed"
  needs_input   the step did not run because it waits on the user (a declined or unavailable confirmation)
  not_attempted never ran (a subgoal the run stopped before)

Persistence (Phase 54). `goals.progress` holds, per step, ONLY references and verdicts: order, tool name, state, the audit row ids
that recorded it, the verification verdict with its check names, and the recovery flag -- never an argument, a result or a read-back
value. What was touched and what happened live in the audit row (redacted when written, owned by a session -- Phase 52), the single
source of truth for outcomes. `reconstruct` therefore never trusts a stored success on its own: it re-reads each step's audit row and
keeps the stored state only if the row still agrees. A stored success with no row, or a row that disagrees, becomes *unknown*; a
stored `verified` stays verified only while its verdict is stored AND the row says succeeded; a step that was in flight when the
process died is found from the audit log even if it was never checkpointed. Nothing here executes, retries or undoes anything.

Read-back verdicts (Phase 68). The audit row records the TOOL's own verdict, and for a step whose read-back contradicted it that verdict is
"succeeded": the contradiction exists only in the stored progress (state `failed`, with a verdict whose status is `failed` and at least one check
false). A stored failure that its row calls succeeded is therefore not a disagreement when that stored verdict explains it -- the step stays
`failed` (it did not take effect), however often the goal is refreshed or reloaded. The verdict is kept in the stored progress for as long as the
step is `failed` or `unknown`, so a step whose row had no outcome yet (and so could only be called unknown) is still failed, not completed, when
its late result lands; only new verification evidence for the step (a different stored verdict) can change that. A stored failure the row
contradicts with no verdict on record keeps the old rule: unknown, never success. A step the tool itself reported as failed is unaffected.

Recovery. A failed READ is recovered by any later successful read of the same tool or kind (it changed nothing, and a different read
can supply the information). A failed state change counts as *recovered* only if a LATER successful step pursued the same objective: the same tool on the same
target (a retry), or another tool of the same kind of action on the same, non-empty target. A success against a different file, page
or name proves nothing about the one that failed and leaves the goal partial. A call with nothing but content to identify it only
matches an identical call. Decided live, when the arguments are at hand, and stored as a flag. Phase 84: a failure that is a CONTRADICTED
READ-BACK (the tool said yes, reading the state back said no -- the verdict is stored with the step) is recovered only as the goal verdict
(`verify.summarize`) accepts: by a later step that pursued the same objective AND was itself read back and VERIFIED (`verify._redone`). A
retry that merely completed, or was only partly or never read back, leaves that failure standing at stage level too.

Stages (Phase 67). A decomposed goal's subgoals ("stages") are credited the same way, from THEIR OWN steps (the ones the orchestrator tagged with
the stage), never from the run as a whole -- `stage_standing`:

  resolved    at least one of its steps succeeded, none is unknown / running / waiting on the user, and every one of its failures was RECOVERED
              (the rule above: a later success against the same objective). Completed and verified both count; the report still tells them apart.
  unresolved  it was attempted and that does not hold: an unrecovered failure, an unknown outcome, a step waiting on the user, or no success at all.
  untouched   no step of its own.

The orchestrator credits a stage SUCCEEDED when it is resolved (the run ends as completed -- planner `done`, or a read-only run's evidence already
answers the goal -- an explicit jump to a later stage, or evidence auto-advance). An unresolved stage stays open when the run ends and is marked FAILED
when the run moves past it. An untouched stage is closed only where nothing contradicts the planner -- no stage of the run unresolved or failed, and at
least one step executed -- so a bare `done` over no steps, or over unresolved work, closes nothing it has no evidence for. Another stage's failure or
success never changes this stage's standing: a failed stage is not closed by an unrelated later success, and a stage that did succeed is not held open
by an earlier stage's failure. (A stage whose only steps failed stays unresolved even if a later stage re-did the same thing: its own account is a
failure, and the report says partial rather than guess.) The standing is derived from the same recovery flags that are persisted with the goal's
progress, and the subgoal statuses are persisted with the goal, so a restart reproduces both.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from friday.log import get

log = get(__name__)

_NOTE_CHARS = 90
_PROGRESS_VERSION = 1


class StepState(str, Enum):
    VERIFIED = "verified"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    UNKNOWN = "unknown"
    NEEDS_INPUT = "needs_input"
    NOT_ATTEMPTED = "not_attempted"


_OK_STATES = (StepState.VERIFIED, StepState.COMPLETED)


@dataclass(slots=True)
class StepRecord:
    n: int
    tool: str
    state: StepState
    note: str = ""              # the step's own (already redacted) words, short; empty for a success. Never persisted.
    recovered: bool = False     # a failed step a later step made good
    after_failure: bool = False  # a success that ran after an unrecovered failure: not vouched for
    audit: list[int] = field(default_factory=list)   # audit rows that recorded this step
    ver: dict[str, Any] | None = None                # {"status": ..., "checks": [[name, passed], ...]}: verdict, no values
    fp: dict[str, Any] | None = None                 # keyed objective fingerprint (Phase 89, friday.objectivefp): digests only. Persisted, never shown.

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "tool": self.tool, "state": self.state.value,
            **({"recovered": True} if self.recovered else {}),
            **({"after_failure": True} if self.after_failure else {}),
        }


@dataclass(slots=True)
class GoalReport:
    outcome: str     # complete | complete_unverified | partial | failed | unknown | needs_input | stopped | running
    steps: list[StepRecord] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)   # subgoals / parts of the request that never ran
    next_action: str = ""
    notes: list[str] = field(default_factory=list)       # what could not be established (reconstruction only)
    empty_active: list[str] = field(default_factory=list)  # ids of ACTIVE stages named in `remaining` because nothing ran under them; stored, not shown

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
            **({"notes": self.notes} if self.notes else {}),
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
        running = names((StepState.RUNNING,))
        if running:
            parts.append(f"Still running: {running}.")
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
        if self.notes:
            parts.append("Limits of this account: " + "; ".join(self.notes[:3]) + ".")
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
    # Phase 94: a call whose own outcome is unknown (a shell command stopped or timed out without the process tree confirmed gone)
    # is UNKNOWN live, as its audit row now says after a restart -- not a failure nobody established.
    from friday import audit

    if audit.outcome_of_result(obs.step.tool, bool(obs.ok), getattr(obs, "data", None)) == audit.UNKNOWN_OUTCOME:
        return StepState.UNKNOWN
    if obs.ok:
        v = getattr(obs, "verification", None)
        status = getattr(getattr(v, "status", None), "value", None)
        return StepState.VERIFIED if status == "verified" else StepState.COMPLETED
    return StepState.FAILED


# -- recovery: did a later success pursue the SAME objective? -------------------------------------------------------------------

def _norm(mode: str, value: str) -> str:
    v = " ".join(value.split())
    if mode == "path":
        return os.path.normcase(os.path.normpath(v))
    return v.lower()


def objective(tool: str, args: dict[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
    """What a call is aimed at, from the arguments that identify objects (`auditview.POLICY`: names, paths, URLs, short requests --
    not bodies of content). ("target", pairs) when the call can be identified that way; ("raw", ...) when it cannot (an unclassified
    tool, or a call with nothing but content), so only an identical call matches."""
    from friday import auditview

    args = args or {}
    modes = auditview.POLICY.get(tool)
    if modes is None:
        return ("raw", (("args", json.dumps(args, sort_keys=True, default=str)),))
    ident = tuple(sorted(
        (k, _norm(modes[k], v)) for k, v in args.items()
        if isinstance(v, str) and modes.get(k) in (auditview.KEEP, auditview.PATH, auditview.URL, auditview.INTENT, auditview.ARGV0)
    ))
    has_content = any(
        isinstance(v, (str, list, dict)) and modes.get(k, auditview.OMIT) == auditview.OMIT for k, v in args.items()
    )
    if has_content and not ident:
        return ("raw", (("args", json.dumps(args, sort_keys=True, default=str)),))
    return ("target", ident)


def same_objective(failed_tool: str, failed_args: dict, later_tool: str, later_args: dict) -> bool:
    if not _changes_state(failed_tool, failed_args):
        # A failed READ left the world as it was; any later read of the same tool or kind can supply what it was after
        # (a rephrased search, the same file by another route). The strict test below is for failures of state changes.
        return failed_tool == later_tool or _action_kind(failed_tool) == _action_kind(later_tool)
    a, b = objective(failed_tool, failed_args), objective(later_tool, later_args)
    if failed_tool == later_tool:
        return a == b
    # another tool: only the same kind of action on the same, non-empty target (e.g. write -> append on one file)
    return (
        a[0] == "target" == b[0] and bool(a[1]) and {v for _, v in a[1]} == {v for _, v in b[1]}
        and _action_kind(failed_tool) == _action_kind(later_tool)
    )


def _verdict(obs: Any) -> dict[str, Any] | None:
    v = getattr(obs, "verification", None)
    if v is None:
        return None
    return {"status": v.status.value, "checks": [[c.name, c.passed] for c in v.checks]}


def build_report(
    observations: list[Any], *, stopped: str, subgoals: list[Any] | None = None, extra_unknown: list[str] | None = None,
) -> GoalReport:
    """The step-by-step account of a run. `stopped` is the orchestrator's stop reason; `subgoals` (when the run was decomposed)
    supplies what was never reached; `extra_unknown` names steps known only from the audit log (one that was in flight when the
    whole run was cut off, so no Observation exists for it)."""
    steps, _sources = _records(observations, extra_unknown)
    # a step known only from the audit log (cut off in flight) belongs to the current stage but has no tag: then say nothing about it
    empty = [] if extra_unknown else _empty_active_ids(subgoals, observations)
    return _finalize(steps, stopped, _remaining_of(subgoals, empty), empty_active=empty)


def _records(observations: list[Any], extra_unknown: list[str] | None = None) -> tuple[list[StepRecord], list[Any]]:
    """One StepRecord per executed observation, in order, with the recovery flags set (the rule in the module docstring), and the
    observation each record came from (None for a step known only from the audit log)."""
    steps: list[StepRecord] = []
    calls: list[tuple[str, dict]] = []
    sources: list[Any] = []
    for o in observations:
        state = classify(o)
        if state is None:
            continue
        note = "" if state in _OK_STATES else _note(o.speech)
        steps.append(StepRecord(
            len(steps) + 1, o.step.tool, state, note, audit=list(getattr(o, "audit_ids", []) or []), ver=_verdict(o),
        ))
        calls.append((o.step.tool, dict(o.step.args or {})))
        sources.append(o)
    for tool in extra_unknown or []:
        steps.append(StepRecord(len(steps) + 1, tool, StepState.UNKNOWN))
        calls.append((tool, {}))
        sources.append(None)

    for i, s in enumerate(steps):
        if s.state is not StepState.FAILED:
            continue
        # a step whose own read-back contradicted the tool (Phase 84) is recovered only the way the goal verdict accepts: `verify._redone`
        # by a later step that was itself read back and VERIFIED -- the same decision `verify.summarize` makes for that failure
        contradicted = (s.ver or {}).get("status") == "failed" and sources[i] is not None
        for j in range(i + 1, len(steps)):
            t = steps[j]
            if t.state not in _OK_STATES:
                continue
            if contradicted:
                from friday import verify

                redone = t.state is StepState.VERIFIED and sources[j] is not None and verify._redone(sources[i], sources[j])  # noqa: SLF001
            else:
                redone = same_objective(s.tool, calls[i][1], t.tool, calls[j][1])
            if redone:
                s.recovered = True
                break
    _fingerprint(steps, calls, sources)
    return steps, sources


def _fingerprint(steps: list[StepRecord], calls: list[tuple[str, dict]], sources: list[Any]) -> None:
    """Phase 89: keyed fingerprints (`friday.objectivefp`) of the steps a restart will need to compare -- a contradicted read-back that a
    verified step ran after, and the verified steps that ran after one -- made now, while the arguments are in hand. Without a usable key
    nothing is set and the step is exactly what it was before (the live decisions above never use a fingerprint)."""
    contradicted = [i for i, s in enumerate(steps)
                    if s.state is StepState.FAILED and (s.ver or {}).get("status") == "failed" and sources[i] is not None]
    verified = [i for i, s in enumerate(steps) if s.state is StepState.VERIFIED and sources[i] is not None]
    want = {i for i in contradicted if any(j > i for j in verified)} | {j for j in verified if any(i < j for i in contradicted)}
    if not want:
        return
    from friday import objectivefp

    key = objectivefp.subkey()
    if key is None:
        return
    for i in sorted(want):
        steps[i].fp = objectivefp.make(steps[i].tool, calls[i][1], key=key)


RESOLVED, UNRESOLVED, UNTOUCHED = "resolved", "unresolved", "untouched"


def step_in_stage(step: Any, sg: Any, subgoals: list[Any]) -> bool:
    """Was `step` run under stage `sg` (one of `subgoals`)? (Phase 82)

      * a step tagged with an id (`PlanStep.subgoal_id`) that names exactly one stage of `subgoals` belongs to THAT stage and no other,
        whatever the descriptions say -- two stages with the same wording are different stages;
      * a step tagged with an id no stage of `subgoals` carries belongs to none: nothing says which stage it was for, and a description
        that happens to match is not evidence;
      * a step tagged with an id that two or more stages share belongs to none (Phase 83): which of them it was for cannot be told, and
        guessing from the description would credit work to stages it may not have run under;
      * only an untagged step (legacy, or hand-built: no or empty id) is matched by `PlanStep.subgoal` = the stage's description,
        exactly as before."""
    sid = str(getattr(step, "subgoal_id", "") or "")
    if sid:
        owners = sum(1 for g in subgoals if str(getattr(g, "id", "") or "") == sid)
        return owners == 1 and str(getattr(sg, "id", "") or "") == sid
    return (getattr(step, "subgoal", "") or "") == getattr(sg, "description", "")


def stage_standing(observations: list[Any], subgoals: list[Any]) -> list[str]:
    """For each subgoal (same order): RESOLVED / UNRESOLVED / UNTOUCHED, from the steps the orchestrator tagged with it
    (`PlanStep.subgoal_id` = the stage's id, else `PlanStep.subgoal` = its description; see `step_in_stage`) -- see the module
    docstring. Pure; reads only the observations."""
    steps, sources = _records(observations)
    out: list[str] = []
    for sg in subgoals:
        recs = [rec for rec, obs in zip(steps, sources) if step_in_stage(obs.step, sg, subgoals)]
        if not recs:
            out.append(UNTOUCHED)
            continue
        open_ended = any(r.state in (StepState.UNKNOWN, StepState.RUNNING, StepState.NEEDS_INPUT) for r in recs)
        unrecovered = any(r.state is StepState.FAILED and not r.recovered for r in recs)
        succeeded = any(r.state in _OK_STATES for r in recs)
        out.append(RESOLVED if succeeded and not open_ended and not unrecovered else UNRESOLVED)
    return out


def _status_of(sg: Any) -> Any:
    return getattr(getattr(sg, "status", None), "value", getattr(sg, "status", ""))


def _empty_active_ids(subgoals: list[Any] | None, observations: list[Any]) -> list[str]:
    """Ids of the ACTIVE stages that no step was tagged with: entered, but nothing ran under them (the planner failed on its first
    decision, say), so they are in neither `steps` nor the pending list. Only a live run can tell (it sees each step's stage); the ids
    are stored with the progress (`empty_active`) so a rebuild can say the same."""
    if not subgoals:
        return []
    standing = stage_standing(observations, subgoals)
    return [str(getattr(sg, "id", "")) for sg, st in zip(subgoals, standing) if _status_of(sg) == "active" and st == UNTOUCHED
            and getattr(sg, "id", "")]


def _remaining_of(subgoals: list[Any] | None, empty_active: list[str] | None = None) -> list[str]:
    remaining: list[str] = []
    for sg in subgoals or []:
        status = _status_of(sg)
        # never started, or active with nothing run under it; a failed one, or an active one with steps, was attempted and is in `steps`
        if status == "pending" or (status == "active" and str(getattr(sg, "id", "")) in (empty_active or ())):
            # model-written text; rows saved before Phase 66 were stored unchecked, so the reader checks it too
            from friday import auditview

            remaining.append(auditview.scrub_free_text(_note(getattr(sg, "description", ""))) or "")
    return remaining


def _finalize(
    steps: list[StepRecord], stopped: str, remaining: list[str], notes: list[str] | None = None, empty_active: list[str] | None = None,
) -> GoalReport:
    """The outcome, the flags on later steps and the next safe action, from step states alone (shared by a live run and a
    reconstruction, so both say the same thing about the same steps)."""
    first_unrecovered = next((i for i, s in enumerate(steps) if s.state is StepState.FAILED and not s.recovered), None)
    for s in steps:
        s.after_failure = False
    if first_unrecovered is not None:
        for t in steps[first_unrecovered + 1:]:
            if t.state in _OK_STATES and _changes_state(t.tool, None):
                t.after_failure = True

    unrecovered = [s for s in steps if s.state is StepState.FAILED and not s.recovered]
    ok_steps = [s for s in steps if s.state in _OK_STATES]
    unknown = [s for s in steps if s.state is StepState.UNKNOWN]
    waiting = [s for s in steps if s.state is StepState.NEEDS_INPUT]
    running = [s for s in steps if s.state is StepState.RUNNING]

    if unknown:
        outcome = "unknown"
    elif running:
        outcome = "running"
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
    return GoalReport(outcome, steps, remaining, next_action, list(notes or []), list(empty_active or []))


# -- persistence --------------------------------------------------------------------------------------------------------------

def progress_of(report: GoalReport, stopped: str) -> dict[str, Any]:
    """What is stored for a goal: references and verdicts only (see the module docstring). No note, argument or result."""
    return {
        "v": _PROGRESS_VERSION,
        "stopped": stopped,
        "steps": [
            {
                "n": s.n, "tool": s.tool, "state": s.state.value, "audit": list(s.audit),
                **({"ver": s.ver} if s.ver else {}),
                **({"recovered": True} if s.recovered else {}),
                **({"fp": s.fp} if s.fp else {}),
            }
            for s in report.steps
        ],
        # stage ids only (no text); absent in progress saved before Phase 80, which a rebuild reads as "unknown", as it always did
        **({"empty_active": list(report.empty_active)} if report.empty_active else {}),
    }


def save_progress(goal_id: str | None, report: GoalReport, stopped: str) -> None:
    """Checkpoint a goal's progress (best-effort: never allowed to break the run). Replaces, never appends."""
    if not goal_id:
        return
    try:
        from friday.intelligence import goals as goals_mod

        goals_mod.set_progress(goal_id, progress_of(report, stopped))
    except Exception:
        log.exception("could not checkpoint goal progress (non-fatal)")


# -- the durable view: what the audit log and the stored progress say a goal did ---------------------------------------------

_AUDIT_STATE = {
    "succeeded": StepState.COMPLETED,
    "failed": StepState.FAILED,
    "blocked": StepState.FAILED,
    "denied": StepState.NEEDS_INPUT,
    "outcome_unknown": StepState.UNKNOWN,
    "interrupted": StepState.UNKNOWN,
}


def _audit_rows(goal_id: str) -> list[dict[str, Any]]:
    from friday import audit, store

    rows = store.conn().execute(
        "SELECT a.*, s.pid AS s_pid, s.proc_start AS s_start, s.host AS s_host FROM audit a "
        "LEFT JOIN runtime_sessions s ON s.id = a.session_id WHERE a.goal_id=? AND a.skill != 'plan.run' ORDER BY a.id",
        (goal_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = audit.status_of(d)
        out.append(d)
    return out


def _cancelled_unproven(row: dict[str, Any]) -> bool:
    """Phase 100: a row that ended `cancelled` for a skill that does NOT define that word. Only `shell.run` does (its tree was confirmed
    gone); for any other skill the audit records that a cancel (a stop or a timeout) was seen, and its docstring says a partial effect is
    not ruled out -- so the row cannot say the step failed. (`status_of` calls it `failed` for older readers.)"""
    from friday import audit

    return row.get("outcome") == audit.CANCELLED and audit.CANCELLED not in audit._STATUS_OUTCOMES.get(row.get("skill"), ())


def _failure_is_definitive(row: dict[str, Any]) -> bool:
    """Phase 100: does this failed-reading row really say the step did not happen / did not succeed? The skill's own failure, a call that
    never started, a blocked call, a shell timeout or a shell cancel (process tree confirmed gone) do; a cancel of a non-shell skill does
    not. A row with NO outcome (written before Phase 94, or never classified) is read exactly as before -- its `ok` is all it has, and
    `audit.status_of` documents that it decides (such a row cannot be told from a cancelled one, which is a known limit)."""
    from friday import audit

    if row.get("status") == "blocked":
        return True
    outcome = row.get("outcome")
    if outcome is None:
        return True
    if outcome in (audit.FAILURE, audit.NOT_STARTED, audit.TIMEOUT):
        return True
    return outcome == audit.CANCELLED and not _cancelled_unproven(row)


def _row_state(row: dict[str, Any], probe: Any) -> StepState:
    """The state an audit row alone supports. An open row is `running` only if its owner is demonstrably alive."""
    from friday import owner

    state = _AUDIT_STATE.get(row["status"])
    if state is StepState.FAILED and row["status"] == "failed" and _cancelled_unproven(row):
        return StepState.UNKNOWN               # Phase 100: cancelled in flight, a partial effect not ruled out (live: UNKNOWN too)
    if state is not None:
        return state
    if row.get("session_id") and row.get("s_pid") is not None:  # unfinished
        try:
            if probe(row["s_pid"], row["s_start"], row["s_host"]) == owner.ALIVE:
                return StepState.RUNNING
        except Exception:  # noqa: BLE001
            pass
    return StepState.UNKNOWN


def ledger_for_goal(goal_id: str, probe: Any = None) -> list[StepRecord]:
    """The steps a goal started, in order, from the audit log alone: which succeeded, which failed, which have no recorded outcome
    (`running` only when the owner is demonstrably alive, otherwise unknown). The goal's own `plan.run` row is not a step."""
    from friday import owner

    probe = probe or owner.probe
    return [StepRecord(i + 1, r["skill"], _row_state(r, probe), audit=[r["id"]]) for i, r in enumerate(_audit_rows(goal_id))]


# States in which a stored read-back verdict still decides the step: it is not (yet) a success, and a row that says "the tool succeeded"
# only repeats the claim the read-back contradicted.
_VERDICT_DECIDES = (StepState.FAILED, StepState.UNKNOWN, StepState.RUNNING)
_CONTRADICTED_NOTE = "it did not take effect: reading the state back contradicted the tool's success"


def _contradicted(ver: Any) -> bool:
    """Does a stored verdict positively establish that the read-back contradicted the tool's success? Its status must be `failed` AND at
    least one check must be an explicit False (an unreadable check is None, which is not a contradiction). Anything else -- no verdict, a
    malformed one, a partial or unverified one -- establishes nothing."""
    if not isinstance(ver, dict) or ver.get("status") != "failed":
        return False
    checks = ver.get("checks")
    return isinstance(checks, list) and any(isinstance(c, (list, tuple)) and len(c) > 1 and c[1] is False for c in checks)


def _merge(
    stored: StepState, row_state: StepState | None, ver: dict | None, *, definitive_failure: bool = True,
) -> tuple[StepState, str]:
    """Reconcile a stored state with what its audit row says now. The row decides what happened; the stored state can only
    confirm it, or supply what the row cannot (`verified`, or the read-back verdict that contradicted a success the row only
    repeats). Disagreement is never resolved in favour of success.

    Phase 100: a stored UNKNOWN becomes FAILED only when the row's failure is `definitive_failure` (see `_failure_is_definitive`); a row
    that merely reads `failed` because a non-shell skill was cancelled in flight leaves it unknown."""
    if row_state is None:                      # references a row that is gone
        if stored in _OK_STATES:
            return StepState.UNKNOWN, "its audit record is no longer available, so a stored success cannot be confirmed"
        return stored, ""
    if row_state in _OK_STATES:                # row: succeeded
        if stored in _VERDICT_DECIDES and _contradicted(ver):
            return StepState.FAILED, ""        # the tool said yes, the read-back said no: the row has only the tool's word
        if stored in (StepState.COMPLETED, StepState.VERIFIED, StepState.UNKNOWN, StepState.RUNNING):
            if stored is StepState.VERIFIED:
                ok = bool(ver) and ver.get("status") == "verified" and all(c[1] is True for c in ver.get("checks", []))
                return (StepState.VERIFIED, "") if ok else (StepState.COMPLETED, "its verification evidence is no longer available")
            return StepState.COMPLETED, ""
        return StepState.UNKNOWN, "the stored outcome and the audit record disagree"
    if row_state is StepState.FAILED:
        if stored is StepState.UNKNOWN and not definitive_failure:
            return StepState.UNKNOWN, "the audit record does not say definitively that it failed"
        if stored in (StepState.FAILED, StepState.UNKNOWN, StepState.RUNNING):
            return StepState.FAILED, ""
        return StepState.UNKNOWN, "the stored outcome and the audit record disagree"
    if row_state is StepState.NEEDS_INPUT:
        if stored is StepState.NEEDS_INPUT:
            return StepState.NEEDS_INPUT, ""
        return StepState.UNKNOWN, "the stored outcome and the audit record disagree"
    return row_state, ""                       # row: running / unknown


def reconstruct(
    goal_id: str, probe: Any = None, *, stopped: str | None = None, reconcile_recovery: bool = True,
) -> GoalReport | None:
    """Rebuild a goal's step account from what is stored, WITHOUT running anything: the checkpointed progress (if any), every audit
    row of the goal (the source of truth for outcomes, including a step that was in flight and never checkpointed), the saved
    subgoal statuses (what never started) and Phase 52 ownership (is an open row's process alive?). Read-only and idempotent.
    None if the goal does not exist.

    Phase 85: a stored `recovered` flag on a CONTRADICTED READ-BACK that an older run set under the pre-Phase-84 rule (any later equivalent
    success) is not taken at its word. Whether a later step was "the same objective" cannot be rebuilt (arguments are not stored; the audit
    row keeps only a sanitized form), but the rule's other half can: Phase 84 recovers such a failure only through a later step that was
    itself VERIFIED. If no later step is verified -- neither as stored nor as rebuilt -- the flag is dropped in this account. With a verified
    later step the flag stays: it may be right, and the evidence to say otherwise is not durable. A plain tool failure (no read-back verdict)
    is never touched. Nothing stored is changed; `reconcile_recovery=False` (the callers that WRITE progress back) leaves the flag as stored,
    so a derived correction is never persisted over history."""
    from friday import owner
    from friday.intelligence import goals as goals_mod

    probe = probe or owner.probe
    goal = goals_mod.get(goal_id)
    if goal is None:
        return None
    rows = _audit_rows(goal_id)
    by_id = {r["id"]: r for r in rows}
    progress = goal.progress if isinstance(goal.progress, dict) else {}
    notes: list[str] = []
    steps: list[StepRecord] = []
    seen: set[int] = set()
    stored_verified: list[bool] = []           # per rebuilt step: did the STORED progress call it verified (before the row re-judged it)?
    stored_contradicted: list[bool] = []       # per rebuilt step: does its stored verdict positively establish a contradicted read-back?

    for p in _stored_steps(progress):
        try:
            stored = StepState(p.get("state"))
        except (ValueError, TypeError):
            stored = StepState.UNKNOWN
        ids = [i for i in (p.get("audit") if isinstance(p.get("audit"), list) else []) if isinstance(i, int)]
        found = [by_id[i] for i in ids if i in by_id]
        seen.update(ids)
        if found:
            row_state: StepState | None = _row_state(found[-1], probe)
        elif ids:
            row_state = None
        else:                                  # never reached the Executor: nothing to contradict the stored state
            row_state = stored if stored in (StepState.FAILED, StepState.NEEDS_INPUT, StepState.NOT_ATTEMPTED) else None
        state, why = _merge(stored, row_state, p.get("ver"), definitive_failure=_failure_is_definitive(found[-1]) if found else True)
        if not ids and stored in _OK_STATES:
            state, why = StepState.UNKNOWN, "no audit record was ever linked to a stored success"
        if why:
            notes.append(f"step {p.get('n')} ('{p.get('tool')}'): {why}")
        note = ""
        if state is StepState.FAILED and found:
            # a contradicted success: the row's own words are the success the read-back refuted, so they are not the note
            note = (_CONTRADICTED_NOTE if row_state in _OK_STATES
                    else _note(found[-1].get("error") or found[-1].get("result") or ""))
        keep_ver = state is StepState.VERIFIED or (state in _VERDICT_DECIDES and _contradicted(p.get("ver")))
        steps.append(StepRecord(
            len(steps) + 1, str(p.get("tool", "?")), state, note, recovered=bool(p.get("recovered")) and state is StepState.FAILED,
            audit=ids, ver=p.get("ver") if keep_ver else None, fp=p.get("fp") if isinstance(p.get("fp"), dict) and p.get("fp") else None,
        ))
        stored_verified.append(stored is StepState.VERIFIED)
        stored_contradicted.append(_contradicted(p.get("ver")))
    for r in rows:                             # started but never checkpointed (the process died around it)
        if r["id"] in seen:
            continue
        steps.append(StepRecord(len(steps) + 1, r["skill"], _row_state(r, probe), audit=[r["id"]]))
        stored_verified.append(False)
        stored_contradicted.append(False)
        notes.append(f"step {len(steps)} ('{r['skill']}') was found only in the audit log")
    if reconcile_recovery:
        from friday import objectivefp

        for i, s in enumerate(steps):
            if not (s.recovered and s.state is StepState.FAILED and stored_contradicted[i]):
                continue
            if not any(steps[j].state is StepState.VERIFIED or stored_verified[j] for j in range(i + 1, len(steps))):
                s.recovered = False
            # Phase 89: with fingerprints the other half of the Phase 84 rule can be replayed -- the flag stays only if a later step with
            # adequate evidence (rebuilt as VERIFIED) is fingerprinted as the same objective. Where the fingerprints cannot decide (a
            # legacy step, no key, another key or version) the flag stays as Phase 85 left it.
            elif objectivefp.recovered(s, steps[i + 1:]) is False:
                s.recovered = False
    if not progress and rows:
        notes = ["no step progress was checkpointed; this account comes from the audit log alone"]

    stop = stopped if stopped is not None else str(progress.get("stopped") or "")
    # Stage ids the live run stored as "entered, nothing ran under it". Used only while the stage is still ACTIVE, and not at all when
    # a step is known only from the audit log (it ran after that checkpoint, in some stage: the stored claim may no longer hold).
    raw_empty = progress.get("empty_active")
    empty = [i for i in raw_empty if isinstance(i, str)] if isinstance(raw_empty, list) and all(r["id"] in seen for r in rows) else []
    remaining = _remaining_of(goal.subgoals, empty)
    kept = [str(getattr(sg, "id", "")) for sg in (goal.subgoals or []) if _status_of(sg) == "active" and str(getattr(sg, "id", "")) in empty]
    if not remaining and goal.status.value in ("running", "partial", "failed") and stop != "completed" and (steps or progress):
        remaining = ["the rest of the request"]
    return _finalize(steps, stop or "interrupted", remaining, notes, empty_active=kept)


def refresh_goals(probe: Any = None) -> int:
    """Bring the stored progress of partial / failed goals up to date with the audit log: a step recorded as unknown whose real
    outcome has since landed (an abandoned thread that finished) becomes that outcome; a stored success whose record has gone
    becomes unknown. Rewrites `progress` only when it actually differs, so running it again changes nothing and history never
    grows. Status and reason are left to the run that wrote them. Returns how many goals changed."""
    from friday import owner, store
    from friday.intelligence import goals as goals_mod

    probe = probe or owner.probe
    c = store.conn()
    changed = 0
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM goals WHERE status IN (?,?) AND progress IS NOT NULL",
        (goals_mod.GoalStatus.PARTIAL.value, goals_mod.GoalStatus.FAILED.value),
    ).fetchall()]
    for gid in ids:
        goal = goals_mod.get(gid)
        report = reconstruct(gid, probe, reconcile_recovery=False)   # never write a derived correction over what was stored
        if goal is None or report is None:
            continue
        stored = goal.progress or {}
        fresh = progress_of(report, str(stored.get("stopped") or ""))
        if _steps_view(fresh) != _steps_view(stored):
            goals_mod.set_progress(gid, fresh)
            changed += 1
    if changed:
        log.info("%d goal(s) had their stored step progress brought up to date from the audit log", changed)
    return changed


def _stored_steps(progress: dict) -> list[dict]:
    """The step entries of a stored progress that are usable at all: a malformed field is dropped, never trusted."""
    raw = progress.get("steps") if isinstance(progress, dict) else None
    return [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []


def _steps_view(progress: dict) -> list[tuple]:
    return [(str(s.get("tool")), str(s.get("state")), tuple(i for i in (s.get("audit") if isinstance(s.get("audit"), list) else [])
                                                         if isinstance(i, int)),
             bool(s.get("recovered")), json.dumps(s.get("ver"), sort_keys=True, default=str),
             json.dumps(s.get("fp") if isinstance(s.get("fp"), dict) and s.get("fp") else None, sort_keys=True, default=str))
            for s in _stored_steps(progress)]


def settle_interrupted_goals(probe: Any = None) -> int:
    """A goal still marked `running` whose steps were started by a process that provably no longer exists was cut off: nothing
    is running it. Rebuild its account (`reconstruct`) and mark it `partial` (some step succeeded) or `failed`, with that account as
    the reason and the rebuilt progress stored, so a later "what happened with that?" is answered from evidence instead of a goal
    that looks alive forever. Nothing is resumed, retried or undone. A goal with no recorded step has no known owner and is left
    alone; so is one whose last step's owner is alive or cannot be checked. The update repeats `status='running'`, so a goal that
    finished meanwhile is never overwritten, and a settled goal is never settled twice."""
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
        report = reconstruct(gid, probe, stopped="interrupted", reconcile_recovery=False)
        if report is None:
            continue
        did = sum(1 for s in report.steps if s.state in _OK_STATES)
        status = goals_mod.GoalStatus.PARTIAL if did else goals_mod.GoalStatus.FAILED
        reason = ("interrupted: the process running this goal ended before it finished; nothing was retried or resumed. "
                  + report.tail())[:600]
        cur = c.execute(
            "UPDATE goals SET status=?, updated_at=?, failure_reason=?, current_step=?, progress=? WHERE id=? AND status=?",
            (status.value, store.now(), reason, len(report.steps),
             json.dumps(progress_of(report, "interrupted"), sort_keys=True), gid, goals_mod.GoalStatus.RUNNING.value),
        )
        c.commit()
        settled += int(cur.rowcount or 0)
    if settled:
        log.warning("%d goal(s) left running by a process that no longer exists are marked interrupted", settled)
    return settled


# -- the user-facing account of a past goal (Phase 55) -------------------------------------------------------------------------

_HEADLINE = {
    "complete": "It finished, and every step that changed something was confirmed.",
    "complete_unverified": "It finished, but I couldn't confirm the result of some steps.",
    "partial": "It did not finish.",
    "failed": "It did not work.",
    "unknown": "It is unfinished, and I can't tell whether part of it took effect.",
    "running": "It is still running.",
    "needs_input": "It is waiting on you.",
    "stopped": "It was stopped before it finished.",
}


def describe_goal(probe: Any = None) -> tuple[str, dict[str, Any]]:
    """What happened to the most recent multi-step request, as (speech, data) -- for `meta.goal_status`. READ-ONLY: it reads the goal
    row, `reconstruct`s its steps from the stored progress and the audit log, and writes nothing, runs nothing, retries nothing.

    The goal row's own status is never taken at its word: a goal recorded as succeeded is described by what its steps show, and when
    they disagree that is said. A goal with no stored progress (every goal before Phase 54, or one that never ran a step) gets its
    status and nothing invented. The request's own words are never shown (it is identified by its start time); steps are named by
    tool, never by argument, result or note. Nothing here claims FRIDAY will carry on by itself."""
    from friday import owner
    from friday.intelligence import goals as goals_mod

    probe = probe or owner.probe
    recent = goals_mod.recent(1)
    if not recent:
        return "I haven't run any multi-step request yet.", {"goal": None}
    goal = recent[0]
    # The request's own words are NOT echoed: free text can carry a secret the audit scrubber has no key to spot ("my password is
    # ..."), and a status answer reaches every daemon client. The request is identified by when it started.
    wanted = f"started {str(goal.created_at)[:16].replace('T', ' ')} UTC"
    info = {"id": goal.id, "status": goal.status.value, "started": goal.created_at, "updated": goal.updated_at}
    status = goal.status.value

    if status == "blocked":
        return (f"Your latest multi-step request ({wanted}) is waiting for your answer to a question I asked; nothing has been done since.",
                {"goal": info, "report": None})
    if status in ("pending",):
        return f"Your latest multi-step request ({wanted}) was accepted but no step is recorded for it.", {"goal": info, "report": None}

    report = reconstruct(goal.id, probe)
    has_record = report is not None and (bool(report.steps) or bool(goal.progress))
    if report is None or not has_record:
        said = {
            "succeeded": "is recorded as finished", "failed": "is recorded as failed", "partial": "is recorded as only partly done",
            "cancelled": "was stopped", "running": "is recorded as still running",
        }.get(status, f"has the status '{status}'")
        return (f"Your latest multi-step request ({wanted}) {said}, but no step-by-step record of it was kept, so I can't say what was done or what "
                "is left.", {"goal": info, "report": None})

    outcome = report.outcome
    if status == "running" and outcome not in ("running", "unknown"):
        # recorded as running, yet nothing alive is running it and no stop was recorded: it was cut off
        headline = "It is recorded as still running, but no step of it is known to be running; it may have been cut off."
    else:
        headline = _HEADLINE.get(outcome, "It is unfinished.")
    contradicts = status == "succeeded" and outcome not in ("complete", "complete_unverified")
    parts = [f"Your latest multi-step request ({wanted}) ran {len(report.steps)} step{'s' if len(report.steps) != 1 else ''}. {headline}"]
    if contradicts:
        parts.append("Its record says it succeeded, but the evidence for its steps doesn't back that up.")
    tail = report.tail()
    generic = report.remaining == ["the rest of the request"]
    if generic and not goal.subgoals:
        tail = tail.replace("Not attempted: the rest of the request.", "I don't have a saved plan, so I can't say exactly what is left.")
    if tail:
        parts.append(tail)
    if outcome not in ("complete", "complete_unverified"):
        parts.append("I won't resume or retry any of it on my own.")
    return " ".join(parts), {"goal": info, "report": report.to_dict()}
