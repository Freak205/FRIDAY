"""Real-machine validation of FRIDAY's UI-automation primitives (`friday/skills/ui.py`).

`scripts/smoke_ui_automation.py` fakes both `uiautomation` and `friday.winput`,
so until this suite existed the real accessibility calls and real SendInput
sequences behind ui.click/fill/right_click/drag/scroll had never executed. This
suite drives the REAL skills against REAL windows and asserts on what those
windows actually received, not on what the skill reported:

  * a disposable WinForms window (`scripts/live_ui_harness.ps1`, real Win32
    controls, real UIA proxy, real OLE drag-drop) that logs every event it gets
  * real Windows Calculator (a WinUI app — a different UIA provider)

It is live, not deterministic: it needs an unlocked interactive desktop and
moves the real mouse for a few seconds. It never acts on a window it didn't
open. Before any skill call, a guard checks that the skill's own window
resolution lands on a test-owned window AND that every screen point the call
could actuate is covered by a test-owned window; if not, that call is skipped
and counted as a failure — so a regression here can misfire only onto the
test's own windows, never onto yours.

Scenarios:
  A  coordinate space: after uiautomation loads, SendInput's screen metrics and
     UIA's desktop rectangle agree (no DPI-virtualization skew)
  B  ui.inspect reads the harness's real accessibility tree by name
  C  ui.click actuates the named button (counter increments, read back via UIA)
  D  ui.fill sets the named field; a second fill replaces, never appends
  E  ui.right_click opens the context menu and is never a left-click/invoke
  F  ui.drag performs a real OLE drag-and-drop between two named controls, 5x
  G  ui.scroll scrolls the window under its center, down then back up
  H  real Calculator: ui.click 7 + 3 = reads back "Display is 10"
  I  target covered by another (normal) window: all five primitives reach the
     target — never the covering window, never "ok" without the target hit
  J  target under an always-on-top window it can't be raised above: click/fill
     still reach it through its own accessibility pattern; right_click/drag/
     scroll (synthetic input only) fail closed with nothing actuated anywhere
  K  a named control with no on-screen area and no invoke pattern (a real
     zero-size rect: Calculator's "System" item): fail closed, nothing at
     screen (0,0) actuated
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

import win32con  # noqa: E402
import win32gui  # noqa: E402

from friday import winput  # noqa: E402
from friday.skills import ui  # noqa: E402
from friday.skills.apps import _focus_hwnd, _windows  # noqa: E402

HARNESS = ROOT / "scripts" / "live_ui_harness.ps1"
CREATE_NO_WINDOW = 0x08000000
GA_ROOT = 2
HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
SWP_FLAGS = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE

TARGET = "UIHarness Target Alpha"
COVER = "UIHarness Occluder Zulu"
SHIELD = "UIHarness Corner Shield"


class Harness:
    def __init__(self, title: str, tag: str, *, topmost: bool, x: int = 100, y: int = 100) -> None:
        self.title, self.tag, self.topmost, self.x, self.y = title, tag, topmost, x, y
        self.log = Path(tempfile.gettempdir()) / f"friday_ui_live_{tag}.log"
        self.proc: subprocess.Popen | None = None
        self.hwnd = 0

    def start(self) -> bool:
        self.log.write_text("", encoding="utf-8")
        args = [
            "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(HARNESS), "-Title", self.title, "-Log", str(self.log), "-Tag", self.tag,
            "-X", str(self.x), "-Y", str(self.y), "-LifetimeSec", "150",
        ]
        if self.topmost:
            args.append("-TopMost")
        self.proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        if not wait_for(lambda: "ready" in self.events(), 20):
            return False
        mine = [w for w in _windows() if w["pid"] == self.proc.pid and w["title"] == self.title]
        self.hwnd = mine[0]["hwnd"] if mine else 0
        return bool(self.hwnd)

    def events(self) -> list[str]:
        try:
            text = self.log.read_text(encoding="utf-8")
        except OSError:
            return []
        prefix = self.tag + "|"
        return [ln[len(prefix):] for ln in text.splitlines() if ln.startswith(prefix)]

    def count(self, prefix: str) -> int:
        return sum(1 for e in self.events() if e.startswith(prefix))

    def raise_to_top(self) -> None:
        if self.topmost:
            win32gui.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            return
        # A no-activate z-order change can't go above the foreground window; activate instead.
        try:
            _focus_hwnd(self.hwnd)
        except Exception:
            win32gui.SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_FLAGS)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def wait_for(pred, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def root_at(x: int, y: int) -> int:
    hit = win32gui.WindowFromPoint((x, y))
    return win32gui.GetAncestor(hit, GA_ROOT) if hit else 0


def guard(app: str, labels: list[str], allowed: set[int], *, whole_window: bool = False) -> bool:
    """True only if calling a ui.* skill for these labels can touch nothing but test-owned windows."""
    win = ui._target_window(app)
    if win is None or win.NativeWindowHandle not in allowed:
        return check(f"GUARD: '{app}' resolves to a test-owned window", False,
                     f"resolved to {getattr(win, 'NativeWindowHandle', None)}, allowed {sorted(allowed)}")
    points = []
    if whole_window:
        r = win.BoundingRectangle
        points.append(((r.left + r.right) // 2, (r.top + r.bottom) // 2))
    for label in labels:
        node, _ = ui._find_control(win, label)
        if node is None:
            return check(f"GUARD: '{label}' is findable in '{app}'", False)
        points.append(tuple(node["center"]))
    stray = [(p, root_at(*p)) for p in points if root_at(*p) not in allowed]
    if stray:
        return check(f"GUARD: every point for {labels} is covered by a test-owned window", False, str(stray))
    return True


def center_of(app: str, label: str) -> tuple[int, int] | None:
    win = ui._target_window(app)
    node = ui._find_control(win, label)[0] if win is not None else None
    return tuple(node["center"]) if node else None


def uia_value(hwnd: int, name: str) -> str | None:
    try:
        return ui._auto().ControlFromHandle(hwnd).EditControl(Name=name).GetValuePattern().Value
    except Exception:
        return None


def inspect_text(app: str, prefix: str, depth: int = 8) -> str | None:
    r = ui.inspect(app=app, depth=depth)
    for node in r.data.get("controls", []):
        if node["name"].startswith(prefix):
            return node["name"]
    return None


def main() -> int:
    t0 = time.perf_counter()
    auto = ui._auto()

    pre = {w["title"] for w in _windows()}
    for title in (TARGET, COVER, SHIELD):
        if title in pre:
            print(f"ABORT: a window titled {title!r} already exists")
            return 1

    target = Harness(TARGET, "T", topmost=True)
    cover = None
    calc_hwnd = 0
    try:
        scenario("A: SendInput and UIA share one coordinate space")
        sw, sh = winput.screen_size()
        desk = auto.GetRootControl().BoundingRectangle
        check("winput's SendInput screen size == UIA desktop rect (no DPI-virtualization skew)",
              (sw, sh) == (desk.right - desk.left, desk.bottom - desk.top), f"{(sw, sh)} vs {desk}")

        scenario("B: ui.inspect reads the real accessibility tree")
        check("harness window came up and was found by pid+title", target.start(), target.log.read_text(encoding="utf-8"))
        target.raise_to_top()
        mine = {target.hwnd}
        r = ui.inspect(app=TARGET)
        names = {n["name"] for n in r.data.get("controls", [])}
        check("inspect ok", r.ok, r.speech)
        for want in ("Increment Counter", "Harness Input", "Context Target", "Drag Source", "Drop Target"):
            check(f"inspect sees '{want}'", want in names)

        scenario("C: ui.click actuates the named button")
        if guard(TARGET, ["Increment Counter"], mine):
            r = ui.click(label="Increment Counter", app=TARGET)
            check("click reports ok", r.ok, r.speech)
            check("the window really received exactly one click", wait_for(lambda: target.count("click:") == 1, 3), str(target.events()))
            check("UIA read-back shows 'Count: 1'", wait_for(lambda: inspect_text(TARGET, "Count:") == "Count: 1", 3))
            ui.click(label="Increment Counter", app=TARGET)
            check("a second click is a second real click", wait_for(lambda: target.count("click:") == 2, 3), str(target.events()))

        scenario("D: ui.fill sets the named field; refilling replaces")
        if guard(TARGET, ["Harness Input"], mine):
            r = ui.fill(field="Harness Input", text="hello from friday", app=TARGET)
            check("fill reports ok", r.ok, r.speech)
            check("the field's real value is the text", wait_for(lambda: uia_value(target.hwnd, "Harness Input") == "hello from friday", 3),
                  repr(uia_value(target.hwnd, "Harness Input")))
            ui.fill(field="Harness Input", text="second value", app=TARGET)
            check("a second fill replaces rather than appends", wait_for(lambda: uia_value(target.hwnd, "Harness Input") == "second value", 3),
                  repr(uia_value(target.hwnd, "Harness Input")))

        scenario("E: ui.right_click opens the context menu, never a left click")
        if guard(TARGET, ["Context Target", "Increment Counter"], mine):
            r = ui.right_click(label="Context Target", app=TARGET)
            check("right_click reports ok", r.ok, r.speech)
            check("the context menu really opened", wait_for(lambda: target.count("context_opened") == 1, 3), str(target.events()))
            before = target.count("click:")
            time.sleep(0.8)  # let the harness auto-close its menu
            ui.right_click(label="Increment Counter", app=TARGET)
            time.sleep(0.6)
            check("right-clicking a button does not invoke it", target.count("click:") == before, str(target.events()))

        scenario("F: ui.drag performs a real drag-and-drop (5 in a row)")
        if guard(TARGET, ["Drag Source", "Drop Target"], mine):
            oks, delivered = [], 0
            for i in range(5):
                r = ui.drag(source="Drag Source", target="Drop Target", app=TARGET)
                oks.append(r.ok)
                if wait_for(lambda: target.count("dropped:friday-payload") == i + 1, 3):
                    delivered += 1
                else:
                    print("     drag", i, "not delivered; events:", target.events()[-6:])
            check("every drag reports ok", all(oks), str(oks))
            check("the drop target really received all 5 payloads", delivered == 5, f"{delivered}/5")

        scenario("G: ui.scroll scrolls the window under its center")
        if guard(TARGET, [], mine, whole_window=True):
            r = ui.scroll(direction="down", amount=3, app=TARGET)
            check("scroll reports ok", r.ok, r.speech)
            tops = lambda: [int(e.split(":")[1]) for e in target.events() if e.startswith("top:")]  # noqa: E731
            check("the list really scrolled down", wait_for(lambda: bool(tops()) and tops()[-1] > 0, 3), str(tops()))
            down_at = tops()[-1] if tops() else 0
            ui.scroll(direction="up", amount=3, app=TARGET)
            check("scrolling up really moves back up", wait_for(lambda: bool(tops()) and tops()[-1] < down_at, 3), str(tops()))

        scenario("H: real Calculator — ui.click 7 + 3 =")
        if any(w["title"] == "Calculator" for w in _windows()):
            check("Calculator not already open (won't touch a user's instance)", False, "skipped")
        else:
            subprocess.Popen(["calc.exe"])
            wait_for(lambda: any(w["title"] == "Calculator" for w in _windows()), 15, 0.3)
            hits = [w for w in _windows() if w["title"] == "Calculator"]
            calc_hwnd = hits[0]["hwnd"] if hits else 0
            check("Calculator launched", bool(calc_hwnd))
            if calc_hwnd:
                time.sleep(1.5)
                win32gui.SetWindowPos(calc_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
                keys = ["Clear", "Seven", "Plus", "Three", "Equals"]
                if guard("Calculator", keys, {calc_hwnd}):
                    oks = [ui.click(label=k, app="Calculator").ok for k in keys]
                    check("every click reports ok", all(oks), str(oks))
                    check("the display really reads 10",
                          wait_for(lambda: inspect_text("Calculator", "Display is", depth=14) == "Display is 10", 3),
                          str(inspect_text("Calculator", "Display is", depth=14)))

        scenario("I: target covered by a normal window — act on the target or fail, never the cover")
        target.stop()
        target = Harness(TARGET, "T", topmost=False)
        cover = Harness(COVER, "C", topmost=False)
        both_up = target.start() and cover.start()
        check("target and cover windows came up", both_up)
        if both_up:
            target.raise_to_top()
            cover.raise_to_top()
            ours = {target.hwnd, cover.hwnd}
            covered = lambda: root_at(*(center_of(TARGET, "Increment Counter") or (0, 0))) == cover.hwnd  # noqa: E731
            check("precondition: the target's button is really under the cover", wait_for(covered, 3))
            if covered() and guard(TARGET, ["Increment Counter", "Harness Input", "Context Target", "Drag Source", "Drop Target"], ours, whole_window=True):
                cases = [
                    ("click", lambda: ui.click(label="Increment Counter", app=TARGET), "click:"),
                    ("fill", lambda: ui.fill(field="Harness Input", text="covered", app=TARGET), "text:"),
                    ("right_click", lambda: ui.right_click(label="Context Target", app=TARGET), "context_opened"),
                    ("drag", lambda: ui.drag(source="Drag Source", target="Drop Target", app=TARGET), "dropped:"),
                    ("scroll", lambda: ui.scroll(direction="down", amount=3, app=TARGET), "top:"),
                ]
                for name, call, evt in cases:
                    cover.raise_to_top()
                    if not check(f"{name}: precondition — the target is covered again", wait_for(covered, 3)):
                        continue
                    time.sleep(0.3)
                    t_before, c_before = target.count(evt), cover.count(evt)
                    r = call()
                    time.sleep(1.0)
                    hit_target = target.count(evt) > t_before
                    hit_cover = cover.count(evt) > c_before
                    check(f"{name}: the covering window received nothing", not hit_cover, str(cover.events()[-4:]))
                    check(f"{name}: reports ok only if the target really received it", (r.ok and hit_target) or not r.ok,
                          f"ok={r.ok} target_hit={hit_target} speech={r.speech!r}")
                    check(f"{name}: the target received it (a normal window can be raised)", hit_target,
                          f"ok={r.ok} speech={r.speech!r}")

        scenario("J: target under an always-on-top window it can't be raised above")
        cover.stop()
        cover = Harness(COVER, "C", topmost=True)
        if cover.start():
            target.raise_to_top()
            cover.raise_to_top()
            ours = {target.hwnd, cover.hwnd}
            if guard(TARGET, ["Increment Counter", "Harness Input", "Context Target", "Drag Source", "Drop Target"], ours, whole_window=True):
                # An accessibility pattern acts on the element itself, so a covered
                # target is still reached correctly; the cover must never be.
                for name, call, evt in [
                    ("click", lambda: ui.click(label="Increment Counter", app=TARGET), "click:"),
                    ("fill", lambda: ui.fill(field="Harness Input", text="pattern reaches target", app=TARGET), "text:"),
                ]:
                    cover.raise_to_top()
                    t_before, c_before = target.count(evt), cover.count(evt)
                    r = call()
                    time.sleep(1.0)
                    check(f"{name}: the always-on-top cover received nothing", cover.count(evt) == c_before, str(cover.events()[-3:]))
                    check(f"{name}: the target received it through its own accessibility pattern",
                          r.ok and target.count(evt) > t_before, f"ok={r.ok} speech={r.speech!r}")
                # Synthetic input can only go to whatever is on top, so these must refuse.
                for name, call, evt in [
                    ("right_click", lambda: ui.right_click(label="Context Target", app=TARGET), "context_opened"),
                    ("drag", lambda: ui.drag(source="Drag Source", target="Drop Target", app=TARGET), "dropped:"),
                    ("scroll", lambda: ui.scroll(direction="down", amount=3, app=TARGET), "top:"),
                ]:
                    cover.raise_to_top()
                    t_before, c_before = target.count(evt), cover.count(evt)
                    r = call()
                    time.sleep(1.0)
                    check(f"{name}: fails closed (ok=False)", not r.ok, r.speech)
                    check(f"{name}: ...the cover received nothing", cover.count(evt) == c_before, str(cover.events()[-3:]))
                    check(f"{name}: ...and neither did the target", target.count(evt) == t_before, str(target.events()[-3:]))
        else:
            check("always-on-top cover came up", False)

        scenario("K: a named control with no on-screen area fails closed")
        if calc_hwnd:
            # A zero-area rect's "center" is screen (0,0). A test-owned always-on-top
            # window is parked so its "Increment Counter" button covers that corner:
            # anything that acts at (0,0) lands on it (and is counted), never on
            # whatever the user has there.
            shield = Harness(SHIELD, "S", topmost=True, x=-20, y=-50)
            shield_up = shield.start()
            try:
                if shield_up:
                    shield.raise_to_top()
                win32gui.SetWindowPos(calc_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
                try:
                    item = auto.ControlFromHandle(calc_hwnd).MenuItemControl(Name="System")
                    rect = item.BoundingRectangle
                    zero_area = (rect.right - rect.left) <= 0 or (rect.bottom - rect.top) <= 0
                    no_invoke = item.GetInvokePattern() is None
                except Exception as exc:
                    zero_area, no_invoke, rect = False, False, exc
                check("precondition (raw UIA): Calculator exposes a 'System' item with a zero-area rect", zero_area, str(rect))
                check("precondition (raw UIA): ...and no Invoke pattern (so only synthetic input could 'click' it)", no_invoke)
                win = ui._target_window("Calculator")
                corner = auto.ControlFromPoint(0, 0)
                safe = (shield_up and win is not None and win.NativeWindowHandle == calc_hwnd
                        and root_at(0, 0) == shield.hwnd and corner is not None and corner.Name == "Increment Counter")
                check("GUARD: screen (0,0) is the test shield's own button", safe, str(getattr(corner, "Name", None)))
                if zero_area and no_invoke and safe:
                    before = shield.count("click:")
                    r = ui.click(label="System", app="Calculator")
                    time.sleep(1.0)
                    check("click on a zero-area control fails closed (ok=False)", not r.ok, r.speech)
                    check("...and nothing at screen (0,0) was actuated", shield.count("click:") == before, str(shield.events()[-3:]))
            finally:
                shield.stop()
        else:
            check("Calculator available for the zero-area probe", False, "scenario H did not launch it")
    finally:
        target.stop()
        if cover:
            cover.stop()
        if calc_hwnd and win32gui.IsWindow(calc_hwnd):
            win32gui.PostMessage(calc_hwnd, win32con.WM_CLOSE, 0, 0)

    return finish("UI automation — real-machine validation", time.perf_counter() - t0,
                  min_assertions=40, min_scenarios=11)


if __name__ == "__main__":
    raise SystemExit(main())
