"""Hybrid retrieval and provider-degradation evaluations for AURA-0068."""

from __future__ import annotations

import pytest
from aura_core.domains.knowledge.documents.public import (
    DocumentService,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.runtime.models.ports import EmbeddingResult, ProviderTraceContext

OWNER = ("https://issuer.example", "retrieval-owner")


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
