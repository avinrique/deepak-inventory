"""Entrypoint for the PySide6 production app.

Startup is an explicit, ordered sequence, and the order is load-bearing --
each step can fail, and every failure has to reach the user as a sentence
they can act on rather than as a window that never appears:

    1. create the user's data directories       (never inside Program Files)
    2. configure logging                         (so every later failure is recorded)
    3. install the crash handler                 (so an unhandled error is not silent)
    4. set the High-DPI rounding policy          (must precede QApplication)
    5. create QApplication, set identity + icon
    6. load the theme                            (bundled resource; may be missing)
    7. ensure a database is configured           (else: setup wizard)
    8. connect and check the schema              (distinguishing the ways it fails)
    9. build the container, show the login window

Run from source with `python -m app.main`. `--self-test` exercises the whole
UI without a database and exits, which is how CI verifies the *packaged*
executable rather than just the source tree.

Flow after startup: LoginWindow -> MainWindow -> (logout or idle timeout) ->
a fresh LoginWindow, without quitting the process -- this is a shared desktop
terminal, so returning to the login screen rather than exiting is the point.
"""
import argparse
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from app.__version__ import APP_NAME, ORG_NAME, VERSION, version_string
from app.config import settings as settings_module
from app.config.settings import settings
from app.core import crash_handler, paths
from app.core.exceptions import (
    AppError,
    DatabaseError,
    ResourceMissingError,
    SchemaVersionMismatchError,
)
from app.core.logging_config import configure_logging, log_display_environment

_logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_CANCELLED = 2


def _configure_high_dpi() -> None:
    """Must run before QApplication is constructed -- Qt reads the rounding
    policy once, at application creation.

    PassThrough keeps Windows' fractional scale factors (125%, 150%, 175%)
    exactly as the user set them. Qt 6 already defaults to this; it is set
    explicitly because the whole layout was audited against it, and a future
    Qt default flipping back to rounded factors would quietly change every
    size in the app.
    """
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def _create_application(argv: list[str]) -> QApplication:
    app = QApplication(argv)
    # Windows uses these for the taskbar grouping, and QStandardPaths uses
    # them to locate per-application directories.
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(ORG_NAME)

    icon = paths.icon_path()
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    else:
        # Only reachable from a source checkout before make_icon.py is run;
        # a packaged build without it fails --self-test.
        _logger.warning("No application icon at %s", icon)
    return app


def _apply_theme(app: QApplication) -> None:
    from app.ui.theme import MUTED, STYLESHEET, reset_scale_cache

    app.setStyleSheet(STYLESHEET)
    # theme.scale() caches its factor on first use; the QApplication font is
    # only knowable now, so drop anything measured during import.
    reset_scale_cache()

    # QSS has no selector for placeholder text, so without this every
    # QLineEdit/QTextEdit placeholder falls back to whatever the OS style
    # computes — invisible-to-low-contrast on some platforms/themes. Setting
    # it once here covers every placeholder in the app consistently.
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    app.setPalette(palette)


def _fatal(title: str, message: str, detail: str = "") -> int:
    _logger.critical("%s: %s", title, message)
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.exec()
    return EXIT_STARTUP_FAILED


def _run_setup_wizard(allow_cancel: bool) -> bool:
    from app.ui.setup_wizard import SetupWizard

    wizard = SetupWizard(allow_cancel=allow_cancel)
    return wizard.exec() == SetupWizard.DialogCode.Accepted


def _connect_and_check_schema() -> bool:
    """Returns True when the database is reachable and at the expected
    revision. Otherwise reports the specific problem and offers the actions
    that can actually fix it, looping until the user succeeds or quits.

    The three failure modes are deliberately kept apart. Reporting an
    unreachable server as "schema out of date" -- which is what the old code
    did, because the schema probe swallowed connection errors -- sends the
    user chasing a migration they cannot run for a network problem they can.
    """
    from app.database.schema_check import check_schema_version

    while True:
        try:
            check_schema_version()
            return True
        except SchemaVersionMismatchError as exc:
            title = "Database needs updating"
            message = (f"{exc}\n\nThis usually means the application was updated "
                       "but the database was not. Contact your administrator "
                       "before continuing — using it now risks showing "
                       "incomplete or incorrect data.")
            detail = ""
            offer_setup = True
        except ResourceMissingError as exc:
            # Nothing the user can retry their way out of — the migrations
            # or alembic.ini are missing from the installation itself.
            _fatal("Installation incomplete", str(exc))
            return False
        except DatabaseError as exc:
            title = "Cannot reach the database"
            message = str(exc)
            detail = exc.detail
            offer_setup = True
        except AppError as exc:
            title = "Cannot start"
            message = str(exc)
            detail = ""
            offer_setup = True

        _logger.error("Startup check failed: %s", message.splitlines()[0])
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
        retry = box.addButton("Try Again", QMessageBox.ButtonRole.AcceptRole)
        change = (box.addButton("Database Settings…", QMessageBox.ButtonRole.ActionRole)
                  if offer_setup else None)
        quit_button = box.addButton("Quit", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(retry)
        box.exec()

        clicked = box.clickedButton()
        if clicked is quit_button:
            return False
        if clicked is change and not _run_setup_wizard(allow_cancel=True):
            return False
        # "Try Again" falls through and re-runs the check.


class AppController:
    """Owns the Login <-> Main window transition so app.main stays a thin
    entrypoint. Keeps at most one window alive at a time.
    """

    def __init__(self, container):
        from app.ui.login_window import LoginWindow

        self._container = container
        self._login_window: LoginWindow | None = None
        self._main_window = None
        self.show_login()

    def show_login(self) -> None:
        # A previous MainWindow (if any) must be torn down, not just have
        # its reference dropped: its idle_timer keeps firing and its
        # activity-filter stays installed on the whole QApplication even
        # after this method returns, since nothing else calls close() on
        # it — Python refcounting alone doesn't trigger Qt's closeEvent.
        # Left alive, a stale timer can later end a *different*, currently
        # valid session out from under the next login, and its still-wired
        # session_ended signal would call show_login() a second time.
        from app.ui.login_window import LoginWindow

        if self._main_window is not None:
            self._main_window.session_ended.disconnect(self.show_login)
            self._main_window.close()
            self._main_window.deleteLater()
        self._main_window = None
        self._login_window = LoginWindow(self._container.auth_service())
        self._login_window.login_succeeded.connect(self._show_main)
        self._login_window.show()

    def _show_main(self, _session) -> None:
        from app.ui.main_window import MainWindow

        self._login_window = None
        self._main_window = MainWindow(self._container)
        self._main_window.session_ended.connect(self.show_login)
        self._main_window.show()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--version", action="version", version=version_string())
    parser.add_argument("--self-test", action="store_true",
                        help="Build every screen without a database, then exit. "
                             "Used to verify a packaged build.")
    parser.add_argument("--screenshot-dir", metavar="DIR",
                        help="With --self-test, save a PNG of every screen here.")
    return parser.parse_known_args(argv[1:])[0]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options = _parse_args(argv)

    paths.ensure_user_dirs()
    configure_logging()
    crash_handler.install()
    _logger.info("Starting %s", version_string())

    _configure_high_dpi()
    app = _create_application(argv)
    log_display_environment()

    try:
        _apply_theme(app)
    except ResourceMissingError as exc:
        return _fatal("Installation incomplete", str(exc))

    if options.self_test:
        from app.selftest import run_self_test

        return run_self_test(app, screenshot_dir=options.screenshot_dir)

    if settings_module.config_error is not None:
        # A config file exists but could not be used. Say so explicitly
        # rather than silently starting the wizard, which would look like
        # the saved settings had simply vanished.
        QMessageBox.warning(None, "Saved settings could not be read",
                            f"{settings_module.config_error}\n\n"
                            "Please enter your database details again.")

    if not settings.is_configured() and not _run_setup_wizard(allow_cancel=False):
        _logger.info("Setup cancelled before a database was configured")
        return EXIT_CANCELLED

    if not _connect_and_check_schema():
        return EXIT_STARTUP_FAILED

    from app.core.container import Container

    container = Container()
    controller = AppController(container)  # noqa: F841 - keeps windows alive
    _logger.info("Startup complete")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
