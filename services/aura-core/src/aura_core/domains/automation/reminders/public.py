"""Public commands, DTOs, and errors for one-shot reminders.

The domain deliberately has no provider dependency.  Persistence adapters are
composed by ``bootstrap.reminder_uow`` and all operations take an explicit
authenticated principal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

REMINDER_COMPONENT = "aura.automation.reminder_management"
REMINDER_TOPIC = "aura.automation.reminders.v1"
REMINDER_CURSOR_VERSION = 1
_CURSOR_SECRET = b"aura-reminder-cursor-v1"


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    FIRED = "fired"
    COMPLETED = "completed"
    CANCELED = "canceled"


class ReminderView(StrEnum):
    UPCOMING = "upcoming"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class Reminder:
    id: UUID
    issuer: str
    subject: str
    content: str
    due_at: datetime
    status: ReminderStatus
    version: int
    created_at: datetime
    updated_at: datetime
    fired_at: datetime | None = None
    completed_at: datetime | None = None
    canceled_at: datetime | None = None

    def with_status(self, status: ReminderStatus, now: datetime) -> Reminder:
        values: dict[str, object] = {
            "status": status,
            "version": self.version + 1,
            "updated_at": now,
        }
        if status is ReminderStatus.FIRED:
            values["fired_at"] = now
        elif status is ReminderStatus.COMPLETED:
            values["completed_at"] = now
        elif status is ReminderStatus.CANCELED:
            values["canceled_at"] = now
        return replace(self, **values)


@dataclass(frozen=True, slots=True)
class ReminderPage:
    items: tuple[Reminder, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ReminderProposal:
    """Validated private arguments awaiting a governance decision.

    This is a public automation DTO.  Its persistence adapter remains private
    to automation; governance receives only this port and never imports the
    proposal table mapping.
    """

    id: UUID
    issuer: str
    subject: str
    content: str
    due_at: datetime
    run_id: UUID
    conversation_id: UUID
    tool_call_id: UUID
    created_at: datetime
    expires_at: datetime


class ReminderProposalPort(Protocol):
    async def create(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        due_at: datetime,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        now: datetime | None = None,
        transaction: object | None = None,
    ) -> ReminderProposal: ...

    async def get(self, issuer: str, subject: str, proposal_id: UUID) -> ReminderProposal: ...


class ReminderError(Exception):
    """Base class for deterministic reminder failures."""


class ReminderNotFound(ReminderError):
    pass


class ReminderValidationError(ReminderError):
    pass


class ReminderVersionConflict(ReminderError):
    pass


class ReminderIdempotencyConflict(ReminderError):
    pass


class ReminderTransitionError(ReminderError):
    pass


class ReminderDeliveryPort(Protocol):
    """Transaction-scoped public port for scheduler-owned firing."""

    async def fire_due(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[Reminder, ...]: ...


class ReminderRepository(ReminderDeliveryPort, Protocol):
    async def create(
        self, issuer: str, subject: str, *, content: str, due_at: datetime, idempotency_key: str
    ) -> Reminder: ...

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder: ...

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: ReminderView,
        limit: int,
        cursor: str | None = None,
    ) -> ReminderPage: ...

    async def complete(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder: ...

    async def cancel(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder: ...


class ReminderApplicationService:
    """Shared reminder use cases consumed by HTTP and future tool/scheduler entrypoints."""

    def __init__(
        self, repository: ReminderRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        due_at: datetime,
        idempotency_key: str,
    ) -> Reminder:
        return await self.repository.create(
            issuer,
            subject,
            content=normalize_content(content),
            # Canonicalization is safe before idempotency lookup.  The
            # repository applies the relative-time rule only for a new
            # command, so an exact replay remains deterministic after dueAt.
            due_at=canonical_due_at(due_at),
            idempotency_key=idempotency_key,
        )

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder:
        return await self.repository.get(issuer, subject, reminder_id)

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: ReminderView,
        limit: int,
        cursor: str | None = None,
    ) -> ReminderPage:
        return await self.repository.list(
            issuer, subject, view=view, limit=limit, cursor=cursor
        )

    async def complete(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder:
        return await self.repository.complete(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def cancel(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder:
        return await self.repository.cancel(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

def normalize_content(value: str) -> str:
    content = value.strip()
    if not 1 <= len(content) <= 2000:
        raise ReminderValidationError("reminder content must contain 1 to 2000 characters")
    return content


def canonical_due_at(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReminderValidationError("dueAt must include an explicit RFC3339 offset")
    return value.astimezone(UTC)


def ensure_due_at_future(value: datetime, *, now: datetime | None = None) -> datetime:
    normalized = canonical_due_at(value)
    comparison_value = now or datetime.now(UTC)
    comparison = canonical_due_at(comparison_value)
    if normalized <= comparison:
        raise ReminderValidationError("dueAt must be in the future")
    return normalized


def normalize_due_at(value: datetime, *, now: datetime | None = None) -> datetime:
    """Canonicalize an input timestamp and enforce first-execution freshness."""

    return ensure_due_at_future(value, now=now)


def command_fingerprint(operation: str, reminder_id: UUID | None, **values: object) -> str:
    payload: dict[str, object] = {
        "operation": operation,
        "reminderId": str(reminder_id) if reminder_id else None,
    }
    for key, value in values.items():
        payload[key] = value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(
    issuer: str, subject: str, view: ReminderView, stamp: datetime, reminder_id: UUID
) -> str:
    owner = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
    body = {
        "v": REMINDER_CURSOR_VERSION,
        "o": owner,
        "view": view.value,
        "t": stamp.astimezone(UTC).isoformat(),
        "id": str(reminder_id),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest()
    body["s"] = signature
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def decode_cursor(
    cursor: str, issuer: str, subject: str, view: ReminderView
) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()))
        signature = str(body.pop("s"))
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest()
        owner = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        if body != {"v": 1, "o": owner, "view": view.value, "t": body["t"], "id": body["id"]}:
            raise ValueError
        return datetime.fromisoformat(str(body["t"])).astimezone(UTC), UUID(str(body["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise ReminderValidationError("invalid reminder cursor") from exc


def event_command_id(reminder_id: UUID, status: ReminderStatus, version: int) -> UUID:
    from uuid import uuid5

    return uuid5(reminder_id, f"reminder:{status.value}:{version}")


def as_payload(item: Reminder) -> dict[str, object]:
    return {
        "id": str(item.id),
        "content": item.content,
        "dueAt": item.due_at.astimezone(UTC).isoformat(),
        "status": item.status.value,
        "version": item.version,
        "createdAt": item.created_at.astimezone(UTC).isoformat(),
        "updatedAt": item.updated_at.astimezone(UTC).isoformat(),
        "firedAt": item.fired_at.astimezone(UTC).isoformat() if item.fired_at else None,
        "completedAt": item.completed_at.astimezone(UTC).isoformat()
        if item.completed_at
        else None,
        "canceledAt": item.canceled_at.astimezone(UTC).isoformat() if item.canceled_at else None,
    }


def from_payload(
    payload: dict[str, object], *, issuer: str = "", subject: str = ""
) -> Reminder:
    def stamp(name: str) -> datetime | None:
        value = payload.get(name)
        return datetime.fromisoformat(str(value)) if value is not None else None

    return Reminder(
        UUID(str(payload["id"])),
        issuer,
        subject,
        str(payload["content"]),
        datetime.fromisoformat(str(payload["dueAt"])),
        ReminderStatus(str(payload["status"])),
        int(str(payload["version"])),
        datetime.fromisoformat(str(payload["createdAt"])),
        datetime.fromisoformat(str(payload["updatedAt"])),
        stamp("firedAt"),
        stamp("completedAt"),
        stamp("canceledAt"),
    )


__all__ = [
    "REMINDER_COMPONENT",
    "REMINDER_TOPIC",
    "Reminder",
    "ReminderApplicationService",
    "ReminderError",
    "ReminderIdempotencyConflict",
    "ReminderNotFound",
    "ReminderPage",
    "ReminderRepository",
    "ReminderDeliveryPort",
    "ReminderStatus",
    "ReminderTransitionError",
    "ReminderValidationError",
    "ReminderVersionConflict",
    "ReminderView",
    "as_payload",
    "command_fingerprint",
    "decode_cursor",
    "encode_cursor",
    "event_command_id",
    "from_payload",
    "normalize_content",
    "canonical_due_at",
    "ensure_due_at_future",
    "normalize_due_at",
]
