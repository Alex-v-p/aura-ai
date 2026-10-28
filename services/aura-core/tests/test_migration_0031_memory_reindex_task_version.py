"""Static guardrails for the source-owned memory lifecycle migration."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0031_memory_reindex_task_version.py"
PREVIOUS = ROOT / "migrations" / "versions" / "0030_task_projection.py"


def test_memory_lifecycle_version_is_reversible_and_owned_by_knowledge_memory() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    previous = PREVIOUS.read_text(encoding="utf-8")
    assert 'revision = "0031_memory_reindex_task_version"' in source
    assert 'down_revision = "0030_task_projection"' in source
    assert "ALTER TABLE memory_embedding_generations" in source
    assert "ADD COLUMN IF NOT EXISTS task_projection_version" in source
    assert "ck_memory_embedding_generations_task_projection_version" in source
    mapping = (
        ROOT
        / "src"
        / "aura_core"
        / "domains"
        / "knowledge"
        / "memory"
        / "persistence.py"
    ).read_text(encoding="utf-8")
    assert "CheckConstraint" in mapping
    assert "task_projection_version > 0" in mapping
    assert "DROP COLUMN IF EXISTS task_projection_version" in source
    assert "memory_embedding_generations" not in previous
    assert "task_source_invalidations" not in previous
    assert "task_source_invalidation_checkpoints" not in previous


def test_live_upgrade_rejects_non_positive_versions_and_downgrades() -> None:
    url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not url or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip(
            "AURA_TEST_DATABASE_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 are required "
            "for PostgreSQL migration coverage"
        )
    spec = importlib.util.spec_from_file_location("aura_migration_0031", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"aura0143_{uuid4().hex[:12]}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.execute(
                text(
                    "CREATE TABLE memory_embedding_generations "
                    "(id UUID PRIMARY KEY)"
                )
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.execute(
                text(
                    "INSERT INTO memory_embedding_generations (id, task_projection_version) "
                    "VALUES (:id, 1)"
                ),
                {"id": uuid4()},
            )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO memory_embedding_generations "
                            "(id, task_projection_version) VALUES (:id, 0)"
                        ),
                        {"id": uuid4()},
                    )
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = :schema "
                    "AND table_name = 'memory_embedding_generations' "
                    "AND column_name = 'task_projection_version'"
                ),
                {"schema": schema},
            ).scalar_one_or_none() is None
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
