"""Main application shell: Sidebar + Header + content QStackedWidget +
status bar + notification overlay, plus real idle-timeout enforcement (an
app-wide event filter marks activity; a QTimer polls for expiry and forces
a return to the login screen). Deliberately thin — it composes widgets and
wires signals, it does not contain business logic or talk to a repository
directly (see app/ui/__init__.py's rule); each page owns its own data
fetching through its injected Service.

The same idle-check timer also polls AuthService.is_current_user_still_active
so a deactivation applied by another admin while this session is open takes
effect promptly (see _check_still_active) rather than only on the next
login/idle-timeout.

Sidebar entries are filtered by permission (see _MODULE_PERMISSIONS/
_visible_modules) — a role with none of a module's relevant .view codes
never sees that entry at all, rather than clicking in and hitting a bare
"couldn't load" error. This is a UX convenience computed once at
construction from the session's permission snapshot, same caveat as every
other UI-side check: app.ui.permission_hints, which this delegates to,
is not the enforcement boundary — @require_permission on the Service
methods those pages call is what actually stops an unauthorized read,
independent of whether the sidebar entry was ever shown.
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.container import Container
from app.security.session import SessionManager
from app.ui import permission_hints
from app.ui.theme import STYLESHEET
from app.ui.widgets.change_password_dialog import ChangePasswordDialog
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.header import Header
from app.ui.widgets.sidebar import Sidebar, SidebarModule
from app.ui.widgets.toast import NotificationCenter
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

MODULES = [
    SidebarModule("dashboard", "Dashboard", "🏠"),
    SidebarModule("profile", "My Profile", "🙍"),
    SidebarModule("products", "Products", "📦"),
    SidebarModule("inventory", "Inventory", "📊"),
    SidebarModule("warehouses", "Warehouses", "🏬"),
    SidebarModule("purchases", "Purchases", "📥"),
    SidebarModule("new_bill", "New Bill", "🧾"),
    SidebarModule("sales", "Sales", "📤"),
    SidebarModule("suppliers", "Suppliers", "🚚"),
    SidebarModule("customers", "Customers", "👥"),
    SidebarModule("reports", "Reports", "📈"),
    SidebarModule("users", "Users", "👤"),
    SidebarModule("settings", "Settings", "⚙️"),
]

# Module key -> the permission code(s) that make it relevant; having any
# ONE is enough (e.g. products.view). A module with no entry here (Home,
# Warehouses/Suppliers/Customers — currently placeholders with nothing to
# gate, and Settings — whose own read side has no permission requirement,
# only editing does, via SettingsPage._can_edit) is visible to everyone
# who's logged in.
_MODULE_PERMISSIONS: dict[str, frozenset[str]] = {
    "new_bill": frozenset({"sales.create"}),
    "products": frozenset({"products.view"}),
    "inventory": frozenset({"inventory.view"}),
    "purchases": frozenset({"purchases.view"}),
    "sales": frozenset({"sales.view"}),
    "customers": frozenset({"customers.view"}),
    "reports": frozenset({"reports.view"}),
    "users": frozenset({"users.view"}),
}

IDLE_CHECK_INTERVAL_MS = 15_000


def _visible_modules(sessions: SessionManager) -> list[SidebarModule]:
    return [module for module in MODULES
           if module.key not in _MODULE_PERMISSIONS
           or permission_hints.can_any(sessions, _MODULE_PERMISSIONS[module.key])]


class MainWindow(QMainWindow):
    session_ended = Signal()  # logout, or idle timeout — either way, back to login

    def __init__(self, container: Container):
        super().__init__()
        self._container = container
        self.setWindowTitle("Inventory Management")
        self.resize(1360, 880)
        self.setMinimumSize(1080, 700)
        self.setStyleSheet(STYLESHEET)

        # Goes through AuthService rather than container.user_repo directly
        # — the same rule every page follows (see this module's docstring)
        # applies here too, and it's what makes "current user" info
        # available the same way to any page that wants it later, not just
        # this one-off lookup at window construction.
        auth_service = container.auth_service()
        session = container.sessions.peek()
        user = auth_service.get_current_user() if session is not None else None
        membership = auth_service.get_current_membership() if session is not None else None
        full_name = user.full_name if user else "Unknown User"
        role_label = (membership.role_name if membership else
                     ("Superuser" if session and session.is_superuser else "Member"))

        visible_modules = _visible_modules(container.sessions)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = Sidebar(visible_modules)
        root.addWidget(self._sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self._header = Header(full_name, role_label)
        self._header.user_menu.logout_requested.connect(self._on_logout_requested)
        self._header.user_menu.change_password_requested.connect(
            self._on_change_password_requested)
        # Reuses the exact same navigation path the sidebar's own "My
        # Profile" entry uses — select() drives the sidebar's
        # currentRowChanged, already wired to _on_module_selected below —
        # rather than duplicating "switch to the profile page" here.
        self._header.user_menu.profile_requested.connect(
            lambda: self._sidebar.select("profile"))
        right_layout.addWidget(self._header)

        self._content = QStackedWidget()
        self._content.setObjectName("contentArea")
        self._pages: dict[str, QWidget] = {}
        for module in visible_modules:
            page = _build_page(module.key, container)
            self._pages[module.key] = page
            self._content.addWidget(page)
            if module.key == "profile":
                # Reuses the exact same confirm+logout+session_ended
                # sequence the header's Log Out already goes through —
                # ProfilePage only ever emits the request, it never
                # confirms or logs out on its own.
                page.logout_requested.connect(self._on_logout_requested)
        right_layout.addWidget(self._content, stretch=1)

        self._status_bar = self.statusBar()
        self._status_bar.setObjectName("statusBar")
        self._status_email = user.email if user else ""
        self._status_bar.showMessage(f"Logged in as {self._status_email}  ·  Ready")

        root.addWidget(right, stretch=1)
        self.setCentralWidget(central)

        self._notifications = NotificationCenter(central)

        self._sidebar.module_selected.connect(self._on_module_selected)
        self._on_module_selected(visible_modules[0].key)

        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._check_idle)
        self._idle_timer.start(IDLE_CHECK_INTERVAL_MS)
        self._activity_filter = _ActivityFilter(container.sessions)
        app_instance = QApplication.instance()
        if app_instance is not None:
            app_instance.installEventFilter(self._activity_filter)

        if session is not None and session.must_change_password:
            QTimer.singleShot(0, self._force_password_change)

    def _on_module_selected(self, key: str) -> None:
        self._content.setCurrentWidget(self._pages[key])
        label = next(m.label for m in MODULES if m.key == key)
        self._header.breadcrumb.set_path(["Home", label])
        page = self._pages[key]
        if hasattr(page, "refresh"):
            page.refresh()

    def _check_idle(self) -> None:
        now = datetime.now(timezone.utc)
        if self._container.sessions.is_idle_expired(now):
            self._container.sessions.end()
            self._notifications.info("Signed out due to inactivity.")
            self.session_ended.emit()
            return
        self._check_still_active()

    def _check_still_active(self) -> None:
        """Piggybacks on the idle-check poll to also notice a deactivation
        that happened *after* login — @require_permission only ever checks
        the session's permission snapshot from login time, so without this
        a deactivated user's already-open session would otherwise keep
        working until they happened to log out or go idle. Runs off the
        GUI thread (a real DB round-trip, not free) so the poll never
        introduces a stutter.
        """
        worker = Worker(self._container.auth_service().is_current_user_still_active)
        worker.signals.finished.connect(self._on_active_check_result)
        worker.signals.error.connect(
            lambda exc: _logger.exception("Active-status check failed", exc_info=exc))
        QThreadPool.globalInstance().start(worker)

    def _on_active_check_result(self, is_active: bool) -> None:
        if is_active or self._container.sessions.peek() is None:
            return
        self._container.sessions.end()
        self._notifications.info(
            "Your account has been deactivated. Please contact an administrator.")
        self.session_ended.emit()

    def _on_logout_requested(self) -> None:
        if confirm(self, "Log Out", "Are you sure you want to log out?",
                  confirm_label="Log Out"):
            self._container.auth_service().logout()
            self.session_ended.emit()

    def _on_change_password_requested(self) -> None:
        dialog = ChangePasswordDialog(self._container.auth_service(), parent=self)
        if dialog.exec():
            self._notifications.success("Password changed.")

    def _force_password_change(self) -> None:
        dialog = ChangePasswordDialog(self._container.auth_service(), parent=self,
                                      mandatory=True)
        dialog.exec()
        self._notifications.success("Password changed.")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Stopped explicitly rather than relying on deleteLater() to reap
        # the timer eventually — a queued deleteLater() doesn't run until
        # the event loop's next pass, and a timer tick landing in that gap
        # would still fire against a window that's already being torn down.
        self._idle_timer.stop()
        app_instance = QApplication.instance()
        if app_instance is not None:
            app_instance.removeEventFilter(self._activity_filter)
        super().closeEvent(event)


class _ActivityFilter(QObject):
    """Any mouse/keyboard activity anywhere in the app counts as session
    activity — installed on the whole QApplication, not just this window,
    so a click in a dialog still resets the idle clock.
    """
    _ACTIVITY_EVENTS = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                        QEvent.Type.KeyPress}

    def __init__(self, sessions):
        super().__init__()
        self._sessions = sessions

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if event.type() in self._ACTIVITY_EVENTS:
            self._sessions.touch(datetime.now(timezone.utc))
        return False


def _build_page(key: str, container: Container) -> QWidget:
    if key == "dashboard":
        from app.ui.pages.dashboard_page import DashboardPage
        return DashboardPage(container.reporting_service())
    if key == "profile":
        from app.ui.pages.profile_page import ProfilePage
        return ProfilePage(container.auth_service(), container.user_service())
    if key == "inventory":
        from app.ui.pages.inventory_page import InventoryPage
        return InventoryPage(container.stock_service(), container.inventory_service(),
                             container.product_service(), container.sessions)
    if key == "new_bill":
        from app.ui.pages.new_bill_page import NewBillPage
        return NewBillPage(container.sales_service(), container.inventory_service(),
                           container.product_service(), container.sessions)
    if key == "sales":
        from app.ui.pages.sales_orders_page import SalesOrdersPage
        return SalesOrdersPage(container.sales_service(), container.inventory_service(),
                               container.product_service(), container.sessions)
    if key == "purchases":
        from app.ui.pages.purchases_page import PurchasesPage
        return PurchasesPage(container.purchase_service(), container.inventory_service(),
                             container.product_service(), container.sessions)
    if key == "products":
        from app.ui.pages.products_page import ProductsPage
        return ProductsPage(container.product_service(), container.catalog_service(),
                            container.sessions, container.organization_service())
    if key == "warehouses":
        from app.ui.pages.warehouses_page import WarehousesPage
        return WarehousesPage()
    if key == "suppliers":
        from app.ui.pages.suppliers_page import SuppliersPage
        return SuppliersPage()
    if key == "customers":
        from app.ui.pages.customers_page import CustomersPage
        return CustomersPage(container.sales_service(), container.sessions)
    if key == "reports":
        from app.ui.pages.reports_page import ReportsPage
        return ReportsPage(container.reporting_service(), container.product_service(),
                           container.catalog_service(), container.inventory_service(),
                           container.purchase_service(), container.sales_service())
    if key == "users":
        from app.ui.pages.users_page import UsersPage
        return UsersPage(container.user_service(), container.sessions,
                         container.organization_service(), container.auth_service())
    if key == "settings":
        from app.ui.pages.settings_page import SettingsPage
        return SettingsPage(container.organization_service(), container.backup_service(),
                            container.inventory_service(), container.sessions)
    raise ValueError(f"Unknown module key: {key!r}")
