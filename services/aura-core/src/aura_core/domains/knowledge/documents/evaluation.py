"""Deterministic, replayable metadata-only document evaluators.

Evaluators deliberately accept identifiers, statuses, counts, and scores only.
They never receive document text, filenames, owner identities, vectors, or
storage keys, which keeps evaluation artifacts safe to retain and replay.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from aura_core.domains.knowledge.documents.public import DocumentService

FIXTURE_REVISION = "document-library-eval-fixtures-v1"
COMPONENT_VERSION = "1.0.0"
EVALUATOR_VERSIONS: Mapping[str, str] = {
    "document_format_validation": "1.0.0",
    "document_owner_isolation": "1.0.0",
    "document_processing_idempotency": "1.0.0",
    "document_retrieval_relevance": "1.0.0",
    "document_deletion_completion": "1.0.0",
}


@dataclass(frozen=True, slots=True)
class DocumentEvaluationResult:
    evaluator_id: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: Mapping[str, str]
    component_version: str = COMPONENT_VERSION
    evaluator_version: str = "1.0.0"

    def payload(self) -> dict[str, object]:
        return {
            "evaluatorId": self.evaluator_id,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
            "componentVersion": self.component_version,
            "evaluatorVersion": self.evaluator_version,
        }


class DocumentReplayAdapter:
    """Replay by invoking the deployed processing behavior for an event."""

    def __init__(
        self,
        service: DocumentService,
        *,
        component_version: str = COMPONENT_VERSION,
    ) -> None:
        if component_version != COMPONENT_VERSION:
            raise ValueError("unsupported document processing component version")
        self._service = service
        self._component_version = COMPONENT_VERSION

    async def replay(self, event_id: UUID) -> dict[str, object]:
        document_id = await self._service.resolve_lifecycle_event(event_id)
        if document_id is None:
            raise ValueError("unknown document lifecycle event")
        document = await self._service.process(document_id)
        return {
            "componentVersion": self._component_version,
            "documentId": str(document.id),
            "status": document.status.value,
            "documentVersion": document.version,
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> DocumentEvaluationResult:
    evaluator_version = EVALUATOR_VERSIONS[evaluator_id]
    replay_key = str(
        uuid5(NAMESPACE_URL, f"{COMPONENT_VERSION}:{FIXTURE_REVISION}:{evaluator_id}")
    )
    return DocumentEvaluationResult(
        evaluator_id,
        FIXTURE_REVISION,
        replay_key,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
        COMPONENT_VERSION,
        evaluator_version,
    )


def evaluate_format_validation(*, accepted: bool, expected: bool) -> DocumentEvaluationResult:
    return _result("document_format_validation", accepted is expected)


def evaluate_owner_isolation(*, foreign_results: int) -> DocumentEvaluationResult:
    return _result(
        "document_owner_isolation", foreign_results == 0, foreign_results=str(foreign_results)
    )


def evaluate_processing_idempotency(
    *, ready_runs: int, chunk_counts: tuple[int, ...]
) -> DocumentEvaluationResult:
    stable = ready_runs >= 1 and len(set(chunk_counts)) == 1
    return _result("document_processing_idempotency", stable, runs=str(ready_runs))


def evaluate_retrieval_relevance(
    *, relevant_rank: int | None, result_count: int
) -> DocumentEvaluationResult:
    passed = relevant_rank is not None and relevant_rank <= max(1, result_count)
    return _result("document_retrieval_relevance", passed, result_count=str(result_count))


def evaluate_deletion_completion(
    *, searchable_results: int, object_deleted: bool
) -> DocumentEvaluationResult:
    return _result(
        "document_deletion_completion",
        searchable_results == 0 and object_deleted,
    )
