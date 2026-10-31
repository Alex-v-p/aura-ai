"""Source-authority and privacy tests for the Task Center projection."""

from __future__ import annotations

import inspect
import os
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aura_core.domains.automation.briefings import public as briefing_public
from aura_core.domains.automation.briefings.persistence import (
    BriefingOccurrenceRow,
    BriefingScheduleRow,
)
from aura_core.domains.automation.workflows import public as workflow_public
from aura_core.domains.automation.workflows.persistence import WorkflowInstanceRow
from aura_core.domains.execution.tasks.contracts import (
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
from aura_core.domains.execution.tasks.transport import TaskSourceInvalidationStage
from aura_core.domains.knowledge.documents import public as document_public
from aura_core.domains.knowledge.documents.persistence import DocumentRow
from aura_core.domains.knowledge.memory import public as memory_public
from aura_core.domains.knowledge.memory.persistence import MemoryEmbeddingGenerationRow
from aura_core.platform.database.base import Base
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox.persistence import OutboxRow
from aura_core.platform.outbox.repository import SqlOutboxRepository
from sqlalchemy import select, text

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sql_source_adapters() -> Any:
    url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not url or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip("isolated PostgreSQL is required for source cursor acceptance")
    # Importing all four mapping modules above registers their tables in the
    # same Core metadata used by production migrations.
    engine = make_engine(url)
    sessions = session_factory(engine)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)
    try:
        from aura_core.domains.automation.briefings.repository import SqlBriefingRepository
        from aura_core.domains.automation.workflows.repository import SqlWorkflowRepository
        from aura_core.domains.knowledge.documents.repository import SqlDocumentRepository
        from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository

        yield sessions, (
            SqlWorkflowRepository(sessions),
            SqlBriefingRepository(sessions),
            SqlDocumentRepository(sessions),
            SqlMemoryRepository(sessions),
        )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def _snapshot(
    source_id: UUID | None = None,
    *,
    owner: UUID | None = None,
    version: int = 1,
    status: TaskNormalizedStatus = TaskNormalizedStatus.RUNNING,
    source_status: str = "running",
) -> TaskProjectionSnapshot:
    return TaskProjectionSnapshot(
        owner_scope_id=owner or uuid4(),
        source_kind=TaskSourceKind.WORKFLOW_INSTANCE,
        source_id=source_id or uuid4(),
        source_version=version,
        status=status,
        source_status=source_status,
        source_outcome=None,
        stage="executing",
        progress=0.25,
        capabilities=TaskProjectionCapability(can_cancel=True),
        route_key="workflow_instance_detail",
        failure_class=None,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=version),
    )


def _invalidation(
    snapshot: TaskProjectionSnapshot, *, version: int | None = None
) -> TaskSourceInvalidation:
    return TaskSourceInvalidation(
        source_kind=snapshot.source_kind,
        source_id=snapshot.source_id,
        source_version=version or snapshot.source_version,
        event_id=uuid4(),
        occurred_at=NOW,
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )


class Source:
    kind = TaskSourceKind.WORKFLOW_INSTANCE

    def __init__(self, snapshots: list[TaskProjectionSnapshot]) -> None:
        self.snapshots = {item.source_id: item for item in snapshots}
        self.queries: list[tuple[TaskProjectionSnapshotQuery, object]] = []

    async def query_task_projection_snapshots(
        self, query: TaskProjectionSnapshotQuery, *, capability: object
    ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
        self.queries.append((query, capability))
        if query.mode == "exact":
            assert query.source_id is not None
            item = self.snapshots.get(query.source_id)
            return item if item and item.source_version >= query.minimum_source_version else None
        items = tuple(self.snapshots.values())
        return TaskProjectionSnapshotPage(items=items, next_cursor=None, scan_complete=True)

    async def command_task(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("source command is outside source-query coverage")

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> dict[str, object]:
        del source_id, owner_scope_id
        return {}


def test_live_reconciliation_uses_exact_source_snapshot_and_internal_capability() -> None:
    snapshot = _snapshot()
    source = Source([snapshot])
    service = TaskProjectionService(  # pyright: ignore[reportArgumentType]
        MemoryTaskProjectionRepository(), [source]
    )

    result = __import__("asyncio").run(service.reconcile(_invalidation(snapshot)))

    assert result is not None
    assert result.source_version == snapshot.source_version
    assert len(source.queries) == 1
    query, capability = source.queries[0]
    assert query == TaskProjectionSnapshotQuery.exact(
        TaskSourceKind.WORKFLOW_INSTANCE, snapshot.source_id, snapshot.source_version
    )
    assert capability is service._capability  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_missing_or_unavailable_source_does_not_fabricate_owner_or_terminal_truth() -> None:
    snapshot = _snapshot()
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [Source([snapshot])])  # pyright: ignore[reportArgumentType]

    await service.reconcile(_invalidation(snapshot))
    source = service.sources[TaskSourceKind.WORKFLOW_INSTANCE]
    source.snapshots.clear()  # type: ignore[attr-defined]
    unknown = await service.reconcile(_invalidation(snapshot, version=snapshot.source_version + 1))

    assert unknown is not None
    assert unknown.owner_scope_id == snapshot.owner_scope_id
    assert unknown.status is TaskNormalizedStatus.UNKNOWN
    assert unknown.source_version == snapshot.source_version
    assert unknown.source_outcome is None


@pytest.mark.asyncio
async def test_unknown_research_source_is_absent_until_research_authority_exists() -> None:
    snapshot = _snapshot()
    snapshot = replace(snapshot, source_kind=TaskSourceKind.RESEARCH, route_key="research_detail")
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository)

    result = await service.reconcile(_invalidation(snapshot))

    assert result is None
    assert await repository.get(TaskSourceKind.RESEARCH, snapshot.source_id) is None


@pytest.mark.asyncio
async def test_reconcile_rejects_mismatched_source_kind_id_and_owner() -> None:
    expected = _snapshot(owner=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
    wrong_kind = replace(expected, source_kind=TaskSourceKind.DOCUMENT_PROCESSING)

    class MisreportingSource(Source):
        def __init__(self, reported: TaskProjectionSnapshot) -> None:
            super().__init__([expected])
            self.reported = reported

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot:
            del query, capability
            return self.reported

    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(  # pyright: ignore[reportArgumentType]
        repository, [MisreportingSource(wrong_kind)]
    )
    result = await service.reconcile(_invalidation(expected))
    assert result is None
    assert await repository.get(expected.source_kind, expected.source_id) is None
    assert (expected.source_kind, expected.source_id) in service.reconciliation_queue

    wrong_id = replace(expected, source_id=uuid4(), source_version=2)
    source = MisreportingSource(wrong_id)
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    result = await service.reconcile(_invalidation(expected, version=2))
    assert result is None
    assert await repository.get(expected.source_kind, expected.source_id) is None
    assert wrong_id.source_id != expected.source_id

    await repository.upsert(expected)
    wrong_owner = replace(
        expected,
        owner_scope_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        source_version=2,
    )

    class WrongOwnerSource(MisreportingSource):
        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot:
            del query, capability
            return wrong_owner

    service = TaskProjectionService(repository, [WrongOwnerSource(expected)])  # pyright: ignore[reportArgumentType]
    result = await service.reconcile(_invalidation(expected, version=2))
    assert result is not None
    assert result.status is TaskNormalizedStatus.UNKNOWN
    preserved = await repository.get(expected.source_kind, expected.source_id)
    assert preserved is not None
    assert preserved.owner_scope_id == expected.owner_scope_id
    assert preserved.capabilities.can_cancel is False
    assert (expected.source_kind, expected.source_id) in service.reconciliation_queue


def test_reconciliation_does_not_accept_caller_supplied_snapshots() -> None:
    parameters = inspect.signature(TaskProjectionService.reconcile).parameters
    assert tuple(parameters) == ("self", "invalidation")
    assert "snapshot" not in parameters


def test_source_public_seams_do_not_accept_caller_snapshot_collections() -> None:
    seams = (
        workflow_public,
        briefing_public,
        document_public,
        memory_public,
    )
    for module in seams:
        parameters = inspect.signature(module.query_task_projection_snapshots).parameters
        assert "snapshots" not in parameters
        assert "capability" in parameters


@pytest.mark.asyncio
async def test_source_public_seams_deny_ordinary_capabilities() -> None:
    query = TaskProjectionSnapshotQuery.cursor_page(TaskSourceKind.WORKFLOW_INSTANCE)
    for module in (workflow_public, briefing_public, document_public, memory_public):
        with pytest.raises(PermissionError):
            await module.query_task_projection_snapshots(query, capability=object())


def test_source_adapters_are_application_owned_task_ports_with_commands() -> None:
    registrations = (
        (workflow_public.WorkflowTaskSourceAdapter, TaskSourceKind.WORKFLOW_INSTANCE),
        (briefing_public.BriefingTaskSourceAdapter, TaskSourceKind.BRIEFING_OCCURRENCE),
        (document_public.DocumentTaskSourceAdapter, TaskSourceKind.DOCUMENT_PROCESSING),
        (memory_public.MemoryReindexTaskSourceAdapter, TaskSourceKind.MEMORY_REINDEX),
    )
    for adapter_type, kind in registrations:
        adapter = adapter_type(object())
        assert adapter.kind is kind
        assert callable(adapter.query_task_projection_snapshots)
        assert callable(adapter.command_task)


@pytest.mark.asyncio
async def test_source_detail_capability_is_truthful_through_rebuild() -> None:
    """Backfill/rebuild must not turn source-only route keys into links."""

    workflow = workflow_public.WorkflowInstance(
        id=uuid4(),
        workflow_id=uuid4(),
        revision_id=uuid4(),
        issuer="https://issuer.example",
        subject="source-capability-owner",
        current_ordinal=1,
        status=workflow_public.WorkflowInstanceStatus.QUEUED,
        version=1,
        admitted_at=NOW,
        deadline=NOW + timedelta(days=1),
        input_values={},
        updated_at=NOW,
    )
    briefing = briefing_public.BriefingOccurrence(
        id=uuid4(),
        schedule_id=uuid4(),
        issuer="https://issuer.example",
        subject="source-capability-owner",
        sequence=1,
        status=briefing_public.BriefingOccurrenceStatus.SCHEDULED,
        due_at=NOW,
        due_local_date=NOW.date(),
        due_local_time=time(12, 0),
        time_zone="UTC",
        source_completeness=briefing_public.BriefingSourceCompleteness.COMPLETE,
        run_id=None,
        result_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    memory = memory_public.MemoryEmbeddingGeneration(
        id=uuid4(),
        generation=1,
        model_id="source-capability-model",
        model_revision=None,
        dimension=3,
        status="building",
        created_at=NOW,
        issuer="https://issuer.example",
        subject="source-capability-owner",
    )
    source_snapshots = (
        (
            TaskSourceKind.WORKFLOW_INSTANCE,
            cast(
                TaskProjectionSnapshot,
                workflow_public.WorkflowTaskProjectionApplication._snapshot(  # pyright: ignore[reportPrivateUsage]
                    workflow, uuid4()
                ),
            ),
        ),
        (
            TaskSourceKind.BRIEFING_OCCURRENCE,
            cast(
                TaskProjectionSnapshot,
                briefing_public.BriefingTaskProjectionApplication._snapshot(  # pyright: ignore[reportPrivateUsage]
                    briefing, uuid4()
                ),
            ),
        ),
        (
            TaskSourceKind.MEMORY_REINDEX,
            cast(
                TaskProjectionSnapshot,
                memory_public.MemoryReindexTaskProjectionApplication._snapshot(  # pyright: ignore[reportPrivateUsage]
                    memory, uuid4()
                ),
            ),
        ),
    )

    for source_kind, snapshot in source_snapshots:
        assert snapshot.capabilities.has_source_detail is False
        source = Source([snapshot])
        source.kind = source_kind
        repository = MemoryTaskProjectionRepository()
        service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]

        assert await service.rebuild(source_kind) == 1
        row = await repository.get(source_kind, snapshot.source_id)
        assert row is not None
        assert row.capabilities.has_source_detail is False
        assert row.route_key in {
            "workflow_instance_detail",
            "briefing_occurrence_detail",
            "memory_reindex_detail",
        }


@pytest.mark.parametrize(
    ("source_kind", "module_path"),
    (
        (TaskSourceKind.WORKFLOW_INSTANCE, "aura_core.domains.automation.workflows.task_cursor"),
        (TaskSourceKind.BRIEFING_OCCURRENCE, "aura_core.domains.automation.briefings.task_cursor"),
        (TaskSourceKind.DOCUMENT_PROCESSING, "aura_core.domains.knowledge.documents.task_cursor"),
        (TaskSourceKind.MEMORY_REINDEX, "aura_core.domains.knowledge.memory.task_cursor"),
    ),
)
def test_source_cursor_is_opaque_stable_fenced_and_restart_safe(
    source_kind: TaskSourceKind, module_path: str
) -> None:
    """All source boundaries preserve one immutable fence across pages."""
    import importlib

    module = importlib.import_module(module_path)
    fence = NOW
    first_created = NOW - timedelta(minutes=1)
    second_created = NOW - timedelta(minutes=2)
    first_id, second_id, inserted_id = uuid4(), uuid4(), uuid4()
    token = module.encode_task_cursor(fence, first_created, first_id)

    # A restart decodes the same opaque token, and a concurrent update cannot
    # move the immutable keyset position because only created_at/id advance it.
    restored_fence, restored_created, restored_id = module.decode_task_cursor(token)
    assert restored_fence == fence
    assert (restored_created, restored_id) == (first_created, first_id)
    records = [
        (first_created, first_id, NOW + timedelta(hours=1)),
        (second_created, second_id, NOW + timedelta(hours=2)),
        (NOW + timedelta(seconds=1), inserted_id, NOW + timedelta(seconds=1)),
    ]
    page_two = [
        item for item in records
        if item[0] <= restored_fence and (item[0], item[1]) < (restored_created, restored_id)
    ]
    assert [item[1] for item in page_two] == [second_id]
    next_token = module.encode_task_cursor(restored_fence, page_two[0][0], page_two[0][1])
    assert module.decode_task_cursor(next_token)[0] == fence
    assert source_kind in {
        TaskSourceKind.WORKFLOW_INSTANCE,
        TaskSourceKind.BRIEFING_OCCURRENCE,
        TaskSourceKind.DOCUMENT_PROCESSING,
        TaskSourceKind.MEMORY_REINDEX,
    }


@pytest.mark.asyncio
async def test_live_sql_source_adapters_keep_fence_across_pages_and_updates(
    sql_source_adapters: Any,
) -> None:
    """Exercise all four production SQL source seams against isolated PostgreSQL."""
    from aura_core.bootstrap.task_uow import task_projection_service
    sessions, repositories = sql_source_adapters
    workflow_repo, briefing_repo, document_repo, memory_repo = repositories
    owner_issuer, owner_subject = "https://issuer.example", "sql-cursor-owner"
    base = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    async with sessions() as session, session.begin():
        workflow_ids = [uuid4() for _ in range(3)]
        for index, item_id in enumerate(workflow_ids):
            created = base - timedelta(minutes=index + 1)
            session.add(
                WorkflowInstanceRow(
                    id=item_id,
                    workflow_id=uuid4(),
                    revision_id=uuid4(),
                    principal_issuer=owner_issuer,
                    principal_subject=owner_subject,
                    current_ordinal=1,
                    status="queued",
                    version=1,
                    admitted_at=created,
                    deadline=created + timedelta(days=1),
                    input_values={},
                    created_at=created,
                    updated_at=created,
                )
            )
        schedule_id = uuid4()
        session.add(
            BriefingScheduleRow(
                id=schedule_id,
                principal_issuer=owner_issuer,
                principal_subject=owner_subject,
                display_name="SQL cursor briefing",
                status="active",
                version=1,
                local_time="12:00:00",
                time_zone="UTC",
                include_reminders=False,
                calendar_ids=[],
                document_ids=[],
                agent_revision_id=uuid4(),
                execution_policies={},
                created_at=base,
                updated_at=base,
            )
        )
        await session.flush()
        briefing_ids = [uuid4() for _ in range(3)]
        for index, item_id in enumerate(briefing_ids):
            created = base - timedelta(minutes=index + 1)
            session.add(
                BriefingOccurrenceRow(
                    id=item_id,
                    schedule_id=schedule_id,
                    principal_issuer=owner_issuer,
                    principal_subject=owner_subject,
                    occurrence_sequence=index + 1,
                    status="scheduled",
                    due_at=created,
                    due_local_date=(base - timedelta(days=index + 1)).date(),
                    due_local_time="12:00:00",
                    time_zone="UTC",
                    source_completeness="complete",
                    version=1,
                    created_at=created,
                    updated_at=created,
                )
            )
        document_ids = [uuid4() for _ in range(3)]
        for index, item_id in enumerate(document_ids):
            created = base - timedelta(minutes=index + 1)
            session.add(
                DocumentRow(
                    id=item_id,
                    principal_issuer=owner_issuer,
                    principal_subject=owner_subject,
                    filename=f"cursor-{index}.txt",
                    media_type="text/plain",
                    byte_count=1,
                    digest=(str(index) * 64)[:64],
                    object_key=f"documents/cursor/{item_id}",
                    status="queued",
                    version=1,
                    created_at=created,
                    updated_at=created,
                )
            )
        memory_ids = [uuid4() for _ in range(3)]
        for index, item_id in enumerate(memory_ids):
            created = base - timedelta(minutes=index + 1)
            session.add(
                MemoryEmbeddingGenerationRow(
                    id=item_id,
                    generation=index + 1,
                    model_id="cursor-model",
                    dimension=3,
                    status="building",
                    principal_issuer=owner_issuer,
                    principal_subject=owner_subject,
                    task_projection_version=1,
                    created_at=created,
                )
            )

    applications = (
        workflow_public.WorkflowTaskSourceAdapter(workflow_repo),
        briefing_public.BriefingTaskSourceAdapter(briefing_repo),
        document_public.DocumentTaskSourceAdapter(document_repo),
        memory_public.MemoryReindexTaskSourceAdapter(memory_repo),
    )
    service = task_projection_service(testing=True, sources=applications)
    queries = (
        (TaskSourceKind.WORKFLOW_INSTANCE, workflow_ids),
        (TaskSourceKind.BRIEFING_OCCURRENCE, briefing_ids),
        (TaskSourceKind.DOCUMENT_PROCESSING, document_ids),
        (TaskSourceKind.MEMORY_REINDEX, memory_ids),
    )
    service_capability = cast(Any, service)._capability
    for kind, identifiers in queries:
        query = TaskProjectionSnapshotQuery.cursor_page(kind, page_size=2)
        adapter = next(adapter for adapter in applications if adapter.kind is kind)
        first = await cast(Any, adapter).query_task_projection_snapshots(
            query, capability=service_capability
        )
        assert isinstance(first, TaskProjectionSnapshotPage)
        assert len(first.items) == 2 and first.next_cursor is not None
        fence = first.next_cursor
        async with sessions() as session, session.begin():
            # New rows after the captured fence must not appear on page two;
            # updating an existing row must not move its immutable keyset key.
            if kind is TaskSourceKind.WORKFLOW_INSTANCE:
                await session.execute(
                    text(
                        "UPDATE workflow_instances SET status='running', version=2, "
                        "updated_at=:updated WHERE id=:id"
                    ),
                    {"id": identifiers[0], "updated": base + timedelta(hours=2)},
                )
                late_id = uuid4()
                session.add(
                    WorkflowInstanceRow(
                        id=late_id, workflow_id=uuid4(), revision_id=uuid4(),
                        principal_issuer=owner_issuer, principal_subject=owner_subject,
                        current_ordinal=1, status="queued", version=1,
                        admitted_at=base + timedelta(hours=1),
                        deadline=base + timedelta(days=1), input_values={},
                        created_at=base + timedelta(hours=1),
                        updated_at=base + timedelta(hours=1),
                    )
                )
            elif kind is TaskSourceKind.BRIEFING_OCCURRENCE:
                await session.execute(
                    text(
                        "UPDATE briefing_occurrences SET status='running', version=2, "
                        "updated_at=:updated WHERE id=:id"
                    ),
                    {"id": identifiers[0], "updated": base + timedelta(hours=2)},
                )
                late_id = uuid4()
                session.add(
                    BriefingOccurrenceRow(
                        id=late_id, schedule_id=schedule_id,
                        principal_issuer=owner_issuer, principal_subject=owner_subject,
                        occurrence_sequence=99, status="scheduled",
                        due_at=base + timedelta(days=99),
                        due_local_date=(base + timedelta(days=99)).date(),
                        due_local_time="12:00:00", time_zone="UTC",
                        source_completeness="complete", version=1,
                        created_at=base + timedelta(hours=1),
                        updated_at=base + timedelta(hours=1),
                    )
                )
            elif kind is TaskSourceKind.DOCUMENT_PROCESSING:
                await session.execute(
                    text(
                        "UPDATE documents SET status='processing', version=2, "
                        "updated_at=:updated WHERE id=:id"
                    ),
                    {"id": identifiers[0], "updated": base + timedelta(hours=2)},
                )
                session.add(
                    DocumentRow(
                        id=uuid4(), principal_issuer=owner_issuer,
                        principal_subject=owner_subject, filename="late.txt",
                        media_type="text/plain", byte_count=1, digest="f" * 64,
                        object_key="documents/cursor/late", status="queued", version=1,
                        created_at=base + timedelta(hours=1),
                        updated_at=base + timedelta(hours=1),
                    )
                )
            elif kind is TaskSourceKind.MEMORY_REINDEX:
                await session.execute(
                    text(
                        "UPDATE memory_embedding_generations "
                        "SET status='active', task_projection_version=2, created_at=created_at "
                        "WHERE id=:id"
                    ),
                    {"id": identifiers[-1]},
                )
                session.add(
                    MemoryEmbeddingGenerationRow(
                        id=uuid4(), generation=99, model_id="cursor-late", dimension=3,
                        status="building", principal_issuer=owner_issuer,
                        principal_subject=owner_subject, task_projection_version=1,
                        created_at=base + timedelta(hours=1),
                    )
                )
        second_query = TaskProjectionSnapshotQuery.cursor_page(kind, cursor=fence, page_size=2)
        second = await cast(Any, adapter).query_task_projection_snapshots(
            second_query, capability=service_capability
        )
        assert isinstance(second, TaskProjectionSnapshotPage)
        assert len(second.items) == 1
        assert second.items[0].source_id == identifiers[-1]
        replay = await cast(Any, adapter).query_task_projection_snapshots(
            second_query, capability=service_capability
        )
        assert isinstance(replay, TaskProjectionSnapshotPage)
        assert tuple(item.source_id for item in replay.items) == tuple(
            item.source_id for item in second.items
        )

    # A source pass through the production adapters must converge the
    # projection and settle a pre-existing row that is absent from the stable
    # source snapshot without inventing a deleted source state.
    projection_repository = cast(Any, service).repository
    for kind, identifiers in queries:
        route_key = {
            TaskSourceKind.WORKFLOW_INSTANCE: "workflow_instance_detail",
            TaskSourceKind.BRIEFING_OCCURRENCE: "briefing_occurrence_detail",
            TaskSourceKind.DOCUMENT_PROCESSING: "document_detail",
            TaskSourceKind.MEMORY_REINDEX: "memory_reindex_detail",
        }[kind]
        stale_id = uuid4()
        stale = TaskProjectionSnapshot(
            owner_scope_id=uuid4(), source_kind=kind, source_id=stale_id,
            source_version=1, status=TaskNormalizedStatus.COMPLETED,
            source_status="completed", source_outcome="ok", stage=None,
            progress=1, capabilities=TaskProjectionCapability(), route_key=route_key,
            failure_class=None, created_at=base, updated_at=base,
        )
        await projection_repository.upsert(stale)
        count = await service.rebuild(kind, page_size=2)
        # A new pass captures a fresh fence, therefore includes the row that
        # was inserted after the earlier cursor pass; the stale row remains
        # absent and is settled below.
        assert count == len(identifiers) + 1
        settled = await projection_repository.get(kind, stale_id)
        assert settled is not None
        assert settled.status is TaskNormalizedStatus.UNKNOWN
        assert settled.source_deleted is False


@pytest.mark.asyncio
async def test_platform_task_invalidation_outbox_commit_or_rollback_generic(
    sql_source_adapters: Any,
) -> None:
    """Each authoritative SQL transition and its task event share one transaction."""
    sessions, repositories = sql_source_adapters
    workflow_repo, briefing_repo, document_repo, memory_repo = repositories
    del workflow_repo, briefing_repo, document_repo, memory_repo
    owner_issuer, owner_subject = "https://issuer.example", "sql-outbox-owner"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    # Use isolated source rows so the test proves the transaction boundary,
    # while the adapters remain the production SQL repositories.
    async with sessions() as session, session.begin():
        workflow_id, schedule_id, briefing_id, document_id, memory_id = (uuid4() for _ in range(5))
        session.add(
            WorkflowInstanceRow(
                id=workflow_id, workflow_id=uuid4(), revision_id=uuid4(),
                principal_issuer=owner_issuer, principal_subject=owner_subject,
                current_ordinal=1, status="queued", version=1, admitted_at=now,
                deadline=now + timedelta(days=1), input_values={}, created_at=now, updated_at=now,
            )
        )
        session.add(
            BriefingScheduleRow(
                id=schedule_id, principal_issuer=owner_issuer, principal_subject=owner_subject,
                display_name="outbox", status="active", version=1, local_time="12:00:00",
                time_zone="UTC", include_reminders=False, calendar_ids=[], document_ids=[],
                agent_revision_id=uuid4(), execution_policies={}, created_at=now, updated_at=now,
            )
        )
        await session.flush()
        session.add(
            BriefingOccurrenceRow(
                id=briefing_id, schedule_id=schedule_id, principal_issuer=owner_issuer,
                principal_subject=owner_subject, occurrence_sequence=1, status="scheduled",
                due_at=now, due_local_date=now.date(), due_local_time="12:00:00", time_zone="UTC",
                source_completeness="complete", version=1, created_at=now, updated_at=now,
            )
        )
        session.add(
            DocumentRow(
                id=document_id, principal_issuer=owner_issuer, principal_subject=owner_subject,
                filename="outbox.txt", media_type="text/plain", byte_count=1, digest="a" * 64,
                object_key="documents/outbox", status="queued", version=1,
                created_at=now, updated_at=now,
            )
        )
        session.add(
            MemoryEmbeddingGenerationRow(
                id=memory_id, generation=1, model_id="outbox", dimension=3, status="building",
                principal_issuer=owner_issuer, principal_subject=owner_subject,
                task_projection_version=1, created_at=now,
            )
        )

    stage = TaskSourceInvalidationStage(SqlOutboxRepository())
    source_rows = (
        (TaskSourceKind.WORKFLOW_INSTANCE, workflow_id, "workflow_instances", "version"),
        (TaskSourceKind.BRIEFING_OCCURRENCE, briefing_id, "briefing_occurrences", "version"),
        (TaskSourceKind.DOCUMENT_PROCESSING, document_id, "documents", "version"),
        (
            TaskSourceKind.MEMORY_REINDEX,
            memory_id,
            "memory_embedding_generations",
            "task_projection_version",
        ),
    )
    for kind, source_id, table, version_column in source_rows:
        event_id = uuid4()
        payload = {
            "schemaVersion": 1, "eventId": str(event_id),
            "eventType": "task.source_invalidated", "sourceKind": kind.value,
            "sourceId": str(source_id), "sourceVersion": 2,
            "occurredAt": now.isoformat(), "correlationId": str(event_id),
            "causationId": str(event_id),
        }
        timestamp_assignment = (
            ", updated_at=:updated" if table != "memory_embedding_generations" else ""
        )
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    f"UPDATE {table} SET {version_column}=2{timestamp_assignment} WHERE id=:id"
                ),
                {"id": source_id, "updated": now + timedelta(seconds=1)},
            )
            await stage.stage_task_source_invalidation(session, payload=payload)
        async with sessions() as session:
            row = await session.get(OutboxRow, event_id)
            assert row is not None and row.payload == payload

        rollback_event_id = uuid4()
        rollback_payload = payload | {"eventId": str(rollback_event_id)}
        with pytest.raises(RuntimeError, match="forced rollback"):
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        f"UPDATE {table} SET {version_column}=3{timestamp_assignment} WHERE id=:id"
                    ),
                    {"id": source_id, "updated": now + timedelta(seconds=2)},
                )
                await stage.stage_task_source_invalidation(session, payload=rollback_payload)
                raise RuntimeError("forced rollback")
        async with sessions() as session:
            assert await session.get(OutboxRow, rollback_event_id) is None
            version = await session.scalar(
                text(f"SELECT {version_column} FROM {table} WHERE id=:id"),
                {"id": source_id},
            )
            assert version == 2


@pytest.mark.asyncio
async def test_live_production_source_transitions_stage_task_events_atomically(
    sql_source_adapters: Any,
) -> None:
    """Production source mutations own staging and share their DB transaction."""
    from aura_core.bootstrap.briefing_uow import briefing_repository
    from aura_core.bootstrap.document_uow import SqlDocumentCommandSink
    from aura_core.bootstrap.workflow_uow import (
        _WorkflowOutboxIntent,  # pyright: ignore[reportPrivateUsage]
    )
    from aura_core.domains.automation.workflows.public import (
        OwnerApprovalStep,
        WorkflowRevision,
    )
    from aura_core.domains.automation.workflows.repository import SqlWorkflowRepository
    from aura_core.domains.knowledge.documents.public import Document, DocumentStatus
    from aura_core.domains.knowledge.documents.repository import SqlDocumentRepository
    from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository

    sessions, _ = sql_source_adapters
    issuer, subject = "https://issuer.example", "production-transition-owner"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    async def task_events(source_kind: TaskSourceKind, source_id: UUID) -> list[dict[str, object]]:
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(OutboxRow).where(
                        OutboxRow.topic == "aura.execution.task_source_invalidated.v1"
                    )
                )
            ).all()
        return [
            row.payload
            for row in rows
            if row.payload.get("sourceKind") == source_kind.value
            and row.payload.get("sourceId") == str(source_id)
        ]

    async def task_event_count() -> int:
        async with sessions() as session:
            return len(
                (
                    await session.scalars(
                        select(OutboxRow).where(
                            OutboxRow.topic == "aura.execution.task_source_invalidated.v1"
                        )
                    )
                ).all()
            )

    class FailingStage:
        async def stage_task_source_invalidation(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected task invalidation staging failure")

    # Workflow admission is the authoritative transition.  Its bootstrap
    # intent stages the task event inside the same SQL transaction.
    workflow_intents = _WorkflowOutboxIntent()

    async def no_wakeup(*_args: object, **_kwargs: object) -> None:
        # This acceptance test isolates the task invalidation lifecycle hook;
        # wake transport validation is covered by the workflow outbox suite.
        return None

    workflow_intents.stage_wakeup = no_wakeup  # type: ignore[method-assign]
    workflow_repo = SqlWorkflowRepository(sessions, intents=workflow_intents)
    workflow_revision = WorkflowRevision(
        id=uuid4(), workflow_id=uuid4(), issuer=issuer, subject=subject,
        display_name="production workflow", input_fields=(), steps=(OwnerApprovalStep(),),
    )
    draft = await workflow_repo.create_draft(
        workflow_revision, issuer=issuer, subject=subject, idempotency_key="live-workflow-draft"
    )
    published = await workflow_repo.publish(
        draft.id, expected_version=draft.version, issuer=issuer, subject=subject,
        idempotency_key="live-workflow-publish",
    )
    admitted = await workflow_repo.admit(
        issuer, subject, published.id, input_values={}, idempotency_key="live-workflow-admit"
    )
    workflow_events = await task_events(TaskSourceKind.WORKFLOW_INSTANCE, admitted.id)
    assert len(workflow_events) == 1
    assert workflow_events[0]["sourceVersion"] == admitted.version
    stored_admitted = await workflow_repo.get_instance(issuer, subject, admitted.id)
    assert stored_admitted.version == admitted.version

    failing_workflow_intents = _WorkflowOutboxIntent()
    failing_workflow_intents.stage_wakeup = no_wakeup  # type: ignore[method-assign]
    failing_workflow_intents._task_stage = FailingStage()  # type: ignore[attr-defined]
    failing_workflow_repo = SqlWorkflowRepository(sessions, intents=failing_workflow_intents)
    failing_revision = await failing_workflow_repo.create_draft(
        WorkflowRevision(
            id=uuid4(), workflow_id=uuid4(), issuer=issuer, subject=subject,
            display_name="rollback workflow", input_fields=(), steps=(OwnerApprovalStep(),),
        ),
        issuer=issuer, subject=subject, idempotency_key="rollback-workflow-draft",
    )
    failing_published = await failing_workflow_repo.publish(
        failing_revision.id, expected_version=failing_revision.version,
        issuer=issuer, subject=subject, idempotency_key="rollback-workflow-publish",
    )
    events_before_workflow_rollback = await task_event_count()
    with pytest.raises(RuntimeError, match="staging failure"):
        await failing_workflow_repo.admit(
            issuer, subject, failing_published.id, input_values={},
            idempotency_key="rollback-workflow-admit",
        )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(WorkflowInstanceRow.id).where(
                    WorkflowInstanceRow.revision_id == failing_published.id
                )
            )
            is None
        )
    assert await task_event_count() == events_before_workflow_rollback

    # Briefing schedule creation creates its first occurrence and invokes the
    # bootstrap lifecycle bridge, which stages an occurrence invalidation.
    briefing_repo = briefing_repository(sessions, testing=False)
    schedule = await briefing_repo.create_schedule(
        issuer, subject, display_name="production briefing",
        cadence=briefing_public.BriefingDailyCadence(time(12, 0), "UTC"),
        sources=briefing_public.BriefingSourceGrants(True, (uuid4(),), ()),
        agent_revision_id=uuid4(),
        execution_policies=briefing_public.BriefingExecutionPolicyReferences(
            uuid4(), uuid4(), uuid4(), uuid4()
        ),
        idempotency_key="live-briefing-create", now=now,
    )
    occurrences = await briefing_repo.list_occurrences(issuer, subject, schedule.id)
    occurrence = occurrences.items[0]
    briefing_events = await task_events(TaskSourceKind.BRIEFING_OCCURRENCE, occurrence.id)
    assert len(briefing_events) == 1
    assert briefing_events[0]["sourceVersion"] == occurrence.version
    failing_briefing = briefing_repository(sessions, testing=False)
    cast(Any, failing_briefing).lifecycle_events._task_stage = FailingStage()
    events_before_briefing_rollback = await task_event_count()
    with pytest.raises(RuntimeError, match="staging failure"):
        await failing_briefing.create_schedule(
            issuer, subject, display_name="rollback briefing",
            cadence=briefing_public.BriefingDailyCadence(time(13, 0), "UTC"),
            sources=briefing_public.BriefingSourceGrants(True, (uuid4(),), ()),
            agent_revision_id=uuid4(),
            execution_policies=briefing_public.BriefingExecutionPolicyReferences(
                uuid4(), uuid4(), uuid4(), uuid4()
            ),
            idempotency_key="rollback-briefing-create", now=now,
        )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(BriefingScheduleRow.id).where(
                    BriefingScheduleRow.display_name == "rollback briefing"
                )
            )
            is None
        )
    assert await task_event_count() == events_before_briefing_rollback

    # Document upload processing is staged by the production SQL command sink.
    document_sink = SqlDocumentCommandSink(sessions)
    document_repo = SqlDocumentRepository(sessions, outbox=document_sink)
    document = Document(
        uuid4(), issuer, subject, "production.txt", "text/plain", 1, "a" * 64,
        "documents/production", DocumentStatus.QUEUED, 1, now, now,
    )
    await document_repo.create_upload(document, idempotency_key="live-document-upload")
    document_events = await task_events(TaskSourceKind.DOCUMENT_PROCESSING, document.id)
    assert len(document_events) == 1 and document_events[0]["sourceVersion"] == 1
    failing_document_sink = SqlDocumentCommandSink(sessions)
    cast(Any, failing_document_sink).task_stage = FailingStage()
    failing_document_repo = SqlDocumentRepository(sessions, outbox=failing_document_sink)
    rollback_document = replace(document, id=uuid4(), object_key="documents/rollback")
    events_before_document_rollback = await task_event_count()
    with pytest.raises(RuntimeError, match="staging failure"):
        await failing_document_repo.create_upload(
            rollback_document, idempotency_key="rollback-document-upload"
        )
    assert await failing_document_repo.get((issuer, subject), rollback_document.id) is None
    assert not await task_events(TaskSourceKind.DOCUMENT_PROCESSING, rollback_document.id)
    assert await task_event_count() == events_before_document_rollback

    # Memory embedding-generation lifecycle is source-owned and increments its
    # durable version before the bootstrap-injected task stage is called.
    memory_repo = SqlMemoryRepository(
        sessions, source_outbox=TaskSourceInvalidationStage(SqlOutboxRepository())
    )
    generation = await memory_repo.register_embedding_generation(
        issuer, subject, generation=77, model_id="production", dimension=3
    )
    memory_events = await task_events(TaskSourceKind.MEMORY_REINDEX, generation.id)
    assert len(memory_events) == 1 and memory_events[0]["sourceVersion"] == 1
    failing_memory_repo = SqlMemoryRepository(sessions, source_outbox=FailingStage())
    events_before_memory_rollback = await task_event_count()
    with pytest.raises(RuntimeError, match="staging failure"):
        await failing_memory_repo.register_embedding_generation(
            issuer, subject, generation=78, model_id="rollback", dimension=3
        )
    assert await task_event_count() == events_before_memory_rollback
    generations = await memory_repo.list_embedding_generations(issuer, subject)
    assert all(item.generation != 78 for item in generations)


def test_bootstrap_binds_capability_only_inside_composition() -> None:
    from aura_core.bootstrap.task_uow import task_projection_service

    adapter = workflow_public.WorkflowTaskSourceAdapter(object())
    service = task_projection_service(testing=True, sources=(adapter,))
    service_any = cast(Any, service)
    adapter_any = cast(Any, adapter)._adapter
    assert service_any._capability is adapter_any._ApplicationTaskSourceAdapter__capability
    assert adapter_any._ApplicationTaskSourceAdapter__capability is not None


@pytest.mark.asyncio
async def test_equal_version_invalidation_is_a_true_noop() -> None:
    snapshot = _snapshot(version=4)
    source = Source([snapshot])
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    first = await repository.upsert(snapshot)

    result = await service.reconcile(_invalidation(snapshot, version=4))

    assert result == first
    assert source.queries == []


@pytest.mark.asyncio
async def test_unknown_projection_recovers_from_same_authoritative_version() -> None:
    snapshot = _snapshot(version=4)
    source = Source([snapshot])
    repository = MemoryTaskProjectionRepository()
    service = TaskProjectionService(repository, [source])  # pyright: ignore[reportArgumentType]
    await repository.upsert(replace(snapshot, status=TaskNormalizedStatus.UNKNOWN))

    result = await service.reconcile(_invalidation(snapshot, version=4))

    assert result is not None
    assert result.status is TaskNormalizedStatus.RUNNING
    assert len(source.queries) == 1


def test_projection_capability_is_not_importable_or_exported() -> None:
    import aura_core.domains.execution.tasks as tasks
    import aura_core.domains.execution.tasks.ports as ports

    assert "PROJECTION_CAPABILITY" not in getattr(ports, "__all__", ())
    assert not hasattr(ports, "PROJECTION_CAPABILITY")
    assert not hasattr(ports, "_new_projection_capability")
    assert not hasattr(tasks, "PROJECTION_CAPABILITY")


@pytest.mark.asyncio
async def test_workflow_source_adapter_reads_real_application_instance() -> None:
    owner_scope = UUID("11111111-1111-1111-1111-111111111111")
    instance = workflow_public.WorkflowInstance(
        id=uuid4(),
        workflow_id=uuid4(),
        revision_id=uuid4(),
        issuer="issuer",
        subject="subject",
        current_ordinal=1,
        status=workflow_public.WorkflowInstanceStatus.RUNNING,
        version=3,
        admitted_at=NOW,
        deadline=NOW + timedelta(hours=1),
        input_values={},
        updated_at=NOW,
    )

    class Application:
        async def get_instance_for_task_projection(self, instance_id: UUID) -> object:
            assert instance_id == instance.id
            return instance

        async def list_instances_for_task_projection(self, **_kwargs: object) -> tuple[object, ...]:
            return (instance,)

        async def get_instance(self, issuer: str, subject: str, instance_id: UUID) -> object:
            assert (issuer, subject, instance_id) == ("issuer", "subject", instance.id)
            return instance

        async def list_instances(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
            return (instance,)

        async def cancel(self, *_args: object, **_kwargs: object) -> object:
            return instance

    adapter = workflow_public.WorkflowTaskSourceAdapter(
        Application(),
        owner_scope_id=owner_scope,
        issuer="issuer",
        subject="subject",
        workflow_id=instance.workflow_id,
    )
    capability = (
        __import__("aura_core.bootstrap.task_uow", fromlist=["task_projection_service"])
        .task_projection_service(testing=True, sources=(adapter,))
        ._capability
    )
    snapshot = await cast(Any, adapter).query_task_projection_snapshots(
        TaskProjectionSnapshotQuery.exact(TaskSourceKind.WORKFLOW_INSTANCE, instance.id),
        capability=capability,
    )
    assert isinstance(snapshot, TaskProjectionSnapshot)
    assert snapshot.source_version == 3
    assert snapshot.capabilities.can_cancel


@pytest.mark.asyncio
async def test_briefing_source_adapter_reads_real_occurrence_and_owner_detail() -> None:
    owner_scope = UUID("22222222-2222-2222-2222-222222222222")
    occurrence = briefing_public.BriefingOccurrence(
        id=uuid4(),
        schedule_id=uuid4(),
        issuer="issuer",
        subject="subject",
        sequence=2,
        status=briefing_public.BriefingOccurrenceStatus.FAILED,
        due_at=NOW,
        due_local_date=NOW.date(),
        due_local_time=NOW.time(),
        time_zone="UTC",
        source_completeness=briefing_public.BriefingSourceCompleteness.COMPLETE,
        run_id=None,
        result_id=None,
        created_at=NOW,
        updated_at=NOW,
        version=4,
    )

    class Application:
        async def get_occurrence_for_task_projection(self, occurrence_id: UUID) -> object:
            assert occurrence_id == occurrence.id
            return occurrence

        async def list_occurrences_for_task_projection(
            self, **_kwargs: object
        ) -> tuple[object, ...]:
            return (occurrence,)

        async def occurrence_by_id(self, occurrence_id: UUID) -> object:
            assert occurrence_id == occurrence.id
            return occurrence

        async def occurrences(self, *_args: object, **_kwargs: object) -> object:
            return briefing_public.BriefingOccurrencePage((occurrence,))

    adapter = briefing_public.BriefingTaskSourceAdapter(
        Application(),
        owner_scope_id=owner_scope,
        issuer="issuer",
        subject="subject",
        schedule_id=occurrence.schedule_id,
    )
    capability = (
        __import__("aura_core.bootstrap.task_uow", fromlist=["task_projection_service"])
        .task_projection_service(testing=True, sources=(adapter,))
        ._capability
    )
    snapshot = await cast(Any, adapter).query_task_projection_snapshots(
        TaskProjectionSnapshotQuery.exact(TaskSourceKind.BRIEFING_OCCURRENCE, occurrence.id),
        capability=capability,
    )
    assert isinstance(snapshot, TaskProjectionSnapshot)
    assert snapshot.status is TaskNormalizedStatus.FAILED
    assert not snapshot.capabilities.can_retry
    detail = await cast(Any, adapter).resolve_task_detail(
        occurrence.id, owner_scope_id=owner_scope
    )
    assert detail["displayLabel"] == "Briefing occurrence 2"


@pytest.mark.asyncio
async def test_document_source_adapter_reads_repository_and_routes_retry() -> None:
    owner_scope = UUID("33333333-3333-3333-3333-333333333333")
    repository = document_public.MemoryDocumentRepository()
    failed = document_public.Document(
        id=uuid4(),
        issuer="issuer",
        subject="subject",
        filename="report.txt",
        media_type="text/plain",
        byte_count=1,
        digest="digest",
        object_key="objects/report",
        status=document_public.DocumentStatus.FAILED,
        version=2,
        created_at=NOW,
        updated_at=NOW,
        failure_code="parser",
    )
    await repository.create_upload(failed)

    class Application:
        async def get_for_task_projection(self, document_id: UUID) -> object:
            return await repository.get_any(document_id)

        async def list_for_task_projection(self, **kwargs: Any) -> tuple[object, ...]:
            return tuple(await cast(Any, repository).list_all(**kwargs))

        async def get(self, issuer: str, subject: str, document_id: UUID) -> object:
            return await repository.get((issuer, subject), document_id)

        async def list(self, issuer: str, subject: str, **kwargs: Any) -> list[Any]:
            return await cast(Any, repository).list((issuer, subject), **kwargs)

        async def retry(self, *_args: object, **_kwargs: object) -> object:
            return failed

    adapter = document_public.DocumentTaskSourceAdapter(
        Application(),
        owner_scope_id=owner_scope,
        issuer="issuer",
        subject="subject",
    )
    capability = (
        __import__("aura_core.bootstrap.task_uow", fromlist=["task_projection_service"])
        .task_projection_service(testing=True, sources=(adapter,))
        ._capability
    )
    snapshot = await cast(Any, adapter).query_task_projection_snapshots(
        TaskProjectionSnapshotQuery.exact(TaskSourceKind.DOCUMENT_PROCESSING, failed.id),
        capability=capability,
    )
    assert isinstance(snapshot, TaskProjectionSnapshot)
    assert snapshot.status is TaskNormalizedStatus.FAILED
    assert snapshot.capabilities.can_retry
    receipt = await cast(Any, adapter).command_task(
        failed.id,
        __import__(
            "aura_core.domains.execution.tasks.contracts", fromlist=["TaskCommand"]
        ).TaskCommand.RETRY,
        owner_scope_id=owner_scope,
        expected_source_version=failed.version,
        idempotency_key="retry-1",
    )
    assert receipt.state.value == "accepted"


@pytest.mark.asyncio
async def test_memory_source_adapter_reads_real_reindex_generation() -> None:
    owner_scope = UUID("44444444-4444-4444-4444-444444444444")
    store = memory_public.MemoryStore()
    generation = await store.register_embedding_generation(
        "issuer", "subject", generation=2, model_id="model", dimension=3
    )

    class Application:
        async def get_reindex_task(self, task_id: UUID) -> object:
            return await store.get_reindex_task(task_id)

        async def list_reindex_tasks(self, **kwargs: Any) -> tuple[object, ...]:
            return await store.list_reindex_tasks(**kwargs)

    adapter = memory_public.MemoryReindexTaskSourceAdapter(
        Application(),
        owner_scope_id=owner_scope,
        issuer="issuer",
        subject="subject",
    )
    capability = (
        __import__("aura_core.bootstrap.task_uow", fromlist=["task_projection_service"])
        .task_projection_service(testing=True, sources=(adapter,))
        ._capability
    )
    snapshot = await cast(Any, adapter).query_task_projection_snapshots(
        TaskProjectionSnapshotQuery.exact(TaskSourceKind.MEMORY_REINDEX, generation.id),
        capability=capability,
    )
    assert isinstance(snapshot, TaskProjectionSnapshot)
    assert snapshot.status is TaskNormalizedStatus.RUNNING
    assert snapshot.capabilities.can_retry is False


def test_public_snapshot_and_invalidation_shapes_cannot_carry_content() -> None:
    snapshot = _snapshot()
    event = _invalidation(snapshot).payload()
    assert set(event) == {
        "schemaVersion",
        "eventId",
        "eventType",
        "sourceKind",
        "sourceId",
        "sourceVersion",
        "occurredAt",
        "correlationId",
        "causationId",
    }
    public = snapshot.as_dict()
    assert {"question", "url", "excerpt", "report", "objectKey", "providerPayload"}.isdisjoint(
        public
    )
