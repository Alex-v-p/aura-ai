# ruff: noqa: B008, B009, E501
"""Thin owner-authenticated document transport."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Literal, cast
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.knowledge.documents.ports import (
    DocumentObjectStoreError,
    DocumentRepositoryUnavailable,
)
from aura_core.domains.knowledge.documents.public import (
    DocumentError,
    DocumentNotFound,
    DocumentService,
    DocumentValidationError,
    DocumentVersionConflict,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.runtime.models.ports import ProviderTraceContext

router = APIRouter(prefix="/api/v1/documents", tags=["Documents"])

DocumentMediaType = Literal[
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "text/plain",
]
DocumentStatus = Literal["uploading", "queued", "processing", "ready", "failed", "deleting"]


class CreateDocumentUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    filename: str = Field(min_length=1, max_length=255)
    media_type: DocumentMediaType = Field(alias="mediaType")
    byte_count: int = Field(alias="byteCount", ge=1, le=100_000_000)


class DocumentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    media_type: DocumentMediaType | None = Field(default=None, alias="mediaType")


class DocumentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _service(request: Request) -> DocumentService:
    return cast(AppState, request.app.state.aura).document_service


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _payload(document: object, *, detail: bool = False) -> dict[str, object]:
    item = document
    failure_code = getattr(item, "failure_code")
    return {
        "id": str(getattr(item, "id")),
        "filename": getattr(item, "filename"),
        "mediaType": getattr(item, "media_type"),
        "byteCount": getattr(item, "byte_count"),
        "status": getattr(getattr(item, "status"), "value", getattr(item, "status")),
        "version": getattr(item, "version"),
        "failure": (
            {"code": failure_code, "retryable": True} if failure_code is not None else None
        ),
        "createdAt": getattr(item, "created_at").isoformat(),
        "updatedAt": getattr(item, "updated_at").isoformat(),
    }


def _detail_payload(document: object) -> dict[str, object]:
    payload = _payload(document, detail=True)
    payload["extractedTextLength"] = getattr(document, "extracted_text_length")
    payload["chunkCount"] = getattr(document, "chunk_count", 0)
    return payload


def _receipt(document: object, *, duplicate: bool = False) -> dict[str, object]:
    return {
        "document": _detail_payload(document),
        "duplicate": duplicate or bool(getattr(document, "duplicate", False)),
    }


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _cursor(offset: int, fingerprint: str) -> str:
    payload = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _offset(value: str | None, fingerprint: str) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("f") != fingerprint:
            raise ValueError
        offset = int(payload["o"])
        if offset < 0 or offset > 100_000:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def _fingerprint(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(part or "") for part in parts).encode()).hexdigest()


def _locator_payload(locator: str) -> dict[str, object]:
    kind, _, ordinal = locator.partition(":")
    try:
        value = max(1, int(ordinal))
    except ValueError:
        value = 1
    if kind == "page":
        return {"kind": "page", "pageNumber": value}
    return {"kind": "section", "sectionOrdinal": value, "heading": None}


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def create_upload_session(
    request: Request,
    body: CreateDocumentUploadRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    try:
        upload = await _service(request).create_upload_session(
            *_owner(session),
            filename=body.filename,
            media_type=body.media_type,
            byte_count=body.byte_count,
            idempotency_key=str(idempotency_key),
        )
        return {
            "id": str(upload.id),
            "documentId": str(upload.document_id),
            "filename": upload.filename,
            "mediaType": upload.media_type,
            "byteCount": upload.byte_count,
            "status": "uploading",
            "version": upload.version,
            "createdAt": upload.created_at.isoformat(),
            "expiresAt": upload.expires_at.isoformat(),
        }
    except DocumentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/uploads/{upload_id}/content", status_code=status.HTTP_202_ACCEPTED)
async def upload_content(
    request: Request,
    upload_id: UUID,
    response: Response,
    content_length: int = Header(..., alias="Content-Length", ge=1, le=100_000_000),
    content_type: DocumentMediaType = Header(..., alias="Content-Type"),
    expected_version: int = Header(..., alias="X-Expected-Version", ge=1),
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    if request.headers.get("content-length") != str(content_length):
        raise HTTPException(status_code=400, detail="content length is invalid")
    try:
        document = await _service(request).complete_upload(
            upload_id,
            request.stream(),
            issuer=session.principal.issuer,
            subject=session.principal.subject,
            content_type=content_type,
            expected_version=expected_version,
            idempotency_key=str(idempotency_key),
        )
        receipt = _receipt(document)
        return receipt
    except DocumentValidationError as exc:
        message = str(exc)
        code = 413 if "exceed" in message or "length" in message else 415
        raise HTTPException(status_code=code, detail=message) from exc
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="upload session not found") from exc
    except DocumentVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="object storage unavailable") from exc


@router.get("")
async def list_documents(
    request: Request,
    response: Response,
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
    media_type: DocumentMediaType | None = Query(default=None, alias="mediaType"),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    fingerprint = _fingerprint(_owner(session), status_filter, media_type)
    offset = _offset(cursor, fingerprint)
    items = await _service(request).list(
        *_owner(session),
        limit=offset + limit + 1,
        status=status_filter,
        media_type=media_type,
    )
    page = items[offset : offset + limit]
    next_cursor = (
        _cursor(offset + limit, fingerprint) if len(items) > offset + limit else None
    )
    return {"items": [_payload(item) for item in page], "nextCursor": next_cursor}


@router.post("/search")
async def search_documents(
    request: Request,
    body: DocumentSearchRequest,
    response: Response,
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    fingerprint = _fingerprint(_owner(session), body.query, body.media_type)
    offset = _offset(body.cursor, fingerprint)
    hits = await _service(request).search(
        *_owner(session),
        body.query,
        limit=offset + body.limit + 1,
        media_type=body.media_type,
    )
    page = hits[offset : offset + body.limit]
    return {
        "items": [
            {
                "evidenceId": str(item.evidence_id),
                "documentId": str(item.document_id),
                "displayName": item.filename,
                "excerpt": item.excerpt,
                "locator": _locator_payload(item.locator),
                "score": item.score,
            }
            for item in page
        ],
        "nextCursor": (
            _cursor(offset + body.limit, fingerprint)
            if len(hits) > offset + body.limit
            else None
        ),
    }


@router.get("/{document_id}")
async def inspect_document(
    request: Request,
    document_id: UUID,
    response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail_payload(await _service(request).get(*_owner(session), document_id))
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/{document_id}/preview")
async def preview_document(
    request: Request,
    document_id: UUID,
    response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    """Return a bounded plain-text preview without touching object storage."""

    _no_store(response)
    try:
        preview = await _service(request).preview(
            session.principal.issuer,
            session.principal.subject,
            document_id,
            context=ProviderTraceContext(
                trace_id=uuid4().hex,
                span_id=uuid4().hex[:16],
            ),
        )
        return {
            "documentId": str(preview.document_id),
            "text": preview.text,
            "truncated": preview.truncated,
            "totalCharacters": preview.total_characters,
        }
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except DocumentValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentRepositoryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="document preview unavailable",
            headers={"Cache-Control": "no-store"},
        ) from exc


@router.get("/{document_id}/content")
async def download_document(
    request: Request,
    document_id: UUID,
    session: Session = Depends(require_session),
) -> Response:
    try:
        document = await _service(request).get(*_owner(session), document_id)
        stream = await _service(request).download(*_owner(session), document_id)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = None
        except DocumentObjectStoreError as exc:
            raise HTTPException(status_code=503, detail="object storage unavailable") from exc

        async def body() -> AsyncIterator[bytes]:
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk

        safe_name = document.filename.replace("\r", "").replace("\n", "").replace('"', "'")
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "document"
        ascii_name = ascii_name.replace("\\", "_")
        return StreamingResponse(
            body(),
            media_type=document.media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(safe_name, safe='')}"
                ),
                "Content-Length": str(document.byte_count),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except DocumentObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="object storage unavailable") from exc


@router.post("/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_document(
    request: Request,
    document_id: UUID,
    body: DocumentTransitionRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail_payload(
            await _service(request).retry(
                *_owner(session),
                document_id,
                expected_version=body.expected_version,
                idempotency_key=str(idempotency_key),
            )
        )
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{document_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_document(
    request: Request,
    document_id: UUID,
    body: DocumentTransitionRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _no_store(response)
    try:
        document = await _service(request).delete(
            *_owner(session),
            document_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
        return {
            "documentId": str(document.id),
            "status": "deleting",
            "version": document.version,
            "acceptedAt": document.updated_at.isoformat(),
        }
    except DocumentVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
