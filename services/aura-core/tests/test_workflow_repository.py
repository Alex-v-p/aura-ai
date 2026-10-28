import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.automation.workflows import repository as workflow_repository
from aura_core.domains.automation.workflows.public import (
    OwnerApprovalStep,
    WorkflowCancelIntent,
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
    admit_instance,
)
from aura_core.domains.automation.workflows.repository import (
    MemoryWorkflowRepository,
    SqlWorkflowRepository,
)
from aura_core.domains.automation.workflows.state import new_state, settle_step, start_step
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
async def test_all_management_mutations_replay_and_reject_idempotency_key_reuse() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Management", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    created = await repository.create_draft(
        revision, issuer="issuer", subject="subject", idempotency_key="draft-key"
    )
    assert await repository.create_draft(
        revision, issuer="issuer", subject="subject", idempotency_key="draft-key"
    ) == created
    regenerated = replace(revision, id=uuid4(), workflow_id=uuid4())
    replayed = await repository.create_draft(
        regenerated, issuer="issuer", subject="subject", idempotency_key="draft-key"
    )
    assert replayed.id == created.id
    assert replayed.workflow_id == created.workflow_id
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.create_draft(
            replace(regenerated, display_name="Different semantic command"),
            issuer="issuer", subject="subject", idempotency_key="draft-key",
        )

    published = await repository.publish(
        created.id, expected_version=1, issuer="issuer", subject="subject",
        idempotency_key="publish-key",
    )
    assert await repository.publish(
        created.id, expected_version=1, issuer="issuer", subject="subject",
        idempotency_key="publish-key",
    ) == published
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.publish(
            created.id, expected_version=2, issuer="issuer", subject="subject",
            idempotency_key="publish-key",
        )

    cloned = await repository.clone_draft(
        "issuer", "subject", published.workflow_id, idempotency_key="clone-key"
    )
    assert await repository.clone_draft(
        "issuer", "subject", published.workflow_id, idempotency_key="clone-key"
    ) == cloned
    updated = replace(cloned, display_name="Updated management")
    assert await repository.update_draft(
        updated, expected_version=cloned.version, issuer="issuer", subject="subject",
        idempotency_key="update-key",
    ) == await repository.update_draft(
        updated, expected_version=cloned.version, issuer="issuer", subject="subject",
        idempotency_key="update-key",
    )
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.update_draft(
            replace(updated, display_name="Different"), expected_version=cloned.version,
            issuer="issuer", subject="subject", idempotency_key="update-key",
        )

    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    state = await repository.get_state("issuer", "subject", instance.id)
    started = start_step(state, execution_id="save-id", expected_version=1).state
    assert await repository.save_state(
        started, expected_version=1, issuer="issuer", subject="subject",
        idempotency_key="save-key",
    ) == await repository.save_state(
        started, expected_version=1, issuer="issuer", subject="subject",
        idempotency_key="save-key",
    )

    retired = await repository.retire_revision(
        "issuer", "subject", created.id, expected_version=2, idempotency_key="retire-key"
    )
    assert await repository.retire_revision(
        "issuer", "subject", created.id, expected_version=2, idempotency_key="retire-key"
    ) == retired
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.retire_revision(
            "issuer", "subject", created.id, expected_version=3, idempotency_key="retire-key"
        )

    definition_revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Definition", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(
        definition_revision, issuer="issuer", subject="subject", idempotency_key="definition-draft"
    )
    definition_published = await repository.publish(
        definition_revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    assert await repository.retire_definition(
        "issuer", "subject", definition_published.workflow_id, expected_version=1,
        idempotency_key="retire-definition-key",
    )
    assert await repository.retire_definition(
        "issuer", "subject", definition_published.workflow_id, expected_version=1,
        idempotency_key="retire-definition-key",
    )


@pytest.mark.asyncio
async def test_idempotency_operation_scope_and_cancel_claim_are_atomic_in_memory() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Operation scope", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    created = await repository.create_draft(
        revision, issuer="issuer", subject="subject", idempotency_key="shared-key"
    )
    assert await repository.get_idempotency_record(
        "issuer", "subject", "shared-key", operation="save_state"
    ) is None
    assert await repository.get_idempotency_record(
        "issuer", "subject", "shared-key", operation="create_draft"
    ) is not None

    published = await repository.publish(
        created.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    state = start_step(
        await repository.get_state("issuer", "subject", instance.id),
        execution_id="cancel-execution", expected_version=1,
    ).state
    await repository.save_state(
        state, expected_version=1, issuer="issuer", subject="subject",
        idempotency_key="shared-key",
    )
    assert await repository.get_idempotency_record(
        "issuer", "subject", "shared-key", operation="create_draft"
    ) is not None
    assert await repository.get_idempotency_record(
        "issuer", "subject", "shared-key", operation="save_state"
    ) is not None
    await repository.record_cancel_intent(
        "issuer", "subject", "cancel-key", instance_id=instance.id,
        execution_id="different-execution",
    )
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.save_state(
            state, expected_version=1, issuer="issuer", subject="subject",
            idempotency_key="cancel-key", semantic_fingerprint="cancel-fingerprint",
            cancel_intent=WorkflowCancelIntent(instance.id, "cancel-execution"),
        )
    unchanged = await repository.get_state("issuer", "subject", instance.id)
    assert unchanged.instance.version == 2
    assert await repository.get_idempotency_record(
        "issuer", "subject", "cancel-key", operation="save_state"
    ) is None


@pytest.mark.asyncio
async def test_sql_cancel_intent_insert_failure_rolls_back_state_claim() -> None:
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "SQL atomicity", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    instance = admit_instance(
        revision, now=datetime(2026, 1, 1, tzinfo=UTC), input_values={}
    )
    state = start_step(
        new_state(revision, instance), execution_id="sql-cancel", expected_version=1
    ).state

    class Session:
        rolled_back = False
        added: list[object]
        scalar_calls = 0

        def __init__(self) -> None:
            self.added = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, exc_type: object, *_args: object) -> None:
            self.rolled_back = exc_type is not None

        async def scalar(self, _statement: object) -> object:
            self.scalar_calls += 1
            return SimpleNamespace(version=1) if self.scalar_calls == 1 else None

        def add(self, _value: object) -> None:
            operation = getattr(_value, "operation", None)
            if operation == "cancel_propagation":
                raise RuntimeError("simulated cancellation-intent insert failure")
            self.added.append(_value)

    class Sessions:
        def __init__(self) -> None:
            self.session = Session()
            self.begin_calls = 0

        def begin(self) -> Session:
            self.begin_calls += 1
            return self.session

    sessions = Sessions()
    repository = SqlWorkflowRepository(cast(Any, sessions))
    with pytest.raises(RuntimeError, match="intent insert failure"):
        await repository.save_state(
            state, expected_version=1, issuer="issuer", subject="subject",
            idempotency_key="sql-cancel-key", semantic_fingerprint="cancel-fingerprint",
            cancel_intent=WorkflowCancelIntent(instance.id, "sql-cancel"),
        )
    assert sessions.session.rolled_back is True
    assert sessions.begin_calls == 1
    assert not any(
        getattr(item, "operation", None) == "cancel_propagation"
        for item in sessions.session.added
    )


@pytest.mark.asyncio
async def test_sql_idempotency_lookup_is_operation_scoped() -> None:
    statements: list[object] = []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, statement: object) -> None:
            statements.append(statement)
            return None

    class Sessions:
        def __call__(self) -> Session:
            return Session()

    repository = SqlWorkflowRepository(cast(Any, Sessions()))
    assert await repository.get_idempotency_record(
        "issuer", "subject", "shared-key", operation="cancel_propagation"
    ) is None
    rendered = str(statements[0]).lower()
    assert "operation" in rendered
    assert "idempotency_key" in rendered


@pytest.mark.asyncio
async def test_deletion_receipt_replays_after_target_removal_and_binds_parent_lineage() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Receipt", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    retired = await repository.retire_revision(
        "issuer", "subject", revision.id, expected_version=published.version
    )
    first = await repository.delete_receipt(
        "issuer", "subject", retired.workflow_id, retired.id,
        scope=WorkflowDeletionScope.PUBLISHED_REVISION,
        expected_version=retired.version, idempotency_key="receipt-key",
    )
    assert first.deleted is True
    replay = await repository.delete_receipt(
        "issuer", "subject", retired.workflow_id, retired.id,
        scope=WorkflowDeletionScope.PUBLISHED_REVISION,
        expected_version=retired.version, idempotency_key="receipt-key",
    )
    assert replay.deleted is True
    assert replay.replayed is True
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.delete_receipt(
            "issuer", "subject", uuid4(), retired.id,
            scope=WorkflowDeletionScope.PUBLISHED_REVISION,
            expected_version=retired.version, idempotency_key="receipt-key",
        )


@pytest.mark.asyncio
async def test_missing_deletion_is_a_stable_false_receipt_and_replays_after_no_target() -> None:
    """A lost response cannot turn a no-op deletion into a later mutation."""

    repository = MemoryWorkflowRepository()
    workflow_id = uuid4()
    missing_revision = uuid4()
    first = await repository.delete_receipt(
        "issuer", "subject", workflow_id, missing_revision,
        scope=WorkflowDeletionScope.DRAFT,
        expected_version=1,
        idempotency_key="missing-delete",
    )
    replay = await repository.delete_receipt(
        "issuer", "subject", workflow_id, missing_revision,
        scope=WorkflowDeletionScope.DRAFT,
        expected_version=1,
        idempotency_key="missing-delete",
    )

    assert first.deleted is False
    assert first.replayed is True
    assert replay.deleted is False
    assert replay.replayed is True


@pytest.mark.asyncio
async def test_clone_requires_definition_version_and_preserves_immutable_source() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Clone source", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    assert await repository.retire_definition(
        "issuer", "subject", published.workflow_id, expected_version=1
    )

    with pytest.raises(WorkflowVersionConflict):
        await repository.clone_draft(
            "issuer", "subject", published.workflow_id,
            revision_id=published.id, expected_definition_version=1,
        )

    cloned = await repository.clone_draft(
        "issuer", "subject", published.workflow_id,
        revision_id=published.id, expected_definition_version=2,
    )
    assert cloned.id != published.id
    assert cloned.status is WorkflowRevisionStatus.DRAFT
    assert cloned.version == 1
    assert cloned.created_at is not None
    assert cloned.updated_at is not None
    assert cloned.published_at is None
    assert cloned.retired_at is None
    assert published.created_at is not None
    assert published.updated_at is not None
    source = await repository.get_revision("issuer", "subject", published.id)
    assert source.status is WorkflowRevisionStatus.PUBLISHED
    assert source.version == published.version
    assert source.created_at == published.created_at
    assert source.updated_at == published.updated_at

    other = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Other source", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(other, issuer="issuer", subject="subject")
    other_published = await repository.publish(
        other.id, expected_version=1, issuer="issuer", subject="subject"
    )
    with pytest.raises(WorkflowNotFound):
        await repository.clone_draft(
            "issuer", "subject", published.workflow_id, revision_id=other_published.id,
            expected_definition_version=2,
        )


@pytest.mark.asyncio
async def test_memory_replays_aggregate_lifecycle_events_without_instance_lineage() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Aggregate replay", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    retired_revision = await repository.retire_revision(
        "issuer", "subject", revision.id, expected_version=published.version
    )
    await repository.retire_definition(
        "issuer", "subject", published.workflow_id, expected_version=1
    )

    page = await repository.replay_events("issuer", "subject")
    event_types = [str(event["eventType"]) for event in page.events]
    assert event_types == [
        "workflow.revision.published",
        "workflow.revision.retired",
        "workflow.definition.retired",
    ]
    assert all("instanceId" not in event for event in page.events)
    assert retired_revision.retired_at is not None


@pytest.mark.asyncio
async def test_terminal_instance_deletion_replays_closed_sse_scope() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Terminal deletion", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.DRAFT,
    )
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    instance = await repository.admit("issuer", "subject", published.id, input_values={})
    state = start_step(
        await repository.get_state("issuer", "subject", instance.id),
        execution_id="terminal-approval", expected_version=1,
    ).state
    state = settle_step(
        state,
        execution_id="terminal-approval", expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": "2026-01-01T00:00:00+00:00"},
    ).state
    await repository.save_state(state, expected_version=1, issuer="issuer", subject="subject")
    assert await repository.delete(
        "issuer", "subject", instance.id,
        scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
    )

    page = await repository.replay_events("issuer", "subject")
    deletion = next(
        event for event in page.events if event["eventType"] == "workflow.retention.deleted"
    )
    assert deletion["deletionScope"] == "terminal_instance_state"
    assert deletion["tombstoneRetained"] is True
    assert set(deletion) == {
        "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
        "instanceId", "deletionScope", "tombstoneRetained", "version",
        "occurredAt", "eventSequence",
    }
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
async def test_sql_recovery_scan_filters_due_waits_before_bounded_limit() -> None:
    statements: list[object] = []
    running_id, waiting_id = uuid4(), uuid4()

    class ScalarResult:
        def __init__(self, values: list[object]) -> None:
            self.values = values

        def all(self) -> list[object]:
            return self.values

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def scalars(self, statement: object) -> ScalarResult:
            statements.append(statement)
            return ScalarResult([running_id] if len(statements) == 1 else [waiting_id])

    class Sessions:
        def __call__(self) -> Session:
            return Session()

    repository = SqlWorkflowRepository(cast(Any, Sessions()))
    assert await repository.recovery_scan(limit=2, now=datetime(2026, 1, 1, tzinfo=UTC)) == (
        running_id, waiting_id
    )
    assert len(statements) == 2
    waiting_sql = str(statements[1]).lower()
    assert "next_due_at" in waiting_sql
    assert "next_due_at <=" in waiting_sql
    assert "limit" in waiting_sql
    assert waiting_sql.index("next_due_at") < waiting_sql.index("limit")


def test_lifecycle_publish_retire_and_settled_step_envelopes_match_closed_schema() -> None:
    revision = WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Lifecycle", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    state = start_step(
        new_state(
            revision,
            admit_instance(revision, now=datetime(2026, 1, 1, tzinfo=UTC), input_values={}),
        ),
        execution_id="approval-step",
        expected_version=1,
    ).state
    aggregate_name = "_aggregate_lifecycle_envelope"
    lifecycle_name = "_lifecycle_envelope"
    aggregate_lifecycle_envelope = getattr(workflow_repository, aggregate_name)
    lifecycle_envelope = getattr(workflow_repository, lifecycle_name)
    definition_event = aggregate_lifecycle_envelope(
        "workflow.revision.published", issuer="issuer", subject="subject",
        workflow_id=revision.workflow_id, revision_id=revision.id, version=2,
        status="published", resource_id=revision.id,
    )
    retired_event = aggregate_lifecycle_envelope(
        "workflow.definition.retired", issuer="issuer", subject="subject",
        workflow_id=revision.workflow_id, revision_id=None, version=2,
        status="retired", resource_id=revision.workflow_id,
    )
    step_event = lifecycle_envelope(state, "approval_settled", uuid4())
    expected_definition = {
        "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
        "version", "occurredAt",
    }
    expected_step = {
        "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
        "instanceId", "stepExecutionId", "stepOrdinal", "stepKind", "stepStatus",
        "effectOutcome", "recoveryCapabilities", "instanceVersion", "occurredAt",
    }
    assert set(definition_event) == expected_definition
    assert set(retired_event) == expected_definition
    assert set(step_event) == expected_step


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
