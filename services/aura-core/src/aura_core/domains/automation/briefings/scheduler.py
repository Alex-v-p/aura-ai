"""Polling-only scheduled briefing admission orchestration.

The scheduler is deliberately a very small composition boundary.  It claims a
bounded number of due occurrences through the Briefings application and hands
those claims to the application's admission operation.  It does not carry an
owner principal, assemble context, invoke a model, or read persistence.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4, uuid5

from aura_core.domains.automation.briefings.public import (
    BRIEFING_ID_NAMESPACE,
    BriefingOccurrence,
)
from aura_core.platform.outbox import BRIEFING_WAKE_SUBJECT, OutboxCommand, make_aggregate_command
from aura_core.platform.telemetry import MetadataMetrics, new_span_id

BRIEFING_SCHEDULER_COMPONENT = "aura.automation.briefing_scheduler"
BRIEFING_SCAN_OPERATION = "briefing.schedule.scan"
BRIEFING_SCAN_BATCH_SIZE = 100


class BriefingSchedulerApplication(Protocol):
    """Public application port used by the Core scheduler process."""

    async def poll_due(self, *, limit: int) -> object: ...


class BriefingDueRepository(Protocol):
    """Public inward port for the repository's atomic due-occurrence claim."""

    async def claim_due_occurrences_and_stage(
        self,
        now: datetime,
        *,
        limit: int,
        stage: Callable[[object, BriefingOccurrence], object | Awaitable[object]],
    ) -> tuple[BriefingOccurrence, ...]: ...


class BriefingOutboxCommandStage(Protocol):
    """Structural port for transaction-local generic-command staging."""

    def stage_command(self, session: Any, *, command: OutboxCommand) -> object: ...


class BriefingPollingApplication:
    """Compose due claims with the existing identifier-only outbox transport.

    The state repository owns the row-lock transaction.  This adapter owns no
    schedule rules or owner authority; it only converts claimed public DTOs to
    the generic wake envelope consumed by :class:`BriefingWorker`.
    """

    def __init__(
        self,
        repository: BriefingDueRepository,
        outbox: BriefingOutboxCommandStage,
        *,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        self.repository = repository
        self.outbox = outbox
        self.metrics = metrics

    async def poll_due(self, *, limit: int) -> BriefingScheduleScanResult:
        if not 1 <= limit <= BRIEFING_SCAN_BATCH_SIZE:
            raise ValueError("briefing scheduler batch size must be between 1 and 100")
        now = datetime.now(UTC)
        started = monotonic()
        trace_id = uuid4().hex
        span_id = new_span_id()
        async def stage(session: Any, occurrence: BriefingOccurrence) -> object:
            command_id = uuid5(BRIEFING_ID_NAMESPACE, f"wake:{occurrence.id}")
            command = make_aggregate_command(
                BRIEFING_WAKE_SUBJECT,
                command_id,
                "automation.briefing",
                occurrence.schedule_id,
                identifiers=(
                    ("eventId", command_id),
                    ("scheduleId", occurrence.schedule_id),
                    ("occurrenceId", occurrence.id),
                    ("occurrenceStatus", occurrence.status.value),
                ),
            )
            result = self.outbox.stage_command(session, command=command)
            if inspect.isawaitable(result):
                await result
            return result

        try:
            claims = await self.repository.claim_due_occurrences_and_stage(
                now,
                limit=limit,
                stage=stage,
            )
        except Exception:
            if self.metrics is not None:
                self.metrics.record_span(
                    BRIEFING_SCHEDULER_COMPONENT,
                    "briefing.schedule.claim",
                    (monotonic() - started) * 1000,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=None,
                    dependency="postgresql",
                    outcome="error",
                    error_class="persistence",
                )
            raise
        if self.metrics is not None:
            self.metrics.record_span(
                BRIEFING_SCHEDULER_COMPONENT,
                "briefing.schedule.claim",
                (monotonic() - started) * 1000,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                dependency="postgresql",
                outcome="ok",
                result_count=str(len(claims)),
            )
        lag = (
            max(0.0, (now - min(item.due_at for item in claims)).total_seconds() * 1000)
            if claims
            else None
        )
        return BriefingScheduleScanResult(
            claimed_count=len(claims),
            admitted_count=len(claims),
            due_lag_ms=lag,
        )


@dataclass(frozen=True, slots=True)
class BriefingScheduleScanResult:
    """Content-free metadata returned from one bounded scheduler pass."""

    claimed_count: int = 0
    admitted_count: int = 0
    skipped_count: int = 0
    missed_count: int = 0
    due_lag_ms: float | None = None

    @property
    def processed_count(self) -> int:
        return self.claimed_count + self.skipped_count + self.missed_count


def _value(result: object, *names: str) -> int:
    for name in names:
        value = getattr(result, name, None)
        if type(value) is int:
            return max(0, value)
    return 0


def _scan_result(result: object) -> BriefingScheduleScanResult:
    if isinstance(result, BriefingScheduleScanResult):
        return result
    due_lag = getattr(result, "due_lag_ms", None)
    return BriefingScheduleScanResult(
        claimed_count=_value(result, "claimed_count", "claim_count", "processed_count"),
        admitted_count=_value(result, "admitted_count", "generated_count"),
        skipped_count=_value(result, "skipped_count"),
        missed_count=_value(result, "missed_count"),
        due_lag_ms=float(due_lag) if isinstance(due_lag, (int, float)) and due_lag >= 0 else None,
    )


def _application_method(application: object) -> Callable[..., object]:
    method = getattr(application, "poll_due", None)
    if not callable(method):
        method = getattr(application, "claim_due", None)
    if not callable(method):
        raise TypeError("briefing scheduler application is missing poll_due")
    return method


async def run_once(
    application: BriefingSchedulerApplication,
    *,
    metrics: MetadataMetrics | None = None,
    batch_size: int = BRIEFING_SCAN_BATCH_SIZE,
) -> BriefingScheduleScanResult:
    """Run one bounded, retryable due-occurrence poll."""

    if not 1 <= batch_size <= BRIEFING_SCAN_BATCH_SIZE:
        raise ValueError("briefing scheduler batch size must be between 1 and 100")
    try:
        result = _application_method(application)(limit=batch_size)
        if inspect.isawaitable(result):
            result = await result
        scan = _scan_result(result)
        if metrics is not None and scan.processed_count > 0:
            metrics.observe(
                BRIEFING_SCHEDULER_COMPONENT,
                "briefing_outcome_count",
                float(scan.processed_count),
                outcome="ok",
            )
            if scan.due_lag_ms is not None:
                metrics.observe(
                    BRIEFING_SCHEDULER_COMPONENT,
                    "briefing_schedule_lag_ms",
                    scan.due_lag_ms,
                    outcome="ok",
                )
        return scan
    except Exception:
        if metrics is not None:
            metrics.increment(
                BRIEFING_SCHEDULER_COMPONENT,
                "briefing_outcome_count",
                outcome="error",
                error_class="persistence",
            )
        raise


class BriefingScheduler:
    """Clock-driven polling loop with bounded work and failure recovery."""

    def __init__(
        self,
        application: BriefingSchedulerApplication,
        *,
        metrics: MetadataMetrics | None = None,
        interval_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("briefing scheduler interval must be positive")
        self.application = application
        self.metrics = metrics
        self.interval_seconds = interval_seconds
        self._sleep = sleep

    async def run(self) -> None:
        while True:
            started = monotonic()
            try:
                await run_once(self.application, metrics=self.metrics)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed transaction leaves the occurrence claim recoverable;
                # the next bounded poll retries it.
                pass
            await self._sleep(max(0.0, self.interval_seconds - (monotonic() - started)))


__all__ = [
    "BRIEFING_SCAN_BATCH_SIZE",
    "BRIEFING_SCHEDULER_COMPONENT",
    "BriefingScheduleScanResult",
    "BriefingPollingApplication",
    "BriefingDueRepository",
    "BriefingOutboxCommandStage",
    "BriefingScheduler",
    "BriefingSchedulerApplication",
    "run_once",
]
