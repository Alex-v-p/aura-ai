"""Forward-only migration contract for governed reminder approvals."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import cast

import pytest
from aura_core.domains.governance.approvals.persistence import ApprovalEventRow
from aura_core.domains.interaction.agents.persistence import ToolPolicyRevisionRow
from aura_core.domains.interaction.agents.public import (
    GENERAL_PROFILE_ID,
    GENERAL_SUCCESSOR_REVISION_ID,
    GENERAL_SUCCESSOR_TOOL_POLICY_ID,
)
from sqlalchemy import Index, Table
from sqlalchemy.schema import ColumnCollectionConstraint, DefaultClause

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0016_approval_backed_reminder_tool.py"
)


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0016", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_approval_migration_is_the_forward_successor_and_has_no_destructive_ddl() -> None:
    migration = _module()
    assert migration.revision.startswith("0016_")
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0015_notification_delivery"
    source = _source().upper()
    assert "DROP TABLE" not in source
    assert "TRUNCATE" not in source
    assert "DELETE FROM" not in source


def test_approval_migration_persists_separate_private_proposals_and_metadata_only_approvals(
) -> None:
    source = _source()
    for table in (
        "reminder_proposals",
        "approvals",
        "approval_events",
        "approval_command_idempotency",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    for column in (
        "principal_issuer",
        "principal_subject",
        "proposal_id",
        "run_id",
        "conversation_id",
        "tool_call_id",
        "tool_id",
        "tool_version",
        "status",
        "execution_status",
        "version",
        "replay_position",
        "expires_at",
    ):
        assert column in source
    assert "CREATE SEQUENCE IF NOT EXISTS approval_events_replay_position_seq" in source
    assert "ALTER SEQUENCE approval_events_replay_position_seq" in source
    assert "SET replay_position = ordered.replay_position" in source
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_events_replay_position" in source
    assert "ix_approval_events_owner_replay_position" in source
    assert "ix_approval_events_owner_cursor ON approval_events" not in source
    # Approval rows reference the proposal, but do not duplicate its private
    # content or requested due instant.
    approval_table = source.split("CREATE TABLE IF NOT EXISTS approvals", 1)[1].split(
        "CREATE TABLE IF NOT EXISTS", 1
    )[0]
    assert "content" not in approval_table
    assert "due_at" not in approval_table
    assert "_GENERAL_SUCCESSOR_POLICY" in source
    assert "_GENERAL_SUCCESSOR_REVISION" in source
    assert '"memory.search", "reminder.create"' in source
    assert "timeout_ms, retry_limit" in source
    assert "WHERE permission_scopes IS NULL" in source
    assert "ALTER COLUMN permission_scopes SET DEFAULT" in source
    assert "ALTER COLUMN permission_scopes SET NOT NULL" in source
    assert source.index("ALTER COLUMN permission_scopes SET NOT NULL") < source.index(
        "INSERT INTO tool_policy_revisions"
    )


def test_metadata_bootstrap_defaults_historical_tool_policy_scope_to_empty() -> None:
    column = ToolPolicyRevisionRow.__table__.c.permission_scopes

    assert column.nullable is False
    assert column.default is not None
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'[]'::jsonb"


def test_metadata_bootstrap_uses_monotonic_approval_replay_sequence() -> None:
    table: Table = cast(Table, ApprovalEventRow.__table__)
    column = table.c.replay_position
    indexes: dict[str, Index] = {
        index.name: index
        for index in table.indexes
        if index.name is not None
    }
    constraints = {
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ColumnCollectionConstraint)
    }

    assert column.nullable is False
    assert column.server_default is not None
    server_default = cast(DefaultClause, column.server_default)
    assert "approval_events_replay_position_seq" in str(server_default.arg)
    assert {
        "uq_approval_events_replay_position",
        "ix_approval_events_owner_replay_position",
    } <= set(indexes)
    assert indexes["uq_approval_events_replay_position"].unique is True
    assert [column.name for column in indexes["uq_approval_events_replay_position"].columns] == [
        "replay_position"
    ]
    assert [
        column.name
        for column in indexes["ix_approval_events_owner_replay_position"].columns
    ] == [
        "principal_issuer",
        "principal_subject",
        "replay_position",
    ]
    assert not any(
        constraint.name is None
        and "replay_position" in {column.name for column in constraint.columns}
        for constraint in constraints
    )


def test_live_migration_upgrade_is_repeatable_and_preserves_previous_schema() -> None:
    url = os.environ.get("AURA_0056_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0056_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    command.upgrade(alembic_config(Settings(database_url=url)), "0015_notification_delivery")
    command.upgrade(
        alembic_config(Settings(database_url=url)), "0016_approval_reminder_tool"
    )
    command.upgrade(
        alembic_config(Settings(database_url=url)), "0016_approval_reminder_tool"
    )
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for table in (
                "reminder_proposals",
                "approvals",
                "approval_events",
                "approval_command_idempotency",
            ):
                assert connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = :table"
                    ),
                    {"table": table},
                ).scalar_one() == 1
            replay_position = connection.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'approval_events' AND column_name = 'replay_position'"
                )
            ).one()
            assert replay_position.is_nullable == "NO"
            assert "approval_events_replay_position_seq" in str(replay_position.column_default)
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.sequences "
                    "WHERE sequence_name = 'approval_events_replay_position_seq'"
                )
            ).scalar_one() == 1
            indexes = {
                row.indexname: row.indexdef
                for row in connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'approval_events'"
                    )
                )
            }
            assert "uq_approval_events_replay_position" in indexes
            assert "UNIQUE INDEX" in indexes["uq_approval_events_replay_position"]
            assert "(replay_position)" in indexes["uq_approval_events_replay_position"]
            assert "ix_approval_events_owner_replay_position" in indexes
            assert "(principal_issuer, principal_subject, replay_position)" in indexes[
                "ix_approval_events_owner_replay_position"
            ]
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = 'approvals' AND column_name IN "
                    "('content', 'due_at')"
                )
            ).scalar_one() == 0
            successor = connection.execute(
                text(
                    "SELECT current_revision_id FROM agent_profiles "
                    "WHERE id = :profile"
                ),
                {"profile": GENERAL_PROFILE_ID},
            ).scalar_one_or_none()
            assert successor == GENERAL_SUCCESSOR_REVISION_ID
            policy_id = connection.execute(
                text(
                    "SELECT tool_policy_revision_id FROM agent_revisions "
                    "WHERE id = :revision AND agent_profile_id = :profile"
                ),
                {"revision": GENERAL_SUCCESSOR_REVISION_ID, "profile": GENERAL_PROFILE_ID},
            ).scalar_one()
            assert policy_id == GENERAL_SUCCESSOR_TOOL_POLICY_ID
            policy = connection.execute(
                text(
                    "SELECT timeout_ms, retry_limit, permission_scopes "
                    "FROM tool_policy_revisions WHERE id = :policy "
                    "AND agent_profile_id = :profile"
                ),
                {"policy": GENERAL_SUCCESSOR_TOOL_POLICY_ID, "profile": GENERAL_PROFILE_ID},
            ).one()
            assert policy.timeout_ms == 10000
            assert policy.retry_limit == 0
            assert set(policy.permission_scopes) == {"memory:read", "reminders:write"}
            column = connection.execute(
                text(
                    "SELECT is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'tool_policy_revisions' "
                    "AND column_name = 'permission_scopes'"
                )
            ).one()
            assert column.is_nullable == "NO"
            assert "[]" in str(column.column_default)
            grants = connection.execute(
                text(
                    "SELECT tool_id, tool_version FROM tool_policy_grants "
                    "WHERE policy_id = :policy ORDER BY tool_id"
                ),
                {"policy": GENERAL_SUCCESSOR_TOOL_POLICY_ID},
            ).all()
            assert grants == [("memory.search", 1), ("reminder.create", 1)]
    finally:
        engine.dispose()


def test_live_fresh_migration_creates_approval_constraints() -> None:
    url = os.environ.get("AURA_0056_FRESH_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0056_FRESH_DATABASE_URL is required for blank-database coverage")
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import create_engine, text

    command.upgrade(alembic_config(Settings(database_url=url)), "head")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.table_constraints "
                    "WHERE table_name = 'approvals' AND constraint_type = 'CHECK'"
                )
            ).scalar_one() >= 3
            indexes = {
                row.indexname: row.indexdef
                for row in connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'approval_events'"
                    )
                )
            }
            assert "uq_approval_events_replay_position" in indexes
            assert "ix_approval_events_owner_replay_position" in indexes
            replay_position = connection.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'approval_events' AND column_name = 'replay_position'"
                )
            ).one()
            assert replay_position.is_nullable == "NO"
            assert "approval_events_replay_position_seq" in str(replay_position.column_default)
            column = connection.execute(
                text(
                    "SELECT is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'tool_policy_revisions' "
                    "AND column_name = 'permission_scopes'"
                )
            ).one()
            assert column.is_nullable == "NO"
            assert "[]" in str(column.column_default)
    finally:
        engine.dispose()
