"""Phase 82 -- a plan step is attributed to its stage by the stage's stable id, not by its description.

Before: `PlanStep.subgoal` held the stage's description and `steps.stage_standing` / `orchestrator._subgoal_evidence` matched on that
text, so two stages with the same wording each claimed the OTHER's steps (a failure under one made the other "unresolved", evidence for
one advanced the pointer past both, an empty ACTIVE stage was hidden by a sibling's step). Now `run_goal` also tags each step with
`Subgoal.id` (`PlanStep.subgoal_id`) and `steps.step_in_stage` matches by id; an untagged (legacy) step keeps the description match.

Pure functions on hand-built observations, plus `Orchestrator.run_goal` over a scripted planner and a recording fake tool world (nothing
real runs; no LLM, no data/).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import iso_env
import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import steps  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
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

S = SubgoalStatus
DESC = "check the time"


def dup(*statuses: SubgoalStatus, kinds: tuple[str, ...] = ()) -> list[Subgoal]:
    """Stages that ALL share one description, told apart only by id."""
    return [
        Subgoal(id=f"id{i}", description=DESC, status=st, kind=kinds[i] if i < len(kinds) else "acquisition")
        for i, st in enumerate(statuses)
    ]


def obs(tool: str, stage: Subgoal | None, *, tagged: bool = True, ok: bool = True, data: dict | None = None) -> Observation:
    """An executed step under `stage`. tagged=False builds it the pre-Phase-82 way: description only, no id."""
    step = PlanStep(tool, {}, subgoal=stage.description if stage else "", subgoal_id=stage.id if stage and tagged else "")
    return Observation(step, ok, "It's 3:45 PM." if ok else "no", data if data is not None else {}, error="" if ok else "RuntimeError")


# -- a recording fake tool world for the run_goal scenario ------------------------------------------------------------------------

CALLS: list[str] = []
SPECS = [
    ToolSpec(name="test.si_a", description="read a", tier="L0", params="", action="read"),
    ToolSpec(name="test.si_b", description="read b", tier="L0", params="", action="read"),
]


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append(tool)
    return SkillResult(speech=f"{tool} ok", data={"time": "15:45"})


def main() -> int:
    t0 = time.time()
    with iso_env.isolated_runtime():
        scenario("A  same description, different ids: a step tagged for one stage is not evidence for the other")
        a, b = dup(S.ACTIVE, S.PENDING)
        failed_a = obs("test.x", a, ok=False)
        check("step_in_stage: own stage", steps.step_in_stage(failed_a.step, a, [a, b]))
        check("step_in_stage: NOT the sibling with the same wording", not steps.step_in_stage(failed_a.step, b, [a, b]))
        check("stage_standing: tagged failure -> its stage unresolved, sibling untouched",
              steps.stage_standing([failed_a], [a, b]) == [steps.UNRESOLVED, steps.UNTOUCHED],
              str(steps.stage_standing([failed_a], [a, b])))
        ok_b = obs("test.x", b)
        check("each stage is credited from its own steps only",
              steps.stage_standing([failed_a, ok_b], [a, b]) == [steps.UNRESOLVED, steps.RESOLVED],
              str(steps.stage_standing([failed_a, ok_b], [a, b])))
        check("_subgoal_evidence: stage 0 sees its step, stage 1 sees none",
              _subgoal_evidence([a, b], 0, [failed_a]) == [failed_a] and _subgoal_evidence([a, b], 1, [failed_a]) == [])
        check("a step with an id that no stage carries belongs to none (a matching description is not evidence)",
              steps.stage_standing([obs("test.x", Subgoal(id="gone", description=DESC))], [a, b]) == [steps.UNTOUCHED] * 2)
        check("an id carried by two stages is ambiguous: falls back to the description (both match, as before)",
              steps.stage_standing([obs("test.x", Subgoal(id="same", description=DESC))],
                                   [Subgoal(id="same", description=DESC), Subgoal(id="same", description=DESC)])
              == [steps.RESOLVED, steps.RESOLVED])

        scenario("B  progression: evidence for stage 0 advances to stage 1 only, never through a same-worded sibling")
        goal = "Check the time."
        a, b, c = dup(S.ACTIVE, S.PENDING, S.PENDING)
        ev = [obs("test.time", a, data={"time": "15:45"})]
        idx = _auto_advance_subgoal_idx([a, b, c], 0, ev, goal)
        check("tagged evidence advances 0 -> 1 and stops (stage 1 has none of its own)", idx == 1, f"idx={idx}")
        check("stage 0 SUCCEEDED, stage 1 ACTIVE, stage 2 untouched", [s.status for s in (a, b, c)] == [S.SUCCEEDED, S.ACTIVE, S.PENDING],
              str([s.status.value for s in (a, b, c)]))
        a, b, c = dup(S.ACTIVE, S.PENDING, S.PENDING)
        legacy = [obs("test.time", a, tagged=False, data={"time": "15:45"})]
        idx = _auto_advance_subgoal_idx([a, b, c], 0, legacy, goal)
        check("LEGACY untagged evidence keeps the old description match: it credits every same-worded stage", idx == 2, f"idx={idx}")

        scenario("C  answer readiness: evidence tagged for the ANSWER stage itself is not 'prior' evidence for a same-worded stage")
        a, ans = dup(S.SUCCEEDED, S.ACTIVE, kinds=("acquisition", "answer"))
        own_read = obs("test.time", a, data={"time": "15:45"})
        check("evidence tagged for stage 0 makes the answer stage ready",
              _answer_subgoal_ready([a, ans], 1, [own_read], goal))
        stray = obs("test.time", ans, data={"time": "15:45"})
        check("evidence tagged for the answer stage does NOT count as stage 0's", not _answer_subgoal_ready([a, ans], 1, [stray], goal))
        legacy_stray = obs("test.time", ans, tagged=False, data={"time": "15:45"})
        check("legacy (untagged) same-text evidence still does, as before", _answer_subgoal_ready([a, ans], 1, [legacy_stray], goal))
        a, ans = dup(S.SUCCEEDED, S.ACTIVE, kinds=("acquisition", "answer"))
        check("an earlier stage not SUCCEEDED still blocks readiness (unchanged)",
              not _answer_subgoal_ready([Subgoal(id="x", description=DESC, status=S.FAILED), ans], 1, [own_read], goal))

        scenario("D  legacy steps without subgoal_id keep the description fallback")
        a, b = dup(S.ACTIVE, S.PENDING)
        leg = obs("test.x", a, tagged=False)
        check("PlanStep() has no id by default", PlanStep("t").subgoal_id == "" and PlanStep("t", {}, "d").subgoal == "d")
        check("untagged step matches both same-worded stages", steps.stage_standing([leg], [a, b]) == [steps.RESOLVED, steps.RESOLVED])
        legacy_fail = obs("test.x", a, tagged=False, ok=False)
        check("untagged failure marks both unresolved (old behaviour, not 'fixed' for legacy)",
              steps.stage_standing([legacy_fail], [a, b]) == [steps.UNRESOLVED, steps.UNRESOLVED])
        check("an untagged step with no stage text matches no stage", steps.stage_standing([obs("test.x", None)], [a, b]) == [steps.UNTOUCHED] * 2)

        scenario("E  empty ACTIVE stage with duplicate descriptions is still reported")
        a, b = dup(S.SUCCEEDED, S.ACTIVE)
        r = steps.build_report([obs("test.x", a)], stopped="planning_failed", subgoals=[a, b])
        check("the step ran under stage 0, so the ACTIVE same-worded stage 1 is 'not attempted'", r.remaining == [DESC], str(r.remaining))
        check("its id is recorded in empty_active", r.empty_active == [b.id], str(r.empty_active))
        check("the step that ran is still reported once", [s.tool for s in r.steps] == ["test.x"])
        a, b = dup(S.SUCCEEDED, S.ACTIVE)
        r = steps.build_report([obs("test.x", a, tagged=False)], stopped="planning_failed", subgoals=[a, b])
        check("legacy untagged step: unchanged (the sibling counts as touched, so the stage is not named; only the generic fallback)",
              DESC not in r.remaining and r.empty_active == [], str((r.remaining, r.empty_active)))
        a, b = dup(S.ACTIVE, S.PENDING)
        r = steps.build_report([obs("test.x", b)], stopped="planning_failed", subgoals=[a, b])
        check("an ACTIVE stage whose sibling's step is tagged elsewhere is reported (and only it, plus the PENDING one)",
              r.empty_active == [a.id] and r.remaining == [DESC, DESC], str((r.empty_active, r.remaining)))

        scenario("F  unique descriptions behave exactly as before, tagged or not")
        u = [Subgoal(id=f"u{i}", description=f"stage {i}", status=st) for i, st in enumerate((S.ACTIVE, S.PENDING, S.PENDING))]
        for tagged in (True, False):
            os_ = [obs("test.x", u[0], tagged=tagged, ok=False), obs("test.x", u[1], tagged=tagged)]
            check(f"standing is the same (tagged={tagged})",
                  steps.stage_standing(os_, u) == [steps.UNRESOLVED, steps.RESOLVED, steps.UNTOUCHED],
                  str(steps.stage_standing(os_, u)))
            check(f"_subgoal_evidence is the same (tagged={tagged})",
                  _subgoal_evidence(u, 0, os_) == [os_[0]] and _subgoal_evidence(u, 2, os_) == [])
        check("out-of-range index -> no evidence", _subgoal_evidence(u, 9, []) == [] and _subgoal_evidence(u, -1, []) == [])

        scenario("G  run_goal tags every new step with the current stage's id (and keeps the description)")
        CFG.planner.structured_output = False
        CFG.planner.subgoal_evidence_advance = False  # only the planner's own pointer moves
        CFG.planner.answer_from_evidence = False
        sg0, sg1 = dup(S.PENDING, S.PENDING)
        replies = [
            call("test.si_a", {}),
            json.dumps({"action": "call", "tool": "test.si_b", "args": {}, "subgoal_index": 1}),
            done("finished"),
        ]
        CALLS.clear()
        orch = Orchestrator(tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=ScriptedPlanner(replies),
                            tool_specs=SPECS, max_steps=6)
        res = asyncio.run(orch.run_goal("Check the time twice.", subgoals=[sg0, sg1]))
        tags = [(o.step.tool, o.step.subgoal, o.step.subgoal_id) for o in res.observations]
        check("both tools ran", CALLS == ["test.si_a", "test.si_b"], str(CALLS))
        check("step 1 carries stage 0's id and description", tags[0] == ("test.si_a", DESC, sg0.id), str(tags))
        check("step 2 (after the jump) carries stage 1's id", tags[1] == ("test.si_b", DESC, sg1.id), str(tags))
        check("each stage sees exactly its own step",
              [o.step.tool for o in _subgoal_evidence([sg0, sg1], 0, res.observations)] == ["test.si_a"]
              and [o.step.tool for o in _subgoal_evidence([sg0, sg1], 1, res.observations)] == ["test.si_b"])
        check("standing: both resolved from their own steps", steps.stage_standing(res.observations, [sg0, sg1]) == [steps.RESOLVED] * 2)

        sg0, sg1 = dup(S.PENDING, S.PENDING)
        CALLS.clear()
        orch = Orchestrator(tools=[s.name for s in SPECS], runner=runner, actor="test",
                            llm_provider=ScriptedPlanner([call("test.si_a", {}), done("finished")]), tool_specs=SPECS, max_steps=6)
        res = asyncio.run(orch.run_goal("Check the time twice.", subgoals=[sg0, sg1]))
        check("steps all under stage 0 -> stage 1 has no evidence and is untouched",
              _subgoal_evidence([sg0, sg1], 1, res.observations) == []
              and steps.stage_standing(res.observations, [sg0, sg1]) == [steps.RESOLVED, steps.UNTOUCHED])
        check("a run without subgoals leaves the tag empty", PlanStep(tool="t").subgoal_id == "")

    return H.finish("Phase 82 stage identity", time.time() - t0, min_assertions=30, min_scenarios=7)


if __name__ == "__main__":
    sys.exit(main())
