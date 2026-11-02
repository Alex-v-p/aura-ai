"""Application service for live reconciliation and rebuild of Task Center."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, TypeGuard, cast
from uuid import UUID, uuid4

from aura_core.domains.execution.tasks.contracts import (
    TASK_PROJECTION_COMPONENT,
    TaskCommand,
    TaskCommandReceipt,
    TaskCommandState,
    TaskListFilter,
    TaskNormalizedStatus,
    TaskProjectionRebuildState,
    TaskProjectionRow,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceInvalidation,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.ports import TaskSourcePort
from aura_core.domains.execution.tasks.repository import TaskProjectionRepository
from aura_core.platform.telemetry import new_span_id

_OUTCOMES = frozenset(
    {"success", "rejected", "partial", "canceled", "timeout", "failed", "unknown"}
)
_ERROR_CLASSES = frozenset(
    {
        "validation_failed",
        "snapshot_unavailable",
        "version_gap",
        "timeout",
        "storage_unavailable",
    }
)


class TaskProjectionNotFound(LookupError):
    pass


class TaskProjectionConflict(RuntimeError):
    pass


def _is_snapshot(value: object) -> TypeGuard[TaskProjectionSnapshot]:
    return isinstance(value, TaskProjectionSnapshot)


@dataclass(frozen=True, slots=True)
class TaskStreamEvent:
    event_id: UUID
    task_id: UUID
    source_kind: TaskSourceKind
    source_id: UUID
    source_version: int
    occurred_at: datetime
    owner_scope_id: UUID | None = None
    sequence: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "eventId": str(self.event_id),
            "taskId": str(self.task_id),
            "sourceKind": self.source_kind.value,
            "sourceId": str(self.source_id),
            "sourceVersion": self.source_version,
            "occurredAt": self.occurred_at.astimezone(UTC).isoformat(),
        }


class TaskProjectionService:
    def __init__(
        self,
        repository: TaskProjectionRepository,
        sources: Iterable[TaskSourcePort] = (),
        *,
        metrics: object | None = None,
        clock: Callable[[], datetime] | None = None,
        capability: object | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        trace_context_provider: Callable[
            [], tuple[str | None, str | None, str | None]
        ]
        | None = None,
    ) -> None:
        self.repository = repository
        self.sources: dict[TaskSourceKind, TaskSourcePort] = {
            source.kind: source for source in sources
        }
        self.metrics = metrics
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock or monotonic
        self._capability = capability if capability is not None else object()
        # Trace lineage is admitted by composition.  A provider is evaluated
        # for every bounded operation so a long-lived service cannot reuse one
        # parent span for unrelated reconciliations.  The legacy fields remain
        # accepted for callers that already admit a fixed trace root.
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.trace_context_provider = trace_context_provider
        self._events: deque[TaskStreamEvent] = deque(maxlen=1024)
        self.reconciliation_queue: set[tuple[TaskSourceKind, UUID]] = set()

    def _record(
        self,
        name: str,
        duration_ms: float,
        *,
        outcome: str,
        lag_ms: float | None = None,
        dependency: str = "core_postgresql",
        error_class: str | None = None,
        source_kind: TaskSourceKind | None = None,
    ) -> None:
        """Emit bounded metadata without ever affecting projection truth."""
        if self.metrics is None:
            return
        try:
            outcome = {
                "stale": "unknown",
                "source_absent": "unknown",
                "unavailable": "unknown",
                "failure": "failed",
                "invalid": "rejected",
            }.get(outcome, outcome)
            outcome = outcome if outcome in _OUTCOMES else "unknown"
            error_class = error_class if error_class in _ERROR_CLASSES else None
            increment = getattr(self.metrics, "increment", None)
            observe = getattr(self.metrics, "observe", None)
            span = getattr(self.metrics, "record_span", None)
            component = TASK_PROJECTION_COMPONENT
            dimensions = {
                "operation": "reconcile",
                "outcome": outcome,
                "dependency": dependency,
            }
            if error_class is not None:
                dimensions["error_class"] = error_class
            if callable(increment):
                increment(
                    component,
                    "task_projection_outcome_count",
                    **dimensions,
                )
            if callable(observe) and lag_ms is not None:
                observe(
                    component,
                    "task_projection_lag_ms",
                    lag_ms,
                    **dimensions,
                )
            if callable(observe):
                observe(
                    component,
                    "task_projection_reconcile_latency_ms",
                    duration_ms,
                    **dimensions,
                )
            if callable(span):
                trace, child, parent = self._span_context()
                span(
                    component,
                    "task_projection.reconcile",
                    duration_ms,
                    trace_id=trace,
                    span_id=child,
                    parent_span_id=parent,
                    dependency=dependency,
                    outcome=outcome,
                    error_class=error_class,
                )
        except Exception:
            return

    def _span_context(self) -> tuple[str, str, str | None]:
        """Return unique telemetry lineage for one bounded operation.

        Composition may admit a fresh ``(trace, parent, _)`` context through
        ``trace_context_provider``.  Without admitted context, the telemetry
        layer mints content-free identifiers; no component name, process
        sequence, or service lifetime value participates in identity.
        """
        provider = self.trace_context_provider
        if provider is not None:
            trace, admitted_parent, explicit_parent = provider()
            if isinstance(trace, str) and len(trace) == 32 and all(
                character in "0123456789abcdefABCDEF" for character in trace
            ):
                parent = (
                    admitted_parent
                    if isinstance(admitted_parent, str)
                    and len(admitted_parent) == 16
                    and all(character in "0123456789abcdefABCDEF" for character in admitted_parent)
                    else explicit_parent
                    if isinstance(explicit_parent, str)
                    and len(explicit_parent) == 16
                    and all(character in "0123456789abcdefABCDEF" for character in explicit_parent)
                    else None
                )
                return trace.lower(), new_span_id(), parent.lower() if parent else None

        trace = self.trace_id
        if isinstance(trace, str) and len(trace) == 32 and all(
            character in "0123456789abcdefABCDEF" for character in trace
        ):
            parent = self.parent_span_id or self.span_id
            return trace.lower(), new_span_id(), parent.lower() if parent else None

        # Two independent telemetry-layer IDs form a valid 32-character trace
        # identifier.  This remains unique across processes and restarts while
        # carrying no user or source content.
        return new_span_id() + new_span_id(), new_span_id(), None

    @staticmethod
    def _error_class(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, ConnectionError):
            return "snapshot_unavailable"
        if isinstance(exc, (ValueError, TypeError)):
            return "validation_failed"
        if isinstance(exc, RuntimeError):
            return "storage_unavailable"
        return "snapshot_unavailable"

    def register_source(self, source: TaskSourcePort) -> None:
        self.sources[source.kind] = source

    async def _mark_reconciliation(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow | None:
        self.reconciliation_queue.add((source_kind, source_id))
        enqueue = getattr(self.repository, "enqueue_reconciliation", None)
        if callable(enqueue):
            await cast(Callable[[TaskSourceKind, UUID], Awaitable[None]], enqueue)(
                source_kind, source_id
            )
        old = await self.repository.get(source_kind, source_id)
        row = await self.repository.mark_source_unknown(source_kind, source_id)
        if row is not None and (old is None or row.projection_version > old.projection_version):
            event_id = uuid4()
            event = TaskStreamEvent(
                event_id,
                row.source_id,
                row.source_kind,
                row.source_id,
                row.source_version,
                self.clock(),
                row.owner_scope_id,
            )
            self._events.append(event)
            append_event = getattr(self.repository, "append_task_event", None)
            if callable(append_event):
                await cast(Callable[[UUID, TaskStreamEvent], Awaitable[None]], append_event)(
                    row.owner_scope_id, event
                )
        return row

    async def reconcile(self, invalidation: TaskSourceInvalidation) -> TaskProjectionRow | None:
        started = self.monotonic_clock()
        now = self.clock()
        lag_ms = max(0.0, (now - invalidation.occurred_at).total_seconds() * 1000)
        try:
            return await self._reconcile(invalidation, started, lag_ms)
        except Exception as exc:
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="failure",
                lag_ms=lag_ms,
                error_class=self._error_class(exc),
            )
            raise

    async def _reconcile(
        self, invalidation: TaskSourceInvalidation, started: float, lag_ms: float
    ) -> TaskProjectionRow | None:
        source = self.sources.get(invalidation.source_kind)
        if source is None:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="unknown",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                error_class="snapshot_unavailable",
            )
            return unknown
        existing = await self.repository.get(invalidation.source_kind, invalidation.source_id)
        if (
            existing is not None
            and existing.source_version >= invalidation.source_version
            and existing.status is not TaskNormalizedStatus.UNKNOWN
            and not existing.source_deleted
        ):
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="stale",
                lag_ms=lag_ms,
                dependency="core_postgresql",
                source_kind=invalidation.source_kind,
            )
            return existing
        query = TaskProjectionSnapshotQuery.exact(
            invalidation.source_kind, invalidation.source_id, invalidation.source_version
        )
        try:
            result = await source.query_task_projection_snapshots(
                query, capability=self._capability
            )
        except TimeoutError:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="timeout",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                error_class="timeout",
                source_kind=invalidation.source_kind,
            )
            return unknown
        except ConnectionError:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="unavailable",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                error_class="snapshot_unavailable",
                source_kind=invalidation.source_kind,
            )
            return unknown
        except RuntimeError:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="failure",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                error_class="storage_unavailable",
                source_kind=invalidation.source_kind,
            )
            return unknown
        except LookupError:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="source_absent",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                source_kind=invalidation.source_kind,
            )
            return unknown
        except Exception:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="failure",
                lag_ms=lag_ms,
                error_class="snapshot_unavailable",
                dependency="source_snapshot_api",
                source_kind=invalidation.source_kind,
            )
            return unknown
        if not isinstance(result, TaskProjectionSnapshot):
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome=("stale" if result is None else "invalid"),
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                source_kind=invalidation.source_kind,
            )
            return unknown
        if (
            result.source_kind is not invalidation.source_kind
            or result.source_id != invalidation.source_id
        ):
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="source_absent",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                source_kind=invalidation.source_kind,
            )
            return unknown
        if existing is not None and result.owner_scope_id != existing.owner_scope_id:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="unknown",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                error_class="version_gap",
            )
            return unknown
        if result.source_version < invalidation.source_version:
            unknown = await self._mark_reconciliation(
                invalidation.source_kind, invalidation.source_id
            )
            self._record(
                "task_projection.reconcile",
                (self.monotonic_clock() - started) * 1000,
                outcome="stale",
                lag_ms=lag_ms,
                dependency="source_snapshot_api",
                source_kind=invalidation.source_kind,
            )
            return unknown
        if existing is not None and existing.status is TaskNormalizedStatus.UNKNOWN:
            recover = getattr(self.repository, "recover_unknown", None)
            if result.source_version < existing.source_version:
                await self._mark_reconciliation(invalidation.source_kind, invalidation.source_id)
                self._record(
                    "task_projection.reconcile",
                    (self.monotonic_clock() - started) * 1000,
                    outcome="unknown",
                    lag_ms=lag_ms,
                    dependency="source_snapshot_api",
                    error_class="version_gap",
                )
                return await self.repository.get(invalidation.source_kind, invalidation.source_id)
            if result.source_version == existing.source_version and callable(recover):
                row = await cast(
                    Callable[[TaskProjectionSnapshot], Awaitable[TaskProjectionRow]], recover
                )(result)
            else:
                row = await self.repository.upsert(result)
        else:
            row = await self.repository.upsert(result)
        if (
            existing is None
            or result.source_version > existing.source_version
            or (
                existing.status is TaskNormalizedStatus.UNKNOWN
                and row.projection_version > existing.projection_version
            )
        ):
            event = TaskStreamEvent(
                invalidation.event_id,
                row.source_id,
                row.source_kind,
                row.source_id,
                row.source_version,
                invalidation.occurred_at,
                row.owner_scope_id,
            )
            self._events.append(event)
            append_event = getattr(self.repository, "append_task_event", None)
            if callable(append_event):
                await cast(Callable[[UUID, TaskStreamEvent], Awaitable[None]], append_event)(
                    row.owner_scope_id, event
                )
        self._record(
            "task_projection.reconcile",
            (self.monotonic_clock() - started) * 1000,
            outcome="success",
            lag_ms=lag_ms,
            dependency="source_snapshot_api",
            source_kind=invalidation.source_kind,
        )
        return row

    async def rebuild(self, source_kind: TaskSourceKind, *, page_size: int = 100) -> int:
        source = self.sources.get(source_kind)
        if source is None:
            self._record(
                "task_projection.reconcile",
                0.0,
                outcome="unknown",
                dependency="source_snapshot_api",
                error_class="snapshot_unavailable",
                source_kind=source_kind,
            )
            return 0
        if source.kind is not source_kind:
            self._record(
                "task_projection.reconcile",
                0.0,
                outcome="rejected",
                dependency="source_snapshot_api",
                error_class="validation_failed",
                source_kind=source_kind,
            )
            raise ValueError("registered source kind does not match rebuild kind")
        try:
            state = await self.repository.begin_rebuild(source_kind)
        except Exception:
            self._record(
                "task_projection.reconcile",
                0.0,
                outcome="failed",
                dependency="core_postgresql",
                error_class="storage_unavailable",
                source_kind=source_kind,
            )
            raise
        cursor: str | None = state.cursor
        seen: set[UUID] = set(state.seen_ids)
        while True:
            page_started = self.monotonic_clock()
            page_outcome = "success"
            page_error: str | None = None
            page_dependency = "source_snapshot_api"
            try:
                page = await source.query_task_projection_snapshots(
                    TaskProjectionSnapshotQuery.cursor_page(source_kind, cursor, page_size),
                    capability=self._capability,
                )
                if not isinstance(page, TaskProjectionSnapshotPage):
                    page_outcome = "rejected"
                    page_error = "validation_failed"
                    raise TypeError("source cursor query returned an exact snapshot")
                page_seen: set[UUID] = set()
                for raw_snapshot in page.items:
                    page_dependency = "source_snapshot_api"
                    snapshot: object = raw_snapshot
                    if not _is_snapshot(snapshot):
                        page_outcome = "rejected"
                        page_error = "validation_failed"
                        raise TypeError("source cursor page contains an invalid snapshot")
                    invalid_kind = snapshot.source_kind is not source_kind
                    duplicate = snapshot.source_id in seen or snapshot.source_id in page_seen
                    if invalid_kind:
                        page_outcome = "rejected"
                        page_error = "validation_failed"
                        continue
                    # Keep boundary validation provenance local to this item.
                    # A previous valid item may have used PostgreSQL, but a
                    # later malformed item is still a source-boundary error.
                    page_dependency = "core_postgresql"
                    existing = await self.repository.get(source_kind, snapshot.source_id)
                    owner_changed = (
                        existing is not None and existing.owner_scope_id != snapshot.owner_scope_id
                    )
                    if duplicate or owner_changed:
                        # The requested source kind is trusted; a malformed page
                        # item is not allowed to select another kind/id for
                        # quarantine or mutate a legitimate cross-domain row.
                        if not invalid_kind:
                            await self._mark_reconciliation(source_kind, snapshot.source_id)
                        page_outcome = "rejected"
                        page_error = "version_gap"
                        continue
                    page_seen.add(snapshot.source_id)
                    seen.add(snapshot.source_id)
                    await self.repository.upsert(snapshot)
                    page_dependency = "source_snapshot_api"
                state = TaskProjectionRebuildState(
                    source_kind,
                    state.pass_id,
                    page.next_cursor,
                    state.started_at,
                    tuple(sorted(seen, key=str)),
                )
                page_dependency = "core_postgresql"
                await self.repository.save_rebuild_checkpoint(state)
                final_page = page.scan_complete or page.next_cursor is None
                if final_page:
                    # Finalization is part of the final page operation.  Do not
                    # publish success before both settlement and pass completion;
                    # a failure therefore produces exactly one terminal sample.
                    await self.repository.tombstone_stale(source_kind, seen, state.started_at)
                    await self.repository.complete_rebuild(state)
                    return len(seen)
                cursor = page.next_cursor
            except Exception as exc:
                if page_dependency == "core_postgresql":
                    page_outcome = "failed"
                    page_error = "storage_unavailable"
                elif page_outcome == "success":
                    page_outcome = (
                        "timeout"
                        if isinstance(exc, TimeoutError)
                        else "unknown"
                        if isinstance(exc, ConnectionError)
                        else "failed"
                    )
                    page_error = self._error_class(exc)
                raise
            finally:
                self._record(
                    "task_projection.reconcile",
                    (self.monotonic_clock() - page_started) * 1000,
                    outcome=page_outcome,
                    dependency=page_dependency,
                    error_class=page_error,
                    source_kind=source_kind,
                )

    async def reconcile_pending(self, *, limit: int = 100) -> int:
        claim = getattr(self.repository, "claim_reconciliations", None)
        complete = getattr(self.repository, "complete_reconciliation", None)
        if not callable(claim) or not callable(complete):
            return 0
        items = await cast(
            Callable[[int], Awaitable[tuple[tuple[TaskSourceKind, UUID], ...]]], claim
        )(limit)
        processed = 0
        for source_kind, source_id in items:
            row = await self.repository.get(source_kind, source_id)
            if row is None:
                await cast(Callable[[TaskSourceKind, UUID], Awaitable[None]], complete)(
                    source_kind, source_id
                )
                continue
            result = await self.reconcile(
                TaskSourceInvalidation(
                    source_kind,
                    source_id,
                    row.source_version,
                    UUID(int=0),
                    self.clock(),
                    UUID(int=0),
                    UUID(int=0),
                )
            )
            if result is None or result.status is not TaskNormalizedStatus.UNKNOWN:
                await cast(Callable[[TaskSourceKind, UUID], Awaitable[None]], complete)(
                    source_kind, source_id
                )
            processed += 1
        return processed

    async def list(self, query: TaskListFilter) -> tuple[tuple[TaskProjectionRow, ...], str | None]:
        return await self.repository.list(query)

    async def detail(
        self, owner_scope_id: UUID, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow:
        row = await self.repository.get(source_kind, source_id)
        if row is None or row.owner_scope_id != owner_scope_id:
            raise TaskProjectionNotFound("task not found")
        return row

    async def find_by_task_id(self, owner_scope_id: UUID, task_id: UUID) -> TaskProjectionRow:
        """Resolve the opaque public task id without exposing the repository."""
        for source_kind in TaskSourceKind:
            row = await self.repository.get(source_kind, task_id)
            if row is not None and row.owner_scope_id == owner_scope_id:
                return row
        raise TaskProjectionNotFound("task not found")

    async def detail_payload(
        self, owner_scope_id: UUID, source_kind: TaskSourceKind, source_id: UUID
    ) -> dict[str, object]:
        row = await self.detail(owner_scope_id, source_kind, source_id)
        detail: Mapping[str, object] = {}
        source = self.sources.get(source_kind)
        if source is not None:
            resolver = getattr(source, "resolve_task_detail", None)
            if callable(resolver):
                value = await cast(Callable[..., Awaitable[Any]], resolver)(
                    source_id, owner_scope_id=owner_scope_id
                )
                if isinstance(value, Mapping):
                    detail = cast(Mapping[str, object], value)
        display_label = detail.get("displayLabel")
        recovery_action = detail.get("recoveryAction")
        artifact_refs = detail.get("artifactRefs")
        return row.as_detail(
            display_label=display_label if isinstance(display_label, str) else None,
            recovery_action=recovery_action if isinstance(recovery_action, str) else None,
            artifact_refs=(
                tuple(cast(Iterable[Mapping[str, object]], artifact_refs))
                if isinstance(artifact_refs, (tuple, list))
                else ()
            ),
        )

    async def replay_events(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        after: UUID | None = None,
    ) -> tuple[TaskStreamEvent, ...]:
        row = await self.repository.get(source_kind, source_id)
        if row is None or row.owner_scope_id != owner_scope_id:
            raise TaskProjectionNotFound("task not found")
        durable = getattr(self.repository, "replay_task_events", None)
        if callable(durable):
            try:
                durable_events = await cast(Callable[..., Awaitable[tuple[object, ...]]], durable)(
                    owner_scope_id, source_kind, source_id, after
                )
                return tuple(cast(TaskStreamEvent, event) for event in durable_events)
            except LookupError:
                # A durable cursor outside retention is an explicit expiry;
                # never mask it with a process-local replay window.
                raise
        events = tuple(
            event
            for event in self._events
            if (event.owner_scope_id is None or event.owner_scope_id == owner_scope_id)
            and event.source_kind is source_kind
            and event.source_id == source_id
        )
        if after is None:
            return events
        for index, event in enumerate(events):
            if event.event_id == after:
                return events[index + 1 :]
        raise LookupError("task event cursor is outside replay window")

    async def command(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: TaskCommand,
        *,
        expected_source_version: int,
        idempotency_key: str,
    ) -> TaskCommandReceipt:
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ValueError("Idempotency-Key is required")
        existing_receipt = await self.repository.command_receipt(
            owner_scope_id, source_kind, source_id, command.value, idempotency_key
        )
        if isinstance(existing_receipt, TaskCommandReceipt):
            return existing_receipt
        row = await self.detail(owner_scope_id, source_kind, source_id)
        if row.source_version != expected_source_version:
            raise TaskProjectionConflict("task source version changed")
        allowed = (
            row.capabilities.can_cancel
            if command is TaskCommand.CANCEL
            else row.capabilities.can_retry
        )
        if not allowed:
            return TaskCommandReceipt(
                source_id, command, TaskCommandState.REJECTED, row.source_version
            )
        source = self.sources.get(source_kind)
        if source is None:
            receipt = TaskCommandReceipt(
                source_id, command, TaskCommandState.UNKNOWN, row.source_version
            )
            await self.repository.save_command_receipt(
                owner_scope_id, source_kind, source_id, command.value, idempotency_key, receipt
            )
            await self._mark_reconciliation(source_kind, source_id)
            return receipt
        try:
            receipt = await source.command_task(
                source_id,
                command,
                owner_scope_id=owner_scope_id,
                expected_source_version=expected_source_version,
                idempotency_key=idempotency_key,
            )
        except TimeoutError, ConnectionError:
            receipt = TaskCommandReceipt(
                source_id, command, TaskCommandState.UNKNOWN, row.source_version
            )
            await self.repository.save_command_receipt(
                owner_scope_id, source_kind, source_id, command.value, idempotency_key, receipt
            )
            await self._mark_reconciliation(source_kind, source_id)
            return receipt
        if receipt.unknown:
            state = TaskCommandState.UNKNOWN
        elif receipt.accepted:
            state = TaskCommandState.ACCEPTED
        else:
            state = TaskCommandState.REJECTED
        result = TaskCommandReceipt(source_id, command, state, receipt.source_version)
        await self.repository.save_command_receipt(
            owner_scope_id, source_kind, source_id, command.value, idempotency_key, result
        )
        if state is TaskCommandState.UNKNOWN:
            await self._mark_reconciliation(source_kind, source_id)
        return result


__all__ = [
    "TaskProjectionService",
    "TaskProjectionNotFound",
    "TaskProjectionConflict",
    "TaskStreamEvent",
]
