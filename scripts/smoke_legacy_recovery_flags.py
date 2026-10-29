"""Phase 85 -- a stale `recovered` flag on a contradicted read-back is not trusted when the stored evidence already rules it out.

Before Phase 84 a failed step was `recovered` by any later equivalent success. Phase 84 made a CONTRADICTED READ-BACK (the tool said yes,
the read-back said no) recoverable only through a later step that was itself VERIFIED, like the goal verdict. Goals stored before that keep
the old flag, and `steps.reconstruct` restored it as written.

What a rebuild can and cannot tell: the stored verdict separates a contradicted read-back from a plain tool failure, and each later step's
verified state is stored -- but arguments are not (the audit row keeps a sanitized form), so "the same objective" cannot be rebuilt.
`reconstruct` therefore drops the flag only when the Phase 84 rule is impossible whatever the arguments were: a contradicted read-back with
NO later step verified (as stored or as rebuilt). With a verified later step the flag stays (it may be right; the evidence to say otherwise is
not durable). A plain tool failure is never touched, and the callers that write progress back (`refresh_goals`, `settle_interrupted_goals`)
opt out, so a derived correction is never persisted over history.

Synthetic goals and audit rows in a throwaway database (no tool, no LLM, no data/).
"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import check, scenario  # noqa: E402

from friday import audit, owner, steps, store  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402

DEAD_ID = owner.Identity("fake-dead85", 4_200_085, 1.0, owner._host())
W = "files.write"
VER_BAD = {"status": "failed", "checks": [["fixture effect", False]]}
VER_OK = {"status": "verified", "checks": [["fixture effect", True]]}
VER_NO_FALSE = {"status": "failed", "checks": [["fixture effect", None]]}   # failed status, but no check actually contradicted


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD if pid == DEAD_ID.pid else owner.UNKNOWN


def goal_with(spec: list[dict], status: str = "failed") -> str:
    """spec: one dict per stored step: tool, state, row (ok | fail | None for no audit row), ver, recovered."""
    g = goals_mod.create("p85 goal", "p85 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
    stored = []
    for n, s in enumerate(spec, 1):
        ids: list[int] = []
        if s.get("row"):
            with owner._use_identity(DEAD_ID), audit.goal_scope(g.id):
                rid = audit.record(actor="text", skill=s["tool"], tier="L1", args={}, decision="auto")
            if s["row"] == "ok":
                audit.complete(rid, ok=True, result="Wrote the file.", duration_ms=1)
            else:
                audit.complete(rid, ok=False, result="Could not.", error="boom", duration_ms=1)
            ids = [rid]
        step = {"n": n, "tool": s["tool"], "state": s["state"], "audit": ids}
        if s.get("ver"):
            step["ver"] = s["ver"]
        if s.get("recovered"):
            step["recovered"] = True
        stored.append(step)
    goals_mod.set_progress(g.id, {"v": 1, "stopped": "completed", "steps": stored})
    return g.id


def contra(recovered: bool = True) -> dict:
    return {"tool": W, "state": "failed", "row": "ok", "ver": VER_BAD, "recovered": recovered}   # the row only repeats the tool's "yes"


def completed(tool: str = W) -> dict:
    return {"tool": tool, "state": "completed", "row": "ok"}


def verified(tool: str = W) -> dict:
    return {"tool": tool, "state": "verified", "row": "ok", "ver": VER_OK}


def flags(gid: str, **kw) -> list[tuple[str, bool]]:  # noqa: ANN003
    r = steps.reconstruct(gid, probe_dead, **kw)
    return [(s.state.value, s.recovered) for s in r.steps]


def stored(gid: str) -> str:
    return json.dumps(goals_mod.get(gid).progress, sort_keys=True)


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        scenario("A  contradicted read-back, stale flag, a retry that was NOT verified: the flag is dropped")
        gid = goal_with([contra(), completed()])
        check("legacy flag dropped: failed, not recovered, then completed", flags(gid) == [("failed", False), ("completed", False)], str(flags(gid)))
        r = steps.reconstruct(gid, probe_dead)
        check("the account says it failed and is not complete", "Failed:" in r.tail() and r.outcome in ("partial", "failed"), f"{r.outcome} {r.tail()}")
        gid = goal_with([contra(), {"tool": W, "state": "unknown", "row": None}])
        check("a retry whose outcome is unknown recovers nothing either", flags(gid)[0] == ("failed", False), str(flags(gid)))
        gid = goal_with([contra()])
        check("no later step at all: dropped", flags(gid) == [("failed", False)], str(flags(gid)))
        gid = goal_with([verified(), contra(), completed()])
        check("a verified step BEFORE the failure does not count", flags(gid)[1] == ("failed", False), str(flags(gid)))
        gid = goal_with([contra(), {"tool": W, "state": "failed", "row": "fail"}])
        check("a later failed step recovers nothing", flags(gid)[0] == ("failed", False), str(flags(gid)))

        scenario("B  contradicted read-back, stale flag, a later VERIFIED step: the flag stays (the objective is not durable)")
        gid = goal_with([contra(), verified()])
        check("flag kept with a verified later step", flags(gid) == [("failed", True), ("verified", False)], str(flags(gid)))
        r = steps.reconstruct(gid, probe_dead)
        check("the account shows the recovery: Done, no Failed", "Failed:" not in r.tail() and r.outcome == "complete_unverified", f"{r.outcome} {r.tail()}")
        gid = goal_with([contra(), {"tool": W, "state": "verified", "row": "ok"}])   # stored verified, its verdict gone: rebuilt as completed
        got = flags(gid)
        check("stored 'verified' whose evidence is gone is rebuilt as completed, yet still blocks the drop (no inference from a downgrade)",
              got == [("failed", True), ("completed", False)], str(got))

        scenario("C  a plain tool failure keeps its Phase 54 recovery")
        gid = goal_with([{"tool": W, "state": "failed", "row": "fail", "recovered": True}, completed()])
        check("plain failure + later completed equivalent: still recovered", flags(gid) == [("failed", True), ("completed", False)], str(flags(gid)))
        gid = goal_with([{"tool": W, "state": "failed", "row": None, "recovered": True}, completed()])
        check("plain failure that never reached the Executor: still recovered", flags(gid)[0] == ("failed", True), str(flags(gid)))

        scenario("D  each failure is judged on its own later steps")
        gid = goal_with([contra(), verified(), contra(), completed()])
        check("the first has a verified step after it (kept), the second has none (dropped)",
              [f[1] for f in flags(gid)] == [True, False, False, False], str(flags(gid)))
        gid = goal_with([contra(), contra(), verified()])
        check("both are followed by a verified step: both kept", [f[1] for f in flags(gid)] == [True, True, False], str(flags(gid)))

        scenario("E  nothing is dropped without a positive contradiction, and nothing is created")
        gid = goal_with([{"tool": W, "state": "failed", "row": "fail", "ver": VER_NO_FALSE, "recovered": True}, completed()])
        check("a failed verdict with no explicitly False check is not a contradiction: flag as stored", flags(gid)[0] == ("failed", True), str(flags(gid)))
        gid = goal_with([contra(recovered=False), verified()])
        check("a contradicted failure that was NOT flagged is never made recovered", flags(gid) == [("failed", False), ("verified", False)], str(flags(gid)))
        gid = goal_with([contra(recovered=False), completed()])
        check("progress already stored under Phase 84 (flag false) rebuilds as stored", flags(gid) == [("failed", False), ("completed", False)], str(flags(gid)))
        gid = goal_with([completed(), completed()])
        check("steps that did not fail are untouched", flags(gid) == [("completed", False), ("completed", False)], str(flags(gid)))

        scenario("F  reconstruction never changes the stored record, and nothing writes the correction back")
        gid = goal_with([contra(), completed()])
        before = stored(gid)
        snapshot = copy.deepcopy(goals_mod.get(gid).progress)
        steps.reconstruct(gid, probe_dead)
        check("reconstruct leaves the stored progress byte-identical", stored(gid) == before)
        check("the stale flag is still in the stored input", goals_mod.get(gid).progress["steps"][0].get("recovered") is True and goals_mod.get(gid).progress == snapshot)
        check("idempotent: the same account twice", flags(gid) == flags(gid))
        check("reconcile_recovery=False restores the flag as stored (the callers that write)", flags(gid, reconcile_recovery=False)[0] == ("failed", True))
        rows_before = store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        steps.refresh_goals(probe_dead)
        check("refresh_goals does not persist the correction: stored progress unchanged", stored(gid) == before, stored(gid))
        check("... and adds no audit row", store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == rows_before)
        gid2 = goal_with([contra(), completed()], status="running")
        before2 = stored(gid2)
        steps.settle_interrupted_goals(probe_dead)
        stored2 = goals_mod.get(gid2).progress["steps"]
        check("settle_interrupted_goals keeps the stored flag too", stored2[0].get("recovered") is True, str(stored2))
        del before2
        check("the read path (describe_goal's reconstruct) still corrects it",
              [s.recovered for s in steps.reconstruct(gid2, probe_dead).steps][0] is False)

    return H.finish("Phase 85 legacy recovery flags", time.perf_counter() - t0, min_assertions=25, min_scenarios=6)


if __name__ == "__main__":
    sys.exit(main())
