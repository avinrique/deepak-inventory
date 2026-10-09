"""First-run database setup.

A packaged .exe cannot ship with credentials in it, so a fresh installation
has to ask. This dialog is where it asks, and it is the only route by which
a non-technical user can get from "I just ran the installer" to a working
login. It does four jobs in order:

1. Ask *where* the data should live. "On this computer" uses the PostgreSQL
   server bundled with the installer (app.database.local_server) and needs
   no input at all; "on a server or cloud database" goes to step 2.
2. Collect and *verify* connection details before saving them, so a typo
   fails here with an explanation instead of at the next launch with a
   dialog nobody can act on.
3. Offer to initialize a database that has no schema yet -- running the
   migrations and seeding the role catalog.
4. Create the first Organization and OWNER account when the database has no
   users, which is otherwise impossible: every other path to creating a user
   requires an authenticated session, and there is nobody to authenticate as.

Steps 3 and 4 are shared: the local choice produces a verified URL and then
rejoins the same code path the typed-in one uses, so there is exactly one
implementation of "prepare an empty database".

Also reachable afterwards from the startup error dialog, for moving an
installation to a different server.
"""
import logging

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.engine import make_url

from app.__version__ import APP_NAME
from app.config import store
from app.config.settings import reload_settings, settings
from app.core.exceptions import AppError
from app.database import bootstrap, local_server
from app.database.dsn import build_url
from app.database.session import reset_engine, test_connection
from app.ui.theme import GREEN, MUTED, RED, STYLESHEET, TEXT, scale
from app.ui.widgets.responsive import fit_to_screen, wrap_in_scroll
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

_SSL_MODES = [
    ("Require (recommended for cloud databases)", "require"),
    ("Prefer", "prefer"),
    ("Disable (local server only)", "disable"),
    ("Verify full", "verify-full"),
]

# The three pages, in the order they sit in the QStackedWidget. Named
# because the flow branches: choosing "on this computer" skips the
# connection form entirely and lands straight on the initialize page.
_PAGE_CHOICE = 0
_PAGE_CONNECTION = 1
_PAGE_INITIALIZE = 2

_PAGE_HEADINGS = {
    _PAGE_CHOICE: (
        "Where should your data be stored?",
        "This is where your products, bills and customers will be kept."),
    _PAGE_CONNECTION: (
        "Connect to your database",
        "These details are stored on this computer only. Your administrator "
        "can supply them if you do not have them."),
    _PAGE_INITIALIZE: (
        "Set up this database",
        "This database is empty. It will be prepared for use, and the "
        "account you enter below becomes its administrator."),
}


class SetupWizard(QDialog):
    """Returns QDialog.Accepted only once a verified connection is saved."""

    configuration_saved = Signal()

    def __init__(self, parent: QWidget | None = None, *, allow_cancel: bool = True):
        super().__init__(parent)
        self._allow_cancel = allow_cancel
        self._verified_url: str | None = None
        self._busy = False
        # Which page "Back" returns to from the initialize page. The local
        # choice never visits the connection form, so it cannot be assumed.
        self._initialize_came_from = _PAGE_CONNECTION
        # Which kind of database the user picked, recorded alongside the URL
        # so the next launch knows whether to start a server first.
        self._managed_locally = False

        self.setWindowTitle(f"{APP_NAME} — Database Setup")
        self.setStyleSheet(STYLESHEET)
        self.setModal(True)
        fit_to_screen(self, 560, 620, minimum_width=420, minimum_height=360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(scale(28), scale(24), scale(28), scale(20))
        outer.setSpacing(scale(12))

        self._title = QLabel()
        self._title.setObjectName("pageTitle")
        self._subtitle = QLabel()
        self._subtitle.setObjectName("pageSubtitle")
        self._subtitle.setWordWrap(True)
        outer.addWidget(self._title)
        outer.addWidget(self._subtitle)

        self._pages = QStackedWidget()
        self._pages.addWidget(wrap_in_scroll(self._build_choice_page()))
        self._pages.addWidget(wrap_in_scroll(self._build_connection_page()))
        self._pages.addWidget(wrap_in_scroll(self._build_initialize_page()))
        outer.addWidget(self._pages, stretch=1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.hide()
        outer.addWidget(self._status)

        # Footer lives outside the scroll area, so these stay reachable no
        # matter how short the screen is.
        outer.addLayout(self._build_footer())
        self._load_existing_values()
        self._show_page(_PAGE_CHOICE)

    # -- pages ------------------------------------------------------------ #
    def _build_choice_page(self) -> QWidget:
        """Two buttons that navigate, rather than radios plus Continue.

        A choice needs no verification, so there is nothing for Continue to
        gate on here -- and keeping Continue off this page is what leaves the
        "never save an untested connection" rule on the connection page
        exactly as it was.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, scale(8), 0, 0)
        layout.setSpacing(scale(8))

        self._local_button = QPushButton("On this computer")
        self._local_button.setObjectName("primary")
        self._local_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._local_button.clicked.connect(self._choose_local)
        layout.addWidget(self._local_button)

        self._local_note = QLabel(
            "Recommended. Everything stays on this PC — nothing is sent over "
            "the internet, and there is nothing to set up or sign up for.")
        self._local_note.setWordWrap(True)
        self._local_note.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._local_note)

        layout.addSpacing(scale(10))

        remote_button = QPushButton("On a server or cloud database")
        remote_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_button.clicked.connect(self._choose_remote)
        layout.addWidget(remote_button)

        remote_note = QLabel(
            "For a PostgreSQL database that already exists. You will need its "
            "address, username and password.")
        remote_note.setWordWrap(True)
        remote_note.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(remote_note)
        layout.addStretch(1)

        # Disabled rather than hidden, with the reason on show: a build
        # assembled without the server binaries is a packaging oversight, and
        # silently dropping the option would look like the feature never
        # existed. Same treatment as Backup without pg_dump.
        if not local_server.is_bundled():
            self._local_button.setEnabled(False)
            self._local_note.setText(
                "Unavailable: this copy of the application was installed "
                "without the built-in database. Reinstalling should restore "
                "it, or use a server below.")
        return page

    def _build_connection_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(0, scale(8), 0, 0)
        form.setSpacing(scale(10))

        self._host = QLineEdit()
        self._host.setPlaceholderText("localhost, or your cloud database host")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(5432)
        self._database = QLineEdit()
        self._database.setPlaceholderText("inventory")
        self._username = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._sslmode = QComboBox()
        for label, value in _SSL_MODES:
            self._sslmode.addItem(label, value)

        form.addRow("Server", self._host)
        form.addRow("Port", self._port)
        form.addRow("Database", self._database)
        form.addRow("Username", self._username)
        form.addRow("Password", self._password)
        form.addRow("Encryption", self._sslmode)

        paste = QPushButton("Paste a connection link instead…")
        paste.setObjectName("linkButton")
        paste.setCursor(Qt.CursorShape.PointingHandCursor)
        paste.clicked.connect(self._paste_url)
        form.addRow("", paste)

        for field in (self._host, self._database, self._username, self._password):
            field.textChanged.connect(self._invalidate_verification)
        self._port.valueChanged.connect(self._invalidate_verification)
        self._sslmode.currentIndexChanged.connect(self._invalidate_verification)
        return page

    def _build_initialize_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, scale(8), 0, 0)
        layout.setSpacing(scale(10))

        self._initialize_note = QLabel()
        self._initialize_note.setWordWrap(True)
        layout.addWidget(self._initialize_note)

        self._owner_form_host = QWidget()
        owner_form = QFormLayout(self._owner_form_host)
        owner_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        owner_form.setContentsMargins(0, 0, 0, 0)
        owner_form.setSpacing(scale(10))

        self._org_name = QLineEdit()
        self._org_name.setPlaceholderText("Your business name")
        self._owner_name = QLineEdit()
        self._owner_email = QLineEdit()
        self._owner_email.setPlaceholderText("you@company.com")
        self._owner_password = QLineEdit()
        self._owner_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._owner_confirm = QLineEdit()
        self._owner_confirm.setEchoMode(QLineEdit.EchoMode.Password)

        owner_form.addRow("Business name", self._org_name)
        owner_form.addRow("Your name", self._owner_name)
        owner_form.addRow("Email", self._owner_email)
        owner_form.addRow("Password", self._owner_password)
        owner_form.addRow("Confirm password", self._owner_confirm)
        layout.addWidget(self._owner_form_host)
        layout.addStretch(1)
        return page

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setSpacing(scale(8))

        self._cancel_button = QPushButton("Cancel" if self._allow_cancel else "Quit")
        self._cancel_button.clicked.connect(self._on_cancel)
        footer.addWidget(self._cancel_button)
        footer.addStretch(1)

        self._back_button = QPushButton("Back")
        self._back_button.clicked.connect(self._go_back)
        self._back_button.hide()
        footer.addWidget(self._back_button)

        self._test_button = QPushButton("Test Connection")
        self._test_button.clicked.connect(self._test_connection)
        footer.addWidget(self._test_button)

        self._primary_button = QPushButton("Continue")
        self._primary_button.setObjectName("primary")
        self._primary_button.setDefault(True)
        self._primary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary_button.clicked.connect(self._on_primary)
        self._primary_button.setEnabled(False)
        footer.addWidget(self._primary_button)
        return footer

    # -- navigation ------------------------------------------------------- #
    def _show_page(self, index: int) -> None:
        """The single place that decides what the header and footer say.

        Three pages with branching navigation had the heading and the button
        states being reassigned from four different methods, which is how a
        "Back" ends up on a page whose title belongs to another one.
        """
        self._pages.setCurrentIndex(index)
        title, subtitle = _PAGE_HEADINGS[index]
        self._title.setText(title)
        self._subtitle.setText(subtitle)
        self._status.hide()

        self._back_button.setVisible(index != _PAGE_CHOICE)
        self._test_button.setVisible(index == _PAGE_CONNECTION)
        self._primary_button.setVisible(index != _PAGE_CHOICE)

        if index == _PAGE_CONNECTION:
            self._primary_button.setText("Continue")
            # Still gated on a successful test -- navigating onto the page
            # does not count as having tried the connection.
            self._primary_button.setEnabled(self._verified_url is not None)
        elif index == _PAGE_INITIALIZE:
            self._primary_button.setText("Set Up Database")
            self._primary_button.setEnabled(True)

    def _go_back(self) -> None:
        if self._pages.currentIndex() == _PAGE_INITIALIZE:
            self._show_page(self._initialize_came_from)
        else:
            self._show_page(_PAGE_CHOICE)

    # -- state ------------------------------------------------------------ #
    def _load_existing_values(self) -> None:
        """Pre-fills from whatever is configured, so "change the server" is
        an edit rather than a retype. The password is deliberately left
        blank -- it is re-entered, never displayed."""
        if not settings.database_url:
            return
        try:
            url = make_url(settings.database_url)
        except Exception:  # noqa: BLE001 - a corrupt URL just means empty fields
            return
        self._host.setText(url.host or "")
        self._port.setValue(url.port or 5432)
        self._database.setText(url.database or "")
        self._username.setText(url.username or "")
        index = self._sslmode.findData(url.query.get("sslmode", "require"))
        if index >= 0:
            self._sslmode.setCurrentIndex(index)

    def _current_url(self) -> str:
        return build_url(host=self._host.text().strip(), port=self._port.value(),
                         database=self._database.text().strip(),
                         username=self._username.text().strip(),
                         password=self._password.text(),
                         sslmode=self._sslmode.currentData())

    def _invalidate_verification(self) -> None:
        """Any edit invalidates a previous successful test, so Continue can
        never save details that were never actually tried."""
        self._verified_url = None
        self._primary_button.setEnabled(False)
        self._status.hide()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for widget in (self._test_button, self._primary_button, self._back_button,
                       self._pages):
            widget.setEnabled(not busy)
        self._cancel_button.setEnabled(not busy)
        if busy:
            self._show_status(message, "info")

    def _show_status(self, message: str, level: str = "info") -> None:
        colour = {"error": RED, "success": GREEN, "info": MUTED}.get(level, TEXT)
        self._status.setStyleSheet(f"color: {colour}; font-size: 12px;")
        self._status.setText(message)
        self._status.show()

    # -- actions ---------------------------------------------------------- #
    def _paste_url(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Paste a connection link",
            "Paste the full connection link your administrator gave you:")
        if not ok or not text.strip():
            return
        try:
            url = make_url(text.strip())
        except Exception:  # noqa: BLE001 - user-pasted text
            self._show_status("That does not look like a database connection link.",
                              "error")
            return
        self._host.setText(url.host or "")
        self._port.setValue(url.port or 5432)
        self._database.setText(url.database or "")
        self._username.setText(url.username or "")
        self._password.setText(url.password or "")
        index = self._sslmode.findData(url.query.get("sslmode", "require"))
        if index >= 0:
            self._sslmode.setCurrentIndex(index)
        self._show_status("Details filled in. Test the connection to continue.", "info")

    def _test_connection(self) -> None:
        if not self._host.text().strip() or not self._database.text().strip():
            self._show_status("Enter at least a server and a database name.", "error")
            return
        url = self._current_url()
        self._set_busy(True, "Connecting…")
        worker = Worker(test_connection, url)
        worker.signals.finished.connect(lambda _result: self._on_test_ok(url))
        worker.signals.error.connect(self._on_test_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_test_ok(self, url: str) -> None:
        self._set_busy(False)
        self._verified_url = url
        self._primary_button.setEnabled(True)
        self._show_status("Connected successfully.", "success")

    def _on_test_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._verified_url = None
        self._primary_button.setEnabled(False)
        _logger.warning("Setup wizard connection test failed: %s", exc)
        self._show_status(str(exc) if isinstance(exc, AppError)
                          else "Could not connect to that database.", "error")

    def _on_primary(self) -> None:
        if self._pages.currentIndex() == _PAGE_CONNECTION:
            self._advance_from_connection()
        else:
            self._finish_initialization()

    def _choose_remote(self) -> None:
        self._managed_locally = False
        self._initialize_came_from = _PAGE_CONNECTION
        self._show_page(_PAGE_CONNECTION)

    # -- the "on this computer" path -------------------------------------- #
    def _choose_local(self) -> None:
        """Prepares and starts the bundled server, then rejoins the normal
        flow.

        There is nothing for the user to type and nothing for them to test,
        because ensure_running() *is* the test: it only returns a DSN once it
        has created the database over a real connection to the server it just
        started. So it produces a verified URL the same way Test Connection
        does, and everything after this point is the existing code path.
        """
        self._managed_locally = True
        self._initialize_came_from = _PAGE_CHOICE
        self._title.setText("Setting up your database")
        self._subtitle.setText(
            "Preparing the database on this computer. The first time, this "
            "takes about a minute.")
        self._set_busy(True, "Working…")
        worker = Worker(local_server.ensure_running)
        worker.signals.finished.connect(self._on_local_server_ready)
        worker.signals.error.connect(self._on_local_server_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_local_server_ready(self, dsn: str) -> None:
        self._set_busy(False)
        self._verified_url = dsn
        self._advance_from_connection()

    def _on_local_server_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._show_page(_PAGE_CHOICE)
        _logger.exception("Could not start the local database", exc_info=exc)
        self._show_status(
            str(exc) if isinstance(exc, AppError)
            else "The database on this computer could not be started. See the "
                 "log file for details.", "error")

    def _advance_from_connection(self) -> None:
        """Applies the verified connection, then decides whether the database
        still needs schema, an owner, or nothing at all."""
        assert self._verified_url is not None
        self._set_busy(True, "Checking the database…")
        worker = Worker(self._inspect_database, self._verified_url,
                        self._managed_locally)
        worker.signals.finished.connect(self._on_inspected)
        worker.signals.error.connect(self._on_test_failed)
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _inspect_database(url: str, managed_locally: bool) -> dict:
        """Runs on a worker thread. Applies the connection first so that
        has_any_users()/the schema check look at the *new* database rather
        than whatever the process was pointed at before.

        Both routes through the wizard converge here, which is why the
        managed-locally flag is written here too rather than on either branch:
        choosing a remote server has to *clear* it, or an installation that
        once used the built-in database would keep starting a server it no
        longer connects to.
        """
        from app.database.schema_check import database_is_empty

        store.save({**store.load(), "database_url": url,
                    "database_managed_locally": managed_locally})
        reload_settings()
        reset_engine()
        empty = database_is_empty()
        return {"needs_schema": empty,
                "needs_owner": True if empty else not bootstrap.has_any_users()}

    def _on_inspected(self, state: dict) -> None:
        self._set_busy(False)
        if not state["needs_schema"] and not state["needs_owner"]:
            self._show_status("Connected.", "success")
            self._accept_saved()
            return

        self._show_page(_PAGE_INITIALIZE)
        self._initialize_note.setText(
            "The database has no tables yet — they will be created now."
            if state["needs_schema"] else
            "The database has no user accounts yet.")
        self._org_name.setFocus()

    def _finish_initialization(self) -> None:
        password = self._owner_password.text()
        if password != self._owner_confirm.text():
            self._show_status("The two passwords do not match.", "error")
            return

        details = {
            "organization_name": self._org_name.text(),
            "full_name": self._owner_name.text(),
            "email": self._owner_email.text(),
            "password": password,
        }
        self._set_busy(True, "Setting up the database… this can take a minute.")
        worker = Worker(self._run_initialization, self._verified_url, details)
        worker.signals.finished.connect(lambda _result: self._accept_saved())
        worker.signals.error.connect(self._on_initialize_failed)
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _run_initialization(url: str, details: dict) -> None:
        bootstrap.initialize(url)
        if not bootstrap.has_any_users():
            bootstrap.create_first_owner(**details)

    def _on_initialize_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        _logger.exception("Database initialization failed", exc_info=exc)
        message = (str(exc) if isinstance(exc, (AppError, bootstrap.BootstrapError))
                   else "The database could not be set up. See the log file for details.")
        self._show_status(message, "error")

    def _accept_saved(self) -> None:
        reload_settings()
        reset_engine()
        _logger.info("Database configuration saved: %s",
                     store.redacted_url(settings.database_url))
        self.configuration_saved.emit()
        self.accept()

    def _on_cancel(self) -> None:
        self.reject()

    def reject(self) -> None:  # noqa: D102 - Qt override
        if self._busy:
            return  # never tear the dialog down mid-migration
        super().reject()
