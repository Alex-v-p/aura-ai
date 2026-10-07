"""Conversation HTTP translations; rules live in the conversation service."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from datetime import datetime
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aura_core.domains.execution.runs.public import Run, RunStatus, message_payload, run_payload
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentSwitchConfirmationRequired,
    AgentUnavailable,
    ArchivedConversationConflict,
    Conversation,
    ConversationArchiveState,
    ConversationListFilters,
    ConversationNotFound,
    IdempotencyConflict,
    InvalidConversationCursor,
    InvalidConversationMetadata,
    ModelUnavailable,
    PersonaUnavailable,
    VersionConflict,
)
from aura_core.domains.interaction.personas.public import PersonaRevisionQueryPort
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
    personaRevisionId: UUID | None = None


class UpdateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modelId: str | None = Field(default=None, min_length=1, max_length=255)
    agentRevisionId: UUID | None = None
    personaRevisionId: UUID | None = None
    useAgentDefaultPersona: Literal[True] | None = None
    version: int = Field(ge=1)
    transcriptSharingConfirmed: bool = False

    @model_validator(mode="after")
    def validate_persona_operation(self) -> UpdateConversationRequest:
        if self.personaRevisionId is not None and self.useAgentDefaultPersona is True:
            raise ValueError("personaRevisionId and useAgentDefaultPersona are mutually exclusive")
        return self


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=32768)
    conversationVersion: int = Field(ge=1)


class UpdateConversationMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=255)
    archived: bool | None = None
    version: int = Field(ge=1)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title_for_transport(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(
            ord(character) < 0x20
            or 0x7F <= ord(character) <= 0x9F
            or ord(character) in {0x2028, 0x2029}
            for character in value
        ):
            return value
        return value.strip()

    @model_validator(mode="after")
    def validate_mutation(self) -> UpdateConversationMetadataRequest:
        if self.title is None and self.archived is None:
            raise ValueError("title or archived is required")
        return self


def state(request: Request) -> AppState:
    return request.app.state.aura


def conversation_payload(
    conversation: Conversation,
    agents: AgentCatalog | None = None,
    personas: PersonaRevisionQueryPort | None = None,
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
    def persona_reference(identifier: UUID) -> dict[str, object] | None:
        profiles = personas.list_personas() if personas else []
        profile = next(
            (
                item
                for item in profiles
                if any(revision.id == identifier for revision in item.revisions)
            ),
            None,
        )
        if profile is None:
            return None
        revision = next(item for item in profile.revisions if item.id == identifier)
        return {
            "profileId": str(profile.id),
            "revisionId": str(revision.id),
            "displayName": revision.display_name,
            "revision": revision.revision,
            "status": profile.status.value,
            "newerRevisionAvailable": any(
                item.revision > revision.revision for item in profile.revisions
            ),
        }

    effective_persona_id = conversation.persona_override_revision_id
    if effective_persona_id is None:
        try:
            effective_persona_id = agents.resolve_revision_unchecked(
                conversation.agent_revision_id
            ).persona_revision_id if agents else None
        except Exception:
            effective_persona_id = None
    current_persona = (
        persona_reference(effective_persona_id) if effective_persona_id is not None else None
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
        "archivedAt": conversation.archived_at.isoformat()
        if conversation.archived_at is not None
        else None,
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
        "personaOverride": conversation.persona_override_revision_id is not None,
        "personaAssignments": [
            {
                "id": str(item.id),
                "persona": persona_reference(item.persona_revision_id),
                "source": item.source.value,
                "reason": item.reason.value,
                "effectiveAfterMessageId": str(item.effective_after_message_id)
                if item.effective_after_message_id
                else None,
                "createdAt": item.created_at.isoformat(),
            }
            for item in conversation.persona_assignments
            if persona_reference(item.persona_revision_id) is not None
        ],
    }
    if current_reference is not None:
        payload["agent"] = current_reference
    if current_persona is not None:
        payload["persona"] = current_persona
    return payload


def detail_payload(
    conversation: Conversation,
    agents: AgentCatalog | None = None,
    personas: PersonaRevisionQueryPort | None = None,
) -> dict[str, object]:
    result = conversation_payload(conversation, agents, personas)
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
    q: str | None = Query(default=None, max_length=200),
    agent_profile_id: UUID | None = Query(default=None, alias="agentProfileId"),
    model_id: str | None = Query(default=None, alias="modelId", max_length=255),
    run_status: RunStatus | None = Query(default=None, alias="runStatus"),
    archive_state: ConversationArchiveState = Query(
        default=ConversationArchiveState.ACTIVE, alias="archiveState"
    ),
    activity_from: datetime | None = Query(default=None, alias="activityFrom"),
    activity_to: datetime | None = Query(default=None, alias="activityTo"),
) -> dict[str, object]:
    trace_id = uuid4().hex
    root_span_id = new_span_id()
    started = perf_counter()
    filters = ConversationListFilters(
        q=q,
        agent_profile_id=agent_profile_id,
        model_id=model_id,
        run_status=run_status,
        archive_state=archive_state,
        activity_from=activity_from,
        activity_to=activity_to,
    ).normalized()
    try:
        if filters == ConversationListFilters():
            items, next_cursor = await state(request).store.list(
                session.principal.subject,
                limit,
                cursor,
                session.principal.issuer,
                trace_id=trace_id,
                parent_span_id=root_span_id,
            )
        else:
            items, next_cursor = await state(request).store.list(
                session.principal.subject,
                limit,
                cursor,
                session.principal.issuer,
                filters=filters,
                trace_id=trace_id,
                parent_span_id=root_span_id,
            )
    except InvalidConversationCursor as exc:
        state(request).metrics.record_span(
            "aura.interaction.conversation_persistence",
            "conversation.list.request",
            (perf_counter() - started) * 1000,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            dependency="conversation_store",
            outcome="error",
            error_class="validation",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        state(request).metrics.record_span(
            "aura.interaction.conversation_persistence",
            "conversation.list.request",
            (perf_counter() - started) * 1000,
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            dependency="conversation_store",
            outcome="error",
            error_class="persistence",
        )
        raise
    state(request).metrics.record_span(
        "aura.interaction.conversation_persistence",
        "conversation.list.request",
        (perf_counter() - started) * 1000,
        trace_id=trace_id,
        span_id=root_span_id,
        parent_span_id=None,
        dependency="conversation_store",
        outcome="ok",
    )
    return {
        "items": [
            conversation_payload(item, state(request).agents, state(request).personas)
            for item in items
        ],
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
            body.personaRevisionId,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersonaUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_model_persistence(request, run, persistence_timer.elapsed_ms(), routing_duration_ms)
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(
            conversation, state(request).agents, state(request).personas
        ),
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
    return detail_payload(conversation, state(request).agents, state(request).personas)


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
    if (
        body.modelId is None
        and body.agentRevisionId is None
        and body.personaRevisionId is None
        and not body.useAgentDefaultPersona
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "modelId, agentRevisionId, personaRevisionId, or "
                "useAgentDefaultPersona is required"
            ),
        )
    if body.personaRevisionId is not None and body.useAgentDefaultPersona:
        raise HTTPException(
            status_code=422, detail="persona override options are mutually exclusive"
        )
    current = await state(request).store.get(
        conversation_id, session.principal.subject, session.principal.issuer
    )
    model_changed = body.modelId is not None and body.modelId != current.model_id
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
            body.personaRevisionId,
            body.useAgentDefaultPersona,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except (VersionConflict, ActiveRunConflict, ArchivedConversationConflict) as exc:
        raise HTTPException(status_code=409, detail="conversation version conflict") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ModelUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (AgentUnavailable, AgentSwitchConfirmationRequired, PersonaUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if model_changed:
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
    return conversation_payload(conversation, state(request).agents, state(request).personas)


@router.patch("/{conversation_id}/metadata")
async def update_conversation_metadata(
    request: Request,
    conversation_id: UUID,
    body: UpdateConversationMetadataRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    started = perf_counter()
    trace_id = uuid4().hex
    root_span_id = new_span_id()
    try:
        conversation = await state(request).store.update_metadata(
            conversation_id,
            session.principal.subject,
            title=body.title,
            archived=body.archived,
            version=body.version,
            idempotency_key=idempotency_key,
            issuer=session.principal.issuer,
            trace_id=trace_id,
            parent_span_id=root_span_id,
            metrics=state(request).metrics,
        )
    except ConversationNotFound as exc:
        _record_metadata_mutation(
            request,
            trace_id,
            root_span_id,
            conversation_id,
            started,
            "unknown",
            "error",
            "not_found",
            body.archived is not None,
        )
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except InvalidConversationMetadata as exc:
        _record_metadata_mutation(
            request,
            trace_id,
            root_span_id,
            conversation_id,
            started,
            "unknown",
            "error",
            "validation",
            body.archived is not None,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (VersionConflict, ActiveRunConflict, ArchivedConversationConflict) as exc:
        _record_metadata_mutation(
            request,
            trace_id,
            root_span_id,
            conversation_id,
            started,
            "unknown",
            "error",
            "conflict",
            body.archived is not None,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        _record_metadata_mutation(
            request,
            trace_id,
            root_span_id,
            conversation_id,
            started,
            "unknown",
            "error",
            "idempotency",
            body.archived is not None,
        )
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except Exception:
        _record_metadata_mutation(
            request,
            trace_id,
            root_span_id,
            conversation_id,
            started,
            "unknown",
            "error",
            "persistence",
            body.archived is not None,
        )
        raise
    _record_metadata_mutation(
        request,
        trace_id,
        root_span_id,
        conversation_id,
        started,
        "archived" if conversation.archived_at is not None else "active",
        "ok",
        None,
        body.archived is not None,
        result_count=1,
    )
    return conversation_payload(conversation, state(request).agents, state(request).personas)


def _record_metadata_mutation(
    request: Request,
    trace_id: str,
    root_span_id: str,
    conversation_id: UUID,
    started: float,
    archive_state: str,
    outcome: str,
    error_class: str | None,
    archive_operation: bool,
    *,
    result_count: int = 0,
) -> None:
    duration_ms = (perf_counter() - started) * 1000
    state(request).metrics.record_span(
        "aura.interaction.conversation_persistence",
        "conversation.metadata.request",
        duration_ms,
        trace_id=trace_id,
        span_id=root_span_id,
        parent_span_id=None,
        dependency="conversation_store",
        outcome=outcome,
        error_class=error_class,
        conversation_id=str(conversation_id),
        result_count=str(result_count),
    )
    state(request).metrics.observe(
        "aura.interaction.conversation_persistence",
        "conversation_metadata_mutation_duration_ms",
        (perf_counter() - started) * 1000,
        trace_id=trace_id,
        conversation_id=str(conversation_id),
        archive_state=archive_state,
        result_count=str(result_count),
        outcome=outcome,
    )
    if archive_operation:
        state(request).metrics.increment(
            "aura.interaction.conversation_persistence",
            "conversation_archive_outcome",
            trace_id=trace_id,
            conversation_id=str(conversation_id),
            archive_state=archive_state,
            result_count=str(result_count),
            outcome=outcome,
        )


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
    except (
        VersionConflict,
        ActiveRunConflict,
        ArchivedConversationConflict,
        IdempotencyConflict,
        AgentUnavailable,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _record_model_persistence(request, run, persistence_timer.elapsed_ms())
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(
            conversation, state(request).agents, state(request).personas
        ),
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
