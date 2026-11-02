"""Small gateway that keeps application code independent of adapters."""

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from inspect import signature
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from aura_core.runtime.models.ports import (
    ChatMessage,
    ChatModelPort,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelToolStreamEvent,
    ProviderTelemetryPort,
    ProviderTraceContext,
)


class _UnknownPreviewFailure(Exception):
    """Provider failure whose concrete class must not cross the preview port."""

    def __init__(self, provider: str = "unknown") -> None:
        super().__init__("preview provider unavailable")
        self.provider = provider


class _LegacyPreviewTimeout(TimeoutError):
    def __init__(self) -> None:
        super().__init__("preview timed out")
        self.provider = "unknown"


class _LegacyPreviewFailure(RuntimeError):
    def __init__(self) -> None:
        super().__init__("preview provider unavailable")
        self.provider = "unknown"


@dataclass(frozen=True, slots=True)
class ModelPreviewResult:
    content: str
    provider: str


class ModelPreviewFailure(RuntimeError):
    """Sanitized preview failure with bounded provider metadata."""

    def __init__(self, error_class: str, provider: str | None = None) -> None:
        super().__init__("preview provider unavailable")
        self.error_class = error_class
        self.provider = provider


def _bounded_provider(value: str | None) -> str:
    return value if value and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) else "unknown"


class ModelGateway:
    def __init__(
        self, provider: ChatModelPort, telemetry: ProviderTelemetryPort | None = None
    ) -> None:
        self.provider = provider
        self.telemetry = telemetry

    async def inventory(
        self, *, context: ProviderTraceContext | None = None
    ) -> Sequence[ModelDescriptor]:
        method = cast(Any, self.provider).list_models
        if "context" in signature(method).parameters:
            return await method(context=context)
        return await method()

    async def preview(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> str:
        try:
            result = await self.preview_with_metadata(
                model_id, system_prompt, prompt, context=context
            )
        except ModelPreviewFailure as exc:
            if exc.error_class == "timeout":
                raise _LegacyPreviewTimeout() from exc
            if exc.error_class == "unknown":
                raise _UnknownPreviewFailure("unknown") from exc
            raise _LegacyPreviewFailure() from exc
        return result.content

    async def preview_with_metadata(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> ModelPreviewResult:
        try:
            models = await self.inventory(context=context)
        except TimeoutError as exc:
            raise ModelPreviewFailure("timeout") from exc
        except RuntimeError as exc:
            raise ModelPreviewFailure("provider") from exc
        except Exception as exc:
            raise ModelPreviewFailure("unknown") from exc
        selected = next((item for item in models if item.id == model_id), None)
        if (
            selected is None
            or not selected.selectable
            or selected.availability != "available"
            or not {"chat", "completion"}.intersection(selected.capabilities)
        ):
            raise LookupError("selected model is unavailable")
        started = monotonic()
        provider = _bounded_provider(selected.provider)
        trace_id = context.trace_id if context and context.trace_id else uuid4().hex
        parent_span_id = context.span_id if context and context.span_id else None
        outcome = "ok"
        error_class: str | None = None
        chunks: list[str] = []
        size = 0
        try:
            async for chunk in self.provider.stream_chat(
                model_id,
                (ChatMessage("system", system_prompt), ChatMessage("user", prompt)),
            ):
                remaining = 32_768 - size
                if remaining <= 0:
                    break
                value = chunk[:remaining]
                chunks.append(value)
                size += len(value)
        except TimeoutError as exc:
            outcome, error_class = "error", "timeout"
            raise ModelPreviewFailure("timeout", provider) from exc
        except Exception as exc:
            outcome, error_class = "error", "provider"
            if isinstance(exc, RuntimeError):
                raise ModelPreviewFailure("provider", provider) from exc
            error_class = "unknown"
            raise ModelPreviewFailure("unknown", provider) from exc
        finally:
            if self.telemetry is not None:
                if error_class in {"provider", "timeout"}:
                    self.telemetry.increment(
                        "aura.runtime.model_inference",
                        "provider_errors",
                        trace_id=trace_id,
                        outcome=outcome,
                        error_class=error_class,
                        provider=provider,
                    )
                self.telemetry.record_span(
                    "aura.runtime.model_inference",
                    "model.infer",
                    max(0.0, (monotonic() - started) * 1000.0),
                    trace_id=trace_id,
                    span_id=uuid4().hex[:16],
                    parent_span_id=parent_span_id,
                    dependency="model_provider",
                    outcome=outcome,
                    error_class=error_class,
                    provider=provider,
                    model_id=model_id,
                )
        return ModelPreviewResult("".join(chunks), provider)

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
