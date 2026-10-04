"""UI Automation — the universal fallback for controlling any application.

Windows exposes an accessibility tree for essentially every app. Reading that
tree and invoking its controls is far more reliable than clicking pixels: it
survives window moves, resolution changes, and theme switches, and it tells us
what a control *is* rather than what it looks like.

Order of preference for controlling an app, most to least reliable:
  1. a real API                (skills in browser.py, comms.py, ...)
  2. the accessibility tree    (this module)
  3. synthetic input by coords (friday.winput)
  4. screenshot + vision       (P4)
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from friday.log import get
from friday.registry import SkillResult, skill
from friday.intent import CLICK_RULE

log = get(__name__)

# Control types worth reporting; the tree is full of layout noise otherwise.
_INTERESTING = {
    "ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "ListItemControl", "MenuItemControl", "TabItemControl",
    "HyperlinkControl", "TreeItemControl", "TextControl", "DocumentControl",
    "SliderControl", "SpinnerControl",
}

_MAX_NODES = 400  # hard cap: some apps have enormous trees

_AIM_TRIES = 3  # a restore animation or a passing tooltip can briefly hide a control
_AIM_WAIT_S = 0.15
_MAX_HIT_DEPTH = 64
_GA_ROOT = 2


def _auto():
    import uiautomation as auto

    # Keep failures fast — the default 10s per lookup is unusable in a REPL.
    auto.SetGlobalSearchTimeout(2)
    return auto


def _walk(control: Any, depth: int, max_depth: int, out: list[dict]) -> None:
    if depth > max_depth or len(out) >= _MAX_NODES:
        return
    try:
        children = control.GetChildren()
    except Exception:
        return

    for child in children:
        if len(out) >= _MAX_NODES:
            return
        try:
            type_name = child.ControlTypeName
            name = (child.Name or "").strip()
            if type_name in _INTERESTING and name:
                rect = child.BoundingRectangle
                out.append({
                    "name": name[:80],
                    "type": type_name.replace("Control", ""),
                    "enabled": bool(child.IsEnabled),
                    "depth": depth,
                    "center": [
                        (rect.left + rect.right) // 2,
                        (rect.top + rect.bottom) // 2,
                    ],
                    "_control": child,
                })
            _walk(child, depth + 1, max_depth, out)
        except Exception:
            continue


def _public(nodes: list[dict]) -> list[dict]:
    """Nodes without their live control handles, safe to return as tool data."""
    return [{k: v for k, v in n.items() if not k.startswith("_")} for n in nodes]


def _target_window(app: str):
    """Return the UIA element for `app`, or the foreground window."""
    auto = _auto()
    if not app or app.lower() in ("this", "that", "current", "active", "it"):
        return auto.GetForegroundControl()

    from rapidfuzz import fuzz, process, utils

    from friday.skills.apps import _windows

    wins = _windows()
    titles = {w["title"]: w for w in wins}
    # partial_ratio, not WRatio — see friday.skills.apps.focus_window for why.
    match = process.extractOne(
        app, titles.keys(), scorer=fuzz.partial_ratio,
        processor=utils.default_process, score_cutoff=80,
    )
    if match is None:
        return None
    return auto.ControlFromHandle(titles[match[0]]["hwnd"])


def _find_control(window: Any, label: str, kind: str = ""):
    """Best fuzzy match for a control by its accessible name."""
    from rapidfuzz import fuzz

    nodes: list[dict] = []
    _walk(window, 0, 12, nodes)
    if not nodes:
        return None, []

    wanted = kind.lower().replace("control", "") if kind else ""
    best, best_score = None, 0.0

    for node in nodes:
        if wanted and wanted not in node["type"].lower():
            continue
        score = fuzz.WRatio(label.lower(), node["name"].lower())
        if score > best_score:
            best, best_score = node, score

    return (best if best_score >= 65 else None), nodes


# Synthetic input lands on whatever is on screen at a point, not on the control we
# matched by name — so it is only sent after re-checking what is actually there.


def _bring_forward(window: Any) -> None:
    """Raise `window` so synthetic input can reach it; a failure is left to the hit-test."""
    try:
        import win32gui

        from friday.skills.apps import _focus_hwnd

        hwnd = window.NativeWindowHandle
        if hwnd and win32gui.GetForegroundWindow() != hwnd:
            _focus_hwnd(hwnd)
    except Exception:
        log.debug("could not bring the window forward", exc_info=True)


def _root_window_at(x: int, y: int) -> int:
    import win32gui

    hit = win32gui.WindowFromPoint((x, y))
    return win32gui.GetAncestor(hit, _GA_ROOT) if hit else 0


def _foreground_root() -> int:
    import win32gui

    fg = win32gui.GetForegroundWindow()
    return win32gui.GetAncestor(fg, _GA_ROOT) if fg else 0


def _visible_center(element: Any) -> tuple[int, int] | None:
    try:
        r = element.BoundingRectangle
    except Exception:
        return None
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return None
    return (r.left + r.right) // 2, (r.top + r.bottom) // 2


def _hit_reaches(auto: Any, point: tuple[int, int], element: Any) -> bool:
    """Whether the element actually under `point` is `element` or one of its descendants."""
    try:
        hit = auto.ControlFromPoint(*point)
        for _ in range(_MAX_HIT_DEPTH):
            if hit is None:
                return False
            if auto.ControlsAreSame(hit, element):
                return True
            hit = hit.GetParentControl()
    except Exception:
        log.debug("hit-test failed", exc_info=True)
    return False


def _aim(auto: Any, node: dict) -> tuple[int, int] | str:
    """Where synthetic input reaches `node`'s own control, or why no point safely does."""
    element = node.get("_control")
    reason = f"'{node['name']}' isn't visible on screen"
    for attempt in range(_AIM_TRIES):
        if attempt:
            time.sleep(_AIM_WAIT_S)
        point = _visible_center(element) if element is not None else None
        if point is None:
            reason = f"'{node['name']}' isn't visible on screen"
        elif _hit_reaches(auto, point, element):
            return point
        else:
            reason = f"'{node['name']}' is covered by another window"
    return reason


def _aim_window(window: Any) -> tuple[int, int] | str:
    """The window's center, provided the window itself is what's on screen there."""
    reason = "that window isn't visible on screen"
    for attempt in range(_AIM_TRIES):
        if attempt:
            time.sleep(_AIM_WAIT_S)
        point = _visible_center(window)
        if point is None:
            reason = "that window isn't visible on screen"
        elif _root_window_at(*point) == window.NativeWindowHandle:
            return point
        else:
            reason = "that window is covered by another window"
    return reason


# --------------------------------------------------------------------------


@skill(
    name="ui.inspect",
    tier="L0",
    description="List the clickable controls in a window",
    examples=[
        "what can I click here",
        "inspect this window",
        "what controls are on screen",
        "show me the buttons",
        "what are my options here",
    ],
)
def inspect(
    app: Annotated[str, "window to inspect, blank for the active one"] = "",
    depth: Annotated[int, "how deep to walk the tree"] = 8,
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    nodes: list[dict] = []
    _walk(window, 0, depth, nodes)

    if not nodes:
        return SkillResult(
            speech="That window doesn't expose any controls I can read.", ok=False
        )

    buttons = [n["name"] for n in nodes if n["type"] == "Button"][:8]
    fields = [n["name"] for n in nodes if n["type"] == "Edit"][:5]

    parts = [f"{len(nodes)} controls."]
    if buttons:
        parts.append(f"Buttons: {', '.join(buttons)}.")
    if fields:
        parts.append(f"Fields: {', '.join(fields)}.")

    return SkillResult(
        speech=" ".join(parts),
        data={"window": window.Name, "controls": _public(nodes)},
    )


@skill(
    name="ui.click",
    tier="L1",
    action=CLICK_RULE,
    description="Click a button or control by its visible name",
    examples=[
        "click save",
        "press the ok button",
        "click on cancel",
        "hit submit",
        "select the settings tab",
        "click the send button",
    ],
)
def click(
    label: Annotated[str, "visible text of the control to click"],
    app: Annotated[str, "window to act in, blank for the active one"] = "",
    kind: Annotated[str, "optional control type filter, e.g. 'button'"] = "",
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    node, nodes = _find_control(window, label, kind)
    if node is None:
        available = ", ".join(n["name"] for n in nodes[:6]) or "nothing readable"
        return SkillResult(
            speech=f"I can't find '{label}'. I can see: {available}.", ok=False
        )

    data = {"clicked": node["name"], "type": node["type"]}
    # The control's own Invoke pattern acts on that exact element, whatever covers it.
    try:
        pattern = node["_control"].GetInvokePattern()
        if pattern is not None and pattern.Invoke():
            return SkillResult(speech=f"Clicked {node['name']}.", data=data)
    except Exception:
        log.debug("no usable invoke pattern on %r", node["name"], exc_info=True)

    _bring_forward(window)
    point = _aim(_auto(), node)
    if isinstance(point, str):
        return SkillResult(speech=f"I didn't click anything: {point}.", ok=False,
                           data={"control": node["name"], "refused": point})

    from friday import winput

    winput.click(*point)
    return SkillResult(speech=f"Clicked {node['name']}.", data=data)


@skill(
    name="ui.fill",
    tier="L1",
    action="modify",
    description="Type text into a named input field",
    examples=[
        "type my email in the address field",
        "fill in the search box",
        "enter text in the name field",
        "put this in the password box",
    ],
)
def fill(
    field: Annotated[str, "name of the input field"],
    text: Annotated[str, "text to enter"],
    app: Annotated[str, "window to act in, blank for the active one"] = "",
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    node, nodes = _find_control(window, field, "edit")
    if node is None:
        fields = ", ".join(n["name"] for n in nodes if n["type"] == "Edit")[:120]
        return SkillResult(
            speech=f"I can't find a field called '{field}'."
            + (f" I see: {fields}." if fields else ""),
            ok=False,
        )

    data = {"field": node["name"]}
    try:
        pattern = node["_control"].GetValuePattern()
        if pattern is not None and pattern.SetValue(text):
            return SkillResult(speech=f"Filled {node['name']}.", data=data)
    except Exception:
        log.debug("no usable value pattern on %r", node["name"], exc_info=True)

    _bring_forward(window)
    point = _aim(_auto(), node)
    if isinstance(point, str):
        return SkillResult(speech=f"I didn't type anything: {point}.", ok=False, data={**data, "refused": point})

    from friday import winput

    winput.click(*point)
    time.sleep(0.05)
    # Keystrokes go to whichever window has focus, not to a point.
    if _foreground_root() != window.NativeWindowHandle:
        reason = f"'{node['name']}' didn't get keyboard focus"
        return SkillResult(speech=f"I didn't type anything: {reason}.", ok=False, data={**data, "refused": reason})
    winput.press("ctrl+a")
    winput.type_text(text)
    return SkillResult(speech=f"Filled {node['name']}.", data=data)


@skill(
    name="ui.right_click",
    tier="L1",
    action=CLICK_RULE,
    description="Right-click a control by its visible name, opening its context menu",
    examples=[
        "right click save",
        "right click on the file",
        "open the context menu for this item",
        "right click the desktop",
        "show the right click menu for this icon",
    ],
)
def right_click(
    label: Annotated[str, "visible text of the control to right-click"],
    app: Annotated[str, "window to act in, blank for the active one"] = "",
    kind: Annotated[str, "optional control type filter, e.g. 'button'"] = "",
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    node, nodes = _find_control(window, label, kind)
    if node is None:
        available = ", ".join(n["name"] for n in nodes[:6]) or "nothing readable"
        return SkillResult(
            speech=f"I can't find '{label}'. I can see: {available}.", ok=False
        )

    _bring_forward(window)
    point = _aim(_auto(), node)
    if isinstance(point, str):
        return SkillResult(speech=f"I didn't right-click anything: {point}.", ok=False,
                           data={"control": node["name"], "refused": point})

    from friday import winput

    winput.click(*point, button="right")

    return SkillResult(
        speech=f"Right-clicked {node['name']}.",
        data={"clicked": node["name"], "type": node["type"]},
    )


@skill(
    name="ui.drag",
    tier="L1",
    action="modify",
    description="Drag one control onto another (e.g. drag a file onto a folder, reorder a list item)",
    examples=[
        "drag this file into that folder",
        "drag the first item below the second",
        "drag and drop this onto that",
        "move this item to the top by dragging it",
    ],
    dry_run=lambda source, target, app="": f"Drag '{source}' onto '{target}'",
)
def drag(
    source: Annotated[str, "visible text of the control to drag"],
    target: Annotated[str, "visible text of the control to drop it onto"],
    app: Annotated[str, "window to act in, blank for the active one"] = "",
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    src_node, nodes = _find_control(window, source)
    dst_node, _ = _find_control(window, target)
    if src_node is None or dst_node is None:
        missing = source if src_node is None else target
        available = ", ".join(n["name"] for n in nodes[:6]) or "nothing readable"
        return SkillResult(
            speech=f"I can't find '{missing}'. I can see: {available}.", ok=False
        )

    _bring_forward(window)
    auto = _auto()
    src = _aim(auto, src_node)
    dst = _aim(auto, dst_node) if not isinstance(src, str) else src
    if isinstance(src, str) or isinstance(dst, str):
        reason = src if isinstance(src, str) else dst
        return SkillResult(speech=f"I didn't drag anything: {reason}.", ok=False,
                           data={"source": src_node["name"], "target": dst_node["name"], "refused": reason})

    from friday import winput

    winput.drag(*src, *dst)

    return SkillResult(
        speech=f"Dragged {src_node['name']} onto {dst_node['name']}.",
        data={"source": src_node["name"], "target": dst_node["name"]},
    )


@skill(
    name="ui.scroll",
    tier="L1",
    action="modify",
    description="Scroll a window up or down",
    examples=[
        "scroll down",
        "scroll up a bit",
        "scroll down this page",
        "scroll to the bottom",
        "scroll up in this window",
    ],
)
def scroll(
    direction: Annotated[str, "'up' or 'down'"] = "down",
    amount: Annotated[int, "how many notches to scroll"] = 3,
    app: Annotated[str, "window to act in, blank for the active one"] = "",
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    _bring_forward(window)
    point = _aim_window(window)
    if isinstance(point, str):
        return SkillResult(speech=f"I didn't scroll: {point}.", ok=False,
                           data={"direction": direction, "refused": point})

    from friday import winput

    winput.move_mouse(*point)
    sign = -1 if direction.strip().lower().startswith("down") else 1
    winput.scroll(sign * abs(int(amount)))

    return SkillResult(speech=f"Scrolled {direction}.", data={"direction": direction, "amount": amount})


@skill(
    name="ui.read",
    tier="L0",
    description="Read the text content of the current window",
    examples=[
        "read this window",
        "what does this say",
        "read the text on screen",
        "what's written here",
        "read this dialog to me",
    ],
)
def read(
    app: Annotated[str, "window to read, blank for the active one"] = "",
    limit: Annotated[int, "maximum characters to return"] = 2000,
) -> SkillResult:
    window = _target_window(app)
    if window is None:
        return SkillResult(speech=f"I can't find a window for {app}.", ok=False)

    nodes: list[dict] = []
    _walk(window, 0, 10, nodes)

    text_nodes = [n["name"] for n in nodes if n["type"] in ("Text", "Document")]
    body = "\n".join(text_nodes)[:limit]

    if not body.strip():
        return SkillResult(
            speech="I can't read any text from that window. It may be canvas-drawn.",
            ok=False,
        )

    return SkillResult(
        speech=f"Read {len(body)} characters from {window.Name[:40]}.",
        data={"window": window.Name, "text": body},
    )
