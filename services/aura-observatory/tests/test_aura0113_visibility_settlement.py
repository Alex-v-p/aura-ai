from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from aura_observatory.adapters.query import (
    ProductionObservatoryStore,
    _settle_deleted_retention_visibility,  # pyright: ignore[reportPrivateUsage]
)
from aura_observatory.query import DeletionResult, RetentionAction, RetentionKind, RetentionStatus


class _Result:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self._row = row

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _VisibilityLedger:
    """Small SQL-shaped ledger for the settlement boundary tests."""

    def __init__(self, *, target: set[int], pending: set[int], visible: set[int]) -> None:
        self.target = target
        self.pending = pending
        self.visible = visible
        self.watermark = 0
        self.statements: list[str] = []

    async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> _Result:
        self.statements.append(statement)
        if "SELECT count(*) FROM observatory_retention_targets" in statement:
            return _Result((len(self.target),))
        if "INSERT INTO observatory_ingest_visibility" in statement:
            # The production statement derives this set exclusively through
            # the action's immutable retention target join.  The fake models
            # that result without accepting target identifiers from a caller.
            self.visible.update(self.target)
            self.pending.difference_update(self.target)
            return _Result()
        if "WITH current AS" in statement and "UPDATE observatory_ingest_watermark" in statement:
            sequences = self.target | self.pending | self.visible
            if not sequences:
                return _Result()
            maximum = max(sequences)
            batch_maximum = min(maximum, self.watermark + 65_536)
            missing = [
                sequence
                for sequence in range(self.watermark + 1, batch_maximum + 1)
                if sequence not in self.visible
            ]
            target = missing[0] - 1 if missing else batch_maximum
            if target <= self.watermark:
                return _Result()
            self.watermark = max(self.watermark, target)
            return _Result((self.watermark, maximum))
        raise AssertionError(f"unexpected SQL: {statement}")


@pytest.mark.asyncio
async def test_settlement_advances_through_targeted_slots_but_not_pending_fence() -> None:
    ledger = _VisibilityLedger(target={1}, pending={2}, visible={3})

    await _settle_deleted_retention_visibility(ledger, "action-1", 1)

    assert ledger.watermark == 1
    assert ledger.pending == {2}
    settlement = next(
        sql for sql in ledger.statements if "INSERT INTO observatory_ingest_visibility" in sql
    )
    assert "observatory_retention_targets" in settlement
    assert "t.action_id = %s" in settlement
    assert "observation_id::text" in settlement
    assert "t.phase = 'receipt:'" in settlement
    assert "ingest_sequence IS NOT NULL" in settlement


@pytest.mark.asyncio
async def test_settlement_converges_contiguous_target_and_visible_slots() -> None:
    ledger = _VisibilityLedger(target={1, 2}, pending=set(), visible={3})

    await _settle_deleted_retention_visibility(ledger, "action-2", 2)

    assert ledger.watermark == 3


@pytest.mark.asyncio
async def test_settlement_is_idempotent_and_does_not_touch_untargeted_pending() -> None:
    ledger = _VisibilityLedger(target={1}, pending={2}, visible=set())

    await _settle_deleted_retention_visibility(ledger, "action-3", 1)
    await _settle_deleted_retention_visibility(ledger, "action-3", 1)

    assert ledger.watermark == 1
    assert ledger.pending == {2}
    settlement_count = sum(
        "INSERT INTO observatory_ingest_visibility" in sql for sql in ledger.statements
    )
    assert settlement_count == 2


@pytest.mark.asyncio
async def test_settlement_treats_an_absent_visibility_slot_as_a_pending_gap() -> None:
    # Sequence 2 has no ledger row at all; the set-based scan must not infer
    # that it was settled merely because 1 and 3 are visible.
    ledger = _VisibilityLedger(target={3}, pending=set(), visible={1})

    await _settle_deleted_retention_visibility(ledger, "action-absent", 1)

    assert ledger.watermark == 1


@pytest.mark.asyncio
async def test_settlement_converges_a_large_tail_with_bounded_round_trips() -> None:
    ledger = _VisibilityLedger(target=set(range(1, 147_001)), pending=set(), visible=set())

    await _settle_deleted_retention_visibility(ledger, "action-large", 147_000)

    assert ledger.watermark == 147_000
    assert sum("WITH current AS" in sql for sql in ledger.statements) <= 3


@pytest.mark.asyncio
async def test_settlement_emits_bounded_metadata_only_phase_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = _VisibilityLedger(target={1}, pending=set(), visible=set())

    with caplog.at_level(logging.INFO, logger="aura_observatory.adapters.query"):
        await _settle_deleted_retention_visibility(ledger, "action-observability", 1)

    record = next(item for item in caplog.records if "retention settlement" in item.message)
    assert "component=aura.observatory.telemetry_storage" in record.message
    assert "component_version=1.0.0" in record.message
    assert "operation=deletion" in record.message
    assert "dependency=postgresql" in record.message
    assert "outcome=success" in record.message
    assert "action_id=action-observability" in record.message
    assert "duration_ms=" in record.message


@pytest.mark.asyncio
async def test_settlement_reports_cancellation_truthfully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class CancelledConnection:
        async def execute(
            self, _statement: str, _params: tuple[Any, ...] = ()
        ) -> _Result:
            raise asyncio.CancelledError

    with caplog.at_level(logging.INFO, logger="aura_observatory.adapters.query"):
        with pytest.raises(asyncio.CancelledError):
            await _settle_deleted_retention_visibility(
                CancelledConnection(), "action-cancelled", 0
            )

    record = next(item for item in caplog.records if "retention settlement" in item.message)
    assert "outcome=cancelled" in record.message
    assert "error_code=cancelled" in record.message
    assert "action_id=action-cancelled" in record.message


class _RetentionResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _RetentionConnection:
    def __init__(
        self,
        *,
        late_receipt: bool,
        fail_commit_once: bool = False,
        captured_sequence: int | None = 7,
        legacy_targeted: bool = False,
    ) -> None:
        self.action_id = "action-production"
        self.observation_id = "00000000-0000-4000-8000-000000000901"
        self.captured_sequence = captured_sequence
        self.receipts: dict[str, int | None] = {
            self.observation_id: 9 if late_receipt else self.captured_sequence
        }
        self.target_phase = "targeted" if legacy_targeted else (
            "receipt:null"
            if self.captured_sequence is None
            else f"receipt:{self.captured_sequence}"
        )
        self.visibility: dict[int, bool] = {1: False}
        if self.captured_sequence is not None:
            self.visibility[self.captured_sequence] = False
        self.watermark = 0
        self.phase = "projection_deleted"
        self.fail_commit_once = fail_commit_once
        self.commits = 0

    async def execute(
        self, statement: str, params: tuple[Any, ...] = ()
    ) -> _RetentionResult:
        if "SELECT target_observation_count" in statement:
            return _RetentionResult([(1, 1, 1, self.phase)])
        if "SELECT count(*) FROM observatory_retention_targets" in statement:
            encoded = 0 if self.target_phase == "targeted" else 1
            return _RetentionResult([(encoded,)])
        if "INSERT INTO observatory_ingest_visibility" in statement:
            sequence = self.receipts.get(self.observation_id)
            if sequence is not None and sequence == self.captured_sequence:
                self.visibility[sequence] = True
            return _RetentionResult()
        if "WITH current AS" in statement and "UPDATE observatory_ingest_watermark" in statement:
            sequences = set(self.visibility)
            missing = sorted(
                sequence for sequence in sequences if not self.visibility[sequence]
            )
            target = missing[0] - 1 if missing else (max(sequences) if sequences else None)
            if target is None or target <= self.watermark:
                return _RetentionResult()
            self.watermark = max(self.watermark, target)
            return _RetentionResult([(self.watermark, max(sequences))])
        if "UPDATE observatory_ingest_watermark" in statement:
            self.watermark = int(params[0])
            return _RetentionResult()
        if "SELECT observation_id FROM observatory_retention_targets" in statement:
            return _RetentionResult([(self.observation_id,)] if len(params) == 1 else [])
        if statement.startswith("DELETE FROM observatory_observation_receipts"):
            current = self.receipts.get(self.observation_id)
            if current == self.captured_sequence:
                del self.receipts[self.observation_id]
            return _RetentionResult()
        if (
            "UPDATE observatory_retention_actions" in statement
            and "SET deletion_phase" in statement
        ):
            self.phase = str(params[0])
            return _RetentionResult()
        raise AssertionError(f"unexpected SQL: {statement}")

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit_once:
            self.fail_commit_once = False
            # A real PostgreSQL rollback restores all statements in the
            # transaction. The test double models that boundary explicitly.
            self.receipts[self.observation_id] = self.captured_sequence
            if self.captured_sequence is not None:
                self.visibility[self.captured_sequence] = False
            self.watermark = 0
            self.phase = "projection_deleted"
            raise RuntimeError("receipt settlement commit lost")


class _RetentionContext:
    def __init__(self, connection: _RetentionConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _RetentionConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _NoopClickHouseClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _NoopClickHouseClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_production_retention_leaves_late_same_id_pending_and_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetentionConnection(late_receipt=True)
    monkeypatch.setattr(
        "aura_observatory.adapters.query.httpx.AsyncClient", _NoopClickHouseClient
    )
    store = ProductionObservatoryStore(
        lambda: _RetentionContext(connection),
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    action = RetentionAction(
        connection.action_id,
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="late-same-id",
        deletion_phase="projection_deleted",
    )
    await store.attach_retention(action)

    result = await store.delete_observations(None)

    assert result == DeletionResult(1, 1, 1)
    assert connection.receipts[connection.observation_id] == 9
    assert connection.watermark == 0
    assert connection.phase == "receipts_deleted"


@pytest.mark.asyncio
async def test_production_retention_rolls_back_settlement_and_retries_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetentionConnection(late_receipt=False, fail_commit_once=True)
    monkeypatch.setattr(
        "aura_observatory.adapters.query.httpx.AsyncClient", _NoopClickHouseClient
    )
    store = ProductionObservatoryStore(
        lambda: _RetentionContext(connection),
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    action = RetentionAction(
        connection.action_id,
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="rollback-settlement",
        deletion_phase="projection_deleted",
    )
    await store.attach_retention(action)

    with pytest.raises(RuntimeError, match="commit lost"):
        await store.delete_observations(None)
    assert connection.observation_id in connection.receipts
    assert connection.phase == "projection_deleted"

    result = await store.delete_observations(None)

    assert result == DeletionResult(1, 1, 1)
    assert connection.observation_id not in connection.receipts
    assert connection.phase == "receipts_deleted"
    assert connection.commits >= 2


@pytest.mark.asyncio
async def test_production_retention_deletes_captured_null_sequence_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetentionConnection(late_receipt=False, captured_sequence=None)
    monkeypatch.setattr(
        "aura_observatory.adapters.query.httpx.AsyncClient", _NoopClickHouseClient
    )
    store = ProductionObservatoryStore(
        lambda: _RetentionContext(connection),
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    action = RetentionAction(
        connection.action_id,
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="null-sequence",
        deletion_phase="projection_deleted",
    )
    await store.attach_retention(action)

    result = await store.delete_observations(None)

    assert result == DeletionResult(1, 1, 1)
    assert connection.observation_id not in connection.receipts
    # NULL legacy receipts are deleted as metadata but never enter the
    # visibility ledger or advance the committed fence.
    assert connection.watermark == 0
    assert connection.phase == "receipts_deleted"


@pytest.mark.asyncio
async def test_production_recovery_fails_closed_for_legacy_unencoded_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RetentionConnection(late_receipt=False, legacy_targeted=True)
    monkeypatch.setattr(
        "aura_observatory.adapters.query.httpx.AsyncClient", _NoopClickHouseClient
    )
    store = ProductionObservatoryStore(
        lambda: _RetentionContext(connection),
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    action = RetentionAction(
        connection.action_id,
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="legacy-targeted",
        deletion_phase="projection_deleted",
    )
    await store.attach_retention(action)

    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        await store.delete_observations(None)

    assert connection.observation_id in connection.receipts
    assert connection.watermark == 0
    assert connection.phase == "projection_deleted"
