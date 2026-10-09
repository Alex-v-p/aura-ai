"""Deterministic in-memory memory repository implementation."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4, uuid5

from aura_core.domains.knowledge.memory.contracts import (
    DEFAULT_MEMORY_HALF_LIFE_DAYS,
    DEFAULT_MEMORY_IMPORTANCE,
    MEMORY_ID_NAMESPACE,
    PURGE_CONFIRMATION,
    CandidateState,
    MemoryAction,
    MemoryActivityItem,
    MemoryActivitySnapshot,
    MemoryAuditRecord,
    MemoryCandidate,
    MemoryEmbedding,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingJob,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryPurgeReplayNotFound,
    MemoryRecord,
    MemoryReindexSnapshot,
    MemoryRelation,
    MemoryRevision,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemorySensitivity,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
    classify_sensitivity,
    command_values,
    contains_secret,
    content_free_candidate,
    decide_candidate,
    fingerprint,
    memory_activity_id,
    normalize_candidate_for_approval,
    owner,
    reindex_pending,
    scope_authorized,
    validate_memory_text,
    validate_provenance,
    validate_revision,
)
from aura_core.domains.knowledge.memory.repository_ports import MemoryRepository

_command_values = command_values
_content_free_candidate = content_free_candidate
_fingerprint = fingerprint
_normalize_candidate_for_approval = normalize_candidate_for_approval
_owner = owner
_scope_authorized = scope_authorized
_REINDEX_PENDING = reindex_pending


def _authorized_ids(value: object) -> frozenset[UUID] | None:
    """Validate grants without turning malformed input into no restriction."""
    if value is None:
        return None
    if not isinstance(value, (set, frozenset, list, tuple)):
        raise MemoryValidationError("authorized agent grants are invalid")
    items = tuple(cast(Iterable[object], value))
    if not all(isinstance(item, UUID) for item in items):
        raise MemoryValidationError("authorized agent grants are invalid")
    return frozenset(cast(Iterable[UUID], items))


def _scope_type(value: object) -> MemoryScopeType | None:
    if value is None:
        return None
    if isinstance(value, MemoryScopeType):
        return value
    raise MemoryValidationError("memory scope type is invalid")


def _agent_id(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    raise MemoryValidationError("agent profile identifier is invalid")


def _as_int(value: object, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float, str)) else default


def _as_float(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float, str)) else default


class _MemoryStoreBase(MemoryRepository):
    """Deterministic in-memory implementation of the public memory port."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.memories: dict[UUID, MemoryRecord] = {}
        self.purge_audit: list[MemoryAuditRecord] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, object]] = {}
        self._purge_tombstones: set[tuple[str, str, str]] = set()
        self._purge_fences: set[tuple[str, str, UUID]] = set()
        self.embedding_generations: dict[UUID, MemoryEmbeddingGeneration] = {}
        self.embedding_jobs: dict[UUID, MemoryEmbeddingJob] = {}
        self.processing_jobs: dict[UUID, MemoryProcessingJob] = {}
        self.candidates: dict[UUID, MemoryCandidate] = {}
        self.outcomes: list[dict[str, object]] = []
        self.model_configurations: dict[tuple[str, str], MemoryModelConfiguration] = {}
        self.maintenance_state: dict[tuple[str, str], datetime] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self._candidate_evidence_loader: Callable[..., Awaitable[object]] | None = None
        self._embedding_queue_boundary: Callable[..., Awaitable[object]] | None = None

    def set_candidate_evidence_loader(
        self, loader: Callable[..., Awaitable[object]] | None
    ) -> None:
        self._candidate_evidence_loader = loader

    def set_embedding_queue_boundary(
        self, boundary: Callable[..., Awaitable[object]] | None
    ) -> None:
        self._embedding_queue_boundary = boundary

    async def save_maintenance_state(
        self,
        issuer: str,
        subject: str,
        *,
        ran_at: datetime,
        generation: int | None = None,
        generation_id: UUID | None = None,
        cursor: UUID | None = None,
        completed: int = 0,
    ) -> None:
        del generation, generation_id, cursor, completed
        self.maintenance_state[(issuer, subject)] = ran_at

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def _replay(
        self, issuer: str, subject: str, key: str | None, fingerprint: str
    ) -> object | None:
        """Return a previously recorded command result for this owner."""
        if not key:
            return None
        prior = self._idempotency.get((issuer, subject, key))
        if (issuer, subject, key) in self._purge_tombstones:
            raise MemoryIdempotencyConflict("idempotency key is unavailable after purge")
        if prior is None:
            return None
        if prior[0] != fingerprint:
            raise MemoryIdempotencyConflict("idempotency key payload conflict")
        return prior[1]

    def _record_replay(
        self, issuer: str, subject: str, key: str | None, fingerprint: str, value: object
    ) -> None:
        """Record a command result for deterministic idempotent replay."""
        if key:
            self._idempotency[(issuer, subject, key)] = (fingerprint, value)

    async def queue_embedding_job(
        self,
        issuer: str,
        subject: str,
        *,
        memory_id: UUID,
        revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob:
        """Queue a durable embedding job in the concrete repository."""
        raise NotImplementedError

    async def reserve_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        generation = self.embedding_generations.get(generation_id)
        if generation is None or generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if generation.status != "building":
            raise MemoryValidationError("embedding generation is not resumable")
        command_key = f"reindex:{idempotency_key}"
        prior = self._replay(issuer, subject, command_key, fingerprint)
        if prior is not None:
            return prior is _REINDEX_PENDING
        self._record_replay(
            issuer,
            subject,
            command_key,
            fingerprint,
            _REINDEX_PENDING,
        )
        return True

    async def release_reindex_command(
        self, issuer: str, subject: str, idempotency_key: str, fingerprint: str
    ) -> None:
        command_key = (issuer, subject, f"reindex:{idempotency_key}")
        prior = self._idempotency.get(command_key)
        if prior is not None and prior[0] == fingerprint:
            self._idempotency.pop(command_key, None)

    async def complete_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> None:
        command_key = (issuer, subject, f"reindex:{idempotency_key}")
        prior = self._idempotency.get(command_key)
        if prior is None or prior[0] != fingerprint:
            raise MemoryIdempotencyConflict("reindex command receipt is unavailable")
        generation = await self.get_embedding_generation(issuer, subject, generation_id)
        self._idempotency[command_key] = (
            fingerprint,
            MemoryReindexSnapshot(None, generation, 0, 0),
        )

    async def get_active_embedding_generation(
        self, issuer: str, subject: str
    ) -> MemoryEmbeddingGeneration | None:
        """Return the generation atomically selected for this owner."""
        configuration = self.model_configurations.get((issuer, subject))
        if configuration is None:
            return None
        if configuration.embedding_generation is None:
            matches = [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and item.status == "active"
                and item.model_id == configuration.embedding_model_id
                and item.model_revision == configuration.embedding_model_revision
            ]
            if len(matches) == 1:
                configuration = replace(
                    configuration,
                    embedding_generation=matches[0].id,
                    version=configuration.version + 1,
                )
                self.model_configurations[(issuer, subject)] = configuration
        if configuration.embedding_generation is None:
            return None
        generation = self.embedding_generations.get(configuration.embedding_generation)
        if (
            generation is None
            or generation.status != "active"
            or generation.issuer != issuer
            or generation.subject != subject
        ):
            return None
        return generation

    async def _ensure_active_embedding_job(
        self, issuer: str, subject: str, record: MemoryRecord
    ) -> None:
        """Queue the selected generation without invoking an embedding provider."""

        generation = await self.get_active_embedding_generation(issuer, subject)
        if generation is None:
            return
        queue = self._embedding_queue_boundary or self.queue_embedding_job
        await queue(
            issuer,
            subject,
            memory_id=record.id,
            revision_id=record.current_revision_id,
            generation_id=generation.id,
        )

    async def _stage_active_embedding_job(
        self, issuer: str, subject: str, record: MemoryRecord
    ) -> MemoryEmbeddingJob | None:
        """Persist the selected-generation job without crossing instrumentation."""

        generation = await self.get_active_embedding_generation(issuer, subject)
        if generation is None:
            return None
        self._assert_not_fenced(issuer, subject, record.id)
        job_id = uuid5(
            MEMORY_ID_NAMESPACE,
            f"embedding-job:{issuer}:{subject}:{record.current_revision_id}:{generation.id}",
        )
        existing = self.embedding_jobs.get(job_id)
        if existing is not None:
            return replace(existing, lease_id=None, lease_until=None)
        item = MemoryEmbeddingJob(
            job_id,
            issuer,
            subject,
            record.id,
            record.current_revision_id,
            generation.id,
            available_at=self._now(),
        )
        self.embedding_jobs[job_id] = item
        return item

    def _find(
        self,
        issuer: str,
        subject: str,
        memory_id: UUID,
        *,
        scope_type: MemoryScopeType | None = None,
        agent_profile_id: UUID | None = None,
        authorized_agent_ids: frozenset[UUID] | None = None,
    ) -> MemoryRecord:
        record = self.memories.get(memory_id)
        if (
            record is None
            or not _owner(record, issuer, subject)
            or not _scope_authorized(record, scope_type, agent_profile_id, authorized_agent_ids)
        ):
            raise MemoryNotFound("memory not found")
        return record

    def _assert_not_fenced(self, issuer: str, subject: str, memory_id: UUID) -> None:
        if (issuer, subject, memory_id) in self._purge_fences:
            raise MemoryNotFound("memory not found")

    def _candidate_owned(self, issuer: str, subject: str, candidate_id: UUID) -> MemoryCandidate:
        candidate = self.candidates.get(candidate_id)
        if candidate is None or candidate.issuer != issuer or candidate.subject != subject:
            raise MemoryNotFound("memory candidate not found")
        return candidate

    async def get_run_memory_activity(
        self, run_id: UUID, issuer: str, subject: str
    ) -> MemoryActivitySnapshot:
        jobs = [
            item
            for item in self.processing_jobs.values()
            if item.run_id == run_id and item.issuer == issuer and item.subject == subject
        ]
        if not jobs:
            raise MemoryNotFound("run memory activity not found")
        job_ids = {item.id for item in jobs}
        items: list[MemoryActivityItem] = []
        for candidate in self.candidates.values():
            if candidate.job_id not in job_ids:
                continue
            # Ignored and rejected extraction decisions are content-free
            # diagnostics, not owner-facing memory activity.
            if candidate.state is CandidateState.REJECTED:
                continue
            scope: dict[str, object] | None = None
            if candidate.scope is not None:
                scope = {"type": candidate.scope.type.value}
                if candidate.scope.agent_profile_id is not None:
                    scope["agentProfileId"] = str(candidate.scope.agent_profile_id)
            if candidate.state is CandidateState.ACCEPTED:
                action = {
                    MemoryAction.CREATE: "created",
                    MemoryAction.REINFORCE: "reinforced",
                    MemoryAction.DISPUTE: "disputed",
                    MemoryAction.SUPERSEDE: "disputed",
                }.get(candidate.action, "queued_for_review")
                status = "completed"
            else:
                action = "queued_for_review"
                candidate_job = next((item for item in jobs if item.id == candidate.job_id), None)
                status = (
                    "completed"
                    if candidate_job is not None
                    and candidate_job.status
                    in {ProcessingJobStatus.COMPLETED, ProcessingJobStatus.FAILED}
                    else "queued"
                )
            items.append(
                MemoryActivityItem(
                    memory_activity_id(candidate.job_id),
                    action,
                    status,
                    scope,
                    candidate_id=candidate.id,
                    memory_id=candidate.memory_id,
                    occurred_at=candidate.decided_at or candidate.created_at,
                )
            )
        processing = (
            "running"
            if any(item.status is ProcessingJobStatus.RUNNING for item in jobs)
            else "queued"
            if any(
                item.status in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE}
                for item in jobs
            )
            else "settled"
        )
        return MemoryActivitySnapshot(
            run_id,
            processing,
            tuple(sorted(items, key=lambda item: (item.occurred_at, item.id))[:100]),
            reconciled_at=self._now(),
        )

    async def list_candidates(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryCandidate]:
        state = kwargs.get("state")
        action = kwargs.get("action")
        sensitivity = kwargs.get("sensitivity")
        run_id = kwargs.get("run_id")
        limit = max(1, min(_as_int(kwargs.get("limit"), 30), 100))
        values: list[MemoryCandidate] = []
        for candidate in self.candidates.values():
            if candidate.issuer != issuer or candidate.subject != subject:
                continue
            if state is not None and candidate.state.value != str(state):
                continue
            if action is not None and candidate.action.value != str(action):
                continue
            if sensitivity is not None and candidate.sensitivity.value != str(sensitivity):
                continue
            if run_id is not None:
                job = self.processing_jobs.get(candidate.job_id)
                if job is None or job.run_id != run_id:
                    continue
            cursor_created_at = kwargs.get("cursor_created_at")
            cursor_id = kwargs.get("cursor_id")
            if isinstance(cursor_created_at, datetime) and isinstance(cursor_id, UUID):
                if not (
                    candidate.created_at < cursor_created_at
                    or (candidate.created_at == cursor_created_at and candidate.id < cursor_id)
                ):
                    continue
            values.append(candidate)
        return sorted(values, key=lambda item: (item.created_at, item.id), reverse=True)[:limit]

    async def get_candidate(self, issuer: str, subject: str, candidate_id: UUID) -> MemoryCandidate:
        return self._candidate_owned(issuer, subject, candidate_id)

    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None:
        matches = [
            candidate
            for candidate in self.candidates.values()
            if candidate.job_id == job_id
            and candidate.issuer == issuer
            and candidate.subject == subject
        ]
        return max(matches, key=lambda item: (item.created_at, item.id), default=None)

    async def _decide_candidate_owner(
        self,
        issuer: str,
        subject: str,
        candidate_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str | None,
        edit: Mapping[str, object] | None = None,
    ) -> MemoryCandidate:
        candidate = self._candidate_owned(issuer, subject, candidate_id)
        fingerprint = _fingerprint(
            "candidate.approve",
            {"candidate": str(candidate_id), "expected": expected_version, "edit": edit or {}},
        )
        command_key = f"candidate:{idempotency_key}" if idempotency_key else None
        prior = self._replay(issuer, subject, command_key, fingerprint)
        pending_command = False
        if prior is not None:
            assert isinstance(prior, MemoryCandidate)
            if prior.state is not CandidateState.RETRYABLE:
                if prior.state is CandidateState.ACCEPTED and prior.memory_id is not None:
                    record = self.memories.get(prior.memory_id)
                    if record is not None:
                        try:
                            await self._ensure_active_embedding_job(issuer, subject, record)
                        except Exception:
                            pass
                return prior
            pending_command = True
        if candidate.version != expected_version:
            raise MemoryVersionConflict("memory candidate version conflict")
        if candidate.state not in {CandidateState.PROPOSED, CandidateState.REVIEW} and not (
            pending_command and candidate.state is CandidateState.RETRYABLE
        ):
            raise MemoryVersionConflict("memory candidate is already decided")
        job = self.processing_jobs.get(candidate.job_id)
        if edit is not None:
            try:
                scope_value = edit["scope"]
                scope = (
                    scope_value
                    if isinstance(scope_value, MemoryScope)
                    else MemoryScope(
                        MemoryScopeType(str(cast(Mapping[str, object], scope_value)["type"])),
                        UUID(str(cast(Mapping[str, object], scope_value)["agentProfileId"]))
                        if cast(Mapping[str, object], scope_value).get("agentProfileId") is not None
                        else None,
                    )
                )
                candidate = replace(
                    candidate,
                    action=MemoryAction(str(edit["action"])),
                    content=str(edit["content"]),
                    kind=MemoryKind(str(edit["kind"])),
                    scope=scope,
                    confidence=_as_float(edit["confidence"], 0.0),
                    importance=_as_float(edit["importance"], 0.0),
                    half_life_days=_as_float(edit["halfLifeDays"], DEFAULT_MEMORY_HALF_LIFE_DAYS),
                    valid_to=cast(datetime | None, edit.get("validTo")),
                    related_memory_id=cast(UUID | None, edit.get("relatedMemoryId")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MemoryValidationError("memory candidate edit is invalid") from exc
        else:
            # ``review`` is a provider disposition, not an approval action.
            # Normalize legacy rows so an owner can approve grounded content
            # without having to rewrite the candidate first.
            candidate = _normalize_candidate_for_approval(candidate)
        if candidate.content is None or contains_secret(candidate.content):
            raise MemoryValidationError("credential-like candidate content is not accepted")
        if (
            classify_sensitivity(candidate.content) is MemorySensitivity.CREDENTIAL
            or candidate.sensitivity is MemorySensitivity.CREDENTIAL
        ):
            raise MemoryValidationError("credential-like candidate content is not accepted")
        scope = candidate.scope
        if scope is None:
            raise MemoryValidationError("memory candidate scope is required")
        if scope.type is MemoryScopeType.AGENT and scope.agent_profile_id is None:
            raise MemoryValidationError("agent candidate scope requires an agent profile")
        if (
            job is not None
            and scope.type is MemoryScopeType.AGENT
            and scope.agent_profile_id != job.agent_profile_id
        ):
            raise MemoryScopeAuthorizationRequired("candidate agent scope is not authorized")
        validate_revision(
            candidate.content,
            candidate.confidence,
            candidate.importance if candidate.importance is not None else DEFAULT_MEMORY_IMPORTANCE,
            candidate.half_life_days
            if candidate.half_life_days is not None
            else DEFAULT_MEMORY_HALF_LIFE_DAYS,
            None,
            candidate.valid_to,
        )
        if not pending_command and command_key is not None:
            claimed = replace(candidate, state=CandidateState.RETRYABLE)
            self.candidates[candidate_id] = claimed
            self._record_replay(issuer, subject, command_key, fingerprint, claimed)
        if edit is not None:
            if job is None:
                raise MemoryValidationError("edited memory evidence is unavailable")
            grounded = set(candidate.grounded_message_ids)
            if not grounded or not grounded.issubset(set(job.user_message_ids)):
                raise MemoryValidationError("edited memory candidate grounding is invalid")
            if (
                candidate.scope
                and candidate.scope.type is MemoryScopeType.AGENT
                and candidate.scope.agent_profile_id != job.agent_profile_id
            ):
                raise MemoryScopeAuthorizationRequired("edited agent scope is not authorized")
            if (
                candidate.scope
                and candidate.scope.type is MemoryScopeType.USER
                and not job.allow_shared_user_promotion
            ):
                raise MemoryScopeAuthorizationRequired("shared-user promotion is not authorized")
            conflicts = await self.list_memories(
                issuer,
                subject,
                MemoryFilters(
                    scope_type=None,
                    include_all_scopes=True,
                    include_historical=True,
                    limit=100000,
                ),
            )
            existing_conflict = any(
                item.content == candidate.content and item.scope != candidate.scope
                for item in conflicts
            )
            user_content: str | None = None
            evidence_loader = self._candidate_evidence_loader
            if evidence_loader is not None:
                evidence = await evidence_loader(job)
                user_content = getattr(evidence, "user_content", None)
                if not isinstance(user_content, str):
                    raise MemoryValidationError("edited memory evidence is unavailable")
            decision = decide_candidate(
                candidate,
                user_message_ids=frozenset(job.user_message_ids),
                run_agent_profile_id=job.agent_profile_id,
                existing_conflict=existing_conflict,
                user_content=user_content,
                allow_shared_user_promotion=job.allow_shared_user_promotion,
            )
            if decision.state is CandidateState.REJECTED:
                raise MemoryValidationError("edited memory candidate failed deterministic policy")
        provenance = [
            MemoryProvenance(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate-review:{candidate.id}"),
                "run",
                source_id=job.run_id if job is not None else None,
                run_id=job.run_id if job is not None else None,
                observed_at=self._now(),
            )
        ]
        if candidate.action in {MemoryAction.REINFORCE, MemoryAction.CREATE}:
            matches = await self.list_memories(
                issuer,
                subject,
                MemoryFilters(
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                    include_historical=True,
                    limit=100000,
                ),
            )
            duplicate = next((item for item in matches if item.content == candidate.content), None)
            if duplicate is not None:
                result = await self.reinforce_memory(
                    issuer,
                    subject,
                    duplicate.id,
                    provenance=provenance,
                    idempotency_key=f"memory-action:{candidate.id}",
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                candidate = replace(candidate, memory_id=result.id)
            else:
                result = await self.create_memory(
                    issuer,
                    subject,
                    content=candidate.content,
                    kind=candidate.kind or MemoryKind.SEMANTIC,
                    scope=scope,
                    confidence=candidate.confidence,
                    importance=(
                        candidate.importance
                        if candidate.importance is not None
                        else DEFAULT_MEMORY_IMPORTANCE
                    ),
                    half_life_days=(
                        candidate.half_life_days
                        if candidate.half_life_days is not None
                        else DEFAULT_MEMORY_HALF_LIFE_DAYS
                    ),
                    valid_to=candidate.valid_to,
                    provenance=provenance,
                    idempotency_key=f"memory-action:{candidate.id}",
                    _defer_embedding_queue=True,
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                candidate = replace(candidate, memory_id=result.id)
        elif candidate.action in {MemoryAction.DISPUTE, MemoryAction.SUPERSEDE}:
            if candidate.related_memory_id is None:
                raise MemoryValidationError("related memory is required")
            result = await self.set_status(
                issuer,
                subject,
                candidate.related_memory_id,
                status=(
                    MemoryLifecycleStatus.DISPUTED
                    if candidate.action is MemoryAction.DISPUTE
                    else MemoryLifecycleStatus.SUPERSEDED
                ),
                related_memory_id=None,
                expected_version=(
                    await self.get_memory(
                        issuer,
                        subject,
                        candidate.related_memory_id,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                ).version,
                idempotency_key=f"memory-action:{candidate.id}",
                scope_type=scope.type,
                agent_profile_id=scope.agent_profile_id,
            )
            candidate = replace(candidate, memory_id=result.id)
        else:
            raise MemoryValidationError("candidate action is not approvable")
        decided = replace(
            candidate,
            state=CandidateState.ACCEPTED,
            decision_reason="owner_approved",
            version=candidate.version + 1,
            decided_at=self._now(),
        )
        self.candidates[candidate_id] = decided
        self._record_replay(issuer, subject, command_key, fingerprint, decided)
        try:
            await self._ensure_active_embedding_job(issuer, subject, result)
        except Exception:
            # Approval is already durably accepted.  Queue repair is retried
            # by an idempotent approval replay and never invokes inference.
            pass
        return decided

    async def approve_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        return await self._decide_candidate_owner(
            issuer,
            subject,
            candidate_id,
            expected_version=_as_int(kwargs.get("expected_version"), 1),
            idempotency_key=str(kwargs.get("idempotency_key"))
            if kwargs.get("idempotency_key")
            else None,
            edit=cast(Mapping[str, object] | None, kwargs.get("edit")),
        )

    async def reject_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        candidate = self._candidate_owned(issuer, subject, candidate_id)
        expected = _as_int(kwargs.get("expected_version"), 1)
        key = str(kwargs.get("idempotency_key")) if kwargs.get("idempotency_key") else None
        reason = str(kwargs.get("reason", "owner_rejected"))
        validate_memory_text(reason, "rejection reason")
        fingerprint = _fingerprint(
            "candidate.reject",
            {"candidate": str(candidate_id), "expected": expected, "reason": reason[:64]},
        )
        command_key = f"candidate:{key}" if key else None
        prior = self._replay(issuer, subject, command_key, fingerprint)
        if prior is not None:
            assert isinstance(prior, MemoryCandidate)
            return prior
        if candidate.version != expected:
            raise MemoryVersionConflict("memory candidate version conflict")
        decided = _content_free_candidate(
            replace(
                candidate,
                state=CandidateState.REJECTED,
                decision_reason=reason[:64],
            )
        )
        decided = replace(
            decided,
            state=CandidateState.REJECTED,
            decision_reason=reason[:64],
            version=candidate.version + 1,
            decided_at=self._now(),
        )
        self.candidates[candidate_id] = decided
        self._record_replay(issuer, subject, command_key, fingerprint, decided)
        return decided


class MemoryStore(_MemoryStoreBase):
    async def list_embedding_generations(
        self, issuer: str, subject: str, *, status: str | None = None
    ) -> list[MemoryEmbeddingGeneration]:
        return sorted(
            [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and (status is None or item.status == status)
            ],
            key=lambda item: item.generation,
        )

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        item = self.embedding_generations.get(generation_id)
        if item is None or item.issuer != issuer or item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        return item

    async def list_memories(
        self, issuer: str, subject: str, filters: MemoryFilters | None = None
    ) -> list[MemoryRecord]:
        criteria = filters or MemoryFilters()
        results: list[MemoryRecord] = []
        for item in self.memories.values():
            if not _owner(item, issuer, subject):
                continue
            if criteria.kind and item.kind is not criteria.kind:
                continue
            if (
                criteria.scope_type
                and (
                    not criteria.include_all_scopes or criteria.scope_type is MemoryScopeType.AGENT
                )
                and item.scope.type is not criteria.scope_type
            ):
                continue
            if (
                criteria.agent_profile_id
                and item.scope.agent_profile_id != criteria.agent_profile_id
            ):
                continue
            if (
                item.scope.type is MemoryScopeType.AGENT
                and not criteria.include_all_scopes
                and criteria.authorized_agent_ids is not None
                and item.scope.agent_profile_id not in criteria.authorized_agent_ids
            ):
                continue
            if criteria.status and item.status is not criteria.status:
                continue
            if criteria.provenance_type and not any(
                source.source_type == criteria.provenance_type for source in item.provenance
            ):
                continue
            if (
                criteria.confidence_min is not None
                and item.current_revision.confidence < criteria.confidence_min
            ):
                continue
            if (
                criteria.confidence_max is not None
                and item.current_revision.confidence > criteria.confidence_max
            ):
                continue
            if criteria.created_from is not None and item.created_at < criteria.created_from:
                continue
            if criteria.created_to is not None and item.created_at >= criteria.created_to:
                continue
            if criteria.q and criteria.q.casefold() not in item.content.casefold():
                continue
            if not criteria.include_historical and item.status in {
                MemoryLifecycleStatus.DORMANT,
                MemoryLifecycleStatus.ARCHIVED,
                MemoryLifecycleStatus.DISABLED,
                MemoryLifecycleStatus.DISPUTED,
                MemoryLifecycleStatus.SUPERSEDED,
            }:
                continue
            results.append(item)
        results.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        if criteria.cursor_updated_at is not None and criteria.cursor_id is not None:
            results = [
                item
                for item in results
                if (item.updated_at, item.id) < (criteria.cursor_updated_at, criteria.cursor_id)
            ]
        return results[: max(1, min(criteria.limit, 100_000))]

    async def get_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        return self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )

    def _purge_tombstone(self, issuer: str, subject: str, key: object) -> None:
        if key and (issuer, subject, str(key)) in self._purge_tombstones:
            raise MemoryPurgeReplayNotFound("memory not found")

    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        # Validate every supplied authorization selector before any owner or
        # repository lookup. Malformed non-null context must fail closed even
        # when the selected scope is user-scoped.
        _scope_type(kwargs.get("scope_type"))
        _agent_id(kwargs.get("agent_profile_id"))
        _authorized_ids(kwargs.get("authorized_agent_ids"))
        kind = MemoryKind(str(kwargs["kind"]))
        scope = kwargs["scope"]
        if not isinstance(scope, MemoryScope):
            try:
                scope = MemoryScope(
                    MemoryScopeType(str(scope)), _agent_id(kwargs.get("agent_profile_id"))
                )
            except (TypeError, ValueError) as exc:
                raise MemoryValidationError("memory scope is invalid") from exc
        if scope.type is MemoryScopeType.AGENT:
            authorized = _authorized_ids(kwargs.get("authorized_agent_ids"))
            if authorized is not None and scope.agent_profile_id not in authorized:
                raise MemoryScopeAuthorizationRequired("agent scope is not authorized")
        content = str(kwargs["content"])
        confidence = _as_float(kwargs.get("confidence"), 1.0)
        importance = _as_float(kwargs.get("importance"), 0.5)
        half_life = _as_float(kwargs.get("half_life_days"), 30.0)
        valid_from = kwargs.get("valid_from")
        valid_to = kwargs.get("valid_to")
        validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint("create", _command_values(kwargs))
        prior = self._replay(issuer, subject, str(key) if key else None, fingerprint)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            await self._ensure_active_embedding_job(issuer, subject, prior)
            return prior
        now = self._now()
        memory_id = UUID(str(kwargs["memory_id"])) if kwargs.get("memory_id") else uuid4()
        self._assert_not_fenced(issuer, subject, memory_id)
        revision_id = uuid4()
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", [])))
        for item in provenance:
            validate_provenance(item)
        revision = MemoryRevision(
            revision_id,
            memory_id,
            1,
            kind,
            content,
            cast(datetime, kwargs.get("observed_at")) if kwargs.get("observed_at") else now,
            now,
            confidence,
            importance,
            half_life,
            cast(datetime | None, valid_from),
            cast(datetime | None, valid_to),
            tuple(item.id for item in provenance),
        )  # type: ignore[arg-type]
        record = MemoryRecord(
            memory_id,
            issuer,
            subject,
            kind,
            scope,
            MemoryLifecycleStatus.ACTIVE,
            bool(kwargs.get("pinned", False)),
            1,
            revision_id,
            [revision],
            provenance,
            list(cast(Iterable[MemoryEmbedding], kwargs.get("embeddings", []))),
            tuple(cast(Iterable[UUID], kwargs.get("related_memory_ids", ()))),
            [],
            now,
            None,
            None,
            now,
            now,
        )
        self.memories[memory_id] = record
        generation = await self.get_active_embedding_generation(issuer, subject)
        await self._stage_active_embedding_job(issuer, subject, record)
        if (
            not kwargs.get("_defer_embedding_queue")
            and self._embedding_queue_boundary is not None
            and generation is not None
        ):
            await self._embedding_queue_boundary(
                issuer,
                subject,
                memory_id=record.id,
                revision_id=record.current_revision_id,
                generation_id=generation.id,
            )
        self._record_replay(issuer, subject, str(key) if key else None, fingerprint, record)
        return record

    async def reinforce_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        key = kwargs.get("idempotency_key")
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", ())))
        fp = _fingerprint(
            "reinforce",
            {
                "memory_id": str(memory_id),
                "provenance": [str(item.id) for item in provenance],
                **(
                    {
                        "expected_version": kwargs.get("expected_version"),
                        "require_active": kwargs.get("require_active") is True,
                    }
                    if kwargs.get("expected_version") is not None
                    or kwargs.get("require_active") is True
                    else {}
                ),
            },
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if (
            kwargs.get("require_active") is True
            and record.status is not MemoryLifecycleStatus.ACTIVE
        ):
            raise MemoryNotFound("memory is no longer active")
        expected_version = kwargs.get("expected_version")
        if expected_version is not None and record.version != _as_int(expected_version):
            raise MemoryVersionConflict("memory version conflict")
        for item in provenance:
            validate_provenance(item)
        existing_provenance = {item.id for item in record.provenance}
        record.provenance.extend(item for item in provenance if item.id not in existing_provenance)
        record.reinforced_at = self._now()
        record.status = MemoryLifecycleStatus.ACTIVE
        record.dormant_at = None
        record.updated_at = self._now()
        record.version += 1
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def revise_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        expected = _as_int(kwargs.get("expected_version", kwargs.get("expectedVersion")))
        content = str(kwargs["content"])
        reason = kwargs.get("reason")
        if reason is not None:
            validate_memory_text(str(reason), "correction reason")
        current = record.current_revision
        confidence = _as_float(kwargs.get("confidence"), current.confidence)
        importance = _as_float(kwargs.get("importance"), current.importance)
        half_life = _as_float(kwargs.get("half_life_days"), current.half_life_days)
        valid_from = (
            current.valid_from
            if kwargs.get("valid_from") is None
            else cast(datetime | None, kwargs["valid_from"])
        )
        valid_to = (
            None
            if kwargs.get("clear_valid_to") is True
            else current.valid_to
            if kwargs.get("valid_to") is None
            else cast(datetime | None, kwargs["valid_to"])
        )
        validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("revise", _command_values(kwargs) | {"memory_id": str(memory_id)})
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        now = self._now()
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", [])))
        for item in provenance:
            validate_provenance(item)
        kind = MemoryKind(str(kwargs["kind"])) if kwargs.get("kind") is not None else record.kind
        revision = MemoryRevision(
            uuid4(),
            memory_id,
            len(record.revisions) + 1,
            kind,
            content,
            cast(datetime, kwargs.get("observed_at")) if kwargs.get("observed_at") else now,
            now,
            confidence,
            importance,
            half_life,
            valid_from,
            valid_to,
            tuple(item.id for item in provenance),
            str(reason) if reason is not None else None,
        )  # type: ignore[arg-type]
        record.revisions.append(revision)
        record.provenance.extend(provenance)
        record.current_revision_id = revision.id
        record.kind = kind
        record.version += 1
        record.reinforced_at = now
        record.updated_at = now
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def set_status(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        expected = _as_int(kwargs.get("expected_version", kwargs.get("expectedVersion")))
        status = MemoryLifecycleStatus(str(kwargs["status"]))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "status",
            {
                "memory": str(memory_id),
                "status": status.value,
                "expected": expected,
                "related": kwargs.get("related_memory_id"),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        related = kwargs.get("related_memory_id")
        relation = (
            "disputes"
            if status is MemoryLifecycleStatus.DISPUTED
            else "supersedes"
            if status is MemoryLifecycleStatus.SUPERSEDED
            else None
        )
        related_record: MemoryRecord | None = None
        if isinstance(related, UUID) and relation is not None:
            try:
                if related == memory_id:
                    raise MemoryNotFound("related memory not found")
                related_record = self.memories.get(related)
                if related_record is None or not _owner(related_record, issuer, subject):
                    raise MemoryNotFound("related memory not found")
                self._find(
                    issuer,
                    subject,
                    related,
                    scope_type=related_record.scope.type,
                    agent_profile_id=related_record.scope.agent_profile_id,
                    authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
                )
                if related_record.scope != record.scope:
                    raise MemoryNotFound("related memory not found")
            except MemoryNotFound as exc:
                raise MemoryNotFound("related memory not found") from exc
        now = self._now()
        record.status = status
        record.version += 1
        record.updated_at = now
        if status is MemoryLifecycleStatus.DORMANT:
            record.dormant_at = now
        if status is MemoryLifecycleStatus.ARCHIVED:
            record.archived_at = now
        if related_record is not None and relation is not None:
            record.relations.append(MemoryRelation(cast(UUID, related), relation, now))
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def set_pinned(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        expected = _as_int(kwargs.get("expected_version", kwargs.get("expectedVersion")))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "pin",
            {
                "memory": str(memory_id),
                "expected": expected,
                "pinned": bool(kwargs["pinned"]),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        record.pinned = bool(kwargs["pinned"])
        record.version += 1
        record.updated_at = self._now()
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def purge(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryAuditRecord:
        if kwargs.get("confirmation") != PURGE_CONFIRMATION:
            raise MemoryPurgeConfirmationRequired("exact purge confirmation is required")
        expected = _as_int(kwargs.get("expected_version", kwargs.get("expectedVersion")))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "purge",
            {
                "memory": str(memory_id),
                "expected": expected,
                "confirmation": kwargs.get("confirmation"),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        self._purge_tombstone(issuer, subject, key)
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryAuditRecord)
            return prior
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        audit = MemoryAuditRecord(uuid4(), issuer, subject, memory_id, "purge", self._now())
        candidate_ids = {
            candidate.id
            for candidate in self.candidates.values()
            if candidate.issuer == issuer
            and candidate.subject == subject
            and (candidate.memory_id == memory_id or candidate.related_memory_id == memory_id)
        }
        job_ids = {
            job.id
            for job in self.processing_jobs.values()
            if job.issuer == issuer and job.subject == subject and job.memory_id == memory_id
        }
        # Derive deterministic action links before scrubbing the memory.  This
        # covers a crash after create/reinforce committed but before candidate
        # or job linkage was written.
        for (id_issuer, id_subject, idempotency_key), (_, value) in tuple(
            self._idempotency.items()
        ):
            if (id_issuer, id_subject) != (issuer, subject):
                continue
            if isinstance(value, MemoryRecord) and value.id == memory_id:
                if idempotency_key.startswith("memory-job:"):
                    try:
                        job_ids.add(UUID(idempotency_key.removeprefix("memory-job:")))
                    except ValueError:
                        pass
                elif idempotency_key.startswith("memory-action:"):
                    try:
                        candidate_id = UUID(idempotency_key.removeprefix("memory-action:"))
                    except ValueError:
                        continue
                    candidate_ids.add(candidate_id)
                    candidate = self.candidates.get(candidate_id)
                    if candidate is not None:
                        job_ids.add(candidate.job_id)
                elif idempotency_key.startswith("memory-fallback:"):
                    for candidate in self.candidates.values():
                        if (
                            candidate.issuer == issuer
                            and candidate.subject == subject
                            and candidate.content == value.content
                            and (
                                candidate.decision_reason == "provider_ignore_deterministic_create"
                                or (candidate.decision_reason or "").startswith(
                                    "provider_ignore_deterministic_create@"
                                )
                            )
                        ):
                            candidate_ids.add(candidate.id)
                            job_ids.add(candidate.job_id)
        job_ids.update(
            candidate.job_id
            for candidate in self.candidates.values()
            if candidate.id in candidate_ids
        )
        job_ids.update(
            UUID(str(outcome["job_id"]))
            for outcome in self.outcomes
            if outcome.get("issuer", issuer) == issuer
            and outcome.get("subject", subject) == subject
            and outcome.get("memory_id") == memory_id
            and outcome.get("job_id") is not None
        )
        self._purge_fences.add((issuer, subject, memory_id))
        for job_id, job in tuple(self.processing_jobs.items()):
            if job_id in job_ids:
                self.processing_jobs[job_id] = replace(
                    job,
                    status=ProcessingJobStatus.FAILED,
                    last_error_class="purged",
                    lease_id=None,
                    lease_until=None,
                    user_message_ids=(),
                    assistant_message_ids=(),
                    evidence_digest=None,
                    memory_id=None,
                )
        self.candidates = {
            candidate_id: candidate
            for candidate_id, candidate in self.candidates.items()
            if candidate_id not in candidate_ids and candidate.job_id not in job_ids
        }
        self.outcomes = [
            outcome
            for outcome in self.outcomes
            if outcome.get("memory_id") != memory_id and outcome.get("job_id") not in job_ids
        ]
        for embedding_id, embedding_job in tuple(self.embedding_jobs.items()):
            if (
                embedding_job.issuer == issuer
                and embedding_job.subject == subject
                and embedding_job.memory_id == memory_id
            ):
                del self.embedding_jobs[embedding_id]
        del self.memories[memory_id]
        stale_keys: list[tuple[str, str, str]] = []
        for key, value in self._idempotency.items():
            if key[:2] != (issuer, subject):
                continue
            if isinstance(value[1], MemoryRecord) and value[1].id == memory_id:
                stale_keys.append(key)
                continue
            if key[2].startswith("memory-job:"):
                try:
                    if UUID(key[2].removeprefix("memory-job:")) in job_ids:
                        stale_keys.append(key)
                except ValueError:
                    continue
        for stale_key in stale_keys:
            del self._idempotency[stale_key]
            self._purge_tombstones.add(stale_key)
        self.purge_audit.append(audit)
        self._record_replay(issuer, subject, str(key) if key else None, fp, audit)
        return audit

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None:
        try:
            job = await self.get_processing_job(job_id, issuer, subject)
        except MemoryNotFound:
            # Direct in-memory application calls may process an already
            # materialized job without staging the worker row first. Durable
            # repositories always stage it and therefore enforce the link.
            return
        self._assert_not_fenced(issuer, subject, memory_id)
        self.processing_jobs[job_id] = replace(job, memory_id=memory_id)

    async def persist_candidate(
        self, candidate: MemoryCandidate, **kwargs: object
    ) -> MemoryCandidate:
        job = await self.get_processing_job(candidate.job_id, candidate.issuer, candidate.subject)
        del job
        expected_version = kwargs.get("expected_version")
        existing = self.candidates.get(candidate.id)
        if existing is not None and expected_version is not None:
            if existing.version != _as_int(expected_version):
                raise MemoryVersionConflict("memory candidate version conflict")
        self.candidates[candidate.id] = candidate
        idempotency_key = kwargs.get("idempotency_key")
        fingerprint = kwargs.get("fingerprint")
        if idempotency_key and isinstance(fingerprint, str):
            self._record_replay(
                candidate.issuer,
                candidate.subject,
                f"candidate:{idempotency_key}",
                fingerprint,
                candidate,
            )
        return candidate

    async def record_action_outcome(
        self,
        *,
        candidate_id: UUID | None,
        job_id: UUID,
        issuer: str,
        subject: str,
        action: str,
        outcome: str,
        memory_id: UUID | None = None,
        revision_id: UUID | None = None,
        error_class: str | None = None,
    ) -> None:
        await self.get_processing_job(job_id, issuer, subject)
        outcome_record: dict[str, object] = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "issuer": issuer,
            "subject": subject,
            "action": action,
            "outcome": outcome,
        }
        if memory_id is not None:
            outcome_record["memory_id"] = memory_id
        if revision_id is not None:
            outcome_record["revision_id"] = revision_id
        if error_class is not None:
            outcome_record["error_class"] = error_class
        self.outcomes.append(outcome_record)

    async def register_embedding_generation(
        self, issuer: str, subject: str, **kwargs: object
    ) -> MemoryEmbeddingGeneration:
        now = self._now()
        generation = _as_int(kwargs.get("generation"))
        model_id = str(kwargs["model_id"])
        dimension = _as_int(kwargs.get("dimension"))
        if dimension < 1 or not model_id:
            raise MemoryValidationError("embedding model and dimension are required")
        digest = kwargs.get("model_digest")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise MemoryValidationError("embedding model digest must be sha256")
        if any(
            item.generation == generation and item.issuer == issuer and item.subject == subject
            for item in self.embedding_generations.values()
        ):
            raise MemoryValidationError("embedding generation number already exists")
        item = MemoryEmbeddingGeneration(
            uuid4(),
            generation,
            model_id,
            cast(str | None, kwargs.get("model_revision")),
            dimension,
            "building",
            now,
            None,
            str(digest) if digest else None,
            issuer,
            subject,
        )
        self.embedding_generations[item.id] = item
        return item

    async def activate_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        try:
            item = self.embedding_generations[generation_id]
        except KeyError as exc:
            raise MemoryNotFound("embedding generation not found") from exc
        if item.issuer and item.issuer != issuer or item.subject and item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if item.status != "building":
            raise MemoryValidationError("only a building embedding generation can be activated")
        retained = [
            revision.id
            for record in self.memories.values()
            if _owner(record, issuer, subject)
            for revision in record.revisions
        ]
        embedded = {
            embedding.revision_id
            for record in self.memories.values()
            if _owner(record, issuer, subject)
            for embedding in record.embeddings
            if embedding.generation_id == generation_id
        }
        if set(retained) - embedded:
            raise MemoryValidationError("embedding generation is incomplete")
        configuration = self.model_configurations.get((issuer, subject))
        if (
            configuration is not None
            and configuration.embedding_generation != generation_id
            and (
                item.model_id != configuration.embedding_model_id
                or item.model_revision != configuration.embedding_model_revision
            )
        ):
            raise MemoryValidationError("embedding generation does not match configured target")
        if configuration is not None:
            previous_id = configuration.embedding_generation
            if isinstance(previous_id, UUID) and previous_id != generation_id:
                previous = self.embedding_generations.get(previous_id)
                if (
                    previous is not None
                    and previous.issuer == issuer
                    and previous.subject == subject
                ):
                    previous.status = "retired"
            self.model_configurations[(issuer, subject)] = replace(
                configuration,
                embedding_generation=generation_id,
                version=configuration.version + 1,
            )
        item.status = "active"
        item.activated_at = self._now()
        activated = item
        self.embedding_generations[generation_id] = item
        for record in self.memories.values():
            record.embedding_generations = [
                activated if generation.id == generation_id else generation
                for generation in record.embedding_generations
            ]
        return activated

    async def mark_embedding_generation_failed(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        item = self.embedding_generations.get(generation_id)
        if item is None or item.issuer != issuer or item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if item.status != "building":
            raise MemoryValidationError("only a building embedding generation can fail")
        item.status = "failed"
        return item

    async def attach_embedding(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        self._assert_not_fenced(issuer, subject, memory_id)
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=_scope_type(kwargs.get("scope_type")),
            agent_profile_id=_agent_id(kwargs.get("agent_profile_id")),
            authorized_agent_ids=_authorized_ids(kwargs.get("authorized_agent_ids")),
        )
        revision_id = cast(UUID, kwargs.get("revision_id", record.current_revision_id))
        if revision_id not in {item.id for item in record.revisions}:
            raise MemoryNotFound("memory revision not found")
        generation_id = cast(UUID, kwargs["generation_id"])
        generation = self.embedding_generations.get(generation_id)
        if generation is None:
            raise MemoryNotFound("embedding generation not found")
        if generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        vector = tuple(
            float(cast(float | int | str, value))
            for value in cast(Iterable[object], kwargs["vector"])
        )
        if len(vector) != generation.dimension or not all(math.isfinite(value) for value in vector):
            raise MemoryValidationError("embedding dimension or values do not match generation")
        digest = str(kwargs.get("digest", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MemoryValidationError("embedding digest must be sha256")
        if not {"model_id", "model_revision", "model_digest"} <= kwargs.keys():
            raise MemoryValidationError("observed embedding identity is required")
        model_id = str(kwargs["model_id"])
        model_revision = cast(str | None, kwargs["model_revision"])
        model_digest = cast(str | None, kwargs["model_digest"])
        if (
            model_id != generation.model_id
            or model_revision != generation.model_revision
            or model_digest != generation.model_digest
        ):
            raise MemoryValidationError("embedding provider identity does not match generation")
        if any(
            item.revision_id == revision_id and item.generation_id == generation.id
            for item in record.embeddings
        ):
            raise MemoryValidationError("embedding already exists for revision and generation")
        record.embeddings.append(
            MemoryEmbedding(
                uuid4(),
                revision_id,
                generation.generation,
                model_id,
                model_revision,
                generation.dimension,
                digest,
                self._now(),
                vector,
                generation.id,
                model_digest,
            )
        )
        if all(item.id != generation.id for item in record.embedding_generations):
            record.embedding_generations.append(generation)
        return record

    async def save_model_configuration(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object
    ) -> MemoryModelConfiguration:
        if configuration.issuer != issuer or configuration.subject != subject:
            raise MemoryScopeAuthorizationRequired("model configuration owner mismatch")
        expected = kwargs.get("expected_version")
        prior = self.model_configurations.get((issuer, subject))
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint(
            "model.configuration",
            {
                "extraction": configuration.extraction_model_id,
                "embedding": configuration.embedding_model_id,
                "expectedVersion": expected,
                "dimension": kwargs.get("dimension"),
                "modelDigest": kwargs.get("model_digest"),
            },
        )
        command_key = f"model:{key}" if key else None
        replay = self._replay(issuer, subject, command_key, fingerprint)
        if replay is not None:
            if not isinstance(replay, MemoryModelConfiguration):
                raise MemoryIdempotencyConflict("idempotency key is unavailable")
            return replay
        if prior is None and expected is not None and _as_int(expected) != 1:
            raise MemoryVersionConflict("model configuration version conflict")
        if expected is not None and prior is not None and prior.version != _as_int(expected):
            raise MemoryVersionConflict("model configuration version conflict")
        selected = configuration.embedding_generation
        if selected is not None:
            generation = self.embedding_generations.get(selected)
            if generation is None or generation.issuer != issuer or generation.subject != subject:
                raise MemoryNotFound("embedding generation not found")
        if prior is not None and (
            prior.embedding_model_id != configuration.embedding_model_id
            or prior.embedding_model_revision != configuration.embedding_model_revision
        ):
            # A newer model target supersedes unfinished building generations;
            # retain them as audit metadata but never let them starve the
            # current resumable cutover.
            for existing_generation in self.embedding_generations.values():
                if (
                    existing_generation.issuer == issuer
                    and existing_generation.subject == subject
                    and existing_generation.status == "building"
                ):
                    existing_generation.status = "retired"
            previous = (
                self.embedding_generations.get(prior.embedding_generation)
                if prior.embedding_generation
                else None
            )
            dimension = _as_int(kwargs.get("dimension"), previous.dimension if previous else 0)
            if dimension > 0:
                next_number = (
                    max(
                        (
                            item.generation
                            for item in self.embedding_generations.values()
                            if item.issuer == issuer and item.subject == subject
                        ),
                        default=0,
                    )
                    + 1
                )
                generation_id = uuid4()
                self.embedding_generations[generation_id] = MemoryEmbeddingGeneration(
                    generation_id,
                    next_number,
                    configuration.embedding_model_id,
                    configuration.embedding_model_revision,
                    dimension,
                    "building",
                    self._now(),
                    None,
                    str(kwargs["model_digest"]) if kwargs.get("model_digest") else None,
                    issuer,
                    subject,
                )
                selected = prior.embedding_generation
        saved = replace(configuration, embedding_generation=selected)
        self.model_configurations[(issuer, subject)] = saved
        self._record_replay(issuer, subject, command_key, fingerprint, saved)
        return saved

    async def get_model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        try:
            configuration = self.model_configurations[(issuer, subject)]
        except KeyError as exc:
            raise MemoryNotFound("memory model configuration not found") from exc
        if configuration.embedding_generation is None:
            matches = [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and item.status == "active"
                and item.model_id == configuration.embedding_model_id
                and item.model_revision == configuration.embedding_model_revision
            ]
            if len(matches) == 1:
                configuration = replace(
                    configuration,
                    embedding_generation=matches[0].id,
                    version=configuration.version + 1,
                )
                self.model_configurations[(issuer, subject)] = configuration
        return configuration

    async def queue_embedding_job(
        self, issuer: str, subject: str, *, memory_id: UUID, revision_id: UUID, generation_id: UUID
    ) -> MemoryEmbeddingJob:
        self._assert_not_fenced(issuer, subject, memory_id)
        record = self.memories.get(memory_id)
        if record is None or not _owner(record, issuer, subject):
            raise MemoryNotFound("memory not found")
        if revision_id not in {item.id for item in record.revisions}:
            raise MemoryNotFound("memory revision not found")
        generation = self.embedding_generations.get(generation_id)
        if generation is None or generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        job_id = uuid5(
            MEMORY_ID_NAMESPACE, f"embedding-job:{issuer}:{subject}:{revision_id}:{generation_id}"
        )
        existing = self.embedding_jobs.get(job_id)
        if existing is not None:
            # Queue/replay is not a lease capability; the current lease, if
            # any, belongs to the worker that claimed the durable row.
            return replace(existing, lease_id=None, lease_until=None)
        item = MemoryEmbeddingJob(
            job_id, issuer, subject, memory_id, revision_id, generation_id, available_at=self._now()
        )
        self.embedding_jobs[job_id] = item
        return item

    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob:
        if job.memory_id is not None:
            self._assert_not_fenced(job.issuer, job.subject, job.memory_id)
        existing = self.processing_jobs.get(job.id)
        if existing is not None:
            if (existing.issuer, existing.subject) != (job.issuer, job.subject):
                raise MemoryNotFound("memory processing job not found")
            return existing
        self.processing_jobs[job.id] = job
        return job

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob:
        job = self.processing_jobs.get(job_id)
        if job is None or (job.issuer, job.subject) != (issuer, subject):
            raise MemoryNotFound("memory processing job not found")
        return job

    async def is_processing_command_purged(
        self, issuer: str, subject: str, command_id: UUID
    ) -> bool:
        """Return whether purge fenced this owner's processing command."""

        return (issuer, subject, f"memory-job:{command_id}") in self._purge_tombstones

    async def claim_processing_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryProcessingJob | None:
        job = await self.get_processing_job(job_id, issuer, subject)
        now = self._now()
        if job.available_at > now:
            return None
        if job.status not in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE} and not (
            job.status is ProcessingJobStatus.RUNNING
            and job.lease_until is not None
            and job.lease_until <= now
        ):
            return None
        claimed = replace(
            job,
            status=ProcessingJobStatus.RUNNING,
            attempt_count=job.attempt_count + 1,
            lease_id=uuid4(),
            lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
        )
        self.processing_jobs[job_id] = claimed
        return claimed

    async def settle_processing_job(
        self,
        job_id: UUID,
        lease_id: UUID,
        *,
        issuer: str,
        subject: str,
        retryable: bool = False,
        error_class: str | None = None,
    ) -> MemoryProcessingJob:
        job = await self.get_processing_job(job_id, issuer, subject)
        if job.status is not ProcessingJobStatus.RUNNING or job.lease_id != lease_id:
            raise MemoryValidationError("processing job lease is stale")
        settled = replace(
            job,
            status=ProcessingJobStatus.RETRYABLE if retryable else ProcessingJobStatus.COMPLETED,
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        if retryable:
            settled = replace(
                settled,
                available_at=self._now()
                + timedelta(seconds=min(3600, 2 ** min(job.attempt_count, 10))),
            )
        self.processing_jobs[job_id] = settled
        return settled

    async def claim_embedding_job(
        self, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        now = self._now()
        for item in sorted(self.embedding_jobs.values(), key=lambda value: value.available_at):
            if item.issuer != issuer or item.subject != subject or item.available_at > now:
                continue
            generation = self.embedding_generations.get(item.generation_id)
            if generation is None or generation.status != "active":
                continue
            if item.status not in {
                ProcessingJobStatus.QUEUED,
                ProcessingJobStatus.RETRYABLE,
            } and not (
                item.status is ProcessingJobStatus.RUNNING
                and item.lease_until
                and item.lease_until <= now
            ):
                continue
            claimed = replace(
                item,
                status=ProcessingJobStatus.RUNNING,
                attempt_count=item.attempt_count + 1,
                lease_id=uuid4(),
                lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
            )
            self.embedding_jobs[item.id] = claimed
            return claimed
        return None

    async def settle_embedding_job(
        self,
        job_id: UUID,
        *,
        issuer: str,
        subject: str,
        lease_id: UUID,
        retryable: bool = False,
        failed: bool = False,
        error_class: str | None = None,
    ) -> MemoryEmbeddingJob:
        item = self.embedding_jobs.get(job_id)
        if item is None or (issuer, subject) != (item.issuer, item.subject):
            raise MemoryNotFound("embedding job not found")
        if item.status is not ProcessingJobStatus.RUNNING or item.lease_id != lease_id:
            raise MemoryValidationError("embedding job lease is stale")
        provider_retry = failed and error_class == "provider"
        settled = replace(
            item,
            status=ProcessingJobStatus.FAILED
            if failed and not provider_retry
            else (
                ProcessingJobStatus.RETRYABLE
                if retryable or provider_retry
                else ProcessingJobStatus.COMPLETED
            ),
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        if retryable or provider_retry:
            settled = replace(
                settled,
                available_at=self._now()
                + timedelta(seconds=min(3600, 2 ** min(item.attempt_count, 10))),
            )
        self.embedding_jobs[job_id] = settled
        return settled

    async def claim_embedding_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        item = self.embedding_jobs.get(job_id)
        now = self._now()
        if (
            item is None
            or (item.issuer, item.subject) != (issuer, subject)
            or item.available_at > now
        ):
            return None
        if item.status not in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE} and not (
            item.status is ProcessingJobStatus.RUNNING
            and item.lease_until is not None
            and item.lease_until <= now
        ):
            return None
        self._assert_not_fenced(issuer, subject, item.memory_id)
        claimed = replace(
            item,
            status=ProcessingJobStatus.RUNNING,
            attempt_count=item.attempt_count + 1,
            lease_id=uuid4(),
            lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
        )
        self.embedding_jobs[job_id] = claimed
        return claimed

    async def claim_embedding_job_for_revision(
        self,
        issuer: str,
        subject: str,
        *,
        revision_id: UUID,
        generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None:
        now = self._now()
        for item in sorted(self.embedding_jobs.values(), key=lambda value: value.available_at):
            if (
                item.issuer != issuer
                or item.subject != subject
                or item.revision_id != revision_id
                or item.generation_id != generation_id
                or item.available_at > now
            ):
                continue
            if item.status not in {
                ProcessingJobStatus.QUEUED,
                ProcessingJobStatus.RETRYABLE,
            } and not (
                item.status is ProcessingJobStatus.RUNNING
                and item.lease_until is not None
                and item.lease_until <= now
            ):
                continue
            self._assert_not_fenced(issuer, subject, item.memory_id)
            claimed = replace(
                item,
                status=ProcessingJobStatus.RUNNING,
                attempt_count=item.attempt_count + 1,
                lease_id=uuid4(),
                lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
            )
            self.embedding_jobs[item.id] = claimed
            return claimed
        return None
