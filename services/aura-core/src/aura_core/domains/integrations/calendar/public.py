"""Public provider-neutral calendar values, validation, and deterministic rules.

This module is the only calendar surface that API, provider composition, or
other domains should import.  It contains no SQLAlchemy or provider types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_CALENDARS_PER_SELECTION = 50
MAX_DISCOVERY_PAGE = 100
MAX_EVENT_PAGE = 100
MAX_CONFLICT_PAGE = 200
MAX_QUERY_DAYS = 31
OAUTH_ATTEMPT_TTL_SECONDS = 600
MAX_REVOCATION_ATTEMPTS = 6


@dataclass(frozen=True, slots=True)
class CalendarRequestContext:
    """Public application seam for monotonic deadline and cancellation."""

    deadline_monotonic: float
    cancelled: Callable[[], bool] | None = None

    def check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise asyncio.CancelledError
        if monotonic() >= self.deadline_monotonic:
            raise TimeoutError("calendar request deadline exceeded")

    def remaining_seconds(self) -> float:
        self.check()
        return max(0.0, self.deadline_monotonic - monotonic())


class CalendarError(RuntimeError):
    """Base class for content-free calendar failures."""


class CalendarValidationError(CalendarError, ValueError):
    pass


class CalendarVersionConflict(CalendarError):
    pass


class CalendarNotConnected(CalendarError):
    pass


class CalendarAuthorizationRejected(CalendarError):
    pass


class CalendarConnectionStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    REVOCATION_UNCERTAIN = "revocation_uncertain"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


class CalendarConnectionAction(StrEnum):
    """Lifecycle commands exposed by a connection projection."""

    CONNECT = "connect"
    COMPLETE_CONNECTION = "complete_connection"
    REAUTHORIZE = "reauthorize"
    DISCONNECT = "disconnect"


class CalendarFailureCategory(StrEnum):
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    CALLBACK_REJECTED = "callback_rejected"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    REVOCATION_UNCERTAIN = "revocation_uncertain"


class CalendarRecoveryAction(StrEnum):
    CONNECT = "connect"
    COMPLETE_CONNECTION = "complete_connection"
    RETRY = "retry"
    REAUTHORIZE = "reauthorize"
    DISCONNECT = "disconnect"
    WAIT = "wait"
    CONTACT_ADMINISTRATOR = "contact_administrator"


class CalendarEventAvailability(StrEnum):
    BUSY = "busy"
    FREE = "free"


class CalendarConflictRelationship(StrEnum):
    OVERLAPS = "overlaps"
    CONTAINS = "contains"
    CONTAINED_BY = "contained_by"
    SAME_INTERVAL = "same_interval"


@dataclass(frozen=True, slots=True)
class CalendarConnectionError:
    category: CalendarFailureCategory
    message: str
    retryable: bool
    recovery_action: CalendarRecoveryAction
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarConnection:
    status: CalendarConnectionStatus
    reads_enabled: bool
    selected_calendar_count: int
    connected_at: datetime | None
    updated_at: datetime
    last_error: CalendarConnectionError | None = None
    # Terminal uncertainty deliberately remains non-connectable until an
    # explicit external-revocation acknowledgement exists.
    revocation_terminal: bool = False

    @property
    def available_actions(self) -> tuple[str, ...]:
        actions = {
            CalendarConnectionStatus.DISCONNECTED: (CalendarConnectionAction.CONNECT,),
            CalendarConnectionStatus.CONNECTING: (CalendarConnectionAction.COMPLETE_CONNECTION,),
            CalendarConnectionStatus.CONNECTED: (CalendarConnectionAction.DISCONNECT,),
            CalendarConnectionStatus.REAUTHORIZATION_REQUIRED: (
                CalendarConnectionAction.REAUTHORIZE,
                CalendarConnectionAction.DISCONNECT,
            ),
            # A disconnect command resumes an uncertain revocation.  Retry is
            # recovery guidance, not a separate public lifecycle operation.
            CalendarConnectionStatus.REVOCATION_UNCERTAIN: (
                () if self.revocation_terminal else (CalendarConnectionAction.DISCONNECT,)
            ),
            CalendarConnectionStatus.DISABLED: (),
            CalendarConnectionStatus.UNAVAILABLE: (),
        }[self.status]
        return tuple(action.value for action in actions)


@dataclass(frozen=True, slots=True)
class CalendarAuthorizationAttempt:
    """Opaque server-side OAuth attempt metadata; no provider secret fields."""

    attempt_id: UUID
    owner: tuple[str, str]
    session_id: str
    state: str
    nonce: str
    handle: str
    expires_at: datetime
    # PKCE verifier is server-held attempt state and is never serialized into
    # an Aura-facing DTO or persisted in the relational repository.
    verifier: str = ""


@dataclass(frozen=True, slots=True)
class CalendarAuthorizationStart:
    connection: CalendarConnection
    authorization_path: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarCallbackResult:
    """Short-lived callback result held by the attempt store."""

    attempt_id: UUID
    owner: tuple[str, str]
    session_id: str
    credential: object
    expires_at: datetime
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarCredentialEnvelope:
    """Persisted ciphertext envelope; key identifiers are bounded strings."""

    ciphertext: bytes
    nonce: bytes
    key_id: str
    associated_data_digest: bytes


@dataclass(frozen=True, slots=True)
class CalendarRevocationState:
    attempts: int
    deadline: datetime | None
    next_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class CalendarRevocationLease:
    """Opaque reservation for one outbound provider attempt."""

    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SelectableCalendar:
    id: UUID
    display_name: str
    time_zone: str
    selected: bool
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class SelectableCalendarPage:
    items: tuple[SelectableCalendar, ...]
    next_cursor: str | None
    selection_version: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarSelection:
    calendar_ids: tuple[UUID, ...]
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarQuery:
    range_start: datetime
    range_end: datetime
    display_time_zone: str
    calendar_ids: tuple[UUID, ...]
    limit: int = 50
    cursor: str | None = None
    timeout_ms: int = 5000


# Contract-facing names retained as aliases so entrypoints do not need to
# invent transport-specific domain types.
CalendarEventQuery = CalendarQuery
CalendarConflictQuery = CalendarQuery


@dataclass(frozen=True, slots=True)
class TimedCalendarEvent:
    id: str
    calendar_id: UUID
    title: str
    description: str | None
    location: str | None
    availability: CalendarEventAvailability
    start_instant: datetime
    end_instant: datetime
    event_time_zone: str
    display_time_zone: str
    spans_multiple_days: bool
    kind: str = "timed"


@dataclass(frozen=True, slots=True)
class AllDayCalendarEvent:
    id: str
    calendar_id: UUID
    title: str
    description: str | None
    location: str | None
    availability: CalendarEventAvailability
    start_date: date
    end_date_exclusive: date
    event_time_zone: str
    display_time_zone: str
    spans_multiple_days: bool
    kind: str = "all_day"


CalendarEvent = TimedCalendarEvent | AllDayCalendarEvent


@dataclass(frozen=True, slots=True)
class CalendarReadWarning:
    calendar_id: UUID
    category: CalendarFailureCategory
    retryable: bool
    recovery_action: CalendarRecoveryAction


@dataclass(frozen=True, slots=True)
class CalendarEventPage:
    range_start: datetime
    range_end: datetime
    display_time_zone: str
    items: tuple[CalendarEvent, ...]
    next_cursor: str | None
    partial: bool
    warnings: tuple[CalendarReadWarning, ...]
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarEventReference:
    event_id: str
    calendar_id: UUID


@dataclass(frozen=True, slots=True)
class CalendarConflict:
    left: CalendarEventReference
    right: CalendarEventReference
    relationship: CalendarConflictRelationship
    overlap_start: datetime
    overlap_end: datetime


@dataclass(frozen=True, slots=True)
class CalendarConflictPage:
    range_start: datetime
    range_end: datetime
    display_time_zone: str
    items: tuple[CalendarConflict, ...]
    next_cursor: str | None
    partial: bool
    warnings: tuple[CalendarReadWarning, ...]
    evaluated_at: datetime


class CalendarEventLike(Protocol):
    id: str
    calendar_id: UUID
    availability: CalendarEventAvailability


def validate_timezone(value: str) -> str:
    if not value or len(value) > 128:
        raise CalendarValidationError("invalid timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CalendarValidationError("invalid timezone") from exc
    return value


def validate_query(query: CalendarQuery) -> CalendarQuery:
    if query.range_start.tzinfo is None or query.range_end.tzinfo is None:
        raise CalendarValidationError("range must be timezone-aware")
    start = query.range_start.astimezone(UTC)
    end = query.range_end.astimezone(UTC)
    if end <= start or end - start > timedelta(days=MAX_QUERY_DAYS):
        raise CalendarValidationError("range is outside the permitted window")
    validate_timezone(query.display_time_zone)
    if not 1 <= len(query.calendar_ids) <= MAX_CALENDARS_PER_SELECTION:
        raise CalendarValidationError("calendar selection is outside bounds")
    if len(set(query.calendar_ids)) != len(query.calendar_ids):
        raise CalendarValidationError("calendar selection must be unique")
    if not 1 <= query.limit <= MAX_EVENT_PAGE:
        raise CalendarValidationError("event limit is outside bounds")
    if not 500 <= query.timeout_ms <= 10_000:
        raise CalendarValidationError("timeout is outside bounds")
    return CalendarQuery(
        start,
        end,
        query.display_time_zone,
        query.calendar_ids,
        query.limit,
        query.cursor,
        query.timeout_ms,
    )


def _event_interval(event: CalendarEvent) -> tuple[datetime, datetime] | None:
    if isinstance(event, TimedCalendarEvent):
        start = event.start_instant.astimezone(UTC)
        end = event.end_instant.astimezone(UTC)
    else:
        zone = ZoneInfo(event.event_time_zone)
        start = datetime.combine(event.start_date, datetime.min.time(), tzinfo=zone).astimezone(UTC)
        end = datetime.combine(
            event.end_date_exclusive, datetime.min.time(), tzinfo=zone
        ).astimezone(UTC)
    if end <= start or event.availability is CalendarEventAvailability.FREE:
        return None
    return start, end


def _event_sort_key(event: CalendarEvent) -> tuple[datetime, datetime, str]:
    interval = _event_interval(event)
    if interval is None:
        # Free and zero-duration events are excluded before sorting.  This
        # fallback keeps the helper total for callers inspecting bad input.
        return datetime.max.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC), event.id
    return interval[0], interval[1], event.id


def evaluate_conflicts(
    events: tuple[CalendarEvent, ...] | list[CalendarEvent], *, limit: int = MAX_CONFLICT_PAGE
) -> tuple[CalendarConflict, ...]:
    """Return each positive-duration conflict once in canonical order."""
    if not 1 <= limit <= MAX_CONFLICT_PAGE:
        raise CalendarValidationError("conflict limit is outside bounds")
    deduplicated: dict[tuple[UUID, str], CalendarEvent] = {}
    for event in events:
        deduplicated.setdefault((event.calendar_id, event.id), event)
    eligible = [(event, _event_interval(event)) for event in deduplicated.values()]
    eligible = [(event, interval) for event, interval in eligible if interval is not None]
    eligible.sort(key=lambda pair: _event_sort_key(pair[0]))
    conflicts: list[CalendarConflict] = []
    for index, (left, left_interval) in enumerate(eligible):
        assert left_interval is not None
        for right, right_interval in eligible[index + 1 :]:
            assert right_interval is not None
            if right_interval[0] >= left_interval[1]:
                break
            overlap_start = max(left_interval[0], right_interval[0])
            overlap_end = min(left_interval[1], right_interval[1])
            if overlap_end <= overlap_start:
                continue
            if left_interval == right_interval:
                relationship = CalendarConflictRelationship.SAME_INTERVAL
            elif left_interval[0] <= right_interval[0] and left_interval[1] >= right_interval[1]:
                relationship = CalendarConflictRelationship.CONTAINS
            elif right_interval[0] <= left_interval[0] and right_interval[1] >= left_interval[1]:
                relationship = CalendarConflictRelationship.CONTAINED_BY
            else:
                relationship = CalendarConflictRelationship.OVERLAPS
            conflicts.append(
                CalendarConflict(
                    CalendarEventReference(left.id, left.calendar_id),
                    CalendarEventReference(right.id, right.calendar_id),
                    relationship,
                    overlap_start,
                    overlap_end,
                )
            )
            if len(conflicts) >= limit:
                return tuple(conflicts)
    conflicts.sort(
        key=lambda item: (
            item.overlap_start,
            item.overlap_end,
            item.left.event_id,
            item.right.event_id,
        )
    )
    return tuple(conflicts)


def normalize_timed_event(
    *,
    event_id: str,
    calendar_id: UUID,
    title: str,
    description: str | None,
    location: str | None,
    availability: CalendarEventAvailability,
    start_instant: datetime,
    end_instant: datetime,
    event_time_zone: str,
    display_time_zone: str,
) -> TimedCalendarEvent:
    if not event_id or len(event_id) > 512 or len(title) > 1000:
        raise CalendarValidationError("event fields are outside bounds")
    if description is not None and len(description) > 10_000:
        raise CalendarValidationError("event fields are outside bounds")
    if location is not None and len(location) > 1000:
        raise CalendarValidationError("event fields are outside bounds")
    validate_timezone(event_time_zone)
    validate_timezone(display_time_zone)
    if start_instant.tzinfo is None or end_instant.tzinfo is None or end_instant <= start_instant:
        raise CalendarValidationError("event interval is invalid")
    start = start_instant.astimezone(UTC)
    end = end_instant.astimezone(UTC)
    display = ZoneInfo(display_time_zone)
    return TimedCalendarEvent(
        event_id,
        calendar_id,
        title,
        description,
        location,
        availability,
        start,
        end,
        event_time_zone,
        display_time_zone,
        start.astimezone(display).date()
        != (end - timedelta(microseconds=1)).astimezone(display).date(),
    )


def normalize_all_day_event(
    *,
    event_id: str,
    calendar_id: UUID,
    title: str,
    description: str | None,
    location: str | None,
    availability: CalendarEventAvailability,
    start_date: date,
    end_date_exclusive: date,
    event_time_zone: str,
    display_time_zone: str,
) -> AllDayCalendarEvent:
    """Normalize a provider all-day event to a half-open local-date interval."""
    if not event_id or len(event_id) > 512 or len(title) > 1000:
        raise CalendarValidationError("event fields are outside bounds")
    if description is not None and len(description) > 10_000:
        raise CalendarValidationError("event fields are outside bounds")
    if location is not None and len(location) > 1000:
        raise CalendarValidationError("event fields are outside bounds")
    validate_timezone(event_time_zone)
    validate_timezone(display_time_zone)
    if end_date_exclusive <= start_date:
        raise CalendarValidationError("event date interval is invalid")
    return AllDayCalendarEvent(
        event_id,
        calendar_id,
        title,
        description,
        location,
        availability,
        start_date,
        end_date_exclusive,
        event_time_zone,
        display_time_zone,
        (end_date_exclusive - start_date).days > 1,
    )


def normalize_event(**values: object) -> CalendarEvent:
    """Normalize one narrowed provider record without accepting provider types."""
    kind = values.pop("kind", "timed")
    if kind == "all_day":
        return normalize_all_day_event(**values)  # type: ignore[arg-type]
    return normalize_timed_event(**values)  # type: ignore[arg-type]
