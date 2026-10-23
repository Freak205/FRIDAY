"""Phase 68 -- a step whose read-back contradicted the tool's success stays `failed` after persistence, refresh and restart.

Found by Phase 67 (reported there, not fixed): a tool succeeds, its required read-back fails, and the step is `failed` live. But the audit
row only has the TOOL's verdict -- "succeeded" -- so rebuilt from the database the stored `failed` and the row "disagreed" and the step
became `unknown` (Phase 54's merge rule); `refresh_goals` then stored that `unknown` (dropping the verdict, which `reconstruct` kept only for
`verified` steps), and the NEXT reconstruction read stored-unknown + a succeeded row as `completed`: "Done: <tool>" for a step the read-back
had shown did not take effect. A goal that had recovered the step (a later verified repeat) was rebuilt as `unknown` on the first restart.

Phase 68 keeps the verdict: a stored `failed` whose stored read-back verdict is a contradiction (status failed AND an explicit false check)
is not a disagreement with a row that says the tool succeeded -- the step stays failed -- and `reconstruct` keeps that verdict for as long as
the step is failed or unknown, so no refresh loses it and a late real result cannot turn the step into a completion. Missing or uninformative
evidence is still `unknown`, never failed and never success.

Drives REAL `plan.run` goals (real Executor, real audit, real Goal rows, a scripted model behind the `llm` seam, `test.vd_*` fixture skills with
registered verifiers) through the whole lifecycle -- live, persisted, rebuilt after a "restart" (this thread's connection dropped, so the file is
re-opened), refreshed, rebuilt again, three times over -- and crafted post-restart states for the merge matrix and the late-result chain.
Nothing real runs and every database is a throwaway.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import ScriptedPlanner, check, done, finish, scenario, scripted_provider  # noqa: E402

from friday import audit, auditview, dbplace, owner, steps, store, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
CONTRA: set[str] = set()        # names whose read-back always contradicts the tool's success
CONTRA_ONCE: set[str] = set()   # ... only the first time (a flaky effect that a repeat fixes)
SECRET = "VD-READBACK-7731"     # a read-back value: it must never reach the stored progress or a rebuilt account
DEAD_ID = owner.Identity("fake-dead68", 4_200_068, 1.0, owner._host())


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD if pid == DEAD_ID.pid else owner.UNKNOWN


@skill(name="test.vd_write", tier="L1", action="modify", description="write a named file (Phase 68 fixture)")
def write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"write:{name}")
    return SkillResult(speech="Wrote the file.")


@skill(name="test.vd_send", tier="L1", action="communicate", description="send a named file (Phase 68 fixture)")
def send(name: str = "a") -> SkillResult:
    LOG.append(f"send:{name}")
    return SkillResult(speech="Sent the file.")


@skill(name="test.vd_note", tier="L1", action="modify", description="a state change nothing can read back (Phase 68 fixture)")
def note(name: str = "a") -> SkillResult:
    LOG.append(f"note:{name}")
    return SkillResult(speech="Noted it.")


auditview.POLICY["test.vd_write"] = {"name": auditview.KEEP, "text": auditview.OMIT}
auditview.POLICY["test.vd_send"] = {"name": auditview.KEEP}
auditview.POLICY["test.vd_note"] = {"name": auditview.KEEP}


def _readback(args, data, before):  # noqa: ANN001
    name = args.get("name", "")
    bad = name in CONTRA or name in CONTRA_ONCE
    CONTRA_ONCE.discard(name)
    return [verify.Check("fixture effect", not bad, "the file is there", SECRET if bad else "the file is there")]


for _tool in ("test.vd_write", "test.vd_send"):          # test.vd_note has no verifier: nothing can be read back
    verify.register(_tool, verify.Verifier(_readback))


class Planner(ScriptedPlanner):
    """A scripted model: a decomposition request gets `stages`; a decision turn gets the next entry (the last one repeats)."""

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


# One long-lived event loop on its own thread, like the app's backend.
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="verdict-durability-loop").start()


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


def restart() -> None:
    """Forget this thread's open connection to the database, as a new process would find it: the next read re-opens the file (and
    anything that was never committed is gone)."""
    c = getattr(store._local, "conns", {}).pop(store._key(dbplace.resolve()), None)
    if c is not None:
        c.close()


def count(table: str) -> int:
    return int(store.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def stage_rows(gid: str) -> list[str]:
    return [s.status.value for s in (goals_mod.get(gid).subgoals or [])]


def tuples(report: dict) -> list[tuple]:
    return [(s["tool"], s["state"], bool(s.get("recovered"))) for s in report["steps"]]


def sts(report: steps.GoalReport) -> list[tuple[str, str]]:
    return [(s.tool, s.state.value) for s in report.steps]


def stored_progress(gid: str) -> str:
    return json.dumps(goals_mod.get(gid).progress, sort_keys=True)


def durable(gid: str) -> str:
    """The stored step account as a reader depends on it. A verdict that established nothing (unverified / partial) is not part of it:
    Phase 54's refresh has always dropped those on its first pass, and nothing reads them. A verified or a failed verdict is."""
    prog = goals_mod.get(gid).progress or {}
    kept = [s if s.get("state") == "verified" or (isinstance(s.get("ver"), dict) and s["ver"].get("status") == "failed")
            else {k: v for k, v in s.items() if k != "ver"} for s in prog.get("steps", [])]
    return json.dumps({**prog, "steps": kept}, sort_keys=True)


def done_clause(tail: str) -> str:
    m = re.search(r"Done: (.*?)(?= Failed:| Ran after| Still running:| Outcome unknown:| Waiting on you:| Not attempted:|$)", tail)
    return m.group(1) if m else ""


def audit_rows(gid: str) -> list[tuple]:
    rows = store.conn().execute("SELECT * FROM audit WHERE goal_id=? AND skill != 'plan.run' ORDER BY id", (gid,)).fetchall()
    return [(r["id"], r["skill"], audit.status_of(dict(r))) for r in rows]


def lifecycle(p: str, r: SkillResult, want: list[tuple], *, never_done: tuple[str, ...] = (), restarts: int = 3) -> list[steps.GoalReport]:
    """The goal `r` ran, as every later reader finds it: persisted, rebuilt after a restart, refreshed, rebuilt again -- `restarts` times.
    Each rebuilt account must be the LIVE one, with no 'limits of this account' note, and nothing may drift."""
    gid = r.data["goal_id"]
    live = r.data["report"]
    check(f"{p}.live the live account is {want}", tuples(live) == want, str(live))
    goal = goals_mod.get(gid)
    base = (stage_rows(gid), goal.status.value, durable(gid))
    audit_n, ran = count("audit"), list(LOG)
    outs: list[steps.GoalReport] = []
    for n in range(1, restarts + 1):
        restart()
        rebuilt = steps.reconstruct(gid)
        outs.append(rebuilt)
        check(f"{p}.{n}a restart #{n}: rebuilt from the database alone, the account IS the live one (outcome, steps, recovery flags, what is left, "
              "next action) and carries no 'limits of this account' note", rebuilt.to_dict() == live, f"{rebuilt.to_dict()} VS {live}")
        steps.refresh_goals()
        check(f"{p}.{n}b ...a refresh then leaves the goal exactly as the live run saved it (stages, status, stored step account)",
              (stage_rows(gid), goals_mod.get(gid).status.value, durable(gid)) == base,
              f"{(stage_rows(gid), goals_mod.get(gid).status.value, durable(gid))} VS {base}")
        speech, _data = steps.describe_goal()
        for tool in never_done:
            check(f"{p}.{n}c ...the status answer never lists '{tool}' as done", tool not in done_clause(speech), speech)
    again = steps.reconstruct(gid)
    check(f"{p}.z after every refresh the account is still the live one", again.to_dict() == live, f"{again.to_dict()} VS {live}")
    check(f"{p}.zz none of it ran, retried or recorded anything: no tool call, no new audit row",
          LOG == ran and count("audit") == audit_n, f"{LOG} {ran} {count('audit')} {audit_n}")
    outs.append(again)
    return outs


# crafted post-restart states ------------------------------------------------------------------------------------------------
def add_rows(gid: str, ident: owner.Identity, specs: list[tuple[str, str]]) -> list[int]:
    """Audit rows of `gid` written by session `ident`: outcome ok | fail | open | denied."""
    ids: list[int] = []
    for tool, outcome in specs:
        with owner._use_identity(ident), audit.goal_scope(gid):
            rid = audit.record(actor="text", skill=tool, tier="L1", args={}, decision="denied" if outcome == "denied" else "auto")
        if outcome == "ok":
            audit.complete(rid, ok=True, result="Wrote the file.", duration_ms=1)
        elif outcome == "fail":
            audit.complete(rid, ok=False, result="Could not.", error="boom", duration_ms=1)
        ids.append(rid)
    return ids


def new_goal(status: str = "partial") -> str:
    g = goals_mod.create("p68 goal", "p68 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus(status))
    return g.id


def crafted(state: str, outcome: str, *, ver=None, stopped: str = "failure", ids_override: list[int] | None = None) -> tuple[str, int]:  # noqa: ANN001
    gid = new_goal("partial")
    rid = add_rows(gid, DEAD_ID, [("test.vd_write", outcome)])[0] if ids_override is None else -1
    step = {"n": 1, "tool": "test.vd_write", "state": state, "audit": ids_override if ids_override is not None else [rid]}
    if ver is not None:
        step["ver"] = ver
    goals_mod.set_progress(gid, {"v": 1, "stopped": stopped, "steps": [step]})
    return gid, rid


VER_FAILED = {"status": "failed", "checks": [["fixture effect", False]]}
VER_OK = {"status": "verified", "checks": [["fixture effect", True]]}


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
            section_f()
    finally:
        (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s,
         CFG.planner.subgoal_evidence_advance) = saved
        CFG.permissions.overrides = saved_tools
        CONTRA.clear()
        CONTRA_ONCE.clear()
    return finish("Phase 68 -- a failed read-back verdict is durable; a success the row only repeats never overrules it",
                  time.perf_counter() - t0, min_assertions=100, min_scenarios=6)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. the reported defect: the tool succeeds, the read-back fails -- failed live, persisted, refreshed, restarted (x3)")
    CONTRA.add("a")
    r = run_plan([call("test.vd_write", {"name": "a", "text": "x"}), done("Wrote it.")], goal="Write file a")
    gid = r.data["goal_id"]
    rows = audit_rows(gid)
    check("A1 premise: the audit row records only the TOOL's verdict -- succeeded -- so nothing in the log itself contradicts it",
          [(skill_, status) for _id, skill_, status in rows] == [("test.vd_write", "succeeded")], str(rows))
    check("A2 live: the step is failed (the read-back overrode the tool's claim), the run is failed and not ok, the goal is failed",
          r.data["steps"][0]["error"] == "postcondition_failed" and tuples(r.data["report"]) == [("test.vd_write", "failed", False)]
          and r.data["report"]["outcome"] == "failed" and r.ok is False
          and goals_mod.get(gid).status is goals_mod.GoalStatus.FAILED, f"{r.data['steps']} {r.data['report']}")
    step = goals_mod.get(gid).progress["steps"][0]
    check("A3 persisted: state failed, the audit row id, and a verdict of check NAMES and booleans only -- no read-back value",
          step["state"] == "failed" and step["audit"] == [rows[0][0]] and step["ver"] == VER_FAILED and SECRET not in stored_progress(gid),
          str(step))
    outs = lifecycle("A4", r, [("test.vd_write", "failed", False)], never_done=("test.vd_write",))
    check("A5 every rebuilt account says failed -- never unknown, never completed -- and says why in words with no read-back value",
          all(o.outcome == "failed" and sts(o) == [("test.vd_write", "failed")] and "did not take effect" in o.steps[0].note
              and SECRET not in o.steps[0].note and SECRET not in o.tail() for o in outs), str([o.to_dict() for o in outs]))
    check("A6 ...and the plain-language account has a Failed clause and no Done clause at all",
          all("Failed: 'test.vd_write'" in o.tail() and "Done:" not in o.tail() for o in outs), outs[-1].tail())
    speech, data = steps.describe_goal()
    check("A7 the status answer: 'did not work', the step named as failed with the reason, not a word of success or of the read-back value",
          "It did not work." in speech and "Failed: 'test.vd_write' -- it did not take effect" in speech and "Done" not in speech
          and "finished" not in speech and SECRET not in speech and SECRET not in json.dumps(data, default=str), speech)
    check("A8 meta.goal_status data agrees: outcome failed, the step failed", data["report"]["outcome"] == "failed"
          and [s["state"] for s in data["report"]["steps"]] == ["failed"], str(data["report"]))
    check("A9 the stored account still has the verdict after all of it (no read-back value, the same names and booleans)",
          goals_mod.get(gid).progress["steps"][0].get("ver") == VER_FAILED and SECRET not in stored_progress(gid))
    CONTRA.clear()


def section_b() -> None:
    scenario("B. controls: a verified step, an unconfirmable step and a mixed goal rebuild exactly as they ran")
    r = run_plan([call("test.vd_send", {"name": "b"}), done("Sent it.")], goal="Send file b")
    outs = lifecycle("B1", r, [("test.vd_send", "verified", False)])
    check("B1x a genuinely successful and verified step stays verified, with its verdict, and the goal complete",
          all(sts(o) == [("test.vd_send", "verified")] and o.outcome == "complete" and o.steps[0].ver == VER_OK for o in outs)
          and goals_mod.get(r.data["goal_id"]).status is goals_mod.GoalStatus.SUCCEEDED, str(outs[-1].to_dict()))

    r = run_plan([call("test.vd_note", {"name": "n"}), done("Noted it.")], goal="Note n")
    outs = lifecycle("B2", r, [("test.vd_note", "completed", False)])
    check("B2x a step nothing can read back is completed (not verified, not failed) after every restart, and the goal complete_unverified",
          all(sts(o) == [("test.vd_note", "completed")] and o.outcome == "complete_unverified" for o in outs), str(outs[-1].to_dict()))

    CONTRA.add("m")
    r = run_plan([call("test.vd_send", {"name": "s"}), call("test.vd_write", {"name": "m", "text": "x"}), call("test.vd_note", {"name": "n"}),
                  done("All done.")], goal="Send s, write m, note n")
    outs = lifecycle("B3", r, [("test.vd_send", "verified", False), ("test.vd_write", "failed", False), ("test.vd_note", "completed", False)],
                     never_done=("test.vd_write",))
    check("B3x a mixed goal keeps each step's own state: the verified one verified, the contradicted one failed, the unconfirmable one completed "
          "but not vouched for (it ran after the failure)",
          all(sts(o) == [("test.vd_send", "verified"), ("test.vd_write", "failed"), ("test.vd_note", "completed")] and o.outcome == "partial"
              and o.steps[2].after_failure for o in outs), str(outs[-1].to_dict()))
    check("B3y ...and the answer is not a success: partial, the failure named, the contradicted tool never under Done",
          "Failed: 'test.vd_write'" in outs[-1].tail() and "test.vd_write" not in done_clause(outs[-1].tail())
          and goals_mod.get(r.data["goal_id"]).status is not goals_mod.GoalStatus.SUCCEEDED, outs[-1].tail())
    CONTRA.clear()

    CONTRA_ONCE.add("r")
    r = run_plan([call("test.vd_write", {"name": "r", "text": "x"}), call("test.vd_write", {"name": "r", "text": "again"}), done("Wrote r.")],
                 goal="Write file r")
    outs = lifecycle("B4", r, [("test.vd_write", "failed", True), ("test.vd_write", "verified", False)])
    check("B4x a contradicted step that a later verified write made good stays failed AND recovered (the flag is not lost) after every restart, "
          "and the outcome is the live one -- never 'unknown'",
          all(sts(o) == [("test.vd_write", "failed"), ("test.vd_write", "verified")] and o.steps[0].recovered
              and o.outcome == r.data["report"]["outcome"] != "unknown" for o in outs), str(outs[-1].to_dict()))
    speech, _ = steps.describe_goal()
    check("B4y ...and the status answer neither calls the recovered step failed nor says it can't tell what happened",
          "Failed:" not in speech and "can't tell" not in speech, speech)


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    scenario("C. a late real result does not erase the verdict; without a verdict it is recorded as before")
    for label, ver in (("with a failed read-back verdict", VER_FAILED), ("without any verdict (a step that timed out)", None)):
        gid = new_goal("partial")
        rid = add_rows(gid, DEAD_ID, [("test.vd_write", "open")])[0]
        goals_mod.set_progress(gid, {"v": 1, "stopped": "failure", "steps": [
            {"n": 1, "tool": "test.vd_write", "state": "failed" if ver else "unknown", "audit": [rid], **({"ver": ver} if ver else {})}]})
        p = "C1" if ver else "C2"
        check(f"{p}a {label}: the owner is dead, the row has no outcome -> marked interrupted; the step is unknown (the row decides, nothing is invented)",
              audit.reconcile_orphans(probe_dead) == 1 and sts(steps.reconstruct(gid, probe_dead)) == [("test.vd_write", "unknown")])
        steps.refresh_goals(probe_dead)
        kept = goals_mod.get(gid).progress["steps"][0]
        check(f"{p}b ...a refresh stores unknown" + (" and KEEPS the verdict" if ver else " (no verdict to keep)"),
              kept["state"] == "unknown" and kept.get("ver") == ver, str(kept))
        audit.complete(rid, ok=True, result="Wrote the file.", duration_ms=1)       # the late real result
        late = [steps.reconstruct(gid, probe_dead)]
        steps.refresh_goals(probe_dead)
        late.append(steps.reconstruct(gid, probe_dead))
        steps.refresh_goals(probe_dead)
        restart()
        late.append(steps.reconstruct(gid, probe_dead))
        want = [("test.vd_write", "failed")] if ver else [("test.vd_write", "completed")]
        check(f"{p}c the late success lands: the step is " + ("still FAILED (the read-back contradicted it; nothing re-verified it)" if ver
              else "completed (the Phase 54 late-result handling is unchanged)") + ", through a refresh and a restart",
              all(sts(x) == want and not x.notes for x in late), str([x.to_dict() for x in late]))
        check(f"{p}d ...and the stored account agrees", goals_mod.get(gid).progress["steps"][0]["state"] == want[0][1])
        if ver:
            check(f"{p}e ...the account is a failure, not a completion", late[-1].outcome == "failed" and "Done:" not in late[-1].tail()
                  and "Failed: 'test.vd_write' -- it did not take effect" in late[-1].tail(), late[-1].tail())

    gid = new_goal("partial")
    rid = add_rows(gid, DEAD_ID, [("test.vd_write", "open")])[0]
    goals_mod.set_progress(gid, {"v": 1, "stopped": "failure", "steps": [
        {"n": 1, "tool": "test.vd_write", "state": "failed", "audit": [rid], "ver": VER_FAILED}]})
    audit.reconcile_orphans(probe_dead)
    steps.refresh_goals(probe_dead)
    audit.complete(rid, ok=False, result="Could not.", error="boom", duration_ms=1)
    rep = steps.reconstruct(gid, probe_dead)
    check("C3 a late FAILURE is a failure too (the row's own word), and says what the row says, not the read-back's",
          sts(rep) == [("test.vd_write", "failed")] and "did not take effect" not in rep.steps[0].note, str(rep.to_dict()))

    gid, rid = crafted("failed", "ok", ver=VER_FAILED)
    before = steps.reconstruct(gid, probe_dead)
    goals_mod.set_progress(gid, {"v": 1, "stopped": "completed", "steps": [
        {"n": 1, "tool": "test.vd_write", "state": "verified", "audit": [rid], "ver": VER_OK}]})
    after = steps.reconstruct(gid, probe_dead)
    check("C4 NEW verification evidence changes it: the same step with a verified verdict stored is verified (the failed verdict does not stick)",
          sts(before) == [("test.vd_write", "failed")] and sts(after) == [("test.vd_write", "verified")] and after.outcome == "complete",
          f"{sts(before)} {sts(after)}")


# ---------------------------------------------------------------------------------------------------------------------------
def section_d() -> None:
    scenario("D. the merge matrix: only a stored contradiction explains a success the row repeats; missing or uninformative evidence stays unknown")
    cases = [
        # (label, stored state, row outcome, stored verdict, expected state, expect a 'disagree' note)
        ("D1 failed + verdict (failed, a false check) + row succeeded => FAILED  [the rule]", "failed", "ok", VER_FAILED, "failed", False),
        ("D2 unknown + verdict (failed, a false check) + row succeeded => FAILED", "unknown", "ok", VER_FAILED, "failed", False),
        ("D3 failed + NO verdict + row succeeded => unknown  [Phase 54's rule, kept]", "failed", "ok", None, "unknown", True),
        ("D4 failed + verdict with no checks => unknown", "failed", "ok", {"status": "failed", "checks": []}, "unknown", True),
        ("D5 failed + verdict whose only check is unreadable (null) => unknown", "failed", "ok",
         {"status": "failed", "checks": [["fixture effect", None]]}, "unknown", True),
        ("D6 failed + verdict whose status says failed but whose checks all passed => unknown", "failed", "ok",
         {"status": "failed", "checks": [["fixture effect", True]]}, "unknown", True),
        ("D7 failed + a partial verdict => unknown", "failed", "ok",
         {"status": "partial", "checks": [["a", True], ["b", None]]}, "unknown", True),
        ("D8 failed + a verified verdict => unknown (contradictory evidence is not resolved either way)", "failed", "ok", VER_OK, "unknown", True),
        ("D9 failed + a malformed verdict (no checks list) => unknown", "failed", "ok", {"status": "failed"}, "unknown", True),
        ("D10 failed + a verdict that is not even a dict => unknown", "failed", "ok", "boom", "unknown", True),
        ("D11 failed + verdict + row with no outcome yet => unknown (the row decides; a verdict alone does not make a failure)", "failed", "open",
         VER_FAILED, "unknown", False),
        ("D12 unknown + verdict + row with no outcome yet => unknown", "unknown", "open", VER_FAILED, "unknown", False),
        ("D13 failed + verdict + row failed => failed (the row's own word)", "failed", "fail", VER_FAILED, "failed", False),
        ("D14 failed + verdict + row denied => unknown (the step never ran: the stored outcome disagrees)", "failed", "denied", VER_FAILED, "unknown", True),
        ("D15 needs_input + verdict + row succeeded => unknown (the rule is for a failed or unknown label only)", "needs_input", "ok", VER_FAILED,
         "unknown", True),
        ("D16 completed + no verdict + row succeeded => completed  [control]", "completed", "ok", None, "completed", False),
        ("D17 verified + verified verdict + row succeeded => verified  [control]", "verified", "ok", VER_OK, "verified", False),
        ("D18 unknown + no verdict + row succeeded => completed  [control: a late result is recorded]", "unknown", "ok", None, "completed", False),
        ("D19 failed + NO verdict + row failed => failed  [control]", "failed", "fail", None, "failed", False),
    ]
    for label, state, outcome, ver, want, disagree in cases:
        gid, _rid = crafted(state, outcome, ver=ver)
        rep = steps.reconstruct(gid, probe_dead)
        check(label + (" (and says the record disagrees)" if disagree else ""),
              sts(rep) == [("test.vd_write", want)] and any("disagree" in n for n in rep.notes) == disagree,
              f"{sts(rep)} {rep.notes}")
    gid, _ = crafted("failed", "ok", ver=VER_FAILED, ids_override=[])
    check("D20 a failure that never reached the Executor (no audit id) stays failed, as before",
          sts(steps.reconstruct(gid, probe_dead)) == [("test.vd_write", "failed")])
    gid, _ = crafted("failed", "ok", ver=VER_FAILED, ids_override=[987654])
    check("D21 a failure whose audit row is gone stays failed (a stored failure needs no confirmation to stay a failure)",
          sts(steps.reconstruct(gid, probe_dead)) == [("test.vd_write", "failed")])
    gid, _ = crafted("completed", "ok", ver=VER_FAILED)
    check("D22 the rule does not reach a stored success: completed + a failed verdict + a succeeded row is untouched here (Phase 54 behaviour)",
          sts(steps.reconstruct(gid, probe_dead)) == [("test.vd_write", "completed")])

    contradicted = getattr(steps, "_contradicted", lambda v: None)
    check("D23 _contradicted: only status failed WITH an explicit false check counts", [contradicted(v) for v in (
        VER_FAILED, {"status": "failed", "checks": [["a", True], ["b", False]]}, None, {}, "x", {"status": "failed"},
        {"status": "failed", "checks": []}, {"status": "failed", "checks": [["a", None]]}, {"status": "failed", "checks": [["a", True]]},
        {"status": "partial", "checks": [["a", False]]}, {"status": "verified", "checks": [["a", False]]}, {"status": "failed", "checks": "x"},
        {"status": "failed", "checks": [["a"]]}, {"status": "failed", "checks": [None]},
    )] == [True, True] + [False] * 12)

    # a goal that ENDED as completed, with a contradicted step a later verified step made good: it is complete, not "unknown" after a restart
    gid = new_goal("succeeded")
    ids = add_rows(gid, DEAD_ID, [("test.vd_write", "ok"), ("test.vd_write", "ok")])
    goals_mod.set_progress(gid, {"v": 1, "stopped": "completed", "steps": [
        {"n": 1, "tool": "test.vd_write", "state": "failed", "audit": [ids[0]], "ver": VER_FAILED, "recovered": True},
        {"n": 2, "tool": "test.vd_write", "state": "verified", "audit": [ids[1]], "ver": VER_OK}]})
    rep = steps.reconstruct(gid, probe_dead)
    check("D25 a goal that ended completed, whose contradicted step was made good by a verified one, rebuilds as failed+recovered then verified: "
          "complete_unverified, not 'unknown' (a finished goal must not look unfinished after a restart)",
          sts(rep) == [("test.vd_write", "failed"), ("test.vd_write", "verified")] and rep.steps[0].recovered
          and rep.outcome == "complete_unverified" and not rep.notes, str(rep.to_dict()))
    gid = new_goal("succeeded")
    rid = add_rows(gid, DEAD_ID, [("test.vd_write", "ok")])[0]
    goals_mod.set_progress(gid, {"v": 1, "stopped": "completed", "steps": [
        {"n": 1, "tool": "test.vd_write", "state": "failed", "audit": [rid], "ver": VER_FAILED}]})
    rep = steps.reconstruct(gid, probe_dead)
    check("D26 ...and a goal recorded as completed whose only step the read-back contradicted is a FAILURE, never complete (the stop reason "
          "alone cannot make a failed step a success)",
          sts(rep) == [("test.vd_write", "failed")] and rep.outcome == "failed" and "Done:" not in rep.tail(), str(rep.to_dict()))

    # idempotence of the durable view: refreshing never changes the verdict, however often
    gid, _ = crafted("failed", "ok", ver=VER_FAILED)
    steps.refresh_goals(probe_dead)
    first = stored_progress(gid)
    for _ in range(3):
        steps.refresh_goals(probe_dead)
    check("D24 a failed step with its verdict is a fixed point of refresh: the stored account does not change, however often",
          stored_progress(gid) == first and goals_mod.get(gid).progress["steps"][0]["state"] == "failed"
          and goals_mod.get(gid).progress["steps"][0].get("ver") == VER_FAILED, first)


# ---------------------------------------------------------------------------------------------------------------------------
def section_e() -> None:
    scenario("E. Phase 67 stays consistent: stage statuses and the goal's status agree with the failed step, before and after every restart")
    CFG.planner.subgoal_evidence_advance = True
    CONTRA.add("a")
    r = run_plan([call("test.vd_write", {"name": "a", "text": "x"}, 0), call("test.vd_send", {"name": "s"}, 1), done("Wrote and sent it.")],
                 stages=STAGES)
    gid = r.data["goal_id"]
    live_stages = stage_rows(gid)
    check("E1 a contradicted step leaves its stage unresolved: the stage is not succeeded and the goal is not succeeded",
          live_stages[0] != "succeeded" and goals_mod.get(gid).status is not goals_mod.GoalStatus.SUCCEEDED and r.ok is False
          and r.data["report"]["outcome"] in ("partial", "failed"), f"{live_stages} {r.data['report']}")
    outs = lifecycle("E2", r, tuples(r.data["report"]), never_done=("test.vd_write",))
    check("E3 the saved stage statuses are the live ones after all of it, and no stage claims a success the steps do not",
          stage_rows(gid) == live_stages and (live_stages != ["succeeded"] * len(live_stages)
                                              or outs[-1].outcome in ("complete", "complete_unverified")), f"{stage_rows(gid)} {live_stages}")
    check("E4 the failed step is failed in every rebuilt account and the outcome is never better than the live one",
          all(sts(o)[0] == ("test.vd_write", "failed") and o.outcome == r.data["report"]["outcome"] for o in outs), str(outs[-1].to_dict()))
    CONTRA.clear()

    # a contradicted step that a verified repeat made good: the stage IS resolved, and that survives
    CONTRA_ONCE.add("g")
    r = run_plan([call("test.vd_write", {"name": "g", "text": "x"}, 0), call("test.vd_write", {"name": "g", "text": "again"}, 0),
                  call("test.vd_send", {"name": "g"}, 1), done("Wrote and sent it.")], stages=STAGES)
    gid = r.data["goal_id"]
    live_stages = stage_rows(gid)
    check("E5 a contradicted write made good by a later verified write resolves ITS stage from its own steps (Phase 67): stage 0 is succeeded "
          "although its first step failed",
          live_stages[0] == "succeeded" and r.data["report"]["steps"][0].get("recovered") is True
          and r.data["report"]["steps"][0]["state"] == "failed", f"{live_stages} {r.data['report']}")
    outs = lifecycle("E6", r, tuples(r.data["report"]))
    check("E7 ...and after every restart the stage is still succeeded, and the account keeps the recovery (failed + recovered, then verified), "
          "with the live outcome -- never unknown",
          all(sts(o)[0] == ("test.vd_write", "failed") and o.steps[0].recovered and o.outcome == r.data["report"]["outcome"] != "unknown"
              for o in outs) and stage_rows(gid) == live_stages, str(outs[-1].to_dict()))


# ---------------------------------------------------------------------------------------------------------------------------
def section_f() -> None:
    scenario("F. what is stored stays references and verdicts only, and reading it never changes it")
    CONTRA.add("f")
    r = run_plan([call("test.vd_write", {"name": "f", "text": "private words"}), done("Wrote it.")], goal="Write file f")
    gid = r.data["goal_id"]
    blob = json.dumps(goals_mod.get(gid).progress, sort_keys=True)
    check("F1 the stored progress holds the verdict as check names and booleans: no read-back value, no argument, no tool speech",
          SECRET not in blob and "private words" not in blob and "Wrote the file" not in blob
          and set(goals_mod.get(gid).progress["steps"][0]) == {"n", "tool", "state", "audit", "ver"}, blob)
    for _ in range(2):
        restart()
        steps.refresh_goals()
    after = json.dumps(goals_mod.get(gid).progress, sort_keys=True)
    rebuilt = steps.reconstruct(gid)
    speech, data = steps.describe_goal()
    check("F2 after restarts and refreshes the stored progress is byte-identical, and nothing a reader returns carries a read-back value or an argument",
          after == blob and all(x not in (rebuilt.tail() + speech + json.dumps(data, default=str) + json.dumps(rebuilt.to_dict()))
                                for x in (SECRET, "private words")), speech)
    before = (count("audit"), count("goals"), list(LOG))
    steps.reconstruct(gid)
    steps.describe_goal()
    steps.refresh_goals()
    steps.settle_interrupted_goals()
    check("F3 reading, refreshing and settling run nothing and add no audit row, goal or tool call",
          (count("audit"), count("goals"), list(LOG)) == before, str(before))
    CONTRA.clear()


if __name__ == "__main__":
    sys.exit(main())
