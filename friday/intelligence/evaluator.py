"""Deterministic, evidence-based evaluation.

FRIDAY must never *assume* a step or a goal succeeded just because a tool
call returned without raising — it evaluates the actual evidence a
`friday.orchestrator.Observation`/`OrchestratorResult` already carries: the
tool's own `ok` flag, its stop reason, and its reported speech. No model call
is involved; this is intentionally simple and inspectable, matching the
brief's "start deterministic" instruction. Nothing here fabricates confidence
or guesses at intent — every `Evaluation.reason` names the concrete evidence
it used.

Phase 11.2 adds a third `Verdict` alongside plain success/failure: UNCERTAIN,
for a tool call that reported `ok=True` but flagged its own result as
unconfirmed (`SkillResult.data["uncertain"]`, e.g. "process started" without
being able to confirm the window actually appeared) — still evidence-based,
just evidence the tool itself supplied, never a guess this module invents.
`Evaluation.success` is unchanged in meaning (`== observation.ok`, exactly
as before); `verdict` is additive and never flips a step into looking like a
failure it wasn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from friday.orchestrator import Observation, OrchestratorResult

# Stop reasons that are policy/limit boundaries, not "the tool tried and
# failed" — replanning around these would either be pointless (the same
# limit still applies) or, for a permission denial or a declined
# confirmation, exactly the bypass the safety section of Phase 10/11.2
# forbids. See friday.orchestrator.run_goal and friday.permissions.Executor
# (which tags a declined/unavailable confirmation as "confirmation_declined"
# on the SkillResult so it lands here, not as an ordinary retryable failure).
_NON_REPLANNABLE_ERRORS = {
    "PermissionError_", "timeout", "tool_not_allowed", "repeated_call",
    "confirmation_declined",
    # Phase 20.0: the decision never ran — the planner is re-prompted with the
    # rejection itself, not through the failed-step replan budget.
    "intent_mismatch",
}


class Verdict(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNCERTAIN = "uncertain"


@dataclass(slots=True)
class Evaluation:
    success: bool
    confidence: float  # 0.0-1.0
    reason: str
    needs_replan: bool = False
    goal_complete: bool = False
    verdict: Verdict = Verdict.FAILURE


def evaluate_step(observation: Observation) -> Evaluation:
    """Evidence-based verdict on one tool call. Never guesses at success."""
    if observation.ok:
        uncertain = bool(observation.data) and bool(observation.data.get("uncertain"))
        if uncertain:
            return Evaluation(
                success=True, confidence=0.5,
                reason=f"'{observation.step.tool}' reported success but flagged its own result as unconfirmed.",
                needs_replan=False, goal_complete=False, verdict=Verdict.UNCERTAIN,
            )
        return Evaluation(
            success=True, confidence=1.0,
            reason=f"'{observation.step.tool}' reported success.",
            needs_replan=False, goal_complete=False, verdict=Verdict.SUCCESS,
        )

    if observation.error in _NON_REPLANNABLE_ERRORS:
        return Evaluation(
            success=False, confidence=1.0,
            reason=f"'{observation.step.tool}' stopped ({observation.error}); not eligible for replanning.",
            needs_replan=False, goal_complete=False, verdict=Verdict.FAILURE,
        )

    return Evaluation(
        success=False, confidence=0.8,
        reason=f"'{observation.step.tool}' failed: {observation.speech or observation.error or 'no detail'}",
        needs_replan=True, goal_complete=False, verdict=Verdict.FAILURE,
    )


def classify_evidence(observation: Observation) -> str:
    """Phase 17.0 — a plain-language evidence label layered on top of the
    existing `Verdict`, for reporting only, never a second judgment: the
    same evidence `evaluate_step` already reads decides this too. A failed
    check is still an observed fact (of a negative kind), not "unknown"."""
    verdict = evaluate_step(observation).verdict
    if verdict == Verdict.SUCCESS:
        return "observed_fact"
    if verdict == Verdict.UNCERTAIN:
        return "unconfirmed"
    if verdict == Verdict.FAILURE:
        return "failed_check"
    return "unknown"


def evaluate_goal(result: OrchestratorResult) -> Evaluation:
    """Evidence-based verdict on a whole `run_plan`/`run_goal` result.

    `goal_complete` is only ever True when the orchestrator itself reports a
    clean "completed" stop — which `run_goal`/`run_plan` only ever report
    from the "the model declared the goal done" branch, never by silently
    falling through a stop it didn't intend (see friday.orchestrator). A
    failed observation can still appear in `result.observations` on a
    "completed" run: Phase 11.2's bounded replanning (`run_goal`'s
    `max_replans`) deliberately forgives a *recoverable* failure and lets
    the plan continue, so a legitimately recovered plan (fail once, adapt,
    then finish) must still register as a real success here — see
    PLAN.md Phase 12.0 §4, where the reliability harness's adaptive-
    recovery scenario caught this evaluator treating every such recovery as
    goal_complete=False, which defeated the entire point of Phase 11.2's
    recovery feature. Confidence is lowered (not the verdict) when a
    recovery happened, so a caller can still tell "clean run" from
    "recovered" apart without it looking like a failure.
    """
    # Phase 20.0: a blocked repeat / rejected mismatch is a message TO the planner,
    # not something that happened — it can neither count as a step nor as a
    # "forgiven failure", and a run whose only observations are such messages ran
    # nothing (its "done" is unverified, exactly like a bare done).
    from friday.orchestrator import NOT_EXECUTED_ERRORS

    result = OrchestratorResult(
        result.goal, [o for o in result.observations if o.error not in NOT_EXECUTED_ERRORS],
        result.ok, result.summary, result.stopped, result.subgoal_index, result.invalid_decision,
    )
    if not result.observations:
        if result.ok and result.stopped == "completed":
            # Phase 19.0: a bare {"action": "done"} is a planner SUGGESTION.
            # With zero observations there is no evidence at all behind it —
            # nothing ran, nothing was read — so it must not be established as
            # SUCCESS (and therefore never becomes a "succeeded" goal or a
            # successful recorded episode). It also isn't a failure: nothing
            # went wrong, the claim is simply unverified — hence UNCERTAIN.
            # `success` stays True (no step failed), `goal_complete` is False.
            return Evaluation(
                success=True, confidence=0.4,
                reason="Planner declared the goal satisfied with no tool calls — nothing was observed to confirm it.",
                needs_replan=False, goal_complete=False, verdict=Verdict.UNCERTAIN,
            )
        return Evaluation(
            success=False, confidence=0.5,
            reason=f"No steps were run; stopped={result.stopped}.",
            needs_replan=False, goal_complete=False, verdict=Verdict.FAILURE,
        )

    failed = sum(1 for o in result.observations if not o.ok)
    if result.ok and result.stopped == "completed":
        reason = (
            f"{len(result.observations)} step(s) ran, planner declared done, all succeeded."
            if failed == 0 else
            f"{len(result.observations)} step(s) ran, planner declared done, "
            f"recovered from {failed} forgiven failure(s) along the way."
        )
        return Evaluation(
            success=True, confidence=0.9 if failed == 0 else 0.75,
            reason=reason, needs_replan=False, goal_complete=True, verdict=Verdict.SUCCESS,
        )

    reason = f"stopped={result.stopped}, {failed} failed step(s)."
    return Evaluation(
        success=False, confidence=0.7, reason=reason,
        needs_replan=False, goal_complete=False, verdict=Verdict.FAILURE,
    )
