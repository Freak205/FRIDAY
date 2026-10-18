"""Where FRIDAY's runtime database lives, how it gets there, and how tests are kept away from it (Phase 35).

The database holds the audit log, goals, episodes, corrections, memory and the undo journal -- personal
data. Until Phase 34 it was `data/friday.db` inside the repository, which here is a OneDrive folder.
Now it is `%LOCALAPPDATA%\\FRIDAY\\friday.db` (not synced), overridable with `storage.db_path` in config.yaml
or the FRIDAY_DB environment variable.

Contents:
  resolve()              the ONE path every connection (main thread and workers alike) opens.
  ensure_ready(path)     first open of a path: migrate the old in-repository database if this is the real
                         location and nothing is there yet. Never overwrites, never touches the old file.
  migrate_legacy(...)    SQLite online-backup copy -> validate -> atomic no-overwrite publish.
  guard / guard_store    fail-fast tripwires: a test or benchmark entry point that selects the real database
                         or the real recovery store is refused (IsolationError) before anything is written.
  status / scan_sensitive  read-only reports (counts only -- no stored value is ever returned).

Nothing here deletes or edits an existing database.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday import paths
from friday.log import get

log = get(__name__)


class DbPlacementError(RuntimeError):
    """The database could not be placed or migrated safely. The message says how to recover."""


class IsolationError(RuntimeError):
    """A test or benchmark selected the user's real database or recovery store."""


# --- path resolution ---------------------------------------------------------------------------------

def _norm(p: str | os.PathLike[str]) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.fspath(p)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(os.fspath(p)))


def _under(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _temp_root() -> str:
    return tempfile.gettempdir()


def configured_db() -> Path | None:
    """`storage.db_path` from config.yaml, or None. A relative path is relative to the repository."""
    try:
        from friday.config import CFG

        raw = str(getattr(getattr(CFG, "storage", None), "db_path", "") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    p = Path(os.path.expandvars(raw)).expanduser()
    return p if p.is_absolute() else paths.ROOT / p


def redirected() -> bool:
    """True when `paths.DB_PATH` was reassigned (store.use_temp_db): an explicit redirect beats everything."""
    return _norm(paths.DB_PATH) != _norm(paths.DEFAULT_DB_PATH)


def resolve() -> Path:
    """The database path. Read at call time by EVERY connection, so the main thread and a worker thread
    always agree, including inside `store.use_temp_db()`."""
    if redirected():
        return Path(paths.DB_PATH)
    return configured_db() or Path(paths.DB_PATH)


def canonical_db() -> Path | None:
    """The per-user application-data location, independent of FRIDAY_DB."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "FRIDAY" / "friday.db" if local else None


def canonical_store() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "FRIDAY" / "snapshots" if local else None


def real_db_paths() -> list[Path]:
    out = [p for p in (canonical_db(), Path(paths.LEGACY_DB)) if p is not None]
    cfg = configured_db()
    if cfg is not None and not _under(cfg, _temp_root()):
        out.append(cfg)
    return out


def real_store_paths() -> list[Path]:
    out = [p for p in (canonical_store(), Path(paths.LEGACY_SNAPSHOTS)) if p is not None]
    try:
        from friday.config import CFG

        raw = str(getattr(CFG.files, "snapshot_dir", "") or "").strip()
    except Exception:
        raw = ""
    if raw:
        p = Path(os.path.expandvars(raw)).expanduser()
        p = p if p.is_absolute() else paths.ROOT / p
        if not _under(p, _temp_root()):
            out.append(p)
    return out


def is_real_db(path: str | os.PathLike[str]) -> bool:
    n = _norm(path)
    return any(n == _norm(p) for p in real_db_paths())


def is_real_store(path: str | os.PathLike[str]) -> bool:
    return any(_under(path, p) for p in real_store_paths())


# --- test / benchmark tripwires ----------------------------------------------------------------------

# Entry points that must never touch the user's data: `scripts/smoke_*`, benchmarks, regression runners.
_TEST_NAME = re.compile(r"^(smoke_|test_|bench|agent_|regression|phase\d+_)", re.IGNORECASE)


def is_test_entrypoint() -> bool:
    """True when this process was started as a test / benchmark / regression runner. Automatic -- nobody has
    to remember an environment variable. FRIDAY_ALLOW_REAL_DB=1 opts out; FRIDAY_REFUSE_REAL_DB=1 opts in."""
    if os.environ.get("FRIDAY_ALLOW_REAL_DB") == "1":
        return False
    if os.environ.get("FRIDAY_REFUSE_REAL_DB") == "1":
        return True
    if "pytest" in sys.modules:
        return True
    main = sys.modules.get("__main__")
    name = getattr(main, "__file__", None) or (sys.argv[0] if sys.argv else "")
    return bool(name) and bool(_TEST_NAME.match(Path(name).stem))


def guard(path: str | os.PathLike[str]) -> None:
    """Refuse to open the real database from a test or benchmark entry point."""
    if is_test_entrypoint() and is_real_db(path):
        raise IsolationError(
            f"refusing to open the real FRIDAY database {path} from a test/benchmark run. "
            "Wrap the run in `friday.store.use_temp_db()` (and point paths.SNAPSHOTS at a temp folder), "
            "or set FRIDAY_ALLOW_REAL_DB=1 if you really mean it."
        )


def guard_store(path: str | os.PathLike[str]) -> None:
    """Refuse to use the real recovery store from a test or benchmark entry point."""
    if is_test_entrypoint() and is_real_store(path):
        raise IsolationError(
            f"refusing to use the real recovery store {path} from a test/benchmark run. "
            "Point paths.SNAPSHOTS at a temp folder, or set FRIDAY_ALLOW_REAL_DB=1 if you really mean it."
        )


def require_isolated(*, db: bool = True, store: bool = True) -> None:
    """Call at the top of a destructive test: raises unless the selected database / recovery store are throwaways."""
    if db:
        p = resolve()
        if is_real_db(p) or not _under(p, _temp_root()):
            raise IsolationError(f"the selected database {p} is not a throwaway under the temp folder")
    if store:
        from friday import recovery

        try:
            r = recovery.root()
        except Exception as exc:  # noqa: BLE001
            raise IsolationError(f"the recovery store can't be resolved: {exc}") from exc
        if is_real_store(r) or not _under(r, _temp_root()):
            raise IsolationError(f"the selected recovery store {r} is not a throwaway under the temp folder")


# --- cloud-sync detection ----------------------------------------------------------------------------

def in_synced_folder(path: str | os.PathLike[str]) -> bool:
    """Best effort: is this path inside a OneDrive / Dropbox / Google Drive folder?"""
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var, "").strip()
        if root and _under(path, root):
            return True
    parts = [x.lower() for x in Path(_norm(path)).parts]
    return any(x.startswith(("onedrive", "dropbox", "google drive", "googledrive", "icloud")) for x in parts)


# --- migration ---------------------------------------------------------------------------------------

_LOCK_STALE_S = 15 * 60
LOCK_WAIT_S = 60.0
_ready: set[str] = set()
_ready_lock = threading.RLock()


@dataclass(slots=True)
class Migration:
    source: Path
    dest: Path
    performed: bool = False
    reason: str = ""
    tables: dict[str, int] = field(default_factory=dict)


def _recovery_help(source: Path, dest: Path, why: str) -> str:
    return (
        f"FRIDAY could not move its database to {dest}: {why}. Nothing was lost -- your existing database at "
        f"{source} was not changed. To keep using it for now, set `storage.db_path: {source}` in config.yaml "
        f"(or the FRIDAY_DB environment variable). To retry, close FRIDAY and run `python run.py db migrate`."
    )


def _table_counts(c: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: int(c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names}


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)


def _acquire_lock(lock: Path, *, wait_s: float | None = None) -> bool:
    """Exclusive create. False when another process holds a fresh lock for the whole wait."""
    deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()  # a crashed migration's lock: ours to break, it holds no data
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def _publish_no_overwrite(tmp: Path, dest: Path) -> bool:
    """Atomically give `tmp` the name `dest` unless `dest` already exists. True if published."""
    try:
        os.link(str(tmp), str(dest))  # fails if dest exists, on every platform
    except FileExistsError:
        return False
    except OSError:
        if dest.exists():
            return False
        os.rename(str(tmp), str(dest))  # no hard links here; the exists() check above is the guard
        return True
    try:
        os.unlink(str(tmp))
    except OSError:
        pass
    return True


def migrate_legacy(dest: Path, source: Path | None = None) -> Migration:
    """Copy the old database to `dest` using SQLite's online backup (consistent even if it is open in another
    process or has -wal/-shm sidecars), validate the copy, and publish it atomically WITHOUT overwriting.

    The source is opened read-only and never modified or deleted. An interrupted run leaves only a
    `<name>.migrating-*` temp file next to `dest`, which the next run discards. If `dest` already exists the
    function does nothing."""
    source = Path(source or paths.LEGACY_DB)
    dest = Path(dest)
    out = Migration(source=source, dest=dest)
    if dest.exists():
        out.reason = "destination already exists; left untouched"
        return out
    if not source.exists():
        out.reason = "no old database to migrate"
        return out
    if _norm(source) == _norm(dest):
        out.reason = "source and destination are the same file"
        return out
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DbPlacementError(_recovery_help(source, dest, f"cannot create {dest.parent}: {exc}")) from exc

    lock = dest.with_name(dest.name + ".migrating.lock")
    if not _acquire_lock(lock):
        raise DbPlacementError(_recovery_help(source, dest, "another FRIDAY process is migrating right now"))
    tmp: Path | None = None
    try:
        if dest.exists():  # the other process finished while we waited
            out.reason = "destination appeared while waiting; left untouched"
            return out
        for stale in dest.parent.glob(dest.name + ".migrating-*"):  # our own interrupted copies; hold no unique data
            try:
                stale.unlink()
            except OSError:
                pass
        tmp = dest.with_name(f"{dest.name}.migrating-{secrets.token_hex(4)}")
        last = ""
        for attempt in range(3):
            if tmp.exists():
                tmp.unlink()
            src = _open_ro(source)
            dst = sqlite3.connect(str(tmp))
            try:
                before = _table_counts(src)
                src.backup(dst)
                after = _table_counts(src)
                copied = _table_counts(dst)
                verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:
                raise DbPlacementError(_recovery_help(source, dest, f"copy failed ({exc})")) from exc
            finally:
                dst.close()
                src.close()
            if verdict != "ok":
                raise DbPlacementError(_recovery_help(source, dest, "the copy failed SQLite's integrity check"))
            if copied == before or copied == after:
                out.tables = copied
                break
            last = "the old database changed while it was being copied"
        else:
            raise DbPlacementError(_recovery_help(source, dest, last or "the copy did not match the original"))

        # the copy is a rollback-journal file with the source's WAL setting in its header; fine either way.
        if not _publish_no_overwrite(tmp, dest):
            out.reason = "destination appeared first; left untouched"
            return out
        tmp = None
        # Re-open what was published and prove it, before anything may rely on it.
        chk = _open_ro(dest)
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or _table_counts(chk) != out.tables:
                bad = dest.with_name(dest.name + f".invalid-{int(time.time())}")
                chk.close()
                os.rename(str(dest), str(bad))  # kept for inspection, never deleted
                raise DbPlacementError(_recovery_help(source, dest, f"the published copy failed validation (kept as {bad})"))
        finally:
            try:
                chk.close()
            except sqlite3.Error:
                pass
        out.performed = True
        out.reason = "migrated"
        log.info("database migrated %s -> %s (%d tables); the old file was left in place", source, dest, len(out.tables))
        return out
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        try:
            lock.unlink()
        except OSError:
            pass


def ensure_ready(path: Path) -> Migration | None:
    """Called by `store.conn()` before the first open of `path`. Migrates the old database into `path` only when
    `path` is a production location (the per-user default or the configured one) that does not exist yet."""
    key = _norm(path)
    with _ready_lock:
        if key in _ready:
            return None
        production = any(key == _norm(p) for p in (canonical_db(), configured_db()) if p is not None)
        result: Migration | None = None
        if production and not Path(path).exists() and not is_test_entrypoint():
            result = migrate_legacy(Path(path))
        elif production and Path(path).exists() and Path(paths.LEGACY_DB).exists() and _norm(paths.LEGACY_DB) != key:
            log.warning(
                "an old FRIDAY database is still at %s (inside the repository folder). It is NOT used. After "
                "checking the data in %s, delete the old file yourself: it may hold personal data in a synced folder.",
                paths.LEGACY_DB, path,
            )
        if in_synced_folder(path):
            log.warning("the FRIDAY database %s is inside a cloud-synced folder; move it with storage.db_path", path)
        _ready.add(key)
        return result


def _forget_ready() -> None:  # tests only
    with _ready_lock:
        _ready.clear()


# --- read-only reports --------------------------------------------------------------------------------

def status() -> dict[str, Any]:
    """Where everything is. Counts and sizes only."""
    path = resolve()
    legacy = Path(paths.LEGACY_DB)

    def info(p: Path) -> dict[str, Any]:
        d: dict[str, Any] = {"path": str(p), "exists": p.exists(), "synced_folder": in_synced_folder(p)}
        if p.exists():
            d["bytes"] = p.stat().st_size
            d["sidecars"] = [s for s in ("-wal", "-shm") if Path(str(p) + s).exists()]
            try:
                c = _open_ro(p)
                try:
                    d["tables"] = _table_counts(c)
                finally:
                    c.close()
            except sqlite3.Error as exc:
                d["error"] = type(exc).__name__
        return d

    out = {"active": info(path), "legacy": info(legacy) if _norm(legacy) != _norm(path) else None}
    return out


# Skills whose argument carries what the user typed / a document body / code, and whose `result` speech
# carries content that was read back. Mirrors friday.auditview, but kept independent so the scan can describe
# data that was written BEFORE that policy existed.
_CONTENT_ARGS = {
    "files.write": ("content",), "input.type": ("text",), "ui.fill": ("text",), "browser.type": ("text",),
    "clipboard.write": ("text",), "whatsapp.compose": ("message",), "comms.send_message": ("text",),
    "notify.send": ("message",), "dev.python": ("code",), "shell.run": ("command",), "memory.remember": ("text",),
    "notes.add": ("text",), "routine.create": ("steps",),
}
_CONTENT_RESULTS = (
    "files.read", "clipboard.read", "memory.recall", "memory.list", "notes.read", "screen.read_text", "ui.read",
    "browser.read", "web.fetch", "knowledge.ask", "shell.run", "dev.python", "dev.git",
)


def scan_sensitive(db: Path | None = None) -> dict[str, Any]:
    """Count (never return) the sensitive values already stored. Opens the database read-only."""
    import json

    path = Path(db) if db else resolve()
    c = _open_ro(path)
    try:
        report: dict[str, Any] = {"database": str(path), "audit_rows": 0, "audit_args_with_content": {},
                                  "audit_results_with_content": {}, "episode_steps_with_content": {}, "urls_with_query": 0}
        report["audit_rows"] = int(c.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
        for skill, args in c.execute("SELECT skill, args FROM audit"):
            keys = _CONTENT_ARGS.get(skill)
            try:
                d = json.loads(args)
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            if keys:
                n = sum(1 for k in keys if isinstance(d.get(k), (str, list)) and len(d.get(k)) > 0)
                if n:
                    report["audit_args_with_content"][skill] = report["audit_args_with_content"].get(skill, 0) + 1
            u = d.get("url")
            if isinstance(u, str) and "?" in u:
                report["urls_with_query"] += 1
        for skill in _CONTENT_RESULTS:
            n = c.execute("SELECT COUNT(*) FROM audit WHERE skill=? AND length(COALESCE(result,''))>120", (skill,)).fetchone()[0]
            if n:
                report["audit_results_with_content"][skill] = int(n)
        for (plan,) in c.execute("SELECT plan FROM episodes"):
            try:
                steps = json.loads(plan or "[]")
            except ValueError:
                continue
            for s in steps if isinstance(steps, list) else []:
                a = s.get("args") if isinstance(s, dict) else None
                keys = _CONTENT_ARGS.get(s.get("tool", "")) if isinstance(s, dict) else None
                if keys and isinstance(a, dict) and any(a.get(k) for k in keys):
                    t = s["tool"]
                    report["episode_steps_with_content"][t] = report["episode_steps_with_content"].get(t, 0) + 1
        return report
    finally:
        c.close()
