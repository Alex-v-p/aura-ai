"""Small gateway that keeps application code independent of adapters."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import cast

from aura_core.runtime.models.ports import (
    ChatMessage,
    ChatModelPort,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelToolStreamEvent,
)


class ModelGateway:
    def __init__(self, provider: ChatModelPort) -> None:
        self.provider = provider

    async def inventory(self) -> Sequence[ModelDescriptor]:
        return await self.provider.list_models()

    async def stream(self, model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        async for chunk in self.provider.stream_chat(model_id, messages):
            yield chunk

    async def ready(self, model_id: str | None = None) -> bool:
        return await self.provider.is_ready(model_id)

    def stream_with_tools(
        self,
        model_id: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, object]],
    ) -> AsyncIterator[ModelToolStreamEvent]:
        provider = self.provider
        method = getattr(provider, "stream_chat_with_tools", None)
        if not callable(method):
            async def unavailable() -> AsyncIterator[ModelToolStreamEvent]:
                if False:
                    yield ModelStreamCompleted()  # pragma: no cover
                return
            return unavailable()
        return cast(AsyncIterator[ModelToolStreamEvent], method(model_id, messages, tools))
