"""Products page — search/filter/sort/paginate the catalog, add/edit/view/
archive products, manage Category/Brand/Unit. No business logic here: every
action calls ProductService/CatalogService on a background Worker and
renders whatever comes back — validation, permission checks, and
archive-excludes-from-transactions all live in the service layer.

Permission-gated actions (Add/Edit/Archive/Restore/Manage Catalog) are
hidden when the current session lacks the permission — a convenience, not
the actual boundary: ProductService/CatalogService enforce the same rule
independently via @require_permission, so a hidden button is not what's
stopping an unauthorized write.
"""
import logging
from decimal import Decimal

from PySide6.QtCore import Qt, QThreadPool, QTimer
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

from app.domain.product import ProductStatus
from app.schemas.product import ProductFilter, ProductOut, ProductPage
from app.security.session import SessionManager
from app.services.catalog_service import CatalogService
from app.services.product_service import ProductService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.catalog_manager_dialog import CatalogManagerDialog
from app.ui.widgets.combo_utils import select_by_data
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.add_product_dialog import AddProductDialog
from app.ui.widgets.product_form_dialog import ProductFormDialog
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker
from app.ui.theme import scale

_COLUMNS = [
    ("SKU", None), ("Name", "name"), ("Category", None), ("Brand", None), ("Unit", None),
    ("Purchase Price", "purchase_price"), ("Selling Price", "selling_price"),
    ("Tax %", None), ("Min Stock", None), ("Status", None),
]
_SEARCH_DEBOUNCE_MS = 300

_logger = logging.getLogger(__name__)


class ProductsPage(QWidget):
    def __init__(self, product_service: ProductService, catalog_service: CatalogService,
                sessions: SessionManager, organization_service=None, inventory_service=None):
        super().__init__()
        self._product_service = product_service
        self._catalog_service = catalog_service
        self._sessions = sessions
        self._organization_service = organization_service
        self._inventory_service = inventory_service
        self._default_tax_percent = Decimal("0")
        if organization_service is not None:
            worker = Worker(organization_service.get_current_organization)
            worker.signals.finished.connect(
                lambda org: setattr(self, "_default_tax_percent", org.default_tax_percent))
            worker.signals.error.connect(
                lambda exc: _logger.exception("Failed to load default tax percent",
                                              exc_info=exc))
            QThreadPool.globalInstance().start(worker)

        # Loaded once via a background Worker (see _load_catalog_options)
        # rather than queried synchronously wherever they're needed — a
        # per-dialog-open or per-page-load synchronous CatalogService call
        # used to block the GUI thread. Cached here and handed to
        # ProductFormDialog/the filter dropdowns already-fetched.
        self._categories: list = []
        self._brands: list = []
        self._units: list = []
        self._warehouses: list = []

        self._page = 1
        self._sort_by = "name"
        self._sort_desc = False
        self._current_items: list[ProductOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Products", "Manage your product catalog."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=lambda: self._product_service.search_products(self._build_filter()),
            render=self._render_table, is_empty=lambda page: len(page.items) == 0,
            empty_state=EmptyStateWidget(
                "No products found", icon="📦",
                message="Try a different search, or add your first product."),
            error_message="Couldn't load products.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        layout.addLayout(self._build_row_actions())

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self.refresh)

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search SKU, barcode, or name…")
        self._search.setFixedWidth(scale(240))
        self._search.textChanged.connect(self._on_search_changed)
        bar.addWidget(self._search)

        self._category_filter = QComboBox()
        self._category_filter.addItem("All Categories", None)
        self._category_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._category_filter)

        self._brand_filter = QComboBox()
        self._brand_filter.addItem("All Brands", None)
        self._brand_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._brand_filter)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        self._status_filter.addItem("Active", ProductStatus.ACTIVE)
        self._status_filter.addItem("Archived", ProductStatus.ARCHIVED)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._status_filter)

        bar.addStretch()

        if self._can("products.create") or self._can("products.update") or self._can(
                "products.delete"):
            manage_catalog_button = QPushButton("Manage Catalog")
            manage_catalog_button.setObjectName("ghost")
            manage_catalog_button.setCursor(Qt.CursorShape.PointingHandCursor)
            manage_catalog_button.clicked.connect(self._open_catalog_manager)
            bar.addWidget(manage_catalog_button)

        if self._can("products.create"):
            add_button = QPushButton("+ Add Product")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)

        self._load_catalog_options()
        return bar

    def _load_catalog_options(self) -> None:
        def load():
            warehouses = (self._inventory_service.list_warehouses()
                         if self._inventory_service is not None else [])
            return (self._catalog_service.list_categories(),
                    self._catalog_service.list_brands(), self._catalog_service.list_units(),
                    warehouses)

        worker = Worker(load)
        worker.signals.finished.connect(self._on_catalog_options_loaded)
        worker.signals.error.connect(self._on_catalog_options_error)
        QThreadPool.globalInstance().start(worker)

    def _on_catalog_options_loaded(self, result) -> None:
        self._categories, self._brands, self._units, self._warehouses = result

        previous_category = self._category_filter.currentData()
        previous_brand = self._brand_filter.currentData()

        self._category_filter.clear()
        self._category_filter.addItem("All Categories", None)
        for category in self._categories:
            self._category_filter.addItem(category.name, category.id)
        self._brand_filter.clear()
        self._brand_filter.addItem("All Brands", None)
        for brand in self._brands:
            self._brand_filter.addItem(brand.name, brand.id)

        select_by_data(self._category_filter, previous_category)
        select_by_data(self._brand_filter, previous_brand)

    def _on_catalog_options_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load catalog options (categories/brands/units)",
                          exc_info=exc)

    # -- row actions ----------------------------------------------------#
    def _build_row_actions(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 0, 28, 16)
        bar.setSpacing(10)

        self._view_button = QPushButton("View / Edit Selected")
        self._view_button.setObjectName("ghost")
        self._view_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_button.clicked.connect(self._open_selected)
        self._view_button.setEnabled(False)
        bar.addWidget(self._view_button)

        if self._can("products.delete"):
            self._archive_button = QPushButton("Archive Selected")
            self._archive_button.setObjectName("danger")
            self._archive_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._archive_button.clicked.connect(self._archive_selected)
            self._archive_button.setEnabled(False)
            bar.addWidget(self._archive_button)

        if self._can("products.update"):
            self._restore_button = QPushButton("Restore Selected")
            self._restore_button.setObjectName("ghost")
            self._restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._restore_button.clicked.connect(self._restore_selected)
            self._restore_button.setEnabled(False)
            bar.addWidget(self._restore_button)

        bar.addStretch()
        return bar

    # -- data flow ------------------------------------------------------#
    def _build_filter(self) -> ProductFilter:
        return ProductFilter(
            search=self._search.text().strip() or None,
            category_id=self._category_filter.currentData(),
            brand_id=self._brand_filter.currentData(),
            status=self._status_filter.currentData(), sort_by=self._sort_by,
            sort_desc=self._sort_desc, page=self._page, page_size=25)

    def refresh(self) -> None:
        self._async_area.reload()

    def _on_search_changed(self) -> None:
        self._page = 1
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)

    def _on_filter_changed(self) -> None:
        self._page = 1
        self.refresh()

    def _on_page_changed(self, page: int) -> None:
        self._page = page
        self.refresh()

    def _on_sort_clicked(self, column: int) -> None:
        _, sort_key = _COLUMNS[column]
        if sort_key is None:
            return
        if self._sort_by == sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_by = sort_key
            self._sort_desc = False
        self._page = 1
        self.refresh()

    # -- table rendering --------------------------------------------- #
    def _render_table(self, page: ProductPage) -> QTableWidget:
        self._current_items = page.items
        self._pagination.set_state(page=page.page, total_pages=page.total_pages,
                                   total=page.total)

        table = QTableWidget(len(page.items), len(_COLUMNS))
        table.setHorizontalHeaderLabels([label for label, _ in _COLUMNS])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().sectionClicked.connect(self._on_sort_clicked)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        table.doubleClicked.connect(lambda _: self._open_selected())

        right_align = {5, 6, 7, 8}
        for row, product in enumerate(page.items):
            values = [
                product.sku, product.name,
                product.category.name if product.category else "—",
                product.brand.name if product.brand else "—",
                f"{product.unit.name} ({product.unit.abbreviation})",
                f"{product.purchase_price:,.2f}", f"{product.selling_price:,.2f}",
                f"{product.tax_percent:g}%", f"{product.minimum_stock_level:g}",
                "Active" if product.status == ProductStatus.ACTIVE else "Archived",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in right_align:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, col, item)
        return table

    def _on_selection_changed(self) -> None:
        has_selection = self._selected_product() is not None
        self._view_button.setEnabled(has_selection)
        product = self._selected_product()
        if hasattr(self, "_archive_button"):
            self._archive_button.setEnabled(
                has_selection and product.status == ProductStatus.ACTIVE)
        if hasattr(self, "_restore_button"):
            self._restore_button.setEnabled(
                has_selection and product.status == ProductStatus.ARCHIVED)

    def _selected_product(self) -> ProductOut | None:
        table = self._async_area.currentWidget()
        if not isinstance(table, QTableWidget):
            return None
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        if row >= len(self._current_items):
            return None
        return self._current_items[row]

    # -- dialogs -------------------------------------------------------- #
    def _open_add_dialog(self) -> None:
        dialog = AddProductDialog(self._product_service, self._categories, self._brands,
                                  self._units, self._warehouses, parent=self,
                                  default_tax_percent=self._default_tax_percent)
        if dialog.exec():
            self.refresh()

    def _open_selected(self) -> None:
        product = self._selected_product()
        if product is None:
            return
        read_only = not self._can("products.update")
        dialog = ProductFormDialog(self._product_service, self._categories, self._brands,
                                   self._units, product=product, read_only=read_only,
                                   parent=self)
        if dialog.exec():
            self.refresh()

    def _open_catalog_manager(self) -> None:
        dialog = CatalogManagerDialog(self._catalog_service, parent=self)
        dialog.exec()
        self._load_catalog_options()  # catalog may have changed — reload, off the GUI thread
        self.refresh()

    def _archive_selected(self) -> None:
        product = self._selected_product()
        if product is None:
            return
        if confirm(self, "Archive Product",
                  f"Archive {product.name!r}? It will no longer be usable in new "
                  "sales or purchases.", confirm_label="Archive", danger=True):
            self._run_status_change(self._product_service.archive_product, product.id)

    def _restore_selected(self) -> None:
        product = self._selected_product()
        if product is None:
            return
        self._run_status_change(self._product_service.restore_product, product.id)

    def _run_status_change(self, fn, product_id) -> None:
        worker = Worker(fn, product_id)
        worker.signals.finished.connect(lambda _: self.refresh())
        worker.signals.error.connect(self._on_status_change_error)
        QThreadPool.globalInstance().start(worker)

    def _on_status_change_error(self, exc: Exception) -> None:
        # Not swallowed: logged, and the next reload (e.g. the user
        # retrying) will surface an error/stale state rather than silently
        # leaving the row looking unchanged with zero indication anything
        # went wrong.
        _logger.exception("Archive/restore failed", exc_info=exc)
