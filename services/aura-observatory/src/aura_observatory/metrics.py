"""Bounded metadata-only ingest counters."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)

COMPONENT_SPAN_COMPONENTS = frozenset(
    {
        "aura.observatory.evaluation_orchestration",
        "aura.observatory.evaluation_scheduler",
    }
)
COMPONENT_SPAN_OPERATIONS = frozenset(
    {"dispatch", "publish", "result_apply", "recover", "schedule_materialize", "heartbeat"}
)
COMPONENT_SPAN_DEPENDENCIES = frozenset({"postgresql", "evaluation_nats"})
COMPONENT_SPAN_OUTCOMES = frozenset({"success", "failure", "duplicate", "timeout", "unavailable"})
COMPONENT_SPAN_ERRORS = frozenset(
    {"database_failure", "transport_unavailable", "timeout", "validation"}
)
MAX_LAG_MS = 604_800_000.0
MAX_LATENCY_MS = 600_000.0
HEALTH_HISTORY_LIMIT = 1000
REJECTION_REASONS = frozenset(
    {
        "encoding",
        "json",
        "unsupported_fields",
        "dropped_nonzero",
        "status_code",
        "status_message",
        "shape_object",
        "shape_array",
        "shape_trace_id",
        "shape_span_id",
        "shape_parent_span_id",
        "shape_link_trace_id",
        "shape_link_span_id",
        "shape_attribute_key",
        "shape_attribute_value",
        "scope",
        "link",
        "event",
        "attribute",
        "shape",
        "contract_value",
        "payload_too_large",
        "schema_validation",
        "malformed",
    }
)

# Request/envelope and OTLP contract failures are malformed input, not
# rejected-but-well-formed observations.  Keep this finite so durable
# counters cannot be confused by arbitrary caller-controlled reason strings.
MALFORMED_REASONS = frozenset(REJECTION_REASONS - {"schema_validation"})


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DELAYED = "delayed"
    PARTIAL = "partial"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def bounded_lag_ms(value: float | int | None) -> float | None:
    """Return a finite, non-negative lag bounded to the public contract."""

    if value is None:
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    if not math.isfinite(number):
        return None
    return min(MAX_LAG_MS, max(0.0, number))


def bounded_latency_ms(value: float | int | None) -> float | None:
    """Return a finite latency bounded to ten minutes."""

    if value is None:
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    if not math.isfinite(number):
        return None
    return min(MAX_LATENCY_MS, max(0.0, number))


@dataclass(frozen=True, slots=True)
class LatencySummary:
    sample_count: int
    latest_ms: float | None
    p50_ms: float | None
    p95_ms: float | None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "sampleCount": self.sample_count,
            "latestMs": self.latest_ms,
            "p50Ms": self.p50_ms,
            "p95Ms": self.p95_ms,
        }


class BoundedLatencyWindow:
    """Small deterministic latency reservoir; it never stores request data."""

    def __init__(self, max_samples: int = 128) -> None:
        self._values: deque[float] = deque(maxlen=max_samples)

    def add(self, value: float | int | None) -> None:
        bounded = bounded_latency_ms(value)
        if bounded is not None:
            self._values.append(bounded)

    def summary(self) -> LatencySummary:
        if not self._values:
            return LatencySummary(0, None, None, None)
        # Keep insertion order for the latest retained observation. Percentiles
        # deliberately operate on a separate sorted copy so a low-latency
        # observation arriving after a high one is not misreported as stale.
        values = sorted(self._values)

        def percentile(rank: float) -> float:
            position = (len(values) - 1) * rank
            lower = int(position)
            upper = min(len(values) - 1, lower + 1)
            fraction = position - lower
            return round(values[lower] + (values[upper] - values[lower]) * fraction, 3)

        return LatencySummary(len(values), self._values[-1], percentile(0.50), percentile(0.95))


@dataclass(frozen=True, slots=True)
class ComponentSpan:
    """Bounded metadata-only operation span; never contains lifecycle IDs."""

    component_id: str
    component_version: str
    deployment_id: str
    operation: str
    duration_ms: float
    outcome: str
    dependency: str | None = None
    error_code: str | None = None
    trace_id: str = ""
    span_id: str = ""


class ComponentSpanExporter(Protocol):
    """Inward port for bounded metadata-only component span export."""

    def submit(self, span: ComponentSpan) -> None: ...

    async def flush(self, *, timeout_seconds: float = 5.0) -> None: ...


@dataclass(slots=True)
class HealthSnapshot:
    """Content-free operational snapshot shared by REST and SSE."""

    snapshot_id: str
    observed_at: datetime
    state: HealthState
    intake_lag_ms: float | None
    visibility_watermark: int
    visibility_lag_count: int
    rejection_reasons: dict[str, int]
    duplicate_count: int
    storage_latency: LatencySummary
    query_latency: LatencySummary
    query_freshness_lag_ms: float | None
    worker_heartbeat_at: datetime | None
    worker_heartbeat_age_ms: float | None
    backlog_count: int
    oldest_backlog_age_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id,
            "observedAt": _iso(self.observed_at),
            "state": self.state.value,
            "intakeLagMs": self.intake_lag_ms,
            "visibilityWatermark": max(0, int(self.visibility_watermark)),
            "visibilityLagCount": max(0, int(self.visibility_lag_count)),
            "rejectionReasons": [
                {"reason": reason, "count": max(0, int(count))}
                for reason, count in sorted(self.rejection_reasons.items())
                if reason in REJECTION_REASONS and count > 0
            ][:24],
            "duplicateCount": max(0, int(self.duplicate_count)),
            "storageLatency": self.storage_latency.as_dict(),
            "queryLatency": self.query_latency.as_dict(),
            "queryFreshnessLagMs": self.query_freshness_lag_ms,
            "workerHeartbeatAt": _iso(self.worker_heartbeat_at),
            "workerHeartbeatAgeMs": self.worker_heartbeat_age_ms,
            "backlogCount": max(0, int(self.backlog_count)),
            "oldestBacklogAgeMs": self.oldest_backlog_age_ms,
        }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


class MetricLabels(tuple[tuple[str, str], ...]):
    """Tuple-compatible labels with a bounded mapping accessor."""

    def get(self, key: str, default: str | None = None) -> str | None:
        return dict(self).get(key, default)


@dataclass(slots=True)
class IngestCounters:
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    malformed: int = 0
    storage_failure: int = 0
    duration_ms_total: float = 0.0

    def record_duration(self, started: float) -> float:
        """Record and return one batch duration, never a cumulative value."""

        duration_ms = max(0.0, (monotonic() - started) * 1000.0)
        self.duration_ms_total += duration_ms
        return duration_ms

    def as_dict(self) -> dict[str, int | float]:
        return {
            "telemetry_ingest_accepted_count": self.accepted,
            "telemetry_ingest_rejected_count": self.rejected,
            "telemetry_ingest_duplicate_count": self.duplicate,
            "telemetry_ingest_malformed_count": self.malformed,
            "telemetry_ingest_storage_failure_count": self.storage_failure,
            "telemetry_ingest_duration_ms": self.duration_ms_total,
        }


class IngestHealth:
    """Bounded in-process health accumulator for one Observatory process."""

    def __init__(self) -> None:
        self.rejection_reasons: dict[str, int] = defaultdict(int)
        self.storage_latency = BoundedLatencyWindow()
        self.query_latency = BoundedLatencyWindow()
        self.duplicate_count = 0
        self.visibility_watermark = 0
        self.visibility_lag_count = 0
        self.backlog_count = 0
        self.oldest_backlog_age_ms: float | None = None
        self.last_observed_at: datetime | None = None
        self.last_accepted_at: datetime | None = None
        self.last_rejection_at: datetime | None = None
        self.last_duplicate_at: datetime | None = None
        self.last_storage_failure_at: datetime | None = None
        self.worker_heartbeat_at: datetime | None = None
        self.query_freshness_lag_ms: float | None = None
        self._last_snapshot: HealthSnapshot | None = None

    def rejection(self, reason: str, amount: int = 1) -> None:
        self.rejection_reasons[reason if reason in REJECTION_REASONS else "malformed"] += max(
            0, amount
        )
        if amount > 0:
            self.last_rejection_at = datetime.now(UTC)

    def accepted(self, observed_at: datetime, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        normalized = observed_at.astimezone(UTC)
        self.last_observed_at = max(self.last_observed_at or normalized, normalized)
        self.last_accepted_at = current

    def duplicate(self) -> None:
        self.duplicate_count += 1
        self.last_duplicate_at = datetime.now(UTC)

    def storage_failure(self, amount: int = 1) -> None:
        if amount > 0:
            self.last_storage_failure_at = datetime.now(UTC)

    def heartbeat(self, at: datetime | None = None) -> None:
        self.worker_heartbeat_at = (at or datetime.now(UTC)).astimezone(UTC)

    def visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | int | None = None,
    ) -> None:
        self.visibility_watermark = max(0, int(watermark))
        self.visibility_lag_count = max(0, int(lag_count))
        self.backlog_count = max(0, int(backlog_count))
        self.oldest_backlog_age_ms = bounded_lag_ms(oldest_backlog_age_ms)

    def snapshot(self, *, now: datetime | None = None) -> HealthSnapshot:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        intake_lag = (
            bounded_lag_ms((current - self.last_observed_at).total_seconds() * 1000)
            if self.last_observed_at is not None
            else None
        )
        heartbeat_age = (
            bounded_lag_ms((current - self.worker_heartbeat_at).total_seconds() * 1000)
            if self.worker_heartbeat_at is not None
            else None
        )
        recent_failure = any(
            timestamp is not None
            and (current - timestamp.astimezone(UTC)).total_seconds() * 1000 <= 120_000
            for timestamp in (
                self.last_rejection_at,
                self.last_duplicate_at,
                self.last_storage_failure_at,
            )
        )
        state = HealthState.HEALTHY
        if self.worker_heartbeat_at is None or (
            heartbeat_age is not None and heartbeat_age > 120_000
        ):
            state = HealthState.UNAVAILABLE
        elif self.visibility_lag_count > 0 or self.backlog_count > 0:
            state = HealthState.PARTIAL
        elif intake_lag is not None and intake_lag > 120_000:
            state = HealthState.DELAYED
        elif recent_failure:
            state = HealthState.DEGRADED
        snapshot = HealthSnapshot(
            snapshot_id=str(uuid4()),
            observed_at=current,
            state=state,
            intake_lag_ms=intake_lag,
            visibility_watermark=self.visibility_watermark,
            visibility_lag_count=self.visibility_lag_count,
            rejection_reasons=dict(self.rejection_reasons),
            duplicate_count=self.duplicate_count,
            storage_latency=self.storage_latency.summary(),
            query_latency=self.query_latency.summary(),
            query_freshness_lag_ms=bounded_lag_ms(self.query_freshness_lag_ms),
            worker_heartbeat_at=self.worker_heartbeat_at,
            worker_heartbeat_age_ms=heartbeat_age,
            backlog_count=self.backlog_count,
            oldest_backlog_age_ms=self.oldest_backlog_age_ms,
        )
        self._last_snapshot = snapshot
        return snapshot

    @property
    def last_snapshot(self) -> HealthSnapshot | None:
        return self._last_snapshot


class ObservatoryMetrics:
    """Content-free metric sink with bounded, allowlisted labels."""

    HISTOGRAMS = frozenset(
        {
            "observatory_query_duration_ms",
            "observatory_comparison_duration_ms",
            "observatory_auth_callback_duration_ms",
            "observatory_ingest_storage_duration_ms",
            "observatory_evaluation_artifact_operation_duration_ms",
            "observatory_evaluation_deletion_duration_ms",
            "observatory_evaluation_result_lag_seconds",
            "observatory_evaluation_operation_duration_ms",
        }
    )

    ALLOWED = {
        "observatory_ingest_intake_lag_ms",
        "observatory_ingest_visibility_lag_count",
        "observatory_ingest_rejection_count",
        "observatory_ingest_duplicate_count",
        "observatory_ingest_storage_duration_ms",
        "observatory_ingest_worker_heartbeat_age_ms",
        "observatory_ingest_backlog_count",
        "observatory_query_freshness_lag_ms",
        "observatory_query_duration_ms",
        "observatory_comparison_duration_ms",
        "observatory_query_outcome",
        "observatory_comparison_outcome",
        "observatory_comparison_partial_count",
        "observatory_comparison_unavailable_marker_count",
        "observatory_sse_connection_count",
        "observatory_sse_active",
        "observatory_sse_replay_count",
        "observatory_retention_action_count",
        "observatory_retention_deleted_count",
        "observatory_retention_failure_count",
        "observatory_auth_callback_duration_ms",
        "observatory_auth_callback_outcome",
        "observatory_evaluation_artifact_operation_count",
        "observatory_evaluation_artifact_operation_duration_ms",
        "observatory_evaluation_deletion_action_count",
        "observatory_evaluation_deletion_deleted_count",
        "observatory_evaluation_deletion_failure_count",
        "observatory_evaluation_deletion_duration_ms",
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
    LABELS: dict[str, set[str]] = {
        "observatory_ingest_intake_lag_ms": set(),
        "observatory_ingest_visibility_lag_count": set(),
        "observatory_ingest_rejection_count": {"reason"},
        "observatory_ingest_duplicate_count": set(),
        "observatory_ingest_storage_duration_ms": set(),
        "observatory_ingest_worker_heartbeat_age_ms": set(),
        "observatory_ingest_backlog_count": set(),
        "observatory_query_freshness_lag_ms": set(),
        "observatory_query_duration_ms": {"resource"},
        "observatory_query_outcome": {"resource", "outcome"},
        "observatory_comparison_duration_ms": set(),
        "observatory_comparison_outcome": {"outcome"},
        "observatory_comparison_partial_count": set(),
        "observatory_comparison_unavailable_marker_count": set(),
        "observatory_sse_connection_count": {"event_type"},
        "observatory_sse_active": {"event_type"},
        "observatory_sse_replay_count": {"event_type"},
        "observatory_retention_action_count": {"action", "status"},
        "observatory_retention_deleted_count": {"action"},
        "observatory_retention_failure_count": {"action", "failure_code"},
        "observatory_auth_callback_duration_ms": {"stage", "outcome"},
        "observatory_auth_callback_outcome": {"stage", "outcome"},
        "observatory_evaluation_artifact_operation_count": {"operation", "outcome"},
        "observatory_evaluation_artifact_operation_duration_ms": {"operation"},
        "observatory_evaluation_deletion_action_count": {"selector", "phase", "outcome"},
        "observatory_evaluation_deletion_deleted_count": {"selector"},
        "observatory_evaluation_deletion_failure_count": {"phase", "failure_code"},
        "observatory_evaluation_deletion_duration_ms": {"selector", "outcome"},
        "observatory_evaluation_job_admission_count": {"outcome"},
        "observatory_evaluation_attempt_transition_count": {"outcome"},
        "observatory_evaluation_retry_scheduled_count": {"outcome"},
        "observatory_evaluation_terminal_count": {"outcome"},
        "observatory_evaluation_dependency_outcome_count": {"operation", "outcome"},
        "observatory_evaluation_cancellation_count": {"outcome"},
        "observatory_evaluation_deadline_count": {"outcome"},
        "observatory_evaluation_outbox_pending_count": set(),
        "observatory_evaluation_backlog_count": set(),
        "observatory_evaluation_outbox_oldest_age_seconds": set(),
        "observatory_evaluation_outbox_publication_count": {"outcome"},
        "observatory_evaluation_result_consumption_count": {"outcome"},
        "observatory_evaluation_worker_heartbeat_age_seconds": set(),
        "observatory_evaluation_scheduler_heartbeat_age_seconds": set(),
        "observatory_evaluation_scheduler_occurrence_count": {"outcome"},
        "observatory_evaluation_result_lag_seconds": set(),
        "observatory_evaluation_operation_duration_ms": {"operation"},
    }
    LABEL_VALUES = {
        "reason": set(REJECTION_REASONS),
        "resource": {
            "overview",
            "runs",
            "run_detail",
            "trace_detail",
            "components",
            "component_health",
            "deployments",
            "events",
            "health_history",
            "analytics",
            "run_comparison",
        },
        "outcome": {
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
            "success",
            "unavailable",
            "missing",
            "invalid",
            "denied",
            "code_missing",
            "exchange_failed",
            "rejected",
            "partial",
            "failure",
            "not_found",
            "execution_limit",
            "duplicate",
            "running",
            "accepted",
            "published",
            "applied",
            "out_of_order",
            "recovered",
            "materialized",
            "deadline",
        },
        "operation": {
            "store",
            "result_apply",
            "dispatch",
            "publish",
            "recover",
            "heartbeat",
            "schedule_materialize",
            "postgresql",
            "evaluation_nats",
        },
        "selector": {"job", "dataset", "suite", "cutoff", "purge_all"},
        "phase": {"started", "scores", "objects", "complete", "worker"},
        "event_type": {
            "stream",
            "connection",
            "observation.changed",
            "health.changed",
            "heartbeat",
        },
        "action": {"delete_before", "purge_all"},
        "status": {"queued", "running", "succeeded", "failed"},
        "failure_code": {
            "storage_failure",
            "storage_authentication",
            "storage_integrity",
            "storage_timeout",
            "database_failure",
            "target_invalid",
            "lease_lost",
            "conflict",
            "cancelled",
            "unknown",
        },
        "stage": {
            "callback",
            "login_state_store",
            "provider",
            "authorization",
            "session_store",
        },
    }
    DURATION_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
    _MAX_PERSISTENCE_TASKS = 256
    _MAX_PERSISTENCE_DIAGNOSTICS = 32

    def __init__(
        self,
        export_path: Path | None = None,
        persistence: Any | None = None,
        *,
        span_exporter: ComponentSpanExporter | None = None,
        deployment_id: str = "observatory",
    ) -> None:
        self.values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histogram_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(
            int
        )
        self._histogram_buckets: dict[tuple[str, tuple[tuple[str, str], ...]], dict[int, int]] = (
            defaultdict(lambda: defaultdict(int))
        )
        # File export is an explicit local/test seam only. Production
        # cross-process health histograms come from the Observatory-owned
        # PostgreSQL health port and are rendered as persisted series.
        self.export_path = export_path
        self.persistence = persistence
        self._persistence_tasks: set[asyncio.Task[Any]] = set()
        self._persistence_failures = 0
        self._persistence_drops = 0
        self._persistence_diagnostics: deque[str] = deque(maxlen=self._MAX_PERSISTENCE_DIAGNOSTICS)
        self._persistence_logged_codes: set[str] = set()
        self._external_write_failed = False
        self._component_spans: deque[ComponentSpan] = deque(maxlen=256)
        self.span_exporter = span_exporter
        self.deployment_id = deployment_id
        self._trace_id = uuid4().hex

    def record_component_span(
        self,
        *,
        component_id: str,
        component_version: str,
        deployment_id: str,
        operation: str,
        duration_ms: float,
        outcome: str,
        dependency: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record one finite, content-free span at the telemetry boundary."""

        if component_id not in COMPONENT_SPAN_COMPONENTS:
            raise ValueError("component span component is not allowlisted")
        if component_version != "1.0.0" or not deployment_id or len(deployment_id) > 64:
            raise ValueError("component span identity is invalid")
        if operation not in COMPONENT_SPAN_OPERATIONS:
            raise ValueError("component span operation is not allowlisted")
        if outcome not in COMPONENT_SPAN_OUTCOMES:
            raise ValueError("component span outcome is not allowlisted")
        if dependency is not None and dependency not in COMPONENT_SPAN_DEPENDENCIES:
            raise ValueError("component span dependency is not allowlisted")
        if error_code is not None and error_code not in COMPONENT_SPAN_ERRORS:
            raise ValueError("component span error is not allowlisted")
        duration = bounded_latency_ms(duration_ms)
        if duration is None:
            raise ValueError("component span duration is invalid")
        span = ComponentSpan(
            component_id=component_id,
            component_version=component_version,
            deployment_id=deployment_id,
            operation=operation,
            duration_ms=duration,
            outcome=outcome,
            dependency=dependency,
            error_code=error_code,
            trace_id=self._trace_id,
            span_id=uuid4().hex[:16],
        )
        self._component_spans.append(span)
        if self.span_exporter is not None:
            self.span_exporter.submit(span)
        self.record(
            "observatory_evaluation_operation_duration_ms",
            duration,
            {"operation": operation},
        )

    @property
    def component_spans(self) -> tuple[ComponentSpan, ...]:
        return tuple(self._component_spans)

    def record(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        if name not in self.ALLOWED:
            raise ValueError("unknown Observatory metric")
        supplied = dict(labels or {})
        self._validate_labels(name, supplied)
        if not math.isfinite(float(value)):
            raise ValueError("metric value must be finite")
        key = (name, MetricLabels(sorted((str(k), str(v)) for k, v in supplied.items())))
        self.values[key] += float(value)
        if name in self.HISTOGRAMS:
            self._histogram_counts[key] += 1
            for bucket in self.DURATION_BUCKETS:
                if value <= bucket:
                    self._histogram_buckets[key][bucket] += 1
        if self.export_path is not None:
            try:
                self._write_external(name, key[1], float(value))
            except OSError:
                # Keep the local series available for diagnostics if the
                # shared sink is temporarily unavailable.
                self._external_write_failed = True
        self._schedule_persistence(name, float(value), supplied)

    def _schedule_persistence(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str],
        *,
        replace: bool = False,
    ) -> None:
        setter = getattr(self.persistence, "set_metric", None) if replace else None
        use_setter = replace and callable(setter)
        recorder = setter if use_setter else getattr(self.persistence, "record_metric", None)
        if not callable(recorder):
            return
        self._reap_persistence_tasks()
        if len(self._persistence_tasks) >= self._MAX_PERSISTENCE_TASKS:
            self._record_persistence_issue("queue_full", dropped=True)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._record_persistence_issue("no_event_loop", dropped=True)
            return
        record_metric = cast(Callable[..., Coroutine[Any, Any, None]], recorder)
        task: asyncio.Task[None] = loop.create_task(
            record_metric(name, value, dict(labels))
            if use_setter
            else record_metric(name, value, dict(labels), histogram=name in self.HISTOGRAMS)
        )
        self._persistence_tasks.add(task)

    def _reap_persistence_tasks(self) -> None:
        """Collect completed persistence tasks without retaining task state."""

        completed = [task for task in self._persistence_tasks if task.done()]
        for task in completed:
            self._persistence_tasks.discard(task)
            self._classify_persistence_result(task)

    def _classify_persistence_result(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            self._record_persistence_issue("cancelled")
            return
        try:
            task.result()
        except Exception as exc:
            failure_code = getattr(exc, "failure_code", "database_failure")
            if failure_code not in {"database_failure", "storage_timeout", "storage_failure"}:
                failure_code = "database_failure"
            self._record_persistence_issue(failure_code)

    def _record_persistence_issue(self, code: str, *, dropped: bool = False) -> None:
        if dropped:
            self._persistence_drops += 1
            event = f"dropped:{code}"
        else:
            self._persistence_failures += 1
            event = f"failed:{code}"
        self._persistence_diagnostics.append(event)
        if code not in self._persistence_logged_codes:
            self._persistence_logged_codes.add(code)
            _LOGGER.warning("observatory metric persistence %s", event)

    @property
    def persistence_failures(self) -> int:
        return self._persistence_failures

    @property
    def persistence_drops(self) -> int:
        return self._persistence_drops

    @property
    def persistence_diagnostics(self) -> tuple[str, ...]:
        return tuple(self._persistence_diagnostics)

    async def flush(self, *, timeout_seconds: float = 5.0) -> None:
        """Drain managed persistence writes before a worker process exits.

        The timeout and task cap keep shutdown bounded.  Every completed or
        cancelled task is classified without exposing provider exception text.
        """

        bounded_timeout = min(30.0, max(0.1, float(timeout_seconds)))
        self._reap_persistence_tasks()
        if self._persistence_tasks:
            tasks = tuple(self._persistence_tasks)
            done, pending = await asyncio.wait(tasks, timeout=bounded_timeout)
            for task in done:
                self._persistence_tasks.discard(task)
                self._classify_persistence_result(task)
            if pending:
                for task in pending:
                    task.cancel()
                cancelled = await asyncio.gather(*pending, return_exceptions=True)
                for task, result in zip(pending, cancelled, strict=True):
                    self._persistence_tasks.discard(task)
                    if isinstance(result, BaseException):
                        self._record_persistence_issue("shutdown_timeout")
        if self.span_exporter is not None:
            await self.span_exporter.flush(timeout_seconds=bounded_timeout)

    async def drain(self, *, timeout_seconds: float = 5.0) -> None:
        """Compatibility alias for callers that use drain terminology."""

        await self.flush(timeout_seconds=timeout_seconds)

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set an authoritative gauge/current total without read inflation."""

        if name not in self.ALLOWED:
            raise ValueError("unknown Observatory metric")
        supplied = dict(labels or {})
        self._validate_labels(name, supplied)
        if not math.isfinite(float(value)):
            raise ValueError("metric value must be finite")
        key = (name, MetricLabels(sorted((str(k), str(v)) for k, v in supplied.items())))
        self.values[key] = float(value)
        if self.export_path is not None:
            try:
                self._write_external(name, key[1], float(value), replace=True)
            except OSError:
                self._external_write_failed = True
        self._schedule_persistence(name, float(value), supplied, replace=True)

    def _write_external(
        self,
        name: str,
        labels: tuple[tuple[str, str], ...],
        value: float,
        *,
        replace: bool = False,
    ) -> None:
        """Atomically update one bounded series in the shared sink."""

        assert self.export_path is not None
        self.export_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.export_path.with_name(self.export_path.name + ".lock")
        import fcntl

        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records: dict[str, dict[str, Any]] = {}
                try:
                    with self.export_path.open("r", encoding="utf-8") as stream:
                        loaded: object = json.load(stream)
                        if isinstance(loaded, dict):
                            loaded_dict = cast(dict[object, object], loaded)
                            records = {
                                str(key): cast(dict[str, Any], value)
                                for key, value in loaded_dict.items()
                                if isinstance(value, dict)
                            }
                except FileNotFoundError, json.JSONDecodeError:
                    pass
                series_key = json.dumps([name, list(labels)], separators=(",", ":"))
                current = records.get(series_key, {})
                current["name"] = name
                current["labels"] = dict(labels)
                current["value"] = value if replace else float(current.get("value", 0.0)) + value
                if name in self.HISTOGRAMS:
                    current["count"] = int(current.get("count", 0)) + 1
                    buckets = current.setdefault("buckets", {})
                    if isinstance(buckets, dict):
                        bucket_values = cast(dict[str, int], buckets)
                        for bucket in self.DURATION_BUCKETS:
                            if value <= bucket:
                                bucket_values[str(bucket)] = (
                                    int(bucket_values.get(str(bucket), 0)) + 1
                                )
                records[series_key] = current
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{self.export_path.name}.", dir=str(self.export_path.parent)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(records, stream, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.export_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, Any]:
        # Keep each bounded label set as an independent series.  A plain
        # name-keyed dict silently overwrote e.g. succeeded and failed query
        # outcomes and made the in-process snapshot disagree with /metrics.
        return {
            f"{name}|{','.join(f'{key}={label}' for key, label in labels)}": {
                "name": name,
                "labels": dict(labels),
                "value": value,
            }
            for (name, labels), value in self.values.items()
        }

    def prometheus(self, *, exclude_names: set[str] | None = None) -> str:
        """Render the bounded in-process sink for a Prometheus-compatible scrape."""

        lines: list[str] = []
        emitted_types: set[str] = set()
        histogram_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = dict(
            self._histogram_counts
        )
        histogram_buckets: dict[tuple[str, tuple[tuple[str, str], ...]], dict[int, int]] = {
            key: dict(value) for key, value in self._histogram_buckets.items()
        }
        values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = dict(self.values)
        if self.export_path is not None and not self._external_write_failed:
            external_values: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = (
                defaultdict(float)
            )
            histogram_counts = {}
            try:
                import fcntl

                lock_path = self.export_path.with_name(self.export_path.name + ".lock")
                with lock_path.open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                    try:
                        with self.export_path.open("r", encoding="utf-8") as stream:
                            loaded: object = json.load(stream)
                        raw_records = (
                            list(cast(dict[str, Any], loaded).values())
                            if isinstance(loaded, dict)
                            else []
                        )
                        for raw_record in raw_records:
                            try:
                                if not isinstance(raw_record, dict):
                                    continue
                                record = cast(dict[str, Any], raw_record)
                                name = str(record["name"])
                                labels = MetricLabels(
                                    sorted(
                                        (str(key), str(value))
                                        for key, value in dict(record.get("labels", {})).items()
                                    )
                                )
                                if name in self.ALLOWED and self._labels_valid(name, labels):
                                    value = float(record["value"])
                                    if not math.isfinite(value):
                                        continue
                                    external_values[(name, labels)] += float(record["value"])
                                    if name in self.HISTOGRAMS:
                                        histogram_counts[(name, labels)] = int(
                                            record.get("count", 0)
                                        )
                                        raw_buckets = record.get("buckets", {})
                                        if isinstance(raw_buckets, dict):
                                            bucket_values = cast(dict[str, object], raw_buckets)
                                            histogram_buckets[(name, labels)] = {
                                                int(bucket): int(count)
                                                for bucket, count in bucket_values.items()
                                                if bucket.isdigit()
                                                and isinstance(count, (int, float))
                                                and int(count) >= 0
                                            }
                            except ValueError, TypeError, KeyError, json.JSONDecodeError:
                                continue
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                external_values = defaultdict(float, self.values)
            values = dict(external_values)
        if exclude_names:
            values = {key: value for key, value in values.items() if key[0] not in exclude_names}
            histogram_counts = {
                key: count for key, count in histogram_counts.items() if key[0] not in exclude_names
            }
            histogram_buckets = {
                key: buckets
                for key, buckets in histogram_buckets.items()
                if key[0] not in exclude_names
            }
        for (name, labels), value in sorted(values.items()):
            metric_type = (
                "histogram"
                if name in self.HISTOGRAMS
                else "gauge"
                if name
                in {
                    "observatory_sse_active",
                    "observatory_retention_action_count",
                    "observatory_retention_failure_count",
                    "observatory_ingest_intake_lag_ms",
                    "observatory_ingest_visibility_lag_count",
                    "observatory_ingest_duplicate_count",
                    "observatory_ingest_worker_heartbeat_age_ms",
                    "observatory_ingest_backlog_count",
                    "observatory_query_freshness_lag_ms",
                    "observatory_evaluation_backlog_count",
                    "observatory_evaluation_outbox_pending_count",
                    "observatory_evaluation_outbox_oldest_age_seconds",
                    "observatory_evaluation_worker_heartbeat_age_seconds",
                    "observatory_evaluation_scheduler_heartbeat_age_seconds",
                }
                else "counter"
            )
            if name not in emitted_types:
                lines.append(f"# TYPE {name} {metric_type}")
                emitted_types.add(name)
            label_text = ""
            if labels:

                def escape(label: str) -> str:
                    return label.replace("\\", "\\\\").replace('"', '\\"')

                label_text = (
                    "{" + ",".join(f'{key}="{escape(str(label))}"' for key, label in labels) + "}"
                )
            if name in self.HISTOGRAMS:
                suffix = label_text
                lines.append(f"{name}_sum{suffix} {value:g}")
                lines.append(f"{name}_count{suffix} {histogram_counts.get((name, labels), 0)}")
                buckets = histogram_buckets.get((name, labels), {})
                for bucket in self.DURATION_BUCKETS:
                    bucket_labels = (
                        label_text[:-1] + ("," if label_text else "{") + f'le="{bucket}"}}'
                    )
                    lines.append(f"{name}_bucket{bucket_labels} {buckets.get(bucket, 0)}")
                inf_labels = label_text[:-1] + ("," if label_text else "{") + 'le="+Inf"}'
                lines.append(f"{name}_bucket{inf_labels} {histogram_counts.get((name, labels), 0)}")
            else:
                lines.append(f"{name}{label_text} {value:g}")
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def prometheus_for_series(cls, series: list[Mapping[str, object]]) -> str:
        """Render validated service-owned persisted series for API scraping."""

        metrics = cls()
        for item in series:
            name = item.get("name")
            value = item.get("value")
            labels = item.get("labels")
            if not isinstance(name, str) or not isinstance(value, (int, float)):
                continue
            if not isinstance(labels, Mapping):
                continue
            label_mapping = cast(Mapping[object, object], labels)
            if any(
                not isinstance(key, str) or not isinstance(label, str)
                for key, label in label_mapping.items()
            ):
                continue
            try:
                normalized_labels = {
                    cast(str, key): cast(str, label) for key, label in label_mapping.items()
                }
                if name in cls.HISTOGRAMS and (
                    isinstance(item.get("count"), (int, float))
                    or isinstance(item.get("buckets"), Mapping)
                ):
                    if not cls._labels_valid(name, normalized_labels):
                        continue
                    total = float(value)
                    raw_count = item.get("count", 0)
                    if not isinstance(raw_count, (int, float)) or not math.isfinite(
                        float(raw_count)
                    ):
                        continue
                    count = int(raw_count)
                    raw_buckets = item.get("buckets", {})
                    if (
                        not math.isfinite(total)
                        or count < 0
                        or not isinstance(raw_buckets, Mapping)
                    ):
                        continue
                    raw_bucket_mapping = cast(Mapping[object, object], raw_buckets)
                    buckets: dict[int, int] = {}
                    for bucket, bucket_count in raw_bucket_mapping.items():
                        bucket_text = str(bucket)
                        if (
                            bucket_text.isdigit()
                            and isinstance(bucket_count, (int, float))
                            and math.isfinite(float(bucket_count))
                            and int(bucket_count) >= 0
                        ):
                            buckets[int(bucket_text)] = int(bucket_count)
                    label_key = MetricLabels(sorted(normalized_labels.items()))
                    key = (name, label_key)
                    metrics.values[key] = total
                    metrics._histogram_counts[key] = count
                    metrics._histogram_buckets[key] = buckets
                    continue
                metrics.record(
                    name,
                    float(value),
                    normalized_labels,
                )
            except TypeError, ValueError:
                continue
        return metrics.prometheus()

    @classmethod
    def _labels_valid(
        cls,
        name: str,
        labels: Mapping[str, str] | tuple[tuple[str, str], ...],
    ) -> bool:
        normalized = dict(labels)
        allowed = cls.LABELS[name]
        if set(normalized) - allowed:
            return False
        return all(value in cls.LABEL_VALUES.get(key, set()) for key, value in normalized.items())

    @classmethod
    def labels_valid(cls, name: str, labels: Mapping[str, str]) -> bool:
        """Validate labels for service-owned persistence adapters."""

        return cls._labels_valid(name, labels)

    @classmethod
    def _validate_labels(cls, name: str, labels: Mapping[str, str]) -> None:
        if not cls._labels_valid(name, labels):
            raise ValueError("metric labels are not bounded")


__all__ = [
    "ComponentSpan",
    "ComponentSpanExporter",
    "HEALTH_HISTORY_LIMIT",
    "REJECTION_REASONS",
    "BoundedLatencyWindow",
    "HealthSnapshot",
    "HealthState",
    "IngestCounters",
    "IngestHealth",
    "LatencySummary",
    "ObservatoryMetrics",
    "bounded_lag_ms",
    "bounded_latency_ms",
]
