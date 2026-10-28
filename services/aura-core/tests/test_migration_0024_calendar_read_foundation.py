"""Static and optional live safeguards for the calendar foundation migration."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations/versions/0024_calendar_read_foundation.py"


def _source() -> str:
    assert MIGRATION.exists(), "AURA-0130 must provide migration 0024"
    return MIGRATION.read_text(encoding="utf-8")


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0024", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calendar_migration_is_linear_forward_only_successor() -> None:
    migration = _module()
    assert migration.revision == "0024_calendar_read_foundation"
    assert migration.down_revision == "0023_evaluation_runner"
    source = _source().upper()
    assert "DROP TABLE" not in source
    assert "DROP COLUMN" not in source
    assert "TRUNCATE" not in source
    assert "DELETE FROM" not in source
    assert "DOWNGRADE" in source and "DISABLED" in source


def test_calendar_migration_has_only_owner_scoped_metadata_state() -> None:
    source = _source().lower()
    for table in (
        "calendar_connections",
        "calendar_credentials",
        "calendar_discovered_calendars",
        "calendar_selected_calendars",
        "calendar_selection_versions",
        "calendar_command_idempotency",
        "calendar_audit_entries",
    ):
        assert f"create table if not exists {table}" in source

    for column in (
        "principal_issuer",
        "principal_subject",
        "connection_id",
        "status",
        "version",
        "created_at",
        "updated_at",
    ):
        assert column in source

    # Event payloads are transient provider results; this migration must not
    # create a tempting durable event cache or outbox payload.
    assert "calendar_events" not in source
    assert "event_title" not in source
    assert "description" not in source
    assert "attendees" not in source
    assert "location" not in source
    assert "event_content" not in source
    assert "provider_payload" not in source


def test_calendar_migration_binds_every_private_table_to_an_owner() -> None:
    source = _source().lower()
    assert "unique (principal_issuer, principal_subject, provider)" in source
    audit_definition = source.split(
        "create table if not exists calendar_audit_entries", 1
    )[1].split(")", 1)[0]
    assert "principal_issuer" not in audit_definition
    assert "principal_subject" not in audit_definition
    assert "check (status in" in source
    assert "check (version > 0)" in source or "check (version >= 1)" in source
    assert "idempotency_key" in source
    assert "revocation_attempt" in source or "attempt_count" in source
    assert "retry_after" in source or "next_revocation_at" in source


def test_calendar_migration_has_owner_and_lifecycle_indexes() -> None:
    source = _source().lower()
    for index_name in (
        "ix_calendar_connections_owner",
        "ix_calendar_metadata_connection_readable",
        "ix_calendar_audit_time",
    ):
        assert index_name in source
    assert "ix_calendar_audit_owner_time" not in source
    assert "on conflict" not in source


def test_calendar_audit_schema_has_only_bounded_lifecycle_metadata() -> None:
    from aura_core.domains.integrations.calendar.persistence import CalendarAuditRow

    columns = {column.name for column in CalendarAuditRow.__table__.columns}
    assert {"id", "action", "outcome", "category", "occurred_at"} <= columns
    assert not {
        "principal_issuer",
        "principal_subject",
        "owner_identity",
        "provider_id",
        "provider_identifier",
        "calendar_name",
        "event_title",
        "metadata",
        "payload",
    } & columns


@pytest.mark.skipif(
    not os.environ.get("AURA_0130_MIGRATION_DATABASE_URL"),
    reason="AURA_0130_MIGRATION_DATABASE_URL is required for PostgreSQL coverage",
)
def test_calendar_migration_live_upgrade_is_repeatable() -> None:
    """The disposable integration database must be explicitly supplied."""

    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    url = os.environ["AURA_0130_MIGRATION_DATABASE_URL"]
    config = alembic_config(Settings(database_url=url))
    command.upgrade(config, "0023_evaluation_runner")
    command.upgrade(config, "0024_calendar_read_foundation")
    command.upgrade(config, "0024_calendar_read_foundation")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'calendar_%'"
                )
            ).scalars()
            assert {
                "calendar_connections",
                "calendar_credentials",
                "calendar_discovered_calendars",
                "calendar_selected_calendars",
                "calendar_selection_versions",
                "calendar_command_idempotency",
                "calendar_audit_entries",
            } <= set(tables)
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'calendar_events'"
                )
            ).scalar_one() == 0
    finally:
        engine.dispose()
