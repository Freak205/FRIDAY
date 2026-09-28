"""Browser automation: a full open -> read -> inspect -> click -> type -> read
cycle against a local data: URL, so this never depends on the internet.

Runs headless and against a throwaway profile directory regardless of
config.yaml, so it never pops a visible window or touches your real,
logged-in browser profile.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

_PAGE_HTML = """
<html><head><title>FRIDAY Browser Test</title></head>
<body>
  <h1>Hello FRIDAY</h1>
  <button onclick="document.getElementById('out').innerText='Clicked!'">Click Me</button>
  <input placeholder="Type here" id="box">
  <div id="out">Not clicked</div>
</body></html>
""".strip()

MATCH_CASES = [
    ("start a browser session on this url", "browser.open"),
    ("read the page you have open", "browser.read"),
    ("what buttons are on this page", "browser.inspect"),
    ("click send in the browser", "browser.click"),
]


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    # Isolate from the real, persistent profile: headless, throwaway dir.
    CFG.browser.headless = True
    tmp_profile = tempfile.mkdtemp(prefix="friday-browser-test-")
    CFG.browser.profile_dir = tmp_profile

    print("\n--- registration ---\n")
    for name in ("browser.open", "browser.read", "browser.inspect", "browser.click",
                 "browser.type", "browser.press", "browser.close"):
        registered = REGISTRY.get(name) is not None
        print(f"  {'OK  ' if registered else 'MISS'} {name} registered")
        overall &= registered

    print("\n--- intent matching ---\n")
    match_ok = 0
    for utterance, expected in MATCH_CASES:
        u = BRAIN.understand(utterance)
        good = u.skill == expected
        match_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance:40} -> {u.skill} ({u.score:.2f})")
    print(f"\n  {match_ok}/{len(MATCH_CASES)} correct")
    overall &= match_ok == len(MATCH_CASES)

    print("\n--- live cycle against a local data: URL ---\n")
    data_url = "data:text/html," + quote(_PAGE_HTML)

    try:
        info = await browser.goto(data_url)
        opened = info.title == "FRIDAY Browser Test"
        print(f"  {'OK  ' if opened else 'MISS'} goto() -> title={info.title!r}")
        overall &= opened

        page_info = await browser.read_page()
        read_ok = "Hello FRIDAY" in page_info.text and not page_info.via_ocr
        print(f"  {'OK  ' if read_ok else 'MISS'} read_page() -> {page_info.text[:60]!r}")
        overall &= read_ok

        elements = await browser.list_interactive()
        texts = {e.text for e in elements}
        inspect_ok = "Click Me" in texts
        print(f"  {'OK  ' if inspect_ok else 'MISS'} list_interactive() found {len(elements)} elements: {sorted(texts)}")
        overall &= inspect_ok

        clicked = await browser.click("Click Me")
        after_click = await browser.read_page()
        click_ok = "Clicked!" in after_click.text
        print(f"  {'OK  ' if click_ok else 'MISS'} click('Click Me') -> page now reads {after_click.text[:60]!r}")
        overall &= click_ok

        filled = await browser.fill("Type here", "hello from friday")
        value = await (await browser.ensure_open()).evaluate("() => document.getElementById('box').value")
        fill_ok = value == "hello from friday"
        print(f"  {'OK  ' if fill_ok else 'MISS'} fill('Type here', ...) -> input value is {value!r}")
        overall &= fill_ok

        try:
            await browser.click("This Does Not Exist Anywhere")
            print("  FAIL expected ElementNotFound for a missing target")
            overall = False
        except browser.ElementNotFound:
            print("  OK   click() on a missing element raises ElementNotFound cleanly")

        try:
            await browser.goto("http://this-domain-should-not-resolve.invalid")
            print("  WARN navigation to a bogus domain didn't raise (unexpected DNS result?)")
        except browser.NavigationError as exc:
            print(f"  OK   bad navigation fails cleanly: {str(exc)[:80]}")

    except browser.BrowserNotAvailable as exc:
        print(f"  FAIL browser not available: {exc}")
        overall = False
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile, ignore_errors=True)

    print("\n--- end to end via SESSION.handle (intent -> slot ask -> fill) ---\n")
    CFG.browser.headless = True
    tmp_profile2 = tempfile.mkdtemp(prefix="friday-browser-test2-")
    CFG.browser.profile_dir = tmp_profile2
    try:
        asked = await SESSION.handle("start a browser session on this url", actor="text")
        ask_ok = (not asked.ok) and SESSION.pending is not None and SESSION.pending.kind == "slot"
        print(f"  {'OK  ' if ask_ok else 'MISS'} no url given -> asks for the slot: {asked.speech!r}")
        overall &= ask_ok

        filled = await SESSION.handle(data_url, actor="text")
        e2e_ok = filled.ok
        print(f"  {'OK  ' if e2e_ok else 'MISS'} slot filled with the data: URL -> {filled.speech[:100]}")
        overall &= e2e_ok
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile2, ignore_errors=True)

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
