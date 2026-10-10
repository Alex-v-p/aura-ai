"""Memory extraction, maintenance, and reindex application services."""

from __future__ import annotations

import hashlib
import math
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid5

from aura_core.domains.knowledge.memory.candidate_actions import (
    MemoryCandidateExecutionService,
    action_from_provider,
)
from aura_core.domains.knowledge.memory.contracts import (
    MEMORY_ACTION_SCHEMA,
    MEMORY_EXTRACTION_POLICY_VERSION,
    MEMORY_ID_NAMESPACE,
    CandidateDecision,
    CandidateState,
    MemoryAction,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingSettlement,
    MemoryProvenance,
    MemoryRetentionBasis,
    MemoryScope,
    MemoryScopeType,
    MemorySensitivity,
    MemoryTurnEvidence,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
    classify_retention_basis,
    classify_sensitivity,
    contains_secret,
    content_free_candidate,
    decide_candidate,
    deterministic_create_idempotency_key,
    deterministic_fallback_kind,
    fallback_reinforcement_marker,
    fallback_reinforcement_target_marker,
    normalize_retention_horizon,
    user_family_relation_event,
)
from aura_core.domains.knowledge.memory.embedding import MemoryEmbeddingJobCoordinator
from aura_core.domains.knowledge.memory.maintenance import MemoryMaintenanceService
from aura_core.domains.knowledge.memory.ports import MemoryTelemetry
from aura_core.domains.knowledge.memory.repository_ports import (
    MemoryProcessingRepository,
)
from aura_core.runtime.models.ports import (
    EmbeddingPort,
    EmbeddingResult,
    ProviderTraceContext,
    StructuredInferencePort,
    StructuredInferenceRequest,
)

_content_free_candidate = content_free_candidate
_deterministic_create_idempotency_key = deterministic_create_idempotency_key
_FALLBACK_REINFORCEMENT_MARKER = fallback_reinforcement_marker
_FALLBACK_REINFORCEMENT_TARGET_MARKER = fallback_reinforcement_target_marker
_USER_FAMILY_RELATION_EVENT = user_family_relation_event


class MemoryProcessingService:
    """Worker-side orchestration for extraction, acceptance, and embedding.

    The service accepts identifier-only job metadata and receives turn content
    from the owner-scoped conversation application port.  Providers are
    injected through the runtime ports; provider payloads never cross this
    boundary.
    """

    def __init__(
        self,
        repository: MemoryProcessingRepository,
        inference: StructuredInferencePort,
        embedder: EmbeddingPort,
        *,
        clock: Callable[[], datetime] | None = None,
        evidence_loader: Callable[
            [MemoryProcessingJob], Awaitable[MemoryTurnEvidence | tuple[str, str]]
        ]
        | None = None,
        job_loader: Callable[[UUID], Awaitable[MemoryProcessingJob]] | None = None,
        telemetry: MemoryTelemetry | None = None,
        maintenance_telemetry: MemoryTelemetry | None = None,
    ) -> None:
        self.repository = repository
        self.inference = inference
        self.embedder = embedder
        self._clock = clock or (lambda: datetime.now(UTC))
        self._evidence_loader = evidence_loader
        self._job_loader = job_loader
        self._telemetry = telemetry
        self._maintenance_telemetry = maintenance_telemetry
        self.jobs: dict[tuple[str, str, UUID], MemoryProcessingJob] = {}
        self.candidates: dict[UUID, MemoryCandidate] = {}
        self.outcomes: list[dict[str, object]] = []
        self.configurations: dict[tuple[str, str], MemoryModelConfiguration] = {}
        self._maintenance = MemoryMaintenanceService(
            self.repository,
            clock=self._clock,
            transition_telemetry=self._emit_maintenance_transition,
        )
        self._candidate_execution = MemoryCandidateExecutionService(self.repository)
        self._embedding_jobs = MemoryEmbeddingJobCoordinator(self.repository)

    def _emit(
        self,
        operation: str,
        started: float,
        *,
        trace_id: str,
        outcome: str,
        error_class: str | None = None,
        memory_id: UUID | None = None,
        revision_id: UUID | None = None,
        generation_id: UUID | None = None,
        attempt_count: int | None = None,
        backlog: int | None = None,
        progress: float | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            normalized_outcome = {
                "received": "ok",
                "parked": "retryable",
            }.get(outcome, outcome)
            self._telemetry(
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000),
                trace_id=trace_id,
                outcome=normalized_outcome,
                error_class=error_class,
                memory_id=str(memory_id) if memory_id else None,
                memory_revision_id=str(revision_id) if revision_id else None,
                generation_id=str(generation_id) if generation_id else None,
                attempt_count=attempt_count,
                backlog=backlog,
                progress=progress,
            )
        except Exception:
            # Telemetry is explicitly non-blocking and cannot alter durable
            # processing state or provider retry behavior.
            return

    def _emit_maintenance_transition(
        self,
        started: float,
        *,
        trace_id: str,
        memory_id: UUID | None,
        destination_status: MemoryLifecycleStatus | None = None,
    ) -> None:
        """Route one lifecycle transition to the maintenance telemetry sink."""

        operation = (
            "memory.decay"
            if destination_status is MemoryLifecycleStatus.DORMANT
            else "memory.archive"
            if destination_status is MemoryLifecycleStatus.ARCHIVED
            else "memory.maintenance"
        )
        if self._maintenance_telemetry is None:
            self._emit(operation, started, trace_id=trace_id, outcome="ok", memory_id=memory_id)
            return
        extraction_telemetry = self._telemetry
        self._telemetry = self._maintenance_telemetry
        try:
            self._emit(operation, started, trace_id=trace_id, outcome="ok", memory_id=memory_id)
        finally:
            self._telemetry = extraction_telemetry

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _embed(
        self, model_id: str, content: str, context: ProviderTraceContext
    ) -> EmbeddingResult:
        """Embed through the typed inward port.

        Providers implement the port's context-aware signature.  There is no
        compatibility retry here: catching ``TypeError`` would hide genuine
        provider failures and could silently drop correlation metadata.
        """

        return await self.embedder.embed(model_id, content, context=context)

    async def _selected_generation(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration
    ) -> MemoryEmbeddingGeneration | None:
        generation_id = configuration.embedding_generation
        if not isinstance(generation_id, UUID):
            return None
        try:
            generation = await self.repository.get_embedding_generation(
                issuer, subject, generation_id
            )
        except MemoryNotFound:
            return None
        return generation if generation.status == "active" else None

    def _job_provenance(self, job: MemoryProcessingJob) -> tuple[MemoryProvenance, ...]:
        return tuple(
            MemoryProvenance(
                uuid5(
                    MEMORY_ID_NAMESPACE,
                    f"provenance:{job.id}:{message_id}:{job.evidence_digest or ''}",
                ),
                "conversation_message",
                source_id=message_id,
                conversation_id=job.conversation_id,
                run_id=job.run_id,
                message_id=message_id,
                observed_at=self._now(),
                evidence_digest=job.evidence_digest,
            )
            for message_id in sorted(job.user_message_ids, key=str)
        )

    async def configure_models(
        self, configuration: MemoryModelConfiguration
    ) -> MemoryModelConfiguration:
        configuration = await self.repository.save_model_configuration(
            configuration.issuer, configuration.subject, configuration
        )
        self.configurations[(configuration.issuer, configuration.subject)] = configuration
        return configuration

    async def model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        # Production workers are long-lived while owners may update model
        # selection in the API process. SQL is authoritative here.
        configuration = await self.repository.get_model_configuration(issuer, subject)
        self.configurations[(issuer, subject)] = configuration
        return configuration

    async def enqueue(
        self,
        issuer: str,
        subject: str,
        *,
        run_id: UUID,
        conversation_id: UUID,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        agent_revision_id: UUID | None = None,
        user_message_ids: tuple[UUID, ...] = (),
        assistant_message_ids: tuple[UUID, ...] = (),
        evidence_digest: str | None = None,
        agent_profile_id: UUID | None = None,
        allow_shared_user_promotion: bool = False,
        memory_policy_revision_id: UUID | None = None,
    ) -> MemoryProcessingJob:
        key = (issuer, subject, run_id)
        prior = self.jobs.get(key)
        if prior is not None and prior.status is not ProcessingJobStatus.RETRYABLE:
            return prior
        job = MemoryProcessingJob(
            uuid5(MEMORY_ID_NAMESPACE, f"job:{issuer}:{subject}:{run_id}"),
            issuer,
            subject,
            run_id,
            conversation_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            available_at=self._now(),
            agent_revision_id=agent_revision_id,
            user_message_ids=user_message_ids,
            assistant_message_ids=assistant_message_ids,
            evidence_digest=evidence_digest,
            agent_profile_id=agent_profile_id,
            allow_shared_user_promotion=allow_shared_user_promotion,
            memory_policy_revision_id=memory_policy_revision_id,
        )
        self.jobs[key] = job
        job = await self.repository.enqueue_processing_job(job)
        self.jobs[key] = job
        return job

    @staticmethod
    def _action_from_provider(
        raw: object,
        job: MemoryProcessingJob,
        evidence_handles: Mapping[str, UUID] | None = None,
        run_agent_profile_id: UUID | None = None,
    ) -> MemoryCandidate:
        return action_from_provider(raw, job, evidence_handles, run_agent_profile_id)

    async def process(
        self,
        job: MemoryProcessingJob,
        *,
        user_content: str,
        assistant_content: str,
        user_message_ids: frozenset[UUID],
        run_agent_profile_id: UUID | None = None,
        provenance: Iterable[MemoryProvenance] = (),
        allow_shared_user_promotion: bool = False,
    ) -> MemoryCandidate:
        trace_id = (job.correlation_id or job.id).hex
        process_started = monotonic()
        queue_wait = max(0.0, (datetime.now(UTC) - job.available_at).total_seconds())
        queue_started = monotonic() - queue_wait
        self._emit(
            "memory.job.queue",
            queue_started,
            trace_id=trace_id,
            outcome="ok",
            attempt_count=job.attempt_count,
            backlog=len(self.jobs),
        )
        # A purge fences the deterministic action key, including delayed
        # in-memory redelivery after a worker restart.  Check the fence before
        # invoking inference so a purged turn cannot recreate content.
        if await self.repository.is_processing_command_purged(
            job.issuer, job.subject, job.id
        ):
            raise MemoryIdempotencyConflict("memory action is unavailable after purge")
        config = await self.model_configuration(job.issuer, job.subject)
        prior = next((item for item in self.candidates.values() if item.job_id == job.id), None)
        if prior is not None and prior.state is not CandidateState.RETRYABLE:
            return prior
        # Secrets are rejected from the turn before constructing an inference
        # request.  Persist only the deterministic, content-free outcome.
        if classify_sensitivity(user_content) is MemorySensitivity.CREDENTIAL or contains_secret(
            assistant_content
        ):
            candidate = MemoryCandidate(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                job.id,
                job.issuer,
                job.subject,
                MemoryAction.IGNORE,
                None,
                None,
                None,
                0.0,
                state=CandidateState.REJECTED,
                decision_reason="credential",
                sensitivity=MemorySensitivity.CREDENTIAL,
            )
            self.candidates[candidate.id] = candidate
            await self._candidate_execution.persist(candidate)
            await self._candidate_execution.record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=MemoryAction.IGNORE.value,
                outcome="ignored",
                error_class="credential",
            )
            self._emit(
                "memory.candidate",
                process_started,
                trace_id=trace_id,
                outcome="rejected",
                error_class="credential",
                attempt_count=job.attempt_count,
            )
            return candidate
        # Evidence handles are intentionally invocation-local capabilities.
        # They contain no durable identifiers and cannot be replayed by a
        # provider or correlated across jobs.
        ordered_message_ids = sorted(user_message_ids, key=str)
        segment_count = max(1, math.ceil(len(user_content) / 8192))
        invocation_nonce = secrets.token_urlsafe(18)
        evidence_handles = (
            {
                secrets.token_urlsafe(18): ordered_message_ids[
                    min(index, len(ordered_message_ids) - 1)
                ]
                for index in range(segment_count)
            }
            if ordered_message_ids
            else {}
        )
        request_segments: list[dict[str, str]] = []
        handle_values = tuple(evidence_handles)
        for index in range(segment_count):
            handle = (
                handle_values[index]
                if index < len(handle_values)
                else f"opaque:{invocation_nonce}:{index}"
            )
            request_segments.append(
                {
                    "handle": handle,
                    "text": user_content[index * 8192 : (index + 1) * 8192],
                }
            )
        request = StructuredInferenceRequest(
            model_id=config.extraction_model_id,
            schema=MEMORY_ACTION_SCHEMA,
            input={
                "decision_contract": {
                    "allowed_actions": [item.value for item in MemoryAction],
                    "instructions": (
                        f"Apply {MEMORY_EXTRACTION_POLICY_VERSION}. Extract only durable "
                        "owner-relevant facts explicitly stated by the user. "
                        "Ignore ordinary world knowledge, topical questions, and facts "
                        "provided only by the assistant. "
                        "For create or review, copy the durable fact into content, choose "
                        "kind from the enum, choose scope_type agent or user, and return "
                        "the matching opaque evidence handle. Set retention_basis to personal "
                        "for owner facts, or explicit_request only when the user asks Aura "
                        "to remember/save/note it. Use none with ignore otherwise. Choose "
                        "importance from "
                        "0 to 1 based on durable value and propose a half_life_days between "
                        "0.25 and 3650 based on the fact's relevance horizon; if uncertain, "
                        "use 0.5 importance and 30 days. Set valid_to only when the user "
                        "states a concrete end date; otherwise return null and do not invent "
                        "an expiry. Use action ignore when there is no durable personal or "
                        "explicitly requested fact. Never invent identifiers or evidence "
                        "handles. Assistant context is background only and is never grounding."
                    ),
                    "required_for_create_or_review": [
                        "content",
                        "kind",
                        "scope_type",
                        "retention_basis",
                        "grounded_evidence_handles",
                        "importance",
                        "half_life_days",
                        "sensitivity",
                        "valid_to",
                        "agent_profile_id",
                        "related_memory_id",
                    ],
                    "scope_guidance": {
                        "agent": "private to the current agent",
                        "user": "shared user memory; policy may require review",
                    },
                },
                "evidence_segments": request_segments,
                "assistant_context": assistant_content[:8192],
            },
            trace=ProviderTraceContext(
                trace_id=trace_id,
                correlation_id=str(job.correlation_id) if job.correlation_id else None,
                causation_id=str(job.causation_id) if job.causation_id else None,
                job_id=str(job.id),
                run_id=str(job.run_id),
                conversation_id=str(job.conversation_id),
            ),
        )
        extraction_started = monotonic()
        try:
            raw = await self.inference.infer(request)
        except Exception:
            self._emit(
                "memory.extraction",
                extraction_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            candidate = MemoryCandidate(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                job.id,
                job.issuer,
                job.subject,
                MemoryAction.REVIEW,
                None,
                None,
                None,
                0.0,
                state=CandidateState.RETRYABLE,
                decision_reason="provider_error",
            )
        else:
            try:
                candidate = self._action_from_provider(
                    raw,
                    job,
                    evidence_handles,
                    run_agent_profile_id=run_agent_profile_id or job.agent_profile_id,
                )
                # Provider output cannot choose ownership or bind a candidate
                # to a different job.  Normalize the identity to the server
                # job before any persistence, while retaining only
                # policy-relevant fields.
                if candidate.issuer != job.issuer or candidate.subject != job.subject:
                    raise MemoryValidationError("structured action owner mismatch")
                if candidate.job_id != job.id:
                    candidate = MemoryCandidate(
                        candidate.id,
                        job.id,
                        job.issuer,
                        job.subject,
                        candidate.action,
                        candidate.content,
                        candidate.kind,
                        candidate.scope,
                        candidate.confidence,
                        candidate.importance,
                        candidate.half_life_days,
                        candidate.valid_to,
                        candidate.sensitivity,
                        candidate.grounded_message_ids,
                        candidate.related_memory_id,
                        candidate.state,
                        candidate.decision_reason,
                        retention_basis=candidate.retention_basis,
                    )
            except MemoryValidationError:
                # The provider transport succeeded, but its schema-valid
                # payload did not satisfy the domain action contract.  This is
                # terminal review work, not an outage: retrying would replay
                # the same malformed decision indefinitely.  Do not retain
                # any provider-supplied content in this diagnostic outcome.
                self._emit(
                    "memory.extraction",
                    extraction_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                )
                candidate = MemoryCandidate(
                    uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                    job.id,
                    job.issuer,
                    job.subject,
                    MemoryAction.IGNORE,
                    None,
                    None,
                    None,
                    0.0,
                    state=CandidateState.REJECTED,
                    decision_reason="invalid_provider_output",
                    retention_basis=MemoryRetentionBasis.NONE,
                )
            else:
                self._emit(
                    "memory.extraction",
                    extraction_started,
                    trace_id=trace_id,
                    outcome="ok",
                    attempt_count=job.attempt_count,
                )
        if candidate.state is CandidateState.RETRYABLE:
            self.candidates[candidate.id] = candidate
            await self._candidate_execution.persist(candidate)
            self._emit(
                "memory.error",
                process_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            self.outcomes.append({"job_id": job.id, "action": "review", "outcome": "retryable"})
            return candidate
        # A provider may conservatively return ``ignore`` for exact,
        # owner-authored durable evidence.  Recover only narrow family facts,
        # unmistakable preferences, and explicit remember/save commands.  The
        # exact user text remains the canonical content; no model or assistant
        # text is used to derive a fallback.
        retention_basis = classify_retention_basis(user_content)
        deterministic_family_reinforcement = False
        deterministic_fallback_reinforcement = False
        fallback_source: str | None = None
        fallback_target_id: UUID | None = None
        fallback_target_version: int | None = None
        normalized_content = user_content.strip()
        family_event = _USER_FAMILY_RELATION_EVENT.search(user_content)
        deterministic_kind = deterministic_fallback_kind(user_content, retention_basis)
        allow_deterministic_create = (
            deterministic_kind is not None and family_event is None and allow_shared_user_promotion
        )
        if (
            candidate.action is MemoryAction.IGNORE
            and candidate.content is None
            and (
                (retention_basis is MemoryRetentionBasis.PERSONAL and family_event is not None)
                or allow_deterministic_create
            )
        ):
            existing_fallback_records = await self.repository.list_memories(
                job.issuer,
                job.subject,
                MemoryFilters(
                    scope_type=None,
                    include_all_scopes=True,
                    include_historical=False,
                    q=normalized_content,
                    limit=200,
                ),
            )
            existing_fallback = next(
                (
                    item
                    for item in existing_fallback_records
                    if item.content == normalized_content
                    and (
                        (item.scope.type is MemoryScopeType.USER and allow_shared_user_promotion)
                        or (
                            family_event is not None
                            and item.scope.type is MemoryScopeType.AGENT
                            and item.scope.agent_profile_id
                            == (run_agent_profile_id or job.agent_profile_id)
                        )
                    )
                ),
                None,
            )
            if existing_fallback is not None:
                allow_deterministic_create = False
                revision = existing_fallback.current_revision
                deterministic_family_reinforcement = family_event is not None
                deterministic_fallback_reinforcement = True
                fallback_source = "provider_ignore_reinforcement"
                fallback_target_id = existing_fallback.id
                fallback_target_version = existing_fallback.version
                candidate = replace(
                    candidate,
                    action=MemoryAction.REINFORCE,
                    content=normalized_content,
                    kind=existing_fallback.kind,
                    scope=existing_fallback.scope,
                    confidence=revision.confidence,
                    importance=revision.importance,
                    half_life_days=revision.half_life_days,
                    valid_to=revision.valid_to,
                    sensitivity=MemorySensitivity.ORDINARY,
                    grounded_message_ids=tuple(sorted(user_message_ids, key=str)),
                    retention_basis=retention_basis,
                    decision_reason="deterministic_reinforcement",
                )
            elif allow_deterministic_create:
                historical_fallback_records = await self.repository.list_memories(
                    job.issuer,
                    job.subject,
                    MemoryFilters(
                        scope_type=MemoryScopeType.USER,
                        include_historical=True,
                        q=normalized_content,
                        limit=200,
                    ),
                )
                if any(
                    item.content == normalized_content and item.scope.type is MemoryScopeType.USER
                    for item in historical_fallback_records
                ):
                    # Historical user records are never reactivated by a
                    # provider-ignore fallback; owner lifecycle controls or
                    # explicit UI actions must perform that transition.
                    allow_deterministic_create = False
            if allow_deterministic_create:
                fallback_source = "provider_ignore_deterministic_create"
                candidate = replace(
                    candidate,
                    action=MemoryAction.CREATE,
                    content=normalized_content,
                    kind=deterministic_kind,
                    scope=MemoryScope(MemoryScopeType.USER),
                    confidence=0.95,
                    importance=0.7,
                    half_life_days=365.0,
                    valid_to=None,
                    sensitivity=classify_sensitivity(normalized_content),
                    grounded_message_ids=tuple(sorted(user_message_ids, key=str)),
                    retention_basis=retention_basis,
                    decision_reason="deterministic_create",
                )

        conflict = False
        if candidate.content:
            existing = await self.repository.list_memories(
                job.issuer,
                job.subject,
                MemoryFilters(
                    scope_type=None,
                    include_all_scopes=True,
                    include_historical=True,
                    q=candidate.content,
                    limit=200,
                ),
            )
            conflict = any(
                item.content == candidate.content
                and item.scope != (candidate.scope or MemoryScope(MemoryScopeType.USER))
                for item in existing
            )
        retention_gate_started = monotonic()
        if candidate.action is not MemoryAction.IGNORE:
            candidate = replace(candidate, retention_basis=retention_basis)
            candidate = normalize_retention_horizon(candidate, user_content=user_content)
        self._emit(
            "memory.retention_gate",
            retention_gate_started,
            trace_id=trace_id,
            outcome=retention_basis.value,
            attempt_count=job.attempt_count,
        )
        policy_started = monotonic()
        provider_requested_review = candidate.decision_reason == "provider_requested_review"
        if candidate.decision_reason == "invalid_provider_output":
            decision = CandidateDecision(CandidateState.REJECTED, "invalid_provider_output")
        elif (
            candidate.action is not MemoryAction.IGNORE
            and retention_basis is MemoryRetentionBasis.NONE
        ):
            decision = CandidateDecision(CandidateState.REJECTED, "not_personal")
        else:
            decision = decide_candidate(
                candidate,
                user_message_ids=user_message_ids,
                run_agent_profile_id=run_agent_profile_id or job.agent_profile_id,
                existing_conflict=conflict,
                user_content=user_content,
                allow_shared_user_promotion=allow_shared_user_promotion,
            )
        if provider_requested_review and decision.state is CandidateState.ACCEPTED:
            decision = CandidateDecision(CandidateState.REVIEW, "provider_requested_review")
        decision_outcome = decision.state.value
        if candidate.action is MemoryAction.IGNORE and decision.reason != "invalid_provider_output":
            decision_outcome = "ignored"
        self._emit(
            "memory.policy",
            policy_started,
            trace_id=trace_id,
            outcome=decision_outcome,
            attempt_count=job.attempt_count,
        )
        candidate = replace(
            candidate,
            state=decision.state,
            decision_reason=(
                (f"pif@{fallback_target_id.hex}@{fallback_target_version}")
                if (
                    fallback_source == "provider_ignore_reinforcement"
                    and fallback_target_id is not None
                    and fallback_target_version is not None
                )
                else fallback_source or decision.reason
            ),
        )
        candidate = _content_free_candidate(candidate)
        self.candidates[candidate.id] = candidate
        await self._candidate_execution.persist(candidate)
        if decision.state is not CandidateState.ACCEPTED:
            self._emit(
                "memory.candidate",
                process_started,
                trace_id=trace_id,
                outcome=decision_outcome,
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                process_started,
                trace_id=trace_id,
                outcome=decision.state.value,
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            self.outcomes.append(
                {
                    "job_id": job.id,
                    "action": candidate.action.value,
                    "outcome": "review" if decision.state is CandidateState.REVIEW else "ignored",
                }
            )
            await self._candidate_execution.record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=candidate.action.value,
                outcome="review" if decision.state is CandidateState.REVIEW else "ignored",
            )
            return candidate
        scope = candidate.scope or MemoryScope(MemoryScopeType.USER)
        prov = tuple(provenance)
        action_started = monotonic()
        matches = await self.repository.list_memories(
            job.issuer,
            job.subject,
            MemoryFilters(
                scope_type=scope.type,
                agent_profile_id=scope.agent_profile_id,
                include_historical=not deterministic_fallback_reinforcement,
                limit=100000,
            ),
        )
        duplicate = (
            next((item for item in matches if item.id == fallback_target_id), None)
            if fallback_target_id is not None
            else next(
                (
                    item
                    for item in matches
                    if candidate.content and item.content == candidate.content
                ),
                None,
            )
        )
        if deterministic_fallback_reinforcement and duplicate is None:
            # The exact active target disappeared or changed scope between
            # the owner-scoped lookup and mutation.  Never fall through to
            # CREATE: a purge fence or concurrent lifecycle transition must
            # settle this provider-ignore fallback without recreating content.
            candidate = _content_free_candidate(
                replace(
                    candidate,
                    action=MemoryAction.IGNORE,
                    content=None,
                    kind=None,
                    scope=None,
                    importance=None,
                    half_life_days=None,
                    valid_to=None,
                    grounded_message_ids=(),
                    state=CandidateState.REJECTED,
                    decision_reason="reinforcement_target_unavailable",
                    retention_basis=MemoryRetentionBasis.NONE,
                )
            )
            self.candidates[candidate.id] = candidate
            await self._candidate_execution.persist(candidate)
            await self._candidate_execution.record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=MemoryAction.REINFORCE.value,
                outcome="ignored",
                error_class="not_found",
            )
            self._emit(
                "memory.candidate",
                process_started,
                trace_id=trace_id,
                outcome="rejected",
                error_class="not_found",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                process_started,
                trace_id=trace_id,
                outcome="ok",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            self.outcomes.append(
                {
                    "job_id": job.id,
                    "action": MemoryAction.REINFORCE.value,
                    "outcome": "ignored",
                    "error_class": "not_found",
                }
            )
            return candidate
        if fallback_source == "provider_ignore_deterministic_create":
            exact = [
                item for item in matches if candidate.content and item.content == candidate.content
            ]
            active_exact = next(
                (item for item in exact if item.status is MemoryLifecycleStatus.ACTIVE), None
            )
            if active_exact is not None:
                duplicate = active_exact
                deterministic_fallback_reinforcement = True
                fallback_target_id = active_exact.id
                fallback_target_version = active_exact.version
                fallback_source = "provider_ignore_reinforcement"
            elif exact:
                candidate = _content_free_candidate(
                    replace(
                        candidate,
                        action=MemoryAction.IGNORE,
                        content=None,
                        kind=None,
                        scope=None,
                        importance=None,
                        half_life_days=None,
                        valid_to=None,
                        grounded_message_ids=(),
                        state=CandidateState.REJECTED,
                        decision_reason="reinforcement_target_unavailable",
                        retention_basis=MemoryRetentionBasis.NONE,
                    )
                )
                self.candidates[candidate.id] = candidate
                await self._candidate_execution.persist(candidate)
                await self._candidate_execution.record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=MemoryAction.CREATE.value,
                    outcome="ignored",
                    error_class="not_found",
                )
                self._emit(
                    "memory.candidate",
                    process_started,
                    trace_id=trace_id,
                    outcome="rejected",
                    error_class="not_found",
                    attempt_count=job.attempt_count,
                )
                self._emit(
                    "memory.job",
                    process_started,
                    trace_id=trace_id,
                    outcome="ok",
                    error_class="not_found",
                    attempt_count=job.attempt_count,
                    backlog=len(self.jobs),
                )
                self.outcomes.append(
                    {
                        "job_id": job.id,
                        "action": MemoryAction.CREATE.value,
                        "outcome": "ignored",
                        "error_class": "not_found",
                    }
                )
                return candidate
        if duplicate is not None and candidate.action in {
            MemoryAction.CREATE,
            MemoryAction.REINFORCE,
        }:
            try:
                record = await self._candidate_execution.reinforce(
                    job.issuer,
                    job.subject,
                    duplicate.id,
                    provenance=prov,
                    idempotency_key=f"memory-action:{candidate.id}",
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                    authorized_agent_ids=frozenset({scope.agent_profile_id})
                    if scope.agent_profile_id
                    else frozenset[UUID](),
                    expected_version=(
                        fallback_target_version
                        if deterministic_fallback_reinforcement
                        and fallback_target_version is not None
                        else duplicate.version
                    ),
                    require_active=deterministic_fallback_reinforcement,
                )
            except MemoryNotFound, MemoryVersionConflict:
                if not deterministic_fallback_reinforcement:
                    raise
                candidate = _content_free_candidate(
                    replace(
                        candidate,
                        action=MemoryAction.IGNORE,
                        content=None,
                        kind=None,
                        scope=None,
                        importance=None,
                        half_life_days=None,
                        valid_to=None,
                        grounded_message_ids=(),
                        state=CandidateState.REJECTED,
                        decision_reason="reinforcement_target_unavailable",
                        retention_basis=MemoryRetentionBasis.NONE,
                    )
                )
                self.candidates[candidate.id] = candidate
                await self._candidate_execution.persist(candidate)
                await self._candidate_execution.record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=MemoryAction.REINFORCE.value,
                    outcome="ignored",
                    error_class="not_found",
                )
                self._emit(
                    "memory.candidate",
                    process_started,
                    trace_id=trace_id,
                    outcome="rejected",
                    error_class="not_found",
                    attempt_count=job.attempt_count,
                )
                self._emit(
                    "memory.job",
                    process_started,
                    trace_id=trace_id,
                    outcome="ok",
                    attempt_count=job.attempt_count,
                    backlog=len(self.jobs),
                )
                self.outcomes.append(
                    {
                        "job_id": job.id,
                        "action": MemoryAction.REINFORCE.value,
                        "outcome": "ignored",
                        "error_class": "not_found",
                    }
                )
                return candidate
            # A deterministic family-event normalization may strengthen
            # the horizon of an older matching record.  Keep revisions
            # immutable: reinforcement resets relevance, while this
            # metadata correction creates one bounded successor revision.
            current_revision = record.current_revision
            if (
                deterministic_family_reinforcement
                and (
                    candidate.half_life_days is not None
                    and candidate.half_life_days > current_revision.half_life_days
                    and candidate.valid_to is None
                )
                or (
                    deterministic_family_reinforcement
                    and candidate.valid_to is None
                    and current_revision.valid_to is not None
                )
            ):
                try:
                    record = await self._candidate_execution.revise(
                        job.issuer,
                        job.subject,
                        duplicate.id,
                        content=record.content,
                        half_life_days=candidate.half_life_days,
                        valid_to=None,
                        clear_valid_to=True,
                        expected_version=record.version,
                        reason="deterministic family-event horizon normalization",
                        idempotency_key=f"memory-horizon:{candidate.id}",
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                        authorized_agent_ids=frozenset({scope.agent_profile_id})
                        if scope.agent_profile_id
                        else frozenset[UUID](),
                    )
                except (MemoryNotFound, MemoryVersionConflict) as revision_error:
                    # Reinforcement may have raced with a purge or owner
                    # correction.  Re-read the exact target; preserve a
                    # successful concurrent correction, but never leave
                    # an accepted candidate pointing at a missing record.
                    try:
                        if isinstance(revision_error, MemoryVersionConflict):
                            raise MemoryNotFound("memory horizon revision lost an owner correction")
                        record = await self.repository.get_memory(
                            job.issuer,
                            job.subject,
                            duplicate.id,
                            scope_type=scope.type,
                            agent_profile_id=scope.agent_profile_id,
                        )
                        if record.status is not MemoryLifecycleStatus.ACTIVE:
                            raise MemoryNotFound("memory is no longer active")
                        horizon_ready = (
                            candidate.half_life_days is not None
                            and record.current_revision.half_life_days >= candidate.half_life_days
                            and record.current_revision.valid_to is None
                        )
                        if not horizon_ready:
                            try:
                                record = await self._candidate_execution.revise(
                                    job.issuer,
                                    job.subject,
                                    duplicate.id,
                                    content=record.content,
                                    half_life_days=candidate.half_life_days,
                                    valid_to=None,
                                    clear_valid_to=True,
                                    expected_version=record.version,
                                    reason="deterministic family-event horizon normalization",
                                    idempotency_key=f"memory-horizon-retry:{candidate.id}",
                                    scope_type=scope.type,
                                    agent_profile_id=scope.agent_profile_id,
                                    authorized_agent_ids=frozenset({scope.agent_profile_id})
                                    if scope.agent_profile_id
                                    else frozenset[UUID](),
                                )
                            except (MemoryNotFound, MemoryVersionConflict) as retry_error:
                                raise MemoryNotFound(
                                    "memory horizon revision lost its target"
                                ) from retry_error
                    except MemoryNotFound:
                        candidate = _content_free_candidate(
                            replace(
                                candidate,
                                action=MemoryAction.IGNORE,
                                content=None,
                                kind=None,
                                scope=None,
                                importance=None,
                                half_life_days=None,
                                valid_to=None,
                                grounded_message_ids=(),
                                state=CandidateState.REJECTED,
                                decision_reason="reinforcement_target_unavailable",
                                retention_basis=MemoryRetentionBasis.NONE,
                            )
                        )
                        self.candidates[candidate.id] = candidate
                        await self._candidate_execution.persist(candidate)
                        await self._candidate_execution.record_outcome(
                            candidate_id=candidate.id,
                            job_id=job.id,
                            issuer=job.issuer,
                            subject=job.subject,
                            action=MemoryAction.REINFORCE.value,
                            outcome="ignored",
                            error_class="not_found",
                        )
                        self._emit(
                            "memory.candidate",
                            process_started,
                            trace_id=trace_id,
                            outcome="rejected",
                            error_class="not_found",
                            attempt_count=job.attempt_count,
                        )
                        self._emit(
                            "memory.job",
                            process_started,
                            trace_id=trace_id,
                            outcome="ok",
                            attempt_count=job.attempt_count,
                            backlog=len(self.jobs),
                        )
                        self.outcomes.append(
                            {
                                "job_id": job.id,
                                "action": MemoryAction.REINFORCE.value,
                                "outcome": "ignored",
                                "error_class": "not_found",
                            }
                        )
                        return candidate
        elif candidate.action is MemoryAction.CREATE:
            record = await self._candidate_execution.create(
                job.issuer,
                job.subject,
                kind=candidate.kind or MemoryKind.SEMANTIC,
                scope=scope,
                content=candidate.content,
                confidence=candidate.confidence,
                importance=candidate.importance or 0.5,
                half_life_days=candidate.half_life_days or 30.0,
                valid_to=candidate.valid_to,
                provenance=prov,
                idempotency_key=(
                    deterministic_create_idempotency_key(
                        job.issuer, job.subject, candidate.content or ""
                    )
                    if fallback_source == "provider_ignore_deterministic_create"
                    else f"memory-job:{job.id}"
                ),
                agent_profile_id=scope.agent_profile_id,
            )
        else:
            record = next(
                (
                    item
                    for item in matches
                    if candidate.content and item.content == candidate.content
                ),
                None,
            )
            if record is None:
                if (
                    candidate.action in {MemoryAction.SUPERSEDE, MemoryAction.DISPUTE}
                    and candidate.related_memory_id
                ):
                    related = await self.repository.get_memory(
                        job.issuer,
                        job.subject,
                        candidate.related_memory_id,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                    transition = (
                        MemoryLifecycleStatus.SUPERSEDED
                        if candidate.action is MemoryAction.SUPERSEDE
                        else MemoryLifecycleStatus.DISPUTED
                    )
                    record = await self._candidate_execution.transition(
                        job.issuer,
                        job.subject,
                        candidate.related_memory_id,
                        status=transition,
                        expected_version=related.version,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                else:
                    record = await self._candidate_execution.create(
                        job.issuer,
                        job.subject,
                        kind=candidate.kind or MemoryKind.SEMANTIC,
                        scope=scope,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        importance=candidate.importance or 0.5,
                        half_life_days=candidate.half_life_days or 30.0,
                        provenance=prov,
                        idempotency_key=f"memory-job:{job.id}",
                        agent_profile_id=scope.agent_profile_id,
                    )
            else:
                record.reinforced_at = self._now()
                record.status = MemoryLifecycleStatus.ACTIVE
                record.provenance.extend(prov)
        if fallback_source == "provider_ignore_reinforcement":
            fallback_source = f"{fallback_source}@{record.version}"
        candidate = replace(
            candidate,
            memory_id=record.id,
            decision_reason=fallback_source or candidate.decision_reason,
        )
        await self._candidate_execution.link(job.id, job.issuer, job.subject, record.id)
        self.candidates[candidate.id] = candidate
        await self._candidate_execution.persist(candidate)
        self.outcomes.append(
            {
                "job_id": job.id,
                "action": candidate.action.value,
                "outcome": "created",
                "memory_id": record.id,
                "revision_id": record.current_revision_id,
            }
        )
        await self._candidate_execution.record_outcome(
            candidate_id=candidate.id,
            job_id=job.id,
            issuer=job.issuer,
            subject=job.subject,
            action=candidate.action.value,
            outcome="created",
            memory_id=record.id,
            revision_id=record.current_revision_id,
        )
        if fallback_source is not None:
            self._emit(
                "memory.fallback",
                action_started,
                trace_id=trace_id,
                outcome="fallback",
                attempt_count=job.attempt_count,
            )
        self._emit(
            "memory.candidate",
            process_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
        )
        self._emit(
            "memory.action",
            action_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
        )
        # Embedding failures remain retryable worker work; the accepted memory
        # is already durable and never turns a completed conversation into an error.
        embedding_job: MemoryEmbeddingJob | None = None
        embedding_started = monotonic()
        try:
            selected_generation = await self._selected_generation(job.issuer, job.subject, config)
            generation_id = selected_generation.id if selected_generation is not None else None
            if selected_generation is not None:
                generation_id = selected_generation.id
                embedding_job = await self._embedding_jobs.claim(
                    job.issuer,
                    job.subject,
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                    generation_id=generation_id,
                )
                embedding = await self._embed(
                    selected_generation.model_id,
                    record.content,
                    ProviderTraceContext(
                        trace_id=trace_id,
                        correlation_id=str(job.correlation_id) if job.correlation_id else None,
                        causation_id=str(job.causation_id) if job.causation_id else None,
                        job_id=str(job.id),
                        run_id=str(job.run_id),
                        conversation_id=str(job.conversation_id),
                        generation_id=str(generation_id),
                    ),
                )
                if (
                    embedding.model_id != selected_generation.model_id
                    or embedding.model_revision != selected_generation.model_revision
                    or embedding.model_digest != selected_generation.model_digest
                    or embedding.dimension != selected_generation.dimension
                ):
                    raise MemoryValidationError("embedding provider identity or dimension mismatch")
                self._emit(
                    "memory.embedding",
                    embedding_started,
                    trace_id=trace_id,
                    outcome="ok",
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                    generation_id=generation_id,
                    attempt_count=job.attempt_count,
                    backlog=0,
                )
                await self.repository.attach_embedding(
                    job.issuer,
                    job.subject,
                    record.id,
                    revision_id=record.current_revision_id,
                    generation_id=UUID(str(generation_id)),
                    vector=embedding.vector,
                    digest=embedding.digest,
                    model_id=embedding.model_id,
                    model_revision=embedding.model_revision,
                    model_digest=embedding.model_digest,
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                await self._embedding_jobs.settle(
                    embedding_job, issuer=job.issuer, subject=job.subject
                )
            else:
                # The accepted revision is durable, but without a selected
                # active generation it remains an explicit missing-embedding
                # backlog item for maintenance/reindex.
                self._emit(
                    "memory.embedding",
                    embedding_started,
                    trace_id=trace_id,
                    outcome="retryable",
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                    backlog=1,
                )
        except Exception:
            self._emit(
                "memory.embedding",
                embedding_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
                backlog=1,
            )
            self._emit(
                "memory.error",
                process_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
            )
            self.outcomes.append(
                {
                    "job_id": job.id,
                    "action": "embedding",
                    "outcome": "retryable",
                    "memory_id": record.id,
                }
            )
            if embedding_job is not None:
                if embedding_job.lease_id is not None:
                    await self._embedding_jobs.settle(
                        embedding_job,
                        issuer=job.issuer,
                        subject=job.subject,
                        retryable=True,
                        error_class="provider",
                    )
        self._emit(
            "memory.job",
            process_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
            backlog=len(self.jobs),
        )
        return candidate

    async def _resume_accepted_candidate(
        self, job: MemoryProcessingJob, candidate: MemoryCandidate
    ) -> MemoryCandidate:
        """Resume the durable action phase without a second model decision."""

        pending_marker = _FALLBACK_REINFORCEMENT_TARGET_MARKER.fullmatch(
            candidate.decision_reason or ""
        )
        if pending_marker is not None:
            pending_action = candidate.action
            try:
                target_id = UUID(pending_marker.group("memory_id"))
                expected_version = int(pending_marker.group("version"))
                scope = candidate.scope
                if scope is None:
                    raise MemoryNotFound("fallback target scope is unavailable")
                target = await self.repository.get_memory(
                    job.issuer,
                    job.subject,
                    target_id,
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                if target.status is not MemoryLifecycleStatus.ACTIVE:
                    raise MemoryNotFound("fallback target is no longer active")
                record = await self._candidate_execution.reinforce(
                    job.issuer,
                    job.subject,
                    target_id,
                    provenance=self._job_provenance(job),
                    idempotency_key=f"memory-action:{candidate.id}",
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                    authorized_agent_ids=frozenset({scope.agent_profile_id})
                    if scope.agent_profile_id
                    else frozenset[UUID](),
                    expected_version=expected_version,
                    require_active=True,
                )
                if _USER_FAMILY_RELATION_EVENT.search(candidate.content or "") is not None and (
                    (
                        candidate.half_life_days is not None
                        and candidate.half_life_days > record.current_revision.half_life_days
                        and candidate.valid_to is None
                    )
                    or (candidate.valid_to is None and record.current_revision.valid_to is not None)
                ):
                    try:
                        record = await self._candidate_execution.revise(
                            job.issuer,
                            job.subject,
                            target_id,
                            content=record.content,
                            half_life_days=candidate.half_life_days,
                            valid_to=None,
                            clear_valid_to=True,
                            expected_version=record.version,
                            reason="deterministic family-event horizon normalization",
                            idempotency_key=f"memory-horizon:{candidate.id}",
                            scope_type=scope.type,
                            agent_profile_id=scope.agent_profile_id,
                            authorized_agent_ids=frozenset({scope.agent_profile_id})
                            if scope.agent_profile_id
                            else frozenset[UUID](),
                        )
                    except (MemoryNotFound, MemoryVersionConflict) as exc:
                        raise MemoryNotFound("fallback horizon target is unavailable") from exc
                await self._candidate_execution.link(job.id, job.issuer, job.subject, record.id)
                candidate = replace(
                    candidate,
                    memory_id=record.id,
                    decision_reason=f"provider_ignore_reinforcement@{record.version}",
                )
                self.candidates[candidate.id] = candidate
                await self._candidate_execution.persist(candidate)
                self.outcomes.append(
                    {
                        "job_id": job.id,
                        "action": MemoryAction.REINFORCE.value,
                        "outcome": "created",
                        "memory_id": record.id,
                        "revision_id": record.current_revision_id,
                    }
                )
                await self._candidate_execution.record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=MemoryAction.REINFORCE.value,
                    outcome="created",
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                )
                return candidate
            except MemoryNotFound, MemoryVersionConflict:
                candidate = _content_free_candidate(
                    replace(
                        candidate,
                        action=MemoryAction.IGNORE,
                        content=None,
                        kind=None,
                        scope=None,
                        importance=None,
                        half_life_days=None,
                        valid_to=None,
                        grounded_message_ids=(),
                        state=CandidateState.REJECTED,
                        decision_reason="reinforcement_target_unavailable",
                        retention_basis=MemoryRetentionBasis.NONE,
                    )
                )
                self.candidates[candidate.id] = candidate
                await self._candidate_execution.persist(candidate)
                self.outcomes.append(
                    {
                        "job_id": job.id,
                        "action": pending_action.value,
                        "outcome": "ignored",
                        "error_class": "not_found",
                    }
                )
                await self._candidate_execution.record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=pending_action.value,
                    outcome="ignored",
                    error_class="not_found",
                )
                return candidate

        if (
            candidate.memory_id is None
            and candidate.decision_reason == "provider_ignore_deterministic_create"
        ):
            if candidate.content is None:
                raise MemoryValidationError("deterministic create candidate is incomplete")
            records = await self.repository.list_memories(
                job.issuer,
                job.subject,
                MemoryFilters(
                    scope_type=MemoryScopeType.USER,
                    include_historical=True,
                    q=candidate.content,
                    limit=200,
                ),
            )
            exact = [item for item in records if item.content == candidate.content]
            active = next(
                (item for item in exact if item.status is MemoryLifecycleStatus.ACTIVE), None
            )
            if exact and active is None:
                candidate = _content_free_candidate(
                    replace(
                        candidate,
                        action=MemoryAction.IGNORE,
                        content=None,
                        kind=None,
                        scope=None,
                        importance=None,
                        half_life_days=None,
                        valid_to=None,
                        grounded_message_ids=(),
                        state=CandidateState.REJECTED,
                        decision_reason="reinforcement_target_unavailable",
                        retention_basis=MemoryRetentionBasis.NONE,
                    )
                )
                self.candidates[candidate.id] = candidate
                await self._candidate_execution.persist(candidate)
                return candidate
            if active is not None:
                try:
                    record = await self._candidate_execution.reinforce(
                        job.issuer,
                        job.subject,
                        active.id,
                        provenance=self._job_provenance(job),
                        idempotency_key=f"memory-action:{candidate.id}",
                        scope_type=MemoryScopeType.USER,
                        expected_version=active.version,
                        require_active=True,
                    )
                except MemoryNotFound, MemoryVersionConflict:
                    candidate = _content_free_candidate(
                        replace(
                            candidate,
                            action=MemoryAction.IGNORE,
                            content=None,
                            kind=None,
                            scope=None,
                            importance=None,
                            half_life_days=None,
                            valid_to=None,
                            grounded_message_ids=(),
                            state=CandidateState.REJECTED,
                            decision_reason="reinforcement_target_unavailable",
                            retention_basis=MemoryRetentionBasis.NONE,
                        )
                    )
                    self.candidates[candidate.id] = candidate
                    await self._candidate_execution.persist(candidate)
                    return candidate
            else:
                record = await self._candidate_execution.create(
                    job.issuer,
                    job.subject,
                    kind=candidate.kind or MemoryKind.SEMANTIC,
                    scope=MemoryScope(MemoryScopeType.USER),
                    content=candidate.content,
                    confidence=candidate.confidence,
                    importance=candidate.importance or 0.5,
                    half_life_days=candidate.half_life_days or 30.0,
                    valid_to=candidate.valid_to,
                    provenance=self._job_provenance(job),
                    idempotency_key=_deterministic_create_idempotency_key(
                        job.issuer, job.subject, candidate.content
                    ),
                )
            await self._candidate_execution.link(job.id, job.issuer, job.subject, record.id)
            candidate = replace(
                candidate,
                memory_id=record.id,
                decision_reason=f"provider_ignore_deterministic_create@{record.version}",
            )
            self.candidates[candidate.id] = candidate
            await self._candidate_execution.persist(candidate)
            await self._candidate_execution.record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=(
                    MemoryAction.REINFORCE.value
                    if active is not None
                    else MemoryAction.CREATE.value
                ),
                outcome="created",
                memory_id=record.id,
                revision_id=record.current_revision_id,
            )
            return candidate

        if candidate.memory_id is not None:
            marker = _FALLBACK_REINFORCEMENT_MARKER.fullmatch(candidate.decision_reason or "")
            if marker is not None:
                try:
                    record = await self.repository.get_memory(
                        job.issuer,
                        job.subject,
                        candidate.memory_id,
                        scope_type=candidate.scope.type if candidate.scope else None,
                        agent_profile_id=candidate.scope.agent_profile_id
                        if candidate.scope
                        else None,
                    )
                    if record.status is not MemoryLifecycleStatus.ACTIVE or record.version != int(
                        marker.group("version")
                    ):
                        raise MemoryNotFound(
                            "fallback target is no longer the captured active version"
                        )
                except MemoryNotFound, MemoryVersionConflict:
                    candidate = _content_free_candidate(
                        replace(
                            candidate,
                            action=MemoryAction.IGNORE,
                            content=None,
                            kind=None,
                            scope=None,
                            importance=None,
                            half_life_days=None,
                            valid_to=None,
                            grounded_message_ids=(),
                            state=CandidateState.REJECTED,
                            decision_reason="reinforcement_target_unavailable",
                            retention_basis=MemoryRetentionBasis.NONE,
                        )
                    )
                    self.candidates[candidate.id] = candidate
                    await self._candidate_execution.persist(candidate)
                    return candidate
            return candidate
        if candidate.content is None or candidate.scope is None:
            raise MemoryValidationError("accepted candidate is incomplete")
        matches = await self.repository.list_memories(
            job.issuer,
            job.subject,
            MemoryFilters(
                scope_type=candidate.scope.type,
                agent_profile_id=candidate.scope.agent_profile_id,
                include_historical=True,
                limit=100000,
            ),
        )
        record = next((item for item in matches if item.content == candidate.content), None)
        if record is None:
            record = await self._candidate_execution.create(
                job.issuer,
                job.subject,
                kind=candidate.kind or MemoryKind.SEMANTIC,
                scope=candidate.scope,
                content=candidate.content,
                confidence=candidate.confidence,
                importance=candidate.importance or 0.5,
                half_life_days=candidate.half_life_days or 30.0,
                valid_to=candidate.valid_to,
                provenance=self._job_provenance(job),
                idempotency_key=f"memory-job:{job.id}",
                agent_profile_id=candidate.scope.agent_profile_id,
            )
        else:
            record = await self._candidate_execution.reinforce(
                job.issuer,
                job.subject,
                record.id,
                provenance=self._job_provenance(job),
                idempotency_key=f"memory-action:{candidate.id}",
                scope_type=candidate.scope.type,
                agent_profile_id=candidate.scope.agent_profile_id,
            )
        candidate = replace(candidate, memory_id=record.id)
        await self._candidate_execution.link(job.id, job.issuer, job.subject, record.id)
        await self._candidate_execution.persist(candidate)
        await self._candidate_execution.record_outcome(
            candidate_id=candidate.id,
            job_id=job.id,
            issuer=job.issuer,
            subject=job.subject,
            action=candidate.action.value,
            outcome="created",
            memory_id=record.id,
            revision_id=record.current_revision_id,
        )
        return candidate

    async def process_job(
        self, job_id: UUID, *, lease_id: UUID | None = None
    ) -> MemoryCandidate | None:
        """Process a previously enqueued job when its evidence is available.

        Durable deployments hydrate the evidence through the conversation
        application port before calling ``process``.  The in-memory adapter
        keeps this method intentionally content-free for worker composition.
        """

        job = next((item for item in self.jobs.values() if item.id == job_id), None)
        if job is None:
            loader = self._job_loader
            if loader is not None:
                job = await loader(job_id)
            else:
                raise MemoryValidationError(
                    "owner-scoped memory processing lookup requires a job loader"
                )
            self.jobs[(job.issuer, job.subject, job.run_id)] = job
        elif lease_id is not None:
            # A worker may pass a lease obtained from a fresh claim while the
            # process-local cache still contains an older retryable snapshot.
            # Replace that snapshot before validating the capability.
            job = await self.repository.get_processing_job(job.id, job.issuer, job.subject)
        job_started = monotonic()
        trace_id = (job.correlation_id or job.id).hex
        # A loaded job is only a snapshot. Never reuse its lease: every
        # delivery must obtain a fresh capability from the durable owner
        # boundary. The sole exception is a lease returned by this call.
        if lease_id is None:
            claimed = await self.repository.claim_processing_job_by_id(
                job.id, job.issuer, job.subject
            )
            if claimed is None:
                # A completed job or an unexpired lease is already settled by
                # another worker. Do not invoke inference twice.
                return None
            job = claimed
            lease_id = job.lease_id
        elif job.lease_id != lease_id:
            raise MemoryValidationError("processing job lease capability is stale")
        self.jobs[(job.issuer, job.subject, job.run_id)] = job

        async def settle_evidence_rejection() -> None:
            capability = lease_id or job.lease_id
            if capability is None:
                return
            settled = await self.repository.settle_processing_job(
                job.id,
                capability,
                issuer=job.issuer,
                subject=job.subject,
                retryable=False,
                error_class="evidence",
            )
            self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled

        async def settle_parked(error_class: str) -> None:
            capability = lease_id or job.lease_id
            if capability is None:
                return
            settled = await self.repository.settle_processing_job(
                job.id,
                capability,
                issuer=job.issuer,
                subject=job.subject,
                retryable=True,
                error_class=error_class,
            )
            self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled

        prior = next((item for item in self.candidates.values() if item.job_id == job.id), None)
        if prior is None:
            prior_value = await self.repository.get_candidate_for_job(
                job.id, job.issuer, job.subject
            )
            if prior_value is not None:
                prior = prior_value
                self.candidates[prior.id] = prior
        if prior is not None and prior.state is not CandidateState.RETRYABLE:
            # Accepted candidates carry the durable memory identity.  Resume
            # the embedding phase after a worker crash without asking the
            # extractor to make a second decision.  Review/rejected outcomes
            # are already terminal and need no provider work.
            pending_resume = prior.state is CandidateState.ACCEPTED and (
                _FALLBACK_REINFORCEMENT_TARGET_MARKER.fullmatch(prior.decision_reason or "")
                is not None
                or prior.decision_reason == "provider_ignore_deterministic_create"
            )
            if prior.state is CandidateState.ACCEPTED and (
                prior.memory_id is None
                or _FALLBACK_REINFORCEMENT_MARKER.fullmatch(prior.decision_reason or "") is not None
            ):
                prior = await self._resume_accepted_candidate(job, prior)
            if pending_resume:
                if prior.state is CandidateState.ACCEPTED and prior.memory_id is not None:
                    self._emit(
                        "memory.fallback",
                        job_started,
                        trace_id=trace_id,
                        outcome="fallback",
                        memory_id=prior.memory_id,
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.action",
                        job_started,
                        trace_id=trace_id,
                        outcome="accepted",
                        memory_id=prior.memory_id,
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.candidate",
                        job_started,
                        trace_id=trace_id,
                        outcome="accepted",
                        memory_id=prior.memory_id,
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.job",
                        job_started,
                        trace_id=trace_id,
                        outcome="accepted",
                        memory_id=prior.memory_id,
                        attempt_count=job.attempt_count,
                        backlog=len(self.jobs),
                    )
                else:
                    self._emit(
                        "memory.fallback",
                        job_started,
                        trace_id=trace_id,
                        outcome="rejected",
                        error_class="not_found",
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.candidate",
                        job_started,
                        trace_id=trace_id,
                        outcome="rejected",
                        error_class="not_found",
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.job",
                        job_started,
                        trace_id=trace_id,
                        outcome="ok",
                        error_class="not_found",
                        attempt_count=job.attempt_count,
                        backlog=len(self.jobs),
                    )
            if prior.state is CandidateState.ACCEPTED and prior.memory_id is not None:
                try:
                    config = await self.model_configuration(job.issuer, job.subject)
                    selected_generation = await self._selected_generation(
                        job.issuer, job.subject, config
                    )
                    if selected_generation is not None:
                        generation_id = selected_generation.id
                        record = await self.repository.get_memory(
                            job.issuer,
                            job.subject,
                            prior.memory_id,
                            scope_type=prior.scope.type if prior.scope else None,
                            agent_profile_id=prior.scope.agent_profile_id if prior.scope else None,
                        )
                        if not any(
                            item.revision_id == record.current_revision_id
                            and item.generation_id == generation_id
                            for item in record.embeddings
                        ):
                            embedding_job = await self._embedding_jobs.claim(
                                job.issuer,
                                job.subject,
                                memory_id=record.id,
                                revision_id=record.current_revision_id,
                                generation_id=generation_id,
                            )
                            embedding = await self._embed(
                                selected_generation.model_id,
                                record.content,
                                ProviderTraceContext(
                                    trace_id=trace_id,
                                    correlation_id=str(job.correlation_id)
                                    if job.correlation_id
                                    else None,
                                    causation_id=str(job.causation_id)
                                    if job.causation_id
                                    else None,
                                    job_id=str(job.id),
                                    run_id=str(job.run_id),
                                    conversation_id=str(job.conversation_id),
                                    generation_id=str(generation_id),
                                ),
                            )
                            if (
                                embedding.model_id != selected_generation.model_id
                                or embedding.model_revision != selected_generation.model_revision
                                or embedding.model_digest != selected_generation.model_digest
                                or embedding.dimension != selected_generation.dimension
                            ):
                                raise MemoryValidationError(
                                    "embedding provider identity or dimension mismatch"
                                )
                            await self.repository.attach_embedding(
                                job.issuer,
                                job.subject,
                                record.id,
                                revision_id=record.current_revision_id,
                                generation_id=generation_id,
                                vector=embedding.vector,
                                digest=embedding.digest,
                                model_id=embedding.model_id,
                                model_revision=embedding.model_revision,
                                model_digest=embedding.model_digest,
                                scope_type=record.scope.type,
                                agent_profile_id=record.scope.agent_profile_id,
                            )
                            await self._embedding_jobs.settle(
                                embedding_job, issuer=job.issuer, subject=job.subject
                            )
                except Exception:
                    # The accepted memory remains durable; the embedding job
                    # remains retryable for maintenance and is not a second
                    # extraction decision.
                    pass
            await settle_evidence_rejection()
            return prior

        try:
            config = await self.model_configuration(job.issuer, job.subject)
            if await self._selected_generation(job.issuer, job.subject, config) is None:
                await settle_parked("embedding_generation")
                return None
        except MemoryNotFound:
            # Configuration is owner state, not candidate evidence.  Park the
            # job with metadata-only retry state and avoid loading content or
            # invoking either provider until maintenance can retry it.
            await settle_parked("unconfigured")
            self._emit(
                "memory.error",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="queue",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                job_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="queue",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                job_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="queue",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            return None

        if self._evidence_loader is None:
            await settle_parked("evidence_loader")
            return None
        try:
            evidence = await self._evidence_loader(job)
        except MemoryValidationError:
            await settle_evidence_rejection()
            self._emit(
                "memory.error",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="validation",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="validation",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            return None
        if isinstance(evidence, MemoryTurnEvidence):
            try:
                evidence.validate_for(job)
            except MemoryValidationError:
                # Evidence mismatches are terminal for this attempt and must
                # never reach an inference provider.  Keep the outcome
                # content-free so callers can safely acknowledge the job.
                await settle_evidence_rejection()
                self._emit(
                    "memory.error",
                    job_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                )
                self._emit(
                    "memory.job",
                    job_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                    backlog=len(self.jobs),
                )
                return None
            user_content, assistant_content = evidence.user_content, evidence.assistant_content
            message_ids = frozenset(evidence.user_message_ids)
            # Provenance is reconstructed from the server-verified envelope,
            # never from provider output.  It is stable across retries and
            # contains identifiers/digests only (not turn text).
            provenance = tuple(
                MemoryProvenance(
                    uuid5(
                        MEMORY_ID_NAMESPACE,
                        f"provenance:{job.id}:{message_id}:{evidence.evidence_digest}",
                    ),
                    "conversation_message",
                    source_id=message_id,
                    conversation_id=job.conversation_id,
                    run_id=job.run_id,
                    message_id=message_id,
                    observed_at=self._now(),
                    evidence_digest=evidence.evidence_digest,
                )
                for message_id in sorted(evidence.user_message_ids, key=str)
            )
        else:
            # Keep a narrow compatibility seam for the in-process conversation
            # port.  Production loaders return MemoryTurnEvidence, while this
            # tuple form is accepted only when the job has no digest to verify.
            # When a digest is present, validate it before invoking inference.
            if not isinstance(evidence, (tuple, list)) or len(evidence) != 2:  # pyright: ignore[reportUnnecessaryIsInstance]
                raise MemoryValidationError("server-validated memory evidence is required")
            user_content, assistant_content = (str(evidence[0]), str(evidence[1]))
            if job.evidence_digest is not None:
                digest = hashlib.sha256(user_content.encode()).hexdigest()
                if digest != job.evidence_digest:
                    await settle_evidence_rejection()
                    self._emit(
                        "memory.error",
                        job_started,
                        trace_id=trace_id,
                        outcome="error",
                        error_class="validation",
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.job",
                        job_started,
                        trace_id=trace_id,
                        outcome="error",
                        error_class="validation",
                        attempt_count=job.attempt_count,
                        backlog=len(self.jobs),
                    )
                    return None
            message_ids = frozenset(job.user_message_ids)
            provenance = tuple(
                MemoryProvenance(
                    uuid5(
                        MEMORY_ID_NAMESPACE,
                        f"provenance:{job.id}:{message_id}:{job.evidence_digest or ''}",
                    ),
                    "conversation_message",
                    source_id=message_id,
                    conversation_id=job.conversation_id,
                    run_id=job.run_id,
                    message_id=message_id,
                    observed_at=self._now(),
                    evidence_digest=job.evidence_digest,
                )
                for message_id in sorted(job.user_message_ids, key=str)
            )
        result = await self.process(
            job,
            user_content=user_content,
            assistant_content=assistant_content,
            user_message_ids=message_ids,
            run_agent_profile_id=job.agent_profile_id,
            provenance=provenance,
            allow_shared_user_promotion=job.allow_shared_user_promotion,
        )
        capability = lease_id or job.lease_id
        if capability is not None:
            settled = await self.repository.settle_processing_job(
                job.id,
                capability,
                issuer=job.issuer,
                subject=job.subject,
                retryable=result.state is CandidateState.RETRYABLE,
                error_class="provider" if result.state is CandidateState.RETRYABLE else None,
            )
            self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled
        return result

    async def process_command(self, command: MemoryProcessingCommand) -> MemoryProcessingSettlement:
        """Validate an identifier envelope and return durable settlement."""

        loader = self.repository.get_processing_job
        # The command is identifier-only; production resolves the owner from
        # the authenticated run loader before using the owner-required SQL
        # lookup.  This prevents an ownerless repository read.
        if self._job_loader is not None:
            job = await self._job_loader(command.job_id)
        else:
            try:
                cached = next(
                    (item for item in self.jobs.values() if item.id == command.job_id), None
                )
                if cached is None:
                    raise MemoryNotFound("memory processing job not found")
                job = await loader(command.job_id, cached.issuer, cached.subject)
            except MemoryNotFound:
                raise
        if (
            job.id != command.job_id
            or job.run_id != command.run_id
            or job.conversation_id != command.conversation_id
            or job.correlation_id != command.correlation_id
            or job.causation_id != command.causation_id
            or job.agent_revision_id != command.agent_revision_id
            or command.user_message_id not in job.user_message_ids
            or command.assistant_message_id not in job.assistant_message_ids
        ):
            raise MemoryValidationError("memory command does not match durable job")
        result = await self.process_job(command.job_id)
        settled_job = job
        if job.issuer and job.subject:
            try:
                settled_job = await loader(command.job_id, job.issuer, job.subject)
            except MemoryNotFound:
                pass
        else:
            settled_job = next(
                (item for item in self.jobs.values() if item.id == command.job_id),
                job,
            )
        terminal = settled_job.status in {ProcessingJobStatus.COMPLETED, ProcessingJobStatus.FAILED}
        return MemoryProcessingSettlement(
            command.command_id,
            command.job_id,
            settled_job.status,
            terminal,
            result,
        )

    async def maintain(self, issuer: str, subject: str, *, now: datetime | None = None) -> int:
        return await self._maintenance.maintain(issuer, subject, now=now)


MemoryProcessor = MemoryProcessingService
