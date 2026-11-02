"""Core projection behavior owned by AURA-0143."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository


def _snapshot(version: int = 1) -> TaskProjectionSnapshot:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    return TaskProjectionSnapshot(
        owner_scope_id=uuid4(),
        source_kind=TaskSourceKind.WORKFLOW_INSTANCE,
        source_id=uuid4(),
        source_version=version,
        status=TaskNormalizedStatus.RUNNING,
        source_status="running",
        source_outcome=None,
        stage="executing",
        progress=0.5,
        capabilities=TaskProjectionCapability(can_cancel=True),
        route_key="workflow_instance_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_equal_and_older_projection_replays_are_true_no_ops() -> None:
    repository = MemoryTaskProjectionRepository()
    snapshot = _snapshot()
    first = await repository.upsert(snapshot)
    equal = await repository.upsert(snapshot)
    newer = await repository.upsert(replace(snapshot, source_version=2))
    older = await repository.upsert(snapshot)
    assert equal == first
    assert older == newer


@pytest.mark.asyncio
async def test_unknown_source_disables_mutation_capabilities() -> None:
    repository = MemoryTaskProjectionRepository()
    snapshot = _snapshot()
    await repository.upsert(snapshot)
    row = await repository.mark_source_unknown(snapshot.source_kind, snapshot.source_id)
    assert row is not None
    assert row.status is TaskNormalizedStatus.UNKNOWN
    assert row.capabilities.can_cancel is False
    assert row.capabilities.can_retry is False
