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

    def _forget_sequences(self, guarded: tuple[Any, Any]) -> None:
        for sequence, candidate in list(self._sequence_guards.items()):
            if candidate is guarded:
                self._sequence_guards.pop(sequence, None)

    async def _mark_ingest_visible_on(self, connection: Any, sequence: int) -> None:
        await connection.execute(
            "UPDATE observatory_ingest_visibility SET visible = TRUE "
            "WHERE sequence = %s",
            (sequence,),
        )
        result = await connection.execute(
            "SELECT committed_sequence FROM observatory_ingest_watermark "
            "WHERE watermark_key = TRUE FOR UPDATE"
        )
        row = await result.fetchone()
        current = int(row[0]) if row else 0
        while True:
            candidate = current + 1
            visible = await connection.execute(
                "SELECT visible FROM observatory_ingest_visibility WHERE sequence = %s",
                (candidate,),
            )
            visible_row = await visible.fetchone()
            if not visible_row or not visible_row[0]:
                break
            current = candidate
        await connection.execute(
            "UPDATE observatory_ingest_watermark SET committed_sequence = %s "
            "WHERE watermark_key = TRUE",
            (current,),
        )


async def _commit(connection: Any) -> None:
    commit = getattr(connection, "commit", None)
    if commit is not None:
        await commit()


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
        await context.__aexit__(
            type(cleanup_error), cleanup_error, cleanup_error.__traceback__
        )
        raise cleanup_error
    await context.__aexit__(exc_type, exc, traceback)


__all__ = ["PostgresCatalog", "PostgresObservationIdRegistry"]
