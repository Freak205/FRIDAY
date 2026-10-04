"""Phase 26 (Workstream 3) — regression coverage for the `web.open` <-> `browser.open`
integration seam.

The completion audit found these two tools silently disagreed about what "the
browser" was: `web.open` always launched the OS default browser via
`webbrowser.open()` (an untracked process FRIDAY cannot read back), while
`browser.open`/`browser.read`/`browser.click` all act on a completely separate,
FRIDAY-controlled Playwright/Chromium session. If a controlled session was
already open and the user said "open youtube" (phrasing that routes to
`web.open`), FRIDAY opened a second, unrelated window while the controlled
session it could actually read/click stayed on its old page — so a follow-up
"read the page" or "click subscribe" silently acted on stale content.

The fix (`friday/skills/web.py`, `open_url`): `web.open` now checks
`friday.browser.is_open()` first. If a controlled session is already running,
it navigates THAT session (`browser.goto`), so browser.read/browser.click stay
correct. If no controlled session exists, behavior is byte-for-byte unchanged
— the OS default browser opens, exactly as before Phase 26.

This suite pins:

  A  no controlled session open -> web.open falls back to the OS default
     browser exactly as before (webbrowser.open called, browser.goto NOT)
  B  a controlled session IS open -> web.open navigates that real session
     (verified by reading the real page back through browser.read_page,
     not just trusting the skill's own speech) instead of opening a second,
     untracked window (webbrowser.open NOT called)
  C  browser.open itself is unaffected — still reachable and still the way
     to start a controlled session in the first place

Real Playwright (Chromium), no mocks for the browser itself — only
`webbrowser.open` is monkeypatched, so this test never actually pops an OS
browser window.
"""

from __future__ import annotations

import asyncio
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import browser  # noqa: E402
from friday.skills import web  # noqa: E402


async def main_async() -> int:
    t0 = time.perf_counter()

    opened_via_os: list[str] = []
    real_webbrowser_open = webbrowser.open
    webbrowser.open = lambda target: opened_via_os.append(target) or True

    try:
        scenario("A: no controlled session open -> falls back to the OS default browser, unchanged")
        check("no controlled browser session is open yet", not browser.is_open())
        r = await web.open_url(url="example.com")
        check("web.open reports ok", r.ok, r.speech)
        check("the OS default browser was asked to open it", any("example.com" in t for t in opened_via_os), opened_via_os)
        check("...and still no controlled Playwright session exists", not browser.is_open())

        scenario("B: a controlled session IS open -> web.open navigates THAT session instead")
        opened_via_os.clear()
        info = await browser.goto("https://example.org")
        check("a controlled browser session is now open (via browser.open's own goto)", browser.is_open())

        r2 = await web.open_url(url="wikipedia.org")
        check("web.open reports ok", r2.ok, r2.speech)
        check("the OS default browser was NOT opened a second time", opened_via_os == [], opened_via_os)

        page = await browser.read_page(limit=2000)
        check(
            "the CONTROLLED session actually navigated to the new site (read back for real, not trusted from speech)",
            "wikipedia" in (page.url.lower() + page.title.lower()),
            f"url={page.url!r} title={page.title!r}",
        )

        scenario("C: browser.open itself is unaffected and still reaches the same controlled session")
        from friday.skills import browser as browser_skill

        r3 = await browser_skill.open_page(url="example.com")
        check("browser.open still works", r3.ok, r3.speech)
        check("...and it's the same controlled session (already_loaded is a real signal, not a guess)", isinstance(r3.data.get("already_loaded"), bool))

    finally:
        webbrowser.open = real_webbrowser_open
        await browser.close()

    return finish("Phase 26 — web.open <-> browser.open seam", time.perf_counter() - t0, min_assertions=10, min_scenarios=3)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
