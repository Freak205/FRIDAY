"""Append-only audit log.

Every skill invocation is recorded before it runs and updated after. If FRIDAY
does something surprising, this is the record that says what, when, with which
arguments, on whose instruction, and whether you approved it.
"""

from __future__ import annotations

import threading
from typing import Any

from friday import auditview, store
from friday.log import get

log = get(__name__)

# audit id -> the raw content values the policy refuses to store, kept in MEMORY only so that `complete` can
# strip an echo of them ("Composed 'hello ...' to Sam") from the stored result. Bounded; never persisted.
_echo: dict[int, list[str]] = {}
_echo_lock = threading.Lock()
_ECHO_CAP = 256


def record(
    *,
    actor: str,
    skill: str,
    tier: str,
    args: dict[str, Any],
    decision: str,
) -> int:
    """Write the pre-execution row. Returns the audit id.

    Phase 35: `args` is stored in the field-aware form of `friday.auditview` -- what was touched and with which
    non-content options, never the body of a file, a typed text, a message or code."""
    c = store.conn()
    cur = c.execute(
        "INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES (?,?,?,?,?,?)",
        (store.now(), actor, skill, tier, store.dumps(auditview.sanitize_args(skill, args)), decision),
    )
    c.commit()
    audit_id = int(cur.lastrowid)
    values = auditview.content_values(skill, args)
    if values:
        with _echo_lock:
            _echo[audit_id] = values
            while len(_echo) > _ECHO_CAP:
                _echo.pop(next(iter(_echo)))
    return audit_id


def complete(
    audit_id: int,
    *,
    ok: bool,
    result: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Fill in the outcome once the skill has run. `ok` is the skill's own verdict, passed through untouched;
    only the free text (what it said, what went wrong) is minimised before it is stored."""
    c = store.conn()
    row = c.execute("SELECT skill FROM audit WHERE id=?", (audit_id,)).fetchone()
    skill = row["skill"] if row is not None else ""
    with _echo_lock:
        echoes = _echo.pop(audit_id, [])
    c.execute(
        "UPDATE audit SET ok=?, result=?, error=?, duration_ms=? WHERE id=?",
        (1 if ok else 0, auditview.sanitize_result(skill, result, echoes), auditview.sanitize_error(error, echoes),
         duration_ms, audit_id),
    )
    c.commit()


# Phase 48: the text of a row whose skill was still running when FRIDAY closed. `ok` stays NULL on purpose -- it did not
# fail (it may well have succeeded) and nothing says it succeeded -- and no undo entry exists for it, because its result
# (and so its inverse) was never seen. The words are fixed; they carry nothing from the request.
OUTCOME_UNKNOWN = (
    "outcome unknown: FRIDAY was closed while this was still running, so whether it finished was not recorded "
    "(check the result yourself; nothing was registered to undo it)"
)


# Phase 51: the text of a row that a PREVIOUS process left open and that can only belong to a process that no longer exists.
# Distinct from OUTCOME_UNKNOWN (an orderly quit that saw the work still running): here nobody saw anything. `ok` stays NULL
# -- it may have finished, failed or never run -- and nothing is retried, undone or registered for undo.
INTERRUPTED = (
    "interrupted: the process that started this ended before recording an outcome (crash, kill or restart), so whether it "
    "finished is unknown; nothing was retried or undone"
)


def is_interrupted(row: dict[str, Any]) -> bool:
    return row.get("ok") is None and row.get("error") == INTERRUPTED


def reconcile_stale(boot_time: float | None = None, *, margin_s: float = 60.0) -> int:
    """Mark started-but-never-recorded rows that predate the last operating-system boot as `interrupted`; returns how many.

    The audit log has no owner for a row (no pid, no session), several FRIDAY processes (GUI, daemon, CLI) may share the
    database, and only the GUI holds a lock -- so an open row is NOT evidence of a dead process, and age alone proves
    nothing (a worker thread may legitimately run for minutes). The one fact that is: no process survives a reboot. A row
    written before the machine last booted therefore belongs to a process that is gone. Rows from after that boot, including
    those of a crashed process, are left `unfinished` rather than guessed at.

    Only rows with a started decision (auto/confirmed) and no ok/result/error are touched, so a recorded outcome, a
    denied/blocked row and an earlier marker are never overwritten, and running it again changes nothing. A live process
    that (contrary to the above, e.g. after a large clock change) later completes its row simply replaces the marker.
    No boot time (psutil missing, or a value that is not plausible) means no change."""
    import time
    from datetime import datetime, timedelta, timezone

    if boot_time is None:
        try:
            import psutil

            boot_time = float(psutil.boot_time())
        except Exception:
            log.exception("boot time unavailable; open audit rows are left as they are")
            return 0
    if not (0 < boot_time <= time.time()):
        return 0
    cutoff = (datetime.fromtimestamp(boot_time, timezone.utc) - timedelta(seconds=margin_s)).isoformat()
    c = store.conn()
    cur = c.execute(
        "UPDATE audit SET error=? WHERE ok IS NULL AND result IS NULL AND error IS NULL "
        "AND decision IN ('auto','confirmed') AND at < ?",
        (INTERRUPTED, cutoff),
    )
    c.commit()
    n = int(cur.rowcount or 0)
    if n:
        log.warning("audit: %d action(s) left open before the last system start are marked interrupted", n)
    return n


def is_outcome_unknown(row: dict[str, Any]) -> bool:
    """Phase 49: is this audit row (as `recent` returns it) one that `mark_outcome_unknown` marked, and that nothing has
    since given a real outcome? The marker must match exactly, so a genuine error that merely mentions the words is not it."""
    return row.get("ok") is None and row.get("error") == OUTCOME_UNKNOWN


def mark_outcome_unknown(audit_id: int) -> bool:
    """Mark a row that started (an `auto`/`confirmed` decision) and has no outcome at all as "outcome unknown".
    True only if it changed something. It never touches a row that already has an outcome or any text, nor one that never
    ran (denied / blocked), so it is safe to repeat; a real `complete` that arrives later simply replaces the marker."""
    c = store.conn()
    cur = c.execute(
        "UPDATE audit SET error=? WHERE id=? AND ok IS NULL AND result IS NULL AND error IS NULL "
        "AND decision IN ('auto','confirmed')",
        (OUTCOME_UNKNOWN, audit_id),
    )
    c.commit()
    return cur.rowcount > 0


def register_undo(audit_id: int, skill: str, inverse: dict[str, Any]) -> None:
    """Record how to reverse an action. `inverse` is {"skill": ..., "args": {...}}."""
    c = store.conn()
    c.execute(
        "INSERT INTO undo (at, audit_id, skill, inverse) VALUES (?,?,?,?)",
        (store.now(), audit_id, skill, store.dumps(inverse)),
    )
    c.commit()


def status_of(row: dict[str, Any]) -> str:
    """Phase 50: where this row stands, as one stable word, derived from its own fields (never stored, never guessed):

    - ``denied`` / ``blocked`` -- the decision; the skill never ran (``ok`` stays NULL);
    - ``succeeded`` / ``failed`` -- the skill's own verdict (``ok`` 1 / 0), exactly as recorded;
    - ``outcome_unknown`` -- it started and was still running when FRIDAY closed (the Phase 48 marker, matched exactly);
    - ``interrupted`` -- it started in an earlier process that ended without recording it (the Phase 51 marker);
    - ``unfinished`` -- it started and nothing has been recorded for it (yet).
    A missing result is never read as success or failure."""
    decision = row.get("decision")
    if decision in ("denied", "blocked"):
        return decision
    ok = row.get("ok")
    if ok is not None:
        return "succeeded" if ok else "failed"
    if is_interrupted(row):
        return "interrupted"
    return "outcome_unknown" if is_outcome_unknown(row) else "unfinished"


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Newest first. Every column as stored, plus a derived ``status`` (see `status_of`) so a machine reader does not have
    to parse ``error``; the daemon's ``GET /audit`` and ``meta.recent``'s ``data.actions`` return these rows."""
    c = store.conn()
    rows = c.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["status"] = status_of(row)
    return out
