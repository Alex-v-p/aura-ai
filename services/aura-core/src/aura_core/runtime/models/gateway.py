"""Small gateway that keeps application code independent of adapters."""

from collections.abc import AsyncIterator, Sequence

from aura_core.runtime.models.ports import ChatMessage, ChatModelPort, ModelDescriptor


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
