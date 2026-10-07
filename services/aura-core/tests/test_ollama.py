import json
from typing import cast

import httpx
import pytest
from aura_core.providers.models.ollama.adapter import OllamaAdapter, OllamaUnavailable
from aura_core.runtime.models.ports import ChatMessage


def _traceback_locals(exception: BaseException) -> str:
    values: list[str] = []
    traceback = exception.__traceback__
    while traceback is not None:
        values.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return " ".join(values)


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


@pytest.mark.asyncio
async def test_ollama_title_request_is_pinned_bounded_non_streaming_and_non_thinking() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.update(cast(dict[str, object], json.loads(request.content)))
        return httpx.Response(200, json={"message": {"content": "A short title"}})

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        assert await adapter.infer_title(
            "qwen3:8b", [ChatMessage("system", "Return only a title")]
        ) == "A short title"
        assert seen["model"] == "qwen3:8b"
        assert seen["stream"] is False
        assert seen["think"] is False
        options = cast(dict[str, object], seen["options"])
        assert 0 < cast(int, options["num_predict"]) <= 64
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_title_rejects_malformed_response_without_provider_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={"message": {"content": 42}})

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable, match="title response malformed") as error:
            await adapter.infer_title("qwen3:8b", [])
        assert "private-ollama" not in str(error.value)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_title_http_failure_has_no_provider_exception_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(502, text="PRIVATE_PROVIDER_RESPONSE")

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable) as error:
            await adapter.infer_title(
                "qwen3:8b", [ChatMessage("user", "PRIVATE_TRANSCRIPT_REQUEST")]
            )
        exception = error.value
        assert str(exception) == "title inference unavailable"
        assert exception.__cause__ is None
        assert exception.__context__ is None
        assert "PRIVATE_PROVIDER_RESPONSE" not in repr(exception)
        trace_locals = _traceback_locals(exception)
        assert "PRIVATE_PROVIDER_RESPONSE" not in trace_locals
        assert "PRIVATE_TRANSCRIPT_REQUEST" not in trace_locals
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_title_rejects_oversized_raw_response_before_json_parsing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            content=("{'PRIVATE_TITLE_RESPONSE':" + "x" * 20_000).encode(),
        )

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable) as error:
            await adapter.infer_title(
                "qwen3:8b", [ChatMessage("user", "PRIVATE_TRANSCRIPT_REQUEST")]
            )
        exception = error.value
        assert str(exception) == "title response exceeded configured limit"
        assert exception.__cause__ is None
        assert exception.__context__ is None
        assert "PRIVATE_TITLE_RESPONSE" not in repr(exception)
        trace_locals = _traceback_locals(exception)
        assert "PRIVATE_TITLE_RESPONSE" not in trace_locals
        assert "PRIVATE_TRANSCRIPT_REQUEST" not in trace_locals
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]
