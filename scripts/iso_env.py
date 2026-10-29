"""Shared isolation for smoke tests (Phase 35): a throwaway database AND a throwaway recovery store.

`friday.dbplace` refuses (IsolationError) any test/benchmark entry point that opens the user's real database or
uses the real recovery store. A smoke test that writes through `store`, `audit`, `EXECUTOR`, the undo journal or
the `files.*` mutators wraps its run in:

    with iso_env.isolated_runtime():
        ...

`store.use_temp_db()` redirects the database for every thread; this also points `paths.SNAPSHOTS` and
`paths.LEGACY_SNAPSHOTS` at a temp folder so `recovery.root()` never resolves to the real store, and (Phase 87)
`paths.KEY_FILE` at a temp file so `friday.keystore` never reaches the user's real DPAPI-protected key.
Everything is restored (and the temp folder removed) on exit. It adds no assertion and weakens none.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import keystore, paths, store  # noqa: E402


@contextlib.contextmanager
def isolated_runtime():
    with tempfile.TemporaryDirectory(prefix="friday-smoke-", ignore_cleanup_errors=True) as t, store.use_temp_db():
        saved = (paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS, paths.KEY_FILE)
        paths.SNAPSHOTS = Path(t) / "snapshots"
        paths.LEGACY_SNAPSHOTS = Path(t) / "legacy-snapshots"
        paths.KEY_FILE = Path(t) / "keys" / "fingerprint.key"
        try:
            yield
        finally:
            paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS, paths.KEY_FILE = saved
            keystore._forget()
