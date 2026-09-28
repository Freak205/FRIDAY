"""L1 — normalize a raw utterance before matching.

Cheap string work that removes the variation the embedding matcher shouldn't
have to learn: politeness, wake-word residue, filler, and the handful of
mistakes Whisper reliably makes on short commands.
"""

from __future__ import annotations

import re

from friday.config import CFG

_FILLERS = re.compile(
    r"\b(um+|uh+|er+|ah+|hmm+|like|you know|i mean|basically|actually|just)\b",
    re.IGNORECASE,
)

_POLITE = re.compile(
    r"^\s*(please|hey|ok(ay)?|so|now|alright|right)\b[,\s]*"
    r"|\b(please|thanks|thank you)\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Direct address, anchored to the edges of the utterance only. "friday" in the
# middle of a sentence is far more likely to be the weekday than the assistant.
_ADDRESS_EDGE = re.compile(
    r"^\s*(hey\s+)?(friday|computer|assistant)\b[,\s]*"
    r"|[,\s]+(hey\s+)?(friday|computer|assistant)\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# Whisper's habitual mistakes on short spoken commands.
_FIXES = {
    r"\bvolume up\b": "volume up",
    r"\bturn it up\b": "turn up the volume",
    r"\bturn it down\b": "turn down the volume",
    r"\bwifi\b": "wi-fi",
    r"\bscreen shot\b": "screenshot",
    r"\bvs code\b": "vscode",
    r"\bv s code\b": "vscode",
    r"\bmy pc\b": "the pc",
    r"\bthis pc\b": "the pc",
    r"\bwhats\b": "what's",
    r"\bwhat is\b": "what's",
}

_CONTRACTIONS = {
    "can't": "cannot", "won't": "will not", "n't": " not",
    "'re": " are", "'ve": " have", "'ll": " will", "'m": " am",
}


def normalize(text: str) -> str:
    """Return a cleaned, lowercase form suitable for embedding."""
    s = (text or "").strip().lower()
    if not s:
        return ""

    # Strip the wake word and direct address only where they'd actually appear —
    # at the start, or trailing. Stripping globally corrupts content that
    # legitimately contains the name: "the deadline is friday the 14th" became
    # "the deadline is the 14th".
    wake = CFG.identity.wake_word.lower()
    if wake and s.startswith(wake):
        s = s[len(wake):]
    s = _ADDRESS_EDGE.sub(" ", s)

    s = _POLITE.sub(" ", s)
    s = _FILLERS.sub(" ", s)

    for pattern, repl in _FIXES.items():
        s = re.sub(pattern, repl, s)

    s = re.sub(r"[^\w\s'\-.:/\\%]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,!?")
    return s


def expand(text: str) -> str:
    """Expand contractions — used for slot extraction, not for matching."""
    s = text
    for short, long in _CONTRACTIONS.items():
        s = s.replace(short, long)
    return s
