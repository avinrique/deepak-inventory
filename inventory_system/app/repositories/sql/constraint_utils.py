"""Shared helper for translating a database-level constraint violation into
a specific domain exception. Used by repositories (category/brand/unit
today) whose uniqueness is enforced only by a DB index — no application-
level pre-check exists — so a caller must be told the write failed
*because of that specific constraint*, not any IntegrityError under the sun
(a NOT NULL or CHECK violation on the same insert should not be reported as
"already exists").
"""
from sqlalchemy.exc import IntegrityError


def constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort: the name of the constraint/index that actually failed,
    read from psycopg's diagnostics on the wrapped driver error. None if
    unavailable (a different driver, or a psycopg version without this
    attribute) — callers should treat that as "unknown, don't assume".
    """
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None
