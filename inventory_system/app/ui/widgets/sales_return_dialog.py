"""Record a customer return against a fulfilled sales order line —
SalesService.record_sales_return. Only order lines with something still
fulfilled (quantity_fulfilled > 0) are offered; over-return, a zero/
negative quantity, and a missing reason are all rejected server-side
(SalesOrderRepository.record_return), this dialog just surfaces whatever
comes back.

products is passed in already-fetched by the caller (SalesOrdersPage),
same convention as every other form dialog in app.ui.widgets — see
ProductFormDialog's docstring for why.
"""
import logging
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.exceptions import (
    SalesOrderItemNotFoundError,
    SalesOrderNotFoundError,
    SalesOrderValidationError,
)
from app.schemas.product import ProductOut
from app.schemas.sales import SalesOrderOut
from app.services.sales_service import SalesService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class SalesReturnDialog(QDialog):
    def __init__(self, sales_service: SalesService, sales_order: SalesOrderOut,
                products: list[ProductOut], parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._sales_order = sales_order
        product_names = {p.id: p.name for p in products}
        self._returnable_items = [item for item in sales_order.items
                                  if item.quantity_fulfilled > 0]

        self.setWindowTitle("Record Return")
        self.setMinimumWidth(400)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        if not self._returnable_items:
            layout.addWidget(QLabel("Nothing on this order is currently fulfilled — "
                                    "there's nothing to return."))
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
            return

        form = QFormLayout()
        form.setSpacing(8)

        self._item = QComboBox()
        for item in self._returnable_items:
            name = product_names.get(item.product_id, str(item.product_id))
            self._item.addItem(f"{name} — fulfilled {item.quantity_fulfilled:g}", item.id)
        self._item.currentIndexChanged.connect(self._on_item_changed)
        form.addRow("Item *", self._item)

        self._quantity = QLineEdit()
        form.addRow("Quantity *", self._quantity)

        self._reason = QLineEdit()
        self._reason.setPlaceholderText("Why is this being returned?")
        form.addRow("Reason *", self._reason)

        layout.addLayout(form)

        self._hint_label = QLabel("")
        self._hint_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._hint_label)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._save_button = QPushButton("Record Return")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.clicked.connect(self._submit)
        layout.addWidget(self._save_button)

        self._on_item_changed()

    def _selected_item(self):
        item_id = self._item.currentData()
        return next((i for i in self._returnable_items if i.id == item_id), None)

    def _on_item_changed(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self._quantity.setText(f"{item.quantity_fulfilled:g}")
        self._hint_label.setText(f"Up to {item.quantity_fulfilled:g} can be returned.")

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Record Return")

    def _submit(self) -> None:
        self._error_label.hide()
        item = self._selected_item()
        if item is None:
            self._show_error("Select an item.")
            return
        try:
            quantity = Decimal(self._quantity.text().strip() or "0")
        except InvalidOperation:
            self._show_error("Quantity must be a number.")
            return
        if quantity <= 0:
            self._show_error("Quantity must be greater than zero.")
            return
        reason = self._reason.text().strip()
        if not reason:
            self._show_error("A reason is required.")
            return

        self._set_busy(True)
        worker = Worker(self._sales_service.record_sales_return, self._sales_order.id,
                        item.id, quantity, reason)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, result) -> None:
        self._set_busy(False)
        QMessageBox.information(
            self, "Return recorded",
            f"{result.quantity:g} unit(s) returned to stock. "
            f"{result.credit_amount:,.2f} credited to the customer's balance.")
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, SalesOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (SalesOrderItemNotFoundError, SalesOrderNotFoundError)):
            self._show_error(str(exc))
        else:
            _logger.exception("Recording sales return failed", exc_info=exc)
            self._show_error("Something went wrong recording this return. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
