"""AURA-0139 aggregate workflow monitoring backend invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import ProductionObservatoryStore, StorageUnavailableError
from aura_observatory.metrics import COMPONENT_SPAN_COMPONENTS, ObservatoryMetrics
from aura_observatory.query import (
    ANALYTICS_FOCUSES,
    ANALYTICS_MAX_POINTS,
    ANALYTICS_MAX_SERIES,
    ANALYTICS_METRICS,
    ANALYTICS_UNITS,
    WORKFLOW_COMPONENTS,
    WORKFLOW_CORRELATION_COMPONENT,
    WORKFLOW_CORRELATION_OPERATION,
    WORKFLOW_ERROR_CLASSES,
    WORKFLOW_LATENCY_METRICS,
    WORKFLOW_MEASUREMENT_BINDINGS,
    WORKFLOW_METRICS,
    WORKFLOW_OPERATIONS,
    WORKFLOW_OUTCOME_METRICS,
    ObservatoryQueryService,
    analytics_metric_catalogue,
    build_workflow_monitoring_snapshot,
    is_workflow_monitoring_self_event,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot_items(snapshot: dict[str, object], key: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], snapshot[key])


def _row(
    number: int,
    name: str,
    unit: str,
    value: float,
    *,
    at: datetime = BASE,
    outcome: str = "succeeded",
    run_id: str = "",
    trace_id: str = "",
    component: str | None = None,
    operation: str | None = None,
    error_class: str = "authorization",
) -> dict[str, object]:
    default_component, default_operations = WORKFLOW_MEASUREMENT_BINDINGS.get(
        name,
        ("aura.automation.workflow_state", frozenset({"workflow.transition"})),
    )
    component = component or default_component
    operation = operation or sorted(default_operations)[0]
    return {
        "observation_id": f"00000000-0000-4000-8000-{number:012d}",
        "observed_at": at,
        "component_id": component,
        "operation": operation,
        "measurement_name": name,
        "measurement_unit": unit,
        "measurement_value": value,
        "dimensions": {"outcome": outcome, "errorClass": error_class},
        "run_id": run_id,
        "trace_id": trace_id,
    }


def test_workflow_catalogue_is_exact_and_automation_focused() -> None:
    assert len(WORKFLOW_METRICS) == 15
    assert set(WORKFLOW_MEASUREMENT_BINDINGS) == set(WORKFLOW_METRICS)
    assert set(WORKFLOW_METRICS).issubset(ANALYTICS_METRICS)
    assert not any("scheduler" in metric for metric in WORKFLOW_METRICS)
    catalogue = analytics_metric_catalogue(focus="automation", now=BASE)
    items = {
        str(item["metric"]): item
        for item in cast(list[dict[str, object]], catalogue["items"])
    }
    assert all(
        items[metric]["focusAreas"] == ["automation", "all"] for metric in WORKFLOW_METRICS
    )
    assert all(items[metric]["unit"] == "count" for metric in WORKFLOW_OUTCOME_METRICS)
    assert all(items[metric]["unit"] == "milliseconds" for metric in WORKFLOW_LATENCY_METRICS)
    assert set(ANALYTICS_UNITS) >= {"count", "milliseconds"}
    assert set(ANALYTICS_FOCUSES) >= {"automation"}


def test_snapshot_uses_utc_buckets_and_exposes_only_bounded_correlations() -> None:
    rows = [
        _row(1, "workflow_transition_outcome", "count", 1, outcome="failed"),
        _row(
            2,
            "workflow_agent_run_step_duration_ms",
            "milliseconds",
            40,
            at=BASE + timedelta(minutes=2),
            run_id="00000000-0000-4000-8000-000000000001",
            trace_id="0123456789abcdef0123456789abcdef",
            component=WORKFLOW_CORRELATION_COMPONENT,
            operation=WORKFLOW_CORRELATION_OPERATION,
        ),
        _row(
            3,
            "workflow_wait_lag_ms",
            "milliseconds",
            12,
            at=BASE + timedelta(minutes=4),
            outcome="failed",
        ),
        # An unallowlisted scheduler measurement must not reach any output.
        _row(
            4,
            "workflow_scheduler_scan_duration_ms",
            "milliseconds",
            99,
            at=BASE + timedelta(minutes=5),
        ),
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert snapshot["capturePolicy"] == "metadata_only"
    assert snapshot["window"]["from"] == "2026-01-01T00:00:00Z"  # type: ignore[index]
    assert snapshot["lifecycleCounts"] == [  # type: ignore[index]
        {
            "metric": "workflow_transition_outcome",
            "operation": "workflow.transition",
            "outcome": "failed",
            "count": 1,
            "partial": False,
        }
    ]
    assert snapshot["recentCorrelations"][0]["runId"] == "00000000-0000-4000-8000-000000000001"  # type: ignore[index]
    assert snapshot["limits"]["returnedRecentCorrelations"] == 1  # type: ignore[index]
    assert snapshot["recentCorrelations"][0]["component"] == WORKFLOW_CORRELATION_COMPONENT  # type: ignore[index]
    assert snapshot["recentCorrelations"][0]["operation"] == WORKFLOW_CORRELATION_OPERATION  # type: ignore[index]
    assert all(
        forbidden not in snapshot
        for forbidden in ("workflowId", "instanceId", "stepExecutionId", "content", "ownerId")
    )


def test_snapshot_includes_count_series_before_latency_and_never_infers_rate() -> None:
    rows = [
        _row(4, "workflow_transition_outcome", "count", 1, outcome="succeeded"),
        _row(5, "workflow_command_outcome", "count", 2, outcome="failed"),
        _row(6, "workflow_repository_outcome", "count", 1, outcome="succeeded"),
        *[
            _row(7 + index, metric, "milliseconds", 20)
            for index, metric in enumerate(WORKFLOW_LATENCY_METRICS)
        ],
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    series = _snapshot_items(snapshot, "series")
    assert [item["metric"] for item in series[:3]] == list(WORKFLOW_OUTCOME_METRICS)
    assert series[0]["unit"] == "count"
    assert series[1]["points"][0]["value"] == 2  # type: ignore[index]
    assert all(
        item["summary"]["ratePerInterval"] is None  # type: ignore[index]
        for item in series
    )
    # The established twelve-series cap is retained.  Outcome series have
    # deterministic priority and omitted latency signals make partialness clear.
    assert snapshot["partial"] is True
    assert "series_limit" in snapshot["partialReasons"]  # type: ignore[operator]


def test_snapshot_accepts_production_workflow_operations_and_error_classes() -> None:
    rows: list[dict[str, object]] = []
    operations = (
        "workflow.transition",
        "workflow.command",
        "workflow.repository",
    )
    for index, (metric, operation) in enumerate(
        zip(WORKFLOW_OUTCOME_METRICS, operations, strict=True), 1
    ):
        rows.append(
            _row(
                index,
                metric,
                "count",
                index,
                outcome="failed",
                operation=operation,
                error_class=("authorization", "database", "persistence")[index - 1],
            )
        )
    for index, error_class in enumerate(sorted(WORKFLOW_ERROR_CLASSES - {"unknown"}), 10):
        rows.append(
            _row(
                index,
                "workflow_transition_outcome",
                "count",
                1,
                outcome="failed",
                operation="workflow.transition",
                error_class=error_class,
            )
        )
    # Every bounded step/lifecycle operation must survive failed-step
    # classification, not only the three lifecycle outcome operations.
    failed_step_probes = (
        (
            "workflow_definition_validation_duration_ms",
            "aura.automation.workflow_definition",
            "validate",
            "validation",
        ),
        (
            "workflow_agent_run_step_duration_ms",
            WORKFLOW_CORRELATION_COMPONENT,
            "execute",
            "timeout",
        ),
        (
            "workflow_approval_command_duration_ms",
            "aura.automation.workflow_approval",
            "execute",
            "conflict",
        ),
        (
            "workflow_notification_step_duration_ms",
            "aura.automation.workflow_step.notification",
            "execute",
            "authorization",
        ),
        (
            "workflow_wait_command_duration_ms",
            "aura.automation.workflow_step.wait_until",
            "execute",
            "database",
        ),
        (
            "workflow_wait_command_duration_ms",
            "aura.automation.workflow_step.wait_until",
            "resume",
            "timeout",
        ),
        (
            "workflow_wait_lag_ms",
            "aura.automation.workflow_step.wait_until",
            "resume",
            "timeout",
        ),
        (
            "workflow_checkpoint_persistence_duration_ms",
            "aura.automation.workflow_checkpoint",
            "persist",
            "persistence",
        ),
        (
            "workflow_cancellation_duration_ms",
            "aura.automation.workflow_cancellation",
            "cancel",
            "internal",
        ),
        (
            "workflow_settlement_duration_ms",
            "aura.automation.workflow_settlement",
            "settle",
            "execution",
        ),
    )
    for index, (metric, component, operation, error_class) in enumerate(failed_step_probes, 40):
        rows.append(
            _row(
                index,
                metric,
                "milliseconds",
                5,
                outcome="failed",
                component=component,
                operation=operation,
                error_class=error_class,
            )
        )
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    lifecycle = _snapshot_items(snapshot, "lifecycleCounts")
    assert {item["operation"] for item in lifecycle} == set(operations)
    failures = _snapshot_items(snapshot, "failureBreakdowns")
    assert {item["errorClass"] for item in failures} == set(WORKFLOW_ERROR_CLASSES - {"unknown"})
    assert {
        (item["component"], item["operation"])
        for item in failures
        if item["operation"] in {"execute", "validate", "persist", "cancel", "settle", "resume"}
    } >= {
        (component, operation) for _, component, operation, _ in failed_step_probes
    }


def test_cross_paired_workflow_rows_cannot_poison_aggregates() -> None:
    rows = [
        _row(
            900,
            "workflow_transition_outcome",
            "count",
            1,
            outcome="failed",
            component="aura.automation.workflow_state",
            operation="execute",
            error_class="timeout",
        ),
        _row(
            901,
            "workflow_wait_lag_ms",
            "milliseconds",
            99,
            component="aura.automation.workflow_step.wait_until",
            operation="execute",
        ),
        _row(
            902,
            "workflow_agent_run_step_duration_ms",
            "milliseconds",
            99,
            component="aura.automation.workflow_definition",
            operation="execute",
            run_id="00000000-0000-4000-8000-000000000099",
            trace_id="0123456789abcdef0123456789abcdef",
        ),
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert snapshot["lifecycleCounts"] == []
    assert snapshot["latencySummaries"] == []
    assert snapshot["failureBreakdowns"] == []
    assert snapshot["recentCorrelations"] == []


def test_wait_command_resume_duration_is_bound_but_other_pairs_are_not() -> None:
    rows = [
        _row(
            910,
            "workflow_wait_command_duration_ms",
            "milliseconds",
            7,
            component="aura.automation.workflow_step.wait_until",
            operation="resume",
        ),
        _row(
            911,
            "workflow_wait_command_duration_ms",
            "milliseconds",
            99,
            component="aura.automation.workflow_step.wait_until",
            operation="settle",
        ),
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    summaries = _snapshot_items(snapshot, "latencySummaries")
    wait_summary = next(
        item for item in summaries if item["metric"] == "workflow_wait_command_duration_ms"
    )
    assert wait_summary["summary"]["sampleCount"] == 1  # type: ignore[index]
    assert wait_summary["summary"]["average"] == 7  # type: ignore[index]


@pytest.mark.asyncio
async def test_workflow_monitoring_self_invalidations_are_filtered_without_time_window() -> None:
    self_event = {
        "id": "query-span",
        "type": "observation.changed",
        "data": {
            "observationId": "query-span",
            "componentId": "aura.observatory.query_api",
            "operation": "workflow_monitoring",
        },
        "_persisted_replay": True,
    }
    delayed_self_event = {
        **self_event,
        "id": "query-span-delayed",
        "data": {
            **cast(dict[str, object], self_event["data"]),
            "observationId": "query-span-delayed",
        },
    }
    genuine_core_event = {
        "id": "core-workflow",
        "type": "observation.changed",
        "data": {
            "observationId": "core-workflow",
            "componentId": "aura.automation.workflow_state",
            "operation": "workflow.transition",
        },
        "_persisted_replay": True,
    }
    assert is_workflow_monitoring_self_event(self_event)
    assert is_workflow_monitoring_self_event(delayed_self_event)
    assert not is_workflow_monitoring_self_event(genuine_core_event)
    assert not is_workflow_monitoring_self_event(
        {
            **self_event,
            "data": {
                **cast(dict[str, object], self_event["data"]),
                "operation": "query",
            },
        }
    )

    class _EventsPort:
        def events_after_query(
            self, _last_event_id: str | None, _snapshot_sequence: int | None = None
        ) -> Any:
            async def stream() -> Any:
                yield self_event
                yield delayed_self_event
                yield genuine_core_event

            return stream()

    service = ObservatoryQueryService(
        MemoryObservatoryStore(), query_port=cast(Any, _EventsPort())
    )
    events = [event async for event in service.events_after(None, snapshot_sequence=0)]
    assert [event["id"] for event in events] == ["core-workflow"]


@pytest.mark.asyncio
async def test_clickhouse_self_only_batch_advances_cursor_before_later_core_event() -> None:
    class _TailStore(ProductionObservatoryStore):
        def __init__(self) -> None:
            super().__init__(
                lambda: None,
                "http://clickhouse.invalid",
                database="observatory",
                user="user",
                password="password",
            )
            self.query_count = 0
            self.batches: list[list[dict[str, object]]] = [
                [
                    {
                        "observation_id": "query-span",
                        "run_id": "",
                        "trace_id": "",
                        "component_id": "aura.observatory.query_api",
                        "operation": "workflow_monitoring",
                        "ingest_sequence": 1,
                    }
                ],
                [
                    {
                        "observation_id": "core-workflow",
                        "run_id": "run-opaque",
                        "trace_id": "trace-opaque",
                        "component_id": "aura.automation.workflow_state",
                        "operation": "workflow.transition",
                        "ingest_sequence": 2,
                    }
                ],
            ]

        async def _overview_counts(self) -> dict[str, object]:
            return {"latest_sequence": 2}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            del params
            if "ingest_sequence >" not in statement:
                return []
            self.query_count += 1
            return self.batches.pop(0) if self.batches else []

    store = _TailStore()
    stream = store.events_after_query(None, snapshot_sequence=0)
    event = await anext(stream)
    assert event["id"] == "core-workflow"
    assert store.query_count == 2


def test_snapshot_is_genuinely_empty_without_observations_and_vocab_is_closed() -> None:
    empty = build_workflow_monitoring_snapshot(
        [],
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert empty["latencySummaries"] == []
    assert empty["series"] == []
    rows = [
        {
            **_row(8, "workflow_transition_outcome", "count", 1, outcome="failed"),
            "component_id": "aura.automation.tenant_owned",
            "operation": "tenant.secret_operation",
            "dimensions": {"outcome": "failed", "errorClass": "tenant.exception.text"},
        }
    ]
    bounded = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert bounded["lifecycleCounts"] == []
    assert bounded["failureBreakdowns"] == []
    assert bounded["recentCorrelations"] == []


def test_query_api_component_span_vocabulary_is_finite() -> None:
    assert "aura.observatory.query_api" in COMPONENT_SPAN_COMPONENTS
    metrics = ObservatoryMetrics()
    metrics.record_component_span(
        component_id="aura.observatory.query_api",
        component_version="1.0.0",
        deployment_id="observatory-api",
        operation="query",
        duration_ms=3,
        outcome="success",
        dependency="clickhouse",
    )
    assert metrics.component_spans[-1].dependency == "clickhouse"
    assert WORKFLOW_COMPONENTS
    assert WORKFLOW_OPERATIONS == {
        "workflow.transition",
        "workflow.command",
        "workflow.repository",
        "execute",
        "validate",
        "persist",
        "cancel",
        "settle",
        "resume",
    }
    assert WORKFLOW_CORRELATION_COMPONENT == "aura.automation.workflow_step.agent_run"
    assert WORKFLOW_CORRELATION_OPERATION == "execute"
    assert WORKFLOW_ERROR_CLASSES == {
        "unknown",
        "authorization",
        "database",
        "database_programming",
        "idempotency",
        "internal",
        "not_found",
        "persistence",
        "serialization",
        "execution",
        "budget",
        "capability",
        "provider_protocol",
        "provider",
        "timeout",
        "validation",
        "conflict",
    }


@pytest.mark.asyncio
async def test_clickhouse_boundary_emits_bounded_query_storage_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ObservatoryMetrics()

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                text='{"ok": 1}\n',
                request=httpx.Request("POST", "http://clickhouse.invalid"),
            )

    class _ProbeStore(ProductionObservatoryStore):
        async def probe(self) -> list[dict[str, Any]]:
            return await self._query_rows("SELECT 1")

        async def probe_storage(self) -> list[dict[str, Any]]:
            return await self._rows("SELECT 1")

    def client_factory(**_kwargs: object) -> _Client:
        return _Client()

    monkeypatch.setattr(httpx, "AsyncClient", cast(Any, client_factory))
    probe_store = _ProbeStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="password",
        metrics=metrics,
    )
    assert await probe_store.probe() == [{"ok": 1}]
    assert await probe_store.probe_storage() == [{"ok": 1}]
    assert metrics.component_spans[-1].component_id == "aura.observatory.query_api"
    assert metrics.component_spans[-1].dependency == "clickhouse"
    assert metrics.component_spans[-1].outcome == "success"

    class _MonitoringClient(_Client):
        async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                text="",
                request=httpx.Request("POST", "http://clickhouse.invalid"),
            )

    def monitoring_client_factory(**_kwargs: object) -> _MonitoringClient:
        return _MonitoringClient()

    monkeypatch.setattr(httpx, "AsyncClient", cast(Any, monitoring_client_factory))
    monitoring = await probe_store.workflow_monitoring_query(
        BASE,
        BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
        series_limit=12,
        point_limit=200,
    )
    assert monitoring["capturePolicy"] == "metadata_only"
    assert metrics.component_spans[-1].operation == "workflow_monitoring"

    class _UnavailableClient(_Client):
        async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                503,
                text="provider unavailable",
                request=httpx.Request("POST", "http://clickhouse.invalid"),
            )

    def unavailable_client_factory(**_kwargs: object) -> _UnavailableClient:
        return _UnavailableClient()

    monkeypatch.setattr(httpx, "AsyncClient", cast(Any, unavailable_client_factory))
    with pytest.raises(StorageUnavailableError):
        await probe_store.probe()
    assert metrics.component_spans[-1].outcome == "unavailable"
    assert metrics.component_spans[-1].error_code == "database_unavailable"


def test_snapshot_keeps_metric_units_distinct_and_uses_half_open_utc_buckets() -> None:
    rows = [
        _row(10, "workflow_transition_outcome", "count", 1, at=BASE),
        _row(11, "workflow_transition_duration_ms", "milliseconds", 10, at=BASE),
        _row(
            12,
            "workflow_transition_duration_ms",
            "milliseconds",
            30,
            at=BASE + timedelta(hours=1),
        ),
        # A count value with a duration unit must be rejected rather than
        # silently mixing incompatible measurements.
        _row(13, "workflow_command_outcome", "milliseconds", 1, at=BASE),
        # Negative and non-finite latency values are not valid observations.
        _row(14, "workflow_wait_lag_ms", "milliseconds", -1, at=BASE),
        _row(15, "workflow_wait_lag_ms", "milliseconds", float("nan"), at=BASE),
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=2),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    series = next(
        item
        for item in _snapshot_items(snapshot, "series")
        if item["metric"] == "workflow_transition_duration_ms"
    )
    points = cast(list[dict[str, Any]], series["points"])
    assert [point["startAt"] for point in points] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
    ]
    assert [point["value"] for point in points] == [10.0, 30.0]
    wait_summary = next(
        item
        for item in _snapshot_items(snapshot, "latencySummaries")
        if item["metric"] == "workflow_wait_lag_ms"
    )
    assert cast(dict[str, Any], wait_summary["summary"])["sampleCount"] == 0


def test_snapshot_marks_late_sparse_and_bounded_data_explicitly() -> None:
    rows = [
        _row(20, "workflow_wait_lag_ms", "milliseconds", 40, at=BASE + timedelta(minutes=20)),
        _row(21, "workflow_wait_lag_ms", "milliseconds", 80, at=BASE + timedelta(minutes=10)),
    ]
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
        scanned_rows=1_000_000,
        truncated_by_storage_limit=True,
        truncated_by_execution_limit=True,
        retention_boundary=True,
    )
    assert snapshot["partial"] is True
    assert set(snapshot["partialReasons"]) >= {  # type: ignore[arg-type]
        "late_data",
        "storage_limit",
        "execution_limit",
        "retention_boundary",
    }
    assert snapshot["limits"]["truncatedByStorageLimit"] is True  # type: ignore[index]
    assert snapshot["limits"]["truncatedByExecutionLimit"] is True  # type: ignore[index]
    assert snapshot["freshness"]["state"] == "stale"  # type: ignore[index]


def test_snapshot_caps_correlations_deterministically_and_rejects_unbounded_metadata() -> None:
    rows: list[dict[str, object]] = []
    for number in range(26):
        rows.append(
            _row(
                100 + number,
                "workflow_agent_run_step_duration_ms",
                "milliseconds",
                number + 1,
                at=BASE + timedelta(minutes=number),
                run_id=f"00000000-0000-4000-8000-{number + 1:012d}",
                trace_id=f"{number + 1:032x}",
                component=WORKFLOW_CORRELATION_COMPONENT,
                operation=WORKFLOW_CORRELATION_OPERATION,
            )
        )
    # These rows are otherwise valid but must not become correlations when
    # their IDs or finite grouping labels violate the public vocabulary.
    rows.extend(
        [
            _row(
                200,
                "workflow_agent_run_step_duration_ms",
                "milliseconds",
                1,
                at=BASE + timedelta(minutes=30),
                run_id="not-a-uuid",
                trace_id="not-a-trace",
                component=WORKFLOW_CORRELATION_COMPONENT,
                operation=WORKFLOW_CORRELATION_OPERATION,
            ),
            {
                **_row(
                    201,
                    "workflow_transition_outcome",
                    "count",
                    1,
                    at=BASE + timedelta(minutes=31),
                    outcome="failed",
                ),
                "component_id": "aura.core.workflow;DROP TABLE observations",
                "operation": "workflow.advance\nownerId=secret",
                "dimensions": {
                    "outcome": "failed",
                    "errorClass": "exception message and definition content",
                    "ownerId": "owner-secret",
                    "input": "workflow-input",
                },
            },
        ]
    )
    snapshot = build_workflow_monitoring_snapshot(
        rows,
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    correlations = _snapshot_items(snapshot, "recentCorrelations")
    assert len(correlations) == 25
    assert correlations[0]["runId"].endswith("000000000026")
    assert snapshot["partialReasons"] == ["correlation_limit"]
    assert all("ownerId" not in item and "input" not in item for item in correlations)
    assert all(
        "DROP" not in str(item.get("component", ""))
        for item in _snapshot_items(snapshot, "failureBreakdowns")
    )


def test_snapshot_reports_unavailable_signals_for_empty_and_stale_windows() -> None:
    empty = build_workflow_monitoring_snapshot(
        [],
        lower=BASE,
        upper=BASE + timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert empty["freshness"]["state"] == "unavailable"  # type: ignore[index]
    assert set(empty["unavailableSignals"]) == {  # type: ignore[arg-type]
        "lifecycle_counts",
        "latency_summaries",
        "time_series",
        "failure_breakdowns",
        "run_trace_correlation",
    }
    now = datetime.now(UTC)
    stale = build_workflow_monitoring_snapshot(
        [_row(300, "workflow_wait_lag_ms", "milliseconds", 4, at=now - timedelta(hours=2))],
        lower=now - timedelta(hours=3),
        upper=now - timedelta(hours=1),
        bucket="one_hour",
        bucket_seconds=3600,
    )
    assert stale["freshness"]["state"] == "stale"  # type: ignore[index]


@pytest.mark.asyncio
async def test_service_rejects_unbounded_workflow_monitoring_requests() -> None:
    service = ObservatoryQueryService(MemoryObservatoryStore())
    with pytest.raises(ValueError):
        await service.workflow_monitoring(series_limit=ANALYTICS_MAX_SERIES + 1)
    with pytest.raises(ValueError):
        await service.workflow_monitoring(point_limit=ANALYTICS_MAX_POINTS + 1)
    with pytest.raises(ValueError):
        await service.workflow_monitoring(bucket="one_hour", from_time=BASE, to_time=BASE)
