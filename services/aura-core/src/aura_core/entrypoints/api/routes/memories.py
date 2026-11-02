"""Owner-authenticated HTTP translation for durable memories."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.knowledge.memory.public import (
    CandidateState,
    MemoryAction,
    MemoryCandidate,
    MemoryCollectionScopeType,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryNotFound,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryRecord,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemorySensitivity,
    MemoryValidationError,
    MemoryVersionConflict,
    decode_memory_cursor,
    encode_memory_cursor,
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


class SearchMemoriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    query: str = Field(min_length=1, max_length=500)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=30, ge=1, le=100)
    kind: MemoryKind | None = None
    scope_type: MemoryCollectionScopeType | None = Field(default=None, alias="scopeType")
    agent_profile_id: UUID | None = Field(default=None, alias="agentProfileId")
    status: MemoryLifecycleStatus | None = None
    provenance_type: str | None = Field(default=None, alias="provenanceType", max_length=64)
    confidence_min: float | None = Field(default=None, alias="confidenceMin", ge=0, le=1)
    confidence_max: float | None = Field(default=None, alias="confidenceMax", ge=0, le=1)
    created_from: datetime | None = Field(default=None, alias="createdFrom")
    created_to: datetime | None = Field(default=None, alias="createdTo")
    include_historical: bool = Field(default=False, alias="includeHistorical")


class CandidateEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    content: str = Field(min_length=1, max_length=32768)
    action: MemoryAction
    kind: MemoryKind
    scope: ScopeRequest
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    half_life_days: float = Field(ge=0.25, le=3650, alias="halfLifeDays")
    valid_to: datetime | None = Field(default=None, alias="validTo")
    related_memory_id: UUID | None = Field(default=None, alias="relatedMemoryId")


class CandidateApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(ge=1, alias="expectedVersion")
    edit: CandidateEditRequest | None = None


class CandidateRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(ge=1, alias="expectedVersion")
    reason: str = Field(min_length=1, max_length=500)


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
        # Keep idempotent mutation receipts byte-stable while exposing the
        # bounded, current relevance value to clients.
        "currentRelevance": round(item.relevance(datetime.now(UTC)), 6),
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
                    "type": source.source_type
                    if source.source_type
                    in {"manual", "conversation_message", "run", "system", "import"}
                    else "system",
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
                    "generationId": str(
                        embedding.generation_id or uuid5(item.id, str(embedding.generation))
                    ),
                    "modelId": embedding.model_id,
                    "modelRevision": embedding.model_revision,
                    "digest": embedding.digest
                    if len(embedding.digest) == 64
                    else uuid5(item.id, embedding.digest).hex,
                    "dimension": embedding.dimension,
                    "modelDigest": embedding.model_digest,
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
                    "activatedAt": generation.activated_at.isoformat()
                    if generation.activated_at
                    else None,
                }
                for generation in item.embedding_generations
            ],
            "relations": [
                {
                    "type": relation.relation,
                    "memoryId": str(relation.memory_id),
                    "createdAt": relation.created_at.isoformat(),
                }
                for relation in item.relations
            ],
        }
    )
    return payload


async def _candidate_payload(
    request: Request, item: MemoryCandidate, *, detail: bool = False
) -> dict[str, object]:
    repository = _state(request).memory_repository
    job = None
    loader = getattr(repository, "get_processing_job", None)
    if callable(loader):
        try:
            typed_loader = cast(Callable[..., Awaitable[object]], loader)
            job = await typed_loader(item.job_id, item.issuer, item.subject)
        except Exception:
            job = None
    payload: dict[str, object] = {
        "id": str(item.id),
        "jobId": str(item.job_id),
        "runId": str(getattr(job, "run_id", item.job_id)),
        "version": item.version,
        "action": item.action.value,
        "state": item.state.value,
        "content": item.content,
        "kind": item.kind.value if item.kind else None,
        "scope": _scope_payload(item.scope) if item.scope else None,
        "confidence": item.confidence,
        "importance": item.importance,
        "halfLifeDays": item.half_life_days,
        "validTo": item.valid_to.isoformat() if item.valid_to else None,
        "sensitivity": item.sensitivity.value,
        "relatedMemoryId": str(item.related_memory_id) if item.related_memory_id else None,
        "memoryId": str(item.memory_id) if item.memory_id else None,
        "decisionReason": item.decision_reason,
        "createdAt": item.created_at.isoformat(),
        "decidedAt": item.decided_at.isoformat() if item.decided_at else None,
    }
    if detail:
        payload["groundedMessageIds"] = [str(value) for value in item.grounded_message_ids]
    return payload


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _validate_scope(scope_type: MemoryScopeType | None, agent_profile_id: UUID | None) -> None:
    if scope_type is None:
        if agent_profile_id is not None:
            raise HTTPException(422, "agentProfileId requires an agent scope")
        return
    if scope_type is MemoryScopeType.AGENT and agent_profile_id is None:
        raise HTTPException(422, "agent scope requires agentProfileId")
    if scope_type is MemoryScopeType.USER and agent_profile_id is not None:
        raise HTTPException(422, "user scope cannot have agentProfileId")


def _validate_collection_scope(
    scope_type: MemoryCollectionScopeType | None, agent_profile_id: UUID | None
) -> None:
    """Validate collection selectors while preserving omission=user semantics."""

    effective = scope_type or MemoryCollectionScopeType.USER
    # Collection-level agent scope without an ID intentionally selects all
    # owner-authorized agent scopes; detail and mutation routes still use the
    # stricter MemoryScopeType validator below.
    if effective in (MemoryCollectionScopeType.USER, MemoryCollectionScopeType.ALL) and (
        agent_profile_id is not None
    ):
        raise HTTPException(422, "agentProfileId requires an agent scope")


def _filter_fingerprint(values: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(values, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _memory_page(
    values: list[MemoryRecord],
    *,
    issuer: str,
    subject: str,
    fingerprint: str,
    limit: int,
) -> dict[str, object]:
    has_more = len(values) > limit
    page = values[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_memory_cursor(
            issuer, subject, fingerprint, last.updated_at, last.id
        )
    return {"items": [_summary(item) for item in page], "nextCursor": next_cursor}


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFound):
        return HTTPException(404, "memory not found")
    if isinstance(exc, (MemoryVersionConflict, MemoryIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, MemoryScopeAuthorizationRequired):
        return HTTPException(403, "memory scope is not authorized")
    if isinstance(exc, MemoryPurgeConfirmationRequired):
        return HTTPException(400, str(exc))
    if isinstance(exc, (MemoryValidationError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "memory operation failed")


@router.get("")
async def list_memories(
    request: Request,
    kind: MemoryKind | None = Query(default=None),
    scope_type: MemoryCollectionScopeType | None = Query(default=None, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    status_filter: MemoryLifecycleStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    provenance_type: str | None = Query(default=None, alias="provenanceType", max_length=64),
    confidence_min: float | None = Query(default=None, alias="confidenceMin", ge=0, le=1),
    confidence_max: float | None = Query(default=None, alias="confidenceMax", ge=0, le=1),
    created_from: datetime | None = Query(default=None, alias="createdFrom"),
    created_to: datetime | None = Query(default=None, alias="createdTo"),
    include_historical: bool = Query(default=False, alias="includeHistorical"),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _validate_collection_scope(scope_type, agent_profile_id)
    effective_scope = scope_type or MemoryCollectionScopeType.USER
    repository_scope = (
        None
        if effective_scope is MemoryCollectionScopeType.ALL
        else MemoryScopeType(effective_scope.value)
    )
    include_all_scopes = effective_scope in {
        MemoryCollectionScopeType.ALL,
        MemoryCollectionScopeType.AGENT,
    }
    filters = {
        "kind": kind.value if kind else None,
        "scopeType": effective_scope.value,
        "agentProfileId": str(agent_profile_id) if agent_profile_id else None,
        "status": status_filter.value if status_filter else None,
        "provenanceType": provenance_type,
        "confidenceMin": confidence_min,
        "confidenceMax": confidence_max,
        "createdFrom": created_from,
        "createdTo": created_to,
        "includeHistorical": include_historical,
    }
    fingerprint = _filter_fingerprint(filters)
    cursor_updated_at = cursor_id = None
    if cursor is not None:
        try:
            cursor_updated_at, cursor_id = decode_memory_cursor(
                cursor, session.principal.issuer, session.principal.subject, fingerprint
            )
        except Exception as exc:
            raise _error(exc) from exc
    try:
        values = await _state(request).memory_repository.list_memories(
            session.principal.issuer,
            session.principal.subject,
            MemoryFilters(
                kind,
                repository_scope,
                agent_profile_id,
                status_filter,
                None,
                include_historical,
                limit + 1,
                provenance_type=provenance_type,
                confidence_min=confidence_min,
                confidence_max=confidence_max,
                created_from=created_from,
                created_to=created_to,
                include_all_scopes=include_all_scopes,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            ),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _memory_page(
        values,
        issuer=session.principal.issuer,
        subject=session.principal.subject,
        fingerprint=fingerprint,
        limit=limit,
    )


@router.post("/search")
async def search_memories(
    request: Request,
    body: SearchMemoriesRequest,
    session: Session = Depends(require_csrf),
) -> dict[str, object]:
    _validate_collection_scope(body.scope_type, body.agent_profile_id)
    effective_scope = body.scope_type or MemoryCollectionScopeType.USER
    repository_scope = (
        None
        if effective_scope is MemoryCollectionScopeType.ALL
        else MemoryScopeType(effective_scope.value)
    )
    include_all_scopes = effective_scope in {
        MemoryCollectionScopeType.ALL,
        MemoryCollectionScopeType.AGENT,
    }
    filters = body.model_dump(by_alias=True)
    filters.pop("query", None)
    filters["scopeType"] = effective_scope.value
    fingerprint = _filter_fingerprint({**filters, "queryDigest": sha256(body.query.encode()).hexdigest()})
    cursor_updated_at = cursor_id = None
    if body.cursor is not None:
        try:
            cursor_updated_at, cursor_id = decode_memory_cursor(
                body.cursor, session.principal.issuer, session.principal.subject, fingerprint
            )
        except Exception as exc:
            raise _error(exc) from exc
    try:
        values = await _state(request).memory_repository.list_memories(
            session.principal.issuer,
            session.principal.subject,
            MemoryFilters(
                body.kind,
                repository_scope,
                body.agent_profile_id,
                body.status,
                body.query,
                body.include_historical,
                body.limit + 1,
                provenance_type=body.provenance_type,
                confidence_min=body.confidence_min,
                confidence_max=body.confidence_max,
                created_from=body.created_from,
                created_to=body.created_to,
                include_all_scopes=include_all_scopes,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            ),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _memory_page(
        values,
        issuer=session.principal.issuer,
        subject=session.principal.subject,
        fingerprint=fingerprint,
        limit=body.limit,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    request: Request,
    body: MemoryCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).memory_repository.create_memory(
            session.principal.issuer,
            session.principal.subject,
            content=body.content,
            kind=body.kind,
            scope=MemoryScope(body.scope.type, body.scope.agent_profile_id),
            provenance=[
                MemoryProvenance(
                    uuid4(), "manual", observed_at=body.observed_at or datetime.now(UTC)
                )
            ],
            confidence=body.confidence,
            importance=body.importance,
            half_life_days=body.half_life_days,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            observed_at=body.observed_at,
            idempotency_key=str(idempotency_key),
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
            session.principal.issuer,
            session.principal.subject,
            memory_id,
            scope_type=scope_type,
            agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.post("/{memory_id}/revisions", status_code=status.HTTP_201_CREATED)
async def correct_memory(
    request: Request,
    memory_id: UUID,
    body: MemoryCorrectionRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.revise_memory(
            session.principal.issuer,
            session.principal.subject,
            memory_id,
            content=body.content,
            reason=body.reason,
            kind=body.kind,
            expected_version=body.expected_version,
            confidence=body.confidence,
            importance=body.importance,
            half_life_days=body.half_life_days,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            observed_at=body.observed_at,
            idempotency_key=str(idempotency_key),
            provenance=[
                MemoryProvenance(
                    uuid4(), "manual", observed_at=body.observed_at or datetime.now(UTC)
                )
            ],
            scope_type=scope_type,
            agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.patch("/{memory_id}/status")
async def update_memory_status(
    request: Request,
    memory_id: UUID,
    body: MemoryStatusRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.set_status(
            session.principal.issuer,
            session.principal.subject,
            memory_id,
            status=body.status,
            related_memory_id=body.related_memory_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            scope_type=scope_type,
            agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.patch("/{memory_id}/pin")
async def pin_memory(
    request: Request,
    memory_id: UUID,
    body: MemoryPinRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        item = await _state(request).memory_repository.set_pinned(
            session.principal.issuer,
            session.principal.subject,
            memory_id,
            pinned=body.pinned,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            scope_type=scope_type,
            agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _detail(item)


@router.post("/{memory_id}/purge")
async def purge_memory(
    request: Request,
    memory_id: UUID,
    body: MemoryPurgeRequest,
    scope_type: MemoryScopeType = Query(default=MemoryScopeType.USER, alias="scopeType"),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _validate_scope(scope_type, agent_profile_id)
    try:
        audit = await _state(request).memory_repository.purge(
            session.principal.issuer,
            session.principal.subject,
            memory_id,
            confirmation=body.confirmation,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            scope_type=scope_type,
            agent_profile_id=agent_profile_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "memoryId": str(audit.memory_id),
        "auditId": str(audit.id),
        "purgedAt": audit.created_at.isoformat(),
    }


candidate_router = APIRouter(prefix="/api/v1/memory-candidates", tags=["Memories"])


@candidate_router.get("")
async def list_memory_candidates(
    request: Request,
    state_filter: CandidateState | None = Query(default=None, alias="state"),
    action: MemoryAction | None = Query(default=None),
    sensitivity: MemorySensitivity | None = Query(default=None),
    run_id: UUID | None = Query(default=None, alias="runId"),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    fingerprint = _filter_fingerprint(
        {
            "state": state_filter.value if state_filter else None,
            "action": action.value if action else None,
            "sensitivity": sensitivity.value if sensitivity else None,
            "runId": str(run_id) if run_id else None,
        }
    )
    cursor_stamp: datetime | None = None
    cursor_id: UUID | None = None
    if cursor is not None:
        try:
            cursor_stamp, cursor_id = decode_memory_cursor(
                cursor, session.principal.issuer, session.principal.subject, fingerprint
            )
        except Exception as exc:
            raise _error(exc) from exc
    try:
        values = await _state(request).memory_repository.list_candidates(
            session.principal.issuer,
            session.principal.subject,
            state=state_filter.value if state_filter else None,
            action=action.value if action else None,
            sensitivity=sensitivity.value if sensitivity else None,
            run_id=run_id,
            limit=100,
            cursor_created_at=cursor_stamp,
            cursor_id=cursor_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    values.sort(key=lambda item: (item.created_at, item.id), reverse=True)
    page = values[:limit]
    next_cursor = None
    if len(values) > limit and page:
        last = page[-1]
        next_cursor = encode_memory_cursor(
            session.principal.issuer,
            session.principal.subject,
            fingerprint,
            last.created_at,
            last.id,
        )
    return {"items": [await _candidate_payload(request, item) for item in page], "nextCursor": next_cursor}


@candidate_router.get("/{candidate_id}")
async def get_memory_candidate(
    request: Request, candidate_id: UUID, session: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        item = await _state(request).memory_repository.get_candidate(
            session.principal.issuer, session.principal.subject, candidate_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return await _candidate_payload(request, item, detail=True)


@candidate_router.post("/{candidate_id}/approve")
async def approve_memory_candidate(
    request: Request,
    candidate_id: UUID,
    body: CandidateApproveRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        edit_body = body.edit
        edit = edit_body.model_dump(by_alias=True) if edit_body else None
        if edit is not None and edit_body is not None:
            edit["scope"] = edit_body.scope.model_dump(by_alias=True)
        item = await _state(request).memory_repository.approve_candidate(
            session.principal.issuer,
            session.principal.subject,
            candidate_id,
            expected_version=body.expected_version,
            edit=edit,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "candidate": await _candidate_payload(request, item, detail=True),
        "activityId": str(uuid5(candidate_id, "approve")),
    }


@candidate_router.post("/{candidate_id}/reject")
async def reject_memory_candidate(
    request: Request,
    candidate_id: UUID,
    body: CandidateRejectRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        item = await _state(request).memory_repository.reject_candidate(
            session.principal.issuer,
            session.principal.subject,
            candidate_id,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {
        "candidate": await _candidate_payload(request, item, detail=True),
        "activityId": str(uuid5(candidate_id, "reject")),
    }


__all__ = ["router", "candidate_router"]
