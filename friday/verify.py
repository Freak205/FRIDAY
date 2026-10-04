"""Phase 22.0 — post-condition verification.

Until now a state-changing step counted as done because the TOOL said so ("Volume
set to 40 percent.") and the planner said `done`. Neither is evidence about the
world. This module is the small deterministic layer that closes that gap:

    goal -> action -> execution -> POST-CONDITION CHECK -> verified | failed | partial | unverified

After a state-changing tool reports success, the orchestrator asks the tool's
`Verifier` to READ THE REAL STATE BACK (the file exists, the volume is the level that
was asked for, the window is gone...). The verdict is built from those observations
only — never from the tool's speech, never from the planner's words.

Honesty rules (each pinned by `scripts/smoke_postconditions.py`):

  * VERIFIED needs every check to have positively passed.
  * FAILED needs evidence that the state is NOT what was asked for. A reader that
    crashed or could not see anything is not evidence of failure — that is UNVERIFIED.
  * A state-changing tool with no verifier is UNVERIFIED with the reason spelled out:
    there is no registry of "assumed fine". `UNVERIFIABLE` lists every real skill that
    has no safe read-back and why, and a test fails if a new state-changing skill is in
    neither table.
  * Verification only READS. It never calls a tool, never touches permission or
    confirmation, and runs strictly AFTER the executor returned — a call that was
    refused, declined or failed is never verified (there is nothing to verify).

The orchestrator wires this into `Orchestrator._run_step`; `summarize` folds the
per-step results into one goal-level verdict for `plan.run`.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from friday.log import get

log = get(__name__)


class VerifyStatus(str, Enum):
    VERIFIED = "verified"      # every check positively passed
    FAILED = "failed"          # at least one check contradicts the intended state
    PARTIAL = "partial"        # some checks passed, the rest could not be read back
    UNVERIFIED = "unverified"  # nothing could be read back — an explicit "unknown"


@dataclass(slots=True)
class Check:
    """One observation. `passed` is True/False when the state could be read, None when
    it could not — None is never treated as a pass."""

    name: str
    passed: bool | None
    expected: str = ""
    observed: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed, "expected": self.expected,
            "observed": self.observed, "detail": self.detail,
        }


@dataclass(slots=True)
class Verification:
    status: VerifyStatus
    checks: list[Check] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "reason": self.reason, "checks": [c.to_dict() for c in self.checks]}

    @property
    def verified(self) -> bool:
        return self.status is VerifyStatus.VERIFIED


def status_of(checks: list[Check]) -> VerifyStatus:
    """The one rule that turns observations into a status."""
    if not checks:
        return VerifyStatus.UNVERIFIED
    if any(c.passed is False for c in checks):
        return VerifyStatus.FAILED
    if all(c.passed is True for c in checks):
        return VerifyStatus.VERIFIED
    if any(c.passed is True for c in checks):
        return VerifyStatus.PARTIAL
    return VerifyStatus.UNVERIFIED


def _reason(status: VerifyStatus, checks: list[Check]) -> str:
    if status is VerifyStatus.VERIFIED:
        return "; ".join(f"{c.name}: {c.observed or 'confirmed'}" for c in checks)[:300]
    if status is VerifyStatus.FAILED:
        bad = [c for c in checks if c.passed is False]
        return "; ".join(
            f"{c.name}: expected {c.expected or 'the requested state'}, found {c.observed or 'something else'}"
            for c in bad
        )[:300]
    unknown = [c for c in checks if c.passed is None]
    if not unknown:
        return "no read-back was possible"
    return "; ".join(f"{c.name}: {c.detail or 'could not be read back'}" for c in unknown)[:300]


def build(checks: list[Check]) -> Verification:
    status = status_of(checks)
    return Verification(status, checks, _reason(status, checks))


# -- the read-only world -----------------------------------------------------------------


class _Readers:
    """Every place the verifiers look at the real machine. Read-only by construction,
    each returns None when the state cannot be read here — and each is a plain attribute,
    so a test replaces one with a fake instead of touching the OS."""

    def volume_pct(self) -> int | None:
        try:
            from friday.skills.system import _get_volume_pct

            return int(_get_volume_pct())
        except Exception:
            return None

    def muted(self) -> bool | None:
        try:
            from friday.skills.system import _endpoint

            return bool(_endpoint().GetMute())
        except Exception:
            return None

    def brightness_pct(self) -> int | None:
        try:
            from friday.skills.hardware import _get_brightness

            return _get_brightness()
        except Exception:
            return None

    def power_plan_name(self) -> str | None:
        try:
            import subprocess

            out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=5).stdout
            return out.strip().lower() or None
        except Exception:
            return None

    def windows(self) -> list[dict] | None:
        try:
            from friday.skills.apps import _windows

            return list(_windows())
        except Exception:
            return None

    def match_window(self, app: str) -> dict | None:
        try:
            from friday.skills.apps import _match_window

            return _match_window(app, self.windows() or [])
        except Exception:
            return None

    def foreground(self) -> tuple[int, str] | None:
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            return hwnd, win32gui.GetWindowText(hwnd)
        except Exception:
            return None

    def resolve_window(self, app: str) -> tuple[int, str] | None:
        """The window `window.*` would act on for `app` (blank = the foreground one)."""
        try:
            from friday.skills.windows import _find

            return _find(app)
        except Exception:
            return None

    def window_state(self, hwnd: int) -> dict | None:
        try:
            import win32gui

            if not win32gui.IsWindow(hwnd):
                return {"exists": False, "zoomed": False, "iconic": False}
            return {"exists": True, "zoomed": bool(win32gui.IsZoomed(hwnd)), "iconic": bool(win32gui.IsIconic(hwnd))}
        except Exception:
            return None

    def memory_values(self) -> list[str] | None:
        try:
            from friday import memory

            return [str(r["value"]) for r in memory.all_memories(limit=5000)]
        except Exception:
            return None

    def job_by_id(self, job_id: int) -> Any:
        try:
            from friday import jobs

            return jobs.get_job(int(job_id))
        except Exception:
            return False  # False = unreadable (None means "no such job")

    def job_by_name(self, name: str) -> Any:
        try:
            from friday import jobs

            return jobs.by_name(name)
        except Exception:
            return False

    def jobs(self) -> list | None:
        try:
            from friday import jobs

            return list(jobs.all_jobs())
        except Exception:
            return None

    def notes_lines(self) -> list[str] | None:
        try:
            from friday.skills.utility import NOTES_FILE

            if not NOTES_FILE.exists():
                return []
            return [ln for ln in NOTES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            return None

    def clipboard_text(self) -> str | None:
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return None

    def wifi_up(self) -> bool | None:
        try:
            import psutil

            wifi = [
                s for name, s in psutil.net_if_stats().items()
                if "wi-fi" in name.lower() or "wireless" in name.lower() or "wlan" in name.lower()
            ]
            return any(s.isup for s in wifi) if wifi else None
        except Exception:
            return None

    def running_pids(self, pids: list[int]) -> set[int] | None:
        try:
            import psutil

            return {p for p in pids if psutil.pid_exists(p)}
        except Exception:
            return None

    def pids_named(self, name: str) -> list[int] | None:
        try:
            import psutil

            target = name.lower().removesuffix(".exe")
            return [
                p.pid for p in psutil.process_iter(["name"])
                if (p.info.get("name") or "").lower().removesuffix(".exe") == target
            ]
        except Exception:
            return None


READERS = _Readers()


# -- filesystem primitives -----------------------------------------------------------------
#
# FRIDAY has no skill that creates, renames or deletes an arbitrary user file (web.download
# is the one that writes a file; shell.run/dev.python could, but their effect cannot be known
# from their arguments — see UNVERIFIABLE). These are the checks a file-mutating tool would
# declare, exercised by `scripts/smoke_postconditions.py` through fixture tools.


def _clip(text: str, n: int = 120) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 3] + "..."


def path_exists(path: str, *, kind: str = "any", min_bytes: int = 0, name: str = "file exists") -> Check:
    p = os.path.expanduser(str(path))
    try:
        exists = os.path.exists(p)
        if not exists:
            return Check(name, False, f"{_clip(path)} exists", "it does not exist")
        if kind == "file" and not os.path.isfile(p):
            return Check(name, False, f"{_clip(path)} is a file", "it is not a regular file")
        if kind == "dir" and not os.path.isdir(p):
            return Check(name, False, f"{_clip(path)} is a folder", "it is not a folder")
        if min_bytes and os.path.isfile(p) and os.path.getsize(p) < min_bytes:
            return Check(name, False, f"at least {min_bytes} bytes", f"{os.path.getsize(p)} bytes")
        return Check(name, True, f"{_clip(path)} exists", f"{_clip(path)} exists")
    except OSError as exc:
        return Check(name, None, detail=f"could not inspect {_clip(path)}: {exc}")


def path_absent(path: str, *, name: str = "target is gone") -> Check:
    p = os.path.expanduser(str(path))
    try:
        if os.path.lexists(p):
            return Check(name, False, f"{_clip(path)} gone", "it is still there")
        return Check(name, True, f"{_clip(path)} gone", f"{_clip(path)} is gone")
    except OSError as exc:
        return Check(name, None, detail=f"could not inspect {_clip(path)}: {exc}")


def file_contains(path: str, text: str, *, name: str = "file content") -> Check:
    try:
        body = open(os.path.expanduser(str(path)), encoding="utf-8", errors="replace").read()
    except OSError as exc:
        return Check(name, None, detail=f"could not read {_clip(path)}: {exc}")
    found = str(text) in body
    return Check(name, found, f"contains {_clip(text, 40)!r}", "it does" if found else "it does not")


def path_moved(old: str, new: str) -> list[Check]:
    return [path_exists(new, name="new path exists"), path_absent(old, name="old path is gone")]


# -- verifiers ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verifier:
    """How to verify one tool.

    `check(args, data, before)` returns the observed checks, or None when THIS call has no
    state effect to verify (a "report" mode of a setter). `prepare(args)` runs BEFORE the
    call and may read whatever baseline `check` needs (the old volume, the window that was
    open); it must only read. `settle_s` keeps re-reading while the state has not yet
    reached the intended value (a window takes a moment to close), up to that long."""

    check: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], list[Check] | None]
    prepare: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    settle_s: float = 0.0
    interval_s: float = 0.25


VERIFIERS: dict[str, Verifier] = {}

# Every real state-changing skill that has NO safe deterministic read-back, and why.
# Explicit on purpose: the honest answer for these is "unverified", and a test refuses a new
# state-changing skill that is in neither this table nor VERIFIERS.
UNVERIFIABLE: dict[str, str] = {
    "shell.run": "arbitrary command; its effect cannot be derived from its arguments",
    "dev.python": "arbitrary code; its effect cannot be derived from its arguments",
    "dev.git": "arbitrary git subcommand; no single state to read back",
    "ui.click": "a click has no generic read-back; the UI change it caused is unknown",
    "ui.fill": "field contents are not readable through a generic, safe API",
    "ui.right_click": "a right-click opens a context menu with no generic read-back",
    "ui.drag": "a drag's effect (reorder, drop) has no generic, safe read-back",
    "ui.scroll": "scroll position is not exposed through a generic, safe API",
    "screen.click_text": "a click has no generic read-back",
    "browser.click": "a click has no generic read-back",
    "browser.type": "page field contents are not readable back reliably",
    "browser.press": "a key press has no generic read-back",
    "browser.open": "the page load is not confirmed by the call; navigation state is asynchronous",
    "browser.close": "browser session state is not exposed for read-back",
    "web.open": "opens the default browser (or navigates the controlled session, if one is open); navigation state is asynchronous either way",
    "input.type": "typed text lands in whatever window has focus; not readable back",
    "input.hotkey": "a key combination has no generic read-back",
    "project.open": "launches an editor; no reliable window signature for a project",
    "files.reveal": "opens Explorer; the window is not identifiable reliably",
    "desktop.new": "virtual-desktop state is not exposed for read-back",
    "desktop.switch": "virtual-desktop state is not exposed for read-back",
    "window.snap": "window geometry after a snap depends on DPI/borders; no exact expectation",
    "window.minimize_all": "'show desktop' is a toggle with no reliable state read-back",
    "system.lock": "a locked session cannot be observed from inside it",
    "system.shutdown": "the machine is going away; nothing to read back",
    "knowledge.index": "indexing is asynchronous and its result is not a single readable state",
    "knowledge.forget": "no reliable per-document read-back",
    "notify.send": "a toast notification cannot be observed",
    "routine.create": "no read-back API for routines",
    "routine.run": "runs several actions; each would need its own read-back",
    "schedule.run_now": "runs the job's actions; their effects are not known here",
    "whatsapp.open": "external app state is not readable",
    "whatsapp.compose": "external app state is not readable",
    "whatsapp.send": "delivery cannot be confirmed from here",
    "meta.undo": "reverses whichever action came last; the expected state is not known here",
    "plan.run": "a nested plan; its own steps are verified individually",
}


def register(tool: str, verifier: Verifier) -> None:
    VERIFIERS[tool] = verifier


def unregister(tool: str) -> None:
    VERIFIERS.pop(tool, None)


def verifier_for(tool: str) -> Verifier | None:
    return VERIFIERS.get(tool)


def _clamp(n: int) -> int:
    return max(0, min(100, int(n)))


def _num(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _level_check(name: str, expected: int, observed: int | None, tol: int) -> Check:
    if observed is None:
        return Check(name, None, f"{expected}%", detail="the level could not be read back on this machine")
    ok = abs(observed - expected) <= tol
    return Check(name, ok, f"{expected}%", f"{observed}%")


# volume ------------------------------------------------------------------------------------


def _volume_set(args, data, before):
    return [_level_check("volume level", _clamp(_num(args.get("level"), 0)), READERS.volume_pct(), 1)]


def _volume_before(args):
    return {"level": READERS.volume_pct()}


def _volume_delta(sign: int):
    def check(args, data, before):
        base = before.get("level")
        if base is None:
            return [Check("volume level", None, detail="no baseline reading before the change")]
        expected = _clamp(base + sign * _num(args.get("amount"), 10))
        return [_level_check("volume level", expected, READERS.volume_pct(), 1)]

    return check


def _volume_mute(args, data, before):
    want = args.get("state", True)
    if isinstance(want, str):
        want = want.strip().lower() not in ("false", "0", "no", "off")
    seen = READERS.muted()
    if seen is None:
        return [Check("mute state", None, detail="the mute state could not be read back")]
    return [Check("mute state", seen == bool(want), "muted" if want else "unmuted", "muted" if seen else "unmuted")]


# brightness -----------------------------------------------------------------------------------


def _brightness_set(args, data, before):
    return [_level_check("brightness", _clamp(_num(args.get("level"), 0)), READERS.brightness_pct(), 3)]


def _brightness_before(args):
    return {"level": READERS.brightness_pct()}


def _brightness_delta(sign: int):
    def check(args, data, before):
        base = before.get("level")
        if base is None:
            return [Check("brightness", None, detail="no baseline reading (this display may not report brightness)")]
        expected = _clamp(base + sign * _num(args.get("amount"), 20))
        return [_level_check("brightness", expected, READERS.brightness_pct(), 3)]

    return check


# power plan / wifi ------------------------------------------------------------------------------


def _power_plan(args, data, before):
    plan = str(args.get("plan") or "").strip().lower()
    if not plan:
        return None  # report mode: nothing was changed
    key = next((k for k in ("balanced", "high performance", "power saver") if k in plan), None)
    if key is None:
        return [Check("power plan", None, detail=f"unrecognised plan {plan!r}")]
    seen = READERS.power_plan_name()
    if seen is None:
        return [Check("power plan", None, detail="the active power plan could not be read back")]
    return [Check("power plan", key in seen, key, _clip(seen, 80))]


def _wifi(args, data, before):
    want = args.get("enable", True)
    if isinstance(want, str):
        want = want.strip().lower() not in ("false", "0", "no", "off")
    up = READERS.wifi_up()
    if up is None:
        return [Check("wi-fi adapter", None, detail="no Wi-Fi interface could be read on this machine")]
    if want:
        # An enabled adapter that is not associated to a network reports "down" too, so
        # "not up" proves nothing about enabling — only "up" confirms it.
        return [Check("wi-fi adapter", True if up else None, "enabled", "up" if up else "not up yet",
                      "" if up else "an enabled adapter with no connection also reads as down")]
    return [Check("wi-fi adapter", not up, "disabled", "still up" if up else "down")]


# applications / windows ----------------------------------------------------------------------------


def _open_app(args, data, before):
    name = str(data.get("app") or args.get("app") or "")
    win = READERS.match_window(name)
    if win is not None:
        return [Check("app window", True, f"a window for {_clip(name, 40)}", _clip(win.get("title", ""), 60))]
    return [Check("app window", None, detail=f"no window for {_clip(name, 40)} is visible (yet) — "
                                              "some apps start slowly or without a titled window")]


def _close_before(args):
    win = READERS.match_window(str(args.get("app") or ""))
    return {"hwnd": win.get("hwnd") if win else None, "title": win.get("title", "") if win else ""}


def _close_app(args, data, before):
    hwnd = before.get("hwnd")
    if hwnd is None:
        return [Check("window closed", None, detail="the window was not identified before closing")]
    wins = READERS.windows()
    if wins is None:
        return [Check("window closed", None, detail="the window list could not be read back")]
    still = any(w.get("hwnd") == hwnd for w in wins)
    return [Check("window closed", not still, f"{_clip(before.get('title', ''), 40)} closed",
                  "still open (it may be waiting on a prompt)" if still else "it is gone")]


def _focus_app(args, data, before):
    fg = READERS.foreground()
    if fg is None:
        return [Check("window focused", None, detail="the foreground window could not be read")]
    want = str(data.get("title") or "")
    if not want:
        return [Check("window focused", None, detail="the target window was not reported")]
    return [Check("window focused", fg[1] == want, _clip(want, 50), _clip(fg[1], 50))]


def _window_before(args):
    target = READERS.resolve_window(str(args.get("app") or ""))
    return {"hwnd": target[0] if target else None}


def _window_state(want: str):
    def check(args, data, before):
        hwnd = before.get("hwnd")
        if hwnd is None:
            return [Check("window state", None, detail="the window was not identified before the change")]
        st = READERS.window_state(hwnd)
        if st is None or not st.get("exists", True):
            return [Check("window state", None, detail="the window state could not be read back")]
        if want == "maximized":
            return [Check("window state", st["zoomed"], "maximized", "maximized" if st["zoomed"] else "not maximized")]
        if want == "minimized":
            return [Check("window state", st["iconic"], "minimized", "minimized" if st["iconic"] else "not minimized")]
        normal = not st["zoomed"] and not st["iconic"]
        return [Check("window state", normal, "normal size", "normal" if normal else "still maximized/minimized")]

    return check


def _kill_before(args):
    return {"pids": READERS.pids_named(str(args.get("name") or "")) or []}


def _kill_process(args, data, before):
    pids = [p for p in (data.get("pids") or before.get("pids") or []) if isinstance(p, int)]
    if not pids:
        return [Check("process gone", None, detail="no process ids were reported")]
    alive = READERS.running_pids(pids)
    if alive is None:
        return [Check("process gone", None, detail="process state could not be read back")]
    return [Check("process gone", not alive, "stopped", f"{len(alive)} still running" if alive else "all stopped")]


# memory / notes / schedule / clipboard / files ---------------------------------------------------


def _memory_before(args):
    vals = READERS.memory_values()
    return {"values": vals}


def _remember(args, data, before):
    value = data.get("remembered")
    if not value:
        return None  # rerouted to a recall: nothing was stored
    vals = READERS.memory_values()
    if vals is None:
        return [Check("memory stored", None, detail="the memory store could not be read back")]
    return [Check("memory stored", str(value) in vals, f"{_clip(str(value), 50)} stored",
                  "it is stored" if str(value) in vals else "not found in the store")]


def _forget(args, data, before):
    gone = [str(v) for v in (data.get("forgotten") or [])]
    if not gone:
        return None
    vals = READERS.memory_values()
    if vals is None:
        return [Check("memory removed", None, detail="the memory store could not be read back")]
    prior = before.get("values")
    checks = []
    for v in dict.fromkeys(gone):
        now = vals.count(v)
        if prior is not None:
            expect = prior.count(v) - gone.count(v)
            checks.append(Check("memory removed", now <= max(0, expect), f"{_clip(v, 40)} removed",
                                "removed" if now <= max(0, expect) else f"{now} copies remain"))
        else:
            checks.append(Check("memory removed", True if now == 0 else None, f"{_clip(v, 40)} removed",
                                "removed" if now == 0 else "", "" if now == 0 else "no baseline to compare against"))
    return checks


def _job_created(args, data, before):
    job_id = data.get("id")
    if job_id is None:
        return [Check("job saved", None, detail="the job id was not reported")]
    job = READERS.job_by_id(job_id)
    if job is False:
        return [Check("job saved", None, detail="the job store could not be read back")]
    return [Check("job saved", job is not None, f"job {job_id} saved", "it is saved" if job else "no such job")]


def _job_deleted(args, data, before):
    name = data.get("deleted")
    if not name:
        return None
    job = READERS.job_by_name(str(name))
    if job is False:
        return [Check("job removed", None, detail="the job store could not be read back")]
    return [Check("job removed", job is None, f"{_clip(str(name), 40)} removed", "it is gone" if job is None else "still scheduled")]


def _job_toggled(args, data, before):
    name = data.get("name")
    if not name:
        return None
    want = args.get("enable", False)
    if isinstance(want, str):
        want = want.strip().lower() in ("true", "1", "yes", "on")
    job = READERS.job_by_name(str(name))
    if job is False:
        return [Check("job state", None, detail="the job store could not be read back")]
    if job is None:
        return [Check("job state", False, "job present", "no such job")]
    return [Check("job state", bool(job.enabled) == bool(want), "enabled" if want else "paused",
                  "enabled" if job.enabled else "paused")]


def _timer_created(args, data, before):
    fires = data.get("fires_at")
    if not fires:
        return [Check("timer saved", None, detail="the timer time was not reported")]
    jobs = READERS.jobs()
    if jobs is None:
        return [Check("timer saved", None, detail="the job store could not be read back")]
    found = any((getattr(j, "trigger_spec", {}) or {}).get("run_at") == fires for j in jobs)
    return [Check("timer saved", found, "a one-shot job at that time", "found" if found else "no such job")]


def _notes_before(args):
    lines = READERS.notes_lines()
    return {"count": None if lines is None else len(lines)}


def _note_added(args, data, before):
    note = str(data.get("note") or "")
    lines = READERS.notes_lines()
    if not note:
        return [Check("note saved", None, detail="the note text was not reported")]
    if lines is None:
        return [Check("note saved", None, detail="the notes file could not be read back")]
    wrote = bool(lines) and lines[-1].rstrip().endswith(note)
    grew = before.get("count") is None or len(lines) == before["count"] + 1
    return [Check("note saved", wrote and grew, "the note is the last line", "it is" if wrote and grew else "not found")]


def _clipboard_written(args, data, before):
    seen = READERS.clipboard_text()
    if seen is None:
        return [Check("clipboard", None, detail="the clipboard could not be read back")]
    want = str(args.get("text", ""))
    return [Check("clipboard", seen == want, f"{len(want)} characters", f"{len(seen)} characters")]


def _downloaded(args, data, before):
    path = data.get("path")
    if not path:
        return [Check("downloaded file", None, detail="the saved path was not reported")]
    return [path_exists(str(path), kind="file", min_bytes=1, name="downloaded file")]


# filesystem mutation (Phase 26) ----------------------------------------------------------------


def _fs_write(args, data, before):
    path = data.get("path") or args.get("path")
    if not path:
        return [Check("file written", None, detail="the written path was not reported")]
    checks = [path_exists(str(path), kind="file", name="file exists")]
    content = args.get("content")
    if content:
        checks.append(file_contains(str(path), str(content), name="content matches"))
    return checks


def _fs_copy(args, data, before):
    dst = data.get("dst") or args.get("dst")
    if not dst:
        return [Check("file copied", None, detail="the destination path was not reported")]
    return [path_exists(str(dst), kind="file", min_bytes=1, name="copy exists")]


def _fs_move(args, data, before):
    src = data.get("src") or args.get("src")
    dst = data.get("dst") or args.get("dst")
    if not src or not dst:
        return [Check("file moved", None, detail="the source/destination path was not reported")]
    return path_moved(str(src), str(dst))


def _fs_delete(args, data, before):
    path = args.get("path")
    if not path:
        return [Check("file deleted", None, detail="no path was given")]
    return [path_absent(str(path), name="path is gone")]


def _fs_mkdir(args, data, before):
    path = data.get("path") or args.get("path")
    if not path:
        return [Check("folder created", None, detail="the created path was not reported")]
    return [path_exists(str(path), kind="dir", name="folder exists")]


def _register_real() -> None:
    register("system.volume.set", Verifier(_volume_set, settle_s=0.6))
    register("system.volume.up", Verifier(_volume_delta(+1), _volume_before, settle_s=0.6))
    register("system.volume.down", Verifier(_volume_delta(-1), _volume_before, settle_s=0.6))
    register("system.volume.mute", Verifier(_volume_mute, settle_s=0.6))
    register("system.brightness.set", Verifier(_brightness_set, settle_s=1.0))
    register("system.brightness.up", Verifier(_brightness_delta(+1), _brightness_before, settle_s=1.0))
    register("system.brightness.down", Verifier(_brightness_delta(-1), _brightness_before, settle_s=1.0))
    register("system.power_plan", Verifier(_power_plan, settle_s=1.0))
    register("network.wifi.toggle", Verifier(_wifi, settle_s=6.0, interval_s=0.5))
    register("apps.open", Verifier(_open_app, settle_s=6.0, interval_s=0.5))
    register("apps.close", Verifier(_close_app, _close_before, settle_s=3.0, interval_s=0.3))
    register("apps.focus", Verifier(_focus_app, settle_s=1.5, interval_s=0.3))
    register("window.maximize", Verifier(_window_state("maximized"), _window_before, settle_s=1.0))
    register("window.minimize", Verifier(_window_state("minimized"), _window_before, settle_s=1.0))
    register("window.restore", Verifier(_window_state("normal"), _window_before, settle_s=1.0))
    register("process.kill", Verifier(_kill_process, _kill_before, settle_s=2.0, interval_s=0.3))
    register("memory.remember", Verifier(_remember))
    register("memory.forget", Verifier(_forget, _memory_before))
    register("schedule.create", Verifier(_job_created))
    register("schedule.delete", Verifier(_job_deleted))
    register("schedule.toggle", Verifier(_job_toggled))
    register("timer.set", Verifier(_timer_created))
    register("notes.add", Verifier(_note_added, _notes_before))
    register("clipboard.write", Verifier(_clipboard_written))
    register("web.download", Verifier(_downloaded))
    register("files.write", Verifier(_fs_write, settle_s=0.3))
    register("files.copy", Verifier(_fs_copy, settle_s=0.3))
    register("files.move", Verifier(_fs_move, settle_s=0.3))
    register("files.delete", Verifier(_fs_delete, settle_s=0.3))
    register("files.mkdir", Verifier(_fs_mkdir, settle_s=0.3))


_register_real()


# -- running a verification -----------------------------------------------------------------------


async def capture_before(tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    """The baseline a verifier asked for, read BEFORE the call. Never raises: a baseline
    that cannot be read simply leaves the dependent check unverifiable."""
    v = verifier_for(tool)
    if v is None or v.prepare is None:
        return {}
    try:
        return dict(await asyncio.wait_for(asyncio.to_thread(v.prepare, dict(args or {})), timeout=timeout_s) or {})
    except Exception as exc:  # incl. timeout — a missing baseline is "unknown", not "failed"
        log.debug("verify: baseline for %s unavailable: %s", tool, exc)
        return {}


def no_verifier(tool: str) -> Verification:
    why = UNVERIFIABLE.get(tool)
    reason = f"there is no way to read the result of {tool} back" + (f" ({why})" if why else "")
    return Verification(VerifyStatus.UNVERIFIED, [Check("read-back", None, detail=reason)], reason)


async def verify_call(
    tool: str, args: dict[str, Any], data: dict[str, Any], before: dict[str, Any] | None, *, timeout_s: float,
) -> Verification | None:
    """Read the real state back for one state-changing call that reported success.

    None  = this call has no state effect to verify (the verifier said so).
    Else a `Verification`. A reader that raises or a read-back that times out is
    UNVERIFIED, never FAILED, and never an exception in the caller."""
    v = verifier_for(tool)
    if v is None:
        return no_verifier(tool)

    started = time.monotonic()
    deadline = started + max(0.1, timeout_s)
    settle_until = started + min(max(0.0, v.settle_s), max(0.0, timeout_s - 0.2))
    last: Verification | None = None
    while True:
        try:
            checks = await asyncio.wait_for(
                asyncio.to_thread(v.check, dict(args or {}), dict(data or {}), dict(before or {})),
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            return last or Verification(
                VerifyStatus.UNVERIFIED, [Check("read-back", None, detail="the read-back timed out")],
                "the read-back timed out",
            )
        except Exception as exc:
            log.debug("verify: check for %s raised: %s", tool, exc)
            return Verification(
                VerifyStatus.UNVERIFIED, [Check("read-back", None, detail=f"the read-back failed: {type(exc).__name__}")],
                f"the read-back failed: {type(exc).__name__}",
            )
        if checks is None:
            return None
        last = build(checks)
        if last.status is VerifyStatus.VERIFIED or time.monotonic() >= settle_until:
            return last
        await asyncio.sleep(min(v.interval_s, max(0.0, settle_until - time.monotonic())))


# -- factories for tools that declare their own file effect ---------------------------------------------


def creates_file(arg: str = "path", *, min_bytes: int = 0) -> Verifier:
    """The path in `args[arg]` must exist as a regular file afterwards."""
    return Verifier(lambda a, d, b: [path_exists(str(a.get(arg, "")), kind="file", min_bytes=min_bytes)])


def creates_folder(arg: str = "path") -> Verifier:
    return Verifier(lambda a, d, b: [path_exists(str(a.get(arg, "")), kind="dir")])


def removes_path(arg: str = "path") -> Verifier:
    """The path must be gone afterwards (a deleted file/folder)."""
    return Verifier(lambda a, d, b: [path_absent(str(a.get(arg, "")))])


def renames_path(old_arg: str = "old", new_arg: str = "new") -> Verifier:
    """The new path must exist and the old one must not."""
    return Verifier(lambda a, d, b: path_moved(str(a.get(old_arg, "")), str(a.get(new_arg, ""))))


def writes_text(path_arg: str = "path", text_arg: str = "text") -> Verifier:
    """The file must exist and contain the text that was written."""
    return Verifier(lambda a, d, b: [
        path_exists(str(a.get(path_arg, "")), kind="file"),
        file_contains(str(a.get(path_arg, "")), str(a.get(text_arg, ""))),
    ])


# -- the goal-level verdict --------------------------------------------------------------------------


@dataclass(slots=True)
class GoalVerification:
    """What the state-changing steps of one run add up to. `status` is one of the
    VerifyStatus values or "not_applicable" (no state-changing step ran)."""

    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "steps": list(self.steps), "failed": list(self.failed), "unverified": list(self.unverified)}

    @property
    def verified_ok(self) -> bool:
        return self.status in ("verified", "not_applicable")


def _identity(tool: str, args: dict[str, Any]) -> str:
    from friday import intent

    return f"{tool}:{sorted(intent.normalize_args(tool, args, semantic=True).items(), key=repr)!r}"


def summarize(observations: list[Any]) -> GoalVerification:
    """Fold per-step verifications into one verdict. Considers only steps that carry a
    verification (a state-changing call that really executed and reported success); a step
    that failed on its own is an ordinary failure and is reported as one elsewhere. A
    FAILED step is recovered when the same call later VERIFIED."""
    rows: list[tuple[Any, Verification]] = [
        (o, o.verification) for o in observations if getattr(o, "verification", None) is not None
    ]
    if not rows:
        return GoalVerification("not_applicable")

    steps = [
        {"tool": o.step.tool, "status": v.status.value, "reason": v.reason[:160]}
        for o, v in rows
    ]
    failed: list[str] = []
    unverified: list[str] = []
    n_verified = 0
    for i, (o, v) in enumerate(rows):
        label = f"{o.step.tool}: {v.reason}" if v.reason else o.step.tool
        if v.status is VerifyStatus.VERIFIED:
            n_verified += 1
            continue
        if v.status is VerifyStatus.FAILED:
            key = _identity(o.step.tool, o.step.args)
            if any(
                later.status is VerifyStatus.VERIFIED and _identity(lo.step.tool, lo.step.args) == key
                for lo, later in rows[i + 1:]
            ):
                continue  # recovered by a later, verified repeat of the same call
            failed.append(label)
        else:
            unverified.append(label)

    if failed:
        status = "failed"
    elif not unverified:
        status = "verified"
    elif n_verified:
        status = "partial"
    else:
        status = "unverified"
    return GoalVerification(status, steps, failed, unverified)
