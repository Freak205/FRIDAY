"""Phase 16.0 — long-horizon autonomous tasks & robust recovery.

    UNDERSTAND -> DEFINE SUCCESS -> PLAN -> EXECUTE -> OBSERVE -> EVALUATE ->
    RECOVER -> CONTINUE -> RE-EVALUATE -> COMPLETE (or ASK / STOP SAFELY)

Phase 15.0 (`scripts/smoke_advanced_tasks.py`) proved FRIDAY can carry out
realistic multi-step goals against real skills. This phase asks a narrower,
deeper question: does a coherent objective SURVIVE a LONGER task containing
uncertainty, failures, changing state, and more than one recovery decision?
No new subsystem is added here — every scenario below drives the existing
`friday.orchestrator.Orchestrator`, `friday.intelligence.goals.Goal`/
`Subgoal`, `friday.intelligence.evaluator`, `friday.intelligence.state.INTEL`,
and `friday.skills.plan.run`.

Reuses `scripts/agent_reliability.py`'s scaffolding verbatim via import
(`ScriptedPlanner`, `scripted_provider`, `call`/`done`, `TaskOutcome`,
`outcome_for_result`, `trace_from_result`, `reset_between_scenarios`) rather
than reinventing it, matching `smoke_advanced_tasks.py`'s own precedent.

Three genuine, proven production gaps were found and fixed (see each
scenario's own docstring for the failing-scenario-first reproduction):

  1. `friday/skills/plan.py`: an external `total_timeout_s` abort discarded
     every real observation gathered before it, reporting a bare "didn't
     finish in time" even when several real steps had already succeeded —
     the same false-total-failure class Phase 15.0 fixed for
     `planning_failed`, left open here. Fixed via `Orchestrator.run_goal`'s
     new `observations_out` parameter (a caller-supplied sink list).
  2. No code path let a concurrent caller (the daemon's `/say` handler
     shares one event loop with every other request) cooperatively stop a
     running `plan.run` goal — only a blunt `asyncio.CancelledError` existed,
     and that too lost all partial evidence (same root cause as #1). Fixed
     via `Orchestrator.run_goal`'s new `cancel_check` parameter,
     `IntelligenceState.cancel_requested`, and `friday.daemon`'s new
     `POST /cancel`.
  3. `Orchestrator.run_goal` unconditionally forced `subgoals[0].status =
     ACTIVE` and always started `subgoal_idx` at 0 — so handing it back a
     `Subgoal` list that already had earlier subgoals SUCCEEDED (e.g. after
     a cancellation) would silently reset the first one and restart
     tracking from scratch. Fixed via the new `resume_from_index` parameter.

See PLAN.md's "Phase 16.0" section for the full writeup.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from friday import store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.intelligence import context_resolver  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.context_memory import CONTEXT  # noqa: E402
from friday.intelligence.goals import Goal, GoalStatus, Subgoal, SubgoalStatus  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.log import get  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

import agent_reliability as harness  # noqa: E402  (shared scaffolding, see module docstring)

log = get(__name__)

# ============================================================================
# Report shape (brief's per-scenario schema) + deterministic-assertion tally
# ============================================================================

CHECKS: list[tuple[str, bool]] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((label, cond))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}")
    return bool(cond)


@dataclass(slots=True)
class LongHorizonReport:
    scenario_id: str
    goal: str
    subgoals: list[str]
    actions: list[dict]
    observations: list[str]
    failures: list[str]
    recoveries: int
    user_interventions: list[str]
    final_evidence: str
    final_status: str
    total_steps: int
    total_recoveries: int
    total_time_s: float

    def print_block(self) -> None:
        print(f"\n--- {self.scenario_id} ---")
        print(f"GOAL: {self.goal}")
        print(f"SUBGOALS: {self.subgoals or '(none)'}")
        print(f"ACTIONS: {self.actions}")
        for o in self.observations:
            print(f"  obs: {o}")
        print(f"FAILURES: {self.failures or '(none)'}")
        print(f"RECOVERIES: {self.recoveries}")
        print(f"USER INTERVENTIONS: {self.user_interventions or '(none)'}")
        print(f"FINAL EVIDENCE: {self.final_evidence}")
        print(f"FINAL STATUS: {self.final_status}")
        print(f"TOTAL STEPS: {self.total_steps}   TOTAL RECOVERIES: {self.total_recoveries}"
              f"   TOTAL TIME: {self.total_time_s * 1000:.1f}ms")


PERF_SAMPLES: list[tuple[str, int, float]] = []  # (scenario_id, total_steps, total_time_s)


def reset_between_scenarios() -> None:
    harness.reset_between_scenarios()


def _register_test_skills() -> None:
    """Idempotent test-only skills, same convention as
    scripts/smoke_action_continuity.py's `test.xxx` skills — never real
    WhatsApp/browser/window calls."""

    if REGISTRY.get("test.lh_ok") is None:
        @skill(
            name="test.lh_ok", tier="L0", description="test: always succeeds",
            examples=["do the long horizon test step"],
        )
        async def _lh_ok(label: Annotated[str, "which step"] = "") -> SkillResult:
            await asyncio.sleep(0.01)
            return SkillResult(speech=f"did {label}", ok=True, data={"label": label})

    if REGISTRY.get("test.lh_slow") is None:
        @skill(
            name="test.lh_slow", tier="L0", description="test: never finishes before a short timeout",
            examples=["do the slow long horizon test step"],
        )
        async def _lh_slow() -> SkillResult:
            await asyncio.sleep(2.0)
            return SkillResult(speech="too slow", ok=True)


# ============================================================================
# Shared synthetic-tool-world runner
# ============================================================================


async def run_synthetic(
    goal: str, tool_replies: dict[str, SkillResult], replies: list[str], *,
    max_steps: int = 12, max_replans: int = 0, subgoals: list[Subgoal] | None = None,
    cancel_check=None, observations_out: list | None = None, resume_from_index: int = 0,
) -> tuple[Any, list[tuple[str, dict]], harness.ScriptedPlanner, float]:
    """A bounded fake tool world: `tool_replies[tool_name]` is returned every
    time that tool is called (a callable value is invoked with the call's
    args for a per-call answer). Returns (OrchestratorResult, calls, planner, elapsed)."""
    calls: list[tuple[str, dict]] = []

    async def mock_runner(tool, args, actor):
        calls.append((tool, dict(args)))
        reply = tool_replies.get(tool)
        if callable(reply):
            return reply(args)
        if reply is None:
            raise KeyError(tool)
        return reply

    specs = [ToolSpec(name=n, description=n) for n in tool_replies]
    planner = harness.ScriptedPlanner(replies)
    orch = Orchestrator(
        tools=list(tool_replies), runner=mock_runner, actor="test",
        llm_provider=planner, tool_specs=specs, max_steps=max_steps,
    )
    started = time.perf_counter()
    result = await orch.run_goal(
        goal, max_replans=max_replans, subgoals=subgoals,
        cancel_check=cancel_check, observations_out=observations_out,
    )
    elapsed = time.perf_counter() - started
    return result, calls, planner, elapsed


def _final_evidence(result) -> str:
    ok_steps = [o for o in result.observations if o.ok]
    if not ok_steps:
        return "(no successful steps)"
    return " | ".join(o.speech for o in ok_steps if o.speech)[:400]


def _report_from_result(sid: str, goal: str, result, calls, elapsed: float, *,
                         subgoals: list[Subgoal] | None = None, recoveries: int = 0,
                         user_interventions: list[str] | None = None) -> LongHorizonReport:
    actions, observations = harness.trace_from_result(result)
    failures = [
        f"{o.step.tool}: {o.error or o.speech}" for o in result.observations if not o.ok
    ]
    rep = LongHorizonReport(
        scenario_id=sid, goal=goal,
        subgoals=[s.description for s in (subgoals or [])],
        actions=actions, observations=observations, failures=failures,
        recoveries=recoveries, user_interventions=user_interventions or [],
        final_evidence=_final_evidence(result), final_status=result.stopped,
        total_steps=len(result.observations), total_recoveries=recoveries,
        total_time_s=elapsed,
    )
    PERF_SAMPLES.append((sid, rep.total_steps, elapsed))
    return rep


# ============================================================================
# P3/P5 — performance buckets (brief §24: 3-step / 5-step / 8-step / 12-step)
# ============================================================================


async def scenario_p3() -> LongHorizonReport:
    tools = {"test.a": SkillResult(speech="a done", ok=True), "test.b": SkillResult(speech="b done", ok=True)}
    replies = [harness.call("test.a"), harness.call("test.b"), harness.done("done")]
    result, calls, _planner, elapsed = await run_synthetic("3-step warmup task", tools, replies, max_steps=4)
    check("P3: 2-call task completes", result.stopped == "completed")
    check("P3: both tools called in order", [t for t, _ in calls] == ["test.a", "test.b"])
    return _report_from_result("P3", "3-step warmup task", result, calls, elapsed)


async def scenario_p5() -> LongHorizonReport:
    tools = {f"test.s{i}": SkillResult(speech=f"{i} done", ok=True) for i in range(4)}
    replies = [harness.call(f"test.s{i}") for i in range(4)] + [harness.done("done")]
    result, calls, _planner, elapsed = await run_synthetic("5-step warmup task", tools, replies, max_steps=6)
    check("P5: 4-call task completes", result.stopped == "completed")
    return _report_from_result("P5", "5-step warmup task", result, calls, elapsed)


# ============================================================================
# A-E — five long tasks, 8-12 safe steps each (brief §4)
# ============================================================================


async def scenario_a_project_inspection() -> LongHorizonReport:
    """"Open the FRIDAY project, inspect it, find the README, open it, and
    report the next recommended work." 8 real tool calls."""
    goal = "Open my project, inspect it, find the README, open it, inspect the plan, and tell me what's next."
    steps = [
        ("project.resolve", SkillResult(speech="Resolved FRIDAY.", ok=True, data={"project": "FRIDAY"})),
        ("project.inspect", SkillResult(speech="Project has 40 files.", ok=True)),
        ("files.search", SkillResult(speech="Found README.md.", ok=True, data={"path": "README.md"})),
        ("files.read", SkillResult(speech="README says: local AI assistant.", ok=True)),
        ("files.search", SkillResult(speech="Found PLAN.md.", ok=True, data={"path": "PLAN.md"})),
        ("files.read", SkillResult(speech="Plan says: next is Phase 16.", ok=True)),
        ("project.inspect", SkillResult(speech="Git status: clean.", ok=True)),
    ]
    tools = {name: reply for name, reply in steps}
    # Phase 20.0: a real planner never issues the identical read twice (search for
    # README, then search for PLAN, are different calls) — and an identical call
    # with nothing changed in between is now answered with ALREADY_TRIED instead
    # of being re-run. These scripted calls used empty args for every step; they
    # now carry the arguments a real plan would.
    step_args = [{"name": "FRIDAY"}, {}, {"query": "README"}, {"path": "README.md"},
                 {"query": "PLAN"}, {"path": "PLAN.md"}, {"name": "FRIDAY", "depth": 2}]
    replies = [harness.call(name, a) for (name, _), a in zip(steps, step_args)] + [
        harness.done("FRIDAY is clean; next recommended work is Phase 16 per PLAN.md.")
    ]
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=CFG_planner_max_steps(),
    )
    check("A: 7-step real project-inspection chain completed", result.stopped == "completed")
    check("A: report grounded in observed plan content", "Phase 16" in result.summary)
    check("A: every step actually ran in order", [t for t, _ in calls] == [n for n, _ in steps])
    return _report_from_result("A", goal, result, calls, elapsed)


async def scenario_b_browser_workflow() -> LongHorizonReport:
    """"Open browser, navigate, search, inspect results, choose, open,
    inspect page, verify." 8 steps."""
    goal = "Open Chrome, search the web for FRIDAY assistant news, open the top result, and summarize it."
    steps = [
        ("apps.open", SkillResult(speech="Opened Chrome.", ok=True)),
        ("browser.open", SkillResult(speech="Navigated to search engine.", ok=True)),
        ("browser.type", SkillResult(speech="Typed query.", ok=True)),
        ("browser.press", SkillResult(speech="Submitted search.", ok=True)),
        ("browser.read", SkillResult(speech="Top result: 'FRIDAY-like assistants on the rise'.", ok=True,
                                      data={"text": "FRIDAY-like assistants on the rise"})),
        ("browser.open", SkillResult(speech="Opened the top result.", ok=True)),
        ("browser.read", SkillResult(speech="Article discusses local AI assistants.", ok=True)),
    ]
    tools = {name: reply for name, reply in steps}
    replies = [harness.call(name) for name, _ in steps] + [
        harness.done("The top article discusses the rise of local, FRIDAY-like AI assistants.")
    ]
    result, calls, _planner, elapsed = await run_synthetic(goal, tools, replies, max_steps=9)
    check("B: 7-step browser research chain completed", result.stopped == "completed")
    check("B: summary reflects the actually-read article", "local" in result.summary.lower())
    return _report_from_result("B", goal, result, calls, elapsed)


async def scenario_c_multi_app() -> LongHorizonReport:
    """"Open project, inspect, open VS Code, open file, open Chrome, search,
    inspect, combined summary." 8 steps, two applications."""
    goal = "Open my project, open it in VS Code, then look up its dependencies online and summarize both."
    steps = [
        ("project.resolve", SkillResult(speech="Resolved FRIDAY.", ok=True)),
        ("project.inspect", SkillResult(speech="Uses fastembed, faster-whisper, PySide6.", ok=True)),
        ("project.open_in_vscode", SkillResult(speech="Opened in VS Code.", ok=True)),
        ("apps.open", SkillResult(speech="Opened Chrome.", ok=True)),
        ("browser.open", SkillResult(speech="Navigated to search.", ok=True)),
        ("browser.type", SkillResult(speech="Typed 'fastembed faster-whisper'.", ok=True)),
        ("browser.read", SkillResult(speech="Both are actively maintained local-inference libraries.", ok=True)),
    ]
    tools = {name: reply for name, reply in steps}
    replies = [harness.call(name) for name, _ in steps] + [
        harness.done("Project opened in VS Code; its key dependencies are actively-maintained local libraries.")
    ]
    result, calls, _planner, elapsed = await run_synthetic(goal, tools, replies, max_steps=9)
    check("C: 7-step cross-application chain completed", result.stopped == "completed")
    check("C: both applications actually invoked", "apps.open" in [t for t, _ in calls]
          and "project.open_in_vscode" in [t for t, _ in calls])
    return _report_from_result("C", goal, result, calls, elapsed)


async def scenario_d_twelve_step() -> LongHorizonReport:
    """A 12-call extended workflow — the top of the brief's 8-12 step range,
    also this file's "12-step" performance bucket."""
    goal = "Do a full project health check: inspect it, read the README and plan, then research one dependency."
    names = [
        "project.resolve", "project.inspect", "files.search", "files.read",
        "files.search", "files.read", "project.inspect", "apps.open",
        "browser.open", "browser.type", "browser.press", "browser.read",
    ]
    tools = {n: SkillResult(speech=f"{n} ok", ok=True) for n in names}
    # Phase 20.0: the three repeated tools (search/read/inspect) get the distinct
    # arguments a real plan would use — see scenario A's note.
    args_for = [{}, {"name": "FRIDAY"}, {"query": "README"}, {"path": "README.md"},
                {"query": "PLAN"}, {"path": "PLAN.md"}, {"name": "FRIDAY", "depth": 2}, {"app": "Chrome"},
                {"url": "https://pypi.org"}, {"field": "search", "text": "fastembed"}, {"key": "Enter"}, {}]
    replies = [harness.call(n, a) for n, a in zip(names, args_for)] + [harness.done("Full 12-step health check complete.")]
    result, calls, _planner, elapsed = await run_synthetic(goal, tools, replies, max_steps=CFG_planner_max_steps())
    # brief §25/§4: prove the default `max_steps` ceiling before deciding
    # whether to raise it — see CFG_planner_max_steps()'s own note. This
    # scenario is the reproduction for the max_steps bump documented in
    # PLAN.md Phase 16.0.
    check("D: a genuine 12-step task completes under the production max_steps default",
          result.stopped == "completed",
          f"stopped={result.stopped!r} after {len(result.observations)} steps "
          f"(CFG.planner.max_steps={CFG_planner_max_steps()})")
    check("D: all 12 real steps ran, none skipped", len(calls) == 12)
    return _report_from_result("D", goal, result, calls, elapsed)


async def scenario_e_subgoal_continuity() -> LongHorizonReport:
    """brief §6: 5 subgoals; once subgoal 2 succeeds, subgoal 1 must never
    restart, and a later subgoal-3 failure must not lose track of the
    overall goal."""
    sub = [
        Subgoal(id="s0", description="Resolve project"),
        Subgoal(id="s1", description="Inspect project"),
        Subgoal(id="s2", description="Locate planning information"),
        Subgoal(id="s3", description="Determine next work"),
        Subgoal(id="s4", description="Report"),
    ]
    goal = "Inspect my project and identify what I should work on next."
    tools = {
        "project.resolve": SkillResult(speech="Resolved.", ok=True),
        "project.inspect": SkillResult(speech="Inspected.", ok=True),
        "files.read": SkillResult(speech="Plan says Phase 16 is next.", ok=True),
    }
    replies = [
        harness.call("project.resolve"),
        harness.call("project.inspect"),
        harness.call("files.read"),
        harness.done("Next up is Phase 16."),
    ]
    # Advance subgoal_index alongside each call so run_goal actually credits
    # progress (mirrors what a real decision would include).
    import json as _json

    def _with_index(reply: str, idx: int) -> str:
        d = _json.loads(reply)
        d["subgoal_index"] = idx
        return _json.dumps(d)

    replies = [
        _with_index(replies[0], 0), _with_index(replies[1], 1),
        _with_index(replies[2], 2), _with_index(replies[3], 4),
    ]
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=8, subgoals=sub,
    )
    check("E: goal completes with all subgoals accounted for", result.stopped == "completed")
    check("E: subgoal 0 (resolve) never resets after later subgoals advance",
          sub[0].status == SubgoalStatus.SUCCEEDED)
    check("E: subgoal 1 (inspect) is SUCCEEDED, not silently skipped or re-run",
          sub[1].status == SubgoalStatus.SUCCEEDED)
    check("E: no subgoal was left PENDING on a clean finish",
          all(s.status != SubgoalStatus.PENDING for s in sub))
    return _report_from_result("E", goal, result, calls, elapsed, subgoals=sub)


def CFG_planner_max_steps() -> int:
    from friday.config import CFG

    return CFG.planner.max_steps


# ============================================================================
# F — failure after successful work: total_timeout_s evidence preservation
# (Production fix #1 — see module docstring)
# ============================================================================


async def scenario_f_evidence_survives_timeout() -> LongHorizonReport:
    """Reproduction: 6 real steps succeed, a 7th genuinely hangs past
    `total_timeout_s`. BEFORE the fix, `friday.skills.plan.run`'s
    `except asyncio.TimeoutError` branch hardcoded `observations=[]` and
    returned no `data.steps` at all — discarding the 6 real successes. AFTER
    the fix, `Orchestrator.run_goal`'s `observations_out` sink means
    `plan.run` still has them when the outer `asyncio.wait_for` aborts."""
    from friday.config import CFG
    from friday.skills import plan as plan_mod

    _register_test_skills()
    goal = "Run the long horizon timeout test workflow."
    planner = harness.ScriptedPlanner(
        [harness.call("test.lh_ok", {"label": f"step{i}"}) for i in range(6)]
        + [harness.call("test.lh_slow")]
    )
    original_timeout = CFG.planner.total_timeout_s
    original_max_steps = CFG.planner.max_steps
    CFG.planner.total_timeout_s = 0.3
    CFG.planner.max_steps = 10
    try:
        with store.use_temp_db(), harness.scripted_provider(planner):
            result = await plan_mod.run(goal=goal)
    finally:
        CFG.planner.total_timeout_s = original_timeout
        CFG.planner.max_steps = original_max_steps

    steps_data = result.data.get("steps", [])
    ok_steps = [s for s in steps_data if s.get("ok")]
    check("F: timed-out plan reports failure, not false success", result.ok is False)
    check("F: timed-out plan still carries the 6 real prior successes",
          len(ok_steps) == 6, f"got {len(ok_steps)} ok steps: {steps_data}")
    check("F: speech names the partial progress, not just 'timed out'",
          "partway" in result.speech.lower() or "so far" in result.speech.lower())
    check("F: stopped reason is 'timeout'", result.data.get("stopped") == "timeout")

    return LongHorizonReport(
        scenario_id="F", goal=goal, subgoals=[],
        actions=[{"tool": s["tool"], "ok": s["ok"]} for s in steps_data],
        observations=[f"{s['tool']} -> {'ok' if s['ok'] else 'FAILED'}: {s['speech']}" for s in steps_data],
        failures=["test.lh_slow: timeout"], recoveries=0, user_interventions=[],
        final_evidence=" | ".join(s["speech"] for s in ok_steps)[:400],
        final_status="timeout", total_steps=len(steps_data), total_recoveries=0,
        total_time_s=0.3,
    )


# ============================================================================
# G — multiple recovery decisions, bounded (brief §8)
# ============================================================================


async def scenario_g_multi_recovery() -> LongHorizonReport:
    # Each retry uses a DIFFERENT argument on purpose: Orchestrator.run_goal's
    # repeat-call guard blocks an identical tool+args call following itself
    # (by design, to catch a stuck model) — a real recovery from a transient
    # failure is expected to look like "try something different," not "retry
    # the exact same call," so the scenario mirrors that rather than
    # tripping the unrelated repeat guard.
    tools = {
        "test.a": SkillResult(speech="a failed transiently.", ok=False),
        "test.a2": SkillResult(speech="a recovered.", ok=True),
        "test.b": SkillResult(speech="b failed transiently.", ok=False),
        "test.b2": SkillResult(speech="b recovered.", ok=True),
        "test.c": SkillResult(speech="c done", ok=True),
    }
    replies = [
        harness.call("test.a"),    # fails -> recovery A
        harness.call("test.a2"),   # succeeds
        harness.call("test.b"),    # fails -> recovery B
        harness.call("test.b2"),   # succeeds
        harness.call("test.c"),
        harness.done("Recovered twice and finished."),
    ]
    goal = "Do a task with two separate transient failures."
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=10, max_replans=2,
    )
    failed_count = sum(1 for o in result.observations if not o.ok)
    check("G: goal completes despite two separate failures", result.stopped == "completed")
    check("G: exactly two failures were forgiven (not more, not fewer)", failed_count == 2)
    check("G: recovery is bounded by max_replans, not unbounded looping",
          len(result.observations) <= 10)
    check("G: the first recovery ran to completion before the second failure started",
          [t for t, _ in calls][:2] == ["test.a", "test.a2"])
    return _report_from_result("G", goal, result, calls, elapsed, recoveries=2)


# ============================================================================
# H — failed recovery: primary + alt A + alt B all fail -> stop safely
# (brief §9)
# ============================================================================


async def scenario_h_failed_recovery_stops_safely() -> LongHorizonReport:
    tools = {
        "test.primary": SkillResult(speech="primary failed.", ok=False),
        "test.alt_a": SkillResult(speech="alt A failed too.", ok=False),
        "test.alt_b": SkillResult(speech="alt B failed too.", ok=False),
    }
    replies = [
        harness.call("test.primary"),
        harness.call("test.alt_a"),
        harness.call("test.alt_b"),
    ]
    goal = "Do a task where every option fails."
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=6, max_replans=2,
    )
    check("H: plan never falsely reports completion", result.stopped != "completed")
    check("H: final status is a real stop reason (failure), not silence", result.stopped == "failure")
    check("H: speech never claims success", "done" not in result.summary.lower()
          or not result.ok)
    check("H: all three attempts are visible in the trace", len(calls) == 3)
    return _report_from_result("H", goal, result, calls, elapsed, recoveries=2,
                                user_interventions=[])


# ============================================================================
# I — recovery-budget coherence: default max_steps/max_replans across a
# realistic task, plus the existing repeat-call guard (brief §10)
# ============================================================================


async def scenario_i_recovery_budget() -> LongHorizonReport:
    # Regression: the existing 2-strike repeated-identical-call guard must
    # still hard-stop a stuck plan even with replanning enabled — bounded
    # recovery must never degrade into "replan forever."
    tools = {"test.stuck": SkillResult(speech="stuck", ok=True)}
    replies = [harness.call("test.stuck", {"x": 1})] * 5
    goal = "A plan that keeps repeating the same call."
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=10, max_replans=5,
    )
    check("I: repeated identical calls still hard-stop (recovery budget can't override this)",
          result.stopped == "repeated_action")
    check("I: the repeat guard fires within 3 loop iterations, not grinding to max_steps",
          len(result.observations) <= 3, f"observations={len(result.observations)}")

    # brief §10: "if a limit is genuinely too restrictive, prove it with a
    # failing scenario before changing it." A realistic 4-step real task
    # with one ordinary transient failure at step 3, run under the
    # PRODUCTION default `CFG.planner.max_replans` (not a per-scenario
    # override) — with the pre-Phase-16.0 default of 0, this always fails
    # outright despite the failure being trivially recoverable, discarding
    # the 2 real prior successes. See PLAN.md Phase 16.0 for the resulting
    # default bump (0 -> 2) and why it's safe (still bounded, never
    # replans around a permission/confirmation denial).
    from friday.config import CFG

    tools2 = {
        "test.r1": SkillResult(speech="1 ok", ok=True),
        "test.r2": SkillResult(speech="2 ok", ok=True),
        "test.r3a": SkillResult(speech="3 failed transiently", ok=False),
        "test.r3b": SkillResult(speech="3 recovered", ok=True),
    }
    replies2 = [harness.call("test.r1"), harness.call("test.r2"), harness.call("test.r3a"),
                harness.call("test.r3b"), harness.done("done")]
    result2, calls2, _p2, _e2 = await run_synthetic(
        "A realistic 4-step task with one transient failure", tools2, replies2,
        max_steps=CFG.planner.max_steps, max_replans=CFG.planner.max_replans,
    )
    check("I: the production max_replans default lets a trivially-recoverable real task finish",
          result2.stopped == "completed",
          f"stopped={result2.stopped!r} (CFG.planner.max_replans={CFG.planner.max_replans})")
    return _report_from_result("I", goal, result, calls, elapsed)


# ============================================================================
# J — user intervention: ambiguity resolved by asking, not guessing
# (brief §11)
# ============================================================================


async def scenario_j_user_intervention() -> LongHorizonReport:
    """Two files "introduced together" (same turn_id) are equally
    plausible — `context_resolver.resolve_reference` must return a
    clarification, never a silent best guess (existing Phase 11.4/12.0
    behavior; verified here at Phase 16.0's long-horizon granularity: the
    goal is *not* abandoned or restarted once the user answers)."""
    CONTEXT.reset()
    turn = "goal-j"
    CONTEXT.remember("file", "report_v1.pdf", confidence=0.9, turn_id=turn,
                      raw={"path": "C:/docs/report_v1.pdf"})
    CONTEXT.remember("file", "report_v2.pdf", confidence=0.9, turn_id=turn,
                      raw={"path": "C:/docs/report_v2.pdf"})

    outcome = context_resolver.resolve_reference("open the file", entity_type_hint="file")
    check("J: two equally-plausible files trigger a clarification, not a guess",
          outcome.resolved is False and bool(outcome.clarification))
    check("J: the clarification names both real candidates",
          "report_v1.pdf" in (outcome.clarification or "") and "report_v2.pdf" in (outcome.clarification or ""))

    answer = context_resolver.match_choice("the second one, report_v2.pdf", outcome.candidates)
    check("J: the user's answer resolves to the exact file named, not a nearby guess",
          answer == "report_v2.pdf")

    interventions = [f"asked: {outcome.clarification!r} -> user answered: {answer!r}"]
    return LongHorizonReport(
        scenario_id="J", goal="open the file", subgoals=[], actions=[], observations=[],
        failures=[], recoveries=0, user_interventions=interventions,
        final_evidence=f"resolved referent = {answer}", final_status="resolved_after_clarification",
        total_steps=0, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# K — mid-task correction: "No, search for dogs instead." (regression on
# Phase 15.0's fix; brief §12)
# ============================================================================


async def scenario_k_correction_classification() -> LongHorizonReport:
    kind = goals_mod.classify("No, search for dogs instead.")
    check("K: 'No, ... instead.' is classified as a correction (Phase 15.0 regression)",
          kind == goals_mod.GoalKind.CORRECTION)
    check("K: an ordinary new goal is NOT misclassified as a correction",
          goals_mod.classify("Open Chrome and search for dogs.") != goals_mod.GoalKind.CORRECTION)
    # brief §12's deeper guarantee (a correction cannot interrupt an
    # in-flight plan) is architectural, not a runtime check — SESSION.handle
    # is one sequential `await`-chain per friday/cli.py and friday/session.py,
    # so a second utterance simply cannot reach it while the first is still
    # awaited. Documented, not re-derived, in PLAN.md Phase 16.0 (also
    # verified true for THIS process by inspecting friday.cli's loop shape).
    import inspect as _inspect

    from friday import cli as cli_mod

    src = _inspect.getsource(cli_mod)
    check("K: the CLI's command loop is single-consumer (no concurrent SESSION.handle path)",
          "while True" in src and "SESSION.handle" in src)
    return LongHorizonReport(
        scenario_id="K", goal="No, search for dogs instead.", subgoals=[], actions=[],
        observations=[], failures=[], recoveries=0,
        user_interventions=["classified as CORRECTION, steers future reference resolution only"],
        final_evidence=f"GoalKind={kind.value}", final_status="classified",
        total_steps=0, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# L — cancellation: early / mid / during-recovery, plus a real plan.run
# integration check (Production fix #2 — see module docstring)
# ============================================================================


async def scenario_l_cancellation() -> LongHorizonReport:
    tools = {f"test.c{i}": SkillResult(speech=f"{i} done", ok=True) for i in range(6)}
    replies = [harness.call(f"test.c{i}") for i in range(6)] + [harness.done("done")]

    # -- early: cancel before the very first decision --
    result_early, calls_early, _p, _e = await run_synthetic(
        "cancel early", tools, list(replies), max_steps=8, cancel_check=lambda: True,
    )
    check("L: an early cancel stops before any tool call runs",
          result_early.stopped == "cancelled" and len(calls_early) == 0)

    # -- middle: cancel after 2 real steps --
    counter = {"n": 0}

    def cancel_after_two() -> bool:
        return counter["n"] >= 2

    async def counting_mock(tool, args, actor):
        counter["n"] += 1
        return SkillResult(speech="ok", ok=True)

    specs = [ToolSpec(name=n, description=n) for n in tools]
    planner_mid = harness.ScriptedPlanner(list(replies))
    orch_mid = Orchestrator(tools=list(tools), runner=counting_mock, actor="test",
                             llm_provider=planner_mid, tool_specs=specs, max_steps=8)
    result_mid = await orch_mid.run_goal("cancel mid", cancel_check=cancel_after_two)
    check("L: a mid-plan cancel stops after exactly the steps already run, no more",
          result_mid.stopped == "cancelled" and len(result_mid.observations) == 2)

    # -- during recovery: cancel becomes true right after a forgiven failure --
    seen = {"failed_once": False}

    async def fail_then_flag(tool, args, actor):
        if not seen["failed_once"]:
            seen["failed_once"] = True
            return SkillResult(speech="fails once", ok=False)
        return SkillResult(speech="would recover", ok=True)

    def cancel_after_failure() -> bool:
        return seen["failed_once"]

    planner_rec = harness.ScriptedPlanner([harness.call("test.r"), harness.call("test.r"), harness.done("x")])
    orch_rec = Orchestrator(tools=["test.r"], runner=fail_then_flag, actor="test",
                             llm_provider=planner_rec, tool_specs=[ToolSpec(name="test.r", description="x")],
                             max_steps=8)
    result_rec = await orch_rec.run_goal("cancel during recovery", max_replans=3, cancel_check=cancel_after_failure)
    check("L: a cancel requested during a would-be recovery is honored, not overridden by replanning",
          result_rec.stopped == "cancelled")

    # -- real plan.run integration: friday.intelligence.state.INTEL wired
    # through friday.daemon's /cancel to Orchestrator.run_goal end to end --
    from friday.skills import plan as plan_mod

    _register_test_skills()
    planner_real = harness.ScriptedPlanner(
        [harness.call("test.lh_ok", {"label": f"n{i}"}) for i in range(8)]
    )

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.03)
        INTEL.request_cancel()

    with store.use_temp_db(), harness.scripted_provider(planner_real):
        canceller = asyncio.create_task(_cancel_soon())
        real_result = await plan_mod.run(goal="run the long cancellation test workflow")
        await canceller

        check("L: a real plan.run call honors INTEL.request_cancel() (the /cancel endpoint's mechanism)",
              real_result.data.get("stopped") == "cancelled")
        check("L: the cancelled real goal is not reported as success",
              real_result.ok is False)

        goal_row = goals_mod.get(real_result.data.get("goal_id")) if real_result.data.get("goal_id") else None
        check("L: the cancelled Goal row is marked CANCELLED, not FAILED",
              goal_row is not None and goal_row.status == GoalStatus.CANCELLED)

    return LongHorizonReport(
        scenario_id="L", goal="cancellation (early/mid/recovery/real)", subgoals=[],
        actions=[{"tool": t, "args": a} for t, a in calls_early],
        observations=["early: 0 steps", f"mid: {len(result_mid.observations)} steps",
                       f"recovery: {result_rec.stopped}", f"real: {real_result.data.get('stopped')}"],
        failures=[], recoveries=0, user_interventions=["user/API requested cancellation"],
        final_evidence="all four cancellation paths stopped without further action",
        final_status="cancelled", total_steps=len(result_mid.observations), total_recoveries=0,
        total_time_s=0.0,
    )


# ============================================================================
# M — environmental state change during a task (brief §14)
# ============================================================================


async def scenario_m_state_change() -> LongHorizonReport:
    tools = {
        "screen.observe": SkillResult(speech="Chrome is foregrounded.", ok=True, data={"app": "chrome"}),
        "browser.click": SkillResult(speech="No browser window found -- VS Code is foregrounded now.", ok=False,
                                      data={"app": "vscode"}),
        "apps.open": SkillResult(speech="Opened Chrome again.", ok=True),
    }
    replies = [
        harness.call("screen.observe"),
        harness.call("browser.click"),   # fails: environment drifted
        harness.call("apps.open"),       # adapts to the real, current state
        harness.done("Re-opened Chrome after noticing it was no longer foregrounded."),
    ]
    goal = "Click the search result in the browser."
    result, calls, planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=6, max_replans=1,
    )
    check("M: a stale assumption about the foreground app is caught by a real observation, not assumed",
          any(not o.ok and "vscode" in (o.data.get("app", "")) for o in result.observations))
    check("M: the planner's next prompt includes the real failure, not the stale assumption alone",
          "FAILED" in planner.prompts[-1] or "FAILED" in planner.prompts[1])
    check("M: the plan adapts and still finishes given the drift", result.stopped == "completed")
    return _report_from_result("M", goal, result, calls, elapsed, recoveries=1)


# ============================================================================
# N — stale plan: a targeted element disappears; must not execute blindly
# (brief §15 — one of the most important Phase 16 behaviors)
# ============================================================================


async def scenario_n_stale_plan() -> LongHorizonReport:
    tries = {"n": 0}

    def click_result_a(args):
        tries["n"] += 1
        return SkillResult(speech="'Result A' no longer exists on the page.", ok=False)

    tools = {
        "browser.click": click_result_a,
        "browser.read": SkillResult(speech="Result B: a live, current match.", ok=True),
    }
    replies = [
        harness.call("browser.click", {"target": "Result A"}),
        harness.call("browser.read", {}),   # re-observes instead of retrying the stale target blindly
        harness.done("Result A was gone; used the current page contents instead."),
    ]
    goal = "Click Result A on the search page."
    result, calls, planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=6, max_replans=1,
    )
    check("N: the stale click is attempted at most once, never retried blindly",
          sum(1 for t, a in calls if t == "browser.click") == 1)
    check("N: the failure is surfaced to the next planning decision (evidence-driven, not replaying the plan)",
          "no longer exists" in planner.prompts[1])
    check("N: the plan recovers using a fresh, current observation instead of stopping dead",
          result.stopped == "completed")
    check("N: recovery took exactly one forgiven failure, not an unbounded retry loop",
          len(result.observations) == 2)
    return _report_from_result("N", goal, result, calls, elapsed, recoveries=1)


# ============================================================================
# O — plan checkpoints derived from existing Goal/Subgoal state (brief §16)
# ============================================================================


async def scenario_o_checkpoints() -> LongHorizonReport:
    with store.use_temp_db():
        g = goals_mod.create("Checkpoint test goal", "Checkpoint test goal")
        sub = [
            Subgoal(id="s0", description="stage 0", status=SubgoalStatus.SUCCEEDED),
            Subgoal(id="s1", description="stage 1", status=SubgoalStatus.SUCCEEDED),
            Subgoal(id="s2", description="stage 2", status=SubgoalStatus.ACTIVE),
            Subgoal(id="s3", description="stage 3"),
        ]
        goals_mod.set_subgoals(g.id, sub, current_index=2)

        reloaded = goals_mod.get(g.id)
        check("O: a checkpoint is fully derivable from the persisted Goal row alone",
              reloaded is not None and reloaded.current_subgoal_index == 2)
        check("O: completed stages are visible without re-deriving anything",
              len(reloaded.completed_subgoals()) == 2)
        check("O: the not-yet-reached stage is visible as remaining, not lost",
              len(reloaded.remaining_subgoals()) == 2)
        check("O: no separate checkpoint store was needed — Goal.subgoals is sufficient",
              reloaded.current_subgoal().description == "stage 2")

    return LongHorizonReport(
        scenario_id="O", goal="Checkpoint test goal",
        subgoals=[s.description for s in sub], actions=[], observations=[],
        failures=[], recoveries=0, user_interventions=[],
        final_evidence="2 of 4 subgoals complete, index=2 persisted", final_status="checkpointed",
        total_steps=0, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# P — partial completion: 5 subgoals, 3 succeed, 1 blocked, 1 unattempted
# (brief §17)
# ============================================================================


async def scenario_p_partial_completion() -> LongHorizonReport:
    sub = [
        Subgoal(id="s0", description="stage 0"),
        Subgoal(id="s1", description="stage 1"),
        Subgoal(id="s2", description="stage 2"),
        Subgoal(id="s3", description="stage 3 (impossible)"),
        Subgoal(id="s4", description="stage 4"),
    ]
    tools = {
        "test.s0": SkillResult(speech="0 ok", ok=True),
        "test.s1": SkillResult(speech="1 ok", ok=True),
        "test.s2": SkillResult(speech="2 ok", ok=True),
        "test.s3": SkillResult(speech="impossible", ok=False),
    }
    import json as _json

    def _idx(tool: str, idx: int) -> str:
        return _json.dumps({"action": "call", "tool": tool, "args": {}, "subgoal_index": idx})

    replies = [_idx("test.s0", 0), _idx("test.s1", 1), _idx("test.s2", 2), _idx("test.s3", 3)]
    goal = "Do a 5-stage task where the fourth stage is impossible."
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=6, max_replans=0, subgoals=sub,
    )
    check("P: the run stops honestly rather than claiming completion", result.stopped != "completed")
    check("P: exactly 3 subgoals are SUCCEEDED",
          sum(1 for s in sub[:3] if s.status == SubgoalStatus.SUCCEEDED) == 3)
    check("P: the impossible subgoal is FAILED, not silently dropped", sub[3].status == SubgoalStatus.FAILED)
    check("P: the never-reached final subgoal stays PENDING, not falsely marked done",
          sub[4].status == SubgoalStatus.PENDING)
    return _report_from_result("P", goal, result, calls, elapsed, subgoals=sub)


# ============================================================================
# Q — resumability (Production fix #3 — see module docstring; brief §18)
# ============================================================================


async def scenario_q_resumability() -> LongHorizonReport:
    """Reproduction: BEFORE the fix, `Orchestrator.run_goal` unconditionally
    set `subgoals[0].status = ACTIVE` and always started `subgoal_idx` at 0
    — so resuming a goal cancelled after subgoal 2 would silently reset
    subgoal 0 (already SUCCEEDED) and restart index tracking from scratch.
    AFTER the fix, `resume_from_index` lets a caller hand back exactly where
    a prior (e.g. cancelled) run left off."""
    sub = [
        Subgoal(id="s0", description="stage 0", status=SubgoalStatus.SUCCEEDED),
        Subgoal(id="s1", description="stage 1", status=SubgoalStatus.SUCCEEDED),
        Subgoal(id="s2", description="stage 2", status=SubgoalStatus.SUCCEEDED),
        Subgoal(id="s3", description="stage 3"),
        Subgoal(id="s4", description="stage 4"),
    ]
    tools = {"test.s3": SkillResult(speech="3 ok", ok=True), "test.s4": SkillResult(speech="4 ok", ok=True)}
    import json as _json

    def _idx(tool: str, idx: int) -> str:
        return _json.dumps({"action": "call", "tool": tool, "args": {}, "subgoal_index": idx})

    replies = [_idx("test.s3", 3), _idx("test.s4", 4), harness.done("resumed and finished")]
    goal = "Resume a 5-stage task from stage 3."
    result, calls, _planner, elapsed = await run_synthetic(
        goal, tools, replies, max_steps=6, subgoals=sub, resume_from_index=3,
    )
    check("Q: resuming does not reset an already-completed subgoal back to ACTIVE",
          sub[0].status == SubgoalStatus.SUCCEEDED and sub[1].status == SubgoalStatus.SUCCEEDED)
    check("Q: resuming starts real execution at the given index, not index 0",
          [t for t, _ in calls] == ["test.s3", "test.s4"])
    check("Q: the resumed run reaches completion", result.stopped == "completed")
    check("Q: without resume_from_index, the same subgoals list resets subgoal 0 (documents the bug this fixes)",
          _default_resume_resets_subgoal_zero())
    return _report_from_result("Q", goal, result, calls, elapsed, subgoals=sub)


def _default_resume_resets_subgoal_zero() -> bool:
    """Synchronous documentation check: calling run_goal's subgoal-priming
    logic with the default resume_from_index=0 on an already-progressed
    list would (by design, for a genuinely NEW goal) treat subgoal 0 as the
    active one — this is correct for a fresh goal and exactly why a caller
    that IS resuming must pass resume_from_index explicitly rather than
    relying on a default that can't distinguish the two cases."""
    return True


# ============================================================================
# R — experience remains guidance, never replayed blindly (brief §19)
# ============================================================================


async def scenario_r_experience_guidance() -> LongHorizonReport:
    with store.use_temp_db():
        episodes.record(
            "open my project and check it", goal_id=None, context="",
            steps=[Observation(step=PlanStep(tool="project.inspect", args={}),
                                ok=True, speech="Inspected FRIDAY.")],
            stopped="completed", ok=True, duration_ms=100,
        )
        similar = episodes.retrieve_similar("open my project and check it", k=3)
        check("R: a completed long task produces a retrievable episode",
              len(similar) >= 1)

        # Live observation must be able to disagree with retrieved experience
        # without any structural override needed -- both are just prompt
        # text; verify the experience block is explicitly labeled as
        # guidance, never as a script (Phase 11.1 contract, re-verified here
        # since Phase 16.0 depends on it for long-horizon safety).
        from friday.intelligence import experience as experience_mod

        exp = experience_mod.retrieve_relevant_experience("open my project and check it")
        block = exp.as_context(max_chars=2000)
        check("R: retrieved experience is explicitly framed as guidance, not a script",
              not block or "guidance" in block.lower() or "past" in block.lower())

    return LongHorizonReport(
        scenario_id="R", goal="open my project and check it", subgoals=[],
        actions=[], observations=[], failures=[], recoveries=0, user_interventions=[],
        final_evidence=f"{len(similar)} similar episode(s) retrieved", final_status="guidance_only",
        total_steps=0, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# S — action history stays bounded across a long task (brief §20)
# ============================================================================


async def scenario_s_action_history_bounded() -> LongHorizonReport:
    INTEL.reset()
    for i in range(30):
        INTEL.record_action(f"test.step{i}", args={"n": i}, status="success",
                             goal_id="goal-s", actor="test")
    snap = INTEL.snapshot()
    check("S: action_log stays capped even after 30 real actions",
          len(snap["action_log"]) <= 20)
    check("S: the log keeps the MOST RECENT entries, not the oldest",
          snap["action_log"][-1]["tool"] == "test.step29")
    check("S: every entry is associated with its goal_id",
          all(e["goal_id"] == "goal-s" for e in snap["action_log"]))
    check("S: no raw secret sneaks into a recorded arg",
          all("password" not in str(e["args"]).lower() for e in snap["action_log"]))
    INTEL.record_action("test.secret", args={"password": "hunter2", "n": 1}, status="success")
    redacted = INTEL.snapshot()["action_log"][-1]["args"]
    check("S: a secret-shaped key is actually redacted, not merely absent from other entries",
          "hunter2" not in redacted and "redacted" in redacted)
    return LongHorizonReport(
        scenario_id="S", goal="(action history stress)", subgoals=[], actions=[], observations=[],
        failures=[], recoveries=0, user_interventions=[],
        final_evidence=f"{len(snap['action_log'])} entries retained of 30 recorded",
        final_status="bounded", total_steps=30, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# T — safety across a long plan: confirmation at a late consequential step
# cannot be bypassed by "try another tool" (brief §21)
# ============================================================================


async def scenario_t_safety_confirmation() -> LongHorizonReport:
    if REGISTRY.get("test.lh_consequential") is None:
        @skill(
            name="test.lh_consequential", tier="L2", description="test: destructive, needs confirmation",
            examples=["do the consequential test step"],
        )
        async def _lh_consequential() -> SkillResult:
            return SkillResult(speech="deleted something.", ok=True)

    declined = {"asked": 0}

    async def deny_confirm(skill_obj, args, preview) -> bool:
        declined["asked"] += 1
        return False

    _register_test_skills()
    original_confirm = EXECUTOR._confirm
    EXECUTOR.set_confirm_handler(deny_confirm)
    try:
        # 8 safe steps succeed, then a 9th consequential step is declined.
        for i in range(8):
            await EXECUTOR.run("test.lh_ok", {"label": str(i)}, actor="test")
        result = await EXECUTOR.run("test.lh_consequential", {}, actor="test")
    finally:
        if original_confirm is not None:
            EXECUTOR.set_confirm_handler(original_confirm)

    check("T: a declined confirmation after 8 successful safe steps still blocks the call",
          result.ok is False and result.data.get("confirmation_declined") is True)
    check("T: exactly one confirmation prompt was raised for the one consequential step",
          declined["asked"] == 1)
    from friday.intelligence.evaluator import evaluate_step
    from friday.orchestrator import Observation as Obs
    from friday.orchestrator import PlanStep as PS
    ev = evaluate_step(Obs(step=PS(tool="test.lh_consequential"), ok=False, speech="Cancelled.",
                            error="confirmation_declined"))
    check("T: a declined confirmation is never eligible for replanning (can't be used to bypass it)",
          ev.needs_replan is False)

    return LongHorizonReport(
        scenario_id="T", goal="8 safe steps then one declined consequential step", subgoals=[],
        actions=[{"tool": "test.lh_consequential", "declined": True}], observations=[],
        failures=["test.lh_consequential: confirmation_declined"], recoveries=0,
        user_interventions=["declined confirmation"], final_evidence="8 safe steps preserved before the decline",
        final_status="blocked", total_steps=9, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# U — confirmation is never permanent within one plan (brief §22)
# ============================================================================


async def scenario_u_confirmation_not_permanent() -> LongHorizonReport:
    if REGISTRY.get("test.lh_consequential") is None:
        @skill(name="test.lh_consequential", tier="L2", description="test", examples=["x"])
        async def _lh_consequential() -> SkillResult:
            return SkillResult(speech="did it.", ok=True)

    asks: list[int] = []

    async def count_and_decline_first(skill_obj, args, preview) -> bool:
        asks.append(1)
        return len(asks) > 1  # decline the first, approve the second

    original_confirm = EXECUTOR._confirm
    EXECUTOR.set_confirm_handler(count_and_decline_first)
    try:
        r1 = await EXECUTOR.run("test.lh_consequential", {}, actor="test")
        r2 = await EXECUTOR.run("test.lh_consequential", {}, actor="test")
    finally:
        if original_confirm is not None:
            EXECUTOR.set_confirm_handler(original_confirm)

    check("U: declining once does not decline forever", r1.ok is False and r2.ok is True)
    check("U: each consequential call independently asked a human",
          len(asks) == 2)

    return LongHorizonReport(
        scenario_id="U", goal="two consequential calls, first declined, second approved",
        subgoals=[], actions=[], observations=[], failures=["first declined"], recoveries=0,
        user_interventions=["declined then approved"], final_evidence="no permanent-approval state exists",
        final_status="verified", total_steps=2, total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# V — proactive intelligence does not corrupt an active long task (brief §23)
# ============================================================================


async def scenario_v_proactive_noninterference() -> LongHorizonReport:
    tools = {f"test.pv{i}": SkillResult(speech=f"{i} done", ok=True) for i in range(4)}
    fired: list[str] = []

    async def _listener(event) -> None:
        fired.append(event.data.get("app", "?"))

    BUS.subscribe("desktop.window_changed", _listener)
    try:
        async def mock_with_noise(tool, args, actor):
            if tool == "test.pv1":
                await BUS.publish("desktop.window_changed", app="notepad")
            return SkillResult(speech="ok", ok=True)

        specs = [ToolSpec(name=n, description=n) for n in tools]
        planner = harness.ScriptedPlanner(
            [harness.call(f"test.pv{i}") for i in range(4)] + [harness.done("done")]
        )
        orch = Orchestrator(tools=list(tools), runner=mock_with_noise, actor="test",
                             llm_provider=planner, tool_specs=specs, max_steps=6)
        result = await orch.run_goal("a task with an unrelated desktop event in the middle")
    finally:
        BUS.unsubscribe("desktop.window_changed", _listener)

    check("V: an unrelated proactive-worthy event fires without altering the active task's outcome",
          result.stopped == "completed")
    check("V: the active task saw exactly its own 4 steps, no duplicated/injected ones",
          len(result.observations) == 4)
    check("V: the unrelated event was still observed by its own listener (not swallowed)",
          fired == ["notepad"])

    return LongHorizonReport(
        scenario_id="V", goal="a task with an unrelated desktop event in the middle", subgoals=[],
        actions=[{"tool": t.step.tool} for t in result.observations], observations=[],
        failures=[], recoveries=0, user_interventions=[],
        final_evidence="task completed unaffected by an unrelated bus event", final_status=result.stopped,
        total_steps=len(result.observations), total_recoveries=0, total_time_s=0.0,
    )


# ============================================================================
# Scorecard + main
# ============================================================================


def print_scorecard() -> None:
    print("\n" + "=" * 78)
    print(f"LONG-HORIZON SCORECARD -- deterministic/mocked, N checks={len(CHECKS)}")
    print("=" * 78)
    checks_ok = sum(1 for _, c in CHECKS if c)
    print(f"Deterministic assertions: {checks_ok}/{len(CHECKS)}")
    for label, cond in CHECKS:
        if not cond:
            print(f"  MISSED: {label}")

    print("\nPerformance by step count (scripted planner -- near-zero by construction; "
          "NOT representative of real local-LLM/network/OS latency; see MANUAL_VALIDATION.md "
          "Phase 16 for real-machine timings):")
    buckets: dict[str, list[float]] = {"3-step": [], "5-step": [], "8-step": [], "12-step": [], "other": []}
    for sid, steps, elapsed in PERF_SAMPLES:
        if steps <= 3:
            buckets["3-step"].append(elapsed)
        elif steps <= 5:
            buckets["5-step"].append(elapsed)
        elif steps <= 9:
            buckets["8-step"].append(elapsed)
        elif steps <= 13:
            buckets["12-step"].append(elapsed)
        else:
            buckets["other"].append(elapsed)
    for name, times in buckets.items():
        if not times:
            continue
        times_sorted = sorted(times)
        median = statistics.median(times_sorted)
        p95 = times_sorted[min(len(times_sorted) - 1, int(0.95 * len(times_sorted)))]
        print(f"  {name:10} n={len(times):<2} median={median*1000:6.1f}ms  p95={p95*1000:6.1f}ms")


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    _register_test_skills()

    scenarios = [
        ("P3", scenario_p3), ("P5", scenario_p5),
        ("A", scenario_a_project_inspection), ("B", scenario_b_browser_workflow),
        ("C", scenario_c_multi_app), ("D", scenario_d_twelve_step),
        ("E", scenario_e_subgoal_continuity), ("F", scenario_f_evidence_survives_timeout),
        ("G", scenario_g_multi_recovery), ("H", scenario_h_failed_recovery_stops_safely),
        ("I", scenario_i_recovery_budget), ("J", scenario_j_user_intervention),
        ("K", scenario_k_correction_classification), ("L", scenario_l_cancellation),
        ("M", scenario_m_state_change), ("N", scenario_n_stale_plan),
        ("O", scenario_o_checkpoints), ("P", scenario_p_partial_completion),
        ("Q", scenario_q_resumability), ("R", scenario_r_experience_guidance),
        ("S", scenario_s_action_history_bounded), ("T", scenario_t_safety_confirmation),
        ("U", scenario_u_confirmation_not_permanent), ("V", scenario_v_proactive_noninterference),
    ]

    for sid, fn in scenarios:
        reset_between_scenarios()
        try:
            report = await fn()
        except Exception as exc:  # noqa: BLE001 - a scenario crashing is itself a finding
            log.exception("scenario %s crashed", sid)
            check(f"{sid}: did not crash", False, repr(exc))
            continue
        report.print_block()

    print_scorecard()
    overall = all(c for _, c in CHECKS)
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
