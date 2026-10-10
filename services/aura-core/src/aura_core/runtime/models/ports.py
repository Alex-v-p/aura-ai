"""Inward-facing model ports. Providers implement these protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from aura_core.domains.execution.runs.ports import ChatMessage

# Provider payloads are normalized at the adapter boundary.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    id: str
    display_name: str
    provider: str
    capabilities: tuple[str, ...]
    availability: str = "available"
    selectable: bool = True
    disabled_reason: str | None = None
    # Provider-observed identity.  A missing digest/revision is not replaced
    # by a local synthetic value at the memory product boundary.
    model_revision: str | None = None
    model_digest: str | None = None
    dimension: int | None = None


class ChatModelPort(Protocol):
    async def list_models(self) -> Sequence[ModelDescriptor]: ...

    def stream_chat(self, model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...

    async def is_ready(self, model_id: str | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelThinkingDelta:
    """Ephemeral provider-neutral model reasoning delta.

    Thinking is deliberately separate from visible text.  It is never an
    assistant message, a tool-protocol token, or a title input.  Transport
    adapters impose the live-only owner authorization and bounded offset
    rules; providers only normalize their native frame here.
    """

    text: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    name: str
    arguments: object


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    pass


ModelToolStreamEvent = ModelTextDelta | ModelThinkingDelta | ModelToolCall | ModelStreamCompleted
ModelStreamEvent = ModelTextDelta | ModelThinkingDelta | ModelStreamCompleted


class ToolCallingModelPort(Protocol):
    """Optional native-tool capability; provider payloads stay behind adapters."""

    def stream_chat_with_tools(
        self,
        model_id: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, object]],
    ) -> AsyncIterator[ModelToolStreamEvent]: ...


class ThinkingModelPort(Protocol):
    """Optional typed stream for providers that expose private reasoning."""

    def stream_chat_events(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[ModelStreamEvent]: ...


class ThinkingDeltaPublisher(Protocol):
    """Optional live-only sink for bounded owner-authorized thinking deltas."""

    async def publish_thinking_delta(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        offset: int,
        text: str,
        parent_span_id: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderTraceContext:
    """Optional metadata-only context propagated across provider calls.

    These identifiers are transport/runtime correlation metadata.  They carry
    no prompts, responses, vectors, or domain-specific action semantics.
    """

    trace_id: str | None = None
    span_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    command_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    generation_id: str | None = None


class ModelSelectionPort(Protocol):
    """Provider-neutral port for owner-selected memory model verification."""

    async def list_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> Sequence[ModelDescriptor]: ...

    async def refresh_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> Sequence[ModelDescriptor]: ...

    async def verify_model(
        self,
        model_id: str,
        capability: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> ModelDescriptor | None: ...


class ProviderTelemetryPort(Protocol):
    """Minimal injectable metadata-only telemetry sink for providers."""

    def increment(
        self,
        component: str,
        name: str,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None: ...

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **trace_attributes: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StructuredInferenceRequest:
    """Provider-neutral bounded request for schema-constrained JSON inference.

    ``input`` is deliberately an opaque JSON object.  Domain-specific action
    names, evidence rules, and policy decisions belong to the owning domain,
    not to this runtime port or a provider adapter.
    """

    model_id: str
    schema: Mapping[str, object] | None = None
    input: Mapping[str, object] = field(default_factory=dict)
    max_output_bytes: int = 32_768
    trace: ProviderTraceContext | None = None


class StructuredInferencePort(Protocol):
    async def infer(
        self, request: StructuredInferenceRequest
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    model_id: str
    model_revision: str | None
    dimension: int
    # Provider-verified artifact identity, distinct from the vector digest.
    model_digest: str
    digest: str


class EmbeddingPort(Protocol):
    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult: ...

    async def is_ready(self, model_id: str | None = None) -> bool: ...
