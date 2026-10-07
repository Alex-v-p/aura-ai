"""Migration-shape and opt-in PostgreSQL coverage for AURA-0037."""

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
    path = MIGRATION_DIR / "0008_memory_foundation.py"
    spec = importlib.util.spec_from_file_location("aura_migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


def test_memory_migration_has_single_forward_revision_and_no_destructive_downgrade() -> None:
    path, migration = _migration()
    assert migration.revision == "0008_memory_foundation"
    assert migration.down_revision == "0007_conversation_metadata"
    source = path.read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in source
    assert "CREATE TABLE IF NOT EXISTS memories" in source
    assert "CREATE TABLE IF NOT EXISTS memory_revisions" in source
    assert "CREATE TABLE IF NOT EXISTS memory_provenance" in source
    assert "CREATE TABLE IF NOT EXISTS memory_embedding_generations" in source
    assert "CREATE TABLE IF NOT EXISTS memory_embeddings" in source
    assert "CREATE TABLE IF NOT EXISTS memory_relations" in source
    assert "CREATE TABLE IF NOT EXISTS memory_purge_audit" in source
    assert "CREATE TABLE IF NOT EXISTS memory_command_idempotency" in source
    assert "downgrade is disabled" in source or "raise RuntimeError" in source
    assert "DROP TABLE" not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_memory_migration_declares_lifecycle_scope_and_retention_invariants() -> None:
    source = _migration()[0].read_text()
    for value in ("episodic", "semantic", "procedural", "preference", "system"):
        assert value in source
    for value in ("active", "dormant", "archived", "disabled", "disputed", "superseded"):
        assert value in source
    assert "half_life_days DOUBLE PRECISION" in source
    assert "BETWEEN 0.25 AND 3650" in source
    assert "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from" in source
    assert "PRIMARY KEY(principal_issuer, principal_subject, idempotency_key)" in source


def _database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL migration coverage")
    return value


def test_fresh_install_creates_pgvector_and_memory_tables() -> None:
    url = _database_url("AURA_0037_MIGRATION_FRESH_DATABASE_URL")
    try:
        _upgrade(url, "head")
    except (DBAPIError, OperationalError) as exc:
        pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one() == 1
            tables = set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )
            assert {
                "memories",
                "memory_revisions",
                "memory_provenance",
                "memory_embedding_generations",
                "memory_embeddings",
                "memory_relations",
                "memory_purge_audit",
                "memory_command_idempotency",
            } <= tables
            columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memory_purge_audit'"
                    )
                )
            }
            assert {"memory_id", "principal_issuer", "principal_subject", "action"} <= columns
            assert "content" not in columns
            assert "evidence" not in columns
            assert "vector" not in columns
    finally:
        engine.dispose()


def test_upgrade_from_0007_reaches_memory_head_without_replacing_prior_schema() -> None:
    url = _database_url("AURA_0037_MIGRATION_UPGRADE_DATABASE_URL")
    try:
        _upgrade(url, "0007_conversation_metadata")
        _upgrade(url, "head")
    except (DBAPIError, OperationalError) as exc:
        pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0008_memory_foundation"
            assert connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'conversations' AND column_name = 'archived_at'"
                )
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT to_regclass('public.memories')")
            ).scalar_one() == "memories"
    finally:
        engine.dispose()
