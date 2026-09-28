"""Deterministic speech truncation — no LLM in this loop.

Most skills already keep `speech` short (see PLAN.md's P4/P5 notes on
browser.read/knowledge.ask bounding their snippets), but voice is the one
interface that *must* stay short regardless of what a skill returns, so this
is a final, cheap backstop rather than something every skill has to get right.
"""

from __future__ import annotations

DEFAULT_MAX_CHARS = 200


def for_speech(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary > max_chars * 0.4:
        return cut[: boundary + 1]

    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"
