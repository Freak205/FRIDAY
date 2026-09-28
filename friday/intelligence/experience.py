"""Phase 11.1 — turning retrieved episodic experience into planner context.

Closes the Phase 10 gap noted in PLAN.md: `episodes.retrieve_similar()`
could already find semantically similar past goals, but nothing fed that
into `plan.run`'s own prompt. This module is the bridge — deterministic,
bounded, best-effort — from "past episodes relevant to this goal" to a
short block of text `friday/skills/plan.py` can append to the same bounded
context string that already carries the desktop summary and working
memory (see `_append_working_memory` there; `_append_experience` follows
the identical pattern).

What counts as "experience" here is always an *executed, observed*
`friday.intelligence.episodes.Episode` — a row written only after a real
`Orchestrator.run_goal` finished and its steps were actually run through
`friday.permissions.EXECUTOR`. This module never surfaces a raw,
unexecuted model-generated plan as if it were fact, and every item is
formatted as evidence ("this is what happened last time"), not as an
instruction the planner must follow — matching `retrieve_similar`'s own
docstring.

Only `goal_text`, the recorded step sequence (`Episode.plan`, already
secret-redacted at write time by `episodes._sanitize_args`), the failure
reason, and a linked user correction are ever surfaced. `Episode.context`
(the free-text working-memory/desktop snippet stored alongside each
episode) is deliberately never included here — it was only length-capped
at write time, not scrubbed for secrets, so leaving it out entirely avoids
that gap rather than adding a second redaction pass for it.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from friday.intelligence.episodes import Episode
from friday.log import get

log = get(__name__)

# Same pattern scripts/export_training_data.py already uses as a free-text
# backstop beyond episodes.py's argument-name redaction — kept local (this
# codebase's own convention: no shared cross-module truncate/redact helper,
# see WorkingMemory.as_context and episodes._sanitize_args) rather than
# importing a script module into friday/ proper.
_SECRET_PATTERN = re.compile(
    r"\b(password|passwd|token|secret|api[_-]?key|otp)\b\s*[:=]", re.IGNORECASE
)


def _redact(text: str) -> str:
    return "[omitted: looked sensitive]" if _SECRET_PATTERN.search(text) else text


def _plan_step_summary(episode: Episode, *, max_steps: int = 4) -> str:
    tools = [str(step.get("tool", "?")) for step in episode.plan[:max_steps]]
    if not tools:
        return "(no steps recorded)"
    summary = " -> ".join(tools)
    if len(episode.plan) > max_steps:
        summary += " -> ..."
    return summary


def _failure_reason(episode: Episode) -> str:
    for step in episode.plan:
        if not step.get("ok", True) and step.get("error"):
            return f"{step.get('tool', '?')} failed: {str(step['error'])[:120]}"
    if episode.errors:
        return episode.errors[:160]
    return "unknown reason"


def _correction_for(episode: Episode) -> str | None:
    """A user correction recorded against this same goal, if any — the only
    way a correction reaches planner context: tied to a goal that was
    already judged relevant, never a global dump of every correction."""
    if not episode.goal_id:
        return None
    try:
        from friday.intelligence import corrections

        hits = corrections.for_goal(episode.goal_id)
    except Exception:
        log.exception("experience: correction lookup failed (non-fatal)")
        return None
    if not hits:
        return None
    text = _redact(hits[0].user_correction.strip())[:160]
    return text or None


@dataclass(slots=True)
class RelevantExperience:
    """Bounded, already-ranked evidence from past episodes for one goal."""

    successes: list[Episode] = field(default_factory=list)
    failures: list[Episode] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.successes and not self.failures

    def as_context(self, max_chars: int = 4000) -> str:
        """Format as a compact block for a planner prompt, hard-capped at
        `max_chars` (same `[:n].rstrip() + "…"` pattern WorkingMemory.as_context
        uses, for the same reason: a prompt must stay bounded no matter how
        much text the items above would otherwise add up to)."""
        if self.is_empty():
            return ""

        lines = [
            "RELEVANT PAST EXPERIENCE (evidence from prior runs — guidance only, "
            "not a script to replay verbatim; tools, arguments, or the situation "
            "may have changed since):"
        ]
        n = 0
        for ep in self.successes:
            n += 1
            lines.append(f'{n}. SUCCEEDED before: "{ep.goal_text[:100]}" — steps: {_plan_step_summary(ep)}')
            corr = _correction_for(ep)
            if corr:
                lines.append(f'   User correction on file for that goal: "{corr}"')
        for ep in self.failures:
            n += 1
            lines.append(f'{n}. FAILED before: "{ep.goal_text[:100]}" — {_failure_reason(ep)}')
            corr = _correction_for(ep)
            if corr:
                lines.append(f'   User correction on file for that goal: "{corr}"')

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text


def retrieve_relevant_experience(
    goal_text: str,
    limit: int | None = None,
    *,
    threshold: float | None = None,
) -> RelevantExperience:
    """Bounded, deterministic retrieval of past experience relevant to `goal_text`.

    Retrieves successes and failures independently (each already ranked by
    semantic similarity to `goal_text` and threshold-filtered — see
    `friday.intelligence.episodes.retrieve_similar`/`retrieve_similar_failures`),
    then interleaves them and caps the combined total at `limit` so neither
    category can crowd the other out: a known failure is exactly as
    important as a known success (see PLAN.md Phase 11.1).

    Never raises — any retrieval failure yields an empty result, the same
    best-effort contract the rest of `friday.intelligence` follows, so a
    broken embedding model or a locked database can never break `plan.run`.
    """
    from friday.config import CFG
    from friday.intelligence import episodes

    limit = CFG.intelligence.experience_max_episodes if limit is None else limit
    goal_text = (goal_text or "").strip()
    if limit <= 0 or not goal_text:
        return RelevantExperience()

    try:
        successes = episodes.retrieve_similar(goal_text, k=limit, success_only=True, threshold=threshold)
    except Exception:
        log.exception("experience: success retrieval failed (non-fatal)")
        successes = []

    try:
        failures = episodes.retrieve_similar_failures(goal_text, k=limit, threshold=threshold)
    except Exception:
        log.exception("experience: failure retrieval failed (non-fatal)")
        failures = []

    ordered: list[tuple[str, Episode]] = []
    for s, f in itertools.zip_longest(successes, failures):
        if s is not None:
            ordered.append(("success", s))
        if f is not None:
            ordered.append(("failure", f))
    ordered = ordered[:limit]

    return RelevantExperience(
        successes=[ep for kind, ep in ordered if kind == "success"],
        failures=[ep for kind, ep in ordered if kind == "failure"],
    )
