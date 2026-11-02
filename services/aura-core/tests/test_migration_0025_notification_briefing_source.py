"""Static safeguards for the additive notification-source migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0025_notification_briefing_source.py"
)


def test_notification_source_migration_follows_calendar_foundation_and_is_forward_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "0025_notification_brief_source"' in source
    assert 'down_revision = "0024_calendar_read_foundation"' in source
    assert "downgrade is disabled" in source.lower()
    assert "DROP TABLE" not in source.upper()
    assert "TRUNCATE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_source_evolution_preserves_reminders_and_adds_only_briefing_result_identity() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ALTER COLUMN reminder_id DROP NOT NULL" in source
    assert "source_kind" in source
    assert "briefing_schedule_id" in source
    assert "briefing_occurrence_id" in source
    assert "briefing_result_id" in source
    assert "source_kind = 'reminder'" in source
    assert "source_kind = 'briefing'" in source
    assert "uq_notification_reminder" not in source
    assert "briefing" in source.lower()
    assert "CREATE TABLE" not in source.upper()


def test_source_migration_does_not_create_briefing_owned_tables_or_content_columns() -> None:
    source = MIGRATION.read_text(encoding="utf-8").lower()
    assert "briefing_schedules" not in source
    assert "briefing_occurrences" not in source
    assert "briefing_results" not in source
    for forbidden in ("content", "prompt", "provider", "credentials"):
        assert forbidden not in source
