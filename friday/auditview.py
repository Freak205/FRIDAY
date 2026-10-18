"""What the audit log, the episode store and the log files may keep about a skill call (Phase 35).

The audit log exists to answer "what did FRIDAY do, when, on whose instruction, and did I approve it" -- not to
keep a second copy of everything the user typed, wrote or read. So storage is ALLOWLIST-by-field: every
parameter of every skill is classified below, and anything not classified is dropped to a length marker (fail
closed), instead of relying on a regex to spot secrets in text we chose to keep.

  keep     enumerated / structural value (app name, key combo, direction, snapshot id): stored as is, bounded.
  path     a file system location: stored as is (it is the evidence of WHAT was touched), bounded.
  url      scheme + host + path only; userinfo, query string and fragment are dropped (that is where tokens live).
  intent   a short human request (search query, goal, label): bounded and credential-scrubbed. Never a body.
  argv0    a command line: only the executable's name and how many arguments followed.
  omit     a body of content (file content, typed text, a message, code, a note): replaced by "[omitted: N chars]".

Numbers, booleans and None are always kept. A key that NAMES a credential (password, token, ...) is always
"[redacted]". The skill name, tier, decision, outcome, duration and the undo journal are untouched, and
`ok` is never inferred from anything here: recording an argument is not evidence that an action happened.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import urlsplit

_MAX_KEEP = 200
_MAX_PATH = 500
_MAX_INTENT = 200
_MAX_RESULT = 500
_MAX_ERROR = 300

KEEP, PATH, URL, INTENT, ARGV0, OMIT = "keep", "path", "url", "intent", "argv0", "omit"

# skill -> {param: mode}. A param that is missing here is OMITTED when it is text/list/dict (fail closed).
# tests/test_auditview-style coverage (scripts/smoke_data_privacy.py) fails if a registered skill has an
# unclassified parameter, so a new skill cannot silently ship an unreviewed argument.
POLICY: dict[str, dict[str, str]] = {
    "apps.close": {"app": KEEP}, "apps.focus": {"app": KEEP}, "apps.open": {"app": KEEP},
    "browser.click": {"target": INTENT}, "browser.open": {"url": URL}, "browser.press": {"key": KEEP},
    "browser.type": {"field": INTENT, "text": OMIT},
    "clipboard.write": {"text": OMIT},
    "desktop.switch": {"direction": KEEP},
    "dev.git": {"subcommand": ARGV0, "repo": PATH}, "dev.python": {"code": OMIT},
    "files.copy": {"src": PATH, "dst": PATH}, "files.delete": {"path": PATH}, "files.mkdir": {"path": PATH},
    "files.move": {"src": PATH, "dst": PATH}, "files.read": {"path": PATH},
    "files.recover": {"snapshot": KEEP}, "files.restore": {"snapshot": KEEP, "path": PATH},
    "files.reveal": {"path": PATH}, "files.search": {"query": INTENT, "extension": KEEP},
    "files.write": {"path": PATH, "content": OMIT},
    "input.hotkey": {"combo": KEEP}, "input.type": {"text": OMIT},
    "knowledge.ask": {"query": INTENT}, "knowledge.forget": {"query": INTENT}, "knowledge.index": {"path": PATH},
    "math.calculate": {"expression": INTENT},
    "memory.forget": {"query": INTENT}, "memory.list": {"kind": KEEP}, "memory.recall": {"query": INTENT},
    "memory.remember": {"text": OMIT},
    "meta.capabilities": {"area": KEEP},
    "notes.add": {"text": OMIT},
    "notify.send": {"title": INTENT, "message": OMIT},
    "plan.run": {"goal": INTENT, "resume_goal_id": KEEP, "clarification_answer": OMIT, "scope_expansion": INTENT},
    "process.kill": {"name": KEEP}, "process.top": {"by": KEEP},
    "project.inspect": {"name": KEEP}, "project.open": {"name": KEEP},
    "routine.create": {"steps": OMIT}, "routine.run": {"name": KEEP},
    "schedule.create": {"command": INTENT, "when": KEEP, "name": KEEP}, "schedule.delete": {"name": KEEP},
    "schedule.history": {"name": KEEP}, "schedule.run_now": {"name": KEEP}, "schedule.toggle": {"name": KEEP},
    "screen.capture": {"region": KEEP, "save": PATH},
    "screen.click_text": {"query": INTENT, "region": KEEP}, "screen.find_text": {"query": INTENT, "region": KEEP},
    "screen.read_text": {"region": KEEP},
    "shell.run": {"command": ARGV0, "shell": KEEP, "cwd": PATH},
    "system.power_plan": {"plan": KEEP}, "system.shutdown": {"mode": KEEP},
    "system.volume.mute": {"state": KEEP},
    "timer.set": {"duration": KEEP, "label": INTENT},
    "ui.click": {"label": INTENT, "app": KEEP, "kind": KEEP}, "ui.right_click": {"label": INTENT, "app": KEEP, "kind": KEEP},
    "ui.drag": {"source": INTENT, "target": INTENT, "app": KEEP},
    "ui.fill": {"field": INTENT, "text": OMIT, "app": KEEP},
    "ui.inspect": {"app": KEEP}, "ui.read": {"app": KEEP}, "ui.scroll": {"direction": KEEP, "app": KEEP},
    "weather.now": {"location": INTENT},
    "web.download": {"url": URL, "filename": PATH}, "web.fetch": {"url": URL}, "web.open": {"url": URL},
    "web.search": {"query": INTENT},
    "whatsapp.compose": {"contact": INTENT, "message": OMIT},
    "window.maximize": {"app": KEEP}, "window.minimize": {"app": KEEP}, "window.restore": {"app": KEEP},
    "window.snap": {"side": KEEP, "app": KEEP},
    # no longer registered, but old rows and old episodes carry them
    "comms.send_message": {"contact": INTENT, "text": OMIT},
}

# A skill whose `result` speech is content that was READ BACK (a file, the clipboard, a page, command output).
# Only its length is stored.
CONTENT_RESULT_SKILLS = frozenset({  # outputs of 80 chars or fewer are kept (scrubbed); longer ones keep only their length
    "files.read", "clipboard.read", "memory.recall", "memory.list", "notes.read", "screen.read_text", "screen.observe",
    "ui.read", "ui.inspect", "browser.read", "browser.inspect", "web.fetch", "knowledge.ask", "shell.run",
    "dev.python", "dev.git",
})

_CREDENTIAL_KEY = re.compile(
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential|"
    r"authori[sz]ation|bearer|cookie|session[_-]?id|otp|cvv|ssn|(?<![a-z])pin(?![a-z])",
    re.I,
)


# Forms this module itself writes. Recognising them makes every function here a FIXED POINT
# (f(f(x)) == f(x)), which is what lets `friday.dbscrub` re-run over history without drifting.
_OMIT_MARK = re.compile(
    r"^\[(?:omitted: \d+ (?:chars(?:, \d+ lines)?|items|bytes)|omitted|redacted|redacted opaque string"
    r"|read-back content not stored: \d+ chars"
    r"|omitted: (?:could not be scrubbed|unparseable url|non-web url|result could not be scrubbed"
    r"|error could not be scrubbed|arguments could not be summarised safely))\]$"
)
_BOUND_MARK = re.compile(r"\.\.\.\[\+\d+ chars\]$")
_ARGV_MARK = re.compile(r"^([A-Za-z0-9_.+\-]{1,60}|\(command\)) \[omitted: \d+ args, \d+ chars\]$")
_URL_NOTE = " [query/credentials not stored]"
# http(s) URL whose query or fragment follows: the part up to ? or # survives, the rest does not.
_URL_ANY = re.compile(r"\b([a-z][a-z0-9+.\-]{1,15})://(?:([^\s/?#@]*)@)?([^\s/?#]*)([^\s?#]*)([?#]\S*)?", re.I)
MIN_ECHO = 6  # an omitted value shorter than this is not searched for in other fields (too ambiguous)


def strip_query_urls(text: str) -> str:
    """`https://h/p?token=x#frag` -> `https://h/p [query/credentials not stored]`. Idempotent."""
    def one(m: re.Match[str]) -> str:
        scheme, userinfo, host, path, tail = m.groups()
        if userinfo is None and not tail:
            return m.group(0)
        return f"{scheme}://{host}{path}{_URL_NOTE}"

    return _URL_ANY.sub(one, text)


def _scrub(text: str) -> str:
    try:
        from friday.toolview import sanitize

        return strip_query_urls(sanitize(text, prose=True))
    except Exception:  # noqa: BLE001 -- a broken scrubber must not turn into stored plain text
        return "[omitted: could not be scrubbed]"


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    m = _BOUND_MARK.search(text)
    if m and m.start() <= limit:  # already bounded by us
        return text
    return f"{text[:limit]}...[+{len(text) - limit} chars]"


def content_values(skill: str, args: Any) -> list[str]:
    """The raw strings of `args` that the policy will NOT store (OMIT-mode, long enough to search for).
    Used only in memory, to remove echoes of them from the skill's spoken result / error."""
    if not isinstance(args, dict):
        return []
    modes = POLICY.get(skill, {})
    found: list[str] = []
    for key, value in args.items():
        if modes.get(str(key), OMIT) != OMIT:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, str) and len(item.strip()) >= MIN_ECHO and not _OMIT_MARK.match(item):
                found.append(item.strip())
    return sorted(set(found), key=len, reverse=True)


_DERIVED_MIN = 12
_DERIVED_BUDGET = 4_000_000  # len(text) * len(value) above this is not searched for derived echoes


def redact_echoes(text: str, values: Any) -> str:
    """Replace every occurrence of an omitted argument value inside `text` by its length marker. A skill often
    repeats only PART of what it was given ("Noted - <text without the lead-in>", a truncated "Typed 'abc...'"),
    so a run of 12+ characters shared with the value is replaced too."""
    if not isinstance(text, str):
        return text
    import difflib

    for v in values or ():
        if not v:
            continue
        if v in text:
            text = text.replace(v, f"[omitted: {len(v)} chars]")
        elif len(v) >= _DERIVED_MIN and len(text) * len(v) <= _DERIVED_BUDGET:
            for _ in range(8):
                m = difflib.SequenceMatcher(None, text, v, autojunk=False).find_longest_match(0, len(text), 0, len(v))
                if m.size < _DERIVED_MIN:
                    break
                text = text[: m.a] + f"[omitted: {m.size} chars]" + text[m.a + m.size:]
    return text


def scrub_free_text(text: str | None, echoes: Any = ()) -> str | None:
    """Episode `errors` / `context`: echoes of omitted values and URL query/fragment/userinfo, nothing else."""
    if not isinstance(text, str):
        return text
    return strip_query_urls(redact_echoes(text, echoes))


def _marker(value: Any) -> str:
    if isinstance(value, str):
        lines = value.count("\n") + 1 if value else 0
        return f"[omitted: {len(value)} chars" + (f", {lines} lines]" if lines > 1 else "]")
    if isinstance(value, (list, tuple, dict)):
        return f"[omitted: {len(value)} items]"
    return "[omitted]"


def _url(value: str) -> str:
    had_note = value.endswith(_URL_NOTE)
    if had_note:
        value = value[: -len(_URL_NOTE)]
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() in ("data", "javascript", "vbscript", "blob"):
            return "[omitted: non-web url]"
        if not parts.scheme or not parts.hostname:
            return _bound(_scrub(value.split("?", 1)[0].split("#", 1)[0]), _MAX_KEEP) + (_URL_NOTE if had_note else "")
        port = f":{parts.port}" if parts.port else ""
        out = f"{parts.scheme}://{parts.hostname}{port}{parts.path}"
        dropped = had_note or bool(parts.query or parts.fragment or parts.username or parts.password)
        return _bound(out, _MAX_KEEP) + (_URL_NOTE if dropped else "")
    except ValueError:
        return "[omitted: unparseable url]"


def _argv0(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if _ARGV_MARK.match(text):
        return text  # already "<exe> [omitted: N args, M chars]"
    first = text.split(None, 1)[0].strip("\"'")
    exe = os.path.basename(first.replace("\\", "/")) or first
    if "=" in exe or exe.startswith(("$", "%")) or not re.fullmatch(r"[A-Za-z0-9_.+\-]{1,60}", exe):
        exe = "(command)"  # an env assignment / expression / odd token is not an executable name
    rest = len(text.split()) - 1
    return f"{_bound(exe, 60)} [omitted: {rest} args, {len(text)} chars]" if rest else _bound(exe, 60)


def _field(mode: str, value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and _OMIT_MARK.match(value.strip()):
        return value  # one of our own markers: leave it exactly as it is
    if mode == OMIT or not isinstance(value, str):
        return _marker(value)
    if mode == KEEP:
        return _bound(_scrub(value), _MAX_KEEP)
    if mode == PATH:
        return _bound(value, _MAX_PATH)
    if mode == URL:
        return _url(value)
    if mode == INTENT:
        return _bound(_scrub(value), _MAX_INTENT)
    if mode == ARGV0:
        return _argv0(value)
    return _marker(value)


def _fixed(fn: Callable[[Any], Any], value: Any) -> Any:
    """Apply `fn` until its output stops changing (<=4 rounds), so every public sanitizer is idempotent by
    construction. If it never settles, fall back to a length marker, which is stable."""
    out = fn(value)
    for _ in range(4):
        try:
            nxt = fn(out)
        except Exception:  # noqa: BLE001
            break
        if json.dumps(nxt, default=str, sort_keys=True) == json.dumps(out, default=str, sort_keys=True):
            return out
        out = nxt
    return _marker(value if isinstance(value, (str, list, tuple, dict)) else str(value))


def sanitize_args(skill: str, args: Any) -> Any:
    """The form of `args` that may be stored. Pure; never raises; never mutates `args`."""
    return _fixed(lambda a: _sanitize_args_once(skill, a), args)


def _sanitize_args_once(skill: str, args: Any) -> Any:
    """The form of `args` that may be stored. Pure; never raises; never mutates `args`."""
    try:
        if not isinstance(args, dict):
            if isinstance(args, str) and _OMIT_MARK.match(args.strip()):
                return args  # one of our own markers (a non-object args value scrubbed earlier): fixed point
            return _marker(args) if isinstance(args, (str, list, tuple)) else args
        modes = POLICY.get(skill, {})
        out: dict[str, Any] = {}
        for key, value in args.items():
            k = str(key)
            if _CREDENTIAL_KEY.search(k) and not isinstance(value, (bool, type(None))):
                out[k] = "[redacted]"
            else:
                out[k] = _field(modes.get(k, OMIT), value)
        return out
    except Exception:  # noqa: BLE001 -- storing less is always the safe failure
        return {"_unavailable": "[omitted: arguments could not be summarised safely]"}


def sanitize_result(skill: str, text: str | None, echoes: Any = ()) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        return f"[omitted: {len(text)} bytes]" if isinstance(text, (bytes, bytearray)) else "[omitted]"
    return _fixed(lambda t: _sanitize_result_once(skill, t, echoes), text)


def _sanitize_result_once(skill: str, text: str | None, echoes: Any = ()) -> str | None:
    """The spoken outcome as it may be stored. Read-back content is reduced to its length, and any omitted
    argument value the skill repeated back ("Composed 'hello ...' to Sam") is replaced by its marker."""
    if text is None:
        return None
    try:
        text = redact_echoes(text, echoes)
        if skill in CONTENT_RESULT_SKILLS:
            return f"[read-back content not stored: {len(text)} chars]" if len(text) > 80 else _scrub(text)
        return _bound(_scrub(text), _MAX_RESULT)
    except Exception:  # noqa: BLE001
        return "[omitted: result could not be scrubbed]"


def sanitize_error(text: str | None, echoes: Any = ()) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        return f"[omitted: {len(text)} bytes]" if isinstance(text, (bytes, bytearray)) else "[omitted]"
    return _fixed(lambda t: _sanitize_error_once(t, echoes), text)


def _sanitize_error_once(text: str | None, echoes: Any = ()) -> str | None:
    if text is None:
        return None
    try:
        return _bound(_scrub(redact_echoes(text, echoes)), _MAX_ERROR)
    except Exception:  # noqa: BLE001
        return "[omitted: error could not be scrubbed]"


def unclassified_params(skills: Any) -> list[tuple[str, str]]:
    """(skill, param) pairs of text-typed parameters with no policy entry -- for the coverage test."""
    missing: list[tuple[str, str]] = []
    for s in skills:
        for p in getattr(s, "params", []):
            if getattr(p, "type", str) in (bool, int, float):
                continue
            if p.name not in POLICY.get(s.name, {}):
                missing.append((s.name, p.name))
    return missing
