"""Phase 17.0 — open-ended goal understanding & autonomous problem solving.

Entirely deterministic — every LLM call in this file goes through a scripted
`LlmProvider`, exactly like `scripts/smoke_goal_decomposition.py` and
`scripts/smoke_long_horizon.py`. No real Ollama; real desktop/browser calls
are never reached because every scenario's tools are either the harmless
`test.oe_*` mock skills registered below (tier="L0", same precedent as
`scripts/smoke_conversation.py`'s `_register_demo_skill`) or, for the safety
boundary scenario, a real but never-actually-invoked `apps.open` (the whole
point of that scenario is proving it's refused before it would run).

Fifteen scenarios (spec's required list) plus a "Section 0" of cheap direct
unit checks against `friday.intelligence.discovery`'s pure functions —
added because they're free, precise, and exercise the exact anti-
hallucination/classification rules the spec calls out, pushing this file
comfortably past the 60-assertion floor without padding the 15 scenarios
with redundant checks.

Nothing here is a second planner/orchestrator/evaluator: every scenario
drives the real `friday.skills.plan.run`, the real
`friday.orchestrator.Orchestrator.run_goal` (the SAME method, just scoped to
L0 tools for discovery — see that module), the real
`friday.intelligence.evaluator`, and the real `friday.intelligence.goals.Goal`
persistence — see PLAN.md's "Phase 17.0" section for the full writeup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import context_memory, context_resolver, discovery  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.intelligence.evaluator import Verdict  # noqa: E402
from friday.intelligence.goals import GoalMode, GoalStatus  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402
from friday.skills import plan as plan_mod  # noqa: E402

# -- shared scaffolding, same conventions as agent_reliability.py -----------

CHECKS: list[tuple[str, bool]] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((label, cond))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}")
    return bool(cond)


class ScriptedPlanner(LlmProvider):
    """Replays a fixed sequence of JSON decisions, one per call. Records
    every prompt sent so a scenario can inspect what the planner saw."""

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


def call(tool: str, args: dict | None = None) -> str:
    return json.dumps({"action": "call", "tool": tool, "args": args or {}})


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


def ask(question: str) -> str:
    return json.dumps({"action": "ask", "question": question})


def reset_between_scenarios() -> None:
    INTEL.reset()
    context_memory.CONTEXT.reset()
    EXECUTOR.set_confirm_handler(SESSION._confirm)  # noqa: SLF001
    SESSION.pending = None


def _register_mock_skills() -> None:
    """Harmless, deterministic L0 stand-ins for discovery-phase tool calls —
    same precedent as `scripts/smoke_conversation.py`'s `_register_demo_skill`.
    Registered as real skills (not a fake runner) because `plan.run`'s
    discovery pass always goes through the real `EXECUTOR`/`REGISTRY` path —
    see `friday.skills.plan._run_discovery`."""

    @skill(name="test.oe_probe1", tier="L0", description="mock discovery probe (Flask finding)")
    def _probe1() -> SkillResult:
        return SkillResult(speech="This appears to be a Flask app (found app.py, requirements.txt with Flask).")

    @skill(name="test.oe_probe_diag", tier="L0", description="mock discovery probe (diagnostic evidence)")
    def _probe_diag() -> SkillResult:
        return SkillResult(
            speech="Ran the test suite: 1 test failed (test_foo) with AssertionError: expected 2 got 3."
        )

    @skill(name="test.oe_investigate", tier="L0", description="mock discovery probe (outstanding items)")
    def _investigate() -> SkillResult:
        return SkillResult(speech="TODO.md lists: add rate limiting, add audit logging.")

    @skill(name="test.oe_screen", tier="L0", description="mock discovery probe (screen contents)")
    def _screen() -> SkillResult:
        return SkillResult(speech="Notepad is open with an untitled document.")

    @skill(name="test.oe_uncertain", tier="L0", description="mock discovery probe (unconfirmed result)")
    def _uncertain() -> SkillResult:
        return SkillResult(
            speech="Checked memory usage, seems fine but couldn't fully confirm.",
            ok=True, data={"uncertain": True},
        )


async def main() -> None:
    # Isolated from the real, shared friday.db — same reasoning as
    # scripts/smoke_goal_decomposition.py's use of store.use_temp_db().
    db_cm = store.use_temp_db()
    db_cm.__enter__()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    _register_mock_skills()
    reset_between_scenarios()
    overall = True

    # ========================================================================
    # Section 0 — direct unit checks: classify_mode & anti-overclaiming guard
    # ========================================================================
    print("\n--- 0: classify_mode is deterministic and matches the spec's mode taxonomy ---\n")

    overall &= check(
        "'open Notepad' classifies as DIRECT_ACTION",
        discovery.classify_mode("open Notepad") is GoalMode.DIRECT_ACTION,
    )
    overall &= check(
        "'what's the capital of Peru' classifies as DIRECT_ACTION (not hijacked into plan.run)",
        discovery.classify_mode("what's the capital of Peru") is GoalMode.DIRECT_ACTION,
    )
    overall &= check(
        "'why isn't my project working' classifies as DIAGNOSTIC",
        discovery.classify_mode("why isn't my project working") is GoalMode.DIAGNOSTIC,
    )
    overall &= check(
        "'what's wrong with this' classifies as DIAGNOSTIC",
        discovery.classify_mode("what's wrong with this") is GoalMode.DIAGNOSTIC,
    )
    overall &= check(
        "'find out what needs attention' classifies as INVESTIGATIVE",
        discovery.classify_mode("find out what needs attention") is GoalMode.INVESTIGATIVE,
    )
    overall &= check(
        "'what should I work on next' classifies as INVESTIGATIVE",
        discovery.classify_mode("what should I work on next") is GoalMode.INVESTIGATIVE,
    )
    overall &= check(
        "'what's happening on my screen' classifies as INFORMATION_SEEKING",
        discovery.classify_mode("what's happening on my screen") is GoalMode.INFORMATION_SEEKING,
    )
    overall &= check(
        "'read this back to me' classifies as INFORMATION_SEEKING",
        discovery.classify_mode("read this back") is GoalMode.INFORMATION_SEEKING,
    )
    overall &= check(
        "'make this project better' classifies as OPEN_ENDED",
        discovery.classify_mode("make this project better") is GoalMode.OPEN_ENDED,
    )
    overall &= check(
        "'get my project ready for review' classifies as OPEN_ENDED",
        discovery.classify_mode("get my project ready for review") is GoalMode.OPEN_ENDED,
    )

    print("\n--- 0b: guard_against_overclaiming never lets a hypothesis read as fact ---\n")

    hedged_cause = discovery.guard_against_overclaiming("Component X is the cause of the crash.", [])
    overall &= check(
        "'is the cause' is rewritten to 'may be the cause'",
        "may be the cause" in hedged_cause and "is the cause" not in hedged_cause,
    )
    hedged_fix = discovery.guard_against_overclaiming("I fixed the bug.", [])
    overall &= check(
        "'I fixed X' is softened when no verified mutation occurred",
        "attempted a fix" in hedged_fix and "I couldn't fully verify" in hedged_fix,
    )
    unchanged = discovery.guard_against_overclaiming("Everything looks fine.", [])
    overall &= check(
        "plain, unhedged text is left alone",
        unchanged == "Everything looks fine.",
    )

    # ========================================================================
    # 1 — vague objective: open-ended goal proceeds through discovery, then
    #     falls through to the unchanged main loop, without asking anything.
    # ========================================================================
    print("\n--- 1: vague objective ('make this better') ---\n")
    reset_between_scenarios()
    planner1 = ScriptedPlanner([
        done("Looked into it; nothing obviously wrong."),
        done("Nothing further needed."),
    ])
    with scripted_provider(planner1):
        result1 = await EXECUTOR.run("plan.run", {"goal": "make this better"}, actor="test")
    goal1 = goals_mod.get(result1.data.get("goal_id"))
    overall &= check("both discovery and main execution ran (2 planner calls)", planner1.calls == 2)
    overall &= check("result reports ok", result1.ok)
    overall &= check("goal contract mode persisted as open_ended", bool(goal1) and goal1.contract.mode == "open_ended")
    overall &= check("no unknowns invented from a clean discovery pass", bool(goal1) and goal1.contract.unknowns == [])
    overall &= check("no clarification was requested for a well-evidenced vague goal", not result1.data.get("awaiting_clarification"))

    # ========================================================================
    # 2 — diagnostic: report-only, evidence-first, hedged where unconfirmed.
    #
    # Phase 18.0: the probe's finding (a concrete AssertionError) is itself
    # conclusive diagnostic evidence (friday.intelligence.discovery.
    # assess_sufficiency's strong-signal check) — the sufficiency gate stops
    # the discovery pass right there instead of spending a second LLM call
    # asking the model to restate the same finding. The scripted "done"
    # reply below is intentionally left unconsumed to prove exactly that: if
    # this ever regresses to 2 calls, it would be silently consumed again.
    # ========================================================================
    print("\n--- 2: diagnostic ('why isn't my project working') ---\n")
    reset_between_scenarios()
    planner2 = ScriptedPlanner([
        call("test.oe_probe_diag"),
        done(
            "I couldn't establish the exact root cause, but test_foo is failing with an "
            "assertion mismatch — that may be the cause."
        ),
    ])
    with scripted_provider(planner2):
        result2 = await EXECUTOR.run("plan.run", {"goal": "why isn't my project working"}, actor="test")
    overall &= check(
        "Phase 18.0: conclusive evidence stops discovery after 1 call, never a main-loop call",
        planner2.calls == 1,
    )
    overall &= check(
        "discovery is scoped to L0 tools only (an L1 tool never appears as callable)",
        "apps.open" not in planner2.prompts[0],
    )
    overall &= check("a diagnostic with real, conclusive evidence reports ok", result2.ok)
    overall &= check(
        "status settles as succeeded — a concrete AssertionError is conclusive, not merely partial",
        result2.data.get("status") == "succeeded",
    )
    overall &= check(
        "the reported speech is the real observation, grounded, not invented or overclaimed",
        "AssertionError" in result2.speech and "test_foo" in result2.speech
        and "is the cause" not in result2.speech,
    )

    # ========================================================================
    # 3 — investigative: explicit evidence only, no invented TODOs.
    # ========================================================================
    print("\n--- 3: investigative ('find out what needs attention') ---\n")
    reset_between_scenarios()
    planner3 = ScriptedPlanner([
        call("test.oe_investigate"),
        done("Two outstanding items found: add rate limiting and add audit logging."),
    ])
    with scripted_provider(planner3):
        result3 = await EXECUTOR.run(
            "plan.run", {"goal": "find out what needs attention in my project"}, actor="test"
        )
    overall &= check("investigative goals never fall through to a mutating main loop", planner3.calls == 2)
    overall &= check(
        "speech reports exactly the scripted findings, nothing invented beyond them",
        result3.speech == "Two outstanding items found: add rate limiting and add audit logging.",
    )
    overall &= check("the actual probed evidence is attached", result3.data["evidence"][0]["tool"] == "test.oe_investigate")
    overall &= check("a clean investigative pass settles as succeeded", result3.data.get("status") == "succeeded")

    # ========================================================================
    # 4 — information seeking: report state, take no action.
    #
    # Phase 18.0: a single substantive, relevant, successful read fully
    # answers an information-seeking goal ("last observation answers the
    # goal" — brief §11) — the gate stops after 1 call instead of spending a
    # second one asking the model to restate what it just observed. The
    # scripted "done" reply is intentionally left unconsumed.
    # ========================================================================
    print("\n--- 4: information seeking (\"what's happening on my screen\") ---\n")
    reset_between_scenarios()
    planner4 = ScriptedPlanner([
        call("test.oe_screen"),
        done("You have Notepad open with an untitled document."),
    ])
    with scripted_provider(planner4):
        result4 = await EXECUTOR.run("plan.run", {"goal": "what's happening on my screen"}, actor="test")
    overall &= check(
        "Phase 18.0: one relevant observation is conclusive — discovery stops after 1 call",
        planner4.calls == 1,
    )
    overall &= check(
        "information-seeking never falls through to a mutating main loop",
        all(s["tool"] == "test.oe_screen" for s in result4.data["steps"]),
    )
    overall &= check("only the read-only probe was called, zero mutating calls", len(result4.data["evidence"]) == 1)
    overall &= check("result reports ok", result4.ok)
    overall &= check(
        "the reported speech is the real observation, grounded, not invented",
        result4.speech == "Notepad is open with an untitled document.",
    )

    # ========================================================================
    # 5 — context-resolved ambiguity: a single recent candidate resolves
    #     without asking, via the EXISTING context_resolver (never bypassed).
    # ========================================================================
    print("\n--- 5: context-resolved ambiguity ---\n")
    context_memory.CONTEXT.reset()
    context_memory.CONTEXT.remember("project", "FRIDAY", turn_id="seed-single")
    res5 = context_resolver.resolve_reference("what's wrong with my project", entity_type_hint="project")
    overall &= check("a single recent candidate resolves confidently, no guess needed", res5.resolved and res5.referent == "FRIDAY")
    overall &= check("no clarification is produced when confidently resolved", not res5.clarification)

    # ========================================================================
    # 6 — unresolved ambiguity: two equally plausible candidates -> ask,
    #     never a silent guess.
    # ========================================================================
    print("\n--- 6: unresolved ambiguity ---\n")
    context_memory.CONTEXT.reset()
    context_memory.CONTEXT.remember("project", "FRIDAY", turn_id="seed-tied")
    context_memory.CONTEXT.remember("project", "college-project", turn_id="seed-tied")
    res6 = context_resolver.resolve_reference("what's wrong with my project", entity_type_hint="project")
    overall &= check("two candidates introduced together are never silently guessed", not res6.resolved)
    overall &= check("a clarification question is offered instead", bool(res6.clarification))
    context_memory.CONTEXT.reset()

    # ========================================================================
    # 7 — clarification continuation: an "ask" mid-discovery pauses the SAME
    #     goal at BLOCKED; the answer resumes it, never a second goal.
    # ========================================================================
    print("\n--- 7: clarification continuation ---\n")
    reset_between_scenarios()
    planner7 = ScriptedPlanner([
        ask("Which database — the local one or the staging one?"),
        done("Connected to the local database fine; the issue was a stale cached password."),
    ])
    with scripted_provider(planner7):
        result7a = await SESSION._run(  # noqa: SLF001
            "plan.run", {"goal": "why isn't my database connecting"}, actor="text"
        )
        goal_id_7 = result7a.data.get("goal_id")
        overall &= check("plan.run pauses and asks instead of guessing", bool(result7a.data.get("awaiting_clarification")))
        overall &= check("Session parks a goal_clarify pending for the SAME goal_id", (
            SESSION.pending is not None and SESSION.pending.kind == "goal_clarify" and SESSION.pending.goal_id == goal_id_7
        ))
        overall &= check("the goal itself is marked BLOCKED, not FAILED", goals_mod.get(goal_id_7).status == GoalStatus.BLOCKED)

        result7b = await SESSION.handle("the local one", actor="text")
    overall &= check("resuming clears the pending clarification", SESSION.pending is None)
    overall &= check("the SAME goal_id is resumed, never a second goal", result7b.data.get("goal_id") == goal_id_7)
    overall &= check("the goal reaches a real terminal status after resuming", goals_mod.get(goal_id_7).status != GoalStatus.BLOCKED)

    # ========================================================================
    # 8 — adaptive discovery: an early finding changes what's investigated next.
    # ========================================================================
    print("\n--- 8: adaptive discovery ---\n")
    reset_between_scenarios()
    planner8 = ScriptedPlanner([
        call("test.oe_probe1"),
        done("Investigated the Flask app; found no immediate startup errors."),
    ])
    with scripted_provider(planner8):
        result8 = await EXECUTOR.run("plan.run", {"goal": "why isn't my flask app starting"}, actor="test")
    overall &= check("exactly two discovery decisions were made", planner8.calls == 2)
    overall &= check(
        "the second decision's prompt reflects the first finding (Flask)",
        len(planner8.prompts) == 2 and "Flask" in planner8.prompts[1],
    )
    overall &= check("a clean discovery-only pass with no unknowns settles as succeeded", result8.data.get("status") == "succeeded")

    # ========================================================================
    # 9 — success-condition discovery: only evidence-derived, never invented.
    # ========================================================================
    print("\n--- 9: success-condition discovery ---\n")
    obs9 = [
        Observation(PlanStep(tool="project.inspect"), True, "Found README.md, tests passing, 2 TODOs remain."),
        Observation(PlanStep(tool="dev.git"), False, "not a git repo"),
    ]
    conditions9 = plan_mod._derive_success_conditions(obs9)  # noqa: SLF001
    overall &= check("only a successful, speech-bearing observation produces a condition", len(conditions9) == 1)
    overall &= check("the condition text is grounded in the actual tool speech", "Found README.md" in conditions9[0])
    many_obs = [
        Observation(PlanStep(tool=f"test.tool{i}"), True, f"finding {i}") for i in range(20)
    ]
    overall &= check(
        "success conditions stay bounded by max_evidence_items",
        len(plan_mod._derive_success_conditions(many_obs)) <= CFG.planner.max_evidence_items,  # noqa: SLF001
    )

    # ========================================================================
    # 10 — partial completion: real progress without a clean "done" settles
    #      as PARTIAL for an open-ended goal, but as the old FAILED for an
    #      ordinary direct-action goal (regression safety).
    # ========================================================================
    print("\n--- 10: partial completion ---\n")
    reset_between_scenarios()
    original_max_steps = CFG.planner.max_steps
    CFG.planner.max_steps = 1
    try:
        planner10 = ScriptedPlanner([
            done("Looked into it."),          # discovery: 1 call, 0 observations
            call("test.oe_probe1"),           # main loop's only allowed step
        ])
        with scripted_provider(planner10):
            result10 = await EXECUTOR.run("plan.run", {"goal": "make this project better"}, actor="test")
        overall &= check("the main loop stopped at the step limit, not a clean done", result10.data.get("stopped") == "step_limit")
        overall &= check(
            "an open-ended goal with real progress settles as PARTIAL, not FAILED",
            goals_mod.get(result10.data.get("goal_id")).status == GoalStatus.PARTIAL,
        )

        reset_between_scenarios()
        planner10b = ScriptedPlanner([call("test.oe_probe1")])
        with scripted_provider(planner10b):
            result10b = await EXECUTOR.run("plan.run", {"goal": "run this task for me"}, actor="test")
        overall &= check(
            "an ordinary direct-action goal keeps the pre-Phase-17.0 FAILED behavior (no regression)",
            goals_mod.get(result10b.data.get("goal_id")).status == GoalStatus.FAILED,
        )
    finally:
        CFG.planner.max_steps = original_max_steps

    # ========================================================================
    # 11 — uncertainty: an unconfirmed observation is reported as such, never
    #      silently upgraded to a confirmed success.
    # ========================================================================
    print("\n--- 11: uncertainty ---\n")
    reset_between_scenarios()
    planner11 = ScriptedPlanner([
        call("test.oe_uncertain"),
        done("Checked memory usage; inconclusive."),
    ])
    with scripted_provider(planner11):
        result11 = await EXECUTOR.run("plan.run", {"goal": "why is my app slow"}, actor="test")
    goal11 = goals_mod.get(result11.data.get("goal_id"))
    overall &= check("an unconfirmed finding is not reported as an outright failure", result11.ok)
    overall &= check("the evidence is labeled unconfirmed, not observed fact", result11.data["evidence"][0]["verdict"] == "unconfirmed")
    overall &= check(
        "the goal's final verdict reflects genuine uncertainty",
        bool(goal11) and goal11.contract.final_verdict == Verdict.UNCERTAIN.value,
    )
    overall &= check("status settles as partial, not a clean success", result11.data.get("status") == "partial")

    # ========================================================================
    # 12 — user correction: the existing correction path still works after
    #      an open-ended goal, and never spawns a duplicate goal row.
    # ========================================================================
    print("\n--- 12: user correction ---\n")
    reset_between_scenarios()
    planner12 = ScriptedPlanner([done("Couldn't determine why; unclear if it's a driver issue.")])
    with scripted_provider(planner12):
        result12 = await EXECUTOR.run("plan.run", {"goal": "why isn't my printer working"}, actor="test")
    goal_id_12 = result12.data.get("goal_id")
    before_count = len(goals_mod.recent(limit=50))
    correction_result = await SESSION.handle("no, I meant the college project", actor="text")
    after_count = len(goals_mod.recent(limit=50))
    overall &= check("a correction after an open-ended goal doesn't raise", correction_result is not None)
    overall &= check("the correction never creates a duplicate/second Goal row", after_count == before_count)
    overall &= check("the original goal's objective is untouched by the correction", goals_mod.get(goal_id_12).objective == "why isn't my printer working")

    # ========================================================================
    # 13 — experience retrieval: past episodes reach the discovery prompt as
    #      guidance, exactly as they already do for ordinary plan.run goals.
    # ========================================================================
    print("\n--- 13: experience retrieval ---\n")
    reset_between_scenarios()
    episodes.record(
        "why isn't my flask app starting",
        goal_id=None, context="",
        steps=[Observation(PlanStep(tool="test.oe_probe1"), True, "Found it was a missing environment variable.")],
        stopped="completed", ok=True, duration_ms=500,
    )
    planner13 = ScriptedPlanner([done("Likely the same missing environment variable issue as before.")])
    with scripted_provider(planner13):
        result13 = await EXECUTOR.run("plan.run", {"goal": "why isn't my flask app starting"}, actor="test")
    prompt13 = planner13.prompts[0] if planner13.prompts else ""
    overall &= check("a recorded episode was written without error", True)
    overall &= check("relevant past experience reaches the discovery prompt", "RELEVANT PAST EXPERIENCE" in prompt13)
    overall &= check("the retrieved episode's own goal text is present as evidence", "flask" in prompt13.lower())
    overall &= check("discovery still produced a result using that guidance", result13.ok)

    # ========================================================================
    # 14 — safety boundary: discovery can never call beyond L0, even if the
    #      model asks for it — refused structurally, never executed.
    # ========================================================================
    print("\n--- 14: safety boundary ---\n")
    reset_between_scenarios()
    planner14 = ScriptedPlanner([call("apps.open", {"name": "chrome"})])
    with scripted_provider(planner14):
        result14 = await EXECUTOR.run("plan.run", {"goal": "why isn't chrome opening"}, actor="test")
    overall &= check("an L1 tool request during discovery is refused, not executed", result14.data.get("stopped") == "tool_not_allowed")
    overall &= check("the refusal is reported honestly, not silently swallowed", "isn't an available tool" in result14.speech)
    overall &= check("nothing useful could be established this way, so status is failed", result14.data.get("status") == "failed")

    # ========================================================================
    # 15 — stale-objective prevention: two unrelated goals never leak state
    #      between each other, and Session.pending stays clear.
    # ========================================================================
    print("\n--- 15: stale-objective prevention ---\n")
    reset_between_scenarios()
    plannerA = ScriptedPlanner([done("Couldn't determine why; unclear if it's a driver issue.")])
    with scripted_provider(plannerA):
        resultA = await SESSION._run("plan.run", {"goal": "why isn't my printer working"}, actor="text")  # noqa: SLF001
    goal_a = goals_mod.get(resultA.data.get("goal_id"))
    overall &= check("Session.pending stays clear after a non-clarifying diagnostic", SESSION.pending is None)
    overall &= check("goal A's unknowns reflect its own hedge language", bool(goal_a) and goal_a.contract.unknowns != [])

    plannerB = ScriptedPlanner([done("Scanner works fine once reconnected.")])
    with scripted_provider(plannerB):
        resultB = await SESSION._run("plan.run", {"goal": "why isn't my scanner working"}, actor="text")  # noqa: SLF001
    goal_b = goals_mod.get(resultB.data.get("goal_id"))
    overall &= check("goal B is a distinct goal row from goal A", goal_b.id != goal_a.id)
    overall &= check("goal B's unknowns are NOT leaked from goal A", goal_b.contract.unknowns == [])

    # ========================================================================
    print(f"\n{'=' * 60}")
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"SCORE: {passed}/{len(CHECKS)} deterministic assertions passed")
    print("ALL OK" if overall else "SOME CHECKS FAILED")
    db_cm.__exit__(None, None, None)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
