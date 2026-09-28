"""Memory skills: remember, recall, forget."""

from __future__ import annotations

import re
from typing import Annotated

from friday import memory
from friday.registry import SkillResult, skill

# "remember that X" / "note that X" / "don't forget X"
_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+)?"
    r"(remember|note|memorise|memorize|keep in mind|don'?t forget|store|save)\s+"
    r"(that\s+|this[:\s]+|the fact that\s+)?",
    re.IGNORECASE,
)

_PREFERENCE = re.compile(
    r"\b(i (prefer|like|want|always|never|usually|hate|dislike)|"
    r"my favou?rite|from now on|please always|please never)\b",
    re.IGNORECASE,
)

# Interrogatives. Storing a question as a fact poisons recall — ask "where did I
# leave the car" twice and the second answer is the first question.
_QUESTION = re.compile(
    r"^\s*(what|when|where|who|whose|which|why|how|"
    r"do|does|did|is|are|was|were|can|could|will|would|should|have|has|am)\b",
    re.IGNORECASE,
)


@skill(
    name="memory.remember",
    tier="L1",
    action="modify",
    description="Remember a fact or preference for later",
    examples=[
        "remember that my sister's name is priya",
        "remember my wifi password is upstairs",
        "note that the project deadline is friday",
        "keep in mind i prefer dark mode",
        "don't forget my car is parked on level 3",
        "remember this for me",
        "save that i work at gitam",
        "i always want the volume at 30",
    ],
)
def remember(
    text: Annotated[str, "the thing to remember"],
) -> SkillResult:
    had_lead = bool(_LEAD.search(text))
    value = _LEAD.sub("", text).strip().rstrip(".?!")

    if not value:
        return SkillResult(speech="What would you like me to remember?", ok=False)

    # A question that wasn't explicitly prefixed with "remember" is a recall
    # attempt that landed here by mistake. Answer it instead of storing it —
    # storing questions corrupts the memory for every later lookup.
    if not had_lead and _QUESTION.match(value):
        hits = memory.recall(value, k=3)
        strong = [h for h in hits if h.score >= 0.55]
        if strong:
            return SkillResult(
                speech=strong[0].value,
                data={"recalled": [h.value for h in strong], "rerouted": True},
            )
        return SkillResult(
            speech="I don't have anything about that.",
            ok=False,
            data={"rerouted": True},
        )

    kind = "preference" if _PREFERENCE.search(value) else "fact"
    memory.remember(value, kind=kind)

    return SkillResult(
        speech=f"Noted — {value}.",
        data={"remembered": value, "kind": kind},
    )


@skill(
    name="memory.recall",
    tier="L0",
    description="Recall something previously remembered",
    examples=[
        "what's my sister's name",
        "what did i tell you about the deadline",
        "do you remember my wifi password",
        "what do you know about my car",
        "remind me what i said about the project",
        "what do you remember about me",
        "what are my preferences",
        # Interrogative frames about personal facts. Without broad coverage
        # here, "where am i employed" drifts to unrelated skills and
        # "when is the project due" gets stored as a fact instead of answered.
        "when is the deadline",
        "when is the project due",
        "where did i leave my car",
        "where did i park",
        "where do i work",
        "where am i employed",
        "who is my manager",
        "what is my wifi password",
        "what is my address",
        "what did i say about that",
        "what theme do i prefer",
        "how do i like things done",
        "which one do i prefer",
        "did i tell you about my sister",
        "was there something about the meeting",
    ],
)
def recall(
    query: Annotated[str, "what to look for"],
) -> SkillResult:
    # "remember that X" and "do you remember X" are near-identical to an
    # embedding matcher but opposite in intent. Rather than fight that, each
    # skill detects the other's frame and hands over. An imperative store
    # instruction that lands here gets stored, not looked up.
    if _LEAD.match(query) and not _QUESTION.match(query):
        value = _LEAD.sub("", query).strip().rstrip(".?!")
        if value:
            kind = "preference" if _PREFERENCE.search(value) else "fact"
            memory.remember(value, kind=kind)
            return SkillResult(
                speech=f"Noted — {value}.",
                data={"remembered": value, "kind": kind, "rerouted": True},
            )

    hits = memory.recall(query, k=4)
    strong = [h for h in hits if h.score >= 0.55]

    if not strong:
        return SkillResult(
            speech="I don't have anything about that.",
            ok=False,
            data={"considered": [{"value": h.value, "score": h.score} for h in hits]},
        )

    best = strong[0]
    speech = best.value
    if len(strong) > 1:
        speech += f" Also: {strong[1].value}"

    return SkillResult(
        speech=speech,
        data={
            "matches": [
                {"value": h.value, "kind": h.kind, "score": round(h.score, 3)}
                for h in strong
            ]
        },
    )


@skill(
    name="memory.list",
    tier="L0",
    description="List everything FRIDAY remembers",
    examples=[
        "what do you remember",
        "list your memories",
        "show me everything you know",
        "what have i told you",
    ],
)
def list_memories(
    kind: Annotated[str, "optional filter: fact, preference, or procedure"] = "",
) -> SkillResult:
    rows = memory.all_memories(kind=kind or None, limit=50)
    if not rows:
        return SkillResult(speech="I don't remember anything yet.")

    preview = "; ".join(r["value"][:50] for r in rows[:4])
    more = f" and {len(rows) - 4} more" if len(rows) > 4 else ""

    return SkillResult(
        speech=f"I remember {len(rows)} things. {preview}{more}.",
        data={"memories": rows},
    )


@skill(
    name="memory.forget",
    tier="L2",
    action="delete",
    description="Forget something previously remembered",
    examples=[
        "forget what i said about the deadline",
        "forget my wifi password",
        "delete that memory",
        "erase what you know about my car",
    ],
    dry_run=lambda query: f"Delete memories closely matching '{query}'",
)
def forget(
    query: Annotated[str, "what to forget"],
) -> SkillResult:
    cleaned = re.sub(
        r"^\s*(please\s+)?(forget|delete|erase|remove)\s+"
        r"(what (i|you) (said|know|told me) about\s+|that\s+|my\s+)?",
        "", query, flags=re.IGNORECASE,
    ).strip()

    removed = memory.forget_matching(cleaned or query)
    if not removed:
        return SkillResult(speech="I don't have anything matching that.", ok=False)

    return SkillResult(
        speech=f"Forgotten: {removed[0]}"
        + (f" and {len(removed) - 1} more." if len(removed) > 1 else "."),
        data={"forgotten": removed},
    )
