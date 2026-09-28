"""Bounded, task-relevant working memory.

Not a copy of long-term memory — a small, cheap-to-build snapshot of what's
actually relevant to *this* request: the active project/window, a couple of
recent actions FRIDAY just took, and a handful of long-term facts that are
semantically close to the current goal (reusing `friday.memory.context_block`,
already bounded by `k`/`threshold` — no new retrieval mechanism needed).

`WorkingMemory.as_context()` is the only thing a planner prompt ever sees,
and it is hard-capped in characters — see `friday.config.IntelligenceConfig`.
This is the "never dump the entire long-term memory into every prompt"
requirement, made concrete and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkingMemory:
    active_project: str = ""
    active_app: str = ""
    active_window: str = ""
    recent_actions: list[str] = field(default_factory=list)
    relevant_facts: list[str] = field(default_factory=list)

    def as_context(self, max_chars: int = 400) -> str:
        parts: list[str] = []
        if self.active_window:
            where = f"Active window: {self.active_window}"
            where += f" ({self.active_app})." if self.active_app else "."
            parts.append(where)
        if self.active_project:
            parts.append(f"Active project: {self.active_project}.")
        if self.recent_actions:
            parts.append("Recent actions: " + "; ".join(self.recent_actions) + ".")
        if self.relevant_facts:
            parts.append("Relevant memory: " + " ".join(self.relevant_facts))
        text = " ".join(parts).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text


def build(
    goal_text: str,
    *,
    observation: Any | None = None,
    max_facts: int | None = None,
    max_actions: int = 3,
) -> WorkingMemory:
    """Assemble a bounded working-memory snapshot for `goal_text`.

    `observation` is an optional `friday.desktop_observer.DesktopObservation`
    a caller already fetched — reused here instead of taking another
    screenshot/OCR pass just to learn the active window.
    """
    from friday.config import CFG
    from friday.intelligence.state import INTEL

    max_facts = CFG.intelligence.working_memory_max_facts if max_facts is None else max_facts

    wm = WorkingMemory()
    if observation is not None:
        wm.active_app = getattr(observation, "active_app", "") or ""
        wm.active_window = getattr(observation, "active_window_title", "") or ""

    wm.recent_actions = list(INTEL.state.recent_actions)[-max_actions:]

    if goal_text.strip():
        try:
            from friday import memory as memory_mod

            block = memory_mod.context_block(goal_text, k=max_facts)
            wm.relevant_facts = [line[2:] for line in block.splitlines() if line.startswith("- ")]
        except Exception:
            wm.relevant_facts = []

    return wm
