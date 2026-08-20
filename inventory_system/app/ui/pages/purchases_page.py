"""Purchases page — search/filter/paginate PurchaseOrder records, with
Create/Submit/Approve/Receive Goods/Cancel actions. Mirrors
sales_orders_page.py structure. Replaces the previous content of this
sidebar entry (a read-only legacy Excel Bill list, via
app.services.billing_service) — that list was a second, disconnected
purchase record a real purchase order here never appeared in, which is
why "record a purchase" looked unavailable. The real purchasing workflow
(Supplier/PurchaseOrder/GoodsReceipt, see app.services.purchase_service)
is what actually tracks purchases and moves inventory (via receive_goods).

No business logic here: every action calls PurchaseService on a
background Worker. Permission-gated actions are hidden per-row when the
session lacks the permission — a convenience, not the boundary:
PurchaseService enforces the same rule independently via
@require_permission.
"""
import logging

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QComboBox,
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
from app.domain.purchasing import PurchaseOrderStatus
from app.schemas.product import ProductFilter
from app.schemas.purchasing import PurchaseOrderFilter, PurchaseOrderOut, PurchaseOrderPage
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.purchase_service import PurchaseService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.purchase_order_form_dialog import PurchaseOrderFormDialog
from app.ui.widgets.receive_goods_dialog import ReceiveGoodsDialog
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_COLUMNS = ["Order #", "Supplier", "Warehouse", "Status", "Total"]

_logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{value:,.2f}"


class PurchasesPage(QWidget):
    def __init__(self, purchase_service: PurchaseService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._purchase_service = purchase_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions

        self._page = 1
        self._current_items: list[PurchaseOrderOut] = []
        self._suppliers: list = []
        self._warehouses: list = []
        self._products: list = []
        self._supplier_names: dict = {}
        self._warehouse_names: dict = {}
        self._product_names: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Purchases", "Purchase orders, from draft through "
                                                  "goods received."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=lambda: self._purchase_service.search_purchase_orders(self._build_filter()),
            render=self._render_table, is_empty=lambda page: len(page.items) == 0,
            empty_state=EmptyStateWidget(
                "No purchase orders found", icon="📥",
                message="Purchase orders you create will show up here."),
            error_message="Couldn't load purchase orders.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        layout.addLayout(self._build_row_actions())

        self._load_reference_data()

    def refresh(self) -> None:
        self._async_area.reload()

    # -- permissions --------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar --------------------------------------------------------- #
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        for status in PurchaseOrderStatus:
            self._status_filter.addItem(status.value.replace("_", " ").title(), status)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._status_filter)

        bar.addStretch()

        self._create_button = None
        if self._can("purchases.create"):
            self._create_button = QPushButton("+ Create Purchase Order")
            self._create_button.setObjectName("primary")
            self._create_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._create_button.clicked.connect(self._open_create_dialog)
            # Suppliers/warehouses/products load asynchronously (see
            # _load_reference_data) — disabled until that finishes so a
            # click landing before the load completes can't open the
            # Create dialog with stale-empty lists (every field would show
            # "No suppliers/warehouses/products available" even when real
            # data exists, just not fetched yet).
            self._create_button.setEnabled(False)
            self._create_button.setToolTip("Loading suppliers, warehouses, and products…")
            bar.addWidget(self._create_button)
        return bar

    def _fetch_reference_data(self):
        # Inactive suppliers/warehouses are excluded — same "archived
        # things don't populate new-transaction pickers" rule already
        # applied to products just below (ProductStatus.ACTIVE). Extracted
        # from _load_reference_data so it's directly unit-testable without
        # going through QThreadPool — see tests/ui/test_purchases_page.py.
        suppliers = [s for s in self._purchase_service.list_suppliers() if s.is_active]
        warehouses = [w for w in self._inventory_service.list_warehouses() if w.is_active]
        products = self._product_service.search_products(
            ProductFilter(status=ProductStatus.ACTIVE, page_size=500)).items
        return suppliers, warehouses, products

    def _load_reference_data(self) -> None:
        worker = Worker(self._fetch_reference_data)
        worker.signals.finished.connect(self._on_reference_data_loaded)
        worker.signals.error.connect(self._on_reference_data_error)
        QThreadPool.globalInstance().start(worker)

    def _on_reference_data_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load purchase reference data", exc_info=exc)
        if self._create_button is not None:
            self._create_button.setEnabled(True)
            self._create_button.setToolTip("Couldn't load suppliers/warehouses/products — "
                                           "the form may be missing options. Try again.")

    def _on_reference_data_loaded(self, result) -> None:
        self._suppliers, self._warehouses, self._products = result
        self._supplier_names = {s.id: s.name for s in self._suppliers}
        self._warehouse_names = {w.id: w.name for w in self._warehouses}
        self._product_names = {p.id: p.name for p in self._products}
        table = self._async_area.currentWidget()
        if isinstance(table, QTableWidget):
            self._render_table_contents(table)
        if self._create_button is not None:
            self._create_button.setEnabled(True)
            self._create_button.setToolTip("")

    # -- create ------------------------------------------------------------#
    def _open_create_dialog(self) -> None:
        dialog = PurchaseOrderFormDialog(self._purchase_service, self._product_service,
                                         self._inventory_service, self._suppliers,
                                         self._warehouses, parent=self)
        if dialog.exec():
            self.refresh()

    # -- row actions ------------------------------------------------------#
    def _build_row_actions(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 0, 28, 16)
        bar.setSpacing(10)

        self._submit_button = QPushButton("Submit")
        self._submit_button.setObjectName("ghost")
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.clicked.connect(self._submit_selected)
        self._submit_button.setEnabled(False)
        bar.addWidget(self._submit_button)

        self._approve_button = QPushButton("Approve")
        self._approve_button.setObjectName("ghost")
        self._approve_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._approve_button.clicked.connect(self._approve_selected)
        self._approve_button.setEnabled(False)
        bar.addWidget(self._approve_button)

        self._receive_button = QPushButton("Receive Goods")
        self._receive_button.setObjectName("ghost")
        self._receive_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._receive_button.clicked.connect(self._receive_selected)
        self._receive_button.setEnabled(False)
        bar.addWidget(self._receive_button)

        self._cancel_button = QPushButton("Cancel Order")
        self._cancel_button.setObjectName("danger")
        self._cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_button.clicked.connect(self._cancel_selected)
        self._cancel_button.setEnabled(False)
        bar.addWidget(self._cancel_button)

        bar.addStretch()
        return bar

    # -- data flow ----------------------------------------------------- #
    def _build_filter(self) -> PurchaseOrderFilter:
        return PurchaseOrderFilter(status=self._status_filter.currentData(),
                                   page=self._page, page_size=25)

    def _on_filter_changed(self) -> None:
        self._page = 1
        self.refresh()

    def _on_page_changed(self, page: int) -> None:
        self._page = page
        self.refresh()

    # -- table rendering ------------------------------------------------ #
    def _render_table(self, page: PurchaseOrderPage) -> QTableWidget:
        self._current_items = page.items
        self._pagination.set_state(page=page.page, total_pages=page.total_pages,
                                   total=page.total)

        table = QTableWidget(len(page.items), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.itemSelectionChanged.connect(self._on_selection_changed)
        self._render_table_contents(table)
        return table

    def _render_table_contents(self, table: QTableWidget) -> None:
        for row, po in enumerate(self._current_items):
            supplier_name = self._supplier_names.get(po.supplier_id, str(po.supplier_id))
            warehouse_name = self._warehouse_names.get(po.warehouse_id, str(po.warehouse_id))
            values = [
                po.order_number or "—", supplier_name, warehouse_name,
                po.status.value.replace("_", " ").title(), _money(po.total_amount),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, col, item)

    def _on_selection_changed(self) -> None:
        po = self._selected_order()
        status = po.status if po is not None else None
        self._submit_button.setEnabled(
            po is not None and self._can("purchases.update")
            and status == PurchaseOrderStatus.DRAFT)
        self._approve_button.setEnabled(
            po is not None and self._can("purchases.approve")
            and status == PurchaseOrderStatus.SUBMITTED)
        self._receive_button.setEnabled(
            po is not None and self._can("purchases.receive")
            and status in (PurchaseOrderStatus.APPROVED,
                          PurchaseOrderStatus.PARTIALLY_RECEIVED))
        self._cancel_button.setEnabled(
            po is not None and self._can("purchases.cancel")
            and status in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SUBMITTED,
                          PurchaseOrderStatus.APPROVED))

    def _selected_order(self) -> PurchaseOrderOut | None:
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

    # -- actions --------------------------------------------------------- #
    def _submit_selected(self) -> None:
        po = self._selected_order()
        if po is None:
            return
        self._run_action(self._purchase_service.submit_purchase_order, po.id)

    def _approve_selected(self) -> None:
        po = self._selected_order()
        if po is None:
            return
        self._run_action(self._purchase_service.approve_purchase_order, po.id)

    def _cancel_selected(self) -> None:
        po = self._selected_order()
        if po is None:
            return
        if confirm(self, "Cancel Purchase Order",
                  f"Cancel purchase order {po.order_number or po.id}?",
                  confirm_label="Cancel Order", danger=True):
            self._run_action(self._purchase_service.cancel_purchase_order, po.id)

    def _receive_selected(self) -> None:
        po = self._selected_order()
        if po is None:
            return
        dialog = ReceiveGoodsDialog(self._purchase_service, po, self._product_names,
                                    parent=self)
        if dialog.exec():
            self.refresh()

    def _run_action(self, fn, purchase_order_id) -> None:
        worker = Worker(fn, purchase_order_id)
        worker.signals.finished.connect(lambda _: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Purchase order action failed", exc_info=exc)
        QMessageBox.critical(self, "Action failed", str(exc))
