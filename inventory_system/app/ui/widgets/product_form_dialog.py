"""Add/Edit/View product dialog.

No business logic here — this widget only collects raw field values,
hands them to ProductService, and displays whatever it returns or raises
(ProductValidationError/DuplicateSkuError/DuplicateBarcodeError). SKU
normalization, price/tax validation, and duplicate checks all happen in
app.domain.product / ProductService, not in this class.

Categories/brands/units are passed in already-fetched (see ProductsPage,
which loads them once via a background Worker and caches them) rather
than this dialog querying CatalogService itself in __init__ — doing that
query synchronously used to block the GUI thread on every single dialog
open (a real, measured freeze, not theoretical), since __init__ runs
before dialog.exec() returns control to the event loop.

read_only=True turns this into a view-only dialog (fields disabled, no
Save button) — the caller decides that based on whether the current
session actually has product.update, giving "View Product" without a
separate screen.
"""
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductNotFoundError,
    ProductValidationError,
)
from app.schemas.product import BrandOut, CategoryOut, ProductCreate, ProductOut, ProductUpdate, UnitOut
from app.services.product_service import ProductService
from app.ui.theme import RED, STYLESHEET
from app.workers.base_worker import Worker

_NONE_ITEM = "—"


class ProductFormDialog(QDialog):
    def __init__(self, product_service: ProductService, categories: list[CategoryOut],
                brands: list[BrandOut], units: list[UnitOut],
                product: ProductOut | None = None, read_only: bool = False, parent=None,
                default_tax_percent: Decimal = Decimal("0")):
        super().__init__(parent)
        self._product_service = product_service
        self._product = product
        self._read_only = read_only
        self.setWindowTitle("View Product" if read_only else
                            ("Edit Product" if product else "Add Product"))
        self.setMinimumWidth(420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._sku = QLineEdit(product.sku if product else "")
        form.addRow("SKU *", self._sku)

        self._barcode = QLineEdit(product.barcode if product and product.barcode else "")
        form.addRow("Barcode", self._barcode)

        self._name = QLineEdit(product.name if product else "")
        form.addRow("Name *", self._name)

        self._description = QTextEdit(product.description if product and product.description
                                      else "")
        self._description.setFixedHeight(60)
        form.addRow("Description", self._description)

        self._category = QComboBox()
        self._category.addItem(_NONE_ITEM, None)
        for c in categories:
            self._category.addItem(c.name, c.id)
        if product and product.category:
            self._select_by_data(self._category, product.category.id)
        form.addRow("Category", self._category)

        self._brand = QComboBox()
        self._brand.addItem(_NONE_ITEM, None)
        for b in brands:
            self._brand.addItem(b.name, b.id)
        if product and product.brand:
            self._select_by_data(self._brand, product.brand.id)
        form.addRow("Brand", self._brand)

        self._unit = QComboBox()
        for u in units:
            self._unit.addItem(f"{u.name} ({u.abbreviation})", u.id)
        if product:
            self._select_by_data(self._unit, product.unit.id)
        form.addRow("Unit *", self._unit)

        self._purchase_price = QLineEdit(str(product.purchase_price) if product else "0")
        form.addRow("Purchase Price *", self._purchase_price)

        self._selling_price = QLineEdit(str(product.selling_price) if product else "0")
        form.addRow("Selling Price *", self._selling_price)

        self._tax_percent = QLineEdit(
            str(product.tax_percent) if product else str(default_tax_percent))
        form.addRow("Tax %", self._tax_percent)

        self._minimum_stock_level = QLineEdit(
            str(product.minimum_stock_level) if product else "0")
        form.addRow("Minimum Stock Level", self._minimum_stock_level)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        if read_only:
            for widget in (self._sku, self._barcode, self._name, self._description,
                          self._category, self._brand, self._unit, self._purchase_price,
                          self._selling_price, self._tax_percent,
                          self._minimum_stock_level):
                widget.setEnabled(False)
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
        else:
            self._save_button = QPushButton("Save Product")
            self._save_button.setObjectName("primary")
            self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_button.clicked.connect(self._submit)
            layout.addWidget(self._save_button)

    @staticmethod
    def _select_by_data(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _parse_decimal(self, text: str, field_label: str) -> Decimal | None:
        try:
            return Decimal(text.strip() or "0")
        except InvalidOperation:
            self._show_error(f"{field_label} must be a number.")
            return None

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Product")

    def _submit(self) -> None:
        self._error_label.hide()
        purchase_price = self._parse_decimal(self._purchase_price.text(), "Purchase price")
        if purchase_price is None:
            return
        selling_price = self._parse_decimal(self._selling_price.text(), "Selling price")
        if selling_price is None:
            return
        tax_percent = self._parse_decimal(self._tax_percent.text(), "Tax %")
        if tax_percent is None:
            return
        minimum_stock_level = self._parse_decimal(self._minimum_stock_level.text(),
                                                   "Minimum stock level")
        if minimum_stock_level is None:
            return

        unit_id = self._unit.currentData()
        if unit_id is None:
            self._show_error("Select a unit.")
            return

        description = self._description.toPlainText().strip() or None
        barcode = self._barcode.text().strip() or None

        self._set_busy(True)
        if self._product is None:
            data = ProductCreate(sku=self._sku.text(), barcode=barcode,
                                 name=self._name.text(), description=description,
                                 category_id=self._category.currentData(),
                                 brand_id=self._brand.currentData(), unit_id=unit_id,
                                 purchase_price=purchase_price, selling_price=selling_price,
                                 tax_percent=tax_percent,
                                 minimum_stock_level=minimum_stock_level)
            worker = Worker(self._product_service.create_product, data)
        else:
            data = ProductUpdate(sku=self._sku.text(), barcode=barcode,
                                 name=self._name.text(), description=description,
                                 category_id=self._category.currentData(),
                                 brand_id=self._brand.currentData(), unit_id=unit_id,
                                 purchase_price=purchase_price, selling_price=selling_price,
                                 tax_percent=tax_percent,
                                 minimum_stock_level=minimum_stock_level)
            worker = Worker(self._product_service.update_product, self._product.id, data)

        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _product: ProductOut) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, ProductValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (DuplicateSkuError, DuplicateBarcodeError, ProductNotFoundError)):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this product. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
