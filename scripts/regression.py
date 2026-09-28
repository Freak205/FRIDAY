"""Full regression: intent matching across every skill group, plus live execution.

The matching table matters most — every skill added to the corpus is another
chance for an utterance to land on the wrong one.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.brain import BRAIN  # noqa: E402
from friday.brain.engine import Action  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

# (utterance, expected skill) — none of these are registered examples.
MATCH_CASES = [
    # system
    ("crank it up",                        "system.volume.up"),
    ("its way too loud",                   "system.volume.down"),
    ("set the volume to 40",               "system.volume.set"),
    ("how much juice is left",             "system.battery"),
    ("is my laptop plugged in",            "system.battery"),
    ("whats the clock say",                "system.time"),
    ("is my machine struggling",           "system.info"),
    ("secure my computer",                 "system.lock"),
    # brightness
    ("the screen is too dark",             "system.brightness.up"),
    ("dim the display",                    "system.brightness.down"),
    ("set brightness to 60",               "system.brightness.set"),
    # network
    ("am i online",                        "network.status"),
    ("which wifi am i connected to",       "network.status"),
    # processes
    ("whats eating all my memory",         "process.top"),
    ("why is everything so slow",          "process.top"),
    # clipboard
    ("what did i copy",                    "clipboard.read"),
    # apps and windows
    ("fire up notepad",                    "apps.open"),
    ("i need chrome",                      "apps.open"),
    ("show me everything thats running",   "apps.list"),
    # apps.open vs. project.open disambiguation — a bare app-launch phrase
    # must win even though project.open's own examples are full of "vs code"
    ("open vs code",                       "apps.open"),
    ("open vscode",                        "apps.open"),
    ("launch vs code",                     "apps.open"),
    ("start vs code",                      "apps.open"),
    ("open visual studio code",            "apps.open"),
    ("snap this to the left",              "window.snap"),
    ("make this fill the screen",          "window.maximize"),
    ("get this out of my way",             "window.minimize"),
    ("show me the desktop",                "window.minimize_all"),
    # input
    ("press control s",                    "input.hotkey"),
    # screen
    ("grab my screen",                     "screen.capture"),
    ("which window am i in",               "screen.active_window"),
    ("read my screen",                     "screen.read_text"),
    ("what text is on my screen",          "screen.read_text"),
    ("what does my screen say",            "screen.read_text"),
    # desktop situational awareness (P9)
    ("give me a full picture of my screen", "screen.observe"),
    ("take stock of my desktop",           "screen.observe"),
    # ui automation
    ("what can i click here",              "ui.inspect"),
    ("click the save button",              "ui.click"),
    ("read this dialog to me",             "ui.read"),
    # files
    ("dig up my resume",                   "files.search"),
    # web
    ("look up the weather in mumbai",      "web.search"),
    ("take me to youtube",                 "web.open"),
    # scheduling
    ("every day at 8am tell me my battery", "schedule.create"),
    ("in 10 minutes tell me the time",     "schedule.create"),
    ("what have you got planned",          "schedule.list"),
    # meta
    ("undo that",                          "meta.undo"),
    ("what can you do",                    "meta.capabilities"),
    ("what did you just do",               "meta.recent"),
    # routines
    ("run my morning routine",             "routine.run"),
    ("what routines do i have",            "routine.list"),
    # memory — "remember" is ambiguous between store and recall, so either
    # skill is an acceptable match; both reroute internally to the right one.
    ("remember that my car is a blue swift", ("memory.remember", "memory.recall")),
    ("what do you remember",               "memory.list"),
    # knowledge (P4 RAG)
    ("index this folder",                  "knowledge.index"),
    ("learn this document",                "knowledge.index"),
    ("what have you indexed",              "knowledge.list"),
    ("stop remembering that pdf",          "knowledge.forget"),
    ("search my documents for the plan",   "knowledge.ask"),
    # screen OCR bridge (P4)
    ("where is the login button on my screen", "screen.find_text"),
    ("locate the text sign in on the screen",  "screen.find_text"),
    ("click where it says next on the screen", "screen.click_text"),
    ("tap the ok label on my screen",          "screen.click_text"),
    # browser / computer-use (P4)
    ("start a browser session on this url",    "browser.open"),
    ("launch the browser and load this site",  "browser.open"),
    ("read the page you have open",            "browser.read"),
    ("what does the current browser page say", "browser.read"),
    ("what buttons are on this page",          "browser.inspect"),
    ("click send in the browser",              "browser.click"),
    ("fill in the browser search field",       "browser.type"),
    ("hit enter in the browser",               "browser.press"),
    ("close your browser tab",                 "browser.close"),
    # project awareness (P4)
    ("what is my project status",              "project.inspect"),
    ("check on my friday project",             "project.inspect"),
    ("open my friday project",                 "project.inspect"),
    ("open this project in visual studio code", "project.open"),
    ("launch vs code on this project",         "project.open"),
    ("open the friday project in vs code",     "project.open"),
    # multi-step planning bridge (P4.3)
    ("work through this task on your own and get it done", "plan.run"),
    ("put together a plan and carry it out for me",        "plan.run"),
    ("handle this whole multi-step thing for me",          "plan.run"),
    # WhatsApp Web workflow (P6)
    ("open whatsapp web",                      "whatsapp.open"),
    ("launch whatsapp",                        "whatsapp.open"),
    ("message raja on whatsapp: are you free tomorrow", "whatsapp.compose"),
    ("text priya saying i will be late",       "whatsapp.compose"),
    ("go ahead and send that",                 "whatsapp.send"),
    # utilities
    ("whats the weather like",             "weather.now"),
    ("do i need an umbrella",              "weather.now"),
    ("set a timer for 10 minutes",         "timer.set"),
    ("whats 15 percent of 2400",           "math.calculate"),
    ("jot this down",                      "notes.add"),
    ("read my notes",                      "notes.read"),
    # out of domain — must be refused
    ("write me a poem about the sea",      None),
    ("what is the capital of peru",        None),
    ("explain quantum physics",            None),
    ("tell me a joke",                     None),
]

# Skills safe to actually execute (all L0, no side effects).
EXEC_CASES = [
    "system.time", "system.battery", "system.info", "system.volume.get",
    "system.brightness.get", "network.status", "process.top",
    "screen.active_window", "screen.observe", "apps.list", "meta.capabilities", "meta.recent",
    "ui.inspect", "knowledge.list",
]


async def main() -> None:
    REGISTRY.discover()
    t0 = time.perf_counter()
    BRAIN.warm()
    warm = time.perf_counter() - t0

    print(f"\n{len(REGISTRY)} skills | brain warm in {warm:.1f}s\n")
    print("--- intent matching ---\n")

    ok = 0
    failures = []
    latencies = []

    for utterance, expected in MATCH_CASES:
        t = time.perf_counter()
        u = BRAIN.understand(utterance)
        latencies.append((time.perf_counter() - t) * 1000)

        if expected is None:
            good = u.action in (Action.UNKNOWN, Action.CLARIFY)
            got = u.action.value if good else str(u.skill)
        else:
            allowed = expected if isinstance(expected, tuple) else (expected,)
            good = u.action in (Action.ACT, Action.ASK_SLOT) and u.skill in allowed
            got = str(u.skill) + ("" if u.action is Action.ACT else " (needs slot)")

        ok += good
        if not good:
            failures.append((utterance, expected, got, u.score))
        mark = "OK  " if good else "MISS"
        print(f"  {mark} {utterance:38} -> {got:28} {u.score:.2f}")

    print(f"\n  {ok}/{len(MATCH_CASES)} correct")
    print(f"  latency: avg {sum(latencies)/len(latencies):.1f}ms, "
          f"max {max(latencies):.0f}ms")

    if failures:
        print("\n  failures:")
        for utterance, expected, got, score in failures:
            print(f"    {utterance!r}\n      expected {expected}, got {got} ({score:.2f})")

    print("\n--- live execution (read-only skills) ---\n")
    run_ok = 0
    for name in EXEC_CASES:
        try:
            result = await EXECUTOR.run(name, {}, actor="test")
            run_ok += result.ok
            mark = "OK  " if result.ok else "WARN"
            print(f"  {mark} {name:24} {result.speech[:64]}")
        except Exception as exc:
            print(f"  FAIL {name:24} {type(exc).__name__}: {exc}")

    print(f"\n  {run_ok}/{len(EXEC_CASES)} executed cleanly\n")


if __name__ == "__main__":
    asyncio.run(main())
