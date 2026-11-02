"""Budget, fallback, and extension evaluations for governed research."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aura_core.domains.knowledge.research.public import (
    DEFAULT_BUDGETS,
    ResearchBudget,
    ResearchBudgetConsumption,
    ResearchConflict,
    ResearchCoordinator,
    ResearchMode,
    ResearchOutcome,
    ResearchValidationError,
)
from test_research_lifecycle_evaluation import OWNER, _providers, _service


@pytest.mark.asyncio
async def test_lowered_admission_budget_is_immutable_until_one_bounded_extension() -> None:
    service, _ = _service(_providers())
    lowered = ResearchBudget(60, 2, 1, 1, 2)
    admission = await service.admit(
        *OWNER,
        question="lowered budget",
        mode=ResearchMode.QUICK,
        budget=lowered,
    )
    assert admission.admitted_budget == lowered

    extended = await service.extend(
        *OWNER,
        admission.research_id,
        expected_version=admission.version,
        cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
    )
    assert extended.extension_accepted is True
    assert extended.admitted_budget == DEFAULT_BUDGETS[ResearchMode.QUICK]

    with pytest.raises(ResearchConflict):
        await service.extend(
            *OWNER,
            admission.research_id,
            expected_version=extended.version,
            cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
        )


@pytest.mark.asyncio
async def test_extension_idempotency_replays_after_service_recreation() -> None:
    providers = _providers()
    service, repository = _service(providers)
    lowered = ResearchBudget(60, 2, 1, 1, 2)
    admission = await service.admit(
        *OWNER, question="extension replay", mode=ResearchMode.QUICK, budget=lowered
    )
    task = await service.get(*OWNER, admission.research_id)
    first = await service.extend(
        *OWNER,
        task.id,
        expected_version=task.version,
        cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
        idempotency_key="extension-restart",
    )
    recreated = type(service)(repository, provider_clients=providers)
    replay = await recreated.extend(
        *OWNER,
        task.id,
        expected_version=task.version,
        cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
        idempotency_key="extension-restart",
    )
    assert replay == first


@pytest.mark.asyncio
async def test_idempotency_key_cannot_change_command_fingerprint() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(
        *OWNER,
        question="idempotency conflict",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )
    task = await service.get(*OWNER, admission.research_id)
    await service.extend(
        *OWNER,
        task.id,
        expected_version=task.version,
        cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
        idempotency_key="same-command-key",
    )
    with pytest.raises(ResearchConflict, match="idempotency key"):
        await service.cancel(
            *OWNER,
            task.id,
            expected_version=task.version,
            idempotency_key="same-command-key",
        )


@pytest.mark.asyncio
async def test_extension_uses_cumulative_deadline_envelope_from_task_creation() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    current = now

    def clock() -> datetime:
        return current

    service, _ = _service(_providers())
    service.clock = clock  # type: ignore[method-assign]
    lowered = ResearchBudget(60, 2, 1, 1, 2)
    admission = await service.admit(
        *OWNER,
        question="deadline extension",
        mode=ResearchMode.QUICK,
        budget=lowered,
    )
    task = await service.get(*OWNER, admission.research_id)
    assert task.deadline_at == now + timedelta(seconds=60)

    current = now + timedelta(seconds=30)
    extended = await service.extend(
        *OWNER,
        task.id,
        expected_version=task.version,
        cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
    )

    assert extended.deadline_at == task.created_at + timedelta(seconds=90)
    assert extended.deadline_at == current + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_one_second_remaining_bounds_search_and_fetch_request_deadlines() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    current = now
    providers = _providers()
    search_requests: list[object] = []
    fetch_requests: list[object] = []
    browser_requests: list[object] = []
    original_search = providers.search.search
    original_fetch = providers.static_fetch.fetch
    from aura_core.providers.research.contracts import FetchInadequateResult
    from test_research_lifecycle_evaluation import _Settings, _StaticFetch

    providers.static_fetch.result = FetchInadequateResult(
        outcome="inadequate_extraction",
        finalUrl="https://example.org/one",
        redirectCount=0,
        declaredMediaType="text/html",
        detectedMediaType="text/html",
        transferredBytes=32,
    )  # type: ignore[assignment]
    browser = _StaticFetch(_providers().static_fetch.result)
    providers.browser_fetch = browser
    providers.settings = _Settings(browser_fetch_enabled=True)
    original_browser_fetch = browser.fetch

    async def search(request: object):
        search_requests.append(request)
        return await original_search(request)

    async def fetch(request: object):
        fetch_requests.append(request)
        return await original_fetch(request)

    async def browser_fetch(request: object):
        browser_requests.append(request)
        return await original_browser_fetch(request)

    providers.search.search = search  # type: ignore[method-assign]
    providers.static_fetch.fetch = fetch  # type: ignore[method-assign]
    browser.fetch = browser_fetch  # type: ignore[method-assign]

    def clock() -> datetime:
        return current

    service, _ = _service(providers)
    service.clock = clock  # type: ignore[method-assign]
    admission = await service.admit(
        *OWNER, question="one second remaining", mode=ResearchMode.QUICK
    )
    current = now + timedelta(seconds=89)

    settled = await ResearchCoordinator(service, providers).run_once(admission.research_id)

    assert settled.outcome in {ResearchOutcome.COMPLETED, ResearchOutcome.PARTIAL}
    assert search_requests and fetch_requests and browser_requests
    assert search_requests[0].limits.deadline_ms <= 1_000
    assert fetch_requests[0].limits.deadline_ms <= 1_000
    assert browser_requests[0].limits.deadline_ms <= 1_000


@pytest.mark.asyncio
async def test_consumption_cannot_exceed_the_admitted_envelope() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(
        *OWNER,
        question="bounded counters",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )
    task = await service.get(*OWNER, admission.research_id)

    with pytest.raises(ResearchValidationError):
        await service.settle(
            task,
            ResearchOutcome.BUDGET_EXHAUSTED,
            consumed=ResearchBudgetConsumption(fetched_sources=3),
        )


@pytest.mark.asyncio
async def test_deadline_and_every_counter_are_bounded_at_settlement() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(
        *OWNER,
        question="all counters",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )
    task = await service.get(*OWNER, admission.research_id)
    for consumed in (
        ResearchBudgetConsumption(wall_time_seconds=61),
        ResearchBudgetConsumption(search_requests=2),
        ResearchBudgetConsumption(browser_pages=2),
        ResearchBudgetConsumption(model_calls=3),
    ):
        with pytest.raises(ResearchValidationError):
            await service.settle(task, ResearchOutcome.BUDGET_EXHAUSTED, consumed=consumed)


@pytest.mark.asyncio
async def test_wall_clock_deadline_stops_execution_before_provider_dispatch() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return now if clock_calls == 1 else now + timedelta(seconds=61)

    providers = _providers()
    repository_service, _ = _service(providers)
    service = type(repository_service)(
        repository_service.repository,
        provider_clients=providers,
        clock=clock,
    )
    admission = await service.admit(
        *OWNER,
        question="deadline",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )

    settled = await ResearchCoordinator(service, providers).run_once(admission.research_id)
    assert settled.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert providers.search.calls == 0


@pytest.mark.asyncio
async def test_wall_clock_expiry_has_exact_terminal_failure_labels() -> None:
    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
    )
    from aura_core.platform.telemetry import MetadataMetrics

    now = datetime(2026, 10, 30, tzinfo=UTC)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return now if calls == 1 else now + timedelta(seconds=61)

    providers = _providers()
    metrics = MetadataMetrics()
    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics, clock=clock
    )
    admission = await service.admit(
        *OWNER,
        question="exact deadline labels",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )

    settled = await ResearchCoordinator(service, providers, metrics=metrics).run_once(
        admission.research_id
    )

    assert settled.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert settled.failure_class == "timeout"
    records = [
        item
        for item in metrics.measurements
        if item.metric == "research_coordinate_outcome_count"
    ]
    assert records
    attributes = dict(records[-1].dimensions)
    assert attributes["outcome"] == "failed"
    assert attributes["status"] == "budget_exhausted"
    assert attributes["error_class"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_failure_classes_are_closed_to_the_approved_enum() -> None:
    from aura_core.domains.knowledge.research.public import ResearchValidationError

    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="failure enum", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)

    with pytest.raises(ResearchValidationError):
        await service.settle(
            task,
            ResearchOutcome.FAILED,
            failure_class="invented_failure_class",
        )


@pytest.mark.asyncio
async def test_absolute_deadline_is_checked_after_search_before_fetch_dispatch() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    elapsed = 0
    providers = _providers()
    original_search = providers.search.search

    def clock() -> datetime:
        return now + timedelta(seconds=elapsed)

    async def search(request: object):
        nonlocal elapsed
        result = await original_search(request)
        elapsed = 61
        return result

    providers.search.search = search  # type: ignore[method-assign]
    service, _ = _service(providers)
    service.clock = clock  # type: ignore[method-assign]
    admission = await service.admit(
        *OWNER,
        question="deadline after search",
        mode=ResearchMode.QUICK,
        budget=ResearchBudget(60, 2, 1, 1, 2),
    )

    settled = await ResearchCoordinator(service, providers).run_once(admission.research_id)

    assert settled.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert providers.search.calls == 1
    assert providers.static_fetch.calls == 0


@pytest.mark.asyncio
async def test_invalid_override_cannot_raise_operator_caps() -> None:
    service, _ = _service(_providers())
    with pytest.raises(ResearchValidationError):
        await service.admit(
            *OWNER,
            question="cap bypass",
            mode=ResearchMode.QUICK,
            budget=ResearchBudget(91, 6, 2, 1, 4),
        )


@pytest.mark.asyncio
async def test_extension_is_rejected_after_terminal_settlement() -> None:
    service, _ = _service(_providers())
    lowered = ResearchBudget(60, 2, 1, 1, 2)
    admission = await service.admit(
        *OWNER,
        question="terminal extension",
        mode=ResearchMode.QUICK,
        budget=lowered,
    )
    task = await service.get(*OWNER, admission.research_id)
    settled = await service.settle(
        task, ResearchOutcome.FAILED, failure_class="provider_unavailable"
    )

    with pytest.raises(ResearchConflict):
        await service.extend(
            *OWNER,
            settled.id,
            expected_version=settled.version,
            cumulative_budget=DEFAULT_BUDGETS[ResearchMode.QUICK],
        )


@pytest.mark.asyncio
async def test_cancel_command_replay_is_idempotent() -> None:
    from aura_core.domains.execution.tasks.contracts import TaskCommand

    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="cancel replay", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    first = await service.command_task(
        task.id,
        TaskCommand.CANCEL,
        owner_scope_id=task.owner_scope_id,
        expected_source_version=task.version,
        idempotency_key="cancel-replay",
    )
    second = await service.command_task(
        task.id,
        TaskCommand.CANCEL,
        owner_scope_id=task.owner_scope_id,
        expected_source_version=task.version,
        idempotency_key="cancel-replay",
    )
    assert first.source_version == second.source_version
    assert first.accepted is second.accepted


@pytest.mark.asyncio
async def test_mutation_command_idempotency_survives_service_recreation() -> None:
    from aura_core.domains.execution.tasks.contracts import TaskCommand

    providers = _providers()
    service, repository = _service(providers)
    admission = await service.admit(*OWNER, question="restart replay", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    first = await service.command_task(
        task.id,
        TaskCommand.CANCEL,
        owner_scope_id=task.owner_scope_id,
        expected_source_version=task.version,
        idempotency_key="restart-cancel",
    )

    recreated = type(service)(repository, provider_clients=providers)
    second = await recreated.command_task(
        task.id,
        TaskCommand.CANCEL,
        owner_scope_id=task.owner_scope_id,
        expected_source_version=task.version,
        idempotency_key="restart-cancel",
    )

    assert second.source_version == first.source_version
    assert second.accepted is first.accepted
