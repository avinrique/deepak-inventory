"""Excel-backed PartyRepository — thin adapter over the existing storage.py."""
from decimal import Decimal

import storage as db
from app.schemas.party import PartyOut


class ExcelPartyRepository:
    def upsert_totals(self, pan: str, name: str, address: str,
                      total: Decimal, is_sale: bool) -> None:
        db.update_party(pan, name, address, float(total), is_sale)

    def list_all(self) -> list[PartyOut]:
        rows = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)
        return [
            PartyOut(pan=str(r[0] or ""), name=str(r[1] or ""), address=str(r[2] or ""),
                    total_sales=Decimal(str(r[3] or 0)),
                    total_purchases=Decimal(str(r[4] or 0)),
                    total_combined=Decimal(str(r[5] or 0)))
            for r in rows
        ]
