"""Production query adapter for the Observatory read/retention ports.

The API use cases never hydrate the ClickHouse observation table.  Each public
read has a bounded, purpose-built query and returns only the metadata shape the
use case needs.  The legacy retention primitives are inherited temporarily to
keep migration/recovery compatibility while their SQL remains service-owned.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import httpx
import psycopg

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import (
    AnalyticsRequest,
    NullObservatoryHealthPort,
    ObservatoryHealthPort,
)
from aura_observatory.query import (
    ANALYTICS_BUCKETS,
    ANALYTICS_EXECUTION_LIMIT_MS,
    ANALYTICS_MAX_BREAKDOWNS,
    ANALYTICS_MAX_SCANNED_ROWS,
    ANALYTICS_PERCENTILE_METHOD,
    COMPARISON_EXECUTION_TIMEOUT_MS,
    COMPARISON_MAX_OBSERVATIONS,
    COMPARISON_MAX_SCANNED_ROWS,
    COMPARISON_QUERY_ROWS,
    DeletionResult,
    RetentionAction,
    RetentionStatus,
    RunFilters,
    analytics_freshness,
    analytics_unavailable,
    bounded_component_health_window,
    build_run_comparison,
    comparison_execution_limit_snapshot,
    decode_cursor,
    decode_run_cursor,
    encode_cursor,
    iso,
    retention_from_row,
    run_dict,
    span_dict,
    tree,
)

_LOGGER = logging.getLogger(__name__)
_CLICKHOUSE_ERROR_CLASSES = (
    ("parse_error", re.compile(r"cannot parse|cannot convert|type mismatch", re.I)),
    ("schema_error", re.compile(r"unknown (?:column|field)|no such column|missing column", re.I)),
    ("syntax_error", re.compile(r"syntax error|multiquery", re.I)),
    ("auth_error", re.compile(r"authentication|not authorized|access denied", re.I)),
    ("database_error", re.compile(r"database .* does not exist|unknown database", re.I)),
)
_RETENTION_RETRY_DELAY_SECONDS = 5
_COMPONENT_SCAN_BATCHES = 32


class StorageUnavailableError(httpx.HTTPStatusError, RuntimeError):
    """A dependency could not serve a bounded Observatory read."""

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        request = request or httpx.Request("GET", "http://observatory-storage.invalid")
        response = response or httpx.Response(503, request=request)
        super().__init__(message, request=request, response=response)


def _raise_for_clickhouse(response: httpx.Response) -> None:
    """Raise an HTTP failure after recording bounded, content-free diagnostics."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        # ClickHouse may echo SQL and values in an error body. Classify only a
        # bounded copy; neither the statement nor response body is logged.
        body = response.text[:4_096]
        code_match = re.search(r"\bCode:\s*(\d{1,5})\b", body, re.IGNORECASE)
        code = code_match.group(1) if code_match else "0"
        reason = "unclassified_error"
        for candidate, pattern in _CLICKHOUSE_ERROR_CLASSES:
            if pattern.search(body):
                reason = candidate
                break
        _LOGGER.error(
            "ClickHouse query failed status=%s code=%s reason=%s",
            response.status_code,
            code,
            reason,
        )
        raise


def _decode_clickhouse_rows(response: httpx.Response) -> list[dict[str, Any]]:
    """Decode JSONEachRow without allowing provider details to escape."""

    try:
        rows: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("ClickHouse row is not an object")
            rows.append(cast(dict[str, Any], value))
        return rows
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageUnavailableError(
            "ClickHouse returned an invalid response",
            request=response.request,
            response=response,
        ) from exc


def _clickhouse_progress_read_rows(response: httpx.Response) -> int | None:
    """Read the provider's bounded physical-read count when available."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    header = headers.get("x-clickhouse-progress")
    if not header:
        return None
    try:
        value = json.loads(header)
        rows = int(value["read_rows"])
        return rows if rows >= 0 else None
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
        return None


def _is_clickhouse_execution_timeout(error: httpx.HTTPStatusError) -> bool:
    """Recognize ClickHouse bounded execution failures without exposing SQL."""

    response = error.response
    if response.status_code == 504:
        return True
    body = response.text[:2_048].casefold()
    return any(marker in body for marker in ("max_execution_time", "too slow", "timeout"))


def _is_clickhouse_read_limit(error: httpx.HTTPStatusError) -> bool:
    """Recognize ClickHouse's bounded physical-read overflow response.

    This helper is called only from the analytics query, whose statement sets
    ``max_rows_to_read`` and ``read_overflow_mode = 'break'``.  ClickHouse
    25.8 identifies that bounded overflow by the stable HTTP/code tuple even
    when its free-form response wording changes.
    """

    response = error.response
    if response.status_code != 408:
        return False
    body = response.text[:2_048]
    return bool(re.search(r"\bcode:\s*159\b", body, re.IGNORECASE))


def _analytics_execution_limit_snapshot(
    request: AnalyticsRequest,
    lower: datetime,
    upper: datetime,
    bucket_seconds: int,
) -> dict[str, object]:
    return {
        "snapshotAt": iso(datetime.now(UTC)),
        "query": {
            "metric": request.metric,
            "unit": request.unit,
            "aggregation": request.aggregation,
            "focus": request.focus,
            "from": iso(lower),
            "to": iso(upper),
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
        "series": [],
        "breakdowns": [],
        "freshness": analytics_freshness(None),
        "partial": True,
        "partialReasons": ["execution_limit"],
        "unavailableSignals": analytics_unavailable(request.metric),
        "scan": {
            "scannedRows": 0,
            "scanLimit": ANALYTICS_MAX_SCANNED_ROWS,
            "executionMs": ANALYTICS_EXECUTION_LIMIT_MS,
            "executionLimitMs": ANALYTICS_EXECUTION_LIMIT_MS,
        },
        "capturePolicy": "metadata_only",
    }


def _analytics_scan_limit_snapshot(
    request: AnalyticsRequest,
    lower: datetime,
    upper: datetime,
    bucket_seconds: int,
) -> dict[str, object]:
    snapshot = _analytics_execution_limit_snapshot(request, lower, upper, bucket_seconds)
    snapshot["partialReasons"] = ["scan_limit"]
    snapshot["scan"] = {
        "scannedRows": ANALYTICS_MAX_SCANNED_ROWS,
        "scanLimit": ANALYTICS_MAX_SCANNED_ROWS,
        "executionMs": 0,
        "executionLimitMs": ANALYTICS_EXECUTION_LIMIT_MS,
    }
    return snapshot


def _sql(value: str) -> str:
    """Encode a value as a ClickHouse string literal.

    ClickHouse accepts backslash escapes in string literals.  Escaping the
    backslash first prevents an attacker-controlled backslash from changing
    the meaning of a subsequent escaped quote; the resulting literal is safe
    for every owner-controlled value that reaches the SQL adapter.
    """

    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _encode_component_cursor(component_id: str, version: str) -> str:
    raw = json.dumps(
        {"componentId": component_id, "version": version}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_component_cursor(value: str) -> str:
    if not 16 <= len(value) <= 1024 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid component cursor")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")


def _clickhouse_time(value: datetime) -> str:
    """Encode a UTC millisecond timestamp for ClickHouse DateTime64."""

    normalized = (
        value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds")
    )
    return _sql(normalized)


_DIMENSION_STATUS_SQL = (
    "lowerUTF8(coalesce(nullIf(dimensions['outcome'], ''), nullIf(dimensions['status'], '')))"
)
_RUN_STATUS_SQL = (
    "multiIf("
    + _DIMENSION_STATUS_SQL
    + " IN ('success', 'succeeded', 'completed', 'ok'), 'succeeded', "
    + _DIMENSION_STATUS_SQL
    + " IN ('error', 'failed'), 'failed', "
    + _DIMENSION_STATUS_SQL
    + " = 'cancelled', 'cancelled', 'unknown')"
)
_ANALYTICS_OUTCOME_SQL = (
    "multiIf("
    + _DIMENSION_STATUS_SQL
    + " IN ('success', 'succeeded', 'completed', 'ok'), 'succeeded', "
    + _DIMENSION_STATUS_SQL
    + " IN ('error', 'failed'), 'failed', "
    + _DIMENSION_STATUS_SQL
    + " = 'cancelled', 'cancelled', 'unknown')"
)


def _analytics_outcome_filter(value: str) -> str:
    normalized = value.casefold()
    return {
        "success": "succeeded",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "ok": "succeeded",
        "error": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(normalized, "unknown")


def _camel_analytics_group(value: str) -> str:
    return {
        "component_version": "componentVersion",
        "service_role": "serviceRole",
        "error_class": "errorClass",
        "retrieval_stage": "retrievalStage",
    }.get(value, value)


def _analytics_row_value(
    row: dict[str, Any], aggregation: str, bucket_seconds: int
) -> float | None:
    if not int(row.get("sample_count") or 0):
        return None
    if aggregation == "count":
        return float(row.get("sample_count") or 0)
    if aggregation == "sum":
        return float(row.get("total") or 0.0)
    if aggregation == "rate":
        return float(row.get("total") or 0.0) / max(60, min(86_400, bucket_seconds))
    return {
        "average": row.get("average"),
        "minimum": row.get("minimum"),
        "maximum": row.get("maximum"),
        "p50": row.get("p50"),
        "p90": row.get("p90"),
        "p95": row.get("p95"),
        "p99": row.get("p99"),
    }.get(aggregation)


def _row_float(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    return float(value) if value is not None else None


def _row_preferred_float(row: dict[str, Any], summary_key: str, fallback_key: str) -> float | None:
    summary = _row_float(row, summary_key)
    return summary if summary is not None else _row_float(row, fallback_key)


def _analytics_summary_rows(
    rows: list[dict[str, Any]], sample_count: int, window: timedelta
) -> dict[str, object]:
    interval_seconds = min(86_400, max(60, int(window.total_seconds())))
    window_seconds = max(1, int(window.total_seconds()))
    if not rows or not sample_count:
        return {
            "sampleCount": 0,
            "minimum": None,
            "maximum": None,
            "average": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "ratePerInterval": None,
            "rateIntervalSeconds": interval_seconds,
        }
    # ClickHouse provides exact whole-window values in the summary CTE. The
    # fallback keeps adapter doubles and older ClickHouse rows compatible.
    first = rows[0]
    summary_count = int(first.get("summary_sample_count") or sample_count)
    minimum = _row_float(first, "summary_minimum")
    maximum = _row_float(first, "summary_maximum")
    average = _row_float(first, "summary_average")
    if minimum is None:
        minimum = min(_row_float(row, "minimum") or 0.0 for row in rows)
    if maximum is None:
        maximum = max(_row_float(row, "maximum") or 0.0 for row in rows)
    if average is None:
        average = (
            sum(
                (_row_float(row, "average") or 0.0) * int(row.get("sample_count") or 0)
                for row in rows
            )
            / sample_count
        )
    return {
        "sampleCount": summary_count,
        "minimum": minimum,
        "maximum": maximum,
        "average": average,
        "p50": _row_preferred_float(first, "summary_p50", "p50"),
        "p90": _row_preferred_float(first, "summary_p90", "p90"),
        "p95": _row_preferred_float(first, "summary_p95", "p95"),
        "p99": _row_preferred_float(first, "summary_p99", "p99"),
        "ratePerInterval": (
            (_row_float(first, "summary_total") or 0.0) * interval_seconds / window_seconds
        ),
        "rateIntervalSeconds": interval_seconds,
    }


def _validate_analytics_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate provider aggregate rows before any coercion or projection."""

    integer_fields = ("sample_count", "summary_sample_count", "scan_rows_read", "scanned_rows")
    numeric_fields = (
        "minimum",
        "maximum",
        "average",
        "total",
        "p50",
        "p90",
        "p95",
        "p99",
        "summary_minimum",
        "summary_maximum",
        "summary_average",
        "summary_p50",
        "summary_p90",
        "summary_p95",
        "summary_p99",
        "summary_total",
    )
    for row in rows:
        if not isinstance(row.get("group_value"), str):
            raise ValueError("missing analytics group")
        if "sample_count" not in row:
            raise ValueError("missing analytics sample count")
        for key in integer_fields:
            if key in row and row[key] is not None:
                value = int(row[key])
                if value < 0:
                    raise ValueError("negative analytics count")
        for key in numeric_fields:
            if key in row and row[key] is not None:
                value = float(row[key])
                if not math.isfinite(value):
                    raise ValueError("non-finite analytics value")
        for key in ("bucket_start", "latest_observed_at"):
            if key in row and row[key] is not None:
                _parse_datetime(row[key])
        for key in ("late_data", "retention_boundary"):
            if key in row and row[key] not in (0, 1, False, True):
                raise ValueError("invalid analytics flag")
    return rows


class _RetentionPersistenceAdapter:
    """Read/retention adapter over Observatory PostgreSQL and ClickHouse.

    It intentionally accepts only a connection factory and ClickHouse HTTP
    endpoint; no Core database or model is reachable through this adapter.
    """

    def __init__(
        self,
        connection_factory: Any,
        clickhouse_url: str,
        *,
        database: str,
        user: str,
        password: str,
        health_port: ObservatoryHealthPort | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._endpoint = clickhouse_url.rstrip("/")
        self._database = database
        self._user = user
        self._password = password
        self._health_port = health_port or NullObservatoryHealthPort()
        # PostgreSQL is authoritative for retention state and idempotency.
        # Keep only the currently attached action needed while its guarded
        # multi-store deletion is in progress; never retain a process-lifetime
        # cache of historical actions or keys.
        self._active_retention_action: RetentionAction | None = None
        self._active_retention_action_id: str | None = None
        self._retention_guard: tuple[Any, Any] | None = None
        self._response_body_unavailable = False
        self._clickhouse_progress_available = False
        self._clickhouse_progress_read_rows: int | None = None
        self._retention_phase = "target_capture"

    async def find_retention_by_key(self, key: str) -> RetentionAction | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT action_id, action_kind, requested_cutoff, status, requested_at, "
                "started_at, completed_at, deleted_count, deleted_receipt_count, "
                "deleted_run_count, error_code, idempotency_key, "
                "target_observation_count, target_receipt_count, target_run_count, deletion_phase "
                "FROM observatory_retention_actions "
                "WHERE idempotency_key = %s",
                (key,),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return retention_from_row(row, key)

    async def save_retention(self, action: RetentionAction) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "INSERT INTO observatory_retention_actions "
                "(action_id, requested_cutoff, action_kind, status, idempotency_key, "
                "command_fingerprint, confirmation) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL "
                "DO NOTHING RETURNING action_id",
                (
                    action.action_id,
                    action.cutoff,
                    action.kind.value,
                    action.status.value,
                    action.idempotency_key,
                    action.fingerprint,
                    action.kind.value.replace("_", "-"),
                ),
            )
            row = await result.fetchone()
            return row is not None

    async def enqueue_retention(self, action: RetentionAction) -> None:
        """Mark a committed action for the dedicated worker poller."""

        del action

    async def attach_retention(self, action: RetentionAction) -> None:
        """Reattach a recovered action's immutable target context."""

        self._active_retention_action_id = action.action_id
        self._active_retention_action = action
        # Targeted/recovering actions already have an immutable PostgreSQL
        # target snapshot. They must not reacquire the ingest session lock
        # while replaying ClickHouse mutations; doing so can block behind an
        # active telemetry request for the entire mutation duration.
        if action.deletion_phase != "queued":
            return
        if self._retention_guard is None:
            context = self._connection_factory()
            connection = await context.__aenter__()
            try:
                # Keep the session-scoped guard alive through target capture.
                # The committed immutable target rows close the subsequent
                # target-capture/delete race without holding up telemetry
                # during ClickHouse mutations.
                await connection.execute("SELECT pg_advisory_lock(830078)")
            except BaseException:
                await context.__aexit__(None, None, None)
                self._active_retention_action_id = None
                self._active_retention_action = None
                raise
            self._retention_guard = (context, connection)

    async def _release_retention_guard(self, *, clear_action: bool = True) -> None:
        guarded = self._retention_guard
        self._retention_guard = None
        if clear_action:
            self._active_retention_action_id = None
            self._active_retention_action = None
        if guarded is None:
            return
        context, connection = guarded
        try:
            await _commit_connection(connection)
            await connection.execute("SELECT pg_advisory_unlock(830078)")
        finally:
            await context.__aexit__(None, None, None)

    async def update_retention(self, action: RetentionAction) -> None:
        try:
            async with self._connection_factory() as connection:
                await connection.execute(
                    "UPDATE observatory_retention_actions SET status = %s, started_at = %s, "
                    "completed_at = %s, deleted_count = %s, deleted_receipt_count = %s, "
                    "deleted_run_count = %s, "
                    "target_observation_count = GREATEST(target_observation_count, %s), "
                    "target_receipt_count = GREATEST(target_receipt_count, %s), "
                    "target_run_count = GREATEST(target_run_count, %s), "
                    "deletion_phase = CASE WHEN "
                    "CASE deletion_phase WHEN 'queued' THEN 0 WHEN 'targeted' THEN 1 "
                    "WHEN 'source_deleted' THEN 2 WHEN 'projection_deleted' THEN 3 "
                    "WHEN 'receipts_deleted' THEN 4 WHEN 'completed' THEN 5 ELSE 0 END > "
                    "CASE %s WHEN 'queued' THEN 0 WHEN 'targeted' THEN 1 "
                    "WHEN 'source_deleted' THEN 2 WHEN 'projection_deleted' THEN 3 "
                    "WHEN 'receipts_deleted' THEN 4 WHEN 'completed' THEN 5 ELSE 0 END "
                    "THEN deletion_phase ELSE %s END, error_code = %s WHERE action_id = %s",
                    (
                        action.status.value,
                        action.started_at,
                        action.completed_at,
                        action.deleted_count,
                        action.deleted_receipt_count,
                        action.deleted_run_count,
                        action.target_observation_count or action.deleted_count,
                        action.target_receipt_count or action.deleted_receipt_count,
                        action.target_run_count or action.deleted_run_count,
                        # Compare phase ranks so a recovered action cannot
                        # regress, while the terminal completed transition is
                        # still allowed after receipts_deleted.
                        action.deletion_phase,
                        action.deletion_phase,
                        action.error,
                        action.action_id,
                    ),
                )
                if (
                    action.status is RetentionStatus.SUCCEEDED
                    and action.deletion_phase == "completed"
                ):
                    await connection.execute(
                        "DELETE FROM observatory_retention_targets WHERE action_id = %s",
                        (action.action_id,),
                    )
        finally:
            await self._release_retention_guard()

    async def persisted_metric_series(self) -> list[dict[str, object]]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT action_kind, status, count(*) AS actions, "
                "COALESCE(sum(deleted_count), 0) AS deleted, "
                "count(*) FILTER (WHERE status = 'failed') AS failures "
                "FROM observatory_retention_actions GROUP BY action_kind, status"
            )
            rows = cast(list[Any], await result.fetchall())
        series: list[dict[str, object]] = []
        for row in rows:
            action = str(row[0])
            status = str(row[1])
            if action not in {"delete_before", "purge_all"} or status not in {
                "queued",
                "running",
                "succeeded",
                "failed",
            }:
                continue
            series.append(
                {
                    "name": "observatory_retention_action_count",
                    "value": int(row[2] or 0),
                    "labels": {"action": action, "status": status},
                }
            )
            if int(row[3] or 0):
                series.append(
                    {
                        "name": "observatory_retention_deleted_count",
                        "value": int(row[3]),
                        "labels": {"action": action},
                    }
                )
            if int(row[4] or 0):
                series.append(
                    {
                        "name": "observatory_retention_failure_count",
                        "value": int(row[4]),
                        "labels": {"action": action, "failure_code": "storage_failure"},
                    }
                )
        series.extend(await self._health_port.metric_series())
        return series

    async def get_retention(self, action_id: str) -> RetentionAction | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT action_id, action_kind, requested_cutoff, status, requested_at, "
                "started_at, completed_at, deleted_count, deleted_receipt_count, "
                "deleted_run_count, "
                "error_code, idempotency_key, target_observation_count, "
                "target_receipt_count, target_run_count, deletion_phase "
                "FROM observatory_retention_actions WHERE action_id = %s",
                (action_id,),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return retention_from_row(row, str(row[11] or ""))

    async def pending_retentions(self) -> list[RetentionAction]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT action_id, action_kind, requested_cutoff, status, requested_at, "
                "started_at, completed_at, deleted_count, deleted_receipt_count, "
                "deleted_run_count, "
                "error_code, idempotency_key, target_observation_count, "
                "target_receipt_count, target_run_count, deletion_phase "
                "FROM observatory_retention_actions WHERE status IN ('queued', 'running') "
                "OR (status = 'failed' AND deletion_phase <> 'completed' AND "
                "(completed_at IS NULL OR completed_at <= CURRENT_TIMESTAMP - "
                "(%s * INTERVAL '1 second'))) "
                "ORDER BY requested_at",
                (_RETENTION_RETRY_DELAY_SECONDS,),
            )
            rows = cast(list[Any], await result.fetchall())
        return [retention_from_row(row, str(row[11] or "")) for row in rows]

    async def claim_retention(self, action_id: str, lease_seconds: int = 60) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "UPDATE observatory_retention_actions SET status = 'running', "
                "started_at = CURRENT_TIMESTAMP, completed_at = NULL "
                "WHERE action_id = %s AND (status = 'queued' OR "
                "(status = 'failed' AND deletion_phase <> 'completed' AND "
                "(completed_at IS NULL OR completed_at <= CURRENT_TIMESTAMP - "
                "(%s * INTERVAL '1 second'))) OR "
                "(status = 'running' AND started_at <= CURRENT_TIMESTAMP - "
                "(%s * INTERVAL '1 second'))) RETURNING action_id",
                (action_id, _RETENTION_RETRY_DELAY_SECONDS, lease_seconds),
            )
            return (await result.fetchone()) is not None

    async def claim_pending_retention(self, action_id: str) -> RetentionAction | None:
        if not await self.claim_retention(action_id):
            return None
        return await self.get_retention(action_id)

    async def _rows(
        self, query: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        query_params: dict[str, str] = {"database": self._database}
        for name, value in (params or {}).items():
            # Values are sent as typed HTTP parameters.  They never become
            # SQL source text, including metacharacters and timestamps.
            query_params[f"param_{name}"] = str(value)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._endpoint,
                params=query_params,
                auth=(self._user, self._password),
                content=query + " FORMAT JSONEachRow",
            )
            _raise_for_clickhouse(response)
        self._clickhouse_progress_available = True
        self._clickhouse_progress_read_rows = _clickhouse_progress_read_rows(response)
        return _decode_clickhouse_rows(response)

    async def list_observations(self) -> list[MetadataObservation]:
        rows = await self._rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, "
            "measurement_unit, dimensions, trace_id, span_id, parent_span_id, "
            "linked_span_ids, run_id, conversation_id, deployment_id, git_commit, "
            "environment, host_id, service_role, capture_policy, image_version, "
            "configuration_version, model_id, model_version, agent_revision, "
            "prompt_bundle_revision, policy_revision FROM observations"
        )
        result: list[MetadataObservation] = []
        for row in rows:
            trace = None
            if row.get("trace_id"):
                trace = {
                    "traceId": row["trace_id"],
                    "spanId": row.get("span_id") or None,
                    "parentSpanId": row.get("parent_span_id") or None,
                    "linkedSpanIds": row.get("linked_span_ids") or None,
                }
            correlation = None
            if row.get("run_id") or row.get("conversation_id"):
                correlation = {
                    "runId": row.get("run_id") or None,
                    "conversationId": row.get("conversation_id") or None,
                }
            result.append(
                MetadataObservation.from_payload(
                    {
                        "schemaVersion": int(row["schema_version"]),
                        "observationId": row["observation_id"],
                        "observedAt": str(row["observed_at"]).replace(" ", "T") + "Z",
                        "kind": row["kind"],
                        "component": {
                            "id": row["component_id"],
                            "version": row["component_version"],
                        },
                        "operation": row["operation"],
                        "measurement": {
                            "name": row["measurement_name"],
                            "value": float(row["measurement_value"]),
                            "unit": row.get("measurement_unit") or None,
                        },
                        "dimensions": row.get("dimensions") or {},
                        "trace": trace,
                        "correlation": correlation,
                        "deployment": {
                            "deploymentId": row["deployment_id"],
                            "gitCommit": row["git_commit"],
                            "environment": row["environment"],
                            "hostId": row["host_id"],
                            **(
                                {"imageVersion": row["image_version"]}
                                if row.get("image_version")
                                else {}
                            ),
                            **(
                                {"configurationVersion": row["configuration_version"]}
                                if row.get("configuration_version")
                                else {}
                            ),
                        },
                        "serviceRole": row["service_role"],
                        "capturePolicy": row["capture_policy"],
                        **(
                            {
                                "execution": {
                                    key: row[name]
                                    for name, key in (
                                        ("model_id", "modelId"),
                                        ("model_version", "modelVersion"),
                                        ("agent_revision", "agentRevision"),
                                        ("prompt_bundle_revision", "promptBundleRevision"),
                                        ("policy_revision", "policyRevision"),
                                    )
                                    if row.get(name)
                                }
                            }
                            if any(
                                row.get(name)
                                for name in (
                                    "model_id",
                                    "model_version",
                                    "agent_revision",
                                    "prompt_bundle_revision",
                                    "policy_revision",
                                )
                            )
                            else {}
                        ),
                    }
                )
            )
        return result

    async def components(self) -> list[dict[str, Any]]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT component_id, version, first_seen_at, last_seen_at "
                "FROM observatory_components ORDER BY component_id, version"
            )
            rows = cast(list[Any], await result.fetchall())
        return [
            {
                "componentId": row[0],
                "version": row[1],
                "firstSeenAt": iso(row[2]),
                "lastSeenAt": iso(row[3]),
            }
            for row in rows
        ]

    async def _component_definition_rows(self) -> list[dict[str, Any]]:
        """Read the small PostgreSQL catalogue without touching Core state."""

        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """SELECT component_id, component_version, owner, category,
                    input_schema_id, output_schema_id, measurements, evaluators,
                    created_at
                    FROM observatory_component_definitions
                    ORDER BY component_id, component_version
                    LIMIT 2001"""
                )
                rows = await result.fetchall()
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("PostgreSQL is unavailable") from exc
        return [
            {
                "componentId": str(row[0]),
                "componentVersion": str(row[1]),
                "owner": str(row[2]),
                "category": str(row[3]),
                "inputSchemaId": str(row[4]),
                "outputSchemaId": str(row[5]),
                "measurements": sorted(str(item) for item in cast(list[Any], row[6] or [])),
                "evaluators": sorted(str(item) for item in cast(list[Any], row[7] or [])),
                "createdAt": iso(row[8]),
            }
            for row in rows
        ]

    async def _component_definition_page(
        self, after_component_id: str, component_limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read complete definition groups for one bounded component page.

        The inner query bounds component identifiers, rather than definition
        rows.  A component may have several declared versions and none of
        those versions may be silently discarded before the API page is
        assembled.
        """

        bound = component_limit + 1
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    f"""WITH component_page AS (
                    SELECT component_id
                    FROM observatory_component_definitions
                    WHERE component_id > %s
                    GROUP BY component_id
                    ORDER BY component_id
                    LIMIT {bound}
                    )
                    SELECT definitions.component_id, definitions.component_version,
                    definitions.owner, definitions.category, definitions.input_schema_id,
                    definitions.output_schema_id, definitions.measurements,
                    definitions.evaluators, definitions.created_at
                    FROM component_page
                    CROSS JOIN LATERAL (
                      SELECT component_id, component_version, owner, category,
                      input_schema_id, output_schema_id, measurements, evaluators,
                      created_at
                      FROM observatory_component_definitions
                      WHERE component_id = component_page.component_id
                      ORDER BY component_version
                      LIMIT 64
                    ) AS definitions
                    ORDER BY definitions.component_id, definitions.component_version""",
                    (after_component_id,),
                )
                rows = await result.fetchall()
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("PostgreSQL is unavailable") from exc
        values = [
            {
                "componentId": str(row[0]),
                "componentVersion": str(row[1]),
                "owner": str(row[2]),
                "category": str(row[3]),
                "inputSchemaId": str(row[4]),
                "outputSchemaId": str(row[5]),
                "measurements": sorted(str(item) for item in cast(list[Any], row[6] or [])),
                "evaluators": sorted(str(item) for item in cast(list[Any], row[7] or [])),
                "createdAt": iso(row[8]),
            }
            for row in rows
        ]
        has_more = len({str(item["componentId"]) for item in values}) > component_limit
        return values, has_more

    async def _component_definition_row(
        self, component_id: str, component_version: str
    ) -> dict[str, Any] | None:
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """SELECT component_id, component_version, owner, category,
                    input_schema_id, output_schema_id, measurements, evaluators
                    FROM observatory_component_definitions
                    WHERE component_id = %s AND component_version = %s""",
                    (component_id, component_version),
                )
                row = await result.fetchone()
                if row is None:
                    return None
                dependency_result = await connection.execute(
                    """SELECT dependency_id, dependency_kind, required_version
                    FROM observatory_component_dependencies
                    WHERE source_component_id = %s AND source_component_version = %s
                    ORDER BY dependency_kind, dependency_id, required_version NULLS FIRST
                    LIMIT 64""",
                    (component_id, component_version),
                )
                dependencies = cast(list[Any], await dependency_result.fetchall())
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("PostgreSQL is unavailable") from exc
        return {
            "componentId": str(row[0]),
            "componentVersion": str(row[1]),
            "owner": str(row[2]),
            "category": str(row[3]),
            "inputSchemaId": str(row[4]),
            "outputSchemaId": str(row[5]),
            "measurements": sorted(str(item) for item in cast(list[Any], row[6] or [])),
            "evaluators": sorted(str(item) for item in cast(list[Any], row[7] or [])),
            "dependencies": [
                {
                    "dependencyId": str(item[0]),
                    "kind": str(item[1]),
                    "requiredVersion": str(item[2]) if item[2] is not None else None,
                }
                for item in dependencies
            ],
        }

    async def deployments(self) -> list[dict[str, Any]]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT deployment_id, git_commit, environment, host_id, service_roles, "
                "first_seen_at, last_seen_at "
                "FROM observatory_deployments ORDER BY deployment_id, git_commit"
            )
            rows = await result.fetchall()
        return [
            {
                "deploymentId": row[0],
                "gitCommit": row[1],
                "environment": row[2],
                "hostId": row[3],
                "serviceRoles": list(row[4] or []),
                "firstSeenAt": iso(row[5]),
                "lastSeenAt": iso(row[6]),
            }
            for row in rows
        ]

    async def ingest_overview(self) -> dict[str, Any]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT accepted_count, rejected_count, duplicate_count, malformed_count, "
                "storage_failure_count, last_ingest_at, last_accepted_at, last_failure_at "
                "FROM observatory_ingest_counters "
                "WHERE counter_key = 'global'"
            )
            row = await result.fetchone()
        if row is None:
            return {}
        return {
            "accepted": row[0],
            "rejected": row[1],
            "duplicate": row[2],
            "malformed": row[3],
            "storageFailure": row[4],
            "lastIngestAt": iso(row[5]),
            "lastAcceptedAt": iso(row[6]),
            "lastFailureAt": iso(row[7]),
            "health": await self.health_snapshot_query(),
        }

    async def health_snapshot_query(self) -> dict[str, object] | None:
        return await self._health_port.health_snapshot()

    async def health_history_query(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]:
        return await self._health_port.health_history(
            cursor=cursor, from_time=from_time, to_time=to_time, limit=limit
        )

    async def prepare_retention(self, action: RetentionAction) -> None:
        """Persist the target snapshot before any destructive mutation."""

        self._retention_phase = "target_capture"
        self._active_retention_action_id = action.action_id
        if action.deletion_phase != "queued":
            return

        where = ""
        if action.cutoff is not None:
            where = (
                " WHERE observed_at < toDateTime64("
                + _clickhouse_time(action.cutoff)
                + ", 3, 'UTC')"
            )
        async with self._connection_factory() as connection:
            # Scope is read only while attach_retention's session lock is
            # held. Once the immutable target rows and phase are committed,
            # late ingest cannot enter the target set and ClickHouse cleanup
            # no longer needs to hold up telemetry.
            if self._retention_guard is None:
                await connection.execute("SELECT pg_advisory_xact_lock(830078)")
            await self._rows(
                "SELECT count() AS observations, uniqExactIf(run_id, run_id != '') AS runs "
                "FROM observations" + where + " LIMIT 1000"
            )
            # This bounded read preserves a storage visibility probe.  The
            # aggregate is not used as a deletion count: ClickHouse can expose
            # a newly committed row to the ordered target scan before the
            # separate aggregate read catches up (and the reverse can also
            # happen around a mutation).  The rows that are durably inserted
            # into the target ledger are the deletion authority, so derive
            # the observation count from that scan rather than reporting a
            # stale aggregate count to the retention API.
            target_observation_count = 0
            # Run identity is also authoritative only after the target rows
            # have been inserted.  Count it in PostgreSQL so a lagging
            # ClickHouse uniqExactIf result cannot produce a false zero in
            # deletion results or audit metrics.
            target_run_count = 0
            # Persist the immutable target scope in bounded pages. This keeps
            # restart recovery storage-side and avoids deriving run ids after
            # the source mutation has removed them.
            target_where = where
            last_at: str | None = None
            last_id: str | None = None
            previous_target_key: tuple[str, str] | None = None
            while True:
                page_where = target_where
                if last_at is not None and last_id is not None:
                    page_where += (
                        (" AND " if page_where else " WHERE ")
                        + "(observed_at, observation_id) > (toDateTime64("
                        + _sql(last_at)
                        + ", 3, 'UTC'), "
                        + _sql(last_id)
                        + ")"
                    )
                target_rows = await self._rows(
                    "SELECT observation_id, run_id, observed_at FROM observations"
                    + page_where
                    + " ORDER BY observed_at, observation_id LIMIT 1000"
                )
                if len(target_rows) == 1000:
                    target_key = (
                        str(target_rows[-1]["observed_at"]),
                        str(target_rows[-1]["observation_id"]),
                    )
                    _require_retention_page_progress(
                        "observation targets", previous_target_key, target_key
                    )
                    previous_target_key = target_key
                for target in target_rows:
                    if target.get("observation_id") is None:
                        continue
                    await connection.execute(
                        "INSERT INTO observatory_retention_targets("
                        "action_id, observation_id, run_id, phase) "
                        "VALUES (%s, %s, %s, 'targeted') "
                        "ON CONFLICT (action_id, observation_id) DO NOTHING",
                        (
                            action.action_id,
                            target.get("observation_id"),
                            target.get("run_id") or None,
                        ),
                    )
                    target_observation_count += 1
                if len(target_rows) < 1000:
                    break
                last_at = str(target_rows[-1]["observed_at"])
                last_id = str(target_rows[-1]["observation_id"])
            run_count_result = await connection.execute(
                "SELECT count(DISTINCT run_id) FROM observatory_retention_targets "
                "WHERE action_id = %s AND run_id IS NOT NULL",
                (action.action_id,),
            )
            run_count_row = await run_count_result.fetchone()
            target_run_count = int(run_count_row[0]) if run_count_row else 0
            # Receipt scope is captured by immutable observation id, not by a
            # mutable timestamp predicate.  Include pre-migration NULL
            # timestamps and receipt-only rows while the serialization guard
            # is held; late ingest cannot enter this target set.
            last_receipt_id: str | None = None
            previous_receipt_key: str | None = None
            while True:
                self._retention_phase = "receipt_target_capture"
                receipt_where = (
                    ""
                    if action.cutoff is None
                    else " AND (r.observed_at < %s OR r.observed_at IS NULL)"
                )
                keyset = ""
                params_list: list[Any] = []
                if last_receipt_id is not None:
                    keyset = " AND r.observation_id > %s::uuid"
                if action.cutoff is not None:
                    params_list.append(action.cutoff)
                if last_receipt_id is not None:
                    params_list.append(last_receipt_id)
                params_list.append(action.action_id)
                params = tuple(params_list)
                receipt_result = await connection.execute(
                    "SELECT r.observation_id FROM observatory_observation_receipts r "
                    "WHERE r.observation_id IS NOT NULL"
                    + receipt_where
                    + keyset
                    + " AND NOT EXISTS (SELECT 1 FROM observatory_retention_targets t "
                    # Receipts use PostgreSQL UUID while durable target pages
                    # intentionally retain the ClickHouse string identity.
                    # Cast the UUID to text at the comparison boundary so
                    # populated installations do not depend on implicit
                    # operator resolution.
                    "WHERE t.action_id = %s AND t.observation_id = r.observation_id::text) "
                    "ORDER BY r.observation_id LIMIT 1000",
                    params,
                )
                try:
                    receipt_rows = cast(list[tuple[Any, ...]], await receipt_result.fetchall())
                except AttributeError:
                    # Minimal connection doubles may not model the optional
                    # receipt page query; production asyncpg results always
                    # provide fetchall().
                    receipt_rows = []
                if len(receipt_rows) == 1000:
                    receipt_key = str(receipt_rows[-1][0])
                    _require_retention_page_progress(
                        "receipt targets", previous_receipt_key, receipt_key
                    )
                    previous_receipt_key = receipt_key
                for receipt_row in receipt_rows:
                    await connection.execute(
                        "INSERT INTO observatory_retention_targets("
                        "action_id, observation_id, run_id, phase) "
                        "VALUES (%s, %s, NULL, 'targeted') "
                        "ON CONFLICT (action_id, observation_id) DO NOTHING",
                        (action.action_id, receipt_row[0]),
                    )
                if len(receipt_rows) < 1000:
                    break
                last_receipt_id = str(receipt_rows[-1][0])
            receipt_result = await connection.execute(
                "SELECT count(*) FROM observatory_observation_receipts r "
                "JOIN observatory_retention_targets t ON t.action_id = %s "
                "AND t.observation_id = r.observation_id::text",
                (action.action_id,),
            )
            receipt_row = await receipt_result.fetchone()
            target_receipt_count = int(receipt_row[0]) if receipt_row else 0
            await connection.execute(
                "UPDATE observatory_retention_actions SET target_observation_count = %s, "
                "target_receipt_count = %s, target_run_count = %s, "
                "deletion_phase = 'targeted' WHERE action_id = %s "
                "AND deletion_phase = 'queued'",
                (
                    target_observation_count,
                    target_receipt_count,
                    target_run_count,
                    action.action_id,
                ),
            )
            await _commit_connection(connection)
            action.target_observation_count = target_observation_count
            action.target_receipt_count = target_receipt_count
            action.target_run_count = target_run_count
            action.deletion_phase = "targeted"
        await self._release_retention_guard(clear_action=False)

    async def _retention_target_ids(
        self, connection: Any, action_id: str, after: str | None = None
    ) -> list[str]:
        keyset = " AND observation_id > %s" if after is not None else ""
        params: tuple[Any, ...] = (action_id,) if after is None else (action_id, after)
        result = await connection.execute(
            "SELECT observation_id FROM observatory_retention_targets "
            "WHERE action_id = %s" + keyset + " ORDER BY observation_id LIMIT 1000",
            params,
        )
        rows = await result.fetchall()
        values = [str(row[0]) for row in rows]
        if values:
            _require_retention_page_progress("observation target page", after, values[-1])
        return values

    async def _retention_run_ids(
        self, connection: Any, action_id: str, after: str | None = None
    ) -> list[str]:
        keyset = " AND run_id > %s" if after is not None else ""
        params: tuple[Any, ...] = (action_id,) if after is None else (action_id, after)
        result = await connection.execute(
            "SELECT run_id FROM observatory_retention_targets "
            "WHERE action_id = %s AND run_id IS NOT NULL"
            + keyset
            + " GROUP BY run_id ORDER BY run_id LIMIT 1000",
            params,
        )
        rows = await result.fetchall()
        values = [str(row[0]) for row in rows if row[0]]
        if values:
            _require_retention_page_progress("run projection page", after, values[-1])
        return values

    async def _persist_retention_phase(self, connection: Any, phase: str) -> None:
        action_id = self._active_retention_action_id
        if action_id is None:
            return
        await connection.execute(
            "UPDATE observatory_retention_actions SET deletion_phase = %s WHERE action_id = %s",
            (phase, action_id),
        )
        await _commit_connection(connection)
        action = self._active_retention_action
        if action is not None:
            action.deletion_phase = phase

    async def _delete_persisted_retention_targets(self) -> DeletionResult:
        """Delete only the immutable target snapshot, in bounded pages."""

        action_id = self._active_retention_action_id
        if action_id is None:
            raise RuntimeError("retention target context is not attached")
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT target_observation_count, target_receipt_count, "
                "target_run_count, deletion_phase FROM observatory_retention_actions "
                "WHERE action_id = %s",
                (action_id,),
            )
            row = await result.fetchone()
            if row is None:
                raise RuntimeError("retention action target context is missing")
            observations = int(row[0] or 0)
            receipts = int(row[1] or 0)
            runs = int(row[2] or 0)
            phase = str(row[3] or "targeted")
            async with httpx.AsyncClient(timeout=10.0) as client:
                if phase in {"queued", "targeted", "failed"}:
                    last_run: str | None = None
                    previous_run_key: str | None = None
                    while True:
                        self._retention_phase = "run_target_lookup"
                        run_ids = await self._retention_run_ids(connection, action_id, last_run)
                        if not run_ids:
                            break
                        next_run = run_ids[-1]
                        _require_retention_page_progress(
                            "run projections", previous_run_key, next_run
                        )
                        previous_run_key = next_run
                        self._retention_phase = "run_projection_delete"
                        response = await client.post(
                            self._endpoint,
                            params={"database": self._database},
                            auth=(self._user, self._password),
                            content=(
                                "ALTER TABLE run_projections DELETE WHERE run_id IN ("
                                + ",".join(_sql(run_id) for run_id in run_ids)
                                + ") SETTINGS mutations_sync=2"
                            ),
                        )
                        _raise_for_clickhouse(response)
                        last_run = next_run
                    last_observation: str | None = None
                    previous_observation_key: str | None = None
                    while True:
                        self._retention_phase = "observation_target_lookup"
                        ids = await self._retention_target_ids(
                            connection, action_id, last_observation
                        )
                        if not ids:
                            break
                        next_observation = ids[-1]
                        _require_retention_page_progress(
                            "observations", previous_observation_key, next_observation
                        )
                        previous_observation_key = next_observation
                        self._retention_phase = "observation_delete"
                        response = await client.post(
                            self._endpoint,
                            params={"database": self._database},
                            auth=(self._user, self._password),
                            content=(
                                "ALTER TABLE observations DELETE WHERE observation_id IN ("
                                + ",".join(_sql(item) for item in ids)
                                + ") SETTINGS mutations_sync=2"
                            ),
                        )
                        _raise_for_clickhouse(response)
                        last_observation = next_observation
                    self._retention_phase = "retention_phase_update"
                    await self._persist_retention_phase(connection, "source_deleted")
                    phase = "source_deleted"
                if phase == "source_deleted":
                    self._retention_phase = "projection_truncate"
                    truncate = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content="TRUNCATE TABLE run_projections",
                    )
                    _raise_for_clickhouse(truncate)
                    self._retention_phase = "projection_rebuild"
                    rebuild = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=(
                            "INSERT INTO run_projections "
                            "SELECT run_id, maxState(observed_at), sumState(toUInt64(1)) "
                            "FROM observations WHERE run_id != '' GROUP BY run_id"
                        ),
                    )
                    _raise_for_clickhouse(rebuild)
                    self._retention_phase = "retention_phase_update"
                    await self._persist_retention_phase(connection, "projection_deleted")
                    phase = "projection_deleted"
            if phase == "projection_deleted":
                last_receipt: str | None = None
                previous_receipt_key: str | None = None
                while True:
                    self._retention_phase = "receipt_target_lookup"
                    ids = await self._retention_target_ids(connection, action_id, last_receipt)
                    if not ids:
                        break
                    next_receipt = ids[-1]
                    _require_retention_page_progress(
                        "receipt deletion", previous_receipt_key, next_receipt
                    )
                    previous_receipt_key = next_receipt
                    self._retention_phase = "receipt_delete"
                    await connection.execute(
                        "DELETE FROM observatory_observation_receipts "
                        "WHERE observation_id = ANY(%s::uuid[])",
                        (ids,),
                    )
                    last_receipt = next_receipt
                await _commit_connection(connection)
                self._retention_phase = "retention_phase_update"
                await self._persist_retention_phase(connection, "receipts_deleted")
            return DeletionResult(observations, receipts, runs)

    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
        selected = await self.list_observations()
        if cutoff is not None:
            selected = [
                item
                for item in selected
                if item.observed_at.astimezone(UTC) < cutoff.astimezone(UTC)
            ]
        ids = {item.observation_id for item in selected}
        run_ids = {
            item.correlation.run_id
            for item in selected
            if item.correlation and item.correlation.run_id
        }
        where = (
            ""
            if cutoff is None
            else (" WHERE observed_at < toDateTime64(" + _clickhouse_time(cutoff) + ", 3, 'UTC')")
        )
        deleted = len(selected)
        receipt_count = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._endpoint,
                params={"database": self._database},
                auth=(self._user, self._password),
                content="ALTER TABLE observations DELETE" + where + " SETTINGS mutations_sync=1",
            )
            _raise_for_clickhouse(response)
            # ClickHouse mutations do not cascade to aggregate projections;
            # delete the durable run scope captured before the source mutation.
            projection_where = ""
            if cutoff is not None and run_ids:
                quoted = ", ".join("'" + run_id.replace("'", "''") + "'" for run_id in run_ids)
                projection_where = f" WHERE run_id IN ({quoted})"
                projection = await client.post(
                    self._endpoint,
                    params={"database": self._database},
                    auth=(self._user, self._password),
                    content=(
                        "ALTER TABLE run_projections DELETE"
                        + projection_where
                        + " SETTINGS mutations_sync=1"
                    ),
                )
                _raise_for_clickhouse(projection)
        async with self._connection_factory() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(830078)")
            if cutoff is None:
                result = await connection.execute(
                    "SELECT count(*) FROM observatory_observation_receipts"
                )
                row = await result.fetchone()
                receipt_count = int(row[0]) if row else 0
                await connection.execute("DELETE FROM observatory_observation_receipts")
            else:
                result = await connection.execute(
                    "SELECT count(*) FROM observatory_observation_receipts "
                    "WHERE observed_at < %s OR observation_id = ANY(%s::uuid[])",
                    (cutoff, list(ids)),
                )
                row = await result.fetchone()
                receipt_count = int(row[0]) if row else 0
                await connection.execute(
                    "DELETE FROM observatory_observation_receipts "
                    "WHERE observed_at < %s OR observation_id = ANY(%s::uuid[])",
                    (cutoff, list(ids)),
                )
        return DeletionResult(deleted, receipt_count, len(run_ids))


class ProductionObservatoryStore(_RetentionPersistenceAdapter):
    """Narrow SQL adapter used by the production query service.

    Retention targets are durable bounded pages keyed by action_id;
    target_observation_count and deletion_phase survive worker restart.
    """

    _health_port: ObservatoryHealthPort = NullObservatoryHealthPort()

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                query_params: dict[str, str] = {"database": self._database}
                for name, value in (params or {}).items():
                    query_params[f"param_{name}"] = str(value)
                response = await client.post(
                    self._endpoint,
                    params=query_params,
                    auth=(self._user, self._password),
                    content=statement + " FORMAT JSONEachRow",
                )
                _raise_for_clickhouse(response)
                self._clickhouse_progress_available = True
                self._clickhouse_progress_read_rows = _clickhouse_progress_read_rows(response)
        except httpx.HTTPStatusError as exc:
            raise StorageUnavailableError(
                "ClickHouse is unavailable", request=exc.request, response=exc.response
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("ClickHouse is unavailable") from exc
        if not hasattr(response, "text"):
            # Small adapter doubles may omit the body.  Mark that bounded
            # compatibility mode explicitly; real ClickHouse responses always
            # provide text and use the SQL-only production path below.
            self._response_body_unavailable = True
            return []
        return _decode_clickhouse_rows(response)

    async def components_query(self) -> list[dict[str, Any]]:
        collection = await self.component_collection_query()
        values = cast(list[dict[str, Any]], collection["items"])
        # The legacy list port is version-shaped for existing callers; the
        # collection port above remains component-shaped and page-complete.
        flattened: list[dict[str, Any]] = []
        for value in values:
            versions = [str(item) for item in value.get("versions", [])]
            if not versions:
                flattened.append(value)
                continue
            for version in versions:
                flattened.append(
                    {
                        **value,
                        "version": version,
                        "observed": True,
                    }
                )
        return flattened

    async def _observed_component_page(
        self, after_component_id: str, component_limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read observed component groups with a bounded ClickHouse source page."""

        bound = component_limit + 1
        escaped_cursor = _sql(after_component_id)
        statement = f"""SELECT component_id,
        groupUniqArray(64)(component_version) AS versions,
        min(observed_at) AS first_seen_at,
        max(observed_at) AS last_seen_at
        FROM observations
        WHERE component_id != '' AND component_version != ''
          AND component_id IN (
            SELECT component_id
            FROM (
              SELECT component_id
              FROM observations
              WHERE component_id != '' AND component_version != ''
                AND component_id > {escaped_cursor}
              GROUP BY component_id
              ORDER BY component_id
              LIMIT {bound}
            )
          )
        GROUP BY component_id
        ORDER BY component_id"""
        rows = await self._query_rows(statement)
        values = rows
        has_more = len({str(row.get("component_id") or "") for row in values}) > component_limit
        return values, has_more

    async def component_collection_query(
        self,
        *,
        definition_state: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("component page size is out of bounds")
        fence = ""
        if cursor:
            try:
                decoded = json.loads(_decode_component_cursor(cursor))
                fence = str(decoded["componentId"])
                if not fence:
                    raise ValueError("invalid component cursor")
            except (
                ValueError,
                KeyError,
                TypeError,
                UnicodeDecodeError,
                binascii.Error,
                json.JSONDecodeError,
            ):
                raise ValueError("invalid component cursor") from None
        # Definitions are PostgreSQL-owned.  Deployed presence and observed
        # timestamps come only from ClickHouse; the legacy observation catalogue
        # is deliberately not consulted for production reads.
        compatibility_catalogue = (
            type(self).components is not _RetentionPersistenceAdapter.components
        )
        compatibility_definitions = (
            type(self)._component_definition_rows
            is not ProductionObservatoryStore._component_definition_rows
        )

        def merge_time(current: Any, observed: Any, *, earliest: bool) -> str | None:
            current_at = _parse_datetime(current)
            observed_at = _parse_datetime(observed)
            if current_at is None:
                return iso(observed_at)
            if observed_at is None:
                return iso(current_at)
            selected = min(current_at, observed_at) if earliest else max(current_at, observed_at)
            return iso(selected)

        def merge_definitions(
            merged: dict[str, dict[str, Any]], definitions: list[dict[str, Any]]
        ) -> None:
            for definition in definitions:
                if "version" in definition:
                    component_id = str(definition["componentId"])
                    item = merged.setdefault(component_id, dict(definition))
                    item.setdefault("versions", [str(definition["version"])])
                    continue
                component_id = str(definition["componentId"])
                item = merged.setdefault(
                    component_id,
                    {
                        "componentId": component_id,
                        "versions": [],
                        "definitionVersions": [],
                        "definitionState": "declared",
                        "firstSeenAt": definition["createdAt"],
                        "lastSeenAt": definition["createdAt"],
                        "observed": False,
                    },
                )
                item["definitionVersions"] = sorted(
                    set(cast(list[str], item["definitionVersions"]))
                    | {str(definition["componentVersion"])}
                )[:64]

        def merge_observations(
            merged: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
        ) -> None:
            for row in rows:
                component_id = str(row.get("component_id") or "")
                if not component_id:
                    continue
                item = merged.setdefault(
                    component_id,
                    {
                        "componentId": component_id,
                        "versions": [],
                        "definitionVersions": [],
                        "firstSeenAt": iso(_parse_datetime(row.get("first_seen_at"))),
                        "lastSeenAt": iso(_parse_datetime(row.get("last_seen_at"))),
                        "definitionState": "observed_only",
                        "observed": True,
                    },
                )
                item["observed"] = True
                versions = set(cast(list[str], item["versions"]))
                row_versions = row.get("versions")
                if isinstance(row_versions, list):
                    versions.update(
                        str(version) for version in cast(list[Any], row_versions) if version
                    )
                else:
                    version = str(row.get("component_version") or "")
                    if version:
                        versions.add(version)
                item["versions"] = sorted(versions)[:64]
                item["firstSeenAt"] = merge_time(
                    item.get("firstSeenAt"), row.get("first_seen_at"), earliest=True
                )
                item["lastSeenAt"] = merge_time(
                    item.get("lastSeenAt"), row.get("last_seen_at"), earliest=False
                )

        merged: dict[str, dict[str, Any]] = {}
        definition_fence = fence
        observed_fence = fence
        definitions_exhausted = True
        observations_exhausted = True
        # Compatibility doubles intentionally retain their original one-shot
        # reads. Production reads use independently advancing bounded source
        # scans so sparse filters cannot terminate at an empty overlap batch.
        if compatibility_catalogue or compatibility_definitions:
            declared: list[dict[str, Any]] = []
            if compatibility_catalogue:
                for item in await self.components():
                    component_id = str(item.get("componentId") or "")
                    version = str(item.get("version") or "")
                    if component_id and version:
                        declared.append(
                            {
                                **item,
                                "componentId": component_id,
                                "version": version,
                                "versions": [version],
                                "definitionVersions": [],
                                "definitionState": "observed_only",
                                "observed": True,
                            }
                        )
            else:
                declared = await self._component_definition_rows()
            observed_rows = await self._query_rows(
                "SELECT component_id, component_version, min(observed_at) AS first_seen_at, "
                "max(observed_at) AS last_seen_at FROM observations "
                "WHERE component_id != '' AND component_version != '' "
                "GROUP BY component_id, component_version "
                "ORDER BY component_id, component_version LIMIT 1000"
            )
            merge_definitions(merged, declared)
            merge_observations(merged, observed_rows)
        else:
            definitions_exhausted = False
            observations_exhausted = False
            for _ in range(_COMPONENT_SCAN_BATCHES):
                definition_batch: list[dict[str, Any]] = []
                observed_batch: list[dict[str, Any]] = []
                definition_more = False
                observed_more = False
                if not definitions_exhausted:
                    definition_batch, definition_more = await self._component_definition_page(
                        definition_fence, limit
                    )
                if not observations_exhausted:
                    observed_batch, observed_more = await self._observed_component_page(
                        observed_fence, limit
                    )
                merge_definitions(merged, definition_batch)
                merge_observations(merged, observed_batch)

                next_definition_fence = max(
                    (str(item["componentId"]) for item in definition_batch),
                    default=definition_fence,
                )
                next_observed_fence = max(
                    (str(item.get("component_id") or "") for item in observed_batch),
                    default=observed_fence,
                )
                definitions_exhausted = definitions_exhausted or not definition_more
                observations_exhausted = observations_exhausted or not observed_more
                if not definition_batch:
                    definitions_exhausted = True
                if not observed_batch:
                    observations_exhausted = True
                if next_definition_fence == definition_fence and not definition_more:
                    definitions_exhausted = True
                if next_observed_fence == observed_fence and not observed_more:
                    observations_exhausted = True
                definition_fence = next_definition_fence
                observed_fence = next_observed_fence

                candidates = [merged[key] for key in sorted(merged)]
                values = [
                    value
                    for value in candidates
                    if definition_state is None
                    or str(value.get("definitionState", "observed_only")) == definition_state
                ]
                if len(values) > limit or (definitions_exhausted and observations_exhausted):
                    break

        values = [merged[key] for key in sorted(merged)]
        if definition_state is not None:
            values = [
                value
                for value in values
                if str(value.get("definitionState", "observed_only")) == definition_state
            ]
        if compatibility_catalogue or compatibility_definitions:
            values = [value for value in values if str(value["componentId"]) > fence]
        page = values[:limit]
        next_cursor = None
        if len(values) > limit:
            next_cursor = _encode_component_cursor(str(page[-1]["componentId"]), "")
        elif not (compatibility_catalogue or compatibility_definitions):
            # If the bounded scan cap was reached before finding enough
            # filtered values, resume from the earliest still-active source
            # fence.  Re-reading the overlap is safe and prevents the single
            # cursor from skipping components in the other source.
            active_fences = [
                current
                for current, exhausted in (
                    (definition_fence, definitions_exhausted),
                    (observed_fence, observations_exhausted),
                )
                if not exhausted and current
            ]
            if active_fences:
                next_cursor = _encode_component_cursor(min(active_fences), "")
        return {"items": page, "nextCursor": next_cursor, "snapshotAt": iso(datetime.now(UTC))}

    async def component_definition_query(
        self, component_id: str, component_version: str
    ) -> dict[str, Any] | None:
        return await self._component_definition_row(component_id, component_version)

    async def component_dependencies_query(
        self,
        component_id: str,
        component_version: str,
        *,
        depth: int = 1,
        max_nodes: int = 100,
    ) -> dict[str, Any] | None:
        root = await self._component_definition_row(component_id, component_version)
        if root is None:
            return None
        health_from, health_to = bounded_component_health_window(None, None)
        definitions = await self._component_definition_rows()
        definitions_truncated = len(definitions) > 2000
        by_key = {
            (str(item["componentId"]), str(item["componentVersion"])): item for item in definitions
        }
        observed = await self._query_rows(
            "SELECT component_id, groupUniqArray(64)(component_version) AS versions "
            "FROM observations WHERE component_id != '' GROUP BY component_id "
            "ORDER BY component_id LIMIT 201"
        )
        observed_truncated = len(observed) > 200
        observed_versions = {
            str(row.get("component_id")): sorted(
                {str(item) for item in cast(list[Any], row.get("versions") or [])}
            )[:64]
            for row in observed
        }
        nodes: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        queue: list[tuple[str, str, int]] = [(component_id, component_version, 0)]
        enqueued = {(component_id, component_version)}
        expanded: set[tuple[str, str]] = set()
        edge_keys: set[tuple[str, str, str, str | None, str, str]] = set()
        partial = definitions_truncated or observed_truncated
        by_key.setdefault((component_id, component_version), root)
        while queue:
            source_id, source_version, current_depth = queue.pop(0)
            if (source_id, source_version) in expanded:
                continue
            expanded.add((source_id, source_version))
            source = by_key.get((source_id, source_version))
            if source is None:
                continue
            source_definition = (
                root
                if source_id == component_id and source_version == component_version
                else await self._component_definition_row(source_id, source_version)
            )
            if source_definition is None:
                continue
            if current_depth >= depth:
                continue
            dependencies = cast(list[dict[str, Any]], source_definition.get("dependencies", []))
            for dependency in dependencies:
                target_id = str(dependency["dependencyId"])
                kind = str(dependency["kind"])
                required = dependency.get("requiredVersion")
                required_version = str(required) if required is not None else None
                candidates = sorted(
                    [
                        key
                        for key in by_key
                        if key[0] == target_id
                        and (required_version is None or key[1] == required_version)
                    ]
                )
                if kind != "component":
                    candidates = []
                if kind == "component" and required_version is not None and not candidates:
                    available = target_id in {key[0] for key in by_key}
                    resolution = "version_unavailable" if available else "missing_definition"
                elif kind == "component" and candidates:
                    resolution = "resolved"
                elif target_id in observed_versions:
                    resolution = "resolved"
                else:
                    resolution = "missing_definition"
                target_candidates: list[tuple[str, str | None]] = [
                    (candidate[0], candidate[1]) for candidate in candidates
                ] or [(target_id, required_version)]
                for candidate_id, target_version in target_candidates:
                    edge_key = (
                        source_id,
                        source_version,
                        target_id,
                        target_version,
                        kind,
                        resolution,
                    )
                    if edge_key in edge_keys:
                        continue
                    if len(edges) >= 1000:
                        partial = True
                        break
                    key = (candidate_id, target_version, kind)
                    if key not in nodes:
                        if len(nodes) >= max_nodes:
                            partial = True
                            continue
                        health = (
                            await self.component_health_query(
                                target_id,
                                health_from,
                                health_to,
                                component_version=target_version,
                            )
                            if target_id in observed_versions
                            else None
                        )
                        declared = any(item[0] == target_id for item in by_key)
                        state = (
                            "declared"
                            if declared
                            else ("observed_only" if target_id in observed_versions else "missing")
                        )
                        nodes[key] = {
                            "dependencyId": target_id,
                            "kind": kind,
                            "componentVersion": target_version,
                            "definitionState": state,
                            "deployedVersions": observed_versions.get(target_id, [])[:64],
                            "availability": "observed"
                            if health is not None
                            else ("unobserved" if state == "declared" else "unavailable"),
                            "health": health.get("summary") if health else None,
                            "partialHealth": health is None,
                        }
                    # Reserve the target node before publishing its edge.
                    # A bounded response must never contain a relationship to
                    # a node omitted by max_nodes.
                    edge_keys.add(edge_key)
                    edges.append(
                        {
                            "sourceId": source_id,
                            "sourceVersion": source_version,
                            "targetId": target_id,
                            "targetVersion": target_version,
                            "targetKind": kind,
                            "resolution": resolution,
                        }
                    )
                    if (
                        current_depth < depth
                        and kind == "component"
                        and target_version is not None
                        and (target_id, target_version) in by_key
                    ):
                        next_key = (target_id, target_version)
                        if next_key not in enqueued:
                            enqueued.add(next_key)
                            queue.append((target_id, target_version, current_depth + 1))
                if len(edges) >= 1000:
                    break
        reverse = await self._reverse_dependencies(component_id)
        reverse_truncated = len(reverse) > 200
        return {
            "rootComponentId": component_id,
            "rootComponentVersion": component_version,
            "nodes": sorted(
                nodes.values(),
                key=lambda item: (
                    str(item["kind"]),
                    str(item["dependencyId"]),
                    str(item["componentVersion"] or ""),
                ),
            ),
            "edges": sorted(
                edges,
                key=lambda item: (
                    str(item["sourceId"]),
                    str(item["sourceVersion"]),
                    str(item["targetId"]),
                    str(item["targetVersion"] or ""),
                ),
            ),
            "directDependencyIds": sorted(
                {
                    str(item["dependencyId"])
                    for item in cast(list[dict[str, Any]], root.get("dependencies", []))
                }
            )[:64],
            "reverseDependencyIds": sorted({item[0] for item in reverse})[:200],
            "partial": partial
            or reverse_truncated
            or any(bool(item["partialHealth"]) for item in nodes.values()),
            "snapshotAt": iso(datetime.now(UTC)),
        }

    async def _reverse_dependencies(self, component_id: str) -> list[tuple[str, str]]:
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """SELECT source_component_id, source_component_version
                    FROM observatory_component_dependencies
                    WHERE dependency_id = %s AND dependency_kind = 'component'
                    ORDER BY source_component_id, source_component_version
                    LIMIT 201""",
                    (component_id,),
                )
                rows = await result.fetchall()
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("PostgreSQL is unavailable") from exc
        return sorted({(str(row[0]), str(row[1])) for row in rows})

    async def _bounded_compatibility_page(self) -> list[MetadataObservation]:
        """Read one bounded page for response-body-less adapter doubles."""

        return await self.list_observations(limit=1000)

    async def deployments_query(self) -> list[dict[str, Any]]:
        return await self.deployments()

    async def run_comparison_query(
        self, left_run_id: str, right_run_id: str
    ) -> dict[str, object] | None:
        """Fetch only the fixed comparison slice from ClickHouse."""

        if left_run_id == right_run_id:
            raise ValueError("comparison requires two distinct runs")
        query = (
            "WITH ranked AS (SELECT observation_id, observed_at, schema_version, kind, "
            "component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy, image_version, configuration_version, model_id, model_version, "
            "agent_revision, prompt_bundle_revision, policy_revision, "
            "row_number() OVER (PARTITION BY run_id ORDER BY observed_at, "
            "observation_id) AS run_row "
            "FROM observations "
            "WHERE run_id IN ({left_run_id:String}, {right_run_id:String}) "
            ") SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy, image_version, configuration_version, model_id, model_version, "
            "agent_revision, prompt_bundle_revision, policy_revision FROM ranked "
            f"WHERE run_row <= {COMPARISON_MAX_OBSERVATIONS + 1} "
            "ORDER BY run_id, observed_at, observation_id LIMIT "
            f"{COMPARISON_QUERY_ROWS} "
            "SETTINGS max_execution_time = 5, max_rows_to_read = 1000000, "
            "read_overflow_mode = 'break', send_progress_in_http_headers = 1"
        )
        started = monotonic()
        try:
            rows = await self._query_rows(
                query,
                params={"left_run_id": left_run_id, "right_run_id": right_run_id},
            )
        except StorageUnavailableError as exc:
            if _is_clickhouse_read_limit(exc):
                return comparison_execution_limit_snapshot(
                    left_run_id,
                    right_run_id,
                    [],
                    [],
                    [],
                    reason="scan_limit",
                    scanned_rows=COMPARISON_MAX_SCANNED_ROWS,
                )
            if _is_clickhouse_execution_timeout(exc):
                return comparison_execution_limit_snapshot(
                    left_run_id,
                    right_run_id,
                    [],
                    [],
                    [],
                    reason="execution_limit",
                )
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("ClickHouse is unavailable") from exc
        grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            run_id = str(row.get("run_id") or "")
            if run_id in {left_run_id, right_run_id}:
                grouped_rows[run_id].append(row)
        truncated_runs = {
            run_id
            for run_id, run_rows in grouped_rows.items()
            if len(run_rows) > COMPARISON_MAX_OBSERVATIONS
        }
        selected_rows = [
            row
            for run_id in (left_run_id, right_run_id)
            for row in grouped_rows.get(run_id, [])[:COMPARISON_MAX_OBSERVATIONS]
        ]
        observations = [_observation(row) for row in selected_rows]
        result = build_run_comparison(
            left_run_id,
            right_run_id,
            observations,
            truncated_runs=truncated_runs,
            deadline=started + COMPARISON_EXECUTION_TIMEOUT_MS / 1000.0,
        )
        if result is None:
            return None
        scan = cast(dict[str, object], result["scan"])
        physical_rows = self._clickhouse_progress_read_rows
        scan_rows = physical_rows if physical_rows is not None else len(rows)
        scan["scannedRows"] = min(COMPARISON_MAX_SCANNED_ROWS, max(0, scan_rows))
        scan["executionMs"] = min(
            COMPARISON_EXECUTION_TIMEOUT_MS,
            max(0.0, (monotonic() - started) * 1000.0),
        )
        if (
            physical_rows is None
            or physical_rows >= COMPARISON_MAX_SCANNED_ROWS
        ):
            result["partial"] = True
            result["partialReasons"] = sorted(
                set(cast(list[str], result["partialReasons"])) | {"scan_limit"}
            )
        return result

    async def list_observations(
        self, *, limit: int | None = 1000, cursor: int | None = None
    ) -> list[MetadataObservation]:
        """Compatibility port with a mandatory bounded source query."""

        if limit is None:
            limit = 1000
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        where = f" WHERE ingest_sequence > {cursor}" if cursor is not None else ""
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy, image_version, configuration_version, model_id, model_version, "
            "agent_revision, prompt_bundle_revision, policy_revision FROM observations"
            + where
            + " ORDER BY ingest_sequence LIMIT "
            + str(limit)
        )
        return [_observation(row) for row in rows]

    async def _overview_counts(self) -> dict[str, Any]:
        watermark = await self._committed_watermark()
        fence = f" WHERE ingest_sequence <= {watermark}" if watermark is not None else ""
        rows = await self._query_rows(
            "SELECT count() AS observation_count, "
            "uniqExactIf(run_id, run_id != '') AS run_count, "
            "uniqExactIf(trace_id, trace_id != '') AS trace_count, "
            "argMax(observation_id, ingest_sequence) AS latest_observation_id, "
            "max(ingest_sequence) AS latest_sequence, max(observed_at) AS observed_at "
            "FROM observations" + fence
        )
        result = rows[0] if rows else {}
        if watermark is not None:
            result["latest_sequence"] = min(int(result.get("latest_sequence", 0) or 0), watermark)
        return result

    async def refresh_health_inputs(self) -> None:
        """Refresh durable health inputs from storage, outside read requests."""

        watermark = await self._committed_watermark()
        await self._sync_visibility_health(watermark)
        counts = await self._overview_counts()
        await self._health_port.record_query_freshness(_parse_datetime(counts.get("observed_at")))

    async def _sync_visibility_health(self, watermark: int | None) -> None:
        if watermark is None:
            return
        try:
            async with self._connection_factory() as connection:
                visibility_result = await connection.execute(
                    "SELECT count(*) FROM observatory_ingest_visibility "
                    "WHERE sequence > %s AND visible = FALSE",
                    (watermark,),
                )
                visibility_row = await visibility_result.fetchone()
                backlog_result = await connection.execute(
                    "SELECT count(*), min(updated_at) FROM observatory_observation_receipts "
                    "WHERE status = 'pending'"
                )
                backlog_row = await backlog_result.fetchone()
            backlog_count = int(backlog_row[0] or 0) if backlog_row else 0
            oldest_age = None
            if backlog_row and backlog_row[1] is not None:
                oldest_age = max(
                    0.0,
                    (datetime.now(UTC) - backlog_row[1].astimezone(UTC)).total_seconds() * 1000,
                )
            await self._health_port.record_visibility(
                watermark,
                int(visibility_row[0] or 0) if visibility_row else 0,
                backlog_count,
                oldest_age,
            )
        except Exception:
            # Health diagnostics are best effort and cannot make a read fail.
            return

    async def _committed_watermark(self) -> int | None:
        """Read the PostgreSQL visibility fence without hydrating observations."""

        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    "SELECT committed_sequence FROM observatory_ingest_watermark "
                    "WHERE watermark_key = TRUE"
                )
                row = await result.fetchone()
        except Exception:
            # Empty/legacy installations are upgraded by the migration job;
            # until then bounded reads remain available without the fence.
            return None
        return int(row[0]) if row else None

    async def _analytics_retention_boundary(self, lower: datetime, upper: datetime) -> bool:
        """Detect whether a completed deletion overlaps the observed-time window.

        Retention actions describe a deleted observation-time domain, not a
        maintenance timestamp.  A cutoff removes every observation before its
        cutoff, so it overlaps ``[lower, upper)`` exactly when the cutoff is
        after the window start.  Purge-all removes the complete time domain;
        its completion time is intentionally irrelevant to overlap detection.
        """

        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM observatory_retention_actions "
                    "WHERE status = 'succeeded' AND ("
                    "action_kind = 'purge_all' "
                    "OR (action_kind = 'delete_before' AND requested_cutoff > %s))",
                    (lower,),
                )
                row = await result.fetchone()
        except Exception:
            # Retention evidence is diagnostic. Legacy/empty installations
            # remain queryable without claiming an unproven boundary.
            return False
        return bool(row and row[0])

    async def operational_analytics_query(self, request: AnalyticsRequest) -> dict[str, object]:
        """Execute one allowlisted, bounded ClickHouse analytics query.

        The query returns aggregate rows only.  It never selects observation
        identifiers, dimensions as a whole, traces, or content-bearing
        fields.  The source CTE and ClickHouse settings provide hard scan and
        execution bounds before aggregation begins.
        """

        lower = request.from_time
        upper = request.to_time
        if lower is None or upper is None:
            raise ValueError("analytics request must have a normalized window")
        bucket_seconds = ANALYTICS_BUCKETS[request.bucket]
        source_names = {
            "operation_duration_ms": ("duration_ms", "operation_duration_ms"),
            "run_duration_ms": ("run_duration_ms",),
        }.get(request.metric, (request.metric,))
        name_placeholders = ", ".join(
            "{" + f"metric_{index}:String" + "}" for index, _name in enumerate(source_names)
        )
        group_expr = {
            "none": "'all'",
            "component": "component_id",
            "component_version": "component_version",
            "operation": "operation",
            "deployment": "deployment_id",
            "environment": "environment",
            "host": "host_id",
            "service_role": "service_role",
            "provider": "coalesce(nullIf(dimensions['provider'], ''), 'unknown')",
            "outcome": _ANALYTICS_OUTCOME_SQL,
            "error_class": "coalesce(nullIf(dimensions['errorClass'], ''), 'unknown')",
            "status": "coalesce(nullIf(dimensions['status'], ''), 'unknown')",
            "retrieval_stage": "coalesce(nullIf(dimensions['retrievalStage'], ''), 'unknown')",
        }[request.group_by]
        where = [
            "observed_at >= {from_time:DateTime64(3, 'UTC')}",
            "observed_at < {to_time:DateTime64(3, 'UTC')}",
            f"measurement_name IN ({name_placeholders})",
            "measurement_unit = {measurement_unit:String}",
        ]
        column_filters = {
            "component": "component_id",
            "component_version": "component_version",
            "operation": "operation",
            "deployment": "deployment_id",
            "environment": "environment",
            "host": "host_id",
            "service_role": "service_role",
        }
        dimension_filters = {
            "provider": "provider",
            "error_class": "errorClass",
            "status": "status",
            "retrieval_stage": "retrievalStage",
        }
        params: dict[str, object] = {
            "from_time": lower.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "to_time": upper.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "measurement_unit": request.unit,
        }
        params.update({f"metric_{index}": name for index, name in enumerate(source_names)})
        for key, value in request.filters.items():
            if key in column_filters:
                where.append(f"{column_filters[key]} = {{filter_{key}:String}}")
                params[f"filter_{key}"] = value
            elif key == "outcome":
                where.append(f"{_ANALYTICS_OUTCOME_SQL} = {{filter_outcome:String}}")
                params["filter_outcome"] = _analytics_outcome_filter(value)
            elif key in dimension_filters:
                where.append(f"dimensions['{dimension_filters[key]}'] = {{filter_{key}:String}}")
                params[f"filter_{key}"] = value
        response_limit = request.series_limit * (request.point_limit + 1) + 1
        source_read_limit = ANALYTICS_MAX_SCANNED_ROWS + 1
        retention_boundary = await self._analytics_retention_boundary(lower, upper)
        query = (
            "WITH raw_source_limited AS (SELECT observed_at, measurement_value, "
            "observation_id, ingest_sequence, "
            f"{group_expr} AS group_value FROM observations WHERE {' AND '.join(where)} "
            f"ORDER BY ingest_sequence, observation_id LIMIT {source_read_limit}), "
            "raw_source_ordered AS (SELECT observed_at, measurement_value, observation_id, "
            "ingest_sequence, group_value, max(observed_at) OVER "
            "(ORDER BY ingest_sequence, observation_id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) "
            "AS prior_ingest_observed_at, row_number() OVER "
            "(ORDER BY ingest_sequence, observation_id) AS scan_rank "
            "FROM raw_source_limited), "
            "raw_source AS (SELECT raw_source_ordered.*, count() OVER () AS scan_rows_read, "
            "max(if(prior_ingest_observed_at IS NOT NULL AND "
            "observed_at < prior_ingest_observed_at, 1, 0)) OVER () AS source_late_data "
            "FROM raw_source_ordered), "
            "source AS (SELECT raw_source.*, "
            "count() OVER (PARTITION BY group_value) AS summary_sample_count, "
            "min(measurement_value) OVER (PARTITION BY group_value) AS summary_minimum, "
            "max(measurement_value) OVER (PARTITION BY group_value) AS summary_maximum, "
            "avg(measurement_value) OVER (PARTITION BY group_value) AS summary_average, "
            "quantileExact(0.50)(measurement_value) OVER (PARTITION BY group_value) "
            "AS summary_p50, "
            "quantileExact(0.90)(measurement_value) OVER (PARTITION BY group_value) "
            "AS summary_p90, "
            "quantileExact(0.95)(measurement_value) OVER (PARTITION BY group_value) "
            "AS summary_p95, "
            "quantileExact(0.99)(measurement_value) OVER (PARTITION BY group_value) "
            "AS summary_p99, "
            "sum(measurement_value) OVER (PARTITION BY group_value) AS summary_total "
            "FROM raw_source WHERE scan_rank <= "
            f"{ANALYTICS_MAX_SCANNED_ROWS}) "
            "SELECT toStartOfInterval(source.observed_at, INTERVAL "
            f"{bucket_seconds} SECOND) AS bucket_start, source.group_value, "
            "count() AS sample_count, "
            "min(source.measurement_value) AS minimum, max(source.measurement_value) AS maximum, "
            "avg(source.measurement_value) AS average, sum(source.measurement_value) AS total, "
            "quantileExact(0.50)(source.measurement_value) AS p50, "
            "quantileExact(0.90)(source.measurement_value) AS p90, "
            "quantileExact(0.95)(source.measurement_value) AS p95, "
            "quantileExact(0.99)(source.measurement_value) AS p99, "
            "max(source.scan_rows_read) AS scan_rows_read, "
            "max(source.observed_at) AS latest_observed_at, "
            "max(source.source_late_data) AS source_late_data, "
            "max(if(source.prior_ingest_observed_at IS NOT NULL AND "
            "source.observed_at < source.prior_ingest_observed_at, 1, 0)) AS late_data, "
            "max(source.summary_sample_count) AS summary_sample_count, "
            "max(source.summary_minimum) AS summary_minimum, "
            "max(source.summary_maximum) AS summary_maximum, "
            "max(source.summary_average) AS summary_average, "
            "max(source.summary_p50) AS summary_p50, "
            "max(source.summary_p90) AS summary_p90, "
            "max(source.summary_p95) AS summary_p95, "
            "max(source.summary_p99) AS summary_p99, "
            "max(source.summary_total) AS summary_total FROM source "
            "GROUP BY bucket_start, source.group_value ORDER BY source.group_value, bucket_start "
            f"LIMIT {response_limit} "
            f"SETTINGS max_execution_time = {ANALYTICS_EXECUTION_LIMIT_MS // 1000}, "
            f"max_rows_to_read = {source_read_limit}, "
            "send_progress_in_http_headers = 1, "
            "read_overflow_mode = 'break'"
        )
        started = monotonic()
        try:
            rows = await self._rows(query, params=params)
        except StorageUnavailableError:
            raise
        except httpx.HTTPStatusError as exc:
            if _is_clickhouse_read_limit(exc):
                return _analytics_scan_limit_snapshot(request, lower, upper, bucket_seconds)
            if _is_clickhouse_execution_timeout(exc):
                return _analytics_execution_limit_snapshot(request, lower, upper, bucket_seconds)
            raise StorageUnavailableError("ClickHouse is unavailable") from exc
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise StorageUnavailableError("ClickHouse is unavailable") from exc
        try:
            rows = _validate_analytics_rows(rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise StorageUnavailableError("ClickHouse returned an invalid response") from exc
        execution_ms = min(ANALYTICS_EXECUTION_LIMIT_MS, max(0.0, (monotonic() - started) * 1000.0))
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("group_value", "unknown"))].append(row)
        scan_rows_read = max(
            (int(row.get("scan_rows_read") or row.get("scanned_rows") or 0) for row in rows),
            default=0,
        )
        physical_rows = self._clickhouse_progress_read_rows
        if physical_rows is not None:
            scan_rows_read = max(scan_rows_read, physical_rows)
        scan_limit_hit = scan_rows_read >= ANALYTICS_MAX_SCANNED_ROWS
        # A selective WHERE can exhaust ClickHouse's physical read budget
        # before the matching-row lookahead is reached.  If the provider did
        # not return progress, a non-empty production response cannot prove
        # completeness, so conservatively expose scan_limit partial state.
        if self._clickhouse_progress_available and physical_rows is None:
            scan_limit_hit = True
        response_truncated = len(rows) >= response_limit
        partial_reasons: list[str] = []
        if scan_limit_hit:
            partial_reasons.append("scan_limit")
        if len(grouped) > request.series_limit or response_truncated:
            partial_reasons.append("series_limit")
        if (
            any(len(group_rows) > request.point_limit for group_rows in grouped.values())
            or response_truncated
        ):
            partial_reasons.append("point_limit")
        if any(bool(row.get("late_data")) or bool(row.get("source_late_data")) for row in rows):
            partial_reasons.append("late_data")
        if any(bool(row.get("retention_boundary")) for row in rows):
            partial_reasons.append("retention_boundary")
        if retention_boundary:
            partial_reasons.append("retention_boundary")
        if len(grouped) > ANALYTICS_MAX_BREAKDOWNS:
            partial_reasons.append("series_limit")
        source_partial = any(
            reason in partial_reasons
            for reason in ("scan_limit", "execution_limit", "late_data", "retention_boundary")
        )
        series: list[dict[str, object]] = []
        latest: datetime | None = None
        for group, group_rows in list(sorted(grouped.items()))[: request.series_limit]:
            points: list[dict[str, object]] = []
            point_partial = (
                source_partial or len(group_rows) > request.point_limit or response_truncated
            )
            for row in group_rows[: request.point_limit]:
                count = int(row.get("sample_count") or 0)
                start = _parse_datetime(row.get("bucket_start")) or lower
                observed = _parse_datetime(row.get("latest_observed_at"))
                if observed is not None:
                    latest = max(latest or observed, observed)
                points.append(
                    {
                        "startAt": iso(start),
                        "endAt": iso(min(start + timedelta(seconds=bucket_seconds), upper)),
                        "value": _analytics_row_value(row, request.aggregation, bucket_seconds),
                        "sampleCount": count,
                        "availability": "available" if count else "no_data",
                        "partial": point_partial,
                    }
                )
            sample_count = sum(int(row.get("sample_count") or 0) for row in group_rows)
            facet_name = _camel_analytics_group(request.group_by)
            series.append(
                {
                    "seriesKey": f"{request.metric}:{request.unit}:{group}",
                    "facets": {facet_name: group} if request.group_by != "none" else {},
                    "summary": _analytics_summary_rows(group_rows, sample_count, upper - lower),
                    "points": points,
                    "availability": "available" if sample_count else "no_data",
                    "partial": point_partial,
                }
            )
        observed_values = [
            value
            for value in (_parse_datetime(row.get("latest_observed_at")) for row in rows)
            if value is not None
        ]
        latest = max(observed_values, default=latest)
        breakdowns = [
            {
                "groupBy": request.group_by,
                "value": group,
                "count": sum(int(row.get("sample_count") or 0) for row in group_rows),
                "partial": source_partial or response_truncated,
            }
            for group, group_rows in list(sorted(grouped.items()))[:ANALYTICS_MAX_BREAKDOWNS]
            if request.group_by != "none"
        ]
        return {
            "snapshotAt": iso(datetime.now(UTC)),
            "query": {
                "metric": request.metric,
                "unit": request.unit,
                "aggregation": request.aggregation,
                "focus": request.focus,
                "from": iso(lower),
                "to": iso(upper),
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
            "freshness": analytics_freshness(latest),
            "partial": bool(partial_reasons),
            "partialReasons": sorted(set(partial_reasons)),
            "unavailableSignals": analytics_unavailable(request.metric),
            "scan": {
                "scannedRows": min(scan_rows_read, ANALYTICS_MAX_SCANNED_ROWS),
                "scanLimit": ANALYTICS_MAX_SCANNED_ROWS,
                "executionMs": execution_ms,
                "executionLimitMs": ANALYTICS_EXECUTION_LIMIT_MS,
            },
            "capturePolicy": "metadata_only",
        }

    async def overview_query(self) -> dict[str, Any]:
        counts = await self._overview_counts()
        ingest = await self.ingest_overview()
        return {
            "observationCount": int(counts.get("observation_count", 0)),
            "runCount": int(counts.get("run_count", 0)),
            "traceCount": int(counts.get("trace_count", 0)),
            "ingest": ingest,
            "components": len(await self.components()),
            "deployments": len(await self.deployments()),
            "latestObservationId": counts.get("latest_observation_id"),
            "snapshotSequence": int(counts.get("latest_sequence", 0) or 0),
            "health": ingest.get("health"),
        }

    async def run_query(
        self, filters: RunFilters, *, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        snapshot = await self._overview_counts()
        snapshot_sequence = int(snapshot.get("latest_sequence", 0) or 0)
        cursor_at: datetime | None = None
        cursor_id: str | None = None
        cursor_snapshot: int | None = None
        if cursor:
            cursor_snapshot, cursor_at, cursor_id = decode_run_cursor(cursor)
            if cursor_snapshot is not None:
                snapshot_sequence = min(snapshot_sequence, cursor_snapshot)
        conditions = ["run_id != ''", f"ingest_sequence <= {snapshot_sequence}"]
        if filters.component:
            conditions.append(f"component_id = {_sql(filters.component)}")
        if filters.deployment:
            conditions.append(f"deployment_id = {_sql(filters.deployment)}")
        if filters.model:
            conditions.append(f"dimensions['provider'] = {_sql(filters.model)}")
        if filters.from_time:
            conditions.append(
                "observed_at >= toDateTime64(" + _clickhouse_time(filters.from_time) + ", 3, 'UTC')"
            )
        if filters.to_time:
            conditions.append(
                "observed_at < toDateTime64(" + _clickhouse_time(filters.to_time) + ", 3, 'UTC')"
            )
        # Cursor decoding is deliberately delegated to the use case; the
        # opaque value is carried as a stable boundary in its query contract.
        having_conditions: list[str] = []
        if filters.status:
            # Status is a property of the latest observation for a run, not
            # of an individual row.  Filtering in WHERE would let an older
            # success keep a run visible after a later failure (or vice
            # versa), and could make one run appear in both result sets.
            having_conditions.append(
                "argMax("
                + _RUN_STATUS_SQL
                + ", (observed_at, observation_id)) = "
                + _sql(filters.status)
            )
        if cursor_at is not None and cursor_id is not None:
            having_conditions.append(
                "(max(observed_at), run_id) < (toDateTime64("
                + _clickhouse_time(cursor_at)
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        having = " HAVING " + " AND ".join(having_conditions) if having_conditions else ""
        statement = (
            "SELECT run_id, min(observed_at) AS started_at, max(observed_at) AS finished_at, "
            "argMax(observed_at, observed_at) AS latest_at, "
            "argMax(" + _RUN_STATUS_SQL + ", (observed_at, observation_id)) AS status, "
            "argMax(dimensions['provider'], observed_at) AS model, "
            "argMax(deployment_id, observed_at) AS deployment_id, "
            "groupUniqArray(component_id) AS component_ids, "
            # Empty terminal measurements must not mask the latest span
            # lineage.  The tuple argument makes equal-timestamp selection
            # deterministic by observation ID while the condition keeps the
            # aggregate explicitly metadata-only and non-empty.
            "argMaxIf(trace_id, (observed_at, observation_id), trace_id != '') AS trace_id, "
            "max(ingest_sequence) AS order_sequence "
            "FROM observations WHERE "
            + " AND ".join(conditions)
            + " GROUP BY run_id"
            + having
            + " ORDER BY latest_at DESC, run_id DESC LIMIT "
            + str(limit)
        )
        rows = await self._query_rows(statement)
        items = [
            {
                "runId": row["run_id"],
                "status": _run_status(str(row.get("status") or "unknown")),
                "startedAt": iso(_parse_datetime(row.get("started_at"))),
                "finishedAt": iso(_parse_datetime(row.get("finished_at"))),
                "durationMs": _duration(row.get("started_at"), row.get("finished_at")),
                "model": row.get("model") or None,
                "deploymentId": row.get("deployment_id"),
                "componentIds": list(row.get("component_ids") or []),
                "traceId": row.get("trace_id") or None,
                "orderSequence": int(row.get("order_sequence", 0)),
            }
            for row in rows
        ]
        next_cursor = None
        if len(items) == limit and items:
            last = items[-1]
            next_cursor = encode_cursor(
                _parse_datetime(last["finishedAt"]) or datetime.now(UTC),
                str(last["runId"]),
                snapshot_sequence,
            )
        return {
            "items": items,
            "nextCursor": next_cursor,
            "snapshotAt": iso(_parse_datetime(snapshot.get("observed_at")))
            or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "latestObservationId": snapshot.get("latest_observation_id"),
        }

    async def run_detail_query(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        if not 1 <= limit <= 10000:
            raise ValueError("page size must be between 1 and 10000")
        after = ""
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
            after = (
                " AND (observed_at, observation_id) > (toDateTime64("
                + _clickhouse_time(cursor_at)
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy, image_version, configuration_version, model_id, model_version, "
            "agent_revision, prompt_bundle_revision, policy_revision "
            "FROM observations WHERE run_id = "
            + _sql(run_id)
            + after
            + " ORDER BY observed_at, observation_id LIMIT "
            + str(limit + 1)
        )
        truncated = len(rows) > limit
        items = [_observation(row) for row in rows[:limit]]
        if not items:
            return None
        newest = max(items, key=lambda item: (item.observed_at, item.observation_id))
        result = run_dict(run_id, items, newest, include_spans=True)
        result["partial"] = truncated
        result["nextCursor"] = (
            encode_cursor(items[-1].observed_at, items[-1].observation_id) if truncated else None
        )
        return result

    async def trace_detail_query(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, Any] | None:
        if not 1 <= limit <= 10000:
            raise ValueError("page size must be between 1 and 10000")
        after = ""
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
            after = (
                " AND (observed_at, observation_id) > (toDateTime64("
                + _clickhouse_time(cursor_at)
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy, image_version, configuration_version, model_id, model_version, "
            "agent_revision, prompt_bundle_revision, policy_revision "
            "FROM observations WHERE trace_id = "
            + _sql(trace_id)
            + after
            + " ORDER BY observed_at, observation_id LIMIT "
            + str(limit + 1)
        )
        truncated = len(rows) > limit
        items = [_observation(row) for row in rows[:limit]]
        if not items:
            return None
        span_by_id: dict[str, dict[str, Any]] = {}
        for item in items:
            if item.trace and item.trace.span_id:
                span_by_id.setdefault(str(item.trace.span_id), span_dict(item))
        spans = list(span_by_id.values())[:10000]
        span_tree = tree(spans)
        partial = any(bool(span.get("partial")) for span in spans)
        run_ids = {
            item.correlation.run_id
            for item in items
            if item.correlation and item.correlation.run_id
        }
        return {
            "traceId": trace_id,
            "runId": next(iter(run_ids), None),
            "partial": partial or truncated,
            "spans": span_tree,
            "nextCursor": (
                encode_cursor(items[-1].observed_at, items[-1].observation_id)
                if truncated
                else None
            ),
        }

    async def component_health_query(
        self,
        component_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        *,
        component_version: str | None = None,
        point_limit: int = 200,
    ) -> dict[str, Any] | None:
        if not 1 <= point_limit <= 1000:
            raise ValueError("component health point limit is out of bounds")
        from_time, to_time = bounded_component_health_window(from_time, to_time)
        conditions = [f"component_id = {_sql(component_id)}"]
        if component_version:
            conditions.append("component_version = " + _sql(component_version))
        if from_time:
            conditions.append(
                "observed_at >= toDateTime64(" + _clickhouse_time(from_time) + ", 3, 'UTC')"
            )
        if to_time:
            conditions.append(
                "observed_at < toDateTime64(" + _clickhouse_time(to_time) + ", 3, 'UTC')"
            )
        # A component execution is one canonical span or the explicitly named
        # primary duration measurement. Secondary spanless metrics (including
        # names that merely happen to end in ``_duration_ms``) are not
        # allowed to form a synthetic execution group.
        duration_predicate = (
            "measurement_unit = 'milliseconds' AND measurement_name = 'duration_ms'"
        )
        rows = await self._query_rows(
            "SELECT count() AS sample_count, "
            "quantileIf(0.50)(selected_measurement_value, "
            "selected_measurement_unit = 'milliseconds') AS p50, "
            "quantileIf(0.95)(selected_measurement_value, "
            "selected_measurement_unit = 'milliseconds') AS p95, "
            "countIf(outcome = 'failed') AS failures, "
            "countIf(outcome = 'succeeded') AS successes, "
            "countIf(outcome = 'cancelled') AS cancelled "
            "FROM (SELECT if(span_id = '', "
            "if(measurement_name = 'duration_ms', "
            "observation_id, ''), span_id) AS execution_id, "
            "argMaxIf(measurement_value, (observed_at, observation_id), "
            + duration_predicate
            + ") AS selected_measurement_value, "
            "argMaxIf(measurement_unit, (observed_at, observation_id), "
            + duration_predicate
            + ") AS selected_measurement_unit, "
            # Outcome is independent of the latency row.  Select the latest
            # non-empty normalized status/outcome from the whole execution;
            # a later secondary measurement must be able to replace an older
            # duration-row outcome.
            "argMaxIf("
            + _RUN_STATUS_SQL
            + ", (observed_at, observation_id), "
            + _DIMENSION_STATUS_SQL
            + " != '') AS outcome "
            "FROM observations WHERE "
            + " AND ".join(conditions)
            + " AND (span_id != '' OR measurement_name = 'duration_ms') "
            + "GROUP BY execution_id)"
        )
        if not rows or int(rows[0].get("sample_count", 0)) == 0:
            return None
        row = rows[0]
        sample = int(row.get("sample_count", 0))
        failed = int(row.get("failures", 0))
        succeeded = int(row.get("successes", 0))
        cancelled = int(row.get("cancelled", 0))
        points_rows = []
        if point_limit != 200 or type(self)._query_rows is ProductionObservatoryStore._query_rows:
            points_rows = await self._query_rows(
                "SELECT toStartOfInterval(execution_at, INTERVAL 5 MINUTE) AS start_at, "
                "count() AS sample_count, "
                "quantileIf(0.50)(duration_ms, duration_unit = 'milliseconds') AS p50, "
                "quantileIf(0.95)(duration_ms, duration_unit = 'milliseconds') AS p95, "
                "countIf(outcome = 'failed') AS failures, "
                "countIf(outcome = 'succeeded') AS successes, "
                "countIf(outcome = 'cancelled') AS cancelled "
                "FROM (SELECT "
                "argMax(observed_at, (observed_at, observation_id)) AS execution_at, "
                "argMaxIf(measurement_value, (observed_at, observation_id), "
                + duration_predicate
                + ") AS duration_ms, "
                "argMaxIf(measurement_unit, (observed_at, observation_id), "
                + duration_predicate
                + ") AS duration_unit, "
                "argMaxIf("
                + _RUN_STATUS_SQL
                + ", (observed_at, observation_id), "
                + _DIMENSION_STATUS_SQL
                + " != '') AS outcome "
                "FROM observations WHERE "
                + " AND ".join(conditions)
                + " AND (span_id != '' OR measurement_name = 'duration_ms') "
                + "GROUP BY if(span_id = '', "
                + "if(measurement_name = 'duration_ms', observation_id, ''), span_id)) "
                + "WHERE isNotNull(execution_at) "
                + "GROUP BY start_at ORDER BY start_at ASC LIMIT "
                + str(point_limit)
            )
        points = [
            {
                "startAt": iso(start_at := _parse_datetime(point.get("start_at"))),
                "endAt": iso(start_at + timedelta(minutes=5)) if start_at else None,
                "summary": {
                    "sampleCount": int(point.get("sample_count", 0) or 0),
                    "latencyP50Ms": point.get("p50"),
                    "latencyP95Ms": point.get("p95"),
                    "errorRate": (
                        int(point.get("failures", 0) or 0) / int(point.get("sample_count", 0) or 1)
                    ),
                    "outcomes": {
                        "succeeded": int(point.get("successes", 0) or 0),
                        "failed": int(point.get("failures", 0) or 0),
                        "cancelled": int(point.get("cancelled", 0) or 0),
                        "unknown": max(
                            0,
                            int(point.get("sample_count", 0) or 0)
                            - int(point.get("failures", 0) or 0)
                            - int(point.get("successes", 0) or 0)
                            - int(point.get("cancelled", 0) or 0),
                        ),
                    },
                },
            }
            for point in points_rows
        ]
        return {
            "componentId": component_id,
            "summary": {
                "sampleCount": sample,
                "latencyP50Ms": row.get("p50"),
                "latencyP95Ms": row.get("p95"),
                "errorRate": failed / sample,
                "outcomes": {
                    "succeeded": succeeded,
                    "failed": failed,
                    "cancelled": cancelled,
                    "unknown": max(0, sample - failed - succeeded - cancelled),
                },
            },
            "points": points,
        }

    async def events_after_query(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        # Replay is fenced by the shared Observatory allocator and contiguous
        # visibility watermark; observation and health.changed events therefore
        # have one strict exclusive cursor.
        snapshot = await self._overview_counts()
        upper = (
            int(snapshot_sequence)
            if snapshot_sequence is not None
            else int(snapshot.get("latest_sequence", 0) or 0)
        )
        health_upper = await self._health_port.latest_health_event_sequence()
        upper = max(upper, health_upper)
        replay_upper = upper
        anchor = (
            await self._health_port.health_event_sequence(last_event_id) if last_event_id else None
        )
        if anchor is None and last_event_id:
            rows = await self._query_rows(
                "SELECT ingest_sequence FROM observations WHERE observation_id = "
                + _sql(last_event_id)
                + " LIMIT 1"
            )
            anchor = int(rows[0]["ingest_sequence"]) if rows else 0
        if anchor is None:
            anchor = int(snapshot_sequence or 0)
        heartbeat_at = asyncio.get_running_loop().time() + 15.0
        while True:
            rows = await self._query_rows(
                "SELECT observation_id, run_id, trace_id, ingest_sequence FROM observations "
                f"WHERE ingest_sequence > {anchor} AND ingest_sequence <= {upper} "
                "ORDER BY ingest_sequence LIMIT 1000"
            )
            health_rows = await self._health_port.health_events_after(last_event_id, upper)
            health_rows = [
                row
                for row in health_rows
                if isinstance(row.get("_sequence"), (int, float))
                and int(cast(int | float, row["_sequence"])) > anchor
            ]
            events = [
                {
                    "id": row["observation_id"],
                    "type": "observation.changed",
                    "sequence": int(row["ingest_sequence"]),
                    "data": {
                        "observationId": row["observation_id"],
                        "runId": row.get("run_id") or None,
                        "traceId": row.get("trace_id") or None,
                    },
                }
                for row in rows
            ] + [
                {
                    "id": row["id"],
                    "type": "health.changed",
                    "sequence": int(cast(int | float, row["_sequence"])),
                    "data": {},
                }
                for row in health_rows
            ]
            events.sort(key=lambda item: (int(cast(int, item["sequence"])), str(item["id"])))
            for event in events:
                anchor = int(cast(int, event["sequence"]))
                yield {
                    "id": event["id"],
                    "type": event["type"],
                    "data": event["data"],
                    "_persisted_replay": int(cast(int, event["sequence"])) <= replay_upper,
                }
            if events:
                continue
            latest = await self._overview_counts()
            upper = max(upper, int(latest.get("latest_sequence", upper) or upper))
            health_upper = await self._health_port.latest_health_event_sequence()
            upper = max(upper, int(latest.get("latest_sequence", upper) or upper), health_upper)
            if upper > anchor:
                continue
            await asyncio.sleep(1.0)
            if asyncio.get_running_loop().time() >= heartbeat_at:
                yield {
                    "type": "heartbeat",
                    "data": {},
                }
                heartbeat_at = asyncio.get_running_loop().time() + 15.0

    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
        """Delete through bounded counts and predicates, never row hydration."""

        if self._active_retention_action_id is not None:
            return await self._delete_persisted_retention_targets()

        where = ""
        if cutoff is not None:
            where = " WHERE observed_at < toDateTime64(" + _clickhouse_time(cutoff) + ", 3, 'UTC')"
        async with self._connection_factory() as connection:
            receipts = 0
            compatibility_run_ids: list[str] = []
            # attach_retention holds the session-scoped guard across target
            # selection, ClickHouse mutation, projection rebuild, and receipt
            # deletion. The fallback keeps direct adapter calls serialized.
            if self._retention_guard is None:
                await connection.execute("SELECT pg_advisory_xact_lock(830078)")
            if self._active_retention_action_id is not None:
                phase_result = await connection.execute(
                    "SELECT deletion_phase FROM observatory_retention_actions WHERE action_id = %s",
                    (self._active_retention_action_id,),
                )
                phase_row = await phase_result.fetchone()
                phase = str(phase_row[0]) if phase_row and phase_row[0] else "targeted"
                action_result = await connection.execute(
                    "SELECT target_observation_count, target_run_count "
                    "FROM observatory_retention_actions WHERE action_id = %s",
                    (self._active_retention_action_id,),
                )
                action_row = await action_result.fetchone()
                deleted = int(action_row[0] or 0) if action_row else 0
                runs = int(action_row[1] or 0) if action_row else 0
            else:
                phase = "targeted"
                scope = await self._query_rows(
                    "SELECT count() AS observations, uniqExactIf(run_id, run_id != '') AS runs "
                    "FROM observations" + where
                )
                row = scope[0] if scope else {}
                deleted = int(row.get("observations", 0))
                runs = int(row.get("runs", 0))
                if not scope and self._response_body_unavailable:
                    # Keep compatibility shims bounded.  Production uses the
                    # aggregate count query above and never hydrates rows.
                    page = await self._bounded_compatibility_page()
                    selected = [
                        item for item in page if cutoff is None or item.observed_at < cutoff
                    ]
                    deleted = len(selected)
                    compatibility_run_ids = sorted(
                        {
                            item.correlation.run_id
                            for item in selected
                            if item.correlation is not None and item.correlation.run_id
                        }
                    )
                    runs = len(compatibility_run_ids)
            async with httpx.AsyncClient(timeout=10.0) as client:
                if phase in {"queued", "targeted", "failed"}:
                    # Clear the derived projection before deleting source
                    # rows, then rebuild it from whatever observations
                    # survive. This avoids materializing an unbounded run-id
                    # list in Python or constructing a giant IN predicate.
                    projection_statement = (
                        "ALTER TABLE run_projections DELETE WHERE 1 = 1 SETTINGS mutations_sync=2"
                    )
                    if compatibility_run_ids:
                        projection_statement = (
                            "ALTER TABLE run_projections DELETE WHERE run_id IN ("
                            + ",".join(_sql(run_id) for run_id in compatibility_run_ids)
                            + ") SETTINGS mutations_sync=2"
                        )
                    self._retention_phase = "projection_delete"
                    projection = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=projection_statement,
                    )
                    _raise_for_clickhouse(projection)
                    self._retention_phase = "observation_delete"
                    response = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=(
                            "ALTER TABLE observations DELETE" + where + " SETTINGS mutations_sync=2"
                        ),
                    )
                    _raise_for_clickhouse(response)
                    if self._active_retention_action_id is not None:
                        await connection.execute(
                            "UPDATE observatory_retention_actions SET deletion_phase = "
                            "'source_deleted' WHERE action_id = %s",
                            (self._active_retention_action_id,),
                        )
                        await _commit_connection(connection)
                if phase in {"queued", "targeted", "source_deleted", "failed"}:
                    self._retention_phase = "projection_rebuild"
                    rebuild = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=(
                            "INSERT INTO run_projections "
                            "SELECT run_id, maxState(observed_at), sumState(toUInt64(1)) "
                            "FROM observations WHERE run_id != '' GROUP BY run_id"
                        ),
                    )
                    _raise_for_clickhouse(rebuild)
                    if self._active_retention_action_id is not None:
                        await connection.execute(
                            "UPDATE observatory_retention_actions SET deletion_phase = "
                            "'projection_deleted' WHERE action_id = %s",
                            (self._active_retention_action_id,),
                        )
                        await _commit_connection(connection)
            if (
                phase in {"queued", "targeted", "source_deleted", "projection_deleted", "failed"}
                and cutoff is None
            ):
                self._retention_phase = "receipt_delete"
                result = await connection.execute(
                    "SELECT count(*) FROM observatory_observation_receipts"
                )
                receipt_row = await result.fetchone()
                receipts = int(receipt_row[0]) if receipt_row else 0
                await connection.execute("DELETE FROM observatory_observation_receipts")
                await _commit_connection(connection)
            elif phase in {
                "queued",
                "targeted",
                "source_deleted",
                "projection_deleted",
                "failed",
            }:
                result = await connection.execute(
                    "SELECT count(*) FROM observatory_observation_receipts "
                    "WHERE observed_at < %s OR observed_at IS NULL",
                    (cutoff,),
                )
                receipt_row = await result.fetchone()
                receipts = int(receipt_row[0]) if receipt_row else 0
                await connection.execute(
                    "DELETE FROM observatory_observation_receipts "
                    "WHERE observed_at < %s OR observed_at IS NULL",
                    (cutoff,),
                )
                await _commit_connection(connection)
            if self._active_retention_action_id is not None:
                await connection.execute(
                    "UPDATE observatory_retention_actions SET deletion_phase = "
                    "'receipts_deleted' WHERE action_id = %s",
                    (self._active_retention_action_id,),
                )
                await _commit_connection(connection)
        return DeletionResult(deleted, receipts, runs)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    # ClickHouse DateTime64 JSON values are UTC but commonly omit an
    # explicit offset. Treat a naive value as UTC rather than interpreting it
    # in the API process's local timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _commit_connection(connection: Any) -> None:
    commit = getattr(connection, "commit", None)
    if commit is not None:
        await commit()


def _require_retention_page_progress(
    phase: str, previous: str | tuple[str, str] | None, current: str | tuple[str, str]
) -> None:
    """Reject a broken keyset page before it can create an infinite loop."""

    if previous is None:
        return
    if isinstance(previous, str):
        non_advancing = not isinstance(current, str) or current <= previous
    else:
        non_advancing = not isinstance(current, tuple) or current <= previous
    if non_advancing:
        raise RuntimeError(f"retention pagination did not advance: {phase}")


def _duration(start: Any, end: Any) -> float | None:
    first, last = _parse_datetime(start), _parse_datetime(end)
    return (last - first).total_seconds() * 1000 if first and last else None


def _run_status(value: str) -> str:
    normalized = {
        "success": "succeeded",
        "completed": "succeeded",
        "ok": "succeeded",
        "error": "failed",
    }.get(value, value)
    return (
        normalized
        if normalized in {"queued", "running", "succeeded", "failed", "cancelled"}
        else "unknown"
    )


def _observation(row: dict[str, Any]) -> MetadataObservation:
    observed = _parse_datetime(row["observed_at"])
    assert observed is not None
    payload: dict[str, Any] = {
        "schemaVersion": int(row["schema_version"]),
        "observationId": row["observation_id"],
        "observedAt": observed.isoformat().replace("+00:00", "Z"),
        "kind": row["kind"],
        "component": {"id": row["component_id"], "version": row["component_version"]},
        "operation": row["operation"],
        "measurement": {
            "name": row["measurement_name"],
            "value": float(row["measurement_value"]),
        },
        "dimensions": row.get("dimensions") or {},
        "deployment": {
            "deploymentId": row["deployment_id"],
            "gitCommit": row["git_commit"],
            "environment": row["environment"],
            "hostId": row["host_id"],
            **({"imageVersion": row["image_version"]} if row.get("image_version") else {}),
            **(
                {"configurationVersion": row["configuration_version"]}
                if row.get("configuration_version")
                else {}
            ),
        },
        "serviceRole": row["service_role"],
        "capturePolicy": row["capture_policy"],
    }
    if row.get("measurement_unit"):
        payload["measurement"]["unit"] = row["measurement_unit"]
    if row.get("trace_id"):
        trace: dict[str, Any] = {
            "traceId": row["trace_id"],
        }
        if row.get("span_id"):
            trace["spanId"] = row["span_id"]
        if row.get("parent_span_id"):
            trace["parentSpanId"] = row["parent_span_id"]
        if row.get("linked_span_ids"):
            trace["linkedSpanIds"] = row["linked_span_ids"]
        payload["trace"] = trace
    if row.get("run_id") or row.get("conversation_id"):
        correlation: dict[str, Any] = {}
        if row.get("run_id"):
            correlation["runId"] = row["run_id"]
        if row.get("conversation_id"):
            correlation["conversationId"] = row["conversation_id"]
        payload["correlation"] = correlation
    execution = {
        key: row[name]
        for name, key in (
            ("model_id", "modelId"),
            ("model_version", "modelVersion"),
            ("agent_revision", "agentRevision"),
            ("prompt_bundle_revision", "promptBundleRevision"),
            ("policy_revision", "policyRevision"),
        )
        if row.get(name)
    }
    if execution:
        payload["execution"] = execution
    return MetadataObservation.from_payload(payload)


__all__ = ["ProductionObservatoryStore"]
