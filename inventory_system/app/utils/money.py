"""Decimal helpers for the new layers. storage.num() stays as-is (float,
silently coerces bad input to 0) for the existing Excel code path — this is
its Decimal-strict equivalent, used by app.domain/services.
"""
from decimal import Decimal, InvalidOperation


def to_decimal(value) -> Decimal:
    """Parse into Decimal; raises ValueError on unparseable input.

    Unlike storage.num(), this does NOT silently coerce bad input to 0 — see
    docs/architecture.md's note on num()'s silent-zero behavior.
    """
    if value is None or str(value).strip() == "":
        return Decimal("0")
    text = str(value).replace(",", "").replace("%", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc
