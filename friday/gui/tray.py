"""Native Qt tray icon — replaces `friday/desktop.py`'s `pystray` tray now
that `QApplication.exec()` owns the main thread. One fewer thread, one
fewer dependency; menu clicks land on the Qt thread for free instead of
needing their own marshal-back dance.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from friday import __version__
from friday.config import CFG
from friday.log import get
from friday.registry import REGISTRY

from . import theme

log = get(__name__)


def _draw_icon() -> QIcon:
    """Same two-circle motif as the old `_icon_image()` (friday/desktop.py),
    drawn directly with QPainter instead of round-tripping through Pillow."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.ACCENT_SOFT))
    painter.drawEllipse(4, 4, 56, 56)
    painter.setBrush(QColor(theme.WINDOW_BG))
    painter.drawEllipse(20, 20, 24, 24)
    painter.end()
    return QIcon(pixmap)


def build_tray(
    *,
    on_toggle_window: Any,
    on_quit: Any,
) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(_draw_icon())
    tray.setToolTip(CFG.identity.name)

    menu = QMenu()

    title_action = menu.addAction(f"{CFG.identity.name} v{__version__}")
    title_action.setEnabled(False)

    skills_action = menu.addAction(f"{len(REGISTRY)} skills")
    skills_action.setEnabled(False)

    menu.addSeparator()

    show_action = menu.addAction("Show / Hide")
    show_action.triggered.connect(on_toggle_window)

    pause_action = menu.addAction("Pause all jobs")
    pause_action.triggered.connect(_pause_all_jobs)

    menu.addSeparator()

    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(on_quit)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: on_toggle_window()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    return tray


def _pause_all_jobs() -> None:
    try:
        from friday.jobs import SCHEDULER, all_jobs, set_enabled

        for job in all_jobs(enabled_only=True):
            set_enabled(job.id, False)
        SCHEDULER.reload()
        log.info("all jobs paused from tray")
    except Exception:
        log.exception("pause-all-jobs failed")
