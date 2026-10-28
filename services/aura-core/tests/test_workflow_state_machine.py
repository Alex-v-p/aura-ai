from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.automation.workflows.public import (
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    Selector,
    WorkflowEffectOutcome,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStep,
    WorkflowStepStatus,
    WorkflowTerminalOutcome,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowValueType,
    admit_instance,
)
from aura_core.domains.automation.workflows.state import (
    cancel,
    new_state,
    reconcile,
    request_replay,
    settle_step,
    start_step,
)
from aura_core.platform.telemetry import MetadataMetrics

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def revision(*steps: WorkflowStep) -> WorkflowRevision:
    return WorkflowRevision(
        uuid4(), uuid4(), "issuer", "subject", "Safe workflow",
        (InputField("prompt", WorkflowValueType.BOUNDED_TEXT),),
        tuple(steps), status=WorkflowRevisionStatus.PUBLISHED,
    )


def test_closed_revision_rejects_later_selector_and_more_than_one_agent() -> None:
    with pytest.raises(WorkflowValidationError):
        revision(AgentRunStep("agent_a", "run", Selector("step_output", "output", 1)))
    with pytest.raises(WorkflowValidationError):
        revision(
            AgentRunStep("agent_a", "run", Selector("instance_input", "prompt")),
            AgentRunStep("agent_b", "run", Selector("instance_input", "prompt")),
        )


def test_sequential_success_advances_and_terminalizes_last_step() -> None:
    rev = revision(
        AgentRunStep("agent_a", "run", Selector("instance_input", "prompt")),
        OwnerApprovalStep(),
    )
    instance = admit_instance(rev, now=NOW, input_values={"prompt": "hello"})
    state = new_state(rev, instance)
    started = start_step(state, execution_id="run_1", expected_version=1, now=NOW).state
    advanced = settle_step(
        started, execution_id="run_1", expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"status": "completed", "output": "done"},
    ).state
    assert advanced.instance.status is WorkflowInstanceStatus.QUEUED
    assert advanced.instance.current_ordinal == 2
    started = start_step(advanced, execution_id="approval_1", expected_version=3, now=NOW).state
    terminal = settle_step(
        started, execution_id="approval_1", expected_version=4,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": NOW.isoformat()},
    ).state
    assert terminal.instance.status is WorkflowInstanceStatus.TERMINAL
    assert terminal.instance.terminal_outcome is WorkflowTerminalOutcome.COMPLETED


def test_unknown_effect_only_allows_reconciliation_and_cannot_replay() -> None:
    rev = revision(AgentRunStep("agent_a", "run", Selector("instance_input", "prompt")))
    state = new_state(rev, admit_instance(rev, now=NOW, input_values={"prompt": "hello"}))
    state = start_step(state, execution_id="run_1", expected_version=1, now=NOW).state
    state = settle_step(
        state, execution_id="run_1", expected_version=2,
        status=WorkflowStepStatus.RESOLUTION_REQUIRED,
        effect_outcome=WorkflowEffectOutcome.UNKNOWN,
    ).state
    assert state.instance.status is WorkflowInstanceStatus.RESOLUTION_REQUIRED
    with pytest.raises(WorkflowTransitionError):
        request_replay(state, expected_version=3)
    canceled = cancel(state, expected_version=3).state
    assert canceled.instance.terminal_outcome is WorkflowTerminalOutcome.CANCELED
    late = reconcile(canceled, expected_version=4, confirmed=True) if False else canceled
    assert late.instance.terminal_outcome is WorkflowTerminalOutcome.CANCELED


def test_replayable_failure_allows_exactly_one_replay_then_terminal_failure() -> None:
    rev = revision(OwnerApprovalStep())
    state = new_state(rev, admit_instance(rev, now=NOW, input_values={"prompt": "hello"}))
    state = start_step(state, execution_id="approval_1", expected_version=1, now=NOW).state
    state = settle_step(
        state, execution_id="approval_1", expected_version=2,
        status=WorkflowStepStatus.FAILED_REPLAYABLE,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
        replayable=True,
    ).state
    state = request_replay(state, expected_version=3, now=NOW).state
    with pytest.raises(WorkflowTransitionError):
        request_replay(state, expected_version=4)
    state = start_step(state, execution_id="approval_1", expected_version=4, now=NOW).state
    state = settle_step(
        state, execution_id="approval_1", expected_version=5,
        status=WorkflowStepStatus.FAILED,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
    ).state
    assert state.instance.terminal_outcome is WorkflowTerminalOutcome.FAILED


def test_agent_failures_are_never_replayable_and_result_fields_are_closed() -> None:
    rev = revision(AgentRunStep("agent_a", "run", Selector("instance_input", "prompt")))
    state = new_state(rev, admit_instance(rev, now=NOW, input_values={"prompt": "hello"}))
    state = start_step(state, execution_id="run_1", expected_version=1, now=NOW).state
    with pytest.raises(WorkflowTransitionError):
        settle_step(
            state, execution_id="run_1", expected_version=2,
            status=WorkflowStepStatus.FAILED_REPLAYABLE,
            effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
            replayable=True,
        )
    with pytest.raises(WorkflowValidationError):
        settle_step(
            state, execution_id="run_1", expected_version=2,
            status=WorkflowStepStatus.COMPLETED,
            effect_outcome=WorkflowEffectOutcome.COMPLETED,
            output={"status": "completed", "output": "done", "control": "x"},
        )


def test_stale_version_and_terminal_state_are_rejected() -> None:
    rev = revision(OwnerApprovalStep())
    state = new_state(rev, admit_instance(rev, now=NOW, input_values={"prompt": "hello"}))
    with pytest.raises(WorkflowTransitionError):
        start_step(state, execution_id="approval_1", expected_version=0)
    terminal = cancel(state, expected_version=1).state
    assert terminal.instance.deadline > NOW + timedelta(days=29)
    assert cancel(terminal, expected_version=2).replayed


def test_instance_deadline_is_closed_to_thirty_days() -> None:
    with pytest.raises(WorkflowValidationError):
        WorkflowInstance(
            uuid4(), uuid4(), uuid4(), "issuer", "subject", 1,
            WorkflowInstanceStatus.QUEUED, 1, NOW, NOW + timedelta(days=31), {},
        )


def test_transition_telemetry_is_bounded_and_trace_only() -> None:
    metrics = MetadataMetrics()
    rev = revision(OwnerApprovalStep())
    state = new_state(rev, admit_instance(rev, now=NOW, input_values={"prompt": "hello"}))
    instrumented_start = cast(Any, start_step)
    instrumented_settle = cast(Any, settle_step)
    started = instrumented_start(
        state,
        execution_id="approval_1",
        expected_version=1,
        now=NOW,
        metrics=metrics,
        trace_id="1234567890abcdef1234567890abcdef",
        parent_span_id="1234567890abcdef",
    ).state
    instrumented_settle(
        started,
        execution_id="approval_1",
        expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": NOW.isoformat()},
        metrics=metrics,
    )
    retained = metrics.snapshot()
    assert {item.metric for item in retained} == {
        "workflow_transition_outcome",
        "workflow_transition_duration_ms",
    }
    assert all(dict(item.dimensions)["operation"] == "workflow.transition" for item in retained)
    assert all("issuer" not in repr(item) and "subject" not in repr(item) for item in retained)
    assert metrics.stats().rejected == 0
    spans = [item for item in retained if item.kind == "span"]
    assert spans and spans[0].trace_id == "1234567890abcdef1234567890abcdef"
    assert spans[0].parent_span_id == "1234567890abcdef"
    assert all(item.trace_id and set(item.trace_id) != {"0"} for item in spans)
    assert dict(spans[0].dimensions)["dependency"] == "application_validation"
