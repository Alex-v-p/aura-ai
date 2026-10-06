"""Public conversation commands, queries, DTOs, and errors."""

from aura_core.domains.interaction.conversations.context import build_context
from aura_core.domains.interaction.conversations.dto import (
    AgentAssignment,
    AssignmentReason,
    Conversation,
    Message,
    MessageRole,
    MessageState,
    PersonaAssignment,
    PersonaAssignmentReason,
    PersonaAssignmentSource,
    SeededAgent,
    now,
)
from aura_core.domains.interaction.conversations.repository import (
    ConversationRecord,
    SqlConversationRepository,
)
from aura_core.domains.interaction.conversations.store import (
    GENERAL_AGENT,
    ActiveRunConflict,
    AgentSwitchConfirmationRequired,
    AgentUnavailable,
    ConversationNotFound,
    ConversationStore,
    IdempotencyConflict,
    ModelUnavailable,
    PersonaUnavailable,
    VersionConflict,
)

__all__ = [
    "GENERAL_AGENT",
    "AgentAssignment",
    "AssignmentReason",
    "PersonaAssignment",
    "PersonaAssignmentReason",
    "PersonaAssignmentSource",
    "ActiveRunConflict",
    "AgentSwitchConfirmationRequired",
    "AgentUnavailable",
    "PersonaUnavailable",
    "Conversation",
    "ConversationNotFound",
    "ConversationRecord",
    "ConversationStore",
    "IdempotencyConflict",
    "Message",
    "MessageRole",
    "MessageState",
    "ModelUnavailable",
    "SeededAgent",
    "SqlConversationRepository",
    "VersionConflict",
    "build_context",
    "now",
]
