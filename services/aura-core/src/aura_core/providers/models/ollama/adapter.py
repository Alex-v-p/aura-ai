"""Ollama HTTP adapter; no Ollama types cross the Core boundary."""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import httpx

from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor


class OllamaUnavailable(RuntimeError):
    """Raised when the configured provider cannot be reached."""


class OllamaAdapter:
    def __init__(self, endpoint: str, timeout_seconds: float = 30.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)

    async def list_models(self) -> Sequence[ModelDescriptor]:
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaUnavailable("model inventory unavailable") from exc
        descriptors: list[ModelDescriptor] = []
        raw_models: Any = payload["models"] if "models" in payload else []
        for item in cast(list[Any], raw_models):
            if not isinstance(item, dict):
                continue
            item_map = cast(dict[str, Any], item)
            model_name: Any = item_map["name"] if "name" in item_map else None
            if not isinstance(model_name, str):
                continue
            model_id = model_name
            capabilities = await self._capabilities(model_id)
            chat_capable = "chat" in capabilities or "completion" in capabilities
            descriptors.append(
                ModelDescriptor(
                    id=model_id,
                    display_name=model_id,
                    provider="ollama",
                    capabilities=tuple(sorted(capabilities)),
                    availability="available",
                    selectable=chat_capable,
                    disabled_reason=None
                    if chat_capable
                    else "model does not advertise chat capability",
                )
            )
        return descriptors

    async def _capabilities(self, model_id: str) -> set[str]:
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                response = await client.post("/api/show", json={"name": model_id})
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
        except httpx.HTTPError, ValueError:
            return set()
        if "capabilities" not in payload:
            # Older Ollama versions predate this field but do support native
            # chat. An explicit empty list is authoritative and must not use
            # that compatibility fallback.
            return {"chat"}
        capabilities = payload["capabilities"]
        if isinstance(capabilities, list):
            return {value for value in cast(list[Any], capabilities) if isinstance(value, str)}
        return set()

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        body = {
            "model": model_id,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                async with client.stream("POST", "/api/chat", json=body) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            payload = cast(dict[str, Any], json.loads(line))
                        except json.JSONDecodeError:
                            raise OllamaUnavailable("provider returned malformed stream") from None
                        message = cast(dict[str, Any] | None, payload.get("message"))
                        if isinstance(message, dict) and isinstance(message.get("content"), str):
                            yield message["content"]
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("model inference unavailable") from exc

    async def is_ready(self, model_id: str | None = None) -> bool:
        try:
            models = await self.list_models()
        except OllamaUnavailable:
            return False
        return model_id is None or any(
            model.id == model_id and model.selectable for model in models
        )
