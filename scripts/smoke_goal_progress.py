"""Phase 54 -- a multi-step goal's progress survives a restart, and a restart never invents or repeats anything.

Phase 53 gave every executed step a state, but the account lived only in memory: after a crash or a restart "what happened with that
goal?" could only be answered from the audit log. Phase 54 checkpoints, per step, a REFERENCE-AND-VERDICT record in `goals.progress`
(order, tool, state, audit ids, the verification verdict with check names, the recovery flag -- never an argument, a result, a note
or a read-back value) and adds `steps.reconstruct` (read-only; the audit row decides what happened, the stored progress can only
confirm it), `steps.refresh_goals` and `steps.settle_interrupted_goals`. Recovery is stricter too: a failed step is "recovered" only
by a later success on the SAME objective, not by any success of the same tool.

Drives REAL `plan.run` goals (real Executor, real audit, real Goal rows, a scripted model behind the `llm` seam, `test.p54_*` fixture
skills) and crafted audit/goal rows for the restart cases. Nothing real runs and every database is a throwaway.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import ScriptedPlanner, call, check, done, finish, scenario, scripted_provider  # noqa: E402

from friday import audit, auditview, owner, paths, steps, store, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
PEEK: list[dict] = []
FAIL_ALWAYS: set[str] = set()
FAIL_ONCE: set[str] = set()
FAILED_ONCE: set[str] = set()
SECRET = "hunter2-SECRET-body-p54"
SECRET_PW = "pw-SECRET-p54"
CHECK_TEXT = "OBSERVED-DETAIL-p54"
FORBIDDEN_KEYS = {"args", "speech", "result", "note", "observed", "detail"}

DEAD_ID = owner.Identity("fake-dead54", 4_200_001, 1.0, owner._host())
ALIVE_ID = owner.Identity("fake-alive54", 4_200_002, 1.0, owner._host())


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD if pid == DEAD_ID.pid else (owner.ALIVE if pid == ALIVE_ID.pid else owner.UNKNOWN)


def probe_all_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD


def probe_unknown(pid, start, host):  # noqa: ANN001
    return owner.UNKNOWN


def probe_alive(pid, start, host):  # noqa: ANN001
    return owner.ALIVE


@skill(name="test.p54_mkdir", tier="L1", action="modify", description="make a folder (Phase 54 fixture)",
       undo=lambda name="a": {"skill": "test.p54_mkdir_undo", "args": {}})
def mkdir(name: str = "a") -> SkillResult:
    LOG.append(f"mkdir:{name}")
    return SkillResult(speech="Created the folder.")


@skill(name="test.p54_write", tier="L1", action="modify", description="write a file (Phase 54 fixture)")
def write(name: str = "a", text: str = "", retry: int = 0) -> SkillResult:
    LOG.append(f"write:{name}:{retry}")
    if name in FAIL_ALWAYS or (name in FAIL_ONCE and name not in FAILED_ONCE):
        FAILED_ONCE.add(name)
        return SkillResult(speech="Could not write the file: disk full.", ok=False)
    return SkillResult(speech="Wrote the file.")


@skill(name="test.p54_append", tier="L1", action="modify", description="append to a file (Phase 54 fixture)")
def append(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"append:{name}")
    return SkillResult(speech="Appended to the file.")


@skill(name="test.p54_send", tier="L1", action="communicate", description="send a file (Phase 54 fixture)")
def send(name: str = "a") -> SkillResult:
    LOG.append(f"send:{name}")
    return SkillResult(speech="Sent the file.")


@skill(name="test.p54_peek", tier="L0", description="read the running goal's stored progress (Phase 54 fixture)")
async def peek(tag: str = "") -> SkillResult:
    gid = audit._CURRENT_GOAL.get()
    g = goals_mod.get(gid) if gid else None
    PEEK.append({"tag": tag, "gid": gid, "progress": copy.deepcopy(g.progress) if g is not None else None})
    return SkillResult(speech="Looked.")


@skill(name="test.p54_move", tier="L1", action="modify", description="move a path (Phase 54 fixture)")
def move(path: str = "") -> SkillResult:
    return SkillResult(speech="Moved.")


@skill(name="test.p54_secret", tier="L1", action="modify", description="write with sensitive arguments (Phase 54 fixture)")
def secret_write(name: str = "a", text: str = "", password: str = "") -> SkillResult:
    LOG.append(f"secret:{name}")
    if name == "bad":
        return SkillResult(speech="Could not write the file: disk full.", ok=False)
    return SkillResult(speech="Wrote the protected file.")


_POLICY = {
    "test.p54_mkdir": {"name": auditview.KEEP},
    "test.p54_write": {"name": auditview.KEEP, "text": auditview.OMIT},
    "test.p54_append": {"name": auditview.KEEP, "text": auditview.OMIT},
    "test.p54_send": {"name": auditview.KEEP},
    "test.p54_move": {"path": auditview.PATH},
    "test.p54_secret": {"name": auditview.KEEP, "text": auditview.OMIT},
}

for _tool in ("test.p54_mkdir", "test.p54_write", "test.p54_append", "test.p54_send"):
    verify.register(_tool, verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", True, "it ran", "it ran")]))
verify.register("test.p54_secret", verify.Verifier(
    lambda a, d, b: [verify.Check("fixture effect", True, "expected-p54", CHECK_TEXT, "DETAIL-TEXT-p54")]))


class Planner(ScriptedPlanner):
    def __init__(self, decisions: list[str], subgoals: list[str] | None = None) -> None:
        super().__init__(decisions)
        self.subgoals = subgoals
        self.n = 0

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        system = next((m.content for m in request.messages if m.role == "system"), "")
        if "Break a real-world goal down" in system:
            text = json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in self.subgoals or []]})
        else:
            text = self.replies[min(self.n, len(self.replies) - 1)]
            self.n += 1
        return LlmResponse(text=text, model="scripted", provider=self.name)


_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="goal-progress-loop").start()


def on_loop(coro, timeout: float = 60.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


GOAL = "Create folder a, write a file in it, then send it"


def run_plan(goal: str, decisions: list[str], subgoals: list[str] | None = None):
    LOG.clear()
    with scripted_provider(Planner(decisions, subgoals)):
        return on_loop(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"))


def goal_rows(goal_id: str) -> list[dict]:
    return [dict(r) | {"status": audit.status_of(dict(r))} for r in
            store.conn().execute("SELECT * FROM audit WHERE goal_id=? ORDER BY id", (goal_id,)).fetchall()]


def sts(report) -> list[tuple[str, str]]:
    return [(s.tool, s.state.value) for s in report.steps]


def live_sts(r: SkillResult) -> list[tuple[str, str]]:
    return [(s["tool"], s["state"]) for s in r.data["report"]["steps"]]


def count(table: str) -> int:
    return int(store.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def goal_raw(gid: str) -> tuple:
    r = store.conn().execute(
        "SELECT status, failure_reason, progress, updated_at, current_step FROM goals WHERE id=?", (gid,)).fetchone()
    return tuple(r)


def all_keys(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= all_keys(v)
    return out


# -- crafted rows ------------------------------------------------------------------------------------------------------------
def add_rows(gid: str, ident: owner.Identity, specs: list[tuple[str, str]]) -> list[int]:
    """Audit rows of `gid` written by session `ident`: outcome ok | fail | open | denied."""
    ids: list[int] = []
    for tool, outcome in specs:
        with owner._use_identity(ident), audit.goal_scope(gid):
            rid = audit.record(actor="text", skill=tool, tier="L1", args={}, decision="denied" if outcome == "denied" else "auto")
        if outcome == "ok":
            audit.complete(rid, ok=True, result="Done.", duration_ms=1)
        elif outcome == "fail":
            audit.complete(rid, ok=False, result="Could not.", error="boom", duration_ms=1)
        ids.append(rid)
    return ids


def new_goal(status: str = "running", progress: dict | None = None) -> str:
    g = goals_mod.create("p54 goal", "p54 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
    if progress is not None:
        goals_mod.set_progress(g.id, progress)
    return g.id


def st(n: int, tool: str, state: str, ids: list[int], ver: dict | None = None, recovered: bool = False) -> dict:
    return {"n": n, "tool": tool, "state": state, "audit": ids, **({"ver": ver} if ver else {}), **({"recovered": True} if recovered else {})}


def prog(step_list: list[dict], stopped: str = "completed") -> dict:
    return {"v": 1, "stopped": stopped, "steps": step_list}


VER = {"status": "verified", "checks": [["fixture effect", True]]}


def one_step_goal(stored_state: str, outcome: str, *, ver: dict | None = None, status: str = "partial",
                  ids_override: list[int] | None = None) -> tuple[str, int]:
    """One stored step over one audit row. With `ids_override` the stored step points elsewhere and NO row is written (otherwise the
    unlinked row would, correctly, be appended as a step found only in the audit log)."""
    gid = new_goal(status)
    rid = add_rows(gid, DEAD_ID, [("test.p54_write", outcome)])[0] if ids_override is None else -1
    goals_mod.set_progress(gid, prog([st(1, "test.p54_write", stored_state, ids_override if ids_override is not None else [rid], ver)]))
    return gid, rid


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s)
    CFG.desktop_observer.enabled = False
    added = []
    for k, v in _POLICY.items():
        if k not in auditview.POLICY:
            auditview.POLICY[k] = v
            added.append(k)
    try:
        with iso_env.isolated_runtime():
            section_a()
            section_b()
            section_c()
            section_e()
            section_f()
            section_h()
            section_i()
        with iso_env.isolated_runtime():
            section_d()
        with iso_env.isolated_runtime():
            section_g()
        with iso_env.isolated_runtime():
            section_j()
    finally:
        for k in added:
            auditview.POLICY.pop(k, None)
        CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s = saved
        CFG.permissions.overrides = saved_tools
    return finish("Phase 54 -- durable per-step goal progress, evidence-first reconstruction, strict recovery",
                  time.perf_counter() - t0, min_assertions=73, min_scenarios=7)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. a partially completed goal stores its progress; a reconstruction says what the live report said")
    FAIL_ALWAYS.clear()
    FAIL_ALWAYS.add("a.txt")
    r = run_plan(GOAL, [call("test.p54_mkdir"), call("test.p54_write", {"name": "a.txt", "text": "x"}), done("Created and wrote it.")])
    gid = r.data["goal_id"]
    g = goals_mod.get(gid)
    rows = goal_rows(gid)
    check("A1 the run is partial (a step failed, the planner said done) and the goal is PARTIAL",
          r.ok is False and g.status is goals_mod.GoalStatus.PARTIAL and r.data["report"]["outcome"] == "partial", f"{r.ok} {g.status}")
    p = g.progress
    check("A2 the goal row carries progress: version 1 and the final stop reason (no longer 'running')",
          p.get("v") == 1 and p.get("stopped") == "completed" and r.data["stopped"] == "completed", str(p))
    check("A3 exactly the executed steps are stored, with their states",
          [(s["n"], s["tool"], s["state"]) for s in p["steps"]] == [(1, "test.p54_mkdir", "verified"), (2, "test.p54_write", "failed")],
          str(p))
    check("A4 each step names the audit row(s) that recorded it",
          [s["audit"] for s in p["steps"]] == [[rows[0]["id"]], [rows[1]["id"]]] and len(rows) == 2, str(p) + str([x["id"] for x in rows]))
    check("A5 the verified step stores its verdict as names and booleans only",
          p["steps"][0]["ver"] == {"status": "verified", "checks": [["fixture effect", True]]} and "ver" not in p["steps"][1], str(p))
    rep = steps.reconstruct(gid, probe_dead)
    check("A6 after a 'restart' the reconstruction has the same step states as the live report",
          sts(rep) == [(t, s) for t, s in live_sts(r)] == [("test.p54_mkdir", "verified"), ("test.p54_write", "failed")], str(sts(rep)))
    check("A7 ...the same outcome, and the same whole account as the live report", rep.outcome == "partial"
          and rep.to_dict() == r.data["report"], f"{rep.to_dict()} vs {r.data['report']}")
    check("A8 the reconstruction keeps the failure's own reason from the audit row, and runs nothing",
          rep.steps[1].note and "boom" not in rep.tail() and LOG == ["mkdir:a", "write:a.txt:0"], rep.tail() + str(LOG))
    check("A9 a checkpoint written by a live run already agrees with the audit log: nothing to refresh", steps.refresh_goals(probe_dead) == 0)


# ---------------------------------------------------------------------------------------------------------------------------
def section_b() -> None:
    scenario("B. progress exists WHILE the run is going; a crash between steps is settled without running anything")
    FAIL_ALWAYS.clear()
    PEEK.clear()
    r = run_plan(GOAL, [call("test.p54_mkdir"), call("test.p54_peek", {"tag": "1"}), call("test.p54_write", {"name": "b.txt"}),
                        call("test.p54_peek", {"tag": "2"}), done("Created and wrote it.")])
    gid = r.data["goal_id"]
    p1 = next((x for x in PEEK if x["tag"] == "1"), {})
    p2 = next((x for x in PEEK if x["tag"] == "2"), {})
    check("B1 mid-run (during step 2) the previous step is already persisted and the run is marked 'running'",
          p1.get("gid") == gid and p1["progress"]["stopped"] == "running"
          and [(s["tool"], s["state"]) for s in p1["progress"]["steps"]] == [("test.p54_mkdir", "verified")], str(p1))
    check("B2 later in the run the checkpoint has grown by exactly the steps finished so far (not the one in flight)",
          [s["tool"] for s in p2["progress"]["steps"]] == ["test.p54_mkdir", "test.p54_peek", "test.p54_write"]
          and p2["progress"]["stopped"] == "running", str(p2))
    final = goals_mod.get(gid).progress
    check("B3 once the run ends the same row holds all four steps and the final stop reason",
          final["stopped"] == "completed" and len(final["steps"]) == 4 and r.ok, str(final))

    # a crash: step 1 checkpointed, step 2's audit row open and never checkpointed, owner dead
    g = new_goal("running")
    ids = add_rows(g, DEAD_ID, [("test.p54_mkdir", "ok"), ("test.p54_write", "open")])
    goals_mod.set_progress(g, prog([st(1, "test.p54_mkdir", "completed", [ids[0]])], "running"))
    ran: list[str] = []
    real_run = EXECUTOR.run

    async def spy(*a, **k):
        ran.append(str(a))
        return await real_run(*a, **k)

    LOG.clear()
    audit_n, undo_n = count("audit"), count("undo")
    rep = steps.reconstruct(g, probe_dead)
    EXECUTOR.run = spy  # type: ignore[method-assign]
    try:
        n = steps.settle_interrupted_goals(probe_dead)
    finally:
        EXECUTOR.run = real_run  # type: ignore[method-assign]
    got = goals_mod.get(g)
    check("B4 the crashed goal is settled: exactly one goal, and PARTIAL (one step is known to have succeeded)",
          n == 1 and got.status is goals_mod.GoalStatus.PARTIAL and "interrupted" in got.failure_reason, f"{n} {got.status}")
    check("B5 the never-checkpointed step is found from the audit log and is UNKNOWN, not completed",
          sts(rep) == [("test.p54_mkdir", "completed"), ("test.p54_write", "unknown")] and rep.outcome == "unknown"
          and any("only in the audit log" in x for x in rep.notes), f"{sts(rep)} {rep.notes}")
    check("B6 the settled progress stores that unknown step with its audit id",
          [(s["tool"], s["state"], s["audit"]) for s in got.progress["steps"]] ==
          [("test.p54_mkdir", "completed", [ids[0]]), ("test.p54_write", "unknown", [ids[1]])] and got.progress["stopped"] == "interrupted",
          str(got.progress))
    check("B7 nothing was executed, retried or undone; no audit row was added; the open row was left as it was",
          ran == [] and LOG == [] and count("audit") == audit_n and count("undo") == undo_n
          and store.conn().execute("SELECT ok FROM audit WHERE id=?", (ids[1],)).fetchone()[0] is None)


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    scenario("C. 'verified' survives a restart only with evidence")
    gid, rid = one_step_goal("verified", "ok", ver=VER)
    rep = steps.reconstruct(gid, probe_dead)
    check("C1 stored verified + stored verdict + audit row succeeded => verified", sts(rep) == [("test.p54_write", "verified")]
          and rep.steps[0].ver == VER, str(sts(rep)))
    gid2, _ = one_step_goal("verified", "ok", ver=None)
    rep2 = steps.reconstruct(gid2, probe_dead)
    check("C2 the verdict removed from the stored progress => only 'completed', with a note saying why",
          sts(rep2) == [("test.p54_write", "completed")] and any("verification evidence" in n for n in rep2.notes), str(rep2.notes))
    gid3, _ = one_step_goal("verified", "ok", ver={"status": "verified", "checks": [["fixture effect", False]]})
    gid3b, _ = one_step_goal("verified", "ok", ver={"status": "unverified", "checks": [["fixture effect", True]]})
    check("C3 a stored verdict with a failed check, or whose status is not 'verified', does not keep 'verified'",
          sts(steps.reconstruct(gid3, probe_dead)) == [("test.p54_write", "completed")]
          and sts(steps.reconstruct(gid3b, probe_dead)) == [("test.p54_write", "completed")])
    gid4, rid4 = one_step_goal("verified", "ok", ver=VER)
    store.conn().execute("UPDATE audit SET ok=0, error='later failure' WHERE id=?", (rid4,))
    store.conn().commit()
    rep4 = steps.reconstruct(gid4, probe_dead)
    check("C4 the audit row later says failed => unknown (never verified, never silently 'failed' either)",
          sts(rep4) == [("test.p54_write", "unknown")] and rep4.outcome == "unknown" and any("disagree" in n for n in rep4.notes),
          f"{sts(rep4)} {rep4.notes}")
    gid5, rid5 = one_step_goal("verified", "ok", ver=VER)
    store.conn().execute("UPDATE audit SET ok=NULL, error=? WHERE id=?", (audit.OUTCOME_UNKNOWN_DETACHED, rid5))
    store.conn().commit()
    check("C5 the audit row later marked 'outcome unknown' => unknown", sts(steps.reconstruct(gid5, probe_dead)) == [("test.p54_write", "unknown")])
    gid6, rid6 = one_step_goal("verified", "ok", ver=VER)
    store.conn().execute("DELETE FROM audit WHERE id=?", (rid6,))
    store.conn().commit()
    rep6 = steps.reconstruct(gid6, probe_dead)
    check("C6 the audit row gone => unknown, with a note that the record is no longer available",
          sts(rep6) == [("test.p54_write", "unknown")] and any("no longer available" in n for n in rep6.notes), str(rep6.notes))
    check("C7 none of the degraded cases is ever reported verified or complete", all(
        s.state is not steps.StepState.VERIFIED for x in (rep2, rep4, rep6) for s in x.steps)
        and all(not x.outcome.startswith("complete") for x in (rep4, rep6)))


# ---------------------------------------------------------------------------------------------------------------------------
def section_d() -> None:
    scenario("D. an unknown outcome is never repeated, and a late real outcome is recorded once")
    g = new_goal("running")
    ids = add_rows(g, DEAD_ID, [("test.p54_mkdir", "ok"), ("test.p54_write", "open")])
    goals_mod.set_progress(g, prog([st(1, "test.p54_mkdir", "completed", [ids[0]]), st(2, "test.p54_write", "unknown", [ids[1]])], "running"))
    check("D1 the owner is provably dead: recovery marks the open row 'interrupted'", audit.reconcile_orphans(probe_dead) == 1
          and goal_rows(g)[1]["status"] == "interrupted")
    rep = steps.reconstruct(g, probe_dead)
    check("D2 the interrupted step reconstructs as unknown; the goal outcome is 'unknown'",
          sts(rep) == [("test.p54_mkdir", "completed"), ("test.p54_write", "unknown")] and rep.outcome == "unknown", str(sts(rep)))
    check("D3 the next action is to check it by hand, and says it will not be repeated",
          "I won't repeat it on my own" in rep.next_action and "Check whether 'test.p54_write' took effect" in rep.next_action
          and "Outcome unknown: 'test.p54_write'" in rep.tail(), rep.next_action)
    ran: list[str] = []
    real_run = EXECUTOR.run

    async def spy(*a, **k):
        ran.append(str(a))
        return await real_run(*a, **k)

    LOG.clear()
    audit_n, undo_n = count("audit"), count("undo")
    EXECUTOR.run = spy  # type: ignore[method-assign]
    try:
        first = steps.settle_interrupted_goals(probe_dead)
        for _ in range(3):
            steps.settle_interrupted_goals(probe_dead)
            steps.refresh_goals(probe_dead)
            steps.reconstruct(g, probe_dead)
    finally:
        EXECUTOR.run = real_run  # type: ignore[method-assign]
    check("D4 settling, refreshing and reconstructing again and again never executes anything",
          first == 1 and ran == [] and LOG == [] and count("audit") == audit_n and count("undo") == undo_n, f"{first} {ran} {LOG}")
    check("D5 the goal stays PARTIAL with the step unknown",
          goals_mod.get(g).status is goals_mod.GoalStatus.PARTIAL
          and [s["state"] for s in goals_mod.get(g).progress["steps"]] == ["completed", "unknown"])
    audit.complete(ids[1], ok=True, result="Done.", duration_ms=1)   # the abandoned thread finally ended
    check("D6 a late real outcome makes the next refresh turn the step into 'completed' (one goal changed)",
          steps.refresh_goals(probe_dead) == 1
          and [s["state"] for s in goals_mod.get(g).progress["steps"]] == ["completed", "completed"])
    snap = goal_raw(g)
    check("D7 the following refresh changes nothing at all", steps.refresh_goals(probe_dead) == 0 and goal_raw(g) == snap)


# ---------------------------------------------------------------------------------------------------------------------------
def section_e() -> None:
    scenario("E. what never ran stays 'not attempted'")
    FAIL_ALWAYS.clear()
    FAIL_ALWAYS.add("e.txt")
    r = run_plan(GOAL, [call("test.p54_mkdir"), call("test.p54_write", {"name": "e.txt"}), call("test.p54_write", {"name": "e.txt", "retry": 1}),
                        done("done")],
                 subgoals=["Make the folder a", "Write the file into it", "Send the file to Sam"])
    gid = r.data["goal_id"]
    pending = [x.description for x in goals_mod.get(gid).subgoals if x.status.value == "pending"]
    rep = steps.reconstruct(gid, probe_dead)
    check("E1 the reconstruction lists the saved pending subgoals as remaining, exactly as the live report did",
          pending and rep.remaining == pending == r.data["report"].get("remaining") and "Send the file to Sam" in rep.remaining,
          f"{rep.remaining} {pending}")
    executed = {s.tool for s in rep.steps}
    check("E2 an unreached subgoal is not a step, let alone a success: only executed tools appear, and nothing was sent",
          executed <= {"test.p54_mkdir", "test.p54_write"} and not any("Send" in s.tool for s in rep.steps)
          and not any(x == "send:a" for x in LOG) and rep.outcome in ("partial", "failed"), f"{sts(rep)} {LOG}")
    check("E3 the next safe action is the same as the live one (nothing is retried on its own)",
          rep.next_action == r.data["report"].get("next", "") and "I can try the failed step again if you ask" in rep.next_action)

    FAIL_ALWAYS.clear()
    check("E4 a goal with no subgoals that stopped early gets the generic 'rest of the request'",
          steps.reconstruct(new_goal_with_steps("running", "running"), probe_dead).remaining == ["the rest of the request"])
    gid_c = new_goal_with_steps("partial", "completed")
    check("E5 ...but not when the run reached 'completed' (nothing was left behind)", steps.reconstruct(gid_c, probe_dead).remaining == [])
    gid_s = new_goal_with_steps("partial", "failure")
    goals_mod.set_subgoals(gid_s, [goals_mod.Subgoal("s1", "Make the folder", status=goals_mod.SubgoalStatus.SUCCEEDED),
                                   goals_mod.Subgoal("s2", "Write the file", status=goals_mod.SubgoalStatus.FAILED),
                                   goals_mod.Subgoal("s3", "Send the file", status=goals_mod.SubgoalStatus.PENDING),
                                   goals_mod.Subgoal("s4", "Tell Sam", status=goals_mod.SubgoalStatus.PENDING)])
    rep_s = steps.reconstruct(gid_s, probe_dead)
    check("E6 only PENDING saved subgoals are listed (a failed one was attempted) and they replace the generic line",
          rep_s.remaining == ["Send the file", "Tell Sam"], str(rep_s.remaining))
    r2 = run_plan(GOAL, [call("test.p54_mkdir"), call("test.p54_write", {"name": "f.txt"}), done("Created and wrote it.")])
    rep2 = steps.reconstruct(r2.data["goal_id"], probe_dead)
    check("E7 a clean run that finished reports nothing remaining, live or reconstructed",
          r2.ok and "remaining" not in r2.data["report"] and rep2.remaining == [] and rep2.to_dict() == r2.data["report"],
          f"{r2.data['report']} {rep2.to_dict()}")


def new_goal_with_steps(status: str, stopped: str) -> str:
    gid = new_goal(status)
    rid = add_rows(gid, DEAD_ID, [("test.p54_mkdir", "ok")])[0]
    goals_mod.set_progress(gid, prog([st(1, "test.p54_mkdir", "completed", [rid])], stopped))
    return gid


# ---------------------------------------------------------------------------------------------------------------------------
def section_f() -> None:
    scenario("F. conflicting or missing evidence never becomes success")
    cases = {
        "completed vs row failed": one_step_goal("completed", "fail"),
        "failed vs row succeeded": one_step_goal("failed", "ok"),
        "completed with no audit ids": one_step_goal("completed", "ok", ids_override=[]),
        "completed with a nonexistent id": one_step_goal("completed", "ok", ids_override=[987654]),
        "needs_input vs row succeeded": one_step_goal("needs_input", "ok"),
        "verified vs row denied": one_step_goal("verified", "denied", ver=VER),
        "completed vs row open (dead owner)": one_step_goal("completed", "open"),
    }
    got = {name: steps.reconstruct(gid, probe_dead) for name, (gid, _) in cases.items()}
    for i, name in enumerate(cases, 1):
        check(f"F{i} {name} => unknown", sts(got[name]) == [("test.p54_write", "unknown")] and got[name].outcome == "unknown",
              str(sts(got[name])))
    check("F8 none of them is completed or verified, and each says why in the account's notes",
          all(x.steps[0].state not in (steps.StepState.COMPLETED, steps.StepState.VERIFIED) for x in got.values())
          and all(x.notes for k, x in got.items() if k != "completed vs row open (dead owner)"))
    g_fail, _ = one_step_goal("unknown", "fail")
    g_bad_only, _ = one_step_goal("banana", "ok", ids_override=[])
    g_bad_fail, _ = one_step_goal("banana", "fail")
    g_bad_ok, _ = one_step_goal("banana", "ok")
    g_same, _ = one_step_goal("failed", "fail")
    g_noids, _ = one_step_goal("failed", "ok", ids_override=[])
    check("F9 a stored 'unknown' whose row says failed is failed (the audit row decides)",
          sts(steps.reconstruct(g_fail, probe_dead)) == [("test.p54_write", "failed")])
    check("F10 an unparseable stored state with no evidence is unknown; with a failed row it is failed",
          sts(steps.reconstruct(g_bad_only, probe_dead)) == [("test.p54_write", "unknown")]
          and sts(steps.reconstruct(g_bad_fail, probe_dead)) == [("test.p54_write", "failed")])
    rep_bad_ok = steps.reconstruct(g_bad_ok, probe_dead)
    check("F11 an unparseable stored state backed by a succeeded row is only 'completed' (the row's word), never verified",
          sts(rep_bad_ok) == [("test.p54_write", "completed")] and rep_bad_ok.steps[0].ver is None, str(sts(rep_bad_ok)))
    check("F12 agreement is kept: stored failed + row failed stays failed; a failure that never reached the Executor stays failed",
          sts(steps.reconstruct(g_same, probe_dead)) == [("test.p54_write", "failed")]
          and sts(steps.reconstruct(g_noids, probe_dead)) == [("test.p54_write", "failed")])
    g_alive, _ = one_step_goal("unknown", "open")
    check("F13 an open row is 'running' only when its owner is demonstrably alive; dead or unverifiable => unknown",
          sts(steps.reconstruct(g_alive, probe_alive)) == [("test.p54_write", "running")]
          and sts(steps.reconstruct(g_alive, probe_unknown)) == [("test.p54_write", "unknown")]
          and sts(steps.reconstruct(g_alive, probe_dead)) == [("test.p54_write", "unknown")])


# ---------------------------------------------------------------------------------------------------------------------------
def section_g() -> None:
    scenario("G. idempotence: settling and refreshing twice change nothing")
    g1 = new_goal("running")
    ids1 = add_rows(g1, DEAD_ID, [("test.p54_mkdir", "ok"), ("test.p54_write", "open")])
    goals_mod.set_progress(g1, prog([st(1, "test.p54_mkdir", "completed", [ids1[0]])], "running"))
    g2 = new_goal("partial")
    ids2 = add_rows(g2, DEAD_ID, [("test.p54_mkdir", "ok"), ("test.p54_write", "open")])
    goals_mod.set_progress(g2, prog([st(1, "test.p54_mkdir", "completed", [ids2[0]]), st(2, "test.p54_write", "unknown", [ids2[1]])],
                                    "interrupted"))
    audit_n, goals_n, ep_n, undo_n = count("audit"), count("goals"), count("episodes"), count("undo")
    check("G1 the first settlement settles the one running goal", steps.settle_interrupted_goals(probe_dead) == 1)
    snap = goal_raw(g1)
    check("G2 the second settlement does nothing and the goal row (status, reason, progress, updated_at) is byte-identical",
          steps.settle_interrupted_goals(probe_dead) == 0 and goal_raw(g1) == snap)
    check("G3 settling added no audit row, goal, episode or undo entry",
          (count("audit"), count("goals"), count("episodes"), count("undo")) == (audit_n, goals_n, ep_n, undo_n))
    rep_a, rep_b = steps.reconstruct(g1, probe_dead), steps.reconstruct(g1, probe_dead)
    check("G4 reconstructing twice gives equal accounts and writes nothing", rep_a.to_dict() == rep_b.to_dict() and goal_raw(g1) == snap)
    audit.complete(ids2[1], ok=True, result="Done.", duration_ms=1)
    check("G5 refresh rewrites exactly the goal whose evidence changed (g1 already agrees)", steps.refresh_goals(probe_dead) == 1)
    snap2 = goal_raw(g2)
    check("G6 a second refresh returns 0 and the progress string and updated_at are unchanged",
          steps.refresh_goals(probe_dead) == 0 and goal_raw(g2) == snap2 and goal_raw(g1) == snap)
    check("G7 the refreshed progress is stored once, replacing (two steps, not four)", len(goals_mod.get(g2).progress["steps"]) == 2
          and count("goals") == goals_n)


# ---------------------------------------------------------------------------------------------------------------------------
def section_h() -> None:
    scenario("H. recovery needs the SAME objective")
    FAIL_ALWAYS.clear()
    FAIL_ONCE.clear()
    FAILED_ONCE.clear()
    FAIL_ALWAYS.add("a.txt")
    r = run_plan(GOAL, [call("test.p54_write", {"name": "a.txt"}), call("test.p54_write", {"name": "b.txt"}), done("Wrote both files.")])
    rep = r.data["report"]
    check("H1 a.txt failed, b.txt succeeded (a different target): the failure is NOT recovered, the goal is partial and not ok",
          live_sts(r) == [("test.p54_write", "failed"), ("test.p54_write", "verified")] and "recovered" not in rep["steps"][0]
          and rep["outcome"] == "partial" and r.ok is False, str(rep))
    check("H2 ...the later success is flagged as ran-after-failure and the stored progress carries no recovered flag",
          rep["steps"][1].get("after_failure") is True
          and not any(s.get("recovered") for s in goals_mod.get(r.data["goal_id"]).progress["steps"]))
    FAIL_ALWAYS.clear()
    FAIL_ONCE.add("c.txt")
    r2 = run_plan(GOAL, [call("test.p54_write", {"name": "c.txt"}), call("test.p54_write", {"name": "c.txt", "retry": 1}), done("Wrote it.")])
    gid2 = r2.data["goal_id"]
    check("H3 the same tool retried on the same target (only a non-identifying option changed) and succeeding => recovered, run ok",
          r2.ok and r2.data["report"]["steps"][0].get("recovered") is True and r2.data["report"]["outcome"] in ("complete", "complete_unverified")
          and LOG == ["write:c.txt:0", "write:c.txt:1"], f"{r2.data['report']} {r2.speech} {LOG}")
    rep2 = steps.reconstruct(gid2, probe_dead)
    check("H4 the recovery decision was stored: a restart reconstructs the same recovered step and outcome",
          goals_mod.get(gid2).progress["steps"][0].get("recovered") is True and rep2.steps[0].recovered
          and rep2.outcome == r2.data["report"]["outcome"], str(rep2.to_dict()))
    FAIL_ALWAYS.add("d.txt")
    r3 = run_plan(GOAL, [call("test.p54_write", {"name": "d.txt"}), call("test.p54_append", {"name": "D.TXT"}), done("Wrote it.")])
    check("H5 another tool of the same kind on the same target (write -> append, name case-folded) recovers it",
          r3.data["report"]["steps"][0].get("recovered") is True and r3.ok, str(r3.data["report"]))
    FAIL_ALWAYS.clear()

    same = steps.same_objective
    check("H6 different tool, same kind, same non-empty target => recovers; a different target does not",
          same("test.p54_write", {"name": "x"}, "test.p54_append", {"name": "X"})
          and not same("test.p54_write", {"name": "x"}, "test.p54_append", {"name": "y"}))
    check("H7 different tools with no identifying target at all => no", not same("test.p54_write", {}, "test.p54_append", {})
          and not same("test.p54_write", {"name": "x"}, "test.p54_send", {"name": "x"}))
    check("H8 content-only calls match only an identical call (different text no, same text yes)",
          not same("input.type", {"text": "one"}, "input.type", {"text": "two"})
          and same("input.type", {"text": "one"}, "input.type", {"text": "one"})
          and not same("input.type", {"text": "one"}, "notes.add", {"text": "one"}))
    check("H9 unregistered tool names need identical arguments",
          same("zzz.unknown", {"x": 1}, "zzz.unknown", {"x": 1}) and not same("zzz.unknown", {"x": 1}, "zzz.unknown", {"x": 2})
          and not same("zzz.unknown", {"x": 1}, "zzz.other", {"x": 1}))
    check("H10 paths are normalised (case, separators, '..'); a content option never identifies a target",
          same("test.p54_move", {"path": "C:/Tmp/a.txt"}, "test.p54_move", {"path": "c:/tmp/../tmp/A.TXT"})
          and not same("test.p54_move", {"path": "c:/tmp/a.txt"}, "test.p54_move", {"path": "c:/tmp/b.txt"})
          and steps.objective("test.p54_write", {"name": "A  b", "text": "body"}) == ("target", (("name", "a b"),)))


# ---------------------------------------------------------------------------------------------------------------------------
def section_i() -> None:
    scenario("I. sensitive data never reaches the result, the progress, the reason or the audit args")
    FAIL_ALWAYS.clear()
    r = run_plan(GOAL, [call("test.p54_secret", {"name": "ok", "text": SECRET, "password": SECRET_PW}),
                        call("test.p54_secret", {"name": "bad", "text": SECRET, "password": SECRET_PW}), done("Wrote it.")])
    gid = r.data["goal_id"]
    g = goals_mod.get(gid)
    raw_progress = store.conn().execute("SELECT progress FROM goals WHERE id=?", (gid,)).fetchone()[0]
    audit_args = " ".join(x["args"] for x in goal_rows(gid)) + " ".join(str(x["result"]) + str(x["error"]) for x in goal_rows(gid))
    blob = {
        "speech": r.speech, "data": json.dumps(r.data, default=str), "progress": raw_progress,
        "reason": g.failure_reason, "audit": audit_args,
    }
    for name, text in blob.items():
        check(f"I{list(blob).index(name) + 1} neither secret appears in the {name}", SECRET not in text and SECRET_PW not in text, text[:200])
    check("I6 data.steps[*].args is the audited form: content omitted, credential redacted, the name kept",
          r.data["steps"][0]["args"]["name"] == "ok" and r.data["steps"][0]["args"]["text"].startswith("[omitted")
          and r.data["steps"][0]["args"]["password"] == "[redacted]", str(r.data["steps"][0]["args"]))
    check("I7 data.report steps hold only n/tool/state(/recovered/after_failure)",
          all(set(s) <= {"n", "tool", "state", "recovered", "after_failure"} for s in r.data["report"]["steps"]), str(r.data["report"]))
    p = json.loads(raw_progress)
    check("I8 the stored progress has none of the keys args / speech / result / note / observed / detail",
          not (all_keys(p) & FORBIDDEN_KEYS), str(all_keys(p) & FORBIDDEN_KEYS))
    check("I9 the stored verdict is exactly [name, bool|None] per check, and the read-back text is not in the progress",
          p["steps"][0]["ver"]["checks"] == [["fixture effect", True]] and CHECK_TEXT not in raw_progress
          and "DETAIL-TEXT-p54" not in raw_progress and "expected-p54" not in raw_progress
          and all(isinstance(c, list) and len(c) == 2 and isinstance(c[0], str) and (c[1] is None or isinstance(c[1], bool))
                  for s in p["steps"] for c in s.get("ver", {}).get("checks", [])), raw_progress)
    check("I10 a reconstruction's account carries none of the secrets either", SECRET not in json.dumps(steps.reconstruct(gid, probe_dead).to_dict())
          and SECRET not in steps.reconstruct(gid, probe_dead).tail())


# ---------------------------------------------------------------------------------------------------------------------------
def section_j() -> None:
    scenario("J. migration: an old database without goals.progress is upgraded and reconstructed from the audit log alone")
    raw = sqlite3.connect(str(paths.DB_PATH))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(
        "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, skill TEXT NOT NULL,"
        " tier TEXT NOT NULL, args TEXT NOT NULL, decision TEXT NOT NULL, ok INTEGER, result TEXT, error TEXT, duration_ms INTEGER);"
        "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-01','text','test.p54_mkdir','L1','{}','auto',1,'ok');"
        "INSERT INTO audit (at,actor,skill,tier,args,decision) VALUES ('2025-01-02','text','test.p54_write','L1','{}','auto');"
        "CREATE TABLE goals (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, original_request TEXT NOT NULL,"
        " objective TEXT NOT NULL, status TEXT NOT NULL, parent_goal_id TEXT, current_step INTEGER NOT NULL DEFAULT 0,"
        " success_criteria TEXT, failure_reason TEXT);"
        "INSERT INTO goals (id,created_at,updated_at,original_request,objective,status,current_step,failure_reason) VALUES"
        " ('old-partial','2025-01-01','2025-01-02','old goal','old goal','partial',2,'old reason');"
        "INSERT INTO goals (id,created_at,updated_at,original_request,objective,status,current_step,failure_reason) VALUES"
        " ('old-ok','2025-01-01','2025-01-02','old ok goal','old ok goal','succeeded',1,'');"
        "INSERT INTO goals (id,created_at,updated_at,original_request,objective,status,current_step,failure_reason) VALUES"
        " ('old-running','2025-01-01','2025-01-02','old run goal','old run goal','running',1,'');"
    )
    raw.commit()
    raw.close()
    cols = {r[1] for r in store.conn().execute("PRAGMA table_info(goals)")}
    check("J1 opening the old database adds goals.progress (and audit.goal_id)",
          "progress" in cols and "goal_id" in {r[1] for r in store.conn().execute("PRAGMA table_info(audit)")}, str(cols))
    old = goals_mod.get("old-partial")
    check("J2 an old goal has an empty progress and its other fields intact", old is not None and old.progress == {}
          and old.failure_reason == "old reason" and old.status is goals_mod.GoalStatus.PARTIAL)
    c = store.conn()
    c.execute("UPDATE audit SET goal_id='old-partial'")   # tie the two old rows to the old goal (as if Phase 53 had run for it)
    c.execute("INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result,goal_id) VALUES "
              "('2025-01-03','text','test.p54_mkdir','L1','{}','auto',1,'ok','old-ok')")
    c.commit()
    rep = steps.reconstruct("old-partial", probe_dead)
    check("J3 reconstructing a goal with audit rows but no progress says the account is from the audit log alone",
          rep is not None and rep.notes == ["no step progress was checkpointed; this account comes from the audit log alone"], str(rep and rep.notes))
    check("J4 ...the unfinished row (no owner) is unknown, the finished one completed, and the outcome is unknown",
          sts(rep) == [("test.p54_mkdir", "completed"), ("test.p54_write", "unknown")] and rep.outcome == "unknown", str(sts(rep)))
    rep_ok = steps.reconstruct("old-ok", probe_dead)
    check("J5 a succeeded old goal without stored progress is never reported 'complete' on the audit log alone",
          rep_ok.outcome == "partial" and not rep_ok.outcome.startswith("complete"), rep_ok.outcome)
    before = [goal_raw(g) for g in ("old-partial", "old-ok", "old-running")]
    check("J6 refresh leaves a goal without progress untouched, and so does settlement of an owner-less running goal",
          steps.refresh_goals(probe_dead) == 0 and steps.settle_interrupted_goals(probe_dead) == 0
          and [goal_raw(g) for g in ("old-partial", "old-ok", "old-running")] == before)
    check("J7 reconstructing writes nothing: still no progress stored", store.conn().execute(
        "SELECT progress FROM goals WHERE id='old-partial'").fetchone()[0] is None)
    check("J8 a goal that does not exist reconstructs to None", steps.reconstruct("no-such-goal", probe_dead) is None)
    store.conn().close()
    store._local.conns.clear()
    again = store.conn()
    check("J9 re-opening (the migration running again) is a no-op", {r[1] for r in again.execute("PRAGMA table_info(goals)")} == cols
          and goals_mod.get("old-partial").progress == {})


if __name__ == "__main__":
    sys.exit(main())
