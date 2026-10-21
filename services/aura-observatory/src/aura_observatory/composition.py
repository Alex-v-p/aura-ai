"""Production composition for the independent Observatory service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

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
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
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
from aura_observatory.evaluation_definitions import EvaluationDefinitionService
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
    EvaluationQueryService,
)
from aura_observatory.evaluation_storage import (
    ArtifactStorageService,
    EvaluationDeletionService,
)
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import EvaluationTransportPort, ObservatoryQueryPort
from aura_observatory.query import ObservatoryHealthProjector, ObservatoryQueryService
from aura_observatory.settings import Settings


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

    catalog = PostgresCatalog(lambda: postgres_connection(settings.database_url))
    id_registry = PostgresObservationIdRegistry(
        lambda: postgres_connection(settings.database_url),
        lease_seconds=settings.observation_claim_lease_seconds,
    )
    observations = ClickHouseObservationStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        id_registry=id_registry,
    )
    return TelemetryIngestService(
        observations,
        catalog,
        credential=settings.ingest_credential(),
        max_batch_size=settings.ingest_max_batch_size,
        health_port=catalog,
        metrics=_production_metrics(settings),
    )


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
    )
    return ObservatoryQueryService(
        store,
        query_port=store,
        retention_port=store,
        health_port=catalog,
        # Retention and health metrics are derived from Observatory-owned
        # PostgreSQL rows at API scrape time; no cross-container filesystem is
        # required for worker-to-API metric visibility.
        metrics=_production_metrics(settings),
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
        evaluator_compatibility=DeterministicEvaluatorRegistry().validate_definition,
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
    if settings.evaluation_object_access_key and settings.evaluation_object_secret_key:
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
    return EvaluationApiApplicationService(
        query=query,
        live=live_store,
        definition_service=EvaluationDefinitionService(
            experiment_definition_repository,
            artifact_repository=PostgresEvaluationArtifactRepository(
                lambda: postgres_connection(settings.database_url)
            ),
            metrics=api_metrics,
            evaluator_compatibility=DeterministicEvaluatorRegistry().validate_definition,
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
    "build_auth",
    "build_health_projector",
    "build_ingest_service",
    "build_query_service",
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
