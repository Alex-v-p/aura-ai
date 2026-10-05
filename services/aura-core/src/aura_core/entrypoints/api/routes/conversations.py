"""Conversation HTTP translations; rules live in the conversation service."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.execution.runs.public import Run, message_payload, run_payload
from aura_core.domains.interaction.conversations.public import (
    GENERAL_AGENT,
    ActiveRunConflict,
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
    modelId: str = Field(min_length=1, max_length=255)


class UpdateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelId: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32768)
    conversationVersion: int = Field(ge=1)


def state(request: Request) -> AppState:
    return request.app.state.aura


def conversation_payload(conversation: Conversation) -> dict[str, object]:
    current = conversation.current_run
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "agentProfileId": str(conversation.agent_profile_id),
        "agentRevisionId": str(conversation.agent_revision_id),
        "modelId": conversation.model_id,
        "version": conversation.version,
        "createdAt": conversation.created_at.isoformat(),
        "updatedAt": conversation.updated_at.isoformat(),
        "currentRun": run_payload(current) if current else None,
    }


def detail_payload(conversation: Conversation) -> dict[str, object]:
    result = conversation_payload(conversation)
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
    return {"items": [conversation_payload(item) for item in items], "nextCursor": next_cursor}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_conversation(
    request: Request,
    body: CreateConversationRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    routing_timer = Stopwatch()
    models = await model_descriptors(request)
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
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _record_model_persistence(
        request, run, persistence_timer.elapsed_ms(), routing_duration_ms
    )
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(conversation),
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
    return detail_payload(conversation)


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
    _ = next((model for model in models if model.id == body.modelId), None)
    routing_duration_ms = routing_timer.elapsed_ms()
    persistence_timer = Stopwatch()
    try:
        conversation = await state(request).store.update_model(
            conversation_id,
            session.principal.subject,
            body.modelId,
            body.version,
            models,
            idempotency_key,
            session.principal.issuer,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail="conversation version conflict") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _record_conversation_model_persistence(
        request,
        conversation,
        persistence_timer.elapsed_ms(),
        routing_duration_ms,
    )
    return conversation_payload(conversation)


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
    except (VersionConflict, ActiveRunConflict, IdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_model_persistence(request, run, persistence_timer.elapsed_ms())
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(conversation),
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
    metadata = {
        "trace_id": trace_id,
        "conversation_id": str(conversation.id),
        "provider": "ollama",
        "model_id": conversation.model_id,
        "agent_revision_id": str(conversation.agent_revision_id),
        "model_policy_revision_id": str(GENERAL_AGENT.policy_revision_id),
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
        model_policy_revision_id=str(GENERAL_AGENT.policy_revision_id),
    )
    state(request).metrics.increment(
        "aura.interaction.conversation_persistence",
        "model_selection_persisted",
        **metadata,
    )
