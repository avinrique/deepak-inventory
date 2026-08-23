"""The drift guard for app.repositories.sql.transaction_list.

The transaction registers total the *whole filtered set* in SQL, because
summing them in Python would mean loading every matching order — which a
register over years of history must never do. That necessarily states the
tax rules a second time, in SQL, alongside app.domain.pricing /
app.domain.sales.

This module is what keeps the two statements equal. It asserts **exact**
Decimal equality (never pytest.approx) between the SQL aggregate and the
Python computed properties on PurchaseOrderOut / SalesOrderOut, over
deliberately awkward inputs. Exactness is achievable, not optimistic: the
domain functions never quantize and every division is by 100 — a power of
ten, exact in both Python Decimal and Postgres NUMERIC — so there is no
rounding step for the two sides to disagree about.

If a domain formula changes and transaction_list isn't updated to match,
these tests fail. That is their entire purpose.

Uses the ``live_db`` fixture (tests/conftest.py) — Postgres only. SQLite
would coerce NUMERIC to float and produce false failures.
"""
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.models import Customer, Organization, Product, Supplier, Unit, User, Warehouse
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.purchasing import (
    PurchaseOrderCreate,
    PurchaseOrderFilter,
    PurchaseOrderItemInput,
)
from app.schemas.sales import SalesOrderCreate, SalesOrderFilter, SalesOrderItemInput

# Rates chosen to be hostile to floating point and to naive rounding:
# a repeating-decimal percent (33.33), a boundary (100), and zero — the
# last being the one that decides the taxable/non-taxable split.
_TAX_RATES = [Decimal("0"), Decimal("13"), Decimal("33.33"), Decimal("100")]
_DISCOUNTS = [Decimal("0"), Decimal("33.33"), Decimal("50")]
_EXCISES = [Decimal("0"), Decimal("5"), Decimal("12.5")]
# Three-decimal quantities exercise the full NUMERIC(14,3) scale.
_QUANTITIES = [Decimal("1"), Decimal("2.755"), Decimal("13.001")]
_PRICES = [Decimal("0.01"), Decimal("19.99"), Decimal("1234.56")]


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Parity Traders")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()
        products = []
        for i in range(4):
            products.append(Product(
                organization_id=org.id, sku=f"SKU-{i}", name=f"Product {i}",
                unit_id=unit.id, purchase_price=Decimal("10"), selling_price=Decimal("15"),
                tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"),
                hsn_code=f"100{i}"))
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        supplier = Supplier(organization_id=org.id, name="Best Supplies")
        customer = Customer(organization_id=org.id, name="Jane Buyer")
        user = User(email="parity@example.com", username="parity", hashed_password="x",
                    full_name="Parity")
        session.add_all([*products, warehouse, supplier, customer, user])
        session.flush()
        return {
            "org_id": org.id, "product_ids": [p.id for p in products],
            "warehouse_id": warehouse.id, "supplier_id": supplier.id,
            "customer_id": customer.id, "user_id": user.id,
        }


def _adversarial_lines():
    """One line per (tax, discount, excise) combination, cycling quantity
    and price so no two lines share the same arithmetic.
    """
    lines = []
    i = 0
    for tax in _TAX_RATES:
        for discount in _DISCOUNTS:
            for excise in _EXCISES:
                lines.append((
                    _QUANTITIES[i % len(_QUANTITIES)],
                    _PRICES[i % len(_PRICES)],
                    tax, discount, excise,
                ))
                i += 1
    return lines


def _create_purchases(world) -> None:
    """Spread the adversarial lines across several orders — including a
    single-line order and a many-line one — so the aggregate is exercised
    both within an order and across orders.
    """
    repo = SqlPurchaseOrderRepository()
    lines = _adversarial_lines()
    product_ids = world["product_ids"]
    for chunk in (lines[:1], lines[1:5], lines[5:]):
        repo.create(world["org_id"], PurchaseOrderCreate(
            supplier_id=world["supplier_id"], warehouse_id=world["warehouse_id"],
            items=[PurchaseOrderItemInput(
                product_id=product_ids[i % len(product_ids)],
                quantity_ordered=qty, unit_price=price, tax_percent=tax)
                for i, (qty, price, tax, _d, _e) in enumerate(chunk)],
        ), world["user_id"])


def _create_sales(world) -> None:
    repo = SqlSalesOrderRepository()
    lines = _adversarial_lines()
    product_ids = world["product_ids"]
    for chunk in (lines[:1], lines[1:5], lines[5:]):
        repo.create(world["org_id"], SalesOrderCreate(
            customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
            items=[SalesOrderItemInput(
                product_id=product_ids[i % len(product_ids)],
                quantity_ordered=qty, unit_price=price, tax_percent=tax,
                discount_percent=discount, excise_percent=excise)
                for i, (qty, price, tax, discount, excise) in enumerate(chunk)],
        ), world["user_id"])


# --------------------------------------------------------------------- #
# Purchases
# --------------------------------------------------------------------- #
def test_purchase_totals_match_domain_exactly(world):
    _create_purchases(world)
    repo = SqlPurchaseOrderRepository()

    python_orders = repo.search(world["org_id"], PurchaseOrderFilter(page_size=500)).items
    sql = repo.list_transactions(world["org_id"], PurchaseOrderFilter(page_size=500)).totals

    assert sql.total_amount == sum((o.total_amount for o in python_orders), Decimal("0"))
    assert sql.vat_amount == sum((o.tax_amount for o in python_orders), Decimal("0"))
    # Purchase orders carry no excise at all — see PurchaseOrderItem.
    assert sql.excise_amount == Decimal("0")
    # Taxable + non-taxable is the whole (pre-tax) subtotal, split by
    # whether the line is taxed. Both buckets together must reconstruct it.
    assert (sql.taxable_amount + sql.non_taxable_amount
            == sum((o.subtotal for o in python_orders), Decimal("0")))
    assert sql.record_count == len(python_orders)


def test_purchase_per_row_totals_match_domain_exactly(world):
    _create_purchases(world)
    repo = SqlPurchaseOrderRepository()

    by_id = {o.id: o for o in
             repo.search(world["org_id"], PurchaseOrderFilter(page_size=500)).items}
    rows = repo.list_transactions(world["org_id"], PurchaseOrderFilter(page_size=500)).items

    assert rows, "fixture produced no purchase orders"
    for row in rows:
        order = by_id[row.id]
        assert row.total_amount == order.total_amount
        assert row.vat_amount == order.tax_amount
        assert row.taxable_amount + row.non_taxable_amount == order.subtotal


def test_purchase_taxable_split_follows_tax_percent(world):
    """A line lands in taxable iff its tax percent is above zero — the rule
    TransactionItemsTable.compute_tax_split states for the form.
    """
    repo = SqlPurchaseOrderRepository()
    repo.create(world["org_id"], PurchaseOrderCreate(
        supplier_id=world["supplier_id"], warehouse_id=world["warehouse_id"],
        items=[
            PurchaseOrderItemInput(product_id=world["product_ids"][0],
                                   quantity_ordered=Decimal("2"), unit_price=Decimal("100"),
                                   tax_percent=Decimal("13")),
            PurchaseOrderItemInput(product_id=world["product_ids"][1],
                                   quantity_ordered=Decimal("3"), unit_price=Decimal("50"),
                                   tax_percent=Decimal("0")),
        ]), world["user_id"])

    totals = repo.list_transactions(world["org_id"], PurchaseOrderFilter()).totals
    assert totals.taxable_amount == Decimal("200")
    assert totals.non_taxable_amount == Decimal("150")
    assert totals.vat_amount == Decimal("26")
    assert totals.total_amount == Decimal("376")


# --------------------------------------------------------------------- #
# Sales
# --------------------------------------------------------------------- #
def test_sales_totals_match_domain_exactly(world):
    _create_sales(world)
    repo = SqlSalesOrderRepository()

    python_orders = repo.search(world["org_id"], SalesOrderFilter(page_size=500)).items
    sql = repo.list_transactions(world["org_id"], SalesOrderFilter(page_size=500)).totals

    assert sql.total_amount == sum((o.total_amount for o in python_orders), Decimal("0"))
    assert sql.vat_amount == sum((o.tax_amount for o in python_orders), Decimal("0"))
    assert sql.excise_amount == sum((o.excise_amount for o in python_orders), Decimal("0"))
    assert sql.record_count == len(python_orders)


def test_sales_per_row_totals_match_domain_exactly(world):
    _create_sales(world)
    repo = SqlSalesOrderRepository()

    by_id = {o.id: o for o in
             repo.search(world["org_id"], SalesOrderFilter(page_size=500)).items}
    rows = repo.list_transactions(world["org_id"], SalesOrderFilter(page_size=500)).items

    assert rows, "fixture produced no sales orders"
    for row in rows:
        order = by_id[row.id]
        assert row.total_amount == order.total_amount
        assert row.vat_amount == order.tax_amount
        assert row.excise_amount == order.excise_amount


def test_sales_excise_is_included_in_the_total(world):
    """The register's Amount column must carry excise.

    Regression guard with history: app.repositories.sql.reporting_repository's
    own sales totals subquery silently omits excise (it predates the field),
    which is exactly the mistake a copy-paste here would reintroduce.
    """
    repo = SqlSalesOrderRepository()
    repo.create(world["org_id"], SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(
            product_id=world["product_ids"][0], quantity_ordered=Decimal("2"),
            unit_price=Decimal("100"), tax_percent=Decimal("13"),
            discount_percent=Decimal("0"), excise_percent=Decimal("5"))],
    ), world["user_id"])

    totals = repo.list_transactions(world["org_id"], SalesOrderFilter()).totals
    assert totals.taxable_amount == Decimal("200")
    assert totals.vat_amount == Decimal("26")
    assert totals.excise_amount == Decimal("10")
    assert totals.total_amount == Decimal("236")


def test_sales_discount_is_applied_before_tax_and_excise(world):
    """Tax and excise both apply to the post-discount base, and neither
    compounds on the other — app.domain.sales.line_total_after_discount.
    """
    repo = SqlSalesOrderRepository()
    repo.create(world["org_id"], SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(
            product_id=world["product_ids"][0], quantity_ordered=Decimal("1"),
            unit_price=Decimal("1000"), tax_percent=Decimal("13"),
            discount_percent=Decimal("10"), excise_percent=Decimal("5"))],
    ), world["user_id"])

    totals = repo.list_transactions(world["org_id"], SalesOrderFilter()).totals
    assert totals.taxable_amount == Decimal("900")   # 1000 less 10%
    assert totals.vat_amount == Decimal("117")       # 13% of 900
    assert totals.excise_amount == Decimal("45")     # 5% of 900
    assert totals.total_amount == Decimal("1062")


# --------------------------------------------------------------------- #
# Totals are over the filtered set, not the page
# --------------------------------------------------------------------- #
def test_totals_cover_the_whole_filtered_set_not_just_the_page(world):
    _create_purchases(world)
    repo = SqlPurchaseOrderRepository()

    everything = repo.list_transactions(world["org_id"], PurchaseOrderFilter(page_size=500))
    assert everything.total > 1, "need several orders to page through"

    first_page = repo.list_transactions(world["org_id"], PurchaseOrderFilter(page_size=1))
    assert len(first_page.items) == 1
    # The visible page shrank; the totals did not.
    assert first_page.totals.total_amount == everything.totals.total_amount
    assert first_page.totals.record_count == everything.total


def test_empty_result_set_totals_are_zero_not_null(world):
    repo = SqlPurchaseOrderRepository()
    page = repo.list_transactions(world["org_id"], PurchaseOrderFilter())
    assert page.items == []
    assert page.totals.total_amount == Decimal("0")
    assert page.totals.taxable_amount == Decimal("0")
    assert page.totals.record_count == 0
