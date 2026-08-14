from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class BillLineIn(BaseModel):
    product: str
    qty: Decimal = Field(gt=0)
    rate: Decimal = Field(ge=0)


class BillCreate(BaseModel):
    date: date
    bill_no: str = ""
    pan: str = ""
    vendor: str = ""
    address: str = ""
    ecs: Decimal = Decimal("0")
    vat_percent: Decimal = Decimal("13")
    lines: list[BillLineIn]


class BillOut(BaseModel):
    date: date
    bill_no: str
    pan: str
    vendor: str
    address: str
    lines: list[BillLineIn]
    subtotal: Decimal
    ecs: Decimal
    vat_percent: Decimal
    vat_amount: Decimal
    total: Decimal
