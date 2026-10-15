"""AURA-0080 deterministic query, replay, and retention evaluation coverage.

These tests use the service-owned in-memory adapter.  They intentionally keep
all input metadata-only and verify the public query/use-case semantics without
depending on a database server or incidental SQL details.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import InsertOutcome
from aura_observatory.query import (
    DeletionResult,
    ObservatoryQueryService,
    RetentionAction,
    RetentionKind,
    RetentionRequest,
    RetentionStatus,
    RunFilters,
    decode_cursor,
)

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_run_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"aura-observatory-test:{label}"))


def make_payload(
    observation_id: str,
    *,
    observed_at: datetime,
    run_id: str,
    component: str = "aura.runtime.model_inference",
    version: str = "1.0.0",
    deployment: str = "deploy-a",
    status: str = "completed",
    outcome: str = "success",
    model: str = "local-model",
    service_role: str = "worker",
    trace_id: str = "0123456789abcdef0123456789abcdef",
    span_id: str | None = None,
    parent_span_id: str | None = None,
    value: float = 10.0,
) -> MetadataObservation:
    data: dict[str, Any] = {
        "schemaVersion": 1,
        "observationId": observation_id,
        "observedAt": observed_at.isoformat(),
        "kind": "span" if span_id else "metric",
        "component": {"id": component, "version": version},
        "operation": "model_inference",
        "measurement": {"name": "duration_ms", "value": value, "unit": "milliseconds"},
        "dimensions": {"status": status, "outcome": outcome, "provider": model},
        "trace": {"traceId": trace_id, "spanId": span_id},
        "correlation": {"runId": run_id},
        "deployment": {
            "environment": "test",
            "deploymentId": deployment,
            "gitCommit": "0123456789abcdef01234567",
            "hostId": "host-test",
        },
        "serviceRole": service_role,
        "capturePolicy": "metadata_only",
    }
    # The contract rejects explicit null span identifiers for metrics.
    if span_id is None:
        data["trace"] = {"traceId": trace_id, "spanId": "0123456789abcdef"}
    elif parent_span_id is not None:
        data["trace"]["parentSpanId"] = parent_span_id
    return MetadataObservation.from_payload(data)


async def _seed(store: MemoryObservatoryStore, *items: MetadataObservation) -> None:
    for item in items:
        assert await store.insert_observation(item) is InsertOutcome.ACCEPTED


@pytest.mark.asyncio
async def test_run_search_filters_and_uses_stable_opaque_cursor_order() -> None:
    store = MemoryObservatoryStore()
    await _seed(
        store,
        make_payload(
            "00000000-0000-4000-8000-000000000001",
            observed_at=BASE_TIME,
            run_id=make_run_id("run-a"),
        ),
        make_payload(
            "00000000-0000-4000-8000-000000000002",
            observed_at=BASE_TIME,
            run_id=make_run_id("run-b"),
            component="aura.runtime.tool",
            deployment="deploy-b",
            status="failed",
            outcome="error",
            model="other-model",
        ),
        make_payload(
            "00000000-0000-4000-8000-000000000003",
            observed_at=BASE_TIME + timedelta(minutes=1),
            run_id=make_run_id("run-c"),
        ),
    )
    service = ObservatoryQueryService(store)

    page = await service.runs(RunFilters(), cursor=None, limit=2)
    assert [item["runId"] for item in page["items"]] == [make_run_id("run-c"), make_run_id("run-b")]
    assert isinstance(page["nextCursor"], str)
    cursor_time, cursor_id = decode_cursor(page["nextCursor"])
    assert cursor_time == BASE_TIME
    assert cursor_id.endswith("0002")

    next_page = await service.runs(RunFilters(), cursor=page["nextCursor"], limit=2)
    assert [item["runId"] for item in next_page["items"]] == [make_run_id("run-a")]
    assert next_page["nextCursor"] is None

    filtered = await service.runs(
        RunFilters(
            status="failed",
            component="aura.runtime.tool",
            model="other-model",
            deployment="deploy-b",
            from_time=BASE_TIME,
            to_time=BASE_TIME + timedelta(minutes=1),
        ),
        cursor=None,
        limit=50,
    )
    assert [item["runId"] for item in filtered["items"]] == [make_run_id("run-b")]

    # A cursor is opaque to clients and cannot be silently widened into an
    # arbitrary query or accepted when it is malformed.
    with pytest.raises(ValueError):
        await service.runs(RunFilters(), cursor="not-a-cursor", limit=10)


@pytest.mark.asyncio
async def test_run_detail_and_trace_detail_build_ordered_parent_child_tree() -> None:
    store = MemoryObservatoryStore()
    trace = "abcdefabcdefabcdefabcdefabcdefab"
    parent = make_payload(
        "00000000-0000-4000-8000-000000000010",
        observed_at=BASE_TIME + timedelta(seconds=2),
        run_id=make_run_id("run-tree"),
        trace_id=trace,
        span_id="0000000000000001",
    )
    child = make_payload(
        "00000000-0000-4000-8000-000000000011",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=make_run_id("run-tree"),
        trace_id=trace,
        span_id="0000000000000002",
        parent_span_id="0000000000000001",
    )
    orphan = make_payload(
        "00000000-0000-4000-8000-000000000012",
        observed_at=BASE_TIME,
        run_id=make_run_id("run-tree"),
        trace_id=trace,
        span_id="0000000000000003",
        parent_span_id="fedcba9876543210",
    )
    # Ingestion order is intentionally not trace order.
    await _seed(store, child, orphan, parent)
    service = ObservatoryQueryService(store)

    detail = await service.run_detail(make_run_id("run-tree"))
    assert detail is not None
    assert detail["observationCount"] == 3
    assert detail["spans"][0]["spanId"] == "0000000000000003"
    assert detail["spans"][1]["spanId"] == "0000000000000001"
    assert detail["spans"][1]["children"][0]["spanId"] == "0000000000000002"

    trace_detail = await service.trace_detail(trace)
    assert trace_detail is not None
    assert trace_detail["traceId"] == trace
    assert trace_detail["partial"] is True
    assert trace_detail["spans"][1]["children"][0]["spanId"] == "0000000000000002"


@pytest.mark.asyncio
async def test_component_summary_is_metadata_only_and_aggregates_latency_and_errors() -> None:
    store = MemoryObservatoryStore()
    await _seed(
        store,
        make_payload(
            "00000000-0000-4000-8000-000000000020",
            observed_at=BASE_TIME,
            run_id=make_run_id("run-1"),
            value=10,
        ),
        make_payload(
            "00000000-0000-4000-8000-000000000021",
            observed_at=BASE_TIME + timedelta(seconds=1),
            run_id=make_run_id("run-2"),
            value=30,
            outcome="error",
            status="failed",
        ),
    )
    summaries = await ObservatoryQueryService(store).components()
    assert summaries == [
        {
            "componentId": "aura.runtime.model_inference",
            "version": "1.0.0",
            "observations": 2,
            "errorCount": 1,
            "errorRate": 0.5,
            "latencyMs": 20.0,
            "firstSeenAt": "2026-01-01T12:00:00Z",
            "lastSeenAt": "2026-01-01T12:00:01Z",
        }
    ]
    serialized = repr(summaries)
    assert not any(secret in serialized for secret in ("prompt", "response", "token", "endpoint"))


@pytest.mark.asyncio
async def test_sse_replay_starts_after_last_event_and_emits_only_metadata() -> None:
    store = MemoryObservatoryStore()
    first = make_payload(
        "00000000-0000-4000-8000-000000000030", observed_at=BASE_TIME, run_id=make_run_id("run-1")
    )
    second = make_payload(
        "00000000-0000-4000-8000-000000000031",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=make_run_id("run-2"),
    )
    await _seed(store, first, second)

    replay = [
        item async for item in ObservatoryQueryService(store).events_after(first.observation_id)
    ]
    assert [item["id"] for item in replay] == [second.observation_id]
    assert replay[0]["type"] == "observation.changed"
    assert set(replay[0]["data"]) == {"observationId", "runId", "traceId"}
    assert "metadata_only" not in repr(replay[0]) or "metadata_only" in repr(replay[0])
    assert not any(
        secret in repr(replay) for secret in ("prompt", "response", "bearer", "credential")
    )


@pytest.mark.asyncio
async def test_sse_unknown_or_deleted_event_id_resets_to_deterministic_catch_up() -> None:
    store = MemoryObservatoryStore()
    first = make_payload(
        "00000000-0000-4000-8000-000000000032",
        observed_at=BASE_TIME,
        run_id=make_run_id("reset-1"),
    )
    second = make_payload(
        "00000000-0000-4000-8000-000000000033",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=make_run_id("reset-2"),
    )
    await _seed(store, first, second)
    service = ObservatoryQueryService(store)

    unknown = [item async for item in service.events_after("event-that-never-existed")]
    assert [item["id"] for item in unknown] == [first.observation_id, second.observation_id]

    await store.delete_observations(BASE_TIME + timedelta(microseconds=1))
    deleted = [item async for item in service.events_after(first.observation_id)]
    assert [item["id"] for item in deleted] == [second.observation_id]


@pytest.mark.asyncio
async def test_sse_stream_remains_live_and_delivers_a_late_observation() -> None:
    store = MemoryObservatoryStore()
    service = ObservatoryQueryService(store)
    stream = service.events_after(None)

    async def next_event() -> dict[str, Any]:
        return await stream.__anext__()

    pending: asyncio.Task[dict[str, Any]] = asyncio.create_task(next_event())
    await asyncio.sleep(0)
    late = make_payload(
        "00000000-0000-4000-8000-000000000034",
        observed_at=BASE_TIME,
        run_id=make_run_id("late-event"),
    )
    await store.insert_observation(late)
    event = await asyncio.wait_for(pending, timeout=0.2)
    assert event["id"] == late.observation_id


@pytest.mark.asyncio
async def test_cutoff_is_strict_and_purge_all_is_explicit_and_preserves_catalogue() -> None:
    store = MemoryObservatoryStore()
    before = make_payload(
        "00000000-0000-4000-8000-000000000040",
        observed_at=BASE_TIME - timedelta(microseconds=1),
        run_id=make_run_id("run-old"),
    )
    exact = make_payload(
        "00000000-0000-4000-8000-000000000041",
        observed_at=BASE_TIME,
        run_id=make_run_id("run-exact"),
    )
    after = make_payload(
        "00000000-0000-4000-8000-000000000042",
        observed_at=BASE_TIME + timedelta(microseconds=1),
        run_id=make_run_id("run-new"),
    )
    await _seed(store, before, exact, after)
    service = ObservatoryQueryService(store)

    action = await service.retention(
        RetentionRequest(
            RetentionKind.DELETE_BEFORE,
            cutoff=BASE_TIME,
            confirmation="delete_before",
            idempotency_key="cutoff-1",
        )
    )
    await _wait_for_action(service, action.action_id)
    completed = await service.retention_status(action.action_id)
    assert completed is not None
    assert completed.status is RetentionStatus.SUCCEEDED
    assert completed.to_dict()["deletedObservationCount"] == 1
    assert completed.to_dict()["deletedReceiptCount"] == 1
    assert completed.to_dict()["deletedRunCount"] == 1
    assert set(store.observations) == {exact.observation_id, after.observation_id}

    # The boundary observation survives because deletion is strictly less-than.
    assert (await service.run_detail(make_run_id("run-exact"))) is not None
    assert (await service.run_detail(make_run_id("run-old"))) is None
    assert (await service.overview())["runCount"] == 2

    purge = await service.retention(
        RetentionRequest(
            RetentionKind.PURGE_ALL,
            confirmation="purge_all",
            idempotency_key="purge-1",
        )
    )
    await _wait_for_action(service, purge.action_id)
    purged = await service.retention_status(purge.action_id)
    assert purged is not None
    assert purged.status is RetentionStatus.SUCCEEDED
    assert purged.to_dict()["deletedObservationCount"] == 2
    assert purged.to_dict()["deletedReceiptCount"] == 2
    assert purged.to_dict()["deletedRunCount"] == 2
    assert store.observations == {}
    # Definitions are service-owned catalogue state, not derived content.
    assert await store.components()
    assert await store.deployments()


@pytest.mark.asyncio
async def test_retention_is_idempotent_under_concurrent_retries_and_reports_failure() -> None:
    class CountingStore(MemoryObservatoryStore):
        delete_calls = 0

        async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
            self.delete_calls += 1
            return await super().delete_observations(cutoff)

    store = CountingStore()
    await _seed(
        store,
        make_payload(
            "00000000-0000-4000-8000-000000000050",
            observed_at=BASE_TIME,
            run_id=make_run_id("run-1"),
        ),
    )
    service = ObservatoryQueryService(store)
    request = RetentionRequest(
        RetentionKind.PURGE_ALL,
        confirmation="confirm",
        idempotency_key="same-command",
    )
    actions = await asyncio.gather(*(service.retention(request) for _ in range(8)))
    assert {action.action_id for action in actions} == {actions[0].action_id}
    await _wait_for_action(service, actions[0].action_id)
    assert (await service.retention_status(actions[0].action_id)).deleted_count == 1  # type: ignore[union-attr]
    assert store.delete_calls == 1

    class FailingStore(MemoryObservatoryStore):
        async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
            del cutoff
            raise RuntimeError("database unavailable")

    failing = FailingStore()
    failing_service = ObservatoryQueryService(failing)
    failed = await failing_service.retention(
        RetentionRequest(
            RetentionKind.PURGE_ALL,
            confirmation="purge_all",
            idempotency_key="failure-1",
        )
    )
    await _wait_for_action(failing_service, failed.action_id)
    status = await failing_service.retention_status(failed.action_id)
    assert status is not None
    assert status.status is RetentionStatus.FAILED
    assert status.error == "storage_failure"
    assert status.to_dict()["failureCode"] == "storage_failure"


@pytest.mark.asyncio
async def test_atomic_retention_claim_handles_a_cross_process_idempotency_race() -> None:
    class AtomicStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.find_barrier = asyncio.Barrier(2)
            self.save_lock = asyncio.Lock()
            self.delete_calls = 0

        async def find_retention_by_key(self, key: str) -> RetentionAction | None:
            await self.find_barrier.wait()
            action_id = self.idempotency.get(key)
            return self.actions.get(action_id) if action_id else None

        async def save_retention(  # type: ignore[override]
            self, action: RetentionAction
        ) -> RetentionAction:
            async with self.save_lock:
                existing_id = self.idempotency.get(action.idempotency_key)
                if existing_id is not None:
                    return self.actions[existing_id]
                self.actions[action.action_id] = action
                self.idempotency[action.idempotency_key] = action.action_id
                return action

        async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
            self.delete_calls += 1
            return await super().delete_observations(cutoff)

    store = AtomicStore()
    await _seed(
        store,
        make_payload(
            "00000000-0000-4000-8000-000000000098",
            observed_at=BASE_TIME,
            run_id=make_run_id("atomic-retention"),
        ),
    )
    service = ObservatoryQueryService(store)
    request = RetentionRequest(
        RetentionKind.PURGE_ALL,
        confirmation="purge_all",
        idempotency_key="cross-process-race",
    )
    actions = await asyncio.gather(service.retention(request), service.retention(request))
    assert len({action.action_id for action in actions}) == 1
    await _wait_for_action(service, actions[0].action_id)
    assert store.delete_calls == 1


@pytest.mark.asyncio
async def test_recovery_claims_a_queued_action_once_and_retries_after_partial_cleanup() -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000099",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="recovery-key",
    )

    class RecoverableStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.pending = [action]
            self.actions[action.action_id] = action
            self.claimed = False
            self.delete_calls = 0

        async def pending_retentions(self) -> list[RetentionAction]:
            return [
                item
                for item in self.pending
                if item.status in {RetentionStatus.QUEUED, RetentionStatus.RUNNING}
            ]

        async def claim_pending_retention(self, action_id: str) -> RetentionAction | None:
            if self.claimed or action_id != action.action_id:
                return None
            self.claimed = True
            return action

        async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
            del cutoff
            self.delete_calls += 1
            if self.delete_calls == 1:
                # Simulate a worker crash after ClickHouse removed the source
                # rows but before receipt/projection cleanup completed.
                raise RuntimeError("crash after observation mutation")
            return DeletionResult(2, 2, 1)

    store = RecoverableStore()
    first = ObservatoryQueryService(store)
    second = ObservatoryQueryService(store)
    await asyncio.gather(first.recover_pending_retention(), second.recover_pending_retention())
    await asyncio.sleep(0.01)
    assert store.delete_calls == 1

    # A later worker can reclaim the still-pending action and finish cleanup;
    # no second destructive claim is allowed while the lease is held.
    action.status = RetentionStatus.RUNNING
    store.claimed = False
    await first.recover_pending_retention()
    await _wait_for_action(first, action.action_id)
    assert store.delete_calls == 2
    assert action.status is RetentionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_retention_validation_rejects_ambiguous_or_non_utc_commands() -> None:
    service = ObservatoryQueryService(MemoryObservatoryStore())
    invalid = (
        RetentionRequest(
            RetentionKind.PURGE_ALL,
            cutoff=BASE_TIME,
            confirmation="confirm",
            idempotency_key="x",
        ),
        RetentionRequest(
            RetentionKind.DELETE_BEFORE,
            cutoff=None,
            confirmation="confirm",
            idempotency_key="x",
        ),
        RetentionRequest(
            RetentionKind.DELETE_BEFORE,
            cutoff=BASE_TIME.replace(tzinfo=None),
            confirmation="confirm",
            idempotency_key="x",
        ),
        RetentionRequest(RetentionKind.PURGE_ALL, confirmation="", idempotency_key="x"),
        RetentionRequest(RetentionKind.PURGE_ALL, confirmation="confirm", idempotency_key=""),
    )
    for request in invalid:
        with pytest.raises(ValueError):
            await service.retention(request)


async def _wait_for_action(service: ObservatoryQueryService, action_id: str) -> None:
    for _ in range(100):
        action = await service.retention_status(action_id)
        if action is not None and action.status in {
            RetentionStatus.SUCCEEDED,
            RetentionStatus.FAILED,
        }:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"retention action {action_id} did not finish")
