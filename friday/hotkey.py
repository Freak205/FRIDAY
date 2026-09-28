"""Global hotkey registration via RegisterHotKey.

Runs its own thread with a Win32 message loop. Chosen over the `keyboard`
package because it needs no elevation, installs no low-level keyboard hook, and
therefore doesn't show up as a keylogger to security software.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

from friday.log import get

log = get(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_MODS = {
    "alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN,
}

_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _parse(combo: str) -> tuple[int, int] | None:
    modifiers = 0
    key = None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _MODS:
            modifiers |= _MODS[part]
        elif part in _KEYS:
            key = _KEYS[part]
        elif len(part) == 1:
            key = ord(part.upper())
    if key is None or modifiers == 0:
        return None
    return modifiers | MOD_NOREPEAT, key


class HotkeyManager:
    """Registers hotkeys and dispatches them to callbacks on a worker thread."""

    def __init__(self) -> None:
        self._bindings: dict[int, Callable[[], None]] = {}
        self._pending: list[tuple[str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._running = False
        self._status: dict[str, bool] = {}
        self._ready = threading.Event()

    def bind(self, combo: str, callback: Callable[[], None]) -> None:
        """Register a hotkey. Safe to call before `start`."""
        self._pending.append((combo, callback))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkeys")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def wait_ready(self, timeout: float = 2.0) -> bool:
        """Blocks until initial hotkey registration has been attempted (or
        `timeout`), so a startup diagnostic can log real results instead of
        guessing. See `status()`/`is_alive()`.
        """
        return self._ready.wait(timeout)

    def status(self) -> dict[str, bool]:
        """combo -> whether RegisterHotKey succeeded for it, as of the last
        `start()`. Empty before registration has run.
        """
        return dict(self._status)

    def is_alive(self) -> bool:
        """Whether the message-pump thread is still running. False here
        after `start()` means the thread died before/without pumping
        messages — every hotkey on this process is dead in the water even
        if `status()` shows it registered.
        """
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        # Hotkeys must be registered on the same thread that pumps the messages.
        registered = 0
        for index, (combo, callback) in enumerate(self._pending, start=1):
            parsed = _parse(combo)
            if parsed is None:
                log.warning("unparseable hotkey %r", combo)
                self._status[combo] = False
                continue
            modifiers, key = parsed
            if user32.RegisterHotKey(None, index, modifiers, key):
                self._bindings[index] = callback
                registered += 1
                self._status[combo] = True
                log.info("hotkey %s registered", combo)
            else:
                self._status[combo] = False
                log.warning(
                    "could not register %s (error %s) — another app "
                    "(possibly a duplicate FRIDAY process) may own it",
                    combo, ctypes.get_last_error(),
                )

        if not registered:
            self._ready.set()
            return
        self._ready.set()

        msg = wintypes.MSG()
        while self._running:
            # PeekMessage rather than GetMessage so `stop()` is honoured promptly.
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY:
                    callback = self._bindings.get(msg.wParam)
                    if callback:
                        try:
                            callback()
                        except Exception:
                            log.exception("hotkey callback failed")
            else:
                ctypes.windll.kernel32.Sleep(30)

        for index in self._bindings:
            user32.UnregisterHotKey(None, index)


HOTKEYS = HotkeyManager()
