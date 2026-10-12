"""In-memory and PostgreSQL notification repositories.

The PostgreSQL delivery operation owns the single transaction that claims due
reminders, fires them, creates the inbox row, persists the replay cursor, and
stages the identifier-only reminder event.  This keeps scheduler entrypoints
thin and makes a crash before commit safe to retry.
"""

# Repository SQL and DTO constructors stay adjacent for transaction auditing.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.notifications.persistence import (
    NotificationEventRow,
    NotificationIdempotencyRow,
    NotificationRow,
)
from aura_core.domains.automation.notifications.public import (
    NOTIFICATION_TOPIC,
    DueDeliveryResult,
    Notification,
    NotificationIdempotencyConflict,
    NotificationNotFound,
    NotificationPage,
    NotificationReplayEvent,
    NotificationStatus,
    NotificationVersionConflict,
    NotificationView,
    as_payload,
    decode_cursor,
    encode_cursor,
)
from aura_core.domains.automation.reminders.public import (
    REMINDER_TOPIC,
    Reminder,
    ReminderDeliveryPort,
    ReminderStatus,
    event_command_id,
)
from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.platform.outbox import SqlOutboxRepository, make_aggregate_command
from aura_core.platform.telemetry import MetadataMetrics, new_span_id

SCHEDULER_COMPONENT = "aura.automation.reminder_scheduler"


def _now() -> datetime:
    return datetime.now(UTC)


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject:
        raise ValueError("authenticated owner is required")
    return issuer, subject


def _idempotency_lock_key(issuer: str, subject: str, idempotency_key: str) -> str:
    """Return a stable, NUL-free advisory-lock key for one owner command."""

    encoded = json.dumps(
        [issuer, subject, idempotency_key],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"aura.notification:{encoded}"


def _notification(row: NotificationRow | dict[str, object]) -> Notification:
    def value(name: str) -> object:
        return row[name] if isinstance(row, dict) else getattr(row, name)

    def stamp(name: str) -> datetime:
        item = value(name)
        return item.astimezone(UTC) if isinstance(item, datetime) else _now()

    acknowledged = value("acknowledged_at")
    return Notification(
        UUID(str(value("id"))),
        str(value("principal_issuer")),
        str(value("principal_subject")),
        UUID(str(value("reminder_id"))),
        NotificationStatus(str(value("status"))),
        int(str(value("version"))),
        stamp("created_at"),
        stamp("updated_at"),
        stamp("delivered_at"),
        acknowledged.astimezone(UTC) if isinstance(acknowledged, datetime) else None,
    )


def _replay(row: NotificationEventRow | dict[str, object]) -> NotificationReplayEvent:
    def value(name: str) -> object:
        return row[name] if isinstance(row, dict) else getattr(row, name)

    occurred = value("occurred_at")
    return NotificationReplayEvent(
        UUID(str(value("id"))),
        UUID(str(value("notification_id"))),
        UUID(str(value("reminder_id"))),
        NotificationStatus(str(value("status"))),
        int(str(value("version"))),
        occurred.astimezone(UTC) if isinstance(occurred, datetime) else _now(),
    )


def _notification_from_response(
    response: dict[str, object], issuer: str, subject: str
) -> Notification:
    def stamp(name: str) -> datetime:
        return datetime.fromisoformat(str(response[name])).astimezone(UTC)

    acknowledged = response.get("acknowledgedAt")
    return Notification(
        UUID(str(response["id"])),
        issuer,
        subject,
        UUID(str(response["reminderId"])),
        NotificationStatus(str(response["status"])),
        int(str(response["version"])),
        stamp("createdAt"),
        stamp("updatedAt"),
        stamp("deliveredAt"),
        datetime.fromisoformat(str(acknowledged)).astimezone(UTC)
        if acknowledged is not None
        else None,
    )


def _fingerprint(operation: str, notification_id: UUID, expected_version: int) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "operation": operation,
                "notificationId": str(notification_id),
                "expectedVersion": expected_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class MemoryNotificationRepository:
    """Deterministic composition used by API and scheduler unit tests."""

    def __init__(
        self,
        reminder_repository: object | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        self.reminder_repository = reminder_repository
        self._clock = clock or _now
        self.metrics = metrics
        self._items: dict[UUID, Notification] = {}
        self._events: list[NotificationReplayEvent] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, Notification]] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._last_wakeup_id: UUID | None = None

    def _time(self) -> datetime:
        current = self._clock()
        return current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)

    def _metric(self, name: str, **values: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(SCHEDULER_COMPONENT, name, **values)

    async def notify_wakeup(self, event_id: UUID) -> None:
        self._last_wakeup_id = event_id
        self._wake.set()
        self._wake = asyncio.Event()

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        wake = self._wake
        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def list(self, issuer: str, subject: str, *, view: NotificationView = NotificationView.ALL, status: NotificationStatus | None = None, limit: int, cursor: str | None = None) -> NotificationPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject) if cursor else None
        values = [item for item in self._items.values() if (item.issuer, item.subject) == (issuer, subject)]
        if view is NotificationView.UNREAD or status is NotificationStatus.DELIVERED:
            values = [item for item in values if item.status is NotificationStatus.DELIVERED]
        elif status is NotificationStatus.ACKNOWLEDGED:
            values = [item for item in values if item.status is NotificationStatus.ACKNOWLEDGED]
        values.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        if stamp_id:
            stamp, item_id = stamp_id
            values = [item for item in values if (item.created_at, item.id) < (stamp, item_id)]
        page = values[:limit]
        next_cursor = None
        if len(values) > limit and page:
            item = page[-1]
            next_cursor = encode_cursor(issuer, subject, item.created_at, item.id)
        unacknowledged = sum(
            item.status is NotificationStatus.DELIVERED
            for item in self._items.values()
            if (item.issuer, item.subject) == (issuer, subject)
        )
        return NotificationPage(tuple(page), next_cursor, unacknowledged)

    async def get(self, issuer: str, subject: str, notification_id: UUID) -> Notification:
        issuer, subject = _owner(issuer, subject)
        item = self._items.get(notification_id)
        if item is None or (item.issuer, item.subject) != (issuer, subject):
            raise NotificationNotFound("notification not found")
        return item

    async def acknowledge(self, issuer: str, subject: str, notification_id: UUID, *, expected_version: int, idempotency_key: str) -> Notification:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = await self.get(issuer, subject, notification_id)
            fingerprint = _fingerprint("acknowledge", notification_id, expected_version)
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    raise NotificationIdempotencyConflict("idempotency key was already used")
                return existing[1]
            if item.version != expected_version:
                raise NotificationVersionConflict("notification version does not match")
            if item.status is NotificationStatus.ACKNOWLEDGED:
                raise NotificationVersionConflict("notification is already acknowledged")
            now = self._time()
            updated = Notification(item.id, item.issuer, item.subject, item.reminder_id, NotificationStatus.ACKNOWLEDGED, item.version + 1, item.created_at, now, item.delivered_at, now)
            self._items[item.id] = updated
            event = NotificationReplayEvent(uuid4(), updated.id, updated.reminder_id, updated.status, updated.version, now)
            self._events.append(event)
            self._idempotency[(issuer, subject, idempotency_key)] = (fingerprint, updated)
            self._wake.set()
            self._wake = asyncio.Event()
            return updated

    async def deliver_due(self, *, limit: int = 100) -> DueDeliveryResult:
        if not 1 <= limit <= 100:
            raise ValueError("notification delivery batch must contain 1 to 100 reminders")
        async with self._lock:
            now = self._time()
            delivery = self.reminder_repository
            fire_due = getattr(delivery, "fire_due", None)
            if not callable(fire_due):
                raise RuntimeError("reminder delivery port is not configured")
            fire = cast(
                Callable[..., Awaitable[tuple[Reminder, ...]]],
                fire_due,
            )
            fired_reminders = tuple(
                await fire(now=now, limit=limit)
            )
            fired: list[Notification] = []
            duplicates = 0
            for reminder in fired_reminders:
                existing = next((item for item in self._items.values() if item.reminder_id == reminder.id), None)
                if existing is not None:
                    duplicates += 1
                    continue
                notification = Notification(uuid4(), reminder.issuer, reminder.subject, reminder.id, NotificationStatus.DELIVERED, 1, reminder.created_at, now, now)
                self._items[notification.id] = notification
                self._events.append(NotificationReplayEvent(uuid4(), notification.id, reminder.id, notification.status, notification.version, now))
                fired.append(notification)
            if fired:
                self._wake.set()
                self._wake = asyncio.Event()
            lag = max((max(0.0, (now - item.due_at).total_seconds() * 1000) for item in fired_reminders), default=0.0)
            if duplicates:
                if self.metrics is not None:
                    self.metrics.observe(
                        SCHEDULER_COMPONENT,
                        "reminder_duplicate_prevention_count",
                        float(duplicates),
                        outcome="duplicate",
                    )
            return DueDeliveryResult(tuple(fired), len(fired_reminders), duplicates, lag)

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[NotificationReplayEvent, ...]:
        issuer, subject = _owner(issuer, subject)
        events = [event for event in self._events if (self._items[event.notification_id].issuer, self._items[event.notification_id].subject) == (issuer, subject)]
        if after is None:
            return tuple(events)
        positions = [index for index, event in enumerate(events) if event.id == after]
        return tuple(events[positions[0] + 1 :]) if positions else tuple()

    async def fire_due(self, *, limit: int = 100) -> DueDeliveryResult:
        return await self.deliver_due(limit=limit)

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        issuer, subject = _owner(issuer, subject)
        return any(event.id == event_id and (self._items[event.notification_id].issuer, self._items[event.notification_id].subject) == (issuer, subject) for event in self._events)

    async def stream(self, issuer: str, subject: str, *, after: UUID | None = None) -> AsyncIterator[NotificationReplayEvent]:
        cursor = after
        while True:
            events = await self.replay(issuer, subject, after=cursor)
            if events:
                for event in events:
                    cursor = event.id
                    yield event
                continue
            wake = self._wake
            try:
                await asyncio.wait_for(wake.wait(), timeout=15.0)
            except TimeoutError:
                # No id means a heartbeat cannot advance the durable cursor.
                continue


class SqlNotificationRepository:
    """PostgreSQL adapter for durable inbox delivery and owner replay."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, metrics: MetadataMetrics | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.sessions = sessions
        self.metrics = metrics
        self._clock = clock or _now
        self._outbox = SqlOutboxRepository()
        self._audit = SqlAuditRepository()
        self._identities = SqlIdentityRepository()
        self._wake = asyncio.Event()
        self._last_wakeup_id: UUID | None = None
        self._reminder_delivery: ReminderDeliveryPort | None = None

    def set_reminder_delivery(self, delivery: ReminderDeliveryPort) -> None:
        self._reminder_delivery = delivery

    def _time(self) -> datetime:
        current = self._clock()
        return current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)

    def _metric(self, name: str, **values: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(SCHEDULER_COMPONENT, name, **values)

    def _record_fire_span(
        self, started: float, outcome: str, error_class: str | None = None
    ) -> None:
        if self.metrics is not None:
            self.metrics.record_span(
                SCHEDULER_COMPONENT,
                "reminder.fire",
                (monotonic() - started) * 1000,
                trace_id=uuid4().hex,
                span_id=new_span_id(),
                parent_span_id=None,
                dependency="postgresql",
                outcome=outcome,
                error_class=error_class,
            )

    @staticmethod
    def _outbox_command(event_id: UUID, item: Notification):
        return make_aggregate_command(
            NOTIFICATION_TOPIC,
            event_id,
            "automation.notification",
            item.id,
            identifiers=(
                ("eventType", f"notification.{item.status.value}"),
                ("status", item.status.value),
                ("version", item.version),
                ("eventId", event_id),
            ),
        )

    async def notify_wakeup(self, event_id: UUID) -> None:
        self._last_wakeup_id = event_id
        self._wake.set()
        self._wake = asyncio.Event()

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        wake = self._wake
        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def list(self, issuer: str, subject: str, *, view: NotificationView = NotificationView.ALL, status: NotificationStatus | None = None, limit: int, cursor: str | None = None) -> NotificationPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject) if cursor else None
        async with self.sessions() as session:
            query = select(NotificationRow).where(NotificationRow.principal_issuer == issuer, NotificationRow.principal_subject == subject)
            if view is NotificationView.UNREAD or status is NotificationStatus.DELIVERED:
                query = query.where(NotificationRow.status == NotificationStatus.DELIVERED.value)
            elif status is NotificationStatus.ACKNOWLEDGED:
                query = query.where(NotificationRow.status == NotificationStatus.ACKNOWLEDGED.value)
            query = query.order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
            if stamp_id:
                stamp, item_id = stamp_id
                query = query.where((NotificationRow.created_at < stamp) | ((NotificationRow.created_at == stamp) & (NotificationRow.id < item_id)))
            rows = list((await session.execute(query.limit(limit + 1))).scalars().all())
            items = tuple(_notification(row) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit and items:
                last = items[-1]
                next_cursor = encode_cursor(issuer, subject, last.created_at, last.id)
            unread_rows = await session.execute(
                select(NotificationRow.id).where(
                    NotificationRow.principal_issuer == issuer,
                    NotificationRow.principal_subject == subject,
                    NotificationRow.status == NotificationStatus.DELIVERED.value,
                )
            )
            count = len(unread_rows.scalars().all())
            return NotificationPage(items, next_cursor, count)

    async def get(self, issuer: str, subject: str, notification_id: UUID) -> Notification:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = (await session.execute(select(NotificationRow).where(NotificationRow.id == notification_id, NotificationRow.principal_issuer == issuer, NotificationRow.principal_subject == subject))).scalar_one_or_none()
            if row is None:
                raise NotificationNotFound("notification not found")
            return _notification(row)

    async def acknowledge(self, issuer: str, subject: str, notification_id: UUID, *, expected_version: int, idempotency_key: str) -> Notification:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            fingerprint = _fingerprint("acknowledge", notification_id, expected_version)
            existing = await session.get(NotificationIdempotencyRow, (issuer, subject, idempotency_key))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise NotificationIdempotencyConflict("idempotency key was already used")
                return _notification_from_response(existing.response, issuer, subject)
            row = (await session.execute(select(NotificationRow).where(NotificationRow.id == notification_id, NotificationRow.principal_issuer == issuer, NotificationRow.principal_subject == subject).with_for_update())).scalar_one_or_none()
            if row is None:
                raise NotificationNotFound("notification not found")
            if row.version != expected_version or row.status != NotificationStatus.DELIVERED.value:
                raise NotificationVersionConflict("notification version does not match")
            now = self._time()
            row.status = NotificationStatus.ACKNOWLEDGED.value
            row.version += 1
            row.updated_at = now
            row.acknowledged_at = now
            item = _notification(row)
            event_id = uuid4()
            event = NotificationEventRow(id=event_id, principal_issuer=issuer, principal_subject=subject, notification_id=item.id, reminder_id=item.reminder_id, status=item.status.value, version=item.version, occurred_at=now)
            session.add(event)
            principal = await self._identities.resolve(session, issuer, subject)
            self._audit.stage(session, principal.id, "notification.acknowledge", str(item.id), {"status": item.status.value, "version": item.version})
            await self._outbox.stage_command(
                session,
                command=self._outbox_command(event_id, item),
            )
            session.add(NotificationIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, notification_id=item.id, response=as_payload(item), created_at=now))
            self._wake.set()
            self._wake = asyncio.Event()
            return item

    async def deliver_due(self, *, limit: int = 100) -> DueDeliveryResult:
        if not 1 <= limit <= 100:
            raise ValueError("notification delivery batch must contain 1 to 100 reminders")
        started = monotonic()
        now = self._time()
        fired: list[Notification] = []
        duplicates = 0
        max_lag = 0.0
        try:
            async with self.sessions() as session, session.begin():
                if self._reminder_delivery is None:
                    raise RuntimeError("reminder delivery port is not configured")
                fired_reminders = await self._reminder_delivery.fire_due(
                    transaction=session, now=now, limit=limit
                )
                for reminder in fired_reminders:
                    reminder_id = reminder.id
                    max_lag = max(max_lag, max(0.0, (now - reminder.due_at).total_seconds() * 1000))
                    result = (await session.execute(text("INSERT INTO notifications (id, principal_issuer, principal_subject, reminder_id, status, version, created_at, updated_at, delivered_at) VALUES (:id, :issuer, :subject, :reminder_id, 'delivered', 1, :now, :now, :now) ON CONFLICT (reminder_id) DO NOTHING RETURNING id"), {"id": uuid4(), "issuer": reminder.issuer, "subject": reminder.subject, "reminder_id": reminder_id, "now": now})).first()
                    if result is None:
                        duplicates += 1
                        continue
                    notification_id = UUID(str(result[0]))
                    notification = Notification(notification_id, reminder.issuer, reminder.subject, reminder_id, NotificationStatus.DELIVERED, 1, reminder.created_at, now, now)
                    event_id = uuid4()
                    session.add(NotificationEventRow(id=event_id, principal_issuer=notification.issuer, principal_subject=notification.subject, notification_id=notification.id, reminder_id=notification.reminder_id, status=notification.status.value, version=notification.version, occurred_at=now))
                    principal = await self._identities.resolve(session, notification.issuer, notification.subject)
                    self._audit.stage(session, principal.id, "notification.delivered", str(notification.id), {"status": notification.status.value, "version": notification.version})
                    command = make_aggregate_command(REMINDER_TOPIC, event_command_id(reminder_id, ReminderStatus.FIRED, reminder.version), "automation.reminder", reminder_id, identifiers=(("eventType", "reminder.fired"), ("status", "fired"), ("version", reminder.version), ("eventId", event_id)))
                    await self._outbox.stage_command(session, command=command)
                    fired.append(notification)
        except Exception:
            self._metric("reminder_fire_persistence_outcome", outcome="error")
            self._record_fire_span(started, "error", "persistence")
            raise
        if duplicates and self.metrics is not None:
            self.metrics.observe(
                SCHEDULER_COMPONENT,
                "reminder_duplicate_prevention_count",
                float(duplicates),
                outcome="duplicate",
            )
        if fired:
            self._wake.set()
            self._wake = asyncio.Event()
        self._metric("reminder_fire_persistence_outcome", outcome="ok")
        self._record_fire_span(started, "ok")
        return DueDeliveryResult(tuple(fired), len(fired_reminders), duplicates, max_lag)

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[NotificationReplayEvent, ...]:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            query = select(NotificationEventRow).where(NotificationEventRow.principal_issuer == issuer, NotificationEventRow.principal_subject == subject).order_by(NotificationEventRow.occurred_at, NotificationEventRow.id)
            if after is not None:
                cursor = await session.get(NotificationEventRow, after)
                if cursor is not None and (cursor.principal_issuer != issuer or cursor.principal_subject != subject):
                    return ()
                if cursor is not None:
                    query = query.where((NotificationEventRow.occurred_at > cursor.occurred_at) | ((NotificationEventRow.occurred_at == cursor.occurred_at) & (NotificationEventRow.id > cursor.id)))
            return tuple(_replay(row) for row in (await session.execute(query)).scalars().all())

    async def fire_due(self, *, limit: int = 100) -> DueDeliveryResult:
        return await self.deliver_due(limit=limit)

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = await session.get(NotificationEventRow, event_id)
            return row is not None and row.principal_issuer == issuer and row.principal_subject == subject

    async def stream(self, issuer: str, subject: str, *, after: UUID | None = None) -> AsyncIterator[NotificationReplayEvent]:
        cursor = after
        while True:
            events = await self.replay(issuer, subject, after=cursor)
            if events:
                for event in events:
                    cursor = event.id
                    yield event
                continue
            wake = self._wake
            try:
                await asyncio.wait_for(wake.wait(), timeout=15.0)
            except TimeoutError:
                continue


__all__ = ["MemoryNotificationRepository", "SqlNotificationRepository"]
