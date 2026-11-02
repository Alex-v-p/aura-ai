import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.events import RunEvent, new_event, validate_memory_activity
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.telemetry import MetadataMetrics, new_span_id
from aura_core.runtime.streaming.publisher import EventPublisher
from nats.errors import TimeoutError as NatsTimeoutError


class _JetStreamConfig:
    def __init__(self, name: str, subjects: tuple[str, ...]) -> None:
        self.name = name
        self.subjects = subjects


class _JetStreamInfo:
    def __init__(self, name: str, subjects: tuple[str, ...]) -> None:
        self.config = _JetStreamConfig(name, subjects)


class _FakeJetStream:
    def __init__(self, subjects: tuple[str, ...] = ()) -> None:
        self.subjects = subjects

    async def streams_info(self) -> list[_JetStreamInfo]:
        return [_JetStreamInfo("AURA_RUNS", self.subjects)]


async def _next_event(stream: AsyncIterator[RunEvent]) -> RunEvent:
    return await stream.__anext__()


async def _close_stream(stream: AsyncIterator[RunEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if callable(close):
        await cast(Callable[[], Awaitable[None]], close)()


def _activity(status: str, *, action: str = "created") -> dict[str, object]:
    timestamp = datetime.now(UTC).isoformat()
    return {
        "id": str(uuid4()),
        "action": action,
        "status": status,
        "scope": {"type": "user"},
        "candidateId": str(uuid4()),
        "memoryId": str(uuid4()),
        "memoryRevisionId": str(uuid4()),
        "policyRevisionId": str(uuid4()),
        "embeddingGenerationId": str(uuid4()),
        "reconciliationStatus": "pending" if status == "queued" else "authoritative",
        "occurredAt": timestamp,
    }


@pytest.mark.asyncio
async def test_publisher_replays_from_event_cursor() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    first = new_event(
        "run.status",
        run_id,
        conversation_id,
        0,
        {"status": "running", "startedAt": None, "finishedAt": None},
    )
    second = new_event(
        "run.status",
        run_id,
        conversation_id,
        1,
        {"status": "completed", "startedAt": None, "finishedAt": None},
    )
    await publisher.publish(first)
    await publisher.publish(second)
    events = [event async for event in publisher.stream(run_id, first.event_id)]
    assert [event.event_id for event in events] == [second.event_id]


@pytest.mark.asyncio
async def test_publisher_serializes_concurrent_event_sequences() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    events = [
        new_event(
            "run.status",
            run_id,
            conversation_id,
            0,
            {"status": "running", "startedAt": None, "finishedAt": None},
        )
        for _ in range(24)
    ]
    await asyncio.gather(*(publisher.publish(event) for event in events))
    history = await publisher.history(run_id)
    assert [event.sequence for event in history] == list(range(len(events)))
    assert {event.event_id for event in history} == {event.event_id for event in events}


def test_memory_activity_validation_rejects_private_fields() -> None:
    data = _activity("queued")
    data["content"] = "private memory"
    with pytest.raises(ValueError):
        validate_memory_activity(data)


@pytest.mark.asyncio
async def test_terminal_run_stays_open_until_memory_activity_settles() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    await publisher.set_memory_processing_status(run_id, "queued")
    terminal = new_event(
        "run.status",
        run_id,
        conversation_id,
        1,
        {"status": "completed", "startedAt": None, "finishedAt": None},
    )
    queued_data = _activity("queued")
    completed_data = dict(_activity("completed"), id=queued_data["id"])
    queued = new_event("memory.activity", run_id, conversation_id, 2, queued_data)
    completed = new_event("memory.activity", run_id, conversation_id, 3, completed_data)
    await publisher.publish(terminal)
    await publisher.publish(queued)
    stream = publisher.stream(run_id)
    assert (await anext(stream)).event_type == "run.status"
    assert (await anext(stream)).event_type == "memory.activity"
    await asyncio.sleep(0)
    await publisher.publish(completed)
    assert (await anext(stream)).event_type == "memory.activity"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(_next_event(stream), timeout=0.1)


@pytest.mark.asyncio
async def test_nats_event_wakeup_contains_only_persisted_identifiers() -> None:
    class Connection:
        def __init__(self) -> None:
            self.subject = ""
            self.payload = b""

        async def publish(self, subject: str, payload: bytes) -> None:
            self.subject = subject
            self.payload = payload

    connection = Connection()
    transport = NatsOutbox("nats://unused")
    cast(Any, transport)._connection = connection
    cast(Any, transport)._jetstream = _FakeJetStream()
    event = new_event("assistant.delta", uuid4(), uuid4(), 1, {"text": "private content"})

    await transport.publish_event(event)

    assert connection.subject.endswith(str(event.run_id))
    assert json.loads(connection.payload) == {
        "eventId": str(event.event_id),
        "runId": str(event.run_id),
    }
    assert b"private content" not in connection.payload


@pytest.mark.asyncio
async def test_nats_consumer_recovers_after_idle_fetch_timeout() -> None:
    command_id, run_id, conversation_id = uuid4(), uuid4(), uuid4()

    class Message:
        data = json.dumps(
            {
                "commandId": str(command_id),
                "runId": str(run_id),
                "conversationId": str(conversation_id),
                "createdAt": datetime.now(UTC).isoformat(),
                "correlationId": str(run_id),
                "causationId": str(run_id),
            }
        ).encode()

    class Subscription:
        calls = 0

        async def fetch(self, batch: int, timeout: int) -> list[Message]:
            del batch, timeout
            self.calls += 1
            if self.calls == 1:
                raise NatsTimeoutError
            return [Message()]

    consumer = NatsRunConsumer("nats://unused")
    cast(Any, consumer)._subscription = Subscription()

    assert await consumer.receive() is None
    command = await consumer.receive()
    assert command is not None
    assert command.id == command_id
    assert command.run_id == run_id


@pytest.mark.asyncio
async def test_thinking_fanout_is_live_only_and_not_replayed() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    stream = publisher.stream(run_id)
    pending = asyncio.create_task(_next_event(stream))
    await asyncio.sleep(0)

    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=0, text="private"
    )
    event = await asyncio.wait_for(pending, timeout=0.2)
    assert event.event_type == "model.thinking.delta"
    assert event.data == {"offset": 0, "text": "private"}
    assert await publisher.history(run_id) == []
    await _close_stream(stream)


@pytest.mark.asyncio
async def test_thinking_fanout_reaches_multiple_active_subscribers() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    first = publisher.stream(run_id)
    second = publisher.stream(run_id)
    first_pending = asyncio.create_task(_next_event(first))
    second_pending = asyncio.create_task(_next_event(second))
    await asyncio.sleep(0)

    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=0, text="delta"
    )
    received = await asyncio.gather(
        asyncio.wait_for(first_pending, timeout=0.2),
        asyncio.wait_for(second_pending, timeout=0.2),
    )
    assert [event.data["text"] for event in received] == ["delta", "delta"]
    await _close_stream(first)
    await _close_stream(second)


@pytest.mark.asyncio
async def test_nats_thinking_envelope_is_strict_and_non_jetstream() -> None:
    class Connection:
        def __init__(self) -> None:
            self.messages: list[tuple[str, bytes]] = []
            self.callback: Any = None

        async def publish(self, subject: str, payload: bytes) -> None:
            self.messages.append((subject, payload))

        async def subscribe(self, subject: str, cb: Any) -> object:
            assert subject == "aura.runs.thinking.v1"
            self.callback = cb
            return object()

    connection = Connection()
    metrics = MetadataMetrics()
    transport = NatsOutbox("nats://unused", metrics=metrics)
    cast(Any, transport)._connection = connection
    cast(Any, transport)._jetstream = _FakeJetStream()
    run_id, conversation_id = uuid4(), uuid4()
    parent_span_id = new_span_id()
    await transport.publish_thinking_delta(
        run_id=run_id,
        conversation_id=conversation_id,
        offset=0,
        text="private",
        parent_span_id=parent_span_id,
    )
    assert len(connection.messages) == 1
    subject, payload = connection.messages[0]
    assert subject == "aura.runs.thinking.v1"
    assert json.loads(payload) == {
        "runId": str(run_id),
        "conversationId": str(conversation_id),
        "offset": 0,
        "text": "private",
    }
    assert cast(Any, transport)._jetstream is not None

    received: list[tuple[UUID, UUID, int, str]] = []

    async def handler(
        received_run: UUID, received_conversation: UUID, offset: int, text: str
    ) -> None:
        received.append((received_run, received_conversation, offset, text))

    await transport.subscribe_thinking_events(handler)
    valid = {
        "runId": str(run_id),
        "conversationId": str(conversation_id),
        "offset": 1,
        "text": "ok",
    }
    await connection.callback(type("Message", (), {"data": json.dumps(valid).encode()})())
    invalid = {**valid, "extra": "reject"}
    await connection.callback(type("Message", (), {"data": json.dumps(invalid).encode()})())
    await connection.callback(
        type("Message", (), {"data": b"not-json"})()
    )
    assert received == [(run_id, conversation_id, 1, "ok")]
    publication = next(
        item for item in metrics.snapshot() if item.metric == "stream_delivery_duration_ms"
    )
    assert dict(publication.dimensions) == {
        "dependency": "nats_transient",
        "outcome": "ok",
    }
    assert publication.parent_span_id == parent_span_id
    assert any(
        item.metric == "thinking_stream_outcome"
        and dict(item.dimensions).get("outcome") == "transport_published"
        for item in metrics.snapshot()
    )
    stage_outcomes = [
        dict(item.dimensions).get("outcome")
        for item in metrics.snapshot()
        if item.metric == "thinking_stream_outcome"
    ]
    assert stage_outcomes.count("transport_published") == 1
    assert "published" not in stage_outcomes
    assert "delivered" not in stage_outcomes


@pytest.mark.asyncio
async def test_thinking_fanout_rejects_invalid_order_and_per_run_budget_without_history() -> None:
    publisher = EventPublisher()
    run_id, conversation_id = uuid4(), uuid4()
    stream = publisher.stream(run_id)
    first_pending = asyncio.create_task(_next_event(stream))
    await asyncio.sleep(0)

    chunk = "x" * 4_096
    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=0, text=chunk
    )
    first = await asyncio.wait_for(first_pending, timeout=0.2)
    assert first.data == {"offset": 0, "text": chunk}

    # A duplicate/out-of-order frame and an oversized frame are fail-closed;
    # neither advances the expected offset for the valid next frame.
    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=1, text="stale"
    )
    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=4_096, text="x" * 4_097
    )
    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=4_096, text="next"
    )
    second = await asyncio.wait_for(_next_event(stream), timeout=0.2)
    assert second.data == {"offset": 4_096, "text": "next"}

    offset = 4_100
    remaining = 65_536 - offset
    queued_events = 0
    while remaining:
        size = min(4_096, remaining)
        await publisher.publish_thinking_delta(
            run_id=run_id,
            conversation_id=conversation_id,
            offset=offset,
            text="x" * size,
        )
        offset += size
        remaining -= size
        queued_events += 1

    for _ in range(queued_events):
        await asyncio.wait_for(_next_event(stream), timeout=0.2)

    over_budget = asyncio.create_task(_next_event(stream))
    await publisher.publish_thinking_delta(
        run_id=run_id, conversation_id=conversation_id, offset=65_536, text="overflow"
    )
    await asyncio.sleep(0.01)
    assert not over_budget.done()
    over_budget.cancel()
    await asyncio.gather(over_budget, return_exceptions=True)
    assert await publisher.history(run_id) == []
    await _close_stream(stream)


@pytest.mark.asyncio
async def test_nats_thinking_subscription_drops_wrong_types_and_boundaries() -> None:
    class Connection:
        def __init__(self) -> None:
            self.callback: Any = None

        async def subscribe(self, subject: str, cb: Any) -> object:
            assert subject == "aura.runs.thinking.v1"
            self.callback = cb
            return object()

    connection = Connection()
    metrics = MetadataMetrics()
    transport = NatsOutbox("nats://unused", metrics=metrics)
    cast(Any, transport)._connection = connection
    cast(Any, transport)._jetstream = _FakeJetStream()
    received: list[str] = []

    async def handler(
        run_id: UUID, conversation_id: UUID, offset: int, text: str
    ) -> None:
        del run_id, conversation_id, offset
        received.append(text)

    await transport.subscribe_thinking_events(handler)
    run_id, conversation_id = uuid4(), uuid4()
    for payload in (
        {
            "runId": str(run_id),
            "conversationId": str(conversation_id),
            "offset": -1,
            "text": "bad",
        },
        {
            "runId": str(run_id),
            "conversationId": str(conversation_id),
            "offset": 0.0,
            "text": "bad",
        },
        {
            "runId": str(run_id),
            "conversationId": str(conversation_id),
            "offset": 0,
            "text": "",
        },
        {
            "runId": str(run_id),
            "conversationId": str(conversation_id),
            "offset": 0,
            "text": "x" * 4_097,
        },
        {
            "runId": "not-a-uuid",
            "conversationId": str(conversation_id),
            "offset": 0,
            "text": "bad",
        },
    ):
        await connection.callback(type("Message", (), {"data": json.dumps(payload).encode()})())
    await connection.callback(
        type(
            "Message",
            (),
            {
                "data": json.dumps(
                    {
                        "runId": str(run_id),
                        "conversationId": str(conversation_id),
                        "offset": 0,
                        "text": "valid",
                    }
                ).encode()
            },
        )()
    )
    assert received == ["valid"]
    malformed = [
        item
        for item in metrics.snapshot()
        if item.metric == "thinking_stream_outcome"
        and dict(item.dimensions).get("outcome") == "malformed_envelope"
    ]
    assert len(malformed) == 5
    assert all(dict(item.dimensions).get("dependency") == "nats_transient" for item in malformed)
    assert all(dict(item.dimensions).get("error_class") == "validation" for item in malformed)


@pytest.mark.asyncio
async def test_nats_thinking_publish_failure_preserves_parent_and_error_metadata() -> None:
    class Connection:
        async def publish(self, subject: str, payload: bytes) -> None:
            del subject, payload
            raise RuntimeError("transient transport failure")

    metrics = MetadataMetrics()
    transport = NatsOutbox("nats://unused", metrics=metrics)
    cast(Any, transport)._connection = Connection()
    cast(Any, transport)._jetstream = _FakeJetStream()
    run_id, conversation_id = uuid4(), uuid4()
    parent_span_id = new_span_id()
    with pytest.raises(RuntimeError, match="transient transport failure"):
        await transport.publish_thinking_delta(
            run_id=run_id,
            conversation_id=conversation_id,
            offset=0,
            text="private reasoning must not be recorded",
            parent_span_id=parent_span_id,
        )

    failure = next(
        item
        for item in metrics.snapshot()
        if item.kind == "span" and dict(item.dimensions).get("outcome") == "error"
    )
    assert failure.parent_span_id == parent_span_id
    assert dict(failure.dimensions) == {
        "dependency": "nats_transient",
        "error_class": "delivery",
        "outcome": "error",
    }
    rendered = repr(metrics.snapshot())
    assert "private reasoning must not be recorded" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retained_subject",
    ["aura.runs.thinking.v1", "aura.runs.>", "aura.>", "aura.runs.*.v1"],
)
async def test_nats_rejects_jetstream_retention_overlap_before_subscription(
    retained_subject: str,
) -> None:
    class Connection:
        async def publish(self, subject: str, payload: bytes) -> None:
            del subject, payload
            raise AssertionError("must fail before publication")

        async def subscribe(self, subject: str, cb: Any) -> object:
            del subject, cb
            raise AssertionError("must fail before subscription")

    transport = NatsOutbox("nats://unused")
    cast(Any, transport)._connection = Connection()
    cast(Any, transport)._jetstream = _FakeJetStream((retained_subject,))
    with pytest.raises(RuntimeError, match="overlaps configured JetStream retention"):
        await transport.subscribe_thinking_events(
            lambda run_id, conversation_id, offset, text: asyncio.sleep(0)
        )
    with pytest.raises(RuntimeError, match="overlaps configured JetStream retention"):
        await transport.publish_thinking_delta(
            run_id=uuid4(), conversation_id=uuid4(), offset=0, text="private"
        )
