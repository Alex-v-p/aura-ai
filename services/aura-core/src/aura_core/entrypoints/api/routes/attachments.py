"""Thin owner-authenticated image attachment transport."""

# ruff: noqa: B008, B009, E501

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.artifacts.attachments.public import (
    AttachmentConflict,
    AttachmentError,
    AttachmentNotFound,
    AttachmentService,
    ImageValidationError,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/attachments", tags=["Attachments"])
AttachmentMediaType = Literal["image/png", "image/jpeg", "image/webp"]
AttachmentStatusLiteral = Literal["uploading", "processing", "ready", "failed", "deleting"]


class CreateAttachmentUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    filename: str = Field(min_length=1, max_length=255)
    media_type: AttachmentMediaType = Field(alias="mediaType")
    byte_count: int = Field(alias="byteCount", ge=1, le=25_000_000)


class AttachmentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _service(request: Request) -> AttachmentService:
    return cast(AppState, request.app.state.aura).attachment_service


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _payload(item: object) -> dict[str, object]:
    failure = getattr(item, "failure_code", None)
    return {
        "id": str(getattr(item, "id")),
        "filename": getattr(item, "filename"),
        "mediaType": getattr(item, "media_type"),
        "byteCount": getattr(item, "byte_count"),
        "status": getattr(getattr(item, "status"), "value", getattr(item, "status")),
        "failure": {"code": failure, "retryable": True} if failure else None,
        "version": getattr(item, "version"),
        "width": getattr(item, "width", None),
        "height": getattr(item, "height", None),
        "createdAt": getattr(item, "created_at").isoformat(),
        "updatedAt": getattr(item, "updated_at").isoformat(),
    }


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def create_upload(
    request: Request,
    body: CreateAttachmentUploadRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    try:
        upload = await _service(request).create_upload_session(
            session.principal.issuer,
            session.principal.subject,
            filename=body.filename,
            media_type=body.media_type,
            byte_count=body.byte_count,
            idempotency_key=str(idempotency_key),
        )
    except AttachmentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    upload_id = cast(UUID, upload["id"])
    attachment_id = cast(UUID, upload["attachment_id"])
    created = cast(datetime, upload["created_at"])
    return {
        "id": str(upload_id),
        "attachmentId": str(attachment_id),
        "filename": cast(str, upload["filename"]),
        "mediaType": cast(str, upload["media_type"]),
        "byteCount": cast(int, upload["byte_count"]),
        "status": cast(str, upload.get("status", "uploading")),
        "version": cast(int, upload.get("version", 1)),
        "createdAt": created.isoformat(),
        "expiresAt": cast(datetime, upload["expires_at"]).isoformat(),
    }


@router.put("/uploads/{upload_id}/content", status_code=status.HTTP_202_ACCEPTED)
async def upload_content(
    request: Request,
    upload_id: UUID,
    response: Response,
    content_length: int = Header(..., alias="Content-Length", ge=1, le=25_000_000),
    content_type: AttachmentMediaType = Header(..., alias="Content-Type"),
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    expected_version: int = Header(..., alias="X-Expected-Version", ge=1),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    if request.headers.get("content-length") != str(content_length):
        raise HTTPException(status_code=400, detail="content length is invalid")
    try:
        item = await _service(request).complete_upload(
            session.principal.issuer,
            session.principal.subject,
            upload_id,
            stream=request.stream(),
            idempotency_key=str(idempotency_key),
            media_type=content_type,
            expected_version=expected_version,
        )
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="upload session not found") from exc
    except AttachmentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImageValidationError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=503, detail="attachment storage unavailable") from exc
    return {"attachment": _payload(item)}


@router.get("")
async def list_attachments(
    request: Request,
    response: Response,
    limit: int = Query(default=30, ge=1, le=100),
    status_filter: AttachmentStatusLiteral | None = Query(default=None, alias="status"),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    items = await _service(request).list(*_owner(session))
    if status_filter:
        items = [item for item in items if item.status.value == status_filter]
    return {"items": [_payload(item) for item in items[:limit]], "nextCursor": None}


@router.get("/{attachment_id}")
async def get_attachment(
    request: Request,
    attachment_id: UUID,
    response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        return _payload(await _service(request).get(*_owner(session), attachment_id))
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="attachment not found") from exc


@router.delete("/{attachment_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_attachment(
    request: Request,
    attachment_id: UUID,
    response: Response,
    body: AttachmentTransitionRequest,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).delete(
            *_owner(session),
            attachment_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="attachment not found") from exc
    except AttachmentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload: dict[str, object] = {
        "attachmentId": str(item.id),
        # The successful receipt is intentionally the public ``deleting``
        # contract even though the durable row is already an unavailable
        # tombstone.  A failed cleanup is a retryable 503 and exposes FAILED.
        "status": "failed" if item.status.value == "failed" else "deleting",
        "version": item.version,
        "acceptedAt": datetime.now(UTC).isoformat(),
    }
    if item.status.value == "failed":
        return cast(
            dict[str, object],
            JSONResponse(
                status_code=503,
                content=payload,
                headers={"Cache-Control": "no-store"},
            ),
        )
    return payload


@router.get("/{attachment_id}/content")
async def get_content(
    request: Request,
    attachment_id: UUID,
    response: Response,
    session: Session = Depends(require_session),
) -> StreamingResponse:
    _no_store(response)
    try:
        data = await _service(request).content(*_owner(session), attachment_id)
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="attachment not found") from exc
    stream = StreamingResponse(iter((data,)), media_type="application/octet-stream")
    _no_store(stream)
    return stream


@router.get("/{attachment_id}/preview")
async def get_preview(
    request: Request,
    attachment_id: UUID,
    response: Response,
    session: Session = Depends(require_session),
) -> StreamingResponse:
    _no_store(response)
    try:
        item = await _service(request).get(*_owner(session), attachment_id)
        data = await _service(request).preview(*_owner(session), attachment_id)
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail="attachment not found") from exc
    stream = StreamingResponse(iter((data,)), media_type=item.media_type)
    _no_store(stream)
    return stream
