"""The Purchase/Sales transaction-register query — the one place the
per-order tax split (taxable / non-taxable / VAT / total) is expressed in
SQL rather than Python.

Why SQL at all, when app.domain already computes these? Because the
register's footer totals span the *whole filtered set*, not the visible
page. Summing them in Python would mean loading every matching order —
exactly what a register over years of history must not do. So the sums are
pushed into the database, and the page query joins the same expressions per
order.

That leaves two implementations of one rule, which is a real risk, so it is
constrained two ways:

1. The expressions below mirror app.domain.pricing / app.domain.sales
   *term for term* — see the comments on each builder. They are the only
   copy; nothing else in the codebase recomputes tax in SQL.
2. tests/repositories/test_transaction_totals_parity.py asserts the SQL
   result equals the Python result (PurchaseOrderOut/SalesOrderOut's
   computed properties) over randomised orders. If a domain formula
   changes and this file isn't updated, that test fails.

The arithmetic agrees exactly rather than approximately: the domain
functions never quantize, and every division is by 100 — a power of ten,
exact in both Python's Decimal and Postgres NUMERIC. There is no rounding
step to disagree about.

Empty orders (no line items) are kept by a LEFT JOIN and report zeros
rather than vanishing from the register.
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, literal, or_, select

from app.models.customer import Customer
from app.models.product import Product
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.models.sales_order import Invoice, SalesOrder, SalesOrderItem
from app.models.supplier import Supplier
from app.schemas.transactions import TransactionListPage, TransactionListRow, TransactionTotals

_HUNDRED = literal(Decimal("100"))
_ONE_DAY = timedelta(days=1)

# An export is still bounded — a register with more rows than this would
# exhaust memory building the spreadsheet, so the cap is deliberate rather
# than an accident of paging.
_EXPORT_CAP = 50_000


@dataclass(frozen=True)
class _LineExprs:
    """The three per-line money expressions the register needs.

    ``base`` is the amount tax applies to — what app.domain calls the line
    subtotal (Purchasing) or the post-discount subtotal (Sales). The
    taxable/non-taxable split is ``base`` bucketed by whether the line
    carries any tax percent, matching
    TransactionItemsTable.compute_tax_split.
    """
    base: object
    vat: object
    excise: object
    total: object
    tax_percent: object


def _purchase_line_exprs() -> _LineExprs:
    # Mirrors app.domain.pricing:
    #   line_subtotal = quantity * unit_price
    #   line_tax      = quantity * unit_price * tax_percent / 100
    #   line_total    = line_subtotal + line_tax
    # Purchase orders carry neither discount nor excise (see
    # app.models.purchase_order.PurchaseOrderItem).
    base = PurchaseOrderItem.quantity_ordered * PurchaseOrderItem.unit_price
    vat = base * PurchaseOrderItem.tax_percent / _HUNDRED
    return _LineExprs(base=base, vat=vat, excise=literal(Decimal("0")), total=base + vat,
                      tax_percent=PurchaseOrderItem.tax_percent)


def _sales_line_exprs() -> _LineExprs:
    # Mirrors app.domain.sales:
    #   line_subtotal_after_discount = qty*price - qty*price*discount/100
    #   line_tax_after_discount      = after_discount * tax_percent / 100
    #   line_excise_after_discount   = after_discount * excise_percent / 100
    #   line_total_after_discount    = after_discount + tax + excise
    # Tax and excise are both applied to the same post-discount base and
    # neither compounds on the other.
    gross = SalesOrderItem.quantity_ordered * SalesOrderItem.unit_price
    base = gross - gross * SalesOrderItem.discount_percent / _HUNDRED
    vat = base * SalesOrderItem.tax_percent / _HUNDRED
    excise = base * SalesOrderItem.excise_percent / _HUNDRED
    return _LineExprs(base=base, vat=vat, excise=excise, total=base + vat + excise,
                      tax_percent=SalesOrderItem.tax_percent)


def _agg_columns(exprs: _LineExprs):
    """The four register money columns, as aggregate expressions.

    COALESCE keeps an order with no lines at 0.00 instead of NULL, so the
    LEFT JOIN above can preserve empty orders without the DTO needing
    optional money fields.
    """
    zero = literal(Decimal("0"))
    taxable = func.coalesce(
        func.sum(exprs.base).filter(exprs.tax_percent > 0), zero)
    non_taxable = func.coalesce(
        func.sum(exprs.base).filter(exprs.tax_percent == 0), zero)
    vat = func.coalesce(func.sum(exprs.vat), zero)
    excise = func.coalesce(func.sum(exprs.excise), zero)
    total = func.coalesce(func.sum(exprs.total), zero)
    return taxable, non_taxable, vat, excise, total


# Columns a caller may sort by, mapped to the expression that implements
# them. Sorting is server-side because the register is SQL-paginated — a
# client-side sort would only reorder the 25 rows already on screen. Any
# sort_by outside this map falls back to created_at rather than reaching
# SQL, so the value is never interpolated into a query.
_PURCHASE_SORTS = {
    "created_at": PurchaseOrder.created_at,
    "order_number": PurchaseOrder.order_number,
    "invoice_number": PurchaseOrder.supplier_invoice_number,
    "reference_number": PurchaseOrder.reference_number,
    "party_name": Supplier.name,
    "status": PurchaseOrder.status,
}
_SALES_SORTS = {
    "created_at": SalesOrder.created_at,
    "invoice_number": Invoice.invoice_number,
    "reference_number": SalesOrder.reference_number,
    "party_name": Customer.name,
    "status": SalesOrder.status,
}
# Sorting by a money column orders on the aggregate, which only exists in
# the joined subquery — handled separately from the plain column map above.
_AGGREGATE_SORTS = frozenset({"taxable_amount", "non_taxable_amount",
                              "vat_amount", "excise_amount", "total_amount"})


def _day_bounds(day: date) -> datetime:
    """created_at is a timestamptz; a date filter must cover the whole day
    in UTC, not just its midnight instant.
    """
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _date_filters(created_at_col, date_from: date | None, date_to: date | None) -> list:
    clauses = []
    if date_from is not None:
        clauses.append(created_at_col >= _day_bounds(date_from))
    if date_to is not None:
        # Inclusive of the whole end day: strictly-before the next midnight
        # beats <= 23:59:59, which would drop rows in the final second.
        clauses.append(created_at_col < _day_bounds(date_to) + _ONE_DAY)
    return clauses


def _search_clause(columns, term: str):
    like = f"%{term.strip()}%"
    return or_(*[col.ilike(like) for col in columns])


def _money(value) -> Decimal:
    """The LEFT JOIN to the per-order aggregate yields NULL for an order
    with no line items — report it as 0.00 rather than dropping the order.
    """
    return Decimal("0") if value is None else value


def _build_row(r, *, order_number: str | None, invoice_number: str | None
               ) -> TransactionListRow:
    return TransactionListRow(
        id=r.id, status=r.status.value, created_at=r.created_at,
        order_number=order_number, invoice_number=invoice_number,
        reference_number=r.reference_number, party_name=r.party_name,
        hs_codes=sorted(r.hs_codes or []),
        taxable_amount=_money(r.taxable_amount),
        non_taxable_amount=_money(r.non_taxable_amount),
        vat_amount=_money(r.vat_amount),
        excise_amount=_money(r.excise_amount),
        total_amount=_money(r.total_amount))


# --------------------------------------------------------------------- #
# Purchases
# --------------------------------------------------------------------- #
def purchase_list(db, organization_id: uuid.UUID, filter) -> TransactionListPage:
    exprs = _purchase_line_exprs()
    taxable, non_taxable, vat, excise, total = _agg_columns(exprs)

    agg = (
        select(
            PurchaseOrderItem.purchase_order_id.label("order_id"),
            taxable.label("taxable_amount"),
            non_taxable.label("non_taxable_amount"),
            vat.label("vat_amount"),
            excise.label("excise_amount"),
            total.label("total_amount"),
            func.array_remove(func.array_agg(func.distinct(Product.hsn_code)), None)
                .label("hs_codes"),
        )
        .join(Product, Product.id == PurchaseOrderItem.product_id)
        .group_by(PurchaseOrderItem.purchase_order_id)
        .subquery()
    )

    where = [PurchaseOrder.organization_id == organization_id]
    if filter.supplier_id:
        where.append(PurchaseOrder.supplier_id == filter.supplier_id)
    if filter.status:
        where.append(PurchaseOrder.status == filter.status)
    where += _date_filters(PurchaseOrder.created_at, filter.date_from, filter.date_to)
    if filter.search and filter.search.strip():
        where.append(_search_clause(
            [PurchaseOrder.order_number, PurchaseOrder.supplier_invoice_number,
             PurchaseOrder.reference_number, Supplier.name],
            filter.search))

    page = max(1, filter.page)
    page_size = max(1, filter.page_size)

    count_q = (select(func.count()).select_from(PurchaseOrder)
               .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
               .where(and_(*where)))
    record_count = db.execute(count_q).scalar() or 0

    rows_q = (
        select(
            PurchaseOrder.id, PurchaseOrder.status, PurchaseOrder.created_at,
            PurchaseOrder.order_number, PurchaseOrder.supplier_invoice_number,
            PurchaseOrder.reference_number, Supplier.name.label("party_name"),
            agg.c.hs_codes, agg.c.taxable_amount, agg.c.non_taxable_amount,
            agg.c.vat_amount, agg.c.excise_amount, agg.c.total_amount,
        )
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .outerjoin(agg, agg.c.order_id == PurchaseOrder.id)
        .where(and_(*where))
        .order_by(*_order_by(filter, _PURCHASE_SORTS, agg, PurchaseOrder.created_at,
                             PurchaseOrder.id))
        .offset((page - 1) * page_size).limit(page_size)
    )

    items = [
        _build_row(r, order_number=r.order_number, invoice_number=r.supplier_invoice_number)
        for r in db.execute(rows_q)
    ]

    totals = _purchase_totals(db, where, record_count)
    return TransactionListPage(items=items, totals=totals, total=record_count,
                               page=page, page_size=page_size)


def _purchase_totals(db, where: list, record_count: int) -> TransactionTotals:
    """Sums over the whole filtered set, independent of pagination.

    Scoped by an IN over the filtered order ids rather than by re-joining,
    so the aggregate visits each line exactly once.
    """
    exprs = _purchase_line_exprs()
    taxable, non_taxable, vat, excise, total = _agg_columns(exprs)
    ids = (select(PurchaseOrder.id)
           .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
           .where(and_(*where)))
    q = (select(taxable, non_taxable, vat, excise, total)
         .select_from(PurchaseOrderItem)
         .where(PurchaseOrderItem.purchase_order_id.in_(ids)))
    row = db.execute(q).one()
    return TransactionTotals(taxable_amount=row[0], non_taxable_amount=row[1],
                             vat_amount=row[2], excise_amount=row[3], total_amount=row[4],
                             record_count=record_count)


def purchase_export_rows(db, organization_id: uuid.UUID, filter) -> list[TransactionListRow]:
    """The same register, unpaginated — what Export writes to CSV/Excel.

    Still one bounded query over the *filtered* set, not the whole table.
    """
    unpaged = filter.model_copy(update={"page": 1, "page_size": _EXPORT_CAP})
    return purchase_list(db, organization_id, unpaged).items


# --------------------------------------------------------------------- #
# Sales
# --------------------------------------------------------------------- #
def sales_list(db, organization_id: uuid.UUID, filter) -> TransactionListPage:
    exprs = _sales_line_exprs()
    taxable, non_taxable, vat, excise, total = _agg_columns(exprs)

    agg = (
        select(
            SalesOrderItem.sales_order_id.label("order_id"),
            taxable.label("taxable_amount"),
            non_taxable.label("non_taxable_amount"),
            vat.label("vat_amount"),
            excise.label("excise_amount"),
            total.label("total_amount"),
            func.array_remove(func.array_agg(func.distinct(Product.hsn_code)), None)
                .label("hs_codes"),
        )
        .join(Product, Product.id == SalesOrderItem.product_id)
        .group_by(SalesOrderItem.sales_order_id)
        .subquery()
    )

    where = [SalesOrder.organization_id == organization_id]
    if filter.customer_id:
        where.append(SalesOrder.customer_id == filter.customer_id)
    if filter.status:
        where.append(SalesOrder.status == filter.status)
    where += _date_filters(SalesOrder.created_at, filter.date_from, filter.date_to)
    if filter.search and filter.search.strip():
        where.append(_search_clause(
            [Invoice.invoice_number, SalesOrder.reference_number, Customer.name],
            filter.search))

    page = max(1, filter.page)
    page_size = max(1, filter.page_size)

    # Invoice is LEFT-joined: an order only has one once it's been
    # generated, and un-invoiced orders must still appear in the register.
    def _base(stmt):
        return (stmt.join(Customer, Customer.id == SalesOrder.customer_id)
                    .outerjoin(Invoice, Invoice.sales_order_id == SalesOrder.id))

    count_q = _base(select(func.count()).select_from(SalesOrder)).where(and_(*where))
    record_count = db.execute(count_q).scalar() or 0

    rows_q = (
        _base(select(
            SalesOrder.id, SalesOrder.status, SalesOrder.created_at,
            Invoice.invoice_number, SalesOrder.reference_number,
            Customer.name.label("party_name"),
            agg.c.hs_codes, agg.c.taxable_amount, agg.c.non_taxable_amount,
            agg.c.vat_amount, agg.c.excise_amount, agg.c.total_amount,
        ))
        .outerjoin(agg, agg.c.order_id == SalesOrder.id)
        .where(and_(*where))
        .order_by(*_order_by(filter, _SALES_SORTS, agg, SalesOrder.created_at,
                             SalesOrder.id))
        .offset((page - 1) * page_size).limit(page_size)
    )

    items = [
        _build_row(r, order_number=None, invoice_number=r.invoice_number)
        for r in db.execute(rows_q)
    ]

    totals = _sales_totals(db, where, record_count)
    return TransactionListPage(items=items, totals=totals, total=record_count,
                               page=page, page_size=page_size)


def _sales_totals(db, where: list, record_count: int) -> TransactionTotals:
    exprs = _sales_line_exprs()
    taxable, non_taxable, vat, excise, total = _agg_columns(exprs)
    ids = (select(SalesOrder.id)
           .join(Customer, Customer.id == SalesOrder.customer_id)
           .outerjoin(Invoice, Invoice.sales_order_id == SalesOrder.id)
           .where(and_(*where)))
    q = (select(taxable, non_taxable, vat, excise, total)
         .select_from(SalesOrderItem)
         .where(SalesOrderItem.sales_order_id.in_(ids)))
    row = db.execute(q).one()
    return TransactionTotals(taxable_amount=row[0], non_taxable_amount=row[1],
                             vat_amount=row[2], excise_amount=row[3], total_amount=row[4],
                             record_count=record_count)


def sales_export_rows(db, organization_id: uuid.UUID, filter) -> list[TransactionListRow]:
    unpaged = filter.model_copy(update={"page": 1, "page_size": _EXPORT_CAP})
    return sales_list(db, organization_id, unpaged).items


def _order_by(filter, sorts: dict, agg, default, pk):
    """Resolve sort_by against the whitelist; anything unknown falls back
    to the default column, so an unexpected value can never reach SQL.

    The primary key always terminates the ordering. Sorting by status, or
    by a total two orders happen to share, otherwise leaves those rows in
    an order the database is free to vary between queries — under
    LIMIT/OFFSET that means a row can appear on two consecutive pages
    while another never appears at all. A total ordering removes the
    ambiguity.
    """
    key = getattr(filter, "sort_by", None) or "created_at"
    desc = bool(getattr(filter, "sort_desc", True))
    if key in _AGGREGATE_SORTS:
        column = getattr(agg.c, key)
    else:
        column = sorts.get(key, default)
    primary = column.desc() if desc else column.asc()
    return [primary, default.desc(), pk.asc()]
