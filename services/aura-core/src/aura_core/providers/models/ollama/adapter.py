"""Ollama HTTP adapter; no Ollama types cross the Core boundary."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, cast

import httpx

from aura_core.domains.execution.runs.ports import (
    TITLE_RESPONSE_MAX_BYTES,
    TITLE_RESPONSE_MAX_CHARS,
)
from aura_core.runtime.models.ports import (
    ChatMessage,
    ModelDescriptor,
)

# Title inference is presentation metadata.  Keep the provider-side budget
# deliberately small even when a model ignores the neutral prompt's brevity.
TITLE_MAX_PREDICT = 32
TITLE_RAW_RESPONSE_MAX_BYTES = TITLE_RESPONSE_MAX_BYTES


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

    async def infer_title(self, model_id: str, messages: Sequence[ChatMessage]) -> str:
        """Run a bounded, non-streaming title request on the pinned model.

        This is intentionally a dedicated request rather than ``stream_chat``:
        title inference must not inherit interactive streaming defaults or a
        provider-selected model.  The caller supplies the run's exact pinned
        model identifier.
        """

        body = {
            "model": model_id,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": False,
            "think": False,
            "options": {"num_predict": TITLE_MAX_PREDICT},
        }
        raw_response, transport_status = await self._fetch_title_response(body)
        if transport_status != "ok":
            body = {}
            messages = ()
            raw_response = b""
            raise OllamaUnavailable(
                "title inference unavailable"
                if transport_status == "unavailable"
                else "title response exceeded configured limit"
            )

        title, response_status = self._decode_title_response(raw_response)
        raw_response = b""
        if response_status != "ok" or title is None:
            body = {}
            messages = ()
            title = ""
            raise OllamaUnavailable(
                "title response malformed"
                if response_status == "malformed"
                else "title response exceeded configured limit"
            )
        return title

    async def _fetch_title_response(
        self, body: Mapping[str, object]
    ) -> tuple[bytes, str]:
        """Fetch only a bounded response body and return a sanitized status."""

        raw_response = bytearray()
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                async with client.stream("POST", "/api/chat", json=body) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if len(raw_response) + len(chunk) > TITLE_RAW_RESPONSE_MAX_BYTES:
                            return b"", "oversized"
                        raw_response.extend(chunk)
        except Exception:
            # HTTP errors can retain response bodies; transport errors can
            # retain request data.  Return a status after the helper frame is
            # gone so neither can become part of the caller's exception graph.
            return b"", "unavailable"
        return bytes(raw_response), "ok"

    @staticmethod
    def _decode_title_response(raw_response: bytes) -> tuple[str | None, str]:
        """Decode and validate a title while keeping provider data local."""

        try:
            payload: Any = json.loads(raw_response)
            if not isinstance(payload, dict):
                return None, "malformed"
            payload_map = cast(dict[str, Any], payload)
            message = payload_map.get("message")
            if not isinstance(message, dict):
                return None, "malformed"
            message_map = cast(dict[str, Any], message)
            title = message_map.get("content")
            if not isinstance(title, str):
                return None, "malformed"
        except Exception:
            return None, "malformed"
        if (
            len(title) > TITLE_RESPONSE_MAX_CHARS
            or len(title.encode("utf-8")) > TITLE_RESPONSE_MAX_BYTES
        ):
            return None, "oversized"
        return title, "ok"
