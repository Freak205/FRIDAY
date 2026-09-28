"""WhatsApp Web workflow — search a contact, open the chat, draft a message,
and stop before the one consequential step: sending it.

Built on top of `friday.browser`'s persistent Chromium session rather than a
second browser stack. The only WhatsApp-specific code here is *sequencing*
(search box -> filtered result -> compose box -> send button) — each step
tries a small, ranked list of semantic locators (role/aria-label/placeholder,
matching WhatsApp Web's current accessible markup) before giving up, the same
DOM-first philosophy as `friday.browser`. There is deliberately no OCR/pixel
fallback for contact search or the send button: guessing a screen coordinate
for "click the top search result" risks messaging the wrong person, which is
exactly the failure this module exists to prevent — if the semantic locators
don't find something, this fails cleanly instead.

No credential handling: if WhatsApp Web isn't logged in, `open_whatsapp`
raises `LoginRequired` with instructions for the user to scan the QR code
themselves. Nothing here reads or touches the persistent browser profile's
contents directly (see friday/browser.py's own note on that).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from friday import browser
from friday.log import get

log = get(__name__)

WHATSAPP_URL = "https://web.whatsapp.com"

# Text that shows up on WhatsApp Web's own pre-login screen. Checked against
# the page's visible text, not a specific selector, since that screen's exact
# markup has changed across WhatsApp's own releases more than once.
_LOGIN_MARKERS = (
    "scan the qr code",
    "use whatsapp on your computer",
    "log into whatsapp web",
    "keep me signed in",
)


class WhatsAppError(Exception):
    """Base class for WhatsApp Web automation failures."""


class LoginRequired(WhatsAppError):
    """WhatsApp Web is showing its pre-login / QR-code screen."""


class ContactNotFound(WhatsAppError):
    """No chat/contact matched the search query."""


@dataclass(slots=True)
class ComposeState:
    contact: str = ""
    message: str = ""


# Module-level: the last message drafted-but-not-sent, so the confirmation
# preview (friday.skills.whatsapp.send's dry_run) can show exactly what's
# about to go out, the way Part J's UX asks for.
_pending = ComposeState()


async def _first_usable(candidates: list[Any], timeout_ms: int = 3000) -> Any | None:
    """Return the first locator among `candidates` that actually matches something visible."""
    for locator in candidates:
        try:
            first = locator.first
            if await first.count() == 0:
                continue
            await first.wait_for(state="visible", timeout=timeout_ms)
            return first
        except Exception:
            continue
    return None


async def open_whatsapp(url: str = WHATSAPP_URL) -> browser.PageInfo:
    """Navigate to WhatsApp Web and confirm it's actually logged in.

    `url` is overridable for tests (a local mock page) — real callers should
    never pass it.
    """
    info = await browser.goto(url)
    page_text = (await browser.read_page(limit=3000)).text.lower()
    if any(marker in page_text for marker in _LOGIN_MARKERS):
        raise LoginRequired(
            "WhatsApp Web needs you to log in first. On your phone: WhatsApp > "
            "Settings > Linked Devices > Link a Device, then scan the QR code "
            "in the browser window I opened. I won't handle logins myself — "
            "once you're signed in, ask me again."
        )
    return info


async def compose(contact: str, message: str) -> str:
    """Search for `contact`, open the chat, and type `message` — never sends it.

    Returns the resolved contact/chat name that was actually opened.
    """
    page = await browser.ensure_open()

    search_box = await _first_usable([
        page.get_by_role("textbox", name="Search input textbox"),
        page.get_by_placeholder("Search or start a new chat", exact=False),
        page.locator("div[contenteditable='true'][data-tab='3']"),
        page.locator("[aria-label='Search input textbox']"),
    ])
    if search_box is None:
        raise WhatsAppError(
            "couldn't find WhatsApp's search box — the page layout may have changed"
        )

    await search_box.click()
    await search_box.fill(contact)
    await page.wait_for_timeout(500)  # let the chat list finish filtering

    # Deliberately scoped to list-shaped result containers only — no bare
    # `get_by_text(contact)` fallback here. An unscoped text search can match
    # the search box itself (its own contenteditable content is now the
    # query text), silently "opening" nothing and risking a click on the
    # wrong element. Failing to find a scoped match raises ContactNotFound
    # instead, per this module's fail-safe-over-fail-clever policy.
    result_row = await _first_usable([
        page.get_by_role("listitem").filter(has_text=contact),
        page.locator("[aria-label='Search results']").get_by_text(contact, exact=False),
        page.locator("[aria-label='Chat list']").get_by_text(contact, exact=False),
    ])
    if result_row is None:
        raise ContactNotFound(
            f"no WhatsApp chat matching '{contact}' — check the name and that "
            "you've messaged them before, or start the chat yourself first"
        )
    await result_row.click()
    await page.wait_for_timeout(400)  # let the conversation pane finish opening

    compose_box = await _first_usable([
        page.get_by_role("textbox", name="Type a message"),
        page.get_by_placeholder("Type a message", exact=False),
        page.locator("div[contenteditable='true'][data-tab='10']"),
        page.locator("[aria-label='Type a message']"),
    ])
    if compose_box is None:
        raise WhatsAppError(
            "opened the chat but couldn't find the message box — nothing was typed"
        )
    await compose_box.click()
    await compose_box.fill(message)

    global _pending
    _pending = ComposeState(contact=contact, message=message)
    return contact


async def send() -> str:
    """Click WhatsApp's Send button. The confirmation boundary lives one layer
    up, in friday.skills.whatsapp.send (tier L3) — by the time this runs, a
    human has already approved it."""
    page = await browser.ensure_open()

    send_btn = await _first_usable([
        page.get_by_role("button", name="Send"),
        page.locator("[aria-label='Send']"),
        page.locator("button[data-tab='11']"),
    ])
    if send_btn is None:
        raise WhatsAppError("couldn't find the Send button — nothing was sent")

    await send_btn.click()
    return _pending.contact


def pending_preview() -> str:
    """Human-readable description of the drafted-but-unsent message, for the
    confirmation prompt shown before `send` actually runs."""
    if not _pending.contact:
        return "Send the drafted WhatsApp message"
    return f'Send this WhatsApp message to {_pending.contact}: "{_pending.message}"'
