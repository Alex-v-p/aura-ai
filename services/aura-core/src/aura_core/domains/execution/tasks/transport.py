"""Execution-owned task invalidation transport adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.platform.outbox import NatsJsonEventConsumer, OutboxCommand, SqlOutboxRepository

from .contracts import TASK_SOURCE_INVALIDATED_TOPIC, TaskSourceInvalidation


def parse_task_source_invalidation(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate and normalize the closed TaskSourceInvalidated v1 envelope."""

    return TaskSourceInvalidation.from_payload(payload).payload()


def make_task_source_invalidation_command(
    payload: Mapping[str, object],
) -> OutboxCommand:
    """Build the execution-owned task event over the generic outbox command."""

    event = TaskSourceInvalidation.from_payload(payload)
    return OutboxCommand(
        event.event_id,
        TASK_SOURCE_INVALIDATED_TOPIC,
        None,
        None,
        event.occurred_at,
        event.correlation_id,
        event.causation_id,
        schema_version=event.schema_version,
        aggregate_type="execution.task_projection",
        aggregate_id=event.source_id,
        event_payload=event.payload(),
    )


class TaskSourceInvalidationStage:
    """Bootstrap-composed source lifecycle staging adapter."""

    def __init__(self, outbox: SqlOutboxRepository | None = None) -> None:
        self._outbox = outbox or SqlOutboxRepository()

    async def stage_task_source_invalidation(
        self, session: AsyncSession, *, payload: Mapping[str, object]
    ) -> UUID:
        return await self._outbox.stage_command(
            session, command=make_task_source_invalidation_command(payload)
        )


TaskSourceInvalidationHandler = Callable[[dict[str, object]], Awaitable[None]]


class NatsTaskSourceInvalidationConsumer:
    """Execution adapter over the generic platform JSON JetStream consumer."""

    def __init__(
        self,
        connection_url: str,
        *,
        durable: str = "aura-core-task-projection",
        subject: str = TASK_SOURCE_INVALIDATED_TOPIC,
    ) -> None:
        self._consumer = NatsJsonEventConsumer(
            connection_url,
            subject=subject,
            parser=parse_task_source_invalidation,
            durable=durable,
        )

    async def connect(self) -> None:
        await self._consumer.connect()

    async def consume(
        self,
        handler: TaskSourceInvalidationHandler,
        stop: asyncio.Event,
    ) -> None:
        async def adapt(payload: Mapping[str, object]) -> None:
            await handler(dict(payload))

        await self._consumer.consume(adapt, stop)

    async def close(self) -> None:
        await self._consumer.close()


__all__ = [
    "NatsTaskSourceInvalidationConsumer",
    "TaskSourceInvalidationHandler",
    "TaskSourceInvalidationStage",
    "make_task_source_invalidation_command",
    "parse_task_source_invalidation",
]
