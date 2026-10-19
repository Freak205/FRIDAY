"""Phase 52 -- process-aware ownership of audited actions, and the conservative recovery built on it.

Phase 51 labelled open audit rows `interrupted` from the operating-system boot time. That is a guess about a clock, it cannot see
a process that crashed without a reboot, and a clock change could mislabel a live process's work. Phase 52 replaces it: every
started action is bound (in the transaction that creates its row) to the runtime session that started it, and recovery only
speaks when that session's process is PROVABLY gone. This suite checks that with real second processes sharing one throwaway
database, and with injected probes for the cases the OS cannot show on demand.

Nothing here executes, retries or undoes a skill during recovery (spied), and no real database, `data/` folder or user file is
read: every database is a throwaway under the temp folder (`iso_env`), and the child processes are pointed at one too.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _child(db_path: str) -> int:
    """A second FRIDAY process: opens the shared throwaway database, starts an action (so its row is open), and waits."""
    from friday import audit, paths

    paths.DB_PATH = db_path
    audit.start_session("cli")
    rid = audit.record(actor="text", skill="t.child_work", tier="L1", args={}, decision="auto")
    print(f"READY {rid} {os.getpid()}", flush=True)
    sys.stdin.read()  # until the parent closes the pipe or kills us
    return 0


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--child":
    sys.exit(_child(sys.argv[2]))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402
from rich.console import Console  # noqa: E402

from friday import audit, cli, owner, paths, store  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.skills import meta  # noqa: E402

SECRET = "hunter2-SECRET-body"
RAN: list[str] = []


@skill(name="test.work52", tier="L0", description="Phase 52 test skill.",
       undo=lambda: {"skill": "test.work52_undo", "args": {}})
def work() -> SkillResult:
    RAN.append("work")
    return SkillResult(speech="Did the work.")


SEEN: list[int | None] = []


@skill(name="test.peek52", tier="L0", description="Phase 52 test skill: reports its own open row's owner pid.")
def peek() -> SkillResult:
    r = store.conn().execute(
        "SELECT s.pid FROM audit a JOIN runtime_sessions s ON s.id=a.session_id WHERE a.skill='test.peek52' AND a.ok IS NULL").fetchone()
    SEEN.append(r["pid"] if r else None)
    return SkillResult(speech="peeked")


@skill(name="test.boom52", tier="L0", description="Phase 52 test skill that raises.")
def boom() -> SkillResult:
    RAN.append("boom")
    raise RuntimeError("it broke")


@skill(name="test.confirm52", tier="L2", description="Phase 52 test skill that needs confirmation.")
def needs_confirm() -> SkillResult:
    RAN.append("confirm")
    return SkillResult(speech="Confirmed work.")


def row(rid: int) -> dict:
    r = dict(store.conn().execute("SELECT * FROM audit WHERE id=?", (rid,)).fetchone())
    r["status"] = audit.status_of(r)
    return r


def sessions() -> list[dict]:
    return [dict(r) for r in store.conn().execute("SELECT * FROM runtime_sessions ORDER BY started_at")]


def open_row(name: str, ident: owner.Identity | None = None, *, decision: str = "auto", args: dict | None = None) -> int:
    if ident is None:
        return audit.record(actor="text", skill=name, tier="L1", args=args or {}, decision=decision)
    with owner._use_identity(ident):
        return audit.record(actor="text", skill=name, tier="L1", args=args or {}, decision=decision)


def fake(label: str, *, pid: int = 4_000_000, start: float | None = 1.0, host: str | None = None) -> owner.Identity:
    return owner.Identity(f"fake-{label}", pid, start, host or owner._host())


def fixed(verdict: str):
    return lambda pid, start, host: verdict


def hold_handle(pid: int):
    """Open (and keep) a Windows process handle, as a parent that never reaped its child does. None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes

    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    return k.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION


def release_handle(h) -> None:
    if h:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


def spawn(db: str) -> tuple[subprocess.Popen, int, int]:
    """Returns the Popen, the child's open audit row id and the child's REAL pid (the venv's python.exe is a launcher
    whose own pid is not the interpreter's)."""
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", db], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=str(ROOT))
    line = p.stdout.readline().strip()
    if not line.startswith("READY"):
        p.kill()
        raise RuntimeError(f"child did not start: {line!r}")
    return p, int(line.split()[1]), int(line.split()[2])


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        db = str(paths.DB_PATH)
        ran: list[str] = []
        real_run = EXECUTOR.run

        async def spy(*a, **k):  # recovery must never execute anything
            ran.append(str(a))
            return await real_run(*a, **k)

        # ------------------------------------------------------------------------------------------------
        scenario("A. two real processes share one database; each row names its own session")
        import psutil

        held = None
        mine = open_row("t.parent_work")
        child, child_rid, child_pid = spawn(db)
        try:
            rows = sessions()
            by_id = {r["id"]: r for r in rows}
            parent_row, child_row = row(mine), row(child_rid)
            check("A1 there are two distinct sessions", len(rows) == 2 and len(by_id) == 2, str(rows))
            check("A2 each row is bound to a different session", parent_row["session_id"] != child_row["session_id"]
                  and parent_row["session_id"] in by_id and child_row["session_id"] in by_id)
            check("A3 the parent's session is this process (pid, start time, host)",
                  by_id[parent_row["session_id"]]["pid"] == os.getpid()
                  and by_id[parent_row["session_id"]]["proc_start"] == owner.identity().proc_start
                  and by_id[parent_row["session_id"]]["host"] == owner._host())
            check("A4 the child's session is the child process, declared as the kind it registered as",
                  by_id[child_row["session_id"]]["pid"] == child_pid and by_id[child_row["session_id"]]["kind"] == "cli"
                  and by_id[child_row["session_id"]]["proc_start"] is not None)

            scenario("B. a live owner is never marked interrupted")
            before = (row(mine), row(child_rid))
            check("B1 reconciling with the real probe changes nothing while the child lives", audit.reconcile_orphans() == 0)
            check("B2 both rows are still unfinished and untouched", (row(mine), row(child_rid)) == before
                  and row(child_rid)["status"] == "unfinished")
            old = "2020-01-01T00:00:00+00:00"
            store.conn().execute("UPDATE audit SET at=? WHERE id=?", (old, child_rid))
            store.conn().commit()
            check("B3 a ten-year-old row of a LIVE owner is not touched: age proves nothing",
                  audit.reconcile_orphans() == 0 and row(child_rid)["status"] == "unfinished")

            scenario("C. PID reuse: the pid exists but the recorded start time is not that process's")
            reused = fake("reuse", pid=child_pid, start=by_id[child_row["session_id"]]["proc_start"] - 5.0)
            reused_rid = open_row("t.reused_pid", reused)
            check("C1 a live pid with a different creation time is a different process: dead", owner.probe(
                reused.pid, reused.proc_start, reused.host) == owner.DEAD)
            check("C2 the same pid with the genuine creation time is alive", owner.probe(
                child_pid, by_id[child_row["session_id"]]["proc_start"], owner._host()) == owner.ALIVE)
            n = audit.reconcile_orphans()
            check("C3 only the reused-pid row is reconciled; the genuine live child's row is not",
                  n == 1 and row(reused_rid)["status"] == "interrupted" and row(child_rid)["status"] == "unfinished", str(n))

            scenario("D. a dead owner: the real process is killed (its handle still open, as for a parent that never reaped it)")
            held = hold_handle(child_pid)  # the OS keeps a dead process object around while anyone holds a handle to it
            psutil.Process(child_pid).kill()
            time.sleep(0.3)
            check("D1 the killed process (handle still held by someone) is reported dead, not alive", owner.probe(
                child_pid, by_id[child_row["session_id"]]["proc_start"], owner._host()) == owner.DEAD)
            EXECUTOR.run = spy  # type: ignore[method-assign]
            try:
                n = audit.reconcile_orphans()
            finally:
                EXECUTOR.run = real_run  # type: ignore[method-assign]
            cr = row(child_rid)
            check("D2 exactly the dead child's open row is marked", n == 1, str(n))
            check("D3 it is interrupted, with ok still NULL, no result, no duration invented",
                  cr["status"] == "interrupted" and cr["ok"] is None and cr["result"] is None and cr["duration_ms"] is None
                  and cr["error"] == audit.INTERRUPTED)
            check("D4 this process's own open row is never touched", row(mine)["status"] == "unfinished")
            check("D5 nothing was executed, retried or undone, and no undo entry or audit row appeared",
                  ran == [] and store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0] == 0
                  and "work" not in RAN)
            check("D6 the wording does not claim the side effect did or did not happen",
                  "unknown" in audit.INTERRUPTED and "nothing was retried or undone" in audit.INTERRUPTED)
            snap = {r["id"]: (r["ok"], r["result"], r["error"], r["session_id"]) for r in audit.recent(100)}
            check("D7 reconciling again changes nothing (idempotent)", audit.reconcile_orphans() == 0
                  and snap == {r["id"]: (r["ok"], r["result"], r["error"], r["session_id"]) for r in audit.recent(100)})
        finally:
            try:
                child.kill()
            except OSError:
                pass
            child.wait(5)
            release_handle(held)

        # ------------------------------------------------------------------------------------------------
        scenario("E. the probe: every case the OS cannot settle stays UNKNOWN")
        me = owner.identity()
        reaped = subprocess.Popen([sys.executable, "-c", "pass"])
        reaped.wait()
        check("E1 a pid that no longer exists is dead", owner.probe(reaped.pid, 1.0, owner._host()) == owner.DEAD)
        check("E2 this very process, with its real start time, is alive", owner.probe(me.pid, me.proc_start, me.host) == owner.ALIVE)
        check("E3 a live pid with NO recorded start time is unknown (whose process is it?)",
              owner.probe(me.pid, None, me.host) == owner.UNKNOWN)
        check("E4 a record from another host is unknown, even for a pid that does not exist",
              owner.probe(reaped.pid, 1.0, "some-other-machine") == owner.UNKNOWN)
        check("E5 a nonsensical pid is unknown", owner.probe(0, 1.0, me.host) == owner.UNKNOWN
              and owner.probe(-3, 1.0, me.host) == owner.UNKNOWN and owner.probe("x", 1.0, me.host) == owner.UNKNOWN)  # type: ignore[arg-type]
        saved_proc = psutil.Process

        def denied(pid):  # a process we may not inspect
            raise psutil.AccessDenied(pid)

        psutil.Process = denied  # type: ignore[assignment]
        try:
            check("E6 access denied while reading the creation time is unknown", owner.probe(me.pid, me.proc_start, me.host) == owner.UNKNOWN)
        finally:
            psutil.Process = saved_proc  # type: ignore[assignment]
        saved_mod = sys.modules.get("psutil")
        sys.modules["psutil"] = None  # type: ignore[assignment]  # import psutil -> ImportError
        try:
            check("E7 no psutil at all is unknown", owner.probe(me.pid, me.proc_start, me.host) == owner.UNKNOWN)
        finally:
            sys.modules["psutil"] = saved_mod  # type: ignore[assignment]
        saved_exists = psutil.pid_exists

        def broken(pid):
            raise OSError("boom")

        psutil.pid_exists = broken  # type: ignore[assignment]
        try:
            check("E8 any unexpected error is unknown, never an exception", owner.probe(me.pid, me.proc_start, me.host) == owner.UNKNOWN)
        finally:
            psutil.pid_exists = saved_exists  # type: ignore[assignment]

        scenario("F. reconcile acts on DEAD only; indeterminate and failing probes leave the row alone")
        rid_unknown = open_row("t.unknown_owner", fake("u"))
        rid_alive = open_row("t.alive_owner", fake("a"))
        rid_dead = open_row("t.dead_owner", fake("d"))
        # three fake sessions share a pid in this table design, so give each a distinct pid
        for label, pid in (("u", 4_000_001), ("a", 4_000_002), ("d", 4_000_003)):
            store.conn().execute("UPDATE runtime_sessions SET pid=? WHERE id=?", (pid, f"fake-{label}"))
        store.conn().commit()
        pid_verdict = {4_000_001: owner.UNKNOWN, 4_000_002: owner.ALIVE, 4_000_003: owner.DEAD}
        n = audit.reconcile_orphans(lambda pid, s, h: pid_verdict[pid])
        check("F1 only the dead owner's row was marked", n == 1 and row(rid_dead)["status"] == "interrupted"
              and row(rid_unknown)["status"] == "unfinished" and row(rid_alive)["status"] == "unfinished")

        def raising(pid, s, h):
            raise RuntimeError("probe blew up")

        rid_x = open_row("t.probe_fails", fake("x"))
        store.conn().execute("UPDATE runtime_sessions SET pid=4000009 WHERE id='fake-x'")
        store.conn().commit()
        check("F2 a probe that raises changes nothing and does not stop reconciliation",
              audit.reconcile_orphans(raising) == 0 and row(rid_x)["status"] == "unfinished")
        check("F3 a probe that returns garbage is not 'dead'", audit.reconcile_orphans(fixed("deadish")) == 0
              and audit.reconcile_orphans(fixed("")) == 0 and row(rid_x)["status"] == "unfinished")

        scenario("G. rows reconciliation must never touch, even for a dead owner")
        dead = fake("g", pid=4_000_010)
        rid_ok = open_row("t.g_ok", dead)
        audit.complete(rid_ok, ok=True, result="Done.", duration_ms=3)
        rid_fail = open_row("t.g_fail", dead)
        audit.complete(rid_fail, ok=False, error="it failed", duration_ms=3)
        rid_den = open_row("t.g_denied", dead, decision="denied")
        rid_blk = open_row("t.g_blocked", dead, decision="blocked")
        rid_unk = open_row("t.g_unknown", dead)
        assert audit.mark_outcome_unknown(rid_unk)
        rid_old_int = open_row("t.g_prev_interrupted", dead)
        store.conn().execute("UPDATE audit SET error=? WHERE id=?", (audit.INTERRUPTED, rid_old_int))
        rid_empty_ok = open_row("t.g_empty_result", dead)
        audit.complete(rid_empty_ok, ok=True, result="", duration_ms=1)
        store.conn().commit()
        before = {r: row(r) for r in (rid_ok, rid_fail, rid_den, rid_blk, rid_unk, rid_old_int, rid_empty_ok)}
        check("G0 the denied and blocked rows do carry an owner", before[rid_den]["session_id"] == "fake-g"
              and before[rid_blk]["session_id"] == "fake-g")
        n = audit.reconcile_orphans(fixed(owner.DEAD))
        after = {r: row(r) for r in before}
        check("G1 success, failure, denied, blocked, shutdown-unknown, earlier-interrupted and empty-result rows are byte-identical",
              after == before, str(n))
        check("G2 their statuses are exactly succeeded/failed/denied/blocked/outcome_unknown/interrupted/succeeded",
              [after[r]["status"] for r in before] == ["succeeded", "failed", "denied", "blocked", "outcome_unknown",
                                                         "interrupted", "succeeded"])
        rid_still_open = open_row("t.g_open", dead)
        check("G3 an unresolved row of that same dead owner IS marked, and only it",
              audit.reconcile_orphans(fixed(owner.DEAD)) == 1 and row(rid_still_open)["status"] == "interrupted"
              and {r: row(r) for r in before} == before)

        scenario("H. legacy rows with no owner are never inferred about")
        c = store.conn()
        legacy = []
        for at, decision in (("2019-01-01T00:00:00+00:00", "auto"), ("2026-10-18T00:00:00+00:00", "confirmed"), (store.now(), "auto")):
            cur = c.execute("INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES (?,?,?,?,?,?)",
                            (at, "text", "t.legacy", "L1", "{}", decision))
            legacy.append(int(cur.lastrowid))
        c.commit()
        check("H1 legacy rows are session-less", all(row(r)["session_id"] is None for r in legacy))
        check("H2 whatever their age, with every probe verdict, they stay unfinished",
              all(audit.reconcile_orphans(fixed(v)) == 0 for v in (owner.DEAD, owner.ALIVE, owner.UNKNOWN))
              and all(row(r)["status"] == "unfinished" for r in legacy))
        check("H3 the Phase 51 boot-time rule is gone (no entry point or API infers death from a clock)",
              not hasattr(audit, "reconcile_stale"))

        scenario("I. recovery versus a real completion (atomic, conditional)")
        racer = fake("race", pid=4_000_020)
        rid_race = open_row("t.race", racer)

        def finish_during_probe(pid, s, h):  # the owner's late result lands after we chose it, before our update
            t = threading.Thread(target=lambda: audit.complete(rid_race, ok=True, result="Done.", duration_ms=7))
            t.start()
            t.join()
            return owner.DEAD

        n = audit.reconcile_orphans(finish_during_probe)
        rr = row(rid_race)
        check("I1 a completion that lands between the decision and the update is never replaced by the marker",
              n == 0 and rr["status"] == "succeeded" and rr["result"] == "Done." and rr["error"] is None, str(rr))
        rid_late = open_row("t.late", fake("late", pid=4_000_021))
        audit.reconcile_orphans(fixed(owner.DEAD))
        was = row(rid_late)["status"]
        audit.complete(rid_late, ok=True, result="Done.", duration_ms=4)
        check("I2 a real result arriving after the marker replaces it", was == "interrupted"
              and row(rid_late)["status"] == "succeeded" and row(rid_late)["error"] is None)

        dead_rows = []
        for i in range(24):
            dead_rows.append(open_row(f"t.many{i}", fake("many", pid=4_000_030)))
        barrier = threading.Barrier(6)
        counts: list[int] = []

        def reconcile_thread() -> None:
            barrier.wait()
            counts.append(audit.reconcile_orphans(fixed(owner.DEAD)))

        ts = [threading.Thread(target=reconcile_thread) for _ in range(6)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        check("I3 six reconcilers started together mark each of 24 rows exactly once", sum(counts) == 24 and len(counts) == 6,
              str(counts))

        stress = [open_row(f"t.stress{i}", fake("stress", pid=4_000_040)) for i in range(40)]
        barrier2 = threading.Barrier(5)

        def completer(ids: list[int]) -> None:
            barrier2.wait()
            for rid in ids:
                audit.complete(rid, ok=True, result="Done.", duration_ms=1)

        def reconciler() -> None:
            barrier2.wait()
            for _ in range(30):
                audit.reconcile_orphans(fixed(owner.DEAD))

        ts = [threading.Thread(target=completer, args=(stress[0::2],)), threading.Thread(target=completer, args=(stress[1::2],)),
              threading.Thread(target=reconciler), threading.Thread(target=reconciler), threading.Thread(target=reconciler)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        finals = [row(r) for r in stress]
        check("I4 with completions and reconcilers racing, every row ends with its real outcome",
              all(f["status"] == "succeeded" and f["result"] == "Done." for f in finals), str([f["status"] for f in finals]))
        check("I5 and none has a real outcome AND a recovery marker", not any(f["ok"] is not None and f["error"] == audit.INTERRUPTED
                                                                          for f in finals))

        # ------------------------------------------------------------------------------------------------
        scenario("J. the Executor binds ownership before the side effect, and keeps its semantics")
        RAN.clear()
        res = asyncio.run(EXECUTOR.run("test.work52", {}))
        r1 = next(r for r in audit.recent(5) if r["skill"] == "test.work52")
        check("J1 a normal run succeeds, its row has this session as owner and a recorded outcome",
              res.ok and r1["status"] == "succeeded" and r1["session_id"] == owner.identity().id and RAN == ["work"])
        check("J2 its undo entry is still registered", store.conn().execute(
            "SELECT COUNT(*) FROM undo WHERE audit_id=?", (r1["id"],)).fetchone()[0] == 1)
        SEEN.clear()
        asyncio.run(EXECUTOR.run("test.peek52", {}))
        check("J3 while the skill body runs, its open row already names a recorded session with this pid",
              len(SEEN) == 1 and SEEN[0] == os.getpid(), str(SEEN))
        RAN.clear()
        try:
            asyncio.run(EXECUTOR.run("test.boom52", {}))
        except RuntimeError:
            pass
        rb = next(r for r in audit.recent(5) if r["skill"] == "test.boom52")
        check("J4 a skill that raises is a recorded failure of this session, never a recovery marker",
              rb["status"] == "failed" and rb["session_id"] == owner.identity().id and rb["error"] == "it broke")

        async def decline(skill, args, preview):
            return False

        EXECUTOR.set_confirm_handler(decline)
        RAN.clear()
        rd = asyncio.run(EXECUTOR.run("test.confirm52", {}))
        rdn = next(r for r in audit.recent(5) if r["skill"] == "test.confirm52")
        check("J5 a declined confirmation is a denied row (owner recorded, skill never ran) and recovery cannot change it",
              not rd.ok and rdn["status"] == "denied" and rdn["session_id"] == owner.identity().id and RAN == []
              and audit.reconcile_orphans(fixed(owner.DEAD)) in (0,) and row(rdn["id"])["status"] == "denied")

        async def approve(skill, args, preview):
            return True

        EXECUTOR.set_confirm_handler(approve)
        asyncio.run(EXECUTOR.run("test.confirm52", {}))
        rc = next(r for r in audit.recent(5) if r["skill"] == "test.confirm52")
        check("J6 an approved confirmation is a confirmed row with an owner", rc["decision"] == "confirmed"
              and rc["status"] == "succeeded" and rc["session_id"] == owner.identity().id)

        scenario("K. a session that cannot be recorded does not get to run anything")
        rows_before = store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        real_ensure = owner.ensure_session

        def failing(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        owner.ensure_session = failing  # type: ignore[assignment]
        RAN.clear()
        try:
            err = None
            try:
                asyncio.run(EXECUTOR.run("test.work52", {}))
            except Exception as exc:  # noqa: BLE001
                err = exc
            check("K1 Executor.run raises OwnerError and the skill body never ran", isinstance(err, owner.OwnerError) and RAN == [], repr(err))
            check("K2 no audit row was left behind", store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == rows_before)
            blocked_id = audit.record(actor="text", skill="t.blocked_no_owner", tier="L3", args={}, decision="blocked")
            check("K3 a blocked/denied row is still written (it never ran), just without an owner",
                  row(blocked_id)["status"] == "blocked" and row(blocked_id)["session_id"] is None)
            audit.start_session("cli")  # must not raise even though registering fails
            check("K4 start_session itself does not take a startup down", True)
            err = None
            try:
                audit.record(actor="text", skill="t.confirmed", tier="L2", args={}, decision="confirmed")
            except Exception as exc:  # noqa: BLE001
                err = exc
            check("K5 a confirmed action is refused too", isinstance(err, owner.OwnerError))
        finally:
            owner.ensure_session = real_ensure  # type: ignore[assignment]
        asyncio.run(EXECUTOR.run("test.work52", {}))
        check("K6 once the session can be recorded again, actions run", RAN[-1:] == ["work"])

        # ------------------------------------------------------------------------------------------------
        scenario("L. entry points record their session and reconcile (GUI backend, daemon, CLI)")
        import friday.daemon as daemon
        from friday import jobs, triggers
        from friday.gui import backend as backend_mod

        async def nothing(*a, **k):
            return None

        saved_patches = (backend_mod.BRAIN.warm, jobs.SCHEDULER.start, triggers.WATCHER.start, jobs.SCHEDULER.stop,
                         triggers.WATCHER.stop, backend_mod.setup, cli.setup)
        backend_mod.BRAIN.warm = lambda *a, **k: None  # type: ignore[method-assign]
        jobs.SCHEDULER.start = lambda *a, **k: None  # type: ignore[method-assign]
        triggers.WATCHER.start = lambda *a, **k: None  # type: ignore[method-assign]
        jobs.SCHEDULER.stop = lambda *a, **k: None  # type: ignore[method-assign]
        triggers.WATCHER.stop = lambda *a, **k: None  # type: ignore[method-assign]
        backend_mod.setup = lambda *a, **k: None  # type: ignore[assignment]
        cli.setup = lambda *a, **k: None  # type: ignore[assignment]
        try:
            for kind_name in ("gui", "daemon", "cli"):
                gone = fake(f"entry-{kind_name}", pid=4_000_050 + len(kind_name))
                orphan = open_row(f"t.orphan_{kind_name}", gone)
                store.conn().execute("UPDATE runtime_sessions SET pid=? WHERE id=?", (gone.pid, gone.id))
                store.conn().commit()
                ident = fake(f"boot-{kind_name}", pid=os.getpid(), start=owner.identity().proc_start)
                # the entry point's reconciliation uses the real probe: pid 4_00x_xxx does not exist, so its owner is dead
                with owner._use_identity(ident):
                    if kind_name == "gui":
                        asyncio.run(backend_mod.Backend()._boot())
                    elif kind_name == "daemon":
                        async def lifecycle() -> None:
                            async with daemon.lifespan(daemon.app):
                                pass

                        asyncio.run(lifecycle())
                    else:
                        asyncio.run(cli._boot(quiet=True))
                srow = next((s for s in sessions() if s["id"] == ident.id), None)
                check(f"L-{kind_name}1 the {kind_name} start recorded its session with that kind",
                      srow is not None and srow["kind"] == kind_name, str(srow))
                check(f"L-{kind_name}2 and reconciled an action left open by a dead process",
                      row(orphan)["status"] == "interrupted")
        finally:
            (backend_mod.BRAIN.warm, jobs.SCHEDULER.start, triggers.WATCHER.start, jobs.SCHEDULER.stop,
             triggers.WATCHER.stop, backend_mod.setup, cli.setup) = saved_patches

        # ------------------------------------------------------------------------------------------------
        scenario("M. how the existing views present the rows (status contract and redaction unchanged)")
        add_secret = open_row("input.type", fake("view", pid=4_000_070), args={"text": SECRET})
        audit.reconcile_orphans(fixed(owner.DEAD))
        buf = io.StringIO()
        saved_con = cli.con
        cli.con = Console(file=buf, width=220, force_terminal=False, color_system=None)
        try:
            cli._audit_table(300)
        finally:
            cli.con = saved_con
        table = buf.getvalue()
        interrupted_line = next((ln for ln in table.splitlines() if "input.type" in ln), "")
        check("M1 the CLI table marks an interrupted row with '?' and says so in words",
              "?" in interrupted_line and "interrupted" in interrupted_line, interrupted_line)
        check("M2 the table never shows the redacted content or a session id", SECRET not in table and "fake-" not in table)
        spoken = meta.recent(count=300)
        check("M3 the spoken history says the action is interrupted with an unknown outcome",
              "input.type (interrupted, outcome unknown)" in spoken.speech and SECRET not in spoken.speech)
        served = asyncio.run(daemon.recent_audit(limit=300))
        check("M4 the daemon's /audit rows keep a derived status and stay redacted",
              next(r for r in served if r["id"] == add_secret)["status"] == "interrupted" and SECRET not in json.dumps(served))
        statuses = {audit.status_of(r) for r in served}
        check("M5 every status category the machine readers rely on is still produced",
              {"succeeded", "failed", "denied", "blocked", "outcome_unknown", "interrupted", "unfinished"} <= statuses, str(statuses))
        check("M6 no new status word appeared", statuses <= {"succeeded", "failed", "denied", "blocked", "outcome_unknown",
                                                              "interrupted", "unfinished"})

    # ----------------------------------------------------------------------------------------------------
    scenario("N. migration: an old database (no session column, no sessions table) is upgraded in place and safely")
    with iso_env.isolated_runtime():
        old_path = str(paths.DB_PATH)
        raw = sqlite3.connect(old_path)
        raw.execute("PRAGMA journal_mode=WAL")  # as the real database always is
        raw.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, skill TEXT NOT NULL,"
            " tier TEXT NOT NULL, args TEXT NOT NULL, decision TEXT NOT NULL, ok INTEGER, result TEXT, error TEXT, duration_ms INTEGER);"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-01','text','a.done','L0','{}','auto',1,'ok');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision) VALUES ('2025-01-02','text','a.open','L1','{}','auto');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision) VALUES ('2025-01-03','text','a.denied','L1','{}','denied');"
        )
        raw.commit()
        raw.close()
        errors: list[BaseException] = []
        barrier3 = threading.Barrier(4)

        def opener() -> None:
            try:
                barrier3.wait()
                store.conn().execute("SELECT 1").fetchone()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=opener) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        cols = {r[1] for r in store.conn().execute("PRAGMA table_info(audit)")}
        check("N1 four connections opening the old database at once all succeed", not errors, repr(errors))
        check("N2 the owner column and the sessions table now exist, plus an index",
              {"session_id", "goal_id"} <= cols and store.conn().execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0] == 0
              and store.conn().execute("SELECT COUNT(*) FROM sqlite_master WHERE name='idx_audit_session'").fetchone()[0] == 1)
        legacy_rows = audit.recent(10)
        check("N3 the old rows are unchanged, have no owner, and keep their statuses",
              [(r["skill"], r["status"], r["session_id"]) for r in reversed(legacy_rows)] ==
              [("a.done", "succeeded", None), ("a.open", "unfinished", None), ("a.denied", "denied", None)])
        check("N4 reconciling an old database changes nothing", audit.reconcile_orphans(fixed(owner.DEAD)) == 0
              and audit.recent(10) == legacy_rows)
        store.conn().close()
        store._local.conns.clear()
        again = store.conn()
        check("N5 re-opening (the migration running again) is a no-op", {r[1] for r in again.execute("PRAGMA table_info(audit)")} == cols
              and audit.recent(10) == legacy_rows)

    return finish("Phase 52 -- process-aware ownership and conservative audit recovery", time.perf_counter() - t0,
                  min_assertions=65, min_scenarios=13)


if __name__ == "__main__":
    sys.exit(main())
