"""Pure workflow state transitions.

The coordinator calls these functions while holding the persistence row lock.
They do not perform I/O and therefore cannot accidentally make a provider or
executor authoritative for workflow truth.
"""

# ruff: noqa: E501

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from typing import Any, cast

from aura_core.platform.telemetry import MetadataMetrics, new_span_id

from .public import (
    MAX_CHECKPOINT_BYTES,
    MAX_TOTAL_OUTPUT_BYTES,
    RecoveryCapability,
    WorkflowEffectOutcome,
    WorkflowError,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowRevision,
    WorkflowStepKind,
    WorkflowStepState,
    WorkflowStepStatus,
    WorkflowTerminalOutcome,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowValidationError,
    canonical_json,
    normalise_instant,
    validate_revision,
    validate_step_result,
)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return normalise_instant(current)


def _emit_transition(
    metrics: MetadataMetrics | None,
    operation: str,
    state: WorkflowState,
    started: float,
    outcome: str,
    error_class: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> None:
    if metrics is None:
        return
    from time import monotonic

    duration = max(0.0, min((monotonic() - started) * 1000.0, 60_000.0))
    trace = {
        "workflow_id": str(state.instance.workflow_id),
        "revision_id": str(state.instance.revision_id),
        "instance_id": str(state.instance.id),
        "result_version": str(state.instance.version),
    }
    if not trace_id or set(trace_id) == {"0"}:
        trace_id = secrets.token_hex(16)
    span_id = new_span_id()
    metrics.increment(
        "aura.automation.workflow_state",
        "workflow_transition_outcome",
        operation="workflow.transition",
        outcome=outcome,
        trace_id=trace_id,
        **trace,
    )
    metrics.record_span(
        "aura.automation.workflow_state",
        "workflow.transition",
        duration,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        dependency="application_validation",
        outcome=outcome,
        error_class=error_class,
        **trace,
    )


def _instrument_transition(operation: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    from time import monotonic

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(state: WorkflowState, *args: Any, **kwargs: Any) -> TransitionReceipt:
            metrics = cast(MetadataMetrics | None, kwargs.pop("metrics", None))
            trace_context = cast(WorkflowTraceContext | None, kwargs.pop("trace_context", None))
            trace_id = cast(str | None, kwargs.pop("trace_id", None))
            parent_span_id = cast(str | None, kwargs.pop("parent_span_id", None))
            if trace_context is not None:
                trace_id = trace_context.trace_id
                parent_span_id = trace_context.parent_span_id
            started = monotonic()
            try:
                receipt = cast(TransitionReceipt, function(state, *args, **kwargs))
            except WorkflowError as error:
                error_class = "validation" if isinstance(error, WorkflowValidationError) else "conflict"
                _emit_transition(
                    metrics, operation, state, started, "error", error_class,
                    trace_id, parent_span_id,
                )
                raise
            outcome = "replayed" if receipt.replayed else "completed" if receipt.changed else "duplicate"
            _emit_transition(
                metrics, operation, receipt.state, started, outcome,
                trace_id=trace_id, parent_span_id=parent_span_id,
            )
            return receipt

        return wrapped

    return decorator


@dataclass(frozen=True, slots=True)
class WorkflowState:
    instance: WorkflowInstance
    steps: tuple[WorkflowStepState, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise WorkflowValidationError("workflow state requires steps")
        if any(step.instance_id != self.instance.id for step in self.steps):
            raise WorkflowValidationError("step belongs to another instance")
        if tuple(step.ordinal for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise WorkflowValidationError("step ordinals must be contiguous")

    @property
    def current(self) -> WorkflowStepState:
        return self.steps[self.instance.current_ordinal - 1]


@dataclass(frozen=True, slots=True)
class TransitionReceipt:
    state: WorkflowState
    changed: bool
    replayed: bool = False


def new_state(revision: WorkflowRevision, instance: WorkflowInstance) -> WorkflowState:
    revision = validate_revision(revision)
    if instance.revision_id != revision.id:
        raise WorkflowValidationError("instance is not pinned to this revision")
    return WorkflowState(
        instance,
        tuple(
            WorkflowStepState(instance.id, i, step.kind) for i, step in enumerate(revision.steps, 1)
        ),
    )


def start_step(
    state: WorkflowState,
    *,
    execution_id: str,
    expected_version: int,
    now: datetime | None = None,
) -> TransitionReceipt:
    instance = state.instance
    step = state.current
    if instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    if instance.status is WorkflowInstanceStatus.TERMINAL:
        raise WorkflowTransitionError("terminal workflow cannot start a step")
    if step.status is WorkflowStepStatus.RUNNING and step.execution_id == execution_id:
        return TransitionReceipt(state, False, True)
    if step.status is not WorkflowStepStatus.PENDING:
        raise WorkflowTransitionError("current step is not pending")
    if _now(now) >= instance.deadline:
        return terminalize(state, outcome=WorkflowTerminalOutcome.FAILED, now=now)
    updated_step = replace(
        step,
        status=WorkflowStepStatus.RUNNING,
        effect_outcome=WorkflowEffectOutcome.NOT_ATTEMPTED,
        execution_id=execution_id,
        version=step.version + 1,
        recovery_capabilities=(RecoveryCapability.CANCEL,),
    )
    updated_instance = replace(
        instance,
        status=WorkflowInstanceStatus.RUNNING,
        version=instance.version + 1,
    )
    return TransitionReceipt(_replace(state, updated_instance, updated_step), True)


def settle_step(
    state: WorkflowState,
    *,
    execution_id: str,
    expected_version: int,
    status: WorkflowStepStatus,
    effect_outcome: WorkflowEffectOutcome,
    output: object | None = None,
    replayable: bool = False,
    now: datetime | None = None,
) -> TransitionReceipt:
    instance = state.instance
    step = state.current
    if instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    if step.execution_id != execution_id:
        raise WorkflowTransitionError("step execution identity is not current")
    if instance.status is WorkflowInstanceStatus.TERMINAL:
        return TransitionReceipt(state, False, True)
    if step.status not in (WorkflowStepStatus.RUNNING, WorkflowStepStatus.RESOLUTION_REQUIRED):
        raise WorkflowTransitionError("step is not executing")
    result = validate_step_result(step.kind, status, effect_outcome, output)
    _validate_state_budget(state, result, step.ordinal)
    if effect_outcome is WorkflowEffectOutcome.UNKNOWN:
        updated = replace(
            step,
            status=WorkflowStepStatus.RESOLUTION_REQUIRED,
            effect_outcome=effect_outcome,
            output=None,
            version=step.version + 1,
            recovery_capabilities=(RecoveryCapability.RECONCILE, RecoveryCapability.CANCEL),
        )
        return TransitionReceipt(
            _replace(
                state,
                replace(
                    instance,
                    status=WorkflowInstanceStatus.RESOLUTION_REQUIRED,
                    version=instance.version + 1,
                ),
                updated,
            ),
            True,
        )
    if status is WorkflowStepStatus.FAILED_REPLAYABLE:
        if not replayable or step.kind not in {
            WorkflowStepKind.OWNER_APPROVAL,
            WorkflowStepKind.OWNER_NOTIFICATION,
            WorkflowStepKind.WAIT_UNTIL,
        }:
            raise WorkflowTransitionError("this step kind is not replayable")
        updated = replace(
            step,
            status=status,
            effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
            output=None,
            version=step.version + 1,
            recovery_capabilities=(
                RecoveryCapability.REQUEST_REPLAY,
                RecoveryCapability.SETTLE_FAILED,
                RecoveryCapability.CANCEL,
            ),
        )
        return TransitionReceipt(
            _replace(
                state,
                replace(
                    instance,
                    status=WorkflowInstanceStatus.WAITING_FOR_RESOLUTION,
                    version=instance.version + 1,
                ),
                updated,
            ),
            True,
        )
    updated = replace(
        step,
        status=status,
        effect_outcome=effect_outcome,
        output=result,
        version=step.version + 1,
        recovery_capabilities=(),
    )
    if status is WorkflowStepStatus.COMPLETED:
        if instance.current_ordinal == len(state.steps):
            updated_instance = replace(
                instance,
                status=WorkflowInstanceStatus.TERMINAL,
                terminal_outcome=WorkflowTerminalOutcome.COMPLETED,
                version=instance.version + 1,
            )
        else:
            updated_instance = replace(
                instance,
                current_ordinal=instance.current_ordinal + 1,
                status=WorkflowInstanceStatus.QUEUED,
                version=instance.version + 1,
            )
    else:
        updated_instance = replace(
            instance,
            status=WorkflowInstanceStatus.TERMINAL,
            terminal_outcome=WorkflowTerminalOutcome.FAILED,
            version=instance.version + 1,
        )
    return TransitionReceipt(_replace(state, updated_instance, updated), True)


def request_replay(
    state: WorkflowState, *, expected_version: int, now: datetime | None = None
) -> TransitionReceipt:
    instance, step = state.instance, state.current
    if instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    if instance.status is WorkflowInstanceStatus.TERMINAL:
        raise WorkflowTransitionError("terminal workflow cannot replay")
    if step.status is not WorkflowStepStatus.FAILED_REPLAYABLE:
        raise WorkflowTransitionError("only replayable failures may be replayed")
    if step.kind not in {
        WorkflowStepKind.OWNER_APPROVAL,
        WorkflowStepKind.OWNER_NOTIFICATION,
        WorkflowStepKind.WAIT_UNTIL,
    }:
        raise WorkflowTransitionError("this step kind is not replayable")
    if instance.replay_count >= 1:
        raise WorkflowTransitionError("only one replay is permitted")
    if _now(now) >= instance.deadline:
        return terminalize(state, outcome=WorkflowTerminalOutcome.FAILED, now=now)
    updated_step = replace(
        step,
        status=WorkflowStepStatus.PENDING,
        effect_outcome=WorkflowEffectOutcome.NOT_ATTEMPTED,
        recovery_capabilities=(),
        version=step.version + 1,
    )
    updated_instance = replace(
        instance,
        status=WorkflowInstanceStatus.QUEUED,
        replay_count=instance.replay_count + 1,
        version=instance.version + 1,
    )
    return TransitionReceipt(_replace(state, updated_instance, updated_step), True)


def settle_failed(
    state: WorkflowState, *, expected_version: int, now: datetime | None = None
) -> TransitionReceipt:
    if state.instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    if state.current.status is not WorkflowStepStatus.FAILED_REPLAYABLE:
        raise WorkflowTransitionError("current step is not awaiting failure settlement")
    return terminalize(state, outcome=WorkflowTerminalOutcome.FAILED, now=now)


def reconcile(
    state: WorkflowState,
    *,
    expected_version: int,
    confirmed: bool,
    output: object | None = None,
    now: datetime | None = None,
) -> TransitionReceipt:
    if state.instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    step = state.current
    if step.status is not WorkflowStepStatus.RESOLUTION_REQUIRED:
        raise WorkflowTransitionError("current step is not unresolved")
    if confirmed:
        return settle_step(
            state,
            execution_id=step.execution_id or "reconciled",
            expected_version=expected_version,
            status=WorkflowStepStatus.COMPLETED,
            effect_outcome=WorkflowEffectOutcome.COMPLETED,
            output=output,
            now=now,
        )
    return settle_step(
        state,
        execution_id=step.execution_id or "reconciled",
        expected_version=expected_version,
        status=WorkflowStepStatus.FAILED,
        effect_outcome=WorkflowEffectOutcome.DEFINITIVELY_FAILED,
        output=output,
        now=now,
    )


def cancel(
    state: WorkflowState, *, expected_version: int, now: datetime | None = None
) -> TransitionReceipt:
    if state.instance.version != expected_version:
        raise WorkflowTransitionError("stale workflow instance version")
    if state.instance.status is WorkflowInstanceStatus.TERMINAL:
        return TransitionReceipt(state, False, True)
    return terminalize(state, outcome=WorkflowTerminalOutcome.CANCELED, now=now)


def terminalize(
    state: WorkflowState, *, outcome: WorkflowTerminalOutcome, now: datetime | None = None
) -> TransitionReceipt:
    if state.instance.status is WorkflowInstanceStatus.TERMINAL:
        return TransitionReceipt(state, False, True)
    updated_step = state.current
    if outcome is WorkflowTerminalOutcome.CANCELED and updated_step.status in (
        WorkflowStepStatus.RUNNING,
        WorkflowStepStatus.RESOLUTION_REQUIRED,
    ):
        updated_step = replace(
            updated_step,
            status=WorkflowStepStatus.CANCELED,
            version=updated_step.version + 1,
            recovery_capabilities=(),
        )
    updated_instance = replace(
        state.instance,
        status=WorkflowInstanceStatus.TERMINAL,
        terminal_outcome=outcome,
        version=state.instance.version + 1,
    )
    return TransitionReceipt(_replace(state, updated_instance, updated_step), True)


def checkpoint_payload(state: WorkflowState) -> dict[str, object]:
    payload: dict[str, object] = {
        "instance_id": str(state.instance.id),
        "revision_id": str(state.instance.revision_id),
        "current_ordinal": state.instance.current_ordinal,
        "instance_status": state.instance.status.value,
        "instance_version": state.instance.version,
        "step_status": state.current.status.value,
        "step_version": state.current.version,
        "deadline": state.instance.deadline.isoformat(),
        "execution_id": state.current.execution_id,
    }
    if len(canonical_json(payload)) > MAX_CHECKPOINT_BYTES:
        raise WorkflowValidationError("checkpoint exceeds 8 KiB")
    return payload


def _replace(
    state: WorkflowState, instance: WorkflowInstance, step: WorkflowStepState
) -> WorkflowState:
    values = list(state.steps)
    values[step.ordinal - 1] = step
    return WorkflowState(instance, tuple(values))


def _validate_state_budget(
    state: WorkflowState, candidate: object | None, candidate_ordinal: int
) -> None:
    """Apply the aggregate private-value budget at the lock boundary."""

    total = len(canonical_json(dict(state.instance.input_values)))
    for step in state.steps:
        value = candidate if step.ordinal == candidate_ordinal else step.output
        if value is not None:
            total += len(canonical_json(value))
    if total > MAX_TOTAL_OUTPUT_BYTES:
        raise WorkflowValidationError("workflow input and outputs exceed 64 KiB")


def validate_state_budget(state: WorkflowState) -> None:
    _validate_state_budget(state, None, -1)


# Keep the transition functions pure by making telemetry an optional boundary
# concern.  The wrapper consumes ``metrics`` and never exposes private values.
start_step = cast(Any, _instrument_transition("start_step")(start_step))
settle_step = cast(Any, _instrument_transition("settle_step")(settle_step))
request_replay = cast(Any, _instrument_transition("request_replay")(request_replay))
settle_failed = cast(Any, _instrument_transition("settle_failed")(settle_failed))
reconcile = cast(Any, _instrument_transition("reconcile")(reconcile))
cancel = cast(Any, _instrument_transition("cancel")(cancel))
