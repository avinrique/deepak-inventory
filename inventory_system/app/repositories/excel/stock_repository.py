"""Excel-backed StockRepository — thin adapter over the existing storage.py."""
from decimal import Decimal

import storage as db
from app.schemas.stock import StockLevel


class ExcelStockRepository:
    def on_hand(self, product: str) -> Decimal:
        return Decimal(str(db.stock_on_hand(product)))

    def adjust(self, product: str, qty: Decimal, add: bool) -> Decimal:
        return Decimal(str(db.update_stock(product, float(qty), add)))

    def product_names(self) -> list[str]:
        return db.product_names()

    def list_all(self) -> list[StockLevel]:
        rows = db.read_rows(db.STOCK_FILE, db.STOCK_HEADERS)
        return [StockLevel(product=str(r[0] or ""), quantity=Decimal(str(r[1] or 0)))
                for r in rows]
