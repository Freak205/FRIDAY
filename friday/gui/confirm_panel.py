"""The confirmation overlay — FRIDAY's permission-tier gate made visible.

Wiring (see `friday.gui.state_hub.GuiStateHub` and PLAN §14):
`Session._confirm` publishes `session.awaiting_confirm` with
`{skill, tier, preview, speech, actor}` — `actor` is whichever actor
triggered the underlying command ("text" or "voice"), never a new "gui"
value, since resolving a pending confirmation means calling
`SESSION.handle("yes"/"no", actor=<that same actor>)` — there is no separate
"answer" API. Confirm/Cancel here does exactly that, on a worker thread
(mirroring the old `QuickBar._submit` pattern — `Backend.ask()` blocks up to
120s and must never run on the Qt thread).

A voice-triggered confirmation can also be resolved by
`ConversationLoop`'s own spoken-answer listener, or simply time out (60s) —
so this panel dismisses on *whichever arrives first*: its own click result,
or `GuiStateHub.confirmResolved` (call `resolve_externally`). Both paths are
guarded on "already dismissed," so the second arrival is a no-op, and a
stray click result that lost the race is discarded rather than shown as an
error.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .backend import Backend


class ConfirmPanel(QWidget):
    _askFinished = Signal(str, bool)  # (skill, ok) — internal, Qt-thread-marshaled

    def __init__(self, backend: Backend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._pending_skill: str | None = None
        self._pending_actor: str = "text"
        self.hide()

        self._askFinished.connect(self._on_ask_finished)

        card = QWidget(self)
        card.setObjectName("panel")
        card.setFixedWidth(400)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 22, 28, 22)
        card_layout.setSpacing(12)

        title = QLabel("AWAITING AUTHORIZATION")
        title.setObjectName("sectionHeader")
        title.setStyleSheet(f"color: {theme.WARNING}; letter-spacing: 3px;")
        card_layout.addWidget(title)

        accent_line = theme.hairline()
        accent_line.setStyleSheet(f"background-color: {theme.WARNING};")
        card_layout.addWidget(accent_line)

        self._preview_label = QLabel("")
        self._preview_label.setWordWrap(True)
        card_layout.addWidget(self._preview_label)

        button_row = QHBoxLayout()
        self._cancel_btn = QPushButton("CANCEL")
        self._confirm_btn = QPushButton("CONFIRM")
        self._confirm_btn.setObjectName("confirmButton")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._confirm_btn.clicked.connect(self._on_confirm)
        button_row.addWidget(self._cancel_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._confirm_btn)
        card_layout.addLayout(button_row)

        self._card = card
        self._center_card()

    # -- layout -----------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        self._center_card()

    def _center_card(self) -> None:
        self._card.adjustSize()
        x = (self.width() - self._card.width()) // 2
        y = (self.height() - self._card.height()) // 2
        self._card.move(max(0, x), max(0, y - 40))

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 160))
        painter.end()

    # -- showing / dismissing -----------------------------------------------------

    def show_request(self, payload: dict[str, Any]) -> None:
        self._pending_skill = str(payload.get("skill") or "")
        self._pending_actor = str(payload.get("actor") or "text")
        preview = str(payload.get("preview") or payload.get("speech") or "")
        tier = str(payload.get("tier") or "")
        self._preview_label.setText(f"{preview}\n\n({tier})" if tier else preview)
        self._cancel_btn.setEnabled(True)
        self._confirm_btn.setEnabled(True)
        self._center_card()
        self.show()
        self.raise_()

    def resolve_externally(self, skill: str, outcome: str) -> None:
        """Call from a slot connected to `GuiStateHub.confirmResolved` — the
        confirmation may have been answered by voice, or timed out, while
        this panel was still showing."""
        if self._pending_skill is None:
            return
        self._dismiss()

    def _dismiss(self) -> None:
        self._pending_skill = None
        self.hide()

    # -- button handlers ----------------------------------------------------------

    def _on_confirm(self) -> None:
        self._ask("yes")

    def _on_cancel(self) -> None:
        self._ask("no")

    def _ask(self, answer: str) -> None:
        if self._pending_skill is None:
            return
        skill = self._pending_skill
        actor = self._pending_actor
        self._cancel_btn.setEnabled(False)
        self._confirm_btn.setEnabled(False)

        def work() -> None:
            try:
                self._backend.ask(answer, actor=actor)
                ok = True
            except Exception:
                ok = False
            self._askFinished.emit(skill, ok)

        threading.Thread(target=work, daemon=True, name="confirm-answer").start()

    def _on_ask_finished(self, skill: str, _ok: bool) -> None:
        if self._pending_skill is None or self._pending_skill != skill:
            return  # already resolved elsewhere (voice answer / timeout) — discard
        self._dismiss()
