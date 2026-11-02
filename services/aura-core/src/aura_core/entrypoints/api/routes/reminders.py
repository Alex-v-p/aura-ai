"""Owner-authenticated HTTP translation for one-shot and recurring reminders."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aura_core.domains.automation.reminders.public import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurrenceValidationError,
    ReminderIdempotencyConflict,
    ReminderNotFound,
    ReminderOccurrence,
    ReminderSeries,
    ReminderSeriesError,
    ReminderSeriesIdempotencyConflict,
    ReminderSeriesNotFound,
    ReminderSeriesTransitionError,
    ReminderSeriesVersionConflict,
    ReminderTransitionError,
    ReminderValidationError,
    ReminderVersionConflict,
    ReminderView,
    as_payload,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(tags=["Reminders"])


class ReminderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=2000)
    due_at: datetime = Field(alias="dueAt")


class ReminderTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class RecurrenceRuleRequest(BaseModel):
    """Transport representation of the bounded recurrence rule.

    RecurrenceRule remains the domain authority.  This DTO only translates
    JSON naming and performs the cheap shape checks appropriate at the HTTP
    boundary.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    frequency: RecurrenceFrequency
    interval: int = Field(..., ge=1, le=365)
    weekdays: list[int] = Field(...)
    month_day: int | None = Field(..., alias="monthDay", ge=1, le=31)
    count: int | None = Field(..., ge=1, le=10000)
    end_date: date | None = Field(..., alias="endDate")
    local_start: datetime = Field(..., alias="localStart")
    timezone: str = Field(..., alias="timeZone", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_weekdays(self) -> RecurrenceRuleRequest:
        if len(self.weekdays) > 7 or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must contain at most seven unique ISO days")
        return self

    def to_domain(self) -> RecurrenceRule:
        return RecurrenceRule(
            self.frequency,
            self.interval,
            tuple(self.weekdays),
            self.month_day,
            self.count,
            self.end_date,
        )


class ReminderSeriesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=2000)
    recurrence: RecurrenceRuleRequest

    def to_rule(self) -> RecurrenceRule:
        return self.recurrence.to_domain()

    def local_start_value(self) -> datetime:
        return self.recurrence.local_start

    def timezone_value(self) -> str:
        return self.recurrence.timezone


class ReminderSeriesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    recurrence: RecurrenceRuleRequest | None = None

    @model_validator(mode="after")
    def require_change(self) -> ReminderSeriesUpdateRequest:
        if self.content is None and self.recurrence is None:
            raise ValueError("content or recurrence is required")
        return self

    def to_rule(self) -> RecurrenceRule | None:
        return self.recurrence.to_domain() if self.recurrence is not None else None


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ReminderNotFound, ReminderSeriesNotFound)):
        return HTTPException(404, "reminder not found")
    if isinstance(
        exc,
        (
            ReminderVersionConflict,
            ReminderIdempotencyConflict,
            ReminderSeriesVersionConflict,
            ReminderSeriesIdempotencyConflict,
        ),
    ):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ReminderTransitionError, ReminderSeriesTransitionError)):
        return HTTPException(409, str(exc))
    if isinstance(
        exc,
        (
            ReminderValidationError,
            ReminderSeriesError,
            RecurrenceValidationError,
            ValueError,
            TypeError,
        ),
    ):
        return HTTPException(422, str(exc))
    return HTTPException(500, "reminder operation failed")


@router.get("/api/v1/reminders")
async def list_reminders(
    request: Request,
    view: ReminderView = Query(default=ReminderView.UPCOMING),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        page = await _state(request).reminder_service.list(
            session.principal.issuer,
            session.principal.subject,
            view=view,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [as_payload(item) for item in page.items], "nextCursor": page.next_cursor}


@router.post("/api/v1/reminders", status_code=status.HTTP_201_CREATED)
async def create_reminder(
    request: Request,
    body: ReminderCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).reminder_service.create(
            session.principal.issuer,
            session.principal.subject,
            content=body.content,
            due_at=body.due_at,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_payload(item)


@router.get("/api/v1/reminders/{reminder_id}")
async def get_reminder(
    request: Request,
    reminder_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        item = await _state(request).reminder_service.get(
            session.principal.issuer, session.principal.subject, reminder_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_payload(item)


async def _transition(
    request: Request,
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    session: Session,
    idempotency_key: UUID,
    operation: str,
) -> dict[str, object]:
    service = _state(request).reminder_service
    try:
        item = await getattr(service, operation)(
            session.principal.issuer,
            session.principal.subject,
            reminder_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_payload(item)


@router.post("/api/v1/reminders/{reminder_id}/complete")
async def complete_reminder(
    request: Request,
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(request, reminder_id, body, session, idempotency_key, "complete")


@router.post("/api/v1/reminders/{reminder_id}/cancel")
async def cancel_reminder(
    request: Request,
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(request, reminder_id, body, session, idempotency_key, "cancel")


def _readable_rule(rule: RecurrenceRule) -> str:
    """Build a stable, private-owner-facing description of a rule."""

    interval = rule.interval
    if rule.frequency is RecurrenceFrequency.DAILY:
        unit = "day" if interval == 1 else "days"
        return f"Every {interval} {unit}"
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    days = ", ".join(names[day - 1] for day in rule.weekdays)
    if rule.frequency is RecurrenceFrequency.SELECTED_WEEKDAY:
        unit = "week" if interval == 1 else "weeks"
        return f"Every {interval} {unit} on {days}"
    if rule.frequency is RecurrenceFrequency.WEEKLY:
        unit = "week" if interval == 1 else "weeks"
        return f"Every {interval} {unit} on {days}"
    unit = "month" if interval == 1 else "months"
    return f"Every {interval} {unit} on day {rule.month_day}"


def _series_payload(item: ReminderSeries) -> dict[str, object]:
    recurrence = item.rule.to_payload()
    recurrence["localStart"] = item.local_start.isoformat()
    recurrence["timeZone"] = item.timezone
    return {
        "id": str(item.id),
        "content": item.content,
        "recurrence": recurrence,
        "readableRule": _readable_rule(item.rule),
        "timeZone": item.timezone,
        "localStart": item.local_start.isoformat(),
        "nextOccurrence": item.next_due_at.isoformat() if item.next_due_at else None,
        "nextReminderId": str(item.next_reminder_id) if item.next_reminder_id else None,
        "count": item.rule.count,
        "endDate": item.rule.end_date.isoformat() if item.rule.end_date else None,
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


def _occurrence_payload(item: ReminderOccurrence) -> dict[str, object]:
    occurred_at = item.fired_at or item.completed_at or item.skipped_at
    if occurred_at is None and item.status.value in {
        "replaced",
        "paused-window",
        "series-canceled",
    }:
        occurred_at = item.updated_at
    return {
        "id": str(item.id),
        "seriesId": str(item.series_id),
        "reminderId": str(item.reminder_id),
        "sequence": item.sequence,
        "outcome": item.status.value,
        "localDueAt": item.local_due_at.isoformat(),
        "dueAt": item.due_at.isoformat(),
        "occurredAt": occurred_at.isoformat() if occurred_at else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "skippedAt": item.skipped_at.isoformat() if item.skipped_at else None,
        "firedAt": item.fired_at.isoformat() if item.fired_at else None,
        "completedAt": item.completed_at.isoformat() if item.completed_at else None,
    }


@router.get("/api/v1/reminder-series")
async def list_reminder_series(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        page = await _state(request).reminder_service.list_series(
            session.principal.issuer,
            session.principal.subject,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "items": [_series_payload(item) for item in page.items],
        "nextCursor": page.next_cursor,
    }


@router.post("/api/v1/reminder-series", status_code=status.HTTP_201_CREATED)
async def create_reminder_series(
    request: Request,
    body: ReminderSeriesCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).reminder_service.create_series(
            session.principal.issuer,
            session.principal.subject,
            content=body.content,
            local_start=body.local_start_value(),
            timezone=body.timezone_value(),
            rule=body.to_rule(),
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _series_payload(item)


@router.get("/api/v1/reminder-series/{series_id}")
async def get_reminder_series(
    request: Request,
    series_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        item = await _state(request).reminder_service.get_series(
            session.principal.issuer, session.principal.subject, series_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _series_payload(item)


@router.get("/api/v1/reminder-series/{series_id}/occurrences")
async def list_reminder_series_occurrences(
    request: Request,
    series_id: UUID,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        page = await _state(request).reminder_service.list_occurrences(
            session.principal.issuer,
            session.principal.subject,
            series_id,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "items": [_occurrence_payload(item) for item in page.items],
        "nextCursor": page.next_cursor,
    }


async def _series_transition(
    request: Request,
    series_id: UUID,
    body: ReminderTransitionRequest,
    session: Session,
    idempotency_key: UUID,
    operation: str,
) -> dict[str, object]:
    service = _state(request).reminder_service
    try:
        item = await getattr(service, operation)(
            session.principal.issuer,
            session.principal.subject,
            series_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _series_payload(item)


@router.post("/api/v1/reminder-series/{series_id}/pause")
async def pause_reminder_series(
    request: Request,
    series_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _series_transition(
        request, series_id, body, session, idempotency_key, "pause_series"
    )


@router.post("/api/v1/reminder-series/{series_id}/resume")
async def resume_reminder_series(
    request: Request,
    series_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _series_transition(
        request, series_id, body, session, idempotency_key, "resume_series"
    )


@router.post("/api/v1/reminder-series/{series_id}/skip-next")
async def skip_next_reminder_series_occurrence(
    request: Request,
    series_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _series_transition(request, series_id, body, session, idempotency_key, "skip_next")


@router.post("/api/v1/reminder-series/{series_id}/cancel")
async def cancel_reminder_series(
    request: Request,
    series_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _series_transition(
        request, series_id, body, session, idempotency_key, "cancel_series"
    )


@router.post("/api/v1/reminder-series/{series_id}/update")
async def update_reminder_series(
    request: Request,
    series_id: UUID,
    body: ReminderSeriesUpdateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).reminder_service.update_series(
            session.principal.issuer,
            session.principal.subject,
            series_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            content=body.content,
            local_start=body.recurrence.local_start if body.recurrence else None,
            timezone=body.recurrence.timezone if body.recurrence else None,
            rule=body.to_rule(),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _series_payload(item)


__all__ = ["router"]
