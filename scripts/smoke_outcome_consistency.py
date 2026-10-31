"""Phase 96 -- the execution layer records an outcome wherever it can establish one, and only there.

Phase 94 stored an outcome for a call that RETURNED a result. This phase audits the endings that return nothing -- a call
cancelled mid-flight, refused before it began, abandoned while its worker thread kept going -- and fixes the ones the audit
described wrongly. Everything is driven through the real `Executor.run`, with real cancellations and real threads.

  A  a call the skill never saw (unknown arguments) is `not_started`, not a `failure`; a normal call is untouched
  B  an async skill cancelled mid-flight is `cancelled` (ok stays 0 and the error text is the old one), never `failure`
  C  a worker-thread skill cancelled while still queued never ran: `not_started`, and it really did not run
  D  the detach race: a cancel landing as the worker takes its job used to leave "failed" for a thread that then ran. The
     row is unknown until the thread ends, and the thread's real outcome is recorded
  E  an abandoned worker that finishes: success / failure / a shell status all land; another skill's status never does
  F  a cancel while `skill.start` is published used to leave the row open for good: now `not_started`
  G  shell classifications are unchanged (all six, through the real shell.run), including after an abandoned run
  H  tool-specific status values (`status`, `stopped`) never become an execution outcome
  I  paths left unclassified on purpose: denied, a skill that raised, reconcile's `interrupted`
  J  legacy rows read exactly as before; the schema is unchanged (no column added, nothing rewritten)

Deterministic; every database is a throwaway under the temp folder (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, permissions, shellproc, store  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, Detach, SkillResult, skill  # noqa: E402

REGISTRY.discover()

RAN: list[str] = []
BODY_ENTERED = threading.Event()
RELEASE = threading.Event()


@skill(name="test.ok96", tier="L0", description="Phase 96 test skill that succeeds.")
def _ok() -> SkillResult:
    RAN.append("test.ok96")
    return SkillResult(speech="Did it.")


@skill(name="test.status96", tier="L0", description="Phase 96 test skill whose data carries other skills' status words.")
def _status(kind: str = "timeout") -> SkillResult:
    data = {
        "timeout": {"status": "timeout"}, "cancelled": {"status": "cancelled"}, "not_started": {"status": "not_started"},
        "unknown": {"status": "unknown_outcome"}, "plan": {"status": "failed", "stopped": "timeout"},
    }[kind]
    return SkillResult(speech="no", ok=False, data=data)


@skill(name="test.okstatus96", tier="L0", description="Phase 96 test skill that succeeds with a status word.")
def _okstatus() -> SkillResult:
    return SkillResult(speech="fine", data={"status": "cancelled"})


@skill(name="test.async96", tier="L0", description="Phase 96 async test skill that waits to be cancelled.")
async def _async_wait() -> SkillResult:
    BODY_ENTERED.set()
    await asyncio.sleep(30)
    return SkillResult(speech="never")


@skill(name="test.thread96", tier="L0", description="Phase 96 worker-thread test skill.")
def _thread(mode: str = "ok") -> SkillResult:
    RAN.append("test.thread96")
    BODY_ENTERED.set()
    RELEASE.wait(10)
    if mode == "boom":
        raise RuntimeError("worker died")
    if mode == "status":   # a non-shell skill finishing late with another skill's status word
        return SkillResult(speech="late no", ok=False, data={"status": "cancelled"})
    return SkillResult(speech="late done")


@skill(name="test.boom96", tier="L0", description="Phase 96 test skill that raises.")
def _boom() -> SkillResult:
    raise RuntimeError("it broke")


@skill(name="test.gated96", tier="L2", description="Phase 96 test skill behind a confirmation.")
def _gated() -> SkillResult:
    RAN.append("test.gated96")
    return SkillResult(speech="ran")


def rows_of(skill_name: str) -> list[dict]:
    return [r for r in reversed(audit.recent(500)) if r["skill"] == skill_name]


def last(skill_name: str) -> dict:
    return rows_of(skill_name)[-1]


def raw(audit_id: int) -> dict:
    return dict(store.conn().execute("SELECT * FROM audit WHERE id=?", (audit_id,)).fetchone())


async def settle(skill_name: str, want, timeout: float = 5.0) -> dict:
    """Wait for the newest row of a skill to reach an outcome (a late write happens in a worker thread)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = last(skill_name)
        if row["outcome"] == want:
            return row
        await asyncio.sleep(0.02)
    return last(skill_name)


async def cancelled_run(name: str, args: dict | None, wait_for: threading.Event | None = None) -> bool:
    """Start the real Executor.run, wait until the skill body is running (or a moment), cancel it. True if it raised Cancelled."""
    task = asyncio.create_task(EXECUTOR.run(name, args or {}))
    if wait_for is not None:
        await asyncio.get_running_loop().run_in_executor(None, wait_for.wait, 5)
    else:
        await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


class GatedDetach(Detach):
    """A Detach whose `started` flag, when a WORKER thread sets it, waits at a gate -- holding the exact moment between the worker
    taking its job (future RUNNING) and recording that it has. The event loop's own writes pass straight through."""

    gate = threading.Event()
    in_setter = threading.Event()
    loop_tid = 0

    @property
    def started(self):
        return self._started

    @started.setter
    def started(self, value):
        if value and threading.get_ident() != GatedDetach.loop_tid:
            GatedDetach.in_setter.set()
            GatedDetach.gate.wait(10)
        self._started = value


SHELL_CASES = {
    "success": (shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout="hi"), 1, "success"),
    "failure": (shellproc.Outcome(shellproc.COMPLETED, returncode=3, stderr="boom"), 0, "failure"),
    "not_started": (shellproc.Outcome(shellproc.NOT_STARTED, detail="no_job"), 0, "not_started"),
    "unknown_outcome": (shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed"), None, "unknown_outcome"),
    "timeout": (shellproc.Outcome(shellproc.TIMEOUT, how="forced"), 0, "timeout"),
    "cancelled": (shellproc.Outcome(shellproc.CANCELLED, how="graceful"), 0, "cancelled"),
}


async def main_async() -> None:
    loop = asyncio.get_running_loop()
    GatedDetach.loop_tid = threading.get_ident()
    EXECUTOR.set_confirm_handler(lambda s, a, p: asyncio.sleep(0, result=(s.name == "shell.run")))   # only shell.run is approved
    columns_before = [r["name"] for r in store.conn().execute("PRAGMA table_info(audit)")]

    # ------------------------------------------------------------------------------------------------ A
    scenario("A. a call the skill never saw is not_started")
    RAN.clear()
    res = await EXECUTOR.run("test.ok96", {"bogus": 1})
    row = last("test.ok96")
    check("A1 unknown arguments: the skill refused, ok False, and its body did not run", res.ok is False and RAN == [] and "doesn't take argument" in res.speech)
    check("A2 ...the row is not_started (not a failure), ok 0, reads as failed to older readers", row["outcome"] == "not_started" and row["ok"] == 0 and row["status"] == "failed", str(row))
    res = await EXECUTOR.run("test.ok96", {})
    row = last("test.ok96")
    check("A3 a valid call is untouched: success", res.ok and RAN == ["test.ok96"] and row["outcome"] == "success" and row["ok"] == 1)
    await EXECUTOR.run("test.ok96", {"bogus": 1, "other": 2})
    check("A4 several unknown arguments: still not_started", last("test.ok96")["outcome"] == "not_started")
    RAN.clear()
    await EXECUTOR.run("test.thread96", {"bogus": 1})
    check("A5 a worker-thread skill refused for its arguments: not_started, its body never ran",
          last("test.thread96")["outcome"] == "not_started" and RAN == [])
    await EXECUTOR.run("test.async96", {"bogus": 1})
    check("A6 an async skill refused for its arguments: not_started (not cancelled), returned at once", last("test.async96")["outcome"] == "not_started")

    # ------------------------------------------------------------------------------------------------ B
    scenario("B. an async skill cancelled mid-flight is cancelled")
    BODY_ENTERED.clear()
    raised = await cancelled_run("test.async96", None, BODY_ENTERED)
    row = last("test.async96")
    check("B1 the cancellation still propagates to the caller", raised)
    check("B2 the row is cancelled with ok 0 (it is not a plain failure)", row["outcome"] == "cancelled" and row["ok"] == 0, str(row))
    check("B3 the old error text is kept, and older readers still read it as failed",
          row["error"] == "interrupted before it finished (cancelled)" and row["status"] == "failed")
    check("B4 it is NOT marked unknown: nothing of it is still running", row["outcome"] != "unknown_outcome" and row["ok"] is not None)

    # ------------------------------------------------------------------------------------------------ C
    scenario("C. a worker-thread skill cancelled while queued never ran")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    blocker_running, free = threading.Event(), threading.Event()
    pool.submit(lambda: (blocker_running.set(), free.wait(10)))
    blocker_running.wait(5)
    RAN.clear()
    BODY_ENTERED.clear()
    RELEASE.set()
    task = asyncio.create_task(EXECUTOR.run("test.thread96", {}))
    await asyncio.sleep(0.3)               # its job is queued behind the blocker
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    row = last("test.thread96")            # as it stands right after the cancel...
    free.set()
    await asyncio.sleep(0.4)               # ...the pool is free again: a job that was still queued would run now
    check("C1 the skill body never ran", RAN == [] and not BODY_ENTERED.is_set(), str(RAN))
    check("C2 the row is not_started (ok 0), not cancelled and not unknown", row["outcome"] == "not_started" and row["ok"] == 0, str(row))
    check("C3 nothing was recorded later: the row is the same after the pool freed up", last("test.thread96") == row)
    pool.shutdown(wait=True)
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=8))
    RELEASE.clear()

    # ------------------------------------------------------------------------------------------------ D
    scenario("D. the detach race: the cancel lands as the worker takes its job")
    permissions.Detach = GatedDetach
    GatedDetach.gate.clear()
    GatedDetach.in_setter.clear()
    BODY_ENTERED.clear()
    RELEASE.clear()
    RAN.clear()
    try:
        task = asyncio.create_task(EXECUTOR.run("test.thread96", {}))
        await loop.run_in_executor(None, GatedDetach.in_setter.wait, 5)   # the worker holds the job, `started` not yet recorded
        check("D0 the worker has taken the job but not started the skill body", GatedDetach.in_setter.is_set() and RAN == [])
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        row = last("test.thread96")
        check("D1 the row is unknown_outcome with ok NULL -- NOT a definite failure for a thread that is about to run",
              row["outcome"] == "unknown_outcome" and row["ok"] is None and row["status"] == "outcome_unknown", str(row))
        GatedDetach.gate.set()
        await loop.run_in_executor(None, BODY_ENTERED.wait, 5)
        check("D2 the thread did run", RAN == ["test.thread96"])
        RELEASE.set()
        row = await settle("test.thread96", "success")
        check("D3 its real outcome is recorded when it ends (it used to stay 'failed' for good)", row["outcome"] == "success" and row["ok"] == 1, str(row))
    finally:
        permissions.Detach = Detach
        GatedDetach.gate.set()
        RELEASE.set()

    # ------------------------------------------------------------------------------------------------ E
    scenario("E. an abandoned worker that finishes is recorded consistently")
    for mode, want_outcome, want_ok, label in (
        ("ok", "success", 1, "success"),
        ("boom", "failure", 0, "an exception"),
        ("status", "failure", 0, "ok=False carrying another skill's 'cancelled' status"),
    ):
        BODY_ENTERED.clear()
        RELEASE.clear()
        raised = await cancelled_run("test.thread96", {"mode": mode}, BODY_ENTERED)
        mid = last("test.thread96")
        RELEASE.set()
        row = await settle("test.thread96", want_outcome)
        check(f"E1 {label}: abandoned while running -> unknown_outcome (ok NULL) first", raised and mid["outcome"] == "unknown_outcome" and mid["ok"] is None, str(mid))
        check(f"E2 {label}: the worker's end lands as {want_outcome}", row["outcome"] == want_outcome and row["ok"] == want_ok, str(row))
    check("E3 the late 'cancelled' status of a non-shell skill did not become the cancelled outcome", last("test.thread96")["outcome"] == "failure")

    # ------------------------------------------------------------------------------------------------ F
    scenario("F. a cancel while skill.start is published leaves no open row")
    original_publish = permissions.BUS.publish

    def publisher(exc):
        async def publish(topic, **kw):
            if topic == "skill.start":
                raise exc
            return await original_publish(topic, **kw)
        return publish

    RAN.clear()
    permissions.BUS.publish = publisher(asyncio.CancelledError())
    try:
        raised = False
        try:
            await EXECUTOR.run("test.ok96", {})
        except asyncio.CancelledError:
            raised = True
    finally:
        permissions.BUS.publish = original_publish
    row = last("test.ok96")
    check("F1 the cancellation propagates and the skill never ran", raised and RAN == [])
    check("F2 the row is closed as not_started (it used to stay open: ok/result/error all NULL)", row["outcome"] == "not_started" and row["ok"] == 0 and row["status"] == "failed", str(row))
    permissions.BUS.publish = publisher(RuntimeError("bus down"))
    try:
        with contextlib.suppress(RuntimeError):
            await EXECUTOR.run("test.ok96", {})
    finally:
        permissions.BUS.publish = original_publish
    row = last("test.ok96")
    check("F3 a failing start event: the skill never ran, the row is not_started with the error", RAN == [] and row["outcome"] == "not_started" and "bus down" in (row["error"] or ""), str(row))
    check("F4 a normal call right after is fine", (await EXECUTOR.run("test.ok96", {})).ok and last("test.ok96")["outcome"] == "success")
    original_complete = audit.complete

    def broken_complete(*a, **kw):
        raise RuntimeError("database is locked")

    permissions.BUS.publish = publisher(asyncio.CancelledError())
    audit.complete = broken_complete
    kept = False
    try:
        try:
            await EXECUTOR.run("test.ok96", {})
        except asyncio.CancelledError:
            kept = True
        except RuntimeError:
            kept = False
    finally:
        audit.complete = original_complete
        permissions.BUS.publish = original_publish
    check("F5 if closing the row fails, the caller still gets the CancelledError (not the database error)", kept)
    stuck = last("test.ok96")
    check("F5b ...and that row really was left open by the failure (so the next line closes it by hand for the later checks)", stuck["ok"] is None and stuck["outcome"] is None)
    audit.complete(stuck["id"], ok=False, outcome=audit.NOT_STARTED, error="closed by the test")

    # ------------------------------------------------------------------------------------------------ G
    scenario("G. shell classifications are unchanged")
    original_run_owned = shellproc.run_owned
    try:
        for name, (outcome, want_ok, want_outcome) in SHELL_CASES.items():
            shellproc.run_owned = lambda argv, _o=outcome, **kw: _o
            await EXECUTOR.run("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": str(ROOT)})
            row = last("shell.run")
            check(f"G1 {name}: outcome {want_outcome}, ok {want_ok!r}", row["outcome"] == want_outcome and row["ok"] == want_ok, str(row))
        statuses = {r["outcome"]: r["status"] for r in rows_of("shell.run")[-6:]}
        check("G2 not_started/timeout/cancelled still read failed to older readers; unknown reads outcome_unknown", statuses == {"success": "succeeded", "failure": "failed", "not_started": "failed", "unknown_outcome": "outcome_unknown",
                                                      "timeout": "failed", "cancelled": "failed"}, str(statuses))
        # shell.run abandoned mid-run (a plan timeout), then the command's own cancelled status arrives
        gate, entered = threading.Event(), threading.Event()

        def slow(argv, **kw):
            entered.set()
            gate.wait(10)
            return shellproc.Outcome(shellproc.CANCELLED, how="graceful")

        shellproc.run_owned = slow
        raised = await cancelled_run("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": str(ROOT)}, entered)
        mid = last("shell.run")
        gate.set()
        row = await settle("shell.run", "cancelled")
        check("G3 abandoned shell.run: unknown first, then its own cancelled status lands (ok 0)",
              raised and mid["outcome"] == "unknown_outcome" and mid["ok"] is None and row["outcome"] == "cancelled" and row["ok"] == 0, f"{mid} {row}")
        gate2, entered2 = threading.Event(), threading.Event()

        def slow_unknown(argv, **kw):
            entered2.set()
            gate2.wait(10)
            return shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed")

        shellproc.run_owned = slow_unknown
        await cancelled_run("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": str(ROOT)}, entered2)
        gate2.set()
        await asyncio.sleep(0.5)
        row = last("shell.run")
        check("G4 abandoned shell.run whose own end is unknown_outcome: stays unknown (ok NULL)", row["outcome"] == "unknown_outcome" and row["ok"] is None, str(row))
    finally:
        shellproc.run_owned = original_run_owned

    # ------------------------------------------------------------------------------------------------ H
    scenario("H. tool-specific status values are not execution outcomes")
    for kind in ("timeout", "cancelled", "not_started", "unknown", "plan"):
        await EXECUTOR.run("test.status96", {"kind": kind})
        row = last("test.status96")
        check(f"H1 a non-shell skill returning {kind!r} data: failure, ok 0", row["outcome"] == "failure" and row["ok"] == 0, str(row))
    await EXECUTOR.run("test.okstatus96", {})
    check("H2 ok=True with status 'cancelled' is a success", last("test.okstatus96")["outcome"] == "success")
    check("H3 the mapping still only knows shell.run", set(audit._STATUS_OUTCOMES) == {"shell.run"})
    check("H4 plan.run's timeout (data.stopped) and goal status are not read as outcomes",
          audit.outcome_of_result("plan.run", False, {"stopped": "timeout", "status": "failed"}) == audit.FAILURE
          and audit.outcome_of_result("plan.run", False, {"stopped": "cancelled"}) == audit.FAILURE)

    # ------------------------------------------------------------------------------------------------ I
    scenario("I. paths left unclassified on purpose")
    RAN.clear()
    res = await EXECUTOR.run("test.gated96", {})
    row = last("test.gated96")
    check("I1 a declined confirmation: it never ran, decision 'denied', no outcome", RAN == [] and not res.ok and row["decision"] == "denied" and row["outcome"] is None and row["status"] == "denied", str(row))
    with contextlib.suppress(RuntimeError):
        await EXECUTOR.run("test.boom96", {})
    row = last("test.boom96")
    check("I2 a skill that raised is failure (Phase 94, unchanged)", row["outcome"] == "failure" and row["ok"] == 0)
    rid = audit.record(actor="text", skill="test.ok96", tier="L0", args={}, decision="auto")
    store.conn().execute("UPDATE audit SET session_id='dead-96' WHERE id=?", (rid,))
    store.conn().execute("INSERT INTO runtime_sessions (id, kind, pid, proc_start, host, started_at) VALUES ('dead-96','cli',1,'t','h',?)", (store.now(),))
    store.conn().commit()
    audit.reconcile_orphans(lambda pid, start, host: "dead")
    row = next(r for r in audit.recent(500) if r["id"] == rid)
    check("I3 a row an earlier process left open stays interrupted with NO outcome (nobody saw what happened)", row["status"] == "interrupted" and row["outcome"] is None)
    open_rows = [r for r in audit.recent(500) if r["ok"] is None and r["error"] is None and r["result"] is None and r["decision"] in ("auto", "confirmed")]
    check("I4 no row of this run was left open except the one made to be reconciled", all(r["id"] == rid or r["status"] == "interrupted" for r in open_rows) and len(open_rows) <= 1, str([r["id"] for r in open_rows]))

    # ------------------------------------------------------------------------------------------------ J
    scenario("J. legacy rows and the schema")
    c = store.conn()
    legacy = []
    for ok, result, error in ((1, "Did it.", None), (0, "Could not.", None), (0, None, "interrupted before it finished (cancelled)")):
        cur = c.execute("INSERT INTO audit (at, actor, skill, tier, args, decision, ok, result, error, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (store.now(), "text", "legacy.skill96", "L0", "{}", "auto", ok, result, error, 5))
        legacy.append(int(cur.lastrowid))
    c.commit()
    before = [raw(i) for i in legacy]
    rows = {r["id"]: r for r in audit.recent(500) if r["id"] in legacy}
    check("J1 legacy rows have no outcome and read exactly as before",
          [rows[i]["outcome"] for i in legacy] == [None] * 3 and [rows[i]["status"] for i in legacy] == ["succeeded", "failed", "failed"])
    BODY_ENTERED.clear()
    await cancelled_run("test.async96", None, BODY_ENTERED)
    new_cancel = last("test.async96")
    old_style = rows[legacy[2]]
    check("J2 a new cancelled row looks the same to an older reader (ok, error text, status) as the legacy interrupted one",
          (new_cancel["ok"], new_cancel["error"], new_cancel["status"]) == (old_style["ok"], old_style["error"], old_style["status"]))
    check("J3 nothing back-filled: the legacy rows are byte-for-byte what they were", [raw(i) for i in legacy] == before)
    columns_after = [r["name"] for r in store.conn().execute("PRAGMA table_info(audit)")]
    check("J4 the schema is unchanged (no column added by this phase)", columns_before == columns_after and "outcome" in columns_after)
    check("J5 every stored outcome is a vocabulary word or NULL", all(r["outcome"] is None or r["outcome"] in audit.OUTCOMES for r in audit.recent(500)))


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        asyncio.run(main_async())
    return finish("Phase 96 -- consistent execution outcome classification", time.perf_counter() - t0, min_assertions=50, min_scenarios=10)


if __name__ == "__main__":
    raise SystemExit(main())
