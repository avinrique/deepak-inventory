"""Every window and dialog must remain usable on the smallest supported
configuration.

The target hardware includes 1366x768 laptops. Windows runs those at 125%
scaling by default and users push it to 150% or 175%, which leaves Qt with
logical desktops of 1092x614, 910x512 and 780x438 respectively. A widget
whose *minimum* size exceeds that cannot be resized out of it, so whatever
sits at the bottom — Save, Cancel, the totals row — is permanently
unreachable. That is exactly what MainWindow's old setMinimumSize(1080, 700)
did at 125%.

These tests assert the property directly rather than eyeballing screenshots.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

import app.ui.widgets.responsive as responsive
from app.security.session import SessionManager

# (label, logical width, logical height) for 1366x768 at each scale factor.
SMALL_SCREENS = [
    ("1366x768 @100%", 1366, 768),
    ("1366x768 @125%", 1092, 614),
    ("1366x768 @150%", 910, 512),
    ("1366x768 @175%", 780, 438),
    ("1280x800 @150%", 853, 533),
]


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


@pytest.fixture()
def sessions():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    manager.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(),
                  role_id=uuid.uuid4(), permissions=frozenset(), is_superuser=True,
                  must_change_password=False, now=datetime.now(timezone.utc))
    return manager


def _pretend_screen(monkeypatch, width: int, height: int) -> None:
    monkeypatch.setattr(responsive, "available_size", lambda widget=None: (width, height))


def _assert_fits(widget: QWidget, width: int, height: int, label: str) -> None:
    minimum = widget.minimumSize()
    assert minimum.width() <= width, (
        f"{type(widget).__name__} demands {minimum.width()}px width on {label} "
        f"({width}px available) — the user cannot resize below a minimum")
    assert minimum.height() <= height, (
        f"{type(widget).__name__} demands {minimum.height()}px height on {label} "
        f"({height}px available) — its lower edge is unreachable")


def _dialog_factories(sessions=None):
    """Every QDialog a user can open, with stub services."""
    from app.ui.widgets.add_product_dialog import AddProductDialog
    from app.ui.widgets.catalog_manager_dialog import CatalogManagerDialog
    from app.ui.widgets.change_password_dialog import ChangePasswordDialog
    from app.ui.widgets.customer_form_dialog import CustomerFormDialog
    from app.ui.widgets.product_form_dialog import ProductFormDialog
    from app.ui.widgets.stock_in_dialog import StockInDialog
    from app.ui.widgets.supplier_form_dialog import SupplierFormDialog
    from app.ui.widgets.warehouse_form_dialog import WarehouseFormDialog

    return {
        "AddProductDialog": lambda: AddProductDialog(MagicMock(), [], [], [], []),
        "CatalogManagerDialog": lambda: CatalogManagerDialog(MagicMock()),
        "ChangePasswordDialog": lambda: ChangePasswordDialog(MagicMock()),
        "CustomerFormDialog": lambda: CustomerFormDialog(MagicMock()),
        "ProductFormDialog": lambda: ProductFormDialog(MagicMock(), [], [], []),
        "StockInDialog": lambda: StockInDialog(MagicMock(), [], []),
        "SupplierFormDialog": lambda: SupplierFormDialog(MagicMock()),
        "WarehouseFormDialog": lambda: WarehouseFormDialog(MagicMock()),
    }


@pytest.mark.parametrize("label,width,height", SMALL_SCREENS,
                         ids=[s[0] for s in SMALL_SCREENS])
def test_the_login_window_fits(qapp, monkeypatch, label, width, height):
    _pretend_screen(monkeypatch, width, height)
    from app.ui.login_window import LoginWindow

    _assert_fits(LoginWindow(MagicMock()), width, height, label)


@pytest.mark.parametrize("label,width,height", SMALL_SCREENS,
                         ids=[s[0] for s in SMALL_SCREENS])
def test_the_setup_wizard_fits(qapp, monkeypatch, label, width, height):
    _pretend_screen(monkeypatch, width, height)
    from app.ui.setup_wizard import SetupWizard

    _assert_fits(SetupWizard(), width, height, label)


@pytest.mark.parametrize("label,width,height", SMALL_SCREENS,
                         ids=[s[0] for s in SMALL_SCREENS])
def test_every_dialog_fits(qapp, monkeypatch, sessions, label, width, height):
    _pretend_screen(monkeypatch, width, height)

    too_big = []
    for name, factory in _dialog_factories(sessions).items():
        dialog = factory()
        minimum = dialog.minimumSize()
        if minimum.width() > width or minimum.height() > height:
            too_big.append(f"{name} min={minimum.width()}x{minimum.height()}")
    assert not too_big, f"unusable on {label} ({width}x{height}): {', '.join(too_big)}"


def test_no_dialog_is_hard_fixed_in_width(qapp, sessions):
    """setFixedWidth cannot adapt to anything. ChangePasswordDialog is shown
    *mandatorily* after an admin-initiated reset, so a clipped one locks the
    user out entirely."""
    for name, factory in _dialog_factories(sessions).items():
        dialog = factory()
        assert dialog.minimumWidth() != dialog.maximumWidth(), \
            f"{name} has a hard-fixed width and cannot adapt"


def test_the_sidebar_collapses_to_icons_on_a_narrow_window(qapp):
    from app.ui.widgets.sidebar import Sidebar, SidebarModule

    sidebar = Sidebar([SidebarModule("dashboard", "Dashboard", "🏠")])
    expanded = sidebar.width()

    sidebar.set_collapsed(True)

    assert sidebar.width() < expanded / 2
    # Collapsed is a width trade, not a loss of function: the entry is still
    # there and still says what it is.
    assert sidebar._list.count() == 1
    assert sidebar._list.item(0).toolTip() == "Dashboard"


def test_scale_tracks_the_ui_font(qapp, monkeypatch):
    from app.ui import theme

    monkeypatch.setattr(theme, "_scale_factor", 2.0)
    assert theme.scale(100) == 200

    monkeypatch.setattr(theme, "_scale_factor", 1.0)
    assert theme.scale(100) == 100


def test_scale_never_collapses_a_size_to_zero(qapp, monkeypatch):
    """Several call sites scale small values (a 6px progress bar, an 8px
    badge); rounding those to 0 would make them invisible."""
    from app.ui import theme

    monkeypatch.setattr(theme, "_scale_factor", 0.1)
    assert theme.scale(1) >= 1
    assert theme.scale(6) >= 1


def _fake_container(sessions):
    """MagicMock services with a real SessionManager — same shape as
    tests/ui/test_build_page_smoke.py, and for the same reason: a real
    Container would wire real repositories against the real database_url.

    The header reads a few values straight into QLabel/QFontMetrics, which
    reject a MagicMock, so those specific ones are given real strings.
    """
    container = MagicMock()
    container.sessions = sessions

    auth_service = container.auth_service.return_value
    auth_service.get_current_user.return_value.full_name = "Test User"
    auth_service.get_current_user.return_value.email = "test@example.com"
    auth_service.get_current_membership.return_value.role_name = "OWNER"
    organization = container.organization_service.return_value
    organization.get_current_organization.return_value.default_tax_percent = Decimal("13")
    return container


@pytest.fixture(autouse=True)
def clean_saved_geometry():
    """QSettings is real and persists to disk, so a window size left behind
    by one test would be restored into the next — and into the developer's
    actual application."""
    from PySide6.QtCore import QSettings

    QSettings().remove("main_window/geometry")
    yield
    QSettings().remove("main_window/geometry")


@pytest.fixture()
def stub_pages(monkeypatch):
    """Replaces page bodies with empty widgets for the geometry tests.

    What is under test here is the *shell* — its minimum size, its sidebar,
    its saved geometry. Real pages would pull their contents from MagicMock
    services and fail on formatting a mock as a number, which says nothing
    about window sizing. That every page really does construct is covered
    separately, by tests/ui/test_build_page_smoke.py.
    """
    import app.ui.main_window as main_window
    monkeypatch.setattr(main_window, "_build_page", lambda key, container: QWidget())


@pytest.mark.parametrize("label,width,height", SMALL_SCREENS,
                         ids=[s[0] for s in SMALL_SCREENS])
def test_the_main_window_fits(qapp, monkeypatch, sessions, stub_pages,
                              label, width, height):
    """The original bug: resize(1360, 880) with setMinimumSize(1080, 700).
    At 125% on a 1366x768 laptop the minimum was 86px taller than the whole
    logical desktop, so the bottom of the window could not be reached and,
    being a minimum, could not be resized away."""
    _pretend_screen(monkeypatch, width, height)
    from app.ui.main_window import MainWindow

    window = MainWindow(_fake_container(sessions))

    _assert_fits(window, width, height, label)
    assert window.width() <= width
    assert window.height() <= height


def test_the_main_window_collapses_its_sidebar_when_narrow(qapp, monkeypatch, sessions,
                                                          stub_pages):
    _pretend_screen(monkeypatch, 910, 512)   # 1366x768 at 150%
    from app.ui.main_window import MainWindow

    window = MainWindow(_fake_container(sessions))

    assert window._sidebar.is_collapsed() is True


def test_the_main_window_keeps_its_sidebar_labels_when_wide(qapp, monkeypatch, sessions,
                                                           stub_pages):
    _pretend_screen(monkeypatch, 1920, 1080)
    from app.ui.main_window import MainWindow

    window = MainWindow(_fake_container(sessions))

    assert window._sidebar.is_collapsed() is False


def test_restoring_geometry_from_a_monitor_that_is_gone_stays_on_screen(
        qapp, monkeypatch, sessions, stub_pages):
    """Saved geometry from a second monitor would otherwise put the window
    somewhere the user cannot see or drag it back from."""
    from PySide6.QtCore import QSettings

    from app.ui.main_window import MainWindow

    _pretend_screen(monkeypatch, 1366, 768)
    first = MainWindow(_fake_container(sessions))
    first.move(4000, 2000)          # as if on a monitor to the right
    first._save_geometry()
    try:
        second = MainWindow(_fake_container(sessions))
        screen = second.screen().availableGeometry()
        assert second.x() <= screen.right()
        assert second.y() <= screen.bottom()
    finally:
        QSettings().remove("main_window/geometry")


def test_geometry_restored_from_a_larger_monitor_is_shrunk_to_fit(
        qapp, sessions, stub_pages):
    """Docked to a big monitor yesterday, on the laptop's own screen today:
    the saved size has to come down, not just move.

    Deliberately does *not* patch available_size. keep_on_screen reads the
    real QScreen, because that is the only thing that knows where a window
    can actually go — so the assertion has to use the same screen, or it
    just measures the mock.
    """
    available = qapp.primaryScreen().availableGeometry()

    oversized = MainWindowFactory(sessions)
    oversized.resize(available.width() * 3, available.height() * 3)
    oversized._save_geometry()

    restored = MainWindowFactory(sessions)

    assert restored.width() <= available.width()
    assert restored.height() <= available.height()


def MainWindowFactory(sessions):  # noqa: N802 - a factory, named for the class
    from app.ui.main_window import MainWindow

    return MainWindow(_fake_container(sessions))
