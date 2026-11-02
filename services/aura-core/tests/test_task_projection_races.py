"""Race and command-routing tests for Task Center projection state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskCommandState,
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceInvalidation,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
from aura_core.domains.execution.tasks.service import TaskProjectionConflict, TaskProjectionService

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _snapshot(
    source_id: UUID, version: int, *, owner: UUID | None = None
) -> TaskProjectionSnapshot:
    return TaskProjectionSnapshot(
        owner_scope_id=owner or uuid4(), source_kind=TaskSourceKind.BRIEFING_OCCURRENCE,
        source_id=source_id, source_version=version, status=TaskNormalizedStatus.RUNNING,
        source_status="running", source_outcome=None, stage="dispatching", progress=0.2,
        capabilities=TaskProjectionCapability(can_cancel=True, can_retry=False),
        route_key="briefing_occurrence_detail", failure_class=None,
        created_at=NOW, updated_at=NOW,
    )


def _event(snapshot: TaskProjectionSnapshot, version: int) -> TaskSourceInvalidation:
    return TaskSourceInvalidation(
        source_kind=snapshot.source_kind, source_id=snapshot.source_id, source_version=version,
        event_id=uuid4(), occurred_at=NOW, correlation_id=uuid4(), causation_id=uuid4(),
    )


class Source:
    kind = TaskSourceKind.BRIEFING_OCCURRENCE

    def __init__(self, snapshots: dict[int, TaskProjectionSnapshot]) -> None:
        self.snapshots = snapshots
        self.commands: list[tuple[TaskCommand, UUID, int, str]] = []

    async def query_task_projection_snapshots(
        self, query: TaskProjectionSnapshotQuery, *, capability: object
    ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
        requested = getattr(query, "minimum_source_version", 1)
        return self.snapshots[max(self.snapshots)] if max(self.snapshots) >= requested else None

    async def command_task(
        self,
        source_id: UUID,
        command: TaskCommand,
        *,
        owner_scope_id: UUID,
        expected_source_version: int,
        idempotency_key: str,
    ) -> object:
        self.commands.append((command, owner_scope_id, expected_source_version, idempotency_key))
        return type(
            "Receipt",
            (),
            {"accepted": True, "unknown": False, "source_version": expected_source_version + 1},
        )()

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> dict[str, object]:
        del source_id, owner_scope_id
        return {}


@pytest.mark.asyncio
async def test_out_of_order_and_duplicate_invalidations_cannot_regress_source_version() -> None:
    source_id = uuid4()
    source = Source({1: _snapshot(source_id, 1), 3: _snapshot(source_id, 3)})
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    await asyncio.gather(
        service.reconcile(_event(source.snapshots[3], 3)),
        service.reconcile(_event(source.snapshots[1], 1)),
    )
    await service.reconcile(_event(source.snapshots[3], 3))

    row = await repository.get(TaskSourceKind.BRIEFING_OCCURRENCE, source_id)
    assert row is not None
    assert row.source_version == 3


@pytest.mark.asyncio
async def test_command_requires_owner_and_expected_version_and_routes_to_source() -> None:
    owner = uuid4()
    snapshot = _snapshot(uuid4(), 4, owner=owner)
    source = Source({4: snapshot})
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    await repository.upsert(snapshot)

    with pytest.raises(LookupError):
        await service.command(
            uuid4(), snapshot.source_kind, snapshot.source_id, TaskCommand.CANCEL,
            expected_source_version=4, idempotency_key="wrong-owner",
        )
    with pytest.raises(TaskProjectionConflict):
        await service.command(
            owner, snapshot.source_kind, snapshot.source_id, TaskCommand.CANCEL,
            expected_source_version=3, idempotency_key="stale",
        )

    receipt = await service.command(
        owner, snapshot.source_kind, snapshot.source_id, TaskCommand.CANCEL,
        expected_source_version=4, idempotency_key="cancel-1",
    )
    assert receipt.state is TaskCommandState.ACCEPTED
    assert source.commands == [(TaskCommand.CANCEL, owner, 4, "cancel-1")]


@pytest.mark.asyncio
async def test_unknown_source_command_outcome_is_not_inferred_as_success() -> None:
    owner = uuid4()
    snapshot = _snapshot(uuid4(), 2, owner=owner)

    class UnknownSource(Source):
        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            return type("Receipt", (), {"accepted": False, "unknown": True, "source_version": 2})()

    repository = MemoryTaskProjectionRepository()
    await repository.upsert(snapshot)
    service = TaskProjectionService(repository, cast(Any, [UnknownSource({2: snapshot})]))

    receipt = await service.command(
        owner, snapshot.source_kind, snapshot.source_id, TaskCommand.CANCEL,
        expected_source_version=2, idempotency_key="unknown-1",
    )
    assert receipt.state is TaskCommandState.UNKNOWN

    unresolved = await repository.get(snapshot.source_kind, snapshot.source_id)
    assert unresolved is not None
    assert unresolved.status is TaskNormalizedStatus.UNKNOWN
    assert unresolved.capabilities.can_cancel is False
    assert unresolved.capabilities.can_retry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotency_key", ["", "   ", "\t\n"])
async def test_cancel_and_retry_reject_missing_or_blank_idempotency_keys(
    idempotency_key: str,
) -> None:
    owner = uuid4()
    snapshot = _snapshot(uuid4(), 2, owner=owner)
    source = Source({2: snapshot})
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(snapshot)
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    with pytest.raises(ValueError):
        await service.command(
            owner,
            snapshot.source_kind,
            snapshot.source_id,
            TaskCommand.CANCEL,
            expected_source_version=2,
            idempotency_key=idempotency_key,
        )
    assert source.commands == []


@pytest.mark.asyncio
async def test_disabled_capability_rejects_command_without_routing_or_reconciliation() -> None:
    owner = uuid4()
    snapshot = _snapshot(uuid4(), 3, owner=owner)
    snapshot = replace(
        snapshot,
        capabilities=TaskProjectionCapability(can_cancel=False, can_retry=False),
    )
    source = Source({3: snapshot})
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(snapshot)
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    receipt = await service.command(
        owner,
        snapshot.source_kind,
        snapshot.source_id,
        TaskCommand.RETRY,
        expected_source_version=3,
        idempotency_key="disabled-retry",
    )
    assert receipt.state is TaskCommandState.REJECTED
    assert source.commands == []
