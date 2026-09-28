"""The brain's front door: utterance in, decision out.

    understand("turn it up a bit")
      -> Understanding(action=ACT, skill="system.volume.up", args={}, score=0.71)

The engine only *decides*. Executing the chosen skill is the executor's job, so
permission checks can never be bypassed by the brain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from friday.brain.extract import _resolve_app_name, _strip_to_object, extract
from friday.brain.matcher import MATCHER, Match
from friday.brain.normalize import normalize
from friday.brain.rejects import SENTINEL
from friday.config import CFG
from friday.log import get
from friday.registry import REGISTRY

log = get(__name__)

# Explicit launch verbs only — deliberately narrower than extract._LEAD_VERBS,
# which also covers close/search/switch phrasing that has nothing to do with
# this ambiguity.
_APP_LAUNCH_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|would you\s+|will you\s+)?"
    r"(open up|open|launch|start up|start|run|fire up|boot up|bring up|pull up|load up|load)\s+",
    re.IGNORECASE,
)

# A project/workspace target keeps the utterance on project.open even when it
# also names an app, e.g. "open the friday project in vs code".
_PROJECT_CONTEXT = re.compile(
    r"\b(project|repo|repository|codebase|workspace|solution)\b", re.IGNORECASE
)


def _prefer_known_app(clean: str, candidates: list[Match]) -> list[Match]:
    """Bare "open/launch/start <app>" beats project.open when <app> is a real,
    installed application and the utterance carries no project context of its
    own.

    Both skills legitimately use "vs code" in their example phrasings
    (apps.open: "fire up vs code"; project.open: "launch vs code on this
    project"), so the embedding matcher alone sometimes ranks project.open
    first for a bare "open vscode" — it has more examples pulling toward that
    token. This breaks the tie using the same app-discovery index apps.open
    itself relies on, rather than guessing from phrasing alone.
    """
    top = candidates[0]
    if top.skill != "project.open":
        return candidates
    if _PROJECT_CONTEXT.search(clean) or not _APP_LAUNCH_LEAD.match(clean):
        return candidates

    obj = _strip_to_object(clean)
    if not obj:
        return candidates

    from friday.skills.apps import resolve_app

    if resolve_app(_resolve_app_name(obj)) is None:
        return candidates

    promoted = [c for c in candidates if c.skill == "apps.open"]
    rest = [c for c in candidates if c.skill != "apps.open"]
    if promoted:
        return promoted + rest
    return [Match(skill="apps.open", score=top.score, matched_example=clean)] + rest


# Phase 14.0 — "focus VS Code" routing fix. Explicit focus/switch verbs
# only, deliberately narrower than extract._LEAD_VERBS's own broader list
# (which also covers open/close/search phrasing unrelated to this
# ambiguity). "bring up"/"pull up"/"show me" are left out here even though
# apps.focus lists some of them as its own examples: those phrasings
# already score well on their own (undocumented as broken in Phase 13.0's
# audit) and overlap with _APP_LAUNCH_LEAD's "bring up"/"pull up", which
# exist for the open/project.open tie-break above — adding them here too
# would risk the two tie-breaks fighting over the same utterance.
_FOCUS_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|would you\s+|will you\s+)?"
    r"(focus(\s+on)?|switch to|switch over to|go to|jump to)\s+",
    re.IGNORECASE,
)


def _prefer_known_focus(clean: str, candidates: list[Match]) -> list[Match]:
    """Bare "focus/switch to/go to <app>" beats an unrelated top match
    (confirmed empirically, Phase 13.0 §7: "focus VS Code" landed on
    `input.type` rather than `apps.focus`) when <app> is a real, installed
    application — the same real-world-verification approach
    `_prefer_known_app` above uses for "open", rather than re-tuning
    `apps.focus`'s own example corpus.

    A no-op whenever the top candidate is already `apps.focus` (the common
    case — "switch to chrome"/"focus on spotify" already score correctly),
    so this can never demote a working decision, only rescue a wrong one.
    """
    top = candidates[0]
    if top.skill == "apps.focus":
        return candidates
    if not _FOCUS_LEAD.match(clean):
        return candidates

    obj = _strip_to_object(clean)
    if not obj:
        return candidates

    from friday.skills.apps import resolve_app

    if resolve_app(_resolve_app_name(obj)) is None:
        return candidates

    promoted = [c for c in candidates if c.skill == "apps.focus"]
    rest = [c for c in candidates if c.skill != "apps.focus"]
    if promoted:
        return promoted + rest
    return [Match(skill="apps.focus", score=top.score, matched_example=clean)] + rest


# Phase 13.0 — entity-type compatibility for post-resolution routing (see
# `route_with_resolved_entity` below). Maps a `friday.intelligence
# .context_memory` entity_type to the skill parameter names that can
# plausibly accept it, derived straight from the existing registry
# (`friday.registry.Skill.params`) rather than a hardcoded skill list —
# a new file-shaped skill becomes a valid route the moment it names one of
# these params, with no change needed here.
_ENTITY_PARAM_NAMES: dict[str, frozenset[str]] = {
    "file": frozenset({"path", "file", "folder", "directory"}),
    "app": frozenset({"app", "application", "program", "window"}),
    "contact": frozenset({"contact"}),
    "browser_page": frozenset({"url"}),
}
# "project" is scoped to the project.* namespace rather than a bare "name"
# param — "name" alone is also used by unrelated skills (schedule.delete,
# process.kill, routine.run) that have nothing to do with a project entity.
_PROJECT_NAMESPACE = "project."

# Generic, value-free noun phrase for each entity type — substituted in
# place of the concrete resolved value (a path, a contact name, ...) only
# for the routing-only match `route_with_resolved_entity` performs.
# Matching on the raw value instead is exactly what let a resolved file
# path drift the embedding match to an unrelated skill (e.g. "Read
# C:\...\report.pdf." matching knowledge.index or weather.now purely
# because the literal word "file" was gone) — see PLAN.md Phase 13.0,
# originally recorded as an unfixed limitation in Phase 12.0 §5.
_ENTITY_CANONICAL_PHRASE: dict[str, str] = {
    "file": "the file",
    "app": "the app",
    "contact": "the contact",
    "project": "the project",
    "browser_page": "the page",
}


def _skill_accepts_entity(skill_name: str, entity_type: str) -> bool:
    skill = REGISTRY.get(skill_name)
    if skill is None:
        return False
    if entity_type == "project":
        return skill_name.startswith(_PROJECT_NAMESPACE)
    wanted = _ENTITY_PARAM_NAMES.get(entity_type)
    if not wanted:
        return False
    return any(p.name in wanted for p in skill.params)


def route_with_resolved_entity(
    original_text: str, matched_span: str, entity_type: str, referent: str,
) -> "Understanding | None":
    """Re-run intent matching after a reference resolves, without letting
    the resolved value's own vocabulary (a path, a contact name, ...) drag
    the embedding match toward an unrelated skill.

    Matches on a generic canonical phrase for `entity_type` ("the file",
    never the literal path), so the match reflects the verb/intent alone —
    "original_text = intent, resolved entity = target" rather than
    "semantic_match(resolved value)" (brief §4/§6). Only a skill whose own
    parameters can actually accept this entity type
    (`_skill_accepts_entity`) is considered: an incompatible top match
    (e.g. `weather.now` for a "file" reference) is skipped in favor of the
    next-ranked compatible candidate, never guessed into.

    Returns `None` — caller keeps whatever understanding it already had —
    when `entity_type` has no canonical phrase, or no candidate at any
    rank is compatible with it.
    """
    phrase = _ENTITY_CANONICAL_PHRASE.get(entity_type)
    if phrase is None:
        return None

    from friday.intelligence.context_resolver import substitute

    canonical_text = substitute(original_text, matched_span, phrase)
    if canonical_text == original_text:
        return None
    canonical = BRAIN.understand(canonical_text)

    pool: list[Match] = []
    if canonical.skill:
        pool.append(Match(skill=canonical.skill, score=canonical.score, matched_example=""))
    pool.extend(canonical.candidates)

    chosen: str | None = None
    for match in pool:
        if _skill_accepts_entity(match.skill, entity_type):
            chosen = match.skill
            break
    if chosen is None:
        return None

    skill = REGISTRY.get(chosen)
    if skill is None:
        return None

    # The value-carrying text (the real referent, not the canonical
    # phrase) is what argument extraction runs against — filling the slot
    # is a separate step from choosing the skill (brief §6: extraction
    # must not drive intent; intent has already been chosen above).
    value_text = substitute(original_text, matched_span, referent)
    args, missing = extract(skill, value_text)

    if missing:
        return Understanding(
            action=Action.ASK_SLOT, utterance=original_text, normalized=value_text,
            skill=skill.name, args=args, score=canonical.score, missing=missing,
            speech=f"Which {missing[0]}?",
        )
    return Understanding(
        action=Action.ACT, utterance=original_text, normalized=value_text,
        skill=skill.name, args=args, score=canonical.score,
    )


class Action(str, Enum):
    ACT = "act"            # confident: run the skill
    CLARIFY = "clarify"    # plausible candidates, ask which
    ASK_SLOT = "ask_slot"  # right skill, missing a required argument
    UNKNOWN = "unknown"    # nothing close enough


@dataclass(slots=True)
class Understanding:
    action: Action
    utterance: str
    normalized: str
    skill: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    missing: list[str] = field(default_factory=list)
    candidates: list[Match] = field(default_factory=list)
    speech: str = ""


class Brain:
    def warm(self) -> None:
        """Load the model and build the intent index. Call once at startup."""
        MATCHER.build()

    def understand(self, utterance: str) -> Understanding:
        clean = normalize(utterance)

        if not clean:
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                speech="I didn't catch that.",
            )

        candidates = MATCHER.match(clean, top_k=5)
        if not candidates:
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                speech="I don't know how to do that yet.",
            )

        best = candidates[0]
        cfg = CFG.brain

        # The out-of-domain sentinel won: this isn't something FRIDAY does.
        # Checked before the thresholds, because a confident sentinel match is
        # exactly the case we must not let through.
        if best.skill == SENTINEL:
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                score=best.score,
                speech="That's not something I can do.",
            )

        # Drop the sentinel from the candidate list so it never gets offered.
        candidates = [c for c in candidates if c.skill != SENTINEL]
        if not candidates:
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                speech="That's not something I can do.",
            )
        candidates = _prefer_known_app(clean, candidates)
        candidates = _prefer_known_focus(clean, candidates)
        best = candidates[0]

        # Nothing is remotely close.
        if best.score < cfg.clarify_threshold:
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                score=best.score,
                candidates=candidates,
                speech="I don't know how to do that yet. You can teach me.",
            )

        # Plausible but not confident — ask rather than guess. Guessing wrong on
        # an L2/L3 skill is exactly what the tier system exists to prevent.
        if best.score < cfg.match_threshold:
            options = [c for c in candidates[:3] if c.score >= cfg.clarify_threshold]
            names = [REGISTRY.get(o.skill) for o in options]
            listed = ", or ".join(s.description.lower() for s in names if s)
            return Understanding(
                action=Action.CLARIFY,
                utterance=utterance,
                normalized=clean,
                score=best.score,
                candidates=options,
                speech=f"Did you want me to {listed}?" if listed else "I'm not sure.",
            )

        skill = REGISTRY.get(best.skill)
        if skill is None:  # corpus drifted from the registry
            return Understanding(
                action=Action.UNKNOWN,
                utterance=utterance,
                normalized=clean,
                speech="That skill isn't available.",
            )

        args, missing = extract(skill, clean)

        if missing:
            return Understanding(
                action=Action.ASK_SLOT,
                utterance=utterance,
                normalized=clean,
                skill=skill.name,
                args=args,
                score=best.score,
                missing=missing,
                candidates=candidates,
                speech=f"Which {missing[0]}?",
            )

        return Understanding(
            action=Action.ACT,
            utterance=utterance,
            normalized=clean,
            skill=skill.name,
            args=args,
            score=best.score,
            candidates=candidates,
        )

    def teach(self, utterance: str, skill_name: str) -> None:
        MATCHER.teach(normalize(utterance), skill_name)


BRAIN = Brain()
