"""Small PostgreSQL adapter for Observatory transactional catalogues.

The adapter receives a connection factory rather than importing Core's
database module.  It owns only Observatory table names and never exposes a
general-purpose query port.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from aura_observatory.ports import ClaimOutcome


class AsyncConnection(Protocol):
    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> Any: ...


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

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                """
                INSERT INTO observatory_deployments(deployment_id, git_commit, environment, host_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (deployment_id, git_commit, environment, host_id) DO NOTHING
                """,
                (deployment_id, git_commit, environment, host_id),
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
        async with self._connection_factory() as connection:
            await connection.execute(
                f"""INSERT INTO observatory_ingest_counters(counter_key, {column})
                VALUES ('global', %s)
                ON CONFLICT (counter_key) DO UPDATE SET {column} =
                observatory_ingest_counters.{column} + EXCLUDED.{column},
                last_ingest_at = CURRENT_TIMESTAMP""",
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


class PostgresObservationIdRegistry:
    """Recoverable pending/completed stable-ID state in Observatory PostgreSQL."""

    def __init__(self, connection_factory: Any, *, lease_seconds: int = 60) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._connection_factory = connection_factory
        self._lease_seconds = lease_seconds

    async def claim(self, observation_id: str) -> ClaimOutcome:
        async with self._connection_factory() as connection:
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
                return ClaimOutcome.ACQUIRED
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
                return ClaimOutcome.DUPLICATE
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
            return (
                ClaimOutcome.ACQUIRED
                if (await stale.fetchone()) is not None
                else ClaimOutcome.PENDING
            )

    async def complete(self, observation_id: str) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                """
                UPDATE observatory_observation_receipts
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                WHERE observation_id = %s AND status = 'pending'
                """,
                (observation_id,),
            )


__all__ = ["PostgresCatalog", "PostgresObservationIdRegistry"]
