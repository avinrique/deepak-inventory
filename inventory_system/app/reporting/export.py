"""Turns a ReportResult into CSV/Excel/PDF files, or sends it to a printer.

Pandas is used here — and only here — for exactly the tabular-I/O job it's
good at: shaping already-SQL-aggregated rows into a DataFrame so
to_csv()/to_excel() can handle quoting, encoding, and the .xlsx container
format correctly. No aggregation happens in this module; every row already
arrived pre-computed from app.repositories.sql.reporting_repository.

PDF export and the Print action both render the same HTML table through
Qt's own QTextDocument + QtPrintSupport (QPrinter/QPrintDialog) — already a
project dependency (PySide6), so no reportlab/wkhtmltopdf install needed,
and "export to PDF" and "print" share one rendering path instead of two.
"""
import html as html_lib
from datetime import date, datetime
from decimal import Decimal

import pandas as pd

from app.schemas.reporting import ReportResult


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:,}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def to_dataframe(result: ReportResult) -> pd.DataFrame:
    """Decimal -> float at this boundary only: spreadsheet cells need to be
    numeric (so a user can sum/format them in Excel), and the underlying
    calculation was already done in Decimal upstream — this conversion is
    purely for display, not further financial computation.
    """
    def _coerce(value):
        return float(value) if isinstance(value, Decimal) else value

    if not result.rows:
        return pd.DataFrame(columns=result.columns)
    coerced = [{k: _coerce(v) for k, v in row.items()} for row in result.rows]
    return pd.DataFrame(coerced, columns=result.columns)


def export_csv(result: ReportResult, path: str) -> str:
    to_dataframe(result).to_csv(path, index=False)
    return path


def export_excel(result: ReportResult, path: str) -> str:
    sheet_name = (result.title or "Report")[:31]  # Excel's sheet-name length limit
    to_dataframe(result).to_excel(path, index=False, sheet_name=sheet_name, engine="openpyxl")
    return path


def render_html_table(result: ReportResult) -> str:
    header_cells = "".join(f"<th>{html_lib.escape(str(col))}</th>" for col in result.columns)
    body_rows = []
    for row in result.rows:
        cells = "".join(f"<td>{html_lib.escape(_format_cell(row.get(col)))}</td>"
                        for col in result.columns)
        body_rows.append(f"<tr>{cells}</tr>")
    generated = result.generated_at.strftime("%Y-%m-%d %H:%M")
    return f"""<html><head><style>
        body {{ font-family: sans-serif; font-size: 11px; }}
        h2 {{ margin-bottom: 2px; }}
        .meta {{ color: #666; font-size: 10px; margin-bottom: 12px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
        th {{ background: #f0f0f0; }}
    </style></head>
    <body>
        <h2>{html_lib.escape(result.title)}</h2>
        <div class="meta">Generated {generated} &mdash; {result.row_count} row(s)</div>
        <table><thead><tr>{header_cells}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>
    </body></html>"""


def export_pdf(result: ReportResult, path: str) -> str:
    from PySide6.QtGui import QTextDocument
    from PySide6.QtPrintSupport import QPrinter

    document = QTextDocument()
    document.setHtml(render_html_table(result))

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    document.print_(printer)
    return path


def print_report(result: ReportResult, parent=None) -> bool:
    """Shows the OS print dialog; returns False if the user cancels it
    rather than raising, since cancelling is a normal outcome here, not an
    error condition.
    """
    from PySide6.QtGui import QTextDocument
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter

    document = QTextDocument()
    document.setHtml(render_html_table(result))

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialog = QPrintDialog(printer, parent)
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return False
    document.print_(printer)
    return True
