"""Phase 69 -- the run's final outcome respects the recovery decision the step report already made.

Found by Phase 68: a state change whose read-back contradicted the tool, redone by a VERIFIED later step that pursued the same objective, is
`failed` + `recovered` in the step report (Phase 54's rule: the same tool on the same target, or another tool of the same kind of action
on the same target). But `verify.summarize` -- which the orchestrator's `done` guard and `plan.run` both consult -- recognised a recovery
only when the later call was IDENTICAL (`_identity`: same tool, same arguments, spelling aside). The orchestrator's repeat guard refuses an
identical call, so a real retry always differs in some argument (the content written): the report said recovered and the guard still
said "failed", the run ended `failure`, the goal `failed`, and a stage that had succeeded on its own steps was marked failed.

Phase 69 lets `summarize` accept a later step that is VERIFIED and that `steps.same_objective` accepts, in addition to the identical call it
always accepted. Nothing else changes: the later step must itself have been read back and found good (an unverified completion is the
Phase 54 label, not new evidence about the world), a different file / another kind of action / a retry that was contradicted again
recovers nothing, and every other way a run can be unfinished (an unrecovered tool failure, a step waiting on the user) is still decided by
the step report, exactly as before.

Drives REAL `plan.run` goals (real Executor, audit, Goal rows, a scripted model behind the `llm` seam, `test.ro_*` fixtures with registered
verifiers) and `verify.summarize` directly. Nothing real runs and every database is a throwaway.
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
from phase21_common import ScriptedPlanner, check, done, finish, scenario, scripted_provider  # noqa: E402

from friday import auditview, steps, store, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
CONTRA: set[str] = set()        # names whose read-back always contradicts the tool's success
CONTRA_ONCE: set[str] = set()   # ... only the first time
FAIL_NAMES: set[str] = set()    # test.ro_write fails for these names (the tool itself reports failure)
SECRET = "RO-READBACK-5519"


@skill(name="test.ro_write", tier="L1", action="modify", description="write a named file (Phase 69 fixture)")
def write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"write:{name}")
    if name in FAIL_NAMES:
        return SkillResult(speech="Could not write the file: disk full.", ok=False)
    return SkillResult(speech="Wrote the file.")


@skill(name="test.ro_append", tier="L1", action="modify", description="append to a named file (Phase 69 fixture)")
def append(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"append:{name}")
    return SkillResult(speech="Appended to the file.")


@skill(name="test.ro_note", tier="L1", action="modify", description="a state change on a named file nothing can read back (Phase 69 fixture)")
def note(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"note:{name}")
    return SkillResult(speech="Noted it.")


@skill(name="test.ro_send", tier="L1", action="communicate", description="send a named file (Phase 69 fixture)")
def send(name: str = "a") -> SkillResult:
    LOG.append(f"send:{name}")
    return SkillResult(speech="Sent the file.")


@skill(name="test.ro_confirm", tier="L2", action="modify", description="a write that needs confirmation (Phase 69 fixture)")
def confirm_write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append("confirm:ran")
    return SkillResult(speech="Wrote the protected file.")


for _t in ("test.ro_write", "test.ro_append", "test.ro_note", "test.ro_confirm"):
    auditview.POLICY[_t] = {"name": auditview.KEEP, "text": auditview.OMIT}
auditview.POLICY["test.ro_send"] = {"name": auditview.KEEP}


def _readback(args, data, before):  # noqa: ANN001
    name = args.get("name", "")
    bad = name in CONTRA or name in CONTRA_ONCE
    CONTRA_ONCE.discard(name)
    return [verify.Check("fixture effect", not bad, "the file is there", SECRET if bad else "the file is there")]


for _tool in ("test.ro_write", "test.ro_append"):    # test.ro_note / test.ro_confirm: nothing can be read back
    verify.register(_tool, verify.Verifier(_readback))
verify.register("test.ro_send", verify.Verifier(lambda a, d, b: [verify.Check("sent", True, "sent", "sent")]))   # a send always checks out


class Planner(ScriptedPlanner):
    def __init__(self, decisions: list[str], stages: list[str] | None = None) -> None:
        super().__init__(decisions)
        self.stages = stages
        self.n = 0

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        system = next((m.content for m in request.messages if m.role == "system"), "")
        if "Break a real-world goal down" in system:
            text = json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in self.stages or []]})
        else:
            text = self.replies[min(self.n, len(self.replies) - 1)]
            self.n += 1
        return LlmResponse(text=text, model="scripted", provider=self.name)


_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="recovery-outcome-loop").start()


def on_loop(coro, timeout: float = 60.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


def call(tool: str, args: dict | None = None, stage: int | None = None) -> str:
    payload: dict = {"action": "call", "tool": tool, "args": args or {}}
    if stage is not None:
        payload["subgoal_index"] = stage
    return json.dumps(payload)


STAGES = ["Write the file", "Send the file"]


def run_plan(decisions: list[str], stages: list[str] | None = None, goal: str = "Write file a, then send it"):
    LOG.clear()
    with scripted_provider(Planner(decisions, stages if stages is not None else [])):
        return on_loop(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"))


def stage_rows(gid: str) -> list[str]:
    return [s.status.value for s in (goals_mod.get(gid).subgoals or [])]


def tuples(report: dict) -> list[tuple]:
    return [(s["tool"].split(".")[-1], s["state"], bool(s.get("recovered"))) for s in report["steps"]]


def gstatus(gid: str) -> str:
    return goals_mod.get(gid).status.value


def restart() -> None:
    from friday import dbplace

    c = getattr(store._local, "conns", {}).pop(store._key(dbplace.resolve()), None)
    if c is not None:
        c.close()


def count(table: str) -> int:
    return int(store.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def vstatus(r) -> str:  # noqa: ANN001
    return r.data.get("verification", {}).get("status", "not_applicable")


def unresolved(p: str, r, what: str, *, want_steps: list[tuple] | None = None) -> None:
    """The run is NOT a success: the guard held, the run is a failure, the goal is not succeeded, the account does not call it complete."""
    gid = r.data["goal_id"]
    check(f"{p} {what}: the done guard holds -- stopped failure, verification failed, not ok, the goal is not succeeded, the report is not complete",
          r.data["stopped"] == "failure" and vstatus(r) == "failed" and r.ok is False and gstatus(gid) != "succeeded"
          and r.data["report"]["outcome"] in ("partial", "failed") and r.data["verified"] is False, f"{r.data['stopped']} {vstatus(r)} {r.ok} {gstatus(gid)} {r.data['report']}")
    if want_steps is not None:
        check(f"{p}a ...with the steps {want_steps}", tuples(r.data["report"]) == want_steps, str(tuples(r.data["report"])))


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s, CFG.planner.subgoal_evidence_advance)
    CFG.desktop_observer.enabled = False
    try:
        with iso_env.isolated_runtime():
            section_a()
            section_b()
            section_c()
            section_d()
            section_e()
    finally:
        (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s,
         CFG.planner.subgoal_evidence_advance) = saved
        CFG.permissions.overrides = saved_tools
        CONTRA.clear()
        CONTRA_ONCE.clear()
        FAIL_NAMES.clear()
    return finish("Phase 69 -- the run's outcome follows the recovery decision; a label alone never overrides a failed read-back",
                  time.perf_counter() - t0, min_assertions=30, min_scenarios=5)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. the mismatch: a contradicted write redone by a VERIFIED retry (other content) -- recovered in the report, so the run is too")
    CFG.planner.subgoal_evidence_advance = True
    CONTRA_ONCE.add("r")
    r = run_plan([call("test.ro_write", {"name": "r", "text": "x"}), call("test.ro_write", {"name": "r", "text": "again"}), done("Wrote r.")],
                 goal="Write file r")
    gid = r.data["goal_id"]
    check("A1 the step report: the contradicted write is failed AND recovered, the retry verified",
          tuples(r.data["report"]) == [("ro_write", "failed", True), ("ro_write", "verified", False)], str(tuples(r.data["report"])))
    check("A2 the goal-level verification now agrees: verified, nothing failed (the read-back that failed was redone and confirmed)",
          vstatus(r) == "verified" and r.data["verification"]["failed"] == [], str(r.data.get("verification")))
    check("A3 the run ends completed and ok, the goal is succeeded, the account is complete_unverified (a step did fail on the way)",
          r.data["stopped"] == "completed" and r.ok and gstatus(gid) == "succeeded" and r.data["report"]["outcome"] == "complete_unverified"
          and r.data["verified"] is True, f"{r.data['stopped']} {r.ok} {gstatus(gid)} {r.data['report']['outcome']} {r.data['verified']}")
    check("A4 the answer does not say it could not confirm, and does not carry the read-back value",
          "couldn't confirm" not in r.speech and "did not check out" not in r.speech and SECRET not in r.speech, r.speech)
    live = r.data["report"]
    audit_n, ran = count("audit"), list(LOG)
    ok = True
    for _ in range(3):
        restart()
        rebuilt = steps.reconstruct(gid)
        steps.refresh_goals()
        ok = ok and rebuilt.to_dict() == live and gstatus(gid) == "succeeded"
    check("A5 after three restarts / refreshes the rebuilt account is still the live one (Phase 68's failed + recovered step survives), the goal succeeded",
          ok and LOG == ran and count("audit") == audit_n, str(steps.reconstruct(gid).to_dict()))

    CONTRA_ONCE.add("g")
    r = run_plan([call("test.ro_write", {"name": "g", "text": "x"}, 0), call("test.ro_write", {"name": "g", "text": "again"}, 0),
                  call("test.ro_send", {"name": "g"}, 1), done("Wrote and sent it.")], stages=STAGES)
    gid = r.data["goal_id"]
    check("A6 with stages (Phase 67): both stages succeeded -- the second is no longer marked failed by the guard -- and the goal succeeded",
          stage_rows(gid) == ["succeeded", "succeeded"] and r.ok and gstatus(gid) == "succeeded" and r.data["stopped"] == "completed",
          f"{stage_rows(gid)} {r.ok} {gstatus(gid)} {r.data['stopped']}")
    check("A7 ...and the stages are the ones a restart finds", all((restart(), steps.refresh_goals(), stage_rows(gid) == ["succeeded", "succeeded"])[2] for _ in range(2)))


def section_b() -> None:
    scenario("B. recovery by ANOTHER tool of the same kind on the same target (Phase 54's rule), verified")
    CONTRA_ONCE.add("b")
    r = run_plan([call("test.ro_write", {"name": "b", "text": "x"}), call("test.ro_append", {"name": "b", "text": "x"}), done("Wrote b.")],
                 goal="Write file b")
    gid = r.data["goal_id"]
    check("B1 a write contradicted, then an append to the SAME file read back and verified: recovered, the run completes and the goal succeeds",
          tuples(r.data["report"]) == [("ro_write", "failed", True), ("ro_append", "verified", False)] and vstatus(r) == "verified"
          and r.data["stopped"] == "completed" and r.ok and gstatus(gid) == "succeeded", f"{tuples(r.data['report'])} {vstatus(r)} {r.data['stopped']} {gstatus(gid)}")


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    scenario("C. negative controls: unrelated or insufficient work does not recover a failed read-back")
    CONTRA_ONCE.clear()
    CONTRA.clear()
    CONTRA.add("a")
    r = run_plan([call("test.ro_write", {"name": "a", "text": "x"}), call("test.ro_write", {"name": "z", "text": "x"}), done("Wrote.")])
    unresolved("C1", r, "a verified write to ANOTHER file", want_steps=[("ro_write", "failed", False), ("ro_write", "verified", False)])

    r = run_plan([call("test.ro_write", {"name": "a", "text": "x"}), call("test.ro_send", {"name": "a"}), done("Wrote.")])
    unresolved("C2", r, "a verified success of another KIND of action on the same name (a send is not the write)",
               want_steps=[("ro_write", "failed", False), ("ro_send", "verified", False)])

    r = run_plan([call("test.ro_write", {"name": "a", "text": "x"}), call("test.ro_write", {"name": "a", "text": "again"}), done("Wrote.")])
    unresolved("C3", r, "a retry whose read-back is contradicted AGAIN", want_steps=[("ro_write", "failed", False), ("ro_write", "failed", False)])

    CONTRA.clear()
    CONTRA_ONCE.add("n")
    r = run_plan([call("test.ro_write", {"name": "n", "text": "x"}), call("test.ro_note", {"name": "n", "text": "x"}), done("Wrote.")])
    unresolved("C4", r, "a retry that could not be read back (a completion, not new evidence about the world)",
               want_steps=[("ro_write", "failed", True), ("ro_note", "completed", False)])
    check("C4b ...although the step report labels the failed step recovered (Phase 54: completed counts for the report) -- the label alone does not "
          "override the failed read-back, and the answer still says it could not confirm",
          r.data["report"]["steps"][0].get("recovered") is True and "couldn't confirm" in r.speech, r.speech)

    CONTRA_ONCE.add("p")
    CONTRA.add("o")
    r = run_plan([call("test.ro_write", {"name": "p", "text": "x"}), call("test.ro_write", {"name": "o", "text": "x"}),
                  call("test.ro_write", {"name": "p", "text": "again"}), done("Wrote.")])
    check("C5 two contradicted files, only one redone and verified: the other keeps the guard shut, and it alone is named",
          r.data["stopped"] == "failure" and vstatus(r) == "failed" and len(r.data["verification"]["failed"]) == 1
          and "ro_write" in r.data["verification"]["failed"][0] and r.ok is False,
          f"{r.data['stopped']} {r.data.get('verification')}")
    check("C5a ...the report agrees: the retried file's failure is recovered, the other's is not",
          [(t[1], t[2]) for t in tuples(r.data["report"])] == [("failed", True), ("failed", False), ("verified", False)], str(tuples(r.data["report"])))
    CONTRA.clear()
    CONTRA_ONCE.clear()


def section_d() -> None:
    scenario("D. a recovered read-back never hides other unfinished work")
    CONTRA_ONCE.add("d")
    FAIL_NAMES.add("e")
    r = run_plan([call("test.ro_write", {"name": "d", "text": "x"}), call("test.ro_write", {"name": "d", "text": "again"}),
                  call("test.ro_write", {"name": "e", "text": "x"}), done("Wrote d and e.")], goal="Write d and e")
    gid = r.data["goal_id"]
    check("D1 the read-back is recovered but a DIFFERENT step's tool failure is not: the guard lets `done` through (nothing failed its read-back), "
          "yet the report is partial, the run not ok, the goal partial",
          r.data["stopped"] == "completed" and vstatus(r) == "verified"
          and tuples(r.data["report"]) == [("ro_write", "failed", True), ("ro_write", "verified", False), ("ro_write", "failed", False)]
          and r.data["report"]["outcome"] == "partial" and r.ok is False and gstatus(gid) == "partial", f"{r.data['report']} {r.ok} {gstatus(gid)}")
    check("D2 ...and the answer names the failure, not a success", "Failed: 'test.ro_write'" in r.speech and "I didn't finish all of that" in r.speech, r.speech)
    FAIL_NAMES.clear()

    async def decline(skill_, args, preview):  # noqa: ANN001
        return False

    EXECUTOR.set_confirm_handler(decline)
    CONTRA_ONCE.add("f")
    r = run_plan([call("test.ro_write", {"name": "f", "text": "x"}), call("test.ro_write", {"name": "f", "text": "again"}),
                  call("test.ro_confirm", {"name": "q", "text": "x"}), done("Done.")], goal="Write f and q")
    gid = r.data["goal_id"]
    check("D3 a recovered read-back plus a step waiting on the user (the run stops at the declined step): needs_input, not ok, the goal not "
          "succeeded, the protected step never ran",
          r.data["report"]["outcome"] == "needs_input" and r.ok is False and gstatus(gid) != "succeeded" and "confirm:ran" not in LOG,
          f"{r.data['report']} {r.ok} {gstatus(gid)} {LOG}")


# ---------------------------------------------------------------------------------------------------------------------------
def vobs(tool: str, args: dict, status: str) -> Observation:
    o = Observation(PlanStep(tool, dict(args)), status != "failed", "", {}, error="" if status != "failed" else "postcondition_failed")
    o.verification = verify.Verification(verify.VerifyStatus(status), [], reason=f"{status} reason")
    return o


def section_e() -> None:
    scenario("E. verify.summarize directly: the old rule is kept, the new one is exactly Phase 54's objective AND a verified later step")
    W = lambda n, t="x": ("test.ro_write", {"name": n, "text": t})  # noqa: E731
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs(*W("a", "again"), "verified")])
    check("E1 same tool + same target, other content, later verified -> recovered", gv.status == "verified" and gv.failed == [], str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs("test.ro_append", {"name": "a", "text": "x"}, "verified")])
    check("E2 another tool of the same kind on the same target, later verified -> recovered", gv.status == "verified", str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs(*W("b"), "verified")])
    check("E3 another target -> not recovered", gv.status == "failed" and len(gv.failed) == 1, str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs("test.ro_send", {"name": "a"}, "verified")])
    check("E4 another kind of action on the same target -> not recovered", gv.status == "failed", str(gv.to_dict()))
    for later in ("unverified", "partial", "failed"):
        gv = verify.summarize([vobs(*W("a"), "failed"), vobs(*W("a", "again"), later)])
        check(f"E5 the later step is '{later}', not verified -> not recovered", gv.status == "failed", str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a", "again"), "verified"), vobs(*W("a"), "failed")])
    check("E6 a verified step BEFORE the failure does not recover it (it must be later)", gv.status == "failed", str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs(*W("b"), "failed"), vobs(*W("a", "again"), "verified")])
    check("E7 each failure needs its own recovery: only the retried file is cleared, the other is still named",
          gv.status == "failed" and len(gv.failed) == 1 and gv.failed[0].startswith("test.ro_write"), str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "failed"), vobs(*W("a", "again"), "verified"), vobs(*W("c"), "unverified")])
    check("E8 recovery does not hide another step that merely could not be read back: partial", gv.status == "partial" and gv.failed == [], str(gv.to_dict()))
    # the rule that was already there is kept: an unclassified tool is recognised by an identical call, spelling aside
    gv = verify.summarize([vobs("test.ro_raw", {"name": "A"}, "failed"), vobs("test.ro_raw", {"name": "a"}, "verified")])
    check("E9 the identical-call rule (Phase 22) still recovers a tool nothing classifies, spelling aside", gv.status == "verified", str(gv.to_dict()))
    gv = verify.summarize([vobs("test.ro_raw", {"name": "a"}, "failed"), vobs("test.ro_raw", {"name": "b"}, "verified")])
    check("E10 ...and an unclassified tool with different arguments recovers nothing (Phase 54: only an identical call matches)",
          gv.status == "failed", str(gv.to_dict()))
    gv = verify.summarize([vobs(*W("a"), "verified")])
    check("E11 no failure at all: unchanged", gv.status == "verified" and gv.failed == [])
    gv = verify.summarize([])
    check("E12 no verification at all: not_applicable", gv.status == "not_applicable")


if __name__ == "__main__":
    sys.exit(main())
