"""Resource-bounded controlled evaluation runner application service."""

# Transport payloads are intentionally parsed from untyped JSON at the closed
# contract boundary; validation is centralized in platform.evaluation_transport.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportIndexIssue=false, reportGeneralTypeIssues=false, reportReturnType=false

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from aura_core.platform.evaluation_transport import (
    FailureCode,
    RunnerMessage,
    TransportValidationError,
    deterministic_message_id,
    deterministic_terminal_id,
    validate_message,
)
from aura_core.runtime.replay.registry import (
    MissingRuntimeEvidence,
    ReplayContext,
    ReplayError,
    ReplayRegistry,
    ReplayResult,
    RuntimeEvidenceProvider,
)
from aura_core.runtime.replay.state import (
    AttemptConflict,
    AttemptRecord,
    EvaluationRunnerLedger,
)


class RunnerMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...

    def record_span(
        self, component: str, operation: str, duration_ms: float, **attributes: str
    ) -> None: ...

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...


class NullRunnerMetrics:
    def increment(self, component: str, name: str, **attributes: str) -> None:
        del component, name, attributes

    def record_span(
        self, component: str, operation: str, duration_ms: float, **attributes: str
    ) -> None:
        del component, operation, duration_ms, attributes

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
        del component, name, value, attributes


class ResultPublisher(Protocol):
    async def publish(self, payload: Mapping[str, object]) -> None: ...


class RunnerArtifactPort(Protocol):
    async def read(
        self, reference: dict[str, object], *, max_bytes: int | None = None
    ) -> bytes: ...

    async def write(self, reference: dict[str, object], content: bytes) -> object: ...

    async def delete(self, reference: dict[str, object]) -> None: ...


class _ConcurrencyGate:
    """A bounded gate whose per-request ceiling is part of the request contract."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._active: list[int] = []
        self._condition = asyncio.Condition()

    async def acquire(self, requested: int) -> int:
        ceiling = min(self._maximum, requested)
        async with self._condition:
            while self._active and len(self._active) + 1 > min(*self._active, ceiling):
                await self._condition.wait()
            self._active.append(ceiling)
            return ceiling

    async def release(self, ceiling: int) -> None:
        async with self._condition:
            try:
                self._active.remove(ceiling)
            except ValueError:
                return
            self._condition.notify_all()


@dataclass(slots=True)
class _ArtifactBudget:
    maximum: int
    used: int = 0

    def consume(self, count: int = 1) -> None:
        if count < 0 or self.used + count > self.maximum:
            raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
        self.used += count


class NullResultPublisher:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def publish(self, payload: Mapping[str, object]) -> None:
        self.messages.append(dict(payload))


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    worker_id: str = "evaluation-runner"
    max_concurrency: int = 2
    max_queue_depth: int = 32
    max_timeout_ms: int = 300_000
    max_cases: int = 100
    max_input_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_tokens: int = 1_000_000
    max_model_calls: int = 100
    max_artifact_operations: int = 100
    lease_seconds: int = 60
    max_queue_time_ms: int = 3_600_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 64 or not 1 <= self.max_queue_depth <= 4096:
            raise ValueError("runner capacity is invalid")
        if not 100 <= self.max_timeout_ms <= 86_400_000:
            raise ValueError("runner timeout is invalid")
        if not 1 <= self.max_cases <= 100_000:
            raise ValueError("runner case limit is invalid")
        if not 100 <= self.max_queue_time_ms <= 3_600_000:
            raise ValueError("runner queue time is invalid")


@dataclass(frozen=True, slots=True)
class EffectiveLimits:
    timeout_ms: int
    max_queue_time_ms: int
    max_concurrency: int
    max_attempts: int
    max_cases: int
    max_input_bytes: int
    max_output_bytes: int
    max_tokens: int
    max_model_calls: int
    max_artifact_operations: int

    def payload(self) -> dict[str, object]:
        return {
            "execution": {
                "timeoutMs": self.timeout_ms,
                "maxConcurrency": self.max_concurrency,
                "maxAttempts": self.max_attempts,
                "maxCases": self.max_cases,
                "maxInputBytes": self.max_input_bytes,
                "maxOutputBytes": self.max_output_bytes,
                "maxTokens": self.max_tokens,
            },
            "maxQueueTimeMs": self.max_queue_time_ms,
            "maxModelCalls": self.max_model_calls,
            "maxArtifactOperations": self.max_artifact_operations,
        }


def _effective_limits(declared: Mapping[str, object], config: RunnerConfig) -> EffectiveLimits:
    execution = declared["execution"]
    if not isinstance(execution, dict):
        raise TransportValidationError("execution limits are invalid")
    return EffectiveLimits(
        timeout_ms=min(int(execution["timeoutMs"]), config.max_timeout_ms),
        max_queue_time_ms=min(int(declared["maxQueueTimeMs"]), config.max_queue_time_ms),
        max_concurrency=min(int(execution["maxConcurrency"]), config.max_concurrency),
        max_attempts=int(execution["maxAttempts"]),
        max_cases=min(int(execution["maxCases"]), config.max_cases),
        max_input_bytes=min(int(execution["maxInputBytes"]), config.max_input_bytes),
        max_output_bytes=min(int(execution["maxOutputBytes"]), config.max_output_bytes),
        max_tokens=min(int(execution["maxTokens"]), config.max_tokens),
        max_model_calls=min(int(declared["maxModelCalls"]), config.max_model_calls),
        max_artifact_operations=min(
            int(declared["maxArtifactOperations"]), config.max_artifact_operations
        ),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_exception_class(error: BaseException) -> str:
    """Map provider/ledger failures to a finite metadata-only dimension."""

    names = {type(error).__name__.lower()}
    original = getattr(error, "orig", None)
    if isinstance(original, BaseException):
        names.add(type(original).__name__.lower())
    if any("timeout" in name for name in names):
        return "timeout"
    if any(
        marker in name
        for name in names
        for marker in ("insufficientprivilege", "permissiondenied", "authorization")
    ):
        return "authorization"
    if any(
        marker in name
        for name in names
        for marker in ("programming", "ambiguousparameter", "undefined", "syntax", "datatype")
    ):
        return "database_programming"
    if any(
        marker in name
        for name in names
        for marker in ("connection", "operational", "integrity", "database")
    ):
        return "database"
    return "execution"


class EvaluationRunner:
    """Consume validated commands and publish one deterministic terminal result."""

    component = "aura.evaluation.runner"

    def __init__(
        self,
        ledger: EvaluationRunnerLedger,
        registry: ReplayRegistry,
        publisher: ResultPublisher | None = None,
        *,
        config: RunnerConfig | None = None,
        metrics: RunnerMetrics | None = None,
        artifact_adapter: RunnerArtifactPort | None = None,
        runtime_evidence: RuntimeEvidenceProvider | None = None,
    ) -> None:
        self.ledger = ledger
        self.registry = registry
        self.publisher = publisher or NullResultPublisher()
        self.config = config or RunnerConfig()
        self.metrics = metrics or NullRunnerMetrics()
        self.artifact_adapter = artifact_adapter
        self.runtime_evidence = runtime_evidence or MissingRuntimeEvidence()
        self._queue: asyncio.Queue[RunnerMessage] = asyncio.Queue(
            maxsize=self.config.max_queue_depth
        )
        self._cancel_events: dict[UUID, asyncio.Event] = {}
        self._request_messages: dict[UUID, RunnerMessage] = {}
        self._enqueued_at: dict[UUID, datetime] = {}
        self._queued_attempts: set[UUID] = set()
        self._accounted_terminals: dict[UUID, str] = {}
        self._execution_gate = _ConcurrencyGate(self.config.max_concurrency)
        # Terminal settlement and publication are one process-local critical
        # section.  This closes the active-cancellation race where the
        # cancellation command and the replay task both observe the same
        # cancelled attempt and would otherwise publish it twice.
        self._publication_lock = asyncio.Lock()

    async def accept(self, payload: Mapping[str, object]) -> AttemptRecord | None:
        """Validate and durably receipt a request or cancellation.

        Poison messages are rejected before a receipt is created.  A duplicate
        request is safe to acknowledge because the ledger returns its original
        record and the terminal publisher is deterministic.
        """

        message = validate_message(payload)
        if message.message_type == "evaluation.runner.cancellation_requested":
            return await self.cancel(message)
        if message.message_type != "evaluation.runner.requested":
            raise TransportValidationError("runner accepts requests and cancellations only")
        data = message.payload["data"]
        if not isinstance(data, dict):
            raise TransportValidationError("request data is invalid")
        limits = _effective_limits(data["limits"], self.config)
        cases = data["caseIds"]
        if not isinstance(cases, list) or len(cases) > limits.max_cases:
            return await self._fail_before_queue(message, "RESOURCE_LIMIT_EXCEEDED")
        deadline = datetime.fromisoformat(str(data["deadlineAt"]).replace("Z", "+00:00"))
        if deadline <= _now():
            return await self._fail_before_queue(message, "DEADLINE_EXCEEDED")
        record = await self.ledger.receive(
            AttemptRecord(
                attempt_id=message.attempt_id,
                request_id=message.request_id,
                job_id=message.job_id,
                idempotency_key=message.idempotency_key,
                request_digest=message.request_digest,
                request_projection=message.payload,
            )
        )
        if record.request_projection is not None and record.request_projection != message.payload:
            # A reused idempotency/attempt identity may not smuggle a changed
            # case set, artifact reference, or execution declaration behind a
            # copied digest.  The durable projection is the comparison source.
            raise AttemptConflict("request projection conflict")
        self._request_messages[message.attempt_id] = message
        if record.status in {"completed", "cancelled", "failed"}:
            if record.status == "cancelled" and record.terminal_payload is None:
                await self._publish_cancelled(
                    message,
                    record,
                    record.cancellation_reason or "OWNER_REQUESTED",
                )
            else:
                await self._republish_terminal(record, message)
            return record
        if message.attempt_id not in self._queued_attempts:
            try:
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                return await self._fail_before_queue(message, "RESOURCE_LIMIT_EXCEEDED")
            self._queued_attempts.add(message.attempt_id)
        self._cancel_events.setdefault(message.attempt_id, asyncio.Event())
        self._enqueued_at[message.attempt_id] = _now()
        self.metrics.increment(
            self.component, "evaluation_runner_queue_accepted", outcome="accepted"
        )
        return record

    async def cancel(self, message: RunnerMessage) -> AttemptRecord | None:
        data = message.payload["data"]
        if not isinstance(data, dict):
            raise TransportValidationError("cancellation data is invalid")
        cancellation_id = UUID(str(data["cancellationId"]))
        reason = str(data["reasonCode"])
        deadline = datetime.fromisoformat(
            str(data["deadlineAt"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        if deadline <= _now():
            raise AttemptConflict("expired cancellation")
        record = await self.ledger.cancel(
            message.attempt_id,
            cancellation_id,
            reason,
            request_id=message.request_id,
            job_id=message.job_id,
            idempotency_key=message.idempotency_key,
            request_digest=message.request_digest,
            deadline_at=deadline,
        )
        if record is None:
            # The durable ledger keeps a bounded cancellation tombstone for a
            # request that has not arrived yet; no process-local map entry is
            # needed and retaining it would grow with unknown redeliveries.
            self._cancel_events.pop(message.attempt_id, None)
            self._request_messages.pop(message.attempt_id, None)
            return None
        if record.status == "cancelled":
            self._cancel_events.setdefault(message.attempt_id, asyncio.Event()).set()
            request_message = self._request_messages.get(message.attempt_id)
            if request_message is None and record.request_projection is not None:
                request_message = validate_message(record.request_projection)
            if request_message is not None:
                message = replace(message, payload=request_message.payload)
            await self._publish_cancelled(message, record, reason)
        return record

    async def run_once(self) -> AttemptRecord | None:
        """Run one queued command; the caller owns process lifecycle."""

        try:
            message = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        try:
            self._queued_attempts.discard(message.attempt_id)
            data = message.payload.get("data")
            limits = (
                _effective_limits(data["limits"], self.config)
                if isinstance(data, dict)
                else None
            )
            gate_ceiling = await self._execution_gate.acquire(
                limits.max_concurrency if limits else 1
            )
            try:
                enqueued = self._enqueued_at.get(message.attempt_id, _now())
                if limits is not None:
                    queue_duration_ms = max(0.0, (_now() - enqueued).total_seconds() * 1000)
                    self._observe(
                        "evaluation_runner_queue_time_ms",
                        queue_duration_ms,
                        outcome="expired"
                        if queue_duration_ms > limits.max_queue_time_ms
                        else "accepted",
                        dependency="application_validation",
                    )
                    if queue_duration_ms > limits.max_queue_time_ms:
                        record = await self.ledger.get(message.attempt_id)
                        if record is not None:
                            return await self._publish_cancelled(
                                message, record, "DEADLINE_EXCEEDED", started=enqueued
                            )
                return await self._execute(message)
            finally:
                await self._execution_gate.release(gate_ceiling)
        finally:
            self._queue.task_done()

    async def serve(self, stop: asyncio.Event) -> None:
        await self._recover_attempts()
        await self._drain_publications()
        running: set[asyncio.Task[AttemptRecord | None]] = set()
        while not stop.is_set() or running:
            while not stop.is_set() and len(running) < self.config.max_concurrency:
                if self._queue.empty():
                    break
                running.add(asyncio.create_task(self.run_once()))
            if not running:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    await self._recover_attempts()
                    await self._drain_publications()
                continue
            done, running = await asyncio.wait(running, timeout=0.25)
            for task in done:
                try:
                    task.result()
                except Exception:
                    continue
            await self._recover_attempts()
            await self._drain_publications()

    async def _recover_attempts(self) -> None:
        """Re-enqueue durable receipts after ACK, restart, or lease expiry."""

        try:
            records = await self.ledger.recoverable_attempts()
        except Exception as exc:
            self._observe(
                "evaluation_runner_recovery_scan_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
                error_class=_bounded_exception_class(exc),
            )
            return
        recovered = 0
        for record in records:
            if record.attempt_id in self._queued_attempts:
                continue
            projection = record.request_projection
            if projection is None:
                continue
            try:
                message = validate_message(projection)
                data = message.payload["data"]
                if not isinstance(data, dict):
                    continue
                deadline = datetime.fromisoformat(str(data["deadlineAt"]).replace("Z", "+00:00"))
                if deadline <= _now():
                    await self._publish_cancelled(message, record, "DEADLINE_EXCEEDED")
                    continue
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                return
            except (TransportValidationError, AttemptConflict):
                continue
            self._queued_attempts.add(record.attempt_id)
            self._request_messages[record.attempt_id] = message
            self._cancel_events.setdefault(record.attempt_id, asyncio.Event())
            self._enqueued_at.setdefault(record.attempt_id, record.updated_at or _now())
            recovered += 1
        if recovered:
            self._observe(
                "evaluation_runner_recovery_count",
                float(recovered),
                outcome="recovered",
                dependency="postgresql",
            )

    async def _drain_publications(self) -> None:
        try:
            pending = await self.ledger.pending_publications()
        except Exception as exc:
            # A failed pending-publication query is a ledger dependency
            # failure, not a broker failure. It is retried on the next bounded
            # drain cycle without exposing provider diagnostics.
            self._observe(
                "evaluation_runner_publication_recovery_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
                error_class=_bounded_exception_class(exc),
            )
            return
        for record in pending:
            await self._republish_terminal(record, None)  # type: ignore[arg-type]

    async def _execute(self, message: RunnerMessage) -> AttemptRecord:
        data = message.payload["data"]
        assert isinstance(data, dict)
        limits = _effective_limits(data["limits"], self.config)
        claimed = await self.ledger.claim(
            message.attempt_id,
            self.config.worker_id,
            timedelta(seconds=self.config.lease_seconds),
            max_attempts=limits.max_attempts,
        )
        if claimed is None:
            record = await self.ledger.get(message.attempt_id)
            if record is None:
                raise AttemptConflict("attempt disappeared before claim")
            if record.status in {"completed", "cancelled", "failed"}:
                await self._republish_terminal(record, message)
            elif datetime.fromisoformat(str(data["deadlineAt"]).replace("Z", "+00:00")) <= _now():
                return await self._publish_cancelled(message, record, "DEADLINE_EXCEEDED")
            elif record.attempt_count >= limits.max_attempts:
                return await self._publish_failed(
                    message, record, "RESOURCE_LIMIT_EXCEEDED"
                )
            return record
        started = _now()
        deadline = datetime.fromisoformat(str(data["deadlineAt"]).replace("Z", "+00:00"))
        execution_deadline = min(deadline, started + timedelta(milliseconds=limits.timeout_ms))
        cancel_event = self._cancel_events.setdefault(message.attempt_id, asyncio.Event())
        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease(claimed, cancel_event, lease_lost))
        results: list[ReplayResult] = []
        try:
            self._assert_runtime_evidence(message)
            if len(data["caseIds"]) > limits.max_cases:
                raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
            input_artifacts: dict[UUID, bytes] = {}
            references = data.get("inputArtifactReferences", [])
            if not isinstance(references, list):
                raise _RunnerFailure("REQUEST_INVALID")
            input_bytes = 0
            artifact_budget = _ArtifactBudget(limits.max_artifact_operations)
            fixture_started = _now()
            fixture_outcome = "completed"
            fixture_dependency = "object_storage" if references else "application_validation"
            try:
                if references:
                    if self.artifact_adapter is None:
                        raise _RunnerFailure("DEPENDENCY_UNAVAILABLE")
                    request_capture = data["captureAuthorization"]
                    if not isinstance(request_capture, dict):
                        raise _RunnerFailure("REQUEST_INVALID")
                    for raw_reference in references:
                        if not isinstance(raw_reference, dict):
                            raise _RunnerFailure("REQUEST_INVALID")
                        reference_capture = raw_reference.get("captureAuthorization")
                        if not self._capture_compatible(request_capture, reference_capture):
                            raise _RunnerFailure("REQUEST_INVALID")
                        artifact_budget.consume()
                        declared_bytes = raw_reference.get("byteCount")
                        remaining_bytes = limits.max_input_bytes - input_bytes
                        if not isinstance(declared_bytes, int) or declared_bytes > remaining_bytes:
                            raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
                        try:
                            if cancel_event.is_set():
                                raise _RunnerCancelled("OWNER_REQUESTED")
                            remaining = (execution_deadline - _now()).total_seconds()
                            if remaining <= 0:
                                raise TimeoutError
                            content = cast(
                                bytes,
                                await self._run_guarded(
                                    self.artifact_adapter.read(
                                        raw_reference, max_bytes=remaining_bytes
                                    ),
                                    cancel_event=cancel_event,
                                    lease_lost=lease_lost,
                                    timeout=remaining,
                                ),
                            )
                        except TimeoutError:
                            raise
                        except (_RunnerCancelled, _RunnerFailure):
                            raise
                        except Exception:
                            raise _RunnerFailure(
                                "STORAGE_FAILED", dependency="object_storage"
                            ) from None
                        input_bytes += len(content)
                        if input_bytes > limits.max_input_bytes:
                            raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
                        input_artifacts[UUID(str(raw_reference["artifactReferenceId"]))] = content
            except (_RunnerCancelled, TimeoutError) as exc:
                fixture_outcome = "canceled" if isinstance(exc, _RunnerCancelled) else "timeout"
                fixture_duration_ms = max(0.0, (_now() - fixture_started).total_seconds() * 1000)
                self._record_span(
                    "evaluation.fixture.load",
                    fixture_duration_ms,
                    message,
                    dependency=fixture_dependency,
                    outcome=fixture_outcome,
                    error_class="cancellation" if isinstance(exc, _RunnerCancelled) else "timeout",
                    parent_span_id=self._span_id(message, "evaluation.execute"),
                )
                self._observe(
                    "evaluation_runner_fixture_load_duration_ms",
                    fixture_duration_ms,
                    outcome=fixture_outcome,
                    dependency=fixture_dependency,
                )
                raise
            except _RunnerFailure as exc:
                fixture_outcome = "failed"
                self._record_span(
                    "evaluation.fixture.load",
                    max(0.0, (_now() - fixture_started).total_seconds() * 1000),
                    message,
                    dependency=fixture_dependency,
                    outcome=fixture_outcome,
                    error_class=self._error_class_for_code(exc.code),
                    parent_span_id=self._span_id(message, "evaluation.execute"),
                )
                self._observe(
                    "evaluation_runner_fixture_load_duration_ms",
                    max(0.0, (_now() - fixture_started).total_seconds() * 1000),
                    outcome=fixture_outcome,
                    dependency=fixture_dependency,
                )
                raise
            fixture_duration_ms = max(0.0, (_now() - fixture_started).total_seconds() * 1000)
            self._record_span(
                "evaluation.fixture.load",
                fixture_duration_ms,
                message,
                dependency=fixture_dependency,
                outcome=fixture_outcome,
                parent_span_id=self._span_id(message, "evaluation.execute"),
            )
            self._observe(
                "evaluation_runner_fixture_load_duration_ms",
                fixture_duration_ms,
                outcome=fixture_outcome,
                dependency=fixture_dependency,
            )
            target = data["targetComponent"]
            assert isinstance(target, dict)
            total_tokens = 0
            total_model_calls = 0
            for raw_case in data["caseIds"]:
                if cancel_event.is_set():
                    raise _RunnerCancelled("OWNER_REQUESTED")
                context = ReplayContext(
                    message.attempt_id,
                    UUID(str(raw_case)),
                    execution_deadline,
                    cancel_event,
                    input_artifacts=input_artifacts,
                    max_input_bytes=limits.max_input_bytes,
                    max_output_bytes=limits.max_output_bytes,
                    max_tokens=limits.max_tokens,
                    max_model_calls=limits.max_model_calls,
                    max_artifact_operations=limits.max_artifact_operations,
                    artifact_budget=artifact_budget,
                    capture_authorization=cast(
                        Mapping[str, object], data["captureAuthorization"]
                    ),
                )
                try:
                    result = cast(
                        ReplayResult,
                        await self._run_guarded(
                            self.registry.execute(
                                execution_kind=str(data["executionKind"]),
                                component_id=str(target["componentId"]),
                                component_version=str(target["componentVersion"]),
                                case_id=context.case_id,
                                context=context,
                            ),
                            cancel_event=cancel_event,
                            lease_lost=lease_lost,
                            timeout=max(0.001, (execution_deadline - _now()).total_seconds()),
                        ),
                    )
                except (_RunnerCancelled, _RunnerFailure, TimeoutError, ReplayError):
                    raise
                except Exception:
                    raise _RunnerFailure(
                        "DEPENDENCY_UNAVAILABLE", dependency="model_provider"
                    ) from None
                context.consume(
                    tokens=result.tokens_used,
                    model_calls=result.model_calls,
                    artifact_operations=result.artifact_operations,
                )
                total_tokens += result.tokens_used
                total_model_calls += result.model_calls
                if (
                    total_tokens > limits.max_tokens
                    or total_model_calls > limits.max_model_calls
                ):
                    raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
                results.append(result)
            return await self._publish_completed(
                message,
                claimed,
                started,
                limits,
                results,
                execution_deadline=execution_deadline,
                cancel_event=cancel_event,
                lease_lost=lease_lost,
                artifact_budget=artifact_budget,
            )
        except _RunnerCancelled as exc:
            return await self._publish_cancelled(message, claimed, exc.reason, started=started)
        except TimeoutError:
            return await self._publish_cancelled(
                message, claimed, "DEADLINE_EXCEEDED", started=started
            )
        except ReplayError as exc:
            reason = "OWNER_REQUESTED" if cancel_event.is_set() else None
            if reason is not None or str(exc) == "deadline":
                return await self._publish_cancelled(
                    message,
                    claimed,
                    reason or "DEADLINE_EXCEEDED",
                    started=started,
                )
            if str(exc) == "resource limit":
                return await self._publish_failed(
                    message, claimed, "RESOURCE_LIMIT_EXCEEDED", started=started
                )
            return await self._publish_failed(
                message,
                claimed,
                "EXECUTION_FAILED",
                started=started,
                dependency=exc.dependency,
            )
        except _RunnerFailure as exc:
            return await self._publish_failed(
                message,
                claimed,
                exc.code,
                started=started,
                dependency=exc.dependency,
            )
        except Exception:
            return await self._publish_failed(
                message, claimed, "INTERNAL_FAILURE", started=started
            )
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)

    async def _renew_lease(
        self,
        record: AttemptRecord,
        cancel_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        if record.lease_generation is None:
            return
        interval = max(0.05, self.config.lease_seconds / 3)
        while not cancel_event.is_set():
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=interval)
            except TimeoutError:
                try:
                    renewed = await self.ledger.renew(
                        record.attempt_id,
                        self.config.worker_id,
                        record.lease_generation,
                        timedelta(seconds=self.config.lease_seconds),
                    )
                except Exception as exc:
                    lease_lost.set()
                    self._observe(
                        "evaluation_runner_lease_renewal_failure_count",
                        1.0,
                        outcome="error",
                        dependency="postgresql",
                        error_class=_bounded_exception_class(exc),
                    )
                    return
                if renewed is None:
                    lease_lost.set()
                    self._observe(
                        "evaluation_runner_lease_lost_count",
                        1.0,
                        outcome="fenced",
                        dependency="postgresql",
                    )
                    return
            else:
                return

    async def _run_guarded(
        self,
        operation: Awaitable[Any],
        *,
        cancel_event: asyncio.Event,
        lease_lost: asyncio.Event,
        timeout: float,
    ) -> Any:
        """Run provider work with cancellation, fencing, and deadline races."""

        task = asyncio.ensure_future(operation)
        cancelled = asyncio.create_task(cancel_event.wait())
        fenced = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancelled, fenced},
                timeout=max(0.001, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return await task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if lease_lost.is_set():
                raise _RunnerFailure("DEPENDENCY_UNAVAILABLE", dependency="postgresql")
            if cancel_event.is_set():
                raise _RunnerCancelled("OWNER_REQUESTED")
            raise TimeoutError
        finally:
            cancelled.cancel()
            fenced.cancel()
            await asyncio.gather(cancelled, fenced, return_exceptions=True)

    async def _fail_before_queue(self, message: RunnerMessage, code: FailureCode) -> AttemptRecord:
        record = await self.ledger.receive(
            AttemptRecord(
                message.attempt_id,
                message.request_id,
                message.job_id,
                message.idempotency_key,
                message.request_digest,
                request_projection=message.payload,
            )
        )
        if code == "DEADLINE_EXCEEDED":
            return await self._publish_cancelled(message, record, code)
        return await self._publish_failed(message, record, code)

    async def _publish_completed(
        self,
        message: RunnerMessage,
        record: AttemptRecord,
        started: datetime,
        limits: EffectiveLimits,
        results: Sequence[ReplayResult],
        *,
        execution_deadline: datetime | None = None,
        cancel_event: asyncio.Event | None = None,
        lease_lost: asyncio.Event | None = None,
        artifact_budget: _ArtifactBudget | None = None,
    ) -> AttemptRecord:
        result_id = deterministic_terminal_id(message.attempt_id, "completed")
        data = self._base_terminal_data(message, record, limits, result_id, "completed")
        output_references = await self._write_outputs(
            results,
            limits,
            artifact_budget=artifact_budget
            or _ArtifactBudget(limits.max_artifact_operations),
            lease_lost=lease_lost or asyncio.Event(),
            request_capture=cast(dict[str, object], message.payload["data"])[
                "captureAuthorization"
            ],
            execution_deadline=execution_deadline,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            await self._delete_written_outputs(output_references)
            raise _RunnerCancelled("OWNER_REQUESTED")
        if lease_lost is not None and lease_lost.is_set():
            await self._delete_written_outputs(output_references)
            raise _RunnerFailure("DEPENDENCY_UNAVAILABLE", dependency="postgresql")
        data.update(
            {
                "startedAt": _iso(started),
                "completedAt": _iso(_now()),
                "outputArtifactReferences": output_references,
            }
        )
        digest = self._terminal_digest(data)
        data["resultDigest"] = digest
        payload = self._envelope(message, "evaluation.runner.completed", result_id, data)
        async with self._publication_lock:
            try:
                updated = await self._settle_terminal(
                    message.attempt_id,
                    {
                        "status": "completed",
                        "result_id": result_id,
                        "result_digest": digest,
                        "terminal_message_id": UUID(payload["messageId"]),
                        "terminal_payload": payload,
                        "execution_started_at": started,
                    },
                    lease_owner=record.lease_owner,
                    lease_generation=record.lease_generation,
                )
            except AttemptConflict:
                existing = await self.ledger.get(message.attempt_id)
                if existing is None or existing.status not in {"completed", "cancelled", "failed"}:
                    raise
                if existing.status == "cancelled":
                    await self._delete_written_outputs(output_references)
                self._forget(message.attempt_id)
                del results
                return existing
            await self._account_terminal(
                message,
                "completed",
                started,
                dependency="application_validation",
            )
            self._forget(message.attempt_id)
            if updated.publication_status == "published":
                del results
                return updated
            await self.publisher.publish(payload)
            await self.ledger.mark_published(message.attempt_id)
            del results
            return updated

    async def _write_outputs(
        self,
        results: Sequence[ReplayResult],
        limits: EffectiveLimits,
        *,
        artifact_budget: _ArtifactBudget,
        lease_lost: asyncio.Event,
        request_capture: object,
        execution_deadline: datetime | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> list[dict[str, object]]:
        outputs: list[dict[str, object]] = []
        total_bytes = 0
        attempted_reference: dict[str, object] | None = None
        provider_started = False
        try:
            for result in results:
                for reference, content in result.output_artifacts:
                    if cancel_event is not None and cancel_event.is_set():
                        raise _RunnerCancelled("OWNER_REQUESTED")
                    if lease_lost.is_set():
                        raise _RunnerFailure("DEPENDENCY_UNAVAILABLE", dependency="postgresql")
                    remaining = 0.0
                    if execution_deadline is not None:
                        remaining = (execution_deadline - _now()).total_seconds()
                        if remaining <= 0:
                            raise TimeoutError
                    if not self._capture_compatible(
                        request_capture, reference.get("captureAuthorization")
                    ):
                        raise _RunnerFailure("REQUEST_INVALID")
                    artifact_budget.consume()
                    total_bytes += len(content)
                    if total_bytes > limits.max_output_bytes:
                        raise _RunnerFailure("RESOURCE_LIMIT_EXCEEDED")
                    if self.artifact_adapter is None:
                        raise _RunnerFailure("DEPENDENCY_UNAVAILABLE")
                    # The provider may have accepted bytes before the await
                    # below is interrupted.  Keep this exact opaque
                    # reference until cleanup has been attempted; it is
                    # never inferred from content or a provider locator.
                    attempted_reference = dict(reference)
                    adapter = self.artifact_adapter
                    assert adapter is not None

                    async def provider_write(
                        reference_for_write: dict[str, object] = attempted_reference,
                        content_for_write: bytes = content,
                        adapter_for_write: RunnerArtifactPort = adapter,
                    ) -> object:
                        nonlocal provider_started
                        provider_started = True
                        return await adapter_for_write.write(reference_for_write, content_for_write)

                    try:
                        await self._run_guarded(
                            provider_write(),
                            cancel_event=cancel_event or asyncio.Event(),
                            lease_lost=lease_lost,
                            timeout=max(0.001, remaining)
                            if execution_deadline is not None
                            else float(self.config.max_timeout_ms) / 1000,
                        )
                    except (_RunnerCancelled, TimeoutError, _RunnerFailure):
                        raise
                    except Exception:
                        raise _RunnerFailure(
                            "STORAGE_FAILED", dependency="object_storage"
                        ) from None
                    if cancel_event is not None and cancel_event.is_set():
                        raise _RunnerCancelled("OWNER_REQUESTED")
                    if lease_lost.is_set():
                        raise _RunnerFailure("DEPENDENCY_UNAVAILABLE")
                    outputs.append(attempted_reference)
                    attempted_reference = None
                    provider_started = False
            return outputs
        except (_RunnerCancelled, TimeoutError, _RunnerFailure):
            cleanup = [*outputs]
            if attempted_reference is not None and provider_started:
                cleanup.append(attempted_reference)
            await self._delete_written_outputs(cleanup)
            raise

    async def _delete_written_outputs(self, outputs: Sequence[dict[str, object]]) -> None:
        if self.artifact_adapter is None:
            return
        delete = getattr(self.artifact_adapter, "delete", None)
        if not callable(delete):
            return
        for reference in outputs:
            try:
                await asyncio.wait_for(delete(reference), timeout=1.0)
            except Exception:
                self._observe(
                    "evaluation_runner_artifact_cleanup_failure_count",
                    1.0,
                    outcome="error",
                    dependency="object_storage",
                )

    @staticmethod
    def _capture_compatible(
        request_capture: Mapping[str, object], reference_capture: object
    ) -> bool:
        if not isinstance(reference_capture, dict):
            return False
        if reference_capture.get("authorizationContext") != request_capture.get(
            "authorizationContext"
        ) or reference_capture.get("retentionMode") != request_capture.get("retentionMode"):
            return False
        if reference_capture.get("authorizationPurpose") != request_capture.get(
            "authorizationPurpose"
        ):
            return False
        levels = {
            "metadata_only": 0,
            "hashed": 1,
            "redacted": 2,
            "sampled_content": 3,
            "full_content": 4,
        }
        return levels.get(str(reference_capture.get("captureClassification")), -1) <= levels.get(
            str(request_capture.get("captureClassification")), -1
        )

    async def _publish_cancelled(
        self,
        message: RunnerMessage,
        record: AttemptRecord,
        reason: str,
        *,
        started: datetime | None = None,
    ) -> AttemptRecord:
        result_id = deterministic_terminal_id(message.attempt_id, "cancelled")
        data = self._base_terminal_data(
            message,
            record,
            _effective_limits(message.payload["data"]["limits"], self.config),
            result_id,
            "cancelled",
        )
        data["reasonCode"] = reason
        data["completedAt"] = _iso(_now())
        digest = self._terminal_digest(data)
        data["resultDigest"] = digest
        payload = self._envelope(message, "evaluation.runner.cancelled", result_id, data)
        async with self._publication_lock:
            try:
                updated = await self._settle_terminal(
                    message.attempt_id,
                    {
                        "status": "cancelled",
                        "result_id": result_id,
                        "result_digest": digest,
                        "terminal_message_id": UUID(payload["messageId"]),
                        "terminal_payload": payload,
                        "execution_started_at": started,
                    },
                    lease_owner=record.lease_owner if record.status == "leased" else None,
                    lease_generation=record.lease_generation if record.status == "leased" else None,
                )
            except AttemptConflict:
                existing = await self.ledger.get(message.attempt_id)
                if existing is None or existing.status not in {"completed", "cancelled", "failed"}:
                    raise
                self._forget(message.attempt_id)
                return existing
            await self._account_terminal(
                message,
                "canceled",
                started,
                error_class="cancellation" if reason == "OWNER_REQUESTED" else "timeout",
                dependency="application_validation",
            )
            self._forget(message.attempt_id)
            if updated.publication_status == "published":
                return updated
            await self.publisher.publish(payload)
            await self.ledger.mark_published(message.attempt_id)
            return updated

    async def _publish_failed(
        self,
        message: RunnerMessage,
        record: AttemptRecord,
        code: FailureCode,
        *,
        started: datetime | None = None,
        dependency: str | None = None,
    ) -> AttemptRecord:
        result_id = deterministic_terminal_id(message.attempt_id, "failed")
        data = self._base_terminal_data(
            message,
            record,
            _effective_limits(message.payload["data"]["limits"], self.config),
            result_id,
            "failed",
        )
        data.update(
            {
                "failureCode": code,
                "retryable": code in {"DEPENDENCY_UNAVAILABLE", "STORAGE_FAILED"},
                "completedAt": _iso(_now()),
            }
        )
        digest = self._terminal_digest(data)
        data["resultDigest"] = digest
        payload = self._envelope(message, "evaluation.runner.failed", result_id, data)
        async with self._publication_lock:
            try:
                updated = await self._settle_terminal(
                    message.attempt_id,
                    {
                        "status": "failed",
                        "result_id": result_id,
                        "result_digest": digest,
                        "terminal_message_id": UUID(payload["messageId"]),
                        "failure_code": code,
                        "terminal_payload": payload,
                        "execution_started_at": started,
                    },
                    lease_owner=record.lease_owner,
                    lease_generation=record.lease_generation,
                )
            except AttemptConflict:
                existing = await self.ledger.get(message.attempt_id)
                if existing is None or existing.status not in {"completed", "cancelled"}:
                    raise
                self._forget(message.attempt_id)
                return existing
            await self._account_terminal(
                message,
                "failed",
                started,
                error_class=self._error_class_for_code(code),
                dependency=dependency or self._dependency_for_code(code),
                failure_code=code,
            )
            self._forget(message.attempt_id)
            if updated.publication_status == "published":
                return updated
            await self.publisher.publish(payload)
            await self.ledger.mark_published(message.attempt_id)
            return updated

    async def _republish_terminal(
        self, record: AttemptRecord, message: RunnerMessage | None
    ) -> None:
        # Business truth is the ledger.  The exact content-free envelope is
        # persisted with the terminal transition, so recovery never rebuilds
        # timestamps or evidence from a process-local request cache.
        del message
        payload = record.terminal_payload
        if record.publication_status == "published" or payload is None:
            self._forget(record.attempt_id)
            return
        try:
            terminal_message = validate_message(payload)
            outcome = {
                "completed": "completed",
                "cancelled": "canceled",
                "failed": "failed",
            }[record.status]
            terminal_data = cast(Mapping[str, object], terminal_message.payload["data"])
            await self._account_terminal(
                terminal_message,
                outcome,
                record.execution_started_at,
                dependency=self._dependency_for_code(str(terminal_data.get("failureCode", ""))),
                failure_code=str(terminal_data.get("failureCode", "")),
            )
        except (TransportValidationError, KeyError):
            self._observe(
                "evaluation_runner_terminal_accounting_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
            )
            return
        except Exception as exc:
            self._observe(
                "evaluation_runner_terminal_accounting_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
                error_class=_bounded_exception_class(exc),
            )
            return
        try:
            await self.publisher.publish(payload)
        except Exception as exc:
            self._observe(
                "evaluation_runner_publication_recovery_failure_count",
                1.0,
                outcome="error",
                dependency="nats_jetstream",
                error_class=_bounded_exception_class(exc),
            )
            return
        try:
            await self.ledger.mark_published(record.attempt_id)
        except Exception as exc:
            self._observe(
                "evaluation_runner_publication_recovery_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
                error_class=_bounded_exception_class(exc),
            )
            return
        self._forget(record.attempt_id)
        self._observe(
            "evaluation_runner_nats_recovery_count",
            1.0,
            outcome="published",
            dependency="nats_jetstream",
        )

    async def _settle_terminal(
        self,
        attempt_id: UUID,
        update: Mapping[str, object],
        *,
        lease_owner: str | None,
        lease_generation: UUID | None,
    ) -> AttemptRecord:
        """Persist terminal state and account for bounded ledger failures.

        The worker loop deliberately does not expose provider exception text.
        Recording the finite class here makes a failed SQL transaction visible
        to operators while preserving the retry/recovery path.
        """

        try:
            return await self.ledger.terminal(
                attempt_id,
                update,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
        except AttemptConflict:
            raise
        except Exception as exc:
            self._observe(
                "evaluation_runner_terminal_settlement_failure_count",
                1.0,
                outcome="error",
                dependency="postgresql",
                error_class=_bounded_exception_class(exc),
            )
            raise

    def _forget(self, attempt_id: UUID) -> None:
        self._request_messages.pop(attempt_id, None)
        self._cancel_events.pop(attempt_id, None)
        self._enqueued_at.pop(attempt_id, None)
        self._queued_attempts.discard(attempt_id)

    @staticmethod
    def _span_id(message: RunnerMessage, operation: str) -> str:
        return hashlib.sha256(
            f"{message.message_id}:{message.attempt_id}:{operation}".encode()
        ).hexdigest()[:16]

    @staticmethod
    def _error_class_for_code(code: str) -> str:
        if code in {"DEADLINE_EXCEEDED"}:
            return "timeout"
        if code in {"RESOURCE_LIMIT_EXCEEDED"}:
            return "budget"
        if code in {"DEPENDENCY_UNAVAILABLE", "STORAGE_FAILED"}:
            return "provider"
        if code in {"REQUEST_INVALID"}:
            return "validation"
        if code in {"CANCELLED"}:
            return "cancellation"
        if code in {"EXECUTION_FAILED", "INTERNAL_FAILURE"}:
            return "execution"
        return "unknown"

    @staticmethod
    def _dependency_for_code(code: str) -> str:
        if code == "STORAGE_FAILED":
            return "object_storage"
        if code == "DEPENDENCY_UNAVAILABLE":
            return "model_provider"
        if code in {"REQUEST_INVALID", "RESOURCE_LIMIT_EXCEEDED", "EXECUTION_FAILED"}:
            return "application_validation"
        if code == "INTERNAL_FAILURE":
            return "application_unknown"
        return "application_validation"

    def _record_terminal_span(
        self,
        message: RunnerMessage,
        outcome: str,
        started: datetime | None,
        *,
        error_class: str | None = None,
        dependency: str = "application_validation",
    ) -> None:
        measurement_started = started or _now()
        duration_ms = max(0.001, (_now() - measurement_started).total_seconds() * 1000)
        self._observe(
            "evaluation_runner_execution_duration_ms",
            duration_ms,
            outcome=outcome,
            dependency=dependency,
            **({"error_class": error_class} if error_class else {}),
        )
        self._record_span(
            "evaluation.execute",
            duration_ms,
            message,
            dependency=dependency,
            outcome=outcome,
            error_class=error_class,
        )

    async def _account_terminal(
        self,
        message: RunnerMessage,
        outcome: str,
        started: datetime | None,
        *,
        error_class: str | None = None,
        dependency: str = "application_validation",
        failure_code: str | None = None,
    ) -> bool:
        """Account durable settlement exactly once, before broker publication."""

        if message.attempt_id in self._accounted_terminals:
            return False
        account = getattr(self.ledger, "account_terminal", None)
        if callable(account):
            if not await account(message.attempt_id):
                self._accounted_terminals[message.attempt_id] = outcome
                return False
        self._accounted_terminals[message.attempt_id] = outcome
        if len(self._accounted_terminals) > 4096:
            oldest = next(iter(self._accounted_terminals))
            del self._accounted_terminals[oldest]
        metric = (
            "evaluation_runner_cancellation_count"
            if outcome == "canceled"
            else "evaluation_runner_outcome_count"
        )
        self.metrics.increment(self.component, metric, outcome=outcome)
        if (
            outcome == "failed"
            and failure_code == "RESOURCE_LIMIT_EXCEEDED"
        ):
            self.metrics.increment(
                self.component, "evaluation_runner_resource_limit_count", outcome="rejected"
            )
        self._record_terminal_span(
            message,
            outcome,
            started,
            error_class=error_class,
            dependency=dependency,
        )
        return True

    def _record_span(
        self,
        operation: str,
        duration_ms: float,
        message: RunnerMessage,
        *,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        # Correlation is the trace root.  Attempt and operation only derive a
        # stable span identity; neither is emitted as a metric dimension.
        trace_id = hashlib.sha256(str(message.correlation_id).encode()).hexdigest()[:32]
        try:
            self.metrics.record_span(
                self.component,
                operation,
                duration_ms,
                trace_id=trace_id,
                span_id=self._span_id(message, operation),
                parent_span_id=parent_span_id,
                dependency=dependency,
                outcome=outcome,
                error_class=error_class,
                attempt_id=str(message.attempt_id),
                correlation_id=str(message.correlation_id),
            )
        except TypeError:
            # Keep third-party test ports compatible while MetadataMetrics
            # receives the complete trace context above.
            self.metrics.record_span(
                self.component, operation, duration_ms, outcome=outcome
            )

    def _observe(self, name: str, value: float, **attributes: str) -> None:
        observe = getattr(self.metrics, "observe", None)
        if callable(observe):
            try:
                observe(self.component, name, value, **attributes)
            except TypeError:
                observe(self.component, name, value)

    def _base_terminal_data(
        self,
        message: RunnerMessage,
        record: AttemptRecord,
        limits: EffectiveLimits,
        result_id: UUID,
        status: str,
    ) -> dict[str, object]:
        del record, result_id
        request = message.payload["data"]
        assert isinstance(request, dict)
        provenance = request["provenance"]
        target = request["targetComponent"]
        assert isinstance(provenance, dict) and isinstance(target, dict)
        declared = self._declared_runtime_evidence(message)
        try:
            observed = self.runtime_evidence.observe(
                component_id=str(target["componentId"]),
                component_version=str(target["componentVersion"]),
            )
        except ReplayError:
            # Admission has already converted a configured binding mismatch
            # into REQUEST_INVALID; terminal projection remains content-free.
            observed = {}
        runtime = {
            key: self._evidence(declared_value, observed.get(key))
            for key, declared_value in declared.items()
        }
        verified = sum(1 for value in runtime.values() if value.get("status") == "verified")
        self._observe("evaluation_runner_reproducibility_completeness", verified / len(runtime))
        return {
            "resultId": str(deterministic_terminal_id(message.attempt_id, status)),
            "resultDigest": "0" * 64,
            "status": status,
            "deadlineAt": request["deadlineAt"],
            "captureAuthorization": request["captureAuthorization"],
            "effectiveLimits": limits.payload(),
            "provenance": provenance,
            "runtimeEvidence": runtime,
        }

    @staticmethod
    def _declared_runtime_evidence(message: RunnerMessage) -> dict[str, object]:
        request = message.payload["data"]
        assert isinstance(request, dict)
        provenance = request["provenance"]
        target = request["targetComponent"]
        assert isinstance(provenance, dict) and isinstance(target, dict)
        model = provenance["model"]
        reproducibility = provenance["reproducibility"]
        assert isinstance(model, dict) and isinstance(reproducibility, dict)
        return {
            "componentVersion": target["componentVersion"],
            "codeRevision": target["codeRevision"],
            "imageDigest": target["imageDigest"],
            "configurationRevision": target["configurationRevision"],
            "modelId": model["modelId"],
            "modelVersion": model["modelVersion"],
            "modelDigest": model["modelDigest"],
            "deploymentRevision": provenance["deploymentRevision"],
            "fixtureRevision": reproducibility["fixtureRevision"],
            "promptRevision": reproducibility["promptRevision"],
            "policyRevision": reproducibility["policyRevision"],
            "seed": reproducibility["seed"],
        }

    def _assert_runtime_evidence(self, message: RunnerMessage) -> None:
        request = message.payload["data"]
        assert isinstance(request, dict)
        target = request["targetComponent"]
        assert isinstance(target, dict)
        try:
            observed = self.runtime_evidence.observe(
                component_id=str(target["componentId"]),
                component_version=str(target["componentVersion"]),
            )
        except ReplayError as exc:
            raise _RunnerFailure("REQUEST_INVALID") from exc
        declared = self._declared_runtime_evidence(message)
        if any(
            observed.get(key) is not None and observed.get(key) != value
            for key, value in declared.items()
        ):
            raise _RunnerFailure("REQUEST_INVALID")

    @staticmethod
    def _evidence(declared: object, observed: object) -> dict[str, object]:
        if observed is None:
            return {"status": "missing"}
        if observed == declared:
            return {"status": "verified", "observedValue": observed}
        return {"status": "mismatched", "observedValue": observed}

    def _envelope(
        self, message: RunnerMessage, message_type: str, result_id: UUID, data: dict[str, object]
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "messageId": str(deterministic_message_id(result_id, data["status"])),
            "messageType": message_type,
            "occurredAt": _iso(_now()),
            "correlationId": str(message.correlation_id),
            "causationId": str(message.message_id),
            "requestId": str(message.request_id),
            "jobId": str(message.job_id),
            "attemptId": str(message.attempt_id),
            "idempotencyKey": str(message.idempotency_key),
            "requestDigest": message.request_digest,
            "data": data,
        }

    @staticmethod
    def _terminal_digest(data: Mapping[str, object]) -> str:
        unsigned = dict(data)
        unsigned["resultDigest"] = ""
        return hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class _RunnerFailure(Exception):
    def __init__(self, code: FailureCode, *, dependency: str = "application_validation") -> None:
        self.code = code
        self.dependency = dependency


class _RunnerCancelled(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


__all__ = [
    "EffectiveLimits",
    "EvaluationRunner",
    "NullResultPublisher",
    "ResultPublisher",
    "RunnerConfig",
]
