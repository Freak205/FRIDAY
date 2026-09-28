"""Long-term memory: things FRIDAY knows about you.

Each memory is a sentence plus its embedding, so recall works by meaning rather
than keyword — "what's my sister's name" finds "Priya is my sister" without the
word "name" appearing anywhere.

Kinds:
    fact        something true about you or your world
    preference  how you like things done
    procedure   how to do a task you've taught it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from friday import store
from friday.log import get

log = get(__name__)

KINDS = ("fact", "preference", "procedure")


@dataclass(slots=True)
class Recalled:
    id: int
    kind: str
    key: str | None
    value: str
    score: float
    at: str


def _embed_one(text: str) -> bytes:
    from friday.brain.matcher import embed

    vector = embed([text])[0].astype(np.float32)
    return vector.tobytes()


def remember(
    value: str,
    kind: str = "fact",
    key: str | None = None,
    source: str = "user",
) -> int:
    """Store a memory. Replaces an existing one with the same key."""
    value = value.strip()
    if not value:
        raise ValueError("empty memory")
    if kind not in KINDS:
        kind = "fact"

    blob = _embed_one(value)
    c = store.conn()

    if key:
        c.execute("DELETE FROM memory WHERE key = ? AND kind = ?", (key, kind))

    cur = c.execute(
        "INSERT INTO memory (at, kind, key, value, embedding, source) "
        "VALUES (?,?,?,?,?,?)",
        (store.now(), kind, key, value, blob, source),
    )
    c.commit()
    log.info("remembered (%s): %s", kind, value[:60])
    return int(cur.lastrowid)


def recall(query: str, k: int = 5, kind: str | None = None) -> list[Recalled]:
    """Find the memories most similar in meaning to `query`."""
    from friday.brain.matcher import embed

    sql = "SELECT * FROM memory WHERE embedding IS NOT NULL"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)

    rows = store.conn().execute(sql, params).fetchall()
    if not rows:
        return []

    vectors = np.stack([
        np.frombuffer(r["embedding"], dtype=np.float32) for r in rows
    ])
    scores = vectors @ embed([query])[0]

    ranked = sorted(zip(scores, rows), key=lambda pair: -pair[0])[:k]
    return [
        Recalled(
            id=row["id"], kind=row["kind"], key=row["key"],
            value=row["value"], score=float(score), at=row["at"],
        )
        for score, row in ranked
    ]


def all_memories(kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT id, at, kind, key, value, source FROM memory"
    params: list[Any] = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in store.conn().execute(sql, params).fetchall()]


def forget(memory_id: int) -> bool:
    c = store.conn()
    cur = c.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
    c.commit()
    return cur.rowcount > 0


def forget_matching(query: str, threshold: float = 0.75) -> list[str]:
    """Delete memories that closely match `query`. Returns what was removed."""
    hits = [r for r in recall(query, k=5) if r.score >= threshold]
    removed = []
    for hit in hits:
        if forget(hit.id):
            removed.append(hit.value)
    return removed


def context_block(query: str, k: int = 3, threshold: float = 0.5) -> str:
    """Relevant memories as plain text, for injecting into a response."""
    hits = [r for r in recall(query, k=k) if r.score >= threshold]
    return "\n".join(f"- {h.value}" for h in hits)
