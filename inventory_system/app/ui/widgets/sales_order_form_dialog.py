"""Create Sales Order dialog — a DRAFT order (customer, warehouse, line
items) via SalesService.create_sales_order. Customers/warehouses are
passed in already-fetched by the caller (SalesOrdersPage); products are
searched live through ProductService, same convention as New Bill's
BillItemsTable — see TransactionItemsTable's docstring for why this and
PurchaseOrderFormDialog now share that one items-table widget instead of
each rolling their own.

No business logic here: line-item shape/positivity, customer/warehouse/
product existence, and the DRAFT-only edit rule all live in SalesService /
app.domain.sales. A sales order created here is always a DRAFT — payment
and invoicing happen later, via the Confirm/Fulfill/Generate Invoice/
Record Payment actions already on the Sales list (or via the New Bill
page, which does the full create-through-invoice flow in one step); this
dialog deliberately doesn't duplicate that finalize logic.
"""
from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    CustomerNotFoundError,
    ProductNotFoundError,
    SalesOrderValidationError,
    WarehouseNotFoundError,
)
from app.schemas.inventory import WarehouseOut
from app.schemas.sales import CustomerBalance, CustomerOut, SalesOrderCreate, SalesOrderItemInput
from app.security.authorization import PermissionDeniedError
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.sales_service import SalesService
from app.ui.theme import RED, STYLESHEET
from app.ui.widgets.customer_form_dialog import CustomerFormDialog
from app.ui.widgets.order_form_style import ORDER_FORM_STYLESHEET, apply_card_shadow, field_label
from app.ui.widgets.transaction_items_table import TransactionItemsTable
from app.workers.base_worker import Worker


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


class SalesOrderFormDialog(QDialog):
    def __init__(self, sales_service: SalesService, product_service: ProductService,
                inventory_service: InventoryService, customers: list[CustomerOut],
                warehouses: list[WarehouseOut], parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._customers = list(customers)
        self.order = None
        self.setWindowTitle("Create Sales Order")
        self.setMinimumWidth(760)
        self.resize(800, 680)
        self.setStyleSheet(STYLESHEET + ORDER_FORM_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(14)

        title = QLabel("New Sales Order")
        title.setObjectName("formTitle")
        outer.addWidget(title)
        subtitle = QLabel("Create a draft order for a customer — inventory isn't "
                          "touched until it's confirmed and fulfilled.")
        subtitle.setObjectName("formSubtitle")
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 4, 6, 0)
        content_layout.setSpacing(16)

        content_layout.addWidget(self._build_order_info_card(warehouses))
        content_layout.addWidget(self._build_items_card(product_service, inventory_service))
        content_layout.addWidget(self._build_notes_card())

        if not customers or not warehouses:
            missing = "customers" if not customers else "warehouses"
            notice = QLabel(f"No {missing} available yet — add one before creating a "
                            "sales order.")
            notice.setObjectName("secondaryText")
            notice.setWordWrap(True)
            content_layout.addWidget(notice)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        content_layout.addWidget(self._error_label)

        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        outer.addLayout(self._build_footer(bool(customers), bool(warehouses)))

        self._populate_customers()
        self._on_customer_changed()
        self._on_warehouse_changed()
        self._on_totals_changed()

    # -- order information ---------------------------------------------------#
    def _build_order_info_card(self, warehouses: list[WarehouseOut]) -> QWidget:
        card = QWidget()
        card.setObjectName("formCard")
        apply_card_shadow(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        section_title = QLabel("Order Information")
        section_title.setObjectName("sectionTitle")
        layout.addWidget(section_title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        customer_row = QHBoxLayout()
        customer_row.setSpacing(8)
        self._customer = QComboBox()
        # Editable + NoInsert: type-to-filter a customer by name instead of
        # scrolling a flat list, same convention New Bill's Customer field
        # already uses.
        self._customer.setEditable(True)
        self._customer.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._customer.currentIndexChanged.connect(self._on_customer_changed)
        customer_row.addWidget(self._customer, stretch=1)
        self._new_customer_button = QPushButton("+ New")
        self._new_customer_button.setObjectName("orderGhost")
        self._new_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_customer_button.clicked.connect(self._open_new_customer_dialog)
        customer_row.addWidget(self._new_customer_button)
        grid.addWidget(field_label("Customer *"), 0, 0)
        grid.addLayout(customer_row, 1, 0)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(w.name, w.id)
        self._warehouse.currentIndexChanged.connect(self._on_warehouse_changed)
        grid.addWidget(field_label("Ship From *"), 0, 1)
        grid.addWidget(self._warehouse, 1, 1)

        self._customer_balance_label = QLabel("")
        self._customer_balance_label.setObjectName("secondaryText")
        self._customer_balance_label.setWordWrap(True)
        grid.addWidget(self._customer_balance_label, 2, 0, 1, 2)

        invoice_number_value = QLabel("Assigned when invoiced")
        invoice_number_value.setObjectName("readOnlyValue")
        grid.addWidget(field_label("Invoice Number"), 3, 0)
        grid.addWidget(invoice_number_value, 4, 0)

        order_date_value = QLabel(datetime.now().strftime("%Y-%m-%d"))
        order_date_value.setObjectName("readOnlyValue")
        grid.addWidget(field_label("Order Date"), 3, 1)
        grid.addWidget(order_date_value, 4, 1)

        layout.addLayout(grid)
        return card

    def _populate_customers(self) -> None:
        self._customer.blockSignals(True)
        self._customer.clear()
        for c in sorted(self._customers, key=lambda c: c.name.lower()):
            self._customer.addItem(c.name, c.id)
        self._customer.blockSignals(False)

    def _on_customer_changed(self) -> None:
        customer_id = self._customer.currentData()
        if customer_id is None:
            self._customer_balance_label.setText("")
            return
        worker = Worker(self._sales_service.get_customer_balance, customer_id)
        worker.signals.finished.connect(self._on_customer_balance_loaded)
        worker.signals.error.connect(lambda _exc: self._customer_balance_label.setText(""))
        QThreadPool.globalInstance().start(worker)

    def _on_customer_balance_loaded(self, balance: CustomerBalance) -> None:
        credit = ("no limit" if balance.available_credit is None
                 else _money(balance.available_credit))
        self._customer_balance_label.setText(
            f"Outstanding: {_money(balance.outstanding_balance)}  •  "
            f"Available credit: {credit}")

    def _on_warehouse_changed(self) -> None:
        if hasattr(self, "_items_editor"):
            self._items_editor.set_warehouse(self._warehouse.currentData())

    def _open_new_customer_dialog(self) -> None:
        existing_ids = {c.id for c in self._customers}
        dialog = CustomerFormDialog(self._sales_service, parent=self)
        if not dialog.exec():
            return

        def load():
            return self._sales_service.list_customers()

        worker = Worker(load)
        worker.signals.finished.connect(
            lambda customers: self._on_customer_created(customers, existing_ids))
        QThreadPool.globalInstance().start(worker)

    def _on_customer_created(self, customers: list[CustomerOut],
                             existing_ids: set) -> None:
        self._customers = customers
        self._populate_customers()
        new_customer = next((c for c in customers if c.id not in existing_ids), None)
        if new_customer is not None:
            index = self._customer.findData(new_customer.id)
            if index >= 0:
                self._customer.setCurrentIndex(index)
        self._on_customer_changed()
        self._submit_button.setEnabled(True)

    # -- items -----------------------------------------------------------------#
    def _build_items_card(self, product_service: ProductService,
                          inventory_service: InventoryService) -> QWidget:
        card = QWidget()
        card.setObjectName("formCard")
        apply_card_shadow(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        section_title = QLabel("Order Items")
        section_title.setObjectName("sectionTitle")
        layout.addWidget(section_title)

        self._items_editor = TransactionItemsTable(
            product_service, inventory_service, include_discount=True,
            price_label="Rate", price_field="selling_price")
        self._items_editor.totals_changed.connect(self._on_totals_changed)
        layout.addWidget(self._items_editor)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        totals_row = QHBoxLayout()
        totals_row.addStretch()

        totals_grid = QGridLayout()
        totals_grid.setHorizontalSpacing(24)
        totals_grid.setVerticalSpacing(4)

        totals_grid.addWidget(self._totals_caption("Subtotal"), 0, 0)
        self._subtotal_value = self._totals_value()
        totals_grid.addWidget(self._subtotal_value, 0, 1)

        totals_grid.addWidget(self._totals_caption("Discount"), 1, 0)
        self._discount_value = self._totals_value()
        totals_grid.addWidget(self._discount_value, 1, 1)

        totals_grid.addWidget(self._totals_caption("Non-taxable Total"), 2, 0)
        self._non_taxable_value = self._totals_value()
        totals_grid.addWidget(self._non_taxable_value, 2, 1)

        totals_grid.addWidget(self._totals_caption("Taxable Total"), 3, 0)
        self._taxable_value = self._totals_value()
        totals_grid.addWidget(self._taxable_value, 3, 1)

        totals_grid.addWidget(self._totals_caption("Tax"), 4, 0)
        self._tax_value = self._totals_value()
        totals_grid.addWidget(self._tax_value, 4, 1)

        grand_label = QLabel("Grand Total")
        grand_label.setObjectName("grandTotalLabel")
        totals_grid.addWidget(grand_label, 5, 0)
        self._grand_total_value = QLabel(_money(Decimal("0")))
        self._grand_total_value.setObjectName("grandTotalValue")
        totals_grid.addWidget(self._grand_total_value, 5, 1,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        totals_row.addLayout(totals_grid)
        layout.addLayout(totals_row)
        return card

    @staticmethod
    def _totals_caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("secondaryText")
        return label

    @staticmethod
    def _totals_value() -> QLabel:
        label = QLabel(_money(Decimal("0")))
        label.setStyleSheet("font-size: 13px; font-weight: 600;")
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return label

    def _on_totals_changed(self) -> None:
        subtotal, discount_total, tax_total, grand_total = self._items_editor.compute_totals()
        non_taxable, taxable = self._items_editor.compute_tax_split()
        self._subtotal_value.setText(_money(subtotal))
        self._discount_value.setText(_money(discount_total))
        self._non_taxable_value.setText(_money(non_taxable))
        self._taxable_value.setText(_money(taxable))
        self._tax_value.setText(_money(tax_total))
        self._grand_total_value.setText(_money(grand_total))

    # -- notes -------------------------------------------------------------------#
    def _build_notes_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("formCard")
        apply_card_shadow(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        section_title = QLabel("Notes")
        section_title.setObjectName("sectionTitle")
        layout.addWidget(section_title)

        self._notes = QTextEdit()
        self._notes.setPlaceholderText("Optional — delivery instructions, special "
                                       "terms, etc.")
        self._notes.setMaximumHeight(72)
        layout.addWidget(self._notes)
        return card

    # -- footer -------------------------------------------------------------------#
    def _build_footer(self, has_customers: bool, has_warehouses: bool) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 6, 0, 0)
        bar.addStretch()

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("orderSecondary")
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        bar.addWidget(cancel_button)

        # A sales order created here is always a DRAFT (Confirm/Fulfill/
        # Generate Invoice/Record Payment happen later, as separate row
        # actions on the Sales list — or use New Bill for the full
        # create-through-invoice flow in one step) — labeled "Save Draft"
        # to be honest about what this button does and to read the same
        # as New Bill's own "Save Draft" action for the equivalent step.
        self._submit_button = QPushButton("Save Draft")
        self._submit_button.setObjectName("orderPrimary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(has_customers and has_warehouses)
        self._submit_button.clicked.connect(self._submit)
        bar.addWidget(self._submit_button)
        return bar

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Saving…" if busy else "Save Draft")

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
                                notes=self._notes.toPlainText().strip() or None, items=items)

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
