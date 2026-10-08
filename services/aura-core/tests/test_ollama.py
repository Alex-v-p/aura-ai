import asyncio
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
    inventory_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        inventory_paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "chat",
                            "digest": "a" * 64,
                            "modified_at": "chat-rev",
                            "capabilities": ["chat"],
                            "details": {},
                        },
                        {
                            "name": "embed",
                            "digest": "b" * 64,
                            "modified_at": "embed-rev",
                            "capabilities": ["embedding"],
                            "details": {"embedding_length": 3},
                        },
                    ]
                },
            )
        if request.url.path == "/api/show":
            name = cast(str, json.loads(request.content)["name"])
            return httpx.Response(
                200, json={"capabilities": ["chat"] if name == "chat" else ["embedding"]}
            )
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
        if request.url.path == "/api/chat":
            body = cast(dict[str, object], json.loads(request.content))
            if "format" in body:
                return httpx.Response(
                    200, json={"message": {"content": '{"ok":true}'}}
                )
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
        assert models[1].selectable is True
        assert "structured_output" not in models[0].capabilities
        assert models[0].model_digest == "a" * 64
        assert models[1].dimension == 3
        assert inventory_paths == ["/api/tags"]
        verified = await adapter.verify_model("chat", "structured_output")
        assert verified is not None
        assert "structured_output" in verified.capabilities
        chunks = [chunk async for chunk in adapter.stream_chat("chat", [])]
        assert chunks == ["Hi"]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_inventory_cache_and_legacy_show_failure_isolated() -> None:
    tags_calls = 0
    show_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tags_calls
        if request.url.path == "/api/tags":
            tags_calls += 1
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "chat", "digest": "a" * 64, "modified_at": "chat-rev"},
                        {"name": "broken", "digest": "b" * 64, "modified_at": "broken-rev"},
                        {"name": "embed", "digest": "c" * 64, "modified_at": "embed-rev"},
                    ]
                },
            )
        assert request.url.path == "/api/show"
        name = cast(str, json.loads(request.content)["name"])
        show_calls.append(name)
        if name == "broken":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "capabilities": ["chat"] if name == "chat" else ["embedding"],
                "details": {"embedding_length": 3} if name == "embed" else {},
            },
        )

    adapter = OllamaAdapter("https://ollama.test", inventory_cache_ttl_seconds=15)
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        first = await adapter.list_models()
        second = await adapter.list_models()
        assert first == second
        assert tags_calls == 1
        assert sorted(show_calls) == ["broken", "chat", "embed"]
        assert next(item for item in first if item.id == "chat").selectable
        broken = next(item for item in first if item.id == "broken")
        assert broken.capabilities == ()
        assert broken.selectable is False
        assert next(item for item in first if item.id == "embed").dimension == 3
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_selected_verification_is_cached_by_digest_and_embedding_probe_is_non_empty(
) -> None:
    tags_calls = 0
    structured_calls = 0
    embedding_calls = 0
    digest = "a" * 64

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tags_calls, structured_calls, embedding_calls
        if request.url.path == "/api/tags":
            tags_calls += 1
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "chat",
                            "digest": digest,
                            "modified_at": "chat-rev",
                            "capabilities": ["chat", "structured_output"],
                            "details": {},
                        },
                        {
                            "name": "embed",
                            "digest": "b" * 64,
                            "modified_at": "embed-rev",
                            "capabilities": ["embedding"],
                        },
                    ]
                },
            )
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["embedding"], "details": {}})
        if request.url.path == "/api/chat":
            structured_calls += 1
            return httpx.Response(200, json={"message": {"content": '{"ok":true}'}})
        if request.url.path == "/api/embed":
            embedding_calls += 1
            body = cast(dict[str, object], json.loads(request.content))
            assert body["input"] == "aura embedding dimension probe"
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
        raise AssertionError(request.url.path)

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        assert (await adapter.verify_model("chat", "structured_output")) is not None
        assert (await adapter.verify_model("chat", "structured_output")) is not None
        verified_embedding = await adapter.verify_model("embed", "embedding")
        assert verified_embedding is not None
        assert verified_embedding.dimension == 3
        assert structured_calls == 1
        assert embedding_calls == 1
        assert tags_calls == 1

        digest = "c" * 64
        await adapter.refresh_models()
        assert (await adapter.verify_model("chat", "structured_output")) is not None
        assert tags_calls == 2
        assert structured_calls == 2
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_failed_verification_retries_for_same_digest() -> None:
    calls = 0
    failures = 1

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, failures
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "chat",
                            "digest": "a" * 64,
                            "modified_at": "chat-rev",
                            "capabilities": ["chat", "structured_output"],
                            "details": {},
                        }
                    ]
                },
            )
        assert request.url.path == "/api/chat"
        calls += 1
        if failures:
            failures -= 1
            return httpx.Response(200, json={"message": {"content": "not-json"}})
        return httpx.Response(200, json={"message": {"content": '{"ok":true}'}})

    metrics = MetadataMetrics()
    context = ProviderTraceContext(trace_id="c" * 32, span_id="d" * 16)
    adapter = OllamaAdapter("https://ollama.test", telemetry=metrics)
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        assert await adapter.verify_model("chat", "structured_output", context=context) is None
        assert await adapter.verify_model("chat", "structured_output", context=context) is not None
        assert await adapter.verify_model("chat", "structured_output", context=context) is not None
        assert calls == 2
        verification_spans = [
            item
            for item in metrics.snapshot()
            if item.kind == "span"
            and dict(item.trace_attributes).get("operation") == "model.capability.verify"
        ]
        assert verification_spans
        assert all(item.trace_id == context.trace_id for item in verification_spans)
        assert all(item.parent_span_id == context.span_id for item in verification_spans)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_refresh_is_atomic_and_cache_expiry_is_observed() -> None:
    calls = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/api/tags"
        calls += 1
        call_number = calls
        if call_number == 1:
            first_started.set()
            await release_first.wait()
        digest = "a" * 64 if call_number == 1 else "b" * 64
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "chat",
                        "digest": digest,
                        "modified_at": str(call_number),
                        "capabilities": ["chat"],
                        "details": {},
                    }
                ]
            },
        )

    adapter = OllamaAdapter("https://ollama.test", inventory_cache_ttl_seconds=0.01)
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        listing = asyncio.create_task(adapter.list_models())
        await first_started.wait()
        refresh = asyncio.create_task(adapter.refresh_models())
        await asyncio.sleep(0)
        release_first.set()
        first, fresh = await asyncio.gather(listing, refresh)
        assert first[0].model_digest == "a" * 64
        assert fresh[0].model_digest == "b" * 64
        await asyncio.sleep(0.02)
        expired = await adapter.list_models()
        assert expired[0].model_digest == "b" * 64
        assert calls == 3
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_legacy_show_fallback_has_bounded_concurrency() -> None:
    active = 0
    maximum = 0
    names = [f"legacy-{index}" for index in range(8)]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": name, "digest": "a" * 64, "modified_at": name}
                        for name in names
                    ]
                },
            )
        assert request.url.path == "/api/show"
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json={"capabilities": ["chat"], "details": {}})

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        models = await adapter.list_models()
        assert len(models) == len(names)
        assert 1 < maximum <= 4
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_missing_capabilities_fail_closed_without_provider_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "legacy", "digest": "c" * 64},
                        {"name": "explicit-empty", "digest": "d" * 64},
                    ]
                },
            )
        name = cast(str, json.loads(request.content)["name"])
        return httpx.Response(200, json={} if name == "legacy" else {"capabilities": []})

    adapter = OllamaAdapter("https://ollama.test")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        legacy, explicit_empty = await adapter.list_models()
        assert legacy.selectable is False
        assert legacy.capabilities == ()
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
async def test_ollama_inventory_rejects_oversized_metadata_without_provider_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, content=b"{" + b"x" * (2 * 1024 * 1024) + b"}")

    adapter = OllamaAdapter("https://private-ollama.invalid")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
    try:
        with pytest.raises(OllamaUnavailable, match="model inventory unavailable") as error:
            await adapter.list_models()
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
        assert "private-ollama" not in repr(error.value)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_inventory_telemetry_classifies_provider_timeout_and_validation() -> None:
    failures = (
        (httpx.Response(503), "provider"),
        (httpx.Response(200, content=b"[]"), "validation"),
    )
    original = httpx.AsyncClient
    try:
        for response, error_class in failures:
            async def handler(
                request: httpx.Request, result: httpx.Response = response
            ) -> httpx.Response:
                assert request.url.path == "/api/tags"
                return result

            metrics = MetadataMetrics()
            adapter = OllamaAdapter("https://ollama.test", telemetry=metrics)
            transport = httpx.MockTransport(handler)
            httpx.AsyncClient = lambda *args, _transport=transport, **kwargs: original(  # type: ignore[method-assign]
                *args, transport=_transport, **kwargs
            )
            with pytest.raises(OllamaUnavailable):
                await adapter.list_models()
            spans = [item for item in metrics.snapshot() if item.kind == "span"]
            assert spans
            assert dict(spans[-1].dimensions)["error_class"] == error_class

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("provider timeout", request=request)

        metrics = MetadataMetrics()
        adapter = OllamaAdapter("https://ollama.test", telemetry=metrics)
        transport = httpx.MockTransport(timeout_handler)
        httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
        with pytest.raises(OllamaUnavailable):
            await adapter.list_models()
        spans = [item for item in metrics.snapshot() if item.kind == "span"]
        assert spans
        assert dict(spans[-1].dimensions)["error_class"] == "timeout"
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
