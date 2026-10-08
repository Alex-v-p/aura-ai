import json
from typing import Any, cast

import httpx
import pytest
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.providers.embeddings.ollama.adapter import (
    OllamaEmbeddingAdapter,
    OllamaEmbeddingUnavailable,
)
from aura_core.providers.models.ollama.adapter import OllamaAdapter, OllamaUnavailable
from aura_core.runtime.models.ports import (
    ChatMessage,
    ProviderTraceContext,
    StructuredInferenceRequest,
)


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
        assert (
            await adapter.infer_title("qwen3:8b", [ChatMessage("system", "Return only a title")])
            == "A short title"
        )
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


@pytest.mark.asyncio
async def test_ollama_memory_inference_is_bounded_schema_constrained_and_non_streaming() -> None:
    seen: dict[str, object] = {}
    message_id = "7f7c9c1f-94d1-4d93-8cf0-0a37f9e3908b"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.update(cast(dict[str, object], json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "action": "create",
                            "content": "The owner prefers concise answers.",
                            "kind": "preference",
                            "scope_type": "user",
                            "confidence": 0.9,
                            "sensitivity": "ordinary",
                            "grounded_message_ids": [message_id],
                        }
                    )
                }
            },
        )

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        action = await adapter.infer(
            StructuredInferenceRequest(
                "qwen3:8b",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "confidence", "grounded_message_ids"],
                    "properties": {
                        "action": {"type": "string"},
                        "content": {"type": ["string", "null"]},
                        "kind": {"type": ["string", "null"]},
                        "scope_type": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "sensitivity": {"type": "string"},
                        "grounded_message_ids": {"type": "array"},
                    },
                },
                input={"user_content": "I prefer concise answers."},
            )
        )
        assert action["action"] == "create"
        assert action["grounded_message_ids"] == [message_id]
        assert seen["stream"] is False
        assert seen["think"] is False
        schema = cast(dict[str, object], seen["format"])
        assert schema["additionalProperties"] is False
        options = cast(dict[str, object], seen["options"])
        assert 0 < cast(int, options["num_predict"]) <= 8192
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_memory_inference_malformed_and_oversized_responses_are_content_safe() -> None:
    responses = [
        httpx.Response(200, json={"message": {"content": "not-json"}}),
        httpx.Response(200, content=("{" + "x" * 200_000).encode()),
    ]
    for response in responses:

        async def handler(
            request: httpx.Request, result: httpx.Response = response
        ) -> httpx.Response:
            assert request.url.path == "/api/chat"
            return result

        adapter = OllamaAdapter("https://private-ollama.invalid")
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def patched_client(
            *args: Any,
            _original: Any = original,
            _transport: Any = transport,
            **kwargs: Any,
        ) -> httpx.AsyncClient:
            return _original(*args, transport=_transport, **kwargs)

        httpx.AsyncClient = patched_client  # type: ignore[method-assign]
        try:
            with pytest.raises(OllamaUnavailable) as error:
                await adapter.infer(
                    StructuredInferenceRequest("qwen3:8b", input={"user_content": "private"})
                )
            assert "private-ollama" not in str(error.value)
            assert error.value.__cause__ is None
            assert error.value.__context__ is None
        finally:
            httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_embedding_returns_finite_vector_metadata_without_text_leakage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3-embedding:4b", "digest": "sha256:" + "a" * 64}]},
            )
        assert request.url.path == "/api/embed"
        body = cast(dict[str, object], json.loads(request.content))
        assert body["model"] == "qwen3-embedding:4b"
        assert body["input"] == "private memory text"
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    adapter = OllamaEmbeddingAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        result = await adapter.embed("qwen3-embedding:4b", "private memory text")
        assert result.vector == (0.1, 0.2, 0.3)
        assert result.dimension == 3
        assert result.model_digest == "a" * 64
        assert len(result.digest) == 64
        assert "private memory text" not in repr(result)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_embedding_fails_closed_for_ambiguous_model_inventory() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "embedder", "digest": "sha256:" + "a" * 64},
                    {"name": "embedder", "digest": "sha256:" + "b" * 64},
                ]
            },
        )

    adapter = OllamaEmbeddingAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaEmbeddingUnavailable, match="artifact unavailable"):
            await adapter.embed("embedder", "safe text")
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_provider_telemetry_is_correlated_and_metadata_only() -> None:
    context = ProviderTraceContext(
        trace_id="a" * 32,
        span_id="b" * 16,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": '{"answer":"ok"}'}})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3-embedding:4b", "digest": "sha256:" + "a" * 64}]},
            )
        assert request.url.path == "/api/embed"
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    metrics = MetadataMetrics()
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        await OllamaAdapter("https://ollama.test", telemetry=metrics).infer(
            StructuredInferenceRequest(
                "qwen3:8b",
                schema={"type": "object", "properties": {"answer": {"type": "string"}}},
                input={"private": "prompt text"},
                trace=context,
            )
        )
        await OllamaEmbeddingAdapter("https://ollama.test", telemetry=metrics).embed(
            "qwen3-embedding:4b", "private memory text", context=context
        )
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]

    measurements = metrics.snapshot()
    assert {item.metric for item in measurements} == {
        "structured_inference_duration_ms",
        "embedding_duration_ms",
    }
    assert all(item.trace_id == context.trace_id for item in measurements)
    rendered = repr(measurements)
    assert "prompt text" not in rendered
    assert "private memory text" not in rendered
