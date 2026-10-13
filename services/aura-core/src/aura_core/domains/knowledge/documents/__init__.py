"""Owner-private document storage and retrieval boundary."""

from aura_core.domains.knowledge.documents.evaluation import (
    COMPONENT_VERSION,
    EVALUATOR_VERSIONS,
    DocumentEvaluationResult,
    DocumentReplayAdapter,
)
from aura_core.domains.knowledge.documents.public import (
    DOCUMENT_PROCESSING_TOPIC,
    Document,
    DocumentChunk,
    DocumentError,
    DocumentLifecycleEvent,
    DocumentNotFound,
    DocumentSearchHit,
    DocumentService,
    DocumentStatus,
    DocumentValidationError,
)

__all__ = [
    "DOCUMENT_PROCESSING_TOPIC",
    "Document",
    "DocumentChunk",
    "DocumentLifecycleEvent",
    "DocumentEvaluationResult",
    "DocumentReplayAdapter",
    "COMPONENT_VERSION",
    "EVALUATOR_VERSIONS",
    "DocumentError",
    "DocumentNotFound",
    "DocumentSearchHit",
    "DocumentService",
    "DocumentStatus",
    "DocumentValidationError",
]
