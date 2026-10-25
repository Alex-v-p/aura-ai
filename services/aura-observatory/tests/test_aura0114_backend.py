"""AURA-0114 regressions for immutable lifecycle run projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.query import ObservatoryQueryService, RunFilters, decode_run_cursor

BASE_TIME = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _run_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"aura-0114:{label}"))


def _observation(
    observation_id: str,
    run_id: str,
    observed_at: datetime,
    *,
    name: str = "duration_ms",
    value: float = 10.0,
    status: str = "succeeded",
    trace_id: str = "0123456789abcdef0123456789abcdef",
    outcome: str = "success",
    component: str = "aura.execution.run_coordinator",
    provider: str | None = "local-model",
) -> MetadataObservation:
    return MetadataObservation.from_payload(
        {
            "schemaVersion": 1,
            "observationId": observation_id,
            "observedAt": observed_at.isoformat(),
            "kind": "metric",
            "component": {"id": component, "version": "1.0.0"},
            "operation": "run_lifecycle",
            "measurement": {
                "name": name,
                "value": value,
                "unit": "milliseconds" if name == "run_duration_ms" else "count",
            },
            "dimensions": {
                **{"status": status, "outcome": outcome},
                **({"provider": provider} if provider is not None else {}),
            },
            "trace": {"traceId": trace_id, "spanId": "0123456789abcdef"},
            "correlation": {"runId": run_id},
            "deployment": {
                "environment": "test",
                "deploymentId": "deploy-a",
                "gitCommit": "0123456789abcdef01234567",
                "hostId": "host-test",
            },
            "serviceRole": "worker",
            "capturePolicy": "metadata_only",
        }
    )


async def _seed(store: MemoryObservatoryStore, *items: MetadataObservation) -> None:
    for item in items:
        await store.insert_observation(item)


@pytest.mark.asyncio
async def test_lifecycle_header_ignores_late_background_telemetry_in_list_and_detail() -> None:
    run_id = _run_id("stable")
    run_trace = run_id.replace("-", "")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000001",
            run_id,
            BASE_TIME,
            name="run_started",
            trace_id=run_trace,
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000002",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_status",
            trace_id=run_trace,
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000003",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_duration_ms",
            value=1234.5,
            trace_id=run_trace,
            status="succeeded",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000004",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_status",
            trace_id=run_trace,
            status="succeeded",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000005",
            run_id,
            BASE_TIME + timedelta(hours=3),
            name="memory_retry_count",
            value=99,
            trace_id="fedcba9876543210fedcba9876543210",
            status="failed",
            outcome="error",
            provider=None,
        ),
    )

    service = ObservatoryQueryService(store)
    listed = (await service.runs(RunFilters(), cursor=None, limit=10))["items"][0]
    detail = await service.run_detail(run_id)

    assert listed["status"] == "succeeded"
    assert listed["durationMs"] == 1234.5
    assert listed["startedAt"] == BASE_TIME.isoformat().replace("+00:00", "Z")
    assert listed["finishedAt"] == (BASE_TIME + timedelta(seconds=2)).isoformat().replace(
        "+00:00", "Z"
    )
    assert listed["traceId"] == run_trace
    assert detail is not None
    assert detail["status"] == listed["status"]
    assert detail["durationMs"] == listed["durationMs"]
    assert detail["traceId"] == listed["traceId"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer_status", "expected_status"),
    [("failed", "failed"), ("canceled", "cancelled")],
)
async def test_terminal_lifecycle_states_are_projected_for_each_outcome(
    producer_status: str, expected_status: str
) -> None:
    run_id = _run_id(producer_status)
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000011",
            run_id,
            BASE_TIME,
            name="run_started",
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000012",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_duration_ms",
            value=25,
            status=producer_status,
            outcome="error" if producer_status == "failed" else producer_status,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000013",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_status",
            status=producer_status,
            outcome="error" if producer_status == "failed" else producer_status,
        ),
    )
    result = (await ObservatoryQueryService(store).runs(RunFilters(), cursor=None, limit=10))[
        "items"
    ][0]
    assert result["status"] == expected_status
    assert result["durationMs"] == 25
    if expected_status == "cancelled":
        filtered = await ObservatoryQueryService(store).runs(
            RunFilters(status="cancelled"), cursor=None, limit=10
        )
        assert [item["runId"] for item in filtered["items"]] == [run_id]


@pytest.mark.asyncio
async def test_legacy_runs_keep_legacy_duration_and_trace_fallback() -> None:
    run_id = _run_id("legacy")
    store = MemoryObservatoryStore()
    first_trace = "0123456789abcdef0123456789abcdef"
    later_trace = "fedcba9876543210fedcba9876543210"
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000021",
            run_id,
            BASE_TIME,
            trace_id=first_trace,
            name="duration_ms",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000022",
            run_id,
            BASE_TIME + timedelta(seconds=5),
            trace_id=later_trace,
            name="memory_retry_count",
            value=1,
        ),
    )
    result = (await ObservatoryQueryService(store).runs(RunFilters(), cursor=None, limit=10))[
        "items"
    ][0]
    assert result["durationMs"] == 5000
    assert result["traceId"] == later_trace


@pytest.mark.asyncio
async def test_discovery_filters_qualify_run_before_full_lifecycle_projection() -> None:
    """A matching row must not hide lifecycle rows from list or detail headers."""

    run_id = _run_id("filtered-lifecycle")
    run_trace = run_id.replace("-", "")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000031",
            run_id,
            BASE_TIME,
            name="run_started",
            trace_id=run_trace,
            status="running",
            provider=None,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000032",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_duration_ms",
            value=321,
            trace_id=run_trace,
            status="succeeded",
            provider=None,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000033",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_status",
            trace_id=run_trace,
            status="succeeded",
            provider=None,
        ),
        # This is the only row inside the discovery window and the only row
        # matching the requested component/model.  It is not lifecycle data.
        _observation(
            "00000000-0000-4000-8000-000000000034",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="model_tokens",
            component="aura.runtime.model_inference",
            provider="local-model",
            status="succeeded",
        ),
    )
    service = ObservatoryQueryService(store)
    for discovery_filters in (
        RunFilters(component="aura.runtime.model_inference"),
        RunFilters(model="local-model"),
        RunFilters(
            from_time=BASE_TIME + timedelta(seconds=2),
            to_time=BASE_TIME + timedelta(seconds=3),
        ),
    ):
        listed = (await service.runs(discovery_filters, cursor=None, limit=10))["items"]
        assert len(listed) == 1
        assert listed[0]["runId"] == run_id
        assert listed[0]["status"] == "succeeded"
        assert listed[0]["durationMs"] == 321
        assert listed[0]["startedAt"] == BASE_TIME.isoformat().replace("+00:00", "Z")

    combined = RunFilters(
        component="aura.runtime.model_inference",
        model="local-model",
        status="succeeded",
        from_time=BASE_TIME + timedelta(seconds=2),
        to_time=BASE_TIME + timedelta(seconds=3),
    )
    listed = (await service.runs(combined, cursor=None, limit=10))["items"]
    assert len(listed) == 1
    assert listed[0]["runId"] == run_id

    detail = await service.run_detail(run_id)
    assert detail is not None
    assert detail["status"] == "succeeded"
    assert detail["durationMs"] == 321


@pytest.mark.asyncio
async def test_disjoint_discovery_rows_do_not_qualify_reference_or_production() -> None:
    """All discovery predicates must match one observation, not the run union."""

    run_id = _run_id("disjoint-filter-rows")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000041",
            run_id,
            BASE_TIME,
            name="run_started",
            provider=None,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000042",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_duration_ms",
            value=20,
            provider=None,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000043",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_status",
            provider=None,
        ),
        # Component and model predicates are intentionally split across rows.
        _observation(
            "00000000-0000-4000-8000-000000000044",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="model_tokens",
            component="aura.runtime.model_inference",
            provider="other-model",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000045",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="model_tokens",
            component="aura.execution.run_coordinator",
            provider="local-model",
        ),
    )
    filters = RunFilters(
        component="aura.runtime.model_inference",
        model="local-model",
        status="succeeded",
    )
    reference = await ObservatoryQueryService(store).runs(filters, cursor=None, limit=10)
    assert reference["items"] == []

    statements: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 5, "observed_at": BASE_TIME + timedelta(seconds=2)}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            statements.append(statement)
            # The disjoint fixture above is represented by the empty result
            # after ClickHouse evaluates the single-row qualification.
            return []

    production = Store(
        lambda: None, "http://clickhouse.test", database="observatory", user="u", password="p"
    )
    result = await production.run_query(filters, cursor=None, limit=10)
    assert result["items"] == []
    assert (
        "countIf(component_id = 'aura.runtime.model_inference' "
        "AND dimensions['provider'] = 'local-model') > 0"
    ) in statements[0]


@pytest.mark.asyncio
async def test_running_lifecycle_has_no_fabricated_finished_at() -> None:
    run_id = _run_id("running")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000031",
            run_id,
            BASE_TIME,
            name="run_started",
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000032",
            run_id,
            BASE_TIME + timedelta(hours=2),
            name="run_status",
            status="running",
        ),
    )
    result = (await ObservatoryQueryService(store).runs(RunFilters(), cursor=None, limit=10))[
        "items"
    ][0]
    assert result["status"] == "running"
    assert result["finishedAt"] is None
    assert result["durationMs"] is None


@pytest.mark.asyncio
async def test_model_stays_on_execution_evidence_after_providerless_background_row() -> None:
    run_id = _run_id("model-stable")
    run_trace = run_id.replace("-", "")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000041",
            run_id,
            BASE_TIME,
            name="run_started",
            trace_id=run_trace,
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000042",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="model.route",
            trace_id=run_trace,
            component="aura.runtime.model_routing",
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000043",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_duration_ms",
            value=20,
            trace_id=run_trace,
            status="succeeded",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000044",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_status",
            trace_id=run_trace,
            status="succeeded",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000045",
            run_id,
            BASE_TIME + timedelta(hours=1),
            name="memory_retry_count",
            value=1,
            trace_id="fedcba9876543210fedcba9876543210",
            status="failed",
            outcome="error",
        ),
    )
    result = (await ObservatoryQueryService(store).runs(RunFilters(), cursor=None, limit=10))[
        "items"
    ][0]
    assert result["model"] == "local-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer_status", ["succeeded", "failed", "canceled"])
async def test_detail_header_uses_full_snapshot_when_terminal_rows_are_on_later_pages(
    producer_status: str,
) -> None:
    run_id = _run_id(f"paged-{producer_status}")
    store = MemoryObservatoryStore()
    await _seed(
        store,
        _observation(
            "00000000-0000-4000-8000-000000000051",
            run_id,
            BASE_TIME,
            name="run_started",
            status="running",
        ),
        _observation(
            "00000000-0000-4000-8000-000000000052",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_duration_ms",
            value=30,
            status=producer_status,
            outcome="error" if producer_status == "failed" else producer_status,
        ),
        _observation(
            "00000000-0000-4000-8000-000000000053",
            run_id,
            BASE_TIME + timedelta(seconds=1),
            name="run_status",
            status=producer_status,
            outcome="error" if producer_status == "failed" else producer_status,
        ),
    )
    service = ObservatoryQueryService(store)
    first = await service.run_detail(run_id, limit=1)
    assert first is not None
    assert first["status"] == ("cancelled" if producer_status == "canceled" else producer_status)
    assert first["durationMs"] == 30
    assert first["nextCursor"] is not None
    second = await service.run_detail(run_id, cursor=first["nextCursor"], limit=1)
    assert second is not None
    assert second["status"] == first["status"]
    assert second["durationMs"] == first["durationMs"]


@pytest.mark.asyncio
async def test_production_query_asks_clickhouse_for_lifecycle_projection() -> None:
    run_id = _run_id("production")
    statements: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 1, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            statements.append(statement)
            return [
                {
                    "run_id": run_id,
                    "started_at": BASE_TIME,
                    "finished_at": BASE_TIME + timedelta(hours=2),
                    "status": "succeeded",
                    "run_status_count": 1,
                    "run_status_at": BASE_TIME + timedelta(seconds=2),
                    "run_started_count": 1,
                    "run_started_at": BASE_TIME,
                    "run_duration_count": 1,
                    "run_duration_ms": 37.25,
                    "run_duration_at": BASE_TIME + timedelta(seconds=2),
                    "model_execution_count": 1,
                    "model_execution": "local-model",
                    "model": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": ["aura.execution.run_coordinator"],
                    "primary_trace_id": run_id.replace("-", ""),
                    "legacy_trace_id": "fedcba9876543210fedcba9876543210",
                    "order_sequence": 1,
                }
            ]

    store = Store(
        lambda: None, "http://clickhouse.test", database="observatory", user="u", password="p"
    )
    result = await store.run_query(RunFilters(), cursor=None, limit=10)
    item = result["items"][0]
    assert item["durationMs"] == 37.25
    assert item["startedAt"] == BASE_TIME.isoformat().replace("+00:00", "Z")
    assert item["traceId"] == run_id.replace("-", "")
    assert "run_duration_ms" in statements[0]
    assert "run_status" in statements[0]
    assert "primary_trace_id" in statements[0]


@pytest.mark.asyncio
async def test_production_discovery_filters_qualify_ids_without_narrowing_projection() -> None:
    run_id = _run_id("production-filtered-lifecycle")
    statements: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 4, "observed_at": BASE_TIME + timedelta(seconds=2)}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            statements.append(statement)
            return [
                {
                    "run_id": run_id,
                    "latest_at": BASE_TIME + timedelta(seconds=2),
                    "started_at": BASE_TIME,
                    "finished_at": BASE_TIME + timedelta(seconds=2),
                    "status": "succeeded",
                    "run_status_count": 1,
                    "run_status_at": BASE_TIME + timedelta(seconds=1),
                    "run_started_count": 1,
                    "run_started_at": BASE_TIME,
                    "run_duration_count": 1,
                    "run_duration_ms": 321,
                    "run_duration_at": BASE_TIME + timedelta(seconds=1),
                    "model_execution_count": 1,
                    "model_execution": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": ["aura.execution.run_coordinator"],
                    "primary_trace_id": run_id.replace("-", ""),
                    "order_sequence": 4,
                }
            ]

    store = Store(
        lambda: None, "http://clickhouse.test", database="observatory", user="u", password="p"
    )
    result = await store.run_query(
        RunFilters(
            component="aura.runtime.model_inference",
            model="local-model",
            status="succeeded",
            from_time=BASE_TIME + timedelta(seconds=2),
            to_time=BASE_TIME + timedelta(seconds=3),
        ),
        cursor=None,
        limit=10,
    )

    assert result["items"][0]["status"] == "succeeded"
    assert result["items"][0]["durationMs"] == 321
    statement = statements[0]
    assert "WHERE component_id =" not in statement
    assert "WHERE dimensions['provider'] =" not in statement
    assert "countIf(component_id = 'aura.runtime.model_inference' AND" in statement
    assert "dimensions['provider'] = 'local-model'" in statement
    assert "WHERE observed_at >=" not in statement
    assert "AND observed_at >=" in statement
    assert "AND observed_at <" in statement
    assert "HAVING" in statement


@pytest.mark.asyncio
async def test_production_legacy_trace_fallback_matches_reference_newest_policy() -> None:
    run_id = _run_id("production-legacy")

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 1, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del statement, params
            return [
                {
                    "run_id": run_id,
                    "started_at": BASE_TIME,
                    "finished_at": BASE_TIME + timedelta(seconds=2),
                    "status": "succeeded",
                    "model": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": ["aura.runtime.model_inference"],
                    "trace_id": "fedcba9876543210fedcba9876543210",
                    "legacy_trace_id": "0123456789abcdef0123456789abcdef",
                    "order_sequence": 1,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.run_query(RunFilters(), cursor=None, limit=10)
    assert result["items"][0]["traceId"] == "fedcba9876543210fedcba9876543210"


@pytest.mark.asyncio
async def test_production_canceled_status_uses_public_cancelled_vocabulary() -> None:
    run_id = _run_id("production-canceled")

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 1, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del statement, params
            return [
                {
                    "run_id": run_id,
                    "started_at": BASE_TIME,
                    "finished_at": BASE_TIME,
                    "status": "canceled",
                    "run_status_count": 1,
                    "run_status_at": BASE_TIME + timedelta(seconds=1),
                    "run_started_count": 1,
                    "run_started_at": BASE_TIME,
                    "run_duration_count": 1,
                    "run_duration_ms": 10,
                    "run_duration_at": BASE_TIME + timedelta(seconds=1),
                    "model_execution_count": 1,
                    "model_execution": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": ["aura.execution.run_coordinator"],
                    "primary_trace_id": run_id.replace("-", ""),
                    "order_sequence": 1,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.run_query(RunFilters(status="cancelled"), cursor=None, limit=10)
    assert result["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_production_cursor_uses_private_latest_order_boundary() -> None:
    run_id = _run_id("cursor-boundary")
    latest_at = BASE_TIME + timedelta(hours=3)

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 1, "observed_at": latest_at}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del statement, params
            return [
                {
                    "run_id": run_id,
                    "latest_at": latest_at,
                    "finished_at": BASE_TIME + timedelta(seconds=2),
                    "status": "succeeded",
                    "run_status_count": 1,
                    "run_started_count": 1,
                    "run_duration_count": 1,
                    "run_duration_ms": 20,
                    "model_execution_count": 1,
                    "model_execution": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": [],
                    "order_sequence": 1,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.run_query(RunFilters(), cursor=None, limit=1)
    assert result["nextCursor"] is not None
    _, cursor_at, _ = decode_run_cursor(result["nextCursor"])
    assert cursor_at == latest_at
