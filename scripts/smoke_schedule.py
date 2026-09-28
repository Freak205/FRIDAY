"""Smoke test the automation engine: parsing, job creation, execution, triggers."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import jobs, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.schedparse import parse_when  # noqa: E402
from friday.session import SESSION  # noqa: E402

WHEN_CASES = [
    "every day at 8am",
    "every weekday at 9:30",
    "every morning",
    "every 30 minutes",
    "every 2 hours",
    "in 20 minutes",
    "at 5pm",
    "when the battery is low",
    "whenever i go idle",
    "when the wifi disconnects",
]

UTTERANCES = [
    "every day at 8am tell me my battery",
    "every 30 minutes check my system",
    "when the battery is low lock the pc",
    "in 10 minutes tell me the time",
]


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()

    print("\n--- time expression parsing ---\n")
    for text in WHEN_CASES:
        parsed = parse_when(text)
        if parsed:
            kind, spec = parsed
            spec = {k: v for k, v in spec.items() if k != "human"}
            print(f"  OK   {text:28} -> {kind:9} {spec}")
        else:
            print(f"  MISS {text:28} -> unparsed")

    print("\n--- creating jobs from natural language ---\n")
    for text in UTTERANCES:
        result = await SESSION.handle(text, actor="text")
        mark = "OK  " if result.ok else "MISS"
        print(f"  {mark} {text:40} -> {result.speech}")

    print("\n--- registered jobs ---\n")
    for job in jobs.all_jobs():
        print(f"  #{job.id} {job.describe()}")

    print("\n--- executing a job immediately ---\n")
    all_jobs = jobs.all_jobs()
    if all_jobs:
        target = all_jobs[0]
        ok = await jobs.run_job(target.id, actor="text")
        print(f"  ran '{target.name}': {'ok' if ok else 'failed'}")
        for run in jobs.history(target.id, 1):
            print(f"  -> {run['detail']}  ({run['ms']}ms)")

    print("\n--- unattended ceiling (L2 job must be blocked) ---\n")
    guard = jobs.create(
        "ceiling-test", "once", {"run_at": "2099-01-01T00:00:00"},
        [{"skill": "process.kill", "args": {"name": "definitely-not-real.exe"}}],
    )
    ok = await jobs.run_job(guard.id, actor="scheduler")
    detail = jobs.history(guard.id, 1)
    print(f"  scheduler running an L2 skill -> {'ALLOWED (BAD)' if ok else 'blocked (good)'}")
    if detail:
        print(f"  -> {detail[0]['detail'][:120]}")
    jobs.delete(guard.id)

    print("\n--- cleanup ---\n")
    for job in jobs.all_jobs():
        jobs.delete(job.id)
    print(f"  removed test jobs, {len(jobs.all_jobs())} remaining\n")


if __name__ == "__main__":
    asyncio.run(main())
