"""Run command and SSE HTTP translations."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from aura_core.domains.execution.runs.events import new_event
from aura_core.domains.execution.runs.public import message_payload, run_payload
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentUnavailable,
    ConversationNotFound,
    IdempotencyConflict,
)
from aura_core.entrypoints.api.routes.conversations import conversation_payload, state
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session
from aura_core.platform.telemetry import Stopwatch, new_span_id

router = APIRouter(prefix="/api/v1/runs", tags=["Runs"])


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
    except (ActiveRunConflict, IdempotencyConflict, AgentUnavailable) as exc:
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


@router.get("/{run_id}/events")
async def stream_run_events(
    request: Request,
    run_id: UUID,
    session: Session = Depends(require_session),
    last_event_id: UUID | None = Header(default=None, alias="Last-Event-ID"),
) -> Response:
    connection_timer = Stopwatch()
    try:
        conversation, run = await state(request).store.find_run(
            run_id, session.principal.subject, session.principal.issuer
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
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
    sql_store = getattr(state(request), "sql_store", None)
    if sql_store is not None:
        await publisher.hydrate(await sql_store.event_history(run_id))
    if last_event_id is not None and not await publisher.contains(run_id, last_event_id):
        raise HTTPException(status_code=410, detail="event cursor is no longer available")
    replay = await publisher.history(run_id, last_event_id)

    stream_metadata = {
        "trace_id": run.id.hex,
        "run_id": str(run.id),
        "conversation_id": str(conversation.id),
    }
    state(request).metrics.increment(
        "aura.runtime.stream_delivery",
        "sse_connections",
        **stream_metadata,
    )
    connection_span_id = new_span_id()
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "sse.connect",
        connection_timer.elapsed_ms(),
        trace_id=run.id.hex,
        span_id=connection_span_id,
        parent_span_id=None,
        dependency="sse_client",
        outcome="ok",
        run_id=str(run.id),
        conversation_id=str(conversation.id),
    )
    if last_event_id is not None:
        state(request).metrics.increment(
            "aura.runtime.stream_delivery",
            "sse_reconnects",
            **stream_metadata,
        )
        state(request).metrics.record_span(
            "aura.runtime.stream_delivery",
            "sse.reconnect",
            0.0,
            trace_id=run.id.hex,
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
            snapshot_timer.elapsed_ms(),
        )
        yield f"event: {snapshot.event_type}\ndata: {snapshot_payload}\n\n"
        await asyncio.sleep(0)
        if not replay and run.status.value in {
            "canceled",
            "completed",
            "failed",
            "interrupted",
        }:
            return
        async for event in publisher.stream(run_id, last_event_id):
            delivery_timer = Stopwatch()
            payload = json.dumps(event.payload(), separators=(",", ":"))
            cursor = "" if event.event_type == "heartbeat" else f"id: {event.event_id}\n"
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
    duration_ms: float,
) -> None:
    state(request).metrics.record_span(
        "aura.runtime.stream_delivery",
        "sse.deliver",
        duration_ms,
        trace_id=run_id.hex,
        span_id=new_span_id(),
        parent_span_id=connection_span_id,
        dependency="sse_client",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )
