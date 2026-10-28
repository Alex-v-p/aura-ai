"""AURA-0139 workflow-monitoring transport contract invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
GENERATED_PATH = (
    ROOT
    / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)

WORKFLOW_OUTCOME_METRICS = [
    "workflow_transition_outcome",
    "workflow_command_outcome",
    "workflow_repository_outcome",
]
WORKFLOW_LATENCY_METRICS = [
    "workflow_transition_duration_ms",
    "workflow_command_duration_ms",
    "workflow_repository_duration_ms",
    "workflow_definition_validation_duration_ms",
    "workflow_agent_run_step_duration_ms",
    "workflow_approval_command_duration_ms",
    "workflow_notification_step_duration_ms",
    "workflow_wait_command_duration_ms",
    "workflow_wait_lag_ms",
    "workflow_checkpoint_persistence_duration_ms",
    "workflow_cancellation_duration_ms",
    "workflow_settlement_duration_ms",
]
WORKFLOW_METRICS = WORKFLOW_OUTCOME_METRICS + WORKFLOW_LATENCY_METRICS
WORKFLOW_COMPONENTS = [
    "aura.automation.workflow_state",
    "aura.automation.workflow_definition",
    "aura.automation.workflow_step.agent_run",
    "aura.automation.workflow_approval",
    "aura.automation.workflow_step.notification",
    "aura.automation.workflow_step.wait_until",
    "aura.automation.workflow_checkpoint",
    "aura.automation.workflow_cancellation",
    "aura.automation.workflow_settlement",
]
WORKFLOW_LIFECYCLE_OPERATIONS = [
    "workflow.transition",
    "workflow.command",
    "workflow.repository",
]
WORKFLOW_FAILURE_OPERATIONS = WORKFLOW_LIFECYCLE_OPERATIONS + [
    "execute",
    "validate",
    "resume",
    "persist",
    "cancel",
    "settle",
]
WORKFLOW_ERROR_CLASSES = [
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
]


def _document() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _accepts_property_value(
    schemas: dict[str, Any], schema_name: str, property_name: str, value: str
) -> bool:
    def accepts(property_schema: dict[str, Any]) -> bool:
        reference = property_schema.get("$ref")
        if reference is not None:
            return accepts(schemas[reference.rsplit("/", 1)[-1]])
        if "oneOf" in property_schema:
            return any(accepts(candidate) for candidate in property_schema["oneOf"])
        if "const" in property_schema:
            return value == property_schema["const"]
        return value in property_schema.get("enum", [])

    return accepts(schemas[schema_name]["properties"][property_name])


def test_monitoring_operation_is_additive_owner_only_and_no_store() -> None:
    document = _document()
    operation = document["paths"]["/api/v1/automation/workflows/monitoring"]["get"]

    assert document["info"]["x-contract-id"] == "observatory-v1"
    assert document["info"]["version"] == "1.0.0"
    assert operation["operationId"] == "getWorkflowMonitoring"
    assert operation.get("security", document["security"]) == [
        {"observatorySession": []}
    ]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "from",
        "to",
        "bucket",
        "series_limit",
        "point_limit",
    }
    assert operation["responses"]["200"]["headers"]["Cache-Control"]["schema"] == {
        "type": "string",
        "const": "no-store",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WorkflowMonitoringSnapshot"
    }
    assert {"400", "401", "403", "422", "503"} <= operation["responses"].keys()


def test_workflow_metric_allowlist_is_exact_and_scheduler_free() -> None:
    schemas = _document()["components"]["schemas"]

    assert schemas["WorkflowMetricName"]["enum"] == WORKFLOW_METRICS
    assert schemas["WorkflowOutcomeMetricName"]["enum"] == WORKFLOW_OUTCOME_METRICS
    assert schemas["WorkflowLatencyMetricName"]["enum"] == WORKFLOW_LATENCY_METRICS
    analytics_metrics = schemas["AnalyticsMetricName"]["enum"]
    assert all(metric in analytics_metrics for metric in WORKFLOW_METRICS)
    assert not any("scheduler" in metric for metric in WORKFLOW_METRICS)

    outcome_series = schemas["WorkflowOutcomeSeries"]["properties"]
    latency_series = schemas["WorkflowLatencySeries"]["properties"]
    assert outcome_series["unit"] == {"type": "string", "const": "count"}
    assert latency_series["unit"] == {
        "type": "string",
        "const": "milliseconds",
    }
    assert schemas["WorkflowLatencySummary"]["properties"]["unit"] == {
        "type": "string",
        "const": "milliseconds",
    }


def test_snapshot_is_closed_bounded_and_explicit_about_limits() -> None:
    schemas = _document()["components"]["schemas"]
    snapshot = schemas["WorkflowMonitoringSnapshot"]
    properties = snapshot["properties"]

    assert snapshot["additionalProperties"] is False
    assert set(snapshot["required"]) == set(properties)
    assert properties["lifecycleCounts"]["maxItems"] == 50
    assert properties["latencySummaries"]["maxItems"] == 12
    assert properties["series"]["maxItems"] == 12
    assert properties["series"]["items"] == {
        "$ref": "#/components/schemas/WorkflowMonitoringSeries"
    }
    assert properties["failureBreakdowns"]["maxItems"] == 50
    assert properties["recentCorrelations"]["maxItems"] == 25
    assert properties["capturePolicy"] == {"type": "string", "const": "metadata_only"}

    limits = schemas["WorkflowMonitoringLimits"]
    assert limits["additionalProperties"] is False
    assert set(limits["required"]) == set(limits["properties"])
    expected_constants = {
        "defaultWindowHours": 24,
        "maximumWindowDays": 31,
        "maximumScannedRows": 1_000_000,
        "maximumSeries": 12,
        "maximumPointsPerSeries": 200,
        "maximumFailureBreakdowns": 50,
        "maximumRecentCorrelations": 25,
        "executionLimitMs": 5_000,
    }
    for name, value in expected_constants.items():
        assert limits["properties"][name]["const"] == value
    assert {"truncatedByExecutionLimit", "truncatedByStorageLimit"} <= set(
        limits["properties"]
    )


def test_breakdowns_and_correlations_expose_only_bounded_metadata() -> None:
    schemas = _document()["components"]["schemas"]
    breakdown = schemas["WorkflowFailureBreakdown"]
    correlation = schemas["WorkflowRunTraceReference"]

    assert breakdown["additionalProperties"] is False
    assert set(breakdown["properties"]) == {
        "component",
        "operation",
        "outcome",
        "errorClass",
        "count",
        "partial",
    }
    assert correlation["additionalProperties"] is False
    assert correlation["properties"]["metric"] == {
        "type": "string",
        "const": "workflow_agent_run_step_duration_ms",
    }
    assert set(correlation["properties"]) == {
        "metric",
        "runId",
        "traceId",
        "observedAt",
        "component",
        "operation",
        "outcome",
        "errorClass",
    }
    assert correlation["properties"]["runId"] == {
        "$ref": "#/components/schemas/Uuid"
    }
    assert correlation["properties"]["traceId"] == {
        "$ref": "#/components/schemas/TraceId"
    }

    forbidden = {
        "workflowId",
        "definitionId",
        "revisionId",
        "instanceId",
        "stepId",
        "stepExecutionId",
        "eventId",
        "ownerId",
        "content",
        "input",
        "output",
        "checkpoint",
        "result",
        "message",
        "attributes",
    }
    workflow_schema_names = {
        name
        for name in schemas
        if name.startswith("Workflow")
    }
    represented_properties = {
        property_name
        for name in workflow_schema_names
        for property_name in schemas[name].get("properties", {})
    }
    assert forbidden.isdisjoint(represented_properties)


def test_breakdown_and_correlation_vocabularies_are_closed() -> None:
    document = _document()
    schemas = document["components"]["schemas"]

    assert schemas["WorkflowComponent"]["enum"] == WORKFLOW_COMPONENTS
    assert schemas["WorkflowLifecycleOperation"]["enum"] == (
        WORKFLOW_LIFECYCLE_OPERATIONS
    )
    assert schemas["WorkflowFailureOperation"]["enum"] == WORKFLOW_FAILURE_OPERATIONS
    assert schemas["WorkflowCorrelationOperation"] == {
        "type": "string",
        "const": "execute",
    }
    assert schemas["WorkflowOutcome"]["enum"] == [
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    ]
    assert schemas["WorkflowErrorClass"]["enum"] == WORKFLOW_ERROR_CLASSES
    lifecycle_properties = schemas["WorkflowLifecycleCount"]["properties"]
    assert lifecycle_properties["operation"] == {
        "$ref": "#/components/schemas/WorkflowLifecycleOperation"
    }
    assert lifecycle_properties["outcome"] == {
        "$ref": "#/components/schemas/WorkflowOutcome"
    }

    failure = {
        "component": "aura.automation.workflow_state",
        "operation": "workflow.transition",
        "outcome": "failed",
        "errorClass": "database_programming",
        "count": 2,
        "partial": False,
    }
    correlation = {
        "metric": "workflow_agent_run_step_duration_ms",
        "runId": "6f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a01",
        "traceId": "0123456789abcdef0123456789abcdef",
        "observedAt": "2026-10-28T08:00:00Z",
        "component": "aura.automation.workflow_step.agent_run",
        "operation": "execute",
        "outcome": "failed",
        "errorClass": "provider_protocol",
    }
    for name in ("component", "operation", "outcome", "errorClass"):
        assert _accepts_property_value(
            schemas, "WorkflowFailureBreakdown", name, failure[name]
        )
        assert _accepts_property_value(
            schemas, "WorkflowRunTraceReference", name, correlation[name]
        )
    for operation in WORKFLOW_FAILURE_OPERATIONS:
        assert _accepts_property_value(
            schemas, "WorkflowFailureBreakdown", "operation", operation
        )
    for error_class in WORKFLOW_ERROR_CLASSES:
        assert _accepts_property_value(
            schemas, "WorkflowFailureBreakdown", "errorClass", error_class
        )
    for operation in WORKFLOW_LIFECYCLE_OPERATIONS:
        assert _accepts_property_value(
            schemas, "WorkflowLifecycleCount", "operation", operation
        )
    assert not _accepts_property_value(
        schemas, "WorkflowLifecycleCount", "operation", "execute"
    )

    for name, invalid in {
        "component": "aura.automation.workflow_attacker",
        "operation": "workflow.arbitrary",
        "outcome": "rejected",
        "errorClass": "raw_exception_message",
    }.items():
        assert not _accepts_property_value(
            schemas, "WorkflowFailureBreakdown", name, invalid
        )
    for name, invalid in {
        "component": "aura.workflow.core",
        "operation": "workflow.agent_run",
        "outcome": "timeout",
        "errorClass": "arbitrary_provider_payload",
    }.items():
        assert not _accepts_property_value(
            schemas, "WorkflowRunTraceReference", name, invalid
        )


def test_generated_client_exports_monitoring_operation_and_closed_types() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")

    assert "export type WorkflowMonitoringSnapshot" in generated
    assert "export type WorkflowRunTraceReference" in generated
    assert "readonly getWorkflowMonitoring:" in generated
    operation_descriptor = (
        "getWorkflowMonitoring: { method: 'GET', "
        "pathTemplate: '/api/v1/automation/workflows/monitoring'"
    )
    assert operation_descriptor in generated
    for metric in WORKFLOW_METRICS:
        assert json.dumps(metric) in generated
