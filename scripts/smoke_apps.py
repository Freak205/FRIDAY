"""App launching: discovery diagnostics, and "already running" focuses
instead of spawning a duplicate instance.

Uses Notepad for the live "already running" case — it's a stock Windows app
present on every machine, harmless to open/close repeatedly, and closing it
needs no unsaved-changes confirmation for a blank document.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.registry import REGISTRY  # noqa: E402
from friday.skills import apps  # noqa: E402


def _notepad_windows() -> list[dict]:
    return [w for w in apps._windows() if w["process"].lower().startswith("notepad")]


def _calc_windows() -> list[dict]:
    return [w for w in apps._windows() if "calculator" in w["title"].lower()]


def _close(windows: list[dict]) -> None:
    import win32con
    import win32gui

    for w in windows:
        try:
            win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


async def main() -> None:
    REGISTRY.discover()
    overall = True

    print("\n--- app discovery ---\n")
    index = apps._app_index()
    print(f"  indexed {len(index)} apps")
    overall &= len(index) > 0

    for name in ("chrome", "notepad", "terminal", "vs code"):
        hit = apps.resolve_app(name)
        found = hit is not None
        print(f"  {'OK  ' if found else 'WARN'} resolve_app({name!r}) -> {hit}")
        # Not asserted into `overall`: which of these are actually installed
        # is machine-dependent (e.g. VS Code might live elsewhere); the app
        # index existing and Notepad resolving (always present on Windows)
        # are the only load-bearing checks here.

    print("\n--- case-insensitive resolution (rapidfuzz 3.x regression, see PLAN.md) ---\n")
    lower = apps.resolve_app("notepad")
    upper = apps.resolve_app("NOTEPAD")
    case_ok = lower is not None and upper is not None and lower[1] == upper[1]
    print(f"  {'OK  ' if case_ok else 'MISS'} 'notepad' and 'NOTEPAD' resolve the same -> {lower}")
    overall &= case_ok

    print("\n--- missing app fails cleanly with diagnostics, not a crash ---\n")
    result = apps.open_app("a-totally-made-up-app-xyz-123")
    missing_ok = not result.ok and "indexed_apps" in result.data
    print(f"  {'OK  ' if missing_ok else 'MISS'} unknown app -> ok={result.ok}, speech={result.speech!r}")
    overall &= missing_ok

    print("\n--- opening an already-open app focuses it instead of duplicating ---\n")
    _close(_notepad_windows())
    time.sleep(0.5)

    try:
        first = apps.open_app("notepad")
        print(f"  first open -> ok={first.ok}, already_running={first.data.get('already_running')}")
        time.sleep(1.5)  # give Notepad time to actually appear as a window

        before = len(_notepad_windows())
        second = apps.open_app("notepad")
        time.sleep(0.5)
        after = len(_notepad_windows())

        no_dup_ok = (
            first.ok and second.ok
            and second.data.get("already_running") is True
            and before >= 1 and after == before
        )
        print(
            f"  {'OK  ' if no_dup_ok else 'MISS'} second open -> "
            f"already_running={second.data.get('already_running')}, "
            f"windows before={before} after={after}"
        )
        overall &= no_dup_ok
    finally:
        _close(_notepad_windows())

    print("\n--- real end-to-end: focus genuinely switches at the OS level ---\n")
    print(
        "    Regression for the Windows foreground-lock bug: a bare\n"
        "    SetForegroundWindow called from a background thread is denied\n"
        "    (raises pywintypes.error) whenever the target window isn't\n"
        "    already frontmost — apps.open_app's own ok=True is not proof\n"
        "    of anything by itself, so this checks win32gui.GetForegroundWindow()\n"
        "    directly, the same way a human looking at the screen would.\n"
    )
    import win32gui

    _close(_notepad_windows())
    _close(_calc_windows())
    time.sleep(0.5)

    try:
        apps.open_app("notepad")
        time.sleep(1.5)
        apps.open_app("calculator")
        time.sleep(1.5)

        switch_ok = True
        for round_num in range(1, 4):
            note_result = apps.open_app("notepad")
            note_wins = _notepad_windows()
            note_focused = bool(note_wins) and win32gui.GetForegroundWindow() == note_wins[0]["hwnd"]
            print(f"  round {round_num}: -> notepad   ok={note_result.ok} actually-foreground={note_focused}")
            switch_ok &= note_result.ok and note_focused

            calc_result = apps.open_app("calculator")
            calc_wins = _calc_windows()
            calc_focused = bool(calc_wins) and win32gui.GetForegroundWindow() == calc_wins[0]["hwnd"]
            print(f"  round {round_num}: -> calculator ok={calc_result.ok} actually-foreground={calc_focused}")
            switch_ok &= calc_result.ok and calc_focused

        print(f"\n  {'OK  ' if switch_ok else 'MISS'} every round-trip actually moved OS foreground, not just claimed to")
        overall &= switch_ok
    finally:
        _close(_notepad_windows())
        _close(_calc_windows())

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
