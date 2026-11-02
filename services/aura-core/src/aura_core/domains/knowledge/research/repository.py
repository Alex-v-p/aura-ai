# ruff: noqa: E501

"""Memory and SQL repositories for durable research."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.execution.tasks.contracts import TaskSourceInvalidation, TaskSourceKind
from aura_core.platform.outbox import SqlOutboxRepository, make_aggregate_command

from .persistence import (
    ResearchCheckpointRow,
    ResearchCitationRow,
    ResearchCommandIdempotencyRow,
    ResearchDeletionCleanupRow,
    ResearchEvidenceRow,
    ResearchFallbackCohortRow,
    ResearchIdempotencyRow,
    ResearchOperationRow,
    ResearchSettingsRow,
    ResearchTaskRow,
)
from .public import (
    MemoryResearchRepository,
    ResearchBudget,
    ResearchBudgetConsumption,
    ResearchCheckpoint,
    ResearchCitation,
    ResearchConflict,
    ResearchContentState,
    ResearchDeliveryClaim,
    ResearchEvidence,
    ResearchMode,
    ResearchNotFound,
    ResearchOutcome,
    ResearchSettings,
    ResearchStage,
    ResearchTask,
    ResearchTerminalDeliverySnapshot,
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class SqlResearchRepository:
    """Research-owned SQL adapter; no other domain tables are queried."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] | None = None, outbox_repository: SqlOutboxRepository | None = None, invalidation_stage: object | None = None) -> None:
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.outbox_repository = outbox_repository or SqlOutboxRepository()
        self.invalidation_stage = invalidation_stage

    async def _stage_invalidation(self, session: AsyncSession, task: ResearchTask) -> None:
        stage = self.invalidation_stage
        method = getattr(stage, "stage_task_source_invalidation", None)
        if not callable(method):
            return
        event_id = uuid5(UUID("9fae2b80-f0c7-4f1d-8e9a-8bd4d7c9a145"), f"task-invalidation:{task.id}:{task.version}")
        event = TaskSourceInvalidation(
            TaskSourceKind.RESEARCH,
            task.id,
            task.version,
            event_id,
            task.updated_at,
            event_id,
            event_id,
        )
        await cast(Any, method)(session, payload=event.payload())

    async def get_settings(self, owner: tuple[str, str]) -> ResearchSettings | None:
        async with self.sessions() as session:
            row = await session.get(ResearchSettingsRow, {"principal_issuer": owner[0], "principal_subject": owner[1]})
            if row is None:
                return None
            defaults = {ResearchMode(mode): ResearchBudget(**_budget_kwargs(value)) for mode, value in row.defaults.items()}
            return ResearchSettings(row.version, _utc(row.updated_at), defaults)

    async def save_settings(self, owner: tuple[str, str], settings: ResearchSettings, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, result: Mapping[str, object] | None = None) -> ResearchSettings:
        async with self.sessions() as session, session.begin():
            if idempotency_key:
                receipt = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": owner[0], "principal_subject": owner[1], "idempotency_key": idempotency_key}, with_for_update=True)
                if receipt is not None:
                    if receipt.fingerprint != fingerprint or receipt.command != "settings":
                        raise ResearchConflict("idempotency key was reused")
                    current = await session.get(ResearchSettingsRow, {"principal_issuer": owner[0], "principal_subject": owner[1]})
                    if current is None:
                        raise ResearchConflict("research settings replay is unavailable")
                    return ResearchSettings(current.version, _utc(current.updated_at), {ResearchMode(mode): ResearchBudget(**_budget_kwargs(value)) for mode, value in current.defaults.items()})
            row = await session.get(ResearchSettingsRow, {"principal_issuer": owner[0], "principal_subject": owner[1]}, with_for_update=True)
            if row is None:
                if expected_version != 1:
                    raise ResearchConflict("research settings version conflict")
                row = ResearchSettingsRow(principal_issuer=owner[0], principal_subject=owner[1], version=settings.version, defaults={mode.value: value.as_dict() for mode, value in settings.defaults.items()}, updated_at=settings.updated_at)
                session.add(row)
            else:
                if row.version != expected_version:
                    raise ResearchConflict("research settings version conflict")
                row.version = settings.version
                row.defaults = {mode.value: value.as_dict() for mode, value in settings.defaults.items()}
                row.updated_at = settings.updated_at
            if idempotency_key and fingerprint is not None:
                session.add(ResearchCommandIdempotencyRow(principal_issuer=owner[0], principal_subject=owner[1], idempotency_key=idempotency_key, fingerprint=fingerprint, command="settings", research_id=None, source_version=settings.version, result=dict(result or {})))
            return settings

    async def _load(self, session: AsyncSession, owner: tuple[str, str], research_id: UUID) -> ResearchTask:
        row = await session.get(ResearchTaskRow, research_id)
        if row is None or (row.principal_issuer, row.principal_subject) != owner:
            raise ResearchNotFound("research task not found")
        evidence_rows = (await session.scalars(select(ResearchEvidenceRow).where(ResearchEvidenceRow.research_id == research_id).order_by(ResearchEvidenceRow.id))).all()
        citation_rows = (await session.scalars(select(ResearchCitationRow).where(ResearchCitationRow.research_id == research_id).order_by(ResearchCitationRow.id))).all()
        checkpoint_rows = (await session.scalars(select(ResearchCheckpointRow).where(ResearchCheckpointRow.research_id == research_id).order_by(ResearchCheckpointRow.version))).all()
        return ResearchTask(
            row.id, row.principal_issuer, row.principal_subject, row.question, ResearchMode(row.mode), ResearchStage(row.stage), ResearchOutcome(row.outcome) if row.outcome else None, ResearchContentState(row.content_state), row.version, _utc(row.created_at), _utc(row.updated_at), _utc(row.finished_at) if row.finished_at else None, ResearchBudget(**{key: value for key, value in _budget_kwargs(row.admitted_budget).items()}), ResearchBudgetConsumption(**_consumption_kwargs(row.consumed_budget)), row.extension_accepted, row.failure_class, tuple(row.seed_urls or ()), tuple(row.domains or ()), tuple(ResearchEvidence(item.id, item.research_id, item.label, item.url, item.publisher, _utc(item.retrieved_at), item.media_type, item.extraction_method, _utc(item.freshness_at) if item.freshness_at else None, tuple(item.excerpts or ()), item.content_sha256, item.locator) for item in evidence_rows), tuple(ResearchCitation(item.source_id, item.label, item.url, _utc(item.retrieved_at), item.locator) for item in citation_rows), tuple(row.artifact_refs or ()), row.cancellation_requested, row.deletion_audit_id, tuple(ResearchCheckpoint(item.id, item.research_id, item.version, ResearchStage(item.stage), _utc(item.occurred_at), item.operation) for item in checkpoint_rows), row.cancellation_requested_at, _utc(row.deadline_at) if row.deadline_at else None, dict(row.capability_snapshot or {}), dict(row.origin or {}), row.policy_revision, row.synthesized_answer, row.delivery_claim_token, row.delivery_claim_phase, _utc(row.delivery_claim_expires_at) if row.delivery_claim_expires_at else None,
        )

    async def admit(self, task: ResearchTask, *, idempotency_key: str | None, fingerprint: str) -> ResearchTask:
        async with self.sessions() as session, session.begin():
            if idempotency_key:
                prior = await session.get(ResearchIdempotencyRow, {"principal_issuer": task.issuer, "principal_subject": task.subject, "idempotency_key": idempotency_key})
                if prior:
                    if prior.fingerprint != fingerprint:
                        raise ResearchConflict("idempotency key was reused")
                    return await self._load(session, task.owner, prior.research_id)
            session.add(ResearchTaskRow(id=task.id, principal_issuer=task.issuer, principal_subject=task.subject, question=task.question, synthesized_answer=task.synthesized_answer, delivery_claim_token=task.delivery_claim_token, delivery_claim_phase=task.delivery_claim_phase, delivery_claim_expires_at=task.delivery_claim_expires_at, mode=task.mode.value, stage=task.stage.value, outcome=task.outcome.value if task.outcome else None, content_state=task.content_state.value, version=task.version, admitted_budget=task.admitted_budget.as_dict(), consumed_budget=task.consumed_budget.as_dict(), extension_accepted=task.extension_accepted, failure_class=task.failure_class, seed_urls=list(task.seed_urls), domains=list(task.domains), artifact_refs=list(task.artifact_refs), capability_snapshot=dict(task.capability_snapshot), origin=dict(task.origin), policy_revision=task.policy_revision, deadline_at=task.deadline_at, cancellation_requested=task.cancellation_requested, cancellation_requested_at=task.cancellation_requested_at, deletion_audit_id=task.deletion_audit_id, created_at=task.created_at, updated_at=task.updated_at, finished_at=task.finished_at))
            for item in task.checkpoints:
                session.add(ResearchCheckpointRow(id=item.id, research_id=item.research_id, version=item.version, stage=item.stage.value, operation=item.operation, occurred_at=item.occurred_at))
            await self.outbox_repository.stage_command(
                session,
                command=make_aggregate_command(
                    "aura.knowledge.research.execute.v1",
                    uuid5(UUID("9fae2b80-f0c7-4f1d-8e9a-8bd4d7c9a145"), f"dispatch:{task.id}:{task.version}"),
                    "knowledge.research",
                    task.id,
                    identifiers=(("researchId", task.id), ("taskVersion", task.version)),
                ),
            )
            await self._stage_invalidation(session, task)
            if idempotency_key:
                session.add(ResearchIdempotencyRow(principal_issuer=task.issuer, principal_subject=task.subject, idempotency_key=idempotency_key, fingerprint=fingerprint, research_id=task.id))
            return task

    async def get(self, owner: tuple[str, str], research_id: UUID) -> ResearchTask:
        async with self.sessions() as session:
            return await self._load(session, owner, research_id)

    async def save(self, task: ResearchTask, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask:
        async with self.sessions() as session, session.begin():
            if idempotency_key:
                receipt = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": task.issuer, "principal_subject": task.subject, "idempotency_key": idempotency_key}, with_for_update=True)
                if receipt is not None:
                    if receipt.fingerprint != fingerprint or receipt.command != command:
                        raise ResearchConflict("idempotency key was reused")
                    return await self._load(session, task.owner, receipt.research_id or task.id)
            row = await session.get(ResearchTaskRow, task.id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != task.owner:
                raise ResearchNotFound("research task not found")
            if row.version != expected_version:
                raise ResearchConflict("research task version conflict")
            row.question = task.question
            row.synthesized_answer = task.synthesized_answer
            row.delivery_claim_token = task.delivery_claim_token
            row.delivery_claim_phase = task.delivery_claim_phase
            row.delivery_claim_expires_at = task.delivery_claim_expires_at
            row.stage = task.stage.value
            row.outcome = task.outcome.value if task.outcome else None
            row.content_state = task.content_state.value
            row.version = task.version
            row.consumed_budget = task.consumed_budget.as_dict()
            row.admitted_budget = task.admitted_budget.as_dict()
            row.extension_accepted = task.extension_accepted
            row.failure_class = task.failure_class
            row.seed_urls = list(task.seed_urls)
            row.domains = list(task.domains)
            row.artifact_refs = [dict(reference) for reference in task.artifact_refs]
            row.capability_snapshot = dict(task.capability_snapshot)
            row.origin = dict(task.origin)
            row.policy_revision = task.policy_revision
            row.deadline_at = task.deadline_at
            row.cancellation_requested = task.cancellation_requested
            row.cancellation_requested_at = task.cancellation_requested_at
            row.deletion_audit_id = task.deletion_audit_id
            row.updated_at = task.updated_at
            row.finished_at = task.finished_at
            for item in task.evidence:
                if await session.get(ResearchEvidenceRow, item.source_id) is None:
                    session.add(ResearchEvidenceRow(id=item.source_id, research_id=item.task_id, label=item.label, url=item.url, publisher=item.publisher, retrieved_at=item.retrieved_at, media_type=item.media_type, extraction_method=item.extraction_method, freshness_at=item.freshness_at, excerpts=list(item.excerpts), content_sha256=item.content_sha256, locator=item.locator))
            for item in task.citations:
                existing = (await session.scalars(select(ResearchCitationRow).where(ResearchCitationRow.research_id == task.id, ResearchCitationRow.source_id == item.source_id))).first()
                if existing is None:
                    session.add(ResearchCitationRow(id=uuid4(), research_id=task.id, source_id=item.source_id, label=item.label, url=item.url, retrieved_at=item.retrieved_at, locator=item.locator))
            for item in task.checkpoints:
                if await session.get(ResearchCheckpointRow, item.id) is None:
                    session.add(ResearchCheckpointRow(id=item.id, research_id=item.research_id, version=item.version, stage=item.stage.value, operation=item.operation, occurred_at=item.occurred_at))
            await self._stage_invalidation(session, task)
            if idempotency_key and fingerprint is not None and command is not None:
                session.add(ResearchCommandIdempotencyRow(principal_issuer=task.issuer, principal_subject=task.subject, idempotency_key=idempotency_key, fingerprint=fingerprint, command=command, research_id=task.id, source_version=task.version, result=dict(result or {})))
            return task

    async def list(self, owner: tuple[str, str], *, mode: ResearchMode | None = None, outcome: ResearchOutcome | None = None, limit: int = 25, offset: int = 0) -> list[ResearchTask]:
        async with self.sessions() as session:
            statement = select(ResearchTaskRow).where(ResearchTaskRow.principal_issuer == owner[0], ResearchTaskRow.principal_subject == owner[1]).order_by(ResearchTaskRow.created_at.desc()).offset(offset).limit(limit)
            if mode:
                statement = statement.where(ResearchTaskRow.mode == mode.value)
            if outcome:
                statement = statement.where(ResearchTaskRow.outcome == outcome.value)
            rows = (await session.scalars(statement)).all()
            return [await self._load(session, owner, row.id) for row in rows]

    async def evidence(self, owner: tuple[str, str], research_id: UUID, *, limit: int = 100, offset: int = 0) -> list[ResearchEvidence]:
        task = await self.get(owner, research_id)
        if task.content_state is not ResearchContentState.ACTIVE:
            return []
        return list(task.evidence[offset : offset + limit])

    async def projection_tasks(self) -> list[ResearchTask]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(ResearchTaskRow).order_by(ResearchTaskRow.updated_at, ResearchTaskRow.id))).all()
            return [await self._load(session, (row.principal_issuer, row.principal_subject), row.id) for row in rows]

    async def get_unscoped(self, research_id: UUID) -> ResearchTask:
        async with self.sessions() as session:
            row = await session.get(ResearchTaskRow, research_id)
            if row is None:
                raise ResearchNotFound("research task not found")
            return await self._load(session, (row.principal_issuer, row.principal_subject), research_id)

    async def delivery_snapshot(self, owner: tuple[str, str], research_id: UUID, *, expected_version: int, expected_origin: Mapping[str, object] | None = None) -> ResearchTerminalDeliverySnapshot:
        # Lock the source row while materialising the bounded snapshot.  A
        # deletion or terminal rewrite therefore cannot pass the fence between
        # authorization and delivery's append/notification phases.
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchTaskRow, research_id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != owner:
                raise ResearchNotFound("research task not found")
            task = await self._load(session, owner, research_id)
            if task.version != expected_version or not task.terminal:
                raise ResearchConflict("research terminal version is stale")
            if task.content_state is not ResearchContentState.ACTIVE:
                raise ResearchConflict("research content is no longer deliverable")
            if expected_origin is not None and dict(task.origin) != dict(expected_origin):
                raise ResearchConflict("research origin fence failed")
            return ResearchTerminalDeliverySnapshot(
                task.id, task.issuer, task.subject, task.version,
                cast(ResearchOutcome, task.outcome), task.content_state,
                task.failure_class, task.synthesized_answer or "Research completed without a persisted answer.",
                tuple(task.citations[:32]), tuple(task.artifact_refs[:8]), dict(task.origin), task.mode,
            )

    async def claim_delivery(
        self,
        owner: tuple[str, str],
        research_id: UUID,
        *,
        expected_version: int,
        phase: str,
        lease_seconds: float = 60.0,
    ) -> ResearchDeliveryClaim:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchTaskRow, research_id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != owner:
                raise ResearchNotFound("research task not found")
            now_value = self.clock()
            expires = _utc(now_value) + timedelta(seconds=lease_seconds)
            existing_expires = _utc(row.delivery_claim_expires_at) if row.delivery_claim_expires_at else None
            if row.version != expected_version or row.outcome is None or row.content_state != ResearchContentState.ACTIVE.value:
                raise ResearchConflict("research content is no longer deliverable")
            if row.delivery_claim_token and existing_expires and existing_expires > _utc(now_value):
                raise ResearchConflict("research delivery is already claimed")
            token = uuid4().hex
            row.delivery_claim_token = token
            row.delivery_claim_phase = phase
            row.delivery_claim_expires_at = expires
            return ResearchDeliveryClaim(research_id, expected_version, token, phase, expires)

    async def settle_delivery_claim(
        self, owner: tuple[str, str], claim: ResearchDeliveryClaim
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchTaskRow, claim.research_id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != owner:
                raise ResearchNotFound("research task not found")
            if row.delivery_claim_token != claim.token:
                raise ResearchConflict("research delivery claim is stale")
            row.delivery_claim_token = None
            row.delivery_claim_phase = None
            row.delivery_claim_expires_at = None

    async def delivery_claim_active(self, research_id: UUID) -> bool:
        async with self.sessions() as session:
            row = await session.get(ResearchTaskRow, research_id)
            return bool(
                row
                and row.delivery_claim_token
                and row.delivery_claim_expires_at is not None
                and _utc(row.delivery_claim_expires_at) > _utc(self.clock())
            )

    async def admit_deletion(
        self, task: ResearchTask, *, expected_version: int
    ) -> ResearchTask:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchTaskRow, task.id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != task.owner:
                raise ResearchNotFound("research task not found")
            if row.version != expected_version or row.outcome is None:
                raise ResearchConflict("research content is not deletable")
            if row.content_state != ResearchContentState.ACTIVE.value:
                return await self._load(session, task.owner, task.id)
            now_value = _utc(self.clock())
            if (
                row.delivery_claim_token
                and row.delivery_claim_expires_at is not None
                and _utc(row.delivery_claim_expires_at) > now_value
            ):
                raise ResearchConflict("research delivery is settling")
            current = await self._load(session, task.owner, task.id)
            tombstone = replace(
                current,
                question="",
                seed_urls=(),
                domains=(),
                evidence=(),
                citations=(),
                synthesized_answer=None,
                content_state=ResearchContentState.DELETION_PENDING,
                deletion_audit_id=uuid4(),
                delivery_claim_token=None,
                delivery_claim_phase=None,
                delivery_claim_expires_at=None,
                version=current.version + 1,
                updated_at=now_value,
            )
            row.question = ""
            row.synthesized_answer = None
            row.seed_urls = []
            row.domains = []
            row.content_state = tombstone.content_state.value
            row.deletion_audit_id = tombstone.deletion_audit_id
            row.version = tombstone.version
            row.updated_at = tombstone.updated_at
            row.delivery_claim_token = None
            row.delivery_claim_phase = None
            row.delivery_claim_expires_at = None
            return tombstone

    async def purge_private_content(self, research_id: UUID) -> None:
        async with self.sessions() as session, session.begin():
            task_row = await session.get(ResearchTaskRow, research_id, with_for_update=True)
            if task_row is not None:
                task_row.synthesized_answer = None
            await session.execute(delete(ResearchCitationRow).where(ResearchCitationRow.research_id == research_id))
            await session.execute(delete(ResearchEvidenceRow).where(ResearchEvidenceRow.research_id == research_id))
            rows = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id))).all()
            for row in rows:
                row.response = {}

    async def finalize_deletion(self, task: ResearchTask, *, expected_version: int, artifact_refs: tuple[Mapping[str, object], ...], delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask:
        async with self.sessions() as session, session.begin():
            if idempotency_key:
                receipt = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": task.issuer, "principal_subject": task.subject, "idempotency_key": idempotency_key}, with_for_update=True)
                if receipt is not None:
                    if receipt.fingerprint != fingerprint or receipt.command != command:
                        raise ResearchConflict("idempotency key was reused")
                    return await self._load(session, task.owner, receipt.research_id or task.id)
            row = await session.get(ResearchTaskRow, task.id, with_for_update=True)
            if row is None or (row.principal_issuer, row.principal_subject) != task.owner:
                raise ResearchNotFound("research task not found")
            if row.version != expected_version:
                raise ResearchConflict("research task version conflict")
            cleaned = replace(task, question="", seed_urls=(), domains=(), evidence=(), citations=(), artifact_refs=artifact_refs, synthesized_answer=None, content_state=ResearchContentState.DELETED, version=task.version + 1, updated_at=task.updated_at)
            await session.execute(delete(ResearchCitationRow).where(ResearchCitationRow.research_id == task.id))
            await session.execute(delete(ResearchEvidenceRow).where(ResearchEvidenceRow.research_id == task.id))
            rows = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == task.id))).all()
            for operation in rows:
                operation.response = {}
            row.question = ""
            row.synthesized_answer = None
            row.stage = cleaned.stage.value
            row.outcome = cleaned.outcome.value if cleaned.outcome else None
            row.content_state = cleaned.content_state.value
            row.version = cleaned.version
            row.seed_urls = []
            row.domains = []
            row.artifact_refs = [dict(reference) for reference in artifact_refs]
            row.updated_at = cleaned.updated_at
            row.finished_at = cleaned.finished_at
            cleanup = await session.get(ResearchDeletionCleanupRow, task.id, with_for_update=True)
            if cleanup is None:
                session.add(ResearchDeletionCleanupRow(research_id=task.id, audit_id=task.deletion_audit_id or uuid4(), delete_linked_artifacts=delete_linked_artifacts, evidence_status=evidence_status, artifact_status=artifact_status, status=status, last_error=None, updated_at=self.clock()))
            else:
                cleanup.evidence_status = evidence_status
                cleanup.artifact_status = artifact_status
                cleanup.status = status
                cleanup.last_error = None
                cleanup.updated_at = self.clock()
            await self._stage_invalidation(session, cleaned)
            if idempotency_key and fingerprint is not None and command is not None:
                session.add(ResearchCommandIdempotencyRow(principal_issuer=task.issuer, principal_subject=task.subject, idempotency_key=idempotency_key, fingerprint=fingerprint, command=command, research_id=task.id, source_version=cleaned.version, result=dict(result or {"sourceVersion": cleaned.version})))
            return cleaned

    async def record_cleanup(self, research_id: UUID, audit_id: UUID, *, delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, last_error: str | None = None) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchDeletionCleanupRow, research_id, with_for_update=True)
            if row is None:
                session.add(ResearchDeletionCleanupRow(research_id=research_id, audit_id=audit_id, delete_linked_artifacts=delete_linked_artifacts, evidence_status=evidence_status, artifact_status=artifact_status, status=status, last_error=last_error, updated_at=self.clock()))
            else:
                row.audit_id = audit_id
                row.delete_linked_artifacts = delete_linked_artifacts
                row.evidence_status = evidence_status
                row.artifact_status = artifact_status
                row.status = status
                row.last_error = last_error
                row.updated_at = self.clock()

    async def get_cleanup(self, research_id: UUID) -> Mapping[str, object] | None:
        async with self.sessions() as session:
            row = await session.get(ResearchDeletionCleanupRow, research_id)
            if row is None:
                return None
            return {"auditId": row.audit_id, "deleteLinkedArtifacts": row.delete_linked_artifacts, "evidenceStatus": row.evidence_status, "artifactStatus": row.artifact_status, "status": row.status, "lastError": row.last_error}

    async def begin_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None = None, capability_enabled: bool = True) -> bool:
        async with self.sessions() as session, session.begin():
            task_row = await session.get(ResearchTaskRow, research_id, with_for_update=True)
            if task_row is None or task_row.outcome is not None or task_row.cancellation_requested or task_row.content_state != ResearchContentState.ACTIVE.value or not capability_enabled or (deadline_at is not None and now >= deadline_at):
                return False
            existing = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id, ResearchOperationRow.operation == operation, ResearchOperationRow.request_id == request_id))).first()
            if existing is not None:
                return False
            session.add(ResearchOperationRow(id=uuid4(), research_id=research_id, operation=operation, request_id=request_id, attempt=1, status="running", admitted_at=now, settled_at=None, response={}))
            return True

    async def dispatch_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None, capability_check: Callable[[ResearchTask], bool] | None = None, capability_enabled: bool | None = None, invoke: Callable[[], Any]) -> tuple[bool, Any | None]:
        async with self.sessions() as session, session.begin():
            task_row = await session.get(ResearchTaskRow, research_id, with_for_update=True)
            row = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id, ResearchOperationRow.operation == operation, ResearchOperationRow.request_id == request_id))).first()
            current_task = await self._load(session, (task_row.principal_issuer, task_row.principal_subject), research_id) if task_row is not None else None
            current_capability = capability_check(current_task) if current_task is not None and capability_check is not None else bool(capability_enabled)
            if task_row is None or row is None or row.status != "running" or task_row.outcome is not None or task_row.cancellation_requested or task_row.content_state != ResearchContentState.ACTIVE.value or not current_capability or (deadline_at is not None and now >= deadline_at):
                if row is not None and row.status == "running" and (task_row is None or task_row.cancellation_requested or not current_capability):
                    row.status = "canceled"
                return False, None
            row.response = {"dispatchLease": str(request_id), "dispatchStartedAt": now.isoformat()}
            return True, asyncio.create_task(invoke())

    async def settle_operation(self, research_id: UUID, operation: str, request_id: UUID, *, status: str, now: datetime, response: Mapping[str, object] | None = None) -> None:
        async with self.sessions() as session, session.begin():
            row = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id, ResearchOperationRow.operation == operation, ResearchOperationRow.request_id == request_id))).first()
            task = await session.get(ResearchTaskRow, research_id)
            if row is not None and row.status == "running" and task is not None and task.outcome is None:
                row.status = status
                row.settled_at = now
                row.response = dict(response or {})

    async def get_operation(self, research_id: UUID, operation: str, request_id: UUID) -> Mapping[str, object] | None:
        async with self.sessions() as session:
            row = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id, ResearchOperationRow.operation == operation, ResearchOperationRow.request_id == request_id))).first()
            if row is None:
                return None
            return {"status": row.status, "admittedAt": row.admitted_at, "settledAt": row.settled_at, "response": dict(row.response or {})}

    async def active_operations(self, research_id: UUID) -> tuple[tuple[str, UUID], ...]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(ResearchOperationRow).where(ResearchOperationRow.research_id == research_id, ResearchOperationRow.status == "running"))).all()
            return tuple((row.operation, row.request_id) for row in rows)

    async def record_fallback(self, at: datetime, *, accepted: bool) -> tuple[int, int, float]:
        value = _utc(at)
        cohort = value.replace(minute=(value.minute // 5) * 5, second=0, microsecond=0)
        # The unique component/cohort key is the durable race boundary.  A
        # concurrent first writer may win the insert; retry then locks and
        # increments the row rather than losing either numerator or denominator.
        for attempt in range(2):
            try:
                async with self.sessions() as session, session.begin():
                    row = await session.get(ResearchFallbackCohortRow, {"component": "aura.research.coordinator@1", "cohort_start": cohort}, with_for_update=True)
                    if row is None:
                        row = ResearchFallbackCohortRow(component="aura.research.coordinator@1", cohort_start=cohort, static_count=1, browser_count=int(accepted))
                        session.add(row)
                        await session.flush()
                    else:
                        row.static_count += 1
                        row.browser_count += int(accepted)
                    return row.static_count, row.browser_count, row.browser_count / row.static_count
            except IntegrityError:
                if attempt == 1:
                    raise
        raise RuntimeError("fallback cohort upsert did not settle")

    async def get_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, str, UUID, Mapping[str, object]] | None:
        async with self.sessions() as session:
            row = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": owner[0], "principal_subject": owner[1], "idempotency_key": key})
            if row is None or row.research_id is None:
                return None
            return row.fingerprint, row.command, row.research_id, row.result

    async def save_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, command: str, research_id: UUID, source_version: int, result: Mapping[str, object]) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": owner[0], "principal_subject": owner[1], "idempotency_key": key}, with_for_update=True)
            if row is None:
                session.add(ResearchCommandIdempotencyRow(principal_issuer=owner[0], principal_subject=owner[1], idempotency_key=key, fingerprint=fingerprint, command=command, research_id=research_id, source_version=source_version, result=dict(result)))
            elif row.fingerprint != fingerprint or row.command != command:
                raise ResearchConflict("idempotency key was reused")

    async def get_settings_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, Mapping[str, object]] | None:
        async with self.sessions() as session:
            row = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": owner[0], "principal_subject": owner[1], "idempotency_key": key})
            if row is None or row.command != "settings":
                return None
            return row.fingerprint, dict(row.result)

    async def save_settings_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, result: Mapping[str, object]) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchCommandIdempotencyRow, {"principal_issuer": owner[0], "principal_subject": owner[1], "idempotency_key": key}, with_for_update=True)
            if row is None:
                session.add(ResearchCommandIdempotencyRow(principal_issuer=owner[0], principal_subject=owner[1], idempotency_key=key, fingerprint=fingerprint, command="settings", research_id=None, source_version=int(cast(Any, result.get("version", 0))), result=dict(result)))
            elif row.fingerprint != fingerprint or row.command != "settings":
                raise ResearchConflict("idempotency key was reused")


def _budget_kwargs(value: dict[str, Any]) -> dict[str, int]:
    return {"wall_time_seconds": int(value.get("wallTimeSeconds", 0)), "fetched_sources": int(value.get("fetchedSources", 0)), "search_requests": int(value.get("searchRequests", 0)), "browser_pages": int(value.get("browserPages", 0)), "model_calls": int(value.get("modelCalls", 0))}


def _consumption_kwargs(value: dict[str, Any]) -> dict[str, int]:
    return _budget_kwargs(value)


__all__ = ["MemoryResearchRepository", "SqlResearchRepository"]
