"""Migration checks for AURA-0048 personal-first memory promotion."""

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
_GENERAL_AGENT = uuid5(_NAMESPACE, "general-assistant")
_SUCCESSOR_POLICY = uuid5(
    _NAMESPACE, "general-assistant-memory-personal-first-policy-3"
)
_SUCCESSOR_REVISION = uuid5(
    _NAMESPACE, "general-assistant-memory-personal-first-revision-3"
)


def _migration() -> tuple[Path, Any]:
    path = MIGRATION_DIR / "0012_memory_personal_first.py"
    spec = importlib.util.spec_from_file_location("aura_migration_0012", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


def _run_upgrade(module: Any, connection: Any) -> None:
    operations = Operations(MigrationContext.configure(connection))
    with Operations.context(operations):
        module.upgrade()


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


def test_personal_first_migration_is_forward_only_and_settles_empty_reviews() -> None:
    path, migration = _migration()
    source = path.read_text()

    assert migration.revision == "0012_memory_personal_first"
    assert migration.down_revision == "0011_memory_recall_modes"
    assert "ADD COLUMN IF NOT EXISTS " in source
    assert "retention_basis VARCHAR(16)" in source
    assert "SET retention_basis = 'none'" in source
    assert "ALTER COLUMN retention_basis SET NOT NULL" in source
    assert "memory_candidate_retention_basis_check" in source
    assert "retention_basis IN ('personal', 'explicit_request', 'none')" in source
    assert "SET state = 'rejected'" in source
    assert "decision_reason = 'invalid_provider_output'" in source
    assert "AND content IS NULL" in source
    assert "DELETE FROM" not in source.upper()
    assert "DROP TABLE" not in source.upper()
    assert "downgrade is disabled" in source


def test_personal_first_successor_is_built_in_only_and_preserves_policy_values() -> None:
    source = _migration()[0].read_text()

    assert '"general-assistant"' in source
    assert "general-assistant-memory-personal-first-policy-3" in source
    assert "general-assistant-memory-personal-first-revision-3" in source
    assert "allow_shared_user_promotion, " in source
    assert "recall_mode, automatic_recall_threshold" in source
    assert "shared_user_read, current_agent_read" in source
    assert "max_memories, context_budget_fraction" in source
    assert "ON CONFLICT (policy_id, foreign_agent_profile_id) DO NOTHING" in source
    assert "UPDATE agent_profiles SET current_revision_id" in source
    assert "WHERE id = :profile AND current_revision_id = :source" in source
    assert "UPDATE conversations" not in source
    assert "UPDATE runs" not in source


def test_candidate_orm_persists_a_non_null_retention_basis() -> None:
    mapping = (
        Path(__file__).parents[1]
        / "src/aura_core/domains/knowledge/memory/persistence.py"
    ).read_text()

    assert "retention_basis: Mapped[str]" in mapping
    assert "String(16), nullable=False, server_default=\"none\"" in mapping


def test_personal_first_migration_executes_idempotently_and_preserves_pins() -> None:
    """Exercise the real PostgreSQL migration when a test database is configured."""

    url = os.environ.get("AURA_0048_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0048_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")

    module = _migration()[1]
    custom_profile = uuid4()
    built_in_revision = uuid4()
    built_in_policy = uuid4()
    custom_revision = uuid4()
    custom_policy = uuid4()
    foreign_profile = uuid4()
    model_policy = uuid4()
    principal = uuid4()
    conversation = uuid4()
    message = uuid4()
    run = uuid4()
    job = uuid4()
    candidate = uuid4()

    try:
        _upgrade(url, "0011_memory_recall_modes")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://aura.test', 'personal-first-owner', 'Owner')"
                ),
                {"id": principal},
            )
            connection.execute(
                text(
                    "INSERT INTO model_policy_revisions (id, revision, provider, policy) "
                    "VALUES (:id, 1, 'ollama', CAST(:policy AS JSONB))"
                ),
                {"id": model_policy, "policy": '{"fallback": false}'},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, slug, display_name, status, version, current_revision_id) "
                    "VALUES (:built_in, 'general-assistant', 'Aura', 'active', 1, :built_rev), "
                    "(:custom, 'custom-personal-first', 'Custom', 'active', 1, :custom_rev), "
                    "(:foreign, 'foreign-personal-first', 'Foreign', 'active', 1, NULL)"
                ),
                {
                    "built_in": _GENERAL_AGENT,
                    "built_rev": built_in_revision,
                    "custom": custom_profile,
                    "custom_rev": custom_revision,
                    "foreign": foreign_profile,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memory_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "shared_user_read, current_agent_read, fallback_relevance_threshold, "
                    "max_memories, context_budget_fraction, allow_shared_user_promotion, "
                    "recall_mode, automatic_recall_threshold) "
                    "VALUES (:built_policy, 'https://aura.test', "
                    "'personal-first-owner', :built_in, "
                    "1, FALSE, TRUE, 0.83, 4, 0.11, FALSE, 'off', 0.91), "
                    "(:custom_policy, 'https://aura.test', 'personal-first-owner', :custom, "
                    "1, TRUE, FALSE, 0.66, 3, 0.09, FALSE, 'off', 0.88)"
                ),
                {
                    "built_policy": built_in_policy,
                    "built_in": _GENERAL_AGENT,
                    "custom_policy": custom_policy,
                    "custom": custom_profile,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                    "instructions, model_policy_revision_id, memory_policy_revision_id) "
                    "VALUES (:built_rev, :built_in, 1, 'Aura', 'prompt', 'purpose', "
                    "'instructions', :model, :built_policy), "
                    "(:custom_rev, :custom, 1, 'Custom', 'prompt', 'purpose', "
                    "'instructions', :model, :custom_policy)"
                ),
                {
                    "built_rev": built_in_revision,
                    "built_in": _GENERAL_AGENT,
                    "custom_rev": custom_revision,
                    "custom": custom_profile,
                    "model": model_policy,
                    "built_policy": built_in_policy,
                    "custom_policy": custom_policy,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memory_policy_fallback_grants "
                    "(policy_id, foreign_agent_profile_id) VALUES (:policy, :foreign)"
                ),
                {"policy": built_in_policy, "foreign": foreign_profile},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) "
                    "VALUES (:id, :principal, 'Pinned', :agent, :revision, 'chat', 1)"
                ),
                {
                    "id": conversation,
                    "principal": principal,
                    "agent": _GENERAL_AGENT,
                    "revision": built_in_revision,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'diagnostic', 'complete')"
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
                    "revision": built_in_revision,
                    "model": model_policy,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO memory_processing_jobs "
                    "(id, principal_issuer, principal_subject, run_id, conversation_id, status) "
                    "VALUES (:id, 'https://aura.test', 'personal-first-owner', "
                    ":run, :conversation, 'completed')"
                ),
                {"id": job, "run": run, "conversation": conversation},
            )
            connection.execute(
                text(
                    "INSERT INTO memory_candidates "
                    "(id, job_id, principal_issuer, principal_subject, action, confidence, "
                    "state, decision_reason) "
                    "VALUES (:id, :job, 'https://aura.test', 'personal-first-owner', 'review', "
                    "0.8, 'review', 'invalid_provider_output')"
                ),
                {"id": candidate, "job": job},
            )

        with engine.begin() as connection:
            _run_upgrade(module, connection)

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": _GENERAL_AGENT},
            ).one() == (_SUCCESSOR_REVISION, 2)
            successor = connection.execute(
                text(
                    "SELECT shared_user_read, current_agent_read, fallback_relevance_threshold, "
                    "max_memories, context_budget_fraction, allow_shared_user_promotion, "
                    "recall_mode, automatic_recall_threshold "
                    "FROM memory_policy_revisions WHERE id = :id"
                ),
                {"id": _SUCCESSOR_POLICY},
            ).one()
            assert tuple(successor) == (False, True, 0.83, 4, 0.11, True, "off", 0.91)
            assert connection.execute(
                text(
                    "SELECT foreign_agent_profile_id FROM memory_policy_fallback_grants "
                    "WHERE policy_id = :id"
                ),
                {"id": _SUCCESSOR_POLICY},
            ).scalar_one() == foreign_profile
            assert connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": custom_profile},
            ).one() == (custom_revision, 1)
            assert connection.execute(
                text("SELECT agent_revision_id FROM conversations WHERE id = :id"),
                {"id": conversation},
            ).scalar_one() == built_in_revision
            assert connection.execute(
                text("SELECT agent_revision_id FROM runs WHERE id = :id"),
                {"id": run},
            ).scalar_one() == built_in_revision
            candidate_state = connection.execute(
                text(
                    "SELECT state, decided_at, retention_basis "
                    "FROM memory_candidates WHERE id = :id"
                ),
                {"id": candidate},
            ).one()
            assert candidate_state[0] == "rejected"
            assert candidate_state[1] is not None
            assert candidate_state[2] == "none"
            before_replay = connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": _GENERAL_AGENT},
            ).one()
            _run_upgrade(module, connection)
            after_replay = connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": _GENERAL_AGENT},
            ).one()
            assert after_replay == before_replay
            assert connection.execute(
                text("SELECT COUNT(*) FROM agent_revisions WHERE id = :id"),
                {"id": _SUCCESSOR_REVISION},
            ).scalar_one() == 1
    finally:
        engine.dispose()
