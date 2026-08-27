"""Production logging setup.

The legacy Tkinter app surfaced an error once via messagebox and then lost
it; this gives the new app a persistent log to diagnose a user's bug report
after the fact. Three things that only matter once it is a packaged Windows
app, and each of which used to be wrong here:

* The log directory is the user's own AppData, not a relative "logs" next
  to the CWD. Installed under C:\\Program Files, creating that directory
  raised PermissionError on the very first line of main() -- before any
  window existed, so a --windowed build simply vanished.
* The file rotates. A till that runs for a year should not accumulate an
  unbounded log.
* A stream handler is only attached when there is actually a stream. In a
  --windowed PyInstaller build sys.stderr is None, and logging.StreamHandler()
  then fails on every single record it tries to emit.
"""
import logging
import logging.handlers
import platform
import sys
from pathlib import Path

from app.__version__ import APP_NAME, version_string
from app.config.settings import settings

LOG_FILENAME = "inventory_system.log"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 5

_configured = False


def log_file() -> Path:
    return Path(settings.log_dir) / LOG_FILENAME


def configure_logging() -> None:
    """Idempotent -- --self-test and the setup wizard can both call it."""
    global _configured
    if _configured:
        return

    handlers: list[logging.Handler] = []
    directory = Path(settings.log_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            directory / LOG_FILENAME, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8"))
        file_error = None
    except OSError as exc:
        # Never fatal: losing the log is bad, refusing to start because of
        # it is worse. The console handler below (when there is a console)
        # still reports it, and app.main surfaces nothing to the user --
        # they cannot act on it anyway.
        file_error = exc

    # sys.stderr is None in a --windowed build; it is a real stream when run
    # from a terminal in development or with --self-test in CI.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    if file_error is not None and sys.stderr is not None:
        print(f"Warning: cannot write logs to {directory}: {file_error}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    _configured = True
    _log_environment()


def _log_environment() -> None:
    """First lines of every log file: enough to reproduce a bug report
    without another round trip asking the user what they are running."""
    logger = logging.getLogger(__name__)
    logger.info("=" * 62)
    logger.info("%s %s", APP_NAME, version_string())
    logger.info("Python %s (%s)", platform.python_version(), platform.architecture()[0])
    logger.info("OS %s %s", platform.system(), platform.release())
    logger.info("Frozen: %s", getattr(sys, "frozen", False))
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        logger.info("PySide6 %s / Qt %s", pyside_version, qVersion())
    except Exception:  # noqa: BLE001 - version reporting must never break startup
        logger.info("PySide6 version unavailable")


def log_display_environment() -> None:
    """Screen geometry and DPI scaling, logged separately because it needs a
    live QApplication. Called from app.main once the app object exists --
    this is the first thing to look at in any "text is clipped" report.
    """
    logger = logging.getLogger(__name__)
    try:
        from PySide6.QtGui import QGuiApplication
        for screen in QGuiApplication.screens():
            geometry = screen.geometry()
            available = screen.availableGeometry()
            logger.info(
                "Screen %r: %dx%d (available %dx%d), DPR %.2f, logical DPI %.0f",
                screen.name(), geometry.width(), geometry.height(),
                available.width(), available.height(),
                screen.devicePixelRatio(), screen.logicalDotsPerInch())
    except Exception:  # noqa: BLE001 - diagnostics only
        logger.debug("Could not query screen information", exc_info=True)
