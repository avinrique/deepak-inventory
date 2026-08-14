"""Sales orders page — search/filter/paginate SalesOrder records, with a
"View Invoice" action per selected row. Replaces the "Sales" sidebar
entry's previous content (a read-only legacy Excel Bill list — still at
app.ui.pages.sales_page.SalesPage, untouched, just no longer wired into
the sidebar) now that the real sales workflow (Customer/SalesOrder/
Invoice/Payment, see app.services.sales_service) is what actually tracks
sales.

No business logic here: every action calls SalesService on a background
Worker. Only browsing + invoice viewing are wired up — creating/confirming/
fulfilling a sale from this UI isn't built yet (sales.create/confirm/
fulfill/etc. permissions already exist in the catalog for when that lands).
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

from app.domain.sales import SalesOrderStatus
from app.schemas.sales import SalesOrderFilter, SalesOrderOut, SalesOrderPage
from app.services.inventory_service import InventoryService
from app.services.sales_service import SalesService
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.invoice_preview_dialog import InvoicePreviewDialog
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_COLUMNS = ["Order Date", "Customer", "Warehouse", "Status", "Total"]
_INVOICEABLE_STATUSES = {SalesOrderStatus.FULFILLED, SalesOrderStatus.COMPLETED}

_logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{value:,.2f}"


class SalesOrdersPage(QWidget):
    def __init__(self, sales_service: SalesService, inventory_service: InventoryService):
        super().__init__()
        self._sales_service = sales_service
        self._inventory_service = inventory_service

        self._page = 1
        self._current_items: list[SalesOrderOut] = []
        self._customer_names: dict = {}
        self._warehouse_codes: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Sales", "Sales orders, from draft through payment."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=lambda: self._sales_service.search_sales_orders(self._build_filter()),
            render=self._render_table, is_empty=lambda page: len(page.items) == 0,
            empty_state=EmptyStateWidget(
                "No sales orders found", icon="📤",
                message="Sales orders you create will show up here."),
            error_message="Couldn't load sales orders.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        layout.addLayout(self._build_row_actions())

        self._load_filter_options()

    def refresh(self) -> None:
        self._async_area.reload()

    # -- toolbar ---------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        for status in SalesOrderStatus:
            self._status_filter.addItem(status.value.title(), status)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._status_filter)

        self._customer_filter = QComboBox()
        self._customer_filter.addItem("All Customers", None)
        self._customer_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._customer_filter)

        bar.addStretch()
        return bar

    def _load_filter_options(self) -> None:
        def load():
            return self._sales_service.list_customers(), self._inventory_service.list_warehouses()

        worker = Worker(load)
        worker.signals.finished.connect(self._on_filter_options_loaded)
        worker.signals.error.connect(self._on_filter_options_error)
        QThreadPool.globalInstance().start(worker)

    def _on_filter_options_loaded(self, result) -> None:
        customers, warehouses = result
        for customer in customers:
            self._customer_filter.addItem(customer.name, customer.id)
            self._customer_names[customer.id] = customer.name
        for warehouse in warehouses:
            self._warehouse_codes[warehouse.id] = warehouse.code
        # Rows already rendered (from the page's own load) won't have had
        # customer/warehouse names available yet on first paint — refresh
        # once so they pick these lookups up instead of showing raw ids.
        table = self._async_area.currentWidget()
        if isinstance(table, QTableWidget):
            self._render_table_contents(table)

    def _on_filter_options_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load sales page filter options", exc_info=exc)

    # -- row actions ------------------------------------------------------#
    def _build_row_actions(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 0, 28, 16)
        bar.setSpacing(10)

        self._view_invoice_button = QPushButton("View Invoice")
        self._view_invoice_button.setObjectName("ghost")
        self._view_invoice_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_invoice_button.clicked.connect(self._view_invoice_for_selected)
        self._view_invoice_button.setEnabled(False)
        bar.addWidget(self._view_invoice_button)

        bar.addStretch()
        return bar

    # -- data flow ----------------------------------------------------- #
    def _build_filter(self) -> SalesOrderFilter:
        return SalesOrderFilter(customer_id=self._customer_filter.currentData(),
                                status=self._status_filter.currentData(),
                                page=self._page, page_size=25)

    def _on_filter_changed(self) -> None:
        self._page = 1
        self.refresh()

    def _on_page_changed(self, page: int) -> None:
        self._page = page
        self.refresh()

    # -- table rendering ------------------------------------------------ #
    def _render_table(self, page: SalesOrderPage) -> QTableWidget:
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
        for row, so in enumerate(self._current_items):
            customer_name = self._customer_names.get(so.customer_id, str(so.customer_id))
            warehouse_code = self._warehouse_codes.get(so.warehouse_id, str(so.warehouse_id))
            values = [
                so.created_at.strftime("%Y-%m-%d"), customer_name, warehouse_code,
                so.status.value.replace("_", " ").title(), _money(so.total_amount),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, col, item)

    def _on_selection_changed(self) -> None:
        so = self._selected_order()
        self._view_invoice_button.setEnabled(
            so is not None and so.status in _INVOICEABLE_STATUSES)

    def _selected_order(self) -> SalesOrderOut | None:
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

    # -- view invoice --------------------------------------------------- #
    def _view_invoice_for_selected(self) -> None:
        so = self._selected_order()
        if so is None:
            return
        self._view_invoice_button.setEnabled(False)
        worker = Worker(self._sales_service.get_invoice_by_sales_order, so.id)
        worker.signals.finished.connect(self._on_invoice_looked_up)
        worker.signals.error.connect(self._on_invoice_lookup_error)
        QThreadPool.globalInstance().start(worker)

    def _on_invoice_looked_up(self, invoice) -> None:
        self._view_invoice_button.setEnabled(True)
        if invoice is None:
            QMessageBox.information(self, "No invoice yet",
                                   "This order doesn't have an invoice generated yet.")
            return
        dialog = InvoicePreviewDialog(self._sales_service, invoice.id, parent=self)
        dialog.exec()

    def _on_invoice_lookup_error(self, exc: Exception) -> None:
        self._view_invoice_button.setEnabled(True)
        _logger.exception("Looking up invoice for sales order failed", exc_info=exc)
        QMessageBox.critical(self, "Couldn't open invoice", str(exc))
