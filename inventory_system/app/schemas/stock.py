from decimal import Decimal

from pydantic import BaseModel


class StockLevel(BaseModel):
    product: str
    quantity: Decimal
