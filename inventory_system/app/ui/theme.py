"""Central design tokens (colors/spacing/fonts) + the app-wide QSS
stylesheet. Every widget under app/ui pulls from here rather than hardcoding
a color, so the app reads as one consistent system, not a pile of one-off
styles.
"""
import platform

MAC = platform.system() == "Darwin"
FAMILY = "Helvetica Neue" if MAC else "Segoe UI"

# Palette — dark sidebar / light content, the same system proven out on the
# legacy Tkinter app's UI pass, carried over for visual continuity.
SIDEBAR_BG = "#111827"
SIDEBAR_FG = "#cbd5e1"
SIDEBAR_HOVER = "#1f2937"
SIDEBAR_MUTED = "#94a3b8"
NAV_ACTIVE = "#2563eb"
CONTENT_BG = "#eef1f5"
CARD_BG = "#ffffff"
BORDER = "#e2e6ec"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_TINT = "#eef2ff"
GREEN = "#059669"
GREEN_DARK = "#047857"
RED = "#dc2626"
RED_TINT = "#fef2f2"
AMBER = "#d97706"
TEXT = "#111827"
MUTED = "#6b7280"

SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 16
SPACING_LG = 24
SPACING_XL = 32

RADIUS = 8

STYLESHEET = f"""
* {{
    font-family: "{FAMILY}";
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: {CONTENT_BG};
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QWidget#contentArea {{
    background: {CONTENT_BG};
}}
QWidget#card {{
    background: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#pageTitle {{
    font-size: 22px;
    font-weight: 700;
}}
QLabel#pageSubtitle {{
    font-size: 13px;
    color: {MUTED};
}}
QLabel#sectionLabel {{
    font-size: 11px;
    font-weight: 700;
    color: {MUTED};
    letter-spacing: 0.5px;
}}
QLabel#sectionTitle {{
    font-size: 14px;
    font-weight: 700;
    color: {TEXT};
}}

QPushButton {{
    border-radius: {RADIUS}px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
    border: none;
}}
QPushButton#primary {{
    background: {ACCENT};
    color: white;
}}
QPushButton#primary:hover {{
    background: {ACCENT_DARK};
}}
QPushButton#primary:disabled {{
    background: #cbd5e1;
    color: #94a3b8;
}}
QPushButton#danger {{
    background: {RED};
    color: white;
}}
QPushButton#danger:hover {{
    background: #b91c1c;
}}
QPushButton#ghost {{
    background: {ACCENT_TINT};
    color: {ACCENT};
}}
QPushButton#ghost:hover {{
    background: #e0e7ff;
}}
QPushButton#flat {{
    background: transparent;
    color: {MUTED};
    font-weight: 500;
}}
QPushButton#flat:hover {{
    background: {BORDER};
}}

QLineEdit {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 9px 12px;
    font-size: 13px;
    background: white;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit[error="true"] {{
    border: 1px solid {RED};
}}
QLineEdit:disabled {{
    background: #f1f5f9;
    color: {MUTED};
}}

/* QComboBox/QDateEdit/QTextEdit have no Qt Style Sheet rules by default in
   this app, unlike QLineEdit above — without one, each falls back to the
   OS-native widget style (dark/near-black on a system in dark mode, on
   macOS in particular), which is what produced the black Description
   textarea and black report filter dropdowns/date fields this block
   fixes. Every value below reuses the same tokens QLineEdit already uses,
   so all four input types look and behave identically. */
QComboBox, QDateEdit, QTextEdit, QPlainTextEdit {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 9px 12px;
    font-size: 13px;
    background: white;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QComboBox:focus, QDateEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox:disabled, QDateEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    background: #f1f5f9;
    color: {MUTED};
}}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: white;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    outline: none;
    padding: 4px;
    selection-background-color: {ACCENT_TINT};
    selection-color: {ACCENT};
}}

/* QDateEdit's calendar popup (setCalendarPopup(True), used by every date
   filter in Reports) is a QCalendarWidget — same "unstyled = OS-native
   dark chrome" problem as above. */
QCalendarWidget {{
    background: white;
    color: {TEXT};
}}
QCalendarWidget QWidget {{
    background: white;
    color: {TEXT};
}}
QCalendarWidget QToolButton {{
    background: white;
    color: {TEXT};
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
    font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{
    background: {ACCENT_TINT};
    color: {ACCENT};
}}
QCalendarWidget QAbstractItemView {{
    background: white;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: white;
    outline: none;
}}
QCalendarWidget QAbstractItemView:disabled {{
    color: {MUTED};
}}
QCalendarWidget #qt_calendar_navigationbar {{
    background: white;
    border-bottom: 1px solid {BORDER};
}}

QListWidget#sidebarList {{
    background: {SIDEBAR_BG};
    border: none;
    outline: none;
    padding: 4px 0;
}}
QListWidget#sidebarList::item {{
    color: {SIDEBAR_FG};
    padding: 11px 22px;
    border-left: 3px solid transparent;
    font-size: 13px;
}}
QListWidget#sidebarList::item:hover {{
    background: {SIDEBAR_HOVER};
}}
QListWidget#sidebarList::item:selected {{
    background: {NAV_ACTIVE};
    color: white;
    border-left: 3px solid white;
}}

QFrame#header {{
    background: {CARD_BG};
    border-bottom: 1px solid {BORDER};
}}
QStatusBar#statusBar {{
    background: {CARD_BG};
    border-top: 1px solid {BORDER};
    color: {MUTED};
    font-size: 11px;
    padding-left: 12px;
}}
QStatusBar#statusBar::item {{
    border: none;
}}

QMenu {{
    background: white;
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 14px;
    border-radius: 4px;
    font-size: 13px;
}}
QMenu::item:selected {{
    background: {ACCENT_TINT};
    color: {ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 6px 4px;
}}

QToolTip {{
    background: {TEXT};
    color: white;
    border: none;
    padding: 6px 8px;
    border-radius: 4px;
}}

QTableWidget {{
    background: white;
    border: none;
    gridline-color: {BORDER};
    font-size: 13px;
}}
QHeaderView::section {{
    background: #f8fafc;
    color: {MUTED};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 700;
    font-size: 11px;
}}
QTableWidget::item {{
    padding: 6px;
}}

QScrollBar:vertical {{
    width: 10px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
