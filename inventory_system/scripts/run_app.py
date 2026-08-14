#!/usr/bin/env python3
"""Convenience launcher, equivalent to ``python -m app.main`` — lets the app
be started as ``python scripts/run_app.py`` from any working directory
without worrying about package-relative execution.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
