"""Application skills: launch, focus, close, list.

App names are resolved against the *actual* Start Menu index rather than a
hardcoded list, so "open spotify" works for whatever you happen to have installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import psutil
from rapidfuzz import fuzz, process, utils

from friday.log import get
from friday.registry import SkillResult, skill

log = get(__name__)

# Names that appear in Start Menu folders but aren't apps you'd launch by voice.
_NOISE = {
    "uninstall", "readme", "documentation", "help", "license", "changelog",
    "website", "homepage", "support", "manual", "release notes",
}


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for var in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(var)
        if base:
            p = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if p.exists():
                dirs.append(p)
    return dirs


def _app_paths_registry() -> dict[str, str]:
    """Windows' own executable index: HKLM/HKCU ...\\App Paths.

    Every installer that wants `Run` / `start <name>` to find it without a
    PATH entry registers itself here — Chrome, Edge, and most non-Store apps
    do. This is a second, independent discovery source alongside Start Menu
    shortcuts (some installs register one but not the other), and unlike a
    hardcoded path list it reflects whatever is actually installed on *this*
    machine.
    """
    index: dict[str, str] = {}
    try:
        import winreg
    except ImportError:
        return index

    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        target, _ = winreg.QueryValueEx(sub, "")
                except OSError:
                    continue
                if not target:
                    continue
                display = Path(sub_name).stem.lower()
                index.setdefault(display, target.strip('"'))
        finally:
            root.Close()
    return index


# Extra display names worth mapping onto an App-Paths executable stem, since
# people say "chrome" or "terminal", not the .exe filename.
_APP_PATHS_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
}


@lru_cache(maxsize=1)
def _app_index() -> dict[str, str]:
    """Map lowercase app name -> launch target. Cached for the process lifetime."""
    index: dict[str, str] = {}
    for root in _start_menu_dirs():
        for lnk in root.rglob("*.lnk"):
            name = lnk.stem.strip()
            low = name.lower()
            if any(n in low for n in _NOISE):
                continue
            # First one wins; user Start Menu is scanned before ProgramData.
            index.setdefault(low, str(lnk))

    # Second discovery source: Windows' own App Paths registry. Fills gaps
    # the Start Menu scan misses (e.g. a Chrome install that registered
    # App Paths but whose shortcut lives somewhere the scan doesn't reach).
    by_paths = _app_paths_registry()
    for alias, stem in _APP_PATHS_ALIASES.items():
        if alias not in index and stem in by_paths:
            index[alias] = by_paths[stem]
    for stem, target in by_paths.items():
        index.setdefault(stem, target)

    # A few things that often aren't shortcuts but should always work.
    for name, target in {
        "settings": "ms-settings:",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "task manager": "taskmgr.exe",
        "control panel": "control.exe",
        "calculator": "calc.exe",
        "notepad": "notepad.exe",
        "command prompt": "cmd.exe",
        "powershell": "powershell.exe",
    }.items():
        index.setdefault(name, target)

    # Windows Terminal ships as `wt.exe` on PATH when installed, with no
    # App-Paths entry and a Start Menu shortcut that isn't always indexed
    # the same way across Windows builds.
    wt = shutil.which("wt.exe") or shutil.which("wt")
    if wt:
        index.setdefault("terminal", wt)
        index.setdefault("windows terminal", wt)

    # VS Code's own CLI, when on PATH, is a more reliable launch target than
    # whatever its Start Menu shortcut is named across versions/channels.
    code_bin = shutil.which("code") or shutil.which("code.cmd")
    if code_bin:
        index.setdefault("vs code", code_bin)
        index.setdefault("vscode", code_bin)
        index.setdefault("visual studio code", code_bin)

    log.info("app index: %d entries", len(index))
    return index


def resolve_app(query: str) -> tuple[str, str] | None:
    """Fuzzy-match a spoken app name. Returns (display_name, target) or None."""
    index = _app_index()
    if not index:
        return None

    q = query.strip().lower()
    if q in index:
        return q, index[q]

    match = process.extractOne(
        q, index.keys(), scorer=fuzz.WRatio,
        processor=utils.default_process, score_cutoff=72,
    )
    if match is None:
        return None
    name = match[0]
    return name, index[name]


def _suggestions(query: str, limit: int = 3) -> list[str]:
    """Best-effort near-matches for a failed app lookup, for diagnostics only."""
    index = _app_index()
    if not index:
        return []
    hits = process.extract(
        query, index.keys(), scorer=fuzz.WRatio,
        processor=utils.default_process, score_cutoff=45, limit=limit,
    )
    return [name for name, _score, _idx in hits]


def _windows() -> list[dict]:
    """Enumerate visible top-level windows with titles."""
    import win32gui
    import win32process

    found: list[dict] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid).name()
        except Exception:
            pid, proc = 0, ""
        found.append({"hwnd": hwnd, "title": title, "pid": pid, "process": proc})

    win32gui.EnumWindows(cb, None)
    return found


def _match_window(app: str, wins: list[dict]) -> dict | None:
    """Best-effort window lookup by title, falling back to process name.

    partial_ratio (best-aligned substring), not WRatio: a short app name like
    "chrome" against a long, noisy window title ("Foo Bar - Google Chrome")
    scores WRatio's whole-string alignment lower than an unrelated title of
    similar length, purely by coincidence — partial_ratio correctly scores
    the literal substring match at 100 instead. See friday.skills.windows
    for the same fix, needed for the same reason.
    """
    if not wins:
        return None
    choices = {w["title"]: w for w in wins}
    match = process.extractOne(
        app, choices.keys(), scorer=fuzz.partial_ratio,
        processor=utils.default_process, score_cutoff=80,
    )
    if match is not None:
        return choices[match[0]]

    by_proc = {w["process"]: w for w in wins if w["process"]}
    match = process.extractOne(
        app, by_proc.keys(), scorer=fuzz.partial_ratio,
        processor=utils.default_process, score_cutoff=80,
    )
    return by_proc[match[0]] if match is not None else None


def _focus_hwnd(hwnd: int) -> None:
    """Bring hwnd to the foreground, working around Windows' foreground lock.

    A bare SetForegroundWindow is only honoured when Windows considers the
    calling thread to already own user input. Every call here runs on a
    background asyncio-executor thread, not whichever thread last received a
    keystroke, so a plain call is denied — raising pywintypes.error(0, ...) —
    whenever some other window (e.g. the quickbar itself) currently holds the
    foreground. This reproduces 100% of the time for any already-running app,
    not just one in particular; it was masked for apps that are usually
    launched fresh (a newly created process window doesn't need this).
    Temporarily attaching this thread's input queue to the foreground
    thread's is the standard, documented workaround (the same one
    pywinauto/AutoHotkey use) — it makes the request look like it comes from
    the thread Windows already trusts with focus changes.
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    current_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0

    attached = set()
    for thread_id in (target_thread, fg_thread):
        if thread_id and thread_id != current_thread and thread_id not in attached:
            try:
                win32process.AttachThreadInput(current_thread, thread_id, True)
                attached.add(thread_id)
            except Exception:
                log.debug("AttachThreadInput failed for thread %s", thread_id)

    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        for thread_id in attached:
            try:
                win32process.AttachThreadInput(current_thread, thread_id, False)
            except Exception:
                pass


# --------------------------------------------------------------------------


@skill(
    name="apps.open",
    tier="L1",
    action="open",
    description="Launch an application by name",
    examples=[
        "open chrome",
        "launch spotify",
        "start notepad",
        "run calculator",
        "open the browser",
        "fire up vs code",
        "can you open settings",
    ],
)
def open_app(
    app: Annotated[str, "name of the application, e.g. 'chrome'"],
) -> SkillResult:
    hit = resolve_app(app)
    if hit is None:
        suggestions = _suggestions(app)
        hint = f" Did you mean {', '.join(suggestions)}?" if suggestions else ""
        count = len(_app_index())
        return SkillResult(
            speech=f"I couldn't find an app called {app}.{hint}",
            ok=False,
            data={"suggestions": suggestions, "indexed_apps": count},
        )

    name, target = hit

    # Most apps people ask FRIDAY to "open" are single-window/single-instance
    # in practice (VS Code, Chrome, the browser session, a chat app) — if a
    # matching window is already up, switch to it instead of spawning a
    # second instance the user didn't ask for.
    existing = _match_window(name, _windows())
    if existing is not None:
        try:
            _focus_hwnd(existing["hwnd"])
        except Exception as exc:
            return SkillResult(speech=f"{name} is already open, but I couldn't switch to it: {exc}", ok=False)
        return SkillResult(
            speech=f"{name} is already open — switched to it.",
            data={"app": name, "already_running": True, "title": existing["title"]},
        )

    try:
        if target.endswith(".lnk"):
            os.startfile(target)
        elif target.startswith("ms-"):
            os.startfile(target)
        else:
            subprocess.Popen(target, shell=True)
    except Exception as exc:
        return SkillResult(speech=f"I couldn't open {name}: {exc}", ok=False)

    return SkillResult(speech=f"Opening {name}.", data={"app": name})


@skill(
    name="apps.focus",
    tier="L1",
    action="focus",
    description="Bring an already-running window to the foreground",
    examples=[
        "switch to chrome",
        "focus on spotify",
        "bring up my browser",
        "go to notepad",
        "show me vs code",
    ],
)
def focus_window(
    app: Annotated[str, "window title or process name to focus"],
) -> SkillResult:
    wins = _windows()
    if not wins:
        return SkillResult(speech="I don't see any open windows.", ok=False)

    win = _match_window(app, wins)
    if win is None:
        return SkillResult(speech=f"I don't see {app} open.", ok=False)

    try:
        _focus_hwnd(win["hwnd"])
    except Exception as exc:
        return SkillResult(speech=f"I couldn't focus that window: {exc}", ok=False)

    return SkillResult(
        speech=f"Switched to {win['title'][:60]}.", data={"title": win["title"]}
    )


@skill(
    name="apps.list",
    tier="L0",
    description="List the currently open application windows",
    examples=[
        "what's open",
        "list open windows",
        "what apps are running",
        "show me what's running",
        "which programs are open",
    ],
)
def list_windows() -> SkillResult:
    wins = _windows()
    titles = [w["title"] for w in wins][:15]
    if not titles:
        return SkillResult(speech="Nothing seems to be open.")

    spoken = ", ".join(t[:40] for t in titles[:6])
    more = f" and {len(titles) - 6} more" if len(titles) > 6 else ""
    return SkillResult(
        speech=f"You have {len(wins)} windows open: {spoken}{more}.",
        data={"windows": wins},
    )


@skill(
    name="apps.close",
    tier="L2",
    action="delete",
    description="Close an application window",
    examples=["close chrome", "quit spotify", "shut notepad", "close that window"],
    dry_run=lambda app: f"Close the window matching '{app}'",
)
def close_app(
    app: Annotated[str, "window title or process name to close"],
) -> SkillResult:
    import win32con
    import win32gui

    win = _match_window(app, _windows())
    if win is None:
        return SkillResult(speech=f"I don't see {app} open.", ok=False)

    win32gui.PostMessage(win["hwnd"], win32con.WM_CLOSE, 0, 0)
    time.sleep(0.3)
    return SkillResult(speech=f"Closed {win['title'][:60]}.")
