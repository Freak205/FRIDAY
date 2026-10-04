"""Phase 28.0 -- one user-reachable stop for a running goal.

Before this phase a running multi-step goal could only be cancelled through the daemon's
`POST /cancel`; the GUI CANCEL button and Esc only aborted a voice cycle, and the stop signal
(`INTEL.cancel_requested`) was only looked at between steps. This suite proves, deterministically
(no Ollama, no real side effect: every real non-L0 skill is hard-denied and every step is a
`test.*` fixture or a fake runner):

  A. the signal (`INTEL`): armed only while a goal runs, idempotent, cleared when the goal ends
  B. no step STARTS after a stop (loop top, the `_run_step` gate, `run_plan`), and the result is
     truthful about what ran; normal runs (cancel_check False / absent) are unchanged
  C. a step in flight that is a coroutine IS interrupted (cancellation reaches the tool), and the
     Executor closes its audit row honestly
  D. a step in flight that runs in a worker thread is NOT interrupted -- it finishes, and the
     result says exactly that instead of claiming it was stopped
  E. `Session.stop`: idle no-op, idempotent, declines a waiting confirmation, the typed/spoken
     phrase routing (and that it never swallows ordinary "stop the music"-style commands)
  F. end to end through the real `plan.run` + Executor + Session confirmation: a stop while an L2
     confirmation is waiting declines it (the action never runs); without a stop the
     confirmation/decline behaviour is exactly what it was
  G. the daemon's `/cancel` goes through the same path
  H. the GUI wiring: the STOP control follows INTEL's goal state and calls Backend.stop; the
     emergency hotkey is a valid, non-clashing global hotkey
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from phase21_common import (  # noqa: E402
    ScriptedPlanner, call, check, done, finish, lock_down_real_tools, scenario, scripted_provider,
)

from friday import audit, store  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION, Pending, is_stop_phrase  # noqa: E402

RAN: dict[str, int] = {}
FLAGS: dict[str, bool] = {}


def _register_fixtures() -> None:
    if REGISTRY.get("test.stop_ok") is None:
        @skill(name="test.stop_ok", tier="L0", action="read", description="test: instant ok",
               examples=["do the stop control test step"])
        async def _ok(label: Annotated[str, "which"] = "") -> SkillResult:
            RAN["ok"] = RAN.get("ok", 0) + 1
            return SkillResult(speech=f"did {label}", data={"label": label})

        @skill(name="test.stop_slow", tier="L0", action="read", description="test: an async tool that takes 5s",
               examples=["do the slow stop control test step"])
        async def _slow() -> SkillResult:
            RAN["slow"] = RAN.get("slow", 0) + 1
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                FLAGS["slow_saw_cancel"] = True  # cancellation really reached the tool
                raise
            return SkillResult(speech="slow finished")

        @skill(name="test.stop_blocking", tier="L0", action="read",
               description="test: a plain function (runs in a worker thread) that takes 0.8s",
               examples=["do the blocking stop control test step"])
        def _blocking() -> SkillResult:
            RAN["blocking"] = RAN.get("blocking", 0) + 1
            time.sleep(0.8)
            FLAGS["blocking_finished"] = True
            return SkillResult(speech="blocking finished")

        @skill(name="test.stop_l2", tier="L2", action="read", description="test: needs confirmation",
               examples=["do the confirmed stop control test step"],
               dry_run=lambda: "Run the L2 stop-control test step")
        async def _l2() -> SkillResult:
            RAN["l2"] = RAN.get("l2", 0) + 1
            return SkillResult(speech="l2 ran")


def _orch(tools: list[str], planner: ScriptedPlanner, **kw) -> Orchestrator:
    return Orchestrator(tools=tools, actor="text", llm_provider=planner, max_steps=kw.pop("max_steps", 8), **kw)


def _fake_orch(replies: list[str], runner, tools: list[str]) -> Orchestrator:
    return Orchestrator(
        tools=tools, runner=runner, actor="test", llm_provider=ScriptedPlanner(replies),
        tool_specs=[ToolSpec(name=t, description=t) for t in tools], max_steps=8,
    )


def _begin_goal() -> None:
    INTEL.reset()
    INTEL.start_goal("stop control test", "gid-stop")


# ---------------------------------------------------------------------------------------------
async def scenario_a_signal() -> None:
    scenario("A. the stop signal: armed only while a goal runs, idempotent, always cleared")
    INTEL.reset()
    check("A: idle -> request_cancel reports nothing to stop", INTEL.request_cancel() is False)
    check("A: idle -> NOTHING is armed (no stale request can cancel a later goal)",
          INTEL.is_cancel_requested() is False)
    INTEL.start_goal("g1", "id1")
    check("A: a running goal is signalled", INTEL.request_cancel() is True and INTEL.is_cancel_requested())
    check("A: repeated requests are idempotent", INTEL.request_cancel() is True and INTEL.is_cancel_requested())
    INTEL.end_goal(ok=False, status="cancelled")
    check("A: the request is cleared when the goal ends", INTEL.is_cancel_requested() is False)
    INTEL.start_goal("g2", "id2")
    check("A: a finished goal's request never cancels the next goal", INTEL.is_cancel_requested() is False)
    INTEL.end_goal(ok=True)
    INTEL.request_cancel()
    INTEL.start_goal("g3", "id3")
    check("A: a request made between goals does not leak into the next one", INTEL.is_cancel_requested() is False)
    INTEL.end_goal(ok=True)


async def scenario_b_no_new_steps() -> None:
    scenario("B. no step starts after a stop; the result says what actually ran; normal runs unchanged")
    tools = [f"test.s{i}" for i in range(5)]
    replies = [call(t) for t in tools] + [done("all done")]

    # normal execution is unaffected: a cancel_check that never fires == no cancel_check
    async def runner_ok(tool, args, actor):
        return SkillResult(speech=f"{tool} ok")

    base = await _fake_orch(list(replies), runner_ok, tools).run_goal("normal run")
    with_check = await _fake_orch(list(replies), runner_ok, tools).run_goal("normal run", cancel_check=lambda: False)
    check("B: a never-firing cancel_check completes exactly like no cancel_check",
          base.stopped == with_check.stopped == "completed" and base.ok and with_check.ok
          and [o.step.tool for o in base.observations] == [o.step.tool for o in with_check.observations] == tools)

    # a stop landing during step 2: step 3+ never run, and the summary is truthful
    calls: list[str] = []
    state = {"stop": False}

    async def runner_stop_during_2(tool, args, actor):
        calls.append(tool)
        if len(calls) == 2:
            state["stop"] = True
        return SkillResult(speech=f"{tool} ok")

    r = await _fake_orch(list(replies), runner_stop_during_2, tools).run_goal("stop mid", cancel_check=lambda: state["stop"])
    check("B: stop during step 2 -> goal stops, stopped='cancelled', not ok",
          r.stopped == "cancelled" and r.ok is False)
    check("B: the planned steps after it never started", calls == ["test.s0", "test.s1"], str(calls))
    check("B: both executed steps are in the evidence", [o.step.tool for o in r.observations] == calls)
    check("B: the summary says what ran and that nothing was undone",
          "Stopped." in r.summary and "2 steps had already run" in r.summary and "nothing was undone" in r.summary,
          r.summary)
    check("B: it does not claim a cancelled step was interrupted when none was", "interrupted" not in r.summary)

    # a stop before anything runs
    calls.clear()
    r0 = await _fake_orch(list(replies), runner_stop_during_2, tools).run_goal("stop first", cancel_check=lambda: True)
    check("B: stop before step 1 -> nothing ran", r0.stopped == "cancelled" and not calls and not r0.observations)
    check("B: ...and the summary says so", r0.summary == "Stopped. Nothing had run yet.", r0.summary)

    # the _run_step gate itself: a decision that already reached execution is not started
    calls.clear()
    orch = _fake_orch([done("x")], runner_stop_during_2, ["test.s0"])
    obs, stop = await orch._run_step(PlanStep(tool="test.s0", args={}), cancel_check=lambda: True)
    check("B: _run_step never starts a step once a stop is requested",
          stop == "cancelled" and obs.error == "cancelled_before_start" and not calls)
    check("B: ...and run_goal does not record it as a step",
          all(o.error != "cancelled_before_start" for o in r0.observations) and not r0.observations)

    # a stop must not be turned into a replan/retry by a failing step
    state["stop"] = False
    calls.clear()

    async def runner_fail_and_stop(tool, args, actor):
        calls.append(tool)
        state["stop"] = True
        return SkillResult(speech="it failed", ok=False)

    rf = await _fake_orch([call("test.s0"), call("test.s0", {"x": 1}), done("x")], runner_fail_and_stop,
                          ["test.s0"]).run_goal("fail then stop", max_replans=3, cancel_check=lambda: state["stop"])
    check("B: a step that fails while a stop is pending is NOT replanned around",
          rf.stopped == "cancelled" and len(calls) == 1)

    # run_plan (explicit plan) honours it too
    calls.clear()
    state["stop"] = False
    plan_orch = _fake_orch([done("x")], runner_stop_during_2, tools)
    rp = await plan_orch.run_plan("explicit", [PlanStep(tool=t, args={}) for t in tools], cancel_check=lambda: state["stop"])
    check("B: run_plan stops after the step that saw the stop and runs no more",
          rp.stopped == "cancelled" and calls == ["test.s0", "test.s1"], str(calls))
    plain = await _fake_orch([done("x")], runner_ok, tools).run_plan("explicit", [PlanStep(tool=t, args={}) for t in tools])
    check("B: run_plan without cancel_check is unchanged", plain.stopped == "completed" and plain.ok)

    # a repeated stop request changes nothing
    state["stop"] = True
    calls.clear()
    r2 = await _fake_orch(list(replies), runner_ok, tools).run_goal("again", cancel_check=lambda: state["stop"])
    r3 = await _fake_orch(list(replies), runner_ok, tools).run_goal("again", cancel_check=lambda: state["stop"])
    check("B: a repeated/standing stop is stable", r2.stopped == r3.stopped == "cancelled" and r2.summary == r3.summary)


async def scenario_c_interrupt_async() -> None:
    scenario("C. a coroutine tool in flight is interrupted; the Executor audit is closed truthfully")
    _begin_goal()
    RAN.clear()
    FLAGS.clear()
    events: list[tuple[str, dict]] = []

    async def spy(event):
        events.append((event.topic, dict(event.data)))

    BUS.subscribe("orchestrator.cancelling", spy)
    BUS.subscribe("orchestrator.done", spy)
    planner = ScriptedPlanner([call("test.stop_ok", {"label": "first"}), call("test.stop_slow"),
                               call("test.stop_ok", {"label": "never"}), done("x")])
    orch = _orch(["test.stop_ok", "test.stop_slow"], planner, step_timeout_s=30)

    async def stop_soon():
        while RAN.get("slow", 0) == 0:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        INTEL.request_cancel()

    with store.use_temp_db():
        t0 = time.perf_counter()
        stopper = asyncio.create_task(stop_soon())
        r = await orch.run_goal("interrupt the slow step", cancel_check=INTEL.is_cancel_requested)
        await stopper
        elapsed = time.perf_counter() - t0
        await asyncio.sleep(0.05)
        rows = [dict(row) for row in store.conn().execute("SELECT * FROM audit WHERE skill='test.stop_slow'")]

    check("C: the goal stopped promptly instead of waiting out the 5s tool", elapsed < 2.5, f"{elapsed:.2f}s")
    check("C: cancellation really reached the running tool", FLAGS.get("slow_saw_cancel") is True)
    check("C: stopped='cancelled' and the later planned step never ran", r.stopped == "cancelled" and RAN.get("ok") == 1)
    cut = [o for o in r.observations if o.error == "interrupted"]
    check("C: the cut-off step is recorded as interrupted, not as a success or a plain failure",
          len(cut) == 1 and cut[0].ok is False and cut[0].step.tool == "test.stop_slow")
    check("C: the summary is honest that it can't confirm the interrupted step finished",
          "interrupted while running" in r.summary and "can't confirm" in r.summary, r.summary)
    check("C: the summary reports the earlier step that did complete", "1 step had already run" in r.summary, r.summary)
    check("C: its audit row is closed as interrupted (not left open, not 'ok')",
          len(rows) == 1 and rows[0]["ok"] == 0 and "interrupted" in (rows[0]["error"] or ""), str(rows))
    last = list(INTEL.state.action_log)[-1] if INTEL.state.action_log else {}
    check("C: the action log records it as cancelled", last.get("status") == "cancelled", str(last))
    topics = [t for t, _ in events]
    check("C: a cancelling event announced it as interruptible",
          any(t == "orchestrator.cancelling" and d.get("interruptible") is True for t, d in events))
    check("C: the done event says cancelled with the true executed count",
          any(t == "orchestrator.done" and d.get("stopped") == "cancelled" and d.get("executed") == 2 for t, d in events),
          str(topics))
    BUS.unsubscribe("orchestrator.cancelling", spy)
    BUS.unsubscribe("orchestrator.done", spy)
    INTEL.end_goal(ok=False, status="cancelled")


async def scenario_d_blocking() -> None:
    scenario("D. a worker-thread tool in flight is NOT interrupted -- and the result says so")
    _begin_goal()
    RAN.clear()
    FLAGS.clear()
    events: list[tuple[str, dict]] = []

    async def spy(event):
        events.append((event.topic, dict(event.data)))

    BUS.subscribe("orchestrator.cancelling", spy)
    planner = ScriptedPlanner([call("test.stop_blocking"), call("test.stop_ok", {"label": "never"}), done("x")])
    orch = _orch(["test.stop_blocking", "test.stop_ok"], planner, step_timeout_s=30)

    async def stop_soon():
        while RAN.get("blocking", 0) == 0:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        INTEL.request_cancel()

    with store.use_temp_db():
        stopper = asyncio.create_task(stop_soon())
        t0 = time.perf_counter()
        r = await orch.run_goal("stop during a blocking tool", cancel_check=INTEL.is_cancel_requested)
        await stopper
        elapsed = time.perf_counter() - t0
        rows = [dict(row) for row in store.conn().execute("SELECT * FROM audit WHERE skill='test.stop_blocking'")]

    check("D: the thread tool was left to finish (it cannot be interrupted)", FLAGS.get("blocking_finished") is True)
    check("D: waiting for it is bounded by its own run time, not extended", 0.7 < elapsed < 3.0, f"{elapsed:.2f}s")
    check("D: no further step started after it", r.stopped == "cancelled" and RAN.get("ok", 0) == 0)
    check("D: its real outcome is recorded (ok, not 'interrupted')",
          len(r.observations) == 1 and r.observations[0].ok is True and not r.observations[0].error)
    check("D: the summary says it could not be interrupted and ran to completion",
          "can't be interrupted" in r.summary and "ran to completion" in r.summary, r.summary)
    check("D: the summary does not claim it was interrupted", "interrupted while running" not in r.summary, r.summary)
    check("D: a cancelling event announced it as NOT interruptible",
          any(t == "orchestrator.cancelling" and d.get("interruptible") is False for t, d in events))
    check("D: its audit row is the normal completed one", len(rows) == 1 and rows[0]["ok"] == 1, str(rows))
    BUS.unsubscribe("orchestrator.cancelling", spy)
    INTEL.end_goal(ok=False, status="cancelled")


async def scenario_e_session_stop() -> None:
    scenario("E. Session.stop: idle no-op, idempotent, declines a waiting confirmation, phrase routing")
    INTEL.reset()
    SESSION.pending = None
    with store.use_temp_db():
        idle = await SESSION.stop(source="test")
        check("E: idle -> says there is nothing to stop", "nothing running" in idle.speech and idle.ok)
        check("E: idle -> reports stopped=False and arms nothing",
              idle.data["stopped"] is False and not INTEL.is_cancel_requested())
        idle2 = await SESSION.stop(source="test")
        check("E: a repeated idle stop is the same harmless no-op", idle2.speech == idle.speech)

        INTEL.start_goal("a running goal", "gid-e")
        s1 = await SESSION.stop(source="gui")
        check("E: running -> stopping, flag armed",
              s1.data["stopped"] is True and s1.data["goal_running"] is True and INTEL.is_cancel_requested())
        check("E: its speech does not overclaim (no 'stopped', no 'undone')",
              s1.speech.startswith("Stopping.") and "undone" not in s1.speech.lower(), s1.speech)
        s2 = await SESSION.stop(source="hotkey")
        check("E: stopping twice is safe and identical", s2.data == s1.data and s2.speech == s1.speech)
        rows = [dict(row) for row in store.conn().execute("SELECT * FROM audit WHERE skill='friday.stop'")]
        check("E: every stop request is audited with its source",
              [r["actor"] for r in rows] == ["test", "test", "gui", "hotkey"], str([r["actor"] for r in rows]))

        # a waiting confirmation is declined, not left to be revived by a late 'yes'
        fut = asyncio.get_running_loop().create_future()
        SESSION.pending = Pending(kind="confirm", skill="test.stop_l2", args={}, future=fut, actor="text")
        s3 = await SESSION.stop(source="gui")
        check("E: a waiting confirmation is declined by the stop", fut.done() and fut.result() is False)
        check("E: ...and cleared, and the speech says so",
              SESSION.pending is None and s3.data["declined_confirmation"] is True and "declined" in s3.speech)

        # phrase routing
        before = INTEL.state.current_request
        r = await SESSION.handle("Stop", actor="text")
        check("E: typed 'stop' during a goal is the stop (not routed to a skill)",
              r.data.get("stopped") is True and r.speech.startswith("Stopping."), r.speech)
        check("E: ...and does not overwrite the running goal's request", INTEL.state.current_request == before)
        INTEL.end_goal(ok=False, status="cancelled")
        check("E: with nothing running a bare 'stop' is NOT intercepted (routes as it always did)",
              SESSION._stop_applies() is False)

    yes = ["stop", "Stop!", "cancel", "cancel that", "Hey FRIDAY, stop", "stop what you're doing", "stop everything",
           "abort", "emergency stop", "please stop", "stop it now", "stop the task"]
    no = ["stop the music", "cancel my subscription", "stop the timer", "stopwatch", "pause", "never mind the stop",
          "don't stop", "cancel the meeting tomorrow", "stop recording", "what does stop mean", ""]
    check("E: the stop phrases are recognised", all(is_stop_phrase(p) for p in yes), str([p for p in yes if not is_stop_phrase(p)]))
    check("E: ordinary commands containing stop/cancel are NOT stop phrases",
          not any(is_stop_phrase(p) for p in no), str([p for p in no if is_stop_phrase(p)]))


async def scenario_f_end_to_end() -> None:
    scenario("F. end to end: real plan.run + Executor + Session confirmation -- stop declines, risk unchanged")
    CFG.desktop_observer.enabled = False
    goal = "run the stop control test steps"

    async def run_goal_with_answer(answer: str | None, *, stop: bool = False):
        RAN.clear()
        SESSION.pending = None
        planner = ScriptedPlanner([call("test.stop_ok", {"label": "one"}), call("test.stop_l2"),
                                   call("test.stop_ok", {"label": "after"}), done("finished")])

        async def controller():
            for _ in range(500):
                if SESSION.pending is not None and SESSION.pending.kind == "confirm":
                    break
                await asyncio.sleep(0.01)
            else:
                return "no-confirmation"
            if stop:
                await SESSION.stop(source="test")
                await SESSION.stop(source="test")  # repeated: must be harmless
            else:
                await SESSION.handle(answer, actor="text")
            return "answered"

        with scripted_provider(planner):
            ctl = asyncio.create_task(controller())
            result = await asyncio.wait_for(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"), timeout=30)
            outcome = await ctl
        return result, outcome

    with store.use_temp_db():
        # control 1: 'yes' -> the confirmation was required AND the step ran, the goal continued
        r_yes, o_yes = await run_goal_with_answer("yes")
        check("F: (control) the L2 step still PAUSED for confirmation", o_yes == "answered")
        check("F: (control) 'yes' -> the L2 step ran once, and the goal went on",
              RAN.get("l2") == 1 and RAN.get("ok") == 2, str(RAN))

        # control 2: 'no' -> declined, the L2 body never ran
        r_no, o_no = await run_goal_with_answer("no")
        check("F: (control) 'no' -> the L2 step did not run", o_no == "answered" and RAN.get("l2", 0) == 0, str(RAN))
        check("F: (control) a declined confirmation is still not a 'cancelled' stop",
              r_no.data.get("stopped") != "cancelled", str(r_no.data.get("stopped")))

        # the new path: STOP while the confirmation is waiting
        r_stop, o_stop = await run_goal_with_answer(None, stop=True)
        check("F: the confirmation was reached before the stop", o_stop == "answered")
        check("F: STOP at the confirmation -> the L2 action never ran", RAN.get("l2", 0) == 0, str(RAN))
        check("F: ...and no step after it ran either", RAN.get("ok") == 1, str(RAN))
        check("F: the goal reports stopped='cancelled' and is not ok",
              r_stop.data.get("stopped") == "cancelled" and r_stop.ok is False, str(r_stop.data.get("stopped")))
        check("F: the user-facing result is a truthful stop summary",
              r_stop.speech.startswith("Stopped.") and "1 step had already run (test.stop_ok)" in r_stop.speech
              and "'test.stop_l2' was waiting for confirmation and did not run" in r_stop.speech, r_stop.speech)
        denied = [dict(row) for row in store.conn().execute(
            "SELECT * FROM audit WHERE skill='test.stop_l2' AND decision='denied'")]
        check("F: the audit shows the L2 call as denied (it was never confirmed)", len(denied) >= 1)
        gid = r_stop.data.get("goal_id")
        row = goals_mod.get(gid) if gid else None
        check("F: the tracked Goal is CANCELLED", row is not None and row.status == goals_mod.GoalStatus.CANCELLED)
        check("F: INTEL is back to a clean, non-armed state afterwards",
              INTEL.state.goal_status == "cancelled" and not INTEL.is_cancel_requested() and SESSION.pending is None)

        # a stop never authorises anything: an unattended actor is still capped
        try:
            await EXECUTOR.run("test.stop_l2", {}, actor="scheduler")
            capped = False
        except Exception:
            capped = True
        check("F: the unattended ceiling is untouched (scheduler still cannot run an L2 tool)", capped)


async def scenario_g_daemon() -> None:
    scenario("G. the daemon's /cancel goes through the same path")
    from friday import daemon

    INTEL.reset()
    with store.use_temp_db():
        idle = await daemon.cancel()
        check("G: /cancel with nothing running is ok and says so",
              idle["ok"] is True and "nothing running" in idle["speech"] and idle["data"]["stopped"] is False)
        check("G: ...and arms nothing", INTEL.is_cancel_requested() is False)
        INTEL.start_goal("daemon goal", "gid-g")
        res = await daemon.cancel()
        check("G: /cancel during a goal signals it", res["data"]["goal_running"] is True and INTEL.is_cancel_requested())
        res2 = await daemon.cancel()
        check("G: /cancel is idempotent", res2["data"] == res["data"])
        INTEL.end_goal(ok=False, status="cancelled")


async def scenario_h_gui() -> None:
    scenario("H. GUI wiring: STOP follows INTEL's goal state, calls Backend.stop; hotkey is valid")
    from PySide6.QtWidgets import QApplication

    from friday.gui.command_bar import CommandBar
    from friday.hotkey import _parse

    app = QApplication.instance() or QApplication([])
    calls: list[dict] = []

    class FakeBackend:
        def stop(self, *, source="gui", wait=True):
            calls.append({"source": source, "wait": wait})

    bar = CommandBar(FakeBackend())  # type: ignore[arg-type]
    check("H: the STOP control starts hidden", bar._stop_btn.isHidden())
    bar.on_task_state({"goal_status": "running"})
    check("H: it appears while a goal is running", not bar._stop_btn.isHidden())
    bar._stop_btn.click()
    for _ in range(100):
        if calls:
            break
        app.processEvents()
        await asyncio.sleep(0.01)
    check("H: clicking it calls Backend.stop(source='gui')", calls == [{"source": "gui", "wait": True}], str(calls))
    check("H: it shows the stop is in progress and cannot be double-clicked",
          bar._stop_btn.text().startswith("STOPPING") and not bar._stop_btn.isEnabled())
    bar.on_task_state({"goal_status": "running"})
    check("H: still stopping while the goal is still running", not bar._stop_btn.isEnabled())
    bar.on_task_state({"goal_status": "cancelled"})
    check("H: it disappears when the goal ends and resets for the next goal",
          bar._stop_btn.isHidden() and bar._stop_btn.isEnabled() and bar._stop_btn.text() == "STOP")

    hk = CFG.permissions.stop_hotkey
    others = {"ctrl+alt+space", CFG.voice.activation_hotkey}
    check("H: the emergency hotkey is a valid global hotkey", bool(hk) and _parse(hk) is not None, hk)
    check("H: ...and does not clash with the other FRIDAY hotkeys",
          all(_parse(hk) != _parse(o) for o in others), hk)
    src = Path(__file__).resolve().parent.parent.joinpath("friday", "gui", "app.py").read_text(encoding="utf-8")
    check("H: app.py binds it before HOTKEYS.start() and never blocks the hotkey thread",
          "stop_hotkey" in src and "wait=False" in src and src.index("stop_hotkey") < src.index("    HOTKEYS.start()"))

    from friday.gui.backend import Backend

    bk = Backend()
    INTEL.reset()
    INTEL.start_goal("backend goal", "gid-h")
    import threading

    box: dict = {}
    loop_thread = threading.Thread(target=lambda: (asyncio.set_event_loop(bk.loop), bk.loop.run_forever()), daemon=True)
    loop_thread.start()
    with store.use_temp_db():
        t = threading.Thread(target=lambda: box.setdefault("res", bk.stop(source="hotkey")))
        t.start()
        t.join(5)
        bk.loop.call_soon_threadsafe(bk.loop.stop)
    check("H: Backend.stop works from a foreign thread and arms the signal",
          INTEL.is_cancel_requested() and getattr(box.get("res"), "data", {}).get("goal_running") is True)
    INTEL.end_goal(ok=False, status="cancelled")


async def main() -> int:
    started = time.perf_counter()
    store.init()
    REGISTRY.discover()
    _register_fixtures()
    saved = lock_down_real_tools(REGISTRY)
    try:
        await scenario_a_signal()
        await scenario_b_no_new_steps()
        await scenario_c_interrupt_async()
        await scenario_d_blocking()
        await scenario_e_session_stop()
        await scenario_f_end_to_end()
        await scenario_g_daemon()
        await scenario_h_gui()
    finally:
        CFG.permissions.overrides = saved
    return finish("Phase 28.0 stop control", time.perf_counter() - started, min_assertions=80, min_scenarios=8)


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
