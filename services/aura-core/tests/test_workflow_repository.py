import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.automation.workflows.public import (
    OwnerApprovalStep,
    WorkflowDeletionScope,
    WorkflowEffectOutcome,
    WorkflowIdempotencyConflict,
    WorkflowNotFound,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStepStatus,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowVersionConflict,
)
from aura_core.domains.automation.workflows.repository import (
    MemoryWorkflowRepository,
    SqlWorkflowRepository,
)
from aura_core.domains.automation.workflows.state import settle_step, start_step
from aura_core.platform.telemetry import MetadataMetrics


@pytest.mark.asyncio
async def test_memory_repository_scopes_owner_and_deduplicates_admission() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    revision = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    first = await repository.admit(
        "issuer", "subject", revision.id,
        input_values={}, now=datetime(2026, 1, 1, tzinfo=UTC), idempotency_key="admit-1",
    )
    duplicate = await repository.admit(
        "issuer", "subject", revision.id,
        input_values={}, now=datetime(2026, 1, 1, tzinfo=UTC), idempotency_key="admit-1",
    )
    assert duplicate.id == first.id
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.admit(
            "issuer", "subject", revision.id,
            input_values={"extra": "value"}, idempotency_key="admit-1",
        )
    with pytest.raises(WorkflowNotFound):
        await repository.get_instance("other", "subject", first.id)


@pytest.mark.asyncio
async def test_mutating_repository_commands_require_authenticated_owner() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    with pytest.raises(TypeError):
        await cast(Any, repository.create_draft)(revision)
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    with pytest.raises(TypeError):
        await cast(Any, repository.publish)(revision.id, expected_version=1)
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    state = await repository.get_state("issuer", "subject", instance.id)
    with pytest.raises(TypeError):
        await cast(Any, repository.save_state)(state, expected_version=1)


@pytest.mark.asyncio
async def test_terminal_instance_deletion_keeps_tombstone_and_rejects_active_delete() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    with pytest.raises(WorkflowTransitionError):
        await repository.delete(
            "issuer", "subject", instance.id, scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
            expected_version=1,
        )


@pytest.mark.asyncio
async def test_each_retained_instance_scope_is_terminal_only_and_idempotent() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    for scope in (
        WorkflowDeletionScope.INSTANCE_HISTORY,
        WorkflowDeletionScope.INSTANCE_RESULT,
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
    ):
        with pytest.raises(WorkflowTransitionError):
            await repository.delete(
                "issuer", "subject", instance.id, scope=scope, expected_version=1
            )
    state = await repository.get_state("issuer", "subject", instance.id)
    state = start_step(state, execution_id="approval", expected_version=1).state
    state = settle_step(
        state, execution_id="approval", expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": "2026-01-01T00:00:00+00:00"},
    ).state
    await repository.save_state(state, expected_version=1, issuer="issuer", subject="subject")
    await repository.delete(
        "issuer", "subject", instance.id, scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
    )
    for scope in (
        WorkflowDeletionScope.INSTANCE_RESULT,
        WorkflowDeletionScope.INSTANCE_HISTORY,
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
    ):
        assert await repository.delete(
            "issuer", "subject", instance.id, scope=scope, expected_version=3,
            idempotency_key=f"delete-{scope.value}",
        )
        assert await repository.delete(
            "issuer", "subject", instance.id, scope=scope, expected_version=3,
            idempotency_key=f"delete-{scope.value}",
        )
    with pytest.raises(WorkflowNotFound):
        await repository.delete(
            "foreign", "subject", instance.id,
            scope=WorkflowDeletionScope.INSTANCE_RESULT,
            expected_version=3,
        )


@pytest.mark.asyncio
async def test_repository_telemetry_is_bounded_metadata_only() -> None:
    class Metrics:
        def __init__(self) -> None:
            self.metrics: list[tuple[str, str, dict[str, str]]] = []
            self.spans: list[tuple[str, str, dict[str, str]]] = []

        def increment(self, component: str, name: str, **attributes: str) -> None:
            self.metrics.append((component, name, attributes))

        def record_span(
            self, component: str, operation: str, duration: float, **attributes: str
        ) -> None:
            del duration
            self.spans.append((component, operation, attributes))

    metrics = Metrics()
    repository = MemoryWorkflowRepository(metrics=metrics)  # type: ignore[arg-type]
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    assert metrics.metrics
    assert metrics.spans
    assert any(name == "workflow_command_outcome" for _, name, _ in metrics.metrics)
    assert any(operation == "workflow.command" for _, operation, _ in metrics.spans)
    for _, _, dimensions in metrics.metrics:
        assert set(dimensions) <= {
            "operation", "outcome", "trace_id", "workflow_id", "revision_id", "result_version"
        }
        assert "issuer" not in dimensions and "subject" not in dimensions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "scope", "deletes"),
    [
        ("draft", WorkflowDeletionScope.PUBLISHED_REVISION, False),
        ("published", WorkflowDeletionScope.PUBLISHED_REVISION, False),
        ("retired", WorkflowDeletionScope.PUBLISHED_REVISION, True),
        ("draft", WorkflowDeletionScope.DRAFT, True),
        ("published", WorkflowDeletionScope.DRAFT, False),
        ("retired", WorkflowDeletionScope.DRAFT, False),
    ],
)
async def test_revision_status_and_deletion_scope_matrix(
    status: str, scope: WorkflowDeletionScope, deletes: bool
) -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    if status == "published":
        await repository.publish(
            revision.id, expected_version=1, issuer="issuer", subject="subject"
        )
    elif status == "retired":
        await repository.publish(
            revision.id, expected_version=1, issuer="issuer", subject="subject"
        )
        await repository.retire_revision("issuer", "subject", revision.id, expected_version=2)
    if deletes:
        expected = {"draft": 1, "published": 2, "retired": 3}[status]
        assert await repository.delete(
            "issuer", "subject", revision.id, scope=scope, expected_version=expected
        )
    else:
        with pytest.raises(WorkflowTransitionError):
            await repository.delete(
                "issuer", "subject", revision.id, scope=scope,
                expected_version={"draft": 1, "published": 2, "retired": 3}[status],
            )


@pytest.mark.asyncio
async def test_retired_revision_can_be_deleted_before_terminal_instance_tombstone() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    state = await repository.get_state("issuer", "subject", instance.id)
    state = start_step(state, execution_id="approval", expected_version=1).state
    state = settle_step(
        state,
        execution_id="approval",
        expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": "2026-01-01T00:00:00+00:00"},
    ).state
    await repository.save_state(state, expected_version=1, issuer="issuer", subject="subject")
    await repository.retire_revision("issuer", "subject", revision.id, expected_version=2)
    assert await repository.delete(
        "issuer", "subject", revision.id, scope=WorkflowDeletionScope.PUBLISHED_REVISION,
        expected_version=3,
    )
    assert await repository.delete(
        "issuer", "subject", instance.id, scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
    )


@pytest.mark.asyncio
async def test_all_deletion_scopes_reject_stale_versions_and_replay_idempotently() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    with pytest.raises(WorkflowVersionConflict):
        await repository.delete(
            "issuer", "subject", revision.id, scope=WorkflowDeletionScope.DRAFT,
            expected_version=2,
        )
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    await repository.retire_revision("issuer", "subject", revision.id, expected_version=2)
    with pytest.raises(WorkflowVersionConflict):
        await repository.delete(
            "issuer", "subject", revision.id,
            scope=WorkflowDeletionScope.PUBLISHED_REVISION, expected_version=2,
        )
    await repository.retire_definition(
        "issuer", "subject", published.workflow_id, expected_version=1
    )
    with pytest.raises(WorkflowVersionConflict):
        await repository.delete(
            "issuer", "subject", published.workflow_id,
            scope=WorkflowDeletionScope.RETIRED_DEFINITION, expected_version=1,
        )

    active_revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Active", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(active_revision, issuer="issuer", subject="subject")
    active_revision = await repository.publish(
        active_revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", active_revision.id, input_values={})
    state = await repository.get_state("issuer", "subject", instance.id)
    state = start_step(state, execution_id="approval", expected_version=1).state
    state = settle_step(
        state,
        execution_id="approval",
        expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": "2026-01-01T00:00:00+00:00"},
    ).state
    await repository.save_state(state, expected_version=1, issuer="issuer", subject="subject")
    for scope in (
        WorkflowDeletionScope.TERMINAL_INSTANCE,
        WorkflowDeletionScope.INSTANCE_HISTORY,
        WorkflowDeletionScope.INSTANCE_RESULT,
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
    ):
        with pytest.raises(WorkflowVersionConflict):
            await repository.delete(
                "issuer", "subject", instance.id, scope=scope, expected_version=2,
            )
    assert await repository.delete(
        "issuer", "subject", instance.id,
        scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
        idempotency_key="terminal-delete",
    )
    assert await repository.delete(
        "issuer", "subject", instance.id,
        scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
        idempotency_key="terminal-delete",
    )
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.delete(
            "issuer", "subject", instance.id,
            scope=WorkflowDeletionScope.INSTANCE_HISTORY,
            expected_version=3,
            idempotency_key="terminal-delete",
        )


@pytest.mark.asyncio
async def test_production_repository_telemetry_has_nonzero_trace_topology() -> None:
    metrics = MetadataMetrics()
    repository = MemoryWorkflowRepository(metrics=metrics)
    context = WorkflowTraceContext("e" * 32, "f" * 16)
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(
        revision, issuer="issuer", subject="subject", trace_context=context
    )
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject",
        trace_context=context,
    )
    instance = await repository.admit(
        "issuer", "subject", published.id, input_values={}, trace_context=context
    )
    await repository.get_state("issuer", "subject", instance.id, trace_context=context)
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert metrics.stats().rejected == 0
    assert spans and all(item.trace_id == context.trace_id for item in spans)
    commands = [
        item for item in spans
        if dict(item.trace_attributes).get("operation") == "workflow.command"
    ]
    repositories = [
        item for item in spans
        if dict(item.trace_attributes).get("operation") == "workflow.repository"
    ]
    assert commands and repositories
    assert all(item.parent_span_id == context.parent_span_id for item in commands)
    assert all(
        item.parent_span_id == commands[index].span_id
        for index, item in enumerate(repositories[: len(commands)])
    )
    assert repositories[-1].parent_span_id == context.parent_span_id
    assert all(dict(item.dimensions)["dependency"] == "application_validation" for item in spans)
    assert {dict(item.dimensions)["operation"] for item in spans} == {
        "workflow.repository", "workflow.command"
    }


@pytest.mark.asyncio
async def test_repository_trace_contexts_are_isolated_under_concurrent_commands() -> None:
    metrics = MetadataMetrics()
    repository = MemoryWorkflowRepository(metrics=metrics)
    revisions = [
        WorkflowRevision(
            uuid4(), uuid4(), f"issuer-{index}", f"subject-{index}", "Demo", (),
            (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
        )
        for index in range(2)
    ]
    contexts = [
        WorkflowTraceContext("a" * 32, "1" * 16),
        WorkflowTraceContext("b" * 32, "2" * 16),
    ]
    await asyncio.gather(*(
        repository.create_draft(
            revision, issuer=revision.issuer, subject=revision.subject,
            trace_context=context,
        )
        for revision, context in zip(revisions, contexts, strict=True)
    ))
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert metrics.stats().rejected == 0
    for context in contexts:
        owned = [item for item in spans if item.trace_id == context.trace_id]
        commands = [
            item for item in owned
            if dict(item.trace_attributes).get("operation") == "workflow.command"
        ]
        repositories = [
            item for item in owned
            if dict(item.trace_attributes).get("operation") == "workflow.repository"
        ]
        assert commands and repositories
        assert all(item.parent_span_id == context.parent_span_id for item in commands)
        assert all(item.parent_span_id == commands[0].span_id for item in repositories)
    assert {item.trace_id for item in spans} == {context.trace_id for context in contexts}


@pytest.mark.asyncio
async def test_sql_repository_uses_postgresql_dependency_with_explicit_context() -> None:
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def scalar(self, statement: object) -> object:
            del statement
            now = datetime(2026, 1, 1, tzinfo=UTC)
            return SimpleNamespace(
                id=uuid4(), workflow_id=uuid4(), revision_id=uuid4(),
                principal_issuer="issuer", principal_subject="subject",
                current_ordinal=1, status="queued", version=1,
                admitted_at=now, deadline=now + timedelta(days=30),
                input_values={}, terminal_outcome=None, replay_count=0,
            )

    class Sessions:
        def __call__(self) -> Session:
            return Session()

    metrics = MetadataMetrics()
    context = WorkflowTraceContext("9" * 32, "8" * 16)
    repository = SqlWorkflowRepository(cast(Any, Sessions()), metrics=metrics)
    await repository.get_instance("issuer", "subject", uuid4(), trace_context=context)
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert spans and dict(spans[-1].dimensions)["dependency"] == "postgresql"
    assert spans[-1].trace_id == context.trace_id
    assert spans[-1].parent_span_id == context.parent_span_id


@pytest.mark.asyncio
async def test_command_and_transition_share_explicit_trace_parent() -> None:
    metrics = MetadataMetrics()
    repository = MemoryWorkflowRepository(metrics=metrics)
    context = WorkflowTraceContext("c" * 32, "d" * 16)
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Demo", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(
        revision, issuer="issuer", subject="subject", trace_context=context
    )
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject",
        trace_context=context,
    )
    instance = await repository.admit(
        "issuer", "subject", published.id, input_values={}, trace_context=context
    )
    command_spans = [
        item for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "workflow.command"
    ]
    assert command_spans and command_spans[-1].span_id is not None
    command_context = WorkflowTraceContext(context.trace_id, command_spans[-1].span_id)
    state = await repository.get_state(
        "issuer", "subject", instance.id, trace_context=command_context
    )
    started = cast(Any, start_step)(
        state, execution_id="approval", expected_version=1, metrics=metrics,
        trace_context=command_context,
    )
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert metrics.stats().rejected == 0
    assert spans and all(item.trace_id == context.trace_id for item in spans)
    children = [
        item for item in spans
        if dict(item.trace_attributes).get("operation") in {
            "workflow.repository", "workflow.transition",
        }
    ]
    assert children and all(
        item.parent_span_id == command_spans[-1].span_id for item in children[-2:]
    )
    assert started.state.instance.version == 2
