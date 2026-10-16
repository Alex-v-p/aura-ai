"""Owner-facing Observatory query and retention use cases."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import (
    DeletionResult,
    ObservatoryHealthPort,
    ObservatoryQueryPort,
    ObservatoryReferencePort,
    ObservatoryRetentionPort,
)

_LOGGER = logging.getLogger(__name__)
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


def _retention_exception_class(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _RETENTION_EXCEPTION_CLASSES else "other"

_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{16,1024}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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
    async def list_observations(self) -> list[MetadataObservation]: ...

    async def components(self) -> list[dict[str, Any]]: ...

    async def deployments(self) -> list[dict[str, Any]]: ...

    async def ingest_overview(self) -> dict[str, Any]: ...

    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult: ...


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
        self.retention_port = retention_port or cast(ObservatoryRetentionPort, store)
        self.health_port = health_port or cast(ObservatoryHealthPort, store)
        self._durable_retention = retention_port is not None

    @_query_resource("overview")
    async def overview(self) -> dict[str, Any]:
        started = monotonic()
        if self.query_port is not None:
            narrow = self.query_port
            started = monotonic()
            result = await narrow.overview_query()
            health = result.get("health")
            self._record_health_metrics(
                cast(dict[str, object] | None, health)
                if isinstance(health, dict)
                else None
            )
            self._record_query_metrics("overview", started, "succeeded")
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
        self._record_query_metrics("overview", started, "succeeded")
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
        started = monotonic()
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
        self._record_query_metrics("health_history", started, "succeeded")
        return result

    @_query_resource("runs")
    async def runs(
        self, filters: RunFilters, *, cursor: str | None, limit: int = 50
    ) -> dict[str, Any]:
        started = monotonic()
        if self.query_port is not None:
            narrow = self.query_port
            started = monotonic()
            result = await narrow.run_query(filters, cursor=cursor, limit=limit)
            self._record_query_metrics("runs", started, "succeeded")
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
        self._record_query_metrics("runs", started, "succeeded")
        return result

    @_query_resource("run_detail")
    async def run_detail(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        started = monotonic()
        if self.query_port is not None:
            narrow = self.query_port
            result = await narrow.run_detail_query(run_id, cursor=cursor, limit=limit)
            self._record_query_metrics("run_detail", started, "succeeded")
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
                if (item.observed_at.astimezone(UTC), item.observation_id)
                > (cursor_at, cursor_id)
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
        self._record_query_metrics("run_detail", started, "succeeded")
        return _ReferenceRunDetail(result, page)

    @_query_resource("trace_detail")
    async def trace_detail(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        started = monotonic()
        if self.query_port is not None:
            narrow = self.query_port
            result_value = await narrow.trace_detail_query(
                trace_id, cursor=cursor, limit=limit
            )
            self._record_query_metrics("trace_detail", started, "succeeded")
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
                if (item.observed_at.astimezone(UTC), item.observation_id)
                > (cursor_at, cursor_id)
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
        self._record_query_metrics("trace_detail", started, "succeeded")
        return result

    async def components(self) -> list[dict[str, Any]]:
        started = monotonic()
        try:
            result = (
                await self.query_port.components_query()
                if self.query_port is not None
                else await self.store.components()
            )
        except Exception:
            self._record_query_metrics("components", started, "failed")
            await self._record_query_health_latency(started)
            raise
        self._record_query_metrics("components", started, "succeeded")
        await self._record_query_health_latency(started)
        return result

    @_query_resource("component_health")
    async def component_health(
        self,
        component_id: str,
        *,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any] | None:
        started = monotonic()
        if self.query_port is not None:
            narrow = self.query_port
            result_value = await narrow.component_health_query(
                component_id, from_time, to_time
            )
            self._record_query_metrics("component_health", started, "succeeded")
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
            duration.measurement.value
            for duration, _outcome in canonical
            if duration is not None
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
        self._record_query_metrics("component_health", started, "succeeded")
        return result

    def _record_query_metrics(self, resource: str, started: float, outcome: str) -> None:
        self.metrics.record(
            "observatory_query_duration_ms",
            (monotonic() - started) * 1000.0,
            {"resource": resource},
        )
        self.metrics.record(
            "observatory_query_outcome", labels={"resource": resource, "outcome": outcome}
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
            self.metrics.record(
                "observatory_sse_active", -1.0, {"event_type": "stream"}
            )

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
        self.metrics.record(
            "observatory_sse_connection_count", labels={"event_type": "connection"}
        )
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
                    raise RetentionConflictError(
                        "idempotency key conflicts with retention command"
                    )
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
    trace_items = [
        item for item in items if item.trace is not None and item.trace.trace_id
    ]
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
    value = observation.dimensions.get("outcome", observation.dimensions.get("status", "unknown"))
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
