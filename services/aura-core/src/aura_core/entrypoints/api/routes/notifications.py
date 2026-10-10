"""Owner-authenticated notification APIs and metadata-only SSE replay."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.automation.notifications.public import (
    NotificationIdempotencyConflict,
    NotificationNotFound,
    NotificationStatus,
    NotificationVersionConflict,
    as_payload,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import new_span_id

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])


class NotificationAcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotificationNotFound):
        return HTTPException(404, "notification not found")
    if isinstance(exc, (NotificationVersionConflict, NotificationIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "notification operation failed")


def _acknowledgement_error_class(exc: Exception) -> str:
    if isinstance(exc, NotificationNotFound):
        return "not_found"
    if isinstance(exc, (NotificationVersionConflict, NotificationIdempotencyConflict)):
        return "conflict"
    if isinstance(exc, (ValueError, TypeError)):
        return "validation"
    return "persistence"


def _record_acknowledgement(
    state: AppState, started: float, outcome: str, error_class: str | None = None
) -> None:
    duration = (monotonic() - started) * 1000
    trace_id = uuid4().hex
    state.metrics.increment(
        "aura.automation.notification_delivery",
        "notification_acknowledgement_outcome",
        outcome=outcome,
    )
    state.metrics.record_span(
        "aura.automation.notification_delivery",
        "notification.acknowledge",
        duration,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="postgresql",
        outcome=outcome,
        error_class=error_class,
    )
    if outcome == "error":
        state.metrics.increment(
            "aura.automation.notification_delivery",
            "notification_failure_count",
            dependency="postgresql",
            outcome="error",
            error_class=error_class or "persistence",
        )


@router.get("")
async def list_notifications(
    request: Request,
    status_filter: NotificationStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        page = await _state(request).notification_service.list(
            session.principal.issuer,
            session.principal.subject,
            status=status_filter,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "items": [as_payload(item) for item in page.items],
        "nextCursor": page.next_cursor,
        "unacknowledgedCount": page.unacknowledged_count,
    }


@router.get("/{notification_id:uuid}")
async def get_notification(
    request: Request,
    notification_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        item = await _state(request).notification_service.get(
            session.principal.issuer, session.principal.subject, notification_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_payload(item)


@router.post("/{notification_id:uuid}/acknowledge")
async def acknowledge_notification(
    request: Request,
    notification_id: UUID,
    body: NotificationAcknowledgeRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    started = monotonic()
    state = _state(request)
    try:
        item = await state.notification_service.acknowledge(
            session.principal.issuer,
            session.principal.subject,
            notification_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        _record_acknowledgement(
            state,
            started,
            "error",
            _acknowledgement_error_class(exc),
        )
        raise _error(exc) from exc
    _record_acknowledgement(state, started, "ok")
    return as_payload(item)


@router.get("/stream")
async def stream_notification_events(
    request: Request,
    session: Session = Depends(require_session),
    last_event_id: UUID | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = _state(request)
    issuer = session.principal.issuer
    subject = session.principal.subject
    if last_event_id is not None and not await state.notification_service.cursor_is_authorized(
        issuer, subject, last_event_id
    ):
        # Do not reveal whether another owner has a matching cursor.
        raise HTTPException(status_code=410, detail="event cursor is no longer available")

    connection_started = monotonic()
    trace_id = uuid4().hex
    state.metrics.increment(
        "aura.automation.notification_delivery",
        "notification_sse_connection_count",
        outcome="ok",
    )
    state.metrics.record_span(
        "aura.automation.notification_delivery",
        "notification.sse.connect",
        (monotonic() - connection_started) * 1000,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="sse_client",
        outcome="ok",
    )

    async def generate() -> AsyncIterator[str]:
        cursor = last_event_id
        while True:
            replay_started = monotonic()
            try:
                events = await state.notification_service.replay(issuer, subject, after=cursor)
            except Exception:
                duration = (monotonic() - replay_started) * 1000
                state.metrics.increment(
                    "aura.automation.notification_delivery",
                    "notification_failure_count",
                    dependency="postgresql",
                    outcome="error",
                    error_class="persistence",
                )
                state.metrics.record_span(
                    "aura.automation.notification_delivery",
                    "notification.sse.replay",
                    duration,
                    trace_id=trace_id,
                    span_id=new_span_id(),
                    parent_span_id=None,
                    dependency="postgresql",
                    outcome="error",
                    error_class="persistence",
                )
                raise
            if events:
                state.metrics.observe(
                    "aura.automation.notification_delivery",
                    "notification_sse_replay_count",
                    float(len(events)),
                    outcome="ok",
                )
            state.metrics.record_span(
                "aura.automation.notification_delivery",
                "notification.sse.replay",
                (monotonic() - replay_started) * 1000,
                trace_id=trace_id,
                span_id=new_span_id(),
                parent_span_id=None,
                dependency="postgresql",
                outcome="ok",
            )
            for event in events:
                cursor = event.id
                yield f"id: {event.id}\nevent: {event.payload()['eventType']}\ndata: {json.dumps(event.payload(), separators=(',', ':'))}\n\n"
                await asyncio.sleep(0)
            # NATS is only an optimization wakeup; PostgreSQL replay remains
            # authoritative, so a missed or duplicate wakeup cannot lose or
            # duplicate an inbox event. AppState subscribes to that wakeup and
            # a timeout produces a heartbeat.
            woke = await state.notification_service.wait_for_wakeup(15.0)
            if not woke:
                yield f"event: heartbeat\ndata: {json.dumps({'schemaVersion': 1, 'eventType': 'heartbeat', 'occurredAt': datetime.now(UTC).isoformat()}, separators=(',', ':'))}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
