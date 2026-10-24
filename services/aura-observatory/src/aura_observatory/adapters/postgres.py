"""Small PostgreSQL adapter for Observatory transactional catalogues.

The adapter receives a connection factory rather than importing Core's
database module.  It owns only Observatory table names and never exposes a
general-purpose query port.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import uuid4

from aura_observatory.catalogue import manifest_hash
from aura_observatory.metrics import (
    HEALTH_HISTORY_LIMIT,
    MALFORMED_REASONS,
    REJECTION_REASONS,
    BoundedLatencyWindow,
    HealthState,
    ObservatoryMetrics,
    bounded_lag_ms,
    bounded_latency_ms,
)
from aura_observatory.ports import ClaimOutcome, PendingObservationReceipt


class AsyncConnection(Protocol):
    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any: ...


def _definition_statements(definition: Mapping[str, object]) -> list[tuple[str, tuple[Any, ...]]]:
    """Build the service-owned SQL for one normalized manifest definition."""

    component_id = str(definition["componentId"])
    version = str(definition["componentVersion"])
    dependencies = definition.get("dependencies", [])
    dependency_rows = (
        cast(list[Mapping[str, object]], dependencies) if isinstance(dependencies, list) else []
    )
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            """INSERT INTO observatory_component_definitions(
            component_id, component_version, owner, category,
            input_schema_id, output_schema_id, measurements, evaluators,
            manifest_hash, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (component_id, component_version) DO UPDATE SET
            owner = EXCLUDED.owner, category = EXCLUDED.category,
            input_schema_id = EXCLUDED.input_schema_id,
            output_schema_id = EXCLUDED.output_schema_id,
            measurements = EXCLUDED.measurements,
            evaluators = EXCLUDED.evaluators,
            manifest_hash = EXCLUDED.manifest_hash,
            updated_at = CURRENT_TIMESTAMP""",
            (
                component_id,
                version,
                str(definition["owner"]),
                str(definition["category"]),
                str(definition["inputSchemaId"]),
                str(definition["outputSchemaId"]),
                list(cast(list[object], definition.get("measurements", []))),
                list(cast(list[object], definition.get("evaluators", []))),
                manifest_hash(definition),
            ),
        ),
        (
            "DELETE FROM observatory_component_dependencies "
            "WHERE source_component_id = %s AND source_component_version = %s",
            (component_id, version),
        ),
    ]
    statements.extend(
        (
            """INSERT INTO observatory_component_dependencies(
            source_component_id, source_component_version, dependency_id,
            dependency_kind, required_version) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""",
            (
                component_id,
                version,
                str(dependency["dependencyId"]),
                str(dependency["kind"]),
                dependency.get("requiredVersion"),
            ),
        )
        for dependency in dependency_rows
    )
    return statements


class PostgresSyncCatalogueAdapter:
    """Synchronous migration-time adapter for the same catalogue SQL."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def sync_component_definitions(self, definitions: Sequence[Mapping[str, object]]) -> int:
        for definition in definitions:
            statement, params = _definition_statements(definition)[0]
            self._connection.execute(statement, params)
        for definition in definitions:
            for statement, params in _definition_statements(definition)[1:]:
                self._connection.execute(statement, params)
        return len(definitions)


class PostgresCatalog:
    def __init__(self, connection_factory: Any) -> None:
        self._connection_factory = connection_factory

    async def record_component(self, component_id: str, version: str) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                """
                INSERT INTO observatory_components(component_id, version)
                VALUES (%s, %s)
                ON CONFLICT (component_id, version) DO NOTHING
                """,
                (component_id, version),
            )

    async def sync_component_definitions(self, definitions: Sequence[Mapping[str, object]]) -> int:
        """Atomically upsert the bounded, already-validated manifest set.

        The caller owns parsing and validation.  This adapter only accepts the
        normalized metadata shape produced by the service-owned manifest
        loader; it never reads a Core module or accepts arbitrary definition
        text.  Existing definitions are retained when a later sync omits them
        so manual retention cannot remove catalogue history.
        """

        async with self._connection_factory() as connection:
            for definition in definitions:
                statement, params = _definition_statements(definition)[0]
                await connection.execute(statement, params)
            for definition in definitions:
                for statement, params in _definition_statements(definition)[1:]:
                    await connection.execute(statement, params)
        return len(definitions)

    async def component_definitions(
        self, *, definition_state: str | None = None
    ) -> list[dict[str, object]]:
        """Return definitions in stable identifier/version order."""

        del definition_state  # state is applied after merging observations.
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT component_id, component_version, owner, category,
                input_schema_id, output_schema_id, measurements, evaluators
                FROM observatory_component_definitions
                ORDER BY component_id, component_version LIMIT 2000"""
            )
            rows = await result.fetchall()
        return [_definition_from_row(row) for row in rows]

    async def component_definition(
        self, component_id: str, component_version: str
    ) -> dict[str, object] | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT component_id, component_version, owner, category,
                input_schema_id, output_schema_id, measurements, evaluators
                FROM observatory_component_definitions
                WHERE component_id = %s AND component_version = %s LIMIT 1""",
                (component_id, component_version),
            )
            row = await result.fetchone()
        return _definition_from_row(row) if row is not None else None

    async def component_dependencies(
        self, component_id: str, component_version: str
    ) -> list[dict[str, object]]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT dependency_id, dependency_kind, required_version
                FROM observatory_component_dependencies
                WHERE source_component_id = %s AND source_component_version = %s
                ORDER BY dependency_kind, dependency_id, required_version NULLS FIRST
                LIMIT 64""",
                (component_id, component_version),
            )
            rows = await result.fetchall()
        return [
            {
                "dependencyId": str(row[0]),
                "kind": str(row[1]),
                "requiredVersion": str(row[2]) if row[2] is not None else None,
            }
            for row in rows
        ]

    async def reverse_component_dependencies(self, dependency_id: str) -> list[tuple[str, str]]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT source_component_id, source_component_version
                FROM observatory_component_dependencies
                WHERE dependency_id = %s AND dependency_kind = 'component'
                ORDER BY source_component_id, source_component_version LIMIT 200""",
                (dependency_id,),
            )
            rows = await result.fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                """
                INSERT INTO observatory_deployments(deployment_id, git_commit, environment, host_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (deployment_id, git_commit, environment, host_id)
                DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                """,
                (deployment_id, git_commit, environment, host_id),
            )

    async def record_deployment_role(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str, role: str
    ) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_deployments SET service_roles = "
                "(SELECT ARRAY(SELECT DISTINCT unnest(service_roles || %s::varchar[]))) "
                "WHERE deployment_id = %s AND git_commit = %s "
                "AND environment = %s AND host_id = %s",
                ([role], deployment_id, git_commit, environment, host_id),
            )

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        if name not in {
            "accepted",
            "rejected",
            "duplicate",
            "malformed",
            "storage_failure",
            "duration_ms",
        }:
            raise ValueError("unknown ingest counter")
        column = f"{name}_count" if name != "duration_ms" else "duration_ms_total"
        health_column = (
            "last_accepted_at"
            if name == "accepted"
            else (
                "last_failure_at" if name in {"rejected", "malformed", "storage_failure"} else None
            )
        )
        health_update = f", {health_column} = CURRENT_TIMESTAMP" if health_column else ""
        health_insert = f", {health_column}" if health_column else ""
        health_value = ", CURRENT_TIMESTAMP" if health_column else ""
        async with self._connection_factory() as connection:
            await connection.execute(
                f"""INSERT INTO observatory_ingest_counters(
                    counter_key, {column}, last_ingest_at{health_insert}
                )
                VALUES ('global', %s, CURRENT_TIMESTAMP{health_value})
                ON CONFLICT (counter_key) DO UPDATE SET {column} =
                observatory_ingest_counters.{column} + EXCLUDED.{column},
                last_ingest_at = CURRENT_TIMESTAMP{health_update}""",
                (amount,),
            )

    async def counters(self) -> Mapping[str, int]:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT accepted_count, rejected_count, duplicate_count, "
                "malformed_count, storage_failure_count "
                "FROM observatory_ingest_counters WHERE counter_key = 'global'"
            )
            row = await result.fetchone()
        if row is None:
            return {}
        return {
            "accepted": int(row[0]),
            "rejected": int(row[1]),
            "duplicate": int(row[2]),
            "malformed": int(row[3]),
            "storage_failure": int(row[4]),
        }

    async def last_ingest_at(self) -> datetime | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT last_ingest_at FROM observatory_ingest_counters "
                "WHERE counter_key = 'global'"
            )
            row = await result.fetchone()
        return None if row is None else row[0]

    async def record_metric(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str],
        *,
        histogram: bool = False,
    ) -> None:
        """Persist one allowlisted evaluation metric series without payloads."""

        evaluation_names = {
            metric_name
            for metric_name in ObservatoryMetrics.ALLOWED
            if metric_name.startswith("observatory_evaluation_")
        }
        if name not in evaluation_names or not math.isfinite(value) or abs(value) > 1e15:
            return
        normalized = {str(key): str(label) for key, label in labels.items()}
        if not ObservatoryMetrics.labels_valid(name, normalized):
            return
        columns = {
            "operation": normalized.get("operation", ""),
            "outcome": normalized.get("outcome", ""),
            "selector": normalized.get("selector", ""),
            "phase": normalized.get("phase", ""),
            "failure_code": normalized.get("failure_code", ""),
        }
        buckets = (
            {str(bucket): int(value <= bucket) for bucket in ObservatoryMetrics.DURATION_BUCKETS}
            if histogram
            else {}
        )
        bucket_merge = ", ".join(
            f"'{bucket}', COALESCE("
            "(observatory_evaluation_metric_series.histogram_buckets->>"
            f"'{bucket}')::bigint, 0) "
            f"+ COALESCE((EXCLUDED.histogram_buckets->>'{bucket}')::bigint, 0)"
            for bucket in ObservatoryMetrics.DURATION_BUCKETS
        )
        async with self._connection_factory() as connection:
            await connection.execute(
                f"""INSERT INTO observatory_evaluation_metric_series(
                metric_name, label_operation, label_outcome, label_selector,
                label_phase, label_failure_code, value, histogram_count,
                histogram_buckets, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                CURRENT_TIMESTAMP)
                ON CONFLICT (
                    metric_name, label_operation, label_outcome, label_selector,
                    label_phase, label_failure_code
                ) DO UPDATE SET
                    value = observatory_evaluation_metric_series.value + EXCLUDED.value,
                    histogram_count = observatory_evaluation_metric_series.histogram_count
                        + EXCLUDED.histogram_count,
                    histogram_buckets = jsonb_build_object({bucket_merge}),
                    updated_at = CURRENT_TIMESTAMP""",
                (
                    name,
                    columns["operation"],
                    columns["outcome"],
                    columns["selector"],
                    columns["phase"],
                    columns["failure_code"],
                    value,
                    1 if histogram else 0,
                    json.dumps(buckets, separators=(",", ":")),
                ),
            )

    async def record_rejection(self, reason: str, amount: int = 1) -> None:
        if amount < 1:
            return
        normalized_reason = reason if reason in REJECTION_REASONS else "malformed"
        rejected_amount = 0 if normalized_reason in MALFORMED_REASONS else amount
        malformed_amount = amount if normalized_reason in MALFORMED_REASONS else 0
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, rejected_count, malformed_count, rejection_reasons,
                    last_ingest_at, last_failure_at, last_rejection_at
                ) VALUES ('global', %s, %s, jsonb_build_object(%s, %s),
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (counter_key) DO UPDATE SET rejection_reasons =
                jsonb_set(
                    COALESCE(observatory_ingest_counters.rejection_reasons, '{}'::jsonb),
                    ARRAY[%s],
                    to_jsonb(COALESCE(
                        (observatory_ingest_counters.rejection_reasons->>%s)::bigint, 0
                    ) + %s),
                    TRUE
                ), rejected_count = observatory_ingest_counters.rejected_count
                    + EXCLUDED.rejected_count,
                malformed_count = observatory_ingest_counters.malformed_count
                    + EXCLUDED.malformed_count,
                last_ingest_at = CURRENT_TIMESTAMP, last_failure_at = CURRENT_TIMESTAMP,
                last_rejection_at = CURRENT_TIMESTAMP""",
                (
                    rejected_amount,
                    malformed_amount,
                    normalized_reason,
                    amount,
                    normalized_reason,
                    normalized_reason,
                    amount,
                ),
            )

    async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None:
        normalized = observed_at.astimezone(UTC)
        if amount < 1:
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, accepted_count, last_observed_at,
                    last_accepted_at, last_ingest_at
                ) VALUES ('global', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (counter_key) DO UPDATE SET
                accepted_count = observatory_ingest_counters.accepted_count
                    + EXCLUDED.accepted_count,
                last_observed_at = GREATEST(
                    COALESCE(
                        observatory_ingest_counters.last_observed_at,
                        EXCLUDED.last_observed_at
                    ),
                    EXCLUDED.last_observed_at
                ), last_accepted_at = CURRENT_TIMESTAMP,
                last_ingest_at = CURRENT_TIMESTAMP""",
                (amount, normalized),
            )

    async def record_duplicate(self, amount: int = 1) -> None:
        if amount < 1:
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, duplicate_count, last_duplicate_at
                ) VALUES ('global', %s, CURRENT_TIMESTAMP)
                ON CONFLICT (counter_key) DO UPDATE SET
                duplicate_count = observatory_ingest_counters.duplicate_count
                    + EXCLUDED.duplicate_count,
                last_duplicate_at = CURRENT_TIMESTAMP""",
                (amount,),
            )

    async def record_storage_failure(self, amount: int = 1) -> None:
        if amount < 1:
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, storage_failure_count, last_storage_failure_at, last_failure_at
                ) VALUES ('global', %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (counter_key) DO UPDATE SET
                storage_failure_count = observatory_ingest_counters.storage_failure_count
                    + EXCLUDED.storage_failure_count,
                last_storage_failure_at = CURRENT_TIMESTAMP,
                last_failure_at = CURRENT_TIMESTAMP""",
                (amount,),
            )

    async def record_latency(self, kind: str, duration_ms: float) -> None:
        if kind not in {"storage", "query"}:
            return
        bounded = bounded_latency_ms(duration_ms)
        if bounded is None:
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                "INSERT INTO observatory_ingest_counters(counter_key) VALUES ('global') "
                "ON CONFLICT (counter_key) DO NOTHING"
            )
            result = await connection.execute(
                "SELECT storage_latency, query_latency FROM observatory_ingest_counters "
                "WHERE counter_key = 'global' FOR UPDATE"
            )
            row = await result.fetchone()
            raw_value: object = cast(object, row[0 if kind == "storage" else 1]) if row else {}
            raw: object = {}
            if isinstance(raw_value, (dict, list, str)):
                raw = cast(object, raw_value)
            if isinstance(raw, str):
                try:
                    raw = cast(object, json.loads(raw))
                except json.JSONDecodeError:
                    raw = {}
            samples_value: object = (
                cast(dict[str, object], raw).get("samples", []) if isinstance(raw, dict) else []
            )
            samples: list[object] = (
                list(cast(list[object], samples_value)) if isinstance(samples_value, list) else []
            )
            samples = [
                value
                for value in samples[-127:]
                if isinstance(value, (int, float)) and bounded_latency_ms(value) is not None
            ]
            samples.append(bounded)
            reservoir = json.dumps({"samples": samples[-128:]})
            if kind == "query":
                await connection.execute(
                    "UPDATE observatory_ingest_counters SET query_latency = %s::jsonb "
                    "WHERE counter_key = 'global'",
                    (reservoir,),
                )
                return
            bucket_updates = ", ".join(
                f"'{bucket}', COALESCE((storage_latency_buckets->>'{bucket}')::bigint, 0) "
                f"+ CASE WHEN %s <= {bucket} THEN 1 ELSE 0 END"
                for bucket in ObservatoryMetrics.DURATION_BUCKETS
            )
            await connection.execute(
                "UPDATE observatory_ingest_counters SET storage_latency = %s::jsonb, "
                "storage_latency_count = COALESCE(storage_latency_count, 0) + 1, "
                "storage_latency_sum = COALESCE(storage_latency_sum, 0) + %s, "
                "storage_latency_buckets = jsonb_build_object(" + bucket_updates + ") "
                "WHERE counter_key = 'global'",
                (reservoir, bounded, *([bounded] * len(ObservatoryMetrics.DURATION_BUCKETS))),
            )

    async def metric_series(self) -> list[dict[str, object]]:
        """Expose bounded persisted health histograms for the API scrape."""

        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT storage_latency, storage_latency_count, storage_latency_sum, "
                "storage_latency_buckets FROM observatory_ingest_counters "
                "WHERE counter_key = 'global'"
            )
            row = await result.fetchone()
            evaluation_result = await connection.execute(
                """WITH ranked AS (
                SELECT metric_name, label_operation, label_outcome, label_selector,
                label_phase, label_failure_code, value, histogram_count, histogram_buckets,
                ROW_NUMBER() OVER (
                    PARTITION BY metric_name
                    ORDER BY label_operation, label_outcome, label_selector,
                    label_phase, label_failure_code
                ) AS series_rank
                FROM observatory_evaluation_metric_series
                WHERE metric_name LIKE 'observatory_evaluation_%'
                )
                SELECT metric_name, label_operation, label_outcome, label_selector,
                label_phase, label_failure_code, value, histogram_count, histogram_buckets
                FROM ranked
                WHERE series_rank <= 64
                ORDER BY metric_name, label_operation, label_outcome, label_selector,
                label_phase, label_failure_code"""
            )
            evaluation_rows = await evaluation_result.fetchall()
            lease_result = await connection.execute(
                """SELECT component_id,
                GREATEST(0.0, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - heartbeat_at)))
                FROM observatory_evaluation_worker_leases
                WHERE component_id IN (%s, %s)""",
                (
                    "aura.observatory.evaluation_orchestration",
                    "aura.observatory.evaluation_scheduler",
                ),
            )
            lease_rows = await lease_result.fetchall()
        series: list[dict[str, object]] = []
        if row is not None and len(row) >= 4:
            try:
                cumulative_count = max(0, int(row[1] or 0))
                cumulative_sum = float(row[2] or 0.0)
            except TypeError, ValueError, OverflowError:
                cumulative_count = 0
                cumulative_sum = 0.0
            if cumulative_count > 0 and math.isfinite(cumulative_sum):
                raw_buckets = row[3]
                if isinstance(raw_buckets, str):
                    try:
                        raw_buckets = json.loads(raw_buckets)
                    except json.JSONDecodeError:
                        raw_buckets = {}
                buckets = _metric_buckets(cast(object, raw_buckets))
                series.append(
                    {
                        "name": "observatory_ingest_storage_duration_ms",
                        "value": cumulative_sum,
                        "sum": cumulative_sum,
                        "count": cumulative_count,
                        "buckets": buckets,
                        "labels": {},
                    }
                )
        for evaluation_row in evaluation_rows:
            if len(evaluation_row) < 9:
                continue
            name = str(evaluation_row[0])
            labels = {
                key: str(evaluation_row[index])
                for key, index in (
                    ("operation", 1),
                    ("outcome", 2),
                    ("selector", 3),
                    ("phase", 4),
                    ("failure_code", 5),
                )
                if evaluation_row[index]
            }
            if name not in {
                metric_name
                for metric_name in ObservatoryMetrics.ALLOWED
                if metric_name.startswith("observatory_evaluation_")
            } or not ObservatoryMetrics.labels_valid(name, labels):
                continue
            value = _finite_metric_value(evaluation_row[6])
            if name in ObservatoryMetrics.HISTOGRAMS:
                series.append(
                    {
                        "name": name,
                        "value": value,
                        "sum": value,
                        "count": _bounded_metric_count(evaluation_row[7]),
                        "buckets": _metric_buckets(evaluation_row[8]),
                        "labels": labels,
                    }
                )
            else:
                series.append({"name": name, "value": value, "labels": labels})
        heartbeat_names = {
            "aura.observatory.evaluation_orchestration": (
                "observatory_evaluation_worker_heartbeat_age_seconds"
            ),
            "aura.observatory.evaluation_scheduler": (
                "observatory_evaluation_scheduler_heartbeat_age_seconds"
            ),
        }
        for lease_row in lease_rows:
            if len(lease_row) < 2:
                continue
            name = heartbeat_names.get(str(lease_row[0]))
            try:
                age = float(lease_row[1])
            except TypeError, ValueError, OverflowError:
                continue
            if name is not None and math.isfinite(age) and 0.0 <= age <= 1e15:
                series.append({"name": name, "value": age, "labels": {}})
        return series

    async def record_worker_heartbeat(self, at: datetime | None = None) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, worker_heartbeat_at
                )
                VALUES ('global', %s)
                ON CONFLICT (counter_key) DO UPDATE SET
                worker_heartbeat_at = EXCLUDED.worker_heartbeat_at""",
                (at or datetime.now(UTC),),
            )

    async def record_query_freshness(self, observed_at: datetime | None) -> None:
        if observed_at is None:
            return
        normalized = observed_at.astimezone(UTC)
        async with self._connection_factory() as connection:
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(counter_key, query_freshness_lag_ms)
                VALUES ('global', LEAST(604800000.0, GREATEST(0.0,
                    EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - %s)) * 1000.0)))
                ON CONFLICT (counter_key) DO UPDATE SET
                query_freshness_lag_ms = EXCLUDED.query_freshness_lag_ms""",
                (normalized,),
            )

    async def record_visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | None = None,
    ) -> None:
        async with self._connection_factory() as connection:
            oldest = None
            if oldest_backlog_age_ms is not None:
                age = bounded_lag_ms(oldest_backlog_age_ms)
                oldest = (
                    datetime.now(UTC).replace(microsecond=0)
                    if age is not None and age <= 0
                    else (datetime.now(UTC) - timedelta(milliseconds=age))
                    if age is not None
                    else None
                )
            await connection.execute(
                """INSERT INTO observatory_ingest_counters(
                    counter_key, visibility_watermark, visibility_lag_count,
                    backlog_count, oldest_backlog_at
                ) VALUES ('global', %s, %s, %s, %s)
                ON CONFLICT (counter_key) DO UPDATE SET
                visibility_watermark = EXCLUDED.visibility_watermark,
                visibility_lag_count = EXCLUDED.visibility_lag_count,
                backlog_count = EXCLUDED.backlog_count,
                oldest_backlog_at = EXCLUDED.oldest_backlog_at""",
                (max(0, watermark), max(0, lag_count), max(0, backlog_count), oldest),
            )

    async def health_snapshot(self) -> dict[str, object] | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT accepted_count, rejected_count, duplicate_count, malformed_count,
                storage_failure_count, rejection_reasons, storage_latency, query_latency,
                last_observed_at, worker_heartbeat_at, visibility_watermark, visibility_lag_count,
                backlog_count, oldest_backlog_at, query_freshness_lag_ms, last_ingest_at,
                last_rejection_at, last_duplicate_at, last_storage_failure_at
                FROM observatory_ingest_counters WHERE counter_key = 'global'"""
            )
            row = await result.fetchone()
        if row is None:
            return None
        return _health_snapshot_from_row(row)

    async def save_health_snapshot(self, snapshot: dict[str, object]) -> None:
        payload = _health_snapshot_values(snapshot)
        async with self._connection_factory() as connection:
            latest = await connection.execute(
                "SELECT state, visibility_watermark, visibility_lag_count "
                "FROM observatory_ingest_health_history "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1"
            )
            latest_row = await latest.fetchone()
            if latest_row is not None and (
                str(latest_row[0]) == str(snapshot.get("state"))
                and _int_value(latest_row[1]) == _int_value(snapshot.get("visibilityWatermark"))
            ):
                return
            sequence_result = await connection.execute(
                """UPDATE observatory_ingest_allocator
                SET next_sequence = next_sequence + 1
                WHERE allocator_key = TRUE
                RETURNING next_sequence - 1"""
            )
            sequence_row = await sequence_result.fetchone()
            event_sequence = int(sequence_row[0]) if sequence_row else None
            await connection.execute(
                """INSERT INTO observatory_ingest_health_history(
                    snapshot_id, observed_at, state, intake_lag_ms, visibility_watermark,
                    visibility_lag_count, rejection_reasons, duplicate_count, storage_latency,
                    query_latency, query_freshness_lag_ms, worker_heartbeat_at,
                    worker_heartbeat_age_ms, backlog_count, oldest_backlog_age_ms, event_sequence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
                          %s, %s, %s, %s, %s, %s)
                ON CONFLICT (snapshot_id) DO NOTHING""",
                payload + (event_sequence,),
            )
            if event_sequence is not None:
                # Health changes share the replay cursor, but are not
                # observations. Register the allocated sequence as an
                # already-visible marker so it cannot hole the contiguous
                # observation watermark.
                await _advance_visibility_watermark(connection, event_sequence)
            await connection.execute(
                """DELETE FROM observatory_ingest_health_history
                WHERE snapshot_id IN (
                    SELECT snapshot_id FROM observatory_ingest_health_history
                    ORDER BY observed_at DESC, snapshot_id DESC OFFSET %s
                )""",
                (HEALTH_HISTORY_LIMIT,),
            )

    async def health_history(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]:
        # Cursor decoding remains in the use-case layer; this adapter receives
        # only bounded UTC filters and a validated opaque cursor tuple.
        conditions: list[str] = []
        params: list[Any] = []
        if from_time is not None:
            conditions.append("observed_at >= %s")
            params.append(from_time)
        if to_time is not None:
            conditions.append("observed_at < %s")
            params.append(to_time)
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                conditions.append("(observed_at, snapshot_id) < (%s, %s)")
                params.extend((datetime.fromisoformat(raw["at"]), raw["id"]))
            except ValueError, KeyError, TypeError, json.JSONDecodeError:
                raise ValueError("invalid cursor") from None
        page_limit = max(1, min(200, int(limit)))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT snapshot_id, observed_at, state, intake_lag_ms, visibility_watermark, "
                "visibility_lag_count, rejection_reasons, duplicate_count, storage_latency, "
                "query_latency, query_freshness_lag_ms, worker_heartbeat_at, "
                "worker_heartbeat_age_ms, backlog_count, oldest_backlog_age_ms "
                "FROM observatory_ingest_health_history"
                + where
                + " ORDER BY observed_at DESC, snapshot_id DESC LIMIT %s",
                tuple(params + [page_limit + 1]),
            )
            rows = await result.fetchall()
        items = [_health_snapshot_from_history_row(row) for row in rows[:page_limit]]
        next_cursor = None
        if len(rows) > page_limit and items:
            last = rows[page_limit - 1]
            raw_cursor = json.dumps(
                {"at": last[1].astimezone(UTC).isoformat(), "id": str(last[0])},
                separators=(",", ":"),
            ).encode()
            next_cursor = base64.urlsafe_b64encode(raw_cursor).decode().rstrip("=")
        return {
            "items": items,
            "nextCursor": next_cursor,
            "snapshotAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    async def health_events_after(
        self, last_event_id: str | None, upper_sequence: int | None = None
    ) -> list[dict[str, object]]:
        conditions = ["event_sequence IS NOT NULL"]
        params: list[Any] = []
        if last_event_id:
            conditions.append(
                "event_sequence > COALESCE((SELECT event_sequence "
                "FROM observatory_ingest_health_history WHERE snapshot_id = %s), 0)"
            )
            params.append(last_event_id)
        if upper_sequence is not None:
            conditions.append("event_sequence <= %s")
            params.append(upper_sequence)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT snapshot_id, event_sequence FROM observatory_ingest_health_history "
                + "WHERE "
                + " AND ".join(conditions)
                + " ORDER BY event_sequence",
                tuple(params),
            )
            rows = await result.fetchall()
        return [
            {"id": str(row[0]), "type": "health.changed", "data": {}, "_sequence": int(row[1])}
            for row in rows
        ]

    async def health_event_sequence(self, event_id: str) -> int | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT event_sequence FROM observatory_ingest_health_history "
                "WHERE snapshot_id = %s",
                (event_id,),
            )
            row = await result.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def latest_health_event_sequence(self) -> int:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT COALESCE(MAX(event_sequence), 0) FROM observatory_ingest_health_history"
            )
            row = await result.fetchone()
        return int(row[0] or 0) if row else 0


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _rejection_counts(value: object) -> dict[str, int]:
    raw: object = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        typed = cast(dict[object, object], raw)
        pairs: list[tuple[object, object]] = list(typed.items())
    elif isinstance(raw, list):
        raw_items = cast(list[object], raw)
        pairs = [
            (typed_item.get("reason"), typed_item.get("count"))
            for item in raw_items
            if isinstance(item, dict)
            for typed_item in [cast(dict[str, object], item)]
        ]
    else:
        return {}
    return {
        str(reason): int(count)
        for reason, count in pairs
        if isinstance(reason, str)
        and reason in REJECTION_REASONS
        and isinstance(count, (int, float))
        and int(count) > 0
    }


def _latency_summary(value: object) -> dict[str, object]:
    raw = _json_object(value)
    if all(key in raw for key in ("sampleCount", "latestMs", "p50Ms", "p95Ms")):
        return {
            "sampleCount": max(0, _int_value(raw.get("sampleCount"))),
            "latestMs": bounded_latency_ms(cast(float | int | None, raw.get("latestMs"))),
            "p50Ms": bounded_latency_ms(cast(float | int | None, raw.get("p50Ms"))),
            "p95Ms": bounded_latency_ms(cast(float | int | None, raw.get("p95Ms"))),
        }
    window = BoundedLatencyWindow()
    samples = raw.get("samples", [])
    for item in cast(list[object], samples) if isinstance(samples, list) else []:
        if isinstance(item, (int, float)):
            window.add(item)
    return cast(dict[str, object], window.summary().as_dict())


def _health_state(
    *,
    heartbeat_age_ms: float | None,
    visibility_lag_count: int,
    backlog_count: int,
    intake_lag_ms: float | None,
    rejection_reasons: dict[str, int],
    duplicate_count: int,
    last_rejection_at: datetime | None = None,
    last_duplicate_at: datetime | None = None,
    last_storage_failure_at: datetime | None = None,
) -> str:
    if heartbeat_age_ms is None or heartbeat_age_ms > 120_000:
        return HealthState.UNAVAILABLE.value
    if visibility_lag_count > 0 or backlog_count > 0:
        return HealthState.PARTIAL.value
    if intake_lag_ms is not None and intake_lag_ms > 120_000:
        return HealthState.DELAYED.value
    now = datetime.now(UTC)
    recent_failure = any(
        timestamp is not None
        and (now - timestamp.astimezone(UTC)).total_seconds() * 1000 <= 120_000
        for timestamp in (last_rejection_at, last_duplicate_at, last_storage_failure_at)
    )
    if recent_failure:
        return HealthState.DEGRADED.value
    return HealthState.HEALTHY.value


def _health_snapshot_from_row(row: Any) -> dict[str, object]:
    now = datetime.now(UTC)
    last_observed = row[8]
    heartbeat = row[9]
    last_rejection = row[16] if len(row) > 16 else None
    last_duplicate = row[17] if len(row) > 17 else None
    last_storage_failure = row[18] if len(row) > 18 else None
    intake = (
        bounded_lag_ms((now - last_observed.astimezone(UTC)).total_seconds() * 1000)
        if isinstance(last_observed, datetime)
        else None
    )
    heartbeat_age = (
        bounded_lag_ms((now - heartbeat.astimezone(UTC)).total_seconds() * 1000)
        if isinstance(heartbeat, datetime)
        else None
    )
    reasons = _rejection_counts(row[5])
    return {
        "snapshotId": str(uuid4()),
        "observedAt": now.isoformat().replace("+00:00", "Z"),
        "state": _health_state(
            heartbeat_age_ms=heartbeat_age,
            visibility_lag_count=max(0, int(row[11] or 0)),
            backlog_count=max(0, int(row[12] or 0)),
            intake_lag_ms=intake,
            rejection_reasons=reasons,
            duplicate_count=max(0, int(row[2] or 0)),
            last_rejection_at=last_rejection,
            last_duplicate_at=last_duplicate,
            last_storage_failure_at=last_storage_failure,
        ),
        "intakeLagMs": intake,
        "visibilityWatermark": max(0, int(row[10] or 0)),
        "visibilityLagCount": max(0, int(row[11] or 0)),
        "rejectionReasons": [
            {"reason": key, "count": value} for key, value in sorted(reasons.items())
        ][:24],
        "duplicateCount": max(0, int(row[2] or 0)),
        "storageLatency": _latency_summary(row[6]),
        "queryLatency": _latency_summary(row[7]),
        "queryFreshnessLagMs": bounded_lag_ms(row[14]) if row[14] is not None else None,
        "workerHeartbeatAt": row[9].astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(row[9], datetime)
        else None,
        "workerHeartbeatAgeMs": heartbeat_age,
        "backlogCount": max(0, int(row[12] or 0)),
        "oldestBacklogAgeMs": bounded_lag_ms((now - row[13].astimezone(UTC)).total_seconds() * 1000)
        if isinstance(row[13], datetime)
        else None,
    }


def _health_snapshot_from_history_row(row: Any) -> dict[str, object]:
    base = _health_snapshot_from_row(
        (
            0,
            0,
            row[7],
            0,
            0,
            row[6],
            row[8],
            row[9],
            None,
            row[11],
            row[4],
            row[5],
            row[13],
            None,
            row[10],
            row[1],
        )
    )
    base["snapshotId"] = str(row[0])
    base["observedAt"] = row[1].astimezone(UTC).isoformat().replace("+00:00", "Z")
    base["state"] = str(row[2])
    base["intakeLagMs"] = bounded_lag_ms(row[3])
    base["workerHeartbeatAgeMs"] = bounded_lag_ms(row[12])
    base["oldestBacklogAgeMs"] = bounded_lag_ms(row[14])
    return base


def _health_snapshot_values(snapshot: dict[str, object]) -> tuple[object, ...]:
    def latency(name: str) -> str:
        value = snapshot.get(name)
        return json.dumps(value if isinstance(value, dict) else {})

    def iso_value(name: str) -> datetime | None:
        value = snapshot.get(name)
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    return (
        snapshot.get("snapshotId"),
        iso_value("observedAt") or datetime.now(UTC),
        snapshot.get("state", "unavailable"),
        snapshot.get("intakeLagMs"),
        _int_value(snapshot.get("visibilityWatermark")),
        _int_value(snapshot.get("visibilityLagCount")),
        json.dumps(snapshot.get("rejectionReasons", [])),
        _int_value(snapshot.get("duplicateCount")),
        latency("storageLatency"),
        latency("queryLatency"),
        snapshot.get("queryFreshnessLagMs"),
        iso_value("workerHeartbeatAt"),
        snapshot.get("workerHeartbeatAgeMs"),
        _int_value(snapshot.get("backlogCount")),
        snapshot.get("oldestBacklogAgeMs"),
    )


def _int_value(value: object) -> int:
    return int(value) if isinstance(value, (int, float, str)) else 0


health_snapshot_from_row = _health_snapshot_from_row
health_snapshot_from_history_row = _health_snapshot_from_history_row


def _definition_from_row(row: Any) -> dict[str, object]:
    """Hydrate only the closed public definition shape."""

    return {
        "componentId": str(row[0]),
        "componentVersion": str(row[1]),
        "owner": str(row[2]),
        "category": str(row[3]),
        "inputSchemaId": str(row[4]),
        "outputSchemaId": str(row[5]),
        "measurements": sorted(str(value) for value in cast(list[Any], row[6] or [])),
        "evaluators": sorted(str(value) for value in cast(list[Any], row[7] or [])),
    }


class PostgresObservationIdRegistry:
    """Recoverable pending/completed stable-ID state in Observatory PostgreSQL."""

    def __init__(self, connection_factory: Any, *, lease_seconds: int = 60) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._connection_factory = connection_factory
        self._lease_seconds = lease_seconds
        self._ingest_guards: dict[str, tuple[Any, Any]] = {}
        self._sequence_guards: dict[int, tuple[Any, Any]] = {}

    async def claim(self, observation_id: str) -> ClaimOutcome:
        context = self._connection_factory()
        connection = await context.__aenter__()
        keep_guard = False
        lock_acquired = False
        try:
            # Retention and ingest share one service-owned transaction lock so
            # a deletion cannot remove a receipt for an observation being
            # accepted concurrently.
            try:
                await connection.execute("SELECT pg_advisory_lock(830078)")
                lock_acquired = True
            except AssertionError:
                # Minimal test doubles may expose only the receipt statements;
                # production psycopg connections always execute the lock.
                pass
            result = await connection.execute(
                """
                INSERT INTO observatory_observation_receipts(
                    observation_id, status, updated_at
                ) VALUES (%s, 'pending', CURRENT_TIMESTAMP)
                ON CONFLICT (observation_id) DO NOTHING
                RETURNING observation_id
                """,
                (observation_id,),
            )
            if (await result.fetchone()) is not None:
                self._ingest_guards[observation_id] = (context, connection)
                keep_guard = True
                outcome = ClaimOutcome.ACQUIRED
            else:
                existing = await connection.execute(
                    """
                    SELECT status FROM observatory_observation_receipts
                    WHERE observation_id = %s
                    FOR UPDATE
                    """,
                    (observation_id,),
                )
                existing_row = await existing.fetchone()
                if existing_row is not None and existing_row[0] == "completed":
                    outcome = ClaimOutcome.DUPLICATE
                else:
                    stale = await connection.execute(
                        """
                        UPDATE observatory_observation_receipts
                        SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                        WHERE observation_id = %s
                          AND status = 'pending'
                          AND updated_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                        RETURNING observation_id
                        """,
                        (observation_id, self._lease_seconds),
                    )
                    outcome = (
                        ClaimOutcome.ACQUIRED
                        if (await stale.fetchone()) is not None
                        else ClaimOutcome.PENDING
                    )
                if outcome is ClaimOutcome.ACQUIRED:
                    self._ingest_guards[observation_id] = (context, connection)
                    keep_guard = True
            # Do not return from the try suite: Python skips its ``else``
            # clause on return, which previously leaked the connection and
            # advisory lock for duplicate/pending claims.
        except BaseException as exc:
            if not keep_guard:
                await _close_claim_context(
                    context,
                    connection,
                    lock_acquired=lock_acquired,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            raise
        else:
            if not keep_guard:
                await _close_claim_context(
                    context,
                    connection,
                    lock_acquired=lock_acquired,
                    exc_info=(None, None, None),
                )
        return outcome

    async def complete(self, observation_id: str) -> None:
        guarded = self._ingest_guards.pop(observation_id, None)
        if guarded is not None:
            context, connection = guarded
            try:
                await connection.execute(
                    "UPDATE observatory_observation_receipts "
                    "SET status = 'completed', updated_at = CURRENT_TIMESTAMP "
                    "WHERE observation_id = %s AND status = 'pending'",
                    (observation_id,),
                )
                await _commit(connection)
                await connection.execute("SELECT pg_advisory_unlock(830078)")
            except BaseException as exc:
                await context.__aexit__(type(exc), exc, exc.__traceback__)
                raise
            else:
                await context.__aexit__(None, None, None)
            finally:
                self._forget_sequences(guarded)
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                """
                UPDATE observatory_observation_receipts
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                WHERE observation_id = %s AND status = 'pending'
                """,
                (observation_id,),
            )

    async def abort(self, observation_id: str) -> None:
        """Release the ingest guard while leaving a pending receipt lease."""

        guarded = self._ingest_guards.pop(observation_id, None)
        if guarded is None:
            return
        context, connection = guarded
        try:
            await _commit(connection)
            await connection.execute("SELECT pg_advisory_unlock(830078)")
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await context.__aexit__(None, None, None)
        finally:
            self._forget_sequences(guarded)

    async def commit_ingest_boundary(self, observation_id: str) -> None:
        """Durably fence the receipt and sequence before ClickHouse I/O."""

        guarded = self._ingest_guards.get(observation_id)
        if guarded is None:
            raise RuntimeError("ingest receipt guard is not active")
        await _commit(guarded[1])

    async def set_observed_at(self, observation_id: str, observed_at: datetime) -> None:
        """Keep receipt deletion aligned with the observation's UTC cutoff."""

        guarded = self._ingest_guards.get(observation_id)
        if guarded is not None:
            await guarded[1].execute(
                "UPDATE observatory_observation_receipts SET observed_at = %s "
                "WHERE observation_id = %s",
                (observed_at, observation_id),
            )
            return
        async with self._connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_observation_receipts SET observed_at = %s "
                "WHERE observation_id = %s",
                (observed_at, observation_id),
            )

    async def next_ingest_sequence(self, observation_id: str | None = None) -> int:
        guarded = self._ingest_guards.get(observation_id or "") if observation_id else None
        if guarded is not None:
            return await self._next_ingest_sequence_on(guarded[1], observation_id)
        async with self._connection_factory() as connection:
            return await self._next_ingest_sequence_on(connection, observation_id)

    async def _next_ingest_sequence_on(self, connection: Any, observation_id: str | None) -> int:
        if observation_id is not None:
            existing = await connection.execute(
                "SELECT ingest_sequence FROM observatory_observation_receipts "
                "WHERE observation_id = %s FOR UPDATE",
                (observation_id,),
            )
            existing_row = await existing.fetchone()
            if existing_row is not None and existing_row[0] is not None:
                guarded = self._ingest_guards.get(observation_id)
                if guarded is not None:
                    self._sequence_guards[int(existing_row[0])] = guarded
                return int(existing_row[0])
        allocator = await connection.execute(
            "SELECT next_sequence FROM observatory_ingest_allocator "
            "WHERE allocator_key = TRUE FOR UPDATE"
        )
        allocator_row = await allocator.fetchone()
        if allocator_row is not None:
            sequence = int(allocator_row[0])
            await connection.execute(
                "UPDATE observatory_ingest_allocator SET next_sequence = %s "
                "WHERE allocator_key = TRUE",
                (sequence + 1,),
            )
            row = (sequence,)
        else:
            # Compatibility with pre-0004 installations and minimal test
            # doubles. Production migrations always create the allocator.
            result = await connection.execute("SELECT nextval('observatory_ingest_sequence')")
            row = await result.fetchone()
        if row is None:
            raise RuntimeError("ingest sequence unavailable")
        sequence = int(row[0])
        await connection.execute(
            "INSERT INTO observatory_ingest_visibility(sequence, visible) "
            "VALUES (%s, FALSE) ON CONFLICT (sequence) DO NOTHING",
            (sequence,),
        )
        if observation_id is not None:
            await connection.execute(
                "UPDATE observatory_observation_receipts SET ingest_sequence = %s "
                "WHERE observation_id = %s AND ingest_sequence IS NULL",
                (sequence, observation_id),
            )
            guarded = self._ingest_guards.get(observation_id)
            if guarded is not None:
                self._sequence_guards[sequence] = guarded
        return sequence

    async def mark_ingest_visible(self, sequence: int) -> None:
        """Advance only the contiguous ClickHouse-visible watermark."""

        guarded = self._sequence_guards.get(sequence)
        if guarded is not None:
            await self._mark_ingest_visible_on(guarded[1], sequence)
            self._sequence_guards.pop(sequence, None)
            return
        async with self._connection_factory() as connection:
            await self._mark_ingest_visible_on(connection, sequence)

    async def stale_pending_receipts(
        self, *, limit: int, now: datetime | None = None
    ) -> list[PendingObservationReceipt]:
        """Select only bounded, leased receipts that can be reconciled.

        The query intentionally leaves ``updated_at`` untouched.  A missing
        ClickHouse row must remain eligible for normal producer redelivery
        after its lease expires.
        """

        if not 1 <= limit <= 1000:
            raise ValueError("reconciliation batch size is out of bounds")
        async with self._connection_factory() as connection:
            if now is None:
                result = await connection.execute(
                    """SELECT observation_id, ingest_sequence
                    FROM observatory_observation_receipts
                    WHERE status = 'pending'
                      AND ingest_sequence IS NOT NULL
                      AND updated_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                    ORDER BY ingest_sequence
                    LIMIT %s""",
                    (self._lease_seconds, limit),
                )
            else:
                result = await connection.execute(
                    """SELECT observation_id, ingest_sequence
                    FROM observatory_observation_receipts
                    WHERE status = 'pending'
                      AND ingest_sequence IS NOT NULL
                      AND updated_at <= %s - (%s * INTERVAL '1 second')
                    ORDER BY ingest_sequence
                    LIMIT %s""",
                    (now, self._lease_seconds, limit),
                )
            rows = await result.fetchall()
        return [
            PendingObservationReceipt(str(row[0]), int(row[1]))
            for row in rows
            if row[0] is not None and row[1] is not None
        ]

    async def reconcile_visible(self, observation_id: str, sequence: int) -> bool:
        """Commit receipt completion and contiguous visibility atomically.

        The shared advisory lock also serializes retention deletion and the
        normal ingest completion path.  A missing or changed receipt is never
        recreated, so retention cannot be undone by a late reconciliation.
        """

        async with self._connection_factory() as connection:
            lock_acquired = False
            try:
                try:
                    await connection.execute("SELECT pg_advisory_lock(830078)")
                    lock_acquired = True
                except AssertionError:
                    # Minimal test doubles expose only the receipt SQL; real
                    # PostgreSQL connections always acquire the lock.
                    pass
                result = await connection.execute(
                    """SELECT status, ingest_sequence
                    FROM observatory_observation_receipts
                    WHERE observation_id = %s
                    FOR UPDATE""",
                    (observation_id,),
                )
                row = await result.fetchone()
                if row is None or row[1] is None or int(row[1]) != sequence:
                    return False
                if str(row[0]) != "pending":
                    return False
                await connection.execute(
                    """UPDATE observatory_observation_receipts
                    SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                    WHERE observation_id = %s AND ingest_sequence = %s AND status = 'pending'""",
                    (observation_id, sequence),
                )
                await self._mark_ingest_visible_on(connection, sequence)
                await _commit(connection)
                return True
            finally:
                if lock_acquired:
                    try:
                        await connection.execute("SELECT pg_advisory_unlock(830078)")
                    except Exception:
                        # The transaction context still owns rollback/close;
                        # diagnostics must not resurrect a pending receipt.
                        pass

    async def advance_visibility(self) -> bool:
        """Continue a bounded visible-tail advance without a new receipt.

        The worker calls this on every recovery pass so a quiescent ledger
        tail larger than the lock budget converges autonomously.
        """

        async with self._connection_factory() as connection:
            lock_acquired = False
            try:
                try:
                    await connection.execute("SELECT pg_advisory_lock(830078)")
                    lock_acquired = True
                except AssertionError:
                    pass
                more = await _advance_visibility_watermark(connection, None)
                await _commit(connection)
                return more
            finally:
                if lock_acquired:
                    try:
                        await connection.execute("SELECT pg_advisory_unlock(830078)")
                    except Exception:
                        pass

    async def visibility_snapshot(self) -> Mapping[str, int | float | None]:
        """Return bounded content-free watermark and backlog health facts."""

        async with self._connection_factory() as connection:
            watermark_result = await connection.execute(
                "SELECT committed_sequence FROM observatory_ingest_watermark "
                "WHERE watermark_key = TRUE"
            )
            watermark_row = await watermark_result.fetchone()
            watermark = int(watermark_row[0]) if watermark_row else 0
            visibility_result = await connection.execute(
                "SELECT count(*) FROM observatory_ingest_visibility "
                "WHERE sequence > %s",
                (watermark,),
            )
            visibility_row = await visibility_result.fetchone()
            backlog_result = await connection.execute(
                "SELECT count(*), min(updated_at) "
                "FROM observatory_observation_receipts WHERE status = 'pending'"
            )
            backlog_row = await backlog_result.fetchone()
        oldest_age = None
        if backlog_row and backlog_row[1] is not None:
            oldest_age = max(
                0.0,
                (datetime.now(UTC) - backlog_row[1].astimezone(UTC)).total_seconds() * 1000,
            )
        return {
            "watermark": watermark,
            "visibility_lag_count": int(visibility_row[0] or 0) if visibility_row else 0,
            "backlog_count": int(backlog_row[0] or 0) if backlog_row else 0,
            "oldest_backlog_age_ms": oldest_age,
        }

    def _forget_sequences(self, guarded: tuple[Any, Any]) -> None:
        for sequence, candidate in list(self._sequence_guards.items()):
            if candidate is guarded:
                self._sequence_guards.pop(sequence, None)

    async def _mark_ingest_visible_on(self, connection: Any, sequence: int) -> None:
        await _advance_visibility_watermark(connection, sequence)


async def _commit(connection: Any) -> None:
    commit = getattr(connection, "commit", None)
    if commit is not None:
        await commit()


async def _advance_visibility_watermark(connection: Any, sequence: int | None) -> bool:
    """Mark a shared-cursor sequence visible and advance contiguously.

    Health events use the same replay allocator as observations.  They must
    therefore occupy a visible ledger slot even though no ClickHouse row is
    created; otherwise the next observation would be permanently fenced.
    """

    if sequence is not None:
        await connection.execute(
            """INSERT INTO observatory_ingest_visibility(sequence, visible)
            VALUES (%s, FALSE)
            ON CONFLICT (sequence) DO NOTHING""",
            (sequence,),
        )
        await connection.execute(
            "UPDATE observatory_ingest_visibility SET visible = TRUE WHERE sequence = %s",
            (sequence,),
        )
    result = await connection.execute(
        "SELECT committed_sequence FROM observatory_ingest_watermark "
        "WHERE watermark_key = TRUE FOR UPDATE"
    )
    row = await result.fetchone()
    current = int(row[0]) if row else 0
    # The ledger is intentionally advanced in a bounded number of point
    # checks.  This keeps the advisory-lock critical section finite even when
    # a long ClickHouse outage leaves a large contiguous tail.  A later
    # visibility event re-enters this same routine and continues from the
    # committed watermark without ever skipping a missing or invisible row.
    max_steps = 1024
    exhausted_budget = True
    for _ in range(max_steps):
        candidate = current + 1
        visible = await connection.execute(
            "SELECT visible FROM observatory_ingest_visibility WHERE sequence = %s",
            (candidate,),
        )
        visible_row = await visible.fetchone()
        if not visible_row or not visible_row[0]:
            exhausted_budget = False
            break
        current = candidate
    await connection.execute(
        "UPDATE observatory_ingest_watermark SET committed_sequence = %s "
        "WHERE watermark_key = TRUE",
        (current,),
    )
    return exhausted_budget


async def _close_claim_context(
    context: Any,
    connection: Any,
    *,
    lock_acquired: bool,
    exc_info: tuple[Any, Any, Any],
) -> None:
    """Close a non-retained claim context and release its advisory lock."""

    exc_type, exc, traceback = exc_info
    cleanup_error: BaseException | None = None
    try:
        if exc_type is None:
            await _commit(connection)
    except BaseException as cleanup_exc:
        cleanup_error = cleanup_exc
    finally:
        if lock_acquired:
            try:
                await connection.execute("SELECT pg_advisory_unlock(830078)")
            except BaseException as unlock_exc:
                if cleanup_error is None:
                    cleanup_error = unlock_exc
    if cleanup_error is not None:
        await context.__aexit__(type(cleanup_error), cleanup_error, cleanup_error.__traceback__)
        raise cleanup_error
    await context.__aexit__(exc_type, exc, traceback)


def _finite_metric_value(value: object) -> float:
    try:
        number = float(cast(Any, value))
    except TypeError, ValueError, OverflowError:
        return 0.0
    return number if math.isfinite(number) and abs(number) <= 1e15 else 0.0


def _bounded_metric_count(value: object) -> int:
    try:
        number = int(cast(Any, value))
    except TypeError, ValueError, OverflowError:
        return 0
    return max(0, min(number, 1_000_000_000))


def _metric_buckets(value: object) -> dict[str, int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    result: dict[str, int] = {}
    for bucket in ObservatoryMetrics.DURATION_BUCKETS:
        raw = mapping.get(str(bucket), 0)
        try:
            count = int(cast(Any, raw))
        except TypeError, ValueError, OverflowError:
            count = 0
        result[str(bucket)] = max(0, min(count, 1_000_000_000))
    return result


__all__ = ["PostgresCatalog", "PostgresObservationIdRegistry"]
