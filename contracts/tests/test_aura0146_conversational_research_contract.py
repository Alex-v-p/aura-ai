"""Contract acceptance checks for AURA-0146 research notifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/aura-v1.yaml"
EVENT_PATH = ROOT / "contracts/events/automation/v1/notification-events.schema.json"
GENERATED_PATH = (
    ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"
)

UUIDS = {
    "event": "10000000-0000-4000-8000-000000000001",
    "notification": "10000000-0000-4000-8000-000000000002",
    "research": "10000000-0000-4000-8000-000000000003",
}
NOW = "2026-10-30T12:00:00Z"
PRIVATE_FIELDS = {
    "owner",
    "ownerId",
    "question",
    "query",
    "answer",
    "content",
    "citation",
    "citations",
    "evidence",
    "excerpt",
    "source",
    "sources",
    "url",
    "artifactId",
    "artifactIds",
    "artifactRef",
    "provider",
    "providerData",
    "recovery",
    "failureDetail",
}


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _resolve(document: dict[str, Any], ref: str) -> dict[str, Any]:
    value: Any = document
    for part in ref.removeprefix("#/").split("/"):
        value = value[part]
    return cast(dict[str, Any], value)


def _accepts(document: dict[str, Any], schema: dict[str, Any], value: Any) -> bool:
    if "$ref" in schema:
        return _accepts(document, _resolve(document, schema["$ref"]), value)
    if "oneOf" in schema:
        return sum(_accepts(document, item, value) for item in schema["oneOf"]) == 1
    if "allOf" in schema and not all(
        _accepts(document, item, value) for item in schema["allOf"]
    ):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_accepts(document, {**schema, "type": item}, value) for item in expected)
    if expected == "null":
        return value is None
    if expected == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            name not in value or _accepts(document, properties[name], value[name])
            for name in properties
        )
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
        )
    if expected == "string":
        return isinstance(value, str)
    return True


def _notification() -> dict[str, Any]:
    return {
        "id": UUIDS["notification"],
        "researchId": UUIDS["research"],
        "status": "delivered",
        "version": 1,
        "createdAt": NOW,
        "updatedAt": NOW,
        "deliveredAt": NOW,
        "acknowledgedAt": None,
    }


def _event(event_type: str = "notification.delivered") -> dict[str, Any]:
    status = "delivered" if event_type.endswith("delivered") else "acknowledged"
    return {
        "schemaVersion": 1,
        "eventId": UUIDS["event"],
        "eventType": event_type,
        "notificationId": UUIDS["notification"],
        "researchId": UUIDS["research"],
        "status": status,
        "notificationVersion": 1,
        "occurredAt": NOW,
    }


def test_research_notification_is_additive_across_rest_list_detail_and_sse() -> None:
    document = _json(OPENAPI_PATH)
    schemas = document["components"]["schemas"]

    assert {item["$ref"] for item in schemas["Notification"]["oneOf"]} == {
        "#/components/schemas/ReminderNotification",
        "#/components/schemas/BriefingNotification",
        "#/components/schemas/WorkflowNotification",
        "#/components/schemas/ResearchNotification",
    }
    assert document["paths"]["/api/v1/notifications"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/NotificationPage"}
    assert document["paths"]["/api/v1/notifications/{notification_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Notification"
    }
    assert document["paths"]["/api/v1/notifications/stream"]["get"]["responses"][
        "200"
    ]["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/NotificationStreamEvent"
    }
    stream_refs = {item["$ref"] for item in schemas["NotificationStreamEvent"]["oneOf"]}
    assert "#/components/schemas/ResearchNotificationLifecycleEvent" in stream_refs
    assert "#/components/schemas/NotificationHeartbeatEvent" in stream_refs


def test_existing_notification_variants_and_shapes_remain_available() -> None:
    schemas = _json(OPENAPI_PATH)["components"]["schemas"]
    assert set(schemas["ReminderNotification"]["properties"]) == {
        "id", "reminderId", "status", "version", "createdAt", "updatedAt",
        "deliveredAt", "acknowledgedAt",
    }
    assert set(schemas["BriefingNotification"]["properties"]) == {
        "id", "briefingScheduleId", "briefingOccurrenceId", "briefingResultId",
        "status", "version", "createdAt", "updatedAt", "deliveredAt",
        "acknowledgedAt",
    }
    assert set(schemas["WorkflowNotification"]["properties"]) == {
        "id", "workflowId", "workflowRevisionId", "workflowInstanceId",
        "workflowStepOrdinal", "workflowStepExecutionId", "workflowResultId",
        "status", "version", "createdAt", "updatedAt", "deliveredAt",
        "acknowledgedAt",
    }
    lifecycle_refs = {
        item["$ref"] for item in schemas["NotificationLifecycleEvent"]["oneOf"]
    }
    assert {
        "#/components/schemas/ReminderNotificationLifecycleEvent",
        "#/components/schemas/BriefingNotificationLifecycleEvent",
        "#/components/schemas/WorkflowNotificationLifecycleEvent",
    } <= lifecycle_refs


def test_research_notification_shapes_are_closed_and_metadata_only() -> None:
    document = _json(OPENAPI_PATH)
    schemas = document["components"]["schemas"]
    expected_notification = {
        "id", "researchId", "status", "version", "createdAt", "updatedAt",
        "deliveredAt", "acknowledgedAt",
    }
    expected_event = {
        "schemaVersion", "eventId", "eventType", "notificationId", "researchId",
        "status", "notificationVersion", "occurredAt",
    }
    assert schemas["ResearchNotification"]["additionalProperties"] is False
    assert set(schemas["ResearchNotification"]["properties"]) == expected_notification
    assert schemas["ResearchNotificationLifecycleEvent"]["additionalProperties"] is False
    assert set(schemas["ResearchNotificationLifecycleEvent"]["properties"]) == expected_event
    assert not PRIVATE_FIELDS & expected_notification
    assert not PRIVATE_FIELDS & expected_event
    assert _accepts(document, schemas["Notification"], _notification())
    assert _accepts(document, schemas["NotificationStreamEvent"], _event())
    assert not _accepts(
        document, schemas["Notification"], {**_notification(), "answer": "private"}
    )
    assert not _accepts(
        document, schemas["NotificationStreamEvent"], {**_event(), "url": "https://example.test"}
    )


def test_versioned_event_contract_is_closed_private_and_preserves_prior_variants() -> None:
    document = _json(EVENT_PATH)
    root_refs = {item["$ref"] for item in document["oneOf"]}
    assert {
        "#/$defs/NotificationDeliveredEvent",
        "#/$defs/NotificationAcknowledgedEvent",
        "#/$defs/BriefingDeliveredEvent",
        "#/$defs/BriefingAcknowledgedEvent",
        "#/$defs/WorkflowDeliveredEvent",
        "#/$defs/WorkflowAcknowledgedEvent",
        "#/$defs/ResearchDeliveredEvent",
        "#/$defs/ResearchAcknowledgedEvent",
    } == root_refs
    fields = set(document["$defs"]["ResearchEventProperties"]["properties"])
    assert fields == {
        "schemaVersion", "eventId", "eventType", "notificationId", "researchId",
        "status", "notificationVersion", "occurredAt",
    }
    assert not PRIVATE_FIELDS & fields
    assert _accepts(document, document, _event())
    assert _accepts(document, document, _event("notification.acknowledged"))
    for private_field in PRIVATE_FIELDS:
        assert not _accepts(document, document, {**_event(), private_field: "private"})


def test_generated_client_contains_only_transport_types_for_research_notifications() -> None:
    generated = GENERATED_PATH.read_text()
    assert "export type ResearchNotification =" in generated
    assert "export type ResearchNotificationLifecycleEvent =" in generated
    assert "readonly \"researchId\": string;" in generated
    assert (
        "export type Notification = ReminderNotification | BriefingNotification "
        "| WorkflowNotification | ResearchNotification;"
    ) in generated
    assert "ResearchNotificationLifecycleEvent | NotificationHeartbeatEvent" in generated
    assert "researchNotificationAnswer" not in generated
    assert "researchNotificationContent" not in generated
