"""New Bill — build a sale from a blank slate: pick a customer and
warehouse, add real products via BillItemsTable, and either park it as a
DRAFT (touches nothing but the SalesOrder itself) or finalize it into a
real sale (confirm + fulfill + invoice + optional payment, all inside one
database transaction — see SalesOrderRepository.finalize_new_bill).

Every calculation shown here (line amounts, subtotal, item discount, tax,
grand total) is a live *preview* using the same app.domain.sales functions
the service/repository use to compute the real, saved numbers — this page
never invents its own math and never writes a total to the database itself;
Save Draft/Save as Sale always send raw line inputs and let the service
layer recompute and validate everything server-side.

No inventory is touched, no invoice number is assigned, and nothing is
audited until "Save as Sale" (or "Save & Print"/"Generate PDF", which are
the same finalize step, just followed by opening the invoice preview).
Save Draft only ever calls create_sales_order/update_sales_order, which -
per SalesOrderRepository.create/update - never write to Inventory.
"""
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QDate, QThreadPool, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    CreditLimitExceededError,
    CustomerNotFoundError,
    InsufficientStockError,
    SalesOrderValidationError,
    WarehouseNotFoundError,
)
from app.domain.sales import PaymentMethod, SalesOrderStatus
from app.schemas.sales import (
    CustomerBalance,
    CustomerOut,
    FinalizeSaleRequest,
    FinalizeSaleResult,
    SalesOrderCreate,
    SalesOrderItemInput,
    SalesOrderOut,
    SalesOrderUpdate,
)
from app.schemas.inventory import WarehouseOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.sales_service import SalesService
from app.ui import permission_hints
from app.ui.theme import GREEN, MUTED, RADIUS
from app.ui.widgets.bill_items_table import BillItemsTable
from app.ui.widgets.customer_form_dialog import CustomerFormDialog
from app.ui.widgets.invoice_preview_dialog import InvoicePreviewDialog
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.record_payment_dialog import RecordPaymentDialog
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

_NO_PAYMENT_NOW = "— No payment now —"

# Light-gray input styling for the bill-details card (Due Date, Customer,
# Warehouse, Notes) — QComboBox/QDateEdit/QTextEdit have no styling in the
# app-wide QSS (only QLineEdit does), so they'd otherwise fall back to
# native/dark rendering. Scoped to that card only, via group.setStyleSheet,
# so no other page's combo/date/text widgets are affected.
_INPUT_BG = "#F3F4F6"
_INPUT_BORDER = "#D1D5DB"
_INPUT_TEXT = "#172033"
_INPUT_PLACEHOLDER = "#6B7280"
_INPUT_FOCUS_BORDER = "#2196F3"

_BILL_DETAILS_STYLE = f"""
QComboBox, QDateEdit, QTextEdit {{
    background: {_INPUT_BG};
    color: {_INPUT_TEXT};
    border: 1px solid {_INPUT_BORDER};
    border-radius: {RADIUS}px;
    padding: 9px 12px;
    font-size: 13px;
}}
QComboBox:focus, QDateEdit:focus, QTextEdit:focus {{
    border: 1px solid {_INPUT_FOCUS_BORDER};
}}
QComboBox:disabled, QDateEdit:disabled {{
    color: {_INPUT_PLACEHOLDER};
}}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: #FFFFFF;
    color: {_INPUT_TEXT};
    border: 1px solid {_INPUT_BORDER};
    outline: none;
    selection-background-color: {_INPUT_FOCUS_BORDER};
    selection-color: #FFFFFF;
}}
QComboBox QLineEdit {{
    background: {_INPUT_BG};
    color: {_INPUT_TEXT};
    border: none;
    padding: 0;
}}
"""


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _parse_decimal(text: str) -> Decimal | None:
    text = text.strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


class NewBillPage(QWidget):
    def __init__(self, sales_service: SalesService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._sales_service = sales_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions

        self._customers: list[CustomerOut] = []
        self._warehouses: list[WarehouseOut] = []
        self._sales_order_id: uuid.UUID | None = None
        self._sales_order_status: SalesOrderStatus | None = None
        self._invoice_id: uuid.UUID | None = None
        self._invoice_number: str | None = None
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(PageHeader("New Bill", "Build a sale from live customer, product, "
                                             "and inventory data."))

        self._status_banner = QLabel("")
        self._status_banner.setStyleSheet(
            f"color: {MUTED}; font-size: 12px; padding: 0 28px 8px 28px;")
        outer.addWidget(self._status_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        outer.addLayout(self._build_actions_bar())

        self._load_reference_data()
        self._update_status_banner()
        self._update_button_states()

    # -- permissions -------------------------------------------------------#
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    def _can_finalize(self) -> bool:
        return all(self._can(code) for code in
                  ("sales.confirm", "sales.fulfill", "sales.invoice"))

    # -- left column: bill details + items ---------------------------------#
    def _build_left_column(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 6, 16, 16)
        layout.setSpacing(18)

        layout.addWidget(self._build_bill_details_group())

        products_card = QWidget()
        products_card.setObjectName("card")
        products_layout = QVBoxLayout(products_card)
        products_layout.setContentsMargins(20, 18, 20, 18)
        products_layout.setSpacing(10)

        items_label = QLabel("Products")
        items_label.setObjectName("sectionTitle")
        products_layout.addWidget(items_label)

        self._items_table = BillItemsTable(self._product_service, self._inventory_service)
        self._items_table.totals_changed.connect(self._on_totals_changed)
        self._items_table.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Expanding)
        products_layout.addWidget(self._items_table, stretch=1)
        layout.addWidget(products_card, stretch=1)

        scroll.setWidget(content)
        return scroll

    def _build_bill_details_group(self) -> QWidget:
        group = QWidget()
        group.setObjectName("card")
        form = QFormLayout(group)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._bill_number_label = QLabel("Assigned on save")
        self._bill_number_label.setStyleSheet(f"color: {MUTED};")
        form.addRow("Bill Number", self._bill_number_label)

        self._bill_date_label = QLabel(datetime.now().strftime("%Y-%m-%d"))
        self._bill_date_label.setStyleSheet(f"color: {MUTED};")
        form.addRow("Bill Date", self._bill_date_label)

        due_row = QHBoxLayout()
        self._due_date_check = QCheckBox("Set due date")
        self._due_date_check.toggled.connect(self._on_due_date_toggled)
        due_row.addWidget(self._due_date_check)
        self._due_date_edit = QDateEdit(QDate.currentDate())
        self._due_date_edit.setCalendarPopup(True)
        self._due_date_edit.setEnabled(False)
        due_row.addWidget(self._due_date_edit, stretch=1)
        form.addRow("Due Date", due_row)

        customer_row = QHBoxLayout()
        self._customer_combo = QComboBox()
        self._customer_combo.setEditable(True)
        self._customer_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._customer_combo.currentIndexChanged.connect(self._on_customer_changed)
        customer_row.addWidget(self._customer_combo, stretch=1)
        self._new_customer_button = QPushButton("+ New")
        self._new_customer_button.setObjectName("ghost")
        self._new_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_customer_button.clicked.connect(self._open_new_customer_dialog)
        customer_row.addWidget(self._new_customer_button)
        form.addRow("Customer *", customer_row)

        self._customer_balance_label = QLabel("")
        self._customer_balance_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self._customer_balance_label.setWordWrap(True)
        form.addRow("", self._customer_balance_label)

        self._warehouse_combo = QComboBox()
        form.addRow("Warehouse *", self._warehouse_combo)
        self._warehouse_combo.currentIndexChanged.connect(self._on_warehouse_changed)

        self._notes_edit = QTextEdit()
        self._notes_edit.setPlaceholderText("Notes for this bill (optional)")
        self._notes_edit.setMaximumHeight(64)
        form.addRow("Notes", self._notes_edit)

        notes_palette = self._notes_edit.palette()
        notes_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(_INPUT_PLACEHOLDER))
        self._notes_edit.setPalette(notes_palette)

        group.setStyleSheet(_BILL_DETAILS_STYLE)
        return group

    def _on_due_date_toggled(self, checked: bool) -> None:
        self._due_date_edit.setEnabled(checked)

    def _on_warehouse_changed(self) -> None:
        warehouse_id = self._warehouse_combo.currentData()
        self._items_table.set_warehouse(warehouse_id)

    # -- right column: totals + payment -------------------------------------#
    def _build_right_column(self) -> QWidget:
        panel = QWidget()
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        outer_layout = QVBoxLayout(panel)
        outer_layout.setContentsMargins(16, 6, 28, 16)
        outer_layout.setSpacing(18)

        card = QWidget()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        totals_title = QLabel("Totals")
        totals_title.setObjectName("sectionTitle")
        layout.addWidget(totals_title)

        form = QFormLayout()
        form.setSpacing(8)

        self._subtotal_label = QLabel(_money(Decimal("0")))
        form.addRow("Subtotal", self._subtotal_label)

        self._item_discount_label = QLabel(_money(Decimal("0")))
        form.addRow("Item Discount", self._item_discount_label)

        self._overall_discount_edit = QLineEdit("0")
        self._overall_discount_edit.textChanged.connect(self._on_totals_changed)
        form.addRow("Overall Discount", self._overall_discount_edit)

        self._tax_label = QLabel(_money(Decimal("0")))
        form.addRow("Tax", self._tax_label)

        self._other_charges_edit = QLineEdit("0")
        self._other_charges_edit.textChanged.connect(self._on_totals_changed)
        form.addRow("Other Charges", self._other_charges_edit)

        self._grand_total_label = QLabel(_money(Decimal("0")))
        self._grand_total_label.setStyleSheet("font-weight: 600; font-size: 15px;")
        form.addRow("Grand Total", self._grand_total_label)

        layout.addLayout(form)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        payment_title = QLabel("Payment")
        payment_title.setObjectName("sectionTitle")
        layout.addWidget(payment_title)

        payment_form = QFormLayout()
        payment_form.setSpacing(8)

        self._payment_method_combo = QComboBox()
        self._payment_method_combo.addItem(_NO_PAYMENT_NOW, None)
        for method in PaymentMethod:
            self._payment_method_combo.addItem(method.value.replace("_", " ").title(), method)
        self._payment_method_combo.currentIndexChanged.connect(self._on_totals_changed)
        payment_form.addRow("Method", self._payment_method_combo)

        self._payment_amount_edit = QLineEdit("0")
        self._payment_amount_edit.textChanged.connect(self._on_totals_changed)
        payment_form.addRow("Amount Received", self._payment_amount_edit)

        self._balance_due_label = QLabel(_money(Decimal("0")))
        self._balance_due_label.setStyleSheet("font-weight: 600;")
        payment_form.addRow("Balance Due", self._balance_due_label)

        layout.addLayout(payment_form)

        outer_layout.addWidget(card)
        outer_layout.addStretch()
        return panel

    def _on_totals_changed(self) -> None:
        subtotal, item_discount, tax_total, _line_total_sum = self._items_table.compute_totals()
        overall_discount = _parse_decimal(self._overall_discount_edit.text()) or Decimal("0")
        other_charges = _parse_decimal(self._other_charges_edit.text()) or Decimal("0")
        grand_total = subtotal - item_discount - overall_discount + tax_total + other_charges

        self._subtotal_label.setText(_money(subtotal))
        self._item_discount_label.setText(_money(item_discount))
        self._tax_label.setText(_money(tax_total))
        self._grand_total_label.setText(_money(grand_total))

        payment_amount = Decimal("0")
        if self._payment_method_combo.currentData() is not None:
            payment_amount = _parse_decimal(self._payment_amount_edit.text()) or Decimal("0")
        balance_due = grand_total - payment_amount
        self._balance_due_label.setText(_money(balance_due))
        self._balance_due_label.setStyleSheet(
            f"font-weight: 600; color: {GREEN if balance_due <= 0 else MUTED};")

    # -- actions bar --------------------------------------------------------#
    def _build_actions_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 8, 28, 20)
        bar.setSpacing(10)

        self._clear_button = QPushButton("Clear")
        self._clear_button.setObjectName("ghost")
        self._clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_button.clicked.connect(self._on_clear)
        bar.addWidget(self._clear_button)

        bar.addStretch()

        self._record_payment_button = QPushButton("Record Payment")
        self._record_payment_button.setObjectName("ghost")
        self._record_payment_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._record_payment_button.clicked.connect(self._on_record_payment)
        bar.addWidget(self._record_payment_button)

        self._generate_pdf_button = QPushButton("Generate PDF")
        self._generate_pdf_button.setObjectName("ghost")
        self._generate_pdf_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._generate_pdf_button.clicked.connect(lambda: self._on_finalize(then_open=True))
        bar.addWidget(self._generate_pdf_button)

        self._save_print_button = QPushButton("Save && Print")
        self._save_print_button.setObjectName("ghost")
        self._save_print_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_print_button.clicked.connect(lambda: self._on_finalize(then_open=True))
        bar.addWidget(self._save_print_button)

        self._save_draft_button = QPushButton("Save Draft")
        self._save_draft_button.setObjectName("ghost")
        self._save_draft_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_draft_button.clicked.connect(self._on_save_draft)
        bar.addWidget(self._save_draft_button)

        self._save_sale_button = QPushButton("Save as Sale")
        self._save_sale_button.setObjectName("primary")
        self._save_sale_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_sale_button.clicked.connect(lambda: self._on_finalize(then_open=False))
        bar.addWidget(self._save_sale_button)

        return bar

    def _update_button_states(self) -> None:
        can_finalize = self._can_finalize() and not self._busy
        has_invoice = self._invoice_id is not None
        is_editable_draft = self._sales_order_status in (None, SalesOrderStatus.DRAFT)

        self._save_draft_button.setEnabled(
            not self._busy and is_editable_draft
            and self._can("sales.update" if self._sales_order_id else "sales.create"))
        self._save_sale_button.setEnabled(can_finalize and is_editable_draft and not has_invoice)
        self._save_print_button.setEnabled(
            (can_finalize and is_editable_draft and not has_invoice) or has_invoice)
        self._generate_pdf_button.setEnabled(
            (can_finalize and is_editable_draft and not has_invoice) or has_invoice)
        self._record_payment_button.setEnabled(
            has_invoice and self._can("sales.payment") and not self._busy)
        self._new_customer_button.setEnabled(self._can("customers.create") and not self._busy)
        self._clear_button.setEnabled(not self._busy)

    def _update_status_banner(self) -> None:
        if self._invoice_id is not None:
            self._status_banner.setText(
                f"Invoiced as {self._invoice_number} — this bill is complete. "
                "Start a new bill with Clear, or use Record Payment to collect more.")
        elif self._sales_order_id is not None:
            self._status_banner.setText(
                "Draft saved — inventory has not been touched yet. "
                "Use Save as Sale to confirm, fulfill, and invoice it.")
        else:
            self._status_banner.setText(
                "Unsaved — nothing is recorded until you Save Draft or Save as Sale.")

    # -- reference data ------------------------------------------------------#
    def _fetch_reference_data(self):
        # Inactive customers/warehouses are excluded from the picker — a
        # deactivated customer/warehouse must not be selectable for a
        # brand new bill (see CustomersPage._deactivate's promise that a
        # deactivated customer "won't be selectable for new sales orders'
        # filters going forward"). Extracted from _load_reference_data so
        # it's directly unit-testable without going through QThreadPool —
        # see tests/ui/test_new_bill_page.py.
        customers = [c for c in self._sales_service.list_customers() if c.is_active]
        warehouses = [w for w in self._inventory_service.list_warehouses() if w.is_active]
        return customers, warehouses

    def _load_reference_data(self) -> None:
        worker = Worker(self._fetch_reference_data)
        worker.signals.finished.connect(self._on_reference_data_loaded)
        worker.signals.error.connect(self._on_reference_data_error)
        QThreadPool.globalInstance().start(worker)

    def _on_reference_data_loaded(self, result) -> None:
        customers, warehouses = result
        self._customers = customers
        self._warehouses = warehouses

        current_customer_id = self._customer_combo.currentData()
        self._customer_combo.blockSignals(True)
        self._customer_combo.clear()
        for customer in customers:
            self._customer_combo.addItem(customer.name, customer.id)
        self._customer_combo.blockSignals(False)
        if current_customer_id is not None:
            index = self._customer_combo.findData(current_customer_id)
            if index >= 0:
                self._customer_combo.setCurrentIndex(index)

        self._warehouse_combo.blockSignals(True)
        self._warehouse_combo.clear()
        for warehouse in warehouses:
            self._warehouse_combo.addItem(f"{warehouse.code} — {warehouse.name}", warehouse.id)
        self._warehouse_combo.blockSignals(False)
        self._on_warehouse_changed()
        self._on_customer_changed()

    def _on_reference_data_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load New Bill reference data", exc_info=exc)
        QMessageBox.critical(self, "Couldn't load data",
                            "Failed to load customers/warehouses. Please retry.")

    # -- customer -------------------------------------------------------------#
    def _on_customer_changed(self) -> None:
        customer_id = self._customer_combo.currentData()
        if customer_id is None:
            self._customer_balance_label.setText("")
            return
        worker = Worker(self._sales_service.get_customer_balance, customer_id)
        worker.signals.finished.connect(self._on_customer_balance_loaded)
        worker.signals.error.connect(
            lambda exc: _logger.exception("Failed to load customer balance", exc_info=exc))
        QThreadPool.globalInstance().start(worker)

    def _on_customer_balance_loaded(self, balance: CustomerBalance) -> None:
        credit = ("no limit" if balance.available_credit is None
                 else _money(balance.available_credit))
        self._customer_balance_label.setText(
            f"Outstanding: {_money(balance.outstanding_balance)}  •  "
            f"Available credit: {credit}")

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
        worker.signals.error.connect(self._on_reference_data_error)
        QThreadPool.globalInstance().start(worker)

    def _on_customer_created(self, customers: list[CustomerOut],
                             existing_ids: set[uuid.UUID]) -> None:
        self._customers = customers
        self._customer_combo.blockSignals(True)
        self._customer_combo.clear()
        for customer in customers:
            self._customer_combo.addItem(customer.name, customer.id)
        self._customer_combo.blockSignals(False)

        new_customer = next((c for c in customers if c.id not in existing_ids), None)
        if new_customer is not None:
            index = self._customer_combo.findData(new_customer.id)
            if index >= 0:
                self._customer_combo.setCurrentIndex(index)
        self._on_customer_changed()

    # -- validation ------------------------------------------------------------#
    def _collect_items(self) -> tuple[list[SalesOrderItemInput], list[str]]:
        raw_items, errors = self._items_table.collect_items()
        items = [SalesOrderItemInput(
            product_id=raw["product_id"], quantity_ordered=raw["quantity"],
            unit_price=raw["unit_price"], tax_percent=raw["tax_percent"],
            discount_percent=raw["discount_percent"]) for raw in raw_items]
        return items, errors

    def _validate_common(self) -> list[str]:
        errors = []
        if self._customer_combo.currentData() is None:
            errors.append("Select a customer for this bill.")
        if self._warehouse_combo.currentData() is None:
            errors.append("Select a warehouse for this bill.")
        return errors

    # -- save draft --------------------------------------------------------- #
    def _on_save_draft(self) -> None:
        items, item_errors = self._collect_items()
        errors = self._validate_common() + item_errors
        if errors:
            QMessageBox.warning(self, "Can't save draft", "\n".join(errors))
            return

        customer_id = self._customer_combo.currentData()
        warehouse_id = self._warehouse_combo.currentData()
        notes = self._notes_edit.toPlainText().strip() or None

        self._set_busy(True)
        if self._sales_order_id is None:
            data = SalesOrderCreate(customer_id=customer_id, warehouse_id=warehouse_id,
                                    notes=notes, items=items)
            worker = Worker(self._sales_service.create_sales_order, data)
        else:
            data = SalesOrderUpdate(customer_id=customer_id, warehouse_id=warehouse_id,
                                    notes=notes, items=items)
            worker = Worker(self._sales_service.update_sales_order, self._sales_order_id, data)
        worker.signals.finished.connect(self._on_draft_saved)
        worker.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(worker)

    def _on_draft_saved(self, sales_order: SalesOrderOut) -> None:
        self._set_busy(False)
        self._sales_order_id = sales_order.id
        self._sales_order_status = sales_order.status
        self._update_status_banner()
        self._update_button_states()
        QMessageBox.information(self, "Draft saved", "This bill has been saved as a draft. "
                               "No inventory has been affected yet.")

    # -- finalize (Save as Sale / Save & Print / Generate PDF) -------------- #
    def _on_finalize(self, *, then_open: bool) -> None:
        if self._invoice_id is not None:
            # Already invoiced — these buttons just reopen the preview.
            self._open_invoice_preview()
            return

        items, item_errors = self._collect_items()
        errors = self._validate_common() + item_errors

        overall_discount = _parse_decimal(self._overall_discount_edit.text())
        if overall_discount is None or overall_discount < 0:
            errors.append("Overall discount must be a non-negative number.")
        other_charges = _parse_decimal(self._other_charges_edit.text())
        if other_charges is None or other_charges < 0:
            errors.append("Other charges must be a non-negative number.")

        payment_method = self._payment_method_combo.currentData()
        payment_amount = None
        if payment_method is not None:
            payment_amount = _parse_decimal(self._payment_amount_edit.text())
            if payment_amount is None or payment_amount <= 0:
                errors.append("Enter a valid payment amount greater than zero, or set "
                             "Method back to “No payment now”.")

        if errors:
            QMessageBox.warning(self, "Can't save this bill", "\n".join(errors))
            return

        due_date: date | None = None
        if self._due_date_check.isChecked():
            due_date = self._due_date_edit.date().toPython()

        self._pending_then_open = then_open
        self._set_busy(True)

        if self._sales_order_id is None:
            self._finalize_after_draft_create(items, overall_discount, other_charges,
                                              payment_amount, payment_method, due_date)
        else:
            self._finalize_existing_draft(overall_discount, other_charges, payment_amount,
                                          payment_method, due_date)

    def _finalize_after_draft_create(self, items, overall_discount, other_charges,
                                     payment_amount, payment_method, due_date) -> None:
        data = SalesOrderCreate(customer_id=self._customer_combo.currentData(),
                                warehouse_id=self._warehouse_combo.currentData(),
                                notes=self._notes_edit.toPlainText().strip() or None,
                                items=items)

        def work():
            sales_order = self._sales_service.create_sales_order(data)
            finalize_data = FinalizeSaleRequest(
                due_date=due_date, overall_discount_amount=overall_discount,
                other_charges=other_charges, payment_amount=payment_amount,
                payment_method=payment_method)
            return self._sales_service.finalize_new_bill(sales_order.id, finalize_data)

        worker = Worker(work)
        worker.signals.finished.connect(self._on_finalized)
        worker.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(worker)

    def _finalize_existing_draft(self, overall_discount, other_charges, payment_amount,
                                 payment_method, due_date) -> None:
        items, _errors = self._collect_items()
        update = SalesOrderUpdate(customer_id=self._customer_combo.currentData(),
                                  warehouse_id=self._warehouse_combo.currentData(),
                                  notes=self._notes_edit.toPlainText().strip() or None,
                                  items=items)

        def work():
            self._sales_service.update_sales_order(self._sales_order_id, update)
            finalize_data = FinalizeSaleRequest(
                due_date=due_date, overall_discount_amount=overall_discount,
                other_charges=other_charges, payment_amount=payment_amount,
                payment_method=payment_method)
            return self._sales_service.finalize_new_bill(self._sales_order_id, finalize_data)

        worker = Worker(work)
        worker.signals.finished.connect(self._on_finalized)
        worker.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(worker)

    def _on_finalized(self, result: FinalizeSaleResult) -> None:
        self._set_busy(False)
        self._sales_order_id = result.sales_order.id
        self._sales_order_status = result.sales_order.status
        self._invoice_id = result.invoice.id
        self._invoice_number = result.invoice.invoice_number
        self._bill_number_label.setText(result.invoice.invoice_number)
        self._bill_number_label.setStyleSheet("")
        self._update_status_banner()
        self._update_button_states()
        self._on_customer_changed()  # outstanding balance now includes this bill
        if getattr(self, "_pending_then_open", False):
            self._open_invoice_preview()
        else:
            QMessageBox.information(self, "Sale saved",
                                   f"Invoice {result.invoice.invoice_number} created "
                                   "and inventory updated.")

    def _open_invoice_preview(self) -> None:
        if self._invoice_id is None:
            return
        dialog = InvoicePreviewDialog(self._sales_service, self._invoice_id, parent=self)
        dialog.exec()

    def _on_save_error(self, exc: Exception) -> None:
        self._set_busy(False)
        _logger.exception("New Bill save failed", exc_info=exc)
        if isinstance(exc, SalesOrderValidationError):
            message = " ".join(exc.errors)
        elif isinstance(exc, (CustomerNotFoundError, WarehouseNotFoundError,
                             InsufficientStockError, CreditLimitExceededError)):
            message = str(exc)
        elif isinstance(exc, PermissionDeniedError):
            message = "You don't have permission to complete this action."
        else:
            message = "Something went wrong saving this bill. Please try again."
        QMessageBox.critical(self, "Couldn't save this bill", message)

    # -- record payment (post-invoice, additional payments) -------------------#
    def _on_record_payment(self) -> None:
        if self._invoice_id is None:
            return
        dialog = RecordPaymentDialog(self._sales_service, self._invoice_id, parent=self)
        if dialog.exec():
            QMessageBox.information(self, "Payment recorded",
                                   "Payment recorded against this invoice.")

    # -- clear -----------------------------------------------------------------#
    def _on_clear(self) -> None:
        self._sales_order_id = None
        self._sales_order_status = None
        self._invoice_id = None
        self._invoice_number = None
        self._items_table.clear_items()
        self._overall_discount_edit.setText("0")
        self._other_charges_edit.setText("0")
        self._payment_method_combo.setCurrentIndex(0)
        self._payment_amount_edit.setText("0")
        self._due_date_check.setChecked(False)
        self._notes_edit.clear()
        self._bill_number_label.setText("Assigned on save")
        self._bill_number_label.setStyleSheet(f"color: {MUTED};")
        self._bill_date_label.setText(datetime.now().strftime("%Y-%m-%d"))
        self._on_totals_changed()
        self._update_status_banner()
        self._update_button_states()

    # -- busy state --------------------------------------------------------- #
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_button_states()

    def refresh(self) -> None:
        self._load_reference_data()
