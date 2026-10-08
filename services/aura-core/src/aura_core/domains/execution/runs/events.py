"""Execution-owned versioned run events used by transports and SSE."""

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
