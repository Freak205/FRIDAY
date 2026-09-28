"""Phase 11.4 — reference resolution against bounded contextual memory.

Turns "it" / "him" / "the file" / "my project" / "again" into a concrete
referent using `friday.intelligence.context_memory.CONTEXT` (recent
entities), `friday.intelligence.state.INTEL` (recent actions, for "again"),
and `friday.memory` (explicit stored preferences, for personalization) —
never a new memory store of its own.

The one rule this whole module exists to enforce: **never guess when more
than one recent candidate is plausible.** `resolve_reference` either returns
a single confident referent, or a `ResolutionResult` with `resolved=False`
and a ready-to-speak `clarification` — there is no third "best guess"
outcome. See `ContextEntity.turn_id`'s docstring for exactly how "two things
recent enough to be ambiguous" is distinguished from "one thing is clearly
more recent than the other."

Deterministic and cheap by design (brief §19): every check here is a regex
scan plus a handful of in-memory list operations — no LLM call, no
embedding, no extra DB round trip beyond what `CONTEXT`/`INTEL`/`memory`
already do. `contains_reference` is the fast-path gate a caller (see
`friday.session.Session`) should check first so an ordinary command with no
pronoun/reference never pays for any of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from friday.log import get

log = get(__name__)


@dataclass(slots=True)
class ResolutionResult:
    """Structured outcome of a resolution attempt — see module docstring."""

    resolved: bool
    referent: str | None = None
    entity_type: str | None = None
    confidence: float = 0.0
    reason: str = ""
    candidates: list[str] = field(default_factory=list)
    clarification: str | None = None
    # The literal substring matched in the original utterance ("it", "the
    # file", "him", ...), so a caller can substitute the resolved referent
    # back in, or splice a later clarification answer into the same spot.
    matched_span: str = ""


# -- reference detection ------------------------------------------------------

# Ordered most-specific-first: a phrase pattern that also implies an entity
# type (e.g. "the file") must win over the bare generic-pronoun pattern
# below it, or the type hint would be lost.
_REFERENCE_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"\b(my project|the project)\b", re.IGNORECASE), "project"),
    (re.compile(r"\bthe file\b", re.IGNORECASE), "file"),
    (re.compile(r"\b(the app|the application|the program)\b", re.IGNORECASE), "app"),
    (re.compile(r"\b(him|her)\b", re.IGNORECASE), "contact"),
    (
        re.compile(
            r"\b(the same one|that one|the other one|the last one|the previous one)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    (re.compile(r"\b(it|that|this|there|them)\b", re.IGNORECASE), None),
]

_TEMPORAL_REPEAT = re.compile(
    r"^(do (that|it)( again)?|again|do the same thing( as before)?|"
    r"same (thing|as before|as last time|as yesterday)|repeat that|"
    r"one more time|the previous one|what we just did)[.!]?$",
    re.IGNORECASE,
)

_MY_USUAL = re.compile(r"\bmy usual\b", re.IGNORECASE)


def is_temporal_repeat(text: str) -> bool:
    """Bounded, canonical "do that again" phrasings only — deliberately
    narrow (brief §7: "do not attempt unrestricted historical reasoning").
    A sentence that merely contains the word "again" as a modifier of
    something else ("search for it again") is not matched here; it still
    goes through ordinary pronoun resolution for "it" instead."""
    return bool(_TEMPORAL_REPEAT.match((text or "").strip()))


def is_personalization_query(text: str) -> bool:
    return bool(_MY_USUAL.search(text or ""))


def first_reference_span(text: str) -> str | None:
    """The literal substring matched by the first reference pattern in
    `text` ("it", "him", "the file", ...), or `None` — the read-only half
    of `resolve_pronoun_in_text`, exposed separately so a caller can check
    *whether and what* matched without resolving it. Used by
    `friday.session.Session._needs_context_resolution` to detect an
    `Action.ACT` decision whose own extracted argument is just this same
    span echoed back unresolved (brief §6: an unresolved pronoun passed
    through as an argument value is not real evidence for that skill).
    """
    for pattern, _hint in _REFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def contains_reference(text: str) -> bool:
    """Cheap fast-path gate: does `text` contain anything worth resolving?

    A `False` here means a caller should skip contextual resolution
    entirely and go straight to normal intent handling — the fast path for
    ordinary commands (brief §12/§19)."""
    if not text:
        return False
    if is_temporal_repeat(text) or is_personalization_query(text):
        return True
    return any(pattern.search(text) for pattern, _ in _REFERENCE_PATTERNS)


# -- entity reference resolution ----------------------------------------------


def resolve_reference(
    text: str, *, entity_type_hint: str | None = None, consequential: bool = False,
) -> ResolutionResult:
    """Resolve one entity reference (a pronoun, "the file", ...) against
    `ContextMemory`'s bounded recent-entity window.

    `entity_type_hint` narrows the candidate pool ("contact" for him/her,
    "file" for "the file", ...); `None` searches across every recently
    remembered entity type, for a bare "it"/"that". `consequential` (see
    `friday.risk.is_consequential`) applies the stricter of the two
    confidence thresholds in `CFG.intelligence` — brief §5's "for
    consequential actions, use an even stricter confidence threshold."

    `text` itself is only used for logging/diagnostics here — the actual
    match span is found by the caller (`resolve_pronoun_in_text` below);
    this function is also called directly by tests/callers that already
    know which entity type they're after.
    """
    from friday.config import CFG
    from friday.intelligence.context_memory import CONTEXT

    threshold = (
        CFG.intelligence.context_consequential_confidence_threshold
        if consequential else CFG.intelligence.context_confidence_threshold
    )
    label = entity_type_hint or "item"
    pool = CONTEXT.candidates(entity_type_hint, limit=5)

    if not pool:
        return ResolutionResult(
            resolved=False,
            reason=f"no recent {label} in context",
            clarification=f"I don't have a recent {label} to refer to. Could you tell me which one you mean?",
        )

    top = pool[0]
    tied_group = [e for e in pool if top.turn_id is not None and e.turn_id == top.turn_id]

    if len(tied_group) > 1:
        names: list[str] = []
        for e in tied_group:
            if e.display_name not in names:
                names.append(e.display_name)
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        return ResolutionResult(
            resolved=False,
            entity_type=top.entity_type,
            candidates=names,
            reason=f"{len(names)} {label}s introduced together — recency can't break the tie",
            clarification=f"I found {len(names)} recent {label}s: {listed}. Which one?",
        )

    if top.confidence < threshold:
        return ResolutionResult(
            resolved=False,
            reason=f"top candidate confidence {top.confidence:.2f} below threshold {threshold:.2f}",
            clarification=f"I'm not confident enough about which {label} you mean. Could you clarify?",
        )

    # A "file" entity's display_name is just a basename (see
    # context_memory._file_name) -- substituting that alone back into text
    # can never satisfy files.read/files.reveal's path-shaped slot
    # extraction. When the full path rode along as `raw["path"]`
    # (context_memory.record_from_skill), use it instead so "read it"
    # actually resolves to something openable. See PLAN.md Phase 12.0 §4.
    referent = top.display_name
    if top.entity_type == "file" and top.raw and top.raw.get("path"):
        referent = top.raw["path"]

    return ResolutionResult(
        resolved=True,
        referent=referent,
        entity_type=top.entity_type,
        confidence=top.confidence,
        reason=f"most recent matching {top.entity_type}",
    )


def resolve_display_name(entity_type: str, display_name: str) -> str:
    """The full referent for a display name already offered as a
    clarification candidate (see `resolve_reference`'s tied-group branch,
    which only stores `ContextEntity.display_name` — a file's basename,
    per `context_memory._file_name` — in `ResolutionResult.candidates`).

    Mirrors the single-candidate path's own preference for
    `raw["path"]` over the bare basename (module docstring / PLAN.md Phase
    12.0 §4): once the user picks "summary.pdf" from an ambiguity
    question, this looks up that same entity's full path so the answer is
    actually usable by `files.read`/`files.reveal`'s path-shaped slot,
    instead of asking a second, redundant "which path?" question. Returns
    `display_name` unchanged when no richer referent is on file (every
    other entity type, or a file entity recorded without a path).
    """
    from friday.intelligence.context_memory import CONTEXT

    for entity in CONTEXT.candidates(entity_type, limit=10):
        if entity.display_name.lower() != display_name.lower():
            continue
        if entity.entity_type == "file" and entity.raw and entity.raw.get("path"):
            return entity.raw["path"]
        return entity.display_name
    return display_name


def resolve_pronoun_in_text(
    text: str, *, consequential: bool = False,
) -> tuple[str, ResolutionResult | None]:
    """Find the first reference in `text`, resolve it, and splice the
    referent back in.

    Returns `(text, None)` when there is nothing to resolve (fast path).
    Returns `(substituted_text, result)` when resolved with sufficient
    confidence — `substituted_text` has the matched span replaced with the
    concrete referent, ready for normal intent handling. Returns
    `(text, result)` with `result.resolved=False` when a reference is
    present but must not be guessed — the caller must ask the
    `result.clarification` question instead of proceeding (brief §5/§14:
    context resolution must never authorize an action on a guess).
    """
    for pattern, hint in _REFERENCE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        result = resolve_reference(text, entity_type_hint=hint, consequential=consequential)
        result.matched_span = match.group(0)
        if not result.resolved:
            return text, result
        substituted = text[: match.start()] + result.referent + text[match.end():]
        return substituted, result
    return text, None


def resolve_temporal_repeat() -> ResolutionResult:
    """"Do that again" / "same as before" — resolves to the single most
    recent recorded action, only when one actually exists (brief §7: "only
    resolve if an actual relevant prior episode/action exists"). The caller
    (`friday.session.Session`) is the one that knows *how* to re-run it —
    this only certifies *that* there is exactly one clearly eligible thing
    to repeat.
    """
    from friday.intelligence.state import INTEL

    actions = list(INTEL.state.recent_actions)
    if not actions:
        return ResolutionResult(
            resolved=False,
            reason="no recent action recorded",
            clarification="I don't have anything recent to repeat.",
        )
    return ResolutionResult(
        resolved=True,
        referent=actions[-1],
        entity_type="action",
        confidence=0.85,
        reason="most recent recorded action",
    )


def resolve_personalization(phrase: str, *, consequential: bool = False) -> ResolutionResult:
    """"my usual browser" style personalization — resolves ONLY against an
    explicitly stored `friday.memory` preference (kind="preference"), never
    against how often something was merely used (brief §8: "observed
    behavior != confirmed preference"). No stored preference on file means
    no resolution, full stop — this function never falls back to a guess.
    """
    from friday.config import CFG
    from friday import memory as memory_mod

    threshold = (
        CFG.intelligence.context_consequential_confidence_threshold
        if consequential else CFG.intelligence.personalization_confidence_threshold
    )
    try:
        hits = memory_mod.recall(phrase, k=3, kind="preference")
    except Exception:
        log.exception("context_resolver: preference recall failed (non-fatal)")
        hits = []

    if not hits or hits[0].score < threshold:
        return ResolutionResult(
            resolved=False,
            reason="no explicit stored preference on file",
            clarification="I don't have a preference set for that yet.",
        )

    top = hits[0]
    return ResolutionResult(
        resolved=True,
        referent=top.value,
        entity_type="preference",
        confidence=float(top.score),
        reason="explicit stored preference",
    )


# -- corrections ---------------------------------------------------------------


def apply_correction(entity_type: str, display_name: str, *, source: str = "correction"):
    """A user correction ("no, I meant the backend project") should steer
    the *next* reference resolution without rewriting any existing
    memory/episode row. Remembering a fresh, high-confidence entity is
    enough: it becomes the most-recent (and therefore winning) candidate
    for the next "it"/"that" purely through `ContextMemory.candidates`'
    existing recency rule — no separate "corrected" flag needed.
    """
    from friday.intelligence.context_memory import CONTEXT

    return CONTEXT.remember(entity_type, display_name, source=source, confidence=1.0, relevance=1.0)


# -- clarification follow-ups --------------------------------------------------


def match_choice(text: str, options: list[str]) -> str | None:
    """Match a user's answer to an ambiguity question ("results.xlsx", "the
    second one", "the xlsx one") against the offered `options`. `None` means
    "didn't match any option" — the caller should treat the utterance as a
    fresh command instead, never as a wrong guess."""
    if not options:
        return None
    low = (text or "").lower().strip(" .!?")
    if low in ("first", "the first one", "first one", "1"):
        return options[0]
    if len(options) > 1 and low in ("second", "the second one", "second one", "2"):
        return options[1]
    for option in options:
        opt_low = option.lower()
        if opt_low in low or low in opt_low:
            return option
    return None


def substitute(template: str, span: str, referent: str) -> str:
    """Replace the first whole-word occurrence of `span` in `template` with
    `referent`, case-insensitively — used once a reference is resolved or a
    clarification question is answered. Word-boundary matched so a short
    span like "it" never clobbers a substring inside another word (e.g.
    "circuit"); replacement is done via a function, not a raw string, so a
    referent containing backslashes (a Windows path segment) is never
    misread as a regex backreference.
    """
    if not span or not template:
        return template
    pattern = re.compile(rf"\b{re.escape(span)}\b", re.IGNORECASE)
    return pattern.sub(lambda _m: referent, template, count=1)
