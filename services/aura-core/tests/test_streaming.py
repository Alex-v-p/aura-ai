import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.events import new_event
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.runtime.streaming.publisher import EventPublisher
from nats.errors import TimeoutError as NatsTimeoutError


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
