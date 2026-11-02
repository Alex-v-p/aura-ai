"""Contract-level acceptance checks for the AURA-0137 workflow API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

OPENAPI = Path(__file__).parents[3] / "contracts" / "openapi" / "aura-v1.yaml"


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


def test_workflow_api_exposes_bounded_management_and_runtime_operations(
    document: dict[str, Any],
) -> None:
    paths = document["paths"]
    expected = {
        "/api/v1/workflows",
        "/api/v1/workflows/stream",
        "/api/v1/workflows/{workflow_id}",
        "/api/v1/workflows/{workflow_id}/retire",
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/retire",
        "/api/v1/workflows/{workflow_id}/instances",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/cancel",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/resolve",
        "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/deletions",
        "/api/v1/workflows/{workflow_id}/deletions",
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/history-deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/result-deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/checkpoint-deletions",
    }
    assert expected <= set(paths)
    assert not any("schedule" in path.casefold() for path in paths if "workflow" in path)


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}/retire",
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/retire",
        "/api/v1/workflows/{workflow_id}/instances",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/cancel",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/resolve",
        "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/deletions",
        "/api/v1/workflows/{workflow_id}/deletions",
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/history-deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/result-deletions",
        "/api/v1/workflows/{workflow_id}/instances/{instance_id}/checkpoint-deletions",
    ),
)
def test_workflow_mutations_require_csrf_and_idempotency_headers(
    document: dict[str, Any], path: str
) -> None:
    operation = document["paths"][path]["post"]
    parameter_refs = {
        parameter["$ref"]
        for parameter in operation.get("parameters", [])
        if "$ref" in parameter
    }
    assert "#/components/parameters/CsrfToken" in parameter_refs
    assert "#/components/parameters/IdempotencyKey" in parameter_refs


def test_workflow_stream_is_persisted_replay_with_metadata_only_events(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/v1/workflows/stream"]["get"]
    assert operation["operationId"] == "streamWorkflowEvents"
    assert any(
        parameter.get("name") == "Last-Event-ID"
        and parameter.get("in") == "header"
        for parameter in operation.get("parameters", [])
    )
    response = operation["responses"]["200"]
    headers = response["headers"]
    assert headers["Cache-Control"]["schema"]["const"] == "no-store"
    assert headers["X-Accel-Buffering"]["schema"]["const"] == "no"
    content = response["content"]["text/event-stream"]
    schema = content["schema"]
    assert schema["$ref"].endswith("WorkflowStreamEvent")


def test_workflow_contract_keeps_private_fields_out_of_stream_and_summary_schemas(
    document: dict[str, Any],
) -> None:
    schemas = document["components"]["schemas"]
    stream = schemas["WorkflowStreamEvent"]
    assert "metadata-only" in stream.get("description", "").casefold()
    assert {
        "input", "output", "instructions", "mappings", "result", "owner", "subject",
    }.isdisjoint(stream.get("properties", {}))
    for variant in stream["oneOf"]:
        variant_schema = schemas[variant["$ref"].rsplit("/", 1)[-1]]
        assert variant_schema.get("additionalProperties") is False
        assert {
            "input", "output", "instructions", "mappings", "result", "owner", "subject",
        }.isdisjoint(variant_schema.get("properties", {}))

    summary = schemas["WorkflowInstanceSummary"]
    assert summary.get("additionalProperties") is False
    summary_properties = set(summary.get("properties", {}))
    assert not summary_properties & {"input", "output", "instructions", "mappings", "owner"}


def test_workflow_revision_and_instance_schemas_are_closed_and_versioned(
    document: dict[str, Any],
) -> None:
    schemas = document["components"]["schemas"]
    for name in ("WorkflowRevision", "WorkflowInstance"):
        schema = schemas[name]
        assert schema.get("additionalProperties") is False
        properties = schema["properties"]
        assert "version" in properties
    revision_status = schemas["WorkflowRevisionStatus"]
    assert set(revision_status["enum"]) >= {"published", "retired"}
    assert {
        "id", "workflowId", "revision", "displayName", "inputFields", "steps",
        "limits", "status", "version", "publishedAt", "retiredAt",
    } <= set(schemas["WorkflowRevision"]["required"])


def test_workflow_lifecycle_schema_covers_publish_retire_delete_and_settled_steps(
    document: dict[str, Any],
) -> None:
    schemas = document["components"]["schemas"]
    definition = schemas["WorkflowDefinitionLifecycleEvent"]
    assert set(definition["properties"]["eventType"]["enum"]) == {
        "workflow.revision.published", "workflow.revision.retired",
        "workflow.definition.retired",
    }
    retention = schemas["WorkflowRetentionLifecycleEvent"]
    assert retention["properties"]["eventType"]["const"] == "workflow.retention.deleted"
    assert {"deletionScope", "tombstoneRetained"} <= set(retention["required"])
    step = schemas["WorkflowStepLifecycleEvent"]
    assert {"stepExecutionId", "stepOrdinal", "stepStatus", "effectOutcome"} <= set(
        step["required"]
    )


def test_admission_flag_gates_publish_and_start_but_not_draft_creation_or_reads(
    document: dict[str, Any],
) -> None:
    paths = document["paths"]
    create_responses = paths["/api/v1/workflows"]["post"]["responses"]
    publish_responses = paths[
        "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/publish"
    ]["post"]["responses"]
    start_responses = paths["/api/v1/workflows/{workflow_id}/instances"]["post"]["responses"]
    assert "503" not in create_responses
    assert "503" in publish_responses
    assert "503" in start_responses
    assert "503" not in paths["/api/v1/workflows"]["get"]["responses"]


def test_retirement_returns_full_definition_or_revision_shape(
    document: dict[str, Any],
) -> None:
    paths = document["paths"]
    definition_response = paths["/api/v1/workflows/{workflow_id}/retire"]["post"][
        "responses"
    ]["200"]
    revision_response = paths[
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/retire"
    ]["post"]["responses"]["200"]
    assert definition_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "WorkflowDefinition"
    )
    assert revision_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "WorkflowRevision"
    )


def test_flagged_admission_is_a_core_setting_and_defaults_closed() -> None:
    from aura_core.platform.auth import Settings

    settings = Settings()
    assert settings.workflow_admission_enabled is False
