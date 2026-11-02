"""AURA-0097 dashboard, alert, incident, and annotation contract invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"


def _document() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


def _resolve_parameter(document: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    reference = item.get("$ref")
    if reference is None:
        return item
    return document["components"]["parameters"][reference.rsplit("/", 1)[-1]]


def test_aura0097_adds_complete_owner_only_operations_surface() -> None:
    document = _document()
    operations = _operations(document)
    expected = {
        "listDashboardPresets",
        "listDashboards",
        "createDashboard",
        "getDashboard",
        "deleteDashboard",
        "listSavedQueries",
        "createSavedQuery",
        "getSavedQuery",
        "executeSavedQuery",
        "deleteSavedQuery",
        "listAlertRules",
        "createAlertRule",
        "getAlertRule",
        "changeAlertRuleState",
        "listAlertInstances",
        "getAlertInstance",
        "acknowledgeAlertInstance",
        "listAlertSuppressions",
        "createAlertSuppression",
        "clearAlertSuppression",
        "listIncidents",
        "createIncident",
        "getIncident",
        "transitionIncident",
        "listAnnotations",
        "createAnnotation",
        "getAnnotation",
        "deleteAnnotation",
        "listOperationsHistory",
        "streamOperationsEvents",
    }
    assert expected <= operations.keys()

    for operation_id in expected:
        operation = operations[operation_id]
        assert operation.get("security", [{"observatorySession": []}]) == [
            {"observatorySession": []}
        ]
        assert {"401", "403"} <= operation["responses"].keys()


def test_all_lists_are_cursor_bounded_and_snapshot_stable() -> None:
    document = _document()
    operations = _operations(document)
    schemas = document["components"]["schemas"]
    list_operations = {
        "listDashboardPresets": "DashboardPresetPage",
        "listDashboards": "DashboardPage",
        "listSavedQueries": "SavedQueryPage",
        "listAlertRules": "AlertRulePage",
        "listAlertInstances": "AlertInstancePage",
        "listAlertSuppressions": "AlertSuppressionPage",
        "listIncidents": "IncidentPage",
        "listAnnotations": "AnnotationPage",
        "listOperationsHistory": "OperationsHistoryPage",
    }
    for operation_id, page_name in list_operations.items():
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id].get("parameters", [])
        ]
        names = {item["name"] for item in parameters}
        assert {"cursor", "page_size"} <= names, operation_id
        page_size = next(item for item in parameters if item["name"] == "page_size")
        assert page_size["schema"]["maximum"] == 200
        page = schemas[page_name]
        assert page["additionalProperties"] is False
        assert {"items", "nextCursor", "snapshotAt", "partial"} <= set(
            page["required"]
        )
        assert page["properties"]["items"]["maxItems"] == 200


def test_commands_require_csrf_idempotency_and_closed_confirmations() -> None:
    document = _document()
    operations = _operations(document)
    commands = {
        "createDashboard": None,
        "deleteDashboard": {"delete-dashboard"},
        "createSavedQuery": None,
        "deleteSavedQuery": {"delete-saved-query"},
        "createAlertRule": None,
        "changeAlertRuleState": {"change-alert-rule-state"},
        "acknowledgeAlertInstance": None,
        "createAlertSuppression": None,
        "clearAlertSuppression": {"clear-suppression"},
        "createIncident": None,
        "transitionIncident": {"transition-incident"},
        "createAnnotation": None,
        "deleteAnnotation": {"delete-annotation"},
    }
    for operation_id, expected_confirmation in commands.items():
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id]["parameters"]
        ]
        headers = {item["name"]: item for item in parameters if item["in"] == "header"}
        assert {"X-Observatory-CSRF", "Idempotency-Key"} <= headers.keys()
        assert headers["X-Observatory-CSRF"]["required"] is True
        assert headers["Idempotency-Key"]["required"] is True
        if expected_confirmation is not None:
            confirmation = headers["X-Observatory-Confirmation"]["schema"]
            values = set(confirmation.get("enum", [confirmation.get("const")]))
            assert expected_confirmation <= values
        assert {"409", "422", "503"} <= operations[operation_id]["responses"].keys()


def test_saved_queries_are_closed_finite_unions_without_expression_escape_hatches() -> None:
    schemas = _document()["components"]["schemas"]
    union = schemas["SavedQueryDefinition"]
    members = {item["$ref"].rsplit("/", 1)[-1] for item in union["oneOf"]}
    assert members == {
        "AnalyticsSavedQuery",
        "RunSearchSavedQuery",
        "TraceSearchSavedQuery",
        "RunComparisonSavedQuery",
        "RegressionSearchSavedQuery",
        "ComponentHealthSavedQuery",
    }
    forbidden = {
        "sql",
        "query",
        "expression",
        "attributes",
        "dimensions",
        "filters",
        "url",
        "endpoint",
        "credential",
        "token",
        "owner",
        "content",
    }
    for name in members:
        schema = schemas[name]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert forbidden.isdisjoint(schema["properties"]), name
    assert schemas["AnalyticsSavedQuery"]["properties"]["seriesLimit"]["maximum"] == 12
    assert schemas["AnalyticsSavedQuery"]["properties"]["pointLimit"]["maximum"] == 200
    assert schemas["OperationalWindow"]["enum"][-1] == "thirty_one_days"


def test_saved_query_definition_keys_and_enums_are_exact() -> None:
    schemas = _document()["components"]["schemas"]
    expected_required = {
        "AnalyticsSavedQuery": {
            "kind",
            "metric",
            "unit",
            "aggregation",
            "window",
            "bucket",
            "groupBy",
            "focus",
            "facets",
            "seriesLimit",
            "pointLimit",
        },
        "RunSearchSavedQuery": {
            "kind",
            "window",
            "statuses",
            "components",
            "deployments",
            "models",
            "pageSize",
        },
        "TraceSearchSavedQuery": {
            "kind",
            "window",
            "components",
            "outcomes",
            "pageSize",
        },
        "RunComparisonSavedQuery": {
            "kind",
            "baselineRunId",
            "candidateRunId",
        },
        "RegressionSearchSavedQuery": {
            "kind",
            "window",
            "outcomes",
            "experimentReferences",
            "pageSize",
        },
        "ComponentHealthSavedQuery": {
            "kind",
            "componentId",
            "componentVersion",
            "window",
            "pointLimit",
        },
    }
    for name, keys in expected_required.items():
        assert set(schemas[name]["required"]) == keys
        assert set(schemas[name]["properties"]) == keys

    assert schemas["OperationalFacetName"]["enum"] == [
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
        "model",
        "regression_outcome",
    ]
    regression = schemas["RegressionSearchSavedQuery"]
    assert regression["properties"]["experimentReferences"] == {
        "type": "array",
        "maxItems": 32,
        "uniqueItems": True,
        "items": {"$ref": "#/components/schemas/EvaluationResourceReference"},
    }
    reference = schemas["EvaluationResourceReference"]
    assert reference["required"] == ["resourceId", "resourceVersion"]
    assert set(reference["properties"]) == {"resourceId", "resourceVersion"}


def test_exact_saved_query_execution_is_read_only_closed_and_no_store() -> None:
    document = _document()
    path = "/api/v1/saved-queries/{query_id}/versions/{resource_version}/execute"
    assert set(document["paths"][path]) == {"get"}
    operation = document["paths"][path]["get"]
    assert operation["operationId"] == "executeSavedQuery"
    parameters = [
        _resolve_parameter(document, item) for item in operation["parameters"]
    ]
    assert [(item["name"], item["in"]) for item in parameters] == [
        ("query_id", "path"),
        ("resource_version", "path"),
        ("cursor", "query"),
    ]
    assert "requestBody" not in operation
    assert {"400", "401", "403", "404", "422", "503"} <= set(
        operation["responses"]
    )
    response = operation["responses"]["200"]
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SavedQueryExecutionResult"
    }


def test_saved_query_execution_result_is_closed_bounded_and_discriminated() -> None:
    schemas = _document()["components"]["schemas"]
    expected = {
        "analytics": ("AnalyticsSavedQueryExecution", "OperationalAnalyticsSnapshot"),
        "run_search": ("RunSearchSavedQueryExecution", "RunPage"),
        "trace_search": ("TraceSearchSavedQueryExecution", "RunPage"),
        "run_comparison": ("RunComparisonSavedQueryExecution", "RunComparison"),
        "regression_search": (
            "RegressionSearchSavedQueryExecution",
            "EvaluationRegressionPage",
        ),
        "component_health": ("ComponentHealthSavedQueryExecution", "ComponentHealth"),
    }
    members = {
        item["$ref"].rsplit("/", 1)[-1]
        for item in schemas["SavedQueryExecutionResult"]["oneOf"]
    }
    assert members == {name for name, _ in expected.values()}
    assert schemas["SavedQueryExecutionState"]["enum"] == [
        "complete",
        "partial",
        "unavailable",
        "incompatible",
    ]
    assert schemas["SavedQueryFreshnessState"]["enum"] == [
        "fresh",
        "stale",
        "unavailable",
    ]
    freshness = schemas["SavedQueryExecutionFreshness"]
    assert freshness["additionalProperties"] is False
    assert set(freshness["required"]) == {"state", "measuredAt", "lagMs"}
    assert freshness["properties"]["lagMs"]["maximum"] == 604800000

    for kind, (name, result_name) in expected.items():
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["kind"] == {"type": "string", "const": kind}
        assert {
            "kind",
            "query",
            "state",
            "partial",
            "snapshotAt",
            "freshness",
            "result",
        } <= set(schema["required"])
        assert {"$ref": f"#/components/schemas/{result_name}"} in schema[
            "properties"
        ]["result"]["oneOf"]
        assert {"type": "null"} in schema["properties"]["result"]["oneOf"]

    for name in (
        "RunSearchSavedQueryExecution",
        "TraceSearchSavedQueryExecution",
        "RegressionSearchSavedQueryExecution",
    ):
        assert "nextCursor" in schemas[name]["required"]
        assert schemas[name]["properties"]["nextCursor"]["oneOf"][-1] == {
            "type": "null"
        }


def test_dashboards_presets_and_alerts_are_bounded_and_exactly_versioned() -> None:
    schemas = _document()["components"]["schemas"]
    for name in (
        "SavedQueryReference",
        "DashboardPanel",
        "DashboardPresetPanel",
        "DashboardPreset",
        "Dashboard",
        "AlertCondition",
        "AlertRule",
        "AlertEvidenceReference",
        "AlertInstance",
        "AlertAcknowledgement",
        "AlertSuppression",
    ):
        assert schemas[name]["additionalProperties"] is False, name

    assert schemas["Dashboard"]["properties"]["panels"]["maxItems"] == 24
    assert schemas["DashboardPreset"]["properties"]["immutable"]["const"] is True
    group_by = schemas["AlertCondition"]["properties"]["groupBy"]
    assert group_by["maxItems"] == 1
    assert group_by["uniqueItems"] is True
    assert group_by["items"] == {
        "$ref": "#/components/schemas/OperationalFacetName"
    }
    assert schemas["AlertCondition"]["properties"]["query"] == {
        "$ref": "#/components/schemas/SavedQueryReference"
    }
    assert schemas["AlertInstance"]["properties"]["evidence"]["maxItems"] == 128
    assert schemas["AlertMissingEvidencePolicy"]["enum"] == [
        "no_data",
        "inconclusive",
        "firing",
    ]


def test_incidents_annotations_history_and_sse_preserve_content_boundary() -> None:
    schemas = _document()["components"]["schemas"]
    assert schemas["IncidentState"]["enum"] == [
        "open",
        "investigating",
        "mitigated",
        "resolved",
        "closed",
    ]
    assert schemas["AnnotationTargetType"]["enum"] == [
        "run",
        "deployment",
        "experiment",
        "regression",
        "incident",
    ]
    assert schemas["AnnotationText"]["maxLength"] == 2000
    assert schemas["AnnotationTargetReference"]["required"] == [
        "targetType",
        "targetId",
        "targetVersion",
    ]

    for name in (
        "Incident",
        "Annotation",
        "OperationsHistoryEntry",
        "OperationalLiveEvent",
    ):
        assert schemas[name]["additionalProperties"] is False

    history_properties = schemas["OperationsHistoryEntry"]["properties"]
    live_properties = schemas["OperationalLiveEvent"]["properties"]
    forbidden = {
        "text",
        "title",
        "description",
        "resolutionNote",
        "query",
        "evidence",
        "groupingFacets",
        "owner",
        "identity",
        "credential",
        "url",
    }
    assert forbidden.isdisjoint(history_properties)
    assert forbidden.isdisjoint(live_properties)
    assert live_properties["eventType"]["const"] == "operations.changed"
    assert live_properties["capturePolicy"]["const"] == "metadata_only"


def test_operational_sse_is_persisted_same_origin_metadata_only() -> None:
    document = _document()
    stream = document["paths"]["/api/v1/operations/events"]["get"]
    assert stream["operationId"] == "streamOperationsEvents"
    assert stream["parameters"] == [{"$ref": "#/components/parameters/LastEventId"}]
    response = stream["responses"]["200"]
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert response["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/OperationalLiveEvent"
    }
    description = stream["description"].lower()
    assert "metadata-only" in description
    assert "annotation text" in description


def test_openapi_local_references_resolve_and_operation_ids_are_unique() -> None:
    document = _document()
    operation_ids = [
        operation["operationId"]
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(operation_ids) == len(set(operation_ids))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/components/"):
                current: Any = document
                for segment in reference.removeprefix("#/").split("/"):
                    current = current[segment]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
