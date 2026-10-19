"""Phase 53 -- multi-step goals report what each step actually did, and never claim more.

Found by driving REAL `plan.run` goals (real Executor, real audit, real Goal rows, a scripted model behind the `llm` seam,
`test.*` fixture skills):

  1. a required step failed, the planner went on to a step that depended on it and said "done": `plan.run` returned ok=True,
     stopped=completed, "Created the folder, wrote the file and sent it." -- one flat string, one boolean, no step states;
  2. a step in a worker thread outlived its timeout: the user was told it "timed out" (which reads as "it did not happen"),
     the audit row said `failed` ("interrupted ... cancelled") -- and the thread went on to finish the write. Nothing
     recorded that, and no later view could tell.

Phase 53 gives every executed step one state (verified / completed / failed / unknown / needs_input; plus not_attempted for the
subgoals that never started), builds the account from them, ties each audited step to its goal (`audit.goal_id`), records a
worker-thread step abandoned mid-run as "outcome unknown" and writes its real outcome over that marker when the thread ends, and
settles a goal whose process died. This suite pins those, with the pre-change behaviour reproduced first (section A, B).

Nothing real runs: every fixture is a `test.*` skill, the model is scripted, the databases are throwaway.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import ScriptedPlanner, call, check, done, finish, scenario, scripted_provider  # noqa: E402

from friday import audit, owner, steps, store, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
GATE = threading.Event()
SECRET = "hunter2-SECRET-body"
WORLD: dict[str, SkillResult] = {}


@skill(name="test.g_mkdir", tier="L1", action="modify", description="make a folder (Phase 53 fixture)",
       undo=lambda name="a": {"skill": "test.g_mkdir_undo", "args": {}})
def mkdir(name: str = "a") -> SkillResult:
    LOG.append(f"mkdir:{name}")
    return WORLD.get("mkdir", SkillResult(speech="Created the folder."))


@skill(name="test.g_write", tier="L1", action="modify", description="write a file (Phase 53 fixture)")
def write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"write:{name}")
    return WORLD.get("write", SkillResult(speech="Wrote the file."))


@skill(name="test.g_send", tier="L1", action="communicate", description="send a file (Phase 53 fixture)")
def send(name: str = "a") -> SkillResult:
    LOG.append(f"send:{name}")
    return SkillResult(speech="Sent the file.")


@skill(name="test.g_slow", tier="L1", action="modify", description="slow write in a worker thread (Phase 53 fixture)")
def slow(name: str = "a") -> SkillResult:
    LOG.append("slow:start")
    GATE.wait(20)
    if WORLD.get("slow_raises"):
        raise RuntimeError("disk vanished")
    LOG.append("slow:finished")
    return SkillResult(speech="Wrote the slow file.")


@skill(name="test.g_async_slow", tier="L1", action="modify", description="slow coroutine write (Phase 53 fixture)")
async def async_slow(name: str = "a") -> SkillResult:
    LOG.append("async:start")
    await asyncio.sleep(30)
    LOG.append("async:finished")
    return SkillResult(speech="never")


@skill(name="test.g_read_slow", tier="L0", description="slow read (Phase 53 fixture)")
def read_slow(name: str = "a") -> SkillResult:
    GATE.wait(20)
    return SkillResult(speech="Read it.")


@skill(name="test.g_confirm", tier="L2", action="modify", description="a write that needs confirmation (Phase 53 fixture)")
def confirm_write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append("confirm:ran")
    return SkillResult(speech="Wrote the protected file.")


@skill(name="test.g_stopper", tier="L1", action="modify", description="asks FRIDAY to stop mid-run (Phase 53 fixture)")
def stopper() -> SkillResult:
    LOG.append("stopper")
    INTEL.request_cancel()
    return SkillResult(speech="Did the second thing.")


for _tool in ("test.g_mkdir", "test.g_write", "test.g_send"):
    verify.register(_tool, verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", True, "it ran", "it ran")]))


class Planner(ScriptedPlanner):
    """A scripted model: a decomposition request gets `subgoals` (default none); a decision turn gets the next entry."""

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


# One long-lived event loop on its own thread, like the app's backend: `asyncio.run` would wait at exit for the default
# executor's threads, i.e. for the very abandoned worker-thread step these tests are about.
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="goal-steps-loop").start()


def on_loop(coro, timeout: float = 60.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


def run_plan(goal: str, decisions: list[str], subgoals: list[str] | None = None):
    LOG.clear()
    planner = Planner(decisions, subgoals)
    with scripted_provider(planner):
        return on_loop(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"))


def goal_rows(goal_id: str) -> list[dict]:
    return [dict(r) | {"status": audit.status_of(dict(r))} for r in
            store.conn().execute("SELECT * FROM audit WHERE goal_id=? ORDER BY id", (goal_id,)).fetchall()]


def states(r: SkillResult) -> list[tuple[str, str]]:
    return [(s["tool"], s["state"]) for s in r.data["report"]["steps"]]


GOAL = "Create folder a, write a file in it, then send it"


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s)
    CFG.desktop_observer.enabled = False
    try:
        with iso_env.isolated_runtime():
            section_a()
            section_b()
            section_c()
            section_d()
            section_e()
            section_f()
            section_g()
    finally:
        CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s = saved
        CFG.permissions.overrides = saved_tools
        GATE.set()
    return finish("Phase 53 -- step states, honest multi-step reports, abandoned-step outcomes", time.perf_counter() - t0,
                  min_assertions=70, min_scenarios=7)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. a required step fails and the planner goes on (the pre-change false 'completed')")
    WORLD.clear()
    WORLD["write"] = SkillResult(speech="Could not write the file: disk full.", ok=False)
    r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_send"),
                        done("Created the folder, wrote the file and sent it.")])
    rep = r.data["report"]
    check("A1 the run does not claim success: ok is False (it was True before this phase)", r.ok is False, str(r.ok))
    check("A2 the planner's false 'wrote the file and sent it' is not passed through",
          "wrote the file and sent it" not in r.speech and r.speech.startswith("I didn't finish all of that."), r.speech)
    check("A3 the speech says what was done and what failed, with the failure's own reason",
          "Done: 'test.g_mkdir'" in r.speech and "Failed: 'test.g_write'" in r.speech and "disk full" in r.speech, r.speech)
    check("A4 a step that ran AFTER the failure is not vouched for", "can't vouch for it: 'test.g_send'" in r.speech, r.speech)
    check("A5 the structured report has one state per executed step, in order",
          states(r) == [("test.g_mkdir", "verified"), ("test.g_write", "failed"), ("test.g_send", "verified")], str(states(r)))
    check("A6 ...with the later step flagged and the outcome 'partial'",
          rep["outcome"] == "partial" and rep["steps"][2].get("after_failure") is True and "after_failure" not in rep["steps"][0])
    check("A7 the data says it is not verified", r.data["verified"] is False)
    check("A8 the user is told what can be done next, without anything being retried", "try the failed step again if you ask" in r.speech
          and LOG == ["mkdir:a", "write:a", "send:a"], str(LOG))
    gid = r.data["goal_id"]
    g = goals_mod.get(gid)
    check("A9 the Goal row is PARTIAL (it used to be SUCCEEDED) and its reason carries the account",
          g is not None and g.status is goals_mod.GoalStatus.PARTIAL and "Failed: 'test.g_write'" in (g.failure_reason or ""),
          f"{g.status if g else None} {g.failure_reason if g else ''}")
    rows = goal_rows(gid)
    check("A10 every audited step names the goal; the plan.run call itself does not",
          [x["skill"] for x in rows] == ["test.g_mkdir", "test.g_write", "test.g_send"]
          and all(x["goal_id"] == gid for x in rows)
          and store.conn().execute("SELECT goal_id FROM audit WHERE skill='plan.run' ORDER BY id DESC LIMIT 1").fetchone()[0] is None)
    check("A11 the audit statuses agree with the report (one source of truth)",
          [x["status"] for x in rows] == ["succeeded", "failed", "succeeded"]
          and [(s.tool, s.state.value) for s in steps.ledger_for_goal(gid)] ==
          [("test.g_mkdir", "completed"), ("test.g_write", "failed"), ("test.g_send", "completed")])
    check("A12 each step's row is owned by this process's session (Phase 52)", all(x["session_id"] == owner.identity().id for x in rows))
    check("A13 only the successful state-changing step registered an undo", store.conn().execute(
        "SELECT COUNT(*) FROM undo WHERE audit_id=?", (rows[0]["id"],)).fetchone()[0] == 1 and store.conn().execute(
        "SELECT COUNT(*) FROM undo WHERE audit_id=?", (rows[1]["id"],)).fetchone()[0] == 0)

    scenario("A'. the same goal when everything works, and when a failure is made good")
    WORLD.clear()
    r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_send"), done("Created, wrote and sent it.")])
    check("A'1 a clean, verified run is reported complete, ok and verified, in the planner's own words",
          r.ok and r.speech.startswith("Created, wrote and sent it.") and r.data["verified"] is True
          and r.data["report"]["outcome"] == "complete", f"{r.ok} {r.speech} {r.data['report']['outcome']}")
    check("A'2 the Goal is SUCCEEDED", goals_mod.get(r.data["goal_id"]).status is goals_mod.GoalStatus.SUCCEEDED)
    verify.unregister("test.g_write")
    try:
        r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_send"), done("Created, wrote and sent it.")])
        check("A'3 a step whose effect cannot be read back is 'completed', not 'verified', and the run says it is unconfirmed",
              ("test.g_write", "completed") in states(r) and r.data["report"]["outcome"] == "complete_unverified"
              and r.data["verified"] is False and "unconfirmed" in r.speech, str(states(r)) + r.speech)
    finally:
        verify.register("test.g_write", verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", True, "it ran", "it ran")]))
    WORLD["write"] = SkillResult(speech="Could not write the file: disk full.", ok=False)
    first = {"n": 0}
    real_write = REGISTRY.get("test.g_write").fn

    def flaky(name: str = "a", text: str = "") -> SkillResult:
        first["n"] += 1
        LOG.append(f"write:{name}")
        return SkillResult(speech="Wrote the file.") if first["n"] > 1 else SkillResult(speech="Could not write the file: busy.", ok=False)

    REGISTRY.get("test.g_write").fn = flaky  # type: ignore[misc]
    WORLD.clear()
    try:
        r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write", {"name": "a"}), call("test.g_write", {"name": "b"}),
                            call("test.g_send"), done("Created, wrote and sent it.")])
        check("A'4 a failure a later step of the same tool made good is reported as recovered (Phase 11.2 behaviour kept)",
              r.ok and r.data["report"]["outcome"] in ("complete", "complete_unverified")
              and r.data["report"]["steps"][1].get("recovered") is True, str(r.data["report"]) + r.speech)
    finally:
        REGISTRY.get("test.g_write").fn = real_write  # type: ignore[misc]
    WORLD.clear()

    scenario("A''. subgoals that never started are 'not attempted'")
    WORLD["write"] = SkillResult(speech="Could not write the file: disk full.", ok=False)
    r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_write"), done("done")],
                 subgoals=["Make the folder a", "Write the file into it", "Send the file to Sam"])
    rep = r.data["report"]
    check("A''1 the run stops at the failure and the untouched subgoal is listed as not attempted",
          rep["outcome"] in ("partial", "failed") and "Send the file to Sam" in rep.get("remaining", []) and "Send the file to Sam" in r.speech
          and "send:a" not in LOG, f"{rep} {r.speech} {LOG}")
    saved_sg = [(x.description, x.status.value) for x in (goals_mod.get(r.data["goal_id"]).subgoals or [])]
    check("A''2 the SAVED breakdown no longer says every stage succeeded (a bare `done` used to mark them all)",
          len(saved_sg) == 3 and saved_sg[2][1] == "pending" and any(st != "succeeded" for _, st in saved_sg), str(saved_sg))
    WORLD.clear()
    r = run_plan(GOAL, [call("test.g_mkdir"), call("test.g_write"), call("test.g_send"), done("All three stages done.")],
                 subgoals=["Make the folder a", "Write the file into it", "Send the file to Sam"])
    saved_ok = [x.status.value for x in (goals_mod.get(r.data["goal_id"]).subgoals or [])]
    check("A''3 a run that really finished still settles every stage as succeeded", r.ok and saved_ok == ["succeeded"] * 3, str(saved_ok))


# ---------------------------------------------------------------------------------------------------------------------------
def section_b() -> None:
    scenario("B. a step in a worker thread outlives its timeout (the pre-change 'timed out' / 'failed' lie)")
    WORLD.clear()
    GATE.clear()
    CFG.planner.step_timeout_s = 1.0
    CFG.planner.total_timeout_s = 120.0
    before_undo = store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0]
    r = run_plan("Write the slow file then send it", [call("test.g_slow"), call("test.g_send"), call("test.g_slow"), done("All done.")])
    rep = r.data["report"]
    gid = r.data["goal_id"]
    slow_row = next(x for x in goal_rows(gid) if x["skill"] == "test.g_slow")
    check("B1 the run stops at the step and is not ok", r.ok is False and r.data["stopped"] == "timeout")
    check("B2 the step is 'unknown', not 'failed'", states(r) == [("test.g_slow", "unknown")] and rep["outcome"] == "unknown", str(states(r)))
    check("B3 the user hears that FRIDAY cannot tell whether it took effect, and that it was not retried",
          "can't tell whether it took effect" in r.speech and "did not try it again" in r.speech and "Outcome unknown: 'test.g_slow'" in r.speech,
          r.speech)
    check("B4 the next safe action is to check, and FRIDAY promises not to repeat it itself",
          "Check whether 'test.g_slow' took effect" in r.speech and "won't repeat it on my own" in r.speech)
    check("B5 the dependent step never ran and the abandoned step was started exactly once",
          LOG == ["slow:start"] and "send:a" not in LOG, str(LOG))
    check("B6 the audit row is 'outcome_unknown' (was: failed 'interrupted ... cancelled'), ok NULL, no result",
          slow_row["status"] == "outcome_unknown" and slow_row["ok"] is None and slow_row["result"] is None
          and slow_row["error"] == audit.OUTCOME_UNKNOWN_DETACHED, str(slow_row))
    check("B7 no undo was registered for it", store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0] == before_undo)
    check("B8 the goal's report (from the audit log) agrees: unknown",
          [(s.tool, s.state.value) for s in steps.ledger_for_goal(gid)] == [("test.g_slow", "unknown")])
    check("B9 the goal row is not SUCCEEDED and carries the account",
          goals_mod.get(gid).status is not goals_mod.GoalStatus.SUCCEEDED and "Outcome unknown" in (goals_mod.get(gid).failure_reason or ""))

    scenario("B'. the thread finishes later: its real outcome is written over the marker, once")
    undo_before = store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0]
    rows_before = store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    GATE.set()
    deadline = time.time() + 5
    while time.time() < deadline and "slow:finished" not in LOG:
        time.sleep(0.05)
    time.sleep(0.3)
    after = next(x for x in goal_rows(gid) if x["skill"] == "test.g_slow")
    check("B'1 the abandoned step really did finish after the run had moved on", "slow:finished" in LOG)
    check("B'2 its audit row now says what happened: succeeded, with the result", after["status"] == "succeeded"
          and after["result"] == "Wrote the slow file." and after["error"] is None, str(after))
    check("B'3 exactly one row for it (no duplicate), nothing was run again, and still no undo entry",
          store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == rows_before and LOG.count("slow:start") == 1
          and store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0] == undo_before)
    check("B'4 the earlier report is history: the goal row still says the run ended with an unknown step",
          goals_mod.get(gid).status is not goals_mod.GoalStatus.SUCCEEDED)

    scenario("B''. the abandoned thread raises, a read times out, a coroutine tool is really cancelled")
    GATE.clear()
    WORLD["slow_raises"] = SkillResult(speech="x")
    r = run_plan("Write the slow file", [call("test.g_slow"), done("ok")])
    gid2 = r.data["goal_id"]
    GATE.set()
    time.sleep(0.8)
    row = next(x for x in goal_rows(gid2) if x["skill"] == "test.g_slow")
    check("B''1 a late exception is recorded as a failure with its message", row["status"] == "failed" and row["error"] == "disk vanished", str(row))
    WORLD.clear()
    GATE.clear()
    r = run_plan("Read the slow file", [call("test.g_read_slow"), done("ok")])
    GATE.set()
    check("B''2 a READ that times out changed nothing: failed, 'timed out', not unknown",
          states(r) == [("test.g_read_slow", "failed")] and "timed out" in r.speech and "Outcome unknown" not in r.speech, r.speech + str(states(r)))
    r = run_plan("Write the async file", [call("test.g_async_slow"), done("ok")])
    arow = next(x for x in goal_rows(r.data["goal_id"]) if x["skill"] == "test.g_async_slow")
    check("B''3 a coroutine tool was genuinely cancelled at its timeout: recorded as before (failed, cancelled), never ran on",
          arow["status"] == "failed" and "cancelled" in arow["error"] and "async:finished" not in LOG, str(arow))
    check("B''4 ...yet the run is still told the step is unknown (it may have changed something before it was cancelled)",
          states(r) == [("test.g_async_slow", "unknown")], str(states(r)))
    CFG.planner.step_timeout_s = 90.0


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    scenario("C. the WHOLE run is cut off while a worker-thread step is in flight")
    WORLD.clear()
    GATE.clear()
    CFG.planner.step_timeout_s = 90.0
    CFG.planner.total_timeout_s = 1.5
    r = run_plan("Write the slow file then send it", [call("test.g_mkdir"), call("test.g_slow"), call("test.g_send"), done("done")])
    CFG.planner.total_timeout_s = 120.0
    gid = r.data["goal_id"]
    check("C1 the run stopped on the overall time limit and is not ok", r.ok is False and r.data["stopped"] == "timeout")
    check("C2 the in-flight step (no Observation exists for it) is reported unknown from the audit log",
          ("test.g_slow", "unknown") in states(r) and r.data["report"]["outcome"] == "unknown", str(r.data["report"]))
    check("C3 the step that did finish is reported done; the dependent step never ran",
          ("test.g_mkdir", "verified") in states(r) and "send:a" not in LOG, str(LOG))
    check("C4 the speech says so", "Outcome unknown: 'test.g_slow'" in r.speech and "won't repeat it on my own" in r.speech, r.speech)
    row = next(x for x in goal_rows(gid) if x["skill"] == "test.g_slow")
    check("C5 its audit row is outcome_unknown, not failed", row["status"] == "outcome_unknown", str(row))
    GATE.set()
    time.sleep(0.6)
    check("C6 when the thread ends, the row gets the real outcome and the skill was run once",
          next(x for x in goal_rows(gid) if x["skill"] == "test.g_slow")["status"] == "succeeded" and LOG.count("slow:start") == 1)


# ---------------------------------------------------------------------------------------------------------------------------
def section_d() -> None:
    scenario("D. STOP between steps, and a confirmation that is declined")
    WORLD.clear()
    GATE.set()
    r = run_plan("Create folder a, do the second thing, then send it",
                 [call("test.g_mkdir"), call("test.g_stopper"), call("test.g_send"), done("done")])
    gid = r.data["goal_id"]
    check("D1 a stop requested during step 2 ends the run before step 3 (existing Phase 28 behaviour kept)",
          r.data["stopped"] == "cancelled" and "send:a" not in LOG and LOG == ["mkdir:a", "stopper"], str(LOG))
    check("D2 the speech is still the Phase 28 account, not a second one stacked on it",
          r.speech.startswith("Stopped.") and "had already run" in r.speech and "Done:" not in r.speech, r.speech)
    check("D3 the structured report calls it 'stopped' and lists both steps that ran", r.data["report"]["outcome"] == "stopped"
          and [s["tool"] for s in r.data["report"]["steps"]] == ["test.g_mkdir", "test.g_stopper"])
    check("D4 the goal is CANCELLED, as before", goals_mod.get(gid).status is goals_mod.GoalStatus.CANCELLED)

    async def decline(skill, args, preview):
        return False

    async def approve(skill, args, preview):
        return True

    EXECUTOR.set_confirm_handler(decline)
    r = run_plan("Create folder a, write a protected file, then send it",
                 [call("test.g_mkdir"), call("test.g_confirm", {"name": "p", "text": SECRET}), done("done")])
    rep = r.data["report"]
    check("D5 a declined confirmation is 'needs_input': the step did not run and the user is asked",
          "confirm:ran" not in LOG and rep["outcome"] == "needs_input" and ("test.g_confirm", "needs_input") in states(r)
          and "Waiting on you: 'test.g_confirm' did not run." in r.speech and "Tell me whether to go ahead" in r.speech, r.speech + str(rep))
    check("D6 the run is not ok and nothing after it was attempted", r.ok is False and LOG == ["mkdir:a"], str(LOG))
    drow = next(x for x in goal_rows(r.data["goal_id"]) if x["skill"] == "test.g_confirm")
    check("D7 the audit row is 'denied' with its owner, and recovery cannot turn it into anything else",
          drow["status"] == "denied" and drow["session_id"] == owner.identity().id and audit.reconcile_orphans(lambda *a: owner.DEAD) == 0)
    check("D8 neither the speech, the report nor the audit row contains the secret argument",
          SECRET not in r.speech and SECRET not in json.dumps(rep) and SECRET not in json.dumps(drow))
    EXECUTOR.set_confirm_handler(approve)
    r = run_plan("Create folder a, write a protected file, then send it",
                 [call("test.g_mkdir"), call("test.g_confirm", {"name": "p", "text": SECRET}), done("Created and wrote it.")])
    crow = next(x for x in goal_rows(r.data["goal_id"]) if x["skill"] == "test.g_confirm")
    check("D9 once approved, the same step runs, as a 'confirmed' row owned by this session",
          "confirm:ran" in LOG and crow["decision"] == "confirmed" and crow["status"] == "succeeded", str(crow))
    check("D10 the stored audit args never hold the secret", SECRET not in json.dumps(crow))


# ---------------------------------------------------------------------------------------------------------------------------
def section_e() -> None:
    scenario("E. a repeated request or repeated call does not repeat an uncertain side effect")
    WORLD.clear()
    GATE.clear()
    CFG.planner.step_timeout_s = 1.0
    r = run_plan("Write the slow file", [call("test.g_slow"), call("test.g_slow"), call("test.g_slow"), done("done")])
    GATE.set()
    time.sleep(0.5)
    check("E1 a planner that keeps asking for the abandoned step does not get it run again (it was started once)",
          LOG.count("slow:start") == 1 and r.data["stopped"] == "timeout", str(LOG))
    CFG.planner.step_timeout_s = 90.0
    rows = goal_rows(r.data["goal_id"])
    check("E2 one audit row for the one execution: the repeated requests left no row of their own", len(rows) == 1
          and rows[0]["skill"] == "test.g_slow", str([x["skill"] for x in rows]))


# ---------------------------------------------------------------------------------------------------------------------------
def section_f() -> None:
    scenario("F. a goal whose process died is settled from the audit log (nothing resumed, retried or undone)")
    c = store.conn()

    def make_goal(status: str = "running") -> str:
        g = goals_mod.create("settle me", "settle me")
        goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
        return g.id

    dead = owner.Identity("fake-dead53", 4_100_001, 1.0, owner._host())
    alive = owner.Identity("fake-alive53", 4_100_002, 1.0, owner._host())

    def step_row(gid: str, ident: owner.Identity, name: str, *, finish: bool) -> int:
        with owner._use_identity(ident), audit.goal_scope(gid):
            rid = audit.record(actor="text", skill=name, tier="L1", args={}, decision="auto")
        if finish:
            audit.complete(rid, ok=True, result="Done.", duration_ms=1)
        return rid

    g_mid = make_goal()
    step_row(g_mid, dead, "test.f_one", finish=True)
    step_row(g_mid, dead, "test.f_two", finish=False)
    g_between = make_goal()
    step_row(g_between, dead, "test.f_one", finish=True)
    g_alive = make_goal()
    step_row(g_alive, alive, "test.f_one", finish=False)
    g_none = make_goal()
    g_done = make_goal("succeeded")
    step_row(g_done, dead, "test.f_one", finish=True)
    g_unfinished_only = make_goal()
    step_row(g_unfinished_only, dead, "test.f_one", finish=False)
    ran: list[str] = []
    real_run = EXECUTOR.run

    async def spy(*a, **k):
        ran.append(str(a))
        return await real_run(*a, **k)

    undo_before = c.execute("SELECT COUNT(*) FROM undo").fetchone()[0]
    EXECUTOR.run = spy  # type: ignore[method-assign]
    try:
        verdicts = {4_100_001: owner.DEAD, 4_100_002: owner.ALIVE}
        probe = lambda pid, s, h: verdicts.get(pid, owner.UNKNOWN)  # noqa: E731
        audit.reconcile_orphans(probe)
        n = steps.settle_interrupted_goals(probe)
    finally:
        EXECUTOR.run = real_run  # type: ignore[method-assign]
    check("F1 exactly the three goals whose last step belonged to a dead process were settled", n == 3, str(n))
    mid, between, unfinished = goals_mod.get(g_mid), goals_mod.get(g_between), goals_mod.get(g_unfinished_only)
    check("F2 a goal with a recorded success and an interrupted step is PARTIAL, and the reason says what is unknown",
          mid.status is goals_mod.GoalStatus.PARTIAL and "interrupted" in mid.failure_reason
          and "Outcome unknown: 'test.f_two'" in mid.failure_reason and "nothing was retried or resumed" in mid.failure_reason,
          mid.failure_reason)
    check("F3 a goal cut off between steps (every recorded step done) is PARTIAL and says the rest was not attempted",
          between.status is goals_mod.GoalStatus.PARTIAL and "Not attempted" in between.failure_reason)
    check("F4 a goal whose only step has no outcome is FAILED, not succeeded", unfinished.status is goals_mod.GoalStatus.FAILED)
    check("F5 a live owner's goal, a goal with no recorded step and a finished goal are untouched",
          goals_mod.get(g_alive).status is goals_mod.GoalStatus.RUNNING and goals_mod.get(g_none).status is goals_mod.GoalStatus.RUNNING
          and goals_mod.get(g_done).status is goals_mod.GoalStatus.SUCCEEDED)
    check("F6 nothing was executed, retried or undone", ran == [] and c.execute("SELECT COUNT(*) FROM undo").fetchone()[0] == undo_before)
    check("F7 settling again changes nothing", steps.settle_interrupted_goals(probe) == 0)
    g_race = make_goal()
    step_row(g_race, dead, "test.f_one", finish=True)

    def finishes_meanwhile(pid, s, h):
        goals_mod.update_status(g_race, goals_mod.GoalStatus.SUCCEEDED)
        return owner.DEAD

    steps.settle_interrupted_goals(finishes_meanwhile)
    check("F8 a goal that finished while settlement was deciding is never overwritten", goals_mod.get(g_race).status is goals_mod.GoalStatus.SUCCEEDED)
    g_mine = make_goal()
    with audit.goal_scope(g_mine):
        audit.complete(audit.record(actor="text", skill="test.f_one", tier="L1", args={}, decision="auto"), ok=True, result="Done.")
    steps.settle_interrupted_goals(lambda *a: owner.DEAD)
    check("F10 a goal this very process is running is never settled, whatever a probe says",
          goals_mod.get(g_mine).status is goals_mod.GoalStatus.RUNNING)
    check("F9 an unknown liveness verdict leaves the goal running", steps.settle_interrupted_goals(lambda *a: owner.UNKNOWN) == 0)


# ---------------------------------------------------------------------------------------------------------------------------
def section_g() -> None:
    scenario("G. the building blocks")
    from friday.orchestrator import Observation, PlanStep

    def obs(tool: str, ok: bool, error: str = "", speech: str = "", data: dict | None = None) -> Observation:
        return Observation(PlanStep(tool, {}), ok, speech, data or {}, error=error)

    check("G1 messages to the planner (blocked repeats, rejected mismatches) are not steps",
          steps.classify(obs("test.g_write", False, "repeated_call")) is None and steps.classify(obs("test.g_write", False, "intent_mismatch")) is None
          and steps.classify(obs("test.g_write", False, "cancelled_before_start")) is None)
    check("G2 an interrupted step is unknown; so is a timed-out state change; a timed-out read is failed",
          steps.classify(obs("test.g_write", False, "interrupted")) is steps.StepState.UNKNOWN
          and steps.classify(obs("test.g_write", False, "timeout")) is steps.StepState.UNKNOWN
          and steps.classify(obs("test.g_read_slow", False, "timeout")) is steps.StepState.FAILED)
    check("G3 a declined confirmation needs input; an ordinary failure fails; success is completed",
          steps.classify(obs("test.g_write", False, "confirmation_declined")) is steps.StepState.NEEDS_INPUT
          and steps.classify(obs("test.g_write", False, "RuntimeError")) is steps.StepState.FAILED
          and steps.classify(obs("test.g_write", True)) is steps.StepState.COMPLETED)
    rep = steps.build_report([], stopped="completed")
    check("G4 a run that executed nothing is not 'complete'", rep.outcome == "complete_unverified" and not rep.needs_account)
    rep = steps.build_report([obs("test.g_write", False, "RuntimeError", "boom")], stopped="failure")
    check("G5 a lone failure is 'failed' and keeps the existing reply (no extra account)", rep.outcome == "failed" and not rep.needs_account)
    rep = steps.build_report([obs("test.g_mkdir", True), obs("test.g_write", False, "RuntimeError", "boom " + "x" * 300)], stopped="failure")
    check("G6 the note on a failed step is short and carries no arguments", rep.outcome == "partial" and len(rep.steps[1].note) <= 90
          and "args" not in json.dumps(rep.to_dict()))
    check("G7 the OS-level facts the report relies on exist: audit.goal_id column and index", "goal_id" in {
        r[1] for r in store.conn().execute("PRAGMA table_info(audit)")} and store.conn().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='idx_audit_goal'").fetchone()[0] == 1)
    check("G8 a direct (non-goal) call has no goal id", (lambda rid: dict(store.conn().execute(
        "SELECT goal_id FROM audit WHERE id=?", (rid,)).fetchone())["goal_id"])(
        on_loop(_direct_row())) is None)


async def _direct_row() -> int:
    await EXECUTOR.run("test.g_mkdir", {})
    return int(store.conn().execute("SELECT MAX(id) FROM audit").fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())
