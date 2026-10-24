"""AURA-0109 bounded batch claim and ClickHouse write tests."""
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUntypedFunctionDecorator=false

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.memory import MemoryObservationIdRegistry
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
from aura_observatory.contract import MetadataObservation
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import BatchClaim, ClaimOutcome, InsertOutcome


def _payload(observation_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "observationId": observation_id,
        "observedAt": "2026-01-01T00:00:00Z",
        "kind": "metric",
        "component": {"id": "aura.runtime.model_inference", "version": "1.0.0"},
        "operation": "inference",
        "measurement": {"name": "duration_ms", "value": 1.0, "unit": "milliseconds"},
        "dimensions": {"outcome": "success"},
        "trace": {"traceId": "abcdefabcdefabcdefabcdefabcdefab", "spanId": "abcdefabcdefabcd"},
        "correlation": {"runId": "fedcba98-7654-4fed-8123-fedcba987654"},
        "deployment": {
            "environment": "test",
            "deploymentId": "deployment-test",
            "gitCommit": "0123456789abcdef01234567",
            "hostId": "host-test",
        },
        "serviceRole": "worker",
        "capturePolicy": "metadata_only",
    }


def _observation(number: int) -> MetadataObservation:
    return MetadataObservation.from_payload(
        _payload(f"00000000-0000-4000-8000-{number:012d}")
    )


class BatchRegistry:
    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.settled: list[tuple[str, int]] = []
        self.aborted: list[str] = []

    async def claim_batch(self, observations: Sequence[tuple[str, datetime]]) -> list[BatchClaim]:
        self.claimed = [item[0] for item in observations]
        return [
            BatchClaim(observation_id, ClaimOutcome.ACQUIRED, number + 1)
            for number, (observation_id, _observed_at) in enumerate(observations)
        ]

    async def settle_batch(self, successful: Sequence[tuple[str, int]]) -> None:
        self.settled = list(successful)

    async def abort_batch(self, observation_ids: Sequence[str]) -> None:
        self.aborted = list(observation_ids)


class _FakeResult:
    def __init__(self, rows: Sequence[tuple[Any, ...]] = ()) -> None:
        self.rows = list(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _BatchConnection:
    def __init__(
        self,
        observation_id: str,
        *,
        block_claim_insert: bool = False,
        advisory_lock: asyncio.Lock | None = None,
        existing_pending_sequence: int | None = None,
    ) -> None:
        self.observation_id = observation_id
        self.block_claim_insert = block_claim_insert
        self.advisory_lock = advisory_lock
        self.existing_pending_sequence = existing_pending_sequence
        self.claim_insert_started = asyncio.Event()
        self.claim_insert_release = asyncio.Event()
        if not block_claim_insert:
            self.claim_insert_release.set()
        self.lock_held = False
        self.unlock_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.events: list[str] = []
        self.queries: list[str] = []
        self.status = "pending"
        self.deleted = False
        self.observed_at_updates = 0
        self.sequence_updates = 0

    async def execute(self, query: str, _params: tuple[Any, ...] = ()) -> _FakeResult:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "pg_advisory_lock" in normalized:
            if self.advisory_lock is not None:
                await self.advisory_lock.acquire()
            self.lock_held = True
            return _FakeResult()
        if "pg_advisory_unlock" in normalized:
            self.events.append("unlock")
            self.lock_held = False
            if self.advisory_lock is not None and self.advisory_lock.locked():
                self.advisory_lock.release()
            self.unlock_count += 1
            return _FakeResult()
        if normalized.startswith("DELETE FROM observatory_observation_receipts"):
            self.deleted = True
            return _FakeResult()
        if "INSERT INTO observatory_observation_receipts" in normalized:
            self.claim_insert_started.set()
            await self.claim_insert_release.wait()
            if self.existing_pending_sequence is not None:
                return _FakeResult()
            return _FakeResult([(self.observation_id,)])
        if normalized.startswith("UPDATE observatory_observation_receipts SET status = 'pending'"):
            return _FakeResult()
        if "SELECT observation_id, status, ingest_sequence" in normalized:
            sequence = self.existing_pending_sequence or 1
            return _FakeResult([(self.observation_id, self.status, sequence)])
        if "SELECT next_sequence FROM observatory_ingest_allocator" in normalized:
            return _FakeResult([(1,)])
        if "UPDATE observatory_ingest_allocator" in normalized:
            return _FakeResult()
        if "INSERT INTO observatory_ingest_visibility" in normalized:
            return _FakeResult()
        if "UPDATE observatory_observation_receipts AS receipt SET observed_at" in normalized:
            if "SET observed_at = batch_values.observed_at" not in normalized:
                raise AssertionError("observed_at update must use the UNNEST relation alias")
            self.observed_at_updates += 1
            return _FakeResult()
        if "UPDATE observatory_observation_receipts AS receipt SET ingest_sequence" in normalized:
            if "SET ingest_sequence = batch_values.ingest_sequence" not in normalized:
                raise AssertionError("sequence update must use the UNNEST relation alias")
            self.sequence_updates += 1
            return _FakeResult()
        if "UPDATE observatory_observation_receipts AS receipt SET status" in normalized:
            self.status = "completed"
            return _FakeResult()
        if "SELECT committed_sequence" in normalized:
            return _FakeResult([(0,)])
        if "UPDATE observatory_ingest_watermark" in normalized:
            return _FakeResult()
        if "SELECT MIN(sequence)" in normalized:
            return _FakeResult([(None, 1)])
        if "SELECT visible FROM observatory_ingest_visibility" in normalized:
            return _FakeResult([(True,)])
        raise AssertionError(f"unexpected SQL in test double: {normalized}")

    async def commit(self) -> None:
        self.events.append("commit")
        self.commit_count += 1

    async def rollback(self) -> None:
        self.events.append("rollback")
        self.rollback_count += 1


class _BatchContext:
    def __init__(self, connection: _BatchConnection) -> None:
        self.connection = connection
        self.exited = False

    async def __aenter__(self) -> _BatchConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True


class _BatchFactory:
    def __init__(self, connection: _BatchConnection) -> None:
        self.connection = connection
        self.contexts: list[_BatchContext] = []

    def __call__(self) -> _BatchContext:
        context = _BatchContext(self.connection)
        self.contexts.append(context)
        return context


@pytest.mark.asyncio
async def test_batch_writes_share_client_and_respect_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BatchRegistry()
    requests: list[dict[str, Any]] = []
    active = 0
    max_active = 0
    clients = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            nonlocal clients
            clients += 1
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> Response:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            requests.append(kwargs)
            await asyncio.sleep(0)
            active -= 1
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    observations = [_observation(number) for number in range(1, 9)]
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=2
    )

    result = await store.insert_observations(observations)

    assert set(result.values()) == {InsertOutcome.ACCEPTED}
    assert clients == 1
    assert max_active <= 2
    assert len(registry.claimed) == 8
    assert len(registry.settled) == 8
    assert [
        request["content"].split("\n", 1)[0]
        for request in requests
    ] == [
        "INSERT INTO observations SETTINGS insert_deduplicate=1, "
        f"insert_deduplication_token='{item.observation_id}' FORMAT JSONEachRow"
        for item in observations
    ]
    assert all(
        json.loads(request["content"].split("\n", 1)[1])["ingest_sequence"] == index
        for index, request in enumerate(requests, 1)
    )


@pytest.mark.asyncio
async def test_batch_writes_complete_in_configured_release_waves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The semaphore must produce finite waves, not merely a max-active peak."""

    registry = BatchRegistry()
    concurrency = 2
    wave_count = 4
    wave_started = [asyncio.Event() for _ in range(wave_count)]
    wave_release = [asyncio.Event() for _ in range(wave_count)]
    wave_sizes = [0] * wave_count
    active = 0
    maximum_active = 0
    current_wave = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            nonlocal active, maximum_active
            wave = current_wave
            wave_sizes[wave] += 1
            wave_started[wave].set()
            active += 1
            maximum_active = max(maximum_active, active)
            await wave_release[wave].wait()
            active -= 1
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=concurrency
    )
    task = asyncio.create_task(
        store.insert_observations([_observation(number) for number in range(1, 8)])
    )

    for wave, expected_size in enumerate((2, 2, 2, 1)):
        await asyncio.wait_for(wave_started[wave].wait(), timeout=1)
        assert wave_sizes[wave] == expected_size
        assert maximum_active <= concurrency
        current_wave += 1
        wave_release[wave].set()

    result = await asyncio.wait_for(task, timeout=1)
    assert set(result.values()) == {InsertOutcome.ACCEPTED}
    assert wave_sizes == [2, 2, 2, 1]
    assert maximum_active == concurrency


@pytest.mark.asyncio
async def test_max_configured_batch_is_processed_in_bounded_task_waves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full configured batch creates only one bounded gather wave at a time."""

    import aura_observatory.adapters.clickhouse as clickhouse_adapter

    registry = BatchRegistry()
    concurrency = 8
    gather_sizes: list[int] = []
    original_gather = clickhouse_adapter.asyncio.gather

    async def observed_gather(*aws: Any, **kwargs: Any) -> Any:
        gather_sizes.append(len(aws))
        return await original_gather(*aws, **kwargs)

    monkeypatch.setattr(clickhouse_adapter.asyncio, "gather", observed_gather)
    active = 0
    maximum_active = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=concurrency
    )
    observations = [_observation(number) for number in range(1, 5001)]

    result = await store.insert_observations(observations)

    assert len(result) == 5000
    assert set(result.values()) == {InsertOutcome.ACCEPTED}
    assert gather_sizes == [concurrency] * 625
    assert maximum_active == concurrency
    assert len(registry.settled) == 5000


@pytest.mark.asyncio
async def test_batch_mixed_claim_outcomes_settle_only_acquired_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MixedRegistry(BatchRegistry):
        async def claim_batch(
            self, observations: Sequence[tuple[str, datetime]]
        ) -> list[BatchClaim]:
            self.claimed = [item[0] for item in observations]
            outcomes = {
                self.claimed[0]: BatchClaim(self.claimed[0], ClaimOutcome.ACQUIRED, 11),
                self.claimed[1]: BatchClaim(self.claimed[1], ClaimOutcome.DUPLICATE),
                self.claimed[2]: BatchClaim(self.claimed[2], ClaimOutcome.PENDING),
                self.claimed[3]: BatchClaim(self.claimed[3], ClaimOutcome.ACQUIRED, 14),
            }
            return [outcomes[item[0]] for item in observations]

    registry = MixedRegistry()
    requests: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> Response:
            requests.append(kwargs)
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    observations = [_observation(number) for number in range(1, 5)]
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=2
    )

    result = await store.insert_observations(observations)

    assert result[observations[0].observation_id] is InsertOutcome.ACCEPTED
    assert result[observations[1].observation_id] is InsertOutcome.DUPLICATE
    assert result[observations[2].observation_id] is InsertOutcome.RETRYABLE
    assert result[observations[3].observation_id] is InsertOutcome.ACCEPTED
    assert registry.settled == [
        (observations[0].observation_id, 11),
        (observations[3].observation_id, 14),
    ]
    assert registry.aborted == []
    assert [
        item["content"].split("\n", 1)[0]
        for item in requests
    ] == [
        "INSERT INTO observations SETTINGS insert_deduplicate=1, "
        f"insert_deduplication_token='{observations[0].observation_id}' FORMAT JSONEachRow",
        "INSERT INTO observations SETTINGS insert_deduplicate=1, "
        f"insert_deduplication_token='{observations[3].observation_id}' FORMAT JSONEachRow",
    ]


@pytest.mark.asyncio
async def test_provider_failure_reclaims_same_sequence_and_deduplication_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    registry = MemoryObservationIdRegistry(lease_seconds=10, clock=lambda: now[0])
    requests: list[dict[str, Any]] = []
    fail_first = True

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> Response:
            nonlocal fail_first
            requests.append(kwargs)
            if fail_first:
                fail_first = False
                raise TimeoutError("ambiguous provider response")
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    observation = _observation(1)
    store = ClickHouseObservationStore("http://clickhouse.test", id_registry=registry)

    first = await store.insert_observations([observation])
    now[0] = 11.0
    second = await store.insert_observations([observation])

    assert first[observation.observation_id] is InsertOutcome.RETRYABLE
    assert second[observation.observation_id] is InsertOutcome.ACCEPTED
    assert [
        json.loads(item["content"].split("\n", 1)[1])["ingest_sequence"]
        for item in requests
    ] == [1, 1]
    assert [
        item["content"].split("\n", 1)[0]
        for item in requests
    ] == [
        "INSERT INTO observations SETTINGS insert_deduplicate=1, "
        f"insert_deduplication_token='{observation.observation_id}' FORMAT JSONEachRow",
        "INSERT INTO observations SETTINGS insert_deduplicate=1, "
        f"insert_deduplication_token='{observation.observation_id}' FORMAT JSONEachRow",
    ]


@pytest.mark.asyncio
async def test_service_classifies_request_duplicates_and_persists_health_as_aggregates() -> None:
    class Store:
        async def insert_observations(
            self, observations: Sequence[MetadataObservation]
        ) -> dict[str, InsertOutcome]:
            return {item.observation_id: InsertOutcome.ACCEPTED for item in observations}

        async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
            return (await self.insert_observations([observation]))[observation.observation_id]

    class Catalog:
        async def record_component(self, component_id: str, version: str) -> None:
            del component_id, version
            return None

        async def record_deployment(
            self, deployment_id: str, git_commit: str, environment: str, host_id: str
        ) -> None:
            del deployment_id, git_commit, environment, host_id
            return None

        async def increment_counter(self, name: str, amount: int = 1) -> None:
            del name, amount
            return None

        async def counters(self) -> dict[str, int]:
            return {}

        async def last_ingest_at(self) -> datetime | None:
            return None

    class Health:
        def __init__(self) -> None:
            self.rejections: list[tuple[str, int]] = []
            self.duplicates: list[int] = []
            self.accepted: list[tuple[datetime, int]] = []
            self.latencies: list[tuple[str, float]] = []

        async def record_rejection(self, reason: str, amount: int = 1) -> None:
            self.rejections.append((reason, amount))

        async def record_duplicate(self, amount: int = 1) -> None:
            self.duplicates.append(amount)

        async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None:
            self.accepted.append((observed_at, amount))

        async def record_latency(self, kind: str, duration_ms: float) -> None:
            self.latencies.append((kind, duration_ms))

        async def record_storage_failure(self, _amount: int = 1) -> None:
            raise AssertionError("the aggregate request should not report a storage failure")

    valid = _payload("00000000-0000-4000-8000-000000000001")
    duplicate = dict(valid)
    invalid = {**valid, "observationId": "not-an-opaque-id"}
    health = Health()
    service = TelemetryIngestService(
        Store(), Catalog(), credential="secret", health_port=cast(Any, health)
    )

    result = await service.ingest([valid, duplicate, invalid], credential="secret")

    assert result["accepted"] == 1
    assert result["duplicate"] == 1
    assert result["rejected"] == 1
    assert health.accepted[0][1] == 1
    assert health.duplicates == [1]
    assert health.rejections == [("schema_validation", 1)]
    assert [kind for kind, _duration in health.latencies] == ["storage"]


@pytest.mark.asyncio
async def test_service_uses_one_set_based_idempotent_catalogue_batch() -> None:
    class Store:
        async def insert_observations(
            self, observations: Sequence[MetadataObservation]
        ) -> dict[str, InsertOutcome]:
            return {item.observation_id: InsertOutcome.ACCEPTED for item in observations}

        async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
            return (await self.insert_observations([observation]))[observation.observation_id]

    class Catalog:
        def __init__(self) -> None:
            self.batches: list[
                tuple[list[tuple[str, str]], list[tuple[str, str, str, str, Sequence[str]]]]
            ] = []
            self.single_calls = 0

        async def record_ingest_batch(
            self,
            components: Sequence[tuple[str, str]],
            deployments: Sequence[tuple[str, str, str, str, Sequence[str]]],
        ) -> None:
            self.batches.append((list(components), list(deployments)))

        async def record_component(self, _component_id: str, _version: str) -> None:
            self.single_calls += 1

        async def record_deployment(
            self, _deployment_id: str, _git_commit: str, _environment: str, _host_id: str
        ) -> None:
            self.single_calls += 1

        async def increment_counter(self, _name: str, _amount: int = 1) -> None:
            return None

        async def counters(self) -> dict[str, int]:
            return {}

        async def last_ingest_at(self) -> datetime | None:
            return None

    catalog = Catalog()
    service = TelemetryIngestService(Store(), cast(Any, catalog), credential="secret")
    first = _payload("00000000-0000-4000-8000-000000000001")
    second = _payload("00000000-0000-4000-8000-000000000002")
    second["serviceRole"] = "api"

    result = await service.ingest([first, second, dict(first)], credential="secret")

    assert result["accepted"] == 2
    assert result["duplicate"] == 1
    assert catalog.single_calls == 0
    assert len(catalog.batches) == 1
    components, deployments = catalog.batches[0]
    assert components == [("aura.runtime.model_inference", "1.0.0")]
    assert deployments == [
        (
            "deployment-test",
            "0123456789abcdef01234567",
            "test",
            "host-test",
            ("api", "worker"),
        )
    ]


@pytest.mark.asyncio
async def test_postgres_catalogue_uses_json_recordset_for_unequal_roles() -> None:
    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
            self.calls.append((query, params))

    class Context:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()
    catalog = PostgresCatalog(lambda: Context(connection))
    await catalog.record_ingest_batch(
        [],
        [
            ("deployment-a", "commit-a", "test", "host-a", ("api",)),
            ("deployment-b", "commit-b", "test", "host-b", ("worker", "scheduler")),
        ],
    )

    query, params = connection.calls[0]
    assert "jsonb_to_recordset(%s::jsonb)" in query
    assert "text[][]" not in query
    rows = json.loads(cast(str, params[0]))
    assert [row["roles"] for row in rows] == [["api"], ["worker", "scheduler"]]


@pytest.mark.asyncio
async def test_clickhouse_failure_logs_are_content_free(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    observation = _observation(1)
    endpoint = "http://sensitive-clickhouse.test:8123"
    password = "provider-password-not-for-logs"
    secret_body = (
        "Code: 516; provider exception contains "
        f"{observation.observation_id} {endpoint} {password}"
    )

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(
                500,
                text=secret_body,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    caplog.set_level(logging.INFO, logger="aura_observatory.adapters.clickhouse")
    store = ClickHouseObservationStore(
        endpoint,
        password=password,
        id_registry=cast(Any, BatchRegistry()),
    )

    result = await store.insert_observations([observation])

    assert result[observation.observation_id] is InsertOutcome.RETRYABLE
    logs = "\n".join(caplog.messages)
    assert observation.observation_id not in logs
    assert endpoint not in logs
    assert password not in logs
    assert "provider exception" not in logs
    assert "unclassified_error" in logs


@pytest.mark.asyncio
async def test_batch_storage_and_registry_emit_correct_content_free_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ObservatoryMetrics()
    registry = BatchRegistry()

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test",
        id_registry=cast(Any, registry),
        metrics=metrics,
    )
    await store.insert_observations([_observation(1)])

    connection = _BatchConnection(_observation(2).observation_id)
    production_registry = PostgresObservationIdRegistry(
        _BatchFactory(connection), metrics=metrics
    )
    await production_registry.claim_batch(
        [(_observation(2).observation_id, datetime(2026, 1, 1))]
    )
    await production_registry.abort_batch([_observation(2).observation_id])

    spans = metrics.component_spans
    assert {span.dependency for span in spans} >= {"clickhouse", "postgresql"}
    assert {span.operation for span in spans} == {"admit", "write"}
    assert all(span.outcome in {"complete", "partial", "failure"} for span in spans)
    assert all(span.error_code is None for span in spans)


@pytest.mark.asyncio
async def test_ingest_phase_spans_correlate_and_separate_catalogue_claim_write_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ObservatoryMetrics()
    observation = _observation(1)
    connection = _BatchConnection(observation.observation_id)
    registry = PostgresObservationIdRegistry(
        _BatchFactory(connection), lease_seconds=1, metrics=metrics
    )

    class Catalog:
        async def record_ingest_batch(
            self,
            _components: Sequence[tuple[str, str]],
            _deployments: Sequence[tuple[str, str, str, str, Sequence[str]]],
        ) -> None:
            return None

        async def increment_counter(self, _name: str, _amount: int = 1) -> None:
            return None

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=registry, metrics=metrics
    )
    service = TelemetryIngestService(
        store, cast(Any, Catalog()), credential="secret", metrics=metrics
    )

    result = await service.ingest([_payload(observation.observation_id)], credential="secret")

    assert result["accepted"] == 1
    spans = metrics.component_spans
    assert [
        (span.component_id, span.operation, span.dependency, span.outcome)
        for span in spans
    ] == [
        ("aura.observatory.telemetry_ingestion", "write", "postgresql", "complete"),
        ("aura.observatory.telemetry_storage", "admit", "postgresql", "complete"),
        ("aura.observatory.telemetry_storage", "write", "clickhouse", "complete"),
        ("aura.observatory.telemetry_storage", "write", "postgresql", "complete"),
    ]
    assert len({span.trace_id for span in spans}) == 1
    assert all(span.duration_ms > 0 for span in spans)


@pytest.mark.asyncio
async def test_clickhouse_partial_and_cancelled_spans_use_finite_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ObservatoryMetrics()
    registry = BatchRegistry()
    calls = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

    class PartialClient:
        async def __aenter__(self) -> PartialClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("provider unavailable")
            return Response()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient", PartialClient
    )
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), metrics=metrics
    )
    await store.insert_observations([_observation(1), _observation(2)])
    partial = metrics.component_spans[-1]
    assert partial.outcome == "partial"
    assert partial.error_code == "transport_unavailable"

    class CancelledClient:
        async def __aenter__(self) -> CancelledClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient", CancelledClient
    )
    with pytest.raises(asyncio.CancelledError):
        await store.insert_observations([_observation(3)])
    cancelled = metrics.component_spans[-1]
    assert cancelled.outcome == "failure"
    assert cancelled.error_code == "transport_unavailable"


@pytest.mark.asyncio
async def test_batch_registry_rejects_duplicate_identifiers_before_mutation() -> None:
    registry = MemoryObservationIdRegistry()
    observed_at = datetime(2026, 1, 1)

    with pytest.raises(ValueError, match="duplicate identifiers"):
        await registry.claim_batch([("same", observed_at), ("same", observed_at)])

    claims = await registry.claim_batch([("same", observed_at)])
    assert claims == [BatchClaim("same", ClaimOutcome.ACQUIRED, 1)]


@pytest.mark.asyncio
async def test_batch_failure_leaves_failed_receipts_pending_and_settles_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BatchRegistry()
    calls = 0

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("provider unavailable")
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    observations = [_observation(number) for number in range(1, 4)]
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=3
    )

    result = await store.insert_observations(observations)

    assert list(result.values()).count(InsertOutcome.ACCEPTED) == 2
    assert list(result.values()).count(InsertOutcome.RETRYABLE) == 1
    assert len(registry.settled) == 2
    assert registry.aborted == []


@pytest.mark.asyncio
async def test_batch_cancellation_releases_provider_batch_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BatchRegistry()

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    observations = [_observation(number) for number in range(1, 3)]
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=2
    )

    with pytest.raises(asyncio.CancelledError):
        await store.insert_observations(observations)

    assert registry.settled == []
    assert registry.aborted == [item.observation_id for item in observations]


@pytest.mark.asyncio
async def test_cancellation_during_settlement_releases_client_and_batch_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SettlementCancellationRegistry(BatchRegistry):
        async def settle_batch(self, successful: Sequence[tuple[str, int]]) -> None:
            self.settled = list(successful)
            raise asyncio.CancelledError()

    registry = SettlementCancellationRegistry()
    client_exited = False

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            nonlocal client_exited
            client_exited = True

        async def post(self, _endpoint: str, **_kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(Any, registry), max_concurrency=2
    )

    with pytest.raises(asyncio.CancelledError):
        await store.insert_observations([_observation(1), _observation(2)])

    assert client_exited
    assert len(registry.settled) == 2
    assert registry.aborted == [item.observation_id for item in [_observation(1), _observation(2)]]


@pytest.mark.asyncio
async def test_production_registry_claim_cancellation_releases_lock_and_context() -> None:
    observation_id = _observation(1).observation_id
    connection = _BatchConnection(observation_id, block_claim_insert=True)
    factory = _BatchFactory(connection)
    registry = PostgresObservationIdRegistry(factory, lease_seconds=1)
    task = asyncio.create_task(
        registry.claim_batch([(observation_id, datetime(2026, 1, 1))])
    )

    await asyncio.wait_for(connection.claim_insert_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.unlock_count == 1
    assert connection.rollback_count == 1
    assert connection.events.index("rollback") < connection.events.index("unlock")
    assert not connection.lock_held
    assert factory.contexts[0].exited


@pytest.mark.asyncio
async def test_production_registry_settlement_cancellation_releases_lock_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aura_observatory.adapters.postgres as postgres_adapter

    observation_id = _observation(1).observation_id
    connection = _BatchConnection(observation_id)
    factory = _BatchFactory(connection)
    registry = PostgresObservationIdRegistry(factory, lease_seconds=1)
    claims = await registry.claim_batch([(observation_id, datetime(2026, 1, 1))])
    assert claims == [BatchClaim(observation_id, ClaimOutcome.ACQUIRED, 1)]

    async def cancel_visibility(_connection: Any) -> bool:
        raise asyncio.CancelledError()

    monkeypatch.setattr(postgres_adapter, "_advance_visibility_batch", cancel_visibility)
    with pytest.raises(asyncio.CancelledError):
        await registry.settle_batch([(observation_id, 1)])

    assert connection.unlock_count == 1
    assert connection.rollback_count == 1
    assert connection.events.index("rollback") < connection.events.index("unlock")
    assert not connection.lock_held
    assert factory.contexts[0].exited


@pytest.mark.asyncio
async def test_production_batch_claim_casts_observation_ids_to_uuid() -> None:
    observation_id = _observation(1).observation_id
    connection = _BatchConnection(observation_id)
    registry = PostgresObservationIdRegistry(_BatchFactory(connection), lease_seconds=1)

    claims = await registry.claim_batch([(observation_id, datetime(2026, 1, 1))])
    assert claims == [BatchClaim(observation_id, ClaimOutcome.ACQUIRED, 1)]

    claim_sql = "\n".join(
        " ".join(query.split())
        for query in getattr(connection, "queries", [])
        if "observatory_observation_receipts" in query
    )
    assert "batch_values.observation_id::uuid" in claim_sql
    assert "ANY(%s::uuid[])" in claim_sql
    assert connection.observed_at_updates == 1
    assert connection.sequence_updates == 1
    await registry.abort_batch([observation_id])


@pytest.mark.asyncio
async def test_production_batch_claim_exposes_existing_pending_sequence() -> None:
    observation_id = _observation(1).observation_id
    connection = _BatchConnection(observation_id, existing_pending_sequence=3)
    registry = PostgresObservationIdRegistry(_BatchFactory(connection), lease_seconds=1)

    claims = await registry.claim_batch([(observation_id, datetime(2026, 1, 1))])

    assert claims == [BatchClaim(observation_id, ClaimOutcome.PENDING, 3)]
    assert "SELECT observation_id, status, ingest_sequence" in "\n".join(connection.queries)


@pytest.mark.asyncio
async def test_retention_barrier_cannot_delete_until_batch_guard_is_released() -> None:
    observation_id = _observation(1).observation_id
    advisory_lock = asyncio.Lock()
    batch_connection = _BatchConnection(observation_id, advisory_lock=advisory_lock)
    retention_connection = _BatchConnection(observation_id, advisory_lock=advisory_lock)
    factory = _BatchFactory(batch_connection)
    registry = PostgresObservationIdRegistry(factory, lease_seconds=1)
    claims = await registry.claim_batch([(observation_id, datetime(2026, 1, 1))])
    assert claims == [BatchClaim(observation_id, ClaimOutcome.ACQUIRED, 1)]

    retention_done = asyncio.Event()

    async def delete_for_retention() -> None:
        await retention_connection.execute("SELECT pg_advisory_lock(830078)")
        await retention_connection.execute(
            "DELETE FROM observatory_observation_receipts WHERE observation_id = %s"
        )
        await retention_connection.execute("SELECT pg_advisory_unlock(830078)")
        retention_done.set()

    deletion = asyncio.create_task(delete_for_retention())
    await asyncio.sleep(0)
    assert not retention_done.is_set()
    assert not retention_connection.deleted

    await registry.abort_batch([observation_id])
    await asyncio.wait_for(retention_done.wait(), timeout=1)
    assert retention_connection.deleted
    with pytest.raises(RuntimeError, match="guard is not active"):
        await registry.settle_batch([(observation_id, 1)])
    await deletion


@pytest.mark.asyncio
async def test_service_validates_complete_batch_before_batch_storage() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls: list[list[MetadataObservation]] = []

        async def insert_observations(
            self, observations: Sequence[MetadataObservation]
        ) -> dict[str, InsertOutcome]:
            self.calls.append(list(observations))
            return {item.observation_id: InsertOutcome.ACCEPTED for item in observations}

        async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
            return (await self.insert_observations([observation]))[observation.observation_id]

    class Catalog:
        def __init__(self) -> None:
            self.components: list[str] = []
            self.counter_values: dict[str, int] = {}

        async def record_component(self, component_id: str, version: str) -> None:
            del version
            self.components.append(component_id)

        async def record_deployment(
            self, deployment_id: str, git_commit: str, environment: str, host_id: str
        ) -> None:
            del deployment_id, git_commit, environment, host_id
            return None

        async def increment_counter(self, name: str, amount: int = 1) -> None:
            self.counter_values[name] = self.counter_values.get(name, 0) + amount

        async def counters(self) -> dict[str, int]:
            return dict(self.counter_values)

        async def last_ingest_at(self) -> datetime | None:
            return None

    store = Store()
    catalog = Catalog()
    service = TelemetryIngestService(store, catalog, credential="secret")
    valid = _payload("00000000-0000-4000-8000-000000000001")
    invalid = dict(valid)
    invalid["observationId"] = "not-an-opaque-id"

    result = await service.ingest([invalid, valid], credential="secret")

    assert result["accepted"] == 1
    assert result["rejected"] == 1
    assert len(store.calls) == 1
    assert [item.observation_id for item in store.calls[0]] == [valid["observationId"]]


@pytest.mark.asyncio
async def test_live_postgres_batch_claim_settle_abort_and_restart_recovery() -> None:
    """Exercise the production registry against an isolated PostgreSQL schema."""

    database_url = os.environ.get("AURA_AURA0109_POSTGRES_URL", "")
    if not database_url:
        pytest.skip("set AURA_AURA0109_POSTGRES_URL for live AURA-0109 coverage")
    psycopg = pytest.importorskip("psycopg")

    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0109_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(database_url)
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await connection.execute(f"SET search_path TO {quoted_schema}")
                yield connection
            finally:
                await connection.close()

        async def receipt(observation_id: str) -> tuple[str, int | None] | None:
            async with connection_factory() as connection:
                result = await connection.execute(
                    "SELECT status, ingest_sequence FROM observatory_observation_receipts "
                    "WHERE observation_id = %s",
                    (observation_id,),
                )
                row = await result.fetchone()
                return (str(row[0]), int(row[1]) if row[1] is not None else None) if row else None

        async def watermark() -> int:
            async with connection_factory() as connection:
                result = await connection.execute(
                    "SELECT committed_sequence FROM observatory_ingest_watermark "
                    "WHERE watermark_key = TRUE"
                )
                row = await result.fetchone()
                return int(row[0]) if row else 0

        observations = [_observation(number) for number in range(1, 4)]
        pairs = [(item.observation_id, item.observed_at) for item in observations]
        registry = PostgresObservationIdRegistry(connection_factory, lease_seconds=1)
        claims = await registry.claim_batch(pairs)
        assert [claim.outcome for claim in claims] == [ClaimOutcome.ACQUIRED] * 3
        sequences = [claim.ingest_sequence for claim in claims]
        assert sequences == list(range(1, 4))

        await registry.settle_batch(
            [(observations[0].observation_id, cast(int, sequences[0])),
             (observations[1].observation_id, cast(int, sequences[1]))]
        )
        assert await receipt(observations[0].observation_id) == ("completed", 1)
        assert await receipt(observations[2].observation_id) == ("pending", 3)
        assert await watermark() == 2

        restarted = PostgresObservationIdRegistry(connection_factory, lease_seconds=1)
        pending = await restarted.claim_batch(
            [(observations[2].observation_id, observations[2].observed_at)]
        )
        assert pending == [
            BatchClaim(observations[2].observation_id, ClaimOutcome.PENDING, 3)
        ]

        async with connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_observation_receipts SET updated_at = CURRENT_TIMESTAMP "
                "- INTERVAL '2 seconds' WHERE observation_id = %s",
                (observations[2].observation_id,),
            )
            await connection.commit()
        reclaimed = await restarted.claim_batch(
            [(observations[2].observation_id, observations[2].observed_at)]
        )
        assert reclaimed == [
            BatchClaim(observations[2].observation_id, ClaimOutcome.ACQUIRED, 3)
        ]
        await restarted.abort_batch([observations[2].observation_id])
        assert await receipt(observations[2].observation_id) == ("pending", 3)

        async with connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_observation_receipts SET updated_at = CURRENT_TIMESTAMP "
                "- INTERVAL '2 seconds' WHERE observation_id = %s",
                (observations[2].observation_id,),
            )
            await connection.commit()
        final = PostgresObservationIdRegistry(connection_factory, lease_seconds=1)
        acquired = await final.claim_batch(
            [(observations[2].observation_id, observations[2].observed_at)]
        )
        assert acquired == [
            BatchClaim(observations[2].observation_id, ClaimOutcome.ACQUIRED, 3)
        ]
        await final.settle_batch([(observations[2].observation_id, 3)])
        assert await watermark() == 3
        assert await PostgresObservationIdRegistry(
            connection_factory, lease_seconds=1
        ).claim_batch(pairs[:1]) == [
            BatchClaim(observations[0].observation_id, ClaimOutcome.DUPLICATE, 1)
        ]
    finally:
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


@pytest.mark.asyncio
async def test_live_postgres_catalog_batch_preserves_deployment_role_associations() -> None:
    database_url = os.environ.get("AURA_AURA0109_POSTGRES_URL", "")
    if not database_url:
        pytest.skip("set AURA_AURA0109_POSTGRES_URL for live AURA-0109 coverage")
    psycopg = pytest.importorskip("psycopg")

    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0109_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(database_url)
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async with await psycopg.AsyncConnection.connect(database_url) as connection:
                await connection.execute(f"SET search_path TO {quoted_schema}")
                yield connection

        components = [
            ("aura.runtime.model_inference", "1.0.0"),
            ("aura.runtime.retrieval", "2.0.0"),
        ]
        deployments = [
            ("deployment-a", "commit-a", "test", "host-a", ("worker", "api")),
            ("deployment-b", "commit-b", "test", "host-b", ("scheduler",)),
            ("deployment-c", "commit-c", "prod", "host-c", ("worker", "reviewer", "api")),
        ]
        catalog = PostgresCatalog(connection_factory)
        await catalog.record_ingest_batch(components, deployments)
        await catalog.record_ingest_batch(components, deployments)

        async with connection_factory() as connection:
            component_result = await connection.execute(
                "SELECT component_id, version FROM observatory_components "
                "ORDER BY component_id"
            )
            component_rows = await component_result.fetchall()
            deployment_result = await connection.execute(
                "SELECT deployment_id, git_commit, environment, host_id, service_roles "
                "FROM observatory_deployments ORDER BY deployment_id"
            )
            deployment_rows = await deployment_result.fetchall()

        assert [(str(row[0]), str(row[1])) for row in component_rows] == sorted(components)
        assert len(deployment_rows) == len(deployments)
        for row, expected in zip(deployment_rows, deployments, strict=True):
            assert tuple(str(value) for value in row[:4]) == expected[:4]
            assert set(str(value) for value in row[4]) == set(expected[4])
    finally:
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


@pytest.mark.asyncio
async def test_live_128_observations_reach_contiguous_watermark_without_lag() -> None:
    """Run the Collector-sized production batch when both stores are available."""

    postgres_url = os.environ.get("AURA_AURA0109_POSTGRES_URL", "")
    clickhouse_url = os.environ.get("AURA_AURA0109_CLICKHOUSE_URL", "")
    if not postgres_url or not clickhouse_url:
        pytest.skip(
            "set AURA_AURA0109_POSTGRES_URL and AURA_AURA0109_CLICKHOUSE_URL "
            "for live AURA-0109 coverage"
        )
    psycopg = pytest.importorskip("psycopg")
    from test_aura0094_clickhouse_integration import (
        _apply_all_migrations,
        _isolated_database,
    )

    postgres_migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0109_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(postgres_url)
    clickhouse_user = os.environ.get("AURA_AURA0109_CLICKHOUSE_USER", "default")
    clickhouse_password = os.environ.get("AURA_AURA0109_CLICKHOUSE_PASSWORD", "")
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(postgres_migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(postgres_url)
            try:
                await connection.execute(f"SET search_path TO {quoted_schema}")
                yield connection
            finally:
                await connection.close()

        with _isolated_database(clickhouse_url, clickhouse_user, clickhouse_password) as settings:
            _apply_all_migrations(settings)
            registry = PostgresObservationIdRegistry(connection_factory, lease_seconds=60)
            store = ClickHouseObservationStore(
                settings.clickhouse_url,
                database=settings.clickhouse_database,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password,
                id_registry=registry,
                max_concurrency=8,
            )
            observations = [_observation(number) for number in range(1, 129)]
            result = await asyncio.wait_for(store.insert_observations(observations), timeout=90)
            assert len(result) == 128
            assert set(result.values()) == {InsertOutcome.ACCEPTED}

            response = httpx.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                content=(
                    "SELECT count(), uniqExact(observation_id), max(ingest_sequence) "
                    "FROM observations FORMAT JSONEachRow"
                ),
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                timeout=20,
            )
            response.raise_for_status()
            row = json.loads(response.text.splitlines()[0])
            assert int(row["count()"] if "count()" in row else row["count()\n"]) == 128
            assert int(row["uniqExact(observation_id)"]) == 128

            async with connection_factory() as connection:
                watermark_result = await connection.execute(
                    "SELECT committed_sequence FROM observatory_ingest_watermark "
                    "WHERE watermark_key = TRUE"
                )
                watermark_row = await watermark_result.fetchone()
                lag_result = await connection.execute(
                    "SELECT count(*) FROM observatory_ingest_visibility "
                    "WHERE visible = FALSE"
                )
                lag_row = await lag_result.fetchone()
            assert watermark_row is not None
            assert int(watermark_row[0]) == 128
            assert lag_row is not None and int(lag_row[0]) == 0
    finally:
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


@pytest.mark.asyncio
async def test_live_telemetry_service_admits_128_through_catalog_registry_and_clickhouse() -> None:
    """Exercise the complete production admission path, not direct storage only."""

    postgres_url = os.environ.get("AURA_AURA0109_POSTGRES_URL", "")
    clickhouse_url = os.environ.get("AURA_AURA0109_CLICKHOUSE_URL", "")
    if not postgres_url or not clickhouse_url:
        pytest.skip(
            "set AURA_AURA0109_POSTGRES_URL and AURA_AURA0109_CLICKHOUSE_URL "
            "for live AURA-0109 coverage"
        )
    psycopg = pytest.importorskip("psycopg")
    from test_aura0094_clickhouse_integration import (
        _apply_all_migrations,
        _isolated_database,
    )

    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0109_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(postgres_url)
    clickhouse_user = os.environ.get("AURA_AURA0109_CLICKHOUSE_USER", "default")
    clickhouse_password = os.environ.get("AURA_AURA0109_CLICKHOUSE_PASSWORD", "")
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(postgres_url)
            try:
                await connection.execute(f"SET search_path TO {quoted_schema}")
                yield connection
            finally:
                await connection.close()

        with _isolated_database(clickhouse_url, clickhouse_user, clickhouse_password) as settings:
            _apply_all_migrations(settings)
            catalog = PostgresCatalog(connection_factory)
            registry = PostgresObservationIdRegistry(connection_factory, lease_seconds=60)
            store = ClickHouseObservationStore(
                settings.clickhouse_url,
                database=settings.clickhouse_database,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password,
                id_registry=registry,
                max_concurrency=8,
            )
            service = TelemetryIngestService(
                store,
                catalog,
                credential="telemetry-secret",
                max_batch_size=5000,
            )
            records = []
            for number in range(1, 129):
                record = _payload(f"00000000-0000-4000-8000-{number:012d}")
                role = "api" if number % 3 == 0 else "worker"
                record["serviceRole"] = role
                record["deployment"] = {
                    "environment": "test",
                    "deploymentId": f"service-deployment-{number % 3}",
                    "gitCommit": "0123456789abcdef01234567",
                    "hostId": f"service-host-{number % 3}",
                }
                records.append(record)
            result = await asyncio.wait_for(
                service.ingest(records, credential="telemetry-secret"), timeout=90
            )
            assert result["accepted"] == 128
            assert result["storage_failure"] == 0

            response = httpx.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                content=(
                    "SELECT count(), uniqExact(observation_id) FROM observations "
                    "FORMAT JSONEachRow"
                ),
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                timeout=20,
            )
            response.raise_for_status()
            row = json.loads(response.text.splitlines()[0])
            assert int(row["count()"] if "count()" in row else row["count()\n"]) == 128
            assert int(row["uniqExact(observation_id)"]) == 128

            async with connection_factory() as connection:
                watermark_result = await connection.execute(
                    "SELECT committed_sequence FROM observatory_ingest_watermark "
                    "WHERE watermark_key = TRUE"
                )
                watermark_row = await watermark_result.fetchone()
                receipt_result = await connection.execute(
                    "SELECT count(*) FROM observatory_observation_receipts "
                    "WHERE status = 'completed'"
                )
                receipt_row = await receipt_result.fetchone()
            assert watermark_row is not None and int(watermark_row[0]) == 128
            assert receipt_row is not None and int(receipt_row[0]) == 128
    finally:
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


@pytest.mark.asyncio
async def test_live_clickhouse_response_loss_redelivery_keeps_one_row_and_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_url = os.environ.get("AURA_AURA0109_POSTGRES_URL", "")
    clickhouse_url = os.environ.get("AURA_AURA0109_CLICKHOUSE_URL", "")
    if not postgres_url or not clickhouse_url:
        pytest.skip(
            "set AURA_AURA0109_POSTGRES_URL and AURA_AURA0109_CLICKHOUSE_URL "
            "for live AURA-0109 coverage"
        )
    psycopg = pytest.importorskip("psycopg")
    from test_aura0094_clickhouse_integration import (
        _apply_all_migrations,
        _isolated_database,
    )

    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0109_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection = psycopg.connect(postgres_url)
    clickhouse_user = os.environ.get("AURA_AURA0109_CLICKHOUSE_USER", "default")
    clickhouse_password = os.environ.get("AURA_AURA0109_CLICKHOUSE_PASSWORD", "")
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            connection = await psycopg.AsyncConnection.connect(postgres_url)
            try:
                await connection.execute(f"SET search_path TO {quoted_schema}")
                yield connection
            finally:
                await connection.close()

        with _isolated_database(clickhouse_url, clickhouse_user, clickhouse_password) as settings:
            _apply_all_migrations(settings)
            registry = PostgresObservationIdRegistry(connection_factory, lease_seconds=1)
            store = ClickHouseObservationStore(
                settings.clickhouse_url,
                database=settings.clickhouse_database,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password,
                id_registry=registry,
                max_concurrency=1,
            )
            real_client = httpx.AsyncClient
            first_response_loss = True
            sent: list[tuple[str, int]] = []

            class ResponseLossClient:
                def __init__(self) -> None:
                    self.client = real_client()

                async def __aenter__(self) -> ResponseLossClient:
                    await self.client.__aenter__()
                    return self

                async def __aexit__(self, *_args: object) -> None:
                    await self.client.__aexit__(*_args)

                async def post(self, url: str, **kwargs: Any) -> httpx.Response:
                    nonlocal first_response_loss
                    response = await self.client.post(url, **kwargs)
                    body = json.loads(str(kwargs["content"]).split("\n", 1)[1])
                    statement = str(kwargs["content"])
                    token = statement.split("insert_deduplication_token=", 1)[1].split(
                        " ", 1
                    )[0].strip("'")
                    sent.append(
                        (
                            token,
                            int(body["ingest_sequence"]),
                        )
                    )
                    if first_response_loss:
                        first_response_loss = False
                        raise RuntimeError("response lost after provider accepted insert")
                    return response

            monkeypatch.setattr(
                "aura_observatory.adapters.clickhouse.httpx.AsyncClient", ResponseLossClient
            )
            observation = _observation(1)
            first = await store.insert_observations([observation])
            assert first[observation.observation_id] is InsertOutcome.RETRYABLE

            async with connection_factory() as connection:
                await connection.execute(
                    "UPDATE observatory_observation_receipts SET updated_at = CURRENT_TIMESTAMP "
                    "- INTERVAL '2 seconds' WHERE observation_id = %s",
                    (observation.observation_id,),
                )
                await connection.commit()
            second = await store.insert_observations([observation])
            assert second[observation.observation_id] is InsertOutcome.ACCEPTED, (
                f"redelivery diagnostics sent={sent!r} first_response_loss={first_response_loss!r}"
            )
            assert sent == [(observation.observation_id, 1)] * 2

            response = httpx.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                content=(
                    "SELECT count(), uniqExact(ingest_sequence), min(ingest_sequence), "
                    "max(ingest_sequence) FROM observations WHERE observation_id = '"
                    + observation.observation_id
                    + "' FORMAT JSONEachRow"
                ),
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                timeout=20,
            )
            response.raise_for_status()
            row = json.loads(response.text.splitlines()[0])
            assert int(row["count()"] if "count()" in row else row["count()\n"]) == 1
            assert int(row["uniqExact(ingest_sequence)"]) == 1
            assert int(row["min(ingest_sequence)"]) == 1
            assert int(row["max(ingest_sequence)"]) == 1
    finally:
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()
