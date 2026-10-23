"""NATS JetStream transport adapter. Business state remains Core-owned."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

import nats
from nats.errors import TimeoutError as NatsTimeoutError

from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.ports import MetricsPort, NullMetrics
from aura_core.platform.evaluation_transport import (
    EVALUATION_CANCELLATION_SUBJECT,
    EVALUATION_REQUEST_SUBJECT,
    EVALUATION_RESULT_SUBJECT,
    EVALUATION_STREAM,
    TransportValidationError,
    validate_message,
)
from aura_core.platform.outbox.service import (
    APPROVAL_EVENT_TOPIC,
    RUN_COMMAND_TOPIC,
    RUN_RESUME_TOPIC,
    OutboxCommand,
    identifier_trace_metadata,
)
from aura_core.platform.telemetry import Stopwatch, new_span_id

EventHandler = Callable[[UUID, UUID], Awaitable[None]]
NotificationEventHandler = Callable[[UUID], Awaitable[None]]
ApprovalEventHandler = Callable[[UUID], Awaitable[None]]
ResumptionEventHandler = Callable[[UUID], Awaitable[None]]
ThinkingEventHandler = Callable[[UUID, UUID, int, str], Awaitable[None]]
NOTIFICATION_EVENT_SUBJECT = "aura.notifications.events.v1"
APPROVAL_EVENT_SUBJECT = "aura.approvals.events.v1"
RESUMPTION_EVENT_SUBJECT = "aura.runs.resume.v1"
# Deliberately plain Core NATS: this subject is live-only and must never be
# included in a JetStream stream or transactional outbox command.
THINKING_EVENT_SUBJECT = "aura.runs.thinking.v1"
AURA_RUNS_STREAM = "AURA_RUNS"
_STREAM_LOOKUP_ATTEMPTS = 3
_STREAM_LOOKUP_DELAY_SECONDS = 0.1


class EvaluationTransportMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...


def _evaluation_metric(
    metrics: EvaluationTransportMetrics | None,
    name: str,
    *,
    outcome: str,
    error_class: str | None = None,
) -> None:
    """Record bounded transport health without coupling delivery to telemetry."""

    if metrics is None:
        return
    attributes = {"dependency": "nats_jetstream", "outcome": outcome}
    if error_class is not None:
        attributes["error_class"] = error_class
    try:
        metrics.increment("aura.evaluation.runner", name, **attributes)
    except Exception:
        return


class EvaluationJetStreamPublisher:
    """Durable result publisher for the isolated evaluation identity."""

    def __init__(
        self,
        connection: Any,
        *,
        subject: str = EVALUATION_RESULT_SUBJECT,
        metrics: EvaluationTransportMetrics | None = None,
    ) -> None:
        self._connection = connection
        self._subject = subject
        self._metrics = metrics
        self._publish_degraded = False

    async def publish(self, payload: Mapping[str, object]) -> None:
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > 1_048_576:
                raise ValueError("evaluation result exceeds transport limit")
            validate_message(payload)
            jetstream = self._connection.jetstream()
            await jetstream.publish(
                self._subject,
                encoded,
                headers={"Nats-Msg-Id": str(payload["messageId"])},
            )
        except Exception:
            _evaluation_metric(
                self._metrics,
                "evaluation_runner_nats_publish_failure_count",
                outcome="error",
                error_class="delivery",
            )
            self._publish_degraded = True
            raise
        if self._publish_degraded:
            _evaluation_metric(
                self._metrics,
                "evaluation_runner_nats_recovery_count",
                outcome="ok",
            )
            self._publish_degraded = False


class EvaluationJetStreamConsumer:
    """Authenticated pull consumers with ack-after-durable-receipt semantics."""

    def __init__(
        self,
        connection: Any,
        *,
        request_durable: str = "aura-core-evaluation-runner",
        cancellation_durable: str = "aura-core-evaluation-cancellation",
        metrics: EvaluationTransportMetrics | None = None,
    ) -> None:
        self._connection = connection
        self._request_durable = request_durable
        self._cancellation_durable = cancellation_durable
        self._metrics = metrics

    async def consume(self, runner: Any, stop: asyncio.Event) -> None:
        jetstream = self._connection.jetstream()

        async def subscribe(subject: str, durable: str) -> Any:
            for attempt in range(3):
                try:
                    subscription = await jetstream.pull_subscribe(
                        subject,
                        durable=durable,
                        stream=EVALUATION_STREAM,
                    )
                except Exception:
                    _evaluation_metric(
                        self._metrics,
                        "evaluation_runner_nats_drain_failure_count",
                        outcome="error",
                        error_class="delivery",
                    )
                    if attempt == 2 or stop.is_set():
                        raise
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                if attempt:
                    _evaluation_metric(
                        self._metrics,
                        "evaluation_runner_nats_recovery_count",
                        outcome="ok",
                    )
                return subscription
            raise RuntimeError("evaluation subscription unavailable")

        request_sub = await subscribe(EVALUATION_REQUEST_SUBJECT, self._request_durable)
        cancellation_sub = await subscribe(
            EVALUATION_CANCELLATION_SUBJECT, self._cancellation_durable
        )

        async def drain(subscription: Any) -> None:
            degraded = False
            while not stop.is_set():
                try:
                    messages = await subscription.fetch(1, timeout=0.5)
                except NatsTimeoutError:
                    continue
                except Exception:
                    _evaluation_metric(
                        self._metrics,
                        "evaluation_runner_nats_fetch_failure_count",
                        outcome="error",
                        error_class="delivery",
                    )
                    degraded = True
                    continue
                if degraded:
                    _evaluation_metric(
                        self._metrics,
                        "evaluation_runner_nats_recovery_count",
                        outcome="ok",
                    )
                    degraded = False
                for message in messages:
                    try:
                        payload = json.loads(message.data)
                        if not isinstance(payload, dict):
                            raise TransportValidationError("evaluation message is not an object")
                        validate_message(cast(dict[str, object], payload))
                        await runner.accept(payload)
                        await message.ack()
                    except (TransportValidationError, UnicodeDecodeError, json.JSONDecodeError):
                        # Poison messages are terminally rejected and cannot
                        # be retried into an unbounded redelivery loop.
                        await message.ack()
                        _evaluation_metric(
                            self._metrics,
                            "evaluation_runner_nats_poison_count",
                            outcome="rejected",
                            error_class="validation",
                        )
                    except Exception as exc:
                        if type(exc).__name__ == "AttemptConflict":
                            # Identity conflicts are permanent poison: retrying
                            # a request under a different tuple cannot make it
                            # valid and would redeliver forever.
                            _evaluation_metric(
                                self._metrics,
                                "evaluation_runner_nats_poison_count",
                                outcome="rejected",
                                error_class="conflict",
                            )
                            await message.ack()
                            _evaluation_metric(
                                self._metrics,
                                "evaluation_runner_nats_identity_conflict_count",
                                outcome="rejected",
                                error_class="identity",
                            )
                            continue
                        # Leave transient provider/application failures
                        # unacknowledged for bounded JetStream redelivery.
                        _evaluation_metric(
                            self._metrics,
                            "evaluation_runner_nats_drain_failure_count",
                            outcome="error",
                            error_class="delivery",
                        )
                        degraded = True
                        continue

        await asyncio.gather(drain(request_sub), drain(cancellation_sub))


class NatsOutbox:
    def __init__(
        self,
        connection_url: str,
        subject: str = RUN_COMMAND_TOPIC,
        additional_subjects: tuple[str, ...] = (),
        metrics: MetricsPort | None = None,
    ) -> None:
        self.connection_url = connection_url
        self.subject = subject
        self.additional_subjects = additional_subjects
        self.metrics = metrics or NullMetrics()
        self._connection: Any = None
        self._jetstream: Any = None
        self._event_subscription: Any = None
        self._notification_subscription: Any = None
        self._approval_subscription: Any = None
        self._resumption_subscription: Any = None
        self._thinking_subscription: Any = None
        self._readiness_detail: str | None = "not connected"

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._jetstream is not None

    async def connect(self) -> None:
        nats_module: Any = nats
        self._connection = await nats_module.connect(self.connection_url)
        self._jetstream = self._connection.jetstream()
        await _assert_thinking_subject_unretained(self._jetstream)
        await _ensure_command_stream(self._jetstream, (self.subject, *self.additional_subjects))
        # Stream creation is idempotent but can be rejected for reasons other
        # than an existing stream.  Account info proves JetStream is actually
        # usable before readiness reports the broker as healthy.
        await self._jetstream.account_info()
        info = await self._jetstream.stream_info(AURA_RUNS_STREAM)
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
            await _assert_thinking_subject_unretained(self._jetstream)
            await self._jetstream.account_info()
            info = await self._jetstream.stream_info(AURA_RUNS_STREAM)
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
        duplicate = bool(getattr(acknowledgment, "duplicate", False))
        # A committed reminder firing has a durable PostgreSQL replay event;
        # this second publish is only a process-local SSE wakeup.  Keep it
        # outside JetStream and carry no reminder content or owner identity.
        if (
            command.aggregate_type == "automation.reminder"
            and command.identifier("eventType") == "reminder.fired"
        ):
            # Core stages the persisted replay-event identifier in the
            # identifier-only command.  Keep the command id as a compatibility
            # fallback for older producers; it is still wake-only and never a
            # replay cursor or source of notification truth.
            wake_id = command.identifier("eventId") or command.identifier("replayEventId")
            try:
                persisted_event_id = UUID(wake_id) if wake_id is not None else command.id
            except ValueError:
                persisted_event_id = command.id
            await self.publish_notification_event(persisted_event_id)
        if (
            command.aggregate_type == "governance.approval"
            and command.topic == APPROVAL_EVENT_TOPIC
        ):
            wake_id = command.identifier("eventId") or command.id
            try:
                persisted_event_id = UUID(str(wake_id))
            except ValueError:
                persisted_event_id = command.id
            await self.publish_approval_event(persisted_event_id)
        if command.topic == RUN_RESUME_TOPIC:
            wake_id = command.identifier("eventId") or command.id
            try:
                persisted_event_id = UUID(str(wake_id))
            except ValueError:
                persisted_event_id = command.id
            await self.publish_resumption_event(persisted_event_id)
        return duplicate

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

    async def publish_notification_event(self, event_id: UUID | Any) -> None:
        """Publish an ephemeral, identifier-only notification wakeup."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        identifier = event_id if isinstance(event_id, UUID) else getattr(event_id, "id", None)
        if not isinstance(identifier, UUID):
            raise ValueError("notification wakeup requires an event identifier")
        await self._connection.publish(
            NOTIFICATION_EVENT_SUBJECT,
            json.dumps({"eventId": str(identifier)}, separators=(",", ":")).encode(),
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

    async def subscribe_notification_events(
        self, handler: NotificationEventHandler
    ) -> Any:
        """Subscribe to notification wakeups after database replay."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")

        async def callback(message: Any) -> None:
            try:
                payload = cast(dict[str, Any], json.loads(message.data))
                if set(payload) != {"eventId"}:
                    return
                await handler(UUID(str(payload["eventId"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return

        self._notification_subscription = await self._connection.subscribe(
            NOTIFICATION_EVENT_SUBJECT, cb=callback
        )
        return self._notification_subscription

    async def publish_approval_event(self, event_id: UUID | Any) -> None:
        """Publish an ephemeral identifier-only approval wakeup."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        identifier = event_id if isinstance(event_id, UUID) else getattr(event_id, "id", None)
        if not isinstance(identifier, UUID):
            raise ValueError("approval wakeup requires an event identifier")
        await self._connection.publish(
            APPROVAL_EVENT_SUBJECT,
            json.dumps({"eventId": str(identifier)}, separators=(",", ":")).encode(),
        )

    async def publish_resumption_event(self, event_id: UUID | Any) -> None:
        """Publish an ephemeral identifier-only waiting-run wakeup."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        identifier = event_id if isinstance(event_id, UUID) else getattr(event_id, "id", None)
        if not isinstance(identifier, UUID):
            raise ValueError("resumption wakeup requires an event identifier")
        await self._connection.publish(
            RESUMPTION_EVENT_SUBJECT,
            json.dumps({"eventId": str(identifier)}, separators=(",", ":")).encode(),
        )

    async def publish_thinking_delta(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        offset: int,
        text: str,
        parent_span_id: str | None = None,
    ) -> None:
        """Publish a bounded live-only thinking envelope over plain NATS."""

        timer = Stopwatch()
        if (
            not isinstance(cast(object, run_id), UUID)
            or not isinstance(cast(object, conversation_id), UUID)
            or type(offset) is not int
            or offset < 0
            or not isinstance(cast(object, text), str)
            or not text
            or len(text) > 4096
        ):
            self.metrics.increment(
                "aura.runtime.stream_delivery",
                "thinking_stream_outcome",
                outcome="invalid",
            )
            raise ValueError("invalid thinking delta")
        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        try:
            await _assert_thinking_subject_unretained(self._jetstream)
            await self._connection.publish(
                THINKING_EVENT_SUBJECT,
                json.dumps(
                    {
                        "runId": str(run_id),
                        "conversationId": str(conversation_id),
                        "offset": offset,
                        "text": text,
                    },
                    separators=(",", ":"),
                ).encode(),
            )
        except Exception:
            self.metrics.record_span(
                "aura.runtime.stream_delivery",
                "thinking.publish",
                timer.elapsed_ms(),
                trace_id=run_id.hex,
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                dependency="nats_transient",
                outcome="error",
                error_class="delivery",
                run_id=str(run_id),
                conversation_id=str(conversation_id),
            )
            self.metrics.increment(
                "aura.runtime.stream_delivery",
                "thinking_stream_outcome",
                outcome="transport_error",
                dependency="nats_transient",
                run_id=str(run_id),
                conversation_id=str(conversation_id),
            )
            raise
        self.metrics.record_span(
            "aura.runtime.stream_delivery",
            "thinking.publish",
            timer.elapsed_ms(),
            trace_id=run_id.hex,
            span_id=new_span_id(),
            parent_span_id=parent_span_id,
            dependency="nats_transient",
            outcome="ok",
            run_id=str(run_id),
            conversation_id=str(conversation_id),
        )
        self.metrics.increment(
            "aura.runtime.stream_delivery",
            "thinking_stream_outcome",
            outcome="transport_published",
            dependency="nats_transient",
            run_id=str(run_id),
            conversation_id=str(conversation_id),
        )

    async def subscribe_thinking_events(self, handler: ThinkingEventHandler) -> Any:
        """Receive only strict, bounded transient thinking envelopes."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")
        await _assert_thinking_subject_unretained(self._jetstream)

        async def callback(message: Any) -> None:
            try:
                payload = cast(dict[str, Any], json.loads(message.data))
                if set(payload) != {"runId", "conversationId", "offset", "text"}:
                    raise ValueError("unexpected thinking envelope fields")
                run_id = UUID(str(payload["runId"]))
                conversation_id = UUID(str(payload["conversationId"]))
                offset = payload["offset"]
                text = payload["text"]
                if (
                    type(offset) is not int
                    or offset < 0
                    or not isinstance(text, str)
                    or not text
                    or len(text) > 4096
                ):
                    raise ValueError("invalid thinking envelope")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self.metrics.increment(
                    "aura.runtime.stream_delivery",
                    "thinking_stream_outcome",
                    outcome="malformed_envelope",
                    dependency="nats_transient",
                    error_class="validation",
                )
                return
            try:
                await handler(run_id, conversation_id, offset, text)
            except Exception:
                self.metrics.increment(
                    "aura.runtime.stream_delivery",
                    "thinking_stream_outcome",
                    outcome="delivery",
                    dependency="nats_transient",
                    error_class="delivery",
                )
                return

        self._thinking_subscription = await self._connection.subscribe(
            THINKING_EVENT_SUBJECT, cb=callback
        )
        return self._thinking_subscription

    async def subscribe_approval_events(self, handler: ApprovalEventHandler) -> Any:
        """Subscribe to approval wakeups after PostgreSQL replay."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")

        async def callback(message: Any) -> None:
            try:
                payload = cast(dict[str, Any], json.loads(message.data))
                if set(payload) != {"eventId"}:
                    return
                await handler(UUID(str(payload["eventId"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return

        self._approval_subscription = await self._connection.subscribe(
            APPROVAL_EVENT_SUBJECT, cb=callback
        )
        return self._approval_subscription

    async def subscribe_resumption_events(self, handler: ResumptionEventHandler) -> Any:
        """Subscribe to waiting-run wakeups after durable replay."""

        if self._connection is None:
            raise RuntimeError("NATS transport is not connected")

        async def callback(message: Any) -> None:
            try:
                payload = cast(dict[str, Any], json.loads(message.data))
                if set(payload) != {"eventId"}:
                    return
                await handler(UUID(str(payload["eventId"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return

        self._resumption_subscription = await self._connection.subscribe(
            RESUMPTION_EVENT_SUBJECT, cb=callback
        )
        return self._resumption_subscription

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.drain()
            self._connection = None
            self._jetstream = None
            self._event_subscription = None
            self._notification_subscription = None
            self._approval_subscription = None
            self._resumption_subscription = None
            self._thinking_subscription = None
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
        info = await jetstream.stream_info(AURA_RUNS_STREAM)
        subjects = _stream_subjects(info)
        if self.subject not in subjects:
            raise RuntimeError("AURA_RUNS does not contain the configured run subject")
        self._subscription = await jetstream.pull_subscribe(
            self.subject, durable=self.durable, stream=AURA_RUNS_STREAM
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
            **_command_metric_metadata(command, delivery_count),
        )
        if delivery_count > 1:
            self.metrics.increment(
                self.component,
                "consumer_redelivered",
                **_command_metric_metadata(command, delivery_count),
            )
            self.metrics.increment(
                self.component,
                "outbox_duplicates",
                **_command_metric_metadata(command, delivery_count),
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
                    _command_metric_metadata(command, self._pending_delivery_count)
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
                    **_command_metric_metadata(command, self._pending_delivery_count),
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
                    error_class=error_class,
                    **_command_metric_metadata(command, self._pending_delivery_count),
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
                    error_class=error_class,
                    **_command_metric_metadata(command, self._pending_delivery_count),
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
    """Durable pull consumer for generic identifier-only commands.

    Unlike :class:`NatsRunConsumer`, this path parses the versioned envelope
    through ``OutboxCommand.from_record``. It accepts both legacy identifier
    commands (memory) and generic aggregate commands (governance/resumption),
    without coupling a worker to an adapter-specific payload shape.
    """

    emits_consumer_metrics = True

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
            command_id = UUID(str(payload["commandId"]))
            created_at = datetime.fromisoformat(str(payload["createdAt"]))
            command = OutboxCommand.from_record(
                command_id,
                self.subject,
                payload,
                created_at,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.metrics.increment(self.component, "errors", error_class="delivery")
            raise ValueError("invalid generic command metadata") from None
        self._pending_command = command
        self.metrics.increment(
            self.component,
            "consumer_received",
            **_command_metric_metadata(command, delivery_count),
        )
        if delivery_count > 1:
            self.metrics.increment(
                self.component,
                "consumer_redelivered",
                **_command_metric_metadata(command, delivery_count),
            )
            self.metrics.increment(
                self.component,
                "outbox_duplicates",
                **_command_metric_metadata(command, delivery_count),
            )
        return command


def _stream_subjects(info: Any) -> tuple[str, ...]:
    raw: Any = getattr(getattr(info, "config", None), "subjects", None)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(value) for value in cast(list[Any] | tuple[Any, ...], raw))


async def _configured_stream_subjects(jetstream: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Enumerate every configured JetStream subject without assuming one API shape."""

    stream_infos: Any = getattr(jetstream, "streams_info", None)
    if callable(stream_infos):
        result: Any = stream_infos()
        if hasattr(result, "__aiter__"):
            entries: list[tuple[str, tuple[str, ...]]] = []
            async for info in result:
                config = getattr(info, "config", None)
                name = str(getattr(config, "name", ""))
                entries.append((name, _stream_subjects(info)))
            return tuple(entries)
        if hasattr(result, "__await__"):
            result = await result
        infos: tuple[Any, ...] = (
            tuple(cast(list[Any] | tuple[Any, ...], result))
            if isinstance(result, (list, tuple))
            else (result,)
        )
        return tuple(
            (
                str(getattr(getattr(info, "config", None), "name", "")),
                _stream_subjects(info),
            )
            for info in infos
        )
    stream_names: Any = getattr(jetstream, "stream_names", None)
    if not callable(stream_names):
        raise RuntimeError("JetStream stream enumeration unavailable")
    result: Any = stream_names()
    if hasattr(result, "__aiter__"):
        names = [str(name) async for name in result]
    else:
        resolved = await result if hasattr(result, "__await__") else result
        names = [str(name) for name in resolved]
    entries = []
    for name in names:
        info = await jetstream.stream_info(name)
        entries.append((name, _stream_subjects(info)))
    return tuple(entries)


def _nats_subject_matches(pattern: str, concrete: str) -> bool:
    """Return whether a NATS wildcard pattern can capture one concrete subject."""

    pattern_tokens = pattern.split(".")
    concrete_tokens = concrete.split(".")
    index = 0
    for token in pattern_tokens:
        if token == ">":
            return index < len(concrete_tokens) and token == pattern_tokens[-1]
        if index >= len(concrete_tokens) or (token != "*" and token != concrete_tokens[index]):
            return False
        index += 1
    return index == len(concrete_tokens)


async def _assert_thinking_subject_unretained(jetstream: Any) -> None:
    """Fail closed if any JetStream stream could retain the live-only subject."""

    if jetstream is None:
        raise RuntimeError("JetStream is not connected")
    configured = await _configured_stream_subjects(jetstream)
    overlaps = [
        f"{name}:{subject}"
        for name, subjects in configured
        for subject in subjects
        if _nats_subject_matches(subject, THINKING_EVENT_SUBJECT)
    ]
    if overlaps:
        raise RuntimeError("live thinking subject overlaps configured JetStream retention")


async def _ensure_command_stream(jetstream: Any, subjects: tuple[str, ...]) -> None:
    requested = tuple(dict.fromkeys(subjects))
    try:
        await jetstream.add_stream(name=AURA_RUNS_STREAM, subjects=requested)
        return
    except Exception as creation_error:
        info = await _lookup_created_stream(jetstream, creation_error)
        config = getattr(info, "config", None)
        if str(getattr(config, "name", "")) != AURA_RUNS_STREAM:
            raise RuntimeError(
                "AURA_RUNS lookup returned a differently named stream"
            ) from creation_error
        raw_subjects = cast(object, getattr(config, "subjects", None))
        if not isinstance(raw_subjects, (list, tuple)):
            raise RuntimeError("AURA_RUNS subject configuration is unavailable") from creation_error
        existing = tuple(
            str(subject)
            for subject in cast(list[Any] | tuple[Any, ...], raw_subjects)
        )
        missing = [subject for subject in requested if subject not in existing]
        if missing:
            # Upgrade an older AURA_RUNS stream in place.  The stream retains
            # all existing messages while adding the versioned memory subject.
            await jetstream.update_stream(
                name=AURA_RUNS_STREAM,
                subjects=list(dict.fromkeys(existing + tuple(missing))),
            )


async def _lookup_created_stream(jetstream: Any, creation_error: Exception) -> Any:
    """Bound a lost-create race and preserve the provider error if absent."""

    for attempt in range(_STREAM_LOOKUP_ATTEMPTS):
        try:
            info = await jetstream.stream_info(AURA_RUNS_STREAM)
        except Exception:
            if attempt + 1 == _STREAM_LOOKUP_ATTEMPTS:
                raise creation_error from None
            await asyncio.sleep(_STREAM_LOOKUP_DELAY_SECONDS * (attempt + 1))
            continue
        config = getattr(info, "config", None)
        if str(getattr(config, "name", "")) == AURA_RUNS_STREAM:
            return info
        if attempt + 1 == _STREAM_LOOKUP_ATTEMPTS:
            raise creation_error
        await asyncio.sleep(_STREAM_LOOKUP_DELAY_SECONDS * (attempt + 1))
    raise creation_error


def _optional_command_metadata(command: OutboxCommand) -> dict[str, str]:
    return identifier_trace_metadata(command)


def _command_metric_metadata(
    command: OutboxCommand,
    delivery_count: int,
) -> dict[str, str]:
    """Build bounded command trace metadata for both legacy and generic envelopes."""

    identity = command.run_id or command.aggregate_id or command.id
    result = {
        "trace_id": (command.correlation_id or identity).hex,
        "command_id": str(command.id),
        "correlation_id": str(command.correlation_id or identity),
        "causation_id": str(command.causation_id or identity),
        "delivery_count": str(delivery_count),
        **_optional_command_metadata(command),
    }
    if command.run_id is not None:
        result["run_id"] = str(command.run_id)
    if command.conversation_id is not None:
        result["conversation_id"] = str(command.conversation_id)
    return result
