import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class ReportFilter(BaseModel):
    """Every field optional; each report applies whichever subset is
    semantically relevant to it and ignores the rest (e.g. a Supplier
    report has no product_id to filter on) — see
    app.repositories.sql.reporting_repository for which filters each
    report method actually reads.
    """
    date_from: date | None = None
    date_to: date | None = None
    warehouse_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None


class TrendPoint(BaseModel):
    bucket: date
    total: Decimal
    count: int


class TopProductRow(BaseModel):
    product_id: uuid.UUID
    sku: str
    name: str
    quantity_sold: Decimal
    revenue: Decimal


class RecentTransactionRow(BaseModel):
    id: uuid.UUID
    transaction_type: str
    product_sku: str
    product_name: str
    warehouse_code: str
    quantity_change: Decimal
    performed_by_email: str
    created_at: datetime


class CategoryValueRow(BaseModel):
    category_id: uuid.UUID | None
    category_name: str
    quantity_on_hand: Decimal
    value: Decimal


class DashboardMetrics(BaseModel):
    total_products: int
    total_inventory_units: Decimal
    inventory_value: Decimal
    low_stock_count: int
    total_sales: Decimal
    total_purchases: Decimal
    outstanding_payments: Decimal
    recent_transactions: list[RecentTransactionRow]
    top_selling_products: list[TopProductRow]
    sales_trend: list[TrendPoint]
    purchase_trend: list[TrendPoint]
    inventory_by_category: list[CategoryValueRow]


class ReportResult(BaseModel):
    """Generic tabular result shared by all nine exportable reports — one
    shape so app.reporting.export (CSV/Excel/PDF/print) has a single thing
    to render instead of nine bespoke exporters. ``rows`` values are kept
    as native Python types (Decimal, date, str, int) rather than pre-
    formatted strings — export.py decides formatting per output format.
    """
    title: str
    generated_at: datetime
    columns: list[str]
    rows: list[dict[str, Any]]

    @property
    def row_count(self) -> int:
        return len(self.rows)
