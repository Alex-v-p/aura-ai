"""Content-free attempt, lease, cancellation, and publication ledgers."""

# ruff: noqa: E501
# SQL/JSON-compatible update dictionaries are narrowed by the ledger state
# machine before persistence.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportIndexIssue=false, reportGeneralTypeIssues=false, reportReturnType=false

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _request_deadline(projection: Mapping[str, object] | None) -> datetime | None:
    if projection is None:
        return None
    data = projection.get("data")
    if not isinstance(data, Mapping):
        return None
    value = data.get("deadlineAt")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: UUID
    request_id: UUID
    job_id: UUID
    idempotency_key: UUID
    request_digest: str
    status: str = "queued"
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    cancellation_id: UUID | None = None
    cancellation_reason: str | None = None
    result_id: UUID | None = None
    result_digest: str | None = None
    terminal_message_id: UUID | None = None
    publication_status: str = "pending"
    business_accounted: bool = False
    failure_code: str | None = None
    created_at: datetime = datetime.min.replace(tzinfo=UTC)
    updated_at: datetime = datetime.min.replace(tzinfo=UTC)
    completed_at: datetime | None = None
    execution_started_at: datetime | None = None
    attempt_count: int = 0
    lease_generation: UUID | None = None
    request_projection: Mapping[str, object] | None = None
    terminal_payload: Mapping[str, object] | None = None


class AttemptConflict(RuntimeError):
    """An idempotency or terminal-state identity conflict."""


@dataclass(frozen=True, slots=True)
class CancellationTombstone:
    cancellation_id: UUID
    request_id: UUID
    job_id: UUID
    idempotency_key: UUID
    request_digest: str
    deadline_at: datetime
    reason: str


class EvaluationRunnerLedger(Protocol):
    async def receive(self, attempt: AttemptRecord) -> AttemptRecord: ...

    async def claim(
        self,
        attempt_id: UUID,
        owner: str,
        lease: timedelta,
        *,
        max_attempts: int | None = None,
    ) -> AttemptRecord | None: ...

    async def renew(
        self, attempt_id: UUID, owner: str, lease_generation: UUID, lease: timedelta
    ) -> AttemptRecord | None: ...

    async def cancel(
        self,
        attempt_id: UUID,
        cancellation_id: UUID,
        reason: str,
        *,
        request_id: UUID,
        job_id: UUID,
        idempotency_key: UUID,
        request_digest: str,
        deadline_at: datetime,
    ) -> AttemptRecord | None: ...

    async def terminal(
        self,
        attempt_id: UUID,
        update: Mapping[str, object],
        *,
        lease_owner: str | None = None,
        lease_generation: UUID | None = None,
    ) -> AttemptRecord: ...

    async def mark_published(self, attempt_id: UUID) -> AttemptRecord: ...

    async def account_terminal(self, attempt_id: UUID) -> bool: ...

    async def get(self, attempt_id: UUID) -> AttemptRecord | None: ...

    async def pending_publications(self, limit: int = 64) -> list[AttemptRecord]: ...

    async def recoverable_attempts(self, limit: int = 64) -> list[AttemptRecord]: ...


class InMemoryEvaluationRunnerLedger:
    """Deterministic ledger used by the runner and unit tests.

    The production adapter can use the same protocol with the migration's
    table; this implementation intentionally models compare-and-set behavior
    so cancellation and redelivery races are tested without a database.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[UUID, AttemptRecord] = {}
        self._by_idempotency: dict[UUID, UUID] = {}
        self._pending_cancellations: dict[UUID, CancellationTombstone] = {}
        self._lock = asyncio.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def receive(self, attempt: AttemptRecord) -> AttemptRecord:
        now = self._clock()
        async with self._lock:
            existing_id = self._by_idempotency.get(attempt.idempotency_key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if (
                    existing.attempt_id != attempt.attempt_id
                    or existing.request_digest != attempt.request_digest
                    or existing.request_id != attempt.request_id
                    or existing.job_id != attempt.job_id
                    or existing.idempotency_key != attempt.idempotency_key
                ):
                    raise AttemptConflict("idempotency identity conflict")
                return existing
            if attempt.attempt_id in self._records:
                existing = self._records[attempt.attempt_id]
                if (
                    existing.request_id != attempt.request_id
                    or existing.job_id != attempt.job_id
                    or existing.idempotency_key != attempt.idempotency_key
                    or existing.request_digest != attempt.request_digest
                ):
                    raise AttemptConflict("attempt identity conflict")
                return existing
            record = replace(attempt, created_at=now, updated_at=now)
            pending = self._pending_cancellations.pop(record.attempt_id, None)
            if pending is not None:
                if (
                    pending.request_id != record.request_id
                    or pending.job_id != record.job_id
                    or pending.idempotency_key != record.idempotency_key
                    or pending.request_digest != record.request_digest
                    or pending.deadline_at
                    != _request_deadline(record.request_projection)
                ):
                    raise AttemptConflict("cancellation identity conflict")
                record = replace(
                    record,
                    status="cancelled",
                    cancellation_id=pending.cancellation_id,
                    cancellation_reason=pending.reason,
                    completed_at=now,
                )
            self._records[record.attempt_id] = record
            self._by_idempotency[record.idempotency_key] = record.attempt_id
            return record

    async def claim(
        self,
        attempt_id: UUID,
        owner: str,
        lease: timedelta,
        *,
        max_attempts: int | None = None,
    ) -> AttemptRecord | None:
        if not owner or lease.total_seconds() <= 0:
            raise ValueError("lease owner and duration are required")
        now = self._clock()
        async with self._lock:
            record = self._records.get(attempt_id)
            if record is None or record.status in {"completed", "cancelled", "failed"}:
                return None
            if max_attempts is not None and record.attempt_count >= max_attempts:
                return None
            if (
                record.status == "leased"
                and record.lease_expires_at
                and record.lease_expires_at > now
            ):
                return None
            updated = replace(
                record,
                status="leased",
                lease_owner=owner,
                lease_expires_at=now + lease,
                lease_generation=uuid4(),
                attempt_count=record.attempt_count + 1,
                updated_at=now,
            )
            self._records[attempt_id] = updated
            return updated

    async def renew(
        self, attempt_id: UUID, owner: str, lease_generation: UUID, lease: timedelta
    ) -> AttemptRecord | None:
        now = self._clock()
        async with self._lock:
            record = self._records.get(attempt_id)
            if (
                record is None
                or record.status != "leased"
                or record.lease_owner != owner
                or record.lease_generation != lease_generation
                or record.lease_expires_at is None
                or record.lease_expires_at <= now
            ):
                return None
            updated = replace(record, lease_expires_at=now + lease, updated_at=now)
            self._records[attempt_id] = updated
            return updated

    async def cancel(
        self,
        attempt_id: UUID,
        cancellation_id: UUID,
        reason: str,
        *,
        request_id: UUID,
        job_id: UUID,
        idempotency_key: UUID,
        request_digest: str,
        deadline_at: datetime,
    ) -> AttemptRecord | None:
        if reason not in {"OWNER_REQUESTED", "SUPERSEDED", "DEADLINE_EXCEEDED", "SHUTDOWN"}:
            raise ValueError("invalid cancellation reason")
        now = self._clock()
        async with self._lock:
            record = self._records.get(attempt_id)
            if record is None:
                existing = self._pending_cancellations.get(attempt_id)
                tombstone = CancellationTombstone(
                    cancellation_id,
                    request_id,
                    job_id,
                    idempotency_key,
                    request_digest,
                    deadline_at,
                    reason,
                )
                if existing is not None and existing != tombstone:
                    raise AttemptConflict("cancellation identity conflict")
                self._pending_cancellations[attempt_id] = tombstone
                return None
            if (
                record.request_id != request_id
                or record.job_id != job_id
                or record.idempotency_key != idempotency_key
                or record.request_digest != request_digest
                or deadline_at != _request_deadline(record.request_projection)
            ):
                raise AttemptConflict("cancellation identity conflict")
            if record.status in {"completed", "cancelled", "failed"}:
                return record
            updated = replace(
                record,
                status="cancelled",
                cancellation_id=cancellation_id,
                cancellation_reason=reason,
                updated_at=now,
                completed_at=now,
            )
            self._records[attempt_id] = updated
            return updated

    async def terminal(
        self,
        attempt_id: UUID,
        update: Mapping[str, object],
        *,
        lease_owner: str | None = None,
        lease_generation: UUID | None = None,
    ) -> AttemptRecord:
        now = self._clock()
        async with self._lock:
            record = self._records.get(attempt_id)
            if record is None:
                raise AttemptConflict("unknown attempt")
            status = update.get("status")
            if status not in {"completed", "cancelled", "failed"}:
                raise AttemptConflict("invalid terminal status")
            if record.status in {"completed", "cancelled", "failed"}:
                # Terminal identities must remain deterministic and duplicate
                # deliveries must return the original record.
                if record.status != status or (
                    record.result_id is not None and record.result_id != update.get("result_id")
                ):
                    raise AttemptConflict("terminal state conflict")
                if record.result_id is None:
                    updated = replace(
                        record,
                        result_id=update.get("result_id"),
                        result_digest=update.get("result_digest"),
                        terminal_message_id=update.get("terminal_message_id"),
                        failure_code=update.get("failure_code"),
                        terminal_payload=update.get("terminal_payload"),
                        updated_at=now,
                    )
                    self._records[attempt_id] = updated
                    return updated
                return record
            if lease_owner is not None and (
                record.lease_owner != lease_owner
                or lease_generation is None
                or record.lease_generation != lease_generation
            ):
                raise AttemptConflict("lease ownership changed")
            fields = {
                "status": status,
                "result_id": update.get("result_id"),
                "result_digest": update.get("result_digest"),
                "terminal_message_id": update.get("terminal_message_id"),
                "failure_code": update.get("failure_code"),
                "updated_at": now,
                "completed_at": now,
                "terminal_payload": update.get("terminal_payload"),
            }
            if "execution_started_at" in update:
                fields["execution_started_at"] = update["execution_started_at"]
            updated = replace(record, **fields)
            self._records[attempt_id] = updated
            return updated

    async def mark_published(self, attempt_id: UUID) -> AttemptRecord:
        async with self._lock:
            record = self._records.get(attempt_id)
            if record is None:
                raise AttemptConflict("unknown attempt")
            updated = replace(record, publication_status="published", updated_at=self._clock())
            self._records[attempt_id] = updated
            return updated

    async def account_terminal(self, attempt_id: UUID) -> bool:
        async with self._lock:
            record = self._records.get(attempt_id)
            if record is None or record.status not in {"completed", "cancelled", "failed"}:
                raise AttemptConflict("unknown terminal state")
            if record.business_accounted:
                return False
            self._records[attempt_id] = replace(
                record, business_accounted=True, updated_at=self._clock()
            )
            return True

    async def get(self, attempt_id: UUID) -> AttemptRecord | None:
        async with self._lock:
            return self._records.get(attempt_id)

    async def pending_publications(self, limit: int = 64) -> list[AttemptRecord]:
        if not 1 <= limit <= 1024:
            raise ValueError("publication batch size is invalid")
        async with self._lock:
            return [
                record
                for record in self._records.values()
                if record.publication_status == "pending"
                and record.terminal_payload is not None
                and record.status in {"completed", "cancelled", "failed"}
            ][:limit]

    async def recoverable_attempts(self, limit: int = 64) -> list[AttemptRecord]:
        if not 1 <= limit <= 1024:
            raise ValueError("recovery batch size is invalid")
        now = self._clock()
        async with self._lock:
            return [
                record
                for record in self._records.values()
                if record.status == "queued"
                or (
                    record.status == "leased"
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now
                )
            ][:limit]


class SqlEvaluationRunnerLedger:
    """PostgreSQL adapter for the runner's own content-free state table."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    async def _lock_attempt(session: AsyncSession, attempt_id: UUID) -> None:
        """Serialize receipt and cancellation transactions for one attempt.

        PostgreSQL advisory transaction locks work before either side has a
        row to lock.  This closes the READ COMMITTED gap between the attempt
        table and the pre-receipt cancellation tombstone table while keeping
        the lock key content-free and bounded to this ledger.
        """

        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(CAST(:attempt_id AS TEXT), 7310091))"
            ),
            {"attempt_id": attempt_id},
        )

    async def receive(self, attempt: AttemptRecord) -> AttemptRecord:
        async with self._sessions() as session, session.begin():
            await self._lock_attempt(session, attempt.attempt_id)
            await session.execute(
                text(
                    """
                    INSERT INTO evaluation_runner_attempts
                        (attempt_id, request_id, job_id, idempotency_key, request_digest, request_projection, status)
                    VALUES (:attempt_id, :request_id, :job_id, :idempotency_key, :request_digest, CAST(:request_projection AS JSONB), 'queued')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "attempt_id": attempt.attempt_id,
                    "request_id": attempt.request_id,
                    "job_id": attempt.job_id,
                    "idempotency_key": attempt.idempotency_key,
                    "request_digest": attempt.request_digest,
                    "request_projection": json.dumps(attempt.request_projection),
                },
            )
            row = (
                await session.execute(
                    text("SELECT * FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                    {"attempt_id": attempt.attempt_id},
                )
            ).mappings().first()
            if row is None:
                row = (
                    await session.execute(
                        text("SELECT * FROM evaluation_runner_attempts WHERE idempotency_key = :key"),
                        {"key": attempt.idempotency_key},
                    )
                ).mappings().first()
            if row is None:
                raise AttemptConflict("attempt receipt was not persisted")
            record = self._record(row)
            cancellation = (
                (
                    await session.execute(
                        text(
                            "SELECT cancellation_id, request_id, job_id, idempotency_key, "
                            "request_digest, deadline_at, reason FROM evaluation_runner_cancellations "
                            "WHERE attempt_id = :attempt_id"
                        ),
                        {"attempt_id": attempt.attempt_id},
                    )
                )
                .mappings()
                .first()
            )
            if cancellation and (
                cancellation["request_id"] != record.request_id
                or cancellation["job_id"] != record.job_id
                or cancellation["idempotency_key"] != record.idempotency_key
                or cancellation["request_digest"] != record.request_digest
                or cancellation["deadline_at"] != _request_deadline(record.request_projection)
            ):
                raise AttemptConflict("cancellation identity conflict")
            if cancellation and record.status not in {"completed", "cancelled", "failed"}:
                await session.execute(
                    text(
                        "UPDATE evaluation_runner_attempts SET status = 'cancelled', "
                        "cancellation_id = :cancellation_id, cancellation_reason = :reason, "
                        "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                        "WHERE attempt_id = :attempt_id"
                    ),
                    {
                        "attempt_id": attempt.attempt_id,
                        "cancellation_id": cancellation["cancellation_id"],
                        "reason": cancellation["reason"],
                    },
                )
                row = (
                    await session.execute(
                        text("SELECT * FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                        {"attempt_id": attempt.attempt_id},
                    )
                ).mappings().one()
                record = self._record(row)
            if (
                record.attempt_id != attempt.attempt_id
                or record.request_id != attempt.request_id
                or record.job_id != attempt.job_id
                or record.idempotency_key != attempt.idempotency_key
                or record.request_digest != attempt.request_digest
            ):
                raise AttemptConflict("idempotency identity conflict")
            return record

    async def claim(
        self,
        attempt_id: UUID,
        owner: str,
        lease: timedelta,
        *,
        max_attempts: int | None = None,
    ) -> AttemptRecord | None:
        async with self._sessions() as session, session.begin():
            await self._lock_attempt(session, attempt_id)
            row = (
                (
                    await session.execute(
                        text(
                    """
                        UPDATE evaluation_runner_attempts
                        SET status = 'leased', lease_owner = :owner,
                            lease_generation = :lease_generation,
                            lease_expires_at = CURRENT_TIMESTAMP + (:seconds * INTERVAL '1 second'),
                            attempt_count = attempt_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE attempt_id = :attempt_id
                          AND status IN ('queued', 'leased')
                          AND (status = 'queued' OR lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                          AND (CAST(:max_attempts AS INTEGER) IS NULL
                               OR attempt_count < CAST(:max_attempts AS INTEGER))
                        RETURNING *
                        """
                        ),
                        {
                            "attempt_id": attempt_id,
                            "owner": owner,
                            "lease_generation": uuid4(),
                            "seconds": lease.total_seconds(),
                            "max_attempts": max_attempts,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row:
                return self._record(row)
            return None

    async def renew(
        self, attempt_id: UUID, owner: str, lease_generation: UUID, lease: timedelta
    ) -> AttemptRecord | None:
        async with self._sessions() as session, session.begin():
            await self._lock_attempt(session, attempt_id)
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE evaluation_runner_attempts
                        SET lease_expires_at = CURRENT_TIMESTAMP + (:seconds * INTERVAL '1 second'),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE attempt_id = :attempt_id AND status = 'leased'
                          AND lease_owner = :owner AND lease_generation = :lease_generation
                          AND lease_expires_at > CURRENT_TIMESTAMP
                        RETURNING *
                        """
                    ),
                    {
                        "attempt_id": attempt_id,
                        "owner": owner,
                        "lease_generation": lease_generation,
                        "seconds": lease.total_seconds(),
                    },
                )
            ).mappings().first()
            return self._record(row) if row else None

    async def cancel(
        self,
        attempt_id: UUID,
        cancellation_id: UUID,
        reason: str,
        *,
        request_id: UUID,
        job_id: UUID,
        idempotency_key: UUID,
        request_digest: str,
        deadline_at: datetime,
    ) -> AttemptRecord | None:
        async with self._sessions() as session, session.begin():
            await self._lock_attempt(session, attempt_id)
            row = (
                await session.execute(
                    text("SELECT * FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
            if row is not None:
                record = self._record(row)
                if (
                    record.request_id != request_id
                    or record.job_id != job_id
                    or record.idempotency_key != idempotency_key
                    or record.request_digest != request_digest
                    or deadline_at != _request_deadline(record.request_projection)
                ):
                    raise AttemptConflict("cancellation identity conflict")
                if record.status in {"completed", "cancelled", "failed"}:
                    return record
                updated = (
                    await session.execute(
                        text(
                            "UPDATE evaluation_runner_attempts SET status = 'cancelled', "
                            "cancellation_id = :cancellation_id, cancellation_reason = :reason, "
                            "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                            "WHERE attempt_id = :attempt_id RETURNING *"
                        ),
                        {
                            "attempt_id": attempt_id,
                            "cancellation_id": cancellation_id,
                            "reason": reason,
                        },
                    )
                ).mappings().one()
                return self._record(updated)

            existing = (
                await session.execute(
                    text(
                        "SELECT cancellation_id, request_id, job_id, idempotency_key, "
                        "request_digest, deadline_at, reason "
                        "FROM evaluation_runner_cancellations WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
            if existing is not None:
                if (
                    existing["request_id"] != request_id
                    or existing["job_id"] != job_id
                    or existing["idempotency_key"] != idempotency_key
                    or existing["request_digest"] != request_digest
                    or existing["deadline_at"] != deadline_at
                ):
                    raise AttemptConflict("cancellation identity conflict")
                return None
            await session.execute(
                text(
                    "INSERT INTO evaluation_runner_cancellations "
                    "(attempt_id, cancellation_id, request_id, job_id, idempotency_key, "
                    "request_digest, deadline_at, reason) VALUES (:attempt_id, :cancellation_id, "
                    ":request_id, :job_id, :idempotency_key, :request_digest, :deadline_at, :reason) "
                    "ON CONFLICT (attempt_id) DO NOTHING"
                ),
                {
                    "attempt_id": attempt_id,
                    "cancellation_id": cancellation_id,
                    "request_id": request_id,
                    "job_id": job_id,
                    "idempotency_key": idempotency_key,
                    "request_digest": request_digest,
                    "deadline_at": deadline_at,
                    "reason": reason,
                },
            )
            inserted = (
                await session.execute(
                    text(
                        "SELECT request_id, job_id, idempotency_key, request_digest, deadline_at "
                        "FROM evaluation_runner_cancellations WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": attempt_id},
                )
            ).mappings().one()
            if (
                inserted["request_id"] != request_id
                or inserted["job_id"] != job_id
                or inserted["idempotency_key"] != idempotency_key
                or inserted["request_digest"] != request_digest
                or inserted["deadline_at"] != deadline_at
            ):
                raise AttemptConflict("cancellation identity conflict")
            return None

    async def terminal(
        self,
        attempt_id: UUID,
        update: Mapping[str, object],
        *,
        lease_owner: str | None = None,
        lease_generation: UUID | None = None,
    ) -> AttemptRecord:
        status = update.get("status")
        if status not in {"completed", "cancelled", "failed"}:
            raise AttemptConflict("invalid terminal status")
        async with self._sessions() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            """
                        UPDATE evaluation_runner_attempts
                        SET status = :status, result_id = :result_id, result_digest = :result_digest,
                            terminal_message_id = :terminal_message_id, failure_code = :failure_code,
                            terminal_payload = CAST(:terminal_payload AS JSONB),
                            execution_started_at = COALESCE(
                                CAST(:execution_started_at AS TIMESTAMPTZ), execution_started_at
                            ),
                            completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE attempt_id = :attempt_id
                          AND ((status NOT IN ('completed', 'cancelled', 'failed')
                               AND (CAST(:lease_owner AS VARCHAR) IS NULL OR (
                                    lease_owner = CAST(:lease_owner AS VARCHAR)
                                    AND lease_generation = CAST(:lease_generation AS UUID)
                               )))
                               OR (status = :status AND result_id IS NULL))
                        RETURNING *
                        """
                        ),
                        {
                            "attempt_id": attempt_id,
                            "status": status,
                            "result_id": update.get("result_id"),
                            "result_digest": update.get("result_digest"),
                            "terminal_message_id": update.get("terminal_message_id"),
                            "failure_code": update.get("failure_code"),
                            "terminal_payload": json.dumps(update.get("terminal_payload")),
                            "execution_started_at": update.get("execution_started_at"),
                            "lease_owner": lease_owner,
                            "lease_generation": lease_generation,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row:
                await session.execute(
                    text(
                        """
                        INSERT INTO evaluation_runner_terminal_outbox
                            (attempt_id, message_id, payload, publication_status)
                        VALUES (:attempt_id, :message_id, CAST(:payload AS JSONB), 'pending')
                        ON CONFLICT (attempt_id) DO UPDATE SET
                            message_id = EXCLUDED.message_id,
                            payload = EXCLUDED.payload,
                            publication_status = 'pending'
                        """
                    ),
                    {
                        "attempt_id": attempt_id,
                        "message_id": update.get("terminal_message_id"),
                        "payload": json.dumps(update.get("terminal_payload")),
                    },
                )
                return self._record(row)
            existing_row = (
                await session.execute(
                    text("SELECT * FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
            existing = self._record(existing_row) if existing_row else None
            if (
                existing is None
                or existing.status != status
                or existing.result_id != update.get("result_id")
            ):
                raise AttemptConflict("terminal state conflict")
            return existing

    async def mark_published(self, attempt_id: UUID) -> AttemptRecord:
        async with self._sessions() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "UPDATE evaluation_runner_attempts SET publication_status = 'published', updated_at = CURRENT_TIMESTAMP "
                            "WHERE attempt_id = :attempt_id RETURNING *"
                        ),
                        {"attempt_id": attempt_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AttemptConflict("unknown attempt")
            await session.execute(
                text(
                    "UPDATE evaluation_runner_terminal_outbox "
                    "SET publication_status = 'published', published_at = CURRENT_TIMESTAMP "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": attempt_id},
            )
            return self._record(row)

    async def account_terminal(self, attempt_id: UUID) -> bool:
        async with self._sessions() as session, session.begin():
            await self._lock_attempt(session, attempt_id)
            result = await session.execute(
                text(
                    "UPDATE evaluation_runner_attempts "
                    "SET business_accounted = TRUE, updated_at = CURRENT_TIMESTAMP "
                    "WHERE attempt_id = :attempt_id "
                    "AND status IN ('completed', 'cancelled', 'failed') "
                    "AND business_accounted = FALSE"
                ),
                {"attempt_id": attempt_id},
            )
            if getattr(result, "rowcount", 0):
                return True
            row = (
                await session.execute(
                    text(
                        "SELECT status FROM evaluation_runner_attempts "
                        "WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
            if row is None or row["status"] not in {"completed", "cancelled", "failed"}:
                raise AttemptConflict("unknown terminal state")
            return False

    async def get(self, attempt_id: UUID) -> AttemptRecord | None:
        async with self._sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"
                        ),
                        {"attempt_id": attempt_id},
                    )
                )
                .mappings()
                .first()
            )
            return self._record(row) if row else None

    async def pending_publications(self, limit: int = 64) -> list[AttemptRecord]:
        if not 1 <= limit <= 1024:
            raise ValueError("publication batch size is invalid")
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT a.* FROM evaluation_runner_attempts a "
                        "JOIN evaluation_runner_terminal_outbox o USING (attempt_id) "
                        "WHERE o.publication_status = 'pending' ORDER BY o.created_at LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
            return [self._record(row) for row in rows]

    async def recoverable_attempts(self, limit: int = 64) -> list[AttemptRecord]:
        if not 1 <= limit <= 1024:
            raise ValueError("recovery batch size is invalid")
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM evaluation_runner_attempts
                        WHERE status = 'queued'
                           OR (status = 'leased' AND lease_expires_at <= CURRENT_TIMESTAMP)
                        ORDER BY updated_at, attempt_id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
            return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: Mapping[str, object]) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=row["attempt_id"],
            request_id=row["request_id"],
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            status=row["status"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            cancellation_id=row["cancellation_id"],
            cancellation_reason=row["cancellation_reason"],
            result_id=row["result_id"],
            result_digest=row["result_digest"],
            terminal_message_id=row["terminal_message_id"],
            publication_status=row["publication_status"],
            business_accounted=row.get("business_accounted", False),
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            execution_started_at=row.get("execution_started_at"),
            attempt_count=row.get("attempt_count", 0),
            lease_generation=row.get("lease_generation"),
            request_projection=row.get("request_projection"),
            terminal_payload=row.get("terminal_payload"),
        )


__all__ = [
    "AttemptConflict",
    "AttemptRecord",
    "CancellationTombstone",
    "EvaluationRunnerLedger",
    "InMemoryEvaluationRunnerLedger",
    "SqlEvaluationRunnerLedger",
]
