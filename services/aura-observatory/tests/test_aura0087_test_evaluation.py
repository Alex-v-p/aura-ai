"""Independent AURA-0087 analytics semantics, bounds, and failure coverage."""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import psycopg
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import (
    _ANALYTICS_OUTCOME_SQL,  # pyright: ignore[reportPrivateUsage]
    ProductionObservatoryStore,
    StorageUnavailableError,
)
from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.query import (
    AnalyticsRequest,
    ObservatoryQueryService,
    _analytics_percentile,  # pyright: ignore[reportPrivateUsage]
    _normalize_outcome,  # pyright: ignore[reportPrivateUsage]
    analytics_unavailable,
    bounded_analytics_window,
    choose_analytics_bucket,
)


def _observation(
    index: int,
    at: datetime,
    *,
    value: float = 1.0,
    name: str = "duration_ms",
    unit: str = "milliseconds",
    component: str = "aura.test.analytics",
    dimensions: dict[str, str] | None = None,
) -> MetadataObservation:
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "observationId": f"00000000-0000-0000-0000-{index:012d}",
        "observedAt": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "kind": "metric",
        "component": {"id": component, "version": "1.0.0"},
        "operation": "observe",
        "measurement": {"name": name, "value": value, "unit": unit},
        "dimensions": dimensions or {"provider": "local", "outcome": "success"},
        "correlation": {"runId": "00000000-0000-0000-0000-000000000001"},
        "deployment": {
            "deploymentId": "dev",
            "gitCommit": "abcdef1",
            "environment": "test",
            "hostId": "host",
        },
        "serviceRole": "api",
        "capturePolicy": "metadata_only",
    }
    return MetadataObservation.from_payload(payload)


def test_full_stack_clickhouse_probe_helper_is_outer_scoped() -> None:
    runner = (
        Path(__file__).resolve().parents[3]
        / "deploy/compose/run-observatory-full-stack-e2e.sh"
    ).read_text(encoding="utf-8")
    helper = "def unavailable_postgres()"
    gate = "class ScanBoundaryStore"
    assert runner.count(helper) == 1
    assert runner.index(helper) < runner.index(gate)
    assert "unavailable_postgres," in runner[runner.index(gate) :]
    for label in (
        "ClickHouse quantile adapter preflight failed status=",
        "ClickHouse scan-boundary adapter preflight failed status=",
        "ClickHouse overview/run adapter preflight failed status=",
    ):
        assert label in runner


def test_full_stack_provider_gate_checks_quantiles_before_scan_fixture() -> None:
    runner = (
        Path(__file__).resolve().parents[3]
        / "deploy/compose/run-observatory-full-stack-e2e.sh"
    ).read_text(encoding="utf-8")
    boundary_fixture = runner.index("boundary_insert = (")
    quantile_success = runner.index(
        "ProductionObservatoryStore quantileExact parity passed:"
    )
    scan_failure = runner.index(
        "ClickHouse scan-boundary adapter preflight failed status="
    )
    assert quantile_success < boundary_fixture < scan_failure
    assert '"measurement_name": "duration_ms"' in runner


@pytest.mark.asyncio
async def test_analytics_uses_utc_bucket_boundaries_for_late_out_of_order_data() -> None:
    from zoneinfo import ZoneInfo

    local = ZoneInfo("Europe/Brussels")
    local_from = datetime(2026, 3, 29, 1, 0, tzinfo=local)
    local_to = datetime(2026, 3, 29, 6, 0, tzinfo=local)
    lower, upper = bounded_analytics_window(local_from, local_to)
    assert lower.tzinfo is UTC
    assert upper.tzinfo is UTC
    assert (upper - lower) == timedelta(hours=4)
    assert choose_analytics_bucket("one_hour", lower, upper) == ("one_hour", 3600)

    # Insert in delivery order rather than event-time order.  The snapshot is
    # required to order points by their UTC bucket, including the DST jump.
    observations = [
        _observation(3, datetime(2026, 3, 29, 3, 1, tzinfo=UTC), value=30),
        _observation(1, datetime(2026, 3, 29, 0, 59, tzinfo=UTC), value=10),
        _observation(2, datetime(2026, 3, 29, 1, 1, tzinfo=UTC), value=20),
    ]

    class Store:
        async def list_observations(
            self, *, limit: int | None = None
        ) -> list[MetadataObservation]:
            return observations if limit is None else observations[:limit]

    result = await ObservatoryQueryService(cast(Any, Store())).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            aggregation="p95",
            from_time=local_from,
            to_time=local_to,
            bucket="one_hour",
        )
    )
    series = cast(list[dict[str, Any]], result["series"])
    points = cast(list[dict[str, Any]], series[0]["points"])
    assert [point["startAt"] for point in points] == [
        "2026-03-29T00:00:00Z",
        "2026-03-29T01:00:00Z",
        "2026-03-29T03:00:00Z",
    ]
    assert [point["value"] for point in points] == [10.0, 20.0, 30.0]


@pytest.mark.asyncio
async def test_analytics_percentiles_are_exact_and_zero_is_not_unavailable() -> None:
    at = datetime(2026, 10, 16, 12, tzinfo=UTC)
    values = [1.0, 2.0, 3.0, 100.0]

    class Store:
        async def list_observations(
            self, *, limit: int | None = None
        ) -> list[MetadataObservation]:
            items = [
                _observation(index, at + timedelta(seconds=index), value=value)
                for index, value in enumerate(values, 1)
            ]
            return items if limit is None else items[:limit]

    result = await ObservatoryQueryService(cast(Any, Store())).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            aggregation="p99",
            from_time=at - timedelta(seconds=1),
            to_time=at + timedelta(seconds=10),
            bucket="one_hour",
        )
    )
    summary = cast(dict[str, Any], cast(list[dict[str, Any]], result["series"])[0]["summary"])
    assert summary["sampleCount"] == 4
    assert summary["p50"] == 3.0
    assert summary["p95"] == 100.0
    assert summary["p99"] == 100.0

    zero = await ObservatoryQueryService(
        cast(Any, _ObservationStore([_observation(9, at, value=0)]))
    ).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=at - timedelta(seconds=1),
            to_time=at + timedelta(seconds=1),
            bucket="one_hour",
        )
    )
    zero_series = cast(list[dict[str, Any]], zero["series"])[0]
    assert zero_series["availability"] == "available"
    assert cast(list[dict[str, Any]], zero_series["points"])[0]["value"] == 0.0

    empty = await ObservatoryQueryService(cast(Any, _ObservationStore([]))).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=at - timedelta(seconds=1),
            to_time=at + timedelta(seconds=1),
            bucket="one_hour",
        )
    )
    assert empty["series"] == []
    assert cast(dict[str, Any], empty["freshness"])["state"] == "unavailable"


def test_reference_percentiles_match_quantile_exact_discrete_selection() -> None:
    values = [1.0, 2.0, 3.0, 100.0]
    # These are the values produced by ClickHouse 25.8 quantileExact for the
    # same ordered sample set; neither path may interpolate between samples.
    assert {
        "p50": _analytics_percentile(values, 0.50),
        "p90": _analytics_percentile(values, 0.90),
        "p95": _analytics_percentile(values, 0.95),
        "p99": _analytics_percentile(values, 0.99),
    } == {"p50": 3.0, "p90": 100.0, "p95": 100.0, "p99": 100.0}


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [
        ({"outcome": "OK", "status": "failed"}, "succeeded"),
        ({"outcome": "ErRoR", "status": "ok"}, "failed"),
        ({"status": "COMPLETED"}, "succeeded"),
        ({"status": "CANCELLED"}, "cancelled"),
    ],
)
def test_raw_observation_outcome_aliases_match_production_precedence(
    dimensions: dict[str, str], expected: str
) -> None:
    observation = _observation(500, datetime(2026, 10, 16, 12, tzinfo=UTC), dimensions=dimensions)
    assert _normalize_outcome(observation) == expected
    lowered = _ANALYTICS_OUTCOME_SQL.casefold()
    assert "lowerutf8" in lowered
    assert lowered.index("dimensions['outcome']") < lowered.index("dimensions['status']")


@pytest.mark.asyncio
async def test_pinned_clickhouse_quantile_exact_provider_parity() -> None:
    """Run against a real pinned provider when the integration endpoint is configured."""

    endpoint = os.environ.get("AURA_AURA0087_CLICKHOUSE_URL")
    if not endpoint:
        pytest.skip("set AURA_AURA0087_CLICKHOUSE_URL for ClickHouse 25.8 coverage")
    database = os.environ.get("AURA_AURA0087_CLICKHOUSE_DATABASE", "aura_observatory")
    user = os.environ.get("AURA_AURA0087_CLICKHOUSE_USER", "observatory")
    password = os.environ.get("AURA_AURA0087_CLICKHOUSE_PASSWORD", "")
    statement = (
        "SELECT quantileExact(0.50)(arrayJoin([toFloat64(1), toFloat64(2), "
        "toFloat64(3), toFloat64(100)])) AS p50, "
        "quantileExact(0.90)(arrayJoin([toFloat64(1), toFloat64(2), "
        "toFloat64(3), toFloat64(100)])) AS p90, "
        "quantileExact(0.95)(arrayJoin([toFloat64(1), toFloat64(2), "
        "toFloat64(3), toFloat64(100)])) AS p95, "
        "quantileExact(0.99)(arrayJoin([toFloat64(1), toFloat64(2), "
        "toFloat64(3), toFloat64(100)])) AS p99"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            endpoint,
            params={"database": database},
            auth=(user, password),
            content=statement + " FORMAT JSONEachRow",
        )
    response.raise_for_status()
    result = json.loads(response.text)
    assert result == {"p50": 3, "p90": 100, "p95": 100, "p99": 100}
    assert _analytics_percentile([1.0, 2.0, 3.0, 100.0], 0.50) == result["p50"]


class _ObservationStore:
    def __init__(self, values: list[MetadataObservation]) -> None:
        self.values = values

    async def list_observations(
        self, *, limit: int | None = None
    ) -> list[MetadataObservation]:
        return self.values if limit is None else self.values[:limit]


def test_analytics_unavailable_signals_and_error_dimensions_are_bounded() -> None:
    unavailable = analytics_unavailable("operation_duration_ms")
    assert unavailable == ["model_identity", "retryability", "saturation"]
    assert len(unavailable) <= 10


@pytest.mark.asyncio
async def test_error_class_grouping_returns_only_closed_metadata_values() -> None:
    at = datetime(2026, 10, 16, 12, tzinfo=UTC)

    class Store:
        async def list_observations(
            self, *, limit: int | None = None
        ) -> list[MetadataObservation]:
            values = [
                _observation(
                    1,
                    at,
                    dimensions={"errorClass": "timeout", "outcome": "failed"},
                ),
                _observation(
                    2,
                    at + timedelta(seconds=1),
                    dimensions={"errorClass": "validation", "outcome": "failed"},
                ),
            ]
            return values if limit is None else values[:limit]

    result = await ObservatoryQueryService(cast(Any, Store())).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            group_by="error_class",
            from_time=at - timedelta(seconds=1),
            to_time=at + timedelta(seconds=3),
            bucket="one_hour",
        )
    )
    series = cast(list[dict[str, Any]], result["series"])
    assert [cast(dict[str, str], item["facets"])["errorClass"] for item in series] == [
        "timeout",
        "validation",
    ]
    assert "exception" not in str(result).lower()


class _CapturingAnalyticsStore(ProductionObservatoryStore):
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            lambda: None,
            "http://clickhouse.invalid",
            database="observatory",
            user="user",
            password="pass",
        )
        self.rows = rows or []
        self.query = ""
        self.params: dict[str, object] = {}

    async def _rows(
        self, query: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        self.query = query
        self.params = dict(params or {})
        return self.rows


@pytest.mark.asyncio
async def test_clickhouse_adapter_enforces_scan_execution_and_series_point_bounds() -> None:
    rows = [
        {
            "bucket_start": f"2026-10-16 12:{index:02d}:00",
            "group_value": f"aura.component.{index}",
            "sample_count": 1,
            "average": float(index),
            "minimum": float(index),
            "maximum": float(index),
            "p50": float(index),
            "p90": float(index),
            "p95": float(index),
            "p99": float(index),
            "scanned_rows": 1_000_000,
            "summary_sample_count": 1,
            "summary_minimum": float(index),
            "summary_maximum": float(index),
            "summary_average": float(index),
            "summary_p50": float(index),
            "summary_p90": float(index),
            "summary_p95": float(index),
            "summary_p99": float(index),
            "summary_total": float(index),
        }
        for index in range(12)
    ]
    store = _CapturingAnalyticsStore(rows)
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_minute",
            group_by="component",
            series_limit=2,
            point_limit=3,
        )
    )
    assert len(cast(list[Any], result["series"])) == 2
    assert "series_limit" in cast(list[str], result["partialReasons"])
    assert "scan_limit" in cast(list[str], result["partialReasons"])
    assert "LIMIT 9" in store.query
    assert (
        "row_number() OVER (ORDER BY ingest_sequence, observation_id) AS scan_rank"
        in store.query
    )
    assert "ORDER BY ingest_sequence, observation_id LIMIT 1000001" in store.query
    assert "raw_source_limited AS (SELECT" in store.query
    assert "FROM raw_source_limited" in store.query
    assert store.query.count("FROM raw_source WHERE") == 1
    assert "count() OVER () AS scan_rows_read" in store.query
    assert "source_late_data" in store.query
    assert "max(if(prior_ingest_observed_at IS NOT NULL" in store.query
    assert (
        "quantileExact(0.90)(measurement_value) OVER (PARTITION BY group_value)"
        in store.query
    )
    assert all(
        marker not in store.query
        for marker in ("scan_meta AS", "late_meta AS", "global_meta AS", "INNER JOIN summary")
    )
    assert all(value not in store.query for value in ("operation_duration_ms", "milliseconds"))
    assert "max_execution_time = 5" in store.query
    assert "max_rows_to_read = 1000001" in store.query
    assert "send_progress_in_http_headers = 1" in store.query
    assert "read_overflow_mode = 'break'" in store.query
    assert re.search(r"(?<![A-Za-z0-9_])overflow_mode\s*=", store.query) is None
    for forbidden in ("prompt", "response", "credential", "bearer", "tool_argument"):
        assert forbidden not in store.query.lower()


@pytest.mark.asyncio
async def test_late_data_from_overflow_page_is_source_global() -> None:
    def row(group: str, *, source_late_data: int = 0) -> dict[str, Any]:
        return {
            "bucket_start": "2026-10-16 12:00:00",
            "group_value": group,
            "sample_count": 1,
            "average": 1.0,
            "minimum": 1.0,
            "maximum": 1.0,
            "total": 1.0,
            "p50": 1.0,
            "p90": 1.0,
            "p95": 1.0,
            "p99": 1.0,
            "scan_rows_read": 3,
            "source_late_data": source_late_data,
            "late_data": 0,
            "summary_sample_count": 1,
            "summary_minimum": 1.0,
            "summary_maximum": 1.0,
            "summary_average": 1.0,
            "summary_p50": 1.0,
            "summary_p90": 1.0,
            "summary_p95": 1.0,
            "summary_p99": 1.0,
            "summary_total": 1.0,
        }

    # The provider's aggregate LIMIT omits the late bucket/group.  The
    # source-global marker is repeated on returned rows so Python can retain
    # the late-data signal without another physical table scan.
    store = _CapturingAnalyticsStore(
        [row("first", source_late_data=1), row("second"), row("third")]
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
            group_by="component",
            series_limit=1,
            point_limit=1,
        )
    )
    assert "source_late_data" in store.query
    assert "late_data" in cast(list[str], result["partialReasons"])
    assert cast(list[dict[str, Any]], result["series"])[0]["partial"] is True


@pytest.mark.asyncio
async def test_clickhouse_overflow_http_response_becomes_scan_limit_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the production HTTP path for ClickHouse's bounded break mode."""

    row = {
        "bucket_start": "2026-10-16 12:00:00",
        "group_value": "all",
        "sample_count": 1_000_000,
        "average": 2.0,
        "minimum": 1.0,
        "maximum": 3.0,
        "total": 2_000_000.0,
        "p50": 2.0,
        "p90": 3.0,
        "p95": 3.0,
        "p99": 3.0,
        "scan_rows_read": 1_000_000,
        "summary_sample_count": 1_000_000,
        "summary_minimum": 1.0,
        "summary_maximum": 3.0,
        "summary_average": 2.0,
        "summary_p50": 2.0,
        "summary_p90": 3.0,
        "summary_p95": 3.0,
        "summary_p99": 3.0,
        "summary_total": 2_000_000.0,
        "latest_observed_at": "2026-10-16 12:00:00",
        "late_data": 0,
    }
    captured: dict[str, object] = {}
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://clickhouse.invalid"),
        text=json.dumps(row),
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            captured["url"] = url
            captured.update(kwargs)
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
        )
    )

    assert "read_overflow_mode = 'break'" in str(captured["content"])
    assert re.search(
        r"(?<![A-Za-z0-9_])overflow_mode\s*=", str(captured["content"])
    ) is None
    params = cast(dict[str, str], captured["params"])
    assert params["param_from_time"] == "2026-10-16 12:00:00.000"
    assert "scan_limit" in cast(list[str], result["partialReasons"])
    assert result["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["not-json", "[1, 2, 3]"])
async def test_malformed_clickhouse_http_200_is_typed_and_content_free(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://clickhouse.invalid"),
        text=body,
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    with pytest.raises(StorageUnavailableError) as error:
        await store.operational_analytics_query(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
                to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
                bucket="one_hour",
            )
        )
    assert str(error.value) == "ClickHouse returned an invalid response"
    assert body not in str(error.value)


@pytest.mark.asyncio
async def test_malformed_clickhouse_aggregate_row_is_typed_and_content_free() -> None:
    store = _CapturingAnalyticsStore(
        [{"bucket_start": "2026-10-16 12:00:00", "group_value": "all", "sample_count": "bad"}]
    )
    with pytest.raises(StorageUnavailableError) as error:
        await store.operational_analytics_query(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
                to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
                bucket="one_hour",
            )
        )
    assert str(error.value) == "ClickHouse returned an invalid response"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_clickhouse_aggregate_value_is_typed_and_content_free(
    value: float,
) -> None:
    store = _CapturingAnalyticsStore(
        [
            {
                "bucket_start": "2026-10-16 12:00:00",
                "group_value": "all",
                "sample_count": 1,
                "average": value,
            }
        ]
    )
    with pytest.raises(StorageUnavailableError) as error:
        await store.operational_analytics_query(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
                to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
                bucket="one_hour",
            )
        )
    assert str(error.value) == "ClickHouse returned an invalid response"
    assert "nan" not in str(error.value).lower()
    assert "inf" not in str(error.value).lower()


@pytest.mark.asyncio
async def test_missing_clickhouse_progress_marks_empty_selective_result_partial() -> None:
    class MissingProgressStore(_CapturingAnalyticsStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            self.query = query
            self.params = dict(params or {})
            self._clickhouse_progress_available = True  # pyright: ignore[reportPrivateUsage]
            self._clickhouse_progress_read_rows = None  # pyright: ignore[reportPrivateUsage]
            return []

    store = MissingProgressStore()
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
            filters={"component": "selective-empty"},
        )
    )
    assert result["partial"] is True
    assert "scan_limit" in cast(list[str], result["partialReasons"])


@pytest.mark.asyncio
async def test_clickhouse_execution_timeout_becomes_bounded_partial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://clickhouse.invalid")
    response = httpx.Response(504, request=request, text="Code: 159, max_execution_time exceeded")

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
        )
    )

    assert result["partial"] is True
    assert result["series"] == []
    assert result["breakdowns"] == []
    assert result["partialReasons"] == ["execution_limit"]
    assert cast(dict[str, Any], result["freshness"])["state"] == "unavailable"


@pytest.mark.asyncio
async def test_clickhouse_read_limit_http_response_becomes_empty_scan_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://clickhouse.invalid")
    response = httpx.Response(
        408,
        request=request,
        text="Code: 159",
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
        )
    )

    assert result["partial"] is True
    assert result["series"] == []
    assert result["partialReasons"] == ["scan_limit"]
    scan = cast(dict[str, Any], result["scan"])
    assert scan["scannedRows"] == 1_000_000
    assert scan["scanLimit"] == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (408, "Code: 158, TOO_MANY_ROWS_OR_BYTES: max_rows_to_read limit reached"),
        (408, "request failed without a ClickHouse exception code"),
        (503, "Code: 159, TOO_MANY_ROWS_OR_BYTES: max_rows_to_read limit reached"),
    ],
)
async def test_unrelated_clickhouse_read_limit_responses_remain_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str
) -> None:
    request = httpx.Request("POST", "http://clickhouse.invalid")
    response = httpx.Response(status, request=request, text=body)

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    with pytest.raises(StorageUnavailableError) as error:
        await store.operational_analytics_query(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
                to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
                bucket="one_hour",
            )
        )
    assert str(error.value) == "ClickHouse is unavailable"
    assert body not in str(error.value)


@pytest.mark.asyncio
async def test_clickhouse_code_159_remains_unavailable_outside_analytics_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://clickhouse.invalid")
    response = httpx.Response(408, request=request, text="Code: 159")

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    with pytest.raises(StorageUnavailableError) as error:
        await store._query_rows("SELECT 1")  # pyright: ignore[reportPrivateUsage]
    assert str(error.value) == "ClickHouse is unavailable"


@pytest.mark.asyncio
async def test_clickhouse_transport_failure_is_typed_and_query_metrics_are_exact_once() -> None:
    class FailingStore(_CapturingAnalyticsStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            self.query = query
            raise httpx.ConnectError("backend unavailable")

    request = AnalyticsRequest(
        metric="operation_duration_ms",
        unit="milliseconds",
        from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
        to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
        bucket="one_hour",
    )
    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(object(), metrics=metrics, query_port=FailingStore())  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailableError):
        await service.analytics(request)
    failed = ("observatory_query_outcome", (("outcome", "failed"), ("resource", "analytics")))
    duration = ("observatory_query_duration_ms", (("resource", "analytics"),))
    assert metrics.values[failed] == 1
    assert metrics._histogram_counts[duration] == 1  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_successful_analytics_records_one_success_and_content_free_result() -> None:
    at = datetime(2026, 10, 16, 12, tzinfo=UTC)
    metrics = ObservatoryMetrics()

    class Store:
        async def list_observations(
            self, *, limit: int | None = None
        ) -> list[MetadataObservation]:
            values = [_observation(1, at, value=5)]
            return values if limit is None else values[:limit]

    service = ObservatoryQueryService(cast(Any, Store()), metrics=metrics)
    result = await service.analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=at - timedelta(minutes=1),
            to_time=at + timedelta(minutes=1),
            bucket="one_hour",
        )
    )
    succeeded = ("observatory_query_outcome", (("outcome", "succeeded"), ("resource", "analytics")))
    duration = ("observatory_query_duration_ms", (("resource", "analytics"),))
    assert metrics.values[succeeded] == 1
    assert metrics._histogram_counts[duration] == 1  # pyright: ignore[reportPrivateUsage]
    text = str(result).lower()
    assert all(value not in text for value in ("prompt", "response", "credential", "bearer"))


@pytest.mark.asyncio
async def test_catalogue_combinations_are_enforced_by_the_service() -> None:
    service = ObservatoryQueryService(MemoryObservatoryStore())
    with pytest.raises(ValueError, match="aggregation"):
        await service.analytics(AnalyticsRequest(metric="errors", unit="count", aggregation="p95"))
    with pytest.raises(ValueError, match="focus"):
        await service.analytics(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                focus="model",
            )
        )


@pytest.mark.asyncio
async def test_clickhouse_values_are_http_parameters_and_outcome_filter_is_normalized() -> None:
    store = _CapturingAnalyticsStore(
        [
            {
                "bucket_start": "2026-10-16 12:00:00",
                "group_value": "succeeded",
                "sample_count": 1,
                "average": 4.0,
                "minimum": 4.0,
                "maximum": 4.0,
                "p50": 4.0,
                "p90": 4.0,
                "p95": 4.0,
                "p99": 4.0,
                "summary_sample_count": 1,
                "summary_minimum": 4.0,
                "summary_maximum": 4.0,
                "summary_average": 4.0,
                "summary_p50": 4.0,
                "summary_p90": 4.0,
                "summary_p95": 4.0,
                "summary_p99": 4.0,
                "summary_total": 4.0,
                "scan_rows_read": 1,
                "latest_observed_at": "2026-10-16 12:00:00",
            }
        ]
    )
    value = "aura.safe' OR 1=1"
    await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            group_by="outcome",
            filters={"component": value, "outcome": "succeeded"},
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
        )
    )
    assert value not in store.query
    assert store.params["filter_component"] == value
    assert store.params["filter_outcome"] == "succeeded"
    assert "dimensions['status']" in store.query
    assert "dimensions['outcome']" in store.query


@pytest.mark.asyncio
async def test_aggregate_response_budget_does_not_replace_source_scan_budget() -> None:
    store = _CapturingAnalyticsStore(
        [
            {
                "bucket_start": "2026-10-16 12:00:00",
                "group_value": "all",
                "sample_count": 5_000,
                "average": 2.0,
                "minimum": 1.0,
                "maximum": 3.0,
                "p50": 2.0,
                "p90": 3.0,
                "p95": 3.0,
                "p99": 3.0,
                "summary_sample_count": 5_000,
                "summary_minimum": 1.0,
                "summary_maximum": 3.0,
                "summary_average": 2.0,
                "summary_p50": 2.0,
                "summary_p90": 3.0,
                "summary_p95": 3.0,
                "summary_p99": 3.0,
                "summary_total": 10_000.0,
                "scan_rows_read": 5_000,
                "latest_observed_at": "2026-10-16 12:59:00",
            }
        ]
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
            series_limit=2,
            point_limit=3,
        )
    )
    series = cast(list[dict[str, Any]], result["series"])
    assert series[0]["summary"]["sampleCount"] == 5_000
    assert "scan_limit" not in cast(list[str], result["partialReasons"])
    assert (
        "row_number() OVER (ORDER BY ingest_sequence, observation_id) AS scan_rank"
        in store.query
    )
    assert "read_overflow_mode = 'break'" in store.query


@pytest.mark.asyncio
async def test_reference_rate_outcome_and_bounded_freshness_semantics() -> None:
    store = MemoryObservatoryStore()
    old = datetime.now(UTC) - timedelta(days=30)
    await store.insert_observation(
        _observation(
            99,
            old,
            value=120.0,
            name="errors",
            unit="count",
            dimensions={"status": "ok"},
        )
    )
    result = await ObservatoryQueryService(store).analytics(
        AnalyticsRequest(
            metric="errors",
            unit="count",
            aggregation="rate",
            group_by="outcome",
            from_time=old - timedelta(minutes=1),
            to_time=old + timedelta(minutes=1),
            bucket="one_minute",
        )
    )
    series = cast(list[dict[str, Any]], result["series"])
    assert series[0]["facets"] == {"outcome": "succeeded"}
    assert series[0]["points"][0]["value"] == 2.0
    freshness = cast(dict[str, Any], result["freshness"])
    assert freshness["state"] == "stale"
    assert freshness["lagMs"] == 604_800_000

    future = datetime.now(UTC) + timedelta(minutes=5)
    await store.insert_observation(_observation(100, future, value=1.0))
    future_result = await ObservatoryQueryService(store).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=future - timedelta(minutes=1),
            to_time=future + timedelta(minutes=1),
            bucket="one_minute",
        )
    )
    assert cast(dict[str, Any], future_result["freshness"])["lagMs"] == 0.0


@pytest.mark.asyncio
async def test_reference_summary_rate_is_per_day_for_long_windows() -> None:
    for days, expected in ((2, 200.0), (31, 400.0 / 31.0)):
        store = MemoryObservatoryStore()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        await store.insert_observation(
            _observation(201, start + timedelta(hours=1), value=100.0, name="errors", unit="count")
        )
        await store.insert_observation(
            _observation(202, start + timedelta(hours=2), value=300.0, name="errors", unit="count")
        )
        result = await ObservatoryQueryService(store).analytics(
            AnalyticsRequest(
                metric="errors",
                unit="count",
                aggregation="rate",
                from_time=start,
                to_time=start + timedelta(days=days),
                bucket="one_day",
            )
        )
        series = cast(list[dict[str, Any]], result["series"])
        summary = cast(dict[str, Any], series[0]["summary"])
        assert summary["rateIntervalSeconds"] == 86_400
        assert abs(float(summary["ratePerInterval"]) - expected) < 1e-9


@pytest.mark.asyncio
async def test_late_and_retention_evidence_propagate_to_all_analytics_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        async def fetchone(self) -> tuple[bool]:
            return (True,)

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: str, params: object) -> Result:
            del statement, params
            return Result()

    row = {
        "bucket_start": "2026-10-16 12:00:00",
        "group_value": "succeeded",
        "sample_count": 1,
        "average": 2.0,
        "minimum": 2.0,
        "maximum": 2.0,
        "total": 2.0,
        "p50": 2.0,
        "p90": 2.0,
        "p95": 2.0,
        "p99": 2.0,
        "scan_rows_read": 1,
        "summary_sample_count": 1,
        "summary_minimum": 2.0,
        "summary_maximum": 2.0,
        "summary_average": 2.0,
        "summary_p50": 2.0,
        "summary_p90": 2.0,
        "summary_p95": 2.0,
        "summary_p99": 2.0,
        "summary_total": 2.0,
        "latest_observed_at": "2026-10-16 12:00:00",
        "late_data": 1,
    }
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://clickhouse.invalid"),
        text=json.dumps(row),
    )

    class Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return response

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: Connection(),
        "http://clickhouse.invalid",
        database="observatory",
        user="user",
        password="pass",
    )
    result = await store.operational_analytics_query(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            from_time=datetime(2026, 10, 16, 12, tzinfo=UTC),
            to_time=datetime(2026, 10, 16, 13, tzinfo=UTC),
            bucket="one_hour",
            group_by="outcome",
        )
    )
    reasons = cast(list[str], result["partialReasons"])
    assert "late_data" in reasons
    assert "retention_boundary" in reasons
    series = cast(list[dict[str, Any]], result["series"])
    assert series[0]["partial"] is True
    assert cast(list[dict[str, Any]], series[0]["points"])[0]["partial"] is True
    assert cast(list[dict[str, Any]], result["breakdowns"])[0]["partial"] is True


@pytest.mark.asyncio
async def test_postgres_retention_boundary_uses_deleted_observation_time_domain() -> None:
    """Exercise historical, overlapping, and purge-all rows against PostgreSQL."""

    database_url = os.environ.get("AURA_AURA0087_POSTGRES_URL")
    if not database_url:
        pytest.skip("set AURA_AURA0087_POSTGRES_URL for PostgreSQL retention coverage")
    schema = f"aura0087_{uuid4().hex[:12]}"
    lower = datetime(2026, 10, 16, 12, tzinfo=UTC)
    upper = lower + timedelta(hours=1)
    connection: Any = await psycopg.AsyncConnection.connect(database_url)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute(
            """CREATE TABLE observatory_retention_actions(
                action_kind text NOT NULL,
                status text NOT NULL,
                requested_cutoff timestamptz,
                completed_at timestamptz
            )"""
        )
        await connection.commit()

        @asynccontextmanager
        async def factory() -> AsyncGenerator[Any]:
            yield connection

        store = ProductionObservatoryStore(
            factory,
            "http://clickhouse.invalid",
            database="observatory",
            user="user",
            password="pass",
        )
        await connection.execute(
            "INSERT INTO observatory_retention_actions VALUES (%s, %s, %s, %s)",
            ("delete_before", "succeeded", lower - timedelta(days=1), lower),
        )
        await connection.commit()
        assert await store._analytics_retention_boundary(lower, upper) is False  # pyright: ignore[reportPrivateUsage]

        await connection.execute(
            "INSERT INTO observatory_retention_actions VALUES (%s, %s, %s, %s)",
            ("delete_before", "succeeded", lower + timedelta(minutes=30), lower),
        )
        await connection.commit()
        assert await store._analytics_retention_boundary(lower, upper) is True  # pyright: ignore[reportPrivateUsage]

        await connection.execute("DELETE FROM observatory_retention_actions")
        await connection.execute(
            "INSERT INTO observatory_retention_actions VALUES (%s, %s, %s, %s)",
            ("purge_all", "succeeded", None, lower - timedelta(days=30)),
        )
        await connection.commit()
        assert await store._analytics_retention_boundary(lower, upper) is True  # pyright: ignore[reportPrivateUsage]
    finally:
        await connection.rollback()
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.commit()
        await connection.close()
