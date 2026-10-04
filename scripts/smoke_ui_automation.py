"""Deterministic coverage for the UI Automation skills in `friday/skills/ui.py`
(ui.click/fill/right_click/drag/scroll/inspect), including the wrong-target guard.

This suite fakes the two real dependencies these skills call through
(`uiautomation`, via `friday.skills.ui._auto()`/`_target_window()`, and
`friday.winput`, the raw SendInput layer) plus the three Win32 lookups the guard
uses (`_bring_forward`, `_root_window_at`, `_foreground_root`), so it runs
headless and deterministically while exercising the REAL control matching and
REAL branch logic. The fakes mirror the real library's contract: pattern getters
return None when unsupported, Invoke()/SetValue() return a bool, and
ControlFromPoint returns whatever is topmost at a point — which need not be the
control that was matched by name. Real-machine behavior is covered separately
by `scripts/smoke_ui_live.py`.

This suite pins:

  A  ui.click: a control with its own Invoke pattern is invoked through it
  B  ui.click: no Invoke pattern -> a synthetic click at the control's center
  C  ui.click: an unmatched name fails cleanly, listing what IS visible
  D  ui.click: no window found fails cleanly
  E  ui.fill: the control's own SetValue pattern is used when available
  F  ui.fill: falls back to click + select-all + type when it isn't
  G  ui.right_click: a real synthetic right button, never the Invoke pattern
  H  ui.drag: drags from one matched control's center to another's
  I  ui.scroll: direction and sign both correct
  J  a pattern acts on the matched element itself, even when something else is
     on top of it (no screen point involved)
  K  synthetic input is refused when another window covers the control
  L  a control with no on-screen area is refused (never actuates screen (0,0))
  M  the hit-test accepts a point that lands on a child of the control
  N  ui.fill's fallback types nothing if the window didn't take keyboard focus
  O  right_click/drag/scroll refuse when covered; synthetic paths raise the
     window first, pattern paths never do
  P  ui.inspect's data carries no live control handles (JSON-safe)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import winput  # noqa: E402
from friday.skills import ui  # noqa: E402

WINDOW_HWND = 4242


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class Invokable:
    def __init__(self, log):
        self._log = log

    def Invoke(self):
        self._log.append("invoked")
        return True


class Settable:
    def __init__(self, log):
        self._log = log

    def SetValue(self, text):
        self._log.append(text)
        return True


class FakeControl:
    """Stands in for a real uiautomation control node."""

    def __init__(self, name, ctype, rect=(0, 0, 20, 20), children=None, enabled=True,
                 invoke_log=None, value_log=None, hwnd=0):
        self.Name = name
        self.ControlTypeName = ctype
        self.BoundingRectangle = FakeRect(*rect)
        self.IsEnabled = enabled
        self.NativeWindowHandle = hwnd
        self._children = children or []
        self._parent = None
        self._invoke = Invokable(invoke_log) if invoke_log is not None else None
        self._value = Settable(value_log) if value_log is not None else None
        for child in self._children:
            child._parent = self

    def GetChildren(self):
        return self._children

    def GetParentControl(self):
        return self._parent

    def GetInvokePattern(self):
        return self._invoke

    def GetValuePattern(self):
        return self._value

    @property
    def center(self):
        r = self.BoundingRectangle
        return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


def _contains(control, x, y):
    r = control.BoundingRectangle
    return r.left <= x < r.right and r.top <= y < r.bottom


class FakeAuto:
    """Stands in for the real `uiautomation` module, as returned by ui._auto()."""

    def __init__(self, root):
        self.root = root
        self.on_top: dict[tuple[int, int], FakeControl] = {}  # point -> a foreign control covering it
        self.point_calls = 0

    def ControlFromPoint(self, x, y):
        self.point_calls += 1
        if (x, y) in self.on_top:
            return self.on_top[(x, y)]
        hit = self.root if _contains(self.root, x, y) else None
        while hit is not None:
            deeper = next((c for c in hit.GetChildren() if _contains(c, x, y)), None)
            if deeper is None:
                break
            hit = deeper
        return hit

    @staticmethod
    def ControlsAreSame(a, b):
        return a is b


def build_window(invoke_log, value_log):
    save_btn = FakeControl("Save", "ButtonControl", rect=(0, 0, 20, 20), invoke_log=invoke_log)
    cancel_btn = FakeControl("Cancel", "ButtonControl", rect=(100, 0, 120, 20))
    search_field = FakeControl("Search box", "EditControl", rect=(0, 100, 40, 120), value_log=value_log)
    name_field = FakeControl("Name field", "EditControl", rect=(100, 100, 140, 120))
    item_a = FakeControl("Item A", "ListItemControl", rect=(0, 200, 20, 220))
    item_b = FakeControl("Item B", "ListItemControl", rect=(100, 200, 120, 220))
    ghost = FakeControl("Ghost Menu", "MenuItemControl", rect=(0, 0, 0, 0))  # real UIA reports these
    label = FakeControl("Help label", "TextControl", rect=(150, 0, 190, 20))
    help_btn = FakeControl("Help", "ButtonControl", rect=(140, 0, 200, 20), children=[label])
    root = FakeControl("Root Window", "WindowControl", rect=(0, 0, 220, 240), hwnd=WINDOW_HWND, children=[
        save_btn, cancel_btn, search_field, name_field, item_a, item_b, ghost, help_btn,
    ])
    return root, save_btn, cancel_btn, search_field, name_field, item_a, item_b, label


def main() -> int:
    t0 = time.perf_counter()

    invoke_log: list[str] = []
    setvalue_log: list[str] = []
    root, save_btn, cancel_btn, search_field, name_field, item_a, item_b, help_label = build_window(invoke_log, setvalue_log)
    fake_auto = FakeAuto(root)
    foreign = FakeControl("Someone Else's Button", "ButtonControl", rect=(0, 0, 1, 1))

    winput_calls: list[tuple] = []
    real = {n: getattr(winput, n) for n in ("click", "press", "type_text", "drag", "scroll", "move_mouse")}

    def record(name):
        def fn(*a, **kw):
            winput_calls.append((name, a, kw))
        return fn

    for name in real:
        setattr(winput, name, record(name))

    raised: list[int] = []
    env = {"root_at": WINDOW_HWND, "foreground": WINDOW_HWND}
    saved = {n: getattr(ui, n) for n in ("_target_window", "_auto", "_bring_forward", "_root_window_at",
                                          "_foreground_root", "_AIM_WAIT_S")}

    try:
        ui._target_window = lambda app="": root
        ui._auto = lambda: fake_auto
        ui._bring_forward = lambda window: raised.append(window.NativeWindowHandle)
        ui._root_window_at = lambda x, y: env["root_at"]
        ui._foreground_root = lambda: env["foreground"]
        ui._AIM_WAIT_S = 0

        scenario("A: ui.click invokes a control through its own Invoke pattern")
        r = ui.click(label="Save")
        check("click reports ok", r.ok, r.speech)
        check("the accessibility Invoke pattern was used", invoke_log == ["invoked"], invoke_log)
        check("...and NO synthetic click was sent", not any(c[0] == "click" for c in winput_calls), winput_calls)
        check("speech names the real control", "Save" in r.speech, r.speech)

        scenario("B: ui.click falls back to a synthetic click when there's no Invoke pattern")
        winput_calls.clear()
        r2 = ui.click(label="Cancel")
        check("click still reports ok", r2.ok, r2.speech)
        clicks = [c for c in winput_calls if c[0] == "click"]
        check("friday.winput.click was called as the fallback", len(clicks) == 1, winput_calls)
        check("...at the control's real center", clicks and clicks[0][1] == cancel_btn.center, clicks)

        scenario("C: ui.click on a control that doesn't exist fails cleanly")
        r3 = ui.click(label="Nonexistent Button")
        check("reports ok=False", not r3.ok, r3.speech)
        check("speech lists what IS actually visible", "Save" in r3.speech or "Cancel" in r3.speech, r3.speech)

        scenario("D: ui.click with no window found at all fails cleanly")
        ui._target_window = lambda app="": None
        r4 = ui.click(label="Save", app="some nonexistent window")
        check("reports ok=False", not r4.ok, r4.speech)
        check("speech explains no window was found", "window" in r4.speech.lower(), r4.speech)
        ui._target_window = lambda app="": root

        scenario("E: ui.fill uses the field's own SetValue pattern when available")
        winput_calls.clear()
        r5 = ui.fill(field="Search box", text="hello")
        check("fill reports ok", r5.ok, r5.speech)
        check("SetValue received the real text", setvalue_log == ["hello"], setvalue_log)
        check("...and no synthetic input occurred", not winput_calls, winput_calls)

        scenario("F: ui.fill falls back to click + select-all + type when there's no SetValue")
        winput_calls.clear()
        r6 = ui.fill(field="Name field", text="Ada")
        check("fill still reports ok", r6.ok, r6.speech)
        kinds = [c[0] for c in winput_calls]
        check("the fallback clicked the field, selected all, then typed", kinds == ["click", "press", "type_text"], kinds)
        check("...clicked the field's real center", winput_calls and winput_calls[0][1] == name_field.center, winput_calls)
        check("...selected all via ctrl+a first", len(winput_calls) > 1 and winput_calls[1][1] == ("ctrl+a",), winput_calls)
        check("...typed the real requested text", len(winput_calls) > 2 and winput_calls[2][1] == ("Ada",), winput_calls)

        scenario("G: ui.right_click always uses a real synthetic right-click")
        winput_calls.clear()
        r7 = ui.right_click(label="Save")
        check("right_click reports ok", r7.ok, r7.speech)
        rc = [c for c in winput_calls if c[0] == "click"]
        check("a real click was sent with button='right'", rc and rc[0][2].get("button") == "right", winput_calls)
        check("...at Save's real center", rc and rc[0][1] == save_btn.center, winput_calls)
        check("the Invoke pattern was NOT used for a right-click", invoke_log == ["invoked"], invoke_log)

        scenario("H: ui.drag drags from one matched control's center to another's")
        winput_calls.clear()
        r8 = ui.drag(source="Item A", target="Item B")
        check("drag reports ok", r8.ok, r8.speech)
        drags = [c for c in winput_calls if c[0] == "drag"]
        check("friday.winput.drag was called once", len(drags) == 1, winput_calls)
        check("...from Item A's center to Item B's center", drags and drags[0][1] == (*item_a.center, *item_b.center), drags)
        r8b = ui.drag(source="Item A", target="Does Not Exist")
        check("dragging onto a nonexistent target fails cleanly", not r8b.ok, r8b.speech)

        scenario("I: ui.scroll scrolls with the correct sign for direction")
        winput_calls.clear()
        r9 = ui.scroll(direction="down", amount=3)
        check("scroll down reports ok", r9.ok, r9.speech)
        scrolls = [c for c in winput_calls if c[0] == "scroll"]
        check("scroll amount is NEGATIVE for 'down'", scrolls and scrolls[0][1][0] == -3, scrolls)
        check("...after aiming at the window's center", ("move_mouse", root.center, {}) in winput_calls, winput_calls)
        winput_calls.clear()
        ui.scroll(direction="up", amount=5)
        scrolls_up = [c for c in winput_calls if c[0] == "scroll"]
        check("scroll amount is POSITIVE for 'up'", scrolls_up and scrolls_up[0][1][0] == 5, scrolls_up)

        scenario("J: a pattern acts on the matched element itself, whatever is on top of it")
        winput_calls.clear()
        invoke_log.clear()
        setvalue_log.clear()
        fake_auto.on_top = {save_btn.center: foreign, search_field.center: foreign}
        fake_auto.point_calls = 0
        r10 = ui.click(label="Save")
        r11 = ui.fill(field="Search box", text="still mine")
        check("click reports ok", r10.ok, r10.speech)
        check("...invoked the matched Save button itself", invoke_log == ["invoked"], invoke_log)
        check("fill set the matched field itself", setvalue_log == ["still mine"], setvalue_log)
        check("no screen point was consulted and no synthetic input sent", fake_auto.point_calls == 0 and not winput_calls,
              f"point_calls={fake_auto.point_calls} {winput_calls}")
        fake_auto.on_top = {}

        scenario("K: synthetic input is refused when another window covers the control")
        winput_calls.clear()
        fake_auto.on_top = {cancel_btn.center: foreign}
        r12 = ui.click(label="Cancel")
        check("click reports ok=False", not r12.ok, r12.speech)
        check("...says the control is covered", "covered" in r12.speech, r12.speech)
        check("...and sent no synthetic input at all", not winput_calls, winput_calls)
        check("...never claims it clicked", "Clicked" not in r12.speech, r12.speech)
        fake_auto.on_top = {}

        scenario("L: a control with no on-screen area is refused")
        winput_calls.clear()
        r13 = ui.click(label="Ghost Menu")
        check("click reports ok=False", not r13.ok, r13.speech)
        check("...says it isn't visible", "isn't visible" in r13.speech, r13.speech)
        check("...and never touched screen (0,0) or anywhere else", not winput_calls, winput_calls)
        r13b = ui.right_click(label="Ghost Menu")
        check("right_click on it is refused too", not r13b.ok and not winput_calls, r13b.speech)

        scenario("M: a point landing on a child of the control still counts as the control")
        winput_calls.clear()
        check("precondition: the Help button's center hits its inner label",
              fake_auto.ControlFromPoint(*ui._find_control(root, "Help", "button")[0]["center"]) is help_label)
        r14 = ui.click(label="Help", kind="button")
        check("click reports ok", r14.ok, r14.speech)
        check("...and the synthetic click was sent", [c[0] for c in winput_calls] == ["click"], winput_calls)

        scenario("N: ui.fill's fallback types nothing if the window didn't take keyboard focus")
        winput_calls.clear()
        env["foreground"] = 777
        r15 = ui.fill(field="Name field", text="must not be typed")
        env["foreground"] = WINDOW_HWND
        check("fill reports ok=False", not r15.ok, r15.speech)
        check("...no select-all and no typing happened", not any(c[0] in ("press", "type_text") for c in winput_calls), winput_calls)
        check("...says why", "keyboard focus" in r15.speech, r15.speech)

        scenario("O: right_click/drag/scroll refuse when covered; only synthetic paths raise the window")
        winput_calls.clear()
        raised.clear()
        fake_auto.on_top = {save_btn.center: foreign, item_b.center: foreign}
        r16 = ui.right_click(label="Save")
        r17 = ui.drag(source="Item A", target="Item B")
        fake_auto.on_top = {}
        env["root_at"] = 999
        r18 = ui.scroll(direction="down")
        env["root_at"] = WINDOW_HWND
        check("right_click refused", not r16.ok, r16.speech)
        check("drag refused (its drop point is covered)", not r17.ok, r17.speech)
        check("scroll refused (another window is at the center)", not r18.ok, r18.speech)
        check("...none of them sent any synthetic input", not winput_calls, winput_calls)
        check("each raised the target window before checking", raised == [WINDOW_HWND] * 3, raised)
        raised.clear()
        ui.click(label="Save")
        ui.fill(field="Search box", text="x")
        check("pattern paths (click via Invoke, fill via SetValue) never raise the window", raised == [], raised)

        scenario("P: ui.inspect's data carries no live control handles")
        r19 = ui.inspect()
        controls = r19.data.get("controls", [])
        check("inspect ok", r19.ok, r19.speech)
        check("no node exposes a private key", controls and all(not k.startswith("_") for n in controls for k in n), controls[:2])
        try:
            json.dumps(r19.data)
            serializable = True
        except TypeError:
            serializable = False
        check("the data is JSON-serializable", serializable)

    finally:
        for name, value in saved.items():
            setattr(ui, name, value)
        for name, fn in real.items():
            setattr(winput, name, fn)

    return finish("UI Automation — deterministic coverage", time.perf_counter() - t0, min_assertions=55, min_scenarios=16)


if __name__ == "__main__":
    sys.exit(main())
