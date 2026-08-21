from decimal import Decimal

from app.domain.product import normalize_barcode, normalize_sku, validate_product


def test_normalize_sku_uppercases_and_trims():
    assert normalize_sku("  abc-123  ") == "ABC-123"


def test_normalize_barcode_trims_and_blank_becomes_none():
    assert normalize_barcode("  012345  ") == "012345"
    assert normalize_barcode("   ") is None
    assert normalize_barcode(None) is None


def _valid_kwargs(**overrides):
    kwargs = dict(sku="ABC-1", name="Widget", purchase_price=Decimal("10"),
                  selling_price=Decimal("15"), tax_percent=Decimal("13"),
                  minimum_stock_level=Decimal("5"))
    kwargs.update(overrides)
    return kwargs


def test_validate_product_accepts_valid_data():
    assert validate_product(**_valid_kwargs()) == []


def test_validate_product_rejects_blank_sku():
    errors = validate_product(**_valid_kwargs(sku="   "))
    assert "SKU is required." in errors


def test_validate_product_rejects_blank_name():
    errors = validate_product(**_valid_kwargs(name=""))
    assert "Name is required." in errors


def test_validate_product_rejects_negative_purchase_price():
    errors = validate_product(**_valid_kwargs(purchase_price=Decimal("-1")))
    assert "Purchase price cannot be negative." in errors


def test_validate_product_rejects_negative_selling_price():
    errors = validate_product(**_valid_kwargs(selling_price=Decimal("-1")))
    assert "Selling price cannot be negative." in errors


def test_validate_product_rejects_tax_percent_out_of_range():
    assert "Tax percent must be between 0 and 100." in validate_product(
        **_valid_kwargs(tax_percent=Decimal("101")))
    assert "Tax percent must be between 0 and 100." in validate_product(
        **_valid_kwargs(tax_percent=Decimal("-1")))


def test_validate_product_rejects_excise_percent_out_of_range():
    assert "Excise percent must be between 0 and 100." in validate_product(
        **_valid_kwargs(excise_percent=Decimal("101")))
    assert "Excise percent must be between 0 and 100." in validate_product(
        **_valid_kwargs(excise_percent=Decimal("-1")))


def test_validate_product_accepts_excise_independent_of_is_taxable():
    # Excise duty is a distinct government levy, not coupled to
    # is_taxable/tax_percent the way the tax fields are paired.
    assert validate_product(
        **_valid_kwargs(is_taxable=False, tax_percent=Decimal("0"),
                        excise_percent=Decimal("5"))) == []


def test_validate_product_rejects_negative_minimum_stock_level():
    errors = validate_product(**_valid_kwargs(minimum_stock_level=Decimal("-1")))
    assert "Minimum stock level (re-order point) cannot be negative." in errors


def test_validate_product_reports_multiple_errors_at_once():
    errors = validate_product(**_valid_kwargs(sku="", purchase_price=Decimal("-1")))
    assert len(errors) == 2
