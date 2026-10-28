"""Public approval commands, DTOs, and owner-bound replay contracts."""

# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from aura_core.domains.automation.reminders.public import (
    ReminderCommandProposal,
    ReminderProposal,
)

APPROVAL_COMPONENT = "aura.governance.approval_workflow"
APPROVAL_TOPIC = "aura.approvals.events.v1"

_WORKFLOW_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WORKFLOW_APPROVAL_TRIPLES: dict[str, tuple[str, str]] = {
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

_COMMAND_SUMMARIES = {
    "series.create": "Create recurring reminder",
    "reschedule": "Reschedule reminder",
    "series.update": "Update recurring reminder",
    "series.pause": "Pause recurring reminder",
    "series.resume": "Resume recurring reminder",
    "cancel": "Cancel reminder",
}


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELED = "canceled"


class ApprovalExecutionStatus(StrEnum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class WorkflowApprovalOrigin:
    """Opaque workflow lineage for an approval; never carries private values."""

    kind: str
    workflow_id: UUID
    workflow_revision_id: UUID
    workflow_instance_id: UUID
    workflow_step_ordinal: int
    workflow_step_execution_id: UUID

    def __post_init__(self) -> None:
        if self.kind != "manual_workflow_instance":
            raise ValueError("unsupported approval origin")
        if any(
            type(value) is not UUID
            for value in (
                self.workflow_id,
                self.workflow_revision_id,
                self.workflow_instance_id,
                self.workflow_step_execution_id,
            )
        ):
            raise ValueError("workflow approval origin must use UUIDs")
        if not 1 <= self.workflow_step_ordinal <= 8:
            raise ValueError("workflow step ordinal is out of bounds")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "workflowId": str(self.workflow_id),
            "workflowRevisionId": str(self.workflow_revision_id),
            "workflowInstanceId": str(self.workflow_instance_id),
            "workflowStepOrdinal": self.workflow_step_ordinal,
            "workflowStepExecutionId": str(self.workflow_step_execution_id),
        }


@dataclass(frozen=True, slots=True)
class WorkflowApprovalProposal:
    """System-derived, content-free presentation for a workflow pause."""

    id: UUID
    workflow_id: UUID
    workflow_revision_id: UUID
    workflow_instance_id: UUID
    step_ordinal: int
    step_execution_id: UUID
    workflow_display_name: str
    next_step_kind: str
    consequence: str
    consequence_copy: str
    owner_scope_id: str
    workspace_scope_id: str
    expires_at: datetime

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workflowId": str(self.workflow_id),
            "workflowRevisionId": str(self.workflow_revision_id),
            "workflowInstanceId": str(self.workflow_instance_id),
            "workflowDisplayName": self.workflow_display_name,
            "stepOrdinal": self.step_ordinal,
            "stepExecutionId": str(self.step_execution_id),
            "nextStepKind": self.next_step_kind,
            "consequence": self.consequence,
            "consequenceCopy": self.consequence_copy,
            "ownerScopeId": self.owner_scope_id,
            "workspaceScopeId": self.workspace_scope_id,
            "origin": "manual_workflow_instance",
            "expiresAt": self.expires_at.astimezone(UTC).isoformat(),
        }
        return payload


@dataclass(frozen=True, slots=True)
class CreateWorkflowApproval:
    issuer: str
    subject: str
    workflow_id: UUID
    workflow_revision_id: UUID
    workflow_instance_id: UUID
    workflow_step_ordinal: int
    workflow_step_execution_id: UUID
    workflow_display_name: str
    next_step_kind: str
    consequence: str
    consequence_copy: str
    owner_scope_id: str
    workspace_scope_id: str
    expires_at: datetime
    idempotency_key: str
    instance_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if not self.issuer or not self.subject:
            raise ValueError("authenticated owner is required")
        if any(
            type(value) is not UUID
            for value in (
                self.workflow_id,
                self.workflow_revision_id,
                self.workflow_instance_id,
                self.workflow_step_execution_id,
            )
        ):
            raise ValueError("workflow approval lineage must use UUIDs")
        if not 1 <= self.workflow_step_ordinal <= 8:
            raise ValueError("workflow step ordinal is out of bounds")
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("approval idempotency key is required")
        triple = _WORKFLOW_APPROVAL_TRIPLES.get(self.next_step_kind)
        if triple is None:
            raise ValueError("unsupported workflow approval next step")
        if (self.consequence, self.consequence_copy) != triple:
            raise ValueError("workflow approval consequence is not the system-derived triple")
        if type(self.workflow_display_name) is not str:
            raise ValueError("workflow approval display name must be text")
        display_name = unicodedata.normalize("NFC", self.workflow_display_name)
        if not display_name or len(display_name) > 160 or len(display_name.encode("utf-8")) > 512:
            raise ValueError("workflow approval display name exceeds its bound")
        object.__setattr__(self, "workflow_display_name", display_name)
        if type(self.owner_scope_id) is not str or not _WORKFLOW_SCOPE_RE.fullmatch(self.owner_scope_id):
            raise ValueError("owner scope is not an opaque identifier")
        if type(self.workspace_scope_id) is not str or not _WORKFLOW_SCOPE_RE.fullmatch(self.workspace_scope_id):
            raise ValueError("workspace scope is not an opaque identifier")
        if self.instance_deadline is None:
            raise ValueError("workflow approval instance deadline is required")
        if self.expires_at.tzinfo is None or self.instance_deadline.tzinfo is None:
            raise ValueError("workflow approval expiry must be timezone-aware")
        expires_at = self.expires_at.astimezone(UTC)
        instance_deadline = self.instance_deadline.astimezone(UTC)
        if expires_at > instance_deadline:
            raise ValueError("workflow approval expiry exceeds the instance deadline")
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "instance_deadline", instance_deadline)


@dataclass(frozen=True, slots=True)
class Approval:
    id: UUID
    issuer: str
    subject: str
    proposal_id: UUID
    run_id: UUID | None
    conversation_id: UUID | None
    tool_call_id: UUID | None
    tool_id: str
    tool_version: int
    status: ApprovalStatus
    execution_status: ApprovalExecutionStatus
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    origin: WorkflowApprovalOrigin | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDetail:
    approval: Approval
    proposal: ReminderProposal | ReminderCommandProposal | WorkflowApprovalProposal


@dataclass(frozen=True, slots=True)
class ApprovalCancellation:
    approval: Approval
    run: object


@dataclass(frozen=True, slots=True)
class ApprovalPage:
    items: tuple[Approval, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ApprovalReplayEvent:
    id: UUID
    approval_id: UUID
    proposal_id: UUID
    run_id: UUID | None
    conversation_id: UUID | None
    tool_id: str
    tool_version: int
    status: ApprovalStatus
    execution_status: ApprovalExecutionStatus
    version: int
    occurred_at: datetime
    origin: WorkflowApprovalOrigin | None = None

    def payload(self) -> dict[str, object]:
        # This envelope is intentionally content-free and does not expose the
        # requested due instant.  Clients fetch authorized detail separately.
        payload: dict[str, object] = {
            "schemaVersion": 1,
            "eventId": str(self.id),
            "eventType": f"approval.{self.status.value}",
            "approvalId": str(self.approval_id),
            "proposalId": str(self.proposal_id),
            "runId": str(self.run_id) if self.run_id else None,
            "conversationId": str(self.conversation_id) if self.conversation_id else None,
            "toolId": self.tool_id,
            "toolVersion": self.tool_version,
            "status": self.status.value,
            "executionStatus": self.execution_status.value,
            "version": self.version,
            "occurredAt": self.occurred_at.astimezone(UTC).isoformat(),
        }
        if self.origin is not None:
            payload["origin"] = self.origin.kind
            payload.update({
                "workflowId": str(self.origin.workflow_id),
                "workflowRevisionId": str(self.origin.workflow_revision_id),
                "workflowInstanceId": str(self.origin.workflow_instance_id),
                "workflowStepOrdinal": self.origin.workflow_step_ordinal,
                "workflowStepExecutionId": str(self.origin.workflow_step_execution_id),
            })
        return payload


class ApprovalError(Exception):
    """Base class for deterministic approval failures."""


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalVersionConflict(ApprovalError):
    pass


class ApprovalIdempotencyConflict(ApprovalError):
    pass


class ApprovalTransitionError(ApprovalError):
    pass


class ApprovalRepository(Protocol):
    async def create_workflow_pending(
        self, command: CreateWorkflowApproval
    ) -> ApprovalDetail: ...

    async def create_pending(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        due_at: datetime,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        tool_id: str = "reminder.create",
        tool_version: int = 1,
        now: datetime | None = None,
        execution_port: object | None = None,
        attempt_id: UUID | None = None,
    ) -> ApprovalDetail: ...

    async def create_command_pending(
        self,
        issuer: str,
        subject: str,
        *,
        operation: str,
        tool_id: str,
        tool_version: int,
        target_id: UUID | None,
        expected_version: int | None,
        normalized_arguments: dict[str, object],
        target_type: str | None = None,
        timezone: str | None,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        now: datetime | None = None,
        execution_port: object | None = None,
        attempt_id: UUID | None = None,
    ) -> ApprovalDetail: ...

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        limit: int,
        cursor: str | None = None,
        status: ApprovalStatus | None = None,
    ) -> ApprovalPage: ...

    async def get(self, issuer: str, subject: str, approval_id: UUID) -> ApprovalDetail: ...

    async def decide(
        self,
        issuer: str,
        subject: str,
        approval_id: UUID,
        *,
        decision: ApprovalStatus,
        expected_version: int,
        idempotency_key: str,
    ) -> Approval: ...

    async def approve(
        self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str
    ) -> Approval: ...

    async def reject(
        self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str
    ) -> Approval: ...

    async def cancel(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval: ...

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> tuple[Approval, ...]: ...

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[ApprovalReplayEvent, ...]: ...

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool: ...

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool: ...

    async def notify_wakeup(self, event_id: UUID) -> None: ...

    async def notify_resume_wakeup(self, event_id: UUID) -> None: ...

    async def cancel_waiting_run(
        self,
        issuer: str,
        subject: str,
        run_id: UUID,
        approval_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ApprovalCancellation: ...

    async def set_execution_status(
        self,
        approval_id: UUID,
        status: ApprovalExecutionStatus,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        run_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> Approval: ...

    async def resume_event(self, event_id: UUID) -> ApprovalReplayEvent | None: ...


class ApprovalExecutionPort(Protocol):
    """Public run seam used while composing the approval transaction."""

    async def wait_for_confirmation(
        self,
        run_id: UUID,
        attempt_id: UUID,
        approval_id: UUID,
        tool_call_id: UUID,
        *,
        transaction: object | None = None,
    ) -> object: ...

    async def lock_waiting_run(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        transaction: object,
    ) -> object: ...

    async def cancel_waiting_run(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        transaction: object,
    ) -> object: ...

    async def resume_waiting_run(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        approved: bool,
        transaction: object,
    ) -> object: ...


class ApprovalApplicationService:
    def __init__(self, repository: ApprovalRepository, *, execution_port: ApprovalExecutionPort | None = None) -> None:
        self.repository = repository
        self.execution_port = execution_port

    async def create_pending(self, *args: object, **kwargs: object) -> ApprovalDetail:
        if self.execution_port is not None:
            kwargs.setdefault("execution_port", self.execution_port)
        return await self.repository.create_pending(*args, **kwargs)  # type: ignore[arg-type]

    async def create_workflow_pending(
        self, command: CreateWorkflowApproval
    ) -> ApprovalDetail:
        creator = getattr(self.repository, "create_workflow_pending", None)
        if not callable(creator):
            raise ApprovalTransitionError("workflow approvals are unavailable")
        return await cast(Callable[[CreateWorkflowApproval], Awaitable[ApprovalDetail]], creator)(command)

    async def create_command_pending(self, *args: object, **kwargs: object) -> ApprovalDetail:
        if self.execution_port is not None:
            kwargs.setdefault("execution_port", self.execution_port)
        creator = getattr(self.repository, "create_command_pending", None)
        if not callable(creator):
            raise ApprovalTransitionError("typed reminder command proposals are unavailable")
        typed_creator = cast(Callable[..., Awaitable[ApprovalDetail]], creator)
        return await typed_creator(*args, **kwargs)

    async def list(self, issuer: str, subject: str, *, limit: int, cursor: str | None = None, status: ApprovalStatus | None = None) -> ApprovalPage:
        return await self.repository.list(issuer, subject, limit=limit, cursor=cursor, status=status)

    async def get(self, issuer: str, subject: str, approval_id: UUID) -> ApprovalDetail:
        return await self.repository.get(issuer, subject, approval_id)

    async def approve(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.repository.decide(issuer, subject, approval_id, decision=ApprovalStatus.APPROVED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def reject(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.repository.decide(issuer, subject, approval_id, decision=ApprovalStatus.REJECTED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def cancel(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.repository.cancel(issuer, subject, approval_id, expected_version=expected_version, idempotency_key=idempotency_key)

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> tuple[Approval, ...]:
        return await self.repository.expire_due(now=now, limit=limit)

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[ApprovalReplayEvent, ...]:
        return await self.repository.replay(issuer, subject, after=after)

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        return await self.repository.cursor_is_authorized(issuer, subject, event_id)

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        return await self.repository.wait_for_wakeup(timeout)

    async def notify_wakeup(self, event_id: UUID) -> None:
        await self.repository.notify_wakeup(event_id)

    async def notify_resume_wakeup(self, event_id: UUID) -> None:
        await self.repository.notify_resume_wakeup(event_id)

    async def cancel_waiting_run(
        self,
        issuer: str,
        subject: str,
        run_id: UUID,
        approval_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ApprovalCancellation:
        return await self.repository.cancel_waiting_run(
            issuer,
            subject,
            run_id,
            approval_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    async def set_execution_status(
        self,
        approval_id: UUID,
        status: ApprovalExecutionStatus | str,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        run_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> Approval:
        return await self.repository.set_execution_status(
            approval_id,
            ApprovalExecutionStatus(status),
            issuer=issuer,
            subject=subject,
            run_id=run_id,
            expected_version=expected_version,
        )

    async def resume_event(self, event_id: UUID) -> ApprovalReplayEvent | None:
        return await self.repository.resume_event(event_id)


_CURSOR_SECRET = b"aura-approval-cursor-v1"


def encode_cursor(issuer: str, subject: str, stamp: datetime, approval_id: UUID, status: ApprovalStatus | None = None) -> str:
    body = {"v": 1, "o": hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest(), "status": status.value if status else None, "t": stamp.astimezone(UTC).isoformat(), "id": str(approval_id)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["s"] = hmac.new(_CURSOR_SECRET, raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, issuer: str, subject: str, status: ApprovalStatus | None = None) -> tuple[datetime, UUID]:
    try:
        body = json.loads(base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()))
        signature = str(body.pop("s"))
        expected = hmac.new(_CURSOR_SECRET, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        owner = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
        if not hmac.compare_digest(signature, expected) or body.get("v") != 1 or body.get("o") != owner:
            raise ValueError
        if body.get("status") not in ({None} if status is None else {status.value}):
            raise ValueError
        return datetime.fromisoformat(str(body["t"])).astimezone(UTC), UUID(str(body["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("invalid approval cursor") from exc


def as_payload(item: Approval) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(item.id), "proposalId": str(item.proposal_id),
        "runId": str(item.run_id) if item.run_id else None,
        "conversationId": str(item.conversation_id) if item.conversation_id else None,
        "toolCallId": str(item.tool_call_id) if item.tool_call_id else None,
        "toolId": item.tool_id, "toolVersion": item.tool_version, "status": item.status.value,
        "executionStatus": item.execution_status.value, "version": item.version,
        "createdAt": item.created_at.astimezone(UTC).isoformat(),
        "updatedAt": item.updated_at.astimezone(UTC).isoformat(),
        "expiresAt": item.expires_at.astimezone(UTC).isoformat(),
        "decidedAt": item.decided_at.astimezone(UTC).isoformat() if item.decided_at else None,
    }
    if item.origin is not None:
        payload["origin"] = item.origin.kind
        payload.update({
            "workflowId": str(item.origin.workflow_id),
            "workflowRevisionId": str(item.origin.workflow_revision_id),
            "workflowInstanceId": str(item.origin.workflow_instance_id),
            "workflowStepOrdinal": item.origin.workflow_step_ordinal,
            "workflowStepExecutionId": str(item.origin.workflow_step_execution_id),
        })
    return payload


def detail_payload(detail: ApprovalDetail) -> dict[str, object]:
    payload = as_payload(detail.approval)
    if isinstance(detail.proposal, WorkflowApprovalProposal):
        payload["proposal"] = detail.proposal.payload()
    elif isinstance(detail.proposal, ReminderCommandProposal):
        payload["proposal"] = {
            "id": str(detail.proposal.id),
            "operation": detail.proposal.operation,
            "summary": _COMMAND_SUMMARIES.get(
                detail.proposal.operation, "Manage reminder"
            ),
            "targetType": detail.proposal.target_type,
            "targetId": str(detail.proposal.target_id) if detail.proposal.target_id else None,
            "expectedVersion": detail.proposal.expected_version,
        }
    else:
        payload["proposal"] = {
            "id": str(detail.proposal.id),
            "content": detail.proposal.content,
            "dueAt": detail.proposal.due_at.astimezone(UTC).isoformat(),
        }
    return payload


__all__ = [
    "APPROVAL_COMPONENT", "APPROVAL_TOPIC", "Approval", "ApprovalApplicationService",
    "ApprovalCancellation", "ApprovalDetail", "ApprovalError", "ApprovalExecutionPort", "ApprovalExecutionStatus", "ApprovalIdempotencyConflict",
    "ApprovalNotFound", "ApprovalPage", "ApprovalReplayEvent", "ApprovalRepository",
    "ApprovalStatus", "ApprovalTransitionError", "ApprovalVersionConflict", "CreateWorkflowApproval",
    "WorkflowApprovalOrigin", "WorkflowApprovalProposal", "as_payload",
    "decode_cursor", "detail_payload", "encode_cursor",
]
