"""Opt-in live PostgreSQL coverage for AURA-0093 definition state.

The test uses one generated schema and two independent connections.  It is
skipped unless the repository-scoped ``AURA_AURA0093_POSTGRES_URL`` is set;
the URL is never printed or persisted.
"""
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportCallIssue=false

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from aura_observatory.adapters.evaluation_definitions import (
    PostgresEvaluationDefinitionRepository,
)
from aura_observatory.evaluation_definitions import (
    CaseDefinition,
    DatasetDefinition,
    DefinitionConflictError,
    DefinitionValidationError,
    EvaluationDefinitionService,
)
from test_aura0093_definitions import _case, _dataset

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _database_url() -> str:
    value = os.environ.get("AURA_AURA0093_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0093_POSTGRES_URL for live AURA-0093 coverage")
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _isolated_schema(database_url: str):
    connection = psycopg.connect(database_url)
    schema = f"aura0093_{uuid4().hex[:12]}"
    quoted = _quote_identifier(schema)
    try:
        connection.execute(f"CREATE SCHEMA {quoted}")
        connection.execute(f"SET search_path TO {quoted}")
        migrations = sorted(MIGRATION_ROOT.glob("*.sql"))
        for migration in migrations:
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()
        # A forward-only rerun of the work item's migration must be safe.
        migration = (MIGRATION_ROOT / "0009_evaluation_definitions.sql").read_text(
            encoding="utf-8"
        )
        connection.execute(migration)
        connection.commit()
        yield schema
    finally:
        connection.rollback()
        connection.execute("SET search_path TO public")
        connection.execute(f"DROP SCHEMA {quoted} CASCADE")
        connection.commit()
        connection.close()


@pytest.mark.asyncio
async def test_postgres_definition_insert_is_atomic_idempotent_and_audited() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as schema:

        @asynccontextmanager
        async def connection_factory():
            connection: Any = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote_identifier(schema)}")
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                await connection.close()

        repository = PostgresEvaluationDefinitionRepository(connection_factory)
        definition = DatasetDefinition.from_payload(_dataset())
        results = await asyncio.gather(
            repository.create_dataset(definition), repository.create_dataset(definition)
        )
        assert results[0] == definition
        assert results[1] == definition

        conflicting_payload = _dataset()
        conflicting_payload["caseIds"] = ["00000000-0000-0000-0000-000000000009"]
        with pytest.raises(DefinitionConflictError):
            await repository.create_dataset(DatasetDefinition.from_payload(conflicting_payload))

        async with connection_factory() as connection:
            row = await connection.execute(
                "SELECT count(*) FROM observatory_evaluation_datasets"
            )
            count = await row.fetchone()
            assert count == (1,)
            audits = await connection.execute(
                "SELECT operation, outcome FROM observatory_evaluation_definition_audits "
                "ORDER BY occurred_at, audit_id"
            )
            assert set(await audits.fetchall()) >= {
                ("create", "accepted"),
                ("idempotent_reuse", "duplicate"),
                ("conflict", "rejected"),
            }


@pytest.mark.asyncio
async def test_postgres_admits_first_declared_case_before_membership_exists() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as schema:

        @asynccontextmanager
        async def connection_factory():
            connection: Any = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote_identifier(schema)}")
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                await connection.close()

        repository = PostgresEvaluationDefinitionRepository(connection_factory)
        service = EvaluationDefinitionService(repository)
        dataset = DatasetDefinition.from_payload(_dataset())
        await repository.create_dataset(dataset)

        async with connection_factory() as connection:
            membership = await connection.execute(
                "SELECT count(*) FROM observatory_evaluation_dataset_cases"
            )
            assert await membership.fetchone() == (0,)

        case_payload = _case()
        undeclared_payload = dict(case_payload)
        undeclared_payload["caseId"] = "00000000-0000-0000-0000-000000000009"
        with pytest.raises(DefinitionValidationError):
            await service.admit_case(undeclared_payload)

        wrong_dataset_payload = dict(case_payload)
        wrong_dataset_payload["dataset"] = {
            "id": "00000000-0000-0000-0000-000000000009",
            "version": "1.0.0",
        }
        with pytest.raises(DefinitionValidationError):
            await service.admit_case(wrong_dataset_payload)

        wrong_version_payload = dict(case_payload)
        wrong_version_payload["dataset"] = {
            "id": dataset.resource_id,
            "version": "2.0.0",
        }
        with pytest.raises(DefinitionValidationError):
            await service.admit_case(wrong_version_payload)

        admitted = await service.admit_case(case_payload)
        assert admitted == CaseDefinition.from_payload(case_payload)
        assert await service.admit_case(case_payload) == admitted

        conflicting_payload = dict(case_payload)
        conflicting_payload["caseVersion"] = "2.0.0"
        with pytest.raises(DefinitionConflictError):
            await service.admit_case(conflicting_payload)

        async with connection_factory() as connection:
            stored_cases = await connection.execute(
                "SELECT case_id, case_version, dataset_id, dataset_version "
                "FROM observatory_evaluation_cases"
            )
            assert await stored_cases.fetchall() == [
                (
                    dataset.case_ids[0],
                    "1.0.0",
                    dataset.resource_id,
                    dataset.version,
                )
            ]
            stored_membership = await connection.execute(
                "SELECT case_id, case_version, dataset_id, dataset_version "
                "FROM observatory_evaluation_dataset_cases"
            )
            assert await stored_membership.fetchall() == [
                (
                    dataset.case_ids[0],
                    "1.0.0",
                    dataset.resource_id,
                    dataset.version,
                )
            ]
