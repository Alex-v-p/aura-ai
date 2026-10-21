"""Durable PostgreSQL evaluation live-event adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from uuid import uuid4

from aura_observatory.evaluation_live import EvaluationLiveEvent
from aura_observatory.evaluation_query import EvaluationUnavailable


def _provider_boundary(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except EvaluationUnavailable:
            raise
        except (OSError, TimeoutError) as exc:
            raise EvaluationUnavailable("evaluation live storage is unavailable") from exc
        except Exception as exc:
            # Keep driver-specific diagnostics out of the SSE surface.
            raise EvaluationUnavailable("evaluation live storage is unavailable") from exc

    return guarded


class PostgresEvaluationLiveEventStore:
    """Restart-safe metadata event ledger for the production API."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    @_provider_boundary
    async def publish(
        self, resource_type: str, resource_id: str, change: str, state: str,
        resource_version: str | None = None,
    ) -> EvaluationLiveEvent:
        event_id = str(uuid4())
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """INSERT INTO observatory_evaluation_live_events(
                event_id, resource_type, resource_id, resource_version, change, state)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING event_id, resource_type, resource_id, resource_version, change, state,
                          occurred_at""",
                (event_id, resource_type, resource_id, resource_version, change, state),
            )
            row = await result.fetchone()
        if row is None:
            raise RuntimeError("evaluation live event persistence failed")
        return _event_from_row(row)

    @_provider_boundary
    async def after(
        self, last_event_id: str | None, *, limit: int = 1000
    ) -> Sequence[EvaluationLiveEvent]:
        bounded_limit = max(1, min(1000, int(limit)))
        params: tuple[object, ...] = ()
        if last_event_id is None:
            where = ""
            params = (bounded_limit,)
        else:
            where = (
                " WHERE sequence > COALESCE((SELECT sequence FROM "
                "observatory_evaluation_live_events WHERE event_id = %s), 0)"
            )
            params = (last_event_id, bounded_limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT event_id, resource_type, resource_id, resource_version, change, state, "
                "occurred_at "
                "FROM observatory_evaluation_live_events"
                + where
                + " ORDER BY sequence ASC LIMIT %s",
                params,
            )
            rows = await result.fetchall()
        return tuple(_event_from_row(row) for row in rows)

    @_provider_boundary
    async def wait(self, timeout: float = 15.0) -> None:
        # PostgreSQL LISTEN is intentionally not required for correctness. A
        # short bounded poll works across API processes and survives restarts.
        await asyncio.sleep(max(0.0, min(float(timeout), 15.0)))


def _event_from_row(row: Any) -> EvaluationLiveEvent:
    legacy = len(row) == 6
    occurred = row[5] if legacy else row[6]
    if isinstance(occurred, datetime):
        occurred_at = occurred.astimezone(UTC).isoformat().replace("+00:00", "Z")
    else:
        occurred_at = str(occurred)
    return EvaluationLiveEvent(
        event_id=str(row[0]),
        resource_type=str(row[1]),
        resource_id=str(row[2]),
        change=str(row[3] if legacy else row[4]),
        state=str(row[4] if legacy else row[5]),
        occurred_at=occurred_at,
        resource_version=None if legacy else (str(row[3]) if row[3] is not None else None),
    )

__all__ = ["PostgresEvaluationLiveEventStore"]
