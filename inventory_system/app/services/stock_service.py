"""Application service for stock queries."""
from app.repositories.interfaces import StockRepository
from app.schemas.stock import StockLevel


class StockService:
    def __init__(self, stock: StockRepository):
        self._stock = stock

    def list_stock(self) -> list[StockLevel]:
        return self._stock.list_all()
