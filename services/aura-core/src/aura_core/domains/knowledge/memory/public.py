"""Stable public facade for the owner-scoped memory domain.

Contracts live in the acyclic ``contracts`` leaf.  This module eagerly
re-exports those contracts and the application services so every historical
``memory.public`` import remains source compatible.
"""

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
]
