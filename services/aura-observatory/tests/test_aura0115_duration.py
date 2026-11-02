"""AURA-0115 compatibility projection for canonical run durations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.query import ObservatoryQueryService, RunFilters

BASE_TIME = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _run_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"aura-0115:{label}"))


def _observation(
    observation_id: str,
    run_id: str,
    observed_at: datetime,
    *,
    name: str,
    value: float,
    unit: str | None = "count",
    status: str = "succeeded",
) -> MetadataObservation:
    measurement: dict[str, object] = {"name": name, "value": value}
    if unit is not None:
        measurement["unit"] = unit
    return MetadataObservation.from_payload(
        {
            "schemaVersion": 1,
            "observationId": observation_id,
            "observedAt": observed_at.isoformat(),
            "kind": "metric",
            "component": {"id": "aura.execution.run_coordinator", "version": "1.0.0"},
            "operation": "run_lifecycle",
            "measurement": measurement,
            "dimensions": {"status": status},
            "trace": {"traceId": run_id.replace("-", ""), "spanId": "0123456789abcdef"},
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


@pytest.mark.asyncio
async def test_reference_list_and_detail_default_only_canonical_duration_unit() -> None:
    run_id = _run_id("reference")
    store = MemoryObservatoryStore()
    await store.insert_observation(
        _observation(
            "00000000-0000-4000-8000-000000000001",
            run_id,
            BASE_TIME,
            name="run_started",
            value=1,
            unit="count",
            status="running",
        )
    )
    await store.insert_observation(
        _observation(
            "00000000-0000-4000-8000-000000000002",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_duration_ms",
            value=2000,
            unit=None,
        )
    )
    await store.insert_observation(
        _observation(
            "00000000-0000-4000-8000-000000000003",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="run_status",
            value=1,
            unit="count",
        )
    )
    await store.insert_observation(
        _observation(
            "00000000-0000-4000-8000-000000000004",
            run_id,
            BASE_TIME + timedelta(seconds=2),
            name="queue_wait_ms",
            value=7,
            unit=None,
        )
    )

    service = ObservatoryQueryService(store)
    listed = (await service.runs(RunFilters(), cursor=None, limit=10))["items"][0]
    detail = await service.run_detail(run_id)

    assert listed["durationMs"] == 2000
    assert detail is not None
    assert detail["durationMs"] == 2000
    observations = {item["observationId"]: item for item in detail["observations"]}
    assert observations["00000000-0000-4000-8000-000000000002"]["measurement"]["unit"] == (
        "milliseconds"
    )
    assert observations["00000000-0000-4000-8000-000000000004"]["measurement"]["unit"] is None


def _production_row(
    run_id: str,
    observation_id: str,
    observed_at: datetime,
    *,
    name: str,
    value: float,
    unit: str,
    status: str,
) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "observed_at": observed_at,
        "schema_version": 1,
        "kind": "metric",
        "component_id": "aura.execution.run_coordinator",
        "component_version": "1.0.0",
        "operation": "run_lifecycle",
        "measurement_name": name,
        "measurement_value": value,
        "measurement_unit": unit,
        "dimensions": {"status": status},
        "trace_id": run_id.replace("-", ""),
        "span_id": "0123456789abcdef",
        "parent_span_id": "",
        "linked_span_ids": [],
        "run_id": run_id,
        "conversation_id": "",
        "deployment_id": "deploy-a",
        "git_commit": "0123456789abcdef01234567",
        "environment": "test",
        "host_id": "host-test",
        "service_role": "worker",
        "capture_policy": "metadata_only",
    }


@pytest.mark.asyncio
async def test_production_list_and_detail_accept_empty_duration_unit() -> None:
    run_id = _run_id("production")
    statements: list[str] = []
    duration_at = BASE_TIME + timedelta(seconds=2)
    rows = [
        _production_row(
            run_id,
            "00000000-0000-4000-8000-000000000011",
            duration_at,
            name="run_duration_ms",
            value=2000,
            unit="",
            status="succeeded",
        ),
        _production_row(
            run_id,
            "00000000-0000-4000-8000-000000000012",
            duration_at,
            name="run_status",
            value=1,
            unit="count",
            status="succeeded",
        ),
    ]

    class Store(ProductionObservatoryStore):
        async def _committed_watermark(self) -> int | None:
            return 2

        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 2, "observed_at": duration_at}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            statements.append(statement)
            if "GROUP BY run_id" in statement:
                return [
                    {
                        "run_id": run_id,
                        "latest_at": duration_at,
                        "started_at": BASE_TIME,
                        "status": "succeeded",
                        "run_status_count": 1,
                        "run_status_at": duration_at,
                        "run_started_count": 1,
                        "run_started_at": BASE_TIME,
                        "run_duration_count": 1,
                        "run_duration_ms": 2000,
                        "run_duration_at": duration_at,
                        "model_execution_count": 0,
                        "deployment_id": "deploy-a",
                        "component_ids": ["aura.execution.run_coordinator"],
                        "lifecycle_trace_id": run_id.replace("-", ""),
                        "order_sequence": 2,
                    }
                ]
            return rows

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )

    listed = (await store.run_query(RunFilters(), cursor=None, limit=10))["items"][0]
    detail = await store.run_detail_query(run_id)

    assert listed["durationMs"] == 2000
    assert detail is not None
    assert detail["durationMs"] == 2000
    duration = next(
        item for item in detail["observations"] if item["operation"] == "run_lifecycle"
    )
    assert duration["measurement"]["name"] == "run_duration_ms"
    assert duration["measurement"]["unit"] == "milliseconds"
    assert any("measurement_unit = ''" in statement for statement in statements)
