"""SQLAlchemy-backed PartyRepository — Phase 2. Not implemented yet."""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.schemas.party import PartyOut

_PHASE_2 = "Phase 2 — see docs/architecture.md"


class SqlPartyRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert_totals(self, pan: str, name: str, address: str,
                      total: Decimal, is_sale: bool) -> None:
        raise NotImplementedError(_PHASE_2)

    def list_all(self) -> list[PartyOut]:
        raise NotImplementedError(_PHASE_2)
