import json
from typing import cast

import httpx
import pytest
from aura_core.providers.models.ollama.adapter import OllamaAdapter, OllamaUnavailable


@pytest.mark.asyncio
async def test_ollama_discovery_and_streaming() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "chat"}, {"name": "embed"}]})
        if request.url.path == "/api/show":
            name = cast(str, json.loads(request.content)["name"])
            return httpx.Response(
                200, json={"capabilities": ["chat"] if name == "chat" else ["embedding"]}
            )
        if request.url.path == "/api/chat":
            return httpx.Response(
                200, content=(json.dumps({"message": {"content": "Hi"}}) + "\n").encode()
            )
        return httpx.Response(404)

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    # Replace the constructor boundary for a deterministic provider test.
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        models = await adapter.list_models()
        assert models[0].selectable is True
        assert models[1].selectable is False
        chunks = [chunk async for chunk in adapter.stream_chat("chat", [])]
        assert chunks == ["Hi"]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_missing_capabilities_uses_legacy_chat_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "legacy"}, {"name": "explicit-empty"}]}
            )
        name = cast(str, json.loads(request.content)["name"])
        return httpx.Response(200, json={} if name == "legacy" else {"capabilities": []})

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        legacy, explicit_empty = await adapter.list_models()
        assert legacy.selectable is True
        assert legacy.capabilities == ("chat",)
        assert explicit_empty.selectable is False
        assert explicit_empty.capabilities == ()
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_stream_rejects_malformed_provider_frames_without_leaking_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            content=b'{"message":{"content":"partial"}}\nnot-json\n',
        )

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable, match="malformed stream") as error:
            _ = [chunk async for chunk in adapter.stream_chat("chat", [])]
        assert "private-ollama" not in str(error.value)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_inventory_failure_is_provider_safe() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(503, text="upstream details with private URL")

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable, match="model inventory unavailable") as error:
            await adapter.list_models()
        assert "private-ollama" not in str(error.value)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]
