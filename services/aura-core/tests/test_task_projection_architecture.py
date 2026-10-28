"""Architecture guards for the execution-owned Task Center projection."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.ports import ProjectionReadCapability
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
from aura_core.domains.execution.tasks.service import TaskProjectionService, TaskStreamEvent
from aura_core.entrypoints.api.routes import tasks as task_routes
from fastapi import HTTPException

TASK_ROOT = Path(__file__).parents[1] / "src" / "aura_core" / "domains" / "execution" / "tasks"
FORBIDDEN_IMPORT_MARKERS = (
    ".persistence",
    ".models",
    ".sql",
    ".tables",
    ".orm",
    "domains.artifacts",
    "domains.automation",
    "domains.knowledge",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_projection_modules_do_not_import_source_domains_or_persistence_mappings() -> None:
    for path in TASK_ROOT.glob("*.py"):
        imports = _imports(path)
        assert not [
            item for item in imports if any(marker in item for marker in FORBIDDEN_IMPORT_MARKERS)
        ], path


def test_source_enumeration_capability_cannot_be_constructed_or_delegated() -> None:
    with pytest.raises(TypeError):
        ProjectionReadCapability()
    import aura_core.domains.execution.tasks.ports as ports

    assert "PROJECTION_CAPABILITY" not in getattr(ports, "__all__", ())
    assert not hasattr(ports, "PROJECTION_CAPABILITY")
    assert not hasattr(ports, "_new_projection_capability")


def test_projection_service_does_not_accept_caller_capability_or_snapshot_truth() -> None:
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    reconcile = inspect.signature(TaskProjectionService.reconcile).parameters
    rebuild = inspect.signature(TaskProjectionService.rebuild).parameters
    assert "capability" not in reconcile
    assert "capability" not in rebuild
    assert "snapshot" not in reconcile
    assert "snapshot" not in rebuild


def test_execution_projection_repository_cannot_author_source_invalidations() -> None:
    repository = MemoryTaskProjectionRepository()
    assert not hasattr(repository, "append_source_invalidation")
    assert not hasattr(repository, "replay_source_invalidations")
    from aura_core.domains.execution.tasks import sql_repository

    source = Path(sql_repository.__file__).read_text(encoding="utf-8")
    assert "task_source_invalidations" not in source


def test_projection_boundary_contains_no_table_or_orm_access() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in TASK_ROOT.glob("*.py")).lower()
    assert "__table__" not in source
    assert "select(" not in source
    assert "foreignkey(" not in source


def _row(owner_scope_id: object) -> TaskProjectionSnapshot:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    return TaskProjectionSnapshot(
        owner_scope_id=owner_scope_id,  # type: ignore[arg-type]
        source_kind=TaskSourceKind.WORKFLOW_INSTANCE,
        source_id=uuid4(),
        source_version=4,
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


def test_owner_facing_summary_serializer_is_exact_and_content_free() -> None:
    row = _row(uuid4())
    payload = task_routes._payload(row)  # pyright: ignore[reportPrivateUsage]
    assert set(payload) == {
        "id",
        "sourceKind",
        "sourceId",
        "sourceVersion",
        "status",
        "sourceStatus",
        "sourceOutcome",
        "stage",
        "progress",
        "capabilities",
        "routeKey",
        "createdAt",
        "updatedAt",
    }
    assert "ownerScopeId" not in payload
    assert "schemaVersion" not in payload
    assert "sourceDeleted" not in payload


def test_owner_facing_detail_serializer_declares_required_public_fields() -> None:
    payload = _row(uuid4()).as_detail(
        display_label="Workflow", recovery_action=None, artifact_refs=()
    )
    assert set(payload) == {
        "id",
        "sourceKind",
        "sourceId",
        "sourceVersion",
        "status",
        "sourceStatus",
        "sourceOutcome",
        "stage",
        "progress",
        "capabilities",
        "routeKey",
        "createdAt",
        "updatedAt",
        "displayLabel",
        "failureClass",
        "recoveryAction",
        "artifactRefs",
    }
    assert "ownerScopeId" not in payload


def test_sse_endpoint_has_cursor_replay_and_gap_reconciliation_parameters() -> None:
    parameters = inspect.signature(task_routes.stream_task_events).parameters
    assert "last_event_id" in parameters
    source = Path(task_routes.__file__).read_text(encoding="utf-8").lower()
    assert "last-event-id" in source
    assert "replay" in source
    assert "gap" in source
    assert "no-store" in source


@pytest.mark.asyncio
async def test_sse_rejects_cross_owner_stream_before_starting_transport() -> None:
    row = _row(uuid4())
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(row)
    service = TaskProjectionService(repository)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(aura=SimpleNamespace(task_projection_service=service))
        )
    )
    session = SimpleNamespace(
        principal=SimpleNamespace(issuer="https://issuer.example", subject="other-owner")
    )
    with pytest.raises(HTTPException):
        await cast(Any, task_routes.stream_task_events)(
            cast(Any, request), row.source_id, session=cast(Any, session), last_event_id=None
        )


@pytest.mark.asyncio
async def test_sse_replays_after_last_event_id_and_emits_rest_reconcile_gap() -> None:
    session = SimpleNamespace(
        principal=SimpleNamespace(issuer="https://issuer.example", subject="owner")
    )
    owner = task_routes._owner(cast(Any, session))  # pyright: ignore[reportPrivateUsage]
    row = _row(owner)
    repository = MemoryTaskProjectionRepository()
    await repository.upsert(row)
    service = TaskProjectionService(repository)
    first = TaskStreamEvent(
        uuid4(), row.source_id, row.source_kind, row.source_id, 4, row.updated_at
    )
    second = TaskStreamEvent(
        uuid4(), row.source_id, row.source_kind, row.source_id, 5, row.updated_at
    )
    # Replay must come from the durable repository boundary so reconnects work
    # across API/worker process instances; the service-local deque is not the
    # source of truth.
    await repository.append_task_event(owner, first)
    await repository.append_task_event(owner, second)

    async def is_disconnected() -> bool:
        return True

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(aura=SimpleNamespace(task_projection_service=service))
        ),
        is_disconnected=is_disconnected,
    )
    replay = await cast(Any, task_routes.stream_task_events)(
        cast(Any, request), row.source_id, session=cast(Any, session), last_event_id=first.event_id
    )
    chunk = await anext(replay.body_iterator)
    text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
    assert str(second.event_id) in text
    assert str(first.event_id) not in text

    gap = await cast(Any, task_routes.stream_task_events)(
        cast(Any, request), row.source_id, session=cast(Any, session), last_event_id=uuid4()
    )
    gap_chunk = await anext(gap.body_iterator)
    gap_text = gap_chunk.decode() if isinstance(gap_chunk, bytes) else str(gap_chunk)
    assert "task.gap" in gap_text
    assert '"reconcile":true' in gap_text


@pytest.mark.asyncio
async def test_task_events_replay_across_service_instances_from_repository() -> None:
    from aura_core.domains.execution.tasks.contracts import (
        TaskSourceInvalidation,
        TaskSourceKind,
    )

    owner = uuid4()
    source_id = uuid4()
    now = datetime(2026, 10, 28, tzinfo=UTC)
    snapshot = TaskProjectionSnapshot(
        owner_scope_id=owner,
        source_kind=TaskSourceKind.MEMORY_REINDEX,
        source_id=source_id,
        source_version=1,
        status=TaskNormalizedStatus.RUNNING,
        source_status="running",
        source_outcome=None,
        stage="reindexing",
        progress=0.25,
        capabilities=TaskProjectionCapability(),
        route_key="memory_reindex_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
    )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, query: object, *, capability: object
        ) -> object:
            del query, capability
            return snapshot

        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError

        async def resolve_task_detail(
            self, source_id: object, *, owner_scope_id: object
        ) -> dict[str, object]:
            del source_id, owner_scope_id
            return {}

    repository = MemoryTaskProjectionRepository()
    first = TaskProjectionService(repository, [Source()])  # pyright: ignore[reportArgumentType]
    await first.reconcile(
        TaskSourceInvalidation(
            TaskSourceKind.MEMORY_REINDEX,
            source_id,
            1,
            uuid4(),
            now,
            uuid4(),
            uuid4(),
        )
    )
    second = TaskProjectionService(repository)
    events = await second.replay_events(owner, TaskSourceKind.MEMORY_REINDEX, source_id)
    assert len(events) == 1
    assert events[0].source_version == 1
