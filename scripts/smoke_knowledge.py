"""Knowledge base: index documents, then answer questions by meaning, not keyword."""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import knowledge, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

DOCS = {
    "budget.md": (
        "# Q3 Budget Report\n\n"
        "The marketing budget for Q3 is capped at 50,000 dollars, split evenly "
        "across social media and print advertising.\n\n"
        "Engineering headcount will not grow this quarter; all open positions "
        "are frozen until the next board review.\n\n"
        "The office lease renewal is due on the 15th of September and costs "
        "12,000 dollars per month.\n"
    ),
    "meeting_notes.txt": (
        "Client kickoff notes: Priya is the point of contact for the follow up "
        "email and will send the signed contract by Wednesday. The next "
        "milestone review is scheduled for the last week of October.\n"
    ),
}

ASK = [
    ("how much is the marketing budget",              "50,000"),
    ("when does the office lease need renewing",      "september"),
    ("who is sending over the signed contract",       "priya"),
    ("what is the capital of france",                 None),  # not in any document
]


async def _approve(skill, args, preview: str) -> bool:
    print(f"       [confirm] {preview} -> approved")
    return True


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    from friday.permissions import EXECUTOR

    EXECUTOR.set_confirm_handler(_approve)

    with tempfile.TemporaryDirectory(prefix="friday-kb-") as tmp:
        tmp_dir = Path(tmp)
        for name, text in DOCS.items():
            (tmp_dir / name).write_text(text, encoding="utf-8")

        # Clean slate: forget anything left over from a prior run at this path.
        knowledge.forget_document(str(tmp_dir))

        print("\n--- chunking ---\n")
        chunks = knowledge.chunk_text(DOCS["budget.md"], size=80, overlap=10)
        chunk_ok = len(chunks) > 1 and all(len(c) <= 90 for c in chunks)
        print(f"  {'OK  ' if chunk_ok else 'MISS'} 80-char split of budget.md -> {len(chunks)} chunks")

        print("\n--- indexing (via SESSION.handle) ---\n")
        result = await SESSION.handle(f"index the folder {tmp_dir}", actor="text")
        print(f"  {'OK  ' if result.ok else 'MISS'} index folder -> {result.speech}")
        indexed_ok = result.ok and result.data.get("indexed", 0) == len(DOCS)

        print("\n--- listing ---\n")
        result = await SESSION.handle("what documents do you know about", actor="text")
        print(f"  {'OK  ' if result.ok else 'MISS'} knowledge.list -> {result.speech}")

        print("\n--- asking (wording deliberately different from the source text) ---\n")
        ok = 0
        for question, expected in ASK:
            result = await SESSION.handle(question, actor="text")
            answer = result.speech.lower()

            if expected is None:
                good = not result.ok
                detail = "declined" if good else answer[:50]
            else:
                good = expected in answer
                detail = answer[:60]

            ok += good
            mark = "OK  " if good else "MISS"
            print(f"  {mark} {question:45} -> {detail}")

        print(f"\n  {ok}/{len(ASK)} correct")

        print("\n--- forgetting ---\n")
        result = await SESSION.handle(f"forget the document {tmp_dir / 'budget.md'}", actor="text")
        print(f"  {result.speech}")
        after = knowledge.search("marketing budget", k=3)
        gone = not any("budget.md" in h.path for h in after)
        print(f"  budget.md gone from search: {'yes (good)' if gone else 'STILL THERE (bad)'}")

        remaining = knowledge.list_documents()
        print(f"\n  {len(remaining)} document(s) remain indexed under the temp folder")

        # Leave the knowledge base as we found it.
        knowledge.forget_document(str(tmp_dir))

    overall = chunk_ok and indexed_ok and ok == len(ASK) and gone
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
