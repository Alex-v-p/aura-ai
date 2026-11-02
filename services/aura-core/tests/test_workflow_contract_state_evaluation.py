"""Metadata-only evaluations for the bounded AURA-0136 workflow state slice.

These fixtures exercise the public workflow contracts and deterministic state
machine as an evaluator would: the report contains only bounded identifiers,
statuses, versions, and outcome counts.  No workflow input, output,
instructions, mappings, or owner identity is placed in an evaluation result.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from aura_core.domains.automation.workflows.public import (
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    OwnerNotificationStep,
    Selector,
    WorkflowDeletionScope,
    WorkflowEffectOutcome,
    WorkflowIdempotencyConflict,
    WorkflowInstanceStatus,
    WorkflowNotFound,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStep,
    WorkflowStepStatus,
    WorkflowTerminalOutcome,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowValueType,
    admit_instance,
    canonical_json,
)
from aura_core.domains.automation.workflows.repository import MemoryWorkflowRepository
from aura_core.domains.automation.workflows.state import (
    cancel,
    new_state,
    reconcile,
    request_replay,
    settle_step,
    start_step,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
FIXTURE_REVISION = "workflow-contract-state-eval-v1"
WORKFLOW_COMPONENT = ("aura.automation.workflow_state", "1.0.0")
WORKFLOW_COMPONENT_ID, WORKFLOW_COMPONENT_VERSION = WORKFLOW_COMPONENT


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    component_id: str
    component_version: str
    fixture_revision: str
    scenario_id: str
    case_id: str
    replay_key: str
    score: float
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "scenarioId": self.scenario_id,
            "caseId": self.case_id,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


EVALUATOR_IDS = (
    "workflow_state_transition_correctness",
    "workflow_idempotency_and_race_correctness",
)
EVALUATION_CASES = (
    ("workflow_state_transition_correctness", "transition", "sequential_completion"),
    ("workflow_idempotency_and_race_correctness", "repository", "idempotent_race"),
    ("workflow_state_transition_correctness", "recovery", "unknown_effect"),
    ("workflow_state_transition_correctness", "recovery", "single_replay"),
    ("workflow_state_transition_correctness", "terminal", "immutable_settlement"),
    ("workflow_state_transition_correctness", "validation", "selector_boundary"),
    ("workflow_state_transition_correctness", "retention", "deletion_ordering"),
)
CASE_TO_EVALUATOR = {(scenario, case): evaluator for evaluator, scenario, case in EVALUATION_CASES}


def _result(
    evaluator_id: str,
    scenario_id: str,
    case_id: str,
    passed: bool,
    **metadata: str,
) -> EvaluationResult:
    forbidden = {
        "content",
        "credential",
        "input",
        "instructions",
        "mapping",
        "output",
        "payload",
        "prompt",
        "response",
        "result",
        "secret",
    }
    if forbidden & set(metadata):
        raise AssertionError("workflow evaluator metadata must remain metadata-only")
    if evaluator_id not in EVALUATOR_IDS:
        raise AssertionError("workflow evaluator is not declared by the component manifest")
    if CASE_TO_EVALUATOR.get((scenario_id, case_id)) != evaluator_id:
        raise AssertionError("workflow scenario/case is not declared for the evaluator")
    component_id, component_version = WORKFLOW_COMPONENT
    return EvaluationResult(
        evaluator_id,
        component_id,
        component_version,
        FIXTURE_REVISION,
        scenario_id,
        case_id,
        f"{component_id}@{component_version}:{evaluator_id}:{FIXTURE_REVISION}:{scenario_id}:{case_id}",
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        tuple(sorted(metadata.items())),
    )


def _revision(*steps: WorkflowStep, inputs: tuple[InputField, ...] = ()) -> WorkflowRevision:
    return WorkflowRevision(
        uuid4(),
        uuid4(),
        "evaluation-issuer",
        "evaluation-subject",
        "Synthetic workflow",
        inputs,
        tuple(steps),
        status=WorkflowRevisionStatus.PUBLISHED,
    )


def _agent_revision() -> WorkflowRevision:
    return _revision(
        AgentRunStep(
            "agent_revision",
            "bounded synthetic instruction",
            Selector("instance_input", "prompt"),
        ),
        inputs=(InputField("prompt", WorkflowValueType.BOUNDED_TEXT),),
    )


def _approval_revision() -> WorkflowRevision:
    return _revision(OwnerApprovalStep())


def _start(state: Any, execution_id: str = "execution_1") -> Any:
    return start_step(
        state, execution_id=execution_id, expected_version=state.instance.version, now=NOW
    ).state


async def _transition_result() -> EvaluationResult:
    revision = _revision(
        AgentRunStep(
            "agent_revision",
            "bounded synthetic instruction",
            Selector("instance_input", "prompt"),
        ),
        OwnerApprovalStep(),
        inputs=(InputField("prompt", WorkflowValueType.BOUNDED_TEXT),),
    )
    instance = admit_instance(revision, now=NOW, input_values={"prompt": "synthetic"})
    state = _start(new_state(revision, instance))
    advanced = settle_step(
        state,
        execution_id="execution_1",
        expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"status": "completed", "output": "synthetic-result"},
    ).state
    started_next = _start(advanced, "execution_2")
    terminal = settle_step(
        started_next,
        execution_id="execution_2",
        expected_version=4,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": NOW.isoformat()},
    ).state
    final_outcome = terminal.instance.terminal_outcome
    assert final_outcome is not None
    passed = (
        advanced.instance.status is WorkflowInstanceStatus.QUEUED
        and advanced.instance.current_ordinal == 2
        and terminal.instance.status is WorkflowInstanceStatus.TERMINAL
        and final_outcome is WorkflowTerminalOutcome.COMPLETED
    )
    return _result(
        "workflow_state_transition_correctness",
        "transition",
        "sequential_completion",
        passed,
        transition_count="4",
        final_status=terminal.instance.status.value,
        final_outcome=final_outcome.value,
    )


async def _idempotency_race_result() -> EvaluationResult:
    repository = MemoryWorkflowRepository()
    revision = _approval_revision()
    await repository.create_draft(
        revision, issuer="evaluation-issuer", subject="evaluation-subject"
    )
    published = await repository.publish(
        revision.id,
        expected_version=1,
        issuer="evaluation-issuer",
        subject="evaluation-subject",
    )
    admissions = await asyncio.gather(
        repository.admit(
            "evaluation-issuer",
            "evaluation-subject",
            published.id,
            input_values={},
            now=NOW,
            idempotency_key="same-command",
        ),
        repository.admit(
            "evaluation-issuer",
            "evaluation-subject",
            published.id,
            input_values={},
            now=NOW,
            idempotency_key="same-command",
        ),
    )
    conflict = False
    try:
        await repository.admit(
            "evaluation-issuer",
            "evaluation-subject",
            published.id,
            input_values={"unexpected": "synthetic"},
            now=NOW,
            idempotency_key="same-command",
        )
    except WorkflowIdempotencyConflict:
        conflict = True

    first_state = await repository.get_state(
        "evaluation-issuer", "evaluation-subject", admissions[0].id
    )
    started = _start(first_state)
    saved = await repository.save_state(
        started, expected_version=1, issuer="evaluation-issuer", subject="evaluation-subject"
    )
    stale_rejected = False
    try:
        await repository.save_state(
            first_state,
            expected_version=1,
            issuer="evaluation-issuer",
            subject="evaluation-subject",
        )
    except Exception as exc:  # The public repository exposes a typed conflict.
        stale_rejected = exc.__class__.__name__ == "WorkflowVersionConflict"
    passed = (
        admissions[0].id == admissions[1].id
        and admissions[0].version == admissions[1].version == 1
        and conflict
        and saved.instance.version == 2
        and stale_rejected
    )
    return _result(
        "workflow_idempotency_and_race_correctness",
        "repository",
        "idempotent_race",
        passed,
        concurrent_admissions="2",
        unique_instance_count="1" if admissions[0].id == admissions[1].id else "2",
        stale_version="rejected" if stale_rejected else "accepted",
    )


async def _unknown_effect_result() -> EvaluationResult:
    revision = _agent_revision()
    state = _start(
        new_state(revision, admit_instance(revision, now=NOW, input_values={"prompt": "synthetic"}))
    )
    unresolved = settle_step(
        state,
        execution_id="execution_1",
        expected_version=2,
        status=WorkflowStepStatus.RESOLUTION_REQUIRED,
        effect_outcome=WorkflowEffectOutcome.UNKNOWN,
    ).state
    replay_rejected = False
    try:
        request_replay(unresolved, expected_version=3, now=NOW)
    except WorkflowTransitionError:
        replay_rejected = True
    canceled = cancel(unresolved, expected_version=3, now=NOW).state
    reconcile_rejected = False
    try:
        reconcile(canceled, expected_version=4, confirmed=True, output="late-result", now=NOW)
    except WorkflowTransitionError:
        reconcile_rejected = True
    passed = (
        unresolved.instance.status is WorkflowInstanceStatus.RESOLUTION_REQUIRED
        and bool(unresolved.current.recovery_capabilities)
        and replay_rejected
        and canceled.instance.terminal_outcome is WorkflowTerminalOutcome.CANCELED
        and reconcile_rejected
    )
    return _result(
        "workflow_state_transition_correctness",
        "recovery",
        "unknown_effect",
        passed,
        unresolved_status=unresolved.instance.status.value,
        replay_after_unknown="rejected" if replay_rejected else "accepted",
        late_reconciliation="rejected" if reconcile_rejected else "accepted",
    )


async def _replay_limit_result() -> EvaluationResult:
    revision = _approval_revision()
    state = _start(new_state(revision, admit_instance(revision, now=NOW, input_values={})))
    failed = settle_step(
        state,
        execution_id="execution_1",
        expected_version=2,
        status=WorkflowStepStatus.FAILED_REPLAYABLE,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
        replayable=True,
    ).state
    replayed = request_replay(failed, expected_version=3, now=NOW).state
    second_replay_rejected = False
    try:
        request_replay(replayed, expected_version=4, now=NOW)
    except WorkflowTransitionError:
        second_replay_rejected = True
    passed = (
        failed.instance.status is WorkflowInstanceStatus.WAITING_FOR_RESOLUTION
        and replayed.instance.status is WorkflowInstanceStatus.QUEUED
        and replayed.instance.replay_count == 1
        and second_replay_rejected
    )
    return _result(
        "workflow_state_transition_correctness",
        "recovery",
        "single_replay",
        passed,
        replay_count=str(replayed.instance.replay_count),
        second_replay="rejected" if second_replay_rejected else "accepted",
    )


async def _terminal_immutability_result() -> EvaluationResult:
    revision = _approval_revision()
    state = _start(new_state(revision, admit_instance(revision, now=NOW, input_values={})))
    canceled = cancel(state, expected_version=2, now=NOW).state
    duplicate_cancel = cancel(canceled, expected_version=3, now=NOW)
    late_settlement = settle_step(
        canceled,
        execution_id="execution_1",
        expected_version=3,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output="late-result",
    )
    terminal_outcome = canceled.instance.terminal_outcome
    assert terminal_outcome is not None
    passed = (
        canceled.instance.status is WorkflowInstanceStatus.TERMINAL
        and canceled.instance.terminal_outcome is WorkflowTerminalOutcome.CANCELED
        and duplicate_cancel.replayed
        and not late_settlement.changed
        and late_settlement.replayed
        and late_settlement.state == canceled
    )
    return _result(
        "workflow_state_transition_correctness",
        "terminal",
        "immutable_settlement",
        passed,
        terminal_outcome=terminal_outcome.value,
        duplicate_cancel="replayed" if duplicate_cancel.replayed else "changed",
        late_settlement="replayed" if late_settlement.replayed else "changed",
    )


def _selector_result() -> EvaluationResult:
    rejected = 0
    for candidate in (
        lambda: Selector("unknown_source", "field"),
        lambda: Selector("instance_input", "not-valid"),
        lambda: Selector("step_output", "output", 0),
        lambda: AgentRunStep("agent_revision", "x", Selector("step_output", "output", 1)),
    ):
        try:
            candidate()
        except WorkflowValidationError:
            rejected += 1
    try:
        WorkflowRevision(
            uuid4(),
            uuid4(),
            "evaluation-issuer",
            "evaluation-subject",
            "Synthetic workflow",
            (),
            (OwnerNotificationStep(Selector("step_output", "unknown", 1)),),
            status=WorkflowRevisionStatus.PUBLISHED,
        )
    except WorkflowValidationError:
        rejected += 1
    canonical_rejected = False
    try:
        canonical_json(float("nan"))
    except WorkflowValidationError:
        canonical_rejected = True
    if canonical_rejected:
        rejected += 1
    passed = rejected == 5
    return _result(
        "workflow_state_transition_correctness",
        "validation",
        "selector_boundary",
        passed,
        rejected_selector_cases=str(rejected),
        non_finite_value="rejected" if canonical_rejected else "accepted",
        capture_policy="metadata_only",
    )


async def _deletion_result() -> EvaluationResult:
    repository = MemoryWorkflowRepository()
    revision = _approval_revision()
    draft = await repository.create_draft(
        revision, issuer="evaluation-issuer", subject="evaluation-subject"
    )
    published = await repository.publish(
        draft.id,
        expected_version=1,
        issuer="evaluation-issuer",
        subject="evaluation-subject",
    )
    instance = await repository.admit(
        "evaluation-issuer", "evaluation-subject", published.id, input_values={}, now=NOW
    )
    active_rejected = False
    try:
        await repository.delete(
            "evaluation-issuer",
            "evaluation-subject",
            instance.id,
            scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
            expected_version=1,
        )
    except WorkflowTransitionError:
        active_rejected = True
    state = await repository.get_state("evaluation-issuer", "evaluation-subject", instance.id)
    terminal = settle_step(
        _start(state),
        execution_id="execution_1",
        expected_version=2,
        status=WorkflowStepStatus.COMPLETED,
        effect_outcome=WorkflowEffectOutcome.COMPLETED,
        output={"decision": "approved", "decided_at": NOW.isoformat()},
    ).state
    await repository.save_state(
        terminal,
        expected_version=1,
        issuer="evaluation-issuer",
        subject="evaluation-subject",
    )
    deleted = await repository.delete(
        "evaluation-issuer",
        "evaluation-subject",
        instance.id,
        scope=WorkflowDeletionScope.TERMINAL_INSTANCE,
        expected_version=3,
    )

    async def foreign_delete_attempt(scope: WorkflowDeletionScope) -> bool:
        try:
            return await repository.delete(
                "evaluation-foreign",
                "evaluation-subject",
                instance.id,
                scope=scope,
                expected_version=3,
            )
        except WorkflowNotFound:
            return False

    foreign_delete = await foreign_delete_attempt(WorkflowDeletionScope.INSTANCE_HISTORY)
    foreign_result_delete = await foreign_delete_attempt(WorkflowDeletionScope.INSTANCE_RESULT)
    foreign_checkpoint_delete = await foreign_delete_attempt(
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS
    )
    history_deleted = await repository.delete(
        "evaluation-issuer",
        "evaluation-subject",
        instance.id,
        scope=WorkflowDeletionScope.INSTANCE_HISTORY,
        expected_version=3,
    )
    passed = (
        active_rejected
        and deleted
        and not foreign_delete
        and not foreign_result_delete
        and not foreign_checkpoint_delete
        and history_deleted
    )
    return _result(
        "workflow_state_transition_correctness",
        "retention",
        "deletion_ordering",
        passed,
        active_delete="rejected" if active_rejected else "accepted",
        terminal_delete="completed" if deleted else "missing",
        foreign_delete="rejected" if not foreign_delete else "accepted",
        history_delete="completed" if history_deleted else "missing",
    )


async def _results() -> tuple[EvaluationResult, ...]:
    return (
        await _transition_result(),
        await _idempotency_race_result(),
        await _unknown_effect_result(),
        await _replay_limit_result(),
        await _terminal_immutability_result(),
        _selector_result(),
        await _deletion_result(),
    )


@pytest.mark.asyncio
async def test_workflow_evaluators_are_replayable_and_metadata_only() -> None:
    first = await _results()
    second = await _results()
    assert first == second
    assert {item.evaluator_id for item in first} == set(EVALUATOR_IDS)
    assert {(item.scenario_id, item.case_id) for item in first} == {
        (scenario, case) for _, scenario, case in EVALUATION_CASES
    }
    assert {(item.component_id, item.component_version) for item in first} == {WORKFLOW_COMPONENT}
    assert all(item.outcome == "pass" and item.score == 1.0 for item in first)
    assert len({item.replay_key for item in first}) == len(first)
    for item in first:
        assert item.replay_key.startswith(
            f"{item.component_id}@{item.component_version}:{item.evaluator_id}:"
        )
        payload = item.payload()
        assert set(payload) == {
            "evaluatorId",
            "componentId",
            "componentVersion",
            "fixtureRevision",
            "scenarioId",
            "caseId",
            "replayKey",
            "score",
            "outcome",
            "metadata",
        }
        encoded = json.dumps(payload, sort_keys=True)
        assert not any(
            forbidden in encoded.lower()
            for forbidden in ("synthetic-result", "synthetic-decision", "instruction", "prompt")
        )


def test_workflow_evaluator_payload_is_closed_and_redacted() -> None:
    result = _result(
        "workflow_state_transition_correctness",
        "transition",
        "sequential_completion",
        True,
        final_status="terminal",
        final_outcome="completed",
    )
    payload = result.payload()
    assert set(payload) == {
        "evaluatorId",
        "componentId",
        "componentVersion",
        "fixtureRevision",
        "scenarioId",
        "caseId",
        "replayKey",
        "score",
        "outcome",
        "metadata",
    }
    assert "evaluation-subject" not in repr(payload)
    assert "synthetic" not in repr(payload).lower()


def test_workflow_evaluation_cases_cover_manifest_requirements() -> None:
    assert set(EVALUATOR_IDS) == {
        "workflow_state_transition_correctness",
        "workflow_idempotency_and_race_correctness",
    }
    assert len(EVALUATION_CASES) == 7
    assert set(CASE_TO_EVALUATOR.values()) == set(EVALUATOR_IDS)
    assert WORKFLOW_COMPONENT == (
        "aura.automation.workflow_state",
        "1.0.0",
    )
