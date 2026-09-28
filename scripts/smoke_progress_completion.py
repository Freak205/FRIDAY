"""Phase 24.6 — a progress/attempt observation must not, by itself, end a run.

Phase 24.5 closed one such path (`subgoal_step_satisfied`: an "Attempting to delete x."
line no longer closes a Phase 23 subgoal). Every OTHER place that decides "FRIDAY has enough
evidence, stop" reads conclusiveness from one shared helper, `discovery._is_conclusive`, and
that helper still called any successful line with speech conclusive:

  discovery sufficiency stop   `assess_sufficiency`  -> `run_goal` returns `_summarize(...)`
  look-only coverage stop      `assess_sufficiency` + per-clause coverage
  premature-`done` nudge       per-clause coverage says a clause is already SATISFIED, so a
                               planner `done` over a half-finished multi-part goal is believed
  "no result yet" coverage hint (the planner is never told the clause is still open)

so "Searching for report.csv..." ended a find goal, and "Counting the rows..." satisfied the
"row count" half of "find report.csv and tell me its row count". Phase 24.6 makes progress
lines non-conclusive at that one shared point. This suite pins it: at the helper level (what
counts as progress, what still counts as a result) and through the REAL `Orchestrator.run_goal`
stop paths (the run keeps going until the actual result arrives, and stops when it does).

Deterministic and offline: hand-built observations for the helper sections, a scripted model
behind the `llm.get_provider` seam plus a recording fake tool world for the run_goal sections
(the same scaffolding as the Phase 21/24.5 suites). No Ollama, no real side effect.
"""

from __future__ import annotations

import asyncio
import sys
import time

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import intent  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.registry import SkillResult  # noqa: E402

RS = discovery.RequirementStatus
SUFF = discovery.Sufficiency

FIND_ROWS = "Find report.csv and tell me its row count."


# -- hand-built observations for the helper-level sections -------------------------------


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None, data: dict | None = None) -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, data or {}, "")


def conclusive(goal: str, o: Observation) -> bool:
    return discovery._is_conclusive(discovery.classify_mode(goal), o)


def statuses(goal: str, observations: list[Observation]) -> list[str]:
    return [r.status.value for r in discovery.assess_coverage(goal, observations).requirements]


PROGRESS_LINES = [
    "Attempting to delete report.csv.",
    "Searching for report.csv...",
    "Opening the requested file...",
    "Trying to retrieve the data...",
]


# ================================================================================
# A — what counts as progress, and what is still a result
# ================================================================================


def section_a() -> None:
    scenario("A: progress lines are not conclusive; results, failures and legitimate hedges still are")
    bad = [s for s in PROGRESS_LINES if conclusive("Delete report.csv.", obs("t", s))]
    check("A1: 'Attempting…', 'Searching…', 'Opening…', 'Trying to…' are never conclusive", not bad, str(bad))

    results = [
        "Deleted report.csv successfully.", "Found report.csv.", "report.csv contains 42 rows.",
        "No matching file was found.",
    ]
    bad = [s for s in results if not conclusive("Delete report.csv.", obs("t", s))]
    check("A2: 'Deleted…successfully', 'Found…', 'contains 42 rows', 'No matching file was found' stay conclusive", not bad, str(bad))
    check("A3: a failure that names its cause stays conclusive ('Permission denied while deleting report.csv.')",
          conclusive("Delete report.csv.", obs("t", "Permission denied while deleting report.csv.", ok=False)))

    hedges = ["It will be sunny tomorrow.", "The file should contain 42 rows.", "I expect the operation to fail.",
              "report.csv probably has 42 rows."]
    misread = [s for s in hedges if discovery._is_progress_line(obs("t", s))]
    bad = [s for s in hedges if not conclusive("What will the weather be?", obs("t", s))]
    check("A4: future / expected / hedged FACTUAL statements are not classified as progress and stay conclusive, as before",
          not misread and not bad, f"misread={misread} not_conclusive={bad}")

    check("A5: a line that reports progress AND a result is a result ('Attempting to delete report.csv. Deleted report.csv successfully.')",
          conclusive("Delete report.csv.", obs("t", "Attempting to delete report.csv. Deleted report.csv successfully.")))
    check("A6: a concrete error signal inside a progress-shaped line still counts (Phase 18 diagnostic evidence)",
          conclusive("Why does it crash?", obs("t", "Attempting to import flask: ModuleNotFoundError: flask")))


# ================================================================================
# B — goal-aware sufficiency and coverage over hand-built evidence
# ================================================================================


def section_b() -> None:
    scenario("B: sufficiency / per-clause coverage — a progress line informs, a result completes")
    dele = {"path": "report.csv"}
    attempt = obs("files.delete", "Attempting to delete report.csv.", args=dele)
    check("B1: 'Delete report.csv.' + an attempt line is not SUFFICIENT; its requirement is UNKNOWN (evidence, but no result)",
          discovery.assess_sufficiency("Delete report.csv.", [attempt]) is not SUFF.SUFFICIENT
          and statuses("Delete report.csv.", [attempt]) == ["unknown"], str(statuses("Delete report.csv.", [attempt])))
    success = obs("files.delete_status", "Deleted report.csv successfully.", args=dele)
    check("B2: the actual success after the attempt completes it",
          discovery.assess_sufficiency("Delete report.csv.", [attempt, success]) is SUFF.SUFFICIENT
          and statuses("Delete report.csv.", [attempt, success]) == ["satisfied"])

    search = obs("files.find", "Searching for report.csv...", args={"name": "report.csv"})
    check("B3: a search-progress line does not complete a retrieval goal",
          discovery.assess_sufficiency("Find report.csv.", [search]) is not SUFF.SUFFICIENT)
    opening = obs("files.read", "Opening the requested file...", args={"path": "report.csv"})
    check("B4: an opening-progress line does not complete a read goal",
          discovery.assess_sufficiency("Read report.csv.", [opening]) is not SUFF.SUFFICIENT)
    check("B5: 'It will be sunny tomorrow.' still answers a forecast question (not misread as progress)",
          discovery.assess_sufficiency("What will the weather be tomorrow?", [obs("weather.get", "It will be sunny tomorrow.")]) is SUFF.SUFFICIENT)

    found = obs("files.find", "Found report.csv.", args={"name": "report.csv"})
    counting = obs("files.rows", "Counting the rows...", args=dele)
    rows = obs("files.rows", "report.csv contains 42 rows.", args={"path": "report.csv", "sheet": "final"})
    check("B6: 'Found report.csv.' satisfies the find clause but not the row-count clause",
          statuses(FIND_ROWS, [found]) == ["satisfied", "unsatisfied"], str(statuses(FIND_ROWS, [found])))
    check("B7: a progress line does not satisfy the pending row-count clause (UNKNOWN, goal not complete)",
          statuses(FIND_ROWS, [found, counting]) == ["satisfied", "unknown"]
          and not discovery.assess_coverage(FIND_ROWS, [found, counting]).all_satisfied,
          str(statuses(FIND_ROWS, [found, counting])))
    check("B8: the actual row count completes the goal",
          discovery.assess_coverage(FIND_ROWS, [found, counting, rows]).all_satisfied)

    denied = obs("files.rows", "Could not read report.csv: permission denied.", ok=False, args={"path": "report.csv", "sheet": "final"})
    cov = discovery.assess_coverage(FIND_ROWS, [found, counting, denied])
    check("B9: progress followed by a FAILURE — the failure is the outcome (clause FAILED, not left waiting)",
          [r.status.value for r in cov.requirements] == ["satisfied", "failed"] and cov.any_failed,
          str([r.status.value for r in cov.requirements]))
    check("B10: a failure with no progress line before it is FAILED exactly as before",
          statuses(FIND_ROWS, [found, denied]) == ["satisfied", "failed"], str(statuses(FIND_ROWS, [found, denied])))


# ================================================================================
# The run_goal sections: scripted model, recording tool world, the real orchestrator
# ================================================================================

SPECS = [
    # Named `test.pc.<verb>`: the intent guard classifies a tool the registry has never heard of by
    # the verb its name ends in (`intent._SUFFIX_CLASS`), so these run through the REAL guard.
    ToolSpec(name="test.pc.delete", description="start deleting a file", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.pc.remove", description="finish deleting a file", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.pc.find", description="find a file by name", tier="L0", params="name (str), where (str)", action="read"),
    ToolSpec(name="test.pc.rows", description="count the rows of a file", tier="L0", params="path (str), sheet (str)", action="read"),
    ToolSpec(name="test.pc.read", description="read a file", tier="L0", params="path (str), mode (str)", action="read"),
    ToolSpec(name="test.pc.list", description="list a folder", tier="L0", params="path (str)", action="read"),
]

CALLS: list[tuple[str, dict]] = []
RESULTS: dict[str, list[SkillResult]] = {}  # tool -> results, consumed in call order (the last repeats)
EVENTS: dict[str, list[dict]] = {}


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    queue = RESULTS.get(tool)
    if not queue:
        raise KeyError(f"no scripted result for test tool: {tool}")
    return queue[0] if len(queue) == 1 else queue.pop(0)


def world(**tools: list[SkillResult]) -> None:
    RESULTS.clear()
    RESULTS.update({f"test.pc.{name}": results for name, results in tools.items()})


def R(speech: str, *, ok: bool = True) -> SkillResult:
    return SkillResult(speech=speech, ok=ok)


def ran() -> list[str]:
    return [t.removeprefix("test.pc.") for t, _ in CALLS]


async def _drive(goal: str, decisions: list[str], *, mode: str, max_steps: int = 8):
    """mode: 'look' (look-only scope + coverage, as plan.run derives it), 'disc' (the discovery
    pass: discovery_mode + coverage), 'act' (the main pass: a scope that authorizes the action)."""
    CALLS.clear()
    EVENTS.clear()
    planner = ScriptedPlanner(decisions)
    orch = Orchestrator(
        tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=planner,
        tool_specs=SPECS, max_steps=max_steps,
    )
    topics = ("orchestrator.coverage_nudge", "orchestrator.evidence_stop")
    handlers = []
    for topic in topics:
        async def handler(ev, _t=topic) -> None:
            EVENTS.setdefault(_t, []).append(dict(ev.data))
        BUS.subscribe(topic, handler)
        handlers.append((topic, handler))
    try:
        res = await orch.run_goal(
            goal, action_scope=intent.derive_scope(goal), discovery_mode=(mode == "disc"),
            coverage_goal=goal, max_replans=CFG.planner.max_replans,  # as plan.run does
        )
    finally:
        for topic, h in handlers:
            BUS.unsubscribe(topic, h)
    return res, planner


def drive(goal: str, decisions: list[str], *, mode: str, **kw):
    return asyncio.run(_drive(goal, decisions, mode=mode, **kw))


def n(topic: str) -> int:
    return len(EVENTS.get(f"orchestrator.{topic}", []))


def reset() -> None:
    CFG.planner.structured_output = False
    CFG.planner.goal_coverage = True
    CFG.planner.max_coverage_nudges = 1


ROWS_ARGS = {"path": "report.csv"}
ROWS_ARGS_2 = {"path": "report.csv", "sheet": "final"}


# ================================================================================
# C — the look-only coverage stop
# ================================================================================


def section_c() -> None:
    scenario("C: look-only run — 'find report.csv and tell me its row count' waits for the real count")
    reset()
    check("C0: the goal really is a look-only scope (the precondition for the evidence stop)",
          not intent.derive_scope(FIND_ROWS).allowed)
    world(find=[R("Found report.csv.")], rows=[R("Counting the rows..."), R("report.csv contains 42 rows.")])
    res, pl = drive(FIND_ROWS, [
        call("test.pc.find", {"name": "report.csv"}),
        call("test.pc.rows", ROWS_ARGS),
        call("test.pc.rows", ROWS_ARGS_2),
        done("should never be needed"),
    ], mode="look")
    check("C1: 'Found report.csv.' (row-count clause still open) and then 'Counting the rows...' do not end the run — it reaches the real count",
          ran() == ["find", "rows", "rows"] and pl.calls == 3, f"{ran()} planner_calls={pl.calls}")
    check("C2: the evidence stop fires once, on the count; the run completed and the answer carries it",
          n("evidence_stop") == 1 and res.ok and res.stopped == "completed" and "42 rows" in res.summary,
          f"stops={n('evidence_stop')} ok={res.ok} {res.stopped} {res.summary}")

    reset()
    world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    res, pl = drive("Find report.csv.", [
        call("test.pc.find", {"name": "report.csv"}),
        call("test.pc.find", {"name": "report.csv", "where": "Downloads"}),
        done("unused"),
    ], mode="look")
    check("C3: a search-progress line does not end a find goal; the actual find does",
          ran() == ["find", "find"] and pl.calls == 2 and "Found report.csv." in res.summary, f"{ran()} {res.summary}")

    reset()
    world(read=[R("Opening the requested file..."), R("contents: revenue,42")])
    res, pl = drive("Read report.csv.", [
        call("test.pc.read", {"path": "report.csv"}),
        call("test.pc.read", {"path": "report.csv", "mode": "text"}),
        done("unused"),
    ], mode="look")
    check("C4: an opening-progress line does not end a read goal; the contents do",
          ran() == ["read", "read"] and "revenue,42" in res.summary, f"{ran()} {res.summary}")

    reset()
    world(find=[R("Found report.csv.")], rows=[R("Counting the rows..."), R("Could not read report.csv: permission denied.", ok=False)])
    res, pl = drive(FIND_ROWS, [
        call("test.pc.find", {"name": "report.csv"}),
        call("test.pc.rows", ROWS_ARGS),
        call("test.pc.rows", ROWS_ARGS_2),
        done("I found report.csv but could not read its rows: permission denied."),
    ], mode="look")
    check("C5: progress then FAILURE — the failure is not mistaken for a result (no evidence stop) and is what the user hears, with no invented count",
          n("evidence_stop") == 0 and "permission denied" in res.summary.lower() and "42" not in res.summary,
          f"stops={n('evidence_stop')} {res.summary}")
    check("C6: ...and the failed part is not nudged as if it were unanswered (no waiting for a result that cannot come)",
          n("coverage_nudge") == 0, f"nudges={n('coverage_nudge')}")


# ================================================================================
# D — the discovery sufficiency stop
# ================================================================================


def section_d() -> None:
    scenario("D: discovery pass — the sufficiency stop needs the result, not the progress line")
    reset()
    world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    res, pl = drive("Find report.csv.", [
        call("test.pc.find", {"name": "report.csv"}),
        call("test.pc.find", {"name": "report.csv", "where": "Downloads"}),
        done("unused"),
    ], mode="disc")
    check("D1: a search-progress line does not trigger the sufficiency stop; the run continues to the real find and stops on it",
          ran() == ["find", "find"] and pl.calls == 2 and res.ok and res.stopped == "completed" and "Found report.csv." in res.summary,
          f"{ran()} planner_calls={pl.calls} {res.ok} {res.stopped} {res.summary}")

    reset()
    world(find=[R("Found report.csv.")], rows=[R("Counting the rows..."), R("report.csv contains 42 rows.")])
    res, pl = drive(FIND_ROWS, [
        call("test.pc.find", {"name": "report.csv"}),
        call("test.pc.rows", ROWS_ARGS),
        call("test.pc.rows", ROWS_ARGS_2),
        done("unused"),
    ], mode="disc")
    check("D2: multi-part goal — found + a progress line is not 'covered'; only the real count ends the run",
          ran() == ["find", "rows", "rows"] and "42 rows" in res.summary, f"{ran()} {res.summary}")

    reset()
    world(find=[R("Found report.csv.")], rows=[R("report.csv contains 42 rows.")])
    res, pl = drive(FIND_ROWS, [call("test.pc.find", {"name": "report.csv"}), call("test.pc.rows", ROWS_ARGS), done("unused")], mode="disc")
    check("D3: with real results only, the run stops exactly when it always did (2 calls, 2 planning turns)",
          ran() == ["find", "rows"] and pl.calls == 2 and res.ok, f"{ran()} planner_calls={pl.calls}")


# ================================================================================
# E — a planner `done` over an unfinished part
# ================================================================================


def section_e() -> None:
    scenario("E: premature `done` — a state-changing part that only ATTEMPTED is still open")
    goal = "Delete report.csv and list the folder."
    reset()
    world(delete=[R("Attempting to delete report.csv.")], remove=[R("Deleted report.csv successfully.")], list=[R("Folder contains notes.txt.")])
    dele, listing, remove = call("test.pc.delete", {"path": "report.csv"}), call("test.pc.list", {"path": "."}), call("test.pc.remove", {"path": "report.csv"})
    res, pl = drive(goal, [dele, listing, done("I deleted report.csv and listed the folder."), remove,
                           done("I deleted report.csv and listed the folder.")], mode="act")
    nudged = [u for e in EVENTS.get("orchestrator.coverage_nudge", []) for u in e.get("unmet", [])]
    check("E1: an attempt line does not count as the deletion — the early `done` is sent back once, naming the delete part",
          n("coverage_nudge") == 1 and any("delete" in u.lower() for u in nudged), f"nudges={n('coverage_nudge')} {nudged}")
    check("E2: the run continues to the real result", ran() == ["delete", "list", "remove"], str(ran()))
    check("E3: progress followed by success — the final answer reports the deletion",
          res.ok and "deleted report.csv" in res.summary.lower(), res.summary)

    reset()
    world(delete=[R("Attempting to delete report.csv.")], remove=[R("Delete failed: permission denied.", ok=False)], list=[R("Folder contains notes.txt.")])
    res, pl = drive(goal, [dele, listing, done("I deleted report.csv and listed the folder."), remove,
                           done("Could not delete report.csv: permission denied.")], mode="act")
    check("E4: attempt then FAILURE — the final answer is the failure, not a claimed deletion",
          "permission denied" in res.summary.lower() and "i deleted" not in res.summary.lower(), res.summary)

    reset()
    world(delete=[R("Deleted report.csv successfully.")], list=[R("Folder contains notes.txt.")])
    res, pl = drive(goal, [dele, listing, done("I deleted report.csv and listed the folder.")], mode="act")
    check("E5: nothing changes when both parts really have results — no nudge, the summary passes through",
          n("coverage_nudge") == 0 and res.summary == "I deleted report.csv and listed the folder.", f"nudges={n('coverage_nudge')} {res.summary}")


def main() -> int:
    t0 = time.perf_counter()
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    return H.finish("Phase 24.6 — progress evidence never completes a goal", time.perf_counter() - t0,
                    min_assertions=28, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
