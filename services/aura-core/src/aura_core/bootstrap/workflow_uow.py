"""Composition helpers for the owner-scoped workflow boundary."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.workflows.application import WorkflowApplicationService
from aura_core.domains.automation.workflows.evidence import WorkflowRunEvidenceSerializer
from aura_core.domains.automation.workflows.handlers import (
    AgentRunHandler,
    ApprovalHandler,
    NotificationHandler,
    WaitUntilHandler,
)
from aura_core.domains.automation.workflows.public import (
    WorkflowAuthorityPort,
    WorkflowAuthoritySnapshot,
    WorkflowInstance,
    WorkflowInstanceAuthorization,
    WorkflowIntentPort,
    WorkflowLifecycleIntent,
    WorkflowLifecycleSink,
    WorkflowNotFound,
    WorkflowRepository,
    WorkflowRevision,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowWakeIntent,
)
from aura_core.domains.automation.workflows.repository import (
    MemoryWorkflowRepository,
    SqlWorkflowRepository,
)
from aura_core.domains.automation.workflows.state import WorkflowState
from aura_core.domains.execution.runs.models import WorkflowAgentRunRequest, WorkflowAgentRunResult
from aura_core.domains.execution.runs.ports import RunEvidencePort
from aura_core.domains.execution.runs.public import (
    ChatCompletionPort,
    RunCoordinator,
    RunCoordinatorWorkflowAdapter,
    WorkflowRunLineage,
)
from aura_core.domains.governance.approvals.public import WorkflowApprovalOrigin
from aura_core.domains.interaction.agents.public import AgentCatalog, AgentConfigurationService
from aura_core.platform.outbox import (
    WORKFLOW_WAKE_TOPIC,
    SqlOutboxRepository,
    make_aggregate_command,
)
from aura_core.platform.telemetry import MetadataMetrics


def workflow_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    metrics: MetadataMetrics | None = None,
    intents: WorkflowIntentPort | None = None,
) -> WorkflowRepository:
    """Select the deterministic in-memory or SQL workflow adapter."""
    if testing:
        return cast(WorkflowRepository, MemoryWorkflowRepository(metrics=metrics, intents=intents))
    if sessions is None:
        raise ValueError("database sessions are required for workflow persistence")
    return cast(
        WorkflowRepository,
        SqlWorkflowRepository(sessions, metrics=metrics, intents=intents),
    )


class _WorkflowOutboxIntent(WorkflowIntentPort):
    """Stage identifier-only continuation wakes in the owning SQL transaction."""

    def __init__(self) -> None:
        self._outbox = SqlOutboxRepository()

    async def stage_lifecycle(
        self, intent: WorkflowLifecycleIntent, *, transaction: object | None = None
    ) -> None:
        # Lifecycle rows are already persisted by the workflow repository. The
        # live API replays that authoritative table; no second content-bearing
        # event is sent through NATS.
        del intent, transaction

    async def stage_wakeup(
        self, intent: WorkflowWakeIntent, *, transaction: object | None = None
    ) -> None:
        if transaction is None:
            raise WorkflowCompositionError("workflow wake requires the state transaction")
        command = make_aggregate_command(
            WORKFLOW_WAKE_TOPIC,
            uuid5(
                NAMESPACE_URL,
                f"workflow-wake:{intent.instance_id}:{intent.reason}:{intent.execution_id or ''}",
            ),
            "automation.workflow",
            intent.instance_id,
            identifiers=tuple(
                item for item in (
                    ("instanceId", intent.instance_id),
                    ("executionId", intent.execution_id) if intent.execution_id else None,
                    ("reason", intent.reason),
                ) if item is not None
            ),
        )
        await self._outbox.stage_command(cast(Any, transaction), command=command)


class WorkflowCompositionError(RuntimeError):
    """Raised when a process is missing a required workflow public port."""


class WorkflowAdmissionUnavailable(RuntimeError):
    """Raised by the application boundary while admission is disabled."""


class WorkflowLifecycleCursorNotFound(RuntimeError):
    """The owner supplied a cursor that is not in the retained event window."""


class WorkflowLifecycleCursorExpired(RuntimeError):
    """The owner supplied a cursor older than the bounded replay window."""


class CurrentWorkflowAuthority(WorkflowAuthorityPort):
    """Revalidate authority against current public agent configuration."""

    def __init__(
        self,
        *,
        agents: AgentCatalog | None,
        agent_service: AgentConfigurationService | None,
        workspace_loader: Callable[[str, str, str | None], Awaitable[bool] | bool] | None = None,
    ) -> None:
        self._agents = agents
        self._agent_service = agent_service
        self._workspace_loader = workspace_loader

    async def derive(
        self, issuer: str, subject: str, *, revision: WorkflowRevision
    ) -> WorkflowAuthoritySnapshot:
        """Derive pinned facts from current Core-owned policy sources."""
        if (issuer, subject) != (revision.issuer, revision.subject):
            raise WorkflowTransitionError("workflow authority owner is not current")
        if self._workspace_loader is None:
            raise WorkflowTransitionError("workflow workspace authority is unavailable")
        workspace_ok = self._workspace_loader(issuer, subject, revision.workspace_id)
        if inspect.isawaitable(workspace_ok):
            workspace_ok = await workspace_ok
        if not workspace_ok:
            raise WorkflowTransitionError("workflow workspace authority was revoked")
        agent_revision_id: str | None = None
        grants: list[str] = []
        for step in revision.steps:
            raw_agent_id = getattr(step, "agent_revision_id", None)
            if raw_agent_id is None:
                continue
            if self._agents is None or self._agent_service is None:
                raise WorkflowTransitionError("workflow agent authority is unavailable")
            try:
                resolved_id = UUID(str(raw_agent_id))
                current_revision = self._agents.resolve_revision(resolved_id)
                tools = await self._agent_service.get_tool_policy(
                    issuer, subject, current_revision.tool_policy_revision_id
                )
                policies = await self._agent_service.list_memory_policies(
                    issuer, subject, current_revision.profile_id
                )
            except Exception as exc:
                raise WorkflowTransitionError(
                    "workflow agent authority could not be derived"
                ) from exc
            if current_revision.id != resolved_id or not any(
                item.id == current_revision.memory_policy_revision_id for item in policies
            ) or tools.agent_profile_id != current_revision.profile_id:
                raise WorkflowTransitionError("workflow agent authority was revoked")
            agent_revision_id = str(resolved_id)
            grants.extend((
                str(current_revision.tool_policy_revision_id),
                str(current_revision.memory_policy_revision_id),
            ))
        return WorkflowAuthoritySnapshot(
            issuer=issuer,
            subject=subject,
            household_id=revision.household_id,
            workspace_id=revision.workspace_id,
            agent_revision_id=agent_revision_id,
            grants=tuple(grants),
        )

    async def revalidate(
        self,
        authority: WorkflowAuthoritySnapshot,
        *,
        revision: WorkflowRevision,
        instance: WorkflowInstance | None,
        ordinal: int | None,
    ) -> bool:
        del ordinal
        if (authority.issuer, authority.subject) != (revision.issuer, revision.subject):
            return False
        if instance is not None and instance.revision_id != revision.id:
            return False
        if self._workspace_loader is not None:
            current = self._workspace_loader(
                authority.issuer, authority.subject, revision.workspace_id
            )
            if inspect.isawaitable(current):
                current = await current
            if not current:
                return False
        for step in revision.steps:
            agent_id = getattr(step, "agent_revision_id", None)
            if agent_id is None:
                continue
            try:
                agent_revision_id = UUID(str(agent_id))
            except (TypeError, ValueError):
                return False
            if self._agents is None or self._agent_service is None:
                return False
            try:
                current_revision = self._agents.resolve_revision(agent_revision_id)
                tools = await self._agent_service.get_tool_policy(
                    authority.issuer, authority.subject, current_revision.tool_policy_revision_id
                )
            except Exception:
                return False
            if (
                current_revision.id != agent_revision_id
                or tools.agent_profile_id != current_revision.profile_id
            ):
                return False
            policies = await self._agent_service.list_memory_policies(
                authority.issuer, authority.subject, current_revision.profile_id
            )
            if not any(item.id == current_revision.memory_policy_revision_id for item in policies):
                return False
        return True


async def _current_workspace_membership(
    issuer: str, subject: str, workspace_id: str | None
) -> bool:
    """Fail closed until Core exposes its owner/workspace authority port.

    Workflow revisions currently admit only the default owner scope.  Keeping
    this check as a composition seam prevents a caller-supplied workspace
    value from becoming authority; a future Core workspace source can replace
    this one public adapter without changing workflow application code.
    """
    del issuer, subject
    return workspace_id is None


class WorkflowLifecycleBridge(WorkflowLifecycleSink):
    """Notify live subscribers while replaying repository-owned events only."""

    def __init__(self, repository: WorkflowRepository) -> None:
        self._repository = repository
        self._condition = asyncio.Condition()

    async def publish(self, event: Mapping[str, object]) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def replay(
        self, issuer: str, subject: str, after: UUID | None = None, *, limit: int = 100,
        trace_context: WorkflowTraceContext | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        # Cursor authorization, owner-local sequencing, retention and the
        # bounded page all belong to the persisted workflow repository.  The
        # bridge is deliberately transport-only: it never keeps an in-memory
        # event window and never infers expiry by scanning a page.
        try:
            result = self._repository.replay_events(
                issuer, subject, after=str(after) if after is not None else None, limit=limit,
                trace_context=trace_context,
            )
            if inspect.isawaitable(result):
                result = await result
        except WorkflowNotFound as exc:
            raise WorkflowLifecycleCursorNotFound(
                "workflow lifecycle cursor was not found"
            ) from exc
        except Exception as exc:
            # Domain adapters expose the same semantics without coupling the
            # platform composition layer to their concrete exception class.
            # Only the named lifecycle cursor outcomes are translated; all
            # other failures remain failures rather than becoming a false 404.
            exception_name = type(exc).__name__.casefold()
            if "cursorexpired" in exception_name or "cursor_expired" in exception_name:
                raise WorkflowLifecycleCursorExpired(
                    "workflow lifecycle cursor has expired"
                ) from exc
            if "cursornotfound" in exception_name or "cursor_not_found" in exception_name:
                raise WorkflowLifecycleCursorNotFound(
                    "workflow lifecycle cursor was not found"
                ) from exc
            raise
        page = result
        outcome = getattr(getattr(page, "cursor_outcome", None), "value", None)
        if outcome in {"expired"}:
            raise WorkflowLifecycleCursorExpired("workflow lifecycle cursor has expired")
        if outcome in {"unknown", "foreign"}:
            raise WorkflowLifecycleCursorNotFound("workflow lifecycle cursor was not found")
        events = getattr(page, "events", page)
        return tuple(cast(Mapping[str, object], item) for item in cast(tuple[object, ...], events))

    async def wait(self, timeout: float) -> bool:
        async with self._condition:
            try:
                await asyncio.wait_for(self._condition.wait(), timeout)
            except TimeoutError:
                return False
            return True


class _WorkflowAgentRunAdapter:
    """Bind the public Run Coordinator to the current provider/model policy."""

    def __init__(
        self, coordinator: RunCoordinator, provider: ChatCompletionPort,
        agents: AgentCatalog, *, model_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._provider = provider
        self._agents = agents
        self._model_id = model_id

    async def execute_workflow_agent_run(
        self, request: WorkflowAgentRunRequest, _provider: object | None = None
    ) -> WorkflowAgentRunResult:
        revision = self._agents.resolve_revision(request.agent_revision_id)
        model_id = self._model_id or request.model_id
        adapter = RunCoordinatorWorkflowAdapter(
            self._coordinator, self._provider, provider_name="workflow",
            model_id=model_id, model_policy_revision_id=revision.model_policy_revision_id,
        )
        bound_request = WorkflowAgentRunRequest(
            lineage=request.lineage, agent_revision_id=request.agent_revision_id,
            model_policy_revision_id=revision.model_policy_revision_id, provider="workflow",
            model_id=model_id, instructions=request.instructions, input_text=request.input_text,
            admitted_at=request.admitted_at, deadline=request.deadline,
            issuer=request.issuer, subject=request.subject, trace_id=request.trace_id,
            parent_span_id=request.parent_span_id,
        )
        return await adapter.execute_workflow_agent_run(bound_request)

    async def cancel_workflow_agent_run(self, lineage: WorkflowRunLineage) -> bool:
        return await self._coordinator.cancel_workflow_agent_run(lineage)


class _WorkflowEvidenceAdapter:
    """Create a bounded evidence serializer per workflow instance."""

    def __init__(self, recorder: object) -> None:
        self._recorder = recorder

    def serialize(self, **metadata: object) -> Mapping[str, object]:
        instance_id = cast(UUID, metadata["instance_id"])
        raw_phase = str(metadata.get("phase", "settlement"))
        phase = raw_phase if raw_phase in {
            "admission", "step", "approval", "settlement", "cancellation",
            "definition", "checkpoint", "wait", "notification", "agent_run",
        } else {
            "admitted": "admission", "step_started": "step",
            "step_settled": "settlement", "step_failed": "settlement",
            "canceled": "cancellation", "replay_requested": "settlement",
            "failed_settled": "settlement", "reconciled": "settlement",
            "approval_settled": "approval", "validated": "definition",
        }.get(raw_phase, "settlement")
        outcome = {
            "running": "accepted", "queued": "accepted",
            "waiting_for_approval": "accepted", "waiting_for_time": "accepted",
            "terminal": "completed", "completed": "completed", "canceled": "canceled",
            "cancelled": "canceled", "failed": "failed", "rejected": "rejected",
        }.get(str(metadata.get("outcome")), str(metadata.get("outcome", "unknown")))
        if outcome not in {"accepted", "completed", "failed", "rejected", "unknown", "canceled"}:
            outcome = "unknown"
        serializer = WorkflowRunEvidenceSerializer(
            cast(RunEvidencePort, self._recorder), instance_id
        )
        return serializer.serialize(
            phase=phase, outcome=outcome, workflow_id=metadata["workflow_id"],
            revision_id=metadata["revision_id"], instance_id=instance_id,
            step_ordinal=metadata.get("step_ordinal"), execution_id=metadata.get("execution_id"),
        )


class AdmissionGatedWorkflowApplication(WorkflowApplicationService):
    """Application service with admission policy at the use-case boundary."""

    def __init__(self, *args: Any, admission_enabled: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.admission_enabled = admission_enabled

    def _require_admission(self) -> None:
        if not self.admission_enabled:
            raise WorkflowAdmissionUnavailable("workflow admission is disabled")

    async def publish(self, *args: Any, **kwargs: Any) -> Any:
        self._require_admission()
        return await super().publish(*args, **kwargs)

    async def admit(self, *args: Any, **kwargs: Any) -> Any:
        self._require_admission()
        return await super().admit(*args, **kwargs)

    async def get_revision(self, issuer: str, subject: str, revision_id: UUID) -> object:
        return await self.repository.get_revision(issuer, subject, revision_id)

    async def get_revision_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, revision_id: UUID
    ) -> WorkflowRevision:
        revision = await self.repository.get_revision(issuer, subject, revision_id)
        if revision.workflow_id != workflow_id:
            # Preserve owner-scoped not-found behavior for a child from a
            # different aggregate; callers never learn that it exists.
            from aura_core.domains.automation.workflows.public import WorkflowNotFound
            raise WorkflowNotFound("workflow revision was not found")
        return revision

    async def get_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID
    ) -> WorkflowInstance:
        instance = await self.repository.get_instance(issuer, subject, instance_id)
        if instance.workflow_id != workflow_id:
            from aura_core.domains.automation.workflows.public import WorkflowNotFound
            raise WorkflowNotFound("workflow instance was not found")
        return instance

    async def get_state_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID
    ) -> WorkflowState:
        """Load a full state projection only after parent lineage is checked."""
        instance = await self.get_instance_in_workflow(
            issuer, subject, workflow_id, instance_id
        )
        del instance
        return await self.get_state(issuer, subject, instance_id)

    async def authorize_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID
    ) -> WorkflowInstanceAuthorization:
        """Authorize child lineage through the repository's public projection."""
        return await self.repository.authorize_instance_in_workflow(
            issuer, subject, workflow_id, instance_id
        )

    async def list_instances_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, **kwargs: object
    ) -> object:
        """List children only after the parent aggregate is owner-authorized."""
        await self.repository.get_definition(issuer, subject, workflow_id)
        return await self.list_instances(issuer, subject, workflow_id, **kwargs)

    async def clone_draft_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, *, revision_id: UUID,
        expected_definition_version: int, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        definition = await self.repository.get_definition(issuer, subject, workflow_id)
        if definition.version != expected_definition_version:
            from aura_core.domains.automation.workflows.public import WorkflowVersionConflict
            raise WorkflowVersionConflict("workflow definition version is stale")
        return await self.repository.clone_draft(
            issuer, subject, workflow_id, revision_id=revision_id,
            expected_definition_version=expected_definition_version,
            idempotency_key=idempotency_key,
            trace_context=trace_context,
        )

    async def update_draft_in_workflow(
        self, revision: WorkflowRevision, *, workflow_id: UUID, expected_version: int,
        issuer: str, subject: str, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        if revision.workflow_id != workflow_id:
            from aura_core.domains.automation.workflows.public import WorkflowNotFound
            raise WorkflowNotFound("workflow draft was not found")
        return await self.repository.update_draft(
            revision, expected_version=expected_version, issuer=issuer,
            subject=subject, idempotency_key=idempotency_key,
            trace_context=trace_context,
        )

    async def validate_draft(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> object:
        """Keep the mutation key at the application seam.

        Validation is a read-only deterministic check in the current Core
        repository contract, but the HTTP command still carries its required
        idempotency key.  Accepting it here keeps transport callers on the
        versioned application port without allowing it to affect validation.
        """
        del idempotency_key
        return await self.repository.validate_draft(
            issuer, subject, revision_id, expected_version=expected_version,
            trace_context=trace_context,
        )

    async def retire_definition(
        self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> object:
        await self.repository.retire_definition(
            issuer, subject, workflow_id, expected_version=expected_version,
            idempotency_key=idempotency_key, trace_context=trace_context,
        )
        return await self.repository.get_definition(
            issuer, subject, workflow_id, trace_context=trace_context,
        )

    async def retire_revision(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        return await self.repository.retire_revision(
            issuer, subject, revision_id, expected_version=expected_version,
            idempotency_key=idempotency_key, trace_context=trace_context,
        )

    async def resume_workflow_approval(
        self, origin: object, decision: str, decided_at: datetime | None = None
    ) -> object:
        origin = cast(WorkflowApprovalOrigin, origin)
        instance_id = origin.workflow_instance_id
        state = cast(WorkflowState, await self.repository.get_state_for_worker(instance_id))
        instance = state.instance
        if (
            instance.workflow_id != origin.workflow_id
            or instance.revision_id != origin.workflow_revision_id
        ):
            raise WorkflowCompositionError("workflow approval lineage is not current")
        # Approval delivery is at-least-once.  If the state commit succeeded
        # before the transport acknowledgement, the current aggregate already
        # proves that this origin was consumed: the workflow advanced, or the
        # final approval made the instance terminal while retaining its exact
        # execution identity.  Return the authoritative state so the worker
        # can ACK the redelivery instead of retrying forever.
        if instance.current_ordinal > origin.workflow_step_ordinal or (
            instance.current_ordinal == origin.workflow_step_ordinal
            and getattr(instance.status, "value", instance.status) == "terminal"
            and state.current.execution_id == str(origin.workflow_step_execution_id)
        ):
            return state
        if instance.current_ordinal != origin.workflow_step_ordinal:
            raise WorkflowCompositionError("workflow approval lineage is not current")
        return await self.settle_approval(
            instance.issuer,
            instance.subject,
            instance_id,
            expected_version=instance.version,
            step_execution_id=str(origin.workflow_step_execution_id),
            decision=decision,
            decided_at=decided_at or datetime.now(UTC),
            authority=instance.authority,
        )


@dataclass(frozen=True, slots=True)
class WorkflowComposition:
    repository: WorkflowRepository
    lifecycle: WorkflowLifecycleBridge
    service: AdmissionGatedWorkflowApplication


def compose_workflow(
    sessions: async_sessionmaker[AsyncSession] | None,
    *, testing: bool = False, metrics: MetadataMetrics | None = None,
    coordinator: RunCoordinator, provider: ChatCompletionPort,
    approval_service: object, notification_service: object, agents: AgentCatalog,
    agent_service: AgentConfigurationService, evidence_recorder: object | None = None,
    admission_enabled: bool = False, model_id: str = "",
) -> WorkflowComposition:
    intents = None if testing else _WorkflowOutboxIntent()
    repository = workflow_repository(
        sessions, testing=testing, metrics=metrics, intents=intents
    )
    if not callable(getattr(approval_service, "create_workflow_pending", None)):
        raise WorkflowCompositionError("workflow approval port is required")
    if not callable(getattr(notification_service, "create_workflow_notification", None)):
        raise WorkflowCompositionError("workflow notification port is required")
    if not callable(getattr(coordinator, "execute_workflow_agent_run", None)):
        raise WorkflowCompositionError("workflow run coordinator port is required")
    recovery = getattr(coordinator, "workflow_recovery", None)
    if not testing and (recovery is None or any(
        not callable(getattr(recovery, method, None))
        for method in (
            "reserve_workflow_agent_run",
            "settle_workflow_agent_run",
            "cancel_workflow_agent_run",
        )
    )):
        raise WorkflowCompositionError("durable workflow run recovery port is required")
    lifecycle = WorkflowLifecycleBridge(repository)
    agent_adapter = _WorkflowAgentRunAdapter(coordinator, provider, agents, model_id=model_id)
    evidence = (
        _WorkflowEvidenceAdapter(evidence_recorder)
        if evidence_recorder is not None
        else None
    )
    service = AdmissionGatedWorkflowApplication(
        repository, admission_enabled=admission_enabled,
        authority=CurrentWorkflowAuthority(
            agents=agents, agent_service=agent_service,
            workspace_loader=_current_workspace_membership,
        ),
        agent_run=AgentRunHandler(
            agent_adapter, provider_name="workflow", model_id=model_id or None
        ),
        approval=ApprovalHandler(cast(Any, approval_service)),
        notification=NotificationHandler(cast(Any, notification_service)),
        wait_until=WaitUntilHandler(), lifecycle=lifecycle, evidence=evidence, metrics=metrics,
    )
    return WorkflowComposition(repository, lifecycle, service)


__all__ = [
    "AdmissionGatedWorkflowApplication", "CurrentWorkflowAuthority", "WorkflowComposition",
    "WorkflowCompositionError", "WorkflowAdmissionUnavailable",
    "WorkflowLifecycleCursorExpired", "WorkflowLifecycleCursorNotFound",
    "WorkflowLifecycleBridge",
    "compose_workflow", "workflow_repository",
]
