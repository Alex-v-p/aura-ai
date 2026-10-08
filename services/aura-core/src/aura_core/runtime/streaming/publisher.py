"""In-process publisher used by API SSE and replaceable transport implementations."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from aura_core.domains.execution.runs.events import (
    RunEvent,
    new_event,
    validate_memory_activity,
)


class EventPublisher:
    def __init__(self) -> None:
        self._events: dict[UUID, list[RunEvent]] = defaultdict(list)
        self._waiters: dict[UUID, list[asyncio.Event]] = defaultdict(list)
        # The durable memory job is independently settled from the run.  The
        # status is kept separately from event history so a terminal run can
        # continue streaming until memory processing has settled.
        self._memory_processing_status: dict[UUID, str] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: RunEvent) -> None:
        if event.event_type == "memory.activity":
            event = _validated_memory_event(event)
        await self._publish_event(event, preserve_sequence=False)

    async def _publish_event(self, event: RunEvent, *, preserve_sequence: bool) -> None:
        async with self._lock:
            if any(item.event_id == event.event_id for item in self._events[event.run_id]):
                return
            if not preserve_sequence:
                current = self._events.get(event.run_id, [])
                next_sequence = max((item.sequence for item in current), default=-1) + 1
                event = replace(event, sequence=next_sequence)
            self._events[event.run_id].append(event)
            self._events[event.run_id].sort(key=lambda item: (item.sequence, str(item.event_id)))
            self._observe_memory_event(event)
            for waiter in self._waiters[event.run_id]:
                waiter.set()

    async def hydrate(self, events: list[RunEvent]) -> None:
        async with self._lock:
            for event in events:
                if event.event_type == "memory.activity":
                    event = _validated_memory_event(event)
                if not any(item.event_id == event.event_id for item in self._events[event.run_id]):
                    self._events[event.run_id].append(event)
                    self._observe_memory_event(event)
            for run_events in self._events.values():
                run_events.sort(key=lambda item: (item.sequence, str(item.event_id)))

    async def ingest_external(self, event: RunEvent) -> None:
        """Add a cross-process event and wake active SSE subscribers."""

        async with self._lock:
            if any(item.event_id == event.event_id for item in self._events[event.run_id]):
                return
            if event.event_type == "memory.activity":
                event = _validated_memory_event(event)
            self._events[event.run_id].append(event)
            self._events[event.run_id].sort(key=lambda item: (item.sequence, str(item.event_id)))
            self._observe_memory_event(event)
            for waiter in self._waiters[event.run_id]:
                waiter.set()

    async def set_memory_processing_status(self, run_id: UUID, status: str) -> None:
        """Publish a durable snapshot of processing state to active streams.

        This is intentionally metadata-only and idempotent.  ``queued`` and
        ``running`` keep a completed run open; ``settled`` allows it to close.
        """

        if status not in {"queued", "running", "settled"}:
            raise ValueError("unsupported memory processing status")
        async with self._lock:
            self._memory_processing_status[run_id] = status
            for waiter in self._waiters[run_id]:
                waiter.set()

    async def memory_processing_status(self, run_id: UUID) -> str | None:
        async with self._lock:
            return self._memory_processing_status.get(run_id)

    async def has_pending_memory_activity(self, run_id: UUID) -> bool:
        async with self._lock:
            status = self._memory_processing_status.get(run_id)
            if status in {"queued", "running"}:
                events = self._events.get(run_id, [])
                queued = {
                    event.data.get("id")
                    for event in events
                    if event.event_type == "memory.activity"
                    and event.data.get("status") == "queued"
                }
                settled = {
                    event.data.get("id")
                    for event in events
                    if event.event_type == "memory.activity"
                    and event.data.get("status") in {"completed", "failed"}
                }
                has_activity = bool(queued or settled)
                return not has_activity or bool(queued - settled)
            return False

    def _observe_memory_event(self, event: RunEvent) -> None:
        if event.event_type != "memory.activity":
            return
        activity_status = event.data.get("status")
        if activity_status == "queued":
            self._memory_processing_status[event.run_id] = "queued"
        elif activity_status in {"completed", "failed"} and not any(
            item.event_type == "memory.activity"
            and item.data.get("status") == "queued"
            and item.data.get("id") != event.data.get("id")
            for item in self._events.get(event.run_id, [])
        ):
            self._memory_processing_status[event.run_id] = "settled"

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
                    if not await self.has_pending_memory_activity(run_id):
                        return
            # A memory settlement is itself the terminal boundary when the
            # run status was delivered before the activity event.  Re-check
            # both durable signals after the batch so this ordering cannot
            # leave subscribers open indefinitely.
            if not await self.has_pending_memory_activity(run_id):
                remaining = await self.history(run_id, cursor)
                existing = await self.history(run_id)
                if remaining:
                    continue
                if any(
                    item.event_type == "run.status"
                    and item.data.get("status")
                    in {"canceled", "completed", "failed", "interrupted"}
                    for item in existing
                ):
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
        persist: Callable[[RunEvent], Awaitable[RunEvent | None]],
        publish_external: Callable[[RunEvent], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._persist = persist
        self._publish_external = publish_external

    async def publish(self, event: RunEvent) -> None:
        if event.event_type == "memory.activity":
            event = _validated_memory_event(event)
        if await self.contains(event.run_id, event.event_id):
            return
        persisted = await self._persist(event)
        if persisted is not None:
            event = persisted
        if self._publish_external is not None:
            await self._publish_external(event)
        await super()._publish_event(event, preserve_sequence=True)


def _validated_memory_event(event: RunEvent) -> RunEvent:
    """Validate and normalize an activity before persistence or fan-out."""

    if event.schema_version != 1 or event.event_type != "memory.activity":
        raise ValueError("unsupported memory activity event version")
    data = validate_memory_activity(event.data)
    return RunEvent(
        event.event_id,
        event.sequence,
        event.event_type,
        event.run_id,
        event.conversation_id,
        event.occurred_at,
        data,
        event.schema_version,
    )
