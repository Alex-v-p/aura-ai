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
