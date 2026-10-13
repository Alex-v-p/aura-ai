"""Contract checks for the deterministic Compose embedding provider."""

from __future__ import annotations

import httpx
import pytest
from fake_provider import app


@pytest.mark.asyncio
async def test_fake_provider_embedding_matches_document_vector_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURA_E2E_PROVIDER", "ollama")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        tags = await client.get("/api/tags")
        response = await client.post(
            "/api/embed",
            json={
                "model": "qwen3-embedding:4b",
                "input": "bounded fixture",
                "dimensions": 768,
            },
        )

    assert tags.status_code == 200
    assert any(
        item.get("name") == "qwen3-embedding:4b"
        for item in tags.json().get("models", [])
    )
    assert response.status_code == 200
    body = response.json()
    vector = body["embeddings"][0]
    assert len(vector) == 768
    assert all(isinstance(value, float) for value in vector)
    assert body["model_revision"] == "fixture-qwen-embedding-v1"
