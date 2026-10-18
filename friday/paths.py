"""Filesystem layout. Everything FRIDAY writes lives under ROOT/data."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
LOGS = DATA / "logs"
MODELS = DATA / "models"
CACHE = DATA / "cache"


def _default_snapshots() -> Path:
    """Where recovery copies of overwritten/deleted files live (Phase 33).

    They are copies of the user's own files, so they must not sit in the repository folder, which
    can be synced to a cloud drive. Order: the FRIDAY_SNAPSHOTS environment variable (tests and
    advanced use); then the per-user local application-data folder (not synced); then, only when
    neither exists, the old in-repository location. `files.snapshot_dir` in config.yaml overrides
    all of these (see friday.recovery.root)."""
    override = os.environ.get("FRIDAY_SNAPSHOTS", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "FRIDAY" / "snapshots"
    return DATA / "snapshots"


SNAPSHOTS = _default_snapshots()  # recovery copies for the undo journal (files.write/copy/move/delete)
LEGACY_SNAPSHOTS = DATA / "snapshots"  # where Phase 32 kept them; still READ so old undo entries work

def _default_db() -> Path:
    """Where the runtime database lives (Phase 35).

    It holds the audit log, goals, episodes, corrections and memory -- personal data -- so it must
    not sit in the repository folder, which can be synced to a cloud drive. Order: the FRIDAY_DB
    environment variable (tests and advanced use); then the per-user local application-data folder
    (not synced); then, only when neither exists, the old in-repository location.
    `storage.db_path` in config.yaml overrides all of these (see friday.dbplace.resolve)."""
    override = os.environ.get("FRIDAY_DB", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "FRIDAY" / "friday.db"
    return DATA / "friday.db"


DB_PATH = _default_db()  # tests redirect this (store.use_temp_db); production code never assigns it
DEFAULT_DB_PATH = DB_PATH  # the un-redirected default, so a redirect can be told from the real thing
LEGACY_DB = DATA / "friday.db"  # where Phases 1-34 kept it; migrated from, never deleted by FRIDAY
CONFIG_PATH = ROOT / "config.yaml"


def ensure() -> None:
    """Create every directory FRIDAY needs. Safe to call repeatedly."""
    for d in (DATA, LOGS, MODELS, CACHE):
        d.mkdir(parents=True, exist_ok=True)
