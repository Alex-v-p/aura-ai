"""Production composition for the independent Observatory service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from redis.asyncio import Redis

from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.evaluation_storage import (
    ClickHouseEvaluationScoreStore,
    PostgresEvaluationArtifactRepository,
)
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
from aura_observatory.evaluation_storage import (
    ArtifactStorageService,
    EvaluationDeletionService,
)
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.query import ObservatoryHealthProjector, ObservatoryQueryService
from aura_observatory.settings import Settings


def _production_metrics(settings: Settings, persistence: Any | None = None) -> ObservatoryMetrics:
    """Use the established bounded sink and optional cross-process export."""

    return ObservatoryMetrics(settings.metrics_file, persistence=persistence)


@asynccontextmanager
async def postgres_connection(database_url: str) -> AsyncGenerator[Any]:
    """Open one Observatory-owned PostgreSQL connection for a catalog operation."""

    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        yield connection


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
    store = ProductionObservatoryStore(
        lambda: postgres_connection(settings.database_url),
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
        health_port=catalog,
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

    repository = PostgresEvaluationArtifactRepository(connection_factory)
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
    )


def build_evaluation_deletion(settings: Settings) -> EvaluationDeletionService:
    """Compose recoverable object/score/metadata deletion for the worker."""

    def connection_factory() -> Any:
        return postgres_connection(settings.database_url)

    repository = PostgresEvaluationArtifactRepository(connection_factory)
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
    return EvaluationDeletionService(
        repository, object_store, score_store, metrics=_production_metrics(settings, metrics_store)
    )


__all__ = [
    "build_auth",
    "build_health_projector",
    "build_ingest_service",
    "build_query_service",
    "build_evaluation_artifact_storage",
    "build_evaluation_deletion",
    "postgres_connection",
]
