
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import ClaimOutcome, DeletionResult, InsertOutcome


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _normalize_outcome(observation: MetadataObservation) -> str:
    value = observation.dimensions.get("outcome", observation.dimensions.get("status", "unknown"))
    return {
        "success": "succeeded",
        "completed": "succeeded",
        "ok": "succeeded",
        "error": "failed",
    }.get(
        value, value if value in {"succeeded", "failed", "cancelled"} else "unknown"
    )


class MemoryObservationStore:
    def __init__(self) -> None:
        self.observations: dict[str, MetadataObservation] = {}

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        if observation.observation_id in self.observations:
            return InsertOutcome.DUPLICATE
        self.observations[observation.observation_id] = observation
        return InsertOutcome.ACCEPTED


class MemoryObservationIdRegistry:
    def __init__(
        self,
        *,
        lease_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._pending: dict[str, float] = {}
        self._completed: set[str] = set()
        self._sequences: dict[str, int] = {}
        self._next_sequence = 0
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

    async def commit_ingest_boundary(self, observation_id: str) -> None:
        del observation_id

    async def next_ingest_sequence(self, observation_id: str) -> int:
        sequence = self._sequences.get(observation_id)
        if sequence is None:
            self._next_sequence += 1
            sequence = self._next_sequence
            self._sequences[observation_id] = sequence
        return sequence

    async def set_observed_at(self, observation_id: str, observed_at: datetime) -> None:
        del observation_id, observed_at

    async def mark_ingest_visible(self, sequence: int) -> None:
        del sequence

    async def abort(self, observation_id: str) -> None:
        del observation_id


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

class MemoryObservatoryStore:
    """Concurrency-safe reference store with the complete query semantics."""

    def __init__(self) -> None:
        self.observations: dict[str, MetadataObservation] = {}
        self.components_seen: dict[tuple[str, str], datetime] = {}
        self.deployments_seen: dict[tuple[str, str, str, str], datetime] = {}
        self.deployment_roles: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
        self._counter_values: dict[str, int] = defaultdict(int)
        self.events: list[MetadataObservation] = []
        self.event_sequence: dict[str, int] = {}
        self._next_sequence = 0
        self._last_accepted_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self.actions: dict[str, Any] = {}
        self.idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._event_condition = asyncio.Condition()

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        async with self._lock:
            if observation.observation_id in self.observations:
                return InsertOutcome.DUPLICATE
            self.observations[observation.observation_id] = observation
            self.events.append(observation)
            self._next_sequence += 1
            self.event_sequence[observation.observation_id] = self._next_sequence
            key = (observation.component.id, observation.component.version)
            self.components_seen.setdefault(key, observation.observed_at)
            deployment = observation.deployment
            dkey = (
                deployment.deployment_id,
                deployment.git_commit,
                deployment.environment,
                deployment.host_id,
            )
            self.deployments_seen.setdefault(dkey, observation.observed_at)
            self.deployment_roles[dkey].add(observation.service_role)
            async with self._event_condition:
                self._event_condition.notify_all()
            return InsertOutcome.ACCEPTED

    async def wait_for_event(self, timeout: float) -> None:
        try:
            async with self._event_condition:
                await asyncio.wait_for(self._event_condition.wait(), timeout=timeout)
        except TimeoutError:
            return

    async def record_component(self, component_id: str, version: str) -> None:
        self.components_seen.setdefault((component_id, version), datetime.now(UTC))

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None:
        self.deployments_seen.setdefault(
            (deployment_id, git_commit, environment, host_id), datetime.now(UTC)
        )

    async def record_deployment_role(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str, role: str
    ) -> None:
        key = (deployment_id, git_commit, environment, host_id)
        self.deployment_roles[key].add(role)

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self._counter_values[name] += amount
        now = datetime.now(UTC)
        if name == "accepted":
            self._last_accepted_at = now
        elif name in {"rejected", "malformed", "storage_failure"}:
            self._last_failure_at = now

    async def counters(self) -> Mapping[str, int]:
        return dict(self._counter_values)

    @property
    def snapshot_sequence(self) -> int:
        return self._next_sequence

    async def last_ingest_at(self) -> datetime | None:
        if not self.events:
            return None
        return max(item.observed_at for item in self.events)

    async def list_observations(self) -> list[MetadataObservation]:
        async with self._lock:
            return list(self.observations.values())

    async def components(self) -> list[dict[str, Any]]:
        observations = await self.list_observations()
        grouped: dict[tuple[str, str], list[MetadataObservation]] = defaultdict(list)
        for item in observations:
            grouped[(item.component.id, item.component.version)].append(item)
        result: list[dict[str, Any]] = []
        component_keys = set(grouped) | set(self.components_seen)
        for component_id, version in sorted(component_keys):
            items = grouped.get((component_id, version), [])
            values = [
                item.measurement.value for item in items if item.measurement.unit == "milliseconds"
            ]
            failures = sum(1 for item in items if _normalize_outcome(item) == "failed")
            result.append(
                {
                    "componentId": component_id,
                    "version": version,
                    "observations": len(items),
                    "errorCount": failures,
                    "errorRate": failures / len(items) if items else 0,
                    "latencyMs": sum(values) / len(values) if values else None,
                    "firstSeenAt": _iso(
                        min(
                            (item.observed_at for item in items),
                            default=self.components_seen.get((component_id, version)),
                        )
                    ),
                    "lastSeenAt": _iso(
                        max(
                            (item.observed_at for item in items),
                            default=self.components_seen.get((component_id, version)),
                        )
                    ),
                }
            )
        return result

    async def deployments(self) -> list[dict[str, Any]]:
        return [
            {
                "deploymentId": deployment_id,
                "gitCommit": commit,
                "environment": environment,
                "hostId": host_id,
                "serviceRoles": sorted(
                    self.deployment_roles.get((deployment_id, commit, environment, host_id), set())
                ),
                "firstSeenAt": _iso(when),
                "lastSeenAt": _iso(when),
            }
            for (deployment_id, commit, environment, host_id), when in sorted(
                self.deployments_seen.items()
            )
        ]

    async def ingest_overview(self) -> dict[str, Any]:
        counters = dict(self._counter_values)
        return {
            "accepted": counters.get("accepted", 0),
            "rejected": counters.get("rejected", 0),
            "duplicate": counters.get("duplicate", 0),
            "malformed": counters.get("malformed", 0),
            "storageFailure": counters.get("storage_failure", 0),
            "lastIngestAt": _iso(await self.last_ingest_at()),
            "lastAcceptedAt": _iso(self._last_accepted_at),
            "lastFailureAt": _iso(self._last_failure_at),
        }

    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
        async with self._lock:
            selected = [
                key
                for key, value in self.observations.items()
                if cutoff is None or value.observed_at.astimezone(UTC) < cutoff.astimezone(UTC)
            ]
            selected_ids = set(selected)
            run_count = len(
                {
                    item.correlation.run_id
                    for item in self.observations.values()
                    if item.observation_id in selected_ids
                    and item.correlation
                    and item.correlation.run_id
                }
            )
            for key in selected:
                self.observations.pop(key, None)
            deleted = len(selected)
            self.events = [item for item in self.events if item.observation_id not in selected_ids]
            return DeletionResult(deleted, deleted, run_count)

    # Reference composition implements the same explicit retention port as
    # production. Subclasses can override these methods to model leases and
    # crash recovery without use-case adapter introspection.
    async def find_retention_by_key(self, key: str) -> Any:
        action_id = self.idempotency.get(key)
        return self.actions.get(action_id) if action_id else None

    async def save_retention(self, action: Any) -> bool:
        if action.idempotency_key in self.idempotency:
            return False
        self.actions[action.action_id] = action
        self.idempotency[action.idempotency_key] = action.action_id
        return True

    async def enqueue_retention(self, action: Any) -> None:
        self.actions[action.action_id] = action

    async def attach_retention(self, action: Any) -> None:
        self.actions[action.action_id] = action

    async def get_retention(self, action_id: str) -> Any:
        return self.actions.get(action_id)

    async def pending_retentions(self) -> list[Any]:
        return [
            action
            for action in self.actions.values()
            if action.status.value in {"queued", "running"}
            or (action.status.value == "failed" and action.deletion_phase != "completed")
        ]

    async def claim_pending_retention(self, action_id: str) -> Any:
        action = self.actions.get(action_id)
        if action is None or not (
            action.status.value in {"queued", "running"}
            or (action.status.value == "failed" and action.deletion_phase != "completed")
        ):
            return None
        return action if await self.claim_retention(action_id) else None

    async def claim_retention(self, action_id: str, lease_seconds: int = 60) -> bool:
        del lease_seconds
        action = self.actions.get(action_id)
        if action is None or not (
            action.status.value in {"queued", "running"}
            or (action.status.value == "failed" and action.deletion_phase != "completed")
        ):
            return False
        return True

    async def prepare_retention(self, action: Any) -> None:
        selected = [
            item
            for item in self.observations.values()
            if action.cutoff is None
            or item.observed_at.astimezone(UTC) < action.cutoff.astimezone(UTC)
        ]
        action.target_observation_count = len(selected)
        action.target_receipt_count = 0
        action.target_run_count = len(
            {
                item.correlation.run_id
                for item in selected
                if item.correlation is not None and item.correlation.run_id
            }
        )
        action.deletion_phase = "targeted"

    async def update_retention(self, action: Any) -> None:
        self.actions[action.action_id] = action

    async def persisted_metric_series(self) -> list[dict[str, object]]:
        series: list[dict[str, object]] = []
        for action in self.actions.values():
            series.append(
                {
                    "name": "observatory_retention_action_count",
                    "value": 1,
                    "labels": {"action": action.kind.value, "status": action.status.value},
                }
            )
            if action.deleted_count:
                series.append(
                    {
                        "name": "observatory_retention_deleted_count",
                        "value": action.deleted_count,
                        "labels": {"action": action.kind.value},
                    }
                )
            if action.status.value == "failed":
                series.append(
                    {
                        "name": "observatory_retention_failure_count",
                        "value": 1,
                        "labels": {
                            "action": action.kind.value,
                            "failure_code": action.error or "unknown",
                        },
                    }
                )
        return series


__all__ = [
    "MemoryCatalog",
    "MemoryObservationIdRegistry",
    "MemoryObservationStore",
    "MemoryObservatoryStore",
]
