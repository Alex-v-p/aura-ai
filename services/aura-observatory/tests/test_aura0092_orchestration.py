"""AURA-0092 deterministic orchestration state-machine coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_observatory.evaluation_orchestration import (
    EvaluationAdmission,
    EvaluationOrchestrationService,
    EvaluationSchedule,
    EvaluationSchedulerService,
    InMemoryEvaluationOrchestrationRepository,
    OrchestrationError,
)


class _HeartbeatRepository(InMemoryEvaluationOrchestrationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.lease_tokens: list[str] = []

    async def heartbeat(
        self,
        *,
        component_id: str,
        lease_token: str,
        at: datetime,
        backlog_count: int = 0,
    ) -> Mapping[str, int]:
        del component_id, at, backlog_count
        token = lease_token
        UUID(token)
        self.lease_tokens.append(token)
        return {}


class _RecordingMetrics:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, str], float]] = []
        self.spans: list[dict[str, object]] = []

    def record(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self.records.append((name, dict(labels or {}), value))

    def set(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self.records.append((name, dict(labels or {}), value))

    def record_component_span(self, **span: object) -> None:
        self.spans.append(span)


def _completed_result(request_envelope: Mapping[str, object]) -> dict[str, object]:
    result = dict(request_envelope)
    occurred_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    result["occurredAt"] = occurred_at
    result["causationId"] = request_envelope["messageId"]
    request_data = cast(dict[str, object], request_envelope["data"])
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "a" * 64,
        "status": "completed",
        "startedAt": occurred_at,
        "completedAt": occurred_at,
        "deadlineAt": request_data["deadlineAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": request_data["limits"],
        "provenance": request_data["provenance"],
        "runtimeEvidence": {
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
        },
        "outputArtifactReferences": [],
    }
    return result


def _request(*, key: str | None = None, deadline: datetime | None = None) -> EvaluationAdmission:
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
                "maxAttempts": 3,
                "maxCases": 1,
                "maxInputBytes": 0,
                "maxOutputBytes": 1024,
                "maxTokens": 0,
            },
            "maxQueueTimeMs": 10_000,
            "maxModelCalls": 1,
            "maxArtifactOperations": 0,
        },
        "deadlineAt": (deadline or now + timedelta(minutes=5))
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
        deadline_at=deadline or now + timedelta(minutes=5),
        max_attempts=3,
        envelope={"data": data},
        created_at=now,
    )


@pytest.mark.asyncio
async def test_admission_is_idempotent_and_conflicts_fail_closed() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    key = str(uuid4())
    same_request = _request(key=key)
    first = await service.admit(same_request)
    second = await service.admit(same_request)
    assert first.job_id == second.job_id
    changed_data = dict(cast(dict[str, object], same_request.envelope["data"]))
    changed_data["requestVersion"] = "1.0.1"
    changed = replace(same_request, envelope={"data": changed_data})
    with pytest.raises(OrchestrationError, match="(idempotency_conflict|binding_mismatch)"):
        await service.admit(changed)


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_creates_one_outbox_command() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_request())
    assert await service.cancel(job.job_id)
    assert not await service.cancel(job.job_id)
    cancellations = [
        entry
        for entry in repository.outbox.values()
        if entry.event_type.endswith("cancellation_requested")
    ]
    assert len(cancellations) == 1


@pytest.mark.asyncio
async def test_late_result_after_cancel_is_out_of_order_and_does_not_reopen_job() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_request())
    await service.cancel(job.job_id)
    result = dict(next(iter(repository.outbox.values())).envelope)
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    request_data = cast(dict[str, object], result["data"])
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "0" * 64,
        "status": "completed",
        "startedAt": request_data["deadlineAt"],
        "completedAt": request_data["deadlineAt"],
        "deadlineAt": request_data["deadlineAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": request_data["limits"],
        "provenance": request_data["provenance"],
        "runtimeEvidence": {
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
        },
        "outputArtifactReferences": [],
    }
    assert await service.consume_result(result) == "out_of_order"
    assert repository.jobs[job.job_id].state.value == "cancelled"


@pytest.mark.asyncio
async def test_result_at_deadline_settles_before_periodic_deadline_poll() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    job = await service.admit(_request(deadline=deadline))
    request = next(iter(repository.outbox.values())).envelope
    result = dict(request)
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    data = cast(dict[str, object], request["data"])
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "a" * 64,
        "status": "completed",
        "startedAt": data["deadlineAt"],
        "completedAt": data["deadlineAt"],
        "deadlineAt": data["deadlineAt"],
        "captureAuthorization": data["captureAuthorization"],
        "effectiveLimits": data["limits"],
        "provenance": data["provenance"],
        "runtimeEvidence": {
            key: {"status": "missing"}
            for key in (
                "componentVersion", "codeRevision", "imageDigest", "configurationRevision",
                "modelId", "modelVersion", "modelDigest", "deploymentRevision",
                "fixtureRevision", "promptRevision", "policyRevision", "seed",
            )
        },
        "outputArtifactReferences": [],
    }
    assert await service.consume_result(result, now=deadline) == "out_of_order"
    assert repository.jobs[job.job_id].state.value == "deadline"


@pytest.mark.asyncio
async def test_late_result_deadline_metrics_are_terminal_and_exactly_once() -> None:
    metrics = _RecordingMetrics()
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    job = await service.admit(_request(deadline=deadline))
    request = next(
        entry.envelope
        for entry in repository.outbox.values()
        if entry.event_type == "evaluation.runner.requested"
    )
    result = _completed_result(request)

    assert await service.consume_result(result, now=deadline) == "out_of_order"
    assert await service.consume_result(result, now=deadline + timedelta(seconds=1)) == "duplicate"
    assert repository.jobs[job.job_id].state.value == "deadline"
    assert metrics.records.count(
        ("observatory_evaluation_deadline_count", {"outcome": "deadline"}, 1.0)
    ) == 1
    assert metrics.records.count(
        ("observatory_evaluation_terminal_count", {"outcome": "deadline"}, 1.0)
    ) == 1


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_results_do_not_double_count_terminal_metrics() -> None:
    metrics = _RecordingMetrics()
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, metrics=metrics)

    completed_job = await service.admit(_request())
    completed_request = next(
        entry.envelope
        for entry in repository.outbox.values()
        if entry.job_id == completed_job.job_id
        and entry.event_type == "evaluation.runner.requested"
    )
    completed = _completed_result(completed_request)
    assert await service.consume_result(completed) == "applied"
    assert await service.consume_result(completed) == "duplicate"

    cancelled_job = await service.admit(_request())
    cancelled_request = next(
        entry.envelope
        for entry in repository.outbox.values()
        if entry.job_id == cancelled_job.job_id
        and entry.event_type == "evaluation.runner.requested"
    )
    assert await service.cancel(cancelled_job.job_id)
    late = _completed_result(cancelled_request)
    assert await service.consume_result(late) == "out_of_order"
    assert await service.consume_result(late) == "duplicate"

    assert metrics.records.count(
        ("observatory_evaluation_terminal_count", {"outcome": "succeeded"}, 1.0)
    ) == 1
    assert metrics.records.count(
        ("observatory_evaluation_terminal_count", {"outcome": "cancelled"}, 1.0)
    ) == 1


@pytest.mark.asyncio
async def test_distinct_result_cannot_reopen_any_terminal_job_or_create_retry_work() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)

    succeeded = await service.admit(_request())
    succeeded_request = next(entry.envelope for entry in repository.outbox.values())
    assert await service.consume_result(_completed_result(succeeded_request)) == "applied"
    competing = _completed_result(succeeded_request)
    assert await service.consume_result(competing) == "out_of_order"
    assert repository.jobs[succeeded.job_id].state.value == "succeeded"

    failed = await service.admit(_request())
    failed_request = next(
        entry.envelope for entry in repository.outbox.values() if entry.job_id == failed.job_id
    )
    failed_result = _completed_result(failed_request)
    failed_result["messageType"] = "evaluation.runner.failed"
    failed_data = cast(dict[str, object], failed_result["data"])
    failed_data["status"] = "failed"
    failed_data["failureCode"] = "REQUEST_INVALID"
    failed_data["retryable"] = False
    failed_data.pop("startedAt")
    failed_data.pop("outputArtifactReferences")
    assert await service.consume_result(failed_result) == "applied"
    competing_failed = _completed_result(failed_request)
    assert await service.consume_result(competing_failed) == "out_of_order"
    assert repository.jobs[failed.job_id].state.value == "failed"
    assert (
        len(
            [attempt for attempt in repository.attempts.values() if attempt.job_id == failed.job_id]
        )
        == 1
    )
    assert not any(
        entry.job_id == failed.job_id and entry.event_type == "evaluation.runner.requested"
        for entry in repository.outbox.values()
        if entry.event_id != failed_request["messageId"]
    )


@pytest.mark.asyncio
async def test_validation_rejection_span_has_no_postgres_dependency_claim() -> None:
    metrics = _RecordingMetrics()
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    job = await service.admit(_request())
    request = next(
        entry.envelope for entry in repository.outbox.values() if entry.job_id == job.job_id
    )
    invalid = dict(request)
    invalid["messageType"] = "evaluation.runner.unexpected"

    assert await service.consume_result(invalid) == "rejected"
    rejection_spans = [span for span in metrics.spans if span.get("error_code") == "validation"]
    assert len(rejection_spans) == 1
    assert rejection_spans[0].get("dependency") is None


@pytest.mark.asyncio
async def test_scheduler_heartbeat_postgres_failure_is_counted_once() -> None:
    class FailingHeartbeatRepository(InMemoryEvaluationOrchestrationRepository):
        async def heartbeat(self, **_kwargs: object) -> Mapping[str, int]:
            raise RuntimeError("database unavailable")

    metrics = _RecordingMetrics()
    scheduler = EvaluationSchedulerService(FailingHeartbeatRepository(), metrics=metrics)
    with pytest.raises(OrchestrationError, match="database_failure"):
        await scheduler.heartbeat_once()

    failures = [
        labels
        for name, labels, _value in metrics.records
        if name == "observatory_evaluation_dependency_outcome_count"
        and labels == {"operation": "postgresql", "outcome": "failed"}
    ]
    assert len(failures) == 1
    heartbeat_spans = [
        span
        for span in metrics.spans
        if span.get("operation") == "heartbeat" and span.get("outcome") == "failure"
    ]
    assert len(heartbeat_spans) == 1
    assert heartbeat_spans[0].get("dependency") == "postgresql"


@pytest.mark.asyncio
async def test_scheduler_replicas_claim_one_due_occurrence_at_a_barrier() -> None:
    class BarrierRepository(InMemoryEvaluationOrchestrationRepository):
        def __init__(self) -> None:
            super().__init__()
            self.arrived = 0
            self.ready = asyncio.Event()

        async def materialize_due_schedules(
            self, *, now: datetime, limit: int, lease_token: str
        ) -> int:
            self.arrived += 1
            if self.arrived == 2:
                self.ready.set()
            await self.ready.wait()
            return await super().materialize_due_schedules(
                now=now, limit=limit, lease_token=lease_token
            )

    repository = BarrierRepository()
    due = datetime.now(UTC) - timedelta(seconds=5)
    template = _request(deadline=due + timedelta(seconds=30))
    schedule = EvaluationSchedule(
        schedule_id=str(uuid4()),
        occurrence_id=str(uuid4()),
        dataset_id=template.dataset_id,
        dataset_version=template.dataset_version,
        suite_id=template.suite_id,
        suite_version=template.suite_version,
        evaluator_id=template.evaluator_id,
        evaluator_version=template.evaluator_version,
        next_run_at=due,
        cadence_seconds=60,
        deadline_seconds=30,
        max_attempts=3,
        request_envelope=template.envelope,
    )
    repository.schedules[schedule.schedule_id] = schedule
    first = EvaluationSchedulerService(repository, worker_id="scheduler-a", batch_size=1)
    second = EvaluationSchedulerService(repository, worker_id="scheduler-b", batch_size=1)

    results = await asyncio.gather(first.run_once(now=due), second.run_once(now=due))
    assert sorted(results) == [0, 1]
    assert len(repository.jobs) == 1
    assert len(repository.outbox) == 1


@pytest.mark.asyncio
async def test_expired_lease_recovers_to_queue() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    now = datetime.now(UTC)
    job = await service.admit(_request(deadline=now + timedelta(minutes=5)))
    claimed = await repository.claim_due_attempts(
        now=now + timedelta(seconds=1), limit=1, lease_seconds=1, worker_id="test"
    )
    assert claimed
    assert await service.recover_once(now=now + timedelta(seconds=2)) == 1
    assert repository.jobs[job.job_id].state.value == "queued"


@pytest.mark.asyncio
async def test_scheduler_materializes_occurrence_before_advancing_schedule() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    due = datetime.now(UTC) - timedelta(seconds=5)
    template = _request(deadline=due + timedelta(seconds=30))
    schedule = EvaluationSchedule(
        schedule_id=str(uuid4()),
        occurrence_id=str(uuid4()),
        dataset_id=template.dataset_id,
        dataset_version=template.dataset_version,
        suite_id=template.suite_id,
        suite_version=template.suite_version,
        evaluator_id=template.evaluator_id,
        evaluator_version=template.evaluator_version,
        next_run_at=due,
        cadence_seconds=60,
        deadline_seconds=30,
        max_attempts=3,
        request_envelope=template.envelope,
    )
    repository.schedules[schedule.schedule_id] = schedule
    scheduler = EvaluationSchedulerService(repository, batch_size=1)

    assert await scheduler.run_once(now=datetime.now(UTC)) == 1
    assert len(repository.jobs) == 1
    assert len(repository.outbox) == 1
    assert repository.schedules[schedule.schedule_id].next_run_at == due + timedelta(seconds=60)
    assert repository.audits[0]["event"] == "schedule_materialized"

    assert await scheduler.run_once(now=due + timedelta(seconds=30)) == 0


@pytest.mark.asyncio
async def test_outbox_claim_is_exclusive_and_cancelled_request_is_not_published() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_request())
    now = datetime.now(UTC)
    first = await repository.claim_outbox(now=now, limit=1, worker_id="a")
    second = await repository.claim_outbox(now=now, limit=1, worker_id="b")
    assert len(first) == 1
    assert not second

    await service.cancel(job.job_id, now=now)
    await repository.mark_outbox_failed(
        first[0].event_id, "transport_unavailable", at=now, lease_token=first[0].lease_token
    )
    retry = await repository.claim_outbox(now=now + timedelta(seconds=20), limit=10, worker_id="c")
    assert not any(entry.event_type == "evaluation.runner.requested" for entry in retry)


@pytest.mark.asyncio
async def test_terminal_metric_covers_cancel_and_deadline_transitions_once() -> None:
    class Metrics:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict[str, str]]] = []

        def record(
            self, name: str, _value: float = 1.0, labels: Mapping[str, str] | None = None
        ) -> None:
            self.records.append((name, dict(labels or {})))

        def set(self, *_args: object, **_kwargs: object) -> None:
            return None

    metrics = Metrics()
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    cancelled = await service.admit(_request())
    assert await service.cancel(cancelled.job_id)
    deadline_request = _request()
    await service.admit(deadline_request)
    await service.dispatch_once(now=deadline_request.deadline_at + timedelta(seconds=1))

    terminal = [
        labels
        for name, labels in metrics.records
        if name == "observatory_evaluation_terminal_count"
    ]
    assert terminal.count({"outcome": "cancelled"}) == 1
    assert terminal.count({"outcome": "deadline"}) == 1


@pytest.mark.asyncio
async def test_cancelled_transport_durably_releases_outbox_before_propagating() -> None:
    class CancelledTransport:
        async def publish(self, *_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError

    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository, transport=CancelledTransport())  # type: ignore[arg-type]
    await service.admit(_request())

    with pytest.raises(asyncio.CancelledError):
        await service.publish_outbox_once(now=datetime.now(UTC) + timedelta(seconds=1))

    entry = next(iter(repository.outbox.values()))
    assert entry.publish_attempts == 1
    assert entry.last_error_code == "transport_unavailable"
    assert entry.lease_token is None
    assert entry.lease_expires_at is None


@pytest.mark.asyncio
async def test_metric_failure_cannot_strand_failed_outbox_lease() -> None:
    class FailingTransport:
        async def publish(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("transport unavailable")

    class FailingMetrics:
        def set(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("metric persistence unavailable")

        def record(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("metric persistence unavailable")

    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(
        repository, transport=FailingTransport(), metrics=FailingMetrics()  # type: ignore[arg-type]
    )
    await service.admit(_request())

    assert await service.publish_outbox_once(now=datetime.now(UTC) + timedelta(seconds=1)) == 0
    entry = next(iter(repository.outbox.values()))
    assert entry.publish_attempts == 1
    assert entry.last_error_code == "transport_unavailable"
    assert entry.lease_token is None


@pytest.mark.asyncio
async def test_process_lease_identity_is_uuid_shaped_for_human_readable_defaults() -> None:
    repository = _HeartbeatRepository()
    orchestration = EvaluationOrchestrationService(repository)
    scheduler = EvaluationSchedulerService(repository)

    await orchestration.heartbeat_once()
    await scheduler.heartbeat_once()

    assert len(repository.lease_tokens) == 2
    assert repository.lease_tokens[0] != repository.lease_tokens[1]
