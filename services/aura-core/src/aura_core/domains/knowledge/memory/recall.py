"""Deterministic, owner-scoped hybrid memory retrieval.

This module owns retrieval policy and ranking only.  Storage adapters expose
lexical/vector candidate ports; no provider or ORM implementation is imported
here.  Returned records are evidence and must be rendered by the context
assembler as an explicitly untrusted component.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, cast
from uuid import UUID

from aura_core.domains.interaction.agents.public import MemoryPolicy
from aura_core.domains.knowledge.memory.public import (
    MemoryEmbeddingGeneration,
    MemoryFilters,
    MemoryLifecycleStatus,
    MemoryRecord,
    MemoryRepository,
    MemoryScopeType,
)

RRF_K = 60
RETRIEVAL_VERSION = "memory-retrieval-v1"
MAX_CANDIDATES_PER_CHANNEL = 50
MAX_RECALLED_MEMORIES = 8
ORDINARY_STATUSES = frozenset({MemoryLifecycleStatus.ACTIVE})
HISTORICAL_STATUSES = frozenset(MemoryLifecycleStatus)


@dataclass(frozen=True, slots=True)
class MemoryQueryEmbedding:
    vector: tuple[float, ...]
    generation_id: UUID
    model_id: str
    model_revision: str | None
    dimension: int
    # ``digest`` identifies this query vector; ``model_digest`` identifies the
    # selected embedding model revision and must match the active generation.
    digest: str | None = None
    model_digest: str | None = None

    def __post_init__(self) -> None:
        if self.dimension != len(self.vector) or self.dimension < 1:
            raise ValueError("query embedding dimension is invalid")
        if not all(math.isfinite(item) for item in self.vector):
            raise ValueError("query embedding contains a non-finite value")
        if self.digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("query embedding digest must be sha256")
        if self.model_digest is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.model_digest
        ):
            raise ValueError("query embedding model digest must be sha256")


class MemoryQueryEmbeddingPort(Protocol):
    async def embed_query(
        self, query: str, generation: MemoryEmbeddingGeneration
    ) -> MemoryQueryEmbedding: ...


@dataclass(frozen=True, slots=True)
class MemoryRecallTelemetryEvent:
    """Content-free stage measurement emitted through the inward telemetry port."""

    operation: str
    duration_ms: float
    trace_id: str
    outcome: str
    dependency: str = "memory_store"
    parent_span_id: str | None = None
    retrieval_stage: str | None = None
    degradation: str | None = None
    error_class: str | None = None
    candidate_count: int | None = None
    recall_count: int | None = None
    fallback_outcome: str | None = None
    context_tokens: int | None = None
    memory_id: str | None = None
    memory_revision_id: str | None = None
    memory_policy_revision_id: str | None = None
    generation_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    retrieval_version: str = RETRIEVAL_VERSION


class MemoryRecallTelemetryPort(Protocol):
    """Best-effort, metadata-only telemetry callback owned by composition."""

    def record(self, event: MemoryRecallTelemetryEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    issuer: str
    subject: str
    agent_profile_id: UUID
    query: str
    policy: MemoryPolicy
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    context_token_budget: int = 4096
    historical: bool = False
    requested_historical_statuses: frozenset[MemoryLifecycleStatus] = frozenset()
    query_embedding: MemoryQueryEmbedding | None = None
    # Execution trace links are metadata-only and allow retrieval spans to be
    # nested under the run's context-assembly span without carrying content.
    run_id: UUID | None = None
    conversation_id: UUID | None = None
    trace_id: str | None = None
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("memory recall query must not be empty")
        if self.context_token_budget < 1:
            raise ValueError("context token budget must be positive")
        if self.policy.agent_profile_id != self.agent_profile_id:
            raise PermissionError("memory policy belongs to a different agent")


@dataclass(frozen=True, slots=True)
class MemoryRecallCandidate:
    memory_id: UUID
    revision_id: UUID
    content: str
    scope_type: MemoryScopeType
    agent_profile_id: UUID | None
    provenance_ids: tuple[UUID, ...]
    lexical_score: float
    vector_score: float
    lexical_rank: int | None
    vector_rank: int | None
    reciprocal_rank_score: float
    relevance: float
    importance: float
    confidence: float
    validity_score: float
    scope_score: float
    final_score: float
    embedding_generation_id: UUID | None

    def metadata(self) -> dict[str, object]:
        """Content-free run metadata suitable for durable recall recording."""

        return {
            "memoryId": str(self.memory_id),
            "revisionId": str(self.revision_id),
            "lexicalScore": self.lexical_score,
            "vectorScore": self.vector_score,
            "rrfScore": self.reciprocal_rank_score,
            "relevance": self.relevance,
            "importance": self.importance,
            "confidence": self.confidence,
            "validity": self.validity_score,
            "scope": self.scope_type.value,
            "agentProfileId": str(self.agent_profile_id) if self.agent_profile_id else None,
            "provenanceIds": [str(item) for item in self.provenance_ids],
            "embeddingGenerationId": str(self.embedding_generation_id)
            if self.embedding_generation_id
            else None,
            "retrievalVersion": RETRIEVAL_VERSION,
        }


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    candidates: tuple[MemoryRecallCandidate, ...]
    fallback_used: bool
    degraded: bool
    degradation_reason: str | None
    embedding_generation_id: UUID | None
    policy_revision_id: UUID
    retrieval_version: str = RETRIEVAL_VERSION

    @property
    def memories(self) -> tuple[MemoryRecallCandidate, ...]:
        return self.candidates

    @property
    def metadata(self) -> tuple[Mapping[str, object], ...]:
        return tuple(item.metadata() for item in self.candidates)


class MemoryRecallRepository(Protocol):
    async def get_active_embedding_generation(
        self, issuer: str, subject: str
    ) -> MemoryEmbeddingGeneration | None: ...

    async def search_lexical(
        self,
        issuer: str,
        subject: str,
        *,
        query: str,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        now: datetime,
        historical: bool,
        statuses: frozenset[MemoryLifecycleStatus],
        generation: MemoryEmbeddingGeneration,
        limit: int = MAX_CANDIDATES_PER_CHANNEL,
    ) -> Sequence[MemoryRecord]: ...

    async def search_vector(
        self,
        issuer: str,
        subject: str,
        *,
        vector: tuple[float, ...],
        generation: MemoryEmbeddingGeneration,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        now: datetime,
        historical: bool,
        statuses: frozenset[MemoryLifecycleStatus],
        limit: int = MAX_CANDIDATES_PER_CHANNEL,
    ) -> Sequence[MemoryRecord]: ...


def _is_valid(record: MemoryRecord, now: datetime, historical: bool) -> bool:
    if not historical and record.status is not MemoryLifecycleStatus.ACTIVE:
        return False
    revision = record.current_revision
    if not historical:
        if revision.valid_from is not None and revision.valid_from > now:
            return False
        if revision.valid_to is not None and revision.valid_to < now:
            return False
    return True


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else -1.0


def _lexical_score(query: str, content: str) -> float:
    terms = {item for item in re.findall(r"[\w'-]+", query.casefold()) if item}
    if not terms:
        return 0.0
    words = set(re.findall(r"[\w'-]+", content.casefold()))
    return len(terms & words) / len(terms)


class MemoryRecallService:
    """Hybrid lexical/vector recall with strict deterministic scope gates."""

    def __init__(
        self,
        # The in-memory public store intentionally exposes only the mutation
        # repository and is supported by the deterministic fallback path;
        # SQL-capable production stores implement the narrow recall protocol.
        repository: MemoryRepository | MemoryRecallRepository,
        *,
        embedding_port: MemoryQueryEmbeddingPort | None = None,
        telemetry: MemoryRecallTelemetryPort | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_port = embedding_port
        self.telemetry = telemetry

    def _emit(
        self,
        request: MemoryRecallRequest,
        operation: str,
        started: float,
        *,
        outcome: str,
        dependency: str = "memory_store",
        retrieval_stage: str | None = None,
        degradation: str | None = None,
        error_class: str | None = None,
        candidate_count: int | None = None,
        recall_count: int | None = None,
        fallback_outcome: str | None = None,
        context_tokens: int | None = None,
        generation_id: str | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        event = MemoryRecallTelemetryEvent(
            operation=operation,
            duration_ms=max(0.0, (monotonic() - started) * 1000),
            trace_id=request.trace_id
            or (request.run_id.hex if request.run_id else "memory-recall"),
            parent_span_id=request.parent_span_id,
            outcome=outcome,
            dependency=dependency,
            retrieval_stage=retrieval_stage,
            degradation=degradation,
            error_class=error_class,
            candidate_count=candidate_count,
            recall_count=recall_count,
            fallback_outcome=fallback_outcome,
            context_tokens=context_tokens,
            memory_policy_revision_id=str(request.policy.id),
            generation_id=generation_id,
            run_id=str(request.run_id) if request.run_id else None,
            conversation_id=str(request.conversation_id) if request.conversation_id else None,
        )
        try:
            self.telemetry.record(event)
        except Exception:
            # Telemetry is advisory and must never affect conversational recall.
            return

    async def recall(self, request: MemoryRecallRequest) -> MemoryRecallResult:
        started = monotonic()
        generation: MemoryEmbeddingGeneration | None = None
        if hasattr(self.repository, "get_active_embedding_generation"):
            source = cast(MemoryRecallRepository, self.repository)
            try:
                generation = await source.get_active_embedding_generation(
                    request.issuer, request.subject
                )  # type: ignore[attr-defined]
            except Exception as exc:
                self._emit(
                    request,
                    "degradation",
                    started,
                    outcome="degraded",
                    dependency="postgresql",
                    retrieval_stage="degradation",
                    degradation="database",
                    error_class=type(exc).__name__,
                )
                self._emit(
                    request,
                    "retrieval",
                    started,
                    outcome="degraded",
                    retrieval_stage="primary",
                    degradation="database",
                    error_class=type(exc).__name__,
                )
                return MemoryRecallResult(
                    (),
                    False,
                    True,
                    "active_embedding_generation_unavailable",
                    None,
                    request.policy.id,
                )
        query_embedding = request.query_embedding
        if query_embedding is None and generation is not None and self.embedding_port is not None:
            embedding_started = monotonic()
            try:
                query_embedding = await self.embedding_port.embed_query(request.query, generation)
                self._emit(
                    request,
                    "query_embedding",
                    embedding_started,
                    outcome="ok",
                    dependency="embedding_provider",
                    retrieval_stage="query_embedding",
                    generation_id=str(generation.id),
                )
            except Exception:
                self._emit(
                    request,
                    "query_embedding",
                    embedding_started,
                    outcome="error",
                    dependency="embedding_provider",
                    retrieval_stage="query_embedding",
                    degradation="query_embedding",
                    error_class="provider",
                    generation_id=str(generation.id),
                )
                self._emit(
                    request,
                    "retrieval",
                    started,
                    outcome="degraded",
                    dependency="embedding_provider",
                    retrieval_stage="primary",
                    degradation="query_embedding",
                    error_class="provider",
                    generation_id=str(generation.id),
                )
                return MemoryRecallResult(
                    (), False, True, "query_embedding_unavailable", None, request.policy.id
                )
        if generation is None or query_embedding is None:
            self._emit(
                request,
                "degradation",
                started,
                outcome="degraded",
                dependency="embedding_provider",
                retrieval_stage="degradation",
                degradation="database",
                error_class="generation",
            )
            self._emit(
                request,
                "retrieval",
                started,
                outcome="degraded",
                dependency="embedding_provider",
                retrieval_stage="primary",
                degradation="database",
                error_class="generation",
            )
            return MemoryRecallResult(
                (), False, True, "active_embedding_generation_unavailable", None, request.policy.id
            )
        if (
            query_embedding.generation_id != generation.id
            or query_embedding.dimension != generation.dimension
            or query_embedding.model_id != generation.model_id
            or query_embedding.model_revision != generation.model_revision
            or query_embedding.model_digest != generation.model_digest
        ):
            self._emit(
                request,
                "degradation",
                started,
                outcome="degraded",
                retrieval_stage="degradation",
                degradation="query_embedding",
                error_class="identity_mismatch",
                generation_id=str(generation.id),
            )
            self._emit(
                request,
                "retrieval",
                started,
                outcome="degraded",
                retrieval_stage="primary",
                degradation="query_embedding",
                error_class="identity_mismatch",
                generation_id=str(generation.id),
            )
            return MemoryRecallResult(
                (), False, True, "query_embedding_identity_mismatch", None, request.policy.id
            )

        primary_scopes: tuple[tuple[MemoryScopeType, UUID | None], ...] = tuple(
            scope
            for scope, enabled in (
                ((MemoryScopeType.USER, None), request.policy.shared_user_read),
                (
                    (MemoryScopeType.AGENT, request.agent_profile_id),
                    request.policy.current_agent_read,
                ),
            )
            if enabled
        )
        try:
            candidates = await self._search(
                request, primary_scopes, generation, query_embedding, historical=request.historical
            )
        except Exception as exc:
            self._emit(
                request,
                "degradation",
                started,
                outcome="degraded",
                dependency="memory_store",
                retrieval_stage="degradation",
                degradation="database",
                error_class=type(exc).__name__,
                generation_id=str(generation.id),
            )
            self._emit(
                request,
                "retrieval",
                started,
                outcome="degraded",
                retrieval_stage="primary",
                degradation="database",
                error_class=type(exc).__name__,
                generation_id=str(generation.id),
            )
            return MemoryRecallResult(
                (), False, True, "retrieval_unavailable", generation.id, request.policy.id
            )
        fallback_used = False
        if not request.historical:
            fallback_started = monotonic()
            fallback_outcome = "not_needed"
            fallback_error: str | None = None
            fallback_candidates: list[MemoryRecallCandidate] = []
            if self._primary_below_threshold(
                candidates, request.policy.fallback_relevance_threshold
            ):
                fallback_scopes = tuple(
                    (MemoryScopeType.AGENT, item)
                    for item in request.policy.fallback_agent_profile_ids
                    if item != request.agent_profile_id
                )
                if not fallback_scopes:
                    fallback_outcome = "not_granted"
                else:
                    try:
                        fallback_candidates = await self._search(
                            request, fallback_scopes, generation, query_embedding, historical=False
                        )
                        fallback_used = bool(fallback_candidates)
                        fallback_outcome = "used" if fallback_used else "empty"
                    except Exception as exc:
                        fallback_error = type(exc).__name__
                        fallback_outcome = "error"
                if fallback_outcome != "error":
                    candidates = self._merge(candidates, fallback_candidates)
            self._emit(
                request,
                "fallback",
                fallback_started,
                outcome="error" if fallback_outcome == "error" else "ok",
                dependency="memory_store",
                retrieval_stage="fallback",
                degradation="database" if fallback_outcome == "error" else None,
                error_class=fallback_error,
                fallback_outcome=fallback_outcome,
                candidate_count=len(fallback_candidates),
                generation_id=str(generation.id),
            )
        selection_started = monotonic()
        selected = self._select(candidates, request)
        self._emit(
            request,
            "selection",
            selection_started,
            outcome="ok",
            retrieval_stage="selection",
            candidate_count=len(candidates),
            generation_id=str(generation.id),
        )
        self._emit(
            request,
            "retrieval",
            started,
            outcome="ok",
            retrieval_stage="primary",
            candidate_count=len(candidates),
            generation_id=str(generation.id),
        )
        return MemoryRecallResult(
            tuple(selected), fallback_used, False, None, generation.id, request.policy.id
        )

    @staticmethod
    def _primary_below_threshold(
        candidates: Sequence[MemoryRecallCandidate], threshold: float
    ) -> bool:
        return not candidates or max(item.relevance for item in candidates) < threshold

    async def _search(
        self,
        request: MemoryRecallRequest,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        generation: MemoryEmbeddingGeneration,
        query_embedding: MemoryQueryEmbedding,
        *,
        historical: bool,
    ) -> list[MemoryRecallCandidate]:
        statuses = (
            request.requested_historical_statuses or HISTORICAL_STATUSES
            if historical
            else ORDINARY_STATUSES
        )
        if hasattr(self.repository, "search_lexical") and hasattr(self.repository, "search_vector"):
            source = cast(MemoryRecallRepository, self.repository)
            lexical_started = monotonic()
            try:
                lexical = await source.search_lexical(
                    request.issuer,
                    request.subject,
                    query=request.query,
                    scopes=scopes,
                    generation=generation,
                    now=request.now,
                    historical=historical,
                    statuses=frozenset(statuses),
                    limit=MAX_CANDIDATES_PER_CHANNEL,
                )
                self._emit(
                    request,
                    "lexical",
                    lexical_started,
                    outcome="ok",
                    dependency="postgresql",
                    retrieval_stage="lexical",
                    candidate_count=len(lexical),
                    generation_id=str(generation.id),
                )
                vector_started = monotonic()
                vector = await source.search_vector(
                    request.issuer,
                    request.subject,
                    vector=query_embedding.vector,
                    generation=generation,
                    scopes=scopes,
                    now=request.now,
                    historical=historical,
                    statuses=frozenset(statuses),
                    limit=MAX_CANDIDATES_PER_CHANNEL,
                )
                self._emit(
                    request,
                    "vector",
                    vector_started,
                    outcome="ok",
                    dependency="postgresql",
                    retrieval_stage="vector",
                    candidate_count=len(vector),
                    generation_id=str(generation.id),
                )
            except AttributeError:
                # The decorator is shared with the in-memory test seam.  Its
                # broad persistence port need not implement SQL retrieval;
                # fall back to the bounded deterministic adapter path.
                lexical, vector = await self._memory_search(
                    request, scopes, generation, query_embedding, historical, statuses
                )
        else:
            channel_started = monotonic()
            lexical, vector = await self._memory_search(
                request, scopes, generation, query_embedding, historical, statuses
            )
            self._emit(
                request,
                "lexical",
                channel_started,
                outcome="ok",
                dependency="memory_store",
                retrieval_stage="lexical",
                candidate_count=len(lexical),
                generation_id=str(generation.id),
            )
            self._emit(
                request,
                "vector",
                channel_started,
                outcome="ok",
                dependency="memory_store",
                retrieval_stage="vector",
                candidate_count=len(vector),
                generation_id=str(generation.id),
            )
        fused_started = monotonic()
        fused = self._fuse(lexical, vector, request.query, request.now, generation.id, historical)
        self._emit(
            request,
            "fusion",
            fused_started,
            outcome="ok",
            retrieval_stage="fusion",
            candidate_count=len(fused),
            generation_id=str(generation.id),
        )
        rerank_started = monotonic()
        result = self._rerank(fused)
        self._emit(
            request,
            "rerank",
            rerank_started,
            outcome="ok",
            retrieval_stage="rerank",
            candidate_count=len(result),
            generation_id=str(generation.id),
        )
        return result

    async def _memory_search(
        self,
        request: MemoryRecallRequest,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        generation: MemoryEmbeddingGeneration,
        query_embedding: MemoryQueryEmbedding,
        historical: bool,
        statuses: Iterable[MemoryLifecycleStatus],
    ) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
        source = cast(MemoryRepository, self.repository)
        values = await source.list_memories(
            request.issuer,
            request.subject,
            MemoryFilters(include_all_scopes=True, include_historical=historical, limit=100_000),
        )
        allowed = set(scopes)
        filtered = [
            item
            for item in values
            if (item.scope.type, item.scope.agent_profile_id) in allowed
            and item.status in set(statuses)
            and _is_valid(item, request.now, historical)
        ]
        vectors: list[tuple[float, MemoryRecord]] = []
        eligible: list[MemoryRecord] = []
        for item in filtered:
            matching = [
                embedding
                for embedding in item.embeddings
                if embedding.revision_id == item.current_revision_id
                and embedding.generation_id == generation.id
                and embedding.dimension == generation.dimension
                and embedding.model_id == generation.model_id
                and embedding.model_revision == generation.model_revision
                and embedding.model_digest == generation.model_digest
                and embedding.vector is not None
            ]
            if matching:
                eligible.append(item)
                vectors.append((_cosine(query_embedding.vector, matching[0].vector or ()), item))
        lexical = [
            item
            for item in sorted(
                eligible,
                key=lambda item: (-_lexical_score(request.query, item.content), str(item.id)),
            )
            if _lexical_score(request.query, item.content) > 0
        ][:MAX_CANDIDATES_PER_CHANNEL]
        vectors.sort(key=lambda pair: (-pair[0], str(pair[1].id)))
        return lexical, [item for _, item in vectors[:MAX_CANDIDATES_PER_CHANNEL]]

    def _fuse(
        self,
        lexical: Sequence[MemoryRecord],
        vector: Sequence[MemoryRecord],
        query: str,
        now: datetime,
        generation_id: UUID,
        historical: bool,
    ) -> list[MemoryRecallCandidate]:
        lexical_ranks = {item.id: rank for rank, item in enumerate(lexical, 1)}
        vector_ranks = {item.id: rank for rank, item in enumerate(vector, 1)}
        records = {item.id: item for item in (*lexical, *vector)}
        result: list[MemoryRecallCandidate] = []
        for memory_id, record in records.items():
            if not _is_valid(record, now, historical):
                continue
            revision = record.current_revision
            lexical_score = (
                _lexical_score(query, record.content) if memory_id in lexical_ranks else 0.0
            )
            vector_score = 0.0
            if memory_id in vector_ranks:
                vector_score = 1.0 / vector_ranks[memory_id]
            rrf = (1 / (RRF_K + lexical_ranks[memory_id]) if memory_id in lexical_ranks else 0) + (
                1 / (RRF_K + vector_ranks[memory_id]) if memory_id in vector_ranks else 0
            )
            relevance = record.relevance(now)
            validity = (
                1.0
                if revision.valid_to is None
                else max(0.0, min(1.0, (revision.valid_to - now).total_seconds() / 86400))
            )
            scope_score = 1.0 if record.scope.type is MemoryScopeType.AGENT else 0.95
            final = (
                rrf * 100
                + relevance * 3
                + revision.importance * 2
                + revision.confidence
                + validity
                + scope_score
            )
            result.append(
                MemoryRecallCandidate(
                    record.id,
                    revision.id,
                    revision.content,
                    record.scope.type,
                    record.scope.agent_profile_id,
                    revision.provenance_ids,
                    lexical_score,
                    vector_score,
                    lexical_ranks.get(memory_id),
                    vector_ranks.get(memory_id),
                    rrf,
                    relevance,
                    revision.importance,
                    revision.confidence,
                    validity,
                    scope_score,
                    final,
                    generation_id,
                )
            )
        return result

    @staticmethod
    def _rerank(candidates: Sequence[MemoryRecallCandidate]) -> list[MemoryRecallCandidate]:
        """Apply the deterministic score ordering after reciprocal-rank fusion."""

        return sorted(
            candidates,
            key=lambda item: (-item.final_score, str(item.memory_id), str(item.revision_id)),
        )

    @staticmethod
    def _merge(
        left: Sequence[MemoryRecallCandidate], right: Sequence[MemoryRecallCandidate]
    ) -> list[MemoryRecallCandidate]:
        merged = {item.memory_id: item for item in (*left, *right)}
        return sorted(merged.values(), key=lambda item: (-item.final_score, str(item.memory_id)))

    @staticmethod
    def _select(
        candidates: Sequence[MemoryRecallCandidate], request: MemoryRecallRequest
    ) -> list[MemoryRecallCandidate]:
        limit = min(MAX_RECALLED_MEMORIES, request.policy.max_memories)
        # The budget is a hard ceiling.  Approximate tokens conservatively and
        # never evict the current prompt because memories are optional evidence.
        token_limit = max(
            1, int(request.context_token_budget * request.policy.context_budget_fraction)
        )
        selected: list[MemoryRecallCandidate] = []
        used = 0
        for item in candidates:
            tokens = max(1, math.ceil(len(item.content) / 4))
            if len(selected) >= limit or used + tokens > token_limit:
                continue
            selected.append(item)
            used += tokens
        return selected


__all__ = [
    "MAX_CANDIDATES_PER_CHANNEL",
    "MAX_RECALLED_MEMORIES",
    "MemoryQueryEmbedding",
    "MemoryQueryEmbeddingPort",
    "MemoryRecallCandidate",
    "MemoryRecallRepository",
    "MemoryRecallRequest",
    "MemoryRecallResult",
    "MemoryRecallService",
    "MemoryRecallTelemetryEvent",
    "MemoryRecallTelemetryPort",
    "RRF_K",
    "RETRIEVAL_VERSION",
]
