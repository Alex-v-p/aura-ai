"""Opt-in AURA-0097 PostgreSQL idempotency and scheduler recovery checks."""
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportCallIssue=false, reportArgumentType=false, reportGeneralTypeIssues=false

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
from aura_observatory.adapters.dashboards import PostgresOperationsRepository
from aura_observatory.alerting import AlertService
from aura_observatory.saved_queries import SavedQueryService

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"
NOW = "2026-10-21T10:00:00Z"


def _database_url() -> str:
    value = os.environ.get("AURA_AURA0097_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0097_POSTGRES_URL for live AURA-0097 coverage")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _isolated_schema(database_url: str) -> Generator[tuple[psycopg.Connection[Any], str]]:
    connection = psycopg.connect(database_url)
    schema = f"aura0097_{uuid4().hex[:12]}"
    quoted = _quote(schema)
    primary_error: Exception | None = None
    try:
        connection.execute(f"CREATE SCHEMA {quoted}")
        connection.execute(f"SET search_path TO {quoted}")
        for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
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
            cleanup_error = RuntimeError("AURA-0097 isolated PostgreSQL cleanup failed")
            cleanup_error.__cause__ = error
        finally:
            connection.close()
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def _query() -> dict[str, object]:
    return {
        "kind": "run_search",
        "window": {"kind": "last_hour", "from": None, "to": None},
        "statuses": [],
        "components": [],
        "deployments": [],
        "models": [],
        "pageSize": 10,
    }


def _rule_request() -> dict[str, object]:
    return {
        "name": "latency",
        "severity": "warning",
        "condition": {
            "signal": "latency",
            "query": {"queryId": str(uuid4()), "queryVersion": "1.0.0"},
            "operator": "greater_than",
            "threshold": 100,
            "unit": "milliseconds",
            "window": {"kind": "last_hour", "from": None, "to": None},
            "minimumSampleSize": 1,
            "missingEvidencePolicy": "inconclusive",
            "staleAfterSeconds": 300,
            "groupBy": [],
        },
        "cadence": "five_minutes",
        "state": "enabled",
        "createdAt": NOW,
    }


def _connection_factory(database_url: str, schema: str) -> Any:
    """Return a transaction-scoped async connection factory for one schema."""

    @asynccontextmanager
    async def connection_context() -> AsyncGenerator[Any]:
        connection = await psycopg.AsyncConnection.connect(database_url)
        try:
            await connection.execute(f"SET search_path TO {_quote(schema)}")
            yield connection
            await connection.commit()
        finally:
            await connection.close()

    return connection_context


def _alert_instance(
    *, rule_id: str | None = None, instance_id: str | None = None
) -> dict[str, object]:
    return {
        "instanceId": instance_id or str(uuid4()),
        "instanceVersion": "1.0.0",
        "rule": {
            "resourceType": "alert_rule",
            "resourceId": rule_id or str(uuid4()),
            "resourceVersion": "1.0.0",
        },
        "groupingId": str(uuid4()),
        "groupingFacets": [],
        "severity": "warning",
        "state": "firing",
        "evidenceState": "complete",
        "observedValue": 250,
        "unit": "milliseconds",
        "sampleSize": 2,
        "evidence": [],
        "firstObservedAt": NOW,
        "lastObservedAt": NOW,
        "occurrenceCount": 1,
        "suppressed": False,
        "acknowledged": False,
    }


async def _operation_counts(
    connection_factory: Any, instance_id: str
) -> dict[str, int | str | None]:
    async with connection_factory() as connection:
        counts: dict[str, int | str | None] = {}
        for key, table in (
            ("instances", "observatory_alert_instances"),
            ("acknowledgements", "observatory_alert_acknowledgements"),
            ("commands", "observatory_operations_commands"),
            ("events", "observatory_operations_events"),
            ("audit", "observatory_operations_audit"),
        ):
            result = await connection.execute(f"SELECT COUNT(*) FROM {table}")
            row = await result.fetchone()
            counts[key] = int(row[0]) if row is not None else 0
        result = await connection.execute(
            "SELECT resource_json FROM observatory_alert_instances WHERE resource_id=%s "
            "AND current_version=resource_version",
            (instance_id,),
        )
        row = await result.fetchone()
        counts["instance_json"] = str(row[0]) if row is not None else None
        return counts


@pytest.mark.asyncio
async def test_postgres_command_replay_is_exact_and_changed_intent_conflicts() -> None:
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

        repository = PostgresOperationsRepository(connection_context)
        service = SavedQueryService(repository)
        request = {"name": "Runs", "definition": _query(), "createdAt": NOW}
        first = await service.create(request, command_id="pg-query-replay-0001")
        replay = await service.create(dict(request), command_id="pg-query-replay-0001")
        assert replay == first
        with pytest.raises(RuntimeError, match="idempotency key was reused"):
            await service.create(
                {**request, "name": "Different"}, command_id="pg-query-replay-0001"
            )

        with pytest.raises(RuntimeError, match="idempotency key was reused"):
            await repository.delete_resource(
                "saved_query",
                str(first["queryId"]),
                str(first["queryVersion"]),
                command_id="pg-query-replay-0001",
            )


@pytest.mark.asyncio
async def test_postgres_saved_query_cursor_is_snapshot_stable_across_mutations() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = SavedQueryService(repository)
        base = datetime.now(UTC).replace(microsecond=0)

        def timestamp(seconds: int) -> str:
            return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

        created: list[dict[str, object]] = []
        for index in range(1, 4):
            created.append(
                await service.create(
                    {
                        "name": f"Runs {index}",
                        "definition": _query(),
                        "createdAt": timestamp(index),
                    }
                )
            )

        first = await service.list(limit=1)
        cursor = first["nextCursor"]
        snapshot = first["snapshotAt"]
        assert isinstance(cursor, str)
        assert isinstance(snapshot, str)

        inserted = dict(created[0])
        inserted["queryId"] = str(uuid4())
        inserted["name"] = "Inserted after snapshot"
        inserted["createdAt"] = timestamp(4)
        await repository.create_resource("saved_query", inserted)

        second = await service.list(cursor=cursor, limit=1)
        assert second["snapshotAt"] == snapshot
        assert [item["queryId"] for item in second["items"]] == [created[1]["queryId"]]
        items = second["items"]
        assert isinstance(items, list)
        assert isinstance(items[0], dict)
        assert items[0]["queryVersion"] == "1.0.0"
        assert second["nextCursor"] is not None
        third = await service.list(cursor=str(second["nextCursor"]), limit=1)
        assert [item["queryId"] for item in third["items"]] == [created[2]["queryId"]]
        assert third["snapshotAt"] == snapshot
        assert third["nextCursor"] is None

    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = SavedQueryService(repository)
        base = datetime.now(UTC).replace(microsecond=0)

        def timestamp(seconds: int) -> str:
            return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

        created = [
            await service.create(
                {
                    "name": f"Versioned {index}",
                    "definition": _query(),
                    "createdAt": timestamp(index),
                }
            )
            for index in range(1, 4)
        ]
        first = await service.list(limit=1)
        versioned = dict(created[1])
        versioned["queryVersion"] = "2.0.0"
        await repository.create_resource("saved_query", versioned)
        with pytest.raises(RuntimeError, match="resource snapshot is no longer available"):
            await service.list(cursor=str(first["nextCursor"]), limit=1)

    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = SavedQueryService(repository)
        base = datetime.now(UTC).replace(microsecond=0)

        def timestamp(seconds: int) -> str:
            return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

        created = [
            await service.create(
                {
                    "name": f"Deleted {index}",
                    "definition": _query(),
                    "createdAt": timestamp(index),
                }
            )
            for index in range(1, 4)
        ]
        first = await service.list(limit=1)
        await repository.delete_resource(
            "saved_query", str(created[2]["queryId"]), str(created[2]["queryVersion"])
        )
        with pytest.raises(RuntimeError, match="resource snapshot is no longer available"):
            await service.list(cursor=str(first["nextCursor"]), limit=1)


@pytest.mark.asyncio
async def test_postgres_acknowledgement_commits_instance_and_record_atomically() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = AlertService(repository)
        instance = _alert_instance()
        await repository.create_resource("alert_instance", instance)
        before = await _operation_counts(factory, str(instance["instanceId"]))
        command_id = "pg-ack-success-0001"
        result = await service.acknowledge(
            str(instance["instanceId"]),
            {
                "instanceVersion": "1.0.0",
                "reason": "known_issue",
                "acknowledgedAt": NOW,
            },
            command_id=command_id,
        )

        current = await repository.get_resource("alert_instance", str(instance["instanceId"]))
        assert current is not None
        assert current["acknowledged"] is True
        acknowledged_instance = result["instance"]
        assert isinstance(acknowledged_instance, dict)
        assert acknowledged_instance["resourceId"] == instance["instanceId"]
        after = await _operation_counts(factory, str(instance["instanceId"]))
        assert int(after["acknowledgements"]) == int(before["acknowledgements"]) + 1
        assert int(after["events"]) == int(before["events"]) + 2
        assert int(after["audit"]) == int(before["audit"]) + 2
        async with factory() as connection:
            acknowledgement = await connection.execute(
                "SELECT resource_json FROM observatory_alert_acknowledgements"
            )
            acknowledgement_rows = await acknowledgement.fetchall()
            command = await connection.execute(
                "SELECT status, replay_json FROM observatory_operations_commands "
                "WHERE command_id=%s",
                (command_id,),
            )
            command_row = await command.fetchone()
        assert len(acknowledgement_rows) == 1
        assert command_row is not None
        assert command_row[0] == "completed"
        assert str(result["acknowledgementId"]) in str(command_row[1])


@pytest.mark.asyncio
async def test_postgres_acknowledgement_replay_and_changed_intent_are_noops() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = AlertService(repository)
        instance = _alert_instance()
        await repository.create_resource("alert_instance", instance)
        request = {
            "instanceVersion": "1.0.0",
            "reason": "known_issue",
            "acknowledgedAt": NOW,
        }
        command_id = "pg-ack-replay-0001"
        first = await service.acknowledge(
            str(instance["instanceId"]), request, command_id=command_id
        )
        before_replay = await _operation_counts(factory, str(instance["instanceId"]))
        replay = await service.acknowledge(
            str(instance["instanceId"]), dict(request), command_id=command_id
        )
        after_replay = await _operation_counts(factory, str(instance["instanceId"]))
        assert dict(replay) == dict(first)
        assert after_replay == before_replay

        changed = dict(request)
        changed["reason"] = "accepted_risk"
        with pytest.raises(RuntimeError, match="idempotency key was reused"):
            await service.acknowledge(
                str(instance["instanceId"]), changed, command_id=command_id
            )
        after_conflict = await _operation_counts(factory, str(instance["instanceId"]))
        assert after_conflict == before_replay


@pytest.mark.asyncio
async def test_postgres_acknowledgement_failure_rolls_back_every_write() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        instance = _alert_instance()
        await repository.create_resource("alert_instance", instance)
        before = await _operation_counts(factory, str(instance["instanceId"]))

        class FailAfterInstanceEvent(PostgresOperationsRepository):
            async def _append(
                self,
                connection: Any,
                resource_type: str,
                resource_id: str,
                version: str,
                change: str,
                state: object,
            ) -> dict[str, object]:
                if resource_type == "alert_acknowledgement":
                    raise RuntimeError("injected acknowledgement event failure")
                return await super()._append(
                    connection, resource_type, resource_id, version, change, state
                )

        failing = FailAfterInstanceEvent(factory)
        service = AlertService(failing)
        command_id = "pg-ack-rollback-0001"
        with pytest.raises(RuntimeError, match="injected acknowledgement event failure"):
            await service.acknowledge(
                str(instance["instanceId"]),
                {
                    "instanceVersion": "1.0.0",
                    "reason": "known_issue",
                    "acknowledgedAt": NOW,
                },
                command_id=command_id,
            )
        after_failure = await _operation_counts(factory, str(instance["instanceId"]))
        assert after_failure == before

        # The rolled-back pending reservation is reusable, proving no partial
        # command, acknowledgement, state, event, or audit write survived.
        result = await AlertService(repository).acknowledge(
            str(instance["instanceId"]),
            {
                "instanceVersion": "1.0.0",
                "reason": "known_issue",
                "acknowledgedAt": NOW,
            },
            command_id=command_id,
        )
        acknowledged_instance = result["instance"]
        assert isinstance(acknowledged_instance, dict)
        assert acknowledged_instance["resourceId"] == instance["instanceId"]


@pytest.mark.asyncio
async def test_postgres_evaluator_and_acknowledgement_concurrency_preserves_owner_state() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = AlertService(repository)
        instance = _alert_instance()
        await repository.create_resource("alert_instance", instance)
        evaluated = dict(instance)
        evaluated["instanceVersion"] = "1.0.1"
        evaluated["lastObservedAt"] = "2026-10-21T10:05:00Z"
        evaluated["occurrenceCount"] = 2

        async def evaluate() -> None:
            await repository.replace_resource(
                "alert_instance", evaluated, expected_version="1.0.0"
            )

        async def acknowledge() -> None:
            try:
                await service.acknowledge(
                    str(instance["instanceId"]),
                    {
                        "instanceVersion": "1.0.0",
                        "reason": "known_issue",
                        "acknowledgedAt": NOW,
                    },
                    command_id="pg-ack-race-0001",
                )
            except RuntimeError as error:
                # If evaluation commits first, acknowledge the exact current
                # version. The owner action must not be lost to that race.
                if "version conflict" not in str(error):
                    raise
                current = await repository.get_resource(
                    "alert_instance", str(instance["instanceId"])
                )
                assert current is not None
                await service.acknowledge(
                    str(instance["instanceId"]),
                    {
                        "instanceVersion": current["instanceVersion"],
                        "reason": "known_issue",
                        "acknowledgedAt": NOW,
                    },
                    command_id="pg-ack-race-retry-0001",
                )

        await asyncio.gather(acknowledge(), evaluate())
        current = await repository.get_resource("alert_instance", str(instance["instanceId"]))
        assert current is not None
        assert current["acknowledged"] is True
        assert int(current["occurrenceCount"]) >= 1


@pytest.mark.asyncio
async def test_postgres_scheduler_cadence_failure_restart_and_exact_lease_fence() -> None:
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

        repository = PostgresOperationsRepository(connection_context)
        rule = await AlertService(repository).create_rule(_rule_request())
        rule_id = str(rule["ruleId"])

        first_claim = await repository.claim_due_alerts(
            now=NOW, owner="scheduler-a", lease_seconds=30
        )
        assert len(first_claim) == 1
        first_token = str(first_claim[0]["leaseToken"])
        await repository.complete_alert_claim(
            rule_id,
            now=NOW,
            success=False,
            owner="scheduler-a",
            lease_token=first_token,
        )
        async with connection_context() as connection:
            result = await connection.execute(
                "SELECT next_due_at, attempt_count, last_failure_code "
                "FROM observatory_alert_rules WHERE resource_id=%s",
                (rule_id,),
            )
            row = await result.fetchone()
        assert row is not None
        assert row[0] > datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        assert row[1] == 1
        assert row[2] == "evaluation_failed"

        async with connection_context() as connection:
            await connection.execute(
                "UPDATE observatory_alert_rules SET next_due_at="
                "CURRENT_TIMESTAMP - INTERVAL '1 second' "
                "WHERE resource_id=%s",
                (rule_id,),
            )

        restarted = PostgresOperationsRepository(connection_context)
        retry_claim = await restarted.claim_due_alerts(
            now=NOW, owner="scheduler-b", lease_seconds=30
        )
        assert len(retry_claim) == 1
        retry_token = str(retry_claim[0]["leaseToken"])
        await restarted.complete_alert_claim(
            rule_id,
            now=NOW,
            success=True,
            owner="scheduler-b",
            lease_token=retry_token,
        )
        async with connection_context() as connection:
            result = await connection.execute(
                "SELECT next_due_at, attempt_count, last_failure_code, lease_token "
                "FROM observatory_alert_rules WHERE resource_id=%s",
                (rule_id,),
            )
            row = await result.fetchone()
        assert row is not None
        assert row[0] == datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(minutes=5)
        assert row[1] == 0
        assert row[2] is None
        assert row[3] is None

        async with connection_context() as connection:
            await connection.execute(
                "UPDATE observatory_alert_rules SET next_due_at="
                "CURRENT_TIMESTAMP - INTERVAL '1 second' "
                "WHERE resource_id=%s",
                (rule_id,),
            )
        fenced_claim = await restarted.claim_due_alerts(
            now=NOW, owner="scheduler-c", lease_seconds=30
        )
        assert len(fenced_claim) == 1
        fenced_token = str(fenced_claim[0]["leaseToken"])
        async with connection_context() as connection:
            # The exact boundary is represented by the database transaction
            # timestamp; the completion transaction starts later and must be fenced.
            await connection.execute(
                "UPDATE observatory_alert_rules SET lease_expires_at=CURRENT_TIMESTAMP "
                "WHERE resource_id=%s",
                (rule_id,),
            )
        await restarted.complete_alert_claim(
            rule_id,
            now=NOW,
            success=True,
            owner="scheduler-c",
            lease_token=fenced_token,
        )
        async with connection_context() as connection:
            result = await connection.execute(
                "SELECT lease_token, next_due_at FROM observatory_alert_rules WHERE resource_id=%s",
                (rule_id,),
            )
            row = await result.fetchone()
        assert row is not None
        assert row[0] == fenced_token  # the stale lease remains fenced in place
        assert row[1] < datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(minutes=1)

        async with connection_context() as connection:
            await connection.execute(
                "UPDATE observatory_alert_rules SET next_due_at="
                "CURRENT_TIMESTAMP - INTERVAL '1 second', "
                "lease_expires_at=CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE resource_id=%s",
                (rule_id,),
            )
        takeover = await restarted.claim_due_alerts(
            now=NOW, owner="scheduler-d", lease_seconds=30
        )
        assert len(takeover) == 1
        assert takeover[0]["leaseToken"] != fenced_token

        history = await restarted.list_history(resource_id=rule_id, limit=100)
        states = {item["state"] for item in history["items"]}
        assert {"lease_acquired", "lease_completed", "lease_conflict"} <= states


def test_postgres_operations_migration_reruns_after_fresh_and_upgrade_paths() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (connection, _schema):
        migration = (MIGRATION_ROOT / "0014_observatory_operations.sql").read_text(
            encoding="utf-8"
        )
        connection.execute(migration)
        connection.execute(migration)
        connection.commit()
        command_columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='observatory_operations_commands'"
        ).fetchall()
        assert {row[0] for row in command_columns} >= {
            "command_id", "operation", "target_key", "request_hash", "status"
        }


@pytest.mark.asyncio
async def test_postgres_snapshot_waits_for_held_writer_before_capturing_revision() -> None:
    """A held mutation cannot allocate a visible revision behind a page snapshot."""

    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        service = SavedQueryService(repository)
        for index in range(2):
            await service.create(
                {
                    "name": f"Before writer {index}",
                    "definition": _query(),
                    "createdAt": f"2026-10-21T10:0{index}:00Z",
                }
            )

        writer = await psycopg.AsyncConnection.connect(database_url)
        await writer.execute(f"SET search_path TO {_quote(schema)}")
        await writer.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'aura.observatory.operations.revisions', 0))"
        )
        writer_resource = {
            "queryId": str(uuid4()),
            "queryVersion": "1.0.0",
            "name": "Held writer",
            "definition": _query(),
            "createdAt": "2026-10-21T10:02:00Z",
        }
        await writer.execute(
            "INSERT INTO observatory_saved_queries "
            "(resource_id, resource_version, current_version, resource_json, "
            "created_at, updated_at, visibility_revision) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s, nextval("
            "'observatory_operations_revision_seq'))",
            (
                writer_resource["queryId"],
                writer_resource["queryVersion"],
                writer_resource["queryVersion"],
                json.dumps(writer_resource),
                writer_resource["createdAt"],
                writer_resource["createdAt"],
            ),
        )
        writer_ready = asyncio.Event()
        writer_ready.set()
        await writer_ready.wait()
        page_task = asyncio.create_task(repository.list_resources("saved_query", limit=10))
        await asyncio.sleep(0.05)
        assert not page_task.done(), "page capture must wait on the revision advisory lock"
        await writer.commit()
        await writer.close()
        page = await asyncio.wait_for(page_task, timeout=5)
        assert any(item["queryId"] == writer_resource["queryId"] for item in page["items"])


@pytest.mark.asyncio
async def test_postgres_concurrent_first_version_creation_has_one_current_row() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as (_connection, schema):
        factory = _connection_factory(database_url, schema)
        repository = PostgresOperationsRepository(factory)
        resource_id = str(uuid4())
        resource = {
            "queryId": resource_id,
            "queryVersion": "1.0.0",
            "name": "Concurrent first version",
            "definition": _query(),
            "createdAt": NOW,
        }
        results = await asyncio.gather(
            repository.create_resource("saved_query", resource),
            repository.create_resource("saved_query", dict(resource)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in results) == 1
        assert sum(isinstance(item, Exception) for item in results) == 1
        async with factory() as connection:
            result = await connection.execute(
                "SELECT COUNT(*) FROM observatory_saved_queries "
                "WHERE resource_id=%s AND current_version=resource_version",
                (resource_id,),
            )
            row = await result.fetchone()
        assert row is not None and row[0] == 1
