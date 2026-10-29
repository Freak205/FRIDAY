"""Phase 80 -- an ACTIVE stage that no step ran under is named in "Not attempted" after a restart too.

Phase 79 made the LIVE report name it (it can see each step's stage). Stored progress kept no stage per step, so a rebuild could
not tell that stage from one that had steps and left it out: the same goal said different things before and after a restart. An empty
stage has no step to carry a tag, so the live report now stores the stage IDS it named (`progress["empty_active"]`, ids only -- no
text), and the rebuild names a stored id only while that stage is still ACTIVE and no step is known solely from the audit log.
Progress saved before this has no such key and rebuilds exactly as it always did. Isolated database, no tool runs, no LLM.
"""

from __future__ import annotations

import json
import sys
import time

import iso_env
import phase21_common as H
from phase21_common import check, scenario

from friday import audit, owner, steps  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402

S = SubgoalStatus
DEAD = owner.Identity("fake-dead80", 4_300_001, 1.0, owner._host())


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD


def stages(*statuses: SubgoalStatus) -> list[Subgoal]:
    return [Subgoal(id=f"id-{i}", description=f"stage {i} work", status=st) for i, st in enumerate(statuses)]


def new_goal(sg: list[Subgoal]) -> str:
    g = goals_mod.create("p80 goal", "p80 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus.PARTIAL)
    goals_mod.set_subgoals(g.id, sg)
    return g.id


def audit_row(gid: str) -> int:
    """A finished, successful audit row of goal `gid`, written by a session that no longer exists."""
    with owner._use_identity(DEAD), audit.goal_scope(gid):
        rid = audit.record(actor="text", skill="test.p80_write", tier="L1", args={}, decision="auto")
    audit.complete(rid, ok=True, result="Done.", duration_ms=1)
    return rid


def ran(stage: int, rid: int) -> Observation:
    o = Observation(PlanStep("test.p80_write", {}, subgoal=f"stage {stage} work"), True, "", {}, error="")
    o.audit_ids = [rid]
    return o


def rebuilt(gid: str) -> steps.GoalReport:
    r = steps.reconstruct(gid, probe_dead)
    assert r is not None
    return r


def old_progress(step_list: list[dict] | None = None, **extra) -> dict:
    """Progress as saved before Phase 80: no `empty_active` key."""
    return {"v": 1, "stopped": "planning_failed", "steps": step_list or [], **extra}


def main() -> int:
    t0 = time.time()
    with iso_env.isolated_runtime():
        scenario("A  live report -> stored -> rebuilt: the empty ACTIVE stage is named by both")
        sg = stages(S.ACTIVE, S.PENDING)
        live = steps.build_report([], stopped="planning_failed", subgoals=sg)
        gid = new_goal(sg)
        steps.save_progress(gid, live, "planning_failed")
        re = rebuilt(gid)
        stored = goals_mod.get(gid).progress
        check("live report names the empty ACTIVE stage and the pending one", live.remaining == ["stage 0 work", "stage 1 work"], str(live.remaining))
        check("rebuilt report says the same", re.remaining == live.remaining, f"{re.remaining} VS {live.remaining}")
        check("...and so does the whole account (outcome, steps, tail)",
              (re.outcome, re.to_dict().get("steps"), re.tail()) == (live.outcome, live.to_dict().get("steps"), live.tail()),
              f"{re.to_dict()} VS {live.to_dict()}")
        check("what is stored is the stage id only", stored.get("empty_active") == ["id-0"], str(stored))
        check("no stage text is stored with the progress", "stage 0 work" not in json.dumps(stored))
        check("no step is invented for the empty stage", re.steps == [] and re.outcome == "failed", f"{re.steps} {re.outcome}")
        check("the public report shape is unchanged (the ids are stored, not shown)", "empty_active" not in re.to_dict())
        check("the saved stage statuses are untouched by the rebuild",
              [s.status for s in goals_mod.get(gid).subgoals] == [S.ACTIVE, S.PENDING])

        scenario("B  rewriting the stored progress from a rebuilt report keeps the fact (refresh / settle path)")
        again = steps.progress_of(re, "planning_failed")
        check("progress_of(rebuilt) equals what was stored", again == stored, f"{again} VS {stored}")
        steps.refresh_goals(probe_dead)
        check("refresh_goals leaves it as it was", goals_mod.get(gid).progress == stored and rebuilt(gid).remaining == live.remaining)

        scenario("C  after an advance: done stage out, empty ACTIVE and PENDING named, live and rebuilt alike")
        sg = stages(S.SUCCEEDED, S.ACTIVE, S.PENDING)
        gid = new_goal(sg)
        obs = [ran(0, audit_row(gid))]
        live = steps.build_report(obs, stopped="planning_failed", subgoals=sg)
        steps.save_progress(gid, live, "planning_failed")
        re = rebuilt(gid)
        check("live: stage 1 and 2 named, stage 0 not", live.remaining == ["stage 1 work", "stage 2 work"], str(live.remaining))
        check("rebuilt agrees, and keeps the step that ran", re.remaining == live.remaining and [s.tool for s in re.steps] == ["test.p80_write"],
              f"{re.remaining} {re.steps}")

        scenario("D  an ACTIVE stage that has steps is not 'not attempted' (unchanged), live or rebuilt")
        sg = stages(S.ACTIVE, S.PENDING)
        gid = new_goal(sg)
        live = steps.build_report([ran(0, audit_row(gid))], stopped="planning_failed", subgoals=sg)
        steps.save_progress(gid, live, "planning_failed")
        re = rebuilt(gid)
        check("no empty-stage fact is stored", "empty_active" not in goals_mod.get(gid).progress, str(goals_mod.get(gid).progress))
        check("only the PENDING stage is listed, live and rebuilt", live.remaining == re.remaining == ["stage 1 work"], f"{live.remaining} {re.remaining}")

        scenario("E  compatibility: progress saved before this change (no key) rebuilds as it always did")
        sg = stages(S.ACTIVE, S.PENDING)
        gid = new_goal(sg)
        goals_mod.set_progress(gid, old_progress())
        re = rebuilt(gid)
        check("rebuilds without error; the empty ACTIVE stage is not claimed", re.remaining == ["stage 1 work"] and re.empty_active == [], str(re.remaining))
        check("the rebuilt record is not rewritten with an invented fact", "empty_active" not in steps.progress_of(re, "planning_failed"))
        for label, junk in (("a string", "id-0"), ("a number", 7), ("a dict", {"id-0": 1}), ("non-string ids", [0, None, 1.5]), ("an unknown id", ["id-99"])):
            gid = new_goal(stages(S.ACTIVE, S.PENDING))
            goals_mod.set_progress(gid, old_progress(empty_active=junk))
            check(f"a malformed or unknown value ({label}) is ignored, not trusted", rebuilt(gid).remaining == ["stage 1 work"], str(rebuilt(gid).remaining))

        scenario("F  conservative: a stored id counts only while that stage is still ACTIVE")
        for label, st in (("FAILED", S.FAILED), ("SUCCEEDED", S.SUCCEEDED), ("PENDING", S.PENDING)):
            gid = new_goal(stages(st, S.PENDING))
            goals_mod.set_progress(gid, old_progress(empty_active=["id-0"]))
            re = rebuilt(gid)
            expect = ["stage 0 work", "stage 1 work"] if st is S.PENDING else ["stage 1 work"]
            check(f"stage is now {label}: listed only as pending would have been", re.remaining == expect and re.empty_active == [], str(re.remaining))

        scenario("G  conservative: a step known only from the audit log may have run in that stage, so the stored claim is dropped")
        sg = stages(S.ACTIVE, S.PENDING)
        gid = new_goal(sg)
        goals_mod.set_progress(gid, old_progress(empty_active=["id-0"]))
        audit_row(gid)  # a step that ran after the checkpoint; the progress does not know it
        re = rebuilt(gid)
        check("the empty-stage claim is not applied", "stage 0 work" not in re.remaining and re.empty_active == [], str(re.remaining))
        check("the unlinked step is still reported from the audit log", [s.tool for s in re.steps] == ["test.p80_write"], str(re.steps))

        scenario("H  a run cut off with a step in flight stores no claim about its stage")
        sg = stages(S.ACTIVE, S.PENDING)
        live = steps.build_report([], stopped="timeout", subgoals=sg, extra_unknown=["test.slow"])
        check("live names only the pending stage, and stores no empty-stage id", live.remaining == ["stage 1 work"] and live.empty_active == [])
        check("nothing for the rebuild to repeat", "empty_active" not in steps.progress_of(live, "timeout"))

    return H.finish("Phase 80 empty active stage survives a restart", time.time() - t0, min_assertions=28, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(main())
