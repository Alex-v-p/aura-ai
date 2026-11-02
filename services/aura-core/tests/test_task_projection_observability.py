"""Metadata-only instrumentation and replay contract checks."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from aura_core.domains.execution.tasks.contracts import (
    TASK_PROJECTION_COMPONENT,
    TaskProjectionSnapshot,
)
from aura_core.platform import telemetry

TASK_ROOT = Path(__file__).parents[1] / "src" / "aura_core" / "domains" / "execution" / "tasks"


def test_projection_component_is_stable_and_versioned() -> None:
    assert TASK_PROJECTION_COMPONENT == "aura.execution.task_projection@1"
    manifest = yaml.safe_load(
        (
            Path(__file__).parents[1] / "resources" / "component-manifests" / "task-projection.yaml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["component"] == {
        "id": TASK_PROJECTION_COMPONENT,
        "version": "1.0.0",
        "owner": "aura-core.domains.execution.tasks",
        "category": "projection",
    }
    assert manifest["span"] == "task_projection.reconcile"
    assert set(manifest["metrics"]) == {
        "task_projection_outcome_count",
        "task_projection_reconcile_latency_ms",
        "task_projection_lag_ms",
    }
    assert manifest["capture_policy"] == "metadata_only"
    assert manifest["replay"] == {
        "mode": "deterministic",
        "input": "identifier_and_version_only",
        "clock": "injected",
        "random": "prohibited",
    }


def test_projection_telemetry_catalogue_is_registered_with_exact_contract() -> None:
    metrics = {
        "task_projection_outcome_count",
        "task_projection_reconcile_latency_ms",
        "task_projection_lag_ms",
    }
    assert telemetry.COMPONENT_VERSIONS[TASK_PROJECTION_COMPONENT] == "1.0.0"
    assert metrics <= telemetry._METRICS[TASK_PROJECTION_COMPONENT]  # pyright: ignore[reportPrivateUsage]
    assert "task_projection.reconcile" in telemetry._SPAN_OPERATIONS[TASK_PROJECTION_COMPONENT]  # pyright: ignore[reportPrivateUsage]
    assert telemetry._SPAN_METRICS[TASK_PROJECTION_COMPONENT] == (  # pyright: ignore[reportPrivateUsage]
        "task_projection_reconcile_latency_ms"
    )

    sink = telemetry.MetadataMetrics()
    sink.increment(
        TASK_PROJECTION_COMPONENT,
        "task_projection_outcome_count",
        outcome="success",
        operation="reconcile",
    )
    sink.observe(
        TASK_PROJECTION_COMPONENT,
        "task_projection_lag_ms",
        7,
        outcome="success",
        operation="reconcile",
    )
    sink.record_span(
        TASK_PROJECTION_COMPONENT,
        "task_projection.reconcile",
        3,
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id=None,
        dependency="source_snapshot_api",
        outcome="success",
        metric_name="task_projection_reconcile_latency_ms",
    )
    records = sink.snapshot()
    assert {record.metric for record in records} == metrics
    assert sink.stats().rejected == 0
    assert all("owner" not in repr(record).lower() for record in records)


def test_runtime_projection_recorder_accepts_fake_clock_and_closed_outcomes() -> None:
    from inspect import signature

    from aura_core.domains.execution.tasks.service import TaskProjectionService

    parameters = signature(TaskProjectionService).parameters
    assert "metrics" in parameters
    assert "clock" in parameters


@pytest.mark.asyncio
async def test_rebuild_terminal_branches_emit_one_observation_and_fresh_spans() -> None:
    from aura_core.domains.execution.tasks.contracts import (
        TaskNormalizedStatus,
        TaskProjectionCapability,
        TaskProjectionSnapshot,
        TaskProjectionSnapshotPage,
        TaskProjectionSnapshotQuery,
        TaskSourceKind,
    )
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    owner = UUID("11111111-1111-1111-1111-111111111111")
    source_id = uuid4()
    snapshot = TaskProjectionSnapshot(
        owner,
        TaskSourceKind.MEMORY_REINDEX,
        source_id,
        1,
        TaskNormalizedStatus.RUNNING,
        "running",
        None,
        "reindexing",
        0.5,
        TaskProjectionCapability(),
        "memory_reindex_detail",
        None,
        datetime.now(UTC),
        datetime.now(UTC),
    )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> object:
            del query, capability
            return TaskProjectionSnapshotPage((snapshot,), None, True)

        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError

        async def resolve_task_detail(
            self, source_id: UUID, *, owner_scope_id: UUID
        ) -> dict[str, object]:
            del source_id, owner_scope_id
            return {}

    class FailingRepository(MemoryTaskProjectionRepository):
        async def tombstone_stale(self, *args: object, **kwargs: object) -> int:
            del args, kwargs
            raise RuntimeError("settlement failed")

    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        FailingRepository(),
        cast(Any, (Source(),)),
        metrics=metrics,
        trace_id="a" * 32,
        span_id="b" * 16,
    )
    with pytest.raises(RuntimeError):
        await service.rebuild(TaskSourceKind.MEMORY_REINDEX)

    records = metrics.snapshot()
    terminal = [
        record
        for record in records
        if record.metric == "task_projection_outcome_count"
    ]
    spans = [record for record in records if record.kind == "span"]
    assert len(terminal) == 1
    assert dict(terminal[0].dimensions)["outcome"] == "failed"
    assert len(spans) == 1
    assert spans[0].trace_id == "a" * 32
    assert spans[0].span_id != "b" * 16

    no_source_metrics = telemetry.MetadataMetrics()
    no_source = TaskProjectionService(
        MemoryTaskProjectionRepository(), metrics=no_source_metrics
    )
    assert await no_source.rebuild(TaskSourceKind.MEMORY_REINDEX) == 0
    no_source_records = no_source_metrics.snapshot()
    assert len(
        [record for record in no_source_records if record.metric == "task_projection_outcome_count"]
    ) == 1
    assert len([record for record in no_source_records if record.kind == "span"]) == 1


@pytest.mark.asyncio
async def test_rebuild_repository_read_failure_emits_one_storage_observation() -> None:
    from aura_core.domains.execution.tasks.contracts import (
        TaskNormalizedStatus,
        TaskProjectionCapability,
        TaskProjectionSnapshot,
        TaskProjectionSnapshotPage,
        TaskSourceKind,
    )
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    now = datetime.now(UTC)
    snapshot = TaskProjectionSnapshot(
        uuid4(),
        TaskSourceKind.MEMORY_REINDEX,
        uuid4(),
        1,
        TaskNormalizedStatus.RUNNING,
        "running",
        None,
        "reindexing",
        0.5,
        TaskProjectionCapability(),
        "memory_reindex_detail",
        None,
        now,
        now,
    )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, *_args: object, **_kwargs: object
        ) -> object:
            return TaskProjectionSnapshotPage((snapshot,), None, True)

    class ReadFailingRepository(MemoryTaskProjectionRepository):
        async def get(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionError("projection read unavailable")

    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        ReadFailingRepository(), cast(Any, (Source(),)), metrics=metrics
    )
    with pytest.raises(ConnectionError):
        await service.rebuild(TaskSourceKind.MEMORY_REINDEX)

    terminal = [
        record
        for record in metrics.snapshot()
        if record.metric == "task_projection_outcome_count"
    ]
    assert len(terminal) == 1
    assert dict(terminal[0].dimensions)["outcome"] == "failed"
    assert dict(terminal[0].dimensions)["error_class"] == "storage_unavailable"
    assert len([record for record in metrics.snapshot() if record.kind == "span"]) == 1


@pytest.mark.asyncio
async def test_rebuild_quarantine_failure_emits_one_storage_observation() -> None:
    from aura_core.domains.execution.tasks.contracts import (
        TaskNormalizedStatus,
        TaskProjectionCapability,
        TaskProjectionSnapshot,
        TaskProjectionSnapshotPage,
        TaskSourceKind,
    )
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    now = datetime.now(UTC)
    snapshot = TaskProjectionSnapshot(
        uuid4(),
        TaskSourceKind.MEMORY_REINDEX,
        uuid4(),
        1,
        TaskNormalizedStatus.RUNNING,
        "running",
        None,
        "reindexing",
        0.5,
        TaskProjectionCapability(),
        "memory_reindex_detail",
        None,
        now,
        now,
    )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, *_args: object, **_kwargs: object
        ) -> object:
            return TaskProjectionSnapshotPage((snapshot, snapshot), None, True)

    class QuarantineFailingRepository(MemoryTaskProjectionRepository):
        async def enqueue_reconciliation(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionError("reconciliation queue unavailable")

    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        QuarantineFailingRepository(), cast(Any, (Source(),)), metrics=metrics
    )
    with pytest.raises(ConnectionError):
        await service.rebuild(TaskSourceKind.MEMORY_REINDEX)

    terminal = [
        record
        for record in metrics.snapshot()
        if record.metric == "task_projection_outcome_count"
    ]
    assert len(terminal) == 1
    assert dict(terminal[0].dimensions)["outcome"] == "failed"
    assert dict(terminal[0].dimensions)["error_class"] == "storage_unavailable"
    assert len([record for record in metrics.snapshot() if record.kind == "span"]) == 1


def test_missing_context_mints_unique_ids_across_service_instances() -> None:
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    measurements: list[telemetry.Measurement] = []
    for _ in range(2):
        metrics = telemetry.MetadataMetrics()
        service = TaskProjectionService(MemoryTaskProjectionRepository(), metrics=metrics)
        service._record("reconcile", 1.0, outcome="success")  # pyright: ignore[reportPrivateUsage]
        measurements.append(next(record for record in metrics.snapshot() if record.kind == "span"))

    first, second = measurements
    assert first.trace_id is not None and second.trace_id is not None
    assert first.span_id is not None and second.span_id is not None
    assert first.trace_id != second.trace_id
    assert first.span_id != second.span_id
    assert first.parent_span_id is None
    assert second.parent_span_id is None


def test_admitted_context_provider_is_evaluated_per_operation() -> None:
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    contexts = iter(
        (
            ("a" * 32, "1" * 16, None),
            ("a" * 32, "2" * 16, None),
        )
    )
    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        MemoryTaskProjectionRepository(),
        metrics=metrics,
        trace_context_provider=lambda: next(contexts),
    )
    service._record("reconcile", 1.0, outcome="success")  # pyright: ignore[reportPrivateUsage]
    service._record("reconcile", 1.0, outcome="success")  # pyright: ignore[reportPrivateUsage]

    spans = [record for record in metrics.snapshot() if record.kind == "span"]
    assert [span.trace_id for span in spans] == ["a" * 32, "a" * 32]
    assert [span.parent_span_id for span in spans] == ["1" * 16, "2" * 16]
    assert spans[0].span_id != spans[1].span_id


def test_admitted_lineage_serializes_to_otlp_without_null_ids() -> None:
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        MemoryTaskProjectionRepository(),
        metrics=metrics,
        trace_context_provider=lambda: ("b" * 32, "c" * 16, None),
    )
    service._record("reconcile", 1.0, outcome="success")  # pyright: ignore[reportPrivateUsage]
    span = next(record for record in metrics.snapshot() if record.kind == "span")
    exporter = telemetry.OtlpHttpTelemetryExporter("https://collector.example")
    payload = json.loads(exporter._traces_payload((span,)))  # pyright: ignore[reportPrivateUsage]
    serialized = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert serialized["traceId"] == "b" * 32
    assert serialized["spanId"] == span.span_id
    assert serialized["parentSpanId"] == "c" * 16
    assert None not in serialized.values()


@pytest.mark.asyncio
async def test_runtime_projection_metrics_separate_lag_from_active_reconcile_latency() -> None:
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

    now = [datetime(2026, 10, 28, 12, 0, tzinfo=UTC)]
    owner = UUID("11111111-1111-1111-1111-111111111111")

    def snapshot(source_id: UUID, version: int = 1) -> TaskProjectionSnapshot:
        return TaskProjectionSnapshot(
            owner_scope_id=owner,
            source_kind=TaskSourceKind.MEMORY_REINDEX,
            source_id=source_id,
            source_version=version,
            status=TaskNormalizedStatus.RUNNING,
            source_status="running",
            source_outcome=None,
            stage="reindexing",
            progress=0.5,
            capabilities=TaskProjectionCapability(),
            route_key="memory_reindex_detail",
            failure_class=None,
            created_at=now[0],
            updated_at=now[0],
        )

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        def __init__(self, result: object) -> None:
            self.result = result

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
            del query, capability
            if isinstance(self.result, BaseException):
                raise self.result
            return cast(TaskProjectionSnapshot | TaskProjectionSnapshotPage | None, self.result)

        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("commands are outside reconciliation instrumentation")

        async def resolve_task_detail(
            self, source_id: UUID, *, owner_scope_id: UUID
        ) -> dict[str, object]:
            del source_id, owner_scope_id
            return {}

    metrics = telemetry.MetadataMetrics()
    source_id = uuid4()
    source = Source(snapshot(source_id))
    service = TaskProjectionService(
        MemoryTaskProjectionRepository(),
        cast(Any, [source]),
        metrics=metrics,
        clock=lambda: now[0],
    )

    async def reconcile(result: object, *, version: int = 1) -> None:
        source.result = result
        now[0] += timedelta(milliseconds=25)
        event = TaskSourceInvalidation(
            TaskSourceKind.MEMORY_REINDEX,
            source_id,
            version,
            uuid4(),
            now[0] - timedelta(milliseconds=40),
            uuid4(),
            uuid4(),
        )
        await service.reconcile(event)

    await reconcile(snapshot(source_id))
    await reconcile(snapshot(source_id, version=2), version=2)
    await reconcile(None, version=3)
    await reconcile(TimeoutError("source timeout"), version=4)
    await reconcile(ConnectionError("source unavailable"), version=5)
    await reconcile(RuntimeError("source failure"), version=6)
    await reconcile(TaskProjectionSnapshotPage((), None, True), version=7)

    outcomes = {
        dict(record.dimensions).get("outcome")
        for record in metrics.snapshot()
        if record.metric == "task_projection_outcome_count"
    }
    expected_outcomes = {
        "success",
        "unknown",
        "failed",
        "rejected",
        "timeout",
    }
    assert metrics.stats().rejected == 0
    assert expected_outcomes <= outcomes
    assert outcomes <= expected_outcomes
    active = [
        record.value
        for record in metrics.snapshot()
        if record.metric == "task_projection_reconcile_latency_ms"
    ]
    lag = [
        record.value for record in metrics.snapshot() if record.metric == "task_projection_lag_ms"
    ]
    assert active and lag
    assert max(lag) > max(active)
    assert all(
        not any(key.endswith("_id") for key, _ in record.dimensions)
        for record in metrics.snapshot()
    )


def test_snapshot_serialization_is_metadata_only_and_replayable() -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    from aura_core.domains.execution.tasks.contracts import (
        TaskNormalizedStatus,
        TaskProjectionCapability,
        TaskSourceKind,
    )

    snapshot = TaskProjectionSnapshot(
        owner_scope_id=UUID("11111111-1111-1111-1111-111111111111"),
        source_kind=TaskSourceKind.MEMORY_REINDEX,
        source_id=UUID("22222222-2222-2222-2222-222222222222"),
        source_version=1,
        status=TaskNormalizedStatus.QUEUED,
        source_status="pending",
        source_outcome=None,
        stage="queued",
        progress=0,
        capabilities=TaskProjectionCapability(),
        route_key="memory_reindex_detail",
        failure_class=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload = snapshot.as_dict()
    assert payload == snapshot.as_dict()
    assert all("owner" not in key.lower() or key == "ownerScopeId" for key in payload)
    assert {
        "question",
        "query",
        "url",
        "excerpt",
        "content",
        "providerPayload",
        "objectKey",
    }.isdisjoint(payload)


def test_projection_instrumentation_uses_no_content_fields_or_unbounded_labels() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in TASK_ROOT.glob("*.py"))
    tree = ast.parse(source)
    forbidden = {
        "question",
        "query",
        "url",
        "excerpt",
        "content",
        "provider_payload",
        "object_key",
        "title",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in {"observe", "increment", "record_span"}:
                continue
            keyword_names = {item.arg for item in node.keywords if item.arg is not None}
            assert keyword_names.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_rebuild_mixed_page_keeps_late_boundary_error_source_classification() -> None:
    """A valid item must not taint a later malformed item’s observation."""

    from aura_core.domains.execution.tasks.contracts import (
        TaskNormalizedStatus,
        TaskProjectionCapability,
        TaskProjectionSnapshot,
        TaskProjectionSnapshotPage,
        TaskProjectionSnapshotQuery,
        TaskSourceKind,
    )
    from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
    from aura_core.domains.execution.tasks.service import TaskProjectionService

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    valid = TaskProjectionSnapshot(
        owner_scope_id=uuid4(),
        source_kind=TaskSourceKind.MEMORY_REINDEX,
        source_id=uuid4(),
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

    class Source:
        kind = TaskSourceKind.MEMORY_REINDEX

        async def query_task_projection_snapshots(
            self, query: TaskProjectionSnapshotQuery, *, capability: object
        ) -> object:
            del query, capability
            return TaskProjectionSnapshotPage(cast(Any, (valid, object())), None, True)

        async def command_task(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError

        async def resolve_task_detail(
            self, source_id: UUID, *, owner_scope_id: UUID
        ) -> dict[str, object]:
            del source_id, owner_scope_id
            return {}

    metrics = telemetry.MetadataMetrics()
    service = TaskProjectionService(
        MemoryTaskProjectionRepository(),
        cast(Any, (Source(),)),
        metrics=metrics,
        clock=lambda: now,
    )
    with pytest.raises(TypeError):
        await service.rebuild(TaskSourceKind.MEMORY_REINDEX)

    outcomes = [
        record
        for record in metrics.snapshot()
        if record.metric == "task_projection_outcome_count"
    ]
    spans = [record for record in metrics.snapshot() if record.kind == "span"]
    assert len(outcomes) == 1
    assert dict(outcomes[0].dimensions)["outcome"] == "rejected"
    assert dict(outcomes[0].dimensions)["error_class"] == "validation_failed"
    assert dict(outcomes[0].dimensions)["dependency"] == "source_snapshot_api"
    assert len(spans) == 1
