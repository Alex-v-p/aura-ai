"""AURA-0087 bounded operational analytics contract regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[3]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
GENERATED_PATH = (
    ROOT
    / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)


def _contract() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _parameters(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    document = _contract()
    resolved: dict[str, dict[str, Any]] = {}
    for parameter in operation.get("parameters", []):
        if "$ref" in parameter:
            name = parameter["$ref"].rsplit("/", maxsplit=1)[-1]
            parameter = document["components"]["parameters"][name]
        resolved[parameter["name"]] = parameter
    return resolved


def test_analytics_resources_are_additive_owner_only_snapshots() -> None:
    document = _contract()
    paths = document["paths"]

    assert document["info"]["x-contract-id"] == "observatory-v1"
    assert document["info"]["version"] == "1.0.0"
    assert document["security"] == [{"observatorySession": []}]
    assert paths["/api/v1/analytics/metrics"]["get"]["operationId"] == (
        "listAnalyticsMetrics"
    )
    query = paths["/api/v1/analytics/query"]["get"]
    assert query["operationId"] == "queryOperationalAnalytics"
    assert query["responses"]["200"]["headers"]["Cache-Control"]["schema"] == {
        "type": "string",
        "const": "no-store",
    }
    assert {"400", "401", "403", "422", "503"} <= set(query["responses"])


def test_query_is_finite_and_uses_only_metadata_observation_fields() -> None:
    operation = _contract()["paths"]["/api/v1/analytics/query"]["get"]
    parameters = _parameters(operation)

    assert parameters["metric"]["required"] is True
    assert parameters["unit"]["required"] is True
    assert parameters["series_limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 12,
        "default": 12,
    }
    assert parameters["point_limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 200,
        "default": 200,
    }
    assert set(parameters) == {
        "metric",
        "unit",
        "aggregation",
        "focus",
        "from",
        "to",
        "bucket",
        "group_by",
        "component",
        "component_version",
        "operation",
        "deployment",
        "environment",
        "host",
        "service_role",
        "provider",
        "outcome",
        "error_class",
        "status",
        "retrieval_stage",
        "series_limit",
        "point_limit",
    }
    forbidden = {
        "model",
        "agent",
        "conversation",
        "run",
        "trace",
        "prompt",
        "response",
        "content",
        "filename",
        "argument",
        "result",
        "credential",
        "endpoint",
        "token",
        "owner",
        "attribute",
    }
    assert not forbidden.intersection(parameters)


def test_metric_catalogue_and_snapshot_make_bounds_and_semantics_explicit() -> None:
    schemas = _contract()["components"]["schemas"]
    names = schemas["AnalyticsMetricName"]["enum"]
    units = schemas["AnalyticsUnit"]["enum"]
    groupings = schemas["AnalyticsGroupBy"]["enum"]
    limits = schemas["AnalyticsLimits"]["properties"]
    snapshot = schemas["OperationalAnalyticsSnapshot"]

    assert len(names) == len(set(names))
    assert set(units) == {"count", "milliseconds", "bytes", "ratio", "score"}
    assert set(groupings) == {
        "none",
        "component",
        "component_version",
        "operation",
        "deployment",
        "environment",
        "host",
        "service_role",
        "provider",
        "outcome",
        "error_class",
        "status",
        "retrieval_stage",
    }
    assert limits["defaultWindowHours"]["const"] == 24
    assert limits["maximumWindowDays"]["const"] == 31
    assert limits["maximumPointsPerSeries"]["const"] == 200
    assert limits["maximumSeries"]["const"] == 12
    assert limits["maximumScannedRows"]["const"] == 1_000_000
    assert limits["executionTimeoutMs"]["const"] == 5_000
    assert schemas["AnalyticsSeries"]["properties"]["points"]["maxItems"] == 200
    assert snapshot["properties"]["series"]["maxItems"] == 12
    assert snapshot["properties"]["breakdowns"]["maxItems"] == 50
    assert snapshot["properties"]["capturePolicy"]["const"] == "metadata_only"
    assert schemas["AnalyticsPercentileMethod"]["const"] == (
        "clickhouse_quantile_exact"
    )
    summary_required = set(schemas["AnalyticsSeriesSummary"]["required"])
    assert {"sampleCount", "p50", "p90", "p95", "p99"} <= summary_required
    assert {"ratePerInterval", "rateIntervalSeconds"} <= summary_required


def test_unavailable_and_partial_states_are_closed_not_inferred() -> None:
    schemas = _contract()["components"]["schemas"]

    assert schemas["AnalyticsAvailability"]["enum"] == [
        "available",
        "no_data",
        "unavailable",
    ]
    unavailable = set(schemas["AnalyticsUnavailableSignal"]["enum"])
    assert {
        "model_identity",
        "retryability",
        "input_cohort",
        "gpu",
        "selected_evidence",
        "side_effect_detail",
        "quality",
        "saturation",
    } <= unavailable
    assert set(schemas["AnalyticsPartialReason"]["enum"]) == {
        "scan_limit",
        "execution_limit",
        "series_limit",
        "point_limit",
        "late_data",
        "retention_boundary",
    }
    for name in (
        "AnalyticsMetricDefinition",
        "AnalyticsSeriesFacets",
        "AnalyticsPoint",
        "AnalyticsSeriesSummary",
        "AnalyticsSeries",
        "AnalyticsBreakdown",
        "AnalyticsFreshness",
        "AnalyticsEffectiveQuery",
        "AnalyticsScan",
        "OperationalAnalyticsSnapshot",
    ):
        assert schemas[name]["additionalProperties"] is False


def test_contract_and_generated_transport_exclude_sensitive_analytics_fields() -> None:
    document = _contract()
    analytics = {
        name: schema
        for name, schema in document["components"]["schemas"].items()
        if name.startswith("Analytics") or name == "OperationalAnalyticsSnapshot"
    }
    serialized = json.dumps(analytics).lower()
    forbidden_properties = {
        "prompt",
        "response",
        "documentcontent",
        "filename",
        "toolarguments",
        "toolresults",
        "credential",
        "bearertoken",
        "owneridentity",
        "exceptionmessage",
        "stacktrace",
        "arbitraryattributes",
    }
    assert not {value for value in forbidden_properties if f'"{value}"' in serialized}

    generated = GENERATED_PATH.read_text(encoding="utf-8")
    assert "readonly listAnalyticsMetrics" in generated
    assert "readonly queryOperationalAnalytics" in generated
    assert "pathTemplate: '/api/v1/analytics/query'" in generated
    assert "fetch(" not in generated
    assert "localStorage" not in generated
    assert "sessionStorage" not in generated
