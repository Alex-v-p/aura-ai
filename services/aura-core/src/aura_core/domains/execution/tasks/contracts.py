"""Closed, content-free contracts for the federated Task Center projection.

The execution domain deliberately stores a read model only.  Source modules
remain authoritative and are queried through :class:`TaskSourcePort`.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

TASK_PROJECTION_COMPONENT: Final = "aura.execution.task_projection@1"
TASK_SOURCE_INVALIDATED_TOPIC: Final = "aura.execution.task_source_invalidated.v1"
TASK_ROUTE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "research_detail",
        "workflow_instance_detail",
        "briefing_occurrence_detail",
        "document_detail",
        "memory_reindex_detail",
    }
)
MAX_CURSOR_LENGTH: Final = 1024
_MAX_SOURCE_VERSION: Final = 2_147_483_647
_RFC3339_DATE_TIME: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class TaskSourceKind(StrEnum):
    RESEARCH = "research"
    WORKFLOW_INSTANCE = "workflow_instance"
    BRIEFING_OCCURRENCE = "briefing_occurrence"
    DOCUMENT_PROCESSING = "document_processing"
    MEMORY_REINDEX = "memory_reindex"


class TaskNormalizedStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class TaskCommand(StrEnum):
    CANCEL = "cancel"
    RETRY = "retry"


class TaskCommandState(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TaskProjectionCapability:
    can_cancel: bool = False
    can_retry: bool = False
    has_source_detail: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "canCancel": self.can_cancel,
            "canRetry": self.can_retry,
            "hasSourceDetail": self.has_source_detail,
        }


@dataclass(frozen=True, slots=True)
class TaskProjectionSnapshot:
    """Source-owned metadata DTO; no content or owner display values."""

    owner_scope_id: UUID
    source_kind: TaskSourceKind
    source_id: UUID
    source_version: int
    status: TaskNormalizedStatus
    source_status: str
    source_outcome: str | None
    stage: str | None
    progress: float | None
    capabilities: TaskProjectionCapability
    route_key: str
    failure_class: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    source_deleted: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.source_version < 1:
            raise ValueError("unsupported task projection snapshot")
        if not self.source_status or len(self.source_status) > 64:
            raise ValueError("source status is required and bounded")
        if self.progress is not None and not 0 <= self.progress <= 1:
            raise ValueError("task progress must be between zero and one")
        if self.route_key not in TASK_ROUTE_KEYS:
            raise ValueError("task route key is invalid")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "updated_at", _utc(self.updated_at))
        if self.finished_at is not None:
            object.__setattr__(self, "finished_at", _utc(self.finished_at))

    @property
    def key(self) -> tuple[TaskSourceKind, UUID]:
        return self.source_kind, self.source_id

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "ownerScopeId": str(self.owner_scope_id),
            "sourceKind": self.source_kind.value,
            "sourceId": str(self.source_id),
            "sourceVersion": self.source_version,
            "status": self.status.value,
            "sourceStatus": self.source_status,
            "sourceOutcome": self.source_outcome,
            "stage": self.stage,
            "progress": self.progress,
            "capabilities": self.capabilities.as_dict(),
            "routeKey": self.route_key,
            "failureClass": self.failure_class,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "sourceDeleted": self.source_deleted,
        }

    def as_summary(self, *, task_id: UUID | None = None) -> dict[str, object]:
        """Serialize exactly the public ``DurableTaskSummary`` shape."""
        return {
            "id": str(task_id or self.source_id),
            "sourceKind": self.source_kind.value,
            "sourceId": str(self.source_id),
            "sourceVersion": self.source_version,
            "status": self.status.value,
            "sourceStatus": self.source_status,
            "sourceOutcome": self.source_outcome,
            "stage": self.stage,
            "progress": self.progress,
            "capabilities": self.capabilities.as_dict(),
            "routeKey": self.route_key,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }

    def as_detail(
        self,
        *,
        task_id: UUID | None = None,
        display_label: str | None = None,
        recovery_action: str | None = None,
        artifact_refs: tuple[Mapping[str, object], ...] = (),
    ) -> dict[str, object]:
        """Serialize exactly the public ``DurableTaskDetail`` shape."""
        return self.as_summary(task_id=task_id) | {
            "displayLabel": display_label,
            "failureClass": self.failure_class,
            "recoveryAction": recovery_action,
            "artifactRefs": list(artifact_refs),
        }


@dataclass(frozen=True, slots=True)
class TaskProjectionSnapshotQuery:
    mode: str
    source_kind: TaskSourceKind
    source_id: UUID | None = None
    minimum_source_version: int = 1
    cursor: str | None = None
    page_size: int = 100
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.mode not in {"exact", "cursor"}:
            raise ValueError("invalid task snapshot query")
        if self.mode == "exact" and self.source_id is None:
            raise ValueError("exact task query requires source id")
        if self.mode == "cursor" and self.source_id is not None:
            raise ValueError("cursor task query cannot contain source id")
        if not 1 <= self.page_size <= 500 or self.minimum_source_version < 1:
            raise ValueError("task query bounds are invalid")
        if self.cursor is not None and len(self.cursor) > MAX_CURSOR_LENGTH:
            raise ValueError("task cursor is too long")

    @classmethod
    def exact(
        cls, source_kind: TaskSourceKind, source_id: UUID, minimum_source_version: int = 1
    ) -> TaskProjectionSnapshotQuery:
        return cls(
            "exact", source_kind, source_id=source_id, minimum_source_version=minimum_source_version
        )

    @classmethod
    def cursor_page(
        cls, source_kind: TaskSourceKind, cursor: str | None = None, page_size: int = 100
    ) -> TaskProjectionSnapshotQuery:
        return cls("cursor", source_kind, cursor=cursor, page_size=page_size)


@dataclass(frozen=True, slots=True)
class TaskProjectionSnapshotPage:
    items: tuple[TaskProjectionSnapshot, ...]
    next_cursor: str | None
    scan_complete: bool
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class TaskSourceInvalidation:
    source_kind: TaskSourceKind
    source_id: UUID
    source_version: int
    event_id: UUID
    occurred_at: datetime
    correlation_id: UUID
    causation_id: UUID
    schema_version: int = 1
    # Durable outbox sequence is an internal transport cursor.  It is not
    # serialized into the standard invalidation envelope consumed by Core.
    event_sequence: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.source_version) is not int
            or not 1 <= self.source_version <= _MAX_SOURCE_VERSION
        ):
            raise ValueError("invalid task invalidation")
        if (
            self.event_sequence is not None
            and (type(self.event_sequence) is not int or self.event_sequence < 1)
        ):
            raise ValueError("invalid task invalidation sequence")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TaskSourceInvalidation:
        allowed = {
            "schemaVersion",
            "eventId",
            "eventType",
            "sourceKind",
            "sourceId",
            "sourceVersion",
            "occurredAt",
            "correlationId",
            "causationId",
        }
        if not isinstance(payload, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("task invalidation is not a JSON object")
        if set(payload) != allowed:
            raise ValueError("task invalidation is not schema exact")

        schema_version = payload["schemaVersion"]
        event_type = payload["eventType"]
        source_kind = payload["sourceKind"]
        source_version = payload["sourceVersion"]
        occurred_at = payload["occurredAt"]
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("task invalidation has an invalid schema version")
        if type(event_type) is not str or event_type != "task.source_invalidated":
            raise ValueError("task invalidation has an invalid event type")
        if type(source_kind) is not str or source_kind not in {
            kind.value for kind in TaskSourceKind
        }:
            raise ValueError("task invalidation has an invalid source kind")
        if type(source_version) is not int or not 1 <= source_version <= _MAX_SOURCE_VERSION:
            raise ValueError("task invalidation has an invalid source version")
        if type(occurred_at) is not str or _RFC3339_DATE_TIME.fullmatch(occurred_at) is None:
            raise ValueError("task invalidation has an invalid occurredAt")
        try:
            parsed_occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            if parsed_occurred_at.tzinfo is None or parsed_occurred_at.utcoffset() is None:
                raise ValueError("task invalidation occurredAt must include a timezone")
        except ValueError as exc:
            raise ValueError("task invalidation has an invalid occurredAt") from exc

        def uuid_field(field: str) -> UUID:
            value = payload[field]
            if type(value) is not str:
                raise ValueError(f"task invalidation has an invalid {field}")
            try:
                return UUID(value)
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"task invalidation has an invalid {field}") from exc

        return cls(
            TaskSourceKind(source_kind),
            uuid_field("sourceId"),
            source_version,
            uuid_field("eventId"),
            parsed_occurred_at,
            uuid_field("correlationId"),
            uuid_field("causationId"),
            schema_version=schema_version,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": str(self.event_id),
            "eventType": "task.source_invalidated",
            "sourceKind": self.source_kind.value,
            "sourceId": str(self.source_id),
            "sourceVersion": self.source_version,
            "occurredAt": self.occurred_at.isoformat(),
            "correlationId": str(self.correlation_id),
            "causationId": str(self.causation_id),
        }


@dataclass(frozen=True, slots=True)
class TaskProjectionRow(TaskProjectionSnapshot):
    projection_version: int = 1


@dataclass(frozen=True, slots=True)
class TaskCommandReceipt:
    task_id: UUID
    command: TaskCommand
    state: TaskCommandState
    source_version: int


@dataclass(frozen=True, slots=True)
class TaskListFilter:
    owner_scope_id: UUID
    source_kind: TaskSourceKind | None = None
    status: TaskNormalizedStatus | None = None
    updated_from: datetime | None = None
    updated_to: datetime | None = None
    cursor: str | None = None
    limit: int = 25


@dataclass(frozen=True, slots=True)
class TaskProjectionRebuildState:
    source_kind: TaskSourceKind
    pass_id: UUID
    cursor: str | None
    started_at: datetime
    seen_ids: tuple[UUID, ...] = ()
    completed: bool = False


def encode_cursor(updated_at: datetime, task_id: UUID) -> str:
    raw = json.dumps([_utc(updated_at).isoformat(), str(task_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("invalid task cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(str(raw[0])), UUID(str(cast(object, raw[1])))
    except (ValueError, TypeError, IndexError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid task cursor") from exc


def snapshot_to_row(
    snapshot: TaskProjectionSnapshot, projection_version: int | None = None
) -> TaskProjectionRow:
    fields = {name: getattr(snapshot, name) for name in TaskProjectionSnapshot.__dataclass_fields__}
    return TaskProjectionRow(
        **fields,
        projection_version=projection_version or snapshot.source_version,
    )


__all__ = [
    "TASK_PROJECTION_COMPONENT",
    "TASK_SOURCE_INVALIDATED_TOPIC",
    "TaskSourceKind",
    "TaskNormalizedStatus",
    "TaskCommand",
    "TaskCommandState",
    "TaskProjectionCapability",
    "TaskProjectionSnapshot",
    "TaskProjectionSnapshotQuery",
    "TaskProjectionSnapshotPage",
    "TaskSourceInvalidation",
    "TaskProjectionRow",
    "TaskCommandReceipt",
    "TaskListFilter",
    "TaskProjectionRebuildState",
    "encode_cursor",
    "decode_cursor",
    "snapshot_to_row",
]
