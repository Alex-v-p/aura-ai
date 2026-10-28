"""Thin owner-authorized transport for generated text artifacts."""

# ruff: noqa: B008, B009, E501

from __future__ import annotations

from typing import Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifact,
    GeneratedArtifactConflict,
    GeneratedArtifactLifecycle,
    GeneratedArtifactNotFound,
    GeneratedArtifactService,
    GeneratedArtifactStorageError,
    safe_filename,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/artifacts", tags=["Artifacts"])
ArtifactMediaType = Literal["text/markdown; charset=utf-8", "text/plain; charset=utf-8", "application/json"]


class VersionedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _service(request: Request) -> GeneratedArtifactService:
    return cast(GeneratedArtifactService, getattr(cast(AppState, request.app.state.aura), "artifact_service"))


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _lineage(item: object) -> dict[str, object]:
    lineage = getattr(item, "lineage")
    return {
        "taskId": str(lineage.task_id) if lineage.task_id else None,
        "runId": str(lineage.run_id) if lineage.run_id else None,
        "conversationId": str(lineage.conversation_id) if lineage.conversation_id else None,
        "workspaceId": str(lineage.workspace_id) if lineage.workspace_id else None,
    }


def _capabilities(item: object) -> dict[str, bool]:
    lifecycle = getattr(getattr(item, "lifecycle"), "value", getattr(item, "lifecycle"))
    active = lifecycle == GeneratedArtifactLifecycle.ACTIVE.value
    return {
        "canPreview": active,
        "canDownload": active,
        "canDelete": active,
        "canRetryCleanup": lifecycle == GeneratedArtifactLifecycle.DELETION_FAILED.value,
        "canReconcileCleanup": lifecycle == GeneratedArtifactLifecycle.CLEANUP_UNKNOWN.value,
    }


def _integrity(item: object) -> dict[str, object] | None:
    value = getattr(item, "integrity", None)
    if value is None:
        revision = getattr(item, "revision", None)
        if revision is None:
            return None
        return {"revision": revision.revision, "sha256": revision.sha256, "byteCount": revision.byte_count, "createdAt": revision.created_at.isoformat()}
    return {"revision": value.revision, "sha256": value.sha256, "byteCount": value.byte_count, "createdAt": value.created_at.isoformat()}


def _summary(item: object) -> dict[str, object]:
    lifecycle = getattr(getattr(item, "lifecycle"), "value", getattr(item, "lifecycle"))
    active = lifecycle == GeneratedArtifactLifecycle.ACTIVE.value
    revision = getattr(item, "revision", None)
    return {
        "id": str(getattr(item, "id")),
        "kind": getattr(item, "kind"),
        "title": getattr(item, "title") if active else None,
        "mediaType": revision.media_type if active and revision is not None else None,
        "contentState": lifecycle,
        "currentRevision": getattr(item, "current_revision"),
        "version": getattr(item, "version"),
        "capabilities": _capabilities(item),
        "createdAt": getattr(item, "created_at").isoformat(),
        "updatedAt": getattr(item, "updated_at").isoformat(),
        **({
            "lineage": _lineage(item),
            "integrity": _integrity(item),
            "deletionAuditId": str(getattr(item, "deletion_audit_id")) if getattr(item, "deletion_audit_id", None) else None,
        } if not active else {}),
    }


def _detail(item: object) -> dict[str, object]:
    result = _summary(item)
    lifecycle = cast(str, result["contentState"])
    result["lineage"] = _lineage(item)
    if lifecycle == GeneratedArtifactLifecycle.ACTIVE.value:
        revision = getattr(item, "revision", None)
        result["revision"] = revision.metadata() if revision is not None else None
    else:
        result["integrity"] = _integrity(item)
        result["deletionAuditId"] = str(getattr(item, "deletion_audit_id")) if getattr(item, "deletion_audit_id", None) else None
    return result


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, GeneratedArtifactNotFound):
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    if isinstance(exc, GeneratedArtifactConflict):
        raise HTTPException(status_code=409, detail="artifact state conflict") from exc
    if isinstance(exc, GeneratedArtifactStorageError):
        raise HTTPException(status_code=503, detail="artifact storage unavailable") from exc
    raise HTTPException(status_code=422, detail="invalid artifact request") from exc


@router.get("")
async def list_artifacts(
    request: Request,
    response: Response,
    media_type: ArtifactMediaType | None = Query(default=None, alias="mediaType"),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=25, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    values: list[GeneratedArtifact] = []
    try:
        values = await _service(request).list(*_owner(session), limit=limit + 1, media_type=media_type, offset=offset)
    except Exception as exc:
        _raise(exc)
    next_cursor = str(offset + limit) if len(values) > limit else None
    return {"items": [_summary(item) for item in values[:limit]], "nextCursor": next_cursor}


@router.get("/{artifact_id}")
async def get_artifact(request: Request, artifact_id: UUID, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail(await _service(request).get(*_owner(session), artifact_id))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/{artifact_id}/revisions")
async def list_revisions(request: Request, artifact_id: UUID, response: Response, session: Session = Depends(require_session)) -> list[dict[str, object]]:
    _no_store(response)
    try:
        return [item.metadata() for item in await _service(request).revisions(*_owner(session), artifact_id)]
    except Exception as exc:
        _raise(exc)
    return []


@router.get("/{artifact_id}/preview")
async def preview_artifact(request: Request, artifact_id: UUID, response: Response, revision: int | None = Query(default=None, ge=1), session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        _, selected, text, truncated = await _service(request).preview_with_revision(*_owner(session), artifact_id, revision)
        return {"artifactId": str(artifact_id), "revision": selected.revision, "mediaType": selected.media_type, "text": text, "truncated": truncated}
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/{artifact_id}/content")
async def download_artifact(request: Request, artifact_id: UUID, response: Response, revision: int | None = Query(default=None, ge=1), session: Session = Depends(require_session)) -> StreamingResponse:
    _no_store(response)
    try:
        item, selected, content = await _service(request).content(*_owner(session), artifact_id, revision)
    except Exception as exc:
        _raise(exc)
    output = StreamingResponse(iter((content,)), media_type=selected.media_type)
    output.headers["Cache-Control"] = "no-store"
    output.headers["X-Content-Type-Options"] = "nosniff"
    output.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(item.title, selected.media_type, selected.revision)}"'
    return output


@router.post("/{artifact_id}/deletions", status_code=status.HTTP_202_ACCEPTED)
async def delete_artifact(request: Request, artifact_id: UUID, body: VersionedCommand, response: Response, idempotency_key: UUID = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail(await _service(request).admit_delete(*_owner(session), artifact_id, expected_version=body.expected_version, idempotency_key=str(idempotency_key)))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{artifact_id}/deletions/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_cleanup(request: Request, artifact_id: UUID, body: VersionedCommand, response: Response, idempotency_key: UUID = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail(await _service(request).admit_retry_cleanup(*_owner(session), artifact_id, expected_version=body.expected_version, idempotency_key=str(idempotency_key)))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{artifact_id}/deletions/reconcile", status_code=status.HTTP_202_ACCEPTED)
async def reconcile_cleanup(request: Request, artifact_id: UUID, body: VersionedCommand, response: Response, idempotency_key: UUID = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        return _detail(await _service(request).admit_reconcile_cleanup(*_owner(session), artifact_id, expected_version=body.expected_version, idempotency_key=str(idempotency_key)))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")
