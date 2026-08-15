"""Users page — search/filter/sort/paginate staff accounts, add/edit/view
users, change role, reset password, activate/deactivate. No business logic
here: every action calls UserService on a background Worker and renders
whatever comes back — validation, permission checks, duplicate checks, and
the Owner-protection rule all live in the service layer.

UserService.list_users() returns every member of the caller's organization
in one unpaginated call (there's no UserFilter/UserPage schema, unlike
Products) — organizations are small enough that this page does search/
filter/sort/pagination client-side over that one list rather than adding
server-side paging for what's realistically a few dozen rows.

Permission-gated actions (Add/Edit/Change Role/Reset Password/Activate/
Deactivate) are hidden per-row when the current session lacks the
permission — a convenience, not the actual boundary: UserService enforces
the same rule independently via @require_permission, so a hidden menu item
is not what's stopping an unauthorized write.
"""
import logging
from typing import NamedTuple

from PySide6.QtCore import QThreadPool, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    MembershipNotFoundError,
    OwnerProtectedError,
    RoleNotFoundError,
    UserNotFoundError,
)
from app.domain.security_policy import PasswordPolicy
from app.schemas.user import RoleOut, UserSummaryOut
from app.security.session import SessionManager
from app.services.user_service import UserService
from app.ui import permission_hints
from app.ui.theme import GREEN_DARK, MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.avatar import AvatarLabel
from app.ui.widgets.change_user_role_dialog import ChangeUserRoleDialog
from app.ui.widgets.combo_utils import select_by_data
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.pagination_bar import PaginationBar
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.user_form_dialog import DEFAULT_PASSWORD_POLICY, UserFormDialog
from app.workers.base_worker import Worker

_COLUMNS = [
    ("Avatar", None), ("Full Name", "full_name"), ("Username", "username"),
    ("Email", "email"), ("Role", "role_name"), ("Status", "is_active"),
    ("Last Login", "last_login_at"), ("Created", "created_at"), ("Actions", None),
]
_PAGE_SIZE = 20
_SEARCH_DEBOUNCE_MS = 300
_AVATAR_COL = 0
_ACTIONS_COL = 8

_logger = logging.getLogger(__name__)


class _UsersPageResult(NamedTuple):
    items: list[UserSummaryOut]
    page: int
    total_pages: int
    total: int


class UsersPage(QWidget):
    def __init__(self, user_service: UserService, sessions: SessionManager,
                organization_service=None, auth_service=None):
        super().__init__()
        self._user_service = user_service
        self._sessions = sessions
        self._organization_service = organization_service
        self._auth_service = auth_service

        self._all_users: list[UserSummaryOut] = []
        self._roles: list[RoleOut] = []
        self._password_policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY
        # Used only to disable "Deactivate" on the logged-in admin's own
        # row — a convenience against an accidental self-lockout, not a
        # security boundary (nothing server-side stops self-deactivation
        # today; a determined caller bypassing the UI still could).
        self._current_user_id = None
        self._page = 1
        self._sort_by = "full_name"
        self._sort_desc = False

        if organization_service is not None:
            worker = Worker(organization_service.get_current_organization)
            worker.signals.finished.connect(self._on_organization_loaded)
            worker.signals.error.connect(
                lambda exc: _logger.exception("Failed to load organization password policy",
                                              exc_info=exc))
            QThreadPool.globalInstance().start(worker)

        if auth_service is not None:
            worker = Worker(auth_service.get_current_user)
            worker.signals.finished.connect(self._on_current_user_loaded)
            worker.signals.error.connect(
                lambda exc: _logger.exception("Failed to load the current user",
                                              exc_info=exc))
            QThreadPool.globalInstance().start(worker)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Users", "Manage staff accounts and roles."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._load, render=self._render_table,
            is_empty=lambda result: len(result.items) == 0,
            empty_state=EmptyStateWidget(
                "No users found", icon="👤",
                message="Try a different search or filter, or add your first user."),
            error_message="Couldn't load users.")
        layout.addWidget(self._async_area, stretch=1)

        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        layout.addWidget(self._pagination)

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self.refresh)

        self._load_roles()

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name, username, or email…")
        self._search.setFixedWidth(240)
        self._search.textChanged.connect(self._on_search_changed)
        bar.addWidget(self._search)

        self._role_filter = QComboBox()
        self._role_filter.addItem("All Roles", None)
        self._role_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._role_filter)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        self._status_filter.addItem("Active", True)
        self._status_filter.addItem("Inactive", False)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._status_filter)

        bar.addStretch()

        refresh_button = QPushButton("Refresh")
        refresh_button.setObjectName("ghost")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.clicked.connect(self.refresh)
        bar.addWidget(refresh_button)

        if self._can("users.create"):
            add_button = QPushButton("+ Add User")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)

        return bar

    def _load_roles(self) -> None:
        worker = Worker(self._user_service.list_roles)
        worker.signals.finished.connect(self._on_roles_loaded)
        worker.signals.error.connect(
            lambda exc: _logger.exception("Failed to load roles", exc_info=exc))
        QThreadPool.globalInstance().start(worker)

    def _on_roles_loaded(self, roles: list[RoleOut]) -> None:
        self._roles = roles
        previous = self._role_filter.currentData()
        self._role_filter.clear()
        self._role_filter.addItem("All Roles", None)
        for role in roles:
            self._role_filter.addItem(role.name, role.id)
        select_by_data(self._role_filter, previous)

    def _on_current_user_loaded(self, user) -> None:
        self._current_user_id = user.id

    def _on_organization_loaded(self, organization) -> None:
        self._password_policy = PasswordPolicy(
            min_length=organization.password_min_length,
            require_uppercase=organization.password_require_uppercase,
            require_number=organization.password_require_number,
            require_special_char=organization.password_require_special_char)

    # -- data flow ------------------------------------------------------#
    def refresh(self) -> None:
        self._async_area.reload()

    def _on_search_changed(self) -> None:
        self._page = 1
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)

    def _on_filter_changed(self) -> None:
        self._page = 1
        self.refresh()

    def _on_page_changed(self, page: int) -> None:
        self._page = page
        self.refresh()

    def _on_sort_clicked(self, column: int) -> None:
        _, sort_key = _COLUMNS[column]
        if sort_key is None:
            return
        if self._sort_by == sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_by = sort_key
            self._sort_desc = False
        self._page = 1
        self.refresh()

    def _load(self) -> _UsersPageResult:
        self._all_users = self._user_service.list_users()

        search = self._search.text().strip().lower()
        role_id = self._role_filter.currentData()
        status = self._status_filter.currentData()

        filtered = self._all_users
        if search:
            filtered = [u for u in filtered if search in u.full_name.lower()
                       or search in u.username.lower() or search in u.email.lower()]
        if role_id is not None:
            filtered = [u for u in filtered if u.role_id == role_id]
        if status is not None:
            filtered = [u for u in filtered if u.is_active == status]

        filtered = sorted(filtered, key=self._sort_key, reverse=self._sort_desc)

        total = len(filtered)
        total_pages = max(1, -(-total // _PAGE_SIZE))
        page = min(self._page, total_pages)
        start = (page - 1) * _PAGE_SIZE
        items = filtered[start:start + _PAGE_SIZE]
        return _UsersPageResult(items=items, page=page, total_pages=total_pages, total=total)

    def _sort_key(self, user: UserSummaryOut):
        value = getattr(user, self._sort_by)
        if value is None:
            return (0, "")
        if isinstance(value, str):
            return (1, value.lower())
        return (1, value)

    # -- table rendering --------------------------------------------- #
    def _render_table(self, result: _UsersPageResult) -> QTableWidget:
        self._page = result.page
        self._pagination.set_state(page=result.page, total_pages=result.total_pages,
                                   total=result.total)

        table = QTableWidget(len(result.items), len(_COLUMNS))
        table.setHorizontalHeaderLabels([label for label, _ in _COLUMNS])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(48)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().sectionClicked.connect(self._on_sort_clicked)
        table.doubleClicked.connect(
            lambda index: self._open_view(result.items[index.row()])
            if self._can("users.view") else None)

        for row, user in enumerate(result.items):
            table.setCellWidget(row, _AVATAR_COL, self._build_avatar(user))

            table.setItem(row, 1, QTableWidgetItem(user.full_name))
            table.setItem(row, 2, QTableWidgetItem(user.username))
            table.setItem(row, 3, QTableWidgetItem(user.email))
            table.setItem(row, 4, QTableWidgetItem(user.role_name))

            status_item = QTableWidgetItem("Active" if user.is_active else "Inactive")
            status_item.setForeground(QColor(GREEN_DARK if user.is_active else RED))
            table.setItem(row, 5, status_item)

            last_login = (user.last_login_at.strftime("%Y-%m-%d %H:%M")
                         if user.last_login_at else "Never")
            table.setItem(row, 6, QTableWidgetItem(last_login))
            table.setItem(row, 7, QTableWidgetItem(user.created_at.strftime("%Y-%m-%d")))

            table.setCellWidget(row, _ACTIONS_COL, self._build_actions_button(user))
        return table

    def _build_avatar(self, user: UserSummaryOut) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.addWidget(AvatarLabel(user.full_name, size=30, font_size=11))
        layout.addStretch()
        return holder

    def _build_actions_button(self, user: UserSummaryOut) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 0, 8, 0)

        button = QToolButton()
        button.setText("Actions ▾")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet(f"border: none; color: {MUTED}; font-size: 12px; padding: 4px 6px;")

        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        if self._can("users.view"):
            menu.addAction("View").triggered.connect(
                lambda checked=False, u=user: self._open_view(u))
        if self._can("users.update"):
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, u=user: self._open_edit(u))
        if self._can("users.manage_roles"):
            menu.addAction("Change Role").triggered.connect(
                lambda checked=False, u=user: self._open_change_role(u))
        if self._can("users.reset_password"):
            menu.addAction("Reset Password").triggered.connect(
                lambda checked=False, u=user: self._reset_password(u))
        if self._can("users.update") or self._can("users.deactivate"):
            menu.addSeparator()
        if self._can("users.update") and not user.is_active:
            menu.addAction("Activate").triggered.connect(
                lambda checked=False, u=user: self._activate(u))
        if self._can("users.deactivate") and user.is_active:
            deactivate_action = menu.addAction("Deactivate")
            if user.id == self._current_user_id:
                deactivate_action.setEnabled(False)
                deactivate_action.setToolTip("You can't deactivate your own account.")
            else:
                deactivate_action.triggered.connect(
                    lambda checked=False, u=user: self._deactivate(u))

        if menu.isEmpty():
            button.setEnabled(False)
        button.setMenu(menu)
        layout.addWidget(button)
        layout.addStretch()
        return holder

    # -- dialogs -------------------------------------------------------- #
    def _open_add_dialog(self) -> None:
        session = self._sessions.peek()
        organization_id = session.organization_id if session else None
        dialog = UserFormDialog(self._user_service, self._roles, organization_id, parent=self,
                                password_policy=self._password_policy)
        if dialog.exec():
            self.refresh()

    def _open_view(self, user: UserSummaryOut) -> None:
        dialog = UserFormDialog(self._user_service, self._roles, None, user=user,
                                read_only=True, parent=self)
        dialog.exec()

    def _open_edit(self, user: UserSummaryOut) -> None:
        dialog = UserFormDialog(self._user_service, self._roles, None, user=user, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_change_role(self, user: UserSummaryOut) -> None:
        dialog = ChangeUserRoleDialog(self._user_service, self._roles, user, parent=self)
        if dialog.exec():
            self.refresh()

    # -- row actions ------------------------------------------------------#
    def _reset_password(self, user: UserSummaryOut) -> None:
        if not confirm(self, "Reset Password",
                       f"Generate a new temporary password for {user.full_name!r}? "
                       "They will need to change it on next login.",
                       confirm_label="Reset Password", danger=True):
            return
        worker = Worker(self._user_service.reset_password, user.id)
        worker.signals.finished.connect(
            lambda password: self._on_password_reset(user, password))
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_password_reset(self, user: UserSummaryOut, temporary_password: str) -> None:
        QMessageBox.information(
            self, "Temporary Password Generated",
            f"Temporary password for {user.full_name!r}:\n\n{temporary_password}\n\n"
            "Share it with them securely — it will not be shown again.")
        self.refresh()

    def _activate(self, user: UserSummaryOut) -> None:
        self._run_action(self._user_service.activate_user, user.id)

    def _deactivate(self, user: UserSummaryOut) -> None:
        if confirm(self, "Deactivate User",
                  f"Deactivate {user.full_name!r}? They will no longer be able to log in.",
                  confirm_label="Deactivate", danger=True):
            self._run_action(self._user_service.deactivate_user, user.id)

    def _run_action(self, fn, *args) -> None:
        worker = Worker(fn, *args)
        worker.signals.finished.connect(lambda _=None: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("User action failed", exc_info=exc)
        if isinstance(exc, (OwnerProtectedError, UserNotFoundError,
                            MembershipNotFoundError, RoleNotFoundError)):
            QMessageBox.warning(self, "Action Failed", str(exc))
        else:
            QMessageBox.warning(self, "Action Failed",
                                "Something went wrong. Please try again.")
