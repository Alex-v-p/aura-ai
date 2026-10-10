"""Transactional outbox and transport interfaces."""

from aura_core.platform.outbox.repository import SqlOutboxRepository
from aura_core.platform.outbox.service import (
    APPROVAL_EVENT_TOPIC,
    RUN_RESUME_TOPIC,
    InMemoryOutbox,
    OutboxCommand,
    OutboxTransport,
    TransactionalOutboxTransport,
    identifier_trace_metadata,
    make_aggregate_command,
    make_approval_event_command,
    make_approval_resume_command,
    make_identifier_command,
    make_run_command,
)

__all__ = [
    "InMemoryOutbox",
    "APPROVAL_EVENT_TOPIC",
    "OutboxCommand",
    "OutboxTransport",
    "RUN_RESUME_TOPIC",
    "SqlOutboxRepository",
    "TransactionalOutboxTransport",
    "identifier_trace_metadata",
    "make_aggregate_command",
    "make_approval_event_command",
    "make_approval_resume_command",
    "make_identifier_command",
    "make_run_command",
]
