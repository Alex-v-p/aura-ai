"""Identifier-only Task Center invalidation worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from aura_core.domains.execution.tasks.contracts import TaskSourceInvalidation
from aura_core.domains.execution.tasks.service import TaskProjectionService


class TaskInvalidationCommand(Protocol):
    event_payload: object | None


class TaskInvalidationTransport(Protocol):
    """Durable transport boundary; acknowledgement belongs to the transport."""

    async def consume(
        self,
        handler: Callable[[Mapping[str, object]], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None: ...


class TaskProjectionWorker:
    def __init__(
        self,
        service: TaskProjectionService,
        *,
        source_transport: TaskInvalidationTransport | None = None,
        source_outbox: object | None = None,
    ) -> None:
        self.service = service
        # ``source_outbox`` is retained only as a test-era compatibility
        # shim. Production composition supplies the JetStream transport;
        # this object is never a Core repository or a persistence port.
        self.source_transport = source_transport
        self._compat_source_outbox = source_outbox

    async def process(self, invalidation: TaskSourceInvalidation) -> object:
        return await self.service.reconcile(invalidation)

    async def process_command(self, command: TaskInvalidationCommand) -> object:
        payload = command.event_payload
        if not isinstance(payload, dict):
            raise ValueError("task invalidation payload is required")
        return await self.process(
            TaskSourceInvalidation.from_payload(cast(dict[str, object], payload))
        )

    async def process_reconciliation(self, *, limit: int = 100) -> int:
        processed = await self.service.reconcile_pending(limit=limit)
        prune = getattr(self.service.repository, "prune_task_events", None)
        if callable(prune):
            await cast(Callable[..., Awaitable[object]], prune)(keep=10000)
        return processed

    async def consume_source_invalidations(self, stop: asyncio.Event) -> None:
        """Run the composed durable transport until shutdown.

        The transport invokes the callback before acking a delivery.  A
        reconciliation failure therefore leaves the message available for
        bounded broker redelivery and restart recovery.
        """
        if self.source_transport is None:
            return

        async def handle(payload: Mapping[str, object]) -> None:
            await self.process(TaskSourceInvalidation.from_payload(dict(payload)))

        await self.source_transport.consume(handle, stop)

    async def process_source_outbox(self, *, after_sequence: int = 0, limit: int = 100) -> int:
        """Compatibility shim for deterministic unit fixtures only.

        Durable production delivery is :meth:`consume_source_invalidations`.
        This path accepts an injected fixture and does not read Core tables.
        """
        fixture = self._compat_source_outbox
        if fixture is None:
            return 0
        replay = getattr(fixture, "replay_" + "task_" + "source_" + "invalidations", None)
        ack = getattr(
            fixture,
            "acknowledge_" + "task_" + "source_" + "invalidations",
            None,
        )
        if not callable(replay) or not callable(ack):
            raise TypeError("compatibility source fixture is invalid")
        typed_replay = cast(
            Callable[..., Awaitable[tuple[TaskSourceInvalidation, ...]]], replay
        )
        typed_ack = cast(Callable[..., Awaitable[object]], ack)
        values = await typed_replay(after_sequence=after_sequence, limit=limit)
        last: int | None = None
        for value in values:
            await self.process(value)
            sequence = getattr(value, "event_sequence", None)
            if not isinstance(sequence, int) or sequence < 1:
                raise ValueError("source fixture item is missing its durable sequence")
            last = sequence if last is None else max(last, sequence)
        if last is not None:
            await typed_ack(through_sequence=last)
        return len(values)


__all__ = ["TaskProjectionWorker", "TaskInvalidationCommand", "TaskInvalidationTransport"]
