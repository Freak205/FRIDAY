"""Phase 60 -- the tool set computed for a goal is exactly the tool set the planner is shown, in the prompt AND in the structured-output schema, on every turn.

Traced (`Orchestrator.run_goal` -> `_plan_decision`): `run_goal` computes `prompt_specs = offered_for(scope, specs)` once per run (falling back to the full
catalog when the guard/prefilter is off, there is no scope, or the filter would leave nothing); each turn derives `turn_specs` from it (desktop-window tools
hidden inside a browser task, the previous turn's rejected tool avoided) and passes them to `_plan_decision`, which uses ONE list, `shown`, for the prompt's
tool lines (`_fit_prompt`), the JSON-Schema `tool` enum (`_response_format`), and the repair request's tool names; the validating `ToolCatalog` deliberately
keeps the FULL set so a hidden tool named anyway is a valid decision that `check_alignment` then refuses. Existing tests spot-checked a few tools in the prompt
and in the enum separately; nothing compared the two as sets, followed them across turns, repairs, avoid turns, budget compaction and consecutive runs, or tied
them to `offered_for`. This suite does, through the real `run_goal` with production-style construction (`tools=` names, specs derived from the registry, a scripted
model, a stub runner -- nothing executes). It is static / scripted evidence about what the planner is OFFERED; it does not show a live model choosing well.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase21_common import ScriptedPlanner, call, check, done, finish, scenario  # noqa: E402

from friday import intent  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.orchestrator import Orchestrator, _tool_specs  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402

REGISTRY.discover()
TOOLS = [s.name for s in REGISTRY.all() if s.name != "plan.run" and not s.internal]
SPECS = _tool_specs(TOOLS)
TIERS = [(s.name, s.tier) for s in SPECS]
ALL = set(TOOLS)
RAN: list[str] = []


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    RAN.append(tool)
    return SkillResult(speech=f"{tool} ok")


def reset() -> None:
    CFG.planner.structured_output = True
    CFG.planner.intent_guard = True
    CFG.planner.intent_prefilter = True
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.max_intent_rejections = 3
    CFG.planner.prompt_budget = True
    CFG.planner.chars_per_token = 3.5
    CFG.llm.num_ctx = 6144
    CFG.desktop_observer.enabled = False
    RAN.clear()


def make(replies: list[str]) -> tuple[Orchestrator, ScriptedPlanner]:
    planner = ScriptedPlanner(replies)
    return Orchestrator(tools=TOOLS, runner=runner, actor="test", llm_provider=planner, max_steps=8), planner


async def drive(goal: str, replies: list[str], *, scope="auto", context: str = "", orch: Orchestrator | None = None):
    planner = ScriptedPlanner(replies)
    if orch is None:
        orch = Orchestrator(tools=TOOLS, runner=runner, actor="test", llm_provider=planner, max_steps=8)
    else:
        orch.llm_provider = planner
    sc = intent.derive_scope(goal) if scope == "auto" else scope
    res = await orch.run_goal(goal, context=context, action_scope=sc, coverage_goal=goal)
    return res, planner, sc


def prompt_tools(planner: ScriptedPlanner, i: int) -> set[str]:
    """Tool names listed as tool lines in turn i's prompt (history/evidence lines are not `- <tool> ` lines of a registered tool... filtered by name)."""
    text = planner.prompt_of(i)
    return {m for m in re.findall(r"^- ([A-Za-z0-9_.]+) ", text, re.M) if m in ALL}


def schema_tools(planner: ScriptedPlanner, i: int) -> set[str] | None:
    fmt = planner.requests[i].response_format
    return set(fmt["properties"]["tool"]["enum"]) if isinstance(fmt, dict) else None


def offered(goal: str) -> set[str]:
    return intent.offered_for(intent.derive_scope(goal), TIERS)


def same(planner: ScriptedPlanner, turns: int) -> bool:
    return all(prompt_tools(planner, i) == schema_tools(planner, i) for i in range(turns))


async def main_async() -> None:
    scenario("A. one turn: prompt tool lines == schema enum == offered_for(scope), for different scopes")
    goals = {
        "read-only": "Inspect my project.", "open": "Open my project.", "modify": "Fix my Flask startup issue.",
        "send": "Send Rahul a message on whatsapp that I'll be late.", "volume": "Set the volume to 30.",
        "compound": "Set the volume to 30 and lock my pc.", "unspecified": "Chrome", "empty": "",
    }
    for label, goal in goals.items():
        reset()
        _, pl, sc = await drive(goal, [done("d")])
        p, s = prompt_tools(pl, 0), schema_tools(pl, 0)
        check(f"A1 {label}: the prompt's tool lines and the schema enum are the same set", p == s and bool(p), f"{len(p)} vs {len(s or [])}: {sorted(p ^ (s or set()))[:4]}")
        check(f"A2 {label}: and it is exactly what offered_for computed for that scope ({len(p)} tools)", p == intent.offered_for(sc, TIERS))
    reset()
    _, pl, _ = await drive("Set the volume to 30 and lock my pc.", [done("d")])
    check("A3 a compound goal's set contains the tools of both parts", {"system.volume.set", "system.lock"} <= prompt_tools(pl, 0))
    check("A4 the catalog narrowed for a narrow scope, and a broad scope sees more", len(offered("Inspect my project.")) < len(offered("Fix my Flask startup issue.")) < len(ALL))

    scenario("B. every turn of a multi-step run shows the same set in both places (no drift)")
    reset()
    res, pl, sc = await drive("Fix my Flask startup issue.", [call("files.read", {"path": "a.py"}), call("files.read", {"path": "b.py"}),
                                                              call("shell.run", {"command": "pytest"}), done("done")])
    n = len(pl.requests)
    check("B1 the run took several planning turns", n >= 4, str(n))
    check("B2 on every turn the prompt set equals the schema set", same(pl, n))
    check("B3 and every turn shows the same set as the first (no widening, no narrowing mid-run)", all(prompt_tools(pl, i) == prompt_tools(pl, 0) for i in range(n)))

    scenario("C. a repair turn offers the same set; a hidden tool named anyway is judged by the guard, not run")
    reset()
    res, pl, sc = await drive("Inspect my project.", ["I think I should look around", call("project.inspect", {"name": "x"}), done("d")])
    shown = intent.offered_for(sc, TIERS)
    check("C1 the repaired first turn used the schema again, with the same enum as before", schema_tools(pl, 1) == schema_tools(pl, 0) == shown)
    repair_text = pl.system_of(1) + pl.prompt_of(1)
    check("C2 the repair request lists only offered tools (no hidden one is suggested)",
          all(f"{h}" not in repair_text for h in ("ui.click", "whatsapp.send", "apps.close")) and any(t in repair_text for t in ("project.inspect", "files.read")))
    reset()
    res, pl, sc = await drive("Inspect my project.", [call("ui.click", {"label": "Close"}), done("d")])
    check("C3 a tool outside the shown set, named anyway, is a valid decision the alignment guard refuses (never executed)",
          "ui.click" not in RAN and res.observations[0].error == "intent_mismatch" and res.invalid_decision is None, str([o.error for o in res.observations]))
    check("C4 the retry after that rejection still offers the same set minus only the rejected tool",
          prompt_tools(pl, 1) == schema_tools(pl, 1) == (intent.offered_for(sc, TIERS) - {"ui.click"}))

    scenario("D. the avoid turn narrows one turn only, and never widens")
    reset()
    res, pl, sc = await drive("Fix my Flask startup issue.", [call("files.read", {"path": "a.py"}), call("files.read", {"path": "a.py"}),
                                                              call("shell.run", {"command": "pytest"}), done("d")])
    base = intent.offered_for(sc, TIERS)
    check("D0 the run had the four planning turns the script needs", len(pl.requests) == 4 and "repeated_call" in [o.error for o in res.observations],
          f"{len(pl.requests)} {[o.error for o in res.observations]}")
    check("D1 the turn after a blocked repeat omits that tool in prompt and schema alike", prompt_tools(pl, 2) == schema_tools(pl, 2) == base - {"files.read"},
          str(sorted(base - prompt_tools(pl, 2))[:3]))
    check("D2 the turn after that returns to the full offered set (no stale narrowing)", prompt_tools(pl, 3) == schema_tools(pl, 3) == base)
    check("D3 no turn ever shows a tool that offered_for did not", all(prompt_tools(pl, i) <= base and schema_tools(pl, i) <= base for i in range(len(pl.requests))))

    scenario("E. consecutive runs on one Orchestrator never reuse another goal's catalog")
    reset()
    orch, _ = make([])
    sets = []
    for goal in ("Inspect my project.", "Fix my Flask startup issue.", "Inspect my project."):
        _, pl, sc = await drive(goal, [done("d")], orch=orch)
        sets.append((prompt_tools(pl, 0), schema_tools(pl, 0), intent.offered_for(sc, TIERS)))
    check("E1 each run shows its own goal's set in prompt and schema", all(p == s == o for p, s, o in sets))
    check("E2 the first and third (same goal) agree and differ from the middle one", sets[0][0] == sets[2][0] != sets[1][0])

    scenario("F. conservative fallbacks keep the FULL catalog, in both places")
    reset()
    _, pl, _ = await drive("Inspect my project.", [done("d")], scope=None)
    check("F1 no scope: all tools in the prompt and the schema", prompt_tools(pl, 0) == schema_tools(pl, 0) == ALL)
    reset()
    CFG.planner.intent_prefilter = False
    _, pl, _ = await drive("Inspect my project.", [done("d")])
    check("F2 prefilter off: all tools in both", prompt_tools(pl, 0) == schema_tools(pl, 0) == ALL)
    reset()
    CFG.planner.intent_guard = False
    _, pl, _ = await drive("Inspect my project.", [done("d")])
    check("F3 guard off (scope ignored): all tools in both", prompt_tools(pl, 0) == schema_tools(pl, 0) == ALL)
    reset()
    CFG.planner.structured_output = False
    _, pl, sc = await drive("Inspect my project.", [done("d")])
    check("F4 structured output off: no schema is sent and the prompt still shows exactly the offered set",
          pl.requests[0].response_format is None and prompt_tools(pl, 0) == intent.offered_for(sc, TIERS))

    scenario("G. budget compaction changes how tools are written, never which")
    reset()
    CFG.llm.num_ctx = 4096
    CFG.planner.chars_per_token = 3.0
    ambient = "Active window: Visual Studio Code. " * 120
    _, pl, sc = await drive("Fix my Flask startup issue.", [done("d")], context=ambient)
    full_names = intent.offered_for(sc, TIERS)
    check("G1 under a small window the prompt was compacted yet lists the same tools as the schema", prompt_tools(pl, 0) == schema_tools(pl, 0) == full_names)
    reset()
    _, pl2, _ = await drive("Fix my Flask startup issue.", [done("d")], context=ambient)
    check("G2 compact lines carry fewer characters than the full ones for the same tools", len(pl.prompt_of(0)) < len(pl2.prompt_of(0)))

    scenario("H. the tool lines carry the registry's argument names")
    reset()
    _, pl, sc = await drive("Fix my Flask startup issue.", [done("d")])
    lines = {m.group(1): m.group(0) for m in re.finditer(r"^- ([A-Za-z0-9_.]+) .*$", pl.prompt_of(0), re.M) if m.group(1) in ALL}
    reg = {s.name: s for s in REGISTRY.all()}
    check("H1 every shown tool's line names every one of its registered arguments (required and optional)",
          all(p.name in lines[n] for n in lines for p in reg[n].params), str([n for n in lines if not all(p.name in lines[n] for p in reg[n].params)][:3]))
    check("H2 a tool with required arguments is marked REQUIRED, one without is 'args: none'",
          all(("REQUIRED" in lines[n]) == any(p.required for p in reg[n].params) for n in lines)
          and all("args: none" in lines[n] for n in lines if not reg[n].params))


def main() -> int:
    t0 = time.perf_counter()
    saved = (CFG.planner.structured_output, CFG.planner.intent_guard, CFG.planner.intent_prefilter, CFG.planner.chars_per_token,
             CFG.llm.num_ctx, CFG.desktop_observer.enabled, CFG.planner.prompt_budget)
    try:
        asyncio.run(main_async())
    finally:
        (CFG.planner.structured_output, CFG.planner.intent_guard, CFG.planner.intent_prefilter, CFG.planner.chars_per_token,
         CFG.llm.num_ctx, CFG.desktop_observer.enabled, CFG.planner.prompt_budget) = saved
    return finish("Phase 60 -- the planner is shown exactly the offered catalog", time.perf_counter() - t0, min_assertions=30, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(main())
