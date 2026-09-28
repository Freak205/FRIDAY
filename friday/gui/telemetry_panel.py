"""Restrained system/subsystem status column — atmosphere plus a genuine
status readout, not a performance-monitoring dashboard. Every value comes
from `GuiStateHub`'s `subsystemStatusChanged`/`telemetryUpdated` signals,
which themselves only ever report what the backend actually reported.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from . import theme

_ROWS = [
    ("core", "CORE"), ("brain", "BRAIN"), ("ollama", "OLLAMA"),
    ("voice", "VOICE"), ("microphone", "MIC"), ("wakeword", "WAKE WORD"),
    ("desktop", "DESKTOP"),
]

_OK_VALUES = {"online", "ready", "connected", "active", "observing"}
_WARN_VALUES = {"checking", "booting", "disabled"}
# anything else (offline, unavailable) reads as an error tone


class TelemetryPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACING, theme.SPACING, theme.SPACING, theme.SPACING)
        outer.setSpacing(8)

        header = QLabel("SYSTEM STATUS")
        header.setObjectName("sectionHeader")
        outer.addWidget(header)
        outer.addWidget(theme.hairline())

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        self._value_labels: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(_ROWS):
            name = QLabel(label)
            name.setObjectName("microLabel")
            value = QLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
            self._value_labels[key] = value

        outer.addWidget(theme.hairline())
        cpu_header = QLabel("CPU / RAM")
        cpu_header.setObjectName("microLabel")
        outer.addWidget(cpu_header)
        self._telemetry_label = QLabel("—")
        self._telemetry_label.setObjectName("dimLabel")
        outer.addWidget(self._telemetry_label)
        outer.addStretch(1)

    def update_subsystems(self, status: dict[str, str]) -> None:
        for key, label in self._value_labels.items():
            value = status.get(key, "—")
            label.setText(value.upper())
            if value in _OK_VALUES:
                color = theme.ACCENT
            elif value in _WARN_VALUES:
                color = theme.TEXT_SECONDARY
            else:
                color = theme.ERROR
            label.setStyleSheet(f"color: {color};")

    def update_telemetry(self, data: dict[str, float]) -> None:
        cpu = data.get("cpu_pct")
        ram = data.get("ram_pct")
        if cpu is None or ram is None:
            return
        self._telemetry_label.setText(f"{cpu:4.0f}%  /  {ram:4.0f}%")
