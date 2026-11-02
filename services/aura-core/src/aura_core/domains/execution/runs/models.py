"""Public workflow-agent execution value objects.

Workflow execution is owned by the automation domain.  These values form the
small inward port that lets it ask the Run Coordinator to execute its one
bounded agent step without importing conversation persistence or provider
adapters.  They intentionally contain only the pinned lineage and the
model-visible request; workflow state and step settlement remain owned by the
workflow application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

WORKFLOW_AGENT_MAX_SECONDS = 5 * 60
WORKFLOW_AGENT_MAX_TEXT_BYTES = 8 * 1024
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("workflow agent timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_text(value: str, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be text")
    if len(value.encode("utf-8")) > WORKFLOW_AGENT_MAX_TEXT_BYTES:
        raise ValueError(f"{field} exceeds the workflow agent text limit")
    return value


@dataclass(frozen=True, slots=True)
class WorkflowRunLineage:
    """The complete correlation identity for one workflow agent run."""

    run_id: UUID
    workflow_id: UUID
    workflow_revision_id: UUID
    workflow_instance_id: UUID
    workflow_step_ordinal: int
    workflow_step_execution_id: UUID

    def __post_init__(self) -> None:
        if not 1 <= self.workflow_step_ordinal <= 8:
            raise ValueError("workflow step ordinal is out of bounds")
        for value in (
            self.run_id,
            self.workflow_id,
            self.workflow_revision_id,
            self.workflow_instance_id,
            self.workflow_step_execution_id,
        ):
            if type(value) is not UUID:
                raise ValueError("workflow run lineage must use UUIDs")


class WorkflowAgentRunOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkflowAgentRunRequest:
    """A pinned request for one workflow agent step.

    ``admitted_at`` is the truthful workflow-step admission timestamp and
    ``deadline`` may be the enclosing instance deadline.  The coordinator
    independently applies the five-minute provider runtime cap from its
    actual start timestamp.
    """

    lineage: WorkflowRunLineage
    agent_revision_id: UUID
    model_policy_revision_id: UUID
    provider: str
    model_id: str
    instructions: str
    input_text: str
    admitted_at: datetime
    deadline: datetime
    issuer: str
    subject: str
    trace_id: str | None = None
    parent_span_id: str | None = None

    MAX_SECONDS: ClassVar[int] = WORKFLOW_AGENT_MAX_SECONDS

    def __post_init__(self) -> None:
        if type(self.agent_revision_id) is not UUID:
            raise ValueError("agent revision identifier is required")
        if type(self.model_policy_revision_id) is not UUID:
            raise ValueError("model policy revision identifier is required")
        if type(self.provider) is not str or not self.provider:
            raise ValueError("provider is required")
        if type(self.model_id) is not str or not self.model_id:
            raise ValueError("model identifier is required")
        _bounded_text(self.instructions, "workflow agent instructions")
        _bounded_text(self.input_text, "workflow agent input")
        admitted_at = _utc(self.admitted_at)
        deadline = _utc(self.deadline)
        if deadline <= admitted_at:
            raise ValueError("workflow agent deadline must be after admission")
        if type(self.issuer) is not str or not self.issuer:
            raise ValueError("workflow agent issuer is required")
        if type(self.subject) is not str or not self.subject:
            raise ValueError("workflow agent subject is required")
        if self.trace_id is not None and not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError("workflow agent trace identifier is invalid")
        if self.parent_span_id is not None and not _SPAN_ID.fullmatch(self.parent_span_id):
            raise ValueError("workflow agent parent span identifier is invalid")
        object.__setattr__(self, "admitted_at", admitted_at)
        object.__setattr__(self, "deadline", deadline)


@dataclass(frozen=True, slots=True)
class WorkflowAgentRunResult:
    """Closed result returned to the workflow application for settlement."""

    lineage: WorkflowRunLineage
    outcome: WorkflowAgentRunOutcome
    output: str | None
    error_code: str | None
    trace_id: str
    started_at: datetime
    finished_at: datetime

    def __post_init__(self) -> None:
        started_at = _utc(self.started_at)
        finished_at = _utc(self.finished_at)
        if finished_at < started_at:
            raise ValueError("workflow agent result timestamps are invalid")
        if not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError("workflow agent result trace identifier is invalid")
        if self.outcome is WorkflowAgentRunOutcome.COMPLETED:
            if self.error_code is not None:
                raise ValueError("completed workflow agent result cannot carry an error")
            if self.output is None:
                raise ValueError("completed workflow agent result requires output")
            _bounded_text(self.output, "workflow agent output")
        elif self.output is not None:
            raise ValueError("non-completed workflow agent result cannot carry output")
        if (
            self.outcome is WorkflowAgentRunOutcome.UNKNOWN
            and self.error_code != "WORKFLOW_AGENT_UNKNOWN_OUTCOME"
        ):
            raise ValueError("unknown workflow agent result requires an unknown outcome code")
        if self.error_code is not None and (
            type(self.error_code) is not str or not self.error_code
        ):
            raise ValueError("workflow agent error code is invalid")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)

    @property
    def status(self) -> str:
        """Workflow step status for public application adapters."""

        return {
            WorkflowAgentRunOutcome.COMPLETED: "completed",
            WorkflowAgentRunOutcome.FAILED: "failed",
            WorkflowAgentRunOutcome.CANCELED: "canceled",
            WorkflowAgentRunOutcome.UNKNOWN: "failed",
        }[self.outcome]

    @property
    def effect_outcome(self) -> str:
        """Closed workflow effect outcome without importing workflow state."""

        if self.outcome is WorkflowAgentRunOutcome.COMPLETED:
            return "completed"
        if self.outcome is WorkflowAgentRunOutcome.UNKNOWN:
            return "unknown"
        return "definitively_failed"

    @property
    def replayable(self) -> bool:
        """Agent-run effects are never replayable in the bounded slice."""

        return False


__all__ = [
    "WORKFLOW_AGENT_MAX_SECONDS",
    "WORKFLOW_AGENT_MAX_TEXT_BYTES",
    "WorkflowAgentRunOutcome",
    "WorkflowAgentRunRequest",
    "WorkflowAgentRunResult",
    "WorkflowRunLineage",
]
