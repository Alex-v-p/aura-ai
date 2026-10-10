"""Scheduler acceptance checks for atomic due-reminder delivery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aura_core.entrypoints.scheduler.app import ReminderScheduler, run_once
from aura_core.platform.telemetry import MetadataMetrics

ROOT = Path(__file__).parents[1]
SCHEDULER = ROOT / "src/aura_core/entrypoints/scheduler/app.py"
AUTOMATION = ROOT / "src/aura_core/domains/automation"


def _scheduler_source() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def test_scheduler_is_thin_and_uses_shared_delivery_use_case() -> None:
    source = _scheduler_source()
    assert "reminder" in source.lower()
    assert "delivery" in source.lower()
    assert "ollama" not in source.lower()
    assert "browser" not in source.lower()
    assert "provider" not in source.lower()
    assert "credentials" not in source.lower()


def test_scheduler_claim_is_bounded_locked_and_retryable() -> None:
    source = (
        "\n".join(path.read_text(encoding="utf-8") for path in AUTOMATION.rglob("*.py"))
        + _scheduler_source()
    ).upper()
    assert "FOR UPDATE SKIP LOCKED" in source or "SKIP_LOCKED=TRUE" in source
    assert (
        "LIMIT 100" in source
        or "LIMIT :LIMIT" in source
        or "BATCH_SIZE = 100" in source
        or "SCAN_BATCH_SIZE = 100" in source
    )
    assert "SCHEDULED" in source
    assert "DUE" in source or "DUE_AT" in source
    assert "ROLLBACK" in source or "SESSION" in source


def test_shared_delivery_transaction_contains_all_durable_side_effects() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in AUTOMATION.rglob("*.py"))
    lowered = source.lower()
    for marker in ("fired", "notification", "audit", "replay", "outbox"):
        assert marker in lowered
    event_start = lowered.index("notificationeventrow")
    outbox_start = lowered.index("make_aggregate_command")
    assert "content" not in lowered[event_start:outbox_start]


class _Delivery:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else 0
        self.error = error
        self.limits: list[int] = []

    async def deliver_due(self, *, limit: int) -> object:
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        return self.result

    async def fire_due(self, *, limit: int) -> object:
        return await self.deliver_due(limit=limit)


@pytest.mark.asyncio
async def test_scheduler_run_once_bounds_batch_and_records_metadata_only_metrics() -> None:
    delivery = _Delivery(type("Result", (), {"fired_count": 3, "due_lag_ms": 12.5})())
    metrics = MetadataMetrics()
    assert await run_once(delivery, metrics=metrics) == 3
    assert delivery.limits == [100]
    measurements = metrics.snapshot()
    assert {item.metric for item in measurements} >= {
        "reminder_fired_count",
        "reminder_due_lag_ms",
    }
    assert all("content" not in repr(item).lower() for item in measurements)


@pytest.mark.asyncio
async def test_scheduler_run_once_retries_after_failure_without_swallowing_error() -> None:
    delivery = _Delivery(error=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError, match="database unavailable"):
        await run_once(delivery)
    assert delivery.limits == [100]


def test_scheduler_rejects_unbounded_batches_and_intervals() -> None:
    with pytest.raises(ValueError):
        asyncio.run(run_once(_Delivery(), batch_size=101))
    with pytest.raises(ValueError):
        ReminderScheduler(_Delivery(), interval_seconds=0)
