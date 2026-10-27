"""Deterministic in-memory and PostgreSQL briefing repositories."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import cast
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.briefings.persistence import (
    BriefingCommandIdempotencyRow,
    BriefingOccurrenceRow,
    BriefingResultRow,
    BriefingRunLineageRow,
    BriefingScheduleRow,
)
from aura_core.domains.automation.briefings.public import (
    BRIEFING_ID_NAMESPACE,
    BriefingCancellationLineage,
    BriefingCancellationPort,
    BriefingDailyCadence,
    BriefingDeletionReceipt,
    BriefingDeletionScope,
    BriefingExecutionPolicyReferences,
    BriefingIdempotencyConflict,
    BriefingLifecycleEventPort,
    BriefingNotFound,
    BriefingOccurrence,
    BriefingOccurrencePage,
    BriefingOccurrenceStatus,
    BriefingRepository,
    BriefingResult,
    BriefingResultStatus,
    BriefingSchedule,
    BriefingSchedulePage,
    BriefingScheduleStatus,
    BriefingSourceCompleteness,
    BriefingSourceGrants,
    BriefingTransitionError,
    BriefingValidationError,
    BriefingVersionConflict,
    as_deletion_event,
    as_occurrence_event,
    as_result_event,
    as_schedule_event,
    as_schedule_payload,
    briefing_phase_span_id,
    command_fingerprint,
    due_instant,
    next_due,
    occurrence_id,
    owner_key,
)
from aura_core.platform.telemetry import MetadataMetrics, new_span_id

_CURSOR_SECRET = b"aura-briefing-cursor-v1"


def _now() -> datetime:
    return datetime.now(UTC)


def _record_span(
    metrics: MetadataMetrics | None,
    *,
    component: str,
    operation: str,
    started: float,
    aggregate_id: UUID,
    outcome: str,
    error_class: str | None = None,
    run_id: UUID | None = None,
) -> None:
    if metrics is None:
        return
    phase = "cancellation" if component.endswith("briefing_cancellation") else "persistence"
    trace_id = run_id.hex if run_id is not None else aggregate_id.hex
    metrics.record_span(
        component,
        operation,
        (monotonic() - started) * 1000,
        trace_id=trace_id,
        span_id=(briefing_phase_span_id(run_id, phase) if run_id is not None else new_span_id()),
        parent_span_id=(
            briefing_phase_span_id(run_id, "admission") if run_id is not None else None
        ),
        dependency="postgresql",
        outcome=outcome,
        error_class=error_class,
        aggregate_type="automation.briefing",
        aggregate_id=str(aggregate_id),
    )
    metrics.increment(component, "briefing_outcome_count", outcome=outcome)


def _error_class(error: Exception) -> str:
    if isinstance(error, BriefingNotFound):
        return "not_found"
    if isinstance(error, BriefingVersionConflict):
        return "conflict"
    if isinstance(error, BriefingIdempotencyConflict):
        return "idempotency"
    if isinstance(error, BriefingValidationError):
        return "validation"
    if isinstance(error, BriefingTransitionError):
        return "lifecycle"
    return "persistence"


async def _request_cancellation(
    cancellation: BriefingCancellationPort,
    *,
    schedule_id: UUID,
    occurrence_id_value: UUID,
    run_id: UUID,
) -> object:
    result = cancellation.cancel_briefing(
        BriefingCancellationLineage(schedule_id, occurrence_id_value, run_id)
    )
    return await result if inspect.isawaitable(result) else result


async def _stage_lifecycle_event(
    stage: BriefingLifecycleEventPort | None,
    transaction: object,
    payload: Mapping[str, object],
    aggregate_id: UUID,
) -> None:
    if stage is None:
        return
    result = stage.stage_event(transaction, payload, aggregate_id)
    if inspect.isawaitable(result):
        await result


async def _replayed_command(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    operation: str,
    idempotency_key: str,
    fingerprint: str,
) -> dict[str, object] | None:
    row = await session.get(
        BriefingCommandIdempotencyRow,
        (issuer, subject, operation, idempotency_key),
    )
    if row is None:
        return None
    if row.fingerprint != fingerprint:
        raise BriefingIdempotencyConflict(
            "idempotency key was already used for another command"
        )
    return row.response


def _store_command(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    operation: str,
    idempotency_key: str,
    fingerprint: str,
    response: dict[str, object],
) -> None:
    session.add(
        BriefingCommandIdempotencyRow(
            principal_issuer=issuer,
            principal_subject=subject,
            operation=operation,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            response=response,
        )
    )


def _receipt_from_payload(payload: dict[str, object]) -> BriefingDeletionReceipt:
    return BriefingDeletionReceipt(
        UUID(str(payload["deletionId"])),
        BriefingDeletionScope(str(payload["scope"])),
        UUID(str(payload["scheduleId"])),
        UUID(str(payload["occurrenceId"])) if payload.get("occurrenceId") else None,
        UUID(str(payload["resultId"])) if payload.get("resultId") else None,
        datetime.fromisoformat(str(payload["deletedAt"])),
    )


def _normalise_now(value: datetime | None) -> datetime:
    current = value or _now()
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _cursor(index: int) -> str:
    raw = str(index).encode()
    signature = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(raw + b":" + signature).decode().rstrip("=")


def _cursor_index(value: str | None) -> int:
    if not value:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        raw, signature = decoded.split(b":", 1)
        expected = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        index = int(raw)
        if index < 0:
            raise ValueError
        return index
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BriefingValidationError("invalid briefing pagination cursor") from exc


class MemoryBriefingRepository(BriefingRepository):
    """A deterministic repository used by API and domain tests."""

    def __init__(
        self,
        *,
        metrics: MetadataMetrics | None = None,
        lifecycle_events: BriefingLifecycleEventPort | None = None,
    ) -> None:
        self.metrics = metrics
        self.lifecycle_events = lifecycle_events
        self.schedules: dict[UUID, BriefingSchedule] = {}
        self.occurrences: dict[UUID, BriefingOccurrence] = {}
        self.results: dict[UUID, BriefingResult] = {}
        self._commands: dict[tuple[str, str, str, str], tuple[str, object]] = {}
        self._history_deleted: set[UUID] = set()
        self._sequence: dict[UUID, int] = {}

    def _remember(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        payload: dict[str, object],
        response: object,
    ) -> object:
        fingerprint = command_fingerprint(operation, payload)
        command_key = (*owner, operation, key)
        previous = self._commands.get(command_key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise BriefingIdempotencyConflict(
                    "idempotency key was already used for another command"
                )
            return previous[1]
        self._commands[command_key] = (fingerprint, response)
        return response

    def _get_schedule(self, issuer: str, subject: str, schedule_id: UUID) -> BriefingSchedule:
        owner_key(issuer, subject)
        schedule = self.schedules.get(schedule_id)
        if schedule is None or schedule.owner != (issuer, subject):
            raise BriefingNotFound("briefing schedule not found")
        return schedule

    def _create_initial_occurrence(
        self, schedule: BriefingSchedule, now: datetime
    ) -> BriefingOccurrence:
        if schedule.next_due_at is None:
            raise BriefingValidationError("active briefing schedule requires next due instant")
        local = schedule.next_due_at.astimezone(ZoneInfo(schedule.cadence.time_zone))
        occurrence = BriefingOccurrence(
            id=occurrence_id(schedule.id, local.date()),
            schedule_id=schedule.id,
            issuer=schedule.issuer,
            subject=schedule.subject,
            sequence=1,
            status=BriefingOccurrenceStatus.SCHEDULED,
            due_at=schedule.next_due_at,
            due_local_date=local.date(),
            due_local_time=schedule.cadence.local_time,
            time_zone=schedule.cadence.time_zone,
            source_completeness=BriefingSourceCompleteness.UNKNOWN,
            run_id=None,
            result_id=None,
            created_at=now,
            updated_at=now,
        )
        self.occurrences[occurrence.id] = occurrence
        self._sequence[schedule.id] = 1
        return occurrence

    async def create_schedule(
        self,
        issuer: str,
        subject: str,
        *,
        display_name: str,
        cadence: BriefingDailyCadence,
        sources: BriefingSourceGrants,
        agent_revision_id: UUID,
        execution_policies: BriefingExecutionPolicyReferences,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> BriefingSchedule:
        owner = owner_key(issuer, subject)
        if not display_name.strip() or len(display_name) > 120:
            raise BriefingValidationError("displayName must contain 1 to 120 characters")
        current = _normalise_now(now)
        _, due = next_due(current - timedelta(microseconds=1), cadence)
        schedule = BriefingSchedule(
            uuid4(),
            issuer,
            subject,
            display_name,
            BriefingScheduleStatus.ACTIVE,
            1,
            cadence,
            sources,
            agent_revision_id,
            execution_policies,
            due,
            current,
            current,
        )
        existing = self._remember(
            owner,
            "schedule.create",
            idempotency_key,
            {
                "displayName": display_name,
                "cadence": cadence.as_payload(),
                "sources": sources.as_payload(),
                "agentRevisionId": str(agent_revision_id),
                "executionPolicies": execution_policies.as_payload(),
            },
            schedule,
        )
        if existing is not schedule:
            return cast(BriefingSchedule, existing)
        self.schedules[schedule.id] = schedule
        initial_occurrence = self._create_initial_occurrence(schedule, current)
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_schedule_event(schedule, "briefing.schedule.created"),
            schedule.id,
        )
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_occurrence_event(initial_occurrence),
            schedule.id,
        )
        return schedule

    async def list_schedules(
        self,
        issuer: str,
        subject: str,
        *,
        status: BriefingScheduleStatus | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingSchedulePage:
        owner_key(issuer, subject)
        if not 1 <= limit <= 100:
            raise BriefingValidationError("limit must be between 1 and 100")
        values = [
            x
            for x in self.schedules.values()
            if x.owner == (issuer, subject) and (status is None or x.status is status)
        ]
        values.sort(key=lambda x: (x.created_at, x.id.int))
        start = _cursor_index(cursor)
        items = tuple(values[start : start + limit])
        return BriefingSchedulePage(
            items, _cursor(start + limit) if start + limit < len(values) else None
        )

    async def get_schedule(self, issuer: str, subject: str, schedule_id: UUID) -> BriefingSchedule:
        return self._get_schedule(issuer, subject, schedule_id)

    async def _transition(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        operation: str,
    ) -> BriefingSchedule:
        schedule = self._get_schedule(issuer, subject, schedule_id)
        payload = {"scheduleId": str(schedule_id), "expectedVersion": expected_version}
        existing = self._commands.get((issuer, subject, operation, idempotency_key))
        if existing is not None:
            fingerprint = command_fingerprint(operation, payload)
            if existing[0] != fingerprint:
                raise BriefingIdempotencyConflict(
                    "idempotency key was already used for another command"
                )
            return cast(BriefingSchedule, existing[1])
        if schedule.version != expected_version:
            raise BriefingVersionConflict("briefing schedule version is stale")
        current = _now()
        if operation == "schedule.pause":
            if schedule.status is not BriefingScheduleStatus.ACTIVE:
                raise BriefingTransitionError("only an active schedule can be paused")
            for item in self.occurrences.values():
                if (
                    item.schedule_id == schedule.id
                    and item.status is BriefingOccurrenceStatus.SCHEDULED
                ):
                    self.occurrences[item.id] = replace(
                        item,
                        status=BriefingOccurrenceStatus.SKIPPED,
                        version=item.version + 1,
                        finished_at=current,
                        updated_at=current,
                    )
            updated = replace(
                schedule,
                status=BriefingScheduleStatus.PAUSED,
                version=schedule.version + 1,
                next_due_at=None,
                updated_at=current,
            )
        elif operation == "schedule.resume":
            if schedule.status is not BriefingScheduleStatus.PAUSED:
                raise BriefingTransitionError("only a paused schedule can be resumed")
            _, due = next_due(current, schedule.cadence)
            updated = replace(
                schedule,
                status=BriefingScheduleStatus.ACTIVE,
                version=schedule.version + 1,
                next_due_at=due,
                updated_at=current,
            )
            self._materialise_next(updated, current)
        else:
            raise BriefingValidationError("unsupported schedule operation")
        self.schedules[schedule_id] = updated
        self._commands[(issuer, subject, operation, idempotency_key)] = (
            command_fingerprint(operation, payload),
            updated,
        )
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_schedule_event(
                updated,
                "briefing.schedule.paused"
                if operation == "schedule.pause"
                else "briefing.schedule.resumed",
            ),
            schedule_id,
        )
        return updated

    def _materialise_next(self, schedule: BriefingSchedule, now: datetime) -> BriefingOccurrence:
        if schedule.next_due_at is None:
            raise BriefingValidationError("next due instant is required")
        local = schedule.next_due_at.astimezone(ZoneInfo(schedule.cadence.time_zone))
        item_id = occurrence_id(schedule.id, local.date())
        existing = self.occurrences.get(item_id)
        if existing is not None:
            return existing
        sequence = self._sequence.get(schedule.id, 0) + 1
        item = BriefingOccurrence(
            item_id,
            schedule.id,
            schedule.issuer,
            schedule.subject,
            sequence,
            BriefingOccurrenceStatus.SCHEDULED,
            schedule.next_due_at,
            local.date(),
            schedule.cadence.local_time,
            schedule.cadence.time_zone,
            BriefingSourceCompleteness.UNKNOWN,
            None,
            None,
            now,
            now,
        )
        self.occurrences[item.id] = item
        self._sequence[schedule.id] = sequence
        return item

    def _advance_after_claim(
        self, schedule: BriefingSchedule, claimed: BriefingOccurrence, now: datetime
    ) -> None:
        """Record missed local dates and materialize the next future date."""
        next_date = claimed.due_local_date + timedelta(days=1)
        while True:
            next_due_at = due_instant(
                next_date, schedule.cadence.local_time, schedule.cadence.time_zone
            )
            if next_due_at > now:
                updated = replace(schedule, next_due_at=next_due_at, updated_at=now)
                self.schedules[schedule.id] = updated
                self._materialise_next(updated, now)
                return
            skipped_id = occurrence_id(schedule.id, next_date)
            if skipped_id not in self.occurrences:
                sequence = self._sequence.get(schedule.id, 0) + 1
                self.occurrences[skipped_id] = BriefingOccurrence(
                    skipped_id,
                    schedule.id,
                    schedule.issuer,
                    schedule.subject,
                    sequence,
                    BriefingOccurrenceStatus.SKIPPED,
                    next_due_at,
                    next_date,
                    schedule.cadence.local_time,
                    schedule.cadence.time_zone,
                    BriefingSourceCompleteness.UNKNOWN,
                    None,
                    None,
                    now,
                    now,
                    finished_at=now,
                )
                self._sequence[schedule.id] = sequence
            next_date += timedelta(days=1)

    async def pause_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule:
        return await self._transition(
            issuer,
            subject,
            schedule_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="schedule.pause",
        )

    async def resume_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule:
        return await self._transition(
            issuer,
            subject,
            schedule_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            operation="schedule.resume",
        )

    async def delete_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        cancel_run: BriefingCancellationPort | None = None,
    ) -> BriefingDeletionReceipt:
        schedule = self._get_schedule(issuer, subject, schedule_id)
        payload = {
            "scheduleId": str(schedule_id),
            "expectedVersion": expected_version,
            "scope": "schedule",
        }
        existing = self._commands.get((issuer, subject, "schedule.delete", idempotency_key))
        if existing is not None:
            if existing[0] != command_fingerprint("schedule.delete", payload):
                raise BriefingIdempotencyConflict(
                    "idempotency key was already used for another command"
                )
            return cast(BriefingDeletionReceipt, existing[1])
        if schedule.version != expected_version:
            raise BriefingVersionConflict("briefing schedule version is stale")
        current = _now()
        if cancel_run is not None:
            for item in self.occurrences.values():
                if (
                    item.schedule_id == schedule_id
                    and item.run_id is not None
                    and item.status
                    in {
                        BriefingOccurrenceStatus.ADMITTED,
                        BriefingOccurrenceStatus.RUNNING,
                    }
                ):
                    cancellation_started = monotonic()
                    try:
                        await _request_cancellation(
                            cancel_run,
                            schedule_id=schedule_id,
                            occurrence_id_value=item.id,
                            run_id=item.run_id,
                        )
                    except Exception as error:
                        _record_span(
                            self.metrics,
                            component="aura.automation.briefing_cancellation",
                            operation="briefing.cancel",
                            started=cancellation_started,
                            aggregate_id=item.run_id,
                            outcome="error",
                            error_class=_error_class(error),
                            run_id=item.run_id,
                        )
                        raise
                    _record_span(
                        self.metrics,
                        component="aura.automation.briefing_cancellation",
                        operation="briefing.cancel",
                        started=cancellation_started,
                        aggregate_id=item.run_id,
                        outcome="ok",
                        run_id=item.run_id,
                    )
                    canceled = replace(
                        item,
                        status=BriefingOccurrenceStatus.CANCELED,
                        version=item.version + 1,
                        finished_at=current,
                        updated_at=current,
                    )
                    self.occurrences[item.id] = canceled
                    result_id = uuid5(
                        BRIEFING_ID_NAMESPACE,
                        f"canceled-result:{item.id}",
                    )
                    if result_id not in self.results:
                        self.results[result_id] = BriefingResult(
                            result_id,
                            item.schedule_id,
                            item.id,
                            issuer,
                            subject,
                            item.run_id,
                            BriefingResultStatus.CANCELED,
                            1,
                            item.source_completeness,
                            None,
                            item.created_at,
                            current,
                        )
                    self.occurrences[item.id] = replace(
                        self.occurrences[item.id], result_id=result_id
                    )
        self.schedules[schedule_id] = replace(
            schedule,
            status=BriefingScheduleStatus.DELETED,
            version=schedule.version + 1,
            next_due_at=None,
            deleted_at=current,
            updated_at=current,
        )
        receipt = BriefingDeletionReceipt(
            uuid4(), BriefingDeletionScope.SCHEDULE, schedule_id, None, None, current
        )
        self._commands[(issuer, subject, "schedule.delete", idempotency_key)] = (
            command_fingerprint("schedule.delete", payload),
            receipt,
        )
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_schedule_event(self.schedules[schedule_id], "briefing.schedule.deleted"),
            schedule_id,
        )
        return receipt

    def _owner_occurrences(
        self, issuer: str, subject: str, schedule_id: UUID
    ) -> list[BriefingOccurrence]:
        self._get_schedule(issuer, subject, schedule_id)
        return sorted(
            (
                x
                for x in self.occurrences.values()
                if x.schedule_id == schedule_id
                and x.owner == (issuer, subject)
                and x.id not in self._history_deleted
            ),
            key=lambda x: x.sequence,
            reverse=True,
        )

    async def list_occurrences(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingOccurrencePage:
        if not 1 <= limit <= 100:
            raise BriefingValidationError("limit must be between 1 and 100")
        values = self._owner_occurrences(issuer, subject, schedule_id)
        start = _cursor_index(cursor)
        items = tuple(values[start : start + limit])
        return BriefingOccurrencePage(
            items, _cursor(start + limit) if start + limit < len(values) else None
        )

    async def get_occurrence(
        self, issuer: str, subject: str, schedule_id: UUID, occurrence_id: UUID
    ) -> BriefingOccurrence:
        self._get_schedule(issuer, subject, schedule_id)
        item = self.occurrences.get(occurrence_id)
        if (
            item is None
            or item.schedule_id != schedule_id
            or item.owner != (issuer, subject)
            or item.id in self._history_deleted
        ):
            raise BriefingNotFound("briefing occurrence not found")
        return item

    async def delete_history(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_schedule_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt:
        schedule = self._get_schedule(issuer, subject, schedule_id)
        payload = {
            "scheduleId": str(schedule_id),
            "expectedScheduleVersion": expected_schedule_version,
            "scope": "occurrence_history",
        }
        key = (issuer, subject, "history.delete", idempotency_key)
        existing = self._commands.get(key)
        if existing:
            if existing[0] != command_fingerprint("history.delete", payload):
                raise BriefingIdempotencyConflict(
                    "idempotency key was already used for another command"
                )
            return cast(BriefingDeletionReceipt, existing[1])
        if schedule.version != expected_schedule_version:
            raise BriefingVersionConflict("briefing schedule version is stale")
        self._history_deleted.update(
            x.id
            for x in self.occurrences.values()
            if x.schedule_id == schedule_id
            and x.owner == (issuer, subject)
            and x.status
            in {
                BriefingOccurrenceStatus.COMPLETED,
                BriefingOccurrenceStatus.PARTIAL,
                BriefingOccurrenceStatus.FAILED,
                BriefingOccurrenceStatus.CANCELED,
                BriefingOccurrenceStatus.SKIPPED,
                BriefingOccurrenceStatus.UNKNOWN,
            }
        )
        current = _now()
        receipt = BriefingDeletionReceipt(
            uuid4(), BriefingDeletionScope.OCCURRENCE_HISTORY, schedule_id, None, None, current
        )
        self._commands[key] = (command_fingerprint("history.delete", payload), receipt)
        await _stage_lifecycle_event(
            self.lifecycle_events, None, as_deletion_event(receipt), schedule_id
        )
        return receipt

    async def get_result(self, issuer: str, subject: str, result_id: UUID) -> BriefingResult:
        owner_key(issuer, subject)
        result = self.results.get(result_id)
        if result is None or result.issuer != issuer or result.subject != subject:
            raise BriefingNotFound("briefing result not found")
        return result

    async def delete_result_content(
        self,
        issuer: str,
        subject: str,
        result_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt:
        result = await self.get_result(issuer, subject, result_id)
        payload = {
            "resultId": str(result_id),
            "expectedVersion": expected_version,
            "scope": "result_content",
        }
        key = (issuer, subject, "result.delete", idempotency_key)
        existing = self._commands.get(key)
        if existing:
            if existing[0] != command_fingerprint("result.delete", payload):
                raise BriefingIdempotencyConflict(
                    "idempotency key was already used for another command"
                )
            return cast(BriefingDeletionReceipt, existing[1])
        if result.version != expected_version:
            raise BriefingVersionConflict("briefing result version is stale")
        current = _now()
        updated = replace(
            result,
            status=BriefingResultStatus.DELETED,
            content=None,
            version=result.version + 1,
            deleted_at=current,
            updated_at=current,
            original_status=result.original_status or result.status,
        )
        self.results[result_id] = updated
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_result_event(updated),
            result.schedule_id,
        )
        receipt = BriefingDeletionReceipt(
            uuid4(),
            BriefingDeletionScope.RESULT_CONTENT,
            result.schedule_id,
            result.occurrence_id,
            result_id,
            current,
        )
        self._commands[key] = (command_fingerprint("result.delete", payload), receipt)
        await _stage_lifecycle_event(
            self.lifecycle_events, None, as_deletion_event(receipt), result.schedule_id
        )
        return receipt

    async def create_result(self, result: BriefingResult) -> BriefingResult:
        started = monotonic()
        occurrence = self.occurrences.get(result.occurrence_id)
        if (
            occurrence is None
            or occurrence.schedule_id != result.schedule_id
            or occurrence.owner != result.owner
        ):
            _record_span(
                self.metrics,
                component="aura.automation.briefing_persistence",
                operation="briefing.result.persist",
                started=started,
                aggregate_id=result.id,
                outcome="error",
                error_class="not_found",
                run_id=result.run_id,
            )
            raise BriefingNotFound("briefing occurrence not found")
        existing = self.results.get(result.id)
        if existing is not None:
            if existing.owner != result.owner:
                _record_span(
                    self.metrics,
                    component="aura.automation.briefing_persistence",
                    operation="briefing.result.persist",
                    started=started,
                    aggregate_id=result.id,
                    outcome="error",
                    error_class="not_found",
                    run_id=result.run_id,
                )
                raise BriefingNotFound("briefing result not found")
            _record_span(
                self.metrics,
                component="aura.automation.briefing_persistence",
                operation="briefing.result.persist",
                started=started,
                aggregate_id=result.id,
                outcome="duplicate",
                run_id=result.run_id,
            )
            return existing
        for previous in self.results.values():
            if previous.occurrence_id == result.occurrence_id:
                if previous.owner != result.owner:
                    _record_span(
                        self.metrics,
                        component="aura.automation.briefing_persistence",
                        operation="briefing.result.persist",
                        started=started,
                        aggregate_id=result.id,
                        outcome="error",
                        error_class="not_found",
                        run_id=result.run_id,
                    )
                    raise BriefingNotFound("briefing result not found")
                _record_span(
                    self.metrics,
                    component="aura.automation.briefing_persistence",
                    operation="briefing.result.persist",
                    started=started,
                    aggregate_id=result.id,
                    outcome="duplicate",
                    run_id=result.run_id,
                )
                return previous
        self.results[result.id] = result
        self.occurrences[result.occurrence_id] = replace(
            occurrence,
            result_id=result.id,
            status=BriefingOccurrenceStatus(result.status.value),
            source_completeness=result.source_completeness,
            version=occurrence.version + 1,
            finished_at=result.updated_at,
            updated_at=result.updated_at,
        )
        await _stage_lifecycle_event(
            self.lifecycle_events,
            None,
            as_occurrence_event(self.occurrences[result.occurrence_id]),
            result.schedule_id,
        )
        await _stage_lifecycle_event(
            self.lifecycle_events, None, as_result_event(result), result.schedule_id
        )
        _record_span(
            self.metrics,
            component="aura.automation.briefing_persistence",
            operation="briefing.result.persist",
            started=started,
            aggregate_id=result.id,
            outcome="ok",
            run_id=result.run_id,
        )
        return result

    async def update_result(self, result: BriefingResult) -> BriefingResult:
        existing = self.results.get(result.id)
        if existing is None or existing.owner != result.owner:
            raise BriefingNotFound("briefing result not found")
        self.results[result.id] = result
        return result

    async def admit_occurrence(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        occurrence_id: UUID,
        *,
        run_id: UUID,
        expected_version: int | None = None,
    ) -> BriefingOccurrence:
        schedule = self._get_schedule(issuer, subject, schedule_id)
        if schedule.status is not BriefingScheduleStatus.ACTIVE:
            raise BriefingTransitionError("briefing schedule is not active")
        item = await self.get_occurrence(issuer, subject, schedule_id, occurrence_id)
        if expected_version is not None and item.version != expected_version:
            raise BriefingVersionConflict("briefing occurrence version is stale")
        if item.run_id is not None:
            if item.run_id != run_id:
                raise BriefingTransitionError("briefing occurrence already has a run")
            return item
        if item.status not in {
            BriefingOccurrenceStatus.SCHEDULED,
            BriefingOccurrenceStatus.ADMITTED,
        }:
            raise BriefingTransitionError("briefing occurrence is not schedulable")
        current = _now()
        updated = replace(
            item,
            status=BriefingOccurrenceStatus.ADMITTED,
            run_id=run_id,
            version=item.version + 1,
            started_at=current,
            updated_at=current,
        )
        self.occurrences[item.id] = updated
        await _stage_lifecycle_event(
            self.lifecycle_events, None, as_occurrence_event(updated), schedule_id
        )
        return updated

    async def recover_stale_claims(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> tuple[BriefingOccurrence, ...]:
        current = _normalise_now(now)
        cutoff = current - timedelta(minutes=15)
        recovered: list[BriefingOccurrence] = []
        for item in sorted(self.occurrences.values(), key=lambda value: value.updated_at):
            if len(recovered) >= limit:
                break
            if (
                item.status in {BriefingOccurrenceStatus.ADMITTED, BriefingOccurrenceStatus.RUNNING}
                and item.updated_at <= cutoff
                and item.id not in self._history_deleted
            ):
                updated = replace(
                    item,
                    status=BriefingOccurrenceStatus.UNKNOWN,
                    version=item.version + 1,
                    finished_at=current,
                    updated_at=current,
                )
                self.occurrences[item.id] = updated
                recovered.append(updated)
                result_id = uuid5(BRIEFING_ID_NAMESPACE, f"recovery-result:{item.id}")
                result = self.results.get(result_id)
                if result is None:
                    result = BriefingResult(
                        result_id,
                        item.schedule_id,
                        item.id,
                        item.issuer,
                        item.subject,
                        item.run_id,
                        BriefingResultStatus.UNKNOWN,
                        1,
                        item.source_completeness,
                        None,
                        item.created_at,
                        current,
                    )
                    self.results[result_id] = result
                self.occurrences[item.id] = replace(updated, result_id=result.id)
                recovered[-1] = self.occurrences[item.id]
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    None,
                    as_occurrence_event(self.occurrences[item.id]),
                    item.schedule_id,
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events, None, as_result_event(result), item.schedule_id
                )
        return tuple(recovered)

    async def get_occurrence_by_id(self, occurrence_id: UUID) -> BriefingOccurrence:
        item = self.occurrences.get(occurrence_id)
        if item is None:
            raise BriefingNotFound("briefing occurrence not found")
        return item

    async def claim_due_occurrences(
        self, now: datetime, *, limit: int = 20
    ) -> tuple[BriefingOccurrence, ...]:
        due = [
            x
            for x in self.occurrences.values()
            if x.status is BriefingOccurrenceStatus.SCHEDULED
            and x.due_at <= now
            and self.schedules.get(x.schedule_id) is not None
            and self.schedules[x.schedule_id].status is BriefingScheduleStatus.ACTIVE
        ]
        due.sort(key=lambda x: (x.due_at, x.id.int))
        claimed: list[BriefingOccurrence] = []
        for item in due[:limit]:
            updated = replace(
                item,
                status=BriefingOccurrenceStatus.ADMITTED,
                version=item.version + 1,
                updated_at=now,
            )
            self.occurrences[item.id] = updated
            schedule = self.schedules.get(item.schedule_id)
            if schedule is not None:
                self._advance_after_claim(schedule, updated, now)
            await _stage_lifecycle_event(
                self.lifecycle_events, None, as_occurrence_event(updated), item.schedule_id
            )
            claimed.append(updated)
        return tuple(claimed)

    async def claim_due_occurrences_and_stage(
        self,
        now: datetime,
        *,
        limit: int = 20,
        stage: Callable[[object, BriefingOccurrence], object | Awaitable[object]],
    ) -> tuple[BriefingOccurrence, ...]:
        claimed = await self.claim_due_occurrences(now, limit=limit)
        for occurrence in claimed:
            result = stage(None, occurrence)
            if inspect.isawaitable(result):
                await result
        return claimed


def _schedule_from_row(row: BriefingScheduleRow) -> BriefingSchedule:
    return BriefingSchedule(
        UUID(str(row.id)),
        row.principal_issuer,
        row.principal_subject,
        row.display_name,
        BriefingScheduleStatus(row.status),
        row.version,
        BriefingDailyCadence(time.fromisoformat(str(row.local_time)), row.time_zone),
        BriefingSourceGrants(
            bool(row.include_reminders),
            tuple(UUID(str(x)) for x in row.calendar_ids),
            tuple(UUID(str(x)) for x in row.document_ids),
        ),
        row.agent_revision_id,
        BriefingExecutionPolicyReferences.from_payload(row.execution_policies),
        row.next_due_at,
        row.created_at,
        row.updated_at,
        row.deleted_at,
    )


def _schedule_from_payload(
    payload: Mapping[str, object], issuer: str, subject: str
) -> BriefingSchedule:
    def timestamp(name: str) -> datetime:
        value = payload.get(name)
        if not isinstance(value, str):
            raise BriefingValidationError("briefing command receipt is malformed")
        return datetime.fromisoformat(value)

    try:
        return BriefingSchedule(
            UUID(str(payload["id"])),
            issuer,
            subject,
            str(payload["displayName"]),
            BriefingScheduleStatus(str(payload["status"])),
            int(cast(int | str, payload["version"])),
            BriefingDailyCadence.from_payload(cast(Mapping[str, object], payload["cadence"])),
            BriefingSourceGrants.from_payload(cast(Mapping[str, object], payload["sources"])),
            UUID(str(payload["agentRevisionId"])),
            BriefingExecutionPolicyReferences.from_payload(
                cast(Mapping[str, object], payload["executionPolicies"])
            ),
            (
                datetime.fromisoformat(str(payload["nextDueAt"]))
                if payload.get("nextDueAt")
                else None
            ),
            timestamp("createdAt"),
            timestamp("updatedAt"),
            datetime.fromisoformat(str(payload["deletedAt"]))
            if payload.get("deletedAt")
            else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BriefingValidationError("briefing command receipt is malformed") from error


class SqlBriefingRepository(BriefingRepository):
    """PostgreSQL adapter. Mutations are owner-scoped and transaction-bound."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        metrics: MetadataMetrics | None = None,
        lifecycle_events: BriefingLifecycleEventPort | None = None,
    ) -> None:
        self.sessions = sessions
        self.metrics = metrics
        self.lifecycle_events = lifecycle_events

    async def create_schedule(
        self,
        issuer: str,
        subject: str,
        *,
        display_name: str,
        cadence: BriefingDailyCadence,
        sources: BriefingSourceGrants,
        agent_revision_id: UUID,
        execution_policies: BriefingExecutionPolicyReferences,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> BriefingSchedule:
        owner_key(issuer, subject)
        current = _normalise_now(now)
        _, due = next_due(current - timedelta(microseconds=1), cadence)
        item = BriefingSchedule(
            uuid4(),
            issuer,
            subject,
            display_name,
            BriefingScheduleStatus.ACTIVE,
            1,
            cadence,
            sources,
            agent_revision_id,
            execution_policies,
            due,
            current,
            current,
        )
        async with self.sessions() as session:
            async with session.begin():
                existing = await session.get(
                    BriefingCommandIdempotencyRow,
                    (issuer, subject, "schedule.create", idempotency_key),
                )
                fingerprint = command_fingerprint(
                    "schedule.create",
                    {
                        "displayName": display_name,
                        "cadence": cadence.as_payload(),
                        "sources": sources.as_payload(),
                        "agentRevisionId": str(agent_revision_id),
                        "executionPolicies": execution_policies.as_payload(),
                    },
                )
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise BriefingIdempotencyConflict(
                            "idempotency key was already used for another command"
                        )
                    replayed = await session.get(
                        BriefingScheduleRow, UUID(str(existing.response["scheduleId"]))
                    )
                    if replayed is None:
                        raise BriefingNotFound("briefing schedule not found")
                    return _schedule_from_row(replayed)
                session.add(
                    BriefingScheduleRow(
                        id=item.id,
                        principal_issuer=issuer,
                        principal_subject=subject,
                        display_name=display_name,
                        status=item.status.value,
                        version=1,
                        local_time=cadence.local_time_text,
                        time_zone=cadence.time_zone,
                        include_reminders=sources.include_reminders,
                        calendar_ids=[str(x) for x in sources.calendar_ids],
                        document_ids=[str(x) for x in sources.document_ids],
                        agent_revision_id=agent_revision_id,
                        execution_policies=execution_policies.as_payload(),
                        next_due_at=due,
                        created_at=current,
                        updated_at=current,
                    )
                )
                local = due.astimezone(ZoneInfo(cadence.time_zone))
                initial_occurrence = BriefingOccurrenceRow(
                        id=occurrence_id(item.id, local.date()),
                        schedule_id=item.id,
                        principal_issuer=issuer,
                        principal_subject=subject,
                        occurrence_sequence=1,
                        status=BriefingOccurrenceStatus.SCHEDULED.value,
                        due_at=due,
                        due_local_date=local.date(),
                        due_local_time=cadence.local_time_text,
                        time_zone=cadence.time_zone,
                        source_completeness=BriefingSourceCompleteness.UNKNOWN.value,
                        version=1,
                        created_at=current,
                        updated_at=current,
                )
                session.add(initial_occurrence)
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_schedule_event(item, "briefing.schedule.created"),
                    item.id,
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_occurrence_event(_occurrence_from_row(initial_occurrence)),
                    item.id,
                )
                session.add(
                    BriefingCommandIdempotencyRow(
                        principal_issuer=issuer,
                        principal_subject=subject,
                        operation="schedule.create",
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        response={"scheduleId": str(item.id)},
                    )
                )
        return item

    async def list_schedules(
        self,
        issuer: str,
        subject: str,
        *,
        status: BriefingScheduleStatus | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingSchedulePage:
        owner_key(issuer, subject)
        async with self.sessions() as session:
            stmt = (
                select(BriefingScheduleRow)
                .where(
                    BriefingScheduleRow.principal_issuer == issuer,
                    BriefingScheduleRow.principal_subject == subject,
                )
                .order_by(BriefingScheduleRow.created_at, BriefingScheduleRow.id)
                .limit(limit)
            )
            if status is not None:
                stmt = stmt.where(BriefingScheduleRow.status == status.value)
            rows = (await session.scalars(stmt)).all()
        return BriefingSchedulePage(tuple(_schedule_from_row(row) for row in rows), None)

    async def get_schedule(self, issuer: str, subject: str, schedule_id: UUID) -> BriefingSchedule:
        async with self.sessions() as session:
            row = await session.scalar(
                select(BriefingScheduleRow).where(
                    BriefingScheduleRow.id == schedule_id,
                    BriefingScheduleRow.principal_issuer == issuer,
                    BriefingScheduleRow.principal_subject == subject,
                )
            )
        if row is None:
            raise BriefingNotFound("briefing schedule not found")
        return _schedule_from_row(row)

    async def pause_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule:
        return await self._set_status(
            issuer,
            subject,
            schedule_id,
            expected_version,
            idempotency_key,
            BriefingScheduleStatus.PAUSED,
        )

    async def resume_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule:
        return await self._set_status(
            issuer,
            subject,
            schedule_id,
            expected_version,
            idempotency_key,
            BriefingScheduleStatus.ACTIVE,
        )

    async def _set_status(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        expected_version: int,
        idempotency_key: str,
        target: BriefingScheduleStatus,
    ) -> BriefingSchedule:
        current = _now()
        operation = (
            "schedule.pause"
            if target is BriefingScheduleStatus.PAUSED
            else "schedule.resume"
        )
        payload = {
            "scheduleId": str(schedule_id),
            "expectedVersion": expected_version,
        }
        fingerprint = command_fingerprint(operation, payload)
        async with self.sessions() as session:
            async with session.begin():
                replayed = await _replayed_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replayed is not None:
                    return _schedule_from_payload(replayed, issuer, subject)
                row = await session.scalar(
                    select(BriefingScheduleRow)
                    .where(
                        BriefingScheduleRow.id == schedule_id,
                        BriefingScheduleRow.principal_issuer == issuer,
                        BriefingScheduleRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise BriefingNotFound("briefing schedule not found")
                if row.version != expected_version:
                    raise BriefingVersionConflict("briefing schedule version is stale")
                if target is BriefingScheduleStatus.PAUSED and row.status != "active":
                    raise BriefingTransitionError("only an active schedule can be paused")
                if target is BriefingScheduleStatus.ACTIVE and row.status != "paused":
                    raise BriefingTransitionError("only a paused schedule can be resumed")
                row.status = target.value
                row.version += 1
                row.updated_at = current
                if target is BriefingScheduleStatus.PAUSED:
                    paused_rows = (
                        await session.scalars(
                            select(BriefingOccurrenceRow)
                            .where(
                                BriefingOccurrenceRow.schedule_id == row.id,
                                BriefingOccurrenceRow.status
                                == BriefingOccurrenceStatus.SCHEDULED.value,
                                BriefingOccurrenceRow.deleted_at.is_(None),
                            )
                            .with_for_update()
                        )
                    ).all()
                    for occurrence in paused_rows:
                        occurrence.status = BriefingOccurrenceStatus.SKIPPED.value
                        occurrence.finished_at = current
                        occurrence.updated_at = current
                        occurrence.version += 1
                        await _stage_lifecycle_event(
                            self.lifecycle_events,
                            session,
                            as_occurrence_event(_occurrence_from_row(occurrence)),
                            row.id,
                        )
                    row.next_due_at = None
                else:
                    cadence = BriefingDailyCadence(
                        time.fromisoformat(str(row.local_time)), row.time_zone
                    )
                    _, next_due_at = next_due(current, cadence)
                    row.next_due_at = next_due_at
                    local_date = next_due_at.astimezone(ZoneInfo(cadence.time_zone)).date()
                    existing_occurrence = await session.scalar(
                        select(BriefingOccurrenceRow).where(
                            BriefingOccurrenceRow.schedule_id == row.id,
                            BriefingOccurrenceRow.due_local_date == local_date,
                        )
                    )
                    if existing_occurrence is None:
                        max_sequence = await session.scalar(
                            select(func.max(BriefingOccurrenceRow.occurrence_sequence)).where(
                                BriefingOccurrenceRow.schedule_id == row.id
                            )
                        )
                        resumed_occurrence = BriefingOccurrenceRow(
                            id=occurrence_id(row.id, local_date),
                            schedule_id=row.id,
                            principal_issuer=issuer,
                            principal_subject=subject,
                            occurrence_sequence=int(max_sequence or 0) + 1,
                            status=BriefingOccurrenceStatus.SCHEDULED.value,
                            due_at=row.next_due_at,
                            due_local_date=local_date,
                            due_local_time=cadence.local_time_text,
                            time_zone=cadence.time_zone,
                            source_completeness=BriefingSourceCompleteness.UNKNOWN.value,
                            version=1,
                            created_at=current,
                            updated_at=current,
                        )
                        session.add(resumed_occurrence)
                        await _stage_lifecycle_event(
                            self.lifecycle_events,
                            session,
                            as_occurrence_event(_occurrence_from_row(resumed_occurrence)),
                            row.id,
                        )
                _store_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response=as_schedule_payload(_schedule_from_row(row)),
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_schedule_event(
                        _schedule_from_row(row),
                        "briefing.schedule.paused"
                        if target is BriefingScheduleStatus.PAUSED
                        else "briefing.schedule.resumed",
                    ),
                    row.id,
                )
            return _schedule_from_row(row)

    async def delete_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        cancel_run: BriefingCancellationPort | None = None,
    ) -> BriefingDeletionReceipt:
        current = _now()
        payload = {
            "scheduleId": str(schedule_id),
            "expectedVersion": expected_version,
            "scope": BriefingDeletionScope.SCHEDULE.value,
        }
        fingerprint = command_fingerprint("schedule.delete", payload)
        async with self.sessions() as session:
            async with session.begin():
                replayed = await _replayed_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="schedule.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replayed is not None:
                    return _receipt_from_payload(replayed)
                row = await session.scalar(
                    select(BriefingScheduleRow)
                    .where(
                        BriefingScheduleRow.id == schedule_id,
                        BriefingScheduleRow.principal_issuer == issuer,
                        BriefingScheduleRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise BriefingNotFound("briefing schedule not found")
                if row.version != expected_version:
                    raise BriefingVersionConflict("briefing schedule version is stale")
                occurrences = (
                    await session.scalars(
                        select(BriefingOccurrenceRow)
                        .where(
                            BriefingOccurrenceRow.schedule_id == schedule_id,
                            BriefingOccurrenceRow.principal_issuer == issuer,
                            BriefingOccurrenceRow.principal_subject == subject,
                            BriefingOccurrenceRow.status.in_((
                                BriefingOccurrenceStatus.ADMITTED.value,
                                BriefingOccurrenceStatus.RUNNING.value,
                            )),
                            BriefingOccurrenceRow.run_id.is_not(None),
                        )
                    )
                ).all()
                if occurrences and cancel_run is None:
                    raise BriefingTransitionError(
                        "briefing run cancellation boundary is unavailable"
                    )
                if cancel_run is not None:
                    for occurrence in occurrences:
                        assert occurrence.run_id is not None
                        cancellation_started = monotonic()
                        try:
                            await _request_cancellation(
                                cancel_run,
                                schedule_id=schedule_id,
                                occurrence_id_value=occurrence.id,
                                run_id=occurrence.run_id,
                            )
                        except Exception as error:
                            _record_span(
                                self.metrics,
                                component="aura.automation.briefing_cancellation",
                                operation="briefing.cancel",
                                started=cancellation_started,
                                aggregate_id=occurrence.run_id,
                                outcome="error",
                                error_class=_error_class(error),
                                run_id=occurrence.run_id,
                            )
                            raise
                        _record_span(
                            self.metrics,
                            component="aura.automation.briefing_cancellation",
                            operation="briefing.cancel",
                            started=cancellation_started,
                            aggregate_id=occurrence.run_id,
                            outcome="ok",
                            run_id=occurrence.run_id,
                        )
                        # The typed cancellation adapter may durably settle
                        # the occurrence through the public state port. Do not
                        # hold an occurrence row lock while invoking it: its
                        # state transaction must be able to acquire that lock.
                        # Reload and lock only after the callback returns, then
                        # settle locally iff the callback did not already do so.
                        settled_occurrence = await session.scalar(
                            select(BriefingOccurrenceRow)
                            .where(
                                BriefingOccurrenceRow.id == occurrence.id,
                                BriefingOccurrenceRow.schedule_id == schedule_id,
                                BriefingOccurrenceRow.principal_issuer == issuer,
                                BriefingOccurrenceRow.principal_subject == subject,
                            )
                            .with_for_update()
                        )
                        if settled_occurrence is None:
                            raise BriefingNotFound("briefing occurrence not found")
                        if (
                            settled_occurrence.status
                            not in {
                                BriefingOccurrenceStatus.ADMITTED.value,
                                BriefingOccurrenceStatus.RUNNING.value,
                            }
                            and settled_occurrence.result_id is not None
                        ):
                            continue
                        # Settlement is part of the same transaction as the
                        # deletion when the callback only requests external
                        # cancellation. A callback that already settled state
                        # is detected above, so this path is exactly once.
                        settled_occurrence.status = BriefingOccurrenceStatus.CANCELED.value
                        settled_occurrence.version += 1
                        settled_occurrence.finished_at = current
                        settled_occurrence.updated_at = current
                        existing_result = await session.scalar(
                            select(BriefingResultRow)
                            .where(
                                BriefingResultRow.occurrence_id == settled_occurrence.id,
                                BriefingResultRow.principal_issuer == issuer,
                                BriefingResultRow.principal_subject == subject,
                            )
                            .with_for_update()
                        )
                        if existing_result is None:
                            canceled_result_id = uuid5(
                                BRIEFING_ID_NAMESPACE,
                                f"canceled-result:{occurrence.id}",
                            )
                            existing_result = BriefingResultRow(
                                id=canceled_result_id,
                                schedule_id=schedule_id,
                                occurrence_id=settled_occurrence.id,
                                principal_issuer=issuer,
                                principal_subject=subject,
                                run_id=settled_occurrence.run_id,
                                status=BriefingResultStatus.CANCELED.value,
                                original_status=None,
                                version=1,
                                source_completeness=settled_occurrence.source_completeness,
                                content=None,
                                created_at=settled_occurrence.created_at,
                                updated_at=current,
                                deleted_at=None,
                            )
                            session.add(existing_result)
                        settled_occurrence.result_id = existing_result.id
                        await _stage_lifecycle_event(
                            self.lifecycle_events,
                            session,
                            as_occurrence_event(_occurrence_from_row(settled_occurrence)),
                            schedule_id,
                        )
                        if existing_result.status == BriefingResultStatus.CANCELED.value:
                            await _stage_lifecycle_event(
                                self.lifecycle_events,
                                session,
                                as_result_event(_result_from_row(existing_result)),
                                schedule_id,
                            )
                row.status, row.version, row.next_due_at, row.deleted_at, row.updated_at = (
                    "deleted",
                    row.version + 1,
                    None,
                    current,
                    current,
                )
                receipt = BriefingDeletionReceipt(
                    uuid4(), BriefingDeletionScope.SCHEDULE, schedule_id, None, None, current
                )
                _store_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="schedule.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response=receipt.as_payload(),
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_schedule_event(
                        _schedule_from_row(row), "briefing.schedule.deleted"
                    ),
                    schedule_id,
                )
        return receipt

    async def list_occurrences(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingOccurrencePage:
        await self.get_schedule(issuer, subject, schedule_id)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(BriefingOccurrenceRow)
                    .where(
                        BriefingOccurrenceRow.schedule_id == schedule_id,
                        BriefingOccurrenceRow.principal_issuer == issuer,
                        BriefingOccurrenceRow.principal_subject == subject,
                        BriefingOccurrenceRow.deleted_at.is_(None),
                    )
                    .order_by(BriefingOccurrenceRow.occurrence_sequence.desc())
                    .limit(limit)
                )
            ).all()
        return BriefingOccurrencePage(tuple(_occurrence_from_row(row) for row in rows), None)

    async def get_occurrence(
        self, issuer: str, subject: str, schedule_id: UUID, occurrence_id: UUID
    ) -> BriefingOccurrence:
        await self.get_schedule(issuer, subject, schedule_id)
        async with self.sessions() as session:
            row = await session.scalar(
                select(BriefingOccurrenceRow).where(
                    BriefingOccurrenceRow.id == occurrence_id,
                    BriefingOccurrenceRow.schedule_id == schedule_id,
                    BriefingOccurrenceRow.principal_issuer == issuer,
                    BriefingOccurrenceRow.principal_subject == subject,
                    BriefingOccurrenceRow.deleted_at.is_(None),
                )
            )
        if row is None:
            raise BriefingNotFound("briefing occurrence not found")
        return _occurrence_from_row(row)

    async def get_occurrence_by_id(self, occurrence_id: UUID) -> BriefingOccurrence:
        async with self.sessions() as session:
            row = await session.scalar(
                select(BriefingOccurrenceRow).where(
                    BriefingOccurrenceRow.id == occurrence_id,
                    BriefingOccurrenceRow.deleted_at.is_(None),
                )
            )
        if row is None:
            raise BriefingNotFound("briefing occurrence not found")
        return _occurrence_from_row(row)

    async def delete_history(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_schedule_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt:
        current = _now()
        payload = {
            "scheduleId": str(schedule_id),
            "expectedScheduleVersion": expected_schedule_version,
            "scope": BriefingDeletionScope.OCCURRENCE_HISTORY.value,
        }
        fingerprint = command_fingerprint("history.delete", payload)
        async with self.sessions() as session:
            async with session.begin():
                replayed = await _replayed_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="history.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replayed is not None:
                    return _receipt_from_payload(replayed)
                schedule = await session.scalar(
                    select(BriefingScheduleRow)
                    .where(
                        BriefingScheduleRow.id == schedule_id,
                        BriefingScheduleRow.principal_issuer == issuer,
                        BriefingScheduleRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
                if schedule is None:
                    raise BriefingNotFound("briefing schedule not found")
                if schedule.version != expected_schedule_version:
                    raise BriefingVersionConflict("briefing schedule version is stale")
                rows = (
                    await session.scalars(
                        select(BriefingOccurrenceRow).where(
                            BriefingOccurrenceRow.schedule_id == schedule_id,
                            BriefingOccurrenceRow.principal_issuer == issuer,
                            BriefingOccurrenceRow.principal_subject == subject,
                        )
                    )
                ).all()
                for row in rows:
                    if row.status in {
                        BriefingOccurrenceStatus.COMPLETED.value,
                        BriefingOccurrenceStatus.PARTIAL.value,
                        BriefingOccurrenceStatus.FAILED.value,
                        BriefingOccurrenceStatus.CANCELED.value,
                        BriefingOccurrenceStatus.SKIPPED.value,
                        BriefingOccurrenceStatus.UNKNOWN.value,
                    }:
                        row.deleted_at = current
                receipt = BriefingDeletionReceipt(
                    uuid4(),
                    BriefingDeletionScope.OCCURRENCE_HISTORY,
                    schedule_id,
                    None,
                    None,
                    current,
                )
                _store_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="history.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response=receipt.as_payload(),
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_deletion_event(receipt),
                    schedule_id,
                )
        return receipt

    async def get_result(self, issuer: str, subject: str, result_id: UUID) -> BriefingResult:
        async with self.sessions() as session:
            row = await session.scalar(
                select(BriefingResultRow).where(
                    BriefingResultRow.id == result_id,
                    BriefingResultRow.principal_issuer == issuer,
                    BriefingResultRow.principal_subject == subject,
                )
            )
        if row is None:
            raise BriefingNotFound("briefing result not found")
        return _result_from_row(row)

    async def delete_result_content(
        self,
        issuer: str,
        subject: str,
        result_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt:
        current = _now()
        payload = {
            "resultId": str(result_id),
            "expectedVersion": expected_version,
            "scope": BriefingDeletionScope.RESULT_CONTENT.value,
        }
        fingerprint = command_fingerprint("result.delete", payload)
        async with self.sessions() as session:
            async with session.begin():
                replayed = await _replayed_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="result.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replayed is not None:
                    return _receipt_from_payload(replayed)
                row = await session.scalar(
                    select(BriefingResultRow)
                    .where(
                        BriefingResultRow.id == result_id,
                        BriefingResultRow.principal_issuer == issuer,
                        BriefingResultRow.principal_subject == subject,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise BriefingNotFound("briefing result not found")
                if row.version != expected_version:
                    raise BriefingVersionConflict("briefing result version is stale")
                schedule_id, occurrence_id_value = row.schedule_id, row.occurrence_id
                row.content = None
                row.original_status = (
                    row.status
                    if row.status != BriefingResultStatus.DELETED.value
                    else row.original_status
                )
                row.status, row.version, row.deleted_at, row.updated_at = (
                    BriefingResultStatus.DELETED.value,
                    row.version + 1,
                    current,
                    current,
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_result_event(_result_from_row(row)),
                    row.schedule_id,
                )
                receipt = BriefingDeletionReceipt(
                    uuid4(),
                    BriefingDeletionScope.RESULT_CONTENT,
                    schedule_id,
                    occurrence_id_value,
                    result_id,
                    current,
                )
                _store_command(
                    session,
                    issuer=issuer,
                    subject=subject,
                    operation="result.delete",
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response=receipt.as_payload(),
                )
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_deletion_event(receipt),
                    result_id,
                )
        return receipt

    async def admit_occurrence(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        occurrence_id: UUID,
        *,
        run_id: UUID,
        expected_version: int | None = None,
    ) -> BriefingOccurrence:
        """Bind a scheduled occurrence to a run in one guarded transaction."""
        current = _now()
        async with self.sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(BriefingOccurrenceRow)
                    .join(
                        BriefingScheduleRow,
                        BriefingScheduleRow.id == BriefingOccurrenceRow.schedule_id,
                    )
                    .where(
                        BriefingOccurrenceRow.id == occurrence_id,
                        BriefingOccurrenceRow.schedule_id == schedule_id,
                        BriefingOccurrenceRow.principal_issuer == issuer,
                        BriefingOccurrenceRow.principal_subject == subject,
                        BriefingScheduleRow.status == BriefingScheduleStatus.ACTIVE.value,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise BriefingNotFound("briefing occurrence not found")
                if expected_version is not None and row.version != expected_version:
                    raise BriefingVersionConflict("briefing occurrence version is stale")
                if row.run_id is not None:
                    if row.run_id != run_id:
                        raise BriefingTransitionError("briefing occurrence already has a run")
                    return _occurrence_from_row(row)
                if row.status not in {
                    BriefingOccurrenceStatus.SCHEDULED.value,
                    BriefingOccurrenceStatus.ADMITTED.value,
                }:
                    raise BriefingTransitionError("briefing occurrence is not schedulable")
                row.status = BriefingOccurrenceStatus.ADMITTED.value
                row.run_id = run_id
                row.version += 1
                row.started_at = current
                row.updated_at = current
                session.add(
                    BriefingRunLineageRow(
                        occurrence_id=occurrence_id,
                        schedule_id=schedule_id,
                        run_id=run_id,
                        principal_issuer=issuer,
                        principal_subject=subject,
                    )
                )
                admitted = _occurrence_from_row(row)
                await _stage_lifecycle_event(
                    self.lifecycle_events,
                    session,
                    as_occurrence_event(admitted),
                    row.schedule_id,
                )
                return admitted

    async def create_result(self, result: BriefingResult) -> BriefingResult:
        """Persist a terminal result and settle its occurrence atomically."""
        started = monotonic()
        outcome = "error"
        try:
            async with self.sessions() as session:
                async with session.begin():
                    occurrence = await session.scalar(
                        select(BriefingOccurrenceRow)
                        .where(
                            BriefingOccurrenceRow.id == result.occurrence_id,
                            BriefingOccurrenceRow.schedule_id == result.schedule_id,
                            BriefingOccurrenceRow.principal_issuer == result.issuer,
                            BriefingOccurrenceRow.principal_subject == result.subject,
                        )
                        .with_for_update()
                    )
                    if occurrence is None:
                        raise BriefingNotFound("briefing occurrence not found")
                    existing = await session.scalar(
                        select(BriefingResultRow)
                        .where(
                            BriefingResultRow.occurrence_id == result.occurrence_id,
                            BriefingResultRow.principal_issuer == result.issuer,
                            BriefingResultRow.principal_subject == result.subject,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        outcome = "duplicate"
                        return _result_from_row(existing)
                    result_row = BriefingResultRow(
                            id=result.id,
                            schedule_id=result.schedule_id,
                            occurrence_id=result.occurrence_id,
                            principal_issuer=result.issuer,
                            principal_subject=result.subject,
                            run_id=result.run_id,
                            status=result.status.value,
                            original_status=(
                                result.original_status.value
                                if result.original_status
                                else None
                            ),
                            version=result.version,
                            source_completeness=result.source_completeness.value,
                            content=result.content,
                            created_at=result.created_at,
                            updated_at=result.updated_at,
                            deleted_at=result.deleted_at,
                        )
                    session.add(result_row)
                    occurrence.result_id = result.id
                    occurrence.status = result.status.value
                    occurrence.source_completeness = result.source_completeness.value
                    occurrence.finished_at = result.updated_at
                    occurrence.version += 1
                    occurrence.updated_at = result.updated_at
                    if result.run_id is not None:
                        lineage = await session.scalar(
                            select(BriefingRunLineageRow).where(
                                BriefingRunLineageRow.occurrence_id == result.occurrence_id
                            )
                        )
                        if lineage is None:
                            session.add(
                                BriefingRunLineageRow(
                                    occurrence_id=result.occurrence_id,
                                    schedule_id=result.schedule_id,
                                    run_id=result.run_id,
                                    principal_issuer=result.issuer,
                                    principal_subject=result.subject,
                                )
                            )
                    settled_occurrence = _occurrence_from_row(occurrence)
                    await _stage_lifecycle_event(
                        self.lifecycle_events,
                        session,
                        as_occurrence_event(settled_occurrence),
                        occurrence.schedule_id,
                    )
                    await _stage_lifecycle_event(
                        self.lifecycle_events,
                        session,
                        as_result_event(_result_from_row(result_row)),
                        result.schedule_id,
                    )
                    outcome = "ok"
            return result
        except Exception as error:
            _record_span(
                self.metrics,
                component="aura.automation.briefing_persistence",
                operation="briefing.result.persist",
                started=started,
                aggregate_id=result.id,
                outcome="error",
                error_class=_error_class(error),
                run_id=result.run_id,
            )
            raise
        finally:
            if self.metrics is not None and outcome != "error":
                _record_span(
                    self.metrics,
                    component="aura.automation.briefing_persistence",
                    operation="briefing.result.persist",
                    started=started,
                    aggregate_id=result.id,
                    outcome=outcome,
                    run_id=result.run_id,
                )

    async def update_result(self, result: BriefingResult) -> BriefingResult:
        return await self.create_result(result)

    async def recover_stale_claims(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> tuple[BriefingOccurrence, ...]:
        current = _normalise_now(now)
        cutoff = current - timedelta(minutes=15)
        recovered: list[BriefingOccurrence] = []
        async with self.sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(BriefingOccurrenceRow)
                        .where(
                            BriefingOccurrenceRow.status.in_(
                                (
                                    BriefingOccurrenceStatus.ADMITTED.value,
                                    BriefingOccurrenceStatus.RUNNING.value,
                                )
                            ),
                            BriefingOccurrenceRow.updated_at <= cutoff,
                            BriefingOccurrenceRow.deleted_at.is_(None),
                        )
                        .order_by(BriefingOccurrenceRow.updated_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = BriefingOccurrenceStatus.UNKNOWN.value
                    row.version += 1
                    row.finished_at = current
                    row.updated_at = current
                    existing_result = await session.scalar(
                        select(BriefingResultRow)
                        .where(
                            BriefingResultRow.occurrence_id == row.id,
                            BriefingResultRow.principal_issuer == row.principal_issuer,
                            BriefingResultRow.principal_subject == row.principal_subject,
                        )
                        .with_for_update()
                    )
                    if existing_result is None:
                        recovered_result_id = uuid5(
                            BRIEFING_ID_NAMESPACE,
                            f"recovery-result:{row.id}",
                        )
                        existing_result = BriefingResultRow(
                            id=recovered_result_id,
                            schedule_id=row.schedule_id,
                            occurrence_id=row.id,
                            principal_issuer=row.principal_issuer,
                            principal_subject=row.principal_subject,
                            run_id=row.run_id,
                            status=BriefingResultStatus.UNKNOWN.value,
                            original_status=None,
                            version=1,
                            source_completeness=row.source_completeness,
                            content=None,
                            created_at=row.created_at,
                            updated_at=current,
                            deleted_at=None,
                        )
                        session.add(existing_result)
                    row.result_id = existing_result.id
                    recovered_item = _occurrence_from_row(row)
                    recovered.append(recovered_item)
                    await _stage_lifecycle_event(
                        self.lifecycle_events,
                        session,
                        as_occurrence_event(recovered_item),
                        row.schedule_id,
                    )
                    await _stage_lifecycle_event(
                        self.lifecycle_events,
                        session,
                        as_result_event(_result_from_row(existing_result)),
                        row.schedule_id,
                    )
        return tuple(recovered)

    async def claim_due_occurrences(
        self,
        now: datetime,
        *,
        limit: int = 20,
        stage: Callable[[object, BriefingOccurrence], object | Awaitable[object]] | None = None,
    ) -> tuple[BriefingOccurrence, ...]:
        """Atomically claim a bounded active due set for one poller."""

        if not 1 <= limit <= 100:
            raise BriefingValidationError("claim limit must be between 1 and 100")
        claimed: list[BriefingOccurrence] = []
        async with self.sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(BriefingOccurrenceRow)
                        .join(
                            BriefingScheduleRow,
                            BriefingScheduleRow.id == BriefingOccurrenceRow.schedule_id,
                        )
                        .where(
                            BriefingScheduleRow.status == BriefingScheduleStatus.ACTIVE.value,
                            BriefingOccurrenceRow.status
                            == BriefingOccurrenceStatus.SCHEDULED.value,
                            BriefingOccurrenceRow.due_at <= now,
                            BriefingOccurrenceRow.deleted_at.is_(None),
                        )
                        .order_by(BriefingOccurrenceRow.due_at, BriefingOccurrenceRow.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = BriefingOccurrenceStatus.ADMITTED.value
                    row.version += 1
                    row.updated_at = now
                    schedule = await session.scalar(
                        select(BriefingScheduleRow)
                        .where(BriefingScheduleRow.id == row.schedule_id)
                        .with_for_update()
                    )
                    if schedule is not None:
                        cadence = BriefingDailyCadence(
                            time.fromisoformat(str(schedule.local_time)), schedule.time_zone
                        )
                        next_date = row.due_local_date + timedelta(days=1)
                        while True:
                            next_due_at = due_instant(
                                next_date,
                                cadence.local_time,
                                cadence.time_zone,
                            )
                            if next_due_at > now:
                                schedule.next_due_at = next_due_at
                                schedule.updated_at = now
                                existing_next = await session.scalar(
                                    select(BriefingOccurrenceRow).where(
                                        BriefingOccurrenceRow.schedule_id == schedule.id,
                                        BriefingOccurrenceRow.due_local_date == next_date,
                                    )
                                )
                                if existing_next is None:
                                    max_sequence = await session.scalar(
                                        select(func.max(BriefingOccurrenceRow.occurrence_sequence)).where(
                                            BriefingOccurrenceRow.schedule_id == schedule.id
                                        )
                                    )
                                    existing_next = BriefingOccurrenceRow(
                                        id=occurrence_id(schedule.id, next_date),
                                        schedule_id=schedule.id,
                                        principal_issuer=schedule.principal_issuer,
                                        principal_subject=schedule.principal_subject,
                                        occurrence_sequence=int(max_sequence or 0) + 1,
                                        status=BriefingOccurrenceStatus.SCHEDULED.value,
                                        due_at=next_due_at,
                                        due_local_date=next_date,
                                        due_local_time=cadence.local_time_text,
                                        time_zone=cadence.time_zone,
                                        source_completeness=BriefingSourceCompleteness.UNKNOWN.value,
                                        version=1,
                                        created_at=now,
                                        updated_at=now,
                                    )
                                    session.add(existing_next)
                                    await _stage_lifecycle_event(
                                        self.lifecycle_events,
                                        session,
                                        as_occurrence_event(_occurrence_from_row(existing_next)),
                                        schedule.id,
                                    )
                                break
                            skipped = await session.scalar(
                                select(BriefingOccurrenceRow).where(
                                    BriefingOccurrenceRow.schedule_id == schedule.id,
                                    BriefingOccurrenceRow.due_local_date == next_date,
                                )
                            )
                            if skipped is None:
                                max_sequence = await session.scalar(
                                    select(func.max(BriefingOccurrenceRow.occurrence_sequence)).where(
                                        BriefingOccurrenceRow.schedule_id == schedule.id
                                    )
                                )
                                skipped = BriefingOccurrenceRow(
                                    id=occurrence_id(schedule.id, next_date),
                                    schedule_id=schedule.id,
                                    principal_issuer=schedule.principal_issuer,
                                    principal_subject=schedule.principal_subject,
                                    occurrence_sequence=int(max_sequence or 0) + 1,
                                    status=BriefingOccurrenceStatus.SKIPPED.value,
                                    due_at=next_due_at,
                                    due_local_date=next_date,
                                    due_local_time=cadence.local_time_text,
                                    time_zone=cadence.time_zone,
                                    source_completeness=BriefingSourceCompleteness.UNKNOWN.value,
                                    version=1,
                                    created_at=now,
                                    updated_at=now,
                                    finished_at=now,
                                )
                                session.add(skipped)
                                await _stage_lifecycle_event(
                                    self.lifecycle_events,
                                    session,
                                    as_occurrence_event(_occurrence_from_row(skipped)),
                                    schedule.id,
                                )
                            next_date += timedelta(days=1)
                    claimed_item = _occurrence_from_row(row)
                    if stage is not None:
                        staged = stage(session, claimed_item)
                        if inspect.isawaitable(staged):
                            await staged
                    claimed.append(claimed_item)
        return tuple(claimed)

    async def claim_due_occurrences_and_stage(
        self,
        now: datetime,
        *,
        limit: int = 20,
        stage: Callable[[object, BriefingOccurrence], object | Awaitable[object]],
    ) -> tuple[BriefingOccurrence, ...]:
        return await self.claim_due_occurrences(now, limit=limit, stage=stage)


def _occurrence_from_row(row: BriefingOccurrenceRow) -> BriefingOccurrence:
    return BriefingOccurrence(
        row.id,
        row.schedule_id,
        row.principal_issuer,
        row.principal_subject,
        row.occurrence_sequence,
        BriefingOccurrenceStatus(row.status),
        row.due_at,
        row.due_local_date,
        time.fromisoformat(str(row.due_local_time)),
        row.time_zone,
        BriefingSourceCompleteness(row.source_completeness),
        row.run_id,
        row.result_id,
        row.created_at,
        row.updated_at,
        row.started_at,
        row.finished_at,
        row.version,
    )


def _result_from_row(row: BriefingResultRow) -> BriefingResult:
    return BriefingResult(
        row.id,
        row.schedule_id,
        row.occurrence_id,
        row.principal_issuer,
        row.principal_subject,
        row.run_id,
        BriefingResultStatus(row.status),
        row.version,
        BriefingSourceCompleteness(row.source_completeness),
        row.content,
        row.created_at,
        row.updated_at,
        row.deleted_at,
        BriefingResultStatus(row.original_status) if row.original_status else None,
    )


__all__ = ["MemoryBriefingRepository", "SqlBriefingRepository"]
