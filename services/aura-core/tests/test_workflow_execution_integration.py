"""Independent integration checks for the composed AURA-0137 boundary.

These tests intentionally exercise public composition seams instead of private
ORM rows.  They are also allowed to fail while a work-item implementation is
in flight: each assertion documents a release invariant that a reviewer must
be able to observe from the running Core boundary.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from aura_core.bootstrap.workflow_uow import AdmissionGatedWorkflowApplication
from aura_core.domains.automation.workflows.application import WorkflowApplicationService
from aura_core.domains.automation.workflows.handlers import AgentRunHandler, WaitUntilHandler
from aura_core.domains.automation.workflows.public import (
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    Selector,
    WaitUntilStep,
    WorkflowAuthoritySnapshot,
    WorkflowEffectOutcome,
    WorkflowIdempotencyConflict,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStepStatus,
    WorkflowTransitionError,
    WorkflowValueType,
    admit_instance,
)
from aura_core.domains.automation.workflows.repository import (
    MemoryWorkflowRepository,
    SqlWorkflowRepository,
)
from aura_core.domains.automation.workflows.state import new_state, settle_step, start_step
from aura_core.domains.execution.runs.models import (
    WorkflowAgentRunOutcome,
    WorkflowAgentRunResult,
    WorkflowRunLineage,
)
from aura_core.domains.execution.runs.models import (
    WorkflowAgentRunRequest as RunAgentRequest,
)
from aura_core.domains.execution.runs.service import RunCoordinatorWorkflowAdapter
from aura_core.domains.governance.approvals.public import WorkflowApprovalOrigin
from aura_core.platform.telemetry import MetadataMetrics

OWNER = ("issuer", "subject")
NOW = datetime.now(UTC).replace(microsecond=0)


class _AllowAuthority:
    async def derive(
        self, issuer: str, subject: str, *, revision: WorkflowRevision
    ) -> WorkflowAuthoritySnapshot:
        return WorkflowAuthoritySnapshot(issuer, subject)

    async def revalidate(self, *_args: object, **_kwargs: object) -> bool:
        return True


def _wait_revision(*, due_at: str = "2026-01-01T11:59:00+00:00") -> WorkflowRevision:
    return WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Restart wait",
        (InputField("due_at", WorkflowValueType.INSTANT),),
        (WaitUntilStep(Selector("instance_input", "due_at")),),
        status=WorkflowRevisionStatus.PUBLISHED,
    )


@pytest.mark.asyncio
async def test_identifier_only_worker_continuation_reloads_persisted_state_and_resumes_due_wait(
) -> None:
    repository = MemoryWorkflowRepository()
    revision = _wait_revision()
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    metrics = MetadataMetrics()
    service = WorkflowApplicationService(
        cast(Any, repository), authority=_AllowAuthority(),
        wait_until=WaitUntilHandler(clock=lambda: NOW),
        metrics=metrics,
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    instance = await service.admit(
        *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"}, now=NOW,
        idempotency_key="wake-once", authority=authority,
    )

    resumed = await service.execute_identifier(instance.id)

    assert resumed.instance.status.value == "terminal"
    assert resumed.instance.terminal_outcome is not None
    assert resumed.current.execution_id is not None
    wait_lag = [item for item in metrics.snapshot() if item.metric == "workflow_wait_lag_ms"]
    assert wait_lag and all(item.value >= 0 for item in wait_lag)


@pytest.mark.asyncio
async def test_admission_idempotency_replays_and_rejects_changed_command() -> None:
    repository = MemoryWorkflowRepository()
    revision = _wait_revision()
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    first = await repository.admit(
        *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"},
        now=NOW, idempotency_key="same",
    )
    replay = await repository.admit(
        *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"},
        now=NOW, idempotency_key="same",
    )
    assert replay.id == first.id
    with pytest.raises(Exception, match="idempot"):
        await repository.admit(
            *OWNER, published.id, input_values={"due_at": "2026-01-02T11:59:00+00:00"},
            now=NOW, idempotency_key="same",
        )


@pytest.mark.asyncio
async def test_admission_and_resumption_fail_closed_when_authority_is_revoked() -> None:
    class RevokedAuthority:
        async def derive(
            self, issuer: str, subject: str, *, revision: WorkflowRevision
        ) -> WorkflowAuthoritySnapshot:
            return WorkflowAuthoritySnapshot(issuer, subject)

        async def revalidate(self, *_args: object, **_kwargs: object) -> bool:
            return False

    repository = MemoryWorkflowRepository()
    revision = _wait_revision()
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    service = WorkflowApplicationService(cast(Any, repository), authority=RevokedAuthority())
    authority = WorkflowAuthoritySnapshot(*OWNER, grants=("workflow_execute",))
    with pytest.raises(WorkflowTransitionError, match="revoked"):
        await service.admit(
            *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"},
            authority=authority, now=NOW,
        )


@pytest.mark.asyncio
async def test_cancel_and_recovery_commands_replay_semantically_and_conflict_on_reuse() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Retry commands", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    service = WorkflowApplicationService(cast(Any, repository), authority=_AllowAuthority())
    instance = await service.admit(
        *OWNER, published.id, input_values={}, authority=authority, now=NOW
    )
    canceled = await service.cancel(
        *OWNER, instance.id, expected_version=1, idempotency_key="cancel-retry"
    )
    assert await service.cancel(
        *OWNER, instance.id, expected_version=1, idempotency_key="cancel-retry"
    ) == canceled
    with pytest.raises(WorkflowIdempotencyConflict):
        await service.cancel(
            *OWNER, instance.id, expected_version=2, idempotency_key="cancel-retry"
        )

    replay_revision = WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Replay retry", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(
        replay_revision, issuer=OWNER[0], subject=OWNER[1]
    )
    replay_published = await repository.publish(
        replay_revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    replay_instance = await service.admit(
        *OWNER, replay_published.id, input_values={}, authority=authority, now=NOW
    )
    replay_state = await repository.get_state(*OWNER, replay_instance.id)
    replay_state = cast(Any, start_step)(
        replay_state, execution_id="replay-step", expected_version=1, now=NOW
    ).state
    replay_state = cast(Any, settle_step)(
        replay_state,
        execution_id="replay-step",
        expected_version=2,
        status=WorkflowStepStatus.FAILED_REPLAYABLE,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
        replayable=True,
        now=NOW,
    ).state
    await repository.save_state(
        replay_state, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    recovered = await service.request_replay(
        *OWNER, replay_instance.id, expected_version=3,
        step_execution_id="replay-step", idempotency_key="recovery-retry",
        authority=authority,
    )
    assert await service.request_replay(
        *OWNER, replay_instance.id, expected_version=3,
        step_execution_id="replay-step", idempotency_key="recovery-retry",
        authority=authority,
    ) == recovered
    with pytest.raises(WorkflowIdempotencyConflict):
        await service.request_replay(
            *OWNER, replay_instance.id, expected_version=4,
            step_execution_id="replay-step", idempotency_key="recovery-retry",
            authority=authority,
        )


@pytest.mark.asyncio
async def test_cancel_retry_repropagates_after_downstream_failure() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self.cancel_lineages: list[WorkflowRunLineage] = []

        async def execute_workflow_agent_run(
            self, request: RunAgentRequest
        ) -> WorkflowAgentRunResult:
            del request
            raise AssertionError("execution is not part of this cancellation test")

        async def cancel_workflow_agent_run(self, lineage: WorkflowRunLineage) -> bool:
            self.cancel_calls += 1
            self.cancel_lineages.append(lineage)
            if self.cancel_calls == 1:
                raise RuntimeError("transient coordinator failure")
            return True

    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Cancelable agent",
        (InputField("input", WorkflowValueType.BOUNDED_TEXT),),
        (AgentRunStep(str(uuid4()), "run", Selector("instance_input", "input")),),
        status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    instance = await repository.admit(
        *OWNER, published.id, input_values={"input": "x"}, authority=authority, now=NOW
    )
    running = cast(Any, start_step)(
        await repository.get_state(*OWNER, instance.id),
        execution_id=str(uuid4()), expected_version=1, now=NOW,
    ).state
    await repository.save_state(running, expected_version=1, issuer=OWNER[0], subject=OWNER[1])
    coordinator = Coordinator()
    service = WorkflowApplicationService(
        cast(Any, repository), authority=_AllowAuthority(),
        agent_run=AgentRunHandler(coordinator),
    )
    with pytest.raises(RuntimeError, match="transient"):
        await service.cancel(
            *OWNER, instance.id, expected_version=2, idempotency_key="cancel-agent"
        )
    pending = await repository.get_state(*OWNER, instance.id)
    assert pending.instance.status.value == "terminal"
    assert pending.instance.terminal_outcome is not None
    intent = await repository.get_cancel_intent(*OWNER, "cancel-agent")
    assert intent is not None and intent.propagated is False
    retried = await service.cancel(
        *OWNER, instance.id, expected_version=2, idempotency_key="cancel-agent"
    )
    assert retried.instance.status.value == "terminal"
    assert coordinator.cancel_calls == 2
    assert len(coordinator.cancel_lineages) == 2
    assert coordinator.cancel_lineages[0] == coordinator.cancel_lineages[1]
    assert coordinator.cancel_lineages[0].workflow_instance_id == instance.id
    assert coordinator.cancel_lineages[0].workflow_step_execution_id == UUID(
        str(running.current.execution_id)
    )
    with pytest.raises(WorkflowIdempotencyConflict):
        await service.cancel(
            *OWNER, instance.id, expected_version=3, idempotency_key="cancel-agent"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "revision_value", "authority_value"),
    (("workspace_id", "workspace-current", "workspace-revoked"),
     ("household_id", "household-current", "household-revoked")),
)
async def test_admission_fails_closed_when_pinned_authority_field_changes(
    field: str, revision_value: str, authority_value: str
) -> None:
    repository = MemoryWorkflowRepository()
    base = _wait_revision()
    revision = replace(base, **{field: revision_value})
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    service = WorkflowApplicationService(
        cast(Any, repository),
        authority=_AllowAuthority(),
        wait_until=WaitUntilHandler(clock=lambda: NOW),
    )
    authority = WorkflowAuthoritySnapshot(
        *OWNER,
        workspace_id=authority_value if field == "workspace_id" else None,
        household_id=authority_value if field == "household_id" else None,
    )
    with pytest.raises(WorkflowTransitionError, match="authority"):
        await service.admit(
            *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"},
            authority=authority, now=NOW,
        )


@pytest.mark.asyncio
async def test_evidence_serializer_failure_does_not_change_workflow_result() -> None:
    class FailingEvidence:
        def serialize(self, **_metadata: object) -> Mapping[str, object]:
            raise RuntimeError("telemetry sink unavailable")

    repository = MemoryWorkflowRepository()
    revision = _wait_revision()
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    service = WorkflowApplicationService(
        cast(Any, repository), authority=_AllowAuthority(),
        wait_until=WaitUntilHandler(clock=lambda: NOW),
        evidence=FailingEvidence(),
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    instance = await service.admit(
        *OWNER, published.id, input_values={"due_at": "2026-01-01T11:59:00+00:00"}, now=NOW,
        authority=authority,
    )
    state = await service.execute_next(
        *OWNER, instance.id, authority=authority
    )
    assert state.instance.terminal_outcome is not None


@pytest.mark.asyncio
async def test_approval_resume_requires_exact_current_step_execution_identity() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Approval resume", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    service = WorkflowApplicationService(cast(Any, repository), authority=_AllowAuthority())
    instance = await service.admit(
        *OWNER, published.id, input_values={}, authority=authority, now=NOW,
    )
    state = await repository.get_state(*OWNER, instance.id)
    started = cast(Any, start_step)(
        state, execution_id="approval-current", expected_version=1, now=NOW
    ).state
    await repository.save_state(started, expected_version=1, issuer=OWNER[0], subject=OWNER[1])

    with pytest.raises(WorkflowTransitionError, match="execution identity"):
        await service.settle_approval(
            *OWNER, instance.id, expected_version=2, step_execution_id="approval-stale",
            decision="approved", decided_at=NOW, authority=authority,
        )


@pytest.mark.asyncio
async def test_workflow_approval_redelivery_acks_after_state_commit() -> None:
    repository = MemoryWorkflowRepository()
    revision = WorkflowRevision(
        uuid4(), uuid4(), *OWNER, "Approval redelivery", (),
        (OwnerApprovalStep(),), status=WorkflowRevisionStatus.PUBLISHED,
    )
    await repository.create_draft(revision, issuer=OWNER[0], subject=OWNER[1])
    published = await repository.publish(
        revision.id, expected_version=1, issuer=OWNER[0], subject=OWNER[1]
    )
    authority = WorkflowAuthoritySnapshot(*OWNER)
    service = AdmissionGatedWorkflowApplication(
        cast(Any, repository), authority=_AllowAuthority(), admission_enabled=True
    )
    instance = await service.admit(
        *OWNER, published.id, input_values={}, authority=authority, now=NOW
    )
    state = await repository.get_state(*OWNER, instance.id)
    execution_id = str(uuid4())
    started = cast(Any, start_step)(
        state, execution_id=execution_id, expected_version=1, now=NOW
    ).state
    await repository.save_state(started, expected_version=1, issuer=OWNER[0], subject=OWNER[1])
    origin = WorkflowApprovalOrigin(
        "manual_workflow_instance", published.workflow_id, published.id, instance.id,
        1, uuid4(),
    )
    origin = replace(origin, workflow_step_execution_id=UUID(execution_id))
    settled = await service.resume_workflow_approval(origin, "approved", NOW)
    redelivery = await service.resume_workflow_approval(origin, "approved", NOW)
    assert redelivery == settled


def test_sql_admission_declares_transactional_wakeup_boundary() -> None:
    """Admission must not rely on an in-process wakeup or provider payload."""

    source = inspect.getsource(SqlWorkflowRepository.admit)
    assert "async with self.sessions.begin()" in source
    assert "instance_id" in source
    assert "WORKFLOW_WAKE_TOPIC" in source or "stage_wakeup" in source
    assert "input_values" not in source.split("return instance", 1)[-1]


def test_sql_management_and_deletion_are_owner_scoped_and_tombstone_preserving() -> None:
    source = inspect.getsource(SqlWorkflowRepository)
    for operation in (
        "create_draft", "publish", "admit", "retire_definition", "retire_revision", "delete",
    ):
        assert f"async def {operation}" in source
    delete_source = inspect.getsource(SqlWorkflowRepository.delete)
    assert "scope" in delete_source
    assert "target_id" in delete_source
    assert "expected_version" in delete_source
    assert "principal_issuer" in source and "principal_subject" in source
    assert "tombstone" in source.casefold()
    assert "input_values" not in delete_source


def test_sql_parent_retirement_checks_live_instances_before_definition_mutation() -> None:
    source = inspect.getsource(SqlWorkflowRepository.retire_definition)
    assert "async with self.sessions.begin()" in source
    assert "WorkflowInstanceRow" in source
    assert "status != WorkflowInstanceStatus.TERMINAL.value" in source
    assert "active workflow instance pins this definition" in source


def test_sql_clone_locks_definition_and_uses_immutable_revision_source() -> None:
    source = inspect.getsource(SqlWorkflowRepository.clone_draft)
    assert "WorkflowDefinitionRow" in source
    assert "definition.version != expected_definition_version" in source
    assert "WorkflowRevisionStatus.PUBLISHED.value" in source
    assert "WorkflowRevisionStatus.RETIRED.value" in source
    assert "with_for_update()" in source


@pytest.mark.asyncio
async def test_run_dto_adapter_preserves_complete_workflow_lineage() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.request: RunAgentRequest | None = None

        async def execute_workflow_agent_run(
            self, request: RunAgentRequest, provider: Any
        ) -> WorkflowAgentRunResult:
            del provider
            self.request = request
            return WorkflowAgentRunResult(
                request.lineage, WorkflowAgentRunOutcome.COMPLETED, "ok", None, "a" * 32,
                NOW, NOW,
            )

        async def cancel_workflow_agent_run(self, _lineage: WorkflowRunLineage) -> bool:
            return True

    coordinator = Coordinator()
    run_id, workflow_id, revision_id, instance_id, step_id = (uuid4() for _ in range(5))
    lineage = WorkflowRunLineage(run_id, workflow_id, revision_id, instance_id, 1, step_id)
    request = RunAgentRequest(
        lineage, uuid4(), uuid4(), "test-provider", "test-model", "instructions", "input",
        NOW, NOW + timedelta(minutes=1), *OWNER,
    )
    adapter = RunCoordinatorWorkflowAdapter(
        cast(Any, coordinator), cast(Any, object()),
        provider_name="test-provider", model_id="test-model",
        model_policy_revision_id=request.model_policy_revision_id,
    )
    result = await adapter.execute_workflow_agent_run(request)
    assert result.lineage == lineage
    assert coordinator.request is request


def test_manifest_dependency_graph_has_no_component_cycle() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    manifests = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in manifest_dir.glob("workflow-*.yaml")
    ]
    components = {item["component"]["id"] for item in manifests}
    graph = {
        item["component"]["id"]: {
            dependency for dependency in item.get("dependencies", ()) if dependency in components
        }
        for item in manifests
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise AssertionError(f"component manifest cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for component in components:
        visit(component)


def test_all_eight_workflow_evaluators_are_declared_and_executable() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    expected = {
        "workflow.definition.v1",
        "workflow.agent-run.v1",
        "workflow.authorization.v1",
        "workflow.notification-step.v1",
        "workflow.wait-step.v1",
        "workflow.checkpoint.v1",
        "workflow.cancellation.v1",
        "workflow.state-machine.v1",
    }
    declared = {
        evaluator
        for path in manifest_dir.glob("workflow-*.yaml")
        for evaluator in (yaml.safe_load(path.read_text(encoding="utf-8")).get("evaluators") or ())
    }
    assert declared == expected
    # Every evaluator must have a stable callable entry point rather than
    # being documentation-only metadata.  The explicit registry is a public
    # boundary for deterministic replay/evaluation tooling.
    from aura_core.domains.automation.workflows import evaluators

    for evaluator_id in sorted(expected):
        result = evaluators.evaluate(evaluator_id, {"scenario": "synthetic"})
        replay = evaluators.evaluate(evaluator_id, {"scenario": "synthetic"})
        assert isinstance(result, dict)
        assert result["evaluatorId"] == evaluator_id
        assert result["passed"] is True
        assert result["outcome"] == "pass"
        assert replay == result


def test_workflow_evaluators_cover_component_specific_race_and_replay_cases() -> None:
    """Every registered component must execute its own closed race algebra."""

    from aura_core.domains.automation.workflows.evaluation import (
        WORKFLOW_EVALUATORS,
        evaluate_workflow_case,
    )

    race_inputs = {
        "workflow.definition.v1": ("published", "conflict"),
        "workflow.agent-run.v1": ("resolution_required", "unknown"),
        "workflow.authorization.v1": ("waiting_for_approval", "unknown"),
        "workflow.notification-step.v1": ("failed_replayable", "definitively_failed"),
        "workflow.wait-step.v1": ("waiting_for_time", "not_attempted"),
        "workflow.checkpoint.v1": ("running", "completed"),
        "workflow.cancellation.v1": ("cancel_requested", "unknown"),
        "workflow.state-machine.v1": ("waiting_for_resolution", "unknown"),
    }
    replay_inputs = {
        "workflow.definition.v1": ("published", "completed"),
        "workflow.agent-run.v1": ("completed", "completed"),
        "workflow.authorization.v1": ("waiting_for_approval", "not_attempted"),
        "workflow.notification-step.v1": ("completed", "completed"),
        "workflow.wait-step.v1": ("completed", "completed"),
        "workflow.checkpoint.v1": ("completed", "completed"),
        "workflow.cancellation.v1": ("terminal", "completed"),
        "workflow.state-machine.v1": ("completed", "completed"),
    }
    for evaluator in WORKFLOW_EVALUATORS:
        cases = (("race", race_inputs[evaluator]), ("replay", replay_inputs[evaluator]))
        for case_id, inputs in cases:
            first = evaluate_workflow_case(
                evaluator, status=inputs[0], outcome=inputs[1], case_id=case_id
            )
            second = evaluate_workflow_case(
                evaluator, status=inputs[0], outcome=inputs[1], case_id=case_id
            )
            assert first == second
            assert first.passed is True
            assert first.cases and all(item["deterministic"] is True for item in first.cases)


def test_public_workflow_evaluator_rejects_tampered_selected_cases() -> None:
    """The public registry must report the supplied state, not fixture metadata."""

    from aura_core.domains.automation.workflows import evaluators
    from aura_core.domains.automation.workflows.evaluation import WORKFLOW_EVALUATORS

    for evaluator in WORKFLOW_EVALUATORS:
        for case_id in ("race", "replay"):
            result = evaluators.evaluate(
                evaluator,
                {"caseId": case_id, "status": "tampered", "outcome": "tampered"},
            )
            assert result["caseId"] == case_id
            assert result["passed"] is False
            assert result["outcome"] == "fail"


def test_workflow_transition_emits_production_state_signal() -> None:
    metrics = MetadataMetrics()
    revision = _wait_revision()
    state = cast(Any, new_state)(
        revision,
        # The state helper is the public transition boundary used by SQL and
        # memory repositories alike.
        admit_instance(revision, now=NOW, input_values={"due_at": NOW.isoformat()}),
    )
    started = cast(Any, start_step)(
        state, execution_id="step-1", expected_version=1, now=NOW, metrics=metrics
    ).state
    assert started.instance.version == 2
    assert any(item.component_id == "aura.automation.workflow_state" for item in metrics.snapshot())
    assert metrics.stats().rejected == 0


def test_each_declared_workflow_component_has_a_production_emission_site() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path(__file__).parents[1] / "src/aura_core/domains/automation/workflows/application.py",
            Path(__file__).parents[1] / "src/aura_core/domains/automation/workflows/handlers.py",
            Path(__file__).parents[1] / "src/aura_core/domains/automation/workflows/state.py",
        )
    )
    components = {
        "aura.automation.workflow_state",
        "aura.automation.workflow_definition",
        "aura.automation.workflow_step.agent_run",
        "aura.automation.workflow_approval",
        "aura.automation.workflow_step.notification",
        "aura.automation.workflow_step.wait_until",
        "aura.automation.workflow_checkpoint",
        "aura.automation.workflow_cancellation",
        "aura.automation.workflow_settlement",
    }
    for component in components:
        assert component in source
