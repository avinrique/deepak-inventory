"""End-to-end integration test chaining Purchase -> Receive Stock ->
Inventory -> Sale -> New Bill (finalize) -> Payment -> PDF -> Report in
ONE test, against a live database.

Every stage is already covered thoroughly in isolation (test_sql_purchase_
repository.py, test_sql_sales_repository.py, test_sql_reporting_repository.py,
tests/reports/test_sales_invoice_pdf.py) but nothing previously proved that
downstream reads (the reporting layer, the PDF generator) stay consistent
with upstream writes across BOTH the purchasing and sales repositories
sharing the same product/warehouse/organization — the one thing unit-per-
repository tests structurally cannot catch (e.g. a reporting query joining
the wrong FK, or a PDF pulling stale/duplicated data).

Uses the ``live_db`` fixture (tests/conftest.py) — gated on
INVENTORY_TEST_DATABASE_URL, never the app's real INVENTORY_DATABASE_URL.
"""
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.domain.purchasing import PurchaseOrderStatus
from app.domain.sales import PaymentMethod, SalesOrderStatus
from app.models import Organization, Product, Supplier, Customer, Unit, User, Warehouse
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.reporting_repository import SqlReportingRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.purchasing import GoodsReceiptLineInput, PurchaseOrderCreate, PurchaseOrderItemInput
from app.schemas.reporting import ReportFilter
from app.schemas.sales import PaymentRequest, SalesOrderCreate, SalesOrderItemInput


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Chain Test Traders")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()
        product = Product(organization_id=org.id, sku="CHAIN-1", name="Chain Widget",
                          unit_id=unit.id, purchase_price=Decimal("10"),
                          selling_price=Decimal("25"), tax_percent=Decimal("10"),
                          minimum_stock_level=Decimal("0"))
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        supplier = Supplier(organization_id=org.id, name="Chain Supplier")
        customer = Customer(organization_id=org.id, name="Chain Customer")
        user = User(email="chain-clerk@example.com", username="chainclerk",
                   hashed_password="x", full_name="Chain Clerk")
        session.add_all([product, warehouse, supplier, customer, user])
        session.flush()
        return {
            "org_id": org.id, "product_id": product.id, "warehouse_id": warehouse.id,
            "supplier_id": supplier.id, "customer_id": customer.id, "user_id": user.id,
        }


def test_purchase_receive_sell_pay_pdf_report_chain(world, tmp_path):
    org_id, product_id, warehouse_id = world["org_id"], world["product_id"], world["warehouse_id"]
    user_id = world["user_id"]

    # -- 1. Purchase: create, submit, approve, receive 50 units ----------#
    purchase_repo = SqlPurchaseOrderRepository()
    po = purchase_repo.create(org_id, PurchaseOrderCreate(
        supplier_id=world["supplier_id"], warehouse_id=warehouse_id,
        items=[PurchaseOrderItemInput(product_id=product_id, quantity_ordered=Decimal("50"),
                                     unit_price=Decimal("10"), tax_percent=Decimal("10"))]),
        user_id)
    purchase_repo.submit(org_id, po.id)
    purchase_repo.approve(org_id, po.id, user_id)
    purchase_repo.receive_goods(
        org_id, po.id,
        [GoodsReceiptLineInput(purchase_order_item_id=po.items[0].id, quantity=Decimal("50"))],
        user_id, notes="chain test receipt")

    received_po = purchase_repo.get_by_id(org_id, po.id)
    assert received_po.status == PurchaseOrderStatus.RECEIVED

    # -- 2. Inventory: 50 units now on hand at the warehouse -------------#
    inventory_repo = SqlInventoryRepository()
    level_after_receipt = inventory_repo.get_level(org_id, product_id, warehouse_id)
    assert level_after_receipt.quantity_on_hand == Decimal("50")

    # -- 3. Sale: New Bill for 20 units, confirm + fulfill ----------------#
    sales_repo = SqlSalesOrderRepository()
    so = sales_repo.create(org_id, SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=warehouse_id,
        items=[SalesOrderItemInput(product_id=product_id, quantity_ordered=Decimal("20"),
                                  unit_price=Decimal("25"), tax_percent=Decimal("10"))]),
        user_id)
    sales_repo.confirm(org_id, so.id, user_id)
    fulfilled = sales_repo.fulfill_sale(org_id, so.id, user_id)
    assert fulfilled.status == SalesOrderStatus.FULFILLED

    level_after_sale = inventory_repo.get_level(org_id, product_id, warehouse_id)
    assert level_after_sale.quantity_on_hand == Decimal("30"), (
        "50 received - 20 sold must leave exactly 30 on hand")

    # -- 4. Invoice + full Payment -----------------------------------------#
    invoice = sales_repo.generate_invoice(org_id, so.id, user_id)
    assert invoice.total_amount == Decimal("550.00")  # 20 * 25 * 1.10

    sales_repo.record_payment(
        org_id, PaymentRequest(invoice_id=invoice.id, amount=invoice.total_amount,
                              method=PaymentMethod.CASH), user_id)
    paid_order = sales_repo.get_by_id(org_id, so.id)
    assert paid_order.status == SalesOrderStatus.COMPLETED, "fully paid order must be COMPLETED"

    doc = sales_repo.get_invoice_document(org_id, invoice.id)
    assert doc.amount_paid == invoice.total_amount
    assert doc.amount_due == Decimal("0")
    from app.domain.sales import InvoicePaymentStatus
    assert doc.payment_status == InvoicePaymentStatus.PAID

    # -- 5. PDF: the invoice must render without error --------------------#
    from app.reports.sales_invoice_pdf import render_invoice_pdf

    pdf_path = str(tmp_path / "chain-invoice.pdf")
    render_invoice_pdf(doc, pdf_path)
    import os
    assert os.path.exists(pdf_path)
    assert os.path.getsize(pdf_path) > 0

    # -- 6. Reports: both sides must show up, consistently ----------------#
    reporting_repo = SqlReportingRepository()

    sales_result = reporting_repo.sales_report(org_id, ReportFilter())
    assert any(row["Total"] == Decimal("550.00") for row in sales_result.rows), (
        "the sale generated above must appear in the sales report with the matching total")

    purchase_result = reporting_repo.purchase_report(org_id, ReportFilter())
    assert any(row["Total"] == Decimal("550.00") for row in purchase_result.rows), (
        "50 units @ 10 + 10% tax = 550 must appear in the purchase report")

    stock_result = reporting_repo.stock_report(org_id, ReportFilter())
    matching_stock_rows = [r for r in stock_result.rows if r.get("SKU") == "CHAIN-1"]
    assert matching_stock_rows, "the product must appear in the stock report"
    assert matching_stock_rows[0]["On Hand"] == Decimal("30"), (
        "stock report must reflect the same 30-on-hand the inventory ledger shows directly")
