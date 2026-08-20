"""Editable line-item table shared by PurchaseOrderFormDialog and
SalesOrderFormDialog — a product picker, quantity, unit price, tax %
(plus a discount % column for sales) per row, with a live-computed Line
Total column, add/remove-row controls, and a totals_changed signal so the
owning dialog can show a live Subtotal/Tax/Grand Total preview. Only
collects and validates raw field values; the actual PurchaseOrderItemInput/
SalesOrderItemInput construction and the real validation (product exists,
quantity/price shape) happen in the calling dialog / the service layer,
same division of responsibility as every other form widget in
app.ui.widgets.

Per-line math reuses app.domain.pricing (no discount — what purchase
orders support) / app.domain.sales (with discount — what sales orders
support), the exact same functions the service layer uses server-side, so
this live preview can never silently disagree with what actually gets
saved.
"""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain.pricing import line_subtotal, line_tax, line_total
from app.domain.sales import (
    line_discount,
    line_tax_after_discount,
    line_total_after_discount,
)
from app.schemas.product import ProductOut


class OrderItemsEditor(QWidget):
    totals_changed = Signal()

    def __init__(self, products: list[ProductOut], *, include_discount: bool = False,
                price_label: str = "Unit Price", price_field: str = "selling_price",
                parent=None):
        super().__init__(parent)
        self._products = sorted(products, key=lambda p: p.name.lower())
        self._include_discount = include_discount
        self._price_field = price_field

        self._qty_col = 1
        self._price_col = 2
        if include_discount:
            self._discount_col = 3
            self._tax_col = 4
        else:
            self._discount_col = None
            self._tax_col = 3
        self._total_col = self._tax_col + 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        columns = ["Product", "Quantity", price_label]
        if include_discount:
            columns.append("Discount %")
        columns.append("Tax %")
        columns.append("Line Total")

        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setMinimumHeight(160)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        add_button = QPushButton("+ Add Item")
        add_button.setObjectName("orderGhost")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self.add_row)
        buttons.addWidget(add_button)

        remove_button = QPushButton("Remove Selected")
        remove_button.setObjectName("orderGhost")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.add_row()

    def add_row(self) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        combo = QComboBox()
        for p in self._products:
            combo.addItem(f"{p.sku} — {p.name}", p.id)
        combo.currentIndexChanged.connect(lambda _index, r=row: self._on_row_product_changed(r))
        self._table.setCellWidget(row, 0, combo)

        qty_edit = QLineEdit("1")
        qty_edit.textChanged.connect(lambda _text, r=row: self._on_row_changed(r))
        self._table.setCellWidget(row, self._qty_col, qty_edit)

        price_edit = QLineEdit("0")
        price_edit.textChanged.connect(lambda _text, r=row: self._on_row_changed(r))
        self._table.setCellWidget(row, self._price_col, price_edit)

        if self._discount_col is not None:
            discount_edit = QLineEdit("0")
            discount_edit.textChanged.connect(lambda _text, r=row: self._on_row_changed(r))
            self._table.setCellWidget(row, self._discount_col, discount_edit)

        tax_edit = QLineEdit("0")
        tax_edit.textChanged.connect(lambda _text, r=row: self._on_row_changed(r))
        self._table.setCellWidget(row, self._tax_col, tax_edit)

        total_item = QTableWidgetItem("0.00")
        total_item.setFlags(total_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        total_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._table.setItem(row, self._total_col, total_item)

        if self._products:
            self._apply_product_defaults(row, self._products[0])
        self._recompute_row_total(row)
        self.totals_changed.emit()

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self._table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._table.removeRow(row)
        if rows:
            self.totals_changed.emit()

    # -- product defaults --------------------------------------------------#
    def _on_row_product_changed(self, row: int) -> None:
        if row >= self._table.rowCount():
            return
        combo = self._table.cellWidget(row, 0)
        product_id = combo.currentData() if combo else None
        product = next((p for p in self._products if p.id == product_id), None)
        if product is not None:
            self._apply_product_defaults(row, product)
        self._recompute_row_total(row)
        self.totals_changed.emit()

    def _apply_product_defaults(self, row: int, product: ProductOut) -> None:
        """Pre-fills unit price/tax % from the product's own defaults when
        a row's product changes — otherwise every new row silently starts
        at a 0 price until the user remembers to fill it in by hand.
        """
        price_edit = self._table.cellWidget(row, self._price_col)
        if price_edit is not None:
            price_edit.blockSignals(True)
            price_edit.setText(str(getattr(product, self._price_field)))
            price_edit.blockSignals(False)
        tax_edit = self._table.cellWidget(row, self._tax_col)
        if tax_edit is not None:
            tax_edit.blockSignals(True)
            tax_edit.setText(str(product.tax_percent))
            tax_edit.blockSignals(False)

    # -- live totals ---------------------------------------------------------#
    def _on_row_changed(self, row: int) -> None:
        if row >= self._table.rowCount():
            return
        self._recompute_row_total(row)
        self.totals_changed.emit()

    def _row_decimal_or_none(self, row: int, col: int) -> Decimal | None:
        widget = self._table.cellWidget(row, col)
        if widget is None:
            return None
        try:
            return Decimal(widget.text().strip() or "0")
        except InvalidOperation:
            return None

    def _row_values(self, row: int) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        quantity = self._row_decimal_or_none(row, self._qty_col)
        price = self._row_decimal_or_none(row, self._price_col)
        tax = self._row_decimal_or_none(row, self._tax_col)
        discount = (self._row_decimal_or_none(row, self._discount_col)
                   if self._discount_col is not None else Decimal("0"))
        if None in (quantity, price, tax, discount):
            return None
        return quantity, price, discount, tax

    def _recompute_row_total(self, row: int) -> None:
        total_item = self._table.item(row, self._total_col)
        if total_item is None:
            return
        values = self._row_values(row)
        if values is None:
            total_item.setText("—")
            return
        quantity, price, discount, tax = values
        total = (line_total_after_discount(quantity, price, discount, tax)
                 if self._include_discount else line_total(quantity, price, tax))
        total_item.setText(f"{total:,.2f}")

    def compute_totals(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(subtotal, discount_total, tax_total, grand_total) computed from
        whatever rows currently parse cleanly — rows with an invalid number
        are skipped for this *preview* total (collect_items() is what
        actually blocks Save with a real error for those).
        """
        subtotal = discount_total = tax_total = grand_total = Decimal("0")
        for row in range(self._table.rowCount()):
            values = self._row_values(row)
            if values is None:
                continue
            quantity, price, discount, tax = values
            subtotal += line_subtotal(quantity, price)
            if self._include_discount:
                discount_total += line_discount(quantity, price, discount)
                tax_total += line_tax_after_discount(quantity, price, discount, tax)
                grand_total += line_total_after_discount(quantity, price, discount, tax)
            else:
                tax_total += line_tax(quantity, price, tax)
                grand_total += line_total(quantity, price, tax)
        return subtotal, discount_total, tax_total, grand_total

    # -- collection ----------------------------------------------------------#
    def _parse_row_decimal(self, row: int, col: int, label: str, errors: list[str]) -> Decimal:
        widget = self._table.cellWidget(row, col)
        try:
            return Decimal(widget.text().strip() or "0")
        except InvalidOperation:
            errors.append(f"Row {row + 1}: {label} must be a number.")
            return Decimal("0")

    def collect_items(self) -> tuple[list[dict], list[str]]:
        """Returns (items, errors). Each item dict has keys product_id,
        quantity, unit_price, tax_percent, and (if include_discount)
        discount_percent — ready to spread into the schema the caller
        needs.
        """
        items: list[dict] = []
        errors: list[str] = []
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, 0)
            product_id = combo.currentData() if combo else None
            if product_id is None:
                errors.append(f"Row {row + 1}: select a product.")
                continue
            quantity = self._parse_row_decimal(row, self._qty_col, "Quantity", errors)
            unit_price = self._parse_row_decimal(row, self._price_col, "Price", errors)
            tax_percent = self._parse_row_decimal(row, self._tax_col, "Tax %", errors)
            item = {"product_id": product_id, "quantity": quantity,
                   "unit_price": unit_price, "tax_percent": tax_percent}
            if self._discount_col is not None:
                item["discount_percent"] = self._parse_row_decimal(
                    row, self._discount_col, "Discount %", errors)
            items.append(item)
        if not items and not errors:
            errors.append("Add at least one line item.")
        return items, errors
