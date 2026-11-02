"""Provider/platform coverage for bounded image inference."""

# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportIndexIssue=false

import asyncio
import base64
import io
import json
from typing import Any, cast

import httpx
import pytest
from aura_core.providers.models.ollama.adapter import OllamaAdapter, OllamaUnavailable
from aura_core.providers.object_storage.image_inspection import (
    ImageInspectionError,
    PillowImageInspector,
)
from aura_core.runtime.models.ports import ChatImagePart, ChatMessage


def _client_for(handler: Any) -> tuple[Any, Any]:
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda *args, **kwargs: original(  # type: ignore[method-assign]
        *args, transport=transport, **kwargs
    )
    return original, httpx.AsyncClient


@pytest.mark.asyncio
async def test_ollama_normalizes_vision_capability_and_refreshes_before_verification() -> None:
    tags_calls = 0
    digest = "a" * 64

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tags_calls
        assert request.url.path == "/api/tags"
        tags_calls += 1
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "vision-model",
                        "digest": digest,
                        "modified_at": "revision-1",
                        "capabilities": ["chat", "multimodal"],
                    }
                ]
            },
        )

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        descriptor = (await adapter.list_models())[0]
        assert "vision" in descriptor.capabilities
        assert await adapter.verify_model("vision-model", "vision") == descriptor
        assert await adapter.verify_model("vision-model", "vision") is not None
        assert tags_calls == 3  # list plus one fresh descriptor per verification
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_serializes_normalized_image_parts_for_plain_and_tool_requests() -> None:
    bodies: list[dict[str, object]] = []
    image_bytes = b"normalized-image-bytes"

    async def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        bodies.append(body)
        return httpx.Response(
            200,
            content=(json.dumps({"message": {"content": "ok"}, "done": True}) + "\n").encode(),
        )

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        message = ChatMessage(
            "user", "inspect this", (ChatImagePart("image/png", image_bytes),)
        )
        assert [chunk async for chunk in adapter.stream_chat("vision-model", [message])] == ["ok"]
        _ = [
            event
            async for event in adapter.stream_chat_with_tools(
                "vision-model", [message], [{"type": "function"}]
            )
        ]
        assert len(bodies) == 2
        expected = base64.b64encode(image_bytes).decode("ascii")
        assert (
            cast(list[str], cast(dict[str, object], bodies[0]["messages"][0])["images"])
            == [expected]
        )
        assert (
            cast(list[str], cast(dict[str, object], bodies[1]["messages"][0])["images"])
            == [expected]
        )
        assert "media_type" not in json.dumps(bodies)
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_rejects_non_object_stream_frames_without_provider_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"[]\n")

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        with pytest.raises(OllamaUnavailable, match="malformed stream"):
            _ = [chunk async for chunk in adapter.stream_chat("vision-model", [])]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_keeps_text_only_requests_backward_compatible() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(cast(dict[str, object], json.loads(request.content)))
        return httpx.Response(
            200,
            content=(json.dumps({"message": {"content": "text"}, "done": True}) + "\n").encode(),
        )

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        message = ChatMessage("user", "plain text")
        assert [chunk async for chunk in adapter.stream_chat("text-model", [message])] == ["text"]
        messages = cast(list[dict[str, object]], bodies[0]["messages"])
        assert "images" not in messages[0]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_preserves_image_parts_for_title_synthesis() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(cast(dict[str, object], json.loads(request.content)))
        return httpx.Response(200, json={"message": {"content": "Image title"}})

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        image = ChatImagePart("image/webp", b"normalized")
        assert (
            await adapter.infer_title("vision-model", [ChatMessage("user", "title", (image,))])
            == "Image title"
        )
        messages = cast(list[dict[str, object]], bodies[0]["messages"])
        payload = messages[0]
        assert payload["images"] == [base64.b64encode(b"normalized").decode("ascii")]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_does_not_translate_cancellation_into_provider_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise asyncio.CancelledError

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            _ = [chunk async for chunk in adapter.stream_chat("vision-model", [])]
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ollama_rejects_vision_without_fresh_immutable_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "vision-model",
                        "capabilities": ["chat", "vision"],
                        "modified_at": "revision-1",
                    }
                ]
            },
        )

    adapter = OllamaAdapter("https://ollama.test")
    original, _ = _client_for(handler)
    try:
        assert await adapter.verify_model("vision-model", "vision") is None
    finally:
        httpx.AsyncClient = original  # type: ignore[method-assign]


def test_image_inspector_rejects_animation_and_emits_bounded_metadata_free_derivative() -> None:
    pil = pytest.importorskip("PIL.Image")
    image = pil.new("RGB", (3000, 1000), "red")
    source = io.BytesIO()
    image.save(source, format="JPEG", exif=b"private-exif")
    result = PillowImageInspector().inspect(source.getvalue(), declared_media_type="image/jpeg")
    assert result.width == 3000
    assert result.height == 1000
    assert len(result.derivative_bytes) > 0
    with pil.open(io.BytesIO(result.derivative_bytes)) as derivative:
        assert max(derivative.size) <= 2048
        assert derivative.getexif() == {}

    animated = io.BytesIO()
    first = pil.new("RGB", (2, 2), "red")
    second = pil.new("RGB", (2, 2), "blue")
    first.save(animated, format="WEBP", save_all=True, append_images=[second], duration=10, loop=0)
    with pytest.raises(ImageInspectionError) as raised:
        PillowImageInspector().inspect(animated.getvalue(), declared_media_type="image/webp")
    assert raised.value.reason == "animated_image"
