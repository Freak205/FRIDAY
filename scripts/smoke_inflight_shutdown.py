"""Phase 47 -- quitting while an executor-backed skill is still running.

A blocking skill runs in a worker thread (`Skill.__call__` -> `asyncio.to_thread`). A thread cannot be interrupted, so what
matters is what the backend does around it. Before Phase 47, `Backend.shutdown` stopped the loop at once: the thread went
on to finish the skill's side effect, but the coroutine that records it (`audit.complete`, `audit.register_undo`, the
`skill.done` event) was abandoned -- a finished action with an open audit row and no undo entry.

Deterministic: the real `Backend`, the real `Executor.run` (so the Executor invariant holds) and the real audit on a
throwaway database; the "skill" is a fake that blocks on an Event. Waits are bounded and everything is released in `finally`.
No real tool, user data, microphone, speaker or GUI.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, store  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.gui import app as app_mod  # noqa: E402
from friday.gui import backend as backend_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

STARTED = threading.Event()
GATE = threading.Event()
FINISHED = threading.Event()
EFFECTS: list[str] = []


@skill(name="test.slow_write", tier="L0", description="Phase 47 test skill: blocks in a worker thread, then 'writes'.",
       undo=lambda: {"skill": "test.slow_write_undo", "args": {}})
def slow_write() -> SkillResult:
    STARTED.set()
    GATE.wait(15)
    EFFECTS.append("written")
    FINISHED.set()
    return SkillResult(speech="Written.")


async def _boot_nothing() -> None:
    return None


def make_backend() -> backend_mod.Backend:
    b = backend_mod.Backend()
    b._boot = _boot_nothing  # type: ignore[method-assign]
    b.start_async()
    return b


def last_audit_row() -> dict:
    rows = [r for r in audit.recent(20) if r["skill"] == "test.slow_write"]
    return rows[0] if rows else {}


def undo_rows() -> int:
    return int(store.conn().execute("SELECT COUNT(*) FROM undo WHERE skill='test.slow_write'").fetchone()[0])


def reset() -> None:
    STARTED.clear()
    GATE.clear()
    FINISHED.clear()
    EFFECTS.clear()


def main() -> int:
    t0 = time.perf_counter()
    events: list[tuple[str, float]] = []

    async def on_done(ev) -> None:
        events.append((ev.topic, time.monotonic()))

    BUS.subscribe("skill.done", on_done)
    BUS.subscribe(backend_mod.Backend.LATE_TOPIC, on_done)
    try:
        with iso_env.isolated_runtime():
            scenario("A. quit while a skill runs in a worker thread: its outcome is still recorded")
            reset()
            b = make_backend()
            cancels: list[bool] = []
            real_cancel = INTEL.request_cancel
            INTEL.request_cancel = lambda: cancels.append(True) or real_cancel()  # type: ignore[method-assign]
            try:
                fut = asyncio.run_coroutine_threadsafe(EXECUTOR.run("test.slow_write", {}, actor="text"), b.loop)
                started = STARTED.wait(5)
                check("A1 the skill is running in its worker thread", started and not fut.done())
                row = last_audit_row()
                check("A2 its audit row is open (started, no outcome yet)", row.get("ok") is None and row.get("id"))
                t1 = time.perf_counter()
                b.shutdown()
                check("A3 shutdown() returns at once while the worker is blocked", time.perf_counter() - t1 < 0.5)
                GATE.set()
                finished = FINISHED.wait(5)
                b._thread.join(8)
                row = last_audit_row()
                check("A5 the worker finished its side effect exactly once", finished and EFFECTS == ["written"])
                check("A6 the audit row was completed with the skill's own verdict (ok, not left open)",
                      row.get("ok") == 1 and row.get("result") == "Written.", str(row))
                check("A7 the undo entry for that action was registered", undo_rows() == 1, f"undo rows={undo_rows()}")
                check("A4 the existing STOP flag was raised, so a goal starts no further step", bool(cancels))
                check("A8 the backend reports the loop thread ended", b.stopped.is_set() and not b._thread.is_alive())
            finally:
                GATE.set()
                INTEL.request_cancel = real_cancel  # type: ignore[method-assign]

            scenario("B. a worker that does not finish in time: shutdown is bounded and does not pretend")
            reset()
            b2 = make_backend()
            b2.SHUTDOWN_DRAIN_S = 0.4  # type: ignore[misc]
            try:
                fut = asyncio.run_coroutine_threadsafe(EXECUTOR.run("test.slow_write", {}, actor="text"), b2.loop)
                STARTED.wait(5)
                b2.shutdown()
                ended = b2.stopped.wait(5)
                check("B1 the backend thread ends within the bound although the worker is still blocked", ended)
                check("B2 the worker has NOT finished (nothing was claimed or forced)", not FINISHED.is_set())
                check("B3 the audit row is honestly still open", last_audit_row().get("ok") is None)
            finally:
                GATE.set()
                FINISHED.wait(5)

            scenario("C. a request that outlived the wait is not announced after shutdown")
            reset()
            seen_late: list[dict] = []

            async def on_late(ev) -> None:
                seen_late.append(dict(ev.data))

            BUS.subscribe(backend_mod.Backend.LATE_TOPIC, on_late)
            real_session = backend_mod.SESSION
            gate_c = threading.Event()

            class SlowSession:
                async def handle(self, text, actor="text"):
                    await asyncio.to_thread(lambda: gate_c.wait(10))
                    return SkillResult(speech="Finished.")

            backend_mod.SESSION = SlowSession()  # type: ignore[assignment]
            b3 = make_backend()
            b3.ask_timeout_s = lambda: 0.3  # type: ignore[method-assign]
            try:
                answer = b3.ask("do the slow thing", actor="voice")
                check("C1 the caller was told it is still running", bool((answer.data or {}).get("still_running")))
                b3.shutdown()
                gate_c.set()
                b3._thread.join(8)
                time.sleep(0.2)
                check("C2 no late result is published once shutdown has begun", seen_late == [], str(seen_late))
            finally:
                gate_c.set()
                BUS.unsubscribe(backend_mod.Backend.LATE_TOPIC, on_late)
                backend_mod.SESSION = real_session

            scenario("D. after the window closes the process gives the backend a bounded chance to finish, then exits")

            class FakeBackend:
                def __init__(self, ends_after: float | None) -> None:
                    self.stopped = threading.Event()
                    if ends_after is not None:
                        threading.Timer(ends_after, self.stopped.set).start()

            t1 = time.perf_counter()
            r1 = app_mod._await_backend_stop(FakeBackend(0.1), timeout_s=3.0)
            check("D1 a backend that ends in time is reported as stopped, without waiting the whole bound",
                  r1 is True and time.perf_counter() - t1 < 2.0)
            t1 = time.perf_counter()
            r2 = app_mod._await_backend_stop(FakeBackend(None), timeout_s=0.3)
            check("D2 one that does not end is reported as not stopped, after about the bound",
                  r2 is False and 0.25 < time.perf_counter() - t1 < 2.0)
            check("D3 a backend with no `stopped` event cannot raise or hang",
                  app_mod._await_backend_stop(object(), timeout_s=0.1) is False)

        scenario("E. evidence: a worker thread keeps the process alive until it finishes (CPython joins executor threads)")
        with tempfile.TemporaryDirectory(prefix="friday-p47-") as d:
            marker = Path(d) / "worker-finished"
            script = Path(d) / "child.py"
            script.write_text(
                "import asyncio, sys, threading, time\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "loop = asyncio.new_event_loop()\n"
                "threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True).start()\n"
                "started = threading.Event()\n"
                "def work():\n"
                "    started.set(); time.sleep(1.5)\n"
                f"    open({str(marker)!r}, 'w').write('x')\n"
                "asyncio.run_coroutine_threadsafe(asyncio.to_thread(work), loop)\n"
                "started.wait(5)\n"
                "loop.call_soon_threadsafe(loop.stop)\n"
                "print('main done', flush=True)\n",
                encoding="utf-8",
            )
            t1 = time.perf_counter()
            proc = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=30)
            took = time.perf_counter() - t1
            check("E1 the process only ended after the blocked worker finished (its marker exists)",
                  proc.returncode == 0 and "main done" in proc.stdout and marker.exists(),
                  f"rc={proc.returncode} marker={marker.exists()} {proc.stderr[-200:]}")
            check("E2 so a worker that is slower than the app's own bound delays exit by the remainder", took > 1.0, f"{took:.1f}s")
    finally:
        GATE.set()
        BUS.unsubscribe("skill.done", on_done)
        BUS.unsubscribe(backend_mod.Backend.LATE_TOPIC, on_done)
        REGISTRY._skills.pop("test.slow_write", None)

    return finish("Phase 47 -- in-flight work at shutdown", time.perf_counter() - t0, min_assertions=18, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
