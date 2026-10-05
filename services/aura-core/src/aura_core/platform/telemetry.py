"""Bounded metadata-only telemetry and structured container-log export.

The synchronous recording path performs no I/O. Records are retained in a
bounded ring and exported by a periodic lifecycle task. Capacity pressure drops
the oldest record and counts it. Export failures retain the batch for a bounded
number of attempts; an exhausted batch is dropped and counted so telemetry can
never block or exhaust the application.

Only declared dimensions and trace fields can be represented. Prompts,
responses, provider payloads, endpoints, credentials, and token values have no
accepted field and therefore cannot reach the exporter.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID

COMPONENT_VERSIONS: Mapping[str, str] = {
    "aura.execution.run_coordinator": "1.2.0",
    "aura.interaction.conversation_persistence": "1.2.0",
    "aura.runtime.model_inference": "1.2.0",
    "aura.runtime.model_routing": "1.2.0",
    "aura.runtime.stream_delivery": "1.2.0",
}

_METRICS: Mapping[str, frozenset[str]] = {
    "aura.execution.run_coordinator": frozenset(
        {
            "cancellations",
            "consumer_acknowledged",
            "consumer_received",
            "errors",
            "lease_expired",
            "outbox_dispatched",
            "outbox_duplicates",
            "operation_duration_ms",
            "queue_wait_ms",
            "retries",
            "run_duration_ms",
            "run_started",
            "run_status",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {
            "checkpoint_persisted",
            "errors",
            "model_selection_persisted",
            "persistence_duration_ms",
        }
    ),
    "aura.runtime.model_inference": frozenset(
        {
            "errors",
            "inference_duration_ms",
            "output_tokens",
            "provider_errors",
            "time_to_first_token_ms",
        }
    ),
    "aura.runtime.model_routing": frozenset(
        {"errors", "model_routed", "model_routing_duration_ms", "model_selection_persisted"}
    ),
    "aura.runtime.stream_delivery": frozenset(
        {
            "errors",
            "events_delivered",
            "sse_connections",
            "sse_reconnects",
            "stream_delivery_duration_ms",
        }
    ),
}

_SPAN_OPERATIONS: Mapping[str, frozenset[str]] = {
    "aura.execution.run_coordinator": frozenset(
        {
            "consumer.delivery",
            "conversation.command",
            "lease.expire",
            "outbox.dispatch",
            "run.cancel",
            "run.execute",
            "run.retry",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {"checkpoint.persist", "conversation.persist", "run.claim", "run.finish"}
    ),
    "aura.runtime.model_inference": frozenset({"model.infer"}),
    "aura.runtime.model_routing": frozenset({"model.route", "model.selection.persist"}),
    "aura.runtime.stream_delivery": frozenset(
        {"event.publish", "sse.connect", "sse.deliver", "sse.reconnect"}
    ),
}

_SPAN_METRICS: Mapping[str, str] = {
    "aura.execution.run_coordinator": "operation_duration_ms",
    "aura.interaction.conversation_persistence": "persistence_duration_ms",
    "aura.runtime.model_inference": "inference_duration_ms",
    "aura.runtime.model_routing": "model_routing_duration_ms",
    "aura.runtime.stream_delivery": "stream_delivery_duration_ms",
}

# Dimensions are suitable for metric aggregation and intentionally exclude all
# identifiers. Trace attributes may be high-cardinality but are never labels.
_DIMENSION_KEYS = frozenset(
    {"dependency", "error_class", "outcome", "provider", "status", "token_estimator"}
)
_TRACE_ATTRIBUTE_KEYS = frozenset(
    {
        "agent_revision_id",
        "attempt_count",
        "causation_id",
        "command_id",
        "conversation_id",
        "correlation_id",
        "delivery_count",
        "model_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
    }
)
_UUID_TRACE_ATTRIBUTES = frozenset(
    {
        "agent_revision_id",
        "causation_id",
        "command_id",
        "conversation_id",
        "correlation_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
    }
)
_COUNT_TRACE_ATTRIBUTES = frozenset({"attempt_count", "delivery_count"})
_ENUM_DIMENSIONS: Mapping[str, frozenset[str]] = {
    "dependency": frozenset(
        {
            "conversation_store",
            "event_store",
            "model_policy",
            "model_provider",
            "nats_jetstream",
            "postgresql",
            "sse_client",
        }
    ),
    "error_class": frozenset(
        {"cancel", "delivery", "persistence", "provider", "queue", "timeout"}
    ),
    "outcome": frozenset({"canceled", "duplicate", "error", "ok", "skipped"}),
    "status": frozenset(
        {
            "canceled",
            "cancel_requested",
            "completed",
            "failed",
            "interrupted",
            "queued",
            "running",
        }
    ),
    "token_estimator": frozenset({"chars_div_4_ceil"}),
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Measurement:
    kind: str
    component_id: str
    component_version: str
    metric: str
    value: float
    observed_at: datetime
    dimensions: tuple[tuple[str, str], ...] = ()
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    trace_attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TelemetryStats:
    retained: int
    capacity: int
    dropped: int
    rejected: int
    export_failures: int
    abandoned_batches: int
    exporter_configured: bool


@dataclass(frozen=True, slots=True)
class TelemetryBatch:
    measurements: tuple[Measurement, ...]
    stats: TelemetryStats


class TelemetryExporter(Protocol):
    async def export(self, batch: TelemetryBatch) -> None: ...


class StructuredContainerLogExporter:
    """Write metadata-only JSON lines to stdout for the container log driver."""

    def __init__(self, writer: Callable[[str], None] | None = None) -> None:
        self._writer = writer or _write_stdout

    async def export(self, batch: TelemetryBatch) -> None:
        lines = [self._measurement_line(item) for item in batch.measurements]
        lines.append(self._health_line(batch.stats))
        # Export runs only on the background lifecycle task; the application
        # recording path never performs this container-log I/O.
        self._write_lines(lines)
        await asyncio.sleep(0)

    def _write_lines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self._writer(line)

    @staticmethod
    def _measurement_line(item: Measurement) -> str:
        trace: dict[str, object] = {}
        if item.trace_id is not None:
            trace["trace_id"] = item.trace_id
        if item.span_id is not None:
            trace["span_id"] = item.span_id
        if item.parent_span_id is not None:
            trace["parent_span_id"] = item.parent_span_id
        if item.trace_attributes:
            trace["attributes"] = dict(item.trace_attributes)
        record: dict[str, object] = {
            "capture_policy": "metadata_only",
            "component_id": item.component_id,
            "component_version": item.component_version,
            "dimensions": dict(item.dimensions),
            "event": "aura.telemetry.measurement",
            "kind": item.kind,
            "metric": item.metric,
            "observed_at": item.observed_at.isoformat(),
            "value": item.value,
        }
        if trace:
            record["trace"] = trace
        return json.dumps(record, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _health_line(stats: TelemetryStats) -> str:
        return json.dumps(
            {
                "abandoned_batches": stats.abandoned_batches,
                "capture_policy": "metadata_only",
                "dropped": stats.dropped,
                "event": "aura.telemetry.health",
                "export_failures": stats.export_failures,
                "rejected": stats.rejected,
                "retained": stats.retained,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class MetadataMetrics:
    """Nonblocking bounded sink with metric/trace cardinality separation."""

    def __init__(
        self,
        *,
        capacity: int = 2048,
        exporter: TelemetryExporter | None = None,
        max_export_attempts: int = 3,
    ) -> None:
        if capacity < 1:
            raise ValueError("telemetry capacity must be positive")
        if max_export_attempts < 1:
            raise ValueError("telemetry export attempts must be positive")
        self._capacity = capacity
        self._measurements: deque[Measurement] = deque()
        self._exporter = exporter
        self._max_export_attempts = max_export_attempts
        self._current_export_attempts = 0
        self._dropped = 0
        self._rejected = 0
        self._export_failures = 0
        self._abandoned_batches = 0

    @property
    def exporter(self) -> TelemetryExporter | None:
        return self._exporter

    def increment(
        self,
        component: str,
        name: str,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            1.0,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def observe(
        self,
        component: str,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            value,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **trace_attributes: str,
    ) -> None:
        if operation not in _SPAN_OPERATIONS.get(component, frozenset()):
            self._rejected += 1
            return
        dimensions = {"dependency": dependency, "outcome": outcome}
        if error_class is not None:
            dimensions["error_class"] = error_class
        if provider is not None:
            dimensions["provider"] = provider
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        if (
            normalized is None
            or not _valid_span_id(span_id)
            or parent_span_id is not None
            and not _valid_span_id(parent_span_id)
        ):
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="span",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=_SPAN_METRICS[component],
                value=float(duration_ms),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                trace_attributes=(("operation", operation), *normalized[1]),
            )
        )

    def _record_metric(
        self,
        component: str,
        metric: str,
        value: float,
        *,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        attributes: Mapping[str, str],
    ) -> None:
        if metric not in _METRICS.get(component, frozenset()) or not math.isfinite(value):
            self._rejected += 1
            return
        dimensions = {key: value for key, value in attributes.items() if key in _DIMENSION_KEYS}
        trace_attributes = {
            key: value for key, value in attributes.items() if key in _TRACE_ATTRIBUTE_KEYS
        }
        if len(dimensions) + len(trace_attributes) != len(attributes):
            self._rejected += 1
            return
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        if normalized is None:
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="metric",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=metric,
                value=float(value),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                trace_attributes=normalized[1],
            )
        )

    def _normalize(
        self,
        component: str,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        dimensions: Mapping[str, str],
        trace_attributes: Mapping[str, str],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]] | None:
        if component not in COMPONENT_VERSIONS:
            return None
        if trace_id is not None and not _valid_trace_id(trace_id):
            return None
        complete_trace_attributes = dict(trace_attributes)
        if run_id is not None:
            complete_trace_attributes["run_id"] = run_id
        if conversation_id is not None:
            complete_trace_attributes["conversation_id"] = conversation_id
        normalized_dimensions: list[tuple[str, str]] = []
        for key, value in dimensions.items():
            if key not in _DIMENSION_KEYS or _looks_like_uuid(value):
                return None
            allowed = _ENUM_DIMENSIONS.get(key)
            if allowed is not None and value not in allowed:
                return None
            if key == "provider" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            normalized_dimensions.append((key, value))
        normalized_trace: list[tuple[str, str]] = []
        for key, value in complete_trace_attributes.items():
            if key not in _TRACE_ATTRIBUTE_KEYS:
                return None
            if key in _UUID_TRACE_ATTRIBUTES and not _valid_uuid(value):
                return None
            if key in _COUNT_TRACE_ATTRIBUTES and (
                not value.isascii() or not value.isdecimal()
            ):
                return None
            if key == "model_id" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            normalized_trace.append((key, value))
        return tuple(sorted(normalized_dimensions)), tuple(sorted(normalized_trace))

    def _append(self, measurement: Measurement) -> None:
        if len(self._measurements) == self._capacity:
            self._measurements.popleft()
            self._dropped += 1
        self._measurements.append(measurement)

    def snapshot(self) -> tuple[Measurement, ...]:
        return tuple(self._measurements)

    def stats(self) -> TelemetryStats:
        return TelemetryStats(
            retained=len(self._measurements),
            capacity=self._capacity,
            dropped=self._dropped,
            rejected=self._rejected,
            export_failures=self._export_failures,
            abandoned_batches=self._abandoned_batches,
            exporter_configured=self._exporter is not None,
        )

    async def flush(self) -> bool:
        """Export current records and aggregate health with bounded retries."""

        if self._exporter is None:
            return False
        batch_measurements = tuple(self._measurements)
        batch = TelemetryBatch(batch_measurements, self.stats())
        try:
            await self._exporter.export(batch)
        except Exception:
            self._export_failures += 1
            self._current_export_attempts += 1
            if self._current_export_attempts >= self._max_export_attempts:
                for expected in batch_measurements:
                    if self._measurements and self._measurements[0] is expected:
                        self._measurements.popleft()
                        self._dropped += 1
                self._abandoned_batches += 1
                self._current_export_attempts = 0
            return False
        for expected in batch_measurements:
            if self._measurements and self._measurements[0] is expected:
                self._measurements.popleft()
        self._current_export_attempts = 0
        return True


class TelemetryLifecycle:
    """Periodic exporter lifecycle shared by API and worker composition roots."""

    def __init__(self, metrics: MetadataMetrics, *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("telemetry flush interval must be positive")
        self.metrics = metrics
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> bool:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        return await self.metrics.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.metrics.flush()


def new_span_id() -> str:
    return secrets.token_hex(8)


def root_span_id(trace_id: str) -> str:
    if not _valid_trace_id(trace_id):
        raise ValueError("trace id must be 32 lowercase hex characters")
    return trace_id[:16]


def _write_stdout(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _looks_like_uuid(value: str) -> bool:
    return _valid_uuid(value)


def _valid_trace_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _valid_span_id(value: str) -> bool:
    return len(value) == 16 and all(character in "0123456789abcdef" for character in value)


class Stopwatch:
    def __init__(self) -> None:
        self.started = monotonic()

    def elapsed_ms(self) -> float:
        return (monotonic() - self.started) * 1000
