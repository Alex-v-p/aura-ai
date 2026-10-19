"""AURA-0091 runner failure, privacy, replay, and artifact coverage.

These tests deliberately exercise the public transport, runner, replay, and
artifact ports with deterministic fakes.  They do not depend on an external
provider and never put evaluation content in an assertion message or report.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.evaluation_runner import (
    ConfiguredRuntimeEvidence,
    DocumentProcessingEvaluationReplay,
    SyntheticEvaluationScenario,
)
from aura_core.entrypoints.evaluation_runner.runner import (
    EvaluationRunner,
    NullResultPublisher,
    RunnerConfig,
)
from aura_core.entrypoints.evaluation_runner.state import (
    AttemptConflict,
    AttemptRecord,
    InMemoryEvaluationRunnerLedger,
)
from aura_core.platform.auth import Settings
from aura_core.platform.evaluation_transport import (
    EvaluationTransport,
    TransportValidationError,
    canonical_digest,
    deterministic_message_id,
    deterministic_terminal_id,
    validate_message,
)
from aura_core.platform.outbox.nats import EvaluationJetStreamConsumer
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.providers.object_storage.evaluation import (
    EvaluationArtifactAdapter,
    EvaluationArtifactError,
)
from aura_core.runtime.replay.registry import (
    ReplayContext,
    ReplayError,
    ReplayRegistry,
    ReplayResult,
)
from test_aura0091_core import _request  # pyright: ignore[reportPrivateUsage]


def _cancellation(
    request: dict[str, object], *, reason: str = "OWNER_REQUESTED"
) -> dict[str, object]:
    data = request["data"]
    assert isinstance(data, dict)
    return {
        **request,
        "messageId": str(uuid4()),
        "messageType": "evaluation.runner.cancellation_requested",
        "data": {
            "cancellationId": str(uuid4()),
            "reasonCode": reason,
            "deadlineAt": data["deadlineAt"],
        },
    }


class _Replay:
    component_id = "aura.knowledge.document_processing"
    component_version = "1.0.0"
    fixture_revision = "test-replay-fixture-v1"

    async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
        context.checkpoint()
        return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})


def _registry() -> ReplayRegistry:
    registry = ReplayRegistry()
    registry.register_replay(_Replay())
    return registry


@pytest.mark.asyncio
async def test_cancellation_identity_conflicts_are_permanent_and_tombstones_bind_request() -> None:
    ledger = InMemoryEvaluationRunnerLedger()
    payload = _request()
    message = validate_message(payload)
    data = payload["data"]
    assert isinstance(data, dict)
    deadline_value = cast(str, data["deadlineAt"])
    assert isinstance(deadline_value, str)
    deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
    await ledger.receive(
        AttemptRecord(
            message.attempt_id,
            message.request_id,
            message.job_id,
            message.idempotency_key,
            message.request_digest,
            request_projection=payload,
        )
    )
    with pytest.raises(AttemptConflict):
        await ledger.cancel(
            message.attempt_id,
            uuid4(),
            "OWNER_REQUESTED",
            request_id=uuid4(),
            job_id=message.job_id,
            idempotency_key=message.idempotency_key,
            request_digest=message.request_digest,
            deadline_at=deadline,
        )

    pending_id = uuid4()
    pending_key = uuid4()
    await ledger.cancel(
        pending_id,
        uuid4(),
        "OWNER_REQUESTED",
        request_id=message.request_id,
        job_id=message.job_id,
        idempotency_key=pending_key,
        request_digest=message.request_digest,
        deadline_at=deadline,
    )
    with pytest.raises(AttemptConflict):
        await ledger.cancel(
            pending_id,
            uuid4(),
            "OWNER_REQUESTED",
            request_id=uuid4(),
            job_id=message.job_id,
            idempotency_key=pending_key,
            request_digest=message.request_digest,
            deadline_at=deadline,
        )


@pytest.mark.asyncio
async def test_synthetic_model_call_is_charged_once_at_runner_boundary() -> None:
    for declared_calls, expected_type in (
        (1, "evaluation.runner.completed"),
        (0, "evaluation.runner.failed"),
    ):
        registry = ReplayRegistry()
        registry.register_synthetic(SyntheticEvaluationScenario())
        publisher = NullResultPublisher()
        runner = EvaluationRunner(
            InMemoryEvaluationRunnerLedger(),
            registry,
            publisher,
            config=RunnerConfig(max_model_calls=1),
        )
        payload = _request()
        data = cast(dict[str, object], payload["data"])
        target = cast(dict[str, object], data["targetComponent"])
        target.update(
            {
                "componentId": "aura.evaluation.synthetic",
                "componentVersion": "1.0.0",
            }
        )
        data["executionKind"] = "synthetic_end_to_end"
        provenance = cast(dict[str, object], data["provenance"])
        provenance["components"] = [target]
        limits = cast(dict[str, object], data["limits"])
        limits["maxModelCalls"] = declared_calls
        payload["requestDigest"] = canonical_digest(payload)

        await runner.accept(payload)
        record = await runner.run_once()

        assert record is not None
        assert publisher.messages[-1]["messageType"] == expected_type
        if declared_calls == 0:
            terminal_data = cast(dict[str, object], publisher.messages[-1]["data"])
            assert terminal_data["failureCode"] == "RESOURCE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_nats_identity_conflict_is_acknowledged_as_metered_poison() -> None:
    payload = _request()
    stop = asyncio.Event()

    class Message:
        data = json.dumps(payload).encode()
        acknowledged = False

        async def ack(self) -> None:
            self.acknowledged = True

    message = Message()

    class Subscription:
        delivered = False

        async def fetch(self, count: int, timeout: float) -> list[Message]:
            del count, timeout
            if self.delivered:
                return []
            self.delivered = True
            stop.set()
            return [message]

    subscription = Subscription()

    class JetStream:
        async def pull_subscribe(self, subject: str, *, durable: str, stream: str) -> Subscription:
            del subject, durable, stream
            return subscription

    class Connection:
        def jetstream(self) -> JetStream:
            return JetStream()

    class Metrics:
        calls: list[tuple[str, dict[str, str]]]

        def __init__(self) -> None:
            self.calls = []

        def increment(self, component: str, name: str, **attributes: str) -> None:
            del component
            self.calls.append((name, attributes))

    class ConflictRunner:
        async def accept(self, received: dict[str, object]) -> None:
            del received
            raise AttemptConflict("identity conflict")

    metrics = Metrics()
    await EvaluationJetStreamConsumer(Connection(), metrics=cast(Any, metrics)).consume(
        ConflictRunner(), stop
    )

    assert message.acknowledged
    assert (
        "evaluation_runner_nats_poison_count",
        {"dependency": "nats_jetstream", "outcome": "rejected", "error_class": "conflict"},
    ) in metrics.calls


@pytest.mark.asyncio
async def test_nats_subscription_retries_and_records_recovery() -> None:
    stop = asyncio.Event()

    class Subscription:
        async def fetch(self, count: int, timeout: float) -> list[Any]:
            del count, timeout
            stop.set()
            return []

    class JetStream:
        attempts = 0

        async def pull_subscribe(self, subject: str, *, durable: str, stream: str) -> Subscription:
            del subject, durable, stream
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary subscription failure")
            return Subscription()

    class Connection:
        def __init__(self) -> None:
            self.stream = JetStream()

        def jetstream(self) -> JetStream:
            return self.stream

    class Metrics:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def increment(self, component: str, name: str, **attributes: str) -> None:
            del component
            self.calls.append((name, attributes))

    metrics = Metrics()
    await EvaluationJetStreamConsumer(Connection(), metrics=cast(Any, metrics)).consume(
        object(), stop
    )
    assert any(name == "evaluation_runner_nats_drain_failure_count" for name, _ in metrics.calls)
    assert any(name == "evaluation_runner_nats_recovery_count" for name, _ in metrics.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("request_id", "job_id", "idempotency_key", "request_digest"))
async def test_cancellation_rejects_each_altered_identity_field(field: str) -> None:
    ledger = InMemoryEvaluationRunnerLedger()
    payload = _request()
    message = validate_message(payload)
    data = payload["data"]
    assert isinstance(data, dict)
    deadline = datetime.fromisoformat(
        cast(str, data["deadlineAt"]).replace("Z", "+00:00")
    )
    await ledger.receive(
        AttemptRecord(
            message.attempt_id,
            message.request_id,
            message.job_id,
            message.idempotency_key,
            message.request_digest,
            request_projection=payload,
        )
    )
    values: dict[str, UUID | str] = {
        "request_id": uuid4(),
        "job_id": uuid4(),
        "idempotency_key": uuid4(),
        "request_digest": "b" * 64,
    }
    identity: dict[str, UUID | str] = {
        "request_id": message.request_id,
        "job_id": message.job_id,
        "idempotency_key": message.idempotency_key,
        "request_digest": message.request_digest,
    }
    identity[field] = values[field]
    with pytest.raises(AttemptConflict):
        await ledger.cancel(
            message.attempt_id,
            uuid4(),
            "OWNER_REQUESTED",
            request_id=cast(UUID, identity["request_id"]),
            job_id=cast(UUID, identity["job_id"]),
            idempotency_key=cast(UUID, identity["idempotency_key"]),
            request_digest=cast(str, identity["request_digest"]),
            deadline_at=deadline,
        )


@pytest.mark.asyncio
async def test_cancellation_rejects_expired_identity_before_receipt() -> None:
    payload = _request()
    cancellation = _cancellation(payload)
    data = cancellation["data"]
    assert isinstance(data, dict)
    data["deadlineAt"] = "2020-01-01T00:00:00.000Z"
    with pytest.raises(AttemptConflict):
        await EvaluationRunner(
            InMemoryEvaluationRunnerLedger(), ReplayRegistry(), NullResultPublisher()
        ).accept(cancellation)


@pytest.mark.parametrize(
    ("location", "field", "value"),
    (
        ("envelope", "prompt", "content must not cross the boundary"),
        ("data", "response", "content must not cross the boundary"),
        ("data", "filename", "private.txt"),
        ("data", "toolArguments", {"secret": "value"}),
        ("data", "endpoint", "https://provider.invalid"),
        ("data", "bearerToken", "secret"),
        ("data", "attributes", {"arbitrary": "value"}),
        ("data", "ownerIdentity", "owner"),
    ),
)
def test_transport_rejects_content_credentials_and_arbitrary_fields(
    location: str, field: str, value: object
) -> None:
    payload = _request()
    if location == "envelope":
        payload[field] = value
    else:
        data = payload["data"]
        assert isinstance(data, dict)
        data[field] = value
    with pytest.raises(TransportValidationError):
        validate_message(payload)


def test_transport_rejects_non_finite_and_malformed_identity_values() -> None:
    def mutate_data(payload: dict[str, object], field: str, value: object) -> None:
        data = payload["data"]
        assert isinstance(data, dict)
        data[field] = value

    mutations: tuple[tuple[str, object] | tuple[str, str, object], ...] = (
        ("schemaVersion", 2),
        ("messageId", "not-a-uuid"),
        ("requestDigest", "z" * 64),
        ("data", "caseIds", ["not-a-uuid"]),
        ("data", "limits", {"invalid": True}),
    )
    for mutation in mutations:
        payload = _request()
        if len(mutation) == 2:
            field, value = mutation
            payload[field] = value
        else:
            _, field, value = mutation
            mutate_data(payload, field, value)
        with pytest.raises(TransportValidationError):
            validate_message(payload)


def test_transport_digest_and_terminal_ids_are_stable_and_canonical() -> None:
    payload = _request()
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    assert canonical_digest(payload) == canonical_digest(reordered)
    attempt_id = UUID(str(payload["attemptId"]))
    result_id = deterministic_terminal_id(attempt_id, "completed")
    assert result_id == deterministic_terminal_id(attempt_id, "completed")
    assert deterministic_message_id(result_id, "completed") == deterministic_message_id(
        result_id, "completed"
    )
    with pytest.raises(ValueError):
        deterministic_terminal_id(attempt_id, "queued")


@pytest.mark.asyncio
async def test_transport_validates_before_publish_and_isolates_provider_outage() -> None:
    class Connection:
        def __init__(self) -> None:
            self.published: list[tuple[str, bytes]] = []

        async def publish(self, subject: str, payload: bytes) -> None:
            self.published.append((subject, payload))

        async def subscribe(self, subject: str, *, cb: object) -> tuple[str, object]:
            return subject, cb

    connection = Connection()
    transport = EvaluationTransport(connection)
    await transport.publish_request(_request())
    assert len(connection.published) == 1
    assert connection.published[0][0].endswith("requested.v1")

    malformed = _request()
    malformed["prompt"] = "forbidden"
    with pytest.raises(TransportValidationError):
        await transport.publish_request(malformed)
    assert len(connection.published) == 1

    with pytest.raises(TransportValidationError):
        await EvaluationTransport(connection, max_bytes=1).publish_request(_request())

    class Offline:
        pass

    with pytest.raises(RuntimeError, match="transport is unavailable"):
        await EvaluationTransport(Offline()).publish_result(_request())


@pytest.mark.asyncio
async def test_transport_subscribes_only_to_the_runner_request_subject() -> None:
    class Connection:
        async def subscribe(self, subject: str, *, cb: object) -> tuple[str, object]:
            return subject, cb

    connection = Connection()
    transport = EvaluationTransport(connection)
    callback = object()
    subscription = cast(
        tuple[str, object], await transport.subscribe_requests(callback)
    )
    assert subscription[0] == "aura.evaluation.runner.requested.v1"
    assert subscription[1] is callback

    with pytest.raises(RuntimeError, match="transport is unavailable"):
        await EvaluationTransport(object()).subscribe_requests(callback)


@pytest.mark.asyncio
async def test_ledger_duplicate_delivery_is_idempotent_and_conflicts_fail_closed() -> None:
    ledger = InMemoryEvaluationRunnerLedger()
    payload = _request()
    message = validate_message(payload)
    attempt = AttemptRecord(
        message.attempt_id,
        message.request_id,
        message.job_id,
        message.idempotency_key,
        message.request_digest,
    )
    first = await ledger.receive(attempt)
    duplicate = await ledger.receive(attempt)
    assert duplicate == first
    with pytest.raises(AttemptConflict):
        await ledger.receive(
            AttemptRecord(
                message.attempt_id,
                message.request_id,
                message.job_id,
                message.idempotency_key,
                "b" * 64,
            )
        )
    with pytest.raises(AttemptConflict):
        await ledger.terminal(message.attempt_id, {"status": "invalid"})


@pytest.mark.asyncio
async def test_ledger_lease_allows_one_owner_until_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryEvaluationRunnerLedger(clock=lambda: now)
    payload = _request()
    message = validate_message(payload)
    await ledger.receive(
        AttemptRecord(
            message.attempt_id,
            message.request_id,
            message.job_id,
            message.idempotency_key,
            message.request_digest,
        )
    )
    first = await ledger.claim(message.attempt_id, "runner-a", timedelta(seconds=30))
    assert first is not None
    assert await ledger.claim(message.attempt_id, "runner-b", timedelta(seconds=30)) is None
    now = now + timedelta(seconds=31)
    second = await ledger.claim(message.attempt_id, "runner-b", timedelta(seconds=30))
    assert second is not None and second.lease_owner == "runner-b"


@pytest.mark.asyncio
async def test_ledger_renewal_recovery_and_attempt_exhaustion_are_fenced() -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    ledger = InMemoryEvaluationRunnerLedger(clock=lambda: now[0])
    payload = _request()
    message = validate_message(payload)
    await ledger.receive(
        AttemptRecord(
            message.attempt_id,
            message.request_id,
            message.job_id,
            message.idempotency_key,
            message.request_digest,
            request_projection=payload,
        )
    )
    leased = await ledger.claim(
        message.attempt_id, "runner-a", timedelta(seconds=30), max_attempts=1
    )
    assert leased is not None and leased.attempt_count == 1
    assert leased.lease_generation is not None
    assert (
        await ledger.renew(
            message.attempt_id,
            "runner-b",
            leased.lease_generation,
            timedelta(seconds=30),
        )
        is None
    )
    renewed = await ledger.renew(
        message.attempt_id,
        "runner-a",
        leased.lease_generation,
        timedelta(seconds=30),
    )
    assert renewed is not None
    now[0] += timedelta(seconds=31)
    recovered = await ledger.recoverable_attempts()
    assert [record.attempt_id for record in recovered] == [message.attempt_id]
    assert (
        await ledger.claim(
            message.attempt_id, "runner-b", timedelta(seconds=30), max_attempts=1
        )
        is None
    )


@pytest.mark.asyncio
async def test_runner_uses_stricter_deployment_limits_and_rejects_queue_saturation() -> None:
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        _registry(),
        publisher,
        config=RunnerConfig(
            max_concurrency=1,
            max_queue_depth=1,
            max_timeout_ms=1_000,
            max_cases=1,
            max_output_bytes=16,
            max_tokens=4,
            max_model_calls=1,
            max_artifact_operations=1,
        ),
    )
    first = _request()
    first_data = first["data"]
    assert isinstance(first_data, dict)
    limits = cast(dict[str, object], first_data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    execution.update(
        {
            "timeoutMs": 86_400_000,
            "maxConcurrency": 64,
            "maxCases": 100_000,
            "maxOutputBytes": 1_073_741_824,
            "maxTokens": 100_000_000,
        }
    )
    first["requestDigest"] = canonical_digest(first)
    first_record = await runner.accept(first)
    assert first_record is not None and first_record.status == "queued"
    second = _request()
    second_record = await runner.accept(second)
    assert second_record is not None and second_record.status == "failed"
    assert publisher.messages[-1]["messageType"] == "evaluation.runner.failed"
    failed_data = publisher.messages[-1]["data"]
    assert isinstance(failed_data, dict)
    assert failed_data["failureCode"] == "RESOURCE_LIMIT_EXCEEDED"
    await runner.run_once()
    completed_data = publisher.messages[-1]["data"]
    assert isinstance(completed_data, dict)
    effective = cast(dict[str, object], completed_data["effectiveLimits"])
    effective_execution = cast(dict[str, object], effective["execution"])
    assert effective_execution["timeoutMs"] == 1_000
    assert effective_execution["maxConcurrency"] == 1
    assert effective_execution["maxCases"] == 1
    assert effective_execution["maxOutputBytes"] == 16
    assert effective_execution["maxTokens"] == 4


@pytest.mark.asyncio
async def test_runner_lease_loss_interrupts_active_execution_with_finite_failure() -> None:
    started = asyncio.Event()

    class LostLeaseLedger(InMemoryEvaluationRunnerLedger):
        async def renew(
            self, attempt_id: UUID, owner: str, lease_generation: UUID, lease: timedelta
        ) -> Any:
            del attempt_id, owner, lease_generation, lease
            return None

    class BlockingReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            del case_id
            started.set()
            await asyncio.sleep(10)
            context.checkpoint()
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    publisher = NullResultPublisher()

    class RecordingMetrics:
        def __init__(self) -> None:
            self.names: list[str] = []

        def increment(self, component: str, name: str, **attributes: str) -> None:
            del component, attributes
            self.names.append(name)

        def observe(
            self, component: str, name: str, value: float, **attributes: str
        ) -> None:
            del component, value, attributes
            self.names.append(name)

        def record_span(
            self, component: str, operation: str, duration_ms: float, **attributes: str
        ) -> None:
            del component, operation, duration_ms, attributes

    metrics = RecordingMetrics()
    runner = EvaluationRunner(
        LostLeaseLedger(),
        ReplayRegistry(),
        publisher,
        config=RunnerConfig(lease_seconds=1, max_timeout_ms=2_000),
        metrics=metrics,
    )
    runner.registry.register_replay(BlockingReplay())
    await runner.accept(_request())
    execution = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    record = await asyncio.wait_for(execution, timeout=2)

    assert record is not None and record.status == "failed"
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "DEPENDENCY_UNAVAILABLE"  # type: ignore[index]
    assert any(
        name == "evaluation_runner_lease_lost_count" for name in metrics.names
    )


@pytest.mark.asyncio
async def test_runner_enforces_request_concurrency_across_blocking_replays() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    class BlockingReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            nonlocal active, peak
            del case_id
            active += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            context.checkpoint()
            active -= 1
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    registry = ReplayRegistry()
    registry.register_replay(BlockingReplay())
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(), registry, NullResultPublisher()
    )
    await runner.accept(_request())
    await runner.accept(_request())
    first = asyncio.create_task(runner.run_once())
    second = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert peak == 1
    release.set()
    assert (await first) is not None
    assert (await second) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("first_limit", "second_limit"), ((1, 2), (2, 1)))
async def test_runner_mixed_request_concurrency_uses_strictest_active_ceiling(
    first_limit: int, second_limit: int
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    class BlockingReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            nonlocal active, peak
            del case_id
            active += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            context.checkpoint()
            active -= 1
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    registry = ReplayRegistry()
    registry.register_replay(BlockingReplay())
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, NullResultPublisher())
    payloads = [_request(), _request()]
    for payload, limit in zip(payloads, (first_limit, second_limit), strict=True):
        data = cast(dict[str, object], payload["data"])
        limits = cast(dict[str, object], data["limits"])
        execution = cast(dict[str, object], limits["execution"])
        execution["maxConcurrency"] = limit
        payload["requestDigest"] = canonical_digest(payload)
        await runner.accept(payload)
    first = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(runner.run_once())
    await asyncio.sleep(0.05)
    assert peak == 1
    release.set()
    assert await first is not None
    assert await second is not None


@pytest.mark.asyncio
async def test_runner_enforces_cumulative_artifact_budget_across_cases() -> None:
    class ArtifactUsingReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            del case_id
            return ReplayResult(
                self.component_id,
                self.component_version,
                "pass",
                {"cases": 1},
                artifact_operations=1,
            )

    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    data["caseIds"] = [str(uuid4()), str(uuid4())]
    limits = cast(dict[str, object], data["limits"])
    assert isinstance(limits, dict)
    execution = cast(dict[str, object], limits["execution"])
    assert isinstance(execution, dict)
    execution["maxCases"] = 2
    limits["maxArtifactOperations"] = 1
    payload["requestDigest"] = canonical_digest(payload)
    registry = ReplayRegistry()
    registry.register_replay(ArtifactUsingReplay())
    publisher = NullResultPublisher()
    metrics = MetadataMetrics()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(), registry, publisher, metrics=metrics
    )
    await runner.accept(payload)
    record = await runner.run_once()

    assert record is not None and record.status == "failed"
    settled = await runner.ledger.get(record.attempt_id)
    assert settled is not None and settled.business_accounted
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "RESOURCE_LIMIT_EXCEEDED"  # type: ignore[index]
    resource_limits = [
        item
        for item in metrics.snapshot()
        if item.metric == "evaluation_runner_resource_limit_count"
    ]
    assert len(resource_limits) == 1
    assert dict(resource_limits[0].dimensions)["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_runner_artifact_budget_combines_input_component_and_output_operations() -> None:
    content = b"input"
    output = _artifact(output=True, content=b"output")

    class ArtifactUsingReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            del case_id, context
            return ReplayResult(
                self.component_id,
                self.component_version,
                "pass",
                {"cases": 1},
                output_artifacts=((output, b"output"),),
                artifact_operations=1,
            )

    payload = _request()
    data = cast(dict[str, object], payload["data"])
    request_capture = copy.deepcopy(cast(dict[str, object], data["captureAuthorization"]))
    request_capture.update(
        {
            "captureClassification": "full_content",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        }
    )
    data["captureAuthorization"] = request_capture
    output["captureAuthorization"] = copy.deepcopy(request_capture)
    reference = _artifact(output=False, content=content)
    reference["captureAuthorization"] = copy.deepcopy(request_capture)
    data["inputArtifactReferences"] = [reference]
    limits = cast(dict[str, object], data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    execution["maxInputBytes"] = len(content)
    limits["maxArtifactOperations"] = 2
    payload["requestDigest"] = canonical_digest(payload)
    store = _MemoryObjectStore(content)
    publisher = NullResultPublisher()
    registry = ReplayRegistry()
    registry.register_replay(ArtifactUsingReplay())
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        publisher,
        artifact_adapter=cast(Any, EvaluationArtifactAdapter(cast(Any, store))),
    )
    await runner.accept(payload)
    record = await runner.run_once()
    assert record is not None and record.status == "failed"
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "RESOURCE_LIMIT_EXCEEDED"  # type: ignore[index]
    assert len(store.keys) == 1  # input read only; output write was fenced before provider entry


@pytest.mark.asyncio
async def test_document_replay_charges_input_and_output_provider_operations_once() -> None:
    content = b"production document replay input"
    payload = _request()
    data = cast(dict[str, object], payload["data"])
    request_capture = copy.deepcopy(cast(dict[str, object], data["captureAuthorization"]))
    request_capture.update(
        {
            "captureClassification": "full_content",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        }
    )
    data["captureAuthorization"] = request_capture
    reference = _artifact(output=False, content=content)
    reference["captureAuthorization"] = copy.deepcopy(request_capture)
    data["inputArtifactReferences"] = [reference]
    limits = cast(dict[str, object], data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    execution["maxInputBytes"] = len(content)
    execution["maxOutputBytes"] = 2048
    limits["maxArtifactOperations"] = 2
    payload["requestDigest"] = canonical_digest(payload)
    store = _MemoryObjectStore(content)
    registry = ReplayRegistry()
    registry.register_replay(DocumentProcessingEvaluationReplay())
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        NullResultPublisher(),
        artifact_adapter=cast(Any, EvaluationArtifactAdapter(cast(Any, store))),
    )
    await runner.accept(payload)
    record = await runner.run_once()
    assert record is not None and record.status == "completed"
    assert len(store.keys) == 2  # one input read and one runner-owned output write


@pytest.mark.asyncio
async def test_runner_rejects_incompatible_input_before_object_store_read() -> None:
    class CountingStore(_MemoryObjectStore):
        reads = 0

        async def get(self, key: str) -> AsyncIterator[bytes]:
            self.reads += 1
            async for chunk in super().get(key):
                yield chunk

    content = b"untrusted-input"
    reference = _artifact(output=False, content=content)
    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    request_capture = copy.deepcopy(cast(dict[str, object], data["captureAuthorization"]))
    assert isinstance(request_capture, dict)
    request_capture.update(
        {
            "captureClassification": "full_content",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        }
    )
    data["captureAuthorization"] = request_capture
    data["inputArtifactReferences"] = [reference]
    limits = cast(dict[str, object], data["limits"])
    assert isinstance(limits, dict)
    execution = cast(dict[str, object], limits["execution"])
    assert isinstance(execution, dict)
    execution["maxInputBytes"] = len(content)
    limits["maxArtifactOperations"] = 1
    payload["requestDigest"] = canonical_digest(payload)
    store = CountingStore(content)
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        _registry(),
        publisher,
        artifact_adapter=cast(Any, EvaluationArtifactAdapter(cast(Any, store))),
    )
    await runner.accept(payload)
    record = await runner.run_once()

    assert record is not None and record.status == "failed"
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "REQUEST_INVALID"  # type: ignore[index]
    assert store.reads == 0


@pytest.mark.asyncio
async def test_runner_attributes_object_storage_failure_to_fixture_load() -> None:
    content = b"provider-input"
    reference = _artifact(output=False, content=content)
    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    request_capture = copy.deepcopy(cast(dict[str, object], data["captureAuthorization"]))
    request_capture.update(
        {
            "captureClassification": "full_content",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        }
    )
    data["captureAuthorization"] = request_capture
    reference["captureAuthorization"] = copy.deepcopy(request_capture)
    data["inputArtifactReferences"] = [reference]
    limits = cast(dict[str, object], data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    execution["maxInputBytes"] = len(content)
    limits["maxArtifactOperations"] = 1
    payload["requestDigest"] = canonical_digest(payload)

    class BrokenArtifactAdapter:
        async def read(
            self, reference: dict[str, object], *, max_bytes: int | None = None
        ) -> bytes:
            del reference, max_bytes
            raise RuntimeError("provider details stay outside telemetry")

        async def write(self, reference: dict[str, object], value: bytes) -> object:
            del reference, value
            return None

        async def delete(self, reference: dict[str, object]) -> None:
            del reference

    metrics = MetadataMetrics()
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        _registry(),
        publisher,
        metrics=metrics,
        artifact_adapter=cast(Any, BrokenArtifactAdapter()),
    )
    await runner.accept(payload)
    record = await runner.run_once()

    assert record is not None and record.status == "failed"
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "STORAGE_FAILED"  # type: ignore[index]
    fixture_failures = [
        item
        for item in metrics.snapshot()
        if item.metric == "evaluation_runner_fixture_load_duration_ms"
    ]
    assert fixture_failures
    assert all(dict(item.dimensions)["dependency"] == "object_storage" for item in fixture_failures)


@pytest.mark.asyncio
async def test_runner_maps_deadline_unknown_component_and_provider_failures_to_finite_codes(
) -> None:
    for registry, expected in (
        (ReplayRegistry(), "EXECUTION_FAILED"),
        (_registry(), "DEADLINE_EXCEEDED"),
    ):
        publisher = NullResultPublisher()
        runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, publisher)
        payload = _request()
        if expected == "DEADLINE_EXCEEDED":
            data = payload["data"]
            assert isinstance(data, dict)
            data["deadlineAt"] = "2020-01-01T00:00:00.000Z"
            payload["requestDigest"] = canonical_digest(payload)
        await runner.accept(payload)
        await runner.run_once()
        terminal = validate_message(publisher.messages[-1])
        expected_type = (
            "evaluation.runner.cancelled"
            if expected == "DEADLINE_EXCEEDED"
            else "evaluation.runner.failed"
        )
        assert terminal.message_type == expected_type
        data = terminal.payload["data"]
        assert isinstance(data, dict)
        if expected == "DEADLINE_EXCEEDED":
            assert data["reasonCode"] == expected
        else:
            assert data["failureCode"] == expected
        serialized = str(publisher.messages[-1])
        assert "Traceback" not in serialized
        assert "exception" not in serialized.lower()


@pytest.mark.asyncio
async def test_runner_active_cancellation_has_one_terminal_publication() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            del case_id
            started.set()
            await release.wait()
            context.checkpoint()
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    registry = ReplayRegistry()
    registry.register_replay(SlowReplay())
    publisher = NullResultPublisher()
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, publisher)
    payload = _request()
    await runner.accept(payload)
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    await runner.accept(_cancellation(payload))
    release.set()
    await asyncio.wait_for(running, timeout=1)
    terminal = [
        message
        for message in publisher.messages
        if message["messageType"] == "evaluation.runner.cancelled"
    ]
    assert len(terminal) == 1
    assert validate_message(terminal[0]).payload["data"]["reasonCode"] == "OWNER_REQUESTED"  # type: ignore[index]


@pytest.mark.asyncio
async def test_fixture_timeout_uses_registered_metadata_outcome() -> None:
    started = asyncio.Event()

    class SlowArtifactAdapter:
        async def read(
            self, reference: dict[str, object], *, max_bytes: int | None = None
        ) -> bytes:
            del reference, max_bytes
            started.set()
            await asyncio.sleep(1)
            return b"slow input"

        async def write(self, reference: dict[str, object], value: bytes) -> object:
            del reference, value
            return None

        async def delete(self, reference: dict[str, object]) -> None:
            del reference

    payload = _request()
    data = cast(dict[str, object], payload["data"])
    capture = copy.deepcopy(cast(dict[str, object], data["captureAuthorization"]))
    capture.update(
        {
            "captureClassification": "full_content",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        }
    )
    data["captureAuthorization"] = capture
    reference = _artifact(output=False, content=b"slow input")
    reference["captureAuthorization"] = copy.deepcopy(capture)
    data["inputArtifactReferences"] = [reference]
    limits = cast(dict[str, object], data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    execution["maxInputBytes"] = len(b"slow input")
    limits["maxArtifactOperations"] = 1
    payload["requestDigest"] = canonical_digest(payload)
    metrics = MetadataMetrics()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        _registry(),
        NullResultPublisher(),
        config=RunnerConfig(max_timeout_ms=100),
        metrics=metrics,
        artifact_adapter=cast(Any, SlowArtifactAdapter()),
    )
    await runner.accept(payload)
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    record = await asyncio.wait_for(running, timeout=1)
    assert record is not None and record.status == "cancelled"
    fixture_measurements = [
        item
        for item in metrics.snapshot()
        if item.metric == "evaluation_runner_fixture_load_duration_ms"
    ]
    assert any(dict(item.dimensions).get("outcome") == "timeout" for item in fixture_measurements)
    assert cast(Any, metrics)._rejected == 0


@pytest.mark.asyncio
async def test_terminal_business_accounting_survives_fresh_process_recovery() -> None:
    class FailingPublisher:
        async def publish(self, payload: Mapping[str, object]) -> None:
            del payload
            raise RuntimeError("broker unavailable")

    ledger = InMemoryEvaluationRunnerLedger()
    first_metrics = MetadataMetrics()
    first = EvaluationRunner(
        ledger, _registry(), FailingPublisher(), metrics=first_metrics
    )
    payload = _request()
    await first.accept(payload)
    await first.run_once()
    record = await ledger.get(validate_message(payload).attempt_id)
    assert record is not None and record.business_accounted
    assert record.publication_status == "pending"
    first_outcomes = [
        item
        for item in first_metrics.snapshot()
        if item.metric == "evaluation_runner_outcome_count"
    ]
    first_durations = [
        item
        for item in first_metrics.snapshot()
        if item.metric == "evaluation_runner_execution_duration_ms"
    ]
    assert len(first_outcomes) == 1
    assert first_durations and any(item.value > 0 for item in first_durations)

    publisher = NullResultPublisher()
    metrics = MetadataMetrics()
    recovered = EvaluationRunner(ledger, _registry(), publisher, metrics=metrics)
    await recovered._republish_terminal(record, None)  # pyright: ignore[reportPrivateUsage]
    assert len(publisher.messages) == 1
    assert not any(
        item.metric == "evaluation_runner_outcome_count" for item in metrics.snapshot()
    )
    assert not any(
        item.metric == "evaluation_runner_execution_duration_ms"
        for item in metrics.snapshot()
    )


@pytest.mark.asyncio
async def test_resource_limit_accounting_survives_terminal_publication_recovery() -> None:
    class FailingPublisher:
        async def publish(self, payload: Mapping[str, object]) -> None:
            del payload
            raise RuntimeError("broker unavailable")

    ledger = InMemoryEvaluationRunnerLedger()
    first_metrics = MetadataMetrics()
    first = EvaluationRunner(
        ledger, _registry(), FailingPublisher(), metrics=first_metrics
    )
    payload = _request()
    data = cast(dict[str, object], payload["data"])
    limits = cast(dict[str, object], data["limits"])
    data["caseIds"] = [str(uuid4()), str(uuid4())]
    execution = cast(dict[str, object], limits["execution"])
    execution["maxCases"] = 1
    payload["requestDigest"] = canonical_digest(payload)
    with pytest.raises(RuntimeError):
        await first.accept(payload)
    record = await ledger.get(validate_message(payload).attempt_id)
    assert record is not None and record.business_accounted
    assert sum(
        item.metric == "evaluation_runner_resource_limit_count"
        for item in first_metrics.snapshot()
    ) == 1

    recovered_metrics = MetadataMetrics()
    recovered = EvaluationRunner(
        ledger, _registry(), NullResultPublisher(), metrics=recovered_metrics
    )
    await recovered._republish_terminal(record, None)  # pyright: ignore[reportPrivateUsage]
    assert not any(
        item.metric == "evaluation_runner_resource_limit_count"
        for item in recovered_metrics.snapshot()
    )


@pytest.mark.asyncio
async def test_replay_registry_is_closed_exact_versioned_and_cancellable() -> None:
    registry = _registry()
    context = ReplayContext(
        uuid4(), uuid4(), datetime.now(UTC) + timedelta(seconds=1), asyncio.Event()
    )
    with pytest.raises(ReplayError):
        await registry.execute(
            execution_kind="module_replay",
            component_id="unknown.component",
            component_version="1.0.0",
            case_id=context.case_id,
            context=context,
        )
    with pytest.raises(ReplayError):
        await registry.execute(
            execution_kind="module_replay",
            component_id="aura.knowledge.document_processing",
            component_version="2.0.0",
            case_id=context.case_id,
            context=context,
        )
    context.cancellation.set()
    with pytest.raises(ReplayError):
        await registry.execute(
            execution_kind="module_replay",
            component_id="aura.knowledge.document_processing",
            component_version="1.0.0",
            case_id=context.case_id,
            context=context,
        )


@pytest.mark.asyncio
async def test_document_replay_binds_authorized_input_bytes_to_result() -> None:
    replay = DocumentProcessingEvaluationReplay()
    measurements: list[Mapping[str, int | float | str]] = []
    for content in (b"first evaluation input", b"second evaluation input"):
        context = ReplayContext(
            uuid4(),
            uuid4(),
            datetime.now(UTC) + timedelta(seconds=5),
            asyncio.Event(),
            input_artifacts={uuid4(): content},
            max_input_bytes=1024,
            max_output_bytes=4096,
            max_artifact_operations=2,
            capture_authorization={"captureClassification": "metadata_only"},
        )
        result = await replay.replay(context.case_id, context)
        measurements.append(result.measurements)
    assert measurements[0]["input_digest"] != measurements[1]["input_digest"]
    assert measurements[0]["input_bytes"] != measurements[1]["input_bytes"]


@pytest.mark.asyncio
async def test_synthetic_scenario_invokes_public_core_run_coordinator() -> None:
    scenario = SyntheticEvaluationScenario()
    context = ReplayContext(
        uuid4(), uuid4(), datetime.now(UTC) + timedelta(seconds=5), asyncio.Event()
    )
    result = await scenario.execute(context.case_id, context)
    assert result.measurements["run_lifecycle"] == "completed"
    assert result.measurements["model_calls"] == 1
    assert result.measurements["dependency_checks"] == 1
    assert int(result.measurements["side_effect_events"]) > 0


@pytest.mark.parametrize(
    ("component_id", "component_version", "register"),
    (
        ("aura.knowledge.document_processing", "1.0.0", "replay"),
        ("aura.evaluation.synthetic", "1.0.0", "synthetic"),
    ),
)
def test_runtime_fixture_pin_mismatch_fails_before_selected_adapter(
    component_id: str, component_version: str, register: str
) -> None:
    registry = ReplayRegistry()
    if register == "replay":
        registry.register_replay(_Replay())
    else:
        registry.register_synthetic(SyntheticEvaluationScenario())
    evidence = ConfiguredRuntimeEvidence(
        Settings(evaluation_runtime_fixture_revision="fixture-v1"), registry
    )
    with pytest.raises(ReplayError, match="runtime fixture binding mismatch"):
        evidence.observe(component_id=component_id, component_version=component_version)


@pytest.mark.asyncio
async def test_runtime_fixture_pin_mismatch_settles_before_module_adapter() -> None:
    class Adapter(_Replay):
        called = False

        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            self.called = True
            return await super().replay(case_id, context)

    adapter = Adapter()
    registry = ReplayRegistry()
    registry.register_replay(adapter)
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        publisher,
        runtime_evidence=ConfiguredRuntimeEvidence(
            Settings(evaluation_runtime_fixture_revision="fixture-v1"), registry
        ),
    )
    payload = _request()
    await runner.accept(payload)
    record = await runner.run_once()
    assert record is not None and record.status == "failed"
    assert not adapter.called
    terminal = validate_message(publisher.messages[-1])
    assert terminal.payload["data"]["failureCode"] == "REQUEST_INVALID"  # type: ignore[index]


class _MemoryObjectStore:
    def __init__(self, content: bytes = b"") -> None:
        self.content = content
        self.keys: list[str] = []

    async def get(self, key: str) -> AsyncIterator[bytes]:
        self.keys.append(key)
        yield self.content

    async def put_stream(self, key: str, stream: Any, *, content_type: str) -> int:
        del content_type
        self.keys.append(key)
        chunks = [chunk async for chunk in stream]
        self.content = b"".join(chunks)
        return len(self.content)

    async def delete(self, key: str) -> None:
        self.keys.append(key)
        self.content = b""


def _artifact(*, output: bool, content: bytes = b"safe-evidence") -> dict[str, object]:
    import hashlib

    return {
        "artifactReferenceId": str(uuid4()),
        "mediaClassification": "evaluation_output" if output else "evaluation_input",
        "mediaType": "application/octet-stream",
        "byteCount": len(content),
        "integrity": {"algorithm": "sha256", "digest": hashlib.sha256(content).hexdigest()},
        "captureAuthorization": {
            "captureClassification": "full_content",
            "authorizationPurpose": "owner_supplied_evaluation",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
            "authorizationRecordId": str(uuid4()),
            "authorizedAt": "2026-01-01T00:00:00.000Z",
        },
    }


@pytest.mark.asyncio
async def test_artifact_adapter_verifies_digest_media_bounds_and_opaque_locator() -> None:
    content = b"safe-evidence"
    store = _MemoryObjectStore(content)
    adapter = EvaluationArtifactAdapter(cast(Any, store), max_bytes=1024)
    reference = _artifact(output=False, content=content)
    assert await adapter.read(reference) == content
    with pytest.raises(EvaluationArtifactError):
        await adapter.read(reference, max_bytes=len(content) - 1)
    assert store.keys[0].startswith("runner/")
    assert "filename" not in store.keys[0]
    assert "http" not in store.keys[0]

    bad_digest = copy.deepcopy(reference)
    integrity = bad_digest["integrity"]
    assert isinstance(integrity, dict)
    integrity["digest"] = "0" * 64
    with pytest.raises(EvaluationArtifactError):
        await adapter.read(bad_digest)

    oversized = copy.deepcopy(reference)
    oversized["byteCount"] = 2048
    with pytest.raises(EvaluationArtifactError):
        await adapter.read(oversized)

    output = _artifact(output=True, content=content)
    written = await adapter.write(output, content)
    assert written.byte_count == len(content)
    await adapter.delete(output)


@pytest.mark.asyncio
async def test_artifact_adapter_rejects_metadata_only_content_and_provider_failure() -> None:
    store = _MemoryObjectStore(b"content")
    adapter = EvaluationArtifactAdapter(cast(Any, store))
    reference = _artifact(output=False, content=b"content")
    authorization = reference["captureAuthorization"]
    assert isinstance(authorization, dict)
    authorization["captureClassification"] = "metadata_only"
    with pytest.raises(TransportValidationError):
        await adapter.read(reference)

    class BrokenStore(_MemoryObjectStore):
        async def get(self, key: str) -> AsyncIterator[bytes]:
            del key
            raise RuntimeError("provider diagnostic must not escape")
            yield b"unreachable"

    with pytest.raises(EvaluationArtifactError, match="artifact read failed"):
        await EvaluationArtifactAdapter(cast(Any, BrokenStore())).read(
            _artifact(output=False, content=b"")
        )


@pytest.mark.asyncio
async def test_runner_cancels_output_write_and_cleans_partial_artifact() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    content = b"output"
    output = _artifact(output=True, content=content)
    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    authorization = output["captureAuthorization"]
    assert isinstance(authorization, dict)
    data["captureAuthorization"] = copy.deepcopy(cast(dict[str, object], authorization))
    limits = cast(dict[str, object], data["limits"])
    execution = cast(dict[str, object], limits["execution"])
    assert isinstance(execution, dict)
    execution["maxOutputBytes"] = len(content)
    limits["maxArtifactOperations"] = 1
    payload["requestDigest"] = canonical_digest(payload)

    class BlockingOutputAdapter:
        written = False
        deleted = 0

        async def write(self, reference: dict[str, object], value: bytes) -> object:
            del reference, value
            self.written = True
            started.set()
            await release.wait()
            return None

        async def read(
            self, reference: dict[str, object], *, max_bytes: int | None = None
        ) -> bytes:
            del reference, max_bytes
            return b""

        async def delete(self, reference: dict[str, object]) -> None:
            del reference
            self.deleted += 1

    class OutputReplay(_Replay):
        async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
            del case_id, context
            return ReplayResult(
                self.component_id,
                self.component_version,
                "pass",
                {"cases": 1},
                output_artifacts=((output, content),),
            )

    adapter = BlockingOutputAdapter()
    registry = ReplayRegistry()
    registry.register_replay(OutputReplay())
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        publisher,
        artifact_adapter=cast(Any, adapter),
    )
    await runner.accept(payload)
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    await runner.accept(_cancellation(payload))
    release.set()
    record = await asyncio.wait_for(running, timeout=1)

    assert record is not None and record.status == "cancelled"
    assert adapter.written
    assert adapter.deleted == 1
