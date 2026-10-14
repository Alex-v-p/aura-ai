"""Inward-facing protocols owned by run execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from aura_core.domains.execution.runs.dto import Run, RunClaim, RunError, RunStatus
from aura_core.domains.execution.runs.events import RunEvent

TITLE_RESPONSE_MAX_CHARS = 4096
TITLE_RESPONSE_MAX_BYTES = 16_384

if TYPE_CHECKING:
    from aura_core.domains.interaction.conversations.public import (
        Conversation,
        Message,
        MessageState,
        PendingTitle,
        TitleState,
    )


@dataclass(frozen=True, slots=True, init=False)
class ChatImagePart:
    """Bounded, provider-neutral image bytes for a model request.

    The image is already normalized by the attachment boundary.  This value
    object deliberately carries no filename, EXIF, object key, or attachment
    identifier so provider payloads cannot accidentally become a second
    content/metadata transport.  Providers may only receive the media type
    and normalized bytes.
    """

    media_type: str
    data: bytes = field(repr=False)

    MAX_BYTES = 16 * 1024 * 1024
    MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

    def __init__(
        self,
        media_type: str,
        data: bytes | None = None,
        *,
        normalized_bytes: bytes | None = None,
    ) -> None:
        if data is not None and normalized_bytes is not None:
            raise ValueError("provide image data only once")
        value = data if data is not None else normalized_bytes
        if value is None:
            raise ValueError("image data is required")
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "data", value)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.media_type not in self.MEDIA_TYPES:
            raise ValueError("unsupported image media type")
        if not self.data:
            raise ValueError("image data is required")
        if len(self.data) > self.MAX_BYTES:
            raise ValueError("image data exceeds model input limit")

    @property
    def normalized_bytes(self) -> bytes:
        """Name the invariant explicitly at provider call sites."""

        return self.data


# Public aliases keep the port discoverable from both execution and runtime
# model modules while retaining one concrete value object.
BoundedImagePart = ChatImagePart
ImagePart = ChatImagePart


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str
    image_parts: tuple[ChatImagePart, ...] = ()

    @property
    def images(self) -> tuple[ChatImagePart, ...]:
        """Compatibility alias for adapters that call the parts ``images``."""

        return self.image_parts


class RunRepository(Protocol):
    async def pending_title(self, run_id: UUID) -> PendingTitle | None: ...
    async def settle_title(self, run_id: UUID, title: str, state: TitleState) -> bool: ...

    async def request_cancel(
        self, run_id: UUID, subject: str, idempotency_key: str, issuer: str | None = None
    ) -> Run: ...
    async def start_run(
        self, run_id: UUID, *, worker_id: UUID | None = None, lease_seconds: float = 300.0
    ) -> RunClaim: ...
    async def context(
        self,
        conversation_id: UUID,
        subject: str,
        issuer: str,
        budget: int,
        agent_revision_id: UUID | None = None,
        trace_id: str | None = None,
        persona_revision_id: UUID | None = None,
        parent_span_id: str | None = None,
    ) -> list[tuple[str, str]]: ...
    async def find_run_any(self, run_id: UUID) -> tuple[Conversation, Run]: ...
    async def append_assistant(
        self,
        run_id: UUID,
        text: str,
        state: MessageState,
        *,
        attempt_id: UUID | None = None,
    ) -> Message: ...
    async def mark_document_search_used(
        self, run_id: UUID, *, attempt_id: UUID | None = None
    ) -> None: ...
    async def finish_run(
        self,
        run_id: UUID,
        status: RunStatus,
        error: RunError | None = None,
        *,
        attempt_id: UUID | None = None,
    ) -> tuple[Conversation, Run, Message | None]: ...
    async def wait_for_confirmation(
        self, run_id: UUID, attempt_id: UUID, approval_id: UUID, tool_call_id: UUID
    ) -> Run: ...
    async def resume_waiting_run(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        approved: bool,
    ) -> Run: ...


class AttachmentResolutionPort(Protocol):
    async def resolve_attachment_availability(self, conversation: Conversation) -> Conversation: ...


class ChatCompletionPort(Protocol):
    def stream_chat(self, model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...


class TitleInferencePort(Protocol):
    """Optional provider-neutral capability for bounded title requests."""

    async def infer_title(self, model_id: str, messages: Sequence[ChatMessage]) -> str: ...


class RunEventPort(Protocol):
    async def publish(self, event: RunEvent) -> None: ...
    async def history(self, run_id: UUID, after: UUID | None = None) -> list[RunEvent]: ...


class RunCommandPort(Protocol):
    async def publish_run(self, run_id: UUID, conversation_id: UUID) -> None: ...


class MetricsPort(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...
    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...
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


class NullMetrics:
    def increment(self, component: str, name: str, **attributes: str) -> None:
        del component, name, attributes

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
        del component, name, value, attributes

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
    ) -> None:
        del (
            component,
            operation,
            duration_ms,
            trace_id,
            span_id,
            parent_span_id,
            dependency,
            outcome,
            error_class,
            provider,
            run_id,
            conversation_id,
            trace_attributes,
        )
