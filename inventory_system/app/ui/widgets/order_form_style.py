"""Design tokens + QSS for the Purchase/Sales order *creation* dialogs only
(PurchaseOrderFormDialog, SalesOrderFormDialog, and the OrderItemsEditor
they both embed). Deliberately kept out of app.ui.theme so this palette
never leaks into the Purchases/Sales listing pages or any other part of
the app — those keep using theme.STYLESHEET exactly as before.

Dialogs apply this on top of the app-wide stylesheet
(``self.setStyleSheet(STYLESHEET + ORDER_FORM_STYLESHEET)``), so anything
not overridden here (fonts, QMessageBox, etc.) still falls back to the
normal app look.
"""
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLabel, QWidget

from app.ui.theme import GREEN

FORM_BG = "#F3F6FA"
CARD_BG = "#FFFFFF"
INPUT_BG = "#F3F4F6"
BORDER = "#D1D5DB"
TEXT = "#172033"
TEXT_SECONDARY = "#667085"
PRIMARY = "#1677F2"
PRIMARY_DARK = "#0F5FD1"
FOCUS_BORDER = "#1677F2"
SUCCESS = GREEN

ORDER_FORM_STYLESHEET = f"""
QDialog {{
    background: {FORM_BG};
}}
QLabel {{
    color: {TEXT};
}}
QWidget#formCard {{
    background: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#formTitle {{
    font-size: 20px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#formSubtitle {{
    font-size: 13px;
    color: {TEXT_SECONDARY};
}}
QLabel#sectionTitle {{
    font-size: 13px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#fieldLabel {{
    font-size: 12px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
}}
QLabel#secondaryText {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
}}
QLabel#readOnlyValue {{
    font-size: 13px;
    color: {TEXT};
    padding: 9px 2px;
}}
QLabel#grandTotalLabel {{
    font-size: 13px;
    font-weight: 700;
    color: {TEXT_SECONDARY};
}}
QLabel#grandTotalValue {{
    font-size: 24px;
    font-weight: 800;
    color: {PRIMARY};
}}
QFrame#divider {{
    background: {BORDER};
    max-height: 1px;
    border: none;
}}
QLineEdit, QComboBox, QDateEdit, QTextEdit {{
    background: {INPUT_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 13px;
    min-height: 18px;
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QTextEdit:focus {{
    border: 1px solid {FOCUS_BORDER};
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{
    color: {TEXT_SECONDARY};
}}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {CARD_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    outline: none;
    selection-background-color: {PRIMARY};
    selection-color: white;
}}
QTableWidget {{
    background: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER};
    font-size: 13px;
}}
QHeaderView::section {{
    background: {INPUT_BG};
    color: {TEXT_SECONDARY};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 700;
    font-size: 11px;
}}
QTableWidget::item {{
    padding: 4px;
}}
QPushButton#orderPrimary {{
    background: {PRIMARY};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 11px 22px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#orderPrimary:hover {{
    background: {PRIMARY_DARK};
}}
QPushButton#orderPrimary:disabled {{
    background: #b9d3f7;
    color: #eef4fd;
}}
QPushButton#orderSecondary {{
    background: {CARD_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 11px 22px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton#orderSecondary:hover {{
    background: {INPUT_BG};
}}
QPushButton#orderGhost {{
    background: {INPUT_BG};
    color: {PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#orderGhost:hover {{
    background: #E7F0FE;
}}
"""


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def apply_card_shadow(widget: QWidget) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(24)
    effect.setXOffset(0)
    effect.setYOffset(4)
    effect.setColor(QColor(23, 32, 51, 26))
    widget.setGraphicsEffect(effect)
