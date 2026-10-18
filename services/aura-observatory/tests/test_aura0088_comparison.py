"""Focused bounded comparison behavior for AURA-0088."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import ProductionObservatoryStore, StorageUnavailableError
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.otlp import OtlpPayloadError, traces_to_observations
from aura_observatory.query import (
    COMPARISON_EXECUTION_TIMEOUT_MS,
    COMPARISON_MAX_OBSERVATIONS,
    COMPARISON_MAX_SCANNED_ROWS,
    ObservatoryQueryService,
    build_run_comparison,
    canonical_subtree_digest,
)
from aura_observatory.settings import Settings

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/"
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
    model_id: str = "model-a",
    name: str = "duration_ms",
    kind: str = "span",
    component_id: str = "aura.runtime.model_inference",
) -> MetadataObservation:
    deployment = {
        "deploymentId": "deploy-a",
        "gitCommit": "abcdef1",
        "environment": "dev",
        "hostId": "host-a",
    }
    if image is not None:
        deployment["imageVersion"] = image
    execution = {"modelId": model_id} if include_model else None
    trace = {"traceId": "0123456789abcdef0123456789abcdef", "spanId": span_id}
    if parent_span_id:
        trace["parentSpanId"] = parent_span_id
    payload = {
        "schemaVersion": 2,
        "observationId": f"00000000-0000-4000-8000-{sequence:012x}",
        "observedAt": (BASE + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "component": {"id": component_id, "version": "1.0"},
        "operation": "run",
        "measurement": {"name": name, "value": value, "unit": "milliseconds"},
        "dimensions": {"outcome": "success"},
        "trace": trace,
        "correlation": {"runId": run_id},
        "deployment": deployment,
        "serviceRole": "api",
        "capturePolicy": "metadata_only",
    }
    if kind != "span":
        payload.pop("trace", None)
    if execution is not None:
        payload["execution"] = execution
    return MetadataObservation.from_payload(payload)


@pytest.mark.asyncio
async def test_memory_comparison_preserves_zero_missing_and_trace_integrity() -> None:
    left_run = "00000000-0000-4000-8000-000000000001"
    right_run = "00000000-0000-4000-8000-000000000002"
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(1, left_run, value=0))
    await store.insert_observation(
        _observation(2, right_run, value=2, parent_span_id="0123456789abcde2", include_model=False)
    )
    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

    assert result is not None
    measurement = result["components"][0]["measurements"][0]  # type: ignore[index]
    assert measurement["left"] == {"availability": "available", "value": 0.0}  # type: ignore[index]
    assert measurement["state"] == "changed"  # type: ignore[index]
    markers = {item["marker"]: item for item in result["markerCorrelations"]}  # type: ignore[index]
    assert markers["model_id"]["state"] == "missing"  # type: ignore[index]
    assert result["trace"]["partial"] is True  # type: ignore[index]
    assert "missing_parent" in result["trace"]["rightIntegrity"]  # type: ignore[index]
    freshness = result["freshness"]  # type: ignore[index]
    assert set(freshness) == {"latestObservedAt", "lagMs", "state"}  # type: ignore[arg-type]
    assert "observedAt" not in freshness and "ageMs" not in freshness  # type: ignore[operator]


@pytest.mark.asyncio
async def test_closed_trace_cycle_is_cyclic_not_missing_parent() -> None:
    left_run = "00000000-0000-4000-8000-000000000061"
    right_run = "00000000-0000-4000-8000-000000000062"
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(61, left_run))
    await store.insert_observation(
        _observation(62, right_run, span_id="aaaaaaaaaaaaaaaa", parent_span_id="bbbbbbbbbbbbbbbb")
    )
    await store.insert_observation(
        _observation(63, right_run, span_id="bbbbbbbbbbbbbbbb", parent_span_id="aaaaaaaaaaaaaaaa")
    )
    await store.insert_observation(
        _observation(64, right_run, span_id="dddddddddddddddd", parent_span_id="aaaaaaaaaaaaaaaa")
    )

    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

    assert result is not None
    trace = result["trace"]
    assert trace["rightIntegrity"] == ["cyclic"]  # type: ignore[index]
    assert "missing_parent" not in trace["rightIntegrity"]  # type: ignore[index]
    aligned = trace["alignedSpans"]  # type: ignore[index]
    cycle_rows = [
        item
        for item in aligned
        if item["right"] is not None
        and item["right"]["spanId"] in {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"}
    ]
    assert cycle_rows and all("cyclic" in item["rightIntegrity"] for item in cycle_rows)
    tail_row = next(
        item
        for item in aligned
        if item["right"] and item["right"]["spanId"] == "dddddddddddddddd"
    )
    assert tail_row["rightIntegrity"] == ["complete"]


@pytest.mark.asyncio
async def test_equivalent_trace_topology_aligns_with_trace_local_ids() -> None:
    left_run = "00000000-0000-4000-8000-000000000071"
    right_run = "00000000-0000-4000-8000-000000000072"
    store = MemoryObservatoryStore()
    await store.insert_observation(
        _observation(71, left_run, span_id="1111111111111111", value=4)
    )
    await store.insert_observation(
        _observation(
            72,
            left_run,
            span_id="2222222222222222",
            parent_span_id="1111111111111111",
            value=2,
        )
    )
    await store.insert_observation(
        _observation(73, right_run, span_id="aaaaaaaaaaaaaaaa", value=5)
    )
    await store.insert_observation(
        _observation(
            74,
            right_run,
            span_id="bbbbbbbbbbbbbbbb",
            parent_span_id="aaaaaaaaaaaaaaaa",
            value=3,
        )
    )

    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

    assert result is not None
    trace = cast(dict[str, Any], result["trace"])
    aligned = cast(list[dict[str, Any]], trace["alignedSpans"])
    assert len(aligned) == 2
    assert all(item["alignment"] == "matched" for item in aligned)
    assert {item["left"]["spanId"] for item in aligned} != {
        item["right"]["spanId"] for item in aligned
    }
    assert {item["alignmentKey"] for item in aligned} == {
        "aura.runtime.model_inference:run:0",
        "aura.runtime.model_inference:run:0:aura.runtime.model_inference:run:0",
    }


def test_reordered_structural_roots_and_siblings_align_without_timing() -> None:
    left_run = "00000000-0000-4000-8000-000000000081"
    right_run = "00000000-0000-4000-8000-000000000082"
    left = [
        _observation(
            81, left_run, span_id="1010101010101010", component_id="aura.runtime.component_a"
        ),
        _observation(
            82, left_run, span_id="2020202020202020", component_id="aura.runtime.component_b"
        ),
        _observation(
            83,
            left_run,
            span_id="1111111111111111",
            component_id="aura.runtime.model_inference",
        ),
        _observation(
            84,
            left_run,
            span_id="2222222222222222",
            parent_span_id="1111111111111111",
            component_id="aura.runtime.component_a",
        ),
        _observation(
            85,
            left_run,
            span_id="3333333333333333",
            parent_span_id="1111111111111111",
            component_id="aura.runtime.component_b",
        ),
    ]
    right = [
        _observation(
            86, right_run, span_id="abababababababab", component_id="aura.runtime.component_b"
        ),
        _observation(
            87, right_run, span_id="cdcdcdcdcdcdcdcd", component_id="aura.runtime.component_a"
        ),
        _observation(
            88,
            right_run,
            span_id="aaaaaaaaaaaaaaaa",
            component_id="aura.runtime.model_inference",
        ),
        _observation(
            89,
            right_run,
            span_id="bbbbbbbbbbbbbbbb",
            parent_span_id="aaaaaaaaaaaaaaaa",
            component_id="aura.runtime.component_b",
        ),
        _observation(
            90,
            right_run,
            span_id="cccccccccccccccc",
            parent_span_id="aaaaaaaaaaaaaaaa",
            component_id="aura.runtime.component_a",
        ),
    ]

    result = build_run_comparison(left_run, right_run, left + right)

    assert result is not None
    trace = cast(dict[str, Any], result["trace"])
    aligned = cast(list[dict[str, Any]], trace["alignedSpans"])
    assert len(aligned) == 5
    assert all(item["alignment"] == "matched" for item in aligned)


def test_indistinguishable_repeated_siblings_are_incompatible() -> None:
    left_run = "00000000-0000-4000-8000-000000000091"
    right_run = "00000000-0000-4000-8000-000000000092"
    left = [
        _observation(91, left_run, span_id="1111111111111111"),
        _observation(
            92,
            left_run,
            span_id="2222222222222222",
            parent_span_id="1111111111111111",
            component_id="aura.runtime.component_a",
        ),
        _observation(
            93,
            left_run,
            span_id="3333333333333333",
            parent_span_id="1111111111111111",
            component_id="aura.runtime.component_a",
        ),
    ]
    right = [
        _observation(94, right_run, span_id="aaaaaaaaaaaaaaaa"),
        _observation(
            95,
            right_run,
            span_id="bbbbbbbbbbbbbbbb",
            parent_span_id="aaaaaaaaaaaaaaaa",
            component_id="aura.runtime.component_a",
        ),
        _observation(
            96,
            right_run,
            span_id="cccccccccccccccc",
            parent_span_id="aaaaaaaaaaaaaaaa",
            component_id="aura.runtime.component_a",
        ),
    ]

    result = build_run_comparison(left_run, right_run, left + right)

    assert result is not None
    trace = cast(dict[str, Any], result["trace"])
    aligned = cast(list[dict[str, Any]], trace["alignedSpans"])
    assert sum(item["alignment"] == "incompatible" for item in aligned) == 2


def test_five_thousand_span_chain_is_iterative_and_bounded() -> None:
    left_run = "00000000-0000-4000-8000-000000000101"
    right_run = "00000000-0000-4000-8000-000000000102"
    left: list[MetadataObservation] = []
    right: list[MetadataObservation] = []
    for index in range(COMPARISON_MAX_OBSERVATIONS // 2):
        left.append(
            _observation(
                1000 + index,
                left_run,
                span_id=f"{index + 1:016x}",
                parent_span_id=f"{index:016x}" if index else None,
            )
        )
        right.append(
            _observation(
                6000 + index,
                right_run,
                span_id=f"{index + 1:016x}",
                parent_span_id=f"{index:016x}" if index else None,
            )
        )

    result = build_run_comparison(
        left_run,
        right_run,
        left + right,
        deadline=monotonic() + COMPARISON_EXECUTION_TIMEOUT_MS / 1000,
    )

    assert result is not None
    reasons = cast(list[str], result["partialReasons"])
    assert "execution_limit" not in reasons
    assert len(result["trace"]["alignedSpans"]) == COMPARISON_MAX_OBSERVATIONS // 2  # type: ignore[index]


def test_canonical_subtree_signatures_remain_fixed_size_for_deep_chains() -> None:
    signature = b"leaf"
    sizes: list[int] = []
    for _ in range(COMPARISON_MAX_OBSERVATIONS):
        signature = canonical_subtree_digest(
            "aura.runtime.model_inference", "run", [signature]
        )
        sizes.append(len(signature))
    assert set(sizes) == {32}


def test_duration_uses_any_valid_span_milliseconds_and_closed_metric_fallback() -> None:
    left_run = "00000000-0000-4000-8000-000000000111"
    right_run = "00000000-0000-4000-8000-000000000112"
    left_span = _observation(
        111,
        left_run,
        value=12,
        name="queue_wait_ms",
        span_id="1111111111111111",
    )
    right_span = _observation(
        112,
        right_run,
        value=24,
        name="tool_execution_ms",
        span_id="aaaaaaaaaaaaaaaa",
    )
    left_metric = _observation(
        113,
        left_run,
        value=9,
        name="queue_wait_ms",
        kind="metric",
    )
    right_metric = _observation(
        114,
        right_run,
        value=15,
        name="operation_duration_ms",
        kind="metric",
    )
    negative_span = _observation(
        115,
        left_run,
        value=-20,
        name="negative_ms",
        span_id="2222222222222222",
    )
    non_ms_span = _observation(
        116,
        left_run,
        value=1,
        name="span_count",
        span_id="3333333333333333",
    ).model_dump(by_alias=True, exclude_none=True)
    non_ms_span["measurement"]["unit"] = "count"  # type: ignore[index]
    non_ms_observation = MetadataObservation.from_payload(non_ms_span)

    result = build_run_comparison(
        left_run,
        right_run,
        [left_span, right_span, left_metric, right_metric, negative_span, non_ms_observation],
    )

    assert result is not None
    components = cast(list[dict[str, Any]], result["components"])
    left_side = cast(dict[str, Any], components[0]["left"])
    right_side = cast(dict[str, Any], components[0]["right"])
    trace = cast(dict[str, Any], result["trace"])
    aligned = cast(list[dict[str, Any]], trace["alignedSpans"])
    assert left_side["durationMs"] == 12.0  # type: ignore[index]
    assert right_side["durationMs"] == 24.0  # type: ignore[index]
    assert all(
        side["durationMs"] >= 0
        for item in aligned
        for side in (item["left"], item["right"])
        if side is not None
    )
    malformed = [
        item
        for item in aligned
        if item["left"] and item["left"]["spanId"] in {  # type: ignore[index]
            "2222222222222222",
            "3333333333333333",
        }
    ]
    assert malformed and all("retained_partial" in item["leftIntegrity"] for item in malformed)
    metric_result = build_run_comparison(left_run, right_run, [left_metric, right_metric])
    assert metric_result is not None
    assert metric_result["components"][0]["left"]["durationMs"] is None  # type: ignore[index]
    assert metric_result["components"][0]["right"]["durationMs"] == 15.0  # type: ignore[index]


@pytest.mark.asyncio
async def test_distinct_singular_markers_are_unavailable_not_lexically_collapsed() -> None:
    left_run = "00000000-0000-4000-8000-000000000131"
    right_run = "00000000-0000-4000-8000-000000000132"
    marker_cases = (
        ("deployment", "deploymentId", "deploy-a", "deploy-b", "deployment_id"),
        ("deployment", "gitCommit", "abcdef1", "abcdef2", "git_commit"),
        ("deployment", "imageVersion", "image-a", "image-b", "image_version"),
        ("deployment", "configurationVersion", "config-a", "config-b", "configuration_version"),
        ("execution", "modelId", "qwen3:8b", "llama3.2:latest", "model_id"),
        ("execution", "modelVersion", "model-a", "model-b", "model_version"),
        ("execution", "agentRevision", "agent-a", "agent-b", "agent_revision"),
        ("execution", "promptBundleRevision", "prompt-a", "prompt-b", "prompt_bundle_revision"),
        ("execution", "policyRevision", "policy-a", "policy-b", "policy_revision"),
    )

    for index, (source, field, first, second, marker_name) in enumerate(marker_cases):
        def with_marker(
            observation: MetadataObservation,
            value: str,
            *,
            marker_source: str = source,
            marker_field: str = field,
        ) -> MetadataObservation:
            payload = observation.model_dump(by_alias=True, exclude_none=True)
            payload[marker_source][marker_field] = value  # type: ignore[index]
            return MetadataObservation.from_payload(payload)

        left_first = with_marker(_observation(1300 + index * 3, left_run), first)
        left_second = with_marker(_observation(1301 + index * 3, left_run), second)
        right = with_marker(_observation(1302 + index * 3, right_run), first)
        store = MemoryObservatoryStore()
        await store.insert_observation(left_first)
        await store.insert_observation(left_second)
        await store.insert_observation(right)
        result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

        assert result is not None
        markers = {
            item["marker"]: item
            for item in cast(list[dict[str, Any]], result["markerCorrelations"])
        }
        left_marker = cast(dict[str, Any], markers[marker_name]["left"])
        assert left_marker == {"availability": "unavailable", "value": None}
        assert markers[marker_name]["state"] == "unavailable"


@pytest.mark.asyncio
async def test_run_side_interval_is_one_span_boundary_interval() -> None:
    left_run = "00000000-0000-4000-8000-000000000141"
    right_run = "00000000-0000-4000-8000-000000000142"
    first = _observation(141, left_run, value=100, span_id="1111111111111111")
    second = _observation(
        143,
        left_run,
        value=10,
        span_id="2222222222222222",
    )
    right = _observation(142, right_run, value=20, span_id="aaaaaaaaaaaaaaaa")
    result = build_run_comparison(left_run, right_run, [first, second, right])

    assert result is not None
    left = cast(dict[str, Any], result["left"])
    expected_start = (BASE + timedelta(seconds=141) - timedelta(milliseconds=100)).isoformat()
    expected_finish = (BASE + timedelta(seconds=143)).isoformat()
    assert left["startedAt"] == expected_start.replace("+00:00", "Z")
    assert left["finishedAt"] == expected_finish.replace("+00:00", "Z")
    assert left["durationMs"] == 2100.0


@pytest.mark.asyncio
async def test_invalid_span_timing_and_metric_only_runs_have_unavailable_interval() -> None:
    left_run = "00000000-0000-4000-8000-000000000151"
    right_run = "00000000-0000-4000-8000-000000000152"
    invalid = _observation(151, left_run, value=-1, span_id="1111111111111111")
    metric = _observation(152, right_run, value=25, kind="metric", name="run_duration_ms")
    result = build_run_comparison(left_run, right_run, [invalid, metric])

    assert result is not None
    left = cast(dict[str, Any], result["left"])
    right = cast(dict[str, Any], result["right"])
    assert left["startedAt"] is None
    assert left["finishedAt"] is None
    assert left["durationMs"] is None
    assert left["partial"] is True
    assert right["startedAt"] is None
    assert right["finishedAt"] is None
    assert right["durationMs"] is None


def test_expired_python_comparison_budget_returns_truthful_partial_snapshot() -> None:
    left_run = "00000000-0000-4000-8000-000000000116"
    right_run = "00000000-0000-4000-8000-000000000117"
    observations = [_observation(116, left_run), _observation(117, right_run)]

    result = build_run_comparison(
        left_run,
        right_run,
        observations,
        deadline=monotonic() - 1,
    )

    assert result is not None
    assert result["partial"] is True
    assert result["partialReasons"] == ["execution_limit"]
    assert result["scan"]["executionMs"] == COMPARISON_EXECUTION_TIMEOUT_MS  # type: ignore[index]


def _comparison_row(observation: MetadataObservation) -> dict[str, Any]:
    payload = observation.to_payload()
    deployment = cast(dict[str, Any], payload["deployment"])
    measurement = cast(dict[str, Any], payload["measurement"])
    trace = cast(dict[str, Any], payload.get("trace") or {})
    correlation = cast(dict[str, Any], payload.get("correlation") or {})
    execution = cast(dict[str, Any], payload.get("execution") or {})
    component = cast(dict[str, Any], payload["component"])
    return {
        "schema_version": payload["schemaVersion"],
        "observation_id": payload["observationId"],
        "observed_at": payload["observedAt"],
        "kind": payload["kind"],
        "component_id": component["id"],
        "component_version": component["version"],
        "operation": payload["operation"],
        "measurement_name": measurement["name"],
        "measurement_value": measurement["value"],
        "measurement_unit": measurement.get("unit"),
        "dimensions": payload["dimensions"],
        "trace_id": trace.get("traceId"),
        "span_id": trace.get("spanId"),
        "parent_span_id": trace.get("parentSpanId"),
        "linked_span_ids": trace.get("linkedSpanIds"),
        "run_id": correlation.get("runId"),
        "conversation_id": correlation.get("conversationId"),
        "deployment_id": deployment["deploymentId"],
        "git_commit": deployment["gitCommit"],
        "environment": deployment["environment"],
        "host_id": deployment["hostId"],
        "service_role": payload["serviceRole"],
        "capture_policy": payload["capturePolicy"],
        "image_version": deployment.get("imageVersion"),
        "configuration_version": deployment.get("configurationVersion"),
        "model_id": execution.get("modelId"),
        "model_version": execution.get("modelVersion"),
        "agent_revision": execution.get("agentRevision"),
        "prompt_bundle_revision": execution.get("promptBundleRevision"),
        "policy_revision": execution.get("policyRevision"),
    }


class _ComparisonAdapter(ProductionObservatoryStore):
    def __init__(self, rows: list[dict[str, Any]], progress: int | None) -> None:
        super().__init__(
            lambda: None,
            "http://clickhouse.invalid",
            database="observatory",
            user="user",
            password="pass",
        )
        self.rows = rows
        self.progress = progress

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del statement, params
        self._clickhouse_progress_available = True  # pyright: ignore[reportPrivateUsage]
        self._clickhouse_progress_read_rows = self.progress  # pyright: ignore[reportPrivateUsage]
        return self.rows


class _FailedComparisonAdapter(_ComparisonAdapter):
    def __init__(self, status: int, body: str) -> None:
        super().__init__([], None)
        self.status = status
        self.body = body

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del statement, params
        request = httpx.Request("POST", "http://clickhouse.invalid")
        response = httpx.Response(self.status, request=request, content=self.body.encode())
        raise StorageUnavailableError(
            "ClickHouse is unavailable", request=request, response=response
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", [123, COMPARISON_MAX_SCANNED_ROWS, None])
async def test_comparison_progress_is_bounded_and_missing_progress_is_partial(
    progress: int | None,
) -> None:
    left_run = "00000000-0000-4000-8000-000000000121"
    right_run = "00000000-0000-4000-8000-000000000122"
    rows = [
        _comparison_row(_observation(121, left_run)),
        _comparison_row(_observation(122, right_run)),
    ]
    result = await _ComparisonAdapter(rows, progress).run_comparison_query(left_run, right_run)

    assert result is not None
    scan = cast(dict[str, Any], result["scan"])
    assert scan["scannedRows"] == (progress if progress is not None else len(rows))
    reasons = cast(list[str], result["partialReasons"])
    if progress is None or progress >= COMPARISON_MAX_SCANNED_ROWS:
        assert result["partial"] is True
        assert "scan_limit" in reasons
    else:
        assert "scan_limit" not in reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [(408, "Code: 159", "scan_limit"), (504, "timeout", "execution_limit")],
)
async def test_comparison_provider_limits_return_partial_snapshots(
    status: int, body: str, reason: str
) -> None:
    left_run = "00000000-0000-4000-8000-000000000126"
    right_run = "00000000-0000-4000-8000-000000000127"

    result = await _FailedComparisonAdapter(status, body).run_comparison_query(
        left_run, right_run
    )

    assert result["partial"] is True  # type: ignore[index]
    assert result["partialReasons"] == [reason]  # type: ignore[index]


@pytest.mark.asyncio
async def test_comparison_metric_duration_is_recorded_once_from_wrapper_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left_run = "00000000-0000-4000-8000-000000000131"
    right_run = "00000000-0000-4000-8000-000000000132"
    metrics = ObservatoryMetrics()
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(131, left_run))
    await store.insert_observation(_observation(132, right_run))
    ticks = [100.0, 100.0, 100.0, 100.25]
    monkeypatch.setattr(
        "aura_observatory.query.monotonic",
        lambda: ticks.pop(0) if ticks else 100.25,
    )

    await ObservatoryQueryService(store, metrics=metrics).compare_runs(left_run, right_run)

    key = ("observatory_comparison_duration_ms", ())
    assert metrics._histogram_counts[key] == 1  # pyright: ignore[reportPrivateUsage]
    assert metrics.values[key] == 250.0


def test_comparison_bounds_truncate_before_aggregation() -> None:
    left_run = "00000000-0000-4000-8000-000000000081"
    right_run = "00000000-0000-4000-8000-000000000082"
    left = [
        _observation(
            index,
            left_run,
            value=1,
            name="queue_wait_ms",
            kind="metric",
        )
        for index in range(1, 10_002)
    ]
    right = [_observation(10_002, right_run, kind="metric")]

    result = build_run_comparison(left_run, right_run, [*left, *right])

    assert result is not None
    assert result["partial"] is True
    reasons = cast(list[str], result["partialReasons"])
    assert "observation_limit" in reasons
    assert "span_limit" not in reasons
    assert result["components"][0]["left"]["durationMs"] is None  # type: ignore[index]
    assert result["components"][0]["left"]["spanCount"] == 0  # type: ignore[index]


def test_comparison_enforces_global_measurement_budget() -> None:
    left_run = "00000000-0000-4000-8000-000000000083"
    right_run = "00000000-0000-4000-8000-000000000084"
    observations: list[MetadataObservation] = []
    sequence = 20_000
    for component_index in range(16):
        component_id = f"aura.runtime.component_{component_index}"
        for measurement_index in range(128):
            name = f"measurement_{measurement_index}"
            observations.append(
                _observation(
                    sequence,
                    left_run,
                    name=name,
                    kind="metric",
                    component_id=component_id,
                )
            )
            sequence += 1
            observations.append(
                _observation(
                    sequence,
                    right_run,
                    name=name,
                    kind="metric",
                    component_id=component_id,
                )
            )
            sequence += 1

    result = build_run_comparison(left_run, right_run, observations)

    assert result is not None
    assert result["partial"] is True
    reasons = cast(list[str], result["partialReasons"])
    assert "measurement_limit" in reasons
    components = cast(list[dict[str, Any]], result["components"])
    assert sum(len(cast(list[Any], item["measurements"])) for item in components) <= 2_000


def test_otlp_schema_provenance_distinguishes_v1_and_marker_free_v2() -> None:
    def attribute(key: str, value: str) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": value}}

    def payload(schema_url: str | None, *, image: bool = False) -> dict[str, object]:
        resource_attributes = [
            attribute("service.name", "aura-core"),
            attribute("service.role", "worker"),
            attribute("deployment.id", "deploy-a"),
            attribute("deployment.environment", "test"),
            attribute("deployment.git_commit", "abcdef1"),
            attribute("host.id", "host-a"),
            attribute("capture.policy", "metadata_only"),
        ]
        if image:
            resource_attributes.append(attribute("deployment.image_version", "image-a"))
        resource_span: dict[str, object] = {
            "resource": {"attributes": resource_attributes},
            "scopeSpans": [
                {
                    "scope": {"name": "aura.core.telemetry", "version": "1.0"},
                    "spans": [
                        {
                            "traceId": "0123456789abcdef0123456789abcdef",
                            "spanId": "0123456789abcdef",
                            "name": "run",
                            "startTimeUnixNano": "1735689600000000000",
                            "endTimeUnixNano": "1735689600010000000",
                            "attributes": [
                                attribute(
                                    "aura.observation_id",
                                    "00000000-0000-4000-8000-000000000091",
                                ),
                                attribute("aura.component_id", "aura.runtime.model_inference"),
                                attribute("aura.component_version", "1.0"),
                                attribute("aura.operation", "run"),
                                attribute("aura.metric_name", "duration_ms"),
                                attribute("aura.capture_policy", "metadata_only"),
                            ],
                        }
                    ],
                }
            ],
        }
        if schema_url is not None:
            resource_span["schemaUrl"] = schema_url
        return {"resourceSpans": [resource_span]}

    v1 = traces_to_observations(payload(None))[0]
    v2 = traces_to_observations(
        payload("https://aura.local/contracts/telemetry/v2/metadata-observation.schema.json")
    )[0]
    assert v1["schemaVersion"] == 1
    assert v2["schemaVersion"] == 2
    with pytest.raises(OtlpPayloadError):
        traces_to_observations(
            payload(
                "https://aura.local/contracts/telemetry/v1/metadata-observation.schema.json",
                image=True,
            )
        )
    with pytest.raises(OtlpPayloadError):
        traces_to_observations(payload("https://aura.local/contracts/telemetry/unknown.json"))


def test_tagged_model_identifiers_normalize_and_compare_without_private_shapes() -> None:
    def attribute(key: str, value: str) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": value}}

    def payload(model_id: str) -> dict[str, object]:
        resource_attributes = [
            attribute("service.name", "aura-core"),
            attribute("service.role", "worker"),
            attribute("deployment.id", "deploy-a"),
            attribute("deployment.environment", "test"),
            attribute("deployment.git_commit", "abcdef1"),
            attribute("host.id", "host-a"),
            attribute("capture.policy", "metadata_only"),
        ]
        span_attributes = [
            attribute("aura.observation_id", "00000000-0000-4000-8000-000000000092"),
            attribute("aura.component_id", "aura.runtime.model_inference"),
            attribute("aura.component_version", "1.0"),
            attribute("aura.operation", "run"),
            attribute("aura.metric_name", "duration_ms"),
            attribute("aura.capture_policy", "metadata_only"),
            attribute("aura.execution.model_id", model_id),
        ]
        return {
            "resourceSpans": [
                {
                    "schemaUrl": "https://aura.local/contracts/telemetry/v2/metadata-observation.schema.json",
                    "resource": {"attributes": resource_attributes},
                    "scopeSpans": [
                        {
                            "scope": {"name": "aura.core.telemetry", "version": "1.0"},
                            "spans": [
                                {
                                    "traceId": "0123456789abcdef0123456789abcdef",
                                    "spanId": "0123456789abcdef",
                                    "name": "run",
                                    "startTimeUnixNano": "1735689600000000000",
                                    "endTimeUnixNano": "1735689600010000000",
                                    "attributes": span_attributes,
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    for model_id in (
        "qwen3:8b",
        "llama3.2:latest",
        "sha256_abc",
        "contention",
        "privateer",
        "ownerless",
        "digestive",
        "contenthash",
    ):
        normalized = traces_to_observations(payload(model_id))[0]
        assert normalized["execution"] == {"modelId": model_id}
        assert MetadataObservation.from_payload(normalized).execution is not None  # type: ignore[attr-defined]

    forbidden_segments = (
        "owner",
        "identity",
        "credential",
        "bearer",
        "token",
        "secret",
        "password",
        "private",
        "content",
        "hash",
        "digest",
    )
    for segment in forbidden_segments:
        for model_id in (
            segment,
            segment.upper(),
            f"qwen3:{segment}",
            f"qwen3:{segment.upper()}",
            f"qwen3-{segment}:latest",
        ):
            with pytest.raises(OtlpPayloadError):
                traces_to_observations(payload(model_id))

    invalid_revision = payload("qwen3:8b")
    resource = invalid_revision["resourceSpans"][0]["resource"]  # type: ignore[index]
    resource["attributes"].append(attribute("deployment.image_version", "image:tag"))  # type: ignore[index]
    with pytest.raises(OtlpPayloadError):
        traces_to_observations(invalid_revision)

    invalid_execution_revision = payload("qwen3:8b")
    span = invalid_execution_revision["resourceSpans"][0]["scopeSpans"][0]["spans"][0]  # type: ignore[index]
    span["attributes"].append(attribute("aura.execution.model_version", "version:tag"))  # type: ignore[index]
    with pytest.raises(OtlpPayloadError):
        traces_to_observations(invalid_execution_revision)

    for revision in (
        "sha256_abc",
        "contention",
        "privateer",
        "ownerless",
        "digestive",
        "contenthash",
    ):
        valid_revision = payload("qwen3:8b")
        valid_revision["resourceSpans"][0]["resource"]["attributes"].append(  # type: ignore[index]
            attribute("deployment.image_version", revision)
        )
        assert traces_to_observations(valid_revision)
    for segment in (
        "owner",
        "identity",
        "credential",
        "bearer",
        "token",
        "secret",
        "password",
        "private",
        "content",
        "hash",
        "digest",
    ):
        invalid_revision = payload("qwen3:8b")
        invalid_revision["resourceSpans"][0]["resource"]["attributes"].append(  # type: ignore[index]
            attribute("deployment.image_version", f"release-{segment.upper()}-v1")
        )
        with pytest.raises(OtlpPayloadError):
            traces_to_observations(invalid_revision)


def test_direct_contract_marker_grammar_rejects_private_shapes_and_accepts_safe_values() -> None:
    safe = (
        "qwen3:8b",
        "llama3.2:latest",
        "sha256_abc",
        "contention",
        "privateer",
        "ownerless",
        "digestive",
        "contenthash",
    )
    for model_id in safe:
        assert _observation(93, "00000000-0000-4000-8000-000000000093", model_id=model_id)

    forbidden_segments = (
        "owner",
        "identity",
        "credential",
        "bearer",
        "token",
        "secret",
        "password",
        "private",
        "content",
        "hash",
        "digest",
    )
    for segment in forbidden_segments:
        for model_id in (
            segment,
            segment.upper(),
            f"qwen3:{segment}",
            f"qwen3:{segment.upper()}",
            f"qwen3-{segment}:latest",
        ):
            with pytest.raises(ValueError):
                _observation(94, "00000000-0000-4000-8000-000000000094", model_id=model_id)

    for revision in (
        "sha256_abc",
        "contention",
        "privateer",
        "ownerless",
        "digestive",
        "contenthash",
    ):
        payload = _observation(
            95, "00000000-0000-4000-8000-000000000095"
        ).model_dump(by_alias=True, exclude_none=True)
        payload["deployment"]["imageVersion"] = revision  # type: ignore[index]
        assert MetadataObservation.from_payload(payload)
    for segment in forbidden_segments:
        payload = _observation(
            96, "00000000-0000-4000-8000-000000000096"
        ).model_dump(by_alias=True, exclude_none=True)
        payload["deployment"]["imageVersion"] = f"release-{segment.upper()}-v1"  # type: ignore[index]
        with pytest.raises(ValueError):
            MetadataObservation.from_payload(payload)


@pytest.mark.asyncio
async def test_tagged_model_identifiers_are_available_in_comparison_markers() -> None:
    left_run = "00000000-0000-4000-8000-000000000071"
    right_run = "00000000-0000-4000-8000-000000000072"
    store = MemoryObservatoryStore()
    await store.insert_observation(_observation(71, left_run, model_id="qwen3:8b"))
    await store.insert_observation(_observation(72, right_run, model_id="llama3.2:latest"))

    result = await ObservatoryQueryService(store).compare_runs(left_run, right_run)

    assert result is not None
    model_marker = next(
        marker for marker in result["markerCorrelations"] if marker["marker"] == "model_id"  # type: ignore[index]
    )
    assert model_marker["state"] == "changed"
    assert model_marker["left"] == {"availability": "available", "value": "qwen3:8b"}
    assert model_marker["right"] == {
        "availability": "available",
        "value": "llama3.2:latest",
    }


@pytest.mark.asyncio
async def test_comparison_route_is_owner_only_distinct_and_no_store() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    left_run = "00000000-0000-4000-8000-000000000001"
    right_run = "00000000-0000-4000-8000-000000000002"
    await store.insert_observation(_observation(1, left_run))
    await store.insert_observation(_observation(2, right_run, value=3))
    app = create_app(settings, auth=auth, query_store=store, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/run-comparisons/query",
            json={"leftRunId": left_run, "rightRunId": right_run},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "prompt text" not in response.text
        assert "response content" not in response.text
        same = await client.post(
            "/api/v1/run-comparisons/query",
            json={"leftRunId": left_run, "rightRunId": left_run},
        )
        assert same.status_code == 422
