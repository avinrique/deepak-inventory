"""Central design tokens (colors/spacing/fonts) for the whole app, plus the
loader that turns app/ui/styles/*.qss template files into ready-to-apply
stylesheets. Every widget under app/ui pulls a color from the tokens below
(directly in Python, or via an @TOKEN@ placeholder in a .qss file) rather
than hardcoding a fresh hex value, so the app reads as one consistent
system, not a pile of one-off styles.
"""
import platform

from app.core.exceptions import ResourceMissingError
from app.core.paths import resource_path

MAC = platform.system() == "Darwin"

# Every "icon" in this UI is a literal emoji or symbol character in a Python
# string (see app/ui/main_window.py's MODULES). Naming only the text face
# leaves the fallback for those glyphs to Qt, which on Windows can land on a
# font with different metrics and shift them off their baseline. Listing the
# emoji and symbol faces explicitly, after the text face, keeps ordinary text
# in Segoe UI and sends only the glyphs it lacks to the fonts built for them.
FAMILY = ('"Helvetica Neue", "Apple Color Emoji"' if MAC
          else '"Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol"')

# Resolved through app.core.paths so it works both from source and from
# inside a PyInstaller bundle, where these files are unpacked next to the
# code rather than sitting in the source tree.
STYLES_DIR = resource_path("app", "ui", "styles")

# Palette — dark sidebar / light content, the same system proven out on the
# legacy Tkinter app's UI pass, carried over for visual continuity.
SIDEBAR_BG = "#111827"
SIDEBAR_FG = "#cbd5e1"
SIDEBAR_HOVER = "#1f2937"
SIDEBAR_MUTED = "#94a3b8"
NAV_ACTIVE = "#2563eb"
CONTENT_BG = "#eef1f5"
CARD_BG = "#ffffff"
BORDER = "#e2e6ec"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_TINT = "#eef2ff"
ACCENT_TINT_HOVER = "#e0e7ff"
GREEN = "#059669"
GREEN_DARK = "#047857"
GREEN_TINT = "#ecfdf5"
RED = "#dc2626"
RED_DARK = "#b91c1c"
RED_TINT = "#fef2f2"
AMBER = "#d97706"
AMBER_DARK = "#b45309"
AMBER_TINT = "#fffbeb"
TEXT = "#111827"
MUTED = "#6b7280"
SURFACE_MUTED = "#f8fafc"  # table headers, subtle hover fills
DISABLED_BG = "#f1f5f9"
DISABLED_BORDER = "#cbd5e1"
DISABLED_FG = "#94a3b8"

SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 16
SPACING_LG = 24
SPACING_XL = 32

RADIUS = 8

# Every name a .qss file may reference as @NAME@. Kept as an explicit map
# (rather than scraping globals()) so it's obvious what's part of the public
# token contract between theme.py and app/ui/styles/*.qss.
_TOKENS = {
    "FAMILY": FAMILY,
    "SIDEBAR_BG": SIDEBAR_BG,
    "SIDEBAR_FG": SIDEBAR_FG,
    "SIDEBAR_HOVER": SIDEBAR_HOVER,
    "SIDEBAR_MUTED": SIDEBAR_MUTED,
    "NAV_ACTIVE": NAV_ACTIVE,
    "CONTENT_BG": CONTENT_BG,
    "CARD_BG": CARD_BG,
    "BORDER": BORDER,
    "ACCENT": ACCENT,
    "ACCENT_DARK": ACCENT_DARK,
    "ACCENT_TINT": ACCENT_TINT,
    "ACCENT_TINT_HOVER": ACCENT_TINT_HOVER,
    "GREEN": GREEN,
    "GREEN_DARK": GREEN_DARK,
    "GREEN_TINT": GREEN_TINT,
    "RED": RED,
    "RED_DARK": RED_DARK,
    "RED_TINT": RED_TINT,
    "AMBER": AMBER,
    "AMBER_DARK": AMBER_DARK,
    "AMBER_TINT": AMBER_TINT,
    "TEXT": TEXT,
    "MUTED": MUTED,
    "SURFACE_MUTED": SURFACE_MUTED,
    "DISABLED_BG": DISABLED_BG,
    "DISABLED_BORDER": DISABLED_BORDER,
    "DISABLED_FG": DISABLED_FG,
    "SPACING_XS": SPACING_XS,
    "SPACING_SM": SPACING_SM,
    "SPACING_MD": SPACING_MD,
    "SPACING_LG": SPACING_LG,
    "SPACING_XL": SPACING_XL,
    "RADIUS": RADIUS,
}


def load_qss(filename: str) -> str:
    """Read a .qss file from app/ui/styles/ and substitute every @TOKEN@
    placeholder with its value from the palette above. A plain string
    replace (not str.format) so the .qss files can use real QSS brace
    syntax without needing to escape every '{' / '}'.

    Raises ResourceMissingError rather than FileNotFoundError, because this
    runs at *import* time (see STYLESHEET below): if the .qss files were left
    out of the PyInstaller bundle the app would otherwise die with a bare
    traceback before QApplication exists, showing the user nothing at all.
    app.main catches this and reports it as the packaging fault it is.
    """
    path = STYLES_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResourceMissingError(path) from exc
    for key, value in _TOKENS.items():
        text = text.replace(f"@{key}@", str(value))
    return text


# -- density / DPI ------------------------------------------------------- #
# Every hardcoded pixel size in this UI was written against a 13px base
# font. scale() re-expresses those numbers in terms of the font the user's
# machine actually renders, so a widget sized for one row of text still
# fits one row of text at 150% Windows scaling or with "make text bigger"
# turned on. Qt already scales QSS px by the display factor; what it cannot
# know is that "40" in Python meant "tall enough for a line of text".
_DESIGN_BASE_FONT_PX = 13
_scale_factor: float | None = None


def scale(pixels: int) -> int:
    """A design-time pixel size, adjusted for the current UI font.

    Cached on first use rather than computed at import: QApplication (and
    therefore any real font metrics) does not exist when this module is
    first imported. Falls back to the identity when there is no application
    yet, which keeps it usable from constructors that run very early.
    """
    global _scale_factor
    if _scale_factor is None:
        _scale_factor = _measure_scale_factor()
    return max(1, round(pixels * _scale_factor))


def _measure_scale_factor() -> float:
    try:
        from PySide6.QtGui import QFontMetricsF
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return 1.0
        # Cap the range: a very large accessibility font should loosen the
        # layout, not multiply every margin until nothing fits on screen.
        measured = QFontMetricsF(app.font()).height() / (_DESIGN_BASE_FONT_PX * 1.25)
        return min(2.0, max(0.85, measured))
    except Exception:  # noqa: BLE001 - never let styling break startup
        return 1.0


def reset_scale_cache() -> None:
    """Forces the next scale() call to re-measure. For tests, and for a
    font change applied while the app is running."""
    global _scale_factor
    _scale_factor = None


STYLESHEET = load_qss("theme.qss")
