"""Hybrid retrieval and provider-degradation evaluations for AURA-0068."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from aura_core.domains.knowledge.documents.evaluation import (
    COMPONENT_VERSION,
    DELETION_COMPONENT_ID,
    DOCUMENT_COMPONENT_VERSIONS,
    EVALUATOR_COMPONENTS,
    EVALUATOR_VERSIONS,
    PROCESSING_COMPONENT_ID,
    RETRIEVAL_COMPONENT_ID,
    TOOL_EXECUTION_COMPONENT_ID,
    TOOL_EXECUTION_COMPONENT_VERSION,
    UPLOAD_COMPONENT_ID,
    DocumentEvaluationResult,
    DocumentReplayAdapter,
    evaluate_deletion_completion,
    evaluate_document_chunk_recall,
    evaluate_document_citation_validity,
    evaluate_document_irrelevant_context_rate,
    evaluate_document_prompt_injection_resistance,
    evaluate_document_recall,
    evaluate_document_retrieval_ranking,
    evaluate_format_validation,
    evaluate_owner_isolation,
    evaluate_preview_boundedness,
    evaluate_processing_idempotency,
    evaluate_retrieval_relevance,
)
from aura_core.domains.knowledge.documents.public import (
    DocumentLifecycleEvent,
    DocumentMetrics,
    DocumentService,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.models.ports import EmbeddingResult, ProviderTraceContext
from fixtures.documents.runner import execute_labelled_retrieval, retained_payload_json

OWNER = ("https://issuer.example", "retrieval-owner")
INVALID_PREVIEW_CASES: tuple[tuple[int, int, bool, int], ...] = (
    (20_001, 25_000, True, 20_000),
    (100, 25_000, False, 20_000),
    (101, 100, False, 20_000),
    (-1, 100, False, 20_000),
    (100, 100, False, 0),
)


def test_document_preview_boundedness_evaluator_passes_without_content_capture() -> None:
    result = evaluate_preview_boundedness(
        preview_characters=20_000,
        total_characters=25_000,
        truncated=True,
    )

    assert result.outcome == "pass"
    assert result.score == 1.0
    assert result.component_version == COMPONENT_VERSION
    assert result.evaluator_version == EVALUATOR_VERSIONS["document_preview_boundedness"]
    assert result.metadata == {
        "preview_characters": "20000",
        "total_characters": "25000",
        "max_characters": "20000",
        "truncated": "true",
    }
    payload = result.payload()
    assert "text" not in payload
    assert all(value not in payload.values() for value in ("private evidence", "filename.txt"))


@pytest.mark.parametrize(
    ("preview_characters", "total_characters", "truncated", "max_characters"),
    INVALID_PREVIEW_CASES,
)
def test_document_preview_boundedness_evaluator_rejects_invalid_metadata(
    preview_characters: int,
    total_characters: int,
    truncated: bool,
    max_characters: int,
) -> None:
    result = evaluate_preview_boundedness(
        preview_characters=preview_characters,
        total_characters=total_characters,
        truncated=bool(truncated),
        max_characters=max_characters,
    )

    assert result.outcome == "fail"
    assert result.score == 0.0


def test_document_preview_boundedness_replay_key_is_deterministic() -> None:
    first = evaluate_preview_boundedness(
        preview_characters=100,
        total_characters=100,
        truncated=False,
    )
    second = evaluate_preview_boundedness(
        preview_characters=100,
        total_characters=100,
        truncated=False,
    )

    assert first.replay_key == second.replay_key


@pytest.mark.asyncio
async def test_labelled_document_quality_runner_executes_formats_and_retains_metadata_only(
) -> None:
    results, sensitive_values = await execute_labelled_retrieval()

    assert all(result.outcome == "pass" for result in results)
    assert {result.evaluator_id for result in results} == {
        "document_recall",
        "document_chunk_recall",
        "document_retrieval_ranking",
        "document_citation_validity",
        "document_irrelevant_context_rate",
        "document_prompt_injection_resistance",
    }
    assert all(
        result.component_id == EVALUATOR_COMPONENTS[result.evaluator_id]
        for result in results
    )
    component_versions = dict(DOCUMENT_COMPONENT_VERSIONS)
    component_versions[TOOL_EXECUTION_COMPONENT_ID] = TOOL_EXECUTION_COMPONENT_VERSION
    assert all(
        result.component_version == component_versions[result.component_id]
        for result in results
    )
    assert all(result.replay_key for result in results)
    retained = retained_payload_json(results)
    assert not any(value in retained for value in sensitive_values)
    assert not {
        "text",
        "excerpt",
        "filename",
        "query",
        "owner",
        "prompt",
        "response",
        "content",
    }.intersection(retained)


@pytest.mark.parametrize(
    ("evaluator", "kwargs"),
    [
        (
            evaluate_document_recall,
            {"relevant_document_count": 2, "retrieved_relevant_count": 1},
        ),
        (
            evaluate_document_chunk_recall,
            {"relevant_chunk_count": 2, "retrieved_relevant_chunk_count": 1},
        ),
        (evaluate_document_retrieval_ranking, {"relevant_rank": 4}),
        (
            evaluate_document_citation_validity,
            {"citation_count": 2, "valid_citation_count": 1, "invented_citation_count": 1},
        ),
        (
            evaluate_document_irrelevant_context_rate,
            {"irrelevant_result_count": 3, "result_count": 4},
        ),
        (
            evaluate_document_prompt_injection_resistance,
            {"injection_attempt_count": 2, "blocked_attempt_count": 1},
        ),
    ],
)
def test_document_quality_evaluators_fail_bounded_regressions(
    evaluator: Callable[..., DocumentEvaluationResult], kwargs: dict[str, object]
) -> None:
    result = evaluator(**kwargs)
    assert result.outcome == "fail"
    assert result.score == 0.0


def test_document_quality_evaluator_replay_keys_are_stable() -> None:
    first = evaluate_document_citation_validity(
        citation_count=1, valid_citation_count=1
    )
    second = evaluate_document_citation_validity(
        citation_count=1, valid_citation_count=1
    )
    assert first.replay_key == second.replay_key


def test_non_retrieval_evaluators_use_manifest_component_attribution() -> None:
    format_upload = evaluate_format_validation(accepted=True, expected=True)
    format_processing = evaluate_format_validation(
        accepted=True,
        expected=True,
        component_id=PROCESSING_COMPONENT_ID,
    )
    owner_upload = evaluate_owner_isolation(foreign_results=0)
    owner_deletion = evaluate_owner_isolation(
        foreign_results=0,
        component_id=DELETION_COMPONENT_ID,
    )
    processing = evaluate_processing_idempotency(ready_runs=2, chunk_counts=(3, 3))
    deletion = evaluate_deletion_completion(searchable_results=0, object_deleted=True)
    retrieval = evaluate_retrieval_relevance(relevant_rank=1, result_count=1)

    observed_versions = {
        result.component_id: result.component_version
        for result in (
            format_upload,
            format_processing,
            owner_upload,
            owner_deletion,
            processing,
            deletion,
            retrieval,
        )
    }
    assert observed_versions.items() <= DOCUMENT_COMPONENT_VERSIONS.items()
    assert format_upload.payload()["componentId"] == UPLOAD_COMPONENT_ID
    assert owner_deletion.payload()["componentId"] == DELETION_COMPONENT_ID
    assert retrieval.component_id == RETRIEVAL_COMPONENT_ID


@pytest.mark.asyncio
async def test_document_replay_adapter_reports_processing_component_version() -> None:
    service = DocumentService(MemoryDocumentRepository(), MemoryDocumentObjectStore())
    document = await service.upload(
        *OWNER,
        filename="replay.txt",
        media_type="text/plain",
        stream=b"replayable document",
    )
    ready = await service.process(document.id)
    event = DocumentLifecycleEvent(
        ready.id,
        ready.status,
        ready.version,
        ready.updated_at,
        "document.ready",
    )

    replay = await DocumentReplayAdapter(service).replay(event.event_id)

    assert replay["componentId"] == PROCESSING_COMPONENT_ID
    assert replay["componentVersion"] == DOCUMENT_COMPONENT_VERSIONS[PROCESSING_COMPONENT_ID]


def test_document_replay_adapter_rejects_retrieval_version() -> None:
    service = DocumentService(MemoryDocumentRepository(), MemoryDocumentObjectStore())

    with pytest.raises(ValueError, match="unsupported document processing"):
        DocumentReplayAdapter(service, component_version=COMPONENT_VERSION)


class SemanticEmbedder:
    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult:
        del context
        vector = (1.0, 0.0, 0.0) if "car" in text.casefold() else (0.0, 1.0, 0.0)
        return EmbeddingResult(
            vector=vector,
            model_id=model_id,
            model_revision="test-rev",
            dimension=2,
            model_digest="a" * 64,
            digest="b" * 64,
        )


class DegradedEmbedder(SemanticEmbedder):
    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult:
        if text == "automobile":
            raise TimeoutError("embedding provider unavailable")
        return await super().embed(model_id, text, context=context)


@pytest.mark.asyncio
async def test_hybrid_retrieval_uses_vector_similarity_for_semantic_matches() -> None:
    service = DocumentService(
        MemoryDocumentRepository(), MemoryDocumentObjectStore(), embedder=SemanticEmbedder()
    )
    document = await service.upload(
        *OWNER,
        filename="vehicle.txt",
        media_type="text/plain",
        stream=b"A car is parked in the garage.",
    )
    await service.process(document.id)
    hits = await service.search(*OWNER, "automobile")
    assert hits
    assert hits[0].document_id == document.id
    assert hits[0].score > 0


@pytest.mark.asyncio
async def test_vector_provider_degradation_preserves_bounded_lexical_retrieval() -> None:
    service = DocumentService(
        MemoryDocumentRepository(), MemoryDocumentObjectStore(), embedder=DegradedEmbedder()
    )
    document = await service.upload(
        *OWNER,
        filename="fallback.txt",
        media_type="text/plain",
        stream=b"automobile policy fallback",
    )
    await service.process(document.id)
    hits = await service.search(*OWNER, "automobile")
    assert hits and hits[0].document_id == document.id


@pytest.mark.asyncio
async def test_identity_mismatch_degradation_is_correlated_and_redacted() -> None:
    metrics = MetadataMetrics()
    service = DocumentService(
        MemoryDocumentRepository(),
        MemoryDocumentObjectStore(),
        embedder=SemanticEmbedder(),
        embedding_model="nomic-embed-text",
        metrics=cast(DocumentMetrics, metrics),
    )
    document = await service.upload(
        *OWNER,
        filename="private-marker.txt",
        media_type="text/plain",
        stream=b"automobile private evidence TOP_SECRET_MARKER",
    )
    await service.process(document.id)

    # Simulate a verified embedding-model rollout while old chunks remain.
    service.embedding_model = "qwen3-embedding:4b"
    context = ProviderTraceContext(
        trace_id="a" * 32,
        span_id="b" * 16,
        correlation_id="00000000-0000-0000-0000-000000000007",
    )
    hits = await service.search(*OWNER, "automobile", context=context)

    assert hits and hits[0].document_id == document.id
    assert metrics.stats().rejected == 0
    measurements = metrics.snapshot()
    search_spans = [
        measurement
        for measurement in measurements
        if measurement.kind == "span"
        and measurement.component_id == "aura.knowledge.document_retrieval"
        and dict(measurement.trace_attributes).get("operation") == "document.search"
    ]
    assert len(search_spans) == 1
    span = search_spans[0]
    assert span.trace_id == "a" * 32
    assert span.parent_span_id == "b" * 16
    assert dict(span.trace_attributes)["correlation_id"] == (
        "00000000-0000-0000-0000-000000000007"
    )
    dimensions = dict(span.dimensions)
    assert dimensions["outcome"] == "degraded"
    assert dimensions["degradation"] == "identity_mismatch"
    assert dimensions["error_class"] == "identity_mismatch"
    assert any(
        measurement.metric == "document_retrieval_duration_ms"
        and measurement.trace_id == "a" * 32
        for measurement in measurements
    )
    rendered = repr(measurements)
    assert "private-marker.txt" not in rendered
    assert "TOP_SECRET_MARKER" not in rendered
    assert "a" * 64 not in rendered
    assert "b" * 64 not in rendered


@pytest.mark.asyncio
async def test_retrieval_ranks_exact_relevant_evidence_beyond_initial_candidate_window() -> None:
    service = DocumentService(MemoryDocumentRepository(), MemoryDocumentObjectStore())
    for index in range(32):
        document = await service.upload(
            *OWNER,
            filename=f"filler-{index}.txt",
            media_type="text/plain",
            stream=b"common unrelated material",
        )
        await service.process(document.id)
    target = await service.upload(
        *OWNER,
        filename="target.txt",
        media_type="text/plain",
        stream=b"common rare retrieval target",
    )
    await service.process(target.id)
    hits = await service.search(*OWNER, "rare retrieval target", limit=1)
    assert hits and hits[0].document_id == target.id
