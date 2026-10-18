"""Adversarial and bounded evaluation coverage for AURA-0088."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.otlp import OtlpPayloadError, traces_to_observations
from aura_observatory.query import COMPARISON_QUERY_ROWS, ObservatoryQueryService

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _observation(
    sequence: int,
    run_id: str,
    *,
    value: float = 1,
    span_id: str = "0123456789abcde1",
    parent_span_id: str | None = None,
    image: str | None = "image-a",
    include_model: bool = True,
    unit: str = "milliseconds",
    schema_version: int = 2,
) -> MetadataObservation:
    deployment = {
        "deploymentId": "deploy-a",
        "gitCommit": "abcdef1",
        "environment": "dev",
        "hostId": "host-a",
    }
    if schema_version == 2 and image is not None:
        deployment["imageVersion"] = image
    execution = {"modelId": "model-a"} if schema_version == 2 and include_model else None
    trace = {"traceId": "0123456789abcdef0123456789abcdef", "spanId": span_id}
    if parent_span_id:
        trace["parentSpanId"] = parent_span_id
    payload: dict[str, object] = {
        "schemaVersion": schema_version,
        "observationId": f"00000000-0000-4000-8000-0000000000{sequence:02}",
        "observedAt": (BASE + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z"),
        "kind": "span",
        "component": {"id": "aura.runtime.model_inference", "version": "1.0"},
        "operation": "run",
        "measurement": {"name": "duration_ms", "value": value, "unit": unit},
        "dimensions": {"outcome": "success"},
        "trace": trace,
        "correlation": {"runId": run_id},
        "deployment": deployment,
        "serviceRole": "api",
        "capturePolicy": "metadata_only",
    }
    if execution is not None:
        payload["execution"] = execution
    return MetadataObservation.from_payload(payload)


@pytest.mark.asyncio
async def test_trace_alignment_preserves_duplicate_and_cyclic_evidence() -> None:
    """Malformed lineage must remain explicit instead of becoming a match."""

    left_run = "00000000-0000-4000-8000-000000000011"
    right_run = "00000000-0000-4000-8000-000000000012"
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(11, left_run, span_id="aaaaaaaaaaaaaaaa"))
    await store.insert_observation(_observation(12, left_run, span_id="aaaaaaaaaaaaaaaa"))
    await store.insert_observation(
        _observation(13, right_run, span_id="bbbbbbbbbbbbbbbb", parent_span_id="cccccccccccccccc")
    )
    await store.insert_observation(
        _observation(14, right_run, span_id="cccccccccccccccc", parent_span_id="bbbbbbbbbbbbbbbb")
    )

    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

    assert result is not None
    trace = cast(dict[str, Any], result["trace"])
    assert "duplicate" in trace["leftIntegrity"]
    assert "cyclic" in trace["rightIntegrity"]
    aligned = cast(list[dict[str, Any]], trace["alignedSpans"])
    assert any(item["alignment"] == "incompatible" for item in aligned)
    assert trace["partial"] is True


@pytest.mark.asyncio
async def test_comparison_separates_incompatible_units_and_retention_evidence() -> None:
    left_run = "00000000-0000-4000-8000-000000000021"
    right_run = "00000000-0000-4000-8000-000000000022"
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(21, left_run, value=10, unit="milliseconds"))
    await store.insert_observation(_observation(22, right_run, value=10, unit="count"))

    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)
    assert result is not None
    components = cast(list[dict[str, Any]], result["components"])
    measurements = cast(list[dict[str, Any]], components[0]["measurements"])
    assert {item["unit"] for item in measurements} == {"milliseconds", "count"}
    assert all(item["state"] == "missing" for item in measurements)
    assert all(item["absoluteDelta"] is None for item in measurements)

    deleted = await store.delete_observations(BASE + timedelta(days=1))
    assert deleted.observations == 2
    assert await ObservatoryQueryService(store).compare_runs(left_run, right_run) is None
    assert await store.components()
    assert await store.deployments()


@pytest.mark.asyncio
async def test_comparison_reference_query_requests_fixed_scan_bound() -> None:
    class BoundedStore:
        def __init__(self) -> None:
            self.requested_limit: int | None = None

        async def list_observations(self, *, limit: int | None = None) -> list[MetadataObservation]:
            self.requested_limit = limit
            return []

    store = BoundedStore()
    service = ObservatoryQueryService(cast(Any, store))
    assert await service.compare_runs(
        "00000000-0000-4000-8000-000000000031",
        "00000000-0000-4000-8000-000000000032",
    ) is None
    assert store.requested_limit == COMPARISON_QUERY_ROWS
    assert store.requested_limit == 20_002


@pytest.mark.asyncio
async def test_v1_marker_absence_is_unavailable_and_v2_omission_is_missing() -> None:
    v1_run = "00000000-0000-4000-8000-000000000051"
    v2_missing_run = "00000000-0000-4000-8000-000000000052"
    v2_complete_run = "00000000-0000-4000-8000-000000000053"
    store = MemoryObservatoryStore()
    await store.insert_observation(
        _observation(51, v1_run, image=None, include_model=False, schema_version=1)
    )
    await store.insert_observation(
        _observation(52, v2_missing_run, image=None, include_model=False, schema_version=2)
    )
    await store.insert_observation(_observation(53, v2_complete_run, schema_version=2))
    service = ObservatoryQueryService(store)

    v1_result = await service.compare_runs(v1_run, v2_complete_run)
    assert v1_result is not None
    v1_markers = {
        item["marker"]: item for item in cast(list[dict[str, Any]], v1_result["markerCorrelations"])
    }
    assert v1_markers["image_version"]["state"] == "unavailable"
    assert v1_markers["model_id"]["state"] == "unavailable"

    v2_result = await service.compare_runs(v2_missing_run, v2_complete_run)
    assert v2_result is not None
    v2_markers = {
        item["marker"]: item for item in cast(list[dict[str, Any]], v2_result["markerCorrelations"])
    }
    assert v2_markers["image_version"]["state"] == "missing"
    assert v2_markers["model_id"]["state"] == "missing"


def test_otlp_v1_and_v2_mapping_rejects_content_like_attributes() -> None:
    def attribute(key: str, value: str) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": value}}

    def payload(*, v2: bool, sensitive: bool = False) -> dict[str, object]:
        schema_url = (
            "https://aura.local/contracts/telemetry/v2/metadata-observation.schema.json"
            if v2
            else "https://aura.local/contracts/telemetry/v1/metadata-observation.schema.json"
        )
        resource_attributes = [
            attribute("service.name", "aura-core"),
            attribute("service.role", "worker"),
            attribute("deployment.id", "deploy-a"),
            attribute("deployment.environment", "test"),
            attribute("deployment.git_commit", "abcdef1"),
            attribute("host.id", "host-a"),
            attribute("capture.policy", "metadata_only"),
        ]
        if v2:
            resource_attributes.append(attribute("deployment.image_version", "image-a"))
        span_attributes = [
            attribute("aura.observation_id", "00000000-0000-4000-8000-000000000041"),
            attribute("aura.component_id", "aura.runtime.model_inference"),
            attribute("aura.component_version", "1.0"),
            attribute("aura.operation", "run"),
            attribute("aura.metric_name", "duration_ms"),
            attribute("aura.capture_policy", "metadata_only"),
            attribute("aura.run_id", "00000000-0000-4000-8000-000000000042"),
        ]
        if v2:
            span_attributes.append(attribute("aura.execution.model_id", "model-a"))
        if sensitive:
            span_attributes.append(attribute("prompt", "PRIVATE_PROMPT"))
        return {
            "resourceSpans": [{
                "schemaUrl": schema_url,
                "resource": {"attributes": resource_attributes, "schemaUrl": schema_url},
                "scopeSpans": [{
                    "schemaUrl": schema_url,
                    "scope": {"name": "aura.core.telemetry", "version": "1.0"},
                    "spans": [{
                        "traceId": "0123456789abcdef0123456789abcdef",
                        "spanId": "0123456789abcdef",
                        "name": "run",
                        "startTimeUnixNano": "1735689600000000000",
                        "endTimeUnixNano": "1735689600010000000",
                        "attributes": span_attributes,
                    }],
                }],
            }],
        }

    v1 = traces_to_observations(payload(v2=False))[0]
    v2 = traces_to_observations(payload(v2=True))[0]
    assert v1["schemaVersion"] == 1
    assert v2["schemaVersion"] == 2
    assert MetadataObservation.from_payload(v1).schema_version == 1
    assert MetadataObservation.from_payload(v2).schema_version == 2
    with pytest.raises(OtlpPayloadError):
        traces_to_observations(payload(v2=True, sensitive=True))
