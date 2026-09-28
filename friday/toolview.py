"""Phase 22.0 — what the planner may SEE of a tool's real output.

The planner used to receive only each step's one-line `speech` ("notes.txt has 812
characters. Showing the first part."). The data behind it — the file's content, the
search hits, the window titles — never reached it, so it could not answer from what a tool
had already returned and kept re-calling tools to get it (measured in Phase 21: B2).

`excerpt` builds the bounded text that goes into the planner's history line for one step.
Its contract, all pinned by `scripts/smoke_tool_data.py`:

  * BOUNDED — a hard character cap per step (the caller also caps the whole prompt). A long
    text is shown as its head AND its tail with an explicit "[N chars omitted]" marker, so
    truncation is visible, never silent, and a last line / total is not lost with the middle.
  * NEW INFORMATION ONLY — a value the step's speech already states is not repeated.
  * NO SECRETS — key names that look like credentials are redacted whole; credential-shaped
    text (passwords/tokens/API keys/private keys/JWTs/card numbers/URLs with a password) is
    redacted wherever it appears; a few tools whose whole output is sensitive are withheld.
  * DATA, NOT INSTRUCTIONS — the text is control-character-stripped, prefixed as untrusted
    data, and text that imitates our own protocol (chat role labels, chat template tokens, a
    `{"action": ...}` decision, "ignore previous instructions") is neutralised. This is
    defence in depth only: the real guarantee is structural — tool output never reaches the
    system prompt, the user's goal / scope line / tool list are built from the user's own
    words and the registry, and every decision is still checked by the intent gate, the
    permission tier and the confirmation prompt, none of which read tool output.

Pure functions, no I/O, no model call.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Data keys that never help the planner (bookkeeping the orchestrator or a tool adds).
_SKIP_KEYS = frozenset({
    "speech", "verification", "uncertain", "ok", "status", "previous_result", "previous_ok",
    "previous_step", "args", "tool", "reason", "action_class", "allowed", "hwnd", "pid",
})
# Control fields that say how to get MORE / whether this is all of it: tiny, and worth more than
# any amount of content — rendered before it, so a long `content` can never crowd out the cursor.
_LEAD_KEYS = ("truncated", "next_offset", "next_page")
# Then the fields that usually ARE the answer.
_PRIORITY_KEYS = (
    "content", "text", "body", "results", "matches", "items", "lines", "windows", "titles", "memories", "notes",
    "jobs", "summary", "error", "path", "total_chars", "offset",
)
# Tools whose output is, as a whole, likely to hold something private the user never meant to
# put in a prompt (a copied password, a stored personal fact). Withheld entirely.
_SENSITIVE_TOOLS = frozenset({"clipboard.read", "memory.recall", "memory.list"})

_SECRET_KEY = re.compile(
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"credential|authori[sz]ation|bearer|cookie|session[_-]?id|otp|cvv|ssn",
    re.I,
)

_KEYWORD = (
    r"(?:pass(?:word|wd|phrase)?(?![a-z])|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"auth(?:orization)?(?![a-z])|credentials?)"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[redacted private key]"),
    # An HTTP Authorization header's value is scheme + credential ("Basic <base64>", "Bearer <token>",
    # "Digest ..."), two tokens — the generic `key=value` rule below only swallows one `\S+` token and
    # would leave a Basic/Digest credential exposed right after "Authorization=[redacted]". Redact the
    # whole header value (to end of line) first.
    (re.compile(r"(?im)\bauthorization\s*:\s*[^\r\n]+"), "Authorization: [redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    # `NAME = value` where NAME contains a credential word (db_password, API_KEY, x-auth-token ...)
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*" + _KEYWORD + r"[A-Za-z0-9_.-]*[\"']?)\s*[:=]\s*(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)"), r"\1=[redacted]"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[redacted token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[redacted key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[redacted key]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[redacted key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[redacted key]"),
)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_-]{40,}\b)(?=[A-Za-z_-]*\d)(?=[0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")

_INJECTION: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)^\s*(?:system|assistant|user|human|developer|tool)\s*:"), "(role label removed):"),
    (re.compile(r"<\|[^|>\n]{1,40}\|>"), "(chat token removed)"),
    (re.compile(r"(?i)</?\s*(?:system|assistant|user|instructions?)\s*>"), "(tag removed)"),
    (re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>"), "(tag removed)"),
    (re.compile(r"(?i)\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,50}\b(?:previous|prior|above|earlier|all|any|the)\b"
                r"[^.\n]{0,50}\b(?:instructions?|rules|prompts?|constraints|guidelines|restrictions|safety)\b"), "(instruction-like text removed)"),
    (re.compile(r"(?i)\b(?:new|updated|real)\s+(?:system\s+)?(?:instructions?|rules)\s*:"), "(instruction-like text removed):"),
    (re.compile(r"(?i)\byou\s+(?:are|must)\s+now\b[^.\n]{0,80}"), "(instruction-like text removed)"),
    (re.compile(r"(?i)\b(?:do\s*n[o']?t|never)\s+(?:tell|inform|ask|warn)\s+the\s+user\b[^.\n]{0,40}"), "(instruction-like text removed)"),
    (re.compile(r"[\"']action[\"']\s*:\s*[\"'](?:call|done|ask)[\"']"), '"action (quoted text)"'),
)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[redacted number]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return _CARD.sub(repl, text)


def sanitize(text: str) -> str:
    """Make a piece of tool output safe to show the planner: strip control/bidi characters,
    redact credential-shaped text, neutralise text that imitates our own protocol or tries
    to give the model instructions. Idempotent."""
    text = _CTRL.sub("", text)
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    text = _redact_cards(text)
    text = _OPAQUE.sub("[redacted opaque string]", text)
    for pattern, repl in _INJECTION:
        text = pattern.sub(repl, text)
    return text


def _short(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"...(+{len(text) - limit} more chars)"


def _one_line(text: str) -> str:
    """Squeeze runs of blanks and blank lines. The JSON quoting in `_render_value` then shows
    each remaining newline as a literal backslash-n, so a multi-line value stays one bounded run."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.replace("\r", "")


_RAW_CAP = 20000  # never run the sanitizer over more than this much of one value


def _scalar(value: Any) -> str | None:
    """A value as text — with every STRING passed through `sanitize` here, on the raw text,
    before anything quotes, joins or truncates it (so a line-anchored or word-boundary rule
    still sees the original line breaks). A value longer than `_RAW_CAP` is cut first; the
    tool's own `truncated` / `total_chars` fields carry the true size."""
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return sanitize(value[:_RAW_CAP])
    return None


def _item(value: Any) -> str:
    """One list element, compactly."""
    s = _scalar(value)
    if s is not None:
        return _short(s, 80)
    if isinstance(value, dict):
        for key in ("path", "title", "name", "value", "text", "label", "tool"):
            if isinstance(value.get(key), str) and value[key].strip():
                extra = [
                    f"{k}={_short(_scalar(v) or '', 24)}" for k, v in value.items()
                    if k != key and k not in _SKIP_KEYS and isinstance(v, (int, float, str)) and not isinstance(v, bool)
                    and not _SECRET_KEY.search(k)
                ][:2]
                return _short(_scalar(value[key]) or "", 80) + (f" ({', '.join(extra)})" if extra else "")
        parts = [
            f"{k}={_short(_scalar(v) or '', 24)}" for k, v in value.items()
            if k not in _SKIP_KEYS and isinstance(v, (int, float, str)) and not _SECRET_KEY.search(k)
        ][:3]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_short(_scalar(v) or "...", 24) for v in list(value)[:4]) + ("..." if len(value) > 4 else "") + "]"
    return _short(str(value), 40)


def _head_tail(text: str, limit: int) -> str:
    """A long text as its beginning AND its end, with the omitted size stated. A page of a
    document has its answer at either edge as often as at the start (a heading, a last line,
    a total); showing only the head would hide the tail from the planner for good."""
    text = text.strip()
    if len(text) <= limit:
        return text
    room = max(24, limit - 32)  # the marker below costs ~28 characters
    head = max(12, int(room * 0.65))
    tail = max(8, room - head)
    omitted = len(text) - head - tail
    return f"{text[:head].rstrip()} ...[{omitted} chars omitted]... {text[-tail:].lstrip()}"


def _render_value(value: Any, budget: int) -> str | None:
    s = _scalar(value)
    if s is not None:
        return json.dumps(_head_tail(_one_line(s), budget), ensure_ascii=False) if isinstance(value, str) else s
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        shown = [_item(v) for v in list(value)[:6]]
        more = f" (+{len(value) - 6} more)" if len(value) > 6 else ""
        return "[" + "; ".join(shown) + "]" + more
    if isinstance(value, dict):
        return _item(value) if value else None
    return None


def _mentioned(value: Any, speech_low: str) -> bool:
    """Is this (short) value already stated by the step's speech? Then it is not new."""
    s = _scalar(value)
    if s is None or not speech_low:
        return False
    s = s.strip().casefold()
    return 0 < len(s) <= 60 and s in speech_low


def excerpt(tool: str, data: dict[str, Any] | None, *, speech: str = "", max_chars: int = 500) -> str:
    """The bounded, sanitized "data:" text for one step, or "" when the step has nothing the
    speech does not already say (or `max_chars` is 0). Never longer than ~`max_chars`."""
    if not data or max_chars <= 0:
        return ""
    if tool in _SENSITIVE_TOOLS:
        return "(output withheld: it may contain private information)"

    speech_low = " ".join((speech or "").split()).casefold()
    ordered = _LEAD_KEYS + _PRIORITY_KEYS
    keys = [k for k in ordered if k in data] + [k for k in data if k not in ordered]
    parts: list[str] = []
    used = 0
    for key in keys:
        if key in _SKIP_KEYS:
            continue
        value = data[key]
        if key not in _LEAD_KEYS and _mentioned(value, speech_low):  # (a cursor is always shown)
            continue
        if _SECRET_KEY.search(str(key)):
            piece = f"{key}=[redacted]"
        else:
            room = max(40, max_chars - used - len(str(key)) - 3)
            rendered = _render_value(value, room)
            if rendered is None:
                continue
            piece = f"{key}={sanitize(rendered)}"
        if used and used + len(piece) + 2 > max_chars:
            piece = _short(piece, max(0, max_chars - used - 2))
            if len(piece) < 12:
                break
        parts.append(piece)
        used += len(piece) + 2
        if used >= max_chars:
            break
    if not parts:
        return ""
    out = "; ".join(parts)
    # A hard ceiling: JSON quoting/escaping and the per-key prefixes can push a rendered piece
    # past its budget; the caller's whole-prompt cap relies on this never exceeding `max_chars`.
    return out if len(out) <= max_chars else out[: max(0, max_chars - 3)].rstrip() + "..."
