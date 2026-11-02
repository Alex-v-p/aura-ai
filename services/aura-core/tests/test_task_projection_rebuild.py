"""Rebuild/backfill equivalence tests for the federated Task Center."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskListFilter,
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceInvalidation,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
from aura_core.domains.execution.tasks.service import TaskProjectionService

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _snapshot(source_id: UUID, version: int = 1) -> TaskProjectionSnapshot:
    return TaskProjectionSnapshot(
        owner_scope_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_kind=TaskSourceKind.DOCUMENT_PROCESSING,
        source_id=source_id,
        source_version=version,
        status=TaskNormalizedStatus.COMPLETED if version > 1 else TaskNormalizedStatus.RUNNING,
        source_status="ready" if version > 1 else "processing",
        source_outcome="completed" if version > 1 else None,
        stage=None if version > 1 else "extracting",
        progress=1 if version > 1 else 0.5,
        capabilities=TaskProjectionCapability(),
        route_key="document_detail",
        failure_class=None,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=version),
        finished_at=NOW + timedelta(seconds=version) if version > 1 else None,
    )


class PagedSource:
    kind = TaskSourceKind.DOCUMENT_PROCESSING

    def __init__(self, snapshots: list[TaskProjectionSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls: list[TaskProjectionSnapshotQuery] = []

    async def query_task_projection_snapshots(
        self, query: TaskProjectionSnapshotQuery, *, capability: object
    ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
        self.calls.append(query)
        assert capability is not None
        if query.mode == "exact":
            return next(
                (item for item in self.snapshots if item.source_id == query.source_id), None
            )
        offset = int(query.cursor or "0")
        items = tuple(self.snapshots[offset : offset + query.page_size])
        next_cursor = (
            str(offset + len(items)) if offset + len(items) < len(self.snapshots) else None
        )
        return TaskProjectionSnapshotPage(
            items=items, next_cursor=next_cursor, scan_complete=next_cursor is None
        )

    async def command_task(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("source command is outside rebuild coverage")

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> dict[str, object]:
        del source_id, owner_scope_id
        return {}


@pytest.mark.asyncio
async def test_rebuild_and_live_reconciliation_use_the_same_source_snapshot_boundary() -> None:
    first, second = _snapshot(uuid4()), _snapshot(uuid4())
    source = PagedSource([first, second])
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    await service.reconcile(
        TaskSourceInvalidation(
            source_kind=first.source_kind,
            source_id=first.source_id,
            source_version=1,
            event_id=uuid4(),
            occurred_at=NOW,
            correlation_id=uuid4(),
            causation_id=uuid4(),
        )
    )
    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING, page_size=1)

    assert [query.mode for query in source.calls] == ["exact", "cursor", "cursor"]
    assert all(query.source_kind is TaskSourceKind.DOCUMENT_PROCESSING for query in source.calls)
    rows, _ = await repository.list(TaskListFilter(owner_scope_id=first.owner_scope_id, limit=10))
    assert {row.source_id for row in rows} == {first.source_id, second.source_id}


@pytest.mark.asyncio
async def test_rebuild_is_idempotent_and_marks_absent_rows_unknown_after_completed_scan() -> None:
    first, stale = _snapshot(uuid4()), _snapshot(uuid4())
    source = PagedSource([first])
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    await repository.upsert(stale)

    assert await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING, page_size=10) == 1
    assert await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING, page_size=10) == 1

    stale_row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, stale.source_id)
    assert stale_row is not None
    # Rebuild absence is projection-local uncertainty until the source emits
    # its authoritative deletion tombstone; it must not fabricate deletion.
    assert stale_row.source_deleted is False
    assert stale_row.status is TaskNormalizedStatus.UNKNOWN
    assert stale_row.source_version == stale.source_version
    first_row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, first.source_id)
    assert first_row is not None and first_row.source_version == first.source_version


@pytest.mark.asyncio
async def test_rebuild_cursor_restart_replays_pages_without_regressing_newer_source_versions() -> (
    None
):
    source_id = uuid4()
    newer = _snapshot(source_id, version=2)
    source = PagedSource([newer])
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    await repository.upsert(newer)
    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING)
    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING)

    row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, source_id)
    assert row is not None
    assert row.source_version == 2
    assert row.status is TaskNormalizedStatus.COMPLETED


@pytest.mark.asyncio
async def test_rebuild_resumes_a_durable_cursor_after_interruption() -> None:
    first, second = _snapshot(uuid4()), _snapshot(uuid4(), version=2)

    class InterruptedSource(PagedSource):
        def __init__(self) -> None:
            super().__init__([first, second])
            self.interrupt_once = True

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
            result = await super().query_task_projection_snapshots(query, capability=capability)
            if query.mode == "cursor" and query.cursor == "1" and self.interrupt_once:
                self.interrupt_once = False
                raise TimeoutError("worker restart")
            return result

    source = InterruptedSource()
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    with pytest.raises(TimeoutError):
        await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING, page_size=1)

    restarted = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    await restarted.rebuild(TaskSourceKind.DOCUMENT_PROCESSING, page_size=1)

    cursor_calls = [query.cursor for query in source.calls if query.mode == "cursor"]
    assert cursor_calls == [None, "1", "1"]


@pytest.mark.asyncio
async def test_rebuild_fence_preserves_a_newer_concurrent_live_row() -> None:
    first = _snapshot(uuid4())
    concurrent = _snapshot(uuid4(), version=7)
    repository = MemoryTaskProjectionRepository()

    class RacingSource(PagedSource):
        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
            result = await super().query_task_projection_snapshots(query, capability=capability)
            if query.mode == "cursor" and query.cursor is None:
                await repository.upsert(concurrent)
            return result

    source = RacingSource([first])
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING)

    row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, concurrent.source_id)
    assert row is not None
    assert row.source_version == concurrent.source_version
    assert row.source_deleted is False
    assert row.status is concurrent.status


@pytest.mark.asyncio
async def test_rebuild_does_not_fabricate_source_version_or_tombstone() -> None:
    stale = _snapshot(uuid4(), version=11)
    source = PagedSource([_snapshot(uuid4())])
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(stale)
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING)

    row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, stale.source_id)
    assert row is not None
    assert row.source_version == stale.source_version
    assert row.source_deleted is False


@pytest.mark.asyncio
async def test_rebuild_rejects_cross_kind_owner_and_duplicate_page_items() -> None:
    expected = _snapshot(uuid4())
    wrong_kind = replace(expected, source_kind=TaskSourceKind.WORKFLOW_INSTANCE)
    wrong_owner = replace(expected, owner_scope_id=uuid4(), source_id=uuid4())
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(expected)

    class InvalidPageSource(PagedSource):
        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
            del query, capability
            return TaskProjectionSnapshotPage(
                items=(wrong_kind, wrong_owner, expected, expected),
                next_cursor=None,
                scan_complete=True,
            )

    service = TaskProjectionService(
        repository,
        [InvalidPageSource([])],  # pyright: ignore[reportArgumentType]
    )
    await service.rebuild(TaskSourceKind.DOCUMENT_PROCESSING)

    row = await repository.get(TaskSourceKind.DOCUMENT_PROCESSING, expected.source_id)
    assert row is not None
    assert row.status is TaskNormalizedStatus.UNKNOWN
    assert row.capabilities.can_cancel is False
    assert (TaskSourceKind.DOCUMENT_PROCESSING, expected.source_id) in service.reconciliation_queue
