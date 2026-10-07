"""Runtime request value objects."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: UUID
    conversation_id: UUID
    actor_subject: str


@dataclass(frozen=True, slots=True)
class CancelRequest:
    run_id: UUID
    actor_subject: str
