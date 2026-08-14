"""Application service for party queries."""
from app.repositories.interfaces import PartyRepository
from app.schemas.party import PartyOut


class PartyService:
    def __init__(self, parties: PartyRepository):
        self._parties = parties

    def search(self, query: str = "") -> list[PartyOut]:
        query = query.strip().lower()
        rows = self._parties.list_all()
        if not query:
            return rows
        return [p for p in rows if query in p.pan.lower() or query in p.name.lower()]
