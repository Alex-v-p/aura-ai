"""AURA-0096 projection-outbox worker recovery coverage."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio

import pytest
from aura_observatory.entrypoints.evaluation_worker import _run_projection_recovery


class _RecoveryService:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.limits: list[int] = []

    async def recover_projection_outbox(self, *, limit: int) -> int:
        self.limits.append(limit)
        self.stop.set()
        return 1


@pytest.mark.asyncio
async def test_projection_recovery_loop_drains_one_bounded_page() -> None:
    stop = asyncio.Event()
    service = _RecoveryService(stop)

    await _run_projection_recovery(service, stop, poll_seconds=0)

    assert service.limits == [16]


@pytest.mark.asyncio
async def test_projection_recovery_loop_survives_provider_failure() -> None:
    stop = asyncio.Event()

    class FailingService:
        calls = 0

        async def recover_projection_outbox(self, *, limit: int) -> int:
            assert limit == 16
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider failure")
            stop.set()
            return 0

    service = FailingService()
    await _run_projection_recovery(service, stop, poll_seconds=0)

    assert service.calls == 2
