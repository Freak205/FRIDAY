"""Logging setup: rich console output plus a rotating file log."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

from friday import paths
from friday.config import CFG

_configured = False


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
