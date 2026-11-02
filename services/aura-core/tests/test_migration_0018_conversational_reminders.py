"""Forward-only migration contract for conversational reminder commands."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0018_conversational_reminders.py"
)


def _source() -> str:
    assert MIGRATION.exists(), "AURA-0066 must provide migration 0018"
    return MIGRATION.read_text(encoding="utf-8")


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0018", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conversational_migration_is_additive_and_forward_only() -> None:
    migration = _module()
    assert migration.revision == "0018_conversational_reminders"
    assert migration.down_revision == "0017_recurring_reminders"
    source = _source().upper()
    assert "DROP TABLE" not in source
    assert "TRUNCATE" not in source
    assert "DELETE FROM" not in source
    assert "DOWNGRADE" in source
    assert "RAISE RUNTIMEERROR" in source


def test_command_proposals_are_private_and_policy_successor_is_explicit() -> None:
    source = _source()
    table = source.split("CREATE TABLE IF NOT EXISTS reminder_command_proposals", 1)[1].split(
        "CREATE INDEX IF NOT EXISTS", 1
    )[0]
    for column in (
        "principal_issuer",
        "principal_subject",
        "operation",
        "tool_id",
        "tool_version",
        "target_type",
        "target_id",
        "expected_version",
        "normalized_arguments",
        "client_time_zone",
        "private_summary",
        "run_id",
        "conversation_id",
        "tool_call_id",
        "expires_at",
    ):
        assert column in table
    assert "normalized_arguments JSONB NOT NULL" in table
    assert "uq_reminder_command_proposals_tool_call" in source
    assert "UPDATE agent_profiles SET current_revision_id" in source
    assert "AND current_revision_id = :source" in source
    assert "_GENERAL_AGENT" in source
    assert "_TOOL_IDS" in source
    for tool_id in (
        "reminder.list",
        "reminder.inspect",
        "reminder.series.create",
        "reminder.reschedule",
        "reminder.series.update",
        "reminder.series.pause",
        "reminder.series.resume",
        "reminder.cancel",
    ):
        assert f'"{tool_id}"' in source


def test_migration_does_not_repin_historical_or_custom_agents() -> None:
    source = _source()
    # The only profile mutation is guarded by the built-in general profile and
    # its exact source revision; custom rows and historical pins are untouched.
    update = source.split("UPDATE agent_profiles SET current_revision_id", 1)[1]
    assert "WHERE id = :agent" in update
    assert "current_revision_id = :source" in update
    assert "UPDATE agent_revisions" not in source
    assert "UPDATE tool_policy_revisions" not in source
    assert "principal_issuer LIKE" not in source


def test_live_fresh_and_repeat_upgrade_preserve_historical_rows() -> None:
    url = os.environ.get("AURA_0066_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0066_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")

    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    config = alembic_config(Settings(database_url=url))
    command.upgrade(config, "0017_recurring_reminders")
    engine = create_engine(url)
    historical_id = "00000000-0000-0000-0000-000000000066"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_issuer, principal_subject, title, version) "
                    "VALUES (:id, :issuer, :subject, 'historical', 1) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": historical_id, "issuer": "https://migration.example", "subject": "old"},
            )
        command.upgrade(config, "0018_conversational_reminders")
        command.upgrade(config, "0018_conversational_reminders")
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = 'reminder_command_proposals'"
                )
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT title FROM conversations WHERE id = :id"),
                {"id": historical_id},
            ).scalar_one() == "historical"
    finally:
        engine.dispose()
