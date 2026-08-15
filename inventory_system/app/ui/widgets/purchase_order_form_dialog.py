"""Create Purchase Order dialog — a DRAFT order (supplier, warehouse, line
items) via PurchaseService.create_purchase_order. Suppliers/products/
warehouses are passed in already-fetched by the caller (PurchasesPage),
same convention as every other form dialog in app.ui.widgets — see
ProductFormDialog's docstring for why.

No business logic here: line-item shape/positivity, supplier/warehouse/
product existence, and the DRAFT-only edit rule all live in
PurchaseService / app.domain.purchasing.
"""
from decimal import Decimal

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

from app.core.exceptions import (
    ProductNotFoundError,
    PurchaseOrderValidationError,
    SupplierNotFoundError,
    WarehouseNotFoundError,
)
from app.schemas.inventory import WarehouseOut
from app.schemas.product import ProductOut
from app.schemas.purchasing import PurchaseOrderCreate, PurchaseOrderItemInput, SupplierOut
from app.security.authorization import PermissionDeniedError
from app.services.purchase_service import PurchaseService
from app.ui.theme import RED, STYLESHEET
from app.ui.widgets.order_items_editor import OrderItemsEditor
from app.workers.base_worker import Worker


class PurchaseOrderFormDialog(QDialog):
    def __init__(self, purchase_service: PurchaseService, suppliers: list[SupplierOut],
                warehouses: list[WarehouseOut], products: list[ProductOut], parent=None):
        super().__init__(parent)
        self._purchase_service = purchase_service
        self.order = None
        self.setWindowTitle("Create Purchase Order")
        self.setMinimumWidth(520)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._supplier = QComboBox()
        for s in sorted(suppliers, key=lambda s: s.name.lower()):
            self._supplier.addItem(s.name, s.id)
        form.addRow("Supplier *", self._supplier)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(w.name, w.id)
        form.addRow("Deliver To *", self._warehouse)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Optional")
        form.addRow("Notes", self._notes)

        layout.addLayout(form)

        layout.addWidget(QLabel("Line Items"))
        self._items_editor = OrderItemsEditor(products, include_discount=False,
                                              price_label="Unit Cost")
        layout.addWidget(self._items_editor)

        if not suppliers or not warehouses or not products:
            missing = "suppliers" if not suppliers else (
                "warehouses" if not warehouses else "products")
            notice = QLabel(f"No {missing} available — add one before creating an order.")
            notice.setWordWrap(True)
            layout.addWidget(notice)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Create Purchase Order")
        self._submit_button.setObjectName("primary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(bool(suppliers) and bool(warehouses) and bool(products))
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Creating…" if busy else "Create Purchase Order")

    def _submit(self) -> None:
        self._error_label.hide()
        supplier_id = self._supplier.currentData()
        warehouse_id = self._warehouse.currentData()
        if supplier_id is None or warehouse_id is None:
            return self._show_error("Select a supplier and a warehouse.")

        rows, errors = self._items_editor.collect_items()
        if errors:
            return self._show_error(" ".join(errors))

        items = [PurchaseOrderItemInput(product_id=r["product_id"],
                                        quantity_ordered=r["quantity"],
                                        unit_price=r["unit_price"],
                                        tax_percent=r["tax_percent"]) for r in rows]
        data = PurchaseOrderCreate(supplier_id=supplier_id, warehouse_id=warehouse_id,
                                   notes=self._notes.text().strip() or None, items=items)

        self._set_busy(True)
        worker = Worker(self._purchase_service.create_purchase_order, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, order) -> None:
        self._set_busy(False)
        self.order = order
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, PurchaseOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (SupplierNotFoundError, WarehouseNotFoundError,
                              ProductNotFoundError)):
            self._show_error(str(exc))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to create purchase orders.")
        else:
            self._show_error("Something went wrong creating this purchase order. "
                             "Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
