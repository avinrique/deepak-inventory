"""Last-resort handler for exceptions nobody caught.

Without this, an unhandled exception in a --windowed build prints a
traceback to a stderr that does not exist and the window simply disappears,
leaving the user with nothing to report and us with nothing to diagnose.
With it, the traceback always reaches the log file and the user gets a
plain-language dialog with a button that opens the log folder.

This is a safety net, not an error-handling strategy: anything a user can
actually act on should be an AppError caught and reported by the code that
knows the context (see app.core.exceptions). Reaching here means a bug.
"""
import logging
import sys
import threading
import traceback

from app.__version__ import APP_NAME, version_string
from app.core.logging_config import log_file

_logger = logging.getLogger(__name__)

_MESSAGE = (
    "{app} hit an unexpected problem and may not be able to continue.\n\n"
    "The technical details have been written to the log file. If this keeps "
    "happening, send that file to your administrator."
)


def _show_dialog(summary: str) -> None:
    """Best-effort: only if a QApplication exists and we are on its thread.

    Constructing a widget from a worker thread, or before QApplication is
    created, would itself crash -- which would replace a reportable error
    with an unreportable one.
    """
    try:
        from PySide6.QtCore import QThread, QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None or QThread.currentThread() is not app.thread():
            return

        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"{APP_NAME} - unexpected problem")
        box.setText(_MESSAGE.format(app=APP_NAME))
        box.setDetailedText(f"{APP_NAME} {version_string()}\n\n{summary}")
        open_log = box.addButton("Open Log Folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_log:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file().parent)))
    except Exception:  # noqa: BLE001 - the reporter must never raise
        _logger.exception("Could not display the crash dialog")


def _handle(exc_type, exc_value, exc_traceback) -> None:
    # Ctrl-C should stay a clean exit, not an "unexpected problem" dialog.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    _logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
    summary = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    _show_dialog(summary)


def install() -> None:
    """Covers the main thread and, since 3.8, QThreadPool workers too."""
    sys.excepthook = _handle
    threading.excepthook = lambda args: _handle(
        args.exc_type, args.exc_value, args.exc_traceback)
