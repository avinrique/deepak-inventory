"""Pure backup domain — status enum and filename convention. Actual
process execution (pg_dump/pg_restore) and file I/O live in
app.backup.postgres_backup (subprocess-only, no Excel equivalent — there
is nothing to "back up" for the excel backend beyond copying files, which
is out of scope here).
"""
from datetime import datetime, timedelta
from enum import Enum


class BackupStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BackupFrequency(str, Enum):
    """A *preference*, not an OS-level cron entry — this is a desktop app
    with no background service, so "automatic" backups only happen while
    the app is running (checked once at startup — see MainWindow — and
    whenever is_backup_due is consulted). MANUAL means the setting exists
    purely so BackupService.run_scheduled_backup_if_due never fires on its
    own; the user always triggers backups from the Settings page.
    """
    MANUAL = "MANUAL"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


_FREQUENCY_INTERVAL: dict[BackupFrequency, timedelta] = {
    BackupFrequency.DAILY: timedelta(days=1),
    BackupFrequency.WEEKLY: timedelta(days=7),
}


def is_backup_due(frequency: BackupFrequency, last_completed_backup_at: datetime | None,
                  now: datetime) -> bool:
    """Pure decision: would an automatic backup run right now, given the
    configured frequency and when the last one completed? MANUAL never
    triggers. No prior backup always triggers (nothing to protect yet).
    """
    if frequency == BackupFrequency.MANUAL:
        return False
    if last_completed_backup_at is None:
        return True
    return now - last_completed_backup_at >= _FREQUENCY_INTERVAL[frequency]


def format_backup_filename(database_name: str, when: datetime) -> str:
    """e.g. "inventory_20260813_153045.dump" — sortable and collision-
    resistant down to the second. app.backup.postgres_backup appends a
    numeric suffix on the rare same-second collision rather than
    overwriting an existing file.
    """
    return f"{database_name}_{when.strftime('%Y%m%d_%H%M%S')}.dump"
