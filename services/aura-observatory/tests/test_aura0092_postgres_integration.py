"""Opt-in PostgreSQL integration coverage for AURA-0092 orchestration.

The default test suite deliberately does not require a database.  When
``AURA_AURA0092_POSTGRES_URL`` is set, each test uses an isolated schema and
the real Observatory adapter, including the service-owned migrations.  No
Core connection or Core table is used by these tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from aura_observatory.adapters.evaluation_orchestration import (
    PostgresEvaluationOrchestrationRepository,
)
from aura_observatory.evaluation_orchestration import (
    EvaluationAdmission,
    EvaluationOrchestrationService,
    EvaluationSchedulerService,
    InMemoryEvaluationOrchestrationRepository,
)
from aura_observatory.metrics import ObservatoryMetrics

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _database_url() -> str:
    value = os.environ.get("AURA_AURA0092_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0092_POSTGRES_URL for real PostgreSQL coverage")
    return value


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _request(*, key: str | None = None, max_attempts: int = 3) -> EvaluationAdmission:
    now = datetime.now(UTC)
    dataset_id = str(uuid4())
    suite_id = str(uuid4())
    evaluator_id = str(uuid4())
    target = {
        "componentId": "aura.evaluation.synthetic",
        "componentVersion": "1.0.0",
        "codeRevision": "0000000",
        "imageDigest": "a" * 64,
        "configurationRevision": "local",
    }
    data = {
        "executionKind": "synthetic_end_to_end",
        "requestVersion": "1.0.0",
        "targetComponent": target,
        "captureAuthorization": {
            "captureClassification": "metadata_only",
            "authorizationPurpose": "synthetic_benchmark",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
        },
        "provenance": {
            "dataset": {"id": dataset_id, "version": "1.0.0"},
            "suite": {"id": suite_id, "version": "1.0.0"},
            "evaluators": [
                {"evaluatorId": evaluator_id, "evaluatorVersion": "1.0.0", "kind": "deterministic"}
            ],
            "components": [target],
            "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": "a" * 64},
            "deploymentRevision": "aura0092-test",
            "reproducibility": {
                "seed": 7,
                "clockMode": "fixed",
                "timeZone": "UTC",
                "locale": "en-US",
                "fixtureRevision": "synthetic-v1",
                "promptRevision": "prompt-v1",
                "policyRevision": "policy-v1",
            },
        },
        "limits": {
            "execution": {
                "timeoutMs": 10_000,
                "maxConcurrency": 1,
                "maxAttempts": max_attempts,
                "maxCases": 1,
                "maxInputBytes": 0,
                "maxOutputBytes": 1024,
                "maxTokens": 0,
            },
            "maxQueueTimeMs": 10_000,
            "maxModelCalls": 1,
            "maxArtifactOperations": 0,
        },
        "deadlineAt": (now + timedelta(minutes=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "caseIds": [str(uuid4())],
        "inputArtifactReferences": [],
    }
    return EvaluationAdmission(
        request_id=str(uuid4()),
        idempotency_key=key or str(uuid4()),
        request_version="1.0.0",
        dataset_id=dataset_id,
        dataset_version="1.0.0",
        suite_id=suite_id,
        suite_version="1.0.0",
        evaluator_id=evaluator_id,
        evaluator_version="1.0.0",
        capture_classification="metadata_only",
        deadline_at=now + timedelta(minutes=5),
        max_attempts=max_attempts,
        envelope={"data": data},
        created_at=now,
    )


def _result(request_envelope: dict[str, object], *, message_type: str) -> dict[str, object]:
    result = dict(request_envelope)
    result["messageId"] = str(uuid4())
    result["messageType"] = message_type
    result["occurredAt"] = (
        datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    # Core terminal events causally reference the concrete runner request
    # message while retaining the request's workflow correlation.
    result["causationId"] = request_envelope["messageId"]
    request_data = cast(dict[str, object], request_envelope["data"])
    runtime_evidence = {
        key: {"status": "missing"}
        for key in (
            "componentVersion",
            "codeRevision",
            "imageDigest",
            "configurationRevision",
            "modelId",
            "modelVersion",
            "modelDigest",
            "deploymentRevision",
            "fixtureRevision",
            "promptRevision",
            "policyRevision",
            "seed",
        )
    }
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "a" * 64,
        "status": "completed" if message_type.endswith("completed") else "failed",
        "completedAt": result["occurredAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": request_data["limits"],
        "provenance": request_data["provenance"],
        "runtimeEvidence": runtime_evidence,
        **(
            {"failureCode": "DEPENDENCY_UNAVAILABLE", "retryable": True}
            if message_type.endswith("failed")
            else {}
        ),
    }
    if message_type.endswith("completed"):
        result["data"]["startedAt"] = result["occurredAt"]
        result["data"]["deadlineAt"] = request_data["deadlineAt"]
        result["data"]["outputArtifactReferences"] = []
    else:
        result["data"]["deadlineAt"] = request_data["deadlineAt"]
    return result


def _metric_total(metrics: ObservatoryMetrics, name: str, labels: dict[str, str]) -> float:
    return sum(
        value
        for (metric_name, metric_labels), value in metrics.values.items()
        if metric_name == name and dict(metric_labels) == labels
    )


async def _concurrent_admission(
    service: EvaluationOrchestrationService, request: EvaluationAdmission
) -> list[Any]:
    return await asyncio.gather(*(service.admit(request) for _ in range(8)))


@asynccontextmanager
async def _isolated_postgres() -> AsyncGenerator[tuple[Any, Any]]:
    """Yield a real repository and sync query connection in an isolated schema."""

    database_url = _database_url()
    schema = f"aura0092_{uuid4().hex[:12]}"
    quoted_schema = _quoted_identifier(schema)
    connection: Any = psycopg.connect(database_url)
    try:
        connection.execute(f"CREATE SCHEMA {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection: Any = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        yield (
            PostgresEvaluationOrchestrationRepository(
                connection_factory, retry_base_seconds=1, retry_max_seconds=8
            ),
            connection,
        )
    finally:
        connection.rollback()
        connection.execute("SET search_path TO public")
        connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        connection.commit()
        connection.close()


@pytest.mark.asyncio
async def test_in_memory_admission_is_atomic_under_concurrent_duplicate_delivery() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    request = _request()

    jobs = await _concurrent_admission(service, request)

    assert {job.job_id for job in jobs} == {jobs[0].job_id}
    assert len(repository.jobs) == 1
    assert len(repository.attempts) == 1
    assert len(repository.outbox) == 1


@pytest.mark.asyncio
async def test_retry_policy_is_bounded_and_permanent_failure_is_terminal() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    request = _request(max_attempts=2)
    job = await service.admit(request)
    first = next(iter(repository.outbox.values())).envelope

    assert (
        await service.consume_result(_result(dict(first), message_type="evaluation.runner.failed"))
        == "applied"
    )
    assert len(repository.attempts) == 2
    assert repository.jobs[job.job_id].state.value == "queued"
    retry = next(attempt for attempt in repository.attempts.values() if attempt.attempt_number == 2)
    retry_entry = next(
        entry for entry in repository.outbox.values() if entry.attempt_id == retry.attempt_id
    )
    retry_result = _result(dict(retry_entry.envelope), message_type="evaluation.runner.failed")
    cast(dict[str, object], retry_result["data"])["failureCode"] = "REQUEST_INVALID"
    cast(dict[str, object], retry_result["data"])["retryable"] = False

    assert await service.consume_result(retry_result) == "applied"
    assert repository.jobs[job.job_id].state.value == "failed"
    assert len(repository.attempts) == 2
    assert not any(attempt.attempt_number == 3 for attempt in repository.attempts.values())


@pytest.mark.asyncio
async def test_retryable_failure_is_not_terminal_and_direct_terminal_counters_are_distinct(
) -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    metrics = ObservatoryMetrics()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    await service.admit(_request(max_attempts=2))
    first = next(iter(repository.outbox.values())).envelope

    assert await service.consume_result(
        _result(dict(first), message_type="evaluation.runner.failed")
    ) == "applied"
    assert _metric_total(
        metrics,
        "observatory_evaluation_retry_scheduled_count",
        {"outcome": "accepted"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluation_terminal_count",
        {"outcome": "failed"},
    ) == 0

    cancelled_job = await service.admit(_request())
    assert await service.cancel(cancelled_job.job_id)
    assert _metric_total(
        metrics,
        "observatory_evaluation_cancellation_count",
        {"outcome": "accepted"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluation_terminal_count",
        {"outcome": "cancelled"},
    ) == 1

    deadline_metrics = ObservatoryMetrics()
    deadline_service = EvaluationOrchestrationService(
        InMemoryEvaluationOrchestrationRepository(), metrics=deadline_metrics
    )
    deadline_job = await deadline_service.admit(_request())
    await deadline_service.dispatch_once(
        now=deadline_job.admission.deadline_at + timedelta(seconds=1)
    )
    assert _metric_total(
        deadline_metrics,
        "observatory_evaluation_deadline_count",
        {"outcome": "deadline"},
    ) == 1
    assert _metric_total(
        deadline_metrics,
        "observatory_evaluation_terminal_count",
        {"outcome": "deadline"},
    ) == 1


@pytest.mark.asyncio
async def test_dependency_outcomes_and_spans_distinguish_nats_publish_from_postgres_apply() -> None:
    class FailingTransport:
        async def publish(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic transport outage")

    publish_repository = InMemoryEvaluationOrchestrationRepository()
    publish_metrics = ObservatoryMetrics()
    publish_service = EvaluationOrchestrationService(
        publish_repository, transport=FailingTransport(), metrics=publish_metrics  # type: ignore[arg-type]
    )
    await publish_service.admit(_request())
    assert (
        await publish_service.publish_outbox_once(now=datetime.now(UTC) + timedelta(seconds=1))
        == 0
    )
    assert _metric_total(
        publish_metrics,
        "observatory_evaluation_dependency_outcome_count",
        {"operation": "evaluation_nats", "outcome": "failed"},
    ) == 1
    assert _metric_total(
        publish_metrics,
        "observatory_evaluation_dependency_outcome_count",
        {"operation": "postgresql", "outcome": "failed"},
    ) == 0
    assert publish_metrics.component_spans[-1].dependency == "evaluation_nats"
    assert publish_metrics.component_spans[-1].outcome == "failure"

    apply_repository = InMemoryEvaluationOrchestrationRepository()
    apply_metrics = ObservatoryMetrics()
    apply_service = EvaluationOrchestrationService(apply_repository, metrics=apply_metrics)
    await apply_service.admit(_request())
    request_envelope = next(iter(apply_repository.outbox.values())).envelope
    assert await apply_service.consume_result(
        _result(dict(request_envelope), message_type="evaluation.runner.completed")
    ) == "applied"
    assert _metric_total(
        apply_metrics,
        "observatory_evaluation_dependency_outcome_count",
        {"operation": "postgresql", "outcome": "success"},
    ) == 1
    assert apply_metrics.component_spans[-1].dependency == "postgresql"


@pytest.mark.asyncio
async def test_rejected_result_acknowledges_without_persisting_provider_payload() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    acknowledged = False

    async def ack() -> None:
        nonlocal acknowledged
        acknowledged = True

    malformed = {"messageType": "evaluation.runner.completed", "prompt": "secret"}
    assert await service.consume_result(malformed, acknowledge=ack) == "rejected"
    assert acknowledged
    assert not repository.receipts
    assert not repository.audits


@pytest.mark.asyncio
async def test_lease_claims_are_exclusive_and_expired_work_is_recovered() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, lease_seconds=1)
    request = _request()
    now = request.created_at
    job = await service.admit(request, now=now)

    claims = await asyncio.gather(
        repository.claim_due_attempts(now=now, limit=1, lease_seconds=1, worker_id="worker-a"),
        repository.claim_due_attempts(now=now, limit=1, lease_seconds=1, worker_id="worker-b"),
    )
    assert sum(len(claim) for claim in claims) == 1
    assert await service.recover_once(now=now + timedelta(seconds=2)) == 1
    assert repository.jobs[job.job_id].state.value == "queued"


@pytest.mark.asyncio
async def test_cancel_and_late_result_remain_terminal_and_idempotent() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_request())
    attempt = next(iter(repository.attempts.values()))

    outcomes = await asyncio.gather(service.cancel(job.job_id), service.cancel(job.job_id))
    assert sum(outcomes) == 1
    cancellation_entries = [
        entry
        for entry in repository.outbox.values()
        if entry.event_type.endswith("cancellation_requested")
    ]
    assert len(cancellation_entries) == 1

    late = _result(
        dict(next(iter(repository.outbox.values())).envelope),
        message_type="evaluation.runner.completed",
    )
    late["attemptId"] = attempt.attempt_id
    assert await service.consume_result(late) == "out_of_order"
    assert repository.jobs[job.job_id].state.value == "cancelled"


@pytest.mark.asyncio
async def test_outbox_failure_backoff_is_bounded_and_exhausts_one_row() -> None:
    class FailingTransport:
        async def publish(
            self, _subject: str, _envelope: dict[str, object], _event_id: str
        ) -> None:
            raise RuntimeError("synthetic transport outage")

    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, transport=FailingTransport())  # type: ignore[arg-type]
    request = _request()
    await service.admit(request)
    now = datetime.now(UTC) + timedelta(seconds=1)
    for attempt in range(10):
        assert await service.publish_outbox_once(now=now) == 0
        now += timedelta(seconds=min(86_400, 10 * (2**attempt)) + 1)
    entry = next(iter(repository.outbox.values()))
    assert entry.publish_attempts == 10
    assert entry.last_error_code == "exhausted"
    assert entry.published_at is not None
    assert await service.publish_outbox_once(now=now + timedelta(days=1)) == 0


@pytest.mark.asyncio
async def test_real_postgres_retry_max_attempts_and_durable_receipt_ack() -> None:
    async with _isolated_postgres() as (repository, connection):
        service = EvaluationOrchestrationService(repository)
        request = _request(max_attempts=2)
        job = await service.admit(request)
        # PostgreSQL applies CURRENT_TIMESTAMP to the initial outbox row, so
        # use a post-admission clock value rather than the client-created
        # request timestamp when claiming due work.
        now = datetime.now(UTC) + timedelta(seconds=1)
        claimed = await repository.claim_due_attempts(
            now=now, limit=1, lease_seconds=30, worker_id="retry-worker"
        )
        assert len(claimed) == 1
        first_entry = next(iter(await repository.claim_outbox(now=now, limit=10)))
        first_result = _result(dict(first_entry.envelope), message_type="evaluation.runner.failed")
        ack_count = 0

        async def ack() -> None:
            nonlocal ack_count
            ack_count += 1

        assert (
            await service.consume_result(
                first_result, now=now + timedelta(seconds=1), acknowledge=ack
            )
            == "applied"
        )
        assert ack_count == 1
        retry_entries = await repository.claim_outbox(now=now + timedelta(seconds=3), limit=10)
        assert len(retry_entries) == 1
        assert retry_entries[0].attempt_id != first_entry.attempt_id
        retry_result = _result(
            dict(retry_entries[0].envelope), message_type="evaluation.runner.failed"
        )
        retry_data = cast(dict[str, object], retry_result["data"])
        retry_data["failureCode"] = "REQUEST_INVALID"
        retry_data["retryable"] = False
        assert (
            await service.consume_result(
                retry_result, now=now + timedelta(seconds=4), acknowledge=ack
            )
            == "applied"
        )
        assert ack_count == 2
        terminal = connection.execute(
            "SELECT state, attempt_count FROM observatory_evaluation_jobs WHERE job_id=%s",
            (job.job_id,),
        ).fetchone()
        assert terminal == ("failed", 2)
        counts = connection.execute(
            "SELECT count(*) FROM observatory_evaluation_result_receipts"
        ).fetchone()
        assert counts == (2,)
        audits = connection.execute(
            "SELECT event_type FROM observatory_evaluation_lifecycle_audits "
            "WHERE job_id=%s ORDER BY occurred_at, event_type",
            (job.job_id,),
        ).fetchall()
        assert [row[0] for row in audits] == [
            "evaluation.retry_scheduled",
            "evaluation.failed",
        ]
        terminal_audits = connection.execute(
            "SELECT event_type, outcome, attempt_id "
            "FROM observatory_evaluation_lifecycle_audits "
            "WHERE job_id=%s AND event_type LIKE 'evaluation.%%' "
            "AND event_type NOT IN ('evaluation.retry_scheduled', "
            "'evaluation.deadline', 'evaluation.schedule_materialized') "
            "AND outcome='accepted'",
            (job.job_id,),
        ).fetchall()
        assert len(terminal_audits) == 1
        assert str(terminal_audits[0][2]) == str(retry_entries[0].attempt_id)


@pytest.mark.asyncio
async def test_real_postgres_deadline_and_cancellation_result_races_are_terminal() -> None:
    async with _isolated_postgres() as (repository, connection):
        service = EvaluationOrchestrationService(repository)
        deadline_request = _request()
        deadline_job = await service.admit(deadline_request)
        expired_at = deadline_request.created_at + timedelta(minutes=6)
        assert await repository.settle_expired(now=expired_at, limit=10) == 1
        assert await service.cancel(deadline_job.job_id, now=expired_at) is False
        deadline_envelope = connection.execute(
            "SELECT envelope_json FROM observatory_evaluation_outbox WHERE job_id=%s",
            (deadline_job.job_id,),
        ).fetchone()[0]
        deadline_result = _result(
            dict(deadline_envelope),
            message_type="evaluation.runner.completed",
        )
        assert await service.consume_result(deadline_result, now=expired_at) == "out_of_order"
        deadline_state = connection.execute(
            "SELECT state FROM observatory_evaluation_jobs WHERE job_id=%s", (deadline_job.job_id,)
        ).fetchone()
        assert deadline_state == ("deadline",)
        deadline_audits = connection.execute(
            "SELECT event_type, outcome, attempt_id "
            "FROM observatory_evaluation_lifecycle_audits "
            "WHERE job_id=%s AND event_type='evaluation.deadline'",
            (deadline_job.job_id,),
        ).fetchall()
        assert len(deadline_audits) == 1
        assert deadline_audits[0][1] == "accepted"

        race_request = _request()
        race_job = await service.admit(race_request)
        race_entry = connection.execute(
            "SELECT envelope_json FROM observatory_evaluation_outbox WHERE job_id=%s",
            (race_job.job_id,),
        ).fetchone()[0]
        race_result = _result(dict(race_entry), message_type="evaluation.runner.completed")
        cancelled, result = await asyncio.gather(
            service.cancel(race_job.job_id, now=race_request.created_at + timedelta(seconds=1)),
            service.consume_result(race_result, now=race_request.created_at + timedelta(seconds=1)),
        )
        assert result in {"applied", "out_of_order"}
        final_state = connection.execute(
            "SELECT state FROM observatory_evaluation_jobs WHERE job_id=%s", (race_job.job_id,)
        ).fetchone()
        assert final_state in {("cancelled",), ("succeeded",)}
        assert cancelled is (final_state == ("cancelled",))
        if final_state == ("cancelled",):
            cancellation_audits = connection.execute(
                "SELECT event_type, outcome, attempt_id "
                "FROM observatory_evaluation_lifecycle_audits "
                "WHERE job_id=%s AND outcome='accepted'",
                (race_job.job_id,),
            ).fetchall()
            assert len(cancellation_audits) == 1
            assert cancellation_audits[0][0] == "evaluation.cancelled"
            assert cancellation_audits[0][1] == "accepted"
            assert str(cancellation_audits[0][2]) == str(race_result["attemptId"])


@pytest.mark.asyncio
async def test_real_postgres_scheduler_claim_cadence_occurrence_and_lease_recovery() -> None:
    async with _isolated_postgres() as (repository, connection):
        now = datetime.now(UTC).replace(microsecond=0)
        schedule_id, occurrence_id = str(uuid4()), str(uuid4())
        template = _request(max_attempts=2)
        connection.execute(
            """INSERT INTO observatory_evaluation_schedules(
            schedule_id, occurrence_id, dataset_id, dataset_version, suite_id,
            suite_version, evaluator_id, evaluator_version, next_run_at,
            cadence_seconds, deadline_seconds, max_attempts, request_envelope_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,60,30,2,%s::jsonb)""",
            (
                schedule_id,
                occurrence_id,
                template.dataset_id,
                template.dataset_version,
                template.suite_id,
                template.suite_version,
                template.evaluator_id,
                template.evaluator_version,
                now - timedelta(seconds=1),
                json.dumps(template.envelope),
            ),
        )
        connection.commit()
        scheduler = EvaluationSchedulerService(repository, worker_id="scheduler-a", batch_size=1)
        assert await scheduler.run_once(now=now) == 1
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM observatory_evaluation_jobs), "
            "(SELECT count(*) FROM observatory_evaluation_attempts), "
            "(SELECT count(*) FROM observatory_evaluation_outbox)"
        ).fetchone()
        assert counts == (1, 1, 1)
        scheduled = connection.execute(
            "SELECT next_run_at, occurrence_id, lease_token FROM observatory_evaluation_schedules "
            "WHERE schedule_id=%s",
            (schedule_id,),
        ).fetchone()
        assert scheduled[0] == now + timedelta(seconds=59)
        assert scheduled[1] != occurrence_id
        assert scheduled[2] is None
        assert await scheduler.run_once(now=now + timedelta(seconds=1)) == 0
        assert await scheduler.run_once(now=now + timedelta(seconds=61)) == 1
        counts = connection.execute("SELECT count(*) FROM observatory_evaluation_jobs").fetchone()
        assert counts == (2,)


def test_migration_is_forward_only_and_contains_all_orchestration_ownership_tables() -> None:
    migration = (MIGRATION_ROOT / "0008_evaluation_orchestration.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS" in migration
    assert "DROP TABLE" not in migration.upper()
    assert "ALTER ' || 'TABLE observatory_evaluation_metric_series" in migration
    assert "observatory_evaluation_dependency_outcome_count" in migration
    assert "FOR UPDATE SKIP LOCKED" not in migration
    for table in (
        "observatory_evaluation_jobs",
        "observatory_evaluation_attempts",
        "observatory_evaluation_schedules",
        "observatory_evaluation_outbox",
        "observatory_evaluation_result_receipts",
        "observatory_evaluation_lifecycle_audits",
        "observatory_evaluation_worker_leases",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration


@pytest.mark.asyncio
async def test_real_postgres_fresh_upgrade_rerun_admission_and_result_receipt() -> None:
    database_url = _database_url()
    schema = f"aura0092_{uuid4().hex[:12]}"
    quoted_schema = _quoted_identifier(schema)
    connection: Any = psycopg.connect(database_url)
    try:
        connection.execute(f"CREATE SCHEMA {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        migrations = sorted(MIGRATION_ROOT.glob("*.sql"))
        for _ in range(2):
            for migration in migrations:
                connection.execute(migration.read_text(encoding="utf-8"))
            connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection: Any = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        repository = PostgresEvaluationOrchestrationRepository(connection_factory)
        service = EvaluationOrchestrationService(repository)
        request = _request()
        job = await service.admit(request)
        same_job = await service.admit(request)
        assert same_job.job_id == job.job_id

        counts = connection.execute(
            "SELECT (SELECT count(*) FROM observatory_evaluation_jobs), "
            "(SELECT count(*) FROM observatory_evaluation_attempts), "
            "(SELECT count(*) FROM observatory_evaluation_outbox)"
        ).fetchone()
        assert counts == (1, 1, 1)

        entries = await repository.claim_outbox(now=datetime.now(UTC), limit=10)
        assert len(entries) == 1
        result = _result(dict(entries[0].envelope), message_type="evaluation.runner.completed")
        assert await service.consume_result(result) == "applied"
        stored = connection.execute(
            "SELECT count(*) FROM observatory_evaluation_result_receipts WHERE message_id=%s",
            (result["messageId"],),
        ).fetchone()
        assert stored == (1,)
        assert await service.consume_result(result) == "duplicate"
        terminal = connection.execute(
            "SELECT state FROM observatory_evaluation_jobs WHERE job_id=%s", (job.job_id,)
        ).fetchone()
        assert terminal == ("succeeded",)
        terminal_audits = connection.execute(
            "SELECT event_type, outcome, attempt_id "
            "FROM observatory_evaluation_lifecycle_audits "
            "WHERE job_id=%s AND event_type LIKE 'evaluation.%%' "
            "AND event_type NOT IN ('evaluation.retry_scheduled', "
            "'evaluation.deadline', 'evaluation.schedule_materialized') "
            "AND outcome='accepted'",
            (job.job_id,),
        ).fetchall()
        assert len(terminal_audits) == 1
        assert str(terminal_audits[0][2]) == str(result["attemptId"])
    finally:
        connection.rollback()
        connection.execute("SET search_path TO public")
        connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        connection.commit()
        connection.close()


@pytest.mark.asyncio
async def test_real_postgres_concurrent_admission_and_restart_recovery() -> None:
    database_url = _database_url()
    schema = f"aura0092_{uuid4().hex[:12]}"
    quoted_schema = _quoted_identifier(schema)
    connection: Any = psycopg.connect(database_url)
    try:
        connection.execute(f"CREATE SCHEMA {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection: Any = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        request = _request()
        services = [
            EvaluationOrchestrationService(
                PostgresEvaluationOrchestrationRepository(connection_factory)
            )
            for _ in range(4)
        ]
        jobs = await asyncio.gather(*(service.admit(request) for service in services))
        assert {job.job_id for job in jobs} == {jobs[0].job_id}
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM observatory_evaluation_jobs), "
            "(SELECT count(*) FROM observatory_evaluation_attempts), "
            "(SELECT count(*) FROM observatory_evaluation_outbox)"
        ).fetchone()
        assert counts == (1, 1, 1)

        repository = PostgresEvaluationOrchestrationRepository(connection_factory)
        claimed = await repository.claim_due_attempts(
            now=datetime.now(UTC), limit=1, lease_seconds=1, worker_id="crash-before-publish"
        )
        assert len(claimed) == 1
        recovered = await repository.recover(
            now=datetime.now(UTC) + timedelta(seconds=2), limit=1, worker_id="replacement"
        )
        assert recovered == 1
        state = connection.execute(
            "SELECT state FROM observatory_evaluation_attempts WHERE attempt_id=%s",
            (claimed[0].attempt_id,),
        ).fetchone()
        assert state == ("queued",)
    finally:
        connection.rollback()
        connection.execute("SET search_path TO public")
        connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        connection.commit()
        connection.close()
