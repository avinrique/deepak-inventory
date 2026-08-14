"""Excel-backed repository implementations: thin adapters over the legacy
Tkinter app's storage.py (one directory up, outside this project) rather
than a duplicate copy of its Excel I/O — it stays the single source of
truth for the .xlsx format until Phase 2 replaces this package with sql/.
No business logic lives here — only translation between the schema types
Services use and storage.py's dict/list shapes.

This is the one place in inventory_system/ that reaches outside the project
directory; the path shim below is what makes `import storage` resolve to
that sibling file, and it is meant to be deleted along with this package
once the sql/ backend is the default (see docs/architecture.md).
"""
import sys
from pathlib import Path

_LEGACY_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_LEGACY_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_LEGACY_PROJECT_ROOT))
