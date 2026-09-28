"""Post-STT formatting aliases — NOT an accuracy fix (Phase 10.X.3).

Whisper transcribes speech phonetically/casually ("vs code", "whats app",
"chrome browser"); this module only re-cases/re-joins a short, fixed list of
predictable product-name variants into their canonical written form. It must
never be used to paper over a genuinely wrong transcription (e.g. mishearing
"Chrome" as "Kroom") — that's a model/decoding/VAD problem to fix upstream in
friday.voice.stt/friday.voice.capture, not something to special-case here.

Applied once, right after transcription (see SttEngine._run), so both the
real capture path and the confirmation-answer side-channel get it for free.
The brain's own fuzzy matching (friday.brain) doesn't need exact casing to
work, so this is purely cosmetic for logs/audit/spoken-back text — never
load-bearing for intent matching.
"""

from __future__ import annotations

import re

# (compiled case-insensitive pattern, canonical replacement). Order matters:
# longer/more-specific phrases first so e.g. "visual studio code" doesn't get
# partially matched by a shorter rule first.
_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bvisual studio code\b", re.IGNORECASE), "Visual Studio Code"),
    (re.compile(r"\bvs\s*code\b", re.IGNORECASE), "VS Code"),
    (re.compile(r"\bvscode\b", re.IGNORECASE), "VS Code"),
    (re.compile(r"\bwhats\s*app\b", re.IGNORECASE), "WhatsApp"),
    (re.compile(r"\bwhat's\s*app\b", re.IGNORECASE), "WhatsApp"),
    (re.compile(r"\bgit\s*hub\b", re.IGNORECASE), "GitHub"),
    (re.compile(r"\bchat\s*gpt\b", re.IGNORECASE), "ChatGPT"),
    (re.compile(r"\bchrome\b", re.IGNORECASE), "Chrome"),
    (re.compile(r"\bollama\b", re.IGNORECASE), "Ollama"),
    (re.compile(r"\bfriday\b", re.IGNORECASE), "FRIDAY"),
]


def normalize_transcript(text: str) -> str:
    """Re-cases known product-name aliases in `text`. Returns `text`
    unchanged (including its original casing/spacing) for anything not
    matched by the fixed list above.
    """
    if not text:
        return text
    result = text
    for pattern, canonical in _ALIASES:
        result = pattern.sub(canonical, result)
    return result
