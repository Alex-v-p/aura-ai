"""Lifecycle maintenance collaborator for durable memory records."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid5

from aura_core.domains.knowledge.memory.contracts import (
    ARCHIVE_AFTER_DAYS,
    DORMANT_THRESHOLD,
    MEMORY_ID_NAMESPACE,
    MemoryFilters,
    MemoryLifecycleStatus,
)
from aura_core.domains.knowledge.memory.repository_ports import MemoryProcessingRepository


class MemoryMaintenanceService:
    """Apply deterministic expiry, decay, and archive transitions."""

    def __init__(
        self,
        repository: MemoryProcessingRepository,
        *,
        clock: Callable[[], datetime],
        transition_telemetry: Callable[..., None],
    ) -> None:
        self.repository = repository
        self._clock = clock
        self._transition_telemetry = transition_telemetry

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def maintain(self, issuer: str, subject: str, *, now: datetime | None = None) -> int:
        maintenance_started = monotonic()
        trace_id = uuid5(MEMORY_ID_NAMESPACE, f"maintenance:{issuer}:{subject}").hex
        stamp = now or self._now()
        changed = 0
        records = await self.repository.list_memories(
            issuer,
            subject,
            MemoryFilters(
                scope_type=None, include_all_scopes=True, include_historical=True, limit=100000
            ),
        )
        for record in records:
            if record.pinned or record.status in {
                MemoryLifecycleStatus.DISABLED,
                MemoryLifecycleStatus.DISPUTED,
                MemoryLifecycleStatus.SUPERSEDED,
                MemoryLifecycleStatus.ARCHIVED,
            }:
                continue
            destination: MemoryLifecycleStatus | None = None
            if (
                record.current_revision.valid_to is not None
                and record.current_revision.valid_to <= stamp
            ):
                if record.status is MemoryLifecycleStatus.ACTIVE:
                    destination = MemoryLifecycleStatus.DORMANT
            elif (
                record.status is MemoryLifecycleStatus.ACTIVE
                and record.relevance(stamp) < DORMANT_THRESHOLD
            ):
                destination = MemoryLifecycleStatus.DORMANT
            elif (
                record.status is MemoryLifecycleStatus.DORMANT
                and record.dormant_at
                and (stamp - record.dormant_at).total_seconds() >= ARCHIVE_AFTER_DAYS * 86400
            ):
                destination = MemoryLifecycleStatus.ARCHIVED
            if destination is None:
                continue
            transition_started = monotonic()
            await self.repository.set_status(
                issuer,
                subject,
                record.id,
                status=destination,
                expected_version=record.version,
                scope_type=record.scope.type,
                agent_profile_id=record.scope.agent_profile_id,
            )
            changed += 1
            self._transition_telemetry(
                transition_started,
                trace_id=trace_id,
                memory_id=record.id,
                destination_status=destination,
            )
        await self.repository.save_maintenance_state(issuer, subject, ran_at=stamp)
        self._transition_telemetry(maintenance_started, trace_id=trace_id, memory_id=None)
        return changed


__all__ = ["MemoryMaintenanceService"]
