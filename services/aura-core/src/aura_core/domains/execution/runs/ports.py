"""Inward-facing protocols owned by run execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from aura_core.domains.execution.runs.dto import Run, RunClaim, RunError, RunStatus
from aura_core.domains.execution.runs.events import RunEvent

if TYPE_CHECKING:
    from aura_core.domains.interaction.conversations.public import (
        Conversation,
        Message,
        MessageState,
    )


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


class RunRepository(Protocol):
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
    async def finish_run(
        self,
        run_id: UUID,
        status: RunStatus,
        error: RunError | None = None,
        *,
        attempt_id: UUID | None = None,
    ) -> tuple[Conversation, Run, Message | None]: ...


class ChatCompletionPort(Protocol):
    def stream_chat(self, model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...


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
