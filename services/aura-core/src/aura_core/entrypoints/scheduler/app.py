"""Thin one-shot reminder scheduler composition root.

The scheduler owns polling only.  Firing, notification creation, audit, replay
events, and transactional outbox staging are owned by the automation public
application service and therefore remain one database transaction.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol, cast
from uuid import uuid4

from aura_core.domains.automation.briefings.scheduler import (
    BriefingDueRepository,
    BriefingPollingApplication,
    BriefingScheduler,
    BriefingSchedulerApplication,
    BriefingScheduleScanResult,
)
from aura_core.domains.automation.briefings.scheduler import (
    run_once as run_briefing_once,
)
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox import SqlOutboxRepository
from aura_core.platform.telemetry import (
    MetadataMetrics,
    TelemetryLifecycle,
    make_telemetry_exporter,
    new_span_id,
)

LOGGER = logging.getLogger(__name__)
SCHEDULER_COMPONENT = "aura.automation.reminder_scheduler"
SCAN_INTERVAL_SECONDS = 1.0
SCAN_BATCH_SIZE = 100


class ReminderDelivery(Protocol):
    """Public Core use case consumed by the scheduler entrypoint."""

    async def deliver_due(self, *, limit: int) -> object: ...


def _delivery_method(delivery: ReminderDelivery) -> Callable[..., object]:
    """Resolve the versioned delivery verb while keeping test doubles small."""

    method = getattr(delivery, "deliver_due", None)
    if not callable(method):
        # ``fire_due`` was the draft name used by the approved scheduler
        # contract.  Keep this compatibility shim for older in-process
        # compositions; production composition uses ``deliver_due``.
        method = getattr(delivery, "fire_due", None)
    if not callable(method):
        raise TypeError("reminder delivery use case is missing deliver_due")
    return method


def _result_count(result: object) -> int:
    """Read only bounded metadata from a delivery result."""

    if type(result) is int:
        return max(0, result)
    for name in ("fired_count", "count", "processed"):
        value = getattr(result, name, None)
        if type(value) is int:
            return max(0, value)
    return 0


def _result_lag_ms(result: object) -> float | None:
    value = getattr(result, "due_lag_ms", None)
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _result_has_work(result: object, fired: int) -> bool:
    """Return whether the bounded scan performed meaningful work.

    A successful empty poll is intentionally silent.  Delivery results may
    still contain useful work when no new notification was inserted (for
    example, duplicate prevention or recurring-occurrence accounting), so do
    not reduce this decision to ``fired_count`` alone.
    """

    if fired > 0:
        return True
    for name in (
        "duplicate_count",
        "generated_count",
        "skipped_count",
        "missed_count",
    ):
        value = getattr(result, name, None)
        if type(value) is int and value > 0:
            return True
    return False


async def run_once(
    delivery: ReminderDelivery,
    *,
    metrics: MetadataMetrics | None = None,
    batch_size: int = SCAN_BATCH_SIZE,
) -> int:
    """Run one bounded scan and return the number of reminders fired."""

    if not 1 <= batch_size <= SCAN_BATCH_SIZE:
        raise ValueError("scheduler batch size must be between 1 and 100")
    started = monotonic()
    # Trace roots are 128-bit (32 hexadecimal characters); span IDs remain
    # the shorter 64-bit values used by the telemetry contract.
    trace_id = uuid4().hex
    span_id = new_span_id()
    try:
        result = _delivery_method(delivery)(limit=batch_size)
        if inspect.isawaitable(result):
            result = await result
        fired = _result_count(result)
        if metrics is not None and _result_has_work(result, fired):
            if fired > 0:
                metrics.observe(
                    SCHEDULER_COMPONENT,
                    "reminder_fired_count",
                    float(fired),
                    outcome="ok",
                )
            lag = _result_lag_ms(result)
            if lag is not None:
                metrics.observe(
                    SCHEDULER_COMPONENT,
                    "reminder_due_lag_ms",
                    lag,
                    outcome="ok",
                )
            metrics.record_span(
                SCHEDULER_COMPONENT,
                "reminder.scan",
                (monotonic() - started) * 1000,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                dependency="postgresql",
                outcome="ok",
                result_count=str(fired),
            )
        return fired
    except Exception:
        if metrics is not None:
            metrics.increment(
                SCHEDULER_COMPONENT,
                "reminder_failure_count",
                dependency="postgresql",
                outcome="error",
                error_class="persistence",
            )
            metrics.record_span(
                SCHEDULER_COMPONENT,
                "reminder.scan",
                (monotonic() - started) * 1000,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                dependency="postgresql",
                outcome="error",
                error_class="persistence",
            )
        raise


class ReminderScheduler:
    """Clock-driven scheduler with bounded polling and retry after failures."""

    def __init__(
        self,
        delivery: ReminderDelivery,
        *,
        metrics: MetadataMetrics | None = None,
        interval_seconds: float = SCAN_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("scheduler interval must be positive")
        self.delivery = delivery
        self.metrics = metrics
        self.interval_seconds = interval_seconds
        self._sleep = sleep

    async def run(self) -> None:
        while True:
            started = monotonic()
            try:
                await run_once(self.delivery, metrics=self.metrics)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed transaction leaves rows scheduled.  Keep the
                # process alive so the next scan can retry after a database
                # or transient persistence failure.
                LOGGER.exception("reminder scheduler scan failed")
            await self._sleep(max(0.0, self.interval_seconds - (monotonic() - started)))


async def run_scheduler(settings: Settings | None = None) -> None:
    """Compose the database-only production scheduler and run it forever."""

    from aura_core.bootstrap.briefing_uow import briefing_repository
    from aura_core.bootstrap.notification_uow import notification_repository, notification_service

    effective = settings or Settings(role="scheduler")
    engine = make_engine(effective.database_url)
    sessions = session_factory(engine)
    metrics, telemetry = make_scheduler_telemetry(effective)
    delivery = notification_service(notification_repository(sessions, metrics=metrics))
    briefing_repo = briefing_repository(sessions, metrics=metrics)
    briefing_application = BriefingPollingApplication(
        cast(BriefingDueRepository, briefing_repo), SqlOutboxRepository(), metrics=metrics
    )
    await telemetry.start()
    try:
        await asyncio.gather(
            ReminderScheduler(delivery, metrics=metrics).run(),
            BriefingScheduler(briefing_application, metrics=metrics).run(),
        )
    finally:
        await telemetry.stop()
        await engine.dispose()


async def run_briefing_scan(
    application: BriefingSchedulerApplication,
    *,
    metrics: MetadataMetrics | None = None,
    batch_size: int = 100,
) -> BriefingScheduleScanResult:
    """Run one bounded briefing poll through the shared application port."""

    return await run_briefing_once(application, metrics=metrics, batch_size=batch_size)


def make_scheduler_telemetry(
    settings: Settings | None = None,
) -> tuple[MetadataMetrics, TelemetryLifecycle]:
    """Build the production bounded exporter and lifecycle manager."""

    config = settings or Settings(role="scheduler")
    metrics = MetadataMetrics(
        exporter=make_telemetry_exporter(
            enabled=config.telemetry_otlp_enabled,
            endpoint=config.telemetry_otlp_endpoint,
            credential=config.telemetry_otlp_credential or None,
            queue_capacity=config.telemetry_otlp_queue_capacity,
            max_attempts=config.telemetry_otlp_max_attempts,
            timeout_seconds=config.telemetry_otlp_timeout_seconds,
            deployment_id=config.telemetry_deployment_id,
            git_commit=config.telemetry_git_commit,
            environment=config.environment,
            host_id=config.telemetry_host_id,
            service_role=config.role,
            image_version=config.telemetry_image_version,
            configuration_version=config.telemetry_configuration_version,
        )
    )
    return metrics, TelemetryLifecycle(metrics)


def main() -> None:
    asyncio.run(run_scheduler())


__all__ = [
    "BriefingScheduler",
    "ReminderScheduler",
    "main",
    "make_scheduler_telemetry",
    "run_briefing_scan",
    "run_once",
    "run_scheduler",
]
