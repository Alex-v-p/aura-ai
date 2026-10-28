"""Worker delivery semantics for identifier-only workflow wakeups."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.entrypoints.worker.app import run_resume_once, run_workflow_once
from aura_core.platform.outbox import RUN_RESUME_TOPIC, WORKFLOW_WAKE_TOPIC


class _Consumer:
    def __init__(self, command: object | None) -> None:
        self.command = command
        self.delivery_count = 1
        self.acked = 0
        self.rejected: list[str] = []
        self.nacked: list[tuple[float, str]] = []

    async def receive(self) -> object | None:
        return self.command

    async def ack(self) -> None:
        self.acked += 1

    async def reject(self, *, error_class: str) -> None:
        self.rejected.append(error_class)

    async def nack(self, *, retry_delay: float, error_class: str) -> None:
        self.nacked.append((retry_delay, error_class))


def _missing_identifier(_name: str) -> None:
    return None


def _command(*, topic: str = RUN_RESUME_TOPIC, event_id: UUID | None = None) -> object:
    event_id = event_id or uuid4()

    def identifier(name: str) -> str | None:
        return str(event_id) if name == "eventId" else None

    # Deliberately no content, result, prompt, or workflow payload fields.
    return SimpleNamespace(topic=topic, identifier=identifier)


def _workflow_command(
    *, topic: str = WORKFLOW_WAKE_TOPIC, instance_id: UUID | None = None
) -> object:
    instance_id = instance_id or uuid4()

    def identifier(name: str) -> str | None:
        return str(instance_id) if name in {"instanceId", "aggregateId"} else None

    return SimpleNamespace(topic=topic, identifier=identifier)


@pytest.mark.asyncio
async def test_resume_worker_acks_only_after_handler_success_and_passes_identifier() -> None:
    event_id = uuid4()
    consumer = _Consumer(_command(event_id=event_id))
    received: list[UUID] = []

    async def handler(value: UUID) -> None:
        received.append(value)

    await run_resume_once(cast(Any, consumer), handler)

    assert received == [event_id]
    assert consumer.acked == 1
    assert consumer.rejected == []
    assert consumer.nacked == []


@pytest.mark.asyncio
async def test_resume_worker_rejects_poison_topic_or_missing_identifier() -> None:
    for command in (_command(topic="aura.workflow.invalid.v1"), SimpleNamespace(
        topic=RUN_RESUME_TOPIC, identifier=_missing_identifier
    )):
        consumer = _Consumer(command)

        async def handler(_value: UUID) -> None:
            raise AssertionError("poison delivery must not reach the handler")

        await run_resume_once(cast(Any, consumer), handler)
        assert consumer.acked == 0
        assert consumer.rejected == ["validation"]
        assert consumer.nacked == []


@pytest.mark.asyncio
async def test_resume_worker_nacks_transient_timeout_and_does_not_ack() -> None:
    consumer = _Consumer(_command())
    consumer.delivery_count = 3

    async def handler(_value: UUID) -> None:
        raise TimeoutError("synthetic dependency timeout")

    await run_resume_once(cast(Any, consumer), handler)

    assert consumer.acked == 0
    assert consumer.rejected == []
    assert len(consumer.nacked) == 1
    retry_delay, error_class = consumer.nacked[0]
    assert 0 < retry_delay <= 60
    assert error_class == "delivery"


@pytest.mark.asyncio
async def test_resume_worker_propagates_cancellation_without_ack_or_retry() -> None:
    consumer = _Consumer(_command())

    async def handler(_value: UUID) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_resume_once(cast(Any, consumer), handler)

    assert consumer.acked == 0
    assert consumer.rejected == []
    assert consumer.nacked == []


@pytest.mark.asyncio
async def test_workflow_worker_reloads_by_identifier_and_acks_after_continuation() -> None:
    instance_id = uuid4()
    consumer = _Consumer(_workflow_command(instance_id=instance_id))
    received: list[UUID] = []

    async def handler(value: UUID) -> None:
        received.append(value)

    await run_workflow_once(cast(Any, consumer), handler)

    assert received == [instance_id]
    assert consumer.acked == 1
    assert consumer.rejected == []
    assert consumer.nacked == []


@pytest.mark.asyncio
async def test_workflow_worker_rejects_poison_or_missing_identity_and_retries_transient_failure(
) -> None:
    for command in (
        _workflow_command(topic="aura.workflow.invalid.v1"),
        SimpleNamespace(topic=WORKFLOW_WAKE_TOPIC, identifier=_missing_identifier),
    ):
        consumer = _Consumer(command)
        await run_workflow_once(cast(Any, consumer), lambda _value: asyncio.sleep(0))
        assert consumer.rejected == ["validation"]
        assert consumer.acked == 0

    consumer = _Consumer(_workflow_command())
    consumer.delivery_count = 2

    async def transient(_value: UUID) -> None:
        raise RuntimeError("database unavailable")

    await run_workflow_once(cast(Any, consumer), transient)
    assert consumer.acked == 0
    assert consumer.rejected == []
    assert consumer.nacked and consumer.nacked[0][1] == "delivery"
