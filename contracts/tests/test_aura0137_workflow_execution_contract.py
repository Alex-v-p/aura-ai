"""AURA-0137 acceptance checks for the Aura v1 workflow execution surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/aura-v1.yaml"
WORKFLOW_EVENTS_PATH = ROOT / "contracts/events/automation/v1/workflow-events.schema.json"
GENERATED_PATH = ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _openapi() -> dict[str, Any]:
    return _json(OPENAPI_PATH)


def _operation(operation_id: str) -> dict[str, Any]:
    for item in _openapi()["paths"].values():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"} and operation.get(
                "operationId"
            ) == operation_id:
                return cast(dict[str, Any], operation)
    raise AssertionError(f"missing operation {operation_id}")


def _parameter_refs(operation: dict[str, Any]) -> set[str]:
    return {
        cast(str, parameter["$ref"])
        for parameter in cast(list[dict[str, Any]], operation.get("parameters", []))
        if "$ref" in parameter
    }


def test_aura_v1_additively_exposes_execution_retirement_and_stream() -> None:
    contract = _openapi()
    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["title"] == "Aura Core API"
    assert contract["info"]["version"] == "1.22.0"
    assert {
        "/api/v1/workflows/stream",
        "/api/v1/workflows/{workflow_id}/retire",
        "/api/v1/workflows/{workflow_id}/revisions/{revision_id}/retire",
    } <= set(contract["paths"])


def test_retirement_commands_are_owner_mutations_with_optimistic_versions() -> None:
    contract = _openapi()
    retirement = {
        "retireWorkflowDefinition": "WorkflowDefinition",
        "retireWorkflowRevision": "WorkflowRevision",
    }
    for operation_id, response_schema in retirement.items():
        operation = _operation(operation_id)
        assert {
            "#/components/parameters/CsrfToken",
            "#/components/parameters/IdempotencyKey",
        } <= _parameter_refs(operation)
        assert operation["requestBody"] == {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/WorkflowVersionCommand"}
                }
            },
        }
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{response_schema}"
        }
        assert {"401", "403", "404", "409"} <= set(operation["responses"])
        assert "503" not in operation["responses"]

    version_command = contract["components"]["schemas"]["WorkflowVersionCommand"]
    assert version_command["additionalProperties"] is False
    assert version_command["required"] == ["expectedVersion"]
    assert version_command["properties"]["expectedVersion"] == {
        "type": "integer",
        "minimum": 1,
    }


def test_revision_exposes_additive_lifecycle_and_concurrency_metadata() -> None:
    schemas = _openapi()["components"]["schemas"]
    revision = schemas["WorkflowRevision"]
    assert {"status", "version", "retiredAt"} <= set(revision["required"])
    assert revision["properties"]["status"] == {
        "$ref": "#/components/schemas/WorkflowRevisionStatus"
    }
    assert schemas["WorkflowRevisionStatus"]["enum"] == ["published", "retired"]
    assert revision["properties"]["version"] == {"type": "integer", "minimum": 1}
    assert revision["properties"]["retiredAt"] == {
        "type": ["string", "null"],
        "format": "date-time",
    }
    assert revision["additionalProperties"] is False


def test_feature_flag_disables_only_new_publication_and_admission() -> None:
    gated = {"publishWorkflowDraft", "startWorkflowInstance"}
    workflow_mutations: dict[str, dict[str, Any]] = {}
    for path, item in _openapi()["paths"].items():
        if not path.startswith("/api/v1/workflows"):
            continue
        for method, operation in item.items():
            if method in {"post", "put", "patch", "delete"}:
                workflow_mutations[operation["operationId"]] = operation

    assert all("503" in workflow_mutations[operation_id]["responses"] for operation_id in gated)
    assert "publication is disabled" in workflow_mutations["publishWorkflowDraft"]["responses"][
        "503"
    ]["description"]
    assert "admission is disabled" in workflow_mutations["startWorkflowInstance"]["responses"][
        "503"
    ]["description"]
    for operation_id in gated:
        assert workflow_mutations[operation_id]["responses"]["503"]["content"][
            "application/problem+json"
        ]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert all(
        "503" not in operation["responses"]
        for operation_id, operation in workflow_mutations.items()
        if operation_id not in gated
    )
def test_workflow_stream_has_persisted_last_event_id_replay_contract() -> None:
    stream = _operation("streamWorkflowEvents")
    assert stream["parameters"] == [
        {
            "name": "Last-Event-ID",
            "in": "header",
            "required": False,
            "schema": {"type": "string", "format": "uuid"},
            "description": (
                "Last fully processed owner-authorized persisted workflow lifecycle event. "
                "Replay resumes strictly after this event."
            ),
        }
    ]
    response = stream["responses"]["200"]
    assert response["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/WorkflowStreamEvent"
    }
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert response["headers"]["X-Accel-Buffering"]["schema"]["const"] == "no"
    assert {"400", "401", "403", "404", "410", "503"} <= set(stream["responses"])
    assert "owner-authorized" in stream["responses"]["404"]["description"]
    assert "reconcile" in stream["responses"]["410"]["description"]


def test_stream_payloads_match_persisted_event_families_and_are_metadata_only() -> None:
    schemas = _openapi()["components"]["schemas"]
    persisted = _json(WORKFLOW_EVENTS_PATH)
    expected = {
        "WorkflowDefinitionLifecycleEvent",
        "WorkflowInstanceLifecycleEvent",
        "WorkflowStepLifecycleEvent",
        "WorkflowRetentionLifecycleEvent",
    }
    refs = {
        item["$ref"].rsplit("/", 1)[1]
        for item in schemas["WorkflowStreamEvent"]["oneOf"]
    }
    assert refs == expected | {"WorkflowHeartbeatEvent"}
    assert {
        item["$ref"].rsplit("/", 1)[1] for item in persisted["oneOf"]
    } == expected
    for name in expected:
        assert schemas[name]["additionalProperties"] is False
        assert set(schemas[name]["required"]) == set(persisted["$defs"][name]["required"])
        assert set(schemas[name]["properties"]) == set(persisted["$defs"][name]["properties"])

    heartbeat = schemas["WorkflowHeartbeatEvent"]
    assert heartbeat["additionalProperties"] is False
    assert "eventId" not in heartbeat["properties"]
    serialized = json.dumps(
        {name: schemas[name] for name in refs}, separators=(",", ":")
    ).lower()
    for forbidden in (
        "displayname",
        "instructions",
        "inputvalue",
        "outputvalue",
        "approvalpresentation",
        "notificationcontent",
        "ownerid",
        "credential",
        "executorstate",
        "prompt",
        "response",
        "hiddenreasoning",
    ):
        assert f'"{forbidden}"' not in serialized


def test_revision_retirement_event_requires_exact_metadata_only_revision_lineage() -> None:
    persisted = _json(WORKFLOW_EVENTS_PATH)["$defs"]["WorkflowDefinitionLifecycleEvent"]
    openapi = _openapi()["components"]["schemas"]["WorkflowDefinitionLifecycleEvent"]
    expected_types = [
        "workflow.revision.published",
        "workflow.revision.retired",
        "workflow.definition.retired",
    ]
    assert persisted["properties"]["eventType"]["enum"] == expected_types
    assert openapi["properties"]["eventType"]["enum"] == expected_types

    persisted_revision, persisted_definition = persisted["oneOf"]
    assert persisted_revision["required"] == ["eventType", "revisionId"]
    assert persisted_revision["properties"]["eventType"]["enum"] == expected_types[:2]
    assert persisted_revision["properties"]["revisionId"] == {"$ref": "#/$defs/Uuid"}
    assert persisted_definition["properties"] == {
        "eventType": {"const": "workflow.definition.retired"},
        "revisionId": {"type": "null"},
    }

    openapi_revision, openapi_definition = openapi["oneOf"]
    assert openapi_revision["required"] == ["eventType", "revisionId"]
    assert openapi_revision["properties"]["eventType"]["enum"] == expected_types[:2]
    assert openapi_revision["properties"]["revisionId"] == {
        "type": "string",
        "format": "uuid",
    }
    assert openapi_definition["properties"] == {
        "eventType": {"type": "string", "const": "workflow.definition.retired"},
        "revisionId": {"type": "null"},
    }


def test_pending_step_has_no_execution_identity_and_started_steps_require_uuid() -> None:
    schemas = _openapi()["components"]["schemas"]
    step = schemas["WorkflowStepState"]
    assert "stepExecutionId" not in step["required"]
    assert step["properties"]["stepExecutionId"] == {
        "type": "string",
        "format": "uuid",
    }

    pending, started = step["oneOf"]
    assert pending == {
        "required": ["status"],
        "properties": {"status": {"type": "string", "const": "pending"}},
    }
    assert started["required"] == ["status", "stepExecutionId"]
    assert started["properties"]["stepExecutionId"] == {
        "type": "string",
        "format": "uuid",
    }
    assert started["properties"]["status"]["enum"] == [
        status for status in schemas["WorkflowStepStatus"]["enum"] if status != "pending"
    ]

    persisted_step_event = _json(WORKFLOW_EVENTS_PATH)["$defs"][
        "WorkflowStepLifecycleEvent"
    ]
    assert "stepExecutionId" in persisted_step_event["required"]
    assert persisted_step_event["properties"]["stepExecutionId"] == {
        "$ref": "#/$defs/Uuid"
    }


def test_generated_client_contains_transport_only_execution_additions() -> None:
    generated = GENERATED_PATH.read_text()
    assert "export type WorkflowRevisionStatus = \"published\" | \"retired\";" in generated
    revision = next(
        line for line in generated.splitlines() if line.startswith("export type WorkflowRevision =")
    )
    assert 'readonly "status": WorkflowRevisionStatus' in revision
    assert 'readonly "version": number' in revision
    assert 'readonly "retiredAt": string | null' in revision
    assert "export type WorkflowStreamEvent =" in generated
    lifecycle_event = next(
        line
        for line in generated.splitlines()
        if line.startswith("export type WorkflowDefinitionLifecycleEvent =")
    )
    assert '"workflow.revision.retired"' in lifecycle_event
    assert 'readonly "revisionId": string | null' in lifecycle_event
    step_state = next(
        line
        for line in generated.splitlines()
        if line.startswith("export type WorkflowStepState =")
    )
    assert 'readonly "stepExecutionId"?: string' in step_state
    for operation_id in (
        "retireWorkflowDefinition",
        "retireWorkflowRevision",
        "streamWorkflowEvents",
    ):
        assert f"readonly {operation_id}:" in generated
        assert f"{operation_id}: {{" in generated
    assert (
        "streamWorkflowEvents: { method: 'GET', pathTemplate: "
        "'/api/v1/workflows/stream', requestMode: 'none', responseMode: 'event-stream' }"
    ) in generated
