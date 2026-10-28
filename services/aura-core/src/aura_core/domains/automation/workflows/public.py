"""Public workflow value objects and deterministic validation.

This module intentionally contains no executor, provider, scheduler, or HTTP
code.  A workflow revision is a closed data structure: the only values that
can cross a step edge are the small set of typed selectors defined here.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID, uuid4

WORKFLOW_COMPONENT = "aura.automation.workflow_state"
MAX_STEPS = 8
MAX_WORKFLOW_NAME_BYTES = 512
MAX_WORKFLOW_NAME_SCALARS = 160
MAX_TEXT_BYTES = 8 * 1024
MAX_INSTRUCTIONS_BYTES = 8 * 1024
MAX_INSTANCE_INPUT_BYTES = 32 * 1024
MAX_STEP_VALUE_BYTES = 32 * 1024
MAX_TOTAL_OUTPUT_BYTES = 64 * 1024
MAX_CHECKPOINT_BYTES = 8 * 1024
MAX_INSTANCE_LIFETIME_DAYS = 30
MAX_APPROVAL_DAYS = 7
MAX_AGENT_DEADLINE_SECONDS = 5 * 60
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class WorkflowError(RuntimeError):
    """Base class for deterministic workflow errors."""


class WorkflowValidationError(WorkflowError, ValueError):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowVersionConflict(WorkflowError):
    pass


class WorkflowIdempotencyConflict(WorkflowError):
    pass


class WorkflowTransitionError(WorkflowError):
    pass


class WorkflowStepKind(StrEnum):
    AGENT_RUN = "agent_run"
    OWNER_APPROVAL = "owner_approval"
    OWNER_NOTIFICATION = "owner_notification"
    WAIT_UNTIL = "wait_until"


class WorkflowValueType(StrEnum):
    BOUNDED_TEXT = "bounded_text"
    INSTANT = "instant"


class WorkflowRevisionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class WorkflowInstanceStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_TIME = "waiting_for_time"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_RESOLUTION = "waiting_for_resolution"
    RESOLUTION_REQUIRED = "resolution_required"
    CANCEL_REQUESTED = "cancel_requested"
    TERMINAL = "terminal"


class WorkflowTerminalOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_TIME = "waiting_for_time"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    FAILED_REPLAYABLE = "failed_replayable"
    RESOLUTION_REQUIRED = "resolution_required"
    COMPLETED = "completed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELED = "canceled"
    FAILED = "failed"


class WorkflowEffectOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    COMPLETED = "completed"
    DEFINITIVELY_FAILED = "definitively_failed"
    UNKNOWN = "unknown"


class RecoveryCapability(StrEnum):
    REQUEST_REPLAY = "request_replay"
    SETTLE_FAILED = "settle_failed"
    RECONCILE = "reconcile"
    CANCEL = "cancel"


class WorkflowDeletionScope(StrEnum):
    DRAFT = "draft"
    RETIRED_DEFINITION = "retired_definition"
    PUBLISHED_REVISION = "published_revision"
    TERMINAL_INSTANCE = "terminal_instance"
    INSTANCE_HISTORY = "instance_history"
    INSTANCE_RESULT = "instance_result"
    INSTANCE_CHECKPOINTS = "instance_checkpoints"


class WorkflowEventCursorOutcome(StrEnum):
    """Closed result for an opaque owner-local lifecycle cursor."""

    OK = "ok"
    UNKNOWN = "unknown"
    FOREIGN = "foreign"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class WorkflowTraceContext:
    """Explicit request-scoped trace parent for workflow commands and children."""

    trace_id: str
    parent_span_id: str | None = None


class WorkflowCommand(StrEnum):
    ADMIT = "admit"
    START_STEP = "start_step"
    SETTLE_STEP = "settle_step"
    REQUEST_REPLAY = "request_replay"
    SETTLE_FAILED = "settle_failed"
    RECONCILE = "reconcile"
    CANCEL = "cancel"


def canonical_json(value: object) -> bytes:
    """Return the deterministic JSON representation used for byte limits."""

    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError("value is not canonical JSON") from exc


def normalise_text(value: str, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise WorkflowValidationError("text must be a string")
    result = unicodedata.normalize("NFC", value)
    if len(result.encode("utf-8")) > max_bytes:
        raise WorkflowValidationError("text exceeds its UTF-8 byte limit")
    return result


def normalise_instant(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkflowValidationError("instant must be RFC 3339") from exc
    if value.tzinfo is None:
        raise WorkflowValidationError("instant must have an offset")
    return value.astimezone(UTC)


def validate_opaque_identifier(value: str | UUID) -> str:
    result = str(value)
    if not IDENTIFIER_RE.fullmatch(result):
        raise WorkflowValidationError("opaque identifier is invalid")
    return result


@dataclass(frozen=True, slots=True)
class Selector:
    source: str
    field: str
    step_ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.source not in {"instance_input", "step_output"}:
            raise WorkflowValidationError("selector source is closed")
        if not FIELD_RE.fullmatch(self.field):
            raise WorkflowValidationError("selector field is invalid")
        if self.source == "instance_input" and self.step_ordinal is not None:
            raise WorkflowValidationError("instance selector cannot have a step ordinal")
        if self.source == "step_output" and (self.step_ordinal is None or self.step_ordinal < 1):
            raise WorkflowValidationError("step selector requires a positive ordinal")

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"source": self.source, "field": self.field}
        if self.step_ordinal is not None:
            value["step_ordinal"] = self.step_ordinal
        return value


@dataclass(frozen=True, slots=True)
class InputField:
    name: str
    value_type: WorkflowValueType

    def __post_init__(self) -> None:
        if not FIELD_RE.fullmatch(self.name):
            raise WorkflowValidationError("input field name is invalid")
        if self.value_type not in (WorkflowValueType.BOUNDED_TEXT, WorkflowValueType.INSTANT):
            raise WorkflowValidationError("input field type is closed")


@dataclass(frozen=True, slots=True)
class AgentRunStep:
    agent_revision_id: str
    instructions: str
    input: Selector
    kind: WorkflowStepKind = WorkflowStepKind.AGENT_RUN

    def __post_init__(self) -> None:
        validate_opaque_identifier(self.agent_revision_id)
        normalise_text(self.instructions, max_bytes=MAX_INSTRUCTIONS_BYTES)
        if type(self.input) is not Selector:
            raise WorkflowValidationError("agent input must be a selector")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "agent_revision_id": self.agent_revision_id,
            "instructions": self.instructions,
            "input": self.input.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class OwnerApprovalStep:
    kind: WorkflowStepKind = WorkflowStepKind.OWNER_APPROVAL

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class OwnerNotificationStep:
    content: Selector
    kind: WorkflowStepKind = WorkflowStepKind.OWNER_NOTIFICATION

    def __post_init__(self) -> None:
        if type(self.content) is not Selector:
            raise WorkflowValidationError("notification content must be a selector")

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "content": self.content.as_dict()}


@dataclass(frozen=True, slots=True)
class WaitUntilStep:
    due_at: Selector
    kind: WorkflowStepKind = WorkflowStepKind.WAIT_UNTIL

    def __post_init__(self) -> None:
        if type(self.due_at) is not Selector:
            raise WorkflowValidationError("wait due_at must be a selector")

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "due_at": self.due_at.as_dict()}


type WorkflowStep = AgentRunStep | OwnerApprovalStep | OwnerNotificationStep | WaitUntilStep


def step_output_type(step: WorkflowStep) -> WorkflowValueType | None:
    if isinstance(step, (AgentRunStep, OwnerNotificationStep, OwnerApprovalStep)):
        return WorkflowValueType.BOUNDED_TEXT
    return WorkflowValueType.INSTANT


@dataclass(frozen=True, slots=True)
class WorkflowRevision:
    id: UUID
    workflow_id: UUID
    issuer: str
    subject: str
    display_name: str
    input_fields: tuple[InputField, ...]
    steps: tuple[WorkflowStep, ...]
    version: int = 1
    status: WorkflowRevisionStatus = WorkflowRevisionStatus.DRAFT
    household_id: str | None = None
    workspace_id: str | None = None
    revision_number: int = 1
    published_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        name = normalise_text(self.display_name, max_bytes=MAX_WORKFLOW_NAME_BYTES)
        if len(name) > MAX_WORKFLOW_NAME_SCALARS:
            raise WorkflowValidationError("workflow display name exceeds scalar limit")
        if not self.issuer or not self.subject:
            raise WorkflowValidationError("workflow owner is required")
        if not 1 <= len(self.steps) <= MAX_STEPS:
            raise WorkflowValidationError("workflow must contain one through eight steps")
        if len(self.input_fields) > 16 or len({field.name for field in self.input_fields}) != len(
            self.input_fields
        ):
            raise WorkflowValidationError("workflow input fields are not closed")
        if self.version <= 0:
            raise WorkflowValidationError("workflow version must be positive")
        if self.revision_number <= 0:
            raise WorkflowValidationError("workflow revision number must be positive")
        agent_count = sum(isinstance(step, AgentRunStep) for step in self.steps)
        if agent_count > 1:
            raise WorkflowValidationError("workflow permits at most one agent run")
        for ordinal, step in enumerate(self.steps, start=1):
            _validate_step_selector(step, ordinal, self.input_fields)
        _validate_cross_step_selectors(self.steps)
        if len(canonical_json(self.as_dict())) > 64 * 1024:
            raise WorkflowValidationError("workflow revision exceeds 64 KiB")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "workflow_id": str(self.workflow_id),
            "issuer": self.issuer,
            "subject": self.subject,
            "display_name": self.display_name,
            "input_fields": [
                {"name": item.name, "type": item.value_type.value} for item in self.input_fields
            ],
            "steps": [step.as_dict() for step in self.steps],
            "version": self.version,
            "status": self.status.value,
            # The transport contract calls this immutable sequence ``revision``.
            "revision": self.revision_number,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retired_at": self.retired_at.isoformat() if self.retired_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "limits": {
                "max_steps": MAX_STEPS,
                "max_instance_lifetime_days": MAX_INSTANCE_LIFETIME_DAYS,
                "max_agent_deadline_seconds": MAX_AGENT_DEADLINE_SECONDS,
            },
        }


@dataclass(frozen=True, slots=True)
class WorkflowCreateDraftCommand:
    """Semantic draft-create input; server-side IDs are never caller-owned."""

    issuer: str
    subject: str
    display_name: str
    input_fields: tuple[InputField, ...]
    steps: tuple[WorkflowStep, ...]
    household_id: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowCreateDraftResult:
    revision: WorkflowRevision
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"revision": self.revision.as_dict(), "replayed": self.replayed}


def _output_field(step: WorkflowStep) -> str:
    if isinstance(step, AgentRunStep):
        return "output"
    if isinstance(step, OwnerApprovalStep):
        return "decision"
    if isinstance(step, OwnerNotificationStep):
        return "notification_id"
    if step.kind is WorkflowStepKind.WAIT_UNTIL:
        return "resumed_at"
    raise WorkflowValidationError("unknown step kind")


def _output_field_type(step: WorkflowStep) -> WorkflowValueType:
    if isinstance(step, (AgentRunStep, OwnerApprovalStep)):
        return WorkflowValueType.BOUNDED_TEXT
    if step.kind is WorkflowStepKind.WAIT_UNTIL:
        return WorkflowValueType.INSTANT
    raise WorkflowValidationError("notification receipt is not a mappable value")


def validate_step_result(
    kind: WorkflowStepKind,
    status: WorkflowStepStatus,
    effect_outcome: WorkflowEffectOutcome,
    output: object | None,
) -> object | None:
    """Validate the closed result algebra before it enters durable state.

    Results are intentionally kind-specific.  This prevents an executor from
    smuggling arbitrary control fields or a result belonging to another step
    kind into the workflow record.
    """

    if effect_outcome is WorkflowEffectOutcome.UNKNOWN:
        if output is not None:
            raise WorkflowValidationError("unknown effects cannot carry a result")
        if status is not WorkflowStepStatus.RESOLUTION_REQUIRED:
            raise WorkflowTransitionError("unknown effects require resolution_required status")
        return None
    if status is WorkflowStepStatus.FAILED_REPLAYABLE:
        if effect_outcome is not WorkflowEffectOutcome.DEFINITIVELY_FAILED or output is not None:
            raise WorkflowTransitionError("replayable failures have no result")
        return None
    if effect_outcome is WorkflowEffectOutcome.COMPLETED:
        if status is not WorkflowStepStatus.COMPLETED:
            raise WorkflowTransitionError("completed effects require completed status")
        if not isinstance(output, Mapping):
            raise WorkflowValidationError("completed result must be an object")
        raw_output = cast(Mapping[object, object], output)
        if any(type(key) is not str for key in raw_output):
            raise WorkflowValidationError("result keys must be strings")
        result = dict(cast(Mapping[str, object], raw_output))
        if kind is WorkflowStepKind.AGENT_RUN:
            if set(result) != {"status", "output"} or result.get("status") != "completed":
                raise WorkflowValidationError("agent result algebra is closed")
            if not isinstance(result["output"], str):
                raise WorkflowValidationError("agent output must be text")
            result["output"] = normalise_text(result["output"])
        elif kind is WorkflowStepKind.OWNER_APPROVAL:
            if set(result) != {"decision", "decided_at"}:
                raise WorkflowValidationError("approval result algebra is closed")
            decision = result.get("decision")
            if decision not in {"approved", "rejected", "expired"}:
                raise WorkflowValidationError("approval decision is closed")
            decided_at = result["decided_at"]
            if not isinstance(decided_at, (str, datetime)):
                raise WorkflowValidationError("approval timestamp is invalid")
            result["decided_at"] = normalise_instant(decided_at).isoformat()
        elif kind is WorkflowStepKind.OWNER_NOTIFICATION:
            if set(result) != {"notification_id", "status"} or result.get("status") != "inbox_available":
                raise WorkflowValidationError("notification result algebra is closed")
            notification_id = result["notification_id"]
            if not isinstance(notification_id, (str, UUID)):
                raise WorkflowValidationError("notification identifier is invalid")
            result["notification_id"] = validate_opaque_identifier(notification_id)
        elif kind is WorkflowStepKind.WAIT_UNTIL:
            if set(result) != {"resumed_at"}:
                raise WorkflowValidationError("wait result algebra is closed")
            resumed_at = result["resumed_at"]
            if not isinstance(resumed_at, (str, datetime)):
                raise WorkflowValidationError("resume timestamp is invalid")
            result["resumed_at"] = normalise_instant(resumed_at).isoformat()
        else:
            raise WorkflowValidationError("step kind is closed")
        if len(canonical_json(result)) > MAX_STEP_VALUE_BYTES:
            raise WorkflowValidationError("step result exceeds 32 KiB")
        return result
    if effect_outcome is WorkflowEffectOutcome.DEFINITIVELY_FAILED:
        if status not in {
            WorkflowStepStatus.FAILED,
            WorkflowStepStatus.REJECTED,
            WorkflowStepStatus.EXPIRED,
        }:
            raise WorkflowTransitionError("failed effects require a terminal failure status")
        if output is not None:
            if kind is not WorkflowStepKind.OWNER_APPROVAL or not isinstance(output, Mapping):
                raise WorkflowValidationError("failed result cannot carry output")
            raw = cast(Mapping[object, object], output)
            if set(raw) != {"decision", "decided_at"}:
                raise WorkflowValidationError("approval failure result algebra is closed")
            decision = raw.get("decision")
            expected = "rejected" if status is WorkflowStepStatus.REJECTED else "expired"
            if decision != expected:
                raise WorkflowValidationError("approval decision does not match terminal status")
            timestamp = raw.get("decided_at")
            if not isinstance(timestamp, (str, datetime)):
                raise WorkflowValidationError("approval timestamp is invalid")
            result = {"decision": decision, "decided_at": normalise_instant(timestamp).isoformat()}
            if len(canonical_json(result)) > MAX_STEP_VALUE_BYTES:
                raise WorkflowValidationError("step result exceeds 32 KiB")
            return result
        return None
    if effect_outcome is WorkflowEffectOutcome.NOT_ATTEMPTED:
        raise WorkflowTransitionError("not_attempted cannot settle a step")
    raise WorkflowTransitionError("effect outcome is closed")


def _validate_step_selector(
    step: WorkflowStep, ordinal: int, fields: tuple[InputField, ...]
) -> None:
    if isinstance(step, AgentRunStep):
        slot = step.input
        expected = WorkflowValueType.BOUNDED_TEXT
    elif isinstance(step, OwnerNotificationStep):
        slot = step.content
        expected = WorkflowValueType.BOUNDED_TEXT
    elif isinstance(step, WaitUntilStep):
        slot = step.due_at
        expected = WorkflowValueType.INSTANT
    else:
        return
    if slot.source == "instance_input":
        actual = next((item.value_type for item in fields if item.name == slot.field), None)
        if actual != expected:
            raise WorkflowValidationError("selector type does not match its slot")
    else:
        assert slot.step_ordinal is not None
        if slot.step_ordinal >= ordinal:
            raise WorkflowValidationError("selector cannot reference current or later step")


def validate_revision(revision: WorkflowRevision) -> WorkflowRevision:
    """Re-run validation and return the immutable revision."""

    _validate_cross_step_selectors(revision.steps)
    return revision


def _validate_cross_step_selectors(steps: tuple[WorkflowStep, ...]) -> None:
    """Reject dynamic paths and references to undeclared prior outputs."""

    for _ordinal, step in enumerate(steps, start=1):
        selector = (
            step.input
            if isinstance(step, AgentRunStep)
            else step.content
            if isinstance(step, OwnerNotificationStep)
            else step.due_at
            if isinstance(step, WaitUntilStep)
            else None
        )
        if selector is not None and selector.source == "step_output":
            assert selector.step_ordinal is not None
            prior = steps[selector.step_ordinal - 1]
            if selector.field != _output_field(prior):
                raise WorkflowValidationError("selector references an unknown prior output")
            current_type = (
                WorkflowValueType.BOUNDED_TEXT
                if isinstance(step, (AgentRunStep, OwnerNotificationStep))
                else WorkflowValueType.INSTANT
            )
            if _output_field_type(prior) is not current_type:
                raise WorkflowValidationError("selector type does not match its slot")


@dataclass(frozen=True, slots=True)
class WorkflowInstance:
    id: UUID
    workflow_id: UUID
    revision_id: UUID
    issuer: str
    subject: str
    current_ordinal: int
    status: WorkflowInstanceStatus
    version: int
    admitted_at: datetime
    deadline: datetime
    input_values: Mapping[str, str]
    terminal_outcome: WorkflowTerminalOutcome | None = None
    replay_count: int = 0
    # The admission-time authority facts are pinned to the instance.  They
    # are intentionally a closed value object: no token, policy document, or
    # provider detail crosses the workflow boundary.
    authority: WorkflowAuthoritySnapshot | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None
    completed_effects: tuple[Mapping[str, object], ...] = ()
    checkpoints: tuple[Mapping[str, object], ...] = ()
    result: Mapping[str, object] | None = None
    next_due_at: datetime | None = None
    trace_context: WorkflowTraceContext | None = None

    def __post_init__(self) -> None:
        if self.version <= 0 or self.current_ordinal < 1 or self.current_ordinal > MAX_STEPS:
            raise WorkflowValidationError("instance ordinal or version is invalid")
        if self.deadline <= self.admitted_at:
            raise WorkflowValidationError("instance deadline must be after admission")
        if self.deadline > self.admitted_at + timedelta(days=MAX_INSTANCE_LIFETIME_DAYS):
            raise WorkflowValidationError("instance deadline exceeds 30-day lifetime")
        if (self.status is WorkflowInstanceStatus.TERMINAL) != (self.terminal_outcome is not None):
            raise WorkflowValidationError("terminal outcome must match terminal status")


@dataclass(frozen=True, slots=True)
class WorkflowInstanceAuthorization:
    """Owner-scoped lineage proof that survives terminal-state tombstoning."""

    id: UUID
    workflow_id: UUID
    revision_id: UUID
    issuer: str
    subject: str
    version: int
    status: WorkflowInstanceStatus = WorkflowInstanceStatus.TERMINAL
    terminal_outcome: WorkflowTerminalOutcome | None = None
    tombstoned: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    id: UUID
    display_name: str
    status: str = "active"
    latest_revision_id: UUID | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    retired_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id), "display_name": self.display_name, "status": self.status,
            "latest_revision_id": str(self.latest_revision_id) if self.latest_revision_id else None,
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "retired_at": self.retired_at.isoformat() if self.retired_at else None,
        }


@dataclass(frozen=True, slots=True)
class WorkflowValidationIssue:
    code: str
    path: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path}


@dataclass(frozen=True, slots=True)
class WorkflowValidationResult:
    valid: bool
    issues: tuple[WorkflowValidationIssue | str, ...]
    validated_version: int

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issues": [
                issue.as_dict() if isinstance(issue, WorkflowValidationIssue)
                else {"code": "invalid_value", "path": "/"}
                for issue in self.issues
            ],
            "validated_version": self.validated_version,
        }


@dataclass(frozen=True, slots=True)
class WorkflowPage:
    items: tuple[WorkflowDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowEventPage:
    events: tuple[Mapping[str, object], ...]
    next_cursor: str | None = None
    cursor_outcome: WorkflowEventCursorOutcome = WorkflowEventCursorOutcome.OK


@dataclass(frozen=True, slots=True)
class WorkflowIdempotencyRecord:
    """Owner-scoped semantic fingerprint and opaque command response."""

    fingerprint: str
    response: object


@dataclass(frozen=True, slots=True)
class WorkflowCancelIntent:
    """Durable owner-scoped downstream cancellation propagation state."""

    instance_id: UUID
    execution_id: str
    propagated: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowDeletionReceipt:
    deleted: bool
    deleted_at: datetime
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "deleted": self.deleted,
            "deletedAt": self.deleted_at.isoformat(),
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class WorkflowStepState:
    instance_id: UUID
    ordinal: int
    kind: WorkflowStepKind
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    effect_outcome: WorkflowEffectOutcome = WorkflowEffectOutcome.NOT_ATTEMPTED
    version: int = 1
    execution_id: str | None = None
    output: object | None = None
    recovery_capabilities: tuple[RecoveryCapability, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 1 or self.ordinal > MAX_STEPS or self.version <= 0:
            raise WorkflowValidationError("step state ordinal or version is invalid")
        if self.execution_id is not None:
            validate_opaque_identifier(self.execution_id)


def admit_instance(
    revision: WorkflowRevision,
    *,
    instance_id: UUID | None = None,
    now: datetime,
    input_values: Mapping[str, str],
    authority: WorkflowAuthoritySnapshot | None = None,
) -> WorkflowInstance:
    revision = validate_revision(revision)
    if revision.status is not WorkflowRevisionStatus.PUBLISHED:
        raise WorkflowTransitionError("only a published revision can be admitted")
    current = normalise_instant(now)
    values = dict(input_values)
    declared = {field.name: field.value_type for field in revision.input_fields}
    if set(values) != set(declared):
        raise WorkflowValidationError("instance input must match the declared fields")
    for name, value in values.items():
        if declared[name] is WorkflowValueType.BOUNDED_TEXT:
            values[name] = normalise_text(value)
        else:
            values[name] = normalise_instant(value).isoformat()
    if len(canonical_json(values)) > MAX_INSTANCE_INPUT_BYTES:
        raise WorkflowValidationError("instance input exceeds 32 KiB")
    return WorkflowInstance(
        instance_id or uuid4(),
        revision.workflow_id,
        revision.id,
        revision.issuer,
        revision.subject,
        1,
        WorkflowInstanceStatus.QUEUED,
        1,
        current,
        current + timedelta(days=MAX_INSTANCE_LIFETIME_DAYS),
        values,
        authority=authority,
        updated_at=current,
    )


class WorkflowRepository(Protocol):
    async def get_idempotent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> object | None: ...

    async def get_idempotency_record(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> WorkflowIdempotencyRecord | None: ...

    async def get_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> WorkflowCancelIntent | None: ...

    async def record_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        instance_id: UUID, execution_id: str,
    ) -> None: ...

    async def mark_cancel_propagated(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> None: ...

    async def get_deletion_replay(
        self, issuer: str, subject: str, idempotency_key: str, *,
        workflow_id: UUID | None = None, target_id: UUID | None = None,
        scope: WorkflowDeletionScope | None = None,
        expected_version: int | None = None,
    ) -> WorkflowDeletionReceipt | None: ...

    async def list_definitions(
        self, issuer: str, subject: str, *, limit: int = 50, cursor: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowPage: ...

    async def get_definition(
        self, issuer: str, subject: str, workflow_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDefinition: ...

    async def clone_draft(
        self, issuer: str, subject: str, workflow_id: UUID, *, revision_id: UUID | None = None,
        expected_definition_version: int | None = None,
        idempotency_key: str | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def update_draft(
        self, revision: WorkflowRevision, *, expected_version: int, issuer: str, subject: str,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def validate_draft(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowValidationResult: ...

    async def list_instances(
        self, issuer: str, subject: str, workflow_id: UUID, *, limit: int = 50,
        cursor: str | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> tuple[WorkflowInstance, ...]: ...

    async def create_draft(
        self, revision: WorkflowRevision, *, issuer: str, subject: str,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def create_draft_command(
        self, command: WorkflowCreateDraftCommand, *, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowCreateDraftResult: ...

    async def publish(
        self, revision_id: UUID, *, expected_version: int, issuer: str, subject: str,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def get_revision(
        self, issuer: str, subject: str, revision_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def admit(
        self, issuer: str, subject: str, revision_id: UUID, *,
        input_values: Mapping[str, str], now: datetime | None = None,
        idempotency_key: str | None = None,
        authority: WorkflowAuthoritySnapshot | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance: ...

    async def get_state(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> object: ...

    async def get_state_for_worker(self, instance_id: UUID) -> object: ...

    async def recovery_scan(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> tuple[UUID, ...]: ...

    async def save_state(
        self, state: object, *, expected_version: int, issuer: str, subject: str,
        idempotency_key: str | None = None,
        semantic_fingerprint: str | None = None,
        cancel_intent: WorkflowCancelIntent | None = None,
        lifecycle_event_kind: str = "transition",
        trace_context: WorkflowTraceContext | None = None,
    ) -> object: ...

    async def retire_definition(
        self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> bool: ...

    async def retire_revision(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision: ...

    async def events(
        self, issuer: str, subject: str, *, after: UUID | None = None,
        limit: int = 100, trace_context: WorkflowTraceContext | None = None,
    ) -> tuple[Mapping[str, object], ...]: ...

    async def replay_events(
        self, issuer: str, subject: str, *, after: str | None = None,
        limit: int = 100, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowEventPage: ...

    async def get_instance(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance: ...

    async def authorize_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID,
    ) -> WorkflowInstanceAuthorization: ...

    async def delete(
        self,
        issuer: str,
        subject: str,
        target_id: UUID,
        *,
        workflow_id: UUID | None = None,
        scope: WorkflowDeletionScope,
        expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> bool: ...

    async def delete_receipt(
        self, issuer: str, subject: str, workflow_id: UUID, target_id: UUID, *,
        scope: WorkflowDeletionScope, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDeletionReceipt: ...


@dataclass(frozen=True, slots=True)
class WorkflowLifecycleIntent:
    """Metadata-only lifecycle intent staged with a workflow transaction."""

    event: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkflowWakeIntent:
    """Identifier-only continuation intent staged with a workflow transaction."""

    instance_id: UUID
    reason: str
    execution_id: str | None = None


class WorkflowIntentPort(Protocol):
    async def stage_lifecycle(
        self, intent: WorkflowLifecycleIntent, *, transaction: object | None = None
    ) -> None: ...

    async def stage_wakeup(
        self, intent: WorkflowWakeIntent, *, transaction: object | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowAuthoritySnapshot:
    """Pinned authority facts revalidated before admission and resumption."""

    issuer: str
    subject: str
    household_id: str | None = None
    workspace_id: str | None = None
    agent_revision_id: str | None = None
    grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.issuer or not self.subject:
            raise WorkflowValidationError("workflow authority owner is required")
        if self.agent_revision_id is not None:
            validate_opaque_identifier(self.agent_revision_id)
        for grant in self.grants:
            validate_opaque_identifier(grant)
        canonical_grants = tuple(sorted(set(self.grants)))
        object.__setattr__(self, "grants", canonical_grants)


class WorkflowAuthorityPort(Protocol):
    async def derive(
        self, issuer: str, subject: str, *, revision: WorkflowRevision
    ) -> WorkflowAuthoritySnapshot: ...

    async def revalidate(
        self, authority: WorkflowAuthoritySnapshot, *, revision: WorkflowRevision,
        instance: WorkflowInstance | None, ordinal: int | None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkflowStepExecution:
    """Immutable, bounded context handed to one step handler."""

    workflow_id: UUID
    revision_id: UUID
    instance_id: UUID
    step_ordinal: int
    execution_id: str
    owner: tuple[str, str]
    input_value: object | None
    prior_outputs: Mapping[int, object]
    deadline: datetime
    trace_context: WorkflowTraceContext | None = None
    admitted_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_opaque_identifier(self.execution_id)
        if not 1 <= self.step_ordinal <= MAX_STEPS:
            raise WorkflowValidationError("workflow step ordinal is out of bounds")
        if len(canonical_json(dict(self.prior_outputs))) > MAX_TOTAL_OUTPUT_BYTES:
            raise WorkflowValidationError("workflow execution evidence exceeds its bound")


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    status: WorkflowStepStatus
    effect_outcome: WorkflowEffectOutcome
    output: object | None = None
    replayable: bool = False


class WorkflowStepHandler(Protocol):
    kind: WorkflowStepKind

    async def execute(self, execution: WorkflowStepExecution) -> WorkflowStepResult: ...


class WorkflowLifecycleSink(Protocol):
    async def publish(self, event: Mapping[str, object]) -> None: ...


class WorkflowEvidenceSerializer(Protocol):
    def serialize(self, **metadata: object) -> Mapping[str, object]: ...


__all__ = [
    "WORKFLOW_COMPONENT", "MAX_STEPS", "MAX_WORKFLOW_NAME_BYTES", "MAX_WORKFLOW_NAME_SCALARS",
    "MAX_TEXT_BYTES", "MAX_INSTRUCTIONS_BYTES", "MAX_INSTANCE_INPUT_BYTES", "MAX_STEP_VALUE_BYTES",
    "MAX_TOTAL_OUTPUT_BYTES", "MAX_CHECKPOINT_BYTES", "MAX_INSTANCE_LIFETIME_DAYS", "MAX_APPROVAL_DAYS",
    "MAX_AGENT_DEADLINE_SECONDS", "WorkflowError", "WorkflowValidationError", "WorkflowNotFound",
    "WorkflowVersionConflict", "WorkflowIdempotencyConflict", "WorkflowTransitionError",
    "WorkflowStepKind", "WorkflowValueType", "WorkflowRevisionStatus", "WorkflowInstanceStatus",
    "WorkflowTerminalOutcome", "WorkflowStepStatus", "WorkflowEffectOutcome", "RecoveryCapability",
    "WorkflowDeletionScope", "WorkflowEventCursorOutcome", "WorkflowTraceContext", "WorkflowCommand", "canonical_json", "normalise_text", "normalise_instant",
    "validate_opaque_identifier", "Selector", "InputField", "AgentRunStep", "OwnerApprovalStep",
    "OwnerNotificationStep", "WaitUntilStep", "WorkflowStep", "step_output_type", "WorkflowRevision",
    "validate_revision", "validate_step_result", "WorkflowCreateDraftCommand", "WorkflowCreateDraftResult",
    "WorkflowInstance", "WorkflowInstanceAuthorization", "WorkflowStepState", "WorkflowIdempotencyRecord", "WorkflowCancelIntent", "WorkflowDeletionReceipt", "admit_instance", "WorkflowRepository",
    "WorkflowAuthoritySnapshot", "WorkflowAuthorityPort", "WorkflowStepExecution", "WorkflowStepResult",
    "WorkflowLifecycleIntent", "WorkflowWakeIntent", "WorkflowIntentPort",
    "WorkflowStepHandler", "WorkflowLifecycleSink", "WorkflowEvidenceSerializer",
    "WorkflowDefinition", "WorkflowValidationIssue", "WorkflowValidationResult", "WorkflowPage", "WorkflowEventPage",
]
