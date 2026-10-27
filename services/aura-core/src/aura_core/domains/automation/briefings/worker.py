"""Identifier-only briefing worker and durable settlement boundary."""

from __future__ import annotations

import inspect
from typing import Protocol
from uuid import UUID, uuid5

from aura_core.domains.automation.briefings.execution import BriefingRunResult
from aura_core.domains.automation.briefings.public import BRIEFING_ID_NAMESPACE
from aura_core.domains.automation.briefings.recovery import MAX_RETRY_ATTEMPTS, retry_decision
from aura_core.platform.outbox import BRIEFING_WAKE_SUBJECT, OutboxCommand

BRIEFING_EVENT_TOPIC = BRIEFING_WAKE_SUBJECT
BRIEFING_AGGREGATE_TYPE = "automation.briefing"
TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "canceled", "skipped", "unknown", "settled"}
)


class BriefingWorkerApplication(Protocol):
    async def process_command(self, command: OutboxCommand) -> object: ...


class BriefingConsumer(Protocol):
    @property
    def delivery_count(self) -> int: ...

    async def receive(self) -> OutboxCommand | None: ...

    async def ack(self) -> object: ...

    async def nack(self, *, retry_delay: float, error_class: str) -> object: ...

    async def reject(self, *, error_class: str) -> object: ...


def _identifier(command: OutboxCommand, *names: str) -> UUID:
    for name in names:
        value = command.identifier(name)
        if value is not None:
            try:
                return UUID(value)
            except ValueError as exc:
                raise ValueError(f"invalid briefing {name}") from exc
    raise ValueError(f"briefing command is missing {names[0]}")


def validate_briefing_command(command: OutboxCommand) -> tuple[UUID, UUID]:
    """Validate the closed generic wake envelope and return its IDs."""

    if command.topic != BRIEFING_EVENT_TOPIC:
        raise ValueError("unexpected briefing command topic")
    if command.aggregate_type != BRIEFING_AGGREGATE_TYPE:
        raise ValueError("unexpected briefing aggregate")
    if command.aggregate_id is None:
        raise ValueError("briefing command is missing aggregate identity")
    schedule_id = _identifier(command, "scheduleId")
    occurrence_id = _identifier(command, "occurrenceId", "aggregateId")
    event_identity = command.identifier("eventId")
    if event_identity is None:
        raise ValueError("briefing command is missing event identity")
    try:
        aggregate_id = UUID(str(command.aggregate_id))
        event_id = UUID(str(event_identity))
    except (TypeError, ValueError) as exc:
        raise ValueError("briefing command has invalid aggregate or event identity") from exc
    if aggregate_id != schedule_id:
        raise ValueError("briefing command aggregate does not match schedule identity")
    if event_id != command.id:
        raise ValueError("briefing event identity does not match command identity")
    expected_wake_id = uuid5(BRIEFING_ID_NAMESPACE, f"wake:{occurrence_id}")
    if command.id != expected_wake_id:
        raise ValueError("briefing wake identity is not deterministic for occurrence")
    return schedule_id, occurrence_id


def is_terminal_settlement(result: object) -> bool:
    if not isinstance(result, BriefingRunResult):
        return False
    if not result.terminal or result.status.value not in TERMINAL_STATUSES:
        return False
    return bool(getattr(result, "durably_settled", False))


class BriefingWorker:
    """Process one wake command and require durable settlement before ack."""

    def __init__(self, application: BriefingWorkerApplication) -> None:
        self.application = application

    async def process(self, command: OutboxCommand) -> object:
        validate_briefing_command(command)
        method = getattr(self.application, "process_command", None)
        if not callable(method):
            method = getattr(self.application, "execute_occurrence", None)
        if not callable(method):
            raise TypeError("briefing application is missing command processing")
        result = method(command)
        if inspect.isawaitable(result):
            result = await result
        if not is_terminal_settlement(result):
            raise RuntimeError("briefing occurrence is not durably settled")
        return result


def _retry_delay(delivery_count: int) -> float:
    return min(60.0, max(1.0, 2.0 ** max(0, min(delivery_count, 6) - 1)))


async def run_once(worker: BriefingWorker, consumer: BriefingConsumer) -> object | None:
    """Run one generic outbox delivery; ack only after durable settlement."""

    try:
        command = await consumer.receive()
    except TypeError, ValueError:
        await consumer.reject(error_class="validation")
        return None
    if command is None:
        return None
    try:
        result = await worker.process(command)
    except TypeError, ValueError:
        await consumer.reject(error_class="validation")
        return None
    except Exception:
        count = int(getattr(consumer, "delivery_count", 1))
        if count >= MAX_RETRY_ATTEMPTS:
            finalize = getattr(worker.application, "fail_command", None)
            if callable(finalize):
                terminal = finalize(command, error_class="delivery_exhausted")
                if inspect.isawaitable(terminal):
                    terminal = await terminal
                if is_terminal_settlement(terminal):
                    await consumer.ack()
                    return terminal
            await consumer.nack(retry_delay=_retry_delay(count), error_class="delivery_exhausted")
        else:
            decision = retry_decision(count)
            await consumer.nack(retry_delay=decision.delay_seconds, error_class="delivery")
        return None
    await consumer.ack()
    return result


__all__ = [
    "BRIEFING_AGGREGATE_TYPE",
    "BRIEFING_EVENT_TOPIC",
    "BriefingConsumer",
    "BriefingWorker",
    "BriefingWorkerApplication",
    "is_terminal_settlement",
    "run_once",
    "validate_briefing_command",
]
