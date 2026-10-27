"""Acceptance checks for Aura v1.21 scheduled-briefing contracts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/aura-v1.yaml"
NOTIFICATION_EVENTS_PATH = ROOT / "contracts/events/automation/v1/notification-events.schema.json"
BRIEFING_EVENTS_PATH = ROOT / "contracts/events/automation/v1/briefing-events.schema.json"
GENERATED_PATH = ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"

BRIEFING_PATHS = {
    "/api/v1/briefing-capabilities",
    "/api/v1/briefing-schedules",
    "/api/v1/briefing-schedules/{schedule_id}",
    "/api/v1/briefing-schedules/{schedule_id}/pause",
    "/api/v1/briefing-schedules/{schedule_id}/resume",
    "/api/v1/briefing-schedules/{schedule_id}/deletions",
    "/api/v1/briefing-schedules/{schedule_id}/occurrences",
    "/api/v1/briefing-schedules/{schedule_id}/occurrences/{occurrence_id}",
    "/api/v1/briefing-schedules/{schedule_id}/history-deletions",
    "/api/v1/briefing-results/{result_id}",
    "/api/v1/briefing-results/{result_id}/deletions",
}
MUTATIONS = {
    ("/api/v1/briefing-schedules", "post"),
    ("/api/v1/briefing-schedules/{schedule_id}/pause", "post"),
    ("/api/v1/briefing-schedules/{schedule_id}/resume", "post"),
    ("/api/v1/briefing-schedules/{schedule_id}/deletions", "post"),
    ("/api/v1/briefing-schedules/{schedule_id}/history-deletions", "post"),
    ("/api/v1/briefing-results/{result_id}/deletions", "post"),
}
EVOLVED_COMPONENTS = {
    "Notification",
    "NotificationStreamEvent",
    "NotificationLifecycleEvent",
}
UUIDS = {
    "event": "10000000-0000-4000-8000-000000000001",
    "notification": "10000000-0000-4000-8000-000000000002",
    "schedule": "10000000-0000-4000-8000-000000000003",
    "occurrence": "10000000-0000-4000-8000-000000000004",
    "result": "10000000-0000-4000-8000-000000000005",
}


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _openapi() -> dict[str, Any]:
    return _json(OPENAPI_PATH)


def _baseline() -> dict[str, Any] | None:
    result = subprocess.run(
        ["git", "show", "origin/dev:contracts/openapi/aura-v1.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return cast(dict[str, Any], json.loads(result.stdout)) if result.returncode == 0 else None


def _refs(operation: dict[str, Any]) -> set[str]:
    return {
        cast(str, parameter["$ref"])
        for parameter in cast(list[dict[str, Any]], operation.get("parameters", []))
        if "$ref" in parameter
    }


def test_openapi_v121_is_additive_except_for_authorized_notification_union() -> None:
    contract = _openapi()
    assert contract["openapi"] == "3.1.0"
    assert contract["info"]["version"] == "1.21.0"
    assert BRIEFING_PATHS <= set(contract["paths"])
    assert {"Notifications", "Briefings"} <= {tag["name"] for tag in contract["tags"]}

    operation_ids = [
        operation["operationId"]
        for item in contract["paths"].values()
        for method, operation in item.items()
        if method not in {"parameters", "summary", "description"}
    ]
    assert len(operation_ids) == len(set(operation_ids))

    baseline = _baseline()
    if baseline is None:
        return
    assert contract["servers"] == baseline["servers"]
    assert contract["security"] == baseline["security"]
    for path, item in baseline["paths"].items():
        assert contract["paths"][path] == item, path
    for group, components in baseline["components"].items():
        for name, component in components.items():
            if group == "schemas" and name in EVOLVED_COMPONENTS:
                continue
            assert contract["components"][group][name] == component, f"{group}/{name}"


def test_schedule_sources_cadence_and_policy_references_are_closed_and_bounded() -> None:
    schemas = _openapi()["components"]["schemas"]
    cadence = schemas["BriefingDailyCadence"]
    assert cadence["additionalProperties"] is False
    assert cadence["properties"]["kind"]["const"] == "daily"
    assert cadence["required"] == ["kind", "localTime", "timeZone"]

    sources = schemas["BriefingSourceGrants"]
    assert sources["additionalProperties"] is False
    assert set(sources["properties"]) == {"includeReminders", "calendarIds", "documentIds"}
    assert sources["properties"]["includeReminders"]["const"] is True
    assert sources["properties"]["calendarIds"]["maxItems"] == 50
    assert sources["properties"]["documentIds"]["maxItems"] == 50
    assert "memory" not in {name.lower() for name in sources["properties"]}

    policies = schemas["BriefingExecutionPolicyReferences"]
    assert policies["additionalProperties"] is False
    assert set(policies["required"]) == {
        "executionPolicyRevisionId",
        "promptBundleRevisionId",
        "modelPolicyRevisionId",
        "toolPolicyRevisionId",
    }
    forbidden = {"provider", "modelId", "checkpoint", "worker", "executor", "queue"}
    assert not forbidden & set(policies["properties"])


def test_briefing_capabilities_are_fixed_closed_and_private_by_omission() -> None:
    contract = _openapi()
    path = contract["paths"]["/api/v1/briefing-capabilities"]
    assert set(path) == {"get"}
    operation = path["get"]
    assert operation["operationId"] == "getBriefingCapabilities"
    assert "security" not in operation
    assert (
        operation["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    )
    assert {"401", "403"} <= set(operation["responses"])

    capabilities = contract["components"]["schemas"]["BriefingCapabilities"]
    expected = {
        "executionPolicyRevisionId",
        "executionPolicyRevision",
        "deadlineSeconds",
        "maxOutputTokens",
        "maxOutputChars",
        "cadenceKind",
        "memoryEnabled",
        "toolsEnabled",
    }
    assert capabilities["additionalProperties"] is False
    assert set(capabilities["required"]) == expected
    assert set(capabilities["properties"]) == expected
    properties = capabilities["properties"]
    assert (
        properties["executionPolicyRevisionId"]["const"] == "e80e8c61-575a-5a8c-862e-0c7cc91baa61"
    )
    assert properties["executionPolicyRevision"]["const"] == "briefing-execution-v1"
    assert properties["deadlineSeconds"]["const"] == 300
    assert properties["maxOutputTokens"]["const"] == 16_384
    assert properties["maxOutputChars"]["const"] == 32_768
    assert properties["cadenceKind"]["const"] == "daily"
    assert properties["memoryEnabled"]["const"] is False
    assert properties["toolsEnabled"]["const"] is False

    forbidden = {
        "provider",
        "model",
        "modelId",
        "prompt",
        "promptBundleRevisionId",
        "toolPolicyRevisionId",
        "owner",
        "ownerId",
        "source",
        "sources",
        "calendarIds",
        "documentIds",
        "runtime",
        "runId",
        "resultId",
    }
    assert not forbidden & set(properties)


def test_occurrence_result_and_manual_retention_are_explicit() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert schemas["BriefingOccurrenceStatus"]["enum"] == [
        "scheduled",
        "admitted",
        "running",
        "completed",
        "partial",
        "failed",
        "canceled",
        "skipped",
        "unknown",
    ]
    assert schemas["BriefingResultStatus"]["enum"] == [
        "completed",
        "partial",
        "failed",
        "canceled",
        "unknown",
        "deleted",
    ]
    occurrence = schemas["BriefingOccurrence"]
    assert {
        "scheduleId",
        "dueAt",
        "dueLocalDate",
        "dueLocalTime",
        "timeZone",
        "runId",
        "resultId",
        "sourceCompleteness",
        "version",
    } <= set(occurrence["required"])
    result = schemas["BriefingResult"]
    assert {
        "scheduleId",
        "occurrenceId",
        "runId",
        "sourceCompleteness",
        "content",
        "deletedAt",
    } <= set(result["required"])

    schedule_text = json.dumps(schemas["BriefingSchedule"], separators=(",", ":")).lower()
    assert "expiresat" not in schedule_text and "retentiondays" not in schedule_text
    assert schemas["DeleteBriefingScheduleRequest"]["properties"]["scope"]["const"] == "schedule"
    assert (
        schemas["DeleteBriefingHistoryRequest"]["properties"]["scope"]["const"]
        == "occurrence_history"
    )
    assert (
        schemas["DeleteBriefingResultRequest"]["properties"]["scope"]["const"] == "result_content"
    )
    assert schemas["BriefingDeletionScope"]["enum"] == [
        "schedule",
        "occurrence_history",
        "result_content",
    ]


def test_mutations_are_owner_protected_and_private_reads_are_no_store() -> None:
    contract = _openapi()
    for path, method in MUTATIONS:
        operation = contract["paths"][path][method]
        assert {
            "#/components/parameters/CsrfToken",
            "#/components/parameters/IdempotencyKey",
        } <= _refs(operation)
        assert operation["requestBody"]["required"] is True
    for path in BRIEFING_PATHS:
        for method, operation in contract["paths"][path].items():
            if method in {"parameters", "summary", "description"}:
                continue
            success = next(
                response
                for status, response in operation["responses"].items()
                if status.startswith("2")
            )
            assert success["headers"]["Cache-Control"]["schema"]["const"] == "no-store", (
                path,
                method,
            )


def test_notification_union_preserves_legacy_shape_and_closes_briefing_source() -> None:
    schemas = _openapi()["components"]["schemas"]
    union = schemas["Notification"]
    assert union["oneOf"] == [
        {"$ref": "#/components/schemas/ReminderNotification"},
        {"$ref": "#/components/schemas/BriefingNotification"},
    ]
    reminder = schemas["ReminderNotification"]
    assert reminder["additionalProperties"] is False
    assert "reminderId" in reminder["required"]
    assert not {"briefingScheduleId", "briefingOccurrenceId", "briefingResultId"} & set(
        reminder["properties"]
    )
    briefing = schemas["BriefingNotification"]
    assert briefing["additionalProperties"] is False
    assert {"briefingScheduleId", "briefingOccurrenceId", "briefingResultId"} <= set(
        briefing["required"]
    )
    assert "reminderId" not in briefing["properties"]
    baseline = _baseline()
    if baseline is not None:
        assert reminder == baseline["components"]["schemas"]["Notification"]


def test_notification_event_v1_preserves_legacy_definitions_additively() -> None:
    result = subprocess.run(
        [
            "git",
            "show",
            "origin/dev:contracts/events/automation/v1/notification-events.schema.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return
    baseline = cast(dict[str, Any], json.loads(result.stdout))
    current = _json(NOTIFICATION_EVENTS_PATH)
    for name in (
        "Uuid",
        "Timestamp",
        "NotificationStatus",
        "NotificationEventProperties",
        "NotificationDeliveredEvent",
        "NotificationAcknowledgedEvent",
    ):
        assert current["$defs"][name] == baseline["$defs"][name], name
    assert current["oneOf"][:2] == baseline["oneOf"]


def test_notification_events_accept_legacy_and_briefing_metadata_only() -> None:
    schema = _json(NOTIFICATION_EVENTS_PATH)
    definitions = schema["$defs"]
    legacy = {
        "schemaVersion": 1,
        "eventId": UUIDS["event"],
        "eventType": "notification.delivered",
        "notificationId": UUIDS["notification"],
        "reminderId": UUIDS["result"],
        "status": "delivered",
        "notificationVersion": 1,
        "occurredAt": "2026-10-27T07:30:00Z",
    }
    briefing = {
        "schemaVersion": 1,
        "eventId": UUIDS["event"],
        "eventType": "notification.acknowledged",
        "notificationId": UUIDS["notification"],
        "briefingScheduleId": UUIDS["schedule"],
        "briefingOccurrenceId": UUIDS["occurrence"],
        "briefingResultId": UUIDS["result"],
        "status": "acknowledged",
        "notificationVersion": 2,
        "occurredAt": "2026-10-27T08:00:00Z",
    }
    assert set(legacy) == set(definitions["NotificationEventProperties"]["required"])
    assert set(briefing) == set(definitions["BriefingEventProperties"]["required"])
    assert definitions["NotificationEventProperties"]["additionalProperties"] is False
    assert definitions["BriefingEventProperties"]["additionalProperties"] is False
    assert "content" not in definitions["BriefingEventProperties"]["properties"]
    assert "reminderId" not in definitions["BriefingEventProperties"]["properties"]
    assert "briefingResultId" not in definitions["NotificationEventProperties"]["properties"]
    assert legacy["status"] == "delivered" and briefing["status"] == "acknowledged"


def test_briefing_events_are_closed_bounded_and_reject_private_material() -> None:
    schema = _json(BRIEFING_EVENTS_PATH)
    occurrence = schema["$defs"]["OccurrenceEvent"]
    event = {
        "schemaVersion": 1,
        "eventId": UUIDS["event"],
        "eventType": "briefing.occurrence.partial",
        "scheduleId": UUIDS["schedule"],
        "occurrenceId": UUIDS["occurrence"],
        "occurrenceStatus": "partial",
        "occurrenceVersion": 4,
        "dueAt": "2026-10-27T07:30:00Z",
        "sourceCompleteness": "partial",
        "runId": UUIDS["result"],
        "resultId": UUIDS["result"],
        "occurredAt": "2026-10-27T07:31:00Z",
    }
    assert set(event) == set(occurrence["required"])
    assert occurrence["additionalProperties"] is False
    assert event["eventType"] in occurrence["properties"]["eventType"]["enum"]
    assert event["occurrenceStatus"] in schema["$defs"]["OccurrenceStatus"]["enum"]
    forbidden = {
        "content": "secret",
        "ownerId": UUIDS["event"],
        "documentId": UUIDS["result"],
        "calendarId": UUIDS["schedule"],
        "prompt": "ignore policy",
        "provider": "example",
    }
    for key in forbidden:
        assert key not in occurrence["properties"]
    schema_text = json.dumps(schema).lower()
    for term in (
        "briefing output",
        "document identifiers",
        "calendar identifiers",
        "reminder content",
        "owner identity",
        "credentials",
        "provider material",
        "executor state",
    ):
        assert term in schema_text


def test_generated_transport_contains_briefing_types_operations_and_digest() -> None:
    generated = GENERATED_PATH.read_text()
    digest = hashlib.sha256(OPENAPI_PATH.read_bytes()).hexdigest()
    assert f"Contract SHA-256: {digest}" in generated
    for name in (
        "BriefingSchedule",
        "BriefingOccurrence",
        "BriefingResult",
        "BriefingNotification",
        "ReminderNotification",
        "BriefingSourceGrants",
        "BriefingExecutionPolicyReferences",
        "BriefingCapabilities",
    ):
        assert re.search(rf"export type {name}\b", generated), name
    for operation in (
        "getBriefingCapabilities",
        "createBriefingSchedule",
        "pauseBriefingSchedule",
        "resumeBriefingSchedule",
        "deleteBriefingSchedule",
        "listBriefingOccurrences",
        "getBriefingResult",
        "deleteBriefingOccurrenceHistory",
        "deleteBriefingResult",
    ):
        assert f"readonly {operation}:" in generated
        assert re.search(rf"\b{operation}: \{{ method:", generated)
