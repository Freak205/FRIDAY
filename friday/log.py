"""Logging setup: rich console output plus a rotating file log."""

from __future__ import annotations

import logging
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
