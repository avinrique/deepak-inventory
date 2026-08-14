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
import sys

from PySide6.QtWidgets import QApplication

from app.core.container import Container
from app.core.logging_config import configure_logging
from app.ui.login_window import LoginWindow
from app.ui.main_window import MainWindow
from app.ui.theme import STYLESHEET


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
    container = Container()
    controller = AppController(container)  # noqa: F841 - keeps windows alive via references
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
