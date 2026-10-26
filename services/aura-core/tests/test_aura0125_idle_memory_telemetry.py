"""AURA-0125 deterministic acceptance coverage for memory idle telemetry.

These tests deliberately exercise the Core-owned seams with in-memory stores and
manual clocks.  They do not wait for JetStream redelivery or a housekeeping
interval: time is advanced by replacing the clock value, so retry and duplicate
delivery assertions remain stable in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.knowledge.memory.public import (
    MEMORY_PROCESSING_TOPIC,
    MemoryKind,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingService,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
    ProcessingJobStatus,
)
from aura_core.entrypoints.worker.app import emit_memory_activity, run_memory_once
from aura_core.platform.outbox import make_identifier_command
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.streaming.publisher import EventPublisher

ISSUER = "https://issuer.example"
OWNER = "aura-0125-owner"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def __call__(self, **event: object) -> None:
        self.events.append(dict(event))


class _Inference:
    async def infer(self, request: object) -> dict[str, object]:
        del request
        return {"action": "ignore"}


class _Embedding:
    async def embed(self, model_id: str, text: str, *, context: object = None) -> object:
        del model_id, text, context
        raise AssertionError("idle-memory acceptance must not invoke an embedding provider")


class _FailingStore(MemoryStore):
    async def list_memories(
        self, issuer: str, subject: str, filters: object = None
    ) -> list[object]:
        del issuer, subject, filters
        raise RuntimeError("database unavailable")


class _Consumer:
    delivery_count = 1

    def __init__(self, command: MemoryProcessingCommand) -> None:
        self.command = _envelope(command)
        self.ack_count = 0
        self.nacks: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []

    async def receive(self) -> object:
        return self.command

    async def ack(self) -> None:
        self.ack_count += 1

    async def nack(self, **kwargs: object) -> None:
        self.nacks.append(dict(kwargs))

    async def reject(self, **kwargs: object) -> None:
        self.rejections.append(dict(kwargs))


def _command(*, job_id: UUID | None = None) -> MemoryProcessingCommand:
    run_id = job_id or uuid4()
    return MemoryProcessingCommand(
        uuid4(),
        run_id,
        run_id,
        uuid4(),
        uuid4(),
        run_id,
        uuid4(),
        uuid4(),
        uuid4(),
    )


def _envelope(command: MemoryProcessingCommand) -> object:
    return make_identifier_command(
        MEMORY_PROCESSING_TOPIC,
        command.command_id,
        command.run_id,
        command.conversation_id,
        correlation_id=command.correlation_id,
        causation_id=command.causation_id,
        identifiers=(
            ("jobId", command.job_id),
            ("agentRevisionId", command.agent_revision_id),
            ("userMessageId", command.user_message_id),
            ("assistantMessageId", command.assistant_message_id),
        ),
    )


def _job(command: MemoryProcessingCommand, *, available_at: datetime = NOW) -> MemoryProcessingJob:
    return MemoryProcessingJob(
        command.job_id,
        ISSUER,
        OWNER,
        command.run_id,
        command.conversation_id,
        available_at=available_at,
        correlation_id=command.correlation_id,
        causation_id=command.causation_id,
        agent_revision_id=command.agent_revision_id,
        user_message_ids=(command.user_message_id,),
        assistant_message_ids=(command.assistant_message_id,),
    )


def _service(store: MemoryStore, telemetry: _Telemetry) -> MemoryProcessingService:
    return MemoryProcessingService(
        store,
        _Inference(),
        _Embedding(),
        clock=lambda: NOW,
        maintenance_telemetry=telemetry,
    )


@pytest.mark.asyncio
async def test_empty_and_active_noop_housekeeping_do_not_emit_success_observations() -> None:
    telemetry = _Telemetry()
    store = MemoryStore(clock=lambda: NOW)
    service = _service(store, telemetry)

    assert await service.maintain(ISSUER, OWNER, now=NOW) == 0
    await store.create_memory(
        ISSUER,
        OWNER,
        content="A stable retained fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=365,
    )
    assert await service.maintain(ISSUER, OWNER, now=NOW) == 0

    assert telemetry.events == []


@pytest.mark.asyncio
async def test_lifecycle_transition_is_retained_and_discovery_failure_is_immediate() -> None:
    transition_telemetry = _Telemetry()
    store = MemoryStore(clock=lambda: NOW)
    service = _service(store, transition_telemetry)
    await store.create_memory(
        ISSUER,
        OWNER,
        content="A fact that has expired",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=30,
        valid_to=NOW - timedelta(seconds=1),
    )

    assert await service.maintain(ISSUER, OWNER, now=NOW) == 1
    assert [event["operation"] for event in transition_telemetry.events] == [
        "memory.decay",
        "memory.maintenance",
    ]
    assert transition_telemetry.events[0]["outcome"] == "ok"

    failure_telemetry = _Telemetry()
    failing = _service(_FailingStore(clock=lambda: NOW), failure_telemetry)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await failing.maintain(ISSUER, OWNER, now=NOW)
    assert len(failure_telemetry.events) == 1
    failure = failure_telemetry.events[0]
    assert failure["operation"] == "memory.error"
    assert failure["outcome"] == "error"
    assert failure["error_class"] == "persistence"


@pytest.mark.asyncio
async def test_retry_claim_is_due_only_when_postgresql_available_at_is_due() -> None:
    current = [NOW]
    store = MemoryStore(clock=lambda: current[0])
    command = _command()
    job = await store.enqueue_processing_job(
        _job(command, available_at=NOW + timedelta(minutes=1))
    )

    assert await store.claim_processing_job_by_id(job.id, ISSUER, OWNER) is None
    current[0] = NOW + timedelta(minutes=1)
    claimed = await store.claim_processing_job_by_id(job.id, ISSUER, OWNER)
    assert claimed is not None
    assert claimed.attempt_count == 1

    parked = await store.settle_processing_job(
        job.id,
        claimed.lease_id,  # type: ignore[arg-type]
        issuer=ISSUER,
        subject=OWNER,
        retryable=True,
        error_class="unconfigured",
    )
    assert parked.status is ProcessingJobStatus.RETRYABLE
    assert parked.last_error_class == "unconfigured"
    assert parked.available_at > current[0]
    assert await store.claim_processing_job_by_id(job.id, ISSUER, OWNER) is None

    current[0] = parked.available_at
    redelivery = await store.claim_processing_job_by_id(job.id, ISSUER, OWNER)
    assert redelivery is not None
    assert redelivery.attempt_count == 2


@pytest.mark.asyncio
async def test_transient_failure_and_non_durable_settlement_are_negatively_acknowledged() -> None:
    command = _command()

    class TransientProcessor:
        async def process_command(self, received: MemoryProcessingCommand) -> object:
            assert received.command_id == command.command_id
            raise TimeoutError("provider timeout")

    transient = _Consumer(command)
    await run_memory_once(TransientProcessor(), transient)  # type: ignore[arg-type]
    assert transient.ack_count == 0
    assert len(transient.nacks) == 1
    assert transient.nacks[0]["retry_delay"] == 1.0
    assert transient.nacks[0]["error_class"] in {"provider", "timeout"}

    class NonDurableProcessor:
        async def process_command(self, received: MemoryProcessingCommand) -> object:
            assert received.command_id == command.command_id
            return SimpleNamespace(status=ProcessingJobStatus.RETRYABLE, terminal=False)

    non_durable = _Consumer(command)
    await run_memory_once(NonDurableProcessor(), non_durable)  # type: ignore[arg-type]
    assert non_durable.ack_count == 0
    assert non_durable.nacks == [{"retry_delay": 1.0, "error_class": "delivery"}]


@pytest.mark.asyncio
async def test_durable_retryable_settlement_is_acknowledged_without_waiting_for_redelivery(
) -> None:
    command = _command()
    store = MemoryStore(clock=lambda: NOW)
    job = await store.enqueue_processing_job(_job(command))
    service = _service(store, _Telemetry())
    service.jobs[(ISSUER, OWNER, command.run_id)] = job
    result = await service.process_command(command)

    assert result.status is ProcessingJobStatus.RETRYABLE
    assert result.settled is True
    settled = await store.get_processing_job(job.id, ISSUER, OWNER)
    assert settled.status is ProcessingJobStatus.RETRYABLE

    consumer = _Consumer(command)
    await run_memory_once(service, consumer)  # type: ignore[arg-type]
    assert consumer.ack_count == 1
    assert consumer.nacks == []


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_republish_queued_or_completed_activity() -> None:
    command = _command()

    class Processor:
        async def process_command(self, received: MemoryProcessingCommand) -> object:
            assert received.command_id == command.command_id
            return SimpleNamespace(status=ProcessingJobStatus.COMPLETED, terminal=True)

    publisher = EventPublisher()
    metrics = MetadataMetrics()

    async def emit(received: MemoryProcessingCommand, status: str, result: object | None) -> None:
        await emit_memory_activity(
            publisher,
            MemoryStore(clock=lambda: NOW),
            metrics,
            received,
            status,
            result,
        )

    first = _Consumer(command)
    await run_memory_once(Processor(), first, emit)  # type: ignore[arg-type]
    duplicate = _Consumer(command)
    duplicate.delivery_count = 2
    await run_memory_once(Processor(), duplicate, emit)  # type: ignore[arg-type]

    history = await publisher.history(command.run_id)
    assert [event.event_type for event in history] == ["memory.activity", "memory.activity"]
    assert [event.data["status"] for event in history] == ["queued", "completed"]
    assert len({event.event_id for event in history}) == 2
    assert first.ack_count == duplicate.ack_count == 1


@pytest.mark.asyncio
async def test_real_transition_telemetry_and_activity_ids_remain_deterministic() -> None:
    command = _command()
    publisher = EventPublisher()
    metrics = MetadataMetrics()
    result = SimpleNamespace(
        candidate=SimpleNamespace(
            action=SimpleNamespace(value="create"),
            state=SimpleNamespace(value="accepted"),
        ),
        revision_id=None,
        policy_revision_id=None,
        embedding_generation_id=None,
    )
    await emit_memory_activity(
        publisher,
        MemoryStore(clock=lambda: NOW),
        metrics,
        command,
        "completed",
        result,
    )
    await emit_memory_activity(
        publisher,
        MemoryStore(clock=lambda: NOW),
        metrics,
        command,
        "completed",
        result,
    )
    events = await publisher.history(command.run_id)
    assert len(events) == 1
    assert isinstance(events[0], RunEvent)
    assert events[0].data["status"] == "completed"
    assert events[0].data["action"] == "created"
