"""Sales orders page — the sales register: search/date-range/status/sort/
paginate over SalesService.list_sales_transactions, with a Create dialog
and per-row View/Edit/workflow/Invoice/Payment/Return/Print actions.
Replaces the previous minimal 5-column list (Order Date/Customer/
Warehouse/Status/Total) with the full register the accounting side needs —
Date, H.S Code, Invoice No., Reference, Customer,
Taxable/Non-Taxable/VAT/Amount, and a totals footer that reflects the
whole filtered set (not just the visible page), computed server-side by
app.repositories.sql.transaction_list.

No business logic here: every action calls SalesService on a background
Worker and renders whatever comes back — the outstanding-balance check on
a payment, the over-return check, and the FULFILLED/COMPLETED-only
eligibility for invoicing/payment/returns are all enforced independently
by the service layer; menu-item visibility here is a convenience, not the
boundary.
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import DuplicateInvoiceError, ProductNotFoundError
from app.domain.product import ProductStatus
from app.domain.sales import SalesOrderStatus
from app.schemas.product import ProductFilter
from app.schemas.reporting import ReportResult
from app.schemas.sales import InvoiceOut, SalesOrderFilter
from app.schemas.transactions import TransactionListPage, TransactionListRow, TransactionTotals
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.sales_service import SalesService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.date_range_filter import DateRangeFilter
from app.ui.widgets.invoice_preview_dialog import InvoicePreviewDialog
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.record_payment_dialog import RecordPaymentDialog
from app.ui.widgets.sales_order_form_dialog import SalesOrderFormDialog
from app.ui.widgets.sales_return_dialog import SalesReturnDialog
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.totals_table import TotalsTable
from app.workers.base_worker import Worker

_COLUMNS = ["Date", "H.S Code", "Invoice No.", "Reference", "Customer",
           "Taxable Amount (Rs)", "Non Taxable Amount (Rs)", "VAT (Rs)", "Amount (Rs)",
           "Actions"]
_STRETCH_COL = 4
_RIGHT_ALIGNED = {5, 6, 7, 8}
_FIXED = {9: 110}
_ACTIONS_COL = 9

_SORTABLE_COLUMNS = {0: "created_at", 2: "invoice_number", 3: "reference_number",
                    4: "party_name", 5: "taxable_amount", 6: "non_taxable_amount",
                    7: "vat_amount", 8: "total_amount"}
_TEXT_COLUMNS = {2, 3, 4}

_INVOICEABLE_STATUSES = {SalesOrderStatus.FULFILLED, SalesOrderStatus.COMPLETED}

_SEARCH_DEBOUNCE_MS = 300

_logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{value:,.2f}"


def hs_code_summary(codes: list[str]) -> tuple[str, str]:
    """(display text, tooltip) for a row's H.S Code cell — see
    app.ui.pages.purchases_page.hs_code_summary, identical rule.
    """
    if not codes:
        return "—", ""
    if len(codes) == 1:
        return codes[0], codes[0]
    return f"{codes[0]} +{len(codes) - 1} more", ", ".join(codes)


class SalesOrdersPage(QWidget):
    def __init__(self, sales_service: SalesService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._sales_service = sales_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions

        self._page = 1
        self._sort_by = "created_at"
        self._sort_desc = True
        self._current_rows: list[TransactionListRow] = []
        self._customers: list = []
        self._warehouses: list = []
        self._products: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Sales", "Sales orders, from draft through payment."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=lambda: self._sales_service.list_sales_transactions(self._build_filter()),
            render=self._render_table, is_empty=lambda page: len(page.items) == 0,
            empty_state=EmptyStateWidget(
                "No sales orders found", icon="📤",
                message="Sales orders you create will show up here."),
            error_message="Couldn't load sales orders.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        self._load_filter_options()

    def refresh(self) -> None:
        self._async_area.reload()

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ---------------------------------------------------------#
    def _build_toolbar(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        outer.setContentsMargins(28, 4, 28, 12)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search invoice #, reference, or customer…")
        self._search.setMinimumWidth(260)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_debounce.timeout.connect(self._on_filter_changed)
        self._search.textChanged.connect(lambda: self._search_debounce.start())
        row1.addWidget(self._search, stretch=1)

        self._customer_filter = QComboBox()
        self._customer_filter.addItem("All Customers", None)
        self._customer_filter.currentIndexChanged.connect(self._on_filter_changed)
        row1.addWidget(self._customer_filter)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        for status in SalesOrderStatus:
            self._status_filter.addItem(status.value.title(), status)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        row1.addWidget(self._status_filter)

        self._create_button = None
        if self._can("sales.create"):
            self._create_button = QPushButton("+ Create Sales Order")
            self._create_button.setObjectName("primary")
            self._create_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._create_button.clicked.connect(self._open_create_dialog)
            self._create_button.setEnabled(False)
            self._create_button.setToolTip("Loading customers, warehouses, and products…")
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
        export_menu.addAction("Export CSV…").triggered.connect(lambda: self._export("csv"))
        export_menu.addAction("Export Excel…").triggered.connect(lambda: self._export("excel"))
        export_button.setMenu(export_menu)
        row2.addWidget(export_button)

        print_button = QPushButton("Print")
        print_button.setObjectName("ghost")
        print_button.setCursor(Qt.CursorShape.PointingHandCursor)
        print_button.clicked.connect(self._print_register)
        row2.addWidget(print_button)
        outer.addLayout(row2)
        return outer

    def _fetch_filter_options(self):
        # Inactive customers/warehouses are excluded from the create-form
        # pickers — same "archived things don't populate new-transaction
        # pickers" rule already applied to products just below
        # (ProductStatus.ACTIVE). Extracted from _load_filter_options so
        # it's directly unit-testable without going through QThreadPool.
        customers = [c for c in self._sales_service.list_customers() if c.is_active]
        warehouses = [w for w in self._inventory_service.list_warehouses() if w.is_active]
        products = self._product_service.search_products(
            ProductFilter(status=ProductStatus.ACTIVE, page_size=500)).items
        return customers, warehouses, products

    def _load_filter_options(self) -> None:
        worker = Worker(self._fetch_filter_options)
        worker.signals.finished.connect(self._on_filter_options_loaded)
        worker.signals.error.connect(self._on_filter_options_error)
        QThreadPool.globalInstance().start(worker)

    def _on_filter_options_loaded(self, result) -> None:
        customers, warehouses, products = result
        self._customers, self._warehouses, self._products = customers, warehouses, products
        self._customer_filter.blockSignals(True)
        for customer in sorted(customers, key=lambda c: c.name.lower()):
            self._customer_filter.addItem(customer.name, customer.id)
        self._customer_filter.blockSignals(False)
        if self._create_button is not None:
            self._create_button.setEnabled(True)
            self._create_button.setToolTip("")

    def _on_filter_options_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load sales page filter options", exc_info=exc)
        if self._create_button is not None:
            self._create_button.setEnabled(True)
            self._create_button.setToolTip("Couldn't load customers/warehouses/products — "
                                           "the form may be missing options. Try again.")

    # -- create ----------------------------------------------------------- #
    def _open_create_dialog(self) -> None:
        dialog = SalesOrderFormDialog(self._sales_service, self._product_service,
                                      self._inventory_service, self._customers,
                                      self._warehouses, parent=self)
        if dialog.exec():
            self.refresh()

    # -- data flow ----------------------------------------------------- #
    def _build_filter(self) -> SalesOrderFilter:
        return SalesOrderFilter(
            customer_id=self._customer_filter.currentData(),
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
                if col == 8 and row.excise_amount:
                    item.setToolTip(
                        f"Taxable {_money(row.taxable_amount)} + Non-taxable "
                        f"{_money(row.non_taxable_amount)} + VAT {_money(row.vat_amount)} "
                        f"+ Excise {_money(row.excise_amount)}")
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
            if col == 8 and totals.excise_amount:
                item.setToolTip(f"Includes excise {_money(totals.excise_amount)}")
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

        status = SalesOrderStatus(row.status)
        menu = QMenu(button)
        menu.addAction("View").triggered.connect(
            lambda checked=False, r=row: self._open_view(r))
        if self._can("sales.update") and status == SalesOrderStatus.DRAFT:
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, r=row: self._open_edit(r))
        menu.addSeparator()
        if self._can("sales.confirm") and status == SalesOrderStatus.DRAFT:
            menu.addAction("Confirm").triggered.connect(
                lambda checked=False, r=row: self._confirm_row(r))
        if self._can("sales.fulfill") and status == SalesOrderStatus.CONFIRMED:
            menu.addAction("Fulfill").triggered.connect(
                lambda checked=False, r=row: self._fulfill_row(r))
        if status in _INVOICEABLE_STATUSES:
            menu.addSeparator()
            menu.addAction("View Invoice").triggered.connect(
                lambda checked=False, r=row: self._view_invoice_row(r))
            if self._can("sales.invoice"):
                menu.addAction("Generate Invoice").triggered.connect(
                    lambda checked=False, r=row: self._generate_invoice_row(r))
            if self._can("sales.payment"):
                menu.addAction("Record Payment").triggered.connect(
                    lambda checked=False, r=row: self._record_payment_row(r))
            if self._can("sales.refund"):
                menu.addAction("Return").triggered.connect(
                    lambda checked=False, r=row: self._return_row(r))
        menu.addSeparator()
        menu.addAction("Print").triggered.connect(
            lambda checked=False, r=row: self._print_row(r))
        if self._can("sales.cancel") and status in (SalesOrderStatus.DRAFT,
                                                     SalesOrderStatus.CONFIRMED):
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
    def _fetch_order_and_products(self, sales_order_id):
        order = self._sales_service.get_sales_order(sales_order_id)
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
        dialog = SalesOrderFormDialog(
            self._sales_service, self._product_service, self._inventory_service,
            self._customers, self._warehouses, sales_order=order, party_name=row.party_name,
            invoice_number=row.invoice_number, seed_products=products, read_only=read_only,
            parent=self)
        if dialog.exec() and not read_only:
            self.refresh()

    def _on_load_order_error(self, exc: Exception) -> None:
        _logger.exception("Loading sales order failed", exc_info=exc)
        QMessageBox.critical(self, "Couldn't open order", str(exc))

    # -- lifecycle actions ------------------------------------------------ #
    def _confirm_row(self, row: TransactionListRow) -> None:
        self._run_action(self._sales_service.confirm_sales_order, row.id)

    def _fulfill_row(self, row: TransactionListRow) -> None:
        self._run_action(self._sales_service.fulfill_sale, row.id)

    def _cancel_row(self, row: TransactionListRow) -> None:
        if confirm(self, "Cancel Sales Order", "Cancel this sales order?",
                  confirm_label="Cancel Order", danger=True):
            self._run_action(self._sales_service.cancel_sales_order, row.id)

    def _run_action(self, fn, sales_order_id) -> None:
        worker = Worker(fn, sales_order_id)
        worker.signals.finished.connect(lambda _: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Sales order action failed", exc_info=exc)
        QMessageBox.critical(self, "Action failed", str(exc))

    # -- view invoice --------------------------------------------------- #
    def _view_invoice_row(self, row: TransactionListRow) -> None:
        worker = Worker(self._sales_service.get_invoice_by_sales_order, row.id)
        worker.signals.finished.connect(self._on_invoice_looked_up)
        worker.signals.error.connect(self._on_invoice_lookup_error)
        QThreadPool.globalInstance().start(worker)

    def _on_invoice_looked_up(self, invoice) -> None:
        if invoice is None:
            QMessageBox.information(self, "No invoice yet",
                                   "This order doesn't have an invoice generated yet.")
            return
        dialog = InvoicePreviewDialog(self._sales_service, invoice.id, parent=self)
        dialog.exec()

    def _on_invoice_lookup_error(self, exc: Exception) -> None:
        _logger.exception("Looking up invoice for sales order failed", exc_info=exc)
        QMessageBox.critical(self, "Couldn't open invoice", str(exc))

    # -- generate invoice -------------------------------------------------- #
    def _generate_invoice_row(self, row: TransactionListRow) -> None:
        worker = Worker(self._sales_service.generate_invoice, row.id)
        worker.signals.finished.connect(self._on_invoice_generated)
        worker.signals.error.connect(self._on_generate_invoice_error)
        QThreadPool.globalInstance().start(worker)

    def _on_invoice_generated(self, invoice: InvoiceOut) -> None:
        self.refresh()
        dialog = InvoicePreviewDialog(self._sales_service, invoice.id, parent=self)
        dialog.exec()

    def _on_generate_invoice_error(self, exc: Exception) -> None:
        _logger.exception("Generating invoice failed", exc_info=exc)
        if isinstance(exc, DuplicateInvoiceError):
            QMessageBox.information(self, "Invoice already exists",
                                    "This order already has an invoice — use View Invoice "
                                    "to see it.")
        else:
            QMessageBox.critical(self, "Couldn't generate invoice", str(exc))

    # -- record payment ------------------------------------------------------#
    def _record_payment_row(self, row: TransactionListRow) -> None:
        worker = Worker(self._sales_service.get_invoice_by_sales_order, row.id)
        worker.signals.finished.connect(self._on_invoice_looked_up_for_payment)
        worker.signals.error.connect(self._on_payment_invoice_lookup_error)
        QThreadPool.globalInstance().start(worker)

    def _on_invoice_looked_up_for_payment(self, invoice) -> None:
        if invoice is None:
            QMessageBox.information(self, "No invoice yet",
                                   "Generate an invoice for this order before recording a "
                                   "payment.")
            return
        dialog = RecordPaymentDialog(self._sales_service, invoice.id, parent=self)
        if dialog.exec():
            self.refresh()

    def _on_payment_invoice_lookup_error(self, exc: Exception) -> None:
        _logger.exception("Looking up invoice for payment failed", exc_info=exc)
        QMessageBox.critical(self, "Couldn't record payment", str(exc))

    # -- returns ------------------------------------------------------------#
    def _return_row(self, row: TransactionListRow) -> None:
        worker = Worker(self._sales_service.get_sales_order, row.id)
        worker.signals.finished.connect(self._on_order_loaded_for_return)
        worker.signals.error.connect(self._on_load_order_error)
        QThreadPool.globalInstance().start(worker)

    def _on_order_loaded_for_return(self, order) -> None:
        dialog = SalesReturnDialog(self._sales_service, order, self._products, parent=self)
        if dialog.exec():
            self.refresh()

    # -- print --------------------------------------------------------------#
    def _print_row(self, row: TransactionListRow) -> None:
        if row.invoice_number:
            worker = Worker(self._sales_service.get_invoice_by_sales_order, row.id)
            worker.signals.finished.connect(self._on_invoice_looked_up_for_print)
            worker.signals.error.connect(self._on_invoice_lookup_error)
            QThreadPool.globalInstance().start(worker)
            return
        from app.reporting.export import print_report
        result = ReportResult(title=f"Sales Order {row.id}",
                              generated_at=datetime.now(timezone.utc),
                              columns=_export_columns(), rows=[_row_to_export_dict(row)])
        print_report(result, parent=self)

    def _on_invoice_looked_up_for_print(self, invoice) -> None:
        if invoice is None:
            return
        dialog = InvoicePreviewDialog(self._sales_service, invoice.id, parent=self)
        dialog.exec()

    def _print_register(self) -> None:
        worker = Worker(self._sales_service.export_sales_transactions, self._build_filter())
        worker.signals.finished.connect(self._on_print_register_loaded)
        worker.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(worker)

    def _on_print_register_loaded(self, rows: list[TransactionListRow]) -> None:
        from app.reporting.export import print_report
        result = ReportResult(title="Sales Register", generated_at=datetime.now(timezone.utc),
                              columns=_export_columns(),
                              rows=[_row_to_export_dict(r) for r in rows])
        print_report(result, parent=self)

    # -- export ---------------------------------------------------------------#
    def _export(self, fmt: str) -> None:
        ext = "csv" if fmt == "csv" else "xlsx"
        filt = "CSV Files (*.csv)" if fmt == "csv" else "Excel Files (*.xlsx)"
        path, _ = QFileDialog.getSaveFileName(self, "Export Sales", f"sales.{ext}", filt)
        if not path:
            return
        worker = Worker(self._sales_service.export_sales_transactions, self._build_filter())
        worker.signals.finished.connect(lambda rows: self._on_export_loaded(rows, path, fmt))
        worker.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(worker)

    def _on_export_loaded(self, rows: list[TransactionListRow], path: str, fmt: str) -> None:
        from app.reporting.export import export_csv, export_excel
        result = ReportResult(title="Sales Register", generated_at=datetime.now(timezone.utc),
                              columns=_export_columns(),
                              rows=[_row_to_export_dict(r) for r in rows])
        try:
            (export_csv if fmt == "csv" else export_excel)(result, path)
            QMessageBox.information(self, "Export Complete", f"Sales exported to {path}")
        except OSError as exc:
            _logger.exception("Sales export failed", exc_info=exc)
            QMessageBox.warning(self, "Export Failed", "Couldn't write the export file.")

    def _on_export_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load sales for export", exc_info=exc)
        QMessageBox.warning(self, "Export Failed", "Couldn't load sales to export.")


def _export_columns() -> list[str]:
    return ["Date", "H.S Codes", "Invoice No.", "Reference", "Customer", "Taxable Amount",
           "Non Taxable Amount", "VAT Amount", "Excise Amount", "Amount"]


def _row_to_export_dict(row: TransactionListRow) -> dict:
    return {
        "Date": row.created_at.strftime("%Y-%m-%d"), "H.S Codes": ", ".join(row.hs_codes),
        "Invoice No.": row.invoice_number or "", "Reference": row.reference_number or "",
        "Customer": row.party_name, "Taxable Amount": row.taxable_amount,
        "Non Taxable Amount": row.non_taxable_amount, "VAT Amount": row.vat_amount,
        "Excise Amount": row.excise_amount, "Amount": row.total_amount,
    }
