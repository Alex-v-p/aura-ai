"""Safety and determinism checks for the governed tool-runtime migration."""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4, uuid5

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from aura_core.domains.interaction.agents.public import NAMESPACE
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0013_governed_tool_runtime.py"


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("aura_migration_0013_stateful", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database_unavailable(exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "could not connect",
            "connection timed out",
            "timeout expired",
            "could not translate host name",
            "database system is starting",
            "database system is not yet accepting",
        )
    )


def _run_upgrade(module: Any, connection: Any) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


def test_migration_0013_is_single_forward_only_revision() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert re.search(
        r'^revision\s*=\s*["\']0013_governed_tool_runtime["\']', source, re.MULTILINE
    )
    assert re.search(
        r'^down_revision\s*=\s*["\']0012_memory_personal_first["\']', source, re.MULTILINE
    )
    assert "downgrade is disabled" in source.lower() or "raise RuntimeError" in source


def test_migration_0013_has_idempotent_tool_tables_and_no_destructive_reset() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for table in ("tool_policy_revisions", "tool_policy_grants"):
        assert table in source
    assert "IF NOT EXISTS" in source or "checkfirst=True" in source
    assert "DROP TABLE" not in source.upper()
    assert "TRUNCATE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_migration_0013_backfills_runs_and_agent_revisions_fail_closed() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "tool_policy_revision_id" in source
    assert "tool_call_limit" in source
    assert "tool_calls_used" in source
    assert "tool_timeout_ms" in source
    assert "tool_retry_limit" in source
    assert "memory.search" in source
    assert "general-assistant" in source.lower()
    assert "max_calls_per_run" in source
    assert "retry_limit" in source


def test_migration_0013_executes_built_in_successor_and_is_idempotent() -> None:
    """Exercise the real upgrade against a disposable, pre-0013 database."""

    url = os.environ.get("AURA_0053_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0053_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")

    module = _migration()
    custom_profile = uuid4()
    custom_revision = uuid4()
    principal = uuid4()
    conversation = uuid4()
    message = uuid4()
    run = uuid4()
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "0012_memory_personal_first")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            built_in = connection.execute(
                text(
                    "SELECT id, current_revision_id, version FROM agent_profiles "
                    "WHERE slug = 'general-assistant'"
                )
            ).one()
            old_revision = connection.execute(
                text(
                    "SELECT id, model_policy_revision_id FROM agent_revisions WHERE id = :id"
                ),
                {"id": built_in.current_revision_id},
            ).one()
            connection.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, slug, display_name, status, version, current_revision_id) "
                    "VALUES (:id, 'tool-runtime-migration-custom', 'Custom', 'active', "
                    "1, :revision)"
                ),
                {"id": custom_profile, "revision": custom_revision},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                    "instructions, model_policy_revision_id) "
                    "VALUES (:id, :profile, 1, 'Custom', 'prompt', 'purpose', "
                    "'instructions', :model)"
                ),
                {
                    "id": custom_revision,
                    "profile": custom_profile,
                    "model": old_revision.model_policy_revision_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://aura.test', 'tool-runtime-migration', 'Owner')"
                ),
                {"id": principal},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) VALUES (:id, :principal, 'Legacy run', "
                    ":agent, :revision, 'chat', 1)"
                ),
                {
                    "id": conversation,
                    "principal": principal,
                    "agent": built_in.id,
                    "revision": old_revision.id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'legacy', 'complete')"
                ),
                {"id": message, "conversation": conversation},
            )
            connection.execute(
                text(
                    "INSERT INTO runs "
                    "(id, conversation_id, user_message_id, agent_revision_id, "
                    "model_policy_revision_id, provider, model_id, status, attempt_count) "
                    "VALUES (:id, :conversation, :message, :revision, :model, 'ollama', "
                    "'chat', 'completed', 0)"
                ),
                {
                    "id": run,
                    "conversation": conversation,
                    "message": message,
                    "revision": old_revision.id,
                    "model": old_revision.model_policy_revision_id,
                },
            )

        # Invoke the real migration body twice.  The second invocation models
        # a retry after a partially completed deployment while preserving all
        # immutable IDs and rows.
        with engine.begin() as connection:
            _run_upgrade(module, connection)
        with engine.begin() as connection:
            built_in_after_first = connection.execute(
                text(
                    "SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"
                ),
                {"id": built_in.id},
            ).one()
            old_policy = connection.execute(
                text(
                    "SELECT tool_policy_revision_id FROM agent_revisions WHERE id = :id"
                ),
                {"id": old_revision.id},
            ).scalar_one()
            successor_policy = connection.execute(
                text(
                    "SELECT tool_policy_revision_id FROM agent_revisions WHERE id = :id"
                ),
                {"id": module._GENERAL_REVISION},
            ).scalar_one()
            legacy_run = connection.execute(
                text(
                    "SELECT tool_policy_revision_id, tool_call_limit, tool_calls_used, "
                    "tool_timeout_ms, tool_retry_limit FROM runs WHERE id = :id"
                ),
                {"id": run},
            ).one()
            custom_policy = connection.execute(
                text(
                    "SELECT tool_policy_revision_id FROM agent_revisions WHERE id = :id"
                ),
                {"id": custom_revision},
            ).scalar_one()
            assert built_in_after_first == (module._GENERAL_REVISION, 2)
            assert old_policy == uuid5(NAMESPACE, f"agent-tool-policy:{built_in.id}:1")
            assert successor_policy == module._GENERAL_POLICY
            assert custom_policy == uuid5(NAMESPACE, f"agent-tool-policy:{custom_profile}:1")
            assert legacy_run == (old_policy, 0, 0, 30_000, 0)
            assert connection.execute(
                text(
                    "SELECT max_calls_per_run, timeout_ms, retry_limit "
                    "FROM tool_policy_revisions WHERE id = :id"
                ),
                {"id": successor_policy},
            ).one() == (1, 30_000, 0)
            assert connection.execute(
                text(
                    "SELECT tool_id, tool_version FROM tool_policy_grants "
                    "WHERE policy_id = :id"
                ),
                {"id": successor_policy},
            ).all() == [("memory.search", 1)]
            counts_before = {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tool_policy_revisions", "tool_policy_grants", "agent_revisions")
            }
            _ = connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": built_in.id},
            ).one()
            _run_upgrade(module, connection)
            assert connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": built_in.id},
            ).one() == built_in_after_first
            counts_after = {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tool_policy_revisions", "tool_policy_grants", "agent_revisions")
            }
            assert counts_after == counts_before
            assert connection.execute(
                text("SELECT COUNT(*) FROM tool_policy_grants WHERE policy_id = :id"),
                {"id": old_policy},
            ).scalar_one() == 0
    finally:
        engine.dispose()
