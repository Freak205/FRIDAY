"""Live task panel — reflects `friday.intelligence.state.INTEL`'s snapshot
plus a short orchestrator step ticker. Never fabricates a percentage:
`current_step` is a count, not a percent of an unknown total, so it drives a
✓/◉/○ checklist against the real `current_plan` list rather than a progress
bar. When there's no active goal the panel simply shows the idle label.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

_MAX_TICKER_LINES = 3
_MAX_STEPS_SHOWN = 6


class TaskPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACING, theme.SPACING, theme.SPACING, theme.SPACING)
        outer.setSpacing(6)

        header = QLabel("NO ACTIVE OPERATION")
        header.setObjectName("sectionHeader")
        outer.addWidget(header)
        self._header = header

        self._goal_label = QLabel("")
        self._goal_label.setObjectName("dimLabel")
        self._goal_label.setWordWrap(True)
        self._goal_label.hide()
        outer.addWidget(self._goal_label)

        self._divider = theme.hairline()
        self._divider.hide()
        outer.addWidget(self._divider)

        self._steps_layout = QVBoxLayout()
        self._steps_layout.setSpacing(3)
        outer.addLayout(self._steps_layout)
        self._step_labels: list[QLabel] = []

        outer.addSpacing(4)
        self._ticker_lines: list[QLabel] = []
        for _ in range(_MAX_TICKER_LINES):
            line = QLabel("")
            line.setObjectName("dimLabel")
            line.setWordWrap(True)
            line.hide()
            outer.addWidget(line)
            self._ticker_lines.append(line)

        outer.addStretch(1)
        self._history: list[str] = []

    def update_task_state(self, snapshot: dict) -> None:
        status = snapshot.get("execution_status", "idle")
        request = snapshot.get("current_request", "")

        if status == "idle" or not request:
            self._header.setText("NO ACTIVE OPERATION")
            self._goal_label.hide()
            self._divider.hide()
            self._clear_steps()
            return

        self._header.setText("ACTIVE OPERATION")
        self._goal_label.setText(request[:140])
        self._goal_label.show()

        plan = snapshot.get("current_plan") or []
        step = snapshot.get("current_step", 0)
        if plan:
            self._divider.show()
            self._render_steps(plan, step)
        else:
            self._divider.hide()
            self._clear_steps()

    def _clear_steps(self) -> None:
        for label in self._step_labels:
            label.deleteLater()
        self._step_labels = []

    def _render_steps(self, plan: list, current_step: int) -> None:
        self._clear_steps()
        shown = plan[:_MAX_STEPS_SHOWN]
        for index, step_text in enumerate(shown):
            if index < current_step:
                marker, color = "✓", theme.ACCENT
            elif index == current_step:
                marker, color = "◉", theme.TEXT_PRIMARY
            else:
                marker, color = "○", theme.TEXT_DIM
            label = QLabel(f"{marker}  {str(step_text)[:80]}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {color};")
            self._steps_layout.addWidget(label)
            self._step_labels.append(label)
        remaining = len(plan) - len(shown)
        if remaining > 0:
            more = QLabel(f"○  +{remaining} more")
            more.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self._steps_layout.addWidget(more)
            self._step_labels.append(more)

    def add_ticker_event(self, topic: str, data: dict) -> None:
        text = _describe(topic, data)
        if not text:
            return
        self._history.append(text)
        self._history = self._history[-_MAX_TICKER_LINES:]
        for line, text in zip(self._ticker_lines, reversed(self._history)):
            line.setText(text)
            line.show()


def _describe(topic: str, data: dict) -> str:
    if topic == "orchestrator.start":
        return f"goal: {str(data.get('goal', ''))[:100]}"
    if topic == "orchestrator.step":
        return f"→ {data.get('tool', '')}"
    if topic == "orchestrator.replan":
        return f"replanning (attempt {data.get('attempt', '?')})"
    if topic == "orchestrator.done":
        return "done" if data.get("ok") else f"stopped: {data.get('stopped', 'failed')}"
    return ""
