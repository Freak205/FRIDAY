"""Filesystem layout. Everything FRIDAY writes lives under ROOT/data."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
LOGS = DATA / "logs"
MODELS = DATA / "models"
CACHE = DATA / "cache"
SNAPSHOTS = DATA / "snapshots"  # pre-edit file copies for the undo journal

DB_PATH = DATA / "friday.db"
CONFIG_PATH = ROOT / "config.yaml"


def ensure() -> None:
    """Create every directory FRIDAY needs. Safe to call repeatedly."""
    for d in (DATA, LOGS, MODELS, CACHE, SNAPSHOTS):
        d.mkdir(parents=True, exist_ok=True)
