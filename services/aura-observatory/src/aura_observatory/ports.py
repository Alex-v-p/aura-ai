"""Narrow storage ports owned by Observatory."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from aura_observatory.contract import MetadataObservation


class InsertOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    RETRYABLE = "retryable"


class ClaimOutcome(StrEnum):
    ACQUIRED = "acquired"
    DUPLICATE = "duplicate"
    PENDING = "pending"


class ObservationStore(Protocol):
    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome: ...


class ObservationIdRegistry(Protocol):
    """Durable, atomic pending/completed state for stable observation IDs."""

    async def claim(self, observation_id: str) -> ClaimOutcome: ...

    async def complete(self, observation_id: str) -> None: ...


class ObservatoryCatalog(Protocol):
    async def record_component(self, component_id: str, version: str) -> None: ...

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None: ...

    async def increment_counter(self, name: str, amount: int = 1) -> None: ...

    async def counters(self) -> Mapping[str, int]: ...

    async def last_ingest_at(self) -> datetime | None: ...


__all__ = [
    "ClaimOutcome",
    "InsertOutcome",
    "ObservationIdRegistry",
    "ObservationStore",
    "ObservatoryCatalog",
]
