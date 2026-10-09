"""app.ui.setup_wizard — the only route from "just ran the installer" to a
working login, so its failure modes matter more than most UI code."""
import pytest
from sqlalchemy.engine import make_url

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.database.dsn import build_url
from app.ui.setup_wizard import SetupWizard


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keeps the wizard's saves off the developer's real config.json.

    Not theoretical. This dialog's whole job is to call store.save(), and
    the autouse drain_qt_work fixture in tests/conftest.py deliberately runs
    every queued Worker to completion at teardown — so a test that kicks off
    the save path really does reach the file, after the test body has
    finished and stopped watching.
    """
    from app.config import store

    monkeypatch.setattr(store, "config_file", lambda: tmp_path / "config.json")


@pytest.mark.parametrize("password", [
    "p@ssword",          # '@' would split the URL at the wrong place
    "pa:ss",             # ':' would split username from password
    "pass word",         # a space
    "a/b?c#d",           # path, query and fragment delimiters
    "100%sure",          # a stray percent sign
])
def test_passwords_with_url_metacharacters_survive_the_round_trip(password):
    """The wizard takes a raw password from a text box and has to produce a
    valid DSN. Pasting it together by hand silently yields a URL that parses
    into a different username and host, which then fails as "wrong password"
    — with the real password sitting right there in the box."""
    url = build_url(host="db.example.com", port=5432, database="inventory",
                    username="admin", password=password, sslmode="require")

    parsed = make_url(url)
    assert parsed.password == password
    assert parsed.username == "admin"
    assert parsed.host == "db.example.com"
    assert parsed.database == "inventory"


def test_the_ssl_mode_reaches_the_url():
    """Dropping it downgrades a cloud connection that must be encrypted."""
    url = build_url(host="h", port=5432, database="d", username="u", password="p",
                    sslmode="require")

    assert make_url(url).query["sslmode"] == "require"


def test_the_wizard_fits_on_a_small_screen(qapp, monkeypatch):
    """1366x768 at 125% scaling leaves a 1092x614 logical desktop; a minimum
    larger than that cannot be resized out of."""
    import app.ui.widgets.responsive as responsive
    monkeypatch.setattr(responsive, "available_size", lambda widget=None: (1092, 614))

    wizard = SetupWizard()

    assert wizard.minimumWidth() <= 1092
    assert wizard.minimumHeight() <= 614


def test_continue_is_disabled_until_a_connection_has_been_tested(qapp):
    """Saving untested details is how an install ends up permanently broken
    with a message the user cannot connect to what they typed."""
    wizard = SetupWizard()

    assert wizard._primary_button.isEnabled() is False


def test_editing_a_field_invalidates_an_earlier_successful_test(qapp):
    wizard = SetupWizard()
    wizard._on_test_ok("postgresql+psycopg://u:p@h/db")
    assert wizard._primary_button.isEnabled() is True

    wizard._host.setText("a-different-server")

    assert wizard._verified_url is None
    assert wizard._primary_button.isEnabled() is False


def test_a_failed_test_shows_the_real_reason(qapp):
    from app.core.exceptions import DatabaseAuthenticationError

    wizard = SetupWizard()
    wizard._on_test_failed(DatabaseAuthenticationError(
        "The database rejected the username or password."))

    assert "rejected" in wizard._status.text()
    assert wizard._primary_button.isEnabled() is False


# -- the "where should the data live" page -------------------------------- #

def test_the_wizard_opens_on_the_storage_choice(qapp):
    """Not on the connection form. A shop that just ran the installer should
    never have to recognise that a blank "Server" box means it needs a cloud
    database."""
    from app.ui.setup_wizard import _PAGE_CHOICE

    wizard = SetupWizard()

    assert wizard._pages.currentIndex() == _PAGE_CHOICE


def test_the_local_option_is_disabled_when_no_server_was_bundled(qapp, monkeypatch):
    """Disabled with a reason, not hidden: a build staged without
    fetch_pgserver.py is a packaging mistake, and silently dropping the
    option makes it look like the feature never existed."""
    from app.database import local_server

    monkeypatch.setattr(local_server, "is_bundled", lambda: False)

    wizard = SetupWizard()

    assert wizard._local_button.isEnabled() is False
    assert "Unavailable" in wizard._local_note.text()


def test_the_local_option_is_offered_when_a_server_was_bundled(qapp, monkeypatch):
    from app.database import local_server

    monkeypatch.setattr(local_server, "is_bundled", lambda: True)

    wizard = SetupWizard()

    assert wizard._local_button.isEnabled() is True


def test_choosing_the_local_database_never_builds_a_url_from_the_form(qapp, monkeypatch):
    """The local path has no form to read. If it ever started going through
    the connection fields it would silently connect to a blank host."""
    from app.database import local_server

    monkeypatch.setattr(local_server, "is_bundled", lambda: True)
    monkeypatch.setattr("app.database.dsn.build_url",
                        lambda **_kwargs: pytest.fail("build_url was called"))
    monkeypatch.setattr(local_server, "ensure_running",
                        lambda: "postgresql+psycopg://inventory_app:p@127.0.0.1:5432/inventory")
    monkeypatch.setattr(SetupWizard, "_advance_from_connection", lambda self: None)

    wizard = SetupWizard()
    wizard._on_local_server_ready(
        "postgresql+psycopg://inventory_app:p@127.0.0.1:5432/inventory")

    assert wizard._verified_url is not None


def test_the_local_path_records_that_the_server_is_ours_to_start(qapp, monkeypatch):
    """app.main reads this flag to decide whether to start a server before
    connecting; without it the second launch would fail."""
    from app.database import local_server

    monkeypatch.setattr(local_server, "is_bundled", lambda: True)
    monkeypatch.setattr(local_server, "ensure_running", lambda: "postgresql+psycopg://x")
    # Stop at the point the flag is set: going further would have the worker
    # chain try to inspect a database that does not exist.
    monkeypatch.setattr(SetupWizard, "_advance_from_connection", lambda self: None)
    wizard = SetupWizard()

    wizard._choose_local()

    assert wizard._managed_locally is True


def test_choosing_a_remote_server_clears_the_managed_flag(qapp):
    """Switching away from the built-in database has to turn the flag off,
    or the app keeps starting a server it no longer talks to."""
    wizard = SetupWizard()
    wizard._managed_locally = True

    wizard._choose_remote()

    assert wizard._managed_locally is False


def test_back_from_the_connection_form_returns_to_the_choice(qapp):
    from app.ui.setup_wizard import _PAGE_CHOICE

    wizard = SetupWizard()
    wizard._choose_remote()

    wizard._go_back()

    assert wizard._pages.currentIndex() == _PAGE_CHOICE


def test_back_from_setup_skips_the_form_the_local_path_never_showed(qapp, monkeypatch):
    """The local choice never visits the connection page, so "Back" from the
    owner-account page must not drop the user onto a blank form."""
    from app.database import local_server
    from app.ui.setup_wizard import _PAGE_CHOICE

    monkeypatch.setattr(local_server, "is_bundled", lambda: True)
    monkeypatch.setattr(local_server, "ensure_running", lambda: "postgresql+psycopg://x")
    wizard = SetupWizard()
    wizard._choose_local()
    wizard._on_inspected({"needs_schema": True, "needs_owner": True})

    wizard._go_back()

    assert wizard._pages.currentIndex() == _PAGE_CHOICE


def test_a_failed_local_start_returns_to_the_choice_with_the_reason(qapp, monkeypatch):
    from app.database import local_server
    from app.ui.setup_wizard import _PAGE_CHOICE

    monkeypatch.setattr(local_server, "is_bundled", lambda: True)
    wizard = SetupWizard()

    wizard._on_local_server_failed(
        local_server.LocalServerError("Running as an administrator."))

    assert wizard._pages.currentIndex() == _PAGE_CHOICE
    assert "administrator" in wizard._status.text()


def test_cancel_is_refused_while_a_migration_is_running(qapp):
    """Tearing the dialog down mid-migration would leave a half-migrated
    schema behind. Asserted on the signal rather than result(), which is
    already 0 (== Rejected) for any dialog that was never shown."""
    wizard = SetupWizard()
    closed = []
    wizard.rejected.connect(lambda: closed.append(True))
    wizard._set_busy(True, "Setting up…")

    wizard.reject()
    assert closed == []

    # ...and is allowed again once the work finishes.
    wizard._set_busy(False)
    wizard.reject()
    assert closed == [True]
