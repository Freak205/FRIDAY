"""Phase 24.7 — grounded final-answer quality normalization.

Phases 24.1-24.6 decide what a final answer may CLAIM and when a run may STOP. This suite pins
the last, purely cosmetic step: once an answer has passed `ground_answer`, tidy how it reads —
without ever adding a word. `discovery.normalize_answer` may only DELETE whole sentences:

  1. an identical / merely-restated result is stated once
     ("Created report.csv successfully. Created report.csv successfully.")
  2. a progress / attempt line a LATER real result settled is dropped, so the concrete result
     is what remains ("Attempting to delete report.csv. Deleted report.csv successfully.",
     "Searching... Found report.csv.")
  3. a verbatim echo of a failing tool result about a file the goal never named is dropped
     ("Permission denied opening notes.txt." in an answer about report.csv)

and it must NEVER drop a failure, a negative result, a requested number, a requested filename,
one of several requested results, a conflict, or an attempt nothing has settled — nor return an
empty answer, nor introduce a number or a filename. Sections:

  A  the rules, hand-built text + evidence (what goes, and what must stay)
  B  the safety net: a removal that would cost the answer something is discarded; additions
     are refused
  C  the real Phase 23 composer route (`Orchestrator._answer_from_evidence`)
  D  the real planner `done` route (`Orchestrator._ground_done_summary`)
  E  the real deterministic evidence-stop route (`run_goal` -> `_summarize`)
  F  the order: grounding runs first, normalization sees only grounded text, and cannot
     resurrect what grounding rejected
  G  property checks over a seeded random corpus: output is always a non-empty character
     subsequence of the input, never gains a number or filename, keeps every unique failure
     about the goal's own file, and is idempotent

Deterministic and offline: a scripted model behind the `llm.get_provider` seam plus a recording
fake tool world (the same scaffolding as the Phase 24.5 / 24.6 suites). No Ollama, no real side
effect.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import time
import uuid

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import intent  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.intelligence.goals import Subgoal  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.registry import SkillResult  # noqa: E402


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None) -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, {}, "")


def norm(goal: str, answer: str, observations: list[Observation]) -> str:
    return discovery.normalize_answer(goal, answer, observations)


REPORT = {"path": "report.csv"}
DELETE_EV = [obs("files.delete", "Attempting to delete report.csv.", args=REPORT),
             obs("files.remove", "Deleted report.csv successfully.", args=REPORT)]
CREATE_EV = [obs("files.create", "Created report.csv successfully.", args=REPORT),
             obs("files.make", "Created report.csv successfully.", args={**REPORT, "overwrite": True})]
FIND_EV = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}),
           obs("files.rows", "report.csv contains 42 rows.", args=REPORT)]
NOTES_FAIL = obs("files.read", "Permission denied opening notes.txt.", ok=False, args={"path": "notes.txt"})
FIND_ROWS = "Find report.csv and tell me its row count."


# ================================================================================
# A — the rules
# ================================================================================


def section_a() -> None:
    scenario("A: what normalization removes — and, above all, what it must leave alone")
    dup = "Created report.csv successfully. Created report.csv successfully."
    log_line = "Connection timed out. Connection timed out. Connection timed out."
    log_ev = [obs("files.read", log_line, args={"path": "app.log"})]
    check("A1: an identical result stated twice is stated once — but a repetition the tool's own speech contains (a log) is content and stays",
          norm("Create report.csv.", dup, CREATE_EV) == "Created report.csv successfully."
          and norm("What does app.log say?", log_line, log_ev) == log_line)

    restated = norm("Delete report.csv.", "Deleted report.csv. I deleted report.csv successfully.", DELETE_EV)
    also = norm("Create report.csv.", "Created report.csv successfully. Also: Created report.csv successfully.", CREATE_EV)
    narrowed = "report.csv has 42 rows. report.csv has 42 rows in Sheet1."
    check("A2: a restated result (filler only, or an 'Also:' repeat) is not repeated — but a sentence that adds a real detail is kept",
          restated == "I deleted report.csv successfully." and also == "Created report.csv successfully."
          and norm("Count rows in report.csv.", narrowed, FIND_EV) == narrowed, f"{restated!r} {also!r}")

    attempt = norm("Delete report.csv.", "Attempting to delete report.csv. Deleted report.csv successfully.", DELETE_EV)
    search = norm("Find report.csv.", "Searching... Found report.csv.", FIND_EV)
    opening = norm(FIND_ROWS, "Opening report.csv... report.csv contains 42 rows.", FIND_EV)
    filler = norm("Delete report.csv.", "Working on it... Deleted report.csv successfully.", DELETE_EV)
    to_bob = norm("Send the message to Bob.", "Sending the message to Bob... Sent message to Bob.",
                  [obs("chat.send", "Sent message to Bob.", args={"to": "Bob"})])
    check("A3: intermediate progress is removed when the final result is present (attempt, search, opening, filler, "
          "a non-file object) and the final success remains, verbatim, still naming the file",
          (attempt, search, opening, filler, to_bob) == (
              "Deleted report.csv successfully.", "Found report.csv.", "report.csv contains 42 rows.",
              "Deleted report.csv successfully.", "Sent message to Bob.")
          and norm("Delete report.csv.", "Deleted report.csv successfully.", DELETE_EV) == "Deleted report.csv successfully.",
          str((attempt, search, opening, filler, to_bob)))

    denied = "Permission denied while deleting report.csv."
    denial_ev = [obs("files.delete", "Attempting to delete report.csv.", args=REPORT), obs("files.remove", denied, ok=False, args=REPORT)]
    after_attempt = norm("Delete report.csv.", f"Attempting to delete report.csv. {denied}", denial_ev)
    check("A4: a final failure remains, alone or after the attempt that led to it",
          norm("Delete report.csv.", denied, denial_ev) == denied and after_attempt == denied, after_attempt)

    none_found = norm("Find zzz.csv.", "Searching... No matching file was found.",
                      [obs("files.find", "No matching file was found.", args={"name": "zzz.csv"})])
    check("A5: a negative result remains ('No matching file was found.')",
          none_found == "No matching file was found."
          and norm("Find zzz.csv.", "No matching file was found.", []) == "No matching file was found.", none_found)

    counted = norm(FIND_ROWS, "Counting the rows... report.csv contains 42 rows.", FIND_EV)
    number_only_in_progress = "Counting rows: 12 of 42... report.csv contains 42 rows."
    check("A6: the requested number remains, and a progress line that alone carries a number is kept",
          counted == "report.csv contains 42 rows." and "42" in counted
          and norm(FIND_ROWS, number_only_in_progress, FIND_EV) == number_only_in_progress, counted)

    only_in_attempt = "Attempting to delete report.csv. Delete failed: permission denied."
    check("A7: a requested filename is never normalized away (here it survives only in the attempt line, so that line stays)",
          norm("Delete report.csv.", only_in_attempt,
               [obs("files.delete", "Attempting to delete report.csv.", args=REPORT),
                obs("files.remove", "Delete failed: permission denied.", ok=False, args=REPORT)]) == only_in_attempt)

    two_files = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}), obs("files.find", "Found notes.txt.", args={"name": "notes.txt"})]
    multi = norm("Find report.csv and notes.txt.", "Searching... Found report.csv. Found report.csv. Found notes.txt.", two_files)
    check("A8: multiple requested results remain (each named file is still reported)",
          multi == "Found report.csv. Found notes.txt.", multi)

    answer_without = "Found report.csv. report.csv contains 42 rows."
    unrelated = f"{answer_without} Permission denied opening notes.txt."
    evidence = FIND_EV + [NOTES_FAIL]
    check("A9: an unrelated observation is never ADDED to an answer that does not carry it",
          norm(FIND_ROWS, answer_without, evidence) == answer_without and norm(FIND_ROWS, answer_without, evidence) is answer_without)
    goal_notes = "Find report.csv and notes.txt and tell me report.csv's row count."
    check("A9b: a verbatim failure about a file the goal never named is dropped; the same failure about a file the goal DID name stays",
          norm(FIND_ROWS, unrelated, evidence) == answer_without and norm(goal_notes, unrelated, evidence) == unrelated,
          norm(FIND_ROWS, unrelated, evidence))

    contrast = "The first tool reported success, but the later verification reported failure."
    conflict_ev = [obs("files.delete", "Deleted report.csv.", args=REPORT),
                   obs("files.check", "Could not confirm the deletion: report.csv is still there.", ok=False, args=REPORT)]
    both_sides = norm("Delete report.csv.",
                      "Deleted report.csv. Deleted report.csv. Could not confirm the deletion: report.csv is still there.", conflict_ev)
    check("A10: conflicting results are preserved — the contrast sentence is untouched and neither side of a duplicated success/failure pair is lost",
          norm("Delete report.csv.", contrast, conflict_ev) == contrast
          and both_sides == "Deleted report.csv. Could not confirm the deletion: report.csv is still there.", both_sides)

    real_stage = discovery._drop_restated
    discovery._drop_restated = lambda sents, kept, protected=None: []  # a stage that would leave NOTHING
    try:
        emptied = norm("Create report.csv.", dup, CREATE_EV)
    finally:
        discovery._drop_restated = real_stage
    check("A11: if normalization would leave nothing, the original answer comes back unchanged (and empty input stays empty)",
          emptied == dup and norm("g", "", CREATE_EV) == "" and norm("g", "   ", CREATE_EV) == "   ", emptied)

    concise = ["I found report.csv in Documents.", "Deleted report.csv successfully.", "report.csv contains 42 rows.",
               "Found report.csv. It has 42 rows.", "Yes. Yes."]
    same = [norm(FIND_ROWS, s, FIND_EV) is s for s in concise]
    check("A12: an already concise answer is returned unchanged (the very same string)", all(same), str(same))

    unsettled = ["Attempting to delete report.csv. Found notes.txt.", "Attempting to delete the file... Found report.csv.",
                 "Found report.csv. Still working on it...", "Attempting to delete report.csv. Presumably deleted report.csv.",
                 "Sending the message to Bob... Sent message to Alice."]
    other_ev = DELETE_EV + [obs("files.find", "Found notes.txt.", args={"name": "notes.txt"}), obs("chat.send", "Sent message to Alice.", args={"to": "Alice"})]
    kept = [norm("Delete report.csv.", s, other_ev) is s for s in unsettled]
    check("A13: an attempt nothing has settled (other file, other object, other action, later, hedged result) is honest and stays",
          all(kept), str(kept))


# ================================================================================
# B — the safety net
# ================================================================================


def section_b() -> None:
    scenario("B: a removal that would cost the answer something is discarded, and additions are refused")
    goal = "Find report.csv and open the notes."
    evidence = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}), NOTES_FAIL]
    answer = "Found report.csv. Permission denied opening notes.txt."
    check("B1: a failure the goal's own words point at ('open the notes') stays even though the goal names no such file",
          norm(goal, answer, evidence) == answer, norm(goal, answer, evidence))

    real_rebuild = discovery._rebuild
    discovery._rebuild = lambda text, spans, kept: real_rebuild(text, spans, kept) + " Also: 999 rows."
    try:
        added = norm("Create report.csv.", "Created report.csv successfully. Created report.csv successfully.", CREATE_EV)
    finally:
        discovery._rebuild = real_rebuild
    real_stage = discovery._drop_restated
    discovery._drop_restated = lambda sents, kept, protected=None: [i for i in kept if "report.csv" not in sents[i].text]  # drops the goal's file
    try:
        lost_file = norm("Create report.csv.", "Created report.csv successfully. Created report.csv successfully.", CREATE_EV)
    finally:
        discovery._drop_restated = real_stage
    dup = "Created report.csv successfully. Created report.csv successfully."
    check("B2: a rebuild that ADDED text, or a stage that dropped the goal's own filename, is refused — the answer comes back unchanged",
          added == dup and lost_file == dup, f"{added!r} | {lost_file!r}")


# ================================================================================
# The run_goal sections: scripted model, recording tool world, the real orchestrator
# ================================================================================

SPECS = [
    # `test.nq.<name>`: a tool the registry has never heard of; the intent guard (only used for the
    # look-only route) classifies it by its L0 tier or by the verb its name ends in.
    ToolSpec(name="test.nq.create", description="create a named file", tier="L1", params="path (str)", action="create"),
    ToolSpec(name="test.nq.delete", description="delete a named file", tier="L1", params="path (str), force (bool)", action="delete"),
    ToolSpec(name="test.nq.begin", description="start a slow delete", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.nq.progress", description="report progress", tier="L0", params="", action="read"),
    ToolSpec(name="test.nq.finish", description="finish a slow delete", tier="L1", params="path (str)", action="delete"),
    ToolSpec(name="test.nq.find", description="find a file by name", tier="L0", params="name (str), where (str)", action="read"),
    ToolSpec(name="test.nq.rows", description="count the rows of a file", tier="L0", params="path (str)", action="read"),
    ToolSpec(name="test.nq.read", description="read a file", tier="L0", params="path (str)", action="read"),
]

CALLS: list[tuple[str, dict]] = []
RESULTS: dict[str, list[SkillResult]] = {}  # tool -> results, consumed in call order (the last repeats)


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    queue = RESULTS.get(tool)
    if not queue:
        raise KeyError(f"no scripted result for test tool: {tool}")
    return queue[0] if len(queue) == 1 else queue.pop(0)


def world(**tools: list[SkillResult]) -> None:
    RESULTS.clear()
    RESULTS.update({f"test.nq.{name}": results for name, results in tools.items()})


def R(speech: str, *, ok: bool = True) -> SkillResult:
    return SkillResult(speech=speech, ok=ok)


class RoutedPlanner(ScriptedPlanner):
    """A scripted model that tells the two kinds of call apart: a tool-DECISION turn consumes the
    next entry of `decisions` (the last repeats); the dedicated ANSWER-composition call (Phase 23's
    `_answer_from_evidence`, recognized by its own system prompt) always returns `answer`."""

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
    CFG.planner.max_coverage_nudges = 1


async def _drive(goal: str, decisions: list[str], *, answer: str = "", subgoals: list[Subgoal] | None = None,
                 mode: str = "plain", max_steps: int = 10):
    """Scripted planner -> the real Orchestrator.run_goal -> the recording tool world, called the way
    `plan.run` calls it (same coverage goal, same replan budget). mode: 'plain' (no scope: the composer
    and `done` routes), 'look' (the look-only scope `plan.run` derives, which enables the deterministic
    look-only evidence stop) or 'disc' (the discovery pass: its sufficiency stop)."""
    CALLS.clear()
    planner = RoutedPlanner(decisions, answer)
    orch = Orchestrator(
        tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=planner,
        tool_specs=SPECS, max_steps=max_steps,
    )
    extra = {"action_scope": intent.derive_scope(goal), "discovery_mode": mode == "disc"} if mode != "plain" else {}
    res = await orch.run_goal(
        goal, subgoals=subgoals, coverage_goal=goal, max_replans=CFG.planner.max_replans, **extra,
    )
    return res, planner


def drive(goal: str, decisions: list[str], **kw):
    return asyncio.run(_drive(goal, decisions, **kw))


def ran() -> list[str]:
    return [t.removeprefix("test.nq.") for t, _ in CALLS]


def low(text: str) -> str:
    return (text or "").lower()


DEL = {"path": "report.csv"}
CREATE = {"path": "report.csv"}


# ================================================================================
# C — the Phase 23 composer route
# ================================================================================


def section_c() -> None:
    scenario("C: composer route (`_answer_from_evidence`) — grounded, then tidied")
    reset()
    world(create=[R("Created report.csv successfully.")])
    subs = [sg("Create report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Create report.csv and tell me what happened.", [call("test.nq.create", CREATE)],
                    answer="Created report.csv successfully. Created report.csv successfully.", subgoals=subs)
    check("C1: successful action — the composed answer's repeated result is stated once, the run is ok",
          pl.answer_calls == 1 and res.ok and res.summary == "Created report.csv successfully.", f"{pl.answer_calls} {res.ok} {res.summary!r}")

    reset()
    world(begin=[R("Attempting to delete report.csv.")], finish=[R("Deleted report.csv successfully.")])
    subs = [sg("Delete report.csv"), sg("Tell me what happened", "answer")]
    res, pl = drive("Delete report.csv and tell me what happened.",
                    [call("test.nq.begin", DEL), call("test.nq.finish", DEL)],
                    answer="Attempting to delete report.csv. Deleted report.csv successfully.", subgoals=subs)
    check("C2: intermediate -> final result — the attempt line is gone, the concrete deletion is what the user hears",
          ran() == ["begin", "finish"] and pl.answer_calls == 1 and res.summary == "Deleted report.csv successfully.",
          f"{ran()} {res.summary!r}")


# ================================================================================
# D — the planner `done` route
# ================================================================================


def section_d() -> None:
    scenario("D: planner `done` route (`_ground_done_summary`) — grounded, then tidied")
    reset()
    world(find=[R("Found report.csv in Downloads.")], delete=[R("Delete failed: permission denied.", ok=False)])
    res, pl = drive("Find report.csv and delete it.", [
        call("test.nq.find", {"name": "report.csv"}), call("test.nq.delete", DEL),
        done("Found report.csv in Downloads. Found report.csv in Downloads. Delete failed: permission denied."),
    ])
    check("D1: failed action — the repetition goes, the real failure stays visible",
          res.summary == "Found report.csv in Downloads. Delete failed: permission denied.", res.summary)

    reset()
    world(find=[R("Found report.csv in Documents.")], rows=[R("report.csv has 42 rows.")])
    res, pl = drive("Find report.csv and tell me how many rows it has.", [
        call("test.nq.find", {"name": "report.csv"}), call("test.nq.rows", DEL),
        done("Found report.csv in Documents. Found report.csv in Documents. report.csv has 42 rows."),
    ])
    check("D2: multi-result goal — both requested results (the location and the count) stay, only the repeat goes",
          ran() == ["find", "rows"] and res.summary == "Found report.csv in Documents. report.csv has 42 rows.",
          f"{ran()} {res.summary!r}")

    reset()
    world(begin=[R("Attempting to delete report.csv.")], progress=[R("Still working on it...")],
          finish=[R("Deleted report.csv successfully.")])
    res, pl = drive("Delete report.csv.", [
        call("test.nq.begin", DEL), call("test.nq.progress"), call("test.nq.finish", DEL),
        done("Attempting to delete report.csv. Still working on it... Deleted report.csv successfully."),
    ])
    check("D3: attempt -> intermediate status -> final success — only the final result is left",
          ran() == ["begin", "progress", "finish"] and res.ok and res.summary == "Deleted report.csv successfully.",
          f"{ran()} {res.summary!r}")


# ================================================================================
# E — the deterministic evidence-stop route
# ================================================================================


def section_e() -> None:
    scenario("E: deterministic evidence stop (`_summarize` of a COMPLETED run) — the tools' own words, tidied")
    reset()
    world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    decisions = [call("test.nq.find", {"name": "report.csv"}), call("test.nq.find", {"name": "report.csv", "where": "Downloads"}), done("unused")]
    res, pl = drive("Find report.csv.", decisions, mode="look")
    check("E1: intermediate -> final result — the search-progress line is dropped, the run stops on the real find "
          "(and the goal really is look-only, the condition for this stop)",
          not intent.derive_scope("Find report.csv.").allowed
          and ran() == ["find", "find"] and res.ok and res.stopped == "completed" and res.summary == "Found report.csv.",
          f"{ran()} {res.stopped} {res.summary!r}")

    reset()
    world(find=[R("Found report.csv."), R("Found report.csv.")], rows=[R("report.csv contains 42 rows.")])
    res, pl = drive(FIND_ROWS, [
        call("test.nq.find", {"name": "report.csv"}), call("test.nq.find", {"name": "report.csv", "where": "Downloads"}),
        call("test.nq.rows", REPORT), done("unused"),
    ], mode="look")
    check("E2: multi-result goal — the location and the count are both there, the repeated 'Found report.csv.' is not",
          ran() == ["find", "find", "rows"] and res.summary == "Found report.csv. report.csv contains 42 rows.",
          f"{ran()} {res.summary!r}")

    reset()
    world(find=[R("Found report.csv.")], read=[R("Permission denied opening notes.txt.", ok=False)], rows=[R("report.csv contains 42 rows.")])
    res, pl = drive(FIND_ROWS, [
        call("test.nq.find", {"name": "report.csv"}), call("test.nq.read", {"path": "notes.txt"}),
        call("test.nq.rows", REPORT), done("unused"),
    ], mode="disc")
    check("E2b: the brief's own example — found + a failure about notes.txt + the row count, goal names only report.csv: "
          "the answer keeps the first two results and not the unrelated notes.txt failure",
          ran() == ["find", "read", "rows"] and res.ok and res.summary == "Found report.csv. report.csv contains 42 rows.",
          f"{ran()} {res.ok} {res.summary!r}")

    reset()
    CFG.planner.answer_grounding_guard = False
    world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    res_off, _ = drive("Find report.csv.", decisions, mode="look")
    reset()
    CFG.planner.answer_grounding_guard = False
    world(create=[R("Created report.csv successfully.")])
    res_c, _ = drive("Create report.csv and tell me what happened.", [call("test.nq.create", CREATE)],
                     answer="Created report.csv successfully. Created report.csv successfully.",
                     subgoals=[sg("Create report.csv"), sg("Tell me what happened", "answer")])
    check("E3: with answer_grounding_guard off there is no tidying at all (the same switch governs grounding and normalization)",
          res_off.summary == "Searching for report.csv... Found report.csv."
          and res_c.summary == "Created report.csv successfully. Created report.csv successfully.",
          f"{res_off.summary!r} | {res_c.summary!r}")
    reset()


# ================================================================================
# F — grounding first, normalization second
# ================================================================================


def section_f() -> None:
    scenario("F: the order is ground -> normalize; normalization only ever sees grounded text")
    trace: list[tuple[str, str, str]] = []
    real_ground, real_norm = discovery.ground_answer, discovery.normalize_answer

    def spy_ground(goal, answer, observations, **kw):
        out = real_ground(goal, answer, observations, **kw)
        trace.append(("ground", answer, out))
        return out

    def spy_norm(goal, answer, observations):
        out = real_norm(goal, answer, observations)
        trace.append(("normalize", answer, out))
        return out

    discovery.ground_answer, discovery.normalize_answer = spy_ground, spy_norm
    try:
        reset()
        world(create=[R("Created report.csv successfully.")])
        res, _ = drive("Create report.csv and tell me what happened.", [call("test.nq.create", CREATE)],
                       answer="Created report.csv successfully. Created report.csv successfully.",
                       subgoals=[sg("Create report.csv"), sg("Tell me what happened", "answer")])
        composer = list(trace)
        trace.clear()
        reset()
        world(create=[R("Created report.csv successfully.")])
        res2, _ = drive("Create report.csv.", [call("test.nq.create", CREATE), done("Created report.csv successfully. Created report.csv successfully.")])
        planner = list(trace)
    finally:
        discovery.ground_answer, discovery.normalize_answer = real_ground, real_norm

    def ordered(t: list[tuple[str, str, str]]) -> bool:
        return [k for k, _, _ in t] == ["ground", "normalize"] and t[1][1] == t[0][2] and t[1][2] == "Created report.csv successfully."

    check("F1: composer route — ground_answer runs first, normalize_answer receives exactly its output",
          ordered(composer) and res.summary == "Created report.csv successfully.", str(composer))
    check("F2: planner `done` route — the same order and hand-over",
          ordered(planner) and res2.summary == "Created report.csv successfully.", str(planner))

    reset()
    world(delete=[R("Attempting to delete report.csv.")])
    res3, _ = drive("Delete report.csv.", [call("test.nq.delete", DEL), done("I deleted report.csv. I deleted report.csv.")])
    check("F3: an ungrounded claim is not laundered by normalization — the fail-closed message stands, no 'deleted' claim comes back",
          "insufficient" in low(res3.summary) and not re.search(r"\b(?:deleted|removed)\b", low(res3.summary)), res3.summary)
    reset()


# ================================================================================
# G — properties over a seeded random corpus
# ================================================================================

POOL = [
    "Attempting to delete report.csv.", "Searching for report.csv...", "Working on it...", "Opening report.csv...",
    "Deleted report.csv successfully.", "Found report.csv.", "report.csv contains 42 rows.", "report.csv has 43 rows.",
    "Permission denied opening notes.txt.", "Permission denied while deleting report.csv.", "No matching file was found.",
    "Found notes.txt.", "Also: Found report.csv.", "I deleted report.csv successfully.", "It has 42 rows.",
    "The first tool reported success, but the later verification reported failure.", "Yes.", "Counting rows: 12 of 42...",
    "Presumably deleted report.csv.", "Created out.csv successfully.", "Delete failed: permission denied.",
]
GOALS = ["Delete report.csv.", FIND_ROWS, "Find report.csv and notes.txt.", "How many rows does report.csv have?", "Open the notes."]
CORPUS_EV = DELETE_EV + FIND_EV + [
    NOTES_FAIL, obs("files.find", "Found notes.txt.", args={"name": "notes.txt"}),
    obs("files.remove2", "Delete failed: permission denied.", ok=False, args=REPORT),
]
FILE_RE, NUM_RE = discovery._FILENAME_CLAIM_RE, discovery._NUMBER_TOKEN_RE


def section_g() -> None:
    scenario("G: properties over 400 seeded random answers — removal only, never empty, no new facts, failures kept, idempotent")
    rng = random.Random(247)
    cases = []
    for _ in range(400):
        text = ""
        for k, sentence in enumerate(rng.choices(POOL, k=rng.randint(2, 7))):
            text += ("" if k == 0 else rng.choice([" ", " ", "\n"])) + sentence
        cases.append((rng.choice(GOALS), text))
    outs = [(g, t, norm(g, t, CORPUS_EV)) for g, t in cases]
    changed = sum(1 for _, t, o in outs if o != t)

    def sentences(text: str) -> list[str]:
        return [text[a:b] for a, b in discovery._sentence_spans(text)]

    def sentence_subsequence(small: list[str], big: list[str]) -> bool:
        it = iter(big)
        return all(s in it for s in small)

    bad_shape = [t for _, t, o in outs if not o.strip() or not discovery._is_subsequence(o, t) or not sentence_subsequence(sentences(o), sentences(t))]
    check(f"G1: every output is a non-empty character subsequence of its input, made of the input's own sentences in order "
          f"({len(outs)} answers, {changed} changed)", not bad_shape and 40 < changed < len(outs), f"{len(bad_shape)} bad; changed={changed}")

    new_facts = [
        (t, o) for _, t, o in outs
        if not set(NUM_RE.findall(o)) <= set(NUM_RE.findall(t))
        or not {m.lower() for m in FILE_RE.findall(o)} <= {m.lower() for m in FILE_RE.findall(t)}
    ]
    check("G2: normalization never introduces a number or a filename", not new_facts, str(new_facts[:2]))

    def unique_own_failures(goal: str, text: str) -> list[str]:
        """Sentences that report a failure about the goal's own file (or name no file), stated once."""
        goal_files = discovery._object_files_of_text(goal)
        found = []
        for s in sentences(text):
            line = discovery._Line(s)
            files = discovery._object_files(line)
            if (discovery._evidence_kind(line) == "failure" and (not files or files & goal_files)
                    and sum(1 for x in sentences(text) if discovery._sentence_words(x) == discovery._sentence_words(s)) == 1):
                found.append(s)
        return found

    lost = [(g, s) for g, t, o in outs for s in unique_own_failures(g, t) if s not in o]
    check("G3: a failure about the goal's own file (or naming no file) that is stated once is never lost", not lost, str(lost[:2]))

    unstable = [(g, o) for g, _, o in outs if norm(g, o, CORPUS_EV) != o]
    check("G4: normalization is idempotent (normalizing an already normalized answer changes nothing)", not unstable, str(unstable[:2]))


def main() -> int:
    t0 = time.perf_counter()
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()
    section_g()
    return H.finish("Phase 24.7 — grounded final-answer quality normalization", time.perf_counter() - t0,
                    min_assertions=28, min_scenarios=7)


if __name__ == "__main__":
    sys.exit(main())
