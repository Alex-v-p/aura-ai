"""AURA-0092 evaluation worker lifecycle coverage."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aura_observatory.entrypoints import evaluation_worker
from aura_observatory.evaluation_orchestration import OrchestrationDependencyError
from aura_observatory.evaluation_transport import NatsTransportError


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Metrics:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, str]]] = []

    def record(self, name: str, _value: float, labels: dict[str, str]) -> None:
        self.records.append((name, labels))


class _Service:
    def __init__(self, *, fail_consumer: bool = False, fail_publish: bool = False) -> None:
        self.consumer_started = asyncio.Event()
        self.consumer_stopped = False
        self.fail_consumer = fail_consumer
        self.fail_publish = fail_publish
        self.metrics = _Metrics()
        self.recoveries = 0
        self.dispatches = 0
        self.outbox_publishes = 0

    async def consume_results(self, stop: asyncio.Event) -> None:
        self.consumer_started.set()
        if self.fail_consumer:
            raise RuntimeError("transport_failure")
        await stop.wait()
        self.consumer_stopped = True

    async def recover_once(self) -> int:
        self.recoveries += 1
        return 0

    async def dispatch_once(self) -> int:
        self.dispatches += 1
        return 0

    async def publish_outbox_once(self) -> int:
        self.outbox_publishes += 1
        if self.fail_publish:
            self.metrics.record(
                "observatory_evaluation_dependency_outcome_count",
                1.0,
                {"operation": "evaluation_nats", "outcome": "failed"},
            )
        return 0


def _settings() -> Any:
    settings = SimpleNamespace(
        evaluation_nats_url="nats://test",
        evaluation_max_envelope_bytes=1024,
        evaluation_poll_seconds=0.01,
        evaluation_shutdown_timeout_seconds=0.5,
    )
    settings.evaluation_nats_credential = lambda: ("user", "password")
    return settings


@pytest.mark.asyncio
async def test_worker_supervises_result_consumer_and_shuts_it_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    service = _Service()
    monkeypatch.setattr(evaluation_worker, "connect_nats", _connect(connection))
    monkeypatch.setattr(evaluation_worker, "NatsEvaluationTransport", _transport)
    monkeypatch.setattr(evaluation_worker, "build_evaluation_orchestration", _build(service))

    async def stop() -> None:
        await service.consumer_started.wait()

    await evaluation_worker.run(_settings(), stop())

    assert service.consumer_stopped
    assert service.recoveries >= 1
    assert service.dispatches >= 1
    assert service.outbox_publishes >= 1
    assert connection.closed


@pytest.mark.asyncio
async def test_worker_recovers_consumer_failure_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    service = _Service(fail_consumer=True, fail_publish=True)
    monkeypatch.setattr(evaluation_worker, "connect_nats", _connect(connection))
    monkeypatch.setattr(evaluation_worker, "NatsEvaluationTransport", _transport)
    monkeypatch.setattr(evaluation_worker, "build_evaluation_orchestration", _build(service))

    async def stop() -> None:
        await service.consumer_started.wait()
        await asyncio.sleep(0.03)

    await evaluation_worker.run(_settings(), stop())

    assert connection.closed
    assert service.recoveries >= 1
    assert service.dispatches >= 1
    assert service.outbox_publishes >= 1
    assert (
        "observatory_evaluation_dependency_outcome_count",
        {"operation": "evaluation_nats", "outcome": "failed"},
    ) in service.metrics.records


@pytest.mark.asyncio
async def test_worker_initial_nats_failure_is_bounded_and_does_not_raise_provider_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_connect(*_args: Any, **_kwargs: Any) -> _Connection:
        raise NatsTransportError("transport_unavailable")

    monkeypatch.setattr(evaluation_worker, "connect_nats", fail_connect)
    await evaluation_worker.run(_settings())


def _connect(connection: _Connection) -> Any:
    async def connect(*_args: Any, **_kwargs: Any) -> _Connection:
        return connection

    return connect


def _transport(*_args: Any, **_kwargs: Any) -> object:
    return object()


def _build(service: _Service) -> Any:
    def build(*_args: Any, **_kwargs: Any) -> _Service:
        return service

    return build


def test_worker_classifies_database_dependency_without_provider_diagnostics() -> None:
    service = _Service()
    service.metrics.record(
        "observatory_evaluation_dependency_outcome_count",
        1.0,
        {"operation": "postgresql", "outcome": "failed"},
    )
    before = len(service.metrics.records)
    evaluation_worker._record_failure(  # pyright: ignore[reportPrivateUsage]
        service, OrchestrationDependencyError("database_failure", "postgresql")
    )
    assert len(service.metrics.records) == before
