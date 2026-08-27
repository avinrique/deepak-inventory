"""app.ui.setup_wizard — the only route from "just ran the installer" to a
working login, so its failure modes matter more than most UI code."""
import pytest
from sqlalchemy.engine import make_url

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.ui.setup_wizard import SetupWizard, build_url


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


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
