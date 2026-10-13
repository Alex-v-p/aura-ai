"""AURA-0073 document embedding provider checks."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from aura_core.providers.embeddings.ollama.adapter import (
    OllamaEmbeddingAdapter,
    OllamaEmbeddingUnavailable,
)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


@pytest.mark.asyncio
async def test_document_embedding_requests_the_existing_vector_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            body = cast(dict[str, object], json.loads(request.content))
            requests.append(body)
            return httpx.Response(200, json={"embeddings": [[0.1] * 768]})
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3-embedding:4b",
                        "digest": "sha256:" + "a" * 64,
                        "modified_at": "2026-10-13T00:00:00Z",
                    }
                ]
            },
        )

    _patch_client(monkeypatch, handler)
    result = await OllamaEmbeddingAdapter(
        "https://ollama.test", dimension=768
    ).embed("qwen3-embedding:4b", "private document text")

    assert result.dimension == 768
    assert requests == [
        {
            "model": "qwen3-embedding:4b",
            "input": "private document text",
            "dimensions": 768,
        }
    ]


@pytest.mark.asyncio
async def test_document_embedding_rejects_wrong_dimension_without_content_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * 767]})
        raise AssertionError("artifact lookup must not follow a wrong-dimensional response")

    _patch_client(monkeypatch, handler)
    with pytest.raises(OllamaEmbeddingUnavailable, match="dimension mismatch") as error:
        await OllamaEmbeddingAdapter("https://ollama.test", dimension=768).embed(
            "qwen3-embedding:4b", "private document text"
        )
    assert "private document text" not in str(error.value)


@pytest.mark.asyncio
async def test_document_embedding_requires_exact_tagged_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * 768]})
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3-embedding:latest",
                        "digest": "sha256:" + "a" * 64,
                    }
                ]
            },
        )

    _patch_client(monkeypatch, handler)
    with pytest.raises(OllamaEmbeddingUnavailable, match="artifact unavailable"):
        await OllamaEmbeddingAdapter("https://ollama.test", dimension=768).embed(
            "qwen3-embedding:4b", "private document text"
        )


@pytest.mark.asyncio
async def test_memory_embedding_request_keeps_legacy_unbounded_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            requests.append(cast(dict[str, object], json.loads(request.content)))
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "memory-model", "digest": "sha256:" + "b" * 64}]},
        )

    _patch_client(monkeypatch, handler)
    result = await OllamaEmbeddingAdapter("https://ollama.test").embed(
        "memory-model", "private memory text"
    )

    assert result.dimension == 2
    assert requests == [{"model": "memory-model", "input": "private memory text"}]
