"""Editable line-item table shared by PurchaseOrderFormDialog and
SalesOrderFormDialog — a product picker, quantity, unit price, and tax %
per row (plus a discount % column for sales), with add/remove-row
controls. Only collects and validates raw field values; the actual
PurchaseOrderItemInput/SalesOrderItemInput construction and the real
validation (product exists, quantity/price shape) happen in the calling
dialog / the service layer, same division of responsibility as every
other form widget in app.ui.widgets.
"""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.schemas.product import ProductOut

_QTY_COL = 1
_PRICE_COL = 2
_TAX_COL = 3
_DISCOUNT_COL = 4


class OrderItemsEditor(QWidget):
    def __init__(self, products: list[ProductOut], *, include_discount: bool = False,
                price_label: str = "Unit Price", parent=None):
        super().__init__(parent)
        self._products = sorted(products, key=lambda p: p.name.lower())
        self._include_discount = include_discount

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        columns = ["Product", "Quantity", price_label, "Tax %"]
        if include_discount:
            columns.append("Discount %")
        self._table = QTableWidget(0, len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setMinimumHeight(140)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        add_button = QPushButton("+ Add Line")
        add_button.setObjectName("ghost")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self.add_row)
        buttons.addWidget(add_button)

        remove_button = QPushButton("Remove Selected Line")
        remove_button.setObjectName("ghost")
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
        self._table.setCellWidget(row, 0, combo)

        self._table.setCellWidget(row, _QTY_COL, QLineEdit("1"))
        self._table.setCellWidget(row, _PRICE_COL, QLineEdit("0"))
        self._table.setCellWidget(row, _TAX_COL, QLineEdit("0"))
        if self._include_discount:
            self._table.setCellWidget(row, _DISCOUNT_COL, QLineEdit("0"))

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self._table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._table.removeRow(row)

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
            quantity = self._parse_row_decimal(row, _QTY_COL, "Quantity", errors)
            unit_price = self._parse_row_decimal(row, _PRICE_COL, "Price", errors)
            tax_percent = self._parse_row_decimal(row, _TAX_COL, "Tax %", errors)
            item = {"product_id": product_id, "quantity": quantity,
                   "unit_price": unit_price, "tax_percent": tax_percent}
            if self._include_discount:
                item["discount_percent"] = self._parse_row_decimal(
                    row, _DISCOUNT_COL, "Discount %", errors)
            items.append(item)
        if not items and not errors:
            errors.append("Add at least one line item.")
        return items, errors
