"""PostgreSQL adapter for the Observatory evaluation state machine."""

from __future__ import annotations

import asyncio
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from aura_observatory.evaluation_orchestration import (
    RETRYABLE_FAILURES,  # pyright: ignore[reportPrivateUsage]
    TERMINAL_STATES,
    AttemptState,
    EvaluationAdmission,
    EvaluationAttempt,
    EvaluationJob,
    EvaluationOrchestrationRepository,
    EvaluationOutboxEntry,
    EvaluationSchedule,
    JobState,
    OrchestrationError,
    ResultApplication,
    _cancellation_envelope,  # pyright: ignore[reportPrivateUsage]
    _deterministic_attempt_id,  # pyright: ignore[reportPrivateUsage]
    _deterministic_cancel_id,  # pyright: ignore[reportPrivateUsage]
    _deterministic_job_id,  # pyright: ignore[reportPrivateUsage]
    _deterministic_uuid,  # pyright: ignore[reportPrivateUsage]
    _runner_envelope,  # pyright: ignore[reportPrivateUsage]
    _scheduled_admission,  # pyright: ignore[reportPrivateUsage]
    _timestamp,  # pyright: ignore[reportPrivateUsage]
    _validate_result_binding,  # pyright: ignore[reportPrivateUsage]
)
from aura_observatory.metrics import ComponentSpan, ObservatoryMetrics

_SAFE_TELEMETRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_TELEMETRY_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_SAFE_TELEMETRY_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


class ComponentSpanTransport(Protocol):
    """Provider-side HTTP seam for the metadata-only span exporter."""

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None: ...


class OtlpComponentSpanExporter:
    """Bounded metadata-only OTLP trace provider for Observatory components."""

    def __init__(
        self,
        endpoint: str,
        *,
        credential: str | None = None,
        queue_capacity: int = 32,
        max_attempts: int = 3,
        timeout_seconds: float = 2.0,
        transport: ComponentSpanTransport | None = None,
        environment: str = "production",
        git_commit: str = "0000000",
        host_id: str = "local",
        service_role: str = "worker",
        deployment_id: str = "local",
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or queue_capacity < 1
            or max_attempts < 1
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid Observatory OTLP exporter configuration")
        if service_role not in {"api", "worker", "scheduler", "cli"}:
            raise ValueError("invalid Observatory OTLP service role")
        if not _SAFE_TELEMETRY_ID.fullmatch(deployment_id):
            raise ValueError("invalid Observatory OTLP deployment identity")
        if not _SAFE_TELEMETRY_ID.fullmatch(host_id):
            raise ValueError("invalid Observatory OTLP host identity")
        if not _SAFE_TELEMETRY_ENVIRONMENT.fullmatch(environment):
            raise ValueError("invalid Observatory OTLP environment")
        if not _SAFE_TELEMETRY_COMMIT.fullmatch(git_commit):
            raise ValueError("invalid Observatory OTLP commit identity")
        self.endpoint = endpoint
        self.credential = credential
        self.queue_capacity = queue_capacity
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.environment = environment
        self.git_commit = git_commit
        self.host_id = host_id
        self.service_role = service_role
        self.deployment_id = deployment_id
        self._transport = transport or _DefaultComponentSpanTransport()
        self._queue: asyncio.Queue[tuple[ComponentSpan, int]] = asyncio.Queue(queue_capacity)
        self._task: asyncio.Task[None] | None = None
        self._accepted = 0
        self._failures = 0
        self._dropped = 0
        self._retries = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def stats(self) -> Mapping[str, int]:
        return {
            "accepted": self._accepted,
            "failures": self._failures,
            "dropped": self._dropped,
            "retries": self._retries,
        }

    def submit(self, span: ComponentSpan) -> None:
        try:
            self._queue.put_nowait((span, 0))
        except asyncio.QueueFull:
            self._dropped += 1
            return
        self._accepted += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dropped += 1
            self._accepted = max(0, self._accepted - 1)
            return
        if not self.running:
            self._task = loop.create_task(self._run())

    async def flush(self, *, timeout_seconds: float = 5.0) -> None:
        if self._task is None:
            return
        timeout = min(30.0, max(0.1, float(timeout_seconds)))
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except TimeoutError:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._dropped += 1
                self._queue.task_done()
        task = self._task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            span, attempt = await self._queue.get()
            try:
                await self._transport.send(
                    self._trace_endpoint(),
                    self._payload(span),
                    self.credential,
                    self.timeout_seconds,
                )
            except asyncio.CancelledError:
                self._dropped += 1
                raise
            except Exception:
                self._failures += 1
                if attempt + 1 < self.max_attempts:
                    self._retries += 1
                    try:
                        self._queue.put_nowait((span, attempt + 1))
                    except asyncio.QueueFull:
                        self._dropped += 1
                else:
                    self._dropped += 1
            finally:
                self._queue.task_done()

    def _trace_endpoint(self) -> str:
        parsed = urllib.parse.urlsplit(self.endpoint)
        path = parsed.path.rstrip("/")
        for suffix in ("/v1/observations", "/v1/traces"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"{path}/v1/traces", "", ""))

    def _payload(self, span: ComponentSpan) -> bytes:
        def attribute(key: str, value: str) -> dict[str, object]:
            return {"key": key, "value": {"stringValue": value}}

        # Observatory's existing OTLP boundary requires the same closed
        # observation projection as Core. Derive the durable observation ID
        # from the emitted span identity so retries remain idempotent without
        # putting lifecycle identifiers into telemetry.
        observation_id = str(
            uuid5(NAMESPACE_URL, f"aura-observatory-span:{span.trace_id}:{span.span_id}")
        )
        outcome = span.outcome
        status = "error" if outcome in {"failure", "timeout", "unavailable"} else "ok"
        attributes = [
            attribute("aura.observation_id", observation_id),
            attribute("aura.component_id", span.component_id),
            attribute("aura.component_version", span.component_version),
            attribute("aura.operation", span.operation),
            attribute("aura.metric_name", "observatory_evaluation_operation_duration_ms"),
            attribute("aura.capture_policy", "metadata_only"),
            attribute("aura.dimension.outcome", outcome),
            attribute("aura.dimension.status", status),
        ]
        if span.dependency is not None:
            attributes.append(attribute("aura.dimension.dependency", span.dependency))
        if span.error_code is not None:
            attributes.append(attribute("aura.dimension.error_class", span.error_code))
        end_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000)
        start_ns = max(0, end_ns - int(span.duration_ms * 1_000_000))
        otlp_span: dict[str, object] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.operation,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": attributes,
            "status": {
                "code": "STATUS_CODE_ERROR"
                if span.outcome in {"failure", "timeout", "unavailable"}
                else "STATUS_CODE_OK"
            },
        }
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            # The existing Observatory OTLP adapter accepts
                            # the Core-compatible service marker only; the
                            # Observatory component identity is carried by
                            # the closed span attributes above.
                            attribute("service.name", "aura-core"),
                            attribute("service.role", self.service_role),
                            attribute("deployment.id", self.deployment_id),
                            attribute("deployment.environment", self.environment),
                            attribute("deployment.git_commit", self.git_commit),
                            attribute("host.id", self.host_id),
                            attribute("capture.policy", "metadata_only"),
                        ]
                    },
                    "schemaUrl": "https://aura.local/contracts/telemetry/v2/metadata-observation.schema.json",
                    "scopeSpans": [
                        {
                            "scope": {"name": "aura.observatory.telemetry", "version": "1.0.0"},
                            "spans": [otlp_span],
                        }
                    ],
                }
            ]
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class _DefaultComponentSpanTransport:
    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"

        def request() -> None:
            request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if response.status >= 300:
                        raise urllib.error.HTTPError(
                            endpoint,
                            response.status,
                            "Collector rejected telemetry",
                            response.headers,
                            None,
                        )
            except urllib.error.URLError as exc:
                raise ConnectionError("Collector telemetry export failed") from exc

        await asyncio.to_thread(request)


class PostgresEvaluationMetricStore:
    """Persist bounded orchestration series through Observatory PostgreSQL."""

    _NAMES = frozenset(
        {
            "observatory_evaluation_job_admission_count",
            "observatory_evaluation_attempt_transition_count",
            "observatory_evaluation_retry_scheduled_count",
            "observatory_evaluation_terminal_count",
            "observatory_evaluation_dependency_outcome_count",
            "observatory_evaluation_cancellation_count",
            "observatory_evaluation_deadline_count",
            "observatory_evaluation_outbox_pending_count",
            "observatory_evaluation_backlog_count",
            "observatory_evaluation_outbox_oldest_age_seconds",
            "observatory_evaluation_outbox_publication_count",
            "observatory_evaluation_result_consumption_count",
            "observatory_evaluation_worker_heartbeat_age_seconds",
            "observatory_evaluation_scheduler_heartbeat_age_seconds",
            "observatory_evaluation_scheduler_occurrence_count",
            "observatory_evaluation_result_lag_seconds",
            "observatory_evaluation_operation_duration_ms",
        }
    )

    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    @staticmethod
    def _columns(labels: Mapping[str, str]) -> tuple[str, str, str, str, str]:
        normalized = {str(key): str(value) for key, value in labels.items()}
        return tuple(normalized.get(key, "") for key in (
            "operation", "outcome", "selector", "phase", "failure_code"
        ))  # type: ignore[return-value]

    async def record_metric(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str],
        *,
        histogram: bool = False,
    ) -> None:
        if name not in self._NAMES or not math.isfinite(value) or abs(value) > 1e15:
            return
        if not ObservatoryMetrics.labels_valid(name, dict(labels)):
            return
        columns = self._columns(labels)
        buckets = {
            str(bucket): int(value <= bucket)
            for bucket in ObservatoryMetrics.DURATION_BUCKETS
        } if histogram else {}
        bucket_merge = ", ".join(
            f"'{bucket}', COALESCE("
            f"(observatory_evaluation_metric_series.histogram_buckets->>'{bucket}')::bigint, 0) "
            f"+ COALESCE((EXCLUDED.histogram_buckets->>'{bucket}')::bigint, 0)"
            for bucket in ObservatoryMetrics.DURATION_BUCKETS
        )
        async with self._connection_factory() as connection:
            await connection.execute(
                f"""INSERT INTO observatory_evaluation_metric_series(
                metric_name,label_operation,label_outcome,label_selector,label_phase,
                label_failure_code,value,histogram_count,histogram_buckets,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,CURRENT_TIMESTAMP)
                ON CONFLICT (metric_name,label_operation,label_outcome,label_selector,
                label_phase,label_failure_code) DO UPDATE SET
                value=observatory_evaluation_metric_series.value+EXCLUDED.value,
                histogram_count=observatory_evaluation_metric_series.histogram_count+EXCLUDED.histogram_count,
                histogram_buckets=jsonb_build_object({bucket_merge}),updated_at=CURRENT_TIMESTAMP""",
                (
                    name,
                    *columns,
                    value,
                    1 if histogram else 0,
                    json.dumps(buckets, separators=(",", ":")),
                ),
            )

    async def set_metric(self, name: str, value: float, labels: Mapping[str, str]) -> None:
        if name not in self._NAMES or not math.isfinite(value) or abs(value) > 1e15:
            return
        if not ObservatoryMetrics.labels_valid(name, dict(labels)):
            return
        columns = self._columns(labels)
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_evaluation_metric_series(
                metric_name,label_operation,label_outcome,label_selector,label_phase,
                label_failure_code,value,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT (metric_name,label_operation,label_outcome,label_selector,
                label_phase,label_failure_code) DO UPDATE SET value=EXCLUDED.value,
                updated_at=CURRENT_TIMESTAMP""",
                (name, *columns, value),
            )


class PostgresEvaluationOrchestrationRepository(EvaluationOrchestrationRepository):
    """Only accesses AURA-0092 tables through a caller-owned connection factory."""

    def __init__(
        self,
        connection_factory: Any,
        *,
        retry_base_seconds: int = 10,
        retry_max_seconds: int = 86_400,
    ) -> None:
        if not 1 <= retry_base_seconds <= retry_max_seconds <= 86_400:
            raise ValueError("retry backoff out of bounds")
        self._connection_factory = connection_factory
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    def _retry_delay(self, attempt_number: int) -> int:
        return min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** max(0, attempt_number - 1)),
        )

    async def admit(self, request: EvaluationAdmission, job_id: str) -> EvaluationJob:
        request.validate()
        now = request.created_at.astimezone(UTC)
        attempt_id = _deterministic_attempt_id(job_id, 1)
        attempt = EvaluationAttempt(
            attempt_id, job_id, 1, request.fingerprint(), next_attempt_at=now
        )
        job = EvaluationJob(job_id, request, created_at=now, updated_at=now)
        envelope = _runner_envelope(job, attempt, now)
        async with self._connection_factory() as connection:
            async with connection.transaction():
                existing = await connection.execute(
                    "SELECT job_id, request_fingerprint, envelope_json, state, "
                    "attempt_count, created_at, updated_at "
                    "FROM observatory_evaluation_jobs WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                )
                row = await existing.fetchone()
                if row is not None:
                    if str(row[1]) != request.fingerprint():
                        raise OrchestrationError("idempotency_conflict")
                    return _job_from_row(row, request)
                inserted = await connection.execute(
                    """INSERT INTO observatory_evaluation_jobs(
                    job_id, request_id, idempotency_key, request_fingerprint,
                    request_version, dataset_id, dataset_version, suite_id,
                    suite_version, evaluator_id, evaluator_version,
                    capture_classification, deadline_at, max_attempts,
                    envelope_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING RETURNING job_id""",
                    (
                        job_id,
                        request.request_id,
                        request.idempotency_key,
                        request.fingerprint(),
                        request.request_version,
                        request.dataset_id,
                        request.dataset_version,
                        request.suite_id,
                        request.suite_version,
                        request.evaluator_id,
                        request.evaluator_version,
                        request.capture_classification,
                        request.deadline_at,
                        request.max_attempts,
                        json.dumps(request.envelope, sort_keys=True, separators=(",", ":")),
                    ),
                )
                if await inserted.fetchone() is None:
                    existing = await connection.execute(
                        "SELECT job_id, request_fingerprint, envelope_json, state, "
                        "attempt_count, created_at, updated_at "
                        "FROM observatory_evaluation_jobs WHERE idempotency_key = %s FOR UPDATE",
                        (request.idempotency_key,),
                    )
                    row = await existing.fetchone()
                    if row is None:
                        raise OrchestrationError("idempotency_race")
                    if str(row[1]) != request.fingerprint():
                        raise OrchestrationError("idempotency_conflict")
                    return _job_from_row(row, request)
                await connection.execute(
                    """INSERT INTO observatory_evaluation_attempts(
                    attempt_id, job_id, attempt_number, request_id,
                    request_digest, next_attempt_at) VALUES (%s, %s, 1, %s, %s, %s)""",
                    (attempt_id, job_id, request.request_id, request.fingerprint(), now),
                )
                await connection.execute(
                    """INSERT INTO observatory_evaluation_outbox(
                    event_id, event_type, job_id, attempt_id, envelope_json,
                    next_publish_at)
                    VALUES (%s, 'evaluation.runner.requested', %s, %s, %s::jsonb, %s)
                    ON CONFLICT (event_id) DO NOTHING""",
                    (
                        envelope["messageId"],
                        job_id,
                        attempt_id,
                        json.dumps(envelope, sort_keys=True),
                        now,
                    ),
                )
        return job

    async def get_job(self, job_id: str) -> EvaluationJob | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT job_id, request_id, idempotency_key, request_version,
                dataset_id, dataset_version, suite_id, suite_version,
                evaluator_id, evaluator_version, capture_classification,
                deadline_at, max_attempts, envelope_json, state, attempt_count,
                created_at, updated_at, request_fingerprint
                FROM observatory_evaluation_jobs WHERE job_id = %s""",
                (job_id,),
            )
            row = await result.fetchone()
        return _job_from_full_row(row) if row is not None else None

    async def claim_due_attempts(
        self, *, now: datetime, limit: int, lease_seconds: int, worker_id: str
    ) -> Sequence[EvaluationAttempt]:
        del worker_id
        attempts: list[EvaluationAttempt] = []
        async with self._connection_factory() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """SELECT attempt_id, job_id, attempt_number, request_digest,
                    state, next_attempt_at, lease_token, lease_expires_at,
                    started_at, completed_at, failure_code, terminal_reason
                    FROM observatory_evaluation_attempts a
                    WHERE a.state = 'queued' AND a.next_attempt_at <= %s
                    AND NOT EXISTS (SELECT 1 FROM observatory_evaluation_jobs j
                                    WHERE j.job_id = a.job_id AND j.state IN
                                    ('succeeded', 'failed', 'cancelled', 'deadline'))
                    ORDER BY a.next_attempt_at, a.attempt_id LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (now, limit),
                )
                rows = await result.fetchall()
                for row in rows:
                    token = str(uuid4())
                    expiry = now + timedelta(seconds=lease_seconds)
                    updated = await connection.execute(
                        """UPDATE observatory_evaluation_attempts SET state='running',
                        lease_token=%s, lease_expires_at=%s, started_at=COALESCE(started_at, %s),
                        updated_at=%s WHERE attempt_id=%s AND state='queued'""",
                        (token, expiry, now, now, row[0]),
                    )
                    if getattr(updated, "rowcount", 1) != 1:
                        continue
                    await connection.execute(
                        "UPDATE observatory_evaluation_jobs SET state='running', "
                        "updated_at=%s WHERE job_id=%s AND state='queued'",
                        (now, row[1]),
                    )
                    attempts.append(_attempt_from_row((*row[:5], now, token, expiry, *row[8:])))
        return attempts

    async def claim_outbox(
        self,
        *,
        now: datetime,
        limit: int,
        lease_seconds: int = 120,
        worker_id: str = "",
    ) -> Sequence[EvaluationOutboxEntry]:
        del worker_id
        if not 1 <= lease_seconds <= 3600:
            raise OrchestrationError("outbox_lease_invalid")
        claimed: list[EvaluationOutboxEntry] = []
        async with self._connection_factory() as connection:
            async with connection.transaction():
                # A requested command is no longer publishable once its job or
                # attempt is terminal.  Mark it exhausted before selecting so
                # stale rows cannot block the queue forever.
                await connection.execute(
                    """WITH stale AS (
                    SELECT o.event_id
                    FROM observatory_evaluation_outbox o
                    JOIN observatory_evaluation_jobs j ON j.job_id=o.job_id
                    JOIN observatory_evaluation_attempts a ON a.attempt_id=o.attempt_id
                    WHERE o.event_type='evaluation.runner.requested'
                    AND o.published_at IS NULL AND (
                        j.state IN ('succeeded','failed','cancelled','deadline')
                        OR a.state IN ('succeeded','failed','cancelled','deadline'))
                    )
                    UPDATE observatory_evaluation_outbox o SET published_at=%s,
                    last_error_code='ineligible', lease_token=NULL, lease_expires_at=NULL
                    WHERE o.event_id IN (SELECT event_id FROM stale)""",
                    (now,),
                )
                result = await connection.execute(
                    """SELECT o.event_id, o.event_type, o.job_id, o.attempt_id,
                    o.envelope_json, o.published_at, o.publish_attempts, o.last_error_code,
                    o.next_publish_at, o.lease_token, o.lease_expires_at
                    FROM observatory_evaluation_outbox o
                    WHERE o.published_at IS NULL AND o.publish_attempts < 10
                    AND o.next_publish_at <= %s
                    AND (o.event_type <> 'evaluation.runner.requested'
                         OR EXISTS (
                             SELECT 1 FROM observatory_evaluation_attempts a
                             WHERE a.attempt_id=o.attempt_id
                             AND a.next_attempt_at <= %s
                         ))
                    AND (o.lease_expires_at IS NULL OR o.lease_expires_at <= %s)
                    ORDER BY o.created_at, o.event_id LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (now, now, now, limit),
                )
                rows = await result.fetchall()
                for row in rows:
                    token = str(uuid4())
                    expiry = now + timedelta(seconds=lease_seconds)
                    await connection.execute(
                        """UPDATE observatory_evaluation_outbox
                        SET lease_token=%s, lease_expires_at=%s
                        WHERE event_id=%s AND published_at IS NULL""",
                        (token, expiry, row[0]),
                    )
                    claimed.append(_outbox_from_row((*row[:9], token, expiry)))
        return claimed

    async def mark_outbox_published(
        self, event_id: str, *, at: datetime, lease_token: str | None = None
    ) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "UPDATE observatory_evaluation_outbox SET published_at=%s, "
                "lease_token=NULL, lease_expires_at=NULL WHERE event_id=%s "
                "AND published_at IS NULL AND lease_token IS NOT DISTINCT FROM %s::uuid",
                (at, event_id, lease_token),
            )
        return getattr(result, "rowcount", 1) == 1

    async def mark_outbox_failed(
        self,
        event_id: str,
        code: str,
        *,
        at: datetime,
        lease_token: str | None = None,
    ) -> None:
        if code not in {"transport_unavailable", "database_failure", "timeout"}:
            code = "transport_unavailable"
        async with self._connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_evaluation_outbox SET "
                "publish_attempts=LEAST(10,publish_attempts+1), "
                "last_error_code=CASE WHEN publish_attempts + 1 >= 10 "
                "THEN 'exhausted' ELSE %s END, "
                "next_publish_at=%s::timestamptz + (LEAST(86400, %s * "
                "(2 ^ GREATEST(0, publish_attempts))) * INTERVAL '1 second'), "
                "published_at=CASE WHEN publish_attempts + 1 >= 10 "
                "THEN %s::timestamptz ELSE published_at END, lease_token=NULL, "
                "lease_expires_at=NULL WHERE event_id=%s AND published_at IS NULL "
                "AND lease_token IS NOT DISTINCT FROM %s::uuid",
                (code, at, self._retry_base_seconds, at, event_id, lease_token),
            )

    async def request_cancellation(
        self, job_id: str, *, reason: str, now: datetime
    ) -> EvaluationOutboxEntry | None:
        async with self._connection_factory() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """SELECT job_id, request_id, idempotency_key, request_version,
                    dataset_id, dataset_version, suite_id, suite_version,
                    evaluator_id, evaluator_version, capture_classification,
                    deadline_at, max_attempts, envelope_json, state, attempt_count,
                    created_at, updated_at, request_fingerprint
                    FROM observatory_evaluation_jobs WHERE job_id=%s FOR UPDATE""",
                    (job_id,),
                )
                row = await result.fetchone()
                if row is None:
                    raise OrchestrationError("job_not_found")
                job = _job_from_full_row(row)
                if job.state in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.DEADLINE,
                }:
                    return None
                attempt_result = await connection.execute(
                    """SELECT attempt_id, job_id, attempt_number, request_digest, state,
                    next_attempt_at, lease_token, lease_expires_at, started_at,
                    completed_at, failure_code, terminal_reason
                    FROM observatory_evaluation_attempts WHERE job_id=%s
                    ORDER BY attempt_number DESC LIMIT 1 FOR UPDATE""",
                    (job_id,),
                )
                attempt_row = await attempt_result.fetchone()
                if attempt_row is None:
                    raise OrchestrationError("unknown_attempt")
                attempt = _attempt_from_row(attempt_row)
                event_id = _deterministic_cancel_id(job_id, attempt.attempt_id)
                envelope = _cancellation_envelope(job, attempt, event_id, reason, now)
                await connection.execute(
                    "UPDATE observatory_evaluation_jobs SET state='cancelled', "
                    "updated_at=%s WHERE job_id=%s AND state NOT IN "
                    "('succeeded','failed','cancelled','deadline')",
                    (now, job_id),
                )
                await connection.execute(
                    "UPDATE observatory_evaluation_attempts SET state='cancelled', "
                    "completed_at=%s, terminal_reason=%s, updated_at=%s "
                    "WHERE attempt_id=%s AND state NOT IN "
                    "('succeeded','failed','cancelled','deadline')",
                    (now, reason, now, attempt.attempt_id),
                )
                await connection.execute(
                    """INSERT INTO observatory_evaluation_lifecycle_audits(
                    audit_id, job_id, attempt_id, event_type, outcome, occurred_at)
                    VALUES (%s, %s, %s, 'evaluation.cancelled', 'accepted', %s)
                    ON CONFLICT DO NOTHING""",
                    (str(uuid4()), job_id, attempt.attempt_id, now),
                )
                await connection.execute(
                    """INSERT INTO observatory_evaluation_outbox(
                    event_id,event_type,job_id,attempt_id,envelope_json,next_publish_at)
                    VALUES (%s,'evaluation.runner.cancellation_requested',%s,%s,%s::jsonb,%s)
                    ON CONFLICT (event_id) DO NOTHING""",
                    (
                        event_id,
                        job_id,
                        attempt.attempt_id,
                        json.dumps(envelope, sort_keys=True),
                        now,
                    ),
                )
                return EvaluationOutboxEntry(
                    event_id,
                    "evaluation.runner.cancellation_requested",
                    job_id,
                    attempt.attempt_id,
                    envelope,
                )

    async def apply_result(
        self, envelope: Mapping[str, object], *, received_at: datetime
    ) -> ResultApplication:
        message_id = str(envelope["messageId"])
        data = cast(Mapping[str, object], envelope["data"])
        result_id = str(data["resultId"])
        kind = str(envelope["messageType"])
        status = (
            "completed"
            if kind.endswith("completed")
            else "cancelled"
            if kind.endswith("cancelled")
            else "failed"
        )
        result_digest = str(data["resultDigest"])
        async with self._connection_factory() as connection:
            async with connection.transaction():
                duplicate = await connection.execute(
                    "SELECT disposition FROM observatory_evaluation_result_receipts "
                    "WHERE message_id=%s FOR UPDATE",
                    (message_id,),
                )
                prior = await duplicate.fetchone()
                if prior is not None:
                    return ResultApplication("duplicate")
                attempt_result = await connection.execute(
                    """SELECT a.attempt_id, a.job_id, a.attempt_number, a.request_digest,
                    a.state, a.next_attempt_at, a.lease_token, a.lease_expires_at,
                    a.started_at, a.completed_at, a.failure_code, a.terminal_reason,
                    j.state, j.deadline_at, j.max_attempts
                    FROM observatory_evaluation_attempts a JOIN observatory_evaluation_jobs j
                    ON j.job_id=a.job_id WHERE a.attempt_id=%s AND a.job_id=%s FOR UPDATE""",
                    (envelope["attemptId"], envelope["jobId"]),
                )
                row = await attempt_result.fetchone()
                if row is None:
                    raise OrchestrationError("unknown_attempt")
                attempt = _attempt_from_row(row[:12])
                job_result = await connection.execute(
                    """SELECT job_id, request_id, idempotency_key, request_version,
                    dataset_id, dataset_version, suite_id, suite_version,
                    evaluator_id, evaluator_version, capture_classification,
                    deadline_at, max_attempts, envelope_json, state, attempt_count,
                    created_at, updated_at, request_fingerprint
                    FROM observatory_evaluation_jobs WHERE job_id=%s FOR UPDATE""",
                    (envelope["jobId"],),
                )
                job_row = await job_result.fetchone()
                if job_row is None:
                    raise OrchestrationError("unknown_job")
                job = _job_from_full_row(job_row)
                _validate_result_binding(job, attempt, envelope)
                job_state = job.state.value
                late = received_at >= job.admission.deadline_at
                settled_deadline = False
                if late and job_state not in {"succeeded", "failed", "cancelled", "deadline"}:
                    await connection.execute(
                        "UPDATE observatory_evaluation_attempts SET state='deadline', "
                        "completed_at=%s, terminal_reason='DEADLINE_EXCEEDED', "
                        "lease_token=NULL, lease_expires_at=NULL, updated_at=%s "
                        "WHERE attempt_id=%s AND state IN ('queued','running')",
                        (received_at, received_at, attempt.attempt_id),
                    )
                    await connection.execute(
                        "UPDATE observatory_evaluation_jobs SET state='deadline', "
                        "updated_at=%s WHERE job_id=%s AND state IN ('queued','running')",
                        (received_at, envelope["jobId"]),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_lifecycle_audits(
                        audit_id,job_id,attempt_id,event_type,outcome,occurred_at)
                        VALUES (%s,%s,%s,'evaluation.deadline','accepted',%s)
                        ON CONFLICT DO NOTHING""",
                        (str(uuid4()), envelope["jobId"], envelope["attemptId"], received_at),
                    )
                    job_state = "deadline"
                    settled_deadline = True
                disposition = (
                    "out_of_order"
                    if late
                    or job_state in {state for state in TERMINAL_STATES}
                    or attempt.state in {
                        AttemptState.SUCCEEDED,
                        AttemptState.FAILED,
                        AttemptState.CANCELLED,
                        AttemptState.DEADLINE,
                    }
                    else "applied"
                )
                inserted_receipt = await connection.execute(
                    """INSERT INTO observatory_evaluation_result_receipts(
                    result_id,message_id,job_id,attempt_id,request_digest,result_digest,
                    status,disposition,envelope_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (message_id) DO NOTHING RETURNING message_id""",
                    (
                        result_id,
                        message_id,
                        envelope["jobId"],
                        envelope["attemptId"],
                        envelope["requestDigest"],
                        result_digest,
                        status,
                        disposition,
                        json.dumps(dict(envelope), sort_keys=True),
                    ),
                )
                if await inserted_receipt.fetchone() is None:
                    return ResultApplication("duplicate")
                target_attempt: str | None = None
                if disposition == "applied" and status == "failed":
                    failure_code = str(data["failureCode"])
                    retryable = data["retryable"] is True
                    if (
                        retryable
                        and failure_code in RETRYABLE_FAILURES
                        and attempt.attempt_number < job.admission.max_attempts
                        and received_at < job.admission.deadline_at
                    ):
                        delay = min(
                            self._retry_delay(attempt.attempt_number),
                            max(
                                1,
                                int((job.admission.deadline_at - received_at).total_seconds()),
                            ),
                        )
                        next_at = received_at + timedelta(seconds=delay)
                        next_number = attempt.attempt_number + 1
                        next_attempt_id = _deterministic_attempt_id(job.job_id, next_number)
                        next_attempt = EvaluationAttempt(
                            next_attempt_id,
                            job.job_id,
                            next_number,
                            job.admission.request_fingerprint or job.admission.fingerprint(),
                            next_attempt_at=next_at,
                        )
                        await connection.execute(
                            """UPDATE observatory_evaluation_attempts SET state='failed',
                            completed_at=%s, failure_code=%s, terminal_reason='RETRY_SCHEDULED',
                            lease_token=NULL, lease_expires_at=NULL, next_attempt_at=%s,
                            updated_at=%s WHERE attempt_id=%s AND state IN ('queued','running')""",
                            (received_at, failure_code, next_at, received_at, attempt.attempt_id),
                        )
                        await connection.execute(
                            """INSERT INTO observatory_evaluation_attempts(
                            attempt_id, job_id, attempt_number, request_id, request_digest,
                            next_attempt_at) VALUES (%s,%s,%s,%s,%s,%s)""",
                            (
                                next_attempt_id,
                                job.job_id,
                                next_number,
                                job.admission.request_id,
                                next_attempt.request_digest,
                                next_at,
                            ),
                        )
                        next_envelope = _runner_envelope(job, next_attempt, received_at)
                        await connection.execute(
                            """INSERT INTO observatory_evaluation_outbox(
                            event_id,event_type,job_id,attempt_id,envelope_json,next_publish_at)
                            VALUES (%s,'evaluation.runner.requested',%s,%s,%s::jsonb,%s)
                            RETURNING event_id""",
                            (
                                next_envelope["messageId"],
                                job.job_id,
                                next_attempt_id,
                                json.dumps(next_envelope, sort_keys=True),
                                next_at,
                            ),
                        )
                        await connection.execute(
                            """UPDATE observatory_evaluation_jobs SET state='queued',
                            attempt_count=%s, updated_at=%s WHERE job_id=%s
                            AND state IN ('queued','running')""",
                            (next_number, received_at, job.job_id),
                        )
                        await connection.execute(
                            """INSERT INTO observatory_evaluation_lifecycle_audits(
                            audit_id,job_id,attempt_id,event_type,outcome,failure_code,occurred_at)
                            VALUES (%s,%s,%s,'evaluation.retry_scheduled','accepted',%s,%s)""",
                            (str(uuid4()), job.job_id, next_attempt_id, failure_code, received_at),
                        )
                        return ResultApplication(disposition)
                if disposition == "applied":
                    target_attempt = (
                        "succeeded"
                        if status == "completed"
                        else "cancelled"
                        if status == "cancelled"
                        else "failed"
                    )
                    await connection.execute(
                        "UPDATE observatory_evaluation_attempts SET state=%s, "
                        "completed_at=%s,failure_code=%s,updated_at=%s "
                        "WHERE attempt_id=%s AND state IN ('queued','running')",
                        (
                            target_attempt,
                            received_at,
                            data.get("failureCode"),
                            received_at,
                            envelope["attemptId"],
                        ),
                    )
                    await connection.execute(
                        "UPDATE observatory_evaluation_jobs SET state=%s, "
                        "attempt_count=GREATEST(attempt_count,%s),updated_at=%s "
                        "WHERE job_id=%s AND state IN ('queued','running')",
                        (target_attempt, attempt.attempt_number, received_at, envelope["jobId"]),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_lifecycle_audits(
                        audit_id,job_id,attempt_id,event_type,outcome,failure_code,occurred_at)
                        VALUES (%s,%s,%s,%s,'accepted',%s,%s)
                        ON CONFLICT DO NOTHING""",
                        (
                            str(uuid4()),
                            envelope["jobId"],
                            envelope["attemptId"],
                            f"evaluation.{status}",
                            data.get("failureCode") if status == "failed" else None,
                            received_at,
                        ),
                    )
                return ResultApplication(
                    disposition, "deadline" if settled_deadline else (
                        target_attempt if disposition == "applied" else None
                    )
                )

    async def recover(self, *, now: datetime, limit: int, worker_id: str) -> int:
        del worker_id
        await self.settle_expired(now=now, limit=limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """WITH expired AS (SELECT attempt_id FROM observatory_evaluation_attempts
                WHERE state='running' AND lease_expires_at < %s ORDER BY lease_expires_at
                LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE observatory_evaluation_attempts a SET state='queued', lease_token=NULL,
                lease_expires_at=NULL, next_attempt_at=%s, updated_at=%s
                FROM expired WHERE a.attempt_id=expired.attempt_id""",
                (now, limit, now, now),
            )
        return int(getattr(result, "rowcount", 0))

    async def settle_expired(self, *, now: datetime, limit: int) -> int:
        """Atomically settle queued/running work whose UTC deadline elapsed."""

        async with self._connection_factory() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """SELECT a.attempt_id, a.job_id FROM observatory_evaluation_attempts a
                    JOIN observatory_evaluation_jobs j ON j.job_id = a.job_id
                    WHERE j.deadline_at <= %s AND j.state IN ('queued','running')
                    AND a.state IN ('queued','running')
                    ORDER BY j.deadline_at, a.attempt_id LIMIT %s
                    FOR UPDATE OF a, j SKIP LOCKED""",
                    (now, limit),
                )
                rows = await result.fetchall()
                for attempt_id, job_id in rows:
                    await connection.execute(
                        """UPDATE observatory_evaluation_attempts SET state='deadline',
                        completed_at=%s, terminal_reason='DEADLINE_EXCEEDED',
                        lease_token=NULL, lease_expires_at=NULL, updated_at=%s
                        WHERE attempt_id=%s AND state IN ('queued','running')""",
                        (now, now, attempt_id),
                    )
                    await connection.execute(
                        """UPDATE observatory_evaluation_jobs SET state='deadline', updated_at=%s
                        WHERE job_id=%s AND state IN ('queued','running')""",
                        (now, job_id),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_lifecycle_audits(
                        audit_id, job_id, attempt_id, event_type, outcome, failure_code,
                        occurred_at) VALUES (%s,%s,%s,'evaluation.deadline','accepted',
                        'deadline_exceeded',%s)""",
                        (str(uuid4()), job_id, attempt_id, now),
                    )
        return len(rows)

    async def heartbeat(
        self, *, component_id: str, lease_token: str, at: datetime, backlog_count: int = 0
    ) -> Mapping[str, int]:
        async with self._connection_factory() as connection:
            backlog_result = await connection.execute(
                """SELECT COUNT(*) FROM observatory_evaluation_attempts
                WHERE state IN ('queued','running')"""
            )
            outbox_result = await connection.execute(
                """SELECT COUNT(*), COALESCE(EXTRACT(EPOCH FROM
                (CURRENT_TIMESTAMP - MIN(created_at))), 0)
                FROM observatory_evaluation_outbox WHERE published_at IS NULL"""
            )
            lag_result = await connection.execute(
                """SELECT COALESCE(MAX(EXTRACT(EPOCH FROM (received_at -
                (envelope_json->'data'->>'completedAt')::timestamptz))), 0)
                FROM observatory_evaluation_result_receipts
                WHERE status IN ('completed','cancelled','failed')"""
            )
            backlog_row = await backlog_result.fetchone()
            outbox_row = await outbox_result.fetchone()
            lag_row = await lag_result.fetchone()
            durable_backlog = max(0, int(backlog_row[0] if backlog_row else backlog_count))
            outbox_pending = max(0, int(outbox_row[0] if outbox_row else 0))
            oldest_age = max(0, int(float(outbox_row[1] if outbox_row else 0)))
            result_lag = max(0, int(float(lag_row[0] if lag_row else 0)))
            await connection.execute(
                """INSERT INTO observatory_evaluation_worker_leases(component_id,lease_token,
                lease_expires_at,heartbeat_at,backlog_count,outbox_pending_count,result_lag_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(component_id) DO UPDATE SET lease_token=EXCLUDED.lease_token,
                lease_expires_at=EXCLUDED.lease_expires_at,heartbeat_at=EXCLUDED.heartbeat_at,
                backlog_count=EXCLUDED.backlog_count,outbox_pending_count=EXCLUDED.outbox_pending_count,
                result_lag_count=EXCLUDED.result_lag_count""",
                (
                    component_id,
                    lease_token,
                    at + timedelta(seconds=120),
                    at,
                    durable_backlog,
                    outbox_pending,
                    result_lag,
                ),
            )
        return {
            "backlog_count": durable_backlog,
            "outbox_pending_count": outbox_pending,
            "outbox_oldest_age_seconds": oldest_age,
            "result_lag_seconds": result_lag,
        }

    async def claim_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> Sequence[EvaluationSchedule]:
        if not lease_token:
            raise OrchestrationError("schedule_lease_invalid")
        lease_expires = now + timedelta(seconds=120)
        async with self._connection_factory() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """WITH due AS (
                    SELECT schedule_id FROM observatory_evaluation_schedules
                    WHERE enabled AND next_run_at <= %s
                    AND (lease_expires_at IS NULL OR lease_expires_at < %s)
                    ORDER BY next_run_at,schedule_id LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    )
                    UPDATE observatory_evaluation_schedules s SET
                    lease_token=%s, lease_expires_at=%s, last_run_at=%s,
                    next_run_at=s.next_run_at + (s.cadence_seconds * INTERVAL '1 second'),
                    updated_at=%s FROM due WHERE s.schedule_id=due.schedule_id
                    RETURNING s.schedule_id,s.occurrence_id,s.dataset_id,s.dataset_version,
                    s.suite_id,s.suite_version,s.evaluator_id,s.evaluator_version,
                    s.next_run_at - (s.cadence_seconds * INTERVAL '1 second'),
                    s.cadence_seconds,s.deadline_seconds,s.max_attempts,s.enabled,
                    s.request_envelope_json""",
                    (now, now, limit, lease_token, lease_expires, now, now),
                )
                rows = await result.fetchall()
        return [_schedule_from_row(row) for row in rows]

    async def materialize_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> int:
        if not lease_token:
            raise OrchestrationError("schedule_lease_invalid")
        materialized = 0
        async with self._connection_factory() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """SELECT schedule_id, occurrence_id, dataset_id, dataset_version,
                    suite_id, suite_version, evaluator_id, evaluator_version,
                    next_run_at, cadence_seconds, deadline_seconds, max_attempts, enabled,
                    request_envelope_json
                    FROM observatory_evaluation_schedules
                    WHERE enabled AND next_run_at <= %s
                    AND (lease_expires_at IS NULL OR lease_expires_at < %s)
                    ORDER BY next_run_at, schedule_id LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (now, now, limit),
                )
                rows = await result.fetchall()
                for row in rows:
                    schedule = _schedule_from_row(row)
                    admission = _scheduled_admission(schedule, schedule.next_run_at)
                    job_id = _deterministic_job_id(admission.idempotency_key)
                    attempt_id = _deterministic_attempt_id(job_id, 1)
                    job = EvaluationJob(
                        job_id, admission, created_at=schedule.next_run_at, updated_at=now
                    )
                    attempt = EvaluationAttempt(
                        attempt_id,
                        job_id,
                        1,
                        admission.fingerprint(),
                        next_attempt_at=schedule.next_run_at,
                    )
                    envelope = _runner_envelope(job, attempt, schedule.next_run_at)
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_jobs(
                        job_id, request_id, idempotency_key, request_fingerprint,
                        request_version, dataset_id, dataset_version, suite_id,
                        suite_version, evaluator_id, evaluator_version,
                        capture_classification, deadline_at, max_attempts, attempt_count,
                        envelope_json, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s::jsonb,%s,%s)
                        ON CONFLICT (idempotency_key) DO NOTHING""",
                        (
                            job_id,
                            admission.request_id,
                            admission.idempotency_key,
                            admission.fingerprint(),
                            admission.request_version,
                            admission.dataset_id,
                            admission.dataset_version,
                            admission.suite_id,
                            admission.suite_version,
                            admission.evaluator_id,
                            admission.evaluator_version,
                            admission.capture_classification,
                            admission.deadline_at,
                            admission.max_attempts,
                            json.dumps(admission.envelope, sort_keys=True),
                            schedule.next_run_at,
                            now,
                        ),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_attempts(
                        attempt_id, job_id, attempt_number, request_id, request_digest,
                        next_attempt_at) VALUES (%s,%s,1,%s,%s,%s)
                        ON CONFLICT (job_id, attempt_number) DO NOTHING""",
                        (
                            attempt_id,
                            job_id,
                            admission.request_id,
                            attempt.request_digest,
                            schedule.next_run_at,
                        ),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_outbox(
                        event_id,event_type,job_id,attempt_id,envelope_json,next_publish_at)
                        VALUES (%s,'evaluation.runner.requested',%s,%s,%s::jsonb,%s)
                        ON CONFLICT (event_id) DO NOTHING""",
                        (
                            envelope["messageId"],
                            job_id,
                            attempt_id,
                            json.dumps(envelope, sort_keys=True),
                            schedule.next_run_at,
                        ),
                    )
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_lifecycle_audits(
                        audit_id, job_id, attempt_id, event_type, outcome, occurred_at)
                        VALUES (%s,%s,%s,'evaluation.schedule_materialized','accepted',%s)
                        ON CONFLICT (audit_id) DO NOTHING""",
                        (str(uuid4()), job_id, attempt_id, now),
                    )
                    next_at = schedule.next_run_at + timedelta(seconds=schedule.cadence_seconds)
                    next_occurrence = _deterministic_uuid(
                        "aura-observatory-schedule-occurrence",
                        f"{schedule.schedule_id}:{_timestamp(next_at)}",
                    )
                    await connection.execute(
                        """UPDATE observatory_evaluation_schedules SET occurrence_id=%s,
                        next_run_at=%s, last_run_at=%s, lease_token=NULL, lease_expires_at=NULL,
                        updated_at=%s WHERE schedule_id=%s""",
                        (next_occurrence, next_at, schedule.next_run_at, now, schedule.schedule_id),
                    )
                    materialized += 1
        return materialized


def _job_from_row(row: Any, request: EvaluationAdmission) -> EvaluationJob:
    return EvaluationJob(str(row[0]), request, JobState(str(row[3])), int(row[4]), row[5], row[6])


def _job_from_full_row(row: Any) -> EvaluationJob:
    envelope = cast(Mapping[str, object], row[13] if isinstance(row[13], Mapping) else {})
    request = EvaluationAdmission(
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        str(row[6]),
        str(row[7]),
        str(row[8]),
        str(row[9]),
        str(row[10]),
        row[11],
        int(row[12]),
        envelope,
        str(row[18]),
        row[16],
    )
    return EvaluationJob(
        str(row[0]), request, JobState(str(row[14])), int(row[15]), row[16], row[17]
    )


def _attempt_from_row(row: Any) -> EvaluationAttempt:
    return EvaluationAttempt(
        str(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        AttemptState(str(row[4])),
        row[5],
        row[6],
        row[7],
        row[8],
        row[9],
        row[10],
        row[11],
    )


def _outbox_from_row(row: Any) -> EvaluationOutboxEntry:
    envelope = cast(Mapping[str, object], row[4] if isinstance(row[4], Mapping) else {})
    return EvaluationOutboxEntry(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        envelope,
        row[5],
        int(row[6]),
        row[7],
        row[9] if len(row) > 9 else None,
        row[10] if len(row) > 10 else None,
        row[8] if len(row) > 8 and row[8] is not None else datetime.now(UTC),
    )


def _schedule_from_row(row: Any) -> EvaluationSchedule:
    return EvaluationSchedule(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        str(row[6]),
        str(row[7]),
        row[8],
        int(row[9]),
        int(row[10]),
        int(row[11]),
        bool(row[12]),
        cast(Mapping[str, object], row[13]) if isinstance(row[13], Mapping) else None,
    )


__all__ = [
    "ComponentSpanTransport",
    "OtlpComponentSpanExporter",
    "PostgresEvaluationMetricStore",
    "PostgresEvaluationOrchestrationRepository",
]
