"""Phase 79 -- an ACTIVE stage that no step ran under is named in the progress summary, not dropped.

`steps._remaining_of` listed only PENDING stages, on the reasoning that an ACTIVE or FAILED one "was attempted and is in `steps`".
That is false for a stage the pointer entered but under which nothing ran (the planner failed on its first decision, or on the
first decision after an advance): it was in neither list, so "Not attempted" left it out. Pure report construction -- no tool
runs, no LLM, no stage-pointer or status change.
"""

from __future__ import annotations

import sys
import time

import iso_env
import phase21_common as H
from phase21_common import check, scenario

from friday import steps  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402

S = SubgoalStatus


def stages(*statuses: SubgoalStatus) -> list[Subgoal]:
    return [Subgoal(id=f"s{i}", description=f"stage {i} work", status=st) for i, st in enumerate(statuses)]


def ran(tool: str, stage: int, ok: bool = True) -> Observation:
    return Observation(PlanStep(tool, {}, subgoal=f"stage {stage} work"), ok, "", {}, error="" if ok else "RuntimeError")


def main() -> int:
    t0 = time.time()
    with iso_env.isolated_runtime():
        scenario("A  planning_failed on the first decision: the entered stage is named, with the untouched one")
        sg = stages(S.ACTIVE, S.PENDING)
        before = [s.status for s in sg]
        r = steps.build_report([], stopped="planning_failed", subgoals=sg)
        check("empty ACTIVE stage is in remaining, in order", r.remaining == ["stage 0 work", "stage 1 work"], str(r.remaining))
        check("the summary says it was not attempted", "Not attempted: stage 0 work; stage 1 work." in r.tail(), r.tail())
        check("no step is invented for it", r.steps == [])
        check("the report does not call it done or recovered", r.outcome == "failed" and "Done" not in r.tail(), r.outcome)
        check("building the report leaves every status unchanged", [s.status for s in sg] == before)

        scenario("B  planning_failed after the pointer advanced: done stage stays out, empty ACTIVE and PENDING are named")
        sg = stages(S.SUCCEEDED, S.ACTIVE, S.PENDING)
        r = steps.build_report([ran("test.a", 0)], stopped="planning_failed", subgoals=sg)
        check("succeeded stage is not listed", "stage 0 work" not in r.remaining, str(r.remaining))
        check("empty ACTIVE + PENDING are listed", r.remaining == ["stage 1 work", "stage 2 work"], str(r.remaining))
        check("the step that did run is still reported", [s.tool for s in r.steps] == ["test.a"])

        scenario("C  an ACTIVE stage that DID run steps is not 'not attempted' (unchanged)")
        sg = stages(S.ACTIVE, S.PENDING)
        r = steps.build_report([ran("test.a", 0)], stopped="planning_failed", subgoals=sg)
        check("ACTIVE stage with a step is not listed", r.remaining == ["stage 1 work"], str(r.remaining))
        sg = stages(S.ACTIVE, S.PENDING)
        r = steps.build_report([ran("test.a", 0, ok=False)], stopped="planning_failed", subgoals=sg)
        check("ACTIVE stage with a failed step is not listed", r.remaining == ["stage 1 work"], str(r.remaining))

        scenario("D  FAILED and PENDING stages are reported as before")
        sg = stages(S.FAILED, S.PENDING)
        r = steps.build_report([ran("test.a", 0, ok=False)], stopped="step_limit", subgoals=sg)
        check("FAILED stage is not listed; PENDING is", r.remaining == ["stage 1 work"], str(r.remaining))

        scenario("E  a step cut off in flight (audit-only) has no stage tag: the active stage is not claimed unattempted")
        sg = stages(S.ACTIVE, S.PENDING)
        r = steps.build_report([], stopped="timeout", subgoals=sg, extra_unknown=["test.slow"])
        check("only the PENDING stage is listed", r.remaining == ["stage 1 work"], str(r.remaining))
        check("the in-flight step is reported as unknown", [s.state for s in r.steps] == [steps.StepState.UNKNOWN])

    return H.finish("Phase 79 empty active stage in progress summary", time.time() - t0, min_assertions=12, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
