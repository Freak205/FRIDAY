"""Fast, offscreen smoke test for the PySide6 desktop interface (Phase 10.x).

No real models, no microphone, no Ollama — a fake `Backend` stub stands in
(same convention as scripts/smoke_voice_conversation.py's fakes for
STT/TTS/wake-word). The point is proving the GUI's wiring — BUS
subscriptions, the CoreState projection, the confirm-panel actor
passthrough, and that the real widget tree builds/shows/closes without
exception — not exercising the brain/voice pipeline itself (see
scripts/smoke_gui_live.py for that, manual/opt-in).

Forces `QT_QPA_PLATFORM=offscreen` before any Qt import so this runs headless
in CI/without a display.
"""

import asyncio
import os
import sys
import time
from concurrent.futures import Future
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from friday.bus import BUS  # noqa: E402
from friday.intelligence.self_state import SELF_STATE, SelfStatus  # noqa: E402
from friday.gui.confirm_panel import ConfirmPanel  # noqa: E402
from friday.gui.main_window import MainWindow  # noqa: E402
from friday.gui.state_hub import CoreState, GuiStateHub  # noqa: E402


class FakeBackend:
    """Minimal stand-in for friday.gui.backend.Backend — records calls
    instead of touching a real asyncio loop/brain."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.error = False

        import threading

        self.ready = threading.Event()
        self.ready.set()
        self.stages = ["memory", "registry:0", "brain"]

    def ask(self, text: str, *, actor: str = "text"):
        self.asked.append((text, actor))

        class _Result:
            speech = "ok"
            ok = True

        return _Result()

    def observe(self, **kwargs):
        future: Future = Future()
        future.set_exception(RuntimeError("no desktop_observer in smoke test"))
        return future

    def ping_llm(self):
        future: Future = Future()
        future.set_result(False)
        return future

    def shutdown(self) -> None:
        pass


def main() -> bool:
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    app = QApplication.instance() or QApplication(sys.argv)

    SELF_STATE.wire()  # idempotent — see SelfStateTracker.wire()

    backend = FakeBackend()
    hub = GuiStateHub(backend)
    hub.start()

    # -- CoreState projection from SELF_STATE ------------------------------

    print("\n--- CoreState reflects SELF_STATE via polling ---\n")
    SELF_STATE._set(SelfStatus.EXECUTING, task="test.skill")
    states: list[str] = []
    hub.coreStateChanged.connect(states.append)
    hub._poll_self_state()
    check("EXECUTING self-state maps to CoreState.EXECUTING", states[-1] == CoreState.EXECUTING.value)

    SELF_STATE._set(SelfStatus.IDLE)
    hub._poll_self_state()
    check("IDLE self-state maps to CoreState.IDLE", states[-1] == CoreState.IDLE.value)

    # -- voice raw state takes priority over SELF_STATE for listening -------

    print("\n--- voice on_state overrides SELF_STATE for LISTENING/SPEAKING ---\n")
    hub.on_voice_state("listening")
    check("voice 'listening' maps to CoreState.LISTENING", states[-1] == CoreState.LISTENING.value)
    hub.on_voice_state("done")
    hub._poll_self_state()
    check("voice 'done' releases control back to SELF_STATE (IDLE)", states[-1] == CoreState.IDLE.value)

    # -- task state polling (INTEL.snapshot(), not INTEL.state.snapshot()) --

    print("\n--- task-state polling calls the real INTEL.snapshot() API ---\n")
    # `_poll_task_state` swallows its own exceptions (best-effort, non-fatal
    # per its docstring) — so the regression check that actually matters is
    # whether taskStateChanged fired at all, not whether calling it raised.
    # (This is exactly the class of bug a live launch caught that this
    # offscreen test originally missed: INTEL.state.snapshot() vs. the real
    # INTEL.snapshot() API — see friday/intelligence/state.py.)
    task_snapshots: list[dict] = []
    hub.taskStateChanged.connect(task_snapshots.append)
    hub._poll_task_state()
    check(
        "taskStateChanged emitted a real INTEL.snapshot() dict",
        bool(task_snapshots) and "execution_status" in task_snapshots[-1],
    )

    # -- confirm flow: BUS event -> signal -> actor passthrough -------------

    print("\n--- session.awaiting_confirm -> confirmRequested, then actor-correct resolution ---\n")
    confirm_payloads: list[dict] = []
    hub.confirmRequested.connect(confirm_payloads.append)

    async def publish_confirm() -> None:
        await BUS.publish(
            "session.awaiting_confirm",
            skill="test.dangerous_skill", tier="L3",
            preview="Send a message", speech="Send a message. Should I go ahead?",
            actor="voice",
        )

    asyncio.run(publish_confirm())
    check("confirmRequested fired with the event payload", len(confirm_payloads) == 1)
    check("payload actor is 'voice', not a fabricated 'gui'", confirm_payloads and confirm_payloads[0].get("actor") == "voice")
    check("pending confirmation raises CoreState to AWAITING_CONFIRM", states[-1] == CoreState.AWAITING_CONFIRM.value)

    panel = ConfirmPanel(backend)
    panel.show_request(confirm_payloads[0])
    panel._on_confirm()  # simulates clicking CONFIRM
    time.sleep(0.3)  # the worker thread + queued signal need one beat
    app.processEvents()
    check(
        "ConfirmPanel answers via backend.ask() with the ORIGINAL actor ('voice'), never 'gui'",
        ("yes", "voice") in backend.asked,
    )
    check("panel dismisses itself after answering", panel.isHidden())

    # -- a resolution arriving from elsewhere (voice auto-listener / timeout) --

    print("\n--- confirmResolved from elsewhere dismisses the panel too (no double-answer) ---\n")
    panel2 = ConfirmPanel(backend)
    panel2.show_request({"skill": "test.other_skill", "actor": "text", "preview": "Do a thing"})
    check("panel2 is visible after show_request", not panel2.isHidden())
    panel2.resolve_externally("test.other_skill", "declined")
    check("panel2 dismisses when told the pending resolved elsewhere", panel2.isHidden())
    already_dismissed_len = len(backend.asked)
    panel2._on_confirm()  # a stray click that lost the race must be a no-op
    check("a stray click after external resolution is a no-op (discarded, not re-asked)", len(backend.asked) == already_dismissed_len)

    # -- full widget tree builds/shows/closes without exception -------------

    print("\n--- MainWindow builds, shows, and closes cleanly (offscreen) ---\n")
    try:
        window = MainWindow(backend, hub)
        window.show()
        app.processEvents()
        window.apply_boot_stages(backend.stages)
        window.close()
        app.processEvents()
        built_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  EXCEPTION: {exc!r}")
        built_ok = False
    check("MainWindow constructs/shows/closes without exception", built_ok)

    hub.stop()
    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    return overall


if __name__ == "__main__":
    _ok = main()
    sys.stdout.flush()
    # PySide6 widgets created without ever entering app.exec() can segfault
    # during normal Python interpreter teardown (GC destroys the QApplication
    # and its still-alive child widgets in an unpredictable order) — a
    # test-harness-only artifact of this script never running a real Qt event
    # loop, not a defect in the app itself (friday/gui/app.py's real run()
    # always shuts down via app.quit() from inside app.exec(), the normal,
    # safe path). os._exit() skips that teardown entirely; every check above
    # has already printed its result by this point.
    os._exit(0 if _ok else 1)
