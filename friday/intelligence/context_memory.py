"""Phase 11.4 — bounded, recent contextual entities.

Not a second memory system: `friday.memory` is durable long-term knowledge
("Priya is my sister"), `friday.intelligence.episodes` is durable executed
history ("what happened last time"), and `friday.intelligence.state.INTEL`
is the process-wide "what's happening this instant" record. This module adds
the one thing none of those provide — a small, bounded, *ordered* window of
concrete entities FRIDAY has recently touched or heard about (a file, an
app, a contact, a project, ...), which is exactly what "it" / "him" / "the
file" need to resolve against. See `friday.intelligence.context_resolver`
for the resolution logic that reads this.

Bounded the same way `friday.intelligence.state.IntelligenceState` is
bounded: a fixed-size `deque`, never a plain list — the oldest entity falls
out of the window on its own once enough newer ones have been remembered,
no separate expiry/GC pass needed (brief §3, §18).

Secrets are never stored: `_looks_secret` mirrors the redaction patterns
`friday.intelligence.episodes` (`_SECRET_KEY_MARKERS`) and
`friday.intelligence.experience` (`_SECRET_PATTERN`) already use — this
codebase's convention is a small local check per module rather than one
shared redaction helper (see experience.py's own docstring on this), kept
here for the same reason.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from friday.log import get

log = get(__name__)

# Open set, documented rather than enforced: whatever a caller finds useful
# to remember. Kept here as a reference for what `record_from_skill` below
# actually produces today.
ENTITY_TYPES = (
    "app", "file", "project", "contact", "window", "browser_page",
    "conversation", "action", "preference",
)

# Same marker list episodes._SECRET_KEY_MARKERS uses, for arbitrary `raw`
# metadata keys.
_SECRET_KEY_MARKERS = (
    "password", "passwd", "token", "secret", "otp", "pin", "api_key",
    "apikey", "auth", "credential", "cookie", "session_id",
)

# Same shape as experience._SECRET_PATTERN, for the free-text display_name
# itself (e.g. never remember "password: hunter2" as a display name).
_SECRET_TEXT_PATTERN = re.compile(
    r"\b(password|passwd|token|secret|api[_-]?key|otp)\b\s*[:=]", re.IGNORECASE
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_secret(text: str) -> bool:
    return bool(_SECRET_TEXT_PATTERN.search(text or ""))


def _sanitize_raw(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            continue  # dropped entirely, not even "[redacted]" — raw is optional metadata
        text = str(value)
        if _looks_secret(text):
            continue
        out[key] = text[:200]
    return out or None


def _max_items() -> int:
    from friday.config import CFG

    return max(1, CFG.intelligence.context_max_items)


@dataclass(slots=True)
class ContextEntity:
    """One bounded, recent entity — safe to hand to a planner prompt or a
    reference resolver. Never a raw screenshot/OCR dump (brief §13)."""

    entity_type: str
    display_name: str
    source: str = ""
    at: str = field(default_factory=_now)
    confidence: float = 1.0
    relevance: float = 1.0
    goal_id: str | None = None
    raw: dict[str, Any] | None = None
    # Groups entities introduced by the same utterance/goal (e.g. "open
    # report.pdf and results.xlsx" remembers both files under one turn_id).
    # `friday.intelligence.context_resolver` uses this — not `at`/recency
    # alone — to tell "two things mentioned in the same breath, genuinely
    # ambiguous" apart from "two things mentioned in separate turns, where
    # the most recent one legitimately wins." `None` (the default — most
    # direct `remember()` calls that aren't part of a multi-entity turn)
    # never participates in a tie.
    turn_id: str | None = None

    def describe(self) -> str:
        return f"{self.entity_type}: {self.display_name}"


class ContextMemory:
    """Owns the single process-wide bounded window of recent entities."""

    def __init__(self) -> None:
        self._items: deque[ContextEntity] = deque(maxlen=_max_items())

    def remember(
        self,
        entity_type: str,
        display_name: str,
        *,
        source: str = "",
        confidence: float = 1.0,
        relevance: float = 1.0,
        goal_id: str | None = None,
        raw: dict[str, Any] | None = None,
        turn_id: str | None = None,
    ) -> ContextEntity | None:
        """Add one bounded entity to the recent-context window.

        Returns `None` (and remembers nothing) for an empty or secret-
        looking display name — this window must never leak a credential
        into a planner prompt or a spoken clarification question.
        """
        name = (display_name or "").strip()
        if not name or _looks_secret(name):
            return None

        entity = ContextEntity(
            entity_type=entity_type,
            display_name=name[:200],
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            relevance=max(0.0, min(1.0, relevance)),
            goal_id=goal_id,
            raw=_sanitize_raw(raw),
            turn_id=turn_id,
        )
        # maxlen is read fresh each time so a test that overrides
        # CFG.intelligence.context_max_items mid-run takes effect on the
        # next remember() rather than needing a reset().
        if self._items.maxlen != _max_items():
            self._items = deque(self._items, maxlen=_max_items())
        self._items.append(entity)
        log.debug("context: remembered %s %r (source=%s)", entity_type, name, source)
        return entity

    def recent(self, entity_type: str | None = None, limit: int | None = None) -> list[ContextEntity]:
        """Newest-first, optionally filtered by type."""
        items = [e for e in reversed(self._items) if entity_type is None or e.entity_type == entity_type]
        return items[:limit] if limit else items

    def candidates(self, entity_type: str | None = None, limit: int = 5) -> list[ContextEntity]:
        """Newest-first, de-duplicated by (type, lowercased display name).

        This is the pool a reference resolver reasons about: re-opening the
        same file twice must not look like two distinct plausible referents
        for "it", but two genuinely different recent files must.
        """
        seen: set[tuple[str, str]] = set()
        out: list[ContextEntity] = []
        for e in reversed(self._items):
            if entity_type is not None and e.entity_type != entity_type:
                continue
            key = (e.entity_type, e.display_name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def most_recent(self, entity_type: str | None = None) -> ContextEntity | None:
        items = self.recent(entity_type, limit=1)
        return items[0] if items else None

    def as_context(self, limit: int = 5, max_chars: int = 400) -> str:
        """Bounded, plain-text summary for a planner prompt — same
        `[:max_chars].rstrip() + "…"` pattern WorkingMemory.as_context and
        RelevantExperience.as_context already use."""
        items = self.candidates(limit=limit)
        if not items:
            return ""
        text = "Recent context: " + "; ".join(e.describe() for e in items) + "."
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text

    def reset(self) -> None:
        """Start a fresh, empty window. Tests use this between cases."""
        self._items = deque(maxlen=_max_items())


# -- skill -> entity adapter --------------------------------------------------

# Deliberately explicit and small, matching this codebase's convention for
# this kind of lookup table (episodes._SECRET_KEY_MARKERS, project.py's
# _STACK_MARKERS): only skills whose args/result actually name a concrete,
# reusable entity get one recorded. A skill not listed here (system.volume,
# weather.now, ...) simply records nothing — never a guess at what "the
# entity" might be.
#
# Each entry: skill name -> (entity_type, display_name_fn). display_name_fn
# receives (args, data) — the call's args and the SkillResult.data it
# returned — and returns a display name, or "" to skip.
def _file_full_path(_args: dict, data: dict) -> str:
    path = data.get("path") or _args.get("path") or ""
    if not path:
        results = data.get("results") or []
        if len(results) == 1:
            path = results[0].get("path", "")
    return path


def _file_name(_args: dict, data: dict) -> str:
    from pathlib import Path

    path = _file_full_path(_args, data)
    return Path(path).name if path else ""


_SKILL_ENTITY_MAP: dict[str, tuple[str, Any]] = {
    "apps.open": ("app", lambda args, data: data.get("app") or args.get("app", "")),
    # Names below must match the actually-registered skill names in
    # friday.skills.apps/friday.skills.browser exactly (`apps.focus`, not
    # `apps.focus_window`; `apps.close`, not `apps.close_app`; `browser.open`,
    # not `browser.goto`) — the earlier names never matched anything real,
    # so focusing/closing an app or navigating the browser silently never
    # populated context memory. Found via the Phase 12.0 reliability harness
    # (scenarios D/G/H); see PLAN.md Phase 12.0 §4.
    "apps.focus": ("app", lambda args, data: args.get("app", "")),
    "apps.close": ("app", lambda args, data: args.get("app", "")),
    "files.read": ("file", _file_name),
    "files.reveal": ("file", _file_name),
    "whatsapp.compose": ("contact", lambda args, data: data.get("contact") or args.get("contact", "")),
    "project.open": ("project", lambda args, data: data.get("name") or args.get("name", "")),
    "project.inspect": ("project", lambda args, data: data.get("name") or args.get("name", "")),
    "browser.open": ("browser_page", lambda args, data: data.get("title") or args.get("url", "")),
}


def record_from_skill(
    skill_name: str, args: dict[str, Any], data: dict[str, Any], *,
    goal_id: str | None = None, turn_id: str | None = None,
) -> ContextEntity | None:
    """Best-effort: derive and remember one contextual entity from a
    *successful* skill call, if `skill_name` names one worth remembering
    (see `_SKILL_ENTITY_MAP`). Never raises — callers (friday.session,
    friday.skills.plan) treat this exactly like every other intelligence-
    layer write: a failure here must never break the skill call itself.
    """
    mapping = _SKILL_ENTITY_MAP.get(skill_name)
    if mapping is None:
        return None
    entity_type, name_fn = mapping
    try:
        name = name_fn(args or {}, data or {})
    except Exception:
        log.exception("context_memory: entity extraction failed for %s (non-fatal)", skill_name)
        return None
    if not name:
        return None
    # A "file" entity's display_name is deliberately just the basename (see
    # _file_name) -- readable in a planner prompt/spoken clarification, but
    # never enough on its own to re-open the file: friday.brain.extract's
    # path-slot extraction requires something path-shaped (a drive letter,
    # ./, ../, or ~), which a bare filename never satisfies. The full path
    # rides along as `raw` so a resolved "it"/"that" can substitute back in
    # something files.read/files.reveal can actually use — see
    # context_resolver.resolve_reference. Found via the Phase 12.0
    # reliability harness's scenario D (the brief's own "open X" -> "read
    # it" example, which was silently broken without this); see PLAN.md
    # Phase 12.0 §4.
    raw = None
    if entity_type == "file":
        try:
            full_path = _file_full_path(args or {}, data or {})
        except Exception:
            full_path = ""
        if full_path:
            raw = {"path": full_path}
    return CONTEXT.remember(
        entity_type, str(name), source=f"skill:{skill_name}", goal_id=goal_id, turn_id=turn_id, raw=raw,
    )


# Module-level singleton, same pattern as friday.intelligence.state.INTEL.
CONTEXT = ContextMemory()
