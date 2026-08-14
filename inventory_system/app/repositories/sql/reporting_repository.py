"""SQLAlchemy-backed ReportingRepository — every number here comes out of
a SQL aggregate (func.sum/count/max, GROUP BY, joined subqueries), never
by pulling a table's raw rows into Python and summing them in a loop.
Where a report needs a per-order total (Sales/Purchase report, Supplier/
Customer report), that total is computed via a GROUP BY subquery on the
*item* table and joined in — one row per order reaches Python, not one row
per line item.

The one deliberate exception to "aggregate, don't list" is
product_movement_report, which is a ledger *listing* by nature (that's
what a movement report is) — it's capped at 1000 most-recent-first rows so
an unfiltered call still can't pull an unbounded table into memory.

Costing note: profit_report uses each product's *current*
Product.purchase_price as its cost basis, not the historical cost at the
time a specific unit was purchased. That's a deliberate simplification —
real FIFO/weighted-average lot costing would need to track cost per
inventory lot, which nothing in this schema does yet — called out here so
it isn't mistaken for precise COGS accounting.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func

from app.database.session import get_session
from app.domain.product import ProductStatus
from app.domain.purchasing import PurchaseOrderStatus
from app.domain.sales import SalesOrderStatus
from app.models import (
    Category,
    Customer,
    Inventory,
    InventoryTransaction,
    Invoice,
    Payment,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    SalesOrder,
    SalesOrderItem,
    Supplier,
    User,
    Warehouse,
)
from app.schemas.reporting import (
    CategoryValueRow,
    DashboardMetrics,
    RecentTransactionRow,
    ReportFilter,
    ReportResult,
    TopProductRow,
    TrendPoint,
)

_RECENT_TRANSACTIONS_LIMIT = 20
_TOP_PRODUCTS_LIMIT = 5
_PRODUCT_MOVEMENT_LIMIT = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value) -> Decimal:
    return Decimal(value) if value is not None else Decimal("0")


class SqlReportingRepository:
    # -- shared aggregate subqueries -------------------------------------#
    def _sales_order_totals_subquery(self, db):
        # Discount is applied before tax (tax is charged on the discounted
        # price) — see app.domain.sales.line_tax_after_discount, the same
        # rule this SQL mirrors.
        list_subtotal = SalesOrderItem.quantity_ordered * SalesOrderItem.unit_price
        discount = list_subtotal * SalesOrderItem.discount_percent / 100
        after_discount = list_subtotal - discount
        tax = after_discount * SalesOrderItem.tax_percent / 100
        return (db.query(
                    SalesOrderItem.sales_order_id.label("sales_order_id"),
                    func.sum(list_subtotal).label("subtotal"),
                    func.sum(discount).label("discount_amount"),
                    func.sum(tax).label("tax_amount"),
                    func.sum(after_discount + tax).label("total_amount"))
               .group_by(SalesOrderItem.sales_order_id)
               .subquery())

    def _purchase_order_totals_subquery(self, db):
        return (db.query(
                    PurchaseOrderItem.purchase_order_id.label("purchase_order_id"),
                    func.sum(PurchaseOrderItem.quantity_ordered * PurchaseOrderItem.unit_price)
                        .label("subtotal"),
                    func.sum(PurchaseOrderItem.quantity_ordered * PurchaseOrderItem.unit_price
                            * PurchaseOrderItem.tax_percent / 100).label("tax_amount"),
                    func.sum(PurchaseOrderItem.quantity_ordered * PurchaseOrderItem.unit_price
                            * (1 + PurchaseOrderItem.tax_percent / 100)).label("total_amount"))
               .group_by(PurchaseOrderItem.purchase_order_id)
               .subquery())

    def _item_filter_subquery(self, db, item_model, order_id_col, filter: ReportFilter):
        """Order ids whose items match product_id/category_id — or None
        if neither filter is set, so callers can skip the join entirely.
        """
        if not filter.product_id and not filter.category_id:
            return None
        q = db.query(order_id_col)
        if filter.product_id:
            q = q.filter(item_model.product_id == filter.product_id)
        if filter.category_id:
            q = q.join(Product, item_model.product_id == Product.id).filter(
                Product.category_id == filter.category_id)
        return q.scalar_subquery()

    # -- dashboard ---------------------------------------------------------#
    def get_dashboard_metrics(self, organization_id: uuid.UUID,
                              filter: ReportFilter) -> DashboardMetrics:
        with get_session() as db:
            total_products = (db.query(func.count(Product.id))
                             .filter(Product.organization_id == organization_id,
                                    Product.status == ProductStatus.ACTIVE)
                             .scalar()) or 0

            inv_agg = (db.query(
                        func.coalesce(func.sum(Inventory.quantity_on_hand), 0).label("units"),
                        func.coalesce(func.sum(Inventory.quantity_on_hand * Product.purchase_price),
                                     0).label("value"))
                      .select_from(Inventory)
                      .join(Product, Inventory.product_id == Product.id)
                      .filter(Inventory.organization_id == organization_id)
                      .one())

            low_stock_count = self._low_stock_count(db, organization_id, filter)
            total_sales = self._total_sales(db, organization_id, filter)
            total_purchases = self._total_purchases_received(db, organization_id, filter)
            outstanding = self._outstanding_payments(db, organization_id)
            recent = self._recent_transactions(db, organization_id, _RECENT_TRANSACTIONS_LIMIT)
            top_products = self._top_selling_products(db, organization_id, filter,
                                                       _TOP_PRODUCTS_LIMIT)

            trend_from, trend_to = filter.date_from, filter.date_to
            if trend_from is None and trend_to is None:
                # No range given — default trend charts to the last 30
                # days rather than every day the organization has ever
                # existed, which could be thousands of chart points.
                trend_to = date.today()
                trend_from = trend_to - timedelta(days=30)
            sales_trend = self._sales_trend(db, organization_id, trend_from, trend_to)
            purchase_trend = self._purchase_trend(db, organization_id, trend_from, trend_to)

            inventory_by_category = self._inventory_by_category(db, organization_id, filter)

            return DashboardMetrics(
                total_products=total_products, total_inventory_units=_dec(inv_agg.units),
                inventory_value=_dec(inv_agg.value), low_stock_count=low_stock_count,
                total_sales=total_sales, total_purchases=total_purchases,
                outstanding_payments=outstanding, recent_transactions=recent,
                top_selling_products=top_products, sales_trend=sales_trend,
                purchase_trend=purchase_trend, inventory_by_category=inventory_by_category)

    def _low_stock_count(self, db, organization_id, filter: ReportFilter) -> int:
        inv_totals = (db.query(Inventory.product_id.label("product_id"),
                               func.sum(Inventory.quantity_on_hand).label("on_hand"))
                     .filter(Inventory.organization_id == organization_id))
        if filter.warehouse_id:
            inv_totals = inv_totals.filter(Inventory.warehouse_id == filter.warehouse_id)
        inv_totals = inv_totals.group_by(Inventory.product_id).subquery()

        count = (db.query(func.count(Product.id))
                .outerjoin(inv_totals, inv_totals.c.product_id == Product.id)
                .filter(Product.organization_id == organization_id,
                       Product.status == ProductStatus.ACTIVE,
                       func.coalesce(inv_totals.c.on_hand, 0) < Product.minimum_stock_level)
                .scalar())
        return count or 0

    def _total_sales(self, db, organization_id, filter: ReportFilter) -> Decimal:
        totals = self._sales_order_totals_subquery(db)
        query = (db.query(func.coalesce(func.sum(totals.c.total_amount), 0))
                .select_from(SalesOrder)
                .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
                .filter(SalesOrder.organization_id == organization_id,
                       SalesOrder.status != SalesOrderStatus.CANCELLED))
        if filter.date_from:
            query = query.filter(func.date(SalesOrder.created_at) >= filter.date_from)
        if filter.date_to:
            query = query.filter(func.date(SalesOrder.created_at) <= filter.date_to)
        if filter.warehouse_id:
            query = query.filter(SalesOrder.warehouse_id == filter.warehouse_id)
        if filter.customer_id:
            query = query.filter(SalesOrder.customer_id == filter.customer_id)
        return _dec(query.scalar())

    def _total_purchases_received(self, db, organization_id, filter: ReportFilter) -> Decimal:
        # Value of goods actually received (quantity_received), not merely
        # ordered — "Total purchases" should reflect stock that actually
        # entered the business, matching how "Total sales" only counts
        # non-cancelled orders.
        query = (db.query(func.coalesce(
                    func.sum(PurchaseOrderItem.quantity_received * PurchaseOrderItem.unit_price),
                    0))
                .select_from(PurchaseOrderItem)
                .join(PurchaseOrder, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
                .filter(PurchaseOrder.organization_id == organization_id,
                       PurchaseOrder.status != PurchaseOrderStatus.CANCELLED))
        if filter.date_from:
            query = query.filter(func.date(PurchaseOrder.created_at) >= filter.date_from)
        if filter.date_to:
            query = query.filter(func.date(PurchaseOrder.created_at) <= filter.date_to)
        if filter.warehouse_id:
            query = query.filter(PurchaseOrder.warehouse_id == filter.warehouse_id)
        if filter.supplier_id:
            query = query.filter(PurchaseOrder.supplier_id == filter.supplier_id)
        return _dec(query.scalar())

    def _outstanding_payments(self, db, organization_id) -> Decimal:
        invoiced = (db.query(func.coalesce(func.sum(Invoice.total_amount), 0))
                   .filter(Invoice.organization_id == organization_id).scalar())
        paid = (db.query(func.coalesce(func.sum(Payment.amount), 0))
               .join(Invoice, Payment.invoice_id == Invoice.id)
               .filter(Invoice.organization_id == organization_id).scalar())
        return _dec(invoiced) - _dec(paid)

    def _recent_transactions(self, db, organization_id, limit: int) -> list[RecentTransactionRow]:
        rows = (db.query(InventoryTransaction, Product, Warehouse, User)
               .join(Product, InventoryTransaction.product_id == Product.id)
               .join(Warehouse, InventoryTransaction.warehouse_id == Warehouse.id)
               .join(User, InventoryTransaction.performed_by == User.id)
               .filter(InventoryTransaction.organization_id == organization_id)
               .order_by(InventoryTransaction.created_at.desc())
               .limit(limit).all())
        return [RecentTransactionRow(
                    id=tx.id, transaction_type=tx.transaction_type.value, product_sku=p.sku,
                    product_name=p.name, warehouse_code=w.code, quantity_change=tx.quantity_change,
                    performed_by_email=u.email, created_at=tx.created_at)
               for tx, p, w, u in rows]

    def _top_selling_products(self, db, organization_id, filter: ReportFilter,
                              limit: int) -> list[TopProductRow]:
        discounted_revenue = (SalesOrderItem.quantity_fulfilled * SalesOrderItem.unit_price
                             * (1 - SalesOrderItem.discount_percent / 100))
        query = (db.query(SalesOrderItem.product_id.label("product_id"),
                          func.sum(SalesOrderItem.quantity_fulfilled).label("qty"),
                          func.sum(discounted_revenue).label("revenue"))
                .join(SalesOrder, SalesOrderItem.sales_order_id == SalesOrder.id)
                .filter(SalesOrder.organization_id == organization_id,
                       SalesOrderItem.quantity_fulfilled > 0))
        if filter.date_from:
            query = query.filter(func.date(SalesOrder.created_at) >= filter.date_from)
        if filter.date_to:
            query = query.filter(func.date(SalesOrder.created_at) <= filter.date_to)
        if filter.warehouse_id:
            query = query.filter(SalesOrder.warehouse_id == filter.warehouse_id)
        query = (query.group_by(SalesOrderItem.product_id)
                .order_by(func.sum(SalesOrderItem.quantity_fulfilled).desc())
                .limit(limit))
        agg_rows = query.all()
        if not agg_rows:
            return []
        products = {p.id: p for p in db.query(Product)
                   .filter(Product.id.in_([r.product_id for r in agg_rows])).all()}
        return [TopProductRow(product_id=r.product_id, sku=products[r.product_id].sku,
                              name=products[r.product_id].name, quantity_sold=_dec(r.qty),
                              revenue=_dec(r.revenue))
               for r in agg_rows if r.product_id in products]

    def _sales_trend(self, db, organization_id, date_from, date_to) -> list[TrendPoint]:
        totals = self._sales_order_totals_subquery(db)
        bucket = func.date(SalesOrder.created_at)
        query = (db.query(bucket.label("bucket"),
                          func.coalesce(func.sum(totals.c.total_amount), 0).label("total"),
                          func.count(SalesOrder.id).label("count"))
                .select_from(SalesOrder)
                .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
                .filter(SalesOrder.organization_id == organization_id,
                       SalesOrder.status != SalesOrderStatus.CANCELLED))
        if date_from:
            query = query.filter(bucket >= date_from)
        if date_to:
            query = query.filter(bucket <= date_to)
        query = query.group_by(bucket).order_by(bucket)
        return [TrendPoint(bucket=r.bucket, total=_dec(r.total), count=r.count)
               for r in query.all()]

    def _purchase_trend(self, db, organization_id, date_from, date_to) -> list[TrendPoint]:
        totals = self._purchase_order_totals_subquery(db)
        bucket = func.date(PurchaseOrder.created_at)
        query = (db.query(bucket.label("bucket"),
                          func.coalesce(func.sum(totals.c.total_amount), 0).label("total"),
                          func.count(PurchaseOrder.id).label("count"))
                .select_from(PurchaseOrder)
                .outerjoin(totals, totals.c.purchase_order_id == PurchaseOrder.id)
                .filter(PurchaseOrder.organization_id == organization_id,
                       PurchaseOrder.status != PurchaseOrderStatus.CANCELLED))
        if date_from:
            query = query.filter(bucket >= date_from)
        if date_to:
            query = query.filter(bucket <= date_to)
        query = query.group_by(bucket).order_by(bucket)
        return [TrendPoint(bucket=r.bucket, total=_dec(r.total), count=r.count)
               for r in query.all()]

    def _inventory_by_category(self, db, organization_id,
                               filter: ReportFilter) -> list[CategoryValueRow]:
        query = (db.query(Category.id.label("category_id"),
                          Category.name.label("category_name"),
                          func.sum(Inventory.quantity_on_hand).label("units"),
                          func.sum(Inventory.quantity_on_hand * Product.purchase_price)
                              .label("value"))
                .select_from(Inventory)
                .join(Product, Inventory.product_id == Product.id)
                .outerjoin(Category, Product.category_id == Category.id)
                .filter(Inventory.organization_id == organization_id))
        if filter.warehouse_id:
            query = query.filter(Inventory.warehouse_id == filter.warehouse_id)
        query = query.group_by(Category.id, Category.name)
        rows = [CategoryValueRow(category_id=r.category_id,
                                 category_name=r.category_name or "Uncategorized",
                                 quantity_on_hand=_dec(r.units), value=_dec(r.value))
               for r in query.all()]
        return sorted(rows, key=lambda r: r.value, reverse=True)

    # -- reports -------------------------------------------------------- #
    def stock_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            query = (db.query(Inventory, Product, Warehouse)
                    .join(Product, Inventory.product_id == Product.id)
                    .join(Warehouse, Inventory.warehouse_id == Warehouse.id)
                    .filter(Inventory.organization_id == organization_id))
            if filter.warehouse_id:
                query = query.filter(Inventory.warehouse_id == filter.warehouse_id)
            if filter.product_id:
                query = query.filter(Inventory.product_id == filter.product_id)
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            query = query.order_by(Product.sku, Warehouse.code)

            rows = [{"SKU": p.sku, "Product": p.name, "Warehouse": w.code,
                    "On Hand": inv.quantity_on_hand, "Reserved": inv.quantity_reserved,
                    "Available": inv.quantity_on_hand - inv.quantity_reserved}
                   for inv, p, w in query.all()]
            return ReportResult(title="Stock Report", generated_at=_now(),
                               columns=["SKU", "Product", "Warehouse", "On Hand", "Reserved",
                                       "Available"],
                               rows=rows)

    def sales_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            totals = self._sales_order_totals_subquery(db)
            query = (db.query(SalesOrder, Customer, Warehouse, totals.c.subtotal,
                              totals.c.discount_amount, totals.c.tax_amount,
                              totals.c.total_amount)
                    .join(Customer, SalesOrder.customer_id == Customer.id)
                    .join(Warehouse, SalesOrder.warehouse_id == Warehouse.id)
                    .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
                    .filter(SalesOrder.organization_id == organization_id))
            if filter.date_from:
                query = query.filter(func.date(SalesOrder.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(SalesOrder.created_at) <= filter.date_to)
            if filter.warehouse_id:
                query = query.filter(SalesOrder.warehouse_id == filter.warehouse_id)
            if filter.customer_id:
                query = query.filter(SalesOrder.customer_id == filter.customer_id)
            item_sub = self._item_filter_subquery(db, SalesOrderItem,
                                                  SalesOrderItem.sales_order_id, filter)
            if item_sub is not None:
                query = query.filter(SalesOrder.id.in_(item_sub))
            query = query.order_by(SalesOrder.created_at.desc())

            rows = [{"Order Date": so.created_at, "Customer": c.name, "Warehouse": w.code,
                    "Status": so.status.value, "Subtotal": _dec(subtotal),
                    "Discount": _dec(discount), "Tax": _dec(tax), "Total": _dec(total)}
                   for so, c, w, subtotal, discount, tax, total in query.all()]
            return ReportResult(title="Sales Report", generated_at=_now(),
                               columns=["Order Date", "Customer", "Warehouse", "Status",
                                       "Subtotal", "Discount", "Tax", "Total"],
                               rows=rows)

    def purchase_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            totals = self._purchase_order_totals_subquery(db)
            query = (db.query(PurchaseOrder, Supplier, Warehouse, totals.c.subtotal,
                              totals.c.tax_amount, totals.c.total_amount)
                    .join(Supplier, PurchaseOrder.supplier_id == Supplier.id)
                    .join(Warehouse, PurchaseOrder.warehouse_id == Warehouse.id)
                    .outerjoin(totals, totals.c.purchase_order_id == PurchaseOrder.id)
                    .filter(PurchaseOrder.organization_id == organization_id))
            if filter.date_from:
                query = query.filter(func.date(PurchaseOrder.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(PurchaseOrder.created_at) <= filter.date_to)
            if filter.warehouse_id:
                query = query.filter(PurchaseOrder.warehouse_id == filter.warehouse_id)
            if filter.supplier_id:
                query = query.filter(PurchaseOrder.supplier_id == filter.supplier_id)
            item_sub = self._item_filter_subquery(db, PurchaseOrderItem,
                                                  PurchaseOrderItem.purchase_order_id, filter)
            if item_sub is not None:
                query = query.filter(PurchaseOrder.id.in_(item_sub))
            query = query.order_by(PurchaseOrder.created_at.desc())

            rows = [{"Order Date": po.created_at, "Supplier": s.name, "Warehouse": w.code,
                    "Status": po.status.value, "Subtotal": _dec(subtotal), "Tax": _dec(tax),
                    "Total": _dec(total)}
                   for po, s, w, subtotal, tax, total in query.all()]
            return ReportResult(title="Purchase Report", generated_at=_now(),
                               columns=["Order Date", "Supplier", "Warehouse", "Status",
                                       "Subtotal", "Tax", "Total"],
                               rows=rows)

    def profit_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            discounted_revenue = (SalesOrderItem.quantity_fulfilled * SalesOrderItem.unit_price
                                 * (1 - SalesOrderItem.discount_percent / 100))
            query = (db.query(SalesOrderItem.product_id.label("product_id"),
                              func.sum(SalesOrderItem.quantity_fulfilled).label("qty"),
                              func.sum(discounted_revenue).label("revenue"),
                              func.sum(SalesOrderItem.quantity_fulfilled * Product.purchase_price)
                                  .label("cost"))
                    .join(SalesOrder, SalesOrderItem.sales_order_id == SalesOrder.id)
                    .join(Product, SalesOrderItem.product_id == Product.id)
                    .filter(SalesOrder.organization_id == organization_id,
                           SalesOrderItem.quantity_fulfilled > 0))
            if filter.date_from:
                query = query.filter(func.date(SalesOrder.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(SalesOrder.created_at) <= filter.date_to)
            if filter.warehouse_id:
                query = query.filter(SalesOrder.warehouse_id == filter.warehouse_id)
            if filter.product_id:
                query = query.filter(SalesOrderItem.product_id == filter.product_id)
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            query = (query.group_by(SalesOrderItem.product_id)
                    .order_by(func.sum(discounted_revenue).desc()))
            agg = query.all()
            products = ({p.id: p for p in db.query(Product)
                       .filter(Product.id.in_([r.product_id for r in agg])).all()} if agg else {})

            rows = []
            for r in agg:
                product = products.get(r.product_id)
                if product is None:
                    continue
                revenue, cost = _dec(r.revenue), _dec(r.cost)
                profit = revenue - cost
                margin = (profit / revenue * 100) if revenue else Decimal("0")
                rows.append({"SKU": product.sku, "Product": product.name,
                            "Quantity Sold": _dec(r.qty), "Revenue": revenue, "Cost": cost,
                            "Profit": profit, "Margin %": margin})
            return ReportResult(title="Profit Report", generated_at=_now(),
                               columns=["SKU", "Product", "Quantity Sold", "Revenue", "Cost",
                                       "Profit", "Margin %"],
                               rows=rows)

    def low_stock_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            inv_totals = (db.query(Inventory.product_id.label("product_id"),
                                   func.sum(Inventory.quantity_on_hand).label("on_hand"))
                         .filter(Inventory.organization_id == organization_id))
            if filter.warehouse_id:
                inv_totals = inv_totals.filter(Inventory.warehouse_id == filter.warehouse_id)
            inv_totals = inv_totals.group_by(Inventory.product_id).subquery()

            query = (db.query(Product, func.coalesce(inv_totals.c.on_hand, 0).label("on_hand"))
                    .outerjoin(inv_totals, inv_totals.c.product_id == Product.id)
                    .filter(Product.organization_id == organization_id,
                           Product.status == ProductStatus.ACTIVE,
                           func.coalesce(inv_totals.c.on_hand, 0) < Product.minimum_stock_level))
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            if filter.product_id:
                query = query.filter(Product.id == filter.product_id)
            query = query.order_by(Product.sku)

            rows = []
            for product, on_hand in query.all():
                on_hand_dec = _dec(on_hand)
                rows.append({"SKU": product.sku, "Product": product.name,
                            "On Hand": on_hand_dec, "Minimum": product.minimum_stock_level,
                            "Shortfall": product.minimum_stock_level - on_hand_dec})
            return ReportResult(title="Low Stock Report", generated_at=_now(),
                               columns=["SKU", "Product", "On Hand", "Minimum", "Shortfall"],
                               rows=rows)

    def supplier_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            totals = self._purchase_order_totals_subquery(db)
            query = (db.query(Supplier.id.label("supplier_id"), Supplier.name.label("name"),
                              func.count(PurchaseOrder.id).label("orders"),
                              func.coalesce(func.sum(totals.c.total_amount), 0).label("total"),
                              func.max(PurchaseOrder.created_at).label("last_order"))
                    .select_from(Supplier)
                    .join(PurchaseOrder, PurchaseOrder.supplier_id == Supplier.id)
                    .outerjoin(totals, totals.c.purchase_order_id == PurchaseOrder.id)
                    .filter(Supplier.organization_id == organization_id,
                           PurchaseOrder.status != PurchaseOrderStatus.CANCELLED))
            if filter.date_from:
                query = query.filter(func.date(PurchaseOrder.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(PurchaseOrder.created_at) <= filter.date_to)
            if filter.supplier_id:
                query = query.filter(Supplier.id == filter.supplier_id)
            query = (query.group_by(Supplier.id, Supplier.name)
                    .order_by(func.coalesce(func.sum(totals.c.total_amount), 0).desc()))

            rows = [{"Supplier": r.name, "Orders": r.orders, "Total Purchased": _dec(r.total),
                    "Last Order Date": r.last_order.date() if r.last_order else None}
                   for r in query.all()]
            return ReportResult(title="Supplier Report", generated_at=_now(),
                               columns=["Supplier", "Orders", "Total Purchased",
                                       "Last Order Date"],
                               rows=rows)

    def customer_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            totals = self._sales_order_totals_subquery(db)
            query = (db.query(Customer.id.label("customer_id"), Customer.name.label("name"),
                              func.count(SalesOrder.id).label("orders"),
                              func.coalesce(func.sum(totals.c.total_amount), 0).label("total"),
                              func.max(SalesOrder.created_at).label("last_order"))
                    .select_from(Customer)
                    .join(SalesOrder, SalesOrder.customer_id == Customer.id)
                    .outerjoin(totals, totals.c.sales_order_id == SalesOrder.id)
                    .filter(Customer.organization_id == organization_id,
                           SalesOrder.status != SalesOrderStatus.CANCELLED))
            if filter.date_from:
                query = query.filter(func.date(SalesOrder.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(SalesOrder.created_at) <= filter.date_to)
            if filter.customer_id:
                query = query.filter(Customer.id == filter.customer_id)
            query = (query.group_by(Customer.id, Customer.name)
                    .order_by(func.coalesce(func.sum(totals.c.total_amount), 0).desc()))

            rows = [{"Customer": r.name, "Orders": r.orders, "Total Purchased": _dec(r.total),
                    "Last Order Date": r.last_order.date() if r.last_order else None}
                   for r in query.all()]
            return ReportResult(title="Customer Report", generated_at=_now(),
                               columns=["Customer", "Orders", "Total Purchased",
                                       "Last Order Date"],
                               rows=rows)

    def product_movement_report(self, organization_id: uuid.UUID,
                                filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            query = (db.query(InventoryTransaction, Product, Warehouse, User)
                    .join(Product, InventoryTransaction.product_id == Product.id)
                    .join(Warehouse, InventoryTransaction.warehouse_id == Warehouse.id)
                    .join(User, InventoryTransaction.performed_by == User.id)
                    .filter(InventoryTransaction.organization_id == organization_id))
            if filter.date_from:
                query = query.filter(func.date(InventoryTransaction.created_at) >= filter.date_from)
            if filter.date_to:
                query = query.filter(func.date(InventoryTransaction.created_at) <= filter.date_to)
            if filter.product_id:
                query = query.filter(InventoryTransaction.product_id == filter.product_id)
            if filter.warehouse_id:
                query = query.filter(InventoryTransaction.warehouse_id == filter.warehouse_id)
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            query = (query.order_by(InventoryTransaction.created_at.desc())
                    .limit(_PRODUCT_MOVEMENT_LIMIT))

            rows = [{"Date": tx.created_at, "Type": tx.transaction_type.value, "SKU": p.sku,
                    "Product": p.name, "Warehouse": w.code, "Quantity Change": tx.quantity_change,
                    "On Hand After": tx.quantity_on_hand_after, "Performed By": u.email}
                   for tx, p, w, u in query.all()]
            return ReportResult(
                title=f"Product Movement Report (most recent {_PRODUCT_MOVEMENT_LIMIT})",
                generated_at=_now(),
                columns=["Date", "Type", "SKU", "Product", "Warehouse", "Quantity Change",
                        "On Hand After", "Performed By"],
                rows=rows)

    def inventory_valuation_report(self, organization_id: uuid.UUID,
                                   filter: ReportFilter) -> ReportResult:
        with get_session() as db:
            query = (db.query(Inventory, Product, Warehouse)
                    .join(Product, Inventory.product_id == Product.id)
                    .join(Warehouse, Inventory.warehouse_id == Warehouse.id)
                    .filter(Inventory.organization_id == organization_id))
            if filter.warehouse_id:
                query = query.filter(Inventory.warehouse_id == filter.warehouse_id)
            if filter.product_id:
                query = query.filter(Inventory.product_id == filter.product_id)
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            query = query.order_by(Product.sku, Warehouse.code)

            rows = [{"SKU": p.sku, "Product": p.name, "Warehouse": w.code,
                    "Quantity": inv.quantity_on_hand, "Unit Cost": p.purchase_price,
                    "Value": inv.quantity_on_hand * p.purchase_price}
                   for inv, p, w in query.all()]
            return ReportResult(title="Inventory Valuation Report", generated_at=_now(),
                               columns=["SKU", "Product", "Warehouse", "Quantity", "Unit Cost",
                                       "Value"],
                               rows=rows)
