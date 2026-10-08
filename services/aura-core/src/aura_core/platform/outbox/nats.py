"""NATS JetStream transport adapter. Business state remains Core-owned."""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import nats
from nats.errors import TimeoutError as NatsTimeoutError

from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.ports import MetricsPort, NullMetrics
from aura_core.platform.outbox.service import (
    RUN_COMMAND_TOPIC,
    OutboxCommand,
    identifier_trace_metadata,
)

EventHandler = Callable[[UUID, UUID], Awaitable[None]]


class NatsOutbox:
    def __init__(
        self,
        connection_url: str,
        subject: str = RUN_COMMAND_TOPIC,
        additional_subjects: tuple[str, ...] = (),
    ) -> None:
        self.connection_url = connection_url
        self.subject = subject
        self.additional_subjects = additional_subjects
        self._connection: Any = None
        self._jetstream: Any = None
        self._event_subscription: Any = None
        self._readiness_detail: str | None = "not connected"

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._jetstream is not None

    async def connect(self) -> None:
        nats_module: Any = nats
        self._connection = await nats_module.connect(self.connection_url)
        self._jetstream = self._connection.jetstream()
        await _ensure_command_stream(self._jetstream, (self.subject, *self.additional_subjects))
        # Stream creation is idempotent but can be rejected for reasons other
        # than an existing stream.  Account info proves JetStream is actually
        # usable before readiness reports the broker as healthy.
        await self._jetstream.account_info()
        info = await self._jetstream.stream_info("AURA_RUNS")
        subjects = _stream_subjects(info)
        if self.subject not in subjects:
            raise RuntimeError("AURA_RUNS does not contain the configured run subject")
        missing = [subject for subject in self.additional_subjects if subject not in subjects]
        if missing:
            raise RuntimeError("AURA_RUNS does not contain all configured command subjects")
        self._readiness_detail = None

    async def readiness(self) -> tuple[bool, str | None]:
        if self._connection is None or self._jetstream is None:
            return False, self._readiness_detail or "not connected"
        if bool(getattr(self._connection, "is_closed", False)):
            self._readiness_detail = "connection closed"
            return False, self._readiness_detail
        try:
            await self._jetstream.account_info()
            info = await self._jetstream.stream_info("AURA_RUNS")
            subjects = _stream_subjects(info)
            if self.subject not in subjects:
                self._readiness_detail = "run stream subject missing"
                return False, self._readiness_detail
            if any(subject not in subjects for subject in self.additional_subjects):
                self._readiness_detail = "configured command subject missing"
                return False, self._readiness_detail
        except Exception:
            self._readiness_detail = "JetStream account or run stream unavailable"
            return False, self._readiness_detail
        self._readiness_detail = None
        return True, None

    async def is_ready(self) -> bool:
        ready, _ = await self.readiness()
        return ready

    async def publish(self, command: OutboxCommand) -> bool:
        if self._jetstream is None:
            raise RuntimeError("NATS transport is not connected")
        subject = self.subject if command.topic == RUN_COMMAND_TOPIC else command.topic
        acknowledgment = await self._jetstream.publish(
            subject,
            command.wire_payload(),
            headers={"Nats-Msg-Id": str(command.id)},
        )
        return bool(getattr(acknowledgment, "duplicate", False))

    async def publish_event(self, event: RunEvent) -> None:
        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        # PostgreSQL already owns the complete event.  NATS carries only an
        # ephemeral identifier wakeup so private conversation content never
        # becomes transport-retained state.
        await self._connection.publish(
            f"aura.runs.events.{event.run_id}",
            json.dumps(
                {"eventId": str(event.event_id), "runId": str(event.run_id)},
                separators=(",", ":"),
            ).encode(),
        )

    async def subscribe_events(self, handler: EventHandler) -> None:
        """Subscribe to worker events for API-side SSE fan-out.

        The database event history remains authoritative.  This subscription
        only wakes local SSE clients; reconnecting clients hydrate from
        PostgreSQL before consuming the ephemeral NATS stream.
        """

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")

        async def callback(message: Any) -> None:
            try:
                payload = cast(dict[str, Any], json.loads(message.data))
                if set(payload) != {"eventId", "runId"}:
                    return
                await handler(UUID(str(payload["eventId"])), UUID(str(payload["runId"])))
            except KeyError, TypeError, ValueError, json.JSONDecodeError:
                # Malformed provider/transport data is ignored.  The next SSE
                # request will recover from the persisted event history.
                return

        self._event_subscription = await self._connection.subscribe(
            "aura.runs.events.>", cb=callback
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.drain()
            self._connection = None
            self._jetstream = None
            self._event_subscription = None
            self._readiness_detail = "not connected"


class NatsRunConsumer:
    """Durable JetStream pull consumer for identifier-only run commands."""

    def __init__(
        self,
        connection_url: str,
        subject: str = RUN_COMMAND_TOPIC,
        metrics: MetricsPort | None = None,
        *,
        durable: str = "aura-core-worker",
        component: str = "aura.execution.run_coordinator",
        strict_schema: bool = False,
    ) -> None:
        self.connection_url = connection_url
        self.subject = subject
        self._connection: Any = None
        self._subscription: Any = None
        self._pending_message: Any = None
        self._pending_command: OutboxCommand | None = None
        self._pending_delivery_count = 1
        self.metrics = metrics or NullMetrics()
        self.durable = durable
        self.component = component
        self.strict_schema = strict_schema

    async def connect(self) -> None:
        nats_module: Any = nats
        self._connection = await nats_module.connect(self.connection_url)
        jetstream = self._connection.jetstream()
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC, self.subject))
        await jetstream.account_info()
        info = await jetstream.stream_info("AURA_RUNS")
        subjects = _stream_subjects(info)
        if self.subject not in subjects:
            raise RuntimeError("AURA_RUNS does not contain the configured run subject")
        self._subscription = await jetstream.pull_subscribe(
            self.subject, durable=self.durable, stream="AURA_RUNS"
        )

    async def receive(self) -> OutboxCommand | None:
        if self._subscription is None:
            raise RuntimeError("NATS consumer is not connected")
        try:
            message = (await self._subscription.fetch(1, timeout=30))[0]
        except NatsTimeoutError:
            return None
        self._pending_message = message
        metadata = getattr(message, "metadata", None)
        delivery_count = int(getattr(metadata, "num_delivered", 1) or 1)
        self._pending_delivery_count = delivery_count

        try:
            raw_payload = json.loads(message.data)
            if not isinstance(raw_payload, dict):
                raise ValueError("command payload must be an object")
            payload = cast(dict[str, object], raw_payload)
            schema_version = payload.get("schemaVersion")
            if schema_version is None and not self.strict_schema:
                # Existing run producers predate the generic envelope version
                # field.  They remain wire-compatible at v1; memory commands
                # use the strict identifier consumer and must carry it.
                schema_version = 1
            if type(schema_version) is not int:
                raise ValueError("invalid command schema version")
            reserved = {
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
            if any(type(value) is not str for key, value in payload.items() if key not in reserved):
                raise ValueError("non-string identifier metadata")
            command = OutboxCommand(
                UUID(str(payload["commandId"])),
                self.subject,
                UUID(str(payload["runId"])),
                UUID(str(payload["conversationId"])),
                datetime.fromisoformat(str(payload["createdAt"])),
                UUID(str(payload["correlationId"])),
                UUID(str(payload.get("causationId", payload["runId"]))),
                tuple(
                    (str(key), str(value))
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
                    and type(value) is str
                ),
                UUID(str(payload["attemptId"])) if "attemptId" in payload else None,
                UUID(str(payload["generationId"])) if "generationId" in payload else None,
                schema_version,
            )
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            self.metrics.increment(
                self.component,
                "errors",
                error_class="delivery",
            )
            raise ValueError("invalid run command metadata") from None
        self._pending_command = command
        self.metrics.increment(
            self.component,
            "consumer_received",
            trace_id=(command.correlation_id or command.run_id).hex,
            run_id=str(command.run_id),
            conversation_id=str(command.conversation_id),
            command_id=str(command.id),
            correlation_id=str(command.correlation_id or command.run_id),
            causation_id=str(command.causation_id or command.run_id),
            delivery_count=str(delivery_count),
            **_optional_command_metadata(command),
        )
        if delivery_count > 1:
            self.metrics.increment(
                self.component,
                "consumer_redelivered",
                trace_id=(command.correlation_id or command.run_id).hex,
                run_id=str(command.run_id),
                conversation_id=str(command.conversation_id),
                command_id=str(command.id),
                correlation_id=str(command.correlation_id or command.run_id),
                causation_id=str(command.causation_id or command.run_id),
                delivery_count=str(delivery_count),
                **_optional_command_metadata(command),
            )
            self.metrics.increment(
                self.component,
                "outbox_duplicates",
                trace_id=(command.correlation_id or command.run_id).hex,
                run_id=str(command.run_id),
                conversation_id=str(command.conversation_id),
                command_id=str(command.id),
                correlation_id=str(command.correlation_id or command.run_id),
                causation_id=str(command.causation_id or command.run_id),
                delivery_count=str(delivery_count),
                **_optional_command_metadata(command),
            )
        return command

    @property
    def delivery_count(self) -> int:
        return self._pending_delivery_count

    async def ack(self) -> None:
        """Acknowledge only after the run mutation has reached a terminal state."""

        message = getattr(self, "_pending_message", None)
        if message is not None:
            command = self._pending_command
            try:
                await message.ack()
            except Exception:
                metadata = (
                    {
                        "trace_id": (command.correlation_id or command.run_id).hex,
                        "run_id": str(command.run_id),
                        "conversation_id": str(command.conversation_id),
                    }
                    if command is not None
                    else {}
                )
                self.metrics.increment(
                    self.component,
                    "errors",
                    error_class="delivery",
                    **metadata,
                )
                raise
            if command is not None:
                self.metrics.increment(
                    self.component,
                    "consumer_acknowledged",
                    trace_id=(command.correlation_id or command.run_id).hex,
                    run_id=str(command.run_id),
                    conversation_id=str(command.conversation_id),
                    command_id=str(command.id),
                    correlation_id=str(command.correlation_id or command.run_id),
                    causation_id=str(command.causation_id or command.run_id),
                    delivery_count=str(self._pending_delivery_count),
                    **_optional_command_metadata(command),
                )
            self._pending_message = None
            self._pending_command = None
            self._pending_delivery_count = 1

    async def nack(
        self,
        *,
        retry_delay: float | None = None,
        error_class: str = "delivery",
    ) -> None:
        """Release a delivery for bounded JetStream redelivery."""

        message = self._pending_message
        command = self._pending_command
        if message is None:
            return
        try:
            nack = getattr(message, "nak", None)
            if not callable(nack):
                raise RuntimeError("JetStream message cannot be negatively acknowledged")
            if retry_delay is None:
                await cast(Callable[[], Awaitable[object]], nack)()
            else:
                try:
                    await cast(Callable[..., Awaitable[object]], nack)(delay=retry_delay)
                except TypeError:
                    # Keep compatibility with test doubles and older nats-py
                    # releases that do not expose delayed NAKs.
                    await cast(Callable[[], Awaitable[object]], nack)()
            if command is not None:
                self.metrics.increment(
                    self.component,
                    "consumer_nacked",
                    trace_id=(command.correlation_id or command.run_id).hex,
                    run_id=str(command.run_id),
                    conversation_id=str(command.conversation_id),
                    command_id=str(command.id),
                    correlation_id=str(command.correlation_id or command.run_id),
                    causation_id=str(command.causation_id or command.run_id),
                    delivery_count=str(self._pending_delivery_count),
                    error_class=error_class,
                    **_optional_command_metadata(command),
                )
        finally:
            self._clear_pending()

    async def reject(self, *, error_class: str = "validation") -> None:
        """Permanently terminate a malformed or incompatible delivery."""

        message = self._pending_message
        command = self._pending_command
        if message is None:
            return
        try:
            term = getattr(message, "term", None)
            if not callable(term):
                raise RuntimeError("JetStream message cannot be terminated")
            await cast(Callable[[], Awaitable[object]], term)()
            if command is not None:
                self.metrics.increment(
                    self.component,
                    "consumer_rejected",
                    trace_id=(command.correlation_id or command.run_id).hex,
                    run_id=str(command.run_id),
                    conversation_id=str(command.conversation_id),
                    command_id=str(command.id),
                    correlation_id=str(command.correlation_id or command.run_id),
                    causation_id=str(command.causation_id or command.run_id),
                    delivery_count=str(self._pending_delivery_count),
                    error_class=error_class,
                    **_optional_command_metadata(command),
                )
        finally:
            self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_message = None
        self._pending_command = None
        self._pending_delivery_count = 1

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.drain()
            self._connection = None
            self._subscription = None
            self._pending_message = None
            self._pending_command = None
            self._pending_delivery_count = 1


class NatsIdentifierConsumer(NatsRunConsumer):
    """Durable pull consumer for a caller-owned identifier-only command."""

    def __init__(
        self,
        connection_url: str,
        subject: str,
        metrics: MetricsPort | None = None,
        *,
        durable: str = "aura-core-identifier-worker",
        component: str = "aura.execution.run_coordinator",
    ) -> None:
        super().__init__(
            connection_url,
            subject,
            metrics,
            durable=durable,
            component=component,
            strict_schema=True,
        )


def _stream_subjects(info: Any) -> tuple[str, ...]:
    raw: Any = getattr(getattr(info, "config", None), "subjects", None)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(value) for value in cast(list[Any] | tuple[Any, ...], raw))


async def _ensure_command_stream(jetstream: Any, subjects: tuple[str, ...]) -> None:
    try:
        await jetstream.add_stream(name="AURA_RUNS", subjects=subjects)
    except Exception:
        info = await jetstream.stream_info("AURA_RUNS")
        existing = _stream_subjects(info)
        missing = [subject for subject in subjects if subject not in existing]
        if missing:
            # Upgrade an older AURA_RUNS stream in place.  The stream retains
            # all existing messages while adding the versioned memory subject.
            await jetstream.update_stream(
                name="AURA_RUNS",
                subjects=list(dict.fromkeys(existing + tuple(missing))),
            )


def _optional_command_metadata(command: OutboxCommand) -> dict[str, str]:
    return identifier_trace_metadata(command)
