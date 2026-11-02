"""Optional disposable PostgreSQL recovery checks for AURA-0091."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.entrypoints.evaluation_runner.runner import EvaluationRunner, NullResultPublisher
from aura_core.entrypoints.evaluation_runner.state import (
    AttemptConflict,
    AttemptRecord,
    SqlEvaluationRunnerLedger,
)
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.evaluation_transport import validate_message
from aura_core.runtime.replay.registry import ReplayContext, ReplayRegistry, ReplayResult
from aura_core.runtime.replay.state import InMemoryEvaluationRunnerLedger
from sqlalchemy import text
from test_aura0091_core import _request  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.integration


def _database_url() -> str | None:
    value = os.environ.get("AURA_TEST_DATABASE_URL")
    if not value or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        return None
    return value


class _CompletedReplay:
    component_id = "aura.knowledge.document_processing"
    component_version = "1.0.0"
    fixture_revision = "test-replay-fixture-v1"

    async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
        del case_id
        context.checkpoint()
        return ReplayResult(self.component_id, self.component_version, "pass", {"case_count": 1})


async def _real_completed_terminal(payload: dict[str, object]) -> dict[str, object]:
    """Build the same closed terminal envelope the runner publishes."""

    registry = ReplayRegistry()
    registry.register_replay(_CompletedReplay())
    publisher = NullResultPublisher()
    runner = EvaluationRunner(InMemoryEvaluationRunnerLedger(), registry, publisher)
    await runner.accept(payload)
    await runner.run_once()
    assert len(publisher.messages) == 1
    return publisher.messages[0]


@pytest.mark.asyncio
async def test_sql_runner_ledger_duplicate_lease_terminal_and_publication_recovery() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("set isolated AURA_TEST_DATABASE_URL for runner PostgreSQL acceptance")
    engine = make_engine(database_url)
    sessions = session_factory(engine)
    payload = _request()
    message = validate_message(payload)
    terminal_payload = await _real_completed_terminal(payload)
    terminal_message = validate_message(terminal_payload)
    terminal_data = cast(dict[str, object], terminal_payload["data"])
    attempt = AttemptRecord(
        message.attempt_id,
        message.request_id,
        message.job_id,
        message.idempotency_key,
        message.request_digest,
    )
    try:
        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1 FROM evaluation_runner_attempts LIMIT 1"))
        except Exception as exc:
            pytest.skip(f"runner migration is unavailable: {type(exc).__name__}")

        async with sessions() as session:
            outbox_fk = (
                await session.execute(
                    text(
                        "SELECT 1 FROM pg_constraint WHERE conrelid = "
                        "'evaluation_runner_terminal_outbox'::regclass "
                        "AND contype = 'f' LIMIT 1"
                    )
                )
            ).first()
        assert outbox_fk is None

        ledger = SqlEvaluationRunnerLedger(sessions)
        first = await ledger.receive(attempt)
        duplicate = await ledger.receive(attempt)
        assert duplicate.attempt_id == first.attempt_id
        claimed = await ledger.claim(message.attempt_id, "system-test", timedelta(seconds=30))
        assert claimed is not None
        assert (
            await ledger.claim(message.attempt_id, "second-worker", timedelta(seconds=30)) is None
        )
        terminal = await ledger.terminal(
            message.attempt_id,
            {
                "status": cast(str, terminal_data["status"]),
                "result_id": UUID(str(terminal_data["resultId"])),
                "result_digest": cast(str, terminal_data["resultDigest"]),
                "terminal_message_id": terminal_message.message_id,
                "terminal_payload": terminal_payload,
            },
            lease_owner=claimed.lease_owner,
            lease_generation=claimed.lease_generation,
        )
        async with sessions() as session:
            attempt_row = (
                await session.execute(
                    text(
                        "SELECT status, publication_status, terminal_payload "
                        "FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": message.attempt_id},
                )
            ).mappings().one()
            outbox_row = (
                await session.execute(
                    text(
                        "SELECT message_id, publication_status, payload "
                        "FROM evaluation_runner_terminal_outbox WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": message.attempt_id},
                )
            ).mappings().one()
        assert attempt_row["status"] == "completed"
        assert attempt_row["publication_status"] == "pending"
        assert attempt_row["terminal_payload"] is not None
        assert outbox_row["message_id"] == terminal_message.message_id
        assert outbox_row["publication_status"] == "pending"
        assert outbox_row["payload"] is not None
        replayed = await ledger.terminal(
            message.attempt_id,
            {
                "status": cast(str, terminal_data["status"]),
                "result_id": UUID(str(terminal_data["resultId"])),
                "result_digest": cast(str, terminal_data["resultDigest"]),
                "terminal_message_id": terminal_message.message_id,
                "terminal_payload": terminal_payload,
            },
        )
        assert (
            replayed.result_id
            == terminal.result_id
            == UUID(str(terminal_data["resultId"]))
        )
        published = await ledger.mark_published(message.attempt_id)
        assert published.publication_status == "published"
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM evaluation_runner_terminal_outbox WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": message.attempt_id},
            )
            await session.execute(
                text("DELETE FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                {"attempt_id": message.attempt_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_runner_ledger_conflicts_cancellation_ordering_and_lease_fencing() -> None:
    """Exercise the database compare-and-set rules, not the in-memory model."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip("set isolated AURA_TEST_DATABASE_URL for runner PostgreSQL acceptance")
    engine = make_engine(database_url)
    sessions = session_factory(engine)
    ledger = SqlEvaluationRunnerLedger(sessions)
    payload = _request()
    message = validate_message(payload)
    payload_data = payload["data"]
    assert isinstance(payload_data, dict)

    def attempt_record(*, attempt_id: UUID, idempotency_key: UUID, digest: str) -> AttemptRecord:
        return AttemptRecord(
            attempt_id,
            message.request_id,
            message.job_id,
            idempotency_key,
            digest,
            request_projection=payload,
        )

    rows: list[UUID] = []
    try:
        # Immutable identity conflicts are rejected even when the idempotency
        # key already exists.  This catches an unsafe ON CONFLICT shortcut.
        first = await ledger.receive(
            attempt_record(
                attempt_id=message.attempt_id,
                idempotency_key=message.idempotency_key,
                digest=message.request_digest,
            )
        )
        rows.append(first.attempt_id)
        with pytest.raises(AttemptConflict):
            await ledger.receive(
                attempt_record(
                    attempt_id=message.attempt_id,
                    idempotency_key=message.idempotency_key,
                    digest="b" * 64,
                )
            )
        # The reverse ordering must also fail closed: a new attempt may not
        # reuse an existing idempotency key, even when its digest matches.
        with pytest.raises(AttemptConflict):
            await ledger.receive(
                attempt_record(
                    attempt_id=UUID("00000000-0000-4000-8000-000000009190"),
                    idempotency_key=message.idempotency_key,
                    digest=message.request_digest,
                )
            )
        # Likewise, an existing attempt cannot be rebound to another
        # idempotency identity.
        with pytest.raises(AttemptConflict):
            await ledger.receive(
                attempt_record(
                    attempt_id=message.attempt_id,
                    idempotency_key=UUID("00000000-0000-4000-8000-000000009193"),
                    digest=message.request_digest,
                )
            )

        # Cancellation arriving before the request is durable must win when
        # the request receipt is subsequently inserted.
        pending_id = UUID("00000000-0000-4000-8000-000000009191")
        cancellation_id = UUID("00000000-0000-4000-8000-000000009192")
        rows.append(pending_id)
        assert await ledger.cancel(
            pending_id,
            cancellation_id,
            "OWNER_REQUESTED",
            request_id=message.request_id,
            job_id=message.job_id,
            idempotency_key=UUID("00000000-0000-4000-8000-000000009193"),
            request_digest="c" * 64,
            deadline_at=datetime.fromisoformat(
                cast(str, payload_data["deadlineAt"]).replace("Z", "+00:00")
            ).astimezone(UTC),
        ) is None
        pending = await ledger.receive(
            attempt_record(
                attempt_id=pending_id,
                idempotency_key=UUID("00000000-0000-4000-8000-000000009193"),
                digest="c" * 64,
            )
        )
        assert pending.status == "cancelled"
        assert pending.cancellation_id == cancellation_id

        # Exactly one concurrent owner can claim a queued receipt.
        leased_id = UUID("00000000-0000-4000-8000-000000009194")
        rows.append(leased_id)
        leased = await ledger.receive(
            attempt_record(
                attempt_id=leased_id,
                idempotency_key=UUID("00000000-0000-4000-8000-000000009195"),
                digest="d" * 64,
            )
        )
        del leased
        claimed = await __import__("asyncio").gather(
            ledger.claim(leased_id, "owner-a", timedelta(seconds=30)),
            ledger.claim(leased_id, "owner-b", timedelta(seconds=30)),
        )
        winners = [item for item in claimed if item is not None]
        assert len(winners) == 1
        current = winners[0]
        assert current.lease_generation is not None
        with pytest.raises(AttemptConflict):
            await ledger.terminal(
                leased_id,
                {"status": "completed", "result_id": UUID("00000000-0000-4000-8000-000000009196")},
                lease_owner="stale-owner",
                lease_generation=current.lease_generation,
            )
        # A cancellation settles the current lease; a stale completion cannot
        # overwrite the cancellation terminal state.
        cancelled = await ledger.cancel(
            leased_id,
            UUID("00000000-0000-4000-8000-000000009197"),
            "OWNER_REQUESTED",
            request_id=message.request_id,
            job_id=message.job_id,
            idempotency_key=UUID("00000000-0000-4000-8000-000000009195"),
            request_digest="d" * 64,
            deadline_at=datetime.fromisoformat(
                cast(str, payload_data["deadlineAt"]).replace("Z", "+00:00")
            ).astimezone(UTC),
        )
        assert cancelled is not None and cancelled.status == "cancelled"
        with pytest.raises(AttemptConflict):
            await ledger.terminal(
                leased_id,
                {"status": "completed", "result_id": UUID("00000000-0000-4000-8000-000000009198")},
                lease_owner=current.lease_owner,
                lease_generation=current.lease_generation,
            )

        # A finite max-attempts bind uses the same atomic claim predicate and
        # must allow one concurrent winner, then reject a stale retry after
        # the lease expires.  This exercises the exact runner database role,
        # including the nullable and finite SQL parameter shapes.
        finite_id = UUID("00000000-0000-4000-8000-000000009202")
        rows.append(finite_id)
        await ledger.receive(
            attempt_record(
                attempt_id=finite_id,
                idempotency_key=UUID("00000000-0000-4000-8000-000000009203"),
                digest="f" * 64,
            )
        )
        finite_claims = await __import__("asyncio").gather(
            ledger.claim(finite_id, "finite-owner-a", timedelta(seconds=30), max_attempts=1),
            ledger.claim(finite_id, "finite-owner-b", timedelta(seconds=30), max_attempts=1),
        )
        finite_winners = [item for item in finite_claims if item is not None]
        assert len(finite_winners) == 1
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE evaluation_runner_attempts "
                    "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": finite_id},
            )
        assert (
            await ledger.claim(
                finite_id, "finite-owner-retry", timedelta(seconds=30), max_attempts=1
            )
            is None
        )

        # An expired lease is recoverable by a new generation, while the old
        # generation remains fenced from terminal settlement.
        recovery_id = UUID("00000000-0000-4000-8000-000000009199")
        rows.append(recovery_id)
        await ledger.receive(
            attempt_record(
                attempt_id=recovery_id,
                idempotency_key=UUID("00000000-0000-4000-8000-000000009200"),
                digest="e" * 64,
            )
        )
        old = await ledger.claim(recovery_id, "old-owner", timedelta(seconds=30))
        assert old is not None and old.lease_generation is not None
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE evaluation_runner_attempts "
                    "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": recovery_id},
            )
        recovered = await ledger.claim(recovery_id, "new-owner", timedelta(seconds=30))
        assert recovered is not None and recovered.lease_generation != old.lease_generation
        with pytest.raises(AttemptConflict):
            await ledger.terminal(
                recovery_id,
                {"status": "completed", "result_id": UUID("00000000-0000-4000-8000-000000009201")},
                lease_owner="old-owner",
                lease_generation=old.lease_generation,
            )
    finally:
        # The runner role intentionally cannot delete.  Use the configured
        # Core owner only for this disposable project-scoped cleanup.
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM evaluation_runner_terminal_outbox WHERE attempt_id = ANY(:ids)"),
                {"ids": rows},
            )
            await session.execute(
                text("DELETE FROM evaluation_runner_cancellations WHERE attempt_id = ANY(:ids)"),
                {"ids": rows + [UUID("00000000-0000-4000-8000-000000009194")]},
            )
            await session.execute(
                text("DELETE FROM evaluation_runner_attempts WHERE attempt_id = ANY(:ids)"),
                {"ids": rows},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_receipt_and_pre_receipt_cancel_are_serialized() -> None:
    database_url = _database_url()
    if database_url is None:
        pytest.skip("set isolated AURA_TEST_DATABASE_URL for runner PostgreSQL acceptance")
    engine = make_engine(database_url)
    sessions = session_factory(engine)
    ledger = SqlEvaluationRunnerLedger(sessions)
    payload = _request()
    message = validate_message(payload)
    data = cast(dict[str, object], payload["data"])
    deadline = datetime.fromisoformat(
        cast(str, data["deadlineAt"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    attempt_id = uuid4()
    idempotency_key = uuid4()
    attempt = AttemptRecord(
        attempt_id,
        message.request_id,
        message.job_id,
        idempotency_key,
        message.request_digest,
        request_projection=payload,
    )
    try:
        # Hold the shared attempt lock while both transaction paths are
        # admitted. Once released, either ordering must observe the other's
        # committed state and leave one cancelled terminal.
        async with sessions() as lock_session, lock_session.begin():
            await lock_session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(CAST(:attempt_id AS TEXT), 7310091))"
                ),
                {"attempt_id": attempt_id},
            )
            request_task = asyncio.create_task(ledger.receive(attempt))
            cancel_task = asyncio.create_task(
                ledger.cancel(
                    attempt_id,
                    uuid4(),
                    "OWNER_REQUESTED",
                    request_id=message.request_id,
                    job_id=message.job_id,
                    idempotency_key=idempotency_key,
                    request_digest=message.request_digest,
                    deadline_at=deadline,
                )
            )
            await asyncio.sleep(0.05)
            assert not request_task.done() and not cancel_task.done()
        received, cancelled = await asyncio.gather(request_task, cancel_task)
        assert received.status == "cancelled" or cancelled is None
        current = await ledger.get(attempt_id)
        assert current is not None and current.status == "cancelled"
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM evaluation_runner_cancellations WHERE attempt_id = :attempt_id"),
                {"attempt_id": attempt_id},
            )
            await session.execute(
                text("DELETE FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"),
                {"attempt_id": attempt_id},
            )
        await engine.dispose()


def test_evaluation_runner_migration_is_forward_only_and_rerunnable() -> None:
    migration = Path(__file__).parents[2] / "migrations" / "versions" / "0023_evaluation_runner.py"
    source = migration.read_text(encoding="utf-8")
    assert 'revision = "0023_evaluation_runner"' in source
    assert 'down_revision = "0022_conversation_attachments"' in source
    assert "CREATE TABLE IF NOT EXISTS evaluation_runner_attempts" in source
    assert "CREATE TABLE IF NOT EXISTS evaluation_runner_terminal_outbox" in source
    assert "CREATE TABLE IF NOT EXISTS evaluation_runner_cancellations" in source
    assert "ADD COLUMN IF NOT EXISTS" in source
    assert "CREATE INDEX IF NOT EXISTS" in source
    assert "def downgrade()" in source
    assert "intentionally forward-only" in source
