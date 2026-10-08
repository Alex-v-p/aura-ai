"""Migration-shape and opt-in PostgreSQL checks for AURA-0038 processing state."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from aura_core.domains.knowledge.memory.public import (
    MemoryKind,
    MemoryNotFound,
    MemoryScope,
    MemoryScopeType,
)
from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"


def _migration() -> tuple[Path, Any]:
    path = MIGRATION_DIR / "0009_memory_processing.py"
    spec = importlib.util.spec_from_file_location("aura_migration_0009", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _upgrade(url: str, revision: str) -> None:
    # Importing memory mappings before Alembic registers pgvector columns in
    # Core metadata. Bootstrap the extension for every disposable migration
    # database before any revision runs; SQL failures must propagate.
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


def _database_unavailable(exc: BaseException) -> bool:
    """Return true only for infrastructure/driver failures, not SQL errors."""

    if isinstance(exc, (ConnectionError, TimeoutError, OSError, ImportError)):
        return True
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "could not connect",
            "connection timed out",
            "timeout expired",
            "could not translate host name",
            "name or service not known",
            "no such file or directory",
            "database system is starting up",
            "database system is not yet accepting connections",
            "driver not loaded",
        )
    )


def test_processing_migration_is_forward_only_and_owner_scoped() -> None:
    path, migration = _migration()
    assert migration.revision == "0009_memory_processing"
    assert migration.down_revision == "0008_memory_foundation"
    source = path.read_text()
    for table in (
        "memory_model_configurations",
        "memory_processing_jobs",
        "memory_candidates",
        "memory_action_outcomes",
        "memory_embedding_jobs",
        "memory_maintenance_state",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    assert "UNIQUE (principal_issuer, principal_subject, run_id)" in source
    assert "UNIQUE (revision_id, generation_id)" in source
    assert "downgrade is disabled" in source or "raise RuntimeError" in source
    assert "DROP TABLE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_processing_migration_has_bounded_retry_review_and_generation_states() -> None:
    source = _migration()[0].read_text()
    for value in ("queued", "running", "completed", "retryable", "failed"):
        assert value in source
    for value in ("proposed", "accepted", "rejected", "review", "retryable"):
        assert value in source
    for value in (
        "created",
        "reinforced",
        "disputed",
        "superseded",
        "review",
        "ignored",
        "retryable",
        "failed",
    ):
        assert value in source
    assert "attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)" in source
    assert "available_at TIMESTAMPTZ NOT NULL" in source


def test_fresh_processing_schema_has_no_content_in_maintenance_or_outcome_rows() -> None:
    source = _migration()[0].read_text()
    # Candidate evidence is content-bearing, but operational rows and reindex
    # state must remain identifier/metadata only.
    maintenance_start = source.index("CREATE TABLE IF NOT EXISTS memory_maintenance_state")
    maintenance_sql = source[maintenance_start : source.index("CREATE INDEX", maintenance_start)]
    outcome_start = source.index("CREATE TABLE IF NOT EXISTS memory_action_outcomes")
    outcome_sql = source[outcome_start : source.index("CREATE TABLE", outcome_start + 10)]
    assert "content" not in maintenance_sql
    assert "vector" not in maintenance_sql
    assert "content" not in outcome_sql
    assert "vector" not in outcome_sql


def test_generation_owner_key_replaces_legacy_fk_safely() -> None:
    source = _migration()[0].read_text()
    drop_fk = source.index(
        "ALTER TABLE memory_embeddings DROP CONSTRAINT IF EXISTS memory_embeddings_generation_fkey"
    )
    drop_key = source.index(
        "ALTER TABLE memory_embedding_generations DROP CONSTRAINT IF EXISTS "
        "memory_embedding_generations_generation_key"
    )
    assert drop_fk < drop_key
    assert "ADD COLUMN IF NOT EXISTS principal_issuer" in source
    assert "memory_embeddings_generation_owner_fkey" in source


def test_upgrade_from_memory_foundation_reaches_processing_head_when_database_is_available() -> (
    None
):
    url = os.environ.get("AURA_0038_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip(
            "AURA_0038_MIGRATION_DATABASE_URL is required for PostgreSQL migration coverage"
        )
    try:
        _upgrade(url, "0008_memory_foundation")
        _upgrade(url, "head")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0009_memory_processing"
            )
            tables = set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )
            assert {
                "memory_processing_jobs",
                "memory_candidates",
                "memory_embedding_jobs",
                "memory_maintenance_state",
            } <= tables
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'memory_embeddings'::regclass"
                    )
                ).scalars()
            )
            assert "memory_embeddings_generation_owner_fkey" in constraints
            assert "memory_embeddings_generation_fkey" not in constraints
    finally:
        engine.dispose()


def test_fresh_install_reaches_processing_head_when_database_is_available() -> None:
    url = os.environ.get("AURA_0038_MIGRATION_FRESH_DATABASE_URL")
    if not url:
        pytest.skip(
            "AURA_0038_MIGRATION_FRESH_DATABASE_URL is required for PostgreSQL migration coverage"
        )
    try:
        _upgrade(url, "head")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0009_memory_processing"
            )
            assert connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'memory_embeddings' "
                    "AND column_name = 'principal_issuer'"
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_migrated_schema_accepts_owner_scoped_vector_attachment() -> None:
    """Exercise the composite generation FK after a real Alembic upgrade."""

    url = os.environ.get("AURA_0038_LIVE_ATTACH_DATABASE_URL")
    if not url:
        pytest.skip(
            "AURA_0038_LIVE_ATTACH_DATABASE_URL is required for live migrated attach coverage"
        )
    try:
        _upgrade(url, "head")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise

    engine = make_engine(url)
    try:
        store = SqlMemoryRepository(session_factory(engine))
        generation = await store.register_embedding_generation(
            "https://issuer.example",
            "owner",
            generation=1,
            model_id="migrated-embedder",
            model_revision="rev-1",
            dimension=3,
            model_digest="a" * 64,
        )
        memory = await store.create_memory(
            "https://issuer.example",
            "owner",
            content="A fact created on the migrated schema.",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
            idempotency_key="migrated-attach-memory",
        )
        attached = await store.attach_embedding(
            "https://issuer.example",
            "owner",
            memory.id,
            revision_id=memory.current_revision_id,
            generation_id=generation.id,
            vector=(0.1, 0.2, 0.3),
            digest="b" * 64,
            scope_type=MemoryScopeType.USER,
        )
        assert len(attached.embeddings) == 1
        assert attached.embeddings[0].generation_id == generation.id
        assert attached.embeddings[0].vector == (0.1, 0.2, 0.3)
        with pytest.raises(MemoryNotFound):
            await store.attach_embedding(
                "https://issuer.example",
                "other-owner",
                memory.id,
                revision_id=memory.current_revision_id,
                generation_id=generation.id,
                vector=(0.1, 0.2, 0.3),
                digest="c" * 64,
                scope_type=MemoryScopeType.USER,
            )
    finally:
        await engine.dispose()
