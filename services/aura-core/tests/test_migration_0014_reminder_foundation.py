"""Migration safety and upgrade-shape checks for AURA-0054."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0014_reminder_foundation.py"
REMINDER_REPOSITORY = (
    Path(__file__).parents[1] / "src/aura_core/domains/automation/reminders/repository.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_is_the_forward_only_successor_and_never_destroys_state() -> None:
    source = _source()
    assert re.search(r'^revision\s*=\s*["\']0014_reminder_foundation["\']', source, re.MULTILINE)
    assert re.search(
        r'^down_revision\s*=\s*["\']0013_governed_tool_runtime["\']',
        source,
        re.MULTILINE,
    )
    assert "downgrade is disabled" in source.lower()
    assert "DROP TABLE" not in source.upper()
    assert "TRUNCATE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_fresh_and_upgraded_shapes_are_idempotent_and_backfill_legacy_outbox_rows() -> None:
    source = _source()
    assert "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS aggregate_type" in source
    assert "UPDATE outbox SET aggregate_type = 'run' WHERE aggregate_type IS NULL" in source
    assert "ALTER COLUMN aggregate_type SET NOT NULL" in source
    assert source.count("IF NOT EXISTS") >= 5
    for table in ("reminders", "reminder_command_idempotency"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    for column in (
        "principal_issuer",
        "principal_subject",
        "content",
        "due_at",
        "status",
        "version",
        "created_at",
        "updated_at",
        "fired_at",
        "completed_at",
        "canceled_at",
    ):
        assert column in source
    assert "CHECK (char_length(content) BETWEEN 1 AND 2000)" in source
    assert "CHECK (status IN ('scheduled', 'fired', 'completed', 'canceled'))" in source
    assert "PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)" in source
    assert "UNIQUE (id, principal_issuer, principal_subject)" in source


def test_migration_indexes_support_owner_bound_upcoming_and_history_pagination() -> None:
    source = _source()
    assert "ix_reminders_owner_due" in source
    assert "(principal_issuer, principal_subject, due_at, id)" in source
    assert "ix_reminders_owner_updated" in source
    assert "(principal_issuer, principal_subject, updated_at, id)" in source


def test_durable_reminder_audit_must_record_a_principal_and_never_content() -> None:
    source = REMINDER_REPOSITORY.read_text(encoding="utf-8")
    assert "self._audit.stage(session, None" not in source
    assert (
        "content" not in source[source.index("def _stage") : source.index("async def _transition")]
    )


def test_real_postgresql_upgrade_preserves_legacy_outbox_and_is_repeatable() -> None:
    """Run only when the caller provides a disposable migration database."""

    url = os.environ.get("AURA_0054_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0054_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")
    alembic = pytest.importorskip("alembic")
    del alembic
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    # The configured database is explicitly owned by the integration caller;
    # no test silently mutates a developer's default database.  The migration
    # itself supplies the legacy NULL backfill; this check verifies the
    # resulting NOT NULL invariant without inventing an outbox schema here.
    command.upgrade(alembic_config(Settings(database_url=url)), "0013_governed_tool_runtime")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = 'reminders'"
                    )
                ).scalar_one()
                == 0
            )
        command.upgrade(alembic_config(Settings(database_url=url)), "0014_reminder_foundation")
        command.upgrade(alembic_config(Settings(database_url=url)), "0014_reminder_foundation")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = 'reminders'"
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM outbox WHERE aggregate_type IS NULL")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = 'outbox' AND column_name = 'aggregate_type'"
                    )
                ).scalar_one()
                == 1
            )

            valid_id = str(uuid4())
            valid = {
                "id": valid_id,
                "issuer": "https://migration-owner.example",
                "subject": f"migration-{uuid4()}",
                "content": "valid",
                "due_at": datetime.now(UTC),
            }
            connection.execute(
                text(
                    "INSERT INTO reminders "
                    "(id, principal_issuer, principal_subject, content, due_at, status, version) "
                    "VALUES (:id, :issuer, :subject, :content, :due_at, 'scheduled', 1)"
                ),
                valid,
            )

            def assert_constraint_violation(content: str, status: str, version: int) -> None:
                with pytest.raises(DBAPIError):
                    with connection.begin_nested():
                        connection.execute(
                            text(
                                "INSERT INTO reminders "
                                "(id, principal_issuer, principal_subject, content, due_at, "
                                "status, version) "
                                "VALUES (:id, :issuer, :subject, :content, :due_at, "
                                ":status, :version)"
                            ),
                            {
                                **valid,
                                "id": str(uuid4()),
                                "content": content,
                                "status": status,
                                "version": version,
                            },
                        )

            assert_constraint_violation("", "scheduled", 1)
            assert_constraint_violation("x" * 2001, "scheduled", 1)
            assert_constraint_violation("valid", "unknown", 1)
            assert_constraint_violation("valid", "scheduled", 0)
            connection.execute(text("DELETE FROM reminders WHERE id = :id"), {"id": valid_id})
    finally:
        engine.dispose()


def test_blank_postgresql_upgrade_creates_reminder_constraints() -> None:
    """Run against a separate disposable blank database when provided."""

    url = os.environ.get("AURA_0054_FRESH_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0054_FRESH_DATABASE_URL is required for blank-database coverage")
    pytest.importorskip("alembic")
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = 'reminders'"
                    )
                ).scalar_one()
                == 1
            )
            check_constraints = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.table_constraints "
                    "WHERE table_name = 'reminders' AND constraint_type = 'CHECK'"
                )
            ).scalar_one()
            assert check_constraints >= 3
    finally:
        engine.dispose()
