"""A single polled key check used only to cancel an in-progress voice cycle.

Deliberately not a hook: `friday.hotkey` already avoids `SetWindowsHookEx`
because a global keyboard hook reads every keystroke anywhere, which is what
makes security software flag it as a keylogger. `GetAsyncKeyState` has the
same "reads any key" shape, so this is used the same narrow way hotkey.py
avoids the broader risk: polled for one specific key (Escape), only for the
few seconds FRIDAY is actively recording or speaking in response to a voice
activation the user just triggered themselves.

`request_cancel()` extends this same cancel *input* to a second source: the
desktop GUI's Stop button (friday/gui/command_bar.py), which has no physical
key to poll. It is consumed exactly once (matching every call site's
"one True aborts the current loop" contract — see friday/voice/capture.py
and friday/voice/tts.py) and expires unconsumed after a short TTL, so a
click that lands just after its own cycle already ended naturally can never
bleed into cancelling a later, unrelated cycle.
"""

from __future__ import annotations

import ctypes
import threading
import time

_VK_ESCAPE = 0x1B
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_GUI_CANCEL_TTL_S = 3.0
_gui_cancel_lock = threading.Lock()
_gui_cancel_requested_at: float | None = None


def request_cancel() -> None:
    """Call from the GUI thread when Stop/Cancel is clicked during an active
    voice cycle."""
    global _gui_cancel_requested_at
    with _gui_cancel_lock:
        _gui_cancel_requested_at = time.monotonic()


def _consume_gui_cancel() -> bool:
    global _gui_cancel_requested_at
    with _gui_cancel_lock:
        requested_at, _gui_cancel_requested_at = _gui_cancel_requested_at, None
    if requested_at is None:
        return False
    return (time.monotonic() - requested_at) < _GUI_CANCEL_TTL_S


def esc_pressed() -> bool:
    return bool(_user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000) or _consume_gui_cancel()
