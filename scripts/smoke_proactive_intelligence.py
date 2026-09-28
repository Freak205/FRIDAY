"""Phase 11.5 — proactive situational intelligence.

    EVENT -> CONTEXT -> RELEVANCE -> DECISION -> OPTIONAL SUGGESTION -> USER DECIDES

Entirely deterministic: `friday.notify.send` is monkeypatched so the test
never actually pops a Windows toast, and `friday.intelligence.goals
.most_recent_active` is monkeypatched in the sections that need a specific
active-goal state without depending on whatever real goal rows happen to be
in the shared SQLite store (same convention `smoke_intelligence.py` uses for
`episodes.retrieve_similar`/`goals_mod.create`).

Uses the real BUS, the real global `PROACTIVE`/`SESSION` singletons for the
end-to-end/wiring sections, and fresh `ProactiveEngine()` instances for the
pure pipeline sections (cooldown/rate-limit) that need a clean slate.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.bus import BUS, Event  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence import proactive  # noqa: E402
from friday.intelligence.proactive import PROACTIVE, ProactiveAction, ProactiveEngine, Relevance, SituationalEvent
from friday.intelligence.self_state import SELF_STATE, SelfState, SelfStatus  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402 (importing triggers PROACTIVE.wire() via Session.__init__)


def _idle() -> None:
    SELF_STATE.state = SelfState(status=SelfStatus.IDLE, updated_at="test")


def _busy(status: SelfStatus = SelfStatus.EXECUTING) -> None:
    SELF_STATE.state = SelfState(status=status, updated_at="test")


def _reset_global_engine() -> None:
    PROACTIVE._cooldowns.clear()
    PROACTIVE._notify_times.clear()
    PROACTIVE._queue.clear()
    PROACTIVE._last_goal_seen = None


@contextlib.contextmanager
def fixed_active_goal(goal):
    """Deterministic stand-in for `goals.most_recent_active()` — avoids
    depending on real goal rows already in the shared SQLite store."""
    original = goals_mod.most_recent_active
    goals_mod.most_recent_active = lambda: goal
    try:
        yield
    finally:
        goals_mod.most_recent_active = original


def fake_goal(objective: str, *, goal_id: str = "fake-goal", status=None):
    return types.SimpleNamespace(
        id=goal_id, objective=objective, original_request=objective,
        status=status or goals_mod.GoalStatus.RUNNING,
    )


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    overall = True
    _idle()

    # Never actually pop a Windows toast during the test.
    import friday.notify as notify_mod

    notify_calls: list[tuple[str, str]] = []
    original_notify_send = notify_mod.send

    def fake_notify_send(title, message="", *, urgent=False):
        notify_calls.append((title, message))
        return True

    notify_mod.send = fake_notify_send

    try:
        # -- A: relevant event (active goal matches) ---------------------------
        print("\n--- A: relevant event ---\n")
        goal = fake_goal("Work on my FRIDAY project")
        event = SituationalEvent(
            event_type="active_window_changed", source="desktop_observer",
            summary="Active window changed to 'FRIDAY - Visual Studio Code'.",
            entity="FRIDAY - Visual Studio Code",
        )
        relevance, reason = proactive.assess_relevance(event, goal=goal)
        ok = relevance == Relevance.RELEVANT
        print(f"  {'OK  ' if ok else 'MISS'} VS Code on the FRIDAY project vs. goal 'Work on my FRIDAY project' -> {relevance.value} ({reason})")
        overall &= ok

        # -- B: irrelevant event (no relationship to goal / no goal) -----------
        print("\n--- B: irrelevant event ---\n")
        calc_event = SituationalEvent(
            event_type="application_opened", source="desktop_observer",
            summary="Calculator.exe opened.", entity="Calculator.exe",
        )
        relevance, reason = proactive.assess_relevance(calc_event, goal=goal)
        ok = relevance == Relevance.NOT_RELEVANT
        print(f"  {'OK  ' if ok else 'MISS'} Calculator vs. an active FRIDAY-project goal -> {relevance.value} ({reason})")
        overall &= ok

        relevance, reason = proactive.assess_relevance(calc_event, goal=None)
        ok = relevance == Relevance.NOT_RELEVANT
        print(f"  {'OK  ' if ok else 'MISS'} Calculator with no active goal at all -> {relevance.value} ({reason})")
        overall &= ok

        # -- C: uncertain event (insufficient evidence) -------------------------
        print("\n--- C: uncertain event ---\n")
        mystery_event = SituationalEvent(
            event_type="application_opened", source="desktop_observer",
            summary="SomeRandomTool.exe opened.", entity="SomeRandomTool.exe",
        )
        relevance, reason = proactive.assess_relevance(mystery_event, goal=goal)
        ok = relevance == Relevance.UNCERTAIN
        print(f"  {'OK  ' if ok else 'MISS'} unmapped app vs. an active goal, no keyword overlap -> {relevance.value} ({reason})")
        overall &= ok
        action = proactive.decide_action(relevance, mystery_event)
        ok = action == ProactiveAction.WAIT
        print(f"  {'OK  ' if ok else 'MISS'} UNCERTAIN always decides WAIT (never guesses) -> {action.value}")
        overall &= ok

        # -- D: goal/task completion produces a safe notification --------------
        print("\n--- D: task completion -> safe notification ---\n")
        engine = ProactiveEngine()
        build_event = SituationalEvent(
            event_type="task_completed", source="orchestrator",
            summary="Build completed successfully.", entity="build-d",
        )
        out = await engine.handle(build_event)
        ok = out is not None and out.action == ProactiveAction.INFORM and out.text == "Build completed successfully."
        print(f"  {'OK  ' if ok else 'MISS'} task_completed -> INFORM with the exact safe summary -> {out.text if out else None!r}")
        overall &= ok
        ok = notify_calls and notify_calls[-1][1] == "Build completed successfully."
        print(f"  {'OK  ' if ok else 'MISS'} surfaced via notify.send -> {notify_calls[-1] if notify_calls else None}")
        overall &= ok

        # -- E: a meaningful desktop transition produces exactly one event -----
        print("\n--- E: desktop transition -> one situational event ---\n")
        _reset_global_engine()
        _idle()
        notices: list[dict] = []

        async def _capture_notice(ev: Event) -> None:
            notices.append(dict(ev.data))

        BUS.subscribe("proactive.notice", _capture_notice)
        try:
            with fixed_active_goal(fake_goal("Work on my FRIDAY project", goal_id="g-e")):
                await BUS.publish("trigger.window.changed", title="FRIDAY - Visual Studio Code (E)", previous="")
            ok = len(notices) == 1 and notices[0]["event_type"] == "active_window_changed"
            print(f"  {'OK  ' if ok else 'MISS'} VS Code on the FRIDAY project -> exactly one proactive notice ({len(notices)})")
            overall &= ok

            # -- F: repeated observation of the same state -> no event storm ---
            print("\n--- F: repeated desktop observation -> no storm ---\n")
            with fixed_active_goal(fake_goal("Work on my FRIDAY project", goal_id="g-e")):
                for _ in range(3):
                    await BUS.publish("trigger.window.changed", title="FRIDAY - Visual Studio Code (E)", previous="")
            ok = len(notices) == 1
            print(f"  {'OK  ' if ok else 'MISS'} 3 repeats of the same window -> still exactly one notice total ({len(notices)})")
            overall &= ok

            # -- unrelated app opening in the same window -> silence ------------
            with fixed_active_goal(fake_goal("Work on my FRIDAY project", goal_id="g-e")):
                await BUS.publish("trigger.process.started", process="Calculator.exe")
            ok = len(notices) == 1
            print(f"  {'OK  ' if ok else 'MISS'} Calculator opening stays silent -> notice count unchanged ({len(notices)})")
            overall &= ok
        finally:
            BUS.unsubscribe("proactive.notice", _capture_notice)

        # -- G: cooldown -> only one output within the cooldown window ----------
        print("\n--- G: cooldown suppresses an immediate repeat ---\n")
        engine = ProactiveEngine()
        cd_event = SituationalEvent(
            event_type="task_completed", source="orchestrator",
            summary="Cooldown test task completed successfully.", entity="cooldown-g",
        )
        first = await engine.handle(cd_event)
        second = await engine.handle(cd_event)
        ok = first is not None and second is None
        print(f"  {'OK  ' if ok else 'MISS'} same fingerprint fired twice back-to-back -> first={bool(first)}, second={bool(second)}")
        overall &= ok

        # -- H: notification rate limit ------------------------------------------
        print("\n--- H: notification rate limit ---\n")
        engine = ProactiveEngine()
        original_max = CFG.intelligence.proactive_max_notifications
        CFG.intelligence.proactive_max_notifications = 2
        try:
            results = []
            for i in range(4):
                ev = SituationalEvent(
                    event_type="task_completed", source="orchestrator",
                    summary=f"Rate-limit test task {i} completed successfully.", entity=f"rate-h-{i}",
                )
                results.append(await engine.handle(ev))
            produced = sum(1 for r in results if r is not None)
            ok = produced == 2
            print(f"  {'OK  ' if ok else 'MISS'} 4 distinct relevant events, cap=2 -> {produced} notification(s) produced")
            overall &= ok
        finally:
            CFG.intelligence.proactive_max_notifications = original_max

        # -- I: active interaction suppression -----------------------------------
        print("\n--- I: active interaction is not interrupted ---\n")
        engine = ProactiveEngine()
        _busy(SelfStatus.EXECUTING)
        busy_event = SituationalEvent(
            event_type="task_completed", source="orchestrator",
            summary="Busy-suppressed task completed successfully.", entity="busy-i",
        )
        out = await engine.handle(busy_event)
        ok = out is None and busy_event in engine._queue
        print(f"  {'OK  ' if ok else 'MISS'} relevant event while EXECUTING -> suppressed and queued (queue len={len(engine._queue)})")
        overall &= ok

        # -- J: post-interaction relevance (the queued event can still surface) --
        print("\n--- J: queued event surfaces after the interaction ends ---\n")
        _idle()
        flushed = await engine.flush_queue()
        ok = len(flushed) == 1 and flushed[0].text == busy_event.summary
        print(f"  {'OK  ' if ok else 'MISS'} back to IDLE -> the queued event is now surfaced ({len(flushed)})")
        overall &= ok
        _idle()

        # -- K: consequential action boundary -------------------------------------
        print("\n--- K: proactive engine never executes anything ---\n")
        engine = ProactiveEngine()
        executor_called = False
        original_run = EXECUTOR.run

        async def _tripwire(*a, **kw):
            nonlocal executor_called
            executor_called = True
            return await original_run(*a, **kw)

        EXECUTOR.run = _tripwire
        try:
            out = await engine.handle(SituationalEvent(
                event_type="task_completed", source="orchestrator",
                summary="Consequential-boundary test completed successfully.", entity="boundary-k",
            ))
            with fixed_active_goal(fake_goal("Message Rahul the update", goal_id="g-k")):
                offer_out = await engine.handle(SituationalEvent(
                    event_type="active_window_changed", source="desktop_observer",
                    summary="WhatsApp is active.", entity="WhatsApp.exe",
                    relevant_context={"offer": True},
                ))
            ok = out is not None and executor_called is False
            print(f"  {'OK  ' if ok else 'MISS'} an INFORM output never calls EXECUTOR.run -> executor_called={executor_called}")
            overall &= ok
            ok = offer_out is not None and offer_out.action == ProactiveAction.ASK and executor_called is False
            print(f"  {'OK  ' if ok else 'MISS'} an ASK-shaped (offer=True) output still never calls EXECUTOR.run -> action={offer_out.action.value if offer_out else None}")
            overall &= ok
        finally:
            EXECUTOR.run = original_run

        # -- L: permission boundary is never bypassed ------------------------------
        print("\n--- L: permission denial cannot be bypassed by a suggestion ---\n")
        ok = not hasattr(proactive.ProactiveOutput, "execute") and not hasattr(proactive.SituationalEvent, "execute")
        print(f"  {'OK  ' if ok else 'MISS'} ProactiveOutput/SituationalEvent expose no execution path at all")
        overall &= ok
        ok = executor_called is False
        print(f"  {'OK  ' if ok else 'MISS'} even an ASK-shaped (offer=True) event never touched EXECUTOR -> executor_called={executor_called}")
        overall &= ok

        # -- M: proactive intelligence can be disabled ------------------------------
        print("\n--- M: proactive_enabled=False -> total silence ---\n")
        engine = ProactiveEngine()
        original_enabled = CFG.intelligence.proactive_enabled
        CFG.intelligence.proactive_enabled = False
        try:
            out = await engine.handle(SituationalEvent(
                event_type="task_completed", source="orchestrator",
                summary="Disabled test completed successfully.", entity="disabled-m",
            ))
            ok = out is None
            print(f"  {'OK  ' if ok else 'MISS'} disabled -> handle() returns None for an otherwise-relevant event")
            overall &= ok
        finally:
            CFG.intelligence.proactive_enabled = original_enabled

        # -- N: privacy — no OCR/screenshot/secret content ever enters an event ---
        print("\n--- N: privacy boundaries ---\n")
        field_names = {f for f in SituationalEvent.__dataclass_fields__}
        forbidden = {"screenshot", "screenshot_path", "ocr_text", "ocr", "password", "token", "raw_browser_content"}
        ok = not (field_names & forbidden)
        print(f"  {'OK  ' if ok else 'MISS'} SituationalEvent has no OCR/screenshot/secret field -> fields={sorted(field_names)}")
        overall &= ok

        before = len(episodes.recent(1000))
        engine = ProactiveEngine()
        await engine.handle(SituationalEvent(
            event_type="task_completed", source="orchestrator",
            summary="Privacy test completed successfully.", entity="privacy-n",
            relevant_context={"note": "password: hunter2"},
        ))
        after = len(episodes.recent(1000))
        ok = after == before
        print(f"  {'OK  ' if ok else 'MISS'} handling an event never writes a new episode/DB row ({before} -> {after})")
        overall &= ok

        # -- O: existing goal integration — updates context, no second goal -------
        print("\n--- O: goal integration never creates a second goal ---\n")
        before_goals = len(goals_mod.recent(1000))
        engine = ProactiveEngine()
        await engine.handle(SituationalEvent(
            event_type="task_completed", source="orchestrator",
            summary="Goal-integration test completed successfully.", entity="goal-integration-o",
        ))
        after_goals = len(goals_mod.recent(1000))
        ok = after_goals == before_goals
        print(f"  {'OK  ' if ok else 'MISS'} handling a situational event creates no new goal ({before_goals} -> {after_goals})")
        overall &= ok

        # -- P: existing scheduler/automation integration --------------------------
        print("\n--- P: scheduled task due reuses jobs.py's own notification ---\n")
        _reset_global_engine()
        _idle()
        notices = []
        BUS.subscribe("proactive.notice", _capture_notice)
        try:
            notify_calls.clear()
            await BUS.publish("job.done", job="Smoke Reminder Job", id=9001, ok=True, detail="done", ms=5)
            ok = len(notices) == 1 and notices[0]["event_type"] == "scheduled_task_due"
            print(f"  {'OK  ' if ok else 'MISS'} job.done -> one scheduled_task_due proactive.notice ({len(notices)})")
            overall &= ok
            ok = len(notify_calls) == 0
            print(f"  {'OK  ' if ok else 'MISS'} scheduled_task_due never double-notifies via notify.send (jobs.py already does) -> calls={len(notify_calls)}")
            overall &= ok
        finally:
            BUS.unsubscribe("proactive.notice", _capture_notice)

        # -- Narrative: the realistic end-to-end walkthrough (brief §18) ----------
        print("\n--- Narrative: goal -> relevant suggestion -> silence -> build report ---\n")
        _reset_global_engine()
        _idle()
        notices = []
        BUS.subscribe("proactive.notice", _capture_notice)
        try:
            with fixed_active_goal(fake_goal("Work on my FRIDAY project", goal_id="g-narrative")):
                # 1-5: VS Code opens on the FRIDAY project -> one relevant suggestion.
                await BUS.publish("trigger.window.changed", title="FRIDAY - Visual Studio Code (narrative)", previous="")
                ok = len(notices) == 1 and notices[0]["action"] == "suggest"
                print(f"  {'OK  ' if ok else 'MISS'} VS Code on the FRIDAY project -> one SUGGEST notice ({notices[-1] if notices else None})")
                overall &= ok

                # 6: repeated observer cycles produce nothing further.
                for _ in range(3):
                    await BUS.publish("trigger.window.changed", title="FRIDAY - Visual Studio Code (narrative)", previous="")
                ok = len(notices) == 1
                print(f"  {'OK  ' if ok else 'MISS'} repeated cycles of the same window -> still just one notice ({len(notices)})")
                overall &= ok

                # 7-9: Calculator opens -> no relevant relationship -> silence.
                await BUS.publish("trigger.process.started", process="Calculator.exe")
                ok = len(notices) == 1
                print(f"  {'OK  ' if ok else 'MISS'} Calculator opens -> FRIDAY stays silent ({len(notices)})")
                overall &= ok

            # 10-11: a background build completes -> FRIDAY reports it.
            await BUS.publish(
                "orchestrator.done", goal="Run the FRIDAY build", ok=True,
                stopped="completed", actor="scheduler",
            )
            ok = len(notices) == 2 and notices[-1]["action"] == "inform" and "Run the FRIDAY build" in notices[-1]["text"]
            print(f"  {'OK  ' if ok else 'MISS'} background build completion -> one INFORM notice ({notices[-1] if len(notices) > 1 else None})")
            overall &= ok

            # A direct (text/voice actor) orchestrator run must NOT be re-announced
            # — the user already got that response through the normal reactive path.
            await BUS.publish(
                "orchestrator.done", goal="A direct user-run goal", ok=True,
                stopped="completed", actor="text",
            )
            ok = len(notices) == 2
            print(f"  {'OK  ' if ok else 'MISS'} a direct text-actor orchestrator run is never re-announced ({len(notices)})")
            overall &= ok
        finally:
            BUS.unsubscribe("proactive.notice", _capture_notice)

    finally:
        notify_mod.send = original_notify_send
        _idle()

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
