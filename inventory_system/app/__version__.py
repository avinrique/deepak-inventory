"""Single source of truth for the application's identity and version.

Everything that needs to name or version the app reads from here: the
Windows VERSIONINFO resource embedded in the .exe, the Inno Setup
installer, QApplication's application/organization metadata, the
"Inventory Management System x.y.z" line at the top of every log file, and
Settings -> About.

VERSION is the only value a release bumps. Keep it a plain three-part
"major.minor.patch" string: packaging/make_version_info.py parses it into
the four-integer tuple Windows requires, and installer.iss embeds it as
VersionInfoVersion, neither of which accepts a suffix like "1.0.0-rc1".

BUILD is stamped by CI (a short git sha) and stays "dev" for local builds,
so a bug report's log header says exactly which commit produced the binary
the user is running. It deliberately does NOT participate in VERSION.
"""
import os

VERSION = "1.0.0"
BUILD = os.environ.get("INVENTORY_BUILD", "dev")

# APP_NAME is user-facing (window titles, installer, Start Menu). APP_SLUG
# is the filesystem/registry-safe form, and is what names the per-user data
# directories in app/core/paths.py — changing it orphans every existing
# install's config, so it is effectively frozen once shipped.
APP_NAME = "Inventory Management System"
APP_SLUG = "InventoryManagementSystem"
ORG_NAME = "Inventory Management System"

__version__ = VERSION


def version_string() -> str:
    """e.g. "1.0.0 (build a1b2c3d)" — for logs and the About box."""
    return f"{VERSION} (build {BUILD})"
