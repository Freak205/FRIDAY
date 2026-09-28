"""Scheduling skills — create and manage automated jobs by voice.

`schedule.create` is the recipe compiler from the design: the command you want
automated is resolved through the brain **once**, at creation time, and stored as
a concrete list of skill calls. Every subsequent run replays that list directly.
No re-interpretation, no drift, no network.
"""

from __future__ import annotations

from typing import Annotated

from friday import jobs
from friday.log import get
from friday.registry import SkillResult, skill
from friday.schedparse import parse_when

log = get(__name__)


def _compile(command: str) -> tuple[list[dict], str]:
    """Resolve a natural-language command into concrete skill calls."""
    from friday.brain import BRAIN
    from friday.brain.engine import Action

    understanding = BRAIN.understand(command)

    if understanding.action is not Action.ACT or not understanding.skill:
        return [], (
            f"I don't know how to '{command}' yet, so I can't schedule it."
            if understanding.action is Action.UNKNOWN
            else f"'{command}' is ambiguous — be more specific and I'll schedule it."
        )

    return [{"skill": understanding.skill, "args": understanding.args}], ""


@skill(
    name="schedule.create",
    tier="L1",
    action="modify",
    description="Schedule a command to run automatically at a time or on an event",
    examples=[
        "every day at 8am tell me my battery",
        "remind me in 20 minutes",
        "in 10 minutes tell me the time",
        "in 5 minutes check my battery",
        "in half an hour lock the pc",
        "run this every 30 minutes",
        "every 15 minutes check my system",
        "schedule a task",
        "set up an automation",
        "every morning check my system",
        "every weekday at 9 open my browser",
        "when the battery is low lock the pc",
        "whenever the wifi drops tell me",
        "automate this daily",
        "do this later",
        "set a reminder",
    ],
)
def create(
    command: Annotated[str, "the command to run, e.g. 'check my battery'"],
    when: Annotated[str, "when to run it, e.g. 'every day at 8am'"],
    name: Annotated[str, "optional name for the job"] = "",
) -> SkillResult:
    trigger = parse_when(when)
    if trigger is None:
        return SkillResult(
            speech=f"I couldn't work out a schedule from '{when}'. "
            "Try something like 'every day at 8am' or 'in 20 minutes'.",
            ok=False,
        )

    trigger_type, spec = trigger
    actions, problem = _compile(command)
    if problem:
        return SkillResult(speech=problem, ok=False)

    job_name = (name or command).strip()[:60]
    if jobs.by_name(job_name):
        job_name = f"{job_name} ({jobs.store.now()[11:19]})"

    try:
        job = jobs.create(job_name, trigger_type, spec, actions)
    except Exception as exc:
        return SkillResult(speech=f"I couldn't save that job: {exc}", ok=False)

    jobs.SCHEDULER.reload()

    return SkillResult(
        speech=f"Scheduled. I'll {command} {spec.get('human', when)}.",
        data={
            "id": job.id,
            "name": job.name,
            "trigger": trigger_type,
            "spec": spec,
            "actions": actions,
        },
    )


@skill(
    name="schedule.list",
    tier="L0",
    description="List scheduled and automated jobs",
    examples=[
        "what's scheduled",
        "list my automations",
        "what jobs do you have",
        "show me my scheduled tasks",
        "what have you got planned",
        "what reminders are set",
    ],
)
def list_jobs() -> SkillResult:
    all_jobs = jobs.all_jobs()
    if not all_jobs:
        return SkillResult(speech="You have no scheduled jobs.")

    active = [j for j in all_jobs if j.enabled]
    lines = [j.describe() for j in all_jobs]

    upcoming = jobs.SCHEDULER.next_runs()
    next_line = ""
    if upcoming:
        name, when = upcoming[0]
        next_line = f" Next up: {name} at {when}."

    return SkillResult(
        speech=f"{len(active)} active job{'s' if len(active) != 1 else ''}"
        f" of {len(all_jobs)}.{next_line}",
        data={
            "jobs": [
                {
                    "id": j.id, "name": j.name, "enabled": j.enabled,
                    "trigger": j.trigger_type, "description": j.describe(),
                    "runs": j.run_count, "failures": j.fail_count,
                    "last_run": j.last_run, "last_status": j.last_status,
                }
                for j in all_jobs
            ],
            "next_runs": upcoming,
            "summary": lines,
        },
    )


@skill(
    name="schedule.delete",
    tier="L2",
    action="delete",
    description="Delete a scheduled job",
    examples=[
        "delete that job",
        "cancel the morning reminder",
        "remove that automation",
        "stop that scheduled task",
        # The "<verb> the <command phrase> job" frame. Without these, an
        # utterance like "delete the lock the pc job" matches the *inner*
        # command (system.lock) and performs it instead of deleting the job.
        "delete the lock the pc job",
        "delete the tell me my battery job",
        "delete the check my system job",
        "remove the open chrome job",
        "cancel the turn down the volume job",
        "get rid of the daily battery job",
        "delete the job called morning briefing",
    ],
    dry_run=lambda name: f"Permanently delete the job '{name}'",
)
def delete_job(
    name: Annotated[str, "name of the job to delete"],
) -> SkillResult:
    job = jobs.by_name(name)
    if job is None:
        from rapidfuzz import fuzz, process, utils

        candidates = {j.name: j for j in jobs.all_jobs()}
        match = process.extractOne(
            name, candidates.keys(), scorer=fuzz.WRatio,
            processor=utils.default_process, score_cutoff=60,
        )
        if match is None:
            return SkillResult(speech=f"I have no job called '{name}'.", ok=False)
        job = candidates[match[0]]

    jobs.delete(job.id)
    jobs.SCHEDULER.reload()
    return SkillResult(speech=f"Deleted '{job.name}'.", data={"deleted": job.name})


@skill(
    name="schedule.toggle",
    tier="L1",
    action="modify",
    description="Pause or resume a scheduled job",
    examples=[
        "pause that job",
        "disable the morning reminder",
        "turn that automation back on",
        "resume that scheduled task",
        "pause the lock the pc job",
        "disable the check my system job",
        "resume the tell me my battery job",
        "turn the open chrome job back on",
    ],
)
def toggle_job(
    name: Annotated[str, "name of the job"],
    enable: Annotated[bool, "True to resume, False to pause"] = False,
) -> SkillResult:
    job = jobs.by_name(name)
    if job is None:
        return SkillResult(speech=f"I have no job called '{name}'.", ok=False)

    jobs.set_enabled(job.id, enable)
    jobs.SCHEDULER.reload()
    return SkillResult(
        speech=f"{'Resumed' if enable else 'Paused'} '{job.name}'.",
        data={"name": job.name, "enabled": enable},
    )


@skill(
    name="schedule.run_now",
    tier="L1",
    action="modify",
    description="Run a scheduled job immediately",
    examples=[
        "run that job now",
        "trigger the morning routine",
        "execute that task now",
        "run the lock the pc job now",
        "trigger the check my system job",
        "run the tell me my battery job right now",
    ],
)
async def run_now(
    name: Annotated[str, "name of the job to run"],
) -> SkillResult:
    job = jobs.by_name(name)
    if job is None:
        return SkillResult(speech=f"I have no job called '{name}'.", ok=False)

    # actor="text" so a manual run isn't held to the unattended ceiling.
    ok = await jobs.run_job(job.id, actor="text")
    return SkillResult(
        speech=f"Ran '{job.name}'." if ok else f"'{job.name}' ran but reported a problem.",
        ok=ok,
    )


@skill(
    name="schedule.history",
    tier="L0",
    description="Show the run history of a scheduled job",
    examples=[
        "how did that job go",
        "show me the job history",
        "did that task run",
        "how did the check my system job go",
        "did the lock the pc job run",
        "show me the history of the battery job",
    ],
)
def job_history(
    name: Annotated[str, "name of the job"],
    limit: Annotated[int, "how many runs to show"] = 5,
) -> SkillResult:
    job = jobs.by_name(name)
    if job is None:
        return SkillResult(speech=f"I have no job called '{name}'.", ok=False)

    runs = jobs.history(job.id, limit)
    if not runs:
        return SkillResult(speech=f"'{job.name}' hasn't run yet.")

    failures = sum(1 for r in runs if not r["ok"])
    return SkillResult(
        speech=f"'{job.name}' ran {job.run_count} times, {job.fail_count} failed. "
        f"Last {len(runs)}: {len(runs) - failures} ok.",
        data={"job": job.name, "runs": runs},
    )
