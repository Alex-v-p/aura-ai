"""Production composition for the independent Observatory service."""
# ruff: noqa: E501,I001,F401
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

import psycopg
from redis.asyncio import Redis

from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.evaluation_definitions import (
    PostgresEvaluationDefinitionRepository,
)
from aura_observatory.adapters.evaluation_experiments import PostgresExperimentRepository
from aura_observatory.adapters.evaluation_live import PostgresEvaluationLiveEventStore
from aura_observatory.adapters.evaluation_orchestration import (
    OtlpComponentSpanExporter,
    PostgresEvaluationMetricStore,
    PostgresEvaluationOrchestrationRepository,
)
from aura_observatory.adapters.evaluation_query import PostgresEvaluationQueryBackend
from aura_observatory.adapters.evaluation_storage import (
    ClickHouseEvaluationScoreStore,
    PostgresEvaluationArtifactRepository,
    PostgresEvaluationResultReferencePort,
)
from aura_observatory.adapters.local_ollama import LocalOllamaAdapter
from aura_observatory.adapters.object_storage import GarageEvaluationObjectStore
from aura_observatory.adapters.run_evidence import (
    GarageRunEvidenceObjectStore,
    PostgresRunEvidenceRepository,
    UnavailableRunEvidenceObjectStore,
)
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
from aura_observatory.adapters.dashboards import (
    MemoryOperationsRepository,
    OperationResourceType,
    OperationsRepository,
    PostgresOperationsRepository,
)
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    RedisLoginStateBackend,
    RedisSessionBackend,
)
from aura_observatory.evaluation_api import (
    EvaluationApplicationService as EvaluationApiApplicationService,
)
from aura_observatory.evaluation_calibration import (
    CalibrationService,
    PostgresCalibrationRepository,
)
from aura_observatory.evaluation_definitions import EvaluationDefinitionService, EvaluatorDefinition
from aura_observatory.evaluation_evaluators import (
    DeterministicEvaluatorRegistry,
    EvaluationApplicationService,
)
from aura_observatory.evaluation_experiments import EvaluationExperimentService
from aura_observatory.evaluation_judges import (
    AuthorizedArtifactReader,
    LocalJudgeService,
    PostgresJudgeDefinitionRepository,
    PostgresJudgeExecutionRepository,
)
from aura_observatory.evaluation_orchestration import (
    EvaluationOrchestrationService,
    EvaluationSchedulerService,
)
from aura_observatory.evaluation_query import (
    EvaluationNotFound,
    EvaluationQueryService,
    EvaluationUnavailable,
)
from aura_observatory.evaluation_storage import (
    ArtifactStorageService,
    EvaluationDeletionService,
)
from aura_observatory.ingest import IngestReconciliationService, TelemetryIngestService
from aura_observatory.run_evidence import RunEvidenceIngestionService, RunInspectionService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import EvaluationTransportPort, ObservatoryQueryPort
from aura_observatory.query import ObservatoryHealthProjector, ObservatoryQueryService
from aura_observatory.settings import Settings
from aura_observatory.alerting import AlertService
from aura_observatory.annotations import AnnotationReferencePort, AnnotationService
from aura_observatory.dashboards import DashboardService
from aura_observatory.incidents import IncidentService
from aura_observatory.operational_live import OperationalLiveService
from aura_observatory.saved_queries import SavedQueryExecutionService, SavedQueryService


def _evaluation_definition_compatibility(definition: EvaluatorDefinition) -> None:
    """Keep deterministic registry checks while admitting persisted local judges."""
    if definition.kind == "local_judge":
        return
    DeterministicEvaluatorRegistry().validate_definition(definition)


class _ExactAnnotationReferenceProvider(Protocol):
    """Owning public provider for one exact opaque resource identity."""

    async def get(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None: ...


class _EvaluationAnnotationReferenceProvider(Protocol):
    """Public evaluation query port for exact typed resource references."""

    async def get(
        self, resource_type: str, resource_id: str, resource_version: str
    ) -> Mapping[str, object] | None: ...


class _ObservatoryAnnotationReferenceProvider:
    """Adapt the public Observatory query service to exact references.

    Run and deployment projections currently expose the stable v1 projection
    version rather than mutable storage versions.  The adapter checks that
    explicit version, bounds deployment catalogue scans, and never reads a
    sibling table.
    """

    _PROJECTION_VERSION = "1.0.0"
    _DEPLOYMENT_SCAN_LIMIT = 200

    def __init__(self, query: ObservatoryQueryService, resource_type: str) -> None:
        self._query = query
        self._resource_type = resource_type

    async def get(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        if self._resource_type == "run":
            return await self.run(resource_id, resource_version)
        if self._resource_type == "deployment":
            return await self.deployment(resource_id, resource_version)
        raise RuntimeError("unsupported annotation reference provider")

    async def run(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        if resource_version != self._PROJECTION_VERSION:
            return None
        value = await self._query.run_detail(resource_id, limit=1)
        return _exact_projection(value, "runId", resource_id, "runVersion", resource_version)

    async def deployment(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        if resource_version != self._PROJECTION_VERSION:
            return None
        deployments = await self._query.deployments()
        if len(deployments) > self._DEPLOYMENT_SCAN_LIMIT:
            raise RuntimeError("deployment reference provider exceeded its bounded lookup")
        value = next(
            (
                deployment
                for deployment in deployments
                if deployment.get("deploymentId") == resource_id
            ),
            None,
        )
        return _exact_projection(
            value,
            "deploymentId",
            resource_id,
            "deploymentVersion",
            resource_version,
        )


class _ObservatoryComponentHealthReferenceProvider:
    """Resolve a component-health evidence identity through query APIs."""

    _PROJECTION_VERSION = "1.0.0"

    def __init__(self, query: ObservatoryQueryService) -> None:
        self._query = query

    async def get(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        if resource_version != self._PROJECTION_VERSION:
            return None
        value = await self._query.component_health(resource_id, point_limit=1)
        return _exact_projection(value, "componentId", resource_id, "resourceVersion", resource_version)


def _exact_projection(
    value: Mapping[str, object] | None,
    identifier_key: str,
    resource_id: str,
    version_key: str,
    resource_version: str,
) -> Mapping[str, object] | None:
    """Accept only an exact public projection, never a latest-state guess."""

    if value is None or value.get(identifier_key) != resource_id:
        return None
    explicit_version = value.get(version_key, value.get("resourceVersion"))
    if explicit_version is not None:
        return value if explicit_version == resource_version else None
    return value if resource_version == _ObservatoryAnnotationReferenceProvider._PROJECTION_VERSION else None


class _EvaluationQueryAnnotationReferenceProvider:
    """Adapt the public evaluation query service to exact references."""

    def __init__(self, query: EvaluationQueryService) -> None:
        self._query = query

    async def get(self, resource_type: str, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        try:
            return await self._query.get_async(resource_type, resource_id, resource_version)
        except EvaluationNotFound:
            return None
        except EvaluationUnavailable as exc:
            raise RuntimeError("evaluation reference provider unavailable") from exc


class _OperationsAnnotationReferenceProvider:
    """Adapt the operations repository's public exact-resource port."""

    def __init__(self, repository: _IncidentReferenceRepository, resource_type: OperationResourceType = "incident") -> None:
        self._repository = repository
        self._resource_type = resource_type

    async def get(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        return await self._repository.get_exact(self._resource_type, resource_id, resource_version)


class _IncidentReferenceRepository(Protocol):
    """Public exact-resource surface exposed by the operations repository."""

    async def get_exact(
        self, resource_type: OperationResourceType, resource_id: str, version: str
    ) -> Mapping[str, object] | None: ...


class CompositeAnnotationTargetReferencePort(AnnotationReferencePort):
    """Validate every annotation target through its owning public port."""

    def __init__(
        self,
        *,
        run_provider: _ExactAnnotationReferenceProvider | None = None,
        deployment_provider: _ExactAnnotationReferenceProvider | None = None,
        experiment_provider: _EvaluationAnnotationReferenceProvider | None = None,
        regression_provider: _EvaluationAnnotationReferenceProvider | None = None,
        incident_provider: _ExactAnnotationReferenceProvider | None = None,
        alert_instance_provider: _ExactAnnotationReferenceProvider | None = None,
        component_health_provider: _ExactAnnotationReferenceProvider | None = None,
    ) -> None:
        self._providers: dict[str, _ExactAnnotationReferenceProvider | None] = {
            "run": run_provider,
            "deployment": deployment_provider,
            "experiment": _EvaluationResourceReferenceProvider(experiment_provider, "experiments")
            if experiment_provider is not None
            else None,
            "regression": _EvaluationResourceReferenceProvider(regression_provider, "regressions")
            if regression_provider is not None
            else None,
            "incident": incident_provider,
            "alert_instance": alert_instance_provider,
            "component_health": component_health_provider,
        }

    async def validate_annotation_target(self, target: Mapping[str, str]) -> None:
        target_type = target.get("targetType")
        provider = self._providers.get(target_type or "")
        if provider is None:
            raise RuntimeError("annotation reference provider unavailable")
        try:
            resource = await provider.get(
                target["targetId"],
                target["targetVersion"],
            )
        except (KeyError, ConnectionError, TimeoutError):
            raise
        except RuntimeError:
            raise
        if resource is None:
            raise KeyError("annotation target not found")

    async def validate_incident_references(
        self,
        related_alerts: list[Mapping[str, str]],
        evidence: list[Mapping[str, str]],
    ) -> None:
        """Resolve all incident links through exact owning public providers."""

        for reference in [*related_alerts, *evidence]:
            resource_type = reference.get("resourceType")
            if resource_type == "alert_instance":
                provider = self._providers.get("alert_instance")
            elif resource_type in {"run", "deployment", "experiment", "regression"}:
                provider = self._providers.get(resource_type)
            elif resource_type == "analytics_snapshot":
                raise RuntimeError("incident evidence reference provider unavailable")
            elif resource_type == "component_health":
                provider = self._providers.get("component_health")
            else:
                raise ValueError("incident evidence reference is incompatible")
            if provider is None:
                raise RuntimeError("incident reference provider unavailable")
            value = await provider.get(str(reference["resourceId"]), str(reference["resourceVersion"]))
            if value is None:
                raise KeyError("incident reference not found")


class _EvaluationResourceReferenceProvider:
    """Bind an evaluation query provider to one closed resource kind."""

    def __init__(
        self,
        provider: _EvaluationAnnotationReferenceProvider | None,
        resource_type: str,
    ) -> None:
        self._provider = provider
        self._resource_type = resource_type

    async def get(self, resource_id: str, resource_version: str) -> Mapping[str, object] | None:
        if self._provider is None:
            raise RuntimeError("evaluation reference provider unavailable")
        return await self._provider.get(self._resource_type, resource_id, resource_version)


class ObservatoryOperationsFacade:
    """Typed public operations surface for API and scheduler composition.

    The PostgreSQL/Memory classes are private providers behind this boundary.
    Resource kinds are a closed literal rather than caller-selected table or
    SQL names, and every method has an explicit bounded transport shape.
    """

    def __init__(self, repository: OperationsRepository) -> None:
        self._repository = repository

    async def create_resource(self, resource_type: OperationResourceType, resource: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        return await self._repository.create_resource(resource_type, resource, command_id=command_id)

    async def list_resources(self, resource_type: OperationResourceType, *, cursor: str | None = None, limit: int = 50, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> dict[str, object]:
        return await self._repository.list_resources(resource_type, cursor=cursor, limit=limit, state=state, kind=kind, severity=severity, rule_id=rule_id, target=target)

    async def get_resource(self, resource_type: OperationResourceType, resource_id: str, version: str | None = None) -> dict[str, object] | None:
        return await self._repository.get_resource(resource_type, resource_id, version)

    async def get_exact(self, resource_type: OperationResourceType, resource_id: str, version: str) -> dict[str, object] | None:
        """Resolve one opaque version through the owning operations port."""
        return await self._repository.get_resource(resource_type, resource_id, version)

    async def delete_resource(self, resource_type: OperationResourceType, resource_id: str, version: str, *, command_id: str | None = None) -> None:
        await self._repository.delete_resource(resource_type, resource_id, version, command_id=command_id)

    async def replace_resource(self, resource_type: OperationResourceType, resource: Mapping[str, object], *, command_id: str | None = None, expected_version: str | None = None) -> dict[str, object]:
        return await self._repository.replace_resource(resource_type, resource, command_id=command_id, expected_version=expected_version)

    async def append_event(self, resource_type: str, resource_id: str, version: str, change: str, state: str | None = None) -> dict[str, object]:
        return await self._repository.append_event(resource_type, resource_id, version, change, state)

    async def claim_due_alerts(self, *, now: str, limit: int = 16, owner: str = "evaluation_scheduler", lease_seconds: int = 120) -> list[dict[str, object]]:
        return await self._repository.claim_due_alerts(now=now, limit=limit, owner=owner, lease_seconds=lease_seconds)

    async def complete_alert_claim(self, rule_id: str, *, now: str, success: bool, owner: str = "evaluation_scheduler", lease_token: str | None = None) -> None:
        await self._repository.complete_alert_claim(rule_id, now=now, success=success, owner=owner, lease_token=lease_token)

    async def acknowledge_alert_instance(self, instance_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        """Atomically acknowledge one exact alert instance through its port."""
        return await self._repository.acknowledge_alert_instance(instance_id, request, command_id=command_id)

    async def list_history(self, *, cursor: str | None = None, limit: int = 50, resource_type: str | None = None, resource_id: str | None = None, resource_version: str | None = None) -> dict[str, object]:
        return await self._repository.list_history(cursor=cursor, limit=limit, resource_type=resource_type, resource_id=resource_id, resource_version=resource_version)

    async def events_after(self, last_event_id: str | None, *, limit: int = 1000) -> list[dict[str, object]]:
        return await self._repository.events_after(last_event_id, limit=limit)


def _production_metrics(settings: Settings, persistence: Any | None = None) -> ObservatoryMetrics:
    """Use the established bounded sink and optional cross-process export."""

    span_exporter = None
    if settings.telemetry_otlp_enabled:
        span_exporter = OtlpComponentSpanExporter(
            settings.telemetry_otlp_endpoint or settings.collector_endpoint,
            credential=settings.ingest_credential(),
            queue_capacity=settings.telemetry_otlp_queue_capacity,
            max_attempts=settings.telemetry_otlp_max_attempts,
            timeout_seconds=settings.telemetry_otlp_timeout_seconds,
            environment=settings.environment,
            git_commit=settings.deployment_commit,
            host_id=settings.deployment_host,
            service_role=(
                "worker"
                if settings.role in {"worker", "evaluation_worker"}
                else "scheduler"
                if settings.role == "evaluation_scheduler"
                else settings.service_role
            ),
            deployment_id=settings.deployment_id,
        )
    return ObservatoryMetrics(
        settings.metrics_file,
        persistence=persistence,
        span_exporter=span_exporter,
        deployment_id=settings.deployment_id,
    )


@asynccontextmanager
async def postgres_connection(database_url: str) -> AsyncGenerator[Any]:
    """Open one Observatory-owned PostgreSQL connection for a catalog operation."""

    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        yield connection


def _evaluation_live_publisher(settings: Settings) -> Any:
    """Return the shared durable metadata-event boundary for worker services."""

    live_store = PostgresEvaluationLiveEventStore(
        lambda: postgres_connection(settings.database_url)
    )

    async def publish(
        resource_type: str, resource_id: str, change: str, state: str
    ) -> object:
        return await live_store.publish(resource_type, resource_id, change, state)

    return publish


def build_ingest_service(settings: Settings) -> TelemetryIngestService:
    """Compose production adapters; memory adapters are intentionally test-only."""

    return build_ingest_runtime(settings)[0]


def build_run_evidence_services(settings: Settings) -> tuple[RunEvidenceIngestionService, RunInspectionService]:
    """Compose the isolated run-evidence worker/API repository boundary."""

    metrics = _production_metrics(settings)
    object_store = (
        GarageRunEvidenceObjectStore(
            settings.run_evidence_object_endpoint,
            settings.run_evidence_object_bucket,
            access_key=settings.run_evidence_object_access_key,
            secret_key=settings.run_evidence_object_secret_key,
            region=settings.run_evidence_object_region,
            timeout_seconds=settings.run_evidence_object_timeout_seconds,
        )
        if settings.run_evidence_object_access_key and settings.run_evidence_object_secret_key
        else UnavailableRunEvidenceObjectStore()
    )
    repository = PostgresRunEvidenceRepository(
        lambda: postgres_connection(settings.database_url), object_store, metrics=metrics
    )
    ingestion = RunEvidenceIngestionService(repository, metrics=metrics)
    inspection = RunInspectionService(repository, query=build_query_service(settings), metrics=metrics)
    return ingestion, inspection


def build_ingest_runtime(
    settings: Settings,
) -> tuple[TelemetryIngestService, IngestReconciliationService]:
    """Compose ingest and its bounded crash-window recovery companion."""

    metrics = _production_metrics(settings)
    catalog = PostgresCatalog(
        lambda: postgres_connection(settings.database_url), metrics=metrics
    )
    id_registry = PostgresObservationIdRegistry(
        lambda: postgres_connection(settings.database_url),
        lease_seconds=settings.observation_claim_lease_seconds,
        metrics=metrics,
    )
    observations = ClickHouseObservationStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        id_registry=id_registry,
        max_concurrency=settings.ingest_clickhouse_concurrency,
        metrics=metrics,
    )
    service = TelemetryIngestService(
        observations,
        catalog,
        credential=settings.ingest_credential(),
        max_batch_size=settings.ingest_max_batch_size,
        health_port=catalog,
        metrics=metrics,
    )
    reconciler = IngestReconciliationService(
        id_registry,
        observations,
        batch_size=settings.ingest_reconciliation_batch_size,
        timeout_seconds=settings.ingest_reconciliation_timeout_seconds,
        pass_timeout_seconds=settings.ingest_reconciliation_pass_timeout_seconds,
        health_port=catalog,
        metrics=metrics,
    )
    return service, reconciler


def build_operations_repository(settings: Settings, *, testing: bool = False, metrics: Any | None = None) -> OperationsRepository:
    """Compose the independent PostgreSQL operations boundary."""
    if testing:
        return MemoryOperationsRepository(metrics=metrics)
    return PostgresOperationsRepository(lambda: postgres_connection(settings.database_url), metrics=metrics)


def build_operations_services(
    settings: Settings,
    *,
    testing: bool = False,
    repository: OperationsRepository | None = None,
    annotation_reference_port: AnnotationReferencePort | None = None,
) -> dict[str, Any]:
    metrics = _production_metrics(settings)
    repo = repository or build_operations_repository(settings, testing=testing, metrics=metrics)
    operations = ObservatoryOperationsFacade(repo)
    query = None if testing else build_query_service(settings)
    evaluation_query = None if testing else build_evaluation_query_service(settings)
    reference_port = annotation_reference_port or CompositeAnnotationTargetReferencePort(
        run_provider=(
            _ObservatoryAnnotationReferenceProvider(query, "run")
            if query is not None
            else None
        ),
        deployment_provider=(
            _ObservatoryAnnotationReferenceProvider(query, "deployment")
            if query is not None
            else None
        ),
        experiment_provider=(
            _EvaluationQueryAnnotationReferenceProvider(evaluation_query)
            if evaluation_query is not None
            else None
        ),
        regression_provider=(
            _EvaluationQueryAnnotationReferenceProvider(evaluation_query)
            if evaluation_query is not None
            else None
        ),
        incident_provider=_OperationsAnnotationReferenceProvider(operations),
        alert_instance_provider=_OperationsAnnotationReferenceProvider(operations, "alert_instance"),
        component_health_provider=(
            _ObservatoryComponentHealthReferenceProvider(query)
            if query is not None
            else None
        ),
    )
    return {
        "metrics": metrics,
        "repository": repo,
        "operations": operations,
        "dashboard": DashboardService(operations, reference_port=operations, metrics=metrics),
        "saved_query": SavedQueryService(operations, metrics=metrics),
        "saved_query_execution": SavedQueryExecutionService(
            operations, query_port=query, evaluation_port=evaluation_query, metrics=metrics
        ),
        "alert": AlertService(operations, query_port=query, metrics=metrics),
        "incident": IncidentService(operations, metrics=metrics, reference_port=reference_port),
        "annotation": AnnotationService(operations, reference_port=reference_port, metrics=metrics),
        "live": OperationalLiveService(operations),
    }


def build_auth(settings: Settings, *, testing: bool = False) -> ObservatoryAuth:
    """Compose a dedicated Observatory session boundary.

    The memory backend is explicit test/local composition only.  Deployments
    use the separately provisioned Observatory Valkey instance and namespace.
    """

    if testing:
        return ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    redis_from_url: Any = Redis.from_url  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    client: Any = redis_from_url(
        settings.session_store_url,
        password=settings.session_store_password or None,
        decode_responses=True,
    )
    return ObservatoryAuth(
        settings,
        RedisSessionBackend(client, namespace=settings.session_namespace),
        RedisLoginStateBackend(client, namespace=settings.session_namespace),
    )


def build_query_service(settings: Settings) -> ObservatoryQueryService:
    """Compose the independent PostgreSQL/ClickHouse query boundary."""

    query_metrics = _production_metrics(settings)
    catalog = PostgresCatalog(lambda: postgres_connection(settings.database_url))
    result_reference = PostgresEvaluationResultReferencePort(
        lambda: postgres_connection(settings.database_url)
    )
    definition_repository = build_evaluation_definition_repository(settings)
    experiment_retention_port = PostgresExperimentRepository(
        lambda: postgres_connection(settings.database_url),
        definition_repository=definition_repository,
        deletion_reference=result_reference,
    )
    experiment_regression_fact_store = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    store = ProductionObservatoryStore(
        lambda: postgres_connection(settings.database_url),
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        health_port=catalog,
        experiment_projections_enabled=True,
        experiment_retention_port=experiment_retention_port,
        experiment_regression_fact_store=experiment_regression_fact_store,
        metrics=query_metrics,
    )
    return ObservatoryQueryService(
        store,
        query_port=store,
        retention_port=store,
        health_port=catalog,
        # Retention and health metrics are derived from Observatory-owned
        # PostgreSQL rows at API scrape time; no cross-container filesystem is
        # required for worker-to-API metric visibility.
        metrics=query_metrics,
    )


def build_evaluation_query_service(settings: Settings) -> EvaluationQueryService:
    """Compose the public, read-only evaluation reference boundary."""

    return EvaluationQueryService(
        backend=PostgresEvaluationQueryBackend(
            lambda: postgres_connection(settings.database_url),
            clickhouse_url=settings.clickhouse_url,
            clickhouse_database=settings.clickhouse_database,
            clickhouse_user=settings.clickhouse_user,
            clickhouse_password=settings.clickhouse_password,
        )
    )


def build_health_projector(settings: Settings) -> ObservatoryHealthProjector:
    """Compose the read-independent health projection loop."""

    catalog = PostgresCatalog(lambda: postgres_connection(settings.database_url))
    store = ProductionObservatoryStore(
        lambda: postgres_connection(settings.database_url),
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        health_port=catalog,
    )
    return ObservatoryHealthProjector(catalog, refresh=store.refresh_health_inputs)


def build_evaluation_artifact_storage(settings: Settings) -> ArtifactStorageService:
    """Compose the worker-only evaluation artifact boundary."""

    def connection_factory() -> Any:
        return postgres_connection(settings.database_url)

    result_reference = PostgresEvaluationResultReferencePort(connection_factory)
    repository = PostgresEvaluationArtifactRepository(
        connection_factory, result_reference=result_reference
    )
    metrics_store = PostgresCatalog(connection_factory)
    object_store = GarageEvaluationObjectStore(
        settings.evaluation_object_endpoint,
        settings.evaluation_object_bucket,
        access_key=settings.evaluation_object_access_key,
        secret_key=settings.evaluation_object_secret_key,
        region=settings.evaluation_object_region,
        timeout_seconds=settings.evaluation_object_timeout_seconds,
    )
    return ArtifactStorageService(
        repository,
        object_store,
        max_bytes=settings.evaluation_artifact_max_bytes,
        metrics=_production_metrics(settings, metrics_store),
        event_publisher=_evaluation_live_publisher(settings),
    )


def build_evaluation_artifact_metadata_repository(
    settings: Settings,
) -> PostgresEvaluationArtifactRepository:
    """Compose the content-free metadata port independently from byte storage."""

    def connection_factory() -> Any:
        return postgres_connection(settings.database_url)
    result_reference = PostgresEvaluationResultReferencePort(connection_factory)
    return PostgresEvaluationArtifactRepository(
        connection_factory, result_reference=result_reference
    )


def build_evaluation_deletion(settings: Settings) -> EvaluationDeletionService:
    """Compose recoverable object/score/metadata deletion for the worker."""

    def connection_factory() -> Any:
        return postgres_connection(settings.database_url)

    result_reference = PostgresEvaluationResultReferencePort(connection_factory)
    repository = PostgresEvaluationArtifactRepository(
        connection_factory, result_reference=result_reference
    )
    metrics_store = PostgresCatalog(connection_factory)
    object_store = GarageEvaluationObjectStore(
        settings.evaluation_object_endpoint,
        settings.evaluation_object_bucket,
        access_key=settings.evaluation_object_access_key,
        secret_key=settings.evaluation_object_secret_key,
        region=settings.evaluation_object_region,
        timeout_seconds=settings.evaluation_object_timeout_seconds,
    )
    score_store = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    experiment_repository = PostgresExperimentRepository(
        connection_factory, deletion_reference=repository
    )
    return EvaluationDeletionService(
        repository,
        object_store,
        score_store,
        metrics=_production_metrics(settings, metrics_store),
        event_publisher=_evaluation_live_publisher(settings),
        experiment_repository=experiment_repository,
        regression_fact_store=score_store,
    )


def build_evaluation_orchestration(
    settings: Settings,
    *,
    transport: EvaluationTransportPort | None = None,
    event_publisher: Any | None = None,
    result_scorer: Any | None = None,
) -> EvaluationOrchestrationService:
    """Compose the dedicated evaluation worker's durable orchestration port."""

    repository = PostgresEvaluationOrchestrationRepository(
        lambda: postgres_connection(settings.database_url),
        retry_base_seconds=settings.evaluation_retry_base_seconds,
        retry_max_seconds=settings.evaluation_retry_max_seconds,
    )
    metrics_store = PostgresEvaluationMetricStore(
        lambda: postgres_connection(settings.database_url)
    )
    if event_publisher is None:
        live_store = PostgresEvaluationLiveEventStore(
            lambda: postgres_connection(settings.database_url)
        )

        async def publisher(
            resource_type: str, resource_id: str, change: str, state: str
        ) -> object:
            return await live_store.publish(resource_type, resource_id, change, state)

        event_publisher = publisher

    return EvaluationOrchestrationService(
        repository,
        transport,
        metrics=_production_metrics(settings, metrics_store),
        worker_id=settings.evaluation_worker_id,
        batch_size=settings.evaluation_batch_size,
        lease_seconds=settings.evaluation_lease_seconds,
        retry_base_seconds=settings.evaluation_retry_base_seconds,
        retry_max_seconds=settings.evaluation_retry_max_seconds,
        max_attempts=settings.evaluation_max_attempts,
        event_publisher=event_publisher,
        result_scorer=result_scorer,
    )


def build_evaluation_experiment_service(
    settings: Settings, *, metrics: ObservatoryMetrics | None = None
) -> EvaluationExperimentService:
    """Compose the worker-owned experiment projection recovery boundary.

    The evaluation worker uses this narrow service during startup and between
    dispatch passes to drain PostgreSQL projection outbox rows.  It reuses the
    public result/score reference ports and never opens a Core connection or
    reads a sibling-owned table directly.
    """

    def connection_factory() -> Any:
        return postgres_connection(settings.database_url)

    result_reference = PostgresEvaluationResultReferencePort(connection_factory)
    definition_repository = build_evaluation_definition_repository(settings)
    score_reference = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    shared_metrics = metrics or _production_metrics(settings)
    repository = PostgresExperimentRepository(
        connection_factory,
        definition_repository=definition_repository,
        clickhouse_url=settings.clickhouse_url,
        clickhouse_database=settings.clickhouse_database,
        clickhouse_user=settings.clickhouse_user,
        clickhouse_password=settings.clickhouse_password,
        metrics=shared_metrics,
        result_reference=result_reference,
        score_reference=score_reference,
    )
    return EvaluationExperimentService(
        repository,
        evidence=repository.evidence,
        references=repository.references,
        metrics=shared_metrics,
    )


def build_evaluation_scheduler_repository(
    settings: Settings,
) -> PostgresEvaluationOrchestrationRepository:
    """Compose a DB-only scheduler repository without broker credentials."""

    return PostgresEvaluationOrchestrationRepository(
        lambda: postgres_connection(settings.database_url),
        retry_base_seconds=settings.evaluation_retry_base_seconds,
        retry_max_seconds=settings.evaluation_retry_max_seconds,
    )


def build_evaluation_scheduler(settings: Settings) -> EvaluationSchedulerService:
    """Compose the DB-only application scheduler without broker credentials."""

    metrics_store = PostgresEvaluationMetricStore(
        lambda: postgres_connection(settings.database_url)
    )
    return EvaluationSchedulerService(
        build_evaluation_scheduler_repository(settings),
        metrics=_production_metrics(settings, metrics_store),
        worker_id=settings.evaluation_scheduler_id,
        batch_size=settings.evaluation_batch_size,
    )


def build_evaluation_definition_repository(
    settings: Settings,
) -> PostgresEvaluationDefinitionRepository:
    """Compose the immutable Observatory definition repository."""

    metrics_store = PostgresCatalog(lambda: postgres_connection(settings.database_url))
    return PostgresEvaluationDefinitionRepository(
        lambda: postgres_connection(settings.database_url),
        metrics=_production_metrics(settings, metrics_store),
    )


def build_evaluation_definition_service(settings: Settings) -> EvaluationDefinitionService:
    """Compose definition admission with the closed evaluator compatibility port."""

    return EvaluationDefinitionService(
        build_evaluation_definition_repository(settings),
        artifact_repository=PostgresEvaluationArtifactRepository(
            lambda: postgres_connection(settings.database_url)
        ),
        metrics=_production_metrics(settings),
        evaluator_compatibility=_evaluation_definition_compatibility,
    )


def build_evaluation_application_service(settings: Settings) -> EvaluationApplicationService:
    """Compose the typed definition-to-score application boundary."""

    return EvaluationApplicationService(
        build_evaluation_definition_repository(settings),
        ClickHouseEvaluationScoreStore(
            settings.clickhouse_url,
            database=settings.clickhouse_database,
            user=settings.clickhouse_user,
            password=settings.clickhouse_password,
            metrics=_production_metrics(settings),
        ),
        registry=DeterministicEvaluatorRegistry(),
        metrics=_production_metrics(settings),
    )


def build_local_judge_service(
    settings: Settings, *, metrics: ObservatoryMetrics | None = None
) -> LocalJudgeService:
    """Compose the worker-only local judge with a separate read identity."""

    artifact_repository = PostgresEvaluationArtifactRepository(
        lambda: postgres_connection(settings.database_url)
    )
    object_store = GarageEvaluationObjectStore(
        settings.evaluation_object_endpoint,
        settings.evaluation_object_bucket,
        access_key=settings.judge_object_access_key,
        secret_key=settings.judge_object_secret_key,
        region=settings.evaluation_object_region,
        timeout_seconds=settings.evaluation_object_timeout_seconds,
    )
    shared_metrics = metrics or _production_metrics(settings)
    score_store = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        metrics=shared_metrics,
        component_id="aura.observatory.local_judges",
    )
    definition_repository = PostgresJudgeDefinitionRepository(
        lambda: postgres_connection(settings.database_url)
    )
    qualification_repository = PostgresCalibrationRepository(
        lambda: postgres_connection(settings.database_url)
    )

    async def resolve_qualification(**scope: str) -> object | None:
        return await qualification_repository.active_qualification(
            suite_id=scope["suite_id"],
            suite_version=scope["suite_version"],
            dataset_id=scope["dataset_id"],
            dataset_version=scope["dataset_version"],
            evaluator_id=scope["evaluator_id"],
            evaluator_version=scope["evaluator_version"],
            judge_id=scope["judge_id"],
            judge_version=scope["judge_version"],
            model_name=scope["model_name"],
            model_version=scope["model_version"],
            model_digest=scope["model_digest"],
            instruction_version=scope["prompt_version"],
            prompt_artifact_id=scope["prompt_artifact_id"],
            prompt_artifact_version=scope["prompt_artifact_version"],
            prompt_artifact_digest=scope["prompt_artifact_digest"],
            rubric_version=scope["rubric_version"],
            rubric_artifact_id=scope["rubric_artifact_id"],
            rubric_artifact_version=scope["rubric_artifact_version"],
            rubric_artifact_digest=scope["rubric_artifact_digest"],
            calibration_version=scope["calibration_version"],
        )

    async def resolve_calibration_result(result_id: str) -> object | None:
        return await qualification_repository.get_calibration_result(result_id)

    execution_repository = PostgresJudgeExecutionRepository(
        lambda: postgres_connection(settings.database_url)
    )
    return LocalJudgeService(
        LocalOllamaAdapter(
            settings.local_judge_origin,
            timeout_seconds=settings.local_judge_timeout_seconds,
        ),
        AuthorizedArtifactReader(
            artifact_repository,
            object_store,
            metrics=shared_metrics,
        ),
        definition_repository=definition_repository,
        execution_repository=execution_repository,
        score_store=score_store,
        metrics=shared_metrics,
        max_concurrency=settings.local_judge_max_concurrency,
        qualification_resolver=resolve_qualification,
        calibration_result_resolver=resolve_calibration_result,
    )


def build_calibration_service(
    settings: Settings, *, metrics: ObservatoryMetrics | None = None
) -> CalibrationService:
    """Compose durable PostgreSQL-backed calibration state for the worker."""

    return CalibrationService(
        PostgresCalibrationRepository(lambda: postgres_connection(settings.database_url)),
        metrics=metrics or _production_metrics(settings),
        event_publisher=_evaluation_live_publisher(settings),
    )


def build_evaluation_api_service(settings: Settings) -> EvaluationApiApplicationService:
    """Compose the owner API over existing Observatory application ports.

    The API never constructs Core adapters or opens provider connections at
    import time.  Each supplied service retains its own service-owned storage
    boundary; the transport projection remains in ``evaluation_api``.
    """

    api_metrics = _production_metrics(settings)
    query = EvaluationQueryService(
        backend=PostgresEvaluationQueryBackend(
            lambda: postgres_connection(settings.database_url),
            clickhouse_url=settings.clickhouse_url,
            clickhouse_database=settings.clickhouse_database,
            clickhouse_user=settings.clickhouse_user,
            clickhouse_password=settings.clickhouse_password,
        )
    )
    live_store = PostgresEvaluationLiveEventStore(
        lambda: postgres_connection(settings.database_url)
    )

    async def publish_live(
        resource_type: str, resource_id: str, change: str, state: str
    ) -> object:
        return await live_store.publish(resource_type, resource_id, change, state)

    deletion_service = None
    artifact_storage = None
    artifact_metadata = build_evaluation_artifact_metadata_repository(settings)
    if settings.evaluation_object_access_key and settings.evaluation_object_secret_key:
        artifact_storage = build_evaluation_artifact_storage(settings)
        deletion_service = build_evaluation_deletion(settings)
    calibration_service = build_calibration_service(settings)
    experiment_definition_repository = build_evaluation_definition_repository(settings)
    experiment_catalog = PostgresCatalog(lambda: postgres_connection(settings.database_url))
    experiment_result_reference = PostgresEvaluationResultReferencePort(
        lambda: postgres_connection(settings.database_url)
    )
    experiment_score_reference = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    definition_service = EvaluationDefinitionService(
        experiment_definition_repository,
        artifact_repository=PostgresEvaluationArtifactRepository(
            lambda: postgres_connection(settings.database_url)
        ),
        metrics=api_metrics,
        evaluator_compatibility=_evaluation_definition_compatibility,
    )
    return EvaluationApiApplicationService(
        query=query,
        live=live_store,
        definition_service=definition_service,
        artifact_storage=artifact_storage,
        artifact_metadata=artifact_metadata,
        definition_commands=definition_service,
        judge_definitions=PostgresJudgeDefinitionRepository(
            lambda: postgres_connection(settings.database_url)
        ),
        orchestration=build_evaluation_orchestration(settings, event_publisher=publish_live),
        calibration_service=calibration_service,
        deletion_service=deletion_service,
        metrics=api_metrics,
        experiment_service=EvaluationExperimentService(
            (experiment_repository := PostgresExperimentRepository(
                lambda: postgres_connection(settings.database_url),
                clickhouse_url=settings.clickhouse_url,
                clickhouse_database=settings.clickhouse_database,
                clickhouse_user=settings.clickhouse_user,
                clickhouse_password=settings.clickhouse_password,
                metrics=api_metrics,
                definition_repository=experiment_definition_repository,
                catalog=experiment_catalog,
                query_port=cast(ObservatoryQueryPort, build_query_service(settings).query_port),
                result_reference=experiment_result_reference,
                score_reference=experiment_score_reference,
            )),
            evidence=experiment_repository.evidence,
            references=experiment_repository.references,
            metrics=api_metrics,
        ),
    )


__all__ = [
    "CompositeAnnotationTargetReferencePort",
    "ObservatoryOperationsFacade",
    "build_auth",
    "build_health_projector",
    "build_ingest_service",
    "build_ingest_runtime",
    "build_query_service",
    "build_evaluation_query_service",
    "build_evaluation_artifact_storage",
    "build_evaluation_deletion",
    "build_evaluation_orchestration",
    "build_evaluation_experiment_service",
    "build_evaluation_scheduler_repository",
    "build_evaluation_scheduler",
    "build_evaluation_definition_repository",
    "build_evaluation_definition_service",
    "build_evaluation_application_service",
    "build_local_judge_service",
    "build_calibration_service",
    "build_evaluation_api_service",
    "postgres_connection",
]
