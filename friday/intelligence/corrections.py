"""Structured record of user corrections.

When the user tells FRIDAY it misunderstood — "no, I meant my college
project," "that's not what I asked" — that's a signal worth keeping,
separate from the goal/episode it corrects: it's exactly the shape of data a
future fine-tuning pass would need (original interpretation vs. what the
user actually wanted). Nothing here rewrites the goal or re-runs anything
automatically; `friday.session.Session` records the correction and lets
normal intent handling continue on the corrected utterance.
"""

from __future__ import annotations

from dataclasses import dataclass

from friday import auditview, store
from friday.log import get

log = get(__name__)


@dataclass(slots=True)
class Correction:
    id: int
    at: str
    goal_id: str | None
    original_interpretation: str
    user_correction: str
    corrected_objective: str
    context: str

    @staticmethod
    def from_row(row) -> "Correction":
        return Correction(
            id=row["id"], at=row["at"], goal_id=row["goal_id"],
            original_interpretation=row["original_interpretation"] or "",
            user_correction=row["user_correction"],
            corrected_objective=row["corrected_objective"] or "",
            context=row["context"] or "",
        )


_MAX_TEXT = 500


def _keep(text: str) -> str:
    """What is stored of a piece of free text: credential-checked (a failed check stores a notice, never the original) and bounded."""
    return (auditview.scrub_free_text((text or "").strip()) or "")[:_MAX_TEXT]


def record(
    user_correction: str,
    *,
    goal_id: str | None = None,
    original_interpretation: str = "",
    corrected_objective: str = "",
    context: str = "",
) -> int:
    c = store.conn()
    cur = c.execute(
        "INSERT INTO corrections (at, goal_id, original_interpretation, user_correction, "
        "corrected_objective, context) VALUES (?,?,?,?,?,?)",
        (
            store.now(), goal_id, _keep(original_interpretation),
            _keep(user_correction), _keep(corrected_objective), _keep(context),
        ),
    )
    c.commit()
    log.info("correction recorded (goal_id=%s, %d chars)", goal_id, len(user_correction))
    return int(cur.lastrowid)


def recent(limit: int = 20) -> list[Correction]:
    rows = store.conn().execute(
        "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [Correction.from_row(r) for r in rows]


def for_goal(goal_id: str) -> list[Correction]:
    rows = store.conn().execute(
        "SELECT * FROM corrections WHERE goal_id=? ORDER BY id ASC", (goal_id,)
    ).fetchall()
    return [Correction.from_row(r) for r in rows]
