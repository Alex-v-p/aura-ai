"""Public conversation state and read DTOs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from aura_core.domains.execution.runs.dto import Run, RunStatus


class AssignmentReason(StrEnum):
    """Why an immutable agent assignment became effective."""

    INITIAL = "initial"
    MANUAL_SWITCH = "manual_switch"
    REVISION_UPGRADE = "revision_upgrade"


class PersonaAssignmentSource(StrEnum):
    """Where the effective conversation persona came from."""

    AGENT_DEFAULT = "agent_default"
    CONVERSATION_OVERRIDE = "conversation_override"


class PersonaAssignmentReason(StrEnum):
    INITIAL = "initial"
    AGENT_SWITCH = "agent_switch"
    AGENT_REVISION_UPGRADE = "agent_revision_upgrade"
    MANUAL_OVERRIDE = "manual_override"
    RESET_TO_AGENT_DEFAULT = "reset_to_agent_default"


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    """Conversation-owned assignment history record.

    Assignment history is deliberately owned by conversations.  Agent
    configuration owns revisions, but must not own the user-visible timeline
    that records when a conversation started using one.
    """

    agent_profile_id: UUID
    agent_revision_id: UUID
    reason: AssignmentReason
    effective_after_message_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class PersonaAssignment:
    """Immutable effective-persona history owned by a conversation."""

    persona_revision_id: UUID
    source: PersonaAssignmentSource
    reason: PersonaAssignmentReason
    effective_after_message_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


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
    persona_override_revision_id: UUID | None = None
    version: int = 1
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    messages: list[Message] = field(default_factory=list[Message])
    runs: list[Run] = field(default_factory=list[Run])
    assignments: list[AgentAssignment] = field(default_factory=list[AgentAssignment])
    persona_assignments: list[PersonaAssignment] = field(
        default_factory=list[PersonaAssignment]
    )

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
