"""Append-only audit log.

Every skill invocation is recorded before it runs and updated after. If FRIDAY
does something surprising, this is the record that says what, when, with which
arguments, on whose instruction, and whether you approved it.
"""

from __future__ import annotations

from typing import Any

from friday import store
from friday.log import get

log = get(__name__)


def record(
    *,
    actor: str,
    skill: str,
    tier: str,
    args: dict[str, Any],
    decision: str,
) -> int:
    """Write the pre-execution row. Returns the audit id."""
    c = store.conn()
    cur = c.execute(
        "INSERT INTO audit (at, actor, skill, tier, args, decision) VALUES (?,?,?,?,?,?)",
        (store.now(), actor, skill, tier, store.dumps(args), decision),
    )
    c.commit()
    return int(cur.lastrowid)


def complete(
    audit_id: int,
    *,
    ok: bool,
    result: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Fill in the outcome once the skill has run."""
    c = store.conn()
    c.execute(
        "UPDATE audit SET ok=?, result=?, error=?, duration_ms=? WHERE id=?",
        (1 if ok else 0, result, error, duration_ms, audit_id),
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
