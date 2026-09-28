"""Phase 21.0 — deterministic scorecard for GOAL COVERAGE / COMPLETION SEMANTICS.

The problem (Phase 20.0 report §12.1): FRIDAY can decide a tool call is wrong for the
goal, but it can still stop too early. Nothing between "the model said done" and
"the run is complete" asked whether the USER'S ENTIRE goal was covered — "check the
time and battery level" was finished by `system.time` alone — and the Phase 18
evidence-sufficiency gate judges *relevance*, not *coverage*, so it could not safely
be trusted in the main loop either.

This suite pins the fix (`friday.intelligence.discovery.derive_clauses` /
`assess_coverage`, wired into `Orchestrator.run_goal` behind `coverage_goal=`):

  A  clause derivation: literal, bounded, deterministic, single-clause goals untouched
  B  per-requirement status: satisfied / unsatisfied / failed / unknown
  C  the premature `done`: BEFORE (coverage off) vs AFTER, bounded nudge, honest limits
  D  evidence-driven stop for look-only goals (never on a failure, never on an
     unconfirmed result, never for a goal that authorizes an action)
  E  the Phase 18 discovery gate: coverage guards it, single-clause behaviour unchanged
  F  safety invariants: no widened authority, no unobserved success, real plan.run path
  G  no second subsystem / evaluator semantics pinned

Entirely deterministic: scripted model replies through the existing
`llm.get_provider` seam, a recording runner (no real tool runs) for the orchestrator
sections and the real executor only for L0 read-only skills in section F. No Ollama.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import time

import phase21_common as H
from phase21_common import ROOT, ScriptedPlanner, call, check, done, scenario, scripted_provider

from friday import intent, store  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery, evaluator  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec, _tool_specs  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402

REGISTRY.discover()
RS = discovery.RequirementStatus

GOAL_TB = "Check the time and battery level."

# -- a recording world: no real tool ever runs in sections A-E --------------------------

CALLS: list[tuple[str, dict]] = []
WORLD = {"config.yaml": "port: 8765", "README.md": "FRIDAY readme"}
BEHAVE: dict[str, str] = {}  # tool -> "fail" | "uncertain" for one scenario


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    mode = BEHAVE.get(tool, "")
    if tool == "system.time":
        return SkillResult(speech="It's 3:45 PM.")
    if tool == "system.battery":
        if mode == "fail":
            return SkillResult(speech="I couldn't read the battery.", ok=False)
        if mode == "uncertain":
            return SkillResult(speech="Battery seems to be around 80 percent.", data={"uncertain": True})
        return SkillResult(speech="Battery is at 80 percent, charging.")
    if tool == "network.status":
        return SkillResult(speech="Connected to HomeWiFi.")
    if tool == "files.read":
        path = str(args.get("path", ""))
        if path not in WORLD:
            return SkillResult(speech=f"There's no file at {path}.", ok=False)
        return SkillResult(speech=f"contents: {WORLD[path]}")
    if tool == "files.write":
        return SkillResult(speech="written")
    if tool == "project.inspect":
        return SkillResult(speech="FRIDAY project: python, 3 modules, tests present.")
    return SkillResult(speech=f"{tool} ok")


FAKE_SPECS = [ToolSpec(name="files.write", description="write a file", tier="L2", params="path (str), text (str)")]


def real_tool_names() -> list[str]:
    return [s.name for s in REGISTRY.all() if s.name != "plan.run"]


EVENTS: dict[str, int] = {}


async def _count(topic: str):
    async def handler(ev) -> None:
        EVENTS[topic] = EVENTS.get(topic, 0) + 1

    BUS.subscribe(topic, handler)
    return handler


async def drive(
    goal: str, replies: list[str], *, coverage: bool = True, scope="auto", max_steps: int = 8,
    discovery_mode: bool = False, prompt_goal: str | None = None,
):
    """Scripted planner -> the real Orchestrator.run_goal -> recording runner."""
    CALLS.clear()
    EVENTS.clear()
    specs = _tool_specs(real_tool_names()) + FAKE_SPECS
    planner = ScriptedPlanner(replies)
    orch = Orchestrator(
        tools=[s.name for s in specs], runner=runner, actor="test", llm_provider=planner,
        tool_specs=specs, max_steps=max_steps,
    )
    if scope == "auto":
        scope = intent.derive_scope(goal)
    handlers = [(t, await _count(t)) for t in ("orchestrator.coverage_nudge", "orchestrator.evidence_stop")]
    try:
        res = await orch.run_goal(
            prompt_goal or goal, action_scope=scope, discovery_mode=discovery_mode,
            coverage_goal=goal if coverage else None, max_replans=CFG.planner.max_replans,  # as plan.run does
        )
    finally:
        for topic, h in handlers:
            BUS.unsubscribe(topic, h)
    return res, planner


def ran() -> list[str]:
    return [t for t, _ in CALLS]


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None, error: str = "", data: dict | None = None) -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, data or {}, error)


def reset() -> None:
    CALLS.clear()
    BEHAVE.clear()
    INTEL.reset()
    CFG.planner.structured_output = False
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.intent_guard = True
    CFG.planner.intent_prefilter = True
    CFG.planner.goal_coverage = True
    CFG.planner.max_coverage_nudges = 1
    CFG.desktop_observer.enabled = False


# ================================================================================
# A — clause derivation
# ================================================================================


def section_a() -> None:
    scenario("A: derive_clauses — literal, bounded, deterministic; single-clause goals untouched")
    dc = discovery.derive_clauses
    check("'Check the time and battery level.' -> two clauses", dc(GOAL_TB) == ["Check the time", "battery level"], str(dc(GOAL_TB)))
    check("a comma list -> three clauses", dc("check the time, battery, and network status") == ["check the time", "battery", "network status"])
    check("'open Chrome and search for cats' -> two clauses", len(dc("open Chrome and search for cats")) == 2)
    check("'then' is a connector", len(dc("read the readme then summarize the config")) == 2)
    check("a single-clause goal is one clause (itself)", dc("read this file") == ["read this file"])
    check("a question is one clause", dc("why is my app failing") == ["why is my app failing"])
    check("a clause with no content word does not create a requirement ('... and then continue')", dc("read the readme and then continue") == ["read the readme and then continue"])
    check("a negated fragment is a constraint, not a requirement ('just say done, no decomposition needed')",
          dc("just say done, no decomposition needed") == ["just say done, no decomposition needed"])
    check("...but a real second clause after a comma still counts", len(dc("read the readme, don't change anything, and summarize the config")) == 2)
    check("empty goal -> no clauses", dc("") == [] and dc("   ") == [])
    long_goal = "check " + ", ".join(f"item{i} status" for i in range(9))
    check(f"bounded: at most MAX_CLAUSES ({discovery.MAX_CLAUSES})", len(dc(long_goal)) <= discovery.MAX_CLAUSES, str(len(dc(long_goal))))
    check("deterministic: same input, same output", all(dc(g) == dc(g) for g in (GOAL_TB, long_goal, "open x and read y")))
    literal = True
    for g in (GOAL_TB, "check the time, battery, and network status", "open Chrome and search for cats"):
        for c in dc(g):
            literal &= c.lower() in g.lower()
    check("never invents a requirement: every clause is a fragment of the user's own text", literal)
    check("'search for cats and dogs' splits (documented keyword limitation, bounded cost)", len(dc("search for cats and dogs")) == 2)
    check("clause keywords drop verbs and filler", discovery._clause_keywords("Check the time") == {"time"})
    check("plural folding is consistent", discovery._stem("files") == discovery._stem("file") and discovery._stem("batteries") == "battery")
    check("RequirementStatus has exactly the four required values",
          {s.value for s in RS} == {"satisfied", "unsatisfied", "failed", "unknown"})


# ================================================================================
# B — per-requirement status
# ================================================================================


def section_b() -> None:
    scenario("B: assess_coverage — satisfied / unsatisfied / failed / unknown")
    ac = discovery.assess_coverage
    t = obs("system.time", "It's 3:45 PM.")
    b = obs("system.battery", "Battery is at 80 percent, charging.")

    c = ac(GOAL_TB, [t])
    check("time only: time SATISFIED, battery UNSATISFIED", [r.status for r in c.requirements] == [RS.SATISFIED, RS.UNSATISFIED], str(c.to_dict()))
    check("...so the goal is NOT covered", not c.all_satisfied and c.multi_clause)
    check("...and the unmet part is named from the user's own words", c.unmet() == ["battery level"])
    c = ac(GOAL_TB, [t, b])
    check("time + battery: every requirement SATISFIED", c.all_satisfied and c.unmet() == [])
    c = ac(GOAL_TB, [])
    check("no observations: nothing satisfied", [r.status for r in c.requirements] == [RS.UNSATISFIED, RS.UNSATISFIED] and not c.all_satisfied)
    c = ac(GOAL_TB, [t, obs("system.battery", "I couldn't read the battery.", ok=False)])
    check("a failed relevant call: that requirement is FAILED (not unknown, not satisfied)",
          c.requirements[1].status is RS.FAILED and c.any_failed and not c.all_satisfied)
    c = ac(GOAL_TB, [t, obs("system.battery", "Battery seems to be around 80 percent.", data={"uncertain": True})])
    check("an unconfirmed result is UNKNOWN, never SATISFIED", c.requirements[1].status is RS.UNKNOWN and not c.all_satisfied)
    c = ac(GOAL_TB, [t, obs("files.read", "Error: permission denied", ok=False, args={"path": "x"})])
    check("an unrelated error does not 'cover' the other clause", c.requirements[1].status is RS.UNSATISFIED)
    c = ac(GOAL_TB, [t, obs("system.battery", "blocked", ok=False, error="repeated_call")])
    check("a blocked repeat (never ran) is not evidence", c.requirements[1].status is RS.UNSATISFIED)
    c = ac(GOAL_TB, [t, obs("system.battery", "rejected", ok=False, error="intent_mismatch")])
    check("a rejected mismatch (never ran) is not evidence", c.requirements[1].status is RS.UNSATISFIED)
    c = ac(GOAL_TB, [b, obs("system.battery", "Battery is at 80 percent, charging.", args={"x": 1})])
    check("battery seen twice, time never: still not covered", not c.all_satisfied and c.requirements[0].status is RS.UNSATISFIED)

    single = ac("check the time", [t])
    check("single-clause goal: one requirement, delegated to assess_sufficiency (SATISFIED)",
          len(single.requirements) == 1 and single.all_satisfied and not single.multi_clause)
    check("single-clause with no evidence: UNSATISFIED", ac("check the time", []).requirements[0].status is RS.UNSATISFIED)
    check("single-clause result matches Phase 18 exactly",
          discovery.assess_sufficiency("check the time", [t]) is discovery.Sufficiency.SUFFICIENT)
    check("bounded: never more than MAX_CLAUSES requirements", len(ac("check " + ", ".join(f"item{i} status" for i in range(9)), []).requirements) <= discovery.MAX_CLAUSES)
    d = ac(GOAL_TB, [t]).to_dict()
    check("to_dict shape is stable (for logging)", set(d) == {"all_satisfied", "requirements"} and d["requirements"][0] == {"text": "Check the time", "status": "satisfied"})
    check("pure: assessing twice gives the same result", ac(GOAL_TB, [t, b]) == ac(GOAL_TB, [t, b]))


# ================================================================================
# C — the premature `done`
# ================================================================================


async def section_c() -> None:
    scenario("C: 'check the time and battery level' — BEFORE (coverage off) vs AFTER")
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("It's 3:45 PM.")], coverage=False)
    check("BEFORE: the run stops after only the time (1 tool, completed, ok)", ran() == ["system.time"] and res.ok and res.stopped == "completed")
    check("BEFORE: the battery was never checked and nothing objected", "system.battery" not in ran() and pl.calls == 2)

    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("It's 3:45."), call("system.battery"), done("3:45 PM, battery 80%")], scope=None)
    check("AFTER: both tools ran, time first", ran() == ["system.time", "system.battery"], str(ran()))
    check("AFTER: completed ok", res.ok and res.stopped == "completed")
    check("AFTER: exactly one nudge was needed (4 planner calls)", pl.calls == 4 and EVENTS.get("orchestrator.coverage_nudge") == 1)
    check("AFTER: the turn after the premature done names the missing part", "battery level" in pl.system_of(2) and "Do not reply done yet" in pl.system_of(2))
    check("AFTER: the turn BEFORE it was not nudged", "Do not reply done yet" not in pl.system_of(1))
    check("AFTER: the hint is single-turn (gone on the next prompt)", "Do not reply done yet" not in pl.system_of(3))
    check("AFTER: the summary is the planner's, evidence is real", len([o for o in res.observations if o.ok]) == 2)
    check("AFTER: evaluator still says SUCCESS for the completed run", evaluator.evaluate_goal(res).verdict is evaluator.Verdict.SUCCESS)

    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("It's 3:45."), call("system.battery"), done("x")])
    check("AFTER (look-only scope, the production shape): the missing part is named to the planner BEFORE it can say done", "battery level" in pl.system_of(1))
    check("...a planner that ignored that hint and said done is nudged once more, then both parts ran", ran() == ["system.time", "system.battery"] and EVENTS.get("orchestrator.coverage_nudge") == 1)
    check("...and the run stops on the evidence itself after the battery (3 planner calls, no 4th `done` needed)", pl.calls == 3 and res.ok)

    scenario("C2: the nudge is bounded — a planner that insists is believed")
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("just the time"), done("just the time")], scope=None)
    check("done, nudged, done again: the second done is trusted (3 planner calls)", pl.calls == 3 and res.stopped == "completed")
    check("...only ONE nudge ever fired (coverage must never trap a run)", EVENTS.get("orchestrator.coverage_nudge") == 1)
    check("...nothing beyond the time ran", ran() == ["system.time"])
    reset()
    CFG.planner.max_coverage_nudges = 0
    res, pl = await drive(GOAL_TB, [call("system.time"), done("just the time")], scope=None)
    check("max_coverage_nudges=0: no nudge at all", pl.calls == 2 and not EVENTS.get("orchestrator.coverage_nudge"))
    reset()
    CFG.planner.max_coverage_nudges = 2
    res, pl = await drive("check the time, battery, and network status", [call("system.time"), done("t"), call("system.battery"), done("b"), call("network.status"), done("n")], scope=None)
    check("max_coverage_nudges=2: bounded at two, all three parts covered", ran() == ["system.time", "system.battery", "network.status"] and EVENTS.get("orchestrator.coverage_nudge") == 2)
    reset()

    scenario("C3: what is NOT nudged")
    BEHAVE["system.battery"] = "fail"
    res, pl = await drive(GOAL_TB, [call("system.time"), call("system.battery"), done("time is 3:45; I couldn't read the battery")], scope=None)
    check("a clause whose tool FAILED is not nudged (a real outcome, not a gap)", pl.calls == 3 and not EVENTS.get("orchestrator.coverage_nudge"))
    check("...the run still completes with the real failure in its evidence", res.stopped == "completed" and any(not o.ok for o in res.observations))
    reset()
    res, pl = await drive("Check the time.", [call("system.time"), done("3:45")], scope=None)
    check("single-clause goal: never nudged, 2 planner calls", pl.calls == 2 and not EVENTS.get("orchestrator.coverage_nudge"))
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("t")], coverage=False)
    check("coverage_goal not given (every pre-Phase-21 caller): behaviour unchanged", pl.calls == 2 and ran() == ["system.time"])
    reset()
    CFG.planner.goal_coverage = False
    res, pl = await drive(GOAL_TB, [call("system.time"), done("t")])
    check("master switch off: coverage_goal is ignored", pl.calls == 2 and not EVENTS.get("orchestrator.coverage_nudge"))
    reset()
    BEHAVE["system.battery"] = "uncertain"
    res, pl = await drive(GOAL_TB, [call("system.time"), call("system.battery"), done("t"), done("t")], scope=None)
    check("an UNCONFIRMED battery result does not cover the clause: nudged once, then believed", pl.calls == 4 and EVENTS.get("orchestrator.coverage_nudge") == 1)
    reset()

    scenario("C4: an instant `done` on a two-part goal, and step accounting")
    res, pl = await drive(GOAL_TB, [done("all good"), call("system.time"), call("system.battery"), done("both")], scope=None)
    check("done with NOTHING observed on a two-part goal is sent back", EVENTS.get("orchestrator.coverage_nudge") == 1 and ran() == ["system.time", "system.battery"])
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("t"), call("system.battery"), done("b")], max_steps=3, scope=None)
    check("the nudge does not consume a real step: 2 real steps + the final done fit max_steps=3 (a refunded nudge; 4 turns would be needed otherwise)",
          ran() == ["system.time", "system.battery"] and res.stopped == "completed")
    reset()
    res, pl = await drive(GOAL_TB, [done("nothing"), done("nothing")], scope=None)
    check("a planner that never calls a tool gets no false success: 0 observations", not res.observations)
    ev = evaluator.evaluate_goal(res)
    check("...evaluator: UNCERTAIN, goal_complete False (unobserved is never SUCCESS)", ev.verdict is evaluator.Verdict.UNCERTAIN and not ev.goal_complete)
    reset()


# ================================================================================
# D — evidence-driven stop (look-only goals)
# ================================================================================


async def section_d() -> None:
    scenario("D: a look-only run stops on its own once EVERY part has real evidence")
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), call("system.battery"), done("never asked")])
    check("both parts covered -> stops without a `done` (2 planner calls, not 3)", pl.calls == 2 and ran() == ["system.time", "system.battery"])
    check("...completed ok, evidence stop published once", res.ok and res.stopped == "completed" and EVENTS.get("orchestrator.evidence_stop") == 1)
    check("...after only the time it did NOT stop (turn 2 carried the missing-part hint)", "battery level" in pl.system_of(1))
    check("...the summary is built from real observations", "3:45" in res.summary and "80 percent" in res.summary)
    check("...evaluator: SUCCESS", evaluator.evaluate_goal(res).verdict is evaluator.Verdict.SUCCESS)

    reset()
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "config.yaml"}), call("files.read", {"path": "README.md"}), done("x")])
    check("single-clause look-only goal with its answer in hand: stops after 1 tool", pl.calls == 1 and ran() == ["files.read"])
    check("...that is the fix for the loop-on-reads the Phase 20 live run measured", res.ok and res.stopped == "completed")

    reset()
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "nope.txt"}), done("could not read it")])
    check("a FAILED read never counts as the answer (no evidence stop)", pl.calls == 2 and not EVENTS.get("orchestrator.evidence_stop"))
    check("...and the run does not claim success after a failed-only history", not res.ok and res.stopped == "failure")
    reset()
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "nope.txt"}), call("files.read", {"path": "config.yaml"}), done("x")])
    check("failed read, then a good one: it stops after the good one", pl.calls == 2 and EVENTS.get("orchestrator.evidence_stop") == 1)

    reset()
    BEHAVE["system.battery"] = "uncertain"
    res, pl = await drive("Inspect the battery.", [call("system.battery"), done("about 80")])
    check("an unconfirmed result is not enough to stop on (asks the planner again)", pl.calls == 2 and not EVENTS.get("orchestrator.evidence_stop"))

    reset()
    res, pl = await drive("Fix the port in config.yaml.", [call("files.read", {"path": "config.yaml"}), call("files.write", {"path": "config.yaml", "text": "port: 9000"}), done("fixed")])
    check("a goal that authorizes modify NEVER stops on evidence alone (read, then write ran)", ran() == ["files.read", "files.write"])
    reset()
    res, pl = await drive("Open Chrome and read config.yaml.", [call("files.read", {"path": "config.yaml"}), call("apps.open", {"app": "Chrome"}), done("x")])
    check("a goal that authorizes open never stops on evidence alone", "apps.open" in ran() and not EVENTS.get("orchestrator.evidence_stop"))

    reset()
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "config.yaml"}), done("x")], scope=None)
    check("no action scope (every pre-Phase-20 caller): no evidence stop", pl.calls == 2 and not EVENTS.get("orchestrator.evidence_stop"))
    reset()
    CFG.planner.goal_coverage = False
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "config.yaml"}), done("x")])
    check("master switch off: the model's own done is what ends it", pl.calls == 2 and not EVENTS.get("orchestrator.evidence_stop"))
    reset()
    res, pl = await drive("Inspect config.yaml.", [call("files.read", {"path": "config.yaml"}), done("x")], coverage=False)
    check("coverage_goal not given: no evidence stop", pl.calls == 2 and not EVENTS.get("orchestrator.evidence_stop"))

    reset()
    res, pl = await drive("Inspect the project.", [call("ui.click", {"label": "Close"}), call("project.inspect"), done("x")])
    check("a rejected (mismatched) call is not evidence: the run continued to a real inspect", ran() == ["project.inspect"] and res.observations[0].error == "intent_mismatch")
    check("...then stopped on the real observation", EVENTS.get("orchestrator.evidence_stop") == 1 and pl.calls == 2)
    reset()


# ================================================================================
# E — the Phase 18 discovery gate
# ================================================================================


async def section_e() -> None:
    scenario("E: the Phase 18 sufficiency gate — guarded by coverage, unchanged for one clause")
    wrapped = (f"Investigate before acting: {GOAL_TB[:-1]}. Gather evidence only — do not attempt to fix "
               "or change anything yet. Once you have enough evidence, respond done with a summary "
               "of what you found and what remains unknown.")
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), call("system.battery"), done("x")], discovery_mode=True,
                          prompt_goal=wrapped, coverage=False, scope=None)
    check("BEFORE (no coverage): the discovery gate declares the goal complete after the time alone", ran() == ["system.time"] and pl.calls == 1)
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), call("system.battery"), done("x")], discovery_mode=True,
                          prompt_goal=wrapped, scope=None)
    check("AFTER (coverage on): it does NOT stop after only the time", "system.battery" in ran())
    check("...it stops once both parts have evidence, without another planning call", ran() == ["system.time", "system.battery"] and pl.calls == 2 and res.ok)
    check("...and the planner was told what was missing", "battery level" in pl.system_of(1))
    reset()
    res, pl = await drive("What is the battery level?", [call("system.battery"), done("x")], discovery_mode=True,
                          prompt_goal="Investigate before acting: What is the battery level?. Gather evidence only.", scope=None)
    check("single-clause discovery: Phase 18 behaviour exactly (stops after the 1 relevant read)", ran() == ["system.battery"] and pl.calls == 1)
    reset()


# ================================================================================
# F — safety invariants and the real plan.run path
# ================================================================================


async def section_f() -> None:
    scenario("F: coverage never widens authority, never asserts an unobserved result")
    reset()
    res, pl = await drive(GOAL_TB, [call("system.time"), done("t"), call("files.write", {"path": "x", "text": "y"}), call("system.battery"), done("b")])
    check("a nudged planner that reaches for a write is still rejected by the intent guard", "files.write" not in ran())
    check("...as an intent_mismatch, before anything else", any(o.error == "intent_mismatch" for o in res.observations))
    check("...and the run still covered both parts afterwards", ran() == ["system.time", "system.battery"])
    lines = lambda s: {ln for ln in s.splitlines() if ln.startswith("- ")}  # noqa: E731
    check("the coverage hint adds no tool: the offered tool list is identical",
          lines(pl.prompt_of(1)) == lines(pl.prompt_of(2)))
    check("the nudge never called the runner (only the model's next decision does)", CALLS[0][0] == "system.time")

    reset()
    res, pl = await drive(GOAL_TB, [done("all done"), done("all done")])
    check("the model insisting on done with nothing observed: completed but UNVERIFIED (no observations)", res.stopped == "completed" and not res.observations)
    check("...never SUCCESS", evaluator.evaluate_goal(res).verdict is not evaluator.Verdict.SUCCESS)

    scenario("F2: the real plan.run path (real executor; the tools are L0 reads)")
    with store.use_temp_db():
        for flag, expected in ((True, ["system.time", "system.battery"]), (False, ["system.time"])):
            reset()
            CFG.planner.goal_coverage = flag
            # plan.run spends its first model call on goal decomposition ("...and..."); "{}" = no subgoals
            replies = ["{}", call("system.time"), done("t"), call("system.battery"), done("t and b")]
            planner = ScriptedPlanner(replies)
            with scripted_provider(planner):
                r = await EXECUTOR.run("plan.run", {"goal": GOAL_TB}, actor="text")
            tools = [s["tool"] for s in r.data["steps"]]
            check(f"plan.run, goal_coverage={flag}: steps {expected}", tools == expected, str(tools))
            check(f"plan.run, goal_coverage={flag}: ok and verified", r.ok and r.data["verified"] is True)
            rec = goals_mod.get(r.data["goal_id"])
            check(f"plan.run, goal_coverage={flag}: the Goal row is SUCCEEDED", rec is not None and rec.status is goals_mod.GoalStatus.SUCCEEDED)
        reset()

    scenario("F3: static invariants")
    src = inspect.getsource(discovery)
    tree = ast.parse(src)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    check("discovery.py never imports permissions/executor (coverage cannot touch authority)",
          not any("permissions" in (m or "") for m in imports) and "EXECUTOR" not in src)
    check("discovery.py has no persistence (no second memory)", "store." not in src and "sqlite" not in src)
    sig = inspect.signature(discovery.assess_coverage)
    check("assess_coverage's only inputs are the goal text and the run's observations", list(sig.parameters) == ["goal", "observations"])
    check("derive_clauses takes only text (no context / experience input)", list(inspect.signature(discovery.derive_clauses).parameters) == ["goal", "max_clauses"])
    perm_src = (ROOT / "friday" / "permissions.py").read_text(encoding="utf-8")
    check("permissions.py does not know about coverage", "coverage" not in perm_src.lower())


# ================================================================================
# G — no second subsystem, evaluator semantics pinned
# ================================================================================


def section_g() -> None:
    scenario("G: additive only — Goal/contract/evaluator semantics unchanged")
    check("GoalContract gained no field for coverage (no second goal state)",
          set(goals_mod.GoalContract.__dataclass_fields__) == {
              "intent", "desired_outcome", "mode", "known_constraints", "unknowns", "success_conditions",
              "stop_conditions", "risk_level", "final_verdict", "pending_question", "action_scope"})
    check("Verdict values unchanged", {v.value for v in evaluator.Verdict} == {"success", "failure", "uncertain"})
    ok = evaluator.evaluate_step(obs("system.time", "t"))
    unc = evaluator.evaluate_step(obs("system.battery", "b", data={"uncertain": True}))
    bad = evaluator.evaluate_step(obs("system.battery", "b", ok=False))
    check("evaluate_step semantics pinned (success / uncertain / failure)",
          ok.verdict is evaluator.Verdict.SUCCESS and unc.verdict is evaluator.Verdict.UNCERTAIN and bad.verdict is evaluator.Verdict.FAILURE)
    check("evaluator.py does not import discovery (coverage is an input to stopping, not to judging)",
          "discovery" not in inspect.getsource(evaluator))
    check("there is exactly one goal_coverage switch and one nudge bound in config",
          CFG.planner.goal_coverage is True and CFG.planner.max_coverage_nudges == 1)


async def main() -> int:
    t0 = time.perf_counter()
    saved = H.lock_down_real_tools(REGISTRY)
    saved_cfg = (CFG.desktop_observer.enabled,)
    try:
        with store.use_temp_db():
            section_a()
            section_b()
            await section_c()
            await section_d()
            await section_e()
            await section_f()
            section_g()
    finally:
        CFG.permissions.overrides = saved
        (CFG.desktop_observer.enabled,) = saved_cfg
    return H.finish("GOAL COVERAGE SCORECARD", time.perf_counter() - t0, min_assertions=80, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
