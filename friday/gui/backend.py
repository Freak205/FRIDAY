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
from concurrent.futures import Future
from typing import Any

from friday import store
from friday.brain import BRAIN
from friday.bus import BUS
from friday.log import get, setup
from friday.registry import REGISTRY
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

    def ask(self, text: str, *, actor: str = "text") -> Any:
        """Run an utterance on the backend loop and wait for the result.
        Must only be called from a worker thread, never the Qt thread —
        this blocks the calling thread up to 120s.
        """
        future = asyncio.run_coroutine_threadsafe(
            SESSION.handle(text, actor=actor), self.loop
        )
        return future.result(timeout=120)

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
