"""In-memory and PostgreSQL reminder repositories."""

# Repository calls intentionally keep the transactional SQL expressions close
# to their domain operation; long signatures remain easier to audit together.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.reminders.persistence import (
    ReminderIdempotencyRow,
    ReminderRow,
)
from aura_core.domains.automation.reminders.public import (
    REMINDER_COMPONENT,
    REMINDER_TOPIC,
    Reminder,
    ReminderIdempotencyConflict,
    ReminderNotFound,
    ReminderPage,
    ReminderStatus,
    ReminderTransitionError,
    ReminderValidationError,
    ReminderVersionConflict,
    ReminderView,
    as_payload,
    canonical_due_at,
    command_fingerprint,
    decode_cursor,
    encode_cursor,
    ensure_due_at_future,
    event_command_id,
    from_payload,
    normalize_content,
)
from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.platform.outbox import SqlOutboxRepository, make_aggregate_command
from aura_core.platform.telemetry import MetadataMetrics, Stopwatch, new_span_id


def _now() -> datetime:
    return datetime.now(UTC)


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject:
        raise ReminderValidationError("authenticated owner is required")
    return issuer, subject


def _idempotency_lock_key(issuer: str, subject: str, idempotency_key: str) -> str:
    """Return a stable, non-sensitive advisory-lock key for one owner command."""

    return f"aura.reminder:{issuer}\0{subject}\0{idempotency_key}"


def _item(row: ReminderRow) -> Reminder:
    return Reminder(
        row.id,
        row.principal_issuer,
        row.principal_subject,
        row.content,
        row.due_at.astimezone(UTC),
        ReminderStatus(row.status),
        row.version,
        row.created_at.astimezone(UTC),
        row.updated_at.astimezone(UTC),
        row.fired_at.astimezone(UTC) if row.fired_at else None,
        row.completed_at.astimezone(UTC) if row.completed_at else None,
        row.canceled_at.astimezone(UTC) if row.canceled_at else None,
    )


class MemoryReminderRepository:
    """Deterministic repository used by tests and local in-process composition."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        self._clock = clock or _now
        self._items: dict[UUID, Reminder] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, Reminder]] = {}
        self._lock = asyncio.Lock()
        self.events: list[dict[str, object]] = []
        self.audit: list[dict[str, object]] = []
        self.metrics = metrics

    def _conflict(self, name: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                "aura.automation.reminder_management",
                name,
                dependency="reminder_store",
                outcome="conflict",
            )

    def _time(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def _record(
        self, issuer: str, subject: str, key: str, fingerprint: str, item: Reminder, operation: str
    ) -> Reminder:
        self._idempotency[(issuer, subject, key)] = (fingerprint, item)
        self.events.append(
            {
                "topic": REMINDER_TOPIC,
                "aggregateType": "automation.reminder",
                "aggregateId": str(item.id),
                "eventType": f"reminder.{operation}",
                "status": item.status.value,
                "version": str(item.version),
            }
        )
        self.audit.append(
            {"action": f"reminder.{operation}", "resource": str(item.id), "metadata": {"status": item.status.value}}
        )
        return item

    async def create(self, issuer: str, subject: str, *, content: str, due_at: datetime, idempotency_key: str) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        normalized_content = normalize_content(content)
        due = canonical_due_at(due_at)
        async with self._lock:
            fingerprint = command_fingerprint("create", None, content=normalized_content, dueAt=due.isoformat())
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return existing[1]
            due = ensure_due_at_future(due, now=self._time())
            now = self._time()
            item = Reminder(uuid4(), issuer, subject, normalized_content, due, ReminderStatus.SCHEDULED, 1, now, now)
            self._items[item.id] = item
            return self._record(issuer, subject, idempotency_key, fingerprint, item, "created")

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        item = self._items.get(reminder_id)
        if item is None or (item.issuer, item.subject) != (issuer, subject):
            raise ReminderNotFound("reminder not found")
        return item

    async def list(self, issuer: str, subject: str, *, view: ReminderView, limit: int, cursor: str | None = None) -> ReminderPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject, view) if cursor else None
        values = [item for item in self._items.values() if (item.issuer, item.subject) == (issuer, subject)]
        if view is ReminderView.UPCOMING:
            values = [item for item in values if item.status is ReminderStatus.SCHEDULED]
            values.sort(key=lambda item: (item.due_at, item.id))
            if stamp_id:
                values = [item for item in values if (item.due_at, item.id) > stamp_id]
        else:
            values = [item for item in values if item.status is not ReminderStatus.SCHEDULED]
            values.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            if stamp_id:
                values = [item for item in values if (item.updated_at, item.id) < stamp_id]
        page = values[:limit]
        next_cursor = None
        if len(values) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(issuer, subject, view, last.due_at if view is ReminderView.UPCOMING else last.updated_at, last.id)
        return ReminderPage(tuple(page), next_cursor)

    async def _transition(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str, target: ReminderStatus) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(reminder_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject):
                raise ReminderNotFound("reminder not found")
            fingerprint = command_fingerprint(target.value, reminder_id, expectedVersion=expected_version)
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return existing[1]
            if item.version != expected_version:
                self._conflict("reminder_version_conflict_count")
                raise ReminderVersionConflict("reminder version does not match")
            allowed = (target is ReminderStatus.COMPLETED and item.status in {ReminderStatus.SCHEDULED, ReminderStatus.FIRED}) or (target is ReminderStatus.CANCELED and item.status is ReminderStatus.SCHEDULED)
            if not allowed:
                raise ReminderTransitionError("reminder lifecycle transition is not allowed")
            updated = item.with_status(target, self._time())
            self._items[reminder_id] = updated
            return self._record(issuer, subject, idempotency_key, fingerprint, updated, target.value)

    async def complete(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str) -> Reminder:
        return await self._transition(issuer, subject, reminder_id, expected_version=expected_version, idempotency_key=idempotency_key, target=ReminderStatus.COMPLETED)

    async def cancel(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str) -> Reminder:
        return await self._transition(issuer, subject, reminder_id, expected_version=expected_version, idempotency_key=idempotency_key, target=ReminderStatus.CANCELED)


class SqlReminderRepository:
    """Transactional PostgreSQL adapter for the public reminder port."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        self.sessions = sessions
        self._clock = clock or _now
        self._outbox = SqlOutboxRepository()
        self._audit = SqlAuditRepository()
        self._identities = SqlIdentityRepository()
        self.metrics = metrics

    def _conflict(self, name: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                "aura.automation.reminder_management",
                name,
                dependency="reminder_store",
                outcome="conflict",
            )

    def _time(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def create(self, issuer: str, subject: str, *, content: str, due_at: datetime, idempotency_key: str) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        content = normalize_content(content)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            due = canonical_due_at(due_at)
            fingerprint = command_fingerprint("create", None, content=content, dueAt=due.isoformat())
            existing = await session.get(ReminderIdempotencyRow, (issuer, subject, idempotency_key))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return from_payload(existing.response)
            due = ensure_due_at_future(due, now=self._time())
            now = self._time()
            row = ReminderRow(id=uuid4(), principal_issuer=issuer, principal_subject=subject, content=content, due_at=due, status=ReminderStatus.SCHEDULED.value, version=1, created_at=now, updated_at=now)
            session.add(row)
            item = _item(row)
            await self._stage(session, item, "created", issuer, subject)
            response = as_payload(item)
            session.add(ReminderIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, reminder_id=item.id, response=response, created_at=now))
            return item

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = (await session.execute(select(ReminderRow).where(ReminderRow.id == reminder_id, ReminderRow.principal_issuer == issuer, ReminderRow.principal_subject == subject))).scalar_one_or_none()
            if row is None:
                raise ReminderNotFound("reminder not found")
            return _item(row)

    async def list(self, issuer: str, subject: str, *, view: ReminderView, limit: int, cursor: str | None = None) -> ReminderPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject, view) if cursor else None
        async with self.sessions() as session:
            query = select(ReminderRow).where(ReminderRow.principal_issuer == issuer, ReminderRow.principal_subject == subject)
            if view is ReminderView.UPCOMING:
                query = query.where(ReminderRow.status == ReminderStatus.SCHEDULED.value).order_by(ReminderRow.due_at, ReminderRow.id)
                if stamp_id:
                    stamp, item_id = stamp_id
                    query = query.where((ReminderRow.due_at > stamp) | ((ReminderRow.due_at == stamp) & (ReminderRow.id > item_id)))
            else:
                query = query.where(ReminderRow.status != ReminderStatus.SCHEDULED.value).order_by(ReminderRow.updated_at.desc(), ReminderRow.id.desc())
                if stamp_id:
                    stamp, item_id = stamp_id
                    query = query.where((ReminderRow.updated_at < stamp) | ((ReminderRow.updated_at == stamp) & (ReminderRow.id < item_id)))
            rows = list((await session.execute(query.limit(limit + 1))).scalars().all())
            items = tuple(_item(row) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit and items:
                last = items[-1]
                next_cursor = encode_cursor(issuer, subject, view, last.due_at if view is ReminderView.UPCOMING else last.updated_at, last.id)
            return ReminderPage(items, next_cursor)

    async def _stage(
        self,
        session: AsyncSession,
        item: Reminder,
        operation: str,
        issuer: str,
        subject: str,
    ) -> None:
        command = make_aggregate_command(
            REMINDER_TOPIC,
            event_command_id(item.id, item.status, item.version),
            "automation.reminder",
            item.id,
            identifiers=(
                ("eventType", f"reminder.{operation}"),
                ("status", item.status.value),
                ("version", item.version),
            ),
        )
        span_operation = {
            "created": "reminder.create",
            "fired": "reminder.fire",
            "completed": "reminder.complete",
            "canceled": "reminder.cancel",
        }.get(operation, "reminder.create")
        timer = Stopwatch()
        try:
            await self._outbox.stage_command(session, command=command)
            principal = await self._identities.resolve(session, issuer, subject)
            self._audit.stage(
                session,
                principal.id,
                f"reminder.{operation}",
                str(item.id),
                {"status": item.status.value, "version": item.version},
            )
        except Exception:
            if self.metrics is not None:
                self.metrics.record_span(
                    REMINDER_COMPONENT,
                    span_operation,
                    timer.elapsed_ms(),
                    trace_id=item.id.hex,
                    span_id=new_span_id(),
                    parent_span_id=None,
                    dependency="postgresql",
                    outcome="error",
                    error_class="persistence",
                    command_id=str(command.id),
                    correlation_id=str(command.correlation_id or command.id),
                    causation_id=str(command.causation_id or item.id),
                    aggregate_type="automation.reminder",
                    aggregate_id=str(item.id),
                    reminder_id=str(item.id),
                    event_type=f"reminder.{operation}",
                    event_status=item.status.value,
                    event_version=str(item.version),
                )
            raise
        if self.metrics is not None:
            self.metrics.record_span(
                REMINDER_COMPONENT,
                span_operation,
                timer.elapsed_ms(),
                trace_id=item.id.hex,
                span_id=new_span_id(),
                parent_span_id=None,
                dependency="postgresql",
                outcome="ok",
                command_id=str(command.id),
                correlation_id=str(command.correlation_id or command.id),
                causation_id=str(command.causation_id or item.id),
                aggregate_type="automation.reminder",
                aggregate_id=str(item.id),
                reminder_id=str(item.id),
                event_type=f"reminder.{operation}",
                event_status=item.status.value,
                event_version=str(item.version),
            )

    async def _transition(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str, target: ReminderStatus) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            existing = await session.get(ReminderIdempotencyRow, (issuer, subject, idempotency_key))
            fingerprint = command_fingerprint(target.value, reminder_id, expectedVersion=expected_version)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return from_payload(existing.response)
            row = (await session.execute(select(ReminderRow).where(ReminderRow.id == reminder_id, ReminderRow.principal_issuer == issuer, ReminderRow.principal_subject == subject).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ReminderNotFound("reminder not found")
            if row.version != expected_version:
                self._conflict("reminder_version_conflict_count")
                raise ReminderVersionConflict("reminder version does not match")
            if not ((target is ReminderStatus.COMPLETED and row.status in {ReminderStatus.SCHEDULED.value, ReminderStatus.FIRED.value}) or (target is ReminderStatus.CANCELED and row.status == ReminderStatus.SCHEDULED.value)):
                raise ReminderTransitionError("reminder lifecycle transition is not allowed")
            now = self._time()
            row.status = target.value
            row.version += 1
            row.updated_at = now
            if target is ReminderStatus.COMPLETED:
                row.completed_at = now
            else:
                row.canceled_at = now
            item = _item(row)
            await self._stage(session, item, target.value, issuer, subject)
            session.add(ReminderIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, reminder_id=item.id, response=as_payload(item), created_at=now))
            return item

    async def complete(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str) -> Reminder:
        return await self._transition(issuer, subject, reminder_id, expected_version=expected_version, idempotency_key=idempotency_key, target=ReminderStatus.COMPLETED)

    async def cancel(self, issuer: str, subject: str, reminder_id: UUID, *, expected_version: int, idempotency_key: str) -> Reminder:
        return await self._transition(issuer, subject, reminder_id, expected_version=expected_version, idempotency_key=idempotency_key, target=ReminderStatus.CANCELED)


__all__ = ["MemoryReminderRepository", "SqlReminderRepository"]
