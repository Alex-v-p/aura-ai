"""Opt-in PostgreSQL coverage for the AURA-0095 durable live-event ledger."""
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportCallIssue=false

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from aura_observatory.adapters.evaluation_definitions import PostgresEvaluationDefinitionRepository
from aura_observatory.adapters.evaluation_live import PostgresEvaluationLiveEventStore
from aura_observatory.adapters.evaluation_query import PostgresEvaluationQueryBackend
from aura_observatory.adapters.evaluation_storage import PostgresEvaluationArtifactRepository
from aura_observatory.evaluation_definitions import (
    DefinitionCommandPendingError,
    DefinitionConflictError,
)

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _database_url() -> str:
    value = os.environ.get("AURA_AURA0095_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0095_POSTGRES_URL for live AURA-0095 coverage")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _isolated_schema(
    database_url: str,
    *,
    include_live_events: bool = True,
) -> Generator[tuple[psycopg.Connection[Any], str]]:
    connection = psycopg.connect(database_url)
    schema = f"aura0095_{uuid4().hex[:12]}"
    quoted = _quote(schema)
    primary_error: Exception | None = None
    try:
        connection.execute(f"CREATE SCHEMA {quoted}")
        connection.execute(f"SET search_path TO {quoted}")
        migrations = sorted(MIGRATION_ROOT.glob("*.sql"))
        if not include_live_events:
            migrations = [
                path for path in migrations if path.name != "0011_evaluation_live_events.sql"
            ]
        for migration in migrations:
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()
        # The newest migration must also be safe when an upgraded deployment
        # reruns it independently of the complete fresh migration set.
        if include_live_events:
            latest = MIGRATION_ROOT / "0011_evaluation_live_events.sql"
            connection.execute(latest.read_text(encoding="utf-8"))
            connection.commit()
        yield connection, schema
    except Exception as error:
        primary_error = error
        raise
    finally:
        cleanup_error: Exception | None = None
        try:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(f"DROP SCHEMA {quoted} CASCADE")
            connection.commit()
        except Exception as error:
            cleanup_error = RuntimeError("AURA-0095 isolated PostgreSQL cleanup failed")
            cleanup_error.__cause__ = error
        finally:
            connection.close()
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def test_live_event_migration_inventory_is_contiguous_and_forward_only() -> None:
    versions = sorted(int(path.name[:4]) for path in MIGRATION_ROOT.glob("*.sql"))
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == 11
    migration = (MIGRATION_ROOT / "0011_evaluation_live_events.sql").read_text(
        encoding="utf-8"
    )
    normalized = migration.casefold()
    assert normalized.count("create table if not exists") == 4
    assert normalized.count("create index if not exists") == 3
    assert "observatory_evaluation_live_events" in normalized
    assert "observatory_evaluation_qualification_revocations" in normalized
    assert "observatory_evaluation_cancellation_commands" in normalized
    assert "observatory_evaluation_deletion_audits_completed_idx" in normalized
    assert "observatory_evaluation_definition_commands" in normalized
    assert "DROP TABLE" not in migration.upper()
    assert "DROP COLUMN" not in migration.upper()
    assert "ALTER TABLE" in migration.upper()
    assert "credential" not in migration.casefold()
    assert "owner_identity" not in migration.casefold()


def test_postgres_0011_fresh_upgrade_rerun_constraints() -> None:
    with _isolated_schema(_database_url()) as (connection, _schema):
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'observatory_evaluation_live_events'"
        ).fetchall()
        assert {row[0] for row in columns} == {
            "sequence",
            "event_id",
            "resource_type",
            "resource_id",
            "change",
            "state",
            "occurred_at",
            "capture_policy",
        }
        lineage_columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'observatory_evaluation_deletion_targets'"
        ).fetchall()
        assert {row[0] for row in lineage_columns} >= {
            "artifact_id", "job_id", "dataset_id", "suite_id"
        }
        connection.execute(
            "INSERT INTO observatory_evaluation_live_events "
            "(event_id, resource_type, resource_id, change, state) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                "00000000-0000-4000-8000-000000000201",
                "evaluation_job",
                "00000000-0000-4000-8000-000000000202",
                "created",
                "queued",
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT capture_policy FROM observatory_evaluation_live_events"
        ).fetchone()
        assert row == ("metadata_only",)


def test_postgres_0011_upgrades_an_existing_0010_schema_and_reruns() -> None:
    with _isolated_schema(_database_url(), include_live_events=False) as (connection, _schema):
        migration = (MIGRATION_ROOT / "0011_evaluation_live_events.sql").read_text(
            encoding="utf-8"
        )
        connection.execute(migration)
        connection.execute(migration)
        connection.commit()
        table = connection.execute(
            "SELECT to_regclass('observatory_evaluation_live_events')"
        ).fetchone()
        assert table == ("observatory_evaluation_live_events",)


@pytest.mark.asyncio
async def test_postgres_live_event_store_replays_after_restart() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):

        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        first_store = PostgresEvaluationLiveEventStore(connection_context)
        first = await first_store.publish(
            "evaluation_job",
            "00000000-0000-4000-8000-000000000203",
            "created",
            "queued",
        )
        restarted_store = PostgresEvaluationLiveEventStore(connection_context)
        replay = await restarted_store.after(first.event_id)
        assert replay == ()
        all_events = await restarted_store.after(None)
        assert len(all_events) == 1
        assert all_events[0].event_id == first.event_id
        assert all_events[0].as_sse()["capturePolicy"] == "metadata_only"


@pytest.mark.asyncio
async def test_populated_admitted_job_query_uses_persisted_runner_provenance() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        job_id = uuid4()
        request_id = uuid4()
        idempotency_key = uuid4()
        dataset_id = uuid4()
        suite_id = uuid4()
        evaluator_id = uuid4()
        now = datetime.now(UTC)
        provenance = {
            "dataset": {"id": str(dataset_id), "version": "1.0.0"},
            "suite": {"id": str(suite_id), "version": "1.0.0"},
            "evaluators": [
                {
                    "evaluatorId": str(evaluator_id),
                    "evaluatorVersion": "1.0.0",
                    "kind": "deterministic",
                }
            ],
            "components": [
                {
                    "componentId": "aura.core",
                    "componentVersion": "1.0.0",
                    "codeRevision": "abc123",
                    "imageDigest": "a" * 64,
                    "configurationRevision": "cfg1",
                }
            ],
            "model": {"modelId": "model.test", "modelVersion": "1.0.0", "modelDigest": "b" * 64},
            "deploymentRevision": "deploy1",
            "reproducibility": {
                "seed": 7,
                "clockMode": "fixed",
                "timeZone": "UTC",
                "locale": "en-US",
                "fixtureRevision": "fixture1",
                "promptRevision": "prompt1",
                "policyRevision": "policy1",
            },
        }
        envelope = {"data": {"provenance": provenance}}
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            connection.execute(
                """INSERT INTO observatory_evaluation_jobs(
                job_id, request_id, idempotency_key, request_fingerprint,
                request_version, dataset_id, dataset_version, suite_id, suite_version,
                evaluator_id, evaluator_version, capture_classification, deadline_at,
                max_attempts, envelope_json)
                VALUES (%s, %s, %s, %s, '1.0.0', %s, '1.0.0', %s, '1.0.0',
                %s, '1.0.0', 'metadata_only', %s, 1, %s::jsonb)""",
                (
                    job_id, request_id, idempotency_key, "c" * 64,
                    dataset_id, suite_id, evaluator_id, now + timedelta(hours=1),
                    json.dumps(envelope, sort_keys=True),
                ),
            )
            connection.commit()

        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        backend = PostgresEvaluationQueryBackend(connection_context)
        page = await backend.collection("jobs", cursor=None, limit=10, filters=None)
        detail = await backend.get("jobs", str(job_id), None)
        assert page["items"]
        assert detail is not None
        assert page["items"][0]["provenance"] == detail["provenance"]
        assert page["items"][0]["provenance"]["dataset"]["id"] == str(dataset_id)


@pytest.mark.asyncio
async def test_definition_command_replay_conflict_and_concurrent_completion_are_durable() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        repository = PostgresEvaluationDefinitionRepository(connection_context)
        key = "definition-command-replay-0001"
        operation = "create_dataset"
        fingerprint = "a" * 64
        resource_id = str(uuid4())
        resource_version = "1.0.0"
        claim = await repository.begin_definition_command(
            operation, key, fingerprint, resource_id, resource_version
        )
        assert claim.replay is None
        assert claim.claimed is True
        assert claim.claim_token
        assert claim.claim_phase == "definition_claim"
        response = {
            "schemaVersion": 1,
            "resourceType": "dataset",
            "datasetId": resource_id,
            "datasetVersion": resource_version,
            "caseCount": 1,
            "captureClassification": "metadata_only",
        }
        with pytest.raises(DefinitionCommandPendingError):
            await repository.complete_definition_command(
                operation, key, fingerprint, response, None
            )
        async with connection_context() as connection:
            pending_state = await connection.execute(
                "SELECT status FROM observatory_evaluation_definition_commands "
                "WHERE idempotency_key = %s",
                (key,),
            )
            assert await pending_state.fetchone() == ("pending",)
        assert (
            await repository.complete_definition_command(
                operation, key, fingerprint, response, claim.claim_token
            )
            == response
        )
        restarted = PostgresEvaluationDefinitionRepository(connection_context)
        replay = await restarted.begin_definition_command(
            operation, key, fingerprint, resource_id, resource_version
        )
        assert replay.replay == response
        assert replay.claimed is False
        assert replay.claim_phase == "definition_replay"
        with pytest.raises(DefinitionConflictError):
            await restarted.begin_definition_command(
                operation, key, "b" * 64, resource_id, resource_version
            )

        concurrent_key = "definition-command-concurrent-001"
        concurrent_id = str(uuid4())
        concurrent_claim = await repository.begin_definition_command(
            operation, concurrent_key, "c" * 64, concurrent_id, resource_version
        )
        results = await asyncio.gather(
            repository.complete_definition_command(
                operation,
                concurrent_key,
                "c" * 64,
                response | {"datasetId": concurrent_id},
                concurrent_claim.claim_token,
            ),
            restarted.complete_definition_command(
                operation,
                concurrent_key,
                "c" * 64,
                response | {"datasetId": concurrent_id},
                concurrent_claim.claim_token,
            ),
        )
        assert results[0] == results[1]
        async with connection_context() as connection:
            count_result = await connection.execute(
                "SELECT COUNT(*) FROM observatory_evaluation_definition_commands "
                "WHERE idempotency_key = %s AND status = 'completed'",
                (concurrent_key,),
            )
            assert await count_result.fetchone() == (1,)

        concurrent_begin_key = "definition-command-claim-001"
        concurrent_begin_id = str(uuid4())
        claimed = await asyncio.gather(
            repository.begin_definition_command(
                operation,
                concurrent_begin_key,
                "d" * 64,
                concurrent_begin_id,
                resource_version,
            ),
            restarted.begin_definition_command(
                operation,
                concurrent_begin_key,
                "d" * 64,
                concurrent_begin_id,
                resource_version,
            ),
            return_exceptions=True,
        )
        assert sum(
            isinstance(item, type(claim)) and item.claimed for item in claimed
        ) == 1
        assert sum(isinstance(item, DefinitionCommandPendingError) for item in claimed) == 1

        # Simulate an API crash before admission.  A restarted owner may take
        # over only after the fenced lease expires; the old claimant cannot
        # complete the command or run post-admission side effects.
        stale_key = "definition-command-stale-001"
        stale_id = str(uuid4())
        stale_claim = await repository.begin_definition_command(
            operation, stale_key, "e" * 64, stale_id, resource_version
        )
        assert stale_claim.claim_token
        async with connection_context() as connection:
            await connection.execute(
                "UPDATE observatory_evaluation_definition_commands "
                "SET claim_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                "WHERE idempotency_key = %s",
                (stale_key,),
            )
        original_claim_token = stale_claim.claim_token
        with pytest.raises(DefinitionConflictError):
            await restarted.begin_definition_command(
                operation, stale_key, "e" * 64, str(uuid4()), resource_version
            )
        async with connection_context() as connection:
            claim_state = await connection.execute(
                "SELECT claim_token FROM observatory_evaluation_definition_commands "
                "WHERE idempotency_key = %s",
                (stale_key,),
            )
            assert await claim_state.fetchone() == (original_claim_token,)
        recovered_claim = await restarted.begin_definition_command(
            operation, stale_key, "e" * 64, stale_id, resource_version
        )
        assert recovered_claim.claimed is True
        assert recovered_claim.claim_token != stale_claim.claim_token
        assert recovered_claim.claim_phase == "definition_takeover"
        with pytest.raises(DefinitionCommandPendingError):
            await restarted.complete_definition_command(
                operation, stale_key, "e" * 64,
                response | {"datasetId": stale_id}, None
            )
        async with connection_context() as connection:
            pending_state = await connection.execute(
                "SELECT status FROM observatory_evaluation_definition_commands "
                "WHERE idempotency_key = %s",
                (stale_key,),
            )
            assert await pending_state.fetchone() == ("pending",)
        with pytest.raises(DefinitionCommandPendingError):
            await repository.complete_definition_command(
                operation, stale_key, "e" * 64, response | {"datasetId": stale_id},
                stale_claim.claim_token,
            )
        renewed = await restarted.renew_definition_command(
            operation, stale_key, "e" * 64, recovered_claim.claim_token or ""
        )
        assert renewed is True
        # Simulate a crash after durable admission but before the receipt is
        # acknowledged: completion after restart is still exactly-once.
        assert await restarted.complete_definition_command(
            operation, stale_key, "e" * 64,
            response | {"datasetId": stale_id}, recovered_claim.claim_token,
        ) == response | {"datasetId": stale_id}
        replay_after_receipt = await repository.begin_definition_command(
            operation, stale_key, "e" * 64, stale_id, resource_version
        )
        assert replay_after_receipt.replay == response | {"datasetId": stale_id}


@pytest.mark.asyncio
async def test_attempt_and_deletion_cursors_use_snapshot_keysets() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        now = datetime.now(UTC)
        job_id = uuid4()
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            connection.execute(
                """INSERT INTO observatory_evaluation_jobs(
                job_id, request_id, idempotency_key, request_fingerprint,
                request_version, dataset_id, dataset_version, suite_id, suite_version,
                evaluator_id, evaluator_version, capture_classification, deadline_at,
                max_attempts, envelope_json)
                VALUES (%s, %s, %s, %s, '1.0.0', %s, '1.0.0', %s, '1.0.0',
                %s, '1.0.0', 'metadata_only', %s, 3, '{}'::jsonb)""",
                (job_id, uuid4(), uuid4(), "6" * 64, uuid4(), uuid4(), uuid4(), now),
            )
            for number in range(1, 4):
                connection.execute(
                    """INSERT INTO observatory_evaluation_attempts(
                    attempt_id, job_id, attempt_number, request_id, request_digest,
                    next_attempt_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        uuid4(),
                        job_id,
                        number,
                        uuid4(),
                        "7" * 64,
                        now - timedelta(minutes=5 - number),
                        now - timedelta(minutes=5 - number),
                    ),
                )
            for index in range(1, 4):
                connection.execute(
                    """INSERT INTO observatory_evaluation_deletion_commands(
                    command_id, idempotency_key, selector_kind, selector_value,
                    confirmation, status, requested_at)
                    VALUES (%s, %s, 'job', %s, 'DELETE_EVALUATION_ARTIFACTS', 'queued', %s)""",
                    (uuid4(), uuid4(), str(job_id), now - timedelta(minutes=5 - index)),
                )
            connection.commit()

        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        backend = PostgresEvaluationQueryBackend(connection_context)
        attempts = await backend.collection("attempts", cursor=None, limit=2, filters=None)
        assert [item["attemptNumber"] for item in attempts["items"]] == [1, 2]
        assert attempts["nextCursor"]
        deletions = await backend.collection("deletions", cursor=None, limit=2, filters=None)
        assert len(deletions["items"]) == 2
        assert deletions["nextCursor"]
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            connection.execute(
                """INSERT INTO observatory_evaluation_attempts(
                attempt_id, job_id, attempt_number, request_id, request_digest,
                next_attempt_at, created_at) VALUES (%s, %s, 4, %s, %s, %s, %s)""",
                (
                    uuid4(),
                    job_id,
                    uuid4(),
                    "8" * 64,
                    now + timedelta(minutes=4),
                    now + timedelta(minutes=4),
                ),
            )
            connection.execute(
                """INSERT INTO observatory_evaluation_deletion_commands(
                command_id, idempotency_key, selector_kind, selector_value,
                confirmation, status, requested_at)
                VALUES (%s, %s, 'job', %s, 'DELETE_EVALUATION_ARTIFACTS', 'queued', %s)""",
                (uuid4(), uuid4(), str(job_id), now + timedelta(minutes=4)),
            )
            connection.commit()
        next_attempts = await backend.collection(
            "attempts", cursor=attempts["nextCursor"], limit=2, filters=None
        )
        assert [item["attemptNumber"] for item in next_attempts["items"]] == [3]
        next_deletions = await backend.collection(
            "deletions", cursor=deletions["nextCursor"], limit=2, filters=None
        )
        assert len(next_deletions["items"]) == 1


@pytest.mark.asyncio
async def test_job_retention_removes_child_notifications_and_preserves_audit_boundary() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        job_id = uuid4()
        attempt_id = uuid4()
        result_id = uuid4()
        now = datetime.now(UTC)
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            connection.execute(
                """INSERT INTO observatory_evaluation_jobs(
                job_id, request_id, idempotency_key, request_fingerprint,
                request_version, dataset_id, dataset_version, suite_id, suite_version,
                evaluator_id, evaluator_version, capture_classification, deadline_at,
                max_attempts, envelope_json)
                VALUES (%s, %s, %s, %s, '1.0.0', %s, '1.0.0', %s, '1.0.0',
                %s, '1.0.0', 'metadata_only', %s, 1, '{}'::jsonb)""",
                (
                    job_id, uuid4(), uuid4(), "d" * 64, uuid4(), uuid4(), uuid4(),
                    now + timedelta(hours=1),
                ),
            )
            connection.execute(
                """INSERT INTO observatory_evaluation_attempts(
                attempt_id, job_id, attempt_number, request_id, request_digest,
                next_attempt_at) VALUES (%s, %s, 1, %s, %s, %s)""",
                (attempt_id, job_id, uuid4(), "e" * 64, now),
            )
            connection.execute(
                """INSERT INTO observatory_evaluation_result_receipts(
                result_id, message_id, job_id, attempt_id, request_digest,
                result_digest, status, disposition, envelope_json)
                VALUES (%s, %s, %s, %s, %s, %s, 'completed', 'applied', '{}'::jsonb)""",
                (result_id, uuid4(), job_id, attempt_id, "f" * 64, "a" * 64),
            )
            command_id = uuid4()
            connection.execute(
                """INSERT INTO observatory_evaluation_deletion_commands(
                command_id, idempotency_key, selector_kind, selector_value,
                confirmation, status) VALUES (%s, %s, 'job', %s,
                'DELETE_EVALUATION_ARTIFACTS', 'queued')""",
                (command_id, uuid4(), str(job_id)),
            )
            events = [
                (uuid4(), "evaluation_job", str(job_id), "created", "queued"),
                (uuid4(), "evaluation_attempt", str(attempt_id), "created", "queued"),
                (uuid4(), "evaluation_result", str(result_id), "created", "completed"),
                (uuid4(), "evaluation_job", str(uuid4()), "created", "queued"),
                (uuid4(), "evaluation_deletion", str(command_id), "deletion_requested", "queued"),
            ]
            for event_id, resource_type, resource_id, change, state in events:
                connection.execute(
                    """INSERT INTO observatory_evaluation_live_events(
                    event_id, resource_type, resource_id, change, state)
                    VALUES (%s, %s, %s, %s, %s)""",
                    (event_id, resource_type, resource_id, change, state),
                )
            connection.commit()

        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        repository = PostgresEvaluationArtifactRepository(connection_context)
        assert await repository.complete_deletion_command(str(command_id), 3) == 3
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            remaining = connection.execute(
                "SELECT resource_type, resource_id "
                "FROM observatory_evaluation_live_events ORDER BY sequence"
            ).fetchall()
        assert {(row[0], row[1]) for row in remaining} == {
            ("evaluation_job", str(events[3][2])),
            ("evaluation_deletion", str(command_id)),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_kind", ["dataset", "suite"])
async def test_dataset_and_suite_retention_removes_descendant_notifications_and_retries_once(
    selector_kind: str,
) -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        selected_dataset = uuid4()
        selected_suite = uuid4()
        selected_job = uuid4()
        selected_attempt = uuid4()
        selected_result = uuid4()
        unrelated_job = uuid4()
        unrelated_attempt = uuid4()
        unrelated_result = uuid4()
        command_id = uuid4()
        now = datetime.now(UTC)
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            for job_id, dataset_id, suite_id in (
                (selected_job, selected_dataset, selected_suite),
                (unrelated_job, uuid4(), uuid4()),
            ):
                connection.execute(
                    """INSERT INTO observatory_evaluation_jobs(
                    job_id, request_id, idempotency_key, request_fingerprint,
                    request_version, dataset_id, dataset_version, suite_id, suite_version,
                    evaluator_id, evaluator_version, capture_classification, deadline_at,
                    max_attempts, envelope_json)
                    VALUES (%s, %s, %s, %s, '1.0.0', %s, '1.0.0', %s, '1.0.0',
                    %s, '1.0.0', 'metadata_only', %s, 1, '{}'::jsonb)""",
                    (
                        job_id,
                        uuid4(),
                        uuid4(),
                        "1" * 64 if job_id == selected_job else "2" * 64,
                        dataset_id,
                        suite_id,
                        uuid4(),
                        now + timedelta(hours=1),
                    ),
                )
            for attempt_id, job_id in (
                (selected_attempt, selected_job),
                (unrelated_attempt, unrelated_job),
            ):
                connection.execute(
                    """INSERT INTO observatory_evaluation_attempts(
                    attempt_id, job_id, attempt_number, request_id, request_digest,
                    next_attempt_at) VALUES (%s, %s, 1, %s, %s, %s)""",
                    (attempt_id, job_id, uuid4(), "3" * 64, now),
                )
            for result_id, job_id, attempt_id in (
                (selected_result, selected_job, selected_attempt),
                (unrelated_result, unrelated_job, unrelated_attempt),
            ):
                connection.execute(
                    """INSERT INTO observatory_evaluation_result_receipts(
                    result_id, message_id, job_id, attempt_id, request_digest,
                    result_digest, status, disposition, envelope_json)
                    VALUES (%s, %s, %s, %s, %s, %s, 'completed', 'applied', '{}'::jsonb)""",
                    (result_id, uuid4(), job_id, attempt_id, "4" * 64, "5" * 64),
                )
            selector_value = str(selected_dataset if selector_kind == "dataset" else selected_suite)
            connection.execute(
                """INSERT INTO observatory_evaluation_deletion_commands(
                command_id, idempotency_key, selector_kind, selector_value,
                confirmation, status) VALUES (%s, %s, %s, %s,
                'DELETE_EVALUATION_ARTIFACTS', 'queued')""",
                (command_id, uuid4(), selector_kind, selector_value),
            )
            events = [
                (uuid4(), "evaluation_job", selected_job, "created", "queued"),
                (uuid4(), "evaluation_attempt", selected_attempt, "created", "queued"),
                (uuid4(), "evaluation_result", selected_result, "created", "completed"),
                (uuid4(), "evaluation_job", unrelated_job, "created", "queued"),
                (uuid4(), "evaluation_attempt", unrelated_attempt, "created", "queued"),
                (uuid4(), "evaluation_result", unrelated_result, "created", "completed"),
                (uuid4(), "evaluation_deletion", command_id, "deletion_requested", "queued"),
            ]
            for event_id, resource_type, resource_id, change, state in events:
                connection.execute(
                    """INSERT INTO observatory_evaluation_live_events(
                    event_id, resource_type, resource_id, change, state)
                    VALUES (%s, %s, %s, %s, %s)""",
                    (event_id, resource_type, str(resource_id), change, state),
                )
            connection.commit()

        @asynccontextmanager
        async def connection_context() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {_quote(schema)}")
                yield connection
                await connection.commit()
            finally:
                await connection.close()

        repository = PostgresEvaluationArtifactRepository(connection_context)
        assert await repository.complete_deletion_command(str(command_id), 9) == 9
        # A retry after the terminal transition is a read-only replay: it
        # returns the persisted count and does not duplicate audit/notification
        # effects.
        assert await repository.complete_deletion_command(str(command_id), 99) == 9
        with psycopg.connect(database_url) as connection:
            connection.execute(f"SET search_path TO {_quote(schema)}")
            remaining = connection.execute(
                "SELECT resource_type, resource_id "
                "FROM observatory_evaluation_live_events ORDER BY sequence"
            ).fetchall()
            audits = connection.execute(
                "SELECT COUNT(*) FROM observatory_evaluation_deletion_audits "
                "WHERE command_id = %s AND outcome = 'completed'",
                (command_id,),
            ).fetchone()
        assert {(row[0], row[1]) for row in remaining} == {
            ("evaluation_job", str(unrelated_job)),
            ("evaluation_attempt", str(unrelated_attempt)),
            ("evaluation_result", str(unrelated_result)),
            ("evaluation_deletion", str(command_id)),
        }
        assert audits == (1,)
