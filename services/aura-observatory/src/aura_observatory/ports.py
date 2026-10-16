"""Narrow storage ports owned by Observatory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from aura_observatory.contract import MetadataObservation


class InsertOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class DeletionResult:
    observations: int
    receipts: int
    runs: int


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

    async def commit_ingest_boundary(self, observation_id: str) -> None: ...

    async def next_ingest_sequence(self, observation_id: str) -> int: ...

    async def set_observed_at(self, observation_id: str, observed_at: datetime) -> None: ...

    async def mark_ingest_visible(self, sequence: int) -> None: ...

    async def abort(self, observation_id: str) -> None: ...


class ObservatoryCatalog(Protocol):
    async def record_component(self, component_id: str, version: str) -> None: ...

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None: ...

    async def increment_counter(self, name: str, amount: int = 1) -> None: ...

    async def counters(self) -> Mapping[str, int]: ...

    async def last_ingest_at(self) -> datetime | None: ...



class ObservatoryHealthPort(Protocol):
    """Explicit narrow port for durable self-monitoring state and events."""

    async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None: ...
    async def record_rejection(self, reason: str, amount: int = 1) -> None: ...
    async def record_duplicate(self, amount: int = 1) -> None: ...
    async def record_storage_failure(self, amount: int = 1) -> None: ...
    async def record_latency(self, kind: str, duration_ms: float) -> None: ...
    async def record_worker_heartbeat(self, at: datetime | None = None) -> None: ...
    async def record_visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | None = None,
    ) -> None: ...
    async def record_query_freshness(self, observed_at: datetime | None) -> None: ...
    async def health_snapshot(self) -> dict[str, object] | None: ...
    async def save_health_snapshot(self, snapshot: dict[str, object]) -> None: ...
    async def health_history(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]: ...
    async def health_events_after(
        self, last_event_id: str | None, upper_sequence: int | None = None
    ) -> list[dict[str, object]]: ...
    async def health_event_sequence(self, event_id: str) -> int | None: ...
    async def latest_health_event_sequence(self) -> int: ...
    async def metric_series(self) -> list[dict[str, object]]: ...


class NullObservatoryHealthPort:
    """Explicit no-op health port for isolated unit compositions."""

    async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None:
        del observed_at, amount

    async def record_rejection(self, reason: str, amount: int = 1) -> None:
        del reason, amount

    async def record_duplicate(self, amount: int = 1) -> None:
        del amount

    async def record_storage_failure(self, amount: int = 1) -> None:
        del amount

    async def record_latency(self, kind: str, duration_ms: float) -> None:
        del kind, duration_ms

    async def record_worker_heartbeat(self, at: datetime | None = None) -> None:
        del at

    async def record_visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | None = None,
    ) -> None:
        del watermark, lag_count, backlog_count, oldest_backlog_age_ms

    async def record_query_freshness(self, observed_at: datetime | None) -> None:
        del observed_at

    async def health_snapshot(self) -> dict[str, object] | None:
        return None

    async def save_health_snapshot(self, snapshot: dict[str, object]) -> None:
        del snapshot

    async def health_history(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]:
        del cursor, from_time, to_time, limit
        return {"items": [], "nextCursor": None, "snapshotAt": None}

    async def health_events_after(
        self, last_event_id: str | None, upper_sequence: int | None = None
    ) -> list[dict[str, object]]:
        del last_event_id, upper_sequence
        return []

    async def health_event_sequence(self, event_id: str) -> int | None:
        del event_id
        return None

    async def latest_health_event_sequence(self) -> int:
        return 0

    async def metric_series(self) -> list[dict[str, object]]:
        return []


class ObservatoryQueryPort(Protocol):
    """Query-specific inward port; adapters must not expose table hydration."""

    async def overview_query(self) -> dict[str, object]: ...

    async def components_query(self) -> list[dict[str, object]]: ...

    async def component_collection_query(
        self,
        *,
        definition_state: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]: ...

    async def component_definition_query(
        self, component_id: str, component_version: str
    ) -> dict[str, object] | None: ...

    async def component_dependencies_query(
        self,
        component_id: str,
        component_version: str,
        *,
        depth: int = 1,
        max_nodes: int = 100,
    ) -> dict[str, object] | None: ...

    async def deployments_query(self) -> list[dict[str, object]]: ...

    async def run_query(
        self, filters: Any, *, cursor: str | None, limit: int
    ) -> dict[str, Any]: ...

    async def run_detail_query(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None: ...

    async def trace_detail_query(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None: ...

    def events_after_query(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, object]]: ...

    async def component_health_query(
        self,
        component_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        *,
        component_version: str | None = None,
        point_limit: int = 200,
    ) -> dict[str, object] | None: ...

    async def health_snapshot_query(self) -> dict[str, object] | None: ...

    async def health_history_query(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]: ...


class ObservatoryReferencePort(Protocol):
    """Small in-memory/reference seam used only by deterministic composition."""

    observations: dict[str, MetadataObservation]
    actions: dict[str, Any]
    event_sequence: dict[str, int]
    health_events: list[dict[str, object]]
    snapshot_sequence: int

    async def list_observations(self) -> list[MetadataObservation]: ...
    async def components(self) -> list[dict[str, object]]: ...
    async def deployments(self) -> list[dict[str, object]]: ...
    async def ingest_overview(self) -> dict[str, object]: ...
    async def wait_for_event(self, timeout: float) -> None: ...
    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult: ...



class ObservatoryRetentionPort(Protocol):
    async def find_retention_by_key(self, key: str) -> Any | None: ...

    async def save_retention(self, action: Any) -> bool: ...

    async def enqueue_retention(self, action: Any) -> None: ...

    async def attach_retention(self, action: Any) -> None: ...

    async def get_retention(self, action_id: str) -> Any | None: ...

    async def pending_retentions(self) -> list[Any]: ...

    async def claim_pending_retention(self, action_id: str) -> Any | None: ...

    async def prepare_retention(self, action: Any) -> None: ...

    async def update_retention(self, action: Any) -> None: ...

    async def persisted_metric_series(self) -> list[dict[str, object]]: ...


__all__ = [
    "ClaimOutcome",
    "DeletionResult",
    "InsertOutcome",
    "ObservationIdRegistry",
    "ObservationStore",
    "ObservatoryCatalog",
    "ObservatoryHealthPort",
    "NullObservatoryHealthPort",
    "ObservatoryQueryPort",
    "ObservatoryReferencePort",
    "ObservatoryRetentionPort",
]
