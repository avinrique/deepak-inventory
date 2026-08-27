"""Regression tests for app.core.container's composition root.

Guards against two things reintroducing the pre-cleanup state would cause:
1. Container() failing to construct, or doing I/O while it does. Nothing
   else exercises the composition root without a live database.
2. The dead legacy BillingService/DashboardService/PartyService/StockService
   factories coming back — they were unreachable from any UI page, had none
   of the @require_permission decorators every live service carries, and
   wrote money values through a float round-trip via the Excel repositories
   that wrapped the legacy Tkinter app's storage.py. That whole backend has
   been deleted: it lived outside this project directory and could not be
   packaged. Re-wiring one of these into Container without re-auditing
   RBAC/Decimal-safety would silently reintroduce both problems — see the
   git history around their removal for the audit that found them.
"""
from app.core.container import Container


def test_container_constructs():
    """Also asserts it touches no database: every Sql* repository is
    constructed eagerly here, so any of them opening a connection in
    __init__ would make the app unstartable while offline."""
    container = Container()
    assert container.auth_service() is not None
    assert container.inventory_service() is not None


def test_container_has_no_dead_legacy_service_factories():
    container = Container()
    for dead_factory in ("billing_service", "dashboard_service", "party_service",
                         "stock_service"):
        assert not hasattr(container, dead_factory), (
            f"Container.{dead_factory} was removed as dead/unreachable code with no "
            "RBAC enforcement — see this test's module docstring before reintroducing it.")
