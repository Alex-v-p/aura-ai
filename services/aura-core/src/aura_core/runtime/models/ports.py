"""Inward-facing model ports. Providers implement these protocols."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from aura_core.domains.execution.runs.ports import ChatMessage


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    display_name: str
    provider: str
    capabilities: tuple[str, ...]
    availability: str = "available"
    selectable: bool = True
    disabled_reason: str | None = None


class ChatModelPort(Protocol):
    async def list_models(self) -> Sequence[ModelDescriptor]: ...

    def stream_chat(self, model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...

    async def is_ready(self, model_id: str | None = None) -> bool: ...
