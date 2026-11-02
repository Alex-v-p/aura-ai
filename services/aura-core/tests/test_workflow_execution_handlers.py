"""AURA-0137 execution, recovery, and privacy acceptance tests.

These tests intentionally exercise the public workflow value objects and the
deterministic state boundary.  Handler implementations may call these APIs
from a worker, but executor-private payloads must never become workflow state
or ordinary telemetry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from aura_core.domains.automation.workflows.handlers import ApprovalHandler
from aura_core.domains.automation.workflows.public import (
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    OwnerNotificationStep,
    Selector,
    WaitUntilStep,
    WorkflowEffectOutcome,
    WorkflowIdempotencyConflict,
    WorkflowInstanceStatus,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStepExecution,
    WorkflowStepKind,
    WorkflowStepStatus,
    WorkflowTerminalOutcome,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowValueType,
    admit_instance,
    validate_step_result,
)
from aura_core.domains.automation.workflows.repository import MemoryWorkflowRepository
from aura_core.domains.automation.workflows.state import (
    cancel,
    checkpoint_payload,
    new_state,
    reconcile,
    request_replay,
    settle_step,
    start_step,
)
from aura_core.domains.governance.approvals.public import CreateWorkflowApproval
from aura_core.platform.telemetry import MetadataMetrics

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _revision(*steps: Any, inputs: tuple[InputField, ...] = ()) -> WorkflowRevision:
    return WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "bounded execution", inputs,
        tuple(steps), status=WorkflowRevisionStatus.PUBLISHED,
    )


def _start(state: Any, execution_id: str = "step-execution") -> Any:
    return start_step(
        state,
        execution_id=execution_id,
        expected_version=state.instance.version,
        now=NOW,
    ).state


@pytest.mark.asyncio
async def test_approval_handler_preserves_workflow_origin_and_stable_step_identity() -> None:
    captured: list[CreateWorkflowApproval] = []

    class ApprovalPort:
        async def create_workflow_pending(self, command: CreateWorkflowApproval) -> object:
            captured.append(command)
            return object()

    workflow_id, revision_id, instance_id = uuid4(), uuid4(), uuid4()
    execution_id = str(uuid4())
    execution = WorkflowStepExecution(
        workflow_id, revision_id, instance_id, 1, execution_id, ("issuer", "subject"),
        None, {}, NOW + timedelta(days=1),
    )
    result = await ApprovalHandler(ApprovalPort()).execute(
        execution, step=OwnerApprovalStep(), display_name="Approval workflow",
        workspace_scope_id="workspace",
    )
    command = captured[0]
    assert result.status is WorkflowStepStatus.WAITING_FOR_APPROVAL
    assert command.workflow_id == workflow_id
    assert command.workflow_revision_id == revision_id
    assert command.workflow_instance_id == instance_id
    assert command.workflow_step_execution_id == UUID(execution_id)
    assert command.idempotency_key == f"workflow:{instance_id}:{execution_id}"


@pytest.mark.asyncio
async def test_approval_handler_preserves_successor_and_deadline_bounds() -> None:
    captured: list[CreateWorkflowApproval] = []

    class ApprovalPort:
        async def create_workflow_pending(self, command: CreateWorkflowApproval) -> object:
            captured.append(command)
            return object()

    deadline = datetime.now(UTC) + timedelta(days=2)
    execution = WorkflowStepExecution(
        uuid4(), uuid4(), uuid4(), 1, str(uuid4()), ("issuer", "subject"), None, {}, deadline,
    )
    result = await ApprovalHandler(ApprovalPort()).execute(
        execution, step=OwnerApprovalStep(), display_name="Approval workflow",
        workspace_scope_id="workspace", next_step_kind="owner_notification",
    )

    assert result.status is WorkflowStepStatus.WAITING_FOR_APPROVAL
    assert captured[0].next_step_kind == "owner_notification"
    assert captured[0].expires_at <= deadline
    assert captured[0].instance_deadline == deadline


def test_all_four_closed_handlers_progress_in_order_without_dynamic_edges() -> None:
    revision = _revision(
        AgentRunStep("agent_revision", "bounded instruction", Selector("instance_input", "prompt")),
        OwnerNotificationStep(Selector("step_output", "output", 1)),
        WaitUntilStep(Selector("instance_input", "due_at")),
        OwnerApprovalStep(),
        inputs=(
            InputField("prompt", WorkflowValueType.BOUNDED_TEXT),
            InputField("due_at", WorkflowValueType.INSTANT),
        ),
    )
    state = new_state(
        revision,
        admit_instance(
            revision,
            now=NOW,
            input_values={"prompt": "synthetic", "due_at": "2026-01-02T12:00:00+00:00"},
        ),
    )

    results = (
        (WorkflowStepKind.AGENT_RUN, {"status": "completed", "output": "agent-result"}),
        (
            WorkflowStepKind.OWNER_NOTIFICATION,
            {"notification_id": "notification-1", "status": "inbox_available"},
        ),
        (WorkflowStepKind.WAIT_UNTIL, {"resumed_at": "2026-01-02T12:00:00+00:00"}),
        (WorkflowStepKind.OWNER_APPROVAL, {"decision": "approved", "decided_at": NOW.isoformat()}),
    )
    for ordinal, (kind, output) in enumerate(results, start=1):
        assert state.current.kind is kind
        execution_id = f"step-{ordinal}"
        state = _start(state, execution_id)
        state = settle_step(
            state,
            execution_id=execution_id,
            expected_version=state.instance.version,
            status=WorkflowStepStatus.COMPLETED,
            effect_outcome=WorkflowEffectOutcome.COMPLETED,
            output=output,
            now=NOW,
        ).state
    assert state.instance.status is WorkflowInstanceStatus.TERMINAL
    assert state.instance.terminal_outcome is WorkflowTerminalOutcome.COMPLETED


@pytest.mark.parametrize(
    ("kind", "output"),
    (
        (WorkflowStepKind.AGENT_RUN, {"status": "completed", "output": "ok"}),
        (WorkflowStepKind.OWNER_APPROVAL, {"decision": "approved", "decided_at": NOW}),
        (
            WorkflowStepKind.OWNER_NOTIFICATION,
            {"notification_id": "n-1", "status": "inbox_available"},
        ),
        (WorkflowStepKind.WAIT_UNTIL, {"resumed_at": NOW}),
    ),
)
def test_each_handler_result_is_closed_and_rejects_executor_payloads(
    kind: WorkflowStepKind, output: dict[str, object]
) -> None:
    assert validate_step_result(
        kind, WorkflowStepStatus.COMPLETED, WorkflowEffectOutcome.COMPLETED, output
    ) is not None
    with pytest.raises(WorkflowValidationError):
        validate_step_result(
            kind,
            WorkflowStepStatus.COMPLETED,
            WorkflowEffectOutcome.COMPLETED,
            {**output, "provider_payload": "private"},
        )


def test_unknown_effect_can_only_reconcile_or_cancel_and_never_reopens_terminal_state() -> None:
    revision = _revision(
        OwnerNotificationStep(Selector("instance_input", "message")),
        inputs=(InputField("message", WorkflowValueType.BOUNDED_TEXT),),
    )
    state = new_state(
        revision,
        admit_instance(revision, now=NOW, input_values={"message": "hello"}),
    )
    state = _start(state)
    state = settle_step(
        state,
        execution_id="step-execution",
        expected_version=state.instance.version,
        status=WorkflowStepStatus.RESOLUTION_REQUIRED,
        effect_outcome=WorkflowEffectOutcome.UNKNOWN,
    ).state
    assert state.instance.status is WorkflowInstanceStatus.RESOLUTION_REQUIRED
    with pytest.raises(WorkflowTransitionError):
        request_replay(state, expected_version=state.instance.version, now=NOW)
    terminal = reconcile(
        state,
        expected_version=state.instance.version,
        confirmed=False,
        now=NOW,
    ).state
    assert terminal.instance.terminal_outcome is WorkflowTerminalOutcome.FAILED
    replay = cancel(terminal, expected_version=terminal.instance.version, now=NOW)
    assert replay.replayed
    assert replay.state.instance.terminal_outcome is WorkflowTerminalOutcome.FAILED


def test_only_non_agent_handlers_allow_one_replay_and_second_request_is_rejected() -> None:
    revision = _revision(OwnerApprovalStep())
    state = new_state(revision, admit_instance(revision, now=NOW, input_values={}))
    state = _start(state, "approval-execution")
    state = settle_step(
        state,
        execution_id="approval-execution",
        expected_version=state.instance.version,
        status=WorkflowStepStatus.FAILED_REPLAYABLE,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
        replayable=True,
        now=NOW,
    ).state
    state = request_replay(state, expected_version=state.instance.version, now=NOW).state
    with pytest.raises(WorkflowTransitionError):
        request_replay(state, expected_version=state.instance.version, now=NOW)

    agent_revision = _revision(
        AgentRunStep("agent_revision", "bounded", Selector("instance_input", "prompt")),
        inputs=(InputField("prompt", WorkflowValueType.BOUNDED_TEXT),),
    )
    agent_state = _start(
        new_state(
            agent_revision,
            admit_instance(agent_revision, now=NOW, input_values={"prompt": "x"}),
        ),
        "agent-execution",
    )
    with pytest.raises(WorkflowTransitionError):
        settle_step(
            agent_state,
            execution_id="agent-execution",
            expected_version=agent_state.instance.version,
            status=WorkflowStepStatus.FAILED_REPLAYABLE,
            effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
            replayable=True,
            now=NOW,
        )


def test_cancellation_is_idempotent_and_does_not_reverse_completed_effects() -> None:
    revision = _revision(OwnerApprovalStep())
    state = new_state(revision, admit_instance(revision, now=NOW, input_values={}))
    running = _start(state)
    canceled = cancel(running, expected_version=running.instance.version, now=NOW).state
    assert canceled.instance.terminal_outcome is WorkflowTerminalOutcome.CANCELED
    assert canceled.current.status is WorkflowStepStatus.CANCELED
    assert cancel(canceled, expected_version=canceled.instance.version, now=NOW).replayed

    completed = settle_step(
        running,
        execution_id="step-execution",
        expected_version=running.instance.version,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": NOW.isoformat()},
        now=NOW,
    ).state
    assert completed.instance.terminal_outcome is WorkflowTerminalOutcome.COMPLETED
    assert cancel(completed, expected_version=completed.instance.version, now=NOW).replayed
    assert completed.instance.terminal_outcome is WorkflowTerminalOutcome.COMPLETED


def test_checkpoint_is_bounded_and_metadata_only() -> None:
    revision = _revision(OwnerApprovalStep())
    state = new_state(revision, admit_instance(revision, now=NOW, input_values={}))
    payload = checkpoint_payload(state)
    assert set(payload) == {
        "instance_id", "revision_id", "current_ordinal", "instance_status",
        "instance_version", "step_status", "step_version", "deadline", "execution_id",
    }
    assert not {"input", "output", "instructions", "owner", "prompt"} & set(payload)


def test_declared_workflow_components_have_exact_metadata_only_manifests() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    expected: dict[str, tuple[str, set[str], set[str]]] = {
        "aura.automation.workflow_definition": (
            "workflow_definition_validation_duration_ms",
            {"workflow.definition.v1"},
            {"workflow_definition_validation_duration_ms"},
        ),
        "aura.automation.workflow_step.agent_run": (
            "workflow_agent_run_step_duration_ms",
            {"workflow.agent-run.v1"},
            {"workflow_agent_run_step_duration_ms"},
        ),
        "aura.automation.workflow_approval": (
            "workflow_approval_command_duration_ms",
            {"workflow.authorization.v1"},
            {"workflow_approval_command_duration_ms"},
        ),
        "aura.automation.workflow_step.notification": (
            "workflow_notification_step_duration_ms",
            {"workflow.notification-step.v1"},
            {"workflow_notification_step_duration_ms"},
        ),
        "aura.automation.workflow_step.wait_until": (
            "workflow_wait_command_duration_ms",
            {"workflow.wait-step.v1"},
            {"workflow_wait_command_duration_ms", "workflow_wait_lag_ms"},
        ),
        "aura.automation.workflow_checkpoint": (
            "workflow_checkpoint_persistence_duration_ms",
            {"workflow.checkpoint.v1"},
            {"workflow_checkpoint_persistence_duration_ms"},
        ),
        "aura.automation.workflow_cancellation": (
            "workflow_cancellation_duration_ms",
            {"workflow.cancellation.v1"},
            {"workflow_cancellation_duration_ms"},
        ),
        "aura.automation.workflow_settlement": (
            "workflow_settlement_duration_ms",
            {"workflow.state-machine.v1"},
            {"workflow_settlement_duration_ms"},
        ),
    }
    manifests = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in manifest_dir.glob("workflow-*.yaml")
    ]
    by_id = {manifest["component"]["id"]: manifest for manifest in manifests}
    assert set(expected) <= set(by_id)
    assert "aura.automation.workflow_scheduler" not in by_id
    for component_id, (duration_metric, evaluator_subset, metric_subset) in expected.items():
        manifest = by_id[component_id]
        assert manifest["component"]["version"] == "1.0.0"
        assert manifest["capture_policy"] == "metadata_only"
        assert metric_subset <= set(manifest["metrics"])
        assert duration_metric in manifest["metrics"]
        assert evaluator_subset <= set(manifest["evaluators"])
        assert set(manifest["cardinality"]["metric_attributes"]) <= {
            "operation", "outcome", "error_class", "dependency", "status", "step_kind",
        }


def test_declared_workflow_metrics_are_registered_with_bounded_dimensions() -> None:
    metrics_by_component = {
        "aura.automation.workflow_definition": ("workflow_definition_validation_duration_ms",),
        "aura.automation.workflow_step.agent_run": ("workflow_agent_run_step_duration_ms",),
        "aura.automation.workflow_approval": ("workflow_approval_command_duration_ms",),
        "aura.automation.workflow_step.notification": ("workflow_notification_step_duration_ms",),
        "aura.automation.workflow_step.wait_until": (
            "workflow_wait_command_duration_ms", "workflow_wait_lag_ms",
        ),
        "aura.automation.workflow_checkpoint": ("workflow_checkpoint_persistence_duration_ms",),
        "aura.automation.workflow_cancellation": ("workflow_cancellation_duration_ms",),
        "aura.automation.workflow_settlement": ("workflow_settlement_duration_ms",),
    }
    metrics = MetadataMetrics()
    for component, names in metrics_by_component.items():
        for name in names:
            metrics.observe(
                component,
                name,
                1.0,
                operation="workflow.command",
                outcome="completed",
                error_class="internal",
                dependency="application_validation",
                status="running",
                workflow_id=str(uuid4()),
            )
    retained = metrics.snapshot()
    assert {item.metric for item in retained} == {
        name for names in metrics_by_component.values() for name in names
    }
    assert all(
        set(dict(item.dimensions)).issubset(
            {"operation", "outcome", "error_class", "dependency", "status", "step_kind"}
        )
        for item in retained
    )
    assert all("workflow_id" in dict(item.trace_attributes) for item in retained)
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_concurrent_admission_with_same_key_has_one_durable_effect() -> None:
    repository = MemoryWorkflowRepository()
    revision = _revision(OwnerApprovalStep())
    await repository.create_draft(revision, issuer="issuer", subject="subject")
    published = await repository.publish(
        revision.id, expected_version=1, issuer="issuer", subject="subject"
    )
    admitted = await asyncio.gather(
        *(
            repository.admit(
                "issuer", "subject", published.id, input_values={}, now=NOW,
                idempotency_key="same-admission",
            )
            for _ in range(8)
        )
    )
    assert {item.id for item in admitted}.__len__() == 1
    with pytest.raises(WorkflowIdempotencyConflict):
        await repository.admit(
            "issuer", "subject", published.id, input_values={"unexpected": "x"},
            now=NOW, idempotency_key="same-admission",
        )


def test_deadline_expiry_terminalizes_without_running_a_side_effect() -> None:
    revision = _revision(OwnerApprovalStep())
    admitted = admit_instance(revision, now=NOW, input_values={})
    expired = admit_instance(
        revision,
        now=NOW - timedelta(days=30),
        input_values={},
    )
    state = new_state(revision, expired)
    receipt = start_step(
        state,
        execution_id="expired-step",
        expected_version=expired.version,
        now=expired.deadline,
    )
    assert receipt.state.instance.terminal_outcome is WorkflowTerminalOutcome.FAILED
    assert admitted.status is WorkflowInstanceStatus.QUEUED
