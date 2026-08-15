"""The "New Bill" line-item table — # | Product | SKU | Qty | Unit Price |
Discount | Tax | Amount | Action, with a debounced server-side product
search (name/SKU/barcode) above it rather than a flat pre-loaded combo.

This is a distinct widget from app.ui.widgets.order_items_editor.
OrderItemsEditor (shared by PurchaseOrderFormDialog/SalesOrderFormDialog)
deliberately isn't touched or reused here: it loads every product into a
combo up front (fine for those dialogs' scale) and has no search, no
stock display, and no live per-row/grand total — all of which "New Bill"
explicitly needs. Building a second, purpose-built widget avoids changing
OrderItemsEditor's behavior for the two dialogs that already depend on it.

Per-line math (line subtotal/discount/tax/total) reuses
app.domain.sales.line_* — the exact same functions
SalesOrderRepository.generate_invoice uses server-side — so what this
table shows a cashier while they're building a bill can never silently
disagree with what actually gets billed once saved.
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
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

from app.domain.product import ProductStatus
from app.domain.sales import line_discount, line_tax_after_discount, line_total_after_discount
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
_COL_QTY = 3
_COL_PRICE = 4
_COL_DISCOUNT = 5
_COL_TAX = 6
_COL_AMOUNT = 7
_COL_STOCK = 8
_COL_ACTION = 9
_COLUMN_LABELS = ["#", "Product", "SKU", "Qty", "Unit Price", "Discount %", "Tax %",
                  "Amount", "Stock", ""]


class BillItemsTable(QWidget):
    totals_changed = Signal()

    def __init__(self, product_service: ProductService, inventory_service: InventoryService,
                parent=None):
        super().__init__(parent)
        self._product_service = product_service
        self._inventory_service = inventory_service
        self._warehouse_id: uuid.UUID | None = None
        self._rows: list[dict] = []  # parallel to table rows: {"product": ProductOut}
        self._search_results: list[ProductOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addLayout(self._build_search_bar())

        self._table = QTableWidget(0, len(_COLUMN_LABELS))
        self._table.setHorizontalHeaderLabels(_COLUMN_LABELS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_PRODUCT, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(160)
        layout.addWidget(self._table, stretch=1)

        empty_hint = QLabel("Search for a product above and click “+ Add” to start "
                           "this bill.")
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

        add_button = QPushButton("+ Add")
        add_button.setObjectName("ghost")
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
            label = f"{product.sku} — {product.name} ({_money(product.selling_price)})"
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

        qty_edit = QLineEdit("1")
        qty_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_QTY, qty_edit)

        price_edit = QLineEdit(str(product.selling_price))
        price_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_PRICE, price_edit)

        discount_edit = QLineEdit("0")
        discount_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_DISCOUNT, discount_edit)

        tax_edit = QLineEdit(str(product.tax_percent))
        tax_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_TAX, tax_edit)

        amount_item = QTableWidgetItem("")
        amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row, _COL_AMOUNT, amount_item)

        stock_item = QTableWidgetItem("—")
        stock_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, _COL_STOCK, stock_item)

        remove_button = QPushButton("Remove")
        remove_button.setObjectName("flat")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(lambda: self._remove_row_widget(remove_button))
        self._table.setCellWidget(row, _COL_ACTION, remove_button)

        self._recompute_row_amount(row)
        self._refresh_stock(row)
        self.totals_changed.emit()

    def _row_of_widget(self, widget: QWidget) -> int:
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, _COL_ACTION) is widget:
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

    def _recompute_row_amount(self, row: int) -> None:
        quantity = self._row_decimal(row, _COL_QTY)
        price = self._row_decimal(row, _COL_PRICE)
        discount = self._row_decimal(row, _COL_DISCOUNT)
        tax = self._row_decimal(row, _COL_TAX)
        amount_item = self._table.item(row, _COL_AMOUNT)
        if amount_item is None:
            return
        if None in (quantity, price, discount, tax):
            amount_item.setText("—")
            return
        total = line_total_after_discount(quantity, price, discount, tax)
        amount_item.setText(_money(total))

    def _refresh_stock(self, row: int) -> None:
        if row >= len(self._rows):
            return
        stock_item = self._table.item(row, _COL_STOCK)
        if stock_item is None:
            return
        if self._warehouse_id is None:
            stock_item.setText("—")
            stock_item.setForeground(Qt.GlobalColor.black)
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
        stock_item = self._table.item(row, _COL_STOCK)
        if stock_item is None:
            return
        stock_item.setText(f"{level.quantity_available:g}")
        from PySide6.QtGui import QColor
        stock_item.setForeground(QColor(RED if level.quantity_available <= 0 else GREEN))

    # -- collection / totals ------------------------------------------------#
    def is_empty(self) -> bool:
        return self._table.rowCount() == 0

    def collect_items(self) -> tuple[list[dict], list[str]]:
        """Same shape as OrderItemsEditor.collect_items — (items, errors),
        each item a dict of product_id/quantity/unit_price/tax_percent/
        discount_percent ready to build a SalesOrderItemInput from.
        """
        items: list[dict] = []
        errors: list[str] = []
        for row in range(self._table.rowCount()):
            product = self._rows[row]["product"]
            quantity = self._row_decimal(row, _COL_QTY)
            price = self._row_decimal(row, _COL_PRICE)
            discount = self._row_decimal(row, _COL_DISCOUNT)
            tax = self._row_decimal(row, _COL_TAX)
            if quantity is None:
                errors.append(f"Row {row + 1} ({product.name}): quantity must be a number.")
                continue
            if price is None:
                errors.append(f"Row {row + 1} ({product.name}): unit price must be a number.")
                continue
            if discount is None:
                errors.append(f"Row {row + 1} ({product.name}): discount must be a number.")
                continue
            if tax is None:
                errors.append(f"Row {row + 1} ({product.name}): tax must be a number.")
                continue
            items.append({"product_id": product.id, "quantity": quantity, "unit_price": price,
                         "discount_percent": discount, "tax_percent": tax})
        if not items and not errors:
            errors.append("Add at least one product to the bill.")
        return items, errors

    def compute_totals(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(subtotal, item_discount_total, tax_total, grand_total) computed
        from whatever rows currently parse cleanly — rows with an invalid
        number are skipped for this *preview* total (collect_items() is
        what actually blocks Save with a real error for those).
        """
        subtotal = Decimal("0")
        discount_total = Decimal("0")
        tax_total = Decimal("0")
        grand_total = Decimal("0")
        for row in range(self._table.rowCount()):
            quantity = self._row_decimal(row, _COL_QTY)
            price = self._row_decimal(row, _COL_PRICE)
            discount = self._row_decimal(row, _COL_DISCOUNT)
            tax = self._row_decimal(row, _COL_TAX)
            if None in (quantity, price, discount, tax):
                continue
            subtotal += quantity * price
            discount_total += line_discount(quantity, price, discount)
            tax_total += line_tax_after_discount(quantity, price, discount, tax)
            grand_total += line_total_after_discount(quantity, price, discount, tax)
        return subtotal, discount_total, tax_total, grand_total

    def clear_items(self) -> None:
        self._table.setRowCount(0)
        self._rows.clear()
        self._empty_hint.setVisible(True)
        self.totals_changed.emit()


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"
