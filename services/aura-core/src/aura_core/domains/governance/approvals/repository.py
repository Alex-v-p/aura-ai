"""Deterministic memory and PostgreSQL approval repositories."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.reminders.public import (
    ReminderProposal,
    ReminderProposalPort,
)
from aura_core.domains.governance.approvals.persistence import (
    ApprovalEventRow,
    ApprovalIdempotencyRow,
    ApprovalRow,
)
from aura_core.domains.governance.approvals.public import (
    APPROVAL_COMPONENT,
    Approval,
    ApprovalCancellation,
    ApprovalDetail,
    ApprovalExecutionStatus,
    ApprovalIdempotencyConflict,
    ApprovalNotFound,
    ApprovalPage,
    ApprovalReplayEvent,
    ApprovalStatus,
    ApprovalTransitionError,
    ApprovalVersionConflict,
    CreateWorkflowApproval,
    WorkflowApprovalOrigin,
    WorkflowApprovalProposal,
    as_payload,
    decode_cursor,
    encode_cursor,
)
from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.platform.outbox import (
    SqlOutboxRepository,
    make_aggregate_command,
    make_approval_event_command,
    make_approval_resume_command,
)
from aura_core.platform.telemetry import MetadataMetrics, new_span_id


def _span(metrics: MetadataMetrics | None, operation: str, started: float, *, outcome: str, approval_id: UUID | None = None, dependency: str = "postgresql", error_class: str | None = None) -> None:
    if metrics is None:
        return
    attributes = {"approval_id": str(approval_id)} if approval_id else {}
    metrics.record_span(APPROVAL_COMPONENT, operation, (monotonic() - started) * 1000.0, trace_id=(approval_id or UUID(int=0)).hex, span_id=new_span_id(), parent_span_id=None, dependency=dependency, outcome=outcome, error_class=error_class, **attributes)


def _wait_metric(metrics: MetadataMetrics | None, item: Approval, *, outcome: str) -> None:
    if metrics is None:
        return
    wait_ms = max(0.0, (item.updated_at - item.created_at).total_seconds() * 1000.0)
    metrics.observe(APPROVAL_COMPONENT, "approval_wait_duration_ms", wait_ms, outcome=outcome)


def _decision_telemetry(
    metrics: MetadataMetrics | None,
    started: float,
    *,
    outcome: str,
    approval_id: UUID | None = None,
    error_class: str | None = None,
    tool_id: str | None = None,
) -> None:
    if metrics is None:
        return
    metrics.increment(APPROVAL_COMPONENT, "approval_decision_outcome", outcome=outcome)
    if tool_id is not None and tool_id != "reminder.create":
        metrics.increment(
            APPROVAL_COMPONENT, "reminder_command_approval_outcome", outcome=outcome
        )
    metrics.observe(APPROVAL_COMPONENT, "approval_decision_duration_ms", (monotonic() - started) * 1000.0, outcome=outcome)
    _span(metrics, "approval.decide", started, outcome=outcome, approval_id=approval_id, error_class=error_class)


def _now() -> datetime:
    return datetime.now(UTC)


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject:
        raise ValueError("authenticated owner is required")
    return issuer, subject


def _approval(row: ApprovalRow | dict[str, object]) -> Approval:
    def value(name: str) -> object:
        return row[name] if isinstance(row, dict) else getattr(row, name)

    def stamp(name: str) -> datetime:
        return value(name).astimezone(UTC)  # type: ignore[union-attr]

    decided = value("decided_at")
    origin_kind = row.get("origin") if isinstance(row, dict) else getattr(row, "origin", None)
    def optional_value(name: str) -> object:
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    origin = None
    if origin_kind == "manual_workflow_instance":
        required = ("workflow_id", "workflow_revision_id", "workflow_instance_id", "workflow_step_ordinal", "workflow_step_execution_id")
        if any(optional_value(name) is None for name in required):
            raise ValueError("workflow approval origin is incomplete")
        origin = WorkflowApprovalOrigin(
            "manual_workflow_instance", UUID(str(optional_value("workflow_id"))),
            UUID(str(optional_value("workflow_revision_id"))), UUID(str(optional_value("workflow_instance_id"))),
            int(str(optional_value("workflow_step_ordinal"))), UUID(str(optional_value("workflow_step_execution_id"))),
        )
    run_id = value("run_id")
    conversation_id = value("conversation_id")
    tool_call_id = value("tool_call_id")
    return Approval(
        UUID(str(value("id"))), str(value("principal_issuer")), str(value("principal_subject")),
        UUID(str(value("proposal_id"))), UUID(str(run_id)) if run_id is not None else None,
        UUID(str(conversation_id)) if conversation_id is not None else None,
        UUID(str(tool_call_id)) if tool_call_id is not None else None, str(value("tool_id")), int(str(value("tool_version"))),
        ApprovalStatus(str(value("status"))), ApprovalExecutionStatus(str(value("execution_status"))),
        int(str(value("version"))), stamp("created_at"), stamp("updated_at"), stamp("expires_at"),
        decided.astimezone(UTC) if isinstance(decided, datetime) else None, origin,
    )


def _event(row: ApprovalEventRow | dict[str, object]) -> ApprovalReplayEvent:
    def value(name: str) -> object:
        return row[name] if isinstance(row, dict) else getattr(row, name)

    occurred = value("occurred_at")
    origin = None
    def optional_value(name: str) -> object:
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    if optional_value("origin") == "manual_workflow_instance":
        origin = WorkflowApprovalOrigin(
            "manual_workflow_instance", UUID(str(optional_value("workflow_id"))),
            UUID(str(optional_value("workflow_revision_id"))), UUID(str(optional_value("workflow_instance_id"))),
            int(str(optional_value("workflow_step_ordinal"))), UUID(str(optional_value("workflow_step_execution_id"))),
        )
    run_id = value("run_id")
    conversation_id = value("conversation_id")
    return ApprovalReplayEvent(
        UUID(str(value("id"))), UUID(str(value("approval_id"))), UUID(str(value("proposal_id"))),
        UUID(str(run_id)) if run_id is not None else None,
        UUID(str(conversation_id)) if conversation_id is not None else None, str(value("tool_id")),
        int(str(value("tool_version"))), ApprovalStatus(str(value("status"))),
        ApprovalExecutionStatus(str(value("execution_status"))), int(str(value("version"))),
        occurred.astimezone(UTC), origin,  # type: ignore[union-attr]
    )


def _fingerprint(operation: str, approval_id: UUID, expected_version: int) -> str:
    return hashlib.sha256(json.dumps({"operation": operation, "approvalId": str(approval_id), "expectedVersion": expected_version}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _workflow_fingerprint(command: CreateWorkflowApproval) -> str:
    payload = {
        "workflowId": str(command.workflow_id),
        "workflowRevisionId": str(command.workflow_revision_id),
        "workflowInstanceId": str(command.workflow_instance_id),
        "workflowStepOrdinal": command.workflow_step_ordinal,
        "workflowStepExecutionId": str(command.workflow_step_execution_id),
        "workflowDisplayName": command.workflow_display_name,
        "nextStepKind": command.next_step_kind,
        "consequence": command.consequence,
        "consequenceCopy": command.consequence_copy,
        "ownerScopeId": command.owner_scope_id,
        "workspaceScopeId": command.workspace_scope_id,
        "expiresAt": command.expires_at.isoformat(),
        "instanceDeadline": command.instance_deadline.isoformat()
        if command.instance_deadline is not None
        else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_workflow_timing(command: CreateWorkflowApproval, created: datetime) -> None:
    if command.expires_at <= created:
        raise ValueError("workflow approval expiry must be in the future")
    if command.expires_at > created + timedelta(days=7):
        raise ValueError("workflow approval expiry cannot exceed seven days")
    if command.instance_deadline is None or command.expires_at > command.instance_deadline:
        raise ValueError("workflow approval expiry exceeds the instance deadline")


@dataclass(frozen=True, slots=True)
class _DecisionResult:
    """Classify a committed SQL decision without overloading one boolean."""

    approval: Approval
    replayed: bool = False
    expired: bool = False


class MemoryApprovalRepository:
    def __init__(self, proposal_repository: ReminderProposalPort, *, clock: Callable[[], datetime] | None = None, metrics: MetadataMetrics | None = None, execution_port: object | None = None) -> None:
        self.proposals = proposal_repository
        self._clock = clock or _now
        self.metrics = metrics
        self.execution_port = execution_port
        self._items: dict[UUID, Approval] = {}
        self._proposals: dict[UUID, ReminderProposal] = {}
        self._workflow_proposals: dict[UUID, WorkflowApprovalProposal] = {}
        self._events: list[ApprovalReplayEvent] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, Approval]] = {}
        self._cancellation_idempotency: dict[tuple[str, str, str], tuple[str, ApprovalCancellation]] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def _time(self) -> datetime:
        value = self._clock()
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    async def create_pending(self, issuer: str, subject: str, *, content: str, due_at: datetime, run_id: UUID, conversation_id: UUID, tool_call_id: UUID, tool_id: str = "reminder.create", tool_version: int = 1, now: datetime | None = None, execution_port: object | None = None, attempt_id: UUID | None = None) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        created = (now or self._time()).astimezone(UTC)
        async with self._lock:
            proposal = await self.proposals.create(issuer, subject, content=content, due_at=due_at, run_id=run_id, conversation_id=conversation_id, tool_call_id=tool_call_id, now=created)
            approval = Approval(uuid4(), issuer, subject, proposal.id, run_id, conversation_id, tool_call_id, tool_id, tool_version, ApprovalStatus.PENDING, ApprovalExecutionStatus.NOT_STARTED, 1, created, created, proposal.expires_at)
            self._proposals[proposal.id] = proposal
            self._items[approval.id] = approval
            self._append_event(approval, created)
            if execution_port is not None and attempt_id is not None:
                waiter = getattr(execution_port, "wait_for_confirmation", None)
                if callable(waiter):
                    await cast(Awaitable[object], waiter(run_id, attempt_id, approval.id, tool_call_id, transaction=None))
            if self.metrics is not None:
                self.metrics.increment(APPROVAL_COMPONENT, "approval_creation_outcome", outcome="ok")
            return ApprovalDetail(approval, proposal)

    async def create_workflow_pending(self, command: CreateWorkflowApproval) -> ApprovalDetail:
        issuer, subject = _owner(command.issuer, command.subject)
        created = self._time()
        _validate_workflow_timing(command, created)
        origin = WorkflowApprovalOrigin(
            "manual_workflow_instance", command.workflow_id, command.workflow_revision_id,
            command.workflow_instance_id, command.workflow_step_ordinal,
            command.workflow_step_execution_id,
        )
        async with self._lock:
            fingerprint = _workflow_fingerprint(command)
            existing = self._idempotency.get((issuer, subject, command.idempotency_key))
            if existing is not None:
                if existing[0] != fingerprint:
                    raise ApprovalIdempotencyConflict("approval idempotency key was already used")
                existing_proposal = self._workflow_proposals.get(existing[1].proposal_id)
                if existing_proposal is None:
                    raise ApprovalNotFound("workflow approval proposal not found")
                return ApprovalDetail(existing[1], existing_proposal)
            approval_id = uuid4()
            proposal = WorkflowApprovalProposal(
                approval_id, command.workflow_id, command.workflow_revision_id,
                command.workflow_instance_id, command.workflow_step_ordinal,
                command.workflow_step_execution_id, command.workflow_display_name,
                command.next_step_kind,
                command.consequence, command.consequence_copy, command.owner_scope_id,
                command.workspace_scope_id, command.expires_at.astimezone(UTC),
            )
            approval = Approval(
                approval_id, issuer, subject, approval_id, None, None, None,
                "workflow.owner_approval", 1, ApprovalStatus.PENDING,
                ApprovalExecutionStatus.NOT_STARTED, 1, created, created,
                proposal.expires_at, None, origin,
            )
            self._items[approval.id] = approval
            self._workflow_proposals[proposal.id] = proposal
            self._idempotency[(issuer, subject, command.idempotency_key)] = (fingerprint, approval)
            self._events.append(ApprovalReplayEvent(
                uuid4(), approval.id, proposal.id, None, None, approval.tool_id,
                approval.tool_version, approval.status, approval.execution_status,
                approval.version, created, origin,
            ))
            self._wake.set()
            self._wake = asyncio.Event()
            return ApprovalDetail(approval, proposal)

    async def create_command_pending(
        self, issuer: str, subject: str, *, operation: str, tool_id: str,
        tool_version: int, target_id: UUID | None, expected_version: int | None,
        normalized_arguments: dict[str, object], timezone: str | None,
        target_type: str | None = None,
        run_id: UUID, conversation_id: UUID, tool_call_id: UUID,
        now: datetime | None = None, execution_port: object | None = None,
        attempt_id: UUID | None = None,
    ) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        created = (now or self._time()).astimezone(UTC)
        async with self._lock:
            proposal = await self.proposals.create_command(
                issuer, subject, operation=operation, tool_id=tool_id,
                tool_version=tool_version, target_id=target_id,
                expected_version=expected_version,
                normalized_arguments=normalized_arguments, timezone=timezone,
                target_type=target_type,
                run_id=run_id, conversation_id=conversation_id,
                tool_call_id=tool_call_id, now=created,
            )
            approval = Approval(
                uuid4(), issuer, subject, proposal.id, run_id, conversation_id,
                tool_call_id, tool_id, tool_version, ApprovalStatus.PENDING,
                ApprovalExecutionStatus.NOT_STARTED, 1, created, created,
                proposal.expires_at,
            )
            self._proposals[proposal.id] = proposal  # type: ignore[assignment]
            self._items[approval.id] = approval
            self._append_event(approval, created)
            waiter_port = execution_port or self.execution_port
            if waiter_port is not None and attempt_id is not None:
                waiter = getattr(waiter_port, "wait_for_confirmation", None)
                if callable(waiter):
                    await cast(Awaitable[object], waiter(run_id, attempt_id, approval.id, tool_call_id, transaction=None))
            if self.metrics is not None:
                self.metrics.increment(
                    APPROVAL_COMPONENT, "reminder_command_approval_outcome", outcome="pending"
                )
            return ApprovalDetail(approval, proposal)

    def _append_event(self, item: Approval, occurred_at: datetime) -> None:
        self._events.append(ApprovalReplayEvent(uuid4(), item.id, item.proposal_id, item.run_id, item.conversation_id, item.tool_id, item.tool_version, item.status, item.execution_status, item.version, occurred_at, item.origin))
        self._wake.set()
        self._wake = asyncio.Event()

    async def list(self, issuer: str, subject: str, *, limit: int, cursor: str | None = None, status: ApprovalStatus | None = None) -> ApprovalPage:
        issuer, subject = _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise ValueError("approval page size must be between 1 and 100")
        marker = decode_cursor(cursor, issuer, subject, status) if cursor else None
        async with self._lock:
            items = [item for item in self._items.values() if (item.issuer, item.subject) == (issuer, subject)]
            if status is not None:
                items = [item for item in items if item.status is status]
            items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            if marker:
                items = [item for item in items if (item.updated_at, item.id) < marker]
            page = items[:limit]
            next_cursor = encode_cursor(issuer, subject, page[-1].updated_at, page[-1].id, status) if len(items) > limit and page else None
            return ApprovalPage(tuple(page), next_cursor)

    async def get(self, issuer: str, subject: str, approval_id: UUID) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject):
                raise ApprovalNotFound("approval not found")
            if item.origin is not None:
                proposal = self._workflow_proposals.get(item.proposal_id)
                if proposal is None:
                    raise ApprovalNotFound("workflow approval proposal not found")
            elif item.tool_id == "reminder.create":
                proposal = self._proposals.get(item.proposal_id) or await self.proposals.get(issuer, subject, item.proposal_id)
            else:
                proposal = await self.proposals.get_command(issuer, subject, item.proposal_id)
            return ApprovalDetail(item, proposal)

    async def _decide_transaction(self, issuer: str, subject: str, approval_id: UUID, *, decision: ApprovalStatus, expected_version: int, idempotency_key: str) -> tuple[Approval, bool]:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject):
                raise ApprovalNotFound("approval not found")
            fingerprint = _fingerprint(decision.value, approval_id, expected_version)
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    raise ApprovalIdempotencyConflict("idempotency key was already used")
                return existing[1], True
            now = self._time()
            if item.status is ApprovalStatus.PENDING and now >= item.expires_at:
                expired = replace(item, status=ApprovalStatus.EXPIRED, execution_status=ApprovalExecutionStatus.DENIED, version=item.version + 1, updated_at=now, decided_at=now)
                self._items[item.id] = expired
                self._append_event(expired, now)
                item = expired
            if item.version != expected_version:
                raise ApprovalVersionConflict("approval version does not match")
            if item.status is not ApprovalStatus.PENDING or decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
                raise ApprovalTransitionError("approval lifecycle transition is not allowed")
            updated = replace(item, status=decision, execution_status=ApprovalExecutionStatus.QUEUED if decision is ApprovalStatus.APPROVED else ApprovalExecutionStatus.DENIED, version=item.version + 1, updated_at=now, decided_at=now)
            self._items[item.id] = updated
            self._idempotency[(issuer, subject, idempotency_key)] = (fingerprint, updated)
            self._append_event(updated, now)
            _wait_metric(self.metrics, updated, outcome=updated.status.value)
            return updated, False

    async def _decide(self, issuer: str, subject: str, approval_id: UUID, *, decision: ApprovalStatus, expected_version: int, idempotency_key: str) -> Approval:
        started = monotonic()
        try:
            result, replayed = await self._decide_transaction(issuer, subject, approval_id, decision=decision, expected_version=expected_version, idempotency_key=idempotency_key)
        except ApprovalIdempotencyConflict:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="idempotency", tool_id=getattr(current, "tool_id", None))
            raise
        except ApprovalVersionConflict:
            current = self._items.get(approval_id)
            expired = current is not None and current.status is ApprovalStatus.EXPIRED
            _decision_telemetry(self.metrics, started, outcome="expired" if expired else "conflict", approval_id=approval_id, error_class=None if expired else "conflict", tool_id=getattr(current, "tool_id", None))
            if expired and current is not None:
                _wait_metric(self.metrics, current, outcome="expired")
            raise
        except ApprovalNotFound:
            _decision_telemetry(self.metrics, started, outcome="not_found", approval_id=approval_id, error_class="not_found")
            raise
        except ApprovalTransitionError:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="lifecycle", tool_id=getattr(current, "tool_id", None))
            raise
        except Exception:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="error", approval_id=approval_id, error_class="database", tool_id=getattr(current, "tool_id", None))
            raise
        _decision_telemetry(self.metrics, started, outcome="duplicate" if replayed else result.status.value, approval_id=result.id, tool_id=result.tool_id)
        return result

    async def decide(self, issuer: str, subject: str, approval_id: UUID, *, decision: ApprovalStatus, expected_version: int, idempotency_key: str) -> Approval:
        return await self._decide(issuer, subject, approval_id, decision=decision, expected_version=expected_version, idempotency_key=idempotency_key)

    async def approve(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.decide(issuer, subject, approval_id, decision=ApprovalStatus.APPROVED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def reject(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.decide(issuer, subject, approval_id, decision=ApprovalStatus.REJECTED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def _cancel_transaction(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> tuple[Approval, bool]:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject):
                raise ApprovalNotFound("approval not found")
            fingerprint = _fingerprint("cancel", approval_id, expected_version)
            existing = self._idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    raise ApprovalIdempotencyConflict("idempotency key was already used")
                return existing[1], True
            if item.version != expected_version or item.status is not ApprovalStatus.PENDING:
                raise ApprovalVersionConflict("approval version does not match")
            now = self._time()
            updated = replace(item, status=ApprovalStatus.CANCELED, execution_status=ApprovalExecutionStatus.DENIED, version=item.version + 1, updated_at=now, decided_at=now)
            self._items[item.id] = updated
            self._idempotency[(issuer, subject, idempotency_key)] = (fingerprint, updated)
            self._append_event(updated, now)
            return updated, False

    async def cancel(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        started = monotonic()
        try:
            result, replayed = await self._cancel_transaction(issuer, subject, approval_id, expected_version=expected_version, idempotency_key=idempotency_key)
        except ApprovalIdempotencyConflict:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="idempotency", tool_id=getattr(current, "tool_id", None))
            raise
        except ApprovalVersionConflict:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="conflict", tool_id=getattr(current, "tool_id", None))
            raise
        except ApprovalNotFound:
            _decision_telemetry(self.metrics, started, outcome="not_found", approval_id=approval_id, error_class="not_found")
            raise
        except Exception:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="error", approval_id=approval_id, error_class="database", tool_id=getattr(current, "tool_id", None))
            raise
        _decision_telemetry(self.metrics, started, outcome="duplicate" if replayed else "canceled", approval_id=result.id, tool_id=result.tool_id)
        return result

    async def _cancel_waiting_transaction(
        self, issuer: str, subject: str, run_id: UUID, approval_id: UUID, *,
        expected_version: int, idempotency_key: str,
    ) -> tuple[ApprovalCancellation, bool]:
        issuer, subject = _owner(issuer, subject)
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or (item.issuer, item.subject) != (issuer, subject) or item.run_id != run_id:
                raise ApprovalNotFound("approval not found")
            fingerprint = _fingerprint("cancel_waiting", approval_id, expected_version)
            existing = self._cancellation_idempotency.get((issuer, subject, idempotency_key))
            if existing:
                if existing[0] != fingerprint:
                    raise ApprovalIdempotencyConflict("idempotency key was already used")
                return existing[1], True
            prior = self._idempotency.get((issuer, subject, idempotency_key))
            if prior is not None:
                raise ApprovalIdempotencyConflict("idempotency key was already used")
            if item.version != expected_version or item.status is not ApprovalStatus.PENDING:
                raise ApprovalVersionConflict("approval version does not match")
            canceler = getattr(self.execution_port, "cancel_waiting_run", None)
            if not callable(canceler):
                raise ApprovalTransitionError("approval run cancellation is unavailable")
            run = await cast(Awaitable[object], canceler(run_id, approval_id, transaction=None))
            now = self._time()
            updated = replace(item, status=ApprovalStatus.CANCELED, execution_status=ApprovalExecutionStatus.DENIED, version=item.version + 1, updated_at=now, decided_at=now)
            self._items[approval_id] = updated
            result = ApprovalCancellation(updated, run)
            self._cancellation_idempotency[(issuer, subject, idempotency_key)] = (fingerprint, result)
            self._append_event(updated, now)
            _wait_metric(self.metrics, updated, outcome="canceled")
            return result, False

    async def cancel_waiting_run(
        self, issuer: str, subject: str, run_id: UUID, approval_id: UUID, *,
        expected_version: int, idempotency_key: str,
    ) -> ApprovalCancellation:
        started = monotonic()
        try:
            result, replayed = await self._cancel_waiting_transaction(issuer, subject, run_id, approval_id, expected_version=expected_version, idempotency_key=idempotency_key)
        except ApprovalIdempotencyConflict:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="idempotency", tool_id=getattr(current, "tool_id", None))
            raise
        except ApprovalVersionConflict:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="conflict", tool_id=getattr(current, "tool_id", None))
            raise
        except ApprovalNotFound:
            _decision_telemetry(self.metrics, started, outcome="not_found", approval_id=approval_id, error_class="not_found")
            raise
        except ApprovalTransitionError:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="lifecycle", tool_id=getattr(current, "tool_id", None))
            raise
        except Exception:
            current = self._items.get(approval_id)
            _decision_telemetry(self.metrics, started, outcome="error", approval_id=approval_id, error_class="database", tool_id=getattr(current, "tool_id", None))
            raise
        _decision_telemetry(self.metrics, started, outcome="duplicate" if replayed else "canceled", approval_id=result.approval.id, tool_id=result.approval.tool_id)
        return result

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> tuple[Approval, ...]:
        current = (now or self._time()).astimezone(UTC)
        started = monotonic()
        if not 1 <= limit <= 100:
            raise ValueError("approval expiry batch must contain 1 to 100 approvals")
        async with self._lock:
            expired: list[Approval] = []
            for item in sorted(self._items.values(), key=lambda value: (value.expires_at, value.id)):
                if len(expired) >= limit:
                    break
                if item.status is not ApprovalStatus.PENDING or item.expires_at > current:
                    continue
                updated = replace(item, status=ApprovalStatus.EXPIRED, execution_status=ApprovalExecutionStatus.DENIED, version=item.version + 1, updated_at=current, decided_at=current)
                self._items[item.id] = updated
                self._append_event(updated, current)
                expired.append(updated)
            if self.metrics is not None and expired:
                self.metrics.observe(APPROVAL_COMPONENT, "approval_expiry_count", float(len(expired)), outcome="expired")
                for item in expired:
                    _wait_metric(self.metrics, item, outcome="expired")
                    if item.tool_id != "reminder.create":
                        self.metrics.increment(
                            APPROVAL_COMPONENT,
                            "reminder_command_approval_outcome",
                            outcome="expired",
                        )
            result = tuple(expired)
        # Empty expiry polls are expected housekeeping, not an observable
        # business operation.  Keep the database cadence unchanged while
        # avoiding one successful span for every idle poll.
        if result:
            _span(self.metrics, "approval.expire", started, outcome="ok", dependency="postgresql")
        return result

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[ApprovalReplayEvent, ...]:
        issuer, subject = _owner(issuer, subject)
        events = [event for event in self._events if (self._items[event.approval_id].issuer, self._items[event.approval_id].subject) == (issuer, subject)]
        if after is None:
            return tuple(events)
        positions = [idx for idx, event in enumerate(events) if event.id == after]
        return tuple(events[positions[0] + 1 :]) if positions else tuple()

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        issuer, subject = _owner(issuer, subject)
        return any(event.id == event_id and (self._items[event.approval_id].issuer, self._items[event.approval_id].subject) == (issuer, subject) for event in self._events)

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        wake = self._wake
        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def notify_wakeup(self, event_id: UUID) -> None:
        del event_id
        self._wake.set()

    async def notify_resume_wakeup(self, event_id: UUID) -> None:
        del event_id
        self._wake.set()

    async def set_execution_status(
        self, approval_id: UUID, status: ApprovalExecutionStatus, *, issuer: str | None = None,
        subject: str | None = None, run_id: UUID | None = None, expected_version: int | None = None,
    ) -> Approval:
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or (issuer is not None and subject is not None and (item.issuer, item.subject) != (issuer, subject)):
                raise ApprovalNotFound("approval not found")
            if run_id is not None and item.run_id != run_id:
                raise ApprovalNotFound("approval not found")
            if expected_version is not None and item.version != expected_version:
                raise ApprovalVersionConflict("approval version does not match")
            allowed = {
                ApprovalExecutionStatus.QUEUED: {ApprovalExecutionStatus.EXECUTING, ApprovalExecutionStatus.DENIED},
                ApprovalExecutionStatus.EXECUTING: {ApprovalExecutionStatus.COMPLETED, ApprovalExecutionStatus.FAILED},
            }
            if status not in allowed.get(item.execution_status, set()):
                raise ApprovalTransitionError("approval execution transition is not allowed")
            now = self._time()
            updated = replace(item, execution_status=status, version=item.version + 1, updated_at=now)
            self._items[approval_id] = updated
            self._append_event(updated, now)
            return updated

    async def resume_event(self, event_id: UUID) -> ApprovalReplayEvent | None:
        async with self._lock:
            return next((event for event in self._events if event.id == event_id), None)


class SqlApprovalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, proposal_repository: ReminderProposalPort, execution_port: object | None = None, clock: Callable[[], datetime] | None = None, metrics: MetadataMetrics | None = None) -> None:
        self.sessions = sessions
        self.proposals = proposal_repository
        self.execution_port = execution_port
        self._clock = clock or _now
        self.metrics = metrics
        self._outbox = SqlOutboxRepository()
        self._audit = SqlAuditRepository()
        self._identities = SqlIdentityRepository()
        self._wake = asyncio.Event()

    def _time(self) -> datetime:
        value = self._clock()
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    async def _tool_id_for(self, approval_id: UUID) -> str | None:
        """Read only the non-sensitive tool identifier for conflict telemetry."""
        try:
            async with self.sessions() as session:
                return await session.scalar(
                    select(ApprovalRow.tool_id).where(ApprovalRow.id == approval_id)
                )
        except Exception:
            return None

    async def create_pending(self, issuer: str, subject: str, *, content: str, due_at: datetime, run_id: UUID, conversation_id: UUID, tool_call_id: UUID, tool_id: str = "reminder.create", tool_version: int = 1, now: datetime | None = None, execution_port: object | None = None, attempt_id: UUID | None = None) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        started = monotonic()
        created = (now or self._time()).astimezone(UTC)
        async with self.sessions() as session, session.begin():
            proposal = await self.proposals.create(issuer, subject, content=content, due_at=due_at, run_id=run_id, conversation_id=conversation_id, tool_call_id=tool_call_id, now=created, transaction=session)
            item = Approval(uuid4(), issuer, subject, proposal.id, run_id, conversation_id, tool_call_id, tool_id, tool_version, ApprovalStatus.PENDING, ApprovalExecutionStatus.NOT_STARTED, 1, created, created, proposal.expires_at)
            session.add(ApprovalRow(id=item.id, principal_issuer=issuer, principal_subject=subject, proposal_id=item.proposal_id, run_id=run_id, conversation_id=conversation_id, tool_call_id=tool_call_id, tool_id=tool_id, tool_version=tool_version, status=item.status.value, execution_status=item.execution_status.value, version=item.version, created_at=created, updated_at=created, expires_at=item.expires_at))
            await self._stage(session, item, created)
            waiter_port = execution_port or self.execution_port
            if waiter_port is not None and attempt_id is not None:
                waiter = getattr(waiter_port, "wait_for_confirmation", None)
                if not callable(waiter):
                    raise TypeError("approval execution port is invalid")
                await cast(Awaitable[object], waiter(run_id, attempt_id, item.id, tool_call_id, transaction=session))
            result = ApprovalDetail(item, proposal)
        if self.metrics is not None:
            self.metrics.increment(APPROVAL_COMPONENT, "approval_creation_outcome", outcome="ok")
        _span(self.metrics, "approval.create", started, outcome="ok", approval_id=result.approval.id)
        return result

    async def create_workflow_pending(self, command: CreateWorkflowApproval) -> ApprovalDetail:
        issuer, subject = _owner(command.issuer, command.subject)
        started = monotonic()
        created = self._time()
        _validate_workflow_timing(command, created)
        origin = WorkflowApprovalOrigin(
            "manual_workflow_instance", command.workflow_id, command.workflow_revision_id,
            command.workflow_instance_id, command.workflow_step_ordinal,
            command.workflow_step_execution_id,
        )
        proposal = WorkflowApprovalProposal(
            uuid4(), command.workflow_id, command.workflow_revision_id,
            command.workflow_instance_id, command.workflow_step_ordinal,
            command.workflow_step_execution_id, command.workflow_display_name,
            command.next_step_kind,
            command.consequence, command.consequence_copy, command.owner_scope_id,
            command.workspace_scope_id, command.expires_at.astimezone(UTC),
        )
        item = Approval(
            uuid4(), issuer, subject, proposal.id, None, None, None,
            "workflow.owner_approval", 1, ApprovalStatus.PENDING,
            ApprovalExecutionStatus.NOT_STARTED, 1, created, created,
            proposal.expires_at, None, origin,
        )
        fingerprint = _workflow_fingerprint(command)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"aura.workflow-approval:{issuer}:{subject}:{command.idempotency_key}"},
            )
            existing = await session.get(ApprovalIdempotencyRow, (issuer, subject, command.idempotency_key))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ApprovalIdempotencyConflict("approval idempotency key was already used")
                row = await session.get(ApprovalRow, existing.approval_id)
                if row is None:
                    raise ApprovalNotFound("approval not found")
                return ApprovalDetail(_approval(row), WorkflowApprovalProposal(
                    UUID(str(row.id)), UUID(str(row.workflow_id)), UUID(str(row.workflow_revision_id)),
                    UUID(str(row.workflow_instance_id)), int(str(row.workflow_step_ordinal)),
                    UUID(str(row.workflow_step_execution_id)), str(row.workflow_display_name),
                    str(row.next_step_kind),
                    str(row.consequence), str(row.consequence_copy), str(row.owner_scope_id),
                    str(row.workspace_scope_id), row.expires_at.astimezone(UTC),
                ))
            session.add(ApprovalRow(
                id=item.id, principal_issuer=issuer, principal_subject=subject,
                proposal_id=proposal.id, run_id=None, conversation_id=None, tool_call_id=None,
                tool_id=item.tool_id, tool_version=item.tool_version, status=item.status.value,
                execution_status=item.execution_status.value, version=item.version,
                created_at=created, updated_at=created, expires_at=proposal.expires_at,
                origin=origin.kind, workflow_id=origin.workflow_id,
                workflow_revision_id=origin.workflow_revision_id,
                workflow_instance_id=origin.workflow_instance_id,
                workflow_step_ordinal=origin.workflow_step_ordinal,
                workflow_step_execution_id=origin.workflow_step_execution_id,
                workflow_display_name=proposal.workflow_display_name,
                next_step_kind=proposal.next_step_kind, consequence=proposal.consequence,
                consequence_copy=proposal.consequence_copy, owner_scope_id=proposal.owner_scope_id,
                workspace_scope_id=proposal.workspace_scope_id,
            ))
            await self._stage(session, item, created)
            session.add(ApprovalIdempotencyRow(
                principal_issuer=issuer, principal_subject=subject,
                idempotency_key=command.idempotency_key, fingerprint=fingerprint,
                approval_id=item.id, response=as_payload(item), created_at=created,
            ))
        _span(self.metrics, "approval.create", started, outcome="ok", approval_id=item.id)
        return ApprovalDetail(item, proposal)

    async def create_command_pending(
        self, issuer: str, subject: str, *, operation: str, tool_id: str,
        tool_version: int, target_id: UUID | None, expected_version: int | None,
        normalized_arguments: dict[str, object], timezone: str | None,
        target_type: str | None = None,
        run_id: UUID, conversation_id: UUID, tool_call_id: UUID,
        now: datetime | None = None, execution_port: object | None = None,
        attempt_id: UUID | None = None,
    ) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        started = monotonic()
        created = (now or self._time()).astimezone(UTC)
        async with self.sessions() as session, session.begin():
            proposal = await self.proposals.create_command(
                issuer, subject, operation=operation, tool_id=tool_id,
                tool_version=tool_version, target_id=target_id,
                expected_version=expected_version,
                normalized_arguments=normalized_arguments, timezone=timezone,
                target_type=target_type,
                run_id=run_id, conversation_id=conversation_id,
                tool_call_id=tool_call_id, now=created, transaction=session,
            )
            item = Approval(
                uuid4(), issuer, subject, proposal.id, run_id, conversation_id,
                tool_call_id, tool_id, tool_version, ApprovalStatus.PENDING,
                ApprovalExecutionStatus.NOT_STARTED, 1, created, created,
                proposal.expires_at,
            )
            session.add(ApprovalRow(
                id=item.id, principal_issuer=issuer, principal_subject=subject,
                proposal_id=item.proposal_id, run_id=run_id, conversation_id=conversation_id,
                tool_call_id=tool_call_id, tool_id=tool_id, tool_version=tool_version,
                status=item.status.value, execution_status=item.execution_status.value,
                version=item.version, created_at=created, updated_at=created,
                expires_at=item.expires_at,
            ))
            await self._stage(session, item, created)
            waiter_port = execution_port or self.execution_port
            if waiter_port is not None and attempt_id is not None:
                waiter = getattr(waiter_port, "wait_for_confirmation", None)
                if not callable(waiter):
                    raise TypeError("approval execution port is invalid")
                await cast(Awaitable[object], waiter(run_id, attempt_id, item.id, tool_call_id, transaction=session))
            result = ApprovalDetail(item, proposal)
        if self.metrics is not None:
            self.metrics.increment(APPROVAL_COMPONENT, "approval_creation_outcome", outcome="ok")
            self.metrics.increment(
                APPROVAL_COMPONENT, "reminder_command_approval_outcome", outcome="pending"
            )
        _span(self.metrics, "approval.create", started, outcome="ok", approval_id=result.approval.id)
        return result

    async def _lock_owner_stream(
        self, session: AsyncSession, issuer: str, subject: str
    ) -> None:
        # Sequence values are allocated at flush time. Serialize one owner's
        # event stream until this transaction commits so a later position
        # cannot become visible before an earlier same-owner transaction.
        # The lock key is hashed inside PostgreSQL; no principal text enters
        # the advisory-lock identifier.
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:issuer, 0) # hashtextextended(:subject, 1))"
            ),
            {"issuer": issuer, "subject": subject},
        )

    async def _stage(self, session: AsyncSession, item: Approval, now: datetime, *, resume: bool = False) -> None:
        await self._lock_owner_stream(session, item.issuer, item.subject)
        event_id = uuid4()
        session.add(ApprovalEventRow(id=event_id, principal_issuer=item.issuer, principal_subject=item.subject, approval_id=item.id, proposal_id=item.proposal_id, run_id=item.run_id, conversation_id=item.conversation_id, tool_id=item.tool_id, tool_version=item.tool_version, status=item.status.value, execution_status=item.execution_status.value, version=item.version, occurred_at=now, origin=item.origin.kind if item.origin else None, workflow_id=item.origin.workflow_id if item.origin else None, workflow_revision_id=item.origin.workflow_revision_id if item.origin else None, workflow_instance_id=item.origin.workflow_instance_id if item.origin else None, workflow_step_ordinal=item.origin.workflow_step_ordinal if item.origin else None, workflow_step_execution_id=item.origin.workflow_step_execution_id if item.origin else None))
        if item.origin is not None:
            command = make_aggregate_command(
                "aura.approvals.events.v1", event_id, "governance.approval", item.id,
                identifiers=(("approvalId", item.id), ("eventId", event_id),
                             ("eventType", f"approval.{item.status.value}"),
                             ("status", item.status.value), ("version", item.version),
                             ("origin", item.origin.kind),
                             ("workflowId", item.origin.workflow_id),
                             ("workflowRevisionId", item.origin.workflow_revision_id),
                             ("workflowInstanceId", item.origin.workflow_instance_id),
                             ("workflowStepOrdinal", item.origin.workflow_step_ordinal),
                             ("workflowStepExecutionId", item.origin.workflow_step_execution_id)),
            )
        else:
            if item.run_id is None or item.conversation_id is None:
                raise ApprovalTransitionError("legacy approval lineage is incomplete")
            command = make_approval_event_command(
                item.id, event_id=event_id, status=item.status.value,
                version=item.version, run_id=item.run_id, conversation_id=item.conversation_id,
                event_type=f"approval.{item.status.value}", command_id=event_id,
            )
        await self._outbox.stage_command(session, command=command)
        if resume and item.origin is None:
            if item.run_id is None or item.conversation_id is None:
                raise ApprovalTransitionError("legacy approval lineage is incomplete")
            await self._outbox.stage_command(
                session,
                command=make_approval_resume_command(
                    item.run_id,
                    item.conversation_id,
                    approval_id=item.id,
                    event_id=event_id,
                    status=item.status.value,
                    version=item.version,
                    command_id=uuid4(),
                ),
            )
        principal = await self._identities.resolve(session, item.issuer, item.subject)
        self._audit.stage(session, principal.id, f"approval.{item.status.value}", str(item.id), {"status": item.status.value, "version": item.version})

    async def list(self, issuer: str, subject: str, *, limit: int, cursor: str | None = None, status: ApprovalStatus | None = None) -> ApprovalPage:
        issuer, subject = _owner(issuer, subject)
        marker = decode_cursor(cursor, issuer, subject, status) if cursor else None
        async with self.sessions() as session:
            query = select(ApprovalRow).where(ApprovalRow.principal_issuer == issuer, ApprovalRow.principal_subject == subject).order_by(ApprovalRow.updated_at.desc(), ApprovalRow.id.desc()).limit(limit + 1)
            if status is not None:
                query = query.where(ApprovalRow.status == status.value)
            if marker:
                stamp, item_id = marker
                query = query.where((ApprovalRow.updated_at < stamp) | ((ApprovalRow.updated_at == stamp) & (ApprovalRow.id < item_id)))
            rows = list((await session.execute(query)).scalars().all())
            items = tuple(_approval(row) for row in rows[:limit])
            return ApprovalPage(items, encode_cursor(issuer, subject, items[-1].updated_at, items[-1].id, status) if len(rows) > limit and items else None)

    async def get(self, issuer: str, subject: str, approval_id: UUID) -> ApprovalDetail:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            row = (await session.execute(select(ApprovalRow).where(ApprovalRow.id == approval_id, ApprovalRow.principal_issuer == issuer, ApprovalRow.principal_subject == subject))).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound("approval not found")
            if row.origin == "manual_workflow_instance":
                proposal = WorkflowApprovalProposal(
                    UUID(str(row.id)), UUID(str(row.workflow_id)), UUID(str(row.workflow_revision_id)),
                    UUID(str(row.workflow_instance_id)), int(str(row.workflow_step_ordinal)),
                    UUID(str(row.workflow_step_execution_id)), str(row.workflow_display_name),
                    str(row.next_step_kind),
                    str(row.consequence), str(row.consequence_copy), str(row.owner_scope_id),
                    str(row.workspace_scope_id), row.expires_at.astimezone(UTC),
                )
            elif row.tool_id == "reminder.create":
                proposal = await self.proposals.get(issuer, subject, row.proposal_id)
            else:
                proposal = await self.proposals.get_command(issuer, subject, row.proposal_id)
            return ApprovalDetail(_approval(row), proposal)

    async def _decide_transaction(self, issuer: str, subject: str, approval_id: UUID, *, decision: ApprovalStatus, expected_version: int, idempotency_key: str) -> _DecisionResult:
        issuer, subject = _owner(issuer, subject)
        if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.CANCELED}:
            raise ApprovalTransitionError("unsupported approval decision")
        late_expired = False
        result: Approval | None = None
        async with self.sessions() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": json.dumps([issuer, subject, idempotency_key], ensure_ascii=True, separators=(",", ":"))})
            fingerprint = _fingerprint(decision.value, approval_id, expected_version)
            existing = await session.get(ApprovalIdempotencyRow, (issuer, subject, idempotency_key))
            if existing:
                if existing.fingerprint != fingerprint:
                    raise ApprovalIdempotencyConflict("idempotency key was already used")
                existing_row = await session.get(ApprovalRow, existing.approval_id)
                if existing_row is None:
                    raise ApprovalNotFound("approval not found")
                return _DecisionResult(_approval(existing_row), replayed=True)
            row = (await session.execute(select(ApprovalRow).where(ApprovalRow.id == approval_id, ApprovalRow.principal_issuer == issuer, ApprovalRow.principal_subject == subject).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound("approval not found")
            if row.origin is None and self.execution_port is not None:
                locker = getattr(self.execution_port, "lock_waiting_run", None)
                if callable(locker):
                    await cast(Awaitable[object], locker(row.run_id, row.id, transaction=session))
            now = self._time()
            if row.status == ApprovalStatus.PENDING.value and now >= row.expires_at:
                row.status = ApprovalStatus.EXPIRED.value
                row.execution_status = ApprovalExecutionStatus.DENIED.value
                row.version += 1
                row.updated_at = row.decided_at = now
                expired_item = _approval(row)
                await self._stage(session, expired_item, now, resume=True)
                if row.origin is None and self.execution_port is not None:
                    resumer = getattr(self.execution_port, "resume_waiting_run", None)
                    if callable(resumer):
                        await cast(Awaitable[object], resumer(row.run_id, row.id, approved=False, transaction=session))
                session.add(ApprovalIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, approval_id=expired_item.id, response={"id": str(expired_item.id), "version": expired_item.version}, created_at=now))
                late_expired = True
            if row.version != expected_version:
                if not late_expired:
                    raise ApprovalVersionConflict("approval version does not match")
                result = _approval(row)
            elif row.status != ApprovalStatus.PENDING.value:
                if not late_expired:
                    raise ApprovalTransitionError("approval lifecycle transition is not allowed")
                result = _approval(row)
            else:
                row.status = decision.value
                row.execution_status = ApprovalExecutionStatus.QUEUED.value if decision is ApprovalStatus.APPROVED else ApprovalExecutionStatus.DENIED.value
                row.version += 1
                row.updated_at = row.decided_at = now
                result = _approval(row)
                await self._stage(session, result, now, resume=True)
                if row.origin is None and decision is not ApprovalStatus.CANCELED and self.execution_port is not None:
                    resumer = getattr(self.execution_port, "resume_waiting_run", None)
                    if callable(resumer):
                        await cast(Awaitable[object], resumer(row.run_id, row.id, approved=decision is ApprovalStatus.APPROVED, transaction=session))
                session.add(ApprovalIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, approval_id=result.id, response={"id": str(result.id), "version": result.version}, created_at=now))
        assert result is not None
        return _DecisionResult(result, expired=late_expired)

    async def decide(self, issuer: str, subject: str, approval_id: UUID, *, decision: ApprovalStatus, expected_version: int, idempotency_key: str) -> Approval:
        started = monotonic()
        try:
            decision_result = await self._decide_transaction(issuer, subject, approval_id, decision=decision, expected_version=expected_version, idempotency_key=idempotency_key)
        except ApprovalIdempotencyConflict:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="idempotency", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalVersionConflict:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="conflict", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalNotFound:
            _decision_telemetry(self.metrics, started, outcome="not_found", approval_id=approval_id, error_class="not_found", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalTransitionError:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="lifecycle", tool_id=await self._tool_id_for(approval_id))
            raise
        except Exception:
            _decision_telemetry(self.metrics, started, outcome="error", approval_id=approval_id, error_class="database", tool_id=await self._tool_id_for(approval_id))
            raise
        if decision_result.replayed:
            _decision_telemetry(self.metrics, started, outcome="duplicate", approval_id=decision_result.approval.id, tool_id=decision_result.approval.tool_id)
            return decision_result.approval
        if decision_result.expired:
            _decision_telemetry(self.metrics, started, outcome="expired", approval_id=approval_id, tool_id=decision_result.approval.tool_id)
            _wait_metric(self.metrics, decision_result.approval, outcome="expired")
            raise ApprovalVersionConflict("approval expired before the decision")
        _decision_telemetry(self.metrics, started, outcome=decision_result.approval.status.value, approval_id=decision_result.approval.id, tool_id=decision_result.approval.tool_id)
        _wait_metric(self.metrics, decision_result.approval, outcome=decision_result.approval.status.value)
        return decision_result.approval

    async def approve(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.decide(issuer, subject, approval_id, decision=ApprovalStatus.APPROVED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def reject(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.decide(issuer, subject, approval_id, decision=ApprovalStatus.REJECTED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def cancel(self, issuer: str, subject: str, approval_id: UUID, *, expected_version: int, idempotency_key: str) -> Approval:
        return await self.decide(issuer, subject, approval_id, decision=ApprovalStatus.CANCELED, expected_version=expected_version, idempotency_key=idempotency_key)

    async def _cancel_waiting_transaction(
        self, issuer: str, subject: str, run_id: UUID, approval_id: UUID, *,
        expected_version: int, idempotency_key: str,
    ) -> tuple[ApprovalCancellation, bool]:
        issuer, subject = _owner(issuer, subject)
        if self.execution_port is None:
            raise ApprovalTransitionError("approval run cancellation is unavailable")
        fingerprint = _fingerprint("cancel_waiting", approval_id, expected_version)
        async with self.sessions() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": json.dumps([issuer, subject, idempotency_key], ensure_ascii=True, separators=(",", ":"))})
            existing = await session.get(ApprovalIdempotencyRow, (issuer, subject, idempotency_key))
            if existing:
                if existing.fingerprint != fingerprint:
                    raise ApprovalIdempotencyConflict("idempotency key was already used")
                existing_row = await session.get(ApprovalRow, existing.approval_id)
                if existing_row is None:
                    raise ApprovalNotFound("approval not found")
                return ApprovalCancellation(_approval(existing_row), object()), True
            row = (await session.execute(select(ApprovalRow).where(ApprovalRow.id == approval_id, ApprovalRow.principal_issuer == issuer, ApprovalRow.principal_subject == subject, ApprovalRow.run_id == run_id).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFound("approval not found")
            if row.version != expected_version:
                raise ApprovalVersionConflict("approval version does not match")
            if row.status != ApprovalStatus.PENDING.value:
                raise ApprovalTransitionError("approval lifecycle transition is not allowed")
            canceler = getattr(self.execution_port, "cancel_waiting_run", None)
            if not callable(canceler):
                raise ApprovalTransitionError("approval run cancellation is unavailable")
            run = await cast(Awaitable[object], canceler(run_id, approval_id, transaction=session))
            now = self._time()
            row.status = ApprovalStatus.CANCELED.value
            row.execution_status = ApprovalExecutionStatus.DENIED.value
            row.version += 1
            row.updated_at = row.decided_at = now
            item = _approval(row)
            await self._stage(session, item, now, resume=True)
            session.add(ApprovalIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=idempotency_key, fingerprint=fingerprint, approval_id=item.id, response={"id": str(item.id), "version": item.version}, created_at=now))
        _wait_metric(self.metrics, item, outcome="canceled")
        return ApprovalCancellation(item, run), False

    async def cancel_waiting_run(
        self, issuer: str, subject: str, run_id: UUID, approval_id: UUID, *,
        expected_version: int, idempotency_key: str,
    ) -> ApprovalCancellation:
        started = monotonic()
        try:
            result, replayed = await self._cancel_waiting_transaction(issuer, subject, run_id, approval_id, expected_version=expected_version, idempotency_key=idempotency_key)
        except ApprovalIdempotencyConflict:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="idempotency", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalVersionConflict:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="conflict", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalNotFound:
            _decision_telemetry(self.metrics, started, outcome="not_found", approval_id=approval_id, error_class="not_found", tool_id=await self._tool_id_for(approval_id))
            raise
        except ApprovalTransitionError:
            _decision_telemetry(self.metrics, started, outcome="conflict", approval_id=approval_id, error_class="lifecycle", tool_id=await self._tool_id_for(approval_id))
            raise
        except Exception:
            _decision_telemetry(self.metrics, started, outcome="error", approval_id=approval_id, error_class="database", tool_id=await self._tool_id_for(approval_id))
            raise
        _decision_telemetry(self.metrics, started, outcome="duplicate" if replayed else "canceled", approval_id=result.approval.id, tool_id=result.approval.tool_id)
        return result

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> tuple[Approval, ...]:
        current = (now or self._time()).astimezone(UTC)
        started = monotonic()
        if not 1 <= limit <= 100:
            raise ValueError("approval expiry batch must contain 1 to 100 approvals")
        try:
            async with self.sessions() as session, session.begin():
                rows = list((await session.execute(select(ApprovalRow).where(ApprovalRow.status == ApprovalStatus.PENDING.value, ApprovalRow.expires_at <= current).order_by(ApprovalRow.expires_at, ApprovalRow.id).limit(limit).with_for_update(skip_locked=True))).scalars().all())
                for issuer, subject in sorted(
                    {(row.principal_issuer, row.principal_subject) for row in rows}
                ):
                    await self._lock_owner_stream(session, issuer, subject)
                expired: list[Approval] = []
                for row in rows:
                    if row.origin is None and self.execution_port is not None:
                        locker = getattr(self.execution_port, "lock_waiting_run", None)
                        if callable(locker):
                            await cast(Awaitable[object], locker(row.run_id, row.id, transaction=session))
                    row.status = ApprovalStatus.EXPIRED.value
                    row.execution_status = ApprovalExecutionStatus.DENIED.value
                    row.version += 1
                    row.updated_at = row.decided_at = current
                    item = _approval(row)
                    await self._stage(session, item, current, resume=True)
                    if row.origin is None and self.execution_port is not None:
                        resumer = getattr(self.execution_port, "resume_waiting_run", None)
                        if callable(resumer):
                            await cast(Awaitable[object], resumer(row.run_id, row.id, approved=False, transaction=session))
                    expired.append(item)
                result = tuple(expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            _span(
                self.metrics,
                "approval.expire",
                started,
                outcome="error",
                dependency="postgresql",
                error_class="database",
            )
            raise
        # Empty expiry polls are expected housekeeping, not an observable
        # business operation.  Failures above remain immediately observable.
        if result:
            _span(self.metrics, "approval.expire", started, outcome="ok", dependency="postgresql")
        if self.metrics is not None and result:
            self.metrics.observe(APPROVAL_COMPONENT, "approval_expiry_count", float(len(result)), outcome="expired")
            for item in result:
                _wait_metric(self.metrics, item, outcome="expired")
                if item.tool_id != "reminder.create":
                    self.metrics.increment(
                        APPROVAL_COMPONENT,
                        "reminder_command_approval_outcome",
                        outcome="expired",
                    )
        return result

    async def replay(self, issuer: str, subject: str, *, after: UUID | None = None) -> tuple[ApprovalReplayEvent, ...]:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            position: int | None = None
            if after is not None:
                position = (
                    await session.execute(
                        select(ApprovalEventRow.replay_position).where(
                            ApprovalEventRow.id == after,
                            ApprovalEventRow.principal_issuer == issuer,
                            ApprovalEventRow.principal_subject == subject,
                        )
                    )
                ).scalar_one_or_none()
                if position is None:
                    return ()
            query = select(ApprovalEventRow).where(
                ApprovalEventRow.principal_issuer == issuer,
                ApprovalEventRow.principal_subject == subject,
            )
            if position is not None:
                query = query.where(ApprovalEventRow.replay_position > position)
            query = query.order_by(ApprovalEventRow.replay_position)
            rows = list((await session.execute(query)).scalars().all())
            events = [_event(row) for row in rows]
            return tuple(events)

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: UUID) -> bool:
        issuer, subject = _owner(issuer, subject)
        async with self.sessions() as session:
            return (await session.execute(select(ApprovalEventRow.id).where(ApprovalEventRow.id == event_id, ApprovalEventRow.principal_issuer == issuer, ApprovalEventRow.principal_subject == subject))).scalar_one_or_none() is not None

    async def wait_for_wakeup(self, timeout: float = 15.0) -> bool:
        wake = self._wake
        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def notify_wakeup(self, event_id: UUID) -> None:
        del event_id
        self._wake.set()

    async def notify_resume_wakeup(self, event_id: UUID) -> None:
        del event_id
        self._wake.set()

    async def set_execution_status(
        self, approval_id: UUID, status: ApprovalExecutionStatus, *, issuer: str | None = None,
        subject: str | None = None, run_id: UUID | None = None, expected_version: int | None = None,
    ) -> Approval:
        async with self.sessions() as session, session.begin():
            query = select(ApprovalRow).where(ApprovalRow.id == approval_id)
            if issuer is not None and subject is not None:
                query = query.where(ApprovalRow.principal_issuer == issuer, ApprovalRow.principal_subject == subject)
            row = (await session.execute(query.with_for_update())).scalar_one_or_none()
            if row is None or (run_id is not None and row.run_id != run_id):
                raise ApprovalNotFound("approval not found")
            if expected_version is not None and row.version != expected_version:
                raise ApprovalVersionConflict("approval version does not match")
            current = ApprovalExecutionStatus(row.execution_status)
            allowed = {
                ApprovalExecutionStatus.QUEUED: {ApprovalExecutionStatus.EXECUTING, ApprovalExecutionStatus.DENIED},
                ApprovalExecutionStatus.EXECUTING: {ApprovalExecutionStatus.COMPLETED, ApprovalExecutionStatus.FAILED},
            }
            if status not in allowed.get(current, set()):
                raise ApprovalTransitionError("approval execution transition is not allowed")
            now = self._time()
            row.execution_status = status.value
            row.version += 1
            row.updated_at = now
            item = _approval(row)
            await self._stage(session, item, now)
        return item

    async def resume_event(self, event_id: UUID) -> ApprovalReplayEvent | None:
        async with self.sessions() as session:
            row = (await session.execute(select(ApprovalEventRow).where(ApprovalEventRow.id == event_id))).scalar_one_or_none()
            return _event(row) if row is not None else None


__all__ = ["MemoryApprovalRepository", "SqlApprovalRepository"]
