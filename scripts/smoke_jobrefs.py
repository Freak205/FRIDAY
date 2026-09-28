"""Regression for the job-reference misfire.

"delete the lock the pc job" must resolve to schedule.delete with name="lock the
pc" — NOT to system.lock, which would perform the inner command instead of
managing the job. Same hazard for every <verb> the <command phrase> job frame.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.brain import BRAIN  # noqa: E402
from friday.brain.engine import Action  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

# (utterance, expected skill, expected name slot)
CASES = [
    ("delete the lock the pc job",             "schedule.delete",  "lock the pc"),
    ("delete the tell me my battery job",      "schedule.delete",  "tell me my battery"),
    ("delete the check my system job",         "schedule.delete",  "check my system"),
    ("remove the open chrome job",             "schedule.delete",  "open chrome"),
    ("cancel the turn down the volume job",    "schedule.delete",  "turn down the volume"),
    ("pause the lock the pc job",              "schedule.toggle",  "lock the pc"),
    ("disable the check my system job",        "schedule.toggle",  "check my system"),
    ("run the lock the pc job now",            "schedule.run_now", "lock the pc"),
    ("how did the check my system job go",     "schedule.history", "check my system"),
    # The bare commands must still work — the fix must not break them.
    ("lock the pc",                            "system.lock",      None),
    ("check my system",                        "system.info",      None),
    ("turn down the volume",                   "system.volume.down", None),
    ("open chrome",                            "apps.open",        None),
]


def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()

    print()
    ok = 0
    for utterance, expected_skill, expected_name in CASES:
        u = BRAIN.understand(utterance)
        skill_ok = u.skill == expected_skill and u.action in (Action.ACT, Action.ASK_SLOT)

        name_ok = True
        got_name = u.args.get("name")
        if expected_name is not None:
            name_ok = got_name == expected_name

        good = skill_ok and name_ok
        ok += good
        mark = "OK  " if good else "MISS"
        detail = f" name={got_name!r}" if expected_name is not None else ""
        print(f"  {mark} {utterance:38} -> {str(u.skill):20} {u.score:.2f}{detail}")
        if not good and expected_name is not None and not name_ok:
            print(f"       expected name={expected_name!r}")

    print(f"\n  {ok}/{len(CASES)} correct\n")


if __name__ == "__main__":
    main()
