"""Browser skills: a real, controllable Chromium session (Playwright).

Distinct from `web.*` (friday.skills.web), which fetches/opens pages without
control — no login, no clicking, no reading back what's on screen after a
click. `browser.*` is for pages you need to *use*: read what's there, click
something, fill a field, then read the result. The reusable mechanics live in
friday.browser; these skills are the permission-checked front door to it.

Consequential actions (submitting a form, sending a message) are just a
`browser.click`/`browser.type` to FRIDAY — it can't tell "click like" from
"click delete account" apart at the DOM level. That's what the confirmation
tier is for: raise the tier via `permissions.overrides` in config.yaml for
any site/flow where that distinction matters to you.
"""

from __future__ import annotations

from typing import Annotated

from friday import browser
from friday.registry import SkillResult, skill
from friday.risk import is_consequential
from friday.intent import CLICK_RULE, KEY_RULE


def _fail(exc: Exception, prefix: str) -> SkillResult:
    return SkillResult(speech=f"{prefix}: {exc}", ok=False)


@skill(
    name="browser.open",
    tier="L1",
    action="navigate",
    description=(
        "Open a URL (including file:// local files) in a real controllable browser — "
        "use this over web.fetch for local files, JS-rendered pages, logins, or anything "
        "you'll need to click/type into afterward. Follow with browser.read to get the text"
    ),
    examples=[
        "open this page so you can interact with it",
        "start a browser session on this url",
        "launch the browser and load this site",
        "open this website in your browser and read it",
        "go to this page in your automated browser",
        "pull up this site so you can click things on it",
    ],
)
async def open_page(
    url: Annotated[str, "URL or site name to open"],
) -> SkillResult:
    try:
        info = await browser.goto(url)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't start a browser")
    except browser.NavigationError as exc:
        return _fail(exc, "I couldn't get there")

    speech = f"{info.title or info.url} is already open." if info.already_loaded else f"Opened {info.title or info.url}."
    return SkillResult(
        speech=speech,
        data={"url": info.url, "title": info.title, "already_loaded": info.already_loaded},
    )


@skill(
    name="browser.read",
    tier="L0",
    description="Read the text of the page currently open in the controlled browser",
    examples=[
        "read the page you have open",
        "what does the current browser page say",
        "read what's on the browser tab",
        "what's on this page you opened",
        "read the site you're looking at",
    ],
)
async def read_page(
    limit: Annotated[int, "maximum characters to return"] = 4000,
) -> SkillResult:
    try:
        info = await browser.read_page(limit=limit)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't read the browser")
    except browser.BrowserError as exc:
        return _fail(exc, "I couldn't read that page")

    if not info.text:
        return SkillResult(speech="That page doesn't have any readable text.", ok=False)

    snippet = info.text[:400].rstrip()
    speech = f"{info.title} — {snippet}" if info.title else snippet
    return SkillResult(
        speech=speech,
        data={"url": info.url, "title": info.title, "text": info.text, "via_ocr": info.via_ocr},
    )


@skill(
    name="browser.inspect",
    tier="L0",
    description="List the clickable links, buttons, and fields on the open browser page",
    examples=[
        "what can I click on this page",
        "inspect the browser page",
        "what buttons are on this page",
        "show me the links on this page",
        "what fields can I fill in on this page",
    ],
)
async def inspect(
    limit: Annotated[int, "maximum elements to return"] = 30,
) -> SkillResult:
    try:
        elements = await browser.list_interactive(limit=limit)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't inspect the browser")
    except browser.BrowserError as exc:
        return _fail(exc, "I couldn't inspect that page")

    if not elements:
        return SkillResult(speech="I don't see anything clickable on this page.", ok=False)

    preview = ", ".join(e.text[:40] for e in elements[:8])
    return SkillResult(
        speech=f"{len(elements)} elements: {preview}.",
        data={"elements": [{"role": e.role, "text": e.text, "tag": e.tag} for e in elements]},
    )


@skill(
    name="browser.click",
    tier="L1",
    action=CLICK_RULE,
    description=(
        "Click a button, link, or element on the open browser page by its visible text. "
        "Most clicks (navigation, search, benign UI) run immediately; a click whose text "
        "implies a consequential action (send, buy, delete, publish, ...) pauses for your "
        "confirmation first — see friday.risk"
    ),
    examples=[
        "click the login button on this page",
        "click send in the browser",
        "click the search button",
        "press the submit button on this page",
        "click on the sign in link",
    ],
    dry_run=lambda target: f"Click '{target}'.",
    risk=lambda target: is_consequential(target),
)
async def click(
    target: Annotated[str, "visible text of the element to click"],
) -> SkillResult:
    try:
        clicked = await browser.click(target)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't use the browser")
    except browser.ElementNotFound as exc:
        return _fail(exc, "I couldn't find that")
    except browser.BrowserError as exc:
        return _fail(exc, "That click failed")

    return SkillResult(speech=f"Clicked {clicked}.", data={"clicked": clicked})


@skill(
    name="browser.type",
    tier="L1",
    action="modify",
    description="Type text into a field on the open browser page",
    examples=[
        "type this into the search box on the page",
        "fill in the browser search field",
        "enter this text in the message box on this page",
        "type my search into this page's search bar",
        "fill the email field on this page",
    ],
)
async def type_into(
    field: Annotated[str, "label, placeholder, or name of the field"],
    text: Annotated[str, "text to type into it"],
) -> SkillResult:
    try:
        filled = await browser.fill(field, text)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't use the browser")
    except browser.ElementNotFound as exc:
        return _fail(exc, "I couldn't find that field")
    except browser.BrowserError as exc:
        return _fail(exc, "That failed")

    return SkillResult(speech=f"Filled {filled}.", data={"field": filled})


@skill(
    name="browser.press",
    tier="L1",
    action=KEY_RULE,
    description="Press a key or keyboard shortcut on the open browser page",
    examples=[
        "press enter on this page",
        "hit enter in the browser",
        "press escape on this page",
        "press tab in the browser",
    ],
)
async def press(
    key: Annotated[str, "key or combo, e.g. 'Enter' or 'Control+A'"],
) -> SkillResult:
    try:
        await browser.press(key)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't use the browser")
    except browser.BrowserError as exc:
        return _fail(exc, "That failed")

    return SkillResult(speech=f"Pressed {key}.")


@skill(
    name="browser.close",
    tier="L1",
    action="delete",
    description="Close the controlled browser session",
    examples=[
        "close the browser session",
        "shut down the automated browser",
        "close your browser tab",
        "stop the browser",
    ],
)
async def close_browser() -> SkillResult:
    await browser.close()
    return SkillResult(speech="Browser closed.")
