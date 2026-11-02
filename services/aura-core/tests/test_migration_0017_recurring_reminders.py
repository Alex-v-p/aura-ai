"""Forward-only migration contract for recurring reminder schedules."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0017_recurring_reminders.py"
)


def _source() -> str:
    assert MIGRATION.exists(), "AURA-0064 must provide migration 0017"
    return MIGRATION.read_text(encoding="utf-8")


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0017", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recurring_migration_is_the_additive_successor_to_0016() -> None:
    migration = _module()
    assert migration.revision == "0017_recurring_reminders"
    assert migration.down_revision == "0016_approval_reminder_tool"
    assert len(migration.revision) <= 32

    source = _source().upper()
    assert "DROP TABLE" not in source
    assert "TRUNCATE" not in source
    assert "DELETE FROM" not in source
    assert "ALTER TABLE REMINDERS" not in source
    assert "ALTER TABLE NOTIFICATIONS" not in source


def test_recurring_migration_creates_private_series_occurrences_and_commands() -> None:
    source = _source()
    for table in (
        "reminder_series",
        "reminder_occurrences",
        "reminder_series_command_idempotency",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source

    # Keep the private recurrence payload and lifecycle state in the series
    # aggregate; occurrence history is an immutable link to an ordinary
    # reminder row and must not duplicate reminder content.
    for column in (
        "principal_issuer",
        "principal_subject",
        "content",
        "frequency",
        "interval",
        "weekdays",
        "month_day",
        "local_start",
        "timezone",
        "count_limit",
        "end_date",
        "status",
        "version",
        "next_due_at",
        "missed_count",
        "occurrence_sequence",
        "reminder_id",
        "operation",
        "fingerprint",
        "response",
    ):
        assert column in source

    series_ddl = source.split("CREATE TABLE IF NOT EXISTS reminder_series", 1)[1].split(
        "CREATE TABLE IF NOT EXISTS", 1
    )[0]
    occurrence_ddl = source.split(
        "CREATE TABLE IF NOT EXISTS reminder_occurrences", 1
    )[1].split("CREATE TABLE IF NOT EXISTS", 1)[0]
    idempotency_ddl = source.split(
        "CREATE TABLE IF NOT EXISTS reminder_series_command_idempotency", 1
    )[1]

    assert "status IN ('active', 'paused', 'completed', 'canceled')" in series_ddl
    assert "interval_value > 0" in series_ddl
    assert "version > 0" in series_ddl
    assert "missed_count >= 0" in series_ddl
    assert "UNIQUE" in occurrence_ddl.upper()
    assert "occurrence_sequence" in occurrence_ddl
    assert "reminder_id" in occurrence_ddl
    assert "fingerprint" in idempotency_ddl
    assert "response" in idempotency_ddl

    # A recurring occurrence may link to an existing one-shot reminder, but
    # ordinary reminder content must remain owned by the reminder table.
    assert "content" not in occurrence_ddl.lower()
    assert "due_at" in occurrence_ddl.lower()


def test_live_upgrade_to_0017_is_repeatable_and_preserves_one_shot_rows() -> None:
    url = os.environ.get("AURA_0064_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0064_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")

    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    config = alembic_config(Settings(database_url=url))
    command.upgrade(config, "0016_approval_reminder_tool")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reminders "
                    "(id, principal_issuer, principal_subject, content, due_at) "
                    "VALUES ('00000000-0000-0000-0000-000000000064', :issuer, :subject, "
                    ":content, '2099-01-01T00:00:00+00:00') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "issuer": "https://migration.example",
                    "subject": "migration-owner",
                    "content": "existing one-shot",
                },
            )
        command.upgrade(config, "0017_recurring_reminders")
        command.upgrade(config, "0017_recurring_reminders")
        with engine.begin() as connection:
            for table in (
                "reminder_series",
                "reminder_occurrences",
                "reminder_series_command_idempotency",
            ):
                assert connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = :table"
                    ),
                    {"table": table},
                ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT content FROM reminders "
                    "WHERE id = '00000000-0000-0000-0000-000000000064'"
                )
            ).scalar_one() == "existing one-shot"
    finally:
        engine.dispose()
