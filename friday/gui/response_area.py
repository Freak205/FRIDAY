"""The hero response strip — sits beneath the core, between the state label
and the command channel (see the composition in PLAN §10.x). Shows the most
recent exchange, for either actor (typed or voice), elegantly rather than as
a chat log: one user echo line, one FRIDAY response, nothing older kept on
screen.

Sourced from two places, both already real backend data — never fabricated:
`CommandBar.exchangeReady` for typed commands (the full round trip, since
`CommandBar` already awaits `Backend.ask()`), and `GuiStateHub.voiceRawState`
for voice commands, where `friday/voice/session.py` emits the transcribed
utterance on `"executing"` (`text=`) and the spoken reply on `"speaking"`
(`text=`, `ok=`) — the exact strings TTS actually speaks, not a paraphrase.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

_IDLE_PLACEHOLDER = "Ask a question, type a command, or say the wake word."


class ResponseArea(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        outer.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._user_echo = QLabel("")
        self._user_echo.setObjectName("userEcho")
        self._user_echo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._user_echo.setWordWrap(True)
        self._user_echo.setFixedWidth(520)
        self._user_echo.hide()
        outer.addWidget(self._user_echo, alignment=Qt.AlignmentFlag.AlignHCenter)

        header_row = QLabel("FRIDAY")
        header_row.setObjectName("responseHeader")
        header_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(header_row)
        self._header = header_row

        self._response = QLabel(_IDLE_PLACEHOLDER)
        self._response.setObjectName("responseText")
        self._response.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._response.setWordWrap(True)
        # A *fixed* width (not just a maximum) so Qt always has a concrete
        # value to wrap against — a word-wrapped QLabel's own sizeHint is
        # its unwrapped single-line width, so anything short of a fixed
        # width leaves the label hugging that and never actually wrapping.
        self._response.setFixedWidth(520)
        outer.addWidget(self._response, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._has_content = False

    # -- typed-command path (CommandBar.exchangeReady) ---------------------------

    def set_exchange(self, user_text: str, friday_text: str, ok: bool) -> None:
        self._user_echo.setText(f"“{user_text}”")
        self._user_echo.show()
        self._response.setText(friday_text or "…")
        self._response.setStyleSheet("" if ok else f"color: {theme.WARNING};")
        self._has_content = True

    # -- voice path (GuiStateHub.voiceRawState) ----------------------------------

    def on_voice_raw_state(self, state: str, data: dict) -> None:
        if state == "executing":
            text = str(data.get("text") or "")
            if text:
                self._user_echo.setText(f"“{text}”")
                self._user_echo.show()
                self._response.setText("…")
                self._response.setStyleSheet("")
                self._has_content = True
        elif state == "speaking":
            text = str(data.get("text") or "")
            ok = bool(data.get("ok", True))
            if text:
                self._response.setText(text)
                self._response.setStyleSheet("" if ok else f"color: {theme.WARNING};")
                self._has_content = True
        elif state in ("conversation.wake_detected", "listening"):
            if not self._has_content:
                self._user_echo.hide()
                self._response.setText("Listening…")
        elif state == "cancelled":
            self._response.setText("Cancelled.")
        elif state == "empty":
            self._response.setText("Didn't catch that.")
        elif state == "error":
            self._response.setText("Voice error — see log.")
            self._response.setStyleSheet(f"color: {theme.ERROR};")
