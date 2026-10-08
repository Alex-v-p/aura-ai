"""Migration-shape and opt-in PostgreSQL checks for AURA-0039 policy state."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"


def _migration() -> tuple[Path, Any]:
    path = MIGRATION_DIR / "0010_memory_recall_policies.py"
    spec = importlib.util.spec_from_file_location("aura_migration_0010", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _upgrade(url: str, revision: str = "head") -> None:
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


def test_recall_migration_is_forward_only_and_declares_immutable_policy_state() -> None:
    path, migration = _migration()
    assert migration.revision == "0010_memory_recall_policies"
    assert migration.down_revision == "0009_memory_processing"
    source = path.read_text()
    assert "CREATE TABLE IF NOT EXISTS memory_policy_revisions" in source
    assert "CREATE TABLE IF NOT EXISTS memory_policy_fallback_grants" in source
    assert "memory_policy_revision_id UUID" in source
    assert "memory_embeddings ADD COLUMN IF NOT EXISTS model_digest" in source
    assert "memory_policy_revision_id UUID" in source
    assert "allow_shared_user_promotion BOOLEAN" in source
    assert "FOREIGN KEY (memory_policy_revision_id)" in source
    assert "UNIQUE (principal_issuer, principal_subject, agent_profile_id, revision)" in source
    assert "downgrade is disabled" in source or "raise RuntimeError" in source
    assert "DROP TABLE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_recall_migration_bounds_policy_and_grant_values() -> None:
    source = _migration()[0].read_text()
    assert "fallback_relevance_threshold BETWEEN 0 AND 1" in source
    assert "max_memories BETWEEN 1 AND 8" in source
    assert "context_budget_fraction > 0 AND context_budget_fraction <= 0.2" in source
    assert "CHECK (foreign_agent_profile_id IS NOT NULL)" in source
    assert "INSERT INTO memory_policy_revisions" in source
    assert "TRUE, TRUE, 0.5, 8, 0.2, FALSE, CURRENT_TIMESTAMP" in source
    assert "UPDATE agent_revisions SET memory_policy_revision_id" in source
    assert "agent-memory-policy:" in source
    assert (
        "ON CONFLICT (principal_issuer, principal_subject, agent_profile_id, revision)"
        in source
    )
    assert "RETURNING id" in source


@pytest.mark.parametrize(
    "environment_name",
    ("AURA_0039_MIGRATION_DATABASE_URL", "AURA_0039_MIGRATION_FRESH_DATABASE_URL"),
)
def test_recall_migration_reaches_head_when_postgres_is_available(environment_name: str) -> None:
    url = os.environ.get(environment_name)
    if not url:
        pytest.skip(f"{environment_name} is required for PostgreSQL migration coverage")
    try:
        _upgrade(url)
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0010_memory_recall_policies"
            )
            tables = set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )
            assert {"memory_policy_revisions", "memory_policy_fallback_grants"} <= tables
            columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_revisions'"
                    )
                )
            }
            assert "memory_policy_revision_id" in columns
            embedding_columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memory_embeddings'"
                    )
                )
            }
            assert "model_digest" in embedding_columns
            job_columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memory_processing_jobs'"
                    )
                )
            }
            assert {
                "memory_policy_revision_id",
                "allow_shared_user_promotion",
            } <= job_columns
            nullability = connection.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'agent_revisions' "
                    "AND column_name = 'memory_policy_revision_id'"
                )
            ).scalar_one()
            assert nullability == "NO"
            policy_id = str(_migration()[1]._GENERAL_POLICY)
            seeded = connection.execute(
                text(
                    "SELECT shared_user_read, current_agent_read, "
                    "fallback_relevance_threshold, max_memories, "
                    "context_budget_fraction, allow_shared_user_promotion "
                    "FROM memory_policy_revisions WHERE id = :id"
                ),
                {"id": policy_id},
            ).one()
            assert tuple(seeded) == (True, True, 0.5, 8, 0.2, False)
    finally:
        engine.dispose()
