"""Desktop-awareness panel — active app / window count / browser, sourced
from a periodic `friday.desktop_observer.observe()` poll (see
`GuiStateHub._poll_desktop_context`). Never renders a screenshot here; the
observation is requested with `include_screenshot=False` specifically so
this panel stays a text summary, not a live screen feed.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from . import theme


class DesktopContextPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACING, theme.SPACING, theme.SPACING, theme.SPACING)
        outer.setSpacing(6)

        header = QLabel("ENVIRONMENT")
        header.setObjectName("sectionHeader")
        outer.addWidget(header)
        outer.addWidget(theme.hairline())

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        self._active_key = QLabel("ACTIVE")
        self._active_key.setObjectName("microLabel")
        self._active_val = QLabel("—")
        self._active_val.setWordWrap(True)
        grid.addWidget(self._active_key, 0, 0)
        grid.addWidget(self._active_val, 0, 1)

        self._windows_key = QLabel("WINDOWS")
        self._windows_key.setObjectName("microLabel")
        self._windows_val = QLabel("00")
        self._windows_val.setObjectName("accentLabel")
        grid.addWidget(self._windows_key, 1, 0)
        grid.addWidget(self._windows_val, 1, 1)

        self._browser_key = QLabel("BROWSER")
        self._browser_key.setObjectName("microLabel")
        self._browser_val = QLabel("—")
        self._browser_val.setWordWrap(True)
        grid.addWidget(self._browser_key, 2, 0)
        grid.addWidget(self._browser_val, 2, 1)

        outer.addStretch(1)

    def update_context(self, observation: dict) -> None:
        app = observation.get("active_app") or ""
        title = observation.get("active_window_title") or ""
        if title:
            self._active_val.setText(title[:60])
        elif app:
            self._active_val.setText(app)
        else:
            self._active_val.setText("Nothing focused")

        windows = observation.get("open_windows") or []
        self._windows_val.setText(f"{len(windows):02d}")

        browser = observation.get("browser")
        if browser and (browser.get("title") or browser.get("url")):
            self._browser_val.setText((browser.get("title") or browser.get("url"))[:60])
            self._browser_key.show()
            self._browser_val.show()
        else:
            self._browser_key.hide()
            self._browser_val.hide()
