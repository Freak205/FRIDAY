"""Desktop situational awareness — a bounded, read-only snapshot of what's
currently on screen.

Phase 9. Composes capabilities FRIDAY already has — Win32 window metadata
(the same enumeration pattern as `friday.skills.apps._windows`), the
screenshot/OCR pipeline (`friday.ocr`), and the browser automation layer
(`friday.browser`) — into one structured `DesktopObservation` that answers
"what's on my screen" / "what am I looking at" style questions and gives the
planner (`friday.orchestrator`) ambient grounding for goals like "continue
where I left off."

Strictly L0/read-only: nothing in this module moves the mouse, sends input,
focuses a window, or performs any action — see friday.permissions for the
tier system this respects. Every field is bounded (window count, OCR
character count, browser text length) so a single observation can never turn
into an oversized dump in a planner prompt or a spoken response. Screenshots
are saved locally under data/cache like `screen.capture` already does and are
never transmitted anywhere; OCR and browser reads happen entirely on this
machine through the existing local `friday.ocr` / `friday.browser` modules.

No vision model is used here — every field below is deterministic (Win32
metadata, Tesseract OCR, Playwright DOM text). A local VLM could later plug
in as an additional, optional field without changing this module's shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from friday.log import get

log = get(__name__)

# Foreground process names recognized as "a browser FRIDAY might have opened
# itself" — used only to decide whether it's worth asking friday.browser for
# its state, not to special-case any particular site or workflow.
_BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "chromium.exe"}

# SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN — full multi-monitor desktop size.
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int = 0


@dataclass(slots=True)
class BrowserState:
    url: str = ""
    title: str = ""
    text: str = ""
    via_ocr: bool = False


@dataclass(slots=True)
class DesktopObservation:
    """One bounded, structured snapshot of the current desktop state."""

    timestamp: str
    active_app: str = ""
    active_window_title: str = ""
    active_window_handle: int = 0
    screen_width: int = 0
    screen_height: int = 0
    open_windows: list[WindowInfo] = field(default_factory=list)
    visible_text: str = ""
    ocr_available: bool = False
    screenshot_path: str = ""
    browser: BrowserState | None = None

    def summary(self) -> str:
        """Short natural-language description — safe to speak or hand to an LLM."""
        parts: list[str] = []
        if self.active_window_title:
            where = f"You're in {self.active_window_title[:80]}"
            where += f" ({self.active_app})." if self.active_app else "."
            parts.append(where)
        elif self.active_app:
            parts.append(f"Active app: {self.active_app}.")
        else:
            parts.append("Nothing seems to be focused right now.")

        if self.browser is not None and (self.browser.title or self.browser.url):
            parts.append(f"Browser: {self.browser.title[:60] or self.browser.url}.")

        if self.visible_text:
            snippet = " ".join(self.visible_text.split())
            if len(snippet) > 200:
                snippet = snippet[:200].rstrip() + "…"
            parts.append(f"Visible text: {snippet}")

        parts.append(f"{len(self.open_windows)} window(s) open.")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Structured, bounded representation — never the raw screenshot bytes."""
        return {
            "timestamp": self.timestamp,
            "active_app": self.active_app,
            "active_window_title": self.active_window_title,
            "active_window_handle": self.active_window_handle,
            "screen_size": {"width": self.screen_width, "height": self.screen_height},
            "visible_text": self.visible_text,
            "ocr_available": self.ocr_available,
            "open_windows": [
                {"hwnd": w.hwnd, "title": w.title, "process": w.process, "pid": w.pid}
                for w in self.open_windows
            ],
            "browser": (
                {
                    "url": self.browser.url,
                    "title": self.browser.title,
                    "text": self.browser.text,
                    "via_ocr": self.browser.via_ocr,
                }
                if self.browser is not None
                else None
            ),
            "screenshot_path": self.screenshot_path,
        }


def _foreground_window() -> tuple[int, str, str]:
    """(hwnd, title, process name). Best-effort — empty/zero on any failure."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return 0, "", ""

    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception:
        return 0, "", ""

    title = ""
    process = ""
    try:
        title = win32gui.GetWindowText(hwnd) if hwnd else ""
    except Exception:
        pass
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid).name()
    except Exception:
        pass
    return hwnd, title, process


def _screen_size() -> tuple[int, int]:
    try:
        import win32api

        return (
            win32api.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
            win32api.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
        )
    except Exception:
        return 0, 0


def _open_windows(max_windows: int) -> list[WindowInfo]:
    """Enumerate visible top-level windows — same pattern as friday.skills.apps._windows."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return []

    found: list[WindowInfo] = []

    def cb(hwnd: int, _: Any) -> None:
        if len(found) >= max_windows:
            return
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return
        if not title.strip():
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid).name()
        except Exception:
            pid, proc = 0, ""
        found.append(WindowInfo(hwnd=hwnd, title=title, process=proc, pid=pid))

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        log.exception("desktop_observer: window enumeration failed")
    return found[:max_windows]


def _capture_for_observation(save: bool) -> tuple[Any, str]:
    """Grab one screenshot for OCR/saving, reusing friday.ocr's capture path.

    Returns (PIL.Image | None, screenshot_path). A capture failure degrades
    to (None, "") rather than raising — an observation should never crash
    just because the screen couldn't be grabbed at that instant.
    """
    from friday import ocr

    try:
        image = ocr.capture_screen("screen")
    except ocr.ScreenCaptureFailed:
        return None, ""

    path = ""
    if save:
        from friday import paths

        paths.ensure()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        out = paths.CACHE / f"observe-{stamp}.png"
        try:
            image.save(out, "PNG")
            path = str(out)
        except Exception:
            log.exception("desktop_observer: failed to save screenshot")
    return image, path


def _ocr_text(image: Any, max_chars: int) -> tuple[str, bool]:
    """(text, ocr_available). available=False only means Tesseract itself
    can't be used — an available-but-empty screen still reports True."""
    from friday import ocr

    try:
        ocr.check_available()
    except ocr.OcrError:
        return "", False

    if image is None:
        return "", True

    try:
        result = ocr.read_image(image)
    except ocr.OcrError:
        return "", True

    text = result.text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text, True


async def _browser_state() -> BrowserState | None:
    """FRIDAY's own controlled browser page, only if it's the one in front.

    Deliberately scoped to the browser session `friday.browser` itself
    opened — never inspects an unrelated Chrome/Edge window just because its
    process name matches; that would surface tabs FRIDAY never touched.
    """
    from friday import browser

    if not browser.is_open():
        return None
    try:
        info = await browser.read_page()
    except Exception:
        log.exception("desktop_observer: browser state read failed")
        return None
    return BrowserState(url=info.url, title=info.title, text=info.text[:2000], via_ocr=info.via_ocr)


async def observe(
    *,
    include_screenshot: bool | None = None,
    include_ocr: bool | None = None,
    max_ocr_chars: int | None = None,
    max_windows: int | None = None,
) -> DesktopObservation:
    """Collect one bounded, read-only snapshot of the current desktop.

    Any parameter left as None falls back to `config.yaml`'s `desktop_observer`
    section (see friday.config.DesktopObserverConfig) — pass explicit values
    (as the tests do) to exercise a specific combination regardless of config.
    """
    from friday.config import CFG

    cfg = CFG.desktop_observer
    include_screenshot = cfg.include_screenshot if include_screenshot is None else include_screenshot
    include_ocr = cfg.include_ocr if include_ocr is None else include_ocr
    max_ocr_chars = cfg.max_ocr_chars if max_ocr_chars is None else max_ocr_chars
    max_windows = cfg.max_windows if max_windows is None else max_windows

    hwnd, title, process = _foreground_window()
    windows = _open_windows(max_windows)

    image = None
    screenshot_path = ""
    if include_screenshot or include_ocr:
        image, screenshot_path = _capture_for_observation(include_screenshot)

    width, height = _screen_size()
    if width == 0 and image is not None:
        width, height = image.width, image.height

    visible_text = ""
    ocr_available = False
    if include_ocr:
        visible_text, ocr_available = _ocr_text(image, max_ocr_chars)

    browser_state = None
    if process.lower() in _BROWSER_PROCESS_NAMES:
        browser_state = await _browser_state()

    return DesktopObservation(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        active_app=process,
        active_window_title=title,
        active_window_handle=hwnd,
        screen_width=width,
        screen_height=height,
        open_windows=windows,
        visible_text=visible_text,
        ocr_available=ocr_available,
        screenshot_path=screenshot_path,
        browser=browser_state,
    )
