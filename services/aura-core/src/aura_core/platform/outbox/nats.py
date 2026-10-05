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
from aura_core.platform.outbox.service import OutboxCommand

EventHandler = Callable[[UUID, UUID], Awaitable[None]]


class NatsOutbox:
    def __init__(self, connection_url: str, subject: str = "aura.runs.execute.v1") -> None:
        self.connection_url = connection_url
        self.subject = subject
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
        try:
            await self._jetstream.add_stream(name="AURA_RUNS", subjects=[self.subject])
        except Exception:
            # Existing streams reject add_stream; verify the expected stream
            # below instead of treating every create failure as harmless.
            await self._jetstream.stream_info("AURA_RUNS")
        # Stream creation is idempotent but can be rejected for reasons other
        # than an existing stream.  Account info proves JetStream is actually
        # usable before readiness reports the broker as healthy.
        await self._jetstream.account_info()
        info = await self._jetstream.stream_info("AURA_RUNS")
        subjects = _stream_subjects(info)
        if self.subject not in subjects:
            raise RuntimeError("AURA_RUNS does not contain the configured run subject")
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
        acknowledgment = await self._jetstream.publish(
            self.subject,
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
        subject: str = "aura.runs.execute.v1",
        metrics: MetricsPort | None = None,
    ) -> None:
        self.connection_url = connection_url
        self.subject = subject
        self._connection: Any = None
        self._subscription: Any = None
        self._pending_message: Any = None
        self._pending_command: OutboxCommand | None = None
        self._pending_delivery_count = 1
        self.metrics = metrics or NullMetrics()

    async def connect(self) -> None:
        nats_module: Any = nats
        self._connection = await nats_module.connect(self.connection_url)
        jetstream = self._connection.jetstream()
        try:
            await jetstream.add_stream(name="AURA_RUNS", subjects=[self.subject])
        except Exception:
            await jetstream.stream_info("AURA_RUNS")
        await jetstream.account_info()
        info = await jetstream.stream_info("AURA_RUNS")
        subjects = _stream_subjects(info)
        if self.subject not in subjects:
            raise RuntimeError("AURA_RUNS does not contain the configured run subject")
        self._subscription = await jetstream.pull_subscribe(
            self.subject, durable="aura-core-worker", stream="AURA_RUNS"
        )

    async def receive(self) -> OutboxCommand | None:
        if self._subscription is None:
            raise RuntimeError("NATS consumer is not connected")
        try:
            message = (await self._subscription.fetch(1, timeout=30))[0]
        except NatsTimeoutError:
            return None
        import json

        try:
            payload = cast(dict[str, str], json.loads(message.data))
            command = OutboxCommand(
                UUID(payload["commandId"]),
                self.subject,
                UUID(payload["runId"]),
                UUID(payload["conversationId"]),
                datetime.fromisoformat(payload["createdAt"]),
                UUID(payload["correlationId"]),
                UUID(payload.get("causationId", payload["runId"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.metrics.increment(
                "aura.execution.run_coordinator",
                "errors",
                error_class="delivery",
            )
            raise ValueError("invalid run command metadata") from None
        self._pending_message = message
        self._pending_command = command
        metadata = getattr(message, "metadata", None)
        delivery_count = int(getattr(metadata, "num_delivered", 1) or 1)
        self._pending_delivery_count = delivery_count
        self.metrics.increment(
            "aura.execution.run_coordinator",
            "consumer_received",
            trace_id=(command.correlation_id or command.run_id).hex,
            run_id=str(command.run_id),
            conversation_id=str(command.conversation_id),
            command_id=str(command.id),
            correlation_id=str(command.correlation_id or command.run_id),
            causation_id=str(command.causation_id or command.run_id),
            delivery_count=str(delivery_count),
        )
        if delivery_count > 1:
            self.metrics.increment(
                "aura.execution.run_coordinator",
                "outbox_duplicates",
                trace_id=(command.correlation_id or command.run_id).hex,
                run_id=str(command.run_id),
                conversation_id=str(command.conversation_id),
                command_id=str(command.id),
                correlation_id=str(command.correlation_id or command.run_id),
                causation_id=str(command.causation_id or command.run_id),
                delivery_count=str(delivery_count),
            )
        return command

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
                    "aura.execution.run_coordinator",
                    "errors",
                    error_class="delivery",
                    **metadata,
                )
                raise
            if command is not None:
                self.metrics.increment(
                    "aura.execution.run_coordinator",
                    "consumer_acknowledged",
                    trace_id=(command.correlation_id or command.run_id).hex,
                    run_id=str(command.run_id),
                    conversation_id=str(command.conversation_id),
                    command_id=str(command.id),
                    correlation_id=str(command.correlation_id or command.run_id),
                    causation_id=str(command.causation_id or command.run_id),
                    delivery_count=str(self._pending_delivery_count),
                )
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


def _stream_subjects(info: Any) -> tuple[str, ...]:
    raw: Any = getattr(getattr(info, "config", None), "subjects", None)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(value) for value in cast(list[Any] | tuple[Any, ...], raw))
