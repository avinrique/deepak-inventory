"""Purchases page — the purchase register: search/date-range/status/sort/
paginate over PurchaseService.list_purchase_transactions, with a Create
dialog and per-row View/Edit/workflow/Print actions. Replaces the previous
minimal 5-column list (Order #/Supplier/Warehouse/Status/Total) with the
full register the accounting side needs — Date, H.S Code, Invoice No.,
Reference, Supplier, Taxable/Non-Taxable/VAT/Amount, and a totals footer
that reflects the whole filtered set (not just the visible page), computed
server-side by app.repositories.sql.transaction_list.

No business logic here: every action calls PurchaseService on a
background Worker. Permission-gated actions are hidden per-row when the
session lacks the permission — a convenience, not the boundary:
PurchaseService enforces the same rule independently via
@require_permission.
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.database.errors import user_message
from app.core.exceptions import ProductNotFoundError
from app.domain.product import ProductStatus
from app.domain.purchasing import PurchaseOrderStatus
from app.schemas.product import ProductFilter
from app.schemas.purchasing import PurchaseOrderFilter
from app.schemas.reporting import ReportResult
from app.schemas.transactions import TransactionListPage, TransactionListRow, TransactionTotals
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.purchase_service import PurchaseService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.date_range_filter import DateRangeFilter
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.purchase_order_form_dialog import PurchaseOrderFormDialog
from app.ui.widgets.receive_goods_dialog import ReceiveGoodsDialog
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.totals_table import TotalsTable
from app.workers.base_worker import Worker
from app.ui.theme import scale
from app.ui.file_dialogs import ask_save_path

_COLUMNS = ["Date", "H.S Code", "Invoice No.", "Reference", "Supplier",
           "Taxable Amount (Rs)", "Non Taxable Amount (Rs)", "VAT (Rs)", "Amount (Rs)",
           "Actions"]
_STRETCH_COL = 4
_RIGHT_ALIGNED = {5, 6, 7, 8}
_FIXED = {9: 110}
_ACTIONS_COL = 9

# Column -> the PurchaseOrderFilter.sort_by key it sorts by. Sorting is
# server-side (the register is SQL-paginated) — see
# app.repositories.sql.transaction_list._PURCHASE_SORTS/_AGGREGATE_SORTS
# for the matching whitelist. Columns absent here (H.S Code, Actions)
# don't respond to a header click.
_SORTABLE_COLUMNS = {0: "created_at", 2: "invoice_number", 3: "reference_number",
                    4: "party_name", 5: "taxable_amount", 6: "non_taxable_amount",
                    7: "vat_amount", 8: "total_amount"}
_TEXT_COLUMNS = {2, 3, 4}  # default ascending; money/date columns default descending

_SEARCH_DEBOUNCE_MS = 300

_logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{value:,.2f}"


def hs_code_summary(codes: list[str]) -> tuple[str, str]:
    """(display text, tooltip) for a row's H.S Code cell. An order can span
    several products with different HS codes — this shows the first plus a
    count rather than a truncated, unreadable list.
    """
    if not codes:
        return "—", ""
    if len(codes) == 1:
        return codes[0], codes[0]
    return f"{codes[0]} +{len(codes) - 1} more", ", ".join(codes)


class PurchasesPage(QWidget):
    def __init__(self, purchase_service: PurchaseService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._purchase_service = purchase_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions

        self._page = 1
        self._sort_by = "created_at"
        self._sort_desc = True
        self._current_rows: list[TransactionListRow] = []
        self._suppliers: list = []
        self._warehouses: list = []
        self._products: list = []
        self._product_names: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Purchases", "Purchase orders, from draft through "
                                                  "goods received."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=lambda: self._purchase_service.list_purchase_transactions(self._build_filter()),
            render=self._render_table, is_empty=lambda page: len(page.items) == 0,
            empty_state=EmptyStateWidget(
                "No purchase orders found", icon="📥",
                message="Purchase orders you create will show up here."),
            error_message="Couldn't load purchase orders.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        self._load_reference_data()

    def refresh(self) -> None:
        self._async_area.reload()

    # -- permissions --------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar --------------------------------------------------------- #
    def _build_toolbar(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        outer.setContentsMargins(28, 4, 28, 12)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search order #, invoice #, reference, or supplier…")
        self._search.setMinimumWidth(scale(260))
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_debounce.timeout.connect(self._on_filter_changed)
        self._search.textChanged.connect(lambda: self._search_debounce.start())
        row1.addWidget(self._search, stretch=1)

        self._supplier_filter = QComboBox()
        self._supplier_filter.addItem("All Suppliers", None)
        self._supplier_filter.currentIndexChanged.connect(self._on_filter_changed)
        row1.addWidget(self._supplier_filter)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        for status in PurchaseOrderStatus:
            self._status_filter.addItem(status.value.replace("_", " ").title(), status)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        row1.addWidget(self._status_filter)

        self._create_button = None
        if self._can("purchases.create"):
            self._create_button = QPushButton("+ Create Purchase Order")
            self._create_button.setObjectName("primary")
            self._create_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._create_button.clicked.connect(self._open_create_dialog)
            # Suppliers/warehouses/products load asynchronously (see
            # _load_reference_data) — disabled until that finishes so a
            # click landing before the load completes can't open the
            # Create dialog with stale-empty lists.
            self._create_button.setEnabled(False)
            self._create_button.setToolTip("Loading suppliers, warehouses, and products…")
            row1.addWidget(self._create_button)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self._date_range = DateRangeFilter()
        self._date_range.changed.connect(self._on_filter_changed)
        row2.addWidget(self._date_range)
        row2.addStretch()

        export_button = QToolButton()
        export_button.setText("Export ▾")
        export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(export_button)
        export_menu.addAction("Export CSV…").triggered.connect(
            lambda: self._export("csv"))
        export_menu.addAction("Export Excel…").triggered.connect(
            lambda: self._export("excel"))
        export_button.setMenu(export_menu)
        row2.addWidget(export_button)

        print_button = QPushButton("Print")
        print_button.setObjectName("ghost")
        print_button.setCursor(Qt.CursorShape.PointingHandCursor)
        print_button.clicked.connect(self._print_register)
        row2.addWidget(print_button)
        outer.addLayout(row2)
        return outer

    def _fetch_reference_data(self):
        # Inactive suppliers/warehouses are excluded from the create-form
        # pickers — same "archived things don't populate new-transaction
        # pickers" rule already applied to products just below
        # (ProductStatus.ACTIVE). Extracted from _load_reference_data so
        # it's directly unit-testable without going through QThreadPool.
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
        self._product_names = {p.id: p.name for p in self._products}
        self._supplier_filter.blockSignals(True)
        for s in sorted(self._suppliers, key=lambda s: s.name.lower()):
            self._supplier_filter.addItem(s.name, s.id)
        self._supplier_filter.blockSignals(False)
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

    # -- data flow ----------------------------------------------------- #
    def _build_filter(self) -> PurchaseOrderFilter:
        return PurchaseOrderFilter(
            supplier_id=self._supplier_filter.currentData(),
            status=self._status_filter.currentData(),
            search=self._search.text().strip() or None,
            date_from=self._date_range.date_from(), date_to=self._date_range.date_to(),
            sort_by=self._sort_by, sort_desc=self._sort_desc,
            page=self._page, page_size=25)

    def _on_filter_changed(self) -> None:
        self._page = 1
        self.refresh()

    def _on_page_changed(self, page: int) -> None:
        self._page = page
        self.refresh()

    def _on_sort_clicked(self, column: int) -> None:
        key = _SORTABLE_COLUMNS.get(column)
        if key is None:
            return
        if key == self._sort_by:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_by = key
            self._sort_desc = column not in _TEXT_COLUMNS
        self._page = 1
        self.refresh()

    # -- table rendering ------------------------------------------------ #
    def _render_table(self, page: TransactionListPage) -> TotalsTable:
        self._current_rows = page.items
        self._pagination.set_state(page=page.page, total_pages=page.total_pages,
                                   total=page.total)

        table = TotalsTable()
        table.set_columns(_COLUMNS, stretch_column=_STRETCH_COL,
                          right_aligned=_RIGHT_ALIGNED, fixed=_FIXED)
        table.sort_requested.connect(self._on_sort_clicked)
        sort_column = next((c for c, k in _SORTABLE_COLUMNS.items() if k == self._sort_by), 0)
        table.enable_sort_indicator(sort_column, not self._sort_desc)
        table.set_row_count(len(page.items))

        for row_idx, row in enumerate(page.items):
            hs_text, hs_tooltip = hs_code_summary(row.hs_codes)
            values = [
                row.created_at.strftime("%Y-%m-%d"), hs_text, row.invoice_number or "—",
                row.reference_number or "—", row.party_name,
                _money(row.taxable_amount), _money(row.non_taxable_amount),
                _money(row.vat_amount), _money(row.total_amount),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 1 and hs_tooltip:
                    item.setToolTip(hs_tooltip)
                table.set_item(row_idx, col, item)
            table.set_cell_widget(row_idx, _ACTIONS_COL, self._build_actions_button(row))

        table.set_totals_row(self._totals_row_items(page.totals))
        return table

    def _totals_row_items(self, totals: TransactionTotals) -> list[QTableWidgetItem]:
        bold = QFont()
        bold.setBold(True)
        items = []
        for col in range(len(_COLUMNS)):
            if col == _STRETCH_COL:
                text = "Total"
            elif col == 5:
                text = _money(totals.taxable_amount)
            elif col == 6:
                text = _money(totals.non_taxable_amount)
            elif col == 7:
                text = _money(totals.vat_amount)
            elif col == 8:
                text = _money(totals.total_amount)
            else:
                text = ""
            item = QTableWidgetItem(text)
            item.setFont(bold)
            if col in _RIGHT_ALIGNED:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
        return items

    # -- per-row actions --------------------------------------------------- #
    def _build_actions_button(self, row: TransactionListRow) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 0, 8, 0)

        button = QToolButton()
        button.setText("Actions ▾")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setObjectName("rowActions")

        status = PurchaseOrderStatus(row.status)
        menu = QMenu(button)
        menu.addAction("View").triggered.connect(
            lambda checked=False, r=row: self._open_view(r))
        if self._can("purchases.update") and status == PurchaseOrderStatus.DRAFT:
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, r=row: self._open_edit(r))
        menu.addSeparator()
        if self._can("purchases.update") and status == PurchaseOrderStatus.DRAFT:
            menu.addAction("Submit").triggered.connect(
                lambda checked=False, r=row: self._submit_row(r))
        if self._can("purchases.approve") and status == PurchaseOrderStatus.SUBMITTED:
            menu.addAction("Approve").triggered.connect(
                lambda checked=False, r=row: self._approve_row(r))
        if self._can("purchases.receive") and status in (
                PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.PARTIALLY_RECEIVED):
            menu.addAction("Receive Goods").triggered.connect(
                lambda checked=False, r=row: self._receive_row(r))
        menu.addSeparator()
        menu.addAction("Print").triggered.connect(
            lambda checked=False, r=row: self._print_row(r))
        if self._can("purchases.cancel") and status in (
                PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SUBMITTED,
                PurchaseOrderStatus.APPROVED):
            menu.addSeparator()
            menu.addAction("Cancel Order").triggered.connect(
                lambda checked=False, r=row: self._cancel_row(r))

        if menu.isEmpty():
            button.setEnabled(False)
        button.setMenu(menu)
        layout.addWidget(button)
        layout.addStretch()
        return holder

    # -- view / edit -------------------------------------------------------#
    def _fetch_order_and_products(self, purchase_order_id):
        order = self._purchase_service.get_purchase_order(purchase_order_id)
        products = {}
        for item in order.items:
            try:
                products[item.product_id] = self._product_service.get_product(item.product_id)
            except ProductNotFoundError:
                continue
        return order, products

    def _open_view(self, row: TransactionListRow) -> None:
        worker = Worker(self._fetch_order_and_products, row.id)
        worker.signals.finished.connect(
            lambda result, r=row: self._on_order_loaded_for_view(result, r, read_only=True))
        worker.signals.error.connect(self._on_load_order_error)
        QThreadPool.globalInstance().start(worker)

    def _open_edit(self, row: TransactionListRow) -> None:
        worker = Worker(self._fetch_order_and_products, row.id)
        worker.signals.finished.connect(
            lambda result, r=row: self._on_order_loaded_for_view(result, r, read_only=False))
        worker.signals.error.connect(self._on_load_order_error)
        QThreadPool.globalInstance().start(worker)

    def _on_order_loaded_for_view(self, result, row: TransactionListRow, *,
                                  read_only: bool) -> None:
        order, products = result
        dialog = PurchaseOrderFormDialog(
            self._purchase_service, self._product_service, self._inventory_service,
            self._suppliers, self._warehouses, purchase_order=order,
            party_name=row.party_name, seed_products=products, read_only=read_only,
            parent=self)
        if dialog.exec() and not read_only:
            self.refresh()

    def _on_load_order_error(self, exc: Exception) -> None:
        _logger.exception("Loading purchase order failed", exc_info=exc)
        QMessageBox.critical(self, "Couldn't open order", str(exc))

    # -- lifecycle actions --------------------------------------------------#
    def _submit_row(self, row: TransactionListRow) -> None:
        self._run_action(self._purchase_service.submit_purchase_order, row.id)

    def _approve_row(self, row: TransactionListRow) -> None:
        self._run_action(self._purchase_service.approve_purchase_order, row.id)

    def _cancel_row(self, row: TransactionListRow) -> None:
        if confirm(self, "Cancel Purchase Order",
                  f"Cancel purchase order {row.invoice_number or row.id}?",
                  confirm_label="Cancel Order", danger=True):
            self._run_action(self._purchase_service.cancel_purchase_order, row.id)

    def _receive_row(self, row: TransactionListRow) -> None:
        worker = Worker(self._purchase_service.get_purchase_order, row.id)
        worker.signals.finished.connect(self._on_order_loaded_for_receive)
        worker.signals.error.connect(self._on_load_order_error)
        QThreadPool.globalInstance().start(worker)

    def _on_order_loaded_for_receive(self, order) -> None:
        dialog = ReceiveGoodsDialog(self._purchase_service, order, self._product_names,
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

    # -- print --------------------------------------------------------------#
    def _row_report_result(self, row: TransactionListRow) -> ReportResult:
        return ReportResult(
            title=f"Purchase Order {row.invoice_number or row.id}",
            generated_at=datetime.now(timezone.utc), columns=_export_columns(),
            rows=[_row_to_export_dict(row)])

    def _print_row(self, row: TransactionListRow) -> None:
        from app.reporting.export import print_report
        print_report(self._row_report_result(row), parent=self)

    def _print_register(self) -> None:
        worker = Worker(self._purchase_service.export_purchase_transactions,
                        self._build_filter())
        worker.signals.finished.connect(self._on_print_register_loaded)
        worker.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(worker)

    def _on_print_register_loaded(self, rows: list[TransactionListRow]) -> None:
        from app.reporting.export import print_report
        result = ReportResult(title="Purchase Register", generated_at=datetime.now(timezone.utc),
                              columns=_export_columns(),
                              rows=[_row_to_export_dict(r) for r in rows])
        print_report(result, parent=self)

    # -- export ---------------------------------------------------------------#
    def _export(self, fmt: str) -> None:
        """Fetch *and* write happen on the worker.

        The write used to run in the finished slot, i.e. back on the UI
        thread: a large register serialised to .xlsx and pushed to a network
        drive froze the window for as long as that took, which reads as a
        crash rather than as progress.
        """
        ext = "csv" if fmt == "csv" else "xlsx"
        filt = "CSV Files (*.csv)" if fmt == "csv" else "Excel Files (*.xlsx)"
        path = ask_save_path(self, "Export Purchases", f"purchases.{ext}", filt)
        if path is None:
            return
        worker = Worker(self._fetch_and_write_export, self._build_filter(), path, fmt)
        worker.signals.finished.connect(self._on_exported)
        worker.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(worker)

    def _fetch_and_write_export(self, filters, path: str, fmt: str) -> str:
        from app.reporting.export import export_csv, export_excel

        rows = self._purchase_service.export_purchase_transactions(filters)
        result = ReportResult(title="Purchase Register", generated_at=datetime.now(timezone.utc),
                              columns=_export_columns(),
                              rows=[_row_to_export_dict(r) for r in rows])
        return (export_csv if fmt == "csv" else export_excel)(result, path)

    def _on_exported(self, path: str) -> None:
        QMessageBox.information(self, "Export Complete", f"Purchases exported to {path}")

    def _on_export_error(self, exc: Exception) -> None:
        # Covers both halves of the worker now: reading the purchases and
        # writing the file.
        _logger.exception("Exporting purchases failed", exc_info=exc)
        QMessageBox.warning(self, "Export Failed", user_message(exc))


def _export_columns() -> list[str]:
    return ["Date", "H.S Codes", "Invoice No.", "Reference", "Supplier", "Taxable Amount",
           "Non Taxable Amount", "VAT Amount", "Amount"]


def _row_to_export_dict(row: TransactionListRow) -> dict:
    return {
        "Date": row.created_at.strftime("%Y-%m-%d"), "H.S Codes": ", ".join(row.hs_codes),
        "Invoice No.": row.invoice_number or "", "Reference": row.reference_number or "",
        "Supplier": row.party_name, "Taxable Amount": row.taxable_amount,
        "Non Taxable Amount": row.non_taxable_amount, "VAT Amount": row.vat_amount,
        "Amount": row.total_amount,
    }
