import asyncio
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.automation.reminders.public import (
    RecurrenceFrequency,
    RecurrenceRule,
    ReminderSeriesIdempotencyConflict,
    ReminderSeriesStatus,
    ReminderSeriesTransitionError,
    ReminderSeriesVersionConflict,
)
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository
from aura_core.domains.execution.runs.public import (
    ChatMessage,
    RunClaimLost,
    RunCoordinator,
)
from aura_core.domains.governance.approvals.public import (
    Approval,
    ApprovalExecutionStatus,
    ApprovalStatus,
)
from aura_core.domains.interaction.conversations.public import (
    ConversationStore,
    MessageState,
)
from aura_core.domains.interaction.conversations.public import (
    Message as ConversationMessage,
)
from aura_core.entrypoints.api.state import AppState
from aura_core.entrypoints.worker.app import (  # type: ignore[reportPrivateUsage]
    _resume_approval,  # pyright: ignore[reportPrivateUsage]
    make_worker_telemetry,
    run_once,
)
from aura_core.platform.auth import Settings
from aura_core.platform.outbox import InMemoryOutbox, OutboxCommand
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.telemetry import (
    COMPONENT_VERSIONS,
    CompositeTelemetryExporter,
    DeploymentMetadata,
    MetadataMetrics,
    OtlpHttpTelemetryExporter,
    StructuredContainerLogExporter,
    TelemetryBatch,
    TelemetryLifecycle,
    new_span_id,
    record_memory_retrieval,
    root_span_id,
)
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import ProviderTraceContext
from aura_core.runtime.streaming.publisher import EventPublisher


def test_metadata_metrics_is_bounded_and_separates_dimensions_from_trace_fields() -> None:
    metrics = MetadataMetrics(capacity=2)
    run_id, conversation_id = uuid4(), uuid4()
    metadata = {
        "trace_id": run_id.hex,
        "run_id": str(run_id),
        "conversation_id": str(conversation_id),
    }

    metrics.increment("aura.execution.run_coordinator", "run_started", **metadata)
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        provider="ollama",
        model_id="llama3.2:latest",
        **metadata,
    )
    metrics.increment("aura.runtime.stream_delivery", "sse_connections", **metadata)
    metrics.increment(
        "aura.runtime.stream_delivery",
        "errors",
        prompt="private conversation text",
        **metadata,
    )
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        provider="https://secret-bearing-endpoint.invalid",
        model_id="fake",
        **metadata,
    )

    assert [item.metric for item in metrics.snapshot()] == ["model_routed", "sse_connections"]
    routed = metrics.snapshot()[0]
    assert dict(routed.dimensions) == {"provider": "ollama"}
    assert dict(routed.trace_attributes) == {
        "conversation_id": str(conversation_id),
        "model_id": "llama3.2:latest",
        "run_id": str(run_id),
    }
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    assert metrics.stats().dropped == 1
    assert metrics.stats().rejected == 2


def test_notification_delivery_metrics_are_registered_and_content_free() -> None:
    metrics = MetadataMetrics()
    scheduler_metrics = (
        "reminder_scheduler_scan_duration_ms",
        "reminder_due_lag_ms",
        "reminder_fired_count",
        "reminder_duplicate_prevention_count",
        "reminder_fire_persistence_outcome",
        "reminder_failure_count",
        "reminder_fire_duration_ms",
    )
    notification_count_metrics = (
        "notification_sse_connection_count",
        "notification_sse_replay_count",
        "notification_acknowledgement_outcome",
        "notification_failure_count",
    )
    notification_duration_metrics = (
        "notification_sse_duration_ms",
        "notification_sse_replay_duration_ms",
        "notification_acknowledgement_duration_ms",
    )
    for name in scheduler_metrics:
        metrics.observe("aura.automation.reminder_scheduler", name, 1.0, outcome="ok")
    for name in notification_count_metrics:
        metrics.increment("aura.automation.notification_delivery", name, outcome="ok")
    for name in notification_duration_metrics:
        metrics.observe("aura.automation.notification_delivery", name, 1.0, outcome="ok")
    retained = metrics.snapshot()
    assert {item.metric for item in retained} == set(
        scheduler_metrics + notification_count_metrics + notification_duration_metrics
    )
    assert all("content" not in repr(item).lower() for item in retained)
    assert metrics.stats().rejected == 0


def test_recurring_reminder_metrics_are_registered_and_reject_private_dimensions() -> None:
    metrics = MetadataMetrics()
    required = (
        "reminder_series_command_duration_ms",
        "reminder_series_command_outcome",
        "reminder_recurrence_calculation_outcome",
        "reminder_occurrence_generated_count",
        "reminder_occurrence_skipped_count",
        "reminder_occurrence_missed_count",
        "reminder_series_version_conflict_count",
        "reminder_series_idempotency_conflict_count",
        "reminder_series_due_lag_ms",
    )
    scheduler_owned = {
        "reminder_occurrence_generated_count",
        "reminder_occurrence_skipped_count",
        "reminder_occurrence_missed_count",
        "reminder_series_due_lag_ms",
    }
    for name in required:
        component = (
            "aura.automation.reminder_scheduler"
            if name in scheduler_owned
            else "aura.automation.reminder_management"
        )
        if name.endswith("duration_ms") or name.endswith("lag_ms"):
            metrics.observe(
                component,
                name,
                1.0,
                outcome="ok",
            )
        else:
            metrics.increment(
                component,
                name,
                outcome="ok",
            )
    retained = metrics.snapshot()
    assert {item.metric for item in retained} == set(required)
    assert all(
        item.component_id == (
            "aura.automation.reminder_scheduler"
            if item.metric in scheduler_owned
            else "aura.automation.reminder_management"
        )
        for item in retained
    )
    assert not any(
        item.component_id == "aura.automation.reminder_management"
        and item.metric in scheduler_owned
        for item in retained
    )
    for item in retained:
        attributes = repr((*item.dimensions, *item.trace_attributes)).lower()
        assert not any(
            forbidden in attributes
            for forbidden in ("content", "recurrence", "timezone", "local_time", "due_at", "owner")
        )
    assert metrics.stats().rejected == 0


def test_conversation_branching_metrics_are_registered_and_content_free() -> None:
    metrics = MetadataMetrics()
    required = (
        "conversation_branch_creation_outcome",
        "conversation_branch_load_outcome",
        "conversation_regeneration_outcome",
    )
    for name in required:
        metrics.increment(
            "aura.interaction.conversation_persistence"
            if name != "conversation_regeneration_outcome"
            else "aura.execution.run_coordinator",
            name,
            outcome="ok",
        )
    retained = metrics.snapshot()
    assert {item.metric for item in retained} == set(required)
    assert all("content" not in repr(item).lower() for item in retained)
    assert all("prompt" not in repr(item).lower() for item in retained)
    assert metrics.stats().rejected == 0


def test_conversation_branching_outcomes_retain_error_replay_and_denial_records() -> None:
    metrics = MetadataMetrics()
    cases = (
        ("conversation_branch_load_outcome", "error", "invalid_cursor"),
        ("conversation_branch_load_outcome", "not_found", "not_found"),
        ("conversation_branch_load_outcome", "denied", "authorization"),
        ("conversation_branch_creation_outcome", "idempotent", "idempotency"),
        ("conversation_branch_creation_outcome", "error", "persistence"),
        ("conversation_regeneration_outcome", "ok", None),
        ("conversation_regeneration_outcome", "conflict", "conflict"),
        ("conversation_regeneration_outcome", "error", "persistence"),
    )
    for metric, outcome, error_class in cases:
        component = (
            "aura.execution.run_coordinator"
            if metric == "conversation_regeneration_outcome"
            else "aura.interaction.conversation_persistence"
        )
        dimensions = {"outcome": outcome}
        if error_class is not None:
            dimensions["error_class"] = error_class
        metrics.increment(component, metric, **dimensions)

    retained = metrics.snapshot()
    assert metrics.stats().rejected == 0
    assert {
        (item.metric, dict(item.dimensions).get("outcome"))
        for item in retained
        if item.metric in {
            "conversation_branch_load_outcome",
            "conversation_branch_creation_outcome",
            "conversation_regeneration_outcome",
        }
    } == {(metric, outcome) for metric, outcome, _ in cases}
    assert all(
        not any(
            secret in repr(item).lower()
            for secret in ("message", "prompt", "response", "content")
        )
        for item in retained
    )


def test_conversation_branching_required_evaluators_are_declared_on_affected_manifests() -> None:
    manifest_root = Path(__file__).parents[1] / "resources" / "component-manifests"
    affected = {
        "aura.interaction.conversation_persistence",
        "aura.execution.run_coordinator",
        "aura.runtime.stream_delivery",
    }
    required = {
        "conversation_branch_context_isolation",
        "conversation_regeneration_evidence_freshness",
    }
    declared: set[str] = set()
    for path in manifest_root.glob("*.yaml"):
        document = cast(dict[str, object], yaml.safe_load(path.read_text()))
        component = cast(dict[str, object], document.get("component", {}))
        if component.get("id") not in affected:
            continue
        declared.update(cast(list[str], document.get("evaluators", [])))
    assert required <= declared


@pytest.mark.asyncio
async def test_recurring_repository_runtime_metrics_cover_success_conflict_error_and_firing(
) -> None:
    secret = "private runtime reminder content"
    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    metrics = MetadataMetrics()
    reminders = MemoryReminderRepository(clock=lambda: clock[0], metrics=metrics)
    rule = RecurrenceRule(RecurrenceFrequency.DAILY)
    series = await reminders.create_series(
        "https://authentik.example",
        "telemetry-owner",
        content=secret,
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=rule,
        idempotency_key="00000000-0000-4000-8000-000000000064",
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await reminders.create_series(
            "https://authentik.example",
            "telemetry-owner",
            content="changed private content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=rule,
            idempotency_key="00000000-0000-4000-8000-000000000064",
        )
    with pytest.raises(ValueError):
        await reminders.create_series(
            "https://authentik.example",
            "telemetry-owner",
            content=secret,
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Not/A-Timezone",
            rule=rule,
            idempotency_key="00000000-0000-4000-8000-000000000065",
        )
    notifications = MemoryNotificationRepository(
        reminders,
        clock=lambda: datetime(2026, 1, 5, 10, tzinfo=UTC),
        metrics=metrics,
    )
    result = await notifications.deliver_due(limit=10)
    assert result.fired_count == 1
    assert series.next_reminder_id is not None

    names = {item.metric for item in metrics.snapshot()}
    assert {
        "reminder_series_command_duration_ms",
        "reminder_series_command_outcome",
        "reminder_recurrence_calculation_outcome",
        "reminder_occurrence_generated_count",
        "reminder_occurrence_skipped_count",
        "reminder_occurrence_missed_count",
        "reminder_series_idempotency_conflict_count",
        "reminder_fired_count",
    } <= names
    rendered = repr(metrics.snapshot()).lower()
    assert secret not in rendered
    assert "changed private content" not in rendered
    private_attributes = repr(
        [
            (dict(item.dimensions), dict(item.trace_attributes))
            for item in metrics.snapshot()
        ]
    ).lower()
    assert all(
        field not in private_attributes
        for field in (
            "content",
            "recurrence",
            "timezone",
            "local_start",
            "due_at",
            "owner",
        )
    )


@pytest.mark.asyncio
async def test_recurring_command_telemetry_correlates_lifecycle_outcomes_and_terminal_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    metrics = MetadataMetrics()
    repository = MemoryReminderRepository(clock=lambda: clock[0], metrics=metrics)
    rule = RecurrenceRule(RecurrenceFrequency.DAILY)
    key = "00000000-0000-4000-8000-000000000071"
    series = await repository.create_series(
        "https://authentik.example",
        "telemetry-lifecycle-owner",
        content="private lifecycle content",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=rule,
        idempotency_key=key,
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await repository.create_series(
            "https://authentik.example",
            "telemetry-lifecycle-owner",
            content="different private lifecycle content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=rule,
            idempotency_key=key,
        )
    with pytest.raises(ReminderSeriesVersionConflict):
        await repository.pause_series(
            "https://authentik.example",
            "telemetry-lifecycle-owner",
            series.id,
            expected_version=999,
            idempotency_key="00000000-0000-4000-8000-000000000072",
        )
    with pytest.raises(ReminderSeriesTransitionError):
        await repository.resume_series(
            "https://authentik.example",
            "telemetry-lifecycle-owner",
            series.id,
            expected_version=series.version,
            idempotency_key="00000000-0000-4000-8000-000000000073",
        )
    with pytest.raises(ValueError):
        await repository.create_series(
            "https://authentik.example",
            "telemetry-lifecycle-owner",
            content="private lifecycle content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Not/A-Timezone",
            rule=rule,
            idempotency_key="00000000-0000-4000-8000-000000000074",
        )

    paused = await repository.pause_series(
        "https://authentik.example",
        "telemetry-lifecycle-owner",
        series.id,
        expected_version=series.version,
        idempotency_key="00000000-0000-4000-8000-000000000075",
    )
    resumed = await repository.resume_series(
        "https://authentik.example",
        "telemetry-lifecycle-owner",
        series.id,
        expected_version=paused.version,
        idempotency_key="00000000-0000-4000-8000-000000000076",
    )
    skipped = await repository.skip_next(
        "https://authentik.example",
        "telemetry-lifecycle-owner",
        series.id,
        expected_version=resumed.version,
        idempotency_key="00000000-0000-4000-8000-000000000077",
    )
    await repository.update_series(
        "https://authentik.example",
        "telemetry-lifecycle-owner",
        series.id,
        expected_version=skipped.version,
        content="updated private lifecycle content",
        idempotency_key="00000000-0000-4000-8000-000000000078",
    )

    clock[0] = datetime(2026, 1, 10, 12, tzinfo=UTC)
    notifications = MemoryNotificationRepository(
        repository,
        clock=lambda: clock[0],
        metrics=metrics,
    )
    delivery = await notifications.deliver_due(limit=10)
    assert delivery.fired_count == 1

    terminal_metrics = MetadataMetrics()
    terminal_repository = MemoryReminderRepository(
        clock=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC),
        metrics=terminal_metrics,
    )
    terminal = await terminal_repository.create_series(
        "https://authentik.example",
        "telemetry-terminal-owner",
        content="private terminal content",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=RecurrenceRule(RecurrenceFrequency.DAILY, count=1),
        idempotency_key="00000000-0000-4000-8000-000000000079",
    )
    terminal_delivery = await terminal_repository.fire_due_result(
        now=datetime(2026, 1, 5, 10, tzinfo=UTC), limit=10
    )
    assert terminal_delivery.reminders
    terminal_stored = await terminal_repository.get_series(
        "https://authentik.example", "telemetry-terminal-owner", terminal.id
    )
    assert terminal_stored.status is ReminderSeriesStatus.COMPLETED
    assert terminal_stored.next_reminder_id is None
    assert terminal_delivery.generated_count == 0
    assert terminal_repository.metrics is terminal_metrics

    outcomes = [
        item
        for item in metrics.snapshot()
        if item.metric
        in {
            "reminder_series_command_outcome",
            "reminder_series_command_duration_ms",
            "reminder_recurrence_calculation_outcome",
            "reminder_series_version_conflict_count",
            "reminder_series_idempotency_conflict_count",
            "reminder_series_due_lag_ms",
        }
    ]
    assert outcomes
    assert all(dict(item.dimensions).get("outcome") for item in outcomes)
    command_outcomes = [
        item for item in outcomes if item.metric != "reminder_series_due_lag_ms"
    ]
    assert all(dict(item.dimensions).get("operation") for item in command_outcomes)
    operations = {dict(item.dimensions).get("operation") for item in command_outcomes}
    assert {"create", "pause", "resume", "skip", "update"} <= operations
    successful_duration_operations = [
        dict(item.dimensions).get("operation")
        for item in metrics.snapshot()
        if item.metric == "reminder_series_command_duration_ms"
        and dict(item.dimensions).get("outcome") == "ok"
    ]
    assert {
        operation: successful_duration_operations.count(operation)
        for operation in ("create", "pause", "resume", "skip", "update")
    } == {operation: 1 for operation in ("create", "pause", "resume", "skip", "update")}
    advance_spans = [
        item
        for item in metrics.snapshot()
        if item.metric == "reminder_fire_duration_ms"
        and dict(item.trace_attributes).get("operation") == "reminder.series.advance"
    ]
    assert advance_spans
    assert all(item.value > 0 for item in advance_spans)
    assert all(dict(item.dimensions).get("outcome") == "ok" for item in advance_spans)
    assert metrics.stats().rejected == 0
    rendered = repr(
        [
            (dict(item.dimensions), dict(item.trace_attributes))
            for item in outcomes
        ]
    ).lower()
    assert not any(
        private in rendered
        for private in (
            "private lifecycle content",
            "private terminal content",
            "timezone",
            "local_start",
            "due_at",
            "owner",
        )
    )

    def fail_persistence(*_: object, **__: object) -> None:
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(repository, "_record_series_event", fail_persistence)
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await repository.create_series(
            "https://authentik.example",
            "telemetry-failure-owner",
            content="private failure content",
            local_start=datetime(2026, 1, 15, 9, 30),
            timezone="Europe/Brussels",
            rule=rule,
            idempotency_key="00000000-0000-4000-8000-000000000080",
        )
    failure_metrics = [
        item
        for item in metrics.snapshot()
        if item.metric == "reminder_series_command_outcome"
        and dict(item.dimensions).get("outcome") == "error"
    ]
    assert failure_metrics


@pytest.mark.asyncio
async def test_recurrence_failure_telemetry_is_emitted_after_failed_advancement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = RecurrenceRule(RecurrenceFrequency.DAILY)

    invalid_metrics = MetadataMetrics()
    invalid_repository = MemoryReminderRepository(
        clock=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC), metrics=invalid_metrics
    )
    invalid_series = await invalid_repository.create_series(
        "https://authentik.example",
        "telemetry-invalid-owner",
        content="private invalid content",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=rule,
        idempotency_key="00000000-0000-4000-8000-000000000081",
    )
    with pytest.raises(ReminderSeriesTransitionError):
        await invalid_repository.resume_series(
            "https://authentik.example",
            "telemetry-invalid-owner",
            invalid_series.id,
            expected_version=invalid_series.version,
            idempotency_key="00000000-0000-4000-8000-000000000082",
        )
    with pytest.raises(ValueError):
        await invalid_repository.update_series(
            "https://authentik.example",
            "telemetry-invalid-owner",
            invalid_series.id,
            expected_version=invalid_series.version,
            timezone="Not/A-Timezone",
            idempotency_key="00000000-0000-4000-8000-000000000083",
        )
    invalid_calculations = [
        item
        for item in invalid_metrics.snapshot()
        if item.metric == "reminder_recurrence_calculation_outcome"
        and dict(item.dimensions).get("outcome") == "error"
    ]
    assert {dict(item.dimensions).get("operation") for item in invalid_calculations} >= {
        "resume",
        "update",
    }

    memory_metrics = MetadataMetrics()
    memory_repository = MemoryReminderRepository(
        clock=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC), metrics=memory_metrics
    )
    await memory_repository.create_series(
        "https://authentik.example",
        "telemetry-memory-failure-owner",
        content="private memory failure content",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=rule,
        idempotency_key="00000000-0000-4000-8000-000000000084",
    )

    async def fail_materialization(*_: object, **__: object) -> object:
        raise RuntimeError("simulated advancement materialization failure")

    monkeypatch.setattr(memory_repository, "_materialize_memory_occurrence", fail_materialization)
    with pytest.raises(RuntimeError, match="advancement materialization failure"):
        await memory_repository.fire_due_result(
            now=datetime(2026, 1, 5, 10, tzinfo=UTC), limit=10
        )
    memory_calculations = [
        item
        for item in memory_metrics.snapshot()
        if item.metric == "reminder_recurrence_calculation_outcome"
        and dict(item.dimensions).get("operation") == "fire"
    ]
    assert memory_calculations
    assert any(dict(item.dimensions).get("outcome") == "error" for item in memory_calculations)
    memory_advances = [
        item
        for item in memory_metrics.snapshot()
        if item.metric == "reminder_fire_duration_ms"
        and dict(item.trace_attributes).get("operation") == "reminder.series.advance"
    ]
    assert memory_advances
    assert all(dict(item.dimensions).get("outcome") == "error" for item in memory_advances)

    persistence_metrics = MetadataMetrics()
    persistence_repository = MemoryReminderRepository(
        clock=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC), metrics=persistence_metrics
    )
    await persistence_repository.create_series(
        "https://authentik.example",
        "telemetry-persistence-failure-owner",
        content="private persistence failure content",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=rule,
        idempotency_key="00000000-0000-4000-8000-000000000085",
    )

    def fail_event(*_: object, **__: object) -> None:
        raise RuntimeError("simulated advancement persistence failure")

    monkeypatch.setattr(persistence_repository, "_record_series_event", fail_event)
    with pytest.raises(RuntimeError, match="advancement persistence failure"):
        await persistence_repository.fire_due_result(
            now=datetime(2026, 1, 5, 10, tzinfo=UTC), limit=10
        )
    persistence_calculations = [
        item
        for item in persistence_metrics.snapshot()
        if item.metric == "reminder_recurrence_calculation_outcome"
        and dict(item.dimensions).get("operation") == "fire"
    ]
    assert persistence_calculations
    assert any(
        dict(item.dimensions).get("outcome") == "error" for item in persistence_calculations
    )
    persistence_advances = [
        item
        for item in persistence_metrics.snapshot()
        if item.metric == "reminder_fire_duration_ms"
        and dict(item.trace_attributes).get("operation") == "reminder.series.advance"
    ]
    assert persistence_advances
    assert all(
        dict(item.dimensions).get("outcome") == "error" for item in persistence_advances
    )


@pytest.mark.asyncio
async def test_sql_advancement_outbox_failure_records_one_error_span_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("AURA_0064_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0064_DATABASE_URL is required for SQL advancement telemetry coverage")
    try:
        from alembic import command
        from aura_core.domains.automation.notifications.repository import (
            SqlNotificationRepository,
        )
        from aura_core.domains.automation.reminders.repository import SqlReminderRepository
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    metrics = MetadataMetrics()
    reminders = SqlReminderRepository(sessions, clock=lambda: clock[0], metrics=metrics)
    notifications = SqlNotificationRepository(sessions, clock=lambda: clock[0], metrics=metrics)
    notifications.set_reminder_delivery(reminders)
    subject = f"series-telemetry-live-{uuid4().hex}"
    series_id: UUID | None = None
    try:
        series = await reminders.create_series(
            "https://authentik.example",
            subject,
            content="private SQL advancement failure content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=RecurrenceRule(RecurrenceFrequency.DAILY),
            idempotency_key=str(uuid4()),
        )
        series_id = series.id

        async def fail_stage(*_: object, **__: object) -> None:
            raise RuntimeError("simulated SQL advancement outbox failure")

        monkeypatch.setattr(reminders, "_stage_series", fail_stage)
        clock[0] = datetime(2026, 1, 5, 10, tzinfo=UTC)
        with pytest.raises(RuntimeError, match="SQL advancement outbox failure"):
            await notifications.deliver_due(limit=10)

        advance_spans = [
            item
            for item in metrics.snapshot()
            if item.metric == "reminder_fire_duration_ms"
            and dict(item.trace_attributes).get("operation") == "reminder.series.advance"
        ]
        assert len(advance_spans) == 1
        assert dict(advance_spans[0].dimensions).get("outcome") == "error"
        recurrence_errors = [
            item
            for item in metrics.snapshot()
            if item.metric == "reminder_recurrence_calculation_outcome"
            and dict(item.dimensions).get("operation") == "fire"
            and dict(item.dimensions).get("outcome") == "error"
        ]
        assert len(recurrence_errors) == 1
        assert "private SQL advancement failure content" not in repr(metrics.snapshot())

        stored = await reminders.get_series(
            "https://authentik.example", subject, series_id
        )
        assert stored.status.value == "active"
        history = await reminders.list_occurrences(
            "https://authentik.example", subject, series_id
        )
        assert sum(item.status.value == "scheduled" for item in history) == 1
        assert sum(item.status.value == "fired" for item in history) == 0
    finally:
        async with sessions() as session, session.begin():
            for statement in (
                "DELETE FROM notification_events WHERE principal_subject = :subject",
                "DELETE FROM notifications WHERE principal_subject = :subject",
                "DELETE FROM outbox WHERE aggregate_id = :series_id",
                "DELETE FROM reminder_occurrences WHERE series_id = :series_id",
                "DELETE FROM reminder_series_command_idempotency "
                "WHERE principal_subject = :subject",
                "DELETE FROM reminder_series WHERE principal_subject = :subject",
                "DELETE FROM reminders WHERE principal_subject = :subject",
            ):
                await session.execute(
                    text(statement),
                    {"subject": subject, "series_id": series_id},
                )
        await engine.dispose()


def test_scheduler_and_notification_spans_use_duration_metrics_and_valid_trace_links() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    parent = new_span_id()
    metrics.record_span(
        "aura.automation.reminder_scheduler",
        "reminder.scan",
        2.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent,
        dependency="postgresql",
        outcome="ok",
        result_count="1",
    )
    metrics.record_span(
        "aura.automation.reminder_scheduler",
        "reminder.fire",
        3.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent,
        dependency="postgresql",
        outcome="ok",
    )
    metrics.record_span(
        "aura.automation.notification_delivery",
        "notification.sse.replay",
        4.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent,
        dependency="postgresql",
        outcome="ok",
    )
    metrics.record_span(
        "aura.automation.notification_delivery",
        "notification.acknowledge",
        5.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent,
        dependency="postgresql",
        outcome="ok",
    )
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert {item.metric for item in spans} == {
        "reminder_scheduler_scan_duration_ms",
        "reminder_fire_duration_ms",
        "notification_sse_replay_duration_ms",
        "notification_acknowledgement_duration_ms",
    }
    assert all(item.trace_id == trace_id for item in spans)
    assert all(item.parent_span_id == parent for item in spans)
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_scheduler_telemetry_lifecycle_flushes_on_shutdown() -> None:
    exported: list[TelemetryBatch] = []

    class Exporter:
        async def export(self, batch: TelemetryBatch) -> None:
            exported.append(batch)

    metrics = MetadataMetrics(exporter=Exporter())
    metrics.observe(
        "aura.automation.reminder_scheduler",
        "reminder_scheduler_scan_duration_ms",
        1.0,
        outcome="ok",
    )
    lifecycle = TelemetryLifecycle(metrics, interval_seconds=60.0)
    await lifecycle.start()
    assert lifecycle.running
    assert await lifecycle.stop()
    assert not lifecycle.running
    assert exported and exported[-1].measurements
    assert metrics.stats().rejected == 0


def test_model_inventory_telemetry_is_registered_and_metadata_only() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    metrics.record_span(
        "aura.runtime.structured_inference",
        "model.inventory",
        4.0,
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        dependency="model_provider",
        outcome="ok",
        provider="ollama",
    )
    metrics.increment(
        "aura.runtime.structured_inference",
        "model_inventory_cache_outcome",
        outcome="hit",
        provider="ollama",
    )
    measurements = metrics.snapshot()
    assert {item.metric for item in measurements} == {
        "model_inventory_duration_ms",
        "model_inventory_cache_outcome",
    }
    assert all(item.trace_id in {None, trace_id} for item in measurements)
    assert "model_provider" in repr(measurements)


@pytest.mark.asyncio
async def test_ollama_inventory_cache_hit_and_miss_stay_parented_to_preview_root() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "chat",
                        "capabilities": ["chat"],
                        "details": {},
                    }
                ]
            },
        )

    metrics = MetadataMetrics()
    adapter = OllamaAdapter("https://ollama.test", telemetry=metrics)
    context = ProviderTraceContext(trace_id="a" * 32, span_id="b" * 16)
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(  # type: ignore[method-assign]
        *args, transport=transport, **kwargs
    )
    try:
        await adapter.list_models(context=context)
        await adapter.list_models(context=context)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]

    measurements = metrics.snapshot()
    inventory_spans = [
        item
        for item in measurements
        if item.kind == "span" and item.metric == "model_inventory_duration_ms"
    ]
    assert len(inventory_spans) == 2
    assert all(item.trace_id == context.trace_id for item in inventory_spans)
    assert all(item.parent_span_id == context.span_id for item in inventory_spans)
    cache_outcomes = [
        dict(item.dimensions)["outcome"]
        for item in measurements
        if item.metric == "model_inventory_cache_outcome"
    ]
    assert cache_outcomes == ["miss", "hit"]


def test_tool_runtime_metrics_are_registered_and_reject_content_dimensions() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    metrics.record_span(
        "aura.runtime.tool_selection",
        "tool.select",
        1.0,
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        dependency="model_provider",
        outcome="selected",
    )
    metrics.increment(
        "aura.runtime.policy_evaluation",
        "tool_policy_decision",
        outcome="allowed",
    )
    metrics.increment(
        "aura.runtime.tool_execution",
        "tool_execution_outcome",
        outcome="completed",
    )
    metrics.record_span(
        "aura.runtime.tool_selection",
        "tool.select",
        1.0,
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        dependency="model_provider",
        outcome="selected",
        query="private memory text",
    )
    metrics.increment(
        "aura.runtime.policy_evaluation",
        "tool_policy_decision",
        outcome="allowed",
        arguments='{"query":"private memory text"}',
    )
    metrics.increment(
        "aura.runtime.tool_execution",
        "tool_execution_outcome",
        outcome="completed",
        result="private memory content",
    )
    measurements = metrics.snapshot()
    assert {item.metric for item in measurements} >= {
        "tool_selection_duration_ms",
        "tool_policy_decision",
        "tool_execution_outcome",
    }
    assert all("private" not in repr(item.dimensions) for item in measurements)
    assert all("query" not in dict(item.trace_attributes) for item in measurements)
    assert all("arguments" not in dict(item.trace_attributes) for item in measurements)
    assert all("result" not in dict(item.trace_attributes) for item in measurements)
    assert metrics.stats().rejected >= 3
    assert COMPONENT_VERSIONS["aura.runtime.tool_selection"] == "1.4.0"
    assert COMPONENT_VERSIONS["aura.runtime.policy_evaluation"] == "1.1.0"
    assert COMPONENT_VERSIONS["aura.runtime.tool_execution"] == "1.3.0"


def test_memory_product_observations_are_registered_and_metadata_only() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    required = (
        (
            "aura.knowledge.memory_persistence",
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
        ),
        (
            "aura.knowledge.memory_extraction",
            "memory_candidate_review_duration_ms",
            "memory_candidate_review_outcome",
        ),
        (
            "aura.knowledge.memory_persistence",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
        ),
        (
            "aura.interaction.agent_configuration",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
        ),
        (
            "aura.runtime.stream_delivery",
            "memory_activity_publication_duration_ms",
            "memory_activity_publication_outcome",
        ),
        (
            "aura.runtime.stream_delivery",
            "memory_activity_delivery_duration_ms",
            "memory_activity_delivery_outcome",
        ),
    )
    for component, duration, outcome in required:
        metrics.observe(
            component,
            duration,
            1.0,
            dependency="memory_store",
            outcome="ok",
            trace_id=trace_id,
        )
        metrics.increment(
            component,
            outcome,
            dependency="memory_store",
            outcome="ok",
            trace_id=trace_id,
        )
    assert metrics.stats().rejected == 0
    assert {item.metric for item in metrics.snapshot()} == {
        metric
        for _, duration, outcome in required
        for metric in (duration, outcome)
    }
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    retained = len(metrics.snapshot())
    metrics.increment(
        "aura.knowledge.memory_persistence",
        "memory_product_operation_outcome",
        content="must be rejected before export",
    )
    assert len(metrics.snapshot()) == retained
    assert metrics.stats().rejected == 1


def test_stream_activity_operations_use_distinct_metrics_without_rejection() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.publish",
        3.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="event_store",
        outcome="ok",
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.cursor_expired",
        1.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="event_store",
        outcome="expired",
        error_class="cursor_expired",
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.reconcile",
        2.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="memory_store",
        outcome="not_found",
        error_class="run_not_found",
        run_id=str(uuid4()),
    )
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [item.metric for item in spans] == [
        "memory_activity_publication_duration_ms",
        "stream_delivery_duration_ms",
        "stream_delivery_duration_ms",
    ]
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_export_failure_retries_are_bounded_and_counters_survive() -> None:
    class Exporter:
        def __init__(self) -> None:
            self.fail = True
            self.exported: list[TelemetryBatch] = []

        async def export(self, batch: TelemetryBatch) -> None:
            if self.fail:
                raise RuntimeError("collector unavailable")
            self.exported.append(batch)

    exporter = Exporter()
    metrics = MetadataMetrics(exporter=exporter, max_export_attempts=2)
    metrics.increment("aura.execution.run_coordinator", "run_started")

    assert not await metrics.flush()
    assert len(metrics.snapshot()) == 1
    assert not await metrics.flush()
    assert metrics.snapshot() == ()
    assert metrics.stats().export_failures == 2
    assert metrics.stats().dropped == 1
    assert metrics.stats().abandoned_batches == 1

    exporter.fail = False
    assert await metrics.flush()
    assert exporter.exported[-1].stats.export_failures == 2
    assert exporter.exported[-1].stats.dropped == 1


@pytest.mark.asyncio
async def test_structured_container_export_is_metadata_only_and_exports_health() -> None:
    lines: list[str] = []
    delegate = StructuredContainerLogExporter(lines.append)

    class FailOnce:
        def __init__(self) -> None:
            self.failed = False

        async def export(self, batch: TelemetryBatch) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("stdout temporarily unavailable")
            await delegate.export(batch)

    run_id = uuid4()
    metrics = MetadataMetrics(capacity=1, exporter=FailOnce())
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        trace_id=run_id.hex,
        run_id=str(run_id),
        provider="ollama",
        model_id="chat:latest",
    )
    metrics.increment("aura.runtime.stream_delivery", "sse_connections")
    metrics.increment(
        "aura.runtime.stream_delivery",
        "errors",
        response="private assistant output",
    )

    assert not await metrics.flush()
    assert await metrics.flush()
    records = [cast(dict[str, Any], json.loads(line)) for line in lines]
    measurement = next(
        record for record in records if record["event"] == "aura.telemetry.measurement"
    )
    health = next(record for record in records if record["event"] == "aura.telemetry.health")
    assert measurement["capture_policy"] == "metadata_only"
    assert set(cast(dict[str, object], measurement["dimensions"])) == set()
    assert health["dropped"] == 1
    assert health["rejected"] == 1
    assert health["export_failures"] == 1
    rendered = "\n".join(lines).casefold()
    assert all(
        forbidden not in rendered
        for forbidden in ("private assistant", '"prompt"', '"response"', '"endpoint"', '"token"')
    )


@pytest.mark.asyncio
async def test_periodic_lifecycle_flushes_and_shutdown_flushes() -> None:
    class Exporter:
        def __init__(self) -> None:
            self.batches: list[TelemetryBatch] = []

        async def export(self, batch: TelemetryBatch) -> None:
            self.batches.append(batch)

    exporter = Exporter()
    metrics = MetadataMetrics(exporter=exporter)
    lifecycle = TelemetryLifecycle(metrics, interval_seconds=0.01)
    metrics.increment("aura.execution.run_coordinator", "run_started")

    await lifecycle.start()
    await asyncio.sleep(0.03)
    metrics.increment("aura.execution.run_coordinator", "run_started")
    assert await lifecycle.stop()

    assert not lifecycle.running
    assert any(batch.measurements for batch in exporter.batches)
    assert metrics.snapshot() == ()


def test_normal_api_composition_configures_real_exporter() -> None:
    state = AppState(
        Settings(
            database_url="postgresql+psycopg://aura:aura@127.0.0.1:5432/aura",
            oidc_issuer="https://identity.example",
        )
    )
    assert isinstance(state.metrics.exporter, StructuredContainerLogExporter)
    assert state.metrics.stats().exporter_configured


def test_normal_worker_composition_configures_real_exporter() -> None:
    metrics, lifecycle = make_worker_telemetry()

    assert isinstance(metrics.exporter, StructuredContainerLogExporter)
    assert metrics.stats().exporter_configured
    assert lifecycle.metrics is metrics


@pytest.mark.asyncio
async def test_production_worker_execution_emits_a_unique_real_root_span() -> None:
    provider = FakeChatModel(("ok",))
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "worker", "fake", models, str(uuid4())
    )
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)
    command = OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        run.id,
        run.conversation_id,
        datetime.now(UTC),
        run.id,
        run.id,
    )

    class Consumer:
        acknowledged = False

        async def receive(self) -> OutboxCommand:
            return command

        async def ack(self) -> None:
            self.acknowledged = True

    consumer = Consumer()
    await run_once(coordinator, provider, cast(NatsRunConsumer, cast(object, consumer)))

    roots = [
        item
        for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "run.execute"
    ]
    assert consumer.acknowledged
    assert len(roots) == 1
    assert roots[0].trace_id == run.id.hex
    assert roots[0].parent_span_id is None
    assert roots[0].span_id != root_span_id(run.id.hex)


@pytest.mark.asyncio
async def test_coordinator_records_actual_execution_boundaries() -> None:
    provider = FakeChatModel(("four", " chars"))
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "question", "fake", models, str(uuid4())
    )
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, provider)

    measurements = metrics.snapshot()
    names = {item.metric for item in measurements}
    assert {
        "checkpoint_persisted",
        "model_routed",
        "output_tokens",
        "queue_wait_ms",
        "run_duration_ms",
        "time_to_first_token_ms",
    } <= names
    output = next(item for item in measurements if item.metric == "output_tokens")
    assert output.value == 3
    assert dict(output.dimensions)["token_estimator"] == "chars_div_4_ceil"
    duration = next(item for item in measurements if item.metric == "run_duration_ms")
    assert dict(duration.dimensions)["status"] == "completed"
    assert all(item.trace_id == run.id.hex for item in measurements)
    spans = {
        dict(item.trace_attributes)["operation"]: item
        for item in measurements
        if item.kind == "span"
    }
    worker = spans["run.execute"]
    routed = spans["model.route"]
    inferred = spans["model.infer"]
    assert worker.metric == "operation_duration_ms"
    assert routed.metric == "model_routing_duration_ms"
    assert inferred.metric == "inference_duration_ms"
    assert routed.value > 0
    assert routed.parent_span_id == inferred.parent_span_id == worker.span_id
    assert worker.parent_span_id is None
    assert len({worker.span_id, routed.span_id, inferred.span_id}) == 3


@pytest.mark.asyncio
async def test_claim_loss_during_stream_is_not_classified_as_provider_error() -> None:
    catalog = FakeChatModel()

    class ClaimLostOnAppendStore(ConversationStore):
        async def append_assistant(
            self,
            run_id: UUID,
            text: str,
            state: MessageState = MessageState.PARTIAL,
            *,
            attempt_id: UUID | None = None,
        ) -> ConversationMessage:
            del run_id, text, state, attempt_id
            raise RunClaimLost

    store = ClaimLostOnAppendStore()
    models = await catalog.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "lease loss", "fake", models, str(uuid4())
    )

    class LeaseLostProvider:
        def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del model_id, messages

            async def chunks() -> AsyncIterator[str]:
                yield "stale"

            return chunks()

    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, LeaseLostProvider())

    inference = next(
        item
        for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "model.infer"
    )
    assert dict(inference.dimensions)["outcome"] == "skipped"
    assert "error_class" not in dict(inference.dimensions)
    assert not any(item.metric == "provider_errors" for item in metrics.snapshot())


@pytest.mark.asyncio
async def test_lease_expiry_metric_preserves_the_actual_run_trace() -> None:
    provider = FakeChatModel()
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "expire", "fake", models, str(uuid4())
    )
    await store.start_run(run.id, worker_id=uuid4(), lease_seconds=0.001)
    await asyncio.sleep(0.01)
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, provider)

    expiry = next(item for item in metrics.snapshot() if item.metric == "lease_expired")
    _, interrupted = await store.find_run_any(run.id)
    assert interrupted.error is not None
    assert interrupted.error.trace_id == run.id.hex == expiry.trace_id
    assert expiry.trace_id == run.id.hex
    assert dict(expiry.trace_attributes)["run_id"] == str(run.id)
    assert dict(expiry.trace_attributes)["conversation_id"] == str(run.conversation_id)


def test_parented_span_has_component_identity_dependency_and_trace_only_ids() -> None:
    run_id, conversation_id = uuid4(), uuid4()
    metrics = MetadataMetrics()
    child_id = new_span_id()
    metrics.record_span(
        "aura.interaction.conversation_persistence",
        "checkpoint.persist",
        3.5,
        trace_id=run_id.hex,
        span_id=child_id,
        parent_span_id=root_span_id(run_id.hex),
        dependency="postgresql",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )

    span = metrics.snapshot()[0]
    assert span.kind == "span"
    assert span.component_version == COMPONENT_VERSIONS[
        "aura.interaction.conversation_persistence"
    ] == "1.7.0"
    assert span.span_id == child_id
    assert span.parent_span_id == root_span_id(run_id.hex)
    assert dict(span.dimensions) == {"dependency": "postgresql", "outcome": "ok"}
    assert dict(span.trace_attributes)["run_id"] == str(run_id)
    assert not _contains_uuid(dict(span.dimensions))


def test_title_inference_component_version_remains_explicit() -> None:
    assert COMPONENT_VERSIONS["aura.runtime.model_inference"] == "1.5.0"


def test_component_manifest_versions_match_runtime_catalogue() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    manifest_versions: dict[str, str] = {}
    for path in sorted(manifest_dir.glob("*.yaml")):
        document = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        component = cast(dict[str, Any], document["component"])
        manifest_versions[str(component["id"])] = str(component["version"])
    assert manifest_versions == dict(COMPONENT_VERSIONS)


def test_tool_execution_manifest_matches_emitted_reminder_telemetry() -> None:
    manifest_path = (
        Path(__file__).parents[1] / "resources" / "component-manifests" / "tool-execution.yaml"
    )
    document = cast(dict[str, Any], yaml.safe_load(manifest_path.read_text()))
    cardinality = cast(dict[str, Any], document["cardinality"])
    assert "reminder_store" in document["dependencies"]
    assert set(cardinality["metric_attributes"]) >= {
        "outcome",
        "error_class",
        "dependency",
        "provider",
    }
    assert set(cardinality["trace_only"]) >= {
        "approval_id",
        "tool_call_id",
        "tool_policy_revision_id",
    }
    assert "content" not in cardinality["trace_only"]


def test_persona_preview_component_is_ephemeral_and_cardinality_bounded() -> None:
    manifest_path = (
        Path(__file__).parents[1] / "resources" / "component-manifests" / "persona-preview.yaml"
    )
    document = cast(dict[str, Any], yaml.safe_load(manifest_path.read_text()))
    component = cast(dict[str, Any], document["component"])
    cardinality = cast(dict[str, Any], document["cardinality"])
    assert component == {
        "id": "aura.interaction.persona_preview",
        "version": "1.0.0",
        "owner": "interaction.personas",
        "category": "preview",
    }
    assert document["input"]["replayable"] is False
    assert set(cardinality["metric_attributes"]) == {
        "outcome",
        "error_class",
        "dependency",
        "provider",
    }
    assert set(cardinality["prohibited"]) >= {
        "prompt",
        "response",
        "draft",
        "output",
        "owner",
        "endpoint",
        "token",
        "credential",
    }
    assert document["retention"]["process_buffer"] == "bounded"
    assert document["retention"]["overflow"] == "drop_oldest_and_count"
    assert document["retention"]["export_failure"] == "retry_three_times_then_drop_and_count"


def test_persona_preview_rejects_forbidden_content_attributes_before_retention() -> None:
    metrics = MetadataMetrics(capacity=2)
    trace_id = uuid4().hex
    metrics.increment(
        "aura.interaction.persona_preview",
        "persona_preview_outcome",
        trace_id=trace_id,
        outcome="ok",
        prompt="PRIVATE_PROMPT",
    )
    metrics.record_span(
        "aura.interaction.persona_preview",
        "persona.preview",
        1.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="model_provider",
        outcome="ok",
        output="PRIVATE_OUTPUT",
    )
    assert metrics.snapshot() == ()
    assert metrics.stats().rejected == 2


@pytest.mark.asyncio
async def test_persona_preview_retention_health_counts_drop_and_export_abandonment() -> None:
    class FailingExporter:
        async def export(self, batch: TelemetryBatch) -> None:
            del batch
            raise RuntimeError("collector unavailable")

    metrics = MetadataMetrics(capacity=1, exporter=FailingExporter(), max_export_attempts=2)
    metrics.increment(
        "aura.interaction.persona_preview",
        "persona_preview_outcome",
        outcome="ok",
    )
    metrics.increment(
        "aura.interaction.persona_preview",
        "persona_preview_outcome",
        outcome="error",
        error_class="provider",
    )
    assert metrics.stats().dropped == 1
    assert not await metrics.flush()
    assert not await metrics.flush()
    assert metrics.stats().abandoned_batches == 1
    assert metrics.stats().export_failures == 2


@pytest.mark.asyncio
async def test_thinking_transport_telemetry_excludes_text_and_owner_identity() -> None:
    metrics = MetadataMetrics()
    publisher = EventPublisher(metrics=metrics)
    run_id, conversation_id = uuid4(), uuid4()
    await publisher.publish_thinking_delta(
        run_id=run_id,
        conversation_id=conversation_id,
        offset=0,
        text="private reasoning text",
    )
    stream = publisher.stream(run_id)
    async def next_event() -> object:
        return await stream.__anext__()

    pending = asyncio.create_task(next_event())
    await asyncio.sleep(0)
    await publisher.publish_thinking_delta(
        run_id=run_id,
        conversation_id=conversation_id,
        offset=0,
        text="private reasoning text",
    )
    await asyncio.wait_for(pending, timeout=0.2)
    close = getattr(stream, "aclose", None)
    if callable(close):
        await cast(Callable[[], Awaitable[None]], close)()
    rendered = repr(metrics.snapshot())
    assert "private reasoning text" not in rendered
    assert "owner-subject" not in rendered
    assert any(
        item.metric == "thinking_stream_outcome"
        and dict(item.dimensions).get("outcome") == "no_subscriber"
        for item in metrics.snapshot()
    )


@pytest.mark.asyncio
async def test_approval_resumption_telemetry_is_retained_and_content_free() -> None:
    metrics = MetadataMetrics()
    approval_id, run_id, conversation_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    approval = Approval(
        id=approval_id,
        issuer="owner-subject",
        subject="owner-subject",
        proposal_id=uuid4(),
        run_id=run_id,
        conversation_id=conversation_id,
        tool_call_id=uuid4(),
        tool_id="reminder.create",
        tool_version=1,
        status=ApprovalStatus.PENDING,
        execution_status=ApprovalExecutionStatus.NOT_STARTED,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=now,
    )

    await _resume_approval(  # pyright: ignore[reportPrivateUsage]
        approval,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        metrics,
        event_id=uuid4(),
    )

    records = metrics.snapshot()
    assert len(records) == 1
    record = records[0]
    assert record.component_id == "aura.governance.approval_workflow"
    assert record.metric == "approval_resume_duration_ms"
    assert dict(record.dimensions) == {
        "dependency": "postgresql",
        "outcome": "ignored",
    }
    rendered = repr(records)
    assert "owner-subject" not in rendered
    assert "private reminder content" not in rendered


def test_memory_retrieval_telemetry_is_bounded_and_metadata_only() -> None:
    metrics = MetadataMetrics()
    run_id, memory_id = uuid4(), uuid4()
    record_memory_retrieval(
        metrics,
        operation="vector",
        duration_ms=4.5,
        trace_id=run_id.hex,
        outcome="ok",
        dependency="memory_store",
        scope_type="agent",
        candidate_count=50,
        recall_count=3,
        context_tokens=120,
        memory_id=str(memory_id),
        memory_revision_id=str(uuid4()),
        memory_policy_revision_id=str(uuid4()),
        embedding_generation_id=str(uuid4()),
        run_id=str(run_id),
    )

    measurements = metrics.snapshot()
    assert measurements[0].component_id == "aura.knowledge.memory_retrieval"
    assert measurements[0].metric == "memory_vector_search_duration_ms"
    assert dict(measurements[0].dimensions)["retrieval_stage"] == "vector"
    assert {item.metric for item in measurements} >= {
        "memory_vector_search_duration_ms",
        "memory_retrieval_candidate_count",
        "memory_recall_count",
        "memory_context_tokens",
    }
    rendered = json.dumps(
        [
            {
                "metric": item.metric,
                "dimensions": dict(item.dimensions),
                "trace": dict(item.trace_attributes),
            }
            for item in measurements
        ]
    )
    assert all(
        secret not in rendered
        for secret in ('"content":', '"prompt":', '"response":', '"vector":')
    )


def test_memory_retrieval_production_stage_shapes_preserve_parent_and_bound_labels() -> None:
    metrics = MetadataMetrics()
    trace_id, parent_span_id = uuid4().hex, new_span_id()
    record_memory_retrieval(
        metrics,
        operation="memory.retrieval.lexical",
        duration_ms=2.0,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        outcome="ok",
        dependency="postgresql",
        candidate_count=50,
    )
    record_memory_retrieval(
        metrics,
        operation="memory.retrieval.fallback",
        duration_ms=1.0,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        outcome="ok",
        dependency="foreign-store-driver",
        fallback_outcome="empty",
        error_class="RuntimeError",
    )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(spans) == 2
    assert spans[0].parent_span_id == parent_span_id
    assert spans[0].metric == "memory_lexical_search_duration_ms"
    assert dict(spans[1].dimensions)["dependency"] == "memory_store"
    assert dict(spans[1].dimensions)["error_class"] == "unknown"
    assert metrics.stats().rejected == 0


def test_memory_retrieval_query_embedding_primary_and_post_render_budget_shapes() -> None:
    metrics = MetadataMetrics()
    trace_id, parent_span_id = uuid4().hex, new_span_id()
    for operation, stage, fallback in (
        ("memory.retrieval.query_embedding", "query_embedding", None),
        ("memory.retrieval", "primary", "not_needed"),
        ("memory.retrieval.context_budget", "context_budget", "not_granted"),
    ):
        record_memory_retrieval(
            metrics,
            operation=operation,
            duration_ms=1.0,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            outcome="ok",
            dependency="query_embedding" if stage == "query_embedding" else "memory_store",
            retrieval_stage=stage,
            fallback_outcome=fallback,
            context_tokens=96 if stage == "context_budget" else None,
        )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [item.metric for item in spans] == [
        "memory_query_embedding_duration_ms",
        "memory_retrieval_duration_ms",
        "memory_retrieval_duration_ms",
    ]
    assert all(item.parent_span_id == parent_span_id for item in spans)
    fallback = [
        item for item in metrics.snapshot() if item.metric == "memory_fallback_outcome"
    ]
    assert {dict(item.dimensions)["outcome"] for item in fallback} == {
        "not_needed",
        "not_granted",
    }
    assert any(item.metric == "memory_context_tokens" for item in metrics.snapshot())
    assert metrics.stats().rejected == 0


def test_conversation_list_telemetry_is_postgresql_bound_and_metadata_only() -> None:
    from aura_core.bootstrap.conversation_uow import SqlConversationStore

    metrics = MetadataMetrics()
    store = SqlConversationStore(lambda: None)  # type: ignore[arg-type]
    store.set_prompt_metrics(metrics)
    trace_id = uuid4().hex
    record_list = store._record_conversation_list  # pyright: ignore[reportPrivateUsage]

    parent_span_id = new_span_id()
    record_list(trace_id, parent_span_id, 0.0, "ok", None)
    record_list(trace_id, parent_span_id, 0.0, "error", "persistence")

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [dict(item.trace_attributes)["operation"] for item in spans] == [
        "conversation.list",
        "conversation.list",
    ]
    assert dict(spans[0].dimensions) == {"dependency": "postgresql", "outcome": "ok"}
    assert dict(spans[1].dimensions) == {
        "dependency": "postgresql",
        "error_class": "persistence",
        "outcome": "error",
    }
    assert all("subject" not in dict(item.trace_attributes) for item in spans)
    assert all(item.parent_span_id == parent_span_id for item in spans)


@pytest.mark.asyncio
async def test_sql_conversation_list_records_parented_success_and_failure_spans() -> None:
    from aura_core.bootstrap.conversation_uow import SqlConversationStore

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    metrics = MetadataMetrics()
    store = SqlConversationStore(lambda: SessionContext())  # type: ignore[arg-type]
    store.set_prompt_metrics(metrics)
    trace_id, parent_span_id = uuid4().hex, new_span_id()

    async def no_principal(session: object, issuer: str, subject: str) -> None:
        del session, issuer, subject
        return None

    store.identities.find = no_principal  # type: ignore[method-assign]
    assert await store.list(
        "owner",
        issuer="https://issuer",
        trace_id=trace_id,
        parent_span_id=parent_span_id,
    ) == ([], None)

    async def persistence_failure(session: object, issuer: str, subject: str) -> None:
        del session, issuer, subject
        raise RuntimeError("database details must not escape telemetry")

    store.identities.find = persistence_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await store.list(
            "owner",
            issuer="https://issuer",
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(spans) == 2
    assert [dict(item.dimensions)["outcome"] for item in spans] == ["ok", "error"]
    assert dict(spans[1].dimensions)["error_class"] == "persistence"
    assert all(item.trace_id == trace_id for item in spans)
    assert all(item.parent_span_id == parent_span_id for item in spans)


def test_prompt_compilation_telemetry_keeps_provenance_metadata_without_prompt_content() -> None:
    lines: list[str] = []
    exporter = StructuredContainerLogExporter(lines.append)
    metrics = MetadataMetrics(exporter=exporter)
    run_id, conversation_id = uuid4(), uuid4()
    metrics.record_span(
        "aura.runtime.prompt_compilation",
        "prompt.compile",
        2.5,
        trace_id=run_id.hex,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="model_policy",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
        agent_revision_id=str(uuid4()),
        persona_revision_id=str(uuid4()),
        prompt_bundle_revision_id=str(uuid4()),
        prompt_hash="a" * 64,
        prompt_component_count="4",
        compiled_prompt_size="123",
    )

    measurement = metrics.snapshot()[0]
    assert measurement.component_id == "aura.runtime.prompt_compilation"
    assert measurement.metric == "prompt_compile_duration_ms"
    assert dict(measurement.trace_attributes)["prompt_hash"] == "a" * 64
    assert dict(measurement.trace_attributes)["prompt_component_count"] == "4"
    assert dict(measurement.trace_attributes)["compiled_prompt_size"] == "123"
    assert asyncio.run(metrics.flush())
    rendered = "\n".join(lines)
    assert "instructions" not in rendered
    assert "rendered prompt" not in rendered


@pytest.mark.asyncio
async def test_nats_readiness_recovers_after_stream_subject_is_restored() -> None:
    class Config:
        subjects = ["aura.runs.execute.v1"]

    class Info:
        config = Config()

    class JetStream:
        async def account_info(self) -> object:
            return object()

        async def streams_info(self) -> list[Info]:
            return [Info()]

        async def stream_info(self, name: str) -> Info:
            assert name == "AURA_RUNS"
            return Info()

    class Connection:
        is_closed = False

    transport = NatsOutbox("nats://unused")
    cast(Any, transport)._connection = Connection()
    cast(Any, transport)._jetstream = JetStream()

    assert await transport.readiness() == (True, None)
    Config.subjects = ["wrong.subject"]
    assert await transport.readiness() == (False, "run stream subject missing")
    Config.subjects = ["aura.runs.execute.v1"]
    assert await transport.readiness() == (True, None)


@pytest.mark.asyncio
async def test_consumer_duplicate_and_ack_are_observed_without_uuid_dimensions() -> None:
    command = OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        uuid4(),
        uuid4(),
        datetime.now(UTC),
        uuid4(),
        uuid4(),
    )

    class Message:
        def __init__(self) -> None:
            self.data = command.wire_payload()
            self.metadata = SimpleNamespace(num_delivered=2)
            self.acknowledged = False

        async def ack(self) -> None:
            self.acknowledged = True

    message = Message()

    class Subscription:
        async def fetch(self, count: int, timeout: int) -> Sequence[Message]:
            assert count == 1 and timeout == 30
            return (message,)

    metrics = MetadataMetrics()
    consumer = NatsRunConsumer("nats://unused", metrics=metrics)
    cast(Any, consumer)._subscription = Subscription()

    received = await consumer.receive()
    await consumer.ack()

    assert received == command
    assert message.acknowledged
    names = [item.metric for item in metrics.snapshot()]
    assert names == ["consumer_received", "outbox_duplicates", "consumer_acknowledged"]
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    assert all(
        dict(item.trace_attributes)["command_id"] == str(command.id)
        for item in metrics.snapshot()
    )


def _contains_uuid(values: dict[str, object]) -> bool:
    for value in values.values():
        try:
            from uuid import UUID

            UUID(str(value))
        except ValueError:
            continue
        return True
    return False


def test_aura0056_telemetry_vocabulary_accepts_approval_sse_and_reminder_tool() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    metrics.increment(
        "aura.governance.approval_workflow",
        "approval_sse_connection_count",
        outcome="opened",
    )
    metrics.observe(
        "aura.governance.approval_workflow",
        "approval_sse_replay_count",
        1.0,
        outcome="ok",
    )
    metrics.record_span(
        "aura.governance.approval_workflow",
        "approval.sse",
        2.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="approval-replay",
        outcome="closed",
        error_class="stream_error",
    )
    metrics.increment(
        "aura.runtime.tool_execution",
        "reminder_create_tool_outcome",
        outcome="completed",
        idempotency="approval-derived",
    )

    assert metrics.stats().rejected == 0


def test_optional_client_timezone_omission_is_a_registered_outcome() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        "aura.runtime.prompt_compilation",
        "client_timezone_validation_outcome",
        outcome="omitted",
    )

    assert metrics.stats().rejected == 0


class _Aura0079FakeTransport:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.requests: list[tuple[str, bytes, str | None, float]] = []

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        self.requests.append((endpoint, payload, credential, timeout))
        if len(self.requests) <= self.failures:
            raise ConnectionError("Collector unavailable")


class _Aura0079BlockingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        del endpoint, payload, credential, timeout
        self.started.set()
        await self.release.wait()


def _aura0079_record(metrics: MetadataMetrics) -> None:
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        trace_id="0123456789abcdef0123456789abcdef",
        run_id=str(uuid4()),
        provider="ollama",
        model_id="fixture-chat",
    )


def _aura0079_deployment() -> DeploymentMetadata:
    return DeploymentMetadata(
        deployment_id="deploy-2026-10-15",
        git_commit="0123456789abcdef",
        environment="test",
        host_id="host-a",
        service_role="worker",
    )


@pytest.mark.asyncio
async def test_aura0079_otlp_sink_serializes_only_metadata_contract_fields() -> None:
    transport = _Aura0079FakeTransport()
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid/v1/observations",
        credential="service-secret",
        transport=transport,
        deployment=_aura0079_deployment(),
    )
    metrics = MetadataMetrics(exporter=exporter)
    _aura0079_record(metrics)

    await exporter.start()
    assert await metrics.flush() is True
    await exporter.stop()

    assert len(transport.requests) == 1
    endpoint, raw_payload, credential, _ = transport.requests[0]
    assert endpoint.endswith("/v1/metrics")
    assert credential == "service-secret"
    payload = json.loads(raw_payload)
    resource = payload["resourceMetrics"][0]["resource"]["attributes"]
    attrs = {
        entry["key"]: next(iter(entry["value"].values()))
        for entry in resource
    }
    assert attrs == {
        "service.name": "aura-core",
        "service.role": "worker",
        "deployment.id": "deploy-2026-10-15",
        "deployment.environment": "test",
        "deployment.git_commit": "0123456789abcdef",
        "host.id": "host-a",
        "capture.policy": "metadata_only",
    }
    datapoint = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"][
        "dataPoints"
    ][0]
    datapoint_attrs = {
        entry["key"]: next(iter(entry["value"].values()))
        for entry in datapoint["attributes"]
    }
    assert datapoint_attrs["aura.capture_policy"] == "metadata_only"
    assert datapoint_attrs["aura.observation_id"]
    assert datapoint_attrs["aura.component_id"] == "aura.runtime.model_routing"
    assert not any(
        forbidden in raw_payload.decode().casefold()
        for forbidden in ("prompt", "response", "filename", "credential")
    )


@pytest.mark.asyncio
async def test_aura0079_otlp_span_uses_standard_trace_signal_route_and_shape() -> None:
    transport = _Aura0079FakeTransport()
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid/v1/observations",
        transport=transport,
        deployment=_aura0079_deployment(),
    )
    metrics = MetadataMetrics(exporter=exporter)
    metrics.record_span(
        "aura.runtime.model_routing",
        "model.route",
        2.0,
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        parent_span_id=None,
        dependency="model_provider",
        outcome="ok",
        run_id=str(uuid4()),
    )
    await exporter.start()
    assert await metrics.flush()
    await exporter.stop()

    endpoint, raw_payload, _, _ = transport.requests[0]
    assert endpoint.endswith("/v1/traces")
    payload = json.loads(raw_payload)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == "0123456789abcdef0123456789abcdef"
    assert span["spanId"] == "0123456789abcdef"
    assert span["name"] == "model.route"
    assert any(item["key"] == "aura.capture_policy" for item in span["attributes"])


def test_aura0079_deployment_metadata_rejects_private_or_invalid_shapes() -> None:
    invalid = (
        {"deployment_id": "https://internal.example"},
        {"deployment_id": "/var/run/aura"},
        {"host_id": "operator@example.com"},
        {"host_id": "a" * 129},
        {"git_commit": "not-a-commit"},
        {"environment": "Owner"},
        {"service_role": "admin"},
    )
    for values in invalid:
        with pytest.raises(ValueError):
            DeploymentMetadata(
                deployment_id=values.get("deployment_id", "deploy-2026-10-15"),
                git_commit=values.get("git_commit", "0123456789abcdef"),
                environment=values.get("environment", "test"),
                host_id=values.get("host_id", "host-a"),
                service_role=values.get("service_role", "worker"),
            )


@pytest.mark.asyncio
async def test_aura0079_otlp_sink_retries_with_bounds_then_drops_without_raising_to_core() -> None:
    transport = _Aura0079FakeTransport(failures=20)
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid/v1/observations",
        transport=transport,
        max_attempts=2,
        queue_capacity=1,
    )
    metrics = MetadataMetrics(exporter=exporter)
    _aura0079_record(metrics)

    await exporter.start()
    assert await metrics.flush() is True
    await asyncio.sleep(0.05)
    await exporter.stop()

    stats = exporter.stats
    assert len(transport.requests) == 2
    assert stats["telemetry_export_retry_count"] == 1
    assert stats["telemetry_export_failure_count"] == 2
    assert stats["telemetry_export_dropped_count"] == 1


@pytest.mark.asyncio
async def test_aura0079_queue_overflow_counts_each_dropped_observation() -> None:
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid",
        queue_capacity=1,
        transport=_Aura0079FakeTransport(),
    )
    metrics = MetadataMetrics(exporter=exporter)
    _aura0079_record(metrics)
    metrics.record_span(
        "aura.runtime.model_routing",
        "model.route",
        1.0,
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        parent_span_id=None,
        dependency="model_provider",
        outcome="ok",
    )

    # Keep the worker stopped so the one-slot queue remains full while the
    # metric and span signal payloads are enqueued independently.
    assert await metrics.flush()
    assert exporter.stats["telemetry_export_accepted_count"] == 1
    assert exporter.stats["telemetry_export_dropped_count"] == 1


@pytest.mark.asyncio
async def test_aura0079_shutdown_counts_queued_and_inflight_observations_once() -> None:
    transport = _Aura0079BlockingTransport()
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid",
        queue_capacity=2,
        timeout_seconds=0.01,
        transport=transport,
    )
    metrics = MetadataMetrics(exporter=exporter)
    _aura0079_record(metrics)
    metrics.record_span(
        "aura.runtime.model_routing",
        "model.route",
        1.0,
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        parent_span_id=None,
        dependency="model_provider",
        outcome="ok",
    )
    await exporter.start()
    assert await metrics.flush()
    await asyncio.wait_for(transport.started.wait(), timeout=0.2)
    await exporter.stop()

    assert exporter.stats["telemetry_export_accepted_count"] == 2
    assert exporter.stats["telemetry_export_dropped_count"] == 2


@pytest.mark.asyncio
async def test_aura0079_structured_health_surface_exposes_otlp_counters() -> None:
    lines: list[str] = []
    transport = _Aura0079FakeTransport(failures=20)
    exporter = OtlpHttpTelemetryExporter(
        "http://collector.invalid",
        transport=transport,
        max_attempts=1,
    )
    structured = StructuredContainerLogExporter(
        lines.append,
        deployment=_aura0079_deployment(),
        health_provider=lambda: exporter.stats,
    )
    metrics = MetadataMetrics(exporter=CompositeTelemetryExporter(structured, exporter))
    _aura0079_record(metrics)
    await exporter.start()
    assert await metrics.flush()
    await asyncio.sleep(0.02)
    # A later structured flush publishes the counters accumulated by the
    # independent network task without making Core depend on its availability.
    _aura0079_record(metrics)
    assert await metrics.flush()
    await exporter.stop()

    health = [json.loads(line) for line in lines if '"event":"aura.telemetry.health"' in line]
    assert health[-1]["telemetry_export_accepted_count"] == 1
    assert health[-1]["telemetry_export_failure_count"] == 1
    assert health[-1]["telemetry_export_dropped_count"] == 1
    assert health[-1]["telemetry_export_retry_count"] == 0
    assert health[-1]["telemetry_export_duration_ms"] >= 0


@pytest.mark.asyncio
async def test_aura0079_structured_log_export_survives_otlp_failure() -> None:
    lines: list[str] = []
    structured = StructuredContainerLogExporter(lines.append, deployment=_aura0079_deployment())
    otlp = OtlpHttpTelemetryExporter(
        "http://collector.invalid/v1/observations",
        transport=_Aura0079FakeTransport(failures=20),
        max_attempts=1,
    )
    metrics = MetadataMetrics(exporter=CompositeTelemetryExporter(structured, otlp))
    _aura0079_record(metrics)

    await otlp.start()
    assert await metrics.flush() is True
    await asyncio.sleep(0.02)
    await otlp.stop()

    assert any(json.loads(line).get("event") == "aura.telemetry.measurement" for line in lines)
    assert otlp.stats["telemetry_export_dropped_count"] == 1


def test_aura0079_otlp_exporter_configuration_is_bounded() -> None:
    with pytest.raises(ValueError):
        OtlpHttpTelemetryExporter("")
    with pytest.raises(ValueError):
        OtlpHttpTelemetryExporter("http://collector.invalid", queue_capacity=0)
    with pytest.raises(ValueError):
        OtlpHttpTelemetryExporter("http://collector.invalid", max_attempts=0)
    with pytest.raises(ValueError):
        OtlpHttpTelemetryExporter("http://collector.invalid", timeout_seconds=0)


def test_aura0079_structured_exporter_remains_available_without_otlp() -> None:
    lines: list[str] = []
    metrics = MetadataMetrics(exporter=StructuredContainerLogExporter(lines.append))
    _aura0079_record(metrics)

    assert metrics.exporter is not None
    assert not isinstance(metrics.exporter, OtlpHttpTelemetryExporter)


def test_aura0079_deployment_metadata_is_bounded_and_not_arbitrary_mapping() -> None:
    metadata = _aura0079_deployment()
    assert set(metadata.__dataclass_fields__) == {
        "deployment_id",
        "git_commit",
        "environment",
        "host_id",
        "service_role",
    }


def test_aura0079_manifest_retention_matches_runtime_queue_and_retry_bounds() -> None:
    manifest_path = (
        Path(__file__).parents[1] / "resources" / "component-manifests" / "telemetry-export.yaml"
    )
    document = cast(dict[str, Any], yaml.safe_load(manifest_path.read_text()))
    retention = cast(dict[str, Any], document["retention"])
    measurement_ring = cast(dict[str, Any], retention["measurement_ring"])
    signal_queue = cast(dict[str, Any], retention["otlp_signal_queue"])
    failure = cast(dict[str, Any], retention["export_failure"])
    attempts = cast(dict[str, int], failure["total_attempts"])

    assert measurement_ring == {
        "capacity": "bounded",
        "overflow": "drop_oldest_and_count",
    }
    assert signal_queue == {
        "capacity": "bounded",
        "full": "drop_new_signal_and_count_each_observation",
    }
    assert attempts == {"minimum": 1, "maximum": 5, "default": 3}
    assert failure["default_retries"] == attempts["default"] - 1
    assert failure["exhausted"] == "drop_and_count_each_observation"

    runtime_default = inspect.signature(OtlpHttpTelemetryExporter).parameters[
        "max_attempts"
    ].default
    assert runtime_default == attempts["default"]
    assert Settings().telemetry_otlp_max_attempts == attempts["default"]
    assert (
        Settings(telemetry_otlp_max_attempts=attempts["minimum"]).telemetry_otlp_max_attempts
        == 1
    )
    assert (
        Settings(telemetry_otlp_max_attempts=attempts["maximum"]).telemetry_otlp_max_attempts
        == 5
    )
