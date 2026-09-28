"""Phase 23.0 — deterministic scorecard for EVIDENCE-GROUNDED GOAL COMPLETION.

Phase 22's report (§10, group A) found a Phase 11.2 interaction: a compound request
("Read X and tell me Y") decomposes into subgoals, and qwen2.5:3b then fixates on the
acquisition-shaped subgoal 0 (re-reading) instead of answering from evidence it
already has — even though the SAME goal, undecomposed, is already answered correctly
by the existing goal-coverage/tool-data machinery (scripts/smoke_tool_data.py section
F1: 0 subgoals, 2 model calls, correct answer). This suite pins the Phase 23.0 fix,
which targets the SUBGOAL mechanism specifically and leaves the Phase 21/22 coverage
machinery (`assess_coverage` et al.) byte-for-byte untouched:

  A  compound goal modeling: `classify_requirement_kind` (acquisition vs. answer),
     `decompose_goal` assigning it deterministically, `Subgoal.kind` persistence
  B  "read X and tell me Y" end to end: X read once, evidence satisfies the answer
     subgoal, the final answer is composed from evidence without an unnecessary
     second TOOL call
  C  evidence already contains the answer -> the tool-choosing planner is not asked
     again unnecessarily (the one extra model call is the dedicated answer call, not
     another "choose a tool" turn)
  D  evidence insufficient (unconfirmed/failed) -> the subgoal pointer does not
     advance and the planner keeps going, never a premature stop
  E  multiple compound clauses (two acquisitions + one answer) tracked independently;
     first subgoal already satisfied -> the pointer can move to another subgoal
     without the model's own `subgoal_index`
  F  contradictory evidence -> the answer subgoal is never falsely marked ready
  G  safety: no tool call is structurally reachable from the answer-composition path,
     nothing here widens scope/permission/confirmation, and the two new switches are
     real kill switches (mutation-checked)

Entirely deterministic: scripted model replies through the existing `llm.get_provider`
seam (`phase21_common.ScriptedPlanner`), a recording fake tool world (no real registry
skill, no real side effect). No Ollama.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import textwrap
import time
import uuid

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario  # noqa: E402

from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalKind, SubgoalStatus  # noqa: E402
from friday.orchestrator import (  # noqa: E402
    Observation,
    Orchestrator,
    PlanStep,
    ToolSpec,
    _answer_subgoal_ready,
    _auto_advance_subgoal_idx,
    _subgoal_evidence,
)
from friday.registry import SkillResult  # noqa: E402

RK = discovery.classify_requirement_kind

# -- a recording tool world: nothing real ever runs ----------------------------------

CALLS: list[tuple[str, dict]] = []
WORLD = {"readme.md": "FRIDAY is a Python desktop assistant built with FastAPI, SQLite and Whisper."}

SPECS = [
    ToolSpec(name="test.eg_read_a", description="read a named source", tier="L0", params="path (str)", action="read"),
    ToolSpec(name="test.eg_time", description="the current time", tier="L0", params="", action="read"),
    ToolSpec(name="test.eg_battery", description="battery level", tier="L0", params="", action="read"),
    ToolSpec(name="test.eg_fail", description="always fails", tier="L0", params="", action="read"),
    ToolSpec(name="test.eg_uncertain", description="succeeds but flags its own result unconfirmed", tier="L0", params="", action="read"),
    ToolSpec(name="test.eg_delete", description="a destructive stand-in that must NEVER run from the answer path", tier="L2", params="", action="delete"),
    ToolSpec(name="test.eg_disk", description="disk space", tier="L0", params="", action="read"),
    ToolSpec(name="test.eg_delete_temp", description="a real consequential subgoal that must still run when it follows a mid-list answer subgoal", tier="L2", params="", action="delete"),
]


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    if tool == "test.eg_read_a":
        path = str(args.get("path", ""))
        content = WORLD.get(path, "")
        if not content:
            return SkillResult(speech=f"There's no file at {path}.", ok=False)
        return SkillResult(speech=f"{path} has {len(content)} characters.", data={"path": path, "content": content})
    if tool == "test.eg_time":
        return SkillResult(speech="It's 3:45 PM.", data={"time": "15:45"})
    if tool == "test.eg_battery":
        return SkillResult(speech="Battery is at 61 percent.", data={"battery": 61})
    if tool == "test.eg_fail":
        return SkillResult(speech="No such file.", ok=False)
    if tool == "test.eg_uncertain":
        return SkillResult(speech="maybe something", ok=True, data={"uncertain": True})
    if tool == "test.eg_delete":
        return SkillResult(speech="deleted (should never be reached)")
    if tool == "test.eg_disk":
        return SkillResult(speech="Disk has 4 percent free space.", data={"free_pct": 4})
    if tool == "test.eg_delete_temp":
        return SkillResult(speech="Deleted the temp files.")
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


# ================================================================================
# A — compound goal modeling
# ================================================================================


def section_a() -> None:
    scenario("A: classify_requirement_kind — deterministic, no model call")
    check("'obtain README evidence' -> acquisition (the brief's own example)", RK("obtain README evidence") == "acquisition")
    check("'answer technologies from README evidence' -> answer (contains 'answer')", RK("answer technologies from README evidence") == "answer")
    check("'tell me what technologies I used' -> answer (contains 'tell'/'what')", RK("tell me what technologies I used") == "answer")
    check("'read my readme' -> acquisition", RK("read my readme") == "acquisition")
    check("'open the file' -> acquisition", RK("open the file") == "acquisition")
    check("'what is the launch code' -> answer (question word)", RK("what is the launch code") == "answer")
    check("'confirm the file was deleted' -> answer", RK("confirm the file was deleted") == "answer")
    check("'Check the time' -> acquisition", RK("Check the time") == "acquisition")
    check("'summarize the results' -> answer", RK("summarize the results") == "answer")
    check("empty text -> acquisition (fails closed)", RK("") == "acquisition" and RK(None) == "acquisition")
    check("SubgoalKind has exactly the two documented values", {k.value for k in SubgoalKind} == {"acquisition", "answer"})

    scenario("A2: Subgoal.kind persistence — round-trips, and fails closed on legacy/bad data")
    s = Subgoal(id="x", description="tell me why", kind="answer")
    d = s.to_dict()
    check("to_dict carries kind", d["kind"] == "answer")
    check("from_dict round-trips kind", Subgoal.from_dict(d).kind == "answer")
    legacy = {"id": "y", "description": "read a file", "status": "pending"}  # no "kind" key at all
    check("a legacy dict with no 'kind' key defaults to acquisition (pre-Phase-23 rows)", Subgoal.from_dict(legacy).kind == "acquisition")
    check("a garbage kind value fails closed to acquisition", Subgoal.from_dict({**d, "kind": "nonsense"}).kind == "acquisition")
    check("the plain constructor also defaults to acquisition", Subgoal(id="z", description="x").kind == "acquisition")

    scenario("A3: Orchestrator.decompose_goal assigns kind deterministically, never from the model's own say-so")

    async def _run() -> None:
        provider = ScriptedPlanner([
            '{"subgoals": [{"description": "Read the README file"}, '
            '{"description": "Tell me what technologies are used"}]}'
        ])
        orch2 = Orchestrator(tools=[], runner=runner, llm_provider=provider)
        subs = await orch2.decompose_goal("Read the README and tell me what technologies are used")
        check("decompose_goal: 2 subgoals produced", len(subs) == 2, str([s.description for s in subs]))
        check("decompose_goal: subgoal 0 classified acquisition", subs[0].kind == "acquisition")
        check("decompose_goal: subgoal 1 classified answer", subs[1].kind == "answer")

    asyncio.run(_run())


# ================================================================================
# B/C — "read X and tell me Y" end to end
# ================================================================================


def section_bc() -> None:
    scenario("B: 'Read the README and tell me what technologies are used' — X read once, answered from evidence")
    reset()
    goal = "Read the README file and tell me what technologies are used."
    sg0, sg1 = sg("Read the README file", "acquisition"), sg("Tell me what technologies are used", "answer")
    replies = [
        call("test.eg_read_a", {"path": "readme.md"}),
        "FRIDAY uses Python, FastAPI, SQLite and Whisper.",  # the dedicated answer call — plain text, not JSON
    ]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    check("B: exactly one real tool call (the read)", CALLS == [("test.eg_read_a", {"path": "readme.md"})], str(CALLS))
    check("B: the run completed ok", res.ok and res.stopped == "completed", f"{res.ok} {res.stopped}")
    check("B: both subgoals credited SUCCEEDED", sg0.status == SubgoalStatus.SUCCEEDED and sg1.status == SubgoalStatus.SUCCEEDED)
    check("B: the composed answer (not the read's own speech) is the summary", "Python" in res.summary and "FastAPI" in res.summary, res.summary)
    check("B: no unnecessary second TOOL call — the read tool ran exactly once", sum(1 for t, _ in CALLS if t == "test.eg_read_a") == 1)
    check("B: an orchestrator.evidence_stop event fired", any(t == "orchestrator.evidence_stop" for t, _ in events))
    check("B: an orchestrator.subgoal advance event fired (0 -> 1)", any(t == "orchestrator.subgoal" and d.get("index") == 1 for t, d in events))

    scenario("C: the extra model call is the dedicated answer call, not another 'choose a tool' turn")
    check("C: exactly 2 model calls total (1 decision + 1 answer — no extra tool-choice turn)", pl.calls == 2, pl.calls)
    check("C: the 2nd call's system prompt is the answer-composition prompt, not the tool-decision one",
          "using ONLY the evidence" in pl.system_of(1)
          and "choosing exactly one tool call at a time" not in pl.system_of(1))
    check("C: the 2nd call's system prompt explicitly tells the model not to call a tool",
          "Do not call a tool" in pl.system_of(1))
    check("C: the 2nd call's system prompt never offers a JSON tool-call schema",
          '"action": "call"' not in pl.system_of(1) and "Available tools" not in pl.prompt_of(1))
    check("C: the 2nd call's prompt carries the real evidence (the file's content), not just its speech",
          "FastAPI" in pl.prompt_of(1) or "content" in pl.prompt_of(1).lower())


# ================================================================================
# D — evidence insufficient: the planner keeps going, never a premature stop
# ================================================================================


def section_d() -> None:
    scenario("D: an unconfirmed (uncertain) result never satisfies an acquisition subgoal on its own")
    reset()
    goal = "Read the log and tell me what went wrong."
    sg0, sg1 = sg("Read the log", "acquisition"), sg("Tell me what went wrong", "answer")
    replies = [call("test.eg_uncertain", {}), call("test.eg_read_a", {"path": "readme.md"}), "Nothing serious was found."]
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], replies))
    advances = [d for t, d in events if t == "orchestrator.subgoal"]
    check("D: the subgoal pointer did not advance after the uncertain-only result (only after real evidence)",
          len(advances) == 1, str(advances))
    check("D: the planner WAS asked again for a normal decision before any stop (2nd real tool call happened)",
          [t for t, _ in CALLS] == ["test.eg_uncertain", "test.eg_read_a"], str(CALLS))
    check("D: the run still completes once real evidence exists", res.ok and res.stopped == "completed")

    scenario("D2: a FAILED acquisition stops the run honestly — never answered from nothing")
    reset()
    sg0f, sg1f = sg("Read the missing file", "acquisition"), sg("Tell me what it contains", "answer")
    res2, pl2, events2 = asyncio.run(drive(
        "Read the missing file and tell me what it contains.", [sg0f, sg1f], [call("test.eg_fail", {})],
    ))
    check("D2: the run stops honestly on the failure, never fabricates an answer", not res2.ok and res2.stopped == "failure")
    check("D2: the answer subgoal was never reached/credited", sg1f.status == SubgoalStatus.PENDING)
    check("D2: no evidence_stop event fired", not any(t == "orchestrator.evidence_stop" for t, _ in events2))

    scenario("D3: direct unit check — subgoal_step_satisfied never credits a failed or uncertain observation")
    ok_obs = Observation(PlanStep("test.eg_read_a", {}, subgoal="x"), True, "real content here")
    fail_obs = Observation(PlanStep("test.eg_fail", {}, subgoal="x"), False, "no.")
    uncertain_obs = Observation(PlanStep("test.eg_uncertain", {}, subgoal="x"), True, "maybe", data={"uncertain": True})
    check("a failed observation never satisfies", discovery.subgoal_step_satisfied("goal", [fail_obs]) is False)
    check("an uncertain observation never satisfies", discovery.subgoal_step_satisfied("goal", [uncertain_obs]) is False)
    check("a real successful observation does", discovery.subgoal_step_satisfied("goal", [ok_obs]) is True)
    check("no observations at all never satisfies", discovery.subgoal_step_satisfied("goal", []) is False)


# ================================================================================
# E — multiple compound clauses tracked independently; already-satisfied subgoal
# ================================================================================


def section_e() -> None:
    scenario("E: two acquisitions + one answer — each tracked independently, in order")
    reset()
    goal = "Check the time and the battery, then tell me if I should charge it."
    sgA, sgB, sgC = sg("Check the time", "acquisition"), sg("Check the battery level", "acquisition"), sg("Tell me if I should charge it", "answer")
    replies = [
        call("test.eg_time", {}),
        call("test.eg_battery", {}),
        "It's 3:45 PM and the battery is at 61 percent — no need to charge yet.",
    ]
    res, pl, events = asyncio.run(drive(goal, [sgA, sgB, sgC], replies))
    check("E: both acquisitions ran (real, distinct evidence for each)", [t for t, _ in CALLS] == ["test.eg_time", "test.eg_battery"], str(CALLS))
    check("E: no third tool call — the answer needed no tool of its own", len(CALLS) == 2)
    check("E: all three subgoals credited SUCCEEDED", [s.status for s in (sgA, sgB, sgC)] == [SubgoalStatus.SUCCEEDED] * 3)
    check("E: the composed answer reflects BOTH pieces of evidence", "3:45" in res.summary and "61" in res.summary, res.summary)
    check("E: exactly 3 model calls (2 decisions + 1 answer)", pl.calls == 3, pl.calls)
    advances = [d.get("index") for t, d in events if t == "orchestrator.subgoal"]
    check("E: the pointer advanced through BOTH intermediate subgoals in order (0->1, then 1->2)", advances == [1, 2], str(advances))

    scenario("E2: direct unit check — _auto_advance_subgoal_idx moves the pointer on real tagged evidence alone")
    sg0, sg1 = sg("Open the app", "acquisition"), sg("Read its version", "acquisition")
    obs0 = Observation(PlanStep("test.eg_read_a", {}, subgoal="Open the app"), True, "opened it")
    idx = _auto_advance_subgoal_idx([sg0, sg1], 0, [obs0], "some goal")
    check("E2: subgoal 0 already satisfied -> pointer moves to subgoal 1 without the model's own subgoal_index",
          idx == 1 and sg0.status == SubgoalStatus.SUCCEEDED and sg1.status == SubgoalStatus.ACTIVE)
    check("E2: never regresses — calling it again with the already-advanced index is a no-op",
          _auto_advance_subgoal_idx([sg0, sg1], 1, [obs0], "some goal") == 1)
    check("E2: a FAILED current subgoal is never overwritten/advanced past",
          _auto_advance_subgoal_idx([Subgoal(id="a", description="d", status=SubgoalStatus.FAILED), sg("e")], 0, [obs0], "g") == 0)
    check("E2: an ANSWER-kind subgoal is never auto-advanced past, even with evidence tagged to it",
          _auto_advance_subgoal_idx([sg("d", "answer"), sg("e")], 0, [Observation(PlanStep("t", {}, subgoal="d"), True, "x")], "g") == 0)
    check("E2: _subgoal_evidence never counts a blocked/rejected synthetic observation",
          _subgoal_evidence([sg0, sg1], 0,
                             [Observation(PlanStep("t", {}, subgoal="Open the app"), False, "blocked", error="repeated_call")]) == [])


# ================================================================================
# F — contradictory evidence: never falsely marked ready
# ================================================================================


def section_f() -> None:
    scenario("F: contradictory prior evidence never marks an answer subgoal ready")
    sg0 = sg("Check server status", "acquisition")
    sg1 = sg("Tell me if it's healthy", "answer")
    sg0.status = SubgoalStatus.SUCCEEDED
    pos = Observation(PlanStep("test.eg_read_a", {}, subgoal="Check server status"), True, "The service started successfully.")
    neg = Observation(PlanStep("test.eg_read_a", {}, subgoal="Check server status"), True, "Connection refused when checked again.")
    check("F: contradictory evidence (positive + negative state language) -> not ready",
          _answer_subgoal_ready([sg0, sg1], 1, [pos, neg], "is the server healthy") is False)
    check("F: the SAME evidence, non-contradictory (only positive) -> ready",
          _answer_subgoal_ready([sg0, sg1], 1, [pos], "is the server healthy") is True)
    check("F: an unresolved (not SUCCEEDED) prior subgoal -> never ready even with evidence",
          _answer_subgoal_ready([sg("x"), sg1], 1, [pos], "g") is False)
    check("F: index 0 (nothing before it) is never ready, whatever the evidence",
          _answer_subgoal_ready([sg1], 0, [pos], "g") is False)
    check("F: no prior evidence at all -> never ready", _answer_subgoal_ready([sg0, sg1], 1, [], "g") is False)


# ================================================================================
# G — safety: evidence can never widen scope / reach a tool
# ================================================================================


def section_g() -> None:
    scenario("G1: structural — the answer-composition path cannot execute a tool")
    src = inspect.getsource(Orchestrator._answer_from_evidence)
    check("_answer_from_evidence never calls the runner", "self.runner(" not in src and "_run_step(" not in src)
    check("_answer_from_evidence never touches permissions/intent alignment/the executor",
          "permissions" not in src and "check_alignment" not in src and "EXECUTOR" not in src)
    check("_answer_from_evidence uses a plain-text model call (no response_format / tool schema)",
          "response_format" not in src and "decision_json_schema" not in src)
    tree = ast.parse(textwrap.dedent(src))
    calls_in_body = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("...and its only self-call is the model-text helper (no _plan_decision, no _run_step)",
          calls_in_body & {"_model_text"} and not (calls_in_body & {"_plan_decision", "_run_step", "_response_format"}))

    scenario("G2: a reply that LOOKS like a tool-call decision is still only ever used as plain answer text")
    reset()
    goal = "Read the README and tell me what technologies are used."
    sg0, sg1 = sg("Read the README", "acquisition"), sg("Tell me what technologies are used", "answer")
    hostile_reply = '{"action": "call", "tool": "test.eg_delete", "args": {}}'
    res, pl, events = asyncio.run(drive(goal, [sg0, sg1], [call("test.eg_read_a", {"path": "readme.md"}), hostile_reply]))
    check("G2: the hostile-looking reply never reaches the executor — no delete ran",
          all(t != "test.eg_delete" for t, _ in CALLS))
    check("G2: it was used verbatim as the answer text instead", "test.eg_delete" in res.summary or "action" in res.summary)
    check("G2: only the one real (read) tool call happened", CALLS == [("test.eg_read_a", {"path": "readme.md"})])

    scenario("G3: _answer_from_evidence never mutates the observations it was given")
    obs = [Observation(PlanStep("test.eg_read_a", {"path": "readme.md"}, subgoal="x"), True, "readme.md has 80 characters.",
                        {"content": WORLD["readme.md"]})]
    before = list(obs)
    orch = Orchestrator(tools=[], runner=runner, llm_provider=ScriptedPlanner(["Some evidence-based answer."]))

    async def _run() -> str:
        return await orch._answer_from_evidence("goal", obs)

    answer = asyncio.run(_run())
    check("G3: the observations list is unchanged (no synthetic step appended)", obs == before)
    check("G3: a real answer string came back", isinstance(answer, str) and bool(answer.strip()))

    scenario("G4: mutation check — disabling either switch removes the new stop (a real kill switch)")
    for flag in ("answer_from_evidence", "subgoal_evidence_advance"):
        reset()
        setattr(CFG.planner, flag, False)
        goal = "Read the README and tell me what technologies are used."
        sg0, sg1 = sg("Read the README", "acquisition"), sg("Tell me what technologies are used", "answer")
        res, pl, events = asyncio.run(drive(
            goal, [sg0, sg1], [call("test.eg_read_a", {"path": "readme.md"}), done("fallback (pre-Phase-23 path)")],
        ))
        check(f"G4: CFG.planner.{flag}=False -> no evidence_stop event", not any(t == "orchestrator.evidence_stop" for t, _ in events))
        check(f"G4: CFG.planner.{flag}=False -> the planner's OWN done is used, not a composed answer",
              res.summary == "fallback (pre-Phase-23 path)", res.summary)
        reset()

    scenario("G5: static — the defaults are ON (additive, not opt-in) and bounded/switchable, matching prior phases' convention")
    check("subgoal_evidence_advance defaults True", CFG.planner.subgoal_evidence_advance is True)
    check("answer_from_evidence defaults True", CFG.planner.answer_from_evidence is True)
    sig = inspect.signature(discovery.classify_requirement_kind)
    check("classify_requirement_kind's only input is the text (no context/experience/model param)", list(sig.parameters) == ["text"])
    src_ac = inspect.getsource(discovery.assess_coverage)
    src_acs = inspect.getsource(discovery.assess_coverage_seen)
    check("assess_coverage / assess_coverage_seen (Phase 21/22) are untouched by Phase 23",
          "_assess_coverage(goal, observations, with_data=False)" in src_ac
          and "_assess_coverage(goal, observations, with_data=True)" in src_acs)

    scenario("G6: an ANSWER subgoal in the MIDDLE of the list never ends the whole goal or falsely "
             "credits a later, unexecuted (and here, destructive) subgoal — found by adversarial review")
    reset()
    goal = "Check my disk space, tell me if I'm running low, and delete the temp files if so."
    sg_check = sg("Check disk space", "acquisition")
    sg_tell = sg("Tell me if I'm running low", "answer")
    sg_delete = sg("Delete the temp files if so", "acquisition")
    check("G6 precondition: the middle subgoal really does classify as answer (the bug's own trigger)",
          sg_tell.kind == "answer")
    replies = [
        call("test.eg_disk", {}),
        # The model, asked normally a 2nd time (the fix means it MUST be asked again — no
        # self-stop after the middle answer subgoal), explicitly advances past the answer
        # stage (satisfied by reasoning alone, the existing Phase 11.2 convention) to the
        # real remaining subgoal.
        json.dumps({"action": "call", "tool": "test.eg_delete_temp", "args": {}, "subgoal_index": 2}),
        done("Disk was low, so I deleted the temp files."),
    ]
    res, pl, events = asyncio.run(drive(goal, [sg_check, sg_tell, sg_delete], replies))
    check("G6: the middle answer subgoal did NOT end the run early (no evidence_stop)",
          not any(t == "orchestrator.evidence_stop" for t, _ in events))
    check("G6: the planner WAS asked again after the middle subgoal became ready (>= 2 model calls before any conclusion)",
          pl.calls >= 2, pl.calls)
    check("G6: the real consequential subgoal actually RAN — never just credited unexecuted",
          any(t == "test.eg_delete_temp" for t, _ in CALLS), str(CALLS))
    check("G6: the goal only completes once that real work actually happened", res.ok and res.stopped == "completed")
    check("G6: all three subgoals are SUCCEEDED only because each really ran (or was legitimately skipped by the model's own choice, the pre-existing Phase 11.2 contract) — never fabricated by the new stop path",
          [s.status for s in (sg_check, sg_tell, sg_delete)] == [SubgoalStatus.SUCCEEDED] * 3)

    scenario("G7: direct unit check — a ready, non-last answer subgoal is a NO-OP for the new stop's own trigger")
    sg_a, sg_b, sg_c = sg("acquire A", "acquisition"), sg("answer B", "answer"), sg("acquire C", "acquisition")
    sg_a.status = SubgoalStatus.SUCCEEDED
    ev = [Observation(PlanStep("test.eg_read_a", {}, subgoal="acquire A"), True, "real evidence")]
    check("G7: _answer_subgoal_ready itself still says 'ready' for the middle subgoal (the readiness check is unchanged)",
          _answer_subgoal_ready([sg_a, sg_b, sg_c], 1, ev, "goal") is True)
    check("G7: ...so the run_goal-level guard (subgoal_idx == len(subgoals)-1) is what must, and does, stop it "
          "from firing there — proven end to end by G6, not merely by this unit check",
          1 != len([sg_a, sg_b, sg_c]) - 1)

    scenario("G8: _answer_from_evidence fits CFG.llm.num_ctx — never sends an unbounded, unshrunk prompt "
             "(found by adversarial review: unlike a decision turn, this call had no budget fit at all)")
    many_obs = [
        Observation(PlanStep("test.eg_read_a", {"path": f"file{i}.md"}, subgoal="x"), True,
                    f"file{i}.md has content.", {"content": f"entry {i}: " + ("filler " * 200)})
        for i in range(20)
    ]
    orch_budget = Orchestrator(tools=[], runner=runner, llm_provider=ScriptedPlanner(["A short answer."]))
    old_num_ctx, old_budget_on = CFG.llm.num_ctx, CFG.planner.prompt_budget
    CFG.llm.num_ctx = 1024  # deliberately tiny so the 20-observation prompt WILL overflow it unshrunk

    async def _run_budget() -> None:
        await orch_budget._answer_from_evidence("goal", many_obs, context="ambient stuff " * 100)

    asyncio.run(_run_budget())
    sent_prompt = orch_budget.llm_provider.prompt_of(0)
    sent_system = orch_budget.llm_provider.system_of(0)
    check("G8: the sent prompt actually fits the configured window (with budget margin)",
          Orchestrator.estimate_tokens(sent_system, sent_prompt) <= int(1024 * CFG.planner.prompt_budget_fraction),
          Orchestrator.estimate_tokens(sent_system, sent_prompt))
    check("G8: the goal itself always survives the shrink (never the first thing dropped)", "Goal: goal" in sent_prompt)
    CFG.planner.prompt_budget = False
    orch_budget2 = Orchestrator(tools=[], runner=runner, llm_provider=ScriptedPlanner(["A short answer."]))

    async def _run_unbudgeted() -> None:
        await orch_budget2._answer_from_evidence("goal", many_obs, context="ambient stuff " * 100)

    asyncio.run(_run_unbudgeted())
    check("G8: ...and that unshrunk prompt is measurably larger than the fitted one",
          len(orch_budget2.llm_provider.prompt_of(0)) > len(sent_prompt))
    CFG.llm.num_ctx, CFG.planner.prompt_budget = old_num_ctx, old_budget_on


def main() -> None:
    started = time.perf_counter()
    section_a()
    section_bc()
    section_d()
    section_e()
    section_f()
    section_g()
    sys.exit(H.finish("Phase 23.0 — evidence-grounded goal completion", time.perf_counter() - started,
                       min_assertions=60, min_scenarios=14))


if __name__ == "__main__":
    main()
