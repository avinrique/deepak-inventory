"""Regression tests for app.core.container's composition root.

Guards against two things reintroducing the pre-cleanup state would cause:
1. Container() failing to construct (the excel-backend wiring is small and
   easy to break silently since nothing exercises it in CI otherwise).
2. The dead legacy BillingService/DashboardService/PartyService factories
   coming back — they were unreachable from any UI page, had none of the
   @require_permission decorators every live service carries, and wrote
   money values through a float round-trip via the Excel repositories
   (app.repositories.excel). Re-wiring one of them into Container without
   also re-auditing RBAC/Decimal-safety would silently reintroduce both
   problems — see the git history around their removal for the audit that
   found them.
"""
from app.core.container import Container


def test_container_constructs():
    container = Container()
    assert container.stock_service() is not None


def test_container_has_no_dead_legacy_service_factories():
    container = Container()
    for dead_factory in ("billing_service", "dashboard_service", "party_service"):
        assert not hasattr(container, dead_factory), (
            f"Container.{dead_factory} was removed as dead/unreachable code with no "
            "RBAC enforcement — see this test's module docstring before reintroducing it.")
