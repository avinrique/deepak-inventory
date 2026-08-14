"""Professional printable invoice PDF, built with ReportLab — the one
reusable template every "Generate PDF / Preview / Print / Save PDF /
Re-generate invoice" action in the UI (see
app.ui.widgets.invoice_preview_dialog) renders through.

Deliberately a pure function of app.schemas.sales.InvoiceDocumentData: no
database access, no service calls, no knowledge of Organization/Customer/
SalesOrder as ORM concepts — company information, customer information,
line items, and payment status all arrive already assembled (see
SalesOrderRepository.get_invoice_document). That's what makes this
reusable and independently testable, and what "Do not hardcode company
information" actually means in practice — every company-identifying value
on the page is a field read off ``data``, sourced from Settings
(app.services.organization_service), never a literal in this file.
"""
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.domain.sales import InvoicePaymentStatus
from app.schemas.sales import InvoiceDocumentData

# -- template style — the "reusable" part: change these to re-skin every
# invoice without touching layout logic below. ---------------------------#
ACCENT_COLOR = colors.HexColor("#2563eb")
MUTED_COLOR = colors.HexColor("#6b7280")
BORDER_COLOR = colors.HexColor("#e2e6ec")
TABLE_HEADER_BG = colors.HexColor("#f8fafc")
STATUS_COLORS = {
    InvoicePaymentStatus.PAID: colors.HexColor("#059669"),
    InvoicePaymentStatus.PARTIALLY_PAID: colors.HexColor("#d97706"),
    InvoicePaymentStatus.UNPAID: colors.HexColor("#dc2626"),
}
STATUS_LABELS = {
    InvoicePaymentStatus.PAID: "PAID",
    InvoicePaymentStatus.PARTIALLY_PAID: "PARTIALLY PAID",
    InvoicePaymentStatus.UNPAID: "UNPAID",
}
PAGE_SIZE = A4
MARGIN = 2 * cm

_styles = getSampleStyleSheet()
_company_name_style = ParagraphStyle("CompanyName", parent=_styles["Normal"], fontSize=18,
                                     leading=22, fontName="Helvetica-Bold",
                                     textColor=colors.HexColor("#111827"))
_company_detail_style = ParagraphStyle("CompanyDetail", parent=_styles["Normal"], fontSize=9,
                                       leading=13, textColor=MUTED_COLOR)
_invoice_title_style = ParagraphStyle("InvoiceTitle", parent=_styles["Normal"], fontSize=22,
                                      leading=26, fontName="Helvetica-Bold",
                                      alignment=2, textColor=ACCENT_COLOR)
_invoice_meta_style = ParagraphStyle("InvoiceMeta", parent=_styles["Normal"], fontSize=9,
                                     leading=13, alignment=2, textColor=MUTED_COLOR)
_section_label_style = ParagraphStyle("SectionLabel", parent=_styles["Normal"], fontSize=9,
                                      leading=12, fontName="Helvetica-Bold",
                                      textColor=MUTED_COLOR)
_body_style = ParagraphStyle("Body", parent=_styles["Normal"], fontSize=10, leading=14)
_notes_style = ParagraphStyle("Notes", parent=_styles["Normal"], fontSize=9, leading=13,
                              textColor=MUTED_COLOR)


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _company_lines(data: InvoiceDocumentData) -> str:
    lines = []
    if data.company_legal_name and data.company_legal_name != data.company_name:
        lines.append(data.company_legal_name)
    if data.company_address:
        lines.append(data.company_address)
    contact = " · ".join(v for v in [data.company_phone, data.company_email,
                                     data.company_website] if v)
    if contact:
        lines.append(contact)
    if data.company_tax_id:
        lines.append(f"Tax ID: {data.company_tax_id}")
    return "<br/>".join(lines)


def _customer_lines(data: InvoiceDocumentData) -> str:
    lines = [f"<b>{data.customer_name}</b>"]
    if data.customer_address:
        lines.append(data.customer_address)
    contact = " · ".join(v for v in [data.customer_phone, data.customer_email] if v)
    if contact:
        lines.append(contact)
    if data.customer_tax_id:
        lines.append(f"Tax ID: {data.customer_tax_id}")
    return "<br/>".join(lines)


def _build_header(data: InvoiceDocumentData) -> Table:
    left = [Paragraph(data.company_name, _company_name_style)]
    detail = _company_lines(data)
    if detail:
        left.append(Spacer(1, 4))
        left.append(Paragraph(detail, _company_detail_style))

    right = [Paragraph("INVOICE", _invoice_title_style),
            Spacer(1, 6),
            Paragraph(f"<b>Invoice #:</b> {data.invoice_number}", _invoice_meta_style),
            Paragraph(f"<b>Date:</b> {data.invoice_date.strftime('%Y-%m-%d')}",
                     _invoice_meta_style)]

    table = Table([[left, right]], colWidths=[10 * cm, 7 * cm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _build_bill_to(data: InvoiceDocumentData) -> Table:
    block = [Paragraph("BILL TO", _section_label_style), Spacer(1, 4),
            Paragraph(_customer_lines(data), _body_style)]
    table = Table([[block]], colWidths=[17 * cm])
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _build_items_table(data: InvoiceDocumentData) -> Table:
    header = ["#", "Item", "Qty", "Unit Price", "Discount", "Tax", "Line Total"]
    rows = [header]
    for i, item in enumerate(data.items, start=1):
        rows.append([
            str(i),
            f"{item.product_name}<br/><font size=8 color='#6b7280'>{item.sku}</font>",
            f"{item.quantity:,g}",
            _money(item.unit_price),
            (f"{item.discount_percent:,g}%" if item.discount_percent else "—"),
            (f"{item.tax_percent:,g}%" if item.tax_percent else "—"),
            _money(item.line_total),
        ])
    rows[1:] = [[Paragraph(str(cell), _body_style) if isinstance(cell, str) and "<br/>" in cell
                else cell for cell in row] for row in rows[1:]]

    table = Table(rows, colWidths=[1 * cm, 6.2 * cm, 1.8 * cm, 2.5 * cm, 2 * cm, 1.5 * cm, 2.5 * cm],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED_COLOR),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _build_totals(data: InvoiceDocumentData) -> Table:
    rows = [["Subtotal", _money(data.subtotal)]]
    if data.discount_total:
        rows.append(["Discount", f"-{_money(data.discount_total)}"])
    rows.append(["Tax", _money(data.tax_total)])
    rows.append(["Total", _money(data.total)])
    rows.append(["Amount Paid", _money(data.amount_paid)])
    rows.append(["Amount Due", _money(data.amount_due)])

    table = Table(rows, colWidths=[4 * cm, 3.5 * cm])
    style = [
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), MUTED_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (0, len(rows) - 3), (-1, len(rows) - 3), 0.75, BORDER_COLOR),
        ("FONTNAME", (0, len(rows) - 3), (-1, len(rows) - 3), "Helvetica-Bold"),
        ("FONTSIZE", (0, len(rows) - 3), (-1, len(rows) - 3), 12),
        ("TEXTCOLOR", (0, len(rows) - 3), (0, len(rows) - 3), colors.HexColor("#111827")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]
    table.setStyle(TableStyle(style))
    return table


def _build_status_badge(data: InvoiceDocumentData) -> Table:
    color = STATUS_COLORS[data.payment_status]
    label = STATUS_LABELS[data.payment_status]
    style = ParagraphStyle("StatusBadge", parent=_styles["Normal"], fontSize=10,
                           fontName="Helvetica-Bold", textColor=colors.white, alignment=1)
    table = Table([[Paragraph(label, style)]], colWidths=[3.5 * cm], rowHeights=[0.7 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return table


def _build_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED_COLOR)
    canvas.drawString(MARGIN, 1.2 * cm, "Thank you for your business.")
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 1.2 * cm, f"Page {doc.page}")
    canvas.restoreState()


def render_invoice_pdf(data: InvoiceDocumentData, out_path: str) -> str:
    """Renders ``data`` to a PDF at ``out_path`` and returns that path.
    Called fresh every time — including for "Re-generate invoice", which is
    just this function called again with the same (or freshly re-fetched)
    data, not a distinct code path.
    """
    doc = SimpleDocTemplate(out_path, pagesize=PAGE_SIZE, topMargin=MARGIN,
                            bottomMargin=MARGIN, leftMargin=MARGIN, rightMargin=MARGIN,
                            title=f"Invoice {data.invoice_number}")

    story = [
        _build_header(data),
        Spacer(1, 14),
        HRFlowable(width="100%", thickness=1, color=BORDER_COLOR),
        Spacer(1, 14),
        _build_bill_to(data),
        Spacer(1, 18),
        _build_items_table(data),
        Spacer(1, 14),
    ]

    totals_and_status = Table(
        [[_build_status_badge(data), _build_totals(data)]],
        colWidths=[9.5 * cm, 7.5 * cm])
    totals_and_status.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(totals_and_status)

    if data.notes:
        story.append(Spacer(1, 18))
        story.append(Paragraph("NOTES", _section_label_style))
        story.append(Spacer(1, 4))
        story.append(Paragraph(data.notes, _notes_style))

    doc.build(story, onFirstPage=_build_footer, onLaterPages=_build_footer)
    return out_path
