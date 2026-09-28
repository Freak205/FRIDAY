"""Knowledge skills: local document RAG.

Extends what FRIDAY already knows about you (friday.skills.memory, one fact at
a time) to whatever is sitting in your files — index a document or folder once,
then ask questions about it in plain language. Same embedding model, same
meaning-not-keyword retrieval, just applied to document chunks instead of
one-line facts.
"""

from __future__ import annotations

import re
from typing import Annotated

from friday import knowledge
from friday.config import CFG
from friday.registry import SkillResult, skill

_FORGET_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+)?"
    r"(forget|remove|delete|erase|unindex|un-index|stop remembering)\s+"
    r"(that\s+|this\s+|the\s+)?"
    r"(document|file|pdf)?\s*"
    r"(called|named)?\s*",
    re.IGNORECASE,
)


@skill(
    name="knowledge.index",
    tier="L1",
    action="modify",
    description="Index a document or folder so FRIDAY can answer questions about it",
    examples=[
        "index this folder",
        "index this document",
        "learn this document",
        "read and remember this pdf",
        "index my project notes",
        "add this file to your knowledge base",
        "study this document for me",
        "index the documents in this folder",
        "go through this folder and remember what's in it",
    ],
)
def index(
    path: Annotated[str, "file or folder to index"],
) -> SkillResult:
    try:
        result = knowledge.index_path(path)
    except FileNotFoundError:
        return SkillResult(speech=f"There's nothing at {path}.", ok=False)
    except knowledge.UnsupportedFileType as exc:
        return SkillResult(speech=str(exc), ok=False)

    name = result["path"].replace("\\", "/").rsplit("/", 1)[-1]

    if "chunks" in result:
        if not result["chunks"]:
            return SkillResult(
                speech=f"I couldn't get any readable text out of {name}.",
                ok=False, data=result,
            )
        return SkillResult(
            speech=f"Indexed {name} — {result['chunks']} chunks.", data=result,
        )

    if not result["indexed"]:
        return SkillResult(
            speech=f"I found nothing to index under {path}.", ok=False, data=result,
        )
    return SkillResult(
        speech=f"Indexed {result['indexed']} documents from {name}.", data=result,
    )


@skill(
    name="knowledge.ask",
    tier="L0",
    description="Answer a question using previously indexed documents",
    examples=[
        "what does the document say about the budget",
        "search my documents for the deadline",
        "what do my notes say about the project",
        "find the section about pricing in that pdf",
        "according to the report what is the conclusion",
        "look through my documents for the schedule",
        "what does that file say about the plan",
        "check my indexed documents for the answer",
        "what is my upcoming exam schedule",
        "when is my exam",
        "what room is my exam in",
        "what time is my appointment",
        "who is on my team for this project",
    ],
)
def ask(
    query: Annotated[str, "what to look for in your indexed documents"],
) -> SkillResult:
    hits = knowledge.search(query, k=4)
    strong = [h for h in hits if h.score >= CFG.knowledge.match_threshold]

    if not strong:
        return SkillResult(
            speech="I couldn't find anything about that in your documents.", ok=False,
        )

    best = strong[0]
    snippet = best.text[:400].strip()
    return SkillResult(
        speech=f"From {best.title}: {snippet}",
        data={
            "matches": [
                {"path": h.path, "title": h.title, "score": round(h.score, 3), "text": h.text}
                for h in strong
            ]
        },
    )


@skill(
    name="knowledge.list",
    tier="L0",
    description="List documents FRIDAY has indexed",
    examples=[
        "what documents do you know about",
        "list indexed documents",
        "what have you indexed",
        "show me what's in your knowledge base",
        "what documents have you learned",
    ],
)
def list_docs() -> SkillResult:
    docs = knowledge.list_documents()
    if not docs:
        return SkillResult(speech="I haven't indexed any documents yet.")

    preview = ", ".join(d["title"] for d in docs[:5])
    more = f" and {len(docs) - 5} more" if len(docs) > 5 else ""
    return SkillResult(
        speech=f"I've indexed {len(docs)} documents: {preview}{more}.",
        data={"documents": docs},
    )


@skill(
    name="knowledge.forget",
    tier="L2",
    action="delete",
    description="Remove a document from FRIDAY's knowledge base",
    examples=[
        "forget that document",
        "remove this file from your knowledge base",
        "stop remembering that pdf",
        "unindex this document",
        "delete that document from your knowledge",
    ],
    dry_run=lambda query: f"Remove indexed document(s) matching '{query}'",
)
def forget(
    query: Annotated[str, "file name or path to remove"],
) -> SkillResult:
    cleaned = _FORGET_LEAD.sub("", query).strip().rstrip(".?!")
    removed = knowledge.forget_document(cleaned or query)
    if not removed:
        return SkillResult(
            speech=f"I don't have anything indexed matching {cleaned or query}.", ok=False,
        )

    first = removed[0].replace("\\", "/").rsplit("/", 1)[-1]
    return SkillResult(
        speech=f"Removed {first}"
        + (f" and {len(removed) - 1} more." if len(removed) > 1 else " from my knowledge base."),
        data={"removed": removed},
    )
