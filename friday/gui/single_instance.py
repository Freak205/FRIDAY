"""A real single-instance guard for the desktop GUI.

`friday/desktop.py` (superseded) only ever *warned* about a duplicate
process via a psutil scan — Windows' `RegisterHotKey` is per-process, so a
second instance would silently lose the global hotkeys with no hard error.
This replaces that with an actual gate: a session-local named mutex via
`ctypes`, in the same raw-Win32 style `friday/hotkey.py` and
`friday/voice/keys.py` already use elsewhere in this project rather than
pulling in a new dependency for one Win32 call.

No `Global\\` prefix: that namespace is for cross-Terminal-Services-session
visibility (RDP/fast user switching), which isn't a goal here, and it can
require a privilege a locked-down environment denies for no benefit to a
single desktop user's assistant. The mutex is released automatically by
Windows when this process exits, by any means — clean exit, crash, or being
killed — which is exactly the "next launch sees no stale lock" behavior
wanted.
"""

from __future__ import annotations

import ctypes

from friday.log import get

log = get(__name__)

_MUTEX_NAME = "FRIDAY_Desktop_SingleInstance_9F3E1C"
_ERROR_ALREADY_EXISTS = 183

_mutex_handle: int | None = None


def acquire() -> bool:
    """True if this process now owns the lock (no other GUI instance is
    running); False if another instance already holds it. Safe to call more
    than once — a second call in the same process that already holds the
    lock re-acquires the same named mutex and also returns True.
    """
    global _mutex_handle

    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        # Couldn't even create the mutex (rare) — don't block startup over
        # a diagnostic aid; fall back to the best-effort psutil warning.
        log.warning("single-instance mutex creation failed (%s); falling back to a best-effort process scan", ctypes.get_last_error())
        _warn_duplicate_processes()
        return True

    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _mutex_handle = handle
    return True


def release() -> None:
    """Optional symmetry — Windows already releases the mutex on process
    exit regardless. Safe to call from a tray Quit handler for cleanliness.
    """
    global _mutex_handle
    if _mutex_handle is not None:
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None


def _warn_duplicate_processes() -> None:
    """Best-effort diagnostic only, used when the mutex itself couldn't be
    created — ported from `friday/desktop.py`'s `_warn_duplicate_processes`.
    """
    try:
        import os

        import psutil
    except Exception:
        return

    own_pid = os.getpid()
    ignore_pids = {own_pid}
    try:
        me = psutil.Process(own_pid)
        ignore_pids.update(p.pid for p in me.parents())
        ignore_pids.update(p.pid for p in me.children(recursive=True))
    except psutil.Error:
        pass

    found: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] in ignore_pids:
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "run.py" in cmdline and "desktop" in cmdline:
                found.append(f"pid={proc.info['pid']} cmd={cmdline}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if found:
        log.warning("possible duplicate FRIDAY process(es): %s", "; ".join(found))
