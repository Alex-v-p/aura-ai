"""AURA-0073 document composition and retry checks."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import aura_core.bootstrap.memory_uow as memory_uow
import pytest
from aura_core.bootstrap.document_uow import document_service
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentChunk,
    DocumentService,
    DocumentStatus,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.platform.auth import Settings
from aura_core.runtime.models.ports import EmbeddingResult, ProviderTraceContext


@dataclass
class RecordingEmbedder:
    models: list[str]

    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult:
        del text, context
        self.models.append(model_id)
        return EmbeddingResult(
            vector=(0.1, 0.2),
            model_id=model_id,
            model_revision="revision",
            dimension=2,
            model_digest="a" * 64,
            digest="b" * 64,
        )


@dataclass
class GenerationEmbedder:
    model_id: str
    model_digest: str
    fail: bool = False

    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult:
        del text, context
        if self.fail:
            raise RuntimeError("embedding provider unavailable")
        assert model_id == self.model_id
        return EmbeddingResult(
            vector=(1.0, 0.0),
            model_id=self.model_id,
            model_revision="verified-revision",
            dimension=2,
            model_digest=self.model_digest,
            digest="b" * 64,
        )


@pytest.mark.asyncio
async def test_document_composition_uses_configured_qwen_model() -> None:
    service = document_service(
        settings=Settings(document_embedding_model="qwen3-embedding:4b"),
        testing=True,
    )
    assert service.embedding_model == "qwen3-embedding:4b"


@pytest.mark.asyncio
async def test_document_retry_reprocesses_with_the_configured_embedding_model() -> None:
    repository = MemoryDocumentRepository()
    store = MemoryDocumentObjectStore()
    embedder = RecordingEmbedder([])
    service = DocumentService(
        repository,
        store,
        embedder=embedder,
        embedding_model="qwen3-embedding:4b",
    )
    document = await service.upload(
        "https://issuer.example",
        "owner",
        filename="retry.txt",
        media_type="text/plain",
        stream=b"document retry evidence",
    )

    processed = await service.process(document.id)
    assert isinstance(processed, Document)
    assert processed.status is DocumentStatus.READY
    assert embedder.models == ["qwen3-embedding:4b"]


@pytest.mark.asyncio
async def test_retry_clears_failed_chunks_before_qwen_processing_and_search() -> None:
    repository = MemoryDocumentRepository()
    embedder = GenerationEmbedder("qwen3-embedding:4b", "a" * 64, fail=True)
    service = DocumentService(
        repository,
        MemoryDocumentObjectStore(),
        embedder=embedder,
        embedding_model="qwen3-embedding:4b",
    )
    document = await service.upload(
        *("https://issuer.example", "retry-owner"),
        filename="retry-qwen.txt",
        media_type="text/plain",
        stream=b"retry qwen evidence",
    )
    await repository.replace_chunks(
        document.id,
        [
            DocumentChunk(
                uuid4(), document.id, 1, "stale failed chunk", "section:1", "c" * 64,
                (0.0, 1.0), "nomic-embed-text", "old-revision", "d" * 64,
            )
        ],
    )

    failed = await service.process(document.id)
    assert failed.status is DocumentStatus.FAILED
    assert await repository.chunks(document.id) == []

    embedder.fail = False
    queued = await service.retry(
        "https://issuer.example", "retry-owner", document.id, expected_version=failed.version
    )
    assert queued.status is DocumentStatus.QUEUED
    ready = await service.process(document.id)
    assert ready.status is DocumentStatus.READY
    chunks = await repository.chunks(document.id)
    assert len(chunks) == 1
    assert chunks[0].model_id == "qwen3-embedding:4b"
    assert chunks[0].model_digest == "a" * 64
    hits = await service.search("https://issuer.example", "retry-owner", "qwen evidence")
    assert hits and hits[0].document_id == document.id


@pytest.mark.asyncio
async def test_hybrid_retrieval_excludes_old_model_vectors_but_keeps_lexical_fallback() -> None:
    repository = MemoryDocumentRepository()
    embedder = GenerationEmbedder("nomic-embed-text", "d" * 64)
    service = DocumentService(
        repository,
        MemoryDocumentObjectStore(),
        embedder=embedder,
        embedding_model="nomic-embed-text",
    )
    old = await service.upload(
        "https://issuer.example",
        "generation-owner",
        filename="old-model.txt",
        media_type="text/plain",
        stream=b"car policy from the old model",
    )
    await service.process(old.id)

    embedder.model_id = "qwen3-embedding:4b"
    embedder.model_digest = "a" * 64
    service.embedding_model = "qwen3-embedding:4b"
    current = await service.upload(
        "https://issuer.example",
        "generation-owner",
        filename="qwen-model.txt",
        media_type="text/plain",
        stream=b"vehicle policy from qwen",
    )
    await service.process(current.id)

    semantic_hits = await service.search(
        "https://issuer.example", "generation-owner", "automobile"
    )
    assert [hit.document_id for hit in semantic_hits] == [current.id]

    lexical_hits = await service.search("https://issuer.example", "generation-owner", "car")
    assert lexical_hits[0].document_id == old.id
    assert abs(lexical_hits[0].score - 0.65035) < 1e-9
    assert any(
        hit.document_id == current.id and abs(hit.score - 0.35) < 1e-9
        for hit in lexical_hits
    )


def test_memory_embedding_composition_does_not_inherit_document_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory providers keep their generation-selected vector dimensions."""

    constructor_kwargs: list[dict[str, object]] = []

    class RecordingAdapter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args
            constructor_kwargs.append(kwargs)

    monkeypatch.setattr(memory_uow, "OllamaEmbeddingAdapter", RecordingAdapter)
    settings = Settings(document_embedding_dimension=768)

    memory_uow.memory_processing_service(None, settings=settings)
    memory_uow.memory_recall_service(
        memory_uow.memory_repository(None, testing=True), settings=settings
    )

    assert len(constructor_kwargs) == 2
    assert all("dimension" not in kwargs for kwargs in constructor_kwargs)
