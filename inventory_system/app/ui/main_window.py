"""Main application shell: Sidebar + Header + content QStackedWidget +
status bar + notification overlay, plus real idle-timeout enforcement (an
app-wide event filter marks activity; a QTimer polls for expiry and forces
a return to the login screen). Deliberately thin — it composes widgets and
wires signals, it does not contain business logic or talk to a repository
directly (see app/ui/__init__.py's rule); each page owns its own data
fetching through its injected Service.
"""
from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.container import Container
from app.ui.theme import STYLESHEET
from app.ui.widgets.change_password_dialog import ChangePasswordDialog
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.header import Header
from app.ui.widgets.sidebar import Sidebar, SidebarModule
from app.ui.widgets.toast import NotificationCenter

MODULES = [
    SidebarModule("dashboard", "Dashboard", "🏠"),
    SidebarModule("products", "Products", "📦"),
    SidebarModule("inventory", "Inventory", "📊"),
    SidebarModule("warehouses", "Warehouses", "🏬"),
    SidebarModule("purchases", "Purchases", "📥"),
    SidebarModule("sales", "Sales", "📤"),
    SidebarModule("suppliers", "Suppliers", "🚚"),
    SidebarModule("customers", "Customers", "👥"),
    SidebarModule("reports", "Reports", "📈"),
    SidebarModule("users", "Users", "👤"),
    SidebarModule("settings", "Settings", "⚙️"),
]

IDLE_CHECK_INTERVAL_MS = 15_000


class MainWindow(QMainWindow):
    session_ended = Signal()  # logout, or idle timeout — either way, back to login

    def __init__(self, container: Container):
        super().__init__()
        self._container = container
        self.setWindowTitle("Inventory Management")
        self.resize(1360, 880)
        self.setMinimumSize(1080, 700)
        self.setStyleSheet(STYLESHEET)

        session = container.sessions.peek()
        membership = None
        if session is not None and session.organization_id is not None:
            membership = container.user_repo.get_membership(session.user_id,
                                                             session.organization_id)
        user = container.user_repo.get_by_id(session.user_id) if session else None
        full_name = user.full_name if user else "Unknown User"
        role_label = (membership.role_name if membership else
                     ("Superuser" if session and session.is_superuser else "Member"))

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = Sidebar(MODULES)
        root.addWidget(self._sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self._header = Header(full_name, role_label)
        self._header.user_menu.logout_requested.connect(self._on_logout_requested)
        self._header.user_menu.change_password_requested.connect(
            self._on_change_password_requested)
        right_layout.addWidget(self._header)

        self._content = QStackedWidget()
        self._content.setObjectName("contentArea")
        self._pages: dict[str, QWidget] = {}
        for module in MODULES:
            page = _build_page(module.key, container)
            self._pages[module.key] = page
            self._content.addWidget(page)
        right_layout.addWidget(self._content, stretch=1)

        self._status_bar = self.statusBar()
        self._status_bar.setObjectName("statusBar")
        self._status_email = user.email if user else ""
        self._status_bar.showMessage(f"Logged in as {self._status_email}  ·  Ready")

        root.addWidget(right, stretch=1)
        self.setCentralWidget(central)

        self._notifications = NotificationCenter(central)

        self._sidebar.module_selected.connect(self._on_module_selected)
        self._on_module_selected(MODULES[0].key)

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
    if key == "inventory":
        from app.ui.pages.inventory_page import InventoryPage
        return InventoryPage(container.stock_service())
    if key == "sales":
        from app.ui.pages.sales_page import SalesPage
        return SalesPage(container.billing_service())
    if key == "purchases":
        from app.ui.pages.purchases_page import PurchasesPage
        return PurchasesPage(container.billing_service())
    if key == "products":
        from app.ui.pages.products_page import ProductsPage
        return ProductsPage(container.product_service(), container.catalog_service(),
                            container.sessions)
    if key == "warehouses":
        from app.ui.pages.warehouses_page import WarehousesPage
        return WarehousesPage()
    if key == "suppliers":
        from app.ui.pages.suppliers_page import SuppliersPage
        return SuppliersPage()
    if key == "customers":
        from app.ui.pages.customers_page import CustomersPage
        return CustomersPage()
    if key == "reports":
        from app.ui.pages.reports_page import ReportsPage
        return ReportsPage(container.reporting_service(), container.product_service(),
                           container.catalog_service(), container.inventory_service(),
                           container.purchase_service(), container.sales_service())
    if key == "users":
        from app.ui.pages.users_page import UsersPage
        return UsersPage()
    if key == "settings":
        from app.ui.pages.settings_page import SettingsPage
        return SettingsPage()
    raise ValueError(f"Unknown module key: {key!r}")
