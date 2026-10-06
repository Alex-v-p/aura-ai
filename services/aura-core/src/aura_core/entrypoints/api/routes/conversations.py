"""Conversation HTTP translations; rules live in the conversation service."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.execution.runs.public import Run, message_payload, run_payload
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentSwitchConfirmationRequired,
    AgentUnavailable,
    Conversation,
    ConversationNotFound,
    IdempotencyConflict,
    ModelUnavailable,
    VersionConflict,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id
from aura_core.runtime.models.ports import ModelDescriptor

router = APIRouter(prefix="/api/v1/conversations", tags=["Conversations"])


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32768)
    modelId: str | None = Field(default=None, min_length=1, max_length=255)
    agentRevisionId: UUID | None = None


class UpdateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelId: str | None = Field(default=None, min_length=1, max_length=255)
    agentRevisionId: UUID | None = None
    version: int = Field(ge=1)
    transcriptSharingConfirmed: bool = False


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32768)
    conversationVersion: int = Field(ge=1)


def state(request: Request) -> AppState:
    return request.app.state.aura


def conversation_payload(
    conversation: Conversation, agents: AgentCatalog | None = None
) -> dict[str, object]:
    current = conversation.current_run
    def agent_reference(profile_id: UUID, revision_id: UUID) -> dict[str, object] | None:
        profile = next(
            (item for item in agents.list_agents() if item.id == profile_id), None
        ) if agents else None
        revision = next(
            (
                item
                for item in profile.revisions
                if item.id == revision_id
            ),
            None,
        ) if profile else None
        if revision is None:
            # Never turn an unresolved identifier into a trusted reference.
            return None
        return {
            "profileId": str(profile_id),
            "revisionId": str(revision_id),
            "displayName": revision.display_name,
            "revision": revision.revision,
            "status": profile.status.value if profile else "active",
            "newerRevisionAvailable": bool(
                profile and any(item.revision > revision.revision for item in profile.revisions)
            ),
        }

    current_reference = agent_reference(
        conversation.agent_profile_id, conversation.agent_revision_id
    )
    payload: dict[str, object] = {
        "id": str(conversation.id),
        "title": conversation.title,
        "agentProfileId": str(conversation.agent_profile_id),
        "agentRevisionId": str(conversation.agent_revision_id),
        "modelId": conversation.model_id,
        "version": conversation.version,
        "createdAt": conversation.created_at.isoformat(),
        "updatedAt": conversation.updated_at.isoformat(),
        "currentRun": run_payload(current) if current else None,
        "agentAssignments": [
            {
                "id": str(item.id),
                "agent": reference,
                "reason": item.reason.value,
                "effectiveAfterMessageId": str(item.effective_after_message_id)
                if item.effective_after_message_id
                else None,
                "createdAt": item.created_at.isoformat(),
            }
            for item in conversation.assignments
            for reference in [agent_reference(item.agent_profile_id, item.agent_revision_id)]
            if reference is not None
        ],
    }
    if current_reference is not None:
        payload["agent"] = current_reference
    return payload


def detail_payload(
    conversation: Conversation, agents: AgentCatalog | None = None
) -> dict[str, object]:
    result = conversation_payload(conversation, agents)
    result["messages"] = [message_payload(message) for message in conversation.messages]
    # Retry lineage is durable history.  Do not truncate the summary and make
    # an older retry target disappear from the client-visible conversation.
    result["recentRuns"] = [run_payload(run) for run in conversation.runs]
    return result


async def model_descriptors(request: Request) -> list[ModelDescriptor]:
    models, _ = await state(request).models()
    return models


@router.get("")
async def list_conversations(
    request: Request,
    session: Session = Depends(require_session),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, object]:
    try:
        items, next_cursor = await state(request).store.list(
            session.principal.subject, limit, cursor, session.principal.issuer
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [conversation_payload(item, state(request).agents) for item in items],
        "nextCursor": next_cursor,
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_conversation(
    request: Request,
    body: CreateConversationRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    routing_timer = Stopwatch()
    models = await model_descriptors(request)
    if body.modelId is None:
        raise HTTPException(status_code=422, detail="modelId is required")
    _ = next((model for model in models if model.id == body.modelId), None)
    routing_duration_ms = routing_timer.elapsed_ms()
    persistence_timer = Stopwatch()
    try:
        conversation, message, run = await state(request).store.create(
            session.principal.issuer,
            session.principal.subject,
            body.message,
            body.modelId,
            models,
            idempotency_key,
            body.agentRevisionId,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_model_persistence(request, run, persistence_timer.elapsed_ms(), routing_duration_ms)
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(conversation, state(request).agents),
        "userMessage": message_payload(message),
        "run": run_payload(run),
    }


@router.get("/{conversation_id}")
async def get_conversation(
    request: Request, conversation_id: UUID, session: Session = Depends(require_session)
) -> dict[str, object]:
    try:
        conversation = await state(request).store.get(
            conversation_id, session.principal.subject, session.principal.issuer
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    return detail_payload(conversation, state(request).agents)


@router.patch("/{conversation_id}")
async def update_conversation(
    request: Request,
    conversation_id: UUID,
    body: UpdateConversationRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    routing_timer = Stopwatch()
    models = await model_descriptors(request)
    if body.modelId is None and body.agentRevisionId is None:
        raise HTTPException(status_code=422, detail="modelId or agentRevisionId is required")
    current = await state(request).store.get(
        conversation_id, session.principal.subject, session.principal.issuer
    )
    model_id = body.modelId or current.model_id
    _ = next((model for model in models if model.id == model_id), None)
    routing_duration_ms = routing_timer.elapsed_ms()
    persistence_timer = Stopwatch()
    try:
        conversation = await state(request).store.update_model(
            conversation_id,
            session.principal.subject,
            model_id,
            body.version,
            models,
            idempotency_key,
            session.principal.issuer,
            body.agentRevisionId,
            body.transcriptSharingConfirmed,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except (VersionConflict, ActiveRunConflict) as exc:
        raise HTTPException(status_code=409, detail="conversation version conflict") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (AgentUnavailable, AgentSwitchConfirmationRequired) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_conversation_model_persistence(
        request,
        conversation,
        persistence_timer.elapsed_ms(),
        routing_duration_ms,
    )
    if body.agentRevisionId is not None and conversation.assignments:
        assignment = conversation.assignments[-1]
        await state(request).store.record_auth_audit(
            "conversation.agent.revision_upgrade"
            if assignment.reason.value == "revision_upgrade"
            else "conversation.agent.manual_switch",
            "ok",
            issuer=session.principal.issuer,
            subject=session.principal.subject,
            metadata={
                "conversationId": str(conversation.id),
                "agentProfileId": str(assignment.agent_profile_id),
                "agentRevisionId": str(assignment.agent_revision_id),
                "revision": next(
                    (
                        revision.revision
                        for profile in state(request).agents.list_agents()
                        if profile.id == assignment.agent_profile_id
                        for revision in profile.revisions
                        if revision.id == assignment.agent_revision_id
                    ),
                    1,
                ),
            },
        )
    return conversation_payload(conversation, state(request).agents)


@router.post("/{conversation_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    request: Request,
    conversation_id: UUID,
    body: CreateRunRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    persistence_timer = Stopwatch()
    try:
        conversation, message, run = await state(request).store.add_run(
            conversation_id,
            session.principal.subject,
            body.message,
            body.conversationVersion,
            idempotency_key,
            session.principal.issuer,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except (VersionConflict, ActiveRunConflict, IdempotencyConflict, AgentUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_model_persistence(request, run, persistence_timer.elapsed_ms())
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(conversation, state(request).agents),
        "userMessage": message_payload(message),
        "run": run_payload(run),
    }


def _record_model_persistence(
    request: Request,
    run: Run,
    persistence_duration_ms: float,
    routing_duration_ms: float | None = None,
) -> None:
    trace_id = run.id.hex
    root_id = new_span_id()
    persistence_span_id = new_span_id()
    metadata = {
        "trace_id": trace_id,
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "provider": run.provider,
        "model_id": run.model_id,
        "agent_revision_id": str(run.agent_revision_id),
        "model_policy_revision_id": str(run.model_policy_revision_id),
    }
    state(request).metrics.record_span(
        "aura.execution.run_coordinator",
        "conversation.command",
        persistence_duration_ms,
        trace_id=trace_id,
        span_id=root_id,
        parent_span_id=None,
        dependency="conversation_store",
        outcome="ok",
        run_id=str(run.id),
        conversation_id=str(run.conversation_id),
    )
    state(request).metrics.record_span(
        "aura.interaction.conversation_persistence",
        "conversation.persist",
        persistence_duration_ms,
        trace_id=trace_id,
        span_id=persistence_span_id,
        parent_span_id=root_id,
        dependency="postgresql",
        outcome="ok",
        run_id=str(run.id),
        conversation_id=str(run.conversation_id),
    )
    state(request).metrics.increment(
        "aura.runtime.model_routing",
        "model_selection_persisted",
        **metadata,
    )
    state(request).metrics.increment(
        "aura.interaction.conversation_persistence",
        "model_selection_persisted",
        **metadata,
    )
    if routing_duration_ms is not None:
        state(request).metrics.record_span(
            "aura.runtime.model_routing",
            "model.route",
            routing_duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=root_id,
            dependency="model_policy",
            outcome="ok",
            run_id=str(run.id),
            conversation_id=str(run.conversation_id),
            model_id=run.model_id,
            agent_revision_id=str(run.agent_revision_id),
            model_policy_revision_id=str(run.model_policy_revision_id),
        )


def _record_conversation_model_persistence(
    request: Request,
    conversation: Conversation,
    persistence_duration_ms: float,
    routing_duration_ms: float,
) -> None:
    trace_id = conversation.id.hex
    root_id = new_span_id()
    persistence_span_id = new_span_id()
    try:
        revision = state(request).agents.resolve_revision_unchecked(
            conversation.agent_revision_id
        )
        model_policy_revision_id = str(revision.model_policy_revision_id)
    except Exception:
        model_policy_revision_id = None
    metadata = {
        "trace_id": trace_id,
        "conversation_id": str(conversation.id),
        "provider": "ollama",
        "model_id": conversation.model_id,
        "agent_revision_id": str(conversation.agent_revision_id),
    }
    if model_policy_revision_id is not None:
        metadata["model_policy_revision_id"] = model_policy_revision_id
    state(request).metrics.record_span(
        "aura.execution.run_coordinator",
        "conversation.command",
        persistence_duration_ms,
        trace_id=trace_id,
        span_id=root_id,
        parent_span_id=None,
        dependency="conversation_store",
        outcome="ok",
        conversation_id=str(conversation.id),
    )
    state(request).metrics.record_span(
        "aura.interaction.conversation_persistence",
        "conversation.persist",
        persistence_duration_ms,
        trace_id=trace_id,
        span_id=persistence_span_id,
        parent_span_id=root_id,
        dependency="postgresql",
        outcome="ok",
        conversation_id=str(conversation.id),
    )
    state(request).metrics.increment(
        "aura.runtime.model_routing", "model_selection_persisted", **metadata
    )
    state(request).metrics.record_span(
        "aura.runtime.model_routing",
        "model.route",
        routing_duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=root_id,
        dependency="model_policy",
        outcome="ok",
        conversation_id=str(conversation.id),
        model_id=conversation.model_id,
        agent_revision_id=str(conversation.agent_revision_id),
        **(
            {"model_policy_revision_id": model_policy_revision_id}
            if model_policy_revision_id is not None
            else {}
        ),
    )
    state(request).metrics.increment(
        "aura.interaction.conversation_persistence",
        "model_selection_persisted",
        **metadata,
    )
