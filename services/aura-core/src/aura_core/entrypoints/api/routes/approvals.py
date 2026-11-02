"""Owner-authorized approval inbox and metadata-only SSE replay."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.governance.approvals.public import (
    APPROVAL_COMPONENT,
    ApprovalIdempotencyConflict,
    ApprovalNotFound,
    ApprovalStatus,
    ApprovalTransitionError,
    ApprovalVersionConflict,
    as_payload,
    detail_payload,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import new_span_id

router = APIRouter(prefix="/api/v1/approvals", tags=["Approvals"])


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ApprovalNotFound):
        return HTTPException(404, "approval not found")
    if isinstance(exc, (ApprovalVersionConflict, ApprovalIdempotencyConflict, ApprovalTransitionError)):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "approval operation failed")


@router.get("")
async def list_approvals(
    request: Request,
    status_filter: ApprovalStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    try:
        page = await _state(request).approval_service.list(session.principal.issuer, session.principal.subject, limit=limit, cursor=cursor, status=status_filter)
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [as_payload(item) for item in page.items], "nextCursor": page.next_cursor}


@router.get("/stream")
async def stream_approval_events(
    request: Request,
    session: Session = Depends(require_session),
    last_event_id: UUID | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = _state(request)
    issuer, subject = session.principal.issuer, session.principal.subject
    if last_event_id is not None and not await state.approval_service.cursor_is_authorized(issuer, subject, last_event_id):
        raise HTTPException(status_code=410, detail="event cursor is no longer available")
    started = monotonic()
    state.metrics.increment(APPROVAL_COMPONENT, "approval_sse_connection_count", outcome="accepted")

    async def generate() -> AsyncIterator[str]:
        cursor = last_event_id
        outcome = "canceled"
        try:
            while True:
                replay_started = monotonic()
                replay_trace = cursor or UUID(int=0)
                try:
                    events = await state.approval_service.replay(issuer, subject, after=cursor)
                except Exception:
                    state.metrics.record_span(APPROVAL_COMPONENT, "approval.sse.replay", (monotonic() - replay_started) * 1000.0, trace_id=replay_trace.hex, span_id=new_span_id(), parent_span_id=None, dependency="postgresql", outcome="error", error_class="database")
                    raise
                state.metrics.observe(APPROVAL_COMPONENT, "approval_sse_replay_count", float(len(events)), outcome="ok")
                replay_trace = events[-1].id if events else replay_trace
                replay_duration = (monotonic() - replay_started) * 1000.0
                state.metrics.observe(APPROVAL_COMPONENT, "approval_sse_replay_duration_ms", replay_duration, outcome="ok")
                state.metrics.record_span(APPROVAL_COMPONENT, "approval.sse.replay", replay_duration, trace_id=replay_trace.hex, span_id=new_span_id(), parent_span_id=None, dependency="postgresql", outcome="ok")
                for event in events:
                    cursor = event.id
                    payload = event.payload()
                    yield f"id: {event.id}\nevent: {payload['eventType']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    await asyncio.sleep(0)
                woke = await state.approval_service.wait_for_wakeup(15.0)
                if not woke:
                    yield f"event: heartbeat\ndata: {json.dumps({'schemaVersion': 1, 'eventType': 'heartbeat', 'occurredAt': datetime.now(UTC).isoformat()}, separators=(',', ':'))}\n\n"
        except asyncio.CancelledError:
            outcome = "canceled"
            raise
        except Exception:
            outcome = "error"
            state.metrics.increment(APPROVAL_COMPONENT, "approval_sse_connection_count", outcome="error")
            raise
        finally:
            state.metrics.observe(APPROVAL_COMPONENT, "approval_sse_duration_ms", (monotonic() - started) * 1000.0, outcome=outcome)
            state.metrics.record_span(APPROVAL_COMPONENT, "approval.sse.connect", (monotonic() - started) * 1000.0, trace_id=(last_event_id or UUID(int=0)).hex, span_id=new_span_id(), parent_span_id=None, dependency="sse_client", outcome=outcome, error_class="delivery" if outcome == "error" else None)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/{approval_id:uuid}")
async def get_approval(request: Request, approval_id: UUID, session: Session = Depends(require_session)) -> dict[str, object]:
    try:
        detail = await _state(request).approval_service.get(session.principal.issuer, session.principal.subject, approval_id)
    except Exception as exc:
        raise _error(exc) from exc
    return detail_payload(detail)


async def _decide(request: Request, approval_id: UUID, body: ApprovalDecisionRequest, session: Session, idempotency_key: UUID, operation: str) -> dict[str, object]:
    state = _state(request)
    try:
        item = await getattr(state.approval_service, operation)(session.principal.issuer, session.principal.subject, approval_id, expected_version=body.expected_version, idempotency_key=str(idempotency_key))
    except Exception as exc:
        raise _error(exc) from exc
    return as_payload(item)


@router.post("/{approval_id:uuid}/approve")
async def approve_approval(request: Request, approval_id: UUID, body: ApprovalDecisionRequest, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    return await _decide(request, approval_id, body, session, idempotency_key, "approve")


@router.post("/{approval_id:uuid}/reject")
async def reject_approval(request: Request, approval_id: UUID, body: ApprovalDecisionRequest, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    return await _decide(request, approval_id, body, session, idempotency_key, "reject")


__all__ = ["router"]
