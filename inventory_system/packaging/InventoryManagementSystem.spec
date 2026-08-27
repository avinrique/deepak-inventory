# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for the Windows application.

Run from the project directory (inventory_system/), not from packaging/:

    pyinstaller packaging/InventoryManagementSystem.spec --noconfirm

**onedir, not onefile.** A onefile build unpacks its whole ~200 MB archive
into a temp directory on every launch, which is slow, is a common
false-positive trigger for antivirus, and would defeat Alembic — which reads
the migration scripts off disk at runtime. The installer lays down a
directory anyway, so onefile buys nothing here.

Three categories of thing PyInstaller cannot work out for itself, and each
one is a real runtime failure if it is left out:

1. **Data files.** Anything read from disk at runtime rather than imported.
   app.core.paths resolves all of these against sys._MEIPASS when frozen, so
   the layout below has to match what that module expects.
2. **Hidden imports.** Modules named only inside strings. `psycopg` appears
   solely in the "postgresql+psycopg://" URL and `openpyxl` solely as
   engine="openpyxl" — the analyser sees neither, and without them the app
   cannot reach a database or write a spreadsheet.
3. **Deliberate exclusions.** Large packages nothing imports. Note what is
   *not* excluded: PySide6.QtCharts is used by the dashboard, and
   QtPdf/QtPdfWidgets/QtPrintSupport by invoice preview, PDF export and
   printing.

`--self-test` on the built executable checks every one of these; see
app/selftest.py.
"""
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# The real target is Windows. Building on macOS/Linux still exercises the
# analysis — which is where spec mistakes actually live (a data file with the
# wrong path, a hidden import that does not resolve) — so it is worth being
# able to do as a dry run, and these two options are Windows-only.
IS_WINDOWS = sys.platform == "win32"

# SPECPATH is the directory containing this .spec file (packaging/).
PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - PyInstaller global

APP_NAME = "InventoryManagementSystem"


def _version() -> str:
    """Read from app/__version__.py without importing the app (importing it
    here would pull PySide6 into the build process for no reason).

    Matched with a regex rather than str.startswith: that module's docstring
    also contains a line beginning "VERSION is the only value a release
    bumps", which a prefix check happily mistakes for the assignment.
    """
    text = (PROJECT_ROOT / "app" / "__version__.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if match is None:
        raise SystemExit("Could not find a VERSION assignment in app/__version__.py")
    return match.group(1)


VERSION = _version()

datas = [
    # Read at *import* time by app.ui.theme / app.ui.widgets.order_form_style.
    # Missing, the app dies before QApplication exists and shows nothing.
    (str(PROJECT_ROOT / "app" / "ui" / "styles"), "app/ui/styles"),

    # Alembic reads both of these off the filesystem at startup
    # (app.database.schema_check) and when the setup wizard initialises a
    # database. The revision files must ship as .py *source* — Alembic execs
    # them itself, so they are never imported and never compiled in.
    (str(PROJECT_ROOT / "alembic.ini"), "."),
    (str(PROJECT_ROOT / "migrations"), "migrations"),

    # Window/taskbar icon, loaded via app.core.paths.icon_path().
    (str(PROJECT_ROOT / "packaging" / "app.ico"), "."),
]

# pg_dump.exe / pg_restore.exe, if the release process has staged them. The
# build works without them; Backup and Restore then report that the tools
# are missing instead of failing obscurely. See packaging/fetch_pgtools.py.
_pgtools = PROJECT_ROOT / "packaging" / "pgtools"
if _pgtools.is_dir() and any(_pgtools.iterdir()):
    datas.append((str(_pgtools), "pgtools"))

# ReportLab ships font metrics and other data alongside its code; the invoice
# PDF layout fails at render time without them.
datas += collect_data_files("reportlab")

hiddenimports = [
    # Named only inside the database URL string.
    *collect_submodules("psycopg"),
    "psycopg_binary",
    # Named only as engine="openpyxl" in app/reporting/export.py.
    "openpyxl",
    "openpyxl.cell._writer",
    # argon2-cffi's compiled backend, reached through a cffi indirection.
    "argon2",
    "_argon2_cffi_bindings",
    # Alembic loads these by name from alembic.ini / env.py.
    "alembic.runtime.migration",
    "alembic.autogenerate",
]

excludes = [
    # Removed from app/reporting/export.py — roughly 90 MB between them.
    "pandas",
    "numpy",
    # Test-only.
    "pytest",
    "pypdf",
    "_pytest",
    # The legacy Tkinter app at the repo root is a separate program and is
    # not part of this build.
    "tkinter",
    # Qt modules this application does not use. QtCharts is deliberately NOT
    # here: app/ui/pages/dashboard_page.py imports it.
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
]

analysis = Analysis(  # noqa: F821 - PyInstaller global
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821 - PyInstaller global

exe = EXE(  # noqa: F821 - PyInstaller global
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is a well-known antivirus trigger.
    console=False,      # A GUI app: no console window on launch.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "packaging" / "app.ico") if IS_WINDOWS else None,
    version=str(PROJECT_ROOT / "packaging" / "version_info.txt") if IS_WINDOWS else None,
)

collect = COLLECT(  # noqa: F821 - PyInstaller global
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
