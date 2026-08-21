"""Entrypoint for the PySide6 production app.

The legacy Tkinter app (../inventory_app.py, one directory up) is
unaffected and still what real users run — this is the in-progress
replacement. Run with:

    cd inventory_system
    python -m app.main

Flow: LoginWindow -> MainWindow -> (logout or idle timeout) -> a fresh
LoginWindow, without quitting the process — this is a shared desktop
terminal, so returning to the login screen (not exiting) is the point.
"""
import logging
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.container import Container
from app.core.exceptions import SchemaVersionMismatchError
from app.core.logging_config import configure_logging
from app.database.schema_check import check_schema_version
from app.ui.login_window import LoginWindow
from app.ui.main_window import MainWindow
from app.ui.theme import MUTED, STYLESHEET

_logger = logging.getLogger(__name__)


class AppController:
    """Owns the Login <-> Main window transition so app.main stays a thin
    entrypoint. Keeps at most one window alive at a time.
    """

    def __init__(self, container: Container):
        self._container = container
        self._login_window: LoginWindow | None = None
        self._main_window: MainWindow | None = None
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
        if self._main_window is not None:
            self._main_window.session_ended.disconnect(self.show_login)
            self._main_window.close()
            self._main_window.deleteLater()
        self._main_window = None
        self._login_window = LoginWindow(self._container.auth_service())
        self._login_window.login_succeeded.connect(self._show_main)
        self._login_window.show()

    def _show_main(self, _session) -> None:
        self._login_window = None
        self._main_window = MainWindow(self._container)
        self._main_window.session_ended.connect(self.show_login)
        self._main_window.show()


def main() -> int:
    configure_logging()
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    # QSS has no selector for placeholder text, so without this every
    # QLineEdit/QTextEdit placeholder falls back to whatever the OS style
    # computes — invisible-to-low-contrast on some platforms/themes. Setting
    # it once here covers every placeholder in the app consistently.
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    app.setPalette(palette)

    # Fails fast and clearly on schema drift, before any window is shown —
    # see SchemaVersionMismatchError's docstring for the production
    # incident this prevents a repeat of. Deliberately not wrapped around
    # Container() itself: a database that's merely unreachable (network
    # down, wrong credentials) should still surface its own real error
    # rather than being misreported as a schema mismatch.
    try:
        check_schema_version()
    except SchemaVersionMismatchError as exc:
        _logger.critical("Refusing to start: %s", exc)
        QMessageBox.critical(
            None, "Database schema out of date",
            f"{exc}\n\nContact your administrator before using this application — "
            "starting now would risk showing incomplete or incorrect data.")
        return 1

    container = Container()
    controller = AppController(container)  # noqa: F841 - keeps windows alive via references
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
