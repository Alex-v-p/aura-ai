"""AURA-0092 closed transport and privacy checks."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import aura_observatory.evaluation_transport as transport_module
import pytest
from aura_observatory.evaluation_orchestration import (
    EvaluationAdmission,
    EvaluationOrchestrationService,
    InMemoryEvaluationOrchestrationRepository,
)
from aura_observatory.evaluation_transport import (
    EVALUATION_STREAM,
    REQUEST_DURABLE,
    RESULT_DURABLE,
    RESULT_SUBJECT,
    NatsEvaluationTransport,
    NatsTransportError,
    TransportValidationError,
    canonical_digest,
    decode_envelope,
    encode_envelope,
    validate_transport_envelope,
)
from aura_observatory.ports import EvaluationDispatchIntent, EvaluationResultDelivery


class _PullSubscription:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop

    async def fetch(self, _batch_size: int, *, timeout: float) -> list[object]:
        assert timeout == 0.5
        self._stop.set()
        return []


class _JetStream:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.subject: str | None = None
        self.options: dict[str, object] = {}

    async def pull_subscribe(self, subject: str, *, durable: str, stream: str) -> _PullSubscription:
        self.subject = subject
        self.options = {"durable": durable, "stream": stream}
        return _PullSubscription(self._stop)


class _Connection:
    def __init__(self, jetstream: _JetStream) -> None:
        self._jetstream = jetstream

    def jetstream(self) -> _JetStream:
        return self._jetstream


class _DisconnectedConnection:
    is_connected = False
    is_closed = False

    def jetstream(self) -> _JetStream:
        raise AssertionError("disconnected provider must not be called")


def _admission() -> EvaluationAdmission:
    now = datetime.now(UTC)
    dataset_id = str(uuid4())
    suite_id = str(uuid4())
    evaluator_id = str(uuid4())
    target = {
        "componentId": "aura.evaluation.synthetic",
        "componentVersion": "1.0.0",
        "codeRevision": "0000000",
        "imageDigest": "a" * 64,
        "configurationRevision": "local",
    }
    data = _request_data(dataset_id, suite_id, evaluator_id, target, now)
    return EvaluationAdmission(
        request_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        request_version="1.0.0",
        dataset_id=dataset_id,
        dataset_version="1.0.0",
        suite_id=suite_id,
        suite_version="1.0.0",
        evaluator_id=evaluator_id,
        evaluator_version="1.0.0",
        capture_classification="metadata_only",
        deadline_at=now + timedelta(minutes=5),
        max_attempts=3,
        envelope={"data": data},
        created_at=now,
    )


def _envelope() -> dict[str, object]:
    job = str(uuid4())
    data = _request_data(str(uuid4()), str(uuid4()), str(uuid4()), _component(), datetime.now(UTC))
    return {
        "schemaVersion": 1,
        "messageId": str(uuid4()),
        "messageType": "evaluation.runner.requested",
        "occurredAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "correlationId": job,
        "causationId": job,
        "requestId": str(uuid4()),
        "jobId": job,
        "attemptId": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "requestDigest": "0" * 64,
        "data": data,
    }


def _component() -> dict[str, str]:
    return {
        "componentId": "aura.evaluation.synthetic",
        "componentVersion": "1.0.0",
        "codeRevision": "0000000",
        "imageDigest": "a" * 64,
        "configurationRevision": "local",
    }


def _request_data(
    dataset_id: str,
    suite_id: str,
    evaluator_id: str,
    component: dict[str, str],
    now: datetime,
) -> dict[str, object]:
    return {
        "executionKind": "synthetic_end_to_end",
        "requestVersion": "1.0.0",
        "targetComponent": component,
        "captureAuthorization": {
            "captureClassification": "metadata_only",
            "authorizationPurpose": "synthetic_benchmark",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
        },
        "provenance": {
            "dataset": {"id": dataset_id, "version": "1.0.0"},
            "suite": {"id": suite_id, "version": "1.0.0"},
            "evaluators": [
                {"evaluatorId": evaluator_id, "evaluatorVersion": "1.0.0", "kind": "deterministic"}
            ],
            "components": [component],
            "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": "a" * 64},
            "deploymentRevision": "aura0092-test",
            "reproducibility": {
                "seed": 7,
                "clockMode": "fixed",
                "timeZone": "UTC",
                "locale": "en-US",
                "fixtureRevision": "synthetic-v1",
                "promptRevision": "prompt-v1",
                "policyRevision": "policy-v1",
            },
        },
        "limits": {
            "execution": {
                "timeoutMs": 10_000,
                "maxConcurrency": 1,
                "maxAttempts": 3,
                "maxCases": 1,
                "maxInputBytes": 0,
                "maxOutputBytes": 1024,
                "maxTokens": 0,
            },
            "maxQueueTimeMs": 10_000,
            "maxModelCalls": 1,
            "maxArtifactOperations": 0,
        },
        "deadlineAt": (now + timedelta(minutes=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "caseIds": [str(uuid4())],
        "inputArtifactReferences": [],
    }


def test_transport_round_trip_is_canonical() -> None:
    payload = encode_envelope(_envelope())
    assert decode_envelope(payload)["schemaVersion"] == 1
    assert (
        payload == json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize(
    "field", ["prompt", "response", "content", "credentials", "endpoint", "owner_identity"]
)
def test_transport_rejects_sensitive_fields(field: str) -> None:
    envelope = _envelope()
    envelope["data"] = {field: "synthetic"}
    with pytest.raises(TransportValidationError):
        validate_transport_envelope(envelope)


def test_transport_rejects_unknown_top_level_fields() -> None:
    envelope = _envelope()
    envelope["unknown"] = 1
    with pytest.raises(TransportValidationError):
        validate_transport_envelope(envelope)


@pytest.mark.asyncio
async def test_result_subscription_uses_bootstrap_stream_and_dispatch_durable() -> None:
    stop = asyncio.Event()
    jetstream = _JetStream(stop)
    transport = NatsEvaluationTransport(_Connection(jetstream))

    async for _ in transport.results(stop):
        pass

    assert EVALUATION_STREAM == "AURA_EVALUATION_RUNNER"
    assert REQUEST_DURABLE == "aura-evaluation-dispatch"
    assert RESULT_DURABLE == "aura-evaluation-dispatch"
    assert jetstream.subject == RESULT_SUBJECT
    assert jetstream.options == {
        "durable": "aura-evaluation-dispatch",
        "stream": "AURA_EVALUATION_RUNNER",
    }


def test_result_delivery_owns_acknowledgement_without_hidden_envelope_key() -> None:
    async def acknowledge() -> None:
        return None

    delivery = EvaluationResultDelivery(
        envelope={"messageType": "evaluation.runner.completed"}, acknowledge=acknowledge
    )
    assert delivery.envelope == {"messageType": "evaluation.runner.completed"}
    assert "_ack" not in delivery.envelope


@pytest.mark.asyncio
async def test_publish_fails_fast_when_nats_is_disconnected() -> None:
    transport = NatsEvaluationTransport(_DisconnectedConnection())
    with pytest.raises(NatsTransportError) as error:
        await transport.publish(
            EvaluationDispatchIntent(kind="request", envelope=_envelope()), str(uuid4())
        )
    assert error.value.code == "transport_unavailable"


@pytest.mark.asyncio
async def test_publish_timeout_is_bounded_and_provider_text_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowJetStream:
        async def publish(self, *_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0.05)

    class SlowConnection:
        def jetstream(self) -> SlowJetStream:
            return SlowJetStream()

    monkeypatch.setattr(transport_module, "PUBLISH_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(NatsTransportError) as error:
        await NatsEvaluationTransport(SlowConnection()).publish(
            EvaluationDispatchIntent(kind="request", envelope=_envelope()), str(uuid4())
        )
    assert error.value.code == "timeout"
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_connect_timeout_and_error_callback_use_finite_codes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def slow_connect(*_args: object, **kwargs: object) -> object:
        callback = cast(Callable[[Exception], Awaitable[None]], kwargs["error_cb"])
        await callback(RuntimeError("nats://private-endpoint/password"))
        await asyncio.sleep(0.05)
        return object()

    monkeypatch.setattr(transport_module, "CONNECT_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(transport_module.nats, "connect", slow_connect)
    with pytest.raises(NatsTransportError) as error:
        await transport_module.connect_nats("nats://private-endpoint:4222")
    assert error.value.code == "timeout"
    assert "private-endpoint" not in caplog.text
    assert "password" not in caplog.text


@pytest.mark.asyncio
async def test_result_provider_failure_is_bounded_and_sanitized() -> None:
    class BrokenSubscription:
        async def fetch(self, _batch_size: int, *, timeout: float) -> list[object]:
            del timeout
            raise RuntimeError("nats://private-endpoint/password")

    class BrokenJetStream:
        async def pull_subscribe(
            self, _subject: str, *, durable: str, stream: str
        ) -> BrokenSubscription:
            del durable, stream
            return BrokenSubscription()

    class BrokenConnection:
        def jetstream(self) -> BrokenJetStream:
            return BrokenJetStream()

    with pytest.raises(NatsTransportError) as error:
        await anext(NatsEvaluationTransport(BrokenConnection()).results(asyncio.Event()))
    assert error.value.code == "transport_unavailable"
    assert "private-endpoint" not in str(error.value)


@pytest.mark.asyncio
async def test_producer_digest_is_exact_data_projection_consumed_by_result_path() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_admission())
    request = next(iter(repository.outbox.values())).envelope
    assert request["requestDigest"] == canonical_digest(request["data"])  # type: ignore[arg-type]
    assert request["requestDigest"] != canonical_digest(job.admission.envelope)
    assert request["correlationId"] == job.job_id
    assert request["causationId"] == request["messageId"]

    result = dict(request)
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    request_data = cast(dict[str, object], request["data"])
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "a" * 64,
        "status": "completed",
        "startedAt": request_data["deadlineAt"],
        "completedAt": request_data["deadlineAt"],
        "deadlineAt": request_data["deadlineAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": request_data["limits"],
        "provenance": request_data["provenance"],
        "runtimeEvidence": {
            key: {"status": "missing"}
            for key in (
                "componentVersion",
                "codeRevision",
                "imageDigest",
                "configurationRevision",
                "modelId",
                "modelVersion",
                "modelDigest",
                "deploymentRevision",
                "fixtureRevision",
                "promptRevision",
                "policyRevision",
                "seed",
            )
        },
        "outputArtifactReferences": [],
    }
    assert result["correlationId"] == job.job_id
    assert result["causationId"] == request["messageId"]
    assert await service.consume_result(result) == "applied"


@pytest.mark.asyncio
async def test_result_accepts_core_deployment_clamped_effective_limits() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    service = EvaluationOrchestrationService(repository)
    job = await service.admit(_admission())
    request = next(iter(repository.outbox.values())).envelope
    request_data = cast(dict[str, object], request["data"])
    effective_limits = copy.deepcopy(cast(dict[str, object], request_data["limits"]))
    execution = cast(dict[str, object], effective_limits["execution"])
    execution["maxInputBytes"] = 0
    result = dict(request)
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    result["data"] = {
        "resultId": str(uuid4()),
        "resultDigest": "a" * 64,
        "status": "completed",
        "startedAt": request_data["deadlineAt"],
        "completedAt": request_data["deadlineAt"],
        "deadlineAt": request_data["deadlineAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": effective_limits,
        "provenance": request_data["provenance"],
        "runtimeEvidence": {
            key: {"status": "missing"}
            for key in (
                "componentVersion",
                "codeRevision",
                "imageDigest",
                "configurationRevision",
                "modelId",
                "modelVersion",
                "modelDigest",
                "deploymentRevision",
                "fixtureRevision",
                "promptRevision",
                "policyRevision",
                "seed",
            )
        },
        "outputArtifactReferences": [],
    }
    assert await service.consume_result(result) == "applied"
    stored_job = await repository.get_job(job.job_id)
    assert stored_job is not None
    assert stored_job.state.value == "succeeded"
