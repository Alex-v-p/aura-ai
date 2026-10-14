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
TOOL_EXECUTION_COMPONENT_ID = "aura.runtime.tool_execution"
TOOL_EXECUTION_COMPONENT_VERSION = "1.3.0"
DOCUMENT_COMPONENT_VERSIONS: Mapping[str, str] = {
    UPLOAD_COMPONENT_ID: "1.0.0",
    PROCESSING_COMPONENT_ID: "1.0.0",
    RETRIEVAL_COMPONENT_ID: "1.2.0",
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
    # Retrieval quality evaluators are deliberately metadata-only.  Their
    # inputs are counts/ranks and bounded booleans so fixture replay never
    # exports document text, filenames, queries, or model payloads.
    "document_recall": "1.0.0",
    "document_chunk_recall": "1.0.0",
    "document_retrieval_ranking": "1.0.0",
    "document_citation_validity": "1.0.0",
    "document_irrelevant_context_rate": "1.0.0",
    "document_prompt_injection_resistance": "1.0.0",
    "attached_document_grounding_and_citation": "1.0.0",
}
EVALUATOR_COMPONENTS: Mapping[str, str] = {
    "document_format_validation": UPLOAD_COMPONENT_ID,
    "document_owner_isolation": UPLOAD_COMPONENT_ID,
    "document_processing_idempotency": PROCESSING_COMPONENT_ID,
    "document_retrieval_relevance": RETRIEVAL_COMPONENT_ID,
    "document_preview_boundedness": RETRIEVAL_COMPONENT_ID,
    "document_deletion_completion": DELETION_COMPONENT_ID,
    "document_recall": RETRIEVAL_COMPONENT_ID,
    "document_chunk_recall": RETRIEVAL_COMPONENT_ID,
    "document_retrieval_ranking": RETRIEVAL_COMPONENT_ID,
    "document_citation_validity": TOOL_EXECUTION_COMPONENT_ID,
    "document_irrelevant_context_rate": RETRIEVAL_COMPONENT_ID,
    "document_prompt_injection_resistance": RETRIEVAL_COMPONENT_ID,
    "attached_document_grounding_and_citation": RETRIEVAL_COMPONENT_ID,
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
    "document_recall": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_chunk_recall": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_retrieval_ranking": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_citation_validity": frozenset({TOOL_EXECUTION_COMPONENT_ID}),
    "document_irrelevant_context_rate": frozenset({RETRIEVAL_COMPONENT_ID}),
    "document_prompt_injection_resistance": frozenset({RETRIEVAL_COMPONENT_ID}),
    "attached_document_grounding_and_citation": frozenset({RETRIEVAL_COMPONENT_ID}),
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
    component_version = (
        TOOL_EXECUTION_COMPONENT_VERSION
        if resolved_component_id == TOOL_EXECUTION_COMPONENT_ID
        else DOCUMENT_COMPONENT_VERSIONS[resolved_component_id]
    )
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


def evaluate_document_recall(
    *, relevant_document_count: int, retrieved_relevant_count: int
) -> DocumentEvaluationResult:
    """Evaluate whether every labelled relevant document was retrieved.

    The fixture runner supplies only counts.  A zero-label fixture is a
    valid negative query and passes when no relevant result is reported.
    """

    valid = (
        type(relevant_document_count) is int
        and type(retrieved_relevant_count) is int
        and relevant_document_count >= 0
        and 0 <= retrieved_relevant_count <= relevant_document_count
    )
    score = (
        retrieved_relevant_count / relevant_document_count
        if valid and relevant_document_count
        else 1.0 if valid else 0.0
    )
    return _result(
        "document_recall",
        valid and retrieved_relevant_count == relevant_document_count,
        relevant_document_count=str(relevant_document_count),
        retrieved_relevant_count=str(retrieved_relevant_count),
        recall=f"{score:.6f}",
    )


def evaluate_document_chunk_recall(
    *, relevant_chunk_count: int, retrieved_relevant_chunk_count: int
) -> DocumentEvaluationResult:
    """Evaluate chunk-level recall using label counts only."""

    valid = (
        type(relevant_chunk_count) is int
        and type(retrieved_relevant_chunk_count) is int
        and relevant_chunk_count >= 0
        and 0 <= retrieved_relevant_chunk_count <= relevant_chunk_count
    )
    score = (
        retrieved_relevant_chunk_count / relevant_chunk_count
        if valid and relevant_chunk_count
        else 1.0 if valid else 0.0
    )
    return _result(
        "document_chunk_recall",
        valid and retrieved_relevant_chunk_count == relevant_chunk_count,
        relevant_chunk_count=str(relevant_chunk_count),
        retrieved_relevant_chunk_count=str(retrieved_relevant_chunk_count),
        recall=f"{score:.6f}",
    )


def evaluate_document_retrieval_ranking(
    *, relevant_rank: int | None, expected_max_rank: int = 3
) -> DocumentEvaluationResult:
    """Evaluate a labelled result's bounded rank without retaining its ID."""

    valid = (
        type(expected_max_rank) is int
        and expected_max_rank > 0
        and (relevant_rank is None or type(relevant_rank) is int)
    )
    passed = (
        valid
        and relevant_rank is not None
        and relevant_rank >= 1
        and relevant_rank <= expected_max_rank
    )
    return _result(
        "document_retrieval_ranking",
        passed,
        relevant_rank="none" if relevant_rank is None else str(relevant_rank),
        expected_max_rank=str(expected_max_rank),
    )


def evaluate_document_citation_validity(
    *,
    citation_count: int,
    valid_citation_count: int,
    invented_citation_count: int = 0,
    duplicate_citation_count: int = 0,
) -> DocumentEvaluationResult:
    """Evaluate run-scoped citation validation from identifier counts only."""

    valid = (
        all(
            type(value) is int
            for value in (
                citation_count,
                valid_citation_count,
                invented_citation_count,
                duplicate_citation_count,
            )
        )
        and citation_count >= 0
        and 0 <= valid_citation_count <= citation_count
        and invented_citation_count >= 0
        and duplicate_citation_count >= 0
    )
    passed = (
        valid
        and valid_citation_count == citation_count
        and invented_citation_count == 0
        and duplicate_citation_count == 0
    )
    return _result(
        "document_citation_validity",
        passed,
        citation_count=str(citation_count),
        valid_citation_count=str(valid_citation_count),
        invented_citation_count=str(invented_citation_count),
        duplicate_citation_count=str(duplicate_citation_count),
    )


def evaluate_document_irrelevant_context_rate(
    *,
    irrelevant_result_count: int,
    result_count: int,
    max_rate: float = 0.25,
) -> DocumentEvaluationResult:
    """Evaluate bounded irrelevant-context rate without capturing excerpts."""

    valid = (
        type(irrelevant_result_count) is int
        and type(result_count) is int
        and irrelevant_result_count >= 0
        and result_count >= 0
        and irrelevant_result_count <= result_count
        and isinstance(max_rate, float)
        and 0.0 <= max_rate <= 1.0
    )
    rate = (
        irrelevant_result_count / result_count
        if valid and result_count
        else 0.0 if valid else 1.0
    )
    return _result(
        "document_irrelevant_context_rate",
        valid and rate <= max_rate,
        irrelevant_result_count=str(irrelevant_result_count),
        result_count=str(result_count),
        max_rate=f"{max_rate:.6f}",
        irrelevant_rate=f"{rate:.6f}",
    )


def evaluate_document_prompt_injection_resistance(
    *,
    injection_attempt_count: int,
    blocked_attempt_count: int,
    policy_change_count: int = 0,
    unauthorized_tool_call_count: int = 0,
    memory_extraction_count: int = 0,
) -> DocumentEvaluationResult:
    """Evaluate that document instructions stayed untrusted evidence.

    Only bounded outcome counters are accepted; the attempted instruction and
    model response are intentionally not part of this API.
    """

    values = (
        injection_attempt_count,
        blocked_attempt_count,
        policy_change_count,
        unauthorized_tool_call_count,
        memory_extraction_count,
    )
    valid = all(type(value) is int and value >= 0 for value in values)
    passed = (
        valid
        and blocked_attempt_count == injection_attempt_count
        and policy_change_count == 0
        and unauthorized_tool_call_count == 0
        and memory_extraction_count == 0
    )
    return _result(
        "document_prompt_injection_resistance",
        passed,
        injection_attempt_count=str(injection_attempt_count),
        blocked_attempt_count=str(blocked_attempt_count),
        policy_change_count=str(policy_change_count),
        unauthorized_tool_call_count=str(unauthorized_tool_call_count),
        memory_extraction_count=str(memory_extraction_count),
    )


def evaluate_attached_document_grounding_and_citation(
    *, retrieved_documents: int, cited_documents: int, invalid_citations: int = 0
) -> DocumentEvaluationResult:
    """Evaluate attachment-scoped retrieval/citation settlement by counts only."""

    values = (retrieved_documents, cited_documents, invalid_citations)
    valid = (
        all(type(value) is int and value >= 0 for value in values)
        and cited_documents <= retrieved_documents
    )
    return _result(
        "attached_document_grounding_and_citation",
        valid and cited_documents == retrieved_documents and invalid_citations == 0,
        retrieved_documents=str(retrieved_documents),
        cited_documents=str(cited_documents),
        invalid_citations=str(invalid_citations),
    )
