"""Create Purchase Order dialog — a DRAFT order (supplier, warehouse, line
items) via PurchaseService.create_purchase_order. Suppliers/warehouses are
passed in already-fetched by the caller (PurchasesPage); products are
searched live through ProductService, same convention as New Bill's
BillItemsTable — see TransactionItemsTable's docstring for why this and
SalesOrderFormDialog now share that one items-table widget instead of each
rolling their own.

No business logic here: line-item shape/positivity, supplier/warehouse/
product existence, and the DRAFT-only edit rule all live in
PurchaseService / app.domain.purchasing. A purchase order created here is
always a DRAFT — stock only increases later, when goods are actually
received (PurchaseService.receive_goods), never at creation time.
"""
from datetime import date, datetime
from decimal import Decimal

from PySide6.QtCore import QDate, QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
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
    ProductNotFoundError,
    PurchaseOrderValidationError,
    SupplierNotFoundError,
    WarehouseNotFoundError,
)
from app.schemas.inventory import WarehouseOut
from app.schemas.purchasing import PurchaseOrderCreate, PurchaseOrderItemInput, SupplierOut
from app.security.authorization import PermissionDeniedError
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.purchase_service import PurchaseService
from app.ui.theme import RED, STYLESHEET
from app.ui.widgets.order_form_style import ORDER_FORM_STYLESHEET, apply_card_shadow, field_label
from app.ui.widgets.transaction_items_table import TransactionItemsTable
from app.workers.base_worker import Worker


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


class PurchaseOrderFormDialog(QDialog):
    def __init__(self, purchase_service: PurchaseService, product_service: ProductService,
                inventory_service: InventoryService, suppliers: list[SupplierOut],
                warehouses: list[WarehouseOut], parent=None):
        super().__init__(parent)
        self._purchase_service = purchase_service
        self.order = None
        self.setWindowTitle("Create Purchase Order")
        self.setMinimumWidth(760)
        self.resize(800, 680)
        self.setStyleSheet(STYLESHEET + ORDER_FORM_STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(14)

        title = QLabel("New Purchase Order")
        title.setObjectName("formTitle")
        outer.addWidget(title)
        subtitle = QLabel("Create a draft order for a supplier — nothing is sent or "
                          "billed until you save it.")
        subtitle.setObjectName("formSubtitle")
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 4, 6, 0)
        content_layout.setSpacing(16)

        content_layout.addWidget(self._build_order_info_card(suppliers, warehouses))
        content_layout.addWidget(self._build_items_card(product_service, inventory_service))
        content_layout.addWidget(self._build_notes_card())

        if not suppliers or not warehouses:
            missing = "suppliers" if not suppliers else "warehouses"
            notice = QLabel(f"No {missing} available yet — add one before creating a "
                            "purchase order.")
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

        outer.addLayout(self._build_footer(bool(suppliers), bool(warehouses)))

        self._on_warehouse_changed()
        self._on_totals_changed()

    # -- order information ---------------------------------------------------#
    def _build_order_info_card(self, suppliers: list[SupplierOut],
                               warehouses: list[WarehouseOut]) -> QWidget:
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

        self._supplier = QComboBox()
        # Editable + NoInsert: lets the user type to filter/jump to a
        # supplier by name instead of scrolling a flat list — same
        # search-by-typing convention New Bill's Customer field already
        # uses (QComboBox has no separate "search" mode of its own).
        self._supplier.setEditable(True)
        self._supplier.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for s in sorted(suppliers, key=lambda s: s.name.lower()):
            self._supplier.addItem(s.name, s.id)
        grid.addWidget(field_label("Supplier *"), 0, 0)
        grid.addWidget(self._supplier, 1, 0)

        self._warehouse = QComboBox()
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(w.name, w.id)
        self._warehouse.currentIndexChanged.connect(self._on_warehouse_changed)
        grid.addWidget(field_label("Deliver To *"), 0, 1)
        grid.addWidget(self._warehouse, 1, 1)

        order_number_value = QLabel("Assigned on save")
        order_number_value.setObjectName("readOnlyValue")
        grid.addWidget(field_label("Order Number"), 2, 0)
        grid.addWidget(order_number_value, 3, 0)

        order_date_value = QLabel(datetime.now().strftime("%Y-%m-%d"))
        order_date_value.setObjectName("readOnlyValue")
        grid.addWidget(field_label("Order Date"), 2, 1)
        grid.addWidget(order_date_value, 3, 1)

        expected_row = QHBoxLayout()
        expected_row.setSpacing(8)
        self._expected_date_check = QCheckBox("Set expected date")
        self._expected_date_check.toggled.connect(self._on_expected_date_toggled)
        expected_row.addWidget(self._expected_date_check)
        self._expected_date_edit = QDateEdit(QDate.currentDate())
        self._expected_date_edit.setCalendarPopup(True)
        self._expected_date_edit.setEnabled(False)
        expected_row.addWidget(self._expected_date_edit, stretch=1)
        grid.addWidget(field_label("Expected Date"), 4, 0)
        grid.addLayout(expected_row, 5, 0, 1, 2)

        layout.addLayout(grid)
        return card

    def _on_expected_date_toggled(self, checked: bool) -> None:
        self._expected_date_edit.setEnabled(checked)

    def _on_warehouse_changed(self) -> None:
        if hasattr(self, "_items_editor"):
            self._items_editor.set_warehouse(self._warehouse.currentData())

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
            product_service, inventory_service, include_discount=False,
            price_label="Rate", price_field="purchase_price")
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

        totals_grid.addWidget(self._totals_caption("Non-taxable Total"), 1, 0)
        self._non_taxable_value = self._totals_value()
        totals_grid.addWidget(self._non_taxable_value, 1, 1)

        totals_grid.addWidget(self._totals_caption("Taxable Total"), 2, 0)
        self._taxable_value = self._totals_value()
        totals_grid.addWidget(self._taxable_value, 2, 1)

        totals_grid.addWidget(self._totals_caption("Tax"), 3, 0)
        self._tax_value = self._totals_value()
        totals_grid.addWidget(self._tax_value, 3, 1)

        grand_label = QLabel("Grand Total")
        grand_label.setObjectName("grandTotalLabel")
        totals_grid.addWidget(grand_label, 4, 0)
        self._grand_total_value = QLabel(_money(Decimal("0")))
        self._grand_total_value.setObjectName("grandTotalValue")
        totals_grid.addWidget(self._grand_total_value, 4, 1,
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
        subtotal, _discount, tax_total, grand_total = self._items_editor.compute_totals()
        non_taxable, taxable = self._items_editor.compute_tax_split()
        self._subtotal_value.setText(_money(subtotal))
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
    def _build_footer(self, has_suppliers: bool, has_warehouses: bool) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 6, 0, 0)
        bar.addStretch()

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("orderSecondary")
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        bar.addWidget(cancel_button)

        # A purchase order created here is always a DRAFT (Submit/Approve/
        # Receive Goods happen later, as separate row actions on the
        # Purchases list) — labeled "Save Draft" rather than a generic
        # "Save" so it's honest about what this button actually does, and
        # so it reads the same as New Bill's own "Save Draft" action for
        # the equivalent create-without-finalizing step.
        self._submit_button = QPushButton("Save Draft")
        self._submit_button.setObjectName("orderPrimary")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.setEnabled(has_suppliers and has_warehouses)
        self._submit_button.clicked.connect(self._submit)
        bar.addWidget(self._submit_button)
        return bar

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Saving…" if busy else "Save Draft")

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
        expected_date: date | None = None
        if self._expected_date_check.isChecked():
            expected_date = self._expected_date_edit.date().toPython()
        data = PurchaseOrderCreate(supplier_id=supplier_id, warehouse_id=warehouse_id,
                                   expected_date=expected_date,
                                   notes=self._notes.toPlainText().strip() or None, items=items)

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
