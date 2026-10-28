"""Migration guardrails and isolated PostgreSQL round-trip coverage."""

import importlib.util
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0030_task_projection.py"
ARTIFACT_MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0029_generated_artifacts.py"
)


def _load_migration(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_projection_migration_has_single_head_and_no_cross_domain_foreign_keys() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    sql_repository = (
        MIGRATION.parents[2]
        / "src"
        / "aura_core"
        / "domains"
        / "execution"
        / "tasks"
        / "sql_repository.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0030_task_projection"' in source
    assert 'down_revision = "0029_generated_artifacts"' in source
    assert "CREATE TABLE IF NOT EXISTS task_projection" in source
    assert "FOREIGN KEY (" not in source.upper()
    assert "CASCADE" not in source.upper()
    assert "source_version" in source
    assert "task_projection_rebuilds" in source
    assert "task_projection_reconciliation_queue" in source
    assert "task_projection_events" in source
    assert "CAST(:seen AS jsonb)" in sql_repository


def test_live_ordered_0029_0030_upgrade_restart_and_downgrade_round_trip() -> None:
    """Run both Core revisions repeatedly on an isolated PostgreSQL schema."""

    url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not url or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip(
            "AURA_TEST_DATABASE_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 are required "
            "for ordered migration coverage"
        )

    artifacts = _load_migration(ARTIFACT_MIGRATION, "aura_migration_0029_live")
    projection = _load_migration(MIGRATION, "aura_migration_0030_live")
    assert artifacts.revision == "0029_generated_artifacts"
    assert projection.down_revision == artifacts.revision

    schema = f"aura0143_{uuid4().hex[:12]}"
    engine = create_engine(url)
    expected_tables = {
        "generated_artifacts",
        "generated_artifact_revisions",
        "generated_artifact_idempotency",
        "generated_artifact_lifecycle_events",
        "generated_artifact_tombstones",
        "generated_artifact_cleanup_work",
        "generated_artifact_append_reservations",
        "task_projection",
        "task_projection_rebuilds",
        "task_projection_command_receipts",
        "task_projection_reconciliation_queue",
        "task_projection_events",
    }
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            with Operations.context(MigrationContext.configure(connection)):
                artifacts.upgrade()
                projection.upgrade()
                # A process restart can resume a partially applied additive
                # migration. Both revisions are intentionally idempotent.
                artifacts.upgrade()
                projection.upgrade()
            actual_tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": schema},
                ).scalars()
            )
            assert expected_tables <= actual_tables
            with Operations.context(MigrationContext.configure(connection)):
                projection.downgrade()
                artifacts.downgrade()
            remaining = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": schema},
                ).scalars()
            )
            assert not expected_tables & remaining
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
