"""The one items-table widget shared by New Bill, the Sales Order form, and
the Purchase Order form — # | Product | SKU | HSN Code | Qty | Rate |
[Discount %] | [Excise %] | Tax % | Amount | Stock | Action (excise only
where discount is, i.e. Sales/New Bill — Purchase Orders carry neither),
with a debounced server-side product search (name/SKU/barcode), live
per-row and grand totals, and a per-organization warehouse-scoped stock
column.

Replaces the two previously-separate implementations (app.ui.widgets.
bill_items_table.BillItemsTable and app.ui.widgets.order_items_editor.
OrderItemsEditor) that had drifted into different UX (search-and-add vs. a
flat pre-loaded combo) and different column sets purely because nothing
forced them to stay in sync — exactly the "three unrelated screens"
problem the New Bill/Sale/Purchase form unification is meant to fix.

Per-line math reuses app.domain.pricing (no discount — what purchase
orders support) / app.domain.sales (with discount — what sales orders and
bills support), the exact same functions the service layer uses
server-side, so this live preview can never silently disagree with what
actually gets saved. No business logic lives here: this widget only
collects and previews raw field values — the real
PurchaseOrderItemInput/SalesOrderItemInput construction and the real
validation (product exists, quantity/price shape, stock availability)
happen in the calling form / the service layer.
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain.pricing import line_subtotal, line_tax, line_total
from app.domain.product import ProductStatus
from app.domain.sales import (
    line_discount,
    line_excise_after_discount,
    line_tax_after_discount,
    line_total_after_discount,
)
from app.schemas.inventory import InventoryLevel
from app.schemas.product import ProductFilter, ProductOut
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.ui.theme import GREEN, MUTED, RED
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

_SEARCH_DEBOUNCE_MS = 300
_SEARCH_PAGE_SIZE = 15

_COL_ROW_NUM = 0
_COL_PRODUCT = 1
_COL_SKU = 2
_COL_HSN = 3
_COL_QTY = 4
_COL_PRICE = 5


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


class TransactionItemsTable(QWidget):
    totals_changed = Signal()

    def __init__(self, product_service: ProductService, inventory_service: InventoryService,
                *, include_discount: bool = True, price_label: str = "Rate",
                price_field: str = "selling_price", parent=None):
        super().__init__(parent)
        self._product_service = product_service
        self._inventory_service = inventory_service
        self._include_discount = include_discount
        self._price_field = price_field
        self._warehouse_id: uuid.UUID | None = None
        self._rows: list[dict] = []  # parallel to table rows: {"product": ProductOut}
        self._search_results: list[ProductOut] = []

        self._col_discount = _COL_PRICE + 1 if include_discount else None
        self._col_excise = self._col_discount + 1 if include_discount else None
        self._col_tax = self._col_excise + 1 if include_discount else _COL_PRICE + 1
        self._col_amount = self._col_tax + 1
        self._col_stock = self._col_amount + 1
        self._col_action = self._col_stock + 1

        columns = ["#", "Item/Product", "SKU", "HSN Code", "Qty", price_label]
        if include_discount:
            columns += ["Discount %", "Excise %"]
        columns += ["Tax %", "Amount", "Stock", "Action"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addLayout(self._build_search_bar())

        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_PRODUCT, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(160)
        layout.addWidget(self._table, stretch=1)

        empty_hint = QLabel("Search for a product above and click “+ Add” to start "
                           "this list.")
        empty_hint.setStyleSheet(f"color: {MUTED}; font-size: 12px; padding: 8px 2px;")
        self._empty_hint = empty_hint
        layout.addWidget(empty_hint)

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._run_search)

    # -- search / add ---------------------------------------------------- #
    def _build_search_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search product by name, SKU, or barcode…")
        self._search.textChanged.connect(self._on_search_text_changed)
        bar.addWidget(self._search, stretch=1)

        self._results = QComboBox()
        self._results.setMinimumWidth(320)
        self._results.setPlaceholderText("No matches yet")
        bar.addWidget(self._results, stretch=1)

        add_button = QPushButton("+ Add Product")
        add_button.setObjectName("orderGhost")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self._add_selected_result)
        bar.addWidget(add_button)
        return bar

    def _on_search_text_changed(self) -> None:
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)

    def _run_search(self) -> None:
        text = self._search.text().strip()
        if not text:
            self._results.clear()
            self._search_results = []
            return
        worker = Worker(self._product_service.search_products,
                        ProductFilter(search=text, status=ProductStatus.ACTIVE,
                                     page_size=_SEARCH_PAGE_SIZE))
        worker.signals.finished.connect(self._on_search_results)
        worker.signals.error.connect(self._on_search_error)
        QThreadPool.globalInstance().start(worker)

    def _on_search_results(self, page) -> None:
        self._search_results = page.items
        self._results.clear()
        for product in page.items:
            price = getattr(product, self._price_field)
            label = f"{product.sku} — {product.name} ({_money(price)})"
            self._results.addItem(label, product.id)
        if not page.items:
            self._results.addItem("No matching products", None)

    def _on_search_error(self, exc: Exception) -> None:
        _logger.exception("Product search failed", exc_info=exc)
        self._results.clear()
        self._results.addItem("Search failed — try again", None)

    def _add_selected_result(self) -> None:
        product_id = self._results.currentData()
        if product_id is None:
            return
        product = next((p for p in self._search_results if p.id == product_id), None)
        if product is None:
            return
        self.add_row(product)
        self._search.clear()
        self._results.clear()
        self._search_results = []

    # -- warehouse (for the live stock column) ---------------------------- #
    def set_warehouse(self, warehouse_id: uuid.UUID | None) -> None:
        if warehouse_id == self._warehouse_id:
            return
        self._warehouse_id = warehouse_id
        for row in range(self._table.rowCount()):
            self._refresh_stock(row)

    # -- rows -------------------------------------------------------------- #
    def add_row(self, product: ProductOut) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._rows.insert(row, {"product": product})
        self._empty_hint.setVisible(False)

        num_item = QTableWidgetItem(str(row + 1))
        num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, _COL_ROW_NUM, num_item)
        self._table.setItem(row, _COL_PRODUCT, QTableWidgetItem(product.name))
        self._table.setItem(row, _COL_SKU, QTableWidgetItem(product.sku))
        self._table.setItem(row, _COL_HSN, QTableWidgetItem(product.hsn_code or "—"))

        qty_edit = QLineEdit("1")
        qty_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_QTY, qty_edit)

        price_edit = QLineEdit(str(getattr(product, self._price_field)))
        price_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_PRICE, price_edit)

        if self._col_discount is not None:
            discount_edit = QLineEdit("0")
            discount_edit.textChanged.connect(lambda: self._on_row_changed(row))
            self._table.setCellWidget(row, self._col_discount, discount_edit)

        if self._col_excise is not None:
            excise_edit = QLineEdit(str(product.excise_percent))
            excise_edit.textChanged.connect(lambda: self._on_row_changed(row))
            self._table.setCellWidget(row, self._col_excise, excise_edit)

        tax_edit = QLineEdit(str(product.tax_percent))
        tax_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, self._col_tax, tax_edit)

        amount_item = QTableWidgetItem("")
        amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row, self._col_amount, amount_item)

        stock_item = QTableWidgetItem("—")
        stock_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, self._col_stock, stock_item)

        remove_button = QPushButton("Remove")
        remove_button.setObjectName("flat")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(lambda: self._remove_row_widget(remove_button))
        self._table.setCellWidget(row, self._col_action, remove_button)

        self._recompute_row_amount(row)
        self._refresh_stock(row)
        self.totals_changed.emit()

    def _row_of_widget(self, widget: QWidget) -> int:
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, self._col_action) is widget:
                return row
        return -1

    def _remove_row_widget(self, button: QPushButton) -> None:
        row = self._row_of_widget(button)
        if row < 0:
            return
        self._table.removeRow(row)
        del self._rows[row]
        self._renumber_rows()
        self.totals_changed.emit()
        self._empty_hint.setVisible(self._table.rowCount() == 0)

    def _renumber_rows(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_ROW_NUM)
            if item is not None:
                item.setText(str(row + 1))

    def _on_row_changed(self, row: int) -> None:
        if row >= self._table.rowCount():
            return
        self._recompute_row_amount(row)
        self.totals_changed.emit()

    def _row_decimal(self, row: int, col: int) -> Decimal | None:
        widget = self._table.cellWidget(row, col)
        if widget is None:
            return None
        try:
            return Decimal(widget.text().strip() or "0")
        except InvalidOperation:
            return None

    def _row_values(self, row: int) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
        quantity = self._row_decimal(row, _COL_QTY)
        price = self._row_decimal(row, _COL_PRICE)
        tax = self._row_decimal(row, self._col_tax)
        discount = (self._row_decimal(row, self._col_discount)
                   if self._col_discount is not None else Decimal("0"))
        excise = (self._row_decimal(row, self._col_excise)
                 if self._col_excise is not None else Decimal("0"))
        if None in (quantity, price, discount, excise, tax):
            return None
        return quantity, price, discount, excise, tax

    def _recompute_row_amount(self, row: int) -> None:
        amount_item = self._table.item(row, self._col_amount)
        if amount_item is None:
            return
        values = self._row_values(row)
        if values is None:
            amount_item.setText("—")
            return
        quantity, price, discount, excise, tax = values
        total = (line_total_after_discount(quantity, price, discount, tax,
                                           excise_percent=excise)
                 if self._include_discount else line_total(quantity, price, tax))
        amount_item.setText(_money(total))

    def _refresh_stock(self, row: int) -> None:
        if row >= len(self._rows):
            return
        stock_item = self._table.item(row, self._col_stock)
        if stock_item is None:
            return
        if self._warehouse_id is None:
            stock_item.setText("—")
            stock_item.setForeground(QColor(MUTED))
            return
        product = self._rows[row]["product"]
        worker = Worker(self._inventory_service.get_inventory_level, product.id,
                        self._warehouse_id)
        worker.signals.finished.connect(lambda level: self._on_stock_loaded(row, level))
        worker.signals.error.connect(
            lambda exc: _logger.exception("Failed to load stock level", exc_info=exc))
        QThreadPool.globalInstance().start(worker)

    def _on_stock_loaded(self, row: int, level: InventoryLevel) -> None:
        if row >= self._table.rowCount():
            return  # row was removed while the lookup was in flight
        stock_item = self._table.item(row, self._col_stock)
        if stock_item is None:
            return
        stock_item.setText(f"{level.quantity_available:g}")
        stock_item.setForeground(QColor(RED if level.quantity_available <= 0 else GREEN))

    # -- collection / totals ------------------------------------------------#
    def is_empty(self) -> bool:
        return self._table.rowCount() == 0

    def collect_items(self) -> tuple[list[dict], list[str]]:
        """(items, errors). Each item dict has keys product_id/quantity/
        unit_price/tax_percent, and (if include_discount) discount_percent/
        excise_percent — ready to spread into the schema the caller needs.
        """
        items: list[dict] = []
        errors: list[str] = []
        for row in range(self._table.rowCount()):
            product = self._rows[row]["product"]
            quantity = self._row_decimal(row, _COL_QTY)
            price = self._row_decimal(row, _COL_PRICE)
            discount = (self._row_decimal(row, self._col_discount)
                       if self._col_discount is not None else Decimal("0"))
            excise = (self._row_decimal(row, self._col_excise)
                     if self._col_excise is not None else Decimal("0"))
            tax = self._row_decimal(row, self._col_tax)
            if quantity is None:
                errors.append(f"Row {row + 1} ({product.name}): quantity must be a number.")
                continue
            if price is None:
                errors.append(f"Row {row + 1} ({product.name}): rate must be a number.")
                continue
            if self._col_discount is not None and discount is None:
                errors.append(f"Row {row + 1} ({product.name}): discount must be a number.")
                continue
            if self._col_excise is not None and excise is None:
                errors.append(f"Row {row + 1} ({product.name}): excise duty must be a number.")
                continue
            if tax is None:
                errors.append(f"Row {row + 1} ({product.name}): tax must be a number.")
                continue
            item = {"product_id": product.id, "quantity": quantity, "unit_price": price,
                   "tax_percent": tax}
            if self._col_discount is not None:
                item["discount_percent"] = discount
            if self._col_excise is not None:
                item["excise_percent"] = excise
            items.append(item)
        if not items and not errors:
            errors.append("Add at least one product.")
        return items, errors

    def compute_totals(self) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        """(subtotal, item_discount_total, tax_total, excise_total,
        grand_total) computed from whatever rows currently parse cleanly —
        rows with an invalid number are skipped for this *preview* total
        (collect_items() is what actually blocks Save with a real error for
        those). excise_total is always 0 when include_discount is False
        (Purchase Orders don't carry excise duty).
        """
        subtotal = discount_total = tax_total = excise_total = grand_total = Decimal("0")
        for row in range(self._table.rowCount()):
            values = self._row_values(row)
            if values is None:
                continue
            quantity, price, discount, excise, tax = values
            subtotal += line_subtotal(quantity, price)
            if self._include_discount:
                discount_total += line_discount(quantity, price, discount)
                tax_total += line_tax_after_discount(quantity, price, discount, tax)
                excise_total += line_excise_after_discount(quantity, price, discount, excise)
                grand_total += line_total_after_discount(quantity, price, discount, tax,
                                                         excise_percent=excise)
            else:
                tax_total += line_tax(quantity, price, tax)
                grand_total += line_total(quantity, price, tax)
        return subtotal, discount_total, tax_total, excise_total, grand_total

    def compute_tax_split(self) -> tuple[Decimal, Decimal]:
        """(non_taxable_total, taxable_total) — the post-discount line
        subtotal split by whether that line carries any tax %, purely a
        UI-level aggregation of already-collected per-line data (no new
        field is persisted for this split).
        """
        non_taxable = taxable = Decimal("0")
        for row in range(self._table.rowCount()):
            values = self._row_values(row)
            if values is None:
                continue
            quantity, price, discount, _excise, tax = values
            line_amount = (line_subtotal(quantity, price) - line_discount(quantity, price, discount)
                          if self._include_discount else line_subtotal(quantity, price))
            if tax > 0:
                taxable += line_amount
            else:
                non_taxable += line_amount
        return non_taxable, taxable

    def clear_items(self) -> None:
        self._table.setRowCount(0)
        self._rows.clear()
        self._empty_hint.setVisible(True)
        self.totals_changed.emit()
