"""Smoke test the brain: phrasings it has never seen -> correct skill?

Every utterance below is deliberately NOT one of the registered examples.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.brain import BRAIN  # noqa: E402
from friday.brain.engine import Action  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

# (utterance, expected skill or None)
CASES = [
    # unseen phrasings of known intents
    ("crank it up",                      "system.volume.up"),
    ("its way too loud in here",         "system.volume.down"),
    ("bump the volume to 35",            "system.volume.set"),
    ("how much juice is left",           "system.battery"),
    ("is my laptop plugged in",          "system.battery"),
    ("whats the clock say",              "system.time"),
    ("fire up notepad",                  "apps.open"),
    ("i need chrome",                    "apps.open"),
    # must not be misrouted to project.open just because it mentions vs code
    ("open vs code",                     "apps.open"),
    ("open vscode",                      "apps.open"),
    ("launch vs code",                   "apps.open"),
    ("start vs code",                    "apps.open"),
    ("open visual studio code",          "apps.open"),
    ("show me everything thats running", "apps.list"),
    ("grab my screen",                   "screen.capture"),
    ("which window am i in",             "screen.active_window"),
    ("is my machine struggling",         "system.info"),
    ("secure my computer",               "system.lock"),
    ("dig up my resume",                 "files.search"),
    # should NOT confidently match anything
    ("write me a poem about the sea",    None),
    ("what is the capital of peru",      None),
]


def main() -> None:
    REGISTRY.discover()

    t0 = time.perf_counter()
    BRAIN.warm()
    print(f"\nbrain warm in {time.perf_counter() - t0:.1f}s\n")

    ok = 0
    for utterance, expected in CASES:
        t = time.perf_counter()
        u = BRAIN.understand(utterance)
        ms = (time.perf_counter() - t) * 1000

        if expected is None:
            good = u.action in (Action.UNKNOWN, Action.CLARIFY)
            got = u.action.value if good else f"{u.skill}"
        else:
            good = u.action == Action.ACT and u.skill == expected
            got = f"{u.skill}"

        ok += good
        mark = "OK  " if good else "MISS"
        args = f" {u.args}" if u.args else ""
        print(f"  {mark} {utterance:36} -> {got:24} {u.score:.2f}{args}  [{ms:.0f}ms]")

    print(f"\n{ok}/{len(CASES)} correct\n")


if __name__ == "__main__":
    main()
