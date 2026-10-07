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


class TitleState(StrEnum):
    """Internal lifecycle for model-derived conversation titles."""

    LEGACY = "legacy"
    PENDING = "pending"
    GENERATED = "generated"
    FALLBACK = "fallback"
    MANUAL = "manual"


class ConversationArchiveState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ConversationListFilters:
    """Safe, metadata-only filters for owner-scoped conversation listing."""

    q: str | None = None
    agent_profile_id: UUID | None = None
    model_id: str | None = None
    run_status: RunStatus | None = None
    archive_state: ConversationArchiveState = ConversationArchiveState.ACTIVE
    activity_from: datetime | None = None
    activity_to: datetime | None = None

    def normalized(self) -> ConversationListFilters:
        return ConversationListFilters(
            q=self.q.strip().casefold() if self.q and self.q.strip() else None,
            agent_profile_id=self.agent_profile_id,
            model_id=self.model_id.strip() if self.model_id else None,
            run_status=self.run_status,
            archive_state=self.archive_state,
            activity_from=_utc(self.activity_from),
            activity_to=_utc(self.activity_to),
        )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


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


@dataclass(frozen=True, slots=True)
class PendingTitle:
    """The first completed exchange eligible for title inference.

    This value object deliberately contains only the bounded inference input
    and the pinned model identifier.  It is never passed to telemetry.
    """

    conversation_id: UUID
    run_id: UUID
    model_id: str
    user_content: str
    assistant_content: str


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
    title_state: TitleState = TitleState.PENDING
    archived_at: datetime | None = None
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
