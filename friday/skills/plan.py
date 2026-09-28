"""plan.run: the bridge from a spoken/typed goal to `Orchestrator.run_goal`.

Every other skill is one tool. This one is the escape hatch for a goal that
needs several tools in a sequence FRIDAY doesn't know ahead of time — "open my
project and tell me its state," "go to this page and read it back to me." The
LLM only ever *chooses which registered skill to call next*; the call itself
still goes through the exact same `friday.permissions.EXECUTOR` as if you'd
spoken it directly, so tiers, confirmation, audit, and undo are unchanged.

`plan.run` is deliberately excluded from its own tool list — nothing here
lets the planner recurse into another bounded run and multiply the step
budget.

Phase 10: this is also the one place a `plan.run` call becomes a tracked
`friday.intelligence.goals.Goal` with a real lifecycle, gets bounded working
memory attached to its planner context, is evaluated on evidence rather than
"didn't raise," and is recorded as a `friday.intelligence.episodes.Episode`
for future experience retrieval / training-data export. None of that changes
`plan.run`'s existing contract: `result.speech`/`result.ok`/`result.data`
(`stopped`, `steps`) are produced exactly as before — the intelligence layer
only adds new, additive `data` fields (`goal_id`) and side-effects (SQLite
rows), and every intelligence-layer call is best-effort — a failure there
must never break the plan itself.

Phase 11.1: closes the gap Phase 10 left open — `episodes.retrieve_similar()`
existed but nothing fed it into this planner context. `_append_experience`
now attaches a small, bounded block of relevant past episodes (successes
and known failures alike, plus any linked user correction) alongside the
working-memory context above, same best-effort/never-breaks-the-plan
contract, nested under the same `CFG.desktop_observer.enabled` switch as
working memory (plus its own inner `CFG.intelligence.experience_enabled`
toggle). See `friday/intelligence/experience.py`.

Phase 11.2: adaptive execution. `_maybe_decompose` calls
`Orchestrator.decompose_goal` — one bounded, best-effort LLM call — only for
a goal that `friday.intelligence.goals.looks_decomposable` thinks plausibly
has more than one distinct stage; a plain one-action goal skips it entirely
(the fast path from Phase 10 is unchanged: no extra planning call, same
`run_goal` loop). When decomposition succeeds with more than one subgoal,
those `Subgoal`s are persisted onto the tracked `Goal` and handed to
`Orchestrator.run_goal`, which advances/evaluates them against real
observations as it goes — never a fixed sequence blindly replayed (see
friday/orchestrator.py). A `CancelledError` while a plan is running (the
task backing this call was cancelled) is now caught here just long enough
to mark the goal CANCELLED before re-raising, so a cancelled run never sits
forever in a `running` state.

Phase 11.4: `_append_context_memory` attaches the same bounded recent-entity
block (see `friday/intelligence/context_memory.py`) the simple-command fast
path (`friday.session.Session`) reads and writes, nested under the same
ambient-context switch as working memory/experience above, plus its own
inner `CFG.intelligence.context_memory_enabled` toggle. `_remember_plan_entities`
is the write side: every successful step of a finished plan becomes a
candidate referent for whatever bare "it"/"that" comes next, tagged with
`goal_id` as its `turn_id` so entities from one goal are still correctly
treated as "introduced together" for ambiguity purposes.

Phase 17.0: open-ended/diagnostic/investigative/information-seeking goals
(see `friday.intelligence.discovery.classify_mode` and `GoalMode`) get a
bounded, read-only-first discovery pass — a *second call to this same
Orchestrator.run_goal method*, scoped to only L0 (read-only) tools via the
existing `tools=[...]` allow-list, before any mutating action is considered.
Never a second orchestrator, never new permission plumbing: the allow-list
IS the safety boundary. A purely diagnostic/investigative/information-seeking
goal (no explicit "fix"/mutation language) stops after discovery and reports
findings — evidence-first, hedged where unconfirmed (see
`discovery.guard_against_overclaiming`) — without ever touching the main
adaptive loop. An open-ended goal ("make this better") folds the discovery
evidence into `context` and falls through to the existing main `run_goal`
call exactly as before, still fully subject to the normal per-tool
confirmation gate for anything beyond L0. The `Goal` row gains a
`GoalContract` (intent/unknowns/success-conditions/etc. — see
`friday.intelligence.goals.GoalContract`), and can pause at
`GoalStatus.BLOCKED` awaiting a same-goal clarification answer (`resume_goal_id`/
`clarification_answer` params below), resumed by `friday.session.Session`'s
new `"goal_clarify"` `Pending` kind — never a second/parallel goal.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated

from friday.config import CFG
from friday.log import get
from friday.orchestrator import NOT_EXECUTED_ERRORS, Orchestrator
from friday.permissions import current_actor
from friday.registry import REGISTRY, SkillResult, skill

if TYPE_CHECKING:
    from friday.intelligence.goals import GoalContract, GoalMode, Subgoal
    from friday.intent import GoalScope
    from friday.orchestrator import OrchestratorResult

log = get(__name__)


@skill(
    name="plan.run",
    tier="L1",
    action="modify",
    description=(
        "Plan and carry out a multi-step goal by choosing FRIDAY's existing tools one at a "
        "time with a local LLM, executing each one through the normal permission system"
    ),
    examples=[
        "run this plan for me",
        "execute this multi-step task",
        "figure out how to do this and do it",
        "work out the steps and get this done",
        "do this for me, whatever it takes",
        "handle this task end to end",
        "plan and carry out this task",
        "take care of this multi-step request",
        "you figure out the steps for this one",
        "chain together whatever you need to do to finish this",
    ],
)
async def run(
    goal: Annotated[str, "the natural-language goal to plan and execute"],
    resume_goal_id: Annotated[
        str, "internal: continues a goal paused for clarification; never set directly by the user"
    ] = "",
    clarification_answer: Annotated[
        str, "internal: the user's answer to that clarification"
    ] = "",
    scope_expansion: Annotated[
        str,
        "internal: the user's own explicit follow-up (e.g. 'yes, fix it') that widens the scope of "
        "the goal named by resume_goal_id; never set by the planner",
    ] = "",
) -> SkillResult:
    if not CFG.planner.enabled:
        return SkillResult(speech="Multi-step planning is disabled in configuration.", ok=False)

    goal = goal.strip()
    if not goal:
        return SkillResult(speech="I need a goal to plan for.", ok=False)

    # Phase 17.0: resuming a goal paused at GoalStatus.BLOCKED (see
    # friday.session.Session's "goal_clarify" Pending kind) continues the
    # SAME Goal row rather than starting a second one — `_resume_goal`
    # returns None only if the target goal vanished, in which case this
    # falls back to starting fresh exactly as an ordinary call would.
    from friday.intelligence.goals import GoalMode

    contract_hint = ""
    goal_id: str | None = None
    contract: "GoalContract"
    action_scope: "GoalScope | None" = None
    expanded = False

    # Phase 21.0: the user's own explicit follow-up ("yes, fix it") widens the PREVIOUS
    # goal's scope — same Goal row, scope from the follow-up's literal words only
    # (`friday.intent.derive_expansion`), re-validated here rather than trusted from the
    # caller. Everything after this point is the ordinary pipeline: alignment, repeat
    # guard, permission, confirmation. If the follow-up is not a valid expansion it is
    # simply treated as a new goal that must stand on its own words (a bare "yes"
    # authorizes nothing).
    if scope_expansion.strip():
        if resume_goal_id and CFG.planner.scope_expansion:
            expansion = _expand_goal(resume_goal_id, scope_expansion)
            if expansion is not None:
                goal_id, contract, action_scope, goal, contract_hint = expansion
                expanded = True
        if not expanded:
            goal, resume_goal_id = scope_expansion.strip(), ""

    if not expanded:
        if resume_goal_id:
            goal_id, contract, contract_hint = _resume_goal(resume_goal_id, clarification_answer)
        if goal_id is None:
            goal_id = _start_goal(goal)
            contract = _seed_contract(goal_id, goal)

    mode = GoalMode(contract.mode) if contract.mode else GoalMode.DIRECT_ACTION
    if expanded:
        mode = GoalMode.DIRECT_ACTION  # the user has now asked to act; the report-only discovery pass is done
    else:
        action_scope = _action_scope_for(goal_id, goal, contract)
    started = time.perf_counter()

    tools = [s.name for s in REGISTRY.all() if s.name != "plan.run"]
    orch = Orchestrator(
        tools=tools,
        actor=current_actor(),
        max_steps=CFG.planner.max_steps,
        step_timeout_s=CFG.planner.step_timeout_s,
    )

    # Cheap, bounded ambient grounding (active window + open windows only —
    # no screenshot, no OCR) so goals like "continue where I left off" have
    # some idea what's in front of the user without every plan paying for a
    # screenshot/OCR pass. The planner can still call screen.observe itself
    # as an explicit step when it needs the deeper OCR/browser text.
    context = ""
    observation = None
    if CFG.desktop_observer.enabled:
        try:
            from friday import desktop_observer

            observation = await desktop_observer.observe(include_screenshot=False, include_ocr=False)
            context = observation.summary()
        except Exception:
            context = ""

        # Working memory rides on the same "ambient context" switch as the
        # desktop summary above (see friday.config.DesktopObserverConfig) —
        # one coherent kill switch for everything plan.run tells the planner
        # about the user's current situation, not a second, separate channel.
        context = _append_working_memory(goal, context, observation)

        # Phase 11.1: retrieved past-episode experience follows the same
        # precedent — nested under the same outer switch rather than a
        # second always-on channel (verified against
        # scripts/smoke_desktop_observer.py's "disabling desktop_observer
        # means no 'Current desktop context:' preamble at all" check, which
        # a separate always-on experience channel would otherwise break,
        # since the orchestrator wraps the whole `context` string in that
        # one label regardless of which piece contributed to it). Still has
        # its own independent inner switch, CFG.intelligence.experience_enabled,
        # for a user who wants ambient desktop context without past-episode
        # recall.
        context = _append_experience(goal, context)

        # Phase 11.4: bounded recent-context (files/apps/contacts/projects
        # FRIDAY has recently touched, in or out of this plan) follows the
        # same nesting precedent as working memory/experience above — one
        # outer ambient-context switch, plus its own independent inner
        # toggle for a user who wants desktop context without conversational
        # entity recall. See friday/intelligence/context_memory.py.
        context = _append_context_memory(context)

    # Phase 17.0: fold a resumed clarification's answer into context
    # regardless of the desktop_observer toggle above — this is the actual
    # continuation of a paused goal, not ambient desktop context, so it must
    # reach the planner even with that feature disabled.
    if contract_hint:
        context = f"{contract_hint}\n\n{context}".strip() if context else contract_hint

    # Phase 17.0: bounded, read-only-first discovery for anything other than
    # a plain direct action (see this module's docstring and
    # friday.intelligence.discovery) — the SAME Orchestrator.run_goal method,
    # scoped to L0 tools only via the existing allow-list, never a second
    # orchestrator. A purely diagnostic/investigative/information-seeking
    # goal stops here and reports findings; an open-ended goal folds the
    # evidence into `context` and falls through to the unchanged main loop
    # below.
    if mode is not GoalMode.DIRECT_ACTION:
        context, contract, discovery_result = await _run_discovery(goal, context, contract, goal_id)
        if discovery_result is not None and discovery_result.stopped == "clarification_required":
            return _await_clarification(goal_id, discovery_result.summary)
        if discovery_result is not None and _discovery_is_final(mode, goal):
            duration_ms = int((time.perf_counter() - started) * 1000)
            return _finish_discovery_only(goal_id, goal, context, discovery_result, contract, duration_ms)

    # Phase 11.2: bounded upfront decomposition, only for a goal that looks
    # like it actually has more than one distinct stage — see
    # friday.intelligence.goals.looks_decomposable and this module's
    # docstring. `None` here means "run_goal behaves exactly as before
    # Phase 11.2," which is also what a decomposition failure/timeout/empty
    # result falls back to — never a reason to block the goal.
    subgoals = None if expanded else await _maybe_decompose(orch, goal, context)
    if subgoals:
        _save_subgoals(goal_id, subgoals)

    # Phase 16.0: a plain list, not `result.observations` — passed into
    # `run_goal` as `observations_out` so it already holds every step that
    # ran *before* an external `total_timeout_s` abort, not just whatever a
    # normal return would have handed back. Without this, a plan that got
    # six real steps done before a seventh stalled past the deadline used to
    # report a bare "didn't finish in time" with zero evidence of the six
    # that actually succeeded — the same false-total-failure class Phase
    # 15.0 fixed for `planning_failed`, left open here until now.
    observations: list = []
    from friday.intelligence.state import INTEL

    try:
        result = await asyncio.wait_for(
            orch.run_goal(
                goal, model=CFG.planner.model, context=context,
                max_replans=CFG.planner.max_replans, subgoals=subgoals,
                cancel_check=INTEL.is_cancel_requested, observations_out=observations,
                action_scope=action_scope,
                # Phase 21.0: goal coverage is judged against the user's own words (an
                # expansion's planner goal is a composed sentence, not a request).
                coverage_goal="" if expanded else goal,
            ),
            timeout=CFG.planner.total_timeout_s,
        )
    except asyncio.TimeoutError:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if subgoals:
            _save_subgoals(goal_id, subgoals)
        _finish_goal(goal_id, goal, context, observations, "timeout", ok=False, duration_ms=duration_ms, contract=contract)
        completed = [o for o in observations if o.ok]
        if completed:
            speech = (
                f"That plan didn't finish within {CFG.planner.total_timeout_s:.0f} seconds, "
                "but I got partway through. So far: "
                + " ".join(o.speech for o in completed if o.speech)[:500]
            )
        else:
            speech = f"That plan didn't finish within {CFG.planner.total_timeout_s:.0f} seconds."
        return SkillResult(
            speech=speech,
            ok=False,
            data={
                "stopped": "timeout",
                "goal_id": goal_id,
                "steps": [
                    {
                        "tool": o.step.tool, "args": o.step.args, "ok": o.ok,
                        "speech": o.speech, "error": o.error,
                    }
                    for o in observations
                ],
            },
        )
    except asyncio.CancelledError:
        # The task running this call was cancelled (user/GUI cancellation) —
        # never swallowed: real asyncio cancellation must keep propagating.
        # This only closes out the tracked Goal/subgoals first so a
        # cancelled run doesn't sit forever in "running".
        _cancel_goal(goal_id, goal, subgoals)
        raise

    duration_ms = int((time.perf_counter() - started) * 1000)

    if subgoals:
        _save_subgoals(goal_id, subgoals, current_index=result.subgoal_index)

    if result.stopped == "planning_failed":
        _finish_goal(goal_id, goal, context, result.observations, result.stopped, ok=False, duration_ms=duration_ms, contract=contract)
        # Phase 15.0: found on the real machine, not by inspection — a small
        # local model can stall on a LATER decision (e.g. the final "done")
        # after several EARLIER steps already ran for real (apps.open,
        # web.search, web.open all genuinely executed and succeeded). Before
        # this fix, that real, partial progress was silently discarded: the
        # speech was always the same generic "Local planning is unavailable.
        # The plan came back malformed." (orchestrator.run_goal's malformed
        # branch hands back that literal fixed string as `result.summary`
        # regardless of `observations`), and `data` carried no `steps` at
        # all — the exact opposite failure mode from Phase 12.0's "false
        # success" concern (brief §4/§14): a FALSE total failure that hides
        # real, correct, already-completed work. `ok` stays False (the goal
        # never reached a confirmed "done"), only the reported evidence
        # changes — the same `data.steps` shape the completed-plan branch
        # below already returns, reused here rather than invented.
        completed = [o for o in result.observations if o.ok]
        if completed:
            speech = (
                "I got partway through before the local planner stalled. "
                "So far: " + " ".join(o.speech for o in completed if o.speech)[:500]
            )
        else:
            speech = f"Local planning is unavailable. {result.summary}".strip()
        return SkillResult(
            speech=speech,
            ok=False,
            data={
                "stopped": result.stopped,
                "goal_id": goal_id,
                "steps": [
                    {
                        "tool": o.step.tool, "args": o.step.args, "ok": o.ok,
                        "speech": o.speech, "error": o.error,
                    }
                    for o in result.observations
                ],
                **_invalid_decision_data(result),
            },
        )

    # Phase 22.0: what reading the real state back said about this run's state-changing
    # steps (friday.verify). "not_applicable" for a run that changed nothing.
    gv = result.verification if CFG.planner.postcondition_verify else None
    _finish_goal(
        goal_id, goal, context, result.observations, result.stopped, ok=result.ok, duration_ms=duration_ms,
        contract=contract, verification=gv,
    )

    # Phase 19.0: a "completed" run backed by NO real observation is the
    # planner's bare "done" — reported honestly as unverified (the Goal is
    # PARTIAL, no episode is recorded; see evaluator.evaluate_goal), with a
    # machine-readable flag for callers. `ok` itself is unchanged (nothing
    # failed), so every existing caller keeps working.
    unverified = result.stopped == "completed" and not [
        o for o in result.observations if o.error not in NOT_EXECUTED_ERRORS
    ]
    speech = result.summary
    if unverified:
        speech = f"I didn't run anything to check this, so treat it as unverified: {speech}".strip()
    # Phase 22.0: a state change is only "done" when it was seen to happen. A failed
    # check is stated plainly; a change that could not be read back is reported as
    # unconfirmed, never as verified success. `ok` follows the evidence for a FAILED
    # check (`result.ok` already does — the orchestrator refuses `done` over one) and is
    # otherwise unchanged: an unverifiable step did not fail, we just do not know.
    ok = result.ok
    if gv is not None and gv.status == "failed":
        ok = False
        if "take effect" not in speech and "couldn't confirm" not in speech:
            speech = f"{speech} But it did not check out: {'; '.join(gv.failed)[:300]}.".strip()
    elif gv is not None and gv.status in ("unverified", "partial") and result.stopped == "completed":
        # Spoken text stays short — the per-tool reasons are in data["verification"].
        speech = f"{speech} I couldn't verify that it actually took effect, so treat it as unconfirmed.".strip()
    verified = not unverified and (gv is None or gv.verified_ok)

    return SkillResult(
        speech=speech,
        ok=ok,
        data={
            "stopped": result.stopped,
            "goal_id": goal_id,
            "verified": verified,
            **({"verification": gv.to_dict()} if gv is not None and gv.status != "not_applicable" else {}),
            "steps": [
                {
                    "tool": o.step.tool, "args": o.step.args, "ok": o.ok,
                    "speech": o.speech, "error": o.error,
                    **({"verification": o.verification.status.value} if o.verification is not None else {}),
                }
                for o in result.observations
            ],
            **_invalid_decision_data(result),
            **_intent_data(result, action_scope),
            **({"scope_expanded": True} if expanded else {}),
        },
    )


def _expand_goal(
    resume_goal_id: str, follow_up: str,
) -> "tuple[str, GoalContract, GoalScope, str, str] | None":
    """Phase 21.0: widen an existing goal's action scope from the user's own follow-up.
    Returns `(goal_id, contract, widened_scope, planner_goal, context_hint)` or None if
    this is not a valid expansion (goal vanished, or `friday.intent.derive_expansion`
    says the follow-up is not an explicit anaphoric directive to act). The SAME Goal
    row continues (status back to RUNNING, contract updated) — no second goal.

    Authority comes from exactly one place: `derive_expansion(follow_up, prior_scope)`.
    The findings block handed to the planner is CONTEXT (real tool results recorded on
    the contract by the earlier run); it is never read for authority."""
    from friday import intent
    from friday.intelligence import goals as goals_mod

    try:
        record = goals_mod.get(resume_goal_id)
        if record is None:
            return None
        contract = record.contract
        prior = intent.GoalScope.from_dict(contract.action_scope) if contract.action_scope else None
        if prior is None:
            prior = intent.derive_scope(record.objective, contract.mode, _wants_mutation(record.objective))
        widened = intent.derive_expansion(follow_up, prior)
        if widened is None:
            return None

        text = follow_up.strip()
        original = record.objective.strip()
        findings = "; ".join(c[:150] for c in contract.success_conditions[:4])
        hint = f'Earlier the user asked: "{original}".'
        if findings:
            hint += f" Findings from that (real tool results): {findings}."
        hint += f' The user has now explicitly asked: "{text}".'

        contract.action_scope = widened.to_dict()
        contract.known_constraints = (contract.known_constraints + [f"User authorized: {text[:200]}"])[-5:]
        contract.pending_question = ""
        contract.final_verdict = ""
        goals_mod.update_status(resume_goal_id, goals_mod.GoalStatus.RUNNING)
        _save_contract(resume_goal_id, contract)
        return resume_goal_id, contract, widened, f"{original}. Follow-up from the user: {text}", hint[:900]
    except Exception:
        log.exception("plan.run: expanding the goal scope failed (non-fatal, treated as a new goal)")
        return None


def _intent_data(result: "OrchestratorResult", scope: "GoalScope | None") -> dict:
    """Phase 20.0: what the intent guard did on this run, for `SkillResult.data` —
    only present when a scope was in force, so an unguarded run's data shape is
    unchanged. `rejected` lists decisions that were never executed."""
    if scope is None:
        return {}
    rejected = [
        {"tool": o.step.tool, "args": o.step.args, "action_class": o.data.get("action_class", "")}
        for o in result.observations if o.error == "intent_mismatch"
    ]
    return {"intent": {"scope": scope.to_dict(), "rejected": rejected}}


def _invalid_decision_data(result: "OrchestratorResult") -> dict:
    """Phase 19.0: the structured reason a run stopped on an invalid planner
    decision (friday.decision.InvalidDecision), for `SkillResult.data` — only
    present when that is what actually happened, so every other outcome's
    data shape is unchanged."""
    invalid = getattr(result, "invalid_decision", None)
    return {"invalid_decision": invalid.to_dict()} if invalid is not None else {}


# -- Phase 10 wiring: best-effort, never allowed to break plan.run itself ---


def _start_goal(goal: str) -> str | None:
    try:
        from friday.intelligence import goals as goals_mod
        from friday.intelligence.state import INTEL

        record = goals_mod.create(
            goal, goal,
            success_criteria=f"The plan reports success and the goal is satisfied: {goal}",
        )
        goals_mod.update_status(record.id, goals_mod.GoalStatus.RUNNING)
        INTEL.start_goal(goal, record.id)
        return record.id
    except Exception:
        log.exception("plan.run: goal tracking failed to start (non-fatal)")
        return None


async def _maybe_decompose(orch: Orchestrator, goal: str, context: str) -> list["Subgoal"] | None:
    """Phase 11.2 fast path: only pay for `Orchestrator.decompose_goal`'s one
    extra bounded LLM call when the goal actually looks like it has more
    than one distinct stage (see `looks_decomposable`). A goal that doesn't
    match, a decomposition that comes back malformed/unavailable, and a
    goal the model itself couldn't usefully split into more than one
    subgoal all return `None` here — meaning `run_goal` executes exactly as
    it did before Phase 11.2, never blocked and never handed an invented,
    unbounded breakdown of a vague request.
    """
    from friday.intelligence import goals as goals_mod

    if not goals_mod.looks_decomposable(goal):
        return None
    try:
        subgoals = await orch.decompose_goal(
            goal, model=CFG.planner.model, context=context, max_subgoals=CFG.planner.max_subgoals,
        )
    except Exception:
        log.exception("plan.run: decomposition failed (non-fatal, falling back to direct execution)")
        return None
    return subgoals if len(subgoals) > 1 else None


def _save_subgoals(goal_id: str | None, subgoals: list["Subgoal"], *, current_index: int = 0) -> None:
    try:
        from friday.intelligence import goals as goals_mod

        goals_mod.set_subgoals(goal_id, subgoals, current_index=max(current_index, 0))
    except Exception:
        log.exception("plan.run: saving subgoal state failed (non-fatal)")


# -- Phase 17.0: Goal Contract, discovery, and clarification wiring --------
#
# All best-effort/never-breaks-the-plan, same convention as the rest of this
# module. Nothing here is a second planner/orchestrator/evaluator: the
# discovery pass below is the *same* `Orchestrator.run_goal` this file
# already calls for main execution, just scoped to L0 tools and given a
# smaller step/time budget (see friday.config.PlannerConfig).

_MUTATION_MARKERS = (
    "fix", "make it work", "make this work", "repair", "resolve it",
    "resolve this", "solve it", "solve this", "correct it", "get it working",
)


def _wants_mutation(goal: str) -> bool:
    """Has the user actually asked FRIDAY to change/fix something, or only
    to look into it? See this module's docstring / PLAN.md Phase 17.0 §18 —
    "why isn't this working" must never auto-mutate; "fix why this isn't
    working" must. Deliberately narrow, explicit markers, not a broad guess
    (a false negative here only means an extra, harmless discovery-only
    report; a false positive would let a diagnostic silently start
    mutating, which is the one thing this function must not do)."""
    low = f" {goal.lower().strip()} "
    return any(m in low for m in _MUTATION_MARKERS)


def _discovery_is_final(mode: "GoalMode", goal: str) -> bool:
    """Whether this goal's answer IS the discovery result, with no main
    execution phase at all. INFORMATION_SEEKING and INVESTIGATIVE are
    always report-only ("no action unless requested"; "do not invent
    TODOs"). DIAGNOSTIC is report-only unless the user explicitly asked for
    a fix. OPEN_ENDED always falls through to the unchanged main loop — it's
    inherently about achieving an outcome, gated entirely by the existing
    per-tool confirmation for anything beyond L0."""
    from friday.intelligence.goals import GoalMode

    if mode in (GoalMode.INFORMATION_SEEKING, GoalMode.INVESTIGATIVE):
        return True
    if mode is GoalMode.DIAGNOSTIC:
        return not _wants_mutation(goal)
    return False


def _seed_contract(goal_id: str | None, goal: str) -> "GoalContract":
    """Classify the goal and persist the initial Goal Contract. Never
    invents a "fix" desired outcome for a goal that only asked to look into
    something (spec: "do not assume 'fix it' unless the user actually asked
    to fix it")."""
    from friday.intelligence import discovery
    from friday.intelligence.goals import GoalContract, GoalMode

    mode = discovery.classify_mode(goal)
    report_only = mode in (GoalMode.DIAGNOSTIC, GoalMode.INVESTIGATIVE, GoalMode.INFORMATION_SEEKING)
    desired_outcome = f"identify and report findings for: {goal}" if (report_only and not _wants_mutation(goal)) else goal
    contract = GoalContract(intent=goal, desired_outcome=desired_outcome, mode=mode.value)
    _save_contract(goal_id, contract)
    return contract


def _action_scope_for(goal_id: str | None, goal: str, contract: "GoalContract") -> "GoalScope | None":
    """Phase 20.0: the action classes the USER'S OWN goal text authorizes
    (`friday.intent.derive_scope`), recorded on the Goal Contract so it is
    inspectable and survives a clarification pause. Derived from `goal` and the
    epistemic mode alone — never from `context`, retrieved experience, remembered
    entities or a proactive event, none of which reach this function. Off (None)
    only when `CFG.planner.intent_guard` is. If derivation itself ever fails it
    fails CLOSED (read-only), never open."""
    if not CFG.planner.intent_guard:
        return None
    from friday import intent

    try:
        scope = intent.GoalScope.from_dict(contract.action_scope) if contract.action_scope else None
        if scope is None or scope.goal != goal.strip():
            scope = intent.derive_scope(goal, contract.mode, _wants_mutation(goal))
            contract.action_scope = scope.to_dict()
            _save_contract(goal_id, contract)
        return scope
    except Exception:
        log.exception("plan.run: deriving the action scope failed; defaulting to read-only")
        return intent.GoalScope(goal, frozenset(), frozenset(), (), "error")


def _save_contract(goal_id: str | None, contract: "GoalContract") -> None:
    try:
        from friday.intelligence import goals as goals_mod

        goals_mod.set_contract(goal_id, contract)
    except Exception:
        log.exception("plan.run: saving goal contract failed (non-fatal)")


def _resume_goal(resume_goal_id: str, clarification_answer: str) -> "tuple[str | None, GoalContract, str]":
    """Continue the SAME `Goal` row after a same-goal clarification answer
    (see friday.session.Session's "goal_clarify" `Pending` kind) — never a
    second goal row. Returns `(None, GoalContract(), "")` if the target goal
    has vanished, so the caller falls back to starting a fresh one rather
    than crashing."""
    from friday.intelligence.goals import GoalContract

    try:
        from friday.intelligence import goals as goals_mod

        record = goals_mod.get(resume_goal_id)
        if record is None:
            return None, GoalContract(), ""
        contract = record.contract
        answer = clarification_answer.strip()
        hint = ""
        if answer:
            contract.known_constraints.append(f"User clarified: {answer}")
            hint = f"User clarified: {answer}"
        contract.pending_question = ""
        goals_mod.update_status(resume_goal_id, goals_mod.GoalStatus.RUNNING)
        _save_contract(resume_goal_id, contract)
        return resume_goal_id, contract, hint
    except Exception:
        log.exception("plan.run: resuming goal failed (non-fatal, starting fresh)")
        return None, GoalContract(), ""


def _await_clarification(goal_id: str | None, question: str) -> SkillResult:
    """Pause the SAME goal at `GoalStatus.BLOCKED` rather than failing it —
    `friday.session.Session`'s "goal_clarify" `Pending` kind resumes exactly
    this `goal_id` once the user answers (see `_resume_goal`)."""
    try:
        from friday.intelligence import goals as goals_mod

        if goal_id is not None:
            goals_mod.update_status(goal_id, goals_mod.GoalStatus.BLOCKED)
    except Exception:
        log.exception("plan.run: marking goal BLOCKED failed (non-fatal)")
    speech = question.strip() or "I need more information before I can continue — could you clarify?"
    return SkillResult(
        speech=speech, ok=False,
        data={"awaiting_clarification": True, "goal_id": goal_id, "stopped": "clarification_required"},
    )


async def _run_discovery(
    goal: str, context: str, contract: "GoalContract", goal_id: str | None,
) -> "tuple[str, GoalContract, OrchestratorResult | None]":
    """The bounded, read-only-first discovery pass described in this
    module's docstring — a second call to the *same* `Orchestrator.run_goal`
    method, scoped to L0 (read-only) tools only via the existing allow-list.
    Never blocks the goal on its own failure: any exception here falls back
    to `(context, contract, None)`, meaning the caller proceeds straight to
    ordinary main execution exactly as before this phase."""
    try:
        from friday.intelligence import discovery
        from friday.intelligence.state import INTEL

        l0_tools = [s.name for s in REGISTRY.all() if s.tier == "L0" and s.name != "plan.run"]
        if not l0_tools:
            return context, contract, None

        discovery_orch = Orchestrator(
            tools=l0_tools, actor=current_actor(),
            max_steps=CFG.planner.max_discovery_steps,
            step_timeout_s=CFG.planner.step_timeout_s,
        )
        prompt = (
            f"Investigate before acting: {goal}. Gather evidence only — do not attempt to fix "
            "or change anything yet. Once you have enough evidence, respond done with a summary "
            "of what you found and what remains unknown."
        )
        result = await asyncio.wait_for(
            discovery_orch.run_goal(
                prompt, model=CFG.planner.model, context=context,
                cancel_check=INTEL.is_cancel_requested, discovery_mode=True,
                coverage_goal=goal,
            ),
            timeout=CFG.planner.max_discovery_time_s,
        )
    except Exception:
        log.exception("plan.run: discovery phase failed (non-fatal, falling back to direct execution)")
        return context, contract, None

    if result.stopped == "clarification_required":
        contract.pending_question = result.summary
        _save_contract(goal_id, contract)
        return context, contract, result

    new_context = discovery.build_evidence_block(result.observations, context)
    contract.unknowns = discovery.extract_unknowns(result.summary, result.observations)[
        : CFG.planner.max_evidence_items
    ]
    _save_contract(goal_id, contract)
    return new_context, contract, result


def _derive_success_conditions(observations) -> list[str]:
    """Success conditions are only ever derived from what a tool actually
    reported — never invented criteria (spec §19: no university-style
    requirements dreamed up on FRIDAY's own initiative)."""
    return [
        f"{o.step.tool} confirms: {o.speech.strip()[:150]}"
        for o in observations if o.ok and o.speech
    ][: CFG.planner.max_evidence_items]


def _settle_discovery_status(result: "OrchestratorResult", contract: "GoalContract"):
    from friday.intelligence import evaluator
    from friday.intelligence.goals import GoalStatus

    has_progress = any(o.ok for o in result.observations)
    if result.stopped == "completed" and not contract.unknowns:
        real = [o for o in result.observations if o.error not in NOT_EXECUTED_ERRORS]
        if not real:
            # Phase 19.0: the model said "done" but discovery observed
            # nothing at all — an unverified claim, never SUCCEEDED (see
            # evaluator.evaluate_goal for the main-loop equivalent).
            return GoalStatus.PARTIAL, evaluator.Verdict.UNCERTAIN
        return GoalStatus.SUCCEEDED, evaluator.Verdict.SUCCESS
    if has_progress:
        return GoalStatus.PARTIAL, evaluator.Verdict.UNCERTAIN
    return GoalStatus.FAILED, evaluator.Verdict.FAILURE


def _finish_discovery_only(
    goal_id: str | None, goal: str, context: str, result: "OrchestratorResult",
    contract: "GoalContract", duration_ms: int,
) -> SkillResult:
    """The answer for a purely diagnostic/investigative/information-seeking
    goal (see `_discovery_is_final`) IS the discovery result — this never
    falls through to the main mutating loop. Reports evidence honestly
    (hedged via `guard_against_overclaiming` where unconfirmed) rather than
    claiming a fix that never happened (spec: never "I fixed it" unless a
    fix actually occurred and was verified)."""
    from friday.intelligence import discovery, evaluator
    from friday.intelligence import goals as goals_mod
    from friday.intelligence.goals import GoalStatus

    status, verdict = _settle_discovery_status(result, contract)
    contract.final_verdict = verdict.value
    if not contract.success_conditions:
        contract.success_conditions = _derive_success_conditions(result.observations)

    raw_summary = result.summary or ("Nothing was found." if not result.observations else "")
    speech = discovery.guard_against_overclaiming(raw_summary, result.observations)
    if result.stopped == "completed" and not result.observations and status == GoalStatus.PARTIAL:
        # Phase 19.0: say so plainly instead of presenting an unverified claim as a finding.
        speech = f"I didn't gather any evidence for this, so treat it as unverified: {speech}".strip()
    ok = status != GoalStatus.FAILED

    try:
        from friday.intelligence import episodes
        from friday.intelligence.state import INTEL

        if goal_id is not None:
            goals_mod.update_status(goal_id, status, current_step=len(result.observations))
            goals_mod.set_contract(goal_id, contract)
        # Phase 19.0: see `_finish_goal` — a zero-observation run (planner
        # format failure, or an evidence-free "done") is not experience.
        if result.observations:
            episodes.record(
                goal, goal_id=goal_id, context=context, steps=result.observations,
                stopped=result.stopped, ok=ok, duration_ms=duration_ms,
            )
        INTEL.end_goal(ok=ok, status=status.value)
    except Exception:
        log.exception("plan.run: discovery-only finalization failed (non-fatal)")

    _remember_plan_entities(goal_id, result.observations)

    return SkillResult(
        speech=speech,
        ok=ok,
        data={
            "stopped": result.stopped,
            "goal_id": goal_id,
            "mode": contract.mode,
            "status": status.value,
            "verified": any(o.error not in NOT_EXECUTED_ERRORS for o in result.observations),
            "unknowns": list(contract.unknowns),
            "evidence": [
                {
                    "tool": o.step.tool, "ok": o.ok,
                    "verdict": evaluator.classify_evidence(o), "label": o.speech,
                }
                for o in result.observations
            ],
            "steps": [
                {"tool": o.step.tool, "args": o.step.args, "ok": o.ok, "speech": o.speech, "error": o.error}
                for o in result.observations
            ],
            **_invalid_decision_data(result),
        },
    )


def _cancel_goal(goal_id: str | None, goal: str, subgoals: list["Subgoal"] | None) -> None:
    """Best-effort cleanup for a `plan.run` call whose task got cancelled —
    see the CancelledError handling in `run()` above. Never swallows the
    cancellation itself; only makes sure the tracked Goal doesn't stay
    stuck at RUNNING forever."""
    try:
        from friday.intelligence import goals as goals_mod
        from friday.intelligence.state import INTEL

        if subgoals:
            goals_mod.set_subgoals(goal_id, subgoals)
        if goal_id is not None:
            goals_mod.update_status(
                goal_id, goals_mod.GoalStatus.CANCELLED, failure_reason="cancelled before it finished",
            )
        INTEL.end_goal(ok=False, status="cancelled")
    except Exception:
        log.exception("plan.run: cancellation bookkeeping failed (non-fatal)")
    log.info("plan.run: goal %s cancelled (%r)", goal_id, goal[:60])


def _append_working_memory(goal: str, context: str, observation: object | None) -> str:
    try:
        from friday.intelligence import working_memory

        wm = working_memory.build(goal, observation=observation)
        wm_context = wm.as_context(max_chars=CFG.intelligence.working_memory_max_chars)
        if not wm_context:
            return context
        return f"{context} {wm_context}".strip() if context else wm_context
    except Exception:
        log.exception("plan.run: working-memory context failed (non-fatal)")
        return context


def _append_context_memory(context: str) -> str:
    if not CFG.intelligence.context_memory_enabled:
        return context
    try:
        from friday.intelligence.context_memory import CONTEXT

        ctx_block = CONTEXT.as_context(max_chars=CFG.intelligence.context_block_max_chars)
        if not ctx_block:
            return context
        return f"{context} {ctx_block}".strip() if context else ctx_block
    except Exception:
        log.exception("plan.run: context-memory block failed (non-fatal)")
        return context


def _remember_plan_entities(goal_id: str | None, observations) -> None:
    """Best-effort: every successful step of this plan is a candidate
    referent for the *next* bare command's "it"/"that" — same adapter
    `friday.session.Session._remember_context_entities` uses for the
    simple-command fast path, reused here rather than duplicated. All
    steps of one `plan.run` call share `goal_id` as their turn_id, so two
    files opened by the *same* goal are still treated as introduced
    together (ambiguous) by `context_resolver`, exactly like two files
    named in one sentence on the fast path.
    """
    if not CFG.intelligence.context_memory_enabled:
        return
    try:
        from friday.intelligence import context_memory

        for o in observations:
            if o.ok:
                context_memory.record_from_skill(
                    o.step.tool, o.step.args, o.data, goal_id=goal_id, turn_id=goal_id,
                )
    except Exception:
        log.exception("plan.run: context-memory recording failed (non-fatal)")


def _append_experience(goal: str, context: str) -> str:
    if not CFG.intelligence.experience_enabled:
        return context
    try:
        from friday.intelligence import experience as experience_mod

        exp = experience_mod.retrieve_relevant_experience(goal)
        exp_context = exp.as_context(max_chars=CFG.intelligence.experience_context_max_chars)
        if not exp_context:
            return context
        return f"{context}\n\n{exp_context}".strip() if context else exp_context
    except Exception:
        log.exception("plan.run: experience context failed (non-fatal)")
        return context


def _finish_goal(
    goal_id: str | None, goal: str, context: str, observations, stopped: str, *,
    ok: bool, duration_ms: int, contract: "GoalContract | None" = None, verification=None,
) -> None:
    try:
        from friday.intelligence import episodes, evaluator
        from friday.intelligence import goals as goals_mod
        from friday.intelligence.state import INTEL
        from friday.orchestrator import OrchestratorResult

        # Phase 20.0: blocked repeats / rejected mismatches never ran — they are
        # neither steps of this goal nor experience (`episodes.record` below).
        observations = [o for o in observations if o.error not in NOT_EXECUTED_ERRORS]
        evaluation = evaluator.evaluate_goal(
            OrchestratorResult(goal=goal, observations=observations, ok=ok, summary="", stopped=stopped)
        )
        # Phase 17.0: only a goal actually classified into one of the
        # open-ended modes (mode != "" and != DIRECT_ACTION) is eligible for
        # PARTIAL — every ordinary DIRECT_ACTION/legacy goal keeps the exact
        # pre-Phase-17.0 SUCCEEDED/FAILED-only behavior, so existing
        # regression scenarios are unaffected.
        open_ended = bool(contract and contract.mode and contract.mode != goals_mod.GoalMode.DIRECT_ACTION.value)
        has_progress = any(o.ok for o in observations)
        # Phase 22.0: a run that finished cleanly but changed something nobody could read
        # back is PARTIAL / verdict "uncertain" — never a verified SUCCEEDED (see friday.verify).
        unconfirmed = (
            evaluation.goal_complete and verification is not None
            and getattr(verification, "status", "") in ("unverified", "partial")
        )

        if goal_id is not None:
            if stopped == "cancelled":
                # Phase 16.0: a cooperative mid-plan cancel (see
                # Orchestrator.run_goal's `cancel_check`) is not a failure —
                # give it the dedicated GoalStatus it already has rather
                # than reporting cancelled work as FAILED.
                status = goals_mod.GoalStatus.CANCELLED
            elif unconfirmed:
                status = goals_mod.GoalStatus.PARTIAL
            elif evaluation.goal_complete:
                status = goals_mod.GoalStatus.SUCCEEDED
            elif evaluation.verdict == evaluator.Verdict.UNCERTAIN and not observations:
                # Phase 19.0: "done" with nothing observed — see
                # evaluator.evaluate_goal. Never SUCCEEDED; not FAILED either.
                status = goals_mod.GoalStatus.PARTIAL
            elif open_ended and has_progress:
                # Phase 17.0: real evidence was gathered/acted on even
                # though the goal didn't reach a clean "done" — distinct
                # from FAILED (nothing useful happened at all).
                status = goals_mod.GoalStatus.PARTIAL
            else:
                status = goals_mod.GoalStatus.FAILED
            goals_mod.update_status(
                goal_id, status,
                failure_reason=(
                    "completed, but the result could not be verified: " + "; ".join(verification.unverified)[:300]
                    if unconfirmed else "" if evaluation.goal_complete else evaluation.reason
                ),
                current_step=len(observations),
            )
            if contract is not None:
                contract.final_verdict = evaluator.Verdict.UNCERTAIN.value if unconfirmed else evaluation.verdict.value
                # Phase 21.0: a read-only run records what it really found, so the user's
                # explicit "yes, fix it" can hand the planner the actual findings (context
                # only — see `_expand_goal`). Derived from tool output, never invented.
                if contract.action_scope.get("read_only") and not contract.success_conditions:
                    contract.success_conditions = _derive_success_conditions(observations)
                goals_mod.set_contract(goal_id, contract)

        # Phase 19.0: only real execution/evidence becomes an episode. A run
        # with ZERO observations never did anything — the local planner
        # couldn't produce a valid decision (or Ollama was down), or it
        # declared "done" with nothing observed. Recording that would later
        # surface as a "SUCCEEDED before"/"known failure" experience for a
        # goal that was never actually attempted. The Goal row above still
        # records the truthful status and reason.
        if observations:
            episodes.record(
                goal, goal_id=goal_id, context=context, steps=observations,
                stopped=stopped, ok=ok, duration_ms=duration_ms,
            )
        end_status = (
            "cancelled" if stopped == "cancelled"
            else "partial" if unconfirmed
            else "succeeded" if evaluation.goal_complete
            else "partial" if (evaluation.verdict == evaluator.Verdict.UNCERTAIN and not observations)
            else "failed"
        )
        INTEL.end_goal(ok=ok, status=end_status)
    except Exception:
        log.exception("plan.run: goal/episode finalization failed (non-fatal)")

    _remember_plan_entities(goal_id, observations)
