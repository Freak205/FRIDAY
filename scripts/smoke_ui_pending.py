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
                      data={"still_running": True, "repeat": False, "stoppable": False})


class FakeBackend:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.reply = SkillResult(speech="all good", ok=True)

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
          pending == [("organise my downloads", PENDING.speech)] and not ready, f"{ready} {pending}")
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

    return finish("Phase 40 -- UI shows a still-running request", time.perf_counter() - t0, min_assertions=15, min_scenarios=3)


if __name__ == "__main__":
    sys.exit(main())
