"""Deterministic workflow repositories.

``MemoryWorkflowRepository`` is used by Core unit tests and by composition
tests.  The SQL adapter is intentionally small: the row mappings remain the
only persistence implementation, while transitions are supplied by
``state.py``.
"""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from time import monotonic
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.platform.telemetry import MetadataMetrics, new_span_id

from .models import WorkflowDraft
from .public import (
    WORKFLOW_COMPONENT,
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    OwnerNotificationStep,
    RecoveryCapability,
    Selector,
    WaitUntilStep,
    WorkflowCancelIntent,
    WorkflowCreateDraftCommand,
    WorkflowCreateDraftResult,
    WorkflowDefinition,
    WorkflowDeletionReceipt,
    WorkflowDeletionScope,
    WorkflowEffectOutcome,
    WorkflowError,
    WorkflowEventCursorOutcome,
    WorkflowEventPage,
    WorkflowIdempotencyConflict,
    WorkflowIdempotencyRecord,
    WorkflowInstance,
    WorkflowInstanceAuthorization,
    WorkflowInstanceStatus,
    WorkflowIntentPort,
    WorkflowLifecycleIntent,
    WorkflowNotFound,
    WorkflowPage,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowStep,
    WorkflowStepKind,
    WorkflowStepState,
    WorkflowStepStatus,
    WorkflowTerminalOutcome,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowValidationIssue,
    WorkflowValidationResult,
    WorkflowValueType,
    WorkflowVersionConflict,
    WorkflowWakeIntent,
    admit_instance,
    canonical_json,
    validate_revision,
)
from .state import WorkflowState, validate_state_budget


def command_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json({"operation": operation, "payload": payload})).hexdigest()


def _revision_from_create_command(
    command: WorkflowCreateDraftCommand, idempotency_key: str | None
) -> WorkflowRevision:
    """Materialize server-owned IDs only after the semantic command is received."""
    if idempotency_key:
        seed = f"{command.issuer}:{command.subject}:{idempotency_key}"
        workflow_id = uuid5(NAMESPACE_URL, f"aura-workflow:{seed}")
        revision_id = uuid5(NAMESPACE_URL, f"aura-workflow-revision:{seed}")
    else:
        workflow_id, revision_id = uuid4(), uuid4()
    return WorkflowRevision(
        id=revision_id, workflow_id=workflow_id,
        issuer=command.issuer, subject=command.subject,
        display_name=command.display_name, input_fields=command.input_fields,
        steps=command.steps, household_id=command.household_id,
        workspace_id=command.workspace_id,
    )


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject:
        raise WorkflowValidationError("authenticated workflow owner is required")
    return issuer, subject


def _trace_context(context: WorkflowTraceContext | None) -> WorkflowTraceContext:
    if context is None or not context.trace_id or set(context.trace_id) == {"0"}:
        return WorkflowTraceContext(secrets.token_hex(16))
    return context


def _lifecycle_envelope(state: WorkflowState, event_kind: str, event_id: UUID) -> dict[str, object]:
    """Build the closed metadata-only lifecycle event contract."""
    event_step = state.current
    if event_step.status is WorkflowStepStatus.PENDING and event_step.ordinal > 1:
        event_step = state.steps[event_step.ordinal - 2]
    if event_kind == "admitted":
        event_type = "workflow.instance.admitted"
    elif event_kind == "canceled":
        event_type = "workflow.instance.status_changed"
    elif event_kind in {"approval_settled", "reconciled"}:
        event_type = "workflow.step.reconciled"
    elif state.instance.status is WorkflowInstanceStatus.TERMINAL:
        event_type = "workflow.instance.terminal"
    else:
        event_type = "workflow.step.status_changed"
    envelope: dict[str, object] = {
        "schemaVersion": 1,
        "eventId": str(event_id),
        "eventType": event_type,
        "workflowId": str(state.instance.workflow_id),
        "revisionId": str(state.instance.revision_id),
        "occurredAt": datetime.now(UTC).isoformat(),
    }
    if event_type.startswith("workflow.step"):
        envelope.update({
            "instanceId": str(state.instance.id),
            "stepExecutionId": event_step.execution_id,
            "stepOrdinal": event_step.ordinal,
            "stepKind": event_step.kind.value,
            "stepStatus": event_step.status.value,
            "effectOutcome": event_step.effect_outcome.value,
            "recoveryCapabilities": [x.value for x in event_step.recovery_capabilities],
            "instanceVersion": state.instance.version,
        })
    else:
        envelope.update({
            "instanceId": str(state.instance.id),
            "instanceStatus": state.instance.status.value,
            "terminalOutcome": (
                state.instance.terminal_outcome.value
                if state.instance.terminal_outcome is not None else None
            ),
            "currentStepOrdinal": state.instance.current_ordinal,
            "version": state.instance.version,
        })
    return envelope


def _aggregate_lifecycle_envelope(
    event_type: str, *, issuer: str, subject: str, workflow_id: UUID,
    revision_id: UUID | None, version: int, status: str, resource_id: UUID,
) -> dict[str, object]:
    event_id = uuid4()
    return {
        "schemaVersion": 1, "eventId": str(event_id), "eventType": event_type,
        "workflowId": str(workflow_id),
        "revisionId": str(revision_id) if revision_id is not None else None,
        "version": version, "occurredAt": datetime.now(UTC).isoformat(),
    }


class MemoryWorkflowRepository:
    """Owner-scoped repository with optimistic version and idempotency checks."""

    def __init__(self, metrics: MetadataMetrics | None = None,
                 intents: WorkflowIntentPort | None = None) -> None:
        self._revisions: dict[UUID, WorkflowRevision] = {}
        self._drafts: dict[UUID, WorkflowDraft] = {}
        self._instances: dict[UUID, WorkflowState] = {}
        self._commands: dict[tuple[str, str, str, str], object] = {}
        self._cancel_intents: dict[tuple[str, str, str], WorkflowCancelIntent] = {}
        self._tombstones: dict[UUID, dict[str, object]] = {}
        self._scope_tombstones: set[tuple[UUID, WorkflowDeletionScope]] = set()
        self._definition_tombstones: dict[UUID, dict[str, object]] = {}
        self._revision_tombstones: dict[UUID, dict[str, object]] = {}
        self._events: list[dict[str, object]] = []
        self._event_owners: dict[str, tuple[str, str]] = {}
        self._definitions: dict[UUID, dict[str, object]] = {}
        self._metrics = metrics
        self._intents = intents
        self._lock = asyncio.Lock()

    def _event(self, state: WorkflowState, event_kind: str) -> None:
        event_id = str(UUID(int=secrets.randbits(128)))
        event_step = state.current
        if event_step.status is WorkflowStepStatus.PENDING and event_step.ordinal > 1:
            event_step = state.steps[event_step.ordinal - 2]
        if event_kind == "admitted":
            event_type = "workflow.instance.admitted"
        elif event_kind in {"canceled"}:
            event_type = "workflow.instance.status_changed"
        elif event_kind in {"approval_settled", "reconciled"}:
            event_type = "workflow.step.reconciled"
        elif state.instance.status is WorkflowInstanceStatus.TERMINAL:
            event_type = "workflow.instance.terminal"
        else:
            event_type = "workflow.step.status_changed"
        item: dict[str, object] = {
            "schemaVersion": 1,
            "eventId": event_id,
            "eventType": event_type,
            "workflowId": str(state.instance.workflow_id),
            "revisionId": str(state.instance.revision_id),
            "instanceId": str(state.instance.id),
            "occurredAt": datetime.now(UTC).isoformat(),
            "eventSequence": len(self._events) + 1,
        }
        if event_type.startswith("workflow.step"):
            item.update({
                "stepExecutionId": event_step.execution_id,
                "stepOrdinal": event_step.ordinal,
                "stepKind": event_step.kind.value,
                "stepStatus": event_step.status.value,
                "effectOutcome": event_step.effect_outcome.value,
                "recoveryCapabilities": [x.value for x in event_step.recovery_capabilities],
                "instanceVersion": state.instance.version,
            })
        else:
            item.update({
                "instanceStatus": state.instance.status.value,
                "terminalOutcome": (
                    state.instance.terminal_outcome.value
                    if state.instance.terminal_outcome is not None else None
                ),
                "currentStepOrdinal": state.instance.current_ordinal,
                "version": state.instance.version,
            })
        self._events.append(item)
        self._event_owners[event_id] = (state.instance.issuer, state.instance.subject)

    def _aggregate_event(
        self, event_type: str, *, issuer: str, subject: str, workflow_id: UUID,
        revision_id: UUID | None,
        version: int,
    ) -> None:
        """Persist a closed definition/revision lifecycle variant."""
        item: dict[str, object] = {
            "schemaVersion": 1,
            "eventId": str(uuid4()),
            "eventType": event_type,
            "workflowId": str(workflow_id),
            "revisionId": str(revision_id) if revision_id is not None else None,
            "version": version,
            "occurredAt": datetime.now(UTC).isoformat(),
            "eventSequence": len(self._events) + 1,
        }
        self._events.append(item)
        self._event_owners[str(item["eventId"])] = (str(issuer), str(subject))

    async def get_idempotent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> object | None:
        async with self._lock:
            for (owner_issuer, owner_subject, command, key), value in self._commands.items():
                if (owner_issuer, owner_subject, command, key) == (
                    issuer, subject, operation, idempotency_key
                ):
                    pair = cast(tuple[object, object], value) if isinstance(value, tuple) else None
                    if pair is not None and len(pair) == 2:
                        return pair[1]
            return None

    async def get_idempotency_record(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> WorkflowIdempotencyRecord | None:
        async with self._lock:
            for (owner_issuer, owner_subject, command, key), value in self._commands.items():
                if (owner_issuer, owner_subject, command, key) != (
                    issuer, subject, operation, idempotency_key
                ):
                    continue
                pair = cast(tuple[object, object] | None, value if isinstance(value, tuple) else None)
                if pair is not None and len(pair) == 2:
                    return WorkflowIdempotencyRecord(str(pair[0]), pair[1])
            return None

    async def get_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> WorkflowCancelIntent | None:
        async with self._lock:
            return self._cancel_intents.get((issuer, subject, idempotency_key))

    async def record_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        instance_id: UUID, execution_id: str,
    ) -> None:
        async with self._lock:
            key = (issuer, subject, idempotency_key)
            current = self._cancel_intents.get(key)
            intent = WorkflowCancelIntent(instance_id, execution_id, False)
            if current is not None and current != intent and current.propagated:
                raise WorkflowIdempotencyConflict("idempotency key was reused")
            self._cancel_intents[key] = current or intent

    async def mark_cancel_propagated(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> None:
        async with self._lock:
            current = self._cancel_intents.get((issuer, subject, idempotency_key))
            if current is not None:
                self._cancel_intents[(issuer, subject, idempotency_key)] = replace(
                    current, propagated=True
                )

    async def get_deletion_replay(
        self, issuer: str, subject: str, idempotency_key: str, *,
        workflow_id: UUID | None = None, target_id: UUID | None = None,
        scope: WorkflowDeletionScope | None = None,
        expected_version: int | None = None,
    ) -> WorkflowDeletionReceipt | None:
        async with self._lock:
            value = self._commands.get((issuer, subject, "delete", idempotency_key))
            pair = cast(tuple[object, object] | None, value if isinstance(value, tuple) else None)
            if pair is None or len(pair) != 2:
                return None
            if target_id is not None and scope is not None and expected_version is not None:
                expected = command_fingerprint("delete", {
                    "workflow_id": str(workflow_id) if workflow_id is not None else None,
                    "target_id": str(target_id), "scope": scope.value,
                    "expected_version": expected_version,
                })
                if pair[0] != expected:
                    raise WorkflowIdempotencyConflict("idempotency key was reused")
            response = pair[1]
            if isinstance(response, WorkflowDeletionReceipt):
                return replace(response, replayed=True)
            return None

    async def delete_receipt(
        self, issuer: str, subject: str, workflow_id: UUID, target_id: UUID, *,
        scope: WorkflowDeletionScope, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDeletionReceipt:
        if idempotency_key is not None:
            replay = await self.get_deletion_replay(
                issuer, subject, idempotency_key, workflow_id=workflow_id, target_id=target_id,
                scope=scope, expected_version=expected_version,
            )
            if replay is not None:
                return replay
        deleted = await self.delete(
            issuer, subject, target_id, workflow_id=workflow_id, scope=scope,
            expected_version=expected_version, idempotency_key=idempotency_key,
            trace_context=trace_context,
        )
        if idempotency_key is not None:
            replay = await self.get_deletion_replay(
                issuer, subject, idempotency_key, workflow_id=workflow_id, target_id=target_id,
                scope=scope, expected_version=expected_version,
            )
            if replay is not None:
                return replay
        return WorkflowDeletionReceipt(deleted, datetime.now(UTC))

    def _telemetry(
        self, operation: str, outcome: str, started: float,
        context: WorkflowTraceContext,
        **trace_attributes: str,
    ) -> None:
        metrics = self._metrics
        if metrics is None:
            return
        span_id = new_span_id()
        metrics.increment(
            WORKFLOW_COMPONENT,
            "workflow_repository_outcome",
            operation="workflow.repository",
            outcome=outcome,
            trace_id=context.trace_id,
            **trace_attributes,
        )
        duration = max(0.0, min((monotonic() - started) * 1000.0, 60_000.0))
        observe = getattr(metrics, "observe", None)
        if callable(observe):
            observe(
                WORKFLOW_COMPONENT,
                "workflow_repository_duration_ms",
                duration,
                trace_id=context.trace_id,
                operation="workflow.repository",
                outcome=outcome,
                dependency="postgresql",
                **trace_attributes,
            )
        record_span = getattr(metrics, "record_span", None)
        if callable(record_span):
            record_span(
                WORKFLOW_COMPONENT,
                "workflow.repository",
                duration,
                trace_id=context.trace_id,
                span_id=span_id,
                parent_span_id=context.parent_span_id,
                dependency="workflow",
                outcome=outcome,
                error_class=None if outcome == "ok" else "internal",
                **trace_attributes,
            )

    def _command_telemetry(
        self, outcome: str, started: float, context: WorkflowTraceContext, *,
        error_class: str | None = None,
        **trace_attributes: str,
    ) -> WorkflowTraceContext:
        metrics = self._metrics
        if metrics is None:
            return context
        duration = max(0.0, min((monotonic() - started) * 1000.0, 60_000.0))
        span_id = new_span_id()
        dimensions: dict[str, str] = {
            "operation": "workflow.command", "outcome": outcome,
        }
        if error_class is not None:
            dimensions["error_class"] = error_class
        metrics.increment(
            WORKFLOW_COMPONENT,
            "workflow_command_outcome",
            **dimensions,
            trace_id=context.trace_id,
            **trace_attributes,
        )
        observe = getattr(metrics, "observe", None)
        if callable(observe):
            observe(
                WORKFLOW_COMPONENT,
                "workflow_command_duration_ms",
                duration,
                trace_id=context.trace_id,
                operation="workflow.command",
                outcome=outcome,
                dependency="workflow",
                **trace_attributes,
            )
        record_span = getattr(metrics, "record_span", None)
        if callable(record_span):
            record_span(
                WORKFLOW_COMPONENT,
                "workflow.command",
                duration,
                trace_id=context.trace_id,
                span_id=span_id,
                parent_span_id=context.parent_span_id,
                dependency="workflow",
                outcome=outcome,
                error_class=error_class,
                **trace_attributes,
            )
        return WorkflowTraceContext(context.trace_id, span_id)

    async def create_draft(
        self, revision: WorkflowRevision, *, issuer: str, subject: str,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        if (issuer, subject) != (revision.issuer, revision.subject):
            raise WorkflowNotFound("workflow owner cannot be substituted")
        revision = validate_revision(replace(revision, status=WorkflowRevisionStatus.DRAFT))
        revision = replace(
            revision,
            created_at=revision.created_at or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        fingerprint = command_fingerprint(
            "create_draft",
            {key: value for key, value in revision.as_dict().items()
             if key not in {"id", "workflow_id", "created_at", "updated_at", "published_at", "retired_at"}},
        )
        async with self._lock:
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "create_draft", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowRevision, prior[1])
            self._revisions[revision.id] = revision
            self._definitions[revision.workflow_id] = {
                "issuer": revision.issuer, "subject": revision.subject,
                "status": "active", "version": 1,
                "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
                "retired_at": None,
            }
            self._drafts[revision.id] = WorkflowDraft(
                revision.id, revision.workflow_id, revision.version
            )
            if idempotency_key is not None:
                self._commands[(issuer, subject, "create_draft", idempotency_key)] = (
                    fingerprint, revision
                )
        command_context = self._command_telemetry(
            "accepted", started, context,
            workflow_id=str(revision.workflow_id), revision_id=str(revision.id),
            result_version=str(revision.version),
        )
        self._telemetry(
            "create_draft", "ok", started, command_context,
            workflow_id=str(revision.workflow_id), revision_id=str(revision.id),
        )
        return revision

    async def create_draft_command(
        self, command: WorkflowCreateDraftCommand, *, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowCreateDraftResult:
        prior = await self.get_idempotent(
            command.issuer, command.subject, idempotency_key, operation="create_draft"
        ) if idempotency_key else None
        revision = await self.create_draft(
            _revision_from_create_command(command, idempotency_key),
            issuer=command.issuer, subject=command.subject,
            idempotency_key=idempotency_key, trace_context=trace_context,
        )
        return WorkflowCreateDraftResult(revision, replayed=prior is not None)

    async def publish(
        self, revision_id: UUID, *, expected_version: int,
        issuer: str, subject: str, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        fingerprint = command_fingerprint("publish", {
            "revision_id": str(revision_id), "expected_version": expected_version,
        })
        async with self._lock:
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "publish", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowRevision, prior[1])
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise WorkflowNotFound("workflow revision was not found")
            if (issuer, subject) != (revision.issuer, revision.subject):
                raise WorkflowNotFound("workflow owner cannot be substituted")
            if revision.version != expected_version:
                raise WorkflowVersionConflict("workflow revision version is stale")
            if revision.status is not WorkflowRevisionStatus.DRAFT:
                raise WorkflowTransitionError("only a draft can be published")
            published = replace(
                revision, status=WorkflowRevisionStatus.PUBLISHED, version=revision.version + 1,
                published_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            self._revisions[revision_id] = published
            self._drafts.pop(revision_id, None)
            self._aggregate_event(
                "workflow.revision.published", workflow_id=published.workflow_id,
                revision_id=published.id, version=published.version,
                issuer=published.issuer, subject=published.subject,
            )
            if idempotency_key is not None:
                self._commands[(issuer, subject, "publish", idempotency_key)] = (fingerprint, published)
            command_context = self._command_telemetry(
                "accepted", started, context,
                workflow_id=str(published.workflow_id), revision_id=str(published.id),
                result_version=str(published.version),
            )
            self._telemetry(
                "publish", "ok", started, command_context,
                workflow_id=str(revision.workflow_id), revision_id=str(revision.id),
            )
            return published

    async def get_revision(
        self, issuer: str, subject: str, revision_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        async with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None or (revision.issuer, revision.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            self._telemetry(
                "get_revision", "ok", started, context,
                workflow_id=str(revision.workflow_id), revision_id=str(revision.id),
            )
            return revision

    async def list_definitions(
        self, issuer: str, subject: str, *, limit: int = 50, cursor: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowPage:
        del cursor, trace_context
        _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise WorkflowValidationError("workflow page limit is out of bounds")
        from .public import WorkflowDefinition, WorkflowPage
        async with self._lock:
            items: list[WorkflowDefinition] = []
            for workflow_id, definition in self._definitions.items():
                if (definition["issuer"], definition["subject"]) != (issuer, subject):
                    continue
                revisions = [
                    revision for revision in self._revisions.values()
                    if revision.workflow_id == workflow_id and revision.status is not WorkflowRevisionStatus.DRAFT
                ]
                latest = max(revisions, key=lambda item: item.version, default=None)
                items.append(WorkflowDefinition(
                    workflow_id, latest.display_name if latest else "Workflow",
                    str(definition["status"]), latest.id if latest else None,
                    cast(int, definition["version"]),
                    cast(datetime | None, definition.get("created_at")),
                    cast(datetime | None, definition.get("updated_at")),
                    cast(datetime | None, definition.get("retired_at")),
                ))
            return WorkflowPage(tuple(items[:limit]))

    async def get_definition(self, issuer: str, subject: str, workflow_id: UUID,
                             *, trace_context: WorkflowTraceContext | None = None) -> WorkflowDefinition:
        page = await self.list_definitions(issuer, subject, limit=100, trace_context=trace_context)
        for item in page.items:
            if item.id == workflow_id:
                return item
        raise WorkflowNotFound("workflow definition was not found")

    async def clone_draft(
        self, issuer: str, subject: str, workflow_id: UUID, *, revision_id: UUID | None = None,
        expected_definition_version: int | None = None,
        idempotency_key: str | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        _owner(issuer, subject)
        async with self._lock:
            definition = self._definitions.get(workflow_id)
            if definition is None or (definition["issuer"], definition["subject"]) != (issuer, subject):
                raise WorkflowNotFound("workflow definition was not found")
            fingerprint = command_fingerprint("clone_draft", {
                "workflow_id": str(workflow_id), "revision_id": str(revision_id),
                "expected_definition_version": expected_definition_version,
            })
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "clone_draft", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowRevision, prior[1])
            if expected_definition_version is not None and definition["version"] != expected_definition_version:
                raise WorkflowVersionConflict("workflow definition version is stale")
            source = self._revisions.get(revision_id) if revision_id else max(
                (item for item in self._revisions.values()
                 if item.workflow_id == workflow_id and item.status in {
                     WorkflowRevisionStatus.PUBLISHED, WorkflowRevisionStatus.RETIRED,
                 }),
                key=lambda item: item.version, default=None,
            )
            if source is None or (source.issuer, source.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            if source.workflow_id != workflow_id:
                raise WorkflowNotFound("workflow revision is outside the requested definition")
            if source.status is WorkflowRevisionStatus.DRAFT:
                raise WorkflowTransitionError("workflow clone requires an immutable revision")
            cloned = replace(
                source, id=uuid4(), status=WorkflowRevisionStatus.DRAFT, version=1,
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                published_at=None, retired_at=None,
            )
            self._revisions[cloned.id] = cloned
            self._drafts[cloned.id] = WorkflowDraft(cloned.id, cloned.workflow_id, cloned.version)
            if idempotency_key is not None:
                self._commands[(issuer, subject, "clone_draft", idempotency_key)] = (fingerprint, cloned)
            return cloned

    async def update_draft(self, revision: WorkflowRevision, *, expected_version: int,
                           issuer: str, subject: str, idempotency_key: str | None = None,
                           trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        del trace_context
        _owner(issuer, subject)
        fingerprint = command_fingerprint("update_draft", {
            "revision": revision.as_dict(), "expected_version": expected_version,
        })
        async with self._lock:
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "update_draft", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowRevision, prior[1])
            current = self._revisions.get(revision.id)
            if current is None or (current.issuer, current.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            if current.version != expected_version:
                raise WorkflowVersionConflict("workflow revision version is stale")
            if current.status is not WorkflowRevisionStatus.DRAFT:
                raise WorkflowTransitionError("only a draft can be updated")
            updated = validate_revision(replace(
                revision, version=current.version + 1, status=WorkflowRevisionStatus.DRAFT,
                created_at=current.created_at or datetime.now(UTC), updated_at=datetime.now(UTC),
            ))
            self._revisions[revision.id] = updated
            self._drafts[revision.id] = WorkflowDraft(revision.id, revision.workflow_id, updated.version)
            if idempotency_key is not None:
                self._commands[(issuer, subject, "update_draft", idempotency_key)] = (
                    fingerprint, updated
                )
            return updated

    async def validate_draft(self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
                             trace_context: WorkflowTraceContext | None = None) -> WorkflowValidationResult:
        from .public import WorkflowValidationResult
        revision = await self.get_revision(issuer, subject, revision_id, trace_context=trace_context)
        if revision.version != expected_version:
            raise WorkflowVersionConflict("workflow revision version is stale")
        try:
            validate_revision(revision)
        except WorkflowValidationError:
            return WorkflowValidationResult(
                False, (WorkflowValidationIssue("invalid_value", "/"),), revision.version
            )
        return WorkflowValidationResult(True, (), revision.version)

    async def list_instances(self, issuer: str, subject: str, workflow_id: UUID, *, limit: int = 50,
                             cursor: str | None = None,
                             trace_context: WorkflowTraceContext | None = None) -> tuple[WorkflowInstance, ...]:
        del cursor, trace_context
        _owner(issuer, subject)
        async with self._lock:
            return tuple(
                state.instance for state in self._instances.values()
                if state.instance.workflow_id == workflow_id
                and (state.instance.issuer, state.instance.subject) == (issuer, subject)
            )[:limit]

    async def admit(
        self,
        issuer: str,
        subject: str,
        revision_id: UUID,
        *,
        input_values: Mapping[str, str],
        now: datetime | None = None,
        idempotency_key: str | None = None,
        authority: object | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        fingerprint = command_fingerprint(
            "admit", {"revision_id": str(revision_id), "input": dict(input_values)}
        )
        async with self._lock:
            if idempotency_key is not None:
                key = (issuer, subject, "admit", idempotency_key)
                prior = self._commands.get(key)
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    prior_instance = cast(WorkflowInstance, prior[1])
                    self._command_telemetry(
                        "idempotent", started, context,
                        workflow_id=str(prior_instance.workflow_id),
                        revision_id=str(prior_instance.revision_id),
                        instance_id=str(prior_instance.id),
                        result_version=str(prior_instance.version),
                    )
                    return prior_instance
            revision = self._revisions.get(revision_id)
            if revision is None or (revision.issuer, revision.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            from .public import WorkflowAuthoritySnapshot
            if authority is not None and not isinstance(authority, WorkflowAuthoritySnapshot):
                raise WorkflowValidationError("workflow authority snapshot is invalid")
            instance = admit_instance(
                revision, now=current, input_values=input_values,
                authority=cast(Any, authority),
            )
            instance = replace(instance, trace_context=_trace_context(trace_context))
            from .state import new_state

            self._instances[instance.id] = new_state(revision, instance)
            self._event(self._instances[instance.id], "admitted")
            if self._intents is not None:
                await self._intents.stage_lifecycle(
                    WorkflowLifecycleIntent(self._events[-1])
                )
                await self._intents.stage_wakeup(
                    WorkflowWakeIntent(instance.id, "admitted")
                )
            if idempotency_key is not None:
                self._commands[(issuer, subject, "admit", idempotency_key)] = (
                    fingerprint,
                    instance,
                )
            command_context = self._command_telemetry(
                "accepted", started, context,
                workflow_id=str(instance.workflow_id), revision_id=str(instance.revision_id),
                instance_id=str(instance.id), result_version=str(instance.version),
            )
            self._telemetry(
                "admit", "ok", started, command_context,
                workflow_id=str(instance.workflow_id), revision_id=str(instance.revision_id),
                instance_id=str(instance.id), result_version=str(instance.version),
            )
            return instance

    async def get_state(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        async with self._lock:
            state = self._instances.get(instance_id)
            if state is None or (state.instance.issuer, state.instance.subject) != (
                issuer,
                subject,
            ):
                raise WorkflowNotFound("workflow instance was not found")
            self._telemetry(
                "get_state", "ok", started, context,
                workflow_id=str(state.instance.workflow_id), revision_id=str(state.instance.revision_id),
                instance_id=str(state.instance.id), result_version=str(state.instance.version),
            )
            return state

    async def get_instance(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance:
        return (await self.get_state(
            issuer, subject, instance_id, trace_context=trace_context
        )).instance

    async def authorize_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID,
    ) -> WorkflowInstanceAuthorization:
        _owner(issuer, subject)
        async with self._lock:
            state = self._instances.get(instance_id)
            if state is not None:
                instance = state.instance
                if (instance.issuer, instance.subject, instance.workflow_id) != (
                    issuer, subject, workflow_id
                ):
                    raise WorkflowNotFound("workflow instance was not found")
                return WorkflowInstanceAuthorization(
                    instance.id, instance.workflow_id, instance.revision_id,
                    issuer, subject, instance.version, instance.status,
                    instance.terminal_outcome, False,
                )
            tombstone = self._tombstones.get(instance_id)
            if tombstone is None or (tombstone["issuer"], tombstone["subject"], tombstone["workflow_id"]) != (
                issuer, subject, str(workflow_id)
            ):
                raise WorkflowNotFound("workflow instance was not found")
            return WorkflowInstanceAuthorization(
                instance_id, workflow_id, UUID(str(tombstone["revision_id"])),
                issuer, subject, int(cast(int, tombstone["version"])),
                WorkflowInstanceStatus.TERMINAL,
                WorkflowTerminalOutcome(str(tombstone["outcome"])), True,
            )

    async def get_state_for_worker(self, instance_id: UUID) -> WorkflowState:
        async with self._lock:
            state = self._instances.get(instance_id)
            if state is None:
                raise WorkflowNotFound("workflow instance was not found")
            return state

    async def recovery_scan(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> tuple[UUID, ...]:
        if not 1 <= limit <= 1000:
            raise WorkflowValidationError("workflow recovery scan limit is out of bounds")
        async with self._lock:
            current = now or datetime.now(UTC)
            result: list[UUID] = []
            for state in self._instances.values():
                if state.instance.status is WorkflowInstanceStatus.RUNNING:
                    result.append(state.instance.id)
                elif state.instance.status is WorkflowInstanceStatus.WAITING_FOR_TIME:
                    due_at = state.instance.next_due_at
                    if due_at is not None and due_at <= current:
                        result.append(state.instance.id)
                if len(result) >= limit:
                    break
            return tuple(result)

    async def save_state(
        self, state: WorkflowState, *, expected_version: int,
        issuer: str, subject: str,
        idempotency_key: str | None = None,
        semantic_fingerprint: str | None = None,
        cancel_intent: WorkflowCancelIntent | None = None,
        lifecycle_event_kind: str = "transition",
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        validate_state_budget(state)
        fingerprint = semantic_fingerprint or command_fingerprint("save_state", {
            "instance_id": str(state.instance.id), "expected_version": expected_version,
            "status": state.instance.status.value, "step": state.current.status.value,
        })
        async with self._lock:
            if cancel_intent is not None and idempotency_key is None:
                raise WorkflowValidationError("cancel intent requires idempotency key")
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "save_state", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowState, prior[1])
            if cancel_intent is not None:
                assert idempotency_key is not None
                intent_key = (issuer, subject, idempotency_key)
                current_intent = self._cancel_intents.get(intent_key)
                if current_intent is not None and current_intent != cancel_intent:
                    raise WorkflowIdempotencyConflict("idempotency key was reused")
            current = self._instances.get(state.instance.id)
            if current is None:
                raise WorkflowNotFound("workflow instance was not found")
            owner = (state.instance.issuer, state.instance.subject)
            if (issuer, subject) != owner:
                raise WorkflowNotFound("workflow owner cannot be substituted")
            if (current.instance.issuer, current.instance.subject) != owner:
                raise WorkflowNotFound("workflow owner cannot be substituted")
            if current.instance.version != expected_version:
                raise WorkflowVersionConflict("workflow instance version is stale")
            updated_at = datetime.now(UTC)
            state = WorkflowState(
                replace(
                    state.instance,
                    updated_at=updated_at,
                    terminal_at=(
                        state.instance.terminal_at
                        or updated_at
                        if state.instance.terminal_outcome is not None
                        else state.instance.terminal_at
                    ),
                ),
                state.steps,
            )
            self._instances[state.instance.id] = state
            if idempotency_key is not None:
                self._commands[(issuer, subject, "save_state", idempotency_key)] = (
                    fingerprint, state
                )
            if cancel_intent is not None:
                assert idempotency_key is not None
                self._cancel_intents[(issuer, subject, idempotency_key)] = cancel_intent
            self._event(state, lifecycle_event_kind)
            if self._intents is not None:
                await self._intents.stage_lifecycle(
                    WorkflowLifecycleIntent(self._events[-1])
                )
                if state.instance.status is WorkflowInstanceStatus.QUEUED:
                    await self._intents.stage_wakeup(
                        WorkflowWakeIntent(state.instance.id, "step_settled", state.current.execution_id)
                    )
            self._telemetry(
                "save_state", "ok", started, context,
                workflow_id=str(state.instance.workflow_id), revision_id=str(state.instance.revision_id),
                instance_id=str(state.instance.id), expected_version=str(expected_version),
                result_version=str(state.instance.version),
            )
            return state

    async def events(
        self, issuer: str, subject: str, *, after: UUID | None = None, limit: int = 100,
        trace_context: WorkflowTraceContext | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        page = await self.replay_events(
            issuer, subject, after=str(after) if after is not None else None, limit=limit,
            trace_context=trace_context,
        )
        return page.events

    async def replay_events(
        self, issuer: str, subject: str, *, after: str | None = None,
        limit: int = 100, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowEventPage:
        del trace_context
        _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise WorkflowValidationError("workflow event limit must be between 1 and 100")
        async with self._lock:
            selected: list[dict[str, object]] = []
            for item in self._events:
                if self._event_owners.get(str(item.get("eventId"))) == (issuer, subject):
                    selected.append(item)
                    continue
                raw_instance_id = item.get("instanceId")
                if raw_instance_id is not None:
                    try:
                        instance_id = UUID(str(raw_instance_id))
                    except ValueError:
                        continue
                    if any(
                        state.instance.id == instance_id
                        and (state.instance.issuer, state.instance.subject) == (issuer, subject)
                        for state in self._instances.values()
                    ):
                        selected.append(item)
                    continue
                raw_workflow_id = item.get("workflowId")
                if raw_workflow_id is None:
                    continue
                try:
                    workflow_id = UUID(str(raw_workflow_id))
                except ValueError:
                    continue
                if any(
                    definition_id == workflow_id
                    and (definition["issuer"], definition["subject"]) == (issuer, subject)
                    for definition_id, definition in self._definitions.items()
                ) or any(
                    revision.workflow_id == workflow_id
                    and (revision.issuer, revision.subject) == (issuer, subject)
                    for revision in self._revisions.values()
                ):
                    selected.append(item)
            if after is not None:
                index = next((i for i, item in enumerate(selected)
                              if item["eventId"] == after), None)
                if index is None:
                    foreign = any(item["eventId"] == after for item in self._events)
                    return WorkflowEventPage(
                        (), None,
                        WorkflowEventCursorOutcome.FOREIGN if foreign
                        else WorkflowEventCursorOutcome.UNKNOWN,
                    )
                selected = selected[index + 1:]
            page = tuple(selected[:limit])
            next_cursor = str(page[-1]["eventId"]) if len(page) == limit else None
            return WorkflowEventPage(page, next_cursor)

    async def retire_definition(
        self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> bool:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        fingerprint = command_fingerprint("retire_definition", {
            "workflow_id": str(workflow_id), "expected_version": expected_version,
        })
        async with self._lock:
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "retire_definition", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    response = cast(tuple[object, object], prior)[1]
                    if isinstance(response, WorkflowDeletionReceipt):
                        return response.deleted
                    return cast(bool, response)
            definition = self._definitions.get(workflow_id)
            if definition is None or (definition["issuer"], definition["subject"]) != (issuer, subject):
                raise WorkflowNotFound("workflow definition was not found")
            if definition["version"] != expected_version:
                raise WorkflowVersionConflict("workflow definition version is stale")
            if any(
                state.instance.workflow_id == workflow_id
                and state.instance.status is not WorkflowInstanceStatus.TERMINAL
                for state in self._instances.values()
            ):
                raise WorkflowTransitionError("active workflow instance pins this definition")
            definition["status"] = "retired"
            definition["version"] = cast(int, definition["version"]) + 1
            definition["retired_at"] = datetime.now(UTC)
            definition["updated_at"] = datetime.now(UTC)
            self._aggregate_event(
                "workflow.definition.retired", workflow_id=workflow_id,
                revision_id=None, version=definition["version"],
                issuer=issuer, subject=subject,
            )
            if idempotency_key is not None:
                self._commands[(issuer, subject, "retire_definition", idempotency_key)] = (
                    fingerprint, True
                )
            command_context = self._command_telemetry(
                "accepted", started, context, workflow_id=str(workflow_id),
                result_version=str(definition["version"]),
            )
            self._telemetry(
                "retire_definition", "ok", started, command_context,
                workflow_id=str(workflow_id), result_version=str(definition["version"]),
            )
            return True

    async def retire_revision(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        fingerprint = command_fingerprint("retire_revision", {
            "revision_id": str(revision_id), "expected_version": expected_version,
        })
        async with self._lock:
            if idempotency_key is not None:
                prior = self._commands.get((issuer, subject, "retire_revision", idempotency_key))
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return cast(WorkflowRevision, prior[1])
            revision = self._revisions.get(revision_id)
            if revision is None or (revision.issuer, revision.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            if revision.version != expected_version:
                raise WorkflowVersionConflict("workflow revision version is stale")
            if revision.status is not WorkflowRevisionStatus.PUBLISHED:
                raise WorkflowTransitionError("only a published revision can be retired")
            retired = replace(
                revision, status=WorkflowRevisionStatus.RETIRED, version=revision.version + 1,
                retired_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            self._revisions[revision_id] = retired
            self._aggregate_event(
                "workflow.revision.retired", workflow_id=retired.workflow_id,
                revision_id=retired.id, version=retired.version,
                issuer=retired.issuer, subject=retired.subject,
            )
            if idempotency_key is not None:
                self._commands[(issuer, subject, "retire_revision", idempotency_key)] = (
                    fingerprint, retired
                )
            command_context = self._command_telemetry(
                "accepted", started, context,
                workflow_id=str(retired.workflow_id), revision_id=str(retired.id),
                result_version=str(retired.version),
            )
            self._telemetry(
                "retire_revision", "ok", started, command_context,
                workflow_id=str(retired.workflow_id), revision_id=str(retired.id),
                result_version=str(retired.version),
            )
            return retired

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
        now: datetime | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> bool:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        if expected_version <= 0:
            raise WorkflowValidationError("expected deletion version must be positive")
        fingerprint = command_fingerprint(
            "delete",
            {
                "workflow_id": str(workflow_id) if workflow_id is not None else None,
                "target_id": str(target_id),
                "scope": scope.value,
                "expected_version": expected_version,
            },
        )
        command_key = (issuer, subject, "delete", idempotency_key) if idempotency_key else None
        async with self._lock:
            if command_key is not None:
                prior = self._commands.get(command_key)
                if prior is not None:
                    if not isinstance(prior, tuple) or prior[0] != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    response = cast(tuple[object, object], prior)[1]
                    if isinstance(response, WorkflowDeletionReceipt):
                        return response.deleted
                    return cast(bool, response)

            if scope in (
                WorkflowDeletionScope.TERMINAL_INSTANCE,
                WorkflowDeletionScope.INSTANCE_HISTORY,
                WorkflowDeletionScope.INSTANCE_RESULT,
                WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
            ):
                trace_attributes = {"instance_id": str(target_id)}
            elif scope in (WorkflowDeletionScope.DRAFT, WorkflowDeletionScope.PUBLISHED_REVISION):
                trace_attributes = {"revision_id": str(target_id)}
            elif scope is WorkflowDeletionScope.RETIRED_DEFINITION:
                trace_attributes = {"workflow_id": str(target_id)}
            else:
                trace_attributes = {}

            def deletion_succeeded(
                *, actual_workflow_id: UUID | None = None,
                revision_id: UUID | None = None, instance_id: UUID | None = None,
            ) -> bool:
                command_context = self._command_telemetry(
                    "accepted", started, context, **trace_attributes
                )
                self._telemetry("delete", "ok", started, command_context, **trace_attributes)
                event: dict[str, object] = {
                    "schemaVersion": 1,
                    "eventId": str(uuid4()),
                    "eventType": "workflow.retention.deleted",
                    "workflowId": str(actual_workflow_id or target_id),
                    "revisionId": str(revision_id) if revision_id is not None else None,
                    "instanceId": str(instance_id) if instance_id is not None else None,
                    "deletionScope": {
                        WorkflowDeletionScope.TERMINAL_INSTANCE: "terminal_instance_state",
                        WorkflowDeletionScope.INSTANCE_HISTORY: "terminal_instance_history",
                        WorkflowDeletionScope.INSTANCE_RESULT: "terminal_instance_result",
                        WorkflowDeletionScope.INSTANCE_CHECKPOINTS: "terminal_instance_checkpoints",
                    }.get(scope, scope.value),
                    "tombstoneRetained": True,
                    "version": expected_version,
                    "occurredAt": (now or datetime.now(UTC)).isoformat(),
                    "eventSequence": len(self._events) + 1,
                }
                self._events.append(event)
                self._event_owners[str(event["eventId"])] = (issuer, subject)
                if command_key is not None:
                    self._commands[command_key] = (
                        fingerprint,
                        WorkflowDeletionReceipt(True, now or datetime.now(UTC)),
                    )
                return True

            def deletion_not_found() -> bool:
                if command_key is not None:
                    self._commands[command_key] = (
                        fingerprint,
                        WorkflowDeletionReceipt(False, now or datetime.now(UTC)),
                    )
                return False

            def require_workflow(actual: UUID | str) -> None:
                if workflow_id is not None and str(actual) != str(workflow_id):
                    raise WorkflowNotFound("workflow deletion lineage is not authorized")

            if scope is WorkflowDeletionScope.TERMINAL_INSTANCE:
                if target_id in self._tombstones:
                    tombstone = self._tombstones[target_id]
                    require_workflow(cast(str, tombstone["workflow_id"]))
                    if (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow instance was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                    return deletion_succeeded(
                        actual_workflow_id=UUID(str(tombstone["workflow_id"])),
                        revision_id=UUID(str(tombstone["revision_id"])), instance_id=target_id,
                    )
                state = self._instances.get(target_id)
                if state is None:
                    return deletion_not_found()
                if (state.instance.issuer, state.instance.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow instance was not found")
                require_workflow(state.instance.workflow_id)
                if state.instance.version != expected_version:
                    raise WorkflowVersionConflict("workflow instance version is stale")
                if state.instance.terminal_outcome is None:
                    raise WorkflowTransitionError("active workflow state cannot be deleted")
                self._tombstones[target_id] = {
                    "instance_id": str(target_id),
                    "workflow_id": str(state.instance.workflow_id),
                    "revision_id": str(state.instance.revision_id),
                    "outcome": state.instance.terminal_outcome.value,
                    "version": state.instance.version,
                    "issuer": issuer,
                    "subject": subject,
                }
                self._instances.pop(target_id, None)
                return deletion_succeeded(
                    actual_workflow_id=state.instance.workflow_id,
                    revision_id=state.instance.revision_id, instance_id=target_id,
                )
            if scope in (
                WorkflowDeletionScope.INSTANCE_HISTORY,
                WorkflowDeletionScope.INSTANCE_RESULT,
                WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
            ):
                tombstone = self._tombstones.get(target_id)
                if tombstone is not None and (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                    raise WorkflowNotFound("workflow instance was not found")
                if tombstone is not None:
                    require_workflow(cast(str, tombstone["workflow_id"]))
                if tombstone is not None and tombstone["version"] != expected_version:
                    raise WorkflowVersionConflict("workflow instance version is stale")
                active = self._instances.get(target_id)
                if active is not None:
                    if (active.instance.issuer, active.instance.subject) != (issuer, subject):
                        raise WorkflowNotFound("workflow instance was not found")
                    if active.instance.status is not WorkflowInstanceStatus.TERMINAL:
                        raise WorkflowTransitionError("active workflow state cannot be deleted")
                    require_workflow(active.instance.workflow_id)
                    if active.instance.version != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                elif tombstone is None:
                    return deletion_not_found()
                key = (target_id, scope)
                if key in self._scope_tombstones:
                    assert tombstone is not None
                    return deletion_succeeded(
                        actual_workflow_id=UUID(str(tombstone["workflow_id"])),
                        revision_id=UUID(str(tombstone["revision_id"])), instance_id=target_id,
                    )
                self._scope_tombstones.add(key)
                if tombstone is not None:
                    actual_workflow_id = UUID(str(tombstone["workflow_id"]))
                    actual_revision_id = UUID(str(tombstone["revision_id"]))
                else:
                    assert active is not None
                    actual_workflow_id = active.instance.workflow_id
                    actual_revision_id = active.instance.revision_id
                return deletion_succeeded(
                    actual_workflow_id=actual_workflow_id,
                    revision_id=actual_revision_id,
                    instance_id=target_id,
                )
            if scope is WorkflowDeletionScope.DRAFT:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._revision_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow revision was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow revision version is stale")
                    return deletion_succeeded(
                        actual_workflow_id=UUID(str(tombstone["workflow_id"])),
                        revision_id=target_id,
                    )
                revision = self._revisions.get(target_id)
                if revision is None:
                    return deletion_not_found()
                if (revision.issuer, revision.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow revision was not found")
                require_workflow(revision.workflow_id)
                if revision.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if revision.status is not WorkflowRevisionStatus.DRAFT:
                    raise WorkflowTransitionError("draft scope requires a draft revision")
                self._revisions.pop(target_id, None)
                self._drafts.pop(target_id, None)
                self._revision_tombstones[target_id] = {
                    "revision_id": str(target_id), "workflow_id": str(revision.workflow_id),
                    "issuer": issuer, "subject": subject, "status": "draft",
                    "version": revision.version,
                }
                self._scope_tombstones.add((target_id, scope))
                return deletion_succeeded(actual_workflow_id=revision.workflow_id, revision_id=target_id)
            if scope is WorkflowDeletionScope.PUBLISHED_REVISION:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._revision_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow revision was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow revision version is stale")
                    return deletion_succeeded(
                        actual_workflow_id=UUID(str(tombstone["workflow_id"])),
                        revision_id=target_id,
                    )
                revision = self._revisions.get(target_id)
                if revision is None:
                    return deletion_not_found()
                if (revision.issuer, revision.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow revision was not found")
                require_workflow(revision.workflow_id)
                if revision.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if revision.status is not WorkflowRevisionStatus.RETIRED:
                    raise WorkflowTransitionError("revision must be retired before deletion")
                if any(
                    state.instance.revision_id == target_id
                    and state.instance.status is not WorkflowInstanceStatus.TERMINAL
                    for state in self._instances.values()
                ):
                    raise WorkflowTransitionError("active workflow instance pins this revision")
                self._revisions.pop(target_id, None)
                self._revision_tombstones[target_id] = {
                    "revision_id": str(target_id), "workflow_id": str(revision.workflow_id),
                    "issuer": issuer, "subject": subject, "status": "retired",
                    "version": revision.version,
                }
                self._scope_tombstones.add((target_id, scope))
                return deletion_succeeded(actual_workflow_id=revision.workflow_id, revision_id=target_id)
            if scope is WorkflowDeletionScope.RETIRED_DEFINITION:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._definition_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow definition was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow definition version is stale")
                    return deletion_succeeded(actual_workflow_id=target_id)
                definition = self._definitions.get(target_id)
                if definition is None:
                    return deletion_not_found()
                require_workflow(target_id)
                if (definition["issuer"], definition["subject"]) != (issuer, subject):
                    raise WorkflowNotFound("workflow definition was not found")
                if definition["version"] != expected_version:
                    raise WorkflowVersionConflict("workflow definition version is stale")
                if definition["status"] != "retired":
                    raise WorkflowTransitionError("definition must be retired before deletion")
                if any(
                    state.instance.workflow_id == target_id
                    and state.instance.status is not WorkflowInstanceStatus.TERMINAL
                    for state in self._instances.values()
                ):
                    raise WorkflowTransitionError("active workflow instance pins this definition")
                self._definitions.pop(target_id, None)
                self._definition_tombstones[target_id] = {
                    "workflow_id": str(target_id), "issuer": issuer, "subject": subject,
                    "version": definition["version"],
                }
                self._scope_tombstones.add((target_id, scope))
                return deletion_succeeded(actual_workflow_id=target_id)
            raise WorkflowTransitionError("unknown deletion scope")


def _command_error_class(error: WorkflowError) -> str:
    if isinstance(error, WorkflowNotFound):
        return "not_found"
    if isinstance(error, WorkflowIdempotencyConflict):
        return "idempotency"
    if isinstance(error, WorkflowVersionConflict):
        return "conflict"
    if isinstance(error, WorkflowValidationError):
        return "validation"
    if isinstance(error, WorkflowTransitionError):
        return "conflict"
    return "internal"


def _instrument_command(function: Any) -> Any:
    @wraps(function)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = monotonic()
        context = _trace_context(cast(WorkflowTraceContext | None, kwargs.get("trace_context")))
        kwargs["trace_context"] = context
        try:
            return await function(self, *args, **kwargs)
        except WorkflowError as error:
            self._command_telemetry(
                "error", started, context, error_class=_command_error_class(error)
            )
            raise

    return wrapped


for _command_name in (
    "create_draft", "publish", "admit", "delete", "retire_definition", "retire_revision",
):
    setattr(
        MemoryWorkflowRepository,
        _command_name,
        _instrument_command(getattr(MemoryWorkflowRepository, _command_name)),
    )


class SqlWorkflowRepository:
    """PostgreSQL adapter for the workflow aggregate.

    Every mutating method locks the aggregate row and performs the state,
    history, checkpoint, result, and lifecycle writes in one transaction.
    The adapter only exposes persistence-neutral workflow value objects.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], metrics: MetadataMetrics | None = None,
        intents: WorkflowIntentPort | None = None,
    ) -> None:
        self.sessions = sessions
        self._metrics = metrics
        self._intents = intents

    async def get_idempotent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> object | None:
        from .persistence import WorkflowCommandIdempotencyRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == operation,
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ))
            if row is None:
                return None
            instance_id = row.response.get("instance_id")
            if instance_id is None:
                return row.response
            return await self.get_state(issuer, subject, UUID(str(instance_id)))

    async def get_idempotency_record(
        self, issuer: str, subject: str, idempotency_key: str, *,
        operation: str = "save_state",
    ) -> WorkflowIdempotencyRecord | None:
        from .persistence import WorkflowCommandIdempotencyRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == operation,
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ))
            if row is None:
                return None
            instance_id = row.response.get("instance_id")
            response: object = row.response
            if instance_id is not None:
                response = await self.get_state(issuer, subject, UUID(str(instance_id)))
            return WorkflowIdempotencyRecord(row.fingerprint, response)

    async def get_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> WorkflowCancelIntent | None:
        from .persistence import WorkflowCommandIdempotencyRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == "cancel_propagation",
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ))
            if row is None:
                return None
            return WorkflowCancelIntent(
                UUID(str(row.response["instance_id"])),
                str(row.response["execution_id"]),
                bool(row.response.get("propagated", False)),
            )

    async def record_cancel_intent(
        self, issuer: str, subject: str, idempotency_key: str, *,
        instance_id: UUID, execution_id: str,
    ) -> None:
        from .persistence import WorkflowCommandIdempotencyRow
        fingerprint = command_fingerprint("cancel_propagation", {
            "instance_id": str(instance_id), "execution_id": execution_id,
        })
        async with self.sessions.begin() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == "cancel_propagation",
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ).with_for_update())
            if row is None:
                session.add(WorkflowCommandIdempotencyRow(
                    principal_issuer=issuer, principal_subject=subject,
                    operation="cancel_propagation", idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response={"instance_id": str(instance_id), "execution_id": execution_id,
                              "propagated": False},
                ))
            elif row.fingerprint != fingerprint:
                raise WorkflowIdempotencyConflict("idempotency key was reused")

    async def mark_cancel_propagated(
        self, issuer: str, subject: str, idempotency_key: str
    ) -> None:
        from .persistence import WorkflowCommandIdempotencyRow
        async with self.sessions.begin() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == "cancel_propagation",
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ).with_for_update())
            if row is not None:
                response = dict(row.response)
                response["propagated"] = True
                row.response = response

    async def get_deletion_replay(
        self, issuer: str, subject: str, idempotency_key: str, *,
        workflow_id: UUID | None = None, target_id: UUID | None = None,
        scope: WorkflowDeletionScope | None = None,
        expected_version: int | None = None,
    ) -> WorkflowDeletionReceipt | None:
        from .persistence import WorkflowCommandIdempotencyRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                WorkflowCommandIdempotencyRow.principal_subject == subject,
                WorkflowCommandIdempotencyRow.operation == "delete",
                WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
            ))
            if row is None:
                return None
            if target_id is not None and scope is not None and expected_version is not None:
                expected = command_fingerprint("delete", {
                    "workflow_id": str(workflow_id) if workflow_id is not None else None,
                    "target_id": str(target_id), "scope": scope.value,
                    "expected_version": expected_version,
                })
                if row.fingerprint != expected:
                    raise WorkflowIdempotencyConflict("idempotency key was reused")
            deleted_at = row.response.get("deleted_at")
            if not isinstance(deleted_at, str):
                return None
            return WorkflowDeletionReceipt(
                bool(row.response.get("deleted", False)),
                datetime.fromisoformat(deleted_at), True,
            )

    async def delete_receipt(
        self, issuer: str, subject: str, workflow_id: UUID, target_id: UUID, *,
        scope: WorkflowDeletionScope, expected_version: int,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDeletionReceipt:
        if idempotency_key is not None:
            replay = await self.get_deletion_replay(
                issuer, subject, idempotency_key, workflow_id=workflow_id, target_id=target_id,
                scope=scope, expected_version=expected_version,
            )
            if replay is not None:
                return replay
        deleted = await self.delete(
            issuer, subject, target_id, workflow_id=workflow_id, scope=scope,
            expected_version=expected_version, idempotency_key=idempotency_key,
            trace_context=trace_context,
        )
        if idempotency_key is not None:
            replay = await self.get_deletion_replay(
                issuer, subject, idempotency_key, workflow_id=workflow_id, target_id=target_id,
                scope=scope, expected_version=expected_version,
            )
            if replay is not None:
                return replay
        return WorkflowDeletionReceipt(deleted, datetime.now(UTC))

    @staticmethod
    def _revision_from_row(row: Any) -> WorkflowRevision:
        configuration = cast(dict[str, object], row.configuration)
        raw_inputs = cast(list[dict[str, object]], row.input_schema.get("fields", []))
        inputs = tuple(
            InputField(str(item["name"]), WorkflowValueType(str(item["type"])))
            for item in raw_inputs
        )
        steps: list[WorkflowStep] = []
        for raw in cast(list[dict[str, object]], configuration.get("steps", [])):
            kind = WorkflowStepKind(str(raw["kind"]))
            if kind is WorkflowStepKind.AGENT_RUN:
                input_value = cast(dict[str, object], raw["input"])
                steps.append(AgentRunStep(
                    str(raw["agent_revision_id"]), str(raw["instructions"]),
                    Selector(str(input_value["source"]), str(input_value["field"]),
                             cast(int, input_value["step_ordinal"]) if "step_ordinal" in input_value else None),
                ))
            elif kind is WorkflowStepKind.OWNER_APPROVAL:
                steps.append(OwnerApprovalStep())
            elif kind is WorkflowStepKind.OWNER_NOTIFICATION:
                content = cast(dict[str, object], raw["content"])
                steps.append(OwnerNotificationStep(
                    Selector(str(content["source"]), str(content["field"]),
                             cast(int, content["step_ordinal"]) if "step_ordinal" in content else None)
                ))
            else:
                due_at = cast(dict[str, object], raw["due_at"])
                steps.append(WaitUntilStep(
                    Selector(str(due_at["source"]), str(due_at["field"]),
                             cast(int, due_at["step_ordinal"]) if "step_ordinal" in due_at else None)
                ))
        from .public import WorkflowRevisionStatus
        created_at = getattr(row, "created_at", None)
        updated_at = getattr(row, "updated_at", None)
        return WorkflowRevision(
            row.id, row.workflow_id, row.principal_issuer, row.principal_subject,
            row.display_name, inputs, tuple(steps), version=row.version,
            status=WorkflowRevisionStatus(row.status), household_id=getattr(row, "household_id", None),
            workspace_id=getattr(row, "workspace_id", None),
            revision_number=row.revision_number,
            published_at=row.published_at.astimezone(UTC) if row.published_at else None,
            retired_at=row.retired_at.astimezone(UTC) if row.retired_at else None,
            created_at=created_at.astimezone(UTC) if created_at is not None else None,
            updated_at=updated_at.astimezone(UTC) if updated_at is not None else None,
        )

    @staticmethod
    def _configuration(revision: WorkflowRevision) -> dict[str, object]:
        return {"steps": [step.as_dict() for step in revision.steps]}

    @staticmethod
    def _input_schema(revision: WorkflowRevision) -> dict[str, object]:
        return {"fields": [{"name": item.name, "type": item.value_type.value} for item in revision.input_fields]}

    async def _record_state_rows(
        self, session: AsyncSession, state: WorkflowState, *,
        lifecycle_event_kind: str = "transition",
    ) -> None:
        from .persistence import (
            WorkflowCheckpointRow,
            WorkflowHistoryRow,
            WorkflowInstanceStepRow,
            WorkflowLifecycleEventRow,
            WorkflowResultRow,
        )
        for step in state.steps:
            row = await session.scalar(
                select(WorkflowInstanceStepRow).where(
                    WorkflowInstanceStepRow.instance_id == state.instance.id,
                    WorkflowInstanceStepRow.ordinal == step.ordinal,
                ).with_for_update()
            )
            if row is None:
                row = WorkflowInstanceStepRow(
                    id=uuid4(), instance_id=state.instance.id,
                    principal_issuer=state.instance.issuer, principal_subject=state.instance.subject,
                    ordinal=step.ordinal, kind=step.kind.value,
                )
                session.add(row)
            row.status = step.status.value
            row.effect_outcome = step.effect_outcome.value
            row.execution_id = step.execution_id
            row.output = cast(dict[str, object] | None, step.output)
            row.recovery_capabilities = [item.value for item in step.recovery_capabilities]
            row.version = step.version
        current = state.current
        previous_checkpoint = await session.scalar(select(WorkflowCheckpointRow).where(
            WorkflowCheckpointRow.instance_id == state.instance.id,
            WorkflowCheckpointRow.checkpoint_status == "current",
        ).with_for_update())
        if previous_checkpoint is None:
            checkpoint = WorkflowCheckpointRow(
                id=uuid4(), instance_id=state.instance.id,
                principal_issuer=state.instance.issuer, principal_subject=state.instance.subject,
                ordinal=current.ordinal, checkpoint_status="current",
                payload={}, version=state.instance.version,
            )
            session.add(checkpoint)
        else:
            checkpoint = previous_checkpoint
            checkpoint.ordinal = current.ordinal
            checkpoint.version = state.instance.version
        authority = state.instance.authority
        checkpoint.payload = {
                "instance_id": str(state.instance.id), "revision_id": str(state.instance.revision_id),
                "current_ordinal": state.instance.current_ordinal,
                "instance_status": state.instance.status.value,
                "instance_version": state.instance.version,
                "step_status": current.status.value, "step_version": current.version,
                "deadline": state.instance.deadline.isoformat(), "execution_id": current.execution_id,
                "next_due_at": state.instance.next_due_at.isoformat()
                if state.instance.next_due_at is not None else None,
                "authority": {
                    "issuer": authority.issuer, "subject": authority.subject,
                    "household_id": authority.household_id, "workspace_id": authority.workspace_id,
                    "agent_revision_id": authority.agent_revision_id,
                    "grants": list(authority.grants),
                } if authority is not None else None,
            }
        session.add(WorkflowHistoryRow(
            id=uuid4(), instance_id=state.instance.id, principal_issuer=state.instance.issuer,
            principal_subject=state.instance.subject, ordinal=current.ordinal,
            event_kind="transition", status=state.instance.status.value,
            version=state.instance.version, event_metadata={"step_status": current.status.value},
        ))
        lifecycle_event_id = uuid4()
        event_state = state
        # A completed non-terminal step advances the aggregate cursor before
        # persistence.  Project the completed step for the lifecycle envelope
        # so consumers never receive a null execution identity for the newly
        # pending step.
        if current.status is WorkflowStepStatus.PENDING and current.ordinal > 1:
            event_state = WorkflowState(
                replace(state.instance, current_ordinal=current.ordinal - 1), state.steps
            )
        lifecycle_event = _lifecycle_envelope(
            event_state, lifecycle_event_kind, lifecycle_event_id
        )
        lifecycle_kind = cast(str, lifecycle_event["eventType"])
        session.add(WorkflowLifecycleEventRow(
            id=lifecycle_event_id, instance_id=state.instance.id,
            principal_issuer=state.instance.issuer, principal_subject=state.instance.subject,
            ordinal=current.ordinal, event_kind=lifecycle_kind, status=state.instance.status.value,
            version=state.instance.version,
            event_metadata=lifecycle_event,
        ))
        if self._intents is not None:
            await self._intents.stage_lifecycle(
                WorkflowLifecycleIntent(lifecycle_event), transaction=session
            )
            if state.instance.status is WorkflowInstanceStatus.QUEUED:
                await self._intents.stage_wakeup(
                    WorkflowWakeIntent(state.instance.id, "step_settled", current.execution_id),
                    transaction=session,
                )
        if state.instance.terminal_outcome is not None:
            existing = await session.scalar(
                select(WorkflowResultRow).where(WorkflowResultRow.instance_id == state.instance.id)
            )
            if existing is None:
                session.add(WorkflowResultRow(
                    id=uuid4(), instance_id=state.instance.id,
                    principal_issuer=state.instance.issuer, principal_subject=state.instance.subject,
                    outcome=state.instance.terminal_outcome.value,
                    payload={"instance_id": str(state.instance.id), "version": state.instance.version},
                ))

    async def create_draft(self, revision: WorkflowRevision, *, issuer: str, subject: str,
                           idempotency_key: str | None = None,
                           trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        _owner(issuer, subject)
        if (issuer, subject) != (revision.issuer, revision.subject):
            raise WorkflowNotFound("workflow owner cannot be substituted")
        revision = validate_revision(replace(revision, status=WorkflowRevisionStatus.DRAFT))
        revision = replace(
            revision,
            created_at=revision.created_at or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowDefinitionRow,
            WorkflowDraftRow,
            WorkflowRevisionRow,
        )
        fingerprint = command_fingerprint(
            "create_draft",
            {key: value for key, value in revision.as_dict().items()
             if key not in {"id", "workflow_id", "created_at", "updated_at", "published_at", "retired_at"}},
        )
        replay_revision_id: UUID | None = None
        async with self.sessions.begin() as session:
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "create_draft",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay_revision_id = UUID(str(prior.response["revision_id"]))
            if replay_revision_id is not None:
                pass
            else:
                definition = WorkflowDefinitionRow(
                    id=revision.workflow_id, principal_issuer=issuer, principal_subject=subject,
                    display_name=revision.display_name, status="active", version=1,
                )
                session.add(definition)
                session.add(WorkflowRevisionRow(
                    id=revision.id, workflow_id=revision.workflow_id, draft_id=revision.id,
                    principal_issuer=issuer, principal_subject=subject,
                    display_name=revision.display_name, status="draft", revision_number=revision.version,
                    version=revision.version, input_schema=self._input_schema(revision),
                    configuration=self._configuration(revision),
                ))
                session.add(WorkflowDraftRow(
                    id=revision.id, workflow_id=revision.workflow_id,
                    principal_issuer=issuer, principal_subject=subject, version=revision.version,
                    configuration=self._configuration(revision),
                ))
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="create_draft", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"revision_id": str(revision.id)},
                    ))
        if replay_revision_id is not None:
            return await self.get_revision(issuer, subject, replay_revision_id, trace_context=trace_context)
        return await self.get_revision(issuer, subject, revision.id, trace_context=trace_context)

    async def create_draft_command(
        self, command: WorkflowCreateDraftCommand, *, idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowCreateDraftResult:
        prior = await self.get_idempotent(
            command.issuer, command.subject, idempotency_key, operation="create_draft"
        ) if idempotency_key else None
        revision = await self.create_draft(
            _revision_from_create_command(command, idempotency_key),
            issuer=command.issuer, subject=command.subject,
            idempotency_key=idempotency_key, trace_context=trace_context,
        )
        return WorkflowCreateDraftResult(revision, replayed=prior is not None)

    async def get_revision(self, issuer: str, subject: str, revision_id: UUID,
                           *, trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        _owner(issuer, subject)
        from .persistence import WorkflowRevisionRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowRevisionRow).where(
                WorkflowRevisionRow.id == revision_id,
                WorkflowRevisionRow.principal_issuer == issuer,
                WorkflowRevisionRow.principal_subject == subject,
            ))
            if row is None:
                raise WorkflowNotFound("workflow revision was not found")
            return self._revision_from_row(row)

    async def list_definitions(
        self, issuer: str, subject: str, *, limit: int = 50, cursor: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowPage:
        del trace_context
        _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise WorkflowValidationError("workflow page limit is out of bounds")
        from .persistence import WorkflowDefinitionRow, WorkflowRevisionRow
        async with self.sessions() as session:
            query = select(WorkflowDefinitionRow).where(
                WorkflowDefinitionRow.principal_issuer == issuer,
                WorkflowDefinitionRow.principal_subject == subject,
            ).order_by(WorkflowDefinitionRow.created_at, WorkflowDefinitionRow.id).limit(limit)
            if cursor:
                try:
                    cursor_id = UUID(cursor)
                    query = query.where(WorkflowDefinitionRow.id > cursor_id)
                except ValueError as exc:
                    raise WorkflowValidationError("workflow cursor is invalid") from exc
            definitions = (await session.scalars(query)).all()
            items: list[WorkflowDefinition] = []
            for definition in definitions:
                revision = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.workflow_id == definition.id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                    WorkflowRevisionRow.status != WorkflowRevisionStatus.DRAFT.value,
                ).order_by(WorkflowRevisionRow.version.desc()).limit(1))
                items.append(WorkflowDefinition(
                    definition.id,
                    revision.display_name if revision else definition.display_name,
                    definition.status, revision.id if revision else None,
                    definition.version, definition.created_at.astimezone(UTC),
                    definition.updated_at.astimezone(UTC),
                    None,
                ))
            next_cursor = str(items[-1].id) if len(items) == limit else None
            return WorkflowPage(tuple(items), next_cursor)

    async def get_definition(
        self, issuer: str, subject: str, workflow_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowDefinition:
        page = await self.list_definitions(issuer, subject, limit=100, trace_context=trace_context)
        for item in page.items:
            if item.id == workflow_id:
                return item
        raise WorkflowNotFound("workflow definition was not found")

    async def clone_draft(
        self, issuer: str, subject: str, workflow_id: UUID, *, revision_id: UUID | None = None,
        expected_definition_version: int | None = None,
        idempotency_key: str | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowDefinitionRow,
            WorkflowDraftRow,
            WorkflowRevisionRow,
        )
        fingerprint = command_fingerprint("clone_draft", {
            "workflow_id": str(workflow_id), "revision_id": str(revision_id),
            "expected_definition_version": expected_definition_version,
        })
        replay_revision_id: UUID | None = None
        cloned: WorkflowRevision | None = None
        async with self.sessions.begin() as session:
            definition = await session.scalar(select(WorkflowDefinitionRow).where(
                WorkflowDefinitionRow.id == workflow_id,
                WorkflowDefinitionRow.principal_issuer == issuer,
                WorkflowDefinitionRow.principal_subject == subject,
            ).with_for_update())
            if definition is None:
                raise WorkflowNotFound("workflow definition was not found")
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "clone_draft",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay_revision_id = UUID(str(prior.response["revision_id"]))
            if (replay_revision_id is None and expected_definition_version is not None
                    and definition.version != expected_definition_version):
                raise WorkflowVersionConflict("workflow definition version is stale")
            if replay_revision_id is not None:
                pass
            else:
                query = select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.workflow_id == workflow_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                    WorkflowRevisionRow.status.in_(
                        (WorkflowRevisionStatus.PUBLISHED.value, WorkflowRevisionStatus.RETIRED.value)
                    ),
                )
                if revision_id is not None:
                    query = query.where(WorkflowRevisionRow.id == revision_id)
                source = await session.scalar(query.order_by(WorkflowRevisionRow.version.desc()).limit(1).with_for_update())
                if source is None:
                    raise WorkflowNotFound("workflow revision was not found")
                source_revision = self._revision_from_row(source)
                new_id = uuid4()
                max_number = await session.scalar(select(WorkflowRevisionRow.revision_number).where(
                    WorkflowRevisionRow.workflow_id == workflow_id
                ).order_by(WorkflowRevisionRow.revision_number.desc()).limit(1)) or 0
                cloned = replace(source_revision, id=new_id, status=WorkflowRevisionStatus.DRAFT,
                                 version=1, revision_number=int(max_number) + 1,
                                 published_at=None, retired_at=None)
                session.add(WorkflowRevisionRow(
                    id=cloned.id, workflow_id=cloned.workflow_id, draft_id=cloned.id,
                    principal_issuer=issuer, principal_subject=subject,
                    display_name=cloned.display_name, status=cloned.status.value,
                    revision_number=cloned.revision_number, version=cloned.version,
                    input_schema=self._input_schema(cloned), configuration=self._configuration(cloned),
                ))
                session.add(WorkflowDraftRow(
                    id=cloned.id, workflow_id=cloned.workflow_id, principal_issuer=issuer,
                    principal_subject=subject, version=cloned.version,
                    configuration=self._configuration(cloned),
                ))
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="clone_draft", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"revision_id": str(cloned.id)},
                    ))
        if replay_revision_id is not None:
            return await self.get_revision(issuer, subject, replay_revision_id, trace_context=trace_context)
        if cloned is None:
            raise WorkflowTransitionError("workflow clone did not produce a revision")
        return await self.get_revision(issuer, subject, cloned.id, trace_context=trace_context)

    async def update_draft(
        self, revision: WorkflowRevision, *, expected_version: int, issuer: str, subject: str,
        idempotency_key: str | None = None,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowDraftRow,
            WorkflowRevisionRow,
        )
        fingerprint = command_fingerprint("update_draft", {
            "revision": revision.as_dict(), "expected_version": expected_version,
        })
        updated = validate_revision(replace(revision, status=WorkflowRevisionStatus.DRAFT,
                                             version=expected_version + 1))
        async with self.sessions.begin() as session:
            replay_revision_id: UUID | None = None
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "update_draft",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay_revision_id = UUID(str(prior.response["revision_id"]))
            if replay_revision_id is not None:
                pass
            else:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == revision.id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    raise WorkflowNotFound("workflow revision was not found")
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if row.status != WorkflowRevisionStatus.DRAFT.value:
                    raise WorkflowTransitionError("only a draft can be updated")
                row.display_name = updated.display_name
                row.version = updated.version
                row.updated_at = datetime.now(UTC)
                row.input_schema = self._input_schema(updated)
                row.configuration = self._configuration(updated)
                draft = await session.scalar(select(WorkflowDraftRow).where(WorkflowDraftRow.id == revision.id).with_for_update())
                if draft is not None:
                    draft.version = updated.version
                    draft.configuration = self._configuration(updated)
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="update_draft", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"revision_id": str(revision.id)},
                    ))
        if replay_revision_id is not None:
            return await self.get_revision(issuer, subject, replay_revision_id, trace_context=trace_context)
        return updated

    async def validate_draft(
        self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowValidationResult:
        revision = await self.get_revision(issuer, subject, revision_id, trace_context=trace_context)
        if revision.version != expected_version:
            raise WorkflowVersionConflict("workflow revision version is stale")
        try:
            validate_revision(revision)
        except WorkflowValidationError:
            return WorkflowValidationResult(
                False, (WorkflowValidationIssue("invalid_value", "/"),), revision.version
            )
        return WorkflowValidationResult(True, (), revision.version)

    async def list_instances(
        self, issuer: str, subject: str, workflow_id: UUID, *, limit: int = 50,
        cursor: str | None = None, trace_context: WorkflowTraceContext | None = None,
    ) -> tuple[WorkflowInstance, ...]:
        del cursor, trace_context
        _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise WorkflowValidationError("workflow page limit is out of bounds")
        from .persistence import WorkflowInstanceRow
        async with self.sessions() as session:
            rows_result = await session.scalars(select(WorkflowInstanceRow).where(
                WorkflowInstanceRow.workflow_id == workflow_id,
                WorkflowInstanceRow.principal_issuer == issuer,
                WorkflowInstanceRow.principal_subject == subject,
            ).order_by(WorkflowInstanceRow.updated_at.desc(), WorkflowInstanceRow.id).limit(limit))
            rows = list(rows_result.all())
        instances: list[WorkflowInstance] = []
        for row in rows:
            instances.append(await self.get_instance(issuer, subject, row.id))
        return tuple(instances)

    async def publish(self, revision_id: UUID, *, expected_version: int, issuer: str, subject: str,
                      idempotency_key: str | None = None,
                      trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowDraftRow,
            WorkflowLifecycleEventRow,
            WorkflowRevisionRow,
        )
        replay_revision_id: UUID | None = None
        async with self.sessions.begin() as session:
            fingerprint = command_fingerprint("publish", {
                "revision_id": str(revision_id), "expected_version": expected_version,
            })
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "publish",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay_revision_id = UUID(str(prior.response["revision_id"]))
            if replay_revision_id is None:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == revision_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    raise WorkflowNotFound("workflow revision was not found")
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if row.status != WorkflowRevisionStatus.DRAFT.value:
                    raise WorkflowTransitionError("only a draft can be published")
                row.status = WorkflowRevisionStatus.PUBLISHED.value
                row.version += 1
                row.published_at = datetime.now(UTC)
                row.updated_at = datetime.now(UTC)
                await session.execute(sql_delete(WorkflowDraftRow).where(WorkflowDraftRow.id == revision_id))
                event = _aggregate_lifecycle_envelope(
                    "workflow.revision.published", issuer=issuer, subject=subject,
                    workflow_id=row.workflow_id, revision_id=row.id, version=row.version,
                    status=row.status, resource_id=row.id,
                )
                session.add(WorkflowLifecycleEventRow(
                    id=UUID(str(event["eventId"])), instance_id=None,
                    principal_issuer=issuer, principal_subject=subject, ordinal=None,
                    event_kind="workflow.revision.published", status=row.status,
                    version=row.version, event_metadata=event,
                ))
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="publish", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"revision_id": str(revision_id)},
                    ))
        if replay_revision_id is not None:
            return await self.get_revision(issuer, subject, replay_revision_id, trace_context=trace_context)
        return await self.get_revision(issuer, subject, revision_id, trace_context=trace_context)

    async def admit(self, issuer: str, subject: str, revision_id: UUID, *, input_values: Mapping[str, str],
                    now: datetime | None = None, idempotency_key: str | None = None,
                    authority: object | None = None,
                    trace_context: WorkflowTraceContext | None = None) -> WorkflowInstance:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowInstanceRow,
            WorkflowInstanceStepRow,
            WorkflowRevisionRow,
        )
        fingerprint = command_fingerprint("admit", {"revision_id": str(revision_id), "input": dict(input_values)})
        replay_instance_id: UUID | None = None
        result: WorkflowInstance | None = None
        async with self.sessions.begin() as session:
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "admit",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay_instance_id = UUID(str(prior.response["instance_id"]))
            if replay_instance_id is None:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == revision_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    raise WorkflowNotFound("workflow revision was not found")
                revision = self._revision_from_row(row)
                from .public import WorkflowAuthoritySnapshot
                from .state import new_state
                if authority is not None and not isinstance(authority, WorkflowAuthoritySnapshot):
                    raise WorkflowValidationError("workflow authority snapshot is invalid")
                instance = admit_instance(
                    revision, now=now or datetime.now(UTC), input_values=input_values,
                    authority=cast(Any, authority),
                )
                instance = replace(instance, trace_context=_trace_context(trace_context))
                session.add(WorkflowInstanceRow(
                    id=instance.id, workflow_id=instance.workflow_id, revision_id=instance.revision_id,
                    principal_issuer=issuer, principal_subject=subject,
                    household_id=revision.household_id, workspace_id=revision.workspace_id,
                    current_ordinal=1, status=instance.status.value, version=1,
                    admitted_at=instance.admitted_at, deadline=instance.deadline,
                    next_due_at=instance.next_due_at,
                    input_values=dict(instance.input_values), replay_count=0,
                ))
                for ordinal, step in enumerate(revision.steps, 1):
                    session.add(WorkflowInstanceStepRow(
                        id=uuid4(), instance_id=instance.id, principal_issuer=issuer,
                        principal_subject=subject, ordinal=ordinal, kind=step.kind.value,
                    ))
                from .persistence import WorkflowCheckpointRow, WorkflowLifecycleEventRow
                pinned = cast(Any, authority)
                session.add(WorkflowCheckpointRow(
                    id=uuid4(), instance_id=instance.id, principal_issuer=issuer,
                    principal_subject=subject, ordinal=1, checkpoint_status="current",
                    payload={
                        "instance_id": str(instance.id), "revision_id": str(instance.revision_id),
                        "current_ordinal": 1, "instance_status": instance.status.value,
                        "instance_version": instance.version, "step_status": "pending",
                        "step_version": 1, "deadline": instance.deadline.isoformat(),
                        "execution_id": None,
                        "trace_context": {
                            "trace_id": instance.trace_context.trace_id,
                            "parent_span_id": instance.trace_context.parent_span_id,
                        } if instance.trace_context is not None else None,
                        "authority": {
                            "issuer": pinned.issuer, "subject": pinned.subject,
                            "household_id": pinned.household_id, "workspace_id": pinned.workspace_id,
                            "agent_revision_id": pinned.agent_revision_id,
                            "grants": list(pinned.grants),
                        } if pinned is not None else None,
                    }, version=instance.version,
                ))
                admitted_event_id = uuid4()
                admitted_event = _lifecycle_envelope(
                    new_state(revision, instance), "admitted", admitted_event_id
                )
                session.add(WorkflowLifecycleEventRow(
                    id=admitted_event_id, instance_id=instance.id,
                    principal_issuer=issuer, principal_subject=subject,
                    ordinal=1, event_kind="admitted", status=instance.status.value,
                    version=instance.version,
                    event_metadata=admitted_event,
                ))
                if self._intents is not None:
                    await self._intents.stage_lifecycle(
                        WorkflowLifecycleIntent(admitted_event), transaction=session
                    )
                    await self._intents.stage_wakeup(
                        WorkflowWakeIntent(instance.id, "admitted"), transaction=session
                    )
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject, operation="admit",
                        idempotency_key=idempotency_key, fingerprint=fingerprint,
                        response={"instance_id": str(instance.id)},
                    ))
                result = instance
        if replay_instance_id is not None:
            return await self.get_instance(issuer, subject, replay_instance_id)
        if result is None:
            raise WorkflowTransitionError("workflow admission did not produce an instance")
        instance = result
        return instance

    async def get_state(self, issuer: str, subject: str, instance_id: UUID,
                        *, trace_context: WorkflowTraceContext | None = None) -> WorkflowState:
        instance = await self.get_instance(issuer, subject, instance_id, trace_context=trace_context)
        from .persistence import WorkflowInstanceStepRow
        async with self.sessions() as session:
            rows = (await session.scalars(select(WorkflowInstanceStepRow).where(
                WorkflowInstanceStepRow.instance_id == instance_id,
                WorkflowInstanceStepRow.principal_issuer == issuer,
                WorkflowInstanceStepRow.principal_subject == subject,
            ).order_by(WorkflowInstanceStepRow.ordinal))).all()
        steps = tuple(WorkflowStepState(
            instance_id, row.ordinal, WorkflowStepKind(row.kind), WorkflowStepStatus(row.status),
            WorkflowEffectOutcome(row.effect_outcome), row.version, row.execution_id,
            row.output, tuple(RecoveryCapability(item) for item in (row.recovery_capabilities or [])),
        ) for row in rows)
        return WorkflowState(instance, steps)

    async def get_state_for_worker(self, instance_id: UUID) -> WorkflowState:
        from .persistence import WorkflowInstanceRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowInstanceRow).where(WorkflowInstanceRow.id == instance_id))
            if row is None:
                raise WorkflowNotFound("workflow instance was not found")
            return await self.get_state(row.principal_issuer, row.principal_subject, instance_id)

    async def recovery_scan(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> tuple[UUID, ...]:
        current = now or datetime.now(UTC)
        if not 1 <= limit <= 1000:
            raise WorkflowValidationError("workflow recovery scan limit is out of bounds")
        from .persistence import WorkflowInstanceRow
        async with self.sessions() as session:
            running = (await session.scalars(select(WorkflowInstanceRow.id).where(
                WorkflowInstanceRow.status == WorkflowInstanceStatus.RUNNING.value,
            ).order_by(WorkflowInstanceRow.updated_at, WorkflowInstanceRow.id).limit(limit))).all()
            remaining = max(0, limit - len(running))
            waiting: list[UUID] = []
            if remaining:
                waiting = list((await session.scalars(select(WorkflowInstanceRow.id).where(
                    WorkflowInstanceRow.status == WorkflowInstanceStatus.WAITING_FOR_TIME.value,
                    WorkflowInstanceRow.next_due_at.is_not(None),
                    WorkflowInstanceRow.next_due_at <= current,
                ).order_by(WorkflowInstanceRow.next_due_at, WorkflowInstanceRow.id).limit(remaining))).all())
        return tuple([*running, *waiting])

    async def save_state(self, state: WorkflowState, *, expected_version: int, issuer: str, subject: str,
                         idempotency_key: str | None = None,
                         semantic_fingerprint: str | None = None,
                         cancel_intent: WorkflowCancelIntent | None = None,
                         lifecycle_event_kind: str = "transition",
                         trace_context: WorkflowTraceContext | None = None) -> WorkflowState:
        _owner(issuer, subject)
        if (state.instance.issuer, state.instance.subject) != (issuer, subject):
            raise WorkflowNotFound("workflow owner cannot be substituted")
        validate_state_budget(state)
        updated_at = datetime.now(UTC)
        state = WorkflowState(
            replace(
                state.instance,
                updated_at=updated_at,
                terminal_at=(
                    state.instance.terminal_at
                    or updated_at
                    if state.instance.terminal_outcome is not None
                    else state.instance.terminal_at
                ),
            ),
            state.steps,
        )
        from .persistence import WorkflowCommandIdempotencyRow, WorkflowInstanceRow
        fingerprint = semantic_fingerprint or command_fingerprint("save_state", {
            "instance_id": str(state.instance.id), "expected_version": expected_version,
            "status": state.instance.status.value, "step": state.current.status.value,
        })
        replay = False
        async with self.sessions.begin() as session:
            if cancel_intent is not None and idempotency_key is None:
                raise WorkflowValidationError("cancel intent requires idempotency key")
            row = await session.scalar(select(WorkflowInstanceRow).where(
                WorkflowInstanceRow.id == state.instance.id,
                WorkflowInstanceRow.principal_issuer == issuer,
                WorkflowInstanceRow.principal_subject == subject,
            ).with_for_update())
            if row is None:
                raise WorkflowNotFound("workflow instance was not found")
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "save_state",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay = True
            if cancel_intent is not None:
                assert idempotency_key is not None
                cancel_fingerprint = command_fingerprint("cancel_propagation", {
                    "instance_id": str(cancel_intent.instance_id),
                    "execution_id": cancel_intent.execution_id,
                })
                cancel_prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "cancel_propagation",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if cancel_prior is not None and cancel_prior.fingerprint != cancel_fingerprint:
                    raise WorkflowIdempotencyConflict("idempotency key was reused")
                if cancel_prior is None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="cancel_propagation", idempotency_key=idempotency_key,
                        fingerprint=cancel_fingerprint,
                        response={
                            "instance_id": str(cancel_intent.instance_id),
                            "execution_id": cancel_intent.execution_id,
                            "propagated": cancel_intent.propagated,
                        },
                    ))
            if not replay and row.version != expected_version:
                raise WorkflowVersionConflict("workflow instance version is stale")
            if replay:
                pass
            else:
                row.current_ordinal = state.instance.current_ordinal
                row.status = state.instance.status.value
                row.terminal_outcome = state.instance.terminal_outcome.value if state.instance.terminal_outcome else None
                row.next_due_at = state.instance.next_due_at
                if state.instance.terminal_outcome is not None and row.terminal_at is None:
                    row.terminal_at = state.instance.terminal_at or updated_at
                row.version = state.instance.version
                row.replay_count = state.instance.replay_count
                row.updated_at = updated_at
                await self._record_state_rows(session, state, lifecycle_event_kind=lifecycle_event_kind)
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="save_state", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"instance_id": str(state.instance.id)},
                    ))
        return await self.get_state(issuer, subject, state.instance.id) if replay else state

    async def retire_revision(self, issuer: str, subject: str, revision_id: UUID, *, expected_version: int,
                              idempotency_key: str | None = None,
                              trace_context: WorkflowTraceContext | None = None) -> WorkflowRevision:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowLifecycleEventRow,
            WorkflowRevisionRow,
        )
        fingerprint = command_fingerprint("retire_revision", {
            "revision_id": str(revision_id), "expected_version": expected_version,
        })
        replay = False
        async with self.sessions.begin() as session:
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "retire_revision",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    replay = True
            if replay:
                pass
            else:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == revision_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    raise WorkflowNotFound("workflow revision was not found")
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if row.status != WorkflowRevisionStatus.PUBLISHED.value:
                    raise WorkflowTransitionError("only a published revision can be retired")
                row.status = WorkflowRevisionStatus.RETIRED.value
                row.version += 1
                row.retired_at = datetime.now(UTC)
                row.updated_at = datetime.now(UTC)
                event = _aggregate_lifecycle_envelope(
                    "workflow.revision.retired", issuer=issuer, subject=subject,
                    workflow_id=row.workflow_id, revision_id=row.id, version=row.version,
                    status=row.status, resource_id=row.id,
                )
                session.add(WorkflowLifecycleEventRow(
                    id=UUID(str(event["eventId"])), instance_id=None,
                    principal_issuer=issuer, principal_subject=subject, ordinal=None,
                    event_kind="workflow.revision.retired", status=row.status,
                    version=row.version, event_metadata=event,
                ))
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="retire_revision", idempotency_key=idempotency_key,
                        fingerprint=fingerprint, response={"revision_id": str(revision_id)},
                    ))
        return await self.get_revision(issuer, subject, revision_id, trace_context=trace_context)

    async def retire_definition(self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
                                idempotency_key: str | None = None,
                                trace_context: WorkflowTraceContext | None = None) -> bool:
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCommandIdempotencyRow,
            WorkflowDefinitionRow,
            WorkflowInstanceRow,
            WorkflowLifecycleEventRow,
        )
        fingerprint = command_fingerprint("retire_definition", {
            "workflow_id": str(workflow_id), "expected_version": expected_version,
        })
        async with self.sessions.begin() as session:
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "retire_definition",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return True
            row = await session.scalar(select(WorkflowDefinitionRow).where(
                WorkflowDefinitionRow.id == workflow_id,
                WorkflowDefinitionRow.principal_issuer == issuer,
                WorkflowDefinitionRow.principal_subject == subject,
            ).with_for_update())
            if row is None:
                raise WorkflowNotFound("workflow definition was not found")
            if row.version != expected_version:
                raise WorkflowVersionConflict("workflow definition version is stale")
            active = await session.scalar(select(WorkflowInstanceRow.id).where(
                WorkflowInstanceRow.workflow_id == workflow_id,
                WorkflowInstanceRow.principal_issuer == issuer,
                WorkflowInstanceRow.principal_subject == subject,
                WorkflowInstanceRow.status != WorkflowInstanceStatus.TERMINAL.value,
            ).limit(1))
            if active is not None:
                raise WorkflowTransitionError("active workflow instance pins this definition")
            row.status = "retired"
            row.version += 1
            row.updated_at = datetime.now(UTC)
            event = _aggregate_lifecycle_envelope(
                "workflow.definition.retired", issuer=issuer, subject=subject,
                workflow_id=row.id, revision_id=None, version=row.version,
                status=row.status, resource_id=row.id,
            )
            session.add(WorkflowLifecycleEventRow(
                id=UUID(str(event["eventId"])), instance_id=None,
                principal_issuer=issuer, principal_subject=subject, ordinal=None,
                event_kind="workflow.definition.retired", status=row.status,
                version=row.version, event_metadata=event,
            ))
            if idempotency_key is not None:
                session.add(WorkflowCommandIdempotencyRow(
                    principal_issuer=issuer, principal_subject=subject,
                    operation="retire_definition", idempotency_key=idempotency_key,
                    fingerprint=fingerprint, response={"workflow_id": str(workflow_id)},
                ))
        return True

    async def events(self, issuer: str, subject: str, *, after: UUID | None = None,
                     limit: int = 100, trace_context: WorkflowTraceContext | None = None
                     ) -> tuple[Mapping[str, object], ...]:
        page = await self.replay_events(
            issuer, subject, after=str(after) if after is not None else None,
            limit=limit, trace_context=trace_context,
        )
        return page.events

    async def replay_events(
        self, issuer: str, subject: str, *, after: str | None = None,
        limit: int = 100, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowEventPage:
        del trace_context
        _owner(issuer, subject)
        if not 1 <= limit <= 100:
            raise WorkflowValidationError("workflow event limit must be between 1 and 100")
        from .persistence import WorkflowLifecycleEventRow
        async with self.sessions() as session:
            cursor = None
            outcome = WorkflowEventCursorOutcome.OK
            if after is not None:
                try:
                    cursor_id = UUID(after)
                except ValueError:
                    return WorkflowEventPage((), None, WorkflowEventCursorOutcome.UNKNOWN)
                cursor = await session.scalar(select(WorkflowLifecycleEventRow).where(
                    WorkflowLifecycleEventRow.id == cursor_id,
                    WorkflowLifecycleEventRow.principal_issuer == issuer,
                    WorkflowLifecycleEventRow.principal_subject == subject,
                ))
                if cursor is None:
                    foreign = await session.scalar(select(WorkflowLifecycleEventRow.id).where(
                        WorkflowLifecycleEventRow.id == cursor_id,
                    ))
                    return WorkflowEventPage(
                        (), None,
                        WorkflowEventCursorOutcome.FOREIGN if foreign is not None
                        else WorkflowEventCursorOutcome.UNKNOWN,
                    )
            query = select(WorkflowLifecycleEventRow).where(
                WorkflowLifecycleEventRow.principal_issuer == issuer,
                WorkflowLifecycleEventRow.principal_subject == subject,
            ).order_by(
                WorkflowLifecycleEventRow.event_sequence,
                WorkflowLifecycleEventRow.occurred_at,
                WorkflowLifecycleEventRow.id,
            ).limit(limit)
            if cursor is not None:
                if cursor.event_sequence is not None:
                    query = query.where(
                        WorkflowLifecycleEventRow.event_sequence > cursor.event_sequence
                    )
                else:
                    query = query.where(
                        (WorkflowLifecycleEventRow.occurred_at > cursor.occurred_at)
                        | ((WorkflowLifecycleEventRow.occurred_at == cursor.occurred_at)
                           & (WorkflowLifecycleEventRow.id > cursor.id))
                    )
            rows = (await session.scalars(query)).all()
        items: list[Mapping[str, object]] = []
        for row in rows:
            envelope = dict(cast(Mapping[str, object], row.event_metadata))
            envelope.setdefault("eventId", str(row.id))
            envelope["eventSequence"] = row.event_sequence
            envelope.setdefault("occurredAt", row.occurred_at.astimezone(UTC).isoformat())
            items.append(envelope)
        next_cursor = str(items[-1]["eventId"]) if len(items) == limit else None
        return WorkflowEventPage(tuple(items), next_cursor, outcome)

    async def delete(self, issuer: str, subject: str, target_id: UUID, *,
                     workflow_id: UUID | None = None, scope: WorkflowDeletionScope,
                     expected_version: int, idempotency_key: str | None = None,
                     trace_context: WorkflowTraceContext | None = None) -> bool:
        """Apply one explicit non-cascading retention scope."""
        _owner(issuer, subject)
        from .persistence import (
            WorkflowCheckpointRow,
            WorkflowCommandIdempotencyRow,
            WorkflowDefinitionRow,
            WorkflowDefinitionTombstoneRow,
            WorkflowDraftRow,
            WorkflowHistoryRow,
            WorkflowInstanceRow,
            WorkflowInstanceStepRow,
            WorkflowLifecycleEventRow,
            WorkflowResultRow,
            WorkflowRevisionRow,
            WorkflowRevisionTombstoneRow,
            WorkflowTombstoneRow,
        )
        fingerprint = command_fingerprint("delete", {
            "workflow_id": str(workflow_id) if workflow_id is not None else None,
            "target_id": str(target_id), "scope": scope.value,
            "expected_version": expected_version,
        })
        async with self.sessions.begin() as session:
            prior = None
            if idempotency_key is not None:
                prior = await session.scalar(select(WorkflowCommandIdempotencyRow).where(
                    WorkflowCommandIdempotencyRow.principal_issuer == issuer,
                    WorkflowCommandIdempotencyRow.principal_subject == subject,
                    WorkflowCommandIdempotencyRow.operation == "delete",
                    WorkflowCommandIdempotencyRow.idempotency_key == idempotency_key,
                ).with_for_update())
                if prior is not None:
                    if prior.fingerprint != fingerprint:
                        raise WorkflowIdempotencyConflict("idempotency key was reused")
                    return bool(prior.response.get("deleted", False))

            deleted_at = datetime.now(UTC)

            def require_workflow(actual: UUID) -> None:
                if workflow_id is not None and actual != workflow_id:
                    raise WorkflowNotFound("workflow deletion lineage is not authorized")

            def record(
                result: bool, *, workflow_id: UUID | None = None,
                revision_id: UUID | None = None, instance_id: UUID | None = None,
            ) -> bool:
                if idempotency_key is not None:
                    session.add(WorkflowCommandIdempotencyRow(
                        principal_issuer=issuer, principal_subject=subject,
                        operation="delete", idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        response={"deleted": result, "deleted_at": deleted_at.isoformat()},
                    ))
                if result:
                    event_id = uuid4()
                    deletion_scope = {
                        WorkflowDeletionScope.TERMINAL_INSTANCE: "terminal_instance_state",
                        WorkflowDeletionScope.INSTANCE_HISTORY: "terminal_instance_history",
                        WorkflowDeletionScope.INSTANCE_RESULT: "terminal_instance_result",
                        WorkflowDeletionScope.INSTANCE_CHECKPOINTS: "terminal_instance_checkpoints",
                    }.get(scope, scope.value)
                    event = {
                        "schemaVersion": 1, "eventId": str(event_id),
                        "eventType": "workflow.retention.deleted",
                        "workflowId": str(workflow_id or target_id),
                        "revisionId": str(revision_id) if revision_id else None,
                        "instanceId": str(instance_id) if instance_id else None,
                        "deletionScope": deletion_scope, "tombstoneRetained": True,
                        "version": expected_version,
                        "occurredAt": datetime.now(UTC).isoformat(),
                    }
                    session.add(WorkflowLifecycleEventRow(
                        id=event_id, instance_id=None,
                        principal_issuer=issuer, principal_subject=subject, ordinal=None,
                        event_kind="workflow.retention.deleted", status="deleted",
                        version=expected_version, event_metadata=event,
                    ))
                return result

            if scope is WorkflowDeletionScope.DRAFT:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == target_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    return record(False)
                require_workflow(row.workflow_id)
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if row.status != WorkflowRevisionStatus.DRAFT.value:
                    raise WorkflowTransitionError("draft scope requires a draft revision")
                session.add(WorkflowRevisionTombstoneRow(
                    revision_id=row.id, workflow_id=row.workflow_id,
                    principal_issuer=issuer, principal_subject=subject,
                    revision_number=row.revision_number, version=row.version,
                ))
                await session.execute(sql_delete(WorkflowDraftRow).where(WorkflowDraftRow.id == target_id))
                await session.execute(sql_delete(WorkflowRevisionRow).where(WorkflowRevisionRow.id == target_id))
                return record(True, workflow_id=row.workflow_id, revision_id=row.id)
            if scope is WorkflowDeletionScope.TERMINAL_INSTANCE:
                instance = await session.scalar(select(WorkflowInstanceRow).where(
                    WorkflowInstanceRow.id == target_id,
                    WorkflowInstanceRow.principal_issuer == issuer,
                    WorkflowInstanceRow.principal_subject == subject,
                ).with_for_update())
                if instance is None:
                    tombstone = await session.scalar(select(WorkflowTombstoneRow).where(
                        WorkflowTombstoneRow.instance_id == target_id,
                        WorkflowTombstoneRow.principal_issuer == issuer,
                        WorkflowTombstoneRow.principal_subject == subject,
                    ))
                    if tombstone is None:
                        return record(False)
                    require_workflow(tombstone.workflow_id)
                    if tombstone.version != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                    return record(True, workflow_id=tombstone.workflow_id,
                                  revision_id=tombstone.revision_id, instance_id=tombstone.instance_id)
                if instance.version != expected_version:
                    raise WorkflowVersionConflict("workflow instance version is stale")
                require_workflow(instance.workflow_id)
                if instance.terminal_outcome is None:
                    raise WorkflowTransitionError("active workflow state cannot be deleted")
                session.add(WorkflowTombstoneRow(
                    instance_id=instance.id, principal_issuer=issuer, principal_subject=subject,
                    workflow_id=instance.workflow_id, revision_id=instance.revision_id,
                    terminal_outcome=instance.terminal_outcome, version=instance.version,
                ))
                await session.execute(sql_delete(WorkflowInstanceRow).where(WorkflowInstanceRow.id == target_id))
                return record(True, workflow_id=instance.workflow_id,
                              revision_id=instance.revision_id, instance_id=instance.id)
            if scope in (WorkflowDeletionScope.INSTANCE_HISTORY, WorkflowDeletionScope.INSTANCE_RESULT,
                         WorkflowDeletionScope.INSTANCE_CHECKPOINTS):
                tombstone = await session.scalar(select(WorkflowTombstoneRow).where(
                    WorkflowTombstoneRow.instance_id == target_id,
                    WorkflowTombstoneRow.principal_issuer == issuer,
                    WorkflowTombstoneRow.principal_subject == subject,
                ))
                live_instance = None
                if tombstone is None:
                    live_instance = await session.scalar(select(WorkflowInstanceRow).where(
                        WorkflowInstanceRow.id == target_id,
                        WorkflowInstanceRow.principal_issuer == issuer,
                        WorkflowInstanceRow.principal_subject == subject,
                    ).with_for_update())
                    if live_instance is None:
                        return record(False)
                    if live_instance.terminal_outcome is None:
                        raise WorkflowTransitionError("active workflow state cannot be deleted")
                    require_workflow(live_instance.workflow_id)
                    if live_instance.version != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                else:
                    require_workflow(tombstone.workflow_id)
                    if tombstone.version != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                table = {
                    WorkflowDeletionScope.INSTANCE_HISTORY: WorkflowHistoryRow,
                    WorkflowDeletionScope.INSTANCE_RESULT: WorkflowResultRow,
                    WorkflowDeletionScope.INSTANCE_CHECKPOINTS: WorkflowCheckpointRow,
                }[scope]
                await session.execute(sql_delete(table).where(table.instance_id == target_id))
                if scope is WorkflowDeletionScope.INSTANCE_RESULT:
                    await session.execute(sql_update(WorkflowInstanceStepRow).where(
                        WorkflowInstanceStepRow.instance_id == target_id
                    ).values(output=None))
                if live_instance is not None:
                    actual_workflow_id, revision_id = live_instance.workflow_id, live_instance.revision_id
                else:
                    assert tombstone is not None
                    actual_workflow_id, revision_id = tombstone.workflow_id, tombstone.revision_id
                return record(True, workflow_id=actual_workflow_id, revision_id=revision_id,
                              instance_id=target_id)
            if scope is WorkflowDeletionScope.PUBLISHED_REVISION:
                row = await session.scalar(select(WorkflowRevisionRow).where(
                    WorkflowRevisionRow.id == target_id,
                    WorkflowRevisionRow.principal_issuer == issuer,
                    WorkflowRevisionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    return record(False)
                require_workflow(row.workflow_id)
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow revision version is stale")
                if row.status != WorkflowRevisionStatus.RETIRED.value:
                    raise WorkflowTransitionError("revision must be retired before deletion")
                session.add(WorkflowRevisionTombstoneRow(
                    revision_id=row.id, workflow_id=row.workflow_id,
                    principal_issuer=issuer, principal_subject=subject,
                    revision_number=row.revision_number, version=row.version,
                ))
                await session.execute(sql_delete(WorkflowRevisionRow).where(WorkflowRevisionRow.id == target_id))
                return record(True, workflow_id=row.workflow_id, revision_id=row.id)
            if scope is WorkflowDeletionScope.RETIRED_DEFINITION:
                row = await session.scalar(select(WorkflowDefinitionRow).where(
                    WorkflowDefinitionRow.id == target_id,
                    WorkflowDefinitionRow.principal_issuer == issuer,
                    WorkflowDefinitionRow.principal_subject == subject,
                ).with_for_update())
                if row is None:
                    return record(False)
                require_workflow(row.id)
                if row.version != expected_version:
                    raise WorkflowVersionConflict("workflow definition version is stale")
                if row.status != "retired":
                    raise WorkflowTransitionError("definition must be retired before deletion")
                session.add(WorkflowDefinitionTombstoneRow(
                    workflow_id=row.id, principal_issuer=issuer, principal_subject=subject,
                    version=row.version,
                ))
                await session.execute(sql_delete(WorkflowDefinitionRow).where(WorkflowDefinitionRow.id == target_id))
                return record(True, workflow_id=row.id)
            raise WorkflowTransitionError("unknown deletion scope")

    async def get_instance(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        from .persistence import WorkflowCheckpointRow, WorkflowInstanceRow

        async with self.sessions() as session:
            row = await session.scalar(
                select(WorkflowInstanceRow).where(
                    WorkflowInstanceRow.id == instance_id,
                    WorkflowInstanceRow.principal_issuer == issuer,
                    WorkflowInstanceRow.principal_subject == subject,
                )
            )
            if row is None:
                raise WorkflowNotFound("workflow instance was not found")
            if row.terminal_outcome is None and row.status == "terminal":
                raise WorkflowTransitionError("terminal workflow is missing an outcome")
            checkpoint = await session.scalar(select(WorkflowCheckpointRow).where(
                WorkflowCheckpointRow.instance_id == instance_id,
                WorkflowCheckpointRow.principal_issuer == issuer,
                WorkflowCheckpointRow.principal_subject == subject,
                WorkflowCheckpointRow.checkpoint_status == "current",
            ).order_by(WorkflowCheckpointRow.version.desc()).limit(1))
            authority = None
            checkpoint_payload = cast(
                Mapping[str, object],
                getattr(checkpoint, "payload", {}) if checkpoint is not None else {},
            )
            if isinstance(checkpoint_payload, dict) and isinstance(checkpoint_payload.get("authority"), dict):
                from .public import WorkflowAuthoritySnapshot
                raw = cast(dict[str, object], checkpoint_payload["authority"])
                authority = WorkflowAuthoritySnapshot(
                    issuer=str(raw.get("issuer", issuer)),
                    subject=str(raw.get("subject", subject)),
                    household_id=cast(str | None, raw.get("household_id")),
                    workspace_id=cast(str | None, raw.get("workspace_id")),
                    agent_revision_id=cast(str | None, raw.get("agent_revision_id")),
                    grants=tuple(str(item) for item in cast(list[object], raw.get("grants", []))),
                )
            trace_context = None
            if isinstance(checkpoint_payload, dict) and isinstance(checkpoint_payload.get("trace_context"), dict):
                from .public import WorkflowTraceContext
                raw_trace = cast(dict[str, object], checkpoint_payload["trace_context"])
                trace_context = WorkflowTraceContext(
                    str(raw_trace.get("trace_id", "")),
                    cast(str | None, raw_trace.get("parent_span_id")),
                )
            from .public import WorkflowInstanceStatus, WorkflowTerminalOutcome

            result = WorkflowInstance(
                row.id,
                row.workflow_id,
                row.revision_id,
                row.principal_issuer,
                row.principal_subject,
                row.current_ordinal,
                WorkflowInstanceStatus(row.status),
                row.version,
                row.admitted_at.astimezone(UTC),
                row.deadline.astimezone(UTC),
                row.input_values,
                WorkflowTerminalOutcome(row.terminal_outcome) if row.terminal_outcome else None,
                row.replay_count,
                authority=authority,
                updated_at=getattr(row, "updated_at", None),
                terminal_at=getattr(row, "terminal_at", None),
                trace_context=trace_context,
                next_due_at=getattr(row, "next_due_at", None),
            )
            if self._metrics is not None:
                duration = max(0.0, min((monotonic() - started) * 1000.0, 60_000.0))
                self._metrics.increment(
                    WORKFLOW_COMPONENT,
                    "workflow_repository_outcome",
                    operation="workflow.repository",
                    outcome="ok",
                    trace_id=context.trace_id,
                    workflow_id=str(result.workflow_id),
                    revision_id=str(result.revision_id),
                    instance_id=str(result.id),
                    result_version=str(result.version),
                )
                span_id = new_span_id()
                self._metrics.record_span(
                    WORKFLOW_COMPONENT,
                    "workflow.repository",
                    duration,
                    trace_id=context.trace_id,
                    span_id=span_id,
                    parent_span_id=context.parent_span_id,
                    dependency="postgresql",
                    outcome="ok",
                    workflow_id=str(result.workflow_id),
                    revision_id=str(result.revision_id),
                    instance_id=str(result.id),
                    result_version=str(result.version),
                )
            return result

    async def authorize_instance_in_workflow(
        self, issuer: str, subject: str, workflow_id: UUID, instance_id: UUID,
    ) -> WorkflowInstanceAuthorization:
        _owner(issuer, subject)
        from .persistence import WorkflowInstanceRow, WorkflowTombstoneRow
        async with self.sessions() as session:
            row = await session.scalar(select(WorkflowInstanceRow).where(
                WorkflowInstanceRow.id == instance_id,
                WorkflowInstanceRow.principal_issuer == issuer,
                WorkflowInstanceRow.principal_subject == subject,
            ))
            if row is not None:
                if row.workflow_id != workflow_id:
                    raise WorkflowNotFound("workflow instance was not found")
                return WorkflowInstanceAuthorization(
                    row.id, row.workflow_id, row.revision_id, issuer, subject,
                    row.version, WorkflowInstanceStatus(row.status),
                    WorkflowTerminalOutcome(row.terminal_outcome)
                    if row.terminal_outcome else None, False,
                )
            tombstone = await session.scalar(select(WorkflowTombstoneRow).where(
                WorkflowTombstoneRow.instance_id == instance_id,
                WorkflowTombstoneRow.principal_issuer == issuer,
                WorkflowTombstoneRow.principal_subject == subject,
            ))
            if tombstone is None or tombstone.workflow_id != workflow_id:
                raise WorkflowNotFound("workflow instance was not found")
            return WorkflowInstanceAuthorization(
                instance_id, tombstone.workflow_id, tombstone.revision_id,
                issuer, subject, tombstone.version, WorkflowInstanceStatus.TERMINAL,
                WorkflowTerminalOutcome(tombstone.terminal_outcome), True,
            )


__all__ = ["MemoryWorkflowRepository", "SqlWorkflowRepository", "command_fingerprint"]
