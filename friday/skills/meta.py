"""Meta skills: FRIDAY reasoning about itself — undo, capabilities, history."""

from __future__ import annotations

from typing import Annotated

from friday import audit, undo
from friday.registry import REGISTRY, SkillResult, skill


@skill(
    name="meta.undo",
    tier="L1",
    action="modify",
    description="Undo the last action FRIDAY took",
    examples=[
        "undo that",
        "undo",
        "revert that",
        "take that back",
        "put it back the way it was",
        "cancel what you just did",
        "no go back",
    ],
)
async def undo_last(
    count: Annotated[int, "how many actions to reverse"] = 1,
) -> SkillResult:
    lines = await undo.undo_last(count)
    if not lines:
        return SkillResult(speech="There's nothing I can undo.", ok=False)
    return SkillResult(speech=" ".join(lines), data={"undone": lines})


@skill(
    name="meta.capabilities",
    tier="L0",
    description="List what FRIDAY can do",
    examples=[
        "what can you do",
        "what are your skills",
        "help",
        "show me your capabilities",
        "what commands do you know",
        "list what you can do",
    ],
)
def capabilities(
    area: Annotated[str, "optional group filter, e.g. 'system' or 'files'"] = "",
) -> SkillResult:
    skills = sorted(REGISTRY.all(), key=lambda s: s.name)
    if area:
        skills = [s for s in skills if s.name.startswith(area.lower())]
        if not skills:
            return SkillResult(speech=f"I have nothing under {area}.", ok=False)

    groups: dict[str, list[str]] = {}
    for s in skills:
        groups.setdefault(s.name.split(".")[0], []).append(s.name)

    summary = ", ".join(f"{g} ({len(v)})" for g, v in sorted(groups.items()))
    return SkillResult(
        speech=f"I have {len(skills)} skills across {summary}.",
        data={
            "groups": groups,
            "skills": [
                {"name": s.name, "tier": s.tier, "description": s.description}
                for s in skills
            ],
        },
    )


@skill(
    name="meta.recent",
    tier="L0",
    description="Report what FRIDAY did recently",
    examples=[
        "what did you just do",
        "what have you done",
        "show me recent actions",
        "what was the last thing you did",
        "your history",
    ],
)
def recent(
    count: Annotated[int, "how many actions to report"] = 5,
) -> SkillResult:
    rows = audit.recent(count)
    if not rows:
        return SkillResult(speech="I haven't done anything yet.")

    names = [r["skill"] for r in rows]
    return SkillResult(
        speech=f"Most recently: {', '.join(names[:5])}.",
        data={"actions": rows},
    )
