"""Add Product dialog — a larger, sectioned, scrollable creation form
distinct from ProductFormDialog (which stays exactly as-is and still
handles Edit/View — see its own docstring). This dialog only ever
*creates*, never edits, since "opening quantity + warehouse" only makes
sense once, at creation time.

No business logic here — quantity/price/unit/warehouse validation, SKU
duplicate detection, and the atomic product+opening-stock transaction all
live in ProductService/SqlProductRepository (see
SqlProductRepository.create_with_opening_stock). This widget only collects
field values per the selected product type and displays whatever
ProductService.create_product returns or raises.

Categories/brands/units/warehouses are passed in already-fetched (see
ProductsPage._load_catalog_options) — same convention as the rest of this
app's dialogs, so opening this one never blocks the GUI thread on a
synchronous query.

Product type (Goods/Services/Expenses) controls which fields are
*relevant*, expressed by disabling (not hiding) the fields that don't
apply — Opening Quantity/Warehouse/Size/Color/Flavour/DFTQC No./Made &
Imported From/Expiry Date only make sense for physical GOODS. Re-order
Point is similarly disabled for non-GOODS. Everything else (name,
category, unit, pricing, tax) applies to all three types.
"""
import re
import secrets
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QDate, QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductValidationError,
    UnitNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.product import ProductType
from app.schemas.inventory import WarehouseOut
from app.schemas.product import BrandOut, CategoryOut, ProductCreate, ProductOut, UnitOut
from app.security.authorization import PermissionDeniedError
from app.services.product_service import ProductService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.workers.base_worker import Worker

_NONE_ITEM = "—"
_GOODS_ONLY_HINT = "Only applies to Goods."


def _generate_sku_candidate(name: str) -> str:
    """A reasonable, virtually-collision-free starting point for manual
    entry — not the source of truth for uniqueness (ProductService still
    checks sku_exists server-side, same as any manually-typed SKU; if this
    candidate ever did collide, Save would show the normal duplicate-SKU
    error and the user can click Generate again).
    """
    letters = re.sub(r"[^A-Za-z0-9]", "", name or "").upper()
    prefix = (letters[:4] or "PRD")
    suffix = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))
    return f"{prefix}-{suffix}"


class _SectionCard(QWidget):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(4)

        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            sub.setWordWrap(True)
            outer.addWidget(sub)
        outer.addSpacing(8)

        self.form = QFormLayout()
        self.form.setSpacing(10)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        outer.addLayout(self.form)


class AddProductDialog(QDialog):
    def __init__(self, product_service: ProductService, categories: list[CategoryOut],
                brands: list[BrandOut], units: list[UnitOut], warehouses: list[WarehouseOut],
                parent=None, default_tax_percent: Decimal = Decimal("0")):
        super().__init__(parent)
        self._product_service = product_service
        self._units_data = units
        self._default_tax_percent = default_tax_percent
        self.product: ProductOut | None = None

        self.setWindowTitle("Add Product")
        self.setMinimumSize(560, 640)
        self.resize(640, 760)
        self.setStyleSheet(STYLESHEET)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_type_tabs())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(24, 16, 24, 16)
        scroll_layout.setSpacing(16)

        scroll_layout.addWidget(self._build_general_info_section(categories, brands))
        scroll_layout.addWidget(self._build_units_section(units))
        scroll_layout.addWidget(self._build_inventory_section(warehouses))
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        outer.addWidget(scroll, stretch=1)

        outer.addLayout(self._build_footer())

        self._on_type_changed(ProductType.GOODS)

    # -- header ------------------------------------------------------- #
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(24, 20, 24, 12)
        title = QLabel("Add Product")
        title.setObjectName("pageTitle")
        row.addWidget(title)
        row.addStretch()
        close_button = QPushButton("✕")
        close_button.setObjectName("flat")
        close_button.setFixedSize(32, 32)
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.reject)
        row.addWidget(close_button)
        return row

    # -- product type tabs --------------------------------------------- #
    def _build_type_tabs(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(24, 0, 24, 12)
        row.setSpacing(8)

        self._type_buttons: dict[ProductType, QPushButton] = {}
        labels = {ProductType.GOODS: "Goods", ProductType.SERVICE: "Services",
                 ProductType.EXPENSE: "Expenses"}
        for product_type, label in labels.items():
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setObjectName("ghost")
            button.clicked.connect(lambda _checked, pt=product_type: self._on_type_changed(pt))
            row.addWidget(button)
            self._type_buttons[product_type] = button
        row.addStretch()
        return row

    def _on_type_changed(self, product_type: ProductType) -> None:
        self._selected_type = product_type
        for pt, button in self._type_buttons.items():
            button.setChecked(pt == product_type)
            button.setObjectName("primary" if pt == product_type else "ghost")
            # objectName-based QSS doesn't re-apply automatically after
            # setObjectName() — unpolish/polish forces Qt to re-evaluate it.
            button.style().unpolish(button)
            button.style().polish(button)
        is_goods = product_type == ProductType.GOODS
        for widget in self._goods_only_widgets:
            widget.setEnabled(is_goods)
        if not is_goods:
            self._opening_quantity.setText("0")
            self._warehouse.setCurrentIndex(0)
            self._reorder_point.setText("0")
            self._no_expiry.setChecked(True)

    # -- general info --------------------------------------------------#
    def _build_general_info_section(self, categories: list[CategoryOut],
                                    brands: list[BrandOut]) -> QWidget:
        card = _SectionCard("General Info", "The essentials every product needs.")
        self._goods_only_widgets: list[QWidget] = []

        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Basmati Rice 5kg")
        card.form.addRow("Name *", self._name)

        self._category = QComboBox()
        self._category.setEditable(True)
        self._category.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._category.addItem(_NONE_ITEM, None)
        for c in sorted(categories, key=lambda c: c.name.lower()):
            self._category.addItem(c.name, c.id)
        card.form.addRow("Category *", self._category)

        self._brand = QComboBox()
        self._brand.setEditable(True)
        self._brand.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._brand.addItem(_NONE_ITEM, None)
        for b in sorted(brands, key=lambda b: b.name.lower()):
            self._brand.addItem(b.name, b.id)
        card.form.addRow("Brand", self._brand)

        self._hsn_code = QLineEdit()
        self._hsn_code.setPlaceholderText("Optional")
        card.form.addRow("HSN Code", self._hsn_code)

        self._barcode = QLineEdit()
        self._barcode.setPlaceholderText("Optional")
        card.form.addRow("Barcode", self._barcode)

        sku_row = QHBoxLayout()
        self._sku = QLineEdit()
        self._sku.setPlaceholderText("e.g. RICE-BAS-5KG")
        sku_row.addWidget(self._sku, stretch=1)
        generate_button = QPushButton("Generate")
        generate_button.setObjectName("ghost")
        generate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        generate_button.clicked.connect(self._on_generate_sku)
        sku_row.addWidget(generate_button)
        sku_row_widget = QWidget()
        sku_row_widget.setLayout(sku_row)
        card.form.addRow("Code / SKU *", sku_row_widget)

        self._reorder_point = QLineEdit("0")
        card.form.addRow("Re-order Point", self._reorder_point)
        self._goods_only_widgets.append(self._reorder_point)

        self._description = QTextEdit()
        self._description.setPlaceholderText("Optional notes about this product…")
        self._description.setFixedHeight(72)
        card.form.addRow("Description", self._description)

        return card

    def _on_generate_sku(self) -> None:
        self._sku.setText(_generate_sku_candidate(self._name.text()))

    # -- units -----------------------------------------------------------#
    def _build_units_section(self, units: list[UnitOut]) -> QWidget:
        card = _SectionCard("Units", "The base unit is required; sub-/tertiary units are "
                                     "optional and need a real conversion factor.")

        self._unit = QComboBox()
        for u in sorted(units, key=lambda u: u.name.lower()):
            self._unit.addItem(f"{u.name} ({u.abbreviation})", u.id)
        card.form.addRow("Unit *", self._unit)

        self._sub_unit = QComboBox()
        self._sub_unit.addItem(_NONE_ITEM, None)
        for u in sorted(units, key=lambda u: u.name.lower()):
            self._sub_unit.addItem(f"{u.name} ({u.abbreviation})", u.id)
        self._sub_unit.currentIndexChanged.connect(self._on_sub_unit_changed)
        card.form.addRow("Sub-unit", self._sub_unit)

        self._sub_unit_conversion = QLineEdit()
        self._sub_unit_conversion.setPlaceholderText(
            "e.g. 12 (1 Sub-unit = 12 base units)")
        self._sub_unit_conversion.setEnabled(False)
        card.form.addRow("Unit Conversion", self._sub_unit_conversion)

        self._tertiary_unit = QComboBox()
        self._tertiary_unit.addItem(_NONE_ITEM, None)
        for u in sorted(units, key=lambda u: u.name.lower()):
            self._tertiary_unit.addItem(f"{u.name} ({u.abbreviation})", u.id)
        self._tertiary_unit.currentIndexChanged.connect(self._on_tertiary_unit_changed)
        card.form.addRow("Tertiary Unit", self._tertiary_unit)

        self._tertiary_unit_conversion = QLineEdit()
        self._tertiary_unit_conversion.setPlaceholderText(
            "e.g. 24 (1 Tertiary unit = 24 base units)")
        self._tertiary_unit_conversion.setEnabled(False)
        card.form.addRow("Tertiary Conversion", self._tertiary_unit_conversion)

        return card

    def _on_sub_unit_changed(self) -> None:
        self._sub_unit_conversion.setEnabled(self._sub_unit.currentData() is not None)
        if self._sub_unit.currentData() is None:
            self._sub_unit_conversion.clear()

    def _on_tertiary_unit_changed(self) -> None:
        self._tertiary_unit_conversion.setEnabled(self._tertiary_unit.currentData() is not None)
        if self._tertiary_unit.currentData() is None:
            self._tertiary_unit_conversion.clear()

    # -- inventory --------------------------------------------------------#
    def _build_inventory_section(self, warehouses: list[WarehouseOut]) -> QWidget:
        card = _SectionCard(
            "Inventory",
            "Opening stock is created in one transaction with the product itself." if warehouses
            else "No warehouses yet — add one before recording an opening quantity.")

        self._opening_quantity = QLineEdit("0")
        card.form.addRow("Opening Quantity", self._opening_quantity)
        self._goods_only_widgets.append(self._opening_quantity)

        self._warehouse = QComboBox()
        self._warehouse.addItem(_NONE_ITEM, None)
        for w in sorted(warehouses, key=lambda w: w.name.lower()):
            self._warehouse.addItem(f"{w.code} — {w.name}", w.id)
        card.form.addRow("Warehouse *", self._warehouse)
        self._goods_only_widgets.append(self._warehouse)

        self._purchase_price = QLineEdit("0")
        card.form.addRow("Purchase Price", self._purchase_price)

        self._selling_price = QLineEdit("0")
        card.form.addRow("Selling Price", self._selling_price)

        tax_row = QHBoxLayout()
        self._tax_percent = QLineEdit(str(self._default_tax_percent))
        tax_row.addWidget(self._tax_percent, stretch=1)
        self._non_taxable = QCheckBox("Non-taxable")
        self._non_taxable.toggled.connect(self._on_non_taxable_toggled)
        tax_row.addWidget(self._non_taxable)
        tax_row_widget = QWidget()
        tax_row_widget.setLayout(tax_row)
        card.form.addRow("Tax %", tax_row_widget)

        self._size = QLineEdit()
        card.form.addRow("Size", self._size)
        self._goods_only_widgets.append(self._size)

        self._color = QLineEdit()
        card.form.addRow("Color", self._color)
        self._goods_only_widgets.append(self._color)

        self._flavour = QLineEdit()
        card.form.addRow("Flavour", self._flavour)
        self._goods_only_widgets.append(self._flavour)

        self._dftqc_no = QLineEdit()
        card.form.addRow("DFTQC No.", self._dftqc_no)
        self._goods_only_widgets.append(self._dftqc_no)

        self._country_of_origin = QLineEdit()
        # "&&" not "&" — QFormLayout labels treat a lone & as a mnemonic
        # marker (would silently swallow it and underline the next
        # letter instead of showing a literal ampersand).
        card.form.addRow("Made && Imported From", self._country_of_origin)
        self._goods_only_widgets.append(self._country_of_origin)

        expiry_row = QHBoxLayout()
        self._expiry_date = QDateEdit(QDate.currentDate())
        self._expiry_date.setCalendarPopup(True)
        self._expiry_date.setEnabled(False)
        expiry_row.addWidget(self._expiry_date, stretch=1)
        self._no_expiry = QCheckBox("No expiry")
        self._no_expiry.setChecked(True)
        self._no_expiry.toggled.connect(lambda checked: self._expiry_date.setEnabled(not checked))
        expiry_row.addWidget(self._no_expiry)
        expiry_row_widget = QWidget()
        expiry_row_widget.setLayout(expiry_row)
        card.form.addRow("Expiry Date", expiry_row_widget)
        self._goods_only_widgets.append(expiry_row_widget)

        return card

    def _on_non_taxable_toggled(self, checked: bool) -> None:
        self._tax_percent.setEnabled(not checked)
        if checked:
            self._tax_percent.setText("0")

    # -- footer ------------------------------------------------------- #
    def _build_footer(self) -> QVBoxLayout:
        container = QVBoxLayout()
        container.setContentsMargins(24, 8, 24, 20)
        container.setSpacing(8)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        container.addWidget(self._error_label)

        self._save_button = QPushButton("Save Product")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.setMinimumHeight(40)
        self._save_button.clicked.connect(self._submit)
        container.addWidget(self._save_button)
        return container

    # -- validation helpers ------------------------------------------- #
    def _parse_decimal(self, text: str, field_label: str,
                       allow_blank_as_zero: bool = True) -> Decimal | None:
        text = text.strip()
        if not text and allow_blank_as_zero:
            return Decimal("0")
        try:
            return Decimal(text)
        except InvalidOperation:
            self._show_error(f"{field_label} must be a number.")
            return None

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Product")

    # -- submit --------------------------------------------------------- #
    def _submit(self) -> None:
        self._error_label.hide()

        name = self._name.text().strip()
        sku = self._sku.text().strip()
        if not name:
            return self._show_error("Name is required.")
        if not sku:
            return self._show_error("Code / SKU is required.")

        unit_id = self._unit.currentData()
        if unit_id is None:
            return self._show_error("Select a unit.")

        is_goods = self._selected_type == ProductType.GOODS

        opening_quantity = self._parse_decimal(
            self._opening_quantity.text() if is_goods else "0", "Opening quantity")
        if opening_quantity is None:
            return
        purchase_price = self._parse_decimal(self._purchase_price.text(), "Purchase price")
        if purchase_price is None:
            return
        selling_price = self._parse_decimal(self._selling_price.text(), "Selling price")
        if selling_price is None:
            return
        tax_percent = self._parse_decimal(self._tax_percent.text(), "Tax %")
        if tax_percent is None:
            return
        reorder_point = self._parse_decimal(
            self._reorder_point.text() if is_goods else "0", "Re-order point")
        if reorder_point is None:
            return

        sub_unit_id = self._sub_unit.currentData()
        sub_unit_conversion = None
        if sub_unit_id is not None:
            sub_unit_conversion = self._parse_decimal(
                self._sub_unit_conversion.text(), "Unit conversion", allow_blank_as_zero=False)
            if sub_unit_conversion is None:
                return

        tertiary_unit_id = self._tertiary_unit.currentData()
        tertiary_unit_conversion = None
        if tertiary_unit_id is not None:
            tertiary_unit_conversion = self._parse_decimal(
                self._tertiary_unit_conversion.text(), "Tertiary conversion",
                allow_blank_as_zero=False)
            if tertiary_unit_conversion is None:
                return

        warehouse_id = self._warehouse.currentData() if is_goods else None
        expiry_date = None
        if is_goods and not self._no_expiry.isChecked():
            expiry_date = self._expiry_date.date().toPython()

        data = ProductCreate(
            sku=sku, barcode=self._barcode.text().strip() or None, name=name,
            description=self._description.toPlainText().strip() or None,
            product_type=self._selected_type,
            category_id=self._category.currentData(), brand_id=self._brand.currentData(),
            unit_id=unit_id, sub_unit_id=sub_unit_id,
            sub_unit_conversion_factor=sub_unit_conversion, tertiary_unit_id=tertiary_unit_id,
            tertiary_unit_conversion_factor=tertiary_unit_conversion,
            purchase_price=purchase_price, selling_price=selling_price,
            tax_percent=tax_percent, is_taxable=not self._non_taxable.isChecked(),
            minimum_stock_level=reorder_point, hsn_code=self._hsn_code.text().strip() or None,
            size=self._size.text().strip() or None, color=self._color.text().strip() or None,
            flavour=self._flavour.text().strip() or None,
            dftqc_no=self._dftqc_no.text().strip() or None,
            country_of_origin=self._country_of_origin.text().strip() or None,
            expiry_date=expiry_date)

        self._set_busy(True)
        worker = Worker(self._product_service.create_product, data,
                        warehouse_id=warehouse_id, opening_quantity=opening_quantity)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, result) -> None:
        self._set_busy(False)
        self.product, _opening_transaction = result
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, ProductValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (DuplicateSkuError, DuplicateBarcodeError, WarehouseNotFoundError,
                             UnitNotFoundError)):
            self._show_error(str(exc))
        elif isinstance(exc, PermissionDeniedError):
            self._show_error("You don't have permission to add stock — "
                            "the product itself was not saved.")
        else:
            self._show_error("Something went wrong saving this product. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
