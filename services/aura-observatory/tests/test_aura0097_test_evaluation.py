"""Independent AURA-0097 operations contract and recovery coverage.

These tests deliberately exercise the public service boundaries with bounded
fixtures.  They do not inspect SQL implementation details or require a live
provider; the opt-in PostgreSQL migration checks remain in the integration
suite.
"""
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from aura_observatory.adapters.dashboards import MemoryOperationsRepository
from aura_observatory.alerting import AlertError, AlertService
from aura_observatory.annotations import AnnotationError, AnnotationService
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api import app as api_module
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.entrypoints.worker import _run_operations_worker
from aura_observatory.incidents import IncidentError, IncidentService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.operational_live import OperationResourceType
from aura_observatory.saved_queries import (
    SavedQueryError,
    SavedQueryExecutionService,
    SavedQueryService,
    decode_execution_cursor,
    normalize_definition,
)
from aura_observatory.settings import Settings

NOW = "2026-10-21T10:00:00Z"


def _run_query() -> dict[str, object]:
    return {
        "kind": "run_search",
        "window": {"kind": "last_hour", "from": None, "to": None},
        "statuses": [],
        "components": [],
        "deployments": [],
        "models": [],
        "pageSize": 10,
    }


def _analytics_query() -> dict[str, object]:
    return {
        "kind": "analytics",
        "metric": "operation_duration_ms",
        "unit": "milliseconds",
        "aggregation": "p95",
        "window": {"kind": "last_hour", "from": None, "to": None},
        "bucket": "auto",
        "groupBy": "component",
        "focus": "all",
        "facets": [],
        "seriesLimit": 1,
        "pointLimit": 60,
    }


def _closed_analytics_result(*, facets: dict[str, str] | None = None) -> dict[str, object]:
    """Return a complete observatory-v1 analytics result fixture."""

    return {
        "snapshotAt": NOW,
        "query": {
            "metric": "operation_duration_ms",
            "unit": "milliseconds",
            "aggregation": "p95",
            "focus": "all",
            "from": "2026-10-21T09:00:00Z",
            "to": NOW,
            "bucket": "auto",
            "bucketSeconds": 3600,
            "groupBy": "component",
            "filters": facets or {},
            "seriesLimit": 1,
            "pointLimit": 60,
        },
        "percentileMethod": "clickhouse_quantile_exact",
        "series": [
            {
                "seriesKey": "aura.core.api",
                "facets": facets or {"component": "aura.core.api"},
                "summary": {
                    "sampleCount": 4,
                    "minimum": 50,
                    "maximum": 250,
                    "average": 125,
                    "p50": 100,
                    "p90": 200,
                    "p95": 250,
                    "p99": 250,
                    "ratePerInterval": None,
                    "rateIntervalSeconds": None,
                },
                "points": [],
                "availability": "available",
                "partial": False,
            }
        ],
        "breakdowns": [],
        "freshness": {"latestObservedAt": NOW, "lagMs": 0, "state": "fresh"},
        "partial": False,
        "partialReasons": [],
        "unavailableSignals": [],
        "scan": {
            "scannedRows": 4,
            "scanLimit": 1_000_000,
            "executionMs": 1,
            "executionLimitMs": 5_000,
        },
        "capturePolicy": "metadata_only",
    }


def _closed_run_item(
    *, trace_id: str | None = None, status: str = "succeeded"
) -> dict[str, object]:
    return {
        "runId": str(uuid4()),
        "status": status,
        "startedAt": "2026-10-21T09:59:00Z",
        "finishedAt": NOW,
        "durationMs": 60000,
        "model": "local-model",
        "deploymentId": "dev",
        "componentIds": ["aura.core.api"],
        "traceId": trace_id,
    }


def _closed_run_result(
    *, trace_id: str | None = None, next_cursor: str | None = None
) -> dict[str, object]:
    return {
        "items": [_closed_run_item(trace_id=trace_id)],
        "nextCursor": next_cursor,
        "snapshotAt": NOW,
        "latestObservationId": str(uuid4()),
    }


def _closed_comparison_result(baseline: str, candidate: str) -> dict[str, object]:
    def side(role: str, run_id: str) -> dict[str, object]:
        return {
            "role": role,
            "runId": run_id,
            "evidence": "available",
            "status": "succeeded",
            "outcome": "succeeded",
            "startedAt": "2026-10-21T09:59:00Z",
            "finishedAt": NOW,
            "durationMs": 60000,
            "traceId": None,
            "modelId": {"availability": "available", "value": "local-model"},
            "modelVersion": {"availability": "available", "value": "1.0.0"},
            "deploymentId": {"availability": "available", "value": "dev"},
            "gitCommit": {"availability": "available", "value": "abc123"},
            "imageVersion": {"availability": "available", "value": "image-1"},
            "configurationVersion": {"availability": "available", "value": "config-1"},
            "agentRevision": {"availability": "available", "value": "agent-1"},
            "promptBundleRevision": {"availability": "available", "value": "bundle-1"},
            "policyRevision": {"availability": "available", "value": "policy-1"},
            "componentVersions": [],
            "partial": False,
        }

    return {
        "snapshotAt": NOW,
        "left": side("left", baseline),
        "right": side("right", candidate),
        "markerCorrelations": [],
        "components": [],
        "trace": {
            "leftTraceId": None,
            "rightTraceId": None,
            "leftEvidence": "missing",
            "rightEvidence": "missing",
            "leftIntegrity": ["complete"],
            "rightIntegrity": ["complete"],
            "alignedSpans": [],
            "partial": False,
        },
        "freshness": {"latestObservedAt": NOW, "lagMs": 0, "state": "fresh"},
        "partial": False,
        "partialReasons": [],
        "unavailableMarkerCount": 0,
        "scan": {
            "scannedRows": 2,
            "scanLimit": 1_000_000,
            "executionMs": 1,
            "executionLimitMs": 5_000,
        },
        "limits": {
            "runCount": 2,
            "maximumObservationsPerRun": 10000,
            "maximumSpansPerRun": 5000,
            "maximumMeasurements": 2000,
            "maximumScannedRows": 1_000_000,
            "executionTimeoutMs": 5_000,
        },
        "capturePolicy": "metadata_only",
    }


def _closed_regression_item() -> dict[str, object]:
    digest = "a" * 64
    result_id = str(uuid4())
    return {
        "schemaVersion": 1,
        "resourceType": "regression_decision",
        "regressionId": str(uuid4()),
        "regressionVersion": "1.0.0",
        "experiment": {"resourceId": str(uuid4()), "resourceVersion": "1.0.0"},
        "baseline": {"resourceId": str(uuid4()), "resourceVersion": "1.0.0"},
        "variant": {
            "variantId": "variant-a",
            "components": [
                {
                    "componentId": "aura.core.api",
                    "componentVersion": "1.0.0",
                    "codeRevision": "abc123",
                    "imageDigest": digest,
                    "configurationRevision": "config-1",
                }
            ],
            "model": {"modelId": "local-model", "modelVersion": "1.0.0", "modelDigest": digest},
            "configurationRevision": "config-1",
            "promptRevision": "revision-1",
            "policyRevision": "policy-1",
            "deploymentRevision": "deploy-1",
        },
        "cohortIds": ["cohort-a"],
        "baselineResultIds": [result_id],
        "candidateResultIds": [str(uuid4())],
        "comparisons": [{
            "metric": "latency",
            "metricClass": "operational",
            "evaluator": "deterministic",
            "unit": "milliseconds",
            "aggregation": "p95",
            "direction": "higher_is_worse",
            "comparisonMode": "absolute",
            "baselineValue": 100,
            "candidateValue": 110,
            "difference": 10,
            "relativeChange": 0.1,
            "thresholdValue": 5,
            "minimumSampleSize": 1,
            "baselineSampleSize": 1,
            "candidateSampleSize": 1,
            "missingEvidencePolicy": "inconclusive",
            "outcome": "unchanged",
        }],
        "decision": "unchanged",
        "evidenceState": "complete",
        "evidence": [],
        "attribution": {},
        "capturePolicy": "metadata_only",
        "createdAt": NOW,
    }


def _closed_health_result(component_id: str) -> dict[str, object]:
    summary = {
        "sampleCount": 4,
        "latencyP50Ms": 100,
        "latencyP95Ms": 250,
        "errorRate": 0.0,
        "outcomes": {"succeeded": 4, "failed": 0, "cancelled": 0, "unknown": 0},
    }
    return {"componentId": component_id, "summary": summary, "points": []}


def _saved_query_definitions() -> list[dict[str, object]]:
    """One producer fixture for every closed SavedQueryDefinition variant."""

    run_id = str(uuid4())
    return [
        _analytics_query(),
        {
            **_run_query(),
            "statuses": ["running", "failed"],
            "components": ["aura.core.api", "aura.core.worker"],
            "deployments": ["dev", "canary"],
            "models": ["llama3.2:3b", "qwen2.5:7b"],
        },
        {
            "kind": "trace_search",
            "window": {"kind": "last_6_hours", "from": None, "to": None},
            "components": ["aura.core.api"],
            "outcomes": ["succeeded", "failed"],
            "pageSize": 10,
        },
        {
            "kind": "run_comparison",
            "baselineRunId": run_id,
            "candidateRunId": str(uuid4()),
        },
        {
            "kind": "regression_search",
            "window": {"kind": "last_24_hours", "from": None, "to": None},
            "outcomes": ["regressed", "inconclusive"],
            "experimentReferences": [
                {"resourceId": str(uuid4()), "resourceVersion": "1.0.0"},
                {"resourceId": str(uuid4()), "resourceVersion": "2.0.0"},
            ],
            "pageSize": 10,
        },
        {
            "kind": "component_health",
            "componentId": "aura.core.api",
            "componentVersion": "1.2.3",
            "window": "one_hour",
            "pointLimit": 30,
        },
    ]


def _rule_request(query_id: str | None = None) -> dict[str, object]:
    return {
        "name": "latency",
        "severity": "warning",
        "condition": {
            "signal": "latency",
            "query": {"queryId": query_id or str(uuid4()), "queryVersion": "1.0.0"},
            "operator": "greater_than",
            "threshold": 100,
            "unit": "milliseconds",
            "window": {"kind": "last_hour", "from": None, "to": None},
            "minimumSampleSize": 1,
            "missingEvidencePolicy": "inconclusive",
            "staleAfterSeconds": 300,
            "groupBy": ["component"],
        },
        "cadence": "five_minutes",
        "state": "enabled",
        "createdAt": NOW,
    }


@pytest.mark.parametrize(
    "invalid",
    [
        {"kind": "custom", "from": "2026-10-21T11:00:00Z", "to": "2026-10-21T10:00:00Z"},
        {"kind": "custom", "from": "2026-10-21T10:00:00", "to": "2026-10-21T11:00:00Z"},
        {"kind": "last_hour", "from": "2026-10-21T10:00:00Z", "to": None},
    ],
)
def test_saved_query_rejects_unordered_or_open_window_bounds(invalid: dict[str, object]) -> None:
    with pytest.raises(SavedQueryError):
        normalize_definition(
            {
                "kind": "analytics",
                "metric": "operation_duration_ms",
                "unit": "milliseconds",
                "aggregation": "p95",
                "window": invalid,
                "bucket": "auto",
                "groupBy": "none",
                "focus": "all",
                "facets": [],
                "seriesLimit": 1,
                "pointLimit": 60,
            }
        )


def test_saved_query_rejects_private_facets_and_executable_fields() -> None:
    with pytest.raises(SavedQueryError):
        normalize_definition({**_run_query(), "sql": "select 1"})
    with pytest.raises(SavedQueryError):
        normalize_definition(
            {
                **_analytics_query(),
                "facets": [{"name": "component", "value": "prompt-output"}],
            }
        )


def test_saved_query_producer_conforms_to_each_closed_definition_and_vocabulary() -> None:
    definitions = _saved_query_definitions()
    assert [str(item["kind"]) for item in definitions] == [
        "analytics",
        "run_search",
        "trace_search",
        "run_comparison",
        "regression_search",
        "component_health",
    ]
    for definition in definitions:
        normalized = normalize_definition(definition)
        assert set(normalized) == set(definition)
        assert normalized["kind"] == definition["kind"]
    regression = definitions[4]
    assert set(regression) == {
        "kind",
        "window",
        "outcomes",
        "experimentReferences",
        "pageSize",
    }
    with pytest.raises(SavedQueryError):
        normalize_definition({**regression, "outcomes": ["not-a-regression"]})
    with pytest.raises(SavedQueryError):
        normalize_definition({**_analytics_query(), "aggregation": "not-an-aggregation"})


@pytest.mark.asyncio
async def test_grouping_compatibility_accepts_regression_outcome_only_at_alert_boundary() -> None:
    analytics = {
        **_analytics_query(),
        "groupBy": "error_class",
        "facets": [{"name": "regression_outcome", "value": "regressed"}],
    }
    assert normalize_definition(analytics)["facets"] == [
        {"name": "regression_outcome", "value": "regressed"}
    ]
    with pytest.raises(SavedQueryError):
        normalize_definition({**analytics, "groupBy": "regression_outcome"})

    repository = MemoryOperationsRepository()
    rule_request = _rule_request()
    rule_request["condition"] = {
        **rule_request["condition"],
        "groupBy": ["regression_outcome"],
    }
    rule = await AlertService(repository).create_rule(rule_request)
    assert rule["condition"]["groupBy"] == ["regression_outcome"]


@pytest.mark.asyncio
async def test_execute_saved_query_returns_all_six_closed_result_variants() -> None:
    repository = MemoryOperationsRepository()
    saved_queries = SavedQueryService(repository)
    shared_run_id = str(uuid4())
    shared_trace_id = uuid4().hex

    class QueryPort:
        async def analytics(self, _request: object) -> dict[str, object]:
            return _closed_analytics_result()

        async def runs(
            self, _filters: object, *, cursor: str | None, limit: int
        ) -> dict[str, object]:
            del cursor
            item = _closed_run_item(trace_id=shared_trace_id)
            item["runId"] = shared_run_id
            return {**_closed_run_result(trace_id=shared_trace_id), "items": [item]}

        async def compare_runs(self, baseline: str, candidate: str) -> dict[str, object]:
            return _closed_comparison_result(baseline, candidate)

        async def component_health(self, component_id: str, **_kwargs: object) -> dict[str, object]:
            return _closed_health_result(component_id)

    class EvaluationPort:
        async def collection_async(self, _name: str, **_kwargs: object) -> dict[str, object]:
            return {
                "items": [{**_closed_regression_item(), "decision": "regressed"}],
                "nextCursor": None,
                "snapshotAt": NOW,
                "partial": False,
            }

    execution = SavedQueryExecutionService(
        repository,
        query_port=QueryPort(),
        evaluation_port=EvaluationPort(),
    )
    for index, definition in enumerate(_saved_query_definitions()):
        query = await saved_queries.create(
            {"name": f"query-{index}", "definition": definition, "createdAt": NOW}
        )
        result = await execution.execute(str(query["queryId"]), "1.0.0")
        assert result["kind"] == definition["kind"]
        assert result["state"] == "complete", (definition["kind"], result)
        assert result["partial"] is False
        assert result["result"] is not None


@pytest.mark.asyncio
async def test_saved_query_execution_propagates_filters_windows_facets_and_cursor_replay() -> None:
    repository = MemoryOperationsRepository()
    saved_queries = SavedQueryService(repository)
    analytics_definition = {
        **_analytics_query(),
        "window": {"kind": "last_6_hours", "from": None, "to": None},
        "groupBy": "component",
        "facets": [{"name": "component", "value": "aura.core.api"}],
    }
    analytics_query = await saved_queries.create(
        {"name": "Analytics", "definition": analytics_definition, "createdAt": NOW}
    )
    run_definition = {
        **_run_query(),
        "statuses": ["running"],
        "components": ["aura.core.api"],
        "deployments": ["dev"],
        "models": ["llama3.2:3b"],
    }
    run_query = await saved_queries.create(
        {"name": "Runs", "definition": run_definition, "createdAt": NOW}
    )
    analytics_requests: list[object] = []
    run_filters: list[object] = []
    run_cursors: list[str | None] = []

    class QueryPort:
        async def analytics(self, request: object) -> dict[str, object]:
            analytics_requests.append(request)
            return _closed_analytics_result(facets={"component": "aura.core.api"})

        async def runs(
            self, filters: object, *, cursor: str | None, limit: int
        ) -> dict[str, object]:
            run_filters.append(filters)
            run_cursors.append(cursor)
            assert limit == 10
            return {
                **_closed_run_result(
                    next_cursor="provider-cursor-01" if cursor is None else None
                ),
                "items": [_closed_run_item(status="running")],
            }

    execution = SavedQueryExecutionService(repository, query_port=QueryPort())
    analytics_result = await execution.execute(
        str(analytics_query["queryId"]), "1.0.0"
    )
    assert analytics_result["state"] == "complete"
    request = analytics_requests[0]
    assert request.filters == {"component": "aura.core.api"}  # type: ignore[union-attr]
    assert request.group_by == "component"  # type: ignore[union-attr]
    assert request.to_time - request.from_time == timedelta(hours=6)  # type: ignore[union-attr]

    first = await execution.execute(str(run_query["queryId"]), "1.0.0")
    cursor = first["nextCursor"]
    assert isinstance(cursor, str)
    assert decode_execution_cursor(
        cursor,
        kind="run_search",
        query_id=str(run_query["queryId"]),
        query_version="1.0.0",
    ) == "provider-cursor-01"
    replay = await execution.execute(str(run_query["queryId"]), "1.0.0", cursor=cursor)
    assert replay["state"] == "complete"
    assert run_cursors == [None, "provider-cursor-01"]
    assert len(run_filters) == 2
    assert all(item.status == "running" for item in run_filters)  # type: ignore[union-attr]
    assert all(item.component == "aura.core.api" for item in run_filters)  # type: ignore[union-attr]
    assert all(item.deployment == "dev" for item in run_filters)  # type: ignore[union-attr]
    assert all(item.model == "llama3.2:3b" for item in run_filters)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_saved_query_execution_rejects_closed_result_and_private_payload_escapes() -> None:
    repository = MemoryOperationsRepository()
    saved_queries = SavedQueryService(repository)
    run_query = await saved_queries.create(
        {"name": "Runs", "definition": _run_query(), "createdAt": NOW}
    )
    analytics_query = await saved_queries.create(
        {"name": "Analytics", "definition": _analytics_query(), "createdAt": NOW}
    )

    class RunProvider:
        async def runs(
            self, _filters: object, *, cursor: str | None, limit: int
        ) -> dict[str, object]:
            del cursor, limit
            return {
                "items": [{"runId": str(uuid4()), "prompt": "do not persist"}],
                "nextCursor": None,
                "snapshotAt": NOW,
                "partial": False,
            }

    class AnalyticsProvider:
        async def analytics(self, _request: object) -> dict[str, object]:
            return {
                "series": [
                    {
                        "summary": {"p95": 1, "sampleCount": 1},
                        "facets": {"component": "prompt-output"},
                    }
                ],
                "snapshotAt": NOW,
                "partial": False,
            }

    run_result = await SavedQueryExecutionService(
        repository, query_port=RunProvider()
    ).execute(str(run_query["queryId"]), "1.0.0")
    analytics_result = await SavedQueryExecutionService(
        repository, query_port=AnalyticsProvider()
    ).execute(str(analytics_query["queryId"]), "1.0.0")
    assert run_result["state"] == "incompatible"
    assert analytics_result["state"] == "incompatible"
    assert run_result["result"] is None
    assert analytics_result["result"] is None
    assert "do not persist" not in repr(run_result)
    assert "prompt-output" not in repr(analytics_result)


@pytest.mark.asyncio
async def test_saved_query_execution_instrumentation_is_truthful_and_best_effort() -> None:
    repository = MemoryOperationsRepository()
    query = await SavedQueryService(repository).create(
        {"name": "Runs", "definition": _run_query(), "createdAt": NOW}
    )
    metrics = ObservatoryMetrics()

    class UnavailableProvider:
        async def runs(
            self, _filters: object, *, cursor: str | None, limit: int
        ) -> dict[str, object]:
            del cursor, limit
            raise ConnectionError("query provider unavailable")

    result = await SavedQueryExecutionService(
        repository, query_port=UnavailableProvider(), metrics=metrics
    ).execute(str(query["queryId"]), "1.0.0")
    assert result["state"] == "unavailable"
    spans = [
        span
        for span in metrics.component_spans
        if span.component_id == "aura.observatory.saved_queries"
    ]
    assert {span.operation for span in spans} >= {"definition_get", "query"}
    query_span = next(span for span in spans if span.operation == "query")
    assert (query_span.outcome, query_span.dependency, query_span.error_code) == (
        "unavailable",
        "clickhouse",
        "provider_unavailable",
    )

    class FailingMetrics:
        def record(self, *_args: object, **_kwargs: object) -> None:
            raise ValueError("diagnostics unavailable")

        def record_component_span(self, **_kwargs: object) -> None:
            raise ValueError("diagnostics unavailable")

    resilient = await SavedQueryExecutionService(
        repository,
        query_port=UnavailableProvider(),
        metrics=FailingMetrics(),
    ).execute(str(query["queryId"]), "1.0.0")
    assert resilient["state"] == "unavailable"


@pytest.mark.asyncio
async def test_command_idempotency_is_atomic_under_concurrent_retries() -> None:
    repository = MemoryOperationsRepository()
    service = SavedQueryService(repository)

    values = await asyncio.gather(
        *(
            service.create(
                {"name": "Runs", "definition": _run_query(), "createdAt": NOW},
                command_id="same-command",
            )
            for _ in range(8)
        )
    )

    assert all(value == values[0] for value in values)
    assert len(repository.resources["saved_query"]) == 1


@pytest.mark.asyncio
async def test_alert_grouping_is_stable_and_repeated_observations_are_counted() -> None:
    repository = MemoryOperationsRepository()

    class QueryPort:
        async def analytics(self, _definition: object) -> dict[str, object]:
            return {"value": 250, "sampleSize": 4}

    service = AlertService(repository, query_port=QueryPort())
    rule = await service.create_rule(_rule_request())

    assert await service.evaluate_once(now=NOW) == 1
    assert await service.evaluate_once(now="2026-10-21T10:05:00Z") == 1
    instances = list(repository.resources["alert_instance"].values())
    assert len(instances) == 1
    assert instances[0]["state"] == "firing"
    assert instances[0]["occurrenceCount"] == 2
    assert instances[0]["groupingId"]
    assert instances[0]["rule"]["resourceId"] == rule["ruleId"]


@pytest.mark.asyncio
async def test_alert_multi_series_grouping_minimum_sample_and_stale_boundary() -> None:
    repository = MemoryOperationsRepository()
    query = await SavedQueryService(repository).create(
        {"name": "Latency", "definition": _analytics_query(), "createdAt": NOW}
    )

    class QueryPort:
        async def analytics(self, _request: object) -> dict[str, object]:
            return {
                "series": [
                    {
                        "summary": {"p95": 250, "sampleCount": 2},
                        "facets": {"component": "aura.core.api"},
                    },
                    {
                        "summary": {"p95": 50, "sampleCount": 4},
                        "facets": {"component": "aura.core.worker"},
                    },
                ],
                "freshness": {"state": "fresh", "measuredAt": NOW, "lagMs": 300_000},
                "snapshotAt": NOW,
                "partial": False,
            }

    request = _rule_request(str(query["queryId"]))
    request["condition"] = {
        **request["condition"],
        "groupBy": ["component"],
        "minimumSampleSize": 2,
        "staleAfterSeconds": 300,
    }
    service = AlertService(repository, query_port=QueryPort())
    rule = await service.create_rule(request)
    evidence = await service._measure(rule["condition"], NOW)  # type: ignore[reportPrivateUsage]
    assert evidence["state"] == "stale"
    assert evidence["facets"] == (("component", "aura.core.api"),)
    series_evidence = evidence["seriesEvidence"]
    assert isinstance(series_evidence, list)
    assert len(series_evidence) == 2
    assert series_evidence[1]["facets"] == (("component", "aura.core.worker"),)
    instance = await service._instance(rule, evidence, NOW)  # type: ignore[reportPrivateUsage]
    assert instance["state"] == "stale"

    low_sample = dict(evidence)
    low_sample["state"] = "complete"
    low_sample["sampleSize"] = 1
    low_sample["value"] = 250.0
    minimum_instance = await service._instance(rule, low_sample, NOW)  # type: ignore[reportPrivateUsage]
    assert minimum_instance["state"] == "inconclusive"

    partial = dict(evidence)
    partial["state"] = "partial"
    partial["sampleSize"] = 2
    partial_instance = await service._instance(rule, partial, NOW)  # type: ignore[reportPrivateUsage]
    assert partial_instance["state"] == "partial"

    unavailable = dict(evidence)
    unavailable["state"] = "unavailable"
    unavailable_instance = await service._instance(rule, unavailable, NOW)  # type: ignore[reportPrivateUsage]
    assert unavailable_instance["state"] == "no_data"


@pytest.mark.asyncio
async def test_alert_acknowledgement_is_separate_and_idempotent() -> None:
    repository = MemoryOperationsRepository()
    service = AlertService(repository)
    rule = await service.create_rule(_rule_request())
    instance = {
        "instanceId": str(uuid4()),
        "instanceVersion": "1.0.0",
        "rule": {
            "resourceType": "alert_rule",
            "resourceId": rule["ruleId"],
            "resourceVersion": rule["ruleVersion"],
        },
        "groupingId": str(uuid4()),
        "groupingFacets": [],
        "severity": "warning",
        "state": "firing",
        "evidenceState": "complete",
        "observedValue": 250,
        "unit": "milliseconds",
        "sampleSize": 1,
        "evidence": [],
        "firstObservedAt": NOW,
        "lastObservedAt": NOW,
        "occurrenceCount": 1,
        "suppressed": False,
        "acknowledged": False,
    }
    await repository.create_resource("alert_instance", instance)
    request = {"instanceVersion": "1.0.0", "reason": "known_issue", "acknowledgedAt": NOW}
    values = await asyncio.gather(
        service.acknowledge(instance["instanceId"], request, command_id="ack-command"),
        service.acknowledge(instance["instanceId"], request, command_id="ack-command"),
    )
    assert values[0] == values[1]
    assert values[0]["acknowledged"] is True
    assert values[0]["state"] == "firing"


@pytest.mark.asyncio
async def test_suppression_clear_and_expiry_preserve_acknowledgement_and_occurrence() -> None:
    repository = MemoryOperationsRepository()
    base = datetime.now(UTC).replace(microsecond=0)

    def timestamp(offset: timedelta) -> str:
        return (base + offset).isoformat().replace("+00:00", "Z")

    query = await SavedQueryService(repository).create(
        {"name": "Latency", "definition": _analytics_query(), "createdAt": timestamp(timedelta())}
    )

    class QueryPort:
        async def analytics(self, _request: object) -> dict[str, object]:
            return {
                "series": [
                    {
                        "summary": {"p95": 250, "sampleCount": 3},
                        "facets": {"component": "aura.core.api"},
                    }
                ],
                "snapshotAt": timestamp(timedelta()),
                "partial": False,
            }

    request = _rule_request(str(query["queryId"]))
    request["createdAt"] = timestamp(timedelta())
    service = AlertService(repository, query_port=QueryPort())
    rule = await service.create_rule(request)
    await service.evaluate_once(now=timestamp(timedelta()))
    instances = list(repository.resources["alert_instance"].values())
    assert len(instances) == 1
    instance_id = str(instances[0]["instanceId"])
    await service.acknowledge(
        instance_id,
        {
            "instanceVersion": "1.0.0",
            "reason": "known_issue",
            "acknowledgedAt": timestamp(timedelta()),
        },
    )

    target = {
        "resourceType": "alert_rule",
        "resourceId": rule["ruleId"],
        "resourceVersion": rule["ruleVersion"],
    }
    active = await service.create_suppression(
        {
            "scope": "rule",
            "target": target,
            "startsAt": timestamp(timedelta()),
            "endsAt": timestamp(timedelta(hours=1)),
            "reason": "testing",
        }
    )
    await service.evaluate_once(now=timestamp(timedelta(minutes=5)))
    current = await repository.get_resource("alert_instance", instance_id)
    assert current is not None
    assert current["suppressed"] is True
    assert current["acknowledged"] is True
    assert current["occurrenceCount"] == 2

    await service.clear_suppression(str(active["suppressionId"]))
    await service.evaluate_once(now=timestamp(timedelta(minutes=10)))
    current = await repository.get_resource("alert_instance", instance_id)
    assert current is not None
    assert current["suppressed"] is False
    assert current["acknowledged"] is True
    assert current["occurrenceCount"] == 3

    await service.create_suppression(
        {
            "scope": "rule",
            "target": target,
            "startsAt": timestamp(timedelta(minutes=10)),
            "endsAt": timestamp(timedelta(minutes=12)),
            "reason": "testing",
        }
    )
    await service.evaluate_once(now=timestamp(timedelta(minutes=15)))
    current = await repository.get_resource("alert_instance", instance_id)
    assert current is not None
    assert current["suppressed"] is False
    assert current["acknowledged"] is True
    assert current["occurrenceCount"] == 4


@pytest.mark.asyncio
async def test_suppression_bounds_and_clear_are_explicit_and_idempotent() -> None:
    repository = MemoryOperationsRepository()
    service = AlertService(repository)
    rule = await service.create_rule(_rule_request())
    target = {
        "resourceType": "alert_rule",
        "resourceId": rule["ruleId"],
        "resourceVersion": rule["ruleVersion"],
    }
    with pytest.raises(AlertError):
        await service.create_suppression(
            {
                "scope": "rule",
                "target": target,
                "startsAt": "2026-10-21T11:00:00Z",
                "endsAt": "2026-10-21T10:00:00Z",
                "reason": "maintenance",
            }
        )

    expired = await service.create_suppression(
        {
            "scope": "rule",
            "target": target,
            "startsAt": "2020-01-01T00:00:00Z",
            "endsAt": "2020-01-01T01:00:00Z",
            "reason": "maintenance",
        }
    )
    assert expired["state"] == "expired"

    suppression = await service.create_suppression(
        {
            "scope": "rule",
            "target": target,
            "startsAt": NOW,
            "endsAt": "2026-10-21T11:00:00Z",
            "reason": "maintenance",
        },
        command_id="suppress-command",
    )
    cleared = await service.clear_suppression(
        suppression["suppressionId"], command_id="clear-command"
    )
    replay = await service.clear_suppression(
        suppression["suppressionId"], command_id="clear-command"
    )
    assert cleared == replay
    assert cleared["state"] == "cleared"
    assert cleared["endsAt"] == "2026-10-21T11:00:00Z"


@pytest.mark.asyncio
async def test_incident_state_machine_accepts_ordered_transitions_and_rejects_stale_commands() -> (
    None
):
    repository = MemoryOperationsRepository()
    service = IncidentService(repository)
    alert_id = str(uuid4())
    incident = await service.create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": alert_id,
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    investigating = await service.transition(
        incident["incidentId"],
        {
            "expectedVersion": "1.0.0",
            "targetState": "investigating",
            "impact": "degraded",
            "resolutionNote": None,
            "transitionedAt": NOW,
        },
    )
    assert investigating["state"] == "investigating"
    with pytest.raises(RuntimeError):
        await service.transition(
            incident["incidentId"],
            {
                "expectedVersion": "1.0.0",
                "targetState": "mitigated",
                "impact": "degraded",
                "resolutionNote": None,
                "transitionedAt": NOW,
            },
        )
    mitigated = await service.transition(
        incident["incidentId"],
        {
            "expectedVersion": investigating["incidentVersion"],
            "targetState": "mitigated",
            "impact": "degraded",
            "resolutionNote": None,
            "transitionedAt": "2026-10-21T10:05:00Z",
        },
    )
    assert mitigated["state"] == "mitigated"


@pytest.mark.asyncio
async def test_annotation_target_and_live_history_keep_content_isolated() -> None:
    repository = MemoryOperationsRepository()
    service = AnnotationService(repository)
    target_id = str(uuid4())
    with pytest.raises(AnnotationError):
        await service.create(
            {
                "target": {
                    "targetType": "incident",
                    "targetId": target_id,
                    "targetVersion": "1.0.0",
                },
                "text": "orphan note",
                "createdAt": NOW,
            }
        )

    incident = await IncidentService(repository).create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": str(uuid4()),
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    annotation = await service.create(
        {
            "target": {
                "targetType": "incident",
                "targetId": incident["incidentId"],
                "targetVersion": incident["incidentVersion"],
            },
            "text": "private owner note",
            "createdAt": NOW,
        }
    )
    events = await repository.events_after(None)
    assert any(event["resourceId"] == annotation["annotationId"] for event in events)
    assert all("private owner note" not in repr(event) for event in events)
    assert (
        await service.list(
            target_type="incident", target_id=incident["incidentId"], target_version="1.0.0"
        )
    )["items"][0]["text"] == "private owner note"


@pytest.mark.asyncio
async def test_annotation_create_delete_replay_preserves_content_isolation() -> None:
    repository = MemoryOperationsRepository()
    incident = await IncidentService(repository).create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": str(uuid4()),
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    annotation = await AnnotationService(repository).create(
        {
            "target": {
                "targetType": "incident",
                "targetId": incident["incidentId"],
                "targetVersion": incident["incidentVersion"],
            },
            "text": "delete me privately",
            "createdAt": NOW,
        },
        command_id="annotation-create-replay-0001",
    )
    await repository.delete_resource(
        "annotation",
        str(annotation["annotationId"]),
        str(annotation["annotationVersion"]),
        command_id="annotation-delete-replay-0001",
    )
    await repository.delete_resource(
        "annotation",
        str(annotation["annotationId"]),
        str(annotation["annotationVersion"]),
        command_id="annotation-delete-replay-0001",
    )
    assert await repository.get_resource("annotation", str(annotation["annotationId"])) is None
    assert all("delete me privately" not in repr(value) for value in repository.events)
    assert all("delete me privately" not in repr(value) for value in repository.commands.values())


@pytest.mark.asyncio
async def test_incident_references_require_exact_alert_and_evidence_versions() -> None:
    repository = MemoryOperationsRepository()
    expected_alert = {"resourceId": str(uuid4()), "resourceVersion": "2.0.0"}
    expected_evidence = {"resourceId": str(uuid4()), "resourceVersion": "3.0.0"}

    class References:
        async def validate_incident_references(self, alerts: object, evidence: object) -> None:
            assert alerts == [
                {"resourceType": "alert_instance", **expected_alert}
            ]
            assert evidence == [
                {
                    "resourceType": "run",
                    **expected_evidence,
                    "observedAt": NOW,
                }
            ]

    request = {
        "title": "exact evidence",
        "severity": "warning",
        "impact": "limited",
        "relatedAlerts": [{"resourceType": "alert_instance", **expected_alert}],
        "evidence": [
            {"resourceType": "run", **expected_evidence, "observedAt": NOW}
        ],
        "createdAt": NOW,
    }
    service = IncidentService(repository, reference_port=References())
    created = await service.create(request)
    assert created["relatedAlerts"][0]["resourceVersion"] == "2.0.0"
    assert created["evidence"][0]["resourceVersion"] == "3.0.0"

    class RejectWrongVersion(References):
        async def validate_incident_references(self, alerts: object, evidence: object) -> None:
            del alerts, evidence
            raise KeyError("exact opaque version is absent")

    wrong = dict(request)
    wrong["relatedAlerts"] = [
        {
            "resourceType": "alert_instance",
            "resourceId": expected_alert["resourceId"],
            "resourceVersion": "1.0.0",
        }
    ]
    with pytest.raises(IncidentError, match="incident reference not found"):
        await IncidentService(repository, reference_port=RejectWrongVersion()).create(wrong)


@pytest.mark.asyncio
async def test_evaluator_and_acknowledgement_race_preserves_owner_ack_state() -> None:
    repository = MemoryOperationsRepository()

    class QueryPort:
        async def analytics(self, _definition: object) -> dict[str, object]:
            return {"value": 250, "sampleSize": 2}

    service = AlertService(repository, query_port=QueryPort())
    rule = await service.create_rule(_rule_request())
    instance = {
        "instanceId": str(uuid4()),
        "instanceVersion": "1.0.0",
        "rule": {
            "resourceType": "alert_rule",
            "resourceId": rule["ruleId"],
            "resourceVersion": rule["ruleVersion"],
        },
        "groupingId": str(uuid4()),
        "groupingFacets": [],
        "severity": "warning",
        "state": "firing",
        "evidenceState": "complete",
        "observedValue": 250,
        "unit": "milliseconds",
        "sampleSize": 2,
        "evidence": [],
        "firstObservedAt": NOW,
        "lastObservedAt": NOW,
        "occurrenceCount": 1,
        "suppressed": False, "acknowledged": False,
    }
    await repository.create_resource("alert_instance", instance)
    await asyncio.gather(
        service.acknowledge(
            str(instance["instanceId"]),
            {"instanceVersion": "1.0.0", "reason": "known_issue", "acknowledgedAt": NOW},
            command_id="ack-race-00000001",
        ),
        service.evaluate_once(now="2026-10-21T10:05:00Z"),
    )
    current = await repository.get_resource("alert_instance", str(instance["instanceId"]))
    assert current is not None
    assert current["acknowledged"] is True
    assert int(current["occurrenceCount"]) >= 1


def test_operations_resource_types_and_metrics_are_closed() -> None:
    assert set(OperationResourceType.__args__) == {
        "dashboard", "saved_query", "alert_rule", "alert_instance",
        "alert_acknowledgement", "alert_suppression", "incident", "annotation",
    }
    metrics = ObservatoryMetrics()
    repository = MemoryOperationsRepository(metrics=metrics)
    assert ObservatoryMetrics.LABELS["observatory_operations_recovery_count"] == {
        "operation",
        "outcome",
    }
    with pytest.raises(KeyError):
        # Runtime adapters must fail closed even though the public Python type is a Literal.
        asyncio.run(repository.create_resource("arbitrary_table", {}))


@pytest.mark.asyncio
async def test_scheduler_history_is_authoritative_and_failure_recovery_is_audited() -> None:
    metrics = ObservatoryMetrics()
    repository = MemoryOperationsRepository(metrics=metrics)
    rule = await AlertService(repository).create_rule(_rule_request())
    claimed = await repository.claim_due_alerts(now=NOW, owner="scheduler-a", lease_seconds=10)
    assert claimed and claimed[0]["leaseToken"]
    await repository.complete_alert_claim(
        str(rule["ruleId"]),
        now=NOW,
        success=False,
        owner="scheduler-a",
        lease_token=str(claimed[0]["leaseToken"]),
    )
    history = await repository.list_history(resource_id=str(rule["ruleId"]), limit=100)
    states = {item["state"] for item in history["items"]}
    assert {"lease_acquired", "lease_completed"} <= states
    assert any(item["change"] == "updated" for item in history["items"])
    names = {entry["name"] for entry in metrics.snapshot().values()}
    assert {
        "observatory_operations_recovery_count",
        "observatory_operations_backlog_count",
    } <= names


@pytest.mark.asyncio
async def test_operations_sse_reauth_disconnect_emits_bounded_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "owner-subject"
    issuer = "https://identity.example.invalid/application/o/aura-observatory/"

    class ReauthExpiry(ObservatoryAuth):
        reads = 0

        async def get_session(self, session_id: str | None):
            self.reads += 1
            if self.reads == 1:
                return await super().get_session(session_id)
            return None

    settings = Settings(owner_subject=owner)
    auth = ReauthExpiry(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _session = await auth.create_session(Principal(issuer, owner, "Owner"))
    app = create_app(settings, auth=auth, testing=True)
    values = iter((0.0, 0.0, 16.0, 16.0))
    monkeypatch.setattr(api_module, "monotonic", lambda: next(values))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    )
    async with client:
        response = await client.get("/api/v1/operations/events")
    assert response.status_code == 200
    assert response.text == ""
    snapshot = app.state.operations["metrics"].snapshot()
    assert any(
        entry["name"] == "observatory_sse_failure_count"
        and entry["labels"].get("event_type") == "reauth"
        for entry in snapshot.values()
    )


@pytest.mark.asyncio
async def test_operational_sse_catch_up_is_exclusive_and_bounded() -> None:
    repository = MemoryOperationsRepository()
    value = await SavedQueryService(repository).create(
        {"name": "Runs", "definition": _run_query(), "createdAt": NOW}
    )
    first = (await repository.events_after(None))[0]
    await AlertService(repository).create_rule(_rule_request())
    replay = await repository.events_after(first["eventId"])
    assert len(replay) == 1
    assert replay[0]["eventId"] != first["eventId"]
    assert replay[0]["capturePolicy"] == "metadata_only"
    assert await repository.events_after(replay[0]["eventId"]) == []
    assert value["queryId"] == first["resourceId"]


@pytest.mark.asyncio
async def test_operations_worker_recovers_after_one_evaluation_failure() -> None:
    stop = asyncio.Event()

    class FlakyService:
        calls = 0

        async def evaluate_once(self, *, limit: int) -> int:
            assert limit == 16
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient storage failure")
            stop.set()
            return 1

    service = FlakyService()
    await _run_operations_worker(service, SimpleNamespace(evaluation_poll_seconds=0), stop)
    assert service.calls == 2


def test_operations_migration_is_forward_only_and_rerunnable() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/postgresql/versions/0014_observatory_operations.sql"
    )
    sql = migration.read_text(encoding="utf-8").casefold()
    assert sql.count("create table if not exists") >= 3
    assert "create index if not exists" in sql
    assert "insert into observatory_migration_ledger" in sql
    assert "where not exists" in sql
    assert "drop table" not in sql
    assert "drop column" not in sql
    assert "prompt" not in sql
    assert "response" not in sql
    assert "credential" not in sql
