"""Migration-shape checks for AURA-0047 recall-mode persistence."""

from __future__ import annotations

import importlib.util
import os
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

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"
_NAMESPACE = NAMESPACE


def _migration() -> tuple[Path, Any]:
    path = MIGRATION_DIR / "0011_memory_recall_modes.py"
    spec = importlib.util.spec_from_file_location("aura_migration_0011", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


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
    operations = Operations(MigrationContext.configure(connection))
    with Operations.context(operations):
        module.upgrade()


def test_recall_mode_migration_is_forward_only_and_idempotent() -> None:
    path, migration = _migration()
    source = path.read_text()

    assert migration.revision == "0011_memory_recall_modes"
    assert migration.down_revision == "0010_memory_recall_policies"
    assert "ADD COLUMN IF NOT EXISTS recall_mode" in source
    assert "ADD COLUMN IF NOT EXISTS automatic_recall_threshold" in source
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_policy_owner_revision" in (
        Path(__file__).parents[1]
        / "migrations/versions/0010_memory_recall_policies.py"
    ).read_text()
    assert "UPDATE memory_policy_revisions SET recall_mode = 'automatic'" in source
    assert "UPDATE memory_policy_revisions SET automatic_recall_threshold = 0.7" in source
    assert "ON CONFLICT (policy_id, foreign_agent_profile_id) DO NOTHING" in source
    assert "WHERE id = :id" in source
    assert "DROP TABLE" not in source.upper()
    assert "DELETE FROM" not in source.upper()
    assert "downgrade is disabled" in source


def test_recall_mode_migration_bounds_values_and_preserves_stricter_limits() -> None:
    source = _migration()[0].read_text()

    assert "CHECK (recall_mode IN ('off', 'automatic'))" in source
    assert "CHECK (automatic_recall_threshold BETWEEN 0 AND 1)" in source
    assert "LEAST(:max_memories, 2)" in source
    assert "LEAST(:context_fraction, 0.05)" in source
    assert "GREATEST(:automatic_threshold, 0.7)" in source
    assert ":shared, :current, :fallback_threshold" in source
    assert ":promote, " in source
    assert "'automatic', GREATEST(:automatic_threshold, 0.7)" in source


def test_recall_mode_migration_creates_deterministic_successors_without_repinning_runs() -> None:
    source = _migration()[0].read_text()

    assert "agent-memory-recall-policy:{profile_id}:2" in source
    assert "agent-memory-recall-revision:{profile_id}:2" in source
    assert "UPDATE agent_profiles SET current_revision_id" in source
    assert "version = version + 1" in source
    assert "WHERE id = :profile AND current_revision_id = :old_revision" in source
    assert "COALESCE(MAX(revision), 0) + 1 FROM agent_revisions" in source
    assert "UPDATE runs" not in source
    assert "UPDATE conversations" not in source


def test_new_sql_custom_agent_defaults_are_off_and_conservative() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/aura_core/domains/interaction/agents/adapters/sql_store.py"
    ).read_text()

    assert 'recall_mode="off"' in source
    assert "max_memories=2" in source
    assert "context_budget_fraction=0.05" in source
    assert "automatic_recall_threshold=0.7" in source


def test_recall_mode_migration_executes_successors_grants_and_replay_idempotently() -> None:
    """Run 0011 against a disposable PostgreSQL database when configured."""

    url = os.environ.get("AURA_0047_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0047_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")
    module = _migration()[1]
    profile_id, old_revision_id, old_policy_id = uuid4(), uuid4(), uuid4()
    foreign_profile_id = uuid4()
    conversation_id, message_id, run_id = uuid4(), uuid4(), uuid4()
    strict_profile_id, strict_revision_id, strict_policy_id = uuid4(), uuid4(), uuid4()
    strict_foreign_profile_id = uuid4()
    principal_id = uuid4()
    try:
        _upgrade(url, "0010_memory_recall_policies")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT 1 FROM pg_indexes WHERE tablename = "
                    "'memory_policy_revisions' AND indexname = "
                    "'uq_memory_policy_owner_revision'"
                )
            ).scalar_one() == 1
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://aura.test', 'migration-owner', 'Owner')"
                ),
                {"id": principal_id},
            )
            model_policy_id = uuid4()
            connection.execute(
                text(
                    "INSERT INTO model_policy_revisions (id, revision, provider, policy) "
                    "VALUES (:id, 1, 'ollama', CAST(:policy AS JSONB))"
                ),
                {"id": model_policy_id, "policy": '{"fallback": false}'},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, slug, display_name, status, version, current_revision_id) "
                    "VALUES (:id, 'migration-agent', 'Migration Agent', 'active', 1, :revision), "
                    "(:foreign, 'migration-foreign', 'Foreign', 'active', 1, NULL), "
                    "(:strict, 'strict-agent', 'Strict Agent', 'active', 1, NULL), "
                    "(:strict_foreign, 'strict-foreign', 'Strict Foreign', 'active', 1, NULL)"
                ),
                {
                    "id": profile_id,
                    "revision": old_revision_id,
                    "foreign": foreign_profile_id,
                    "strict": strict_profile_id,
                    "strict_foreign": strict_foreign_profile_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memory_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "shared_user_read, current_agent_read, fallback_relevance_threshold, "
                    "max_memories, context_budget_fraction, allow_shared_user_promotion) "
                    "VALUES (:id, '', '', :agent, 1, TRUE, TRUE, 0.77, 6, 0.18, TRUE), "
                    "(:strict_policy, '', '', :strict_agent, 1, FALSE, TRUE, 0.88, 7, 0.2, TRUE)"
                ),
                {
                    "id": old_policy_id,
                    "agent": profile_id,
                    "strict_policy": strict_policy_id,
                    "strict_agent": strict_profile_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                    "instructions, model_policy_revision_id, memory_policy_revision_id) "
                    "VALUES (:id, :agent, 1, 'Migration Agent', 'prompt', 'purpose', "
                    "'instructions', "
                    ":model, :policy), (:strict_revision, :strict_agent, 1, 'Strict Agent', "
                    "'strict prompt', 'purpose', 'instructions', :model, :strict_policy)"
                ),
                {
                    "id": old_revision_id,
                    "agent": profile_id,
                    "model": model_policy_id,
                    "policy": old_policy_id,
                    "strict_revision": strict_revision_id,
                    "strict_agent": strict_profile_id,
                    "strict_policy": strict_policy_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memory_policy_fallback_grants "
                    "(policy_id, foreign_agent_profile_id) VALUES (:policy, :foreign)"
                ),
                {"policy": old_policy_id, "foreign": foreign_profile_id},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) "
                    "VALUES (:id, :principal, 'Pinned', :agent, :revision, 'chat', 1)"
                ),
                {
                    "id": conversation_id,
                    "principal": principal_id,
                    "agent": profile_id,
                    "revision": old_revision_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'hello', 'complete')"
                ),
                {"id": message_id, "conversation": conversation_id},
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
                    "id": run_id,
                    "conversation": conversation_id,
                    "message": message_id,
                    "revision": old_revision_id,
                    "model": model_policy_id,
                },
            )
        with engine.begin() as connection:
            _run_upgrade(module, connection)
        with engine.begin() as connection:
            # Add a stricter post-0011 source to verify successor min/max
            # behavior and preserve valid scope flags and grants.
            connection.execute(
                text(
                    "UPDATE agent_profiles SET current_revision_id = :revision "
                    "WHERE id = :profile"
                ),
                {"revision": strict_revision_id, "profile": strict_profile_id},
            )
            connection.execute(
                text(
                    "UPDATE memory_policy_revisions SET recall_mode = 'off', "
                    "automatic_recall_threshold = 0.92 WHERE id = :id"
                ),
                {"id": strict_policy_id},
            )
            connection.execute(
                text(
                    "INSERT INTO memory_policy_fallback_grants "
                    "(policy_id, foreign_agent_profile_id) VALUES (:policy, :foreign)"
                ),
                {"policy": strict_policy_id, "foreign": strict_foreign_profile_id},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, slug, display_name, status, version, current_revision_id) "
                    "VALUES (:id, 'strict-fallback', 'Strict Fallback', 'active', 1, NULL) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": strict_foreign_profile_id},
            )
            _run_upgrade(module, connection)
        with engine.begin() as connection:
            first = connection.execute(
                text(
                    "SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"
                ),
                {"id": profile_id},
            ).one()
            successor_policy_id = uuid5(
                _NAMESPACE, f"agent-memory-recall-policy:{profile_id}:2"
            )
            successor_revision_id = uuid5(
                _NAMESPACE, f"agent-memory-recall-revision:{profile_id}:2"
            )
            assert first == (successor_revision_id, 2)
            policy = connection.execute(
                text(
                    "SELECT recall_mode, automatic_recall_threshold, max_memories, "
                    "context_budget_fraction, shared_user_read, current_agent_read, "
                    "allow_shared_user_promotion FROM memory_policy_revisions WHERE id = :id"
                ),
                {"id": successor_policy_id},
            ).one()
            assert tuple(policy) == ("automatic", 0.7, 2, 0.05, True, True, True)
            assert connection.execute(
                text(
                    "SELECT foreign_agent_profile_id FROM memory_policy_fallback_grants "
                    "WHERE policy_id = :id"
                ),
                {"id": successor_policy_id},
            ).scalar_one() == foreign_profile_id
            assert connection.execute(
                text("SELECT agent_revision_id FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            ).scalar_one() == old_revision_id
            assert connection.execute(
                text("SELECT agent_revision_id FROM runs WHERE id = :id"),
                {"id": run_id},
            ).scalar_one() == old_revision_id
            strict_successor = uuid5(
                _NAMESPACE, f"agent-memory-recall-policy:{strict_profile_id}:2"
            )
            strict = connection.execute(
                text(
                    "SELECT recall_mode, automatic_recall_threshold, max_memories, "
                    "context_budget_fraction, shared_user_read, current_agent_read, "
                    "allow_shared_user_promotion FROM memory_policy_revisions WHERE id = :id"
                ),
                {"id": strict_successor},
            ).one()
            assert tuple(strict) == ("automatic", 0.92, 2, 0.05, False, True, True)
            assert connection.execute(
                text(
                    "SELECT foreign_agent_profile_id FROM memory_policy_fallback_grants "
                    "WHERE policy_id = :id"
                ),
                {"id": strict_successor},
            ).scalar_one() == strict_foreign_profile_id
            before_replay = connection.execute(
                text(
                    "SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"
                ),
                {"id": profile_id},
            ).one()
            _run_upgrade(module, connection)
            after_replay = connection.execute(
                text(
                    "SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"
                ),
                {"id": profile_id},
            ).one()
            assert after_replay == before_replay
            assert connection.execute(
                text("SELECT COUNT(*) FROM agent_revisions WHERE id = :id"),
                {"id": successor_revision_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()
