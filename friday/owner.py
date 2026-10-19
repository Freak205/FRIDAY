"""Phase 52 -- which FRIDAY process started an audited action, and is that process still there?

Several FRIDAY processes (GUI, daemon, CLI, scripts) share one SQLite file, and only the GUI holds a lock. An open audit row is
therefore not evidence of anything by itself, and neither is its age: a skill may legitimately run for minutes. What IS
evidence is the owner: every audited action is bound, in the same transaction that creates its row, to a `runtime_sessions`
row naming the process that started it -- a random per-process session id, the pid, the host, and the process creation time
as the operating system reported it. (pid, creation time) is the identity the OS itself guarantees unique over time, so a
recycled pid is a different process, not the owner.

`probe` answers one question about a recorded identity, with three honest answers:

  ALIVE    a process with that pid exists on this host and its creation time matches the record;
  DEAD     no such process exists, or the pid now belongs to a process created at a different time (pid reuse);
  UNKNOWN  anything else -- a record from another host, a creation time that was never captured, a process that cannot be
           inspected (access denied), psutil missing, any error. Uncertainty is kept, never resolved by guessing.

Only DEAD allows `audit.reconcile_orphans` to say anything, and what it says is still only "interrupted: outcome unknown".
Nothing here runs, retries or undoes a skill.
"""

from __future__ import annotations

import contextlib
import os
import platform
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

from friday.log import get

log = get(__name__)

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# Both numbers come from the same OS call, so a genuine match is exact; this only absorbs float round-trip noise.
START_TOLERANCE_S = 0.01


class OwnerError(RuntimeError):
    """The process could not be recorded as the owner of an action, so the action must not start."""


@dataclass(frozen=True)
class Identity:
    id: str
    pid: int
    proc_start: float | None
    host: str


def _host() -> str:
    return platform.node() or "unknown-host"


def _own_start(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        log.warning("process creation time unavailable; this session's liveness will stay indeterminate", exc_info=True)
        return None


_lock = threading.RLock()
_identity: Identity | None = None
_override: Identity | None = None  # test seam only: see `_use_identity`
_kind = "process"


def identity() -> Identity:
    """This process's identity, created on first use. A forked child (different pid) gets a fresh one."""
    global _identity
    with _lock:
        if _override is not None:
            return _override
        pid = os.getpid()
        if _identity is None or _identity.pid != pid:
            _identity = Identity(uuid.uuid4().hex, pid, _own_start(pid), _host())
        return _identity


def kind() -> str:
    return _kind


@contextlib.contextmanager
def _use_identity(ident: Identity, as_kind: str = "process") -> Iterator[Identity]:
    """Test seam: act as another session for the duration of the block."""
    global _override, _kind
    with _lock:
        saved = (_override, _kind)
        _override, _kind = ident, as_kind
    try:
        yield ident
    finally:
        with _lock:
            _override, _kind = saved


def ensure_session(c: sqlite3.Connection, ident: Identity | None = None, as_kind: str | None = None) -> str:
    """Make sure this session has a row in the database `c` points at (no commit: the caller's transaction owns it).

    `INSERT OR IGNORE`, so repeating it is free and cannot disturb an existing row. Returns the session id."""
    from friday import store

    ident = ident or identity()
    c.execute(
        "INSERT OR IGNORE INTO runtime_sessions (id, pid, proc_start, host, kind, started_at) VALUES (?,?,?,?,?,?)",
        (ident.id, ident.pid, ident.proc_start, ident.host, as_kind or _kind, store.now()),
    )
    return ident.id


def register(as_kind: str) -> str:
    """Declare what kind of FRIDAY process this is and record the session now, so a startup that cannot do it fails where it
    can be seen. Raises `OwnerError` if the record cannot be written."""
    global _kind
    from friday import store

    with _lock:
        _kind = as_kind
    ident = identity()
    try:
        c = store.conn()
        ensure_session(c, ident, as_kind)
        c.execute("UPDATE runtime_sessions SET kind=? WHERE id=?", (as_kind, ident.id))
        c.commit()
    except Exception as exc:  # noqa: BLE001
        raise OwnerError(f"could not record this process as a FRIDAY session: {exc}") from exc
    return ident.id


Probe = Callable[[int, "float | None", str], str]


def probe(pid: int, proc_start: float | None, host: str) -> str:
    """ALIVE / DEAD / UNKNOWN for a recorded process identity (see the module docstring). Never raises."""
    try:
        if host != _host():
            return UNKNOWN  # another machine's process: nothing here can say
        if not isinstance(pid, int) or pid <= 0:
            return UNKNOWN
        import psutil

        if not psutil.pid_exists(pid):  # on Windows this also sees a process that exited but still has an open handle
            return DEAD
        try:
            created = float(psutil.Process(pid).create_time())
        except psutil.NoSuchProcess:
            return DEAD
        except (psutil.AccessDenied, OSError):
            return UNKNOWN
        if proc_start is None:
            return UNKNOWN  # something is running at that pid; with no recorded start there is no way to tell whose
        if abs(created - float(proc_start)) > START_TOLERANCE_S:
            return DEAD  # the pid belongs to a different, later process: the recorded one is gone
        return ALIVE
    except Exception:  # noqa: BLE001
        log.warning("process liveness check failed; treating as indeterminate", exc_info=True)
        return UNKNOWN
