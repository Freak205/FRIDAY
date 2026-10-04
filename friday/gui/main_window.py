"""The main command-center window: composition/layout only. All state comes
from `GuiStateHub` signals; all actions go through `Backend`/the existing
voice pipeline — nothing here decides anything on its own.

Composition (see PLAN §10.x's design pass): the FRIDAY core is the hero,
sized to dominate the center column; a restrained telemetry column sits to
its left, task/environment awareness to its right, and a single-line command
channel anchors the bottom. Nothing here is a new state machine — every
label is a direct projection of a `GuiStateHub` signal or `Backend`/`CFG`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

from friday.config import CFG
from friday.log import get

from . import geometry_store, theme
from .backend import Backend
from .command_bar import CommandBar
from .confirm_panel import ConfirmPanel
from .core_widget import CoreWidget
from .desktop_context_panel import DesktopContextPanel
from .response_area import ResponseArea
from .state_hub import CoreState, GuiStateHub
from .task_panel import TaskPanel
from .telemetry_panel import TelemetryPanel

log = get(__name__)

_STATUS_TEXT: dict[CoreState, str] = {
    CoreState.BOOTING: "INITIALIZING",
    CoreState.IDLE: "STANDBY",
    CoreState.LISTENING: "LISTENING",
    CoreState.THINKING: "PROCESSING",
    CoreState.EXECUTING: "EXECUTING",
    CoreState.AWAITING_CONFIRM: "AWAITING CONFIRMATION",
    CoreState.SPEAKING: "SPEAKING",
    CoreState.ERROR: "SYSTEM ERROR",
}

_SECONDARY_TEXT: dict[CoreState, str] = {
    CoreState.BOOTING: "STARTING UP",
    CoreState.IDLE: "SYSTEM NOMINAL",
    CoreState.LISTENING: "",
    CoreState.THINKING: "UNDERSTANDING REQUEST",
    CoreState.EXECUTING: "",  # filled from SELF_STATE.current_task when available
    CoreState.AWAITING_CONFIRM: "AUTHORIZATION REQUIRED",
    CoreState.SPEAKING: "",
    CoreState.ERROR: "",  # filled from SELF_STATE.detail when available
}


def _format_task(task: str) -> str:
    if not task:
        return "WORKING"
    return task.replace("_", " ").replace(".", " ").strip().upper()[:44]


class _CentralCanvas(QWidget):
    """Paints the very subtle depth cues behind everything else: a faint
    radial brightening toward the core's home, and a barely-there reference
    grid — negative space with intent, not an empty rectangle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._focus_y_ratio = 0.42  # where the core roughly sits, vertically

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        painter.fillRect(self.rect(), QColor(theme.WINDOW_BG))

        cx, cy = w * 0.5, h * self._focus_y_ratio
        radius = max(w, h) * 0.75
        gradient = QRadialGradient(cx, cy, radius)
        gradient.setColorAt(0.0, QColor("#0c0f15"))
        gradient.setColorAt(1.0, QColor(theme.WINDOW_BG))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRect(self.rect())

        grid_color = QColor(theme.BORDER)
        grid_color.setAlpha(14)
        painter.setPen(grid_color)
        step = 56
        for x in range(0, w, step):
            painter.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            painter.drawLine(0, y, w, y)

        painter.end()


class _StartupOverlay(QWidget):
    """A brief, honest boot checklist — rows light up as `Backend.stages`
    actually completes, never on a fabricated timer (PLAN §23)."""

    _STEPS = [
        ("memory", "MEMORY"), ("registry", "SKILLS"),
        ("brain", "CORE"), ("automation", "AUTOMATION"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("bootOverlay")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        wordmark = QLabel(CFG.identity.name.upper())
        wordmark.setObjectName("wordmark")
        wordmark.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(wordmark)

        title = QLabel("INITIALIZING")
        title.setObjectName("channelLabel")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)

        rows_wrap = QVBoxLayout()
        rows_wrap.setSpacing(5)
        layout.addLayout(rows_wrap)

        self._rows: dict[str, QLabel] = {}
        for key, label in self._STEPS:
            row = QLabel(f"{label:<10}··········  —")
            row.setObjectName("microLabel")
            row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            rows_wrap.addWidget(row)
            self._rows[key] = row

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)
        self._fade_anim: QPropertyAnimation | None = None

    def update_stages(self, stages: list[str]) -> None:
        completed = {s.split(":", 1)[0] for s in stages}
        for key, label in self._STEPS:
            state = "ONLINE" if key in completed else "—"
            self._rows[key].setText(f"{label:<10}··········  {state}")
            if key in completed:
                self._rows[key].setStyleSheet(f"color: {theme.ACCENT};")

    def fade_out(self, on_finished: Any) -> None:
        anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        anim.setDuration(380)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(on_finished)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade_anim = anim


class MainWindow(QMainWindow):
    def __init__(self, backend: Backend, hub: GuiStateHub) -> None:
        super().__init__()
        self._backend = backend
        self._hub = hub
        self._last_self_state: dict[str, Any] = {}

        self.setWindowTitle(CFG.identity.name)
        self.setMinimumSize(900, 680)

        central = _CentralCanvas()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(theme.SPACING)

        root.addLayout(self._build_header())
        root.addWidget(theme.hairline())

        body = QHBoxLayout()
        body.setSpacing(theme.SPACING * 2)

        self._telemetry = TelemetryPanel()
        self._telemetry.setMinimumWidth(180)
        self._telemetry.setMaximumWidth(230)
        body.addWidget(self._telemetry)

        body.addLayout(self._build_center(), 1)

        right_col = QVBoxLayout()
        right_col.setSpacing(theme.SPACING)
        self._task_panel = TaskPanel()
        self._context_panel = DesktopContextPanel()
        right_col.addWidget(self._task_panel)
        right_col.addWidget(self._context_panel)
        right_col.addStretch(1)
        right_wrap = QWidget()
        right_wrap.setLayout(right_col)
        right_wrap.setMinimumWidth(210)
        right_wrap.setMaximumWidth(280)
        body.addWidget(right_wrap)

        root.addLayout(body, 1)

        self._command_bar = CommandBar(backend)
        root.addWidget(self._command_bar)

        self._confirm_panel = ConfirmPanel(backend, central)
        self._startup_overlay = _StartupOverlay(central)
        self._startup_overlay.show()

        self._wire_hub()
        self._restore_geometry()
        self._sync_overlay_geometry()

    # -- layout helpers -------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()

        brand_col = QVBoxLayout()
        brand_col.setSpacing(2)
        wordmark = QLabel(CFG.identity.name.upper())
        wordmark.setObjectName("wordmark")
        brand_col.addWidget(wordmark)
        tagline = QLabel("PERSONAL ARTIFICIAL INTELLIGENCE")
        tagline.setObjectName("tagline")
        brand_col.addWidget(tagline)
        row.addLayout(brand_col)

        row.addStretch(1)

        status_col = QVBoxLayout()
        status_col.setSpacing(6)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addStretch(1)
        self._status_pill = QLabel("● INITIALIZING")
        self._status_pill.setObjectName("statusPill")
        status_row.addWidget(self._status_pill)
        status_col.addLayout(status_row)

        pin_row = QHBoxLayout()
        pin_row.addStretch(1)
        self._pin_control = QPushButton("PIN — OFF")
        self._pin_control.setObjectName("pinControl")
        self._pin_control.setCheckable(True)
        self._pin_control.setChecked(CFG.gui.always_on_top)
        self._pin_control.toggled.connect(self._on_pin_toggled)
        self._apply_pin_label(self._pin_control.isChecked())
        pin_row.addWidget(self._pin_control)
        status_col.addLayout(pin_row)

        row.addLayout(status_col)
        return row

    def _build_center(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(10)
        col.addStretch(1)

        self._core = CoreWidget()
        col.addWidget(self._core, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._core_status_label = QLabel("INITIALIZING")
        self._core_status_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._core_status_label.setObjectName("coreStateLabel")
        col.addWidget(self._core_status_label)

        self._core_secondary_label = QLabel("")
        self._core_secondary_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._core_secondary_label.setObjectName("coreSecondaryLabel")
        col.addWidget(self._core_secondary_label)

        col.addSpacing(8)
        self._response_area = ResponseArea()
        col.addWidget(self._response_area, alignment=Qt.AlignmentFlag.AlignHCenter)

        col.addStretch(1)
        return col

    # -- hub wiring -----------------------------------------------------------

    def _wire_hub(self) -> None:
        self._hub.coreStateChanged.connect(self._on_core_state)
        self._hub.selfStateChanged.connect(self._on_self_state)
        self._hub.subsystemStatusChanged.connect(self._telemetry.update_subsystems)
        self._hub.telemetryUpdated.connect(self._telemetry.update_telemetry)
        self._hub.taskStateChanged.connect(self._task_panel.update_task_state)
        self._hub.taskStateChanged.connect(self._command_bar.on_task_state)
        self._hub.orchestratorEvent.connect(self._task_panel.add_ticker_event)
        self._hub.desktopContextUpdated.connect(self._context_panel.update_context)
        self._hub.voiceRawState.connect(self._command_bar.on_voice_raw_state)
        self._hub.voiceRawState.connect(self._response_area.on_voice_raw_state)
        self._hub.confirmRequested.connect(self._confirm_panel.show_request)
        self._hub.confirmResolved.connect(self._confirm_panel.resolve_externally)
        self._hub.backendReady.connect(self._on_backend_ready)
        self._command_bar.exchangeReady.connect(self._response_area.set_exchange)

    def apply_boot_stages(self, stages: list[str]) -> None:
        self._startup_overlay.update_stages(stages)

    def set_mic_activate(self, fn: Any) -> None:
        self._command_bar.set_mic_activate(fn)

    def _on_backend_ready(self, ready: bool) -> None:
        self._startup_overlay.fade_out(self._startup_overlay.hide)
        self._status_pill.setText("● ONLINE" if ready else "● BRAIN OFFLINE")

    def _on_core_state(self, state_value: str) -> None:
        try:
            state = CoreState(state_value)
        except ValueError:
            return
        self._core.set_core_state(state_value)
        self._core_status_label.setText(_STATUS_TEXT.get(state, state.value.upper()))
        if state is not CoreState.BOOTING:
            pill = "● ONLINE"
            if state is CoreState.ERROR:
                pill = "● ERROR"
            elif state is CoreState.AWAITING_CONFIRM:
                pill = "● AWAITING CONFIRMATION"
            self._status_pill.setText(pill)
        self._update_secondary(state)

    def _on_self_state(self, data: dict) -> None:
        self._last_self_state = data
        try:
            state = CoreState(self._core.state)
        except ValueError:
            return
        self._update_secondary(state)

    def _update_secondary(self, state: CoreState) -> None:
        text = _SECONDARY_TEXT.get(state, "")
        if state is CoreState.EXECUTING:
            task = self._last_self_state.get("current_task", "")
            text = _format_task(task)
        elif state is CoreState.ERROR:
            detail = self._last_self_state.get("detail", "")
            text = detail[:60].upper() if detail else "SEE LOG"
        self._core_secondary_label.setText(text)

    # -- window behavior --------------------------------------------------------

    def _apply_pin_label(self, checked: bool) -> None:
        self._pin_control.setText("◉ PINNED" if checked else "PIN — OFF")

    def _on_pin_toggled(self, checked: bool) -> None:
        self._apply_pin_label(checked)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()

    def toggle_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _layout_core_size(self) -> None:
        central = self.centralWidget()
        w, h = central.width(), central.height()
        if w <= 0 or h <= 0:
            return
        side = int(max(260, min(540, min(w, h) * 0.48)))
        self._core.setFixedSize(side, side)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        self._sync_overlay_geometry()

    def _sync_overlay_geometry(self) -> None:
        central = self.centralWidget()
        self._confirm_panel.setGeometry(central.rect())
        self._startup_overlay.setGeometry(central.rect())
        self._layout_core_size()

    def changeEvent(self, event) -> None:  # noqa: N802 — Qt override
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            event.ignore()
            self.hide()
            return
        super().changeEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
        # The window's X button minimizes to tray rather than quitting —
        # the tray menu's "Quit" is the real exit (PLAN §15).
        event.ignore()
        self._save_geometry()
        self.hide()

    def _restore_geometry(self) -> None:
        state = geometry_store.load()
        width, height = state.get("width", 1040), state.get("height", 780)
        self.resize(width, height)
        x, y = state.get("x"), state.get("y")
        if x is not None and y is not None:
            self.move(x, y)
        if state.get("always_on_top"):
            self._pin_control.setChecked(True)

    def _save_geometry(self) -> None:
        geometry_store.save({
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self.height(),
            "always_on_top": self._pin_control.isChecked(),
        })

    def shutdown(self) -> None:
        self._save_geometry()
