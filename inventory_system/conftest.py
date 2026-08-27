"""Marks the project directory as pytest's rootdir (see pyproject.toml's
`pythonpath`) and picks a Qt platform plugin the test run can actually use.

The UI tests each construct a real QApplication and skip themselves if that
fails. On a machine with no display — a CI runner, a container, an SSH
session — that means roughly 180 tests quietly skip and the run still passes
green, which is worse than failing: the packaging work these tests guard is
exactly the work that breaks without a developer noticing.

Selecting Qt's "offscreen" platform makes them run for real instead. Set
here, before PySide6 is imported anywhere, because Qt reads QT_QPA_PLATFORM
once when the plugin is loaded. An explicit QT_QPA_PLATFORM in the
environment always wins, so a developer can still force "cocoa"/"xcb" to
watch the windows appear.
"""
import os
import sys


def _has_display() -> bool:
    if sys.platform == "win32":
        return True          # a Windows session always has a window station
    if sys.platform == "darwin":
        # A GUI session has one; a headless CI runner or an SSH login does not.
        return os.environ.get("TERM_PROGRAM") is not None or "SSH_TTY" not in os.environ
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


if "QT_QPA_PLATFORM" not in os.environ and not _has_display():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
