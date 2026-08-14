from decimal import Decimal

from pydantic import BaseModel


class PartyOut(BaseModel):
    pan: str
    name: str
    address: str
    total_sales: Decimal
    total_purchases: Decimal
    total_combined: Decimal
