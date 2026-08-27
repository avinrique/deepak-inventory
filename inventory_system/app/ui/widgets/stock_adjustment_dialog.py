"""Stock Out / Mark Damaged / Manual Adjustment / Transfer dialog — the
counterpart to StockInDialog for the rest of InventoryService's
stock-changing operations. Before this dialog existed, InventoryPage only
ever called stock_in: staff had no in-app way to record shrinkage/damage,
correct a miscount, or move stock between warehouses even though
InventoryService/InventoryRepository fully implement and atomically ledger
all of them (see app/services/inventory_service.py). One dialog with a
mode selector, rather than four near-identical dialogs, since the four
operations share almost every field.

No business logic here — quantity/product/warehouse validation and the
actual ledger update (row locking, the immutable InventoryTransaction row)
live in InventoryService/InventoryRepository. This widget only collects
the form values for whichever mode is selected and displays whatever comes
back or is raised.
"""
from decimal import Decimal, InvalidOperation
from enum import Enum

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
from app.schemas.inventory import AdjustmentRequest, StockMoveRequest, TransferRequest, WarehouseOut
from app.schemas.product import ProductOut
from app.security.authorization import PermissionDeniedError
from app.services.inventory_service import InventoryService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker


class StockAdjustmentMode(str, Enum):
    STOCK_OUT = "Stock Out"
    MARK_DAMAGED = "Mark Damaged"
    ADJUSTMENT = "Manual Adjustment"
    TRANSFER = "Transfer Between Warehouses"


class StockAdjustmentDialog(QDialog):
    def __init__(self, inventory_service: InventoryService, products: list[ProductOut],
                warehouses: list[WarehouseOut], *, allow_transfer: bool, parent=None):
        super().__init__(parent)
        self._inventory_service = inventory_service
        self.transaction = None  # InventoryTransactionOut, or a pair for Transfer
        self.mode: StockAdjustmentMode | None = None
        self.setWindowTitle("Adjust Stock")
        constrain_dialog(self, 400)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._mode = QComboBox()
        modes = [StockAdjustmentMode.STOCK_OUT, StockAdjustmentMode.MARK_DAMAGED,
                StockAdjustmentMode.ADJUSTMENT]
        # inventory.transfer is a separate permission from inventory.adjust
        # (see app.security.permissions) — hiding this option when the
        # session lacks it is a UX convenience only, same as every other
        # permission_hints use in this app; InventoryService.transfer_stock
        # enforces it independently regardless of what this dialog shows.
        if allow_transfer:
            modes.append(StockAdjustmentMode.TRANSFER)
        for m in modes:
            self._mode.addItem(m.value, m)
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Action *", self._mode)

        self._product = QComboBox()
        for p in sorted(products, key=lambda p: p.name.lower()):
            self._product.addItem(f"{p.sku} — {p.name}", p.id)
        form.addRow("Product *", self._product)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(f"{w.code} — {w.name}", w.id)
        self._warehouse_row_label = QLabel("Warehouse *")
        form.addRow(self._warehouse_row_label, self._warehouse)

        self._to_warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._to_warehouse.addItem(f"{w.code} — {w.name}", w.id)
        self._to_warehouse_row_label = QLabel("To Warehouse *")
        form.addRow(self._to_warehouse_row_label, self._to_warehouse)

        self._quantity = QLineEdit()
        self._quantity.setPlaceholderText("0")
        self._quantity_row_label = QLabel("Quantity *")
        form.addRow(self._quantity_row_label, self._quantity)

        self._reason = QLineEdit()
        self._reason.setPlaceholderText("Required for a manual adjustment, "
                                        "e.g. \"cycle count correction\"")
        self._reason_row_label = QLabel("Reason *")
        form.addRow(self._reason_row_label, self._reason)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Optional")
        self._notes_row_label = QLabel("Notes")
        form.addRow(self._notes_row_label, self._notes)

        layout.addLayout(form)

        self._hint_label = QLabel("")
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._hint_label)

        if not products or not warehouses:
            missing = "products" if not products else "warehouses"
            notice = QLabel(f"No {missing} available — add one before adjusting stock.")
            notice.setWordWrap(True)
            notice.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            layout.addWidget(notice)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Submit")
        self._submit_button.setObjectName("primary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(bool(products) and bool(warehouses))
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

        self._on_mode_changed()

    # -- mode-driven field visibility ------------------------------------#
    def _on_mode_changed(self) -> None:
        mode = self._mode.currentData()
        is_transfer = mode is StockAdjustmentMode.TRANSFER
        is_adjustment = mode is StockAdjustmentMode.ADJUSTMENT

        self._to_warehouse_row_label.setVisible(is_transfer)
        self._to_warehouse.setVisible(is_transfer)
        self._reason_row_label.setVisible(is_adjustment)
        self._reason.setVisible(is_adjustment)
        self._notes_row_label.setVisible(not is_adjustment)
        self._notes.setVisible(not is_adjustment)

        hints = {
            StockAdjustmentMode.STOCK_OUT:
                "Removes stock without a sale — e.g. a sample given away.",
            StockAdjustmentMode.MARK_DAMAGED:
                "Removes stock and records it as damaged/written off.",
            StockAdjustmentMode.ADJUSTMENT:
                "Corrects on-hand quantity after a physical count. Quantity may be "
                "negative (found less than recorded) or positive (found more).",
            StockAdjustmentMode.TRANSFER:
                "Moves stock from one warehouse to another; on-hand total is unchanged.",
        }
        self._hint_label.setText(hints.get(mode, ""))
        self._quantity.setPlaceholderText(
            "e.g. -3 or 3" if is_adjustment else "0")

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Submitting…" if busy else "Submit")

    def _submit(self) -> None:
        self._error_label.hide()
        mode = self._mode.currentData()
        product_id = self._product.currentData()
        warehouse_id = self._warehouse.currentData()
        if product_id is None or warehouse_id is None:
            return self._show_error("Select a product and a warehouse.")

        try:
            quantity = Decimal(self._quantity.text().strip() or "0")
        except InvalidOperation:
            return self._show_error("Quantity must be a number.")

        self.mode = mode
        if mode is StockAdjustmentMode.STOCK_OUT:
            if quantity <= 0:
                return self._show_error("Quantity must be greater than zero.")
            data = StockMoveRequest(product_id=product_id, warehouse_id=warehouse_id,
                                    quantity=quantity, notes=self._notes.text().strip() or None)
            method = self._inventory_service.stock_out
        elif mode is StockAdjustmentMode.MARK_DAMAGED:
            if quantity <= 0:
                return self._show_error("Quantity must be greater than zero.")
            data = StockMoveRequest(product_id=product_id, warehouse_id=warehouse_id,
                                    quantity=quantity, notes=self._notes.text().strip() or None)
            method = self._inventory_service.mark_damaged
        elif mode is StockAdjustmentMode.ADJUSTMENT:
            if quantity == 0:
                return self._show_error("Quantity change cannot be zero.")
            reason = self._reason.text().strip()
            if not reason:
                return self._show_error("A reason is required for a manual adjustment.")
            data = AdjustmentRequest(product_id=product_id, warehouse_id=warehouse_id,
                                     quantity_change=quantity, reason=reason)
            method = self._inventory_service.adjust_stock
        else:  # TRANSFER
            to_warehouse_id = self._to_warehouse.currentData()
            if quantity <= 0:
                return self._show_error("Quantity must be greater than zero.")
            if to_warehouse_id == warehouse_id:
                return self._show_error("Source and destination warehouse must be different.")
            data = TransferRequest(product_id=product_id, from_warehouse_id=warehouse_id,
                                   to_warehouse_id=to_warehouse_id, quantity=quantity,
                                   notes=self._notes.text().strip() or None)
            method = self._inventory_service.transfer_stock

        self._set_busy(True)
        worker = Worker(method, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, result) -> None:
        self._set_busy(False)
        self.transaction = result
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, InventoryValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (ProductNotFoundError, WarehouseNotFoundError)):
            self._show_error(str(exc))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to make this change.")
        else:
            self._show_error("Something went wrong. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
