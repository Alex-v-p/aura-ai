"""Deterministic in-memory adapters for unit tests and local composition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from time import monotonic

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import ClaimOutcome, InsertOutcome


class MemoryObservationStore:
    def __init__(self) -> None:
        self.observations: dict[str, MetadataObservation] = {}

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        if observation.observation_id in self.observations:
            return InsertOutcome.DUPLICATE
        self.observations[observation.observation_id] = observation
        return InsertOutcome.ACCEPTED


class MemoryObservationIdRegistry:
    """Atomic pending/completed stable-ID registry used by adapter tests."""

    def __init__(
        self,
        *,
        lease_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._pending: dict[str, float] = {}
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()
        self._lease_seconds = lease_seconds
        self._clock = clock

    async def claim(self, observation_id: str) -> ClaimOutcome:
        async with self._lock:
            if observation_id in self._completed:
                return ClaimOutcome.DUPLICATE
            now = self._clock()
            claimed_at = self._pending.get(observation_id)
            if claimed_at is not None and now - claimed_at < self._lease_seconds:
                return ClaimOutcome.PENDING
            self._pending[observation_id] = now
            return ClaimOutcome.ACQUIRED

    async def complete(self, observation_id: str) -> None:
        async with self._lock:
            self._pending.pop(observation_id, None)
            self._completed.add(observation_id)


class MemoryCatalog:
    def __init__(self) -> None:
        self.components: set[tuple[str, str]] = set()
        self.deployments: set[tuple[str, str, str, str]] = set()
        self._counters: dict[str, int] = {}
        self._last_ingest_at: datetime | None = None

    async def record_component(self, component_id: str, version: str) -> None:
        self.components.add((component_id, version))
        self._last_ingest_at = datetime.now().astimezone()

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None:
        self.deployments.add((deployment_id, git_commit, environment, host_id))

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    async def counters(self) -> Mapping[str, int]:
        return dict(self._counters)

    async def last_ingest_at(self) -> datetime | None:
        return self._last_ingest_at


__all__ = ["MemoryCatalog", "MemoryObservationIdRegistry", "MemoryObservationStore"]
