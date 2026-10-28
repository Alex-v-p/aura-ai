"""Identifier-only invalidation worker tests."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionRow,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotQuery,
    TaskSourceInvalidation,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
from aura_core.domains.execution.tasks.service import TaskProjectionService
from aura_core.entrypoints.worker.tasks import TaskProjectionWorker


@pytest.mark.asyncio
async def test_worker_reconciles_closed_invalidation_payload() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    source_id = uuid4()
    snapshot = TaskProjectionSnapshot(
        owner_scope_id=uuid4(),
        source_kind=TaskSourceKind.MEMORY_REINDEX,
        source_id=source_id,
        source_version=1,
        status=TaskNormalizedStatus.QUEUED,
        source_status="pending",
        source_outcome=None,
        stage="queued",
        progress=0,
        capabilities=TaskProjectionCapability(),
        route_key="memory_reindex_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
    )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: Any
        ) -> TaskProjectionSnapshot:
            del query, capability
            return snapshot

        async def command_task(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise AssertionError("worker test does not command tasks")

        async def resolve_task_detail(
            self, source_id: Any, *, owner_scope_id: Any
        ) -> Mapping[str, object]:
            del source_id, owner_scope_id
            return {}

    invalidation = TaskSourceInvalidation(
        TaskSourceKind.MEMORY_REINDEX, source_id, 1, uuid4(), now, uuid4(), uuid4()
    )
    service = TaskProjectionService(MemoryTaskProjectionRepository(), [Source()])
    result = await TaskProjectionWorker(service).process(invalidation)
    assert isinstance(result, TaskProjectionRow)
    assert result.source_id == source_id


@pytest.mark.asyncio
async def test_unknown_reconciliation_is_durable_and_consumable() -> None:
    repository = MemoryTaskProjectionRepository()
    source_id = uuid4()
    await repository.enqueue_reconciliation(TaskSourceKind.MEMORY_REINDEX, source_id)

    claimed = await repository.claim_reconciliations()
    assert claimed == ((TaskSourceKind.MEMORY_REINDEX, source_id),)

    await repository.complete_reconciliation(TaskSourceKind.MEMORY_REINDEX, source_id)
    assert await repository.claim_reconciliations() == ()


@pytest.mark.asyncio
async def test_worker_consumes_reconciliation_queue_after_authoritative_recovery() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    source_id = uuid4()
    snapshot = TaskProjectionSnapshot(
        owner_scope_id=uuid4(),
        source_kind=TaskSourceKind.MEMORY_REINDEX,
        source_id=source_id,
        source_version=1,
        status=TaskNormalizedStatus.RUNNING,
        source_status="running",
        source_outcome=None,
        stage="reindexing",
        progress=0.5,
        capabilities=TaskProjectionCapability(),
        route_key="memory_reindex_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
    )
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(replace(snapshot, status=TaskNormalizedStatus.UNKNOWN))
    await repository.enqueue_reconciliation(TaskSourceKind.MEMORY_REINDEX, source_id)

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, query: object, *, capability: object
        ) -> TaskProjectionSnapshot:
            del query, capability
            return snapshot

        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError

        async def resolve_task_detail(
            self, source_id: object, *, owner_scope_id: object
        ) -> dict[str, object]:
            del source_id, owner_scope_id
            return {}

    service = TaskProjectionService(repository, [Source()])  # pyright: ignore[reportArgumentType]
    assert await TaskProjectionWorker(service).process_reconciliation() == 1
    recovered = await repository.get(TaskSourceKind.MEMORY_REINDEX, source_id)
    assert recovered is not None and recovered.status is TaskNormalizedStatus.RUNNING


@pytest.mark.asyncio
async def test_worker_acknowledges_the_durable_outbox_sequence_not_page_length() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    class SourceOutbox:
        def __init__(self) -> None:
            self.acknowledged: int | None = None

        async def replay_task_source_invalidations(
            self, *, after_sequence: int, limit: int
        ) -> tuple[TaskSourceInvalidation, ...]:
            del after_sequence, limit
            return (
                TaskSourceInvalidation(
                    TaskSourceKind.MEMORY_REINDEX,
                    uuid4(),
                    1,
                    uuid4(),
                    now,
                    uuid4(),
                    uuid4(),
                    event_sequence=11,
                ),
                TaskSourceInvalidation(
                    TaskSourceKind.MEMORY_REINDEX,
                    uuid4(),
                    1,
                    uuid4(),
                    now,
                    uuid4(),
                    uuid4(),
                    event_sequence=17,
                ),
            )

        async def acknowledge_task_source_invalidations(self, *, through_sequence: int) -> None:
            self.acknowledged = through_sequence

    class NoopService:
        async def reconcile(self, invalidation: TaskSourceInvalidation) -> None:
            del invalidation

    outbox = SourceOutbox()
    worker = TaskProjectionWorker(  # type: ignore[arg-type]
        NoopService(), source_outbox=outbox  # type: ignore[arg-type]
    )
    assert await worker.process_source_outbox(after_sequence=10) == 2
    assert outbox.acknowledged == 17
