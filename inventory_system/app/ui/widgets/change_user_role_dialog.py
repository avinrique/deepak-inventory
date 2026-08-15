"""Change a staff member's role — deliberately separate from UserFormDialog:
UserService.change_user_role is its own permission (users.manage_roles,
distinct from users.update) and its own domain rule (an organization's
Owner can't be demoted, raised as OwnerProtectedError), so it gets its own
small, focused dialog rather than folding role into the profile-edit form.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QPushButton, QVBoxLayout

from app.core.exceptions import OwnerProtectedError, RoleNotFoundError
from app.schemas.user import RoleOut, UserSummaryOut
from app.services.user_service import UserService
from app.ui.theme import RED, STYLESHEET
from app.workers.base_worker import Worker


class ChangeUserRoleDialog(QDialog):
    def __init__(self, user_service: UserService, roles: list[RoleOut],
                user: UserSummaryOut, parent=None):
        super().__init__(parent)
        self._user_service = user_service
        self._user = user
        self.setWindowTitle("Change Role")
        self.setFixedWidth(360)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"Change role for {user.full_name!r}"))

        self._role = QComboBox()
        for role in roles:
            self._role.addItem(role.name, role.id)
        index = self._role.findData(user.role_id)
        if index >= 0:
            self._role.setCurrentIndex(index)
        layout.addWidget(self._role)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._save_button = QPushButton("Save Role")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.clicked.connect(self._submit)
        layout.addWidget(self._save_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("flat")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Role")

    def _submit(self) -> None:
        self._error_label.hide()
        role_id = self._role.currentData()
        if role_id is None:
            self._show_error("Select a role.")
            return
        self._set_busy(True)
        worker = Worker(self._user_service.change_user_role, self._user.id, role_id)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _membership) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, OwnerProtectedError):
            self._show_error("This user is the organization's Owner and cannot be demoted.")
        elif isinstance(exc, RoleNotFoundError):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong changing this user's role. "
                             "Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
