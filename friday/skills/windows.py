"""Window management: snap, move, resize, minimise, maximise, virtual desktops."""

from __future__ import annotations

from typing import Annotated

from friday import winput
from friday.registry import SkillResult, skill
from friday.intent import KEY_RULE


def _foreground() -> tuple[int, str]:
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    return hwnd, win32gui.GetWindowText(hwnd)


def _find(app: str) -> tuple[int, str] | None:
    """Resolve a window by fuzzy title match, or the foreground window."""
    from rapidfuzz import fuzz, process, utils

    from friday.skills.apps import _windows

    if not app or app.lower() in ("this", "that", "current", "active", "it"):
        hwnd, title = _foreground()
        return (hwnd, title) if title else None

    wins = _windows()
    if not wins:
        return None
    titles = {w["title"]: w for w in wins}
    # partial_ratio, not WRatio: a short app name against a long, noisy window
    # title needs best-aligned-substring scoring, not whole-string alignment —
    # see friday.skills.apps.focus_window for the concrete failure this fixes.
    match = process.extractOne(
        app, titles.keys(), scorer=fuzz.partial_ratio,
        processor=utils.default_process, score_cutoff=80,
    )
    if match is None:
        return None
    win = titles[match[0]]
    return win["hwnd"], win["title"]


def _work_area() -> tuple[int, int, int, int]:
    """Screen area excluding the taskbar."""
    import win32api
    import win32con

    monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
    info = win32api.GetMonitorInfo(monitor)
    return info["Work"]  # (left, top, right, bottom)


@skill(
    name="window.snap",
    tier="L1",
    action="focus",
    description="Snap a window to a side or corner of the screen",
    examples=[
        "snap this to the left",
        "put this window on the right half",
        "snap chrome left",
        "move this to the right side",
        "tile this window left",
    ],
)
def snap(
    side: Annotated[str, "left | right | top | bottom | top-left | top-right | bottom-left | bottom-right"] = "left",
    app: Annotated[str, "window to snap, blank for the active one"] = "",
) -> SkillResult:
    import win32con
    import win32gui

    target = _find(app)
    if target is None:
        return SkillResult(speech=f"I can't find a window for {app or 'that'}.", ok=False)
    hwnd, title = target

    left, top, right, bottom = _work_area()
    width, height = right - left, bottom - top
    half_w, half_h = width // 2, height // 2

    layouts = {
        "left": (left, top, half_w, height),
        "right": (left + half_w, top, half_w, height),
        "top": (left, top, width, half_h),
        "bottom": (left, top + half_h, width, half_h),
        "top-left": (left, top, half_w, half_h),
        "top-right": (left + half_w, top, half_w, half_h),
        "bottom-left": (left, top + half_h, half_w, half_h),
        "bottom-right": (left + half_w, top + half_h, half_w, half_h),
        "full": (left, top, width, height),
        "center": (left + width // 4, top + height // 4, half_w, half_h),
    }

    key = side.strip().lower().replace(" ", "-").replace("_", "-")
    geometry = layouts.get(key)
    if geometry is None:
        return SkillResult(speech=f"I don't know the position '{side}'.", ok=False)

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.MoveWindow(hwnd, *geometry, True)
    win32gui.SetForegroundWindow(hwnd)

    return SkillResult(
        speech=f"Snapped {title[:40]} {key}.",
        data={"title": title, "position": key, "geometry": geometry},
    )


@skill(
    name="window.maximize",
    tier="L1",
    action="focus",
    description="Maximise a window to fill the screen",
    examples=["maximise this", "make this fullscreen", "maximize chrome", "fill the screen"],
    undo=lambda app="": {"skill": "window.restore", "args": {"app": app}},
)
def maximize(
    app: Annotated[str, "window to maximise, blank for the active one"] = "",
) -> SkillResult:
    import win32con
    import win32gui

    target = _find(app)
    if target is None:
        return SkillResult(speech="I can't find that window.", ok=False)
    hwnd, title = target
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return SkillResult(speech=f"Maximised {title[:40]}.")


@skill(
    name="window.minimize",
    tier="L1",
    action="focus",
    description="Minimise a window to the taskbar",
    examples=["minimise this", "hide this window", "minimize chrome", "get this out of the way"],
    undo=lambda app="": {"skill": "window.restore", "args": {"app": app}},
)
def minimize(
    app: Annotated[str, "window to minimise, blank for the active one"] = "",
) -> SkillResult:
    import win32con
    import win32gui

    target = _find(app)
    if target is None:
        return SkillResult(speech="I can't find that window.", ok=False)
    hwnd, title = target
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return SkillResult(speech=f"Minimised {title[:40]}.")


@skill(
    name="window.restore",
    tier="L1",
    action="focus",
    description="Restore a minimised or maximised window to its normal size",
    examples=["restore that window", "un-maximise this", "bring it back"],
)
def restore(
    app: Annotated[str, "window to restore, blank for the active one"] = "",
) -> SkillResult:
    import win32con
    import win32gui

    target = _find(app)
    if target is None:
        return SkillResult(speech="I can't find that window.", ok=False)
    hwnd, title = target
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    return SkillResult(speech=f"Restored {title[:40]}.")


@skill(
    name="window.minimize_all",
    tier="L1",
    action="focus",
    description="Minimise every window and show the desktop",
    examples=["show the desktop", "minimise everything", "clear my screen", "hide all windows"],
)
def minimize_all() -> SkillResult:
    winput.press("win+d")
    return SkillResult(speech="Showing the desktop.")


@skill(
    name="desktop.switch",
    tier="L1",
    action="focus",
    description="Switch to the next or previous virtual desktop",
    examples=[
        "next desktop",
        "switch virtual desktop",
        "go to the previous desktop",
        "move to desktop on the right",
    ],
)
def switch_desktop(
    direction: Annotated[str, "'next' or 'previous'"] = "next",
) -> SkillResult:
    key = "win+ctrl+right" if direction.startswith("n") else "win+ctrl+left"
    winput.press(key)
    return SkillResult(speech=f"Switched to the {direction} desktop.")


@skill(
    name="desktop.new",
    tier="L1",
    action="focus",
    description="Create a new virtual desktop",
    examples=["new desktop", "create a virtual desktop", "give me a fresh desktop"],
)
def new_desktop() -> SkillResult:
    winput.press("win+ctrl+d")
    return SkillResult(speech="New desktop created.")


@skill(
    name="input.type",
    tier="L1",
    action="modify",
    description="Type text into the focused window",
    examples=[
        "type hello world",
        "write this out for me",
        "enter that text",
        "type my email address",
    ],
)
def type_text(
    text: Annotated[str, "the text to type"],
) -> SkillResult:
    winput.type_text(text)
    return SkillResult(speech=f"Typed {len(text)} characters.", data={"length": len(text)})


@skill(
    name="input.hotkey",
    tier="L1",
    action=KEY_RULE,
    description="Press a keyboard shortcut, e.g. ctrl+s or alt+tab",
    examples=[
        "press ctrl s",
        "hit alt tab",
        "send control shift escape",
        "press escape",
        "do ctrl c",
    ],
)
def hotkey(
    combo: Annotated[str, "key combination like 'ctrl+s'"],
) -> SkillResult:
    normalized = combo.lower().replace(" and ", "+").replace(" ", "+")
    if winput.press(normalized):
        return SkillResult(speech=f"Pressed {normalized}.")
    return SkillResult(speech=f"I don't recognise the combination '{combo}'.", ok=False)
