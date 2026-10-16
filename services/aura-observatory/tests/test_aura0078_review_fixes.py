"""Focused regressions for the AURA-0078 observability review findings."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore, clickhouse_row
from aura_observatory.adapters.memory import (
    MemoryCatalog,
    MemoryObservationIdRegistry,
    MemoryObservationStore,
)
from aura_observatory.adapters.postgres import PostgresObservationIdRegistry
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.composition import build_ingest_service
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints import cli
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.ports import ClaimOutcome, InsertOutcome
from aura_observatory.settings import Settings


def payload(observation_id: str = "01234567-89ab-4cde-8123-0123456789ab") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "observationId": observation_id,
        "observedAt": datetime.now(UTC).isoformat(),
        "kind": "metric",
        "component": {"id": "aura.runtime.model_inference", "version": "1.0.0"},
        "operation": "inference",
        "measurement": {"name": "duration_ms", "value": 12.5, "unit": "milliseconds"},
        "dimensions": {"outcome": "success"},
        "trace": {
            "traceId": "0123456789abcdef0123456789abcdef",
            "spanId": "0123456789abcdef",
        },
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


def test_production_composition_uses_real_adapters(tmp_path: Path) -> None:
    credential_file = tmp_path / "collector-secret"
    credential_file.write_text("secret", encoding="utf-8")
    service = build_ingest_service(
        Settings(
            ingest_credential_file=credential_file,
            database_url="postgresql://observatory",
            clickhouse_url="http://clickhouse:8123",
        )
    )

    assert service.observation_store.__class__.__name__ == "ClickHouseObservationStore"
    assert service.catalog.__class__.__name__ == "PostgresCatalog"


def test_wire_payload_keys_match_the_authoritative_schema() -> None:
    root = Path(__file__).parents[3]
    schema = json.loads(
        (root / "contracts/telemetry/v1/metadata-observation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    wire = MetadataObservation.from_payload(payload()).to_payload()

    assert set(wire) <= set(schema["properties"])
    assert set(schema["required"]) <= set(wire)
    assert set(wire["trace"]) == {"traceId", "spanId"}
    assert set(wire["correlation"]) == {"runId"}
    assert set(wire["deployment"]) == {
        "deploymentId",
        "gitCommit",
        "environment",
        "hostId",
    }


def test_clickhouse_row_contains_contract_kind_and_capture_policy() -> None:
    observation = MetadataObservation.from_payload(payload())
    row = clickhouse_row(observation.to_payload())

    assert row["kind"] == "metric"
    assert row["capture_policy"] == "metadata_only"


def test_clickhouse_row_normalizes_rfc3339_utc_to_datetime64_milliseconds() -> None:
    wire = payload()
    wire["observedAt"] = "2026-10-16T12:34:56.123987Z"

    row = clickhouse_row(wire)

    assert row["observed_at"] == "2026-10-16 12:34:56.123"


def test_clickhouse_row_normalizes_explicit_utc_offset() -> None:
    wire = payload()
    wire["observedAt"] = "2026-10-16T12:34:56.123456+00:00"

    row = clickhouse_row(wire)

    assert row["observed_at"] == "2026-10-16 12:34:56.123"


@pytest.mark.parametrize("observed_at", ["2026-10-16T12:34:56.123456", "not-a-timestamp"])
def test_clickhouse_row_rejects_invalid_timestamp(observed_at: str) -> None:
    wire = payload()
    wire["observedAt"] = observed_at

    with pytest.raises(ValueError, match="observedAt"):
        clickhouse_row(wire)


def test_clickhouse_row_maps_every_contract_field_without_private_payloads() -> None:
    observation = MetadataObservation.from_payload(payload())
    row = clickhouse_row(observation.to_payload())

    assert set(row) == {
        "observation_id",
        "observed_at",
        "kind",
        "component_id",
        "component_version",
        "operation",
        "measurement_name",
        "measurement_value",
        "measurement_unit",
        "dimensions",
        "trace_id",
        "span_id",
        "parent_span_id",
        "linked_span_ids",
        "run_id",
        "conversation_id",
        "deployment_id",
        "git_commit",
        "environment",
        "host_id",
        "service_role",
        "schema_version",
        "capture_policy",
    }
    assert row["observation_id"] == payload()["observationId"]
    source = payload()
    source_trace = cast(dict[str, Any], source["trace"])
    source_correlation = cast(dict[str, Any], source["correlation"])
    assert row["trace_id"] == source_trace["traceId"]
    assert row["span_id"] == source_trace["spanId"]
    assert row["run_id"] == source_correlation["runId"]
    assert row["dimensions"] == {"outcome": "success"}
    assert not {"prompt", "response", "content", "filename", "credential"} & set(row)


def test_wire_contract_rejects_snake_case_aliases_and_hyphenated_trace_ids() -> None:
    snake_payload = payload()
    snake_payload["trace"] = {"trace_id": "0123456789abcdef0123456789abcdef"}
    with pytest.raises(ValueError):
        MetadataObservation.from_payload(snake_payload)

    hyphenated = payload()
    hyphenated["trace"] = {"traceId": "abcdef01-2345-4abc-8123-abcdef012345"}
    with pytest.raises(ValueError):
        MetadataObservation.from_payload(hyphenated)


def test_span_requires_trace_and_span_id() -> None:
    metric = payload()
    metric["kind"] = "span"
    metric.pop("trace")
    with pytest.raises(ValueError):
        MetadataObservation.from_payload(metric)

    missing_span = payload()
    missing_span["kind"] = "span"
    missing_span["trace"] = {"traceId": "0123456789abcdef0123456789abcdef"}
    with pytest.raises(ValueError):
        MetadataObservation.from_payload(missing_span)


@pytest.mark.asyncio
async def test_duration_response_is_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    service = TelemetryIngestService(MemoryObservationStore(), MemoryCatalog(), credential="secret")
    ticks = iter((0.0, 0.1, 1.0, 1.1))

    def clock() -> float:
        return next(ticks)

    monkeypatch.setattr("aura_observatory.ingest.monotonic", clock)
    monkeypatch.setattr("aura_observatory.metrics.monotonic", clock)

    first = await service.ingest([payload()], credential="secret")
    second = await service.ingest(
        [payload("abcdef98-7654-4fed-8123-fedcba987654")], credential="secret"
    )

    assert abs(float(first["duration_ms"]) - 100.0) < 1e-6
    assert abs(float(second["duration_ms"]) - 100.0) < 1e-6
    assert abs(float(service.counters.duration_ms_total) - 200.0) < 1e-6


@pytest.mark.asyncio
async def test_concurrent_duplicate_observations_have_one_winner() -> None:
    store = MemoryObservationStore()
    observation = MetadataObservation.from_payload(payload())

    outcomes = await asyncio.gather(*(store.insert_observation(observation) for _ in range(8)))

    assert outcomes.count(InsertOutcome.ACCEPTED) == 1
    assert outcomes.count(InsertOutcome.DUPLICATE) == 7
    assert list(store.observations) == [observation.observation_id]


@pytest.mark.asyncio
async def test_clickhouse_deduplication_token_is_stable_across_concurrent_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> FakeResponse:
            requests.append(kwargs)
            return FakeResponse()

    def client_factory() -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient",
        client_factory,
    )
    observation = MetadataObservation.from_payload(payload())
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=MemoryObservationIdRegistry()
    )

    outcomes = await asyncio.gather(*(store.insert_observation(observation) for _ in range(2)))

    assert outcomes.count(InsertOutcome.ACCEPTED) == 1
    assert outcomes.count(InsertOutcome.DUPLICATE) == 1
    assert len(requests) == 1
    assert requests[0]["params"]["insert_deduplicate"] == "1"
    assert requests[0]["params"]["insert_deduplication_token"] == observation.observation_id


@pytest.mark.asyncio
async def test_clickhouse_http_failure_logs_only_bounded_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> httpx.Response:
            request = httpx.Request("POST", "http://clickhouse.test")
            return httpx.Response(
                500,
                request=request,
                text=(
                    "Code: 27, e.displayText() = DB::Exception: Cannot parse input "
                    "from 'private-value' in query observation_id=0123456789abcdef"
                ),
            )

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", FakeClient)
    observation = MetadataObservation.from_payload(payload())
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=MemoryObservationIdRegistry()
    )

    with caplog.at_level(logging.ERROR, logger="aura_observatory.adapters.clickhouse"):
        with pytest.raises(httpx.HTTPStatusError):
            await store.insert_observation(observation)

    assert "status=500" in caplog.text
    assert "code=27" in caplog.text
    assert "message=parse_error" in caplog.text
    assert "private-value" not in caplog.text
    assert "0123456789abcdef" not in caplog.text


@pytest.mark.asyncio
async def test_clickhouse_query_failure_logs_only_bounded_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> httpx.Response:
            request = httpx.Request("POST", "http://clickhouse.test")
            return httpx.Response(
                500,
                request=request,
                text=(
                    "Code: 47, e.displayText() = DB::Exception: Unknown column "
                    "'private-query-value' in SQL SELECT private-query-value"
                ),
            )

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", FakeClient)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.test",
        database="aura_observatory",
        user="default",
        password="secret",
    )

    with caplog.at_level(logging.ERROR, logger="aura_observatory.adapters.query"):
        with pytest.raises(httpx.HTTPStatusError):
            await store.list_observations()

    assert "status=500" in caplog.text
    assert "code=47" in caplog.text
    assert "reason=schema_error" in caplog.text
    assert "private-query-value" not in caplog.text
    assert "SELECT" not in caplog.text


@pytest.mark.asyncio
async def test_component_health_query_uses_clickhouse_conditional_quantiles() -> None:
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _query_rows(self, statement: str) -> list[dict[str, Any]]:
            queries.append(statement)
            return [
                {
                    "sample_count": 1,
                    "p50": 12.5,
                    "p95": 12.5,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="aura_observatory",
        user="default",
        password="secret",
    )

    result = await store.component_health_query("aura.runtime.model_routing", None, None)

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 12.5
    assert len(queries) == 1
    assert "quantileIf(0.50)" in queries[0]
    assert "quantileIf(0.95)" in queries[0]
    assert "AS selected_measurement_value" in queries[0]
    assert "AS selected_measurement_unit" in queries[0]
    assert "selected_measurement_value, selected_measurement_unit" in queries[0]
    assert "AS measurement_value" not in queries[0]
    assert "AS measurement_unit" not in queries[0]
    assert " FILTER " not in queries[0]


@pytest.mark.asyncio
async def test_receipt_claim_recovers_after_crash_and_stale_lease() -> None:
    now = [0.0]
    registry = MemoryObservationIdRegistry(lease_seconds=10, clock=lambda: now[0])

    assert await registry.claim("observation-1") == ClaimOutcome.ACQUIRED
    assert await registry.claim("observation-1") == ClaimOutcome.PENDING
    now[0] = 11.0
    assert await registry.claim("observation-1") == ClaimOutcome.ACQUIRED
    await registry.complete("observation-1")
    assert await registry.claim("observation-1") == ClaimOutcome.DUPLICATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_status", "stale", "expected"),
    [
        ("completed", False, ClaimOutcome.DUPLICATE),
        ("pending", False, ClaimOutcome.PENDING),
        ("pending", True, ClaimOutcome.ACQUIRED),
    ],
)
async def test_postgres_receipt_states_are_distinguished(
    existing_status: str,
    stale: bool,
    expected: ClaimOutcome,
) -> None:
    class FakeResult:
        def __init__(self, row: tuple[str, ...] | None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[str, ...] | None:
            return self.row

    class FakeConnection:
        async def execute(self, query: str, _params: tuple[Any, ...] = ()) -> FakeResult:
            if query.lstrip().startswith("INSERT"):
                return FakeResult(None)
            if "SELECT status" in query:
                return FakeResult((existing_status,))
            if query.lstrip().startswith("UPDATE"):
                return FakeResult(("observation-1",) if stale else None)
            raise AssertionError(f"unexpected SQL: {query}")

    @asynccontextmanager
    async def connection() -> AsyncGenerator[FakeConnection]:
        yield FakeConnection()

    registry = PostgresObservationIdRegistry(connection, lease_seconds=10)

    assert await registry.claim("observation-1") == expected


@pytest.mark.asyncio
async def test_clickhouse_response_loss_recovers_without_second_visible_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    registry = MemoryObservationIdRegistry(lease_seconds=10, clock=lambda: now[0])
    requests: list[dict[str, Any]] = []
    visible_tokens: set[str] = set()
    fail_first = True

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> FakeResponse:
            nonlocal fail_first
            requests.append(kwargs)
            token = kwargs["params"]["insert_deduplication_token"]
            visible_tokens.add(token)
            if fail_first:
                fail_first = False
                raise RuntimeError("response lost after ClickHouse accepted the insert")
            return FakeResponse()

    def client_factory() -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient",
        client_factory,
    )
    observation = MetadataObservation.from_payload(payload())
    store = ClickHouseObservationStore("http://clickhouse.test", id_registry=registry)

    with pytest.raises(RuntimeError, match="response lost"):
        await store.insert_observation(observation)
    assert await store.insert_observation(observation) == InsertOutcome.RETRYABLE

    now[0] = 11.0
    assert await store.insert_observation(observation) == InsertOutcome.ACCEPTED
    assert len(requests) == 2
    assert len(visible_tokens) == 1


@pytest.mark.asyncio
async def test_clickhouse_preinsert_failure_is_retryable_before_stale_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    registry = MemoryObservationIdRegistry(lease_seconds=10, clock=lambda: now[0])
    calls = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **_kwargs: Any) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("connection failed before insert")
            return FakeResponse()

    def client_factory() -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient",
        client_factory,
    )
    observation = MetadataObservation.from_payload(payload())
    store = ClickHouseObservationStore("http://clickhouse.test", id_registry=registry)

    with pytest.raises(RuntimeError, match="before insert"):
        await store.insert_observation(observation)
    assert await store.insert_observation(observation) == InsertOutcome.RETRYABLE
    assert calls == 1

    now[0] = 11.0
    assert await store.insert_observation(observation) == InsertOutcome.ACCEPTED
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_service_batches_account_for_duplicate_once() -> None:
    store = MemoryObservationStore()
    catalog = MemoryCatalog()
    service = TelemetryIngestService(store, catalog, credential="secret")

    results = await asyncio.gather(
        *(service.ingest([payload()], credential="secret") for _ in range(2))
    )

    assert sorted(result["accepted"] for result in results) == [0, 1]
    assert sorted(result["duplicate"] for result in results) == [0, 1]
    assert service.counters.accepted == 1
    assert service.counters.duplicate == 1


@pytest.mark.asyncio
async def test_pending_claim_is_storage_failure_and_not_duplicate() -> None:
    class PendingStore:
        async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
            del observation
            return InsertOutcome.RETRYABLE

    catalog = MemoryCatalog()
    service = TelemetryIngestService(PendingStore(), catalog, credential="secret")

    result = await service.ingest([payload()], credential="secret")

    assert result["accepted"] == 0
    assert result["duplicate"] == 0
    assert result["storage_failure"] == 1
    assert service.counters.duplicate == 0
    assert service.counters.storage_failure == 1
    assert (await catalog.counters())["storage_failure"] == 1


@pytest.mark.asyncio
async def test_pending_claim_returns_retryable_http_status() -> None:
    class PendingStore:
        async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
            del observation
            return InsertOutcome.RETRYABLE

    service = TelemetryIngestService(PendingStore(), MemoryCatalog(), credential="secret")
    app = create_app(Settings(), ingest_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/telemetry/observations",
            json={"observations": [payload()]},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["storage_failure"] == 1


@pytest.mark.asyncio
async def test_catalogue_failure_prevents_observation_write() -> None:
    class FailingCatalog(MemoryCatalog):
        async def record_component(self, component_id: str, version: str) -> None:
            raise RuntimeError("catalogue unavailable")

    store = MemoryObservationStore()
    service = TelemetryIngestService(store, FailingCatalog(), credential="secret")

    result = await service.ingest([payload()], credential="secret")

    assert result["accepted"] == 0
    assert result["storage_failure"] == 1
    assert service.counters.storage_failure == 1
    assert store.observations == {}


@pytest.mark.asyncio
async def test_counter_storage_failure_is_accounted_without_losing_observation() -> None:
    class CounterFailingCatalog(MemoryCatalog):
        async def increment_counter(self, name: str, amount: int = 1) -> None:
            raise RuntimeError(f"counter unavailable: {name}")

    store = MemoryObservationStore()
    service = TelemetryIngestService(store, CounterFailingCatalog(), credential="secret")

    result = await service.ingest([payload()], credential="secret")

    assert result["accepted"] == 1
    assert result["storage_failure"] >= 1
    assert service.counters.accepted == 1
    assert service.counters.storage_failure >= 1
    assert len(store.observations) == 1


@pytest.mark.asyncio
async def test_api_counts_raw_json_failure_without_reading_untrusted_payload() -> None:
    settings = Settings(ingest_max_payload_bytes=1024)
    service = TelemetryIngestService(MemoryObservationStore(), MemoryCatalog(), credential="secret")
    app = create_app(settings, ingest_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/telemetry/observations",
            content=b"{not-json",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert service.counters.malformed == 1


@pytest.mark.asyncio
async def test_api_authenticates_before_reading_request_body() -> None:
    service = TelemetryIngestService(MemoryObservationStore(), MemoryCatalog(), credential="secret")
    app = create_app(Settings(), ingest_service=service)
    messages: list[MutableMapping[str, Any]] = []
    body_read = False

    async def receive() -> dict[str, Any]:
        nonlocal body_read
        body_read = True
        raise AssertionError("request body was read before authentication")

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/internal/v1/telemetry/observations",
        "raw_path": b"/internal/v1/telemetry/observations",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer wrong")],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }

    await app(scope, receive, send)

    assert not body_read
    assert (
        next(message for message in messages if message["type"] == "http.response.start")["status"]
        == 401
    )


@pytest.mark.asyncio
async def test_api_enforces_configured_batch_size_before_storage() -> None:
    settings = Settings(ingest_max_batch_size=1)
    service = TelemetryIngestService(
        MemoryObservationStore(), MemoryCatalog(), credential="secret", max_batch_size=1
    )
    app = create_app(settings, ingest_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/telemetry/observations",
            json={"observations": [payload(), payload("abcdef98-7654-4fed-8123-fedcba987654")]},
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 400
    assert service.counters.malformed == 1
    assert service.counters.accepted == 0


@pytest.mark.asyncio
async def test_api_rejects_single_oversized_request_chunk_before_extending_body() -> None:
    settings = Settings(ingest_max_payload_bytes=1024)
    service = TelemetryIngestService(MemoryObservationStore(), MemoryCatalog(), credential="secret")
    app = create_app(settings, ingest_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/telemetry/observations",
            content=b"x" * 1025,
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 413
    assert service.counters.malformed == 1


def test_manifest_does_not_claim_unimplemented_buffering_or_retries() -> None:
    root = Path(__file__).parents[1]
    for path in (root / "resources" / "component-manifests").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "process_buffer" not in text
        assert "retry_then_count" not in text
        assert "overflow:" not in text


def test_cli_migrate_executes_both_service_owned_migration_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_statements: list[str] = []
    clickhouse_statements: list[str] = []

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> None:
            postgres_statements.append(statement)

        def commit(self) -> None:
            return None

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    def connect(_database_url: str) -> FakeConnection:
        return FakeConnection()

    def post(
        _url: str,
        *,
        params: dict[str, str],
        content: str,
        timeout: float,
        auth: tuple[str, str],
    ) -> FakeResponse:
        assert params["database"] == "aura_observatory"
        assert timeout == 10.0
        expected_settings = Settings()
        assert auth == (expected_settings.clickhouse_user, expected_settings.clickhouse_password)
        clickhouse_statements.append(content)
        return FakeResponse()

    monkeypatch.setattr(cli.psycopg, "connect", connect)
    monkeypatch.setattr(cli.httpx, "post", post)

    cli.migrate()

    expected_postgres_migrations = sorted(
        (Path(__file__).parents[1] / "migrations/postgresql/versions").glob("*.sql")
    )
    assert len(postgres_statements) == len(expected_postgres_migrations)
    assert any("observatory_components" in statement for statement in postgres_statements)
    assert any("observatory_retention_actions" in statement for statement in postgres_statements)
    expected_clickhouse_migrations = sorted(
        (Path(__file__).parents[1] / "migrations/clickhouse/versions").glob("*.sql")
    )
    assert len(clickhouse_statements) > len(expected_clickhouse_migrations)
    assert any(
        "CREATE TABLE IF NOT EXISTS observations" in statement
        for statement in clickhouse_statements
    )


def test_module_registry_and_component_manifests_describe_the_same_service_surface() -> None:
    root = Path(__file__).parents[3]
    registry = yaml.safe_load(
        (root / "tooling/architecture/modules.yaml").read_text(encoding="utf-8")
    )
    modules = {
        entry["id"]: entry
        for entry in registry["modules"]
        if entry["id"].startswith("aura-observatory.")
    }

    expected_modules = {
        "aura-observatory.service",
        "aura-observatory.package",
        "aura-observatory.contract",
        "aura-observatory.ports",
        "aura-observatory.metrics",
        "aura-observatory.settings",
        "aura-observatory.ingest",
        "aura-observatory.adapters",
        "aura-observatory.adapters.memory",
        "aura-observatory.adapters.postgres",
        "aura-observatory.adapters.clickhouse",
        "aura-observatory.composition",
        "aura-observatory.entrypoints",
        "aura-observatory.entrypoints.api",
        "aura-observatory.entrypoints.api.app",
        "aura-observatory.entrypoints.worker",
        "aura-observatory.entrypoints.cli",
    }
    assert expected_modules <= set(modules)
    for entry in modules.values():
        assert (root / entry["root"]).exists(), entry["id"]
        assert (root / entry["public_api"]).exists(), entry["id"]

    manifests = sorted(
        (root / "services/aura-observatory/resources/component-manifests").glob("*.yaml")
    )
    manifest_ids = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["component"]["id"] for path in manifests
    }
    assert manifest_ids == {
        "aura.observatory.telemetry_ingestion",
        "aura.observatory.telemetry_storage",
        "aura.observatory.query_api",
        "aura.observatory.live_updates",
        "aura.observatory.retention",
    }
    ingestion_metrics = {
        "telemetry_ingest_accepted_count",
        "telemetry_ingest_rejected_count",
        "telemetry_ingest_duplicate_count",
        "telemetry_ingest_storage_failure_count",
        "telemetry_ingest_duration_ms",
    }
    for path in manifests:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest_id = manifest["component"]["id"]
        expected_metrics = ingestion_metrics
        if manifest_id == "aura.observatory.telemetry_ingestion":
            expected_metrics = ingestion_metrics | {
                "observatory_ingest_intake_lag_ms",
                "observatory_ingest_visibility_lag_count",
                "observatory_ingest_rejection_count",
                "observatory_ingest_duplicate_count",
                "observatory_ingest_storage_duration_ms",
                "observatory_ingest_worker_heartbeat_age_ms",
                "observatory_ingest_backlog_count",
            }
        elif manifest_id == "aura.observatory.telemetry_storage":
            expected_metrics = ingestion_metrics - {"telemetry_ingest_duration_ms"}
            expected_metrics |= {
                "observatory_ingest_visibility_lag_count",
                "observatory_ingest_storage_duration_ms",
                "observatory_ingest_backlog_count",
            }
            assert "telemetry_ingest_duration_ms" not in manifest["metric_semantics"]
        elif manifest_id == "aura.observatory.query_api":
            expected_metrics = {
                "observatory_query_duration_ms",
                "observatory_query_outcome",
                "observatory_auth_callback_duration_ms",
                "observatory_auth_callback_outcome",
                "observatory_query_freshness_lag_ms",
            }
        elif manifest_id == "aura.observatory.live_updates":
            expected_metrics = {
                "observatory_sse_connection_count",
                "observatory_sse_active",
                "observatory_sse_replay_count",
            }
        elif manifest_id == "aura.observatory.retention":
            expected_metrics = {
                "observatory_retention_action_count",
                "observatory_retention_deleted_count",
                "observatory_retention_failure_count",
            }
        assert set(manifest["metrics"]) == expected_metrics
        assert manifest["capture_policy"] == "metadata_only"
