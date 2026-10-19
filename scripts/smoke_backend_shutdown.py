"""Phase 46 -- `Backend.shutdown` winds down what the backend itself started, and says when the loop thread has really ended.

The GUI backend starts the trigger watcher (an asyncio task) and the APScheduler on its loop. Only the headless daemon
stopped them; the GUI's quit just stopped the loop, which left the watcher task pending and the scheduler registered.

Deterministic: the real `Backend` (its boot replaced by a trivial coroutine), the real `WATCHER` (its polling replaced by
a no-op, so nothing on the machine is read) and the real `SCHEDULER` on a throwaway database. No GUI, mic or speaker.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import jobs, triggers  # noqa: E402
from friday.gui import backend as backend_mod  # noqa: E402


def make_backend(boot) -> backend_mod.Backend:
    b = backend_mod.Backend()
    b._boot = boot  # type: ignore[method-assign]
    return b


def main() -> int:
    t0 = time.perf_counter()
    saved_poll = triggers.TriggerWatcher._poll

    async def no_poll(self) -> None:  # the watcher runs, but reads nothing from the machine
        return None

    triggers.TriggerWatcher._poll = no_poll  # type: ignore[method-assign]
    try:
        with iso_env.isolated_runtime():
            scenario("A. shutdown stops the watcher and the scheduler, then the loop, and reports it")

            async def boot_a() -> None:
                jobs.SCHEDULER.start()
                triggers.WATCHER.start()

            b = make_backend(boot_a)
            b.start_async()
            time.sleep(0.4)
            watcher_task = triggers.WATCHER._task
            check("A1 before shutdown the watcher task and the scheduler are live",
                  watcher_task is not None and not watcher_task.done() and jobs.SCHEDULER._sched is not None)
            t1 = time.perf_counter()
            b.shutdown()
            took = time.perf_counter() - t1
            check("A2 shutdown() returns at once (it asks; it does not wait on the loop)", took < 0.5, f"{took:.2f}s")
            b._thread.join(5)
            check("A3 the backend thread really ended", not b._thread.is_alive())
            check("A4 the watcher task was cancelled and is finished", watcher_task.done() and watcher_task.cancelled(),
                  f"done={watcher_task.done()}")
            check("A5 the scheduler was shut down", jobs.SCHEDULER._sched is None)
            check("A6 `stopped` reports the loop thread's end", getattr(b, "stopped", None) is not None and b.stopped.is_set())
            check("A7 the loop is no longer running", not b.loop.is_running())

            scenario("B. a second shutdown is harmless")
            raised = None
            try:
                b.shutdown()
                b.shutdown()
            except Exception as exc:  # noqa: BLE001
                raised = exc
            check("B1 repeated shutdown() does not raise", raised is None, repr(raised))

            scenario("C. one failing stop does not stop the others from running")
            real_watcher, real_sched = triggers.WATCHER, jobs.SCHEDULER

            class BadWatcher:
                _task = None

                def stop(self) -> None:
                    raise RuntimeError("watcher stop failed")

            class RecScheduler:
                stopped = False

                def stop(self) -> None:
                    RecScheduler.stopped = True

            async def boot_c() -> None:
                return None

            triggers.WATCHER, jobs.SCHEDULER = BadWatcher(), RecScheduler()  # type: ignore[assignment]
            try:
                c = make_backend(boot_c)
                c.start_async()
                time.sleep(0.3)
                c.shutdown()
                c._thread.join(5)
            finally:
                triggers.WATCHER, jobs.SCHEDULER = real_watcher, real_sched
            check("C1 the scheduler is still stopped after the watcher's stop raised", RecScheduler.stopped)
            check("C2 the loop still stops", not c._thread.is_alive() and c.stopped.is_set())

            scenario("D. shutdown on a backend that never started")
            d = backend_mod.Backend()
            raised = None
            try:
                d.shutdown()
            except Exception as exc:  # noqa: BLE001
                raised = exc
            check("D1 shutdown() before start does not raise", raised is None, repr(raised))
            check("D2 and it does not claim the thread stopped", not d.stopped.is_set())

            scenario("E. a backend whose boot fails still reports that its thread ended")

            async def boot_e() -> None:
                raise RuntimeError("boot failed")

            e = make_backend(boot_e)
            e.start_async()
            e._thread.join(5)
            check("E1 after a failed boot the thread has ended and `stopped` says so",
                  not e._thread.is_alive() and e.stopped.is_set())
    finally:
        triggers.TriggerWatcher._poll = saved_poll  # type: ignore[method-assign]

    return finish("Phase 46 -- backend shutdown", time.perf_counter() - t0, min_assertions=13, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
