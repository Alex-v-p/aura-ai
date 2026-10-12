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
from typing import Protocol
from uuid import UUID

NOTIFICATION_COMPONENT = "aura.automation.notification_delivery"
NOTIFICATION_TOPIC = "aura.automation.notifications.v1"


class NotificationStatus(StrEnum):
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"


class NotificationView(StrEnum):
    ALL = "all"
    UNREAD = "unread"


@dataclass(frozen=True, slots=True)
class Notification:
    id: UUID
    issuer: str
    subject: str
    reminder_id: UUID
    status: NotificationStatus
    version: int
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime
    acknowledged_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NotificationReplayEvent:
    id: UUID
    notification_id: UUID
    reminder_id: UUID
    status: NotificationStatus
    version: int
    occurred_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": str(self.id),
            "eventType": f"notification.{self.status.value}",
            "notificationId": str(self.notification_id),
            "reminderId": str(self.reminder_id),
            "status": self.status.value,
            "notificationVersion": self.version,
            "occurredAt": self.occurred_at.astimezone(UTC).isoformat(),
        }


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
    return {
        "id": str(item.id),
        "reminderId": str(item.reminder_id),
        "status": item.status.value,
        "version": item.version,
        "createdAt": item.created_at.astimezone(UTC).isoformat(),
        "updatedAt": item.updated_at.astimezone(UTC).isoformat(),
        "deliveredAt": item.delivered_at.astimezone(UTC).isoformat(),
        "acknowledgedAt": item.acknowledged_at.astimezone(UTC).isoformat()
        if item.acknowledged_at
        else None,
    }


__all__ = [
    "DueDeliveryResult",
    "Notification",
    "NotificationApplicationService",
    "NotificationError",
    "NotificationIdempotencyConflict",
    "NotificationNotFound",
    "NotificationPage",
    "NotificationReplayEvent",
    "NotificationRepository",
    "NotificationStatus",
    "NotificationVersionConflict",
    "NotificationView",
    "NOTIFICATION_COMPONENT",
    "NOTIFICATION_TOPIC",
    "as_payload",
    "decode_cursor",
    "encode_cursor",
]
