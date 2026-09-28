"""Phase 11.2 — goal understanding & adaptive task decomposition.

Entirely deterministic — every LLM call in this file goes through a scripted
or conditional fake `LlmProvider`, exactly like scripts/smoke_orchestrator.py
and scripts/smoke_plan.py. No real Ollama, no real WhatsApp/browser calls;
the four `test.gd_wa_*` skills registered below are harmless stand-ins (see
scripts/smoke_conversation.py's `_register_demo_skill` for the precedent),
so the narrative section at the bottom never sends anything real.

Sections A-O below correspond 1:1 to PLAN.md Phase 11.2's test list. The
critical one is C: it proves the next action genuinely depends on the
observed result, not a precomputed sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.evaluator import Verdict, evaluate_step  # noqa: E402
from friday.intelligence.goals import GoalStatus, Subgoal, SubgoalStatus  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR, PermissionError_  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

# -- shared scaffolding, same conventions as smoke_orchestrator.py/smoke_plan.py --

MOCK_SPECS = [
    ToolSpec(name="mock.echo", description="Echo back some text"),
    ToolSpec(name="mock.fail", description="Always fails"),
    ToolSpec(name="mock.slow", description="Hangs"),
    ToolSpec(name="mock.uncertain", description="Succeeds but flags its own result unconfirmed"),
    ToolSpec(name="mock.verify", description="Confirms whether the uncertain action actually landed"),
    ToolSpec(name="mock.probe", description="Reports the current observed state"),
    ToolSpec(name="mock.path_a", description="Branch A of a two-way fork"),
    ToolSpec(name="mock.path_b", description="Branch B of a two-way fork"),
]

CALLS: list[tuple[str, dict]] = []
PROBE_STATE = {"value": "A"}


async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, args))
    if tool == "mock.echo":
        return SkillResult(speech=f"echoed {args.get('text', '')}", data={"echo": args.get("text", "")})
    if tool == "mock.fail":
        return SkillResult(speech="that didn't work", ok=False)
    if tool == "mock.slow":
        await asyncio.sleep(5)
        return SkillResult(speech="finally done")
    if tool == "mock.uncertain":
        return SkillResult(speech="did something, not sure it landed", ok=True, data={"uncertain": True})
    if tool == "mock.verify":
        return SkillResult(speech="verified: it landed", ok=True)
    if tool == "mock.probe":
        return SkillResult(speech=f"observed state {PROBE_STATE['value']}", data={"state": PROBE_STATE["value"]})
    if tool == "mock.path_a":
        return SkillResult(speech="took path A")
    if tool == "mock.path_b":
        return SkillResult(speech="took path B")
    raise KeyError(f"unknown mock tool: {tool}")


class ScriptedPlanner(LlmProvider):
    """Replays a fixed sequence of JSON decisions, one per call. Records
    every prompt sent so tests can inspect what the planner actually saw."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


@contextlib.contextmanager
def scripted_provider(provider: LlmProvider):
    original = llm.get_provider
    llm.get_provider = lambda name=None: provider
    try:
        yield
    finally:
        llm.get_provider = original


def call(
    tool: str, args: dict | None = None, *,
    subgoal_index: int | None = None, expected_outcome: str | None = None,
) -> str:
    payload: dict = {"action": "call", "tool": tool, "args": args or {}}
    if subgoal_index is not None:
        payload["subgoal_index"] = subgoal_index
    if expected_outcome is not None:
        payload["expected_outcome"] = expected_outcome
    return json.dumps(payload)


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


def no_subgoals() -> str:
    return json.dumps({"subgoals": []})


def subgoals_reply(descriptions: list[str]) -> str:
    return json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in descriptions]})


def sg(desc: str) -> Subgoal:
    return Subgoal(id=str(uuid.uuid4()), description=desc)


async def main() -> None:
    # Phase 14.0: isolated from the real, shared data/friday.db (see
    # friday.store.use_temp_db) — this script's own experience-recall
    # check was observed to intermittently fail standalone due to
    # top-k-crowding from accumulated real episodes (PLAN.md Phase
    # 13.0 §7); this removes that dependency.
    db_cm = store.use_temp_db()
    db_cm.__enter__()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    # ========================================================================
    # A — simple command: remains fast/direct, no unnecessary decomposition
    # ========================================================================
    print("\n--- A: simple command stays on the fast path (no decomposition call) ---\n")

    simple_cases = ["Open Notepad", "What's the time?", "Read my screen.", "forget a document"]
    ok = all(not goals_mod.looks_decomposable(t) for t in simple_cases)
    print(f"  {'OK  ' if ok else 'MISS'} looks_decomposable() is False for simple commands -> {simple_cases}")
    overall &= ok

    planner_a = ScriptedPlanner([done("Opened Notepad.")])
    with scripted_provider(planner_a):
        result_a = await EXECUTOR.run("plan.run", {"goal": "just say done, no decomposition needed"}, actor="test")
    ok = result_a.ok and planner_a.calls == 1  # exactly one call: run_goal's own decision, no decompose call
    print(f"  {'OK  ' if ok else 'MISS'} plan.run made exactly 1 planner call for a simple goal (calls={planner_a.calls})")
    overall &= ok

    # ========================================================================
    # B — multi-step goal produces bounded subgoals
    # ========================================================================
    print("\n--- B: multi-step goal decomposes into bounded subgoals ---\n")

    ok = goals_mod.looks_decomposable("Open Chrome and search weather")
    print(f"  {'OK  ' if ok else 'MISS'} looks_decomposable() is True for 'Open Chrome and search weather'")
    overall &= ok

    decomp_planner = ScriptedPlanner([subgoals_reply(["Open Chrome", "Search for weather"])])
    orch_b = Orchestrator(tools=["mock.echo"], runner=mock_runner, llm_provider=decomp_planner, tool_specs=MOCK_SPECS)
    subgoals_b = await orch_b.decompose_goal("Open Chrome and search weather")
    ok = len(subgoals_b) == 2 and subgoals_b[0].description == "Open Chrome" and all(
        s.status == SubgoalStatus.PENDING for s in subgoals_b
    )
    print(f"  {'OK  ' if ok else 'MISS'} decompose_goal() -> {[s.description for s in subgoals_b]}")
    overall &= ok

    # ========================================================================
    # C — THE CRITICAL TEST: the next action genuinely depends on the
    # observed result, not a precomputed sequence. Same planner logic, two
    # runs that differ only in what the mock tool actually observes.
    # ========================================================================
    print("\n--- C: observation-driven adaptation (critical) ---\n")

    class AdaptivePlanner(LlmProvider):
        """Not a fixed script — reacts to what's actually in the prompt's
        observation history, exactly like a real model would."""

        name = "adaptive"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            # "already took a path" is checked first, deliberately, even
            # though the "observed state" line from step 1 is still visible
            # in step 3's prompt too — history is cumulative, not a single
            # latest-observation field.
            text = request.messages[-1].content
            if "called mock.path_a" in text or "called mock.path_b" in text:
                reply = done("finished")
            elif "-> ok: observed state A" in text:
                reply = call("mock.path_a")
            elif "-> ok: observed state B" in text:
                reply = call("mock.path_b")
            else:
                reply = call("mock.probe")
            return LlmResponse(text=reply, model=self.name, provider=self.name)

    PROBE_STATE["value"] = "A"
    CALLS.clear()
    orch_c1 = Orchestrator(
        tools=["mock.probe", "mock.path_a", "mock.path_b"], runner=mock_runner,
        max_steps=5, llm_provider=AdaptivePlanner(), tool_specs=MOCK_SPECS,
    )
    result_c1 = await orch_c1.run_goal("react to whatever the probe observes")
    trace_a = [t for t, _ in CALLS]

    PROBE_STATE["value"] = "B"
    CALLS.clear()
    orch_c2 = Orchestrator(
        tools=["mock.probe", "mock.path_a", "mock.path_b"], runner=mock_runner,
        max_steps=5, llm_provider=AdaptivePlanner(), tool_specs=MOCK_SPECS,
    )
    result_c2 = await orch_c2.run_goal("react to whatever the probe observes")
    trace_b = [t for t, _ in CALLS]

    ok = (
        result_c1.ok and result_c2.ok
        and trace_a == ["mock.probe", "mock.path_a"]
        and trace_b == ["mock.probe", "mock.path_b"]
        and trace_a != trace_b
    )
    print(f"  {'OK  ' if ok else 'MISS'} state A -> {trace_a} | state B -> {trace_b} (identical planner logic, different action)")
    overall &= ok

    # ========================================================================
    # D — successful subgoal progression: subgoal 1 succeeds -> subgoal 2
    # becomes current
    # ========================================================================
    print("\n--- D: subgoal 1 succeeding makes subgoal 2 current ---\n")

    sg0, sg1 = sg("first stage"), sg("second stage")
    planner_d = ScriptedPlanner([
        call("mock.echo", {"text": "one"}),                 # implicit subgoal 0
        call("mock.fail", {}, subgoal_index=1),              # advances to subgoal 1, then fails there
    ])
    orch_d = Orchestrator(
        tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=5,
        llm_provider=planner_d, tool_specs=MOCK_SPECS,
    )
    result_d = await orch_d.run_goal("two-stage goal", subgoals=[sg0, sg1])
    ok = (
        not result_d.ok and result_d.stopped == "failure" and result_d.subgoal_index == 1
        and sg0.status == SubgoalStatus.SUCCEEDED and sg1.status == SubgoalStatus.FAILED
    )
    print(f"  {'OK  ' if ok else 'MISS'} subgoal 0 -> {sg0.status.value}, subgoal 1 (now current) -> {sg1.status.value}")
    overall &= ok

    # ========================================================================
    # E — bounded failure recovery
    # ========================================================================
    print("\n--- E: bounded recovery from a failed action ---\n")

    sg_e = sg("only stage")
    planner_e = ScriptedPlanner([
        call("mock.fail", {}),
        call("mock.echo", {"text": "recovered"}),
        done("recovered after one replan"),
    ])
    orch_e = Orchestrator(
        tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=6,
        llm_provider=planner_e, tool_specs=MOCK_SPECS,
    )
    result_e = await orch_e.run_goal("recoverable goal", subgoals=[sg_e], max_replans=1)
    ok = result_e.ok and sg_e.recovery_attempts == 1 and sg_e.status == SubgoalStatus.SUCCEEDED
    print(f"  {'OK  ' if ok else 'MISS'} recovered -> ok={result_e.ok}, recovery_attempts={sg_e.recovery_attempts}, "
          f"final status={sg_e.status.value}")
    overall &= ok

    # ========================================================================
    # F — repeated-action protection
    # ========================================================================
    print("\n--- F: identical failed action is never repeated forever ---\n")

    sg_f = sg("repeat-prone stage")
    CALLS.clear()
    planner_f = ScriptedPlanner(
        [call("mock.echo", {"text": "same"})] * 3 + [done("never reached")]
    )
    orch_f = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=8,
        llm_provider=planner_f, tool_specs=MOCK_SPECS,
    )
    result_f = await orch_f.run_goal("a goal that keeps repeating itself", subgoals=[sg_f])
    ok = len(CALLS) == 1 and result_f.stopped == "repeated_action" and sg_f.status == SubgoalStatus.FAILED
    print(f"  {'OK  ' if ok else 'MISS'} tool actually invoked {len(CALLS)}x, stopped={result_f.stopped}, "
          f"subgoal -> {sg_f.status.value}")
    overall &= ok

    # ========================================================================
    # G — UNCERTAIN evaluation selects a verification action
    # ========================================================================
    print("\n--- G: an UNCERTAIN result is visible to, and acted on by, the next decision ---\n")

    uncertain_obs = Observation(PlanStep(tool="mock.uncertain"), True, "maybe", data={"uncertain": True})
    ok = evaluate_step(uncertain_obs).verdict == Verdict.UNCERTAIN and evaluate_step(uncertain_obs).success
    print(f"  {'OK  ' if ok else 'MISS'} evaluate_step() classifies a self-flagged-uncertain success as UNCERTAIN "
          f"(and still success=True, never treated as a failure)")
    overall &= ok

    class UncertaintyAwarePlanner(LlmProvider):
        name = "uncertainty-aware"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            # Checked in this order deliberately: "already verified" must win
            # even though the stale "UNCERTAIN" history line from step 1 is
            # still visible in step 3's prompt too (history is cumulative).
            text = request.messages[-1].content
            if "called mock.verify" in text:
                reply = done("verified and complete")
            elif "UNCERTAIN" in text:
                reply = call("mock.verify")
            else:
                reply = call("mock.uncertain")
            return LlmResponse(text=reply, model=self.name, provider=self.name)

    CALLS.clear()
    orch_g = Orchestrator(
        tools=["mock.uncertain", "mock.verify"], runner=mock_runner, max_steps=4,
        llm_provider=UncertaintyAwarePlanner(), tool_specs=MOCK_SPECS,
    )
    result_g = await orch_g.run_goal("do something whose outcome needs confirming")
    ok = result_g.ok and [t for t, _ in CALLS] == ["mock.uncertain", "mock.verify"]
    print(f"  {'OK  ' if ok else 'MISS'} planner chose to verify after seeing UNCERTAIN -> {[t for t, _ in CALLS]}")
    overall &= ok

    # ========================================================================
    # H — permission denial: never replanned around
    # ========================================================================
    print("\n--- H: a permission denial is never replanned, regardless of budget ---\n")

    async def denying_runner(tool: str, args: dict, actor: str) -> SkillResult:
        if tool == "mock.denied":
            raise PermissionError_("policy denies mock.denied")
        return await mock_runner(tool, args, actor)

    sg_h = sg("denied stage")
    planner_h = ScriptedPlanner([
        call("mock.denied", {}),
        call("mock.echo", {"text": "should never run"}),
    ])
    orch_h = Orchestrator(
        tools=["mock.echo", "mock.denied"], runner=denying_runner, max_steps=6,
        llm_provider=planner_h, tool_specs=[*MOCK_SPECS, ToolSpec(name="mock.denied", description="Always denied")],
    )
    result_h = await orch_h.run_goal("a goal that tries a denied tool", subgoals=[sg_h], max_replans=3)
    ok = (
        not result_h.ok and result_h.stopped == "failure" and len(result_h.observations) == 1
        and result_h.observations[0].error == "PermissionError_" and sg_h.status == SubgoalStatus.FAILED
    )
    print(f"  {'OK  ' if ok else 'MISS'} stops immediately, never replanned -> stopped={result_h.stopped}, "
          f"steps={len(result_h.observations)}")
    overall &= ok

    # ========================================================================
    # I — confirmation denial: no alternative action bypasses it
    # ========================================================================
    print("\n--- I: a declined confirmation is never replanned around ---\n")

    @skill(
        name="test.gd_confirm_only",
        tier="L3",
        action="modify",  # Phase 20.0: the goal that drives it is "run the confirm-only test action"
        description="test-only consequential action for smoke_goal_decomposition's confirmation-denial check",
    )
    def _confirm_only() -> SkillResult:
        return SkillResult(speech="did the consequential thing")

    async def always_decline(skill_obj, args, preview) -> bool:
        return False

    EXECUTOR.set_confirm_handler(always_decline)
    original_max_replans = CFG.planner.max_replans
    CFG.planner.max_replans = 2  # budget available on purpose — must still not be used
    planner_i = ScriptedPlanner([
        call("test.gd_confirm_only", {}),
        call("meta.capabilities", {}),  # would prove a bypass if this ever ran
    ])
    with scripted_provider(planner_i):
        result_i = await EXECUTOR.run(
            "plan.run", {"goal": "run the confirm-only test action"}, actor="test",
        )
    CFG.planner.max_replans = original_max_replans
    EXECUTOR.set_confirm_handler(SESSION._confirm)
    ok = (
        not result_i.ok and result_i.data.get("stopped") == "failure"
        and len(result_i.data.get("steps", [])) == 1
        and result_i.data["steps"][0]["error"] == "confirmation_declined"
    )
    print(f"  {'OK  ' if ok else 'MISS'} declined confirmation stops the plan, no alternative action attempted "
          f"-> steps={len(result_i.data.get('steps', []))}, error={result_i.data.get('steps', [{}])[0].get('error')}")
    overall &= ok

    # ========================================================================
    # J — user cancellation terminates the goal
    # ========================================================================
    print("\n--- J: cancelling the task cleanly terminates the tracked goal ---\n")

    class SlowPlanner(LlmProvider):
        name = "slow"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            await asyncio.sleep(5)
            return LlmResponse(text=done("never reached"), model=self.name, provider=self.name)

    with scripted_provider(SlowPlanner()):
        task = asyncio.ensure_future(
            EXECUTOR.run("plan.run", {"goal": "a goal that gets cancelled mid-flight"}, actor="test")
        )
        await asyncio.sleep(0.1)  # let plan.run start and register the goal
        goal_id_j = INTEL.state.current_goal_id
        task.cancel()
        cancelled_cleanly = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled_cleanly = True
    recorded_j = goals_mod.get(goal_id_j) if goal_id_j else None
    ok = cancelled_cleanly and recorded_j is not None and recorded_j.status == GoalStatus.CANCELLED
    print(f"  {'OK  ' if ok else 'MISS'} task cancellation propagated={cancelled_cleanly}, "
          f"goal status={recorded_j.status.value if recorded_j else None}")
    overall &= ok

    # ========================================================================
    # K — max subgoals cannot be exceeded
    # ========================================================================
    print("\n--- K: decomposition is capped at max_subgoals ---\n")

    too_many = subgoals_reply([f"stage {i}" for i in range(15)])

    class BigDecompositionPlanner(LlmProvider):
        name = "big"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            return LlmResponse(text=too_many, model=self.name, provider=self.name)

    orch_k = Orchestrator(tools=[], runner=mock_runner, llm_provider=BigDecompositionPlanner())
    subgoals_k = await orch_k.decompose_goal("a goal with way too many stages", max_subgoals=8)
    ok = len(subgoals_k) == 8
    print(f"  {'OK  ' if ok else 'MISS'} 15 proposed subgoals capped at 8 -> got {len(subgoals_k)}")
    overall &= ok
    ok = CFG.planner.max_subgoals == 8
    print(f"  {'OK  ' if ok else 'MISS'} configured default max_subgoals is 8 -> {CFG.planner.max_subgoals}")
    overall &= ok

    # ========================================================================
    # L — timeout terminates safely
    # ========================================================================
    print("\n--- L: a hanging tool times out safely ---\n")

    sg_l = sg("slow stage")
    planner_l = ScriptedPlanner([call("mock.slow", {})])
    orch_l = Orchestrator(
        tools=["mock.slow"], runner=mock_runner, max_steps=2, step_timeout_s=0.2,
        llm_provider=planner_l, tool_specs=MOCK_SPECS,
    )
    result_l = await orch_l.run_goal("a goal with a hanging tool", subgoals=[sg_l])
    ok = not result_l.ok and result_l.stopped == "timeout" and sg_l.status == SubgoalStatus.FAILED
    print(f"  {'OK  ' if ok else 'MISS'} timed out cleanly -> stopped={result_l.stopped}, subgoal -> {sg_l.status.value}")
    overall &= ok

    # ========================================================================
    # M — malformed planner output fails safely
    # ========================================================================
    print("\n--- M: malformed output (decomposition and per-step) fails safely ---\n")

    class GarbagePlanner(LlmProvider):
        name = "garbage"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            return LlmResponse(text="not json at all, sorry", model=self.name, provider=self.name)

    orch_m1 = Orchestrator(tools=[], runner=mock_runner, llm_provider=GarbagePlanner())
    subgoals_m = await orch_m1.decompose_goal("a goal the model can't decompose")
    ok = subgoals_m == []
    print(f"  {'OK  ' if ok else 'MISS'} decompose_goal() on garbage output -> {subgoals_m}")
    overall &= ok

    orch_m2 = Orchestrator(tools=["mock.echo"], runner=mock_runner, max_steps=3, llm_provider=GarbagePlanner(), tool_specs=MOCK_SPECS)
    result_m2 = await orch_m2.run_goal("a goal the model can't plan at all")
    ok = not result_m2.ok and result_m2.stopped == "planning_failed"
    print(f"  {'OK  ' if ok else 'MISS'} run_goal() on garbage output -> stopped={result_m2.stopped}")
    overall &= ok

    # ========================================================================
    # N — Phase 11.1 experience context remains available to planning
    # ========================================================================
    print("\n--- N: past experience still reaches the planner alongside subgoal context ---\n")

    # Widened the same way scripts/smoke_experience_planning.py does: this
    # runs against the real, shared, ever-growing episode store, and a
    # repeated run of this same script leaves its own near-duplicate episode
    # behind each time (same distinctive phrasing, new random tag) — enough
    # prior runs can otherwise crowd this run's own exact match out of a
    # narrow top-k window.
    original_experience_max_episodes = CFG.intelligence.experience_max_episodes
    CFG.intelligence.experience_max_episodes = 20

    # Distinctive vocabulary specific to *this* script (not shared with
    # scripts/smoke_experience_planning.py's own phrasing) so repeated runs
    # of the two scripts don't crowd each other's matches out of the top-k
    # retrieval window.
    tag_n = uuid.uuid4().hex[:8]
    success_goal_n = f"smoke_gd_{tag_n} recalibrate the tungsten kiln thermocouple"
    similar_goal_n = f"smoke_gd_{tag_n} readjust the tungsten kiln thermocouple"
    # Phase 19.0 fixture fix: the "prior success" used to be a zero-step
    # `done("Calibrated.")` — a planner claim with no observed evidence behind
    # it, which Phase 19.0 (rightly) no longer records as a successful
    # episode. This test is about experience *retrieval*, so its fixture now
    # gives the prior success one real, harmless L0 step (meta.capabilities)
    # before "done" — a genuine success — leaving what it proves unchanged.
    planner_n1 = ScriptedPlanner([call("meta.capabilities"), done("Calibrated.")])
    with scripted_provider(planner_n1):
        result_n1 = await EXECUTOR.run("plan.run", {"goal": success_goal_n}, actor="test")
    planner_n2 = ScriptedPlanner([done("Recalibrated, using what worked before.")])
    with scripted_provider(planner_n2):
        result_n2 = await EXECUTOR.run("plan.run", {"goal": similar_goal_n}, actor="test")
    experience_lines = [
        line for p in planner_n2.prompts for line in p.splitlines()
        if success_goal_n in line and "SUCCEEDED before" in line
    ]
    CFG.intelligence.experience_max_episodes = original_experience_max_episodes
    ok = result_n1.ok and result_n2.ok and bool(experience_lines)
    print(f"  {'OK  ' if ok else 'MISS'} prior success reaches the next similar goal's planner prompt "
          f"-> {experience_lines[:1]}")
    overall &= ok

    # ========================================================================
    # O — registered-tool enforcement
    # ========================================================================
    print("\n--- O: the model can't choose an unregistered/disallowed tool ---\n")

    planner_o = ScriptedPlanner([call("mock.fail", {})])
    orch_o = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=3,
        llm_provider=planner_o, tool_specs=MOCK_SPECS,
    )
    result_o = await orch_o.run_goal("a goal that tries a tool outside the allow-list")
    ok = not result_o.ok and result_o.stopped == "tool_not_allowed"
    print(f"  {'OK  ' if ok else 'MISS'} disallowed tool refused -> stopped={result_o.stopped}")
    overall &= ok

    # ========================================================================
    # Persistence round-trip: the extended Goal model actually survives SQLite
    # ========================================================================
    print("\n--- extra: subgoal breakdown round-trips through the real Goal store ---\n")

    goal_p = goals_mod.create("round-trip test goal", "round-trip test goal")
    subgoals_p = [sg("stage one"), sg("stage two")]
    subgoals_p[0].status = SubgoalStatus.SUCCEEDED
    subgoals_p[1].status = SubgoalStatus.ACTIVE
    subgoals_p[1].recovery_attempts = 2
    goals_mod.set_subgoals(goal_p.id, subgoals_p, current_index=1)
    fetched_p = goals_mod.get(goal_p.id)
    ok = (
        fetched_p is not None and len(fetched_p.subgoals) == 2
        and fetched_p.current_subgoal_index == 1
        and fetched_p.current_subgoal() is not None
        and fetched_p.current_subgoal().description == "stage two"
        and fetched_p.current_subgoal().recovery_attempts == 2
        and len(fetched_p.completed_subgoals()) == 1
        and len(fetched_p.remaining_subgoals()) == 1
    )
    print(f"  {'OK  ' if ok else 'MISS'} Goal.subgoals/current_subgoal_index round-trip through SQLite")
    overall &= ok

    # ========================================================================
    # Narrative smoke: "Open WhatsApp and send Rahul the latest project
    # update." — bounded decomposition, adaptive per-action execution, a
    # subgoal satisfied by reasoning alone gets skipped over cleanly, and the
    # confirmation boundary before the consequential send is never bypassed.
    # Nothing here sends a real message.
    # ========================================================================
    print("\n--- Narrative: WhatsApp project-update goal ---\n")

    @skill(name="test.gd_wa_open", tier="L1", action="open", description="test-only: open WhatsApp (smoke_goal_decomposition)")
    def _wa_open() -> SkillResult:
        return SkillResult(speech="WhatsApp is now foreground.", data={"window": "WhatsApp"})

    @skill(name="test.gd_wa_search", tier="L1", action="navigate", description="test-only: find a WhatsApp chat (smoke_goal_decomposition)")
    def _wa_search(name: Annotated[str, "contact name"] = "") -> SkillResult:
        if name.strip().lower() == "rahul":
            return SkillResult(speech="Rahul's chat found.", data={"chat": "Rahul"})
        return SkillResult(speech=f"No chat found for {name!r}.", ok=False)

    @skill(name="test.gd_wa_compose", tier="L1", action="communicate", description="test-only: compose a WhatsApp message (smoke_goal_decomposition)")
    def _wa_compose(text: Annotated[str, "message text"] = "") -> SkillResult:
        return SkillResult(speech=f"Message composed: {text[:40]}", data={"draft": text})

    @skill(name="test.gd_wa_send", tier="L3", action="communicate", description="test-only: send the composed message (smoke_goal_decomposition)")
    def _wa_send() -> SkillResult:
        # Never actually reached with a "yes" — the test declines the confirmation.
        return SkillResult(speech="Message sent to Rahul.")

    decomposition_reply = subgoals_reply([
        "Open WhatsApp",
        "Find Rahul's chat",
        "Determine the latest project update",
        "Compose the message",
        "Send the message",
    ])
    narrative_replies = [
        decomposition_reply,
        call("test.gd_wa_open", {}, subgoal_index=0, expected_outcome="WhatsApp becomes foreground"),
        call("test.gd_wa_search", {"name": "Rahul"}, subgoal_index=1, expected_outcome="Rahul's chat is found"),
        # Subgoal 2 ("determine the update") needs no tool call — satisfied by
        # reasoning alone, so the very next call jumps straight to subgoal 3.
        call(
            "test.gd_wa_compose", {"text": "Project update: Phase 11.2 shipped."},
            subgoal_index=3, expected_outcome="message is drafted",
        ),
        call("test.gd_wa_send", {}, subgoal_index=4, expected_outcome="message is sent"),
    ]

    original_max_replans = CFG.planner.max_replans
    CFG.planner.max_replans = 2  # budget available on purpose — must still not bypass confirmation

    async def decline_send(skill_obj, args, preview) -> bool:
        return False

    EXECUTOR.set_confirm_handler(decline_send)
    narrative_planner = ScriptedPlanner(narrative_replies)
    with scripted_provider(narrative_planner):
        result_narrative = await EXECUTOR.run(
            "plan.run", {"goal": "Open WhatsApp and send Rahul the latest project update."}, actor="test",
        )
    CFG.planner.max_replans = original_max_replans
    EXECUTOR.set_confirm_handler(SESSION._confirm)

    steps = result_narrative.data.get("steps", [])
    tool_trace = [s["tool"] for s in steps]
    ok = tool_trace == [
        "test.gd_wa_open", "test.gd_wa_search", "test.gd_wa_compose", "test.gd_wa_send",
    ]
    print(f"  {'OK  ' if ok else 'MISS'} adaptive trace -> {tool_trace}")
    overall &= ok

    ok = all(s["ok"] for s in steps[:3]) and not steps[3]["ok"] and steps[3]["error"] == "confirmation_declined"
    print(f"  {'OK  ' if ok else 'MISS'} open/search/compose succeeded; send stopped at the confirmation boundary "
          f"-> {[s['ok'] for s in steps]}, last error={steps[3]['error'] if len(steps) > 3 else None}")
    overall &= ok

    ok = not result_narrative.ok and result_narrative.data.get("stopped") == "failure"
    print(f"  {'OK  ' if ok else 'MISS'} the plan stops cleanly, never invents a way around the declined send "
          f"-> ok={result_narrative.ok}, stopped={result_narrative.data.get('stopped')}")
    overall &= ok

    goal_id_narrative = result_narrative.data.get("goal_id")
    recorded_narrative = goals_mod.get(goal_id_narrative) if goal_id_narrative else None
    ok = recorded_narrative is not None and len(recorded_narrative.subgoals) == 5
    statuses = [s.status.value for s in recorded_narrative.subgoals] if recorded_narrative else []
    ok = ok and statuses == ["succeeded", "succeeded", "succeeded", "succeeded", "failed"]
    print(f"  {'OK  ' if ok else 'MISS'} persisted subgoal breakdown -> {statuses} "
          f"(subgoal 2, 'determine the update', was skipped over via reasoning alone and still credited)")
    overall &= ok

    db_cm.__exit__(None, None, None)
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
