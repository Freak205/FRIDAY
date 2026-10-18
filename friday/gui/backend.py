"""Owns the asyncio loop the Qt GUI talks to.

Ported from `friday/desktop.py`'s `Backend` (superseded), which existed
because Tkinter insists on owning the main thread — the same is true of
`QApplication.exec()`, so the shape is unchanged: one dedicated asyncio
event loop on a background thread, bridged via
`asyncio.run_coroutine_threadsafe`. Every other subsystem in this project
(hotkeys, wake-word scanning, TTS's COM worker, per-cycle voice threads)
already follows "own your thread, communicate via callback" — this keeps
the GUI consistent with that instead of merging asyncio onto the Qt loop
(e.g. via `qasync`), which would also mean one accidentally-blocking skill
coroutine could stall the entire UI instead of just its own background loop.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from friday import store
from friday.brain import BRAIN
from friday.bus import BUS
from friday.log import get, setup
from friday.registry import REGISTRY, SkillResult
from friday.session import SESSION

log = get(__name__)


class Backend:
    """Owns the asyncio loop so Qt can stay on the main thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error = False
        # Real, non-fabricated boot milestones — appended to as `_boot`
        # actually completes each step, for the startup screen's checklist.
        # Never used to synthesize a minimum wait; only to report the truth.
        self.stages: list[str] = []
        # Phase 40: requests `ask` stopped waiting for but that are still running, by normalised text -> (future, id).
        self._late: dict[str, tuple[Future, str]] = {}
        self._late_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="friday-core")

    # -- lifecycle -------------------------------------------------------------

    def start_async(self) -> None:
        """Non-blocking — starts the backend thread and returns immediately.
        The caller (the Qt GUI) must poll `.ready`/`.error` on its own timer
        rather than block its event-loop thread waiting for readiness.
        """
        self._thread.start()

    def start(self) -> None:
        """Blocking variant, kept for any non-GUI caller (tests, scripts)
        that doesn't have an event loop of its own to keep spinning."""
        self.start_async()
        self.ready.wait(timeout=120)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._boot())
        except Exception:
            # Under pythonw there is no console, so an unhandled exception
            # here would kill the backend thread with no trace anywhere.
            # Log it, and release the waiter so the UI comes up degraded
            # rather than hanging for the full timeout.
            log.exception("backend failed to start")
            self.error = True
            self.ready.set()
            return
        self.loop.run_forever()

    async def _boot(self) -> None:
        setup()
        store.init()
        self.stages.append("memory")

        REGISTRY.discover()
        self.stages.append(f"registry:{len(REGISTRY)}")

        await asyncio.to_thread(BRAIN.warm)
        self.stages.append("brain")

        # Automation is best-effort: if the scheduler can't start, the
        # command bar should still work.
        try:
            from friday.jobs import SCHEDULER
            from friday.triggers import WATCHER

            SCHEDULER.start()
            WATCHER.start()
            self.stages.append("automation")
        except Exception:
            log.exception("automation failed to start; continuing without it")

        log.info("gui backend ready — %d skills", len(REGISTRY))
        self.ready.set()

    def shutdown(self) -> None:
        """Best-effort — stop the loop so the background thread can exit."""
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    # -- bridges used by the GUI thread ----------------------------------------

    # Phase 39: how much longer than a goal's own time bound the caller keeps waiting for it.
    ASK_MARGIN_S = 30.0

    def ask_timeout_s(self) -> float:
        """How long `ask` waits: a goal is bounded by `planner.total_timeout_s` (300 s) and ends itself, so waiting only
        120 s reported "Something went wrong" for a goal that was still running and went on to finish."""
        from friday.config import CFG

        return max(120.0, float(CFG.planner.total_timeout_s) + self.ASK_MARGIN_S)

    def ask(self, text: str, *, actor: str = "text") -> Any:
        """Run an utterance on the backend loop and wait for the result.
        Must only be called from a worker thread, never the Qt thread —
        this blocks the calling thread up to `ask_timeout_s()`.

        Running out of patience is not a failure of the request: the coroutine keeps running on the backend loop
        (cancelling it would cut a step in half), so the caller is told exactly that -- not "failed", not "done".

        Phase 40: that request is remembered, and the SAME words asked again while it is still running are not
        submitted a second time (a retry of a slow state-changing request would run it twice); the caller gets the
        same "still running" answer, marked `repeat`. A request that has finished, or different words, run as usual.

        Phase 41: every request that times out gets an id (`data.request_id`, the same one on a repeat), and the ORIGINAL
        future -- the one already running, never a new call -- reports its real outcome on the BUS as `backend.late_result`
        when it ends: its own result, or its own exception, or its own cancellation, each exactly once.
        """
        key = " ".join(text.lower().split())
        with self._late_lock:
            earlier = self._late.get(key)
            if earlier is not None and earlier[0].done():
                del self._late[key]
                earlier = None
        if earlier is not None:
            log.warning("ask(%r) not resubmitted: the same request is still running", text[:60])
            return self._still_running_result(repeat=True, request_id=earlier[1])

        future = asyncio.run_coroutine_threadsafe(
            SESSION.handle(text, actor=actor), self.loop
        )
        try:
            return future.result(timeout=self.ask_timeout_s())
        except FutureTimeout:
            log.warning("ask(%r) still running after %.0fs; not cancelled", text[:60], self.ask_timeout_s())
            request_id = uuid.uuid4().hex[:12]
            with self._late_lock:
                self._late[key] = (future, request_id)
            # registered once, here; if the future has already ended this runs at once, still exactly one delivery
            future.add_done_callback(lambda f, rid=request_id, said=text, who=actor: self._deliver_late(f, rid, said, who))
            return self._still_running_result(repeat=False, request_id=request_id)

    LATE_TOPIC = "backend.late_result"

    def _deliver_late(self, future: Future, request_id: str, utterance: str, actor: str = "") -> None:
        """Publish how the original request really ended. Nothing is re-run and `ask` is not called: this only reads the
        finished future. A result is reported with its own `ok` (never promoted to success); an exception or a
        cancellation is reported as exactly that, and an exception also says part of the request may have run."""
        error = cancelled = False
        if future.cancelled():
            cancelled, ok = True, False
            speech = "The earlier request was cancelled before it finished."
        else:
            exc = future.exception()
            if exc is not None:
                error, ok = True, False
                try:
                    from friday import toolview

                    detail = toolview.sanitize(str(exc), prose=True)[:200]
                except Exception:
                    detail = ""
                speech = (f"The earlier request ended with an error ({type(exc).__name__}"
                          f"{': ' + detail if detail else ''}). Part of it may already have run, so check before repeating it.")
            else:
                result = future.result()
                ok = bool(getattr(result, "ok", False))
                speech = str(getattr(result, "speech", "") or "") or "The earlier request finished without a message."
        log.info("late result for %s: ok=%s error=%s cancelled=%s", request_id, ok, error, cancelled)
        try:
            self.publish(self.LATE_TOPIC, request_id=request_id, utterance=utterance, speech=speech, ok=ok,
                         error=error, cancelled=cancelled, actor=actor)
        except Exception:
            log.exception("could not publish the late result for %s", request_id)

    def _still_running_result(self, *, repeat: bool, request_id: str = "") -> SkillResult:
        """The one wording of "this has not finished, and I do not know how it will turn out". It never says it worked or
        failed, tells the user not to repeat it, and describes STOP as the existing STOP really behaves: it ends a
        running GOAL (no new step starts; a step already underway may finish; nothing already carried out is reversed), so
        for a request that is not a goal it says STOP cannot end it rather than promising that it can."""
        try:
            from friday.intelligence.state import INTEL

            stoppable = bool(INTEL.goal_running)
        except Exception:
            stoppable = False
        lead = ("That request is still running; I have not started it again." if repeat
                else "That is still running, and I haven't heard back from it yet.")
        stop = ("To end it, press STOP or say stop: no new step will start, though a step already underway may finish "
                "and nothing already carried out is reversed." if stoppable
                else "STOP cannot end this one; it will finish or time out by itself.")
        return SkillResult(
            speech=f"{lead} I can't tell you yet how it turned out, so please don't repeat it. {stop}",
            ok=False,
            data={"still_running": True, "repeat": repeat, "stoppable": stoppable, "request_id": request_id},
        )

    def stop(self, *, source: str = "gui", wait: bool = True) -> Any:
        """Stop the running goal. Safe from ANY thread (Qt, hotkey, voice). The stop signal
        itself is set right here, on the calling thread, so it lands even if the backend loop
        is momentarily busy; `Session.stop` then runs on the loop to decline a waiting
        confirmation and do the bookkeeping. `wait=False` returns at once (the hotkey path)."""
        from friday.intelligence.state import INTEL

        INTEL.request_cancel()
        future = asyncio.run_coroutine_threadsafe(SESSION.stop(source=source), self.loop)
        return future.result(timeout=5) if wait else future

    def publish(self, topic: str, **data: Any) -> None:
        """Fire-and-forget a BUS event from a non-asyncio thread."""
        asyncio.run_coroutine_threadsafe(BUS.publish(topic, **data), self.loop)

    def ping_llm(self) -> Future:
        """Schedule a cheap Ollama reachability check on the backend loop.
        Returns a `concurrent.futures.Future[bool]` — attach a done-callback
        rather than blocking the Qt thread on `.result()`.
        """
        from friday.llm import ping

        return asyncio.run_coroutine_threadsafe(ping(), self.loop)

    def observe(self, **kwargs: Any) -> Future:
        """Schedule a `friday.desktop_observer.observe()` call on the
        backend loop; returns a `concurrent.futures.Future`. Safe to call
        from the Qt thread — attach a `add_done_callback` rather than
        `.result()`, since that callback fires on the backend loop thread
        and must itself do nothing but marshal the result onward (e.g. via
        a Qt signal), never touch a widget directly.
        """
        from friday.desktop_observer import observe

        return asyncio.run_coroutine_threadsafe(observe(**kwargs), self.loop)
