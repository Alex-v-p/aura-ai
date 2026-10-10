"""Execution-owned versioned run events used by transports and SSE."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: UUID
    sequence: int
    event_type: str
    run_id: UUID
    conversation_id: UUID
    occurred_at: datetime
    data: dict[str, Any]
    schema_version: int = 1

    def __getattribute__(self, name: str) -> Any:
        # Event transports inspect ``event_type`` before fan-out. Validate
        # tool lifecycle envelopes at that shared access point so freshly
        # constructed, hydrated, and externally-ingested values all fail
        # closed without allowing private evidence into the publisher.
        value = object.__getattribute__(self, name)
        if name == "event_type" and value == "tool.status":
            validate_tool_status(object.__getattribute__(self, "data"))
        return value

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["schemaVersion"] = value.pop("schema_version")
        value["eventId"] = str(value.pop("event_id"))
        value["eventType"] = value.pop("event_type")
        value["runId"] = str(value.pop("run_id"))
        value["conversationId"] = str(value.pop("conversation_id"))
        value["occurredAt"] = self.occurred_at.isoformat()
        value.pop("occurred_at", None)
        value["data"] = _json_values(value["data"])
        return value


def _json_values(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        mapping: dict[str, Any] = {}
        for key, item in cast(dict[Any, Any], value).items():
            mapping[str(key)] = _json_values(item)
        return mapping
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in cast(list[Any], value)]
    return value


def new_event(
    event_type: str, run_id: UUID, conversation_id: UUID, sequence: int, data: dict[str, Any]
) -> RunEvent:
    return RunEvent(uuid4(), sequence, event_type, run_id, conversation_id, utc_now(), data)


# Realtime memory activity is deliberately a narrow, identifier-only envelope.
# Keep this validation next to the run event boundary so no worker or transport
# can accidentally publish private candidate/evidence text.
_MEMORY_ACTIVITY_FIELDS = frozenset(
    {
        "id",
        "action",
        "status",
        "scope",
        "candidateId",
        "memoryId",
        "memoryRevisionId",
        "policyRevisionId",
        "embeddingGenerationId",
        "reconciliationStatus",
        "occurredAt",
    }
)
_MEMORY_ACTIVITY_ACTIONS = frozenset(
    {"created", "reinforced", "disputed", "recalled", "queued_for_review"}
)
_MEMORY_ACTIVITY_STATUSES = frozenset({"queued", "completed", "failed"})
_MEMORY_ACTIVITY_RECONCILIATION = frozenset({"pending", "authoritative"})

_TOOL_STATUS_FIELDS = frozenset(
    {"callId", "toolId", "toolVersion", "status", "errorClass", "errorCode", "resultCount"}
)
_TOOL_STATUS_VALUES = frozenset({"pending", "executing", "completed", "denied", "failed"})
_TOOL_ERROR_CLASSES = frozenset(
    {
        "capability",
        "validation",
        "policy",
        "budget",
        "timeout",
        "provider_protocol",
        "execution",
        "cancellation",
    }
)
_TOOL_ERROR_CODES = frozenset(
    {
        "TOOL_ARGUMENTS_INVALID",
        "TOOL_BUDGET_EXHAUSTED",
        "TOOL_CANCELLED",
        "TOOL_CAPABILITY_UNAVAILABLE",
        "TOOL_EXECUTION_FAILED",
        "TOOL_HANDLER_UNAVAILABLE",
        "TOOL_LATE_CALL",
        "TOOL_MIXED_STREAM",
        "TOOL_MULTIPLE_CALLS",
        "TOOL_PROVIDER_PROTOCOL",
        "TOOL_RESULT_INVALID",
        "MEMORY_SEARCH_UNAVAILABLE",
        "TOOL_TIMEOUT",
        "TOOL_UNKNOWN",
        "TOOL_NOT_GRANTED",
    }
)
_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def validate_memory_activity(data: dict[str, Any]) -> dict[str, Any]:
    """Return a safe copy of a memory activity payload or raise ``ValueError``.

    Only bounded identifiers, enum values, scope type, and a timestamp are
    allowed.  This is intentionally stricter than the general event payload
    because events are persisted and fanned out to browser clients.
    """

    if frozenset(data) != _MEMORY_ACTIVITY_FIELDS:
        raise ValueError("memory activity contains unsupported fields")
    action = data.get("action")
    status = data.get("status")
    reconciliation = data.get("reconciliationStatus")
    if action not in _MEMORY_ACTIVITY_ACTIONS:
        raise ValueError("unsupported memory activity action")
    if status not in _MEMORY_ACTIVITY_STATUSES:
        raise ValueError("unsupported memory activity status")
    if reconciliation not in _MEMORY_ACTIVITY_RECONCILIATION:
        raise ValueError("unsupported memory activity reconciliation status")
    for key in (
        "id",
        "candidateId",
        "memoryId",
        "memoryRevisionId",
        "policyRevisionId",
        "embeddingGenerationId",
    ):
        value = data[key]
        if key == "id" and value is None:
            raise ValueError("memory activity id is required")
        if value is not None:
            try:
                UUID(str(value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid memory activity identifier: {key}") from exc
    scope = data["scope"]
    if scope is not None:
        scope = cast(dict[str, Any], scope)
        if set(scope) not in ({"type"}, {"type", "agentProfileId"}):
            raise ValueError("invalid memory activity scope")
        if scope.get("type") not in {"user", "agent"}:
            raise ValueError("invalid memory activity scope type")
        if scope.get("type") == "user" and set(scope) != {"type"}:
            raise ValueError("user activity scope cannot contain an agent identifier")
        if scope.get("type") == "agent":
            try:
                UUID(str(scope.get("agentProfileId")))
            except (TypeError, ValueError) as exc:
                raise ValueError("agent activity scope requires an identifier") from exc
    occurred_at = data.get("occurredAt")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise ValueError("memory activity occurredAt is required")
    return _json_values(data)


def validate_tool_status(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the strict metadata-only tool lifecycle envelope."""

    required = {"callId", "toolId", "toolVersion", "status"}
    if not set(data).issubset(_TOOL_STATUS_FIELDS) or not required.issubset(data):
        raise ValueError("tool status contains unsupported or missing fields")
    try:
        UUID(str(data["callId"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid tool status call identifier") from exc
    tool_id = data["toolId"]
    if (
        not isinstance(tool_id, str)
        or not _TOOL_ID_PATTERN.fullmatch(tool_id)
        or len(tool_id) > 128
    ):
        raise ValueError("invalid tool status tool identifier")
    if type(data["toolVersion"]) is not int or data["toolVersion"] < 1:
        raise ValueError("invalid tool status version")
    if data["status"] not in _TOOL_STATUS_VALUES:
        raise ValueError("unsupported tool status")
    error_class = data.get("errorClass")
    if error_class is not None and error_class not in _TOOL_ERROR_CLASSES:
        raise ValueError("unsupported tool status error class")
    error_code = data.get("errorCode")
    if error_code is not None and error_code not in _TOOL_ERROR_CODES:
        raise ValueError("invalid tool status error code")
    count = data.get("resultCount")
    if count is not None and (type(count) is not int or not 0 <= count <= 8):
        raise ValueError("invalid tool status result count")
    return _json_values(dict(data))
