from datetime import datetime, timedelta

from app.domain.backup import BackupFrequency, format_backup_filename, is_backup_due


def test_format_backup_filename_is_sortable_and_second_granular():
    when = datetime(2026, 8, 14, 18, 37, 51)
    assert format_backup_filename("inventory", when) == "inventory_20260814_183751.dump"


def test_format_backup_filename_differs_for_different_seconds():
    a = format_backup_filename("inventory", datetime(2026, 8, 14, 18, 37, 51))
    b = format_backup_filename("inventory", datetime(2026, 8, 14, 18, 37, 52))
    assert a != b


# -- is_backup_due --------------------------------------------------------#

def test_manual_frequency_is_never_due():
    now = datetime(2026, 8, 14, 12, 0, 0)
    assert is_backup_due(BackupFrequency.MANUAL, None, now) is False
    assert is_backup_due(BackupFrequency.MANUAL, now - timedelta(days=365), now) is False


def test_no_prior_backup_is_always_due_for_daily_or_weekly():
    now = datetime(2026, 8, 14, 12, 0, 0)
    assert is_backup_due(BackupFrequency.DAILY, None, now) is True
    assert is_backup_due(BackupFrequency.WEEKLY, None, now) is True


def test_daily_due_after_24_hours_not_before():
    now = datetime(2026, 8, 14, 12, 0, 0)
    assert is_backup_due(BackupFrequency.DAILY, now - timedelta(hours=23), now) is False
    assert is_backup_due(BackupFrequency.DAILY, now - timedelta(hours=25), now) is True


def test_weekly_due_after_7_days_not_before():
    now = datetime(2026, 8, 14, 12, 0, 0)
    assert is_backup_due(BackupFrequency.WEEKLY, now - timedelta(days=6), now) is False
    assert is_backup_due(BackupFrequency.WEEKLY, now - timedelta(days=8), now) is True
