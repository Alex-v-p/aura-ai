"""Candidate normalization and action execution rules."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID, uuid5

from aura_core.domains.knowledge.memory.contracts import (
    DEFAULT_MEMORY_HALF_LIFE_DAYS,
    DEFAULT_MEMORY_IMPORTANCE,
    MEMORY_ID_NAMESPACE,
    CandidateState,
    MemoryAction,
    MemoryCandidate,
    MemoryKind,
    MemoryProcessingJob,
    MemoryRecord,
    MemoryRetentionBasis,
    MemoryScope,
    MemoryScopeType,
    MemorySensitivity,
    MemoryValidationError,
)
from aura_core.domains.knowledge.memory.repository_ports import MemoryProcessingRepository


class MemoryCandidateExecutionService:
    """Typed candidate mutation, linking, and outcome boundary.

    Processing policy decides which action is valid; this collaborator owns
    the durable action vocabulary so the orchestrator does not discover
    repository methods dynamically or duplicate persistence plumbing.
    """

    def __init__(self, repository: MemoryProcessingRepository) -> None:
        self.repository = repository

    async def create(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        return await self.repository.create_memory(issuer, subject, **kwargs)

    async def reinforce(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        return await self.repository.reinforce_memory(issuer, subject, memory_id, **kwargs)

    async def revise(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        return await self.repository.revise_memory(issuer, subject, memory_id, **kwargs)

    async def transition(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        return await self.repository.set_status(issuer, subject, memory_id, **kwargs)

    async def persist(self, candidate: MemoryCandidate) -> MemoryCandidate:
        return await self.repository.persist_candidate(candidate)

    async def link(self, job_id: UUID, issuer: str, subject: str, memory_id: UUID) -> None:
        await self.repository.link_processing_job_memory(job_id, issuer, subject, memory_id)

    async def record_outcome(
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
        await self.repository.record_action_outcome(
            candidate_id=candidate_id,
            job_id=job_id,
            issuer=issuer,
            subject=subject,
            action=action,
            outcome=outcome,
            memory_id=memory_id,
            revision_id=revision_id,
            error_class=error_class,
        )


def action_from_provider(
    raw: object,
    job: MemoryProcessingJob,
    evidence_handles: Mapping[str, UUID] | None = None,
    run_agent_profile_id: UUID | None = None,
) -> MemoryCandidate:
    """Normalize a structured port result without accepting extra fields."""

    candidate_id = uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}")
    if isinstance(raw, MemoryCandidate):
        resolved_scope = raw.scope
        if raw.scope is not None and raw.scope.type is MemoryScopeType.AGENT:
            resolved_agent = run_agent_profile_id or job.agent_profile_id
            if resolved_agent is None:
                raise MemoryValidationError("agent scope cannot be resolved")
            resolved_scope = MemoryScope(MemoryScopeType.AGENT, resolved_agent)
        provider_requested_review = raw.action is MemoryAction.REVIEW
        action = (
            MemoryAction.CREATE
            if provider_requested_review and raw.content is not None
            else raw.action
        )
        return MemoryCandidate(
            candidate_id,
            job.id,
            job.issuer,
            job.subject,
            action,
            raw.content,
            raw.kind,
            resolved_scope,
            raw.confidence,
            raw.importance
            if raw.importance is not None
            else (DEFAULT_MEMORY_IMPORTANCE if raw.content is not None else None),
            raw.half_life_days
            if raw.half_life_days is not None
            else (DEFAULT_MEMORY_HALF_LIFE_DAYS if raw.content is not None else None),
            raw.valid_to,
            raw.sensitivity,
            raw.grounded_message_ids,
            raw.related_memory_id,
            CandidateState.REVIEW if provider_requested_review else raw.state,
            "provider_requested_review" if provider_requested_review else raw.decision_reason,
            retention_basis=raw.retention_basis,
        )
    payload_method = getattr(raw, "as_payload", None)
    data: Mapping[str, object] | None
    if callable(payload_method):
        data = cast(Mapping[str, object], payload_method())
    elif isinstance(raw, Mapping):
        data = cast(Mapping[str, object], raw)
    else:
        data = cast(Mapping[str, object] | None, getattr(raw, "__dict__", None))
    if not isinstance(data, Mapping):
        raise MemoryValidationError("structured memory action is malformed")
    allowed = {
        "action",
        "content",
        "kind",
        "scope_type",
        "agent_profile_id",
        "confidence",
        "importance",
        "half_life_days",
        "valid_to",
        "sensitivity",
        "retention_basis",
        "grounded_message_ids",
        "grounded_evidence_handles",
        "related_memory_id",
    }
    if set(data) - allowed:
        raise MemoryValidationError("structured memory action contains unsupported fields")
    required = {
        "action",
        "content",
        "kind",
        "scope_type",
        "agent_profile_id",
        "confidence",
        "importance",
        "half_life_days",
        "valid_to",
        "sensitivity",
        "retention_basis",
        "grounded_evidence_handles",
        "related_memory_id",
    }
    if not required <= set(data):
        raise MemoryValidationError("structured memory action is incomplete")
    try:
        action = MemoryAction(str(data.get("action")))
        kind = MemoryKind(str(data["kind"])) if data.get("kind") is not None else None
        scope = None
        if data.get("scope_type") is not None:
            scope_type = MemoryScopeType(str(data["scope_type"]))
            if scope_type is MemoryScopeType.AGENT:
                # Provider UUIDs are proposals only.  Bind the candidate
                # to the server-resolved current run agent.
                resolved_agent = run_agent_profile_id or job.agent_profile_id
                if resolved_agent is None:
                    raise MemoryValidationError("agent scope cannot be resolved")
                scope = MemoryScope(scope_type, resolved_agent)
            else:
                scope = MemoryScope(scope_type)
        raw_handles = data.get("grounded_evidence_handles", ())
        if not isinstance(raw_handles, (list, tuple)):
            raise MemoryValidationError("grounded evidence handles are malformed")
        handle_map = evidence_handles or {}
        typed_handles = cast(list[object] | tuple[object, ...], raw_handles)
        if any(str(item) not in handle_map for item in typed_handles):
            raise MemoryValidationError("grounded evidence handle is unavailable")
        grounded = tuple(handle_map[str(item)] for item in typed_handles)
        # Provider-supplied database IDs are never trusted.  They are
        # accepted only as an ungrounded legacy shape, forcing review.
        raw_grounded = data.get("grounded_message_ids", ())
        if not isinstance(raw_grounded, (list, tuple)):
            raise MemoryValidationError("grounded message identifiers are malformed")
        if raw_grounded:
            grounded = ()
        if data.get("related_memory_id") is not None:
            raise MemoryValidationError("related memory identifiers require server resolution")
        related = None
        valid_to = data.get("valid_to")
        if isinstance(valid_to, str):
            valid_to = datetime.fromisoformat(valid_to)
            if valid_to.tzinfo is None:
                raise MemoryValidationError("valid-to must include timezone")
        elif valid_to is not None:
            raise MemoryValidationError("valid-to must be an RFC3339 string or null")
        content = str(data["content"]) if data.get("content") is not None else None
        retention_basis = MemoryRetentionBasis(str(data["retention_basis"]))
        if action in {MemoryAction.CREATE, MemoryAction.REVIEW}:
            if (
                not content
                or kind is None
                or scope is None
                or data.get("importance") is None
                or data.get("half_life_days") is None
                or not grounded
                or retention_basis is MemoryRetentionBasis.NONE
            ):
                raise MemoryValidationError("memory action proposal is incomplete")
        provider_requested_review = action is MemoryAction.REVIEW
        if provider_requested_review and content is not None:
            action = MemoryAction.CREATE
        return MemoryCandidate(
            candidate_id,
            job.id,
            job.issuer,
            job.subject,
            action,
            content,
            kind,
            scope,
            float(cast(float | int | str, data.get("confidence", 0.0))),
            (
                float(cast(float | int | str, data["importance"]))
                if data.get("importance") is not None
                else (DEFAULT_MEMORY_IMPORTANCE if content is not None else None)
            ),
            (
                float(cast(float | int | str, data["half_life_days"]))
                if data.get("half_life_days") is not None
                else (DEFAULT_MEMORY_HALF_LIFE_DAYS if content is not None else None)
            ),
            valid_to,
            MemorySensitivity(str(data.get("sensitivity", "ordinary"))),
            grounded,
            related,
            CandidateState.REVIEW if provider_requested_review else CandidateState.PROPOSED,
            "provider_requested_review" if provider_requested_review else None,
            retention_basis=retention_basis,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryValidationError("structured memory action is malformed") from exc


__all__ = ["MemoryCandidateExecutionService", "action_from_provider"]
