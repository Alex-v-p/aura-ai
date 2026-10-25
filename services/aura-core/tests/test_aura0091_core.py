"""Core-side AURA-0091 transport, ledger, and runner checks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import aura_core.runtime.replay.application as replay_application
import pytest
import yaml
from aura_core.entrypoints.evaluation_runner.runner import EvaluationRunner, NullResultPublisher
from aura_core.entrypoints.evaluation_runner.state import (
    AttemptConflict,
    AttemptRecord,
    InMemoryEvaluationRunnerLedger,
)
from aura_core.platform.evaluation_transport import (
    TransportValidationError,
    canonical_digest,
    deterministic_terminal_id,
    validate_message,
)
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.replay.registry import ReplayRegistry, ReplayResult


def _request(*, attempt_id: UUID | None = None) -> dict[str, object]:
    def new() -> str:
        return str(uuid4())

    digest = "a" * 64
    component = {
        "componentId": "aura.knowledge.document_processing",
        "componentVersion": "1.0.0",
        "codeRevision": "abcdef1",
        "imageDigest": digest,
        "configurationRevision": "local",
    }
    now = datetime.now(UTC)
    payload = {
        "schemaVersion": 1,
        "messageId": new(),
        "messageType": "evaluation.runner.requested",
        "occurredAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "correlationId": new(),
        "causationId": new(),
        "requestId": new(),
        "jobId": new(),
        "attemptId": str(attempt_id or uuid4()),
        "idempotencyKey": new(),
        "requestDigest": digest,
        "data": {
            "executionKind": "module_replay",
            "requestVersion": "1.0.0",
            "targetComponent": component,
            "captureAuthorization": {
                "captureClassification": "metadata_only",
                "authorizationPurpose": "controlled_replay",
                "authorizationContext": "controlled_evaluation",
                "retentionMode": "manual_only",
            },
            "provenance": {
                "dataset": {"id": new(), "version": "1.0.0"},
                "suite": {"id": new(), "version": "1.0.0"},
                "evaluators": [
                    {"evaluatorId": new(), "evaluatorVersion": "1.0.0", "kind": "deterministic"}
                ],
                "components": [component],
                "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": digest},
                "deploymentRevision": "local",
                "reproducibility": {
                    "seed": 7,
                    "clockMode": "fixed",
                    "timeZone": "UTC",
                    "locale": "en-US",
                    "fixtureRevision": "fixture-v1",
                    "promptRevision": "prompt-v1",
                    "policyRevision": "policy-v1",
                },
            },
            "limits": {
                "execution": {
                    "timeoutMs": 10_000,
                    "maxConcurrency": 1,
                    "maxAttempts": 1,
                    "maxCases": 1,
                    "maxInputBytes": 0,
                    "maxOutputBytes": 1024,
                    "maxTokens": 0,
                },
                "maxQueueTimeMs": 10_000,
                "maxModelCalls": 0,
                "maxArtifactOperations": 0,
            },
            "deadlineAt": (now + timedelta(seconds=30))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "caseIds": [new()],
            "inputArtifactReferences": [],
        },
    }
    payload["requestDigest"] = canonical_digest(payload)
    return cast(dict[str, object], payload)


def test_transport_rejects_unknown_content_fields() -> None:
    payload = _request()
    payload["prompt"] = "must never cross the boundary"
    with pytest.raises(TransportValidationError):
        validate_message(payload)


def test_runner_required_metrics_are_registered_and_accept_bounded_dimensions() -> None:
    required = {
        "evaluation_runner_queue_time_ms",
        "evaluation_runner_fixture_load_duration_ms",
        "evaluation_runner_execution_duration_ms",
        "evaluation_runner_outcome_count",
        "evaluation_runner_cancellation_count",
        "evaluation_runner_resource_limit_count",
        "evaluation_runner_reproducibility_completeness",
    }
    metrics = MetadataMetrics(capacity=len(required))
    for name in required:
        if name.endswith(("_time_ms", "_duration_ms", "_completeness")):
            metrics.observe(
                "aura.evaluation.runner",
                name,
                1.0,
                outcome="completed",
                dependency="application_validation",
            )
        else:
            metrics.increment(
                "aura.evaluation.runner",
                name,
                outcome="completed",
                dependency="application_validation",
            )

    retained = {measurement.metric for measurement in metrics.snapshot()}
    assert retained == required
    assert metrics.stats().rejected == 0


def test_runner_manifest_metrics_accept_every_emitted_failure_dimension_tuple() -> None:
    manifest_path = (
        Path(__file__).parents[1]
        / "resources"
        / "component-manifests"
        / "evaluation-runner.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    definitions = manifest["metric_definitions"]
    scenarios = (
        {
            "outcome": "expired",
            "error_class": "timeout",
            "dependency": "application_validation",
        },
        {
            "outcome": "canceled",
            "error_class": "cancellation",
            "dependency": "application_validation",
        },
        {"outcome": "recovered", "error_class": "unknown", "dependency": "postgresql"},
        {"outcome": "fenced", "error_class": "conflict", "dependency": "postgresql"},
        {"outcome": "error", "error_class": "provider", "dependency": "object_storage"},
        {"outcome": "published", "error_class": "delivery", "dependency": "nats_jetstream"},
        {"outcome": "rejected", "error_class": "identity", "dependency": "nats_jetstream"},
    )
    metrics = MetadataMetrics(capacity=len(definitions) * len(scenarios))
    for definition in definitions:
        for scenario in scenarios:
            attributes = {label: scenario[label] for label in definition["labels"]}
            if definition["type"] in {"histogram", "gauge"}:
                value = 1.0 if definition["type"] == "histogram" else 0.5
                metrics.observe("aura.evaluation.runner", definition["name"], value, **attributes)
            else:
                metrics.increment("aura.evaluation.runner", definition["name"], **attributes)

    assert {measurement.metric for measurement in metrics.snapshot()} == {
        definition["name"] for definition in definitions
    }
    assert len(metrics.snapshot()) == len(definitions) * len(scenarios)
    assert metrics.stats().rejected == 0


def test_runner_bounds_sql_programming_and_privilege_failures() -> None:
    class InsufficientPrivilege(Exception):
        pass

    class ProgrammingError(Exception):
        def __init__(self) -> None:
            self.orig = InsufficientPrivilege()

    classify = replay_application._bounded_exception_class  # pyright: ignore[reportPrivateUsage]
    assert classify(ProgrammingError()) == "authorization"

    class AmbiguousParameter(Exception):
        pass

    class SqlProgrammingError(Exception):
        def __init__(self) -> None:
            self.orig = AmbiguousParameter()

    assert classify(SqlProgrammingError()) == "database_programming"


@pytest.mark.asyncio
async def test_ledger_rejects_conflicting_idempotency() -> None:
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
    await ledger.receive(attempt)
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


@pytest.mark.asyncio
async def test_runner_publishes_one_valid_deterministic_terminal_result() -> None:
    class Adapter:
        component_id = "aura.knowledge.document_processing"
        component_version = "1.0.0"
        fixture_revision = "test-replay-fixture-v1"

        async def replay(self, case_id: UUID, context: object) -> ReplayResult:
            del case_id, context
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    registry = ReplayRegistry()
    registry.register_replay(Adapter())
    publisher = NullResultPublisher()
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, publisher)
    payload = _request()
    await runner.accept(payload)
    record = await runner.run_once()
    assert record is not None and record.status == "completed"
    terminal = validate_message(publisher.messages[0])
    assert terminal.message_type == "evaluation.runner.completed"
    data = terminal.payload["data"]
    assert isinstance(data, dict)
    assert data["resultId"] == str(deterministic_terminal_id(record.attempt_id, "completed"))


@pytest.mark.asyncio
async def test_runner_metadata_metrics_use_registered_correlated_spans() -> None:
    class Adapter:
        component_id = "aura.knowledge.document_processing"
        component_version = "1.0.0"
        fixture_revision = "test-replay-fixture-v1"

        async def replay(self, case_id: UUID, context: object) -> ReplayResult:
            del case_id, context
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    metrics = MetadataMetrics()
    registry = ReplayRegistry()
    registry.register_replay(Adapter())
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        NullResultPublisher(),
        metrics=metrics,
    )
    await runner.accept(_request())
    await runner.run_once()

    measurements = cast(Any, metrics)._measurements
    spans = [measurement for measurement in measurements if measurement.kind == "span"]
    assert {dict(item.trace_attributes)["operation"] for item in spans} == {
        "evaluation.execute",
        "evaluation.fixture.load",
    }
    execution = next(
        measurement
        for measurement in spans
        if dict(measurement.trace_attributes)["operation"] == "evaluation.execute"
    )
    fixture = next(
        measurement
        for measurement in spans
        if dict(measurement.trace_attributes)["operation"] == "evaluation.fixture.load"
    )
    assert execution.trace_id == fixture.trace_id
    assert fixture.parent_span_id == execution.span_id
    assert execution.value > 0
    assert fixture.value >= 0
    assert cast(Any, metrics)._rejected == 0


@pytest.mark.asyncio
async def test_runner_surfaces_terminal_ledger_failures_as_bounded_metadata() -> None:
    class FailingLedger(InMemoryEvaluationRunnerLedger):
        async def terminal(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("database failure details stay out of telemetry")

    metrics = MetadataMetrics()
    registry = ReplayRegistry()
    publisher = NullResultPublisher()
    runner = EvaluationRunner(FailingLedger(), registry, publisher, metrics=metrics)
    await runner.accept(_request())

    with pytest.raises(RuntimeError):
        await runner.run_once()

    measurements = cast(Any, metrics)._measurements
    settlement_failures = [
        measurement
        for measurement in measurements
        if measurement.metric == "evaluation_runner_terminal_settlement_failure_count"
    ]
    assert len(settlement_failures) == 1
    assert all(dict(item.dimensions)["dependency"] == "postgresql" for item in settlement_failures)
    assert all(dict(item.dimensions)["error_class"] == "execution" for item in settlement_failures)
    assert cast(Any, metrics)._rejected == 0


@pytest.mark.asyncio
async def test_runner_retries_pending_publication_after_publisher_recovery() -> None:
    class FlakyPublisher(NullResultPublisher):
        def __init__(self) -> None:
            super().__init__()
            self.available = False

        async def publish(self, payload: Mapping[str, object]) -> None:
            if not self.available:
                raise RuntimeError("broker unavailable")
            await super().publish(payload)

    class Adapter:
        component_id = "aura.knowledge.document_processing"
        component_version = "1.0.0"
        fixture_revision = "test-replay-fixture-v1"

        async def replay(self, case_id: UUID, context: object) -> ReplayResult:
            del case_id, context
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    ledger = InMemoryEvaluationRunnerLedger()
    publisher = FlakyPublisher()
    registry = ReplayRegistry()
    registry.register_replay(Adapter())
    first = EvaluationRunner(ledger, registry, publisher)
    await first.accept(_request())
    record = await first.run_once()
    assert record is not None and record.status == "completed"

    pending = await ledger.pending_publications()
    assert len(pending) == 1
    publisher.available = True
    restarted = EvaluationRunner(ledger, registry, publisher)
    await restarted._drain_publications()  # pyright: ignore[reportPrivateUsage]

    assert len(publisher.messages) == 1
    assert (await ledger.pending_publications()) == []


@pytest.mark.asyncio
async def test_runner_recovery_requeues_durable_projection_after_restart() -> None:
    class Adapter:
        component_id = "aura.knowledge.document_processing"
        component_version = "1.0.0"
        fixture_revision = "test-replay-fixture-v1"

        async def replay(self, case_id: UUID, context: object) -> ReplayResult:
            del case_id, context
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    ledger = InMemoryEvaluationRunnerLedger()
    payload = _request()
    first = EvaluationRunner(ledger, ReplayRegistry(), NullResultPublisher())
    await first.accept(payload)

    registry = ReplayRegistry()
    registry.register_replay(Adapter())
    publisher = NullResultPublisher()
    restarted = EvaluationRunner(ledger, registry, publisher)
    await restarted._recover_attempts()  # pyright: ignore[reportPrivateUsage]
    record = await restarted.run_once()

    assert record is not None and record.status == "completed"
    assert (
        publisher.messages
        and publisher.messages[0]["messageType"] == "evaluation.runner.completed"
    )


@pytest.mark.asyncio
async def test_runner_rejects_mismatched_runtime_evidence_before_replay() -> None:
    class Adapter:
        component_id = "aura.knowledge.document_processing"
        component_version = "1.0.0"
        fixture_revision = "test-replay-fixture-v1"
        called = False

        async def replay(self, case_id: UUID, context: object) -> ReplayResult:
            del case_id, context
            self.called = True
            return ReplayResult(self.component_id, self.component_version, "pass", {"cases": 1})

    class Evidence:
        def observe(self, *, component_id: str, component_version: str) -> dict[str, object]:
            del component_id, component_version
            return {"componentVersion": "different-version"}

    adapter = Adapter()
    registry = ReplayRegistry()
    registry.register_replay(adapter)
    publisher = NullResultPublisher()
    runner = EvaluationRunner(
        InMemoryEvaluationRunnerLedger(),
        registry,
        publisher,
        runtime_evidence=Evidence(),
    )
    await runner.accept(_request())
    await runner.run_once()

    assert not adapter.called
    failed = publisher.messages[-1]["data"]
    assert isinstance(failed, dict) and failed["failureCode"] == "REQUEST_INVALID"


@pytest.mark.asyncio
async def test_queued_cancellation_is_terminal_and_schema_valid() -> None:
    registry = ReplayRegistry()
    publisher = NullResultPublisher()
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, publisher)
    payload = _request()
    await runner.accept(payload)
    request = validate_message(payload)
    request_data = payload["data"]
    assert isinstance(request_data, dict)
    cancellation = dict(payload)
    cancellation_data: dict[str, object] = {
        "cancellationId": str(uuid4()),
        "reasonCode": "OWNER_REQUESTED",
        "deadlineAt": request_data["deadlineAt"],
    }
    cancellation.update(
        {
            "messageId": str(uuid4()),
            "messageType": "evaluation.runner.cancellation_requested",
            "data": cancellation_data,
        }
    )
    record = await runner.accept(cancellation)
    assert record is not None and record.status == "cancelled"
    assert validate_message(publisher.messages[-1]).message_type == "evaluation.runner.cancelled"
    assert request.attempt_id == record.attempt_id


@pytest.mark.asyncio
async def test_queue_deadline_is_one_cancelled_terminal() -> None:
    publisher = NullResultPublisher()
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), ReplayRegistry(), publisher)
    payload = _request()
    await runner.accept(payload)
    attempt_id = UUID(str(payload["attemptId"]))
    runner._enqueued_at[attempt_id] = datetime.now(UTC) - timedelta(seconds=11)  # type: ignore[attr-defined]

    record = await runner.run_once()

    assert record is not None and record.status == "cancelled"
    terminals = [
        message
        for message in publisher.messages
        if message["messageType"] == "evaluation.runner.cancelled"
    ]
    assert len(terminals) == 1
    data = terminals[0]["data"]
    assert isinstance(data, dict) and data["reasonCode"] == "DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_recovery_deadline_is_one_cancelled_terminal() -> None:
    ledger = InMemoryEvaluationRunnerLedger()
    publisher = NullResultPublisher()
    runner = EvaluationRunner(ledger, ReplayRegistry(), publisher)
    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    data["deadlineAt"] = "2020-01-01T00:00:00.000Z"
    payload["requestDigest"] = canonical_digest(payload)
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

    await runner._recover_attempts()  # type: ignore[attr-defined]

    record = await ledger.get(message.attempt_id)
    assert record is not None and record.status == "cancelled"
    terminals = [
        item
        for item in publisher.messages
        if item["messageType"] == "evaluation.runner.cancelled"
    ]
    assert len(terminals) == 1
    terminal_data = terminals[0]["data"]
    assert isinstance(terminal_data, dict)
    assert terminal_data["reasonCode"] == "DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_expired_deadline_wins_over_max_attempts() -> None:
    ledger = InMemoryEvaluationRunnerLedger()
    publisher = NullResultPublisher()
    runner = EvaluationRunner(ledger, ReplayRegistry(), publisher)
    payload = _request()
    data = payload["data"]
    assert isinstance(data, dict)
    execution = cast(dict[str, object], data["limits"])
    execution_limits = cast(dict[str, object], execution["execution"])
    execution_limits["maxAttempts"] = 1
    payload["requestDigest"] = canonical_digest(payload)
    message = validate_message(payload)
    await ledger.receive(
        AttemptRecord(
            message.attempt_id,
            message.request_id,
            message.job_id,
            message.idempotency_key,
            message.request_digest,
            attempt_count=1,
            request_projection=payload,
        )
    )
    await runner.accept(payload)
    data["deadlineAt"] = "2020-01-01T00:00:00.000Z"

    record = await runner.run_once()

    assert record is not None and record.status == "cancelled"
    terminals = [
        item
        for item in publisher.messages
        if item["messageType"] == "evaluation.runner.cancelled"
    ]
    assert len(terminals) == 1
    terminal_data = terminals[0]["data"]
    assert isinstance(terminal_data, dict)
    assert terminal_data["reasonCode"] == "DEADLINE_EXCEEDED"
