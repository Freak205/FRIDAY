"""Phase 48 -- work that outlives the shutdown drain is left as an explicit "outcome unknown" audit row.

Phase 47 lets the loop run up to `SHUTDOWN_DRAIN_S` after a quit so work in flight is recorded. A skill in a worker thread
that is still running after that cannot be interrupted, and its recording coroutine is abandoned: its audit row stayed
open (`ok` NULL, nothing else), indistinguishable from a row that was simply never filled in, and the side effect
happened anyway. The honest state is "started, outcome unknown" -- not failed (it may well succeed) and not succeeded.

Deterministic: the real `Backend`, the real `Executor.run` and the real audit on a throwaway database; the skill is a fake
that blocks on an Event. Waits are bounded; the worker is always released in `finally`. No real tool, user data, mic,
speaker or GUI.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, store  # noqa: E402
from friday.gui import backend as backend_mod  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

STARTED = threading.Event()
GATE = threading.Event()
FINISHED = threading.Event()
EFFECTS: list[str] = []
SKILL = "test.slow_write48"


@skill(name=SKILL, tier="L0", description="Phase 48 test skill: blocks in a worker thread, then 'writes'.",
       undo=lambda: {"skill": "test.slow_write48_undo", "args": {}})
def slow_write() -> SkillResult:
    STARTED.set()
    GATE.wait(15)
    EFFECTS.append("written")
    FINISHED.set()
    return SkillResult(speech="Written.")


@skill(name="test.boom48", tier="L0", description="Phase 48 test skill that raises.")
def boom() -> SkillResult:
    raise RuntimeError("it broke")


async def _boot_nothing() -> None:
    return None


def make_backend(drain: float) -> backend_mod.Backend:
    b = backend_mod.Backend()
    b._boot = _boot_nothing  # type: ignore[method-assign]
    b.SHUTDOWN_DRAIN_S = drain  # type: ignore[misc]
    b.start_async()
    return b


def row_of(name: str = SKILL) -> dict:
    rows = [r for r in audit.recent(30) if r["skill"] == name]
    return rows[0] if rows else {}


def undo_rows() -> int:
    return int(store.conn().execute("SELECT COUNT(*) FROM undo WHERE skill=?", (SKILL,)).fetchone()[0])


def reset() -> None:
    STARTED.clear()
    GATE.clear()
    FINISHED.clear()
    EFFECTS.clear()


def is_unknown(row: dict) -> bool:
    return "outcome unknown" in (row.get("error") or "")


def main() -> int:
    t0 = time.perf_counter()
    try:
        with iso_env.isolated_runtime():
            scenario("A. a worker that outlasts the drain: the row says so, and is neither failed nor succeeded")
            reset()
            b = make_backend(0.4)
            try:
                asyncio.run_coroutine_threadsafe(EXECUTOR.run(SKILL, {}, actor="text"), b.loop)
                STARTED.wait(5)
                before = row_of()
                check("A1 while it runs the row is open and unmarked", before.get("ok") is None and not before.get("error"))
                b.shutdown()
                check("A2 the backend ends within the bound although the worker is still blocked", b.stopped.wait(5))
                after = row_of()
                check("A3 the worker really is still running (no outcome could be known)", not FINISHED.is_set())
                check("A4 the row now carries an explicit 'outcome unknown' marker", is_unknown(after), str(after))
                check("A5 it is not recorded as failed or as succeeded (ok stays NULL, no result text)",
                      after.get("ok") is None and not after.get("result"), str(after))
                GATE.set()
                FINISHED.wait(5)
                time.sleep(0.2)
                end = row_of()
                check("A6 the side effect did happen after shutdown began, exactly once", EFFECTS == ["written"])
                check("A7 and the row still says 'unknown' (nothing recorded a result for it)", is_unknown(end) and end.get("ok") is None)
                check("A8 no undo entry was invented for an action whose result is not known", undo_rows() == 0, f"{undo_rows()}")
            finally:
                GATE.set()
                FINISHED.wait(5)

            scenario("B. the marker is idempotent and never overwrites or outlives a real outcome")
            rid = audit.record(actor="text", skill=SKILL, tier="L0", args={}, decision="auto")
            check("B1 marking an open row reports a change", audit.mark_outcome_unknown(rid) is True)
            check("B2 marking again changes nothing", audit.mark_outcome_unknown(rid) is False)
            check("B3 the first marker text is intact", is_unknown(row_of()))
            audit.complete(rid, ok=True, result="Written.", duration_ms=5)
            r = row_of()
            check("B4 a real completion that arrives afterwards replaces the marker with the definitive result",
                  r.get("ok") == 1 and r.get("result") == "Written." and not is_unknown(r), str(r))
            check("B5 marking a row that already has a result changes nothing", audit.mark_outcome_unknown(rid) is False and row_of().get("ok") == 1)
            fid = audit.record(actor="text", skill=SKILL, tier="L0", args={}, decision="auto")
            audit.complete(fid, ok=False, error="it failed", duration_ms=1)
            check("B6 a recorded failure is not overwritten", audit.mark_outcome_unknown(fid) is False and row_of().get("error") == "it failed")
            did = audit.record(actor="text", skill=SKILL, tier="L2", args={}, decision="denied")
            check("B7 a row that never ran (denied) is not marked", audit.mark_outcome_unknown(did) is False)

            scenario("C. the Executor knows what is in flight, and only that")
            reset()
            leftover = EXECUTOR.in_flight()  # scenario A's abandoned run is rightly still listed: it never finished
            check("C1 the abandoned run from A is still listed as unfinished; nothing else is", len(leftover) == 1, str(leftover))
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
            t.start()
            try:
                fut = asyncio.run_coroutine_threadsafe(EXECUTOR.run(SKILL, {}, actor="text"), loop)
                STARTED.wait(5)
                during = {k: v for k, v in EXECUTOR.in_flight().items() if k not in leftover}
                check("C2 a running skill is listed under its audit id", list(during.values()) == [SKILL] and len(during) == 1, str(during))
                GATE.set()
                fut.result(8)
                check("C3 it is gone once the skill has finished and been recorded", EXECUTOR.in_flight() == leftover)
                try:
                    asyncio.run_coroutine_threadsafe(EXECUTOR.run("test.boom48", {}, actor="text"), loop).result(8)
                except Exception:  # noqa: BLE001
                    pass
                check("C4 and gone after a skill that raised", EXECUTOR.in_flight() == leftover)
            finally:
                GATE.set()
                loop.call_soon_threadsafe(loop.stop)
    finally:
        GATE.set()
        REGISTRY._skills.pop(SKILL, None)
        REGISTRY._skills.pop("test.boom48", None)

    return finish("Phase 48 -- audit rows for work that outlives shutdown", time.perf_counter() - t0, min_assertions=19, min_scenarios=3)


if __name__ == "__main__":
    sys.exit(main())
