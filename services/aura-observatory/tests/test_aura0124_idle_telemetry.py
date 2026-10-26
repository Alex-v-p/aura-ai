"""AURA-0124 acceptance coverage for bounded idle evaluation telemetry.

These tests exercise the application services with deterministic repository
seams.  The repository methods still run on every call (the polling cadence is
not under test here), while successful telemetry is expected only for useful
work and sparse heartbeats.  Failures remain visible immediately.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from aura_observatory.evaluation_orchestration import (
    EvaluationOrchestrationService,
    EvaluationSchedulerService,
    OrchestrationDependencyError,
)


class _Metrics:
    def __init__(self) -> None:
        self.records: list[tuple[str, float, dict[str, str]]] = []
        self.spans: list[dict[str, object]] = []

    def record(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.records.append((name, value, dict(labels or {})))

    def set(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.records.append((name, value, dict(labels or {})))

    def record_component_span(self, **span: object) -> None:
        self.spans.append(span)

    def successful_spans(self) -> list[dict[str, object]]:
        return [span for span in self.spans if span.get("outcome") == "success"]

    def dependency_successes(self) -> list[tuple[str, float, dict[str, str]]]:
        return [
            record
            for record in self.records
            if record[0] == "observatory_evaluation_dependency_outcome_count"
            and record[2].get("outcome") == "success"
        ]


class _SchedulerRepository:
    def __init__(self, materialized: int = 0, *, failure: Exception | None = None) -> None:
        self.materialized = materialized
        self.failure = failure
        self.calls = 0
        self.heartbeat_calls: list[datetime] = []

    async def materialize_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> int:
        del now, limit, lease_token
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.materialized

    async def heartbeat(
        self,
        *,
        component_id: str,
        lease_token: str,
        at: datetime,
        backlog_count: int = 0,
    ) -> Mapping[str, int]:
        del component_id, lease_token, backlog_count
        self.heartbeat_calls.append(at)
        return {}


class _OrchestrationRepository:
    def __init__(
        self,
        *,
        recovered: int = 0,
        dispatched: int = 0,
        local_work: object | None = None,
        enqueued: int = 0,
        outbox: list[object] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.recovered = recovered
        self.dispatched = dispatched
        self.local_work = local_work
        self.enqueued = enqueued
        self.outbox = outbox or []
        self.failure = failure
        self.calls: list[str] = []
        self.heartbeat_calls: list[datetime] = []

    def _check(self, operation: str) -> None:
        self.calls.append(operation)
        if self.failure is not None:
            raise self.failure

    async def settle_expired(self, *, now: datetime, limit: int) -> int:
        del now, limit
        self._check("settle_expired")
        return 0

    async def recover(self, *, now: datetime, limit: int, worker_id: str) -> int:
        del now, limit, worker_id
        self._check("recover")
        return self.recovered

    async def claim_due_attempts(
        self, *, now: datetime, limit: int, lease_seconds: int, worker_id: str
    ) -> list[object]:
        del now, limit, lease_seconds, worker_id
        self._check("claim_due_attempts")
        return [object()] * self.dispatched

    async def claim_local_work(
        self, *, now: datetime, lease_seconds: int, worker_id: str
    ) -> object | None:
        del now, lease_seconds, worker_id
        self._check("claim_local_work")
        return self.local_work

    async def enqueue_pending_local_work(self, *, now: datetime, limit: int = 32) -> int:
        del now, limit
        self._check("enqueue_pending_local_work")
        return self.enqueued

    async def claim_outbox(
        self, *, now: datetime, limit: int, lease_seconds: int = 120, worker_id: str = ""
    ) -> list[object]:
        del now, limit, lease_seconds, worker_id
        self._check("claim_outbox")
        return self.outbox

    async def mark_outbox_published(
        self, event_id: str, *, at: datetime, lease_token: str | None = None
    ) -> bool:
        del event_id, at, lease_token
        self._check("mark_outbox_published")
        return True

    async def heartbeat(
        self,
        *,
        component_id: str,
        lease_token: str,
        at: datetime,
        backlog_count: int = 0,
    ) -> Mapping[str, int]:
        del component_id, lease_token, backlog_count
        self._check("heartbeat")
        self.heartbeat_calls.append(at)
        return {}


class _Transport:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, intent: object, event_id: str) -> None:
        del intent
        self.published.append(event_id)


def _span_operations(metrics: _Metrics) -> set[str]:
    return {str(span["operation"]) for span in metrics.spans}


@pytest.mark.asyncio
async def test_empty_scheduler_cycle_keeps_polling_but_emits_no_success_telemetry() -> None:
    repository = _SchedulerRepository(materialized=0)
    metrics = _Metrics()
    service = EvaluationSchedulerService(repository, metrics=metrics)

    assert await service.run_once(now=datetime(2026, 1, 1, tzinfo=UTC)) == 0

    assert repository.calls == 1
    assert metrics.successful_spans() == []
    assert metrics.dependency_successes() == []
    assert not any(name.endswith("occurrence_count") for name, _, _ in metrics.records)


@pytest.mark.asyncio
async def test_scheduler_cycle_with_materialized_work_retains_success_telemetry() -> None:
    repository = _SchedulerRepository(materialized=2)
    metrics = _Metrics()
    service = EvaluationSchedulerService(repository, metrics=metrics)

    assert await service.run_once(now=datetime(2026, 1, 1, tzinfo=UTC)) == 2

    assert "schedule_materialize" in _span_operations(metrics)
    assert len(metrics.dependency_successes()) == 1
    assert any(
        name == "observatory_evaluation_scheduler_occurrence_count" and value == 2
        for name, value, _ in metrics.records
    )


@pytest.mark.asyncio
async def test_scheduler_failure_remains_immediately_visible() -> None:
    repository = _SchedulerRepository(failure=RuntimeError("database unavailable"))
    metrics = _Metrics()
    service = EvaluationSchedulerService(repository, metrics=metrics)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.run_once(now=datetime(2026, 1, 1, tzinfo=UTC))

    assert any(
        span.get("operation") == "schedule_materialize"
        and span.get("outcome") == "failure"
        for span in metrics.spans
    )


@pytest.mark.asyncio
async def test_empty_evaluation_cycles_suppress_success_spans_and_dependency_counters() -> None:
    repository = _OrchestrationRepository()
    metrics = _Metrics()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert await service.recover_once(now=now) == 0
    assert await service.dispatch_once(now=now) == 0
    assert await service.claim_local_work() is None
    assert await service.enqueue_pending_local_work() == 0
    service.transport = _Transport()
    assert await service.publish_outbox_once(now=now) == 0

    assert {
        "settle_expired",
        "recover",
        "claim_due_attempts",
        "claim_local_work",
        "enqueue_pending_local_work",
        "claim_outbox",
    } <= set(repository.calls)
    assert metrics.successful_spans() == []
    assert metrics.dependency_successes() == []


@pytest.mark.asyncio
async def test_evaluation_cycles_with_work_retain_success_telemetry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cases: list[tuple[str, EvaluationOrchestrationService, Any, str]] = [
        (
            "recover",
            EvaluationOrchestrationService(
                _OrchestrationRepository(recovered=1), metrics=_Metrics()
            ),
            1,
            "recover",
        ),
        (
            "dispatch",
            EvaluationOrchestrationService(
                _OrchestrationRepository(dispatched=1), metrics=_Metrics()
            ),
            1,
            "dispatch",
        ),
        (
            "claim_local_work",
            EvaluationOrchestrationService(
                _OrchestrationRepository(local_work=object()), metrics=_Metrics()
            ),
            True,
            "dispatch",
        ),
        (
            "enqueue_pending_local_work",
            EvaluationOrchestrationService(
                _OrchestrationRepository(enqueued=1), metrics=_Metrics()
            ),
            1,
            "dispatch",
        ),
    ]
    for operation, service, expected, span_operation in cases:
        if operation == "recover":
            result = await service.recover_once(now=now)
        elif operation == "dispatch":
            result = await service.dispatch_once(now=now)
        elif operation == "claim_local_work":
            result = await service.claim_local_work()
        else:
            result = await service.enqueue_pending_local_work()
        assert bool(result) is bool(expected)
        metrics = service.metrics
        assert isinstance(metrics, _Metrics)
        assert any(
            span.get("operation") == span_operation and span.get("outcome") == "success"
            for span in metrics.spans
        )


@pytest.mark.asyncio
async def test_heartbeat_keeps_database_cadence_but_is_sparse_at_thirty_seconds() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repository = _OrchestrationRepository()
    metrics = _Metrics()
    clock = [0.0]
    service = EvaluationOrchestrationService(
        repository,
        metrics=metrics,
        heartbeat_clock=lambda: clock[0],
    )

    for offset in (0, 10, 29, 30, 31, 59):
        clock[0] = float(offset)
        await service.heartbeat_once(now=now + timedelta(seconds=offset))

    assert len(repository.heartbeat_calls) == 6
    heartbeat_spans = [
        span
        for span in metrics.spans
        if span.get("operation") == "heartbeat" and span.get("outcome") == "success"
    ]
    assert len(heartbeat_spans) == 2

    scheduler_repository = _SchedulerRepository()
    scheduler_metrics = _Metrics()
    scheduler_clock = [0.0]
    scheduler = EvaluationSchedulerService(
        scheduler_repository,
        metrics=scheduler_metrics,
        heartbeat_clock=lambda: scheduler_clock[0],
    )
    for offset in (0, 10, 30):
        scheduler_clock[0] = float(offset)
        await scheduler.heartbeat_once(now=now + timedelta(seconds=offset))

    assert len(scheduler_repository.heartbeat_calls) == 3
    assert len(scheduler_metrics.successful_spans()) == 2


@pytest.mark.asyncio
async def test_heartbeat_failure_is_emitted_without_waiting_for_sparse_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repository = _OrchestrationRepository()
    metrics = _Metrics()
    service = EvaluationOrchestrationService(repository, metrics=metrics)
    await service.heartbeat_once(now=now)

    repository.failure = RuntimeError("database unavailable")
    with pytest.raises(OrchestrationDependencyError):
        await service.heartbeat_once(now=now + timedelta(seconds=1))

    assert any(
        span.get("operation") == "heartbeat" and span.get("outcome") == "failure"
        for span in metrics.spans
    )
    assert any(
        name == "observatory_evaluation_dependency_outcome_count"
        and labels.get("outcome") == "failed"
        for name, _, labels in metrics.records
    )


@pytest.mark.asyncio
async def test_publish_with_outbox_work_retains_publication_and_success_telemetry() -> None:
    entry = SimpleNamespace(
        event_type="evaluation.runner.requested",
        envelope={"messageId": "event-1"},
        event_id="event-1",
        lease_token=None,
    )
    repository = _OrchestrationRepository(outbox=[entry])
    metrics = _Metrics()
    transport = _Transport()
    service = EvaluationOrchestrationService(repository, transport=transport, metrics=metrics)

    assert await service.publish_outbox_once(now=datetime(2026, 1, 1, tzinfo=UTC)) == 1

    assert transport.published == ["event-1"]
    assert any(
        span.get("operation") == "publish" and span.get("outcome") == "success"
        for span in metrics.spans
    )
    assert any(
        name == "observatory_evaluation_dependency_outcome_count"
        and labels.get("outcome") == "published"
        for name, _, labels in metrics.records
    )
