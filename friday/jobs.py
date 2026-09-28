"""The automation engine: jobs, schedules, and event-driven runs.

A job is **trigger + actions**. Actions are a compiled list of skill calls, not a
stored prompt — so a scheduled run is deterministic, instant, offline, and
identical every time. Nothing is re-interpreted at fire time.

Unattended runs are capped by `permissions.unattended_ceiling`, so a job can
never quietly do something you'd normally be asked to confirm.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from friday import notify, store
from friday.bus import BUS
from friday.log import get

log = get(__name__)


@dataclass(slots=True)
class Job:
    id: int
    name: str
    trigger_type: str
    trigger_spec: dict[str, Any]
    actions: list[dict[str, Any]]
    enabled: bool
    last_run: str | None = None
    last_status: str | None = None
    run_count: int = 0
    fail_count: int = 0

    @staticmethod
    def from_row(row: Any) -> "Job":
        return Job(
            id=row["id"],
            name=row["name"],
            trigger_type=row["trigger_type"],
            trigger_spec=json.loads(row["trigger_spec"]),
            actions=json.loads(row["actions"]),
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
            last_status=row["last_status"],
            run_count=row["run_count"],
            fail_count=row["fail_count"],
        )

    def describe(self) -> str:
        spec = self.trigger_spec
        if self.trigger_type == "cron":
            when = spec.get("human") or spec.get("expression", "on a schedule")
        elif self.trigger_type == "interval":
            when = f"every {spec.get('minutes', '?')} minutes"
        elif self.trigger_type == "once":
            when = f"once at {spec.get('run_at', '?')}"
        else:
            when = f"when {spec.get('event', 'something happens')}"
        what = ", ".join(a["skill"] for a in self.actions)
        return f"{self.name}: {what} — {when}"


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def create(
    name: str,
    trigger_type: str,
    trigger_spec: dict[str, Any],
    actions: list[dict[str, Any]],
) -> Job:
    c = store.conn()
    cur = c.execute(
        "INSERT INTO jobs (name, created_at, trigger_type, trigger_spec, actions) "
        "VALUES (?,?,?,?,?)",
        (name, store.now(), trigger_type, store.dumps(trigger_spec), store.dumps(actions)),
    )
    c.commit()
    return get_job(int(cur.lastrowid))  # type: ignore[return-value]


def get_job(job_id: int) -> Job | None:
    row = store.conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def by_name(name: str) -> Job | None:
    row = store.conn().execute(
        "SELECT * FROM jobs WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return Job.from_row(row) if row else None


def all_jobs(enabled_only: bool = False) -> list[Job]:
    sql = "SELECT * FROM jobs" + (" WHERE enabled = 1" if enabled_only else "")
    return [Job.from_row(r) for r in store.conn().execute(sql + " ORDER BY id").fetchall()]


def set_enabled(job_id: int, enabled: bool) -> None:
    c = store.conn()
    c.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
    c.commit()


def delete(job_id: int) -> None:
    c = store.conn()
    c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    c.commit()


def _record_run(job: Job, ok: bool, detail: str, ms: int) -> None:
    c = store.conn()
    c.execute(
        "INSERT INTO job_runs (job_id, at, ok, detail, ms) VALUES (?,?,?,?,?)",
        (job.id, store.now(), 1 if ok else 0, detail[:500], ms),
    )
    c.execute(
        "UPDATE jobs SET last_run = ?, last_status = ?, run_count = run_count + 1, "
        "fail_count = fail_count + ? WHERE id = ?",
        (store.now(), "ok" if ok else "failed", 0 if ok else 1, job.id),
    )
    c.commit()


def history(job_id: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = store.conn().execute(
        "SELECT * FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


async def run_job(job_id: int, *, actor: str = "scheduler") -> bool:
    """Execute every action in a job. Returns True if all succeeded."""
    from friday.permissions import EXECUTOR, PermissionError_

    job = get_job(job_id)
    if job is None:
        log.warning("job %s vanished before it could run", job_id)
        return False
    if not job.enabled:
        return False

    log.info("running job '%s'", job.name)
    await BUS.publish("job.start", job=job.name, id=job.id)

    started = time.perf_counter()
    results: list[str] = []
    ok = True

    for action in job.actions:
        skill_name = action.get("skill")
        args = action.get("args", {})
        if not skill_name:
            continue
        try:
            result = await EXECUTOR.run(skill_name, args, actor=actor)
            results.append(f"{skill_name}: {result.speech}")
            if not result.ok:
                ok = False
        except PermissionError_ as exc:
            # The unattended ceiling did its job.
            results.append(f"{skill_name}: blocked — {exc}")
            ok = False
            break
        except Exception as exc:
            log.exception("job '%s' action %s failed", job.name, skill_name)
            results.append(f"{skill_name}: error — {exc}")
            ok = False
            break

    elapsed = int((time.perf_counter() - started) * 1000)
    detail = " | ".join(results)
    _record_run(job, ok, detail, elapsed)

    await BUS.publish(
        "job.done", job=job.name, id=job.id, ok=ok, detail=detail, ms=elapsed
    )

    # Unattended runs are invisible unless we say something. Announce failures
    # always; announce successes only when the job asked to be heard.
    if not ok:
        log.warning("job '%s' failed: %s", job.name, detail)
        notify.send(f"{job.name} failed", results[-1] if results else detail, urgent=True)
    elif actor != "text" and job.trigger_spec.get("notify", True):
        spoken = next(
            (r.split(": ", 1)[1] for r in results if ": " in r), job.name
        )
        notify.send(job.name, spoken)

    return ok


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------


class Scheduler:
    def __init__(self) -> None:
        self._sched: AsyncIOScheduler | None = None

    def start(self) -> None:
        if self._sched is not None:
            return
        # No timezone argument: APScheduler resolves the local zone via tzlocal.
        # Passing timezone="localtime" raises ZoneInfoNotFoundError — it is not
        # a valid IANA key, and under pythonw the traceback goes nowhere.
        self._sched = AsyncIOScheduler()
        self._sched.start()
        self.reload()
        log.info("scheduler started (%s)", self._sched.timezone)

    def stop(self) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=False)
            self._sched = None

    def _trigger_for(self, job: Job):
        spec = job.trigger_spec
        if job.trigger_type == "cron":
            if "expression" in spec:
                return CronTrigger.from_crontab(spec["expression"])
            return CronTrigger(
                hour=spec.get("hour", 0),
                minute=spec.get("minute", 0),
                day_of_week=spec.get("day_of_week"),
            )
        if job.trigger_type == "interval":
            return IntervalTrigger(
                minutes=spec.get("minutes", 0) or 0,
                seconds=spec.get("seconds", 0) or 0,
                hours=spec.get("hours", 0) or 0,
            )
        if job.trigger_type == "once":
            return DateTrigger(run_date=spec["run_at"])
        return None  # event-driven jobs aren't time-scheduled

    def reload(self) -> None:
        """Re-read jobs from the database and rebuild the schedule."""
        if self._sched is None:
            return
        self._sched.remove_all_jobs()

        count = 0
        for job in all_jobs(enabled_only=True):
            trigger = self._trigger_for(job)
            if trigger is None:
                continue  # event jobs are handled by friday.triggers
            try:
                self._sched.add_job(
                    run_job, trigger, args=[job.id],
                    id=f"job-{job.id}", name=job.name, replace_existing=True,
                    misfire_grace_time=300, coalesce=True,
                )
                count += 1
            except Exception:
                log.exception("could not schedule job '%s'", job.name)

        log.info("scheduler: %d timed jobs active", count)

    def next_runs(self) -> list[tuple[str, str]]:
        if self._sched is None:
            return []
        return [
            (j.name, j.next_run_time.strftime("%a %d %b %H:%M") if j.next_run_time else "—")
            for j in self._sched.get_jobs()
        ]


SCHEDULER = Scheduler()


async def fire_event(event_name: str, payload: dict[str, Any] | None = None) -> int:
    """Run every enabled job whose event trigger matches. Returns how many ran."""
    ran = 0
    for job in all_jobs(enabled_only=True):
        if job.trigger_type != "event":
            continue
        if job.trigger_spec.get("event") != event_name:
            continue
        asyncio.create_task(run_job(job.id, actor="trigger"))
        ran += 1
    if ran:
        log.info("event '%s' fired %d job(s)", event_name, ran)
    return ran
