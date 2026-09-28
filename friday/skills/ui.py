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
                })
            _walk(child, depth + 1, max_depth, out)
        except Exception:
            continue


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
        data={"window": window.Name, "controls": nodes},
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

    # Prefer the accessibility Invoke pattern; fall back to a real click.
    auto = _auto()
    try:
        control = auto.ControlFromPoint(*node["center"])
        if control and hasattr(control, "GetInvokePattern"):
            control.GetInvokePattern().Invoke()
        else:
            raise AttributeError("no invoke pattern")
    except Exception:
        from friday import winput

        winput.click(*node["center"])

    return SkillResult(
        speech=f"Clicked {node['name']}.",
        data={"clicked": node["name"], "type": node["type"]},
    )


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

    auto = _auto()
    from friday import winput

    try:
        control = auto.ControlFromPoint(*node["center"])
        control.GetValuePattern().SetValue(text)
    except Exception:
        winput.click(*node["center"])
        winput.press("ctrl+a")
        winput.type_text(text)

    return SkillResult(speech=f"Filled {node['name']}.", data={"field": node["name"]})


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
