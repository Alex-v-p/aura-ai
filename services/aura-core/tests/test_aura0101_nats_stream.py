# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Regression coverage for Core-owned AURA_RUNS stream reconciliation."""

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import nats
import pytest
from aura_core.domains.execution.tasks.contracts import TaskSourceInvalidation
from aura_core.domains.execution.tasks.transport import NatsTaskSourceInvalidationConsumer
from aura_core.platform.outbox.nats import (
    AURA_RUNS_STREAM,
    NatsJsonEventConsumer,
    _ensure_command_stream,
)
from aura_core.platform.outbox.nats import (
    asyncio as nats_asyncio,
)
from aura_core.platform.outbox.service import RUN_COMMAND_TOPIC
from nats.js.api import AckPolicy, ConsumerConfig


def _stream_info(name: str, subjects: object) -> Any:
    return SimpleNamespace(config=SimpleNamespace(name=name, subjects=subjects))


def _task_source_event() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "eventId": str(uuid4()),
        "eventType": "task.source_invalidated",
        "sourceKind": "memory_reindex",
        "sourceId": str(uuid4()),
        "sourceVersion": 4,
        "occurredAt": "2026-10-28T12:00:00+00:00",
        "correlationId": str(uuid4()),
        "causationId": str(uuid4()),
    }


class _TaskMessage:
    def __init__(self, payload: dict[str, object]) -> None:
        self.data = json.dumps(payload).encode()
        self.acked = 0

    async def ack(self) -> None:
        self.acked += 1


class _TaskSubscription:
    def __init__(self, message: _TaskMessage) -> None:
        self.message = message
        self.used = False

    async def fetch(self, _count: int, *, timeout: float) -> list[_TaskMessage]:
        del timeout
        if self.used:
            await asyncio.sleep(0)
            return []
        self.used = True
        return [self.message]


@pytest.mark.asyncio
async def test_task_source_consumer_acks_after_reconciliation() -> None:
    message = _TaskMessage(_task_source_event())
    consumer = NatsTaskSourceInvalidationConsumer("nats://unused")
    consumer._consumer._subscription = _TaskSubscription(message)  # type: ignore[attr-defined]
    stop = asyncio.Event()
    received: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> None:
        received.append(payload)
        stop.set()

    await consumer.consume(handler, stop)
    assert received and message.acked == 1


@pytest.mark.asyncio
async def test_task_source_consumer_leaves_delivery_unacked_on_failure() -> None:
    message = _TaskMessage(_task_source_event())
    consumer = NatsTaskSourceInvalidationConsumer("nats://unused")
    consumer._consumer._subscription = _TaskSubscription(message)  # type: ignore[attr-defined]
    stop = asyncio.Event()

    async def handler(_payload: dict[str, object]) -> None:
        stop.set()
        raise RuntimeError("projection unavailable")

    with pytest.raises(RuntimeError, match="projection unavailable"):
        await consumer.consume(handler, stop)
    assert message.acked == 0


@pytest.mark.asyncio
async def test_generic_json_consumer_redelivers_after_restart() -> None:
    """A durable handler failure survives a connection restart and redelivers."""

    connection_url = os.environ.get("AURA_TEST_NATS_URL")
    if not connection_url:
        pytest.skip("AURA_TEST_NATS_URL is required for JetStream restart coverage")

    subject = f"aura.tests.json.{uuid4().hex}.v1"
    durable = f"aura-test-json-{uuid4().hex[:16]}"
    nats_module: Any = nats
    management: Any = await nats_module.connect(connection_url)
    jetstream: Any = management.jetstream()
    first: NatsJsonEventConsumer | None = None
    second: NatsJsonEventConsumer | None = None
    try:
        await _ensure_command_stream(jetstream, (subject,))
        await jetstream.add_consumer(
            AURA_RUNS_STREAM,
            config=ConsumerConfig(
                durable_name=durable,
                filter_subject=subject,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=0.25,
                max_deliver=5,
            ),
        )
        payload = {
            "schemaVersion": 1,
            "eventId": str(uuid4()),
            "eventType": "test.event",
        }
        await jetstream.publish(subject, json.dumps(payload).encode())

        first = NatsJsonEventConsumer(
            connection_url,
            subject=subject,
            parser=lambda event: event,
            durable=durable,
        )
        await first.connect()
        first_stop = asyncio.Event()

        async def fail_once(_event: Any) -> None:
            first_stop.set()
            raise RuntimeError("simulated projection restart")

        with pytest.raises(RuntimeError, match="simulated projection restart"):
            await first.consume(fail_once, first_stop)
        await first.close()
        first = None

        second = NatsJsonEventConsumer(
            connection_url,
            subject=subject,
            parser=lambda event: event,
            durable=durable,
        )
        await second.connect()
        second_stop = asyncio.Event()
        received: list[dict[str, object]] = []

        async def settle(event: Any) -> None:
            received.append(event)
            second_stop.set()

        await asyncio.wait_for(second.consume(settle, second_stop), timeout=5)
        assert received == [payload]
    finally:
        if first is not None:
            await first.close()
        if second is not None:
            await second.close()
        await jetstream.delete_consumer(AURA_RUNS_STREAM, durable)
        info = await jetstream.stream_info(AURA_RUNS_STREAM)
        raw_subjects = getattr(info.config, "subjects", ())
        subjects = [value for value in raw_subjects or () if value != subject]
        await jetstream.update_stream(name=AURA_RUNS_STREAM, subjects=subjects)
        await management.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schemaVersion", "1"),
        ("sourceVersion", 2_147_483_648),
        ("sourceVersion", True),
        ("sourceId", 123),
        ("occurredAt", "2026-10-28T12:00:00"),
    ),
)
async def test_task_source_consumer_terminally_acks_contract_poison(
    field: str, value: object
) -> None:
    payload = _task_source_event()
    payload[field] = value
    stop = asyncio.Event()

    class PoisonMessage:
        data = json.dumps(payload).encode()
        acked = 0

        async def ack(self) -> None:
            self.acked += 1
            stop.set()

    message = PoisonMessage()

    class PoisonSubscription:
        used = False

        async def fetch(self, _count: int, *, timeout: float) -> list[PoisonMessage]:
            del timeout
            if self.used:
                await asyncio.sleep(0)
                return []
            self.used = True
            return [message]

    consumer = NatsTaskSourceInvalidationConsumer("nats://unused")
    consumer._consumer._subscription = PoisonSubscription()  # type: ignore[attr-defined]
    received: list[dict[str, object]] = []

    async def handler(event: dict[str, object]) -> None:
        received.append(event)

    await consumer.consume(handler, stop)
    assert not received
    assert message.acked == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schemaVersion", False),
        ("sourceVersion", "1"),
        ("sourceVersion", 0),
        ("eventId", 123),
        ("sourceKind", "not-a-task-kind"),
        ("eventType", "task.source_invalidated.v2"),
    ),
)
def test_task_source_invalidation_rejects_coercive_or_closed_contract_values(
    field: str, value: object
) -> None:
    payload = _task_source_event()
    payload[field] = value
    with pytest.raises(ValueError):
        TaskSourceInvalidation.from_payload(payload)


class RecordingJetStream:
    """Small provider double that records every stream mutation attempt."""

    def __init__(self, *, add_error: Exception, lookups: list[object]) -> None:
        self.add_error = add_error
        self.lookups = list(lookups)
        self.add_calls: list[tuple[str, tuple[str, ...]]] = []
        self.lookup_calls: list[str] = []
        self.update_calls: list[tuple[str, tuple[str, ...]]] = []
        self.delete_calls: list[str] = []

    async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
        self.add_calls.append((name, subjects))
        raise self.add_error

    async def stream_info(self, name: str) -> Any:
        self.lookup_calls.append(name)
        result = self.lookups.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def update_stream(self, *, name: str, subjects: list[str]) -> None:
        self.update_calls.append((name, tuple(subjects)))

    async def delete_stream(self, name: str) -> None:
        self.delete_calls.append(name)


@pytest.mark.asyncio
async def test_fresh_stream_deduplicates_default_subject() -> None:
    class FreshJetStream(RecordingJetStream):
        async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
            self.add_calls.append((name, subjects))

    jetstream = FreshJetStream(add_error=RuntimeError("unused"), lookups=[])
    await _ensure_command_stream(
        jetstream, (RUN_COMMAND_TOPIC, RUN_COMMAND_TOPIC)
    )

    assert jetstream.add_calls == [(AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC,))]
    assert jetstream.lookup_calls == []
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_fresh_stream_keeps_custom_subject_after_default_in_stable_order() -> None:
    custom_subject = "aura.runs.execute.custom.v1"

    class FreshJetStream(RecordingJetStream):
        async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
            self.add_calls.append((name, subjects))

    jetstream = FreshJetStream(add_error=RuntimeError("unused"), lookups=[])
    await _ensure_command_stream(
        jetstream,
        (RUN_COMMAND_TOPIC, custom_subject, RUN_COMMAND_TOPIC, custom_subject),
    )

    assert jetstream.add_calls == [
        (AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC, custom_subject))
    ]


@pytest.mark.asyncio
async def test_existing_stream_adds_only_missing_subjects_in_place() -> None:
    custom_subject = "aura.runs.execute.custom.v1"
    creation_error = RuntimeError("stream already exists")
    existing_subjects = ("aura.runs.execute.legacy.v1", RUN_COMMAND_TOPIC)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[_stream_info(AURA_RUNS_STREAM, existing_subjects)],
    )

    await _ensure_command_stream(
        jetstream, (RUN_COMMAND_TOPIC, custom_subject, custom_subject)
    )

    assert jetstream.update_calls == [
        (
            AURA_RUNS_STREAM,
            existing_subjects + (custom_subject,),
        )
    ]
    assert jetstream.delete_calls == []
    assert jetstream.lookup_calls == [AURA_RUNS_STREAM]


@pytest.mark.asyncio
async def test_lost_create_race_retries_until_named_stream_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("concurrent create lost")
    sleeps: list[float] = []

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            RuntimeError("stream not visible yet"),
            _stream_info(AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC,)),
        ],
    )

    await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert jetstream.lookup_calls == [AURA_RUNS_STREAM, AURA_RUNS_STREAM]
    assert sleeps == [0.1]
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_permanent_stream_absence_reraises_original_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("provider rejected stream creation")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            RuntimeError("not found") for _ in range(3)
        ],
    )

    with pytest.raises(RuntimeError) as caught:
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert caught.value is creation_error
    assert jetstream.lookup_calls == [AURA_RUNS_STREAM] * 3
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_malformed_subject_metadata_fails_closed_without_mutation() -> None:
    creation_error = RuntimeError("stream already exists")
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[_stream_info(AURA_RUNS_STREAM, None)],
    )

    with pytest.raises(RuntimeError, match="subject configuration is unavailable"):
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_differently_named_stream_fails_closed_and_preserves_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("provider rejected stream creation")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            _stream_info("OTHER_STREAM", (RUN_COMMAND_TOPIC,))
            for _ in range(3)
        ],
    )

    with pytest.raises(RuntimeError) as caught:
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert caught.value is creation_error
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []
