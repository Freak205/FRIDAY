"""Window-state persistence.

JSON under `paths.DATA`, not `QSettings`/the Windows registry — this project
writes every piece of durable state it owns under `ROOT/data` (see
`friday/paths.py`'s own docstring), and `config.yaml` already covers
hand-editable *behavior*; this file is purely ephemeral "where did I leave
the window" state, so it follows the same convention rather than being the
one thing FRIDAY stores in the registry.
"""

from __future__ import annotations

import json
from typing import Any

from friday import paths
from friday.log import get

log = get(__name__)

_PATH = paths.DATA / "gui_state.json"

_DEFAULTS: dict[str, Any] = {
    "x": None, "y": None, "width": 1040, "height": 780,
    "always_on_top": False,
}


def load() -> dict[str, Any]:
    if not _PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
        return merged
    except Exception:
        log.warning("gui_state.json unreadable; using defaults", exc_info=True)
        return dict(_DEFAULTS)


def save(state: dict[str, Any]) -> None:
    try:
        paths.ensure()
        payload = {k: state.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
        _PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        log.warning("failed to persist gui_state.json (non-fatal)", exc_info=True)
