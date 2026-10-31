"""Logging setup: rich console output plus a rotating file log."""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

from friday import paths
from friday.config import CFG

_configured = False

WITHHELD_LOG = "[log message withheld: it could not be checked for credentials]"


def _redact_log_text(text: str) -> str:
    """Credentials named in sentences / `key: value` shapes, URL query strings, and the daemon token itself, wherever they appear."""
    import os

    from friday import auditview

    token = os.environ.get("FRIDAY_DAEMON_TOKEN", "").strip()
    if token:
        text = text.replace(token, "[redacted]")
    out = auditview.scrub_free_text(text)
    return WITHHELD_LOG if out == auditview.WITHHELD_TEXT else out


class RedactingFilter(logging.Filter):
    """Phase 64: every line that reaches a handler -- its message, any exception message and traceback, any stack -- is credential-
    checked once, here, so a call site cannot forget. The exception TYPE, the frames and the logger / level / time are kept; only
    recognisable credential text changes. The traceback is rendered into the (checked) message because Python would otherwise
    format `exc_info` AFTER this filter. If the check itself fails the line keeps its logger and level and loses its text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                text = f"{text}\n{logging.Formatter().formatException(record.exc_info)}"
            if record.stack_info:
                text = f"{text}\n{record.stack_info}"
            record.msg, record.args = _redact_log_text(text), ()
        except Exception:  # noqa: BLE001 -- fail closed
            record.msg, record.args = WITHHELD_LOG, ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


EXC_WITHHELD = "[exception details withheld: they could not be checked for credentials]"
EXC_UNREADABLE = "[message unreadable]"
_EXC_MESSAGE_MAX = 300
_EXC_CHAIN_MAX = 5
_EXC_FRAMES_MAX = 8
_EXC_SCAN_MAX = 8000   # a message is credential-checked up to here; the rest is only counted (a multi-megabyte body is not scanned for a log line)
# control characters, the C1 range (U+0085 is a line break), zero-width and bidi marks, line/paragraph separators: none may start a line or reorder one
_CTRL_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]+")


def _exc_where(exc: BaseException) -> str:
    """Where it was raised, innermost frames last: `file.py:12 in func > other.py:40 in run`. File NAME, line and function only --
    never the source text, never a local variable or an argument."""
    frames = [(f.f_code.co_filename, lineno, f.f_code.co_name) for f, lineno in traceback.walk_tb(exc.__traceback__)]
    shown = frames[-_EXC_FRAMES_MAX:]
    text = " > ".join(f"{os.path.basename(path)}:{lineno} in {name}" for path, lineno, name in shown)
    return (f"... > {text}" if len(frames) > len(shown) else text) or "no traceback"


def _exc_one(exc: BaseException) -> str:
    """`Type: message [where]` for ONE exception. The type name and the location are kept as they are; the message is credential-
    checked (`_redact_log_text`, the filter's own check), flattened to one line and bounded. A message that cannot be read or
    checked is replaced by a fixed notice; the type and the location stay."""
    name = type(exc).__name__
    try:
        raw = str(exc)
        total, cut = len(raw), len(raw) > _EXC_SCAN_MAX
        message = _CTRL_LINE.sub(" ", _redact_log_text(raw[:_EXC_SCAN_MAX])).strip()
        if cut:
            # the check never saw the rest, and a credential may straddle the cut: what the check shortened must not let a
            # half-token at the edge reach the line, so the last stretch before the cut is dropped as well
            message = message[: max(0, min(len(message) - 64, _EXC_MESSAGE_MAX))]
            message = f"{message}...[+{total - len(message)} chars]"
        elif len(message) > _EXC_MESSAGE_MAX:
            message = f"{message[:_EXC_MESSAGE_MAX]}...[+{len(message) - _EXC_MESSAGE_MAX} chars]"
    except Exception:  # noqa: BLE001 -- a broken __str__ or a broken check: never fall back to the raw text
        message = EXC_UNREADABLE
    try:
        where = _exc_where(exc)
    except Exception:  # noqa: BLE001
        where = "location unavailable"
    return f"{name}: {message} [{where}]" if message else f"{name} [{where}]"


def safe_exc_text(exc: BaseException | None = None) -> str:
    """Phase 98: an exception as ONE log-safe line -- type, credential-checked message, location -- followed by its chain
    (`__cause__`, else the exception being handled when it was raised), each link described the same way. Python's own traceback
    formatting prints every message in a chain verbatim; this checks each one. Bounded (5 links, 8 frames each, 300 chars a message).
    Without an argument it describes the exception being handled. If the description cannot be produced at all the result is a
    fixed notice that still names the exception type. Never raises."""
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return "no exception was active"
        parts: list[str] = []
        seen: set[int] = set()
        link, current = "", exc
        while current is not None and id(current) not in seen and len(parts) < _EXC_CHAIN_MAX:
            seen.add(id(current))
            parts.append(f"{link}{_exc_one(current)}")
            if current.__cause__ is not None:
                current, link = current.__cause__, "caused by "
            elif current.__context__ is not None and not current.__suppress_context__:
                current, link = current.__context__, "during handling of "
            else:
                current = None
        if current is not None and id(current) not in seen:
            parts.append("(more chained exceptions not shown)")
        return " | ".join(parts)
    except Exception:  # noqa: BLE001 -- fail closed with a fixed diagnostic
        try:
            return f"{type(exc).__name__}: {EXC_WITHHELD}"
        except Exception:  # noqa: BLE001
            return EXC_WITHHELD


def log_exception(logger: logging.Logger, message: str, *args: object, exc: BaseException | None = None, level: int = logging.ERROR) -> None:
    """`logger.exception(message, *args)` for a path whose exception text may carry what the user or a tool handled (Phase 98): the
    record carries `safe_exc_text` instead of the exception object, so no handler -- this module's, a test's or a later one -- is
    ever handed the raw message, a chained message or a traceback to format. `message` takes `%` placeholders for `args`, as for any
    logger call (a literal `%` with no arguments, or arguments that do not fit, never raises a logging error). Call it inside the
    `except` block (or pass `exc=`). Nothing is done -- the exception is not even described -- when the level is switched off."""
    if not logger.isEnabledFor(level):
        return
    try:
        head = message % args if args else message
    except Exception:  # noqa: BLE001 -- the template as written, never a logging error
        head = message
    logger.log(level, "%s -- %s", head, safe_exc_text(exc))


def setup() -> None:
    global _configured
    if _configured:
        return
    paths.ensure()

    root = logging.getLogger()
    root.setLevel(CFG.logging.level.upper())

    console = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    console.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))

    file_handler = RotatingFileHandler(
        paths.LOGS / "friday.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-24s %(message)s")
    )

    for handler in (console, file_handler):
        handler.addFilter(RedactingFilter())
    root.handlers = [console, file_handler]

    # These are chatty and rarely useful.
    for noisy in ("uvicorn.access", "httpx", "asyncio", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Third-party libraries that log benign failures at WARNING or above:
    # screen_brightness_control warns on every laptop panel with a non-standard
    # EDID, and comtypes narrates its generated-module cache on import.
    for chatty in ("screen_brightness_control", "comtypes", "comtypes.client",
                   "comtypes._ComObject", "PIL"):
        logging.getLogger(chatty).setLevel(logging.ERROR)

    _configured = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
