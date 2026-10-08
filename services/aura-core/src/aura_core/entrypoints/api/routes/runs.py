"""Run command and SSE HTTP translations."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from aura_core.domains.execution.runs.events import new_event
from aura_core.domains.execution.runs.public import message_payload, run_payload
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentUnavailable,
    ArchivedConversationConflict,
    ConversationNotFound,
    IdempotencyConflict,
)
from aura_core.entrypoints.api.routes.conversations import conversation_payload, state
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id

router = APIRouter(prefix="/api/v1/runs", tags=["Runs"])

_MEMORY_ACTIVITY_NAMESPACE = UUID("ab5a2cd0-6ce9-4b69-a6c7-c7d0f9f840a2")


def _memory_activity_payload(value: object) -> dict[str, object]:
    """Translate Core's snapshot DTO without leaking private memory content."""

    if isinstance(value, dict):
        source = cast(dict[str, object], value)
    else:
        source = {
            "runId": getattr(value, "run_id", None),
            "processingStatus": getattr(value, "processing_status", "settled"),
            "items": getattr(value, "items", ()),
            "lastEventId": getattr(value, "last_event_id", None),
            "reconciledAt": getattr(value, "reconciled_at", None),
        }
    items = source.get("items", ())
    rendered_items: list[dict[str, object]] = []
    raw_items = (
        cast(list[object] | tuple[object, ...], items) if isinstance(items, (list, tuple)) else ()
    )
    for item in raw_items:
        if isinstance(item, dict):
            rendered = cast(dict[str, object], item.copy())
        else:
            rendered = {
                "id": getattr(item, "id", None),
                "action": getattr(item, "action", None),
                "status": getattr(item, "status", None),
                "scope": getattr(item, "scope", None),
                "candidateId": getattr(item, "candidate_id", None),
                "memoryId": getattr(item, "memory_id", None),
                "memoryRevisionId": getattr(item, "memory_revision_id", None),
                "policyRevisionId": getattr(item, "policy_revision_id", None),
                "embeddingGenerationId": getattr(item, "embedding_generation_id", None),
                "reconciliationStatus": getattr(item, "reconciliation_status", "authoritative"),
                "occurredAt": getattr(item, "occurred_at", None),
            }
        # Activity snapshots are identifier-only.  Explicitly select the
        # contract fields instead of serializing arbitrary provider/domain
        # objects returned by a repository.
        rendered = {
            key: rendered.get(key)
            for key in (
                "id",
                "action",
                "status",
                "scope",
                "candidateId",
                "memoryId",
                "memoryRevisionId",
                "policyRevisionId",
                "embeddingGenerationId",
                "reconciliationStatus",
                "occurredAt",
            )
        }
        rendered_items.append(cast(dict[str, object], _json_safe(rendered)))
    return {
        "runId": _json_safe(source.get("runId")),
        "processingStatus": _json_safe(source.get("processingStatus", "settled")),
        "items": rendered_items,
        "lastEventId": _json_safe(source.get("lastEventId")),
        "reconciledAt": _json_safe(source.get("reconciledAt")),
    }


def _json_safe(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _json_safe(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return [_json_safe(item) for item in items]
    return value


async def _memory_processing_status(
    request: Request,
    run_id: UUID,
    session: Session,
    *,
    conversation_id: UUID,
    trace_id: str,
    parent_span_id: str | None,
) -> str | None:
    timer = Stopwatch()
    loader = getattr(state(request).store, "get_run_memory_activity", None)
    if not callable(loader):
        return None
    try:
        typed_loader = cast(Callable[..., Awaitable[Any]], loader)
        snapshot = await typed_loader(run_id, session.principal.issuer, session.principal.subject)
    except LookupError:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.reconcile",
            timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=parent_span_id,
            dependency="memory_store",
            outcome="not_found",
            error_class="run_not_found",
            run_id=str(run_id),
            conversation_id=str(conversation_id),
        )
        return None
    except Exception:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.reconcile",
            timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=parent_span_id,
            dependency="memory_store",
            outcome="error",
            error_class="persistence",
            run_id=str(run_id),
            conversation_id=str(conversation_id),
        )
        raise
    payload = _memory_activity_payload(snapshot)
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.reconcile",
        timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent_span_id,
        dependency="memory_store",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )
    value = payload.get("processingStatus")
    return value if isinstance(value, str) else None


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    request: Request,
    run_id: UUID,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    persistence_timer = Stopwatch()
    try:
        run = await state(request).store.request_cancel(
            run_id,
            session.principal.subject,
            idempotency_key,
            session.principal.issuer,
        )
    except ConversationNotFound as exc:
        _record_command_error(request, run_id, "run.cancel", persistence_timer.elapsed_ms())
        state(request).metrics.increment(
            "aura.execution.run_coordinator",
            "errors",
            trace_id=run_id.hex,
            run_id=str(run_id),
            error_class="cancel",
        )
        raise HTTPException(status_code=404, detail="run not found") from exc
    except IdempotencyConflict as exc:
        _record_command_error(request, run_id, "run.cancel", persistence_timer.elapsed_ms())
        state(request).metrics.increment(
            "aura.execution.run_coordinator",
            "errors",
            trace_id=run_id.hex,
            run_id=str(run_id),
            error_class="cancel",
        )
        raise HTTPException(status_code=409, detail="idempotency key payload conflict") from exc
    except ArchivedConversationConflict as exc:
        _record_command_error(request, run_id, "run.cancel", persistence_timer.elapsed_ms())
        raise HTTPException(status_code=409, detail="archived conversation is read-only") from exc
    state(request).metrics.increment(
        "aura.execution.run_coordinator",
        "cancellations",
        trace_id=run.id.hex,
        run_id=str(run.id),
        conversation_id=str(run.conversation_id),
        status=run.status.value,
    )
    _record_run_command(
        request,
        run.id,
        run.conversation_id,
        "run.cancel",
        persistence_timer.elapsed_ms(),
    )
    return run_payload(run)


@router.post("/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_run(
    request: Request,
    run_id: UUID,
    session: Session = Depends(require_csrf),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    persistence_timer = Stopwatch()
    try:
        conversation, message, run = await state(request).store.retry(
            run_id, session.principal.subject, idempotency_key, session.principal.issuer
        )
    except ConversationNotFound as exc:
        _record_command_error(request, run_id, "run.retry", persistence_timer.elapsed_ms())
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (
        ActiveRunConflict,
        ArchivedConversationConflict,
        IdempotencyConflict,
        AgentUnavailable,
    ) as exc:
        _record_command_error(request, run_id, "run.retry", persistence_timer.elapsed_ms())
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    metadata = {
        "trace_id": run.id.hex,
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
    }
    state(request).metrics.increment(
        "aura.execution.run_coordinator",
        "retries",
        retry_of_run_id=str(run.retry_of_run_id or run.id),
        **metadata,
    )
    _record_run_command(
        request,
        run.id,
        run.conversation_id,
        "run.retry",
        persistence_timer.elapsed_ms(),
    )
    state(request).metrics.increment(
        "aura.runtime.model_routing",
        "model_selection_persisted",
        provider=run.provider,
        model_id=run.model_id,
        agent_revision_id=str(run.agent_revision_id),
        model_policy_revision_id=str(run.model_policy_revision_id),
        **metadata,
    )
    await state(request).coordinator.enqueue(run)
    return {
        "conversation": conversation_payload(conversation, state(request).agents),
        "userMessage": message_payload(message),
        "run": run_payload(run),
    }


@router.get("/{run_id}/memory-activity")
async def get_run_memory_activity(
    request: Request,
    run_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    """Return the authoritative owner-scoped memory activity snapshot.

    The memory domain owns the durable projection.  This route only performs
    authentication, invokes that public seam, and selects the identifier-only
    transport shape used to reconcile SSE activity events.
    """

    timer = Stopwatch()
    trace_id = uuid4().hex
    try:
        loader = getattr(state(request).store, "get_run_memory_activity", None)
        if not callable(loader):
            raise ConversationNotFound
        typed_loader = cast(Callable[..., Awaitable[Any]], loader)
        snapshot = await typed_loader(run_id, session.principal.issuer, session.principal.subject)
    except (ConversationNotFound, LookupError) as exc:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.reconcile",
            timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="memory_store",
            outcome="not_found",
            error_class="run_not_found",
            run_id=str(run_id),
        )
        raise HTTPException(status_code=404, detail="run not found") from exc
    except Exception:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.reconcile",
            timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="memory_store",
            outcome="error",
            error_class="persistence",
            run_id=str(run_id),
        )
        raise
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.reconcile",
        timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="memory_store",
        outcome="ok",
        run_id=str(run_id),
    )
    payload = _memory_activity_payload(snapshot)
    processing_status = payload.get("processingStatus")
    if isinstance(processing_status, str):
        await state(request).publisher.set_memory_processing_status(run_id, processing_status)
    return payload


@router.get("/{run_id}/events")
async def stream_run_events(
    request: Request,
    run_id: UUID,
    session: Session = Depends(require_session),
    last_event_id: UUID | None = Header(default=None, alias="Last-Event-ID"),
) -> Response:
    connection_timer = Stopwatch()
    trace_id = uuid4().hex
    connection_span_id = new_span_id()
    # The session dependency has authorized this request before the handler
    # runs. Establish the root before any owner-scoped dependency call so
    # reconciliation and replay failures always have a valid parent.
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "sse.connect",
        connection_timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=connection_span_id,
        parent_span_id=None,
        dependency="sse_client",
        outcome="ok",
        run_id=str(run_id),
    )
    try:
        conversation, run = await state(request).store.find_run(
            run_id, session.principal.subject, session.principal.issuer
        )
    except ConversationNotFound as exc:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "sse.connect",
            connection_timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=connection_span_id,
            dependency="conversation_store",
            outcome="error",
            error_class="not_found",
            run_id=str(run_id),
        )
        raise HTTPException(status_code=404, detail="run not found") from exc
    # Reconnect reconciliation comes from the authoritative owner-scoped
    # projection, never from browser event payloads.  Older test doubles may
    # not expose the optional seam; in that case the publisher derives state
    # from durable activity events.
    memory_processing_status = await _memory_processing_status(
        request,
        run_id,
        session,
        conversation_id=conversation.id,
        trace_id=trace_id,
        parent_span_id=connection_span_id,
    )
    if memory_processing_status is not None:
        await state(request).publisher.set_memory_processing_status(
            run_id, memory_processing_status
        )
    assistant = next(
        (item for item in conversation.messages if item.id == run.assistant_message_id), None
    )
    snapshot = new_event(
        "run.snapshot",
        run.id,
        conversation.id,
        0,
        {
            "run": run_payload(run),
            "assistantMessage": message_payload(assistant) if assistant else None,
        },
    )
    publisher = state(request).publisher
    replay_timer = Stopwatch()
    sql_store = getattr(state(request), "sql_store", None)
    try:
        if sql_store is not None:
            await publisher.hydrate(await sql_store.event_history(run_id))
    except Exception:
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.replay",
            replay_timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=connection_span_id,
            dependency="event_store",
            outcome="error",
            error_class="database",
            run_id=str(run.id),
            conversation_id=str(conversation.id),
        )
        raise
    # The request root was emitted before dependency calls. Expired-cursor
    # telemetry is a child of that root even though the request returns 410.
    stream_metadata = {
        "trace_id": trace_id,
        "run_id": str(run.id),
        "conversation_id": str(conversation.id),
    }
    state(request).metrics.increment(
        "aura.runtime.stream_delivery",
        "sse_connections",
        **stream_metadata,
    )
    if last_event_id is not None and not await publisher.contains(run_id, last_event_id):
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.cursor_expired",
            connection_timer.elapsed_ms(),
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=connection_span_id,
            dependency="event_store",
            outcome="expired",
            error_class="cursor_expired",
            run_id=str(run_id),
            conversation_id=str(conversation.id),
        )
        raise HTTPException(status_code=410, detail="event cursor is no longer available")
    replay = await publisher.history(run_id, last_event_id)
    replay_duration_ms = replay_timer.elapsed_ms()

    if last_event_id is not None:
        state(request).metrics.increment(
            "aura.runtime.stream_delivery",
            "sse_reconnects",
            **stream_metadata,
        )
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "sse.reconnect",
            replay_duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=connection_span_id,
            dependency="event_store",
            outcome="ok",
            run_id=str(run.id),
            conversation_id=str(conversation.id),
        )
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "memory.activity.replay",
            replay_duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=connection_span_id,
            dependency="event_store",
            outcome="ok",
            run_id=str(run.id),
            conversation_id=str(conversation.id),
        )

    async def generate() -> AsyncIterator[str]:
        # This event is reconstructed from authoritative persisted run state
        # on every connection. It intentionally has no SSE id so it cannot
        # replace the cursor into durable event history.
        snapshot_timer = Stopwatch()
        snapshot_payload = json.dumps(snapshot.payload(), separators=(",", ":"))
        state(request).metrics.increment(
            "aura.runtime.stream_delivery",
            "events_delivered",
            **stream_metadata,
        )
        _record_sse_delivery(
            request,
            run.id,
            conversation.id,
            connection_span_id,
            trace_id,
            snapshot_timer.elapsed_ms(),
        )
        yield f"event: {snapshot.event_type}\ndata: {snapshot_payload}\n\n"
        await asyncio.sleep(0)
        if (
            not replay
            and run.status.value
            in {
                "canceled",
                "completed",
                "failed",
                "interrupted",
            }
            and not await publisher.has_pending_memory_activity(run_id)
        ):
            return
        async for event in publisher.stream(run_id, last_event_id):
            delivery_timer = Stopwatch()
            try:
                payload = json.dumps(event.payload(), separators=(",", ":"))
            except Exception:
                state(request).metrics.record_span(
                    "aura.runtime.stream_delivery",
                    "sse.deliver",
                    delivery_timer.elapsed_ms(),
                    trace_id=trace_id,
                    span_id=new_span_id(),
                    parent_span_id=connection_span_id,
                    dependency="sse_client",
                    outcome="error",
                    error_class="serialization",
                    run_id=str(run.id),
                    conversation_id=str(conversation.id),
                )
                raise
            cursor = "" if event.event_type == "heartbeat" else f"id: {event.event_id}\n"
            state(request).metrics.increment(
                "aura.runtime.stream_delivery",
                "events_delivered",
                **stream_metadata,
            )
            if event.event_type == "memory.activity":
                activity_status = event.data.get("status")
                activity_status = (
                    activity_status
                    if isinstance(activity_status, str)
                    and activity_status in {"queued", "completed", "failed"}
                    else "unknown"
                )
                state(request).metrics.observe(
                    "aura.runtime.stream_delivery",
                    "memory_activity_delivery_duration_ms",
                    delivery_timer.elapsed_ms(),
                    status=activity_status,
                    trace_id=trace_id,
                    run_id=str(run.id),
                    conversation_id=str(conversation.id),
                )
                state(request).metrics.increment(
                    "aura.runtime.stream_delivery",
                    "memory_activity_delivery_outcome",
                    outcome="ok",
                    status=activity_status,
                    trace_id=trace_id,
                    run_id=str(run.id),
                    conversation_id=str(conversation.id),
                )
            _record_sse_delivery(
                request,
                run.id,
                conversation.id,
                connection_span_id,
                trace_id,
                delivery_timer.elapsed_ms(),
            )
            yield f"{cursor}event: {event.event_type}\ndata: {payload}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _record_command_error(
    request: Request,
    run_id: UUID,
    operation: str,
    duration_ms: float,
) -> None:
    state(request).metrics.record_span(
        "aura.execution.run_coordinator",
        operation,
        duration_ms,
        trace_id=run_id.hex,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="conversation_store",
        outcome="error",
        error_class="cancel" if operation == "run.cancel" else "persistence",
        run_id=str(run_id),
    )


def _record_run_command(
    request: Request,
    run_id: UUID,
    conversation_id: UUID,
    operation: str,
    duration_ms: float,
) -> None:
    trace_id = run_id.hex
    root_id = new_span_id()
    state(request).metrics.record_span(
        "aura.execution.run_coordinator",
        operation,
        duration_ms,
        trace_id=trace_id,
        span_id=root_id,
        parent_span_id=None,
        dependency="conversation_store",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )
    state(request).metrics.record_span(
        "aura.interaction.conversation_persistence",
        "conversation.persist",
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=root_id,
        dependency="postgresql",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )


def _record_sse_delivery(
    request: Request,
    run_id: UUID,
    conversation_id: UUID,
    connection_span_id: str,
    trace_id: str,
    duration_ms: float,
) -> None:
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "sse.deliver",
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=connection_span_id,
        dependency="sse_client",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )
