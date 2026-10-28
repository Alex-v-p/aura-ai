"""Acceptance checks for Aura v1.22 bounded manual-workflow contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/aura-v1.yaml"
WORKFLOW_EVENTS_PATH = ROOT / "contracts/events/automation/v1/workflow-events.schema.json"
NOTIFICATION_EVENTS_PATH = ROOT / "contracts/events/automation/v1/notification-events.schema.json"
RUN_EVENTS_PATH = ROOT / "contracts/events/runs/v1/run-events.schema.json"
GENERATED_PATH = ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"

WORKFLOW_PATHS = {
    "/api/v1/workflows",
    "/api/v1/workflows/{workflow_id}",
    "/api/v1/workflows/{workflow_id}/drafts",
    "/api/v1/workflows/{workflow_id}/drafts/{draft_id}",
    "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/validate",
    "/api/v1/workflows/{workflow_id}/drafts/{draft_id}/publish",
    "/api/v1/workflows/{workflow_id}/revisions/{revision_id}",
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


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _openapi() -> dict[str, Any]:
    return _json(OPENAPI_PATH)


def _mutation_refs(operation: dict[str, Any]) -> set[str]:
    return {
        cast(str, parameter["$ref"])
        for parameter in cast(list[dict[str, Any]], operation.get("parameters", []))
        if "$ref" in parameter
    }


def _lineage_condition_accepts(
    condition: dict[str, Any],
    origin_schema: dict[str, Any],
    instance: dict[str, Any],
    *,
    includes_user_message: bool,
) -> bool:
    """Evaluate the closed lineage condition exercised by the contract cases."""
    conversation_id = instance.get("conversationId")
    if conversation_id is not None:
        try:
            UUID(conversation_id)
        except AttributeError, TypeError, ValueError:
            return False
    branch = condition["then"] if conversation_id is None else condition["else"]
    if any(field not in instance for field in branch.get("required", [])):
        return False
    prohibited = branch.get("not", {}).get("required", [])
    if prohibited and all(field in instance for field in prohibited):
        return False
    if includes_user_message:
        user_message_id = instance.get("userMessageId")
        expected_type = branch["properties"]["userMessageId"].get("type", "string")
        if expected_type == "null" and user_message_id is not None:
            return False
        if expected_type == "string":
            try:
                UUID(user_message_id)
            except AttributeError, TypeError, ValueError:
                return False
    if "workflowOrigin" not in instance:
        return True
    origin = instance["workflowOrigin"]
    required = set(origin_schema["required"])
    if not isinstance(origin, dict) or set(origin) != required:
        return False
    try:
        for value in origin.values():
            UUID(value)
    except AttributeError, TypeError, ValueError:
        return False
    return True


def _deletion_request_accepts(schema: dict[str, Any], instance: dict[str, Any]) -> bool:
    if schema["additionalProperties"] is False and set(instance) - set(schema["properties"]):
        return False
    if any(field not in instance for field in schema["required"]):
        return False
    if instance["scope"] != schema["properties"]["scope"]["const"]:
        return False
    expected_version = instance["expectedVersion"]
    return (
        isinstance(expected_version, int)
        and not isinstance(expected_version, bool)
        and expected_version >= schema["properties"]["expectedVersion"]["minimum"]
    )


def test_manual_workflow_surface_is_versioned_closed_and_has_no_schedule() -> None:
    contract = _openapi()
    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["version"] == "1.22.0"
    assert WORKFLOW_PATHS <= set(contract["paths"])
    assert "Workflows" in {tag["name"] for tag in contract["tags"]}

    workflow_contract = json.dumps(
        {
            "paths": {
                path: contract["paths"][path]
                for path in contract["paths"]
                if path.startswith("/api/v1/workflows")
            },
            "schemas": {
                name: schema
                for name, schema in contract["components"]["schemas"].items()
                if name.startswith("Workflow") or name.startswith("DeleteWorkflow")
            },
        },
        separators=(",", ":"),
    ).lower()
    assert "schedule" not in workflow_contract
    assert "langgraph" not in workflow_contract
    assert "provider" not in workflow_contract
    assert "expression" not in workflow_contract.replace("expressions", "")


def test_every_workflow_mutation_is_csrf_and_idempotency_protected() -> None:
    for path, item in _openapi()["paths"].items():
        if not path.startswith("/api/v1/workflows"):
            continue
        for method, operation in item.items():
            if method not in {"post", "patch", "put", "delete"}:
                continue
            assert {
                "#/components/parameters/CsrfToken",
                "#/components/parameters/IdempotencyKey",
            } <= _mutation_refs(operation), (path, method)
            assert operation["requestBody"]["required"] is True


def test_step_catalogue_selectors_and_resource_limits_are_exact() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert [ref["$ref"].rsplit("/", 1)[1] for ref in schemas["WorkflowStep"]["oneOf"]] == [
        "WorkflowAgentRunStep",
        "WorkflowOwnerApprovalStep",
        "WorkflowOwnerNotificationStep",
        "WorkflowWaitUntilStep",
    ]
    assert schemas["WorkflowDraft"]["properties"]["steps"]["minItems"] == 1
    assert schemas["WorkflowDraft"]["properties"]["steps"]["maxItems"] == 8
    assert schemas["WorkflowDraft"]["properties"]["inputFields"]["maxItems"] == 16
    assert schemas["WorkflowInputFieldDefinition"]["properties"]["name"]["pattern"] == (
        "^[a-z][a-z0-9_]{0,63}$"
    )
    assert all(
        schemas[name]["additionalProperties"] is False
        for name in (
            "WorkflowAgentRunStep",
            "WorkflowOwnerApprovalStep",
            "WorkflowOwnerNotificationStep",
            "WorkflowWaitUntilStep",
            "WorkflowInstanceInputSelector",
            "WorkflowStepOutputSelector",
        )
    )
    assert [item["$ref"].rsplit("/", 1)[1] for item in schemas["WorkflowStepResult"]["oneOf"]] == [
        "WorkflowAgentResult",
        "WorkflowApprovalResult",
        "WorkflowNotificationReceipt",
        "WorkflowWaitResult",
    ]
    limits = schemas["WorkflowLimits"]["properties"]
    assert limits["maxSteps"]["const"] == 8
    assert limits["maxAgentRuns"]["const"] == 1
    assert limits["agentRunDeadlineSeconds"]["const"] == 300
    assert limits["approvalLifetimeSeconds"]["const"] == 7 * 24 * 60 * 60
    assert limits["waitHorizonSeconds"]["const"] == 30 * 24 * 60 * 60
    assert limits["instanceLifetimeSeconds"]["const"] == 30 * 24 * 60 * 60
    assert limits["maxDefinitionBytes"]["const"] == 65536
    assert limits["maxCombinedValueBytes"]["const"] == 65536
    assert limits["maxCheckpoints"]["const"] == 9
    assert all(
        limits[name]["const"] is False
        for name in ("toolsEnabled", "memoryEnabled", "delegationEnabled", "agentRunReplayEnabled")
    )


def test_state_recovery_and_deletion_vocabularies_match_decision() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert schemas["WorkflowInstanceStatus"]["enum"] == [
        "queued",
        "running",
        "waiting_for_time",
        "waiting_for_approval",
        "waiting_for_resolution",
        "resolution_required",
        "cancel_requested",
        "terminal",
    ]
    assert schemas["WorkflowTerminalOutcome"]["enum"] == ["completed", "canceled", "failed"]
    assert schemas["WorkflowRecoveryCapability"]["enum"] == [
        "request_replay",
        "settle_failed",
        "reconcile",
        "cancel",
    ]
    assert schemas["WorkflowDeletionScope"]["enum"] == [
        "draft",
        "retired_definition",
        "published_revision",
        "terminal_instance_state",
        "terminal_instance_history",
        "terminal_instance_result",
        "terminal_instance_checkpoints",
    ]
    assert set(schemas["WorkflowCompletedEffect"]["required"]) == {"stepOrdinal", "stepKind"}
    assert "partial" not in schemas["WorkflowInstanceStatus"]["enum"]


def test_every_deletion_scope_requires_version_and_uuid_idempotency() -> None:
    contract = _openapi()
    schemas = contract["components"]["schemas"]
    deletion_contracts = {
        "deleteWorkflowDraft": ("DeleteWorkflowDraftRequest", "draft"),
        "deleteWorkflowDefinition": (
            "DeleteWorkflowDefinitionRequest",
            "retired_definition",
        ),
        "deleteWorkflowRevision": (
            "DeleteWorkflowRevisionRequest",
            "published_revision",
        ),
        "deleteWorkflowInstanceState": (
            "DeleteWorkflowInstanceStateRequest",
            "terminal_instance_state",
        ),
        "deleteWorkflowInstanceHistory": (
            "DeleteWorkflowInstanceHistoryRequest",
            "terminal_instance_history",
        ),
        "deleteWorkflowInstanceResult": (
            "DeleteWorkflowInstanceResultRequest",
            "terminal_instance_result",
        ),
        "deleteWorkflowInstanceCheckpoints": (
            "DeleteWorkflowInstanceCheckpointsRequest",
            "terminal_instance_checkpoints",
        ),
    }
    operations = {
        operation["operationId"]: operation
        for item in contract["paths"].values()
        for method, operation in item.items()
        if method in {"post", "patch", "put", "delete"}
        and isinstance(operation, dict)
        and operation.get("operationId") in deletion_contracts
    }
    assert set(operations) == set(deletion_contracts)

    idempotency = contract["components"]["parameters"]["IdempotencyKey"]
    assert idempotency == {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
        "description": "Caller-generated UUID. Reuse only for an exact replay of the same command.",
    }
    UUID("00000000-0000-4000-8000-000000000010")
    for invalid_key in (None, "", "not-a-uuid"):
        with pytest.raises((AttributeError, TypeError, ValueError)):
            UUID(invalid_key)

    for operation_id, (schema_name, scope) in deletion_contracts.items():
        schema = schemas[schema_name]
        assert set(schema["required"]) == {"scope", "expectedVersion"}
        assert schema["properties"]["expectedVersion"] == {
            "type": "integer",
            "minimum": 1,
        }
        assert "#/components/parameters/IdempotencyKey" in _mutation_refs(operations[operation_id])
        assert _deletion_request_accepts(schema, {"scope": scope, "expectedVersion": 1})
        for invalid in (
            {"scope": scope},
            {"scope": scope, "expectedVersion": 0},
            {"scope": scope, "expectedVersion": "1"},
            {"scope": scope, "expectedVersion": True},
        ):
            assert not _deletion_request_accepts(schema, invalid)


def test_approval_and_notification_unions_preserve_legacy_and_add_workflow() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert [item["$ref"].rsplit("/", 1)[1] for item in schemas["Approval"]["oneOf"]] == [
        "ReminderApproval",
        "WorkflowApproval",
    ]
    workflow_approval = schemas["WorkflowApproval"]
    assert workflow_approval["properties"]["origin"]["const"] == "manual_workflow_instance"
    assert workflow_approval["properties"]["runId"]["type"] == "null"
    assert workflow_approval["properties"]["conversationId"]["type"] == "null"
    assert workflow_approval["properties"]["toolCallId"]["type"] == "null"
    for field in ("workflowRevisionId", "workflowInstanceId", "workflowStepExecutionId"):
        assert field in workflow_approval["required"]
    assert [item["$ref"].rsplit("/", 1)[1] for item in schemas["Notification"]["oneOf"]] == [
        "ReminderNotification",
        "BriefingNotification",
        "WorkflowNotification",
    ]
    forbidden = {"content", "input", "output", "instructions", "mapping", "ownerId"}
    assert not forbidden & set(schemas["WorkflowNotification"]["properties"])
    assert set(schemas["CreateWorkflowNotification"]["properties"]) == {
        "workflowId",
        "workflowRevisionId",
        "workflowInstanceId",
        "workflowStepOrdinal",
        "workflowStepExecutionId",
        "workflowResultId",
    }


def test_run_lineage_is_a_closed_ordinary_or_workflow_origin_union() -> None:
    schemas = _openapi()["components"]["schemas"]
    run = schemas["Run"]
    assert "null" in run["properties"]["conversationId"]["type"]
    assert run["then"]["required"] == ["workflowOrigin"]
    assert run["then"]["properties"]["userMessageId"]["type"] == "null"
    assert run["else"]["properties"]["userMessageId"]["type"] == "string"
    assert run["else"]["not"]["required"] == ["workflowOrigin"]
    assert run["if"]["properties"]["conversationId"]["type"] == "null"

    event_schema = _json(RUN_EVENTS_PATH)
    origin = event_schema["$defs"]["WorkflowRunOrigin"]
    assert origin["additionalProperties"] is False
    assert set(origin["required"]) == {
        "workflowRevisionId",
        "workflowInstanceId",
        "workflowStepExecutionId",
    }
    event_condition = event_schema["$defs"]["BaseProperties"]["allOf"][0]
    assert event_condition["then"]["required"] == ["workflowOrigin"]
    assert event_condition["else"]["not"]["required"] == ["workflowOrigin"]
    run_condition = event_schema["$defs"]["Run"]["allOf"][0]
    assert run_condition["then"]["properties"]["userMessageId"]["type"] == "null"
    assert run_condition["else"]["not"]["required"] == ["workflowOrigin"]


def test_run_lineage_contract_accepts_only_exact_unmixed_origin_cases() -> None:
    contract = _openapi()
    schemas = contract["components"]["schemas"]
    run_condition = schemas["Run"]
    event_condition = schemas["RunEventBase"]
    origin_schema = schemas["WorkflowRunOrigin"]
    conversation_id = "00000000-0000-4000-8000-000000000001"
    user_message_id = "00000000-0000-4000-8000-000000000002"
    workflow_origin = {
        "workflowRevisionId": "00000000-0000-4000-8000-000000000003",
        "workflowInstanceId": "00000000-0000-4000-8000-000000000004",
        "workflowStepExecutionId": "00000000-0000-4000-8000-000000000005",
    }

    valid_run_cases = [
        {"conversationId": conversation_id, "userMessageId": user_message_id},
        {
            "conversationId": None,
            "userMessageId": None,
            "workflowOrigin": workflow_origin,
        },
    ]
    invalid_run_cases = [
        {"conversationId": conversation_id, "userMessageId": None},
        {
            "conversationId": conversation_id,
            "userMessageId": user_message_id,
            "workflowOrigin": workflow_origin,
        },
        {"conversationId": None, "userMessageId": None},
        {
            "conversationId": None,
            "userMessageId": user_message_id,
            "workflowOrigin": workflow_origin,
        },
        {
            "conversationId": None,
            "userMessageId": None,
            "workflowOrigin": {"workflowRevisionId": workflow_origin["workflowRevisionId"]},
        },
        {
            "conversationId": None,
            "userMessageId": None,
            "workflowOrigin": {**workflow_origin, "conversationId": conversation_id},
        },
    ]
    for instance in valid_run_cases:
        assert _lineage_condition_accepts(
            run_condition, origin_schema, instance, includes_user_message=True
        )
    for instance in invalid_run_cases:
        assert not _lineage_condition_accepts(
            run_condition, origin_schema, instance, includes_user_message=True
        )

    valid_event_cases = [
        {"conversationId": conversation_id},
        {"conversationId": None, "workflowOrigin": workflow_origin},
    ]
    invalid_event_cases = [
        {"conversationId": conversation_id, "workflowOrigin": workflow_origin},
        {"conversationId": None},
        {
            "conversationId": None,
            "workflowOrigin": {"workflowRevisionId": workflow_origin["workflowRevisionId"]},
        },
    ]
    for instance in valid_event_cases:
        assert _lineage_condition_accepts(
            event_condition, origin_schema, instance, includes_user_message=False
        )
    for instance in invalid_event_cases:
        assert not _lineage_condition_accepts(
            event_condition, origin_schema, instance, includes_user_message=False
        )


def test_workflow_and_notification_events_are_closed_metadata_only() -> None:
    workflow = _json(WORKFLOW_EVENTS_PATH)
    notification = _json(NOTIFICATION_EVENTS_PATH)
    assert {item["$ref"].rsplit("/", 1)[1] for item in workflow["oneOf"]} == {
        "WorkflowDefinitionLifecycleEvent",
        "WorkflowInstanceLifecycleEvent",
        "WorkflowStepLifecycleEvent",
        "WorkflowRetentionLifecycleEvent",
    }
    assert {item["$ref"].rsplit("/", 1)[1] for item in notification["oneOf"]} >= {
        "WorkflowDeliveredEvent",
        "WorkflowAcknowledgedEvent",
    }
    serialized = json.dumps(workflow, separators=(",", ":")).lower()
    for forbidden in (
        "workflowname",
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
    ):
        assert f'"{forbidden}"' not in serialized


def test_generated_client_contains_workflow_transport_types() -> None:
    generated = GENERATED_PATH.read_text()
    assert "export type Run = {" in generated
    assert "export type RunEventBase = {" in generated
    run_type = next(line for line in generated.splitlines() if line.startswith("export type Run ="))
    event_type = next(
        line for line in generated.splitlines() if line.startswith("export type RunEventBase =")
    )
    assert 'readonly "conversationId": string | null' in run_type
    assert 'readonly "userMessageId": string | null' in run_type
    assert 'readonly "workflowOrigin"?: WorkflowRunOrigin' in run_type
    assert 'readonly "conversationId": string | null' in event_type
    assert 'readonly "workflowOrigin"?: WorkflowRunOrigin' in event_type
    for name in (
        "DeleteWorkflowDraftRequest",
        "DeleteWorkflowDefinitionRequest",
        "DeleteWorkflowRevisionRequest",
        "DeleteWorkflowInstanceStateRequest",
        "DeleteWorkflowInstanceHistoryRequest",
        "DeleteWorkflowInstanceResultRequest",
        "DeleteWorkflowInstanceCheckpointsRequest",
    ):
        deletion_type = next(
            line for line in generated.splitlines() if line.startswith(f"export type {name} =")
        )
        assert f"export type {name} = {{" in deletion_type
        assert 'readonly "expectedVersion": number' in deletion_type
    assert "readonly getWorkflow:" in generated
    assert "readonly createWorkflowDraft:" in generated
    assert "readonly startWorkflowInstance:" in generated
    assert "readonly resolveWorkflowInstance:" in generated
    assert "export type WorkflowInstance =" in generated
    assert "export type WorkflowStep" in generated
    assert "export type Approval = ReminderApproval | WorkflowApproval" in generated
    assert "export type Notification =" in generated and "WorkflowNotification" in generated
