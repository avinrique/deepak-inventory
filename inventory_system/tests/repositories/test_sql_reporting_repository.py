"""SqlReportingRepository against a live PostgreSQL database — proves the
dashboard metrics and all nine reports compute correct numbers from a
realistic, hand-verifiable seeded dataset, and that every calculation
happens as a SQL aggregate rather than a Python loop over raw rows.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.

Fixture shape (see ``world``), all created "today" so default trend
windows include everything:

- Products: WID-1 (cat Widgets, cost 10), GAD-1 (uncategorized, cost 15),
  GIZ-1 (cat Widgets, cost 5, deliberately under its minimum_stock_level).
- Purchasing: 3 orders fully received (100 x WID-1, 50 x GAD-1, 3 x
  GIZ-1), plus 1 CANCELLED order that must not count toward any total.
- Sales: SO1 (10 x WID-1, invoiced 220, paid in full -> COMPLETED), SO2 (5
  x GAD-1, invoiced 150, paid 100 -> stays FULFILLED, 50 outstanding),
  plus 1 CANCELLED order that must not count toward any total.
"""
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.domain.inventory import InventoryTransactionType
from app.domain.sales import PaymentMethod
from app.models import Category, Customer, Organization, Product, Supplier, Unit, User, Warehouse
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.reporting_repository import SqlReportingRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.purchasing import GoodsReceiptLineInput, PurchaseOrderCreate, PurchaseOrderItemInput
from app.schemas.reporting import ReportFilter
from app.schemas.sales import PaymentRequest, SalesOrderCreate, SalesOrderItemInput


def _repo():
    return SqlReportingRepository()


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Acme Co")
        session.add(org)
        session.flush()

        category = Category(organization_id=org.id, name="Widgets")
        session.add(category)
        session.flush()

        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()

        p1 = Product(organization_id=org.id, sku="WID-1", name="Widget", unit_id=unit.id,
                    category_id=category.id, purchase_price=Decimal("10"),
                    selling_price=Decimal("20"), tax_percent=Decimal("0"),
                    minimum_stock_level=Decimal("5"))
        p2 = Product(organization_id=org.id, sku="GAD-1", name="Gadget", unit_id=unit.id,
                    purchase_price=Decimal("15"), selling_price=Decimal("30"),
                    tax_percent=Decimal("0"), minimum_stock_level=Decimal("10"))
        p3 = Product(organization_id=org.id, sku="GIZ-1", name="Gizmo", unit_id=unit.id,
                    category_id=category.id, purchase_price=Decimal("5"),
                    selling_price=Decimal("8"), tax_percent=Decimal("0"),
                    minimum_stock_level=Decimal("20"))
        warehouse = Warehouse(organization_id=org.id, code="WH-A", name="Main")
        supplier = Supplier(organization_id=org.id, name="Acme Supplier")
        customer = Customer(organization_id=org.id, name="Acme Customer")
        user = User(email="clerk@example.com", hashed_password="x", full_name="Clerk")
        session.add_all([p1, p2, p3, warehouse, supplier, customer, user])
        session.flush()

        ids = {
            "org_id": org.id, "category_id": category.id, "p1_id": p1.id, "p2_id": p2.id,
            "p3_id": p3.id, "warehouse_id": warehouse.id, "supplier_id": supplier.id,
            "customer_id": customer.id, "user_id": user.id,
        }

    po_repo = SqlPurchaseOrderRepository()
    so_repo = SqlSalesOrderRepository()

    def _received_po(product_id, quantity, unit_price):
        po = po_repo.create(ids["org_id"], PurchaseOrderCreate(
            supplier_id=ids["supplier_id"], warehouse_id=ids["warehouse_id"],
            items=[PurchaseOrderItemInput(product_id=product_id, quantity_ordered=quantity,
                                          unit_price=unit_price, tax_percent=Decimal("0"))]),
            ids["user_id"])
        po_repo.submit(ids["org_id"], po.id)
        po = po_repo.approve(ids["org_id"], po.id, ids["user_id"])
        po_repo.receive_goods(ids["org_id"], po.id,
                             [GoodsReceiptLineInput(purchase_order_item_id=po.items[0].id,
                                                    quantity=quantity)], ids["user_id"])
        return po

    _received_po(ids["p1_id"], Decimal("100"), Decimal("10"))
    _received_po(ids["p2_id"], Decimal("50"), Decimal("15"))
    _received_po(ids["p3_id"], Decimal("3"), Decimal("5"))

    # A cancelled order — must not count toward supplier totals/purchases.
    cancelled_po = po_repo.create(ids["org_id"], PurchaseOrderCreate(
        supplier_id=ids["supplier_id"], warehouse_id=ids["warehouse_id"],
        items=[PurchaseOrderItemInput(product_id=ids["p1_id"], quantity_ordered=Decimal("999"),
                                      unit_price=Decimal("10"), tax_percent=Decimal("0"))]),
        ids["user_id"])
    po_repo.cancel(ids["org_id"], cancelled_po.id)

    def _sale(product_id, quantity, unit_price, tax_percent, pay_amount):
        so = so_repo.create(ids["org_id"], SalesOrderCreate(
            customer_id=ids["customer_id"], warehouse_id=ids["warehouse_id"],
            items=[SalesOrderItemInput(product_id=product_id, quantity_ordered=quantity,
                                       unit_price=unit_price, tax_percent=tax_percent)]),
            ids["user_id"])
        so_repo.confirm(ids["org_id"], so.id, ids["user_id"])
        so = so_repo.fulfill_sale(ids["org_id"], so.id, ids["user_id"])
        invoice = so_repo.generate_invoice(ids["org_id"], so.id, ids["user_id"])
        so_repo.record_payment(ids["org_id"], PaymentRequest(
            invoice_id=invoice.id, amount=pay_amount, method=PaymentMethod.CASH), ids["user_id"])
        return so, invoice

    so1, invoice1 = _sale(ids["p1_id"], Decimal("10"), Decimal("20"), Decimal("10"),
                          Decimal("220"))     # fully paid -> COMPLETED
    so2, invoice2 = _sale(ids["p2_id"], Decimal("5"), Decimal("30"), Decimal("0"),
                          Decimal("100"))      # partially paid -> stays FULFILLED

    # A cancelled sale — must not count toward customer totals/sales.
    cancelled_so = so_repo.create(ids["org_id"], SalesOrderCreate(
        customer_id=ids["customer_id"], warehouse_id=ids["warehouse_id"],
        items=[SalesOrderItemInput(product_id=ids["p2_id"], quantity_ordered=Decimal("999"),
                                   unit_price=Decimal("30"), tax_percent=Decimal("0"))]),
        ids["user_id"])
    so_repo.cancel(ids["org_id"], cancelled_so.id)

    ids["so1_id"] = so1.id
    ids["so2_id"] = so2.id
    return ids


# -- dashboard metrics -------------------------------------------------- #

def test_dashboard_totals(world):
    metrics = _repo().get_dashboard_metrics(world["org_id"], ReportFilter())

    assert metrics.total_products == 3
    assert metrics.total_inventory_units == Decimal("138")     # 90 + 45 + 3
    assert metrics.inventory_value == Decimal("1590")          # 900 + 675 + 15
    assert metrics.low_stock_count == 1                        # only GIZ-1
    assert metrics.total_sales == Decimal("370")                # 220 + 150
    assert metrics.total_purchases == Decimal("1765")           # 1000 + 750 + 15
    assert metrics.outstanding_payments == Decimal("50")        # 370 paid 320


def test_dashboard_recent_transactions_include_every_ledger_entry(world):
    metrics = _repo().get_dashboard_metrics(world["org_id"], ReportFilter())
    types = {t.transaction_type for t in metrics.recent_transactions}
    assert "PURCHASE_RECEIVED" in types
    assert "SALE" in types
    assert len(metrics.recent_transactions) == 5  # 3 receipts + 2 sales


def test_dashboard_top_selling_products_ordered_by_quantity(world):
    metrics = _repo().get_dashboard_metrics(world["org_id"], ReportFilter())
    assert [p.sku for p in metrics.top_selling_products] == ["WID-1", "GAD-1"]
    assert metrics.top_selling_products[0].quantity_sold == Decimal("10")
    assert metrics.top_selling_products[0].revenue == Decimal("200")
    assert metrics.top_selling_products[1].revenue == Decimal("150")


def test_dashboard_trends_default_to_last_30_days_and_sum_correctly(world):
    metrics = _repo().get_dashboard_metrics(world["org_id"], ReportFilter())
    assert sum((p.total for p in metrics.sales_trend), Decimal("0")) == Decimal("370")
    assert sum((p.count for p in metrics.sales_trend), 0) == 2
    assert sum((p.total for p in metrics.purchase_trend), Decimal("0")) == Decimal("1765")
    assert sum((p.count for p in metrics.purchase_trend), 0) == 3


def test_dashboard_inventory_by_category(world):
    metrics = _repo().get_dashboard_metrics(world["org_id"], ReportFilter())
    by_name = {r.category_name: r for r in metrics.inventory_by_category}
    assert by_name["Widgets"].quantity_on_hand == Decimal("93")   # 90 + 3
    assert by_name["Widgets"].value == Decimal("915")             # 900 + 15
    assert by_name["Uncategorized"].quantity_on_hand == Decimal("45")
    assert by_name["Uncategorized"].value == Decimal("675")


def test_dashboard_warehouse_filter_narrows_purchase_total(world):
    with get_session() as session:
        other_wh = Warehouse(organization_id=world["org_id"], code="WH-B", name="Other")
        session.add(other_wh)
        session.flush()
        other_wh_id = other_wh.id

    metrics = _repo().get_dashboard_metrics(world["org_id"],
                                            ReportFilter(warehouse_id=other_wh_id))
    assert metrics.total_purchases == Decimal("0")
    assert metrics.total_sales == Decimal("0")


# -- reports ------------------------------------------------------------- #

def test_stock_report_lists_every_product_warehouse_pair(world):
    result = _repo().stock_report(world["org_id"], ReportFilter())
    assert result.row_count == 3
    by_sku = {r["SKU"]: r for r in result.rows}
    assert by_sku["WID-1"]["On Hand"] == Decimal("90")     # 100 received - 10 sold
    assert by_sku["GAD-1"]["On Hand"] == Decimal("45")     # 50 received - 5 sold
    assert by_sku["GIZ-1"]["On Hand"] == Decimal("3")
    assert by_sku["WID-1"]["Available"] == by_sku["WID-1"]["On Hand"]  # nothing reserved


def test_low_stock_report_only_lists_products_below_minimum(world):
    result = _repo().low_stock_report(world["org_id"], ReportFilter())
    assert [r["SKU"] for r in result.rows] == ["GIZ-1"]
    assert result.rows[0]["Shortfall"] == Decimal("17")   # minimum 20 - on hand 3


def test_sales_report_lists_every_order_including_cancelled(world):
    # sales_report lists every order regardless of status (it shows Status
    # as a column) — unlike the dashboard/customer-report totals, which
    # deliberately exclude CANCELLED. This asserts each row's own total is
    # computed correctly, including the cancelled one (999 x 30 = 29970).
    result = _repo().sales_report(world["org_id"], ReportFilter())
    assert result.row_count == 3
    totals = {r["Total"] for r in result.rows}
    assert totals == {Decimal("220"), Decimal("150"), Decimal("29970")}
    statuses = {r["Status"] for r in result.rows}
    assert "CANCELLED" in statuses


def test_sales_report_customer_filter(world):
    result = _repo().sales_report(world["org_id"],
                                  ReportFilter(customer_id=world["customer_id"]))
    assert result.row_count == 3


def test_purchase_report_totals(world):
    # Also lists every order including the cancelled one (999 x 10 = 9990).
    result = _repo().purchase_report(world["org_id"], ReportFilter())
    totals = sorted(r["Total"] for r in result.rows if r["Total"] > 0)
    assert totals == [Decimal("15"), Decimal("750"), Decimal("1000"), Decimal("9990")]


def test_profit_report_computes_revenue_cost_and_margin(world):
    result = _repo().profit_report(world["org_id"], ReportFilter())
    by_sku = {r["SKU"]: r for r in result.rows}
    assert by_sku["WID-1"]["Revenue"] == Decimal("200")
    assert by_sku["WID-1"]["Cost"] == Decimal("100")
    assert by_sku["WID-1"]["Profit"] == Decimal("100")
    assert by_sku["WID-1"]["Margin %"] == Decimal("50")
    assert "GIZ-1" not in by_sku   # never sold — no revenue row


def test_supplier_report_excludes_cancelled_order(world):
    result = _repo().supplier_report(world["org_id"], ReportFilter())
    assert result.row_count == 1
    row = result.rows[0]
    assert row["Supplier"] == "Acme Supplier"
    assert row["Orders"] == 3   # the 999-qty cancelled PO is not counted
    assert row["Total Purchased"] == Decimal("1765")


def test_customer_report_excludes_cancelled_order(world):
    result = _repo().customer_report(world["org_id"], ReportFilter())
    assert result.row_count == 1
    row = result.rows[0]
    assert row["Customer"] == "Acme Customer"
    assert row["Orders"] == 2
    assert row["Total Purchased"] == Decimal("370")


def test_product_movement_report_for_single_product(world):
    result = _repo().product_movement_report(world["org_id"],
                                              ReportFilter(product_id=world["p1_id"]))
    assert result.row_count == 2
    types = {r["Type"] for r in result.rows}
    assert types == {"PURCHASE_RECEIVED", "SALE"}
    quantities = {r["Quantity Change"] for r in result.rows}
    assert quantities == {Decimal("100"), Decimal("-10")}


def test_inventory_valuation_report(world):
    result = _repo().inventory_valuation_report(world["org_id"], ReportFilter())
    by_sku = {r["SKU"]: r for r in result.rows}
    assert by_sku["WID-1"]["Value"] == Decimal("900")
    assert by_sku["GAD-1"]["Value"] == Decimal("675")
    assert by_sku["GIZ-1"]["Value"] == Decimal("15")


def test_inventory_valuation_report_category_filter(world):
    result = _repo().inventory_valuation_report(
        world["org_id"], ReportFilter(category_id=world["category_id"]))
    assert {r["SKU"] for r in result.rows} == {"WID-1", "GIZ-1"}
