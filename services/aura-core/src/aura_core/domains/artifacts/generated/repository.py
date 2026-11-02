# ruff: noqa: E501

"""Memory and SQL adapters for generated artifacts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.artifacts.generated.persistence import (
    GeneratedArtifactAppendReservationRow,
    GeneratedArtifactCleanupWorkRow,
    GeneratedArtifactCreateReservationRow,
    GeneratedArtifactIdempotencyRow,
    GeneratedArtifactLifecycleEventRow,
    GeneratedArtifactRevisionRow,
    GeneratedArtifactRow,
    GeneratedArtifactTombstoneRow,
)
from aura_core.domains.artifacts.generated.public import (
    ARTIFACT_ID_NAMESPACE,
    GeneratedArtifact,
    GeneratedArtifactCleanupWork,
    GeneratedArtifactConflict,
    GeneratedArtifactIntegrity,
    GeneratedArtifactLifecycle,
    GeneratedArtifactLifecycleEvent,
    GeneratedArtifactLineage,
    GeneratedArtifactNotFound,
)
from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifactRevision as GeneratedArtifactPublicRevision,
)
from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifactStoredRevision as GeneratedArtifactRevision,
)
from aura_core.platform.outbox.repository import SqlOutboxRepository
from aura_core.platform.outbox.service import OutboxCommand

CLEANUP_LEASE_SECONDS = 180


def _now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(value: str | None) -> str:
    return value or ""


def _public_revision(row: GeneratedArtifactRevision | None) -> GeneratedArtifactPublicRevision | None:
    if row is None:
        return None
    return GeneratedArtifactPublicRevision(
        row.artifact_id, row.revision, row.media_type, row.sha256,
        row.byte_count, row.creator_component, row.previous_revision,
        row.created_at,
    )


class MemoryGeneratedArtifactRepository:
    def __init__(self, *, clock: Any = None) -> None:
        self.artifacts: dict[UUID, GeneratedArtifact] = {}
        self.revision_rows: dict[tuple[UUID, int], GeneratedArtifactRevision] = {}
        self.idempotency: dict[tuple[str, str, str, str], tuple[str, UUID, int, str]] = {}
        self.reservations: dict[tuple[str, str, str], tuple[GeneratedArtifact, GeneratedArtifactRevision, str]] = {}
        self.create_reservations: dict[UUID, tuple[GeneratedArtifact, GeneratedArtifactRevision, str | None, str]] = {}
        self.append_reservations: dict[UUID, tuple[int, GeneratedArtifactRevision, tuple[str, str], str | None, str]] = {}
        self.admitted_ids: set[UUID] = set()
        self.tombstones: dict[UUID, GeneratedArtifact] = {}
        self.events: list[dict[str, object]] = []
        self.cleanup_work: dict[tuple[UUID, str], GeneratedArtifactCleanupWork] = {}
        self.clock = clock if callable(clock) else _now
        self._lock = asyncio.Lock()

    async def admit_create(
        self,
        artifact: object,
        revision: object,
        *,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact | None:
        value = cast(GeneratedArtifact, artifact)
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            retrying_admission = False
            if idempotency_key is not None:
                key = (*value.owner, "create", idempotency_key)
                prior = self.idempotency.get(key)
                if prior is not None:
                    if prior[0] != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    existing = self.artifacts.get(prior[1])
                    if existing is not None:
                        return existing
                    retrying_admission = True
            if value.id in self.admitted_ids and not retrying_admission:
                raise GeneratedArtifactConflict("artifact write is already admitted")
            if value.id in self.artifacts or value.id in self.tombstones:
                raise GeneratedArtifactConflict("artifact already exists")
            if idempotency_key is not None:
                self.reservations[(value.issuer, value.subject, idempotency_key)] = (
                    value, row, _fingerprint(idempotency_fingerprint)
                )
                self.idempotency[(*value.owner, "create", idempotency_key)] = (
                    _fingerprint(idempotency_fingerprint), value.id, 1, "admitted"
                )
            self.admitted_ids.add(value.id)
            self.create_reservations[value.id] = (
                value, row, idempotency_key, _fingerprint(idempotency_fingerprint)
            )
            return None

    async def publish_create(self, artifact: object, revision: object, *, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        value = cast(GeneratedArtifact, artifact)
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            existing = self.artifacts.get(value.id) or self.tombstones.get(value.id)
            if existing is not None:
                current = self.revision_rows.get((value.id, existing.current_revision or 1))
                if (
                    existing.owner != value.owner
                    or existing.kind != value.kind
                    or existing.title != value.title
                    or current is None
                    or (current.sha256, current.media_type, current.object_key,
                        current.creator_component, current.previous_revision)
                    != (row.sha256, row.media_type, row.object_key,
                        row.creator_component, row.previous_revision)
                ):
                    raise GeneratedArtifactConflict("create publication differs from admitted command")
                if idempotency_key is not None:
                    prior = self.idempotency.get((*value.owner, "create", idempotency_key))
                    if prior is None or prior[0] != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("create publication receipt mismatch")
                return existing
            reservation = self.create_reservations.get(value.id)
            if reservation is None or reservation[0].owner != value.owner or reservation[0].kind != value.kind or reservation[0].title != value.title or reservation[2] != idempotency_key or reservation[3] != _fingerprint(idempotency_fingerprint):
                raise GeneratedArtifactConflict("create publication is not the admitted command")
            admitted = reservation[1]
            if (admitted.sha256, admitted.media_type, admitted.object_key, admitted.creator_component, admitted.previous_revision) != (row.sha256, row.media_type, row.object_key, row.creator_component, row.previous_revision):
                raise GeneratedArtifactConflict("create publication differs from admitted command")
            self.artifacts[value.id] = value
            self.revision_rows[(value.id, row.revision)] = row
            for key, reservation in tuple(self.reservations.items()):
                if reservation[0].id == value.id:
                    self.idempotency[(*key[:2], "create", key[2])] = (
                        reservation[2], value.id, value.version, value.lifecycle.value
                    )
                    self.reservations.pop(key, None)
            self.admitted_ids.discard(value.id)
            self.create_reservations.pop(value.id, None)
            return value

    async def admit_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact | None:
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            value = self.artifacts.get(artifact_id)
            if value is None or value.owner != owner:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                prior = self.idempotency.get((*owner, "append", idempotency_key))
                if prior is not None:
                    if prior[0] != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    if prior[3] != "admitted":
                        return self.artifacts[prior[1]]
            if value.version != expected_version or not value.available or value.current_revision != row.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            reservation = self.append_reservations.get(artifact_id)
            if reservation is not None:
                prior_version, prior_row, prior_owner, prior_key, prior_fp = reservation
                if (prior_version, prior_row.revision, prior_row.sha256, prior_row.media_type, prior_row.object_key, prior_row.previous_revision) != (expected_version, row.revision, row.sha256, row.media_type, row.object_key, row.previous_revision) or prior_owner != owner or prior_key != idempotency_key or prior_fp != _fingerprint(idempotency_fingerprint):
                    raise GeneratedArtifactConflict("append source version is already reserved")
                return None
            self.append_reservations[artifact_id] = (expected_version, row, owner, idempotency_key, _fingerprint(idempotency_fingerprint))
            if idempotency_key is not None:
                self.reservations[(owner[0], owner[1], f"append:{idempotency_key}")] = (value, row, _fingerprint(idempotency_fingerprint))
                self.idempotency[(*owner, "append", idempotency_key)] = (_fingerprint(idempotency_fingerprint), artifact_id, expected_version, "admitted")
            return None

    async def publish_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact:
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            value = self.artifacts.get(artifact_id)
            if value is None or value.owner != owner:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                prior = self.idempotency.get((*owner, "append", idempotency_key))
                if prior is not None and prior[0] == _fingerprint(idempotency_fingerprint) and prior[3] != "admitted":
                    return value
            reservation = self.append_reservations.get(artifact_id)
            if reservation is None or reservation[0] != expected_version or reservation[2] != owner or reservation[3] != idempotency_key or reservation[4] != _fingerprint(idempotency_fingerprint):
                raise GeneratedArtifactConflict("append publication is not the admitted command")
            admitted = reservation[1]
            if (admitted.sha256, admitted.media_type, admitted.object_key, admitted.creator_component, admitted.previous_revision) != (row.sha256, row.media_type, row.object_key, row.creator_component, row.previous_revision):
                raise GeneratedArtifactConflict("append publication differs from admitted command")
            if value.version == expected_version + 1 and value.current_revision == row.revision:
                current = self.revision_rows.get((artifact_id, row.revision))
                if current is not None and (current.sha256, current.media_type, current.object_key, current.creator_component, current.previous_revision) == (row.sha256, row.media_type, row.object_key, row.creator_component, row.previous_revision):
                    return value
                raise GeneratedArtifactConflict("append publication digest mismatch")
            if value.version != expected_version or not value.available or value.current_revision != row.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            updated = replace(value, current_revision=row.revision, version=value.version + 1, updated_at=row.created_at, revision=_public_revision(row))
            self.artifacts[artifact_id] = updated
            self.revision_rows[(artifact_id, row.revision)] = row
            self.append_reservations.pop(artifact_id, None)
            if idempotency_key is not None:
                self.idempotency[(*owner, "append", idempotency_key)] = (_fingerprint(idempotency_fingerprint), artifact_id, updated.version, updated.lifecycle.value)
                self.reservations.pop((owner[0], owner[1], f"append:{idempotency_key}"), None)
            return updated

    async def create(self, artifact: object, revision: object, *, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        value = cast(GeneratedArtifact, artifact)
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            if idempotency_key is not None:
                key = (*value.owner, "create", idempotency_key)
                prior = self.idempotency.get(key)
                if prior is not None:
                    if prior[0] != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    return self.artifacts[prior[1]]
            if value.id in self.artifacts or value.id in self.tombstones:
                raise GeneratedArtifactConflict("artifact already exists")
            self.artifacts[value.id] = value
            self.revision_rows[(value.id, row.revision)] = row
            if idempotency_key is not None:
                self.idempotency[(*value.owner, "create", idempotency_key)] = (_fingerprint(idempotency_fingerprint), value.id, value.version, value.lifecycle.value)
            return value

    async def replay_append(self, owner: tuple[str, str], artifact_id: UUID, *, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact | None:
        async with self._lock:
            receipt = self.idempotency.get((*owner, "append", idempotency_key))
            if receipt is None:
                return None
            if receipt[1] != artifact_id:
                raise GeneratedArtifactConflict("append receipt belongs to another artifact")
            if receipt[0] != idempotency_fingerprint:
                raise GeneratedArtifactConflict("append idempotency key was reused")
            if receipt[3] == "admitted":
                return None
            return self.artifacts.get(artifact_id)

    async def append_revision(self, artifact_id: UUID, revision: object, *, owner: tuple[str, str], expected_version: int, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        row = cast(GeneratedArtifactRevision, revision)
        async with self._lock:
            value = self.artifacts.get(artifact_id)
            if value is None or value.owner != owner:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                key = (*owner, "append", idempotency_key)
                prior = self.idempotency.get(key)
                if prior is not None:
                    if prior[0] != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    return self.artifacts[prior[1]]
            if value.version != expected_version or not value.available or value.current_revision != row.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            updated = replace(value, current_revision=row.revision, version=value.version + 1, updated_at=row.created_at, revision=_public_revision(row))
            self.artifacts[artifact_id] = updated
            self.revision_rows[(artifact_id, row.revision)] = row
            if idempotency_key is not None:
                self.idempotency[(*owner, "append", idempotency_key)] = (_fingerprint(idempotency_fingerprint), artifact_id, updated.version, updated.lifecycle.value)
            return updated

    async def get(self, owner: tuple[str, str], artifact_id: UUID) -> GeneratedArtifact | None:
        value = self.artifacts.get(artifact_id)
        if value is not None and value.owner == owner:
            return value
        value = self.tombstones.get(artifact_id)
        return value if value is not None and value.owner == owner else None

    async def list(self, owner: tuple[str, str], *, limit: int = 100, offset: int = 0, media_type: str | None = None) -> list[GeneratedArtifact]:
        values = [value for value in (*self.artifacts.values(), *self.tombstones.values()) if value.owner == owner]
        if media_type is not None:
            values = [value for value in values if value.revision is not None and value.revision.media_type == media_type]
        return sorted(values, key=lambda value: (value.created_at, value.id), reverse=True)[offset : offset + limit]

    async def revisions(self, owner: tuple[str, str], artifact_id: UUID) -> list[GeneratedArtifactRevision]:
        value = await self.get(owner, artifact_id)
        if value is None:
            raise GeneratedArtifactNotFound("artifact not found")
        return sorted((row for (aid, _), row in self.revision_rows.items() if aid == artifact_id), key=lambda row: row.revision)

    async def get_revision(self, owner: tuple[str, str], artifact_id: UUID, revision: int | None = None) -> GeneratedArtifactRevision | None:
        value = await self.get(owner, artifact_id)
        if value is None or not value.available:
            return None
        requested = revision or value.current_revision
        return self.revision_rows.get((artifact_id, requested)) if requested is not None else None

    async def begin_delete(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, idempotency_key: str, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        async with self._lock:
            value = await self.get(owner, artifact_id)
            if value is None:
                raise GeneratedArtifactNotFound("artifact not found")
            prior = self.idempotency.get((*owner, "delete", idempotency_key))
            if prior is not None:
                if idempotency_fingerprint is not None and prior[0] != idempotency_fingerprint:
                    raise GeneratedArtifactConflict("delete idempotency key was reused")
                existing = self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)
                if existing is None:
                    raise GeneratedArtifactNotFound("artifact not found")
                return existing
            if value.lifecycle is not GeneratedArtifactLifecycle.ACTIVE:
                raise GeneratedArtifactConflict("artifact is already unavailable")
            if value.version != expected_version or value.revision is None:
                raise GeneratedArtifactConflict("artifact version conflict")
            tombstone = replace(value, title=None, lifecycle=GeneratedArtifactLifecycle.DELETION_PENDING, version=value.version + 1, updated_at=self.clock(), deletion_audit_id=UUID(int=(value.id.int ^ value.version ^ 1)), failure_code=None)
            self.artifacts[artifact_id] = tombstone
            self.idempotency[(*owner, "delete", idempotency_key)] = (idempotency_fingerprint or "delete", artifact_id, tombstone.version, tombstone.lifecycle.value)
            self.cleanup_work[(artifact_id, "delete")] = GeneratedArtifactCleanupWork(artifact_id, "delete", tombstone.version, "pending", idempotency_fingerprint or "delete", idempotency_key)
            return tombstone

    async def set_cleanup_state(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, lifecycle: str, failure_code: str | None = None, idempotency_key: str | None = None, operation: str = "delete", idempotency_fingerprint: str | None = None, claim_token: str | None = None) -> GeneratedArtifact:
        async with self._lock:
            if claim_token is None:
                raise GeneratedArtifactConflict("cleanup claim token is required")
            work = self.cleanup_work.get((artifact_id, operation))
            if work is None or work.status != "claimed" or work.claim_token != claim_token:
                raise GeneratedArtifactConflict("stale cleanup claim")
            value = self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)
            if value is None or value.owner != owner:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                prior = self.idempotency.get((*owner, operation, idempotency_key))
                if prior is not None:
                    if idempotency_fingerprint is not None and prior[0] != idempotency_fingerprint:
                        raise GeneratedArtifactConflict("cleanup idempotency key was reused")
                    if prior[3] not in {"admitted", GeneratedArtifactLifecycle.DELETION_PENDING.value}:
                        return value
            if value.version != expected_version:
                if value.lifecycle.value == lifecycle:
                    return value
                raise GeneratedArtifactConflict("artifact version conflict")
            next_lifecycle = GeneratedArtifactLifecycle(lifecycle)
            revision = value.revision
            integrity = value.integrity
            if revision is not None:
                integrity = GeneratedArtifactIntegrity(revision.revision, revision.sha256, revision.byte_count, revision.created_at)
            updated = replace(value, lifecycle=next_lifecycle, version=value.version + 1, updated_at=self.clock(), failure_code=failure_code, integrity=integrity, title=None)
            self.artifacts.pop(artifact_id, None)
            self.tombstones[artifact_id] = updated
            if idempotency_key is not None:
                self.idempotency[(*owner, operation, idempotency_key)] = (
                    idempotency_fingerprint or f"{artifact_id}:{expected_version}:{lifecycle}",
                    artifact_id,
                    updated.version,
                    updated.lifecycle.value,
                )
                self.cleanup_work[(artifact_id, operation)] = GeneratedArtifactCleanupWork(
                    artifact_id, operation, expected_version, "complete" if lifecycle == GeneratedArtifactLifecycle.DELETED.value else ("unknown" if lifecycle == GeneratedArtifactLifecycle.CLEANUP_UNKNOWN.value else "failed"),
                    idempotency_fingerprint or f"{artifact_id}:{expected_version}:{lifecycle}", idempotency_key,
                )
            return updated

    async def admit_cleanup(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, operation: str, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact:
        async with self._lock:
            value = self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)
            if value is None or value.owner != owner:
                raise GeneratedArtifactNotFound("artifact not found")
            prior = self.idempotency.get((*owner, operation, idempotency_key))
            if prior is not None:
                if prior[0] != idempotency_fingerprint or prior[1] != artifact_id:
                    raise GeneratedArtifactConflict("cleanup idempotency key was reused")
                return value
            required = GeneratedArtifactLifecycle.DELETION_FAILED if operation == "retry_cleanup" else GeneratedArtifactLifecycle.CLEANUP_UNKNOWN
            if value.lifecycle is not required or value.version != expected_version:
                raise GeneratedArtifactConflict("cleanup admission precondition failed")
            self.idempotency[(*owner, operation, idempotency_key)] = (idempotency_fingerprint, artifact_id, expected_version, "admitted")
            self.cleanup_work[(artifact_id, operation)] = GeneratedArtifactCleanupWork(artifact_id, operation, expected_version, "pending", idempotency_fingerprint, idempotency_key)
            return value

    async def claim_pending_cleanup(self, owner: tuple[str, str], *, limit: int = 20) -> list[GeneratedArtifactCleanupWork]:
        async with self._lock:
            now = cast(datetime, self.clock())
            selected: list[GeneratedArtifactCleanupWork] = []
            for work in self.cleanup_work.values():
                value = self.artifacts.get(work.artifact_id) or self.tombstones.get(work.artifact_id)
                if work.status == "pending" or (work.status == "claimed" and work.lease_expires_at is not None and work.lease_expires_at <= now):
                    if value is not None and value.owner == owner:
                        selected.append(work)
                if len(selected) >= limit:
                    break
            claimed = [replace(work, status="claimed", claim_token=uuid4().hex, lease_expires_at=now + timedelta(seconds=CLEANUP_LEASE_SECONDS)) for work in selected]
            for work in claimed:
                self.cleanup_work[(work.artifact_id, work.operation)] = work
            return claimed

    async def replay_cleanup(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact | None:
        async with self._lock:
            receipt = self.idempotency.get((*owner, operation, idempotency_key))
            if receipt is None:
                return None
            if receipt[0] != idempotency_fingerprint:
                raise GeneratedArtifactConflict("cleanup idempotency key was reused")
            if receipt[1] != artifact_id:
                raise GeneratedArtifactConflict("cleanup receipt belongs to another artifact")
            return self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)

    async def assert_cleanup_claim(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, claim_token: str) -> None:
        async with self._lock:
            value = self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)
            work = self.cleanup_work.get((artifact_id, operation))
            now = cast(datetime, self.clock())
            if value is None or value.owner != owner or work is None or work.status != "claimed" or work.claim_token != claim_token or (work.lease_expires_at is not None and work.lease_expires_at <= now):
                raise GeneratedArtifactConflict("stale cleanup claim")

    async def renew_cleanup_claim(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, claim_token: str) -> None:
        async with self._lock:
            value = self.artifacts.get(artifact_id) or self.tombstones.get(artifact_id)
            work = self.cleanup_work.get((artifact_id, operation))
            now = cast(datetime, self.clock())
            if value is None or value.owner != owner or work is None or work.status != "claimed" or work.claim_token != claim_token or (work.lease_expires_at is not None and work.lease_expires_at <= now):
                raise GeneratedArtifactConflict("stale cleanup claim")
            self.cleanup_work[(artifact_id, operation)] = replace(
                work,
                lease_expires_at=now + timedelta(seconds=CLEANUP_LEASE_SECONDS),
            )


class SqlGeneratedArtifactRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.outbox = SqlOutboxRepository()

    async def _stage_lifecycle_event(self, session: AsyncSession, value: GeneratedArtifact, event_type: str) -> None:
        event = GeneratedArtifactLifecycleEvent(
            value.id, value.version, value.lifecycle, value.current_revision,
            value.updated_at, event_type,
            uuid5(ARTIFACT_ID_NAMESPACE, f"correlation:{value.id}"),
            uuid5(ARTIFACT_ID_NAMESPACE, f"causation:{value.id}:{value.version}"),
        )
        await self.outbox.stage_command(
            session,
            command=OutboxCommand(
                id=event.event_id,
                topic="aura.artifacts.generated.events.v1",
                run_id=None,
                conversation_id=None,
                created_at=event.occurred_at,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                schema_version=1,
                aggregate_type="artifacts.generated",
                aggregate_id=value.id,
                event_payload=event.payload(),
            ),
        )

    @staticmethod
    def _revision(row: GeneratedArtifactRevisionRow) -> GeneratedArtifactRevision:
        return GeneratedArtifactRevision(row.artifact_id, row.revision, row.media_type, row.sha256, row.byte_count, row.object_key, row.creator_component, row.previous_revision, row.created_at)

    async def _revisions(self, session: AsyncSession, artifact_id: UUID) -> list[GeneratedArtifactRevision]:
        rows = (await session.scalars(select(GeneratedArtifactRevisionRow).where(GeneratedArtifactRevisionRow.artifact_id == artifact_id).order_by(GeneratedArtifactRevisionRow.revision))).all()
        return [self._revision(row) for row in rows]

    @classmethod
    def _artifact(cls, row: GeneratedArtifactRow, revision: GeneratedArtifactRevision | None = None) -> GeneratedArtifact:
        integrity = None
        if row.integrity_revision is not None and row.integrity_sha256 is not None and row.integrity_byte_count is not None and row.integrity_created_at is not None:
            integrity = GeneratedArtifactIntegrity(row.integrity_revision, row.integrity_sha256, row.integrity_byte_count, row.integrity_created_at)
        lineage = GeneratedArtifactLineage(row.task_id, row.run_id, row.conversation_id, row.workspace_id)
        return GeneratedArtifact(row.id, row.principal_issuer, row.principal_subject, row.kind, row.title, GeneratedArtifactLifecycle(row.lifecycle), row.current_revision, row.version, row.created_at, row.updated_at, lineage, _public_revision(revision), integrity, row.deletion_audit_id, row.failure_code)

    @staticmethod
    def _event_row(value: GeneratedArtifact, event_type: str) -> GeneratedArtifactLifecycleEventRow:
        event = GeneratedArtifactLifecycleEvent(
            value.id,
            value.version,
            value.lifecycle,
            value.current_revision,
            value.updated_at,
            event_type,
            uuid5(ARTIFACT_ID_NAMESPACE, f"correlation:{value.id}"),
            uuid5(ARTIFACT_ID_NAMESPACE, f"causation:{value.id}:{value.version}"),
        )
        return GeneratedArtifactLifecycleEventRow(
            event_id=event.event_id,
            artifact_id=value.id,
            artifact_version=value.version,
            event_type=event_type,
            lifecycle=value.lifecycle.value,
            current_revision=value.current_revision,
            occurred_at=value.updated_at,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
        )

    async def admit_create(
        self,
        artifact: object,
        revision: object,
        *,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact | None:
        value = cast(GeneratedArtifact, artifact)
        async with self.sessions() as session, session.begin():
            if idempotency_key is None:
                existing = await session.get(GeneratedArtifactRow, value.id)
                if existing is not None:
                    raise GeneratedArtifactConflict("artifact already exists")
            receipt = await session.get(
                GeneratedArtifactIdempotencyRow,
                (*value.owner, "create", idempotency_key),
            )
            if receipt is not None:
                if receipt.fingerprint != _fingerprint(idempotency_fingerprint):
                    raise GeneratedArtifactConflict("idempotency key was reused")
                existing = await session.get(GeneratedArtifactRow, receipt.artifact_id)
                if existing is None:
                    return None
                current = await session.get(GeneratedArtifactRevisionRow, (existing.id, existing.current_revision)) if existing.current_revision else None
                return self._artifact(existing, self._revision(current) if current is not None else None)
            existing = await session.get(GeneratedArtifactRow, value.id)
            if existing is not None:
                raise GeneratedArtifactConflict("artifact already exists")
            reservation = await session.get(GeneratedArtifactCreateReservationRow, value.id, with_for_update=True)
            rev = cast(GeneratedArtifactRevision, revision)
            if reservation is not None:
                if not (
                    reservation.principal_issuer == value.issuer
                    and reservation.principal_subject == value.subject
                    and reservation.idempotency_key == idempotency_key
                    and reservation.fingerprint == _fingerprint(idempotency_fingerprint)
                    and reservation.media_type == rev.media_type
                    and reservation.sha256 == rev.sha256
                    and reservation.object_key == rev.object_key
                ):
                    raise GeneratedArtifactConflict("artifact identity is already reserved")
                return None
            session.add(GeneratedArtifactCreateReservationRow(
                artifact_id=value.id, principal_issuer=value.issuer,
                principal_subject=value.subject, idempotency_key=idempotency_key,
                fingerprint=_fingerprint(idempotency_fingerprint), media_type=rev.media_type,
                sha256=rev.sha256, object_key=rev.object_key,
            ))
            if idempotency_key is not None:
                session.add(
                    GeneratedArtifactIdempotencyRow(
                        principal_issuer=value.issuer,
                        principal_subject=value.subject,
                        operation="create",
                        idempotency_key=idempotency_key,
                        fingerprint=_fingerprint(idempotency_fingerprint),
                        artifact_id=value.id,
                        result_version=1,
                        result_lifecycle="admitted",
                    )
                )
            return None

    async def publish_create(self, artifact: object, revision: object, *, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        value = cast(GeneratedArtifact, artifact)
        rev = cast(GeneratedArtifactRevision, revision)
        async with self.sessions() as session, session.begin():
            existing = await session.get(GeneratedArtifactRow, value.id, with_for_update=True)
            if existing is not None:
                current = await session.get(GeneratedArtifactRevisionRow, (value.id, existing.current_revision)) if existing.current_revision else None
                if (
                    existing.principal_issuer != value.issuer
                    or existing.principal_subject != value.subject
                    or existing.kind != value.kind
                    or existing.title != value.title
                    or current is None
                    or (current.sha256, current.media_type, current.object_key,
                        current.creator_component, current.previous_revision)
                    != (rev.sha256, rev.media_type, rev.object_key,
                        rev.creator_component, rev.previous_revision)
                ):
                    raise GeneratedArtifactConflict("create publication differs from admitted command")
                if idempotency_key is not None:
                    receipt = await session.get(GeneratedArtifactIdempotencyRow, (*value.owner, "create", idempotency_key))
                    if receipt is None or receipt.fingerprint != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("create publication receipt mismatch")
                return self._artifact(existing, self._revision(current) if current else None)
            reservation = await session.get(GeneratedArtifactCreateReservationRow, value.id, with_for_update=True)
            if reservation is None or not (
                reservation.principal_issuer == value.issuer
                and reservation.principal_subject == value.subject
                and reservation.media_type == rev.media_type
                and reservation.sha256 == rev.sha256
                and reservation.object_key == rev.object_key
                and reservation.idempotency_key == idempotency_key
                and reservation.fingerprint == _fingerprint(idempotency_fingerprint)
            ):
                raise GeneratedArtifactConflict("create publication is not the admitted command")
            session.add(
                GeneratedArtifactRow(
                    id=value.id,
                    principal_issuer=value.issuer,
                    principal_subject=value.subject,
                    kind=value.kind,
                    title=value.title,
                    lifecycle=value.lifecycle.value,
                    current_revision=value.current_revision,
                    version=value.version,
                    workspace_id=value.lineage.workspace_id,
                    task_id=value.lineage.task_id,
                    run_id=value.lineage.run_id,
                    conversation_id=value.lineage.conversation_id,
                    created_at=value.created_at,
                    updated_at=value.updated_at,
                )
            )
            session.add(
                GeneratedArtifactRevisionRow(
                    artifact_id=rev.artifact_id,
                    revision=rev.revision,
                    media_type=rev.media_type,
                    sha256=rev.sha256,
                    byte_count=rev.byte_count,
                    object_key=rev.object_key,
                    creator_component=rev.creator_component,
                    previous_revision=rev.previous_revision,
                    created_at=rev.created_at,
                )
            )
            session.add(self._event_row(value, "artifact.created"))
            await self._stage_lifecycle_event(session, value, "artifact.created")
            admitted_receipts = (await session.scalars(select(GeneratedArtifactIdempotencyRow).where(
                GeneratedArtifactIdempotencyRow.artifact_id == value.id,
                GeneratedArtifactIdempotencyRow.operation == "create",
                GeneratedArtifactIdempotencyRow.result_lifecycle == "admitted",
            ))).all()
            for receipt in admitted_receipts:
                receipt.result_version = value.version
                receipt.result_lifecycle = value.lifecycle.value
            await session.delete(reservation)
            return value

    async def admit_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact | None:
        async with self.sessions() as session, session.begin():
            if idempotency_key is not None:
                receipt = await session.get(
                    GeneratedArtifactIdempotencyRow,
                    (*owner, "append", idempotency_key),
                )
                if receipt is not None:
                    if receipt.fingerprint != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    if receipt.result_lifecycle != "admitted":
                        row = await session.get(GeneratedArtifactRow, artifact_id)
                        return self._artifact(row) if row is not None else None
            row = await session.scalar(
                select(GeneratedArtifactRow)
                .where(
                    GeneratedArtifactRow.id == artifact_id,
                    GeneratedArtifactRow.principal_issuer == owner[0],
                    GeneratedArtifactRow.principal_subject == owner[1],
                )
                .with_for_update()
            )
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            rev = cast(GeneratedArtifactRevision, revision)
            if row.version != expected_version or row.lifecycle != GeneratedArtifactLifecycle.ACTIVE.value or row.current_revision != rev.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            reservation = await session.get(GeneratedArtifactAppendReservationRow, artifact_id, with_for_update=True)
            if reservation is not None:
                if not (
                    reservation.source_version == expected_version
                    and reservation.principal_issuer == owner[0]
                    and reservation.principal_subject == owner[1]
                    and reservation.idempotency_key == idempotency_key
                    and reservation.fingerprint == _fingerprint(idempotency_fingerprint)
                    and reservation.revision == rev.revision
                    and reservation.media_type == rev.media_type
                    and reservation.sha256 == rev.sha256
                    and reservation.object_key == rev.object_key
                    and reservation.creator_component == rev.creator_component
                    and reservation.previous_revision == rev.previous_revision
                ):
                    raise GeneratedArtifactConflict("append source version is already reserved")
                return None
            session.add(GeneratedArtifactAppendReservationRow(
                artifact_id=artifact_id, source_version=expected_version,
                principal_issuer=owner[0], principal_subject=owner[1],
                idempotency_key=idempotency_key,
                fingerprint=_fingerprint(idempotency_fingerprint),
                revision=rev.revision, media_type=rev.media_type,
                sha256=rev.sha256, object_key=rev.object_key,
                creator_component=rev.creator_component,
                previous_revision=rev.previous_revision,
            ))
            if idempotency_key is not None:
                session.add(
                    GeneratedArtifactIdempotencyRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation="append",
                        idempotency_key=idempotency_key,
                        fingerprint=_fingerprint(idempotency_fingerprint),
                        artifact_id=artifact_id,
                        result_version=row.version,
                        result_lifecycle="admitted",
                    )
                )
            return None

    async def publish_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GeneratedArtifact:
        rev = cast(GeneratedArtifactRevision, revision)
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(GeneratedArtifactRow)
                .where(
                    GeneratedArtifactRow.id == artifact_id,
                    GeneratedArtifactRow.principal_issuer == owner[0],
                    GeneratedArtifactRow.principal_subject == owner[1],
                )
                .with_for_update()
            )
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, "append", idempotency_key))
                if receipt is not None and receipt.fingerprint == _fingerprint(idempotency_fingerprint) and receipt.result_lifecycle != "admitted":
                    current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision)) if row.current_revision else None
                    return self._artifact(row, self._revision(current) if current else None)
            reservation = await session.get(GeneratedArtifactAppendReservationRow, artifact_id, with_for_update=True)
            if reservation is None or reservation.source_version != expected_version or reservation.principal_issuer != owner[0] or reservation.principal_subject != owner[1] or reservation.idempotency_key != idempotency_key or reservation.fingerprint != _fingerprint(idempotency_fingerprint) or reservation.revision != rev.revision or reservation.media_type != rev.media_type or reservation.sha256 != rev.sha256 or reservation.object_key != rev.object_key or reservation.creator_component != rev.creator_component or reservation.previous_revision != rev.previous_revision:
                raise GeneratedArtifactConflict("append publication is not the admitted command")
            if row.version == expected_version + 1 and row.current_revision == rev.revision:
                current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision))
                if current is not None and (current.sha256, current.media_type, current.object_key, current.creator_component, current.previous_revision) == (rev.sha256, rev.media_type, rev.object_key, rev.creator_component, rev.previous_revision):
                    return self._artifact(row, self._revision(current))
                raise GeneratedArtifactConflict("append publication digest mismatch")
            if row.version != expected_version or row.lifecycle != GeneratedArtifactLifecycle.ACTIVE.value or row.current_revision != rev.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            row.current_revision = rev.revision
            row.version += 1
            row.updated_at = rev.created_at
            session.add(
                GeneratedArtifactRevisionRow(
                    artifact_id=rev.artifact_id,
                    revision=rev.revision,
                    media_type=rev.media_type,
                    sha256=rev.sha256,
                    byte_count=rev.byte_count,
                    object_key=rev.object_key,
                    creator_component=rev.creator_component,
                    previous_revision=rev.previous_revision,
                    created_at=rev.created_at,
                )
            )
            session.add(self._event_row(self._artifact(row, rev), "artifact.revision_published"))
            await self._stage_lifecycle_event(session, self._artifact(row, rev), "artifact.revision_published")
            await session.delete(reservation)
            admitted_receipts = (await session.scalars(select(GeneratedArtifactIdempotencyRow).where(
                GeneratedArtifactIdempotencyRow.artifact_id == artifact_id,
                GeneratedArtifactIdempotencyRow.operation == "append",
                GeneratedArtifactIdempotencyRow.idempotency_key == idempotency_key,
                GeneratedArtifactIdempotencyRow.result_lifecycle == "admitted",
            ))).all()
            for receipt in admitted_receipts:
                receipt.result_version = row.version
                receipt.result_lifecycle = row.lifecycle
            return self._artifact(row, rev)

    async def replay_append(self, owner: tuple[str, str], artifact_id: UUID, *, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact | None:
        async with self.sessions() as session:
            receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, "append", idempotency_key))
            if receipt is None:
                return None
            if receipt.artifact_id != artifact_id:
                raise GeneratedArtifactConflict("append receipt belongs to another artifact")
            if receipt.fingerprint != idempotency_fingerprint:
                raise GeneratedArtifactConflict("append idempotency key was reused")
            if receipt.result_lifecycle == "admitted":
                return None
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]))
            if row is None:
                return None
            current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision)) if row.current_revision else None
            return self._artifact(row, self._revision(current) if current else None)
    async def get(self, owner: tuple[str, str], artifact_id: UUID) -> GeneratedArtifact | None:
        async with self.sessions() as session:
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]))
            if row is None:
                return None
            revision = None
            if row.current_revision is not None:
                rev = await session.get(GeneratedArtifactRevisionRow, (row.id, row.current_revision))
                revision = self._revision(rev) if rev is not None else None
            return self._artifact(row, revision)

    async def list(self, owner: tuple[str, str], *, limit: int = 100, offset: int = 0, media_type: str | None = None) -> list[GeneratedArtifact]:
        async with self.sessions() as session:
            query = select(GeneratedArtifactRow).where(GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]).order_by(GeneratedArtifactRow.created_at.desc(), GeneratedArtifactRow.id.desc())
            rows = list((await session.scalars(query)).all())
            values: list[GeneratedArtifact] = []
            for row in rows:
                revision = None
                if row.current_revision is not None:
                    current = await session.get(GeneratedArtifactRevisionRow, (row.id, row.current_revision))
                    revision = self._revision(current) if current is not None else None
                value = self._artifact(row, revision)
                if media_type is None or (revision is not None and revision.media_type == media_type):
                    values.append(value)
            return values[offset : offset + limit]

    async def create(self, artifact: object, revision: object, *, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        value = cast(GeneratedArtifact, artifact)
        rev = cast(GeneratedArtifactRevision, revision)
        async with self.sessions() as session, session.begin():
            if idempotency_key is not None:
                receipt = await session.get(GeneratedArtifactIdempotencyRow, (*value.owner, "create", idempotency_key))
                if receipt is not None:
                    if receipt.fingerprint != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    existing = await session.get(GeneratedArtifactRow, receipt.artifact_id)
                    if existing is not None:
                        return self._artifact(existing, rev)
            session.add(GeneratedArtifactRow(id=value.id, principal_issuer=value.issuer, principal_subject=value.subject, kind=value.kind, title=value.title, lifecycle=value.lifecycle.value, current_revision=value.current_revision, version=value.version, workspace_id=value.lineage.workspace_id, task_id=value.lineage.task_id, run_id=value.lineage.run_id, conversation_id=value.lineage.conversation_id, created_at=value.created_at, updated_at=value.updated_at))
            session.add(GeneratedArtifactRevisionRow(artifact_id=rev.artifact_id, revision=rev.revision, media_type=rev.media_type, sha256=rev.sha256, byte_count=rev.byte_count, object_key=rev.object_key, creator_component=rev.creator_component, previous_revision=rev.previous_revision, created_at=rev.created_at))
            session.add(self._event_row(value, "artifact.created"))
            await self._stage_lifecycle_event(session, value, "artifact.created")
            if idempotency_key is not None:
                session.add(GeneratedArtifactIdempotencyRow(principal_issuer=value.issuer, principal_subject=value.subject, operation="create", idempotency_key=idempotency_key, fingerprint=_fingerprint(idempotency_fingerprint), artifact_id=value.id, result_version=value.version, result_lifecycle=value.lifecycle.value))
            return value

    async def append_revision(self, artifact_id: UUID, revision: object, *, owner: tuple[str, str], expected_version: int, idempotency_key: str | None = None, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        rev = cast(GeneratedArtifactRevision, revision)
        async with self.sessions() as session, session.begin():
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]).with_for_update())
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            if idempotency_key is not None:
                receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, "append", idempotency_key))
                if receipt is not None:
                    if receipt.fingerprint != _fingerprint(idempotency_fingerprint):
                        raise GeneratedArtifactConflict("idempotency key was reused")
                    current = await session.get(GeneratedArtifactRow, artifact_id)
                    return self._artifact(cast(GeneratedArtifactRow, current))
            if row.version != expected_version or row.lifecycle != GeneratedArtifactLifecycle.ACTIVE.value or row.current_revision != rev.previous_revision:
                raise GeneratedArtifactConflict("artifact version conflict")
            row.current_revision = rev.revision
            row.version += 1
            row.updated_at = rev.created_at
            session.add(GeneratedArtifactRevisionRow(artifact_id=rev.artifact_id, revision=rev.revision, media_type=rev.media_type, sha256=rev.sha256, byte_count=rev.byte_count, object_key=rev.object_key, creator_component=rev.creator_component, previous_revision=rev.previous_revision, created_at=rev.created_at))
            session.add(self._event_row(self._artifact(row, rev), "artifact.revision_published"))
            await self._stage_lifecycle_event(session, self._artifact(row, rev), "artifact.revision_published")
            if idempotency_key is not None:
                session.add(GeneratedArtifactIdempotencyRow(principal_issuer=owner[0], principal_subject=owner[1], operation="append", idempotency_key=idempotency_key, fingerprint=_fingerprint(idempotency_fingerprint), artifact_id=artifact_id, result_version=row.version, result_lifecycle=row.lifecycle))
            return self._artifact(row, rev)

    async def revisions(self, owner: tuple[str, str], artifact_id: UUID) -> list[GeneratedArtifactRevision]:
        async with self.sessions() as session:
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]))
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            return await self._revisions(session, artifact_id)

    async def get_revision(self, owner: tuple[str, str], artifact_id: UUID, revision: int | None = None) -> GeneratedArtifactRevision | None:
        value = await self.get(owner, artifact_id)
        if value is None or not value.available:
            return None
        async with self.sessions() as session:
            requested = revision or value.current_revision
            if requested is None:
                return None
            row = await session.get(GeneratedArtifactRevisionRow, (artifact_id, requested))
            return self._revision(row) if row is not None else None

    async def begin_delete(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, idempotency_key: str, idempotency_fingerprint: str | None = None) -> GeneratedArtifact:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]).with_for_update())
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, "delete", idempotency_key))
            if receipt is not None:
                if idempotency_fingerprint is not None and receipt.fingerprint != idempotency_fingerprint:
                    raise GeneratedArtifactConflict("delete idempotency key was reused")
                return self._artifact(row)
            if row.lifecycle != GeneratedArtifactLifecycle.ACTIVE.value or row.version != expected_version:
                raise GeneratedArtifactConflict("artifact version conflict")
            current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision)) if row.current_revision is not None else None
            if current is not None:
                row.integrity_revision = current.revision
                row.integrity_sha256 = current.sha256
                row.integrity_byte_count = current.byte_count
                row.integrity_created_at = current.created_at
            row.title = None
            row.lifecycle = GeneratedArtifactLifecycle.DELETION_PENDING.value
            row.version += 1
            row.deletion_audit_id = UUID(int=(artifact_id.int ^ expected_version ^ 1))
            row.updated_at = _now()
            session.add(self._event_row(self._artifact(row), "artifact.deletion_changed"))
            await self._stage_lifecycle_event(session, self._artifact(row), "artifact.deletion_changed")
            await session.merge(GeneratedArtifactCleanupWorkRow(
                artifact_id=artifact_id,
                operation="delete",
                expected_version=row.version,
                status="pending",
                fingerprint=idempotency_fingerprint or "delete",
                idempotency_key=idempotency_key,
                updated_at=row.updated_at,
            ))
            session.add(GeneratedArtifactIdempotencyRow(principal_issuer=owner[0], principal_subject=owner[1], operation="delete", idempotency_key=idempotency_key, fingerprint=idempotency_fingerprint or "delete", artifact_id=artifact_id, result_version=row.version, result_lifecycle=row.lifecycle))
            return self._artifact(row)

    async def set_cleanup_state(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, lifecycle: str, failure_code: str | None = None, idempotency_key: str | None = None, operation: str = "delete", idempotency_fingerprint: str | None = None, claim_token: str | None = None) -> GeneratedArtifact:
        async with self.sessions() as session, session.begin():
            if claim_token is None:
                raise GeneratedArtifactConflict("cleanup claim token is required")
            row = await session.scalar(select(GeneratedArtifactRow).where(GeneratedArtifactRow.id == artifact_id, GeneratedArtifactRow.principal_issuer == owner[0], GeneratedArtifactRow.principal_subject == owner[1]).with_for_update())
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            work = await session.get(GeneratedArtifactCleanupWorkRow, (artifact_id, operation), with_for_update=True)
            if work is None or work.status != "claimed" or work.claim_token != claim_token:
                raise GeneratedArtifactConflict("stale cleanup claim")
            if idempotency_key is not None:
                receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, operation, idempotency_key))
                if receipt is not None and receipt.result_lifecycle not in {"admitted", GeneratedArtifactLifecycle.DELETION_PENDING.value}:
                    fingerprint = idempotency_fingerprint or receipt.fingerprint
                    if receipt.fingerprint != fingerprint:
                        raise GeneratedArtifactConflict("cleanup idempotency key was reused")
                    return self._artifact(row)
            if row.version != expected_version:
                if row.lifecycle == lifecycle:
                    return self._artifact(row)
                raise GeneratedArtifactConflict("artifact version conflict")
            current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision)) if row.current_revision is not None else None
            if current is not None:
                row.integrity_revision = current.revision
                row.integrity_sha256 = current.sha256
                row.integrity_byte_count = current.byte_count
                row.integrity_created_at = current.created_at
            row.lifecycle = lifecycle
            row.failure_code = failure_code
            row.version += 1
            row.updated_at = _now()
            if (
                row.integrity_revision is not None
                and row.integrity_sha256 is not None
                and row.integrity_byte_count is not None
                and row.integrity_created_at is not None
            ):
                await session.merge(
                    GeneratedArtifactTombstoneRow(
                        artifact_id=artifact_id,
                        final_version=row.version,
                        deleted_at=row.updated_at,
                        integrity_revision=row.integrity_revision,
                        integrity_sha256=row.integrity_sha256,
                        integrity_byte_count=row.integrity_byte_count,
                        integrity_created_at=row.integrity_created_at,
                    )
                )
            work.status = "complete" if lifecycle == GeneratedArtifactLifecycle.DELETED.value else ("unknown" if lifecycle == GeneratedArtifactLifecycle.CLEANUP_UNKNOWN.value else "failed")
            work.updated_at = row.updated_at
            work.idempotency_key = idempotency_key or work.idempotency_key
            if idempotency_key is not None:
                receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, operation, idempotency_key))
                if receipt is None:
                    session.add(GeneratedArtifactIdempotencyRow(
                        principal_issuer=owner[0], principal_subject=owner[1], operation=operation,
                        idempotency_key=idempotency_key,
                        fingerprint=idempotency_fingerprint or f"{artifact_id}:{expected_version}:{lifecycle}",
                        artifact_id=artifact_id, result_version=row.version, result_lifecycle=lifecycle,
                    ))
                else:
                    receipt.fingerprint = idempotency_fingerprint or receipt.fingerprint
                    receipt.result_version = row.version
                    receipt.result_lifecycle = lifecycle
            session.add(self._event_row(self._artifact(row, self._revision(current) if current else None), "artifact.deletion_changed"))
            await self._stage_lifecycle_event(session, self._artifact(row, self._revision(current) if current else None), "artifact.deletion_changed")
            return self._artifact(row, self._revision(current) if current else None)

    async def admit_cleanup(self, owner: tuple[str, str], artifact_id: UUID, *, expected_version: int, operation: str, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(select(GeneratedArtifactRow).where(
                GeneratedArtifactRow.id == artifact_id,
                GeneratedArtifactRow.principal_issuer == owner[0],
                GeneratedArtifactRow.principal_subject == owner[1],
            ).with_for_update())
            if row is None:
                raise GeneratedArtifactNotFound("artifact not found")
            receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, operation, idempotency_key))
            if receipt is not None:
                if receipt.fingerprint != idempotency_fingerprint or receipt.artifact_id != artifact_id:
                    raise GeneratedArtifactConflict("cleanup idempotency key was reused")
                return self._artifact(row)
            required = GeneratedArtifactLifecycle.DELETION_FAILED.value if operation == "retry_cleanup" else GeneratedArtifactLifecycle.CLEANUP_UNKNOWN.value
            if row.lifecycle != required or row.version != expected_version:
                raise GeneratedArtifactConflict("cleanup admission precondition failed")
            now = _now()
            await session.merge(GeneratedArtifactCleanupWorkRow(
                artifact_id=artifact_id, operation=operation, expected_version=expected_version,
                status="pending", fingerprint=idempotency_fingerprint, idempotency_key=idempotency_key,
                updated_at=now,
            ))
            session.add(GeneratedArtifactIdempotencyRow(
                principal_issuer=owner[0], principal_subject=owner[1], operation=operation,
                idempotency_key=idempotency_key, fingerprint=idempotency_fingerprint,
                artifact_id=artifact_id, result_version=expected_version, result_lifecycle="admitted",
            ))
            current = await session.get(GeneratedArtifactRevisionRow, (artifact_id, row.current_revision)) if row.current_revision else None
            return self._artifact(row, self._revision(current) if current else None)

    async def claim_pending_cleanup(self, owner: tuple[str, str], *, limit: int = 20) -> list[GeneratedArtifactCleanupWork]:
        async with self.sessions() as session, session.begin():
            now = _now()
            rows = list((await session.scalars(
                select(GeneratedArtifactCleanupWorkRow)
                .join(GeneratedArtifactRow, GeneratedArtifactRow.id == GeneratedArtifactCleanupWorkRow.artifact_id)
                .where(
                    ((GeneratedArtifactCleanupWorkRow.status == "pending") | ((GeneratedArtifactCleanupWorkRow.status == "claimed") & (GeneratedArtifactCleanupWorkRow.lease_expires_at <= now))),
                    GeneratedArtifactRow.principal_issuer == owner[0],
                    GeneratedArtifactRow.principal_subject == owner[1],
                )
                .order_by(GeneratedArtifactCleanupWorkRow.updated_at, GeneratedArtifactCleanupWorkRow.artifact_id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )).all())
            result: list[GeneratedArtifactCleanupWork] = []
            for work in rows:
                work.status = "claimed"
                work.claim_token = uuid4().hex
                work.lease_expires_at = now + timedelta(seconds=CLEANUP_LEASE_SECONDS)
                result.append(GeneratedArtifactCleanupWork(work.artifact_id, work.operation, work.expected_version, work.status, work.fingerprint, work.idempotency_key, work.claim_token, work.lease_expires_at))
            return result

    async def replay_cleanup(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, idempotency_key: str, idempotency_fingerprint: str) -> GeneratedArtifact | None:
        async with self.sessions() as session:
            receipt = await session.get(GeneratedArtifactIdempotencyRow, (*owner, operation, idempotency_key))
            if receipt is None:
                return None
            if receipt.fingerprint != idempotency_fingerprint:
                raise GeneratedArtifactConflict("cleanup idempotency key was reused")
            if receipt.artifact_id != artifact_id:
                raise GeneratedArtifactConflict("cleanup receipt belongs to another artifact")
            row = await session.scalar(select(GeneratedArtifactRow).where(
                GeneratedArtifactRow.id == artifact_id,
                GeneratedArtifactRow.principal_issuer == owner[0],
                GeneratedArtifactRow.principal_subject == owner[1],
            ))
            return self._artifact(row) if row is not None else None

    async def assert_cleanup_claim(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, claim_token: str) -> None:
        async with self.sessions() as session:
            row = await session.scalar(select(GeneratedArtifactRow).where(
                GeneratedArtifactRow.id == artifact_id,
                GeneratedArtifactRow.principal_issuer == owner[0],
                GeneratedArtifactRow.principal_subject == owner[1],
            ))
            work = await session.get(GeneratedArtifactCleanupWorkRow, (artifact_id, operation))
            now = _now()
            if row is None or work is None or work.status != "claimed" or work.claim_token != claim_token or (work.lease_expires_at is not None and work.lease_expires_at <= now):
                raise GeneratedArtifactConflict("stale cleanup claim")

    async def renew_cleanup_claim(self, owner: tuple[str, str], artifact_id: UUID, *, operation: str, claim_token: str) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(select(GeneratedArtifactRow).where(
                GeneratedArtifactRow.id == artifact_id,
                GeneratedArtifactRow.principal_issuer == owner[0],
                GeneratedArtifactRow.principal_subject == owner[1],
            ))
            work = await session.get(GeneratedArtifactCleanupWorkRow, (artifact_id, operation), with_for_update=True)
            now = _now()
            if row is None or work is None or work.status != "claimed" or work.claim_token != claim_token or (work.lease_expires_at is not None and work.lease_expires_at <= now):
                raise GeneratedArtifactConflict("stale cleanup claim")
            work.lease_expires_at = now + timedelta(seconds=CLEANUP_LEASE_SECONDS)
            work.updated_at = now
