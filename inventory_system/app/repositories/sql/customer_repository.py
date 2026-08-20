"""SQLAlchemy-backed CustomerRepository — real implementation, no excel/
equivalent (customers, distinct from the legacy Excel "Party" concept, are
a new capability this module introduces — see SupplierRepository for the
purchasing-side analog).

get_balance/get_history never invent a price/tax/total calculation: invoiced
exposure sums Invoice.total_amount/Payment.amount directly (the exact same
columns app.repositories.sql.sales_repository.record_payment/
generate_invoice write); not-yet-invoiced exposure sums SalesOrderItem
through the same app.domain.sales.line_total_after_discount every order/
invoice line is already priced with. Billing stays the one place that
formula and those totals are authored — this module only aggregates them
per customer, same relationship
app.repositories.sql.reporting_repository._outstanding_payments has to it
(org-wide instead of per-customer).
"""
import uuid
from decimal import Decimal

from sqlalchemy import func

from app.database.session import get_session
from app.domain.sales import SalesOrderStatus, line_total_after_discount
from app.models import Customer, Invoice, Payment, SalesOrder, SalesReturn
from app.schemas.sales import (
    CustomerBalance,
    CustomerCreate,
    CustomerOut,
    CustomerTransaction,
    CustomerUpdate,
)


def compute_customer_balance(db, organization_id: uuid.UUID, customer: Customer) -> CustomerBalance:
    """Shared by SqlCustomerRepository.get_balance (its own, unlocked,
    read-only session) and SqlSalesOrderRepository.create (which locks
    ``customer`` FOR UPDATE first, in the SAME transaction as the order
    insert, specifically to close the credit-limit TOCTOU race two
    concurrent order creations for the same customer would otherwise hit —
    see that method's docstring). Takes an already-open session and an
    already-loaded Customer row rather than opening its own, so a caller
    that needs the lock held across both the read and the following write
    can do so.
    """
    invoiced = (db.query(func.coalesce(func.sum(Invoice.total_amount), 0))
               .join(SalesOrder, Invoice.sales_order_id == SalesOrder.id)
               .filter(SalesOrder.organization_id == organization_id,
                      SalesOrder.customer_id == customer.id).scalar())
    paid = (db.query(func.coalesce(func.sum(Payment.amount), 0))
           .join(Invoice, Payment.invoice_id == Invoice.id)
           .join(SalesOrder, Invoice.sales_order_id == SalesOrder.id)
           .filter(SalesOrder.organization_id == organization_id,
                  SalesOrder.customer_id == customer.id).scalar())
    invoiced_total = Decimal(invoiced)
    paid_total = Decimal(paid)

    # Every non-CANCELLED order that doesn't have an Invoice yet —
    # DRAFT/CONFIRMED, or FULFILLED but not yet invoiced — is real
    # credit exposure the moment it's created, not only once
    # Billing generates the invoice. Summed via the same per-line
    # formula an invoice/order total is always computed with (see
    # app.domain.sales.line_total_after_discount), never a
    # separately-invented total.
    pending_orders = (
        db.query(SalesOrder)
        .outerjoin(Invoice, Invoice.sales_order_id == SalesOrder.id)
        .filter(SalesOrder.organization_id == organization_id,
               SalesOrder.customer_id == customer.id,
               SalesOrder.status != SalesOrderStatus.CANCELLED,
               Invoice.id.is_(None))
        .all())
    pending_orders_total = sum(
        (line_total_after_discount(item.quantity_ordered, item.unit_price,
                                   item.discount_percent, item.tax_percent)
        for so in pending_orders for item in so.items), Decimal("0"))

    # Returns credit the customer back for goods no longer kept —
    # netted against exposure the same way a payment is, so a
    # return actually reduces outstanding_balance instead of only
    # restoring stock. See SalesReturn.credit_amount.
    returns_credit = (db.query(func.coalesce(func.sum(SalesReturn.credit_amount), 0))
                     .filter(SalesReturn.organization_id == organization_id,
                            SalesReturn.sales_order_id.in_(
                                db.query(SalesOrder.id)
                                .filter_by(organization_id=organization_id,
                                          customer_id=customer.id)))
                     .scalar())
    returns_credit_total = Decimal(returns_credit)

    outstanding = (customer.opening_balance + invoiced_total + pending_orders_total
                  - paid_total - returns_credit_total)

    return CustomerBalance(
        customer_id=customer.id, opening_balance=customer.opening_balance,
        invoiced_total=invoiced_total, pending_orders_total=pending_orders_total,
        paid_total=paid_total, returns_credit_total=returns_credit_total,
        outstanding_balance=outstanding, credit_limit=customer.credit_limit)


def _to_out(customer: Customer) -> CustomerOut:
    return CustomerOut(id=customer.id, customer_code=customer.customer_code,
                       name=customer.name, customer_type=customer.customer_type,
                       contact_person=customer.contact_person, phone=customer.phone,
                       email=customer.email, address=customer.address, city=customer.city,
                       state=customer.state, country=customer.country,
                       tax_id=customer.tax_id, credit_limit=customer.credit_limit,
                       opening_balance=customer.opening_balance, notes=customer.notes,
                       is_active=customer.is_active, created_by=customer.created_by,
                       created_at=customer.created_at, updated_at=customer.updated_at)


class SqlCustomerRepository:
    def create(self, organization_id: uuid.UUID, data: CustomerCreate,
              created_by: uuid.UUID | None = None) -> CustomerOut:
        with get_session() as db:
            customer = Customer(
                organization_id=organization_id, customer_code=data.customer_code,
                name=data.name, customer_type=data.customer_type,
                contact_person=data.contact_person, phone=data.phone, email=data.email,
                address=data.address, city=data.city, state=data.state,
                country=data.country, tax_id=data.tax_id, credit_limit=data.credit_limit,
                opening_balance=data.opening_balance, notes=data.notes,
                created_by=created_by)
            db.add(customer)
            db.flush()
            return _to_out(customer)

    def update(self, organization_id: uuid.UUID, customer_id: uuid.UUID,
              data: CustomerUpdate) -> CustomerOut | None:
        with get_session() as db:
            customer = db.get(Customer, customer_id)
            if customer is None or customer.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(customer, field, value)
            db.flush()
            return _to_out(customer)

    def get_by_id(self, organization_id: uuid.UUID,
                 customer_id: uuid.UUID) -> CustomerOut | None:
        with get_session() as db:
            customer = db.get(Customer, customer_id)
            if customer is None or customer.organization_id != organization_id:
                return None
            return _to_out(customer)

    def list_all(self, organization_id: uuid.UUID) -> list[CustomerOut]:
        with get_session() as db:
            rows = (db.query(Customer).filter_by(organization_id=organization_id)
                   .order_by(Customer.name).all())
            return [_to_out(c) for c in rows]

    def code_exists(self, organization_id: uuid.UUID, code: str,
                    exclude_id: uuid.UUID | None = None) -> bool:
        with get_session() as db:
            query = db.query(Customer.id).filter_by(organization_id=organization_id,
                                                     customer_code=code)
            if exclude_id is not None:
                query = query.filter(Customer.id != exclude_id)
            return db.query(query.exists()).scalar()

    def get_balance(self, organization_id: uuid.UUID,
                    customer_id: uuid.UUID) -> CustomerBalance | None:
        with get_session() as db:
            customer = db.get(Customer, customer_id)
            if customer is None or customer.organization_id != organization_id:
                return None
            return compute_customer_balance(db, organization_id, customer)

    def get_history(self, organization_id: uuid.UUID, customer_id: uuid.UUID,
                    limit: int = 100) -> list[CustomerTransaction]:
        with get_session() as db:
            orders = (db.query(SalesOrder)
                     .filter_by(organization_id=organization_id, customer_id=customer_id)
                     .order_by(SalesOrder.created_at.desc()).limit(limit).all())
            order_ids = [so.id for so in orders]

            invoices = (db.query(Invoice).filter(Invoice.sales_order_id.in_(order_ids)).all()
                       if order_ids else [])
            invoice_by_id = {inv.id: inv for inv in invoices}

            payments = (db.query(Payment)
                       .filter(Payment.invoice_id.in_(invoice_by_id.keys())).all()
                       if invoice_by_id else [])

            returns = (db.query(SalesReturn)
                      .filter(SalesReturn.sales_order_id.in_(order_ids)).all()
                      if order_ids else [])

            rows: list[CustomerTransaction] = []
            for so in orders:
                rows.append(CustomerTransaction(
                    kind="sales_order", id=so.id,
                    reference=f"Order {so.id.hex[:8].upper()}", amount=None,
                    status=so.status.value, occurred_at=so.created_at))
            for inv in invoices:
                rows.append(CustomerTransaction(
                    kind="invoice", id=inv.id, reference=inv.invoice_number,
                    amount=inv.total_amount, status=None, occurred_at=inv.generated_at))
            for pmt in payments:
                rows.append(CustomerTransaction(
                    kind="payment", id=pmt.id, reference=pmt.method.value,
                    amount=pmt.amount, status=None, occurred_at=pmt.received_at))
            for ret in returns:
                rows.append(CustomerTransaction(
                    kind="return", id=ret.id, reference=ret.reason,
                    amount=-ret.credit_amount, status=None, occurred_at=ret.returned_at))

            rows.sort(key=lambda r: r.occurred_at, reverse=True)
            return rows[:limit]
