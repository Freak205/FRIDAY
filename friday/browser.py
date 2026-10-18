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

import asyncio
import io
import re
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
class ClickInfo:
    """What one click did (Phase 31.0): the clicked element's own text, and the page the browser is on
    afterwards -- `navigated` when the click took it to a different URL (a link, a form's submit)."""

    label: str
    url: str = ""
    title: str = ""
    navigated: bool = False

    def __str__(self) -> str:
        return self.label


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


# Phase 31.0: how long reading a candidate's own text (before clicking it) may take. The element is
# already known to exist and be visible, so this only guards against a page that never settles.
_PRE_CLICK_READ_MS = 2000
_CLICKABLE_TAGS = frozenset({"a", "button", "summary"})
_CLICKABLE_ROLES = frozenset({"button", "link", "menuitem", "tab", "checkbox"})


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:160]


async def _offered(page: Any, *, fields: bool) -> str:
    """'. On this page: A, B, C' -- what a failed lookup could have meant (clickable things, or the
    fillable fields), so the caller can name one that exists instead of guessing again. Read-only."""
    try:
        if fields:
            names = [_field_name(f) for f in await _fields(page) if f.get("visible")]
        else:
            raw = await page.evaluate(_COLLECT_JS, _INTERACTIVE_SELECTOR)
            names = [
                " ".join(str(r.get("text") or "").split())[:40] for r in raw
                if r.get("tag") in _CLICKABLE_TAGS or r.get("role") in _CLICKABLE_ROLES
            ]
    except Exception:
        return ""
    names = [n for n in dict.fromkeys(names) if n][:8]
    if not names:
        return ""
    return (". Fields on this page: " if fields else ". On this page: ") + ", ".join(names)


async def click(target: str) -> ClickInfo:
    """Click the first visible element matching `target` (button/link text, label, ...) -- exactly once.

    Phase 31.0: FINDING the element and CLICKING it are separate steps. A candidate locator that matches
    nothing, or nothing visible, is skipped (nothing was clicked). Once an element HAS been clicked, no
    other candidate is ever tried. The old loop read the element's text AFTER clicking it; a click that
    navigates (a form's submit button, a link) made that read fail, so it reported "no clickable element"
    for a click that had happened -- the form was already submitted -- and went on to try the next
    locator on the NEW page, a second, different click. The text is now read before the click, and a
    click that took the browser elsewhere says so (`ClickInfo.navigated`, the new URL and title)."""
    page = await ensure_open()
    before = page.url
    for locator in _locator_candidates(page, target):
        first = locator.first
        try:
            if await first.count() == 0 or not await first.is_visible():
                continue
            label = " ".join(((await first.inner_text(timeout=_PRE_CLICK_READ_MS)) or "").split()) or target.strip()
            await first.scroll_into_view_if_needed(timeout=_PRE_CLICK_READ_MS)
        except Exception:
            continue  # this way of finding it did not pan out; nothing has been clicked
        try:
            await first.click()
        except Exception as exc:
            if page.url == before:
                raise BrowserError(f"couldn't click '{label[:80]}': {_short(exc)}") from exc
            # The page moved on before Playwright reported back: the click happened.
        return await _after_click(page, label, before)
    raise ElementNotFound(f"no clickable element matching '{target}'" + await _offered(page, fields=False))


async def _after_click(page: Any, label: str, before: str) -> ClickInfo:
    url = page.url
    navigated = url != before
    if navigated:
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            log.debug("load state after a navigating click not reached", exc_info=True)
    try:
        title = await page.title()
    except Exception:
        title = ""
    return ClickInfo(label=label[:120], url=url, title=title, navigated=navigated)


# Fillable controls, in document order. `_FIELDS_JS` and `page.locator(_FILLABLE_SELECTOR).nth(i)` walk
# the SAME selector, so an index found by one addresses the same element in the other.
_FILLABLE_SELECTOR = (
    "input:not([type]), input[type=text], input[type=search], input[type=email], input[type=url], "
    "input[type=tel], input[type=password], input[type=number], textarea, [contenteditable=true], [role=textbox]"
)

_FIELDS_JS = """
(sel) => {
  const out = [];
  const all = document.querySelectorAll(sel);
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
      && style.display !== 'none' && !el.disabled && !el.readOnly;
    const labels = el.labels ? Array.from(el.labels).map(l => (l.innerText || '').trim()).filter(Boolean) : [];
    let aria = el.getAttribute('aria-label') || '';
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      for (const id of by.split(/\\s+/)) {
        const n = document.getElementById(id);
        if (n) aria += ' ' + (n.innerText || '');
      }
    }
    out.push({
      i, visible, label: labels.join(' '), aria: aria.trim(),
      placeholder: el.getAttribute('placeholder') || '', name: el.getAttribute('name') || '',
      id: el.id || '', title: el.getAttribute('title') || '',
    });
  }
  return out;
}
"""

# Words that say "a field" without saying which one ("the search BOX", "the email FIELD").
_GENERIC_FIELD_WORDS = frozenset({
    "the", "a", "an", "my", "your", "this", "that", "box", "field", "input", "text", "textbox", "bar",
    "area", "entry", "form", "in", "into", "on", "of", "for", "to",
})


async def _fields(page: Any) -> list[dict]:
    return list(await page.evaluate(_FIELDS_JS, _FILLABLE_SELECTOR))


def _field_name(f: dict) -> str:
    """How a field presents itself: its label, else aria label, placeholder, name, id."""
    for key in ("label", "aria", "placeholder", "name", "id", "title"):
        text = " ".join(str(f.get(key) or "").split())
        if text:
            return text[:40]
    return ""


def _field_words(text: str) -> set[str]:
    words = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _GENERIC_FIELD_WORDS or len(w) < 2:
            continue
        words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return words


def _unique_field(target: str, fields: list[dict]) -> dict | None:
    """The ONE visible field whose own description (label, placeholder, name, id ...) shares a word with
    what was asked for, once the generic words are dropped ("the search box" -> the field whose
    placeholder is "Search products"). None when the request names nothing specific, nothing matches,
    or more than one field does -- never a guess between two."""
    wanted = _field_words(target)
    if not wanted:
        return None
    hits = [
        f for f in fields
        if f.get("visible") and wanted & _field_words(" ".join(
            str(f.get(k) or "") for k in ("label", "aria", "placeholder", "name", "id", "title")
        ))
    ]
    return hits[0] if len(hits) == 1 else None


def _attr_selector(target: str) -> str:
    """CSS for a form control whose `name` or `id` attribute is `target` (case-insensitive)."""
    v = target.strip().replace("\\", "\\\\").replace('"', '\\"')
    return ", ".join(f'{tag}[name="{v}" i], {tag}[id="{v}" i]' for tag in ("input", "textarea", "select"))


async def fill(target: str, text: str) -> str:
    """Type `text` into the first visible input/textarea matching `target`; returns the field filled.

    Matched by placeholder, label or accessible name, and (Phase 31.0) by the field's `name` / `id` --
    the skill has always described `field` as "label, placeholder, or name of the field", but a name such
    as "q" never matched anything. Failing all of those, the ONE visible field whose own description
    shares a word with the request ("the search box" -> "Search products"); never a guess between two.
    A miss names the fields that do exist. Filling replaces the field's content, so trying another way
    of finding the same field after a failed attempt cannot type anything twice."""
    page = await ensure_open()
    wanted = target.strip()
    candidates = [
        page.get_by_placeholder(wanted, exact=False),
        page.get_by_label(wanted, exact=False),
        page.get_by_role("textbox", name=wanted, exact=False),
        page.locator(_attr_selector(wanted)),
    ]
    for locator in candidates:
        first = locator.first
        try:
            if await first.count() == 0 or not await first.is_visible():
                continue
            await first.scroll_into_view_if_needed(timeout=_PRE_CLICK_READ_MS)
            await first.fill(text)
            return target
        except Exception:
            continue
    try:
        pick = _unique_field(wanted, await _fields(page))
    except Exception:
        pick = None
    if pick is not None:
        try:
            field = page.locator(_FILLABLE_SELECTOR).nth(int(pick["i"]))
            await field.scroll_into_view_if_needed(timeout=_PRE_CLICK_READ_MS)
            await field.fill(text)
            return _field_name(pick) or target
        except Exception as exc:
            raise BrowserError(f"couldn't type into '{_field_name(pick) or target}': {_short(exc)}") from exc
    raise ElementNotFound(f"no input field matching '{target}'" + await _offered(page, fields=True))


async def current_page() -> tuple[str, str] | None:
    """(url, title) of the page the controlled browser has open right now, or None when there is no
    session or only a blank tab. Phase 31.0, for the planner's tool-family check
    (`friday.toolfamily`): unlike `ensure_open` it never starts a browser and never navigates."""
    page = _state.page
    if page is None or page.is_closed():
        return None
    url = page.url or ""
    if not url or url == "about:blank":
        return None
    try:
        title = await asyncio.wait_for(page.title(), timeout=1.0)
    except Exception:
        title = ""
    return url, title


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
