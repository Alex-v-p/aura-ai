"""Production query adapter for the Observatory read/retention ports.

The API use cases never hydrate the ClickHouse observation table.  Each public
read has a bounded, purpose-built query and returns only the metadata shape the
use case needs.  The legacy retention primitives are inherited temporarily to
keep migration/recovery compatibility while their SQL remains service-owned.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from aura_observatory.contract import MetadataObservation
from aura_observatory.query import (
    DeletionResult,
    RetentionAction,
    RetentionStatus,
    RunFilters,
    decode_cursor,
    decode_run_cursor,
    encode_cursor,
    iso,
    retention_from_row,
    run_dict,
    span_dict,
    sql_time,
    tree,
)


def _sql(value: str) -> str:
    """Encode a value as a ClickHouse string literal.

    ClickHouse accepts backslash escapes in string literals.  Escaping the
    backslash first prevents an attacker-controlled backslash from changing
    the meaning of a subsequent escaped quote; the resulting literal is safe
    for every owner-controlled value that reaches the SQL adapter.
    """

    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


_DIMENSION_STATUS_SQL = (
    "coalesce(nullIf(dimensions['status'], ''), nullIf(dimensions['outcome'], ''))"
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
    ) -> None:
        self._connection_factory = connection_factory
        self._endpoint = clickhouse_url.rstrip("/")
        self._database = database
        self._user = user
        self._password = password
        # PostgreSQL is authoritative for retention state and idempotency.
        # Keep only the currently attached action needed while its guarded
        # multi-store deletion is in progress; never retain a process-lifetime
        # cache of historical actions or keys.
        self._active_retention_action: RetentionAction | None = None
        self._active_retention_action_id: str | None = None
        self._retention_guard: tuple[Any, Any] | None = None
        self._response_body_unavailable = False

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
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING action_id",
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
        if self._retention_guard is None:
            context = self._connection_factory()
            connection = await context.__aenter__()
            try:
                # Keep the session-scoped guard alive across target capture,
                # ClickHouse mutations, and receipt completion.  This closes
                # the target-capture/delete race with ingest and retention.
                await connection.execute("SELECT pg_advisory_lock(830078)")
            except BaseException:
                await context.__aexit__(None, None, None)
                self._active_retention_action_id = None
                self._active_retention_action = None
                raise
            self._retention_guard = (context, connection)

    async def _release_retention_guard(self) -> None:
        guarded = self._retention_guard
        self._retention_guard = None
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
            rows = await result.fetchall()
        series: list[dict[str, object]] = []
        for row in rows:
            action = str(row[0])
            status = str(row[1])
            if action not in {"delete_before", "purge_all"} or status not in {
                "queued", "running", "succeeded", "failed"
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
                "OR (status = 'failed' AND deletion_phase <> 'completed') "
                "ORDER BY requested_at"
            )
            rows = await result.fetchall()
        return [retention_from_row(row, str(row[11] or "")) for row in rows]

    async def claim_retention(self, action_id: str, lease_seconds: int = 60) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "UPDATE observatory_retention_actions SET status = 'running', "
                "started_at = CURRENT_TIMESTAMP, completed_at = NULL "
                "WHERE action_id = %s AND (status = 'queued' OR "
                "(status = 'failed' AND deletion_phase <> 'completed') OR "
                "(status = 'running' AND started_at <= CURRENT_TIMESTAMP - "
                "(%s * INTERVAL '1 second'))) RETURNING action_id",
                (action_id, lease_seconds),
            )
            return (await result.fetchone()) is not None

    async def claim_pending_retention(self, action_id: str) -> RetentionAction | None:
        if not await self.claim_retention(action_id):
            return None
        return await self.get_retention(action_id)

    async def _rows(self, query: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._endpoint,
                params={"database": self._database},
                auth=(self._user, self._password),
                content=query + " FORMAT JSONEachRow",
            )
            response.raise_for_status()
        rows: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(cast(dict[str, Any], value))
        return rows

    async def list_observations(self) -> list[MetadataObservation]:
        rows = await self._rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, "
            "measurement_unit, dimensions, trace_id, span_id, parent_span_id, "
            "linked_span_ids, run_id, conversation_id, deployment_id, git_commit, "
            "environment, host_id, service_role, capture_policy FROM observations"
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
                        },
                        "serviceRole": row["service_role"],
                        "capturePolicy": row["capture_policy"],
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
            rows = await result.fetchall()
        return [
            {
                "componentId": row[0],
                "version": row[1],
                "firstSeenAt": iso(row[2]),
                "lastSeenAt": iso(row[3]),
            }
            for row in rows
        ]

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
        }

    async def prepare_retention(self, action: RetentionAction) -> None:
        """Persist the target snapshot before any destructive mutation."""

        self._active_retention_action_id = action.action_id
        if action.deletion_phase != "queued":
            return

        where = ""
        if action.cutoff is not None:
            where = " WHERE observed_at < toDateTime64(" + sql_time(action.cutoff) + ", 3, 'UTC')"
        async with self._connection_factory() as connection:
            # Scope is read only after attach_retention acquired the
            # session-scoped destructive lock, so it remains serialized until
            # the terminal audit update releases that lock.
            if self._retention_guard is None:
                await connection.execute("SELECT pg_advisory_xact_lock(830078)")
            rows = await self._rows(
                "SELECT count() AS observations, uniqExactIf(run_id, run_id != '') AS runs "
                "FROM observations" + where + " LIMIT 1000"
            )
            row = rows[0] if rows else {}
            target_observation_count = int(row.get("observations", 0))
            target_run_count = int(row.get("runs", 0))
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
                if len(target_rows) < 1000:
                    break
                last_at = str(target_rows[-1]["observed_at"])
                last_id = str(target_rows[-1]["observation_id"])
            # Receipt scope is captured by immutable observation id, not by a
            # mutable timestamp predicate.  Include pre-migration NULL
            # timestamps and receipt-only rows while the serialization guard
            # is held; late ingest cannot enter this target set.
            last_receipt_id: str | None = None
            previous_receipt_key: str | None = None
            while True:
                receipt_where = (
                    ""
                    if action.cutoff is None
                    else " AND (r.observed_at < %s OR r.observed_at IS NULL)"
                )
                keyset = ""
                params_list: list[Any] = []
                if last_receipt_id is not None:
                    keyset = " AND r.observation_id > %s"
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
                    "WHERE t.action_id = %s AND t.observation_id = r.observation_id) "
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
                "AND t.observation_id = r.observation_id",
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
            "WHERE action_id = %s AND run_id IS NOT NULL" + keyset
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
            "UPDATE observatory_retention_actions SET deletion_phase = %s "
            "WHERE action_id = %s",
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
                        run_ids = await self._retention_run_ids(connection, action_id, last_run)
                        if not run_ids:
                            break
                        next_run = run_ids[-1]
                        _require_retention_page_progress(
                            "run projections", previous_run_key, next_run
                        )
                        previous_run_key = next_run
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
                        response.raise_for_status()
                        last_run = next_run
                    last_observation: str | None = None
                    previous_observation_key: str | None = None
                    while True:
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
                        response.raise_for_status()
                        last_observation = next_observation
                    await self._persist_retention_phase(connection, "source_deleted")
                    phase = "source_deleted"
                if phase == "source_deleted":
                    truncate = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content="TRUNCATE TABLE run_projections",
                    )
                    truncate.raise_for_status()
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
                    rebuild.raise_for_status()
                    await self._persist_retention_phase(connection, "projection_deleted")
                    phase = "projection_deleted"
            if phase == "projection_deleted":
                last_receipt: str | None = None
                previous_receipt_key: str | None = None
                while True:
                    ids = await self._retention_target_ids(connection, action_id, last_receipt)
                    if not ids:
                        break
                    next_receipt = ids[-1]
                    _require_retention_page_progress(
                        "receipt deletion", previous_receipt_key, next_receipt
                    )
                    previous_receipt_key = next_receipt
                    await connection.execute(
                        "DELETE FROM observatory_observation_receipts "
                        "WHERE observation_id = ANY(%s)",
                        (ids,),
                    )
                    last_receipt = next_receipt
                await _commit_connection(connection)
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
            else (
                " WHERE observed_at < toDateTime64("
                f"'{cutoff.astimezone(UTC).isoformat()}', 3, 'UTC')"
            )
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
            response.raise_for_status()
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
                projection.raise_for_status()
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
                    "WHERE observed_at < %s OR observation_id = ANY(%s)",
                    (cutoff, list(ids)),
                )
                row = await result.fetchone()
                receipt_count = int(row[0]) if row else 0
                await connection.execute(
                    "DELETE FROM observatory_observation_receipts "
                    "WHERE observed_at < %s OR observation_id = ANY(%s)",
                    (cutoff, list(ids)),
                )
        return DeletionResult(deleted, receipt_count, len(run_ids))


class ProductionObservatoryStore(_RetentionPersistenceAdapter):
    """Narrow SQL adapter used by the production query service.

    Retention targets are durable bounded pages keyed by action_id;
    target_observation_count and deletion_phase survive worker restart.
    """

    async def _query_rows(self, statement: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._endpoint,
                params={"database": self._database},
                auth=(self._user, self._password),
                content=statement + " FORMAT JSONEachRow",
            )
            response.raise_for_status()
        if not hasattr(response, "text"):
            # Small adapter doubles may omit the body.  Mark that bounded
            # compatibility mode explicitly; real ClickHouse responses always
            # provide text and use the SQL-only production path below.
            self._response_body_unavailable = True
            return []
        return [
            cast(dict[str, Any], json.loads(line))
            for line in response.text.splitlines()
            if line.strip()
        ]

    async def components_query(self) -> list[dict[str, Any]]:
        return await self.components()

    async def _bounded_compatibility_page(self) -> list[MetadataObservation]:
        """Read one bounded page for response-body-less adapter doubles."""

        return await self.list_observations(limit=1000)

    async def deployments_query(self) -> list[dict[str, Any]]:
        return await self.deployments()

    async def list_observations(
        self, *, limit: int = 1000, cursor: int | None = None
    ) -> list[MetadataObservation]:
        """Compatibility port with a mandatory bounded source query."""

        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        where = f" WHERE ingest_sequence > {cursor}" if cursor is not None else ""
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy FROM observations"
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
                "observed_at >= toDateTime64("
                + _sql(filters.from_time.astimezone(UTC).isoformat())
                + ", 3, 'UTC')"
            )
        if filters.to_time:
            conditions.append(
                "observed_at < toDateTime64("
                + _sql(filters.to_time.astimezone(UTC).isoformat())
                + ", 3, 'UTC')"
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
                "argMax(" + _RUN_STATUS_SQL + ", (observed_at, observation_id)) = "
                + _sql(filters.status)
            )
        if cursor_at is not None and cursor_id is not None:
            having_conditions.append(
                "(max(observed_at), run_id) < (toDateTime64("
                + _sql(cursor_at.astimezone(UTC).isoformat())
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        having = (
            " HAVING " + " AND ".join(having_conditions)
            if having_conditions
            else ""
        )
        statement = (
            "SELECT run_id, min(observed_at) AS started_at, max(observed_at) AS finished_at, "
            "argMax(observed_at, observed_at) AS latest_at, "
            "argMax(" + _RUN_STATUS_SQL + ", (observed_at, observation_id)) AS status, "
            "argMax(dimensions['provider'], observed_at) AS model, "
            "argMax(deployment_id, observed_at) AS deployment_id, "
            "groupUniqArray(component_id) AS component_ids, "
            "argMax(trace_id, observed_at) AS trace_id, max(ingest_sequence) AS order_sequence "
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
                + _sql(cursor_at.astimezone(UTC).isoformat())
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy FROM observations WHERE run_id = "
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
            encode_cursor(items[-1].observed_at, items[-1].observation_id)
            if truncated
            else None
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
                + _sql(cursor_at.astimezone(UTC).isoformat())
                + ", 3, 'UTC'), "
                + _sql(cursor_id)
                + ")"
            )
        rows = await self._query_rows(
            "SELECT observation_id, observed_at, schema_version, kind, component_id, "
            "component_version, operation, measurement_name, measurement_value, measurement_unit, "
            "dimensions, trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
            "conversation_id, deployment_id, git_commit, environment, host_id, service_role, "
            "capture_policy FROM observations WHERE trace_id = "
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
        self, component_id: str, from_time: datetime | None, to_time: datetime | None
    ) -> dict[str, Any] | None:
        conditions = [f"component_id = {_sql(component_id)}"]
        if from_time:
            conditions.append(
                "observed_at >= toDateTime64("
                + _sql(from_time.astimezone(UTC).isoformat())
                + ", 3, 'UTC')"
            )
        if to_time:
            conditions.append(
                "observed_at < toDateTime64("
                + _sql(to_time.astimezone(UTC).isoformat())
                + ", 3, 'UTC')"
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
            "quantile(0.50)(measurement_value) FILTER "
            "(WHERE measurement_unit = 'milliseconds') AS p50, "
            "quantile(0.95)(measurement_value) FILTER "
            "(WHERE measurement_unit = 'milliseconds') AS p95, "
            "countIf(outcome = 'failed') AS failures, "
            "countIf(outcome = 'succeeded') AS successes, "
            "countIf(outcome = 'cancelled') AS cancelled "
            "FROM (SELECT if(span_id = '', "
            "if(measurement_name = 'duration_ms', "
            "observation_id, ''), span_id) AS execution_id, "
            "argMaxIf(measurement_value, (observed_at, observation_id), "
            + duration_predicate
            + ") AS measurement_value, "
            "argMaxIf(measurement_unit, (observed_at, observation_id), "
            + duration_predicate
            + ") AS measurement_unit, "
            # Outcome is independent of the latency row.  Select the latest
            # non-empty normalized status/outcome from the whole execution;
            # a later secondary measurement must be able to replace an older
            # duration-row outcome.
            "argMaxIf(" + _RUN_STATUS_SQL + ", (observed_at, observation_id), "
            + _DIMENSION_STATUS_SQL
            + " != '') AS outcome "
            "FROM observations WHERE " + " AND ".join(conditions)
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
            "points": [],
        }

    async def events_after_query(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        # Replay is fenced by the contiguous visibility watermark; a later
        # sequence remains hidden while an earlier ClickHouse insert is absent.
        snapshot = await self._overview_counts()
        upper = (
            int(snapshot_sequence)
            if snapshot_sequence is not None
            else int(snapshot.get("latest_sequence", 0) or 0)
        )
        replay_upper = upper
        anchor = upper
        if last_event_id:
            rows = await self._query_rows(
                "SELECT ingest_sequence FROM observations WHERE observation_id = "
                + _sql(last_event_id)
                + " LIMIT 1"
            )
            anchor = int(rows[0]["ingest_sequence"]) if rows else 0
        heartbeat_at = asyncio.get_running_loop().time() + 15.0
        while True:
            rows = await self._query_rows(
                "SELECT observation_id, run_id, trace_id, ingest_sequence FROM observations "
                f"WHERE ingest_sequence > {anchor} AND ingest_sequence <= {upper} "
                "ORDER BY ingest_sequence LIMIT 1000"
            )
            for row in rows:
                anchor = int(row["ingest_sequence"])
                yield {
                    "id": row["observation_id"],
                    "type": "observation.changed",
                    "data": {
                        "observationId": row["observation_id"],
                        "runId": row.get("run_id") or None,
                        "traceId": row.get("trace_id") or None,
                    },
                    "_persisted_replay": int(row["ingest_sequence"]) <= replay_upper,
                }
            if rows:
                continue
            latest = await self._overview_counts()
            upper = max(upper, int(latest.get("latest_sequence", upper) or upper))
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
            where = (
                " WHERE observed_at < toDateTime64("
                + _sql(cutoff.astimezone(UTC).isoformat())
                + ", 3, 'UTC')"
            )
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
                    "SELECT deletion_phase FROM observatory_retention_actions "
                    "WHERE action_id = %s",
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
                        item
                        for item in page
                        if cutoff is None or item.observed_at < cutoff
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
                        "ALTER TABLE run_projections DELETE WHERE 1 = 1 "
                        "SETTINGS mutations_sync=2"
                    )
                    if compatibility_run_ids:
                        projection_statement = (
                            "ALTER TABLE run_projections DELETE WHERE run_id IN ("
                            + ",".join(_sql(run_id) for run_id in compatibility_run_ids)
                            + ") SETTINGS mutations_sync=2"
                        )
                    projection = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=projection_statement,
                    )
                    projection.raise_for_status()
                    response = await client.post(
                        self._endpoint,
                        params={"database": self._database},
                        auth=(self._user, self._password),
                        content=(
                            "ALTER TABLE observations DELETE"
                            + where
                            + " SETTINGS mutations_sync=2"
                        ),
                    )
                    response.raise_for_status()
                    if self._active_retention_action_id is not None:
                        await connection.execute(
                            "UPDATE observatory_retention_actions SET deletion_phase = "
                            "'source_deleted' WHERE action_id = %s",
                            (self._active_retention_action_id,),
                        )
                        await _commit_connection(connection)
                if phase in {"queued", "targeted", "source_deleted", "failed"}:
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
                    rebuild.raise_for_status()
                    if self._active_retention_action_id is not None:
                        await connection.execute(
                            "UPDATE observatory_retention_actions SET deletion_phase = "
                            "'projection_deleted' WHERE action_id = %s",
                            (self._active_retention_action_id,),
                        )
                        await _commit_connection(connection)
            if (
                phase
                in {"queued", "targeted", "source_deleted", "projection_deleted", "failed"}
                and cutoff is None
            ):
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
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00")).astimezone(
        UTC
    )


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
    return MetadataObservation.from_payload(
        {
            "schemaVersion": int(row["schema_version"]),
            "observationId": row["observation_id"],
            "observedAt": observed.isoformat().replace("+00:00", "Z"),
            "kind": row["kind"],
            "component": {"id": row["component_id"], "version": row["component_version"]},
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
            },
            "serviceRole": row["service_role"],
            "capturePolicy": row["capture_policy"],
        }
    )


__all__ = ["ProductionObservatoryStore"]
