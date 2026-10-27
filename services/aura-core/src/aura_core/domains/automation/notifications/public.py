"""Public notification commands, queries, and content-free replay DTOs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

NOTIFICATION_COMPONENT = "aura.automation.notification_delivery"
NOTIFICATION_TOPIC = "aura.automation.notifications.v1"


class NotificationStatus(StrEnum):
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"


class NotificationView(StrEnum):
    ALL = "all"
    UNREAD = "unread"


class NotificationSourceKind(StrEnum):
    REMINDER = "reminder"
    BRIEFING = "briefing"


@dataclass(frozen=True, slots=True)
class ReminderNotificationSource:
    """The legacy reminder-backed notification source."""

    kind: Literal["reminder"]
    reminder_id: UUID

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind, "reminderId": str(self.reminder_id)}


@dataclass(frozen=True, slots=True)
class BriefingNotificationSource:
    """A content-free link to a durable briefing result."""

    kind: Literal["briefing"]
    briefing_schedule_id: UUID
    briefing_occurrence_id: UUID
    briefing_result_id: UUID


NotificationSource = ReminderNotificationSource | BriefingNotificationSource


@dataclass(frozen=True, slots=True)
class CreateBriefingNotification:
    """Public source-neutral command for a later briefing delivery adapter.

    The command carries only an opaque result identity.  Briefing result
    content, schedule state, execution state, and provider data remain owned by
    the briefing domain and are never read by Notifications.
    """

    issuer: str
    subject: str
    briefing_schedule_id: UUID
    briefing_occurrence_id: UUID
    briefing_result_id: UUID
    idempotency_key: str

    def __post_init__(self) -> None:
        if not self.issuer or not self.subject:
            raise ValueError("authenticated owner is required")
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("notification idempotency key is required")


DeliverBriefingNotification = CreateBriefingNotification


@dataclass(frozen=True, slots=True)
class Notification:
    id: UUID
    issuer: str
    subject: str
    source: NotificationSource
    status: NotificationStatus
    version: int
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime
    acknowledged_at: datetime | None = None

    @property
    def reminder_id(self) -> UUID | None:
        """Compatibility accessor for existing reminder consumers."""

        if isinstance(self.source, ReminderNotificationSource):
            return self.source.reminder_id
        return None

    @property
    def briefing_result_id(self) -> UUID | None:
        if isinstance(self.source, BriefingNotificationSource):
            return self.source.briefing_result_id
        return None


@dataclass(frozen=True, slots=True)
class NotificationReplayEvent:
    id: UUID
    notification_id: UUID
    source: NotificationSource
    status: NotificationStatus
    version: int
    occurred_at: datetime

    @property
    def reminder_id(self) -> UUID | None:
        if isinstance(self.source, ReminderNotificationSource):
            return self.source.reminder_id
        return None

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": 1,
            "eventId": str(self.id),
            "eventType": f"notification.{self.status.value}",
            "notificationId": str(self.notification_id),
            "status": self.status.value,
            "notificationVersion": self.version,
            "occurredAt": self.occurred_at.astimezone(UTC).isoformat(),
        }
        # Keep the existing reminder event field for old clients.  Briefing
        # events deliberately have no fabricated reminder identifier.
        if self.reminder_id is not None:
            payload["reminderId"] = str(self.reminder_id)
        elif isinstance(self.source, BriefingNotificationSource):
            payload.update(
                {
                    "briefingScheduleId": str(self.source.briefing_schedule_id),
                    "briefingOccurrenceId": str(self.source.briefing_occurrence_id),
                    "briefingResultId": str(self.source.briefing_result_id),
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[Notification, ...]
    next_cursor: str | None
    unacknowledged_count: int = 0


@dataclass(frozen=True, slots=True)
class DueDeliveryResult:
    notifications: tuple[Notification, ...]
    fired_count: int
    duplicate_count: int
    due_lag_ms: float
    generated_count: int = 0
    skipped_count: int = 0
    missed_count: int = 0


class NotificationError(Exception):
    """Base class for deterministic notification failures."""


class NotificationNotFound(NotificationError):
    pass


class NotificationVersionConflict(NotificationError):
    pass


class NotificationIdempotencyConflict(NotificationError):
    pass


class NotificationRepository(Protocol):
    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: NotificationView = NotificationView.ALL,
        status: NotificationStatus | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> NotificationPage: ...

    async def get(self, issuer: str, subject: str, notification_id: UUID) -> Notification: ...

    async def acknowledge(
        self,
        issuer: str,
        subject: str,
        notification_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Notification: ...

    async def create_briefing_notification(
        self, command: CreateBriefingNotification
    ) -> Notification: ...

    async def deliver_due(self, *, limit: int = 100) -> DueDeliveryResult: ...

    async def fire_due(self, *, limit: int = 100) -> DueDeliveryResult: ...

    async def replay(
        self,
        issuer: str,
        subject: str,
        *,
        after: UUID | None = None,
    ) -> tuple[NotificationReplayEvent, ...]: ...

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool: ...

    def stream(
        self,
        issuer: str,
        subject: str,
        *,
        after: UUID | None = None,
    ) -> AsyncIterator[NotificationReplayEvent]: ...

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool: ...


class NotificationApplicationService:
    """Shared notification use cases for HTTP and the scheduler entrypoint."""

    def __init__(self, repository: NotificationRepository) -> None:
        self.repository = repository

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: NotificationView = NotificationView.ALL,
        status: NotificationStatus | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> NotificationPage:
        return await self.repository.list(
            issuer, subject, view=view, status=status, limit=limit, cursor=cursor
        )

    async def get(self, issuer: str, subject: str, notification_id: UUID) -> Notification:
        return await self.repository.get(issuer, subject, notification_id)

    async def acknowledge(
        self,
        issuer: str,
        subject: str,
        notification_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Notification:
        return await self.repository.acknowledge(
            issuer,
            subject,
            notification_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def create_briefing_notification(
        self, command: CreateBriefingNotification
    ) -> Notification:
        return await self.repository.create_briefing_notification(command)

    async def deliver_briefing_notification(
        self, command: CreateBriefingNotification
    ) -> Notification:
        """Stable delivery verb used by the later Briefings application."""

        return await self.create_briefing_notification(command)

    async def deliver_due(self, *, limit: int = 100) -> DueDeliveryResult:
        if not 1 <= limit <= 100:
            raise ValueError("notification delivery batch must contain 1 to 100 reminders")
        return await self.repository.deliver_due(limit=limit)

    async def fire_due(self, *, limit: int = 100) -> DueDeliveryResult:
        return await self.deliver_due(limit=limit)

    async def replay(
        self, issuer: str, subject: str, *, after: UUID | None = None
    ) -> tuple[NotificationReplayEvent, ...]:
        return await self.repository.replay(issuer, subject, after=after)

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        return await self.repository.cursor_is_authorized(issuer, subject, event_id)

    def stream(
        self, issuer: str, subject: str, *, after: UUID | None = None
    ) -> AsyncIterator[NotificationReplayEvent]:
        return self.repository.stream(issuer, subject, after=after)

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        return await self.repository.wait_for_wakeup(timeout)


_CURSOR_SECRET = b"aura-notification-cursor-v1"


def encode_cursor(issuer: str, subject: str, stamp: datetime, notification_id: UUID) -> str:
    body = {
        "v": 1,
        "o": hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest(),
        "t": stamp.astimezone(UTC).isoformat(),
        "id": str(notification_id),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["s"] = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest()
    return (
        base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(cursor: str, issuer: str, subject: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()))
        signature = str(body.pop("s"))
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest()
        owner = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
        if not hmac.compare_digest(signature, expected) or body.get("o") != owner:
            raise ValueError
        if body.get("v") != 1:
            raise ValueError
        return datetime.fromisoformat(str(body["t"])).astimezone(UTC), UUID(str(body["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("invalid notification cursor") from exc


def as_payload(item: Notification) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(item.id),
        "status": item.status.value,
        "version": item.version,
        "createdAt": item.created_at.astimezone(UTC).isoformat(),
        "updatedAt": item.updated_at.astimezone(UTC).isoformat(),
        "deliveredAt": item.delivered_at.astimezone(UTC).isoformat(),
        "acknowledgedAt": item.acknowledged_at.astimezone(UTC).isoformat()
        if item.acknowledged_at
        else None,
    }
    # Existing reminder clients continue to receive their established field.
    # It is absent, rather than null, for briefing notifications.
    if item.reminder_id is not None:
        payload["reminderId"] = str(item.reminder_id)
    elif isinstance(item.source, BriefingNotificationSource):
        payload.update(
            {
                "briefingScheduleId": str(item.source.briefing_schedule_id),
                "briefingOccurrenceId": str(item.source.briefing_occurrence_id),
                "briefingResultId": str(item.source.briefing_result_id),
            }
        )
    return payload


__all__ = [
    "DueDeliveryResult",
    "BriefingNotificationSource",
    "CreateBriefingNotification",
    "DeliverBriefingNotification",
    "Notification",
    "NotificationApplicationService",
    "NotificationError",
    "NotificationIdempotencyConflict",
    "NotificationNotFound",
    "NotificationPage",
    "NotificationReplayEvent",
    "NotificationRepository",
    "NotificationStatus",
    "NotificationSource",
    "NotificationSourceKind",
    "NotificationVersionConflict",
    "NotificationView",
    "ReminderNotificationSource",
    "NOTIFICATION_COMPONENT",
    "NOTIFICATION_TOPIC",
    "as_payload",
    "decode_cursor",
    "encode_cursor",
]
