"""CustomFieldsSection — collapsible key/value section shared by New Bill,
SalesOrderFormDialog, and PurchaseOrderFormDialog. Pure widget-state tests:
get_values()/set_values()/clear() round-trip, blank-key rows excluded,
add/remove row mechanics.
"""
import pytest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.custom_fields_section import CustomFieldsSection


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def test_starts_empty_and_collapsed(qapp):
    section = CustomFieldsSection()
    assert section.get_values() == {}
    assert section._toggle.isChecked() is False


def test_add_row_then_get_values(qapp):
    section = CustomFieldsSection()
    section._add_row("PO Number", "12345")
    assert section.get_values() == {"PO Number": "12345"}


def test_get_values_strips_whitespace_and_skips_blank_keys(qapp):
    section = CustomFieldsSection()
    section._add_row("  Gate  ", "  North  ")
    section._add_row("   ", "orphan value")  # blank key -> excluded
    values = section.get_values()
    assert values == {"Gate": "North"}


def test_set_values_replaces_existing_rows_and_expands(qapp):
    section = CustomFieldsSection()
    section._add_row("Old", "value")
    section.set_values({"New": "1", "Fields": "2"})
    assert section.get_values() == {"New": "1", "Fields": "2"}
    assert section._toggle.isChecked() is True


def test_set_values_with_none_or_empty_dict_clears_and_collapses(qapp):
    section = CustomFieldsSection()
    section._add_row("A", "1")
    section.set_values(None)
    assert section.get_values() == {}
    assert section._toggle.isChecked() is False


def test_clear_removes_all_rows_and_collapses(qapp):
    section = CustomFieldsSection()
    section._add_row("A", "1")
    section._add_row("B", "2")
    section.clear()
    assert section.get_values() == {}
    assert len(section._rows) == 0
    assert section._toggle.isChecked() is False


def test_remove_row_via_button_click(qapp):
    section = CustomFieldsSection()
    section._add_row("A", "1")
    section._add_row("B", "2")
    row_widget, _key_edit, _value_edit = section._rows[0]

    # The remove button is the last widget in the row's horizontal layout.
    layout = row_widget.layout()
    remove_btn = layout.itemAt(layout.count() - 1).widget()
    remove_btn.click()

    assert len(section._rows) == 1
    assert section.get_values() == {"B": "2"}
