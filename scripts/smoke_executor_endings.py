"""Phase 100 -- the ends of a call stay true: a cancelled step stays unknown, an audit write that fails does not change what the skill did,
a slow event client cannot break a publish, and a planner that raises keeps the steps that ran.

Found by the Phase 99 audit (read from the code, then re-read for this phase):

  1. `steps._row_state` read an audit row that ended `cancelled` (an async skill stopped or timed out in flight; a partial effect is not
     ruled out) as FAILED. Live the step is UNKNOWN, so after a restart the same step became "it did not work, I can try again" -- for a
     send, an invitation to do it twice. A whole-plan timeout lost such a step from its own report for the same reason.
  2. `Executor.run` wrote its audit rows unguarded after the skill had run: a failing `audit.complete` (database locked, disk full) replaced a
     skill's result with an error, a skill's own exception with the audit's, and a cancel with an error; the row stayed open for good.
  3. `EventBus.publish` raised `asyncio.QueueFull` into the publisher when one websocket client stopped reading -- e.g. out of `skill.done`,
     after the skill had run, so the call was reported as failed.
  4. `plan.run` caught only a timeout and a cancel around the planner: any other exception (a dropped model connection) came out as
     "That failed", the steps that ran unreported, the goal row left `running`, the stop flag armed.

  A  steps: non-shell cancelled -> UNKNOWN (live = restart), shell semantics and definitive failures unchanged, whole-plan timeout keeps the step
  B  Executor.run: a failing audit write on the success / exception / cancel paths changes nothing the caller sees; nothing is invented
  C  the bus: a full client queue drops that client only; healthy clients and the publisher are unaffected; other exceptions still propagate
  D  plan.run: a planner exception keeps the steps (completed and unknown), finishes the goal, returns ok=False; cancel and BaseException pass

Synthetic canary strings only; `test.*` fixture skills; throwaway databases (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

from friday import audit, owner, remote_events, steps, store  # noqa: E402
from friday.bus import BUS, Event  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.orchestrator import Orchestrator  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
GATE = threading.Event()
PW = "CANARYpw100alphaZ"


# ------------------------------------------------------------------------------------------------ fixtures
@skill(name="test.e_ok", tier="L1", action="modify", description="a write that succeeds (Phase 100 fixture)")
def e_ok() -> SkillResult:
    LOG.append("ok")
    return SkillResult(speech="Wrote the file.")


@skill(name="test.e_undoable", tier="L1", action="modify", description="a write with an undo (Phase 100 fixture)",
       undo=lambda: {"skill": "test.e_ok", "args": {}})
def e_undoable() -> SkillResult:
    return SkillResult(speech="Changed it.")


@skill(name="test.e_raises", tier="L1", action="modify", description="a write that raises (Phase 100 fixture)")
def e_raises() -> SkillResult:
    raise ValueError("the skill failed on its own")


@skill(name="test.e_async_slow", tier="L1", action="modify", description="a slow coroutine write (Phase 100 fixture)")
async def e_async_slow() -> SkillResult:
    LOG.append("async:start")
    await asyncio.sleep(30)
    return SkillResult(speech="never")


@skill(name="test.e_thread_slow", tier="L1", action="modify", description="a slow write in a worker thread (Phase 100 fixture)")
def e_thread_slow() -> SkillResult:
    LOG.append("thread:start")
    GATE.wait(20)
    return SkillResult(speech="Wrote the slow file.")


@skill(name="test.e_read_slow", tier="L0", description="a slow coroutine read (Phase 100 fixture)")
async def e_read_slow() -> SkillResult:
    LOG.append("read:start")
    await asyncio.sleep(30)
    return SkillResult(speech="never")


@skill(name="test.e_unknown", tier="L1", action="modify", description="a write whose outcome is unknown (Phase 100 fixture)")
def e_unknown() -> SkillResult:
    return SkillResult(speech="Stopped, outcome unknown.", ok=False, data={"status": audit.UNKNOWN_OUTCOME})


# a skill's own `status` is an outcome only for a skill that defines these words (shell.run); the fixture is given the same standing
audit._STATUS_OUTCOMES["test.e_unknown"] = frozenset({audit.UNKNOWN_OUTCOME})


class Planner(ScriptedPlanner):
    """A scripted model; a decomposition request gets no subgoals; from decision turn `fail_at` on it raises."""

    def __init__(self, decisions: list[str], fail_at: int | None = None) -> None:
        super().__init__(decisions)
        self.fail_at = fail_at
        self.n = 0

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        system = next((m.content for m in request.messages if m.role == "system"), "")
        if "Break a real-world goal down" in system:
            return LlmResponse(text=json.dumps({"subgoals": []}), model="scripted", provider=self.name)
        if self.fail_at is not None and self.n >= self.fail_at:
            raise RuntimeError(f"connection reset, password={PW}")
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return LlmResponse(text=text, model="scripted", provider=self.name)


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.messages)


# One long-lived event loop on its own thread, like the app's backend (an abandoned worker thread must not hold `asyncio.run` at exit).
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="executor-endings-loop").start()


def on_loop(coro, timeout: float = 60.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


def run_plan(goal: str, decisions: list[str], fail_at: int | None = None):
    LOG.clear()
    with scripted_provider(Planner(decisions, fail_at)):
        return on_loop(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"))


def last_row(skill_name: str) -> dict:
    r = store.conn().execute("SELECT * FROM audit WHERE skill=? ORDER BY id DESC LIMIT 1", (skill_name,)).fetchone()
    return dict(r) | {"status": audit.status_of(dict(r))}


def goal_rows(goal_id: str) -> list[dict]:
    return [dict(r) | {"status": audit.status_of(dict(r))} for r in
            store.conn().execute("SELECT * FROM audit WHERE goal_id=? AND skill != 'plan.run' ORDER BY id", (goal_id,)).fetchall()]


def set_row(row_id: int, **cols) -> None:
    c = store.conn()
    c.execute(f"UPDATE audit SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), row_id))
    c.commit()


def states(report) -> list[tuple[str, str]]:
    return [(s.tool, s.state.value) for s in report.steps]


def pstates(r: SkillResult) -> list[tuple[str, str]]:
    return [(s["tool"], s["state"]) for s in r.data["report"]["steps"]]


# Injected audit failures: `FLAKY["complete"]` / `FLAKY["unknown"]` make the next write raise (with a canary in the message).
FLAKY = {"complete": False, "unknown": False, "hits": 0}
_real_complete = audit.complete
_real_unknown = audit.mark_outcome_unknown


def flaky_complete(audit_id, **kw):
    if FLAKY["complete"]:
        FLAKY["hits"] += 1
        raise sqlite3.OperationalError(f"database is locked (password={PW})")
    return _real_complete(audit_id, **kw)


def flaky_unknown(audit_id, **kw):
    if FLAKY["unknown"]:
        FLAKY["hits"] += 1
        raise sqlite3.OperationalError(f"disk I/O error (token={PW})")
    return _real_unknown(audit_id, **kw)


GOAL_ASYNC = "Write the async file"
GOAL_TWO = "Create folder a and write a file in it"


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s)
    CFG.desktop_observer.enabled = False
    audit.complete, audit.mark_outcome_unknown = flaky_complete, flaky_unknown
    cap = Capture()
    root = logging.getLogger()
    root.handlers.insert(0, cap)
    try:
        with iso_env.isolated_runtime():
            section_a()
            section_b(cap)
            section_c()
            section_d(cap)
    finally:
        audit._STATUS_OUTCOMES.pop("test.e_unknown", None)
        root.handlers.remove(cap)
        audit.complete, audit.mark_outcome_unknown = _real_complete, _real_unknown
        CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s = saved
        CFG.permissions.overrides = saved_tools
        GATE.set()
    return finish("Phase 100 -- executor and planner endings stay true", time.perf_counter() - t0, min_assertions=70, min_scenarios=4)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. a cancelled non-shell step is UNKNOWN after a restart, as it is live")
    CFG.planner.step_timeout_s = 1.0
    CFG.planner.total_timeout_s = 120.0
    r = run_plan(GOAL_ASYNC, [call("test.e_async_slow"), done("ok")])
    gid = r.data["goal_id"]
    row = next(x for x in goal_rows(gid) if x["skill"] == "test.e_async_slow")
    check("A1 the coroutine step was cancelled at its timeout: its row says cancelled, ok 0 (audit semantics are unchanged)",
          row["outcome"] == audit.CANCELLED and row["ok"] == 0 and audit.status_of(row) == "failed", str(row))
    check("A2 live, the run already calls it unknown", pstates(r) == [("test.e_async_slow", "unknown")], str(pstates(r)))
    rebuilt = steps.reconstruct(gid)
    check("A3 rebuilt from the stored progress and the audit row (a restart), it is STILL unknown -- not failed",
          states(rebuilt) == [("test.e_async_slow", "unknown")] and rebuilt.outcome == "unknown", str(states(rebuilt)) + rebuilt.outcome)
    check("A4 the account tells the user to check before asking again, and offers no retry",
          "Check whether 'test.e_async_slow' took effect" in rebuilt.tail() and "try the failed step again" not in rebuilt.tail(), rebuilt.tail())
    check("A5 the audit-log-only view agrees", [(s.tool, s.state.value) for s in steps.ledger_for_goal(gid)] == [("test.e_async_slow", "unknown")])
    check("A6 the row itself was not rewritten by reading it back", last_row("test.e_async_slow")["outcome"] == audit.CANCELLED)

    scenario("A'. only a row that definitively says failed makes a stored unknown a failure")
    rid = row["id"]
    set_row(rid, outcome=None)
    check("A7 an older row (ok 0, no outcome at all) is read as it always was -- its own word decides: failed (its cancelled and failed endings "
          "cannot be told apart; a documented limit)", states(steps.reconstruct(gid)) == [("test.e_async_slow", "failed")],
          str(states(steps.reconstruct(gid))))
    check("A8 ...and the rebuilt account carries no 'does not say definitively' note", "definitively" not in " ".join(steps.reconstruct(gid).notes))
    set_row(rid, outcome=audit.FAILURE)
    check("A9 a row with a recorded failure outcome makes it failed", states(steps.reconstruct(gid)) == [("test.e_async_slow", "failed")])
    set_row(rid, outcome=audit.NOT_STARTED)
    check("A10 not_started is definitive too: failed", states(steps.reconstruct(gid)) == [("test.e_async_slow", "failed")])
    set_row(rid, skill="shell.run", outcome=audit.CANCELLED)
    check("A11 a SHELL cancel (process tree confirmed gone) keeps its meaning: failed, in the rebuilt account and the audit-only view",
          states(steps.reconstruct(gid)) == [("test.e_async_slow", "failed")]
          and [(s.tool, s.state.value) for s in steps.ledger_for_goal(gid)] == [("shell.run", "failed")],
          str(states(steps.reconstruct(gid))) + str([(s.tool, s.state.value) for s in steps.ledger_for_goal(gid)]))
    set_row(rid, skill="shell.run", outcome=audit.TIMEOUT)
    check("A12 a shell timeout keeps its meaning: failed", states(steps.reconstruct(gid)) == [("test.e_async_slow", "failed")])
    set_row(rid, skill="test.e_async_slow", outcome=audit.CANCELLED)
    check("A13 restored: unknown again", states(steps.reconstruct(gid)) == [("test.e_async_slow", "unknown")])

    scenario("A''. the state functions themselves")
    probe = lambda *a: owner.UNKNOWN  # noqa: E731
    base = {"skill": "test.x", "status": "failed", "ok": 0}
    rs = lambda **kw: steps._row_state({**base, **kw}, probe)  # noqa: E731
    S = steps.StepState
    check("A14 non-shell cancelled -> UNKNOWN", rs(outcome="cancelled") is S.UNKNOWN)
    check("A15 shell cancelled -> FAILED", rs(skill="shell.run", outcome="cancelled") is S.FAILED)
    check("A16 non-shell failure / not_started / no outcome -> FAILED",
          rs(outcome="failure") is S.FAILED and rs(outcome="not_started") is S.FAILED and rs(outcome=None) is S.FAILED)
    check("A17 a blocked row is FAILED, a denied row NEEDS_INPUT (a cancelled outcome cannot reach them)",
          rs(status="blocked", outcome="cancelled") is S.FAILED and rs(status="denied") is S.NEEDS_INPUT)
    check("A18 a succeeded row is untouched", rs(status="succeeded", ok=1, outcome="success") is S.COMPLETED)
    check("A19 _merge: a stored unknown stays unknown against a non-definitive failed row",
          steps._merge(S.UNKNOWN, S.FAILED, None, definitive_failure=False)[0] is S.UNKNOWN)
    check("A20 _merge: ...becomes failed against a definitive one, and by default (existing callers)",
          steps._merge(S.UNKNOWN, S.FAILED, None, definitive_failure=True)[0] is S.FAILED
          and steps._merge(S.UNKNOWN, S.FAILED, None)[0] is S.FAILED)
    check("A21 _merge: a stored failed stays failed, a stored completed against a failed row is still a disagreement",
          steps._merge(S.FAILED, S.FAILED, None, definitive_failure=False)[0] is S.FAILED
          and steps._merge(S.COMPLETED, S.FAILED, None, definitive_failure=False)[0] is S.UNKNOWN)
    check("A22 _merge never moves toward success", steps._merge(S.UNKNOWN, S.UNKNOWN, None)[0] is S.UNKNOWN)
    fd = lambda **kw: steps._failure_is_definitive({**base, **kw})  # noqa: E731
    check("A22b what is a definitive failure: the skill's own failure, never started, blocked, an older row, a shell timeout / cancel",
          fd(outcome="failure") and fd(outcome="not_started") and fd(status="blocked") and fd(outcome=None)
          and fd(skill="shell.run", outcome="timeout") and fd(skill="shell.run", outcome="cancelled"))
    check("A22b' a blocked row is definitive whatever its outcome field says", fd(status="blocked", outcome="cancelled"))
    check("A22c ...and what is not: a cancel of a non-shell skill (or any outcome that is not a failure)",
          not fd(outcome="cancelled") and not fd(outcome="unknown_outcome") and not fd(outcome="success"))

    scenario("A'''. the WHOLE run is cut off while a coroutine step is in flight")
    CFG.planner.step_timeout_s = 90.0
    CFG.planner.total_timeout_s = 4.0
    r = run_plan("Create folder a and write the async file", [call("test.e_ok"), call("test.e_async_slow"), done("done")])
    CFG.planner.total_timeout_s = 120.0
    check("A23 the run stopped on the overall limit and is not ok", r.ok is False and r.data["stopped"] == "timeout")
    check("A24 the in-flight coroutine step (cancelled, no Observation) is in the report as unknown",
          ("test.e_async_slow", "unknown") in pstates(r) and r.data["report"]["outcome"] == "unknown", str(r.data["report"]))
    check("A25 the step that did finish is reported", any(t == "test.e_ok" for t, _ in pstates(r)), str(pstates(r)))
    check("A26 the speech says the outcome is unknown", "Outcome unknown: 'test.e_async_slow'" in r.speech, r.speech)
    check("A27 the coroutine step ran once and never finished", LOG.count("async:start") == 1)

    scenario("A''''. a timed-out READ: failed live, unknown after a restart (the row cannot tell a timeout from a stop; the cautious reading wins)")
    CFG.planner.step_timeout_s = 1.0
    r = run_plan("Read the async file", [call("test.e_read_slow"), done("ok")])
    CFG.planner.step_timeout_s = 90.0
    gid = r.data["goal_id"]
    rd = next(x for x in goal_rows(gid) if x["skill"] == "test.e_read_slow")
    check("A28 live, a read that ran out of time changed nothing: failed", pstates(r) == [("test.e_read_slow", "failed")], str(pstates(r)))
    check("A29 its row says cancelled (a stop and a timeout read alike)", rd["outcome"] == audit.CANCELLED, str(rd))
    check("A30 rebuilt, it reads unknown -- the same cautious reading as any non-shell cancel (a read cancelled by a STOP is unknown live too)",
          states(steps.reconstruct(gid)) == [("test.e_read_slow", "unknown")], str(states(steps.reconstruct(gid))))


# ---------------------------------------------------------------------------------------------------------------------------
def section_b(cap: Capture) -> None:
    scenario("B. Executor.run: an audit write that fails does not change what the skill did")
    seen: list[str] = []

    async def listen(event) -> None:
        seen.append(event.topic)

    BUS.subscribe("*", listen)
    try:
        # B-success
        cap.messages.clear()
        seen.clear()
        FLAKY.update(complete=True, hits=0)
        r = on_loop(EXECUTOR.run("test.e_undoable", {}, actor="text"))
        FLAKY["complete"] = False
        row = last_row("test.e_undoable")
        check("B1 the failing audit.complete was reached", FLAKY["hits"] >= 1)
        check("B2 the skill's result reaches the caller unchanged", r.ok is True and r.speech == "Changed it.", str(r))
        check("B3 the row is left open: no outcome, no ok, no result -- nothing was invented",
              row["ok"] is None and row["outcome"] is None and row["result"] is None and row["status"] == "unfinished", str(row))
        undo_rows = store.conn().execute("SELECT * FROM undo WHERE audit_id=?", (row["id"],)).fetchall()
        check("B4 the undo was still registered for the call that succeeded", len(undo_rows) == 1)
        check("B5 skill.done was still published", "skill.done" in seen and "skill.error" not in seen, str(seen))
        action = INTEL.state.action_log[-1] if INTEL.state.action_log else {}
        check("B6 the action was still recorded as a success", action.get("status") == "success", str(action))
        text = cap.text()
        check("B7 the failure is logged as a fixed diagnostic, and the exception text it carried is checked (no canary)",
              "could not record the result of test.e_undoable" in text and PW not in text and "OperationalError" in text, text[-300:])

        # B-exception
        cap.messages.clear()
        seen.clear()
        FLAKY.update(complete=True, hits=0)
        try:
            on_loop(EXECUTOR.run("test.e_raises", {}, actor="text"))
            got = None
        except BaseException as exc:  # noqa: BLE001
            got = exc
        FLAKY["complete"] = False
        row = last_row("test.e_raises")
        check("B8 the caller gets the SKILL's own exception, not the audit's",
              isinstance(got, ValueError) and str(got) == "the skill failed on its own", repr(got))
        check("B9 skill.error was still published; the row is left open, not invented",
              "skill.error" in seen and row["ok"] is None and row["outcome"] is None, f"{seen} {row}")
        check("B10 the audit failure is logged without its canary", "could not close the audit row of test.e_raises" in cap.text() and PW not in cap.text())

        # B-cancel (coroutine)
        async def cancelled_run(name: str, marker: str) -> str:
            before = LOG.count(marker)
            t = asyncio.ensure_future(EXECUTOR.run(name, {}, actor="text"))
            for _ in range(200):                       # cancel only once the skill is really running (not on a fixed delay)
                if LOG.count(marker) > before:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                return "cancelled" if t.cancelled() else "cancelled-but-task-ended-otherwise"
            except BaseException as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
            return "returned"

        cap.messages.clear()
        FLAKY.update(complete=True, hits=0)
        verdict = on_loop(cancelled_run("test.e_async_slow", "async:start"))
        FLAKY["complete"] = False
        row = last_row("test.e_async_slow")
        check("B11 a cancel stays a cancel when its audit write fails (the task ends cancelled, not as an error)", verdict == "cancelled", verdict)
        check("B12 the write was attempted and the row is left open (no outcome made up)",
              FLAKY["hits"] >= 1 and row["outcome"] is None and row["ok"] is None, f"{FLAKY} {row}")
        check("B13 ...and logged", "could not record how test.e_async_slow ended on cancel" in cap.text() and PW not in cap.text())

        # B-cancel (worker thread, the unknown marker)
        GATE.clear()
        cap.messages.clear()
        FLAKY.update(unknown=True, hits=0)
        verdict = on_loop(cancelled_run("test.e_thread_slow", "thread:start"))
        FLAKY["unknown"] = False
        row = last_row("test.e_thread_slow")
        check("B14 an abandoned worker-thread step: a failing unknown-marker write does not replace the cancel", verdict == "cancelled", verdict)
        check("B15 the marker write was attempted and logged without its canary",
              FLAKY["hits"] >= 1 and "could not record how test.e_thread_slow ended on cancel" in cap.text() and PW not in cap.text())
        GATE.set()
        for _ in range(100):                           # the abandoned thread finishes and records its real outcome
            if last_row("test.e_thread_slow")["outcome"] == audit.SUCCESS:
                break
            time.sleep(0.05)
        row = last_row("test.e_thread_slow")
        check("B16 when the thread ends its real outcome is still recorded over the open row (the late path is unaffected)",
              row["outcome"] == audit.SUCCESS and row["ok"] == 1, str(row))

        # unchanged when nothing fails
        seen.clear()
        r = on_loop(EXECUTOR.run("test.e_ok", {}, actor="text"))
        row = last_row("test.e_ok")
        check("B17 with no failure the row is closed as before: success, ok 1, its words", row["outcome"] == audit.SUCCESS and row["ok"] == 1
              and row["result"] == "Wrote the file." and r.ok, str(row))
        try:
            on_loop(EXECUTOR.run("test.e_raises", {}, actor="text"))
        except ValueError:
            pass
        row = last_row("test.e_raises")
        check("B18 ...a raised exception is closed as a failure with its message", row["outcome"] == audit.FAILURE and row["ok"] == 0
              and "the skill failed on its own" in (row["error"] or ""), str(row))
        verdict = on_loop(cancelled_run("test.e_async_slow", "async:start"))
        row = last_row("test.e_async_slow")
        check("B19 ...and a cancel is closed as cancelled", verdict == "cancelled" and row["outcome"] == audit.CANCELLED and row["ok"] == 0, f"{verdict} {row}")
    finally:
        FLAKY.update(complete=False, unknown=False)
        BUS.unsubscribe("*", listen)
        GATE.set()


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    scenario("C. the bus: one client that stopped reading cannot break a publish")

    async def body() -> dict:
        out: dict = {}
        probe = remote_events.project("skill.done", {"skill": "test.x", "ms": 1, "speech": "secret words"})
        out["projectable"] = probe is not None
        slow = BUS.stream()
        for _ in range(1000):
            slow.put_nowait(Event("filler", {}))
        healthy = BUS.stream()
        try:
            try:
                await BUS.publish("skill.done", skill="test.x", ms=1, speech="words")
                out["raised"] = None
            except BaseException as exc:  # noqa: BLE001
                out["raised"] = repr(exc)
            out["slow_registered"] = slow in BUS._queues
            out["healthy_registered"] = healthy in BUS._queues
            out["slow_size"] = slow.qsize()
            out["slow_first"] = slow.get_nowait().topic if slow.qsize() else None
            out["healthy_topics"] = [healthy.get_nowait().topic for _ in range(healthy.qsize())]
            await BUS.publish("skill.done", skill="test.y", ms=2)
            out["healthy_after"] = [healthy.get_nowait().topic for _ in range(healthy.qsize())]
            out["slow_after"] = slow.qsize()
            out["healthy_data"] = None

            # a second slow client in the same publish: both are dropped, the healthy one still gets it
            slow2, slow3 = BUS.stream(), BUS.stream()
            for q in (slow2, slow3):
                for _ in range(1000):
                    q.put_nowait(Event("filler", {}))
            await BUS.publish("skill.done", skill="test.z", ms=3)
            out["both_dropped"] = slow2 not in BUS._queues and slow3 not in BUS._queues
            out["healthy_got_z"] = [healthy.get_nowait().topic for _ in range(healthy.qsize())]

            # other exceptions are not swallowed
            class Broken:
                def put_nowait(self, item):
                    raise RuntimeError("not a full queue")

            broken = Broken()
            BUS._queues.add(broken)
            try:
                await BUS.publish("skill.done", skill="test.w", ms=4)
                out["other_raised"] = None
            except RuntimeError as exc:
                out["other_raised"] = str(exc)
            finally:
                BUS._queues.discard(broken)
            real_project = remote_events.project
            remote_events.project = lambda topic, data: (_ for _ in ()).throw(KeyError("projection bug"))
            try:
                await BUS.publish("skill.done", skill="test.v", ms=5)
                out["project_raised"] = None
            except KeyError as exc:
                out["project_raised"] = str(exc)
            finally:
                remote_events.project = real_project

            # the executor, end to end: the publish that used to raise out of skill.start / skill.done
            wedged = BUS.stream()
            for _ in range(1000):
                wedged.put_nowait(Event("filler", {}))
            before = last_row_sync("test.e_ok")
            res = await EXECUTOR.run("test.e_ok", {}, actor="text")
            out["exec_ok"] = bool(res.ok)
            row = last_row_sync("test.e_ok")
            out["exec_row"] = (row["id"] != (before or {}).get("id"), row["outcome"], row["ok"])
            out["wedged_dropped"] = wedged not in BUS._queues
            out["healthy_exec"] = [healthy.get_nowait().topic for _ in range(healthy.qsize())]
        finally:
            for q in list(BUS._queues):
                BUS.drop(q)
        return out

    o = on_loop(body())
    check("C0 the fixture event is one the remote projection delivers (so the queues really receive it)", o["projectable"])
    check("C1 publish does not raise when a client's queue is full", o["raised"] is None, str(o["raised"]))
    check("C2 the slow client is dropped from the bus; the healthy one is kept", not o["slow_registered"] and o["healthy_registered"])
    check("C3 the slow client's backlog is replaced by one fixed bus.dropped notice (so it learns it missed events)",
          o["slow_size"] == 1 and o["slow_first"] == "bus.dropped", f"{o['slow_size']} {o['slow_first']}")
    check("C4 the healthy client got the event", o["healthy_topics"] == ["skill.done"], str(o["healthy_topics"]))
    check("C5 later events reach the healthy client and no longer the dropped one",
          o["healthy_after"] == ["skill.done"] and o["slow_after"] == 0, f"{o['healthy_after']} {o['slow_after']}")
    check("C6 several slow clients are all dropped in one publish and the healthy one still receives it",
          o["both_dropped"] and o["healthy_got_z"] == ["skill.done"], f"{o['both_dropped']} {o['healthy_got_z']}")
    check("C7 an exception from a queue that is not 'full' is NOT swallowed", o["other_raised"] == "not a full queue", str(o["other_raised"]))
    check("C8 nor is one from the projection", o["project_raised"] is not None and "projection bug" in o["project_raised"], str(o["project_raised"]))
    check("C9 the executor, with a wedged client attached: the call succeeds and is audited as a success (it was reported failed before)",
          o["exec_ok"] and o["exec_row"][0] and o["exec_row"][1] == audit.SUCCESS and o["exec_row"][2] == 1, str(o["exec_row"]))
    check("C10 ...the wedged client was dropped, the healthy one saw the skill finish",
          o["wedged_dropped"] and "skill.done" in o["healthy_exec"], f"{o['wedged_dropped']} {o['healthy_exec']}")


def last_row_sync(skill_name: str) -> dict | None:
    r = store.conn().execute("SELECT * FROM audit WHERE skill=? ORDER BY id DESC LIMIT 1", (skill_name,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------------------------------------------------------
def section_d(cap: Capture) -> None:
    scenario("D. plan.run: a planner exception keeps the steps that ran")
    CFG.planner.step_timeout_s = 90.0
    CFG.planner.total_timeout_s = 120.0

    # the model connection dies on the 3rd decision turn, after two steps ran
    cap.messages.clear()
    r = run_plan(GOAL_TWO, [call("test.e_ok"), call("test.e_undoable"), done("all done")], fail_at=2)
    gid = r.data["goal_id"]
    check("D1 the run returns a result (not an exception) and it is NOT ok", isinstance(r, SkillResult) and r.ok is False)
    check("D2 it says why it stopped, and what was done", r.data["stopped"] == "planner_error" and r.data["error"] == "RuntimeError", str(r.data.get("stopped")))
    check("D3 both steps that really ran are in the result", [s["tool"] for s in r.data["steps"]] == ["test.e_ok", "test.e_undoable"]
          and all(s["ok"] for s in r.data["steps"]), str(r.data["steps"]))
    check("D4 the report counts them and says the rest did not happen", [t for t, _ in pstates(r)] == ["test.e_ok", "test.e_undoable"]
          and r.data["report"]["outcome"] in ("partial",) and r.data["report"]["remaining"], str(r.data["report"]))
    check("D5 the speech names what was done and never the exception's text", "Wrote the file." in r.speech and "Changed it." in r.speech
          and PW not in r.speech and "connection reset" not in r.speech, r.speech)
    goal = goals_mod.get(gid)
    check("D6 the goal is finished (not left running) and failed or partial", goal.status in (goals_mod.GoalStatus.FAILED, goals_mod.GoalStatus.PARTIAL), str(goal.status))
    check("D7 the stop flag / running goal is cleared, and the run is recorded as failed (not idle)",
          INTEL.goal_running is False and INTEL.state.goal_status == "failed" and INTEL.state.execution_status == "failed",
          f"{INTEL.state.goal_status} {INTEL.state.execution_status}")
    progress = goal.progress if isinstance(goal.progress, dict) else {}
    check("D8 the step progress was saved with the reason", progress.get("stopped") == "planner_error" and len(progress.get("steps", [])) == 2, str(progress))
    check("D9 a restart rebuilds the same two steps", [t for t, _ in states(steps.reconstruct(gid))] == ["test.e_ok", "test.e_undoable"])
    check("D10 the exception is logged (type and place), without its canary",
          "the planner layer raised" in cap.text() and "RuntimeError" in cap.text() and PW not in cap.text(), cap.text()[-300:])
    check("D11 nothing ran twice", LOG.count("ok") == 1)

    # no step ran before the failure
    r0 = run_plan(GOAL_TWO, [call("test.e_ok")], fail_at=0)
    check("D12 a planner that fails at once: ok=False, nothing reported as done",
          r0.ok is False and r0.data["stopped"] == "planner_error" and r0.data["steps"] == [] and "before I had done anything" in r0.speech, r0.speech)

    # an unknown outcome is kept and said
    r1 = run_plan(GOAL_TWO, [call("test.e_ok"), call("test.e_unknown"), done("done")], fail_at=2)
    check("D13 a step with an UNKNOWN outcome before the failure stays unknown in the report and the speech",
          ("test.e_unknown", "unknown") in pstates(r1) and r1.data["report"]["outcome"] == "unknown"
          and "Outcome unknown: 'test.e_unknown'" in r1.speech and r1.ok is False, r1.speech + str(pstates(r1)))
    check("D14 ...and is not retried", LOG.count("ok") == 1)

    # a step the Executor ran but whose Observation was never made (the planner layer raised around it)
    original = Orchestrator.run_goal

    async def raising_run_goal(self, goal, **kw):
        await EXECUTOR.run("test.e_unknown", {}, actor="text")
        raise RuntimeError(f"boom password={PW}")

    Orchestrator.run_goal = raising_run_goal
    try:
        r2 = on_loop(EXECUTOR.run("plan.run", {"goal": GOAL_TWO}, actor="text"))
    finally:
        Orchestrator.run_goal = original
    check("D15 a step that ran but has no Observation is recovered from the audit log as unknown",
          ("test.e_unknown", "unknown") in pstates(r2) and r2.ok is False and "Outcome unknown: 'test.e_unknown'" in r2.speech, r2.speech + str(pstates(r2)))
    check("D16 ...it is not listed twice when an Observation does exist (D13), nor when it is recovered from the audit log (D15)",
          [t for t, _ in pstates(r1)].count("test.e_unknown") == 1 and [t for t, _ in pstates(r2)].count("test.e_unknown") == 1,
          str(pstates(r1)) + str(pstates(r2)))
    check("D16b the speech for a run whose only step is the recovered unknown one does not claim the steps 'had not worked'",
          "had not worked" not in r2.speech and "had worked" not in r2.speech and "can't tell" in r2.speech, r2.speech)

    # a step that SUCCEEDED but whose Observation was never made: its row is closed, and it is still reported (as unknown: this run never saw its result)
    async def succeeding_run_goal(self, goal, **kw):
        await EXECUTOR.run("test.e_ok", {}, actor="text")
        raise RuntimeError(f"boom password={PW}")

    LOG.clear()
    Orchestrator.run_goal = succeeding_run_goal
    try:
        r2b = on_loop(EXECUTOR.run("plan.run", {"goal": GOAL_TWO}, actor="text"))
    finally:
        Orchestrator.run_goal = original
    check("D16c an executed step with a closed row and no Observation is not lost: it is in the report, and the speech does not say nothing was done",
          ("test.e_ok", "unknown") in pstates(r2b) and "before I had done anything" not in r2b.speech and LOG == ["ok"], r2b.speech + str(pstates(r2b)))
    ep = store.conn().execute("SELECT COUNT(*) AS n FROM episodes WHERE goal_id=?", (gid,)).fetchone()["n"]
    check("D16d a planner failure is infrastructure, not experience: no episode is recorded for the goal (as for a run that did nothing)", ep == 0, str(ep))

    # the other endings of the same boundary are unchanged
    r3 = run_plan(GOAL_TWO, [call("test.e_ok"), call("test.e_undoable"), done("Wrote and changed.")])
    check("D17 a normal run is unaffected: ok, completed", r3.ok is True and r3.data["stopped"] == "completed" and "error" not in r3.data, str(r3.data.get("stopped")))
    ep3 = store.conn().execute("SELECT COUNT(*) AS n FROM episodes WHERE goal_id=?", (r3.data["goal_id"],)).fetchone()["n"]
    check("D17b ...and its episode is still recorded", ep3 == 1, str(ep3))

    async def cancel_plan() -> str:
        with scripted_provider(Planner([call("test.e_async_slow"), done("ok")])):
            before = LOG.count("async:start")
            t = asyncio.ensure_future(EXECUTOR.run("plan.run", {"goal": GOAL_ASYNC}, actor="text"))
            for _ in range(200):                       # cancel once the step is really running
                if LOG.count("async:start") > before:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                return "cancelled" if t.cancelled() else "cancelled-but-not-the-task"
            except BaseException as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
            return "returned"

    check("D18 a cancel of the run is still a cancel (not turned into a result)", on_loop(cancel_plan()) == "cancelled")

    class Fatal(BaseException):
        pass

    async def fatal_run_goal(self, goal, **kw):
        raise Fatal()

    Orchestrator.run_goal = fatal_run_goal
    try:
        try:
            on_loop(EXECUTOR.run("plan.run", {"goal": GOAL_TWO}, actor="text"))
            got = None
        except BaseException as exc:  # noqa: BLE001
            got = exc
    finally:
        Orchestrator.run_goal = original
    check("D19 a BaseException that is not an Exception is not caught", isinstance(got, Fatal), repr(got))
    if INTEL.goal_running:                  # the escaping exception legitimately leaves its goal open; do not carry that into D20/D21
        INTEL.end_goal(ok=False, status="failed")

    CFG.planner.step_timeout_s = 1.0
    r4 = run_plan(GOAL_ASYNC, [call("test.e_async_slow"), done("ok")])
    check("D20 a step timeout is still its own ending ('timeout'), not 'planner_error'", r4.data["stopped"] == "timeout", str(r4.data.get("stopped")))
    CFG.planner.step_timeout_s = 90.0

    CFG.planner.total_timeout_s = 1.5
    r5 = run_plan(GOAL_ASYNC, [call("test.e_async_slow"), done("ok")])
    CFG.planner.total_timeout_s = 120.0
    check("D21 the overall time limit is still its own ending", r5.data["stopped"] == "timeout", str(r5.data.get("stopped")))


if __name__ == "__main__":
    sys.exit(main())
