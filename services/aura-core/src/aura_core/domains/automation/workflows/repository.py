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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.platform.telemetry import MetadataMetrics, new_span_id

from .models import WorkflowDraft
from .public import (
    WORKFLOW_COMPONENT,
    WorkflowDeletionScope,
    WorkflowError,
    WorkflowIdempotencyConflict,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowNotFound,
    WorkflowRevision,
    WorkflowRevisionStatus,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowVersionConflict,
    admit_instance,
    canonical_json,
    validate_revision,
)
from .state import WorkflowState, validate_state_budget


def command_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json({"operation": operation, "payload": payload})).hexdigest()


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject:
        raise WorkflowValidationError("authenticated workflow owner is required")
    return issuer, subject


def _trace_context(context: WorkflowTraceContext | None) -> WorkflowTraceContext:
    if context is None or not context.trace_id or set(context.trace_id) == {"0"}:
        return WorkflowTraceContext(secrets.token_hex(16))
    return context


class MemoryWorkflowRepository:
    """Owner-scoped repository with optimistic version and idempotency checks."""

    def __init__(self, metrics: MetadataMetrics | None = None) -> None:
        self._revisions: dict[UUID, WorkflowRevision] = {}
        self._drafts: dict[UUID, WorkflowDraft] = {}
        self._instances: dict[UUID, WorkflowState] = {}
        self._commands: dict[tuple[str, str, str, str], object] = {}
        self._tombstones: dict[UUID, dict[str, object]] = {}
        self._scope_tombstones: set[tuple[UUID, WorkflowDeletionScope]] = set()
        self._definition_tombstones: dict[UUID, dict[str, object]] = {}
        self._revision_tombstones: dict[UUID, dict[str, object]] = {}
        self._definitions: dict[UUID, dict[str, object]] = {}
        self._metrics = metrics
        self._lock = asyncio.Lock()

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
        metrics.record_span(
            WORKFLOW_COMPONENT,
            "workflow.repository",
            duration,
            trace_id=context.trace_id,
            span_id=span_id,
            parent_span_id=context.parent_span_id,
            dependency="application_validation",
            outcome=outcome,
            error_class=None if outcome == "ok" else outcome,
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
        metrics.record_span(
            WORKFLOW_COMPONENT,
            "workflow.command",
            duration,
            trace_id=context.trace_id,
            span_id=span_id,
            parent_span_id=context.parent_span_id,
            dependency="application_validation",
            outcome=outcome,
            error_class=error_class,
            **trace_attributes,
        )
        return WorkflowTraceContext(context.trace_id, span_id)

    async def create_draft(
        self, revision: WorkflowRevision, *, issuer: str, subject: str,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        if (issuer, subject) != (revision.issuer, revision.subject):
            raise WorkflowNotFound("workflow owner cannot be substituted")
        revision = validate_revision(replace(revision, status=WorkflowRevisionStatus.DRAFT))
        async with self._lock:
            self._revisions[revision.id] = revision
            self._definitions[revision.workflow_id] = {
                "issuer": revision.issuer, "subject": revision.subject,
                "status": "active", "version": 1,
            }
            self._drafts[revision.id] = WorkflowDraft(
                revision.id, revision.workflow_id, revision.version
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

    async def publish(
        self, revision_id: UUID, *, expected_version: int,
        issuer: str, subject: str,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        async with self._lock:
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
                revision, status=WorkflowRevisionStatus.PUBLISHED, version=revision.version + 1
            )
            self._revisions[revision_id] = published
            self._drafts.pop(revision_id, None)
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

    async def admit(
        self,
        issuer: str,
        subject: str,
        revision_id: UUID,
        *,
        input_values: Mapping[str, str],
        now: datetime | None = None,
        idempotency_key: str | None = None,
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
            instance = admit_instance(revision, now=current, input_values=input_values)
            from .state import new_state

            self._instances[instance.id] = new_state(revision, instance)
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

    async def save_state(
        self, state: WorkflowState, *, expected_version: int,
        issuer: str, subject: str,
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowState:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        validate_state_budget(state)
        async with self._lock:
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
            self._instances[state.instance.id] = state
            self._telemetry(
                "save_state", "ok", started, context,
                workflow_id=str(state.instance.workflow_id), revision_id=str(state.instance.revision_id),
                instance_id=str(state.instance.id), expected_version=str(expected_version),
                result_version=str(state.instance.version),
            )
            return state

    async def retire_definition(
        self, issuer: str, subject: str, workflow_id: UUID, *, expected_version: int,
        trace_context: WorkflowTraceContext | None = None,
    ) -> bool:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        async with self._lock:
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
        trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowRevision:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        async with self._lock:
            revision = self._revisions.get(revision_id)
            if revision is None or (revision.issuer, revision.subject) != (issuer, subject):
                raise WorkflowNotFound("workflow revision was not found")
            if revision.version != expected_version:
                raise WorkflowVersionConflict("workflow revision version is stale")
            if revision.status is not WorkflowRevisionStatus.PUBLISHED:
                raise WorkflowTransitionError("only a published revision can be retired")
            retired = replace(
                revision, status=WorkflowRevisionStatus.RETIRED, version=revision.version + 1
            )
            self._revisions[revision_id] = retired
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
                    return cast(bool, prior[1])

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

            def deletion_succeeded() -> bool:
                command_context = self._command_telemetry(
                    "accepted", started, context, **trace_attributes
                )
                self._telemetry("delete", "ok", started, command_context, **trace_attributes)
                if command_key is not None:
                    self._commands[command_key] = (fingerprint, True)
                return True

            def deletion_not_found() -> bool:
                if command_key is not None:
                    self._commands[command_key] = (fingerprint, False)
                return False

            if scope is WorkflowDeletionScope.TERMINAL_INSTANCE:
                if target_id in self._tombstones:
                    tombstone = self._tombstones[target_id]
                    if (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow instance was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                    return deletion_succeeded()
                state = self._instances.get(target_id)
                if state is None:
                    return deletion_not_found()
                if (state.instance.issuer, state.instance.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow instance was not found")
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
                return deletion_succeeded()
            if scope in (
                WorkflowDeletionScope.INSTANCE_HISTORY,
                WorkflowDeletionScope.INSTANCE_RESULT,
                WorkflowDeletionScope.INSTANCE_CHECKPOINTS,
            ):
                tombstone = self._tombstones.get(target_id)
                if tombstone is not None and (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                    raise WorkflowNotFound("workflow instance was not found")
                if tombstone is not None and tombstone["version"] != expected_version:
                    raise WorkflowVersionConflict("workflow instance version is stale")
                active = self._instances.get(target_id)
                if active is not None:
                    if (active.instance.issuer, active.instance.subject) != (issuer, subject):
                        raise WorkflowNotFound("workflow instance was not found")
                    if active.instance.status is not WorkflowInstanceStatus.TERMINAL:
                        raise WorkflowTransitionError("active workflow state cannot be deleted")
                    if active.instance.version != expected_version:
                        raise WorkflowVersionConflict("workflow instance version is stale")
                elif tombstone is None:
                    return deletion_not_found()
                key = (target_id, scope)
                if key in self._scope_tombstones:
                    return deletion_succeeded()
                self._scope_tombstones.add(key)
                return deletion_succeeded()
            if scope is WorkflowDeletionScope.DRAFT:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._revision_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow revision was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow revision version is stale")
                    return deletion_succeeded()
                revision = self._revisions.get(target_id)
                if revision is None:
                    return deletion_not_found()
                if (revision.issuer, revision.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow revision was not found")
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
                return deletion_succeeded()
            if scope is WorkflowDeletionScope.PUBLISHED_REVISION:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._revision_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow revision was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow revision version is stale")
                    return deletion_succeeded()
                revision = self._revisions.get(target_id)
                if revision is None:
                    return deletion_not_found()
                if (revision.issuer, revision.subject) != (issuer, subject):
                    raise WorkflowNotFound("workflow revision was not found")
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
                return deletion_succeeded()
            if scope is WorkflowDeletionScope.RETIRED_DEFINITION:
                if (target_id, scope) in self._scope_tombstones:
                    tombstone = self._definition_tombstones.get(target_id)
                    if tombstone is None or (tombstone["issuer"], tombstone["subject"]) != (issuer, subject):
                        raise WorkflowNotFound("workflow definition was not found")
                    if tombstone["version"] != expected_version:
                        raise WorkflowVersionConflict("workflow definition version is stale")
                    return deletion_succeeded()
                definition = self._definitions.get(target_id)
                if definition is None:
                    return deletion_not_found()
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
                return deletion_succeeded()
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
    """Minimal SQL adapter seam; application transitions stay in ``state.py``."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], metrics: MetadataMetrics | None = None
    ) -> None:
        self.sessions = sessions
        self._metrics = metrics

    async def get_instance(
        self, issuer: str, subject: str, instance_id: UUID,
        *, trace_context: WorkflowTraceContext | None = None,
    ) -> WorkflowInstance:
        started = monotonic()
        context = _trace_context(trace_context)
        _owner(issuer, subject)
        from .persistence import WorkflowInstanceRow

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


__all__ = ["MemoryWorkflowRepository", "SqlWorkflowRepository", "command_fingerprint"]
