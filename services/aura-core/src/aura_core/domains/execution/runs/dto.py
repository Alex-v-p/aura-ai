"""Public execution run DTOs and lifecycle values."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from aura_core.domains.interaction.conversations.dto import Conversation, Message


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class RunError:
    code: str
    message: str
    retryable: bool
    trace_id: str


@dataclass(slots=True)
class Run:
    conversation_id: UUID
    user_message_id: UUID
    agent_revision_id: UUID
    model_policy_revision_id: UUID
    provider: str
    model_id: str
    retry_of_run_id: UUID | None = None
    status: RunStatus = RunStatus.QUEUED
    id: UUID = field(default_factory=uuid4)
    assistant_message_id: UUID | None = None
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: RunError | None = None
    attempt_id: UUID | None = None
    attempt_count: int = 0
    lease_expires_at: datetime | None = None
    persona_revision_id: UUID | None = None
    prompt_bundle_revision_id: UUID | None = None
    prompt_hash: str | None = None
    # The exact immutable memory-policy revision admitted with this run.  It
    # is deliberately separate from the agent revision: retries must reuse the
    # run snapshot even when the agent's current configuration changes.
    memory_policy_revision_id: UUID | None = None
    memory_embedding_generation_id: UUID | None = None
    # Identifier-only recall metadata.  Memory text and vectors never belong
    # in a run DTO; the memory domain owns the durable snapshot projection.
    memory_recall_metadata: dict[str, object] | None = None


class RunClaim(NamedTuple):
    """Atomic result of acquiring a queued run lease."""

    conversation: Conversation
    run: Run
    user_message: Message
    acquired: bool


class RunClaimLost(RuntimeError):
    """Raised when a stale worker tries to mutate a run it no longer owns."""
