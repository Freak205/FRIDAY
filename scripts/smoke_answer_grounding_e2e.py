"""Phase 24.5 — END-TO-END grounded-answer reliability, through the real orchestrator.

Phases 24.1-24.4 pin `discovery.ground_answer` with hand-built observation lists
(scripts/smoke_answer_grounding_guard.py). This suite asks the integration question the
unit suite cannot: when the evidence is produced by the REAL `Orchestrator.run_goal` — real
step execution, real observation ordering, the real Phase 23 subgoal machinery, the real
planner `done` path — does the text that comes back as `OrchestratorResult.summary` (the
final answer the user hears) respect those protections?

Two real final-answer routes exist and both are driven here:

  SUB   the Phase 23 route: a `subgoals` breakdown whose last subgoal is an ANSWER subgoal ->
        `Orchestrator._answer_from_evidence` composes the answer (one bounded, tool-less
        model call), then `guard_against_overclaiming` (Phase 17) and `ground_answer`
        (Phase 24) run over it
  DONE  the ordinary route: the planner says `done` and its own `summary` IS the answer

(The remaining routes — the evidence-sufficiency stop, the look-only coverage stop, a run that
failed outright, the empty-composer fallback — return `_summarize(observations)`: the tools'
own words, deterministic, so there is nothing to ground.)

This suite found two real integration defects, both fixed in Phase 24.5 and pinned here:
  1. the DONE route never went through `ground_answer` at all (a summary claiming a deletion
     the tool refused, or an invented filename/number, reached the user verbatim) — B/C/E/G/I/J/K
     DONE assertions, and P3/P4/P6 through the real plan.run
  2. Phase 23's `subgoal_step_satisfied` credited an "Attempting to ..." line as a finished
     subgoal, so the answer was composed one step BEFORE the tool's final result — B/SUB, B3, F2

Scenarios A-L are the brief's twelve realistic workflows (L also pins that ordinary answers are
not made defensive); M pins the Phase 17 / Phase 23 interaction and the kill switch; P runs the
same protections through the real `plan.run` skill (real executor, real intent guard, real Goal
row) so the string a user actually gets is checked too.

Entirely deterministic: a scripted model behind the existing `llm.get_provider` seam
(`phase21_common`), a recording fake tool world for the run_goal sections and registered
`test.*` fixture skills for the plan.run section. No Ollama, no real side effect.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario, scripted_provider  # noqa: E402

from friday import store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.goals import Subgoal  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.orchestrator import Orchestrator, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()

# -- a recording tool world: nothing real ever runs (sections A-M) ---------------------

CALLS: list[tuple[str, dict]] = []
RESULTS: dict[str, list[SkillResult]] = {}  # tool -> results, consumed in call order (the last repeats)

SPECS = [
    ToolSpec(name="test.e2e_delete", description="delete a named file", tier="L1", params="path (str), force (bool)", action="delete"),
    ToolSpec(name="test.e2e_find", description="find a file by name", tier="L0", params="name (str)", action="read"),
    ToolSpec(name="test.e2e_rows", description="count the rows of a file", tier="L0", params="path (str)", action="read"),
    ToolSpec(name="test.e2e_check", description="verify the state of a file", tier="L0", params="path (str)", action="read"),
    ToolSpec(name="test.e2e_begin", description="start a slow delete", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.e2e_progress", description="report progress", tier="L0", params="", action="read"),
    ToolSpec(name="test.e2e_finish", description="finish a slow delete", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.e2e_time", description="the current time", tier="L0", params="", action="read"),
    ToolSpec(name="test.e2e_open", description="open an app", tier="L1", params="name (str)", action="open"),
]


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    queue = RESULTS.get(tool)
    if not queue:
        raise KeyError(f"no scripted result for test tool: {tool}")
    return queue[0] if len(queue) == 1 else queue.pop(0)


def world(**tools: list[SkillResult]) -> None:
    RESULTS.clear()
    RESULTS.update({f"test.e2e_{name}": results for name, results in tools.items()})


def R(speech: str, *, ok: bool = True, **data) -> SkillResult:
    return SkillResult(speech=speech, ok=ok, data=data or None)


class RoutedPlanner(ScriptedPlanner):
    """A scripted model that tells the two kinds of call apart: a tool-DECISION turn consumes
    the next entry of `decisions` (the last repeats); the dedicated ANSWER-composition call
    (Phase 23's `_answer_from_evidence`, recognized by its own system prompt) always returns
    `answer`. Lets a scenario script a whole workflow without guessing how many turns the
    orchestrator will spend before it composes."""

    def __init__(self, decisions: list[str], answer: str = "") -> None:
        super().__init__(decisions)
        self.answer = answer
        self.decision_calls = 0
        self.answer_calls = 0

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        system = next((m.content for m in request.messages if m.role == "system"), "")
        if "using ONLY the evidence" in system:
            self.answer_calls += 1
            text = self.answer
        else:
            text = self.replies[min(self.decision_calls, len(self.replies) - 1)]
            self.decision_calls += 1
        return LlmResponse(text=text, model="scripted", provider=self.name)


def sg(desc: str, kind: str = "acquisition") -> Subgoal:
    return Subgoal(id=str(uuid.uuid4()), description=desc, kind=kind)


def reset() -> None:
    CALLS.clear()
    RESULTS.clear()
    CFG.planner.structured_output = False
    CFG.planner.subgoal_evidence_advance = True
    CFG.planner.answer_from_evidence = True
    CFG.planner.answer_grounding_guard = True
    CFG.planner.goal_coverage = True


async def _drive(goal: str, decisions: list[str], *, answer: str = "", subgoals: list[Subgoal] | None = None,
                 max_steps: int = 10):
    """Scripted planner -> the real Orchestrator.run_goal -> the recording tool world, called the way
    `plan.run` calls it (same coverage goal, same replan budget)."""
    CALLS.clear()
    planner = RoutedPlanner(decisions, answer)
    orch = Orchestrator(
        tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=planner,
        tool_specs=SPECS, max_steps=max_steps,
    )
    res = await orch.run_goal(
        goal, subgoals=subgoals, coverage_goal=goal, max_replans=CFG.planner.max_replans,
    )
    return res, planner


def drive(goal: str, decisions: list[str], **kw):
    return asyncio.run(_drive(goal, decisions, **kw))


def ran() -> list[str]:
    return [t.removeprefix("test.e2e_") for t, _ in CALLS]


def low(text: str) -> str:
    return (text or "").lower()


def claims_deleted(text: str) -> bool:
    return bool(re.search(r"\b(?:deleted|removed)\b", low(text)))


def failed_closed(text: str) -> bool:
    return "insufficient" in low(text)


DEL = {"path": "report.csv"}


# ================================================================================
# A — a successful action is reported as a success
# ================================================================================


def section_a() -> None:
    scenario("A: delete a named file, the tool reports the deletion — both final-answer routes say so, unchanged")
    reset()
    world(delete=[R("Deleted report.csv successfully.")])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL)], answer="I deleted report.csv successfully.", subgoals=subs)
    check("A/SUB: the delete ran exactly once", ran() == ["delete"], str(CALLS))
    check("A/SUB: the answer came from the Phase 23 composer (one answer call)", pl.answer_calls == 1, str(pl.answer_calls))
    check("A/SUB: the final answer reports the deletion", claims_deleted(res.summary) and "report.csv" in res.summary, res.summary)
    check("A/SUB: a legitimate answer passes through the guards untouched",
          res.summary == "I deleted report.csv successfully.", res.summary)
    check("A/SUB: the run completed ok", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")

    reset()
    world(delete=[R("Deleted report.csv successfully.")])
    res, pl = drive("Delete report.csv.", [call("test.e2e_delete", DEL), done("I deleted report.csv.")])
    check("A/DONE: the planner's own summary reports the deletion, unchanged",
          res.summary == "I deleted report.csv." and res.ok, res.summary)


# ================================================================================
# B — the tool ran, but the evidence never establishes the deletion
# ================================================================================


def section_b() -> None:
    scenario("B: the delete tool ran but only ATTEMPTED — no final answer may claim the deletion")
    reset()
    world(delete=[R("Attempting to delete report.csv.")])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL), done("I deleted report.csv.")], answer="I deleted report.csv.", subgoals=subs)
    check("B/SUB: an attempt line does not close the delete subgoal, so no answer is composed from it",
          pl.answer_calls == 0, str(pl.answer_calls))
    check("B/SUB: the planner's later `done` claiming the deletion is not passed through",
          not claims_deleted(res.summary), res.summary)
    check("B/SUB: it says plainly the claim isn't confirmed (fail-closed)", failed_closed(res.summary), res.summary)

    reset()
    world(delete=[R("Attempting to delete report.csv.")])
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL)], answer="I deleted report.csv.", subgoals=[sg("Delete report.csv"), sg("Tell me what happened", "answer")])
    check("B3/SUB: a planner that only ever gets the attempt line stops honestly — no deletion claim anywhere",
          not res.ok and not claims_deleted(res.summary) and "Attempting to delete report.csv." in res.summary, f"{res.ok} {res.summary}")

    reset()
    world(delete=[R("Attempting to delete report.csv.")])
    res, pl = drive("Delete report.csv.", [call("test.e2e_delete", DEL), done("I deleted report.csv.")])
    check("B/DONE: the planner's `done` summary is not passed through as a completed deletion",
          not claims_deleted(res.summary), res.summary)

    reset()
    world(delete=[R("")])  # ok, but the tool reported nothing at all
    res, pl = drive("Delete report.csv.", [call("test.e2e_delete", DEL), done("Deleted report.csv.")])
    check("B2/DONE: a step that succeeded but reported nothing does not establish a deletion either",
          not claims_deleted(res.summary), res.summary)


# ================================================================================
# C — failure is communicated, never turned into success
# ================================================================================


def section_c() -> None:
    scenario("C: permission denied — the failure is what the user hears")
    reset()
    world(delete=[R("Delete failed: permission denied.", ok=False)])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL), done("I deleted report.csv.")],
                    answer="I deleted report.csv.", subgoals=subs)
    check("C/SUB: only failed steps -> the run reports failure honestly", not res.ok and res.stopped == "failure", f"{res.ok} {res.stopped}")
    check("C/SUB: the answer carries the real failure", "permission denied" in low(res.summary), res.summary)
    check("C/SUB: no deletion claim", not claims_deleted(res.summary), res.summary)

    reset()
    world(find=[R("Found report.csv in Downloads.")], delete=[R("Delete failed: permission denied.", ok=False)])
    res, pl = drive("Find report.csv and delete it.",
                    [call("test.e2e_find", {"name": "report.csv"}), call("test.e2e_delete", DEL),
                     done("I found report.csv in Downloads and deleted it.")])
    check("C2/DONE: a planner summary claiming a deletion the tool refused is not passed through",
          not claims_deleted(res.summary), res.summary)
    check("C2/DONE: the real failure reaches the user", "permission denied" in low(res.summary), res.summary)

    reset()
    world(find=[R("Found report.csv in Downloads.")], delete=[R("Delete failed: permission denied.", ok=False)])
    honest = "I found report.csv in Downloads but couldn't delete it: permission denied."
    res, pl = drive("Find report.csv and delete it.",
                    [call("test.e2e_find", {"name": "report.csv"}), call("test.e2e_delete", DEL), done(honest)])
    check("C3/DONE: an honest failure report is not rewritten or second-guessed", res.summary == honest, res.summary)


# ================================================================================
# D / E — several results in one goal
# ================================================================================

GOAL_ROWS = "Find report.csv and tell me how many rows it has."


def _rows_world() -> None:
    world(find=[R("Found report.csv in Documents.")], rows=[R("report.csv has 42 rows.")])


def _rows_subs() -> list[Subgoal]:
    return [sg("Find report.csv"), sg("Count the rows in report.csv"), sg("Tell me the row count", "answer")]


ROWS_CALLS = [call("test.e2e_find", {"name": "report.csv"}), call("test.e2e_rows", {"path": "report.csv"})]


def section_d() -> None:
    scenario("D: two results across two steps — the final answer carries both")
    reset()
    _rows_world()
    res, pl = drive(GOAL_ROWS, ROWS_CALLS, answer="I found report.csv in Documents, and it has 42 rows.", subgoals=_rows_subs())
    check("D/SUB: both steps ran, then one composed answer", ran() == ["find", "rows"] and pl.answer_calls == 1, f"{ran()} {pl.answer_calls}")
    check("D/SUB: the answer has both the location and the count",
          "Documents" in res.summary and "42" in res.summary, res.summary)
    check("D/SUB: complete + grounded -> untouched",
          res.summary == "I found report.csv in Documents, and it has 42 rows.", res.summary)

    reset()
    _rows_world()
    res, pl = drive(GOAL_ROWS, ROWS_CALLS, answer="I found report.csv in Documents.", subgoals=_rows_subs())
    check("D2/SUB: a composed answer that silently drops the count gets the real result appended (Phase 24.3 end to end)",
          "42" in res.summary and "Documents" in res.summary, res.summary)
    check("D2/SUB: ...as an addition to the answer, never an 'insufficient' rewrite",
          res.summary.startswith("I found report.csv in Documents.") and not failed_closed(res.summary), res.summary)

    reset()
    _rows_world()
    res, pl = drive(GOAL_ROWS, ROWS_CALLS + [done("Found report.csv in Documents; it has 42 rows.")])
    check("D3/DONE: a planner summary with both results is untouched",
          res.summary == "Found report.csv in Documents; it has 42 rows.", res.summary)


def section_e() -> None:
    scenario("E: two results requested, one available — nothing invented, nothing implied")
    goal = "Find report.csv and tell me its row count and its file size."
    subs = lambda: [sg("Count the rows in report.csv"), sg("Tell me the row count and file size", "answer")]  # noqa: E731
    rows_call = [call("test.e2e_rows", {"path": "report.csv"})]

    reset()
    world(rows=[R("report.csv has 42 rows.")])
    res, pl = drive(goal, rows_call, answer="report.csv has 42 rows and is 1500 KB.", subgoals=subs())
    check("E1/SUB: a fabricated 3+ digit size is rejected", "1500" not in res.summary, res.summary)
    check("E1/SUB: ...and what is really known is still said", "42" in res.summary, res.summary)

    reset()
    world(rows=[R("report.csv has 42 rows.")])
    partial = "report.csv has 42 rows. I don't have its file size."
    res, pl = drive(goal, rows_call, answer=partial, subgoals=subs())
    check("E2/SUB: an honest partial answer passes untouched", res.summary == partial, res.summary)
    check("E2/SUB: it does not imply the size was found (no number for it)", not re.search(r"\b\d{2,}\s*(?:kb|mb|bytes)\b", low(res.summary)))

    reset()
    world(rows=[R("report.csv has 42 rows.")])
    res, pl = drive(goal, rows_call + [done("report.csv has 42 rows and is 1500 KB.")])
    check("E3/DONE: the same fabricated size in a planner summary is rejected too", "1500" not in res.summary, res.summary)


# ================================================================================
# F / G / H — evidence that changes during the run
# ================================================================================


def section_f() -> None:
    scenario("F: attempt -> intermediate status -> final success — the final concrete result is what counts")
    reset()
    world(begin=[R("Attempting to delete report.csv.")], progress=[R("Still working on it...")],
          finish=[R("Deleted report.csv successfully.")])
    res, pl = drive("Delete report.csv.",
                    [call("test.e2e_begin", DEL), call("test.e2e_progress"), call("test.e2e_finish", DEL),
                     done("I deleted report.csv.")])
    check("F/DONE: all three steps ran", ran() == ["begin", "progress", "finish"], str(ran()))
    check("F/DONE: the final answer reports the completed deletion, untouched",
          res.summary == "I deleted report.csv." and res.ok, res.summary)

    reset()
    world(begin=[R("Attempting to delete report.csv.")], progress=[R("Still working on it...")],
          finish=[R("Deleted report.csv successfully.")])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_begin", DEL), call("test.e2e_progress"), call("test.e2e_finish", DEL)],
                    answer="I deleted report.csv.", subgoals=subs)
    check("F2/SUB: an attempt line alone does not close the subgoal — the run went on to the FINAL result",
          ran() == ["begin", "progress", "finish"], str(ran()))
    check("F2/SUB: the composed answer reports the completed deletion", res.summary == "I deleted report.csv.", res.summary)


def section_g() -> None:
    scenario("G: success followed by a later failure — the earlier success is not the final state")
    reset()
    world(delete=[R("Deleted report.csv."), R("Delete failed: permission denied on report.csv.", ok=False)])
    res, pl = drive("Delete report.csv.",
                    [call("test.e2e_delete", DEL), call("test.e2e_delete", {**DEL, "force": True}),
                     done("I deleted report.csv.")])
    check("G1/DONE: the same operation later failed -> the answer no longer claims the deletion",
          not claims_deleted(res.summary), res.summary)
    check("G1/DONE: the later failure is what the user is told", "permission denied" in low(res.summary), res.summary)

    reset()
    world(delete=[R("Deleted report.csv.")],
          check=[R("Could not confirm the deletion: report.csv is still there.", ok=False)])
    res, pl = drive("Delete report.csv.",
                    [call("test.e2e_delete", DEL), call("test.e2e_check", DEL), done("I deleted report.csv.")])
    check("G2/DONE: a different tool contradicting the success is a conflict — never silently resolved",
          "still there" in low(res.summary) and "report.csv" in res.summary, res.summary)
    check("G2/DONE: ...and neither side is dropped (the success is still stated)", claims_deleted(res.summary), res.summary)


def section_h() -> None:
    scenario("H: failure -> retry -> success — the final state is the success")
    reset()
    world(delete=[R("Delete failed: permission denied.", ok=False), R("Deleted report.csv successfully.")])
    res, pl = drive("Delete report.csv.",
                    [call("test.e2e_delete", DEL), call("test.e2e_delete", {**DEL, "force": True}),
                     done("I deleted report.csv after retrying.")])
    check("H/DONE: both attempts ran", ran() == ["delete", "delete"], str(ran()))
    check("H/DONE: the answer reports the success, untouched",
          res.summary == "I deleted report.csv after retrying." and res.ok, res.summary)

    reset()
    world(delete=[R("Delete failed: permission denied.", ok=False), R("Deleted report.csv successfully.")])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL), call("test.e2e_delete", {**DEL, "force": True})],
                    answer="I deleted report.csv on the second try.", subgoals=subs)
    check("H2/SUB: the composed answer reports the retried success, untouched",
          res.summary == "I deleted report.csv on the second try." and pl.answer_calls == 1, res.summary)
    check("H2/SUB: the superseded failure is not re-attached to the answer", "permission denied" not in low(res.summary), res.summary)


# ================================================================================
# I — uncertain evidence is never an established fact
# ================================================================================


def section_i() -> None:
    scenario("I: the only support is uncertain — the claim is not presented as fact")
    reset()
    world(delete=[R("Deleted report.csv.", uncertain=True)])
    res, pl = drive("Delete report.csv.", [call("test.e2e_delete", DEL), done("I deleted report.csv.")])
    check("I/DONE: an uncertain-only 'deleted' is not stated as fact", not claims_deleted(res.summary), res.summary)
    check("I/DONE: the user is told there isn't enough confirmed evidence", failed_closed(res.summary), res.summary)

    reset()
    world(find=[R("Found notes.txt in Documents.")], delete=[R("Deleted report.csv.", uncertain=True)])
    res, pl = drive("Find notes.txt and delete report.csv.",
                    [call("test.e2e_find", {"name": "notes.txt"}), call("test.e2e_delete", DEL),
                     done("I found notes.txt and deleted report.csv.")])
    check("I2/DONE: real evidence for one part does not launder the uncertain part", not claims_deleted(res.summary), res.summary)
    check("I2/DONE: what really was found is still reported", "notes.txt" in res.summary, res.summary)

    reset()
    world(delete=[R("Deleted report.csv.", uncertain=True)])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.e2e_delete", DEL)], answer="I deleted report.csv.", subgoals=subs)
    check("I3/SUB: uncertain evidence never satisfies the acquisition subgoal, so no answer is composed from it",
          pl.answer_calls == 0, str(pl.answer_calls))


# ================================================================================
# J / K — numbers and filenames the evidence never produced
# ================================================================================


def section_jk() -> None:
    scenario("J: an invented 3+ digit number is rejected on both routes")
    reset()
    world(rows=[R("report.csv has 42 rows.")])
    subs = [sg("Count the rows in report.csv"), sg("Tell me the row count", "answer")]
    res, pl = drive("Count the rows in report.csv and tell me the row count.",
                    [call("test.e2e_rows", {"path": "report.csv"})], answer="report.csv has 4200 rows.", subgoals=subs)
    check("J/SUB: the invented number is gone", "4200" not in res.summary, res.summary)
    check("J/SUB: the real number is what the user gets", "42" in res.summary, res.summary)
    reset()
    world(rows=[R("report.csv has 42 rows.")])
    res, pl = drive("How many rows does report.csv have?",
                    [call("test.e2e_rows", {"path": "report.csv"}), done("report.csv has 4200 rows.")])
    check("J/DONE: same for a planner summary", "4200" not in res.summary and "42" in res.summary, res.summary)

    scenario("K: an invented filename is rejected on both routes")
    reset()
    world(find=[R("Found report.csv in Documents.")])
    subs = [sg("Find report.csv"), sg("Tell me what you found", "answer")]
    res, pl = drive("Find report.csv and tell me what you found.",
                    [call("test.e2e_find", {"name": "report.csv"})], answer="I found budget.xlsx in Documents.", subgoals=subs)
    check("K/SUB: the invented filename is gone", "budget.xlsx" not in res.summary, res.summary)
    check("K/SUB: the real file is what the user gets", "report.csv" in res.summary, res.summary)
    reset()
    world(find=[R("Found report.csv in Documents.")])
    res, pl = drive("Find report.csv.", [call("test.e2e_find", {"name": "report.csv"}), done("I found budget.xlsx in Documents.")])
    check("K/DONE: same for a planner summary", "budget.xlsx" not in res.summary and "report.csv" in res.summary, res.summary)


# ================================================================================
# L — ordinary answers are not punished
# ================================================================================


def section_l() -> None:
    scenario("L: ordinary conversational and simple-tool answers are not made defensive")
    reset()
    world()
    chat = "I'm doing well, thank you for asking! What can I do for you?"
    res, pl = drive("Hi FRIDAY, how are you today?", [done(chat)])
    check("L1: a plain conversational reply (no tool, no evidence) is returned untouched", res.summary == chat, res.summary)
    check("L1: ...and is not turned into an 'insufficient evidence' message", not failed_closed(res.summary), res.summary)

    reset()
    world(time=[R("It's 3:45 PM.")])
    res, pl = drive("What time is it?", [call("test.e2e_time"), done("It's 3:45 PM.")])
    check("L2: a simple tool answer that restates the evidence is untouched", res.summary == "It's 3:45 PM.", res.summary)

    reset()
    world(open=[R("Opening Notepad.")])
    res, pl = drive("Open Notepad.", [call("test.e2e_open", {"name": "Notepad"}), done("I opened Notepad.")])
    check("L3: FRIDAY's real 'Opening Notepad.' result supports 'I opened Notepad.' (not treated as a mere attempt)",
          res.summary == "I opened Notepad.", res.summary)

    reset()
    world(open=[R("Opening Notepad.")])
    res, pl = drive("Open Notepad.", [call("test.e2e_open", {"name": "Notepad"}), done("Notepad is open.")])
    check("L4: a paraphrase with no claim verb is untouched", res.summary == "Notepad is open.", res.summary)

    reset()
    world(find=[R("TODO.md lists: add rate limiting, add audit logging.")])
    listed = "Two outstanding items found: add rate limiting and add audit logging."
    res, pl = drive("Find out what needs attention in my project.", [call("test.e2e_find", {"name": "TODO.md"}), done(listed)])
    check("L6/DONE: a planner summary that says 'found' over what a read tool merely LISTED is untouched "
          "(the read verbs need no echo on this route; found by re-running the Phase 17 investigative suite)",
          res.summary == listed, res.summary)

    reset()
    world(find=[R("TODO.md lists: add rate limiting, add audit logging.")])
    res, pl = drive("Find out what needs attention in my project.",
                    [call("test.e2e_find", {"name": "TODO.md"}), done("I found NOTES.md with 900 open items.")])
    check("L7/DONE: ...but the same 'found' wording cannot smuggle in a filename or number the tool never produced",
          "NOTES.md" not in res.summary and "900" not in res.summary, res.summary)

    reset()
    world(time=[R("It's 3:45 PM.")])
    subs = [sg("Check the time"), sg("Tell me if it is afternoon", "answer")]
    res, pl = drive("Check the time and tell me if it is afternoon.", [call("test.e2e_time")],
                    answer="It's 3:45 PM, so yes, it's the afternoon.", subgoals=subs)
    check("L5/SUB: an ordinary composed answer is untouched", res.summary == "It's 3:45 PM, so yes, it's the afternoon.", res.summary)


# ================================================================================
# M — Phase 17 and Phase 23 keep doing their own jobs, and Phase 24 does not fight them
# ================================================================================


def section_m() -> None:
    scenario("M: Phase 17 overclaim protection, Phase 23 stopping, and the Phase 24 kill switch")
    reset()
    world(find=[R("Found report.csv in Documents.")])
    subs = [sg("Find report.csv"), sg("Tell me what you found", "answer")]
    res, pl = drive("Find report.csv and tell me what you found.",
                    [call("test.e2e_find", {"name": "report.csv"})],
                    answer="I found report.csv in Documents. That is the cause.", subgoals=subs)
    check("M1: Phase 17 still hedges an unsupported 'is the cause' on the answer path", "may be the cause" in res.summary, res.summary)
    check("M1: ...and Phase 24 leaves the (now grounded) hedged answer alone — no second rewrite",
          not failed_closed(res.summary) and "report.csv" in res.summary, res.summary)

    reset()
    world(find=[R("Found report.csv in Documents.")])
    res, pl = drive("Find report.csv and tell me what you found.",
                    [call("test.e2e_find", {"name": "report.csv"})], answer="I found report.csv in Documents.",
                    subgoals=[sg("Find report.csv"), sg("Tell me what you found", "answer")])
    check("M2: a legitimate answer is not rejected merely because an earlier layer also looked at it",
          res.summary == "I found report.csv in Documents.", res.summary)
    check("M2: Phase 23 stopped on evidence without a second tool-choice turn", pl.decision_calls == 1 and pl.answer_calls == 1,
          f"{pl.decision_calls}/{pl.answer_calls}")

    reset()
    CFG.planner.answer_grounding_guard = False
    world(rows=[R("report.csv has 42 rows.")])
    subs = [sg("Count the rows in report.csv"), sg("Tell me the row count", "answer")]
    res, pl = drive("Count the rows in report.csv and tell me the row count.",
                    [call("test.e2e_rows", {"path": "report.csv"})], answer="report.csv has 4200 rows.", subgoals=subs)
    check("M3: with answer_grounding_guard off the composed answer is the pre-Phase-24 text (the switch is real)",
          res.summary == "report.csv has 4200 rows.", res.summary)
    reset()
    CFG.planner.answer_grounding_guard = False
    world(rows=[R("report.csv has 42 rows.")])
    res, pl = drive("How many rows does report.csv have?",
                    [call("test.e2e_rows", {"path": "report.csv"}), done("report.csv has 4200 rows.")])
    check("M3: ...and the same switch governs the planner-summary route", res.summary == "report.csv has 4200 rows.", res.summary)
    reset()


# ================================================================================
# P — the same protections through the real `plan.run` skill (what a user actually receives)
# ================================================================================

PWORLD: dict[str, SkillResult] = {}
PCALLS: list[str] = []


def _register_fixtures() -> None:
    """`test.e2e_p*` skills: registered for real, so `plan.run` sees them in its tool catalog and
    runs them through the real executor, permission tier, intent guard and post-condition check."""
    from friday import verify

    @skill(name="test.e2e_pdelete", tier="L1", action="delete", description="delete a named file (Phase 24.5 fixture)")
    def _pdelete(path: str = "report.csv") -> SkillResult:
        PCALLS.append("pdelete")
        return PWORLD["pdelete"]

    # a state-changing step is read back (Phase 22): declare the fixture's effect so a verified
    # deletion is not decorated with "couldn't verify" noise that has nothing to do with Phase 24
    verify.register("test.e2e_pdelete", verify.Verifier(
        lambda a, d, b: [verify.Check("fixture effect", True, "the fixture ran", "the fixture ran")]
    ))

    @skill(name="test.e2e_prows", tier="L0", description="count the rows of a named file (Phase 24.5 fixture)")
    def _prows(path: str = "report.csv") -> SkillResult:
        PCALLS.append("prows")
        return PWORLD["prows"]


async def _plan_run(goal: str, decisions: list[str]):
    PCALLS.clear()
    planner = RoutedPlanner(decisions)
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": goal}, actor="text")
    return r, planner


def plan_run(goal: str, decisions: list[str]):
    return asyncio.run(_plan_run(goal, decisions))


def section_p() -> None:
    scenario("P: through the real plan.run skill — the SkillResult.speech the user hears")
    saved_observer = CFG.desktop_observer.enabled
    CFG.desktop_observer.enabled = False
    try:
        reset()
        PWORLD.update(pdelete=R("Deleted report.csv successfully."))
        r, pl = plan_run("Delete report.csv.", [call("test.e2e_pdelete", DEL), done("I deleted report.csv.")])
        check("P1: a real successful deletion is reported, untouched", r.ok and r.speech.startswith("I deleted report.csv."), r.speech)
        check("P1: the run really executed the fixture once, through the executor", PCALLS == ["pdelete"], str(PCALLS))

        reset()
        PWORLD.update(pdelete=R("Delete failed: permission denied.", ok=False))
        r, pl = plan_run("Delete report.csv.", [call("test.e2e_pdelete", DEL), done("I deleted report.csv.")])
        check("P2: a refused deletion is reported as a failure", not r.ok and "permission denied" in low(r.speech), f"{r.ok} {r.speech}")
        check("P2: ...and never as a completed one", not claims_deleted(r.speech), r.speech)
        rec = goals_mod.get(r.data["goal_id"])
        check("P2: the Goal row is not SUCCEEDED", rec is not None and rec.status is not goals_mod.GoalStatus.SUCCEEDED)

        reset()
        PWORLD.update(pdelete=R("Attempting to delete report.csv."))
        r, pl = plan_run("Delete report.csv.", [call("test.e2e_pdelete", DEL), done("I deleted report.csv.")])
        check("P3: an attempt-only result never becomes 'I deleted report.csv.'", not claims_deleted(r.speech), r.speech)
        check("P3: the user is told the claim isn't confirmed", failed_closed(r.speech), r.speech)

        reset()
        PWORLD.update(pdelete=R("Deleted report.csv.", uncertain=True))
        r, pl = plan_run("Delete report.csv.", [call("test.e2e_pdelete", DEL), done("I deleted report.csv.")])
        check("P4: an uncertain-only result is not presented as an established deletion", not claims_deleted(r.speech), r.speech)

        reset()
        r, pl = plan_run("Hi FRIDAY, how are you?", [done("I'm doing well, thank you!")])
        check("P5: a conversational reply keeps its own words", "I'm doing well, thank you!" in r.speech, r.speech)
        check("P5: ...and Phase 24 adds no 'insufficient evidence' message to it", not failed_closed(r.speech), r.speech)

        reset()
        PWORLD.update(prows=R("report.csv has 42 rows."))
        r, pl = plan_run("How many rows does report.csv have?", [call("test.e2e_prows", {"path": "report.csv"}), done("report.csv has 4200 rows.")])
        check("P6: a question answered by the discovery pass never surfaces an invented number",
              "4200" not in r.speech and "42" in r.speech, r.speech)
    finally:
        CFG.desktop_observer.enabled = saved_observer
        reset()


def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    try:
        with store.use_temp_db():
            section_a()
            section_b()
            section_c()
            section_d()
            section_e()
            section_f()
            section_g()
            section_h()
            section_i()
            section_jk()
            section_l()
            section_m()
            section_p()
    finally:
        CFG.permissions.overrides = saved
    return H.finish("Phase 24.5 — end-to-end grounded-answer reliability", time.perf_counter() - t0,
                    min_assertions=60, min_scenarios=13)


if __name__ == "__main__":
    sys.exit(main())
