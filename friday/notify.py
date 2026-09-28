"""Windows toast notifications.

This is how FRIDAY reaches you when you aren't looking at it — a job finished, a
reminder fired, a trigger caught something. Without it, unattended automation is
invisible.

Degrades quietly: if the WinRT toast path is unavailable, falls back to the tray
balloon, then to a log line. A notification failing must never take a job down.
"""

from __future__ import annotations

import threading
from typing import Any

from friday.config import CFG
from friday.log import get

log = get(__name__)

_toaster: Any = None
_toaster_failed = False
_lock = threading.Lock()

# Set by the desktop client so we can fall back to the tray balloon.
_tray_icon: Any = None


def register_tray(icon: Any) -> None:
    global _tray_icon
    _tray_icon = icon


def _get_toaster():
    global _toaster, _toaster_failed
    if _toaster_failed:
        return None
    if _toaster is not None:
        return _toaster

    with _lock:
        if _toaster is None and not _toaster_failed:
            try:
                from windows_toasts import WindowsToaster

                _toaster = WindowsToaster(CFG.identity.name)
            except Exception:
                log.debug("toast backend unavailable", exc_info=True)
                _toaster_failed = True
    return _toaster


def send(title: str, message: str = "", *, urgent: bool = False) -> bool:
    """Show a desktop notification. Returns True if something was displayed."""
    title = (title or CFG.identity.name).strip()[:64]
    message = (message or "").strip()[:220]

    toaster = _get_toaster()
    if toaster is not None:
        try:
            from windows_toasts import Toast

            toast = Toast()
            toast.text_fields = [title, message] if message else [title]
            toaster.show_toast(toast)
            return True
        except Exception:
            log.debug("toast failed, falling back", exc_info=True)

    if _tray_icon is not None:
        try:
            _tray_icon.notify(message or title, title)
            return True
        except Exception:
            log.debug("tray balloon failed", exc_info=True)

    log.info("NOTIFY%s %s — %s", " (urgent)" if urgent else "", title, message)
    return False
