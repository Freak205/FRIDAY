"""Routines: named multi-step sequences.

    "create a routine called morning that tells me the time then my battery
     then the weather"

Each step is compiled to a concrete skill call at creation time — same recipe
model as scheduled jobs, so running a routine costs nothing and never drifts.
A routine is stored as a job with a `manual` trigger; scheduling one later is
just attaching a trigger to it.
"""

from __future__ import annotations

import re
from typing import Annotated

from friday import jobs
from friday.log import get
from friday.registry import SkillResult, skill

log = get(__name__)

# Step separators, longest first so "and then" wins over "and".
_SPLIT = re.compile(
    r"\s*(?:,\s*(?:and\s+)?then\s+|\s+and\s+then\s+|\s+then\s+|,\s*and\s+|,\s+|\s+and\s+)\s*",
    re.IGNORECASE,
)

_NAME_FROM = re.compile(
    r"(?:routine|sequence|macro|workflow)\s+(?:called|named)\s+([\w\s-]+?)"
    r"\s+(?:that|which|to|:)\s+",
    re.IGNORECASE,
)

_LEAD = re.compile(
    r"^\s*(please\s+)?(can you\s+)?"
    r"(create|make|add|define|set up|build|record)\s+"
    r"(a|an|the)?\s*(new\s+)?(routine|sequence|macro|workflow)\s*"
    r"(called|named)?\s*",
    re.IGNORECASE,
)


def _compile_steps(text: str) -> tuple[list[dict], list[str]]:
    """Turn 'do X then do Y' into concrete skill calls. Returns (actions, failed)."""
    from friday.brain import BRAIN
    from friday.brain.engine import Action

    actions: list[dict] = []
    failed: list[str] = []

    for part in _SPLIT.split(text):
        step = part.strip(" .,")
        if not step:
            continue
        understanding = BRAIN.understand(step)
        if understanding.action is Action.ACT and understanding.skill:
            actions.append({"skill": understanding.skill, "args": understanding.args})
        else:
            failed.append(step)

    return actions, failed


@skill(
    name="routine.create",
    tier="L1",
    action="modify",
    description="Create a named routine that runs several commands in order",
    examples=[
        "create a routine called morning that tells me the time then my battery",
        "make a routine named focus that mutes the volume then shows the desktop",
        "define a workflow called shutdown prep that locks the pc",
        "set up a sequence called status that checks my system then my network",
        "create a routine",
        "build me a macro",
    ],
)
def create(
    steps: Annotated[str, "the routine name and its steps"],
) -> SkillResult:
    name_match = _NAME_FROM.search(steps)
    if name_match:
        name = name_match.group(1).strip()
        body = steps[name_match.end():].strip()
    else:
        body = _LEAD.sub("", steps).strip()
        # "morning: do x then do y" or "morning that does x"
        head = re.match(r"^([\w\s-]{2,30}?)\s*(?::|\bthat\b|\bwhich\b)\s+", body)
        if head:
            name = head.group(1).strip()
            body = body[head.end():].strip()
        else:
            name = ""

    if not body:
        return SkillResult(
            speech="Tell me the steps, like 'create a routine called morning "
            "that tells me the time then my battery'.",
            ok=False,
        )

    actions, failed = _compile_steps(body)

    if not actions:
        return SkillResult(
            speech=f"I couldn't work out any steps I know how to do from that."
            + (f" Stuck on: {failed[0]}." if failed else ""),
            ok=False,
        )

    name = (name or body)[:40].strip()
    if jobs.by_name(name):
        return SkillResult(
            speech=f"There's already a routine called '{name}'. "
            "Delete it first or pick another name.",
            ok=False,
        )

    job = jobs.create(name, "manual", {"human": "on request", "notify": False}, actions)

    speech = f"Routine '{name}' created with {len(actions)} steps."
    if failed:
        speech += f" I skipped {len(failed)}: {failed[0]}."

    return SkillResult(
        speech=speech,
        data={
            "name": name, "id": job.id,
            "steps": [a["skill"] for a in actions], "skipped": failed,
        },
    )


@skill(
    name="routine.run",
    tier="L1",
    action="modify",
    description="Run a named routine",
    examples=[
        "run my morning routine",
        "start the focus routine",
        "do my status routine",
        "execute the morning sequence",
        "run morning",
    ],
)
async def run(
    name: Annotated[str, "name of the routine"],
) -> SkillResult:
    cleaned = re.sub(
        r"\b(run|start|do|execute|play|trigger|my|the|routine|sequence|macro|workflow|now)\b",
        " ", name, flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    job = jobs.by_name(cleaned) or jobs.by_name(name)
    if job is None:
        from rapidfuzz import fuzz, process, utils

        candidates = {j.name: j for j in jobs.all_jobs()}
        if not candidates:
            return SkillResult(speech="You don't have any routines yet.", ok=False)
        match = process.extractOne(
            cleaned or name, candidates.keys(), scorer=fuzz.WRatio,
            processor=utils.default_process, score_cutoff=60,
        )
        if match is None:
            return SkillResult(speech=f"I don't have a routine called '{cleaned}'.", ok=False)
        job = candidates[match[0]]

    ok = await jobs.run_job(job.id, actor="text")
    history = jobs.history(job.id, 1)
    detail = history[0]["detail"] if history else ""

    # Speak the steps' own output rather than a bare "done".
    spoken = " ".join(
        part.split(": ", 1)[1] for part in detail.split(" | ") if ": " in part
    )

    return SkillResult(
        speech=spoken or f"Ran '{job.name}'.",
        ok=ok,
        data={"routine": job.name, "steps": len(job.actions), "detail": detail},
    )


@skill(
    name="routine.list",
    tier="L0",
    description="List your saved routines",
    examples=[
        "what routines do i have",
        "list my routines",
        "show me my sequences",
        "what macros are saved",
    ],
)
def list_routines() -> SkillResult:
    routines = [j for j in jobs.all_jobs() if j.trigger_type == "manual"]
    if not routines:
        return SkillResult(speech="You don't have any routines yet.")

    names = ", ".join(j.name for j in routines[:6])
    return SkillResult(
        speech=f"{len(routines)} routines: {names}.",
        data={
            "routines": [
                {"name": j.name, "steps": [a["skill"] for a in j.actions],
                 "runs": j.run_count}
                for j in routines
            ]
        },
    )
