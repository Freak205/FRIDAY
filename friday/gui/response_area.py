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
_HEADER = "FRIDAY"
_PENDING_HEADER = "FRIDAY · STILL RUNNING"


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

        header_row = QLabel(_HEADER)
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

        # Phase 41: a request that finished AFTER Backend.ask stopped waiting for it. Shown on its own line when the strip has
        # moved on to another exchange, so it is never displayed as the result of whatever happens to be on screen.
        self._late = QLabel("")
        self._late.setObjectName("lateResult")
        self._late.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._late.setWordWrap(True)
        self._late.setFixedWidth(520)
        self._late.hide()
        outer.addWidget(self._late, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._pending_id = ""  # the request whose "still running" text is on screen now
        self._late_seen: list[str] = []  # request ids already displayed: a completion is shown once

        self._has_content = False

    # -- typed-command path (CommandBar.exchangeReady) ---------------------------

    def set_exchange(self, user_text: str, friday_text: str, ok: bool) -> None:
        self._user_echo.setText(f"“{user_text}”")
        self._user_echo.show()
        self._show_response(friday_text or "…", ok=ok)
        self._has_content = True

    def set_pending(self, user_text: str, friday_text: str, request_id: str = "") -> None:
        """Phase 40: the request is still running (`Backend.ask` stopped waiting, it did not finish). Shown under its own
        header and in the accent colour -- not the warning colour of a failure -- so it is not mistaken for one."""
        self._user_echo.setText(f"“{user_text}”")
        self._user_echo.show()
        self._show_response(friday_text or "Still running.", pending=True)
        self._pending_id = request_id
        self._has_content = True

    def set_late(self, info: dict) -> None:
        """Phase 41: the ORIGINAL request ended (`Backend` read its own finished future; nothing was run again). If its
        "still running" text is still the one on screen it is replaced by the real outcome; otherwise the strip belongs to a
        newer exchange and is left alone -- the outcome gets its own line, labelled with the request it belongs to. Each
        request id is shown once. The outcome is shown as reported: a failure or an error stays a failure, never a success."""
        rid = str(info.get("request_id") or "")
        if rid and rid in self._late_seen:
            return
        if rid:
            self._late_seen = (self._late_seen + [rid])[-50:]
        text = str(info.get("utterance") or "")
        speech = str(info.get("speech") or "") or "The earlier request ended."
        ok = bool(info.get("ok"))
        if rid and rid == self._pending_id:
            self.set_exchange(text, speech, ok)
            return
        self._late.setText(f"Earlier request “{text}”: {speech}")
        self._late.setStyleSheet("" if ok else f"color: {theme.WARNING};")
        self._late.show()

    def _show_response(self, text: str, *, ok: bool = True, pending: bool = False) -> None:
        self._pending_id = ""
        self._late.hide()
        self._header.setText(_PENDING_HEADER if pending else _HEADER)
        self._response.setText(text)
        if pending:
            self._response.setStyleSheet(f"color: {theme.ACCENT};")
        else:
            self._response.setStyleSheet("" if ok else f"color: {theme.WARNING};")

    # -- voice path (GuiStateHub.voiceRawState) ----------------------------------

    def on_voice_raw_state(self, state: str, data: dict) -> None:
        if state in ("cancelled", "empty", "error"):
            self._header.setText(_HEADER)  # these replace the text below; a previous "still running" header must not stay
        if state == "executing":
            text = str(data.get("text") or "")
            if text:
                self._user_echo.setText(f"“{text}”")
                self._user_echo.show()
                self._show_response("…")
                self._has_content = True
        elif state == "speaking":
            text = str(data.get("text") or "")
            ok = bool(data.get("ok", True))
            if text:
                self._show_response(text, ok=ok, pending=bool(data.get("pending")))
                if data.get("pending"):
                    self._pending_id = str(data.get("request_id") or "")
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
