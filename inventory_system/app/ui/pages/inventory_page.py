"""Inventory page — live per-warehouse stock levels via InventoryService
(the SQL-backed warehouse ledger, see app/services/inventory_service.py),
plus "+ Stock In" and "Adjust Stock" actions that write to the same
ledger. The legacy Excel-backed StockService (app.services.stock_service)
still exists and still powers the old Tkinter app, but this page no
longer reads from it — it was a second, disconnected stock number that a
Stock In here never moved, which made Stock In look broken.

"Adjust Stock" opens StockAdjustmentDialog, covering the rest of
InventoryService's stock-changing operations (stock out, mark damaged,
manual adjustment, transfer between warehouses) that previously had no UI
at all despite being fully implemented and atomically ledgered in
InventoryService/InventoryRepository — see that dialog's docstring.

Products/warehouses for the Stock In / Adjust Stock pickers are loaded via
their own independent _load_reference_data() worker (same convention as
PurchasesPage._load_reference_data/SalesOrdersPage._load_filter_options) —
NOT via the levels table's AsyncContentArea render callback. That used to
be a real bug: AsyncContentArea only calls render() when is_empty(data) is
False, but is_empty here is based on the *levels* list, which is empty
for any organization/warehouse that has never had a Stock In yet — the
levels list. _open_stock_in_dialog previously read self._products/
self._warehouses populated only as a side effect of _render_table, so on
a brand new setup (zero inventory levels — the single most common
real-world state, since a level only exists after a first Stock In) that
side effect never ran, leaving both lists permanently empty: Stock In
would show "No products/warehouses available" and its Add Stock button
permanently disabled, forever, since the very first Stock In that would
create a level could never be submitted. See tests/ui/test_inventory_page.py
for the regression test.
"""
import logging

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain.product import ProductStatus
from app.schemas.inventory import InventoryLevel
from app.schemas.product import ProductFilter
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.stock_service import StockService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.stock_adjustment_dialog import StockAdjustmentDialog, StockAdjustmentMode
from app.ui.widgets.stock_in_dialog import StockInDialog
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class InventoryPage(QWidget):
    def __init__(self, stock_service: StockService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._stock_service = stock_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions
        self._products: list = []
        self._warehouses: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Inventory", "Current quantity on hand, per product "
                                                  "and warehouse."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._load_inventory_data,
            render=self._render_table,
            is_empty=lambda data: len(data[0]) == 0,
            empty_state=EmptyStateWidget(
                "No stock recorded yet", icon="📊",
                message="Stock appears here once you record a Stock In, a purchase "
                        "receipt, or a sale."),
            error_message="Couldn't load inventory levels.")
        layout.addWidget(self._async_area, stretch=1)

        self._load_reference_data()

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.addStretch()

        self._adjust_button = None
        self._stock_in_button = None
        if self._can("inventory.adjust"):
            self._adjust_button = QPushButton("Adjust Stock")
            self._adjust_button.setObjectName("ghost")
            self._adjust_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._adjust_button.clicked.connect(self._open_stock_adjustment_dialog)
            # Disabled until _load_reference_data finishes — see this
            # module's docstring and _on_reference_data_loaded. Without
            # this, a click landing before the load completes opens the
            # dialog with empty product/warehouse pickers even though real
            # data exists, just not fetched yet.
            self._adjust_button.setEnabled(False)
            self._adjust_button.setToolTip("Loading products and warehouses…")
            bar.addWidget(self._adjust_button)

            self._stock_in_button = QPushButton("+ Stock In")
            self._stock_in_button.setObjectName("primary")
            self._stock_in_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._stock_in_button.clicked.connect(self._open_stock_in_dialog)
            self._stock_in_button.setEnabled(False)
            self._stock_in_button.setToolTip("Loading products and warehouses…")
            bar.addWidget(self._stock_in_button)
        return bar

    # -- reference data (products/warehouses for the Stock In / Adjust Stock
    #    pickers) -- loaded independently of the levels table below, so an
    #    organization/warehouse with zero recorded inventory levels (every
    #    org, before its first Stock In) still gets a working picker. See
    #    this module's docstring. ------------------------------------------#
    def _fetch_reference_data(self):
        products = self._product_service.search_products(
            ProductFilter(status=ProductStatus.ACTIVE, page_size=500)).items
        warehouses = self._inventory_service.list_warehouses()
        return products, warehouses

    def _load_reference_data(self) -> None:
        worker = Worker(self._fetch_reference_data)
        worker.signals.finished.connect(self._on_reference_data_loaded)
        worker.signals.error.connect(self._on_reference_data_error)
        QThreadPool.globalInstance().start(worker)

    def _on_reference_data_loaded(self, result) -> None:
        self._products, self._warehouses = result
        for button in (self._adjust_button, self._stock_in_button):
            if button is not None:
                button.setEnabled(True)
                button.setToolTip("")

    def _on_reference_data_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load inventory reference data", exc_info=exc)
        for button in (self._adjust_button, self._stock_in_button):
            if button is not None:
                button.setEnabled(True)
                button.setToolTip("Couldn't load products/warehouses — the form may be "
                                  "missing options. Try again.")

    # -- data flow ------------------------------------------------------#
    def _load_inventory_data(self):
        levels = self._inventory_service.list_all_levels()
        products = self._product_service.search_products(
            ProductFilter(status=ProductStatus.ACTIVE, page_size=500)).items
        warehouses = self._inventory_service.list_warehouses()
        return levels, products, warehouses

    def refresh(self) -> None:
        self._async_area.reload()
        self._load_reference_data()

    # -- stock in -------------------------------------------------------#
    def _open_stock_in_dialog(self) -> None:
        dialog = StockInDialog(self._inventory_service, self._products, self._warehouses,
                               parent=self)
        if dialog.exec() and dialog.transaction is not None:
            self._show_stock_in_confirmation(dialog.transaction)
            self.refresh()

    def _show_stock_in_confirmation(self, transaction) -> None:
        product = next((p for p in self._products if p.id == transaction.product_id), None)
        warehouse = next((w for w in self._warehouses if w.id == transaction.warehouse_id), None)
        product_label = product.name if product else str(transaction.product_id)
        warehouse_label = warehouse.name if warehouse else str(transaction.warehouse_id)
        QMessageBox.information(
            self, "Stock added",
            f"{product_label} at {warehouse_label} now has "
            f"{transaction.quantity_on_hand_after:g} on hand.")

    # -- stock out / mark damaged / adjustment / transfer ----------------#
    def _open_stock_adjustment_dialog(self) -> None:
        dialog = StockAdjustmentDialog(
            self._inventory_service, self._products, self._warehouses,
            allow_transfer=self._can("inventory.transfer"), parent=self)
        if dialog.exec() and dialog.transaction is not None:
            self._show_stock_adjustment_confirmation(dialog.mode, dialog.transaction)
            self.refresh()

    def _show_stock_adjustment_confirmation(self, mode, result) -> None:
        def product_label(product_id):
            product = next((p for p in self._products if p.id == product_id), None)
            return product.name if product else str(product_id)

        def warehouse_label(warehouse_id):
            warehouse = next((w for w in self._warehouses if w.id == warehouse_id), None)
            return warehouse.name if warehouse else str(warehouse_id)

        if mode is StockAdjustmentMode.TRANSFER:
            from_txn, to_txn = result
            QMessageBox.information(
                self, "Stock transferred",
                f"{product_label(from_txn.product_id)}: "
                f"{warehouse_label(from_txn.warehouse_id)} now has "
                f"{from_txn.quantity_on_hand_after:g}, "
                f"{warehouse_label(to_txn.warehouse_id)} now has "
                f"{to_txn.quantity_on_hand_after:g}.")
        else:
            QMessageBox.information(
                self, "Stock updated",
                f"{product_label(result.product_id)} at "
                f"{warehouse_label(result.warehouse_id)} now has "
                f"{result.quantity_on_hand_after:g} on hand.")

    # -- table rendering --------------------------------------------- #
    def _render_table(self, data) -> QTableWidget:
        # self._products/self._warehouses are populated independently by
        # _load_reference_data — NOT reassigned here. This callback is
        # skipped entirely by AsyncContentArea whenever `levels` is empty
        # (see this module's docstring), so it must never be the only
        # place those lists get set.
        levels, products, warehouses = data
        product_names = {p.id: p.name for p in products}
        warehouse_names = {w.id: w.name for w in warehouses}

        table = QTableWidget(len(levels), 4)
        table.setHorizontalHeaderLabels(["Product", "Warehouse", "On Hand", "Available"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setContentsMargins(28, 12, 28, 24)

        def sort_key(level: InventoryLevel) -> str:
            return product_names.get(level.product_id, str(level.product_id)).lower()

        for row, level in enumerate(sorted(levels, key=sort_key)):
            table.setItem(row, 0, QTableWidgetItem(
                product_names.get(level.product_id, str(level.product_id))))
            table.setItem(row, 1, QTableWidgetItem(
                warehouse_names.get(level.warehouse_id, level.warehouse_code)))
            on_hand_item = QTableWidgetItem(f"{level.quantity_on_hand:g}")
            on_hand_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 2, on_hand_item)
            available_item = QTableWidgetItem(f"{level.quantity_available:g}")
            available_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 3, available_item)
        return table
