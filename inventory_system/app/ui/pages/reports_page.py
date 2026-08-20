"""Reports page — pick one of the nine report types, narrow it with the
shared filter panel (date range / warehouse / product / category /
supplier / customer — every report ignores whichever of these it isn't
relevant to, see app.repositories.sql.reporting_repository), preview it in
a table, then export to CSV/Excel/PDF or send it to the printer.

No business logic here: every report call goes through ReportingService on
a background Worker. CSV/Excel/PDF export and Print run synchronously on
the click that triggers them — PDF/Print specifically build a QTextDocument
and must run on the UI thread (Qt GUI objects aren't thread-safe), and
CSV/Excel are fast enough locally that a background thread would add
complexity without a real responsiveness benefit.
"""
import logging

from PySide6.QtCore import QDate, QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.reporting.export import _format_cell, export_csv, export_excel, export_pdf, print_report
from app.schemas.product import ProductFilter
from app.schemas.reporting import ReportFilter, ReportResult
from app.services.catalog_service import CatalogService
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.purchase_service import PurchaseService
from app.services.reporting_service import ReportingService
from app.services.sales_service import SalesService
from app.ui.theme import MUTED
from app.ui.widgets.page_header import PageHeader
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

_REPORTS = [
    ("stock", "Stock Report", "stock_report"),
    ("sales", "Sales Report", "sales_report"),
    ("purchase", "Purchase Report", "purchase_report"),
    ("profit", "Profit Report", "profit_report"),
    ("low_stock", "Low Stock Report", "low_stock_report"),
    ("supplier", "Supplier Report", "supplier_report"),
    ("customer", "Customer Report", "customer_report"),
    ("product_movement", "Product Movement Report", "product_movement_report"),
    ("inventory_valuation", "Inventory Valuation Report", "inventory_valuation_report"),
]
_FILTER_OPTIONS_PAGE_SIZE = 1000


class ReportsPage(QWidget):
    def __init__(self, reporting_service: ReportingService, product_service: ProductService,
                catalog_service: CatalogService, inventory_service: InventoryService,
                purchase_service: PurchaseService, sales_service: SalesService):
        super().__init__()
        self._reporting_service = reporting_service
        self._product_service = product_service
        self._catalog_service = catalog_service
        self._inventory_service = inventory_service
        self._purchase_service = purchase_service
        self._sales_service = sales_service
        self._current_result: ReportResult | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Reports", "Stock, sales, purchase, and financial reports."))

        body = QWidget()
        body.setContentsMargins(28, 12, 28, 24)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 12, 28, 24)
        body_layout.setSpacing(16)

        body_layout.addWidget(self._build_filter_panel())

        actions = QHBoxLayout()
        self._preview_button = QPushButton("Preview")
        self._preview_button.setObjectName("primary")
        self._preview_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._preview_button.clicked.connect(self._run_preview)
        actions.addWidget(self._preview_button)
        actions.addStretch()
        for label, handler in [("Export CSV", self._export_csv), ("Export Excel",
                               self._export_excel), ("Export PDF", self._export_pdf),
                              ("Print", self._print)]:
            button = QPushButton(label)
            button.setObjectName("ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            actions.addWidget(button)
        body_layout.addLayout(actions)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        body_layout.addWidget(self._status_label)

        self._table = QTableWidget()
        self._table.setObjectName("card")
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        # Reports have different column sets per report type, so (unlike
        # every other table in this app) there's no single column to
        # dedicate Stretch to — Stretch on the whole header forces every
        # column to equal, non-interactively-resizable width, truncating
        # long text columns (product/customer names, emails) with no way
        # to recover them. Interactive + a one-time content-sized initial
        # width (done in _render_table, once columns are known) + the last
        # column absorbing any remaining space gives sensible defaults
        # while keeping every column user-resizable.
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        body_layout.addWidget(self._table, stretch=1)

        layout.addWidget(body, stretch=1)

        self._load_filter_options()

    def refresh(self) -> None:
        self._run_preview()

    # -- filter panel ------------------------------------------------------#
    def _build_filter_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("card")
        grid = QGridLayout(panel)
        grid.setContentsMargins(20, 16, 20, 16)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("Report"), 0, 0)
        self._report_combo = QComboBox()
        for _key, label, _method in _REPORTS:
            self._report_combo.addItem(label)
        grid.addWidget(self._report_combo, 1, 0)

        grid.addWidget(QLabel("Date Range"), 0, 1)
        date_row = QHBoxLayout()
        self._date_enabled = QCheckBox("Filter")
        date_row.addWidget(self._date_enabled)
        self._date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self._date_from.setCalendarPopup(True)
        self._date_from.setEnabled(False)
        self._date_to = QDateEdit(QDate.currentDate())
        self._date_to.setCalendarPopup(True)
        self._date_to.setEnabled(False)
        self._date_enabled.toggled.connect(self._date_from.setEnabled)
        self._date_enabled.toggled.connect(self._date_to.setEnabled)
        date_row.addWidget(self._date_from)
        date_row.addWidget(QLabel("to"))
        date_row.addWidget(self._date_to)
        grid.addLayout(date_row, 1, 1)

        grid.addWidget(QLabel("Warehouse"), 0, 2)
        self._warehouse_combo = QComboBox()
        self._warehouse_combo.addItem("All Warehouses", None)
        grid.addWidget(self._warehouse_combo, 1, 2)

        grid.addWidget(QLabel("Category"), 2, 0)
        self._category_combo = QComboBox()
        self._category_combo.addItem("All Categories", None)
        grid.addWidget(self._category_combo, 3, 0)

        grid.addWidget(QLabel("Product"), 2, 1)
        self._product_combo = QComboBox()
        self._product_combo.addItem("All Products", None)
        grid.addWidget(self._product_combo, 3, 1)

        grid.addWidget(QLabel("Supplier"), 2, 2)
        self._supplier_combo = QComboBox()
        self._supplier_combo.addItem("All Suppliers", None)
        grid.addWidget(self._supplier_combo, 3, 2)

        grid.addWidget(QLabel("Customer"), 2, 3)
        self._customer_combo = QComboBox()
        self._customer_combo.addItem("All Customers", None)
        grid.addWidget(self._customer_combo, 3, 3)

        return panel

    def _load_filter_options(self) -> None:
        def load():
            warehouses = self._inventory_service.list_warehouses()
            categories = self._catalog_service.list_categories()
            products = self._product_service.search_products(
                ProductFilter(page_size=_FILTER_OPTIONS_PAGE_SIZE))
            suppliers = self._purchase_service.list_suppliers()
            customers = self._sales_service.list_customers()
            return warehouses, categories, products, suppliers, customers

        worker = Worker(load)
        worker.signals.finished.connect(self._on_filter_options_loaded)
        worker.signals.error.connect(self._on_filter_options_error)
        QThreadPool.globalInstance().start(worker)

    def _on_filter_options_loaded(self, result) -> None:
        warehouses, categories, product_page, suppliers, customers = result
        for w in warehouses:
            self._warehouse_combo.addItem(f"{w.code} — {w.name}", w.id)
        for c in categories:
            self._category_combo.addItem(c.name, c.id)
        for p in product_page.items:
            self._product_combo.addItem(f"{p.sku} — {p.name}", p.id)
        for s in suppliers:
            self._supplier_combo.addItem(s.name, s.id)
        for cust in customers:
            self._customer_combo.addItem(cust.name, cust.id)

    def _on_filter_options_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load report filter options", exc_info=exc)
        self._status_label.setText("Couldn't load filter options (warehouses/products/"
                                   "categories/suppliers/customers). Filters may be incomplete.")

    def _current_filter(self) -> ReportFilter:
        return ReportFilter(
            date_from=(self._date_from.date().toPython() if self._date_enabled.isChecked()
                      else None),
            date_to=(self._date_to.date().toPython() if self._date_enabled.isChecked()
                    else None),
            warehouse_id=self._warehouse_combo.currentData(),
            product_id=self._product_combo.currentData(),
            category_id=self._category_combo.currentData(),
            supplier_id=self._supplier_combo.currentData(),
            customer_id=self._customer_combo.currentData())

    # -- preview -------------------------------------------------------- #
    def _run_preview(self) -> None:
        _key, _label, method_name = _REPORTS[self._report_combo.currentIndex()]
        method = getattr(self._reporting_service, method_name)
        filter = self._current_filter()

        self._preview_button.setEnabled(False)
        self._status_label.setText("Loading…")
        worker = Worker(method, filter)
        worker.signals.finished.connect(self._on_report_loaded)
        worker.signals.error.connect(self._on_report_error)
        QThreadPool.globalInstance().start(worker)

    def _on_report_loaded(self, result: ReportResult) -> None:
        self._preview_button.setEnabled(True)
        self._current_result = result
        self._render_table(result)
        if result.row_count == 0:
            self._status_label.setText(f"{result.title} — no matching data.")
        else:
            generated = result.generated_at.strftime("%Y-%m-%d %H:%M")
            self._status_label.setText(
                f"{result.title} — {result.row_count} row(s), generated {generated}")

    def _on_report_error(self, exc: Exception) -> None:
        self._preview_button.setEnabled(True)
        _logger.exception("Report preview failed", exc_info=exc)
        self._status_label.setText("Couldn't generate this report — see logs for details.")
        self._table.setRowCount(0)
        self._table.setColumnCount(0)

    def _render_table(self, result: ReportResult) -> None:
        self._table.setColumnCount(len(result.columns))
        self._table.setHorizontalHeaderLabels(result.columns)
        self._table.setRowCount(len(result.rows))
        for row_idx, row in enumerate(result.rows):
            for col_idx, column in enumerate(result.columns):
                value = row.get(column)
                self._table.setItem(row_idx, col_idx, QTableWidgetItem(_format_cell(value)))
        self._table.resizeColumnsToContents()

    # -- export / print --------------------------------------------------#
    def _require_result(self) -> ReportResult | None:
        if self._current_result is None:
            QMessageBox.information(self, "Nothing to export",
                                   "Preview a report first.")
            return None
        return self._current_result

    def _export_csv(self) -> None:
        result = self._require_result()
        if result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", f"{result.title}.csv",
                                              "CSV Files (*.csv)")
        if not path:
            return
        try:
            export_csv(result, path)
            self._status_label.setText(f"Exported to {path}")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            _logger.exception("CSV export failed", exc_info=exc)
            QMessageBox.critical(self, "Export failed", str(exc))

    def _export_excel(self) -> None:
        result = self._require_result()
        if result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Excel", f"{result.title}.xlsx",
                                              "Excel Files (*.xlsx)")
        if not path:
            return
        try:
            export_excel(result, path)
            self._status_label.setText(f"Exported to {path}")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Excel export failed", exc_info=exc)
            QMessageBox.critical(self, "Export failed", str(exc))

    def _export_pdf(self) -> None:
        result = self._require_result()
        if result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export PDF", f"{result.title}.pdf",
                                              "PDF Files (*.pdf)")
        if not path:
            return
        try:
            export_pdf(result, path)
            self._status_label.setText(f"Exported to {path}")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("PDF export failed", exc_info=exc)
            QMessageBox.critical(self, "Export failed", str(exc))

    def _print(self) -> None:
        result = self._require_result()
        if result is None:
            return
        try:
            if print_report(result, self):
                self._status_label.setText("Sent to printer.")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Print failed", exc_info=exc)
            QMessageBox.critical(self, "Print failed", str(exc))
