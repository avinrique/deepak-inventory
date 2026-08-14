"""Application service for creating sales/purchases.

Orchestrates app.domain (pure math/validation) + repositories. This is the
seam the UI calls instead of doing the equivalent work inline in a widget —
see docs/architecture.md Phase 1 for the cutover plan from the legacy
Tkinter inventory_app.py.
"""
from app.core.exceptions import DuplicateBillError
from app.domain.billing import BillLine, calculate_totals, stock_shortfall
from app.repositories.interfaces import BillRepository, PartyRepository, StockRepository
from app.schemas.bill import BillCreate, BillOut

__all__ = ["BillingService", "DuplicateBillError"]


class BillingService:
    def __init__(self, sales: BillRepository, purchases: BillRepository,
                stock: StockRepository, parties: PartyRepository):
        # Sales and purchases are separate repositories (separate ledgers/
        # files) — a single shared "bills" repo was an earlier mistake this
        # constructor now makes structurally impossible to repeat.
        self._sales = sales
        self._purchases = purchases
        self._stock = stock
        self._parties = parties

    def _totals(self, bill: BillCreate):
        lines = [BillLine(ln.product, ln.qty, ln.rate) for ln in bill.lines]
        return lines, calculate_totals(lines, bill.ecs, bill.vat_percent)

    def create_sale(self, bill: BillCreate, *, allow_duplicate: bool = False) -> BillOut:
        return self._create(bill, repo=self._sales, add_stock=False, is_sale=True,
                            allow_duplicate=allow_duplicate)

    def create_purchase(self, bill: BillCreate, *, allow_duplicate: bool = False) -> BillOut:
        return self._create(bill, repo=self._purchases, add_stock=True, is_sale=False,
                            allow_duplicate=allow_duplicate)

    def _create(self, bill: BillCreate, *, repo: BillRepository, add_stock: bool,
               is_sale: bool, allow_duplicate: bool) -> BillOut:
        if bill.bill_no and repo.exists(bill.bill_no) and not allow_duplicate:
            raise DuplicateBillError(bill.bill_no)
        lines, totals = self._totals(bill)
        # NOTE: not atomic against the excel/ repositories — a failure between
        # these three calls leaves the ledger/stock/party out of sync, same
        # risk as inventory_app._commit() today. Real atomicity arrives with
        # the sql/ repositories' single database transaction (Phase 2).
        repo.append(bill, totals.subtotal, totals.vat_amount, totals.total)
        for ln in lines:
            self._stock.adjust(ln.product, ln.qty, add=add_stock)
        self._parties.upsert_totals(bill.pan, bill.vendor, bill.address,
                                    totals.total, is_sale=is_sale)
        return BillOut(date=bill.date, bill_no=bill.bill_no, pan=bill.pan,
                       vendor=bill.vendor, address=bill.address, lines=bill.lines,
                       subtotal=totals.subtotal, ecs=totals.ecs,
                       vat_percent=totals.vat_percent, vat_amount=totals.vat_amount,
                       total=totals.total)

    def check_shortfall(self, bill: BillCreate) -> list[str]:
        lines, _ = self._totals(bill)
        on_hand = {ln.product.strip().lower(): self._stock.on_hand(ln.product)
                  for ln in lines}
        return stock_shortfall(lines, on_hand)

    def list_sales(self) -> list[BillOut]:
        return self._sales.list_all()

    def list_purchases(self) -> list[BillOut]:
        return self._purchases.list_all()
