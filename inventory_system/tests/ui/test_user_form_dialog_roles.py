"""UserFormDialog's Role field on create.

The page passes in its own cached role list, but that cache is filled by an
async load. Before this was fixed, opening "Add User" while the load was
still in flight — or after it had failed, which was only ever written to a
log — built an empty "Role *" dropdown that never repopulated, so every
save attempt died on a misleading "Select a role." and creating a user was
impossible until the app was restarted.

Following this repo's UI-test convention, the async-triggering method is
kept thin and the callbacks it dispatches to (_on_roles_loaded /
_on_roles_error) are driven directly rather than through a real
QThreadPool Worker.
"""
import uuid

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.schemas.user import RoleOut
from app.ui.widgets.user_form_dialog import UserFormDialog


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _roles(*names) -> list[RoleOut]:
    return [RoleOut(id=uuid.uuid4(), name=n, description=None, is_system=True)
            for n in names]


class _StubUserService:
    def __init__(self):
        self.created: dict | None = None
        self.list_roles_calls = 0

    def list_roles(self):
        self.list_roles_calls += 1
        return []

    def create_user(self, **kwargs):
        self.created = kwargs
        return None


def _drain(service) -> int:
    """Lets any dispatched Worker finish, then reports how many times
    list_roles was actually called."""
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(2000)
    return service.list_roles_calls


def _fill_valid(dialog) -> None:
    dialog._full_name.setText("Asha Rao")
    dialog._email.setText("asha@example.com")
    dialog._password.setText("password1")
    dialog._confirm_password.setText("password1")


def _errors(dialog) -> list[str]:
    return dialog._validate_locally(full_name="Asha Rao", email="asha@example.com",
                                    username=None, phone=None)


def test_loads_roles_itself_when_the_page_cache_is_not_ready(qapp):
    """The regression: an empty `roles` argument must not produce a dead
    dropdown — the dialog fetches them and becomes usable."""
    service = _StubUserService()
    dialog = UserFormDialog(service, [], uuid.uuid4())

    # The fetch runs on a QThreadPool worker, so what is observable
    # synchronously is the state it puts the field into.
    assert dialog._role.currentText() == "Loading roles…"
    assert not dialog._role.isEnabled()
    assert not dialog._save_button.isEnabled()   # can't save into nothing
    assert _drain(service) == 1                  # ...and it really was dispatched

    dialog._on_roles_loaded(_roles("ACCOUNTANT", "ADMIN", "MANAGER"))

    assert [dialog._role.itemText(i) for i in range(dialog._role.count())] == \
        ["ACCOUNTANT", "ADMIN", "MANAGER"]
    assert dialog._role.isEnabled()
    assert dialog._save_button.isEnabled()


def test_selected_role_reaches_create_user(qapp):
    service = _StubUserService()
    dialog = UserFormDialog(service, [], uuid.uuid4())
    roles = _roles("ACCOUNTANT", "ADMIN", "MANAGER")
    dialog._on_roles_loaded(roles)

    dialog._role.setCurrentIndex(2)
    _fill_valid(dialog)
    assert _errors(dialog) == []

    dialog._submit()
    # _submit dispatches through a Worker; the role it captured is what
    # matters here, so read it off the call the worker was built with.
    assert dialog._role.currentData() == roles[2].id
    assert roles[2].name == "MANAGER"


def test_cached_roles_are_used_without_a_second_query(qapp):
    service = _StubUserService()
    roles = _roles("ACCOUNTANT", "ADMIN")
    dialog = UserFormDialog(service, roles, uuid.uuid4())

    assert _drain(service) == 0
    assert dialog._role.count() == 2
    assert dialog._role.currentData() == roles[0].id
    assert dialog._save_button.isEnabled()


def test_still_loading_says_so_instead_of_select_a_role(qapp):
    service = _StubUserService()
    dialog = UserFormDialog(service, [], uuid.uuid4())
    _fill_valid(dialog)

    assert any("still loading" in e for e in _errors(dialog))


def test_failed_role_load_is_visible_and_explained(qapp):
    service = _StubUserService()
    dialog = UserFormDialog(service, [], uuid.uuid4())

    dialog._on_roles_error(RuntimeError("connection reset"))

    assert dialog._error_label.text()
    assert not dialog._role.isEnabled()
    _fill_valid(dialog)
    assert any("couldn't be loaded" in e for e in _errors(dialog))


def test_a_database_with_no_roles_is_reported(qapp):
    service = _StubUserService()
    dialog = UserFormDialog(service, [], uuid.uuid4())

    dialog._on_roles_loaded([])

    assert "No roles are configured" in dialog._error_label.text()


def test_edit_mode_has_no_role_field_and_never_loads_roles(qapp):
    """Role changes go through the separate, permission-gated Change Role
    dialog — editing must not start a role load."""
    from datetime import datetime, timezone

    from app.schemas.user import UserSummaryOut

    service = _StubUserService()
    user = UserSummaryOut(id=uuid.uuid4(), email="a@b.com", username="ab",
                          full_name="A B", phone=None, is_active=True,
                          is_superuser=False, must_change_password=False,
                          role_id=uuid.uuid4(), role_name="ADMIN",
                          created_at=datetime.now(timezone.utc), last_login_at=None)
    UserFormDialog(service, [], None, user=user)

    assert _drain(service) == 0
