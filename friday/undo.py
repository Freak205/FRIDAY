"""The undo journal.

Skills declare how to reverse themselves via the `undo=` argument on `@skill`.
The executor records the inverse operation after a successful call; this module
replays it. Without this, "reversible write" in the tier table is a claim rather
than a guarantee.
"""

from __future__ import annotations

import json
from typing import Any

from friday import store
from friday.log import get

log = get(__name__)


def pending(limit: int = 10) -> list[dict[str, Any]]:
    """Most recent un-applied inverse operations, newest first."""
    rows = store.conn().execute(
        "SELECT u.*, a.args AS original_args "
        "FROM undo u LEFT JOIN audit a ON a.id = u.audit_id "
        "WHERE u.applied = 0 ORDER BY u.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_applied(undo_id: int) -> None:
    c = store.conn()
    c.execute("UPDATE undo SET applied = 1 WHERE id = ?", (undo_id,))
    c.commit()


async def undo_last(count: int = 1) -> list[str]:
    """Reverse the last `count` undoable actions. Returns human-readable lines."""
    from friday.permissions import EXECUTOR

    entries = pending(limit=count)
    if not entries:
        return []

    done: list[str] = []
    for entry in entries:
        try:
            inverse = json.loads(entry["inverse"])
        except Exception:
            log.exception("undo entry %s has malformed inverse", entry["id"])
            continue

        skill_name = inverse.get("skill")
        args = inverse.get("args", {})
        if not skill_name:
            continue

        try:
            # actor="undo" so reversals are distinguishable in the audit log.
            result = await EXECUTOR.run(skill_name, args, actor="undo")
            mark_applied(entry["id"])
            done.append(f"Reversed {entry['skill']} — {result.speech}")
        except Exception as exc:
            log.exception("failed to undo %s", entry["skill"])
            done.append(f"Couldn't reverse {entry['skill']}: {exc}")

    return done
