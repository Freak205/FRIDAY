"""Command-vocabulary hint for STT decoding (Phase 10.X.5) — NOT a text
replacer. `build_hotwords()` turns a short list of known FRIDAY entity names
into the single string faster-whisper's `hotwords` decode option takes.

`hotwords` biases the decoder's token probabilities toward these words while
it is deciding what was said; it never rewrites text after the fact and
never forces one of these words into the output, so a vocabulary entry can
only make a correct hearing more likely, not silently substitute a word the
user didn't say — that's why this is safe where a generic spell-corrector
would not be (see friday/voice/normalize.py's docstring for the same
distinction drawn for cosmetic re-casing).

DEFAULT_VOCABULARY is the fixed set of app/product names most likely to
appear in a FRIDAY command per the Phase 10.X.5 brief. Whether it's actually
wired in by default lives in config.yaml's `voice.stt.vocabulary`
(SttConfig.vocabulary) — see that field's comment and scripts/benchmark_stt.py
for the measurement before trusting it blindly.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_VOCABULARY: tuple[str, ...] = (
    "FRIDAY",
    "VS Code",
    "WhatsApp",
    "Chrome",
    "Notepad",
    "Windows",
    "GitHub",
    "Python",
    "Ollama",
    "ChatGPT",
)


def build_hotwords(vocabulary: Sequence[str] | None) -> str:
    """Joins `vocabulary` into the single string faster-whisper's `hotwords`
    parameter expects, deduplicating case-insensitively (keeping the first
    casing seen) and dropping blank entries. Returns "" for None/empty input
    — callers should treat that as "no hint" (pass `hotwords=None`), exactly
    like SttConfig.initial_prompt's "blank = none" convention.
    """
    if not vocabulary:
        return ""
    seen: set[str] = set()
    words: list[str] = []
    for word in vocabulary:
        cleaned = word.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(cleaned)
    return ", ".join(words)
