"""Durable Observatory-owned evaluation orchestration.

The application layer owns the monotonic job state machine.  PostgreSQL is the
source of truth; NATS is used only by the transport port.  All identifiers,
references, digests, phases and failure codes are bounded metadata.
"""

# JSON Schema is intentionally traversed through a dynamic, closed data model;
# the boundary below narrows all values before they enter the state machine.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aura_observatory.ports import (
    EvaluationDispatchIntent,
    EvaluationTransportPort,
)

_LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 86_400
MAX_CADENCE_SECONDS = 2_592_000
SAFE_IDENTIFIER_LENGTH = 128
SCHEDULER_COMPONENT_ID = "aura.observatory.evaluation_scheduler"
DEFAULT_WORKER_ID = "00000000-0000-4000-8000-000000000092"
DEFAULT_SCHEDULER_ID = "00000000-0000-4000-8000-000000000093"
RETRYABLE_FAILURES = frozenset({"DEPENDENCY_UNAVAILABLE", "STORAGE_FAILED"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "deadline"})


class TransportValidationError(ValueError):
    """Bounded contract failure shared by application and provider boundaries."""

    def __init__(self, code: str = "malformed_envelope") -> None:
        self.code = code
        super().__init__(code)


_TRANSPORT_SCHEMA_DIR = Path(__file__).resolve().parents[4] / "contracts/evaluation/v1"
_SCHEMA_CACHE: dict[str, Mapping[str, object]] = {}
_SCHEMA_ROOTS: dict[int, Mapping[str, object]] = {}
_FORBIDDEN_TRANSPORT_KEYS = frozenset(
    {
        "prompt",
        "response",
        "content",
        "filename",
        "argument",
        "raw_result",
        "credential",
        "credentials",
        "endpoint",
        "bearer",
        "password",
        "owner",
        "identity",
        "owner_identity",
        "token",
        "objectkey",
        "object_key",
        "url",
        "uri",
    }
)


def _schema_document(filename: str) -> Mapping[str, object]:
    document = _SCHEMA_CACHE.get(filename)
    if document is not None:
        return document
    try:
        loaded = json.loads((_TRANSPORT_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        raise TransportValidationError("schema_unavailable") from None
    if not isinstance(loaded, Mapping):
        raise TransportValidationError("schema_unavailable")
    document = cast(Mapping[str, object], loaded)
    _SCHEMA_CACHE[filename] = document
    return document


def _resolve_schema(reference: str, root: Mapping[str, object]) -> Mapping[str, object]:
    filename, _, fragment = reference.partition("#")
    document = root if not filename else _schema_document(Path(filename).name)
    value: object = document
    for part in fragment.removeprefix("/").split("/") if fragment else ():
        if not isinstance(value, Mapping):
            raise TransportValidationError("schema_invalid")
        typed_value: Mapping[str, object] = value
        value = typed_value.get(part.replace("~1", "/").replace("~0", "~"))
    if not isinstance(value, Mapping):
        raise TransportValidationError("schema_invalid")
    resolved = cast(Mapping[str, object], value)
    if filename:
        _SCHEMA_ROOTS[id(resolved)] = document
    return resolved


def _schema_matches(
    value: object, schema: Mapping[str, object], root: Mapping[str, object]
) -> bool:
    try:
        _validate_schema(value, schema, root)
    except TransportValidationError:
        return False
    return True


def _validate_schema(
    value: object, schema: Mapping[str, object], root: Mapping[str, object]
) -> None:
    root = _SCHEMA_ROOTS.get(id(schema), root)
    if "$ref" in schema:
        reference = schema.get("$ref")
        if not isinstance(reference, str):
            raise TransportValidationError("schema_invalid")
        _validate_schema(value, _resolve_schema(reference, root), root)
        return
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = sum(
            _schema_matches(value, cast(Mapping[str, object], option), root)
            for option in one_of
            if isinstance(option, Mapping)
        )
        if matches != 1:
            raise TransportValidationError("invalid_value")
        return
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for option in all_of:
            if isinstance(option, Mapping):
                _validate_schema(value, cast(Mapping[str, object], option), root)
    conditional = schema.get("if")
    if isinstance(conditional, Mapping):
        branch = (
            schema.get("then") if _schema_matches(value, conditional, root) else schema.get("else")
        )
        if isinstance(branch, Mapping):
            _validate_schema(value, cast(Mapping[str, object], branch), root)
    negated = schema.get("not")
    if isinstance(negated, Mapping) and _schema_matches(value, negated, root):
        raise TransportValidationError("invalid_value")
    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(value, Mapping):
        raise TransportValidationError("invalid_object")
    if expected_type == "array" and not isinstance(value, list):
        raise TransportValidationError("invalid_array")
    if expected_type == "string" and not isinstance(value, str):
        raise TransportValidationError("invalid_value")
    if expected_type == "integer" and (type(value) is not int):
        raise TransportValidationError("invalid_value")
    if expected_type == "boolean" and type(value) is not bool:
        raise TransportValidationError("invalid_value")
    if "const" in schema and value != schema.get("const"):
        raise TransportValidationError("invalid_value")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise TransportValidationError("invalid_value")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(
            schema.get("maxLength", 2**31)
        ):
            raise TransportValidationError("invalid_value")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise TransportValidationError("invalid_value")
    if type(value) is int:
        if value < int(schema.get("minimum", -(2**63))) or value > int(
            schema.get("maximum", 2**63 - 1)
        ):
            raise TransportValidationError("invalid_value")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)) or len(value) > int(
            schema.get("maxItems", 2**31)
        ):
            raise TransportValidationError("limit_exceeded")
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise TransportValidationError("invalid_value")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for item in value:
                _validate_schema(item, cast(Mapping[str, object], item_schema), root)
    if isinstance(value, Mapping):
        typed = cast(Mapping[str, object], value)
        required = schema.get("required")
        if isinstance(required, list) and any(
            key not in typed for key in required if isinstance(key, str)
        ):
            raise TransportValidationError("unsupported_field")
        properties = schema.get("properties")
        typed_properties = (
            cast(Mapping[str, object], properties) if isinstance(properties, Mapping) else {}
        )
        if schema.get("additionalProperties") is False and any(
            key not in typed_properties for key in typed
        ):
            raise TransportValidationError("unsupported_field")
        for key, child_schema in typed_properties.items():
            if key in typed and isinstance(child_schema, Mapping):
                _validate_schema(typed[key], cast(Mapping[str, object], child_schema), root)


def _walk_transport(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in cast(Mapping[object, object], value).items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise TransportValidationError("unsupported_field")
            if key.casefold().replace("-", "_") in _FORBIDDEN_TRANSPORT_KEYS:
                raise TransportValidationError("privacy_field")
            _walk_transport(child)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise TransportValidationError("limit_exceeded")
        for child in value:
            _walk_transport(child)
    elif isinstance(value, str):
        if len(value) > 4096:
            raise TransportValidationError("limit_exceeded")
    elif not isinstance(value, (int, float, bool, type(None))):
        raise TransportValidationError("unsupported_field")


def _schema_ref_for_data(message_type: str) -> str:
    return {
        "evaluation.runner.requested": "#/$defs/RequestData",
        "evaluation.runner.cancellation_requested": "#/$defs/CancellationData",
        "evaluation.runner.completed": "#/$defs/CompletedData",
        "evaluation.runner.cancelled": "#/$defs/CancelledData",
        "evaluation.runner.failed": "#/$defs/FailedData",
    }.get(message_type, "")


def validate_runner_data(message_type: str, value: object) -> dict[str, object]:
    root = _schema_document("evaluation-runner-transport.schema.json")
    reference = _schema_ref_for_data(message_type)
    if not reference:
        raise TransportValidationError("unsupported_message")
    _walk_transport(value)
    _validate_schema(value, _resolve_schema(reference, root), root)
    return cast(
        dict[str, object], json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
    )


def validate_transport_envelope(
    value: Mapping[str, object], *, max_bytes: int = 1_000_000
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TransportValidationError("malformed_envelope")
    candidate = dict(value)
    message_type = candidate.get("messageType")
    root = _schema_document("evaluation-runner-transport.schema.json")
    references = {
        "evaluation.runner.requested": "#/$defs/RunnerRequest",
        "evaluation.runner.cancellation_requested": "#/$defs/CancellationRequest",
        "evaluation.runner.completed": "#/$defs/CompletedResult",
        "evaluation.runner.cancelled": "#/$defs/CancelledResult",
        "evaluation.runner.failed": "#/$defs/FailedResult",
    }
    reference = references.get(message_type) if isinstance(message_type, str) else None
    if reference is None:
        raise TransportValidationError("unsupported_message")
    _walk_transport(candidate)
    _validate_schema(candidate, _resolve_schema(reference, root), root)
    encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise TransportValidationError("limit_exceeded")
    return cast(dict[str, object], json.loads(encoded.decode("utf-8")))


def canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def encode_envelope(envelope: Mapping[str, object], *, max_bytes: int = 1_000_000) -> bytes:
    return json.dumps(
        validate_transport_envelope(envelope, max_bytes=max_bytes),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_envelope(payload: bytes, *, max_bytes: int = 1_000_000) -> dict[str, object]:
    if len(payload) > max_bytes:
        raise TransportValidationError("limit_exceeded")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise TransportValidationError("malformed_envelope") from None
    if not isinstance(decoded, Mapping):
        raise TransportValidationError("malformed_envelope")
    return validate_transport_envelope(cast(Mapping[str, object], decoded), max_bytes=max_bytes)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class AttemptState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class OrchestrationError(RuntimeError):
    """Bounded application failure; provider diagnostics never cross the API."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OrchestrationDependencyError(OrchestrationError):
    """Bounded failure attributed to an infrastructure dependency."""

    def __init__(self, code: str, dependency: str) -> None:
        self.dependency = dependency
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EvaluationAdmission:
    request_id: str
    idempotency_key: str
    request_version: str
    dataset_id: str
    dataset_version: str
    suite_id: str
    suite_version: str
    evaluator_id: str
    evaluator_version: str
    capture_classification: str
    deadline_at: datetime
    max_attempts: int
    envelope: Mapping[str, object]
    request_fingerprint: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def validate(self, *, now: datetime | None = None) -> None:
        for name in (
            "request_id",
            "idempotency_key",
            "dataset_id",
            "suite_id",
            "evaluator_id",
        ):
            _require_uuid(getattr(self, name), name)
        for name in (
            "request_version",
            "dataset_version",
            "suite_version",
            "evaluator_version",
        ):
            _require_identifier(getattr(self, name), name)
        if self.capture_classification not in {
            "metadata_only",
            "hashed",
            "redacted",
            "sampled_content",
            "full_content",
        }:
            raise OrchestrationError("capture_classification_invalid")
        _require_utc(self.deadline_at, "deadline_at")
        _require_utc(self.created_at, "created_at")
        current = now or datetime.now(UTC)
        if self.deadline_at <= current:
            raise OrchestrationError("deadline_expired")
        if not 1 <= self.max_attempts <= MAX_ATTEMPTS:
            raise OrchestrationError("max_attempts_invalid")
        if set(self.envelope) != {"data"}:
            raise OrchestrationError("request_envelope_invalid")
        data = self.envelope.get("data")
        if not isinstance(data, Mapping):
            raise OrchestrationError("request_envelope_invalid")
        typed_data = cast(Mapping[str, object], data)
        try:
            typed_data = validate_runner_data("evaluation.runner.requested", typed_data)
        except TransportValidationError as error:
            raise OrchestrationError(error.code) from None
        _require_equal_binding(
            self.request_version, typed_data.get("requestVersion"), "request_version"
        )
        provenance = _mapping(typed_data.get("provenance"), "provenance")
        _require_reference(provenance, "dataset", self.dataset_id, self.dataset_version)
        _require_reference(provenance, "suite", self.suite_id, self.suite_version)
        if not _has_evaluator(
            provenance.get("evaluators"), self.evaluator_id, self.evaluator_version
        ):
            raise OrchestrationError("evaluator_binding_mismatch")
        capture = _mapping(typed_data.get("captureAuthorization"), "captureAuthorization")
        _require_equal_binding(
            self.capture_classification,
            capture.get("captureClassification"),
            "capture_classification",
        )
        deadline_value = typed_data.get("deadlineAt")
        if not isinstance(deadline_value, str) or deadline_value != _timestamp(self.deadline_at):
            raise OrchestrationError("deadline_binding_mismatch")
        limits = _mapping(typed_data.get("limits"), "limits")
        execution = _mapping(limits.get("execution"), "limits.execution")
        if execution.get("maxAttempts") != self.max_attempts:
            raise OrchestrationError("max_attempts_binding_mismatch")
        fingerprint = self.fingerprint()
        if self.request_fingerprint and self.request_fingerprint != fingerprint:
            raise OrchestrationError("request_fingerprint_mismatch")

    def fingerprint(self) -> str:
        # The runner contract binds requestDigest to the exact canonical
        # ``data`` projection, not to this service's admission wrapper.
        projection = self.envelope.get("data")
        if not isinstance(projection, Mapping):
            raise OrchestrationError("request_envelope_invalid")
        return canonical_digest(cast(Mapping[str, object], projection))


@dataclass(frozen=True, slots=True)
class EvaluationJob:
    job_id: str
    admission: EvaluationAdmission
    state: JobState = JobState.QUEUED
    attempt_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class EvaluationAttempt:
    attempt_id: str
    job_id: str
    attempt_number: int
    request_digest: str
    state: AttemptState = AttemptState.QUEUED
    next_attempt_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationOutboxEntry:
    event_id: str
    event_type: str
    job_id: str
    attempt_id: str
    envelope: Mapping[str, object]
    published_at: datetime | None = None
    publish_attempts: int = 0
    last_error_code: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_publish_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ResultApplication:
    """Atomic result disposition and any terminal transition it caused.

    ``out_of_order`` alone is intentionally not enough for callers: a result
    arriving after a deadline can be the transaction that authoritatively
    settles the job, while a later duplicate must not count that transition a
    second time.  Repositories return this value from the same transaction as
    the receipt/state writes.
    """

    disposition: str
    terminal_transition: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationSchedule:
    schedule_id: str
    occurrence_id: str
    dataset_id: str
    dataset_version: str
    suite_id: str
    suite_version: str
    evaluator_id: str
    evaluator_version: str
    next_run_at: datetime
    cadence_seconds: int
    deadline_seconds: int
    max_attempts: int = 3
    enabled: bool = True
    request_envelope: Mapping[str, object] | None = None

    def validate(self) -> None:
        for name in (
            "schedule_id",
            "occurrence_id",
            "dataset_id",
            "suite_id",
            "evaluator_id",
        ):
            _require_uuid(getattr(self, name), name)
        for name in ("dataset_version", "suite_version", "evaluator_version"):
            _require_identifier(getattr(self, name), name)
        _require_utc(self.next_run_at, "next_run_at")
        if not 60 <= self.cadence_seconds <= MAX_CADENCE_SECONDS:
            raise OrchestrationError("schedule_cadence_invalid")
        if not 1 <= self.deadline_seconds <= MAX_CADENCE_SECONDS:
            raise OrchestrationError("schedule_deadline_invalid")
        if not 1 <= self.max_attempts <= MAX_ATTEMPTS:
            raise OrchestrationError("schedule_attempts_invalid")
        if self.request_envelope is None or set(self.request_envelope) != {"data"}:
            raise OrchestrationError("schedule_request_template_missing")
        data = self.request_envelope.get("data")
        if not isinstance(data, Mapping):
            raise OrchestrationError("schedule_request_template_invalid")
        try:
            typed_data = validate_runner_data(
                "evaluation.runner.requested", cast(Mapping[str, object], data)
            )
        except TransportValidationError as error:
            raise OrchestrationError(error.code) from None
        provenance = _mapping(typed_data.get("provenance"), "provenance")
        _require_reference(provenance, "dataset", self.dataset_id, self.dataset_version)
        _require_reference(provenance, "suite", self.suite_id, self.suite_version)
        if not _has_evaluator(
            provenance.get("evaluators"), self.evaluator_id, self.evaluator_version
        ):
            raise OrchestrationError("evaluator_binding_mismatch")
        limits = _mapping(typed_data.get("limits"), "limits")
        execution = _mapping(limits.get("execution"), "limits.execution")
        if execution.get("maxAttempts") != self.max_attempts:
            raise OrchestrationError("schedule_attempts_binding_mismatch")


class EvaluationOrchestrationRepository(Protocol):
    async def admit(self, request: EvaluationAdmission, job_id: str) -> EvaluationJob: ...

    async def get_job(self, job_id: str) -> EvaluationJob | None: ...

    async def claim_due_attempts(
        self, *, now: datetime, limit: int, lease_seconds: int, worker_id: str
    ) -> Sequence[EvaluationAttempt]: ...

    async def claim_outbox(
        self, *, now: datetime, limit: int, lease_seconds: int = 120, worker_id: str = ""
    ) -> Sequence[EvaluationOutboxEntry]: ...

    async def mark_outbox_published(
        self, event_id: str, *, at: datetime, lease_token: str | None = None
    ) -> bool: ...

    async def mark_outbox_failed(
        self, event_id: str, code: str, *, at: datetime, lease_token: str | None = None
    ) -> None: ...

    async def request_cancellation(
        self, job_id: str, *, reason: str, now: datetime
    ) -> EvaluationOutboxEntry | None: ...

    async def apply_result(
        self, envelope: Mapping[str, object], *, received_at: datetime
    ) -> ResultApplication: ...

    async def recover(self, *, now: datetime, limit: int, worker_id: str) -> int: ...

    async def settle_expired(self, *, now: datetime, limit: int) -> int: ...

    async def heartbeat(
        self, *, component_id: str, lease_token: str, at: datetime, backlog_count: int = 0
    ) -> Mapping[str, int]: ...

    async def materialize_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> int: ...


class EvaluationSchedulerService:
    """Application scheduler that materializes each occurrence transactionally."""

    def __init__(
        self,
        repository: EvaluationOrchestrationRepository,
        *,
        metrics: Any | None = None,
        worker_id: str = DEFAULT_SCHEDULER_ID,
        batch_size: int = 32,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size out of bounds")
        self.repository = repository
        self.metrics = metrics
        self.worker_id = _bounded_worker_id(worker_id)
        self.batch_size = batch_size

    async def run_once(self, *, now: datetime | None = None) -> int:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            materialized = await self.repository.materialize_due_schedules(
                now=at, limit=self.batch_size, lease_token=self.worker_id
            )
        except Exception:
            self._span("schedule_materialize", started, "failure", "database_failure", "postgresql")
            raise
        self._span("schedule_materialize", started, "success", None, "postgresql")
        self._metric(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": "postgresql", "outcome": "success"},
        )
        if materialized:
            self._metric(
                "observatory_evaluation_scheduler_occurrence_count",
                value=materialized,
                labels={"outcome": "materialized"},
            )
        return materialized

    def _span(
        self,
        operation: str,
        started: float,
        outcome: str,
        error_code: str | None,
        dependency: str | None,
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if callable(recorder):
            try:
                recorder(
                    component_id=SCHEDULER_COMPONENT_ID,
                    component_version="1.0.0",
                    deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")),
                    operation=operation,
                    duration_ms=(monotonic() - started) * 1000.0,
                    outcome=outcome,
                    error_code=error_code,
                    dependency=dependency,
                )
            except Exception:
                _LOGGER.debug("bounded scheduler span unavailable")

    async def heartbeat_once(self, *, now: datetime | None = None) -> Mapping[str, int]:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            snapshot = await self.repository.heartbeat(
                component_id=SCHEDULER_COMPONENT_ID,
                lease_token=_lease_token_for_worker(self.worker_id),
                at=at,
            )
        except Exception as error:
            self._span("heartbeat", started, "failure", "database_failure", "postgresql")
            self._metric(
                "observatory_evaluation_dependency_outcome_count",
                labels={"operation": "postgresql", "outcome": "failed"},
            )
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        self._span("heartbeat", started, "success", None, "postgresql")
        return snapshot

    def _metric(
        self,
        name: str,
        *,
        value: float = 1,
        labels: Mapping[str, str],
        replace: bool = False,
    ) -> None:
        if self.metrics is None:
            return
        try:
            (self.metrics.set if replace else self.metrics.record)(name, value, labels)
        except Exception:
            _LOGGER.debug("bounded scheduler metric unavailable")


class EvaluationOrchestrationService:
    """Use-case boundary for admission, dispatch, results, retries and cancel."""

    def __init__(
        self,
        repository: EvaluationOrchestrationRepository,
        transport: EvaluationTransportPort | None = None,
        *,
        metrics: Any | None = None,
        worker_id: str = DEFAULT_WORKER_ID,
        batch_size: int = 32,
        lease_seconds: int = 120,
        retry_base_seconds: int = 10,
        retry_max_seconds: int = 3600,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size out of bounds")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds out of bounds")
        if not 1 <= retry_base_seconds <= retry_max_seconds <= MAX_BACKOFF_SECONDS:
            raise ValueError("retry backoff out of bounds")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("max attempts out of bounds")
        self.repository = repository
        self.transport = transport
        self.metrics = metrics
        self.worker_id = _bounded_worker_id(worker_id)
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.max_attempts = max_attempts

    async def admit(
        self, request: EvaluationAdmission, *, now: datetime | None = None
    ) -> EvaluationJob:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        request.validate(now=at)
        if request.max_attempts > self.max_attempts:
            raise OrchestrationError("max_attempts_exceeds_deployment")
        job_id = _deterministic_job_id(request.idempotency_key)
        job = await self.repository.admit(request, job_id)
        self._metric("observatory_evaluation_job_admission_count", labels={"outcome": "accepted"})
        return job

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            settled = await self.repository.settle_expired(now=at, limit=self.batch_size)
            if settled:
                self._metric(
                    "observatory_evaluation_deadline_count",
                    value=settled,
                    labels={"outcome": "deadline"},
                )
                self._metric(
                    "observatory_evaluation_terminal_count",
                    value=settled,
                    labels={"outcome": "deadline"},
                )
            attempts = await self.repository.claim_due_attempts(
                now=at,
                limit=self.batch_size,
                lease_seconds=self.lease_seconds,
                worker_id=self.worker_id,
            )
        except Exception as error:
            self._record_dependency_failure("dispatch", started, "postgresql")
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        self._span("dispatch", started, "success", None, "postgresql")
        if attempts:
            self._metric(
                "observatory_evaluation_attempt_transition_count",
                value=len(attempts),
                labels={"outcome": "claimed"},
            )
        return len(attempts)

    async def publish_outbox_once(self, *, now: datetime | None = None) -> int:
        if self.transport is None:
            raise OrchestrationError("transport_unavailable")
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            entries = await self.repository.claim_outbox(
                now=at,
                limit=self.batch_size,
                lease_seconds=self.lease_seconds,
                worker_id=self.worker_id,
            )
        except Exception as error:
            self._record_dependency_failure("publish", started, "postgresql")
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        self._metric(
            "observatory_evaluation_outbox_pending_count",
            value=len(entries),
            labels={},
            replace=True,
        )
        count = 0
        publish_failed = False
        for entry in entries:
            kind = (
                "request" if entry.event_type == "evaluation.runner.requested" else "cancellation"
            )
            try:
                await self.transport.publish(
                    EvaluationDispatchIntent(kind=kind, envelope=entry.envelope), entry.event_id
                )
            except asyncio.CancelledError:
                # Provider cancellation is a transport failure, not a reason to
                # abandon the durable lease.  Finish the state transition before
                # preserving cancellation for the caller/worker boundary.
                transitioned = await self._mark_outbox_failed_after_cancellation(
                    entry.event_id, at=at, lease_token=entry.lease_token
                )
                if not transitioned:
                    self._record_dependency_failure("publish", started, "postgresql")
                self._record_publish_failure_metrics()
                self._span(
                    "publish", started, "failure", "transport_unavailable", "evaluation_nats"
                )
                raise
            except Exception:
                publish_failed = True
                try:
                    await self.repository.mark_outbox_failed(
                        entry.event_id,
                        "transport_unavailable",
                        at=at,
                        lease_token=entry.lease_token,
                    )
                except Exception as error:
                    self._record_dependency_failure("publish", started, "postgresql")
                    raise OrchestrationDependencyError("database_failure", "postgresql") from error
                self._metric(
                    "observatory_evaluation_outbox_publication_count",
                    labels={"outcome": "failed"},
                )
                self._metric(
                    "observatory_evaluation_dependency_outcome_count",
                    labels={"operation": "evaluation_nats", "outcome": "failed"},
                )
                continue
            try:
                published = await self.repository.mark_outbox_published(
                    entry.event_id, at=at, lease_token=entry.lease_token
                )
            except Exception as error:
                self._record_dependency_failure("publish", started, "postgresql")
                raise OrchestrationDependencyError("database_failure", "postgresql") from error
            if published:
                count += 1
                self._metric(
                    "observatory_evaluation_outbox_publication_count",
                    labels={"outcome": "published"},
                )
                self._metric(
                    "observatory_evaluation_dependency_outcome_count",
                    labels={"operation": "evaluation_nats", "outcome": "published"},
                )
        self._span(
            "publish",
            started,
            "failure" if publish_failed else "success",
            "transport_unavailable" if publish_failed else None,
            "evaluation_nats",
        )
        return count

    async def heartbeat_once(self, *, now: datetime | None = None) -> Mapping[str, int]:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            snapshot = await self.repository.heartbeat(
                component_id="aura.observatory.evaluation_orchestration",
                lease_token=_lease_token_for_worker(self.worker_id),
                at=at,
            )
        except Exception as error:
            self._record_dependency_failure("heartbeat", started, "postgresql")
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        for name, value in snapshot.items():
            metric_name = {
                "backlog_count": "observatory_evaluation_backlog_count",
                "outbox_pending_count": "observatory_evaluation_outbox_pending_count",
                "outbox_oldest_age_seconds": "observatory_evaluation_outbox_oldest_age_seconds",
            }.get(name)
            if metric_name is not None:
                self._metric(metric_name, value=float(value), labels={}, replace=True)
        self._span("heartbeat", started, "success", None, "postgresql")
        return snapshot

    async def cancel(
        self, job_id: str, *, reason: str = "OWNER_REQUESTED", now: datetime | None = None
    ) -> bool:
        if reason not in {"OWNER_REQUESTED", "SUPERSEDED"}:
            raise OrchestrationError("cancellation_reason_invalid")
        at = (now or datetime.now(UTC)).astimezone(UTC)
        entry = await self.repository.request_cancellation(job_id, reason=reason, now=at)
        if entry is None:
            return False
        self._metric("observatory_evaluation_cancellation_count", labels={"outcome": "accepted"})
        self._metric("observatory_evaluation_terminal_count", labels={"outcome": "cancelled"})
        return True

    async def consume_result(
        self,
        envelope: Mapping[str, object],
        *,
        now: datetime | None = None,
        acknowledge: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            validated = validate_transport_envelope(envelope)
            if validated["messageType"] not in {
                "evaluation.runner.completed",
                "evaluation.runner.cancelled",
                "evaluation.runner.failed",
            }:
                raise TransportValidationError("unexpected_message")
            application = await self.repository.apply_result(validated, received_at=at)
            disposition = application.disposition
            self._span(
                "result_apply",
                started,
                "duplicate" if disposition == "duplicate" else "success",
                None,
                "postgresql",
            )
            self._metric(
                "observatory_evaluation_dependency_outcome_count",
                labels={"operation": "postgresql", "outcome": "success"},
            )
        except (TransportValidationError, OrchestrationError) as error:
            if acknowledge is not None:
                await acknowledge()
            _LOGGER.info("evaluation result rejected", extra={"reason": error.code})
            self._metric(
                "observatory_evaluation_result_consumption_count", labels={"outcome": "rejected"}
            )
            # Contract/schema rejection occurs before any repository call; it
            # has no infrastructure dependency and must not be attributed to
            # PostgreSQL in spans or dependency outcome metrics.
            self._span("result_apply", started, "failure", "validation", None)
            return "rejected"
        except Exception as error:
            self._record_dependency_failure("result_apply", started, "postgresql")
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        if acknowledge is not None:
            await acknowledge()
        if application.terminal_transition == "deadline":
            self._metric(
                "observatory_evaluation_deadline_count", labels={"outcome": "deadline"}
            )
            self._metric(
                "observatory_evaluation_terminal_count", labels={"outcome": "deadline"}
            )
        message_type = str(validated["messageType"])
        data = cast(Mapping[str, object], validated["data"])
        retry_scheduled = False
        if disposition == "applied" and message_type.endswith("failed"):
            try:
                job = await self.repository.get_job(str(validated["jobId"]))
            except Exception as error:
                self._record_dependency_failure("result_apply", started, "postgresql")
                raise OrchestrationDependencyError("database_failure", "postgresql") from error
            if job is not None and job.state is JobState.QUEUED and data.get("retryable") is True:
                retry_scheduled = True
                self._metric(
                    "observatory_evaluation_retry_scheduled_count",
                    labels={"outcome": "accepted"},
                )
        if (
            disposition == "applied"
            and not retry_scheduled
            and message_type.endswith(("completed", "cancelled", "failed"))
        ):
            terminal_outcome = {
                "completed": "succeeded",
                "cancelled": "cancelled",
                "failed": "failed",
            }[message_type.rsplit(".", 1)[-1]]
            self._metric(
                "observatory_evaluation_attempt_transition_count",
                labels={"outcome": terminal_outcome},
            )
            self._metric(
                "observatory_evaluation_terminal_count",
                labels={"outcome": terminal_outcome},
            )
        completed_at = data.get("completedAt")
        if disposition != "duplicate" and isinstance(completed_at, str):
            try:
                lag = max(0.0, (at - _parse_timestamp(completed_at)).total_seconds())
            except OrchestrationError:
                lag = 0.0
            self._metric("observatory_evaluation_result_lag_seconds", value=lag, labels={})
        self._metric(
            "observatory_evaluation_result_consumption_count", labels={"outcome": disposition}
        )
        return disposition

    async def consume_results(self, stop: asyncio.Event) -> None:
        if self.transport is None:
            raise OrchestrationError("transport_unavailable")
        async for delivery in self.transport.results(stop):
            await self.consume_result(delivery.envelope, acknowledge=delivery.acknowledge)

    async def recover_once(self, *, now: datetime | None = None) -> int:
        at = (now or datetime.now(UTC)).astimezone(UTC)
        started = monotonic()
        try:
            settled = await self.repository.settle_expired(now=at, limit=self.batch_size)
            if settled:
                self._metric(
                    "observatory_evaluation_deadline_count",
                    value=settled,
                    labels={"outcome": "deadline"},
                )
                self._metric(
                    "observatory_evaluation_terminal_count",
                    value=settled,
                    labels={"outcome": "deadline"},
                )
            recovered = await self.repository.recover(
                now=at, limit=self.batch_size, worker_id=self.worker_id
            )
        except Exception as error:
            self._record_dependency_failure("recover", started, "postgresql")
            raise OrchestrationDependencyError("database_failure", "postgresql") from error
        if recovered:
            self._metric(
                "observatory_evaluation_attempt_transition_count",
                value=recovered,
                labels={"outcome": "recovered"},
            )
        self._span("recover", started, "success", None, "postgresql")
        return recovered

    def _record_dependency_failure(self, operation: str, started: float, dependency: str) -> None:
        self._metric(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": dependency, "outcome": "failed"},
        )
        self._span(operation, started, "failure", "database_failure", dependency)

    def _metric(
        self,
        name: str,
        *,
        value: float = 1.0,
        labels: Mapping[str, str],
        replace: bool = False,
    ) -> None:
        if self.metrics is None:
            return
        try:
            if replace:
                self.metrics.set(name, value, labels)
            else:
                self.metrics.record(name, value, labels)
        except Exception:
            _LOGGER.debug("bounded orchestration metric unavailable")

    async def _mark_outbox_failed_after_cancellation(
        self, event_id: str, *, at: datetime, lease_token: str | None
    ) -> bool:
        """Complete an outbox failure transition before re-delivering cancellation."""

        transition = asyncio.create_task(
            self.repository.mark_outbox_failed(
                event_id,
                "transport_unavailable",
                at=at,
                lease_token=lease_token,
            )
        )
        while not transition.done():
            try:
                await asyncio.shield(transition)
            except asyncio.CancelledError:
                # A second cancellation must not strand the leased row.  The
                # repository task remains independently scheduled and is joined
                # on the next pass.
                continue
        try:
            transition.result()
        except Exception:
            # Preserve the provider cancellation.  The worker's bounded retry
            # loop will classify the repository failure without raw diagnostics.
            return False
        return True

    def _record_publish_failure_metrics(self) -> None:
        self._metric(
            "observatory_evaluation_outbox_publication_count",
            labels={"outcome": "failed"},
        )
        self._metric(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": "evaluation_nats", "outcome": "failed"},
        )

    def _span(
        self,
        operation: str,
        started: float,
        outcome: str,
        error_code: str | None = None,
        dependency: str | None = None,
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if callable(recorder):
            try:
                recorder(
                    component_id="aura.observatory.evaluation_orchestration",
                    component_version="1.0.0",
                    deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")),
                    operation=operation,
                    duration_ms=(monotonic() - started) * 1000.0,
                    outcome=outcome,
                    error_code=error_code,
                    dependency=dependency,
                )
            except Exception:
                _LOGGER.debug("bounded orchestration span unavailable")


class InMemoryEvaluationOrchestrationRepository:
    """Deterministic repository seam for tests and local composition."""

    def __init__(self) -> None:
        self.jobs: dict[str, EvaluationJob] = {}
        self.attempts: dict[str, EvaluationAttempt] = {}
        self.outbox: dict[str, EvaluationOutboxEntry] = {}
        self.receipts: dict[str, str] = {}
        self.audits: list[dict[str, str]] = []
        self.schedules: dict[str, EvaluationSchedule] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def admit(self, request: EvaluationAdmission, job_id: str) -> EvaluationJob:
        async with self._lock:
            fingerprint = request.fingerprint()
            prior = self._idempotency.get(request.idempotency_key)
            if prior is not None:
                if prior[1] != fingerprint:
                    raise OrchestrationError("idempotency_conflict")
                return self.jobs[prior[0]]
            now = request.created_at
            job = EvaluationJob(
                job_id,
                replace(request, request_fingerprint=fingerprint),
                created_at=now,
                updated_at=now,
            )
            self.jobs[job_id] = job
            self._idempotency[request.idempotency_key] = (job_id, fingerprint)
            attempt_id = _deterministic_attempt_id(job_id, 1)
            attempt = EvaluationAttempt(attempt_id, job_id, 1, fingerprint, next_attempt_at=now)
            self.attempts[attempt_id] = attempt
            envelope = _runner_envelope(job, attempt, now)
            event_id = str(envelope["messageId"])
            self.outbox[event_id] = EvaluationOutboxEntry(
                event_id,
                "evaluation.runner.requested",
                job_id,
                attempt_id,
                envelope,
            )
            return job

    async def get_job(self, job_id: str) -> EvaluationJob | None:
        return self.jobs.get(job_id)

    async def claim_due_attempts(
        self, *, now: datetime, limit: int, lease_seconds: int, worker_id: str
    ) -> Sequence[EvaluationAttempt]:
        del worker_id
        claimed: list[EvaluationAttempt] = []
        async with self._lock:
            for attempt_id, attempt in sorted(
                self.attempts.items(), key=lambda item: item[1].next_attempt_at
            ):
                if (
                    len(claimed) >= limit
                    or attempt.state is not AttemptState.QUEUED
                    or attempt.next_attempt_at > now
                ):
                    continue
                job = self.jobs.get(attempt.job_id)
                if job is None or job.state in TERMINAL_STATES or job.admission.deadline_at <= now:
                    continue
                token = str(uuid4())
                updated = replace(
                    attempt,
                    state=AttemptState.RUNNING,
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=now,
                )
                self.attempts[attempt_id] = updated
                self.jobs[job.job_id] = replace(job, state=JobState.RUNNING, updated_at=now)
                claimed.append(updated)
        return claimed

    async def claim_outbox(
        self,
        *,
        now: datetime,
        limit: int,
        lease_seconds: int = 120,
        worker_id: str = "in-memory",
    ) -> Sequence[EvaluationOutboxEntry]:
        del worker_id
        claimed: list[EvaluationOutboxEntry] = []
        async with self._lock:
            for event_id, entry in list(self.outbox.items()):
                if len(claimed) >= limit or entry.published_at is not None:
                    continue
                if entry.next_publish_at > now:
                    continue
                if entry.lease_expires_at is not None and entry.lease_expires_at > now:
                    continue
                job = self.jobs.get(entry.job_id)
                attempt = self.attempts.get(entry.attempt_id)
                if entry.event_type == "evaluation.runner.requested" and (
                    job is None
                    or attempt is None
                    or job.state in TERMINAL_STATES
                    or attempt.state in TERMINAL_STATES
                ):
                    self.outbox[event_id] = replace(
                        entry, published_at=now, last_error_code="ineligible"
                    )
                    continue
                if (
                    entry.event_type == "evaluation.runner.requested"
                    and attempt is not None
                    and attempt.next_attempt_at > now
                ):
                    continue
                leased = replace(
                    entry,
                    lease_token=str(uuid4()),
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                self.outbox[event_id] = leased
                claimed.append(leased)
        return claimed

    async def mark_outbox_published(
        self, event_id: str, *, at: datetime, lease_token: str | None = None
    ) -> bool:
        async with self._lock:
            entry = self.outbox.get(event_id)
            if (
                entry is None
                or entry.published_at is not None
                or (lease_token is not None and entry.lease_token != lease_token)
            ):
                return False
            self.outbox[event_id] = replace(
                entry, published_at=at, lease_token=None, lease_expires_at=None
            )
            return True

    async def mark_outbox_failed(
        self, event_id: str, code: str, *, at: datetime, lease_token: str | None = None
    ) -> None:
        async with self._lock:
            entry = self.outbox.get(event_id)
            if entry is None or (lease_token is not None and entry.lease_token != lease_token):
                return
            attempts = min(10, entry.publish_attempts + 1)
            exhausted = attempts >= 10
            delay = min(MAX_BACKOFF_SECONDS, 10 * (2 ** max(0, attempts - 1)))
            self.outbox[event_id] = replace(
                entry,
                publish_attempts=attempts,
                last_error_code="exhausted" if exhausted else code,
                published_at=at if exhausted else entry.published_at,
                lease_token=None,
                lease_expires_at=None,
                next_publish_at=at + timedelta(seconds=delay),
            )

    async def request_cancellation(
        self, job_id: str, *, reason: str, now: datetime
    ) -> EvaluationOutboxEntry | None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise OrchestrationError("job_not_found")
            if job.state in TERMINAL_STATES:
                return None
            self.jobs[job_id] = replace(job, state=JobState.CANCELLED, updated_at=now)
            for attempt_id, attempt in list(self.attempts.items()):
                if attempt.job_id == job_id and attempt.state not in {
                    AttemptState.SUCCEEDED,
                    AttemptState.FAILED,
                    AttemptState.CANCELLED,
                    AttemptState.DEADLINE,
                }:
                    self.attempts[attempt_id] = replace(
                        attempt,
                        state=AttemptState.CANCELLED,
                        completed_at=now,
                        terminal_reason=reason,
                    )
            attempt = next(a for a in self.attempts.values() if a.job_id == job_id)
            event_id = _deterministic_cancel_id(job_id, attempt.attempt_id)
            envelope = _cancellation_envelope(job, attempt, event_id, reason, now)
            entry = EvaluationOutboxEntry(
                event_id,
                "evaluation.runner.cancellation_requested",
                job_id,
                attempt.attempt_id,
                envelope,
            )
            if event_id not in self.outbox:
                self.outbox[event_id] = entry
                self.audits.append({"job_id": job_id, "event": "cancelled", "outcome": "accepted"})
                return entry
            return None

    async def apply_result(
        self, envelope: Mapping[str, object], *, received_at: datetime
    ) -> ResultApplication:
        job_id = str(envelope["jobId"])
        attempt_id = str(envelope["attemptId"])
        message_id = str(envelope["messageId"])
        if message_id in self.receipts:
            return ResultApplication("duplicate")
        job = self.jobs.get(job_id)
        attempt = self.attempts.get(attempt_id)
        if job is None or attempt is None or attempt.job_id != job_id:
            raise OrchestrationError("unknown_attempt")
        _validate_result_binding(job, attempt, envelope)
        settled_deadline = False
        if received_at >= job.admission.deadline_at and job.state not in TERMINAL_STATES:
            self.jobs[job_id] = replace(job, state=JobState.DEADLINE, updated_at=received_at)
            self.attempts[attempt_id] = replace(
                attempt,
                state=AttemptState.DEADLINE,
                completed_at=received_at,
                terminal_reason="DEADLINE_EXCEEDED",
            )
            self.audits.append({"job_id": job_id, "event": "deadline", "outcome": "accepted"})
            settled_deadline = True
            job = self.jobs[job_id]
            attempt = self.attempts[attempt_id]
        if (
            received_at >= job.admission.deadline_at
            or job.state in TERMINAL_STATES
            or attempt.state in TERMINAL_STATES
        ):
            self.receipts[message_id] = "out_of_order"
            return ResultApplication("out_of_order", "deadline" if settled_deadline else None)
        kind = str(envelope["messageType"])
        data = cast(Mapping[str, object], envelope["data"])
        result_id = str(data["resultId"])
        result_digest = str(data["resultDigest"])
        status = (
            "completed"
            if kind.endswith("completed")
            else "cancelled"
            if kind.endswith("cancelled")
            else "failed"
        )
        disposition = "applied"
        self.receipts[message_id] = disposition
        if status == "completed":
            new_attempt_state, new_job_state = AttemptState.SUCCEEDED, JobState.SUCCEEDED
        elif status == "cancelled":
            new_attempt_state, new_job_state = AttemptState.CANCELLED, JobState.CANCELLED
        else:
            retryable = bool(data.get("retryable", False))
            if (
                retryable
                and str(data.get("failureCode", "")) in RETRYABLE_FAILURES
                and attempt.attempt_number < job.admission.max_attempts
                and received_at < job.admission.deadline_at
            ):
                delay = min(
                    self._retry_delay(attempt.attempt_number),
                    max(1, int((job.admission.deadline_at - received_at).total_seconds())),
                )
                self.attempts[attempt_id] = replace(
                    attempt,
                    state=AttemptState.FAILED,
                    completed_at=received_at,
                    next_attempt_at=received_at + timedelta(seconds=delay),
                    failure_code=str(data.get("failureCode")),
                )
                next_number = attempt.attempt_number + 1
                next_attempt_id = _deterministic_attempt_id(job_id, next_number)
                next_attempt = EvaluationAttempt(
                    next_attempt_id,
                    job_id,
                    next_number,
                    job.admission.request_fingerprint or job.admission.fingerprint(),
                    next_attempt_at=received_at + timedelta(seconds=delay),
                )
                self.attempts[next_attempt_id] = next_attempt
                next_envelope = _runner_envelope(job, next_attempt, received_at)
                next_event_id = str(next_envelope["messageId"])
                self.outbox[next_event_id] = EvaluationOutboxEntry(
                    next_event_id,
                    "evaluation.runner.requested",
                    job_id,
                    next_attempt_id,
                    next_envelope,
                )
                self.jobs[job_id] = replace(
                    job,
                    state=JobState.QUEUED,
                    updated_at=received_at,
                    attempt_count=next_number,
                )
                self.audits.append(
                    {"job_id": job_id, "event": "retry_scheduled", "outcome": "accepted"}
                )
                return ResultApplication("applied")
            new_attempt_state, new_job_state = AttemptState.FAILED, JobState.FAILED
        self.attempts[attempt_id] = replace(
            attempt,
            state=new_attempt_state,
            completed_at=received_at,
            failure_code=str(data.get("failureCode")) if status == "failed" else None,
        )
        self.jobs[job_id] = replace(
            job, state=new_job_state, updated_at=received_at, attempt_count=attempt.attempt_number
        )
        self.audits.append({"job_id": job_id, "event": status, "outcome": "accepted"})
        del result_id, result_digest
        return ResultApplication(disposition, new_job_state.value)

    async def recover(self, *, now: datetime, limit: int, worker_id: str) -> int:
        del worker_id
        recovered = 0
        async with self._lock:
            for attempt_id, attempt in list(self.attempts.items()):
                if (
                    recovered >= limit
                    or attempt.state is not AttemptState.RUNNING
                    or attempt.lease_expires_at is None
                    or attempt.lease_expires_at > now
                ):
                    continue
                job = self.jobs.get(attempt.job_id)
                if job is None or job.state in TERMINAL_STATES:
                    continue
                self.attempts[attempt_id] = replace(
                    attempt,
                    state=AttemptState.QUEUED,
                    lease_token=None,
                    lease_expires_at=None,
                    next_attempt_at=now,
                )
                self.jobs[job.job_id] = replace(job, state=JobState.QUEUED, updated_at=now)
                recovered += 1
        return recovered

    async def settle_expired(self, *, now: datetime, limit: int) -> int:
        settled = 0
        async with self._lock:
            for job_id, job in list(self.jobs.items()):
                if (
                    settled >= limit
                    or job.state in TERMINAL_STATES
                    or job.admission.deadline_at > now
                ):
                    continue
                self.jobs[job_id] = replace(job, state=JobState.DEADLINE, updated_at=now)
                for attempt_id, attempt in list(self.attempts.items()):
                    if attempt.job_id == job_id and attempt.state not in TERMINAL_STATES:
                        self.attempts[attempt_id] = replace(
                            attempt,
                            state=AttemptState.DEADLINE,
                            completed_at=now,
                            terminal_reason="DEADLINE_EXCEEDED",
                        )
                self.audits.append({"job_id": job_id, "event": "deadline", "outcome": "accepted"})
                settled += 1
        return settled

    async def heartbeat(
        self, *, component_id: str, lease_token: str, at: datetime, backlog_count: int = 0
    ) -> Mapping[str, int]:
        del component_id, lease_token, at, backlog_count
        return {}

    async def materialize_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> int:
        if not lease_token:
            raise OrchestrationError("schedule_lease_invalid")
        materialized = 0
        async with self._lock:
            for schedule_id, schedule in sorted(
                self.schedules.items(), key=lambda item: (item[1].next_run_at, item[0])
            ):
                if materialized >= limit or not schedule.enabled or schedule.next_run_at > now:
                    continue
                admission = _scheduled_admission(schedule, schedule.next_run_at)
                job_id = _deterministic_job_id(admission.idempotency_key)
                if job_id not in self.jobs:
                    attempt_id = _deterministic_attempt_id(job_id, 1)
                    attempt = EvaluationAttempt(
                        attempt_id,
                        job_id,
                        1,
                        admission.fingerprint(),
                        next_attempt_at=schedule.next_run_at,
                    )
                    job = EvaluationJob(
                        job_id,
                        admission,
                        attempt_count=1,
                        created_at=schedule.next_run_at,
                        updated_at=now,
                    )
                    self.jobs[job_id] = job
                    self.attempts[attempt_id] = attempt
                    envelope = _runner_envelope(job, attempt, schedule.next_run_at)
                    event_id = str(envelope["messageId"])
                    self.outbox[event_id] = EvaluationOutboxEntry(
                        event_id,
                        "evaluation.runner.requested",
                        job_id,
                        attempt_id,
                        envelope,
                    )
                    self.audits.append(
                        {"job_id": job_id, "event": "schedule_materialized", "outcome": "accepted"}
                    )
                next_at = schedule.next_run_at + timedelta(seconds=schedule.cadence_seconds)
                self.schedules[schedule_id] = replace(
                    schedule,
                    occurrence_id=_deterministic_uuid(
                        "aura-observatory-schedule-occurrence",
                        f"{schedule.schedule_id}:{_timestamp(next_at)}",
                    ),
                    next_run_at=next_at,
                )
                materialized += 1
        return materialized

    @staticmethod
    def _retry_delay(attempt_number: int) -> int:
        return min(MAX_BACKOFF_SECONDS, 10 * (2 ** max(0, attempt_number - 1)))


def _runner_envelope(
    job: EvaluationJob, attempt: EvaluationAttempt, now: datetime
) -> dict[str, object]:
    data = job.admission.envelope.get("data")
    if not isinstance(data, Mapping):
        raise OrchestrationError("request_envelope_invalid")
    envelope = {
        "schemaVersion": 1,
        # The initial attempt has a deterministic event identity.  A retry of
        # the admission transaction therefore republishes the exact same
        # message and lets JetStream deduplicate it.
        "messageId": _deterministic_attempt_id(job.job_id, attempt.attempt_number),
        "messageType": "evaluation.runner.requested",
        "occurredAt": _timestamp(now),
        "correlationId": job.job_id,
        # Causation points at the concrete runner request message.  Core
        # echoes this lineage on terminal results; the workflow correlation
        # remains the stable job id.
        "causationId": attempt.attempt_id,
        "requestId": job.admission.request_id,
        "jobId": job.job_id,
        "attemptId": attempt.attempt_id,
        "idempotencyKey": job.admission.idempotency_key,
        "requestDigest": attempt.request_digest,
        "data": cast(Mapping[str, object], data),
    }
    return validate_transport_envelope(envelope)


def _scheduled_admission(
    schedule: EvaluationSchedule, occurrence_at: datetime
) -> EvaluationAdmission:
    """Instantiate an authorized template without inventing runner authority."""

    schedule.validate()
    assert schedule.request_envelope is not None
    raw_data = schedule.request_envelope["data"]
    if not isinstance(raw_data, Mapping):
        raise OrchestrationError("schedule_request_template_invalid")
    try:
        validated = validate_runner_data(
            "evaluation.runner.requested", cast(Mapping[str, object], raw_data)
        )
    except TransportValidationError as error:
        raise OrchestrationError(error.code) from None
    data = dict(validated)
    deadline_at = occurrence_at + timedelta(seconds=schedule.deadline_seconds)
    data["deadlineAt"] = _timestamp(deadline_at)
    occurrence_key = f"{schedule.schedule_id}:{_timestamp(occurrence_at)}"
    request_id = _deterministic_uuid("aura-observatory-schedule-request", occurrence_key)
    idempotency_key = schedule.occurrence_id
    capture = _mapping(data["captureAuthorization"], "captureAuthorization")
    return EvaluationAdmission(
        request_id,
        idempotency_key,
        str(data["requestVersion"]),
        schedule.dataset_id,
        schedule.dataset_version,
        schedule.suite_id,
        schedule.suite_version,
        schedule.evaluator_id,
        schedule.evaluator_version,
        str(capture["captureClassification"]),
        deadline_at,
        schedule.max_attempts,
        {"data": data},
        created_at=occurrence_at,
    )


def _cancellation_envelope(
    job: EvaluationJob, attempt: EvaluationAttempt, event_id: str, reason: str, now: datetime
) -> dict[str, object]:
    envelope = {
        "schemaVersion": 1,
        "messageId": event_id,
        "messageType": "evaluation.runner.cancellation_requested",
        "occurredAt": _timestamp(now),
        "correlationId": job.job_id,
        "causationId": attempt.attempt_id,
        "requestId": job.admission.request_id,
        "jobId": job.job_id,
        "attemptId": attempt.attempt_id,
        "idempotencyKey": job.admission.idempotency_key,
        "requestDigest": attempt.request_digest,
        "data": {
            "cancellationId": event_id,
            "reasonCode": reason,
            "deadlineAt": _timestamp(job.admission.deadline_at),
        },
    }
    return validate_transport_envelope(envelope)


def _reject_untrusted_keys(value: object) -> None:
    if isinstance(value, Mapping):
        typed = cast(Mapping[object, object], value)
        for key, child in typed.items():
            if (
                not isinstance(key, str)
                or len(key) > 64
                or key.casefold().replace("-", "_")
                in {
                    "prompt",
                    "response",
                    "content",
                    "filename",
                    "arguments",
                    "tool_arguments",
                    "tool_results",
                    "credentials",
                    "endpoint",
                    "bearer_token",
                    "password",
                    "owner_identity",
                    "object_key",
                    "url",
                    "uri",
                }
            ):
                raise OrchestrationError("privacy_field")
            _reject_untrusted_keys(child)
    elif isinstance(value, list):
        typed_list = cast(list[object], value)
        if len(typed_list) > 1000:
            raise OrchestrationError("limit_exceeded")
        for item in typed_list:
            _reject_untrusted_keys(item)
    elif isinstance(value, str):
        if len(value) > 4096:
            raise OrchestrationError("limit_exceeded")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise OrchestrationError("unsupported_field")


def _validate_result_binding(
    job: EvaluationJob, attempt: EvaluationAttempt, envelope: Mapping[str, object]
) -> None:
    """Require terminal results to preserve every stored request binding."""

    if (
        envelope.get("requestId") != job.admission.request_id
        or envelope.get("jobId") != job.job_id
        or envelope.get("attemptId") != attempt.attempt_id
        or envelope.get("idempotencyKey") != job.admission.idempotency_key
        or envelope.get("correlationId") != job.job_id
        or envelope.get("causationId") != attempt.attempt_id
    ):
        raise OrchestrationError("lineage_mismatch")
    raw_request_data = job.admission.envelope.get("data")
    raw_result_data = envelope.get("data")
    if not isinstance(raw_request_data, Mapping) or not isinstance(raw_result_data, Mapping):
        raise OrchestrationError("provenance_mismatch")
    request_data = cast(Mapping[str, object], raw_request_data)
    result_data = cast(Mapping[str, object], raw_result_data)
    for result_key, request_key in (
        ("captureAuthorization", "captureAuthorization"),
        ("provenance", "provenance"),
        ("deadlineAt", "deadlineAt"),
    ):
        if result_data.get(result_key) != request_data.get(request_key):
            raise OrchestrationError("provenance_mismatch")
    if not _effective_limits_are_compatible(
        request_data.get("limits"), result_data.get("effectiveLimits")
    ):
        raise OrchestrationError("limits_mismatch")
    if attempt.request_digest != str(envelope.get("requestDigest")):
        raise OrchestrationError("provenance_mismatch")


def _require_uuid(value: str, name: str) -> None:
    try:
        UUID(value)
    except ValueError, AttributeError, TypeError:
        raise OrchestrationError(f"{name}_invalid") from None


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OrchestrationError(f"{name}_invalid")
    return cast(Mapping[str, object], value)


def _require_reference(
    container: Mapping[str, object], name: str, identifier: str, version: str
) -> None:
    reference = _mapping(container.get(name), name)
    if reference.get("id") != identifier or reference.get("version") != version:
        raise OrchestrationError(f"{name}_binding_mismatch")


def _has_evaluator(value: object, evaluator_id: str, evaluator_version: str) -> bool:
    if not isinstance(value, list):
        return False
    items = cast(list[object], value)
    return any(
        isinstance(item, Mapping)
        and cast(Mapping[str, object], item).get("evaluatorId") == evaluator_id
        and cast(Mapping[str, object], item).get("evaluatorVersion") == evaluator_version
        for item in items
    )


def _require_equal_binding(expected: object, actual: object, name: str) -> None:
    if expected != actual:
        raise OrchestrationError(f"{name}_binding_mismatch")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OrchestrationError("timestamp_invalid") from None
    if parsed.tzinfo is None:
        raise OrchestrationError("timestamp_invalid")
    return parsed.astimezone(UTC)


def _effective_limits_are_compatible(requested: object, effective: object) -> bool:
    """Accept deployment clamping while rejecting widening or shape changes."""

    if not isinstance(requested, Mapping) or not isinstance(effective, Mapping):
        return False
    requested_map = cast(Mapping[str, object], requested)
    effective_map = cast(Mapping[str, object], effective)
    if set(requested_map) != set(effective_map) or set(requested_map) != {
        "execution",
        "maxQueueTimeMs",
        "maxModelCalls",
        "maxArtifactOperations",
    }:
        return False
    requested_execution = requested_map.get("execution")
    effective_execution = effective_map.get("execution")
    if not isinstance(requested_execution, Mapping) or not isinstance(effective_execution, Mapping):
        return False
    requested_exec = cast(Mapping[str, object], requested_execution)
    effective_exec = cast(Mapping[str, object], effective_execution)
    if set(requested_exec) != set(effective_exec) or set(requested_exec) != {
        "timeoutMs",
        "maxConcurrency",
        "maxAttempts",
        "maxCases",
        "maxInputBytes",
        "maxOutputBytes",
        "maxTokens",
    }:
        return False
    values = [
        *(str(key) for key in requested_exec),
        "maxQueueTimeMs",
        "maxModelCalls",
        "maxArtifactOperations",
    ]
    for key in values:
        effective_value = effective_map.get(key, effective_exec.get(key))
        requested_value = requested_map.get(key, requested_exec.get(key))
        if type(effective_value) is not int or type(requested_value) is not int:
            return False
        if effective_value > requested_value:
            return False
    return True


def _require_identifier(value: str, name: str) -> None:
    if (
        not value
        or len(value) > SAFE_IDENTIFIER_LENGTH
        or not value[0].isalnum()
        or any(not (char.isalnum() or char in "._+-") for char in value)
    ):
        raise OrchestrationError(f"{name}_invalid")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OrchestrationError(f"{name}_invalid")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _deterministic_job_id(idempotency_key: str) -> str:
    return str(
        UUID(bytes=hashlib.sha256(f"aura-observatory-job:{idempotency_key}".encode()).digest()[:16])
    )


def _deterministic_attempt_id(job_id: str, number: int) -> str:
    return str(
        UUID(
            bytes=hashlib.sha256(f"aura-observatory-attempt:{job_id}:{number}".encode()).digest()[
                :16
            ]
        )
    )


def _deterministic_cancel_id(job_id: str, attempt_id: str) -> str:
    return str(
        UUID(
            bytes=hashlib.sha256(
                f"aura-observatory-cancel:{job_id}:{attempt_id}".encode()
            ).digest()[:16]
        )
    )


def _deterministic_uuid(namespace: str, value: str) -> str:
    return str(UUID(bytes=hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:16]))


def _bounded_worker_id(value: str) -> str:
    _require_identifier(value, "worker_id")
    return value


def _lease_token_for_worker(worker_id: str) -> str:
    """Derive the UUID-shaped database lease identity from a bounded worker id."""

    return str(
        UUID(bytes=hashlib.sha256(f"aura-observatory-lease:{worker_id}".encode()).digest()[:16])
    )


__all__ = [
    "AttemptState",
    "EvaluationAdmission",
    "EvaluationAttempt",
    "EvaluationJob",
    "EvaluationOrchestrationRepository",
    "EvaluationOrchestrationService",
    "EvaluationSchedulerService",
    "EvaluationOutboxEntry",
    "EvaluationSchedule",
    "InMemoryEvaluationOrchestrationRepository",
    "JobState",
    "OrchestrationError",
    "_scheduled_admission",
]
