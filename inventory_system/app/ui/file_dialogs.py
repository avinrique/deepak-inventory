"""Save/open dialogs that start somewhere the user can actually write.

Every QFileDialog call in this app used to pass a bare filename ("sales.csv")
as its starting path. Qt resolves that against the process working
directory, which for an application launched from a Start Menu shortcut is
arbitrary — and when the app is installed under C:\\Program Files, not
writable. The user then either could not save, or saved into a directory
they would never find again.

Documents is the right default for an exported report, and Qt already knows
where it is on every platform.
"""
import logging
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QWidget

_logger = logging.getLogger(__name__)


def default_export_dir() -> Path:
    """The user's Documents folder, falling back to their home directory on
    a system that does not report one."""
    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation)
    return Path(location) if location else Path.home()


def ask_save_path(parent: QWidget | None, title: str, filename: str,
                  file_filter: str) -> str | None:
    """Returns the chosen path, or None if the user cancelled.

    Cancelling is a normal outcome, not an error — callers just stop.
    """
    suggested = str(default_export_dir() / filename)
    path, _selected = QFileDialog.getSaveFileName(parent, title, suggested, file_filter)
    return path or None


def ask_open_path(parent: QWidget | None, title: str, file_filter: str) -> str | None:
    path, _selected = QFileDialog.getOpenFileName(parent, title,
                                                  str(default_export_dir()), file_filter)
    return path or None
