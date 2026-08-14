"""Structured logging setup.

The legacy Tkinter app only ever surfaces an error once via messagebox and
then loses it — this gives the new app a persistent log file to diagnose a
user's bug report after the fact.
"""
import logging
from pathlib import Path

from app.config.settings import settings


def configure_logging() -> None:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "inventory_system.log"),
            logging.StreamHandler(),
        ],
    )
