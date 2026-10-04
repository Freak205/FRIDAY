"""Low-level keyboard and mouse synthesis.

Built on SendInput via ctypes rather than a helper library — pywin32's
`keybd_event` is deprecated and doesn't reach elevated or DirectInput windows
reliably. Used by window management, UI automation, and the typing skills.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800

# Virtual key codes we name by hand; anything else goes through VkKeyScanW.
VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "printscreen": 0x2C, "insert": 0x2D, "capslock": 0x14,
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


def _send(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _key_input(vk: int, up: bool = False) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                      time=0, dwExtraInfo=None),
    )


def _char_input(char: str, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags, time=0, dwExtraInfo=None),
    )


def _resolve(name: str) -> int | None:
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if len(name) == 1:
        result = user32.VkKeyScanW(ctypes.c_wchar(name))
        if result != -1:
            return result & 0xFF
    return None


def press(combo: str, *, delay: float = 0.02) -> bool:
    """Press a key combination like 'ctrl+shift+esc' or 'win+left'."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    codes = [_resolve(p) for p in parts]
    if any(c is None for c in codes):
        return False

    for code in codes:
        _send(_key_input(code))
        time.sleep(delay)
    for code in reversed(codes):
        _send(_key_input(code, up=True))
    return True


def type_text(text: str, *, wpm: float = 0.0) -> None:
    """Type text as Unicode, bypassing keyboard layout entirely."""
    for char in text:
        if char == "\n":
            press("enter")
            continue
        _send(_char_input(char), _char_input(char, up=True))
        if wpm:
            time.sleep(60.0 / (wpm * 5))


def move_mouse(x: int, y: int) -> None:
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    nx = int(x * 65535 / max(width - 1, 1))
    ny = int(y * 65535 / max(height - 1, 1))
    _send(INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                      dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                      time=0, dwExtraInfo=None),
    ))


def click(x: int | None = None, y: int | None = None, button: str = "left") -> None:
    if x is not None and y is not None:
        move_mouse(x, y)
        time.sleep(0.03)

    down, up = (
        (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
        if button == "right"
        else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    )
    for flag in (down, up):
        _send(INPUT(
            type=INPUT_MOUSE,
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag,
                          time=0, dwExtraInfo=None),
        ))


def scroll(amount: int) -> None:
    """Positive scrolls up, negative down. One notch is 120."""
    _send(INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=amount * 120,
                      dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=None),
    ))


def drag(x1: int, y1: int, x2: int, y2: int, *, steps: int = 12, delay: float = 0.015) -> None:
    """Press at (x1, y1), move to (x2, y2) in small steps, then release.

    The intermediate moves matter: some drop targets (sortable lists,
    drag-to-reorder UI) only react to a real sequence of move events between
    down and up, not a same-frame teleport.
    """
    move_mouse(x1, y1)
    time.sleep(0.03)
    _send(INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN,
                      time=0, dwExtraInfo=None),
    ))
    time.sleep(delay)

    steps = max(1, steps)
    for i in range(1, steps + 1):
        nx = x1 + (x2 - x1) * i // steps
        ny = y1 + (y2 - y1) * i // steps
        move_mouse(nx, ny)
        time.sleep(delay)

    _send(INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP,
                      time=0, dwExtraInfo=None),
    ))


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
