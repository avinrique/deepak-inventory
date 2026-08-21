"""QSS + a couple of small helpers for the three transaction forms — New
Bill, PurchaseOrderFormDialog, and SalesOrderFormDialog — and the shared
TransactionItemsTable they all embed. The actual rules live in
app/ui/styles/order_form.qss and reuse app.ui.theme's own palette (no
separate color set), so these forms read as the same product as every other
screen — just with a card layout and a couple of form-specific text roles
(formCard, formTitle, sectionTitle, fieldLabel, ...) that aren't needed
anywhere else.

Each form applies this on top of the app-wide stylesheet
(``self.setStyleSheet(STYLESHEET + ORDER_FORM_STYLESHEET)``), so anything
not overridden here (QMessageBox, etc.) still falls back to the normal app
look. New Bill is a QWidget page rather than a QDialog, so any QDialog-only
rule in theme.qss simply doesn't match there — harmless.
"""
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLabel, QWidget

from app.ui.theme import MUTED, load_qss

# Kept as an alias: a couple of call sites use this for a QPalette
# PlaceholderText override, which QSS itself can't express.
TEXT_SECONDARY = MUTED

ORDER_FORM_STYLESHEET = load_qss("order_form.qss")


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
