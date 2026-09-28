"""ChatGPT-in-Chrome workflow: proves the *generic* browser skills (open,
type, press, read) are enough for a "type a question, submit, read the
reply" chat UI — no ChatGPT-specific automation module, per this phase's
explicit instruction not to build one unless the generic abstraction
genuinely can't support the workflow.

Runs against a local mock page shaped like a minimal chat UI (a textbox, a
submit affordance, a reply that appears after a short simulated delay) —
never the real chatgpt.com, never any login. The point of this test is the
*shape* of the interaction, not the real site.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser  # noqa: E402
from friday.config import CFG  # noqa: E402

_CHAT_PAGE = """
<html><head><title>Mock Chat</title></head><body>
  <div id="log"></div>
  <textarea placeholder="Message ChatGPT" id="prompt"></textarea>
  <button id="submit">Send</button>
  <script>
    document.getElementById('submit').addEventListener('click', () => {
      const q = document.getElementById('prompt').value;
      setTimeout(() => {
        document.getElementById('log').innerText =
          'You asked: ' + q + '\\nAnswer: your networks exam is on Friday at 10am in Hall B.';
      }, 150);
    });
  </script>
</body></html>
""".strip()

_LOGIN_PAGE = """
<html><head><title>Mock Chat</title></head>
<body><h1>Log in to continue</h1><p>Sign in to your account to use the assistant.</p></body></html>
""".strip()


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


async def main() -> None:
    overall = True
    CFG.browser.headless = True
    CFG.browser.channel = ""

    print("\n--- generic browser skills drive a ChatGPT-shaped page end to end ---\n")
    tmp_profile = tempfile.mkdtemp(prefix="friday-chatgpt-test-")
    CFG.browser.profile_dir = tmp_profile
    try:
        info = await browser.goto(_data_url(_CHAT_PAGE))
        opened_ok = info.title == "Mock Chat"
        print(f"  {'OK  ' if opened_ok else 'MISS'} browser.open -> title={info.title!r}")
        overall &= opened_ok

        filled = await browser.fill("Message ChatGPT", "what is tomorrow's exam plan")
        print(f"  {'OK  ' if filled else 'MISS'} browser.type into the prompt box -> matched {filled!r}")
        overall &= bool(filled)

        clicked = await browser.click("Send")
        print(f"  {'OK  ' if clicked else 'MISS'} browser.click('Send') -> {clicked!r}")
        overall &= bool(clicked)

        # The reply renders after a short delay, same as a real streaming
        # response — poll read_page briefly instead of assuming it's instant.
        answer_text = ""
        for _ in range(20):
            page_info = await browser.read_page()
            if "Answer:" in page_info.text:
                answer_text = page_info.text
                break
            await (await browser.ensure_open()).wait_for_timeout(50)

        read_ok = "Hall B" in answer_text and "Friday" in answer_text
        print(f"  {'OK  ' if read_ok else 'MISS'} browser.read -> {answer_text[:100]!r}")
        overall &= read_ok
    except browser.BrowserNotAvailable as exc:
        print(f"  FAIL browser not available: {exc}")
        overall = False
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile, ignore_errors=True)

    print("\n--- a login-gated page is still readable, so FRIDAY can tell you to sign in ---\n")
    tmp_profile2 = tempfile.mkdtemp(prefix="friday-chatgpt-test2-")
    CFG.browser.profile_dir = tmp_profile2
    try:
        await browser.goto(_data_url(_LOGIN_PAGE))
        page_info = await browser.read_page()
        login_detected = "log in" in page_info.text.lower() or "sign in" in page_info.text.lower()
        print(f"  {'OK  ' if login_detected else 'MISS'} browser.read surfaces the login prompt -> {page_info.text[:80]!r}")
        overall &= login_detected
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile2, ignore_errors=True)

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
