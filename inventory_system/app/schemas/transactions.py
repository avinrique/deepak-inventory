"""Flattened, display-ready row shape for the Purchase and Sales
transaction-register lists — one row per order, already joined to the
party name and already carrying the tax split, so the list pages never
issue a follow-up query per row (the N+1 that PurchaseOrderOut /
SalesOrderOut would force, since those carry ids and no HS code).

Purchasing and Sales share this one shape deliberately: the two registers
differ only in which party they name and where the invoice number comes
from, so a single DTO lets app.ui.widgets.totals_table and both list pages
share their rendering instead of duplicating it.

The money fields are *not* a second implementation of the tax rules. They
are produced by app.repositories.sql.transaction_list, whose SQL mirrors
app.domain.pricing / app.domain.sales expression for expression, and
tests/repositories/test_transaction_totals_parity.py is what holds the two
together.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class TransactionListRow(BaseModel):
    id: uuid.UUID
    # The order's status enum *value* (e.g. "DRAFT"). Kept as a plain str
    # so one DTO serves both PurchaseOrderStatus and SalesOrderStatus; each
    # page converts it back to its own enum to gate row actions.
    status: str
    created_at: datetime
    # Purchasing: the system-generated PurchaseOrder.order_number.
    # Sales: None — a sales order has no counterpart, its document number
    # is the invoice number below.
    order_number: str | None = None
    # Purchasing: PurchaseOrder.supplier_invoice_number (the supplier's own
    # bill number). Sales: Invoice.invoice_number, once one is generated.
    invoice_number: str | None = None
    reference_number: str | None = None
    party_name: str
    # Every distinct non-null Product.hsn_code across the order's lines. An
    # order may span several; the UI shows the first plus "+N more".
    hs_codes: list[str] = []
    taxable_amount: Decimal
    non_taxable_amount: Decimal
    vat_amount: Decimal
    # Sales only (purchase orders carry no excise). Broken out because
    # otherwise total_amount would not equal taxable + non_taxable + vat and
    # the register would look like it can't add up — the UI surfaces this in
    # the Amount cell's tooltip and as its own export column.
    excise_amount: Decimal = Decimal("0")
    total_amount: Decimal


class TransactionTotals(BaseModel):
    """Aggregates over the whole *filtered* result set, not just the
    visible page — computed by a SQL SUM so a large register never has to
    be loaded into memory to be totalled.
    """
    taxable_amount: Decimal = Decimal("0")
    non_taxable_amount: Decimal = Decimal("0")
    vat_amount: Decimal = Decimal("0")
    excise_amount: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    record_count: int = 0


class TransactionListPage(BaseModel):
    items: list[TransactionListRow]
    totals: TransactionTotals
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))  # ceil division
