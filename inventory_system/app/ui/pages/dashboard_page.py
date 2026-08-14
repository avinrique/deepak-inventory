"""Dashboard — real, live metrics from ReportingService (SQL-aggregated
over the Postgres domain model: Product/Inventory/PurchaseOrder/SalesOrder/
etc.), replacing the earlier Excel-summary version. Everything fetched on
a background thread through AsyncContentArea so the aggregation queries
never freeze the UI. Charts via PySide6.QtCharts — no extra dependency,
already part of the PySide6 install this project already requires.
"""
from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QPieSeries,
    QValueAxis,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.schemas.reporting import DashboardMetrics
from app.services.reporting_service import ReportingService
from app.ui.theme import ACCENT, AMBER, GREEN, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.stat_card import StatCard
from app.ui.widgets.states import EmptyStateWidget

_CHART_MIN_HEIGHT = 260


def _money(value) -> str:
    return f"{value:,.2f}"


def _qty(value) -> str:
    return f"{value:,.2f}"


def _section_label(text: str) -> QWidget:
    from PySide6.QtWidgets import QLabel
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def _empty_chart_view(title: str) -> QChartView:
    chart = QChart()
    chart.setTitle(title)
    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(_CHART_MIN_HEIGHT)
    return view


def _line_chart(title: str, categories: list[str], values: list[float]) -> QChartView:
    if not values:
        return _empty_chart_view(title)
    series = QLineSeries()
    for i, v in enumerate(values):
        series.append(float(i), v)

    chart = QChart()
    chart.addSeries(series)
    chart.setTitle(title)
    chart.legend().hide()

    axis_x = QBarCategoryAxis()
    axis_x.append(categories)
    chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
    series.attachAxis(axis_x)

    axis_y = QValueAxis()
    top = max(values) if values else 1.0
    axis_y.setRange(0, top * 1.15 if top else 1.0)
    chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
    series.attachAxis(axis_y)

    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(_CHART_MIN_HEIGHT)
    return view


def _bar_chart(title: str, categories: list[str], values: list[float],
               series_name: str = "") -> QChartView:
    if not values:
        return _empty_chart_view(title)
    bar_set = QBarSet(series_name)
    bar_set.append(values)
    series = QBarSeries()
    series.append(bar_set)

    chart = QChart()
    chart.addSeries(series)
    chart.setTitle(title)
    chart.legend().hide()

    axis_x = QBarCategoryAxis()
    axis_x.append(categories)
    chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
    series.attachAxis(axis_x)

    axis_y = QValueAxis()
    top = max(values) if values else 1.0
    axis_y.setRange(0, top * 1.15 if top else 1.0)
    chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
    series.attachAxis(axis_y)

    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(_CHART_MIN_HEIGHT)
    return view


def _pie_chart(title: str, labels: list[str], values: list[float]) -> QChartView:
    if not values or not any(values):
        return _empty_chart_view(title)
    series = QPieSeries()
    for label, value in zip(labels, values):
        if value <= 0:
            continue
        series.append(f"{label} ({value:,.0f})", value)

    chart = QChart()
    chart.addSeries(series)
    chart.setTitle(title)
    chart.legend().setAlignment(Qt.AlignmentFlag.AlignRight)

    view = QChartView(chart)
    view.setRenderHint(QPainter.RenderHint.Antialiasing)
    view.setMinimumHeight(_CHART_MIN_HEIGHT)
    return view


class DashboardPage(QWidget):
    def __init__(self, reporting_service: ReportingService):
        super().__init__()
        self._reporting_service = reporting_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Dashboard", "An overview of your business right now."))

        self._async_area = AsyncContentArea(
            load=self._reporting_service.get_dashboard_metrics,
            render=self._render_metrics,
            is_empty=lambda m: (m.total_products == 0 and m.total_inventory_units == 0
                               and not m.recent_transactions),
            empty_state=EmptyStateWidget(
                "Nothing recorded yet", icon="📊",
                message="Once products, sales, or purchases exist, they'll summarize here."),
            error_message="Couldn't load the dashboard.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    @staticmethod
    def _render_metrics(metrics: DashboardMetrics) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(28, 12, 28, 28)
        outer.setSpacing(20)

        # -- KPI cards ------------------------------------------------- #
        cards = QGridLayout()
        cards.setSpacing(16)
        card_defs = [
            ("Products", str(metrics.total_products), "📦", ACCENT),
            ("Inventory Units", _qty(metrics.total_inventory_units), "📊", ACCENT),
            ("Inventory Value", _money(metrics.inventory_value), "💰", GREEN),
            ("Low Stock", str(metrics.low_stock_count), "⚠️", AMBER),
            ("Total Sales", _money(metrics.total_sales), "📤", GREEN),
            ("Total Purchases", _money(metrics.total_purchases), "📥", RED),
            ("Outstanding Payments", _money(metrics.outstanding_payments), "🧾", AMBER),
        ]
        for i, (label, value, icon, accent) in enumerate(card_defs):
            cards.addWidget(StatCard(label, value, icon, accent), i // 4, i % 4)
        outer.addLayout(cards)

        # -- charts ------------------------------------------------------#
        outer.addWidget(_section_label("TRENDS & BREAKDOWN"))
        charts = QGridLayout()
        charts.setSpacing(16)

        sales_categories = [p.bucket.strftime("%b %d") for p in metrics.sales_trend]
        sales_values = [float(p.total) for p in metrics.sales_trend]
        charts.addWidget(_line_chart("Sales Over Time", sales_categories, sales_values), 0, 0)

        purchase_categories = [p.bucket.strftime("%b %d") for p in metrics.purchase_trend]
        purchase_values = [float(p.total) for p in metrics.purchase_trend]
        charts.addWidget(_line_chart("Purchases Over Time", purchase_categories,
                                     purchase_values), 0, 1)

        top_categories = [p.sku for p in metrics.top_selling_products]
        top_values = [float(p.quantity_sold) for p in metrics.top_selling_products]
        charts.addWidget(_bar_chart("Top Products (by quantity sold)", top_categories,
                                    top_values, "Quantity Sold"), 1, 0)

        category_labels = [c.category_name for c in metrics.inventory_by_category]
        category_values = [float(c.value) for c in metrics.inventory_by_category]
        charts.addWidget(_pie_chart("Inventory Value by Category", category_labels,
                                    category_values), 1, 1)

        outer.addLayout(charts)

        # -- recent transactions ------------------------------------------#
        outer.addWidget(_section_label("RECENT TRANSACTIONS"))
        table = QTableWidget()
        table.setObjectName("card")
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(
            ["Date", "Type", "Product", "Warehouse", "Qty Change", "By"])
        table.setRowCount(len(metrics.recent_transactions))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, tx in enumerate(metrics.recent_transactions):
            table.setItem(row, 0, QTableWidgetItem(tx.created_at.strftime("%Y-%m-%d %H:%M")))
            table.setItem(row, 1, QTableWidgetItem(tx.transaction_type))
            table.setItem(row, 2, QTableWidgetItem(f"{tx.product_sku} — {tx.product_name}"))
            table.setItem(row, 3, QTableWidgetItem(tx.warehouse_code))
            qty_item = QTableWidgetItem(_qty(tx.quantity_change))
            qty_item.setForeground(Qt.GlobalColor.darkGreen if tx.quantity_change >= 0
                                   else Qt.GlobalColor.darkRed)
            table.setItem(row, 4, qty_item)
            table.setItem(row, 5, QTableWidgetItem(tx.performed_by_email))
        table.setMinimumHeight(min(400, 44 + 32 * max(1, len(metrics.recent_transactions))))
        if not metrics.recent_transactions:
            table.setRowCount(0)
        outer.addWidget(table)

        outer.addStretch()
        scroll.setWidget(container)
        return scroll
