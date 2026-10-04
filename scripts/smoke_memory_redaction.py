"""Phase 25 (Workstream 4) — regression coverage for memory secret redaction.

The completion audit found `friday.memory.remember()` had NO redaction at all, unlike
every other subsystem that can see model/tool text (friday.toolview, used by the
orchestrator's evidence and by context_memory/episodes/experience) — despite the skill's
own canonical example being "remember my wifi password".

The fix reuses the existing mechanism: `toolview.sanitize(value, prose=True)` (Phase 25.0
adds the `prose` pass to `friday/toolview.py` itself, so this is the SAME module/pattern
set every other redaction call site already uses, not a second framework) is applied in
`friday.memory.remember()` before a value is embedded and stored, and again in the
`memory.remember`/`memory.recall` SKILLS (`friday/skills/memory.py`) so the spoken
confirmation never echoes back the raw secret it just declined to store.

This suite pins:

  A  a secret-like value is redacted before it reaches SQLite (never plaintext on disk)
  B  ...and the skill's own spoken confirmation does not leak it either (found live: the
     confirmation used to build its speech from the raw pre-storage value)
  C  ordinary (non-secret) memories are stored and recalled byte-for-byte unchanged
  D  semantic recall still finds a redacted memory (it's forgotten as a fact WHAT it is,
     never made unfindable) — asking about it returns the redacted placeholder, not silence
  E  redaction is stable under repeated storage (idempotent — storing an already-redacted
     value again does not add a second marker)

Real friday.memory + friday.store (throwaway DB) + the real friday.skills.memory skills
through Session.handle. No Ollama, no network.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import memory, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402


async def main_async() -> int:
    t0 = time.perf_counter()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    with store.use_temp_db():
        for row in memory.all_memories(limit=500):
            memory.forget(row["id"])

        scenario("A: a secret-like value is redacted before it reaches SQLite")
        memory.remember("my wifi password is upstairs123", kind="fact")
        stored = memory.all_memories()[0]["value"]
        check("the password is not stored in plaintext", "upstairs123" not in stored, stored)
        check("the redaction marker is present", "[redacted]" in stored, stored)

        memory.remember("my github token is ghp_abcdefghijklmnopqrstuvwxyz123456", kind="fact")
        stored2 = memory.all_memories()[0]["value"]
        check("a github token is not stored in plaintext", "ghp_abcdefghijklmnopqrstuvwxyz123456" not in stored2, stored2)

        memory.remember("my api key is sk-abcdefghijklmnop1234", kind="fact")
        stored3 = memory.all_memories()[0]["value"]
        check("an api key (structured, no 'is' needed) is not stored in plaintext",
              "sk-abcdefghijklmnop1234" not in stored3, stored3)

        scenario("B: the spoken confirmation does not leak the secret either")
        result = await SESSION.handle("remember my bank password is hunter2000", actor="text")
        check("confirmation speech does not contain the raw secret", "hunter2000" not in result.speech, result.speech)
        check("confirmation speech shows the redaction happened", "[redacted]" in result.speech, result.speech)
        stored4 = memory.all_memories()[0]["value"]
        check("...and what got stored matches what was said (same value, not a second copy)", stored4 in result.speech, f"{stored4!r} / {result.speech!r}")

        scenario("C: ordinary (non-secret) memories are unaffected — stored and recalled verbatim")
        result_ok = await SESSION.handle("remember that my sister priya lives in chennai", actor="text")
        check("an ordinary fact is stored", result_ok.ok, result_ok.speech)
        check("...unchanged, no redaction marker", "[redacted]" not in result_ok.speech, result_ok.speech)
        recall_ok = await SESSION.handle("where does my sister live", actor="text")
        check("ordinary recall still finds it", "chennai" in recall_ok.speech.lower(), recall_ok.speech)

        scenario("D: semantic recall still finds a redacted memory — it's WHAT is hidden, not THAT it exists")
        recall_secret = await SESSION.handle("what is my bank password", actor="text")
        check("recall finds the memory (doesn't just silently fail)", recall_secret.ok, recall_secret.speech)
        check("...but the redacted placeholder, not the real secret", "hunter2000" not in recall_secret.speech, recall_secret.speech)

        scenario("E: redaction is idempotent under repeated storage")
        once = memory.all_memories()[2]["value"]  # the wifi password row, oldest of the three A memories
        memory.remember(once, kind="fact")  # store the already-redacted text again
        twice = memory.all_memories()[0]["value"]
        check("re-storing an already-redacted value adds no second marker", twice.count("[redacted]") <= 1, twice)
        check("re-storing an already-redacted value is unchanged", twice == once, f"{once!r} -> {twice!r}")

    return finish("Phase 25 — memory secret redaction", time.perf_counter() - t0, min_assertions=13, min_scenarios=5)


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
