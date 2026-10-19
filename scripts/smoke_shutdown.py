"""Phase 45 -- one failing shutdown step must not stop the independent cleanup steps after it.

`friday.gui.app._quit` is the tray-Quit handler. Each step is replaced by a recording fake that can be told to raise;
no window, microphone, hotkey thread, backend loop or database is involved.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

import friday.gui.app as app_mod  # noqa: E402
import friday.voice.capture as capture_mod  # noqa: E402

STEPS = ["window", "loop", "mic", "hotkeys", "hub", "backend", "tray", "lock", "quit"]
SECRET = "SECRET-REQUEST-TEXT-do-not-log"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def run_quit(fail: set[str], *, calls: int = 1, voice: bool = True):
    """Run `_quit` with every step faked. Returns (order of steps attempted, raised exception or None)."""
    order: list[str] = []

    def step(name: str):
        def _do(*_a, **_k):
            order.append(name)
            if name in fail:
                raise RuntimeError(f"{name} blew up {SECRET}")
        return _do

    window = SimpleNamespace(shutdown=step("window"))
    loop = SimpleNamespace(stop=step("loop"))
    hotkeys = SimpleNamespace(stop=step("hotkeys"))
    hub = SimpleNamespace(stop=step("hub"))
    backend = SimpleNamespace(shutdown=step("backend"))
    tray = SimpleNamespace(hide=step("tray"))
    app = SimpleNamespace(quit=step("quit"))
    lock = SimpleNamespace(release=step("lock"))

    saved = (app_mod._conversation_loop, app_mod.HOTKEYS, app_mod.single_instance, app_mod.CFG.voice.enabled,
             capture_mod.shutdown_mic_streams)
    app_mod._conversation_loop = loop
    app_mod.HOTKEYS = hotkeys
    app_mod.single_instance = lock
    app_mod.CFG.voice.enabled = voice
    capture_mod.shutdown_mic_streams = step("mic")
    raised = None
    try:
        for _ in range(calls):
            try:
                app_mod._quit(app, backend, hub, window, tray)
            except Exception as exc:  # noqa: BLE001 - the test records what escaped
                raised = exc
    finally:
        (app_mod._conversation_loop, app_mod.HOTKEYS, app_mod.single_instance, app_mod.CFG.voice.enabled,
         capture_mod.shutdown_mic_streams) = saved
    return order, raised


def main() -> int:
    t0 = time.perf_counter()
    cap = Capture()
    logging.getLogger().addHandler(cap)

    scenario("A. the normal path runs every step once, in order")
    order, raised = run_quit(set())
    check("A1 all nine steps, in the original order, nothing raised", order == STEPS and raised is None, str(order))

    scenario("B. a failure at any one step does not stop the others")
    for bad in STEPS[:-1]:
        cap.lines.clear()
        order, raised = run_quit({bad})
        later_ran = order == STEPS
        check(f"B-{bad}: every step is still attempted, in order, and the app is told to quit", later_ran, str(order))
        check(f"B-{bad}: the failure is logged with the step named (not silent)",
              any(bad in ln for ln in cap.lines),
              str(cap.lines))

    scenario("C. several failures, quitting twice, and quit itself failing")
    order, _ = run_quit({"window", "hub", "backend", "lock"})
    check("C1 four failures: all nine steps still attempted", order == STEPS, str(order))
    order, raised = run_quit(set(), calls=2)
    check("C2 a second Quit click re-runs the steps without raising", order == STEPS + STEPS and raised is None, str(order))
    order, raised = run_quit({"quit"})
    check("C3 if app.quit() itself fails nothing else is skipped (it is last)", order == STEPS, str(order))
    order, _ = run_quit({"window"}, voice=False)
    check("C4 voice disabled: the mic step is skipped, the rest still run",
          order == [s for s in STEPS if s != "mic"], str(order))

    logging.getLogger().removeHandler(cap)
    return finish("Phase 45 -- shutdown cleanup", time.perf_counter() - t0, min_assertions=21, min_scenarios=3)


if __name__ == "__main__":
    sys.exit(main())
