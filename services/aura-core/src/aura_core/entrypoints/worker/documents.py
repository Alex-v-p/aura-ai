"""Durable document processing worker boundary."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast
from uuid import UUID

from aura_core.domains.knowledge.documents.public import DocumentService
from aura_core.runtime.models.ports import ProviderTraceContext


class DocumentCommand(Protocol):
    id: UUID
    aggregate_id: UUID | None
    topic: str
    correlation_id: UUID | None
    causation_id: UUID | None


def _command_context(command: DocumentCommand) -> ProviderTraceContext:
    command_id = getattr(command, "id", None) or command.aggregate_id
    if command_id is None:
        raise ValueError("document command is missing an identifier")
    correlation = command.correlation_id or command_id
    causation = command.causation_id or command_id
    return ProviderTraceContext(
        trace_id=correlation.hex,
        span_id=causation.hex[:16],
        correlation_id=str(correlation),
        causation_id=str(causation),
        command_id=str(command_id),
    )


class DocumentWorker:
    def __init__(self, service: DocumentService) -> None:
        self.service = service

    async def process(
        self, document_id: UUID, *, context: ProviderTraceContext | None = None
    ) -> object:
        return await self.service.process(document_id, context=context)

    async def delete(
        self, document_id: UUID, *, context: ProviderTraceContext | None = None
    ) -> object:
        return await self.service.delete_by_id(document_id, context=context)

    async def process_command(self, command: DocumentCommand) -> object:
        if command.topic != "aura.knowledge.documents.process.v1" or command.aggregate_id is None:
            raise ValueError("invalid document processing command")
        return await self.process(command.aggregate_id, context=_command_context(command))

    async def delete_command(self, command: DocumentCommand) -> object:
        if command.topic != "aura.knowledge.documents.delete.v1" or command.aggregate_id is None:
            raise ValueError("invalid document deletion command")
        return await self.delete(command.aggregate_id, context=_command_context(command))


def _record_consumer_metric(
    worker: DocumentWorker,
    name: str,
    *,
    outcome: str,
    component: str,
    context: ProviderTraceContext,
) -> None:
    metrics = worker.service.metrics
    if metrics is not None:
        try:
            metrics.increment(
                component,
                name,
                trace_id=context.trace_id,
                outcome=outcome,
                dependency="nats_jetstream",
                command_id=context.command_id or "",
                correlation_id=context.correlation_id or "",
                causation_id=context.causation_id or "",
            )
        except Exception:
            pass


async def run_document_once(worker: DocumentWorker, consumer: object) -> None:
    """Ack document commands only after atomic Core settlement."""

    command = await consumer.receive()  # type: ignore[attr-defined]
    if command is None:
        return
    typed_command = cast(DocumentCommand, command)
    context = _command_context(typed_command)
    transport_owns_metrics = bool(getattr(consumer, "emits_consumer_metrics", False))

    def record(name: str, *, outcome: str) -> None:
        if not transport_owns_metrics:
            _record_consumer_metric(
                worker, name, outcome=outcome, component=component, context=context
            )

    component = (
        "aura.knowledge.document_deletion"
        if typed_command.topic == "aura.knowledge.documents.delete.v1"
        else "aura.knowledge.document_processing"
    )
    if not transport_owns_metrics:
        _record_consumer_metric(
            worker, "consumer_received", outcome="ok", component=component, context=context
        )
    redelivered = int(getattr(consumer, "delivery_count", 1)) > 1
    if redelivered:
        if not transport_owns_metrics:
            _record_consumer_metric(
                worker, "consumer_redelivered", outcome="ok", component=component, context=context
            )
    try:
        if typed_command.topic == "aura.knowledge.documents.process.v1":
            result = await worker.process_command(typed_command)
        elif typed_command.topic == "aura.knowledge.documents.delete.v1":
            result = await worker.delete_command(typed_command)
        else:
            raise ValueError("unexpected document command topic")
        status = getattr(getattr(result, "status", None), "value", None)
        if status not in {"ready", "failed", "deleting"}:
            raise RuntimeError("document command did not reach a terminal state")
        if (
            typed_command.topic == "aura.knowledge.documents.delete.v1"
            and getattr(result, "failure_code", None) == "delete_retryable"
        ):
            raise RuntimeError("document deletion remains retryable")
        if (
            typed_command.topic == "aura.knowledge.documents.delete.v1"
            and status == "deleting"
            and bool(getattr(result, "object_key", ""))
        ):
            raise RuntimeError("document deletion did not settle a tombstone")
    except (ValueError, TypeError):
        record("consumer_rejected", outcome="failed")
        await consumer.reject(error_class="validation")  # type: ignore[attr-defined]
        return
    except Exception:
        record("consumer_nacked", outcome="failed")
        await consumer.nack(error_class="delivery")  # type: ignore[attr-defined]
        return
    record("consumer_acknowledged", outcome="ok")
    result_status = getattr(getattr(result, "status", None), "value", None)
    if redelivered and typed_command.topic.endswith("process.v1") and result_status == "ready":
        record("outbox_duplicates", outcome="duplicate")
    await consumer.ack()  # type: ignore[attr-defined]


async def run_document_loop(worker: DocumentWorker, consumer: object) -> None:
    while True:
        try:
            await run_document_once(worker, consumer)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.2)
