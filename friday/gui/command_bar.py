"""The command channel — text input + mic/cancel controls, the secondary/
emergency control channel (PLAN §13) plus voice activation (§12), not the
primary interface. Presentation only: it never renders the exchange itself
(see `friday.gui.response_area.ResponseArea`, which sits in the hero column
above this bar) — this widget is just the channel you speak or type into.

Typed submit and the mic button both end up at the exact same place typing
into the old Tkinter QuickBar did: `Backend.ask(text, actor=...)` on a
worker thread (never the Qt thread — `Backend.ask` blocks up to 120s) →
`SESSION.handle()` → the real brain/skills/orchestrator. There is no second
brain here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from friday.voice import keys as voice_keys

from . import theme
from .backend import Backend

_ACTIVE_VOICE_STATES = (
    "listening", "processing", "executing", "speaking", "conversation.wake_detected",
)

_MIC_LABELS = {
    "listening": "LISTENING",
    "conversation.wake_detected": "LISTENING",
    "processing": "PROCESSING",
    "executing": "PROCESSING",
    "speaking": "SPEAKING",
    "cancelled": "STANDBY",
    "done": "STANDBY",
    "empty": "STANDBY",
    "error": "STANDBY",
    "conversation.idle": "STANDBY",
}


class CommandBar(QWidget):
    exchangeReady = Signal(str, str, bool)  # (utterance, speech, ok) — typed path only

    def __init__(self, backend: Backend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("commandChannel")
        self._backend = backend
        self._mic_activate: Callable[[], None] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 10, 0, 0)
        outer.setSpacing(8)

        label_row = QHBoxLayout()
        label_row.setSpacing(10)
        channel_label = QLabel("VOICE / TEXT COMMAND")
        channel_label.setObjectName("channelLabel")
        label_row.addWidget(channel_label)
        label_row.addWidget(theme.hairline(), 1)
        outer.addLayout(label_row)

        row = QHBoxLayout()
        row.setSpacing(12)

        mic_col = QVBoxLayout()
        mic_col.setSpacing(2)
        self._mic_btn = QPushButton("●")
        self._mic_btn.setObjectName("micControl")
        self._mic_btn.setFixedSize(38, 38)
        self._mic_btn.clicked.connect(self._on_mic_clicked)
        mic_col.addWidget(self._mic_btn)
        self._mic_state_label = QLabel("")
        self._mic_state_label.setObjectName("microLabel")
        self._mic_state_label.setStyleSheet(f"font-size: 7pt; color: {theme.TEXT_DIM};")
        mic_col.addWidget(self._mic_state_label)
        row.addLayout(mic_col)

        self._input = QLineEdit()
        self._input.setObjectName("commandInput")
        self._input.setPlaceholderText("Ask FRIDAY…")
        self._input.returnPressed.connect(self._submit)
        row.addWidget(self._input, 1)

        self._cancel_btn = QPushButton("CANCEL")
        self._cancel_btn.setObjectName("cancelLink")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._cancel_btn.hide()
        row.addWidget(self._cancel_btn)

        outer.addLayout(row)

    # -- wiring -------------------------------------------------------------------

    def set_mic_activate(self, fn: Callable[[], None] | None) -> None:
        self._mic_activate = fn
        self._mic_btn.setEnabled(fn is not None)
        self._mic_state_label.setText("MIC READY" if fn is not None else "UNAVAILABLE")

    def on_voice_raw_state(self, state: str, _data: dict) -> None:
        active = state in _ACTIVE_VOICE_STATES
        self._cancel_btn.setVisible(active)
        self._mic_btn.setProperty("voiceState", "active" if active else "ready")
        self._mic_btn.style().unpolish(self._mic_btn)
        self._mic_btn.style().polish(self._mic_btn)

        label = _MIC_LABELS.get(state)
        if label:
            self._mic_state_label.setText(label)

    # -- typed command --------------------------------------------------------

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._run_ask(text, actor="text")

    def _run_ask(self, text: str, *, actor: str) -> None:
        def work() -> None:
            try:
                result = self._backend.ask(text, actor=actor)
                self.exchangeReady.emit(text, result.speech, result.ok)
            except Exception as exc:
                self.exchangeReady.emit(text, f"Something went wrong: {exc}", False)

        threading.Thread(target=work, daemon=True, name="gui-ask").start()

    # -- voice controls -------------------------------------------------------

    def _on_mic_clicked(self) -> None:
        if self._mic_activate is not None:
            self._mic_activate()

    def _on_cancel_clicked(self) -> None:
        voice_keys.request_cancel()
