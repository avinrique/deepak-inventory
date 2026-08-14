"""SQLAlchemy-backed BillRepository — Phase 2. Not implemented yet."""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.schemas.bill import BillCreate, BillOut

_PHASE_2 = "Phase 2 — see docs/architecture.md"


class SqlBillRepository:
    def __init__(self, session: Session):
        self._session = session

    def append(self, bill: BillCreate, subtotal: Decimal, vat_amount: Decimal,
              total: Decimal) -> None:
        raise NotImplementedError(_PHASE_2)

    def exists(self, bill_no: str) -> bool:
        raise NotImplementedError(_PHASE_2)

    def list_all(self) -> list[BillOut]:
        raise NotImplementedError(_PHASE_2)
