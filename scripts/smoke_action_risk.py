"""Action-aware confirmation policy for generic UI tools (friday.risk +
friday.permissions.evaluate's risk-escalation path).

browser.click is tier L1 (auto) in general — most clicks are navigation,
search, reading. But a click whose visible text implies a consequential,
externally-committing action (send, buy, delete, publish, ...) must pause for
confirmation even though the skill itself stays L1, because FRIDAY can't
type-check "click this button" ahead of time — only the actual argument text
at call time reveals whether *this* click is the risky kind. This reuses the
exact existing tier/confirm/audit/ceiling machinery (see friday.permissions);
it does not add a second permission system.

Entirely deterministic — no browser, no LLM. Exercises friday.skills.browser's
real click skill through the real EXECUTOR, with friday.browser.click
monkeypatched so no Chromium instance is needed.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser, store  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

CONFIRM_CALLS: list[str] = []


async def _fake_click(target: str) -> str:
    return target


async def main() -> None:
    store.init()
    REGISTRY.discover()
    browser.click = _fake_click  # no real Chromium needed for this test

    from friday.permissions import EXECUTOR, PermissionError_

    overall = True

    # -- 1. benign clicks proceed automatically, no confirm handler needed --
    print("\n--- benign browser actions run automatically, no prompt ---\n")
    EXECUTOR.set_confirm_handler(None)  # if this gets called, the test below catches it
    for target in ["Next Page", "Search", "Read more", "Home", "Login"]:
        # "Login" is deliberately included: logging in is not itself a
        # commitment (nothing is sent/paid/deleted), so it should stay auto.
        result = await EXECUTOR.run("browser.click", {"target": target}, actor="text")
        ok = result.ok
        print(f"  {'OK  ' if ok else 'MISS'} click('{target}') ran without confirmation -> {result.speech}")
        overall &= ok

    # -- 2. a "Send" click (the WhatsApp-message stand-in) requires confirm --
    print("\n--- consequential click requires confirmation ---\n")
    CONFIRM_CALLS.clear()

    async def approve(skill, args, preview: str) -> bool:
        CONFIRM_CALLS.append(preview)
        print(f"  [confirm shown] {preview}")
        return True

    EXECUTOR.set_confirm_handler(approve)
    result = await EXECUTOR.run("browser.click", {"target": "Send"}, actor="text")
    send_ok = result.ok and len(CONFIRM_CALLS) == 1 and "escalated" in CONFIRM_CALLS[0]
    print(f"  {'OK  ' if send_ok else 'MISS'} click('Send') paused for confirmation -> {result.speech}")
    overall &= send_ok

    # -- 3. destructive-sounding click also requires confirmation ----------
    print("\n--- destructive-sounding click requires confirmation ---\n")
    CONFIRM_CALLS.clear()
    result = await EXECUTOR.run("browser.click", {"target": "Delete Account"}, actor="text")
    delete_ok = result.ok and len(CONFIRM_CALLS) == 1
    print(f"  {'OK  ' if delete_ok else 'MISS'} click('Delete Account') paused for confirmation -> {result.speech}")
    overall &= delete_ok

    # -- 4. a decline blocks the click; the skill body never runs ----------
    print("\n--- declining a consequential click blocks it ---\n")

    async def decline(skill, args, preview: str) -> bool:
        return False

    EXECUTOR.set_confirm_handler(decline)
    result = await EXECUTOR.run("browser.click", {"target": "Buy Now"}, actor="text")
    decline_ok = (not result.ok) and result.speech == "Cancelled."
    print(f"  {'OK  ' if decline_ok else 'MISS'} click('Buy Now') declined -> {result.speech}")
    overall &= decline_ok

    # -- 5. after confirmation, execution continues in the same call -------
    # (there's no separate "resume" step to test — Executor.run awaits the
    # confirm handler in-line and, once approved, proceeds to call the skill
    # within the same coroutine; test 2 above already proves this: send_ok
    # requires both the confirm prompt AND result.ok from the click actually
    # running afterward.)
    print("\n--- confirmed action resumes and completes in the same call ---\n")
    resumes_ok = send_ok  # re-stating what test 2 already proved, for a clear checklist line
    print(f"  {'OK  ' if resumes_ok else 'MISS'} (see 'consequential click requires confirmation' above)")
    overall &= resumes_ok

    # -- 6. an unattended actor is denied outright, confirm handler unused --
    print("\n--- unattended actor denied before any confirmation is attempted ---\n")
    CONFIRM_CALLS.clear()

    async def would_approve(skill, args, preview: str) -> bool:
        CONFIRM_CALLS.append(preview)
        return True

    EXECUTOR.set_confirm_handler(would_approve)
    try:
        await EXECUTOR.run("browser.click", {"target": "Send"}, actor="scheduler")
        unattended_ok = False
    except PermissionError_:
        unattended_ok = len(CONFIRM_CALLS) == 0
    print(f"  {'OK  ' if unattended_ok else 'MISS'} unattended click('Send') denied, confirm handler never invoked")
    overall &= unattended_ok

    # -- 7. a multi-step, all-benign sequence never triggers a prompt -------
    print("\n--- an all-benign multi-step sequence asks nothing ---\n")
    CONFIRM_CALLS.clear()
    EXECUTOR.set_confirm_handler(approve)
    for target in ["Next Page", "Filters", "Sort by price"]:
        await EXECUTOR.run("browser.click", {"target": target}, actor="text")
    no_prompt_ok = len(CONFIRM_CALLS) == 0
    print(f"  {'OK  ' if no_prompt_ok else 'MISS'} 3 benign clicks in a row -> {len(CONFIRM_CALLS)} confirm prompt(s)")
    overall &= no_prompt_ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
