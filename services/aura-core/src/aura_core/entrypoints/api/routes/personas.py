"""Owner-authenticated reusable persona configuration HTTP translation."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

import secrets
from inspect import isawaitable
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PersonaPreviewError,
    PersonaProfile,
    PersonaRevision,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id


class _PersonaRoute(APIRoute):
    """Keep preview transport validation inside its generic problem boundary."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:
            started = monotonic()
            try:
                return await original(request)
            except HTTPException as exc:
                if not request.url.path.endswith("/personas/preview"):
                    raise
                if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                    code, detail, retryable = (
                        "UNAUTHORIZED",
                        "authentication required",
                        False,
                    )
                elif exc.status_code == status.HTTP_403_FORBIDDEN:
                    code, detail, retryable = "FORBIDDEN", "preview authorization failed", False
                else:
                    code, detail, retryable = (
                        "DEPENDENCY_UNAVAILABLE",
                        "preview authorization is temporarily unavailable",
                        True,
                    )
                return _preview_problem(
                    request,
                    exc.status_code,
                    code,
                    detail,
                    retryable=retryable,
                )
            except RequestValidationError:
                if request.url.path.endswith("/personas/preview"):
                    _record_preview_boundary_error(
                        request,
                        "validation",
                        max(0.0, (monotonic() - started) * 1000.0),
                        "transport_validation",
                    )
                    return _preview_problem(
                        request,
                        422,
                        "UNPROCESSABLE_ENTITY",
                        "preview request is invalid",
                    )
                raise

        return handler


router = APIRouter(prefix="/api/v1/personas", tags=["Personas"], route_class=_PersonaRoute)


def _preview_problem(
    request: Request, status_code: int, code: str, detail: str, *, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"urn:aura:problem:{code.lower()}",
            "title": code.replace("_", " ").title(),
            "status": status_code,
            "code": code,
            "detail": detail,
            "retryable": retryable,
            "traceId": secrets.token_hex(16),
            "resource": str(request.url.path)[:255],
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
        media_type="application/problem+json",
    )


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


class PersonaPreviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32768)


class PersonaDraftPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelId: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=32768)
    draft: PersonaPreviewDraft


class PersonaRevisionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelId: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=32768)
    personaRevisionId: UUID


def state(request: Request) -> AppState:
    return request.app.state.aura


async def persona_call(request: Request, name: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(state(request).persona_service, name)(*args, **kwargs)
    return await result if isawaitable(result) else result


def _record_preview_boundary_error(
    request: Request, error_class: str, duration_ms: float, dependency: str
) -> None:
    """Record transport failures without capturing request content or identity."""

    metrics = request.app.state.aura.metrics
    trace_id = uuid4().hex
    span_id = new_span_id()
    metrics.increment(
        "aura.interaction.persona_preview",
        "persona_preview_outcome",
        trace_id=trace_id,
        outcome="error",
        error_class=error_class,
        dependency=dependency,
    )
    metrics.increment(
        "aura.interaction.persona_preview",
        "errors",
        trace_id=trace_id,
        outcome="error",
        error_class=error_class,
        dependency=dependency,
    )
    metrics.record_span(
        "aura.interaction.persona_preview",
        "persona.preview",
        duration_ms,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        dependency=dependency,
        outcome="error",
        error_class=error_class,
    )


async def require_preview_csrf(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
) -> Session:
    """Preserve the shared CSRF contract while instrumenting auth failures."""

    started = monotonic()
    try:
        session = await require_session(request)
        return await require_csrf(
            request,
            session=session,
            x_csrf_token=x_csrf_token,
        )
    except HTTPException:
        _record_preview_boundary_error(
            request,
            "authorization",
            max(0.0, (monotonic() - started) * 1000.0),
            "identity_authorization",
        )
        raise


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


def profile_payload(
    profile: PersonaProfile,
    assigned_agents: tuple[dict[str, object], ...] = (),
    assigned_agent_count: int = 0,
) -> dict[str, object]:
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
        "assignedAgentCount": assigned_agent_count,
        "assignedAgents": list(assigned_agents[:50]),
        "assignedAgentsTruncated": assigned_agent_count > 50,
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


def summary_payload(
    profile: PersonaProfile,
    assigned_agents: tuple[dict[str, object], ...] = (),
    assigned_agent_count: int = 0,
) -> dict[str, object]:
    payload = profile_payload(profile, assigned_agents, assigned_agent_count)
    payload.pop("revisions")
    payload.pop("assignedAgents")
    payload.pop("assignedAgentsTruncated")
    return payload


def _assignment_read_model(
    profile: PersonaProfile, agents: list[Any]
) -> tuple[tuple[dict[str, object], ...], int]:
    matches: dict[UUID, dict[str, object]] = {}
    revision_ids = {revision.id for revision in profile.revisions}
    for agent in agents:
        current = agent.current_revision
        if current.persona_revision_id not in revision_ids:
            continue
        matches[agent.id] = {
            "agentProfileId": str(agent.id),
            "displayName": agent.display_name,
            "status": agent.status.value,
            "currentRevisionNumber": current.revision,
        }
    values = tuple(matches.values())
    return values, len(values)


@router.get("")
async def list_personas(
    request: Request, _: Session = Depends(require_session)
) -> dict[str, object]:
    profiles = await persona_call(request, "list_personas")
    agents = await state(request).agent_service.list_agents()
    return {"items": [
        summary_payload(item, *_assignment_read_model(item, agents)) for item in profiles
    ]}


@router.post("/preview", response_model=None)
async def preview_persona(
    request: Request,
    body: PersonaDraftPreviewRequest | PersonaRevisionPreviewRequest,
    response: Response,
    session: Session = Depends(require_preview_csrf),
) -> dict[str, object] | JSONResponse:
    response.headers["Cache-Control"] = "no-store"
    if isinstance(body, PersonaDraftPreviewRequest):
        draft = PersonaRevision(
            id=uuid4(),
            profile_id=uuid4(),
            revision=1,
            description=body.draft.description,
            instructions=body.draft.instructions,
            display_name=body.draft.displayName,
        )
        revision_id = None
    else:
        draft = None
        revision_id = body.personaRevisionId
    try:
        content = await persona_call(
            request,
            "preview",
            session.principal.issuer,
            session.principal.subject,
            body.modelId,
            body.prompt,
            draft=draft,
            persona_revision_id=revision_id,
        )
    except PersonaPreviewError as exc:
        if exc.error_class in {"validation", "capability"}:
            return _preview_problem(
                request, 422, "UNPROCESSABLE_ENTITY", "preview model is unavailable"
            )
        if exc.error_class in {"not_found", "disabled"}:
            return _preview_problem(request, 404, "NOT_FOUND", "persona revision not found")
        return _preview_problem(
            request,
            503,
            "DEPENDENCY_UNAVAILABLE",
            "persona preview is temporarily unavailable",
            retryable=True,
        )
    return {"modelId": body.modelId, "content": content}


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
    agents = await state(request).agent_service.list_agents()
    return profile_payload(profile, *_assignment_read_model(profile, agents))


@router.get("/{persona_profile_id}")
async def get_persona(
    request: Request, persona_profile_id: UUID, _: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        profile = await persona_call(request, "get_persona", persona_profile_id)
        agents = await state(request).agent_service.list_agents()
        return profile_payload(profile, *_assignment_read_model(profile, agents))
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
    agents = await state(request).agent_service.list_agents()
    return profile_payload(profile, *_assignment_read_model(profile, agents))


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
    agents = await state(request).agent_service.list_agents()
    return profile_payload(profile, *_assignment_read_model(profile, agents))
