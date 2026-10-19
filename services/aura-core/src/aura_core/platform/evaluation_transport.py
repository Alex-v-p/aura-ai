"""Closed, content-free evaluation runner transport primitives.

The JSON schema is the wire contract.  This module is the Core-side runtime
guard: it performs the cross-field checks which a JSON Schema cannot express,
canonicalises envelopes for digesting, and exposes typed, provider-neutral
values to the runner.  No request value is treated as a module name, path,
provider selector, credential, or execution instruction.
"""

# Runtime validation narrows untyped wire data at this boundary.  The public
# functions return fully typed Core values after validation.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportIndexIssue=false, reportGeneralTypeIssues=false, reportReturnType=false

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

TRANSPORT_VERSION = 1
EVALUATION_STREAM = "AURA_EVALUATION_RUNNER"
EVALUATION_REQUEST_SUBJECT = "aura.evaluation.runner.requested.v1"
EVALUATION_CANCELLATION_SUBJECT = "aura.evaluation.runner.cancellation.v1"
EVALUATION_RESULT_SUBJECT = "aura.evaluation.runner.results.v1"
MAX_TRANSPORT_BYTES = 1_048_576
REQUEST_TYPE = "evaluation.runner.requested"
CANCELLATION_TYPE = "evaluation.runner.cancellation_requested"
TERMINAL_TYPES = frozenset(
    {
        "evaluation.runner.completed",
        "evaluation.runner.cancelled",
        "evaluation.runner.failed",
    }
)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$")

FailureCode = Literal[
    "REQUEST_INVALID",
    "DEADLINE_EXCEEDED",
    "RESOURCE_LIMIT_EXCEEDED",
    "DEPENDENCY_UNAVAILABLE",
    "EXECUTION_FAILED",
    "RESULT_VALIDATION_FAILED",
    "STORAGE_FAILED",
    "INTERNAL_FAILURE",
]
FAILURE_CODES = frozenset(
    {
        "REQUEST_INVALID",
        "DEADLINE_EXCEEDED",
        "RESOURCE_LIMIT_EXCEEDED",
        "DEPENDENCY_UNAVAILABLE",
        "EXECUTION_FAILED",
        "RESULT_VALIDATION_FAILED",
        "STORAGE_FAILED",
        "INTERNAL_FAILURE",
    }
)


class TransportValidationError(ValueError):
    """A malformed or privacy-invalid transport message."""


class EvaluationTransport:
    """Small JetStream-shaped adapter used by the isolated entrypoint.

    A concrete NATS composition supplies an object with ``publish`` and
    ``subscribe`` methods.  Keeping those methods behind this boundary means
    Core request handling never depends on the evaluation broker being alive.
    """

    def __init__(self, connection: object, *, max_bytes: int = MAX_TRANSPORT_BYTES) -> None:
        if not 1 <= max_bytes <= MAX_TRANSPORT_BYTES:
            raise ValueError("transport byte limit is invalid")
        self._connection = connection
        self._max_bytes = max_bytes

    async def publish_request(self, payload: Mapping[str, object]) -> None:
        await self._publish(EVALUATION_REQUEST_SUBJECT, payload)

    async def publish_cancellation(self, payload: Mapping[str, object]) -> None:
        await self._publish(EVALUATION_CANCELLATION_SUBJECT, payload)

    async def publish_result(self, payload: Mapping[str, object]) -> None:
        await self._publish(EVALUATION_RESULT_SUBJECT, payload)

    async def subscribe_requests(self, callback: object) -> object:
        subscribe = getattr(self._connection, "subscribe", None)
        if not callable(subscribe):
            raise RuntimeError("evaluation transport is unavailable")
        return await subscribe(EVALUATION_REQUEST_SUBJECT, cb=callback)

    async def _publish(self, subject: str, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > self._max_bytes:
            raise TransportValidationError("evaluation message exceeds transport limit")
        validate_message(payload)
        publish = getattr(self._connection, "publish", None)
        if not callable(publish):
            raise RuntimeError("evaluation transport is unavailable")
        await publish(subject, encoded)


def _object(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TransportValidationError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _closed(value: dict[str, object], allowed: set[str], *, name: str) -> None:
    if set(value) != allowed:
        raise TransportValidationError(f"{name} contains unsupported fields")


def _string(value: object, *, name: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise TransportValidationError(f"{name} is invalid")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise TransportValidationError(f"{name} is invalid")
    return value


def _uuid(value: object, *, name: str) -> UUID:
    text = _string(value, name=name, pattern=_UUID)
    try:
        return UUID(text)
    except ValueError as exc:
        raise TransportValidationError(f"{name} is invalid") from exc


def _timestamp(value: object, *, name: str) -> datetime:
    text = _string(value, name=name, pattern=_TIMESTAMP)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise TransportValidationError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise TransportValidationError(f"{name} is invalid")
    return parsed.astimezone(UTC)


def _sha(value: object, *, name: str) -> str:
    return _string(value, name=name, pattern=_SHA256)


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TransportValidationError(f"{name} is invalid")
    return value


def _capture(value: object) -> dict[str, object]:
    capture = _object(value, name="captureAuthorization")
    classification = capture.get("captureClassification")
    purpose = capture.get("authorizationPurpose")
    if classification not in {
        "metadata_only",
        "hashed",
        "redacted",
        "sampled_content",
        "full_content",
    }:
        raise TransportValidationError("capture classification is invalid")
    if purpose not in {
        "synthetic_benchmark",
        "owner_supplied_evaluation",
        "controlled_replay",
        "human_review",
    }:
        raise TransportValidationError("capture purpose is invalid")
    if capture.get("authorizationContext") != "controlled_evaluation":
        raise TransportValidationError("capture context is invalid")
    if capture.get("retentionMode") != "manual_only":
        raise TransportValidationError("capture retention is invalid")
    base = {
        "captureClassification",
        "authorizationPurpose",
        "authorizationContext",
        "retentionMode",
    }
    content = classification in {"hashed", "redacted", "sampled_content", "full_content"}
    expected = base | ({"authorizationRecordId", "authorizedAt"} if content else set())
    _closed(capture, expected, name="captureAuthorization")
    if content:
        _uuid(capture["authorizationRecordId"], name="authorizationRecordId")
        _timestamp(capture["authorizedAt"], name="authorizedAt")
    return capture


def _binding(value: object, *, name: str = "component") -> dict[str, object]:
    binding = _object(value, name=name)
    _closed(
        binding,
        {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"},
        name=name,
    )
    _string(binding["componentId"], name=f"{name}.componentId", pattern=_COMPONENT)
    _string(binding["componentVersion"], name=f"{name}.componentVersion", pattern=_IDENTIFIER)
    _string(binding["codeRevision"], name=f"{name}.codeRevision", pattern=_IDENTIFIER)
    _sha(binding["imageDigest"], name=f"{name}.imageDigest")
    _string(
        binding["configurationRevision"], name=f"{name}.configurationRevision", pattern=_IDENTIFIER
    )
    return binding


def _limits(value: object, *, name: str) -> dict[str, object]:
    limits = _object(value, name=name)
    _closed(
        limits, {"execution", "maxQueueTimeMs", "maxModelCalls", "maxArtifactOperations"}, name=name
    )
    execution = _object(limits["execution"], name=f"{name}.execution")
    _closed(
        execution,
        {
            "timeoutMs",
            "maxConcurrency",
            "maxAttempts",
            "maxCases",
            "maxInputBytes",
            "maxOutputBytes",
            "maxTokens",
        },
        name=f"{name}.execution",
    )
    bounds = {
        "timeoutMs": (100, 86_400_000),
        "maxConcurrency": (1, 64),
        "maxAttempts": (1, 10),
        "maxCases": (1, 100_000),
        "maxInputBytes": (0, 1_073_741_824),
        "maxOutputBytes": (0, 1_073_741_824),
        "maxTokens": (0, 100_000_000),
    }
    for key, (minimum, maximum) in bounds.items():
        _integer(execution[key], name=f"{name}.execution.{key}", minimum=minimum, maximum=maximum)
    _integer(
        limits["maxQueueTimeMs"], name=f"{name}.maxQueueTimeMs", minimum=100, maximum=3_600_000
    )
    _integer(limits["maxModelCalls"], name=f"{name}.maxModelCalls", minimum=0, maximum=10_000)
    _integer(
        limits["maxArtifactOperations"],
        name=f"{name}.maxArtifactOperations",
        minimum=0,
        maximum=10_000,
    )
    return limits


def _provenance(value: object) -> dict[str, object]:
    provenance = _object(value, name="provenance")
    _closed(
        provenance,
        {
            "dataset",
            "suite",
            "evaluators",
            "components",
            "model",
            "deploymentRevision",
            "reproducibility",
        },
        name="provenance",
    )
    for ref_name in ("dataset", "suite"):
        ref = _object(provenance[ref_name], name=f"provenance.{ref_name}")
        _closed(ref, {"id", "version"}, name=f"provenance.{ref_name}")
        _uuid(ref["id"], name=f"provenance.{ref_name}.id")
        _string(ref["version"], name=f"provenance.{ref_name}.version", pattern=_IDENTIFIER)
    evaluators = provenance["evaluators"]
    if not isinstance(evaluators, list) or not 1 <= len(evaluators) <= 32:
        raise TransportValidationError("provenance.evaluators is invalid")
    for index, item in enumerate(evaluators):
        evaluator = _object(item, name=f"provenance.evaluators[{index}]")
        _closed(evaluator, {"evaluatorId", "evaluatorVersion", "kind"}, name="evaluator")
        _uuid(evaluator["evaluatorId"], name="evaluatorId")
        _string(evaluator["evaluatorVersion"], name="evaluatorVersion", pattern=_IDENTIFIER)
        if evaluator["kind"] not in {"deterministic", "local_judge", "human_review"}:
            raise TransportValidationError("evaluator kind is invalid")
    components = provenance["components"]
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        raise TransportValidationError("provenance.components is invalid")
    for item in components:
        _binding(item, name="provenance.component")
    model = _object(provenance["model"], name="provenance.model")
    _closed(model, {"modelId", "modelVersion", "modelDigest"}, name="provenance.model")
    _string(model["modelId"], name="modelId", pattern=_IDENTIFIER)
    _string(model["modelVersion"], name="modelVersion", pattern=_IDENTIFIER)
    _sha(model["modelDigest"], name="modelDigest")
    _string(provenance["deploymentRevision"], name="deploymentRevision", pattern=_IDENTIFIER)
    reproducibility = _object(provenance["reproducibility"], name="reproducibility")
    _closed(
        reproducibility,
        {
            "seed",
            "clockMode",
            "timeZone",
            "locale",
            "fixtureRevision",
            "promptRevision",
            "policyRevision",
        },
        name="reproducibility",
    )
    _integer(reproducibility["seed"], name="seed", minimum=0, maximum=2_147_483_647)
    if (
        reproducibility["clockMode"] not in {"fixed", "recorded"}
        or reproducibility["timeZone"] != "UTC"
    ):
        raise TransportValidationError("reproducibility mode is invalid")
    _string(reproducibility["locale"], name="locale")
    for key in ("fixtureRevision", "promptRevision", "policyRevision"):
        _string(reproducibility[key], name=key, pattern=_IDENTIFIER)
    _string(provenance["deploymentRevision"], name="deploymentRevision", pattern=_IDENTIFIER)
    return provenance


def _artifact(value: object, *, output: bool) -> dict[str, object]:
    artifact = _object(value, name="artifactReference")
    required = {
        "artifactReferenceId",
        "mediaClassification",
        "mediaType",
        "byteCount",
        "integrity",
        "captureAuthorization",
    }
    _closed(artifact, required, name="artifactReference")
    _uuid(artifact["artifactReferenceId"], name="artifactReferenceId")
    classes = (
        {"evaluation_output", "evaluation_evidence", "evaluation_report"}
        if output
        else {"evaluation_input", "evaluation_context", "evaluation_evidence"}
    )
    if artifact["mediaClassification"] not in classes:
        raise TransportValidationError("artifact media classification is invalid")
    if artifact["mediaType"] not in {
        "application/json",
        "application/octet-stream",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "audio/wav",
    }:
        raise TransportValidationError("artifact media type is invalid")
    _integer(artifact["byteCount"], name="artifact byteCount", minimum=0, maximum=1_073_741_824)
    integrity = _object(artifact["integrity"], name="artifact.integrity")
    _closed(integrity, {"algorithm", "digest"}, name="artifact.integrity")
    if integrity["algorithm"] != "sha256":
        raise TransportValidationError("artifact integrity algorithm is invalid")
    _sha(integrity["digest"], name="artifact digest")
    _capture(artifact["captureAuthorization"])
    return artifact


def _evidence(value: object, *, kind: str) -> dict[str, object]:
    evidence = _object(value, name=kind)
    if set(evidence) - {"status", "observedValue"} or "status" not in evidence:
        raise TransportValidationError(f"{kind} is invalid")
    status = evidence["status"]
    if status not in {"verified", "missing", "mismatched"}:
        raise TransportValidationError(f"{kind} status is invalid")
    if status == "missing" and "observedValue" in evidence:
        raise TransportValidationError(f"{kind} cannot contain a value when missing")
    if status != "missing" and "observedValue" not in evidence:
        raise TransportValidationError(f"{kind} requires an observed value")
    if "observedValue" in evidence:
        if kind == "DigestEvidence":
            _sha(evidence["observedValue"], name="observedValue")
        elif kind == "SeedEvidence":
            _integer(
                evidence["observedValue"], name="observedValue", minimum=0, maximum=2_147_483_647
            )
        else:
            _string(evidence["observedValue"], name="observedValue", pattern=_IDENTIFIER)
    return evidence


def _runtime(value: object) -> dict[str, object]:
    runtime = _object(value, name="runtimeEvidence")
    keys = {
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
    }
    _closed(runtime, keys, name="runtimeEvidence")
    for key, kind in (
        ("componentVersion", "VersionEvidence"),
        ("codeRevision", "SafeValueEvidence"),
        ("imageDigest", "DigestEvidence"),
        ("configurationRevision", "SafeValueEvidence"),
        ("modelId", "SafeValueEvidence"),
        ("modelVersion", "VersionEvidence"),
        ("modelDigest", "DigestEvidence"),
        ("deploymentRevision", "SafeValueEvidence"),
        ("fixtureRevision", "SafeValueEvidence"),
        ("promptRevision", "SafeValueEvidence"),
        ("policyRevision", "SafeValueEvidence"),
        ("seed", "SeedEvidence"),
    ):
        _evidence(runtime[key], kind=kind)
    return runtime


@dataclass(frozen=True, slots=True)
class RunnerMessage:
    payload: dict[str, object]
    message_type: str
    message_id: UUID
    request_id: UUID
    job_id: UUID
    attempt_id: UUID
    idempotency_key: UUID
    request_digest: str
    occurred_at: datetime
    correlation_id: UUID
    causation_id: UUID

    @property
    def is_terminal(self) -> bool:
        return self.message_type in TERMINAL_TYPES


def validate_message(payload: Mapping[str, object]) -> RunnerMessage:
    """Validate one exact transport envelope and return only safe metadata."""

    value = dict(payload)
    allowed = {
        "schemaVersion",
        "messageId",
        "messageType",
        "occurredAt",
        "correlationId",
        "causationId",
        "requestId",
        "jobId",
        "attemptId",
        "idempotencyKey",
        "requestDigest",
        "data",
    }
    _closed(value, allowed, name="runner envelope")
    if value["schemaVersion"] != TRANSPORT_VERSION:
        raise TransportValidationError("unsupported transport version")
    message_type = _string(value["messageType"], name="messageType")
    if message_type not in {REQUEST_TYPE, CANCELLATION_TYPE, *TERMINAL_TYPES}:
        raise TransportValidationError("unsupported message type")
    ids = {
        key: _uuid(value[key], name=key)
        for key in (
            "messageId",
            "correlationId",
            "causationId",
            "requestId",
            "jobId",
            "attemptId",
            "idempotencyKey",
        )
    }
    occurred_at = _timestamp(value["occurredAt"], name="occurredAt")
    request_digest = _sha(value["requestDigest"], name="requestDigest")
    data = _object(value["data"], name="data")
    if message_type == REQUEST_TYPE:
        _closed(
            data,
            {
                "executionKind",
                "requestVersion",
                "targetComponent",
                "captureAuthorization",
                "provenance",
                "limits",
                "deadlineAt",
                "caseIds",
                "inputArtifactReferences",
            },
            name="request data",
        )
        if data["executionKind"] not in {"module_replay", "synthetic_end_to_end"}:
            raise TransportValidationError("execution kind is invalid")
        _string(data["requestVersion"], name="requestVersion", pattern=_IDENTIFIER)
        _binding(data["targetComponent"], name="targetComponent")
        capture = _capture(data["captureAuthorization"])
        provenance = _provenance(data["provenance"])
        target = cast(dict[str, object], data["targetComponent"])
        components = provenance["components"]
        if not isinstance(components, list) or target not in components:
            raise TransportValidationError("target component is not bound by provenance")
        _limits(data["limits"], name="limits")
        _timestamp(data["deadlineAt"], name="deadlineAt")
        cases = data["caseIds"]
        if (
            not isinstance(cases, list)
            or not 1 <= len(cases) <= 1000
            or len({str(item) for item in cases}) != len(cases)
        ):
            raise TransportValidationError("caseIds is invalid")
        for item in cases:
            _uuid(item, name="caseId")
        refs = data["inputArtifactReferences"]
        if not isinstance(refs, list) or len(refs) > 32:
            raise TransportValidationError("input artifact references are invalid")
        if capture["captureClassification"] in {"metadata_only", "hashed"} and refs:
            raise TransportValidationError("content artifacts require explicit authorization")
        for item in refs:
            _artifact(item, output=False)
        if request_digest != canonical_digest(value):
            raise TransportValidationError("requestDigest does not match canonical request data")
    elif message_type == CANCELLATION_TYPE:
        _closed(data, {"cancellationId", "reasonCode", "deadlineAt"}, name="cancellation data")
        _uuid(data["cancellationId"], name="cancellationId")
        if data["reasonCode"] not in {"OWNER_REQUESTED", "SUPERSEDED"}:
            raise TransportValidationError("cancellation reason is invalid")
        _timestamp(data["deadlineAt"], name="deadlineAt")
    else:
        common = {
            "resultId",
            "resultDigest",
            "status",
            "completedAt",
            "deadlineAt",
            "captureAuthorization",
            "effectiveLimits",
            "provenance",
            "runtimeEvidence",
        }
        if message_type == "evaluation.runner.completed":
            _closed(data, common | {"startedAt", "outputArtifactReferences"}, name="completed data")
            _timestamp(data["startedAt"], name="startedAt")
            refs = data["outputArtifactReferences"]
            if not isinstance(refs, list) or len(refs) > 32:
                raise TransportValidationError("output artifact references are invalid")
            for item in refs:
                _artifact(item, output=True)
            if data["status"] != "completed":
                raise TransportValidationError("completed status is invalid")
        elif message_type == "evaluation.runner.cancelled":
            _closed(data, common | {"reasonCode"}, name="cancelled data")
            if data["status"] != "cancelled" or data["reasonCode"] not in {
                "OWNER_REQUESTED",
                "DEADLINE_EXCEEDED",
                "SUPERSEDED",
                "SHUTDOWN",
            }:
                raise TransportValidationError("cancelled data is invalid")
        else:
            _closed(data, common | {"failureCode", "retryable"}, name="failed data")
            if (
                data["status"] != "failed"
                or data["failureCode"] not in FAILURE_CODES
                or type(data["retryable"]) is not bool
            ):
                raise TransportValidationError("failed data is invalid")
        _uuid(data["resultId"], name="resultId")
        _sha(data["resultDigest"], name="resultDigest")
        _timestamp(data["completedAt"], name="completedAt")
        _timestamp(data["deadlineAt"], name="deadlineAt")
        _capture(data["captureAuthorization"])
        _limits(data["effectiveLimits"], name="effectiveLimits")
        _provenance(data["provenance"])
        _runtime(data["runtimeEvidence"])
    return RunnerMessage(
        value,
        message_type,
        ids["messageId"],
        ids["requestId"],
        ids["jobId"],
        ids["attemptId"],
        ids["idempotencyKey"],
        request_digest,
        occurred_at,
        ids["correlationId"],
        ids["causationId"],
    )


def canonical_digest(payload: Mapping[str, object]) -> str:
    """Return the SHA-256 digest of canonical request data.

    Envelope identifiers and delivery timestamps are transport metadata.  The
    digest therefore covers only the closed request ``data`` projection so a
    cancellation or terminal redelivery can carry the original request
    identity without changing its content digest.
    """

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("request data is required for canonical digest")
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def deterministic_terminal_id(attempt_id: UUID, status: str) -> UUID:
    """Derive a stable terminal result identity from one attempt and status."""

    import uuid

    if status not in {"completed", "cancelled", "failed"}:
        raise ValueError("unsupported terminal status")
    return uuid.uuid5(attempt_id, f"evaluation-runner:{status}:v1")


def deterministic_message_id(result_id: UUID, status: str) -> UUID:
    import uuid

    if status not in {"completed", "cancelled", "failed"}:
        raise ValueError("unsupported terminal status")
    return uuid.uuid5(result_id, f"evaluation-runner-message:{status}:v1")


__all__ = [
    "CANCELLATION_TYPE",
    "EVALUATION_CANCELLATION_SUBJECT",
    "EVALUATION_REQUEST_SUBJECT",
    "EVALUATION_RESULT_SUBJECT",
    "EVALUATION_STREAM",
    "EvaluationTransport",
    "FAILURE_CODES",
    "FailureCode",
    "REQUEST_TYPE",
    "RunnerMessage",
    "TERMINAL_TYPES",
    "TransportValidationError",
    "canonical_digest",
    "deterministic_message_id",
    "deterministic_terminal_id",
    "validate_message",
]
