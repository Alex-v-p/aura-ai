from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.postgres import (  # pyright: ignore[reportPrivateUsage]
    _advance_visibility_watermark,  # pyright: ignore[reportPrivateUsage]
)
from aura_observatory.adapters.query import ProductionObservatoryStore, StorageUnavailableError
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints.worker import IngestWorker
from aura_observatory.ingest import IngestReconciliationService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import (
    ClaimOutcome,
    ObservationIdRegistry,
    PendingObservationReceipt,
)
from aura_observatory.query import RunFilters, encode_cursor


class Registry:
    def __init__(self) -> None:
        self.receipts = [
            PendingObservationReceipt("present", 4),
            PendingObservationReceipt("missing", 5),
            PendingObservationReceipt("raced", 6),
        ]
        self.recovered: list[tuple[str, int]] = []

    async def stale_pending_receipts(self, *, limit: int, now: datetime | None = None):
        del now
        return self.receipts[:limit]

    async def reconcile_visible(self, observation_id: str, sequence: int) -> bool:
        self.recovered.append((observation_id, sequence))
        return observation_id != "raced"

    async def visibility_snapshot(self):
        return {
            "watermark": 4,
            "visibility_lag_count": 1,
            "backlog_count": 2,
            "oldest_backlog_age_ms": 10.0,
        }


async def _wait_for_event(event: asyncio.Event) -> None:
    await event.wait()


class Presence:
    async def existing_observations(
        self, receipts: Sequence[PendingObservationReceipt], *, timeout_seconds: float
    ) -> set[str]:
        assert timeout_seconds == 2.0
        assert [(item.observation_id, item.ingest_sequence) for item in receipts] == [
            ("present", 4),
            ("missing", 5),
            ("raced", 6),
        ]
        return {"present", "raced"}


@pytest.mark.asyncio
async def test_reconciliation_is_bounded_and_classifies_each_outcome() -> None:
    registry = Registry()
    metrics = ObservatoryMetrics()
    service = IngestReconciliationService(
        cast(Any, registry),
        Presence(),
        batch_size=3,
        timeout_seconds=2.0,
        metrics=metrics,
    )

    result = await service.reconcile_once(now=datetime.now(UTC) - timedelta(seconds=1))

    assert result["completed"] == 1
    assert result["missing"] == 1
    assert result["raced"] == 1
    assert result["failed"] == 0
    assert registry.recovered == [("present", 4), ("raced", 6)]
    metric_name = "observatory_ingest_reconciliation_count"
    assert metrics.values[(metric_name, (("outcome", "completed"),))] == 1
    assert metrics.values[(metric_name, (("outcome", "missing"),))] == 1
    assert metrics.values[(metric_name, (("outcome", "raced"),))] == 1
    recovery_spans = metrics.component_spans
    assert {span.component_id for span in recovery_spans} == {
        "aura.observatory.telemetry_ingestion",
        "aura.observatory.telemetry_storage",
    }
    assert {span.dependency for span in recovery_spans} >= {"postgresql", "clickhouse"}
    assert {span.operation for span in recovery_spans} == {"recover"}
    assert len({span.trace_id for span in recovery_spans}) == 1


@pytest.mark.asyncio
async def test_presence_probe_requires_matching_observation_id_and_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-id row with a different sequence must not recover a receipt."""

    receipts = [
        PendingObservationReceipt("a" * 32, 41),
        PendingObservationReceipt("b" * 32, 42),
    ]

    class Response:
        text = "\n".join(
            [
                json.dumps({"observation_id": "a" * 32, "ingest_sequence": 999}),
                json.dumps({"observation_id": "b" * 32, "ingest_sequence": 42}),
            ]
        )

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: object) -> Response:
            statement = kwargs["content"]
            assert isinstance(statement, str)
            assert "('" + "a" * 32 + "',41)" in statement
            assert "('" + "b" * 32 + "',42)" in statement
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(ObservationIdRegistry, object())
    )

    present = await store.existing_observations(receipts, timeout_seconds=2.0)

    assert present == {"b" * 32}


@pytest.mark.asyncio
async def test_crash_after_clickhouse_insert_recovers_without_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted provider row is recovered from its pending receipt alone."""

    observation = MetadataObservation.from_payload(
        {
            "schemaVersion": 1,
            "observationId": "00000000-0000-4000-8000-000000000041",
            "observedAt": "2026-01-01T00:00:00Z",
            "kind": "metric",
            "component": {"id": "aura.runtime.model_inference", "version": "1.0.0"},
            "operation": "infer",
            "measurement": {"name": "duration_ms", "value": 3.0, "unit": "milliseconds"},
            "dimensions": {"status": "succeeded"},
            "deployment": {
                "deploymentId": "deployment",
                "gitCommit": "0123456789abcdef0123456789abcdef01234567",
                "environment": "test",
                "hostId": "host",
            },
            "serviceRole": "api",
            "capturePolicy": "metadata_only",
        }
    )

    class Registry:
        def __init__(self) -> None:
            self.sequence = 41
            self.pending = True
            self.visible: list[int] = []
            self.completed = 0
            self.redelivery_calls = 0
            self.cancel_visibility_once = True

        async def claim(self, _observation_id: str) -> ClaimOutcome:
            return ClaimOutcome.ACQUIRED

        async def next_ingest_sequence(self, _observation_id: str) -> int:
            return self.sequence

        async def set_observed_at(self, _observation_id: str, _observed_at: datetime) -> None:
            return None

        async def commit_ingest_boundary(self, _observation_id: str) -> None:
            return None

        async def mark_ingest_visible(self, sequence: int) -> None:
            if self.cancel_visibility_once:
                self.cancel_visibility_once = False
                raise asyncio.CancelledError()
            self.visible.append(sequence)

        async def complete(self, _observation_id: str) -> None:
            self.pending = False
            self.completed += 1

        async def abort(self, _observation_id: str) -> None:
            # The accepted ClickHouse row remains durable while its receipt is
            # intentionally left pending for the Observatory recovery pass.
            self.pending = True

        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del now
            return (
                [PendingObservationReceipt(observation.observation_id, self.sequence)]
                if self.pending and limit
                else []
            )

        async def reconcile_visible(self, observation_id: str, sequence: int) -> bool:
            self.redelivery_calls += 1
            if (
                self.pending
                and observation_id == observation.observation_id
                and sequence == self.sequence
            ):
                self.pending = False
                self.visible.append(sequence)
                self.completed += 1
                return True
            return False

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {
                "watermark": max(self.visible, default=0),
                "visibility_lag_count": int(self.pending),
                "backlog_count": int(self.pending),
                "oldest_backlog_age_ms": 1.0 if self.pending else None,
            }

    class Response:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class Client:
        rows: set[tuple[str, int]] = set()
        insert_count = 0

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: object) -> Response:
            content = str(kwargs["content"])
            if content.startswith("INSERT INTO"):
                Client.insert_count += 1
                row = json.loads(content.split("\n", 1)[1])
                Client.rows.add((str(row["observation_id"]), int(row["ingest_sequence"])))
                return Response()
            body = "\n".join(
                json.dumps({"observation_id": identifier, "ingest_sequence": sequence})
                for identifier, sequence in Client.rows
            )
            return Response(body)

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    registry = Registry()
    store = ClickHouseObservationStore(
        "http://clickhouse.test", id_registry=cast(ObservationIdRegistry, registry)
    )

    with pytest.raises(asyncio.CancelledError):
        await store.insert_observation(observation)
    assert Client.insert_count == 1
    assert registry.pending is True
    assert registry.visible == []

    recovery = IngestReconciliationService(
        cast(Any, registry), store, batch_size=1, timeout_seconds=2.0
    )
    result = await recovery.reconcile_once(now=datetime.now(UTC))

    assert result["completed"] == 1
    assert result["missing"] == 0
    assert registry.redelivery_calls == 1
    assert registry.pending is False
    assert registry.completed == 1
    assert Client.insert_count == 1


@pytest.mark.asyncio
async def test_missing_clickhouse_row_stays_pending_and_redeliverable() -> None:
    class MissingRegistry:
        def __init__(self) -> None:
            self.receipt = PendingObservationReceipt("c" * 32, 43)
            self.commit_calls = 0

        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del now
            return [self.receipt] if limit else []

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            self.commit_calls += 1
            return True

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {"watermark": 42, "visibility_lag_count": 1, "backlog_count": 1}

    class MissingPresence:
        async def existing_observations(
            self,
            receipts: Sequence[PendingObservationReceipt],
            *,
            timeout_seconds: float,
        ) -> set[str]:
            assert len(receipts) == 1
            assert timeout_seconds == 1.0
            return set()

    registry = MissingRegistry()
    service = IngestReconciliationService(
        cast(Any, registry), MissingPresence(), batch_size=1, timeout_seconds=1.0
    )

    first = await service.reconcile_once()
    second = await service.reconcile_once()

    assert first["missing"] == second["missing"] == 1
    assert registry.commit_calls == 0
    # The receipt is still returned by the second pass: no lease renewal,
    # completion, visibility mark, or fabricated delivery occurred.
    assert (await registry.stale_pending_receipts(limit=1))[0] == registry.receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "dependency", "error_code"),
    [
        ("selection", "postgresql", "database_failure"),
        ("presence", "clickhouse", "transport_unavailable"),
        ("commit", "postgresql", "database_failure"),
    ],
)
async def test_reconciliation_failure_phases_emit_finite_causal_spans(
    phase: str, dependency: str, error_code: str
) -> None:
    class FailureRegistry:
        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del limit, now
            if phase == "selection":
                raise RuntimeError("private provider details")
            return [PendingObservationReceipt("d" * 32, 44)]

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            if phase == "commit":
                raise RuntimeError("private database details")
            return True

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {"watermark": 43, "visibility_lag_count": 1, "backlog_count": 1}

    class FailurePresence:
        async def existing_observations(
            self,
            receipts: Sequence[PendingObservationReceipt],
            *,
            timeout_seconds: float,
        ) -> set[str]:
            del receipts, timeout_seconds
            if phase == "presence":
                raise RuntimeError("private ClickHouse details")
            return {"d" * 32}

    metrics = ObservatoryMetrics()
    result = await IngestReconciliationService(
        cast(Any, FailureRegistry()),
        FailurePresence(),
        metrics=metrics,
        batch_size=1,
        timeout_seconds=1.0,
    ).reconcile_once()

    assert result["failed"] == 1
    spans = [
        span
        for span in metrics.component_spans
        if span.outcome == "failure" and span.dependency == dependency
    ]
    assert spans
    assert any(span.error_code == error_code for span in spans)
    for span in metrics.component_spans:
        assert span.component_id in {
            "aura.observatory.telemetry_ingestion",
            "aura.observatory.telemetry_storage",
        }
        assert span.component_version == "1.0.0"
        assert span.dependency in {"postgresql", "clickhouse"}
        assert span.error_code in {None, "database_failure", "transport_unavailable"}
        assert re.fullmatch(r"[0-9a-f]{32}", span.trace_id)
        assert re.fullmatch(r"[0-9a-f]{16}", span.span_id)


@pytest.mark.asyncio
async def test_reconciliation_timeout_then_provider_recovery_and_health_convergence() -> None:
    class Health:
        def __init__(self) -> None:
            self.visibility: list[tuple[int, int, int, float | None]] = []

        async def record_visibility(
            self,
            watermark: int,
            lag_count: int,
            backlog_count: int,
            oldest_backlog_age_ms: float | None,
        ) -> None:
            self.visibility.append(
                (watermark, lag_count, backlog_count, oldest_backlog_age_ms)
            )

    class DynamicRegistry:
        def __init__(self) -> None:
            self.calls = 0
            self.snapshots: list[dict[str, int | float | None]] = [
                {
                    "watermark": 40,
                    "visibility_lag_count": 1,
                    "backlog_count": 1,
                    "oldest_backlog_age_ms": 10.0,
                },
                {
                    "watermark": 41,
                    "visibility_lag_count": 0,
                    "backlog_count": 0,
                    "oldest_backlog_age_ms": None,
                },
            ]

        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del limit, now
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return []

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            return True

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]

    health = Health()
    registry = DynamicRegistry()
    metrics = ObservatoryMetrics()
    service = IngestReconciliationService(
        cast(Any, registry),
        Presence(),
        pass_timeout_seconds=0.01,
        health_port=cast(Any, health),
        metrics=metrics,
    )
    timed_out = await service.reconcile_once()
    assert timed_out["failed"] == 1
    assert any(
        span.outcome == "timeout" and span.error_code == "timeout"
        for span in metrics.component_spans
    )

    # Replace the blocked selection with a recovered provider path. The next
    # pass must remain usable and publish the converged lag/backlog state.
    async def recovered_stale(
        *, limit: int, now: datetime | None = None
    ) -> list[PendingObservationReceipt]:
        del limit, now
        registry.calls += 1
        return []

    registry.stale_pending_receipts = recovered_stale  # type: ignore[method-assign]
    recovered = await service.reconcile_once()
    assert recovered["failed"] == 0
    assert health.visibility[-1] == (41, 0, 0, None)
    assert metrics.values[("observatory_ingest_visibility_lag_count", ())] == 0
    assert metrics.values[("observatory_ingest_backlog_count", ())] == 0


@pytest.mark.asyncio
async def test_reconciliation_presence_timeout_attributes_clickhouse_phase() -> None:
    class Registry:
        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del limit, now
            return [PendingObservationReceipt("f" * 32, 47)]

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            return False

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {"watermark": 46, "visibility_lag_count": 1, "backlog_count": 1}

    class BlockingPresence:
        async def existing_observations(
            self,
            receipts: Sequence[PendingObservationReceipt],
            *,
            timeout_seconds: float,
        ) -> set[str]:
            del receipts, timeout_seconds
            await asyncio.Event().wait()
            return set()

    metrics = ObservatoryMetrics()
    result = await IngestReconciliationService(
        cast(Any, Registry()),
        BlockingPresence(),
        pass_timeout_seconds=0.01,
        metrics=metrics,
    ).reconcile_once()

    assert result["failed"] == 1
    assert any(
        span.outcome == "timeout"
        and span.dependency == "clickhouse"
        and span.error_code == "timeout"
        for span in metrics.component_spans
    )


@pytest.mark.asyncio
async def test_reconciliation_commit_timeout_attributes_postgresql_phase() -> None:
    class Registry:
        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del limit, now
            return [PendingObservationReceipt("g" * 32, 48)]

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            await asyncio.Event().wait()
            return True

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {"watermark": 47, "visibility_lag_count": 1, "backlog_count": 1}

    class Present:
        async def existing_observations(
            self,
            receipts: Sequence[PendingObservationReceipt],
            *,
            timeout_seconds: float,
        ) -> set[str]:
            del timeout_seconds
            return {receipts[0].observation_id}

    metrics = ObservatoryMetrics()
    result = await IngestReconciliationService(
        cast(Any, Registry()),
        Present(),
        pass_timeout_seconds=0.01,
        metrics=metrics,
    ).reconcile_once()

    assert result["failed"] == 1
    assert any(
        span.outcome == "timeout"
        and span.dependency == "postgresql"
        and span.error_code == "timeout"
        for span in metrics.component_spans
    )


@pytest.mark.asyncio
async def test_reconciliation_passes_have_distinct_traces_with_shared_phase_correlation() -> None:
    metrics = ObservatoryMetrics()
    service = IngestReconciliationService(
        cast(Any, Registry()),
        Presence(),
        metrics=metrics,
        batch_size=3,
        timeout_seconds=1.0,
    )

    await service.reconcile_once()
    first_count = len(metrics.component_spans)
    await service.reconcile_once()
    first = metrics.component_spans[:first_count]
    second = metrics.component_spans[first_count:]

    assert first and second
    first_trace_ids = {span.trace_id for span in first}
    second_trace_ids = {span.trace_id for span in second}
    assert len(first_trace_ids) == 1
    assert len(second_trace_ids) == 1
    assert first_trace_ids.isdisjoint(second_trace_ids)


@pytest.mark.asyncio
async def test_clickhouse_provider_failure_is_retryable_on_the_next_pass() -> None:
    class Registry:
        def __init__(self) -> None:
            self.completed = 0

        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del now
            return [PendingObservationReceipt("e" * 32, 46)] if limit else []

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            self.completed += 1
            return True

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {
                "watermark": 46 if self.completed else 45,
                "visibility_lag_count": 0 if self.completed else 1,
                "backlog_count": 0 if self.completed else 1,
            }

    class FlakyPresence:
        def __init__(self) -> None:
            self.calls = 0

        async def existing_observations(
            self,
            receipts: Sequence[PendingObservationReceipt],
            *,
            timeout_seconds: float,
        ) -> set[str]:
            del receipts
            assert timeout_seconds == 1.0
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("provider timeout")
            return {"e" * 32}

    registry = Registry()
    presence = FlakyPresence()
    service = IngestReconciliationService(
        cast(Any, registry), presence, batch_size=1, timeout_seconds=1.0
    )

    failed = await service.reconcile_once()
    recovered = await service.reconcile_once()

    assert failed["failed"] == 1
    assert recovered["completed"] == 1
    assert registry.completed == 1
    assert presence.calls == 2
@pytest.mark.asyncio
async def test_visibility_watermark_autonomously_converges_beyond_one_lock_budget() -> None:
    class Ledger:
        def __init__(self) -> None:
            self.visible = set(range(1, 2_001))
            self.watermark = 0
            self.checks = 0

        async def execute(self, statement: str, params: tuple[object, ...] = ()) -> Any:
            if "INSERT INTO observatory_ingest_visibility" in statement:
                self.visible.add(int(cast(int, params[0])))
            elif "UPDATE observatory_ingest_visibility SET visible" in statement:
                self.visible.add(int(cast(int, params[0])))
            elif "SELECT committed_sequence" in statement:
                return _Result((self.watermark,))
            elif "SELECT visible FROM observatory_ingest_visibility" in statement:
                self.checks += 1
                return _Result((int(cast(int, params[0])) in self.visible,))
            elif "UPDATE observatory_ingest_watermark" in statement:
                self.watermark = int(cast(int, params[0]))
            return _Result(None)

    ledger = Ledger()
    assert await _advance_visibility_watermark(ledger, 2_001)
    assert ledger.watermark == 1_024
    assert ledger.checks == 1_024

    # A periodic reconciliation pass can continue a quiescent tail without a
    # new producer or visibility event. The second bounded lock pass reaches
    # the authoritative end and does not skip the first missing sequence.
    assert not await _advance_visibility_watermark(ledger, None)
    assert ledger.watermark == 2_001
    assert ledger.checks == 2_002


@pytest.mark.asyncio
async def test_visibility_health_reports_visible_tail_until_continuation_converges() -> None:
    class Health:
        def __init__(self) -> None:
            self.samples: list[tuple[int, int]] = []

        async def record_visibility(
            self,
            watermark: int,
            lag_count: int,
            backlog_count: int,
            oldest_backlog_age_ms: float | None,
        ) -> None:
            del backlog_count, oldest_backlog_age_ms
            self.samples.append((watermark, lag_count))

    class TailRegistry:
        def __init__(self) -> None:
            self.watermark = 0
            self.tail = 2_001

        async def stale_pending_receipts(
            self, *, limit: int, now: datetime | None = None
        ) -> list[PendingObservationReceipt]:
            del limit, now
            return []

        async def reconcile_visible(self, _observation_id: str, _sequence: int) -> bool:
            return False

        async def advance_visibility(self) -> bool:
            self.watermark = min(self.tail, self.watermark + 1_024)
            return self.watermark < self.tail

        async def visibility_snapshot(self) -> dict[str, int | float | None]:
            return {
                "watermark": self.watermark,
                "visibility_lag_count": self.tail - self.watermark,
                "backlog_count": 0,
            }

    health = Health()
    registry = TailRegistry()
    metrics = ObservatoryMetrics()
    service = IngestReconciliationService(
        cast(Any, registry),
        Presence(),
        health_port=cast(Any, health),
        pass_timeout_seconds=1.0,
        metrics=metrics,
    )

    await service.reconcile_once()
    await service.reconcile_once()

    assert health.samples == [(1_024, 977), (2_001, 0)]
    assert len({span.trace_id for span in metrics.component_spans}) == 2


class _Result:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent_against_redelivery_and_retention_delete() -> None:
    class Connection:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.status = "pending"
            self.sequence = 45
            self.visible: set[int] = set()
            self.watermark = 44

        async def execute(self, statement: str, params: tuple[object, ...] = ()) -> _Result:
            if "pg_advisory_lock" in statement:
                await self.lock.acquire()
                return _Result(None)
            if "pg_advisory_unlock" in statement:
                if self.lock.locked():
                    self.lock.release()
                return _Result(None)
            if "SELECT status, ingest_sequence" in statement:
                if self.status == "deleted":
                    return _Result(None)
                return _Result((self.status, self.sequence))
            if "UPDATE observatory_observation_receipts" in statement:
                if self.status == "pending":
                    self.status = "completed"
                return _Result(None)
            if "INSERT INTO observatory_ingest_visibility" in statement:
                self.visible.add(int(cast(int, params[0])))
                return _Result(None)
            if "UPDATE observatory_ingest_visibility SET visible" in statement:
                self.visible.add(int(cast(int, params[0])))
                return _Result(None)
            if "SELECT committed_sequence" in statement:
                return _Result((self.watermark,))
            if "SELECT visible FROM observatory_ingest_visibility" in statement:
                return _Result((int(cast(int, params[0])) in self.visible,))
            if "UPDATE observatory_ingest_watermark" in statement:
                self.watermark = int(cast(int, params[0]))
                return _Result(None)
            return _Result(None)

    class Context:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context(connection)

    from aura_observatory.adapters.postgres import PostgresObservationIdRegistry

    registry = PostgresObservationIdRegistry(factory)
    first, second = await asyncio.gather(
        registry.reconcile_visible("observation-45", 45),
        registry.reconcile_visible("observation-45", 45),
    )
    assert sorted((first, second)) == [False, True]
    assert connection.status == "completed"
    assert connection.watermark == 45

    # A retention transaction taking the same advisory lock first must prevent
    # late recovery from recreating the deleted receipt or visibility row.
    connection.status = "pending"
    connection.visible.clear()
    connection.watermark = 44
    delete_started = asyncio.Event()

    async def delete_receipt() -> None:
        await connection.execute("SELECT pg_advisory_lock(830078)")
        delete_started.set()
        await asyncio.sleep(0)
        connection.status = "deleted"
        await connection.execute("SELECT pg_advisory_unlock(830078)")

    delete_task = asyncio.create_task(delete_receipt())
    await asyncio.wait_for(delete_started.wait(), timeout=1.0)
    recovery_task = asyncio.create_task(registry.reconcile_visible("observation-45", 45))
    await asyncio.gather(delete_task, recovery_task)
    assert recovery_task.result() is False
    assert connection.status == "deleted"
    assert connection.visible == set()
    assert connection.watermark == 44


@pytest.mark.asyncio
async def test_run_and_trace_details_keep_the_committed_fence_across_pages() -> None:
    run_id = "11111111-1111-4111-8111-111111111111"
    trace_id = "0123456789abcdef0123456789abcdef"
    queries: list[str] = []
    row_template: dict[str, Any] = {
        "schema_version": 1,
        "kind": "metric",
        "component_id": "aura.runtime.model_inference",
        "component_version": "1.0.0",
        "operation": "infer",
        "measurement_name": "duration_ms",
        "measurement_value": 3.0,
        "measurement_unit": "milliseconds",
        "dimensions": {"status": "succeeded"},
        "trace_id": trace_id,
        "span_id": "0123456789abcdef",
        "parent_span_id": "",
        "linked_span_ids": [],
        "run_id": run_id,
        "conversation_id": "",
        "deployment_id": "deployment",
        "git_commit": "0123456789abcdef0123456789abcdef01234567",
        "environment": "test",
        "host_id": "host",
        "service_role": "api",
        "capture_policy": "metadata_only",
    }
    rows: list[dict[str, Any]] = [
        {
            **row_template,
            "observation_id": "00000000-0000-4000-8000-000000000001",
            "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            **row_template,
            "observation_id": "00000000-0000-4000-8000-000000000002",
            "observed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            "span_id": "fedcba9876543210",
        },
    ]

    class Store(ProductionObservatoryStore):
        watermarks = iter((41, 99, 77))

        async def _committed_watermark(self) -> int | None:
            return next(self.watermarks)

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            return rows

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )

    first = await store.run_detail_query(run_id, limit=1)
    assert first is not None
    cursor = first["nextCursor"]
    assert isinstance(cursor, str)
    await store.run_detail_query(run_id, cursor=cursor, limit=1)
    await store.trace_detail_query(trace_id, limit=1)

    assert "ingest_sequence <= 41" in queries[0]
    assert "ingest_sequence <= 41" in queries[1]
    assert "ingest_sequence <= 99" not in queries[1]
    assert "ingest_sequence <= 77" in queries[2]


@pytest.mark.asyncio
async def test_detail_cursor_cannot_widen_a_fresh_visibility_fence() -> None:
    run_id = "11111111-1111-4111-8111-111111111111"
    observation_id = "00000000-0000-4000-8000-000000000001"
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _committed_watermark(self) -> int | None:
            return 42

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            return []

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    forged = encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), observation_id, 999999)

    await store.run_detail_query(run_id, cursor=forged)
    await store.trace_detail_query("0123456789abcdef0123456789abcdef", cursor=forged)

    assert all("ingest_sequence <= 42" in statement for statement in queries)
    assert all("999999" not in statement for statement in queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["run", "trace"])
async def test_detail_reads_fail_closed_when_watermark_is_unavailable(detail: str) -> None:
    class Store(ProductionObservatoryStore):
        async def _committed_watermark(self) -> int | None:
            raise RuntimeError("database is unavailable")

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            raise AssertionError("ClickHouse must not be queried without a visibility fence")

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    query = (
        store.run_detail_query(
            "11111111-1111-4111-8111-111111111111"
        )
        if detail == "run"
        else store.trace_detail_query("0123456789abcdef0123456789abcdef")
    )
    with pytest.raises(StorageUnavailableError):
        await query


@pytest.mark.asyncio
async def test_out_of_order_tail_keeps_run_list_detail_and_trace_on_one_snapshot() -> None:
    run_id = "22222222-2222-4222-8222-222222222222"
    trace_id = "abcdefabcdefabcdefabcdefabcdefab"
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "metric",
        "component_id": "aura.runtime.model_inference",
        "component_version": "1.0.0",
        "operation": "infer",
        "measurement_name": "duration_ms",
        "measurement_value": 4.0,
        "measurement_unit": "milliseconds",
        "dimensions": {"status": "succeeded"},
        "trace_id": trace_id,
        "span_id": "abcdefabcdefabcd",
        "parent_span_id": "",
        "linked_span_ids": [],
        "run_id": run_id,
        "conversation_id": "",
        "deployment_id": "deployment",
        "git_commit": "0123456789abcdef0123456789abcdef01234567",
        "environment": "test",
        "host_id": "host",
        "service_role": "api",
        "capture_policy": "metadata_only",
    }
    rows: list[dict[str, Any]] = [
        {
            **base,
            "observation_id": "00000000-0000-4000-8000-000000000101",
            "observed_at": observed_at,
            "ingest_sequence": 1,
        },
        {
            **base,
            "observation_id": "00000000-0000-4000-8000-000000000102",
            "observed_at": observed_at + timedelta(seconds=1),
            "ingest_sequence": 2,
            "span_id": "abcdefabcdefabce",
        },
    ]
    statements: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _committed_watermark(self) -> int | None:
            return 1

        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 1, "observed_at": observed_at}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            statements.append(statement)
            assert "ingest_sequence <= 1" in statement
            visible = [item for item in rows if item["ingest_sequence"] <= 1]
            if "GROUP BY run_id" in statement:
                item = visible[0]
                return [
                    {
                        "run_id": run_id,
                        "started_at": item["observed_at"],
                        "finished_at": item["observed_at"],
                        "status": "succeeded",
                        "model": None,
                        "deployment_id": "deployment",
                        "component_ids": ["aura.runtime.model_inference"],
                        "trace_id": trace_id,
                        "order_sequence": 1,
                    }
                ]
            return visible

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )

    listed = await store.run_query(RunFilters(), cursor=None, limit=10)
    detail = await store.run_detail_query(run_id)
    trace = await store.trace_detail_query(trace_id)

    assert len(listed["items"]) == 1
    assert listed["items"][0]["orderSequence"] == 1
    assert detail is not None
    assert len(detail["observations"]) == 1
    assert trace is not None
    assert len(trace["spans"]) == 1
    assert len(statements) == 3


@pytest.mark.asyncio
async def test_worker_starts_intake_and_stop_while_reconciliation_is_blocked() -> None:
    class Service:
        def __init__(self) -> None:
            self.ingested = asyncio.Event()
            self.heartbeats = 0

        async def ingest(self, payload: object, *, credential: str | None) -> None:
            del payload, credential
            self.ingested.set()

        async def record_worker_heartbeat(self) -> None:
            self.heartbeats += 1

    class Reconciler:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def reconcile_once(self) -> dict[str, int | float]:
            self.started.set()
            await asyncio.Event().wait()
            return {}

    service = Service()
    reconciler = Reconciler()
    worker = IngestWorker(
        cast(Any, service), cast(Any, reconciler), reconciliation_interval_seconds=3600.0
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(_wait_for_event(stop)))
    await asyncio.wait_for(reconciler.started.wait(), timeout=1.0)

    await worker.submit({"request": "still available"}, None)
    await asyncio.wait_for(service.ingested.wait(), timeout=1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert service.heartbeats >= 1


@pytest.mark.asyncio
async def test_worker_runs_periodic_recovery_and_restarts_cleanly() -> None:
    class Service:
        async def ingest(self, _payload: object, *, credential: str | None) -> None:
            del credential

        async def record_worker_heartbeat(self) -> None:
            return None

    class Reconciler:
        def __init__(self) -> None:
            self.calls = 0
            self.first = asyncio.Event()

        async def reconcile_once(self) -> dict[str, int | float]:
            self.calls += 1
            self.first.set()
            return {}

    service = Service()
    reconciler = Reconciler()
    worker = IngestWorker(
        cast(Any, service), cast(Any, reconciler), reconciliation_interval_seconds=0.01
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(_wait_for_event(stop)))
    await asyncio.wait_for(reconciler.first.wait(), timeout=1.0)
    await asyncio.sleep(0.04)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    first_run_calls = reconciler.calls
    assert first_run_calls >= 2

    # A fresh worker observes the same reconciler state and starts a new
    # startup/periodic loop without leaking the previous task.
    reconciler.first.clear()
    stop_again = asyncio.Event()
    restarted = IngestWorker(
        cast(Any, service), cast(Any, reconciler), reconciliation_interval_seconds=0.01
    )
    restarted_task = asyncio.create_task(restarted.run(_wait_for_event(stop_again)))
    await asyncio.wait_for(reconciler.first.wait(), timeout=1.0)
    stop_again.set()
    await asyncio.wait_for(restarted_task, timeout=1.0)
    assert reconciler.calls > first_run_calls
