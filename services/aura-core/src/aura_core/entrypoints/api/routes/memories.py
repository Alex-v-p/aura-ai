"""Owner-authenticated HTTP translation for durable memories."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.knowledge.memory.public import (
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryNotFound,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryRecord,
    MemoryScope,
    MemoryScopeType,
    MemoryValidationError,
    MemoryVersionConflict,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/memories", tags=["Memories"])


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: MemoryScopeType
    agent_profile_id: UUID | None = Field(default=None, alias="agentProfileId")


class MemoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=32768)
    kind: MemoryKind
    scope: ScopeRequest
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    half_life_days: float = Field(ge=0.25, le=3650, alias="halfLifeDays")
    valid_from: datetime | None = Field(default=None, alias="validFrom")
    valid_to: datetime | None = Field(default=None, alias="validTo")
    observed_at: datetime | None = Field(default=None, alias="observedAt")


class MemoryCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=32768)
    reason: str = Field(min_length=1, max_length=2000)
    kind: MemoryKind | None = None
    expected_version: int = Field(ge=1, alias="expectedVersion")
    confidence: float | None = Field(default=None, ge=0, le=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    half_life_days: float | None = Field(default=None, ge=0.25, le=3650, alias="halfLifeDays")
    valid_from: datetime | None = Field(default=None, alias="validFrom")
    valid_to: datetime | None = Field(default=None, alias="validTo")
    observed_at: datetime | None = Field(default=None, alias="observedAt")


class MemoryStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    status: MemoryLifecycleStatus
    related_memory_id: UUID | None = Field(default=None, alias="relatedMemoryId")
    expected_version: int = Field(ge=1, alias="expectedVersion")


class MemoryPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    pinned: bool
    expected_version: int = Field(ge=1, alias="expectedVersion")


class MemoryPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    confirmation: str
    expected_version: int = Field(ge=1, alias="expectedVersion")


def _scope_payload(scope: MemoryScope) -> dict[str, object]:
    payload: dict[str, object] = {"type": scope.type.value}
    if scope.agent_profile_id is not None:
        payload["agentProfileId"] = str(scope.agent_profile_id)
    return payload


def _revision_payload(item: MemoryRecord, revision: Any) -> dict[str, object]:
    return {
        "id": str(revision.id),
        "memoryId": str(item.id),
        "revision": revision.revision,
        "kind": revision.kind.value,
        "content": revision.content,
        "confidence": revision.confidence,
        "importance": revision.importance,
        "halfLifeDays": revision.half_life_days,
        "observedAt": revision.observed_at.isoformat() if revision.observed_at else None,
        "validFrom": revision.valid_from.isoformat() if revision.valid_from else None,
        "validTo": revision.valid_to.isoformat() if revision.valid_to else None,
        "createdAt": revision.created_at.isoformat(),
        "correctionReason": revision.correction_reason,
    }


def _summary(item: MemoryRecord) -> dict[str, object]:
    revision = item.current_revision
    return {
        "id": str(item.id),
        "scope": _scope_payload(item.scope),
        "status": item.status.value,
        "pinned": item.pinned,
        "currentRevision": _revision_payload(item, revision),
        "version": item.version,
        "reinforcedAt": item.reinforced_at.isoformat() if item.reinforced_at else None,
        "dormantAt": item.dormant_at.isoformat() if item.dormant_at else None,
        "archivedAt": item.archived_at.isoformat() if item.archived_at else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
    }


def _detail(item: MemoryRecord) -> dict[str, object]:
    payload = _summary(item)
    payload.update(
        {
            "revisions": [_revision_payload(item, revision) for revision in item.revisions],
            "provenance": [
                {
                    "id": str(source.id),
                    "memoryRevisionId": str(revision.id),
                    "type": source.source_type if source.source_type in {"manual", "conversation_message", "run", "system", "import"} else "system",
                    "sourceId": str(source.source_id) if source.source_id else None,
                    "sourceContentDigest": source.evidence_digest,
                    "observedAt": source.observed_at.isoformat() if source.observed_at else None,
                    "createdAt": source.created_at.isoformat(),
                    "evidence": source.evidence,
                }
                for revision in item.revisions
                for source in item.provenance
                if source.id in revision.provenance_ids
            ],
            "embeddings": [
                {
                    "id": str(embedding.id),
                    "memoryRevisionId": str(embedding.revision_id),
                    "generationId": str(embedding.generation_id or uuid5(item.id, str(embedding.generation))),
                    "modelId": embedding.model_id,
                    "modelRevision": embedding.model_revision,
                    "digest": embedding.digest if len(embedding.digest) == 64 else uuid5(item.id, embedding.digest).hex,
                    "dimension": embedding.dimension,
                    "generation": embedding.generation,
                    "createdAt": embedding.created_at.isoformat(),
                }
                for embedding in item.embeddings
            ],
            "embeddingGenerations": [
                {
                    "id": str(generation.id),
                    "generation": generation.generation,
                    "modelId": generation.model_id,
                    "modelRevision": generation.model_revision,
                    "dimension": generation.dimension,
                    "status": generation.status,
                    "createdAt": generation.created_at.isoformat(),
                    "activatedAt": generation.activated_at.isoformat() if generation.activated_at else None,
                }
                for generation in item.embedding_generations
            ],
            "relations": [
                {"type": relation.relation, "memoryId": str(relation.memory_id), "createdAt": relation.created_at.isoformat()}
                for relation in item.relations
            ],
        }
    )
    return payload


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _validate_scope(scope_type: MemoryScopeType, agent_profile_id: UUID | None) -> None:
    if scope_type is MemoryScopeType.AGENT and agent_profile_id is None:
        raise HTTPException(422, "agent scope requires agentProfileId")
    if scope_type is MemoryScopeType.USER and agent_profile_id is not None:
        raise HTTPException(422, "user scope cannot have agentProfileId")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFound):
        return HTTPException(404, "memory not found")
    if isinstance(exc, (MemoryVersionConflict, MemoryIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, MemoryPurgeConfirmationRequired):
        return HTTPException(400, str(exc))
    if isinstance(exc, (MemoryValidationError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "memory operation failed")


@router.get("")
async def list_memories(
    request: Request,
    kind: MemoryKind | None = Query(default=None),
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    status_filter: MemoryLifecycleStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=500),
    include_historical: bool = Query(default=False, alias="includeHistorical"),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        values = await _state(request).memory_repository.list_memories(
            session.principal.issuer, session.principal.subject,
            MemoryFilters(kind, scope_type, agent_profile_id, status_filter, q, include_historical, limit),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [_summary(item) for item in values], "nextCursor": None}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    request: Request,
    body: MemoryCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).memory_repository.create_memory(
            session.principal.issuer, session.principal.subject,
            content=body.content, kind=body.kind, scope=MemoryScope(body.scope.type, body.scope.agent_profile_id),
            provenance=[MemoryProvenance(uuid4(), "manual", observed_at=body.observed_at or datetime.now(UTC))],
            confidence=body.confidence, importance=body.importance, half_life_days=body.half_life_days,
            valid_from=body.valid_from, valid_to=body.valid_to, observed_at=body.observed_at, idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.get("/{memory_id}")
async def get_memory(
    request: Request,
    memory_id: UUID,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.get_memory(
            session.principal.issuer, session.principal.subject, memory_id,
            scope_type=scope_type, agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.post("/{memory_id}/revisions", status_code=status.HTTP_201_CREATED)
async def correct_memory(
    request: Request, memory_id: UUID, body: MemoryCorrectionRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.revise_memory(
            session.principal.issuer, session.principal.subject, memory_id, content=body.content,
            reason=body.reason, kind=body.kind, expected_version=body.expected_version, confidence=body.confidence,
            importance=body.importance, half_life_days=body.half_life_days, valid_from=body.valid_from,
            valid_to=body.valid_to, observed_at=body.observed_at, idempotency_key=str(idempotency_key),
            provenance=[MemoryProvenance(uuid4(), "manual", observed_at=body.observed_at or datetime.now(UTC))],
            scope_type=scope_type, agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.patch("/{memory_id}/status")
async def update_memory_status(
    request: Request, memory_id: UUID, body: MemoryStatusRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.set_status(
            session.principal.issuer, session.principal.subject, memory_id, status=body.status,
            related_memory_id=body.related_memory_id, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            scope_type=scope_type, agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.patch("/{memory_id}/pin")
async def pin_memory(
    request: Request, memory_id: UUID, body: MemoryPinRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.set_pinned(
            session.principal.issuer, session.principal.subject, memory_id,
            pinned=body.pinned, expected_version=body.expected_version, idempotency_key=str(idempotency_key),
            scope_type=scope_type, agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.post("/{memory_id}/purge")
async def purge_memory(
    request: Request, memory_id: UUID, body: MemoryPurgeRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        audit = await _state(request).memory_repository.purge(
            session.principal.issuer, session.principal.subject, memory_id,
            confirmation=body.confirmation, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            scope_type=scope_type, agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"memoryId": str(audit.memory_id), "auditId": str(audit.id), "purgedAt": audit.created_at.isoformat()}


__all__ = ["router"]
