"""Deterministic fake model provider for Core tests and local checks."""

from collections.abc import AsyncIterator, Sequence

from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor


class FakeChatModel:
    def __init__(
        self, chunks: Sequence[str] = ("Hello from Aura.",), model_id: str = "fake"
    ) -> None:
        self.chunks = tuple(chunks)
        self.model_id = model_id

    async def list_models(self) -> Sequence[ModelDescriptor]:
        return (ModelDescriptor(self.model_id, self.model_id, "fake", ("chat", "completion")),)

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        if model_id != self.model_id:
            return
        for chunk in self.chunks:
            yield chunk

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, self.model_id)
