"""Provider-neutral handlers for the four closed workflow step kinds.

Handlers only know about inward public ports.  They never import a provider,
repository adapter, or another domain's persistence implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from aura_core.domains.automation.notifications.public import CreateWorkflowNotification
from aura_core.domains.execution.runs.models import (
    WorkflowAgentRunRequest as RunWorkflowAgentRunRequest,
)
from aura_core.domains.execution.runs.models import (
    WorkflowRunLineage,
)
from aura_core.domains.execution.runs.public import WorkflowAgentRunCoordinatorPort
from aura_core.domains.governance.approvals.public import CreateWorkflowApproval

from .public import (
    AgentRunStep,
    OwnerApprovalStep,
    OwnerNotificationStep,
    WaitUntilStep,
    WorkflowEffectOutcome,
    WorkflowStepExecution,
    WorkflowStepKind,
    WorkflowStepResult,
    WorkflowStepStatus,
    normalise_instant,
)


class WorkflowHandlerError(RuntimeError):
    """Content-free handler failure; private provider detail stays outside Core."""


WorkflowAgentRunPort = WorkflowAgentRunCoordinatorPort


class WorkflowApprovalPort(Protocol):
    async def create_workflow_pending(self, command: CreateWorkflowApproval) -> object: ...


class WorkflowNotificationPort(Protocol):
    async def create_workflow_notification(
        self, command: CreateWorkflowNotification
    ) -> object: ...


def _as_status(value: object, default: WorkflowStepStatus) -> WorkflowStepStatus:
    if isinstance(value, WorkflowStepStatus):
        return value
    if isinstance(value, str):
        try:
            return WorkflowStepStatus(value)
        except ValueError:
            pass
    return default


def _result_from_external(value: object) -> WorkflowStepResult:
    """Normalize a public coordinator result without accepting arbitrary data."""

    if isinstance(value, WorkflowStepResult):
        return value
    status = _as_status(getattr(value, "status", None), WorkflowStepStatus.COMPLETED)
    outcome = getattr(value, "effect_outcome", WorkflowEffectOutcome.COMPLETED)
    if not isinstance(outcome, WorkflowEffectOutcome):
        try:
            outcome = WorkflowEffectOutcome(str(outcome))
        except ValueError as exc:
            raise WorkflowHandlerError("workflow result outcome is unsupported") from exc
    output = getattr(value, "output", None)
    if output is None and isinstance(value, str):
        output = {"status": "completed", "output": value}
    return WorkflowStepResult(status, outcome, output, bool(getattr(value, "replayable", False)))


class AgentRunHandler:
    kind = WorkflowStepKind.AGENT_RUN

    def __init__(
        self, coordinator: WorkflowAgentRunPort, *, provider_name: str = "workflow",
        model_policy_revision_id: UUID | None = None, model_id: str | None = None,
    ) -> None:
        if not callable(getattr(coordinator, "execute_workflow_agent_run", None)):
            raise ValueError("workflow agent-run coordinator port is unavailable")
        self.coordinator = coordinator
        self.provider_name = provider_name
        self.model_policy_revision_id = model_policy_revision_id
        self.model_id = model_id

    async def execute(
        self, execution: WorkflowStepExecution, *, step: AgentRunStep
    ) -> WorkflowStepResult:
        if not isinstance(execution.input_value, str):
            raise WorkflowHandlerError("agent-run input is not bounded text")
        try:
            agent_revision_id = UUID(step.agent_revision_id)
        except ValueError as exc:
            raise WorkflowHandlerError("agent-run lineage identifiers are invalid") from exc
        lineage = self._lineage(execution)
        request = RunWorkflowAgentRunRequest(
            lineage=lineage,
            agent_revision_id=agent_revision_id,
            model_policy_revision_id=self.model_policy_revision_id or agent_revision_id,
            provider=self.provider_name,
            model_id=self.model_id or step.agent_revision_id,
            instructions=step.instructions,
            input_text=execution.input_value,
            admitted_at=execution.admitted_at or execution.deadline - timedelta(minutes=5),
            deadline=execution.deadline,
            issuer=execution.owner[0], subject=execution.owner[1],
            trace_id=(execution.trace_context.trace_id
                      if execution.trace_context and len(execution.trace_context.trace_id) == 32
                      else None),
            parent_span_id=(execution.trace_context.parent_span_id
                            if execution.trace_context and execution.trace_context.parent_span_id
                            and len(execution.trace_context.parent_span_id) == 16 else None),
        )
        result = await self.coordinator.execute_workflow_agent_run(request)
        normalized = _result_from_external(result)
        if normalized.effect_outcome is WorkflowEffectOutcome.COMPLETED:
            output = normalized.output
            if isinstance(output, str):
                output = {"status": "completed", "output": output}
            return WorkflowStepResult(
                normalized.status, normalized.effect_outcome, output, replayable=False
            )
        return WorkflowStepResult(
            normalized.status, normalized.effect_outcome, normalized.output, replayable=False
        )

    @staticmethod
    def _lineage(execution: WorkflowStepExecution) -> WorkflowRunLineage:
        try:
            execution_id = UUID(execution.execution_id)
        except ValueError as exc:
            raise WorkflowHandlerError("agent-run lineage identifiers are invalid") from exc
        return WorkflowRunLineage(
            uuid5(
                NAMESPACE_URL,
                f"aura-workflow-run:{execution.instance_id}:{execution.execution_id}",
            ),
            execution.workflow_id, execution.revision_id, execution.instance_id,
            execution.step_ordinal, execution_id,
        )

    async def cancel(self, execution: WorkflowStepExecution) -> bool:
        return await self.coordinator.cancel_workflow_agent_run(self._lineage(execution))


class ApprovalHandler:
    kind = WorkflowStepKind.OWNER_APPROVAL

    def __init__(self, approvals: WorkflowApprovalPort) -> None:
        if not callable(getattr(approvals, "create_workflow_pending", None)):
            raise ValueError("workflow approval port is unavailable")
        self.approvals = approvals

    async def execute(
        self, execution: WorkflowStepExecution, *, step: OwnerApprovalStep,
        display_name: str, workspace_scope_id: str, next_step_kind: str = "complete",
    ) -> WorkflowStepResult:
        del step
        step_execution_id = UUID(execution.execution_id)
        expires_at = min(execution.deadline, datetime.now(UTC) + timedelta(days=7))
        consequence_by_kind = {
            "agent_run": (
                "continue_to_agent_run",
                "Approve this step to continue to the agent run.",
            ),
            "owner_approval": (
                "continue_to_owner_approval",
                "Approve this step to continue to the next approval.",
            ),
            "owner_notification": (
                "continue_to_owner_notification",
                "Approve this step to continue to the private notification.",
            ),
            "wait_until": (
                "continue_to_wait_until",
                "Approve this step to continue to the wait.",
            ),
            "complete": (
                "complete_workflow",
                "Approve this step to complete the workflow.",
            ),
        }
        consequence, consequence_copy = consequence_by_kind.get(
            next_step_kind, consequence_by_kind["complete"]
        )
        command = CreateWorkflowApproval(
            issuer=execution.owner[0], subject=execution.owner[1],
            workflow_id=execution.workflow_id,
            workflow_revision_id=execution.revision_id,
            workflow_instance_id=execution.instance_id,
            workflow_step_ordinal=execution.step_ordinal,
            workflow_step_execution_id=step_execution_id,
            workflow_display_name=display_name,
            next_step_kind=next_step_kind,
            consequence=consequence,
            consequence_copy=consequence_copy,
            owner_scope_id=execution.owner[1], workspace_scope_id=workspace_scope_id,
            expires_at=expires_at, instance_deadline=execution.deadline,
            idempotency_key=f"workflow:{execution.instance_id}:{execution.execution_id}",
        )
        await self.approvals.create_workflow_pending(command)
        return WorkflowStepResult(
            WorkflowStepStatus.WAITING_FOR_APPROVAL,
            WorkflowEffectOutcome.NOT_ATTEMPTED,
        )


class NotificationHandler:
    kind = WorkflowStepKind.OWNER_NOTIFICATION

    def __init__(self, notifications: WorkflowNotificationPort) -> None:
        if not callable(getattr(notifications, "create_workflow_notification", None)):
            raise ValueError("workflow notification port is unavailable")
        self.notifications = notifications

    async def execute(
        self, execution: WorkflowStepExecution, *, step: OwnerNotificationStep
    ) -> WorkflowStepResult:
        del step
        result_id = uuid5(
            NAMESPACE_URL, f"aura-workflow-result:{execution.instance_id}:{execution.execution_id}"
        )
        notification = await self.notifications.create_workflow_notification(
            CreateWorkflowNotification(
                issuer=execution.owner[0], subject=execution.owner[1],
                workflow_id=execution.workflow_id,
                workflow_revision_id=execution.revision_id,
                workflow_instance_id=execution.instance_id,
                workflow_step_ordinal=execution.step_ordinal,
                workflow_step_execution_id=UUID(execution.execution_id),
                workflow_result_id=result_id,
                idempotency_key=f"workflow:{execution.instance_id}:{execution.execution_id}",
            )
        )
        notification_id = getattr(notification, "id", None)
        if not isinstance(notification_id, UUID):
            raise WorkflowHandlerError("notification port returned no identifier")
        return WorkflowStepResult(
            WorkflowStepStatus.COMPLETED,
            WorkflowEffectOutcome.COMPLETED,
            {"notification_id": notification_id, "status": "inbox_available"},
        )


class WaitUntilHandler:
    kind = WorkflowStepKind.WAIT_UNTIL

    def __init__(self, clock: Any | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self, execution: WorkflowStepExecution, *, step: WaitUntilStep
    ) -> WorkflowStepResult:
        del step
        if not isinstance(execution.input_value, (datetime, str)):
            raise WorkflowHandlerError("wait input is not an instant")
        due_at = normalise_instant(execution.input_value)
        now = normalise_instant(self.clock())
        if due_at > now:
            return WorkflowStepResult(
                WorkflowStepStatus.WAITING_FOR_TIME,
                WorkflowEffectOutcome.NOT_ATTEMPTED,
            )
        return WorkflowStepResult(
            WorkflowStepStatus.COMPLETED,
            WorkflowEffectOutcome.COMPLETED,
            {"resumed_at": now.isoformat()},
        )


__all__ = [
    "WorkflowHandlerError", "WorkflowAgentRunPort", "WorkflowApprovalPort",
    "WorkflowNotificationPort", "AgentRunHandler",
    "ApprovalHandler", "NotificationHandler", "WaitUntilHandler",
]
