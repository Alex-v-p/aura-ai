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
UPLOAD_COMPONENT_ID = "aura.knowledge.document_upload"
PROCESSING_COMPONENT_ID = "aura.knowledge.document_processing"
RETRIEVAL_COMPONENT_ID = "aura.knowledge.document_retrieval"
DELETION_COMPONENT_ID = "aura.knowledge.document_deletion"
DOCUMENT_COMPONENT_VERSIONS: Mapping[str, str] = {
    UPLOAD_COMPONENT_ID: "1.0.0",
    PROCESSING_COMPONENT_ID: "1.0.0",
    RETRIEVAL_COMPONENT_ID: "1.1.0",
    DELETION_COMPONENT_ID: "1.0.0",
}
# Keep the retrieval aliases for callers that evaluated the AURA-0069 search
# slice before upload, processing, and deletion attribution was split out.
COMPONENT_ID = RETRIEVAL_COMPONENT_ID
COMPONENT_VERSION = DOCUMENT_COMPONENT_VERSIONS[RETRIEVAL_COMPONENT_ID]
EVALUATOR_VERSIONS: Mapping[str, str] = {
    "document_format_validation": "1.0.0",
    "document_owner_isolation": "1.0.0",
    "document_processing_idempotency": "1.0.0",
    "document_retrieval_relevance": "1.0.0",
    "document_preview_boundedness": "1.0.0",
    "document_deletion_completion": "1.0.0",
}
EVALUATOR_COMPONENTS: Mapping[str, str] = {
    "document_format_validation": UPLOAD_COMPONENT_ID,
    "document_owner_isolation": UPLOAD_COMPONENT_ID,
    "document_processing_idempotency": PROCESSING_COMPONENT_ID,
    "document_retrieval_relevance": RETRIEVAL_COMPONENT_ID,
    "document_preview_boundedness": RETRIEVAL_COMPONENT_ID,
    "document_deletion_completion": DELETION_COMPONENT_ID,
}
EVALUATOR_ALLOWED_COMPONENTS: Mapping[str, frozenset[str]] = {
    "document_format_validation": frozenset(
        {UPLOAD_COMPONENT_ID, PROCESSING_COMPONENT_ID}
    ),
    "document_owner_isolation": frozenset(
        {UPLOAD_COMPONENT_ID, RETRIEVAL_COMPONENT_ID, DELETION_COMPONENT_ID}
    ),
    "document_processing_idempotency": frozenset({PROCESSING_COMPONENT_ID}),
    "document_retrieval_relevance": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_preview_boundedness": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_deletion_completion": frozenset({DELETION_COMPONENT_ID}),
}


@dataclass(frozen=True, slots=True)
class DocumentEvaluationResult:
    evaluator_id: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: Mapping[str, str]
    component_id: str
    component_version: str
    evaluator_version: str

    def payload(self) -> dict[str, object]:
        return {
            "evaluatorId": self.evaluator_id,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "evaluatorVersion": self.evaluator_version,
        }


class DocumentReplayAdapter:
    """Replay by invoking the deployed processing behavior for an event."""

    def __init__(
        self,
        service: DocumentService,
        *,
        component_version: str = DOCUMENT_COMPONENT_VERSIONS[PROCESSING_COMPONENT_ID],
    ) -> None:
        if component_version != DOCUMENT_COMPONENT_VERSIONS[PROCESSING_COMPONENT_ID]:
            raise ValueError("unsupported document processing component version")
        self._service = service
        self._component_id = PROCESSING_COMPONENT_ID
        self._component_version = component_version

    async def replay(self, event_id: UUID) -> dict[str, object]:
        document_id = await self._service.resolve_lifecycle_event(event_id)
        if document_id is None:
            raise ValueError("unknown document lifecycle event")
        document = await self._service.process(document_id)
        return {
            "componentId": self._component_id,
            "componentVersion": self._component_version,
            "documentId": str(document.id),
            "status": document.status.value,
            "documentVersion": document.version,
        }


def _result(
    evaluator_id: str,
    passed: bool,
    *,
    component_id: str | None = None,
    **metadata: str,
) -> DocumentEvaluationResult:
    evaluator_version = EVALUATOR_VERSIONS[evaluator_id]
    resolved_component_id = component_id or EVALUATOR_COMPONENTS[evaluator_id]
    if resolved_component_id not in EVALUATOR_ALLOWED_COMPONENTS[evaluator_id]:
        raise ValueError(f"{evaluator_id} is not valid for {resolved_component_id}")
    component_version = DOCUMENT_COMPONENT_VERSIONS[resolved_component_id]
    replay_key = str(
        uuid5(
            NAMESPACE_URL,
            f"{resolved_component_id}:{component_version}:{FIXTURE_REVISION}:{evaluator_id}",
        )
    )
    return DocumentEvaluationResult(
        evaluator_id,
        FIXTURE_REVISION,
        replay_key,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
        resolved_component_id,
        component_version,
        evaluator_version,
    )


def evaluate_format_validation(
    *,
    accepted: bool,
    expected: bool,
    component_id: str = UPLOAD_COMPONENT_ID,
) -> DocumentEvaluationResult:
    return _result(
        "document_format_validation", accepted is expected, component_id=component_id
    )


def evaluate_owner_isolation(
    *, foreign_results: int, component_id: str = UPLOAD_COMPONENT_ID
) -> DocumentEvaluationResult:
    return _result(
        "document_owner_isolation",
        foreign_results == 0,
        component_id=component_id,
        foreign_results=str(foreign_results),
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


def evaluate_preview_boundedness(
    *,
    preview_characters: int,
    total_characters: int,
    truncated: bool,
    max_characters: int = 20_000,
) -> DocumentEvaluationResult:
    """Evaluate only the bounded preview metadata returned by Core.

    The evaluator intentionally does not accept preview text.  It verifies
    the contract invariants that can be replayed safely: non-negative lengths,
    a preview no longer than its source, the configured bound, and consistent
    truncation metadata.
    """

    valid_lengths = all(
        type(value) is int
        for value in (preview_characters, total_characters, max_characters)
    )
    passed = (
        valid_lengths
        and max_characters > 0
        and preview_characters >= 0
        and total_characters >= 0
        and preview_characters <= total_characters
        and preview_characters <= max_characters
        and truncated is (total_characters > max_characters)
    )
    return _result(
        "document_preview_boundedness",
        passed,
        preview_characters=str(preview_characters),
        total_characters=str(total_characters),
        max_characters=str(max_characters),
        truncated=str(truncated).lower(),
    )


def evaluate_deletion_completion(
    *, searchable_results: int, object_deleted: bool
) -> DocumentEvaluationResult:
    return _result(
        "document_deletion_completion",
        searchable_results == 0 and object_deleted,
    )
