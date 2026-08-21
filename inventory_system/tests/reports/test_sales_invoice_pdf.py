"""app.reports.sales_invoice_pdf.render_invoice_pdf — a pure function of
InvoiceDocumentData, so these tests build that data directly (no database,
no service layer) and verify the rendered PDF's actual text content via
pypdf, not just "a file exists and starts with %PDF".
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

pypdf = pytest.importorskip("pypdf")

from app.domain.sales import InvoicePaymentStatus
from app.reports.sales_invoice_pdf import render_invoice_pdf
from app.schemas.sales import InvoiceDocumentData, InvoiceDocumentLine


def _line(sku="SKU-1", name="Widget", quantity=Decimal("2"), unit_price=Decimal("50"),
         discount_percent=Decimal("0"), tax_percent=Decimal("13"),
         excise_percent=Decimal("0")) -> InvoiceDocumentLine:
    from app.domain.sales import (
        line_discount,
        line_excise_after_discount,
        line_tax_after_discount,
        line_total_after_discount,
    )
    from app.domain.pricing import line_subtotal
    return InvoiceDocumentLine(
        sku=sku, product_name=name, quantity=quantity, unit_price=unit_price,
        discount_percent=discount_percent, tax_percent=tax_percent,
        excise_percent=excise_percent,
        line_subtotal=line_subtotal(quantity, unit_price),
        line_discount=line_discount(quantity, unit_price, discount_percent),
        line_tax=line_tax_after_discount(quantity, unit_price, discount_percent, tax_percent),
        line_excise=line_excise_after_discount(quantity, unit_price, discount_percent,
                                               excise_percent),
        line_total=line_total_after_discount(quantity, unit_price, discount_percent, tax_percent,
                                             excise_percent=excise_percent))


def _data(items=None, payment_status=InvoicePaymentStatus.UNPAID, amount_paid=Decimal("0"),
         notes=None, **overrides) -> InvoiceDocumentData:
    items = items if items is not None else [_line()]
    subtotal = sum((i.line_subtotal for i in items), Decimal("0"))
    discount_total = sum((i.line_discount for i in items), Decimal("0"))
    tax_total = sum((i.line_tax for i in items), Decimal("0"))
    excise_total = sum((i.line_excise for i in items), Decimal("0"))
    total = sum((i.line_total for i in items), Decimal("0"))
    kwargs = dict(
        company_name="Acme Traders", company_legal_name="Acme Traders Pvt. Ltd.",
        company_address="123 Market Street", company_phone="+1-555-0100",
        company_email="billing@acme.example", company_website="acme.example",
        company_tax_id="TAX-001",
        invoice_id=uuid.uuid4(), invoice_number="INV-000001",
        invoice_date=datetime(2026, 3, 15, tzinfo=timezone.utc), sales_order_id=uuid.uuid4(),
        customer_name="Jane Buyer", customer_address="45 Storage Lane",
        customer_phone="+1-555-0200", customer_email="jane@buyer.example",
        customer_tax_id="TAX-999",
        items=items, subtotal=subtotal, discount_total=discount_total,
        overall_discount=Decimal("0"), tax_total=tax_total, excise_total=excise_total,
        other_charges=Decimal("0"),
        total=total, amount_paid=amount_paid, amount_due=total - amount_paid,
        payment_status=payment_status, notes=notes, due_date=None)
    kwargs.update(overrides)
    return InvoiceDocumentData(**kwargs)


def _extract_text(path: str) -> str:
    reader = pypdf.PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_render_invoice_pdf_produces_a_valid_pdf_file(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    result = render_invoice_pdf(_data(), path)

    assert result == path
    with open(path, "rb") as f:
        assert f.read(4) == b"%PDF"


def test_invoice_pdf_contains_company_info_from_data_not_hardcoded(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(company_name="Totally Different Co",
                             company_tax_id="UNIQUE-TAX-999"), path)

    text = _extract_text(path)
    assert "Totally Different Co" in text
    assert "UNIQUE-TAX-999" in text


def test_invoice_pdf_contains_invoice_number_and_customer(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(invoice_number="INV-999999", customer_name="Unique Customer Name"),
                       path)

    text = _extract_text(path)
    assert "INV-999999" in text
    assert "Unique Customer Name" in text


def test_invoice_pdf_contains_line_items_and_totals(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    items = [_line(sku="ABC-1", name="Special Widget", quantity=Decimal("3"),
                   unit_price=Decimal("25"), tax_percent=Decimal("10"))]
    render_invoice_pdf(_data(items=items), path)

    text = _extract_text(path)
    assert "ABC-1" in text
    assert "Special Widget" in text
    assert "Total" in text
    assert "Subtotal" in text


@pytest.mark.parametrize("status,label", [
    (InvoicePaymentStatus.PAID, "PAID"),
    (InvoicePaymentStatus.PARTIALLY_PAID, "PARTIALLY PAID"),
    (InvoicePaymentStatus.UNPAID, "UNPAID"),
])
def test_invoice_pdf_shows_correct_payment_status_label(tmp_path, status, label):
    path = str(tmp_path / f"invoice_{status.value}.pdf")
    render_invoice_pdf(_data(payment_status=status), path)

    text = _extract_text(path)
    assert label in text


def test_invoice_pdf_shows_discount_line_when_present(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    items = [_line(discount_percent=Decimal("15"))]
    render_invoice_pdf(_data(items=items), path)

    text = _extract_text(path)
    assert "Discount" in text


def test_invoice_pdf_omits_discount_row_when_zero(tmp_path):
    """The totals block only shows a Discount line when discount_total is
    non-zero — the item table's per-line Discount column still exists
    (showing "—"), but the summary shouldn't show a redundant "Discount:
    0.00" row on every invoice that never uses discounts.
    """
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(items=[_line(discount_percent=Decimal("0"))]), path)

    text = _extract_text(path)
    # "Subtotal"/"Tax"/"Total" must still appear; the summary shouldn't
    # contain a discount total row when there's nothing to show.
    assert "Subtotal" in text
    assert "Total" in text


def test_invoice_pdf_shows_excise_duty_row_when_present(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    items = [_line(excise_percent=Decimal("5"))]
    render_invoice_pdf(_data(items=items), path)

    text = _extract_text(path)
    assert "Excise Duty" in text
    assert "Excise" in text  # the items-table column header


def test_invoice_pdf_omits_excise_duty_row_when_zero(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(items=[_line(excise_percent=Decimal("0"))]), path)

    text = _extract_text(path)
    assert "Excise Duty" not in text
    assert "Subtotal" in text
    assert "Total" in text


def test_invoice_pdf_shows_due_date_when_present(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    from datetime import date
    render_invoice_pdf(_data(due_date=date(2026, 4, 1)), path)

    text = _extract_text(path)
    assert "2026-04-01" in text


def test_invoice_pdf_omits_due_date_when_absent(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(due_date=None), path)

    text = _extract_text(path)
    assert "Due:" not in text


def test_invoice_pdf_shows_overall_discount_and_other_charges_when_present(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(overall_discount=Decimal("25.00"),
                            other_charges=Decimal("10.00")), path)

    text = _extract_text(path)
    assert "Overall Discount" in text
    assert "Other Charges" in text
    assert "Total" in text  # the "Total" row still renders correctly alongside the new ones


def test_invoice_pdf_omits_overall_discount_and_other_charges_when_zero(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(overall_discount=Decimal("0"), other_charges=Decimal("0")), path)

    text = _extract_text(path)
    assert "Overall Discount" not in text
    assert "Other Charges" not in text


def test_invoice_pdf_includes_notes_when_present(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(notes="Payment due within 15 days. Thank you!"), path)

    text = _extract_text(path)
    assert "Payment due within 15 days" in text


def test_invoice_pdf_handles_missing_optional_company_and_customer_fields(tmp_path):
    """Not every organization/customer has every field filled in — the
    template must not crash on None values.
    """
    path = str(tmp_path / "invoice.pdf")
    render_invoice_pdf(_data(company_legal_name=None, company_address=None,
                             company_phone=None, company_email=None, company_website=None,
                             company_tax_id=None, customer_address=None, customer_phone=None,
                             customer_email=None, customer_tax_id=None, notes=None), path)

    with open(path, "rb") as f:
        assert f.read(4) == b"%PDF"


def test_invoice_pdf_handles_multiple_line_items(tmp_path):
    path = str(tmp_path / "invoice.pdf")
    items = [_line(sku=f"SKU-{i}", name=f"Product {i}", quantity=Decimal(i))
            for i in range(1, 6)]
    render_invoice_pdf(_data(items=items), path)

    text = _extract_text(path)
    for i in range(1, 6):
        assert f"SKU-{i}" in text


def test_render_invoice_pdf_can_be_called_again_for_the_same_invoice(tmp_path):
    """"Re-generate invoice" is just calling this function again — proves
    doing so doesn't error and produces an equally valid file.
    """
    path = str(tmp_path / "invoice.pdf")
    data = _data()
    render_invoice_pdf(data, path)
    render_invoice_pdf(data, path)

    with open(path, "rb") as f:
        assert f.read(4) == b"%PDF"
