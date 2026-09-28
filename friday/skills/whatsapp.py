"""WhatsApp Web skills: open the session, draft a message, and stop before
sending it.

Navigation, searching for a contact, opening the chat, and typing the draft
are all tier L1 (auto) — nothing external has happened yet; the message is
only sitting in the compose box. Sending is a separate skill, tier L3, which
FRIDAY's normal confirm policy always pauses on (see friday.permissions) —
"typing the message is not itself the consequential external commitment.
Sending it is."
"""

from __future__ import annotations

from typing import Annotated

from friday import browser, whatsapp
from friday.registry import SkillResult, skill


def _fail(exc: Exception, prefix: str) -> SkillResult:
    return SkillResult(speech=f"{prefix}: {exc}", ok=False)


@skill(
    name="whatsapp.open",
    tier="L1",
    action="open",
    description="Open WhatsApp Web in the controlled browser and check you're logged in",
    examples=[
        "open whatsapp",
        "open whatsapp web",
        "launch whatsapp",
        "start whatsapp web for me",
        "pull up whatsapp",
    ],
)
async def open_whatsapp() -> SkillResult:
    try:
        info = await whatsapp.open_whatsapp()
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't start a browser")
    except browser.NavigationError as exc:
        return _fail(exc, "I couldn't reach WhatsApp Web")
    except whatsapp.LoginRequired as exc:
        return SkillResult(speech=str(exc), ok=False, data={"login_required": True})

    return SkillResult(speech="WhatsApp Web is open and you're logged in.", data={"url": info.url})


@skill(
    name="whatsapp.compose",
    tier="L1",
    action="communicate",
    description=(
        "Find a WhatsApp contact, open the chat, and type a draft message — this does NOT "
        "send it. Follow with whatsapp.send, which always asks for confirmation first"
    ),
    examples=[
        "message raja on whatsapp: are you coming to college tomorrow",
        "text raja on whatsapp asking if he's coming to college tomorrow",
        "text priya saying i'll be late",
        "draft a whatsapp message to raja",
        "prepare a whatsapp message to my friend",
        "write a message to raja on whatsapp",
    ],
)
async def compose(
    contact: Annotated[str, "name of the WhatsApp contact/chat to message"],
    message: Annotated[str, "text to draft into the chat, not sent yet"],
) -> SkillResult:
    try:
        found = await whatsapp.compose(contact, message)
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't use the browser")
    except whatsapp.LoginRequired as exc:
        return SkillResult(speech=str(exc), ok=False, data={"login_required": True})
    except whatsapp.ContactNotFound as exc:
        return _fail(exc, "I couldn't find that contact")
    except whatsapp.WhatsAppError as exc:
        return _fail(exc, "That didn't work")

    return SkillResult(
        speech=f'Drafted a message to {found}: "{message}". Say send it when you\'re ready.',
        data={"contact": found, "message": message},
    )


@skill(
    name="whatsapp.send",
    tier="L3",
    action="communicate",
    description="Send the drafted WhatsApp message. Always asks for confirmation first",
    examples=[
        "send the whatsapp message",
        "send it",
        "go ahead and send that",
        "yes, send the message",
        "send the draft to raja",
    ],
    dry_run=lambda: whatsapp.pending_preview(),
)
async def send() -> SkillResult:
    try:
        contact = await whatsapp.send()
    except browser.BrowserNotAvailable as exc:
        return _fail(exc, "I can't use the browser")
    except whatsapp.WhatsAppError as exc:
        return _fail(exc, "I couldn't send that")

    return SkillResult(speech=f"Sent to {contact}.", data={"contact": contact})
