from decimal import Decimal

from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total_products_in_stock: int
    total_parties: int
    sales_count: int
    sales_total: Decimal
    purchases_count: int
    purchases_total: Decimal
