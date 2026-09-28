"""Episodic experience memory.

One row per meaningful task (today: every `plan.run` invocation) recording
what was asked, what bounded context it saw, what it did, and how it turned
out — the raw material Phase 10's brief calls "training data foundation."
Stored in the same SQLite database as everything else (see `friday.store`'s
`episodes` table), embedded with the same `bge-small-en` model
`friday.memory`/`friday.knowledge` already load, so a future goal can
retrieve a handful of similar past experiences as guidance (never as a
script to blindly replay — see `retrieve_similar`'s docstring).

Secrets are never persisted: `_sanitize_args` redacts any argument whose
name looks like a credential before the plan is serialized to JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from friday import store
from friday.log import get
from friday.orchestrator import Observation

log = get(__name__)

# Argument-name substrings that mean "never write this value to disk in
# plain text." Matches friday.risk's bias toward false positives: better to
# redact something benign named "token_count" than to leak a real secret.
_SECRET_KEY_MARKERS = (
    "password", "passwd", "token", "secret", "otp", "pin", "api_key",
    "apikey", "auth", "credential", "cookie", "session_id",
)


@dataclass(slots=True)
class Episode:
    id: int
    at: str
    goal_id: str | None
    goal_text: str
    context: str
    plan: list[dict[str, Any]]
    stopped: str
    success: bool
    errors: str
    duration_ms: int

    @staticmethod
    def from_row(row) -> "Episode":
        return Episode(
            id=row["id"], at=row["at"], goal_id=row["goal_id"], goal_text=row["goal_text"],
            context=row["context"] or "", plan=json.loads(row["plan"] or "[]"),
            stopped=row["stopped"], success=bool(row["success"]),
            errors=row["errors"] or "", duration_ms=row["duration_ms"] or 0,
        )


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            out[key] = "[redacted]"
        else:
            out[key] = value
    return out


def _embed_one(text: str) -> bytes | None:
    try:
        from friday.brain.matcher import embed

        vector = embed([text])[0].astype(np.float32)
        return vector.tobytes()
    except Exception:
        log.exception("episodes: embedding failed; storing without one")
        return None


def record(
    goal_text: str,
    *,
    goal_id: str | None,
    context: str,
    steps: list[Observation],
    stopped: str,
    ok: bool,
    duration_ms: int,
) -> int:
    """Persist one episode. `steps` are the orchestrator's own observations —
    args are sanitized, everything else is kept as-is (bounded already by
    the orchestrator's own per-observation speech truncation)."""
    plan = [
        {
            "tool": o.step.tool, "args": _sanitize_args(o.step.args),
            "ok": o.ok, "error": o.error,
        }
        for o in steps
    ]
    errors = "; ".join(o.speech for o in steps if not o.ok)[:500]

    c = store.conn()
    cur = c.execute(
        "INSERT INTO episodes (at, goal_id, goal_text, context, plan, stopped, success, "
        "errors, duration_ms, embedding) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            store.now(), goal_id, goal_text.strip(), context[:500],
            json.dumps(plan, default=str, ensure_ascii=False),
            stopped, 1 if ok else 0, errors, duration_ms, _embed_one(goal_text),
        ),
    )
    c.commit()
    log.info("episode recorded: goal_id=%s stopped=%s ok=%s", goal_id, stopped, ok)
    return int(cur.lastrowid)


def recent(limit: int = 20) -> list[Episode]:
    rows = store.conn().execute(
        "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [Episode.from_row(r) for r in rows]


def all_episodes() -> list[Episode]:
    """Every recorded episode, oldest first. Used by the training-data
    exporter (scripts/export_training_data.py) — real recorded experiences
    only, never a sampled/limited view."""
    rows = store.conn().execute("SELECT * FROM episodes ORDER BY id ASC").fetchall()
    return [Episode.from_row(r) for r in rows]


def _rank_by_similarity(rows: list, goal_text: str, threshold: float, k: int) -> list[Episode]:
    """Shared cosine-similarity ranking used by every `retrieve_similar*`
    query below — one embedding call, brute-force `numpy` matmul over
    whatever rows the caller already filtered by SQL, thresholded and
    capped. No ANN index; fine at this scale (see module docstring)."""
    if not rows:
        return []
    from friday.brain.matcher import embed

    vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    scores = vectors @ embed([goal_text])[0]

    ranked = sorted(zip(scores, rows), key=lambda pair: -pair[0])
    return [Episode.from_row(row) for score, row in ranked if score >= threshold][:k]


def retrieve_similar(
    goal_text: str, k: int | None = None, *, success_only: bool = True, threshold: float | None = None,
) -> list[Episode]:
    """Past episodes closest in meaning to `goal_text` — guidance, not a script.

    Callers (e.g. a future planner-context builder) should treat these as
    "here's roughly how a similar goal went last time," never replay the
    stored plan verbatim: arguments, available tools, and the desktop state
    can all have changed since.
    """
    from friday.config import CFG

    k = CFG.intelligence.episode_retrieval_k if k is None else k
    threshold = CFG.intelligence.episode_retrieval_threshold if threshold is None else threshold

    sql = "SELECT * FROM episodes WHERE embedding IS NOT NULL"
    if success_only:
        sql += " AND success = 1"
    rows = store.conn().execute(sql).fetchall()
    return _rank_by_similarity(rows, goal_text, threshold, k)


def retrieve_similar_failures(
    goal_text: str, k: int | None = None, *, threshold: float | None = None,
) -> list[Episode]:
    """Past FAILED episodes closest in meaning to `goal_text`.

    The failure-mode counterpart to `retrieve_similar` — lets a caller
    recognize "something like this was tried before and it didn't work"
    instead of only ever being shown successes. Same guidance-not-script
    caveat applies: a past failure's cause (a missing element, a wrong
    argument) may no longer apply.
    """
    from friday.config import CFG

    k = CFG.intelligence.episode_retrieval_k if k is None else k
    threshold = CFG.intelligence.episode_retrieval_threshold if threshold is None else threshold

    rows = store.conn().execute(
        "SELECT * FROM episodes WHERE embedding IS NOT NULL AND success = 0"
    ).fetchall()
    return _rank_by_similarity(rows, goal_text, threshold, k)
