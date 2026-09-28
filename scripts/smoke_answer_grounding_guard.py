"""Phase 24.1/24.2 — deterministic scorecard for ANSWER-FROM-EVIDENCE GROUNDING.

Phase 23.0 gave `Orchestrator._answer_from_evidence` real, bounded evidence and made
it structurally incapable of calling a tool — but nothing checked its OWN reply for
claims the evidence never actually produced. This suite pins the Phase 24.1 fix: a
second, independent, deterministic guard on that reply's TEXT
(`friday.intelligence.discovery.ground_answer`) — never another model call, never a
retry loop. Covers exactly the brief's own test list, both as direct unit checks on
the guard function and end to end through the real `Orchestrator.run_goal`:

  A  evidence contains the requested fact -> the answer is accepted unchanged
  B  evidence does not contain the requested fact -> the answer becomes an explicit
     insufficient/uncertain statement, never a guess
  C  the composer attempts to introduce an unsupported fact (a fabricated number it
     invents on top of otherwise-real evidence) -> the unsupported content is
     rejected/prevented, never repeated in the fallback
  D  the existing Phase 23.0 compound "read X and tell me Y" flow still passes with
     the new guard on (default)
  E  the existing Phase 23.0 "check X, tell me Y, then delete Z" safety case still
     does NOT skip the real (destructive) subgoal after a mid-list answer
  F  Phase 24.2 — broadened claim detection (created/changed/deleted/opened/updated
     resources; retrieved/found/searched information; multi-observation combination;
     tool-name-alone and ordinary-conversational-wording false positives); see
     `section_f` for the brief's own 12-scenario list mapped one to one.

  G  Phase 24.3 — goal-aware completeness (a grounded answer must not silently omit an
     important, goal-relevant result); see `section_g`.
  H  Phase 24.4 — evidence prioritization (attempt vs. result, later-result precedence,
     preserved cross-tool conflicts, relevance/conclusiveness over recency); see
     `section_h` for the brief's own 12-scenario list mapped one to one.

Entirely deterministic: scripted model replies through the existing `llm.get_provider`
seam (`phase21_common.ScriptedPlanner`), a recording fake tool world (no real
registry skill, no real side effect). No Ollama.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phase21_common as H  # noqa: E402
from phase21_common import ScriptedPlanner, call, check, done, scenario  # noqa: E402

from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.registry import SkillResult  # noqa: E402

# -- a recording tool world: nothing real ever runs ----------------------------------

CALLS: list[tuple[str, dict]] = []
WORLD = {"readme.md": "FRIDAY uses Python, FastAPI, SQLite and Whisper."}

SPECS = [
    ToolSpec(name="test.ag_read", description="read a named source", tier="L0", params="path (str)", action="read"),
    ToolSpec(name="test.ag_weather", description="the weather report", tier="L0", params="", action="read"),
    ToolSpec(name="test.ag_disk", description="disk space", tier="L0", params="", action="read"),
    ToolSpec(name="test.ag_delete_temp", description="a real consequential subgoal that must still run when it follows a mid-list answer subgoal", tier="L2", params="", action="delete"),
    # Phase 24.2 section F: a retrieval-shaped tool, end to end -- its speech deliberately
    # uses "Located" rather than "found" (worded differently from the composer's reply)
    # so the end-to-end scenario also exercises the synonym/wording-difference matching.
    ToolSpec(name="test.ag_search", description="search for a named resource", tier="L0", params="query (str)", action="read"),
]


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    if tool == "test.ag_read":
        path = str(args.get("path", ""))
        content = WORLD.get(path, "")
        if not content:
            return SkillResult(speech=f"There's no file at {path}.", ok=False)
        return SkillResult(speech=f"{path} has {len(content)} characters.", data={"path": path, "content": content})
    if tool == "test.ag_weather":
        return SkillResult(speech="It's sunny and 72 degrees.", data={"conditions": "sunny", "temp_f": 72})
    if tool == "test.ag_disk":
        return SkillResult(speech="Disk has 4 percent free space.", data={"free_pct": 4})
    if tool == "test.ag_delete_temp":
        return SkillResult(speech="Deleted the temp files.")
    if tool == "test.ag_search":
        return SkillResult(speech="Located the invoice from March in Downloads.", data={"query": args.get("query"), "found": True})
    raise KeyError(f"unknown test tool: {tool}")


def sg(desc: str, kind: str = "acquisition") -> Subgoal:
    return Subgoal(id=str(uuid.uuid4()), description=desc, kind=kind)


async def drive(goal: str, subgoals: list[Subgoal], replies: list[str], *, max_steps: int = 8):
    """Scripted planner -> the real Orchestrator.run_goal -> a recording tool world."""
    CALLS.clear()
    planner = ScriptedPlanner(replies)
    orch = Orchestrator(
        tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=planner,
        tool_specs=SPECS, max_steps=max_steps,
    )
    events: list[tuple[str, dict]] = []

    async def on_event(ev) -> None:
        if ev.topic.startswith("orchestrator."):
            events.append((ev.topic, dict(ev.data)))

    BUS.subscribe("*", on_event)
    try:
        res = await orch.run_goal(goal, subgoals=subgoals)
    finally:
        BUS.unsubscribe("*", on_event)
    return res, planner, events


def reset() -> None:
    CALLS.clear()
    CFG.planner.structured_output = False
    CFG.planner.subgoal_evidence_advance = True
    CFG.planner.answer_from_evidence = True
    CFG.planner.answer_grounding_guard = True


# ================================================================================
# A — evidence contains the requested fact -> accepted unchanged
# ================================================================================


def section_a() -> None:
    scenario("A1: direct unit check — an answer fully backed by real evidence passes through unchanged")
    obs = [Observation(
        PlanStep("test.ag_read", {"path": "readme.md"}, subgoal="x"), True,
        "readme.md has 56 characters.", data={"path": "readme.md", "content": WORLD["readme.md"]},
    )]
    answer = "FRIDAY uses Python, FastAPI, SQLite and Whisper."
    check("A1: an evidence-backed answer is returned verbatim",
          discovery.ground_answer("what technologies does it use", answer, obs) == answer)

    scenario("A2: direct unit check — a number that IS in the evidence is never flagged")
    obs_num = [Observation(PlanStep("test.ag_disk", {}, subgoal="x"), True, "Disk has 412 MB free.", data={"free_mb": 412})]
    check("A2: a genuinely evidence-backed number passes through",
          discovery.ground_answer("how much disk space is free", "You have 412 MB free.", obs_num)
          == "You have 412 MB free.")

    scenario("A3: end to end — 'Read the README and tell me what technologies are used' answered correctly")
    reset()
    goal = "Read the README file and tell me what technologies are used."
    sg0, sg1 = sg("Read the README file", "acquisition"), sg("Tell me what technologies are used", "answer")
    replies = [call("test.ag_read", {"path": "readme.md"}), "FRIDAY uses Python, FastAPI, SQLite and Whisper."]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("A3: the run completed ok", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("A3: the grounded, evidence-backed answer is used verbatim (the guard did not alter it)",
          res.summary == "FRIDAY uses Python, FastAPI, SQLite and Whisper.", res.summary)
    check("A3: an orchestrator.evidence_stop event still fired", any(t == "orchestrator.evidence_stop" for t, _ in events))


# ================================================================================
# B — evidence does not contain the requested fact -> insufficient, never a guess
# ================================================================================


def section_b() -> None:
    scenario("B1: direct unit check — no real evidence at all -> an explicit insufficient statement")
    result = discovery.ground_answer("what is the launch code", "The launch code is ZEBRA-1234.", [])
    check("B1: the result names the evidence as insufficient", "insufficient" in result.lower(), result)
    check("B1: the fabricated value never appears in the fallback", "ZEBRA-1234" not in result, result)

    scenario("B2: direct unit check — only uncertain/failed evidence -> insufficient, not the fabricated claim")
    unc = [Observation(PlanStep("test.ag_read", {}, subgoal="x"), True, "maybe something", data={"uncertain": True})]
    result2 = discovery.ground_answer("what is the launch code", "It is definitely 9482.", unc)
    check("B2: an uncertain-only observation is treated as no real evidence", "insufficient" in result2.lower(), result2)
    check("B2: the fabricated number is not repeated", "9482" not in result2, result2)
    failed = [Observation(PlanStep("test.ag_read", {}, subgoal="x"), False, "no such file")]
    result3 = discovery.ground_answer("what is the launch code", "It is definitely 9482.", failed)
    check("B2b: a failed-only observation is also treated as no real evidence", "insufficient" in result3.lower(), result3)

    scenario("B3: end to end — real evidence exists but not about the requested fact -> insufficient, honestly")
    reset()
    goal = "Read the weather report and tell me the launch code."
    sg0, sg1 = sg("Read the weather report", "acquisition"), sg("Tell me the launch code", "answer")
    replies = [call("test.ag_weather", {}), "The launch code is ZEBRA-7731."]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("B3: the run still completes (the guard only changes the wording, not the control flow)",
          res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("B3: the fabricated launch code never reaches the final answer", "ZEBRA-7731" not in res.summary and "7731" not in res.summary, res.summary)
    check("B3: the final answer says the evidence was insufficient", "insufficient" in res.summary.lower(), res.summary)
    check("B3: the real (irrelevant) evidence was still gathered exactly once", CALLS == [("test.ag_weather", {})], str(CALLS))


# ================================================================================
# C — composer introduces an unsupported fact on top of otherwise-real evidence
# ================================================================================


def section_c() -> None:
    scenario("C1: direct unit check — a fabricated number tacked onto an otherwise-grounded answer is caught")
    obs = [Observation(
        PlanStep("test.ag_read", {"path": "readme.md"}, subgoal="x"), True,
        "readme.md has 56 characters.", data={"path": "readme.md", "content": WORLD["readme.md"]},
    )]
    answer = "FRIDAY uses Python and FastAPI. It also deleted 9999 temp files just now."
    result = discovery.ground_answer("what does the readme say", answer, obs)
    check("C1: the fabricated number is not present in the result", "9999" not in result, result)
    check("C1: the result explicitly says it's insufficient/unconfirmed for that part", "insufficient" in result.lower(), result)
    check("C1: the real, evidence-backed sentence alone (no fabricated add-on) is untouched",
          discovery.ground_answer("what does the readme say", "FRIDAY uses Python and FastAPI.", obs)
          == "FRIDAY uses Python and FastAPI.")

    scenario("C2: direct unit check — a fabricated completed-action claim with no matching real action is caught")
    action_answer = "I deleted readme.md as you asked."
    result2 = discovery.ground_answer("delete readme.md", action_answer, obs)  # obs only ever READ it, never deleted
    check("C2: the false 'deleted' claim is not passed through as-is", result2 != action_answer, result2)
    check("C2: the fallback names the evidence as insufficient", "insufficient" in result2.lower(), result2)

    scenario("C3: end to end — the composer's reply fabricates an unsupported fact on top of real evidence")
    reset()
    goal = "Read the README file and tell me what technologies are used."
    sg0, sg1 = sg("Read the README file", "acquisition"), sg("Tell me what technologies are used", "answer")
    hostile_answer = "FRIDAY uses Python and FastAPI. It also deleted 8675309 old log files."
    replies = [call("test.ag_read", {"path": "readme.md"}), hostile_answer]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("C3: the run still completes", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("C3: the fabricated number never reaches the final answer", "8675309" not in res.summary, res.summary)
    check("C3: no destructive tool was ever actually called from the answer path",
          all(t != "test.ag_delete_temp" for t, _ in CALLS), str(CALLS))
    check("C3: only the one real (read) tool call happened", CALLS == [("test.ag_read", {"path": "readme.md"})], str(CALLS))

    scenario("C4: mutation check — turning the guard off restores the Phase 23.0 verbatim behavior")
    reset()
    CFG.planner.answer_grounding_guard = False
    goal2 = "Read the README file and tell me what technologies are used."
    sg0b, sg1b = sg("Read the README file", "acquisition"), sg("Tell me what technologies are used", "answer")
    res2, pl2, events2 = asyncio.run(drive(goal2, [sg0b, sg1b], [call("test.ag_read", {"path": "readme.md"}), hostile_answer]))
    check("C4: with the guard off, the unsupported number IS present verbatim (proves the guard, not something else, caught it)",
          "8675309" in res2.summary, res2.summary)
    reset()


# ================================================================================
# D — existing Phase 23.0 compound read->answer flow still passes with the guard on
# ================================================================================


def section_d() -> None:
    scenario("D: Phase 23.0's own 'read X and tell me Y' end-to-end flow, unaffected by the new guard (default ON)")
    reset()
    goal = "Read the README file and tell me what technologies are used."
    sg0, sg1 = sg("Read the README file", "acquisition"), sg("Tell me what technologies are used", "answer")
    replies = [call("test.ag_read", {"path": "readme.md"}), "FRIDAY uses Python, FastAPI, SQLite and Whisper."]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("D: exactly one real tool call (the read)", CALLS == [("test.ag_read", {"path": "readme.md"})], str(CALLS))
    check("D: the run completed ok", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("D: both subgoals credited SUCCEEDED", sg0.status == SubgoalStatus.SUCCEEDED and sg1.status == SubgoalStatus.SUCCEEDED)
    check("D: the composed answer is used, still containing the real evidence-backed facts",
          "Python" in res.summary and "FastAPI" in res.summary, res.summary)
    check("D: exactly 2 model calls total (1 decision + 1 answer)", pl.calls == 2, pl.calls)
    check("D: an orchestrator.evidence_stop event fired", any(t == "orchestrator.evidence_stop" for t, _ in events))


# ================================================================================
# E — existing "check X, tell me Y, then delete Z" safety case still runs the delete
# ================================================================================


def section_e() -> None:
    scenario("E: a mid-list ANSWER subgoal never ends the run early, and the real destructive subgoal after it still runs")
    reset()
    goal = "Check my disk space, tell me if I'm running low, and delete the temp files if so."
    sg_check = sg("Check disk space", "acquisition")
    sg_tell = sg("Tell me if I'm running low", "answer")
    sg_delete = sg("Delete the temp files if so", "acquisition")
    check("E precondition: the middle subgoal really does classify as answer",
          sg_tell.kind == "answer")
    replies = [
        call("test.ag_disk", {}),
        json.dumps({"action": "call", "tool": "test.ag_delete_temp", "args": {}, "subgoal_index": 2}),
        done("Disk was low, so I deleted the temp files."),
    ]
    res, pl, events = asyncio.run(drive(goal, [sg_check, sg_tell, sg_delete], replies))
    check("E: the middle answer subgoal did NOT end the run early (no evidence_stop)",
          not any(t == "orchestrator.evidence_stop" for t, _ in events))
    check("E: the real, destructive subgoal actually RAN -- never just credited unexecuted",
          any(t == "test.ag_delete_temp" for t, _ in CALLS), str(CALLS))
    check("E: the goal only completes once that real work actually happened", res.ok and res.stopped == "completed")
    check("E: all three subgoals are SUCCEEDED only because each really ran (or was legitimately skipped by the model's own choice)",
          [s.status for s in (sg_check, sg_tell, sg_delete)] == [SubgoalStatus.SUCCEEDED] * 3)


# ================================================================================
# F — Phase 24.2: broadened claim detection. Twelve scenarios, mapped 1:1 to the
#     brief's own §6 test list (numbered in each scenario title below).
# ================================================================================


def section_f() -> None:
    scenario("F1 (brief test 1): supported completed action — a previously-uncovered verb ('changed') backed by real evidence")
    obs_changed = [Observation(PlanStep("test.ag_settings", {}, subgoal="x"), True, "Changed the notification settings to silent.")]
    answer_changed = "I changed your notification settings to silent."
    check("F1: a grounded 'changed' claim passes through unchanged",
          discovery.ground_answer("change my notification settings", answer_changed, obs_changed) == answer_changed)

    scenario("F2 (brief test 2): unsupported completed action — 'changed' claimed with no matching real evidence")
    obs_unrelated = [Observation(PlanStep("test.ag_weather", {}, subgoal="x"), True, "It's sunny and 72 degrees.")]
    result_f2 = discovery.ground_answer("change my wallpaper", "I changed your wallpaper to the sunset photo.", obs_unrelated)
    check("F2: the unsupported 'changed' claim is not passed through as-is",
          result_f2 != "I changed your wallpaper to the sunset photo.", result_f2)
    check("F2: the fallback names the evidence as insufficient", "insufficient" in result_f2.lower(), result_f2)

    scenario("F3 (brief test 3): supported retrieval/search result — synonymous evidence wording ('Located') still grounds a 'found' claim")
    obs_found = [Observation(PlanStep("test.ag_find_doc", {}, subgoal="x"), True, "Located the meeting notes in the Documents folder.")]
    answer_found = "I found your meeting notes in the Documents folder."
    check("F3: a retrieval claim backed by synonymous evidence passes through unchanged",
          discovery.ground_answer("find the meeting notes", answer_found, obs_found) == answer_found)

    scenario("F4 (brief test 4): unsupported retrieval/search claim — a search that found nothing cannot ground a 'found' claim")
    obs_search_empty = [Observation(PlanStep("email.search", {"query": "invoice"}, subgoal="x"), True, "Searched your inbox for invoices; no matches.")]
    result_f4 = discovery.ground_answer("find the invoice", "I found the invoice from March.", obs_search_empty)
    check("F4: the unfounded 'found' claim is not passed through as-is",
          result_f4 != "I found the invoice from March.", result_f4)
    check("F4: the fallback names the evidence as insufficient", "insufficient" in result_f4.lower(), result_f4)
    check("F4: the fabricated 'March' detail is not repeated in the fallback", "March" not in result_f4, result_f4)

    scenario("F5 (brief test 5): supported factual result assembled from multiple observations — a resource claim plus a count claim, each from a different observation")
    obs_created = Observation(PlanStep("file.create", {"path": "report.csv"}, subgoal="x"), True, "Created report.csv successfully.")
    obs_rows = Observation(PlanStep("file.read", {"path": "report.csv"}, subgoal="x"), True, "report.csv has 256 rows.", data={"rows": 256})
    answer_combined = "I created report.csv and it has 256 rows."
    check("F5: an answer combining facts from two different real observations passes through unchanged",
          discovery.ground_answer("create the report and tell me its size", answer_combined, [obs_created, obs_rows]) == answer_combined)

    scenario("F6 (brief test 6): an unsupported additional fact tacked onto an otherwise fully-grounded, multi-observation answer fails the WHOLE answer closed")
    answer_plus_extra = "I created report.csv, it has 256 rows, and I also sent it to your manager."
    result_f6 = discovery.ground_answer("create the report and tell me its size", answer_plus_extra, [obs_created, obs_rows])
    check("F6: the unsupported 'sent to your manager' claim is not passed through as-is", result_f6 != answer_plus_extra, result_f6)
    check("F6: the unsupported add-on itself is not preserved in the fallback", "manager" not in result_f6, result_f6)
    check("F6: the fallback names the evidence as insufficient", "insufficient" in result_f6.lower(), result_f6)

    scenario("F7 (brief test 7): different wording between observation and final answer — a synonym on the verb still grounds")
    obs_removed = [Observation(PlanStep("file.delete", {"path": "draft.txt"}, subgoal="x"), True, "Removed draft.txt successfully.")]
    answer_deleted = "I deleted draft.txt for you."
    check("F7: 'deleted' (answer) is grounded by 'Removed' (evidence) even though the wording differs",
          discovery.ground_answer("delete the draft", answer_deleted, obs_removed) == answer_deleted)

    scenario("F8 (brief test 8): tool name alone must NOT prove an action succeeded — a plausibly-named tool that ran but confirmed nothing")
    obs_named_only = [Observation(PlanStep("test.ag_delete_file", {}, subgoal="x"), True, "Started the operation.", data={"status": "queued"})]
    result_f8 = discovery.ground_answer("delete the file", "I deleted the file as you asked.", obs_named_only)
    check("F8: a tool NAME containing 'delete' does not, by itself, ground a 'deleted' claim",
          result_f8 != "I deleted the file as you asked.", result_f8)
    check("F8: the fallback names the evidence as insufficient", "insufficient" in result_f8.lower(), result_f8)

    scenario("F9 (brief test 9): ordinary conversational wording must not trigger a false positive")
    obs_status = [Observation(PlanStep("test.ag_status", {}, subgoal="x"), True, "All systems normal.")]
    answer_filler_1 = "I checked and everything looks fine."
    check("F9a: 'checked' as ordinary commentary is never treated as a claim needing evidence",
          discovery.ground_answer("how's everything looking", answer_filler_1, obs_status) == answer_filler_1)
    obs_logs = [Observation(PlanStep("test.ag_logs", {}, subgoal="x"), True, "Logs show low activity today.")]
    answer_filler_2 = "I found this interesting: the logs are unusually quiet today."
    check("F9b: 'found this interesting' (evaluative filler, no concrete object) is not treated as a result claim",
          discovery.ground_answer("what did you notice", answer_filler_2, obs_logs) == answer_filler_2)
    obs_moved = [Observation(PlanStep("file.move", {"path": "x.txt"}, subgoal="x"), True, "Moved x.txt into the archive folder.")]
    answer_filler_3 = "I never deleted the file, just moved it into the archive folder."
    check("F9c: a negated verb ('never deleted') needs no evidence of a deletion that was explicitly denied, and the later, real 'moved' claim it precedes still grounds",
          discovery.ground_answer("archive the file", answer_filler_3, obs_moved) == answer_filler_3)
    answer_filler_4 = "I never deleted the file, but I also created a new backup.xyz today."
    result_f9d = discovery.ground_answer("archive the file", answer_filler_4, obs_moved)
    check("F9d: negation scope does not leak across a clause boundary -- a later, unsupported 'created' claim past the comma is still caught",
          result_f9d != answer_filler_4 and "insufficient" in result_f9d.lower(), result_f9d)

    scenario("F10 (brief test 10): existing Phase 24.1 numeric and filename protection remains intact")
    obs_plain = [Observation(PlanStep("test.ag_count", {}, subgoal="x"), True, "The queue looks busy.")]
    result_f10a = discovery.ground_answer("what's the count", "There are 9999 items in queue.", obs_plain)
    check("F10a: an unsupported 3+ digit number is still caught",
          "insufficient" in result_f10a.lower() and "9999" not in result_f10a, result_f10a)
    obs_plain_file = [Observation(PlanStep("test.ag_list", {}, subgoal="x"), True, "There are a few files here.")]
    result_f10b = discovery.ground_answer("what files are there", "The file secrets.txt is here.", obs_plain_file)
    check("F10b: an unsupported filename is still caught",
          "insufficient" in result_f10b.lower() and "secrets.txt" not in result_f10b, result_f10b)

    scenario("F11 (brief test 11): uncertain/failed observations must not ground a (newly-covered) retrieval claim")
    obs_uncertain = [Observation(PlanStep("test.ag_find", {}, subgoal="x"), True, "Possibly found the file.", data={"uncertain": True})]
    result_f11a = discovery.ground_answer("find the file", "I found the file.", obs_uncertain)
    check("F11a: an uncertain observation is treated as no real evidence for a retrieval claim", "insufficient" in result_f11a.lower(), result_f11a)
    obs_failed = [Observation(PlanStep("test.ag_find", {}, subgoal="x"), False, "search failed")]
    result_f11b = discovery.ground_answer("find the file", "I found the file.", obs_failed)
    check("F11b: a failed observation is also treated as no real evidence for a retrieval claim", "insufficient" in result_f11b.lower(), result_f11b)

    scenario("F12 (brief test 12): multiple valid observations are combinable end to end through the real Orchestrator, not just in the direct unit check")
    reset()
    goal = "Search for the March invoice and tell me where it is."
    sg0, sg1 = sg("Search for the March invoice", "acquisition"), sg("Tell me where it is", "answer")
    replies = [call("test.ag_search", {"query": "invoice"}), "I found the invoice from March in Downloads."]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("F12: the run completed ok", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("F12: the retrieval-verb answer, backed by real evidence with different wording ('Located' vs. 'found'), is used verbatim",
          res.summary == "I found the invoice from March in Downloads.", res.summary)
    check("F12: an orchestrator.evidence_stop event still fired", any(t == "orchestrator.evidence_stop" for t, _ in events))


# ================================================================================
# G — Phase 24.3: goal-aware completeness (grounded-but-incomplete answers). Twelve
#     scenarios, mapped 1:1 to the brief's own §6 (A-D) and §7 (1-12) test lists.
# ================================================================================


def section_g() -> None:
    obs_found = Observation(PlanStep("test.ag_find_report", {"path": "report.csv"}, subgoal="x"), True, "Located report.csv.")
    obs_rows = Observation(PlanStep("test.ag_read", {"path": "report.csv"}, subgoal="x"), True, "report.csv contains 42 rows.", data={"rows": 42})
    goal_rows = "Find report.csv and tell me how many rows it contains."

    scenario("G1 (brief §6A / §7 test 1): explicit multi-part request, both parts answered -> passes unchanged")
    answer_full = "I found report.csv and it contains 42 rows."
    check("G1: an answer covering every explicitly-requested part passes through unchanged",
          discovery.ground_answer(goal_rows, answer_full, [obs_found, obs_rows]) == answer_full)

    scenario("G2 (brief §6A / §7 test 2): the explicitly-requested row count is omitted -> flagged incomplete")
    answer_partial = "I found report.csv."
    result_g2 = discovery.ground_answer(goal_rows, answer_partial, [obs_found, obs_rows])
    check("G2: the incomplete answer is not returned verbatim", result_g2 != answer_partial, result_g2)
    check("G2: the original (correct, if partial) content is preserved, not discarded", "report.csv" in result_g2, result_g2)
    check("G2: the omitted, explicitly-requested row count is surfaced rather than silently dropped", "42" in result_g2, result_g2)
    check("G2: this is a completeness gap, not an 'insufficient evidence' claim (the evidence WAS enough)",
          "insufficient" not in result_g2.lower(), result_g2)

    scenario("G3 (brief §6B / §7 test 3): same evidence, a general-status goal that never asked for the row count -> omission allowed")
    goal_status = "Check report.csv."
    check("G3: the row count was never explicitly requested, so omitting it does not fail the answer",
          discovery.ground_answer(goal_status, answer_partial, [obs_found, obs_rows]) == answer_partial)

    scenario("G4 (brief §7 test 4): several observations present, only the goal-relevant one is required")
    obs_weather = Observation(PlanStep("test.ag_weather", {}, subgoal="x"), True, "It's sunny and 72 degrees.")
    check("G4: an unrelated third observation (weather) is never required in a report.csv answer",
          discovery.ground_answer(goal_status, "I found report.csv.", [obs_found, obs_rows, obs_weather]) == "I found report.csv.")

    scenario("G5 (brief §6C / §7 test 5): a requested action's own real outcome must be communicated")
    goal_delete = "Delete report.csv."
    obs_deleted = Observation(PlanStep("test.ag_delete_file", {"path": "report.csv"}, subgoal="x"), True, "Deleted report.csv successfully.")
    result_g5 = discovery.ground_answer(goal_delete, "I checked the file.", [obs_deleted])
    check("G5: an answer that never mentions the real action outcome is flagged incomplete", result_g5 != "I checked the file.", result_g5)
    check("G5: the real action result is surfaced in the fallback", "deleted" in result_g5.lower(), result_g5)
    check("G5: still not an 'insufficient evidence' message (the action genuinely happened)",
          "insufficient" not in result_g5.lower(), result_g5)

    scenario("G6 (brief §6D / §7 test 6): a negative/failed result, correctly communicated -> passes unchanged; silently dropped -> flagged")
    goal_find = "Find report.csv."
    obs_missing = Observation(PlanStep("test.ag_find_report", {"path": "report.csv"}, subgoal="x"), False, "No report.csv was found.")
    answer_negative = "I couldn't find report.csv."
    check("G6a: a correctly-communicated negative result passes through unchanged",
          discovery.ground_answer(goal_find, answer_negative, [obs_missing]) == answer_negative)
    result_g6b = discovery.ground_answer(goal_find, "I looked into it.", [obs_missing])
    check("G6b: an answer that never says the file wasn't found is flagged incomplete", result_g6b != "I looked into it.", result_g6b)
    check("G6b: the real negative result is surfaced", "found" in result_g6b.lower() or "report.csv" in result_g6b, result_g6b)
    check("G6b: still not an 'insufficient evidence' message (there IS a real, conclusive result: not found)",
          "insufficient" not in result_g6b.lower(), result_g6b)

    scenario("G7 (brief §7 test 7): two explicitly requested outputs, one missing -> flagged; both present -> unchanged")
    goal_two = "Check the weather and tell me the disk space."
    obs_w = Observation(PlanStep("test.ag_weather", {}, subgoal="x"), True, "It's sunny and 72 degrees.")
    obs_d = Observation(PlanStep("test.ag_disk", {}, subgoal="x"), True, "Disk has 4 percent free space.", data={"free_pct": 4})
    result_g7 = discovery.ground_answer(goal_two, "It's sunny and 72 degrees.", [obs_w, obs_d])
    check("G7: the disk-space clause's own real result, never mentioned, is flagged incomplete",
          result_g7 != "It's sunny and 72 degrees.", result_g7)
    check("G7: the omitted disk result is surfaced", "disk" in result_g7.lower(), result_g7)
    answer_two_full = "It's sunny and 72 degrees, and disk has 4 percent free space."
    check("G7b: both explicitly requested parts answered -> passes unchanged",
          discovery.ground_answer(goal_two, answer_two_full, [obs_w, obs_d]) == answer_two_full)

    scenario("G8 (brief §7 test 8): different wording between evidence and the final answer still grounds completeness")
    obs_invoice = Observation(PlanStep("test.ag_search", {"query": "invoice"}, subgoal="x"), True, "Located the invoice in Downloads.")
    answer_invoice = "I found the invoice in your Downloads folder."
    check("G8: a synonymous, differently-worded answer still counts as acknowledging the result",
          discovery.ground_answer("Find the invoice.", answer_invoice, [obs_invoice]) == answer_invoice)

    scenario("G9 (brief §7 test 9): incidental, off-topic evidence never on its own creates a completeness failure")
    result_g9 = discovery.ground_answer(goal_find, "I found report.csv.", [obs_found, obs_d])
    check("G9: an incidental, off-topic observation (disk space) is never forced into a report.csv answer",
          result_g9 == "I found report.csv.", result_g9)

    scenario("G10 (brief §7 test 10): uncertain evidence creates no completeness requirement")
    obs_uncertain_rows = Observation(PlanStep("test.ag_read", {"path": "report.csv"}, subgoal="x"), True, "maybe 42 rows", data={"uncertain": True})
    result_g10 = discovery.ground_answer(goal_rows, "I found report.csv.", [obs_found, obs_uncertain_rows])
    check("G10: an uncertain-only observation about the row count is never required to be mentioned",
          result_g10 == "I found report.csv.", result_g10)

    scenario("G11 (brief §7 test 11): the existing unsupported-claim check stays active alongside completeness")
    hostile_rows = "I found report.csv and it has 9999 rows."
    result_g11 = discovery.ground_answer(goal_rows, hostile_rows, [obs_found, obs_rows])
    check("G11: a fabricated row count is still caught as unsupported, not waved through as merely 'complete'",
          "9999" not in result_g11 and result_g11 != hostile_rows, result_g11)

    scenario("G12 (brief §7 test 12): existing numeric/filename grounding checks remain intact")
    obs_plain = Observation(PlanStep("test.ag_count", {}, subgoal="x"), True, "The queue looks busy.")
    result_g12 = discovery.ground_answer("what's the count", "There are 9999 items in queue.", [obs_plain])
    check("G12: an unsupported 3+ digit number is still caught (Phase 24.1/24.2 unaffected by Phase 24.3)",
          "insufficient" in result_g12.lower() and "9999" not in result_g12, result_g12)


# ================================================================================
# H — Phase 24.4: evidence prioritization (which observation speaks for the answer).
#     Twelve scenarios, mapped 1:1 to the brief's own test list.
# ================================================================================


def section_h() -> None:
    def mk(tool: str, args: dict, ok: bool, speech: str, data: dict | None = None) -> Observation:
        extra = {"data": data} if data else {}
        return Observation(PlanStep(tool, args, subgoal="x"), ok, speech, **extra)

    ground = discovery.ground_answer
    goal_del = "Delete report.csv."
    attempt = mk("files.delete", {"path": "report.csv"}, True, "Attempting to delete report.csv.")
    ans_deleted = "I deleted report.csv."

    scenario("H1 (brief test 1): an intermediate 'attempt' observation followed by a successful final result")
    deleted = mk("files.delete", {"path": "report.csv", "confirm": True}, True, "Deleted report.csv successfully.")
    check("H1a: the later concrete success grounds the completed-action claim (the attempt line does not get in the way)",
          ground(goal_del, ans_deleted, [attempt, deleted]) == ans_deleted)
    result_h1 = ground(goal_del, ans_deleted, [attempt])
    check("H1b: the attempt line ALONE never grounds 'deleted' -- 'attempting to delete' is not a deletion",
          "insufficient" in result_h1.lower() and result_h1 != ans_deleted, result_h1)

    scenario("H2 (brief test 2): an intermediate 'attempt' observation followed by a final FAILURE")
    denied = mk("files.delete", {"path": "report.csv", "confirm": True}, False, "Delete failed: permission denied.")
    result_h2 = ground(goal_del, ans_deleted, [attempt, denied])
    check("H2a: 'I deleted report.csv' is rejected -- the final result was a failure", "insufficient" in result_h2.lower(), result_h2)
    check("H2b: the real failure is what gets restated, never the attempt line presented as confirmed",
          "permission denied" in result_h2.lower() and "attempting" not in result_h2.lower(), result_h2)
    neg_reason = "I couldn't delete report.csv — permission denied."
    check("H2c: an honest negative answer that says why passes through unchanged",
          ground(goal_del, neg_reason, [attempt, denied]) == neg_reason)
    neg_plain = "I couldn't delete report.csv."
    check("H2d: an honest negative answer that names the target but not the reason passes unchanged too",
          ground(goal_del, neg_plain, [attempt, denied]) == neg_plain)

    scenario("H3 (brief test 3): an earlier success followed by a later failure of the same operation -> the later failure wins")
    first_ok = mk("files.delete", {"path": "report.csv"}, True, "Deleted report.csv.")
    later_fail = mk("files.delete", {"path": "report.csv", "force": True}, False, "Delete failed: permission denied.")
    result_h3 = ground(goal_del, ans_deleted, [first_ok, later_fail])
    check("H3a: the overridden earlier success no longer grounds the claim; the later failure is what's restated",
          "insufficient" in result_h3.lower() and "permission denied" in result_h3.lower(), result_h3)
    check("H3b: the answer that reports the later failure passes unchanged", ground(goal_del, neg_reason, [first_ok, later_fail]) == neg_reason)

    scenario("H4 (brief test 4): an earlier failure followed by a later success (a retry that worked) -> the later success wins")
    first_fail = mk("files.delete", {"path": "report.csv"}, False, "Delete failed: permission denied.")
    retry_ok = mk("files.delete", {"path": "report.csv", "retry": True}, True, "Deleted report.csv successfully.")
    check("H4a: the success claim is grounded, and the resolved earlier failure is not forced into the answer",
          ground(goal_del, ans_deleted, [first_fail, retry_ok]) == ans_deleted)
    check("H4b: prioritization puts the later success first",
          discovery.prioritize_evidence(goal_del, [first_fail, retry_ok])[0] is retry_ok)

    scenario("H5 (brief test 5): a later, irrelevant observation must not override an earlier relevant result")
    goal_rows = "Find report.csv and tell me how many rows it contains."
    found = mk("test.ag_find_report", {"path": "report.csv"}, True, "Found report.csv containing 42 rows.", {"rows": 42})
    opening = mk("test.ag_open_dir", {"path": "C:/Data"}, True, "Opening another directory.")
    ans_full = "I found report.csv and it has 42 rows."
    check("H5a: the earlier useful result still answers the goal", ground(goal_rows, ans_full, [found, opening]) == ans_full)
    result_h5 = ground(goal_rows, "I opened another directory.", [found, opening])
    check("H5b: an answer that only mentions the later, unrelated observation is completed with the useful result",
          "42" in result_h5 and result_h5 != "I opened another directory.", result_h5)
    check("H5c: recency is only a tie-breaker -- the relevant result ranks first in either order",
          discovery.prioritize_evidence(goal_rows, [found, opening])[0] is found
          and discovery.prioritize_evidence(goal_rows, [opening, found])[0] is found)

    scenario("H6 (brief test 6): several observations each holding a partial piece of one answer")
    p_found = mk("test.ag_find_report", {"path": "report.csv"}, True, "Located report.csv.")
    p_rows = mk("test.ag_read", {"path": "report.csv"}, True, "It has 42 rows.", {"rows": 42})
    p_mod = mk("test.ag_stat", {"path": "report.csv"}, True, "It was last modified today.")
    combined = "I found report.csv; it has 42 rows and was last modified today."
    check("H6a: an answer assembled from all three partial observations is grounded and complete",
          ground("Tell me about report.csv.", combined, [p_found, p_rows, p_mod]) == combined)
    result_h6 = ground("Tell me about report.csv.", combined + " I also deleted it.", [p_found, p_rows, p_mod])
    check("H6b: one unsupported extra piece still fails the WHOLE answer closed",
          "insufficient" in result_h6.lower() and "i also deleted it" not in result_h6.lower(), result_h6)

    scenario("H7 (brief test 7): a conclusive, relevant result beats a generic status observation")
    goal_sum = "Find report.csv and create a summary of it."
    searching = mk("files.search", {"query": "report"}, True, "Searching the folder...")
    several = mk("files.list", {"path": "."}, True, "Found several files.")
    final = mk("files.summarize", {"path": "report.csv"}, True, "Found report.csv and created the requested summary.")
    ans_final = "I found report.csv and created the summary."
    check("H7a: intermediate status -> intermediate discovery -> concrete final result: the final answer passes",
          ground(goal_sum, ans_final, [searching, several, final]) == ans_final)
    result_h7 = ground(goal_sum, "I found several files.", [searching, several, final])
    check("H7b: an answer resting on the vague intermediate discovery is completed with the concrete final result",
          "created the requested summary" in result_h7, result_h7)
    still_working = mk("files.summarize", {"path": "report.csv"}, True, "Still working on it...")
    check("H7c: a LATER generic status line never displaces the earlier conclusive result",
          discovery.prioritize_evidence(goal_sum, [final, still_working])[0] is final
          and ground(goal_sum, ans_final, [final, still_working]) == ans_final)

    scenario("H8 (brief test 8): a negative result remains usable evidence")
    goal_find = "Find report.csv."
    not_found = mk("files.find", {"path": "report.csv"}, False, "No report.csv was found.")
    neg_answer = "I couldn't find report.csv."
    check("H8a: the failed result outranks the attempt line and an honest negative answer passes unchanged",
          discovery.prioritize_evidence(goal_find, [searching, not_found])[0] is not_found
          and ground(goal_find, neg_answer, [searching, not_found]) == neg_answer)
    result_h8 = ground(goal_find, "I found report.csv.", [searching, not_found])
    check("H8b: a positive claim contradicting it is rejected, and the real negative result is what's restated",
          "insufficient" in result_h8.lower() and "no report.csv was found" in result_h8.lower(), result_h8)

    scenario("H9 (brief test 9): success and failure reported by DIFFERENT tools are preserved, never silently resolved")
    tool_ok = mk("files.delete", {"path": "report.csv"}, True, "Deleted report.csv successfully.")
    tool_bad = mk("shell.run", {"cmd": "del report.csv"}, False, "Delete failed: access denied.")
    result_h9a = ground(goal_del, ans_deleted, [tool_ok, tool_bad])
    check("H9a: an answer taking only the success side keeps its text AND gets the conflicting failure appended",
          result_h9a.startswith(ans_deleted) and "access denied" in result_h9a.lower() and "insufficient" not in result_h9a.lower(), result_h9a)
    result_h9b = ground(goal_del, "I couldn't delete report.csv — access denied.", [tool_ok, tool_bad])
    check("H9b: an answer taking only the failure side gets the conflicting success appended",
          "deleted report.csv successfully" in result_h9b.lower(), result_h9b)
    reconciled = "The evidence conflicts: one tool says report.csv was deleted, but another says the delete failed with access denied."
    check("H9c: an answer that reconciles both sides passes through unchanged", ground(goal_del, reconciled, [tool_ok, tool_bad]) == reconciled)
    check("H9d: neither observation is dropped from the prioritized evidence", len(discovery.prioritize_evidence(goal_del, [tool_ok, tool_bad])) == 2)

    scenario("H10 (brief test 10): existing unsupported-claim protection stays active alongside prioritization")
    result_h10 = ground(goal_del, "I deleted report.csv and it had 9999 rows.", [attempt, deleted])
    check("H10: a fabricated number on top of a fully-resolved, grounded result still fails the whole answer closed",
          "insufficient" in result_h10.lower() and "9999" not in result_h10, result_h10)

    scenario("H11 (brief test 11): existing completeness protection stays active alongside prioritization")
    result_h11 = ground(goal_del, "I checked the file.", [attempt, deleted])
    check("H11a: an answer that omits the real final result is completed with it",
          "deleted report.csv successfully" in result_h11.lower(), result_h11)
    check("H11b: the completion restates the RESULT, never the superseded attempt line", "attempting" not in result_h11.lower(), result_h11)

    scenario("H12 (brief test 12): uncertain observations can never become preferred (or overriding) evidence")
    located = mk("files.find", {"path": "report.csv"}, True, "Located report.csv.")
    maybe_deleted = mk("files.delete", {"path": "report.csv"}, True, "Deleted report.csv.", {"uncertain": True})
    result_h12 = ground(goal_del, ans_deleted, [located, maybe_deleted])
    check("H12a: an uncertain 'Deleted' neither grounds the claim nor appears in the prioritized evidence",
          "insufficient" in result_h12.lower() and maybe_deleted not in discovery.prioritize_evidence(goal_del, [located, maybe_deleted]), result_h12)
    maybe_failed = mk("files.delete", {"path": "report.csv", "force": True}, False, "Delete failed, maybe.", {"uncertain": True})
    check("H12b: an uncertain LATER failure cannot override an earlier real success",
          ground(goal_del, ans_deleted, [first_ok, maybe_failed]) == ans_deleted)


def main() -> None:
    started = time.perf_counter()
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()
    section_g()
    section_h()
    sys.exit(H.finish("Phase 24.1/24.2/24.3/24.4 — answer-from-evidence grounding + completeness + evidence prioritization",
                       time.perf_counter() - started, min_assertions=105, min_scenarios=47))


if __name__ == "__main__":
    main()
