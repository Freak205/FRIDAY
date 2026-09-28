"""Browser automation — the reach layer for real websites.

A thin, reusable wrapper around Playwright (Chromium). Deliberately *not* a
grab-bag of site-specific automations: it exposes generic primitives —
navigate, read, inspect, click, type, press — that later FRIDAY agent
planning (friday.orchestrator) can call as tools, the same way it calls any
other skill.

Order of preference, mirroring friday.skills.ui:
  1. DOM inspection (this module, `read_page` / `list_interactive`)
  2. screen OCR fallback when the DOM has nothing (canvas-rendered pages —
     `read_page` falls back to `friday.ocr` automatically)

One Chromium instance, one persistent profile directory
(`config.browser.profile_dir`), reused across calls so a login (WhatsApp Web,
ChatGPT, ...) survives a restart. See the "Session persistence" note below for
the security tradeoff that implies.

Session persistence — what this does and doesn't do
-----------------------------------------------------
`launch_persistent_context` stores cookies/local storage/session tokens on
disk under `profile_dir`, exactly like a normal Chrome profile. FRIDAY:
  - never reads that directory's contents itself
  - never extracts cookies/tokens and never passes them to an LLM
  - only ever talks to the browser through the Playwright control channel
    (navigate/click/type/read), the same surface a screen-reader would use
That profile directory is nonetheless equivalent to being logged in as you —
treat it like any other browser profile (don't sync it, don't share the
machine's user account).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from friday import paths
from friday.log import get

log = get(__name__)

_INSTALL_HINT = (
    "Playwright's Chromium build isn't installed. Run: "
    "python -m playwright install chromium"
)


class BrowserError(Exception):
    """Base class for browser automation failures."""


class BrowserNotAvailable(BrowserError):
    """The playwright package or its browser binary isn't usable."""


class NavigationError(BrowserError):
    """A goto/navigation failed (bad URL, timeout, DNS, ...)."""


class ElementNotFound(BrowserError):
    """No element matched the requested text/label/selector."""


@dataclass(slots=True)
class PageInfo:
    url: str
    title: str
    text: str
    via_ocr: bool = False
    # Phase 12.0: True when goto() skipped a real navigation because the
    # exact normalized target URL was already loaded — see goto()'s
    # docstring for why this is an exact-match check, not a same-domain one.
    already_loaded: bool = False


@dataclass(slots=True)
class ElementInfo:
    role: str
    text: str
    tag: str = ""


@dataclass(slots=True)
class _State:
    playwright: Any = None
    context: Any = None
    page: Any = None


_state = _State()

# Elements worth surfacing to `list_interactive` / used as click/fill targets.
_INTERACTIVE_SELECTOR = (
    "a, button, input, textarea, select, summary, "
    "[role=button], [role=link], [role=textbox], [role=checkbox], "
    "[role=menuitem], [role=tab], [contenteditable=true]"
)

_COLLECT_JS = """
(sel) => {
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim();
    if (!text) continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      text: text.slice(0, 120),
    });
    if (out.length >= 60) break;
  }
  return out;
}
"""


async def _ensure_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserNotAvailable(
            "The playwright package isn't installed. Run: pip install playwright"
        ) from exc
    return async_playwright


async def ensure_open() -> Any:
    """Start Chromium (persistent profile) if it isn't already running, return the active page."""
    if _state.page is not None and not _state.page.is_closed():
        return _state.page

    async_playwright = await _ensure_playwright()
    from friday.config import CFG

    if _state.playwright is None:
        _state.playwright = await async_playwright().start()

    profile_dir = paths.ROOT / CFG.browser.profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    if _state.context is None:
        launch_kwargs: dict[str, Any] = {
            "headless": CFG.browser.headless,
            "viewport": {"width": 1280, "height": 800},
        }
        channel = (CFG.browser.channel or "").strip()
        if channel:
            try:
                _state.context = await _state.playwright.chromium.launch_persistent_context(
                    str(profile_dir), channel=channel, **launch_kwargs
                )
            except Exception as exc:
                log.warning(
                    "browser channel %r unavailable (%s); falling back to bundled Chromium",
                    channel, exc,
                )
        if _state.context is None:
            try:
                _state.context = await _state.playwright.chromium.launch_persistent_context(
                    str(profile_dir), **launch_kwargs
                )
            except Exception as exc:
                raise BrowserNotAvailable(f"{_INSTALL_HINT} ({exc})") from exc
        _state.context.set_default_navigation_timeout(CFG.browser.nav_timeout_ms)
        _state.context.set_default_timeout(CFG.browser.action_timeout_ms)

    pages = _state.context.pages
    _state.page = pages[0] if pages else await _state.context.new_page()
    return _state.page


def _normalize_url(url: str) -> str:
    target = url.strip()
    if target.startswith(("http://", "https://", "data:", "file://")):
        return target
    if "." not in target and " " not in target:
        return f"https://{target}.com"
    return "https://" + target if " " not in target else target


async def goto(url: str) -> PageInfo:
    """Navigate the current page to `url`, waiting for it to settle.

    Skips the real navigation (and reports `already_loaded=True`) when the
    exact normalized target URL is already the current page — "open
    YouTube" twice in a row does one navigation, not two. Deliberately an
    *exact* URL match, not a same-domain one: a different path/query on the
    same domain (e.g. a fresh Google search) must still navigate, or a
    same-site heuristic would silently turn "search Google for X" into a
    no-op when Google was already open. Found missing, and added, via the
    Phase 12.0 reliability harness (scenario H) — see PLAN.md Phase 12.0 §4.
    """
    page = await ensure_open()
    target = _normalize_url(url)
    if page.url == target:
        return PageInfo(url=page.url, title=await page.title(), text="", already_loaded=True)
    try:
        await page.goto(target, wait_until="domcontentloaded")
    except Exception as exc:
        raise NavigationError(f"couldn't open {target}: {exc}") from exc
    return PageInfo(url=page.url, title=await page.title(), text="")


async def read_page(limit: int = 6000) -> PageInfo:
    """Extract the page's readable text via the DOM, falling back to OCR if empty."""
    page = await ensure_open()
    title = await page.title()

    try:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
    except Exception as exc:
        raise BrowserError(f"couldn't read the page: {exc}") from exc

    text = (text or "").strip()
    if text:
        return PageInfo(url=page.url, title=title, text=text[:limit])

    # DOM had nothing readable (canvas-rendered app, heavy JS UI) — fall back
    # to a screenshot + OCR of just this page, not the whole desktop.
    from friday import ocr

    try:
        png_bytes = await page.screenshot()
        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes))
        result = ocr.read_image(image)
        return PageInfo(url=page.url, title=title, text=result.text[:limit], via_ocr=True)
    except ocr.OcrError:
        return PageInfo(url=page.url, title=title, text="", via_ocr=True)
    except Exception:
        log.exception("OCR fallback failed for %s", page.url)
        return PageInfo(url=page.url, title=title, text="")


async def list_interactive(limit: int = 30) -> list[ElementInfo]:
    """List visible clickable/fillable elements — the browser equivalent of ui.inspect."""
    page = await ensure_open()
    try:
        raw = await page.evaluate(_COLLECT_JS, _INTERACTIVE_SELECTOR)
    except Exception as exc:
        raise BrowserError(f"couldn't inspect the page: {exc}") from exc
    return [ElementInfo(role=r["role"], text=r["text"], tag=r["tag"]) for r in raw[:limit]]


def _locator_candidates(page: Any, target: str):
    """Ranked Playwright locators to try for a natural-language target string."""
    text = target.strip()
    return [
        page.get_by_role("button", name=text, exact=False),
        page.get_by_role("link", name=text, exact=False),
        page.get_by_placeholder(text, exact=False),
        page.get_by_label(text, exact=False),
        page.get_by_text(text, exact=False),
    ]


async def click(target: str) -> str:
    """Click the first visible element matching `target` (button/link text, label, ...)."""
    page = await ensure_open()
    for locator in _locator_candidates(page, target):
        try:
            first = locator.first
            if await first.count() == 0:
                continue
            await first.scroll_into_view_if_needed()
            await first.click()
            return (await first.inner_text() or target).strip()[:120]
        except Exception:
            continue
    raise ElementNotFound(f"no clickable element matching '{target}'")


async def fill(target: str, text: str) -> str:
    """Type `text` into the first visible input/textarea matching `target`."""
    page = await ensure_open()
    candidates = [
        page.get_by_placeholder(target, exact=False),
        page.get_by_label(target, exact=False),
        page.get_by_role("textbox", name=target, exact=False),
    ]
    for locator in candidates:
        try:
            first = locator.first
            if await first.count() == 0:
                continue
            await first.scroll_into_view_if_needed()
            await first.fill(text)
            return target
        except Exception:
            continue
    raise ElementNotFound(f"no input field matching '{target}'")


async def press(key: str) -> None:
    """Send a key/combo (Playwright syntax, e.g. 'Enter', 'Control+A') to the page."""
    page = await ensure_open()
    try:
        await page.keyboard.press(key)
    except Exception as exc:
        raise BrowserError(f"couldn't press '{key}': {exc}") from exc


async def close() -> None:
    """Close the browser context. Safe to call even if nothing is open."""
    if _state.context is not None:
        try:
            await _state.context.close()
        except Exception:
            log.exception("error closing browser context")
    if _state.playwright is not None:
        try:
            await _state.playwright.stop()
        except Exception:
            log.exception("error stopping playwright")
    _state.context = None
    _state.page = None
    _state.playwright = None


def is_open() -> bool:
    return _state.page is not None and not _state.page.is_closed()
