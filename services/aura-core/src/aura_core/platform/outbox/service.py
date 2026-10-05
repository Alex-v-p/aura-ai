"""Transactional outbox abstractions and an in-process transport."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4


class OutboxTransport(Protocol):
    async def publish(self, command: OutboxCommand) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class OutboxCommand:
    id: UUID
    topic: str
    run_id: UUID
    conversation_id: UUID
    created_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @classmethod
    def from_record(
        cls,
        command_id: UUID,
        topic: str,
        payload: dict[str, object],
        created_at: datetime,
    ) -> OutboxCommand:
        run_id = UUID(str(payload["runId"]))
        return cls(
            command_id,
            topic,
            run_id,
            UUID(str(payload["conversationId"])),
            created_at,
            UUID(str(payload.get("correlationId", run_id))),
            UUID(str(payload.get("causationId", run_id))),
        )

    def wire_payload(self) -> bytes:
        return json.dumps(
            {
                "schemaVersion": 1,
                "commandId": str(self.id),
                "runId": str(self.run_id),
                "conversationId": str(self.conversation_id),
                "createdAt": self.created_at.isoformat(),
                "correlationId": str(self.correlation_id or self.id),
                "causationId": str(self.causation_id or self.run_id),
            },
            separators=(",", ":"),
        ).encode()


def make_run_command(run_id: UUID, conversation_id: UUID) -> OutboxCommand:
    return OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        run_id,
        conversation_id,
        datetime.now(UTC),
        run_id,
        run_id,
    )


class InMemoryOutbox:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[OutboxCommand] = asyncio.Queue()
        self._seen: set[UUID] = set()

    async def publish(self, command: OutboxCommand) -> bool:
        if command.id in self._seen:
            return True
        self._seen.add(command.id)
        await self._queue.put(command)
        return False

    async def publish_run(self, run_id: UUID, conversation_id: UUID) -> None:
        await self.publish(make_run_command(run_id, conversation_id))

    async def receive(self) -> OutboxCommand:
        return await self._queue.get()


class TransactionalOutboxTransport:
    """Command transport used by the API when PostgreSQL owns the outbox.

    A conversation command is inserted into ``outbox`` in the same database
    transaction as the mutation.  The API must not publish a second, detached
    command after returning the HTTP response, so this adapter intentionally
    has no side effect.  The API composition root runs the durable dispatcher
    which claims those rows and publishes them to JetStream.
    """

    async def publish(self, command: OutboxCommand) -> bool:
        del command
        return False

    async def publish_run(self, run_id: UUID, conversation_id: UUID) -> None:
        del run_id, conversation_id
