"""Search, date-range, sort and pagination behaviour of the Purchase and
Sales registers (app.repositories.sql.transaction_list).

The money maths is covered separately by
test_transaction_totals_parity.py; this module is about *which* rows come
back, in *what order*, and whether the totals track the filter.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.domain.purchasing import PurchaseOrderStatus
from app.models import (
    Customer,
    Organization,
    Product,
    PurchaseOrder,
    SalesOrder,
    Supplier,
    Unit,
    User,
    Warehouse,
)
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.purchasing import (
    PurchaseOrderCreate,
    PurchaseOrderFilter,
    PurchaseOrderItemInput,
)
from app.schemas.sales import SalesOrderCreate, SalesOrderFilter, SalesOrderItemInput


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Register Traders")
        other_org = Organization(name="Someone Else")
        session.add_all([org, other_org])
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()
        widget = Product(organization_id=org.id, sku="SKU-W", name="Widget", unit_id=unit.id,
                         purchase_price=Decimal("10"), selling_price=Decimal("15"),
                         tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"),
                         hsn_code="1001")
        gadget = Product(organization_id=org.id, sku="SKU-G", name="Gadget", unit_id=unit.id,
                         purchase_price=Decimal("20"), selling_price=Decimal("30"),
                         tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"),
                         hsn_code="2002")
        # Deliberately without an HS code — must contribute nothing to the
        # row's code list rather than a blank entry.
        plain = Product(organization_id=org.id, sku="SKU-P", name="Plain", unit_id=unit.id,
                        purchase_price=Decimal("5"), selling_price=Decimal("7"),
                        tax_percent=Decimal("0"), minimum_stock_level=Decimal("0"))
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        acme = Supplier(organization_id=org.id, name="Acme Supplies")
        globex = Supplier(organization_id=org.id, name="Globex Trading")
        jane = Customer(organization_id=org.id, name="Jane Buyer")
        raj = Customer(organization_id=org.id, name="Raj Enterprises")
        user = User(email="reg@example.com", username="reg", hashed_password="x",
                    full_name="Registrar")
        session.add_all([widget, gadget, plain, warehouse, acme, globex, jane, raj, user])
        session.flush()
        return {
            "org_id": org.id, "other_org_id": other_org.id,
            "widget_id": widget.id, "gadget_id": gadget.id, "plain_id": plain.id,
            "warehouse_id": warehouse.id, "acme_id": acme.id, "globex_id": globex.id,
            "jane_id": jane.id, "raj_id": raj.id, "user_id": user.id,
        }


def _po(world, *, supplier_id=None, products=None, supplier_invoice_number=None,
        reference_number=None, quantity=Decimal("1"), unit_price=Decimal("100")):
    products = products or [world["widget_id"]]
    return SqlPurchaseOrderRepository().create(world["org_id"], PurchaseOrderCreate(
        supplier_id=supplier_id or world["acme_id"],
        warehouse_id=world["warehouse_id"],
        supplier_invoice_number=supplier_invoice_number,
        reference_number=reference_number,
        items=[PurchaseOrderItemInput(product_id=pid, quantity_ordered=quantity,
                                      unit_price=unit_price, tax_percent=Decimal("13"))
               for pid in products],
    ), world["user_id"])


def _so(world, *, customer_id=None, products=None, reference_number=None,
        quantity=Decimal("1"), unit_price=Decimal("100")):
    products = products or [world["widget_id"]]
    return SqlSalesOrderRepository().create(world["org_id"], SalesOrderCreate(
        customer_id=customer_id or world["jane_id"],
        warehouse_id=world["warehouse_id"], reference_number=reference_number,
        items=[SalesOrderItemInput(product_id=pid, quantity_ordered=quantity,
                                   unit_price=unit_price, tax_percent=Decimal("13"),
                                   discount_percent=Decimal("0"),
                                   excise_percent=Decimal("0"))
               for pid in products],
    ), world["user_id"])


def _backdate(model, order_id: uuid.UUID, when: datetime) -> None:
    """created_at is server-defaulted, so date-range tests have to move it
    explicitly rather than hoping for distinct timestamps.
    """
    with get_session() as db:
        db.query(model).filter(model.id == order_id).update({"created_at": when})


def _list(world, **kwargs):
    return SqlPurchaseOrderRepository().list_transactions(
        world["org_id"], PurchaseOrderFilter(**kwargs))


# --------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------- #
def test_search_matches_supplier_invoice_number(world):
    _po(world, supplier_invoice_number="BILL-9911")
    _po(world, supplier_invoice_number="BILL-2200")

    page = _list(world, search="9911")
    assert [r.invoice_number for r in page.items] == ["BILL-9911"]
    assert page.total == 1


def test_search_matches_reference_number(world):
    _po(world, reference_number="CONTRACT-7")
    _po(world, reference_number="CONTRACT-8")

    assert [r.reference_number for r in _list(world, search="CONTRACT-7").items] \
        == ["CONTRACT-7"]


def test_search_matches_supplier_name_case_insensitively(world):
    _po(world, supplier_id=world["acme_id"])
    _po(world, supplier_id=world["globex_id"])

    page = _list(world, search="globex")
    assert [r.party_name for r in page.items] == ["Globex Trading"]


def test_search_matches_order_number(world):
    created = _po(world)
    _po(world)

    page = _list(world, search=created.order_number)
    assert [r.order_number for r in page.items] == [created.order_number]


def test_search_totals_cover_only_the_matching_rows(world):
    _po(world, supplier_id=world["acme_id"], unit_price=Decimal("100"))
    _po(world, supplier_id=world["globex_id"], unit_price=Decimal("500"))

    page = _list(world, search="Globex")
    assert page.totals.record_count == 1
    assert page.totals.taxable_amount == Decimal("500")


# --------------------------------------------------------------------- #
# Date range
# --------------------------------------------------------------------- #
def test_date_range_bounds_are_both_inclusive(world):
    old = _po(world)
    middle = _po(world)
    new = _po(world)
    _backdate(PurchaseOrder, old.id, datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    _backdate(PurchaseOrder, middle.id, datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))
    # Late in the day: an upper bound implemented as "<= 23:59:59" would
    # wrongly drop this row.
    _backdate(PurchaseOrder, new.id, datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc))

    page = _list(world, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31))
    assert {r.id for r in page.items} == {old.id, middle.id, new.id}

    narrowed = _list(world, date_from=date(2026, 1, 15), date_to=date(2026, 1, 15))
    assert {r.id for r in narrowed.items} == {middle.id}


def test_date_range_excludes_rows_outside_it_from_totals(world):
    inside = _po(world, unit_price=Decimal("100"))
    outside = _po(world, unit_price=Decimal("900"))
    _backdate(PurchaseOrder, inside.id, datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc))
    _backdate(PurchaseOrder, outside.id, datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc))

    page = _list(world, date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
    assert page.totals.record_count == 1
    assert page.totals.taxable_amount == Decimal("100")


# --------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------- #
def test_sort_by_party_name_both_directions(world):
    _po(world, supplier_id=world["globex_id"])
    _po(world, supplier_id=world["acme_id"])

    ascending = _list(world, sort_by="party_name", sort_desc=False)
    assert [r.party_name for r in ascending.items] == ["Acme Supplies", "Globex Trading"]

    descending = _list(world, sort_by="party_name", sort_desc=True)
    assert [r.party_name for r in descending.items] == ["Globex Trading", "Acme Supplies"]


def test_sort_by_total_amount(world):
    _po(world, unit_price=Decimal("10"))
    _po(world, unit_price=Decimal("900"))
    _po(world, unit_price=Decimal("100"))

    ascending = _list(world, sort_by="total_amount", sort_desc=False)
    assert [r.total_amount for r in ascending.items] == [
        Decimal("11.30"), Decimal("113.00"), Decimal("1017.00")]


def test_unknown_sort_by_falls_back_instead_of_reaching_sql(world):
    _po(world)
    _po(world)

    # A value that is not in the whitelist — including one shaped like an
    # injection attempt — must be ignored, not interpolated.
    page = _list(world, sort_by="total_amount; DROP TABLE purchase_orders")
    assert page.total == 2
    with get_session() as db:
        assert db.query(PurchaseOrder).count() == 2


def test_pagination_is_stable_when_sort_keys_tie(world):
    """Every order here shares a status and an identical total, so the sort
    key alone cannot order them. Without the primary-key tiebreaker the
    database may return them in any order per query, and paging would show
    one row twice while another never appeared.
    """
    for _ in range(6):
        _po(world, unit_price=Decimal("100"))

    seen = []
    for page_number in range(1, 4):
        page = _list(world, sort_by="status", page=page_number, page_size=2)
        seen.extend(r.id for r in page.items)

    assert len(seen) == 6
    assert len(set(seen)) == 6, "a row was repeated or dropped across pages"


# --------------------------------------------------------------------- #
# HS codes
# --------------------------------------------------------------------- #
def test_hs_codes_are_collected_across_the_orders_lines(world):
    _po(world, products=[world["widget_id"], world["gadget_id"]])

    row = _list(world).items[0]
    assert row.hs_codes == ["1001", "2002"]


def test_products_without_an_hs_code_contribute_nothing(world):
    _po(world, products=[world["widget_id"], world["plain_id"]])

    row = _list(world).items[0]
    assert row.hs_codes == ["1001"]


def test_hs_codes_are_deduplicated(world):
    _po(world, products=[world["widget_id"], world["widget_id"]])

    assert _list(world).items[0].hs_codes == ["1001"]


# --------------------------------------------------------------------- #
# Scoping and status
# --------------------------------------------------------------------- #
def test_another_organizations_orders_are_never_listed(world):
    _po(world)
    page = SqlPurchaseOrderRepository().list_transactions(
        world["other_org_id"], PurchaseOrderFilter())
    assert page.items == []
    assert page.totals.total_amount == Decimal("0")


def test_status_filter_narrows_rows_and_totals(world):
    draft = _po(world, unit_price=Decimal("100"))
    submitted = _po(world, unit_price=Decimal("700"))
    SqlPurchaseOrderRepository().submit(world["org_id"], submitted.id)

    page = _list(world, status=PurchaseOrderStatus.SUBMITTED)
    assert [r.id for r in page.items] == [submitted.id]
    assert page.totals.taxable_amount == Decimal("700")
    assert draft.id not in {r.id for r in page.items}


# --------------------------------------------------------------------- #
# Sales-specific
# --------------------------------------------------------------------- #
def test_sales_rows_show_the_invoice_number_once_generated(world):
    repo = SqlSalesOrderRepository()
    order = _so(world)
    # Fulfilment deducts stock, so there has to be some to deduct.
    SqlInventoryRepository().stock_in(world["org_id"], world["widget_id"],
                                      world["warehouse_id"], Decimal("50"), world["user_id"])

    before = repo.list_transactions(world["org_id"], SalesOrderFilter()).items[0]
    assert before.invoice_number is None, "an un-invoiced order still belongs in the register"

    repo.confirm(world["org_id"], order.id, world["user_id"])
    repo.fulfill_sale(world["org_id"], order.id, world["user_id"])
    invoice = repo.generate_invoice(world["org_id"], order.id, world["user_id"])

    after = repo.list_transactions(world["org_id"], SalesOrderFilter()).items[0]
    assert after.invoice_number == invoice.invoice_number


def test_sales_search_matches_customer_name(world):
    _so(world, customer_id=world["jane_id"])
    _so(world, customer_id=world["raj_id"])

    page = SqlSalesOrderRepository().list_transactions(
        world["org_id"], SalesOrderFilter(search="raj"))
    assert [r.party_name for r in page.items] == ["Raj Enterprises"]


def test_sales_export_returns_every_filtered_row_unpaginated(world):
    for _ in range(5):
        _so(world)

    rows = SqlSalesOrderRepository().export_transactions(
        world["org_id"], SalesOrderFilter(page_size=2))
    assert len(rows) == 5, "export must ignore the page size the screen uses"
