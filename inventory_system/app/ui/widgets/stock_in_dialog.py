"""Stock In dialog — records a stock receipt against InventoryService, the
real SQL-backed warehouse ledger (app/services/inventory_service.py).

Products and warehouses are passed in already-fetched, same convention as
ProductFormDialog (see its docstring): querying them synchronously in
__init__ would block the GUI thread before dialog.exec() hands control
back to the event loop.

No business logic here — quantity/product/warehouse validation and the
actual ledger update (row locking, the immutable InventoryTransaction
row) live in InventoryService/InventoryRepository. This widget only
collects the form values and displays whatever comes back or is raised.
"""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.exceptions import InventoryValidationError, ProductNotFoundError, WarehouseNotFoundError
from app.schemas.inventory import InventoryTransactionOut, StockMoveRequest, WarehouseOut
from app.schemas.product import ProductOut
from app.security.authorization import PermissionDeniedError
from app.services.inventory_service import InventoryService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker


class StockInDialog(QDialog):
    def __init__(self, inventory_service: InventoryService, products: list[ProductOut],
                warehouses: list[WarehouseOut], parent=None):
        super().__init__(parent)
        self._inventory_service = inventory_service
        self.transaction: InventoryTransactionOut | None = None
        self.setWindowTitle("Stock In")
        constrain_dialog(self, 380)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._product = QComboBox()
        for p in sorted(products, key=lambda p: p.name.lower()):
            self._product.addItem(f"{p.sku} — {p.name}", p.id)
        form.addRow("Product *", self._product)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(f"{w.code} — {w.name}", w.id)
        form.addRow("Warehouse *", self._warehouse)

        self._quantity = QLineEdit()
        self._quantity.setPlaceholderText("0")
        form.addRow("Quantity *", self._quantity)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Optional")
        form.addRow("Notes", self._notes)

        layout.addLayout(form)

        if not products or not warehouses:
            missing = "products" if not products else "warehouses"
            notice = QLabel(f"No {missing} available — add one before recording stock.")
            notice.setWordWrap(True)
            notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            layout.addWidget(notice)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Add Stock")
        self._submit_button.setObjectName("primary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(bool(products) and bool(warehouses))
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Adding…" if busy else "Add Stock")

    def _submit(self) -> None:
        self._error_label.hide()
        product_id = self._product.currentData()
        warehouse_id = self._warehouse.currentData()
        if product_id is None or warehouse_id is None:
            return self._show_error("Select a product and a warehouse.")

        try:
            quantity = Decimal(self._quantity.text().strip() or "0")
        except InvalidOperation:
            return self._show_error("Quantity must be a number.")
        if quantity <= 0:
            return self._show_error("Quantity must be greater than zero.")

        data = StockMoveRequest(product_id=product_id, warehouse_id=warehouse_id,
                                quantity=quantity, notes=self._notes.text().strip() or None)

        self._set_busy(True)
        worker = Worker(self._inventory_service.stock_in, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, transaction: InventoryTransactionOut) -> None:
        self._set_busy(False)
        self.transaction = transaction
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, InventoryValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (ProductNotFoundError, WarehouseNotFoundError)):
            self._show_error(str(exc))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to add stock.")
        else:
            self._show_error("Something went wrong adding stock. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
