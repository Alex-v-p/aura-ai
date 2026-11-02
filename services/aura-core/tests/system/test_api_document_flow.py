"""AURA-0073 assembled document flow regression checks."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


def _headers(csrf: str) -> dict[str, str]:
    return {
        "Origin": "https://aura.dev.example",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": str(uuid4()),
    }


@pytest.mark.asyncio
async def test_api_document_flow_uses_qwen_model_and_preserves_locator(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        service = api_app.state.aura.document_service
        assert service.embedding_model == "qwen3-embedding:4b"

        created = await client.post(
            "/api/v1/documents/uploads",
            headers=_headers(session.csrf_token),
            json={"filename": "qwen-flow.txt", "mediaType": "text/plain", "byteCount": 20},
        )
        assert created.status_code == 201
        upload_id = UUID(created.json()["id"])
        uploaded = await client.put(
            f"/api/v1/documents/uploads/{upload_id}/content",
            headers={
                **_headers(session.csrf_token),
                "Content-Type": "text/plain",
                "Content-Length": "20",
                "X-Expected-Version": "1",
            },
            content=b"qwen source locator!",
        )
        assert uploaded.status_code == 202
        document_id = UUID(uploaded.json()["document"]["id"])
        ready = await service.process(document_id)
        assert ready.status.value == "ready"

        search = await client.post(
            "/api/v1/documents/search",
            headers={
                "Origin": "https://aura.dev.example",
                "X-CSRF-Token": session.csrf_token,
            },
            json={"query": "source locator", "limit": 5},
        )
        assert search.status_code == 200
        hit = search.json()["items"][0]
        assert hit["documentId"] == str(document_id)
        assert hit["locator"]["kind"] == "section"
    finally:
        await client.aclose()
