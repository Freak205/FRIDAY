"""Entry point for `friday desktop` — the cinematic PySide6 client.

Boot sequence (PLAN §23, §5 of the design review): the single-instance lock
is acquired before any Qt object exists; `Backend.start_async()` returns
immediately so the Qt event loop is never blocked waiting for the brain to
warm up; the main window shows in a BOOTING state right away and a short
poll timer flips it to ready the moment the backend genuinely is — no
fabricated delay, no artificial minimum wait. Voice startup begins in
parallel with backend boot, exactly as `friday/desktop.py` (superseded)
already proved safe (`run_coroutine_threadsafe` calls queue safely even
while `_boot()` is still running).
"""

from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from friday.config import CFG
from friday.hotkey import HOTKEYS
from friday.log import get

from . import single_instance
from .backend import Backend
from .main_window import MainWindow
from .state_hub import GuiStateHub
from .tray import build_tray

log = get(__name__)

HOTKEY = "ctrl+alt+space"

_conversation_loop: Any = None


def _speak_late_results(announce: Any) -> None:
    """Phase 42: voice requests that finish after `Backend.ask` stopped waiting are announced once (best-effort)."""
    try:
        from friday.voice.late import subscribe_late_announcements

        subscribe_late_announcements(announce)
    except Exception:
        log.exception("late-result announcements unavailable; results are still shown on screen")


def _start_voice(backend: Backend, hub: GuiStateHub) -> None:
    """Best-effort at every layer, ported from `friday/desktop.py`'s
    `_start_voice`: a failure anywhere here must never stop the rest of the
    GUI from working, and Ctrl+Alt+V must keep working even if wake-word
    listening itself can't start.
    """
    global _conversation_loop

    if not CFG.voice.enabled:
        log.info("voice disabled in config.yaml (voice.enabled: false)")
        hub.set_voice_status(enabled=False, built=False, mic_ok=False, wakeword_active=False)
        return

    handle_text = lambda text: backend.ask(text, actor="voice")  # noqa: E731

    loop: Any = None
    mic_ok = True
    try:
        from friday.voice.conversation import build_conversation_loop

        loop = build_conversation_loop(handle_text=handle_text, on_state=hub.on_voice_state)
        HOTKEYS.bind(CFG.voice.activation_hotkey, loop.activate_from_hotkey)
        _speak_late_results(loop.announce_late)
    except Exception:
        log.exception(
            "conversation/wake-word subsystem failed to initialize; "
            "falling back to hotkey-only voice (%s)", CFG.voice.activation_hotkey,
        )
        loop = None
        try:
            from friday.voice import build_voice_session

            session = build_voice_session(handle_text=handle_text, on_state=hub.on_voice_state)
            HOTKEYS.bind(CFG.voice.activation_hotkey, session.activate)
            _speak_late_results(session.announce)
            _conversation_loop = None
        except Exception:
            log.exception("voice subsystem failed to initialize entirely; continuing without it")
            mic_ok = False
            hub.set_voice_status(enabled=True, built=False, mic_ok=False, wakeword_active=False)
            return

    _conversation_loop = loop

    from friday.voice import warm_up

    warm_up()  # pre-open the mic stream in the background; never blocks startup

    wakeword_active = False
    if loop is not None:
        if CFG.voice.wakeword.enabled:
            try:
                loop.start()
                wakeword_active = True
                log.info(
                    "wake-word listening started (model=%s, threshold=%.2f) — "
                    "say the wake phrase, or press %s",
                    CFG.voice.wakeword.model, CFG.voice.wakeword.threshold,
                    CFG.voice.activation_hotkey,
                )
            except Exception:
                log.exception("wake-word listening failed to start; %s still works", CFG.voice.activation_hotkey)
        else:
            log.info("wake-word listening disabled in config.yaml (voice.wakeword.enabled: false)")

    hub.set_voice_status(enabled=True, built=True, mic_ok=mic_ok, wakeword_active=wakeword_active)


def _mic_activate() -> None:
    if _conversation_loop is not None:
        _conversation_loop.activate_from_hotkey()


def _await_backend_stop(backend: Any, timeout_s: float) -> bool:
    """After the window is gone, give the backend a bounded chance to finish what it was doing (its `stopped` event is
    set when its loop thread really ends). True only when it did; otherwise the process exits anyway."""
    stopped = getattr(backend, "stopped", None)
    if stopped is None:
        return False
    if stopped.wait(timeout_s):
        return True
    log.warning("backend still finishing work after %.0fs; exiting without waiting further", timeout_s)
    return False


def _quit(app: QApplication, backend: Backend, hub: GuiStateHub, window: MainWindow, tray: Any) -> None:
    """Tray-Quit handler. The steps are independent, so each is attempted even if an earlier one raised (a Qt slot
    that raises is only printed: without this, one failure left the lock held, the backend running and the app open).
    A failure is logged with its step name; it does not claim anything stopped. Order is unchanged; `app.quit()` is last."""
    log.info("shutting down FRIDAY GUI")

    def _stop_mic() -> None:
        from friday.voice.capture import shutdown_mic_streams

        shutdown_mic_streams()

    steps: list[tuple[str, Any]] = [("window", window.shutdown)]
    if _conversation_loop is not None:
        steps.append(("voice loop", _conversation_loop.stop))
    if CFG.voice.enabled:
        steps.append(("mic streams", _stop_mic))
    steps += [
        ("hotkeys", HOTKEYS.stop),
        ("state hub", hub.stop),
        ("backend", backend.shutdown),
        ("tray", tray.hide),
        ("single-instance lock", single_instance.release),
        ("app quit", app.quit),
    ]
    for name, step in steps:
        try:
            step()
        except Exception:
            log.exception("shutdown step failed: %s (continuing with the rest)", name)


def run() -> None:
    if not single_instance.acquire():
        log.warning("another FRIDAY GUI instance is already running — exiting")
        return

    app = QApplication(sys.argv)
    from . import theme

    app.setStyleSheet(theme.QSS)
    app.setQuitOnLastWindowClosed(False)

    backend = Backend()
    hub = GuiStateHub(backend)
    hub.start()

    window = MainWindow(backend, hub)
    window.set_mic_activate(_mic_activate)
    if not CFG.gui.start_minimized:
        window.show()

    backend.start_async()  # non-blocking: brain warms on its own thread

    # Every hotkey must be bound before HOTKEYS.start() — its message-pump
    # thread registers `_pending` exactly once, at thread start (see
    # friday/hotkey.py's `_loop`); anything bound afterward is silently
    # dropped. `_start_voice` only *constructs* the voice pipeline here
    # (STT/TTS/wake-word load lazily — see friday/voice/stt.py) and does not
    # wait on `backend.ready`, so this stays fast, matching how
    # `friday/desktop.py` (superseded) always sequenced it.
    HOTKEYS.bind(HOTKEY, window.toggle_visibility)
    if CFG.permissions.stop_hotkey:
        # Phase 28.0: emergency stop for a running goal. wait=False: the hotkey thread must
        # never block on the backend loop.
        HOTKEYS.bind(CFG.permissions.stop_hotkey, lambda: backend.stop(source="hotkey", wait=False))
    _start_voice(backend, hub)
    HOTKEYS.start()
    HOTKEYS.wait_ready(timeout=2.0)

    ready_timer = QTimer()

    def poll_ready() -> None:
        window.apply_boot_stages(backend.stages)
        if backend.ready.is_set():
            ready_timer.stop()
            hub.set_backend_ready(not backend.error)
            hub.backendReady.emit(not backend.error)

    ready_timer.timeout.connect(poll_ready)
    ready_timer.start(75)

    tray = build_tray(
        on_toggle_window=window.toggle_visibility,
        on_quit=lambda: _quit(app, backend, hub, window, tray),
    )
    tray.show()

    log.info("FRIDAY GUI is live — press %s for the command bar", HOTKEY)
    app.exec()
    if backend.shutdown_requested:  # the GUI is closed; this does not hold up any window
        _await_backend_stop(backend, backend.SHUTDOWN_DRAIN_S + 2.0)
