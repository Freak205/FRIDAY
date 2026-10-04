"""Phase 28.0 -- LIVE validation of the stop control (automated; NOT a human validation).

Two things the deterministic suite (smoke_stop_control.py) cannot show:

  1. REAL PLANNER. The real qwen2.5:3b plans a multi-step, read-only goal through the real
     plan.run / Executor / Session, and a stop is requested (through the same Session.stop the
     GUI button, hotkey, daemon and typed "stop" use) as soon as the first real step starts.
     Invariants checked on every conclusive run, whatever the model chose to do:
       - no skill STARTS after the stop request is made (BUS `skill.start` timestamps)
       - the goal ends `stopped="cancelled"`, is not ok, and its Goal row is CANCELLED
       - the user-facing result is the truthful stop summary, and INTEL is left clean
     A run where the model finished the goal before the stop could land is INCONCLUSIVE (it proves
     nothing about stopping) and is reported as such, never counted as a pass.
  2. REAL OS HOTKEY. The configured emergency hotkey is registered with the real RegisterHotKey,
     the real key chord is injected with SendInput, and the exact callback `friday/gui/app.py`
     binds must arm the stop signal for a running goal. Skipped (reported, not passed) if the
     hotkey cannot be registered, so a keystroke is never sent into whatever app is in front.

Safety: every real non-L0 skill is hard-denied (CFG.permissions.overrides) before anything runs,
so the model cannot cause a real side effect whatever it plans. Skips (exit 0) without Ollama.

Usage:  .venv/Scripts/python.exe -X utf8 scripts/smoke_stop_live.py [--reps 3]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from phase21_common import lock_down_real_tools  # noqa: E402

from friday import llm, store  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

GOAL = (
    "Check my battery level, then check the current volume, then tell me which window is "
    "active, then list the files in my Downloads folder, then tell me today's date."
)

RESULTS: list[tuple[str, str, str]] = []  # (verdict, label, detail)


def record(verdict: str, label: str, detail: str = "") -> None:
    RESULTS.append((verdict, label, detail))
    print(f"  {verdict:<12} {label}" + (f" -- {detail}" if detail else ""))


async def one_run(rep: int) -> None:
    print(f"\n--- real-planner run {rep} ---")
    CFG.desktop_observer.enabled = False
    started: list[tuple[float, str]] = []

    async def on_start(event):
        started.append((time.perf_counter(), str(event.data.get("skill"))))

    BUS.subscribe("skill.start", on_start)
    SESSION.pending = None
    try:
        task = asyncio.create_task(SESSION.handle(GOAL, actor="text"))
        deadline = time.perf_counter() + 90
        while not any(s != "plan.run" for _, s in started) and not task.done() and time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
        t_stop = time.perf_counter()
        stop_result = await SESSION.stop(source="live-test")
        result = await asyncio.wait_for(task, timeout=60)
        latency = time.perf_counter() - t_stop
    finally:
        BUS.unsubscribe("skill.start", on_start)

    steps = [s for _, s in started if s != "plan.run"]
    late = [s for t, s in started if t > t_stop + 0.05 and s != "plan.run"]
    stopped = (result.data or {}).get("stopped")
    print(f"     steps started: {steps}; stop->result {latency:.1f}s; stopped={stopped!r}")
    print(f"     result: {result.speech[:300]}")
    if not stop_result.data.get("goal_running"):
        record("INCONCLUSIVE", f"run {rep}: the goal had already ended when the stop landed", f"stopped={stopped!r}")
        return
    if stopped != "cancelled":
        record("INCONCLUSIVE", f"run {rep}: goal ended as {stopped!r} before it noticed the stop")
        return
    record("PASS" if not late else "FAIL", f"run {rep}: no skill started after the stop request", f"late={late}")
    record("PASS" if result.ok is False else "FAIL", f"run {rep}: a stopped goal is not reported ok")
    record("PASS" if result.speech.startswith("Stopped.") else "FAIL", f"run {rep}: truthful stop summary",
           result.speech[:120])
    gid = (result.data or {}).get("goal_id")
    row = goals_mod.get(gid) if gid else None
    record("PASS" if row is not None and row.status == goals_mod.GoalStatus.CANCELLED else "FAIL",
           f"run {rep}: Goal row is CANCELLED", str(getattr(row, "status", None)))
    record("PASS" if not INTEL.is_cancel_requested() and INTEL.state.goal_status == "cancelled" else "FAIL",
           f"run {rep}: INTEL left clean (signal cleared, status cancelled)")
    record("PASS" if latency < 20 else "FAIL", f"run {rep}: stop -> result latency {latency:.1f}s (< 20s)")


def hotkey_check() -> None:
    print("\n--- real OS hotkey ---")
    from friday import winput
    from friday.gui.backend import Backend
    from friday.hotkey import HotkeyManager

    combo = CFG.permissions.stop_hotkey
    backend = Backend()
    threading.Thread(target=lambda: (asyncio.set_event_loop(backend.loop), backend.loop.run_forever()), daemon=True).start()
    mgr = HotkeyManager()
    # exactly the callback friday/gui/app.py binds
    mgr.bind(combo, lambda: backend.stop(source="hotkey", wait=False))
    mgr.start()
    mgr.wait_ready(timeout=3.0)
    if not mgr.status().get(combo):
        record("SKIPPED", f"hotkey {combo} could not be registered (another app/FRIDAY instance owns it); "
                          "no keystroke was sent", str(mgr.status()))
        mgr.stop()
        return
    INTEL.reset()
    INTEL.start_goal("hotkey live goal", "gid-hotkey-live")
    winput.press(combo)
    deadline = time.perf_counter() + 3
    while not INTEL.is_cancel_requested() and time.perf_counter() < deadline:
        time.sleep(0.02)
    record("PASS" if INTEL.is_cancel_requested() else "FAIL",
           f"pressing {combo} (real SendInput -> RegisterHotKey) arms the stop signal for a running goal")
    INTEL.end_goal(ok=False, status="cancelled")
    INTEL.reset()
    winput.press(combo)
    time.sleep(0.5)
    record("PASS" if not INTEL.is_cancel_requested() else "FAIL",
           "the same keypress with NO goal running arms nothing")
    mgr.stop()
    backend.loop.call_soon_threadsafe(backend.loop.stop)


async def main(reps: int) -> int:
    if not await llm.ping():
        print("Ollama is not reachable -- SKIPPED (exit 0).")
        return 0
    store.init()
    REGISTRY.discover()
    saved = lock_down_real_tools(REGISTRY)
    try:
        for rep in range(1, reps + 1):
            await one_run(rep)
        await asyncio.to_thread(hotkey_check)
    finally:
        CFG.permissions.overrides = saved

    print("\n" + "=" * 72)
    tally: dict[str, int] = {}
    for verdict, _, _ in RESULTS:
        tally[verdict] = tally.get(verdict, 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    conclusive_runs = sum(1 for v, label, _ in RESULTS if "no skill started" in label and v in ("PASS", "FAIL"))
    print(f"  conclusive real-planner runs: {conclusive_runs}/{reps}")
    failed = tally.get("FAIL", 0)
    if failed:
        print("FAILURES ABOVE")
        return 1
    if conclusive_runs == 0:
        print("NO CONCLUSIVE REAL-PLANNER RUN -- nothing was validated by part 1")
        return 1
    print("ALL CONCLUSIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()
    code = asyncio.run(main(args.reps))
    sys.stdout.flush()
    import os

    os._exit(code)
