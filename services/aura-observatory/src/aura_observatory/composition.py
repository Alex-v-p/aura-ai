"""Production composition for the independent Observatory service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from redis.asyncio import Redis

from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    RedisLoginStateBackend,
    RedisSessionBackend,
)
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.settings import Settings


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


__all__ = ["build_auth", "build_ingest_service", "postgres_connection"]
