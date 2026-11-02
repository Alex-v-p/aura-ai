"""Adversarial AURA-0085 ingest-health and replay verification.

These checks exercise public Observatory seams with content-free metadata.  The
tests intentionally avoid asserting adapter internals so they can be reused by
the durable PostgreSQL/ClickHouse composition later.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.adapters.memory import (
    MemoryCatalog,
    MemoryObservationStore,
    MemoryObservatoryStore,
)
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.entrypoints.worker import IngestWorker
from aura_observatory.ingest import IngestPayloadError, TelemetryIngestService
from aura_observatory.metrics import (
    HealthState,
    IngestHealth,
    ObservatoryMetrics,
    bounded_lag_ms,
    bounded_latency_ms,
)
from aura_observatory.query import (
    ObservatoryHealthProjector,
    ObservatoryQueryService,
    decode_health_cursor,
    encode_cursor,
)
from aura_observatory.settings import Settings
from test_aura0080_query_retention import BASE_TIME, make_payload

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"
SERVICE_ROOT = Path(__file__).parents[1]


def test_lag_and_latency_reject_non_finite_or_non_numeric_values() -> None:
    for value in ("not-a-number", float("nan"), float("inf"), -float("inf")):
        assert bounded_lag_ms(value) is None  # type: ignore[arg-type]
        assert bounded_latency_ms(value) is None  # type: ignore[arg-type]
    assert bounded_lag_ms(-1) == 0
    assert bounded_latency_ms(-1) == 0
    assert bounded_lag_ms(604_800_001) == 604_800_000
    assert bounded_latency_ms(600_001) == 600_000


def test_observatory_health_manifests_have_closed_metric_definitions() -> None:
    for name in (
        "telemetry-ingestion.yaml",
        "telemetry-storage.yaml",
        "query-api.yaml",
        "live-updates.yaml",
    ):
        manifest = yaml.safe_load(
            (SERVICE_ROOT / "resources/component-manifests" / name).read_text(encoding="utf-8")
        )
        definitions = {
            str(item["name"]): item for item in manifest["metric_definitions"]
        }
        assert set(manifest["metrics"]) == set(definitions)
        assert manifest["retention"] == "manual_owner_delete_only"
        cardinality = set(cast(list[str], manifest["cardinality"]["metric_attributes"]))
        for metric in manifest["metrics"]:
            definition = definitions[metric]
            assert definition["type"] in {"counter", "gauge", "histogram"}
            assert isinstance(definition["unit"], str) and definition["unit"]
            labels = cast(list[str], definition["labels"])
            assert isinstance(labels, list)
            assert set(labels) <= cardinality
            assert definition["dependencies"]
            assert isinstance(definition["trace"], str) and definition["trace"]
            assert isinstance(definition["correlation"], str) and definition["correlation"]
            assert definition["export"] == "prometheus"


def test_health_cursor_requires_timezone_aware_utc_timestamp_and_uuid() -> None:
    valid = encode_cursor(BASE_TIME, "00000000-0000-4000-8000-000000008501")
    # Observation cursors carry an additional snapshot field and are not
    # accepted by the closed health-history cursor decoder.
    with pytest.raises(ValueError):
        decode_health_cursor(valid)
    def cursor(payload: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    health_cursor = cursor(
        {"at": BASE_TIME.isoformat(), "id": "00000000-0000-4000-8000-000000008501"}
    )
    decoded_at, decoded_id = decode_health_cursor(health_cursor)
    assert decoded_at == BASE_TIME
    assert decoded_id.endswith("8501")
    with pytest.raises(ValueError):
        decode_health_cursor(cursor({"at": "2026-10-16T12:00:00", "id": decoded_id}))
    with pytest.raises(ValueError):
        decode_health_cursor(cursor({"at": BASE_TIME.isoformat(), "id": "not-a-uuid"}))


def test_repeated_health_snapshot_metric_export_sets_current_values() -> None:
    metrics = ObservatoryMetrics()
    metrics.set("observatory_ingest_intake_lag_ms", 42)
    metrics.set("observatory_ingest_intake_lag_ms", 42)
    metrics.set("observatory_ingest_rejection_count", 3, {"reason": "malformed"})
    metrics.set("observatory_ingest_rejection_count", 3, {"reason": "malformed"})
    snapshot = metrics.snapshot()
    assert snapshot["observatory_ingest_intake_lag_ms|"]["value"] == 42
    assert snapshot["observatory_ingest_rejection_count|reason=malformed"]["value"] == 3


@pytest.mark.asyncio
async def test_ingest_histogram_is_visible_from_shared_metrics_scrape(tmp_path: Path) -> None:
    metrics_path = tmp_path / "series.json"
    ingest_metrics = ObservatoryMetrics(export_path=metrics_path)
    query_metrics = ObservatoryMetrics(export_path=metrics_path)
    ingest_metrics.record("observatory_ingest_storage_duration_ms", 7)
    ingest_metrics.record("observatory_ingest_storage_duration_ms", 11)

    settings = Settings(metrics_file=metrics_path)
    app = create_app(settings, testing=True)
    app.state.query.metrics = query_metrics
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "# TYPE observatory_ingest_storage_duration_ms histogram" in response.text
    assert "observatory_ingest_storage_duration_ms_count 2" in response.text
    assert "observatory_ingest_storage_duration_ms_sum 18" in response.text


@pytest.mark.asyncio
async def test_postgres_storage_latency_series_drives_api_histogram_scrape() -> None:
    class Result:
        def __init__(self, row: tuple[object, ...] | None = None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[object, ...] | None:
            return self.row

        async def fetchall(self) -> list[tuple[object, ...]]:
            return []

    class Connection:
        def __init__(self) -> None:
            self.storage_latency: dict[str, object] = {}
            self.storage_count = 0
            self.storage_sum = 0.0
            self.storage_buckets = {
                str(bucket): 0 for bucket in ObservatoryMetrics.DURATION_BUCKETS
            }

        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            if "INSERT INTO observatory_ingest_counters" in statement:
                return Result()
            if "SELECT storage_latency" in statement:
                return Result(
                    (
                        json.dumps(self.storage_latency),
                        self.storage_count,
                        self.storage_sum,
                        self.storage_buckets,
                    )
                )
            if "UPDATE observatory_ingest_counters SET storage_latency" in statement:
                self.storage_latency = cast(dict[str, object], json.loads(str(params[0])))
                self.storage_count += 1
                self.storage_sum += float(params[1])
                for bucket, value in zip(
                    ObservatoryMetrics.DURATION_BUCKETS, params[2:], strict=True
                ):
                    self.storage_buckets[str(bucket)] += int(value <= bucket)
                return Result()
            return Result()

    class Context:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context(connection)

    catalog = PostgresCatalog(factory)
    await catalog.record_latency("storage", 7)
    await catalog.record_latency("storage", 11)
    series = await catalog.metric_series()
    assert series == [
        {
            "name": "observatory_ingest_storage_duration_ms",
            "value": 18,
            "sum": 18,
            "count": 2,
            "buckets": {
                "5": 0,
                "10": 1,
                "25": 2,
                "50": 2,
                "100": 2,
                "250": 2,
                "500": 2,
                "1000": 2,
                "2500": 2,
                "5000": 2,
                "10000": 2,
            },
            "labels": {},
        }
    ]

    production_store = object.__new__(ProductionObservatoryStore)
    production_store._connection_factory = factory  # type: ignore[attr-defined]
    production_store._health_port = catalog  # type: ignore[attr-defined]
    settings = Settings(metrics_file=None)
    app = create_app(settings, query_store=production_store, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "# TYPE observatory_ingest_storage_duration_ms histogram" in response.text
    assert "observatory_ingest_storage_duration_ms_sum 18" in response.text
    assert "observatory_ingest_storage_duration_ms_count 2" in response.text
    assert 'observatory_ingest_storage_duration_ms_bucket{le="10"} 1' in response.text
    assert 'observatory_ingest_storage_duration_ms_bucket{le="25"} 2' in response.text

    initial_buckets = cast(dict[str, int], series[0]["buckets"])
    for _ in range(127):
        await catalog.record_latency("storage", 25)
    cumulative = await catalog.metric_series()
    assert cumulative[0]["count"] == 129
    assert cumulative[0]["sum"] == 3193
    cumulative_buckets = cast(dict[str, int], cumulative[0]["buckets"])
    assert all(
        cumulative_buckets[key] >= initial_buckets[key] for key in initial_buckets
    )
    samples_value = connection.storage_latency["samples"]
    assert isinstance(samples_value, list)
    assert len(cast(list[Any], samples_value)) == 128

    # A fresh catalog adapter sees the cumulative state, while the UI
    # reservoir remains bounded to the latest 128 samples.
    restarted = PostgresCatalog(factory)
    assert await restarted.metric_series() == cumulative
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
    ) as client:
        response = await client.get("/metrics")
    assert "observatory_ingest_storage_duration_ms_count 129" in response.text
    assert "observatory_ingest_storage_duration_ms_sum 3193" in response.text


@pytest.mark.asyncio
async def test_postgres_health_hydration_preserves_latest_latency_sample() -> None:
    class Result:
        async def fetchone(self) -> tuple[Any, ...]:
            return (
                1,
                0,
                0,
                0,
                0,
                {},
                {"samples": [90, 20, 10]},
                {},
                None,
                datetime.now(UTC),
                0,
                0,
                0,
                None,
                None,
                datetime.now(UTC),
                None,
                None,
                None,
            )

    class Connection:
        async def execute(self, statement: str) -> Result:
            assert "SELECT accepted_count" in statement
            return Result()

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    snapshot = await PostgresCatalog(lambda: Context()).health_snapshot()
    assert snapshot is not None
    storage_latency = cast(dict[str, object], snapshot["storageLatency"])
    assert storage_latency["latestMs"] == 10
    assert storage_latency["p50Ms"] == 20


@pytest.mark.asyncio
async def test_production_event_adapter_merges_health_and_observation_cursors() -> None:
    class HealthEvents:
        async def latest_health_event_sequence(self) -> int:
            return 2

        async def health_event_sequence(self, event_id: str) -> int | None:
            return 2 if event_id == "health-event" else None

        async def health_events_after(
            self, last_event_id: str | None, upper_sequence: int | None = None
        ) -> list[dict[str, object]]:
            del last_event_id, upper_sequence
            return [{"id": "health-event", "_sequence": 2, "data": {}}]

    store = object.__new__(ProductionObservatoryStore)
    store._health_port = HealthEvents()  # type: ignore[attr-defined]

    async def counts() -> dict[str, int]:
        return {"latest_sequence": 2}

    async def query(statement: str) -> list[dict[str, object]]:
        if "SELECT ingest_sequence FROM observations" in statement:
            return []
        return [
            {
                "observation_id": "00000000-0000-4000-8000-000000008502",
                "run_id": None,
                "trace_id": None,
                "ingest_sequence": 1,
            }
        ]

    store._overview_counts = counts  # type: ignore[attr-defined]
    store._query_rows = query  # type: ignore[attr-defined]
    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(
        store,
        metrics=metrics,
        query_port=store,
        health_port=store._health_port,  # type: ignore[attr-defined]
    )
    stream = service.events_after(None)
    first = await asyncio.wait_for(anext(stream), timeout=1)
    second = await asyncio.wait_for(anext(stream), timeout=1)
    await cast(Any, stream).aclose()
    assert [first["type"], second["type"]] == [
        "observation.changed",
        "health.changed",
    ]
    metric_snapshot = metrics.snapshot()
    assert metric_snapshot[
        "observatory_sse_replay_count|event_type=observation.changed"
    ]["value"] == 1
    assert metric_snapshot["observatory_sse_replay_count|event_type=health.changed"]["value"] == 1


@pytest.mark.asyncio
async def test_production_overview_propagates_and_persists_health_snapshot() -> None:
    class HealthPort:
        def __init__(self) -> None:
            self.saved: list[dict[str, object]] = []
            self.freshness: list[datetime] = []
            self.latencies: list[tuple[str, float]] = []

        async def record_query_freshness(self, observed_at: datetime | None) -> None:
            if observed_at is not None:
                self.freshness.append(observed_at)

        async def record_latency(self, kind: str, duration_ms: float) -> None:
            self.latencies.append((kind, duration_ms))

        async def save_health_snapshot(self, snapshot: dict[str, object]) -> None:
            self.saved.append(snapshot)

        async def health_snapshot(self) -> dict[str, object]:
            return snapshot

    health = HealthPort()
    store = object.__new__(ProductionObservatoryStore)
    store._health_port = health  # type: ignore[attr-defined]
    observed_at = BASE_TIME.isoformat()
    snapshot: dict[str, object] = {
        "snapshotId": "00000000-0000-4000-8000-000000008503",
        "observedAt": observed_at,
        "state": "healthy",
        "intakeLagMs": 12,
        "rejectionReasons": [],
        "duplicateCount": 0,
    }

    async def counts() -> dict[str, object]:
        await health.record_query_freshness(BASE_TIME)
        return {
            "observation_count": 1,
            "run_count": 1,
            "trace_count": 1,
            "observed_at": observed_at,
        }

    async def ingest() -> dict[str, object]:
        return {"health": snapshot}

    async def components() -> list[dict[str, object]]:
        return []

    async def deployments() -> list[dict[str, object]]:
        return []

    store._overview_counts = counts  # type: ignore[attr-defined]
    store.ingest_overview = ingest  # type: ignore[attr-defined]
    store.components = components  # type: ignore[attr-defined]
    store.deployments = deployments  # type: ignore[attr-defined]
    service = ObservatoryQueryService(store, query_port=store, health_port=cast(Any, health))

    result = await service.overview()

    assert result["health"] == snapshot
    assert health.saved == []
    assert len(health.latencies) == 1
    assert health.latencies[0][0] == "query"
    query_latency = health.latencies[0][1]
    assert query_latency >= 0
    await ObservatoryHealthProjector(cast(Any, health)).project_once()
    assert health.saved == [snapshot]
    assert health.freshness == [BASE_TIME]
    # Snapshot persistence is a projection write, not a query sample.
    assert health.latencies == [("query", query_latency)]


@pytest.mark.asyncio
async def test_health_event_marker_does_not_hole_observation_watermark() -> None:
    class Result:
        def __init__(self, row: tuple[Any, ...] | None = None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.next_sequence = 1
            self.watermark = 0
            self.visible: dict[int, bool] = {}

        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            if "SELECT state, visibility_watermark" in statement:
                return Result(None)
            if "UPDATE observatory_ingest_allocator" in statement:
                sequence = self.next_sequence
                self.next_sequence += 1
                return Result((sequence,))
            if "INSERT INTO observatory_ingest_health_history" in statement:
                return Result(None)
            if "INSERT INTO observatory_ingest_visibility" in statement:
                sequence = int(params[0])
                self.visible.setdefault(sequence, False)
                return Result(None)
            if "UPDATE observatory_ingest_visibility SET visible" in statement:
                self.visible[int(params[0])] = True
                return Result(None)
            if "SELECT next_sequence FROM observatory_ingest_allocator" in statement:
                return Result((self.next_sequence,))
            if "SELECT committed_sequence" in statement:
                return Result((self.watermark,))
            if "SELECT visible FROM observatory_ingest_visibility" in statement:
                return Result((self.visible.get(int(params[0]), False),))
            if "UPDATE observatory_ingest_watermark" in statement:
                self.watermark = int(params[0])
                return Result(None)
            return Result(None)

    class Context:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context(connection)

    catalog = PostgresCatalog(factory)
    snapshot: dict[str, object] = {
        "snapshotId": "00000000-0000-4000-8000-000000008504",
        "observedAt": BASE_TIME.isoformat(),
        "state": "healthy",
        "intakeLagMs": 0,
        "visibilityWatermark": 0,
        "visibilityLagCount": 0,
        "rejectionReasons": [],
        "duplicateCount": 0,
        "storageLatency": {"sampleCount": 0, "latestMs": None, "p50Ms": None, "p95Ms": None},
        "queryLatency": {"sampleCount": 0, "latestMs": None, "p50Ms": None, "p95Ms": None},
        "queryFreshnessLagMs": None,
        "workerHeartbeatAt": None,
        "workerHeartbeatAgeMs": None,
        "backlogCount": 0,
        "oldestBacklogAgeMs": None,
    }
    await catalog.save_health_snapshot(snapshot)
    assert connection.watermark == 1

    registry = PostgresObservationIdRegistry(factory)
    sequence = await registry.next_ingest_sequence()
    await registry.mark_ingest_visible(sequence)

    assert sequence == 2
    assert connection.watermark == 2


@pytest.mark.asyncio
async def test_postgres_request_malformed_reasons_are_not_rejected_totals() -> None:
    class Result:
        async def fetchone(self) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.rejected = 0
            self.malformed = 0
            self.reasons: dict[str, int] = {}

        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            if "INSERT INTO observatory_ingest_counters" in statement:
                self.rejected += int(params[0])
                self.malformed += int(params[1])
                reason = str(params[2])
                amount = int(params[3])
                self.reasons[reason] = self.reasons.get(reason, 0) + amount
            return Result()

    class Context:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context(connection)

    catalog = PostgresCatalog(factory)
    await catalog.record_rejection("json", 2)
    await catalog.record_rejection("payload_too_large", 2)
    await catalog.record_rejection("shape_object", 2)
    await catalog.record_rejection("schema_validation", 3)

    assert connection.malformed == 6
    assert connection.rejected == 3
    assert connection.reasons == {
        "json": 2,
        "payload_too_large": 2,
        "shape_object": 2,
        "schema_validation": 3,
    }


@pytest.mark.asyncio
async def test_ingest_future_and_out_of_order_observations_keep_lag_bounded() -> None:
    service = TelemetryIngestService(
        MemoryObservationStore(), MemoryCatalog(), credential="collector-secret"
    )
    future = make_payload(
        "00000000-0000-4000-8000-000000008501",
        observed_at=datetime.now(UTC) + timedelta(days=1),
        run_id="00000000-0000-4000-8000-000000008511",
    )
    older = make_payload(
        "00000000-0000-4000-8000-000000008502",
        observed_at=datetime.now(UTC) - timedelta(hours=1),
        run_id="00000000-0000-4000-8000-000000008512",
    )

    future_result = await service.ingest([future.to_payload()], credential="collector-secret")
    older_result = await service.ingest([older.to_payload()], credential="collector-secret")
    assert future_result["accepted"] == 1
    assert older_result["accepted"] == 1
    await service.record_worker_heartbeat(datetime.now(UTC))
    snapshot = await service.health_snapshot()

    # Future skew cannot produce a negative public lag, and a later older
    # observation cannot move the high-water mark backwards.
    assert snapshot["intakeLagMs"] == 0
    assert snapshot["workerHeartbeatAgeMs"] is not None
    assert snapshot["state"] == HealthState.HEALTHY


@pytest.mark.asyncio
async def test_storage_failure_and_rejected_payload_are_aggregate_only() -> None:
    class FailingStore(MemoryObservationStore):
        async def insert_observation(self, observation: Any) -> Any:
            del observation
            raise RuntimeError("private payload and database endpoint must not escape")

    service = TelemetryIngestService(
        FailingStore(), MemoryCatalog(), credential="collector-secret"
    )
    valid = make_payload(
        "00000000-0000-4000-8000-000000008503",
        observed_at=BASE_TIME,
        run_id="00000000-0000-4000-8000-000000008513",
    )
    result = await service.ingest(
        [valid.to_payload(), {"prompt": "PRIVATE_PROMPT", "response": "PRIVATE_RESPONSE"}],
        credential="collector-secret",
    )
    assert result["storage_failure"] == 1
    assert result["rejected"] == 1
    snapshot = await service.health_snapshot()
    serialized = repr(snapshot).lower()
    assert "private_prompt" not in serialized
    assert "private_response" not in serialized
    assert "database endpoint" not in serialized
    assert snapshot["rejectionReasons"] == [{"reason": "schema_validation", "count": 1}]


@pytest.mark.asyncio
async def test_malformed_batch_counts_once_and_writes_no_observation() -> None:
    store = MemoryObservationStore()
    service = TelemetryIngestService(store, MemoryCatalog(), credential="collector-secret")

    with pytest.raises(IngestPayloadError):
        await service.ingest(
            {"observations": {"tool_arguments": "PRIVATE_ARGUMENTS"}},
            credential="collector-secret",
        )

    assert not store.observations
    assert service.counters.malformed == 1
    snapshot = await service.health_snapshot()
    assert snapshot["rejectionReasons"] == [{"reason": "malformed", "count": 1}]
    assert "PRIVATE_ARGUMENTS" not in repr(snapshot)


def test_health_state_thresholds_distinguish_missing_stale_partial_delayed_and_degraded() -> None:
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    missing = IngestHealth().snapshot(now=now)
    assert missing.state is HealthState.UNAVAILABLE

    stale_health = IngestHealth()
    stale_health.heartbeat(now - timedelta(minutes=3))
    assert stale_health.snapshot(now=now).state is HealthState.UNAVAILABLE

    delayed_health = IngestHealth()
    delayed_health.heartbeat(now)
    delayed_health.accepted(now - timedelta(minutes=3), now=now)
    assert delayed_health.snapshot(now=now).state is HealthState.DELAYED

    partial_health = IngestHealth()
    partial_health.heartbeat(now)
    partial_health.visibility(12, 1, 0)
    assert partial_health.snapshot(now=now).state is HealthState.PARTIAL

    degraded_health = IngestHealth()
    degraded_health.heartbeat(now)
    degraded_health.rejection("schema_validation")
    assert degraded_health.snapshot(now=now).state is HealthState.DEGRADED


@pytest.mark.asyncio
async def test_health_history_is_bounded_and_heartbeat_only_updates_are_not_material() -> None:
    store = MemoryObservatoryStore()
    first = await store.health_snapshot()
    await store.save_health_snapshot(first)
    await store.record_worker_heartbeat(datetime.now(UTC))
    await store.save_health_snapshot(await store.health_snapshot())

    # Heartbeat timestamp and age are operational detail, but not a persisted
    # health.changed event by themselves.
    assert len(store.health_history_rows) == 1
    assert len(store.health_events) == 1
    await store.record_latency("query", 23)
    await store.record_query_freshness(BASE_TIME)
    await store.record_visibility(0, 0, 0)
    await store.save_health_snapshot(await store.health_snapshot())
    assert len(store.health_history_rows) == 1
    assert len(store.health_events) == 1

    for watermark in range(1, 1006):
        await store.record_visibility(watermark, 0, 0)
        await store.save_health_snapshot(await store.health_snapshot())
    assert len(store.health_history_rows) == 1000
    page = await store.health_history(cursor=None, from_time=None, to_time=None, limit=200)
    items = cast(list[dict[str, object]], page["items"])
    assert isinstance(items, list)
    assert len(items) == 200
    assert all(set(item) <= {
        "snapshotId",
        "observedAt",
        "state",
        "intakeLagMs",
        "visibilityWatermark",
        "visibilityLagCount",
        "rejectionReasons",
        "duplicateCount",
        "storageLatency",
        "queryLatency",
        "queryFreshnessLagMs",
        "workerHeartbeatAt",
        "workerHeartbeatAgeMs",
        "backlogCount",
        "oldestBacklogAgeMs",
    } for item in items)


@pytest.mark.asyncio
async def test_health_projector_persists_idle_stale_and_recovery_transitions() -> None:
    store = MemoryObservatoryStore()
    projector = ObservatoryHealthProjector(store)

    await projector.project_once()
    assert len(store.health_events) == 1
    store.health.heartbeat(datetime.now(UTC) - timedelta(minutes=3))
    await projector.project_once()
    assert store.health_history_rows[-1]["state"] == "unavailable"
    assert len(store.health_events) == 2
    store.health.heartbeat(datetime.now(UTC))
    await projector.project_once()
    assert store.health_history_rows[-1]["state"] == "healthy"
    assert len(store.health_events) == 3


@pytest.mark.asyncio
async def test_api_lifecycle_owns_projection_after_worker_heartbeat_stops() -> None:
    store = MemoryObservatoryStore()
    ingest = TelemetryIngestService(store, store, credential="collector-secret")
    app = create_app(
        ingest_service=ingest,
        query_store=store,
        testing=True,
    )
    app.state.background_tasks_enabled = True
    app.state.health_projector.interval_seconds = 0.01
    stop = asyncio.Event()
    worker = IngestWorker(ingest)

    async def stop_signal() -> None:
        await stop.wait()

    async with app.router.lifespan_context(app):
        worker_task = asyncio.create_task(worker.run(stop_signal()))
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(worker_task, timeout=1.0)
        heartbeat_at = store.health.worker_heartbeat_at
        assert heartbeat_at is not None

        store.health.heartbeat(datetime.now(UTC) - timedelta(minutes=3))
        stale_at = store.health.worker_heartbeat_at
        assert stale_at is not None
        await asyncio.sleep(0.04)
        assert store.health_history_rows[-1]["state"] == "unavailable"
        unavailable_event = str(store.health_events[-1]["id"])
        assert stale_at < heartbeat_at

        store.health.heartbeat(datetime.now(UTC))
        await asyncio.sleep(0.04)
        assert store.health_history_rows[-1]["state"] == "healthy"
        recovery_event = str(store.health_events[-1]["id"])
        assert recovery_event != unavailable_event

        stream = app.state.query.events_after(unavailable_event)
        event = await asyncio.wait_for(anext(stream), timeout=1.0)
        await stream.aclose()
        assert event["type"] == "health.changed"
        assert event["id"] == recovery_event
        assert event["id"] != unavailable_event


@pytest.mark.asyncio
async def test_health_changed_replay_is_material_only_and_metadata_free() -> None:
    store = MemoryObservatoryStore()
    await store.save_health_snapshot(await store.health_snapshot())
    first_event_id = str(store.health_events[0]["id"])
    # A second snapshot with the same state and watermark must not create a
    # heartbeat-shaped persisted event.
    await store.record_worker_heartbeat(datetime.now(UTC))
    await store.save_health_snapshot(await store.health_snapshot())
    assert len(store.health_events) == 1

    await store.record_visibility(1, 0, 0)
    await store.save_health_snapshot(await store.health_snapshot())
    assert len(store.health_events) == 2

    stream = ObservatoryQueryService(store).events_after(first_event_id)
    event = await asyncio.wait_for(anext(stream), timeout=0.2)
    await cast(Any, stream).aclose()
    assert event["type"] == "health.changed"
    assert event["id"] == store.health_events[1]["id"]
    assert event["data"] == {}
    assert not any(
        secret in repr(event).lower()
        for secret in ("prompt", "response", "credential", "endpoint", "exception")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "not-a-number"},
        {"cursor": "too-short"},
        {"from": "not-a-timestamp"},
        {"to": "2026-10-16T12:00:00"},
    ],
)
async def test_health_history_rejects_malformed_or_unbounded_query_bounds(
    params: dict[str, str],
) -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get("/api/v1/health/history", params=params)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_health_history_requires_owner_session() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    non_owner_id, _ = await auth.create_session(Principal(ISSUER, "other-subject", "Other"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
    ) as client:
        anonymous = await client.get("/api/v1/health/history")
        non_owner = await client.get(
            "/api/v1/health/history",
            cookies={settings.effective_session_cookie_name: non_owner_id},
        )
    assert anonymous.status_code in {401, 403}
    assert non_owner.status_code == 403


def test_health_history_migration_is_forward_only_rerunnable_and_content_free() -> None:
    migration = (
        SERVICE_ROOT / "migrations" / "postgresql" / "versions" / "0005_ingest_health_history.sql"
    ).read_text(encoding="utf-8").lower()
    executable = "\n".join(
        line for line in migration.splitlines() if not line.lstrip().startswith("--")
    )
    assert "alter table observatory_ingest_counters" in executable
    assert "add column if not exists" in executable
    assert "storage_latency_count bigint not null default 0" in executable
    assert "storage_latency_sum double precision not null default 0" in executable
    assert "storage_latency_buckets jsonb not null default '{}'::jsonb" in executable
    assert "create table if not exists observatory_ingest_health_history" in executable
    assert "create index if not exists" in executable
    assert "drop table" not in executable
    assert not any(
        forbidden in executable
        for forbidden in (
            "prompt",
            "response",
            "filename",
            "credential",
            "bearer",
            "owner_identity",
        )
    )
