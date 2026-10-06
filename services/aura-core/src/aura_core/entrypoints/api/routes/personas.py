"""Owner-authenticated reusable persona configuration HTTP translation."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from inspect import isawaitable
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PersonaProfile,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id

router = APIRouter(prefix="/api/v1/personas", tags=["Personas"])


class PersonaCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32768)


class PersonaRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32768)
    expectedVersion: int = Field(ge=1)


class PersonaStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ConfigurationStatus
    expectedVersion: int = Field(ge=1)


def state(request: Request) -> AppState:
    return request.app.state.aura


async def persona_call(request: Request, name: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(state(request).persona_service, name)(*args, **kwargs)
    return await result if isawaitable(result) else result


def record_configuration(
    request: Request, operation: str, timer: Stopwatch, profile: PersonaProfile, trace_id: str
) -> None:
    request.app.state.aura.metrics.record_span(
        "aura.interaction.agent_configuration",
        "agent.configure",
        timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="configuration_store",
        outcome="ok",
        persona_revision_number=str(profile.current_revision.revision),
    )
    request.app.state.aura.metrics.increment(
        "aura.interaction.agent_configuration",
        "configuration_outcome",
        trace_id=trace_id,
        outcome="ok",
    )


def record_configuration_failure(
    request: Request, operation: str, timer: Stopwatch, trace_id: str, error: Exception
) -> None:
    request.app.state.aura.metrics.record_span(
        "aura.interaction.agent_configuration",
        "agent.configure",
        timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="configuration_store",
        outcome="error",
        error_class=configuration_error_class(error),
    )
    request.app.state.aura.metrics.increment(
        "aura.interaction.agent_configuration",
        "configuration_outcome",
        trace_id=trace_id,
        outcome="error",
    )


def configuration_error_class(error: Exception) -> str:
    if isinstance(error, ConfigurationIdempotencyConflict):
        return "idempotency"
    if isinstance(error, ConfigurationVersionConflict):
        return "conflict"
    if isinstance(error, ConfigurationDisabled):
        return "disabled"
    if isinstance(error, ConfigurationNotFound):
        return "not_found"
    if isinstance(error, (ValueError, TypeError)):
        return "validation"
    return "persistence"


def profile_payload(profile: PersonaProfile) -> dict[str, object]:
    return {
        "id": str(profile.id),
        "status": profile.status.value,
        "version": profile.version,
        "currentRevision": {
            "id": str(profile.current_revision.id),
            "profileId": str(profile.id),
            "revision": profile.current_revision.revision,
            "displayName": profile.current_revision.display_name or profile.display_name,
            "description": profile.current_revision.description,
            "instructions": profile.current_revision.instructions,
            "createdAt": profile.current_revision.created_at.isoformat(),
        },
        "createdAt": profile.created_at.isoformat(),
        "updatedAt": profile.updated_at.isoformat(),
        "revisions": [
            {
                "id": str(item.id),
                "profileId": str(profile.id),
                "revision": item.revision,
                "displayName": item.display_name or profile.display_name,
                "description": item.description,
                "instructions": item.instructions,
                "createdAt": item.created_at.isoformat(),
            }
            for item in profile.revisions
        ],
    }


def summary_payload(profile: PersonaProfile) -> dict[str, object]:
    payload = profile_payload(profile)
    payload.pop("revisions")
    return payload


@router.get("")
async def list_personas(
    request: Request, _: Session = Depends(require_session)
) -> dict[str, object]:
    return {
        "items": [summary_payload(item) for item in await persona_call(request, "list_personas")]
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_persona(
    request: Request,
    body: PersonaCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        profile = await persona_call(
            request,
            "create_persona",
            session.principal.issuer,
            session.principal.subject,
            body.displayName.lower().replace(" ", "-"),
            body.displayName,
            body.description,
            body.instructions,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "persona.create", timer, trace_id, exc)
        if isinstance(exc, ConfigurationIdempotencyConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    record_configuration(request, "persona.create", timer, profile, trace_id)
    return profile_payload(profile)


@router.get("/{persona_profile_id}")
async def get_persona(
    request: Request, persona_profile_id: UUID, _: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        return profile_payload(await persona_call(request, "get_persona", persona_profile_id))
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=404, detail="persona not found") from exc


@router.post("/{persona_profile_id}/revisions", status_code=status.HTTP_201_CREATED)
async def revise_persona(
    request: Request,
    persona_profile_id: UUID,
    body: PersonaRevisionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        profile = await persona_call(
            request,
            "revise_persona",
            session.principal.issuer,
            session.principal.subject,
            persona_profile_id,
            body.expectedVersion,
            body.displayName,
            body.description,
            body.instructions,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "persona.revise", timer, trace_id, exc)
        if isinstance(exc, (ConfigurationIdempotencyConflict, ConfigurationVersionConflict)):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, LookupError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise
    record_configuration(request, "persona.revise", timer, profile, trace_id)
    return profile_payload(profile)


@router.patch("/{persona_profile_id}")
async def update_persona_status(
    request: Request,
    persona_profile_id: UUID,
    body: PersonaStatusRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        profile = await persona_call(
            request,
            "set_status",
            session.principal.issuer,
            session.principal.subject,
            persona_profile_id,
            body.expectedVersion,
            body.status,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "persona.status", timer, trace_id, exc)
        if isinstance(exc, (ConfigurationIdempotencyConflict, ConfigurationVersionConflict)):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, LookupError):
            raise HTTPException(status_code=404, detail="persona not found") from exc
        raise
    record_configuration(request, "persona.status", timer, profile, trace_id)
    return profile_payload(profile)
