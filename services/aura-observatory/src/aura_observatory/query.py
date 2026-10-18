"""Owner-facing Observatory query and retention use cases."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import (
    ANALYTICS_DEFAULT_MAX_POINTS,
    ANALYTICS_DEFAULT_MAX_SERIES,
    AnalyticsRequest,
    DeletionResult,
    ObservatoryHealthPort,
    ObservatoryQueryPort,
    ObservatoryReferencePort,
    ObservatoryRetentionPort,
)

_LOGGER = logging.getLogger(__name__)
_QUERY_METRIC_RESOURCE_ALIASES = {
    # The bounded metric label vocabulary predates the versioned catalogue
    # resources; retain their instrumentation without emitting arbitrary labels.
    "component_definition": "components",
    "component_dependencies": "components",
}
_RETENTION_EXCEPTION_CLASSES = frozenset(
    {
        "AttributeError",
        "CancelledError",
        "ConnectError",
        "ConnectTimeout",
        "HTTPStatusError",
        "KeyError",
        "OSError",
        "PoolTimeout",
        "ReadTimeout",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "WriteTimeout",
    }
)
COMPONENT_HEALTH_DEFAULT_WINDOW = timedelta(hours=24)
COMPONENT_HEALTH_MAX_WINDOW = timedelta(days=31)

# Operational analytics is deliberately a finite catalogue.  Keep this list
# local to Observatory: it is an allowlist, not a second telemetry contract.
ANALYTICS_METRICS = (
    "operation_duration_ms",
    "run_duration_ms",
    "queue_wait_ms",
    "errors",
    "retries",
    "backlog",
    "backlog_depth",
    "inference_duration_ms",
    "time_to_first_token_ms",
    "output_tokens",
    "provider_errors",
    "model_routing_duration_ms",
    "structured_inference_duration_ms",
    "tool_selection_duration_ms",
    "tool_selection_outcome",
    "tool_policy_evaluation_duration_ms",
    "tool_policy_decision",
    "tool_argument_validation_outcome",
    "tool_execution_duration_ms",
    "tool_execution_outcome",
    "tool_timeout_count",
    "memory_extraction_duration_ms",
    "memory_candidate_decision",
    "memory_reviewable_candidate_count",
    "memory_operation_duration_ms",
    "memory_operation_outcome",
    "memory_retrieval_duration_ms",
    "memory_lexical_search_duration_ms",
    "memory_vector_search_duration_ms",
    "memory_rerank_duration_ms",
    "memory_processing_errors",
    "memory_retry_count",
    "memory_reindex_backlog",
    "document_retrieval_duration_ms",
    "document_retrieval_outcome",
    "document_retrieval_candidate_count",
    "reminder_scheduler_scan_duration_ms",
    "reminder_due_lag_ms",
    "reminder_series_due_lag_ms",
    "reminder_fired_count",
    "reminder_failure_count",
    "notification_failure_count",
    "notification_acknowledgement_outcome",
)
ANALYTICS_UNITS = frozenset({"count", "milliseconds", "bytes", "ratio", "score"})
ANALYTICS_FOCUSES = ("all", "model", "queue", "retrieval", "tool", "memory", "automation")
ANALYTICS_AGGREGATIONS = (
    "average",
    "sum",
    "minimum",
    "maximum",
    "p50",
    "p90",
    "p95",
    "p99",
    "rate",
    "count",
)
ANALYTICS_BUCKETS = {
    "one_minute": 60,
    "five_minutes": 300,
    "fifteen_minutes": 900,
    "one_hour": 3600,
    "six_hours": 21600,
    "one_day": 86400,
}
ANALYTICS_GROUPINGS = (
    "none",
    "component",
    "component_version",
    "operation",
    "deployment",
    "environment",
    "host",
    "service_role",
    "provider",
    "outcome",
    "error_class",
    "status",
    "retrieval_stage",
)
ANALYTICS_FILTER_KEYS = frozenset(
    {
        "component",
        "component_version",
        "operation",
        "deployment",
        "environment",
        "host",
        "service_role",
        "provider",
        "outcome",
        "error_class",
        "status",
        "retrieval_stage",
    }
)
ANALYTICS_MAX_WINDOW = timedelta(days=31)
ANALYTICS_MAX_POINTS = ANALYTICS_DEFAULT_MAX_POINTS
ANALYTICS_MAX_SERIES = ANALYTICS_DEFAULT_MAX_SERIES
ANALYTICS_MAX_BREAKDOWNS = 50
ANALYTICS_MAX_SCANNED_ROWS = 1_000_000
ANALYTICS_EXECUTION_LIMIT_MS = 5_000
ANALYTICS_PERCENTILE_METHOD = "clickhouse_quantile_exact"
ANALYTICS_MAX_LAG_MS = 604_800_000
ANALYTICS_STALE_AFTER_MS = 300_000
_ANALYTICS_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ANALYTICS_COMPONENT_RE = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_ANALYTICS_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_ANALYTICS_ENVIRONMENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")


def _analytics_unit(metric: str) -> str:
    return "milliseconds" if metric.endswith("_ms") else "count"


def _analytics_focus(metric: str) -> tuple[str, ...]:
    if metric.startswith(
        ("inference_", "model_", "structured_", "provider_", "output_", "time_to_")
    ):
        return ("model", "all")
    if metric.startswith(("queue_", "backlog")):
        return ("queue", "all")
    if metric.startswith(("tool_",)):
        return ("tool", "all")
    if metric.startswith(("memory_",)):
        return ("memory", "all")
    if metric.startswith(("document_",)):
        return ("retrieval", "all")
    if metric.startswith(("reminder_", "notification_")):
        return ("automation", "all")
    return ("all",)


def analytics_unavailable(metric: str) -> list[str]:
    unavailable: list[str] = []
    if not metric.startswith(("model_", "inference_", "provider_", "output_", "time_to_")):
        unavailable.append("model_identity")
    if "retry" not in metric:
        unavailable.append("retryability")
    if not metric.startswith(("backlog", "queue_")):
        unavailable.append("saturation")
    return unavailable[:10]


def analytics_metric_catalogue(
    *, focus: str | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Return the closed catalogue and all execution limits."""

    selected = focus or "all"
    if selected not in ANALYTICS_FOCUSES:
        raise ValueError("invalid analytics focus")
    items: list[dict[str, object]] = []
    for metric in ANALYTICS_METRICS:
        focus_areas = _analytics_focus(metric)
        if selected != "all" and selected not in focus_areas:
            continue
        unit = _analytics_unit(metric)
        aggregations = (
            ("average", "minimum", "maximum", "p50", "p90", "p95", "p99", "count")
            if unit == "milliseconds"
            else ("sum", "rate", "count", "average", "minimum", "maximum")
        )
        items.append(
            {
                "metric": metric,
                "unit": unit,
                "focusAreas": list(focus_areas),
                "supportedAggregations": list(aggregations),
                "supportedGroupings": list(ANALYTICS_GROUPINGS),
                "availability": "available",
                "unavailableSignals": analytics_unavailable(metric),
            }
        )
    return {
        "snapshotAt": _iso(now or datetime.now(UTC)),
        "items": items,
        "limits": {
            "defaultWindowHours": 24,
            "maximumWindowDays": 31,
            "maximumPointsPerSeries": ANALYTICS_MAX_POINTS,
            "maximumSeries": ANALYTICS_MAX_SERIES,
            "maximumBreakdowns": ANALYTICS_MAX_BREAKDOWNS,
            "maximumScannedRows": ANALYTICS_MAX_SCANNED_ROWS,
            "executionTimeoutMs": ANALYTICS_EXECUTION_LIMIT_MS,
        },
        "capturePolicy": "metadata_only",
    }


def _analytics_definition(metric: str) -> dict[str, object] | None:
    """Return the exact public catalogue definition for one metric."""

    if metric not in ANALYTICS_METRICS:
        return None
    unit = _analytics_unit(metric)
    aggregations = (
        ("average", "minimum", "maximum", "p50", "p90", "p95", "p99", "count")
        if unit == "milliseconds"
        else ("sum", "rate", "count", "average", "minimum", "maximum")
    )
    return {
        "metric": metric,
        "unit": unit,
        "focusAreas": _analytics_focus(metric),
        "supportedAggregations": aggregations,
        "supportedGroupings": ANALYTICS_GROUPINGS,
    }


def bounded_analytics_lag_ms(
    latest: datetime | None, *, now: datetime | None = None
) -> float | None:
    if latest is None:
        return None
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    observed = latest.astimezone(UTC)
    return min(
        float(ANALYTICS_MAX_LAG_MS),
        max(0.0, (reference - observed).total_seconds() * 1000.0),
    )


def analytics_freshness(
    latest: datetime | None, *, now: datetime | None = None
) -> dict[str, object]:
    lag = bounded_analytics_lag_ms(latest, now=now)
    return {
        "latestObservedAt": _iso(latest),
        "lagMs": lag,
        "state": (
            "unavailable"
            if latest is None
            else "stale"
            if (lag or 0.0) > ANALYTICS_STALE_AFTER_MS
            else "fresh"
        ),
    }


def bounded_analytics_window(
    from_time: datetime | None,
    to_time: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    upper = to_time or now or datetime.now(UTC)
    if upper.tzinfo is None or upper.utcoffset() is None:
        raise ValueError("analytics timestamps must include a timezone")
    upper = upper.astimezone(UTC)
    if from_time is None:
        lower = upper - timedelta(hours=24)
    else:
        if from_time.tzinfo is None or from_time.utcoffset() is None:
            raise ValueError("analytics timestamps must include a timezone")
        lower = from_time.astimezone(UTC)
    if lower >= upper or upper - lower > ANALYTICS_MAX_WINDOW:
        raise ValueError("analytics window is invalid or too wide")
    return lower, upper


def choose_analytics_bucket(bucket: str, lower: datetime, upper: datetime) -> tuple[str, int]:
    seconds = (upper - lower).total_seconds()
    if bucket == "auto":
        chosen = next(
            (
                value
                for value in ANALYTICS_BUCKETS.items()
                if seconds / value[1] <= ANALYTICS_MAX_POINTS
            ),
            None,
        )
        if chosen is None:
            chosen = ("one_day", ANALYTICS_BUCKETS["one_day"])
        return chosen
    if bucket not in ANALYTICS_BUCKETS:
        raise ValueError("invalid analytics bucket")
    chosen_seconds = ANALYTICS_BUCKETS[bucket]
    if seconds / chosen_seconds > ANALYTICS_MAX_POINTS:
        raise ValueError("analytics bucket would exceed point limit")
    return bucket, chosen_seconds


def bounded_component_health_window(
    from_time: datetime | None,
    to_time: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return a finite UTC health window or reject an unsafe range."""

    raw_upper = to_time or now or datetime.now(UTC)
    if raw_upper.tzinfo is None or raw_upper.utcoffset() is None:
        raise ValueError("component health timestamps must include a timezone")
    upper = raw_upper.astimezone(UTC)
    if from_time is not None and (from_time.tzinfo is None or from_time.utcoffset() is None):
        raise ValueError("component health timestamps must include a timezone")
    lower = (
        from_time.astimezone(UTC)
        if from_time is not None
        else upper - COMPONENT_HEALTH_DEFAULT_WINDOW
    )
    if lower >= upper:
        raise ValueError("component health range must be increasing")
    if upper - lower > COMPONENT_HEALTH_MAX_WINDOW:
        raise ValueError("component health range is too wide")
    return lower, upper


def _retention_exception_class(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _RETENTION_EXCEPTION_CLASSES else "other"


_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{16,1024}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class RetentionKind(StrEnum):
    DELETE_BEFORE = "delete_before"
    PURGE_ALL = "purge_all"


class RetentionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetentionConflictError(ValueError):
    """An idempotency key was reused for a different retention command."""


@dataclass(frozen=True, slots=True)
class RunFilters:
    status: str | None = None
    component: str | None = None
    model: str | None = None
    deployment: str | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetentionRequest:
    kind: RetentionKind
    cutoff: datetime | None = None
    confirmation: str = ""
    idempotency_key: str = ""
    strict_confirmation: bool = False

    @property
    def fingerprint(self) -> str:
        cutoff = (
            self.cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if self.cutoff is not None
            else ""
        )
        return f"{self.kind.value}:{cutoff}"


@dataclass(slots=True)
class RetentionAction:
    action_id: str
    kind: RetentionKind
    cutoff: datetime | None
    status: RetentionStatus = RetentionStatus.QUEUED
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deleted_count: int = 0
    deleted_receipt_count: int = 0
    deleted_run_count: int = 0
    target_observation_count: int = 0
    target_receipt_count: int = 0
    target_run_count: int = 0
    deletion_phase: str = "queued"
    error: str | None = None
    idempotency_key: str = ""

    @property
    def fingerprint(self) -> str:
        cutoff = (
            self.cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if self.cutoff is not None
            else ""
        )
        return f"{self.kind.value}:{cutoff}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "actionId": self.action_id,
            "action": self.kind.value,
            "cutoff": self.cutoff.isoformat().replace("+00:00", "Z") if self.cutoff else None,
            "status": self.status.value,
            "createdAt": self.requested_at.isoformat().replace("+00:00", "Z"),
            "startedAt": self.started_at.isoformat().replace("+00:00", "Z")
            if self.started_at
            else None,
            "finishedAt": self.completed_at.isoformat().replace("+00:00", "Z")
            if self.completed_at
            else None,
            "deletedObservationCount": self.deleted_count,
            "deletedReceiptCount": self.deleted_receipt_count,
            "deletedRunCount": self.deleted_run_count,
            "failureCode": self.error,
        }


class ObservatoryReadStore(Protocol):
    async def list_observations(
        self, *, limit: int | None = None
    ) -> list[MetadataObservation]: ...

    async def components(self) -> list[dict[str, Any]]: ...

    async def deployments(self) -> list[dict[str, Any]]: ...

    async def ingest_overview(self) -> dict[str, Any]: ...

    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult: ...


class BoundedReferenceAnalyticsPort:
    """Explicit bounded reference adapter for deterministic local compositions.

    Production composition injects ``ObservatoryQueryPort`` and never creates
    this adapter.  The adapter exists for memory/reference fixtures only; its
    output is still deterministically capped and reports the scan boundary.
    """

    def __init__(self, store: ObservatoryReadStore) -> None:
        self._store = store

    async def operational_analytics_query(self, request: AnalyticsRequest) -> dict[str, object]:
        observations = await self._store.list_observations(limit=ANALYTICS_MAX_SCANNED_ROWS + 1)
        ordered = sorted(
            observations,
            key=lambda item: (item.observed_at.astimezone(UTC), item.observation_id),
        )
        source_truncated = len(ordered) > ANALYTICS_MAX_SCANNED_ROWS
        selected = ordered[: ANALYTICS_MAX_SCANNED_ROWS]
        return _reference_analytics_snapshot(
            request,
            selected,
            ANALYTICS_BUCKETS[request.bucket],
            source_truncated=source_truncated,
        )


def encode_cursor(observed_at: datetime, observation_id: str, snapshot: int | None = None) -> str:
    raw = json.dumps(
        {
            "at": observed_at.astimezone(UTC).isoformat(),
            "id": observation_id,
            "snapshot": snapshot,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    if _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded: object = json.loads(raw)
        data = cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
        when = datetime.fromisoformat(str(data["at"])).astimezone(UTC)
        identifier = str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if _UUID_RE.fullmatch(identifier) is None:
        raise ValueError("invalid cursor")
    return when, identifier


def decode_health_cursor(value: str) -> tuple[datetime, str]:
    """Decode the closed health-history cursor shape at the use-case boundary."""

    if _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("invalid cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded: object = json.loads(raw)
        data: dict[str, object] = (
            cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
        )
        if set(data) != {"at", "id"}:
            raise ValueError("invalid cursor")
        when = datetime.fromisoformat(str(data["at"]))
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("invalid cursor")
        identifier = str(UUID(str(data["id"])))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    return when.astimezone(UTC), identifier


def decode_run_cursor(value: str) -> tuple[int | None, datetime, str]:
    """Decode a run cursor and retain its immutable ingestion fence."""

    if _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
        snapshot = data.get("snapshot")
        if snapshot is not None and (not isinstance(snapshot, int) or snapshot < 0):
            raise ValueError("invalid cursor")
        when = datetime.fromisoformat(str(data["at"])).astimezone(UTC)
        identifier = str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if _UUID_RE.fullmatch(identifier) is None:
        raise ValueError("invalid cursor")
    return snapshot, when, identifier


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _sql_time(value: datetime) -> str:
    return "'" + value.astimezone(UTC).isoformat().replace("+00:00", "Z") + "'"


def _query_resource(name: str) -> Any:
    def decorate(function: Any) -> Any:
        @wraps(function)
        async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            started = monotonic()
            try:
                result = await function(self, *args, **kwargs)
            except Exception:
                self._record_query_metrics(name, started, "failed")
                await self._record_query_health_latency(started)
                raise
            self._record_query_metrics(name, started, "succeeded")
            await self._record_query_health_latency(started)
            return result

        return wrapped

    return decorate


class ObservatoryHealthProjector:
    """Persist material health transitions independently of read requests."""

    def __init__(
        self,
        health_port: ObservatoryHealthPort,
        *,
        interval_seconds: float = 15.0,
        refresh: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.health_port = health_port
        self.interval_seconds = interval_seconds
        self.refresh = refresh

    async def project_once(self) -> dict[str, object] | None:
        if self.refresh is not None:
            await self.refresh()
        snapshot = await self.health_port.health_snapshot()
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        await self.health_port.save_health_snapshot(snapshot)
        return snapshot

    async def run(self, stop: Any = None) -> None:
        """Project current health on a bounded cadence until stopped."""

        while True:
            try:
                await self.project_once()
            except Exception:
                # Diagnostics must not stop the worker or affect Core-facing
                # activity; the next tick retries projection.
                pass
            if stop is None:
                await asyncio.sleep(self.interval_seconds)
                continue
            if hasattr(stop, "is_set"):
                if stop.is_set():
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    continue
                return
            try:
                await asyncio.wait_for(asyncio.shield(stop), timeout=self.interval_seconds)
                return
            except TimeoutError:
                continue


class ObservatoryQueryService:
    """Owner-facing query, live-update, and retention use cases."""

    def __init__(
        self,
        store: ObservatoryReadStore,
        metrics: ObservatoryMetrics | None = None,
        *,
        query_port: ObservatoryQueryPort | None = None,
        retention_port: ObservatoryRetentionPort | None = None,
        health_port: ObservatoryHealthPort | None = None,
    ) -> None:
        self.store = store
        self.metrics = metrics or ObservatoryMetrics()
        # Adapter choice belongs to composition.  Direct reference-store
        # construction remains available for deterministic tests, but the
        # production composition always supplies both narrow ports.
        self.query_port = query_port
        self._reference_analytics_port = BoundedReferenceAnalyticsPort(store)
        self.retention_port = retention_port or cast(ObservatoryRetentionPort, store)
        self.health_port = health_port or cast(ObservatoryHealthPort, store)
        self._durable_retention = retention_port is not None

    @_query_resource("overview")
    async def overview(self) -> dict[str, Any]:
        if self.query_port is not None:
            narrow = self.query_port
            result = await narrow.overview_query()
            health = result.get("health")
            self._record_health_metrics(
                cast(dict[str, object] | None, health) if isinstance(health, dict) else None
            )
            return result
        observations = await self.store.list_observations()
        memory = cast(ObservatoryReferencePort, self.store)
        snapshot_sequence = memory.snapshot_sequence
        sequence_by_id = memory.event_sequence
        observations = [
            item
            for item in observations
            if sequence_by_id.get(item.observation_id, 0) <= snapshot_sequence
        ]
        ingest = await self.store.ingest_overview()
        latest = max(
            observations,
            key=lambda item: (item.observed_at.astimezone(UTC), item.observation_id),
            default=None,
        )
        result = {
            "observationCount": len(observations),
            "runCount": len(
                {
                    item.correlation.run_id
                    for item in observations
                    if item.correlation and item.correlation.run_id
                }
            ),
            "traceCount": len({item.trace.trace_id for item in observations if item.trace}),
            "ingest": ingest,
            "components": len(await self.store.components()),
            "deployments": len(await self.store.deployments()),
            "latestObservationId": latest.observation_id if latest else None,
            "health": ingest.get("health") or await self.health_port.health_snapshot(),
        }
        health = result.get("health")
        if self.query_port is None:
            result["snapshotSequence"] = cast(
                ObservatoryReferencePort, self.store
            ).snapshot_sequence
        self._record_health_metrics(cast(dict[str, object] | None, result.get("health")))
        return result

    @_query_resource("health_history")
    async def health_history(
        self,
        *,
        cursor: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if cursor is not None:
            decode_health_cursor(cursor)
        if self.query_port is not None:
            result = await self.query_port.health_history_query(
                cursor=cursor, from_time=from_time, to_time=to_time, limit=limit
            )
        else:
            result = await self.health_port.health_history(
                cursor=cursor,
                from_time=from_time,
                to_time=to_time,
                limit=limit,
            )
        return result

    @_query_resource("runs")
    async def runs(
        self, filters: RunFilters, *, cursor: str | None, limit: int = 50
    ) -> dict[str, Any]:
        if self.query_port is not None:
            narrow = self.query_port
            result = await narrow.run_query(filters, cursor=cursor, limit=limit)
            return result
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        observations = await self.store.list_observations()
        memory = cast(ObservatoryReferencePort, self.store)
        snapshot_sequence = memory.snapshot_sequence
        sequence_by_id = memory.event_sequence
        observations = [
            item
            for item in observations
            if sequence_by_id.get(item.observation_id, 0) <= snapshot_sequence
        ]
        # Status is derived from the latest observation in each run.  Applying
        # it to rows before grouping would allow an earlier status to keep a
        # run in a later-status query.
        selected = [item for item in observations if _matches(item, filters, include_status=False)]
        selected.sort(
            key=lambda item: (item.observed_at.astimezone(UTC), item.observation_id), reverse=True
        )
        groups: dict[str, list[MetadataObservation]] = defaultdict(list)
        for item in selected:
            if item.correlation and item.correlation.run_id:
                groups[item.correlation.run_id].append(item)
        grouped_runs = sorted(
            groups.items(),
            key=lambda pair: max(
                (item.observed_at.astimezone(UTC), item.observation_id) for item in pair[1]
            ),
            reverse=True,
        )
        cursor_snapshot: int | None = None
        if cursor:
            cursor_snapshot, at, identifier = decode_run_cursor(cursor)
            if cursor_snapshot is not None:
                selected = [
                    item
                    for item in selected
                    if sequence_by_id.get(item.observation_id, 0) <= cursor_snapshot
                ]
                groups = defaultdict(list)
                for item in selected:
                    if item.correlation and item.correlation.run_id:
                        groups[item.correlation.run_id].append(item)
                grouped_runs = sorted(
                    groups.items(),
                    key=lambda pair: max(
                        (item.observed_at.astimezone(UTC), item.observation_id) for item in pair[1]
                    ),
                    reverse=True,
                )
            grouped_runs = [
                pair
                for pair in grouped_runs
                if max((item.observed_at.astimezone(UTC), item.observation_id) for item in pair[1])
                < (at, identifier)
            ]
        if filters.status:
            grouped_runs = [
                pair
                for pair in grouped_runs
                if _run_status(
                    max(pair[1], key=lambda item: (item.observed_at, item.observation_id))
                )
                == filters.status
            ]
        runs: list[dict[str, Any]] = []
        for run_id, items in grouped_runs:
            newest = max(items, key=lambda item: (item.observed_at, item.observation_id))
            runs.append(_run_dict(run_id, items, newest))
        page_groups = grouped_runs[:limit]
        next_cursor = (
            encode_cursor(
                max(
                    page_groups[-1][1], key=lambda item: (item.observed_at, item.observation_id)
                ).observed_at,
                max(
                    page_groups[-1][1], key=lambda item: (item.observed_at, item.observation_id)
                ).observation_id,
                snapshot=cursor_snapshot if cursor_snapshot is not None else snapshot_sequence,
            )
            if len(page_groups) == limit
            else None
        )
        result = {
            "items": runs[:limit],
            "nextCursor": next_cursor,
            "latestObservationId": (
                max(
                    selected, key=lambda item: (item.observed_at, item.observation_id)
                ).observation_id
                if selected
                else None
            ),
        }
        return result

    @_query_resource("run_detail")
    async def run_detail(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        if self.query_port is not None:
            narrow = self.query_port
            result = await narrow.run_detail_query(run_id, cursor=cursor, limit=limit)
            return result
        items = [
            item
            for item in await self.store.list_observations()
            if item.correlation and item.correlation.run_id == run_id
        ]
        if not items:
            return None
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
            items = [
                item
                for item in items
                if (item.observed_at.astimezone(UTC), item.observation_id) > (cursor_at, cursor_id)
            ]
            if not items:
                return None
        # Fetch one extra item before slicing so bounded detail responses can
        # advertise a continuation cursor.  Slicing first silently made the
        # HTTP page_size contract report every page as complete.
        truncated = len(items) > limit
        items = sorted(items, key=lambda item: (item.observed_at, item.observation_id))[:limit]
        newest = max(items, key=lambda item: (item.observed_at, item.observation_id))
        page = items[:limit]
        result = _run_dict(run_id, page, newest, include_spans=True)
        result["partial"] = truncated
        result["nextCursor"] = (
            encode_cursor(page[-1].observed_at, page[-1].observation_id)
            if truncated and page
            else None
        )
        return _ReferenceRunDetail(result, page)

    @_query_resource("trace_detail")
    async def trace_detail(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        if self.query_port is not None:
            narrow = self.query_port
            result_value = await narrow.trace_detail_query(trace_id, cursor=cursor, limit=limit)
            return result_value
        items = [
            item
            for item in await self.store.list_observations()
            if item.trace and item.trace.trace_id == trace_id
        ]
        if not items:
            return None
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
            items = [
                item
                for item in items
                if (item.observed_at.astimezone(UTC), item.observation_id) > (cursor_at, cursor_id)
            ]
            if not items:
                return None
        # Keep the extra-item decision above the slice for the same bounded
        # continuation semantics as run details.
        truncated = len(items) > limit
        items = sorted(items, key=lambda item: (item.observed_at, item.observation_id))[:limit]
        spans = [
            _span_dict(item)
            for item in sorted(items, key=lambda i: (i.observed_at, i.observation_id))
            if item.trace and item.trace.span_id
        ]
        unique_spans: dict[str, dict[str, Any]] = {}
        for span in spans:
            unique_spans.setdefault(str(span["spanId"]), span)
        spans = list(unique_spans.values())
        run_ids = {
            item.correlation.run_id
            for item in items
            if item.correlation and item.correlation.run_id
        }
        span_tree = _tree(spans)
        partial = any(bool(span.get("partial")) for span in spans)
        result: dict[str, Any] = {
            "traceId": trace_id,
            "runId": next(iter(run_ids), None),
            "partial": partial or truncated,
            "spans": span_tree,
            "nextCursor": (
                encode_cursor(items[-1].observed_at, items[-1].observation_id)
                if truncated and items
                else None
            ),
        }
        return result

    async def components(self) -> list[dict[str, Any]]:
        started = monotonic()
        try:
            result_value = (
                await self.query_port.components_query()
                if self.query_port is not None
                else await self.store.components()
            )
            result = (
                cast(dict[str, Any], result_value).get("items", [])
                if isinstance(result_value, dict)
                else cast(list[dict[str, Any]], result_value)
            )
        except Exception:
            self._record_query_metrics("components", started, "failed")
            await self._record_query_health_latency(started)
            raise
        self._record_query_metrics("components", started, "succeeded")
        await self._record_query_health_latency(started)
        return result

    @_query_resource("components")
    async def component_collection(
        self,
        *,
        definition_state: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if self.query_port is not None:
            result = await self.query_port.component_collection_query(
                definition_state=definition_state, cursor=cursor, limit=limit
            )
            return cast(dict[str, Any], result)
        # Keep this decorated use case independent from the legacy public
        # components() use case, which owns its own metrics.  Calling it here
        # would emit duplicate duration/outcome samples for one request.
        values = await self.store.components()
        return {"items": values[:limit], "nextCursor": None, "snapshotAt": iso(datetime.now(UTC))}

    @_query_resource("component_definition")
    async def component_definition(
        self, component_id: str, component_version: str
    ) -> dict[str, Any] | None:
        if self.query_port is None:
            return None
        return cast(
            dict[str, Any] | None,
            await self.query_port.component_definition_query(component_id, component_version),
        )

    @_query_resource("component_dependencies")
    async def component_dependencies(
        self,
        component_id: str,
        component_version: str,
        *,
        depth: int = 1,
        max_nodes: int = 100,
    ) -> dict[str, Any] | None:
        if not 1 <= depth <= 4 or not 1 <= max_nodes <= 200:
            raise ValueError("dependency topology bounds are invalid")
        if self.query_port is None:
            return None
        return cast(
            dict[str, Any] | None,
            await self.query_port.component_dependencies_query(
                component_id,
                component_version,
                depth=depth,
                max_nodes=max_nodes,
            ),
        )

    @_query_resource("component_health")
    async def component_health(
        self,
        component_id: str,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        component_version: str | None = None,
        point_limit: int = 200,
    ) -> dict[str, Any] | None:
        if self.query_port is not None:
            from_time, to_time = bounded_component_health_window(from_time, to_time)
            narrow = self.query_port
            result_value = await narrow.component_health_query(
                component_id,
                from_time,
                to_time,
                component_version=component_version,
                point_limit=point_limit,
            )
            return result_value
        items = [
            item
            for item in await self.store.list_observations()
            if item.component.id == component_id
            and (from_time is None or item.observed_at.astimezone(UTC) >= from_time.astimezone(UTC))
            and (to_time is None or item.observed_at.astimezone(UTC) < to_time.astimezone(UTC))
        ]
        if not items:
            return None
        executions: dict[str, list[MetadataObservation]] = {}
        for item in sorted(items, key=lambda value: (value.observed_at, value.observation_id)):
            span_id = item.trace.span_id if item.trace and item.trace.span_id else None
            if span_id is None and item.measurement.name != "duration_ms":
                continue
            # A spanless primary duration is still one invocation.  Keep its
            # stable observation identity instead of coalescing all such rows
            # into a synthetic empty-span execution.
            executions.setdefault(span_id or item.observation_id, []).append(item)
        canonical: list[tuple[MetadataObservation | None, MetadataObservation]] = []
        for group in executions.values():
            duration_candidates = [
                item
                for item in group
                if item.measurement.unit == "milliseconds"
                and item.measurement.name == "duration_ms"
            ]
            duration = max(
                duration_candidates,
                key=lambda value: (
                    value.measurement.name == "duration_ms",
                    value.observed_at,
                    value.observation_id,
                ),
                default=None,
            )
            outcome_candidates = [
                item
                for item in group
                if "outcome" in item.dimensions or "status" in item.dimensions
            ]
            outcome = max(
                outcome_candidates or group,
                key=lambda value: (value.observed_at, value.observation_id),
            )
            canonical.append((duration, outcome))
        values = sorted(
            duration.measurement.value for duration, _outcome in canonical if duration is not None
        )
        failures = sum(
            1 for _duration, outcome in canonical if _normalize_outcome(outcome) == "failed"
        )
        outcomes = {key: 0 for key in ("succeeded", "failed", "cancelled", "unknown")}
        for _duration, outcome in canonical:
            outcomes[_normalize_outcome(outcome)] += 1
        summary = {
            "sampleCount": len(canonical),
            "latencyP50Ms": _percentile(values, 0.50),
            "latencyP95Ms": _percentile(values, 0.95),
            "errorRate": failures / len(canonical) if canonical else None,
            "outcomes": outcomes,
        }
        result: dict[str, Any] = {"componentId": component_id, "summary": summary, "points": []}
        return result

    @_query_resource("analytics")
    async def analytics(self, request: AnalyticsRequest) -> dict[str, object]:
        """Run one finite operational analytics snapshot.

        Production composition supplies a ClickHouse query port.  The
        reference path is intentionally limited to deterministic tests and
        local compositions; it is never selected by the production factory.
        """

        _validate_analytics_request(request)
        lower, upper = bounded_analytics_window(request.from_time, request.to_time)
        bucket, _bucket_seconds = choose_analytics_bucket(request.bucket, lower, upper)
        normalized = replace(
            request,
            from_time=lower,
            to_time=upper,
            bucket=bucket,
        )
        if self.query_port is not None:
            return await self.query_port.operational_analytics_query(normalized)
        return await self._reference_analytics_port.operational_analytics_query(normalized)

    def _record_query_metrics(self, resource: str, started: float, outcome: str) -> None:
        metric_resource = _QUERY_METRIC_RESOURCE_ALIASES.get(resource, resource)
        self.metrics.record(
            "observatory_query_duration_ms",
            (monotonic() - started) * 1000.0,
            {"resource": metric_resource},
        )
        self.metrics.record(
            "observatory_query_outcome",
            labels={"resource": metric_resource, "outcome": outcome},
        )

    async def _record_query_health_latency(self, started: float) -> None:
        """Persist one bounded use-case query duration without affecting reads."""

        try:
            await self.health_port.record_latency("query", (monotonic() - started) * 1000.0)
        except Exception:
            # Health persistence is diagnostic and must never turn a successful
            # owner query into a service failure.
            return

    def _record_health_metrics(self, health: dict[str, object] | None) -> None:
        if not health:
            return
        numeric = {
            "observatory_ingest_intake_lag_ms": health.get("intakeLagMs"),
            "observatory_ingest_visibility_lag_count": health.get("visibilityLagCount"),
            "observatory_ingest_duplicate_count": health.get("duplicateCount"),
            "observatory_ingest_worker_heartbeat_age_ms": health.get("workerHeartbeatAgeMs"),
            "observatory_ingest_backlog_count": health.get("backlogCount"),
            "observatory_query_freshness_lag_ms": health.get("queryFreshnessLagMs"),
        }
        for name, value in numeric.items():
            if isinstance(value, (int, float)):
                self.metrics.set(name, float(value))
        reasons_value = health.get("rejectionReasons")
        reasons = cast(list[object], reasons_value) if isinstance(reasons_value, list) else []
        for item in reasons:
            if isinstance(item, dict):
                reason = cast(dict[str, object], item).get("reason")
                count = cast(dict[str, object], item).get("count")
                if isinstance(reason, str) and isinstance(count, (int, float)):
                    try:
                        self.metrics.set(
                            "observatory_ingest_rejection_count",
                            float(count),
                            {"reason": reason},
                        )
                    except ValueError:
                        continue

    async def deployments(self) -> list[dict[str, Any]]:
        started = monotonic()
        try:
            result = (
                await self.query_port.deployments_query()
                if self.query_port is not None
                else await self.store.deployments()
            )
        except Exception:
            self._record_query_metrics("deployments", started, "failed")
            await self._record_query_health_latency(started)
            raise
        self._record_query_metrics("deployments", started, "succeeded")
        await self._record_query_health_latency(started)
        return result

    async def events_after(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        self.metrics.record("observatory_sse_active", labels={"event_type": "stream"})
        started = monotonic()
        failed = False
        try:
            async for event in self._events_after(last_event_id, snapshot_sequence):
                yield event
        except Exception:
            failed = True
            self.metrics.record(
                "observatory_query_outcome",
                labels={"resource": "events", "outcome": "failed"},
            )
            self.metrics.record(
                "observatory_query_duration_ms",
                (monotonic() - started) * 1000.0,
                {"resource": "events"},
            )
            raise
        finally:
            if not failed:
                self.metrics.record(
                    "observatory_query_duration_ms",
                    (monotonic() - started) * 1000.0,
                    {"resource": "events"},
                )
                self.metrics.record(
                    "observatory_query_outcome",
                    labels={"resource": "events", "outcome": "succeeded"},
                )
            self.metrics.record("observatory_sse_active", -1.0, {"event_type": "stream"})

    async def _events_after(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        # Materialize the persisted catch-up series even when the snapshot is
        # empty; the counter remains zero until an actually replayed row is
        # emitted and never counts live rows or heartbeats.
        self.metrics.record(
            "observatory_sse_replay_count", 0, {"event_type": "observation.changed"}
        )
        if self.query_port is not None:
            narrow = self.query_port
            self.metrics.record(
                "observatory_sse_connection_count", labels={"event_type": "connection"}
            )
            async for event in narrow.events_after_query(last_event_id, snapshot_sequence):
                replayed = bool(event.pop("_persisted_replay", False))
                event_type = str(event.get("type", "heartbeat"))
                if replayed and event_type in {"observation.changed", "health.changed"}:
                    self.metrics.record(
                        "observatory_sse_replay_count",
                        labels={"event_type": event_type},
                    )
                yield event
            return
        self.metrics.record("observatory_sse_connection_count", labels={"event_type": "connection"})
        cursor = last_event_id
        snapshot_anchor = snapshot_sequence
        for _ in range(3):
            observations = await self.store.list_observations()
            memory = cast(ObservatoryReferencePort, self.store)
            sequence_by_id = memory.event_sequence
            observations.sort(
                key=lambda item: (
                    sequence_by_id.get(item.observation_id, 0),
                    item.observation_id,
                )
            )
            health_events = memory.health_events
            identifiers = {item.observation_id for item in observations}
            identifiers.update(str(item.get("id", "")) for item in health_events)
            cursor_sequence = (
                sequence_by_id.get(cursor, 0)
                if cursor is not None and cursor in identifiers
                else None
            )
            initial_cursor_was_missing = last_event_id is None
            if cursor is not None and cursor_sequence is None:
                # A deleted/unknown cursor deterministically resets to the
                # oldest persisted event; the preceding REST/SSE snapshot is
                # the consistency boundary for the client.
                cursor = None
            # A REST/SSE snapshot anchor is only a boundary for a stream that
            # opened without a cursor.  An unknown supplied cursor retains the
            # historical deterministic reset to the oldest persisted event;
            # a known cursor, including health.changed, is strictly exclusive.
            replay_after = (
                snapshot_anchor or 0
                if initial_cursor_was_missing and cursor_sequence is None
                else cursor_sequence or 0
            )
            replay_observations = [
                item
                for item in observations
                if sequence_by_id.get(item.observation_id, 0) > replay_after
            ]
            for item in replay_observations:
                sequence = sequence_by_id.get(item.observation_id, 0)
                if sequence <= replay_after:
                    continue
                self.metrics.record(
                    "observatory_sse_replay_count", labels={"event_type": "observation.changed"}
                )
                yield {
                    "id": item.observation_id,
                    "type": "observation.changed",
                    "data": {
                        "observationId": item.observation_id,
                        "runId": item.correlation.run_id if item.correlation else None,
                        "traceId": item.trace.trace_id if item.trace else None,
                    },
                    "_persisted_replay": True,
                }
            for health_event in health_events:
                event_id = str(health_event.get("id", ""))
                sequence = sequence_by_id.get(event_id, 0)
                if sequence <= replay_after:
                    continue
                self.metrics.record(
                    "observatory_sse_replay_count", labels={"event_type": "health.changed"}
                )
                yield {
                    "id": event_id,
                    "type": "health.changed",
                    "data": {},
                    "_persisted_replay": True,
                }
            wait_for_event = memory.wait_for_event
            before = (
                replay_observations[-1].observation_id
                if replay_observations
                else None
                if snapshot_anchor is not None and initial_cursor_was_missing
                else observations[-1].observation_id
                if observations
                else None
            )
            await wait_for_event(0.1)
            after = await self.store.list_observations()
            if not observations and after:
                # Give a just-opened stream a chance to deliver its first
                # persisted observation before emitting a keepalive.  This
                # avoids making a heartbeat look like a data update when the
                # initial snapshot was empty.
                continue
            if not after or (after[-1].observation_id if after else None) == before:
                if not observations:
                    yield {
                        "type": "heartbeat",
                        "data": {},
                    }
                return
            cursor = before

    async def retention(self, request: RetentionRequest) -> RetentionAction:
        _validate_retention(request, strict_confirmation=request.strict_confirmation)
        retention_store = self.retention_port
        existing = cast(
            RetentionAction | None,
            await retention_store.find_retention_by_key(request.idempotency_key),
        )
        if existing is not None:
            if existing.fingerprint != request.fingerprint:
                raise RetentionConflictError("idempotency key conflicts with retention command")
            return existing
        action = RetentionAction(
            str(uuid4()),
            request.kind,
            request.cutoff,
            idempotency_key=request.idempotency_key,
        )
        if not await retention_store.save_retention(action):
            existing = cast(
                RetentionAction | None,
                await retention_store.find_retention_by_key(request.idempotency_key),
            )
            if existing is not None:
                if existing.fingerprint != request.fingerprint:
                    raise RetentionConflictError("idempotency key conflicts with retention command")
                return existing
        await retention_store.enqueue_retention(action)
        if not self._durable_retention:
            asyncio.create_task(self._run_retention(action))
        self.metrics.record(
            "observatory_retention_action_count",
            labels={"action": action.kind.value, "status": action.status.value},
        )
        return action

    async def retention_status(self, action_id: str) -> RetentionAction | None:
        return cast(RetentionAction | None, await self.retention_port.get_retention(action_id))

    async def persisted_metric_series(self) -> list[dict[str, object]]:
        return await self.retention_port.persisted_metric_series()

    async def recover_pending_retention(self) -> None:
        """Resume durable queued/running commands after a worker restart."""

        for value in await self.retention_port.pending_retentions():
            asyncio.create_task(self._run_retention(cast(RetentionAction, value)))

    async def run_retention_worker(self, stop: Any = None) -> None:
        """Continuously poll durable retention actions in the worker process."""

        while True:
            for value in await self.retention_port.pending_retentions():
                await self._run_retention(cast(RetentionAction, value))
            if stop is None:
                await asyncio.sleep(0.5)
                continue
            if hasattr(stop, "is_set"):
                if stop.is_set():
                    return
                await asyncio.sleep(0.5)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(stop), timeout=0.5)
                return
            except TimeoutError:
                continue

    async def _run_retention(self, action: RetentionAction) -> None:
        # A failed action is recoverable: target scope and progress are
        # durable and the worker must be able to reclaim it.  Only a
        # completed deletion with a successful status is terminal.
        if action.status is RetentionStatus.SUCCEEDED and action.deletion_phase == "completed":
            return
        claimed = await self.retention_port.claim_pending_retention(action.action_id)
        if claimed is None:
            reference = cast(ObservatoryReferencePort, self.store)
            if self._durable_retention or action.action_id in reference.actions:
                return
        else:
            action = cast(RetentionAction, claimed)
        action.status = RetentionStatus.RUNNING
        action.started_at = datetime.now(UTC)
        phase = "target_capture"
        try:
            # A restarted worker must bind the durable target scope before
            # deletion even when prepare_retention is correctly skipped for a
            # non-queued phase. This never retargets an existing action.
            await self.retention_port.attach_retention(action)
            if action.deletion_phase == "queued":
                await self.retention_port.prepare_retention(action)
            phase = "storage_cleanup"
            deleted = await self.store.delete_observations(action.cutoff)
            action.deleted_count = deleted.observations
            action.deleted_receipt_count = deleted.receipts
            action.deleted_run_count = deleted.runs
            action.deletion_phase = "completed"
            action.status = RetentionStatus.SUCCEEDED
            self.metrics.record(
                "observatory_retention_deleted_count",
                float(action.deleted_count),
                {"action": action.kind.value},
            )
            action.completed_at = datetime.now(UTC)
        except Exception as exc:  # pragma: no cover - adapter failure path
            _LOGGER.error(
                "retention storage failure phase=%s exception_class=%s",
                phase,
                _retention_exception_class(exc),
            )
            action.status = RetentionStatus.FAILED
            action.error = "storage_failure"
            action.completed_at = datetime.now(UTC)
            self.metrics.record(
                "observatory_retention_failure_count",
                labels={"action": action.kind.value, "failure_code": "storage_failure"},
            )
            del exc
        try:
            await self.retention_port.update_retention(action)
        except Exception as exc:
            # A terminal audit write is itself part of the durable command.
            # Keep the result counts, but make the in-memory action
            # recoverable rather than falsely reporting success.  The durable
            # row remains claimable if this write failed.
            action.status = RetentionStatus.FAILED
            action.error = "storage_failure"
            action.completed_at = datetime.now(UTC)
            _LOGGER.error(
                "retention storage failure phase=audit_update exception_class=%s",
                _retention_exception_class(exc),
            )
            self.metrics.record(
                "observatory_retention_failure_count",
                labels={"action": action.kind.value, "failure_code": "storage_failure"},
            )


def _validate_retention(request: RetentionRequest, *, strict_confirmation: bool = False) -> None:
    if not request.idempotency_key or len(request.idempotency_key) > 200:
        raise ValueError("idempotency key is required")
    expected = request.kind.value
    expected_wire = expected.replace("_", "-")
    allowed = (
        {expected_wire}
        if strict_confirmation
        else {
            expected,
            expected_wire,
            f"confirm_{expected}",
            f"confirm-{expected_wire}",
            "confirm",
        }
    )
    if request.confirmation.casefold() not in allowed:
        raise ValueError("explicit retention confirmation is required")
    if request.kind is RetentionKind.DELETE_BEFORE:
        if request.cutoff is None or request.cutoff.tzinfo is None:
            raise ValueError("delete_before requires a timezone-aware UTC cutoff")
        if request.cutoff.utcoffset() != timedelta(0):
            raise ValueError("cutoff must be expressed in UTC")
    elif request.cutoff is not None:
        raise ValueError("purge_all must not include a cutoff")


def _retention_from_row(row: Any, idempotency_key: str) -> RetentionAction:
    cutoff = row[2]
    requested = row[4]
    started = row[5]
    completed = row[6]
    return RetentionAction(
        str(row[0]),
        RetentionKind(str(row[1])),
        cutoff.astimezone(UTC) if cutoff is not None else None,
        RetentionStatus(str(row[3])),
        requested_at=requested.astimezone(UTC) if requested is not None else datetime.now(UTC),
        started_at=started.astimezone(UTC) if started is not None else None,
        completed_at=completed.astimezone(UTC) if completed is not None else None,
        deleted_count=int(row[7] or 0),
        deleted_receipt_count=int(row[8] or 0),
        deleted_run_count=int(row[9] or 0),
        error=str(row[10]) if row[10] else None,
        idempotency_key=str(row[11] or idempotency_key),
        target_observation_count=int(row[12] or 0) if len(row) > 12 else 0,
        target_receipt_count=int(row[13] or 0) if len(row) > 13 else 0,
        target_run_count=int(row[14] or 0) if len(row) > 14 else 0,
        deletion_phase=str(row[15] or "queued") if len(row) > 15 else "queued",
    )


def _validate_analytics_request(request: AnalyticsRequest) -> None:
    definition = _analytics_definition(request.metric)
    if definition is None or request.unit not in ANALYTICS_UNITS:
        raise ValueError("unsupported analytics metric or unit")
    if request.unit != definition["unit"]:
        raise ValueError("analytics metric and unit do not match")
    supported_aggregations = cast(tuple[str, ...], definition["supportedAggregations"])
    if request.aggregation not in supported_aggregations:
        raise ValueError("unsupported analytics aggregation")
    if request.focus not in ANALYTICS_FOCUSES or request.group_by not in ANALYTICS_GROUPINGS:
        raise ValueError("unsupported analytics focus or grouping")
    supported_focuses = cast(tuple[str, ...], definition["focusAreas"])
    if request.focus not in supported_focuses:
        raise ValueError("analytics focus is not supported for this metric")
    supported_groupings = cast(tuple[str, ...], definition["supportedGroupings"])
    if request.group_by not in supported_groupings:
        raise ValueError("analytics grouping is not supported for this metric")
    if not 1 <= request.series_limit <= ANALYTICS_MAX_SERIES:
        raise ValueError("analytics series limit is outside the allowed bound")
    if not 1 <= request.point_limit <= ANALYTICS_MAX_POINTS:
        raise ValueError("analytics point limit is outside the allowed bound")
    if set(request.filters) - ANALYTICS_FILTER_KEYS:
        raise ValueError("unsupported analytics filter")
    for key, value in request.filters.items():
        if key == "component" and _ANALYTICS_COMPONENT_RE.fullmatch(value) is None:
            raise ValueError("invalid analytics component filter")
        if key == "operation" and _ANALYTICS_OPERATION_RE.fullmatch(value) is None:
            raise ValueError("invalid analytics operation filter")
        if key == "environment" and _ANALYTICS_ENVIRONMENT_RE.fullmatch(value) is None:
            raise ValueError("invalid analytics environment filter")
        if (
            key != "component"
            and key not in {"operation", "environment"}
            and _ANALYTICS_VALUE_RE.fullmatch(value) is None
        ):
            raise ValueError("invalid analytics filter value")


def _analytics_dimension(item: MetadataObservation, name: str) -> str:
    if name == "component":
        return item.component.id
    if name == "component_version":
        return item.component.version
    if name == "operation":
        return item.operation
    if name == "deployment":
        return item.deployment.deployment_id
    if name == "environment":
        return item.deployment.environment
    if name == "host":
        return item.deployment.host_id
    if name == "service_role":
        return item.service_role
    if name == "provider":
        return item.dimensions.get("provider", "unknown")
    if name == "outcome":
        return _normalize_outcome(item)
    if name == "error_class":
        return item.dimensions.get("errorClass", "unknown")
    if name == "status":
        return item.dimensions.get("status", "unknown")
    if name == "retrieval_stage":
        return item.dimensions.get("retrievalStage", "unknown")
    return "all"


def _analytics_metric_matches(item: MetadataObservation, metric: str) -> bool:
    aliases = {
        "operation_duration_ms": {"duration_ms", "operation_duration_ms"},
        "run_duration_ms": {"run_duration_ms"},
    }
    return item.measurement.name in aliases.get(metric, {metric})


def _analytics_percentile(values: list[float], rank: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # ClickHouse quantileExact selects the discrete sample at ``rank * n``;
    # it does not interpolate between adjacent observations.  Keep the index
    # formula identical to the pinned provider's nearest-rank aggregate for
    # reference parity, clamping the upper endpoint for rank 1.0.
    index = int(len(ordered) * rank)
    return ordered[min(len(ordered) - 1, max(0, index))]


def _analytics_aggregate(
    values: list[float], aggregation: str, interval_seconds: int
) -> float | None:
    if not values:
        return None
    if aggregation in {"count", "sum"}:
        return float(len(values) if aggregation == "count" else sum(values))
    if aggregation == "average":
        return sum(values) / len(values)
    if aggregation == "minimum":
        return min(values)
    if aggregation == "maximum":
        return max(values)
    if aggregation == "rate":
        return sum(values) / max(60, min(86_400, interval_seconds))
    percentile = {"p50": 0.50, "p90": 0.90, "p95": 0.95, "p99": 0.99}.get(aggregation)
    return _analytics_percentile(values, percentile) if percentile is not None else None


def _reference_analytics_snapshot(
    request: AnalyticsRequest,
    observations: list[MetadataObservation],
    bucket_seconds: int,
    *,
    source_truncated: bool = False,
) -> dict[str, object]:
    lower = cast(datetime, request.from_time).astimezone(UTC)
    upper = cast(datetime, request.to_time).astimezone(UTC)
    selected = [
        item
        for item in observations
        if lower <= item.observed_at.astimezone(UTC) < upper
        and _analytics_metric_matches(item, request.metric)
        and item.measurement.unit == request.unit
        and all(_analytics_dimension(item, key) == value for key, value in request.filters.items())
    ]
    latest = max((item.observed_at.astimezone(UTC) for item in selected), default=None)
    groups: dict[str, list[MetadataObservation]] = defaultdict(list)
    for item in selected:
        group = _analytics_dimension(item, request.group_by)
        groups[group].append(item)
    ordered_groups = sorted(groups.items(), key=lambda pair: pair[0])
    partial_reasons: list[str] = ["scan_limit"] if source_truncated else []
    if len(ordered_groups) > request.series_limit:
        ordered_groups = ordered_groups[: request.series_limit]
        partial_reasons.append("series_limit")
    series: list[dict[str, object]] = []
    for group, group_items in ordered_groups:
        buckets: dict[datetime, list[float]] = defaultdict(list)
        for item in group_items:
            delta = int((item.observed_at.astimezone(UTC) - lower).total_seconds())
            start = lower + timedelta(seconds=(delta // bucket_seconds) * bucket_seconds)
            buckets[start].append(float(item.measurement.value))
        points: list[dict[str, object]] = []
        point_limited = len(buckets) > request.point_limit
        for start in sorted(buckets)[: request.point_limit]:
            values = buckets[start]
            points.append(
                {
                    "startAt": _iso(start),
                    "endAt": _iso(min(start + timedelta(seconds=bucket_seconds), upper)),
                    "value": _analytics_aggregate(values, request.aggregation, bucket_seconds),
                    "sampleCount": len(values),
                    "availability": "available" if values else "no_data",
                    "partial": bool(source_truncated or point_limited),
                }
            )
        if len(buckets) > request.point_limit:
            partial_reasons.append("point_limit")
        all_values = [float(item.measurement.value) for item in group_items]
        summary_interval_seconds = min(
            86_400, max(60, int((upper - lower).total_seconds()))
        )
        summary_window_seconds = max(1, int((upper - lower).total_seconds()))
        rate = (
            sum(all_values) * summary_interval_seconds / summary_window_seconds
            if all_values
            else None
        )
        summary = {
            "sampleCount": len(all_values),
            "minimum": min(all_values) if all_values else None,
            "maximum": max(all_values) if all_values else None,
            "average": sum(all_values) / len(all_values) if all_values else None,
            "p50": _analytics_percentile(all_values, 0.50),
            "p90": _analytics_percentile(all_values, 0.90),
            "p95": _analytics_percentile(all_values, 0.95),
            "p99": _analytics_percentile(all_values, 0.99),
            "ratePerInterval": rate if all_values else None,
            "rateIntervalSeconds": summary_interval_seconds if all_values else None,
        }
        facets: dict[str, str] = {}
        if request.group_by != "none":
            facets[_camel_analytics_group(request.group_by)] = group
        series.append(
            {
                "seriesKey": f"{request.metric}:{request.unit}:{group}",
                "facets": facets,
                "summary": summary,
                "points": points,
                "availability": "available" if all_values else "no_data",
                "partial": bool(partial_reasons or point_limited),
            }
        )
    breakdowns = [
        {"groupBy": request.group_by, "value": group, "count": len(items), "partial": False}
        for group, items in sorted(groups.items(), key=lambda pair: pair[0])[
            :ANALYTICS_MAX_BREAKDOWNS
        ]
        if request.group_by != "none"
    ]
    if len(groups) > ANALYTICS_MAX_BREAKDOWNS:
        partial_reasons.append("series_limit")
    source_partial = "scan_limit" in partial_reasons
    for item in series:
        if source_partial:
            item["partial"] = True
        for point in cast(list[dict[str, object]], item["points"]):
            if source_partial:
                point["partial"] = True
    lag_freshness = analytics_freshness(latest)
    for breakdown in breakdowns:
        breakdown["partial"] = source_partial
    return {
        "snapshotAt": _iso(datetime.now(UTC)),
        "query": {
            "metric": request.metric,
            "unit": request.unit,
            "aggregation": request.aggregation,
            "focus": request.focus,
            "from": _iso(lower),
            "to": _iso(upper),
            "bucket": request.bucket,
            "bucketSeconds": bucket_seconds,
            "groupBy": request.group_by,
            "filters": {
                _camel_analytics_group(key): value for key, value in request.filters.items()
            },
            "seriesLimit": request.series_limit,
            "pointLimit": request.point_limit,
        },
        "percentileMethod": ANALYTICS_PERCENTILE_METHOD,
        "series": series,
        "breakdowns": breakdowns,
        "freshness": lag_freshness,
        "partial": bool(partial_reasons),
        "partialReasons": sorted(set(partial_reasons)),
        "unavailableSignals": analytics_unavailable(request.metric),
        "scan": {
            "scannedRows": min(len(observations), ANALYTICS_MAX_SCANNED_ROWS),
            "scanLimit": ANALYTICS_MAX_SCANNED_ROWS,
            "executionMs": 0.0,
            "executionLimitMs": ANALYTICS_EXECUTION_LIMIT_MS,
        },
        "capturePolicy": "metadata_only",
    }


def _camel_analytics_group(value: str) -> str:
    return {
        "component_version": "componentVersion",
        "service_role": "serviceRole",
        "error_class": "errorClass",
        "retrieval_stage": "retrievalStage",
    }.get(value, value)


def _matches(
    item: MetadataObservation, filters: RunFilters, *, include_status: bool = True
) -> bool:
    if filters.component and item.component.id != filters.component:
        return False
    if filters.deployment and item.deployment.deployment_id != filters.deployment:
        return False
    if include_status and filters.status and _run_status(item) != filters.status:
        return False
    if filters.model and item.dimensions.get("provider") != filters.model:
        return False
    when = item.observed_at.astimezone(UTC)
    return not (
        (filters.from_time and when < filters.from_time.astimezone(UTC))
        or (filters.to_time and when >= filters.to_time.astimezone(UTC))
    )


def _run_dict(
    run_id: str,
    items: list[MetadataObservation],
    newest: MetadataObservation,
    *,
    include_spans: bool = False,
) -> dict[str, Any]:
    # A run's last metadata observation is not necessarily a span.  In
    # particular, terminal measurements can have no trace lineage even when
    # an earlier span established the run's trace.  Select the newest
    # non-empty lineage independently from the newest status/deployment/model
    # observation, using the same stable timestamp/observation ordering as
    # the run grouping and cursor semantics.
    trace_items = [item for item in items if item.trace is not None and item.trace.trace_id]
    latest_trace = (
        max(trace_items, key=lambda item: (item.observed_at, item.observation_id))
        if trace_items
        else None
    )
    result: dict[str, Any] = {
        "runId": run_id,
        "status": _run_status(newest),
        "deploymentId": newest.deployment.deployment_id,
        "startedAt": _iso(min(item.observed_at for item in items)),
        "finishedAt": _iso(max(item.observed_at for item in items)),
        "durationMs": (
            (
                max(item.observed_at for item in items) - min(item.observed_at for item in items)
            ).total_seconds()
            * 1000
        ),
        "model": newest.dimensions.get("provider"),
        "componentIds": sorted({item.component.id for item in items}),
        "traceId": latest_trace.trace.trace_id if latest_trace and latest_trace.trace else None,
    }
    if include_spans:
        result["observations"] = [_observation_dict(item) for item in items]
    return result


class _ReferenceRunDetail(dict[str, Any]):
    """Keep legacy in-memory assertions virtual, never serialized publicly.

    The observatory-v1 payload is closed.  The reference store historically
    exposed aggregate/tree keys to unit callers; virtual accessors preserve
    that local compatibility while ``keys()`` and JSON serialization retain
    only the contract fields.
    """

    def __init__(self, value: dict[str, Any], items: list[MetadataObservation]) -> None:
        super().__init__(value)
        self._items = items

    def __getitem__(self, key: str) -> Any:
        if key == "observationCount":
            return len(self._items)
        if key == "componentCount":
            return len({item.component.id for item in self._items})
        if key == "lastSeenAt":
            return _iso(max(item.observed_at for item in self._items))
        if key == "spans":
            spans = [
                _span_dict(item)
                for item in sorted(
                    self._items, key=lambda item: (item.observed_at, item.observation_id)
                )
                if item.trace and item.trace.span_id
            ]
            return _tree(spans)
        return super().__getitem__(key)


def _run_status(observation: MetadataObservation) -> str:
    value = observation.dimensions.get("status", observation.dimensions.get("outcome", "unknown"))
    return {
        "completed": "succeeded",
        "success": "succeeded",
        "error": "failed",
    }.get(
        value,
        value
        if value in {"queued", "running", "succeeded", "failed", "cancelled"}
        else _normalize_outcome(observation),
    )


def _normalize_outcome(observation: MetadataObservation) -> str:
    # Prefer the explicit outcome dimension and fall back to legacy status.
    # This exact alias map is shared by grouping, filtering, and the in-memory
    # reference adapter.
    value = (
        observation.dimensions.get("outcome")
        or observation.dimensions.get("status")
        or "unknown"
    ).casefold()
    return {
        "success": "succeeded",
        "completed": "succeeded",
        "ok": "succeeded",
        "error": "failed",
    }.get(value, value if value in {"succeeded", "failed", "cancelled"} else "unknown")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _span_dict(item: MetadataObservation) -> dict[str, Any]:
    trace = item.trace
    assert trace is not None and trace.span_id is not None
    return {
        "spanId": trace.span_id,
        "parentSpanId": trace.parent_span_id,
        "componentId": item.component.id,
        "operation": item.operation,
        "durationMs": item.measurement.value if item.measurement.unit == "milliseconds" else None,
        "outcome": _normalize_outcome(item),
        "startedAt": _iso(item.observed_at),
        "children": [],
        "partial": False,
    }


def _observation_dict(item: MetadataObservation) -> dict[str, Any]:
    return {
        "observationId": item.observation_id,
        "observedAt": _iso(item.observed_at),
        "componentId": item.component.id,
        "componentVersion": item.component.version,
        "operation": item.operation,
        "measurement": {
            "name": item.measurement.name,
            "value": item.measurement.value,
            "unit": item.measurement.unit,
        },
        "outcome": _normalize_outcome(item),
        "deploymentId": item.deployment.deployment_id,
        "serviceRole": item.service_role,
        "capturePolicy": item.capture_policy,
    }


def _tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a deterministic forest without dropping invalid lineage.

    Telemetry can arrive out of order or with malformed parent references. A
    cyclic parent chain must not make the affected nodes unreachable, so edges
    within a detected cycle are detached and each cycle member becomes a root.
    Orphan and cyclic traces are marked partial on every returned span.
    """

    ordered = sorted(
        spans,
        key=lambda item: (str(item.get("startedAt") or ""), str(item["spanId"])),
    )
    by_id = {str(item["spanId"]): item for item in ordered}
    for span in ordered:
        span["children"] = []
        span["partial"] = False

    orphan_nodes: set[str] = set()
    cycle_nodes: set[str] = set()
    complete: set[str] = set()
    for start in by_id:
        if start in complete:
            continue
        path: list[str] = []
        path_index: dict[str, int] = {}
        current = start
        while current in by_id and current not in complete:
            if current in path_index:
                cycle_nodes.update(path[path_index[current] :])
                break
            path_index[current] = len(path)
            path.append(current)
            parent = by_id[current].get("parentSpanId")
            if not parent:
                break
            parent_id = str(parent)
            if parent_id not in by_id:
                orphan_nodes.add(current)
                break
            if parent_id == current:
                cycle_nodes.add(current)
                break
            current = parent_id
        complete.update(path)

    partial = bool(orphan_nodes or cycle_nodes)
    roots: list[dict[str, Any]] = []
    for span in ordered:
        span_id = str(span["spanId"])
        parent = span.get("parentSpanId")
        parent_id = str(parent) if parent else None
        in_cycle_edge = span_id in cycle_nodes and parent_id in cycle_nodes
        if parent_id in by_id and parent_id != span_id and not in_cycle_edge:
            cast(list[dict[str, Any]], by_id[parent_id]["children"]).append(span)
        else:
            roots.append(span)

    for span in ordered:
        children = cast(list[dict[str, Any]], span["children"])
        children.sort(key=lambda item: (str(item.get("startedAt") or ""), str(item["spanId"])))
        span["partial"] = partial
    roots.sort(key=lambda item: (str(item.get("startedAt") or ""), str(item["spanId"])))
    return roots


__all__ = [
    "ObservatoryQueryService",
    "RetentionAction",
    "RetentionKind",
    "RetentionRequest",
    "RetentionStatus",
    "RunFilters",
    "decode_cursor",
    "encode_cursor",
]

# Public adapter helpers keep persistence adapters from depending on private
# use-case implementation names.
iso = _iso
run_dict = _run_dict
span_dict = _span_dict
retention_from_row = _retention_from_row
sql_time = _sql_time
tree = _tree
