"""Receive Goods dialog — records a delivery against an approved/submitted
purchase order via PurchaseService.receive_goods, which is what actually
moves stock in for a purchase (posts an InventoryTransaction per line,
see InventoryRepository). One row per outstanding line item, defaulted to
its full outstanding quantity and editable down for a partial delivery.
"""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.exceptions import PurchaseOrderValidationError
from app.schemas.purchasing import GoodsReceiptLineInput, PurchaseOrderOut, ReceiveGoodsRequest
from app.security.authorization import PermissionDeniedError
from app.services.purchase_service import PurchaseService
from app.ui.theme import RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker

_QTY_COL = 2


class ReceiveGoodsDialog(QDialog):
    def __init__(self, purchase_service: PurchaseService, order: PurchaseOrderOut,
                product_names: dict, parent=None):
        super().__init__(parent)
        self._purchase_service = purchase_service
        self._order = order
        self._outstanding_items = [item for item in order.items
                                   if item.quantity_outstanding > 0]
        self.receipt = None
        self.setWindowTitle("Receive Goods")
        constrain_dialog(self, 460)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"Purchase Order {order.order_number or order.id}"))

        table = QTableWidget(len(self._outstanding_items), 3)
        table.setHorizontalHeaderLabels(["Product", "Outstanding", "Receive Qty"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for row, item in enumerate(self._outstanding_items):
            table.setItem(row, 0, QTableWidgetItem(
                product_names.get(item.product_id, str(item.product_id))))
            table.setItem(row, 1, QTableWidgetItem(f"{item.quantity_outstanding:g}"))
            table.setCellWidget(row, _QTY_COL, QLineEdit(f"{item.quantity_outstanding:g}"))
        table.resizeRowsToContents()
        self._table = table
        layout.addWidget(table)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Receive Goods")
        self._submit_button.setObjectName("primary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(bool(self._outstanding_items))
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

        if not self._outstanding_items:
            notice = QLabel("Nothing left to receive on this order.")
            layout.addWidget(notice)

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Receiving…" if busy else "Receive Goods")

    def _submit(self) -> None:
        self._error_label.hide()
        lines = []
        for row, item in enumerate(self._outstanding_items):
            widget = self._table.cellWidget(row, _QTY_COL)
            try:
                quantity = Decimal(widget.text().strip() or "0")
            except InvalidOperation:
                return self._show_error(f"Row {row + 1}: quantity must be a number.")
            if quantity < 0 or quantity > item.quantity_outstanding:
                return self._show_error(
                    f"Row {row + 1}: quantity must be between 0 and "
                    f"{item.quantity_outstanding:g}.")
            if quantity > 0:
                lines.append(GoodsReceiptLineInput(purchase_order_item_id=item.id,
                                                   quantity=quantity))
        if not lines:
            return self._show_error("Enter a quantity greater than zero for at least one line.")

        data = ReceiveGoodsRequest(purchase_order_id=self._order.id, lines=lines)
        self._set_busy(True)
        worker = Worker(self._purchase_service.receive_goods, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, receipt) -> None:
        self._set_busy(False)
        self.receipt = receipt
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, PurchaseOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to receive goods.")
        else:
            self._show_error("Something went wrong receiving goods. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
