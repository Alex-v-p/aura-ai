"""Production composition for the independent Observatory service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg

from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.postgres import PostgresCatalog, PostgresObservationIdRegistry
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
        id_registry=id_registry,
    )
    return TelemetryIngestService(
        observations,
        catalog,
        credential=settings.ingest_credential(),
        max_batch_size=settings.ingest_max_batch_size,
    )


__all__ = ["build_ingest_service", "postgres_connection"]
