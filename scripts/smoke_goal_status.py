"""Phase 55 -- the durable account of a past goal is reachable: `meta.goal_status` ("what happened to my last request?").

Gap found by tracing the callers: after Phase 54 stored `goals.progress` and `steps.reconstruct` could rebuild it, NOTHING a user (or a
daemon client) could ask read it -- no skill, endpoint or CLI command touched a goal's steps, and the only goal reader was the
clarification flow. This suite drives the new read-only skill the way a user reaches it (through the real Executor) against goals produced
by real `plan.run` runs and by crafted post-restart states, and pins what it may say: the steps in stored order and their real states
(verified is not completed), a recorded success the evidence contradicts is not reported as one, what is left only when a saved plan
supports it, no promise to resume, a legacy or malformed record never turns into a history, nothing written / executed / exposed.

Reuses the fixtures and harness of `smoke_goal_steps.py`. Throwaway databases; nothing real runs.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
import smoke_goal_steps as G  # noqa: E402  (registers its test.g_* fixtures and the long-lived loop)
from phase21_common import call, check, done, finish, scenario  # noqa: E402

from friday import audit, owner, store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402

SECRET = "hunter2-SECRET-body"
TOKEN = "tok-SECRET-99"


def ask():
    return G.on_loop(EXECUTOR.run("meta.goal_status", {}, actor="text"))


def goal_state(gid: str) -> tuple:
    r = dict(store.conn().execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
    return tuple(r.get(k) for k in ("status", "failure_reason", "progress", "updated_at", "current_step", "subgoals"))


def row_count() -> int:
    return store.conn().execute("SELECT COUNT(*) FROM audit WHERE skill != 'meta.goal_status'").fetchone()[0]


def crafted_goal(status: str, steps_spec: list[tuple[str, str, bool | None]], *, ident: owner.Identity, progress=None,
                 text: str = "tidy up the files then send them") -> tuple[str, list[int]]:
    """A goal as a restart leaves it: audit rows (tool, decision-free) from `ident`'s session; spec = (tool, 'done'|'fail'|'open', _)."""
    g = goals_mod.create(text, text)
    goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
    ids = []
    for tool, how, _ in steps_spec:
        with owner._use_identity(ident), audit.goal_scope(g.id):
            rid = audit.record(actor="text", skill=tool, tier="L1", args={}, decision="auto")
        if how == "done":
            audit.complete(rid, ok=True, result="Done.", duration_ms=1)
        elif how == "fail":
            audit.complete(rid, ok=False, error="it failed", duration_ms=1)
        ids.append(rid)
    if progress is not None:
        goals_mod.set_progress(g.id, progress)
    return g.id, ids


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = CFG.desktop_observer.enabled
    CFG.desktop_observer.enabled = False
    try:
        with iso_env.isolated_runtime():
            run()
    finally:
        CFG.desktop_observer.enabled = saved
        CFG.permissions.overrides = saved_tools
        G.GATE.set()
    return finish("Phase 55 -- asking what happened to a past goal", time.perf_counter() - t0, min_assertions=30, min_scenarios=8)


def run() -> None:
    dead = owner.Identity("fake-dead55", 4_200_001, 1.0, owner._host())
    alive = owner.Identity("fake-alive55", 4_200_002, 1.0, owner._host())

    scenario("A. nothing to report yet")
    r = ask()
    check("A1 with no goal the answer says so, and is not an error", r.ok and "haven't run any multi-step request" in r.speech, r.speech)

    scenario("B. a real mixed run, asked about after 'restart' (fresh reads only)")
    G.WORLD.clear()
    G.WORLD["write"] = SkillResult(speech="Could not write the file: disk full.", ok=False)
    run1 = G.run_plan(G.GOAL, [call("test.g_mkdir"), call("test.g_write"), done("Created the folder and wrote the file.")])
    gid = run1.data["goal_id"]
    G.LOG.clear()
    before_goal, before_rows = goal_state(gid), row_count()
    r = ask()
    check("B1 the skill is read-only L0 and is answered through the real Executor", r.ok and REGISTRY.get("meta.goal_status").tier == "L0")
    check("B2 it reports the steps in stored order with their real states: confirmed, then failed",
          r.speech.index("Done: 'test.g_mkdir'") < r.speech.index("Failed: 'test.g_write'")
          and [s["state"] for s in r.data["report"]["steps"]] == ["verified", "failed"], r.speech)
    check("B3 verified is told apart from completed ('confirmed')", "confirmed" in r.speech or r.data["report"]["steps"][0]["state"] == "verified")
    check("B4 it says it did not finish, not that it succeeded", "did not finish" in r.speech and "finished, and every" not in r.speech, r.speech)
    check("B5 it gives the failure's reason from the audit record, not the planner's claim",
          "disk full" in r.speech and "wrote the file" not in r.speech.replace("write the file", ""), r.speech)
    check("B6 it makes no promise to resume", "won't resume or retry any of it on my own" in r.speech)
    check("B7 reading changed nothing (goal row byte-identical, no step re-run, no new audit row)",
          goal_state(gid) == before_goal and row_count() == before_rows and G.LOG == [], str(G.LOG))
    check("B8 the data keeps the existing report shape and adds nothing sensitive",
          set(r.data) == {"goal", "report"} and set(r.data["report"]) <= {"outcome", "steps", "remaining", "next", "notes"}
          and all(set(s) <= {"n", "tool", "state", "recovered", "after_failure"} for s in r.data["report"]["steps"]))

    scenario("C. the goal row says SUCCEEDED but the evidence does not")
    g_ok, ids = crafted_goal("succeeded", [("test.g_mkdir", "done", None), ("test.g_write", "fail", None)], ident=dead, progress={
        "v": 1, "stopped": "completed",
        "steps": [{"n": 1, "tool": "test.g_mkdir", "state": "completed", "audit": [0]},
                  {"n": 2, "tool": "test.g_write", "state": "completed", "audit": [0]}]})
    progress = goals_mod.get(g_ok).progress
    progress["steps"][0]["audit"], progress["steps"][1]["audit"] = [ids[0]], [ids[1]]
    goals_mod.set_progress(g_ok, progress)
    r = ask()
    check("C1 a recorded success that its audit record contradicts is not reported as success",
          "Its record says it succeeded, but the evidence" in r.speech and "finished, and every" not in r.speech, r.speech)
    check("C2 the contradicted step is unknown, with the limitation stated",
          ("test.g_write", "unknown") in [(s["tool"], s["state"]) for s in r.data["report"]["steps"]]
          and "disagree" in r.speech, r.speech)
    check("C3 the next safe action is to check, and nothing is repeated on its own",
          "Check whether 'test.g_write' took effect" in r.speech and "won't repeat it on my own" in r.speech)

    scenario("D. interrupted: unknown step, never-checkpointed step, a goal still running")
    g_int, ids = crafted_goal("partial", [("test.g_mkdir", "done", None), ("test.g_slow", "open", None)], ident=dead, progress={
        "v": 1, "stopped": "interrupted", "steps": []})
    audit.reconcile_orphans(lambda *a: owner.DEAD)
    r = ask()
    check("D1 an open step of a dead process is reported unknown (found only in the audit log), not failed or done",
          ("test.g_slow", "unknown") in [(s["tool"], s["state"]) for s in r.data["report"]["steps"]]
          and "Outcome unknown: 'test.g_slow'" in r.speech, r.speech)
    check("D2 with no saved plan it does not invent what is left", "I don't have a saved plan, so I can't say exactly what is left" in r.speech
          and "Not attempted: the rest of the request" not in r.speech, r.speech)
    g_run, _ = crafted_goal("running", [("test.g_slow", "open", None)], ident=alive, progress={"v": 1, "stopped": "running", "steps": []})
    saved_probe = owner.probe
    owner.probe = lambda pid, s, h: owner.ALIVE if pid == alive.pid else saved_probe(pid, s, h)  # type: ignore[assignment]
    try:
        r = ask()
    finally:
        owner.probe = saved_probe  # type: ignore[assignment]
    check("D3 a goal whose open step has a demonstrably live owner is reported as still running",
          r.data["report"]["outcome"] == "running" and "still running" in r.speech.lower(), r.speech)
    g_cut, _ = crafted_goal("running", [("test.g_mkdir", "done", None)], ident=dead, progress={"v": 1, "stopped": "running", "steps": []})
    r = ask()
    check("D4 a goal recorded as running with nothing alive running it is not called finished: it 'may have been cut off'",
          "may have been cut off" in r.speech and "finished, and every" not in r.speech, r.speech)
    check("D5 ...and nothing was changed by saying so", goals_mod.get(g_cut).status is goals_mod.GoalStatus.RUNNING)

    scenario("E. a saved plan supports 'what is left'")
    G.WORLD["write"] = SkillResult(speech="Could not write the file: disk full.", ok=False)
    run2 = G.run_plan(G.GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_write"), done("done")],
                      subgoals=["Make the folder a", "Write the file into it", "Send the file to Sam"])
    r = ask()
    check("E1 the subgoal that never started is listed as not attempted, from the saved breakdown",
          "Not attempted: Send the file to Sam" in r.speech and r.data["report"]["remaining"] == ["Send the file to Sam"], r.speech)
    check("E2 it is not listed as a step", all(s["tool"] != "test.g_send" for s in r.data["report"]["steps"]))
    check("E3 a failed step's success elsewhere is not credited: still partial, 'did not finish'",
          r.data["report"]["outcome"] == "partial" and "did not finish" in r.speech)

    scenario("F. clean run is described as finished; unconfirmed is not called confirmed")
    G.WORLD.clear()
    G.run_plan(G.GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_send"), done("Created, wrote and sent it.")])
    r = ask()
    check("F1 a verified clean run is described as finished with every changing step confirmed",
          r.data["report"]["outcome"] == "complete" and "finished, and every step that changed something was confirmed" in r.speech
          and "I won't resume" not in r.speech, r.speech)
    g_unv, ids = crafted_goal("succeeded", [("test.g_mkdir", "done", None)], ident=dead, progress={"v": 1, "stopped": "completed", "steps": []})
    goals_mod.set_progress(g_unv, {"v": 1, "stopped": "completed", "steps": [{"n": 1, "tool": "test.g_mkdir", "state": "completed", "audit": ids}]})
    r = ask()
    check("F2 a completed-but-never-read-back step is 'couldn't confirm', not confirmed",
          r.data["report"]["outcome"] == "complete_unverified" and "couldn't confirm" in r.speech and "every step that changed" not in r.speech, r.speech)

    scenario("G. legacy and malformed records never become a history")
    legacy = goals_mod.create("old request", "old request")
    goals_mod.update_status(legacy.id, goals_mod.GoalStatus.SUCCEEDED)
    store.conn().execute("UPDATE goals SET created_at=? WHERE id=?", ("2999-01-01T00:00:00+00:00", legacy.id))
    store.conn().commit()
    r = ask()
    check("G1 a legacy goal (no progress, no audit rows) gets its status and an honest 'no record', no steps",
          "no step-by-step record" in r.speech and r.data["report"] is None and "Done:" not in r.speech, r.speech)
    check("G2 ...and is not called a success of anything beyond 'recorded as finished'", "recorded as finished" in r.speech)
    for label, bad in (("steps is a string", {"v": 1, "steps": "all done"}), ("junk entries", {"v": 1, "steps": [1, "x", None, {"state": ["ok"]}]}),
                       ("audit not a list", {"v": 1, "steps": [{"n": 1, "tool": "test.g_mkdir", "state": "completed", "audit": "7"}]})):
        goals_mod.set_progress(legacy.id, bad)
        try:
            r = ask()
            crashed = False
        except Exception as exc:  # noqa: BLE001
            crashed, r = True, SkillResult(speech=repr(exc))
        check(f"G3 malformed progress ({label}) does not crash and claims no success",
              not crashed and "finished, and every" not in r.speech and not any(
                  s["state"] in ("verified", "completed") for s in (r.data.get("report") or {}).get("steps", [])), r.speech)
    store.conn().execute("DELETE FROM goals WHERE id=?", (legacy.id,))
    store.conn().commit()

    scenario("H. nothing sensitive crosses the boundary")
    G.WORLD.clear()
    secret_goal = f"write the file then send it, see https://example.test/p?token={TOKEN} and password {SECRET}"
    run3 = G.run_plan(secret_goal, [call("test.g_mkdir", {"name": "a"}), call("test.g_write", {"name": "a", "text": SECRET, "password": SECRET}),
                                    call("test.g_send", {"name": "a"}), done("done")])
    r = ask()
    blob = r.speech + json.dumps(r.data, default=str)
    check("H1 neither secret appears in the speech or the data", SECRET not in blob and TOKEN not in blob, blob[:300])
    check("H2 the request's own words are not echoed at all: it is identified by when it started",
          "write the file then send it" not in blob and "example.test" not in blob and "started 20" in r.speech and "requested" not in r.data["goal"], r.speech[:200])
    check("H3 the stored progress carries no argument, result or note either",
          SECRET not in (goals_mod.get(run3.data["goal_id"]).progress and json.dumps(goals_mod.get(run3.data["goal_id"]).progress)))

    scenario("I. asking changes nothing and runs nothing")
    gid_last = goals_mod.recent(1)[0].id
    before = (goal_state(gid_last), row_count(), store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0],
              store.conn().execute("SELECT COUNT(*) FROM goals").fetchone()[0])
    ran: list[str] = []
    real_run = EXECUTOR.run

    async def spy(name, *a, **k):
        ran.append(name)
        return await real_run(name, *a, **k)

    EXECUTOR.run = spy  # type: ignore[method-assign]
    G.LOG.clear()
    try:
        for _ in range(3):
            ask()
    finally:
        EXECUTOR.run = real_run  # type: ignore[method-assign]
    after = (goal_state(gid_last), row_count(), store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0],
             store.conn().execute("SELECT COUNT(*) FROM goals").fetchone()[0])
    check("I1 three questions: only the status skill ran through the Executor; no fixture body ran", ran == ["meta.goal_status"] * 3 and G.LOG == [], str(ran))
    check("I2 ...and the goal, the audit log (apart from its own rows), the undo journal and the goals table are unchanged", before == after)
    check("I3 the answer is identical when asked again", ask().speech == ask().speech)


if __name__ == "__main__":
    sys.exit(main())
