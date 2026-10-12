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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast, overload
from uuid import UUID
from zoneinfo import ZoneInfo

from .recurrence import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurrenceValidationError,
    next_occurrence,
    validate_timezone,
)

REMINDER_COMPONENT = "aura.automation.reminder_management"
REMINDER_TOPIC = "aura.automation.reminders.v1"
REMINDER_CURSOR_VERSION = 1
_CURSOR_SECRET = b"aura-reminder-cursor-v1"


class ReminderError(Exception):
    """Base class for deterministic reminder failures."""


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    FIRED = "fired"
    COMPLETED = "completed"
    CANCELED = "canceled"


class ReminderSeriesStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"


class ReminderOccurrenceStatus(StrEnum):
    SCHEDULED = "scheduled"
    FIRED = "fired"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    REPLACED = "replaced"
    PAUSED_WINDOW = "paused-window"
    SERIES_CANCELED = "series-canceled"


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
    # Internal linkage for recurring reminders.  These fields are intentionally
    # omitted from the established one-shot payload and event contract.
    series_id: UUID | None = None
    occurrence_sequence: int | None = None

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
class ReminderOccurrence:
    id: UUID
    series_id: UUID
    reminder_id: UUID
    sequence: int
    status: ReminderOccurrenceStatus
    local_due_at: datetime
    due_at: datetime
    created_at: datetime
    updated_at: datetime
    skipped_at: datetime | None = None
    fired_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReminderSeries:
    id: UUID
    issuer: str
    subject: str
    content: str
    rule: RecurrenceRule
    timezone: str
    local_start: datetime
    next_due_at: datetime | None
    next_reminder_id: UUID | None
    status: ReminderSeriesStatus
    version: int
    created_at: datetime
    updated_at: datetime
    missed_count: int = 0
    occurrence_count: int = 0
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    canceled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReminderSeriesPage:
    items: tuple[ReminderSeries, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ReminderOccurrencePage:
    items: tuple[ReminderOccurrence, ...]
    next_cursor: str | None = None

    # Keep the pre-pagination tuple-like read API for existing scheduler and
    # domain callers while exposing the cursor to transport callers.
    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> ReminderOccurrence: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ReminderOccurrence, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ReminderOccurrence | tuple[ReminderOccurrence, ...]:
        return self.items[index]


@dataclass(frozen=True, slots=True)
class ReminderDeliveryResult:
    reminders: tuple[Reminder, ...]
    generated_count: int = 0
    skipped_count: int = 0
    missed_count: int = 0


class ReminderSeriesError(ReminderError):
    pass


class ReminderSeriesNotFound(ReminderSeriesError):
    pass


class ReminderSeriesVersionConflict(ReminderSeriesError):
    pass


class ReminderSeriesIdempotencyConflict(ReminderSeriesError):
    pass


class ReminderSeriesTransitionError(ReminderSeriesError):
    pass


class ReminderSeriesRepository(Protocol):
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
    ) -> ReminderSeries: ...

    async def get_series(self, issuer: str, subject: str, series_id: UUID) -> ReminderSeries: ...

    async def list_series(
        self,
        issuer: str,
        subject: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReminderSeriesPage: ...

    async def list_occurrences(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReminderOccurrencePage: ...

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
    ) -> ReminderSeries: ...

    async def pause_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries: ...
    async def resume_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries: ...
    async def skip_next(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries: ...
    async def cancel_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries: ...


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


@dataclass(frozen=True, slots=True)
class ReminderCommandProposal:
    """Immutable, owner-bound arguments for a conversational command.

    ``normalized_arguments`` is the only source used by the approval resume
    path.  It is intentionally kept in the reminder domain so the governance
    domain carries only a proposal identifier and lifecycle metadata.
    """

    id: UUID
    issuer: str
    subject: str
    operation: str
    tool_id: str
    tool_version: int
    target_id: UUID | None
    expected_version: int | None
    normalized_arguments: Mapping[str, object]
    timezone: str | None
    run_id: UUID
    conversation_id: UUID
    tool_call_id: UUID
    created_at: datetime
    expires_at: datetime
    target_type: str | None = None

    @property
    def arguments(self) -> Mapping[str, object]:
        """Compatibility spelling for application callers."""
        return self.normalized_arguments


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

    async def create_command(
        self,
        issuer: str,
        subject: str,
        *,
        operation: str,
        tool_id: str,
        tool_version: int,
        target_id: UUID | None,
        expected_version: int | None,
        target_type: str | None = None,
        normalized_arguments: Mapping[str, object],
        timezone: str | None,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        now: datetime | None = None,
        transaction: object | None = None,
    ) -> ReminderCommandProposal: ...

    async def get_command(
        self, issuer: str, subject: str, proposal_id: UUID
    ) -> ReminderCommandProposal: ...


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

    async def fire_due_result(
        self,
        *,
        transaction: object | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> ReminderDeliveryResult: ...


class ReminderRepository(ReminderDeliveryPort, ReminderSeriesRepository, Protocol):
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

    async def reschedule(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        due_at: datetime,
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
        return await self.repository.list(issuer, subject, view=view, limit=limit, cursor=cursor)

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

    async def reschedule(
        self,
        issuer: str,
        subject: str,
        reminder_id: UUID,
        *,
        due_at: datetime,
        expected_version: int,
        idempotency_key: str,
    ) -> Reminder:
        return await self.repository.reschedule(
            issuer,
            subject,
            reminder_id,
            due_at=canonical_due_at(due_at),
            expected_version=expected_version,
            idempotency_key=idempotency_key,
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
        validate_timezone(timezone)
        if local_start.tzinfo is not None:
            raise ReminderValidationError("local_start must not contain an offset")
        return await self.repository.create_series(
            issuer,
            subject,
            content=normalize_content(content),
            local_start=local_start,
            timezone=timezone,
            rule=rule,
            idempotency_key=idempotency_key,
        )

    async def get_series(self, issuer: str, subject: str, series_id: UUID) -> ReminderSeries:
        return await self.repository.get_series(issuer, subject, series_id)

    async def list_series(
        self,
        issuer: str,
        subject: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReminderSeriesPage:
        return await self.repository.list_series(issuer, subject, limit=limit, cursor=cursor)

    async def list_occurrences(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReminderOccurrencePage:
        return await self.repository.list_occurrences(
            issuer, subject, series_id, limit=limit, cursor=cursor
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
        if content is not None:
            content = normalize_content(content)
        if timezone is not None:
            validate_timezone(timezone)
        return await self.repository.update_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            content=content,
            local_start=local_start,
            timezone=timezone,
            rule=rule,
        )

    async def pause_series(
        self,
        issuer: str,
        subject: str,
        series_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderSeries:
        return await self.repository.pause_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
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
        return await self.repository.resume_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
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
        return await self.repository.skip_next(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
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
        return await self.repository.cancel_series(
            issuer,
            subject,
            series_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def resolve_target(
        self,
        issuer: str,
        subject: str,
        target: str,
        *,
        kind: str | None = None,
        limit: int = 10,
    ) -> ReminderTargetResolution:
        """Resolve an owner target deterministically without mutation.

        UUIDs are exact identifiers.  Other targets require an exact,
        case-folded content/subject match; ties are deliberately ambiguous.
        The bounded result prevents a query from becoming an unbounded data
        disclosure channel.
        """
        if kind is not None and kind not in {"reminder", "series"}:
            raise ReminderValidationError("target kind is invalid")
        value = target.strip()
        if not value:
            return ReminderTargetResolution("clarification", ())
        candidates: list[ReminderTarget] = []
        try:
            identifier = UUID(value)
        except ValueError:
            identifier = None
        if identifier is not None:
            try:
                candidates.append(ReminderTarget("reminder", identifier, None, None))
                item = await self.get(issuer, subject, identifier)
                candidates = [
                    ReminderTarget(
                        "reminder", item.id, item.version, item.content, item.status.value
                    )
                ]
            except ReminderError:
                try:
                    item = await self.get_series(issuer, subject, identifier)
                    candidates = [
                        ReminderTarget(
                            "series", item.id, item.version, item.content, item.status.value
                        )
                    ]
                except ReminderError:
                    candidates = []
        else:
            needle = value.casefold()
            seen: set[UUID] = set()
            if kind != "series":
                for view in (ReminderView.UPCOMING, ReminderView.HISTORY):
                    cursor: str | None = None
                    while True:
                        page = await self.list(issuer, subject, view=view, limit=100, cursor=cursor)
                        for item in page.items:
                            if item.id not in seen and item.content.casefold() == needle:
                                candidates.append(
                                    ReminderTarget(
                                        "reminder",
                                        item.id,
                                        item.version,
                                        item.content,
                                        item.status.value,
                                    )
                                )
                                seen.add(item.id)
                        if len(candidates) > 1 or page.next_cursor is None:
                            break
                        cursor = page.next_cursor
                    if len(candidates) > 1:
                        break
            if len(candidates) <= 1 and kind != "reminder":
                cursor = None
                while True:
                    series_page = await self.list_series(issuer, subject, limit=100, cursor=cursor)
                    for item in series_page.items:
                        if item.content.casefold() == needle:
                            candidates.append(
                                ReminderTarget(
                                    "series",
                                    item.id,
                                    item.version,
                                    item.content,
                                    item.status.value,
                                )
                            )
                    if len(candidates) > 1 or series_page.next_cursor is None:
                        break
                    cursor = series_page.next_cursor
        if kind is not None:
            candidates = [item for item in candidates if item.kind == kind]
        if len(candidates) == 1:
            return ReminderTargetResolution("resolved", tuple(candidates))
        return ReminderTargetResolution("clarification", tuple(candidates[:limit]))

    async def list_for_owner(
        self,
        issuer: str,
        subject: str,
        *,
        query: str | None = None,
        state: str | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        """Return bounded, owner-authorized summaries for conversational reads."""
        bounded = max(1, min(limit, 10))
        needle = query.casefold() if query else None
        items: list[dict[str, object]] = []
        seen: set[str] = set()

        # Apply the conversational filters while walking each owner-scoped
        # source.  The repository pages are bounded for transport, but the
        # first page is not the complete set of records: a matching historical
        # item may be beyond it.  Exhaust pages until the requested result
        # bound is satisfied (or the source is exhausted), then cap the
        # private result below.
        series_cursor: str | None = None
        while len(items) < bounded:
            series_page = await self.list_series(issuer, subject, limit=100, cursor=series_cursor)
            for item in series_page.items:
                identifier = str(item.id)
                if identifier in seen:
                    continue
                if state and item.status.value != state:
                    continue
                if needle and needle not in item.content.casefold():
                    continue
                items.append(
                    {
                        "id": identifier,
                        "type": "series",
                        "content": item.content,
                        "status": item.status.value,
                        "version": item.version,
                    }
                )
                seen.add(identifier)
                if len(items) >= bounded:
                    break
            if len(items) >= bounded or series_page.next_cursor is None:
                break
            series_cursor = series_page.next_cursor

        for view in (ReminderView.UPCOMING, ReminderView.HISTORY):
            if len(items) >= bounded:
                break
            cursor: str | None = None
            while len(items) < bounded:
                page = await self.list(issuer, subject, view=view, limit=100, cursor=cursor)
                for item in page.items:
                    identifier = str(item.id)
                    if identifier in seen:
                        continue
                    if state and item.status.value != state:
                        continue
                    if needle and needle not in item.content.casefold():
                        continue
                    items.append(
                        {
                            "id": identifier,
                            "type": "reminder",
                            "content": item.content,
                            "status": item.status.value,
                            "version": item.version,
                        }
                    )
                    seen.add(identifier)
                    if len(items) >= bounded:
                        break
                if len(items) >= bounded or page.next_cursor is None:
                    break
                cursor = page.next_cursor
        return {"outcome": "resolved", "items": items[:bounded]}

    async def inspect_for_owner(
        self,
        issuer: str,
        subject: str,
        target: str,
        *,
        resolver: Callable[..., object] | None = None,
    ) -> dict[str, object]:
        """Inspect one uniquely resolved owner record without guessing."""
        resolve = resolver or self.resolve_target
        resolution = await cast(
            "Awaitable[ReminderTargetResolution]", resolve(issuer, subject, target)
        )
        if resolution.status != "resolved" or len(resolution.matches) != 1:
            return {"outcome": "clarification", "items": []}
        match = resolution.matches[0]
        return {
            "outcome": "resolved",
            "items": [
                {
                    "id": str(match.id),
                    "type": match.kind,
                    "content": match.content,
                    "status": match.status,
                    "version": match.version,
                }
            ],
        }

    async def prepare_command(
        self,
        issuer: str,
        subject: str,
        *,
        tool_id: str,
        arguments: Mapping[str, object],
        timezone: str | None,
    ) -> ReminderCommandPreparation:
        """Resolve and canonicalize a conversational command before proposal creation."""
        if tool_id in {"reminder.series.create", "reminder.reschedule"}:
            if not timezone:
                return ReminderCommandPreparation("clarification", {}, None, None, None, None)
            try:
                validate_timezone(timezone)
            except RecurrenceValidationError, ValueError:
                return ReminderCommandPreparation("validation", {}, None, None, None, None)
        normalized = dict(arguments)

        def local_wall(name: str) -> str:
            value = datetime.fromisoformat(str(normalized[name]).replace("Z", "+00:00"))
            if value.tzinfo is not None and value.utcoffset() is not None:
                raise ReminderValidationError(f"{name} must be a local wall-clock value")
            return value.isoformat()

        try:
            if tool_id == "reminder.series.create":
                normalized["localStart"] = local_wall("localStart")
                rule = RecurrenceRule.from_payload(
                    cast(dict[str, object], normalized["recurrence"])
                )
                normalized["recurrence"] = rule.to_payload()
                return ReminderCommandPreparation(
                    "resolved", normalized, None, None, "series", timezone
                )
            if tool_id == "reminder.series.update":
                if "localStart" in normalized:
                    normalized["localStart"] = local_wall("localStart")
                if "recurrence" in normalized:
                    rule = RecurrenceRule.from_payload(
                        cast(dict[str, object], normalized["recurrence"])
                    )
                    normalized["recurrence"] = rule.to_payload()
                kind = "series"
            elif tool_id == "reminder.cancel":
                kind = "series" if normalized.get("scope") == "series" else "reminder"
            else:
                kind = "reminder" if tool_id == "reminder.reschedule" else "series"
                if tool_id == "reminder.reschedule":
                    normalized["dueAt"] = local_wall("dueAt")
            resolution = await self.resolve_target(
                issuer, subject, str(normalized.get("target", "")), kind=kind
            )
            if resolution.status != "resolved" or len(resolution.matches) != 1:
                return ReminderCommandPreparation("clarification", {}, None, None, None, timezone)
            match = resolution.matches[0]
            if match.version is None:
                return ReminderCommandPreparation("clarification", {}, None, None, None, timezone)
            normalized["target"] = str(match.id)
            normalized["expectedVersion"] = match.version
            return ReminderCommandPreparation(
                "resolved", normalized, match.id, match.version, match.kind, timezone
            )
        except (
            RecurrenceValidationError,
            ReminderValidationError,
            TypeError,
            ValueError,
            KeyError,
        ):
            return ReminderCommandPreparation("validation", {}, None, None, None, timezone)

    async def execute_command(
        self,
        issuer: str,
        subject: str,
        proposal: ReminderCommandProposal,
        *,
        idempotency_key: str,
    ) -> object:
        """Execute one already-approved typed command through public APIs."""
        args = dict(proposal.normalized_arguments)
        operation = proposal.operation
        target = proposal.target_id
        version = proposal.expected_version

        def local_start(value: object) -> datetime:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None)

        def due_in_zone(value: object) -> datetime:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                if not proposal.timezone:
                    raise ReminderValidationError("approved reminder command timezone is missing")
                parsed = parsed.replace(tzinfo=ZoneInfo(proposal.timezone))
            return parsed.astimezone(UTC)

        if operation == "series.create":
            return await self.create_series(
                issuer,
                subject,
                content=str(args["content"]),
                local_start=local_start(args["localStart"]),
                timezone=str(proposal.timezone or args["timezone"]),
                rule=RecurrenceRule.from_payload(cast(dict[str, object], args["recurrence"])),
                idempotency_key=idempotency_key,
            )
        if target is None or version is None:
            raise ReminderValidationError("approved reminder command target is incomplete")
        if operation == "series.update":
            return await self.update_series(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
                content=str(args["content"]) if args.get("content") is not None else None,
                local_start=local_start(args["localStart"]) if args.get("localStart") else None,
                timezone=(str(args["timezone"]) if args.get("timezone") else None),
                rule=(
                    RecurrenceRule.from_payload(cast(dict[str, object], args["recurrence"]))
                    if args.get("recurrence")
                    else None
                ),
            )
        if operation == "series.pause":
            return await self.pause_series(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
            )
        if operation == "series.resume":
            return await self.resume_series(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
            )
        if operation == "cancel" and args.get("scope") == "series":
            return await self.cancel_series(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
            )
        if operation == "cancel" and args.get("scope") == "occurrence":
            return await self.cancel(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
            )
        if operation == "reschedule":
            if proposal.target_type == "occurrence":
                return await self.reschedule(
                    issuer,
                    subject,
                    target,
                    due_at=due_in_zone(args["dueAt"]),
                    expected_version=version,
                    idempotency_key=idempotency_key,
                )
            return await self.update_series(
                issuer,
                subject,
                target,
                expected_version=version,
                idempotency_key=idempotency_key,
                local_start=local_start(args.get("localStart", args.get("dueAt"))),
            )
        raise ReminderValidationError("unsupported approved reminder command")


@dataclass(frozen=True, slots=True)
class ReminderTarget:
    kind: str
    id: UUID
    version: int | None
    content: str | None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ReminderTargetResolution:
    status: str
    matches: tuple[ReminderTarget, ...]


@dataclass(frozen=True, slots=True)
class ReminderCommandPreparation:
    outcome: str
    arguments: Mapping[str, object]
    target_id: UUID | None
    expected_version: int | None
    target_type: str | None
    timezone: str | None


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
        "completedAt": item.completed_at.astimezone(UTC).isoformat() if item.completed_at else None,
        "canceledAt": item.canceled_at.astimezone(UTC).isoformat() if item.canceled_at else None,
    }


def from_payload(payload: dict[str, object], *, issuer: str = "", subject: str = "") -> Reminder:
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
    "ReminderCommandProposal",
    "ReminderCommandPreparation",
    "ReminderApplicationService",
    "ReminderOccurrence",
    "ReminderOccurrencePage",
    "ReminderOccurrenceStatus",
    "ReminderSeries",
    "ReminderSeriesError",
    "ReminderSeriesIdempotencyConflict",
    "ReminderSeriesNotFound",
    "ReminderSeriesPage",
    "ReminderSeriesRepository",
    "ReminderSeriesStatus",
    "ReminderSeriesTransitionError",
    "ReminderSeriesVersionConflict",
    "ReminderError",
    "ReminderIdempotencyConflict",
    "ReminderNotFound",
    "ReminderPage",
    "ReminderTarget",
    "ReminderTargetResolution",
    "ReminderRepository",
    "ReminderDeliveryPort",
    "ReminderDeliveryResult",
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
    "RecurrenceFrequency",
    "RecurrenceRule",
    "RecurrenceValidationError",
    "next_occurrence",
    "validate_timezone",
]
