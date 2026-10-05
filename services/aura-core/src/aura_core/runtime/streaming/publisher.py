"""In-process publisher used by API SSE and replaceable transport implementations."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from aura_core.domains.execution.runs.events import RunEvent, new_event


class EventPublisher:
    def __init__(self) -> None:
        self._events: dict[UUID, list[RunEvent]] = defaultdict(list)
        self._waiters: dict[UUID, list[asyncio.Event]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, event: RunEvent) -> None:
        async with self._lock:
            self._events[event.run_id].append(event)
            for waiter in self._waiters[event.run_id]:
                waiter.set()

    async def hydrate(self, events: list[RunEvent]) -> None:
        async with self._lock:
            for event in events:
                if not any(item.event_id == event.event_id for item in self._events[event.run_id]):
                    self._events[event.run_id].append(event)

    async def ingest_external(self, event: RunEvent) -> None:
        """Add a cross-process event and wake active SSE subscribers."""

        async with self._lock:
            if any(item.event_id == event.event_id for item in self._events[event.run_id]):
                return
            self._events[event.run_id].append(event)
            for waiter in self._waiters[event.run_id]:
                waiter.set()

    async def history(self, run_id: UUID, after: UUID | None = None) -> list[RunEvent]:
        async with self._lock:
            events = list(self._events.get(run_id, []))
        if after is None:
            return events
        positions = [index for index, event in enumerate(events) if event.event_id == after]
        return events[positions[0] + 1 :] if positions else events

    async def contains(self, run_id: UUID, event_id: UUID) -> bool:
        async with self._lock:
            return any(event.event_id == event_id for event in self._events.get(run_id, []))

    async def stream(self, run_id: UUID, after: UUID | None = None) -> AsyncIterator[RunEvent]:
        cursor = after
        while True:
            pending = await self.history(run_id, cursor)
            for event in pending:
                cursor = event.event_id
                yield event
                status = event.data.get("status")
                if event.event_type == "run.snapshot":
                    snapshot_run = event.data.get("run")
                    if isinstance(snapshot_run, dict):
                        candidate = cast(dict[str, object], snapshot_run).get("status")
                        status = candidate if isinstance(candidate, str) else None
                if event.event_type in {"run.status", "run.snapshot"} and status in {
                    "canceled",
                    "completed",
                    "failed",
                    "interrupted",
                }:
                    return
            waiter = asyncio.Event()
            async with self._lock:
                self._waiters[run_id].append(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=15.0)
            except TimeoutError:
                existing = await self.history(run_id)
                conversation_id = existing[-1].conversation_id if existing else UUID(int=0)
                heartbeat = new_event(
                    "heartbeat",
                    run_id,
                    conversation_id,
                    len(existing),
                    {"serverTime": datetime.now(UTC)},
                )
                # Heartbeats are connection liveness, not business history.
                # Yield them without persistence or cross-process fan-out.
                yield heartbeat
            finally:
                async with self._lock:
                    if waiter in self._waiters[run_id]:
                        self._waiters[run_id].remove(waiter)


class PersistentEventPublisher(EventPublisher):
    """Persist every event before exposing it to local subscribers."""

    def __init__(
        self,
        persist: Callable[[RunEvent], Awaitable[None]],
        publish_external: Callable[[RunEvent], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._persist = persist
        self._publish_external = publish_external

    async def publish(self, event: RunEvent) -> None:
        await self._persist(event)
        if self._publish_external is not None:
            await self._publish_external(event)
        await super().publish(event)
