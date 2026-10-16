"""Opt-in real PostgreSQL fresh/upgrade/rerun coverage for AURA-0086."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from aura_observatory.adapters.postgres import PostgresSyncCatalogueAdapter
from aura_observatory.entrypoints.cli import load_component_manifests

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _quoted_identifier(value: str) -> str:
    """Quote a test-generated schema name; never interpolate user input."""

    return '"' + value.replace('"', '""') + '"'


def _schema_name() -> str:
    return f"aura0086_{uuid4().hex[:12]}"


def _set_schema(connection: Any, schema: str) -> None:
    connection.execute(f"SET search_path TO {_quoted_identifier(schema)}")


def test_postgres_catalogue_fresh_upgrade_rerun_and_nullable_sync() -> None:
    database_url = os.environ.get("AURA_AURA0086_POSTGRES_URL")
    if not database_url:
        pytest.skip("set AURA_AURA0086_POSTGRES_URL for real PostgreSQL coverage")

    connection: Any = psycopg.connect(database_url)
    with connection:
        fresh_schema = _schema_name()
        connection.execute(f"CREATE SCHEMA {_quoted_identifier(fresh_schema)}")
        try:
            _set_schema(connection, fresh_schema)
            migrations = sorted(MIGRATION_ROOT.glob("*.sql"))
            for _ in range(2):
                for path in migrations:
                    connection.execute(path.read_text(encoding="utf-8"))
                connection.commit()

            definitions = load_component_manifests()
            assert PostgresSyncCatalogueAdapter(connection).sync_component_definitions(
                definitions
            ) == len(definitions)
            connection.commit()
            before_row = connection.execute(
                "SELECT count(*) FROM observatory_component_definitions"
            ).fetchone()
            assert before_row is not None
            before = int(before_row[0])
            PostgresSyncCatalogueAdapter(connection).sync_component_definitions(definitions)
            connection.commit()
            after_row = connection.execute(
                "SELECT count(*) FROM observatory_component_definitions"
            ).fetchone()
            assert after_row is not None
            after = int(after_row[0])
            assert after == before
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f"DROP SCHEMA {_quoted_identifier(fresh_schema)} CASCADE")
            connection.commit()

        upgrade_schema = _schema_name()
        connection.execute(f"CREATE SCHEMA {_quoted_identifier(upgrade_schema)}")
        try:
            _set_schema(connection, upgrade_schema)
            for path in sorted(MIGRATION_ROOT.glob("*.sql"))[:-1]:
                connection.execute(path.read_text(encoding="utf-8"))
            # Reproduce the pre-0006 dependency key, where required_version
            # was part of a NOT NULL composite primary key.
            connection.execute(
                """CREATE TABLE observatory_component_definitions(
                    component_id VARCHAR(96) NOT NULL,
                    component_version VARCHAR(128) NOT NULL,
                    owner VARCHAR(128) NOT NULL,
                    category VARCHAR(128) NOT NULL,
                    input_schema_id VARCHAR(128) NOT NULL,
                    output_schema_id VARCHAR(128) NOT NULL,
                    measurements VARCHAR[] NOT NULL DEFAULT '{}',
                    evaluators VARCHAR[] NOT NULL DEFAULT '{}',
                    manifest_hash CHAR(64) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component_id, component_version)
                )"""
            )
            connection.execute(
                """CREATE TABLE observatory_component_dependencies(
                    source_component_id VARCHAR(96) NOT NULL,
                    source_component_version VARCHAR(128) NOT NULL,
                    dependency_id VARCHAR(128) NOT NULL,
                    dependency_kind VARCHAR(32) NOT NULL,
                    required_version VARCHAR(128) NOT NULL,
                    PRIMARY KEY(
                        source_component_id, source_component_version,
                        dependency_id, dependency_kind, required_version
                    )
                )"""
            )
            migration = (MIGRATION_ROOT / "0006_component_definitions.sql").read_text(
                encoding="utf-8"
            )
            connection.execute(migration)
            connection.execute(migration)
            connection.execute(
                """INSERT INTO observatory_component_dependencies(
                    source_component_id, source_component_version, dependency_id,
                    dependency_kind, required_version
                ) VALUES (%s, %s, %s, 'component', NULL),
                         (%s, %s, %s, 'component', %s)""",
                (
                    "aura.test.source",
                    "1.0.0",
                    "aura.observatory.query_api",
                    "aura.test.source",
                    "1.0.0",
                    "aura.observatory.query_api",
                    "1.0.0",
                ),
            )
            connection.commit()
            rows = connection.execute(
                """SELECT required_version FROM observatory_component_dependencies
                WHERE source_component_id = %s ORDER BY required_version NULLS FIRST""",
                ("aura.test.source",),
            ).fetchall()
            assert [row[0] for row in rows] == [None, "1.0.0"]
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f"DROP SCHEMA {_quoted_identifier(upgrade_schema)} CASCADE")
            connection.commit()
