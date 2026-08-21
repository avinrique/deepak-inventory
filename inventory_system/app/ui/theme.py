"""Central design tokens (colors/spacing/fonts) for the whole app, plus the
loader that turns app/ui/styles/*.qss template files into ready-to-apply
stylesheets. Every widget under app/ui pulls a color from the tokens below
(directly in Python, or via an @TOKEN@ placeholder in a .qss file) rather
than hardcoding a fresh hex value, so the app reads as one consistent
system, not a pile of one-off styles.
"""
import platform
from pathlib import Path

MAC = platform.system() == "Darwin"
FAMILY = "Helvetica Neue" if MAC else "Segoe UI"

STYLES_DIR = Path(__file__).resolve().parent / "styles"

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
    """
    text = (STYLES_DIR / filename).read_text(encoding="utf-8")
    for key, value in _TOKENS.items():
        text = text.replace(f"@{key}@", str(value))
    return text


STYLESHEET = load_qss("theme.qss")
