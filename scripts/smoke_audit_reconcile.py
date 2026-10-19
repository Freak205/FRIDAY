"""Phase 51 -- open audit rows that can only belong to a process that no longer exists are labelled `interrupted`.

The audit log has no per-row owner and several FRIDAY processes may share it, so an open row is not by itself evidence of a
dead process. The one reliable fact is the operating-system boot: no process survives a reboot, so a started row written
before the last boot is stale; anything newer is left `unfinished`. This suite drives `audit.reconcile_stale` with an injected
boot time and synthetic rows on a throwaway database. Nothing is executed, retried or undone, and nothing real is read.
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, cli, store  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.skills import meta  # noqa: E402

SECRET = "hunter2-SECRET-body"


def ts(offset_s: float, boot: float) -> str:
    return datetime.fromtimestamp(boot + offset_s, timezone.utc).isoformat()


def add(skill: str, at: str, *, decision: str = "auto", ok: bool | None = None, error: str | None = None,
        result: str | None = None, unknown: bool = False, args: dict | None = None) -> int:
    rid = audit.record(actor="text", skill=skill, tier="L1", args=args or {}, decision=decision)
    if ok is not None:
        audit.complete(rid, ok=ok, result=result, error=error, duration_ms=2)
    elif unknown:
        assert audit.mark_outcome_unknown(rid)
    c = store.conn()
    c.execute("UPDATE audit SET at=? WHERE id=?", (at, rid))
    c.commit()
    return rid


def snapshot() -> dict[str, tuple]:
    return {r["skill"]: (r["ok"], r["result"], r["error"], r["decision"], r["status"]) for r in audit.recent(100)}


def main() -> int:
    t0 = time.perf_counter()
    boot = time.time() - 3 * 3600  # pretend the machine last booted three hours ago
    with iso_env.isolated_runtime():
        scenario("A. which rows are stale, and which are left alone")
        add("t.old_open", ts(-600, boot))
        add("t.old_open_confirmed", ts(-3600, boot), decision="confirmed")
        add("input.type", ts(-1800, boot), args={"text": SECRET})
        add("t.new_open", ts(+600, boot))
        add("t.margin_open", ts(-30, boot))  # inside the 60 s margin before boot: not touched
        add("t.old_success", ts(-600, boot), ok=True, result="Done.")
        add("t.old_failed", ts(-600, boot), ok=False, error="it failed")
        add("t.old_denied", ts(-600, boot), decision="denied")
        add("t.old_blocked", ts(-600, boot), decision="blocked")
        add("t.old_unknown", ts(-600, boot), unknown=True)
        before = snapshot()
        audit_rows_before = store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0]

        ran: list[str] = []
        real_run = EXECUTOR.run

        async def spy(*a, **k):  # reconciliation must never execute anything
            ran.append(str(a))
            return await real_run(*a, **k)

        EXECUTOR.run = spy  # type: ignore[method-assign]
        try:
            n = audit.reconcile_stale(boot)
        finally:
            EXECUTOR.run = real_run  # type: ignore[method-assign]
        after = snapshot()
        check("A1 exactly the three stale open rows were marked", n == 3, str(n))
        for name in ("t.old_open", "t.old_open_confirmed", "input.type"):
            check(f"A-{name} is now interrupted, with ok still NULL and no result",
                  after[name][4] == "interrupted" and after[name][0] is None and after[name][1] is None, str(after[name]))
        check("A2 a row from this boot (the current process's own work) stays unfinished",
              after["t.new_open"][4] == "unfinished" and after["t.new_open"] == before["t.new_open"], str(after["t.new_open"]))
        check("A3 a row just inside the margin before boot stays unfinished", after["t.margin_open"][4] == "unfinished")
        check("A4 success and failure are untouched", after["t.old_success"] == before["t.old_success"]
              and after["t.old_failed"] == before["t.old_failed"])
        check("A5 denied and blocked rows are untouched and never become interrupted",
              after["t.old_denied"] == before["t.old_denied"] and after["t.old_blocked"] == before["t.old_blocked"]
              and {after["t.old_denied"][4], after["t.old_blocked"][4]} == {"denied", "blocked"})
        check("A6 the graceful-shutdown marker is not replaced: still outcome_unknown",
              after["t.old_unknown"] == before["t.old_unknown"] and after["t.old_unknown"][4] == "outcome_unknown")
        check("A7 nothing was executed, retried or undone, and no row or undo entry was added",
              ran == [] and store.conn().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == audit_rows_before
              and store.conn().execute("SELECT COUNT(*) FROM undo").fetchone()[0] == 0)

        scenario("B. idempotent and safe to repeat")
        check("B1 a second run changes nothing", audit.reconcile_stale(boot) == 0 and snapshot() == after)

        scenario("C. the two different 'unknown' situations stay different")
        check("C1 a graceful-shutdown row and a crash-left row have different, stable statuses",
              after["t.old_unknown"][4] == "outcome_unknown" and after["t.old_open"][4] == "interrupted")
        check("C2 their markers differ and neither helper claims the other's row",
              audit.OUTCOME_UNKNOWN != audit.INTERRUPTED
              and audit.is_interrupted({"ok": None, "error": audit.OUTCOME_UNKNOWN}) is False
              and audit.is_outcome_unknown({"ok": None, "error": audit.INTERRUPTED}) is False)
        check("C3 a real failure that only mentions the words is a failure",
              audit.status_of({"decision": "auto", "ok": 0, "error": "interrupted: server said so"}) == "failed")

        scenario("D. guards: no boot time, an implausible one, the real default, and a late real outcome")
        add("t.o", ts(-600, boot))
        check("D1 an implausible boot time (in the future) changes nothing", audit.reconcile_stale(time.time() + 99999) == 0)
        check("D2 a zero/negative boot time changes nothing", audit.reconcile_stale(0.0) == 0 and audit.reconcile_stale(-5.0) == 0)
        import psutil

        saved = psutil.boot_time

        def broken() -> float:
            raise OSError("no boot time")

        psutil.boot_time = broken  # type: ignore[assignment]
        try:
            check("D3 when the boot time cannot be read, rows are left as they are", audit.reconcile_stale() == 0)
        finally:
            psutil.boot_time = saved  # type: ignore[assignment]
        fresh = audit.record(actor="text", skill="t.fresh", tier="L1", args={}, decision="auto")
        check("D4 with the real boot time a row created just now is never touched (the current process's work)",
              audit.reconcile_stale() == 0 and audit.status_of(next(r for r in audit.recent(5) if r["id"] == fresh)) == "unfinished")
        rid = next(r["id"] for r in audit.recent(5) if r["skill"] == "t.o")
        audit.reconcile_stale(boot)
        audit.complete(rid, ok=True, result="Done.", duration_ms=3)
        row = next(r for r in audit.recent(5) if r["id"] == rid)
        check("D5 if the work did finish later, its real outcome replaces the marker", row["status"] == "succeeded" and row["error"] is None)

        scenario("E. how the existing views present it (the rows from A)")
        buf = io.StringIO()
        saved_con = cli.con
        cli.con = Console(file=buf, width=220, force_terminal=False, color_system=None)
        try:
            cli._audit_table(100)
        finally:
            cli.con = saved_con
        lines = {k: ln for ln in buf.getvalue().splitlines() for k in ("t.old_open ", "t.old_unknown", "t.new_open") if k in ln}
        check("E1 the CLI table marks an interrupted row with '?' and says so in words",
              "?" in lines.get("t.old_open ", "") and "interrupted" in lines.get("t.old_open ", ""), str(lines))
        check("E2 ...differently from the graceful-shutdown row", "outcome unknown" in lines.get("t.old_unknown", "")
              and "interrupted" not in lines.get("t.old_unknown", ""))
        check("E3 ...and a current unfinished row stays blank", "?" not in lines.get("t.new_open", "") and "interrupted" not in lines.get("t.new_open", ""))
        check("E4 the table never shows the redacted content", SECRET not in buf.getvalue())
        add("t.last_interrupted", ts(-100, boot))
        audit.reconcile_stale(boot)
        spoken = meta.recent(count=5)
        check("E5 the spoken history says an interrupted action's outcome is unknown, in its own words",
              "t.last_interrupted (interrupted, outcome unknown)" in spoken.speech and SECRET not in spoken.speech, spoken.speech)
        check("E6 meta.recent's structured rows carry status 'interrupted' for it",
              next(r for r in spoken.data["actions"] if r["skill"] == "t.last_interrupted")["status"] == "interrupted")
        import asyncio

        from friday import daemon

        served = asyncio.run(daemon.recent_audit(limit=100))
        check("E7 the daemon's /audit handler exposes the same status and stays redacted",
              next(r for r in served if r["skill"] == "t.last_interrupted")["status"] == "interrupted"
              and SECRET not in json.dumps(served))

    return finish("Phase 51 -- reconciling crash-interrupted audit rows", time.perf_counter() - t0, min_assertions=26, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
