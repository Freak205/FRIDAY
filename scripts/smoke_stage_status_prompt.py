"""Phase 78 -- a FAILED stage is not rendered as `pending` in the planner prompt.

Phase 77 found `_build_decision_prompts` tagged every non-current stage that was not SUCCEEDED as
"pending", so a stage that was attempted and ended FAILED read to the planner as never attempted.
This pins the display mapping only (pure prompt construction -- no tool runs, no LLM, no stage-pointer
or status change): succeeded -> done, failed -> failed, untouched -> pending, the current stage -> CURRENT.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import iso_env
import phase21_common as H
from phase21_common import check, scenario

from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.orchestrator import Orchestrator  # noqa: E402


def stages(*statuses: SubgoalStatus) -> list[Subgoal]:
    return [Subgoal(id=f"s{i}", description=f"stage {i} work", status=st) for i, st in enumerate(statuses)]


def tags_of(prompt: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2) for m in re.finditer(r"^\s+(\d+)\. \[(\w+)\] ", prompt, re.M)}


def build(subgoals: list[Subgoal], current: int) -> str:
    o = Orchestrator(llm_provider=None, tool_specs=[], runner=None)
    return o._build_decision_prompts("do four things", [], [], subgoals=subgoals, current_index=current)[1]


def main() -> int:
    t0 = time.time()
    S = SubgoalStatus
    with iso_env.isolated_runtime():
        scenario("A  failed stage behind the pointer is 'failed'; untouched stays 'pending'")
        sg = stages(S.SUCCEEDED, S.FAILED, S.ACTIVE, S.PENDING)
        tags = tags_of(build(sg, 2))
        check("succeeded stage -> done", tags.get(0) == "done", str(tags))
        check("FAILED stage -> failed (not pending)", tags.get(1) == "failed", str(tags))
        check("current stage -> CURRENT", tags.get(2) == "CURRENT", str(tags))
        check("untouched stage -> pending", tags.get(3) == "pending", str(tags))
        check("no stage is mislabelled: exactly one pending", list(tags.values()).count("pending") == 1, str(tags))

        scenario("B  a failed stage ahead of the pointer is also not 'pending'; the display changes nothing")
        sg = stages(S.ACTIVE, S.FAILED, S.PENDING)
        before = [s.status for s in sg]
        tags = tags_of(build(sg, 0))
        check("FAILED stage after the pointer -> failed", tags.get(1) == "failed", str(tags))
        check("PENDING stage after it -> pending", tags.get(2) == "pending", str(tags))
        check("building the prompt leaves every status unchanged", [s.status for s in sg] == before)

        scenario("C  no failed stage: the prompt is unchanged")
        tags = tags_of(build(stages(S.SUCCEEDED, S.ACTIVE, S.PENDING), 1))
        check("done / CURRENT / pending as before", tags == {0: "done", 1: "CURRENT", 2: "pending"}, str(tags))

    return H.finish("Phase 78 stage status in planner prompt", time.time() - t0, min_assertions=8, min_scenarios=3)


if __name__ == "__main__":
    sys.exit(main())
