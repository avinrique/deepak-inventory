"""app.core.logging_config / app.core.crash_handler.

Both exist because of how a --windowed PyInstaller build fails: there is no
console, sys.stderr is None, and an unhandled exception therefore produces
no output anywhere and no window — the app just vanishes. These tests pin
the two behaviours that prevent that.
"""
import logging
import sys

import pytest

from app.core import crash_handler, logging_config


@pytest.fixture()
def fresh_logging(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config.settings, "log_dir", str(tmp_path))
    monkeypatch.setattr(logging_config, "_configured", False)
    root = logging.getLogger()
    previous = list(root.handlers), root.level
    try:
        yield tmp_path
    finally:
        root.handlers, root.level = previous


def test_log_file_is_written_under_the_configured_directory(fresh_logging):
    logging_config.configure_logging()
    logging.getLogger("test").info("hello")

    contents = (fresh_logging / logging_config.LOG_FILENAME).read_text(encoding="utf-8")
    assert "hello" in contents


def test_the_header_records_what_is_needed_to_reproduce_a_bug_report(fresh_logging):
    logging_config.configure_logging()

    contents = (fresh_logging / logging_config.LOG_FILENAME).read_text(encoding="utf-8")
    assert "Inventory Management System" in contents
    assert "Python" in contents
    assert "Frozen:" in contents


def test_no_stream_handler_is_attached_when_there_is_no_stderr(fresh_logging, monkeypatch):
    """The --windowed case. logging.StreamHandler() defaults to sys.stderr,
    so attaching one here would raise on every record the app ever logs."""
    monkeypatch.setattr(sys, "stderr", None)

    logging_config.configure_logging()

    assert not any(type(h) is logging.StreamHandler
                   for h in logging.getLogger().handlers)


def test_an_unwritable_log_directory_does_not_prevent_startup(monkeypatch, tmp_path):
    """Losing the log is bad; refusing to start because of it is worse.
    This used to be a PermissionError on the first line of main()."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setattr(logging_config.settings, "log_dir", str(blocked / "logs"))
    monkeypatch.setattr(logging_config, "_configured", False)
    root = logging.getLogger()
    previous = list(root.handlers), root.level
    try:
        logging_config.configure_logging()  # must not raise
        logging.getLogger("test").info("still alive")
    finally:
        root.handlers, root.level = previous


def test_configure_logging_is_idempotent(fresh_logging):
    logging_config.configure_logging()
    count = len(logging.getLogger().handlers)
    logging_config.configure_logging()

    assert len(logging.getLogger().handlers) == count


def test_the_rotating_handler_is_bounded(fresh_logging):
    """A till left running for a year must not fill the disk."""
    logging_config.configure_logging()

    handlers = [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers and handlers[0].maxBytes > 0 and handlers[0].backupCount > 0


def test_an_unhandled_exception_reaches_the_log_file(fresh_logging):
    """Asserted against the file rather than caplog because the file is what
    a user actually sends us — and because configure_logging() takes over the
    root logger (force=True), which detaches caplog's handler anyway."""
    logging_config.configure_logging()

    crash_handler._handle(ValueError, ValueError("boom"), None)

    contents = (fresh_logging / logging_config.LOG_FILENAME).read_text(encoding="utf-8")
    assert "Unhandled exception" in contents
    assert "boom" in contents


def test_the_crash_dialog_is_skipped_when_there_is_no_qapplication(fresh_logging):
    """Constructing a QMessageBox before QApplication exists would itself
    crash, replacing a reportable error with an unreportable one."""
    logging_config.configure_logging()

    crash_handler._handle(ValueError, ValueError("boom"), None)  # must not raise


def test_keyboard_interrupt_is_not_reported_as_a_crash(monkeypatch):
    """Ctrl-C is a clean exit, not an "unexpected problem" dialog."""
    shown = []
    monkeypatch.setattr(crash_handler, "_show_dialog", lambda summary: shown.append(summary))
    monkeypatch.setattr(sys, "__excepthook__", lambda *args: None)

    crash_handler._handle(KeyboardInterrupt, KeyboardInterrupt(), None)

    assert shown == []


def test_install_sets_hooks_for_the_main_thread_and_workers():
    previous_sys, previous_thread = sys.excepthook, __import__("threading").excepthook
    try:
        crash_handler.install()
        assert sys.excepthook is crash_handler._handle
        assert __import__("threading").excepthook is not previous_thread
    finally:
        sys.excepthook = previous_sys
        __import__("threading").excepthook = previous_thread
