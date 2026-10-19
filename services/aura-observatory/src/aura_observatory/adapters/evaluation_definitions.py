"""Provider adapters for immutable evaluation definitions.

The adapter accepts only the content-free domain projection.  PostgreSQL is
never given a case ``content`` member or an object-store locator; authorized
bytes remain behind the existing artifact port.
"""

# psycopg's result row protocol is provider-defined; all values are validated
# again by the domain ``from_payload`` boundary before they become a record.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import json
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import uuid4

from aura_observatory.evaluation_definitions import (
    CaseDefinition,
    DatasetDefinition,
    DefinitionConflictError,
    DefinitionStorageError,
    EvaluatorDefinition,
    SuiteDefinition,
)

if TYPE_CHECKING:
    from aura_observatory.metrics import ObservatoryMetrics

T = TypeVar("T", DatasetDefinition, CaseDefinition, EvaluatorDefinition, SuiteDefinition)


class InMemoryEvaluationDefinitionRepository:
    """Deterministic reference repository used by isolated tests and composition."""

    def __init__(self) -> None:
        self.datasets: dict[tuple[str, str], DatasetDefinition] = {}
        self.cases: dict[tuple[str, str], CaseDefinition] = {}
        self.evaluators: dict[tuple[str, str], EvaluatorDefinition] = {}
        self.suites: dict[tuple[str, str], SuiteDefinition] = {}
        self.components: set[tuple[str, str]] = set()
        self.definition_audits: list[dict[str, str]] = []

    def add_component(self, component_id: str, version: str) -> None:
        self.components.add((component_id, version))

    async def create_dataset(self, definition: DatasetDefinition) -> DatasetDefinition:
        return self._put(self.datasets, definition)

    async def create_case(self, definition: CaseDefinition) -> CaseDefinition:
        dataset = self.datasets.get((definition.dataset.resource_id, definition.dataset.version))
        if dataset is None or definition.case_id not in dataset.case_ids:
            self.definition_audits.append(
                {"resource_type": definition.resource_type, "resource_id": definition.resource_id,
                 "version": definition.version, "operation": "reject", "outcome": "rejected"}
            )
            raise KeyError("case is not declared by dataset")
        conflicting = [
            item for (case_id, _), item in self.cases.items() if case_id == definition.case_id
        ]
        if conflicting and all(item.dataset != definition.dataset for item in conflicting):
            self.definition_audits.append(
                {"resource_type": definition.resource_type, "resource_id": definition.resource_id,
                 "version": definition.version, "operation": "conflict", "outcome": "rejected"}
            )
            raise DefinitionConflictError("case identifier is bound to another dataset")
        if any(
            item.dataset == definition.dataset and item.version != definition.version
            for item in conflicting
        ):
            self.definition_audits.append(
                {"resource_type": definition.resource_type, "resource_id": definition.resource_id,
                 "version": definition.version, "operation": "conflict", "outcome": "rejected"}
            )
            raise DefinitionConflictError("dataset case version is immutable")
        return self._put(self.cases, definition)

    async def create_evaluator(self, definition: EvaluatorDefinition) -> EvaluatorDefinition:
        return self._put(self.evaluators, definition)

    async def create_suite(self, definition: SuiteDefinition) -> SuiteDefinition:
        if (definition.dataset.resource_id, definition.dataset.version) not in self.datasets:
            self.definition_audits.append(
                {"resource_type": definition.resource_type, "resource_id": definition.resource_id,
                 "version": definition.version, "operation": "reject", "outcome": "rejected"}
            )
            raise KeyError("dataset version does not exist")
        for reference in definition.evaluators:
            if (reference.evaluator_id, reference.evaluator_version) not in self.evaluators:
                raise KeyError("evaluator version does not exist")
        for binding in definition.components:
            if (
                self.components
                and (binding.component_id, binding.component_version) not in self.components
            ):
                raise KeyError("component version does not exist")
        return self._put(self.suites, definition)

    async def get_dataset(self, resource_id: str, version: str) -> DatasetDefinition | None:
        return self.datasets.get((resource_id, version))

    async def get_case(self, resource_id: str, version: str) -> CaseDefinition | None:
        return self.cases.get((resource_id, version))

    async def get_evaluator(self, resource_id: str, version: str) -> EvaluatorDefinition | None:
        return self.evaluators.get((resource_id, version))

    async def get_suite(self, resource_id: str, version: str) -> SuiteDefinition | None:
        return self.suites.get((resource_id, version))

    async def component_exists(self, component_id: str, version: str) -> bool:
        return not self.components or (component_id, version) in self.components

    async def case_declared(self, dataset_id: str, version: str, case_id: str) -> bool:
        dataset = self.datasets.get((dataset_id, version))
        return dataset is not None and case_id in dataset.case_ids

    async def dataset_case_membership_complete(self, dataset_id: str, version: str) -> bool:
        dataset = self.datasets.get((dataset_id, version))
        if dataset is None:
            return False
        return all(
            any(
                case.case_id == case_id
                and case.dataset.resource_id == dataset_id
                and case.dataset.version == version
                for case in self.cases.values()
            )
            for case_id in dataset.case_ids
        )

    def _put(self, store: dict[tuple[str, str], T], definition: T) -> T:
        key = (definition.resource_id, definition.version)
        existing = store.get(key)
        if existing is not None:
            if existing.canonical_fingerprint() != definition.canonical_fingerprint():
                self.definition_audits.append(
                    {
                        "resource_type": definition.resource_type,
                        "resource_id": definition.resource_id,
                        "version": definition.version,
                        "operation": "conflict",
                        "outcome": "rejected",
                    }
                )
                raise DefinitionConflictError("immutable definition fingerprint conflict")
            self.definition_audits.append(
                {
                    "resource_type": definition.resource_type,
                    "resource_id": definition.resource_id,
                    "version": definition.version,
                    "operation": "idempotent_reuse",
                    "outcome": "duplicate",
                }
            )
            return existing
        store[key] = definition
        self.definition_audits.append(
            {"resource_type": definition.resource_type, "resource_id": definition.resource_id,
             "version": definition.version, "operation": "create", "outcome": "accepted"}
        )
        return definition


class PostgresEvaluationDefinitionRepository:
    """Transactional adapter for the AURA-0093 definition migration."""

    _TABLES: dict[type[Any], tuple[str, str]] = {
        DatasetDefinition: ("observatory_evaluation_datasets", "dataset"),
        CaseDefinition: ("observatory_evaluation_cases", "case"),
        EvaluatorDefinition: ("observatory_evaluation_evaluators", "evaluator"),
        SuiteDefinition: ("observatory_evaluation_suites", "suite"),
    }

    def __init__(
        self, connection_factory: Callable[[], Any], metrics: ObservatoryMetrics | None = None
    ) -> None:
        self._connection_factory = connection_factory
        self._metrics = metrics

    async def create_dataset(self, definition: DatasetDefinition) -> DatasetDefinition:
        return await self._create(definition)

    async def create_case(self, definition: CaseDefinition) -> CaseDefinition:
        return await self._create(definition)

    async def create_evaluator(self, definition: EvaluatorDefinition) -> EvaluatorDefinition:
        return await self._create(definition)

    async def create_suite(self, definition: SuiteDefinition) -> SuiteDefinition:
        if (
            await self.get_dataset(definition.dataset.resource_id, definition.dataset.version)
            is None
        ):
            await self._record_rejection_audit(definition, "reject")
            raise KeyError("dataset version does not exist")
        for reference in definition.evaluators:
            if (
                await self.get_evaluator(reference.evaluator_id, reference.evaluator_version)
                is None
            ):
                await self._record_rejection_audit(definition, "reject")
                raise KeyError("evaluator version does not exist")
        for binding in definition.components:
            if not await self.component_exists(binding.component_id, binding.component_version):
                await self._record_rejection_audit(definition, "reject")
                raise KeyError("component version does not exist")
        return await self._create(definition)

    async def get_dataset(self, resource_id: str, version: str) -> DatasetDefinition | None:
        return await self._get(DatasetDefinition, resource_id, version)

    async def get_case(self, resource_id: str, version: str) -> CaseDefinition | None:
        return await self._get(CaseDefinition, resource_id, version)

    async def get_evaluator(self, resource_id: str, version: str) -> EvaluatorDefinition | None:
        return await self._get(EvaluatorDefinition, resource_id, version)

    async def get_suite(self, resource_id: str, version: str) -> SuiteDefinition | None:
        return await self._get(SuiteDefinition, resource_id, version)

    async def component_exists(self, component_id: str, version: str) -> bool:
        started = monotonic()
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """SELECT 1 FROM observatory_component_definitions
                    WHERE component_id = %s AND component_version = %s LIMIT 1""",
                    (component_id, version),
                )
                value = await result.fetchone() is not None
        except Exception:
            self._record_dependency("read", "failure", started, "database_failure")
            raise DefinitionStorageError("definition storage failure") from None
        self._record_dependency("read", "success", started)
        return value

    async def case_declared(self, dataset_id: str, version: str, case_id: str) -> bool:
        started = monotonic()
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    "SELECT 1 FROM observatory_evaluation_dataset_cases "
                    "WHERE dataset_id = %s AND dataset_version = %s AND case_id = %s",
                    (dataset_id, version, case_id),
                )
                value = await result.fetchone() is not None
        except Exception:
            self._record_dependency("read", "failure", started, "database_failure")
            raise DefinitionStorageError("definition storage failure") from None
        self._record_dependency("read", "success", started)
        return value

    async def dataset_case_membership_complete(self, dataset_id: str, version: str) -> bool:
        started = monotonic()
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    """SELECT cardinality(dataset.case_ids) = COUNT(membership.case_id)
                    FROM observatory_evaluation_datasets AS dataset
                    LEFT JOIN observatory_evaluation_dataset_cases AS membership
                      ON membership.dataset_id = dataset.dataset_id
                     AND membership.dataset_version = dataset.dataset_version
                    WHERE dataset.dataset_id = %s AND dataset.dataset_version = %s
                    GROUP BY dataset.case_ids""",
                    (dataset_id, version),
                )
                row = await result.fetchone()
                value = bool(row and row[0])
        except Exception:
            self._record_dependency("read", "failure", started, "database_failure")
            raise DefinitionStorageError("definition storage failure") from None
        self._record_dependency("read", "success", started)
        return value

    async def _create(self, definition: T) -> T:
        started = monotonic()
        try:
            result = await self._create_inner(definition)
        except DefinitionConflictError:
            self._record_dependency("write", "failure", started, "validation")
            await self._record_rejection_audit(definition, "conflict")
            raise
        except KeyError:
            self._record_dependency("write", "failure", started, "validation")
            await self._record_rejection_audit(definition, "reject")
            raise
        except Exception:
            self._record_dependency("write", "failure", started, "database_failure")
            await self._record_rejection_audit(definition, "reject")
            raise DefinitionStorageError("definition storage failure") from None
        self._record_dependency("write", "success", started)
        return result

    async def _record_rejection_audit(self, definition: T, operation: str) -> None:
        """Write rejection evidence on a fresh transaction after rollback.

        Rejected definition state is never committed.  Audit failure is
        deliberately swallowed so a provider error cannot cross this boundary
        or turn a deterministic validation response into an availability loop.
        """

        try:
            async with self._connection_factory() as connection:
                await self._audit(connection, definition, operation, "rejected")
        except Exception:
            return

    async def _create_inner(self, definition: T) -> T:
        table, resource_type = self._table(definition)
        storage = definition.to_storage()
        payload = json.dumps(storage["payload"], sort_keys=True, separators=(",", ":"))
        resource = cast(dict[str, Any], storage["payload"])
        authorization = cast(dict[str, Any], resource.get("captureAuthorization", {}))
        provenance = cast(dict[str, Any], resource["provenance"])
        async with self._connection_factory() as connection:
            existing_result = await connection.execute(
                f"SELECT definition_json, fingerprint FROM {table} "
                f"WHERE {resource_type}_id = %s AND {resource_type}_version = %s",
                (definition.resource_id, definition.version),
            )
            existing = await existing_result.fetchone()
            if existing is not None:
                existing_fingerprint = str(
                    existing[1] if not isinstance(existing, dict) else existing["fingerprint"]
                )
                if existing_fingerprint != storage["fingerprint"]:
                    await self._audit(connection, definition, "conflict", "rejected")
                    raise DefinitionConflictError("immutable definition fingerprint conflict")
                await self._audit(connection, definition, "idempotent_reuse", "duplicate")
                return self._decode(
                    type(definition),
                    existing[0] if not isinstance(existing, dict) else existing["definition_json"],
                )
            inserted_result: Any = None
            if isinstance(definition, DatasetDefinition):
                inserted_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_datasets(
                    dataset_id, dataset_version, source_kind, scopes,
                    capture_classification, authorization_purpose,
                    authorization_record_id, authorized_at, source_digest,
                    created_by_component, created_by_version, retention_mode,
                    case_ids, fingerprint, definition_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'manual_only', %s, %s, %s::jsonb, %s)
                    ON CONFLICT (dataset_id, dataset_version) DO NOTHING""",
                    (
                        definition.resource_id,
                        definition.version,
                        definition.source_kind.value,
                        list(definition.scopes),
                        authorization["captureClassification"],
                        authorization["authorizationPurpose"],
                        authorization.get("authorizationRecordId"),
                        authorization.get("authorizedAt"),
                        provenance["sourceDigest"],
                        provenance["createdByComponent"],
                        provenance["createdByVersion"],
                        list(definition.case_ids),
                        storage["fingerprint"],
                        payload,
                        definition.created_at,
                    ),
                )
            elif isinstance(definition, CaseDefinition):
                declared = await connection.execute(
                    "SELECT case_ids FROM observatory_evaluation_datasets "
                    "WHERE dataset_id = %s AND dataset_version = %s FOR SHARE",
                    (definition.dataset.resource_id, definition.dataset.version),
                )
                dataset_row = await declared.fetchone()
                declared_ids = dataset_row[0] if dataset_row is not None else []
                if definition.resource_id not in {str(item) for item in declared_ids}:
                    await self._audit(connection, definition, "reject", "rejected")
                    raise DefinitionConflictError("case is not declared by dataset")
                existing_case_versions = await connection.execute(
                    """SELECT dataset_id, dataset_version, case_version
                    FROM observatory_evaluation_cases
                    WHERE case_id = %s FOR UPDATE""",
                    (definition.resource_id,),
                )
                for existing_case in await existing_case_versions.fetchall():
                    existing_dataset = (str(existing_case[0]), str(existing_case[1]))
                    requested_dataset = (
                        definition.dataset.resource_id,
                        definition.dataset.version,
                    )
                    if existing_dataset == requested_dataset and (
                        str(existing_case[2]) != definition.version
                    ):
                        await self._audit(connection, definition, "conflict", "rejected")
                        raise DefinitionConflictError("dataset case version conflict")
                    if existing_dataset != requested_dataset:
                        await self._audit(connection, definition, "conflict", "rejected")
                        raise DefinitionConflictError("case identifier is bound to another dataset")
                inserted_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_cases(
                    case_id, case_version, dataset_id, dataset_version, source_kind,
                    scopes, capture_classification, authorization_purpose,
                    authorization_record_id, authorized_at, content_digest,
                    artifact_reference_ids, expectations_json, source_digest,
                    created_by_component, created_by_version, retention_mode,
                    fingerprint, definition_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s::jsonb, %s, %s, %s, 'manual_only', %s,
                            %s::jsonb, %s)
                    ON CONFLICT (case_id, case_version) DO NOTHING""",
                    (
                        definition.resource_id,
                        definition.version,
                        definition.dataset.resource_id,
                        definition.dataset.version,
                        definition.source_kind.value,
                        list(definition.scopes),
                        authorization["captureClassification"],
                        authorization["authorizationPurpose"],
                        authorization.get("authorizationRecordId"),
                        authorization.get("authorizedAt"),
                        definition.content_digest,
                        list(definition.input_artifact_ids),
                        json.dumps(definition.expectations.to_payload(), sort_keys=True)
                        if definition.expectations is not None
                        else None,
                        provenance["sourceDigest"],
                        provenance["createdByComponent"],
                        provenance["createdByVersion"],
                        storage["fingerprint"],
                        payload,
                        definition.created_at,
                    ),
                )
                membership_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_dataset_cases(
                    dataset_id, dataset_version, case_id, case_version)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (dataset_id, dataset_version, case_id) DO UPDATE
                    SET case_version = EXCLUDED.case_version
                    WHERE observatory_evaluation_dataset_cases.case_version = EXCLUDED.case_version
                    """,
                    (
                        definition.dataset.resource_id,
                        definition.dataset.version,
                        definition.resource_id,
                        definition.version,
                    ),
                )
                if getattr(membership_result, "rowcount", 1) == 0:
                    await self._audit(connection, definition, "conflict", "rejected")
                    raise DefinitionConflictError("dataset case version conflict")
            elif isinstance(definition, EvaluatorDefinition):
                inserted_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_evaluators(
                    evaluator_id, evaluator_version, kind, metric, component_ids,
                    source_digest, created_by_component, created_by_version,
                    retention_mode, fingerprint, definition_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual_only',
                            %s, %s::jsonb, %s)
                    ON CONFLICT (evaluator_id, evaluator_version) DO NOTHING""",
                    (
                        definition.resource_id,
                        definition.version,
                        definition.kind,
                        definition.metric,
                        list(definition.component_ids),
                        provenance["sourceDigest"],
                        provenance["createdByComponent"],
                        provenance["createdByVersion"],
                        storage["fingerprint"],
                        payload,
                        definition.created_at,
                    ),
                )
            elif isinstance(definition, SuiteDefinition):
                inserted_result = await connection.execute(
                    """INSERT INTO observatory_evaluation_suites(
                    suite_id, suite_version, dataset_id, dataset_version,
                    source_digest, created_by_component, created_by_version,
                    timeout_ms, max_concurrency, max_attempts, max_cases,
                    max_input_bytes, max_output_bytes, max_tokens,
                    retention_mode, fingerprint, definition_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, 'manual_only', %s, %s::jsonb, %s)
                    ON CONFLICT (suite_id, suite_version) DO NOTHING""",
                    (
                        definition.resource_id,
                        definition.version,
                        definition.dataset.resource_id,
                        definition.dataset.version,
                        provenance["sourceDigest"],
                        provenance["createdByComponent"],
                        provenance["createdByVersion"],
                        definition.limits.timeout_ms,
                        definition.limits.max_concurrency,
                        definition.limits.max_attempts,
                        definition.limits.max_cases,
                        definition.limits.max_input_bytes,
                        definition.limits.max_output_bytes,
                        definition.limits.max_tokens,
                        storage["fingerprint"],
                        payload,
                        definition.created_at,
                    ),
                )
                for ordinal, reference in enumerate(definition.evaluators):
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_suite_evaluators(
                        suite_id, suite_version, evaluator_id, evaluator_version, ordinal)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""",
                        (
                            definition.resource_id,
                            definition.version,
                            reference.evaluator_id,
                            reference.evaluator_version,
                            ordinal,
                        ),
                    )
                for ordinal, binding in enumerate(definition.components):
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_suite_components(
                        suite_id, suite_version, component_id, component_version,
                        code_revision, image_digest, configuration_revision, ordinal)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""",
                        (
                            definition.resource_id,
                            definition.version,
                            binding.component_id,
                            binding.component_version,
                            binding.code_revision,
                            binding.image_digest,
                            binding.configuration_revision,
                            ordinal,
                        ),
                    )
                for threshold in definition.manual_thresholds:
                    await connection.execute(
                        """INSERT INTO observatory_evaluation_suite_thresholds(
                        suite_id, suite_version, evaluator_id, evaluator_version,
                        metric, aggregation, operator, threshold_value, unit,
                        minimum_sample_size, missing_evidence_policy)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""",
                        (
                            definition.resource_id,
                            definition.version,
                            threshold.evaluator.evaluator_id,
                            threshold.evaluator.evaluator_version,
                            threshold.metric,
                            threshold.aggregation,
                            threshold.operator,
                            threshold.value,
                            threshold.unit,
                            threshold.minimum_sample_size,
                            threshold.missing_evidence_policy,
                        ),
                    )
            if inserted_result is not None and getattr(inserted_result, "rowcount", 1) == 0:
                existing_result = await connection.execute(
                    f"SELECT definition_json, fingerprint FROM {table} "
                    f"WHERE {resource_type}_id = %s AND {resource_type}_version = %s FOR UPDATE",
                    (definition.resource_id, definition.version),
                )
                existing = await existing_result.fetchone()
                if existing is None:
                    raise RuntimeError("definition insert disappeared")
                existing_fingerprint = str(
                    existing[1] if not isinstance(existing, dict) else existing["fingerprint"]
                )
                if existing_fingerprint != storage["fingerprint"]:
                    await self._audit(connection, definition, "conflict", "rejected")
                    raise DefinitionConflictError("immutable definition fingerprint conflict")
                await self._audit(connection, definition, "idempotent_reuse", "duplicate")
                return self._decode(
                    type(definition),
                    existing[0] if not isinstance(existing, dict) else existing["definition_json"],
                )
            await self._audit(connection, definition, "create", "accepted")
        return definition

    @staticmethod
    async def _audit(connection: Any, definition: T, operation: str, outcome: str) -> None:
        await connection.execute(
            """INSERT INTO observatory_evaluation_definition_audits(
            audit_id, resource_type, resource_id, resource_version, operation, outcome)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                str(uuid4()),
                definition.resource_type,
                definition.resource_id,
                definition.version,
                operation,
                outcome,
            ),
        )

    async def _get(self, definition_type: type[T], resource_id: str, version: str) -> T | None:
        table, resource_type = self._table_for_type(definition_type)
        started = monotonic()
        try:
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    f"SELECT definition_json FROM {table} "
                    f"WHERE {resource_type}_id = %s AND {resource_type}_version = %s",
                    (resource_id, version),
                )
                row = await result.fetchone()
        except Exception:
            self._record_dependency("read", "failure", started, "database_failure")
            raise DefinitionStorageError("definition storage failure") from None
        self._record_dependency("read", "success", started)
        if row is None:
            return None
        payload = row[0] if not isinstance(row, dict) else row["definition_json"]
        return self._decode(definition_type, payload)

    @classmethod
    def _table(cls, definition: Any) -> tuple[str, str]:
        return cls._table_for_type(type(definition))

    @classmethod
    def _table_for_type(cls, definition_type: type[Any]) -> tuple[str, str]:
        try:
            return cls._TABLES[definition_type]
        except KeyError as exc:
            raise TypeError("unsupported evaluation definition") from exc

    @staticmethod
    def _decode(definition_type: type[T], value: Any) -> T:
        payload: Any = json.loads(value) if isinstance(value, str) else value
        return definition_type.from_payload(cast(dict[str, Any], payload))

    def _record_dependency(
        self, operation: str, outcome: str, started: float, error_code: str | None = None
    ) -> None:
        if self._metrics is None:
            return
        duration_ms = max(0.0, (monotonic() - started) * 1000.0)
        self._metrics.record(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": "evaluation_definition_state", "outcome": outcome},
        )
        self._metrics.record_component_span(
            component_id="aura.observatory.evaluation_definitions",
            component_version="1.0.0",
            deployment_id=self._metrics.deployment_id,
            operation=operation,
            duration_ms=duration_ms,
            outcome="success" if outcome == "success" else "failure",
            dependency="evaluation_definition_state",
            error_code=error_code,
        )


__all__ = ["InMemoryEvaluationDefinitionRepository", "PostgresEvaluationDefinitionRepository"]
