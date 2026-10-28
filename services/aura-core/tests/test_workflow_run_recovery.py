"""Repository-backed crash recovery checks for workflow agent runs."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from aura_core.domains.execution.runs.dto import RunStatus
from aura_core.domains.execution.runs.models import (
    WorkflowAgentRunOutcome,
    WorkflowAgentRunRequest,
    WorkflowAgentRunResult,
    WorkflowRunLineage,
)
from aura_core.domains.execution.runs.persistence import RunRow
from aura_core.domains.execution.runs.repository import SqlWorkflowAgentRunRecovery
from aura_core.domains.execution.runs.service import (
    RunCoordinator,
    workflow_agent_execution_deadline,
)
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.telemetry import MetadataMetrics
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _request(*, now: datetime | None = None) -> WorkflowAgentRunRequest:
    admitted_at = now or datetime.now(UTC).replace(microsecond=0)
    lineage = WorkflowRunLineage(
        uuid4(), uuid4(), uuid4(), uuid4(), 1, uuid4()
    )
    return WorkflowAgentRunRequest(
        lineage=lineage,
        agent_revision_id=uuid4(),
        model_policy_revision_id=uuid4(),
        provider="fixture-provider",
        model_id="fixture-model",
        instructions="Answer the bounded workflow step.",
        input_text="workflow input",
        admitted_at=admitted_at,
        # Deliberately exceed the provider bound: the coordinator must clamp
        # this at the truthful invocation start rather than the instance end.
        deadline=admitted_at + timedelta(days=30),
        issuer="https://issuer.example",
        subject="workflow-owner",
    )


def _result(
    request: WorkflowAgentRunRequest,
    *,
    outcome: WorkflowAgentRunOutcome,
    error_code: str | None,
) -> WorkflowAgentRunResult:
    started_at = request.admitted_at + timedelta(seconds=1)
    return WorkflowAgentRunResult(
        lineage=request.lineage,
        outcome=outcome,
        output="completed output" if outcome is WorkflowAgentRunOutcome.COMPLETED else None,
        error_code=error_code,
        trace_id=request.lineage.run_id.hex,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
    )


class _CancellationClaimRecovery:
    """Pause durable admission so cancellation can win before provider effect."""

    def __init__(self) -> None:
        self.admission_started = asyncio.Event()
        self.release_admission = asyncio.Event()
        self.canceled = False

    async def reserve_workflow_agent_run(
        self, request: WorkflowAgentRunRequest, *, started_at: datetime
    ) -> WorkflowAgentRunResult | None:
        self.admission_started.set()
        await self.release_admission.wait()
        if self.canceled:
            return _result(
                request, outcome=WorkflowAgentRunOutcome.CANCELED,
                error_code="WORKFLOW_AGENT_CANCELED",
            )
        return None

    async def settle_workflow_agent_run(
        self, request: WorkflowAgentRunRequest, result: WorkflowAgentRunResult
    ) -> None:
        del request, result

    async def cancel_workflow_agent_run(self, lineage: WorkflowRunLineage) -> bool:
        del lineage
        self.canceled = True
        return True


class _NeverInvokedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        raise AssertionError("provider effect must not start after durable cancellation claim")


@pytest_asyncio.fixture
async def recovery() -> Any:
    database_url = os.environ.get("AURA_0137_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0137_DATABASE_URL is required for workflow recovery coverage")
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
    try:
        yield SqlWorkflowAgentRunRecovery(sessions)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reservation_is_durable_before_provider_effect(recovery: Any) -> None:
    request = _request()
    started_at = request.admitted_at + timedelta(seconds=1)

    assert await recovery.reserve_workflow_agent_run(request, started_at=started_at) is None

    async with recovery.sessions() as session:
        row = await session.get(RunRow, request.lineage.run_id)
    assert row is not None
    assert row.status == RunStatus.RUNNING.value
    assert row.workflow_step_execution_id == request.lineage.workflow_step_execution_id
    assert row.started_at == started_at


@pytest.mark.asyncio
async def test_restart_returns_unknown_without_replaying_provider_effect(recovery: Any) -> None:
    request = _request()
    assert (
        await recovery.reserve_workflow_agent_run(
            request, started_at=request.admitted_at + timedelta(seconds=1)
        )
        is None
    )

    class Provider:
        calls = 0

        def stream_chat(self, *_args: object) -> Any:
            self.calls += 1
            raise AssertionError("provider effect must not be replayed after restart")

    provider = Provider()
    restarted = RunCoordinator(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        workflow_recovery=SqlWorkflowAgentRunRecovery(recovery.sessions),
    )
    result = await restarted.execute_workflow_agent_run(request, cast(Any, provider))

    assert result.outcome is WorkflowAgentRunOutcome.UNKNOWN
    assert result.error_code == "WORKFLOW_AGENT_UNKNOWN_OUTCOME"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_durable_terminal_failure_is_replayed(recovery: Any) -> None:
    request = _request()
    assert (
        await recovery.reserve_workflow_agent_run(
            request, started_at=request.admitted_at + timedelta(seconds=1)
        )
        is None
    )
    await recovery.settle_workflow_agent_run(
        request,
        _result(
            request,
            outcome=WorkflowAgentRunOutcome.FAILED,
            error_code="WORKFLOW_AGENT_TIMEOUT",
        ),
    )

    replay = await recovery.reserve_workflow_agent_run(
        request, started_at=request.admitted_at + timedelta(seconds=2)
    )

    assert replay is not None
    assert replay.outcome is WorkflowAgentRunOutcome.FAILED
    assert replay.error_code == "WORKFLOW_AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_cancellation_is_exact_lineage_and_terminal_replay_is_stable(recovery: Any) -> None:
    request = _request()
    assert (
        await recovery.reserve_workflow_agent_run(
            request, started_at=request.admitted_at + timedelta(seconds=1)
        )
        is None
    )
    wrong_lineage = WorkflowAgentRunRequest(
        lineage=WorkflowRunLineage(
            request.lineage.run_id,
            request.lineage.workflow_id,
            request.lineage.workflow_revision_id,
            request.lineage.workflow_instance_id,
            request.lineage.workflow_step_ordinal,
            uuid4(),
        ),
        agent_revision_id=request.agent_revision_id,
        model_policy_revision_id=request.model_policy_revision_id,
        provider=request.provider,
        model_id=request.model_id,
        instructions=request.instructions,
        input_text=request.input_text,
        admitted_at=request.admitted_at,
        deadline=request.deadline,
        issuer=request.issuer,
        subject=request.subject,
    )

    assert not await recovery.cancel_workflow_agent_run(wrong_lineage.lineage)
    assert await recovery.cancel_workflow_agent_run(request.lineage)
    replay = await recovery.reserve_workflow_agent_run(
        request, started_at=request.admitted_at + timedelta(seconds=2)
    )

    assert replay is not None
    assert replay.outcome is WorkflowAgentRunOutcome.CANCELED
    assert replay.error_code == "WORKFLOW_AGENT_CANCELED"


@pytest.mark.asyncio
async def test_cancellation_claim_wins_before_provider_side_effect() -> None:
    recovery = _CancellationClaimRecovery()
    coordinator = RunCoordinator(
        cast(Any, object()), cast(Any, object()), cast(Any, object()),
        workflow_recovery=cast(Any, recovery),
    )
    request = _request()
    provider = _NeverInvokedProvider()

    task = asyncio.create_task(
        coordinator.execute_workflow_agent_run(request, cast(Any, provider))
    )
    await recovery.admission_started.wait()
    assert await coordinator.cancel_workflow_agent_run(request.lineage)
    recovery.release_admission.set()
    result = await task

    assert result.outcome is WorkflowAgentRunOutcome.CANCELED
    assert result.error_code == "WORKFLOW_AGENT_CANCELED"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_provider_failure_uses_request_trace_and_closed_error_class() -> None:
    class Recovery:
        async def reserve_workflow_agent_run(
            self, request: WorkflowAgentRunRequest, *, started_at: datetime
        ) -> WorkflowAgentRunResult | None:
            del request, started_at
            return None

        async def settle_workflow_agent_run(
            self, request: WorkflowAgentRunRequest, result: WorkflowAgentRunResult
        ) -> None:
            del request, result

        async def cancel_workflow_agent_run(self, lineage: WorkflowRunLineage) -> bool:
            del lineage
            return False

    class Provider:
        async def stream_chat(self, *_args: object, **_kwargs: object) -> Any:
            raise RuntimeError("fixture provider failure")
            yield "unreachable"

    request = _request()
    request = WorkflowAgentRunRequest(
        lineage=request.lineage,
        agent_revision_id=request.agent_revision_id,
        model_policy_revision_id=request.model_policy_revision_id,
        provider=request.provider,
        model_id=request.model_id,
        instructions=request.instructions,
        input_text=request.input_text,
        admitted_at=request.admitted_at,
        deadline=request.deadline,
        issuer=request.issuer,
        subject=request.subject,
        trace_id="a" * 32,
    )
    metrics = MetadataMetrics(capacity=16)
    coordinator = RunCoordinator(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        metrics=metrics,
        workflow_recovery=cast(Any, Recovery()),
    )

    result = await coordinator.execute_workflow_agent_run(request, cast(Any, Provider()))

    assert result.outcome is WorkflowAgentRunOutcome.FAILED
    assert result.error_code == "WORKFLOW_AGENT_EXECUTION_FAILED"
    assert metrics.stats().rejected == 0
    measurements = metrics.snapshot()
    step_metric = next(
        item
        for item in measurements
        if item.kind == "metric"
        and item.metric == "workflow_agent_run_step_duration_ms"
    )
    run_span = next(
        item
        for item in measurements
        if item.kind == "span"
        and item.component_id == "aura.execution.run_coordinator"
    )
    assert step_metric.trace_id == request.trace_id
    assert run_span.trace_id == request.trace_id
    assert dict(run_span.dimensions)["error_class"] == "provider"


def test_provider_deadline_is_capped_from_truthful_start() -> None:
    started_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    instance_deadline = started_at + timedelta(days=30)
    earlier_deadline = started_at + timedelta(minutes=2)

    assert workflow_agent_execution_deadline(started_at, instance_deadline) == (
        started_at + timedelta(minutes=5)
    )
    assert workflow_agent_execution_deadline(started_at, earlier_deadline) == earlier_deadline
