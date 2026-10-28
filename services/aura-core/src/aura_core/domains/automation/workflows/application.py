"""Application/coordinator boundary for bounded manual workflow execution."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from .handlers import (
    AgentRunHandler,
    ApprovalHandler,
    NotificationHandler,
    WaitUntilHandler,
)
from .public import (
    AgentRunStep,
    OwnerApprovalStep,
    OwnerNotificationStep,
    Selector,
    WaitUntilStep,
    WorkflowAuthorityPort,
    WorkflowAuthoritySnapshot,
    WorkflowCancelIntent,
    WorkflowCreateDraftCommand,
    WorkflowCreateDraftResult,
    WorkflowDeletionReceipt,
    WorkflowDeletionScope,
    WorkflowEffectOutcome,
    WorkflowEvidenceSerializer,
    WorkflowIdempotencyConflict,
    WorkflowInstance,
    WorkflowInstanceAuthorization,
    WorkflowInstanceStatus,
    WorkflowLifecycleSink,
    WorkflowRepository,
    WorkflowRevision,
    WorkflowStepExecution,
    WorkflowStepHandler,
    WorkflowStepKind,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowValidationError,
    canonical_json,
)
from .state import (
    TransitionReceipt,
    WorkflowState,
    cancel,
    pause_step,
    reconcile,
    request_replay,
    settle_failed,
    settle_step,
    start_step,
)
from .telemetry import observe_duration


def _selector_value(selector: Selector, state: WorkflowState) -> object:
    if selector.source == "instance_input":
        try:
            return state.instance.input_values[selector.field]
        except KeyError as exc:
            raise WorkflowValidationError("workflow input selector is unavailable") from exc
    assert selector.step_ordinal is not None
    previous = state.steps[selector.step_ordinal - 1]
    output = cast(Mapping[str, object], previous.output)
    if not isinstance(previous.output, Mapping) or selector.field not in output:
        raise WorkflowTransitionError("workflow step output is unavailable")
    return output[selector.field]


def _metadata_event(state: WorkflowState, *, event_kind: str) -> dict[str, object]:
    step = state.current
    # A settlement advances the aggregate cursor before the event is
    # serialized.  The closed step variant must identify the completed step,
    # never the newly pending step with a null execution identity.
    if step.status is WorkflowStepStatus.PENDING and step.ordinal > 1:
        step = state.steps[step.ordinal - 2]
    event_id = str(uuid4())
    instance_status_event = event_kind in {"admitted", "canceled"} or (
        state.instance.status is WorkflowInstanceStatus.TERMINAL
    )
    if event_kind == "admitted":
        contract_type = "workflow.instance.admitted"
    elif instance_status_event:
        contract_type = "workflow.instance.status_changed"
    elif event_kind in {"approval_settled", "reconciled"}:
        contract_type = "workflow.step.reconciled"
    elif state.instance.status is WorkflowInstanceStatus.TERMINAL:
        contract_type = "workflow.instance.terminal"
    else:
        contract_type = "workflow.step.status_changed"
    event: dict[str, object] = {
        "schemaVersion": 1,
        "eventId": event_id,
        "eventType": contract_type,
        "workflowId": str(state.instance.workflow_id),
        "revisionId": str(state.instance.revision_id),
        "instanceId": str(state.instance.id),
        "instanceStatus": state.instance.status.value,
        "terminalOutcome": (
            state.instance.terminal_outcome.value
            if state.instance.terminal_outcome is not None else None
        ),
        "currentStepOrdinal": state.instance.current_ordinal,
        "version": state.instance.version,
        "occurredAt": datetime.now(UTC).isoformat(),
    }
    if contract_type.startswith("workflow.step"):
        event.update({
            "stepExecutionId": step.execution_id,
            "stepOrdinal": step.ordinal,
            "stepKind": step.kind.value,
            "stepStatus": step.status.value,
            "effectOutcome": step.effect_outcome.value,
            "recoveryCapabilities": [item.value for item in step.recovery_capabilities],
            "instanceVersion": state.instance.version,
        })
    return event


class WorkflowApplicationService:
    """Shared workflow use cases consumed by API and worker composition."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        authority: WorkflowAuthorityPort | None = None,
        agent_run: AgentRunHandler | None = None,
        approval: ApprovalHandler | None = None,
        notification: NotificationHandler | None = None,
        wait_until: WaitUntilHandler | None = None,
        lifecycle: WorkflowLifecycleSink | None = None,
        evidence: WorkflowEvidenceSerializer | None = None,
        metrics: object | None = None,
    ) -> None:
        self.repository = repository
        self.authority = authority
        self.handlers: dict[WorkflowStepKind, object | None] = {
            WorkflowStepKind.AGENT_RUN: agent_run,
            WorkflowStepKind.OWNER_APPROVAL: approval,
            WorkflowStepKind.OWNER_NOTIFICATION: notification,
            WorkflowStepKind.WAIT_UNTIL: wait_until,
        }
        self.lifecycle = lifecycle
        self.evidence = evidence
        self.metrics = metrics

    async def create_draft(self, revision: WorkflowRevision, *, issuer: str, subject: str,
                           idempotency_key: str | None = None,
                           trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        return await self.repository.create_draft(
            revision, issuer=issuer, subject=subject, idempotency_key=idempotency_key,
            trace_context=trace_context
        )

    async def create_draft_command(
        self, command: WorkflowCreateDraftCommand, *, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowCreateDraftResult:
        return await self.repository.create_draft_command(
            command, idempotency_key=idempotency_key, trace_context=trace_context,
        )

    async def list_definitions(self, issuer: str, subject: str, **kwargs: object) -> object:
        return await self.repository.list_definitions(issuer, subject, **kwargs)  # type: ignore[arg-type]

    async def get_definition(self, issuer: str, subject: str, workflow_id: UUID) -> object:
        return await self.repository.get_definition(issuer, subject, workflow_id)

    async def clone_draft(
        self, issuer: str, subject: str, workflow_id: UUID, **kwargs: object
    ) -> object:
        return await self.repository.clone_draft(issuer, subject, workflow_id, **kwargs)  # type: ignore[arg-type]

    async def update_draft(self, revision: WorkflowRevision, *, expected_version: int,
                           issuer: str, subject: str,
                           idempotency_key: str | None = None) -> WorkflowRevision:
        return await self.repository.update_draft(
            revision, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key,
        )

    async def validate_draft(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int
    ) -> object:
        started = monotonic()
        try:
            return await self.repository.validate_draft(
            issuer, subject, revision_id, expected_version=expected_version
            )
        finally:
            observe_duration(self.metrics, "aura.automation.workflow_definition",
                             "workflow_definition_validation_duration_ms", started,
                             operation="validate", outcome="ok")

    async def list_instances(
        self, issuer: str, subject: str, workflow_id: UUID, **kwargs: object
    ) -> object:
        return await self.repository.list_instances(issuer, subject, workflow_id, **kwargs)  # type: ignore[arg-type]

    async def authorize_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID,
    ) -> WorkflowInstanceAuthorization:
        return await self.repository.authorize_instance_in_workflow(
            issuer, subject, workflow_id, instance_id,
        )

    async def publish(self, revision_id: UUID, *, expected_version: int, issuer: str, subject: str,
                      idempotency_key: str | None = None,
                      trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        return await self.repository.publish(
            revision_id, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key,
            trace_context=trace_context
        )

    async def retire_definition(
        self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
    ) -> object:
        await self.repository.retire_definition(
            issuer, subject, workflow_id, expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        return await self.repository.get_definition(issuer, subject, workflow_id)

    async def retire_revision(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
    ) -> WorkflowRevision:
        return await self.repository.retire_revision(
            issuer, subject, revision_id, expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def delete(self, issuer: str, subject: str, target_id: UUID, **kwargs: object) -> bool:
        return await self.repository.delete(issuer, subject, target_id, **kwargs)  # type: ignore[arg-type]

    async def delete_receipt(
        self, issuer: str, subject: str, workflow_id: UUID, target_id: UUID, *,
        scope: WorkflowDeletionScope, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDeletionReceipt:
        """Return the durable deletion receipt, including replay outcomes.

        This is intentionally a public application seam so transport code can
        resolve an idempotent retry before looking up a target that the first
        deletion removed.  The repository compares the complete semantic
        command fingerprint before returning a receipt.
        """
        return await self.repository.delete_receipt(
            issuer, subject, workflow_id, target_id, scope=scope,
            expected_version=expected_version, idempotency_key=idempotency_key,
            trace_context=trace_context,
        )

    async def admit(
        self, issuer: str, subject: str, revision_id: UUID, *, input_values: Mapping[str, str],
        idempotency_key: str | None = None, authority: WorkflowAuthoritySnapshot | None = None,
        now: datetime | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance:
        revision = await self.repository.get_revision(
            issuer, subject, revision_id, trace_context=trace_context
        )
        authority = await self._derive_authority(
            issuer, subject, revision=revision, supplied=authority
        )
        await self._revalidate(authority, revision=revision, instance=None, ordinal=None)
        result = await self.repository.admit(
            issuer, subject, revision_id, input_values=input_values, now=now,
            idempotency_key=idempotency_key, trace_context=trace_context,
            authority=authority,
        )
        await self._emit(result, event_kind="admitted")
        return result

    async def get(self, issuer: str, subject: str, instance_id: UUID,
                  *, trace_context: WorkflowTraceContext | None = None) -> WorkflowInstance:
        return await self.repository.get_instance(
            issuer, subject, instance_id, trace_context=trace_context
        )

    async def get_state(self, issuer: str, subject: str, instance_id: UUID, *,
                        trace_context: WorkflowTraceContext | None = None) -> WorkflowState:
        """Return the closed aggregate projection for transport composition."""
        return cast(WorkflowState, await self.repository.get_state(
            issuer, subject, instance_id, trace_context=trace_context
        ))

    async def execute_next(
        self, issuer: str, subject: str, instance_id: UUID, *,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        state = cast(WorkflowState, await self.repository.get_state(
            issuer, subject, instance_id, trace_context=trace_context
        ))
        trace_context = trace_context or state.instance.trace_context or WorkflowTraceContext(
            uuid4().hex
        )
        if state.instance.trace_context != trace_context:
            state = WorkflowState(
                replace(state.instance, trace_context=trace_context), state.steps
            )
        revision = await self.repository.get_revision(
            issuer, subject, state.instance.revision_id, trace_context=trace_context
        )
        pinned = state.instance.authority
        if authority is None:
            authority = pinned
        await self._revalidate(
            authority, revision=revision, instance=state.instance,
            ordinal=state.instance.current_ordinal
        )
        if state.instance.status is WorkflowInstanceStatus.TERMINAL:
            return state
        if state.instance.status in {
            WorkflowInstanceStatus.WAITING_FOR_APPROVAL,
            WorkflowInstanceStatus.RESOLUTION_REQUIRED,
            WorkflowInstanceStatus.WAITING_FOR_RESOLUTION,
            WorkflowInstanceStatus.CANCEL_REQUESTED,
            WorkflowInstanceStatus.TERMINAL,
        }:
            return state
        # A running step is a durable crash-recovery point.  Reuse its exact
        # execution identity so an at-least-once wake cannot create a second
        # downstream effect.  Queued steps receive a fresh identity.
        execution_id = (
            state.current.execution_id
            if state.current.status in {
                WorkflowStepStatus.RUNNING, WorkflowStepStatus.WAITING_FOR_TIME,
            }
            else str(uuid4())
        )
        recovering = state.current.status in {
            WorkflowStepStatus.RUNNING, WorkflowStepStatus.WAITING_FOR_TIME,
        }
        if execution_id is None:
            raise WorkflowTransitionError("running workflow step has no execution identity")
        # A persisted RUNNING agent step may have already made an external
        # request before a process crash.  Never auto-replay that effect;
        # transition it to the explicit resolution path instead.
        if (
            state.current.status is WorkflowStepStatus.RUNNING
            and state.current.kind is WorkflowStepKind.AGENT_RUN
        ):
            unresolved = settle_step(
                state, execution_id=execution_id,
                expected_version=state.instance.version,
                status=WorkflowStepStatus.RESOLUTION_REQUIRED,
                effect_outcome=WorkflowEffectOutcome.UNKNOWN,
            )
            saved_unresolved = cast(WorkflowState, await self.repository.save_state(
                unresolved.state, expected_version=state.instance.version,
                issuer=issuer, subject=subject, trace_context=trace_context,
            ))
            await self._emit(saved_unresolved, event_kind="step_failed")
            return saved_unresolved
        started = (
            TransitionReceipt(state, False, True)
            if recovering
            else start_step(
                state, execution_id=execution_id, expected_version=state.instance.version,
            )
        )
        self._transition_telemetry(state, started.state, trace_context, "completed")
        if started.state.instance.status is WorkflowInstanceStatus.TERMINAL:
            started = replace(
                started,
                state=WorkflowState(
                    replace(started.state.instance, next_due_at=None), started.state.steps
                ),
            )
            saved_terminal = cast(WorkflowState, await self.repository.save_state(
                started.state, expected_version=state.instance.version, issuer=issuer,
                subject=subject, trace_context=trace_context
            ))
            await self._emit(saved_terminal, event_kind="step_failed")
            return saved_terminal
        if not recovering:
            checkpoint_started = monotonic()
            state = cast(WorkflowState, await self.repository.save_state(
                started.state, expected_version=started.state.instance.version - 1,
                issuer=issuer, subject=subject, trace_context=trace_context
            ))
            observe_duration(self.metrics, "aura.automation.workflow_checkpoint",
                             "workflow_checkpoint_persistence_duration_ms", checkpoint_started,
                             operation="persist", outcome="ok", trace_context=trace_context)
        else:
            state = started.state
        await self._emit(state, event_kind="step_started")
        handler = self.handlers.get(state.current.kind)
        if handler is None:
            raise WorkflowTransitionError("workflow step handler is unavailable")
        execution = WorkflowStepExecution(
            state.instance.workflow_id, state.instance.revision_id, state.instance.id,
            state.current.ordinal, execution_id, (issuer, subject),
            self._input_for(revision, state),
            {item.ordinal: item.output for item in state.steps if item.output is not None},
            state.instance.deadline, trace_context, state.instance.admitted_at,
        )
        settlement_started = monotonic()
        try:
            result = await self._run_handler(handler, execution, revision, state)
        except Exception:
            failed = settle_step(
                state, execution_id=execution_id,
                expected_version=state.instance.version,
                status=WorkflowStepStatus.RESOLUTION_REQUIRED,
                effect_outcome=WorkflowEffectOutcome.UNKNOWN,
            )
            await self.repository.save_state(
                failed.state, expected_version=state.instance.version,
                issuer=issuer, subject=subject, trace_context=trace_context
            )
            await self._emit(failed.state, event_kind="step_failed")
            raise
        wait_input = self._input_for(revision, state)
        initial_due_wait = False
        if state.current.kind is WorkflowStepKind.WAIT_UNTIL and state.instance.next_due_at is None:
            try:
                initial_due_wait = (
                    datetime.fromisoformat(str(wait_input).replace("Z", "+00:00"))
                    <= datetime.now(UTC)
                )
            except (TypeError, ValueError):
                initial_due_wait = False
        resuming_wait = (
            state.instance.next_due_at is not None
            or initial_due_wait
        ) and state.current.kind is WorkflowStepKind.WAIT_UNTIL
        if result.status in (
            WorkflowStepStatus.WAITING_FOR_APPROVAL,
            WorkflowStepStatus.WAITING_FOR_TIME,
        ):
            receipt = pause_step(
                state, execution_id=execution_id, expected_version=state.instance.version,
                status=result.status,
            )
        else:
            receipt = settle_step(
                state, execution_id=execution_id, expected_version=state.instance.version,
                status=result.status, effect_outcome=result.effect_outcome,
                output=result.output, replayable=result.replayable,
            )
        next_due_at: datetime | None = None
        if result.status is WorkflowStepStatus.WAITING_FOR_TIME:
            candidate = execution.input_value
            if isinstance(candidate, datetime):
                next_due_at = candidate.astimezone(UTC)
            elif isinstance(candidate, str):
                try:
                    next_due_at = datetime.fromisoformat(
                        candidate.replace("Z", "+00:00")
                    ).astimezone(UTC)
                except ValueError:
                    next_due_at = None
        receipt = replace(
            receipt,
            state=WorkflowState(
                replace(receipt.state.instance, next_due_at=next_due_at), receipt.state.steps
            ),
        )
        self._transition_telemetry(state, receipt.state, trace_context, "completed")
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=state.instance.version,
            issuer=issuer, subject=subject, trace_context=trace_context
        ))
        await self._emit(saved, event_kind="step_settled")
        if (resuming_wait and result.status is WorkflowStepStatus.COMPLETED
            and state.current.kind is WorkflowStepKind.WAIT_UNTIL and isinstance(
            execution.input_value, (datetime, str)
        )):
            try:
                due_at = datetime.fromisoformat(
                    str(execution.input_value).replace("Z", "+00:00")
                )
                lag_ms = max(
                    0.0,
                    (datetime.now(UTC) - due_at.astimezone(UTC)).total_seconds() * 1000.0,
                )
                from aura_core.platform.telemetry import MetadataMetrics
                if isinstance(self.metrics, MetadataMetrics):
                    self.metrics.observe("aura.automation.workflow_step.wait_until",
                                         "workflow_wait_lag_ms", lag_ms,
                                         trace_id=(
                                             trace_context.trace_id if trace_context else None
                                         ),
                                         operation="resume", outcome="ok")
                observe_duration(
                    self.metrics, "aura.automation.workflow_step.wait_until",
                    "workflow_wait_command_duration_ms", settlement_started,
                    operation="resume", outcome="completed", trace_context=trace_context,
                )
            except (TypeError, ValueError):
                pass
        observe_duration(self.metrics, "aura.automation.workflow_settlement",
                         "workflow_settlement_duration_ms", settlement_started,
                         operation="settle", outcome="ok", trace_context=trace_context)
        return saved

    async def execute_identifier(self, instance_id: UUID) -> WorkflowState:
        """Process an identifier-only wakeup by reloading owner authority in Core."""

        loader = getattr(self.repository, "get_state_for_worker", None)
        if not callable(loader):
            raise WorkflowTransitionError("workflow worker state boundary is unavailable")
        load = cast(Callable[[UUID], Awaitable[object]], loader)
        state = cast(WorkflowState, await load(instance_id))
        return await self.execute_next(
            state.instance.issuer, state.instance.subject, instance_id,
            authority=state.instance.authority,
        )

    async def recovery_scan(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> tuple[UUID, ...]:
        """Expose the bounded restart/due-wait scan to worker composition."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        candidates = await self.repository.recovery_scan(limit=limit, now=current)
        selected: list[UUID] = []
        for instance_id in candidates:
            # Repository adapters intentionally return only bounded candidate
            # identifiers; authoritative due-time evaluation happens after the
            # aggregate is loaded so future waits are never woken early.
            state = cast(WorkflowState, await self.repository.get_state_for_worker(instance_id))
            if state.instance.status is WorkflowInstanceStatus.RUNNING:
                selected.append(instance_id)
                continue
            if state.instance.status is not WorkflowInstanceStatus.WAITING_FOR_TIME:
                continue
            revision = await self.repository.get_revision(
                state.instance.issuer, state.instance.subject, state.instance.revision_id
            )
            due = self._input_for(revision, state)
            if isinstance(due, datetime):
                due_at = due.astimezone(UTC)
            elif isinstance(due, str):
                try:
                    due_at = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(UTC)
                except ValueError:
                    continue
            else:
                continue
            if due_at <= current:
                selected.append(instance_id)
            if len(selected) >= limit:
                break
        return tuple(selected)

    async def cancel(self, issuer: str, subject: str, instance_id: UUID, *, expected_version: int,
                     idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None) -> WorkflowState:
        started = monotonic()
        semantic = self._semantic_fingerprint("cancel", {
            "instance_id": str(instance_id), "expected_version": expected_version,
            "action": "cancel",
        })
        pending_intent: WorkflowCancelIntent | None = None
        if idempotency_key is not None:
            intent_lookup = getattr(self.repository, "get_cancel_intent", None)
            if callable(intent_lookup):
                typed_intent_lookup = cast(
                    Callable[[str, str, str], Awaitable[object]], intent_lookup
                )
                pending_intent = cast(
                    WorkflowCancelIntent | None,
                    await typed_intent_lookup(issuer, subject, idempotency_key),
                )
        prior = await self._idempotent_state(issuer, subject, idempotency_key, semantic)
        if pending_intent is not None and not pending_intent.propagated:
            pending = prior
            if pending is None:
                pending = cast(
                    WorkflowState,
                    await self.repository.get_state(
                        issuer, subject, pending_intent.instance_id
                    ),
                )
            await self._propagate_cancel(
                pending, issuer=issuer, subject=subject,
                execution_id=pending_intent.execution_id, trace_context=trace_context,
            )
            marker = getattr(self.repository, "mark_cancel_propagated", None)
            if callable(marker):
                assert idempotency_key is not None
                await cast(
                    Callable[[str, str, str], Awaitable[object]], marker
                )(issuer, subject, idempotency_key)
            return pending
        if prior is not None:
            return prior
        state = cast(WorkflowState, await self.repository.get_state(issuer, subject, instance_id))
        if state.instance.version != expected_version:
            raise WorkflowTransitionError("stale workflow instance version")
        # Claim the cancellation under the repository's optimistic lock before
        # invoking the downstream run coordinator.  A concurrent or stale
        # caller therefore fails at the durable boundary and cannot produce a
        # cancellation side effect for a version it did not own.
        receipt = cancel(
            state, expected_version=expected_version,
        )
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key, semantic_fingerprint=semantic,
            cancel_intent=(
                WorkflowCancelIntent(state.instance.id, state.current.execution_id, False)
                if idempotency_key is not None and state.current.execution_id is not None
                else None
            ),
            lifecycle_event_kind="canceled",
            trace_context=trace_context
        ))
        await self._propagate_cancel(
            state, issuer=issuer, subject=subject,
            execution_id=state.current.execution_id,
            trace_context=trace_context,
        )
        if idempotency_key is not None:
            marker = getattr(self.repository, "mark_cancel_propagated", None)
            if callable(marker):
                await cast(
                    Callable[[str, str, str], Awaitable[object]], marker
                )(issuer, subject, idempotency_key)
        await self._emit(saved, event_kind="canceled")
        observe_duration(self.metrics, "aura.automation.workflow_cancellation",
                         "workflow_cancellation_duration_ms", started,
                         operation="cancel", outcome="ok", trace_context=trace_context)
        return saved

    async def _propagate_cancel(
        self, state: WorkflowState, *, issuer: str, subject: str,
        execution_id: str | None, trace_context: WorkflowTraceContext | None,
    ) -> None:
        if (
            state.current.kind is WorkflowStepKind.AGENT_RUN
            and execution_id is not None
            and isinstance(self.handlers.get(WorkflowStepKind.AGENT_RUN), AgentRunHandler)
        ):
            handler = cast(AgentRunHandler, self.handlers[WorkflowStepKind.AGENT_RUN])
            await handler.cancel(WorkflowStepExecution(
                state.instance.workflow_id, state.instance.revision_id, state.instance.id,
                state.current.ordinal, execution_id, (issuer, subject), None, {},
                state.instance.deadline, trace_context, state.instance.admitted_at,
            ))

    async def request_replay(
        self, issuer: str, subject: str, instance_id: UUID, *, expected_version: int,
        step_execution_id: str | None = None,
        idempotency_key: str | None = None,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        semantic = self._semantic_fingerprint("request_replay", {
            "instance_id": str(instance_id), "expected_version": expected_version,
            "step_execution_id": step_execution_id, "action": "request_replay",
        })
        prior = await self._idempotent_state(issuer, subject, idempotency_key, semantic)
        if prior is not None:
            return prior
        state = cast(WorkflowState, await self.repository.get_state(issuer, subject, instance_id))
        revision = await self.repository.get_revision(issuer, subject, state.instance.revision_id)
        await self._revalidate(authority or state.instance.authority, revision=revision,
                               instance=state.instance, ordinal=state.current.ordinal)
        self._check_execution_id(state, step_execution_id)
        receipt = request_replay(
            state, expected_version=expected_version,
        )
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key, semantic_fingerprint=semantic,
            lifecycle_event_kind="replay_requested",
            trace_context=trace_context
        ))
        await self._emit(saved, event_kind="replay_requested")
        return saved

    async def settle_failed(
        self, issuer: str, subject: str, instance_id: UUID, *, expected_version: int,
        step_execution_id: str | None = None,
        idempotency_key: str | None = None,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        semantic = self._semantic_fingerprint("settle_failed", {
            "instance_id": str(instance_id), "expected_version": expected_version,
            "step_execution_id": step_execution_id, "action": "settle_failed",
        })
        prior = await self._idempotent_state(issuer, subject, idempotency_key, semantic)
        if prior is not None:
            return prior
        state = cast(WorkflowState, await self.repository.get_state(issuer, subject, instance_id))
        revision = await self.repository.get_revision(issuer, subject, state.instance.revision_id)
        await self._revalidate(authority or state.instance.authority, revision=revision,
                               instance=state.instance, ordinal=state.current.ordinal)
        self._check_execution_id(state, step_execution_id)
        receipt = settle_failed(
            state, expected_version=expected_version,
        )
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key, semantic_fingerprint=semantic,
            lifecycle_event_kind="failed_settled",
            trace_context=trace_context
        ))
        await self._emit(saved, event_kind="failed_settled")
        return saved

    async def reconcile(
        self, issuer: str, subject: str, instance_id: UUID, *, expected_version: int,
        confirmed: bool, output: object | None = None,
        step_execution_id: str | None = None,
        idempotency_key: str | None = None,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        semantic = self._semantic_fingerprint("reconcile", {
            "instance_id": str(instance_id), "expected_version": expected_version,
            "step_execution_id": step_execution_id, "confirmed": confirmed,
            "output": output, "action": "reconcile",
        })
        prior = await self._idempotent_state(issuer, subject, idempotency_key, semantic)
        if prior is not None:
            return prior
        state = cast(WorkflowState, await self.repository.get_state(issuer, subject, instance_id))
        revision = await self.repository.get_revision(issuer, subject, state.instance.revision_id)
        await self._revalidate(authority or state.instance.authority, revision=revision,
                               instance=state.instance, ordinal=state.current.ordinal)
        self._check_execution_id(state, step_execution_id)
        receipt = reconcile(
            state, expected_version=expected_version, confirmed=confirmed, output=output,
            execution_id=step_execution_id,
        )
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=expected_version, issuer=issuer, subject=subject,
            idempotency_key=idempotency_key, semantic_fingerprint=semantic,
            lifecycle_event_kind="reconciled",
            trace_context=trace_context
        ))
        await self._emit(saved, event_kind="reconciled")
        return saved

    async def settle_approval(
        self, issuer: str, subject: str, instance_id: UUID, *, expected_version: int,
        step_execution_id: str, decision: str, decided_at: datetime,
        idempotency_key: str | None = None,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        """Settle exactly the approval that created the pending command.

        The command is lineage-bound and optimistic: a stale approval cannot
        settle a later step or a newer instance version.
        """
        semantic = self._semantic_fingerprint("settle_approval", {
            "instance_id": str(instance_id), "expected_version": expected_version,
            "step_execution_id": step_execution_id, "decision": decision,
            "decided_at": decided_at.isoformat(), "action": "settle_approval",
        })
        prior = await self._idempotent_state(issuer, subject, idempotency_key, semantic)
        if prior is not None:
            return prior
        state = cast(WorkflowState, await self.repository.get_state(issuer, subject, instance_id))
        revision = await self.repository.get_revision(issuer, subject, state.instance.revision_id)
        await self._revalidate(authority or state.instance.authority, revision=revision,
                                instance=state.instance, ordinal=state.current.ordinal)
        self._check_execution_id(state, step_execution_id)
        if state.current.kind is not WorkflowStepKind.OWNER_APPROVAL:
            raise WorkflowTransitionError("workflow approval lineage is not current")
        if decision not in {"approved", "rejected", "expired"}:
            raise WorkflowValidationError("workflow approval decision is closed")
        status = WorkflowStepStatus.COMPLETED if decision == "approved" else (
            WorkflowStepStatus.REJECTED if decision == "rejected" else WorkflowStepStatus.EXPIRED
        )
        effect = (
            WorkflowEffectOutcome.COMPLETED
            if decision == "approved"
            else WorkflowEffectOutcome.DEFINITIVELY_FAILED
        )
        receipt = settle_step(state, execution_id=step_execution_id,
                              expected_version=expected_version, status=status,
                              effect_outcome=effect,
                              output={"decision": decision, "decided_at": decided_at.isoformat()},
                              )
        saved = cast(WorkflowState, await self.repository.save_state(
            receipt.state, expected_version=expected_version, issuer=issuer,
            subject=subject, idempotency_key=idempotency_key,
            semantic_fingerprint=semantic, lifecycle_event_kind="approval_settled",
            trace_context=trace_context
        ))
        await self._emit(saved, event_kind="approval_settled")
        return saved

    # Explicit application-command spelling used by the approval adapter.
    resume_approval = settle_approval

    @staticmethod
    def _check_execution_id(state: WorkflowState, execution_id: str | None) -> None:
        current = state.current.execution_id
        if current is None or execution_id != current:
            raise WorkflowTransitionError("step execution identity is not current")

    async def _idempotent_state(
        self, issuer: str, subject: str, idempotency_key: str | None,
        semantic_fingerprint: str | None = None,
    ) -> WorkflowState | None:
        if idempotency_key is None:
            return None
        record_lookup = getattr(self.repository, "get_idempotency_record", None)
        if callable(record_lookup) and semantic_fingerprint is not None:
            record = await cast(Callable[..., Awaitable[object | None]], record_lookup)(
                issuer, subject, idempotency_key, operation="save_state"
            )
            if record is not None:
                if getattr(record, "fingerprint", None) != semantic_fingerprint:
                    raise WorkflowIdempotencyConflict("idempotency key was reused")
                response = getattr(record, "response", None)
                return response if isinstance(response, WorkflowState) else None
        lookup = getattr(self.repository, "get_idempotent", None)
        if not callable(lookup):
            return None
        typed_lookup = cast(Callable[..., Awaitable[object | None]], lookup)
        prior = await typed_lookup(issuer, subject, idempotency_key, operation="save_state")
        return prior if isinstance(prior, WorkflowState) else None

    @staticmethod
    def _semantic_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
        return hashlib.sha256(
            canonical_json({"operation": operation, "payload": payload})
        ).hexdigest()

    def _transition_telemetry(
        self, before: WorkflowState, after: WorkflowState,
        trace_context: WorkflowTraceContext | None, outcome: str,
    ) -> None:
        from aura_core.platform.telemetry import MetadataMetrics, new_span_id
        if not isinstance(self.metrics, MetadataMetrics):
            return
        trace_id = trace_context.trace_id if trace_context else uuid4().hex
        self.metrics.increment(
            "aura.automation.workflow_state", "workflow_transition_outcome",
            operation="workflow.transition", outcome=outcome, trace_id=trace_id,
            workflow_id=str(after.instance.workflow_id),
            revision_id=str(after.instance.revision_id),
            instance_id=str(after.instance.id),
        )
        self.metrics.observe(
            "aura.automation.workflow_state", "workflow_transition_duration_ms", 0.0,
            operation="workflow.transition", outcome=outcome, trace_id=trace_id,
            dependency="workflow", status=after.instance.status.value,
        )
        self.metrics.record_span(
            "aura.automation.workflow_state", "workflow.transition", 0.0,
            trace_id=trace_id, span_id=new_span_id(),
            parent_span_id=trace_context.parent_span_id if trace_context else None,
            dependency="workflow", outcome=outcome, error_class=None,
            workflow_id=str(before.instance.workflow_id),
            revision_id=str(before.instance.revision_id),
            instance_id=str(before.instance.id),
        )

    async def _run_handler(self, handler: object, execution: WorkflowStepExecution,
                           revision: WorkflowRevision, state: WorkflowState) -> WorkflowStepResult:
        typed_handler = cast(WorkflowStepHandler, handler)
        method = cast(Any, typed_handler.execute)
        step = revision.steps[state.current.ordinal - 1]
        started = monotonic()
        metric_spec = {
            WorkflowStepKind.AGENT_RUN: (
                "aura.automation.workflow_step.agent_run",
                "workflow_agent_run_step_duration_ms",
            ),
            WorkflowStepKind.OWNER_APPROVAL: (
                "aura.automation.workflow_approval",
                "workflow_approval_command_duration_ms",
            ),
            WorkflowStepKind.OWNER_NOTIFICATION: (
                "aura.automation.workflow_step.notification",
                "workflow_notification_step_duration_ms",
            ),
            WorkflowStepKind.WAIT_UNTIL: (
                "aura.automation.workflow_step.wait_until",
                "workflow_wait_command_duration_ms",
            ),
        }[state.current.kind]
        outcome = "ok"
        try:
            if isinstance(step, AgentRunStep):
                return await method(execution, step=step)
            if isinstance(step, OwnerApprovalStep):
                next_step_kind = (
                    revision.steps[state.current.ordinal].kind.value
                    if state.current.ordinal < len(revision.steps) else "complete"
                )
                return await method(execution, step=step, display_name=revision.display_name,
                                    workspace_scope_id=revision.workspace_id or "default",
                                    next_step_kind=next_step_kind)
            return await method(execution, step=step)
        except Exception:
            outcome = "error"
            raise
        finally:
            observe_duration(self.metrics, metric_spec[0], metric_spec[1], started,
                             operation="execute", outcome=outcome,
                             trace_context=execution.trace_context)

    async def _revalidate(self, authority: WorkflowAuthoritySnapshot | None, *,
                          revision: WorkflowRevision, instance: WorkflowInstance | None,
                          ordinal: int | None) -> None:
        if authority is None:
            raise WorkflowTransitionError("workflow authority snapshot is required")
        if (authority.issuer, authority.subject) != (revision.issuer, revision.subject):
            raise WorkflowTransitionError("workflow authority owner is not current")
        if authority.workspace_id != revision.workspace_id:
            raise WorkflowTransitionError("workflow authority workspace is not current")
        if authority.household_id != revision.household_id:
            raise WorkflowTransitionError("workflow authority household is not current")
        if instance is not None:
            if instance.authority != authority:
                raise WorkflowTransitionError("workflow authority snapshot is not pinned")
            if instance.revision_id != revision.id:
                raise WorkflowTransitionError("workflow revision lineage is not current")
        if self.authority is None:
            raise WorkflowTransitionError("workflow authority port is unavailable")
        if not await self.authority.revalidate(
            authority, revision=revision, instance=instance, ordinal=ordinal
        ):
            raise WorkflowTransitionError("workflow authority was revoked")

    async def _derive_authority(
        self, issuer: str, subject: str, *, revision: WorkflowRevision,
        supplied: WorkflowAuthoritySnapshot | None,
    ) -> WorkflowAuthoritySnapshot:
        """Derive pinned policy facts from the authorized owner boundary."""
        if self.authority is not None:
            derive = getattr(self.authority, "derive", None)
            if callable(derive):
                typed_derive = cast(
                    Callable[..., Awaitable[object]], derive
                )
                result = await typed_derive(issuer, subject, revision=revision)
                if not isinstance(result, WorkflowAuthoritySnapshot):
                    raise WorkflowTransitionError("workflow authority derivation is invalid")
                return result
        if supplied is None:
            raise WorkflowTransitionError("workflow authority snapshot is required")
        return supplied

    @staticmethod
    def _input_for(revision: WorkflowRevision, state: WorkflowState) -> object:
        step = revision.steps[state.current.ordinal - 1]
        selector = step.input if isinstance(step, AgentRunStep) else (
            step.content if isinstance(step, OwnerNotificationStep) else
            step.due_at if isinstance(step, WaitUntilStep) else None
        )
        return _selector_value(selector, state) if selector is not None else None

    async def _emit(self, state: WorkflowState | WorkflowInstance, *, event_kind: str) -> None:
        if self.lifecycle is not None and isinstance(state, WorkflowState):
            await self.lifecycle.publish(_metadata_event(state, event_kind=event_kind))
        if self.evidence is not None:
            instance = state.instance if isinstance(state, WorkflowState) else state
            try:
                phase = (
                    "admission" if event_kind == "admitted" else
                    "cancellation" if event_kind == "canceled" else
                    "approval" if "approval" in event_kind else
                    "settlement" if "settled" in event_kind or "reconciled" in event_kind else
                    "step"
                )
                outcome = (
                    "accepted" if event_kind == "admitted" else
                    "canceled" if event_kind == "canceled" else
                    "completed" if instance.status.value in {"queued", "terminal"} else
                    "unknown"
                )
                self.evidence.serialize(
                    phase=phase, outcome=outcome,
                    workflow_id=instance.workflow_id, revision_id=instance.revision_id,
                    instance_id=instance.id,
                )
            except Exception:
                return


__all__ = ["WorkflowApplicationService"]
