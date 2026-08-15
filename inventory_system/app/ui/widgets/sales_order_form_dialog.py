"""Create Sales Order dialog — a DRAFT order (customer, warehouse, line
items) via SalesService.create_sales_order. Customers/products/warehouses
are passed in already-fetched by the caller (SalesOrdersPage), same
convention as every other form dialog in app.ui.widgets — see
ProductFormDialog's docstring for why.

No business logic here: line-item shape/positivity, customer/warehouse/
product existence, and the DRAFT-only edit rule all live in SalesService /
app.domain.sales.
"""
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
    CustomerNotFoundError,
    ProductNotFoundError,
    SalesOrderValidationError,
    WarehouseNotFoundError,
)
from app.schemas.inventory import WarehouseOut
from app.schemas.product import ProductOut
from app.schemas.sales import CustomerOut, SalesOrderCreate, SalesOrderItemInput
from app.security.authorization import PermissionDeniedError
from app.services.sales_service import SalesService
from app.ui.theme import RED, STYLESHEET
from app.ui.widgets.order_items_editor import OrderItemsEditor
from app.workers.base_worker import Worker


class SalesOrderFormDialog(QDialog):
    def __init__(self, sales_service: SalesService, customers: list[CustomerOut],
                warehouses: list[WarehouseOut], products: list[ProductOut], parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self.order = None
        self.setWindowTitle("Create Sales Order")
        self.setMinimumWidth(560)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._customer = QComboBox()
        for c in sorted(customers, key=lambda c: c.name.lower()):
            self._customer.addItem(c.name, c.id)
        form.addRow("Customer *", self._customer)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(w.name, w.id)
        form.addRow("Ship From *", self._warehouse)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Optional")
        form.addRow("Notes", self._notes)

        layout.addLayout(form)

        layout.addWidget(QLabel("Line Items"))
        self._items_editor = OrderItemsEditor(products, include_discount=True,
                                              price_label="Unit Price")
        layout.addWidget(self._items_editor)

        if not customers or not warehouses or not products:
            missing = "customers" if not customers else (
                "warehouses" if not warehouses else "products")
            notice = QLabel(f"No {missing} available — add one before creating an order.")
            notice.setWordWrap(True)
            layout.addWidget(notice)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Create Sales Order")
        self._submit_button.setObjectName("primary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(bool(customers) and bool(warehouses) and bool(products))
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Creating…" if busy else "Create Sales Order")

    def _submit(self) -> None:
        self._error_label.hide()
        customer_id = self._customer.currentData()
        warehouse_id = self._warehouse.currentData()
        if customer_id is None or warehouse_id is None:
            return self._show_error("Select a customer and a warehouse.")

        rows, errors = self._items_editor.collect_items()
        if errors:
            return self._show_error(" ".join(errors))

        items = [SalesOrderItemInput(product_id=r["product_id"],
                                     quantity_ordered=r["quantity"],
                                     unit_price=r["unit_price"],
                                     tax_percent=r["tax_percent"],
                                     discount_percent=r["discount_percent"]) for r in rows]
        data = SalesOrderCreate(customer_id=customer_id, warehouse_id=warehouse_id,
                                notes=self._notes.text().strip() or None, items=items)

        self._set_busy(True)
        worker = Worker(self._sales_service.create_sales_order, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, order) -> None:
        self._set_busy(False)
        self.order = order
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, SalesOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (CustomerNotFoundError, WarehouseNotFoundError,
                              ProductNotFoundError)):
            self._show_error(str(exc))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to create sales orders.")
        else:
            self._show_error("Something went wrong creating this sales order. "
                             "Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
