"""Transactional outbox abstractions and an in-process transport."""

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

RUN_COMMAND_TOPIC = "aura.runs.execute.v1"
_IDENTIFIER_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_IDENTIFIER_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_IDENTIFIER_SUFFIXES = (
    "id",
    "at",
    "count",
    "version",
    "digest",
    "hash",
    "status",
    "class",
    "type",
    "revision",
)
_CONTENT_KEY_PARTS = (
    "content",
    "text",
    "prompt",
    "response",
    "vector",
    "secret",
    "credential",
    "password",
    "token",
    "payload",
)


def validate_identifier_metadata(
    identifiers: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Validate the transport's bounded, content-free identifier envelope."""

    normalized = tuple(identifiers)
    if len(normalized) > 32:
        raise ValueError("outbox command has too many identifier fields")
    seen: set[str] = set()
    for key, value in normalized:
        if (
            type(key) is not str
            or not _IDENTIFIER_KEY.fullmatch(key)
            or key in seen
            or type(value) is not str
            or not value
            or len(value) > 255
            or _IDENTIFIER_VALUE.fullmatch(value) is None
            or any(ord(character) < 0x20 for character in value)
        ):
            raise ValueError("outbox command contains invalid identifier metadata")
        lowered = key.casefold()
        if any(part in lowered for part in _CONTENT_KEY_PARTS):
            raise ValueError("outbox command contains content-bearing metadata")
        if not lowered.endswith(_IDENTIFIER_SUFFIXES):
            raise ValueError("outbox command contains an unsupported metadata key")
        seen.add(key)
    return normalized


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
    identifiers: tuple[tuple[str, str], ...] = ()
    attempt_id: UUID | None = None
    generation_id: UUID | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_identifier_metadata(self.identifiers)

    @classmethod
    def from_record(
        cls,
        command_id: UUID,
        topic: str,
        payload: dict[str, object],
        created_at: datetime,
    ) -> OutboxCommand:
        schema_version = payload.get("schemaVersion")
        if type(schema_version) is not int:
            raise ValueError("outbox command has an invalid schema version")
        run_id = UUID(str(payload["runId"]))
        identifiers = tuple(
            (str(key), str(value))
            for key, value in payload.items()
            if key not in {
                "schemaVersion",
                "commandId",
                "runId",
                "conversationId",
                "createdAt",
                "correlationId",
                "causationId",
                "attemptId",
                "generationId",
            }
            and type(value) is str
        )
        unknown = [
            key
            for key, value in payload.items()
            if key
            not in {
                "schemaVersion",
                "commandId",
                "runId",
                "conversationId",
                "createdAt",
                "correlationId",
                "causationId",
                "attemptId",
                "generationId",
            }
            and type(value) is not str
        ]
        if unknown:
            raise ValueError("outbox command contains non-string identifier metadata")
        return cls(
            command_id,
            topic,
            run_id,
            UUID(str(payload["conversationId"])),
            created_at,
            UUID(str(payload.get("correlationId", run_id))),
            UUID(str(payload.get("causationId", run_id))),
            identifiers,
            UUID(str(payload["attemptId"])) if "attemptId" in payload else None,
            UUID(str(payload["generationId"])) if "generationId" in payload else None,
            schema_version,
        )

    def wire_payload(self) -> bytes:
        return json.dumps(self.payload(), separators=(",", ":")).encode()

    def payload(self) -> dict[str, object]:
        """Return the generic identifier-only envelope for a command."""

        payload: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "commandId": str(self.id),
            "runId": str(self.run_id),
            "conversationId": str(self.conversation_id),
            "createdAt": self.created_at.isoformat(),
            "correlationId": str(self.correlation_id or self.id),
            "causationId": str(self.causation_id or self.run_id),
        }
        for key, value in self.identifiers:
            payload[key] = value
        if self.attempt_id is not None:
            payload["attemptId"] = str(self.attempt_id)
        if self.generation_id is not None:
            payload["generationId"] = str(self.generation_id)
        return payload

    def identifier(self, name: str) -> str | None:
        return dict(self.identifiers).get(name)


def make_run_command(run_id: UUID, conversation_id: UUID) -> OutboxCommand:
    return OutboxCommand(
        uuid4(),
        RUN_COMMAND_TOPIC,
        run_id,
        conversation_id,
        datetime.now(UTC),
        run_id,
        run_id,
    )


def make_identifier_command(
    topic: str,
    command_id: UUID,
    run_id: UUID,
    conversation_id: UUID,
    *,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    identifiers: Iterable[tuple[str, UUID | str]] = (),
) -> OutboxCommand:
    normalized_identifiers: list[tuple[str, str]] = []
    for key, value in identifiers:
        if type(key) is not str or (type(value) is not str and not isinstance(value, UUID)):
            raise ValueError("outbox command identifiers must be strings or UUIDs")
        normalized_identifiers.append((key, str(value)))
    return OutboxCommand(
        command_id,
        topic,
        run_id,
        conversation_id,
        datetime.now(UTC),
        correlation_id or run_id,
        causation_id or run_id,
        validate_identifier_metadata(tuple(normalized_identifiers)),
    )


def identifier_trace_metadata(command: OutboxCommand) -> dict[str, str]:
    """Map caller-owned identifier keys to approved snake_case trace names."""

    metadata: dict[str, str] = {}
    for key, value in command.identifiers:
        normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
        metadata[normalized] = value
    if command.attempt_id is not None:
        metadata["attempt_id"] = str(command.attempt_id)
    if command.generation_id is not None:
        metadata["generation_id"] = str(command.generation_id)
    return metadata


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
