"""Cancellation preserves an already durable terminal result."""

import os
from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from aura_core.domains.automation.briefings.application import BriefingRunStateAdapter
from aura_core.domains.automation.briefings.execution import (
    BriefingRunLineage,
    BriefingRunResult,
    BriefingRunStatus,
    SourceCompleteness,
)
from aura_core.domains.automation.briefings.public import (
    BriefingApplicationService,
    BriefingDailyCadence,
    BriefingExecutionPolicyReferences,
    BriefingResult,
    BriefingResultStatus,
    BriefingSourceCompleteness,
    BriefingSourceGrants,
    briefing_phase_span_id,
)
from aura_core.domains.automation.briefings.repository import MemoryBriefingRepository
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Settings
from aura_core.platform.telemetry import MetadataMetrics


@pytest.mark.asyncio
async def test_cancel_state_adapter_returns_authoritative_existing_terminal_result() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    owner = ("https://authentik.example", "briefing-owner")
    metrics = MetadataMetrics(capacity=16)
    repository = MemoryBriefingRepository(metrics=metrics)
    schedule = await repository.create_schedule(
        *owner,
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
        agent_revision_id=uuid4(),
        execution_policies=BriefingExecutionPolicyReferences(
            uuid4(), uuid4(), uuid4(), uuid4()
        ),
        idempotency_key="create",
        now=now,
    )
    occurrence = (await repository.list_occurrences(*owner, schedule.id)).items[0]
    run_id = uuid4()
    await repository.admit_occurrence(
        *owner,
        schedule.id,
        occurrence.id,
        run_id=run_id,
        expected_version=occurrence.version,
    )
    completed = BriefingResult(
        uuid4(),
        schedule.id,
        occurrence.id,
        *owner,
        run_id,
        BriefingResultStatus.COMPLETED,
        1,
        BriefingSourceCompleteness.COMPLETE,
        "already complete",
        now,
        now,
    )
    await repository.create_result(completed)

    state = BriefingRunStateAdapter(BriefingApplicationService(repository))
    result = await state.cancel_briefing_run(
        BriefingRunLineage(schedule.id, occurrence.id, run_id)
    )

    assert result.status is BriefingRunStatus.COMPLETED
    assert result.content == "already complete"
    assert result.durably_settled
    persistence_span = next(item for item in metrics.snapshot() if item.kind == "span")
    assert persistence_span.trace_id == run_id.hex
    assert persistence_span.span_id == briefing_phase_span_id(run_id, "persistence")
    assert persistence_span.parent_span_id == briefing_phase_span_id(run_id, "admission")
    assert metrics.stats().rejected == 0


def test_api_composition_binds_typed_briefing_cancellation_adapter() -> None:
    state = AppState(Settings(), testing=True)
    port = state.briefing_service.cancellation_port
    assert port is not None
    assert type(port).__name__ == "BriefingRunCancellationAdapter"
    assert port is not state.coordinator


@pytest.mark.asyncio
async def test_worker_completion_cannot_publish_after_canceled_state_wins() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    owner = ("https://authentik.example", "briefing-owner")
    repository = MemoryBriefingRepository()
    schedule = await repository.create_schedule(
        *owner,
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
        agent_revision_id=uuid4(),
        execution_policies=BriefingExecutionPolicyReferences(
            uuid4(), uuid4(), uuid4(), uuid4()
        ),
        idempotency_key="create-canceled",
        now=now,
    )
    occurrence = (await repository.list_occurrences(*owner, schedule.id)).items[0]
    run_id = uuid4()
    await repository.admit_occurrence(
        *owner,
        schedule.id,
        occurrence.id,
        run_id=run_id,
        expected_version=occurrence.version,
    )
    state = BriefingRunStateAdapter(BriefingApplicationService(repository))
    lineage = BriefingRunLineage(schedule.id, occurrence.id, run_id)
    await state.cancel_briefing_run(lineage)

    completion = await state.settle_briefing_run(
        BriefingRunResult(
            lineage,
            BriefingRunStatus.COMPLETED,
            SourceCompleteness.COMPLETE,
            "late completion",
            now,
            now,
        )
    )
    assert completion.status is BriefingRunStatus.CANCELED
    assert completion.content is None


@pytest.mark.asyncio
async def test_sql_briefing_spans_correlate_without_rejection() -> None:
    """Exercise the real SQL adapter when a disposable PostgreSQL URL is supplied."""

    database_url = os.environ.get("AURA_0133_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0133_DATABASE_URL is required for PostgreSQL briefing coverage")
    try:
        from alembic import command
        from aura_core.domains.automation.briefings.repository import SqlBriefingRepository
        from aura_core.entrypoints.cli import alembic_config
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as error:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {error}")

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as error:
        message = str(getattr(error, "orig", error)).lower()
        if any(
            marker in message
            for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(error).__name__}")
        raise

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    metrics = MetadataMetrics(capacity=32)
    repository = SqlBriefingRepository(sessions, metrics=metrics)
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    owner = ("https://authentik.example", f"briefing-sql-{uuid4().hex}")
    try:
        schedule = await repository.create_schedule(
            *owner,
            display_name="Daily SQL briefing",
            cadence=BriefingDailyCadence(time(9), "UTC"),
            sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
            agent_revision_id=uuid4(),
            execution_policies=BriefingExecutionPolicyReferences(
                uuid4(), uuid4(), uuid4(), uuid4()
            ),
            idempotency_key=uuid4().hex,
            now=now,
        )
        occurrence = (await repository.list_occurrences(*owner, schedule.id)).items[0]
        run_id = uuid4()
        await repository.admit_occurrence(
            *owner,
            schedule.id,
            occurrence.id,
            run_id=run_id,
            expected_version=occurrence.version,
        )
        result = BriefingResult(
            uuid4(),
            schedule.id,
            occurrence.id,
            *owner,
            run_id,
            BriefingResultStatus.COMPLETED,
            1,
            BriefingSourceCompleteness.COMPLETE,
            "SQL result content is not telemetry",
            now,
            now,
        )
        await repository.create_result(result)
        persistence_span = next(
            item
            for item in metrics.snapshot()
            if item.kind == "span"
            and item.component_id == "aura.automation.briefing_persistence"
        )
        assert persistence_span.trace_id == run_id.hex
        assert persistence_span.span_id == briefing_phase_span_id(run_id, "persistence")
        assert persistence_span.parent_span_id == briefing_phase_span_id(run_id, "admission")

        cancellation_schedule = await repository.create_schedule(
            *owner,
            display_name="Cancelable SQL briefing",
            cadence=BriefingDailyCadence(time(9), "UTC"),
            sources=BriefingSourceGrants(True, (), ()),
            agent_revision_id=uuid4(),
            execution_policies=BriefingExecutionPolicyReferences(
                uuid4(), uuid4(), uuid4(), uuid4()
            ),
            idempotency_key=uuid4().hex,
            now=now,
        )
        cancel_occurrence = (
            await repository.list_occurrences(*owner, cancellation_schedule.id)
        ).items[0]
        cancel_run_id = uuid4()
        await repository.admit_occurrence(
            *owner,
            cancellation_schedule.id,
            cancel_occurrence.id,
            run_id=cancel_run_id,
            expected_version=cancel_occurrence.version,
        )

        class Cancellation:
            async def cancel_briefing(self, lineage: object) -> object:
                del lineage
                return None

        await repository.delete_schedule(
            *owner,
            cancellation_schedule.id,
            expected_version=cancellation_schedule.version,
            idempotency_key=uuid4().hex,
            cancel_run=Cancellation(),
        )
        cancellation_span = next(
            item
            for item in metrics.snapshot()
            if item.kind == "span"
            and item.component_id == "aura.automation.briefing_cancellation"
        )
        assert cancellation_span.trace_id == cancel_run_id.hex
        assert cancellation_span.span_id == briefing_phase_span_id(cancel_run_id, "cancellation")
        assert cancellation_span.parent_span_id == briefing_phase_span_id(
            cancel_run_id, "admission"
        )
        assert metrics.stats().rejected == 0
    finally:
        await engine.dispose()
