"""The one items-table widget shared by New Bill, the Sales Order form, and
the Purchase Order form — # | Product | SKU | HSN Code | Qty | Rate |
[Discount %] | [Excise %] | Tax % | Amount | Stock | Action (excise only
where discount is, i.e. Sales/New Bill — Purchase Orders carry neither),
with a debounced server-side product search (name/SKU/barcode/HSN code)
shown in a rich ProductSuggestPopup (Name/SKU/HSN/Stock/Price per row),
live per-row and grand totals, and a per-organization warehouse-scoped
stock column.

When a search finds nothing, or the caller passes allow_product_creation=
True, the popup's pinned "+ Add Product" row lets the host open its own
product-creation dialog (this widget never does — see add_product_
requested below) and hand the result straight back via add_row(), so
creating a product never interrupts whatever else the user has already
entered in the surrounding form.

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

from PySide6.QtCore import QEvent, QObject, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
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
from app.ui.widgets.product_suggest_popup import ProductSuggestPopup
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
    # Emitted when the user activates the popup's "+ Add Product" row —
    # only ever fires when allow_product_creation=True (see below). Carries
    # whatever text is currently in the search box, so the host's Add
    # Product dialog can prefill Name with it. This widget never builds
    # that dialog itself — it has no catalog/session access, and the host
    # page already owns the permission check and catalog data (see
    # NewBillPage._on_add_product_requested).
    add_product_requested = Signal(str)

    def __init__(self, product_service: ProductService, inventory_service: InventoryService,
                *, include_discount: bool = True, price_label: str = "Rate",
                price_field: str = "selling_price", allow_product_creation: bool = False,
                parent=None):
        super().__init__(parent)
        self._product_service = product_service
        self._inventory_service = inventory_service
        self._include_discount = include_discount
        self._price_field = price_field
        self._allow_product_creation = allow_product_creation
        self._warehouse_id: uuid.UUID | None = None
        # Parallel to table rows: {"product": ProductOut, "available":
        # Decimal | None}. "available" is filled in once _on_stock_loaded
        # returns and is what the soft over-stock warning compares the
        # typed quantity against — see _apply_stock_warning.
        self._rows: list[dict] = []
        self._search_results: list[ProductOut] = []
        self._search_generation = 0
        self._read_only = False

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

    # -- search / suggestions ---------------------------------------------#
    def _build_search_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search product by name, SKU, barcode, or HSN code…")
        self._search.textChanged.connect(self._on_search_text_changed)
        # Up/Down/Enter/Escape are handled here rather than by the popup
        # itself, so the popup never needs keyboard focus and typing never
        # jumps out of this box mid-word — see eventFilter below and
        # ProductSuggestPopup's module docstring.
        self._search.installEventFilter(self)
        bar.addWidget(self._search, stretch=1)

        self._popup = ProductSuggestPopup(self)
        self._popup.product_selected.connect(self._on_suggestion_selected)
        self._popup.add_product_requested.connect(self._on_popup_add_product_requested)
        return bar

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is not self._search or event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        if not self._popup.is_open():
            return super().eventFilter(watched, event)
        key = event.key()
        if key == Qt.Key.Key_Down:
            self._popup.move_selection(1)
            return True
        if key == Qt.Key.Key_Up:
            self._popup.move_selection(-1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._popup.activate_selection()
            return True
        if key == Qt.Key.Key_Escape:
            self._popup.hide_popup()
            return True
        return super().eventFilter(watched, event)

    def _on_search_text_changed(self) -> None:
        text = self._search.text().strip()
        if not text:
            self._search_debounce.stop()
            self._search_generation += 1  # invalidate any in-flight search
            self._search_results = []
            self._popup.hide_popup()
            return
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)
        self._popup.show_loading()
        self._popup.reposition_below(self._search)
        self._popup.show_popup()

    def _run_search(self) -> None:
        text = self._search.text().strip()
        if not text:
            return  # cleared during the debounce window
        # Bumped per search so a slow, superseded result can't clobber a
        # newer one that already returned — the same generation-guard
        # shape AsyncContentArea uses for its own reload() races.
        self._search_generation += 1
        generation = self._search_generation
        worker = Worker(self._search_and_fetch_stock, text)
        worker.signals.finished.connect(
            lambda result: self._on_search_results(result, generation))
        worker.signals.error.connect(lambda exc: self._on_search_error(exc, generation))
        QThreadPool.globalInstance().start(worker)

    def _search_and_fetch_stock(self, text: str):
        """Runs on the worker thread: the product search plus, only when a
        warehouse is already selected, one bulk stock lookup for exactly
        the ids in that page — never per-row, never the whole catalog.
        """
        page = self._product_service.search_products(
            ProductFilter(search=text, status=ProductStatus.ACTIVE,
                         page_size=_SEARCH_PAGE_SIZE))
        stock_by_product = {}
        if self._warehouse_id is not None and page.items:
            stock_by_product = self._inventory_service.get_levels_for_products(
                [p.id for p in page.items], self._warehouse_id)
        return page, stock_by_product

    def _on_search_results(self, result, generation: int) -> None:
        if generation != self._search_generation:
            return  # superseded by a newer search — this result is stale
        page, stock_by_product = result
        self._search_results = page.items
        self._popup.show_products(page.items, self._price_field, stock_by_product,
                                  show_add_product=self._allow_product_creation)
        self._popup.reposition_below(self._search)
        self._popup.show_popup()

    def _on_search_error(self, exc: Exception, generation: int) -> None:
        if generation != self._search_generation:
            return
        _logger.exception("Product search failed", exc_info=exc)
        self._search_results = []
        self._popup.show_error("Couldn't search products — try again.",
                               show_add_product=self._allow_product_creation)
        self._popup.reposition_below(self._search)
        self._popup.show_popup()

    def _on_suggestion_selected(self, product: ProductOut) -> None:
        self.add_row(product)
        self._search.clear()
        self._search_results = []
        self._popup.hide_popup()

    def _on_popup_add_product_requested(self) -> None:
        self._popup.hide_popup()
        self.add_product_requested.emit(self._search.text().strip())

    # -- warehouse (for the live stock column) ---------------------------- #
    def set_warehouse(self, warehouse_id: uuid.UUID | None) -> None:
        if warehouse_id == self._warehouse_id:
            return
        self._warehouse_id = warehouse_id
        for row in range(self._table.rowCount()):
            self._refresh_stock(row)

    # -- rows -------------------------------------------------------------- #
    def add_row(self, product: ProductOut, *, quantity: Decimal | None = None,
               unit_price: Decimal | None = None, tax_percent: Decimal | None = None,
               discount_percent: Decimal | None = None,
               excise_percent: Decimal | None = None) -> None:
        """Adds one row. With no overrides this is "the user just picked a
        product" — quantity 1, and price/tax/excise defaulted from the
        product's own current catalog values (the existing behavior).

        The overrides exist so set_items() below can seed a row from an
        *already-saved* order line without duplicating this method: a
        saved line's price/tax/discount/excise reflect what was actually
        agreed at the time, not whatever the product catalog says today,
        so those must win when supplied.
        """
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._rows.insert(row, {"product": product, "available": None})
        self._empty_hint.setVisible(False)

        num_item = QTableWidgetItem(str(row + 1))
        num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, _COL_ROW_NUM, num_item)
        self._table.setItem(row, _COL_PRODUCT, QTableWidgetItem(product.name))
        self._table.setItem(row, _COL_SKU, QTableWidgetItem(product.sku))
        self._table.setItem(row, _COL_HSN, QTableWidgetItem(product.hsn_code or "—"))

        qty_edit = QLineEdit(str(quantity if quantity is not None else Decimal("1")))
        qty_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_QTY, qty_edit)

        price = unit_price if unit_price is not None else getattr(product, self._price_field)
        price_edit = QLineEdit(str(price))
        price_edit.textChanged.connect(lambda: self._on_row_changed(row))
        self._table.setCellWidget(row, _COL_PRICE, price_edit)

        if self._col_discount is not None:
            discount = discount_percent if discount_percent is not None else Decimal("0")
            discount_edit = QLineEdit(str(discount))
            discount_edit.textChanged.connect(lambda: self._on_row_changed(row))
            self._table.setCellWidget(row, self._col_discount, discount_edit)

        if self._col_excise is not None:
            excise = excise_percent if excise_percent is not None else product.excise_percent
            excise_edit = QLineEdit(str(excise))
            excise_edit.textChanged.connect(lambda: self._on_row_changed(row))
            self._table.setCellWidget(row, self._col_excise, excise_edit)

        tax = tax_percent if tax_percent is not None else product.tax_percent
        tax_edit = QLineEdit(str(tax))
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
        if self._read_only:
            self._apply_read_only_to_row(row)

    def set_items(self, lines: list[tuple[ProductOut, dict]]) -> None:
        """Replaces the table's contents with ``lines`` — (product, field
        overrides) pairs, the overrides being whichever of quantity/
        unit_price/tax_percent/discount_percent/excise_percent apply.
        Used to seed the form when opening an existing order for View or
        Edit; a brand-new order is built entirely through add_row(), this
        method is never in that path.
        """
        self.clear_items()
        for product, overrides in lines:
            self.add_row(product, **overrides)

    # -- read-only (View mode) --------------------------------------------#
    def set_read_only(self, read_only: bool) -> None:
        self._read_only = read_only
        self._search.setEnabled(not read_only)
        if read_only:
            self._popup.hide_popup()
        for row in range(self._table.rowCount()):
            self._apply_read_only_to_row(row)

    def _apply_read_only_to_row(self, row: int) -> None:
        for col in (_COL_QTY, _COL_PRICE, self._col_discount, self._col_excise,
                   self._col_tax):
            if col is None:
                continue
            widget = self._table.cellWidget(row, col)
            if widget is not None:
                widget.setReadOnly(self._read_only)
        remove_button = self._table.cellWidget(row, self._col_action)
        if remove_button is not None:
            remove_button.setVisible(not self._read_only)

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
        self._apply_stock_warning(row)
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
            stock_item.setToolTip("")
            self._rows[row]["available"] = None
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
        self._rows[row]["available"] = level.quantity_available
        self._apply_stock_warning(row)

    def _apply_stock_warning(self, row: int) -> None:
        """Soft, non-blocking heads-up when the typed quantity exceeds
        what's on hand — never refuses to save. Whether that's actually
        allowed is an org-level policy (Organization.allow_negative_stock)
        enforced server-side at fulfillment time, same as before this
        feature; this is only a visual hint so the user notices before
        submitting, not a second copy of that rule.
        """
        if row >= len(self._rows):
            return
        stock_item = self._table.item(row, self._col_stock)
        if stock_item is None:
            return
        available = self._rows[row].get("available")
        if available is None:
            return  # no warehouse selected yet, or stock hasn't loaded
        quantity = self._row_decimal(row, _COL_QTY)
        over_stock = quantity is not None and quantity > available
        stock_item.setForeground(QColor(RED if (over_stock or available <= 0) else GREEN))
        stock_item.setToolTip(
            f"Only {available:g} available — quantity exceeds stock." if over_stock else "")

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
