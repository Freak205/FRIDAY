"""SQLite storage. One file, WAL mode, no ORM.

Holds the audit log, the undo journal, learned phrasings, and (from P2) jobs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from friday import paths
from friday.log import get

log = get(__name__)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    actor       TEXT    NOT NULL,   -- voice | text | scheduler | trigger
    skill       TEXT    NOT NULL,
    tier        TEXT    NOT NULL,
    args        TEXT    NOT NULL,   -- json
    decision    TEXT    NOT NULL,   -- auto | confirmed | denied | blocked
    ok          INTEGER,            -- null until executed
    result      TEXT,
    error       TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS idx_audit_skill ON audit(skill);

CREATE TABLE IF NOT EXISTS undo (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    audit_id  INTEGER,
    skill     TEXT NOT NULL,
    inverse   TEXT NOT NULL,        -- json: {skill, args}
    applied   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (audit_id) REFERENCES audit(id)
);

CREATE TABLE IF NOT EXISTS phrasings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    intent  TEXT NOT NULL,          -- skill name
    text    TEXT NOT NULL,
    source  TEXT NOT NULL           -- builtin | taught
);
CREATE INDEX IF NOT EXISTS idx_phrasings_intent ON phrasings(intent);

CREATE TABLE IF NOT EXISTS memory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- semantic | procedural | episodic
    key     TEXT,
    value   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    trigger_type TEXT NOT NULL,     -- cron | interval | once | event
    trigger_spec TEXT NOT NULL,     -- json
    actions      TEXT NOT NULL,     -- json: [{skill, args}, ...]
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run     TEXT,
    last_status  TEXT,
    run_count    INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_enabled ON jobs(enabled);

CREATE TABLE IF NOT EXISTS job_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER NOT NULL,
    at       TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    detail   TEXT,
    ms       INTEGER,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id);

CREATE TABLE IF NOT EXISTS kb_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    indexed_at  TEXT NOT NULL,
    mtime       REAL NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL,
    seq       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES kb_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);

-- Phase 10: intelligence layer (goals, episodic experience, corrections).
-- See friday/intelligence/ for the code that reads/writes these.
CREATE TABLE IF NOT EXISTS goals (
    id                TEXT    PRIMARY KEY,   -- uuid4
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    original_request  TEXT    NOT NULL,
    objective         TEXT    NOT NULL,
    status            TEXT    NOT NULL,      -- see friday.intelligence.goals.GoalStatus
    parent_goal_id    TEXT,
    current_step      INTEGER NOT NULL DEFAULT 0,
    success_criteria  TEXT,
    failure_reason    TEXT,
    FOREIGN KEY (parent_goal_id) REFERENCES goals(id)
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_created ON goals(created_at);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    goal_id     TEXT,
    goal_text   TEXT    NOT NULL,
    context     TEXT,             -- bounded working-memory/desktop snippet, plain text
    plan        TEXT    NOT NULL, -- json: [{tool, args, ok, error}, ...], secrets redacted
    stopped     TEXT    NOT NULL, -- Orchestrator StopReason
    success     INTEGER NOT NULL,
    errors      TEXT,
    duration_ms INTEGER,
    embedding   BLOB,             -- goal_text embedding, for experience retrieval
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_goal ON episodes(goal_id);
CREATE INDEX IF NOT EXISTS idx_episodes_at ON episodes(at);

CREATE TABLE IF NOT EXISTS corrections (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    at                      TEXT NOT NULL,
    goal_id                 TEXT,
    original_interpretation TEXT,
    user_correction         TEXT NOT NULL,
    corrected_objective     TEXT,
    context                 TEXT,
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);
CREATE INDEX IF NOT EXISTS idx_corrections_goal ON corrections(goal_id);
"""


# Columns added after the initial schema shipped. CREATE TABLE IF NOT EXISTS
# won't add them to an existing database, so they're applied explicitly.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("memory", "embedding", "ALTER TABLE memory ADD COLUMN embedding BLOB"),
    ("memory", "source", "ALTER TABLE memory ADD COLUMN source TEXT"),
    ("jobs", "notify", "ALTER TABLE jobs ADD COLUMN notify INTEGER NOT NULL DEFAULT 1"),
    # Phase 11.2: bounded subgoal decomposition — see friday.intelligence.goals.
    ("goals", "subgoals", "ALTER TABLE goals ADD COLUMN subgoals TEXT"),
    ("goals", "current_subgoal_index", "ALTER TABLE goals ADD COLUMN current_subgoal_index INTEGER NOT NULL DEFAULT 0"),
    # Phase 17.0: Goal Contract (intent/unknowns/success-conditions/etc.) —
    # see friday.intelligence.goals.GoalContract. NULL on old rows deserializes
    # to GoalContract() defaults (Goal.from_row), fully backward compatible.
    ("goals", "goal_contract", "ALTER TABLE goals ADD COLUMN goal_contract TEXT"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, column, statement in MIGRATIONS:
        try:
            existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if existing and column not in existing:
            try:
                c.execute(statement)
                log.info("migrated: %s.%s", table, column)
            except sqlite3.Error:
                log.exception("migration failed for %s.%s", table, column)
    c.commit()


def conn() -> sqlite3.Connection:
    """Thread-local connection. SQLite objects aren't shareable across threads."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        return existing

    paths.ensure()
    c = sqlite3.connect(paths.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    c.commit()
    _migrate(c)
    _local.conn = c
    return c


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def init() -> None:
    conn()
    log.info("store ready at %s", paths.DB_PATH)


@contextlib.contextmanager
def use_temp_db():
    """Phase 14.0 — test-only escape hatch: redirect the store to a
    throwaway SQLite file for the duration of the block, restoring the real
    path/connection afterward.

    Nothing in `paths.DB_PATH`/`conn()` supports redirection on its own
    (every smoke test before this phase ran against the real, shared,
    ever-growing `data/friday.db` — see PLAN.md Phase 12.0/13.0's
    documented top-k-crowding flakiness this fixes). Production code never
    calls this. Only the calling thread's connection is affected —
    `conn()`'s thread-local caching means this is safe for the
    single-threaded smoke scripts that use it, not a general multi-thread
    guarantee.
    """
    previous_path = paths.DB_PATH
    previous_conn = getattr(_local, "conn", None)
    fd, tmp_path = tempfile.mkstemp(prefix="friday-test-", suffix=".db")
    os.close(fd)
    try:
        paths.DB_PATH = tmp_path
        if hasattr(_local, "conn"):
            del _local.conn
        yield
    finally:
        current = getattr(_local, "conn", None)
        if current is not None:
            try:
                current.close()
            except Exception:
                pass
        if hasattr(_local, "conn"):
            del _local.conn
        paths.DB_PATH = previous_path
        if previous_conn is not None:
            _local.conn = previous_conn
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp_path + suffix)
            except OSError:
                pass
