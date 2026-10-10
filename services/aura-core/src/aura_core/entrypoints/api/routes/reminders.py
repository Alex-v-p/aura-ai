"""Owner-authenticated HTTP translation for one-shot reminders."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.automation.reminders.public import (
    ReminderIdempotencyConflict,
    ReminderNotFound,
    ReminderTransitionError,
    ReminderValidationError,
    ReminderVersionConflict,
    ReminderView,
    as_payload,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/reminders", tags=["Reminders"])


class ReminderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=2000)
    due_at: datetime = Field(alias="dueAt")


class ReminderTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReminderNotFound):
        return HTTPException(404, "reminder not found")
    if isinstance(exc, (ReminderVersionConflict, ReminderIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, ReminderTransitionError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ReminderValidationError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "reminder operation failed")


@router.get("")
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


@router.post("", status_code=status.HTTP_201_CREATED)
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


@router.get("/{reminder_id}")
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


@router.post("/{reminder_id}/complete")
async def complete_reminder(
    request: Request,
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(request, reminder_id, body, session, idempotency_key, "complete")


@router.post("/{reminder_id}/cancel")
async def cancel_reminder(
    request: Request,
    reminder_id: UUID,
    body: ReminderTransitionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(request, reminder_id, body, session, idempotency_key, "cancel")


__all__ = ["router"]
