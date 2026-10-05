"""Public conversation state and read DTOs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from aura_core.domains.execution.runs.dto import Run, RunStatus


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Message:
    conversation_id: UUID
    role: MessageRole
    content: str
    state: MessageState = MessageState.COMPLETE
    run_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)


@dataclass(slots=True)
class Conversation:
    principal_issuer: str
    principal_subject: str
    title: str
    agent_profile_id: UUID
    agent_revision_id: UUID
    model_id: str
    version: int = 1
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    messages: list[Message] = field(default_factory=list[Message])
    runs: list[Run] = field(default_factory=list[Run])

    @property
    def current_run(self) -> Run | None:
        for run in reversed(self.runs):
            if run.status in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.CANCEL_REQUESTED,
            }:
                return run
        return None


@dataclass(frozen=True, slots=True)
class SeededAgent:
    profile_id: UUID
    revision_id: UUID
    policy_revision_id: UUID
    system_prompt: str
