"""The undo journal.

Skills declare how to reverse themselves via the `undo=` argument on `@skill`, or -- when the
inverse depends on what actually happened (which snapshot of an overwritten file was kept) --
by returning it as `SkillResult.undo`. The executor records the inverse after a SUCCESSFUL
call; this module replays it. Without this, "reversible write" in the tier table is a claim
rather than a mechanism. Undo is still best effort, not a guarantee: it restores the kept copy only
while that copy is in the store and verifies, and it says so when it cannot.

An inverse is `{"skill": ..., "args": {...}}`, or `{"steps": [that, that, ...]}` when one
action needs several reversals (a move that replaced a file: move back, then restore what was
replaced). Every step runs through `EXECUTOR.run`, so reversing is subject to the same
permission, confirmation and audit as doing.

An entry is marked applied only when EVERY step really succeeded. A declined confirmation or a
failed step leaves it pending (with the steps already done removed), so saying "undo" again
resumes; it is never reported or recorded as reversed when it was not.

`applied` values: 0 pending, 1 reversed, -1 given up after repeated failures, -2 superseded by a
resumed reversal, -3 retired by the opt-in undo window (`files.undo_window_days`). Only 0 and -1
keep a recovery copy alive in friday.retention.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


# After this many failed attempts an entry is given up on (applied = -1), so a reversal that can
# never succeed (its snapshot is gone) does not block every older entry behind it forever.
MAX_FAILURES = 3


RETIRED = -3


def retire_older_than(days: int, *, now: datetime | None = None) -> int:
    """Take pending undo entries older than `days` off the undo list for good (applied = -3) and
    return how many. Only friday.retention calls this, and only when the user has configured a
    bounded undo window AND confirmed a cleanup; the default window is "never"."""
    if days <= 0:
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    c = store.conn()
    cur = c.execute("UPDATE undo SET applied = ? WHERE applied = 0 AND at < ?", (RETIRED, cutoff))
    c.commit()
    return int(cur.rowcount or 0)


def mark_applied(undo_id: int) -> None:
    c = store.conn()
    c.execute("UPDATE undo SET applied = 1 WHERE id = ?", (undo_id,))
    c.commit()


def _save(undo_id: int, inverse: dict[str, Any], *, applied: int = 0) -> None:
    c = store.conn()
    c.execute("UPDATE undo SET inverse = ?, applied = ? WHERE id = ?", (store.dumps(inverse), applied, undo_id))
    c.commit()


def _last_id() -> int:
    row = store.conn().execute("SELECT COALESCE(MAX(id), 0) AS n FROM undo").fetchone()
    return int(row["n"])


def _supersede_after(undo_id: int) -> None:
    """Drop the redo rows the steps of an INTERRUPTED reversal registered (applied = -2). The next
    "undo" must resume the interrupted reversal, not redo the half that already happened."""
    c = store.conn()
    c.execute("UPDATE undo SET applied = -2 WHERE id > ? AND applied = 0", (undo_id,))
    c.commit()


def steps_of(inverse: Any) -> list[dict[str, Any]]:
    """The ordered reversal steps of a journal inverse (one step for the classic shape)."""
    if not isinstance(inverse, dict):
        return []
    raw = inverse.get("steps")
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict) and s.get("skill")]
    return [{"skill": inverse["skill"], "args": inverse.get("args", {})}] if inverse.get("skill") else []


async def undo_last(count: int = 1) -> list[str]:
    """Reverse the last `count` undoable actions. Returns human-readable lines: "Reversed ..."
    for what was reversed, "Didn't reverse ..."/"Couldn't reverse ..." for what was not."""
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
            _save(entry["id"], {"unreadable": True}, applied=-1)
            done.append(f"Couldn't reverse {entry['skill']}: its undo record is unreadable.")
            continue

        steps = steps_of(inverse)
        if not steps:
            _save(entry["id"], inverse if isinstance(inverse, dict) else {}, applied=-1)
            continue

        failure = ""
        declined = False
        said: list[str] = []
        mark = _last_id()
        for i, step in enumerate(steps):
            try:
                # actor="undo" so reversals are distinguishable in the audit log.
                result = await EXECUTOR.run(step["skill"], step.get("args", {}), actor="undo")
            except Exception as exc:
                log.exception("failed to undo %s", entry["skill"])
                failure = str(exc)
                break
            if result.data.get("confirmation_declined"):
                declined = True
                break
            if not result.ok:
                failure = result.speech
                break
            said.append(result.speech)
            remaining = steps[i + 1:]
            if remaining:
                _save(entry["id"], {"steps": remaining, "failures": inverse.get("failures", 0)})

        if not failure and not declined:
            mark_applied(entry["id"])
            done.append(f"Reversed {entry['skill']} — {' '.join(said)}")
            continue

        if said:
            _supersede_after(mark)
        if declined:
            done.append(f"Didn't reverse {entry['skill']}: you declined, so it is still on the undo list.")
        else:
            remaining = steps[len(said):]
            failures = int(inverse.get("failures", 0) if isinstance(inverse, dict) else 0) + 1
            gave_up = failures >= MAX_FAILURES
            _save(entry["id"], {"steps": remaining, "failures": failures}, applied=-1 if gave_up else 0)
            done.append(
                f"Couldn't reverse {entry['skill']}: {failure}"
                + (" I've stopped trying; it is no longer on the undo list." if gave_up else "")
            )
        # Older entries may depend on this one having been reversed first: stop here.
        break

    return done
