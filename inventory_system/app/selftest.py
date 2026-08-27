"""`--self-test`: proves a *packaged* build actually works, without a database.

A PyInstaller bundle fails in ways the source tree never does — a stylesheet
left out of the spec, a hidden import the analyser could not see because the
module is only ever named in a string (openpyxl, psycopg), a missing
reportlab data file. None of that is visible to `pytest` run against the
source, so CI runs *this*, against the built .exe.

It exercises the paths that break: load both stylesheets, construct every
page and dialog, render an invoice PDF through ReportLab, and write a CSV
and an .xlsx through the export module. Services are stubs, so nothing here
touches a database — page constructors defer their loading to Workers, which
this never starts.

With --screenshot-dir it also saves a PNG per screen, which is how the DPI
matrix in CI produces something a human can actually look at.
"""
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication, QWidget

from app.__version__ import version_string
from app.security.session import SessionManager

_logger = logging.getLogger(__name__)


class _Check:
    """Collects results and echoes them everywhere they might be readable.

    A packaged build is windowed (no console), so sys.stdout is None and
    print() is silently discarded — which is why every line is also recorded
    for the report file. Without that, running --self-test against the .exe
    on Windows produces no output at all and there is nothing to diagnose.
    """

    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0
        self.lines: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)
        _logger.info("self-test: %s", line)
        print(line)          # a no-op when there is no stdout

    def run(self, name: str, fn) -> object:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - collecting, not propagating
            _logger.exception("self-test FAILED: %s", name)
            self.failures.append(f"{name}: {exc!r}")
            self.say(f"  FAIL  {name}: {exc!r}")
            return None
        self.passed += 1
        self.say(f"  ok    {name}")
        return result


def _stub_container() -> MagicMock:
    """A Container whose services are stubs but whose SessionManager is real
    — the sidebar and several pages compute visibility from live permission
    checks, so a mock session would not exercise that code."""
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(),
                   role_id=uuid.uuid4(), permissions=frozenset(), is_superuser=True,
                   must_change_password=False, now=datetime.now(timezone.utc))
    container = MagicMock()
    container.sessions = sessions
    return container


def _check_resources(check: _Check) -> None:
    from app.core.paths import icon_path

    def load_theme():
        from app.ui.theme import STYLESHEET
        assert len(STYLESHEET) > 1000, "theme.qss looks truncated"
        return STYLESHEET

    def load_order_form():
        from app.ui.widgets.order_form_style import ORDER_FORM_STYLESHEET
        assert len(ORDER_FORM_STYLESHEET) > 500, "order_form.qss looks truncated"
        return ORDER_FORM_STYLESHEET

    def alembic_scripts():
        from app.database.schema_check import _expected_head_revision
        return _expected_head_revision()

    check.run("resource: theme.qss", load_theme)
    check.run("resource: order_form.qss", load_order_form)
    check.run("resource: alembic.ini + migrations", alembic_scripts)
    def application_icon():
        path = icon_path()
        # A missing icon is a genuine packaging defect (the taskbar falls
        # back to a generic Qt icon), so it fails rather than warns.
        assert path.is_file(), f"app.ico is not in the bundle at {path}"
        return path

    check.run("resource: application icon", application_icon)


def _check_pages(check: _Check, screenshot_dir: Path | None) -> None:
    from app.ui.main_window import MODULES, _build_page

    container = _stub_container()
    for module in MODULES:
        widget = check.run(f"page: {module.key}",
                           lambda m=module: _build_page(m.key, container))
        if widget is not None and screenshot_dir is not None:
            _screenshot(widget, screenshot_dir / f"page-{module.key}.png")


def _check_windows(check: _Check, screenshot_dir: Path | None) -> None:
    def login_window():
        from app.ui.login_window import LoginWindow
        return LoginWindow(MagicMock())

    def setup_wizard():
        from app.ui.setup_wizard import SetupWizard
        return SetupWizard()

    for name, factory in (("login", login_window), ("setup-wizard", setup_wizard)):
        widget = check.run(f"window: {name}", factory)
        if widget is not None and screenshot_dir is not None:
            _screenshot(widget, screenshot_dir / f"window-{name}.png")


def _check_reporting(check: _Check) -> None:
    """The three third-party paths PyInstaller most often gets wrong:
    reportlab's bundled data files, openpyxl (named only as a string), and
    Qt's PDF/print modules."""
    from app.reporting.export import export_csv, export_excel
    from app.schemas.reporting import ReportResult

    result = ReportResult(
        title="Self Test", columns=["Item", "Qty", "Amount"],
        rows=[{"Item": "Widget", "Qty": 2, "Amount": Decimal("19.99")},
              {"Item": "=cmd|calc", "Qty": 1, "Amount": Decimal("0.01")}],
        generated_at=datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        check.run("export: CSV", lambda: export_csv(result, str(temp_path / "r.csv")))
        check.run("export: Excel (openpyxl)",
                  lambda: export_excel(result, str(temp_path / "r.xlsx")))
        check.run("export: PDF (Qt)", lambda: _export_pdf(result, temp_path / "r.pdf"))
        check.run("report: invoice PDF (reportlab)",
                  lambda: _render_invoice(temp_path / "invoice.pdf"))


def _export_pdf(result, path: Path):
    from app.reporting.export import export_pdf
    export_pdf(result, str(path))
    assert path.stat().st_size > 0, "empty PDF"
    return path


def _render_invoice(path: Path):
    """Imports reportlab and actually lays a document out — importing alone
    would not catch a missing reportlab/fonts data directory."""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from reportlab.lib.styles import getSampleStyleSheet

    document = SimpleDocTemplate(str(path), pagesize=A4)
    document.build([Paragraph("Self test", getSampleStyleSheet()["Normal"])])
    assert path.stat().st_size > 0, "empty PDF"
    return path


def _check_drivers(check: _Check) -> None:
    """These are only ever named inside strings — "postgresql+psycopg://" in
    a URL, engine="openpyxl" in a call — so PyInstaller's analyser cannot
    see them and will omit them unless the spec lists them explicitly."""
    check.run("driver: psycopg", lambda: __import__("psycopg").__name__)
    check.run("driver: openpyxl", lambda: __import__("openpyxl").__name__)
    check.run("driver: argon2 (password hashing)",
              lambda: __import__("app.security.passwords", fromlist=["hash_password"])
              .hash_password("self-test-password")[:7])


def _screenshot(widget: QWidget, path: Path) -> None:
    try:
        widget.resize(widget.sizeHint())
        widget.grab().save(str(path))
    except Exception:  # noqa: BLE001 - screenshots are diagnostics, not a gate
        _logger.debug("Could not capture %s", path, exc_info=True)


def run_self_test(app: QApplication, screenshot_dir: str | None = None,
                  report_path: str | None = None) -> int:
    """Returns 0 when everything a packaged build needs is present and works.

    ``report_path`` receives the same lines that would go to stdout. CI uses
    it because the packaged executable is windowed: it has no console to
    print to, so a report file is the only way its result can be read.
    """
    directory = Path(screenshot_dir) if screenshot_dir else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)

    check = _Check()
    check.say(f"Self-test: {version_string()}")
    screen = app.primaryScreen()
    if screen is not None:
        check.say(f"Screen {screen.geometry().width()}x{screen.geometry().height()} "
                  f"@ DPR {screen.devicePixelRatio():.2f}, "
                  f"scale factor env {os.environ.get('QT_SCALE_FACTOR', 'unset')}")

    _check_resources(check)
    _check_drivers(check)
    _check_reporting(check)
    _check_pages(check, directory)
    _check_windows(check, directory)

    check.say(f"{check.passed} passed, {len(check.failures)} failed")
    for failure in check.failures:
        check.say(f"  FAILED: {failure}")

    if report_path:
        try:
            Path(report_path).write_text("\n".join(check.lines) + "\n", encoding="utf-8")
        except OSError:
            _logger.exception("Could not write the self-test report to %s", report_path)
    return 1 if check.failures else 0
