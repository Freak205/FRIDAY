"""Phase 31.0 -- tool-family discipline: a page in FRIDAY's browser vs a desktop window.

The Phase 29/30 benchmark's largest remaining failure class: after `browser.open` had put a page in
FRIDAY's own controlled browser, the planner reached for the DESKTOP tools -- `ui.fill`, `ui.click`,
`ui.inspect(app="chrome")` -- usually with no window named. Those act on whatever desktop window is
in front: with a headless browser never the page, with a headed one the browser window's
accessibility tree instead of the page's DOM, and in both cases around `browser.click`'s
consequential-click confirmation (`ui.click` has no such check). The local 3B planner matches the
user's verb ("fill in", "click") to the tool whose name says it, across families.

This module is the small deterministic piece that knows which family a call belongs to and what the
run is working on. Like `friday.intent` it adds no authority and removes none from the permission
system; it only decides which tool FAMILY may act on the page the request is about:

  * `browser_context` -- the run is working on a page in FRIDAY's controlled browser: this run used
    that browser successfully, or a page is still open from earlier AND the user's own words are
    about a web page. Read from this run's observations and (production only) the browser module's
    own state; it never opens, navigates or reads a page itself.
  * `hide_desktop_ui` -- prompt / JSON-Schema shaping only: in that context, and while nothing in
    the request or the run points at a desktop window, the planner is not shown the desktop-window
    tools at all (measured in Phase 20: hiding a tool works where telling the model does not).
  * `check_call` -- the authority, applied to every decision: in that context a desktop-window call
    that would act on "whatever is in front" (no window named, a deictic one, or the browser itself)
    is not run. The planner is told which browser tool does the same thing. A call that names a real
    desktop window is left alone -- that is a deliberate desktop target, not a family mix-up.

With no controlled-browser page in play nothing here applies: desktop work, and a web task whose
browser could not start (the planner may then fall back to the desktop tools), behave as before.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# FRIDAY's own controlled-browser tools that leave a page open to work on.
BROWSER_PAGE_TOOLS = frozenset({
    "browser.open", "browser.click", "browser.type", "browser.press", "browser.read", "browser.inspect",
})
# Desktop-window tools that take `app` (blank = the window in front) ...
WINDOW_TOOLS = frozenset({
    "ui.click", "ui.fill", "ui.right_click", "ui.drag", "ui.scroll", "ui.read", "ui.inspect",
})
# ... and ones that always act on whatever has the keyboard focus / is on the screen.
FOCUSED_TOOLS = frozenset({"input.type", "input.hotkey", "screen.click_text"})
DESKTOP_UI_TOOLS = WINDOW_TOOLS | FOCUSED_TOOLS

# Calls that show a desktop window is part of THIS run's work (not just the page).
_DESKTOP_RUN_TOOLS = frozenset({
    "apps.open", "apps.focus", "window.snap", "window.maximize", "window.minimize", "window.restore",
})

# `app` values that do not name a window: the planner means "the one in front".
_DEICTIC = frozenset({
    "", "this", "that", "it", "here", "current", "active", "foreground", "front", "focused",
    "this window", "that window", "the window", "current window", "the current window", "active window",
    "the active window", "focused window", "the focused window", "foreground window", "the foreground window",
})
# `app` values that name the browser itself (its window, not the page in it).
_BROWSER_APP_WORDS = frozenset({
    "browser", "chrome", "chromium", "edge", "msedge", "firefox", "brave", "opera", "playwright",
})

# The user's own words are about a web page.
_URL_RE = re.compile(
    r"\b(?:https?://|www\.)\S+"
    r"|\blocalhost(?::\d+)?\b"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|ai|edu|gov|app|info)\b",
    re.IGNORECASE,
)
_WEB_WORDS = frozenset({
    "browser", "website", "websites", "webpage", "webpages", "site", "sites", "web", "url", "urls",
    "link", "links", "hyperlink",
})
_PAGE_RE = re.compile(r"\bpages?\b(?!\s*(?:up|down)\b)", re.IGNORECASE)  # "the page", not a "Page down" button
# Wording that makes a request a web-page task before any page is open: an address, or the browser / a site
# named outright ("page" and "link" alone are too often a document's or an app's).
_STRONG_WEB_WORDS = frozenset({"browser", "website", "websites", "webpage", "webpages", "site", "sites"})
_WEB_PAGE_RE = re.compile(r"\bweb\s+pages?\b", re.IGNORECASE)
# ...unless the user asks for THEIR OWN browser, which FRIDAY can only reach through the desktop tools.
_OWN_BROWSER_RE = re.compile(
    r"\b(?:my|default|my own|my default)\s+(?:web\s+)?(?:browser|chrome|edge|firefox)\b", re.IGNORECASE,
)
_FIRST_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+[^\s.,;:!?)\]'\"]", re.IGNORECASE)
# The user's own words point at a desktop window / app.
_DESKTOP_WORDS = frozenset({
    "window", "windows", "app", "apps", "application", "applications", "program", "programs",
    "desktop", "dialog", "dialogue", "taskbar", "explorer",
})


@dataclass(frozen=True)
class BrowserContext:
    """The page the run is working on is in FRIDAY's controlled browser. `url`/`title` are
    whatever the browser last reported (untrusted page content: used for matching only, never
    put into a prompt). `source`: "run" (this run used the browser) or "live" (a page left open
    earlier, and the request is about a web page)."""

    url: str = ""
    title: str = ""
    source: str = "run"  # "run" | "live" | "request" (the request is a web task; no page is open yet)


@dataclass(frozen=True)
class FamilyVerdict:
    ok: bool
    reason: str = ""
    suggestion: str = ""  # the browser call that does the same thing on the page


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def mentions_web(text: str) -> bool:
    """Do the user's own words refer to a web page (a URL or domain, "in the browser", "this site",
    "the page", "the link")? Deterministic, from the text alone."""
    body = text or ""
    return bool(_URL_RE.search(body) or (_words(body) & _WEB_WORDS) or _PAGE_RE.search(body))


def names_url(text: str) -> bool:
    """Does the request name its own URL / domain (so it will open that page itself)?"""
    return bool(_URL_RE.search(text or ""))


def is_web_request(text: str) -> bool:
    """Is the request itself a web-page task, before any page is open? It names an address, or the
    browser / a website outright -- and does not ask for the user's OWN or default browser (which FRIDAY
    can only operate through the desktop tools)."""
    body = text or ""
    if _OWN_BROWSER_RE.search(body):
        return False
    return bool(names_url(body) or (_words(body) & _STRONG_WEB_WORDS) or _WEB_PAGE_RE.search(body))


def first_url(text: str) -> str:
    m = _FIRST_URL_RE.search(text or "")
    return m.group(0) if m else ""


def _not_executed(o: Any) -> bool:
    from friday.orchestrator import NOT_EXECUTED_ERRORS

    return o.error in NOT_EXECUTED_ERRORS


def names_browser(app: str, ctx: BrowserContext | None = None) -> bool:
    """Does this `app` value point at the browser window itself (or, given the page title, at the
    page) rather than at a desktop app? "chrome", "Google Chrome", "msedge.exe", "the browser"..."""
    a = " ".join((app or "").lower().split())
    if _words(a) & _BROWSER_APP_WORDS:
        return True
    title = " ".join(((ctx.title if ctx else "") or "").lower().split())
    return bool(len(a) >= 3 and title and (a in title or title in a))


def untargeted(tool: str, args: dict[str, Any] | None, ctx: BrowserContext | None = None) -> bool:
    """Would this desktop-window call act on "whatever is in front" rather than on a window it names?
    Always for the focused-input tools; for a `ui.*` tool when `app` is blank, deictic ("this window")
    or the browser itself."""
    if tool in FOCUSED_TOOLS:
        return True
    if tool not in WINDOW_TOOLS:
        return False
    app = " ".join(str((args or {}).get("app") or "").lower().split())
    return app in _DEICTIC or names_browser(app, ctx)


def browser_context(
    goal: str, observations: Iterable[Any], live: tuple[str, str] | None = None,
) -> BrowserContext | None:
    """Is the run working on a page in FRIDAY's controlled browser? Yes when THIS run opened a page
    (and did not close it since) -- or used the browser successfully while the user's own words are
    about a web page -- or when `live` -- the page the browser module still has open, production only
    -- is there and the user's own words are about a web page without naming a URL of their own (a
    follow-up such as "click the Login link on that page"; a request that names a URL opens it first,
    and from then on this run's own browser step decides). A browser call that failed, was refused or
    never ran (no browser could start) establishes nothing, and neither does merely reading a page left
    open by something else on a request that is not about the web, so the desktop tools stay available."""
    page: BrowserContext | None = None
    opened = False
    no_session = False  # FRIDAY's browser could not open a page, or the planner went to the default browser
    for o in observations:
        if _not_executed(o):
            continue
        tool = o.step.tool
        if tool == "browser.open" and not o.ok:
            no_session = True
        if tool == "web.open" and o.ok:
            no_session = True
        if not o.ok:
            continue
        if tool == "browser.close":
            page, opened = None, False
        elif tool in BROWSER_PAGE_TOOLS:
            opened = opened or tool == "browser.open"
            data = o.data or {}
            page = BrowserContext(
                url=str(data.get("url") or (page.url if page else "")),
                title=str(data.get("title") or (page.title if page else "")),
                source="run",
            )
    # Reading or clicking whatever page happened to be open does not make a request about the web: found
    # by the Phase 31 benchmark run -- "Open Calculator and add 7 and 3" read a page left over from an
    # earlier task, and the desktop tools it then needed were treated as the wrong family. This run must
    # have OPENED the page itself, or the user's own words must be about a web page.
    if page is not None and not (opened or mentions_web(goal)):
        page = None
    if page is not None:
        if live is not None:
            page = BrowserContext(url=live[0] or page.url, title=live[1] or page.title, source="run")
        return page
    if live is not None and mentions_web(goal) and not names_url(goal):
        return BrowserContext(url=live[0], title=live[1], source="live")
    # Phase 31.0, found by the final benchmark run: a request that is itself a web-page task ("Open
    # http://.../form in the browser, fill in ...") is one before its page is open -- the planner's FIRST
    # decision went to `apps.open('chrome')` or to `ui.click` on whatever window was in front. Until FRIDAY's
    # browser fails to open the page (the desktop fallback then stays available) the request is the context.
    if not no_session and is_web_request(goal):
        return BrowserContext(url=first_url(goal), title="", source="request")
    return None


def desktop_in_play(goal: str, observations: Iterable[Any]) -> bool:
    """Does the request, or the run so far, point at a desktop window as well as the page? The user
    named a window / app / the desktop, or this run already opened or focused an app, arranged a
    window, or acted on a window it named."""
    if _words(goal) & _DESKTOP_WORDS:
        return True
    for o in observations:
        if _not_executed(o) or not o.ok:
            continue
        tool = o.step.tool
        if tool in _DESKTOP_RUN_TOOLS:
            return True
        if tool in WINDOW_TOOLS and not untargeted(tool, o.step.args):
            return True
    return False


def hide_desktop_ui(ctx: BrowserContext | None, goal: str, observations: Iterable[Any]) -> bool:
    """Prompt shaping: leave the desktop-window tools out of what the planner is shown? Only while
    the run is working on a browser page and nothing points at a desktop window. `check_call` stays
    the authority either way."""
    return ctx is not None and not desktop_in_play(goal, observations)


def is_desktop_ui(tool: str) -> bool:
    return tool in DESKTOP_UI_TOOLS


def _q(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value if value is not None else "").split())[:limit]
    return "'" + text.replace("'", "\\'") + "'"


def browser_equivalent(tool: str, args: dict[str, Any] | None) -> str:
    """The controlled-browser call that does what this desktop-window call meant, on the page."""
    a = args or {}
    if tool in ("ui.click", "screen.click_text"):
        return f"browser.click(target={_q(a.get('label') or a.get('query'))})"
    if tool == "ui.fill":
        return f"browser.type(field={_q(a.get('field'))}, text={_q(a.get('text'))})"
    if tool == "input.type":
        return f"browser.type(field=<the field's label>, text={_q(a.get('text'))})"
    if tool == "input.hotkey":
        return f"browser.press(key={_q(a.get('combo'))})"
    if tool == "ui.read":
        return "browser.read()"
    if tool == "ui.inspect":
        return "browser.inspect()"
    if tool == "ui.scroll":
        up = str(a.get("direction") or "down").strip().lower().startswith("up")
        return f"browser.press(key='{'PageUp' if up else 'PageDown'}')"
    return "browser.inspect() to see what the page offers, then browser.click"


def check_call(ctx: BrowserContext | None, tool: str, args: dict[str, Any] | None = None) -> FamilyVerdict:
    """Does this call act on what the run is working on? Only ever rejects a desktop-window call
    that would act on "whatever is in front" while the request is about a page in FRIDAY's own
    browser; everything else -- including every call when no browser page is in play, and any
    `ui.*` call that names a real desktop window -- passes. Never consulted for authority: the
    intent guard and the permission system decide that, before and after this."""
    if ctx is None:
        return FamilyVerdict(True)
    opening = f"browser.open(url={_q(ctx.url, 120)})" if ctx.url else "browser.open(url=<the page's address>)"
    if tool == "apps.open" and names_browser(str((args or {}).get("app") or "")):
        # Launching a browser APP for a page FRIDAY works on in its own controlled browser: the page would
        # open in a window the browser tools cannot reach. (Only `apps.open`: focusing a window is harmless.)
        suggestion = opening if ctx.source == "request" else "the browser.* tools on the page that is already open"
        reason = (
            f"wrong_tool_family: '{tool}' was not run. FRIDAY opens and works on web pages in its own controlled "
            f"browser, not in a separately launched browser app the browser tools cannot reach. Use {suggestion}."
        )
        return FamilyVerdict(False, reason, suggestion)
    if tool not in DESKTOP_UI_TOOLS or not untargeted(tool, args, ctx):
        return FamilyVerdict(True)
    suggestion = browser_equivalent(tool, args)
    if ctx.source == "request":
        suggestion = f"{opening} first, then {suggestion}"
    if tool in FOCUSED_TOOLS:
        how = "they act on whichever desktop window has the keyboard focus or is on the screen"
    elif names_browser(str((args or {}).get("app") or ""), ctx):
        how = "the browser's window is not the page: its desktop controls are not the page's links, buttons and fields"
    else:
        how = "with no window named they act on whatever desktop window is in front"
    where = (
        "is a web page, which belongs in FRIDAY's own controlled browser (it is not open yet)"
        if ctx.source == "request" else "is open in FRIDAY's own controlled browser"
    )
    reason = (
        f"wrong_tool_family: '{tool}' was not run. The page this request is about {where}; the desktop "
        f"ui.*/input.* tools cannot see or reach it -- {how}. Use {suggestion}. To act on a desktop app "
        "instead, use a ui.* tool with app= naming its window."
    )
    return FamilyVerdict(False, reason, suggestion)


def prompt_line(hidden: bool, source: str = "run") -> str:
    """One system-prompt sentence for a run working on a page in FRIDAY's browser. Deliberately
    carries nothing from the page itself (its title and URL are untrusted content; a page this run
    opened is already shown, as data, in the step history)."""
    tools = (
        "browser.read (its text), browser.inspect (its links, buttons and fields), browser.click, "
        "browser.type and browser.press"
    )
    if source == "request":
        line = (
            "This request is about a web page. Open it in FRIDAY's own controlled browser with browser.open, "
            f"then work on it only with the browser tools: {tools}."
        )
    else:
        where = "(see the browser steps above)" if source == "run" else "(browser.read shows what it says)"
        line = f"A web page is open in FRIDAY's own controlled browser {where}. Work on that page only with the browser tools: {tools}."
    if not hidden:
        line += (
            " The ui.* and input.* tools act on desktop app windows, never on this page: use a ui.* tool "
            "only with app= naming a desktop window."
        )
    return line


async def live_page() -> tuple[str, str] | None:
    """(url, title) of the page FRIDAY's controlled browser has open right now, or None. Never
    starts a browser, never navigates, never reads the page's text (`friday.browser.current_page`)."""
    try:
        from friday import browser

        return await browser.current_page()
    except Exception:
        return None
