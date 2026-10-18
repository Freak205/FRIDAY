"""Phase 39 -- `Backend.ask` never reports a request it has not heard back from as a failure.

It used to wait a fixed 120 s, while a goal may run `planner.total_timeout_s` (300 s). A goal that was merely slow made
the caller raise ("Something went wrong: "), the GUI showed a failure, and the goal went on running and finished.
Now the wait covers the goal's own bound, and when it still runs out the caller is told the truth: still running, not
stopped, outcome unknown. The coroutine is never cancelled (that would cut a step in half).

No skill, database or GUI is involved: `SESSION.handle` is replaced by a fake coroutine on a real event-loop thread.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday.config import CFG  # noqa: E402
from friday.gui import backend as backend_mod  # noqa: E402
from friday.registry import SkillResult  # noqa: E402


def main() -> int:
    t0 = time.perf_counter()
    b = backend_mod.Backend()
    threading.Thread(target=b.loop.run_forever, daemon=True, name="smoke-backend-loop").start()
    finished: list[str] = []
    calls: list[str] = []

    class FakeSession:
        async def handle(self, text, *, actor="text"):
            calls.append(text)
            if text.startswith("slow"):
                await asyncio.sleep(0.8)
                finished.append(text)
                return SkillResult(speech="done the slow thing", ok=True)
            if text == "boom":
                raise RuntimeError("real failure")
            return SkillResult(speech=f"echo {text}", ok=True)

    real_session = backend_mod.SESSION
    backend_mod.SESSION = FakeSession()  # type: ignore[assignment]
    try:
        scenario("A: the wait covers the goal's own bound")
        saved = CFG.planner.total_timeout_s
        try:
            CFG.planner.total_timeout_s = 300.0
            check("A1 default wait is the planner bound plus a margin (330 s), not a fixed 120 s", b.ask_timeout_s() == 330.0, str(b.ask_timeout_s()))
            CFG.planner.total_timeout_s = 20.0
            check("A2 a short planner bound never shortens the wait below 120 s", b.ask_timeout_s() == 120.0, str(b.ask_timeout_s()))
        finally:
            CFG.planner.total_timeout_s = saved

        scenario("B: ordinary requests are unchanged")
        r = b.ask("hello")
        check("B1 a prompt reply is returned as is", r.ok and r.speech == "echo hello", r.speech)
        try:
            b.ask("boom")
            raised = False
        except RuntimeError:
            raised = True
        check("B2 a genuine error from the request still propagates (it is not swallowed into 'still running')", raised)

        scenario("C: a request that outlasts the wait is reported as unknown, not as failed or done")
        b.ask_timeout_s = lambda: 0.2  # type: ignore[method-assign]
        started = time.perf_counter()
        r = b.ask("slow one")
        waited = time.perf_counter() - started
        low = r.speech.lower()
        check("C1 it returned after the wait, not after the request", waited < 0.7, f"{waited:.2f}s")
        check("C2 the result says it is still running and marks it in data", r.data.get("still_running") is True and "still running" in low, r.speech)
        check("C3 it is not a success claim", not r.ok and "done" not in low.replace("haven't", "") and "worked." not in low.replace("whether it worked", ""), r.speech)
        check("C4 it is not a failure claim either ('went wrong' / 'failed' / 'error')", not any(w in low for w in ("went wrong", "failed", "error")), r.speech)
        check("C5 it tells the user how to end it", "stop" in low, r.speech)
        check("C6 the request was NOT cancelled: it was still running, and it finishes", not finished, str(finished))
        time.sleep(0.9)
        check("C7 ...and it did run to completion on the backend loop", finished == ["slow one"], str(finished))

        scenario("D: Phase 40 -- the same request is never submitted twice while it is still running; the wording is honest about STOP")
        from friday.intelligence.state import INTEL  # noqa: E402

        calls.clear()
        first = b.ask("slow two")
        check("D1 the first submission ran once and came back as still running", calls == ["slow two"] and first.data.get("still_running") and first.data.get("repeat") is False, f"{calls} {first.data}")
        t = time.perf_counter()
        again = b.ask("  Slow   TWO ")
        check("D2 the same words again (case / spacing aside) are NOT submitted a second time", calls == ["slow two"], f"{calls}")
        check("D3 ...and the caller is told so at once, marked as a repeat, still not a success or failure",
              time.perf_counter() - t < 0.15 and again.data.get("repeat") is True and not again.ok and "not started it again" in again.speech
              and not any(w in again.speech.lower() for w in ("went wrong", "failed", "error")), again.speech)
        other = b.ask("echo different words")
        check("D4 different words are not blocked", other.ok and calls[-1] == "echo different words", f"{calls} {other.speech!r}")
        time.sleep(0.9)  # 'slow two' finishes on the backend loop
        b.ask_timeout_s = lambda: 5.0  # type: ignore[method-assign]
        after = b.ask("slow two")
        check("D5 once it has finished, the same words run again normally", calls[-1] == "slow two" and after.ok and after.speech == "done the slow thing", f"{calls} {after.speech!r}")
        b.ask_timeout_s = lambda: 0.2  # type: ignore[method-assign]

        low = first.speech.lower()
        check("D6 it tells the user not to repeat it and does not claim success, failure, a stop or a cancellation",
              "don't repeat" in low and not any(w in low for w in ("worked.", "went wrong", "failed", "has been stopped", "cancelled", "i stopped")), first.speech)
        check("D7 not a goal: it says STOP cannot end it (it does not promise what STOP cannot do)", first.data.get("stoppable") is False and "cannot end" in low, first.speech)
        before_state = INTEL.state.goal_status
        INTEL.state.goal_status = "running"
        try:
            goal = b._still_running_result(repeat=False)
        finally:
            INTEL.state.goal_status = before_state
        glow = goal.speech.lower()
        check("D8 a running goal: STOP is offered, with its real semantics (no new step; one underway may finish; nothing reversed)",
              goal.data.get("stoppable") is True and "press stop" in glow and "no new step" in glow and "may finish" in glow and "nothing already carried out is reversed" in glow, goal.speech)
        check("D9 ...and still does not say it has been stopped", "has been stopped" not in glow and "cancelled" not in glow, goal.speech)
    finally:
        backend_mod.SESSION = real_session
        b.loop.call_soon_threadsafe(b.loop.stop)

    return finish("Phase 39 -- Backend.ask honesty", time.perf_counter() - t0, min_assertions=20, min_scenarios=4)


if __name__ == "__main__":
    sys.exit(main())
