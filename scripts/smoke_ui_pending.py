"""Phase 40 -- the GUI shows a still-running request as neither an answer nor a failure, and never resubmits it.

Offscreen Qt (no display, microphone, model or Ollama) with a fake Backend. Covers the real `ResponseArea` and
`CommandBar` widgets: a normal reply, a failure and a `still_running` reply (Phase 39 `Backend.ask`) render
differently; the pending state does not leak into the next exchange; the typed path sends it down its own signal; and
one submission makes exactly one `Backend.ask` call.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from phase21_common import check, finish, scenario  # noqa: E402

from friday.gui import theme  # noqa: E402
from friday.gui.command_bar import CommandBar  # noqa: E402
from friday.gui.response_area import ResponseArea  # noqa: E402
from friday.registry import SkillResult  # noqa: E402

PENDING = SkillResult(speech="That is still running, and I haven't heard back from it yet. please don't repeat it.", ok=False,
                      data={"still_running": True, "repeat": False, "stoppable": False, "request_id": "req-A"})


class FakeBackend:
    def __init__(self) -> None:
        import threading

        self.asked: list[tuple[str, str]] = []
        self.reply = SkillResult(speech="all good", ok=True)
        self.error = False
        self.ready = threading.Event()
        self.ready.set()
        self.stages = ["memory", "registry:0", "brain"]

    def observe(self, **kwargs):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_exception(RuntimeError("no desktop_observer in smoke test"))
        return future

    def ping_llm(self):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_result(False)
        return future

    def shutdown(self) -> None:
        pass

    def ask(self, text: str, *, actor: str = "text"):
        self.asked.append((text, actor))
        return self.reply


def pump(app, until, seconds: float = 3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def main() -> int:
    t0 = time.perf_counter()
    app = QApplication.instance() or QApplication(sys.argv)

    scenario("A: ResponseArea renders pending differently from an answer and from a failure")
    ra = ResponseArea()
    ra.set_exchange("open the report", "Opened.", True)
    check("A1 an answer: plain header, default colour", ra._header.text() == "FRIDAY" and ra._response.styleSheet() == "")
    ra.set_exchange("open the report", "I couldn't.", False)
    check("A2 a failure: warning colour", theme.WARNING in ra._response.styleSheet() and ra._header.text() == "FRIDAY")
    ra.set_pending("organise my downloads", PENDING.speech)
    check("A3 pending: its own header", "STILL RUNNING" in ra._header.text(), ra._header.text())
    check("A4 pending: not the failure colour, and the text is shown whole",
          theme.WARNING not in ra._response.styleSheet() and theme.ERROR not in ra._response.styleSheet() and ra._response.text() == PENDING.speech,
          ra._response.styleSheet())
    check("A5 pending: the request that is running stays on screen", "organise my downloads" in ra._user_echo.text())
    ra.set_exchange("what time is it", "It's 3 pm.", True)
    check("A6 the next exchange clears the pending header and colour", ra._header.text() == "FRIDAY" and ra._response.styleSheet() == "")

    scenario("B: the voice path (same widget) honours the pending flag")
    ra.on_voice_raw_state("executing", {"text": "tidy up"})
    ra.on_voice_raw_state("speaking", {"text": PENDING.speech, "ok": False, "pending": True})
    check("B1 a pending spoken reply is shown as pending, not as a failure", "STILL RUNNING" in ra._header.text() and theme.WARNING not in ra._response.styleSheet())
    ra.on_voice_raw_state("executing", {"text": "next"})
    check("B2 a new command resets it", ra._header.text() == "FRIDAY")
    ra.on_voice_raw_state("speaking", {"text": "I couldn't.", "ok": False})
    check("B3 an ordinary failed reply (no flag) is still a failure", theme.WARNING in ra._response.styleSheet() and ra._header.text() == "FRIDAY")
    ra.on_voice_raw_state("speaking", {"text": PENDING.speech, "ok": False, "pending": True})
    ra.on_voice_raw_state("cancelled", {})
    check("B4 'cancelled' replaces the text and the pending header goes with it", ra._header.text() == "FRIDAY" and ra._response.text() == "Cancelled.")

    scenario("C: CommandBar sends a still-running reply down its own signal and asks exactly once")
    backend = FakeBackend()
    bar = CommandBar(backend)  # type: ignore[arg-type]
    ready: list[tuple] = []
    pending: list[tuple] = []
    bar.exchangeReady.connect(lambda *a: ready.append(a))
    bar.exchangePending.connect(lambda *a: pending.append(a))

    bar._run_ask("hello", actor="text")
    pump(app, lambda: bool(ready or pending))
    check("C1 a normal reply goes to exchangeReady, unchanged", ready == [("hello", "all good", True)] and not pending, f"{ready} {pending}")

    backend.reply = PENDING
    ready.clear()
    bar._run_ask("organise my downloads", actor="text")
    pump(app, lambda: bool(ready or pending))
    check("C2 a still-running reply goes to exchangePending, not exchangeReady (so it is not shown as a failure)",
          pending == [("organise my downloads", PENDING.speech, "req-A")] and not ready, f"{ready} {pending}")
    check("C3 one submission made exactly one Backend.ask call (nothing resubmits it)", backend.asked == [("hello", "text"), ("organise my downloads", "text")], str(backend.asked))

    backend.reply = SkillResult(speech="no", ok=False)
    pending.clear()
    bar._run_ask("do a failing thing", actor="text")
    pump(app, lambda: bool(ready or pending))
    check("C4 an ordinary failure still goes to exchangeReady with ok=False", ready == [("do a failing thing", "no", False)] and not pending, f"{ready} {pending}")

    class Bare:  # a result with no `data` at all (the older fake in smoke_gui.py)
        speech, ok = "ok", True

    backend.reply = Bare()
    ready.clear()
    bar._run_ask("bare", actor="text")
    pump(app, lambda: bool(ready or pending))
    check("C5 a result without .data is handled as a normal reply", ready == [("bare", "ok", True)], f"{ready} {pending}")

    scenario("D: Phase 41 -- a late result replaces ITS OWN pending text, once, and never another request's")
    ra = ResponseArea()
    late_a = {"request_id": "req-A", "utterance": "organise my downloads", "speech": "Moved 12 files.", "ok": True, "error": False, "cancelled": False}
    ra.set_pending("organise my downloads", PENDING.speech, "req-A")
    ra.set_late(late_a)
    check("D1 the pending text for request A is replaced by A's real outcome, as a normal exchange",
          ra._response.text() == "Moved 12 files." and ra._header.text() == "FRIDAY" and "organise my downloads" in ra._user_echo.text() and ra._response.styleSheet() == "",
          f"{ra._header.text()!r} {ra._response.text()!r}")
    ra.set_exchange("what time is it", "It is 3 pm.", True)
    ra.set_late(late_a)
    check("D2 the same completion is never shown twice (a repeat does not touch the screen)", ra._response.text() == "It is 3 pm." and not ra._late.isVisibleTo(ra) and ra._late.text() == "", f"{ra._response.text()!r} {ra._late.text()!r}")

    ra = ResponseArea()
    ra.set_pending("organise my downloads", PENDING.speech, "req-A")
    ra.set_exchange("what time is it", "It is 3 pm.", True)
    ra.set_late(late_a)
    check("D3 A's result arrives while the screen shows request B: B's exchange is untouched",
          ra._response.text() == "It is 3 pm." and "what time is it" in ra._user_echo.text() and ra._header.text() == "FRIDAY", f"{ra._response.text()!r} {ra._user_echo.text()!r}")
    check("D4 ...and A's outcome appears on its own line, labelled with A's words",
          ra._late.isVisibleTo(ra) and "organise my downloads" in ra._late.text() and "Moved 12 files." in ra._late.text(), ra._late.text())
    ra.set_exchange("next thing", "ok", True)
    check("D5 the next exchange clears the late line", not ra._late.isVisibleTo(ra))

    ra = ResponseArea()
    ra.set_pending("do A", PENDING.speech, "req-A")
    ra.set_pending("do B", "B is still running too", "req-B")
    ra.set_late({"request_id": "req-A", "utterance": "do A", "speech": "A done.", "ok": True})
    check("D6 A finishes while B is the one on screen as pending: B's pending text and header are kept",
          "STILL RUNNING" in ra._header.text() and ra._response.text() == "B is still running too" and "do B" in ra._user_echo.text() and "A done." in ra._late.text(),
          f"{ra._header.text()!r} {ra._response.text()!r}")
    ra.set_late({"request_id": "req-B", "utterance": "do B", "speech": "B done.", "ok": True})
    check("D7 then B finishes: it replaces its own pending text", ra._response.text() == "B done." and ra._header.text() == "FRIDAY" and "do B" in ra._user_echo.text(), f"{ra._response.text()!r}")

    ra = ResponseArea()
    ra.set_pending("risky", PENDING.speech, "req-E")
    ra.set_late({"request_id": "req-E", "utterance": "risky", "ok": False, "error": True,
                 "speech": "The earlier request ended with an error (RuntimeError). Part of it may already have run, so check before repeating it."})
    check("D8 a late error is shown as a failure (warning colour) and the words say part of it may have run",
          theme.WARNING in ra._response.styleSheet() and "may already have run" in ra._response.text() and "STILL RUNNING" not in ra._header.text(), ra._response.styleSheet())
    ra.set_pending("risky two", PENDING.speech, "req-F")
    ra.set_exchange("something else", "fine", True)
    ra.set_late({"request_id": "req-F", "utterance": "risky two", "ok": False, "speech": "I could not do it."})
    check("D9 a late failure on the side line is also a failure colour, never a success presentation", theme.WARNING in ra._late.styleSheet() and ra._response.text() == "fine", ra._late.styleSheet())

    ra = ResponseArea()
    ra.on_voice_raw_state("executing", {"text": "tidy up"})
    ra.on_voice_raw_state("speaking", {"text": PENDING.speech, "ok": False, "pending": True, "request_id": "req-V"})
    ra.set_late({"request_id": "req-V", "utterance": "tidy up", "speech": "Tidied.", "ok": True})
    check("D10 a voice request's late result replaces its own pending text too", ra._response.text() == "Tidied." and ra._header.text() == "FRIDAY", ra._response.text())

    scenario("E: BUS -> GuiStateHub.lateResult -> MainWindow's ResponseArea (the real wiring)")
    import asyncio

    from friday.bus import BUS
    from friday.gui.main_window import MainWindow
    from friday.gui.state_hub import GuiStateHub

    backend = FakeBackend()
    hub = GuiStateHub(backend)  # type: ignore[arg-type]
    hub.start()
    got: list[dict] = []
    hub.lateResult.connect(got.append)
    window = MainWindow(backend, hub)  # type: ignore[arg-type]
    window._response_area.set_pending("wired request", PENDING.speech, "req-W")
    payload = dict(request_id="req-W", utterance="wired request", speech="Wired result.", ok=True, error=False, cancelled=False)
    asyncio.run(BUS.publish("backend.late_result", **payload))
    app.processEvents()
    check("E1 a backend.late_result event on the BUS reaches the hub as lateResult, unchanged", got == [payload], str(got))
    check("E2 and MainWindow shows it in the response area", window._response_area._response.text() == "Wired result." and "wired request" in window._response_area._user_echo.text(), window._response_area._response.text())
    asyncio.run(BUS.publish("backend.late_result", **payload))
    app.processEvents()
    check("E3 the same event delivered again is not shown again", window._response_area._response.text() == "Wired result." and not window._response_area._late.isVisibleTo(window._response_area), window._response_area._late.text())
    window.close()
    hub.stop()

    return finish("Phase 40/41 -- UI shows a still-running request and its late result", time.perf_counter() - t0, min_assertions=28, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
