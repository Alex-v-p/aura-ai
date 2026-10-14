"""In-process publisher used by API SSE and replaceable transport implementations."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from aura_core.domains.execution.runs.events import (
    RunEvent,
    new_event,
    validate_memory_activity,
)
from aura_core.domains.execution.runs.ports import MetricsPort, NullMetrics


def _without_durable_attachment_presentation(event: RunEvent) -> RunEvent:
    """Strip stale attachment presentation from replayable run snapshots."""

    if event.event_type != "run.snapshot":
        return event
    snapshot_run = event.data.get("run")
    if not isinstance(snapshot_run, dict) or "attachments" not in snapshot_run:
        return event
    sanitized_run = dict(cast(dict[str, object], snapshot_run))
    sanitized_run.pop("attachments", None)
    data = dict(event.data)
    data["run"] = sanitized_run
    return replace(event, data=data)


class EventPublisher:
    def __init__(self, metrics: MetricsPort | None = None) -> None:
        self._events: dict[UUID, list[RunEvent]] = defaultdict(list)
        self._waiters: dict[UUID, list[asyncio.Event]] = defaultdict(list)
        # The durable memory job is independently settled from the run.  The
        # status is kept separately from event history so a terminal run can
        # continue streaming until memory processing has settled.
        self._memory_processing_status: dict[UUID, str] = {}
        # Thinking is an ephemeral side channel.  Queues exist only while an
        # authenticated SSE request is actively consuming a run stream; they
        # are never included in history or hydrated from PostgreSQL.
        self._thinking_subscribers: dict[
            UUID, dict[UUID, asyncio.Queue[RunEvent]]
        ] = defaultdict(dict)
        self._thinking_offsets: dict[UUID, int] = {}
        self.metrics = metrics or NullMetrics()
        self._lock = asyncio.Lock()

    async def publish_thinking_delta(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        offset: int,
        text: str,
        parent_span_id: str | None = None,
    ) -> None:
        """Fan out one ordered, bounded thinking delta to active subscribers.

        This method intentionally has no persistence path.  Invalid or stale
        input is ignored so an optional private-reasoning side channel can
        never affect the ordinary answer stream.
        """

        # The in-process fan-out has no publication span of its own, but it
        # accepts the caller's correlation parent so all publisher
        # implementations share one seam.
        del parent_span_id
        if (
            not isinstance(cast(object, run_id), UUID)
            or not isinstance(cast(object, conversation_id), UUID)
            or type(offset) is not int
            or offset < 0
            or not isinstance(cast(object, text), str)
            or not text
            or len(text) > 4096
        ):
            self.metrics.increment(
                "aura.runtime.stream_delivery",
                "thinking_stream_outcome",
                outcome="invalid",
            )
            return
        async with self._lock:
            subscribers = self._thinking_subscribers.get(run_id)
            if not subscribers:
                self.metrics.increment(
                    "aura.runtime.stream_delivery",
                    "thinking_stream_outcome",
                    outcome="no_subscriber",
                    run_id=str(run_id),
                    conversation_id=str(conversation_id),
                )
                # There is no replay buffer.  A delta that arrives while no
                # authorized stream is active is intentionally discarded.
                return
            expected = self._thinking_offsets.get(run_id, 0)
            if offset != expected:
                self.metrics.increment(
                    "aura.runtime.stream_delivery",
                    "thinking_stream_outcome",
                    outcome="out_of_order",
                    run_id=str(run_id),
                    conversation_id=str(conversation_id),
                )
                return
            if expected + len(text) > 65_536:
                self.metrics.increment(
                    "aura.runtime.stream_delivery",
                    "thinking_stream_outcome",
                    outcome="over_budget",
                    run_id=str(run_id),
                    conversation_id=str(conversation_id),
                )
                return
            self._thinking_offsets[run_id] = expected + len(text)
            event = new_event(
                "model.thinking.delta",
                run_id,
                conversation_id,
                offset,
                {"offset": offset, "text": text},
            )
            for queue in tuple(subscribers.values()):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.metrics.increment(
                        "aura.runtime.stream_delivery",
                        "thinking_stream_outcome",
                        outcome="queue_full",
                        run_id=str(run_id),
                        conversation_id=str(conversation_id),
                    )
                    # A slow client must not retain private reasoning or block
                    # provider execution.  Subsequent ordered deltas fail
                    # closed at the offset gate.
                    continue

    async def publish(self, event: RunEvent) -> None:
        if event.event_type == "memory.activity":
            event = _validated_memory_event(event)
        await self._publish_event(event, preserve_sequence=False)

    async def _publish_event(self, event: RunEvent, *, preserve_sequence: bool) -> None:
        event = _without_durable_attachment_presentation(event)
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
                event = _without_durable_attachment_presentation(event)
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
            event = _without_durable_attachment_presentation(event)
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
        subscriber_id = uuid4()
        thinking_queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=128)
        async with self._lock:
            self._thinking_subscribers[run_id][subscriber_id] = thinking_queue
        try:
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
                # Drain ephemeral thinking before waiting for another signal.
                try:
                    yield thinking_queue.get_nowait()
                    continue
                except asyncio.QueueEmpty:
                    pass
                # A memory settlement is itself the terminal boundary when
                # the run status was delivered before the activity event.
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
                wait_task = asyncio.create_task(waiter.wait())
                thinking_task = asyncio.create_task(thinking_queue.get())
                try:
                    done, _ = await asyncio.wait(
                        {wait_task, thinking_task}, timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if thinking_task in done:
                        yield thinking_task.result()
                    elif not done:
                        existing = await self.history(run_id)
                        conversation_id = (
                            existing[-1].conversation_id if existing else UUID(int=0)
                        )
                        heartbeat = new_event(
                            "heartbeat",
                            run_id,
                            conversation_id,
                            len(existing),
                            {"serverTime": datetime.now(UTC)},
                        )
                        # Heartbeats are connection liveness, not business
                        # history or cross-process fan-out.
                        yield heartbeat
                finally:
                    for task in (wait_task, thinking_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(wait_task, thinking_task, return_exceptions=True)
                    async with self._lock:
                        if waiter in self._waiters[run_id]:
                            self._waiters[run_id].remove(waiter)
        finally:
            async with self._lock:
                subscribers = self._thinking_subscribers.get(run_id)
                if subscribers is not None:
                    subscribers.pop(subscriber_id, None)
                    if not subscribers:
                        self._thinking_subscribers.pop(run_id, None)
                        self._thinking_offsets.pop(run_id, None)


class PersistentEventPublisher(EventPublisher):
    """Persist every event before exposing it to local subscribers."""

    def __init__(
        self,
        persist: Callable[[RunEvent], Awaitable[RunEvent | None]],
        publish_external: Callable[[RunEvent], Awaitable[None]] | None = None,
        metrics: MetricsPort | None = None,
    ) -> None:
        super().__init__(metrics=metrics)
        self._persist = persist
        self._publish_external = publish_external

    async def publish(self, event: RunEvent) -> None:
        if event.event_type == "memory.activity":
            event = _validated_memory_event(event)
        # Sanitize at the durable boundary, before the persistence callback is
        # invoked. This protects every current and future run.snapshot
        # producer, including events returned by a persistence adapter.
        event = _without_durable_attachment_presentation(event)
        if await self.contains(event.run_id, event.event_id):
            return
        persisted = await self._persist(event)
        if persisted is not None:
            event = _without_durable_attachment_presentation(persisted)
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
