"""Desktop situational awareness (Phase 9): friday.desktop_observer,
screen.observe, and the bounded ambient-context hook into plan.run/Orchestrator.

Mixes real-OS checks (this machine's actual foreground window/open windows,
same philosophy as smoke_apps.py) with fully mocked Win32/OCR/browser checks
so failure fallbacks are deterministic regardless of what's installed on the
machine running this script. Nothing here performs any action — every path
exercised is L0/read-only, matching friday.desktop_observer's own contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import desktop_observer, llm  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

# -- fakes for the Win32 layer -----------------------------------------------


class FakeWin32Gui:
    def __init__(self, windows: list[dict]) -> None:
        self.windows = windows
        self.foreground_hwnd = windows[0]["hwnd"] if windows else 0

    def GetForegroundWindow(self):
        return self.foreground_hwnd

    def GetWindowText(self, hwnd):
        return next((w["title"] for w in self.windows if w["hwnd"] == hwnd), "")

    def IsWindowVisible(self, hwnd):
        return next((w.get("visible", True) for w in self.windows if w["hwnd"] == hwnd), False)

    def EnumWindows(self, cb, extra):
        for w in self.windows:
            cb(w["hwnd"], extra)


class FakeWin32Process:
    def __init__(self, pid_of_hwnd: dict[int, int]) -> None:
        self.pid_of_hwnd = pid_of_hwnd

    def GetWindowThreadProcessId(self, hwnd):
        return (0, self.pid_of_hwnd.get(hwnd, 0))


class _FakeProc:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self):
        return self._name


class FakePsutil:
    def __init__(self, name_of_pid: dict[int, str]) -> None:
        self.name_of_pid = name_of_pid

    def Process(self, pid):
        if pid in self.name_of_pid:
            return _FakeProc(self.name_of_pid[pid])
        raise RuntimeError(f"no such process {pid}")


class FakeWin32Api:
    def GetSystemMetrics(self, index):
        return {78: 1920, 79: 1080}.get(index, 0)


@contextlib.contextmanager
def fake_win32(windows: list[dict]):
    """windows: [{"hwnd", "title", "process", "pid", "visible"?}, ...], first = foreground."""
    pid_of_hwnd = {w["hwnd"]: w["pid"] for w in windows}
    name_of_pid = {w["pid"]: w["process"] for w in windows}
    saved = {
        name: sys.modules.get(name) for name in ("win32gui", "win32process", "psutil", "win32api")
    }
    sys.modules["win32gui"] = FakeWin32Gui(windows)
    sys.modules["win32process"] = FakeWin32Process(pid_of_hwnd)
    sys.modules["psutil"] = FakePsutil(name_of_pid)
    sys.modules["win32api"] = FakeWin32Api()
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# -- fakes for the planner ----------------------------------------------------


class ScriptedPlanner(LlmProvider):
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


@contextlib.contextmanager
def scripted_provider(provider: LlmProvider):
    original = llm.get_provider
    llm.get_provider = lambda name=None: provider
    try:
        yield
    finally:
        llm.get_provider = original


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    # -- registration ---------------------------------------------------------
    print("\n--- registration ---\n")
    skill = REGISTRY.get("screen.observe")
    ok = skill is not None and skill.tier == "L0" and skill.params == []
    print(f"  {'OK  ' if ok else 'MISS'} screen.observe registered, tier={getattr(skill, 'tier', None)}, "
          f"takes no required args")
    overall &= ok

    ok = hasattr(CFG, "desktop_observer") and CFG.desktop_observer.enabled is True
    print(f"  {'OK  ' if ok else 'MISS'} desktop_observer config section present, enabled by default")
    overall &= ok

    # -- L0/read-only contract --------------------------------------------------
    print("\n--- L0 / never performs an action ---\n")
    ok = skill.tier == "L0" and skill.dry_run is None and skill.undo is None
    print(f"  {'OK  ' if ok else 'MISS'} no dry_run/undo registered — nothing here is reversible because "
          f"nothing here acts")
    overall &= ok

    # -- mocked Win32: active window + open windows -----------------------------
    print("\n--- mocked Win32: active window detection ---\n")
    windows = [
        {"hwnd": 111, "title": "untitled - Notepad", "process": "notepad.exe", "pid": 1001},
        {"hwnd": 222, "title": "FRIDAY - Visual Studio Code", "process": "Code.exe", "pid": 1002},
        {"hwnd": 333, "title": "", "process": "svchost.exe", "pid": 1003},  # no title -> excluded
        {"hwnd": 444, "title": "hidden", "process": "x.exe", "pid": 1004, "visible": False},
    ]
    with fake_win32(windows):
        obs = await desktop_observer.observe(include_screenshot=False, include_ocr=False, max_windows=10)
    ok = (
        obs.active_window_handle == 111
        and obs.active_window_title == "untitled - Notepad"
        and obs.active_app == "notepad.exe"
    )
    print(f"  {'OK  ' if ok else 'MISS'} foreground window resolved -> {obs.active_app!r}, {obs.active_window_title!r}")
    overall &= ok

    titles = {w.title for w in obs.open_windows}
    ok = titles == {"untitled - Notepad", "FRIDAY - Visual Studio Code"}
    print(f"  {'OK  ' if ok else 'MISS'} blank-title and invisible windows excluded -> {sorted(titles)}")
    overall &= ok

    ok = obs.screen_width == 1920 and obs.screen_height == 1080
    print(f"  {'OK  ' if ok else 'MISS'} screen size from GetSystemMetrics -> {obs.screen_width}x{obs.screen_height}")
    overall &= ok

    # -- bounded output: max_windows ---------------------------------------------
    print("\n--- bounded output: max_windows ---\n")
    many_windows = [
        {"hwnd": i, "title": f"Window {i}", "process": "test.exe", "pid": i} for i in range(1, 31)
    ]
    with fake_win32(many_windows):
        obs = await desktop_observer.observe(include_screenshot=False, include_ocr=False, max_windows=5)
    ok = len(obs.open_windows) == 5
    print(f"  {'OK  ' if ok else 'MISS'} 30 real windows capped to max_windows=5 -> {len(obs.open_windows)}")
    overall &= ok

    # -- import failure fallback: win32 modules entirely missing ------------------
    print("\n--- Win32 modules unavailable -> clean fallback, no crash ---\n")
    saved = {name: sys.modules.pop(name, None) for name in ("win32gui", "win32process", "psutil")}
    sys.modules["win32gui"] = None  # importing None-valued sys.modules entry raises ImportError
    sys.modules["win32process"] = None
    sys.modules["psutil"] = None
    try:
        hwnd, title, process = desktop_observer._foreground_window()
        wins = desktop_observer._open_windows(10)
        ok = hwnd == 0 and title == "" and process == "" and wins == []
        print(f"  {'OK  ' if ok else 'MISS'} missing pywin32/psutil degrades to empty results, not an exception")
        overall &= ok
    finally:
        for name in ("win32gui", "win32process", "psutil"):
            sys.modules.pop(name, None)
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod

    # -- OCR unavailable fallback --------------------------------------------------
    print("\n--- OCR unavailable -> ocr_available=False, no crash ---\n")
    from friday import ocr as _ocr

    original_check = _ocr.check_available

    def _raise_unavailable():
        raise _ocr.TesseractNotAvailable("test double: tesseract not installed")

    _ocr.check_available = _raise_unavailable
    try:
        with fake_win32(windows):
            obs = await desktop_observer.observe(include_screenshot=False, include_ocr=True)
        ok = obs.ocr_available is False and obs.visible_text == ""
        print(f"  {'OK  ' if ok else 'MISS'} OCR unavailable handled cleanly -> "
              f"ocr_available={obs.ocr_available}, visible_text={obs.visible_text!r}")
        overall &= ok
    finally:
        _ocr.check_available = original_check

    # -- bounded output: max_ocr_chars, using a fake OCR result --------------------
    print("\n--- bounded output: max_ocr_chars ---\n")
    from PIL import Image

    from friday.ocr import OcrResult

    original_capture = _ocr.capture_screen
    original_read_image = _ocr.read_image
    original_check2 = _ocr.check_available
    long_text = "word " * 500

    _ocr.check_available = lambda: "5.0.0 (test double)"
    _ocr.capture_screen = lambda region="screen": Image.new("RGB", (10, 10))
    _ocr.read_image = lambda image: OcrResult(text=long_text, words=[], width=10, height=10)
    try:
        with fake_win32(windows):
            obs = await desktop_observer.observe(include_screenshot=False, include_ocr=True, max_ocr_chars=50)
        ok = obs.ocr_available and len(obs.visible_text) <= 51 and obs.visible_text.endswith("…")
        print(f"  {'OK  ' if ok else 'MISS'} {len(long_text)}-char OCR result truncated to "
              f"{len(obs.visible_text)} chars -> {obs.visible_text!r}")
        overall &= ok
    finally:
        _ocr.check_available = original_check2
        _ocr.capture_screen = original_capture
        _ocr.read_image = original_read_image

    # -- browser: not open -> stays None, even if the foreground app is a browser --
    print("\n--- browser not open by FRIDAY -> browser field stays None ---\n")
    browser_windows = [
        {"hwnd": 999, "title": "Some Site - Google Chrome", "process": "chrome.exe", "pid": 2001},
    ]
    from friday import browser as _browser

    original_is_open = _browser.is_open
    _browser.is_open = lambda: False
    try:
        with fake_win32(browser_windows):
            obs = await desktop_observer.observe(include_screenshot=False, include_ocr=False)
        ok = obs.browser is None
        print(f"  {'OK  ' if ok else 'MISS'} chrome.exe in foreground but FRIDAY never opened a "
              f"browser session -> browser={obs.browser}")
        overall &= ok
    finally:
        _browser.is_open = original_is_open

    # -- browser: open, but reading it fails -> None, not a crash -------------------
    print("\n--- browser open but read_page fails -> clean fallback ---\n")
    original_read_page = _browser.read_page

    async def _raise_browser_error():
        raise RuntimeError("test double: page closed mid-read")

    _browser.is_open = lambda: True
    _browser.read_page = _raise_browser_error
    try:
        with fake_win32(browser_windows):
            obs = await desktop_observer.observe(include_screenshot=False, include_ocr=False)
        ok = obs.browser is None
        print(f"  {'OK  ' if ok else 'MISS'} browser.read_page raising is swallowed cleanly -> browser={obs.browser}")
        overall &= ok
    finally:
        _browser.is_open = original_is_open
        _browser.read_page = original_read_page

    # -- structured, bounded observation (real desktop) -----------------------------
    print("\n--- real desktop observation is structured and bounded ---\n")
    obs = await desktop_observer.observe(include_screenshot=False)
    d = obs.to_dict()
    payload = json.dumps(d)
    ok = (
        isinstance(d["timestamp"], str) and d["timestamp"]
        and isinstance(d["open_windows"], list)
        and len(d["open_windows"]) <= CFG.desktop_observer.max_windows
        and "screenshot_path" in d
        and len(payload) < 20_000  # bounded — never a raw dump
    )
    print(f"  {'OK  ' if ok else 'MISS'} real snapshot: active_app={d['active_app']!r}, "
          f"{len(d['open_windows'])} window(s), payload={len(payload)} chars")
    overall &= ok

    # -- screen.observe skill end to end ---------------------------------------------
    print("\n--- screen.observe via SESSION.handle ---\n")
    result = await SESSION.handle("give me a full picture of my screen", actor="text")
    ok = result.ok and result.speech and "screen_size" in result.data
    print(f"  {'OK  ' if ok else 'MISS'} -> {result.speech[:120]}")
    overall &= ok

    print("\n--- desktop_observer.enabled=False disables the skill cleanly ---\n")
    CFG.desktop_observer.enabled = False
    try:
        result = await EXECUTOR.run("screen.observe", {}, actor="test")
        ok = (not result.ok) and "disabled" in result.speech.lower()
        print(f"  {'OK  ' if ok else 'MISS'} -> {result.speech}")
        overall &= ok
    finally:
        CFG.desktop_observer.enabled = True

    # -- orchestrator integration: bounded ambient context in the planner prompt ----
    print("\n--- plan.run attaches a bounded desktop-context line to the planner prompt ---\n")
    fixed_obs = desktop_observer.DesktopObservation(
        timestamp="2026-01-01T00:00:00",
        active_app="notepad.exe",
        active_window_title="untitled - Notepad",
        active_window_handle=111,
        screen_width=1920,
        screen_height=1080,
        open_windows=[],
    )
    original_observe = desktop_observer.observe

    async def fake_observe(**kwargs):
        return fixed_obs

    desktop_observer.observe = fake_observe
    try:
        planner = ScriptedPlanner([done("Nothing else to do.")])
        with scripted_provider(planner):
            result = await EXECUTOR.run("plan.run", {"goal": "just say done, no tools needed"}, actor="test")
        prompt = planner.prompts[0] if planner.prompts else ""
        ok = (
            result.ok
            and "Current desktop context:" in prompt
            and "untitled - Notepad" in prompt
            and "notepad.exe" in prompt
        )
        print(f"  {'OK  ' if ok else 'MISS'} ambient summary present in the first planning prompt")
        overall &= ok

        print("\n--- ...but not when desktop_observer.enabled is False ---\n")
        CFG.desktop_observer.enabled = False
        planner2 = ScriptedPlanner([done("ok")])
        with scripted_provider(planner2):
            await EXECUTOR.run("plan.run", {"goal": "say ok, no tools needed"}, actor="test")
        prompt2 = planner2.prompts[0] if planner2.prompts else ""
        ok = "Current desktop context:" not in prompt2
        print(f"  {'OK  ' if ok else 'MISS'} no ambient context attached when the feature is disabled")
        overall &= ok
        CFG.desktop_observer.enabled = True
    finally:
        desktop_observer.observe = original_observe

    print("\n--- plan.run's ambient context never includes a screenshot/OCR pass ---\n")
    calls: list[dict] = []
    original_observe2 = desktop_observer.observe

    async def recording_observe(**kwargs):
        calls.append(kwargs)
        return fixed_obs

    desktop_observer.observe = recording_observe
    try:
        planner = ScriptedPlanner([done("done")])
        with scripted_provider(planner):
            await EXECUTOR.run("plan.run", {"goal": "say done, no tools needed"}, actor="test")
        ok = bool(calls) and calls[-1].get("include_screenshot") is False and calls[-1].get("include_ocr") is False
        print(f"  {'OK  ' if ok else 'MISS'} plan.run's own observe() call requests no screenshot/OCR -> {calls[-1] if calls else None}")
        overall &= ok
    finally:
        desktop_observer.observe = original_observe2

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
