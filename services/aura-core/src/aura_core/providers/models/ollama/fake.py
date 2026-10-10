"""Deterministic fake model provider for Core tests and local checks."""

# ruff: noqa: E501

from collections.abc import AsyncIterator, Sequence

from aura_core.runtime.models.ports import (
    ChatMessage,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCall,
)


class FakeChatModel:
    def __init__(
        self,
        chunks: Sequence[str] = ("Hello from Aura.",),
        model_id: str = "fake",
        tool_events: Sequence[object] = (),
        tool_calling: bool = False,
    ) -> None:
        self.chunks = tuple(chunks)
        self.model_id = model_id
        self.tool_events = tuple(tool_events)
        self.tool_calling = tool_calling

    async def list_models(self) -> Sequence[ModelDescriptor]:
        capabilities = ("chat", "completion", "tools", "tool_calling") if self.tool_calling else ("chat", "completion")
        return (ModelDescriptor(self.model_id, self.model_id, "fake", capabilities),)

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        if model_id != self.model_id:
            return
        for chunk in self.chunks:
            yield chunk

    async def stream_chat_with_tools(self, model_id: str, messages: Sequence[ChatMessage], tools: Sequence[object]) -> AsyncIterator[object]:
        del messages, tools
        if model_id != self.model_id:
            return
        for event in self.tool_events:
            if isinstance(event, (ModelTextDelta, ModelToolCall, ModelStreamCompleted)):
                yield event
            elif isinstance(event, str):
                yield ModelTextDelta(event)
        if not self.tool_events:
            yield ModelStreamCompleted()

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, self.model_id)
