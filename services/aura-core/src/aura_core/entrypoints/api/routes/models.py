"""Model and memory-model configuration HTTP translations."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.knowledge.memory.public import (
    MemoryIdempotencyConflict,
    MemoryModelApplicationService,
    MemoryNotFound,
    MemoryValidationError,
    MemoryVersionConflict,
)
from aura_core.entrypoints.api.routes.conversations import state
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import new_span_id
from aura_core.runtime.models.ports import ProviderTraceContext

router = APIRouter(prefix="/api/v1/models", tags=["Models"])
memory_router = APIRouter(tags=["Models", "Memories"])


async def require_memory_session(request: Request) -> Session:
    configured = request.app.state.aura
    session = await configured.sessions.get(
        request.cookies.get(configured.settings.session_cookie.name)
    )
    if session is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if session.principal.issuer != configured.settings.oidc_issuer:
        raise HTTPException(status_code=403, detail="owner authorization required")
    if session.principal.subject != configured.settings.owner_subject:
        raise HTTPException(status_code=404, detail="memory model configuration not found")
    return session


class UpdateMemoryModelConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    extraction_model_id: str = Field(min_length=1, max_length=255, alias="extractionModelId")
    embedding_model_id: str = Field(min_length=1, max_length=255, alias="embeddingModelId")
    expected_version: int = Field(ge=1, alias="expectedVersion")


class ResumeMemoryReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    generation_id: UUID = Field(alias="generationId")


def _selection(model_id: str, revision: str | None, digest: str | None) -> dict[str, object]:
    return {
        "modelId": model_id,
        "modelRevision": revision,
        "modelDigest": digest,
    }


def _model_service(request: Request) -> MemoryModelApplicationService:
    return state(request).memory_model_service


def _generation_payload(item: Any) -> dict[str, object]:
    return {
        "id": str(item.id),
        "generation": item.generation,
        "model": _selection(
            item.model_id, item.model_revision, getattr(item, "model_digest", None)
        ),
        "dimension": item.dimension,
        "status": item.status,
        "createdAt": item.created_at.isoformat(),
        "activatedAt": item.activated_at.isoformat() if item.activated_at else None,
    }


async def _configuration_payload(request: Request, issuer: str, subject: str) -> dict[str, object]:
    snapshot = await _model_service(request).configuration_snapshot(issuer, subject)
    configuration = snapshot.configuration
    extraction = snapshot.extraction_model
    embedding = snapshot.embedding_model
    active = snapshot.active_generation
    building = snapshot.building_generation
    return {
        "version": configuration.version,
        "extraction": _selection(
            configuration.extraction_model_id,
            configuration.extraction_model_revision,
            extraction.model_digest if extraction else None,
        ),
        "embedding": _selection(
            configuration.embedding_model_id,
            configuration.embedding_model_revision,
            embedding.model_digest
            if embedding
            else getattr(building or active, "model_digest", None),
        ),
        "activeGeneration": _generation_payload(active) if active else None,
        "buildingGeneration": _generation_payload(building) if building else None,
        "updatedAt": datetime.now(UTC).isoformat(),
    }


@router.get("")
async def list_models(
    request: Request, session: Session = Depends(require_session)
) -> dict[str, object]:
    del session
    models, default = await state(request).models()
    return {
        "models": [
            {
                "id": model.id,
                "displayName": model.display_name,
                "provider": model.provider,
                "capabilities": list(model.capabilities),
                "availability": model.availability,
                "selectable": model.selectable,
                "disabledReason": model.disabled_reason,
            }
            for model in models
        ],
        "defaultModelId": default,
        "observedAt": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }


@memory_router.get("/api/v1/memory-model-inventory")
async def get_memory_model_inventory(
    request: Request, session: Session = Depends(require_session)
) -> dict[str, object]:
    del session
    try:
        models = await _model_service(request).inventory()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory model inventory unavailable") from exc
    items: list[dict[str, object]] = []
    for model in models:
        if model.availability != "available" or not model.selectable:
            continue
        capabilities = set(model.capabilities)
        normalized = [
            capability
            for capability in ("structured_output", "embedding")
            if capability in capabilities
        ]
        if not normalized:
            continue
        items.append(
            {
                "id": model.id,
                "displayName": model.display_name,
                "provider": model.provider,
                "modelRevision": model.model_revision,
                "modelDigest": model.model_digest,
                "capabilities": normalized,
                "dimension": model.dimension,
                "available": True,
                "disabledReason": model.disabled_reason,
            }
        )
    return {"models": items, "observedAt": datetime.now(UTC).isoformat()}


@memory_router.get("/api/v1/memory-model-configuration")
async def get_memory_model_configuration(
    request: Request, session: Session = Depends(require_memory_session)
) -> dict[str, object]:
    try:
        return await _configuration_payload(
            request, session.principal.issuer, session.principal.subject
        )
    except MemoryNotFound as exc:
        raise HTTPException(status_code=404, detail="memory model configuration not found") from exc


@memory_router.put("/api/v1/memory-model-configuration")
async def update_memory_model_configuration(
    request: Request,
    body: UpdateMemoryModelConfigurationRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        trace = ProviderTraceContext(
            trace_id=getattr(request.state, "memory_trace_id", uuid4().hex),
            span_id=getattr(request.state, "memory_span_id", new_span_id()),
        )
        await _model_service(request).save_configuration(
            session.principal.issuer,
            session.principal.subject,
            expected_version=body.expected_version,
            extraction_model_id=body.extraction_model_id,
            embedding_model_id=body.embedding_model_id,
            idempotency_key=idempotency_key,
            trace=trace,
        )
        return await _configuration_payload(
            request, session.principal.issuer, session.principal.subject
        )
    except HTTPException:
        raise
    except MemoryVersionConflict as exc:
        raise HTTPException(
            status_code=409, detail="memory model configuration version conflict"
        ) from exc
    except MemoryIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except MemoryValidationError as exc:
        raise HTTPException(status_code=422, detail="memory model configuration rejected") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail="memory model configuration rejected") from exc


@memory_router.get("/api/v1/memory-reindex")
async def get_memory_reindex_status(
    request: Request, session: Session = Depends(require_memory_session)
) -> dict[str, object]:
    issuer, subject = session.principal.issuer, session.principal.subject
    snapshot = await _model_service(request).status(issuer, subject)
    active, replacement = snapshot.active_generation, snapshot.replacement_generation
    phase = "running" if replacement else "idle"
    return {
        "phase": phase,
        "activeGeneration": _generation_payload(active) if active else None,
        "replacementGeneration": _generation_payload(replacement) if replacement else None,
        "processedRevisionCount": snapshot.processed_revision_count,
        "totalRevisionCount": snapshot.total_revision_count,
        "startedAt": replacement.created_at.isoformat() if replacement else None,
        "updatedAt": datetime.now(UTC).isoformat(),
        "completedAt": None,
        "retryable": False,
    }


@memory_router.post("/api/v1/memory-reindex/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_memory_reindex(
    request: Request,
    body: ResumeMemoryReindexRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        await _model_service(request).resume(
            session.principal.issuer,
            session.principal.subject,
            body.generation_id,
            idempotency_key,
        )
    except MemoryIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except MemoryNotFound as exc:
        raise HTTPException(status_code=404, detail="embedding generation not found") from exc
    except MemoryVersionConflict as exc:
        raise HTTPException(
            status_code=409, detail="embedding generation is not resumable"
        ) from exc
    except MemoryValidationError as exc:
        raise HTTPException(status_code=422, detail="memory reindex could not be resumed") from exc
    return await get_memory_reindex_status(request, session)
