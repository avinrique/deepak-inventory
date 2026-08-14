"""app.reporting.export — CSV/Excel/PDF generation from a ReportResult.
PDF export needs a real QApplication (QTextDocument/QPrinter are Qt
objects), same requirement as the worker tests — see
tests/workers/test_base_worker.py's qapp fixture, mirrored here.
"""
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

import pandas as pd

from app.reporting.export import export_csv, export_excel, export_pdf, render_html_table, to_dataframe
from app.schemas.reporting import ReportResult


@pytest.fixture(scope="module")
def qapp():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")
    return app


def _sample_result() -> ReportResult:
    return ReportResult(
        title="Sample Report", generated_at=datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc),
        columns=["SKU", "Product", "Quantity", "Value", "As Of"],
        rows=[
            {"SKU": "A-1", "Product": "Widget", "Quantity": Decimal("12.500"),
             "Value": Decimal("125.75"), "As Of": date(2026, 1, 1)},
            {"SKU": "B-2", "Product": "Gadget & <Co>", "Quantity": Decimal("0"),
             "Value": Decimal("0"), "As Of": date(2026, 1, 2)},
        ])


def test_to_dataframe_converts_decimal_to_float_and_keeps_column_order():
    df = to_dataframe(_sample_result())
    assert list(df.columns) == ["SKU", "Product", "Quantity", "Value", "As Of"]
    assert df.iloc[0]["Quantity"] == 12.5
    assert isinstance(df.iloc[0]["Quantity"], float)


def test_to_dataframe_empty_result_has_correct_columns_and_no_rows():
    empty = ReportResult(title="Empty", generated_at=datetime.now(timezone.utc),
                         columns=["A", "B"], rows=[])
    df = to_dataframe(empty)
    assert list(df.columns) == ["A", "B"]
    assert len(df) == 0


def test_export_csv_round_trips_through_pandas(tmp_path):
    path = str(tmp_path / "report.csv")
    export_csv(_sample_result(), path)

    assert Path(path).exists()
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["SKU"] == "A-1"
    assert rows[0]["Quantity"] == "12.5"


def test_export_excel_round_trips_through_pandas(tmp_path):
    path = str(tmp_path / "report.xlsx")
    export_excel(_sample_result(), path)

    assert Path(path).exists()
    df = pd.read_excel(path)
    assert list(df["SKU"]) == ["A-1", "B-2"]
    assert df.iloc[0]["Value"] == 125.75


def test_export_excel_sheet_name_truncated_to_31_chars(tmp_path):
    long_title = "A" * 50
    result = _sample_result().model_copy(update={"title": long_title})
    path = str(tmp_path / "report.xlsx")
    export_excel(result, path)

    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert len(wb.sheetnames[0]) <= 31


def test_export_pdf_produces_a_nonempty_file(qapp, tmp_path):
    path = str(tmp_path / "report.pdf")
    export_pdf(_sample_result(), path)

    assert Path(path).exists()
    assert Path(path).stat().st_size > 0
    with open(path, "rb") as f:
        assert f.read(4) == b"%PDF"


def test_render_html_table_escapes_special_characters():
    html = render_html_table(_sample_result())
    assert "Gadget &amp; &lt;Co&gt;" in html
    assert "<Co>" not in html  # never appears unescaped
    assert "Sample Report" in html
    assert "2 row(s)" in html


def test_render_html_table_handles_empty_rows():
    empty = ReportResult(title="Empty", generated_at=datetime.now(timezone.utc),
                         columns=["A"], rows=[])
    html = render_html_table(empty)
    assert "<table>" in html
    assert "0 row(s)" in html
