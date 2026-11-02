"""Owner-authenticated HTTP translation for the metadata-only Task Center."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskListFilter,
    TaskNormalizedStatus,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.service import (
    TaskProjectionConflict,
    TaskProjectionNotFound,
    TaskProjectionService,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/tasks", tags=["Tasks"])
_OWNER_NAMESPACE = UUID("8edc7ad7-8f83-4f13-b34b-1fa37d4c6bf4")


class TaskCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_source_version: int = Field(alias="expectedSourceVersion", ge=1)


def _service(request: Request) -> TaskProjectionService:
    value = getattr(request.app.state.aura, "task_projection_service", None)
    if not isinstance(value, TaskProjectionService):
        raise HTTPException(503, "task center is unavailable")
    return value


def _owner(session: Session) -> UUID:
    return uuid5(_OWNER_NAMESPACE, f"{session.principal.issuer}:{session.principal.subject}")


def _payload(row: object) -> dict[str, object]:
    return cast(Any, row).as_summary()


def _detail_payload(  # pyright: ignore[reportUnusedFunction]
    row: object,
    *,
    display_label: str | None,
    recovery_action: str | None,
    artifact_refs: list[dict[str, object]] | tuple[dict[str, object], ...],
) -> dict[str, object]:
    return cast(Any, row).as_detail(
        display_label=display_label,
        recovery_action=recovery_action,
        artifact_refs=tuple(artifact_refs),
    )


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, TaskProjectionNotFound):
        return HTTPException(404, "task not found")
    if isinstance(exc, TaskProjectionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "task operation failed")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("")
async def list_tasks(
    request: Request,
    response: Response,
    source_kind: TaskSourceKind | None = Query(default=None, alias="sourceKind"),
    task_status: TaskNormalizedStatus | None = Query(default=None, alias="status"),
    updated_from: datetime | None = Query(default=None, alias="updatedFrom"),
    updated_to: datetime | None = Query(default=None, alias="updatedTo"),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=25, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        rows, next_cursor = await _service(request).list(
            TaskListFilter(
                _owner(session), source_kind, task_status, updated_from, updated_to, cursor, limit
            )
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [_payload(row) for row in rows], "nextCursor": next_cursor}


@router.get("/{task_id:uuid}")
async def get_task(
    request: Request, task_id: UUID, response: Response, session: Session = Depends(require_session)
) -> dict[str, object]:
    _no_store(response)
    service = _service(request)
    try:
        row = await service.find_by_task_id(_owner(session), task_id)
        return await service.detail_payload(_owner(session), row.source_kind, row.source_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{task_id:uuid}/events")
async def stream_task_events(
    request: Request,
    task_id: UUID,
    last_event_id: UUID | None = Header(default=None, alias="Last-Event-ID"),
    session: Session = Depends(require_session),
) -> StreamingResponse:
    service = _service(request)
    try:
        row = await service.find_by_task_id(_owner(session), task_id)
    except TaskProjectionNotFound as exc:
        raise HTTPException(404, "task not found") from exc

    async def generate() -> AsyncIterator[str]:
        cursor = last_event_id
        for _ in range(60):
            try:
                events = await service.replay_events(
                    _owner(session), row.source_kind, row.source_id, cursor
                )
            except LookupError:
                gap = {"taskId": str(row.source_id), "reconcile": True}
                yield f"event: task.gap\ndata: {json.dumps(gap, separators=(',', ':'))}\n\n"
                return
            for item in events:
                event = item.as_dict()
                yield f"id: {event['eventId']}\nevent: task.invalidated\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                cursor = item.event_id
            if await request.is_disconnected():
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def _command(
    request: Request,
    task_id: UUID,
    command: TaskCommand,
    body: TaskCommandRequest,
    session: Session,
    idempotency_key: str,
) -> dict[str, object]:
    service = _service(request)
    try:
        row = await service.find_by_task_id(_owner(session), task_id)
        receipt = await service.command(
            _owner(session),
            row.source_kind,
            row.source_id,
            command,
            expected_source_version=body.expected_source_version,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "taskId": str(receipt.task_id),
        "command": receipt.command.value,
        "state": receipt.state.value,
        "sourceVersion": receipt.source_version,
    }


@router.post("/{task_id:uuid}/cancel", status_code=202)
async def cancel_task(
    request: Request,
    task_id: UUID,
    body: TaskCommandRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    return await _command(request, task_id, TaskCommand.CANCEL, body, session, idempotency_key)


@router.post("/{task_id:uuid}/retry", status_code=202)
async def retry_task(
    request: Request,
    task_id: UUID,
    body: TaskCommandRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    return await _command(request, task_id, TaskCommand.RETRY, body, session, idempotency_key)


__all__ = ["router"]
