"""SQLAlchemy-backed StockRepository — Phase 2. Not implemented yet."""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.schemas.stock import StockLevel

_PHASE_2 = "Phase 2 — see docs/architecture.md"


class SqlStockRepository:
    def __init__(self, session: Session):
        self._session = session

    def on_hand(self, product: str) -> Decimal:
        raise NotImplementedError(_PHASE_2)

    def adjust(self, product: str, qty: Decimal, add: bool) -> Decimal:
        raise NotImplementedError(_PHASE_2)

    def product_names(self) -> list[str]:
        raise NotImplementedError(_PHASE_2)

    def list_all(self) -> list[StockLevel]:
        raise NotImplementedError(_PHASE_2)
