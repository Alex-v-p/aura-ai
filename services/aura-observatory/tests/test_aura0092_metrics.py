"""Production-path metric typing, persistence, and span privacy checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from aura_observatory.adapters.evaluation_orchestration import (
    OtlpComponentSpanExporter,
    PostgresEvaluationMetricStore,
)
from aura_observatory.adapters.postgres import PostgresCatalog
from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.otlp import traces_to_observations


class _Result:
    async def fetchone(self) -> Any:
        return None

    async def fetchall(self) -> list[Any]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> _Result:
        self.calls.append((query, params))
        return _Result()


class _Factory:
    def __init__(self) -> None:
        self.connection = _Connection()

    def __call__(self) -> _Connection:
        return self.connection


class _SpanTransport:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, bytes, str | None, float]] = []
        self.failures = failures

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        self.calls.append((endpoint, payload, credential, timeout))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic collector outage")


class _SeriesResult:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def fetchone(self) -> Any:
        return self._row

    async def fetchall(self) -> list[Any]:
        return self._rows


class _SeriesConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self._results = [
            _SeriesResult(row=(None, 0, 0, {})),
            _SeriesResult(rows=[]),
            _SeriesResult(
                rows=[
                    ("aura.observatory.evaluation_orchestration", 7.5),
                    ("aura.observatory.evaluation_scheduler", 9.0),
                    ("aura.observatory.evaluation_orchestration", "not-a-number"),
                ]
            ),
        ]

    async def __aenter__(self) -> _SeriesConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _query: str, _params: tuple[Any, ...] = ()) -> _SeriesResult:
        self.queries.append(_query)
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_aura0092_metric_scrape_derives_heartbeat_age_from_leases() -> None:
    connection = _SeriesConnection()
    series = await PostgresCatalog(lambda: connection).metric_series()
    ages = {str(item["name"]): item["value"] for item in series}
    assert ages["observatory_evaluation_worker_heartbeat_age_seconds"] == 7.5
    assert ages["observatory_evaluation_scheduler_heartbeat_age_seconds"] == 9.0


@pytest.mark.asyncio
async def test_aura0092_metric_scrape_bounds_each_family_not_global_series() -> None:
    connection = _SeriesConnection()
    await PostgresCatalog(lambda: connection).metric_series()
    query = connection.queries[1]
    assert "ROW_NUMBER() OVER" in query
    assert "PARTITION BY metric_name" in query
    assert "WHERE series_rank <= 64" in query
    assert "LIMIT 64" not in query


def test_aura0092_metric_types_and_metadata_spans_are_bounded() -> None:
    metrics = ObservatoryMetrics()
    metrics.record("observatory_evaluation_terminal_count", labels={"outcome": "failed"})
    metrics.record(
        "observatory_evaluation_dependency_outcome_count",
        labels={"operation": "evaluation_nats", "outcome": "failed"},
    )
    metrics.record("observatory_evaluation_result_lag_seconds", 3.0)
    metrics.record_component_span(
        component_id="aura.observatory.evaluation_orchestration",
        component_version="1.0.0",
        deployment_id="observatory",
        operation="result_apply",
        duration_ms=4.0,
        outcome="success",
        dependency="postgresql",
    )

    rendered = metrics.prometheus()
    assert "# TYPE observatory_evaluation_terminal_count counter" in rendered
    assert "# TYPE observatory_evaluation_dependency_outcome_count counter" in rendered
    assert "# TYPE observatory_evaluation_result_lag_seconds histogram" in rendered
    assert len(metrics.component_spans) == 1
    assert all(
        forbidden not in repr(metrics.component_spans)
        for forbidden in ("prompt", "response", "content", "token", "endpoint")
    )


@pytest.mark.asyncio
async def test_aura0092_component_spans_export_as_metadata_only_otlp_traces() -> None:
    transport = _SpanTransport()
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/observations",
        credential="collector-secret",
        transport=transport,
        max_attempts=1,
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    metrics.record_component_span(
        component_id="aura.observatory.evaluation_orchestration",
        component_version="1.0.0",
        deployment_id="evaluation-worker",
        operation="publish",
        duration_ms=4.0,
        outcome="failure",
        dependency="evaluation_nats",
        error_code="transport_unavailable",
    )
    assert exporter.running
    await metrics.flush()
    assert not exporter.running

    assert len(transport.calls) == 1
    endpoint, payload, credential, timeout = transport.calls[0]
    assert endpoint == "http://collector:4318/v1/traces"
    assert credential == "collector-secret"
    assert timeout == 2.0
    decoded = json.loads(payload)
    span = decoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == metrics.component_spans[0].trace_id
    assert span["spanId"] == metrics.component_spans[0].span_id
    attributes = {item["key"]: item["value"]["stringValue"] for item in span["attributes"]}
    assert attributes["aura.component_id"] == "aura.observatory.evaluation_orchestration"
    assert attributes["aura.metric_name"] == "observatory_evaluation_operation_duration_ms"
    assert attributes["aura.dimension.dependency"] == "evaluation_nats"
    assert attributes["aura.dimension.outcome"] == "failure"
    assert attributes["aura.dimension.error_class"] == "transport_unavailable"
    assert attributes["aura.capture_policy"] == "metadata_only"
    assert decoded["resourceSpans"][0]["resource"]["attributes"][-1]["value"][
        "stringValue"
    ] == "metadata_only"
    assert all(
        forbidden not in payload.decode("utf-8")
        for forbidden in ("prompt", "response", "content", "endpoint", "credential")
    )
    normalized = traces_to_observations(decoded)
    observation = MetadataObservation.from_payload(normalized[0])
    assert observation.kind == "span"
    assert observation.observation_id == attributes["aura.observation_id"]
    assert observation.component.id == "aura.observatory.evaluation_orchestration"
    assert observation.deployment.deployment_id == "local"
    assert observation.dimensions["dependency"] == "evaluation_nats"
    assert observation.dimensions["errorClass"] == "transport_unavailable"
    assert observation.capture_policy == "metadata_only"


@pytest.mark.asyncio
async def test_aura0092_component_span_export_retries_and_bounds_failure() -> None:
    transport = _SpanTransport(failures=1)
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318",
        transport=transport,
        max_attempts=2,
        queue_capacity=1,
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    metrics.record_component_span(
        component_id="aura.observatory.evaluation_scheduler",
        component_version="1.0.0",
        deployment_id="scheduler",
        operation="heartbeat",
        duration_ms=2.0,
        outcome="success",
        dependency="postgresql",
    )
    await metrics.flush()

    assert len(transport.calls) == 2
    assert exporter.stats == {"accepted": 1, "failures": 1, "dropped": 0, "retries": 1}


def test_aura0092_bounded_scrape_keeps_new_and_legacy_metric_families() -> None:
    series: list[dict[str, object]] = [
        {
            "name": "observatory_query_outcome",
            "value": 1,
            "labels": {"resource": "overview", "outcome": "success"},
        },
        {
            "name": "observatory_evaluation_terminal_count",
            "value": 1,
            "labels": {"outcome": "cancelled"},
        },
        {
            "name": "observatory_evaluation_deadline_count",
            "value": 1,
            "labels": {"outcome": "deadline"},
        },
        {
            "name": "observatory_evaluation_dependency_outcome_count",
            "value": 1,
            "labels": {"operation": "postgresql", "outcome": "success"},
        },
        {
            "name": "observatory_evaluation_outbox_publication_count",
            "value": 1,
            "labels": {"outcome": "failed"},
        },
        {
            "name": "observatory_evaluation_operation_duration_ms",
            "value": 4,
            "count": 1,
            "buckets": {"5": 1, "10": 1},
            "labels": {"operation": "result_apply"},
        },
    ]
    rendered = ObservatoryMetrics.prometheus_for_series(
        cast(list[Mapping[str, object]], series)
    )
    for name, metric_type in (
        ("observatory_query_outcome", "counter"),
        ("observatory_evaluation_terminal_count", "counter"),
        ("observatory_evaluation_deadline_count", "counter"),
        ("observatory_evaluation_dependency_outcome_count", "counter"),
        ("observatory_evaluation_outbox_publication_count", "counter"),
        ("observatory_evaluation_operation_duration_ms", "histogram"),
    ):
        assert f"# TYPE {name} {metric_type}" in rendered
    assert (
        'observatory_evaluation_operation_duration_ms_count{operation="result_apply"} 1'
        in rendered
    )
    assert (
        'observatory_evaluation_operation_duration_ms_bucket{operation="result_apply",le="5"} 1'
        in rendered
    )


@pytest.mark.asyncio
async def test_aura0092_metric_store_preserves_actual_name_and_type() -> None:
    factory = _Factory()
    store = PostgresEvaluationMetricStore(factory)
    await store.record_metric(
        "observatory_evaluation_dependency_outcome_count",
        1.0,
        {"operation": "evaluation_nats", "outcome": "published"},
    )
    await store.record_metric("observatory_evaluation_result_lag_seconds", 4.0, {}, histogram=True)
    await store.set_metric("observatory_evaluation_worker_heartbeat_age_seconds", 2.0, {})

    inserts = [params for query, params in factory.connection.calls if "INSERT INTO" in query]
    insert_queries = [
        query for query, _params in factory.connection.calls if "INSERT INTO" in query
    ]
    assert [params[0] for params in inserts] == [
        "observatory_evaluation_dependency_outcome_count",
        "observatory_evaluation_result_lag_seconds",
        "observatory_evaluation_worker_heartbeat_age_seconds",
    ]
    assert len(inserts[0]) == 9
    assert len(inserts[1]) == 9
    assert insert_queries[0].count("%s") == 9
    assert insert_queries[1].count("%s") == 9
    assert inserts[1][7] == 1


@pytest.mark.asyncio
async def test_aura0092_metric_store_accepts_long_allowlisted_operation_label() -> None:
    factory = _Factory()
    store = PostgresEvaluationMetricStore(factory)

    await store.record_metric(
        "observatory_evaluation_operation_duration_ms",
        1.0,
        {"operation": "schedule_materialize"},
        histogram=True,
    )

    inserts = [params for query, params in factory.connection.calls if "INSERT INTO" in query]
    assert inserts[0][1] == "schedule_materialize"


@pytest.mark.asyncio
async def test_aura0092_persistence_failures_are_rate_limited_and_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingPersistence:
        async def record_metric(
            self,
            _name: str,
            _value: float,
            _labels: dict[str, str],
            *,
            histogram: bool,
        ) -> None:
            del histogram
            raise RuntimeError("nats://private-endpoint/password")

    metrics = ObservatoryMetrics(persistence=FailingPersistence())
    for _ in range(3):
        metrics.record("observatory_evaluation_terminal_count", labels={"outcome": "failed"})
    await metrics.flush()
    assert metrics.persistence_failures == 3
    assert metrics.persistence_diagnostics == ("failed:database_failure",) * 3
    assert caplog.text.count("observatory metric persistence failed:database_failure") == 1
    assert "private-endpoint" not in caplog.text
    assert "password" not in caplog.text
