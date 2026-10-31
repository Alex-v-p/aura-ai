"""Stable public facade for the owner-scoped memory domain.

Contracts live in the acyclic ``contracts`` leaf.  This module eagerly
re-exports those contracts and the application services so every historical
``memory.public`` import remains source compatible.
"""

from typing import Any, cast
from uuid import UUID, uuid5

from aura_core.domains.knowledge.memory.contracts import (
    ARCHIVE_AFTER_DAYS,
    DORMANT_THRESHOLD,
    MAX_HALF_LIFE_DAYS,
    MEMORY_ACTION_SCHEMA,
    MEMORY_EXTRACTION_POLICY_VERSION,
    MEMORY_ID_NAMESPACE,
    MEMORY_PROCESSING_SCHEMA_VERSION,
    MEMORY_PROCESSING_TOPIC,
    MEMORY_PROVENANCE_TYPES,
    MEMORY_RELATION_TYPES,
    MIN_HALF_LIFE_DAYS,
    PURGE_CONFIRMATION,
    SENSITIVITY_POLICY_VERSION,
    CandidateDecision,
    CandidateState,
    MemoryAction,
    MemoryActivityItem,
    MemoryActivitySnapshot,
    MemoryAuditRecord,
    MemoryCandidate,
    MemoryCollectionScopeType,
    MemoryEmbedding,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryError,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryModelConfigurationSnapshot,
    MemoryModelDescriptor,
    MemoryNotFound,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingSettlement,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryPurgeReplayNotFound,
    MemoryRecord,
    MemoryReindexSnapshot,
    MemoryRelation,
    MemoryRetentionBasis,
    MemoryRevision,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemorySensitivity,
    MemoryTurnEvidence,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
    classify_retention_basis,
    classify_sensitivity,
    contains_secret,
    decide_candidate,
    decode_memory_cursor,
    deterministic_fallback_kind,
    encode_memory_cursor,
    normalize_retention_horizon,
    validate_memory_text,
    validate_provenance,
    validate_revision,
)
from aura_core.domains.knowledge.memory.contracts import (
    DEFAULT_MEMORY_HALF_LIFE_DAYS as _DEFAULT_MEMORY_HALF_LIFE_DAYS,
)
from aura_core.domains.knowledge.memory.contracts import (
    DEFAULT_MEMORY_IMPORTANCE as _DEFAULT_MEMORY_IMPORTANCE,
)
from aura_core.domains.knowledge.memory.contracts import (
    command_values as _command_values,
)
from aura_core.domains.knowledge.memory.contracts import (
    content_free_candidate as _content_free_candidate,
)
from aura_core.domains.knowledge.memory.contracts import (
    deterministic_create_idempotency_key as _deterministic_create_idempotency_key,
)
from aura_core.domains.knowledge.memory.contracts import (
    fallback_reinforcement_marker as _fallback_reinforcement_marker,
)
from aura_core.domains.knowledge.memory.contracts import (
    fallback_reinforcement_target_marker as _fallback_reinforcement_target_marker,
)
from aura_core.domains.knowledge.memory.contracts import (
    fingerprint as _contract_fingerprint,
)
from aura_core.domains.knowledge.memory.contracts import (
    memory_activity_id as _memory_activity_id,
)
from aura_core.domains.knowledge.memory.contracts import (
    normalize_candidate_for_approval as _normalize_candidate_for_approval,
)
from aura_core.domains.knowledge.memory.contracts import (
    owner as _owner,
)
from aura_core.domains.knowledge.memory.contracts import (
    reindex_pending as _reindex_pending,
)
from aura_core.domains.knowledge.memory.contracts import (
    scope_authorized as _scope_authorized,
)
from aura_core.domains.knowledge.memory.contracts import (
    user_family_relation_event as _user_family_relation_event,
)
from aura_core.domains.knowledge.memory.embedding import MemoryReindexService
from aura_core.domains.knowledge.memory.model_service import MemoryModelApplicationService
from aura_core.domains.knowledge.memory.ports import MemoryTelemetry
from aura_core.domains.knowledge.memory.processing import (
    MemoryProcessingService,
    MemoryProcessor,
)
from aura_core.domains.knowledge.memory.repository_ports import MemoryRepository
from aura_core.domains.knowledge.memory.store import MemoryStore

DEFAULT_MEMORY_HALF_LIFE_DAYS = _DEFAULT_MEMORY_HALF_LIFE_DAYS
DEFAULT_MEMORY_IMPORTANCE = _DEFAULT_MEMORY_IMPORTANCE
_fingerprint = _contract_fingerprint
_normalize_candidate_for_approval = _normalize_candidate_for_approval
memory_activity_id = _memory_activity_id
command_values = _command_values
content_free_candidate = _content_free_candidate
deterministic_create_idempotency_key = _deterministic_create_idempotency_key
fingerprint = _contract_fingerprint
normalize_candidate_for_approval = _normalize_candidate_for_approval
owner = _owner
scope_authorized = _scope_authorized
fallback_reinforcement_marker = _fallback_reinforcement_marker
fallback_reinforcement_target_marker = _fallback_reinforcement_target_marker
user_family_relation_event = _user_family_relation_event
reindex_pending = _reindex_pending
MemoryCatalog = MemoryStore
MemoryService = MemoryStore

__all__ = [
    "ARCHIVE_AFTER_DAYS",
    "DORMANT_THRESHOLD",
    "MAX_HALF_LIFE_DAYS",
    "MIN_HALF_LIFE_DAYS",
    "PURGE_CONFIRMATION",
    "CandidateDecision",
    "CandidateState",
    "MemoryAction",
    "MemoryActivityItem",
    "MemoryActivitySnapshot",
    "MemoryAuditRecord",
    "MemoryCandidate",
    "MemoryEmbedding",
    "MemoryError",
    "MemoryEmbeddingGeneration",
    "MemoryFilters",
    "MemoryIdempotencyConflict",
    "MemoryKind",
    "MemoryLifecycleStatus",
    "MemoryNotFound",
    "MemoryProvenance",
    "MemoryPurgeConfirmationRequired",
    "MemoryPurgeReplayNotFound",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryScopeAuthorizationRequired",
    "MemoryRepository",
    "MemoryRevision",
    "MemoryScope",
    "MemoryScopeType",
    "MemoryCollectionScopeType",
    "MEMORY_ACTION_SCHEMA",
    "MEMORY_EXTRACTION_POLICY_VERSION",
    "MEMORY_ID_NAMESPACE",
    "MEMORY_PROCESSING_SCHEMA_VERSION",
    "MEMORY_PROCESSING_TOPIC",
    "MEMORY_PROVENANCE_TYPES",
    "MEMORY_RELATION_TYPES",
    "SENSITIVITY_POLICY_VERSION",
    "MemoryEmbeddingJob",
    "MemoryModelConfiguration",
    "MemoryModelApplicationService",
    "MemoryModelConfigurationSnapshot",
    "MemoryModelDescriptor",
    "MemoryReindexSnapshot",
    "MemoryProcessingCommand",
    "MemoryProcessingJob",
    "MemoryProcessingSettlement",
    "MemoryRetentionBasis",
    "MemorySensitivity",
    "MemoryTurnEvidence",
    "MemoryValidationError",
    "MemoryVersionConflict",
    "ProcessingJobStatus",
    "classify_retention_basis",
    "normalize_retention_horizon",
    "classify_sensitivity",
    "deterministic_fallback_kind",
    "contains_secret",
    "decode_memory_cursor",
    "decide_candidate",
    "encode_memory_cursor",
    "validate_memory_text",
    "validate_provenance",
    "validate_revision",
    "MemoryCatalog",
    "MemoryProcessingService",
    "MemoryProcessor",
    "MemoryReindexService",
    "MemoryService",
    "MemoryStore",
    "MemoryTelemetry",
    "MemoryReindexTaskProjectionApplication",
    "MemoryReindexTaskSourceAdapter",
]


async def query_task_projection_snapshots(
    query: object,
    *,
    capability: object,
) -> object:
    """Expose only owner-scoped memory reindex metadata to execution."""
    from aura_core.domains.execution.tasks.contracts import (
        TaskProjectionSnapshotQuery,
    )

    if not isinstance(query, TaskProjectionSnapshotQuery):
        raise TypeError("task projection query is invalid")
    raise PermissionError("memory snapshot enumeration requires a bound source adapter")


class MemoryReindexTaskProjectionApplication:
    """Metadata view over the authoritative embedding reindex lifecycle."""

    def __init__(
        self,
        application: Any,
        *,
        owner_scope_id: UUID | None = None,
        issuer: str | None = None,
        subject: str | None = None,
    ) -> None:
        self.application: Any = application
        self.repository = getattr(application, "repository", application)
        self.owner_scope_id: Any = owner_scope_id or getattr(
            application, "task_projection_owner_scope_id", None
        )
        owner: Any = getattr(application, "task_projection_owner", None)
        self.issuer: Any
        self.subject: Any
        self.issuer, self.subject = (
            (issuer, subject)
            if issuer is not None and subject is not None
            else owner
            if isinstance(owner, tuple) and len(cast(tuple[object, ...], owner)) == 2
            else (getattr(application, "issuer", None), getattr(application, "subject", None))
        )
        self.__capability: object | None = None

    def __bind_capability(self, capability: object) -> None:  # composition-only  # pyright: ignore[reportUnusedFunction]  # noqa: E501
        if self.__capability is not None:
            raise PermissionError("memory projection capability is already bound")
        self.__capability = capability

    def __validate_capability(self, capability: object) -> None:
        if self.__capability is None or capability is not self.__capability:
            raise PermissionError("memory projection capability is not admitted")

    async def _items(self, query: Any) -> tuple[object, ...]:
        from aura_core.domains.execution.tasks.contracts import TaskProjectionSnapshotQuery

        if not isinstance(query, TaskProjectionSnapshotQuery):
            raise TypeError("task projection query is invalid")
        # Reindex tasks are the embedding-generation lifecycle.  Conversation
        # processing jobs are deliberately not part of this source kind.
        exact = getattr(self.application, "get_reindex_task", None)
        lister = getattr(self.application, "list_reindex_tasks", None)
        if not callable(exact):
            exact = getattr(self.repository, "get_reindex_task", None)
        if not callable(lister):
            lister = getattr(self.repository, "list_reindex_tasks", None)
        if query.mode == "exact":
            if not callable(exact):
                raise TypeError("memory application must expose reindex lookup")
            item = await cast(Any, exact)(query.source_id)
            return (
                (item,)
                if isinstance(item, MemoryEmbeddingGeneration)
                and item.task_projection_version >= query.minimum_source_version
                else ()
            )
        if not callable(lister):
            raise TypeError("memory application must expose reindex enumeration")
        values = await cast(Any, lister)(cursor=query.cursor, limit=query.page_size)
        return tuple(item for item in values if isinstance(item, MemoryEmbeddingGeneration))

    @staticmethod
    def _snapshot(item: object, owner_scope_id: UUID) -> object:
        from aura_core.domains.execution.tasks.contracts import (
            TaskNormalizedStatus,
            TaskProjectionCapability,
            TaskProjectionSnapshot,
            TaskSourceKind,
        )

        if isinstance(item, MemoryEmbeddingGeneration):
            status_map = {
                "building": TaskNormalizedStatus.RUNNING,
                "active": TaskNormalizedStatus.COMPLETED,
                "failed": TaskNormalizedStatus.FAILED,
                "retired": TaskNormalizedStatus.CANCELED,
            }
            normalized = status_map.get(item.status, TaskNormalizedStatus.UNKNOWN)
            stamp = item.activated_at or item.created_at
            return TaskProjectionSnapshot(
                owner_scope_id,
                TaskSourceKind.MEMORY_REINDEX,
                item.id,
                max(1, item.task_projection_version),
                normalized,
                item.status,
                item.status
                if normalized in {TaskNormalizedStatus.COMPLETED, TaskNormalizedStatus.FAILED}
                else None,
                "embedding_generation",
                1.0 if normalized is TaskNormalizedStatus.COMPLETED else None,
                # Memory reindexing has no owner-facing source detail route in
                # the current additive contract.  The projection may still
                # advertise retry, but it must not imply a navigable detail.
                TaskProjectionCapability(
                    can_retry=item.status == "failed",
                    has_source_detail=False,
                ),
                "memory_reindex_detail",
                item.status if normalized is TaskNormalizedStatus.FAILED else None,
                item.created_at,
                stamp,
                stamp
                if normalized in {TaskNormalizedStatus.COMPLETED, TaskNormalizedStatus.FAILED}
                else None,
            )

    async def query_task_projection_snapshots(self, query: Any, *, capability: object) -> Any:
        self.__validate_capability(capability)
        from aura_core.domains.execution.tasks.contracts import TaskProjectionSnapshotPage

        def owner_for(item: object) -> UUID:
            value = getattr(item, "owner_scope_id", None)
            if isinstance(value, UUID):
                return value
            resolver = getattr(self.application, "task_projection_owner_scope_id_for", None)
            if callable(resolver):
                resolved = resolver(item)
                if isinstance(resolved, UUID):
                    return resolved
            if isinstance(self.owner_scope_id, UUID):
                return self.owner_scope_id
            issuer = getattr(item, "issuer", None)
            subject = getattr(item, "subject", None)
            if isinstance(issuer, str) and isinstance(subject, str):
                return uuid5(
                    UUID("8edc7ad7-8f83-4f13-b34b-1fa37d4c6bf4"),
                    f"{issuer}:{subject}",
                )
            raise ValueError("memory source did not provide an owner scope")

        items: tuple[Any, ...] = tuple(
            self._snapshot(item, owner_for(item)) for item in await self._items(query)
        )
        if getattr(query, "mode", None) == "exact":
            return items[0] if items else None
        page_size = query.page_size
        from .task_cursor import decode_task_cursor, encode_task_cursor

        next_cursor = (
            encode_task_cursor(
                decode_task_cursor(query.cursor)[0] if query.cursor else items[0].created_at,
                items[-1].created_at,
                items[-1].source_id,
            )
            if len(items) == page_size and items
            else None
        )
        return TaskProjectionSnapshotPage(
            items=items,
            next_cursor=next_cursor,
            scan_complete=len(items) < page_size,
        )

    async def command_task(
        self,
        source_id: UUID,
        command: object,
        *,
        owner_scope_id: UUID,
        expected_source_version: int,
        idempotency_key: str,
    ) -> object:
        from aura_core.domains.execution.tasks.contracts import (
            TaskCommand,
            TaskCommandReceipt,
            TaskCommandState,
        )

        method_name = "cancel_task" if str(command) == "cancel" else "retry_task"
        method: Any = getattr(self.application, method_name, None)
        if (
            isinstance(self.owner_scope_id, UUID) and owner_scope_id != self.owner_scope_id
        ) or not callable(method):
            return TaskCommandReceipt(
                source_id,
                TaskCommand(command),
                TaskCommandState.REJECTED
                if owner_scope_id != self.owner_scope_id
                else TaskCommandState.UNKNOWN,
                expected_source_version,
            )
        result: Any = await cast(Any, method)(
            source_id,
            owner_scope_id=owner_scope_id,
            expected_source_version=expected_source_version,
            idempotency_key=idempotency_key,
        )
        return TaskCommandReceipt(
            source_id,
            TaskCommand(command),
            TaskCommandState.ACCEPTED,
            int(getattr(result, "version", expected_source_version)),
        )

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> dict[str, object]:
        if isinstance(self.owner_scope_id, UUID) and owner_scope_id != self.owner_scope_id:
            raise PermissionError("memory task owner mismatch")
        from aura_core.domains.execution.tasks.contracts import (
            TaskProjectionSnapshotQuery,
            TaskSourceKind,
        )

        items = await self._items(
            TaskProjectionSnapshotQuery.exact(TaskSourceKind.MEMORY_REINDEX, source_id)
        )
        if not items:
            raise MemoryNotFound("memory processing task not found")
        item = cast(MemoryEmbeddingGeneration, items[0])
        return {
            "displayLabel": f"Memory reindex {item.id}",
            "recoveryAction": "retry" if item.status == "failed" else None,
        }


class MemoryReindexTaskSourceAdapter:
    """TaskSourcePort backed by the memory application facade."""

    kind = "memory_reindex"

    def __init__(
        self,
        application: object,
        *,
        owner_scope_id: UUID | None = None,
        issuer: str | None = None,
        subject: str | None = None,
    ) -> None:
        from aura_core.domains.execution.tasks.contracts import TaskSourceKind
        from aura_core.domains.execution.tasks.source_adapter import ApplicationTaskSourceAdapter

        bound_application = (
            application
            if callable(getattr(application, "query_task_projection_snapshots", None))
            else MemoryReindexTaskProjectionApplication(
                application, owner_scope_id=owner_scope_id, issuer=issuer, subject=subject
            )
        )
        self._adapter = ApplicationTaskSourceAdapter(
            TaskSourceKind.MEMORY_REINDEX, bound_application
        )
        self.kind = self._adapter.kind

    def __getattr__(self, name: str) -> object:
        return getattr(self._adapter, name)
