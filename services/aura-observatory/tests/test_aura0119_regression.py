"""Regression coverage for reviewer-blocking production invariants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_observatory.adapters.run_evidence import (
    MemoryRunEvidenceObjectStore,
    PostgresRunEvidenceRepository,
)
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.run_evidence import (
    RUN_EVIDENCE_EVALUATORS,
    MemoryRunEvidenceRepository,
    RunEvidenceDeletion,
    RunEvidenceError,
    RunEvidenceIngestionService,
    RunEvidenceManifest,
    RunInspectionService,
    evaluate_run_evidence_projection,
    validate_run_evidence,
)
from aura_observatory.settings import Settings


def test_core_produced_closed_envelope_is_accepted() -> None:
    payload = {"kind": "admitted_request", "request": {"role": "user", "content": "hello"}}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value = {
        "schemaVersion": 1,
        "contractId": "run-evidence-v1",
        "eventId": str(uuid4()),
        "evidenceId": str(uuid4()),
        "runId": "11111111-1111-4111-8111-111111111111",
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef",
        "parentSpanId": None,
        "sequence": 0,
        "stepCategory": "admission",
        "evidenceKind": "admitted_request",
        "mediaClassification": "structured",
        "mediaType": "application/json",
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "byteCount": len(canonical),
        "capturePolicyRevision": "owner-full-content-v1",
        "observedAt": "2026-10-25T12:00:00.000Z",
        "emittedAt": "2026-10-25T12:00:00.001Z",
        "delivery": {
            "kind": "initial",
            "producerId": "aura-core",
            "deliveryId": str(uuid4()),
            "attempt": 1,
        },
        "contentAdmission": {
            "outcome": "passed",
            "scannerRevision": "core-evidence-scanner-v1",
            "policyRevision": "core-evidence-policy-v1",
            "inspectedAt": "2026-10-25T12:00:00.000Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
        "captureState": "captured",
        "reason": None,
        "payload": payload,
    }
    validated = validate_run_evidence(value)
    assert validated.canonical_bytes == canonical


def test_empty_captured_evidence_is_allowed_by_migration() -> None:
    sql = (
        Path(__file__).parents[3]
        / "services/aura-observatory/migrations/postgresql/versions/0016_run_evidence.sql"
    ).read_text()
    assert "capture_state = 'captured' AND digest IS NOT NULL AND byte_count >= 0" in sql
    assert "fingerprint CHAR(64)," in sql
    assert "ALTER COLUMN fingerprint DROP NOT NULL" in sql
    assert "SET fingerprint = NULL" in sql
    assert "WHERE capture_state = 'deleted'" in sql


def test_manual_deletion_clears_content_fingerprint_and_stable_indexes() -> None:
    payload = {"kind": "admitted_request", "request": {"role": "user", "content": "private"}}
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value = {
        "schemaVersion": 1,
        "contractId": "run-evidence-v1",
        "eventId": str(uuid4()),
        "evidenceId": str(uuid4()),
        "runId": "11111111-1111-4111-8111-111111111111",
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef",
        "parentSpanId": None,
        "sequence": 0,
        "stepCategory": "admission",
        "evidenceKind": "admitted_request",
        "mediaClassification": "structured",
        "mediaType": "application/json",
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "byteCount": len(canonical),
        "capturePolicyRevision": "owner-full-content-v1",
        "observedAt": "2026-10-25T12:00:00.000Z",
        "emittedAt": "2026-10-25T12:00:00.001Z",
        "delivery": {
            "kind": "initial",
            "producerId": "aura-core",
            "deliveryId": str(uuid4()),
            "attempt": 1,
        },
        "contentAdmission": {
            "outcome": "passed",
            "scannerRevision": "core-evidence-scanner-v1",
            "policyRevision": "core-evidence-policy-v1",
            "inspectedAt": "2026-10-25T12:00:00.000Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
        "captureState": "captured",
        "reason": None,
        "payload": payload,
    }

    for producer_id in ("ghp_abcdefghijklmnopqrstuvwxyz1234", "AKIAIOSFODNN7EXAMPLE"):
        invalid = cast(dict[str, object], dict(value))
        invalid["delivery"] = {
            **cast(dict[str, object], value["delivery"]),
            "producerId": producer_id,
        }
        with pytest.raises(RunEvidenceError, match="ingest_rejected"):
            validate_run_evidence(invalid)

    async def scenario() -> None:
        repository = MemoryRunEvidenceRepository()
        item = validate_run_evidence(value)
        await repository.ingest(item)
        evidence_id = item.evidence_id
        assert repository.items[evidence_id][2] == item.fingerprint
        await repository.delete(item.run_id, str(uuid4()))
        assert repository.items[evidence_id][2] is None

    asyncio.run(scenario())


def test_attachment_category_must_match_the_closed_contract() -> None:
    # A model-visible attachment is context evidence.  Retrieval is reserved
    # for memory/document chunks and must not be accepted as a producer typo.
    value = {
        "schemaVersion": 1,
        "contractId": "run-evidence-v1",
        "eventId": str(uuid4()),
        "evidenceId": str(uuid4()),
        "runId": "11111111-1111-4111-8111-111111111111",
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef",
        "parentSpanId": None,
        "sequence": 0,
        "stepCategory": "retrieval",
        "evidenceKind": "model_visible_attachment",
        "mediaClassification": "document",
        "mediaType": "application/pdf",
        "digest": "sha256:" + hashlib.sha256(b"%PDF-1.7\n").hexdigest(),
        "byteCount": len(b"%PDF-1.7\n"),
        "capturePolicyRevision": "owner-full-content-v1",
        "observedAt": "2026-10-25T12:00:00.000Z",
        "emittedAt": "2026-10-25T12:00:00.001Z",
        "delivery": {
            "kind": "initial",
            "producerId": "aura-core",
            "deliveryId": str(uuid4()),
            "attempt": 1,
        },
        "contentAdmission": {
            "outcome": "passed",
            "scannerRevision": "core-evidence-scanner-v1",
            "policyRevision": "core-evidence-policy-v1",
            "inspectedAt": "2026-10-25T12:00:00.000Z",
            "inspectionModes": [
                "decoded_text",
                "metadata",
                "container_structure",
                "ocr",
            ],
        },
        "captureState": "captured",
        "reason": None,
        "payload": {
            "kind": "model_visible_attachment",
            "attachmentId": "a-1",
            "displayName": "x.pdf",
            "contentBase64": "JVBERi0xLjcK",
        },
    }
    with pytest.raises(RunEvidenceError, match="ingest_rejected"):
        validate_run_evidence(value)


def test_non_captured_admission_is_bound_to_state_and_reason() -> None:
    def envelope(state: str, reason: str, admission: dict[str, object]) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "contractId": "run-evidence-v1",
            "eventId": str(uuid4()),
            "evidenceId": str(uuid4()),
            "runId": "11111111-1111-4111-8111-111111111111",
            "traceId": "0123456789abcdef0123456789abcdef",
            "spanId": "0123456789abcdef",
            "parentSpanId": None,
            "sequence": 0,
            "stepCategory": "admission",
            "evidenceKind": "admitted_request",
            "mediaClassification": "structured",
            "mediaType": "application/json",
            "digest": None,
            "byteCount": 0,
            "capturePolicyRevision": "owner-full-content-v1",
            "observedAt": "2026-10-25T12:00:00.000Z",
            "emittedAt": "2026-10-25T12:00:00.001Z",
            "delivery": {
                "kind": "initial",
                "producerId": "aura-core",
                "deliveryId": str(uuid4()),
                "attempt": 1,
            },
            "contentAdmission": admission,
            "captureState": state,
            "reason": reason,
            "payload": None,
        }

    for state, reason in (
        ("excluded", "capture_disabled"),
        ("dropped", "queue_pressure"),
        ("deleted", "manually_deleted"),
    ):
        invalid = envelope(state, reason, {"outcome": "rejected_prohibited_material"})
        with pytest.raises(RunEvidenceError, match="ingest_rejected"):
            validate_run_evidence(invalid)

    invalid = envelope("partial", "bearer_material_rejected", {"outcome": "not_applicable"})
    with pytest.raises(RunEvidenceError, match="ingest_rejected"):
        validate_run_evidence(invalid)


def test_postgres_deletion_run_fence_does_not_self_deadlock() -> None:
    class Result:
        def __init__(self, row: object | None = None, rows: list[object] | None = None) -> None:
            self.row = row
            self.rows = rows or []

        async def fetchone(self) -> object | None:
            return self.row

        async def fetchall(self) -> list[object]:
            return self.rows

    class Connection:
        def __init__(self, statements: list[str]) -> None:
            self.statements = statements

        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: str, *_args: object) -> Result:
            self.statements.append(statement)
            if "SELECT deletion_id,status" in statement:
                return Result()
            if "SELECT evidence_id FROM" in statement:
                return Result(rows=[])
            return Result()

    async def scenario() -> list[str]:
        statements: list[str] = []

        @asynccontextmanager
        async def factory() -> AsyncGenerator[Connection]:
            yield Connection(statements)

        repository = PostgresRunEvidenceRepository(
            factory, cast(Any, MemoryRunEvidenceObjectStore())
        )
        result = await repository.delete(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        assert result.status == "completed"
        return statements

    statements = asyncio.run(scenario())
    assert sum("pg_advisory_lock" in statement for statement in statements) == 1
    assert sum("pg_advisory_unlock" in statement for statement in statements) == 1
    assert not any("pg_advisory_xact_lock" in statement for statement in statements)


def test_postgres_deletion_resume_accumulates_durable_count() -> None:
    run_id = "11111111-1111-4111-8111-111111111111"
    deletion_id = "44444444-4444-4444-8444-444444444444"
    idempotency_key = "55555555-5555-4555-8555-555555555555"

    class Result:
        def __init__(
            self,
            *,
            rows: list[object] | None = None,
            row: object | None = None,
        ) -> None:
            self.rows = rows or []
            self.row = row

        async def fetchall(self) -> list[object]:
            return self.rows

        async def fetchone(self) -> object | None:
            return self.row

    class Connection:
        def __init__(self, calls: list[tuple[str, object | None]]) -> None:
            self.calls = calls

        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(
            self, statement: str, params: object | None = None
        ) -> Result:
            self.calls.append((statement, params))
            if "idempotency_key" in statement and "SELECT deletion_id" in statement:
                return Result(
                    row=(
                        deletion_id,
                        "failed",
                        "2026-10-25T12:00:00.000Z",
                        None,
                        1,
                        "object_delete_failed",
                    )
                )
            if "status='completed'" in statement and "SELECT deletion_id" in statement:
                return Result()
            if "SELECT evidence_id,trace_id,parent_span_id" in statement:
                return Result(
                    rows=[
                        ("66666666-6666-4666-8666-666666666666", "0" * 32, None),
                        ("77777777-7777-4777-8777-777777777777", "1" * 32, None),
                    ]
                )
            if "RETURNING evidence_id" in statement:
                values = cast(tuple[object, object], params)
                return Result(row=(str(values[1]),))
            return Result()

    class Metrics:
        def __init__(self) -> None:
            self.spans: list[dict[str, object]] = []
            self.values: list[str] = []

        def record_component_span(self, **span: object) -> None:
            self.spans.append(span)

        def record(self, name: str, *_args: object, **_kwargs: object) -> None:
            self.values.append(name)

    async def scenario() -> tuple[
        RunEvidenceDeletion, list[tuple[str, object | None]], Metrics
    ]:
        calls: list[tuple[str, object | None]] = []

        @asynccontextmanager
        async def factory() -> AsyncGenerator[Connection]:
            yield Connection(calls)

        metrics = Metrics()
        repository = PostgresRunEvidenceRepository(
            factory, cast(Any, MemoryRunEvidenceObjectStore()), metrics=metrics
        )
        result = await repository.delete(run_id, idempotency_key, _resume=True)
        return result, calls, metrics

    result, calls, metrics = asyncio.run(scenario())
    assert result.status == "completed"
    assert result.deleted_evidence_count == 3
    finalized = [
        params
        for statement, params in calls
        if "SET status='completed'" in statement
    ]
    assert len(finalized) == 1
    assert cast(tuple[object, int, str], finalized[0])[1:] == (3, result.deletion_id)
    assert any("RETURNING evidence_id" in statement for statement, _ in calls)
    transition_statements = [
        statement for statement, _ in calls if "RETURNING evidence_id" in statement
    ]
    assert len(transition_statements) == 2
    finalize_spans = [
        span
        for span in metrics.spans
        if span["operation"] == "run_evidence.deletion_finalize"
        and span["outcome"] == "success"
    ]
    assert len(finalize_spans) == 1
    assert finalize_spans[0]["trace_id"] == "0" * 32
    assert finalize_spans[0]["parent_span_id"] is None
    assert "run_evidence_deletion_latency_ms" not in metrics.values


def test_postgres_deletion_crash_boundary_recovers_without_missing_capture() -> None:
    """A progress-transaction failure leaves a captured target retryable."""

    run_id = "11111111-1111-4111-8111-111111111111"
    deletion_id = "44444444-4444-4444-8444-444444444444"
    idempotency_key = "55555555-5555-4555-8555-555555555555"
    evidence_ids = (
        "66666666-6666-4666-8666-666666666666",
        "77777777-7777-4777-8777-777777777777",
    )

    class Result:
        def __init__(self, *, rows: list[object] | None = None, row: object | None = None) -> None:
            self.rows = rows or []
            self.row = row

        async def fetchall(self) -> list[object]:
            return self.rows

        async def fetchone(self) -> object | None:
            return self.row

    class Database:
        def __init__(self, failure_mode: str) -> None:
            self.states: dict[str, str] = {
                evidence_id: "captured" for evidence_id in evidence_ids
            }
            self.deletion: list[object] | None = None
            self.fail_progress_once = failure_mode == "manifest"
            self.fail_count_update_once = failure_mode == "count"
            self.last_transition: str | None = None
            self.calls: list[str] = []

    class Connection:
        def __init__(self, database: Database) -> None:
            self.database = database

        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: str, params: object | None = None) -> Result:
            database = self.database
            database.calls.append(statement)
            if "idempotency_key" in statement and "SELECT deletion_id" in statement:
                return Result(row=tuple(database.deletion) if database.deletion else None)
            if "status='completed'" in statement and "SELECT deletion_id" in statement:
                return Result()
            if "SELECT evidence_id,trace_id,parent_span_id" in statement:
                return Result(
                    rows=[
                        (evidence_id, "0" * 32, None)
                        for evidence_id, state in database.states.items()
                        if state == "captured"
                    ]
                )
            if "RETURNING evidence_id" in statement:
                evidence_id = str(cast(tuple[object, object], params)[1])
                if database.fail_progress_once:
                    database.fail_progress_once = False
                    raise RuntimeError("simulated progress transaction loss")
                if database.states[evidence_id] == "captured":
                    database.states[evidence_id] = "deleted"
                    database.last_transition = evidence_id
                    return Result(row=(evidence_id,))
                return Result()
            if "SET status='failed'" in statement:
                values = cast(tuple[object, object], params)
                database.deletion = [
                    deletion_id,
                    "failed",
                    "2026-10-25T12:00:00.000Z",
                    None,
                    values[0],
                    "object_delete_failed",
                ]
                return Result()
            if "SET status='in_progress'" in statement:
                if database.deletion is not None:
                    database.deletion[1] = "in_progress"
                return Result()
            if "INSERT INTO observatory_run_evidence_deletions" in statement:
                database.deletion = [
                    deletion_id,
                    "in_progress",
                    "2026-10-25T12:00:00.000Z",
                    None,
                    0,
                    None,
                ]
                return Result()
            if "SET deleted_evidence_count" in statement:
                assert database.deletion is not None
                if database.fail_count_update_once:
                    database.fail_count_update_once = False
                    assert database.last_transition is not None
                    database.states[database.last_transition] = "captured"
                    raise RuntimeError("simulated count transaction loss")
                database.deletion[4] = cast(tuple[object, object], params)[0]
                return Result()
            if "SET capture_state='deleted'" in statement:
                for evidence_id in database.states:
                    database.states[evidence_id] = "deleted"
                return Result()
            if "SET status='completed'" in statement:
                assert database.deletion is not None
                values = cast(tuple[object, object, object], params)
                database.deletion = [
                    deletion_id,
                    "completed",
                    database.deletion[2],
                    values[0],
                    values[1],
                    None,
                ]
            return Result()

    class ObjectStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, _run_id: str, evidence_id: str) -> None:
            self.deleted.append(evidence_id)

    async def scenario(
        failure_mode: str,
    ) -> tuple[RunEvidenceDeletion, Database, ObjectStore]:
        database = Database(failure_mode)
        object_store = ObjectStore()

        @asynccontextmanager
        async def factory() -> AsyncGenerator[Connection]:
            yield Connection(database)

        repository = PostgresRunEvidenceRepository(factory, cast(Any, object_store))
        first = await repository.delete(run_id, idempotency_key)
        assert first.status == "failed"
        assert database.states[evidence_ids[0]] == "captured"
        recovered = await repository.delete(run_id, idempotency_key, _resume=True)
        return recovered, database, object_store

    for failure_mode in ("manifest", "count"):
        result, database, object_store = asyncio.run(scenario(failure_mode))
        assert result.status == "completed"
        assert result.deleted_evidence_count == 2
        assert all(state == "deleted" for state in database.states.values())
        assert object_store.deleted.count(evidence_ids[0]) == 2
        assert object_store.deleted.count(evidence_ids[1]) == 1


@pytest.mark.asyncio
async def test_object_stream_not_found_span_is_unavailable_and_run_correlated() -> None:
    run_id = "11111111-1111-4111-8111-111111111111"
    evidence_id = "22222222-2222-4222-8222-222222222222"
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{run_id}/evidence/{evidence_id}"
        )
    assert response.status_code == 404
    spans = [
        span
        for span in app.state.query.metrics.component_spans
        if span.operation == "run_evidence.object_stream"
    ]
    assert len(spans) == 1
    assert spans[0].outcome == "unavailable"
    assert spans[0].run_id == run_id
    assert spans[0].dependency == "postgresql"
    assert spans[0].error_code is None


def test_inspection_uses_atomic_page_and_summary_snapshot() -> None:
    class SnapshotRepository:
        def __init__(self) -> None:
            self.calls = 0
            self.manifest = RunEvidenceManifest(
                "22222222-2222-4222-8222-222222222222",
                "33333333-3333-4333-8333-333333333333",
                "11111111-1111-4111-8111-111111111111",
                "0123456789abcdef0123456789abcdef",
                "0123456789abcdef",
                None,
                0,
                "admission",
                "admitted_request",
                "captured",
                "structured",
                "application/json",
                "sha256:" + "0" * 64,
                0,
                "2026-10-25T12:00:00.000Z",
                None,
            )

        async def inspection_page(
            self,
            run_id: str,
            *,
            cursor: str | None = None,
            limit: int = 250,
        ) -> tuple[list[RunEvidenceManifest], str | None, dict[str, object]]:
            assert run_id == "11111111-1111-4111-8111-111111111111"
            assert cursor is None and limit == 250
            self.calls += 1
            return [self.manifest], None, {
                "total": 1,
                "counts": {"captured": 1},
                "reasons": [],
                "deletionStatus": None,
            }

        async def manifests(
            self, *args: object, **kwargs: object
        ) -> tuple[list[RunEvidenceManifest], str | None]:
            raise AssertionError("inspection must not split page and summary reads")

        async def summary(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("inspection must not split page and summary reads")

    async def scenario() -> None:
        repository = SnapshotRepository()
        service = RunInspectionService(cast(Any, repository))
        projection = await service.inspect(
            "11111111-1111-4111-8111-111111111111"
        )
        assert projection is not None
        completeness = cast(dict[str, object], projection["completeness"])
        assert completeness["capturedEvidenceCount"] == 1
        assert completeness["state"] == "complete"
        assert repository.calls == 1
        assert service.last_evaluator_results["run_inspection_projection_truth"] == "pass"

    asyncio.run(scenario())


def test_postgres_inspection_binds_summary_queries_in_one_snapshot() -> None:
    run_id = "11111111-1111-4111-8111-111111111111"
    evidence_id = "22222222-2222-4222-8222-222222222222"
    event_id = "33333333-3333-4333-8333-333333333333"

    class Result:
        def __init__(
            self,
            *,
            rows: list[object] | None = None,
            row: object | None = None,
        ) -> None:
            self.rows = rows or []
            self.row = row

        async def fetchall(self) -> list[object]:
            return self.rows

        async def fetchone(self) -> object | None:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object | None]] = []

        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(
            self, statement: str, params: object | None = None
        ) -> Result:
            self.calls.append((statement, params))
            if "SELECT evidence_id,event_id" in statement:
                return Result(
                    rows=[
                        (
                            evidence_id,
                            event_id,
                            run_id,
                            "0123456789abcdef0123456789abcdef",
                            "0123456789abcdef",
                            None,
                            0,
                            "admission",
                            "admitted_request",
                            "captured",
                            "structured",
                            "application/json",
                            "sha256:" + "0" * 64,
                            0,
                            "2026-10-25T12:00:00.000Z",
                            None,
                        )
                    ]
                )
            if "SELECT capture_state,COUNT(*)" in statement:
                return Result(rows=[("captured", 1)])
            if "SELECT DISTINCT reason" in statement:
                return Result()
            if "SELECT status FROM observatory_run_evidence_deletions" in statement:
                return Result()
            return Result()

    async def scenario() -> tuple[
        Connection, tuple[list[RunEvidenceManifest], str | None, Mapping[str, object]]
    ]:
        connections: list[Connection] = []

        @asynccontextmanager
        async def factory() -> AsyncGenerator[Connection]:
            connection = Connection()
            connections.append(connection)
            yield connection

        repository = PostgresRunEvidenceRepository(
            factory, cast(Any, MemoryRunEvidenceObjectStore())
        )
        page = await repository.inspection_page(run_id, limit=1)
        assert len(connections) == 1
        return connections[0], page

    connection, page = asyncio.run(scenario())
    manifests, next_cursor, aggregate = page
    assert len(manifests) == 1
    assert next_cursor is None
    assert aggregate == {
        "total": 1,
        "counts": {"captured": 1},
        "reasons": [],
        "deletionStatus": None,
    }
    summary_calls = [
        params
        for statement, params in connection.calls
        if "WHERE run_id=%s" in statement
    ]
    assert summary_calls == [(run_id,), (run_id,), (run_id,)]
    assert any(
        statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        for statement, _ in connection.calls
    )
    assert any(statement == "COMMIT" for statement, _ in connection.calls)


@pytest.mark.asyncio
async def test_live_postgres_inspection_snapshot_excludes_concurrent_insert() -> None:
    """Exercise repeatable-read behavior against a real PostgreSQL snapshot."""

    database_url = os.environ.get("AURA_AURA0119_POSTGRES_URL", "")
    if not database_url:
        pytest.skip("set AURA_AURA0119_POSTGRES_URL for live AURA-0119 coverage")
    psycopg = pytest.importorskip("psycopg")
    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0119_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(database_url)
    snapshot_connection: Any = None
    writer_connection: Any = None
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        snapshot_connection = await psycopg.AsyncConnection.connect(database_url)
        writer_connection = await psycopg.AsyncConnection.connect(database_url)
        await snapshot_connection.execute(f"SET search_path TO {quoted_schema}")
        await snapshot_connection.commit()
        await writer_connection.execute(f"SET search_path TO {quoted_schema}")
        await writer_connection.commit()

        def manifest_values(sequence: int) -> tuple[object, ...]:
            return (
                str(uuid4()),
                str(uuid4()),
                "11111111-1111-4111-8111-111111111111",
                "0123456789abcdef0123456789abcdef",
                "0123456789abcdef",
                None,
                sequence,
                "admission",
                "admitted_request",
                "captured",
                "structured",
                "application/json",
                "sha256:" + "0" * 64,
                0,
                None,
                "f" * 64,
                None,
            )

        insert_sql = (
            "INSERT INTO observatory_run_evidence_manifests("
            "evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,"
            "step_category,evidence_kind,capture_state,media_classification,media_type,"
            "digest,byte_count,captured_at,reason,fingerprint,object_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,%s,%s,%s)"
        )
        await writer_connection.execute(insert_sql, manifest_values(0))
        await writer_connection.commit()

        page_seen = asyncio.Event()
        writer_done = asyncio.Event()

        async def write_late_manifest() -> None:
            await page_seen.wait()
            await writer_connection.execute(insert_sql, manifest_values(1))
            await writer_connection.commit()
            writer_done.set()

        writer_task = asyncio.create_task(write_late_manifest())

        class SnapshotConnection:
            async def __aenter__(self) -> SnapshotConnection:
                return self

            async def __aexit__(self, *_args: object) -> None:
                await snapshot_connection.commit()

            async def execute(
                self, statement: str, params: object | None = None
            ) -> Any:
                result = await snapshot_connection.execute(statement, params)
                if "SELECT evidence_id,event_id" in statement:
                    page_seen.set()
                    await writer_done.wait()
                return result

        @asynccontextmanager
        async def factory() -> AsyncGenerator[SnapshotConnection]:
            yield SnapshotConnection()

        repository = PostgresRunEvidenceRepository(
            factory, cast(Any, MemoryRunEvidenceObjectStore())
        )
        _manifests, _cursor, aggregate = await repository.inspection_page(
            "11111111-1111-4111-8111-111111111111", limit=10
        )
        await writer_task
        assert aggregate["total"] == 1
    finally:
        if snapshot_connection is not None:
            await snapshot_connection.close()
        if writer_connection is not None:
            await writer_connection.close()
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


def test_run_evidence_evaluator_registry_has_executable_outputs() -> None:
    projection: dict[str, object] = {
        "steps": [],
        "completeness": {"state": "partial"},
        "partial": True,
    }
    results = evaluate_run_evidence_projection(projection)
    assert set(results) == set(RUN_EVIDENCE_EVALUATORS)
    assert all(result in {"pass", "fail", "inconclusive"} for result in results.values())
    assert results["observatory_run_evidence_privacy"] == "pass"
    assert evaluate_run_evidence_projection(
        {**projection, "payload": "never accepted"}
    )["observatory_run_evidence_privacy"] == "fail"


def test_inspection_cursor_is_keyset_stable_when_late_evidence_arrives() -> None:
    async def scenario() -> tuple[list[int], list[int]]:
        repository = MemoryRunEvidenceRepository()
        ingestion = RunEvidenceIngestionService(repository)

        async def add(sequence: int) -> None:
            payload = {
                "kind": "admitted_request",
                "request": {"role": "user", "content": str(sequence)},
            }
            canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            await ingestion.ingest(
                {
                    "schemaVersion": 1,
                    "contractId": "run-evidence-v1",
                    "eventId": str(uuid4()),
                    "evidenceId": str(uuid4()),
                    "runId": "11111111-1111-4111-8111-111111111111",
                    "traceId": "0123456789abcdef0123456789abcdef",
                    "spanId": "0123456789abcdef",
                    "parentSpanId": None,
                    "sequence": sequence,
                    "stepCategory": "admission",
                    "evidenceKind": "admitted_request",
                    "mediaClassification": "structured",
                    "mediaType": "application/json",
                    "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                    "byteCount": len(canonical),
                    "capturePolicyRevision": "owner-full-content-v1",
                    "observedAt": f"2026-10-25T12:00:0{sequence}.000Z",
                    "emittedAt": f"2026-10-25T12:00:0{sequence}.001Z",
                    "delivery": {
                        "kind": "initial",
                        "producerId": "aura-core",
                        "deliveryId": str(uuid4()),
                        "attempt": 1,
                    },
                    "contentAdmission": {
                        "outcome": "passed",
                        "scannerRevision": "core-evidence-scanner-v1",
                        "policyRevision": "core-evidence-policy-v1",
                        "inspectedAt": "2026-10-25T12:00:00.000Z",
                        "inspectionModes": ["decoded_text", "container_structure"],
                    },
                    "captureState": "captured",
                    "reason": None,
                    "payload": payload,
                }
            )

        for sequence in (1, 2, 3):
            await add(sequence)
        first, cursor = await repository.manifests("11111111-1111-4111-8111-111111111111", limit=1)
        await add(0)
        second, _ = await repository.manifests(
            "11111111-1111-4111-8111-111111111111", cursor=cursor, limit=10
        )
        return [item.sequence for item in first], [item.sequence for item in second]

    assert asyncio.run(scenario()) == ([1], [2, 3])
