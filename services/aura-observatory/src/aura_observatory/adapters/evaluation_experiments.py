# ruff: noqa: E501
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportReturnType=false, reportPrivateUsage=false, reportUnnecessaryIsInstance=false

"""PostgreSQL adapter for the Observatory experiment ownership zone."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from aura_observatory.evaluation_experiments import (
    EvidenceAssessment,
    ExperimentConflict,
    ExperimentError,
    ExperimentRepository,
    ExperimentUnavailable,
    _version,
)
from aura_observatory.evaluation_storage import DeletionKind, DeletionSelector
from aura_observatory.ports import ExperimentRetentionPlan

if TYPE_CHECKING:
    from aura_observatory.ports import (
        EvaluationDefinitionRepository,
        EvaluationDeletionReferencePort,
        EvaluationResultReferencePort,
        EvaluationScoreReferencePort,
        ObservatoryCatalog,
        ObservatoryQueryPort,
    )

_AUTHORIZATION_PURPOSES = frozenset(
    {"synthetic_benchmark", "owner_supplied_evaluation", "controlled_replay", "human_review"}
)


def _uuid_list(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values or len(values) > 100_000:
        raise ExperimentError("evidence result identities are invalid")
    try:
        parsed = tuple(str(UUID(str(value))) for value in values)
    except (TypeError, ValueError) as exc:
        raise ExperimentError("evidence result identities are invalid") from exc
    if len(set(parsed)) != len(parsed):
        raise ExperimentError("evidence result identities are not unique")
    return parsed


def _require_command_key(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise ExperimentError("idempotency key is invalid") from exc
    if parsed != value:
        raise ExperimentError("idempotency key is invalid")
    return value


def _translate_provider_failures(function: Any) -> Any:
    """Keep transport routes deterministic when a backing provider fails."""

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except (ExperimentError, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("experiment metadata provider is unavailable") from exc

    wrapped.__name__ = getattr(function, "__name__", "provider_call")
    return wrapped


@dataclass(frozen=True, slots=True)
class PostgresCommandClaim:
    replay: Mapping[str, object] | None
    claim_token: str | None = None


class PostgresExperimentRepository(ExperimentRepository):
    """Persist closed experiment projections in Observatory PostgreSQL only."""

    @property
    def history_rows(self) -> list[dict[str, object]]:
        """Compatibility-only view; PostgreSQL history remains authoritative."""

        return []

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        definition_repository: EvaluationDefinitionRepository | None = None,
        catalog: ObservatoryCatalog | None = None,
        query_port: ObservatoryQueryPort | None = None,
        deletion_reference: EvaluationDeletionReferencePort | None = None,
        metrics: Any | None = None,
        result_reference: EvaluationResultReferencePort | None = None,
        score_reference: EvaluationScoreReferencePort | None = None,
        clickhouse_url: str | None = None,
        clickhouse_database: str = "default",
        clickhouse_user: str = "default",
        clickhouse_password: str = "",
    ) -> None:
        self._connection_factory = connection_factory
        self._definition_repository = definition_repository
        self._deletion_reference = deletion_reference
        self._evidence = PostgresExperimentEvidencePort(
            connection_factory,
            clickhouse_url=clickhouse_url,
            clickhouse_database=clickhouse_database,
            clickhouse_user=clickhouse_user,
            clickhouse_password=clickhouse_password,
            metrics=metrics,
            result_reference=result_reference,
            score_reference=score_reference,
        )
        self._references = PostgresExperimentReferencePort(
            connection_factory,
            definition_repository=definition_repository,
            catalog=catalog,
            query_port=query_port,
            metrics=metrics,
            result_reference=result_reference,
        )

    @property
    def evidence(self) -> PostgresExperimentEvidencePort:
        return self._evidence

    @property
    def references(self) -> PostgresExperimentReferencePort:
        return self._references

    @staticmethod
    def _keys(resource_type: str, resource: Mapping[str, object]) -> tuple[str, str]:
        if resource_type == "experiments":
            return str(resource["experimentId"]), str(resource["experimentVersion"])
        if resource_type == "baselines":
            return str(resource["baselineId"]), str(resource["baselineVersion"])
        if resource_type == "regressions":
            return str(resource["regressionId"]), str(resource["regressionVersion"])
        raise ValueError("unsupported experiment resource")

    @staticmethod
    def _table(resource_type: str) -> str:
        return {
            "experiments": "observatory_evaluation_experiments",
            "baselines": "observatory_evaluation_baselines",
            "regressions": "observatory_evaluation_regressions",
        }[resource_type]

    @_translate_provider_failures
    async def put(self, resource_type: str, resource: Mapping[str, object]) -> None:
        projection_facts = resource.get("__projectionFacts")
        resource = dict(resource)
        resource.pop("__projectionFacts", None)
        identifier, version = self._keys(resource_type, resource)
        table = self._table(resource_type)
        async with self._connection_factory() as connection:
            if resource_type == "experiments":
                await connection.execute(
                    f"""INSERT INTO {table}
                    (experiment_id, experiment_version, state, suite_id, suite_version,
                     variants_json, cohorts_json, capture_classification, retention_mode,
                     provenance_json, resource_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s)
                    ON CONFLICT (experiment_id, experiment_version) DO NOTHING""",
                    (identifier, version, resource["state"], resource["suite"]["resourceId"], resource["suite"]["resourceVersion"], json.dumps(resource["variants"]), json.dumps(resource["cohorts"]), resource["captureClassification"], resource["retentionMode"], json.dumps(resource["provenance"]), json.dumps(resource), resource["createdAt"]),
                )
                if self._definition_repository is not None:
                    suite = await self._definition_repository.get_suite(
                        str(resource["suite"]["resourceId"]),
                        str(resource["suite"]["resourceVersion"]),
                    )
                    if suite is None:
                        raise ExperimentUnavailable("suite definition provider is unavailable")
                    dataset = suite.dataset
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_experiment_dataset_bindings(
                        experiment_id, experiment_version, dataset_id, dataset_version)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (experiment_id, experiment_version) DO NOTHING""",
                        (identifier, version, dataset.resource_id, dataset.version),
                    )
            elif resource_type == "baselines":
                await connection.execute(
                    f"""INSERT INTO {table}
                    (baseline_id, baseline_version, state, experiment_id, experiment_version,
                     suite_id, suite_version, variant_id, cohort_ids_json, source_result_ids_json,
                     source_result_from, source_result_to, thresholds_json, capture_classification,
                     reproducibility_json, immutable, promotion_mode, manual_promotion_record_id,
                     promoted_at, retention_mode, provenance_json, resource_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                            %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                    ON CONFLICT (baseline_id, baseline_version) DO NOTHING""",
                    (identifier, version, resource["state"], resource["experiment"]["resourceId"], resource["experiment"]["resourceVersion"], resource["suite"]["resourceId"], resource["suite"]["resourceVersion"], resource["variantId"], json.dumps(resource["cohortIds"]), json.dumps(resource["sourceResultIds"]), resource["sourceResultFrom"], resource["sourceResultTo"], json.dumps(resource["thresholds"]), resource["captureClassification"], json.dumps(resource["reproducibility"]), resource["immutable"], resource["promotionMode"], resource["manualPromotionRecordId"], resource["promotedAt"], resource["retentionMode"], json.dumps(resource["provenance"]), json.dumps(resource), resource["createdAt"]),
                )
            else:
                await connection.execute(
                    f"""INSERT INTO {table}
                    (regression_id, regression_version, experiment_id, experiment_version,
                     baseline_id, baseline_version, variant_id, cohort_ids_json,
                     baseline_result_ids_json, candidate_result_ids_json, comparisons_json,
                     decision, evidence_state, evidence_json, attribution_json, resource_json,
                     capture_policy, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
                    ON CONFLICT (regression_id, regression_version) DO NOTHING""",
                    (identifier, version, resource["experiment"]["resourceId"], resource["experiment"]["resourceVersion"], resource["baseline"]["resourceId"], resource["baseline"]["resourceVersion"], resource["variant"]["variantId"], json.dumps(resource["cohortIds"]), json.dumps(resource["baselineResultIds"]), json.dumps(resource["candidateResultIds"]), json.dumps(resource["comparisons"]), resource["decision"], resource["evidenceState"], json.dumps(resource["evidence"]), json.dumps(resource["attribution"]), json.dumps(resource), resource["capturePolicy"], resource["createdAt"]),
                )
                comparisons = resource.get("comparisons")
                if isinstance(comparisons, list):
                    for threshold_index, comparison in enumerate(comparisons):
                        if not isinstance(comparison, Mapping):
                            continue
                        metric_id = str(comparison.get("metric", ""))
                        if not metric_id:
                            continue
                        projection_payload = {
                            "capturePolicy": "metadata_only",
                            "regressionId": identifier,
                            "regressionVersion": version,
                            "metric": metric_id,
                            "thresholdIndex": threshold_index,
                            "comparison": dict(comparison),
                            "baselineResultIds": list(resource["baselineResultIds"]),
                            "candidateResultIds": list(resource["candidateResultIds"]),
                            "cohortIds": list(resource["cohortIds"]),
                        }
                        if isinstance(projection_facts, list):
                            matching = next(
                                (
                                    fact
                                    for fact in projection_facts
                                    if isinstance(fact, Mapping)
                                    and fact.get("thresholdIndex") == threshold_index
                                ),
                                None,
                            )
                            if matching is not None:
                                projection_payload.update(
                                    {
                                        key: matching[key]
                                        for key in (
                                            "baselineValues",
                                            "candidateValues",
                                            "metricClass",
                                            "evidenceState",
                                        )
                                        if key in matching
                                    }
                                )
                        await connection.execute(
                            """INSERT INTO observatory_evaluation_regression_projection_outbox(
                            projection_id, regression_id, regression_version, metric_id, threshold_index,
                            payload_json, status) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'pending')
                            ON CONFLICT (regression_id, regression_version, metric_id, threshold_index) DO NOTHING""",
                            (str(uuid4()), identifier, version, metric_id, threshold_index, json.dumps(projection_payload)),
                        )
            await connection.commit()
            existing = await self.get(resource_type, identifier, version)
            if existing is not None and existing != dict(resource):
                raise ExperimentConflict("immutable resource conflicts with existing state")

    @_translate_provider_failures
    async def claim_projection_outbox(
        self, *, limit: int = 16, lease_seconds: int = 120
    ) -> list[dict[str, object]]:
        bounded_limit = max(1, min(64, int(limit)))
        bounded_lease = max(30, min(900, int(lease_seconds)))
        token = str(uuid4())
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """WITH claimable AS (
                    SELECT projection_id
                    FROM observatory_evaluation_regression_projection_outbox
                    WHERE status IN ('pending', 'failed')
                      AND NOT cleanup_pending
                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    ORDER BY created_at, projection_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE observatory_evaluation_regression_projection_outbox AS p
                SET status = 'pending', lease_token = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s || ' seconds')::interval,
                    attempts = p.attempts + 1
                FROM claimable c
                WHERE p.projection_id = c.projection_id
                RETURNING p.projection_id, p.regression_id, p.regression_version,
                          p.metric_id, p.threshold_index, p.payload_json, p.lease_token""",
                (bounded_limit, token, bounded_lease),
            )
            rows = await result.fetchall()
            await connection.commit()
        return [
            {
                "projectionId": str(row[0]),
                "regressionId": str(row[1]),
                "regressionVersion": str(row[2]),
                "metric": str(row[3]),
                "thresholdIndex": int(row[4]),
                "payload": row[5] if isinstance(row[5], Mapping) else json.loads(str(row[5])),
                "leaseToken": str(row[6]),
            }
            for row in rows
        ]

    @_translate_provider_failures
    async def claim_projection_cleanup(
        self, *, limit: int = 16, lease_seconds: int = 120
    ) -> list[dict[str, object]]:
        """Claim tombstoned projection identities awaiting CH compensation."""

        bounded_limit = max(1, min(64, int(limit)))
        bounded_lease = max(30, min(900, int(lease_seconds)))
        token = str(uuid4())
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """WITH claimable AS (
                    SELECT projection_id
                    FROM observatory_evaluation_regression_projection_outbox
                    WHERE cleanup_pending
                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    ORDER BY created_at, projection_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE observatory_evaluation_regression_projection_outbox AS p
                SET lease_token = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s || ' seconds')::interval
                FROM claimable c
                WHERE p.projection_id = c.projection_id
                RETURNING p.projection_id, p.regression_id, p.lease_token""",
                (bounded_limit, token, bounded_lease),
            )
            rows = await result.fetchall()
            await connection.commit()
        return [
            {"projectionId": str(row[0]), "regressionId": str(row[1]), "leaseToken": str(row[2])}
            for row in rows
        ]

    @_translate_provider_failures
    async def complete_projection_cleanup(
        self, projection_id: str, lease_token: str
    ) -> bool:
        """Clear a durable cleanup tombstone after exact CH deletion."""

        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET cleanup_pending = FALSE,
                    status = CASE WHEN status = 'deleted' THEN 'deleted' ELSE 'failed' END,
                    last_error = CASE WHEN status = 'deleted' THEN NULL ELSE 'projection_cleanup_complete' END,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE projection_id = %s AND cleanup_pending
                  AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING projection_id""",
                (projection_id, lease_token),
            )
            changed = await result.fetchone()
            if changed is None:
                await connection.rollback()
                return False
            await connection.commit()
        return True

    @_translate_provider_failures
    async def mark_projection_cleanup_pending(self, projection_id: str) -> None:
        """Persist a content-free retry marker when CH compensation is down."""

        async with self._connection_factory() as connection:
            await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET cleanup_pending = TRUE, payload_json = '{}'::jsonb,
                    last_error = 'projection_cleanup_pending',
                    lease_token = NULL, lease_expires_at = NULL
                WHERE projection_id = %s""",
                (projection_id,),
            )
            await connection.commit()

    @_translate_provider_failures
    async def complete_projection_outbox(
        self,
        projection_id: str,
        lease_token: str,
        *,
        applied: bool,
        error_code: str | None = None,
    ) -> bool:
        status = "applied" if applied else "failed"
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET status = %s, applied_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE applied_at END,
                    last_error = %s, lease_token = NULL, lease_expires_at = NULL
                WHERE projection_id = %s AND lease_token = %s
                  AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING projection_id""",
                (status, applied, None if applied else (error_code or "provider_unavailable"), projection_id, lease_token),
            )
            changed = await result.fetchone()
            if changed is None:
                await connection.rollback()
                raise ExperimentUnavailable("projection lease was lost")
            await connection.commit()
        return True

    @_translate_provider_failures
    async def renew_projection_outbox(
        self, projection_id: str, lease_token: str, *, lease_seconds: int = 120
    ) -> bool:
        bounded_lease = max(30, min(900, int(lease_seconds)))
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET lease_expires_at = CURRENT_TIMESTAMP + (%s || ' seconds')::interval
                WHERE projection_id = %s AND status = 'pending'
                  AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING projection_id""",
                (bounded_lease, projection_id, lease_token),
            )
            changed = await result.fetchone()
            if changed is None:
                await connection.rollback()
                return False
            await connection.commit()
        return True

    @_translate_provider_failures
    async def reconcile_projection_outbox(self, *, limit: int = 16) -> int:
        """Replay leased projection facts after a worker/provider restart."""
        bounded = max(1, min(64, int(limit)))
        cleanup_claimed = await self.claim_projection_cleanup(limit=bounded)
        applied = 0
        for item in cleanup_claimed:
            try:
                await self._evidence.delete_projection_facts(
                    str(item["projectionId"]), lock_key=str(item["regressionId"])
                )
                if await self.complete_projection_cleanup(
                    str(item["projectionId"]), str(item["leaseToken"])
                ):
                    applied += 1
            except ExperimentUnavailable:
                continue
        claimed = await self.claim_projection_outbox(limit=bounded)
        for item in claimed:
            payload = item.get("payload")
            if not isinstance(payload, Mapping):
                await self.complete_projection_outbox(
                    str(item["projectionId"]), str(item["leaseToken"]), applied=False,
                    error_code="projection_payload_invalid",
                )
                continue
            try:
                await self._evidence.persist_facts(
                    str(item["regressionId"]),
                    str(item["regressionVersion"]),
                    str(item["metric"]),
                    str(payload.get("metricClass", "operational")),
                    tuple(str(value) for value in payload.get("cohortIds", ())),
                    tuple(float(value) for value in payload.get("baselineValues", ())),
                    tuple(float(value) for value in payload.get("candidateValues", ())),
                    tuple(str(value) for value in payload.get("baselineResultIds", ())),
                    tuple(str(value) for value in payload.get("candidateResultIds", ())),
                    str(payload.get("evidenceState", "missing")),
                    threshold_index=int(item["thresholdIndex"]),
                    projection_id=str(item["projectionId"]),
                    lease_token=str(item["leaseToken"]),
                )
                applied += 1
            except ExperimentUnavailable:
                continue
        return applied

    @_translate_provider_failures
    async def reconcile_command_history(self, *, limit: int = 16) -> int:
        """Rebuild deterministic lifecycle history after a command crash gap."""
        bounded_limit = max(1, min(64, int(limit)))
        inserted = 0
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT idempotency_key, operation, resource_id, resource_version, replay_json, status
                FROM observatory_evaluation_experiment_commands
                WHERE (
                    (status = 'completed' AND replay_json IS NOT NULL)
                    OR (status = 'pending' AND resource_id IS NOT NULL
                        AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP))
                )
                  AND history_reconciled_at IS NULL
                ORDER BY completed_at ASC, idempotency_key ASC LIMIT %s""",
                (bounded_limit,),
            )
            rows = await result.fetchall()
            for command_key, operation, resource_id, resource_version, raw, status in rows:
                response: Mapping[str, object] | None = None
                if isinstance(raw, Mapping):
                    response = raw
                elif raw is not None:
                    decoded = json.loads(str(raw))
                    if isinstance(decoded, Mapping):
                        response = decoded
                if response is None and str(status) == "pending" and resource_id is not None:
                    resource_table = {
                        "create_experiment": ("observatory_evaluation_experiments", "experiment_id"),
                        "create_baseline": ("observatory_evaluation_baselines", "baseline_id"),
                        "create_regression": ("observatory_evaluation_regressions", "regression_id"),
                        "promote_baseline": ("observatory_evaluation_baselines", "baseline_id"),
                    }.get(str(operation))
                    if resource_table is not None:
                        resource_result = await connection.execute(
                            f"SELECT resource_json, state FROM {resource_table[0]} "
                            f"WHERE {resource_table[1]} = %s AND {resource_table[1].replace('_id', '_version')} = %s",
                            (str(resource_id), str(resource_version)),
                        )
                        resource_row = await resource_result.fetchone()
                        if resource_row is not None:
                            candidate = resource_row[0]
                            candidate_state = str(resource_row[1])
                            if isinstance(candidate, Mapping) and (
                                str(operation) != "promote_baseline" or candidate_state == "promoted"
                            ):
                                response = candidate
                                await connection.execute(
                                    """UPDATE observatory_evaluation_experiment_commands
                                    SET status = 'completed', replay_json = %s::jsonb,
                                        completed_at = CURRENT_TIMESTAMP
                                    WHERE idempotency_key = %s AND status = 'pending'
                                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)""",
                                    (json.dumps(dict(candidate)), str(command_key)),
                                )
                if response is None:
                    continue
                resource_type = {
                    "create_experiment": "experiment",
                    "create_baseline": "baseline",
                    "promote_baseline": "baseline",
                    "create_regression": "regression",
                }.get(str(operation))
                if resource_type is None:
                    continue
                id_key = {
                    "experiment": "experimentId",
                    "baseline": "baselineId",
                    "regression": "regressionId",
                }[resource_type]
                version_key = {
                    "experiment": "experimentVersion",
                    "baseline": "baselineVersion",
                    "regression": "regressionVersion",
                }[resource_type]
                resource_id = str(response.get(id_key, ""))
                version = str(response.get(version_key, ""))
                if not resource_id or not version:
                    continue
                if operation == "promote_baseline":
                    change, state = "promoted", "promoted"
                elif operation == "create_baseline":
                    change, state = "candidate_created", "candidate"
                elif operation == "create_regression":
                    change, state = "comparison_completed", str(response.get("decision", "unavailable"))
                else:
                    change, state = "created", str(response.get("state", "active"))
                history_id = str(uuid5(NAMESPACE_URL, f"aura-0096:{resource_type}:{resource_id}:{version}:{change}:{state}"))
                history_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_experiment_history(
                    history_id, resource_type, resource_id, resource_version, change, state)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (history_id) DO NOTHING RETURNING history_id""",
                    (history_id, resource_type, resource_id, version, change, state),
                )
                if await history_result.fetchone() is not None:
                    inserted += 1
                if resource_type == "regression":
                    attribution = response.get("attribution")
                    if isinstance(attribution, Mapping):
                        attribution_id = str(attribution.get("attributionId", ""))
                        if attribution_id:
                            attribution_history_id = str(uuid5(NAMESPACE_URL, f"aura-0096:attribution:{attribution_id}:1.0.0:attribution_completed:{attribution.get('availability', 'unavailable')}"))
                            attribution_result = await connection.execute(
                                """INSERT INTO observatory_evaluation_experiment_history(
                                history_id, resource_type, resource_id, resource_version, change, state)
                                VALUES (%s, 'attribution', %s, '1.0.0', 'attribution_completed', %s)
                                ON CONFLICT (history_id) DO NOTHING RETURNING history_id""",
                                (attribution_history_id, attribution_id, str(attribution.get("availability", "unavailable"))),
                            )
                            if await attribution_result.fetchone() is not None:
                                inserted += 1
                reconciled = await connection.execute(
                    """UPDATE observatory_evaluation_experiment_commands
                    SET history_reconciled_at = CURRENT_TIMESTAMP
                    WHERE idempotency_key = %s AND history_reconciled_at IS NULL""",
                    (str(command_key),),
                )
                if int(getattr(reconciled, "rowcount", 0) or 0) == 0:
                    continue
            await connection.commit()
        return inserted

    @_translate_provider_failures
    async def get(self, resource_type: str, resource_id: str, version: str | None = None) -> Mapping[str, object] | None:
        table = self._table(resource_type)
        id_column = {"experiments": "experiment_id", "baselines": "baseline_id", "regressions": "regression_id"}[resource_type]
        version_column = {"experiments": "experiment_version", "baselines": "baseline_version", "regressions": "regression_version"}[resource_type]
        query = f"SELECT resource_json FROM {table} WHERE {id_column} = %s"
        params: list[object] = [resource_id]
        if version is not None:
            query += f" AND {version_column} = %s"
            params.append(version)
        query += f" ORDER BY {version_column} DESC LIMIT 1"
        async with self._connection_factory() as connection:
            result = await connection.execute(query, params)
            row = await result.fetchone()
        if row is None:
            return None
        value = row[0]
        return dict(value) if isinstance(value, Mapping) else json.loads(str(value))

    async def list(self, resource_type: str) -> list[Mapping[str, object]]:
        page = await self.list_page(resource_type, cursor=None, limit=200, filters=None)
        items = page.get("items")
        return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []

    @_translate_provider_failures
    async def list_page(
        self,
        resource_type: str,
        *,
        cursor: str | None,
        limit: int,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]:
        from aura_observatory.evaluation_experiments import (
            _decode_experiment_cursor,
            _encode_experiment_cursor,
        )

        if resource_type not in {"experiments", "baselines", "regressions"} or not 1 <= limit <= 200:
            raise ExperimentError("query is invalid")
        state = _decode_experiment_cursor(cursor)
        table = self._table(resource_type)
        id_column = {"experiments": "experiment_id", "baselines": "baseline_id", "regressions": "regression_id"}[resource_type]
        version_column = {"experiments": "experiment_version", "baselines": "baseline_version", "regressions": "regression_version"}[resource_type]
        where = ["created_at <= %s"]
        params: list[object] = [state["snapshotAt"]]
        if state["createdAt"] is not None:
            where.append("(created_at, " + id_column + ", " + version_column + ") < (%s, %s, %s)")
            params.extend([state["createdAt"], state["resourceId"], state["version"] or ""])
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            if key == "state":
                where.append("state = %s")
                params.append(expected)
            elif key == "suite" and resource_type in {"experiments", "baselines"}:
                where.extend(["suite_id = %s"])
                params.append(expected)
            elif key == "experiment" and resource_type in {"baselines", "regressions"}:
                where.append("experiment_id = %s")
                params.append(expected)
            elif key == "baseline" and resource_type == "regressions":
                where.append("baseline_id = %s")
                params.append(expected)
            elif key == "decision" and resource_type == "regressions":
                where.append("decision = %s")
                params.append(expected)
            else:
                raise ExperimentError("query filter is invalid")
        sql = f"SELECT resource_json FROM {table} WHERE {' AND '.join(where)} ORDER BY created_at DESC, {id_column} DESC, {version_column} DESC LIMIT %s"
        params.append(limit + 1)
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(sql, tuple(params))
                rows = await result.fetchall()
            values = [
                dict(row[0]) if isinstance(row[0], Mapping) else json.loads(str(row[0]))
                for row in rows
            ]
            items = values[:limit]
            next_cursor = None
            if len(values) > limit and items:
                item = items[-1]
                identifier = str(item.get("experimentId") or item.get("baselineId") or item.get("regressionId"))
                version = str(item.get("experimentVersion") or item.get("baselineVersion") or item.get("regressionVersion"))
                next_cursor = _encode_experiment_cursor(
                    snapshot_at=str(state["snapshotAt"]),
                    created_at=str(item["createdAt"]),
                    resource_id=identifier,
                    version=version,
                )
            return {"items": items, "nextCursor": next_cursor, "snapshotAt": state["snapshotAt"], "partial": False}
        except (ExperimentError, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("experiment query provider unavailable") from exc

    @_translate_provider_failures
    async def idempotent(self, operation: str, key: str, fingerprint: str, response: Mapping[str, object]) -> Mapping[str, object] | None:
        _require_command_key(key)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT request_fingerprint, replay_json FROM observatory_evaluation_experiment_commands WHERE idempotency_key = %s FOR UPDATE",
                (key,),
            )
            row = await result.fetchone()
            if row is not None:
                if str(row[0]) != fingerprint:
                    raise ExperimentConflict("idempotency key conflicts with existing state")
                value = row[1]
                if value is None:
                    raise ExperimentUnavailable("experiment command is in progress")
                return dict(value) if isinstance(value, Mapping) else json.loads(str(value))
            await connection.execute(
                "INSERT INTO observatory_evaluation_experiment_commands (idempotency_key, operation, request_fingerprint, replay_json) VALUES (%s, %s, %s, NULL)",
                (key, operation, fingerprint),
            )
            await connection.commit()
        return None

    @_translate_provider_failures
    async def complete_idempotent(self, operation: str, key: str, response: Mapping[str, object]) -> None:
        _require_command_key(key)
        del operation
        async with self._connection_factory() as connection:
            await connection.execute(
                "UPDATE observatory_evaluation_experiment_commands SET replay_json = %s::jsonb WHERE idempotency_key = %s AND replay_json IS NULL",
                (json.dumps(response), key),
            )
            await connection.commit()

    @_translate_provider_failures
    async def begin_command(
        self, operation: str, key: str, fingerprint: str, resource_id: str | None = None,
        resource_version: str | None = None,
    ) -> PostgresCommandClaim:
        _require_command_key(key)
        if resource_id is not None:
            _uuid_list([resource_id])
        if resource_version is not None:
            _version(resource_version, "resourceVersion")
        claim_token = str(uuid4())
        try:
            async with self._connection_factory() as connection:
                inserted = await connection.execute(
                    """INSERT INTO observatory_evaluation_experiment_commands(
                    idempotency_key, operation, resource_id, resource_version, request_fingerprint, status,
                    lease_token, lease_expires_at)
                    VALUES (%s, %s, %s, %s, %s, 'pending', %s, CURRENT_TIMESTAMP + INTERVAL '120 seconds')
                    ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key""",
                    (key, operation, resource_id, resource_version, fingerprint, claim_token),
                )
                row = await inserted.fetchone()
                if row is None:
                    takeover = await connection.execute(
                        """UPDATE observatory_evaluation_experiment_commands
                        SET lease_token = %s, lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds'
                        WHERE idempotency_key = %s AND operation = %s
                          AND request_fingerprint = %s AND status = 'pending'
                          AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                        RETURNING idempotency_key""",
                        (claim_token, key, operation, fingerprint),
                    )
                    row = await takeover.fetchone()
                result = await connection.execute(
                    """SELECT operation, resource_id, resource_version, request_fingerprint, status, replay_json, lease_token
                    FROM observatory_evaluation_experiment_commands WHERE idempotency_key = %s""",
                    (key,),
                )
                existing = await result.fetchone()
                if existing is None:
                    raise ExperimentUnavailable("experiment command disappeared")
                if str(existing[0]) != operation or str(existing[3]) != fingerprint:
                    raise ExperimentConflict("idempotency key conflicts with existing state")
                if resource_id is not None and existing[1] is not None and str(existing[1]) != resource_id:
                    raise ExperimentConflict("idempotency resource conflicts with existing state")
                if resource_version is not None and existing[2] is not None and str(existing[2]) != resource_version:
                    raise ExperimentConflict("idempotency resource version conflicts with existing state")
                if str(existing[4]) == "completed":
                    value = existing[5]
                    if not isinstance(value, Mapping):
                        value = json.loads(str(value))
                    return PostgresCommandClaim(dict(value))
                if row is None:
                    raise ExperimentUnavailable("experiment command is in progress")
                await connection.commit()
                return PostgresCommandClaim(None, claim_token)
        except (ExperimentConflict, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("experiment command storage unavailable") from exc

    @_translate_provider_failures
    async def complete_command(
        self,
        operation: str,
        key: str,
        fingerprint: str,
        response: Mapping[str, object],
        *,
        claim_token: str | None = None,
    ) -> None:
        _require_command_key(key)
        payload = json.dumps(dict(response), sort_keys=True, separators=(",", ":"))
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_experiment_commands
                    SET status = 'completed', replay_json = %s::jsonb,
                        completed_at = CURRENT_TIMESTAMP, lease_token = NULL,
                        lease_expires_at = NULL
                    WHERE idempotency_key = %s AND operation = %s
                      AND request_fingerprint = %s AND status = 'pending'
                      AND lease_token = %s
                      AND lease_expires_at > CURRENT_TIMESTAMP RETURNING idempotency_key""",
                    (payload, key, operation, fingerprint, claim_token),
                )
                if await result.fetchone() is None:
                    existing = await connection.execute(
                        "SELECT status, request_fingerprint FROM observatory_evaluation_experiment_commands WHERE idempotency_key = %s",
                        (key,),
                    )
                    row = await existing.fetchone()
                    if row is None or str(row[1]) != fingerprint:
                        raise ExperimentConflict("idempotency command claim is invalid")
                    if str(row[0]) != "completed":
                        raise ExperimentUnavailable("experiment command claim was lost")
                await connection.commit()
        except (ExperimentConflict, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("experiment command storage unavailable") from exc

    @_translate_provider_failures
    async def renew_command(
        self, operation: str, key: str, fingerprint: str, claim_token: str | None
    ) -> bool:
        if claim_token is None:
            return True
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_experiment_commands
                SET lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds'
                WHERE operation = %s AND idempotency_key = %s
                  AND request_fingerprint = %s AND status = 'pending'
                  AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING idempotency_key""",
                (operation, key, fingerprint, claim_token),
            )
            changed = await result.fetchone()
            if changed is None:
                await connection.rollback()
                raise ExperimentUnavailable("experiment command lease was lost")
            await connection.commit()
        return True

    @_translate_provider_failures
    async def promote_baseline(
        self,
        baseline_id: str,
        version: str,
        authorization_record_id: str,
        promoted_at: str,
        *,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        claim_token: str | None = None,
    ) -> Mapping[str, object]:
        key = idempotency_key or str(uuid4())
        _require_command_key(key)
        fingerprint = request_fingerprint or ("0" * 64)
        promotion_token = claim_token or str(uuid4())
        try:
            async with self._connection_factory() as connection:
                await connection.execute(
                    """INSERT INTO observatory_evaluation_baseline_promotions(
                    idempotency_key, baseline_id, baseline_version, request_fingerprint,
                    authorization_record_id, lease_token, lease_expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '120 seconds')
                    ON CONFLICT DO NOTHING""",
                    (key, baseline_id, version, fingerprint, authorization_record_id, promotion_token),
                )
                existing_promotion = await connection.execute(
                    """SELECT idempotency_key, status, replay_json, authorization_record_id
                    , request_fingerprint, lease_token, lease_expires_at
                    FROM observatory_evaluation_baseline_promotions
                    WHERE baseline_id = %s AND baseline_version = %s FOR UPDATE""",
                    (baseline_id, version),
                )
                promotion_row = await existing_promotion.fetchone()
                if promotion_row is None:
                    key_result = await connection.execute(
                        "SELECT baseline_id, baseline_version FROM observatory_evaluation_baseline_promotions WHERE idempotency_key = %s",
                        (key,),
                    )
                    key_row = await key_result.fetchone()
                    if key_row is not None:
                        raise ExperimentConflict("promotion idempotency key conflicts with another baseline")
                    raise ExperimentUnavailable("baseline promotion claim was not persisted")
                if promotion_row is not None and str(promotion_row[0]) != key:
                    raise ExperimentConflict("baseline version is already being promoted")
                if promotion_row is not None and str(promotion_row[4]) != fingerprint:
                    raise ExperimentConflict("promotion idempotency key conflicts with existing state")
                if promotion_row is not None and str(promotion_row[1]) == "completed":
                    replay = promotion_row[2]
                    if not isinstance(replay, Mapping):
                        replay = json.loads(str(replay))
                    await connection.commit()
                    return dict(replay)
                claimed = await connection.execute(
                    """UPDATE observatory_evaluation_baseline_promotions
                    SET lease_token = %s, lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds'
                    WHERE idempotency_key = %s AND status = 'pending'
                      AND (lease_token IS NULL OR lease_token = %s OR lease_expires_at <= CURRENT_TIMESTAMP)
                    RETURNING idempotency_key""",
                    (promotion_token, key, promotion_token),
                )
                if await claimed.fetchone() is None:
                    raise ExperimentUnavailable("baseline promotion lease was lost")
                row_result = await connection.execute(
                    """SELECT state, resource_json FROM observatory_evaluation_baselines
                    WHERE baseline_id = %s AND baseline_version = %s FOR UPDATE""",
                    (baseline_id, version),
                )
                row = await row_result.fetchone()
                if row is None:
                    raise ExperimentUnavailable("baseline candidate not found")
                resource = row[1] if isinstance(row[1], Mapping) else json.loads(str(row[1]))
                completed: Any = None
                if str(row[0]) == "promoted":
                    if resource.get("manualPromotionRecordId") != authorization_record_id:
                        raise ExperimentConflict("baseline is already immutable")
                else:
                    resource = dict(resource)
                    resource.update(
                        {
                            "state": "promoted",
                            "manualPromotionRecordId": authorization_record_id,
                            "promotedAt": promoted_at,
                        }
                    )
                    updated = await connection.execute(
                        """UPDATE observatory_evaluation_baselines
                        SET state = 'promoted', manual_promotion_record_id = %s,
                            promoted_at = %s, resource_json = %s::jsonb
                        WHERE baseline_id = %s AND baseline_version = %s AND state = 'candidate'
                        RETURNING baseline_id""",
                        (authorization_record_id, promoted_at, json.dumps(resource), baseline_id, version),
                    )
                    if await updated.fetchone() is None:
                        await connection.rollback()
                        raise ExperimentUnavailable("baseline promotion fence was lost")
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_experiment_history(
                        history_id, resource_type, resource_id, resource_version, change, state)
                        VALUES (%s, 'baseline', %s, %s, 'promoted', 'promoted')
                        ON CONFLICT (history_id) DO NOTHING""",
                        (str(uuid5(NAMESPACE_URL, f"aura-0096:baseline:{baseline_id}:{version}:promoted:promoted")), baseline_id, version),
                    )
                completed = await connection.execute(
                    """UPDATE observatory_evaluation_baseline_promotions
                    SET status = 'completed', replay_json = %s::jsonb, completed_at = CURRENT_TIMESTAMP,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE idempotency_key = %s AND lease_token = %s
                      AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING idempotency_key""",
                    (json.dumps(resource), key, promotion_token),
                )
                if await completed.fetchone() is None:
                    await connection.rollback()
                    raise ExperimentUnavailable("baseline promotion completion fence was lost")
                await connection.commit()
                return dict(resource)
        except (ExperimentConflict, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("baseline promotion storage unavailable") from exc

    @_translate_provider_failures
    async def record_history(self, value: Mapping[str, object]) -> None:
        try:
            async with self._connection_factory() as connection:
                await connection.execute(
                    """INSERT INTO observatory_evaluation_experiment_history(
                    history_id, resource_type, resource_id, resource_version, change, state)
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (history_id) DO NOTHING""",
                    (value["historyId"], value["resourceType"], value["resourceId"], value.get("resourceVersion"), value["change"], value["state"]),
                )
                await connection.commit()
        except Exception as exc:
            raise ExperimentUnavailable("experiment history storage unavailable") from exc

    @_translate_provider_failures
    async def delete_before(self, cutoff: str | None) -> int:
        deleted = 0
        async with self._connection_factory() as connection:
            for table in (
                "observatory_evaluation_regressions",
                "observatory_evaluation_baselines",
                "observatory_evaluation_experiments",
            ):
                result = await connection.execute(
                    f"DELETE FROM {table} WHERE (%s IS NULL OR created_at < %s)",
                    (cutoff, cutoff),
                )
                deleted += int(getattr(result, "rowcount", 0) or 0)
            await connection.commit()
        return deleted

    @_translate_provider_failures
    async def regression_projection_ids(
        self, selector: DeletionSelector, *, snapshot_at: datetime | None = None
    ) -> list[str]:
        """Return bounded CH projection identities before retention mutation."""
        kind = selector.kind.value
        value = selector.value
        async with self._connection_factory() as connection:
            if kind == "purge_all":
                predicate, params = "TRUE", ()
            elif kind == "cutoff":
                predicate, params = "created_at < %s", (value,)
            elif kind in {"job", "dataset", "suite"}:
                if self._deletion_reference is None:
                    raise ExperimentUnavailable("evaluation deletion reference is unavailable")
                result_ids = [
                    str(item)
                    for item in await self._deletion_reference.result_ids_for_selector(selector)
                ]
                if not result_ids:
                    return []
                predicate = "(resource_json->'baselineResultIds' ?| %s::text[] OR resource_json->'candidateResultIds' ?| %s::text[])"
                params = (result_ids, result_ids)
            else:
                raise ExperimentError("deletion selector is invalid")
            if snapshot_at is not None:
                predicate += " AND created_at <= %s"
                params = (*params, snapshot_at)
            rows: list[Any] = []
            last_created: Any = None
            last_id: str | None = None
            while True:
                page_predicate = predicate
                page_params = list(params)
                if last_created is not None and last_id is not None:
                    page_predicate += " AND (created_at, regression_id) > (%s, %s)"
                    page_params.extend((last_created, last_id))
                result = await connection.execute(
                    "SELECT regression_id::text, created_at FROM observatory_evaluation_regressions "
                    f"WHERE {page_predicate} ORDER BY created_at, regression_id LIMIT 1000",
                    tuple(page_params),
                )
                page = await result.fetchall()
                if not page:
                    break
                rows.extend(page)
                if len(rows) > 100_000:
                    raise ExperimentError("retention selection exceeds bound")
                last_id = str(page[-1][0])
                last_created = page[-1][1]
                if len(page) < 1000:
                    break
        return [str(row[0]) for row in rows if row and row[0] is not None]

    @_translate_provider_failures
    async def fence_projection_deletion(self, selector: DeletionSelector) -> None:
        """Tombstone projection intent before retention mutates ClickHouse."""
        kind = selector.kind.value
        value = selector.value
        if kind not in {"job", "dataset", "suite", "cutoff", "purge_all"}:
            raise ExperimentError("deletion selector is invalid")
        if kind in {"cutoff", "purge_all"}:
            predicate, params = ("TRUE", ()) if kind == "purge_all" else ("created_at < %s", (value,))
            lock_ids = await self.retention_projection_ids(
                cutoff=value, purge_all=kind == "purge_all"
            )
        else:
            ids = await self.regression_projection_ids(selector)
            if not ids:
                return
            predicate, params = "regression_id = ANY(%s::uuid[])", (ids,)
            lock_ids = ids
        async with self._connection_factory() as connection:
            for regression_id in lock_ids:
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (regression_id,)
                )
            await connection.execute(
                "UPDATE observatory_evaluation_regression_projection_outbox "
                "SET status = 'deleted', cleanup_pending = TRUE, payload_json = '{}'::jsonb, lease_token = NULL, lease_expires_at = NULL WHERE "
                + predicate,
                params,
            )
            for regression_id in lock_ids:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (regression_id,)
                )
            await connection.commit()

    @_translate_provider_failures
    async def fence_projection_ids(self, regression_ids: Sequence[str]) -> None:
        """Fence the exact selected regression identities before CH deletion."""

        normalized = tuple(dict.fromkeys(str(value) for value in regression_ids))
        if not normalized:
            return
        if len(normalized) > 100_000:
            raise ExperimentError("retention selection exceeds bound")
        async with self._connection_factory() as connection:
            for projection_id in normalized:
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    (projection_id,),
                )
            await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET status = 'deleted', cleanup_pending = TRUE, payload_json = '{}'::jsonb,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE regression_id = ANY(%s::uuid[])""",
                (list(normalized),),
            )
            for projection_id in normalized:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (projection_id,),
                )
            await connection.commit()

    @_translate_provider_failures
    async def fence_projection_keys(self, regression_keys: Sequence[tuple[str, str]]) -> None:
        """Fence exact immutable regression versions before CH deletion."""

        keys = tuple(dict.fromkeys((str(identifier), str(version)) for identifier, version in regression_keys))
        if len(keys) > 100_000:
            raise ExperimentError("retention selection exceeds bound")
        if not keys:
            return
        async with self._connection_factory() as connection:
            for identifier, _version in keys:
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (identifier,)
                )
            predicate = " OR ".join(
                "(regression_id = %s AND regression_version = %s)" for _ in keys
            )
            await connection.execute(
                "UPDATE observatory_evaluation_regression_projection_outbox "
                "SET status = 'deleted', cleanup_pending = TRUE, payload_json = '{}'::jsonb, "
                "lease_token = NULL, lease_expires_at = NULL WHERE " + predicate,
                tuple(value for key in keys for value in key),
            )
            for identifier, _version in keys:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (identifier,)
                )
            await connection.commit()

    @_translate_provider_failures
    async def retention_projection_ids(
        self, *, cutoff: datetime | None, purge_all: bool
    ) -> list[str]:
        """Select a bounded, snapshot-stable retention identity page set."""
        selector = DeletionSelector(
            DeletionKind.PURGE_ALL if purge_all else DeletionKind.CUTOFF,
            None if purge_all else cutoff,
        )
        return await self.regression_projection_ids(selector)

    @_translate_provider_failures
    async def delete_for_retention(
        self, *, cutoff: datetime | None, purge_all: bool,
        plan: ExperimentRetentionPlan | None = None,
    ) -> int:
        """Fence and delete operational cutoff/purge projections via this owner port."""
        if plan is not None:
            if plan.cutoff != cutoff or plan.purge_all != purge_all:
                raise ExperimentConflict("retention plan does not match request")
            return await self._delete_for_retention_plan(plan)
        selector = DeletionSelector(
            DeletionKind.PURGE_ALL if purge_all else DeletionKind.CUTOFF,
            None if purge_all else cutoff,
        )
        await self.fence_projection_deletion(selector)
        return await self.delete_for_selector(selector)

    @_translate_provider_failures
    async def prepare_for_retention(
        self, *, cutoff: datetime | None, purge_all: bool
    ) -> ExperimentRetentionPlan:
        """Fence PG projection rows before any cross-store fact deletion."""

        async with self._connection_factory() as connection:
            regression_query, regression_params = (
                ("SELECT regression_id::text, regression_version, baseline_id::text, "
                 "baseline_version, experiment_id::text, experiment_version "
                 ", attribution_json->>'attributionId' "
                 "FROM observatory_evaluation_regressions LIMIT 100001", ())
                if purge_all
                else ("SELECT regression_id::text, regression_version, baseline_id::text, "
                      "baseline_version, experiment_id::text, experiment_version "
                      ", attribution_json->>'attributionId' "
                      "FROM observatory_evaluation_regressions WHERE created_at < %s LIMIT 100001", (cutoff,))
            )
            regression_result = await connection.execute(regression_query, regression_params)
            regression_records = list(await regression_result.fetchall())
            if len(regression_records) > 100_000:
                raise ExperimentError("retention selection exceeds bound")
            if purge_all:
                baseline_query, baseline_params = (
                    "SELECT baseline_id::text, baseline_version, experiment_id::text, experiment_version "
                    "FROM observatory_evaluation_baselines LIMIT 100001",
                    (),
                )
                experiment_query, experiment_params = (
                    "SELECT experiment_id::text, experiment_version FROM observatory_evaluation_experiments LIMIT 100001",
                    (),
                )
            else:
                baseline_query, baseline_params = (
                    "SELECT baseline_id::text, baseline_version, experiment_id::text, experiment_version "
                    "FROM observatory_evaluation_baselines WHERE created_at < %s LIMIT 100001",
                    (cutoff,),
                )
                experiment_query, experiment_params = (
                    "SELECT experiment_id::text, experiment_version FROM observatory_evaluation_experiments "
                    "WHERE created_at < %s LIMIT 100001",
                    (cutoff,),
                )
            baseline_result = await connection.execute(baseline_query, baseline_params)
            baseline_rows = list(await baseline_result.fetchall())
            experiment_result = await connection.execute(experiment_query, experiment_params)
            experiment_rows = list(await experiment_result.fetchall())
            if len(baseline_rows) > 100_000 or len(experiment_rows) > 100_000:
                raise ExperimentError("retention selection exceeds bound")
            baseline_keys = {(str(row[0]), str(row[1])) for row in baseline_rows}
            experiment_keys = {(str(row[2]), str(row[3])) for row in baseline_rows}
            regression_keys = {(str(row[0]), str(row[1])) for row in regression_records}
            attribution_keys = {
                (str(row[6]), "1.0.0")
                for row in regression_records
                if len(row) > 6 and row[6]
            }
            baseline_keys.update((str(row[2]), str(row[3])) for row in regression_records)
            experiment_keys.update((str(row[4]), str(row[5])) for row in regression_records)
            experiment_keys.update((str(row[0]), str(row[1])) for row in experiment_rows)
        await self.fence_projection_keys(regression_keys)
        return ExperimentRetentionPlan(
            cutoff=cutoff,
            purge_all=purge_all,
            regression_keys=tuple(sorted(regression_keys)),
            baseline_keys=tuple(sorted(baseline_keys)),
            experiment_keys=tuple(sorted(experiment_keys)),
            attribution_keys=tuple(sorted(attribution_keys)),
        )

    async def _delete_for_retention_plan(self, plan: ExperimentRetentionPlan) -> int:
        """Finalize exactly the identities fenced by one retention plan."""

        regression_keys = list(plan.regression_keys)
        baseline_keys = list(plan.baseline_keys)
        experiment_keys = list(plan.experiment_keys)
        regression_ids = [identifier for identifier, _version in regression_keys]
        baseline_ids = [identifier for identifier, _version in baseline_keys]
        experiment_ids = [identifier for identifier, _version in experiment_keys]
        def pair_predicate(id_column: str, version_column: str, keys: Sequence[tuple[str, str]]) -> tuple[str, tuple[str, ...]]:
            return " OR ".join(f"({id_column} = %s AND {version_column} = %s)" for _ in keys), tuple(value for key in keys for value in key)

        regression_predicate, regression_params = pair_predicate("regression_id", "regression_version", regression_keys)
        baseline_predicate, baseline_params = pair_predicate("baseline_id", "baseline_version", baseline_keys)
        experiment_predicate, experiment_params = pair_predicate("experiment_id", "experiment_version", experiment_keys)
        history_terms: list[str] = []
        history_params: list[str] = []
        live_terms: list[str] = []
        live_params: list[str] = []
        command_terms: list[str] = []
        command_params: list[str] = []
        pending_terms: list[str] = []
        pending_params: list[str] = []
        for resource_type, live_type, keys, operation, response_id, response_version in (
            ("experiment", "evaluation_experiment", experiment_keys, "create_experiment", "experimentId", "experimentVersion"),
            ("baseline", "evaluation_baseline", baseline_keys, "create_baseline", "baselineId", "baselineVersion"),
            ("baseline", "evaluation_baseline", baseline_keys, "promote_baseline", "baselineId", "baselineVersion"),
            ("regression", "evaluation_regression", regression_keys, "create_regression", "regressionId", "regressionVersion"),
        ):
            for identifier, version in keys:
                history_terms.append("(resource_type = %s AND resource_id = %s AND resource_version = %s)")
                history_params.extend((resource_type, identifier, version))
                live_terms.append("(resource_type = %s AND resource_id = %s AND resource_version = %s)")
                live_params.extend((live_type, identifier, version))
                command_terms.append(
                    "(operation = %s AND resource_id = %s AND resource_version = %s)"
                )
                command_params.extend((operation, identifier, version))
                pending_terms.append("(operation = %s AND resource_id = %s AND resource_version = %s)")
                pending_params.extend((operation, identifier, version))
                command_terms.append(
                    f"(replay_json->>'{response_id}' = %s AND replay_json->>'{response_version}' = %s)"
                )
                command_params.extend((identifier, version))
        for identifier, version in plan.attribution_keys:
            history_terms.append("(resource_type = %s AND resource_id = %s AND resource_version = %s)")
            history_params.extend(("attribution", identifier, version))
            live_terms.append("(resource_type = %s AND resource_id = %s AND resource_version = %s)")
            live_params.extend(("evaluation_attribution", identifier, version))
        async with self._connection_factory() as connection:
            if regression_ids:
                await connection.execute(
                    "DELETE FROM observatory_evaluation_regression_projection_outbox "
                    f"WHERE {regression_predicate}", regression_params,
                )
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_regressions "
                    f"WHERE {regression_predicate}", regression_params,
                )
                deleted = int(getattr(result, "rowcount", 0) or 0)
            else:
                deleted = 0
            if history_terms:
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_history "
                    "WHERE " + " OR ".join(history_terms), tuple(history_params),
                )
                await connection.execute(
                    "DELETE FROM observatory_evaluation_live_events "
                    "WHERE " + " OR ".join(live_terms), tuple(live_params),
                )
            if baseline_ids:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_baselines "
                    f"WHERE {baseline_predicate}", baseline_params,
                )
                deleted += int(getattr(result, "rowcount", 0) or 0)
                await connection.execute(
                    "UPDATE observatory_evaluation_baseline_promotions "
                    f"SET replay_json = NULL WHERE {baseline_predicate}", baseline_params,
                )
            if experiment_ids:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_experiments "
                    f"WHERE {experiment_predicate}", experiment_params,
                )
                deleted += int(getattr(result, "rowcount", 0) or 0)
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_dataset_bindings "
                    f"WHERE {experiment_predicate}", experiment_params,
                )
            if plan.purge_all:
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_commands"
                )
                await connection.execute(
                    "UPDATE observatory_evaluation_baseline_promotions SET replay_json = NULL"
                )
            elif command_terms:
                await connection.execute(
                    "UPDATE observatory_evaluation_experiment_commands "
                    "SET status = 'deleted', replay_json = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, completed_at = CURRENT_TIMESTAMP "
                    "WHERE status = 'pending' AND replay_json IS NULL "
                    "AND (" + " OR ".join(pending_terms) + ")",
                    tuple(pending_params),
                )
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_commands "
                    "WHERE " + " OR ".join(command_terms), tuple(command_params),
                )
            await connection.commit()
        return deleted

    @_translate_provider_failures
    async def delete_for_selector(self, selector: DeletionSelector) -> int:
        """Delete experiment projections using the evaluation deletion selector.

        Result/job/dataset/suite selectors are resolved through the
        authoritative evaluation receipt/job boundary first.  No selector is
        widened to a timestamp, and command replay payloads are removed with
        the related projection rows.
        """
        kind = selector.kind.value
        value = selector.value
        if kind not in {"job", "dataset", "suite", "cutoff", "purge_all"}:
            raise ExperimentError("deletion selector is invalid")
        async with self._connection_factory() as connection:
            result_ids: list[str] = []
            if kind in {"job", "dataset", "suite"}:
                if self._deletion_reference is None:
                    raise ExperimentUnavailable("evaluation deletion reference is unavailable")
                result_ids = [str(item) for item in await self._deletion_reference.result_ids_for_selector(selector)]
                if len(result_ids) > 100_000:
                    raise ExperimentError("retention result selection exceeds bound")
            dataset_experiment_ids: set[str] = set()
            if kind == "dataset":
                bindings = await connection.execute(
                    "SELECT experiment_id::text FROM observatory_evaluation_experiment_dataset_bindings "
                    "WHERE dataset_id = %s ORDER BY experiment_id, experiment_version LIMIT 100001",
                    (str(value),),
                )
                binding_rows = await bindings.fetchall()
                if len(binding_rows) > 100_000:
                    raise ExperimentError("retention experiment selection exceeds bound")
                dataset_experiment_ids = {str(row[0]) for row in binding_rows}
            if kind == "purge_all":
                regression_predicate, regression_params = "TRUE", ()
                baseline_predicate, baseline_params = "TRUE", ()
            elif kind == "cutoff":
                regression_predicate, regression_params = "created_at < %s", (value,)
                baseline_predicate, baseline_params = "created_at < %s", (value,)
            elif result_ids:
                regression_predicate = "(resource_json->'baselineResultIds' ?| %s::text[] OR resource_json->'candidateResultIds' ?| %s::text[])"
                regression_params = (result_ids, result_ids)
                baseline_predicate = "resource_json->'sourceResultIds' ?| %s::text[]"
                baseline_params = (result_ids,)
            elif kind == "suite":
                # A suite deletion must also remove experiments that have no
                # result receipts yet. The suite binding is immutable and
                # content-free, so this remains a closed predicate.
                regression_predicate, regression_params = "FALSE", ()
                baseline_predicate, baseline_params = "FALSE", ()
            elif kind == "dataset" and dataset_experiment_ids:
                regression_predicate, regression_params = "FALSE", ()
                baseline_predicate, baseline_params = "FALSE", ()
            else:
                return 0
            regression_rows = await connection.execute(
                "SELECT regression_id::text, baseline_id::text, experiment_id::text "
                f"FROM observatory_evaluation_regressions WHERE {regression_predicate} "
                "ORDER BY created_at, regression_id LIMIT 100001",
                regression_params,
            )
            regression_records = await regression_rows.fetchall()
            if len(regression_records) > 100_000:
                raise ExperimentError("retention regression selection exceeds bound")
            regression_ids = [str(row[0]) for row in regression_records]
            baseline_ids = {str(row[1]) for row in regression_records}
            experiment_ids = {str(row[2]) for row in regression_records}
            if kind == "suite":
                experiment_rows = await connection.execute(
                    "SELECT experiment_id::text FROM observatory_evaluation_experiments "
                    "WHERE resource_json->'suite'->>'resourceId' = %s "
                    "ORDER BY created_at, experiment_id LIMIT 100001",
                    (str(value),),
                )
                experiment_records = await experiment_rows.fetchall()
                if len(experiment_records) > 100_000:
                    raise ExperimentError("retention experiment selection exceeds bound")
                experiment_ids.update(str(row[0]) for row in experiment_records)
            elif kind == "cutoff":
                experiment_rows = await connection.execute(
                    "SELECT experiment_id::text FROM observatory_evaluation_experiments "
                    "WHERE created_at < %s ORDER BY created_at, experiment_id LIMIT 100001",
                    (value,),
                )
                experiment_records = await experiment_rows.fetchall()
                if len(experiment_records) > 100_000:
                    raise ExperimentError("retention experiment selection exceeds bound")
                experiment_ids.update(str(row[0]) for row in experiment_records)
            experiment_ids.update(dataset_experiment_ids)
            baseline_rows = await connection.execute(
                "SELECT baseline_id::text, experiment_id::text FROM observatory_evaluation_baselines "
                f"WHERE {baseline_predicate} ORDER BY created_at, baseline_id LIMIT 100001",
                baseline_params,
            )
            baseline_records = await baseline_rows.fetchall()
            if len(baseline_records) > 100_000:
                raise ExperimentError("retention baseline selection exceeds bound")
            baseline_ids.update(str(row[0]) for row in baseline_records)
            experiment_ids.update(str(row[1]) for row in baseline_records)
            selected_resource_ids = list(experiment_ids | baseline_ids | set(regression_ids))
            if kind in {"purge_all", "cutoff"}:
                await connection.execute(
                    "UPDATE observatory_evaluation_regression_projection_outbox "
                    "SET status = 'deleted', cleanup_pending = TRUE, payload_json = '{}'::jsonb, lease_token = NULL, lease_expires_at = NULL "
                    + ("WHERE TRUE" if kind == "purge_all" else "WHERE created_at < %s"),
                    () if kind == "purge_all" else (value,),
                )
            if regression_ids:
                await connection.execute(
                    "UPDATE observatory_evaluation_regression_projection_outbox "
                    "SET status = 'deleted', cleanup_pending = TRUE, payload_json = '{}'::jsonb, lease_token = NULL, lease_expires_at = NULL "
                    "WHERE regression_id = ANY(%s::uuid[])",
                    (regression_ids,),
                )
            deleted = 0
            result = await connection.execute(
                f"DELETE FROM observatory_evaluation_regressions WHERE {regression_predicate}",
                regression_params,
            )
            deleted += int(getattr(result, "rowcount", 0) or 0)
            if kind in {"purge_all", "cutoff"}:
                history_predicate = (
                    "TRUE"
                    if kind == "purge_all"
                    else "occurred_at < %s OR resource_id = ANY(%s)"
                )
                history_params: tuple[object, ...] = (
                    ()
                    if kind == "purge_all"
                    else (value, selected_resource_ids)
                )
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_history WHERE "
                    + history_predicate,
                    history_params,
                )
                await connection.execute(
                    "DELETE FROM observatory_evaluation_live_events WHERE "
                    + history_predicate,
                    history_params,
                )
            elif experiment_ids or baseline_ids or regression_ids:
                await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_history WHERE resource_id = ANY(%s)",
                    (list(experiment_ids | baseline_ids | set(regression_ids)),),
                )
                await connection.execute(
                    "DELETE FROM observatory_evaluation_live_events WHERE resource_id = ANY(%s)",
                    (list(experiment_ids | baseline_ids | set(regression_ids)),),
                )
            if kind in {"purge_all", "cutoff"}:
                result = await connection.execute(
                    f"DELETE FROM observatory_evaluation_baselines WHERE {baseline_predicate}",
                    baseline_params,
                )
            elif baseline_ids:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_baselines WHERE baseline_id = ANY(%s::uuid[])",
                    (list(baseline_ids),),
                )
            else:
                result = None
            deleted += int(getattr(result, "rowcount", 0) or 0)
            if kind in {"purge_all", "cutoff"}:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_experiments WHERE "
                    + ("TRUE" if kind == "purge_all" else "created_at < %s"),
                    () if kind == "purge_all" else (value,),
                )
            elif experiment_ids:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_experiments WHERE experiment_id = ANY(%s::uuid[])",
                    (list(experiment_ids),),
                )
            else:
                result = None
            deleted += int(getattr(result, "rowcount", 0) or 0)
            await connection.execute(
                "DELETE FROM observatory_evaluation_experiment_dataset_bindings b "
                "WHERE NOT EXISTS (SELECT 1 FROM observatory_evaluation_experiments e "
                "WHERE e.experiment_id = b.experiment_id AND e.experiment_version = b.experiment_version)"
            )
            if kind in {"cutoff", "purge_all"}:
                command_result = await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_commands WHERE "
                    + (
                        "TRUE"
                        if kind == "purge_all"
                        else "created_at < %s OR resource_id = ANY(%s::uuid[])"
                    ),
                    ()
                    if kind == "purge_all"
                    else (value, selected_resource_ids),
                )
                deleted += int(getattr(command_result, "rowcount", 0) or 0)
                await connection.execute(
                    "UPDATE observatory_evaluation_baseline_promotions "
                    "SET replay_json = NULL WHERE "
                    + (
                        "TRUE"
                        if kind == "purge_all"
                        else "created_at < %s OR baseline_id = ANY(%s::uuid[])"
                    ),
                    ()
                    if kind == "purge_all"
                    else (value, list(baseline_ids)),
                )
            elif regression_ids or baseline_ids or experiment_ids:
                selected_resource_ids = list(experiment_ids | baseline_ids | set(regression_ids))
                # Pending commands have no replay payload until their
                # immutable resource transaction commits. Fence only claims
                # linked to the selected immutable resources before deleting
                # their projections; unrelated owner commands continue.
                await connection.execute(
                    "UPDATE observatory_evaluation_experiment_commands "
                    "SET status = 'deleted', replay_json = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, completed_at = CURRENT_TIMESTAMP "
                    "WHERE status = 'pending' AND replay_json IS NULL "
                    "AND resource_id = ANY(%s::uuid[])",
                    (selected_resource_ids,),
                )
                command_result = await connection.execute(
                    "DELETE FROM observatory_evaluation_experiment_commands WHERE "
                    "replay_json->>'regressionId' = ANY(%s) OR "
                    "replay_json->>'baselineId' = ANY(%s) OR "
                    "replay_json->>'experimentId' = ANY(%s)",
                    (list(regression_ids), list(baseline_ids), list(experiment_ids)),
                )
                deleted += int(getattr(command_result, "rowcount", 0) or 0)
                if baseline_ids:
                    await connection.execute(
                        "UPDATE observatory_evaluation_baseline_promotions SET replay_json = NULL "
                        "WHERE baseline_id = ANY(%s::uuid[])",
                        (list(baseline_ids),),
                    )
            await connection.commit()
        return deleted


class PostgresExperimentReferencePort:
    """Validate exact Observatory-owned definition and result references."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        definition_repository: EvaluationDefinitionRepository | None = None,
        catalog: ObservatoryCatalog | None = None,
        query_port: ObservatoryQueryPort | None = None,
        metrics: Any | None = None,
        result_reference: EvaluationResultReferencePort | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._definition_repository = definition_repository
        self._catalog = catalog
        self._query_port = query_port
        self._metrics = metrics
        self._result_reference = result_reference

    def _span(self, started: float, outcome: str = "success", error: str | None = None) -> None:
        recorder = getattr(self._metrics, "record_component_span", None)
        if not callable(recorder):
            return
        try:
            recorder(
                component_id="aura.observatory.evaluation_experiments",
                component_version="1.0.0",
                deployment_id=str(getattr(self._metrics, "deployment_id", "observatory")),
                operation="definition_binding_validation",
                duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                outcome=outcome,
                dependency="postgresql",
                error_code=error,
            )
        except Exception:
            return

    async def _definition(self, resource_id: str, version: str) -> Mapping[str, object] | None:
        if self._definition_repository is None:
            raise ExperimentUnavailable("evaluation definition provider is unavailable")
        value = await self._definition_repository.get_suite(resource_id, version)
        if value is None:
            return None
        payload = value.to_payload() if hasattr(value, "to_payload") else value
        return payload if isinstance(payload, Mapping) else None

    async def _dataset_definition(
        self, resource_id: str, version: str
    ) -> Mapping[str, object] | None:
        if self._definition_repository is None:
            raise ExperimentUnavailable("evaluation definition provider is unavailable")
        value = await self._definition_repository.get_dataset(resource_id, version)
        if value is None:
            return None
        payload = value.to_payload() if hasattr(value, "to_payload") else value
        return payload if isinstance(payload, Mapping) else None

    async def _results(self, result_ids: tuple[str, ...]) -> list[Mapping[str, object]]:
        if self._result_reference is None:
            raise ExperimentUnavailable("evaluation result provider is unavailable")
        rows = await self._result_reference.get_result_envelopes(result_ids)
        return [
            self._decode_result_envelope(envelope, result_id)
            for result_id, envelope in rows
        ]

    @staticmethod
    def _decode_result_envelope(
        envelope: Mapping[str, object], result_id: str
    ) -> Mapping[str, object]:
        """Decode only the persisted ``data.provenance`` metadata boundary.

        Result receipts are opaque to this ownership zone.  The projection may
        inspect the closed provenance envelope and result/case identities, but
        never copies request, response, prompt, artifact, or tool content.
        """

        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise ExperimentError("result envelope data is unavailable")
        if data.get("resultId") != result_id:
            raise ExperimentError("result identity is incompatible")
        receipt_metadata = envelope.get("__receiptMetadata")
        if not isinstance(receipt_metadata, Mapping):
            raise ExperimentError("result receipt metadata is unavailable")
        if receipt_metadata.get("status") != "completed" or receipt_metadata.get("disposition") != "applied":
            raise ExperimentError("result receipt is not an applied completed result")
        request_data = receipt_metadata.get("requestData")
        if not isinstance(request_data, Mapping):
            raise ExperimentError("admitted evaluation request metadata is unavailable")
        admission_provenance = request_data.get("provenance")
        provenance = data.get("provenance") or admission_provenance
        if not isinstance(provenance, Mapping):
            raise ExperimentError("result provenance is unavailable")
        required = {"dataset", "suite", "evaluators", "components", "model", "deploymentRevision", "reproducibility"}
        if set(provenance) != required:
            raise ExperimentError("result provenance is incompatible")
        for ref_name in ("dataset", "suite"):
            reference = provenance.get(ref_name)
            if not isinstance(reference, Mapping) or set(reference) not in ({"id", "version"}, {"resourceId", "resourceVersion"}):
                raise ExperimentError("result provenance reference is incompatible")
        if not isinstance(provenance.get("evaluators"), list) or not provenance["evaluators"]:
            raise ExperimentError("result evaluator provenance is unavailable")
        if not isinstance(provenance.get("components"), list) or not provenance["components"]:
            raise ExperimentError("result component provenance is unavailable")
        if not isinstance(provenance.get("model"), Mapping):
            raise ExperimentError("result model provenance is unavailable")
        if not isinstance(provenance.get("deploymentRevision"), str) or not provenance["deploymentRevision"]:
            raise ExperimentError("result deployment provenance is unavailable")
        reproducibility = provenance.get("reproducibility")
        if not isinstance(reproducibility, Mapping) or set(reproducibility) != {"seed", "clockMode", "timeZone", "locale", "fixtureRevision", "promptRevision", "policyRevision"}:
            raise ExperimentError("result reproducibility is incompatible")
        # Admission metadata is authoritative; a runner cannot broaden its
        # own capture authorization in the result envelope.
        capture_auth = request_data.get("captureAuthorization")
        if not isinstance(capture_auth, Mapping) or set(capture_auth) != {
            "captureClassification", "authorizationPurpose", "authorizationContext", "retentionMode"
        }:
            raise ExperimentError("result capture authorization is unavailable")
        capture = capture_auth.get("captureClassification")
        if (
            capture != "metadata_only"
            or capture != receipt_metadata.get("captureClassification")
            or capture_auth.get("authorizationPurpose") not in _AUTHORIZATION_PURPOSES
            or capture_auth.get("authorizationContext") != "controlled_evaluation"
            or capture_auth.get("retentionMode") != "manual_only"
        ):
            raise ExperimentError("result capture classification is incompatible")
        case_ids = request_data.get("caseIds")
        if not isinstance(case_ids, list) or not case_ids or any(not isinstance(item, str) for item in case_ids):
            raise ExperimentError("admitted evaluation case identities are unavailable")
        if admission_provenance != provenance:
            raise ExperimentError("result provenance does not match admitted execution")
        if provenance.get("suite") != receipt_metadata.get("suite"):
            raise ExperimentError("result suite binding is incompatible")
        expected_evaluator = receipt_metadata.get("evaluator")
        evaluators = provenance.get("evaluators")
        if not isinstance(expected_evaluator, Mapping) or not isinstance(evaluators, list) or not any(
            isinstance(item, Mapping)
            and item.get("evaluatorId", item.get("id")) == expected_evaluator.get("id")
            and item.get("evaluatorVersion", item.get("version")) == expected_evaluator.get("version")
            for item in evaluators
        ):
            raise ExperimentError("result evaluator binding is incompatible")
        result = dict(envelope)
        result["__receiptResultId"] = result_id
        result["__resultData"] = dict(data)
        result["__resultData"] = {
            **dict(data),
            "captureAuthorization": dict(capture_auth),
            "caseIds": list(case_ids),
            "provenance": dict(provenance),
        }
        result["__jobMetadata"] = dict(receipt_metadata)
        return result

    async def validate_experiment(self, experiment: Mapping[str, object]) -> None:
        started = monotonic()
        suite = experiment.get("suite")
        if not isinstance(suite, Mapping):
            raise ExperimentError("suite reference is invalid")
        definition = await self._definition(str(suite["resourceId"]), str(suite["resourceVersion"]))
        if definition is None:
            raise ExperimentError("suite version is not persisted")
        dataset = definition.get("dataset")
        if not isinstance(dataset, Mapping):
            raise ExperimentError("suite dataset binding is missing")
        dataset_definition = await self._dataset_definition(
            str(dataset.get("resourceId", dataset.get("id"))),
            str(dataset.get("resourceVersion", dataset.get("version"))),
        )
        if dataset_definition is None:
            raise ExperimentError("suite dataset version is not persisted")
        dataset_case_ids = dataset_definition.get("caseIds")
        if not isinstance(dataset_case_ids, list):
            raise ExperimentError("suite dataset cases are unavailable")
        dataset_cases = {str(value) for value in dataset_case_ids}
        authoritative_limits = definition.get("limits")
        requested_limits = experiment.get("limits")
        if not isinstance(authoritative_limits, Mapping) or not isinstance(requested_limits, Mapping):
            raise ExperimentError("suite execution limits are unavailable")
        if set(requested_limits) != set(authoritative_limits) or any(
            type(requested_limits[key]) is not int
            or type(authoritative_limits[key]) is not int
            or int(requested_limits[key]) > int(authoritative_limits[key])
            for key in authoritative_limits
        ):
            raise ExperimentError("experiment limits broaden suite limits")
        suite_components = {
            (
                str(item.get("componentId")),
                str(item.get("componentVersion")),
                str(item.get("codeRevision")),
                str(item.get("imageDigest")),
                str(item.get("configurationRevision")),
            )
            for item in definition.get("components", [])
            if isinstance(item, Mapping)
        }
        variants = experiment.get("variants")
        if not isinstance(variants, list):
            raise ExperimentError("experiment variants are invalid")
        suite_evaluators = {
            (str(item.get("evaluatorId", item.get("id"))), str(item.get("evaluatorVersion", item.get("version"))))
            for item in definition.get("evaluators", [])
            if isinstance(item, Mapping)
        }
        for variant in variants:
            if not isinstance(variant, Mapping):
                raise ExperimentError("experiment variant is invalid")
            bindings = variant.get("components")
            actual = {
                (
                    str(item.get("componentId")),
                    str(item.get("componentVersion")),
                    str(item.get("codeRevision")),
                    str(item.get("imageDigest")),
                    str(item.get("configurationRevision")),
                )
                for item in bindings
                if isinstance(item, Mapping)
            } if isinstance(bindings, list) else set()
            if suite_components and actual != suite_components:
                raise ExperimentError("variant component bindings do not match suite")
            authoritative_variants = definition.get("variantBindings", definition.get("variants"))
            if not isinstance(authoritative_variants, list):
                # A caller may not mint model, prompt, policy, or deployment
                # provenance when the authoritative suite has not admitted a
                # variant binding.  Result-side checks remain independently
                # strict, but creation must not persist unverified claims.
                raise ExperimentError("authoritative variant provenance is unavailable")
            expected_variant = next(
                (value for value in authoritative_variants
                 if isinstance(value, Mapping) and value.get("variantId") == variant.get("variantId")),
                None,
            )
            if not isinstance(expected_variant, Mapping):
                raise ExperimentError("variant provenance is not admitted by suite")
            expected_components = expected_variant.get("components")
            authoritative_component_bindings = {
                (
                    str(component.get("componentId")),
                    str(component.get("componentVersion")),
                    str(component.get("codeRevision")),
                    str(component.get("imageDigest")),
                    str(component.get("configurationRevision")),
                )
                for component in expected_components
                if isinstance(component, Mapping)
            } if isinstance(expected_components, list) else set()
            if not authoritative_component_bindings or actual != authoritative_component_bindings:
                raise ExperimentError("variant component provenance does not match suite")
            for key in ("model", "configurationRevision", "promptRevision", "policyRevision", "deploymentRevision"):
                if variant.get(key) != expected_variant.get(key):
                    raise ExperimentError("variant provenance does not match suite")
        deployment_revisions = {
            str(variant.get("deploymentRevision"))
            for variant in variants
            if isinstance(variant, Mapping)
        }
        if deployment_revisions:
            if self._query_port is None:
                raise ExperimentUnavailable("deployment catalogue provider is unavailable")
            deployment_rows = await self._query_port.deployments_query()
            known_deployments = {
                str(item.get("deploymentId"))
                for item in deployment_rows
                if isinstance(item, Mapping)
            }
            if not deployment_revisions <= known_deployments:
                raise ExperimentError("variant deployment is not catalogued")
        for cohort in experiment.get("cohorts", []):
            if not isinstance(cohort, Mapping) or not isinstance(cohort.get("caseIds"), list):
                raise ExperimentError("experiment cohort is invalid")
            if not set(str(value) for value in cohort["caseIds"]) <= dataset_cases:
                raise ExperimentError("experiment cohort case is outside suite dataset")
        for threshold in experiment.get("thresholds", []):
            if not isinstance(threshold, Mapping):
                continue
            evaluator = threshold.get("evaluator")
            if isinstance(evaluator, Mapping) and suite_evaluators and (
                str(evaluator.get("evaluatorId")), str(evaluator.get("evaluatorVersion"))
            ) not in suite_evaluators:
                raise ExperimentError("experiment evaluator is outside suite")
        self._span(started)

    async def validate_baseline(
        self, baseline: Mapping[str, object], experiment: Mapping[str, object]
    ) -> None:
        started = monotonic()
        await self.validate_experiment(experiment)
        suite = baseline.get("suite")
        suite_definition: Mapping[str, object] | None = None
        if suite != experiment.get("suite"):
            raise ExperimentError("baseline suite binding is incompatible")
        if baseline.get("captureClassification") != experiment.get("captureClassification"):
            raise ExperimentError("baseline capture classification is incompatible")
        if isinstance(suite, Mapping):
            suite_definition = await self._definition(
                str(suite.get("resourceId")), str(suite.get("resourceVersion"))
            )
            evaluator_refs = {
                (
                    str(item.get("evaluatorId", item.get("id"))),
                    str(item.get("evaluatorVersion", item.get("version"))),
                )
                for item in (suite_definition or {}).get("evaluators", [])
                if isinstance(item, Mapping)
            }
            for threshold in baseline.get("thresholds", []):
                evaluator = threshold.get("evaluator") if isinstance(threshold, Mapping) else None
                if isinstance(evaluator, Mapping) and evaluator_refs and (
                    str(evaluator.get("evaluatorId")), str(evaluator.get("evaluatorVersion"))
                ) not in evaluator_refs:
                    raise ExperimentError("baseline evaluator is outside suite")
        result_ids = _uuid_list(baseline.get("sourceResultIds"))
        results = await self._results(result_ids)
        if len(results) != len(result_ids):
            raise ExperimentError("baseline source results are incomplete")
        baseline_variant = next(
            (item for item in experiment.get("variants", [])
             if isinstance(item, Mapping) and item.get("variantId") == baseline.get("variantId")),
            None,
        )
        self._validate_result_scope(
            results, experiment, baseline.get("cohortIds"), expected_variant=baseline_variant,
            expected_evaluators=baseline.get("thresholds"),
            expected_reproducibility=baseline.get("reproducibility"),
            expected_capture=experiment.get("captureAuthorization"),
            expected_dataset=(suite_definition or {}).get("dataset") if isinstance(suite_definition, Mapping) else None,
            expected_time_range=(str(baseline.get("sourceResultFrom")), str(baseline.get("sourceResultTo"))),
        )
        self._span(started)

    async def validate_regression(
        self,
        regression: Mapping[str, object],
        experiment: Mapping[str, object],
        baseline: Mapping[str, object],
    ) -> None:
        started = monotonic()
        if baseline.get("state") != "promoted":
            raise ExperimentError("regression baseline is not promoted")
        baseline_ids = _uuid_list(baseline.get("sourceResultIds"))
        requested_ids = _uuid_list(regression.get("baselineResultIds"))
        if set(baseline_ids) != set(requested_ids):
            raise ExperimentError("regression baseline result set is incompatible")
        await self.validate_experiment(experiment)
        result_ids = _uuid_list(regression.get("candidateResultIds"))
        results = await self._results(result_ids)
        if len(results) != len(result_ids):
            raise ExperimentError("candidate source results are incomplete")
        selected_variant = next(
            (item for item in experiment.get("variants", [])
             if isinstance(item, Mapping) and item.get("variantId") == regression.get("variantId")),
            None,
        )
        suite_definition = await self._definition(
            str(experiment["suite"]["resourceId"]), str(experiment["suite"]["resourceVersion"])
        ) if isinstance(experiment.get("suite"), Mapping) else None
        self._validate_result_scope(
            results, experiment, regression.get("cohortIds"), expected_variant=selected_variant,
            expected_evaluators=regression.get("thresholds"),
            expected_reproducibility=baseline.get("reproducibility"),
            expected_capture=experiment.get("captureAuthorization"),
            expected_dataset=(suite_definition or {}).get("dataset") if isinstance(suite_definition, Mapping) else None,
        )
        self._span(started)

    @staticmethod
    def _validate_result_scope(
        results: list[Mapping[str, object]],
        experiment: Mapping[str, object],
        cohort_ids: object,
        *,
        expected_variant: Mapping[str, object] | None = None,
        expected_evaluators: object = None,
        expected_reproducibility: Mapping[str, object] | None = None,
        expected_capture: Mapping[str, object] | None = None,
        expected_dataset: Mapping[str, object] | None = None,
        expected_time_range: tuple[str, str] | None = None,
    ) -> None:
        suite = experiment.get("suite")
        if not isinstance(suite, Mapping) or not isinstance(cohort_ids, list):
            raise ExperimentError("result scope is invalid")
        selected_cases = {
            str(case_id)
            for cohort in experiment.get("cohorts", [])
            if isinstance(cohort, Mapping) and cohort.get("cohortId") in cohort_ids
            for case_id in cohort.get("caseIds", [])
        }
        expected_components = {
            (
                str(item.get("componentId")),
                str(item.get("componentVersion")),
                str(item.get("codeRevision")),
                str(item.get("imageDigest")),
                str(item.get("configurationRevision")),
            )
            for item in (expected_variant or {}).get("components", [])
            if isinstance(item, Mapping)
        }
        expected_model = (expected_variant or {}).get("model")
        expected_model_key = (
            str(expected_model.get("modelId")), str(expected_model.get("modelVersion")),
            str(expected_model.get("modelDigest")),
        ) if isinstance(expected_model, Mapping) else None
        expected_evaluator_keys = {
            (str(item.get("evaluator", {}).get("evaluatorId")), str(item.get("evaluator", {}).get("evaluatorVersion")))
            for item in expected_evaluators or []
            if isinstance(item, Mapping) and isinstance(item.get("evaluator"), Mapping)
        }
        observed_cases: set[str] = set()
        observed_receipt_times: list[datetime] = []
        expected_deployment = str((expected_variant or {}).get("deploymentRevision", ""))
        expected_configuration = str((expected_variant or {}).get("configurationRevision", ""))
        expected_prompt = str((expected_variant or {}).get("promptRevision", ""))
        expected_policy = str((expected_variant or {}).get("policyRevision", ""))
        for result in results:
            result_suite = result.get("suite")
            if not isinstance(result_suite, Mapping):
                data = result.get("__resultData")
                provenance = data.get("provenance") if isinstance(data, Mapping) else None
                result_suite = provenance.get("suite") if isinstance(provenance, Mapping) else None
            if not isinstance(result_suite, Mapping):
                raise ExperimentError("result suite version is unavailable")
            if result_suite.get("id", result_suite.get("resourceId")) != suite.get("resourceId") or result_suite.get("version", result_suite.get("resourceVersion")) != suite.get("resourceVersion"):
                raise ExperimentError("result suite version is incompatible")
            data = result.get("__resultData")
            if not isinstance(data, Mapping):
                raise ExperimentError("result case is outside declared cohorts")
            result_case_ids = data.get("caseIds")
            if not isinstance(result_case_ids, list) or not result_case_ids:
                raise ExperimentError("result case identities are unavailable")
            result_cases = {str(case_id) for case_id in result_case_ids}
            if not result_cases <= selected_cases:
                raise ExperimentError("result case is outside declared cohorts")
            observed_cases.update(result_cases)
            provenance = data.get("provenance")
            if not isinstance(provenance, Mapping):
                raise ExperimentError("result provenance is unavailable")
            actual_capture = data.get("captureAuthorization")
            if expected_capture is not None and actual_capture != expected_capture:
                raise ExperimentError("result capture authorization is incompatible")
            if expected_dataset is not None and provenance.get("dataset") != expected_dataset:
                raise ExperimentError("result dataset binding is incompatible")
            actual_components = {
                (
                    str(item.get("componentId")),
                    str(item.get("componentVersion")),
                    str(item.get("codeRevision")),
                    str(item.get("imageDigest")),
                    str(item.get("configurationRevision")),
                )
                for item in provenance.get("components", [])
                if isinstance(item, Mapping)
            }
            if expected_components and actual_components != expected_components:
                raise ExperimentError("result component provenance is incompatible")
            model = provenance.get("model")
            if expected_model_key and (not isinstance(model, Mapping) or (str(model.get("modelId")), str(model.get("modelVersion")), str(model.get("modelDigest"))) != expected_model_key):
                raise ExperimentError("result model provenance is incompatible")
            if expected_evaluator_keys:
                actual_evaluators = {
                    (str(item.get("evaluatorId")), str(item.get("evaluatorVersion")))
                    for item in provenance.get("evaluators", [])
                    if isinstance(item, Mapping)
                }
                if not expected_evaluator_keys <= actual_evaluators:
                    raise ExperimentError("result evaluator provenance is incompatible")
            if expected_deployment and provenance.get("deploymentRevision") != expected_deployment:
                raise ExperimentError("result deployment provenance is incompatible")
            if expected_reproducibility is not None:
                actual_reproducibility = provenance.get("reproducibility")
                if not isinstance(actual_reproducibility, Mapping):
                    raise ExperimentError("result reproducibility is unavailable")
                # Prompt and policy revisions belong to each variant.  Only
                # stable replay controls are shared across baseline/candidate.
                stable_keys = ("seed", "clockMode", "timeZone", "locale", "fixtureRevision")
                if any(
                    actual_reproducibility.get(key) != expected_reproducibility.get(key)
                    for key in stable_keys
                ):
                    raise ExperimentError("result reproducibility is incompatible")
            if expected_configuration or expected_prompt or expected_policy:
                actual_reproducibility = provenance.get("reproducibility")
                if not isinstance(actual_reproducibility, Mapping):
                    raise ExperimentError("result reproducibility is unavailable")
                if expected_prompt and actual_reproducibility.get("promptRevision") != expected_prompt:
                    raise ExperimentError("result prompt provenance is incompatible")
                if expected_policy and actual_reproducibility.get("policyRevision") != expected_policy:
                    raise ExperimentError("result policy provenance is incompatible")
                actual_components = provenance.get("components")
                if expected_configuration and not isinstance(actual_components, list):
                    raise ExperimentError("result component provenance is unavailable")
                if expected_configuration and any(
                    isinstance(component, Mapping)
                    and component.get("configurationRevision") != expected_configuration
                    for component in cast(list[object], actual_components)
                ):
                    raise ExperimentError("result configuration provenance is incompatible")
            if expected_time_range:
                receipt_meta = result.get("__receiptMetadata")
                received = receipt_meta.get("receivedAt") if isinstance(receipt_meta, Mapping) else None
                try:
                    received_at = received if isinstance(received, datetime) else datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                    start_at = datetime.fromisoformat(expected_time_range[0].replace("Z", "+00:00"))
                    end_at = datetime.fromisoformat(expected_time_range[1].replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    raise ExperimentError("result receipt time is unavailable") from None
                if received_at < start_at or received_at > end_at:
                    raise ExperimentError("result receipt time is outside declared range")
                observed_receipt_times.append(received_at)
        if observed_cases != selected_cases:
            raise ExperimentError("result cohort evidence is incomplete")
        if expected_time_range:
            start_at = datetime.fromisoformat(expected_time_range[0].replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(expected_time_range[1].replace("Z", "+00:00"))
            if not observed_receipt_times or min(observed_receipt_times) != start_at or max(observed_receipt_times) != end_at:
                raise ExperimentError("result receipt range is not authoritative")


class PostgresExperimentEvidencePort:
    """Read/write content-free exact-version score facts through Observatory providers."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        clickhouse_url: str | None,
        clickhouse_database: str,
        clickhouse_user: str,
        clickhouse_password: str,
        metrics: Any | None = None,
        result_reference: EvaluationResultReferencePort | None = None,
        score_reference: EvaluationScoreReferencePort | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._endpoint = clickhouse_url.rstrip("/") if clickhouse_url else None
        self._database = clickhouse_database
        self._auth = (clickhouse_user, clickhouse_password)
        self._metrics = metrics
        self._result_reference = result_reference
        self._score_reference = score_reference

    def _span(self, operation: str, started: float, outcome: str, dependency: str, error: str | None = None) -> None:
        recorder = getattr(self._metrics, "record_component_span", None)
        if not callable(recorder):
            return
        try:
            recorder(
                component_id="aura.observatory.evaluation_regressions",
                component_version="1.0.0",
                deployment_id=str(getattr(self._metrics, "deployment_id", "observatory")),
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                outcome=outcome,
                dependency=dependency,
                error_code=error,
            )
        except Exception:
            return

    async def _delete_projection_facts(self, projection_id: str) -> None:
        """Compensate a ClickHouse insert after its PostgreSQL lease is fenced.

        PostgreSQL and ClickHouse cannot share a transaction.  If retention
        tombstones the outbox between the lease check and the ClickHouse
        insert, the fenced completion below fails.  Delete the exact
        regression identity before surfacing the lease loss so a late writer
        cannot resurrect facts after an authorized deletion.
        """

        literal = projection_id.replace("'", "''")
        query = (
            "ALTER TABLE evaluation_regression_metric_facts DELETE "
            f"WHERE projection_id = toUUID('{literal}') SETTINGS mutations_sync=2"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(
                self._endpoint,
                params={"database": self._database, "mutations_sync": "2"},
                auth=self._auth,
                content=query,
            )
            response.raise_for_status()

    async def delete_projection_facts(
        self, projection_id: str, *, lock_key: str | None = None
    ) -> None:
        """Delete one durable projection identity through the CH owner port."""
        context = self._connection_factory()
        connection = await context.__aenter__()
        key = lock_key or projection_id
        await connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        try:
            await self._delete_projection_facts(projection_id)
        finally:
            try:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,)
                )
            finally:
                await context.__aexit__(None, None, None)

    async def mark_projection_cleanup_pending(self, projection_id: str) -> None:
        """Leave a content-free PG tombstone for compensation recovery."""

        async with self._connection_factory() as connection:
            await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET cleanup_pending = TRUE, payload_json = '{}'::jsonb,
                    last_error = 'projection_cleanup_pending',
                    lease_token = NULL, lease_expires_at = NULL
                WHERE projection_id = %s""",
                (projection_id,),
            )
            await connection.commit()

    async def metric_values(
        self,
        result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[float, ...] | None:
        started = monotonic()
        ids = _uuid_list(result_ids)
        if self._score_reference is None:
            raise ExperimentUnavailable("ClickHouse score reference is unavailable")
        case_ids = tuple(
            case_id
            for cohort_id in cohort_ids
            for case_id in (cohort_case_ids or {}).get(str(cohort_id), ())
        )
        try:
            values = await self._score_reference.metric_values(ids, metric, case_ids=case_ids)
            self._span("evidence_read", started, "success", "clickhouse")
            return values
        except (ExperimentError, ExperimentUnavailable):
            raise
        except Exception as exc:
            self._span("evidence_read", started, "failure", "clickhouse", "provider_unavailable")
            raise ExperimentUnavailable("ClickHouse evidence provider is unavailable") from exc
    async def evidence(self, result_ids: Sequence[str], *, version: str) -> list[dict[str, str]]:
        started = monotonic()
        ids = _uuid_list(result_ids)
        try:
            if self._result_reference is None:
                raise ExperimentUnavailable("result evidence reference is unavailable")
            rows = await self._result_reference.get_result_envelopes(ids)
            values: list[dict[str, str]] = []
            for result_id, envelope in rows[:512]:
                receipt = envelope.get("__receiptMetadata")
                if not isinstance(receipt, Mapping) or not receipt.get("resultDigest"):
                    continue
                raw = envelope
                data = raw.get("data") if isinstance(raw, Mapping) else None
                persisted_version = data.get("resultVersion", version) if isinstance(data, Mapping) else version
                if not isinstance(persisted_version, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", persisted_version) is None:
                    raise ExperimentError("result evidence version is invalid")
                values.append({"kind": "result", "id": str(result_id), "digest": str(receipt["resultDigest"]), "version": persisted_version})
            self._span("evidence_reference_read", started, "success", "postgresql")
            return values
        except Exception as exc:
            self._span("evidence_reference_read", started, "failure", "postgresql", "provider_unavailable")
            raise ExperimentUnavailable("result evidence provider is unavailable") from exc

    async def _score_side_state(
        self,
        result_ids: Sequence[str],
        metric: str,
        *,
        cohort_ids: Sequence[str] = (),
        expected_evaluator: Mapping[str, object] | None = None,
        expected_variant: Mapping[str, object] | None = None,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> str:
        """Validate closed score metadata without returning score payloads."""
        ids = _uuid_list(result_ids)
        cases = tuple(
            case_id
            for cohort_id in cohort_ids
            for case_id in (cohort_case_ids or {}).get(str(cohort_id), ())
        )
        try:
            if self._score_reference is None:
                raise ExperimentUnavailable("ClickHouse score reference is unavailable")
            rows = await self._score_reference.score_metadata(ids, metric, case_ids=cases)
            if not isinstance(rows, list) or len(rows) > 100_000:
                raise ExperimentError("score metadata exceeds bound")
            if not rows:
                return "missing"
            observed_ids = {
                str(row.get("result_id")) for row in rows if isinstance(row, Mapping)
            }
            if observed_ids != set(ids):
                return "partial"
            if any(
                not isinstance(row, Mapping)
                or row.get("capture_classification") != "metadata_only"
                for row in rows
            ):
                return "incompatible"
            if any(
                isinstance(row, Mapping) and str(row.get("evidence_status", "")) == "late"
                for row in rows
            ):
                return "late"
            observed_cases = {
                str(row.get("case_id")) for row in rows if isinstance(row, Mapping)
            }
            if cases and observed_cases != set(cases):
                return "partial" if observed_cases < set(cases) else "incompatible"
            # Each side must have one exact evaluator/component boundary. The
            # baseline and candidate sides may legitimately differ.
            expected_evaluator_key = (
                str(expected_evaluator.get("evaluatorId")),
                str(expected_evaluator.get("evaluatorVersion")),
            ) if isinstance(expected_evaluator, Mapping) else None
            expected_components = {
                (str(item.get("componentId")), str(item.get("componentVersion")))
                for item in (expected_variant or {}).get("components", [])
                if isinstance(item, Mapping)
            }
            boundaries = {
                (
                    str(row.get("evaluator_id")),
                    str(row.get("evaluator_version")),
                    str(row.get("component_id")),
                    str(row.get("component_version")),
                )
                for row in rows
                if isinstance(row, Mapping)
            }
            if expected_evaluator_key and any(
                (str(row.get("evaluator_id")), str(row.get("evaluator_version"))) != expected_evaluator_key
                for row in rows if isinstance(row, Mapping)
            ):
                return "incompatible"
            if expected_components and any(
                (str(row.get("component_id")), str(row.get("component_version"))) not in expected_components
                for row in rows if isinstance(row, Mapping)
            ):
                return "incompatible"
            if isinstance(expected_variant, Mapping):
                expected_model = expected_variant.get("model")
                expected_deployment = expected_variant.get("deploymentRevision")
                for row in rows:
                    raw_provenance = row.get("execution_provenance_json") if isinstance(row, Mapping) else None
                    if not isinstance(raw_provenance, str):
                        return "incompatible"
                    try:
                        provenance = json.loads(raw_provenance)
                    except json.JSONDecodeError:
                        return "incompatible"
                    if not isinstance(provenance, Mapping):
                        return "incompatible"
                    if expected_deployment and provenance.get("deploymentRevision") != expected_deployment:
                        return "incompatible"
                    actual_model = provenance.get("model")
                    if isinstance(expected_model, Mapping) and actual_model != expected_model:
                        return "incompatible"
            return "complete" if len(boundaries) == 1 else "incompatible"
        except (ExperimentError, ExperimentUnavailable):
            raise
        except Exception as exc:
            raise ExperimentUnavailable("ClickHouse score metadata is unavailable") from exc

    async def assess(
        self,
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        exact_version: str,
        expected_evaluator: Mapping[str, object] | None = None,
        baseline_variant: Mapping[str, object] | None = None,
        candidate_variant: Mapping[str, object] | None = None,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> EvidenceAssessment:
        baseline = await self.metric_values(
            baseline_result_ids, metric, cohort_ids, cohort_case_ids=cohort_case_ids
        )
        candidate = await self.metric_values(
            candidate_result_ids, metric, cohort_ids, cohort_case_ids=cohort_case_ids
        )
        references = tuple(await self.evidence(tuple(baseline_result_ids) + tuple(candidate_result_ids), version=exact_version))
        baseline_state = await self._score_side_state(
            baseline_result_ids,
            metric,
            cohort_ids=cohort_ids,
            expected_evaluator=expected_evaluator,
            expected_variant=baseline_variant,
            cohort_case_ids=cohort_case_ids,
        )
        candidate_state = await self._score_side_state(
            candidate_result_ids,
            metric,
            cohort_ids=cohort_ids,
            expected_evaluator=expected_evaluator,
            expected_variant=candidate_variant,
            cohort_case_ids=cohort_case_ids,
        )
        if "incompatible" in {baseline_state, candidate_state}:
            return EvidenceAssessment("incompatible", baseline or (), candidate or (), references)
        if "late" in {baseline_state, candidate_state}:
            return EvidenceAssessment("late", baseline or (), candidate or (), references)
        if baseline is None or candidate is None:
            return EvidenceAssessment("missing", baseline or (), candidate or (), references)
        if len(references) < len(set(baseline_result_ids) | set(candidate_result_ids)):
            return EvidenceAssessment("partial", baseline, candidate, references)
        return EvidenceAssessment("complete", baseline, candidate, references)

    async def persist_facts(
        self,
        regression_id: str,
        regression_version: str,
        metric: str,
        metric_class: str,
        cohort_ids: Sequence[str],
        baseline_values: Sequence[float],
        candidate_values: Sequence[float],
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        evidence_state: str,
        *,
        threshold_index: int = 0,
        projection_id: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        started = monotonic()
        if not self._endpoint:
            raise ExperimentUnavailable("ClickHouse evidence provider is unavailable")
        active_projection_id: str | None = projection_id
        active_lease_token: str | None = lease_token
        async with self._connection_factory() as connection:
            if projection_id is None or lease_token is None:
                token = str(uuid4())
                claim = await connection.execute(
                    """WITH claimable AS (
                        SELECT projection_id
                        FROM observatory_evaluation_regression_projection_outbox
                        WHERE regression_id = %s AND regression_version = %s
                          AND metric_id = %s AND threshold_index = %s
                          AND status IN ('pending', 'failed')
                          AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE observatory_evaluation_regression_projection_outbox AS p
                    SET status = 'pending', lease_token = %s,
                        lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds',
                        attempts = p.attempts + 1
                    FROM claimable c
                    WHERE p.projection_id = c.projection_id
                    RETURNING p.projection_id, p.lease_token, p.payload_json""",
                    (regression_id, regression_version, metric, threshold_index, token),
                )
                claimed = await claim.fetchone()
            else:
                claim = await connection.execute(
                    """SELECT projection_id, lease_token, payload_json
                    FROM observatory_evaluation_regression_projection_outbox
                    WHERE projection_id = %s AND lease_token = %s
                      AND status = 'pending' AND lease_expires_at > CURRENT_TIMESTAMP""",
                    (projection_id, lease_token),
                )
                claimed = await claim.fetchone()
            if claimed is None:
                await connection.rollback()
                raise ExperimentUnavailable("projection outbox item is unavailable")
            active_projection_id, active_lease_token = str(claimed[0]), str(claimed[1])
            payload = claimed[2] if isinstance(claimed[2], Mapping) else json.loads(str(claimed[2]))
            if isinstance(payload, Mapping):
                stored_baseline = payload.get("baselineValues")
                stored_candidate = payload.get("candidateValues")
                if isinstance(stored_baseline, list) and isinstance(stored_candidate, list):
                    baseline_values = tuple(float(value) for value in stored_baseline)
                    candidate_values = tuple(float(value) for value in stored_candidate)
            # Mark the exact projection as write-in-flight before leaving
            # PostgreSQL.  A process crash after the CH insert but before the
            # applied update therefore leaves a durable cleanup claim.
            inflight = await connection.execute(
                """UPDATE observatory_evaluation_regression_projection_outbox
                SET cleanup_pending = TRUE
                WHERE projection_id = %s AND lease_token = %s
                  AND status = 'pending' AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING projection_id""",
                (active_projection_id, active_lease_token),
            )
            if await inflight.fetchone() is None:
                await connection.rollback()
                raise ExperimentUnavailable("projection lease was lost")
            await connection.commit()
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        rows: list[dict[str, object]] = []
        for side, values, result_ids in (("baseline", baseline_values, baseline_result_ids), ("candidate", candidate_values, candidate_result_ids)):
            for index, value in enumerate(values[:100_000]):
                rows.append({
                    "projection_id": active_projection_id or str(uuid4()),
                    "regression_id": regression_id,
                    "regression_version": regression_version,
                    # ClickHouse ORDER BY keys are non-nullable.  The zero UUID
                    # is a closed sentinel for a missing source reference;
                    # result identities remain content-free and bounded.
                    "source_result_id": result_ids[index] if index < len(result_ids) else "00000000-0000-0000-0000-000000000000",
                    "exact_version": regression_version,
                    "metric_id": metric,
                    "threshold_index": threshold_index,
                    "metric_class": metric_class,
                    "cohort_id": cohort_ids[index % len(cohort_ids)],
                    "side": side,
                    "value": float(value),
                    "sample_count": len(values),
                    "observed_at": now,
                    "evidence_state": evidence_state,
                    "late": 1 if evidence_state == "late" else 0,
                    "capture_policy": "metadata_only",
                })
        if not rows:
            terminal = evidence_state in {
                "missing", "partial", "insufficient", "late", "incompatible"
            }
            assert active_projection_id is not None and active_lease_token is not None
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_regression_projection_outbox
                    SET status = %s, cleanup_pending = FALSE, last_error = %s, applied_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE applied_at END,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE projection_id = %s AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING projection_id""",
                    (
                        "applied" if terminal else "failed",
                        None if terminal else "empty_projection",
                        terminal,
                        active_projection_id,
                        active_lease_token,
                    ),
                )
                if await result.fetchone() is None:
                    await connection.rollback()
                    raise ExperimentUnavailable("projection lease was lost")
                await connection.commit()
            if not terminal:
                raise ExperimentUnavailable("complete projection has no metric facts")
            self._span("fact_projection_write", started, "success", "clickhouse")
            return
        query = "INSERT INTO evaluation_regression_metric_facts FORMAT JSONEachRow\n" + "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
        insert_succeeded = False
        lock_context = self._connection_factory()
        lock_connection = await lock_context.__aenter__()
        await lock_connection.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (regression_id,),
        )
        guard = await lock_connection.execute(
            """SELECT status, lease_token, cleanup_pending
            FROM observatory_evaluation_regression_projection_outbox
            WHERE projection_id = %s AND lease_token = %s
              AND status = 'pending'
              AND lease_expires_at > CURRENT_TIMESTAMP""",
            (active_projection_id, active_lease_token),
        )
        guard_row = await guard.fetchone()
        if guard_row is None:
            await lock_context.__aexit__(None, None, None)
            raise ExperimentUnavailable("projection lease was fenced")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.post(
                    self._endpoint,
                    params={
                        "database": self._database,
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": f"{regression_id}:{regression_version}:{metric}:{threshold_index}",
                    },
                    auth=self._auth,
                    content=query,
                )
                response.raise_for_status()
            insert_succeeded = True
            # The PG outbox is the retry authority.  Marking it only after a
            # successful CH response makes a crash/provider outage replayable.
            result = await lock_connection.execute(
                    """UPDATE observatory_evaluation_regression_projection_outbox
                    SET status = 'applied', cleanup_pending = FALSE, applied_at = CURRENT_TIMESTAMP,
                        lease_token = NULL, lease_expires_at = NULL, last_error = NULL
                    WHERE projection_id = %s AND lease_token = %s
                      AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING projection_id""",
                    (active_projection_id, active_lease_token),
                )
            if await result.fetchone() is None:
                await lock_connection.rollback()
                raise ExperimentUnavailable("projection lease was lost")
            await lock_connection.commit()
            self._span("fact_projection_write", started, "success", "clickhouse")
        except Exception as exc:
            cleanup_error: Exception | None = None
            if insert_succeeded:
                try:
                    await self._delete_projection_facts(str(active_projection_id))
                except Exception as compensation_error:
                    cleanup_error = compensation_error
                    try:
                        await self.mark_projection_cleanup_pending(str(active_projection_id))
                    except Exception:
                        pass
            try:
                await lock_connection.execute(
                        """UPDATE observatory_evaluation_regression_projection_outbox
                        SET status = 'failed', last_error = %s,
                            lease_token = NULL, lease_expires_at = NULL
                        WHERE projection_id = %s AND lease_token = %s
                          AND lease_expires_at > CURRENT_TIMESTAMP""",
                    ("provider_unavailable", active_projection_id, active_lease_token),
                )
                await lock_connection.commit()
            except Exception:
                pass
            self._span("fact_projection_write", started, "failure", "clickhouse", "provider_unavailable")
            if cleanup_error is not None:
                raise ExperimentUnavailable(
                    "ClickHouse fact projection compensation is unavailable"
                ) from cleanup_error
            raise ExperimentUnavailable("ClickHouse fact projection is unavailable") from exc
        finally:
            try:
                await lock_connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (regression_id,),
                )
            finally:
                await lock_context.__aexit__(None, None, None)


__all__ = [
    "PostgresExperimentEvidencePort",
    "PostgresExperimentReferencePort",
    "PostgresExperimentRepository",
]
