"""Memory: store facts, then recall them by meaning rather than keyword."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import memory, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

TEACH = [
    "remember that my sister's name is priya",
    "remember my wifi password is bluehouse42",
    "note that the project deadline is friday the 14th",
    "keep in mind i prefer dark mode in every app",
    "remember my car is parked on level 3 of the north garage",
    "remember i work at gitam university",
]

# Recall phrased so the answer shares almost no words with the stored fact.
# Phase 25.0: the wifi password is a secret-like value (friday.memory.remember now runs
# storage through toolview.sanitize(prose=True)), so recall must find the memory but the
# password itself is never in the answer — see scripts/smoke_memory_redaction.py for the
# focused redaction suite this pairs with.
ASK = [
    ("what is my sibling called",            "priya"),
    ("how do i get on the network at home",  "[redacted]"),
    ("when is the project due",              "friday"),
    ("what theme do i like",                 "dark mode"),
    ("where did i leave the vehicle",        "level 3"),
    ("where am i employed",                  "gitam"),
    ("what is my blood type",                None),   # never told
]


async def _approve(skill, args, preview: str) -> bool:
    """Auto-approve L2 confirmations so the test doesn't wait on a human."""
    print(f"       [confirm] {preview} -> approved")
    return True


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    from friday.permissions import EXECUTOR

    EXECUTOR.set_confirm_handler(_approve)

    for row in memory.all_memories(limit=500):
        memory.forget(row["id"])

    print("\n--- teaching ---\n")
    for text in TEACH:
        result = await SESSION.handle(text, actor="text")
        mark = "OK  " if result.ok else "MISS"
        print(f"  {mark} {text[:52]:52} -> {result.speech[:50]}")

    print("\n--- recalling (wording deliberately different) ---\n")
    ok = 0
    for question, expected in ASK:
        result = await SESSION.handle(question, actor="text")
        answer = result.speech.lower()

        if expected is None:
            good = not result.ok
            detail = "declined" if good else answer[:40]
        else:
            good = expected in answer
            detail = answer[:48]

        ok += good
        mark = "OK  " if good else "MISS"
        print(f"  {mark} {question:38} -> {detail}")

    print(f"\n  {ok}/{len(ASK)} correct")

    print("\n--- forgetting ---\n")
    result = await SESSION.handle("forget my wifi password", actor="text")
    print(f"  {result.speech}")
    after = await SESSION.handle("how do i get on the network at home", actor="text")
    gone = "bluehouse42" not in after.speech.lower()
    print(f"  recall after forget: {'gone (good)' if gone else 'STILL THERE (bad)'}")

    print(f"\n  {len(memory.all_memories())} memories remain\n")


if __name__ == "__main__":
    asyncio.run(main())
