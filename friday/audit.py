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


def register_undo(audit_id: int, skill: str, inverse: dict[str, Any]) -> None:
    """Record how to reverse an action. `inverse` is {"skill": ..., "args": {...}}."""
    c = store.conn()
    c.execute(
        "INSERT INTO undo (at, audit_id, skill, inverse) VALUES (?,?,?,?)",
        (store.now(), audit_id, skill, store.dumps(inverse)),
    )
    c.commit()


def recent(limit: int = 20) -> list[dict[str, Any]]:
    c = store.conn()
    rows = c.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
