"""In-memory and PostgreSQL reminder repositories."""

# Repository calls intentionally keep the transactional SQL expressions close
# to their domain operation; long signatures remain easier to audit together.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.reminders.persistence import (
    ReminderIdempotencyRow,
    ReminderOccurrenceRow,
    ReminderRow,
    ReminderSeriesIdempotencyRow,
    ReminderSeriesRow,
)
from aura_core.domains.automation.reminders.public import (
    REMINDER_COMPONENT,
    REMINDER_TOPIC,
    RecurrenceRule,
    Reminder,
    ReminderDeliveryResult,
    ReminderIdempotencyConflict,
    ReminderNotFound,
    ReminderOccurrence,
    ReminderOccurrenceStatus,
    ReminderPage,
    ReminderSeries,
    ReminderSeriesIdempotencyConflict,
    ReminderSeriesNotFound,
    ReminderSeriesPage,
    ReminderSeriesStatus,
    ReminderSeriesTransitionError,
    ReminderSeriesVersionConflict,
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
from aura_core.domains.automation.reminders.recurrence import next_occurrence, validate_timezone
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


def _validate_series_idempotency_key(value: str) -> str:
    try:
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ReminderValidationError("series idempotency_key must be a UUID") from exc
    return value


def _series_error_dimensions(error: Exception) -> tuple[str, str]:
    """Map domain failures to bounded telemetry dimensions."""

    if isinstance(error, ReminderSeriesIdempotencyConflict):
        return "conflict", "idempotency"
    if isinstance(error, ReminderSeriesVersionConflict):
        return "conflict", "conflict"
    if isinstance(error, ReminderSeriesTransitionError):
        return "error", "lifecycle"
    if isinstance(error, (ReminderValidationError, ValueError)):
        return "error", "validation"
    return "error", "persistence"


def _record_series_span(
    metrics: MetadataMetrics | None,
    *,
    component: str = REMINDER_COMPONENT,
    operation: str,
    duration_ms: float,
    idempotency_key: str | None,
    series_id: UUID | None = None,
    outcome: str,
    error_class: str | None = None,
    dependency: str,
    status: ReminderSeriesStatus | None = None,
    version: int | None = None,
) -> None:
    if metrics is None:
        return
    try:
        command_id = UUID(idempotency_key).hex if idempotency_key else uuid4().hex
    except (ValueError, AttributeError):
        command_id = uuid4().hex
    trace_id = series_id.hex if series_id is not None else command_id
    attributes: dict[str, str] = {
        "aggregate_type": "automation.reminder-series",
        "command_id": command_id,
        "correlation_id": command_id,
    }
    if series_id is not None:
        attributes["aggregate_id"] = str(series_id)
        attributes["causation_id"] = str(series_id)
    if status is not None:
        attributes["event_status"] = status.value
    if version is not None:
        attributes["event_version"] = str(version)
    metrics.record_span(
        component,
        f"reminder.series.{operation}",
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency=dependency,
        outcome=outcome,
        error_class=error_class,
        **attributes,
    )


@contextmanager
def _series_advancement_span(
    metrics: MetadataMetrics | None,
    *,
    series_id: UUID,
    sequence: int,
    status: ReminderSeriesStatus,
    version: int,
    dependency: str,
):
    """Measure one real recurring-series advancement/calculation."""

    timer = Stopwatch()
    command_id = str(uuid5(series_id, f"advance:{sequence}:{version}"))
    try:
        yield
    except Exception as error:
        outcome, error_class = _series_error_dimensions(error)
        if metrics is not None:
            metrics.increment(
                REMINDER_COMPONENT,
                "reminder_recurrence_calculation_outcome",
                operation="fire",
                outcome=outcome,
                dependency=dependency,
            )
            _record_series_span(
                metrics,
                component="aura.automation.reminder_scheduler",
                operation="advance",
                duration_ms=timer.elapsed_ms(),
                idempotency_key=command_id,
                series_id=series_id,
                outcome=outcome,
                error_class=error_class,
                dependency=dependency,
                status=status,
                version=version,
            )
        raise
    else:
        if metrics is not None:
            metrics.increment(
                REMINDER_COMPONENT,
                "reminder_recurrence_calculation_outcome",
                operation="fire",
                outcome="ok",
                dependency=dependency,
            )
            _record_series_span(
                metrics,
                component="aura.automation.reminder_scheduler",
                operation="advance",
                duration_ms=timer.elapsed_ms(),
                idempotency_key=command_id,
                series_id=series_id,
                outcome="ok",
                dependency=dependency,
                status=status,
                version=version,
            )


def _idempotency_lock_key(issuer: str, subject: str, idempotency_key: str) -> str:
    """Return a stable, NUL-free advisory-lock key for one owner command.

    PostgreSQL text parameters reject literal NUL bytes.  JSON's canonical
    escaped representation preserves the three-field partitioning while
    remaining unambiguous for arbitrary authenticated text values.
    """

    encoded = json.dumps(
        [issuer, subject, idempotency_key],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"aura.reminder:{encoded}"


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


def _series_item(row: ReminderSeriesRow) -> ReminderSeries:
    return ReminderSeries(
        row.id,
        row.principal_issuer,
        row.principal_subject,
        row.content,
        RecurrenceRule.from_payload(
            {
                "frequency": row.frequency,
                "interval": row.interval_value,
                "weekdays": row.weekdays or [],
                "monthDay": row.month_day,
                "count": row.count_limit,
                "endDate": row.end_date.isoformat() if row.end_date else None,
            }
        ),
        row.timezone,
        row.local_start,
        row.next_due_at.astimezone(UTC) if row.next_due_at else None,
        row.next_reminder_id,
        ReminderSeriesStatus(row.status),
        row.version,
        row.created_at.astimezone(UTC),
        row.updated_at.astimezone(UTC),
        row.missed_count,
        row.occurrence_count,
        row.paused_at.astimezone(UTC) if row.paused_at else None,
        row.completed_at.astimezone(UTC) if row.completed_at else None,
        row.canceled_at.astimezone(UTC) if row.canceled_at else None,
    )


def _series_payload(item: ReminderSeries) -> dict[str, object]:
    return {
        "id": str(item.id),
        "content": item.content,
        "rule": item.rule.to_payload(),
        "timezone": item.timezone,
        "localStart": item.local_start.isoformat(),
        "nextDueAt": item.next_due_at.isoformat() if item.next_due_at else None,
        "nextReminderId": str(item.next_reminder_id) if item.next_reminder_id else None,
        "status": item.status.value,
        "version": item.version,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "missedCount": item.missed_count,
        "occurrenceCount": item.occurrence_count,
        "pausedAt": item.paused_at.isoformat() if item.paused_at else None,
        "completedAt": item.completed_at.isoformat() if item.completed_at else None,
        "canceledAt": item.canceled_at.isoformat() if item.canceled_at else None,
    }


def _series_from_payload(payload: dict[str, object], issuer: str, subject: str) -> ReminderSeries:
    def stamp(name: str) -> datetime:
        value = payload.get(name)
        if not isinstance(value, str):
            raise ReminderValidationError(f"series response is missing {name}")
        return datetime.fromisoformat(value).astimezone(UTC)

    def optional_stamp(name: str) -> datetime | None:
        value = payload.get(name)
        return datetime.fromisoformat(value).astimezone(UTC) if isinstance(value, str) else None

    raw_rule = payload.get("rule")
    if not isinstance(raw_rule, dict):
        raise ReminderValidationError("series response has an invalid rule")
    raw_id = payload.get("id")
    raw_local_start = payload.get("localStart")
    raw_timezone = payload.get("timezone")
    if (
        not isinstance(raw_id, str)
        or not isinstance(raw_local_start, str)
        or not isinstance(raw_timezone, str)
    ):
        raise ReminderValidationError("series response has invalid identifiers")
    raw_next_due = payload.get("nextDueAt")
    raw_next_reminder = payload.get("nextReminderId")
    raw_version = payload.get("version", 0)
    raw_missed = payload.get("missedCount", 0)
    raw_occurrence_count = payload.get("occurrenceCount", 0)
    if (
        type(raw_version) is not int
        or type(raw_missed) is not int
        or type(raw_occurrence_count) is not int
    ):
        raise ReminderValidationError("series response has invalid counts")
    return ReminderSeries(
        UUID(raw_id),
        issuer,
        subject,
        str(payload.get("content", "")),
        RecurrenceRule.from_payload(cast(dict[str, object], raw_rule)),
        raw_timezone,
        datetime.fromisoformat(raw_local_start),
        datetime.fromisoformat(raw_next_due).astimezone(UTC)
        if isinstance(raw_next_due, str)
        else None,
        UUID(raw_next_reminder) if isinstance(raw_next_reminder, str) else None,
        ReminderSeriesStatus(str(payload.get("status"))),
        raw_version,
        stamp("createdAt"),
        stamp("updatedAt"),
        raw_missed,
        raw_occurrence_count,
        optional_stamp("pausedAt"),
        optional_stamp("completedAt"),
        optional_stamp("canceledAt"),
    )


def _occurrence_item(row: ReminderOccurrenceRow) -> ReminderOccurrence:
    return ReminderOccurrence(
        row.id,
        row.series_id,
        row.reminder_id,
        row.occurrence_sequence,
        ReminderOccurrenceStatus(row.status),
        row.local_due_at,
        row.due_at.astimezone(UTC),
        row.created_at.astimezone(UTC),
        row.updated_at.astimezone(UTC),
        row.skipped_at.astimezone(UTC) if row.skipped_at else None,
        row.fired_at.astimezone(UTC) if row.fired_at else None,
        row.completed_at.astimezone(UTC) if row.completed_at else None,
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
        self._series: dict[UUID, ReminderSeries] = {}
        self._occurrences: dict[UUID, ReminderOccurrence] = {}
        self._series_keys: dict[tuple[str, str, str], tuple[str, ReminderSeries]] = {}
        self._series_command_keys: dict[tuple[str, str, str], tuple[str, ReminderSeries]] = {}
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

    def _idempotency_outcome(self, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                REMINDER_COMPONENT,
                "reminder_create_idempotency_outcome",
                outcome=outcome,
                dependency="reminder_store",
            )

    def _series_metric(self, metric: str, *, operation: str, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                REMINDER_COMPONENT,
                metric,
                operation=operation,
                outcome=outcome,
                dependency="reminder_store",
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
            {
                "action": f"reminder.{operation}",
                "resource": str(item.id),
                "metadata": {"status": item.status.value},
            }
        )
        return item

    async def create(
        self, issuer: str, subject: str, *, content: str, due_at: datetime, idempotency_key: str
    ) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        try:
            normalized_content = normalize_content(content)
            due = canonical_due_at(due_at)
            async with self._lock:
                fingerprint = command_fingerprint(
                    "create", None, content=normalized_content, dueAt=due.isoformat()
                )
                existing = self._idempotency.get((issuer, subject, idempotency_key))
                if existing:
                    if existing[0] != fingerprint:
                        self._conflict("reminder_idempotency_conflict_count")
                        self._idempotency_outcome("conflict")
                        raise ReminderIdempotencyConflict("idempotency key was already used")
                    self._idempotency_outcome("duplicate")
                    return existing[1]
                due = ensure_due_at_future(due, now=self._time())
                now = self._time()
                item = Reminder(
                    uuid4(),
                    issuer,
                    subject,
                    normalized_content,
                    due,
                    ReminderStatus.SCHEDULED,
                    1,
                    now,
                    now,
                )
                self._items[item.id] = item
                result = self._record(
                    issuer, subject, idempotency_key, fingerprint, item, "created"
                )
                self._idempotency_outcome("created")
                return result
        except ReminderIdempotencyConflict:
            raise
        except Exception:
            self._idempotency_outcome("failed")
            raise

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        item = self._items.get(reminder_id)
        if item is None or (item.issuer, item.subject) != (issuer, subject):
            raise ReminderNotFound("reminder not found")
        return item

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: ReminderView,
        limit: int,
        cursor: str | None = None,
    ) -> ReminderPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject, view) if cursor else None
        values = [
            item
            for item in self._items.values()
            if (item.issuer, item.subject) == (issuer, subject)
        ]
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
            next_cursor = encode_cursor(
                issuer,
                subject,
                view,
                last.due_at if view is ReminderView.UPCOMING else last.updated_at,
                last.id,
            )
        return ReminderPage(tuple(page), next_cursor)

    async def _transition(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        target: ReminderStatus,
    ) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(reminder_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject):
                raise ReminderNotFound("reminder not found")
            fingerprint = command_fingerprint(
                target.value, reminder_id, expectedVersion=expected_version
            )
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return existing[1]
            if item.version != expected_version:
                self._conflict("reminder_version_conflict_count")
                raise ReminderVersionConflict("reminder version does not match")
            allowed = (
                target is ReminderStatus.COMPLETED
                and item.status in {ReminderStatus.SCHEDULED, ReminderStatus.FIRED}
            ) or (target is ReminderStatus.CANCELED and item.status is ReminderStatus.SCHEDULED)
            if not allowed:
                raise ReminderTransitionError("reminder lifecycle transition is not allowed")
            updated = item.with_status(target, self._time())
            self._items[reminder_id] = updated
            found = self._find_occurrence_by_reminder(reminder_id)
            advance_series = False
            if found:
                oid, occurrence = found
                status = (
                    ReminderOccurrenceStatus.COMPLETED
                    if target is ReminderStatus.COMPLETED
                    else ReminderOccurrenceStatus.SERIES_CANCELED
                )
                self._occurrences[oid] = replace(
                    occurrence,
                    status=status,
                    completed_at=self._time()
                    if status is ReminderOccurrenceStatus.COMPLETED
                    else occurrence.completed_at,
                    updated_at=self._time(),
                )
                advance_series = (
                    target is ReminderStatus.COMPLETED
                    and self._series.get(occurrence.series_id) is not None
                    and self._series[occurrence.series_id].next_reminder_id == reminder_id
                )
            if advance_series:
                await self._advance_memory_series(updated, self._time())
            return self._record(
                issuer, subject, idempotency_key, fingerprint, updated, target.value
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
        return await self._transition(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target=ReminderStatus.COMPLETED,
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
        return await self._transition(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target=ReminderStatus.CANCELED,
        )

    async def fire_due(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[Reminder, ...]:
        del transaction
        if not 1 <= limit <= 100:
            raise ValueError("reminder delivery batch must contain 1 to 100 reminders")
        current = now or self._time()
        current = current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)
        async with self._lock:
            due = [
                item
                for item in self._items.values()
                if item.status is ReminderStatus.SCHEDULED and item.due_at <= current
            ]
            due.sort(key=lambda item: (item.due_at, item.id))
            fired = tuple(item.with_status(ReminderStatus.FIRED, current) for item in due[:limit])
            for item in fired:
                self._items[item.id] = item
                if item.series_id is not None and self.metrics is not None:
                    self.metrics.observe(
                        "aura.automation.reminder_scheduler",
                        "reminder_series_due_lag_ms",
                        max(0.0, (current - item.due_at).total_seconds() * 1000),
                        operation="fire",
                        outcome="ok",
                    )
                await self._advance_memory_series(item, current)
            return fired

    async def fire_due_result(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> ReminderDeliveryResult:
        before_occurrences = set(self._occurrences)
        before_missed = {series.id: series.missed_count for series in self._series.values()}
        reminders = await self.fire_due(transaction=transaction, now=now, limit=limit)
        generated = sum(
            1
            for occurrence_id, occurrence in self._occurrences.items()
            if occurrence_id not in before_occurrences
            and occurrence.status is ReminderOccurrenceStatus.SCHEDULED
        )
        skipped = sum(
            1
            for occurrence_id, occurrence in self._occurrences.items()
            if occurrence_id not in before_occurrences
            and occurrence.status is ReminderOccurrenceStatus.SKIPPED
        )
        missed = sum(
            max(0, series.missed_count - before_missed.get(series.id, 0))
            for series in self._series.values()
        )
        return ReminderDeliveryResult(reminders, generated, skipped, missed)

    def _series_payload(self, item: ReminderSeries) -> dict[str, object]:
        return {
            "id": str(item.id),
            "content": item.content,
            "rule": item.rule.to_payload(),
            "timezone": item.timezone,
            "localStart": item.local_start.isoformat(),
            "nextDueAt": item.next_due_at.isoformat() if item.next_due_at else None,
            "nextReminderId": str(item.next_reminder_id) if item.next_reminder_id else None,
            "status": item.status.value,
            "version": item.version,
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
            "missedCount": item.missed_count,
            "occurrenceCount": item.occurrence_count,
        }

    def _occurrence(self, item: ReminderOccurrence) -> ReminderOccurrence:
        return item

    def _record_series_event(
        self,
        series: ReminderSeries,
        operation: str,
        *,
        occurrence_sequence: int = 0,
        skipped_count: int = 0,
        command_id: str | None = None,
    ) -> None:
        operation = {
            "pause": "paused",
            "resume": "resumed",
            "skip": "skipped",
            "update": "updated",
            "cancel": "canceled",
        }.get(operation, operation)
        record: dict[str, object] = {
            "schemaVersion": 1,
            "commandId": command_id
            or str(uuid5(series.id, f"series-command:{operation}:{series.version}")),
            "aggregateType": "automation.reminder-series",
            "aggregateId": str(series.id),
            "eventType": f"reminder.series.{operation}",
            "createdAt": series.updated_at.isoformat(),
            "correlationId": command_id
            or str(uuid5(series.id, f"series-correlation:{operation}:{series.version}")),
            "causationId": command_id
            or str(uuid5(series.id, f"series-causation:{operation}:{series.version}")),
            "status": series.status.value,
            "version": series.version,
            "occurrenceSequence": max(0, occurrence_sequence),
            "occurrenceCount": max(0, series.occurrence_count),
            "skippedCount": max(0, skipped_count),
            "missedCount": max(0, series.missed_count),
        }
        self.events.append(record)
        self.audit.append(
            {
                "action": f"reminder.series.{operation}",
                "resource": str(series.id),
                "metadata": {"status": series.status.value, "version": series.version},
            }
        )

    def _series_now(self) -> datetime:
        return self._time()

    async def _materialize_memory_occurrence(
        self,
        series: ReminderSeries,
        local_due: datetime,
        due: datetime,
        sequence: int,
        *,
        now: datetime,
        status: ReminderOccurrenceStatus = ReminderOccurrenceStatus.SCHEDULED,
    ) -> ReminderSeries:
        reminder_id = uuid4()
        reminder_status = (
            ReminderStatus.SCHEDULED
            if status is ReminderOccurrenceStatus.SCHEDULED
            else ReminderStatus.CANCELED
        )
        reminder = Reminder(
            reminder_id,
            series.issuer,
            series.subject,
            series.content,
            due,
            reminder_status,
            1,
            now,
            now,
            series_id=series.id,
            occurrence_sequence=sequence,
        )
        self._items[reminder_id] = reminder
        occurrence = ReminderOccurrence(
            uuid4(),
            series.id,
            reminder_id,
            sequence,
            status,
            local_due,
            due,
            now,
            now,
            skipped_at=now if status is not ReminderOccurrenceStatus.SCHEDULED else None,
        )
        self._occurrences[occurrence.id] = occurrence
        if status is not ReminderOccurrenceStatus.SCHEDULED:
            return series
        if self.metrics is not None:
            self.metrics.observe(
                REMINDER_COMPONENT,
                "reminder_occurrence_generated_count",
                1.0,
                outcome="ok",
            )
        return replace(
            series,
            next_due_at=due,
            next_reminder_id=reminder_id,
            version=series.version,
            updated_at=now,
        )

    def _find_occurrence_by_reminder(
        self, reminder_id: UUID
    ) -> tuple[UUID, ReminderOccurrence] | None:
        for occurrence_id, occurrence in self._occurrences.items():
            if occurrence.reminder_id == reminder_id:
                return occurrence_id, occurrence
        return None

    async def _advance_memory_series(self, item: Reminder, now: datetime) -> None:
        found = self._find_occurrence_by_reminder(item.id)
        if found is None:
            return
        _, occurrence = found
        series = self._series.get(occurrence.series_id)
        if series is None:
            return
        with _series_advancement_span(
            self.metrics,
            series_id=series.id,
            sequence=occurrence.sequence,
            status=series.status,
            version=series.version,
            dependency="reminder_store",
        ):
            await self._advance_memory_series_impl(item, now)

    async def _advance_memory_series_impl(self, item: Reminder, now: datetime) -> None:
        found = self._find_occurrence_by_reminder(item.id)
        if found is None:
            return
        occurrence_id, occurrence = found
        series = self._series.get(occurrence.series_id)
        if series is None:
            return
        self._occurrences[occurrence_id] = replace(
            occurrence,
            status=(
                ReminderOccurrenceStatus.COMPLETED
                if item.status is ReminderStatus.COMPLETED
                else ReminderOccurrenceStatus.FIRED
            ),
            fired_at=occurrence.fired_at if item.status is ReminderStatus.COMPLETED else now,
            completed_at=now
            if item.status is ReminderStatus.COMPLETED
            else occurrence.completed_at,
            updated_at=now,
        )
        completed_count = series.occurrence_count + 1
        if series.rule.count is not None and completed_count >= series.rule.count:
            self._series[series.id] = replace(
                series,
                status=ReminderSeriesStatus.COMPLETED,
                next_due_at=None,
                next_reminder_id=None,
                occurrence_count=completed_count,
                version=series.version + 1,
                updated_at=now,
                completed_at=now,
            )
            self._record_series_event(
                self._series[series.id], "completed", occurrence_sequence=occurrence.sequence
            )
            return
        local = occurrence.local_due_at
        sequence = occurrence.sequence + 1
        missed = 0
        while True:
            next_local, next_due = next_occurrence(
                series.rule, local, series.timezone, anchor_local=series.local_start
            )
            if series.rule.count is not None and sequence > series.rule.count:
                self._series[series.id] = replace(
                    series,
                    status=ReminderSeriesStatus.COMPLETED,
                    next_due_at=None,
                    next_reminder_id=None,
                    occurrence_count=completed_count,
                    version=series.version + 1,
                    updated_at=now,
                    completed_at=now,
                )
                self._record_series_event(
                    self._series[series.id],
                    "completed",
                    occurrence_sequence=occurrence.sequence,
                )
                return
            if series.rule.end_date is not None and next_local.date() > series.rule.end_date:
                self._series[series.id] = replace(
                    series,
                    status=ReminderSeriesStatus.COMPLETED,
                    next_due_at=None,
                    next_reminder_id=None,
                    occurrence_count=completed_count,
                    version=series.version + 1,
                    updated_at=now,
                    completed_at=now,
                )
                self._record_series_event(
                    self._series[series.id], "completed", occurrence_sequence=occurrence.sequence
                )
                return
            if next_due <= now:
                await self._materialize_memory_occurrence(
                    series,
                    next_local,
                    next_due,
                    sequence,
                    now=now,
                    status=ReminderOccurrenceStatus.SKIPPED,
                )
                skipped_occurrence = next(
                    (
                        o
                        for o in self._occurrences.values()
                        if o.series_id == series.id and o.sequence == sequence
                    ),
                    None,
                )
                if skipped_occurrence:
                    self._occurrences[skipped_occurrence.id] = replace(
                        skipped_occurrence, status=ReminderOccurrenceStatus.SKIPPED, skipped_at=now
                    )
                local, sequence = next_local, sequence + 1
                missed += 1
                continue
            updated = replace(
                series,
                next_due_at=next_due,
                next_reminder_id=None,
                occurrence_count=completed_count,
                missed_count=series.missed_count + missed,
                version=series.version + 1,
                updated_at=now,
            )
            updated = await self._materialize_memory_occurrence(
                updated, next_local, next_due, sequence, now=now
            )
            self._series[series.id] = updated
            self._record_series_event(
                updated, "advanced", occurrence_sequence=sequence, skipped_count=missed
            )
            return

    async def create_series(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        local_start: datetime,
        timezone: str,
        rule: RecurrenceRule,
        idempotency_key: str,
    ) -> ReminderSeries:
        timer = Stopwatch()
        try:
            result = await self._create_series_impl(
                issuer,
                subject,
                content=content,
                local_start=local_start,
                timezone=timezone,
                rule=rule,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            outcome, error_class = _series_error_dimensions(error)
            self._series_metric(
                "reminder_series_command_outcome", operation="create", outcome=outcome
            )
            _record_series_span(
                self.metrics,
                operation="create",
                duration_ms=timer.elapsed_ms(),
                idempotency_key=idempotency_key,
                outcome=outcome,
                error_class=error_class,
                dependency="reminder_store",
            )
            raise
        _record_series_span(
            self.metrics,
            operation="create",
            duration_ms=timer.elapsed_ms(),
            idempotency_key=idempotency_key,
            series_id=result.id,
            outcome="ok",
            dependency="reminder_store",
            status=result.status,
            version=result.version,
        )
        return result

    async def _create_series_impl(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        local_start: datetime,
        timezone: str,
        rule: RecurrenceRule,
        idempotency_key: str,
    ) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        _validate_series_idempotency_key(idempotency_key)
        try:
            validate_timezone(timezone)
        except Exception:
            self._series_metric(
                "reminder_recurrence_calculation_outcome",
                operation="create",
                outcome="error",
            )
            raise
        if local_start.tzinfo is not None:
            raise ReminderValidationError("local_start must not contain an offset")
        async with self._lock:
            normalized_content = normalize_content(content)
            fingerprint = command_fingerprint(
                "series.create",
                None,
                content=normalized_content,
                localStart=local_start.isoformat(),
                timezone=timezone,
                rule=rule.to_payload(),
            )
            existing = self._series_command_keys.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    self._series_metric(
                        "reminder_series_idempotency_conflict_count",
                        operation="create",
                        outcome="conflict",
                    )
                    raise ReminderSeriesIdempotencyConflict("idempotency key was already used")
                return existing[1]
            now = self._series_now()
            due = rule.instant(local_start, timezone)
            if due <= now:
                raise ReminderValidationError("series first occurrence must be in the future")
            series_id = uuid4()
            series = ReminderSeries(
                series_id,
                issuer,
                subject,
                normalized_content,
                rule,
                timezone,
                local_start,
                due,
                None,
                ReminderSeriesStatus.ACTIVE,
                1,
                now,
                now,
            )
            series = await self._materialize_memory_occurrence(series, local_start, due, 1, now=now)
            self._series[series_id] = series
            self._series_command_keys[(issuer, subject, idempotency_key)] = (fingerprint, series)
            self._record_series_event(
                series,
                "created",
                occurrence_sequence=1,
                command_id=idempotency_key,
            )
            self._series_metric("reminder_series_command_outcome", operation="create", outcome="ok")
            self._series_metric(
                "reminder_recurrence_calculation_outcome",
                operation="create",
                outcome="ok",
            )
            # update the response after materialization assigned nextReminderId
            self._series_command_keys[(issuer, subject, idempotency_key)] = (fingerprint, series)
            return series

    async def get_series(self, issuer: str, subject: str, series_id: UUID) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        item = self._series.get(series_id)
        if item is None or (item.issuer, item.subject) != (issuer, subject):
            raise ReminderSeriesNotFound("reminder series not found")
        return item

    async def list_series(
        self, issuer: str, subject: str, *, limit: int = 100
    ) -> ReminderSeriesPage:
        issuer, subject = _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise ReminderValidationError("series limit must be between 1 and 100")
        values = sorted(
            (s for s in self._series.values() if (s.issuer, s.subject) == (issuer, subject)),
            key=lambda s: (s.updated_at, s.id),
            reverse=True,
        )
        return ReminderSeriesPage(tuple(values[:limit]), None)

    async def list_occurrences(
        self, issuer: str, subject: str, series_id: UUID, *, limit: int = 100
    ) -> tuple[ReminderOccurrence, ...]:
        await self.get_series(issuer, subject, series_id)
        if not 1 <= limit <= 100:
            raise ReminderValidationError("occurrence limit must be between 1 and 100")
        return tuple(
            sorted(
                (o for o in self._occurrences.values() if o.series_id == series_id),
                key=lambda o: o.sequence,
            )[:limit]
        )

    async def _series_command(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        operation: str,
        action: Callable[[ReminderSeries, datetime], ReminderSeries],
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        timer = Stopwatch()
        short_operation = operation.removeprefix("series.")
        try:
            result = await self._series_command_impl(
                issuer,
                subject,
                series_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                operation=operation,
                action=action,
                content=content,
                local_start=local_start,
                timezone=timezone,
                rule=rule,
            )
        except Exception as error:
            outcome, error_class = _series_error_dimensions(error)
            self._series_metric(
                "reminder_series_command_outcome", operation=short_operation, outcome=outcome
            )
            if short_operation in {"resume", "skip", "update"}:
                self._series_metric(
                    "reminder_recurrence_calculation_outcome",
                    operation=short_operation,
                    outcome="error",
                )
            _record_series_span(
                self.metrics,
                operation=short_operation,
                duration_ms=timer.elapsed_ms(),
                idempotency_key=idempotency_key,
                series_id=series_id,
                outcome=outcome,
                error_class=error_class,
                dependency="reminder_store",
            )
            raise
        _record_series_span(
            self.metrics,
            operation=short_operation,
            duration_ms=timer.elapsed_ms(),
            idempotency_key=idempotency_key,
            series_id=result.id,
            outcome="ok",
            dependency="reminder_store",
            status=result.status,
            version=result.version,
        )
        if short_operation in {"resume", "skip", "update"}:
            self._series_metric(
                "reminder_recurrence_calculation_outcome", operation=short_operation, outcome="ok"
            )
        return result

    async def _series_command_impl(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        operation: str,
        action: Callable[[ReminderSeries, datetime], ReminderSeries],
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        _validate_series_idempotency_key(idempotency_key)
        normalized_content = normalize_content(content) if content is not None else None
        async with self._lock:
            current = await self.get_series(issuer, subject, series_id)
            fingerprint = command_fingerprint(
                operation,
                series_id,
                expectedVersion=expected_version,
                content=normalized_content,
                localStart=local_start.isoformat() if local_start else None,
                timezone=timezone,
                rule=rule.to_payload() if rule else None,
            )
            existing = self._series_command_keys.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    self._series_metric(
                        "reminder_series_idempotency_conflict_count",
                        operation=operation.removeprefix("series."),
                        outcome="conflict",
                    )
                    raise ReminderSeriesIdempotencyConflict("idempotency key was already used")
                return existing[1]
            if current.version != expected_version:
                self._series_metric(
                    "reminder_series_version_conflict_count",
                    operation=operation.removeprefix("series."),
                    outcome="conflict",
                )
                raise ReminderSeriesVersionConflict("series version does not match")
            now = self._series_now()
            updated = action(current, now)
            updated = await self._finalize_series_command(
                updated, operation, now, replacement_local_start=local_start
            )
            self._series[series_id] = updated
            self._series_command_keys[(issuer, subject, idempotency_key)] = (fingerprint, updated)
            self._record_series_event(
                updated,
                operation.removeprefix("series."),
                occurrence_sequence=max(
                    (o.sequence for o in self._occurrences.values() if o.series_id == series_id),
                    default=0,
                ),
                command_id=idempotency_key,
            )
            self._series_metric(
                "reminder_series_command_outcome",
                operation=operation.removeprefix("series."),
                outcome="ok",
            )
            return updated

    async def _finalize_series_command(
        self,
        series: ReminderSeries,
        operation: str,
        now: datetime,
        replacement_local_start: datetime | None = None,
    ) -> ReminderSeries:
        """Materialize the command's one future occurrence before replaying it."""

        if operation == "series.resume":
            occurrences = [
                occurrence
                for occurrence in self._occurrences.values()
                if occurrence.series_id == series.id
            ]
            previous = (
                max(occurrences, key=lambda occurrence: occurrence.sequence).local_due_at
                if occurrences
                else series.local_start
            )
            local, due = next_occurrence(
                series.rule,
                previous,
                series.timezone,
                anchor_local=series.local_start,
            )
            while due <= now:
                local, due = next_occurrence(
                    series.rule,
                    local,
                    series.timezone,
                    anchor_local=series.local_start,
                )
            sequence = max((occurrence.sequence for occurrence in occurrences), default=0) + 1
            return await self._materialize_memory_occurrence(series, local, due, sequence, now=now)

        if operation == "series.skip":
            occurrence = max(
                (
                    occurrence
                    for occurrence in self._occurrences.values()
                    if occurrence.series_id == series.id
                ),
                key=lambda item: item.sequence,
            )
            local, due = next_occurrence(
                series.rule,
                occurrence.local_due_at,
                series.timezone,
                anchor_local=series.local_start,
            )
            sequence = occurrence.sequence + 1
            if (series.rule.count is not None and sequence > series.rule.count) or (
                series.rule.end_date is not None and local.date() > series.rule.end_date
            ):
                return replace(
                    series,
                    status=ReminderSeriesStatus.COMPLETED,
                    next_due_at=None,
                    next_reminder_id=None,
                    completed_at=now,
                    version=series.version + 1,
                    updated_at=now,
                )
            return await self._materialize_memory_occurrence(series, local, due, sequence, now=now)

        if operation == "series.update":
            occurrences = [
                occurrence
                for occurrence in self._occurrences.values()
                if occurrence.series_id == series.id
            ]
            sequence = (
                max(
                    (
                        occurrence.sequence for occurrence in occurrences
                    ),
                    default=0,
                )
                + 1
            )
            due = series.next_due_at or series.rule.instant(series.local_start, series.timezone)
            local = replacement_local_start or (
                max(occurrences, key=lambda occurrence: occurrence.sequence).local_due_at
                if occurrences
                else series.local_start
            )
            return await self._materialize_memory_occurrence(
                series, local, due, sequence, now=now
            )

        return series

    async def pause_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        def pause(series: ReminderSeries, now: datetime) -> ReminderSeries:
            if series.status is not ReminderSeriesStatus.ACTIVE:
                raise ReminderSeriesTransitionError("series is not active")
            if series.next_reminder_id:
                item = self._items[series.next_reminder_id]
                self._items[item.id] = item.with_status(ReminderStatus.CANCELED, now)
                found = self._find_occurrence_by_reminder(item.id)
                if found:
                    oid, occurrence = found
                    self._occurrences[oid] = replace(
                        occurrence,
                        status=ReminderOccurrenceStatus.PAUSED_WINDOW,
                        updated_at=now,
                        skipped_at=now,
                    )
            return replace(
                series,
                status=ReminderSeriesStatus.PAUSED,
                next_due_at=None,
                next_reminder_id=None,
                paused_at=now,
                version=series.version + 1,
                updated_at=now,
            )

        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="series.pause",
            action=pause,
        )

    async def resume_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        def resume(series: ReminderSeries, now: datetime) -> ReminderSeries:
            if series.status is not ReminderSeriesStatus.PAUSED:
                raise ReminderSeriesTransitionError("series is not paused")
            occurrences = [o for o in self._occurrences.values() if o.series_id == series.id]
            previous = (
                max(occurrences, key=lambda o: o.sequence).local_due_at
                if occurrences
                else series.local_start
            )
            local, due = next_occurrence(
                series.rule, previous, series.timezone, anchor_local=series.local_start
            )
            while due <= now:
                local, due = next_occurrence(
                    series.rule, local, series.timezone, anchor_local=series.local_start
                )
            updated = replace(
                series,
                status=ReminderSeriesStatus.ACTIVE,
                next_due_at=due,
                next_reminder_id=None,
                paused_at=None,
                version=series.version + 1,
                updated_at=now,
            )
            return updated

        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="series.resume",
            action=resume,
        )

    async def resume(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self.resume_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def skip_next(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        def skip(series: ReminderSeries, now: datetime) -> ReminderSeries:
            if series.status is not ReminderSeriesStatus.ACTIVE or series.next_reminder_id is None:
                raise ReminderSeriesTransitionError("series has no pending occurrence")
            item = self._items[series.next_reminder_id]
            self._items[item.id] = item.with_status(ReminderStatus.CANCELED, now)
            found = self._find_occurrence_by_reminder(item.id)
            if found:
                oid, occurrence = found
                self._occurrences[oid] = replace(
                    occurrence,
                    status=ReminderOccurrenceStatus.SKIPPED,
                    skipped_at=now,
                    updated_at=now,
                )
            return replace(
                series,
                next_due_at=None,
                next_reminder_id=None,
                version=series.version + 1,
                updated_at=now,
            )

        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="series.skip",
            action=skip,
        )

    async def cancel_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        def cancel(series: ReminderSeries, now: datetime) -> ReminderSeries:
            if series.status in {ReminderSeriesStatus.COMPLETED, ReminderSeriesStatus.CANCELED}:
                raise ReminderSeriesTransitionError("series is already terminal")
            if series.next_reminder_id:
                item = self._items[series.next_reminder_id]
                self._items[item.id] = item.with_status(ReminderStatus.CANCELED, now)
                found = self._find_occurrence_by_reminder(item.id)
                if found:
                    oid, occurrence = found
                    self._occurrences[oid] = replace(
                        occurrence, status=ReminderOccurrenceStatus.SERIES_CANCELED, updated_at=now
                    )
            return replace(
                series,
                status=ReminderSeriesStatus.CANCELED,
                next_due_at=None,
                next_reminder_id=None,
                canceled_at=now,
                version=series.version + 1,
                updated_at=now,
            )

        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="series.cancel",
            action=cancel,
        )

    async def update_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        def update(series: ReminderSeries, now: datetime) -> ReminderSeries:
            if series.status is not ReminderSeriesStatus.ACTIVE:
                raise ReminderSeriesTransitionError("only active series can be edited")
            new_content = content or series.content
            pending_local_start = series.local_start
            if series.next_reminder_id is not None:
                pending = self._find_occurrence_by_reminder(series.next_reminder_id)
                if pending is not None:
                    pending_local_start = pending[1].local_due_at
            replacement_start = local_start or pending_local_start
            persisted_start = local_start or series.local_start
            new_zone = timezone or series.timezone
            new_rule = rule or series.rule
            validate_timezone(new_zone)
            if replacement_start.tzinfo is not None:
                raise ReminderValidationError("local_start must not contain an offset")
            due = new_rule.instant(replacement_start, new_zone)
            if due <= now:
                raise ReminderValidationError("edited occurrence must be in the future")
            if series.next_reminder_id:
                item = self._items[series.next_reminder_id]
                self._items[item.id] = item.with_status(ReminderStatus.CANCELED, now)
                found = self._find_occurrence_by_reminder(item.id)
                if found:
                    oid, occurrence = found
                    self._occurrences[oid] = replace(
                        occurrence, status=ReminderOccurrenceStatus.REPLACED, updated_at=now
                    )
            return replace(
                series,
                content=new_content,
                local_start=persisted_start,
                timezone=new_zone,
                rule=new_rule,
                next_due_at=due,
                next_reminder_id=None,
                version=series.version + 1,
                updated_at=now,
            )

        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="series.update",
            action=update,
            content=content,
            local_start=local_start,
            timezone=timezone,
            rule=rule,
        )


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

    def _idempotency_outcome(self, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                REMINDER_COMPONENT,
                "reminder_create_idempotency_outcome",
                outcome=outcome,
                dependency="reminder_store",
            )

    def _series_metric(self, metric: str, *, operation: str, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                REMINDER_COMPONENT,
                metric,
                operation=operation,
                outcome=outcome,
                dependency="postgresql",
            )

    def _time(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _stage_series(
        self,
        session: AsyncSession,
        series: ReminderSeries,
        operation: str,
        sequence: int = 0,
        skipped: int = 0,
        missed: int = 0,
    ) -> None:
        operation = {
            "pause": "paused",
            "resume": "resumed",
            "skip": "skipped",
            "update": "updated",
            "cancel": "canceled",
        }.get(operation, operation)
        command = make_aggregate_command(
            "aura.automation.reminder-series.v1",
            uuid5(series.id, f"reminder.series:{series.status.value}:{series.version}"),
            "automation.reminder-series",
            series.id,
            identifiers=(
                ("eventType", f"reminder.series.{operation}"),
                ("status", series.status.value),
                ("version", series.version),
                ("occurrenceSequence", max(0, sequence)),
                ("occurrenceCount", min(series.occurrence_count, 2_147_483_647)),
                ("skippedCount", min(max(0, skipped), 2_147_483_647)),
                ("missedCount", min(max(0, missed), 2_147_483_647)),
            ),
        )
        await self._outbox.stage_command(session, command=command)
        principal = await self._identities.resolve(session, series.issuer, series.subject)
        self._audit.stage(
            session,
            principal.id,
            f"reminder.series.{operation}",
            str(series.id),
            {
                "status": series.status.value,
                "version": series.version,
                "occurrenceSequence": max(0, sequence),
                "skippedCount": min(max(0, skipped), 2_147_483_647),
                "missedCount": min(max(0, missed), 2_147_483_647),
            },
        )

    async def _materialize_sql_occurrence(
        self,
        session: AsyncSession,
        row: ReminderSeriesRow,
        local_due: datetime,
        due: datetime,
        sequence: int,
        now: datetime,
    ) -> ReminderRow:
        reminder = ReminderRow(
            id=uuid4(),
            principal_issuer=row.principal_issuer,
            principal_subject=row.principal_subject,
            content=row.content,
            due_at=due,
            status=ReminderStatus.SCHEDULED.value,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(reminder)
        await session.flush()
        session.add(
            ReminderOccurrenceRow(
                id=uuid4(),
                series_id=row.id,
                reminder_id=reminder.id,
                occurrence_sequence=sequence,
                status=ReminderOccurrenceStatus.SCHEDULED.value,
                local_due_at=local_due,
                due_at=due,
                created_at=now,
                updated_at=now,
            )
        )
        row.next_due_at = due
        row.next_reminder_id = reminder.id
        return reminder

    async def _advance_sql_series(
        self,
        transaction: AsyncSession,
        series: ReminderSeriesRow,
        occurrence: ReminderOccurrenceRow,
        current: datetime,
    ) -> tuple[int, int, int]:
        """Advance one fired series with one measured terminal outcome."""

        generated = 0
        with _series_advancement_span(
            self.metrics,
            series_id=series.id,
            sequence=occurrence.occurrence_sequence,
            status=ReminderSeriesStatus(series.status),
            version=series.version,
            dependency="postgresql",
        ):
            previous_missed = series.missed_count
            series.occurrence_count += 1
            series.version += 1
            series.updated_at = current
            rule = RecurrenceRule.from_payload(
                {
                    "frequency": series.frequency,
                    "interval": series.interval_value,
                    "weekdays": series.weekdays or [],
                    "monthDay": series.month_day,
                    "count": series.count_limit,
                    "endDate": series.end_date.isoformat() if series.end_date else None,
                }
            )
            skipped = 0
            completed = False
            if rule.count is not None and series.occurrence_count >= rule.count:
                series.status = ReminderSeriesStatus.COMPLETED.value
                series.next_due_at = None
                series.next_reminder_id = None
                series.completed_at = current
            else:
                local, due = next_occurrence(
                    rule,
                    occurrence.local_due_at,
                    series.timezone,
                    anchor_local=series.local_start,
                )
                while due <= current:
                    next_sequence = occurrence.occurrence_sequence + skipped + 1
                    if (rule.count is not None and next_sequence > rule.count) or (
                        rule.end_date is not None and local.date() > rule.end_date
                    ):
                        series.status = ReminderSeriesStatus.COMPLETED.value
                        series.next_due_at = None
                        series.next_reminder_id = None
                        series.completed_at = current
                        completed = True
                        break
                    skipped += 1
                    series.missed_count += 1
                    skipped_reminder = ReminderRow(
                        id=uuid4(),
                        principal_issuer=series.principal_issuer,
                        principal_subject=series.principal_subject,
                        content=series.content,
                        due_at=due,
                        status=ReminderStatus.CANCELED.value,
                        version=1,
                        created_at=current,
                        updated_at=current,
                        canceled_at=current,
                    )
                    transaction.add(skipped_reminder)
                    await transaction.flush()
                    transaction.add(
                        ReminderOccurrenceRow(
                            id=uuid4(),
                            series_id=series.id,
                            reminder_id=skipped_reminder.id,
                            occurrence_sequence=occurrence.occurrence_sequence + skipped,
                            status=ReminderOccurrenceStatus.SKIPPED.value,
                            local_due_at=local,
                            due_at=due,
                            created_at=current,
                            updated_at=current,
                            skipped_at=current,
                        )
                    )
                    local, due = next_occurrence(
                        rule, local, series.timezone, anchor_local=series.local_start
                    )
                if not completed:
                    next_sequence = occurrence.occurrence_sequence + skipped + 1
                    if (rule.count is not None and next_sequence > rule.count) or (
                        rule.end_date is not None and local.date() > rule.end_date
                    ):
                        series.status = ReminderSeriesStatus.COMPLETED.value
                        series.next_due_at = None
                        series.next_reminder_id = None
                        series.completed_at = current
                        completed = True
                if not completed:
                    series.next_due_at = due
                    future_reminder = ReminderRow(
                        id=uuid4(),
                        principal_issuer=series.principal_issuer,
                        principal_subject=series.principal_subject,
                        content=series.content,
                        due_at=due,
                        status=ReminderStatus.SCHEDULED.value,
                        version=1,
                        created_at=current,
                        updated_at=current,
                    )
                    transaction.add(future_reminder)
                    await transaction.flush()
                    transaction.add(
                        ReminderOccurrenceRow(
                            id=uuid4(),
                            series_id=series.id,
                            reminder_id=future_reminder.id,
                            occurrence_sequence=occurrence.occurrence_sequence
                            + skipped
                            + 1,
                            status=ReminderOccurrenceStatus.SCHEDULED.value,
                            local_due_at=local,
                            due_at=due,
                            created_at=current,
                            updated_at=current,
                        )
                    )
                    series.next_reminder_id = future_reminder.id
                    generated = 1
            await self._stage_series(
                transaction,
                _series_item(series),
                "completed"
                if series.status == ReminderSeriesStatus.COMPLETED.value
                else "advanced",
                occurrence.occurrence_sequence,
                skipped,
                series.missed_count,
            )
            return skipped, max(0, series.missed_count - previous_missed), generated

    async def _fire_due(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[tuple[Reminder, ...], int, int, int]:
        if not 1 <= limit <= 100:
            raise ValueError("reminder delivery batch must contain 1 to 100 reminders")
        if not isinstance(transaction, AsyncSession):
            raise ValueError("reminder firing requires the caller transaction")
        current = now or self._time()
        current = current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)
        # SQLAlchemy emits the equivalent FOR UPDATE SKIP LOCKED claim.
        rows = list(
            (
                await transaction.execute(
                    select(ReminderRow)
                    .where(
                        ReminderRow.status == ReminderStatus.SCHEDULED.value,
                        ReminderRow.due_at <= current,
                    )
                    .order_by(ReminderRow.due_at, ReminderRow.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        fired: list[Reminder] = []
        generated = 0
        skipped_total = 0
        missed_total = 0
        for row in rows:
            row.status = ReminderStatus.FIRED.value
            row.version += 1
            row.fired_at = current
            row.updated_at = current
            fired.append(_item(row))
            occurrence = (
                await transaction.execute(
                    select(ReminderOccurrenceRow)
                    .where(ReminderOccurrenceRow.reminder_id == row.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if occurrence is not None:
                occurrence.status = ReminderOccurrenceStatus.FIRED.value
                occurrence.fired_at = current
                occurrence.updated_at = current
                if self.metrics is not None:
                    self.metrics.observe(
                        "aura.automation.reminder_scheduler",
                        "reminder_series_due_lag_ms",
                        max(0.0, (current - row.due_at).total_seconds() * 1000),
                        operation="fire",
                        outcome="ok",
                    )
                series = (
                    await transaction.execute(
                        select(ReminderSeriesRow)
                        .where(ReminderSeriesRow.id == occurrence.series_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if series is not None:
                    skipped, missed, generated_count = await self._advance_sql_series(
                        transaction,
                        series,
                        occurrence,
                        current,
                    )
                    skipped_total += skipped
                    missed_total += missed
                    generated += generated_count
        return tuple(fired), generated, skipped_total, missed_total

    async def fire_due(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[Reminder, ...]:
        fired, _, _, _ = await self._fire_due(transaction=transaction, now=now, limit=limit)
        return fired

    async def fire_due_result(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> ReminderDeliveryResult:
        fired, generated, skipped, missed = await self._fire_due(
            transaction=transaction, now=now, limit=limit
        )
        return ReminderDeliveryResult(fired, generated, skipped, missed)

    async def create(
        self, issuer: str, subject: str, *, content: str, due_at: datetime, idempotency_key: str
    ) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        content = normalize_content(content)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            due = canonical_due_at(due_at)
            fingerprint = command_fingerprint(
                "create", None, content=content, dueAt=due.isoformat()
            )
            existing = await session.get(ReminderIdempotencyRow, (issuer, subject, idempotency_key))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    self._idempotency_outcome("conflict")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                self._idempotency_outcome("duplicate")
                # The stored response is the public/API projection and
                # intentionally omits owner identity. Rehydrate from the
                # stored response and restore only the authenticated owner;
                # this preserves exact replay even after later transitions.
                return from_payload(existing.response, issuer=issuer, subject=subject)
            due = ensure_due_at_future(due, now=self._time())
            now = self._time()
            row = ReminderRow(
                id=uuid4(),
                principal_issuer=issuer,
                principal_subject=subject,
                content=content,
                due_at=due,
                status=ReminderStatus.SCHEDULED.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            item = _item(row)
            await self._stage(session, item, "created", issuer, subject)
            response = as_payload(item)
            session.add(
                ReminderIdempotencyRow(
                    principal_issuer=issuer,
                    principal_subject=subject,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    reminder_id=item.id,
                    response=response,
                    created_at=now,
                )
            )
            self._idempotency_outcome("created")
            return item

    async def get(self, issuer: str, subject: str, reminder_id: UUID) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ReminderRow).where(
                        ReminderRow.id == reminder_id,
                        ReminderRow.principal_issuer == issuer,
                        ReminderRow.principal_subject == subject,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ReminderNotFound("reminder not found")
            return _item(row)

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        view: ReminderView,
        limit: int,
        cursor: str | None = None,
    ) -> ReminderPage:
        issuer, subject = _owner(issuer, subject)
        stamp_id = decode_cursor(cursor, issuer, subject, view) if cursor else None
        async with self.sessions() as session:
            query = select(ReminderRow).where(
                ReminderRow.principal_issuer == issuer, ReminderRow.principal_subject == subject
            )
            if view is ReminderView.UPCOMING:
                query = query.where(ReminderRow.status == ReminderStatus.SCHEDULED.value).order_by(
                    ReminderRow.due_at, ReminderRow.id
                )
                if stamp_id:
                    stamp, item_id = stamp_id
                    query = query.where(
                        (ReminderRow.due_at > stamp)
                        | ((ReminderRow.due_at == stamp) & (ReminderRow.id > item_id))
                    )
            else:
                query = query.where(ReminderRow.status != ReminderStatus.SCHEDULED.value).order_by(
                    ReminderRow.updated_at.desc(), ReminderRow.id.desc()
                )
                if stamp_id:
                    stamp, item_id = stamp_id
                    query = query.where(
                        (ReminderRow.updated_at < stamp)
                        | ((ReminderRow.updated_at == stamp) & (ReminderRow.id < item_id))
                    )
            rows = list((await session.execute(query.limit(limit + 1))).scalars().all())
            items = tuple(_item(row) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit and items:
                last = items[-1]
                next_cursor = encode_cursor(
                    issuer,
                    subject,
                    view,
                    last.due_at if view is ReminderView.UPCOMING else last.updated_at,
                    last.id,
                )
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
            if operation == "created":
                self._idempotency_outcome("failed")
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

    async def _transition(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        target: ReminderStatus,
    ) -> Reminder:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            existing = await session.get(ReminderIdempotencyRow, (issuer, subject, idempotency_key))
            fingerprint = command_fingerprint(
                target.value, reminder_id, expectedVersion=expected_version
            )
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._conflict("reminder_idempotency_conflict_count")
                    raise ReminderIdempotencyConflict("idempotency key was already used")
                return from_payload(existing.response, issuer=issuer, subject=subject)
            row = (
                await session.execute(
                    select(ReminderRow)
                    .where(
                        ReminderRow.id == reminder_id,
                        ReminderRow.principal_issuer == issuer,
                        ReminderRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ReminderNotFound("reminder not found")
            if row.version != expected_version:
                self._conflict("reminder_version_conflict_count")
                raise ReminderVersionConflict("reminder version does not match")
            if not (
                (
                    target is ReminderStatus.COMPLETED
                    and row.status in {ReminderStatus.SCHEDULED.value, ReminderStatus.FIRED.value}
                )
                or (
                    target is ReminderStatus.CANCELED
                    and row.status == ReminderStatus.SCHEDULED.value
                )
            ):
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
            occurrence = (
                await session.execute(
                    select(ReminderOccurrenceRow)
                    .where(ReminderOccurrenceRow.reminder_id == row.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if occurrence is not None:
                occurrence.status = (
                    ReminderOccurrenceStatus.COMPLETED
                    if target is ReminderStatus.COMPLETED
                    else ReminderOccurrenceStatus.SERIES_CANCELED
                ).value
                occurrence.updated_at = now
                if target is ReminderStatus.COMPLETED:
                    occurrence.completed_at = now
                if target is ReminderStatus.COMPLETED:
                    series = (
                        await session.execute(
                            select(ReminderSeriesRow)
                            .where(ReminderSeriesRow.id == occurrence.series_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if series is not None and series.next_reminder_id == row.id:
                        series.occurrence_count += 1
                        recurrence = RecurrenceRule.from_payload(
                            {
                                "frequency": series.frequency,
                                "interval": series.interval_value,
                                "weekdays": series.weekdays or [],
                                "monthDay": series.month_day,
                                "count": series.count_limit,
                                "endDate": series.end_date.isoformat() if series.end_date else None,
                            }
                        )
                        sequence = occurrence.occurrence_sequence + 1
                        local, due = next_occurrence(
                            recurrence,
                            occurrence.local_due_at,
                            series.timezone,
                            anchor_local=series.local_start,
                        )
                        skipped = 0
                        while due <= now:
                            if (recurrence.count is not None and sequence > recurrence.count) or (
                                recurrence.end_date is not None
                                and local.date() > recurrence.end_date
                            ):
                                break
                            skipped += 1
                            series.missed_count += 1
                            skipped_reminder = ReminderRow(
                                id=uuid4(),
                                principal_issuer=series.principal_issuer,
                                principal_subject=series.principal_subject,
                                content=series.content,
                                due_at=due,
                                status=ReminderStatus.CANCELED.value,
                                version=1,
                                created_at=now,
                                updated_at=now,
                                canceled_at=now,
                            )
                            session.add(skipped_reminder)
                            await session.flush()
                            session.add(
                                ReminderOccurrenceRow(
                                    id=uuid4(),
                                    series_id=series.id,
                                    reminder_id=skipped_reminder.id,
                                    occurrence_sequence=sequence,
                                    status=ReminderOccurrenceStatus.SKIPPED.value,
                                    local_due_at=local,
                                    due_at=due,
                                    created_at=now,
                                    updated_at=now,
                                    skipped_at=now,
                                )
                            )
                            sequence += 1
                            local, due = next_occurrence(
                                recurrence,
                                local,
                                series.timezone,
                                anchor_local=series.local_start,
                            )
                        terminal = (
                            recurrence.count is not None and sequence > recurrence.count
                        ) or (
                            recurrence.end_date is not None and local.date() > recurrence.end_date
                        )
                        if terminal:
                            series.status = ReminderSeriesStatus.COMPLETED.value
                            series.next_due_at = None
                            series.next_reminder_id = None
                            series.completed_at = now
                        else:
                            await self._materialize_sql_occurrence(
                                session, series, local, due, sequence, now
                            )
                        series.version += 1
                        series.updated_at = now
                        await self._stage_series(
                            session,
                            _series_item(series),
                            "completed" if terminal else "advanced",
                            occurrence.occurrence_sequence,
                            skipped,
                            series.missed_count,
                        )
            session.add(
                ReminderIdempotencyRow(
                    principal_issuer=issuer,
                    principal_subject=subject,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    reminder_id=item.id,
                    response=as_payload(item),
                    created_at=now,
                )
            )
            return item

    async def complete(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder:
        return await self._transition(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target=ReminderStatus.COMPLETED,
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
        return await self._transition(
            issuer,
            subject,
            reminder_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target=ReminderStatus.CANCELED,
        )

    async def create_series(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        local_start: datetime,
        timezone: str,
        rule: RecurrenceRule,
        idempotency_key: str,
    ) -> ReminderSeries:
        timer = Stopwatch()
        try:
            result = await self._create_series_impl(
                issuer,
                subject,
                content=content,
                local_start=local_start,
                timezone=timezone,
                rule=rule,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            outcome, error_class = _series_error_dimensions(error)
            self._series_metric(
                "reminder_series_command_outcome", operation="create", outcome=outcome
            )
            _record_series_span(
                self.metrics,
                operation="create",
                duration_ms=timer.elapsed_ms(),
                idempotency_key=idempotency_key,
                outcome=outcome,
                error_class=error_class,
                dependency="postgresql",
            )
            raise
        _record_series_span(
            self.metrics,
            operation="create",
            duration_ms=timer.elapsed_ms(),
            idempotency_key=idempotency_key,
            series_id=result.id,
            outcome="ok",
            dependency="postgresql",
            status=result.status,
            version=result.version,
        )
        return result

    async def _create_series_impl(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        local_start: datetime,
        timezone: str,
        rule: RecurrenceRule,
        idempotency_key: str,
    ) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        _validate_series_idempotency_key(idempotency_key)
        validate_timezone(timezone)
        if local_start.tzinfo is not None:
            raise ReminderValidationError("local_start must not contain an offset")
        normalized_content = normalize_content(content)
        fingerprint = command_fingerprint(
            "series.create",
            None,
            content=normalized_content,
            localStart=local_start.isoformat(),
            timezone=timezone,
            rule=rule.to_payload(),
        )
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            existing = await session.get(
                ReminderSeriesIdempotencyRow, (issuer, subject, idempotency_key)
            )
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ReminderSeriesIdempotencyConflict("idempotency key was already used")
                return _series_from_payload(existing.response, issuer, subject)
            now = self._time()
            due = rule.instant(local_start, timezone)
            if due <= now:
                raise ReminderValidationError("series first occurrence must be in the future")
            sid, rid = uuid4(), uuid4()
            row = ReminderSeriesRow(
                id=sid,
                principal_issuer=issuer,
                principal_subject=subject,
                content=normalized_content,
                frequency=rule.frequency.value,
                interval_value=rule.interval,
                weekdays=list(rule.weekdays),
                month_day=rule.month_day,
                count_limit=rule.count,
                end_date=rule.end_date,
                timezone=timezone,
                local_start=local_start,
                next_due_at=due,
                next_reminder_id=rid,
                status="active",
                version=1,
                occurrence_count=0,
                missed_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(
                ReminderRow(
                    id=rid,
                    principal_issuer=issuer,
                    principal_subject=subject,
                    content=row.content,
                    due_at=due,
                    status="scheduled",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            # The occurrence FK targets both rows above.  These ORM models are
            # intentionally relationship-free, so make parent insertion order
            # explicit before adding the child and staging the outbox event.
            await session.flush()
            session.add(
                ReminderOccurrenceRow(
                    id=uuid4(),
                    series_id=sid,
                    reminder_id=rid,
                    occurrence_sequence=1,
                    status="scheduled",
                    local_due_at=local_start,
                    due_at=due,
                    created_at=now,
                    updated_at=now,
                )
            )
            item = _series_item(row)
            await self._stage_series(session, item, "created", 1)
            self._series_metric("reminder_series_command_outcome", operation="create", outcome="ok")
            self._series_metric(
                "reminder_recurrence_calculation_outcome",
                operation="create",
                outcome="ok",
            )
            session.add(
                ReminderSeriesIdempotencyRow(
                    principal_issuer=issuer,
                    principal_subject=subject,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    series_id=item.id,
                    response=_series_payload(item),
                    operation="series.create",
                    created_at=now,
                )
            )
            return item

    async def get_series(self, issuer: str, subject: str, series_id: UUID) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ReminderSeriesRow).where(
                        ReminderSeriesRow.id == series_id,
                        ReminderSeriesRow.principal_issuer == issuer,
                        ReminderSeriesRow.principal_subject == subject,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ReminderSeriesNotFound("reminder series not found")
            return _series_item(row)

    async def list_series(
        self, issuer: str, subject: str, *, limit: int = 100
    ) -> ReminderSeriesPage:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ReminderSeriesRow)
                        .where(
                            ReminderSeriesRow.principal_issuer == issuer,
                            ReminderSeriesRow.principal_subject == subject,
                        )
                        .order_by(ReminderSeriesRow.updated_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return ReminderSeriesPage(tuple(_series_item(row) for row in rows), None)

    async def list_occurrences(
        self, issuer: str, subject: str, series_id: UUID, *, limit: int = 100
    ) -> tuple[ReminderOccurrence, ...]:
        await self.get_series(issuer, subject, series_id)
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ReminderOccurrenceRow)
                        .where(ReminderOccurrenceRow.series_id == series_id)
                        .order_by(ReminderOccurrenceRow.occurrence_sequence)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return tuple(_occurrence_item(row) for row in rows)

    async def _series_command(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        operation: str,
        idempotency_key: str,
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        timer = Stopwatch()
        short_operation = operation.removeprefix("series.")
        try:
            result = await self._series_command_impl(
                issuer,
                subject,
                series_id,
                expected_version=expected_version,
                operation=operation,
                idempotency_key=idempotency_key,
                content=content,
                local_start=local_start,
                timezone=timezone,
                rule=rule,
            )
        except Exception as error:
            outcome, error_class = _series_error_dimensions(error)
            self._series_metric(
                "reminder_series_command_outcome", operation=short_operation, outcome=outcome
            )
            if short_operation in {"resume", "skip", "update"}:
                self._series_metric(
                    "reminder_recurrence_calculation_outcome",
                    operation=short_operation,
                    outcome="error",
                )
            _record_series_span(
                self.metrics,
                operation=short_operation,
                duration_ms=timer.elapsed_ms(),
                idempotency_key=idempotency_key,
                series_id=series_id,
                outcome=outcome,
                error_class=error_class,
                dependency="postgresql",
            )
            raise
        _record_series_span(
            self.metrics,
            operation=short_operation,
            duration_ms=timer.elapsed_ms(),
            idempotency_key=idempotency_key,
            series_id=result.id,
            outcome="ok",
            dependency="postgresql",
            status=result.status,
            version=result.version,
        )
        if short_operation in {"resume", "skip", "update"}:
            self._series_metric(
                "reminder_recurrence_calculation_outcome", operation=short_operation, outcome="ok"
            )
        return result

    async def _series_command_impl(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        operation: str,
        idempotency_key: str,
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        issuer, subject = _owner(issuer, subject)
        _validate_series_idempotency_key(idempotency_key)
        normalized_content = normalize_content(content) if content is not None else None
        fingerprint = command_fingerprint(
            operation,
            series_id,
            expectedVersion=expected_version,
            content=normalized_content,
            localStart=local_start.isoformat() if local_start else None,
            timezone=timezone,
            rule=rule.to_payload() if rule else None,
        )
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _idempotency_lock_key(issuer, subject, idempotency_key)},
            )
            existing = await session.get(
                ReminderSeriesIdempotencyRow, (issuer, subject, idempotency_key)
            )
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._series_metric(
                        "reminder_series_idempotency_conflict_count",
                        operation=operation,
                        outcome="conflict",
                    )
                    raise ReminderSeriesIdempotencyConflict("idempotency key was already used")
                return _series_from_payload(existing.response, issuer, subject)
            row = (
                await session.execute(
                    select(ReminderSeriesRow)
                    .where(
                        ReminderSeriesRow.id == series_id,
                        ReminderSeriesRow.principal_issuer == issuer,
                        ReminderSeriesRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ReminderSeriesNotFound("reminder series not found")
            if row.version != expected_version:
                self._series_metric(
                    "reminder_series_version_conflict_count",
                    operation=operation,
                    outcome="conflict",
                )
                raise ReminderSeriesVersionConflict("series version does not match")
            now = self._time()
            pending = (
                await session.get(ReminderRow, row.next_reminder_id)
                if row.next_reminder_id
                else None
            )
            occurrence = None
            if row.next_reminder_id:
                occurrence = (
                    await session.execute(
                        select(ReminderOccurrenceRow)
                        .where(ReminderOccurrenceRow.reminder_id == row.next_reminder_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            if operation == "pause" and row.status != ReminderSeriesStatus.ACTIVE.value:
                raise ReminderSeriesTransitionError("series is not active")
            if operation == "resume" and row.status != ReminderSeriesStatus.PAUSED.value:
                raise ReminderSeriesTransitionError("series is not paused")
            if operation in {"skip", "update"} and row.status != ReminderSeriesStatus.ACTIVE.value:
                raise ReminderSeriesTransitionError("series is not active")
            if operation == "skip" and pending is None:
                raise ReminderSeriesTransitionError("series has no pending occurrence")
            if operation == "skip" and occurrence is None:
                raise ReminderSeriesTransitionError("series occurrence is missing")
            if operation == "cancel" and row.status in {
                ReminderSeriesStatus.COMPLETED.value,
                ReminderSeriesStatus.CANCELED.value,
            }:
                raise ReminderSeriesTransitionError("series is already terminal")
            if operation not in {"pause", "resume", "skip", "cancel", "update"}:
                raise ReminderSeriesTransitionError("unsupported series command")

            if pending and operation in {"pause", "skip", "cancel", "update"}:
                pending.status, pending.canceled_at = "canceled", now
            if occurrence and operation in {"pause", "skip", "cancel", "update"}:
                occurrence.status = {
                    "pause": "paused-window",
                    "skip": "skipped",
                    "cancel": "series-canceled",
                    "update": "replaced",
                }[operation]
                occurrence.updated_at = now

            if operation == "pause":
                row.status, row.paused_at = "paused", now
                row.next_due_at, row.next_reminder_id = None, None
            elif operation == "resume":
                row.status, row.paused_at = "active", None
                latest = (
                    await session.execute(
                        select(ReminderOccurrenceRow)
                        .where(ReminderOccurrenceRow.series_id == row.id)
                        .order_by(ReminderOccurrenceRow.occurrence_sequence.desc())
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                previous = latest.local_due_at if latest else row.local_start
                recurrence = RecurrenceRule.from_payload(
                    {
                        "frequency": row.frequency,
                        "interval": row.interval_value,
                        "weekdays": row.weekdays or [],
                        "monthDay": row.month_day,
                        "count": row.count_limit,
                        "endDate": row.end_date.isoformat() if row.end_date else None,
                    }
                )
                local, due = next_occurrence(
                    recurrence, previous, row.timezone, anchor_local=row.local_start
                )
                while due <= now:
                    local, due = next_occurrence(
                        recurrence, local, row.timezone, anchor_local=row.local_start
                    )
                sequence = (latest.occurrence_sequence if latest else 0) + 1
                if (recurrence.count is not None and sequence > recurrence.count) or (
                    recurrence.end_date is not None and local.date() > recurrence.end_date
                ):
                    row.status, row.completed_at = "completed", now
                    row.next_due_at, row.next_reminder_id = None, None
                else:
                    await self._materialize_sql_occurrence(session, row, local, due, sequence, now)
            elif operation == "skip":
                if occurrence is None:
                    raise ReminderSeriesTransitionError("series occurrence is missing")
                recurrence = RecurrenceRule.from_payload(
                    {
                        "frequency": row.frequency,
                        "interval": row.interval_value,
                        "weekdays": row.weekdays or [],
                        "monthDay": row.month_day,
                        "count": row.count_limit,
                        "endDate": row.end_date.isoformat() if row.end_date else None,
                    }
                )
                local, due = next_occurrence(
                    recurrence,
                    occurrence.local_due_at,
                    row.timezone,
                    anchor_local=row.local_start,
                )
                sequence = occurrence.occurrence_sequence + 1
                if (recurrence.count is not None and sequence > recurrence.count) or (
                    recurrence.end_date is not None and local.date() > recurrence.end_date
                ):
                    row.status, row.completed_at = "completed", now
                    row.next_due_at, row.next_reminder_id = None, None
                else:
                    await self._materialize_sql_occurrence(session, row, local, due, sequence, now)
            elif operation == "update":
                new_content = normalized_content if normalized_content is not None else row.content
                replacement_start = (
                    local_start
                    or (occurrence.local_due_at if occurrence is not None else row.local_start)
                )
                persisted_start = local_start or row.local_start
                new_timezone = timezone or row.timezone
                new_rule = rule or RecurrenceRule.from_payload(
                    {
                        "frequency": row.frequency,
                        "interval": row.interval_value,
                        "weekdays": row.weekdays or [],
                        "monthDay": row.month_day,
                        "count": row.count_limit,
                        "endDate": row.end_date.isoformat() if row.end_date else None,
                    }
                )
                validate_timezone(new_timezone)
                if replacement_start.tzinfo is not None:
                    raise ReminderValidationError("local_start must not contain an offset")
                due = new_rule.instant(replacement_start, new_timezone)
                if due <= now:
                    raise ReminderValidationError("edited occurrence must be in the future")
                row.content = new_content
                row.frequency = new_rule.frequency.value
                row.interval_value = new_rule.interval
                row.weekdays = list(new_rule.weekdays)
                row.month_day = new_rule.month_day
                row.count_limit = new_rule.count
                row.end_date = new_rule.end_date
                row.timezone = new_timezone
                row.local_start = persisted_start
                await self._materialize_sql_occurrence(
                    session,
                    row,
                    replacement_start,
                    due,
                    (occurrence.occurrence_sequence + 1 if occurrence else 1),
                    now,
                )
            elif operation == "cancel":
                row.status = "canceled"
                row.canceled_at = now
                row.next_due_at, row.next_reminder_id = None, None
            row.version += 1
            row.updated_at = now
            item = _series_item(row)
            await self._stage_series(
                session, item, operation, occurrence.occurrence_sequence if occurrence else 0
            )
            session.add(
                ReminderSeriesIdempotencyRow(
                    principal_issuer=issuer,
                    principal_subject=subject,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    series_id=item.id,
                    response=_series_payload(item),
                    operation=f"series.{operation}",
                    created_at=now,
                )
            )
            self._series_metric(
                "reminder_series_command_outcome", operation=operation, outcome="ok"
            )
            return item

    async def pause_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            operation="pause",
            idempotency_key=idempotency_key,
        )

    async def resume_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            operation="resume",
            idempotency_key=idempotency_key,
        )

    async def resume(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self.resume_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def skip_next(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            operation="skip",
            idempotency_key=idempotency_key,
        )

    async def cancel_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            operation="cancel",
            idempotency_key=idempotency_key,
        )

    async def update_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        content: str | None = None,
        local_start: datetime | None = None,
        timezone: str | None = None,
        rule: RecurrenceRule | None = None,
    ) -> ReminderSeries:
        return await self._series_command(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            operation="update",
            idempotency_key=idempotency_key,
            content=content,
            local_start=local_start,
            timezone=timezone,
            rule=rule,
        )


__all__ = ["MemoryReminderRepository", "SqlReminderRepository"]
