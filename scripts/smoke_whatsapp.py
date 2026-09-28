"""WhatsApp Web workflow: search -> open chat -> draft -> STOP before send.

Everything here runs against a local mock page built to mirror WhatsApp
Web's actual accessible markup (role=textbox search box, aria-label results,
a contenteditable compose box, an aria-label="Send" button) — never the real
site, never real credentials, never a real message. Two layers, mirroring
smoke_browser.py + smoke_action_risk.py:

  A. friday.whatsapp's automation functions directly against the mock page
     (proves the search/open-chat/compose/send sequencing actually works).
  B. friday.skills.whatsapp through the real EXECUTOR with friday.whatsapp
     monkeypatched (proves the permission tier — L1 draft, L3 confirm-gated
     send — without needing a live browser for this half).
  C. the same, but driven by Orchestrator.run_goal with a scripted planner —
     the actual "open WhatsApp and prepare a message... STOP before send"
     multi-step shape from PLAN.md's Part G, benign steps auto, send gated.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser, whatsapp  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

_CHAT_PAGE = """
<html><head><title>Mock WhatsApp Web</title></head><body>
<div role="textbox" aria-label="Search input textbox" contenteditable="true" id="search"></div>
<div id="results">
  <div role="listitem"><span>Raja</span></div>
  <div role="listitem"><span>Priya</span></div>
</div>
<div id="chat" style="display:none">
  <div role="textbox" aria-label="Type a message" contenteditable="true" id="compose"></div>
  <button aria-label="Send" id="sendbtn">Send</button>
  <div id="sent-log"></div>
</div>
<script>
document.getElementById('search').addEventListener('input', () => {
  const q = document.getElementById('search').innerText.toLowerCase();
  document.querySelectorAll('#results [role=listitem]').forEach(el => {
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});
document.querySelectorAll('#results [role=listitem]').forEach(el => {
  el.addEventListener('click', () => { document.getElementById('chat').style.display = 'block'; });
});
document.getElementById('sendbtn').addEventListener('click', () => {
  const msg = document.getElementById('compose').innerText;
  document.getElementById('sent-log').innerText = 'SENT:' + msg;
});
</script>
</body></html>
""".strip()

_LOGIN_PAGE = """
<html><head><title>WhatsApp</title></head>
<body><h1>Use WhatsApp on your computer</h1><p>Scan the QR code to log in.</p></body></html>
""".strip()

MATCH_CASES = [
    ("open whatsapp web", "whatsapp.open"),
    ("message raja on whatsapp: are you free tomorrow", "whatsapp.compose"),
    ("send the whatsapp message", "whatsapp.send"),
]


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    CFG.browser.headless = True
    CFG.browser.channel = ""

    print("\n--- registration ---\n")
    for name in ("whatsapp.open", "whatsapp.compose", "whatsapp.send"):
        registered = REGISTRY.get(name) is not None
        print(f"  {'OK  ' if registered else 'MISS'} {name} registered")
        overall &= registered

    print("\n--- intent matching ---\n")
    match_ok = 0
    for utterance, expected in MATCH_CASES:
        u = BRAIN.understand(utterance)
        good = u.skill == expected
        match_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance:48} -> {u.skill} ({u.score:.2f})")
    print(f"\n  {match_ok}/{len(MATCH_CASES)} correct")
    overall &= match_ok == len(MATCH_CASES)

    # -- A. real automation against the mock page ---------------------------
    print("\n--- A: search -> open chat -> compose -> send, against a mock page ---\n")
    tmp_profile = tempfile.mkdtemp(prefix="friday-wa-test-")
    CFG.browser.profile_dir = tmp_profile
    try:
        info = await whatsapp.open_whatsapp(url=_data_url(_CHAT_PAGE))
        opened_ok = info.title == "Mock WhatsApp Web"
        print(f"  {'OK  ' if opened_ok else 'MISS'} open_whatsapp() -> title={info.title!r}")
        overall &= opened_ok

        contact = await whatsapp.compose("Raja", "are you coming to college tomorrow")
        page = await browser.ensure_open()
        drafted = await page.evaluate("() => document.getElementById('compose').innerText")
        compose_ok = contact == "Raja" and drafted == "are you coming to college tomorrow"
        print(f"  {'OK  ' if compose_ok else 'MISS'} compose() -> contact={contact!r} drafted={drafted!r}")
        overall &= compose_ok

        pre_send = await page.evaluate("() => document.getElementById('sent-log').innerText")
        not_sent_yet = pre_send == ""
        print(f"  {'OK  ' if not_sent_yet else 'MISS'} nothing sent before send() is called -> log={pre_send!r}")
        overall &= not_sent_yet

        sent_to = await whatsapp.send()
        post_send = await page.evaluate("() => document.getElementById('sent-log').innerText")
        send_ok = sent_to == "Raja" and post_send == "SENT:are you coming to college tomorrow"
        print(f"  {'OK  ' if send_ok else 'MISS'} send() -> {sent_to!r}, page log now {post_send!r}")
        overall &= send_ok

        preview = whatsapp.pending_preview()
        preview_ok = "Raja" in preview and "are you coming to college tomorrow" in preview
        print(f"  {'OK  ' if preview_ok else 'MISS'} pending_preview() names recipient+message -> {preview!r}")
        overall &= preview_ok
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile, ignore_errors=True)

    # -- login-required detection --------------------------------------------
    print("\n--- login-required page is detected and fails cleanly ---\n")
    tmp_profile2 = tempfile.mkdtemp(prefix="friday-wa-test2-")
    CFG.browser.profile_dir = tmp_profile2
    try:
        try:
            await whatsapp.open_whatsapp(url=_data_url(_LOGIN_PAGE))
            print("  FAIL expected LoginRequired, none raised")
            overall = False
        except whatsapp.LoginRequired as exc:
            login_ok = "log in" in str(exc).lower() or "scan" in str(exc).lower()
            print(f"  {'OK  ' if login_ok else 'MISS'} LoginRequired raised with guidance -> {str(exc)[:90]}")
            overall &= login_ok
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile2, ignore_errors=True)

    # -- contact not found: fails safely, never guesses ----------------------
    print("\n--- unknown contact fails safely (never guesses/sends to the wrong person) ---\n")
    tmp_profile3 = tempfile.mkdtemp(prefix="friday-wa-test3-")
    CFG.browser.profile_dir = tmp_profile3
    try:
        await whatsapp.open_whatsapp(url=_data_url(_CHAT_PAGE))
        try:
            await whatsapp.compose("Zzznotarealcontact", "hello")
            print("  FAIL expected ContactNotFound, none raised")
            overall = False
        except whatsapp.ContactNotFound as exc:
            print(f"  OK   ContactNotFound raised cleanly -> {str(exc)[:90]}")
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile3, ignore_errors=True)

    # -- B. permission boundary through the real EXECUTOR, mocked automation --
    print("\n--- B: whatsapp.send requires confirmation; whatsapp.compose does not ---\n")

    async def fake_compose(contact: str, message: str) -> str:
        return contact

    async def fake_send() -> str:
        return "Raja"

    whatsapp.compose = fake_compose  # type: ignore[assignment]
    whatsapp.send = fake_send  # type: ignore[assignment]
    whatsapp.pending_preview = lambda: 'Send this WhatsApp message to Raja: "are you free?"'  # type: ignore[assignment]

    from friday.permissions import EXECUTOR, PermissionError_

    EXECUTOR.set_confirm_handler(None)
    result = await EXECUTOR.run(
        "whatsapp.compose", {"contact": "Raja", "message": "are you free?"}, actor="text"
    )
    compose_auto_ok = result.ok
    print(f"  {'OK  ' if compose_auto_ok else 'MISS'} compose runs automatically, no confirm needed -> {result.speech}")
    overall &= compose_auto_ok

    confirm_calls = []

    async def approve(skill, args, preview: str) -> bool:
        confirm_calls.append(preview)
        return True

    EXECUTOR.set_confirm_handler(approve)
    result = await EXECUTOR.run("whatsapp.send", {}, actor="text")
    send_confirm_ok = result.ok and len(confirm_calls) == 1 and "Raja" in confirm_calls[0]
    print(f"  {'OK  ' if send_confirm_ok else 'MISS'} send() paused for confirmation naming the recipient -> {confirm_calls}")
    overall &= send_confirm_ok

    print("\n--- declining the send blocks it; approving it is required ---\n")

    async def decline(skill, args, preview: str) -> bool:
        return False

    EXECUTOR.set_confirm_handler(decline)
    result = await EXECUTOR.run("whatsapp.send", {}, actor="text")
    decline_ok = (not result.ok) and result.speech == "Cancelled."
    print(f"  {'OK  ' if decline_ok else 'MISS'} declined send -> {result.speech}")
    overall &= decline_ok

    print("\n--- an unattended actor can never send, confirm handler never invoked ---\n")
    confirm_calls.clear()
    EXECUTOR.set_confirm_handler(approve)
    try:
        await EXECUTOR.run("whatsapp.send", {}, actor="scheduler")
        unattended_ok = False
    except PermissionError_:
        unattended_ok = len(confirm_calls) == 0
    print(f"  {'OK  ' if unattended_ok else 'MISS'} unattended send() denied before any confirmation")
    overall &= unattended_ok

    # -- C. the full multi-step shape through plan.run's own machinery -------
    print("\n--- C: orchestrated compose->send stops exactly once, at send ---\n")
    from friday.llm import LlmProvider, LlmRequest, LlmResponse
    from friday.orchestrator import Orchestrator, ToolSpec

    class ScriptedPlanner(LlmProvider):
        name = "scripted"

        def __init__(self, replies: list[str]) -> None:
            self.replies = list(replies)
            self.calls = 0

        async def complete(self, request: LlmRequest) -> LlmResponse:
            reply = self.replies[min(self.calls, len(self.replies) - 1)]
            self.calls += 1
            return LlmResponse(text=reply, model="scripted", provider=self.name)

    specs = [
        ToolSpec(name="whatsapp.compose", description="draft a whatsapp message"),
        ToolSpec(name="whatsapp.send", description="send the drafted message"),
    ]
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "whatsapp.compose", '
        '"args": {"contact": "Raja", "message": "are you coming to college tomorrow"}}',
        '{"action": "call", "tool": "whatsapp.send", "args": {}}',
        '{"action": "done", "summary": "Sent."}',
    ])

    confirm_calls.clear()
    EXECUTOR.set_confirm_handler(approve)
    orch = Orchestrator(
        tools=["whatsapp.compose", "whatsapp.send"], actor="text",
        max_steps=5, llm_provider=planner, tool_specs=specs,
    )
    result = await orch.run_goal("open whatsapp and message raja, then send it")
    orchestrated_ok = (
        result.ok and result.stopped == "completed"
        and len(result.observations) == 2
        and len(confirm_calls) == 1  # only the send step paused
    )
    print(
        f"  {'OK  ' if orchestrated_ok else 'MISS'} 2 steps run, exactly 1 confirmation "
        f"(at send) -> stopped={result.stopped}, confirms={confirm_calls}"
    )
    overall &= orchestrated_ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
