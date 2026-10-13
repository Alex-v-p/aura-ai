"""Transactional outbox abstractions and an in-process transport."""

import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

RUN_COMMAND_TOPIC = "aura.runs.execute.v1"
APPROVAL_EVENT_TOPIC = "aura.approvals.events.v1"
RUN_RESUME_TOPIC = "aura.runs.resume.v1"
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
    "sequence",
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
    "dueat",
    "requestedtime",
    "localdate",
    "localtime",
    "timezone",
)
_AGGREGATE_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
IdentifierValue = str | int


def _valid_identifier_value(
    value: IdentifierValue,
    *,
    allow_integer: bool,
    key: str,
) -> bool:
    if type(value) is str:
        return (
            bool(value)
            and len(value) <= 255
            and _IDENTIFIER_VALUE.fullmatch(value) is not None
            and all(ord(character) >= 0x20 for character in value)
        )
    if not allow_integer or type(value) is not int or value < 0 or value > 2_147_483_647:
        return False
    return not key.casefold().endswith("version") or value >= 1


def validate_identifier_metadata(
    identifiers: Iterable[tuple[str, IdentifierValue]],
    *,
    allow_integer: bool = False,
) -> tuple[tuple[str, IdentifierValue], ...]:
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
            or not _valid_identifier_value(value, allow_integer=allow_integer, key=key)
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
    run_id: UUID | None
    conversation_id: UUID | None
    created_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    identifiers: tuple[tuple[str, IdentifierValue], ...] = ()
    attempt_id: UUID | None = None
    generation_id: UUID | None = None
    schema_version: int = 1
    # Generic aggregate commands intentionally have no run or conversation
    # identity. Legacy run and memory commands populate both fields; generic
    # construction leaves both absent and serializes only aggregateType/
    # aggregateId.
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None
    event_payload: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        validate_identifier_metadata(
            self.identifiers,
            allow_integer=self.aggregate_type is not None,
        )
        if (self.aggregate_type is None) != (self.aggregate_id is None):
            raise ValueError("outbox command must provide both aggregate type and id")
        if self.aggregate_type is not None:
            if (
                type(self.aggregate_type) is not str
                or type(self.aggregate_id) is not UUID
                or _AGGREGATE_TYPE.fullmatch(self.aggregate_type) is None
            ):
                raise ValueError("outbox command has an invalid aggregate type")
            if self.run_id is not None or self.conversation_id is not None:
                raise ValueError(
                    "generic outbox command cannot contain run or conversation identifiers"
                )
        elif self.run_id is None or self.conversation_id is None:
            raise ValueError(
                "legacy outbox command requires run and conversation identifiers"
            )

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
        if topic == "aura.knowledge.documents.events.v1":
            required = {
                "schemaVersion",
                "eventId",
                "eventType",
                "documentId",
                "status",
                "documentVersion",
                "occurredAt",
                "correlationId",
                "causationId",
            }
            if set(payload) != required or payload["schemaVersion"] != 1:
                raise ValueError("document lifecycle event payload is not schema exact")
            aggregate_id = UUID(str(payload["documentId"]))
            return cls(
                command_id,
                topic,
                None,
                None,
                created_at,
                UUID(str(payload["correlationId"])),
                UUID(str(payload["causationId"])),
                schema_version=1,
                aggregate_type="knowledge.document",
                aggregate_id=aggregate_id,
                event_payload=dict(payload),
            )
        is_generic = "aggregateType" in payload or "aggregateId" in payload
        if is_generic and {"aggregateType", "aggregateId"} != (
            {"aggregateType", "aggregateId"} & payload.keys()
        ):
            raise ValueError("generic outbox command requires aggregate type and id")
        if is_generic and (
            type(payload.get("aggregateType")) is not str
            or type(payload.get("aggregateId")) is not str
        ):
            raise ValueError("generic outbox command has invalid aggregate identity")
        # Generic envelopes may carry run/conversation references as ordinary
        # identifier metadata (for approval correlation), but they must never
        # populate the legacy command identity fields.
        run_id = UUID(str(payload["runId"])) if "runId" in payload and not is_generic else None
        fallback_id: UUID = (
            run_id or UUID(str(payload["aggregateId"])) if is_generic else command_id
        )
        conversation_id = (
            UUID(str(payload["conversationId"]))
            if "conversationId" in payload and not is_generic
            else None
        )
        identifiers = tuple(
            (str(key), value)
            for key, value in payload.items()
            if key not in {
                "schemaVersion",
                "commandId",
                *(() if is_generic else ("runId", "conversationId")),
                "aggregateType",
                "aggregateId",
                "createdAt",
                "correlationId",
                "causationId",
                "attemptId",
                "generationId",
            }
            and (type(value) is str or (is_generic and type(value) is int))
        )
        unknown = [
            key
            for key, value in payload.items()
            if key
            not in {
                "schemaVersion",
                "commandId",
                *(() if is_generic else ("runId", "conversationId")),
                "aggregateType",
                "aggregateId",
                "createdAt",
                "correlationId",
                "causationId",
                "attemptId",
                "generationId",
            }
            and not (type(value) is str or (is_generic and type(value) is int))
        ]
        if unknown:
            raise ValueError("outbox command contains invalid identifier metadata")
        return cls(
            command_id,
            topic,
            run_id,
            conversation_id,
            created_at,
            UUID(str(payload.get("correlationId", fallback_id))),
            UUID(str(payload.get("causationId", fallback_id))),
            identifiers,
            UUID(str(payload["attemptId"])) if "attemptId" in payload else None,
            UUID(str(payload["generationId"])) if "generationId" in payload else None,
            schema_version,
            str(payload["aggregateType"]) if is_generic else None,
            UUID(str(payload["aggregateId"])) if is_generic else None,
        )

    def wire_payload(self) -> bytes:
        return json.dumps(self.payload(), separators=(",", ":")).encode()

    def payload(self) -> dict[str, object]:
        """Return the generic identifier-only envelope for a command."""

        if self.event_payload is not None:
            return dict(self.event_payload)

        payload: dict[str, object]
        if self.aggregate_type is not None:
            payload = {
                "schemaVersion": self.schema_version,
                "commandId": str(self.id),
                "aggregateType": self.aggregate_type,
                "aggregateId": str(self.aggregate_id),
                "createdAt": self.created_at.isoformat(),
                "correlationId": str(self.correlation_id or self.id),
                "causationId": str(self.causation_id or self.aggregate_id),
            }
        else:
            payload = {
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
        value = dict(self.identifiers).get(name)
        return str(value) if value is not None else None


def make_event_command(
    topic: str,
    event_id: UUID,
    aggregate_id: UUID,
    payload: Mapping[str, object],
) -> OutboxCommand:
    """Build a schema-exact metadata-only lifecycle event command."""

    return OutboxCommand(
        event_id,
        topic,
        None,
        None,
        datetime.now(UTC),
        UUID(str(payload["correlationId"])),
        UUID(str(payload["causationId"])),
        schema_version=1,
        aggregate_type="knowledge.document",
        aggregate_id=aggregate_id,
        event_payload=dict(payload),
    )

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


def make_aggregate_command(
    topic: str,
    command_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    *,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    identifiers: Iterable[tuple[str, UUID | str | int]] = (),
) -> OutboxCommand:
    """Build an identifier-only command for a non-run aggregate.

    Generic aggregate events deliberately carry no fabricated run or
    conversation identifiers.  The aggregate identity is validated as part
    of the envelope, while any event-specific metadata remains subject to the
    same content-free identifier checks as legacy commands.
    """

    normalized_identifiers: list[tuple[str, IdentifierValue]] = []
    for key, value in identifiers:
        if type(key) is not str or (
            type(value) not in {str, int} and not isinstance(value, UUID)
        ):
            raise ValueError("outbox command identifiers must be strings, integers, or UUIDs")
        normalized_identifiers.append((key, str(value) if isinstance(value, UUID) else value))
    return OutboxCommand(
        command_id,
        topic,
        None,
        None,
        datetime.now(UTC),
        correlation_id or command_id,
        causation_id or aggregate_id,
        validate_identifier_metadata(
            tuple(normalized_identifiers),
            allow_integer=True,
        ),
        schema_version=1,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )


def make_approval_event_command(
    approval_id: UUID,
    *,
    event_id: UUID,
    status: str,
    version: int,
    run_id: UUID,
    conversation_id: UUID,
    event_type: str = "approval.updated",
    command_id: UUID | None = None,
) -> OutboxCommand:
    """Build an identifier-only durable approval lifecycle signal.

    Approval proposal content and requested times remain in Core-owned
    PostgreSQL.  The outbox envelope carries only bounded identifiers and
    state metadata; run and conversation identities are identifier fields,
    not fabricated legacy run-envelope fields.
    """

    return make_aggregate_command(
        APPROVAL_EVENT_TOPIC,
        command_id or uuid4(),
        "governance.approval",
        approval_id,
        identifiers=(
            ("approvalId", approval_id),
            ("eventId", event_id),
            ("eventType", event_type),
            ("status", status),
            ("version", version),
            ("runId", run_id),
            ("conversationId", conversation_id),
        ),
    )


def make_approval_resume_command(
    run_id: UUID,
    conversation_id: UUID,
    *,
    approval_id: UUID,
    event_id: UUID,
    status: str,
    version: int,
    command_id: UUID | None = None,
) -> OutboxCommand:
    """Build an identifier-only durable wakeup for a waiting run.

    The worker re-reads the authoritative approval and run rows after this
    signal.  A duplicate or lost wakeup is therefore safe: PostgreSQL state
    and the approval-derived idempotency key decide whether resumption runs.
    """

    return make_aggregate_command(
        RUN_RESUME_TOPIC,
        command_id or uuid4(),
        "execution.run",
        run_id,
        identifiers=(
            ("runId", run_id),
            ("conversationId", conversation_id),
            ("approvalId", approval_id),
            ("eventId", event_id),
            ("eventType", "approval.resumed"),
            ("status", status),
            ("version", version),
        ),
    )


def identifier_trace_metadata(command: OutboxCommand) -> dict[str, str]:
    """Map caller-owned identifier keys to approved snake_case trace names."""

    metadata: dict[str, str] = {}
    if command.aggregate_type is not None:
        metadata["aggregate_type"] = command.aggregate_type
        metadata["aggregate_id"] = str(command.aggregate_id)
    for key, value in command.identifiers:
        normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
        metadata[normalized] = str(value)
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
