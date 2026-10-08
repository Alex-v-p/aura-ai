"""Owner-authenticated agent configuration HTTP translation."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from inspect import isawaitable
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.interaction.agents.public import (
    AgentProfile,
    AgentRevision,
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    MemoryPolicy,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32768)
    personaRevisionId: UUID


class AgentRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1, max_length=32768)
    personaRevisionId: UUID
    expectedVersion: int = Field(ge=1)


class AgentStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ConfigurationStatus
    expectedVersion: int = Field(ge=1)


class AgentMemoryPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    shared_user_read: bool = Field(alias="sharedUserRead")
    current_agent_read: bool = Field(alias="currentAgentRead")
    shared_user_promotion: bool = Field(alias="sharedUserPromotion")
    fallback_relevance_threshold: float = Field(ge=0, le=1, alias="fallbackRelevanceThreshold")
    max_memories: int = Field(ge=1, le=8, alias="maxMemories")
    context_budget_fraction: float = Field(gt=0, le=0.2, alias="contextBudgetFraction")
    fallback_agent_profile_ids: list[UUID] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, max_length=64, alias="fallbackAgentProfileIds"
    )
    expected_revision: int = Field(ge=1, alias="expectedRevision")


class AttachAgentMemoryPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_agent_version: int = Field(ge=1, alias="expectedAgentVersion")


def state(request: Request) -> AppState:
    return request.app.state.aura


async def agent_call(request: Request, name: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(state(request).agent_service, name)(*args, **kwargs)
    return await result if isawaitable(result) else result


def record_configuration(
    request: Request, operation: str, timer: Stopwatch, profile: AgentProfile, trace_id: str
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
        agent_revision_id=str(profile.current_revision.id),
        agent_revision_number=str(profile.current_revision.revision),
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


def revision_payload(item: AgentRevision) -> dict[str, object]:
    return {
        "id": str(item.id),
        "profileId": str(item.profile_id),
        "revision": item.revision,
        "displayName": item.display_name,
        "purpose": item.purpose,
        "instructions": item.instructions,
        "personaRevisionId": str(item.persona_revision_id),
        "promptBundleRevisionId": str(item.prompt_bundle_revision_id),
        "modelPolicyRevisionId": str(item.model_policy_revision_id),
        "createdAt": item.created_at.isoformat(),
    }


def profile_payload(profile: AgentProfile) -> dict[str, object]:
    return {
        "id": str(profile.id),
        "status": profile.status.value,
        "version": profile.version,
        "currentRevision": revision_payload(profile.current_revision),
        "createdAt": profile.created_at.isoformat(),
        "updatedAt": profile.updated_at.isoformat(),
        "revisions": [revision_payload(item) for item in profile.revisions],
    }


def summary_payload(profile: AgentProfile) -> dict[str, object]:
    payload = profile_payload(profile)
    payload.pop("revisions")
    return payload


def memory_policy_payload(item: MemoryPolicy) -> dict[str, object]:
    return {
        "id": str(item.id),
        "agentProfileId": str(item.agent_profile_id),
        "revision": item.revision,
        "sharedUserRead": item.shared_user_read,
        "currentAgentRead": item.current_agent_read,
        "sharedUserPromotion": item.allow_shared_user_promotion,
        "fallbackRelevanceThreshold": item.fallback_relevance_threshold,
        "maxMemories": item.max_memories,
        "contextBudgetFraction": item.context_budget_fraction,
        "fallbackAgentProfileIds": [str(value) for value in item.fallback_agent_profile_ids],
        "createdAt": item.created_at.isoformat(),
    }


@router.get("")
async def list_agents(request: Request, _: Session = Depends(require_session)) -> dict[str, object]:
    return {"items": [summary_payload(item) for item in await agent_call(request, "list_agents")]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    request: Request,
    body: AgentCreateRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        persona_revision_id = body.personaRevisionId
        profile = await agent_call(
            request,
            "create_agent",
            session.principal.issuer,
            session.principal.subject,
            body.displayName.lower().replace(" ", "-"),
            body.displayName,
            body.purpose,
            body.instructions,
            persona_revision_id,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "agent.create", timer, trace_id, exc)
        if isinstance(exc, ConfigurationIdempotencyConflict):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, (ConfigurationDisabled, ConfigurationNotFound)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    record_configuration(request, "agent.create", timer, profile, trace_id)
    return profile_payload(profile)


@router.get("/{agent_profile_id}")
async def get_agent(
    request: Request, agent_profile_id: UUID, _: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        return profile_payload(await agent_call(request, "get_agent", agent_profile_id))
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=404, detail="agent not found") from exc


@router.post("/{agent_profile_id}/revisions", status_code=status.HTTP_201_CREATED)
async def revise_agent(
    request: Request,
    agent_profile_id: UUID,
    body: AgentRevisionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        profile = await agent_call(
            request,
            "revise_agent",
            session.principal.issuer,
            session.principal.subject,
            agent_profile_id,
            body.expectedVersion,
            body.displayName,
            body.purpose,
            body.instructions,
            body.personaRevisionId,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "agent.revise", timer, trace_id, exc)
        if isinstance(
            exc,
            ConfigurationIdempotencyConflict | ConfigurationVersionConflict | ConfigurationDisabled,
        ):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, ConfigurationNotFound):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise
    record_configuration(request, "agent.revise", timer, profile, trace_id)
    return profile_payload(profile)


@router.patch("/{agent_profile_id}")
async def update_agent_status(
    request: Request,
    agent_profile_id: UUID,
    body: AgentStatusRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        profile = await agent_call(
            request,
            "set_status",
            session.principal.issuer,
            session.principal.subject,
            agent_profile_id,
            body.expectedVersion,
            body.status,
            str(idempotency_key),
        )
    except Exception as exc:
        record_configuration_failure(request, "agent.status", timer, trace_id, exc)
        if isinstance(exc, (ConfigurationIdempotencyConflict, ConfigurationVersionConflict)):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, ConfigurationNotFound):
            raise HTTPException(status_code=404, detail="agent not found") from exc
        raise
    record_configuration(request, "agent.status", timer, profile, trace_id)
    return profile_payload(profile)


@router.get("/{agent_profile_id}/memory-policies")
async def list_agent_memory_policies(
    request: Request, agent_profile_id: UUID, session: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        profile = await agent_call(request, "get_agent", agent_profile_id)
        policies = await agent_call(
            request,
            "list_memory_policies",
            session.principal.issuer,
            session.principal.subject,
            agent_profile_id,
        )
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=404, detail="agent not found") from exc
    return {
        "items": [memory_policy_payload(item) for item in policies],
        "attachedPolicyRevisionId": str(profile.current_revision.memory_policy_revision_id),
        "agentVersion": profile.version,
    }


@router.post("/{agent_profile_id}/memory-policies", status_code=status.HTTP_201_CREATED)
async def create_agent_memory_policy(
    request: Request,
    agent_profile_id: UUID,
    body: AgentMemoryPolicyRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        profile = await agent_call(request, "get_agent", agent_profile_id)
        policy = MemoryPolicy(
            uuid4(),
            agent_profile_id,
            body.expected_revision + 1,
            body.shared_user_read,
            body.current_agent_read,
            body.fallback_relevance_threshold,
            body.max_memories,
            body.context_budget_fraction,
            body.shared_user_promotion,
            tuple(body.fallback_agent_profile_ids),
        )
        result = await agent_call(
            request,
            "create_memory_policy",
            session.principal.issuer,
            session.principal.subject,
            policy,
            str(idempotency_key),
        )
    except ConfigurationVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigurationIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except (ConfigurationNotFound, ValueError) as exc:
        raise HTTPException(status_code=422, detail="memory policy rejected") from exc
    del profile
    return memory_policy_payload(result)


@router.post(
    "/{agent_profile_id}/memory-policies/{policy_revision_id}/attach",
    status_code=status.HTTP_201_CREATED,
)
async def attach_agent_memory_policy(
    request: Request,
    agent_profile_id: UUID,
    policy_revision_id: UUID,
    body: AttachAgentMemoryPolicyRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        result = await agent_call(
            request,
            "attach_memory_policy",
            session.principal.issuer,
            session.principal.subject,
            agent_profile_id,
            policy_revision_id,
            body.expected_agent_version,
            str(idempotency_key),
        )
    except ConfigurationVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=404, detail="agent not found") from exc
    return profile_payload(result)
