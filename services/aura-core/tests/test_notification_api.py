"""HTTP contract checks for authenticated notification operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[3]


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text()))


def test_notification_api_is_owner_scoped_and_mutations_are_csrf_idempotent() -> None:
    contract = _contract()
    assert contract["info"]["version"] == "1.12.0"
    paths = contract["paths"]
    assert {
        "/api/v1/notifications",
        "/api/v1/notifications/{notification_id}",
        "/api/v1/notifications/{notification_id}/acknowledge",
        "/api/v1/notifications/stream",
    } <= paths.keys()
    operation = paths["/api/v1/notifications/{notification_id}/acknowledge"]["post"]
    refs = {parameter["$ref"] for parameter in operation["parameters"]}
    assert "#/components/parameters/CsrfToken" in refs
    assert "#/components/parameters/IdempotencyKey" in refs
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["$ref"].endswith("/NotificationAcknowledgeRequest")
    required = _contract()["components"]["schemas"]["NotificationAcknowledgeRequest"]["required"]
    assert "expectedVersion" in required


def test_notification_schemas_exclude_private_content_from_event_shapes() -> None:
    schemas = _contract()["components"]["schemas"]
    notification = schemas["Notification"]
    assert {"id", "reminderId", "status", "version", "createdAt"} <= set(
        notification["properties"]
    )
    event = schemas.get("NotificationEvent", {})
    assert not {"content", "prompt", "response"} & set(event.get("properties", {}))
    assert "unacknowledgedCount" in schemas["NotificationPage"]["required"]
