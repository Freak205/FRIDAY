"""Visual design tokens for the cinematic desktop interface.

One restrained palette, used everywhere — no per-widget color literals.
Near-black base, a single cool cyan-blue accent for anything "active," muted
slate for secondary text, amber/red held in reserve for warning/error only.
Negative space over density; thin technical lines over rounded SaaS cards.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QWidget

# -- palette ------------------------------------------------------------------

WINDOW_BG = "#07080b"
PANEL_BG = "#0e1015"
PANEL_BG_RAISED = "#141821"
BORDER = "#1f2430"
BORDER_BRIGHT = "#333a4a"

TEXT_PRIMARY = "#e8eaf0"
TEXT_SECONDARY = "#8a90a0"
TEXT_DIM = "#4d5262"

ACCENT = "#5fb4e0"
ACCENT_BRIGHT = "#c7ecff"
ACCENT_SOFT = "#5b8dd9"
ACCENT_DIM = "#25415a"

WARNING = "#d9a15b"
ERROR = "#d9635b"

# -- typography -----------------------------------------------------------

FONT_FAMILY = "Segoe UI"
FONT_FAMILY_TECHNICAL = "Consolas"

SIZE_WORDMARK = 18
SIZE_TAGLINE = 8
SIZE_STATE = 13
SIZE_TITLE = 13
SIZE_BODY = 12
SIZE_MICRO = 9

# -- misc -------------------------------------------------------------------

RADIUS = 3
SPACING = 14


def hairline(parent: QWidget | None = None, *, vertical: bool = False) -> QFrame:
    """A 1px technical divider — used instead of card borders/boxes to
    separate modules within a panel (see design brief §6: 'prefer thin
    lines... over generic rounded SaaS cards')."""
    line = QFrame(parent)
    line.setObjectName("hairline")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


QSS = f"""
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "{FONT_FAMILY}";
    font-size: {SIZE_BODY}pt;
}}

QMainWindow {{
    background-color: {WINDOW_BG};
}}

QWidget#bootOverlay {{
    background-color: {WINDOW_BG};
}}

QWidget#panel {{
    background-color: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

QFrame#hairline {{
    background-color: {BORDER};
    border: none;
}}

QLabel#microLabel {{
    color: {TEXT_SECONDARY};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
}}

QLabel#dimLabel {{
    color: {TEXT_DIM};
}}

QLabel#accentLabel {{
    color: {ACCENT};
}}

QLabel#wordmark {{
    color: {TEXT_PRIMARY};
    font-size: {SIZE_WORDMARK}pt;
    font-weight: 600;
    letter-spacing: 3px;
}}

QLabel#tagline {{
    color: {TEXT_DIM};
    font-size: {SIZE_TAGLINE}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 2px;
}}

QLabel#statusPill {{
    color: {TEXT_SECONDARY};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
}}

QLabel#coreStateLabel {{
    color: {TEXT_PRIMARY};
    font-size: {SIZE_STATE}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    font-weight: 600;
    letter-spacing: 5px;
}}

QLabel#coreSecondaryLabel {{
    color: {TEXT_SECONDARY};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
}}

QLabel#sectionHeader {{
    color: {TEXT_SECONDARY};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 2px;
}}

QLabel#responseHeader {{
    color: {ACCENT};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 2px;
}}

QLabel#responseText {{
    color: {TEXT_PRIMARY};
    font-size: {SIZE_BODY + 1}pt;
}}

QLabel#userEcho {{
    color: {TEXT_SECONDARY};
    font-size: {SIZE_BODY - 1}pt;
}}

QPushButton#pinControl {{
    background: transparent;
    border: none;
    color: {TEXT_DIM};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
    padding: 2px 6px;
}}

QPushButton#pinControl:hover {{
    color: {TEXT_SECONDARY};
}}

QPushButton#pinControl:checked {{
    color: {ACCENT};
}}

QWidget#commandChannel {{
    background: transparent;
    border-top: 1px solid {BORDER};
}}

QLabel#channelLabel {{
    color: {TEXT_DIM};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 2px;
}}

QLineEdit#commandInput {{
    background: transparent;
    color: {TEXT_PRIMARY};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 2px;
    font-size: {SIZE_BODY + 1}pt;
    selection-background-color: {ACCENT_DIM};
}}

QLineEdit#commandInput:focus {{
    border-bottom: 1px solid {ACCENT};
}}

QPushButton#micControl {{
    background-color: {PANEL_BG_RAISED};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-radius: 19px;
    font-size: 13pt;
    padding: 0px;
}}

QPushButton#micControl:hover {{
    border: 1px solid {BORDER_BRIGHT};
}}

QPushButton#micControl:disabled {{
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
}}

QPushButton#micControl[voiceState="active"] {{
    color: {ACCENT_BRIGHT};
    border: 1px solid {ACCENT};
    background-color: {ACCENT_DIM};
}}

QPushButton#cancelLink {{
    background: transparent;
    border: none;
    color: {TEXT_SECONDARY};
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
}}

QPushButton#cancelLink:hover {{
    color: {ERROR};
}}

QPushButton#stopControl {{
    background-color: transparent;
    color: {ERROR};
    border: 1px solid {ERROR};
    border-radius: 6px;
    padding: 6px 14px;
    font-size: {SIZE_MICRO}pt;
    font-family: "{FONT_FAMILY_TECHNICAL}";
    letter-spacing: 1px;
}}

QPushButton#stopControl:hover {{
    background-color: {ERROR};
    color: {WINDOW_BG};
}}

QPushButton#stopControl:disabled {{
    color: {TEXT_DIM};
    border-color: {TEXT_DIM};
}}

QLineEdit {{
    background-color: {PANEL_BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    selection-background-color: {ACCENT_DIM};
}}

QLineEdit:focus {{
    border: 1px solid {ACCENT_DIM};
}}

QPushButton {{
    background-color: {PANEL_BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 16px;
}}

QPushButton:hover {{
    border: 1px solid {ACCENT_DIM};
}}

QPushButton:pressed {{
    background-color: {ACCENT_DIM};
}}

QPushButton#confirmButton {{
    background-color: {ACCENT_DIM};
    border: 1px solid {ACCENT};
}}

QPushButton#confirmButton:hover {{
    background-color: {ACCENT_SOFT};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
}}
"""
