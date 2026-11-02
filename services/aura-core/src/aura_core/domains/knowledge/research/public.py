# ruff: noqa: E501
# pyright: reportUnusedVariable=false
# pyright: reportUnusedImport=false
# pyright: reportUnusedFunction=false

"""Public application boundary for durable governed research.

The module intentionally keeps provider responses and research content out of
events and telemetry.  Persistence adapters are composed by ``research_uow``;
entrypoints call this facade only.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4, uuid5

from aura_core.domains.artifacts.generated.public import GeneratedArtifactLineage
from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceInvalidation,
    TaskSourceKind,
    decode_cursor,
    encode_cursor,
)

RESEARCH_COMPONENT = "aura.research.coordinator@1"
RESEARCH_ID_NAMESPACE = UUID("9fae2b80-f0c7-4f1d-8e9a-8bd4d7c9a145")
OWNER_SCOPE_NAMESPACE = UUID("d2a0a4e4-1a6d-4cd1-b2e8-5ce2ef4b8f9d")
MAX_QUESTION = 10_000
MAX_SOURCES = 60
MAX_EVIDENCE_EXCERPT = 256 * 1024
# The public field is constrained by the additive Aura API contract.  The
# coordinator also uses a richer internal vocabulary for metadata-only
# telemetry; those values are mapped before a task is persisted or serialized.
RESEARCH_FAILURE_CLASSES = frozenset({
    "provider_disabled", "provider_unavailable", "validation_failed",
    "authorization_denied", "quota_exceeded", "timeout", "execution_failed",
    "cleanup_failed", "cleanup_unknown",
})
_RESEARCH_TELEMETRY_FAILURE_CLASSES = frozenset({
    "validation_failed", "authorization_denied", "conflict", "budget_exhausted",
    "provider_disabled", "provider_unavailable", "destination_blocked",
    "redirect_blocked", "type_rejected", "size_exceeded", "resource_exhausted",
    "prompt_injection_detected", "citation_invalid", "storage_unavailable",
    "deletion_failed", "cleanup_unknown", "snapshot_unavailable", "version_gap",
    "delivery_failed", "queue_unavailable", "cancel_dispatch_failed",
    "settlement_unknown", "timeout",
})
_RESEARCH_FAILURE_TO_PUBLIC = {
    "conflict": "execution_failed",
    "budget_exhausted": "quota_exceeded",
    "destination_blocked": "validation_failed",
    "redirect_blocked": "validation_failed",
    "type_rejected": "validation_failed",
    "size_exceeded": "validation_failed",
    "resource_exhausted": "execution_failed",
    "prompt_injection_detected": "validation_failed",
    "citation_invalid": "validation_failed",
    "storage_unavailable": "provider_unavailable",
    "deletion_failed": "cleanup_failed",
    "snapshot_unavailable": "execution_failed",
    "version_gap": "execution_failed",
    "delivery_failed": "execution_failed",
    "queue_unavailable": "execution_failed",
    "cancel_dispatch_failed": "execution_failed",
    "settlement_unknown": "execution_failed",
}
_OWNER_RE = re.compile(r"^[^\x00\n\r]{1,1024}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_QUESTION_RELATION_RE = re.compile(
    r"^what\s+is\s+the\s+(?P<relation>[a-z][a-z -]{0,63})\s+of\s+"
    r"(?P<object>[A-Za-z0-9][A-Za-z0-9.'’,-]*(?:\s+[A-Za-z0-9][A-Za-z0-9.'’,-]*){0,1})\?$",
    re.IGNORECASE,
)
_DECLARATIVE_RELATION_RE = re.compile(
    r"^(?P<subject>[A-Za-z0-9][A-Za-z0-9.'’,-]*(?:\s+[A-Za-z0-9][A-Za-z0-9.'’,-]*){0,1})\s+is\s+the\s+"
    r"(?P<relation>[A-Za-z][A-Za-z -]{0,63})\s+of\s+"
    r"(?P<object>[A-Za-z0-9][A-Za-z0-9.'’,-]*(?:\s+[A-Za-z0-9][A-Za-z0-9.'’,-]*){0,1})\.$"
)
_ENTITY_TOKEN_RE = re.compile(
    r"(?:[A-Z](?:[A-Za-z0-9]|['’,-](?=[A-Za-z0-9]))*[A-Za-z0-9]|"
    r"[A-Z]|[0-9]+(?:[.,][0-9]+)?%?)$"
)


@dataclass(frozen=True, slots=True)
class _EntityPhrase:
    """Closed normalized v1 answer entity DTO."""

    text: str
    kind: str
    tokens: tuple[str, ...]


def _parse_entity_phrase(value: str) -> _EntityPhrase | None:
    normalized = " ".join(value.split()).strip(" .")
    tokens = tuple(normalized.split())
    if not 1 <= len(tokens) <= 2 or any(
        _ENTITY_TOKEN_RE.fullmatch(token) is None for token in tokens
    ):
        return None
    kind = "numeric" if all(token[0].isdigit() for token in tokens) else "proper_name"
    return _EntityPhrase(normalized, kind, tokens)


@dataclass(frozen=True, slots=True)
class _ClaimRelation:
    subject: _EntityPhrase
    relation: str
    object: _EntityPhrase

    @property
    def relation_key(self) -> str:
        return f"{self.relation.casefold()}_of"

    @property
    def canonical_text(self) -> str:
        return f"{self.subject.text} is the {self.relation} of {self.object.text}."


def _public_failure_class(value: str | None) -> str | None:
    """Map internal failure details to the closed API failure enum."""
    if value is None:
        return None
    if value in RESEARCH_FAILURE_CLASSES:
        return value
    if value in _RESEARCH_FAILURE_TO_PUBLIC:
        return _RESEARCH_FAILURE_TO_PUBLIC[value]
    return None


def _normalize_relation_part(value: str) -> str:
    return " ".join(value.split()).strip(" .")


def _parse_question_relation(question: str) -> tuple[str, _EntityPhrase] | None:
    match = _QUESTION_RELATION_RE.fullmatch(" ".join(question.split()))
    if match is None:
        return None
    relation = _normalize_relation_part(match.group("relation"))
    object_name = _parse_entity_phrase(match.group("object"))
    if not relation or object_name is None:
        return None
    return relation, object_name


def _parse_declarative_relation(value: str) -> _ClaimRelation | None:
    match = _DECLARATIVE_RELATION_RE.fullmatch(value.strip())
    if match is None:
        return None
    subject = _parse_entity_phrase(match.group("subject"))
    relation = _normalize_relation_part(match.group("relation"))
    object_name = _parse_entity_phrase(match.group("object"))
    if subject is None or not relation or object_name is None:
        return None
    return _ClaimRelation(subject, relation, object_name)


def _claim_relation_supported(question: str, claim_text: str, span: str) -> tuple[str, str] | None:
    """Return canonical structured claim data for one closed v1 relation."""
    question_relation = _parse_question_relation(question)
    claim_relation = _parse_declarative_relation(claim_text)
    span_relation = _parse_declarative_relation(span)
    if question_relation is None or claim_relation is None or span_relation is None:
        return None
    relation, object_name = question_relation
    if (
        claim_relation.relation.casefold() != relation.casefold()
        or claim_relation.object.text.casefold() != object_name.text.casefold()
        or span_relation.relation.casefold() != relation.casefold()
        or span_relation.object.text.casefold() != object_name.text.casefold()
        or claim_relation.subject.text.casefold() != span_relation.subject.text.casefold()
    ):
        return None
    return span_relation.canonical_text, span_relation.relation_key


def _telemetry_failure_class(value: str | None, *, outcome: ResearchOutcome | None = None) -> str | None:
    """Return the bounded metadata-only error class for an operation."""
    if outcome is ResearchOutcome.BUDGET_EXHAUSTED and value in {"timeout", "budget_exhausted", "quota_exceeded"}:
        return "budget_exhausted"
    if value == "quota_exceeded":
        return "budget_exhausted"
    if value in _RESEARCH_TELEMETRY_FAILURE_CLASSES:
        return value
    if value in RESEARCH_FAILURE_CLASSES:
        if outcome is ResearchOutcome.BUDGET_EXHAUSTED:
            return "budget_exhausted"
        return "provider_unavailable" if value == "execution_failed" else value
    return None


@dataclass(frozen=True, slots=True)
class _ResearchRequestLimits:
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class _ResearchSearchRequest:
    """Provider-neutral request DTO passed through the inward provider port."""

    query: str
    domain_allowlist: tuple[str, ...]
    request_id: UUID | None = None
    limits: _ResearchRequestLimits = _ResearchRequestLimits(15_000)

    def model_dump(self, *, mode: str = "python", by_alias: bool = False) -> dict[str, object]:
        del mode, by_alias
        return {"kind": "search_request", "query": self.query, "language": "en", "domainAllowlist": self.domain_allowlist, "limits": {"deadlineMs": self.limits.deadline_ms, "maxCandidates": 20}}


@dataclass(frozen=True, slots=True)
class _ResearchStaticFetchRequest:
    target_url: str
    admission_kind: str
    request_id: UUID | None = None
    limits: _ResearchRequestLimits = _ResearchRequestLimits(20_000)

    def model_dump(self, *, mode: str = "python", by_alias: bool = False) -> dict[str, object]:
        del mode, by_alias
        return {"kind": "static_fetch_request", "targetUrl": self.target_url, "admissionKind": self.admission_kind, "limits": {"deadlineMs": self.limits.deadline_ms, "maxRedirects": 5, "maxTransferredBytes": 8 * 1024 * 1024, "maxDecompressedBytes": 8 * 1024 * 1024, "maxExtractedUtf8Bytes": MAX_EVIDENCE_EXCERPT}}


@dataclass(frozen=True, slots=True)
class _ResearchBrowserFetchRequest:
    target_url: str
    admission_kind: str
    justification: str
    request_id: UUID | None = None
    limits: _ResearchRequestLimits = _ResearchRequestLimits(45_000)

    def model_dump(self, *, mode: str = "python", by_alias: bool = False) -> dict[str, object]:
        del mode, by_alias
        return {"kind": "browser_fetch_request", "targetUrl": self.target_url, "admissionKind": self.admission_kind, "justification": self.justification, "limits": {"deadlineMs": self.limits.deadline_ms, "maxRedirects": 5, "maxResponseBytes": 8 * 1024 * 1024, "maxExtractedUtf8Bytes": MAX_EVIDENCE_EXCERPT}}


class ResearchMode(StrEnum):
    QUICK = "quick"
    AUTO = "auto"
    DEEP = "deep"
    AGENT_LED = "agent_led"


class ResearchStage(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    SEARCHING = "searching"
    FETCHING = "fetching"
    VALIDATING_EVIDENCE = "validating_evidence"
    SYNTHESIZING = "synthesizing"
    PERSISTING = "persisting"
    DRAINING = "draining"
    TERMINAL = "terminal"


class ResearchOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


class ResearchFailureClass(StrEnum):
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VALIDATION_FAILED = "validation_failed"
    AUTHORIZATION_DENIED = "authorization_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    EXECUTION_FAILED = "execution_failed"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_UNKNOWN = "cleanup_unknown"


class ResearchContentState(StrEnum):
    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"
    DELETION_FAILED = "deletion_failed"
    CLEANUP_UNKNOWN = "cleanup_unknown"
    DELETED = "deleted"


class ResearchError(RuntimeError):
    pass


class ResearchNotFound(ResearchError):
    pass


class ResearchConflict(ResearchError):
    pass


class ResearchValidationError(ResearchError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    wall_time_seconds: int
    fetched_sources: int
    search_requests: int
    browser_pages: int
    model_calls: int

    def __post_init__(self) -> None:
        if not 1 <= self.wall_time_seconds <= 3600:
            raise ResearchValidationError("wall time budget is out of bounds")
        if not 1 <= self.fetched_sources <= 60:
            raise ResearchValidationError("source budget is out of bounds")
        if not 1 <= self.search_requests <= 20:
            raise ResearchValidationError("search budget is out of bounds")
        if not 0 <= self.browser_pages <= 15:
            raise ResearchValidationError("browser budget is out of bounds")
        if not 1 <= self.model_calls <= 32:
            raise ResearchValidationError("model budget is out of bounds")

    def as_dict(self) -> dict[str, int]:
        return {
            "wallTimeSeconds": self.wall_time_seconds,
            "fetchedSources": self.fetched_sources,
            "searchRequests": self.search_requests,
            "browserPages": self.browser_pages,
            "modelCalls": self.model_calls,
        }

    def within(self, maximum: ResearchBudget) -> bool:
        return all(
            getattr(self, field) <= getattr(maximum, field)
            for field in ("wall_time_seconds", "fetched_sources", "search_requests", "browser_pages", "model_calls")
        )


@dataclass(frozen=True, slots=True)
class ResearchBudgetConsumption:
    wall_time_seconds: int = 0
    fetched_sources: int = 0
    search_requests: int = 0
    browser_pages: int = 0
    model_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "wallTimeSeconds": self.wall_time_seconds,
            "fetchedSources": self.fetched_sources,
            "searchRequests": self.search_requests,
            "browserPages": self.browser_pages,
            "modelCalls": self.model_calls,
        }

    def within(self, budget: ResearchBudget) -> bool:
        return all(
            0 <= getattr(self, field) <= getattr(budget, field)
            for field in (
                "wall_time_seconds",
                "fetched_sources",
                "search_requests",
                "browser_pages",
                "model_calls",
            )
        )


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    source_id: UUID
    task_id: UUID
    label: str
    url: str
    publisher: str | None
    retrieved_at: datetime
    media_type: str
    extraction_method: str
    freshness_at: datetime | None
    excerpts: tuple[str, ...]
    content_sha256: str
    locator: str | None = None

    def citation(self) -> ResearchCitation:
        return ResearchCitation(
            self.source_id, self.label, self.url, self.retrieved_at, self.locator
        )


@dataclass(frozen=True, slots=True)
class ResearchCitation:
    source_id: UUID
    label: str
    url: str
    retrieved_at: datetime
    locator: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "web",
            "sourceId": str(self.source_id),
            "label": self.label,
            "url": self.url,
            "retrievedAt": self.retrieved_at.isoformat(),
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """Closed planning result; provider content never supplies this object."""

    question: str


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """A report claim may only reference bounded spans from one task's evidence."""

    text: str
    source_id: UUID
    span: str


@dataclass(frozen=True, slots=True)
class ResearchSynthesis:
    claims: tuple[ResearchClaim, ...]
    sufficient: bool


class ResearchSynthesisPort(Protocol):
    """Trusted inward port for structured, evidence-linked synthesis.

    Implementations must return claims with exact bounded evidence spans.  The
    coordinator treats the result as untrusted at the boundary and validates
    every identifier, task owner, and span before it can become a citation or
    artifact.  No default implementation is installed in production.
    """

    async def synthesize(
        self, plan: ResearchPlan, evidence: Sequence[ResearchEvidence]
    ) -> ResearchSynthesis | None: ...


class ResearchEntityType(StrEnum):
    """Closed v1 entity types that a resolver may establish."""

    PLACE = "place"
    PERSON = "person"
    ORGANIZATION = "organization"
    PROPER_NAME = "proper_name"
    NUMERIC = "numeric"


class ResearchEntityProvenance(StrEnum):
    """Bounded provenance values for entity resolution results."""

    DETERMINISTIC_V1 = "deterministic_v1"
    CATALOG = "catalog"
    RESOLVER = "resolver"


@dataclass(frozen=True, slots=True)
class ResearchEntityResolution:
    """Typed resolver result; never inferred from token shape alone."""

    candidate: str
    normalized: str
    identity: str
    entity_type: str
    provenance: str
    task_id: UUID
    run_id: str | None = None


class ResearchEntityResolutionPort(Protocol):
    """Inward port for establishing identity and provenance of claim entities."""

    async def resolve(
        self, candidate: str, *, task_id: UUID, run_id: str | None = None
    ) -> ResearchEntityResolution | None: ...


@dataclass(frozen=True, slots=True)
class ResearchCheckpoint:
    id: UUID
    research_id: UUID
    version: int
    stage: ResearchStage
    occurred_at: datetime
    operation: str


@dataclass(frozen=True, slots=True)
class ResearchTask:
    id: UUID
    issuer: str
    subject: str
    question: str
    mode: ResearchMode
    stage: ResearchStage
    outcome: ResearchOutcome | None
    content_state: ResearchContentState
    version: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    admitted_budget: ResearchBudget
    consumed_budget: ResearchBudgetConsumption = ResearchBudgetConsumption()
    extension_accepted: bool = False
    failure_class: str | None = None
    seed_urls: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    evidence: tuple[ResearchEvidence, ...] = ()
    citations: tuple[ResearchCitation, ...] = ()
    artifact_refs: tuple[Mapping[str, object], ...] = ()
    cancellation_requested: bool = False
    deletion_audit_id: UUID | None = None
    checkpoints: tuple[ResearchCheckpoint, ...] = ()
    cancellation_requested_at: datetime | None = None
    deadline_at: datetime | None = None
    capability_snapshot: Mapping[str, object] = MappingProxyType({})
    origin: Mapping[str, object] = MappingProxyType({})
    policy_revision: str = "research.v1"
    synthesized_answer: str | None = None
    delivery_claim_token: str | None = None
    delivery_claim_phase: str | None = None
    delivery_claim_expires_at: datetime | None = None

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject

    @property
    def owner_scope_id(self) -> UUID:
        return uuid5(OWNER_SCOPE_NAMESPACE, f"{self.issuer}:{self.subject}")

    @property
    def task_ref(self) -> str:
        return f"research:{self.id}"

    @property
    def terminal(self) -> bool:
        return self.outcome is not None


@dataclass(frozen=True, slots=True)
class ResearchTerminalDeliverySnapshot:
    """Narrow, owner-authorized terminal view used by completion delivery."""

    research_id: UUID
    issuer: str
    subject: str
    version: int
    outcome: ResearchOutcome
    content_state: ResearchContentState
    failure_class: str | None
    synthesized_answer: str
    citations: tuple[ResearchCitation, ...]
    artifact_refs: tuple[Mapping[str, object], ...]
    origin: Mapping[str, object]
    mode: ResearchMode = ResearchMode.AUTO

    def __post_init__(self) -> None:
        if not self.synthesized_answer or len(self.synthesized_answer.encode("utf-8")) > 32 * 1024:
            raise ResearchValidationError("terminal research answer is missing or too large")
        if len(self.citations) > 32:
            raise ResearchValidationError("terminal research citations exceed the delivery bound")
        if len(self.artifact_refs) > 8:
            raise ResearchValidationError("terminal research artifacts exceed the delivery bound")


@dataclass(frozen=True, slots=True)
class ResearchDeliveryClaim:
    research_id: UUID
    version: int
    token: str
    phase: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResearchAdmission:
    research_id: UUID
    task_ref: str
    mode: ResearchMode
    admission_state: str
    admitted_budget: ResearchBudget
    version: int
    created_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "researchId": str(self.research_id), "taskRef": self.task_ref,
            "mode": self.mode.value, "admissionState": self.admission_state,
            "admittedBudget": self.admitted_budget.as_dict(), "version": self.version,
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    version: int
    updated_at: datetime
    defaults: Mapping[ResearchMode, ResearchBudget]


DEFAULT_BUDGETS: Mapping[ResearchMode, ResearchBudget] = {
    ResearchMode.QUICK: ResearchBudget(90, 6, 2, 1, 4),
    ResearchMode.AUTO: ResearchBudget(600, 15, 5, 3, 8),
    ResearchMode.DEEP: ResearchBudget(1800, 30, 10, 8, 16),
    ResearchMode.AGENT_LED: ResearchBudget(3600, 60, 20, 15, 32),
}


class ResearchRepository(Protocol):
    async def get_settings(self, owner: tuple[str, str]) -> ResearchSettings | None: ...
    async def save_settings(self, owner: tuple[str, str], settings: ResearchSettings, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, result: Mapping[str, object] | None = None) -> ResearchSettings: ...
    async def admit(self, task: ResearchTask, *, idempotency_key: str | None, fingerprint: str) -> ResearchTask: ...
    async def get(self, owner: tuple[str, str], research_id: UUID) -> ResearchTask: ...
    async def save(self, task: ResearchTask, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask: ...
    async def list(self, owner: tuple[str, str], *, mode: ResearchMode | None = None, outcome: ResearchOutcome | None = None, limit: int = 25, offset: int = 0) -> list[ResearchTask]: ...
    async def evidence(self, owner: tuple[str, str], research_id: UUID, *, limit: int = 100, offset: int = 0) -> list[ResearchEvidence]: ...
    async def projection_tasks(self) -> list[ResearchTask]: ...
    async def get_unscoped(self, research_id: UUID) -> ResearchTask: ...
    async def delivery_snapshot(self, owner: tuple[str, str], research_id: UUID, *, expected_version: int, expected_origin: Mapping[str, object] | None = None) -> ResearchTerminalDeliverySnapshot: ...
    async def claim_delivery(self, owner: tuple[str, str], research_id: UUID, *, expected_version: int, phase: str, lease_seconds: float = 60.0) -> ResearchDeliveryClaim: ...
    async def settle_delivery_claim(self, owner: tuple[str, str], claim: ResearchDeliveryClaim) -> None: ...
    async def delivery_claim_active(self, research_id: UUID) -> bool: ...
    async def admit_deletion(self, task: ResearchTask, *, expected_version: int) -> ResearchTask: ...
    async def purge_private_content(self, research_id: UUID) -> None: ...
    async def finalize_deletion(self, task: ResearchTask, *, expected_version: int, artifact_refs: tuple[Mapping[str, object], ...], delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask: ...
    async def record_cleanup(self, research_id: UUID, audit_id: UUID, *, delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, last_error: str | None = None) -> None: ...
    async def get_cleanup(self, research_id: UUID) -> Mapping[str, object] | None: ...
    async def begin_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None = None, capability_enabled: bool = True) -> bool: ...
    async def dispatch_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None, capability_check: Callable[[ResearchTask], bool] | None = None, capability_enabled: bool | None = None, invoke: Callable[[], Any]) -> tuple[bool, Any | None]: ...
    async def settle_operation(self, research_id: UUID, operation: str, request_id: UUID, *, status: str, now: datetime, response: Mapping[str, object] | None = None) -> None: ...
    async def get_operation(self, research_id: UUID, operation: str, request_id: UUID) -> Mapping[str, object] | None: ...
    async def active_operations(self, research_id: UUID) -> tuple[tuple[str, UUID], ...]: ...
    async def record_fallback(self, at: datetime, *, accepted: bool) -> tuple[int, int, float]: ...
    async def get_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, str, UUID, Mapping[str, object]] | None: ...
    async def save_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, command: str, research_id: UUID, source_version: int, result: Mapping[str, object]) -> None: ...
    async def get_settings_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, Mapping[str, object]] | None: ...
    async def save_settings_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, result: Mapping[str, object]) -> None: ...


class ResearchCitationLifecycleQueryPort(Protocol):
    """Read-only owner-scoped lifecycle query for conversation projections."""

    async def citation_lifecycle(
        self, issuer: str, subject: str, research_id: UUID
    ) -> ResearchContentState: ...


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not _OWNER_RE.fullmatch(issuer) or not _OWNER_RE.fullmatch(subject):
        raise ResearchValidationError("invalid owner identity")
    return issuer, subject


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not parsed.hostname:
        raise ResearchValidationError("seed URLs must use public HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_reserved or address.is_multicast):
        raise ResearchValidationError("seed URLs must resolve to a public destination")
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise ResearchValidationError("seed URLs must resolve to a public destination")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _validate_public_destination(value: str) -> str:
    """Revalidate DNS at dispatch, rejecting mixed or private answers."""
    canonical = _canonical_url(value)
    host = urlsplit(canonical).hostname
    if not host:
        raise ResearchValidationError("research destination has no host")
    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise ResearchValidationError("research destination DNS resolution failed") from exc
    if not addresses or any(not address.is_global or address.is_reserved or address.is_multicast for address in addresses):
        raise ResearchValidationError("research destination resolves to a private address")
    return canonical


def _fingerprint(*values: object) -> str:
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _budget_from(value: ResearchBudget | Mapping[str, object] | None, mode: ResearchMode, settings: ResearchSettings) -> ResearchBudget:
    if value is None:
        return settings.defaults[mode]
    if isinstance(value, ResearchBudget):
        result = value
    else:
        try:
            result = ResearchBudget(
                int(cast(Any, value["wallTimeSeconds"])),
                int(cast(Any, value["fetchedSources"])),
                int(cast(Any, value["searchRequests"])),
                int(cast(Any, value["browserPages"])),
                int(cast(Any, value["modelCalls"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchValidationError("invalid research budget") from exc
    if not result.within(DEFAULT_BUDGETS[mode]):
        raise ResearchValidationError("budget exceeds the selected operator envelope")
    return result


class MemoryResearchRepository:
    """Deterministic repository used by tests and explicitly isolated local runs."""

    def __init__(self) -> None:
        self.tasks: dict[UUID, ResearchTask] = {}
        self.idempotency: dict[tuple[str, str, str], tuple[str, UUID]] = {}
        self._lock = asyncio.Lock()
        self.operations: dict[tuple[UUID, str, UUID], dict[str, object]] = {}
        self.command_receipts: dict[tuple[str, str, str], tuple[str, str, UUID, Mapping[str, object]]] = {}
        self.cleanup: dict[UUID, dict[str, object]] = {}
        self.settings: dict[tuple[str, str], ResearchSettings] = {}
        self.settings_command_receipts: dict[tuple[str, str, str], tuple[str, Mapping[str, object]]] = {}
        self.invalidations: list[TaskSourceInvalidation] = []
        self.fallback_cohorts: dict[datetime, list[int]] = {}

    async def get_settings(self, owner: tuple[str, str]) -> ResearchSettings | None:
        async with self._lock:
            return self.settings.get(owner)

    async def save_settings(self, owner: tuple[str, str], settings: ResearchSettings, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, result: Mapping[str, object] | None = None) -> ResearchSettings:
        async with self._lock:
            if idempotency_key:
                key = (*owner, idempotency_key)
                existing = self.settings_command_receipts.get(key)
                if existing is not None:
                    if existing[0] != fingerprint:
                        raise ResearchConflict("idempotency key was reused")
                    return self.settings.get(owner, settings)
                if key in self.command_receipts:
                    raise ResearchConflict("idempotency key was reused")
            current = self.settings.get(owner)
            if current is not None and current.version != expected_version:
                raise ResearchConflict("research settings version conflict")
            self.settings[owner] = settings
            if idempotency_key and fingerprint is not None:
                self.settings_command_receipts[(*owner, idempotency_key)] = (fingerprint, dict(result or {}))
            return settings

    async def admit(self, task: ResearchTask, *, idempotency_key: str | None, fingerprint: str) -> ResearchTask:
        async with self._lock:
            if idempotency_key:
                prior = self.idempotency.get((*task.owner, idempotency_key))
                if prior:
                    if prior[0] != fingerprint:
                        raise ResearchConflict("idempotency key was reused")
                    return self.tasks[prior[1]]
            self.tasks[task.id] = task
            if idempotency_key:
                self.idempotency[(*task.owner, idempotency_key)] = (fingerprint, task.id)
            return task

    async def get(self, owner: tuple[str, str], research_id: UUID) -> ResearchTask:
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is None or task.owner != owner:
                raise ResearchNotFound("research task not found")
            return task

    async def save(self, task: ResearchTask, *, expected_version: int, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask:
        async with self._lock:
            if idempotency_key:
                key = (*task.owner, idempotency_key)
                prior_receipt = self.command_receipts.get(key)
                if prior_receipt is not None:
                    if prior_receipt[0] != fingerprint or prior_receipt[1] != command:
                        raise ResearchConflict("idempotency key was reused")
                    return self.tasks.get(prior_receipt[2], task)
                if key in self.settings_command_receipts:
                    raise ResearchConflict("idempotency key was reused")
            prior = self.tasks.get(task.id)
            if prior is None or prior.owner != task.owner:
                raise ResearchNotFound("research task not found")
            if prior.version != expected_version:
                raise ResearchConflict("research task version conflict")
            self.tasks[task.id] = task
            if idempotency_key and fingerprint is not None and command is not None:
                self.command_receipts[(*task.owner, idempotency_key)] = (fingerprint, command, task.id, dict(result or {}))
            return task

    async def list(self, owner: tuple[str, str], *, mode: ResearchMode | None = None, outcome: ResearchOutcome | None = None, limit: int = 25, offset: int = 0) -> list[ResearchTask]:
        async with self._lock:
            values = [item for item in self.tasks.values() if item.owner == owner and (mode is None or item.mode is mode) and (outcome is None or item.outcome is outcome)]
            values.sort(key=lambda item: (item.created_at, item.id), reverse=True)
            return values[offset : offset + limit]

    async def evidence(self, owner: tuple[str, str], research_id: UUID, *, limit: int = 100, offset: int = 0) -> list[ResearchEvidence]:
        task = await self.get(owner, research_id)
        if task.content_state is not ResearchContentState.ACTIVE:
            return []
        return list(task.evidence[offset : offset + limit])

    async def projection_tasks(self) -> list[ResearchTask]:
        async with self._lock:
            return list(self.tasks.values())

    async def get_unscoped(self, research_id: UUID) -> ResearchTask:
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is None:
                raise ResearchNotFound("research task not found")
            return task

    async def delivery_snapshot(self, owner: tuple[str, str], research_id: UUID, *, expected_version: int, expected_origin: Mapping[str, object] | None = None) -> ResearchTerminalDeliverySnapshot:
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is None or task.owner != owner:
                raise ResearchNotFound("research task not found")
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
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is None or task.owner != owner:
                raise ResearchNotFound("research task not found")
            if task.version != expected_version or not task.terminal:
                raise ResearchConflict("research terminal version is stale")
            if task.content_state is not ResearchContentState.ACTIVE:
                raise ResearchConflict("research content is no longer deliverable")
            now_value = datetime.now(UTC)
            if (
                task.delivery_claim_token
                and task.delivery_claim_expires_at is not None
                and task.delivery_claim_expires_at > now_value
            ):
                raise ResearchConflict("research delivery is already claimed")
            expires = now_value + timedelta(seconds=lease_seconds)
            token = uuid4().hex
            self.tasks[research_id] = replace(
                task,
                delivery_claim_token=token,
                delivery_claim_phase=phase,
                delivery_claim_expires_at=expires,
            )
            return ResearchDeliveryClaim(research_id, expected_version, token, phase, expires)

    async def settle_delivery_claim(
        self, owner: tuple[str, str], claim: ResearchDeliveryClaim
    ) -> None:
        async with self._lock:
            task = self.tasks.get(claim.research_id)
            if task is None or task.owner != owner:
                raise ResearchNotFound("research task not found")
            if task.delivery_claim_token != claim.token:
                raise ResearchConflict("research delivery claim is stale")
            self.tasks[claim.research_id] = replace(
                task,
                delivery_claim_token=None,
                delivery_claim_phase=None,
                delivery_claim_expires_at=None,
            )

    async def delivery_claim_active(self, research_id: UUID) -> bool:
        async with self._lock:
            task = self.tasks.get(research_id)
            return bool(
                task
                and task.delivery_claim_token
                and task.delivery_claim_expires_at is not None
                and task.delivery_claim_expires_at > datetime.now(UTC)
            )

    async def admit_deletion(
        self, task: ResearchTask, *, expected_version: int
    ) -> ResearchTask:
        async with self._lock:
            current = self.tasks.get(task.id)
            if current is None or current.owner != task.owner:
                raise ResearchNotFound("research task not found")
            if current.version != expected_version or not current.terminal:
                raise ResearchConflict("research content is not deletable")
            if current.content_state is not ResearchContentState.ACTIVE:
                return current
            now_value = datetime.now(UTC)
            if (
                current.delivery_claim_token
                and current.delivery_claim_expires_at is not None
                and current.delivery_claim_expires_at > now_value
            ):
                raise ResearchConflict("research delivery is settling")
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
            self.tasks[current.id] = tombstone
            return tombstone

    async def purge_private_content(self, research_id: UUID) -> None:
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is not None:
                self.tasks[research_id] = replace(task, question="", seed_urls=(), domains=(), evidence=(), citations=(), artifact_refs=(), synthesized_answer=None)
            for key, operation in self.operations.items():
                if key[0] == research_id:
                    operation["response"] = {}

    async def finalize_deletion(self, task: ResearchTask, *, expected_version: int, artifact_refs: tuple[Mapping[str, object], ...], delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, idempotency_key: str | None = None, fingerprint: str | None = None, command: str | None = None, result: Mapping[str, object] | None = None) -> ResearchTask:
        async with self._lock:
            if idempotency_key:
                key = (*task.owner, idempotency_key)
                prior_receipt = self.command_receipts.get(key)
                if prior_receipt is not None:
                    if prior_receipt[0] != fingerprint or prior_receipt[1] != command:
                        raise ResearchConflict("idempotency key was reused")
                    return self.tasks.get(prior_receipt[2], task)
                if key in self.settings_command_receipts:
                    raise ResearchConflict("idempotency key was reused")
            prior = self.tasks.get(task.id)
            if prior is None or prior.owner != task.owner:
                raise ResearchNotFound("research task not found")
            if prior.version != expected_version:
                raise ResearchConflict("research task version conflict")
            cleaned = replace(task, question="", seed_urls=(), domains=(), evidence=(), citations=(), artifact_refs=artifact_refs, synthesized_answer=None, content_state=ResearchContentState.DELETED, version=task.version + 1, updated_at=task.updated_at)
            self.tasks[task.id] = cleaned
            for key, operation in self.operations.items():
                if key[0] == task.id:
                    operation["response"] = {}
            self.cleanup[task.id] = {"auditId": task.deletion_audit_id, "deleteLinkedArtifacts": delete_linked_artifacts, "evidenceStatus": evidence_status, "artifactStatus": artifact_status, "status": status, "lastError": None}
            self.invalidations.append(TaskSourceInvalidation(TaskSourceKind.RESEARCH, cleaned.id, cleaned.version, uuid4(), cleaned.updated_at, uuid4(), uuid4()))
            if idempotency_key and fingerprint is not None and command is not None:
                self.command_receipts[(*task.owner, idempotency_key)] = (fingerprint, command, cleaned.id, dict(result or {"sourceVersion": cleaned.version}))
            return cleaned

    async def record_cleanup(self, research_id: UUID, audit_id: UUID, *, delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, last_error: str | None = None) -> None:
        async with self._lock:
            self.cleanup[research_id] = {"auditId": audit_id, "deleteLinkedArtifacts": delete_linked_artifacts, "evidenceStatus": evidence_status, "artifactStatus": artifact_status, "status": status, "lastError": last_error}

    async def get_cleanup(self, research_id: UUID) -> Mapping[str, object] | None:
        async with self._lock:
            value = self.cleanup.get(research_id)
            return dict(value) if value is not None else None

    async def begin_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None = None, capability_enabled: bool = True) -> bool:
        async with self._lock:
            task = self.tasks.get(research_id)
            if task is None or task.terminal or task.cancellation_requested or task.content_state is not ResearchContentState.ACTIVE or not capability_enabled or (deadline_at is not None and now >= deadline_at):
                return False
            key = (research_id, operation, request_id)
            if key in self.operations:
                return False
            self.operations[key] = {"status": "running", "admittedAt": now, "dispatchLease": request_id}
            return True

    async def dispatch_operation(self, research_id: UUID, operation: str, request_id: UUID, *, now: datetime, deadline_at: datetime | None, capability_check: Callable[[ResearchTask], bool] | None = None, capability_enabled: bool | None = None, invoke: Callable[[], Any]) -> tuple[bool, Any | None]:
        async with self._lock:
            task = self.tasks.get(research_id)
            key = (research_id, operation, request_id)
            entry = self.operations.get(key)
            current_capability = capability_check(task) if task is not None and capability_check is not None else bool(capability_enabled)
            if task is None or entry is None or entry.get("status") != "running" or task.terminal or task.cancellation_requested or task.content_state is not ResearchContentState.ACTIVE or not current_capability or (deadline_at is not None and now >= deadline_at):
                if entry is not None and entry.get("status") == "running" and (task is None or task.cancellation_requested or not current_capability):
                    entry["status"] = "canceled"
                return False, None
            entry["dispatchStartedAt"] = now
            # Start the provider coroutine only after the serialized gate has
            # recorded dispatch.  Cancellation may proceed while the provider
            # is in flight; a cancellation committed before this point never
            # creates the task.
            return True, asyncio.create_task(invoke())

    async def settle_operation(self, research_id: UUID, operation: str, request_id: UUID, *, status: str, now: datetime, response: Mapping[str, object] | None = None) -> None:
        async with self._lock:
            entry = self.operations.setdefault((research_id, operation, request_id), {})
            if entry.get("status") not in {None, "running"}:
                return
            task = self.tasks.get(research_id)
            if task is not None and task.terminal:
                return
            entry.update(status=status, settledAt=now, response=dict(response or {}))

    async def get_operation(self, research_id: UUID, operation: str, request_id: UUID) -> Mapping[str, object] | None:
        async with self._lock:
            value = self.operations.get((research_id, operation, request_id))
            return dict(value) if value is not None else None

    async def active_operations(self, research_id: UUID) -> tuple[tuple[str, UUID], ...]:
        async with self._lock:
            return tuple((operation, request_id) for (task_id, operation, request_id), value in self.operations.items() if task_id == research_id and value.get("status") == "running")

    async def record_fallback(self, at: datetime, *, accepted: bool) -> tuple[int, int, float]:
        cohort = _utc(at).replace(minute=(_utc(at).minute // 5) * 5, second=0, microsecond=0)
        async with self._lock:
            counts = self.fallback_cohorts.setdefault(cohort, [0, 0])
            counts[0] += 1
            counts[1] += int(accepted)
            return counts[0], counts[1], counts[1] / counts[0]

    async def get_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, str, UUID, Mapping[str, object]] | None:
        async with self._lock:
            return self.command_receipts.get((*owner, key))

    async def save_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, command: str, research_id: UUID, source_version: int, result: Mapping[str, object]) -> None:
        async with self._lock:
            existing_settings = self.settings_command_receipts.get((*owner, key))
            if existing_settings is not None:
                raise ResearchConflict("idempotency key was reused")
            existing = self.command_receipts.get((*owner, key))
            if existing is not None and (existing[0] != fingerprint or existing[1] != command):
                raise ResearchConflict("idempotency key was reused")
            self.command_receipts[(*owner, key)] = (fingerprint, command, research_id, dict(result))

    async def get_settings_command_receipt(self, owner: tuple[str, str], key: str) -> tuple[str, Mapping[str, object]] | None:
        async with self._lock:
            value = self.settings_command_receipts.get((*owner, key))
            return (value[0], dict(value[1])) if value is not None else None

    async def save_settings_command_receipt(self, owner: tuple[str, str], key: str, *, fingerprint: str, result: Mapping[str, object]) -> None:
        async with self._lock:
            if (*owner, key) in self.command_receipts:
                raise ResearchConflict("idempotency key was reused")
            existing = self.settings_command_receipts.get((*owner, key))
            if existing is not None and existing[0] != fingerprint:
                raise ResearchConflict("idempotency key was reused")
            self.settings_command_receipts[(*owner, key)] = (fingerprint, dict(result))


class ResearchMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...
    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...
    def record_span(self, component: str, operation: str, duration_ms: float, **attributes: object) -> None: ...


_RESEARCH_DEPENDENCY = "core_postgresql"
_RESEARCH_OUTCOME_BY_TASK: Mapping[ResearchOutcome, str] = {
    ResearchOutcome.COMPLETED: "success",
    ResearchOutcome.PARTIAL: "partial",
    ResearchOutcome.BUDGET_EXHAUSTED: "failed",
    ResearchOutcome.FAILED: "failed",
    ResearchOutcome.CANCELED: "canceled",
    ResearchOutcome.INTERRUPTED: "failed",
}


def _emit_research_metric(
    metrics: ResearchMetrics | None,
    component: str,
    metric: str,
    value: float = 1.0,
    *,
    operation: str,
    outcome: str,
    dependency: str | None = _RESEARCH_DEPENDENCY,
    status: str | None = None,
    error_class: str | None = None,
    provider: str | None = None,
    degradation: str | None = None,
) -> None:
    if metrics is None:
        return
    attributes: dict[str, str] = {"operation": operation, "outcome": outcome}
    if dependency is not None:
        attributes["dependency"] = dependency
    for key, candidate in (
        ("status", status),
        ("error_class", error_class),
        ("provider", provider),
        ("degradation", degradation),
    ):
        if candidate is not None:
            attributes[key] = candidate
    try:
        if metric.endswith("_latency_ms") or metric.endswith("_lag_ms") or metric.endswith("_ratio") or metric.endswith("_consumed_ms") or (metric.startswith("research_budget_") and metric.endswith("_count") and metric != "research_budget_exhaustion_count"):
            metrics.observe(component, metric, value, **attributes)
        else:
            metrics.increment(component, metric, **attributes)
    except Exception:
        # Observatory and local sinks are never on the research critical path.
        return


def _emit_research_span(
    metrics: ResearchMetrics | None,
    component: str,
    operation: str,
    duration_ms: float,
    *,
    outcome: str,
    dependency: str | None = _RESEARCH_DEPENDENCY,
    error_class: str | None = None,
    trace_id: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    if metrics is None:
        return
    attributes: dict[str, object] = {"outcome": outcome}
    if dependency is not None:
        attributes["dependency"] = dependency
    if error_class is not None:
        attributes["error_class"] = error_class
    if trace_id is not None:
        attributes.update(
            trace_id=trace_id,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
        )
        if run_id is not None:
            attributes["run_id"] = run_id
        if conversation_id is not None:
            attributes["conversation_id"] = conversation_id
    try:
        metrics.record_span(component, operation, max(0.0, duration_ms), **attributes)
    except Exception:
        return


def _emit_research_budget_metrics(metrics: ResearchMetrics | None, task: ResearchTask) -> None:
    """Emit the durable terminal ledger once, after the save wins."""
    consumed = task.consumed_budget
    fields = (
        ("research_budget_wall_time_consumed_ms", float(consumed.wall_time_seconds * 1000)),
        ("research_budget_sources_consumed_count", float(consumed.fetched_sources)),
        ("research_budget_searches_consumed_count", float(consumed.search_requests)),
        ("research_budget_browser_pages_consumed_count", float(consumed.browser_pages)),
        ("research_budget_model_calls_consumed_count", float(consumed.model_calls)),
    )
    # The terminal ledger records a successfully persisted settlement.  Budget
    # exhaustion is the durable error classification, not a failed metric
    # write: callers must be able to distinguish an exhausted run from an
    # unknown/unsettled run without turning the ledger sample into an unknown
    # outcome.
    outcome = "success"
    error_class = None
    status = "budget_exhausted" if task.outcome is ResearchOutcome.BUDGET_EXHAUSTED else task.outcome.value if task.outcome else "unknown"
    for metric, value in fields:
        _emit_research_metric(
            metrics,
            RESEARCH_COMPONENT,
            metric,
            value,
            operation="research.budget_settle",
            outcome=outcome,
            dependency=None,
            status=status,
            error_class=error_class,
        )
    if task.outcome is ResearchOutcome.BUDGET_EXHAUSTED:
        _emit_research_metric(
            metrics,
            RESEARCH_COMPONENT,
            "research_budget_exhaustion_count",
            operation="research.budget_settle",
            outcome="success",
            dependency=None,
            status="budget_exhausted",
            error_class="budget_exhausted",
        )


class ResearchApplicationService:
    """Owner-authorized admission, lifecycle, and Task Center source API."""

    component = RESEARCH_COMPONENT

    def __init__(self, repository: ResearchRepository, *, settings: ResearchSettings | None = None, provider_clients: object | None = None, provider_settings: object | None = None, artifact_service: object | None = None, admission_enabled: bool = False, metrics: ResearchMetrics | None = None, clock: Any = None) -> None:
        self.repository = repository
        self.settings = settings or ResearchSettings(1, datetime.now(UTC), dict(DEFAULT_BUDGETS))
        self.provider_clients = provider_clients
        self.provider_settings = provider_settings or getattr(provider_clients, "settings", None)
        self.artifact_service = artifact_service
        self.admission_enabled = admission_enabled
        self.metrics = metrics
        self.clock: Callable[[], datetime] = cast(Callable[[], datetime], clock if callable(clock) else lambda: datetime.now(UTC))
        self._projection_capability: object | None = None
        self._owner_settings: dict[tuple[str, str], ResearchSettings] = {}
        self._command_idempotency: dict[tuple[tuple[str, str], str, str], object] = {}

    def _replayed_command(self, owner: tuple[str, str], key: str | None, fingerprint: str) -> object | None:
        if not key:
            return None
        prior = self._command_idempotency.get((owner, key, fingerprint))
        if prior is not None:
            return prior
        if any(item[0] == owner and item[1] == key for item in self._command_idempotency):
            raise ResearchConflict("idempotency key was reused")
        return None

    def _remember_command(self, owner: tuple[str, str], key: str | None, fingerprint: str, value: object) -> None:
        if key:
            self._command_idempotency[(owner, key, fingerprint)] = value

    def _enabled(self) -> bool:
        return bool(getattr(self.provider_clients, "admission_enabled", self.admission_enabled)) if self.provider_clients is not None else self.admission_enabled

    def capability_enabled(self, task: ResearchTask, capability: str) -> bool:
        """Enforce immutable admission permissions plus current narrowing switches."""
        admitted = bool(task.capability_snapshot.get(capability, False))
        settings = cast(Any, getattr(self.provider_clients, "settings", None)) if self.provider_clients is not None else self.provider_settings
        current = bool(getattr(settings, {"search": "search_enabled", "staticFetch": "static_fetch_enabled", "browserFetch": "browser_fetch_enabled"}.get(capability, capability), False))
        return admitted and current

    def settings_for(self, owner: tuple[str, str]) -> ResearchSettings:
        return self._owner_settings.get(owner, self.settings)

    async def get_settings(self, issuer: str, subject: str) -> ResearchSettings:
        owner = _owner(issuer, subject)
        stored = await self.repository.get_settings(owner)
        if stored is not None:
            self._owner_settings[owner] = stored
        return self.settings_for(owner)

    async def replace_settings(
        self,
        issuer: str,
        subject: str,
        *,
        expected_version: int,
        defaults: Mapping[ResearchMode, ResearchBudget],
        idempotency_key: str | None = None,
    ) -> ResearchSettings:
        owner = _owner(issuer, subject)
        fingerprint = _fingerprint("settings", expected_version, {mode.value: value.as_dict() for mode, value in defaults.items()})
        if idempotency_key:
            durable_settings = await self.repository.get_settings_command_receipt(owner, idempotency_key)
            if durable_settings is not None:
                if durable_settings[0] != fingerprint:
                    raise ResearchConflict("idempotency key was reused")
                current = await self.get_settings(issuer, subject)
                return current
        replay = self._replayed_command(owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchSettings, replay)
        current = await self.get_settings(issuer, subject)
        if current.version != expected_version or set(defaults) != set(ResearchMode):
            raise ResearchConflict("research settings version conflict")
        if any(not value.within(DEFAULT_BUDGETS[mode]) for mode, value in defaults.items()):
            raise ResearchValidationError("research defaults exceed operator caps")
        updated = ResearchSettings(current.version + 1, _utc(self.clock()), dict(defaults))
        saved = await self.repository.save_settings(
            owner,
            updated,
            expected_version=current.version,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint if idempotency_key else None,
            result={"version": updated.version} if idempotency_key else None,
        )
        self._owner_settings[owner] = saved
        self._remember_command(owner, idempotency_key, fingerprint, saved)
        return saved

    async def admit(self, issuer: str, subject: str, *, question: str, mode: ResearchMode | str, budget: ResearchBudget | Mapping[str, object] | None = None, seed_urls: Sequence[str] = (), domains: Sequence[str] = (), idempotency_key: str | None = None, origin: Mapping[str, object] | None = None) -> ResearchAdmission:
        owner = _owner(issuer, subject)
        if not isinstance(mode, ResearchMode):
            try:
                mode = ResearchMode(mode)
            except ValueError as exc:
                raise ResearchValidationError("invalid research mode") from exc
        if not question or len(question) > MAX_QUESTION or any(ord(char) < 32 and char not in "\t\n\r" for char in question):
            raise ResearchValidationError("invalid research question")
        if not self._enabled():
            raise ResearchConflict("research admission is disabled")
        seeds = tuple(dict.fromkeys(_canonical_url(value) for value in seed_urls))
        if len(seeds) > 20:
            raise ResearchValidationError("too many seed URLs")
        domain_values = tuple(dict.fromkeys(str(value).lower() for value in domains))
        if len(domain_values) > 20 or any(not _DOMAIN_RE.fullmatch(value) for value in domain_values):
            raise ResearchValidationError("invalid research domain")
        admitted_budget = _budget_from(budget, mode, await self.get_settings(issuer, subject))
        now = _utc(self.clock())
        research_id = uuid5(RESEARCH_ID_NAMESPACE, f"admit:{issuer}:{subject}:{idempotency_key}") if idempotency_key else uuid4()
        provider_settings = cast(Any, getattr(self.provider_clients, "settings", None)) if self.provider_clients is not None else self.provider_settings
        task = ResearchTask(
            research_id,
            owner[0],
            owner[1],
            question,
            mode,
            ResearchStage.QUEUED,
            None,
            ResearchContentState.ACTIVE,
            1,
            now,
            now,
            None,
            admitted_budget,
            seed_urls=seeds,
            domains=domain_values,
            checkpoints=(ResearchCheckpoint(uuid4(), research_id, 1, ResearchStage.QUEUED, now, "admit"),),
            deadline_at=now.replace(microsecond=now.microsecond) + timedelta(seconds=admitted_budget.wall_time_seconds),
            capability_snapshot={
                "admission": self._enabled(),
                "search": bool(provider_settings and getattr(provider_settings, "search_enabled", False)),
                "staticFetch": bool(provider_settings and getattr(provider_settings, "static_fetch_enabled", False)),
                "browserFetch": bool(provider_settings and getattr(provider_settings, "browser_fetch_enabled", False)),
                "settings": {name: getattr(provider_settings, name, None) for name in ("search_deadline_seconds", "static_fetch_deadline_seconds", "browser_fetch_deadline_seconds", "max_candidates", "max_redirects", "max_bytes", "max_extracted_text_bytes") if provider_settings is not None and getattr(provider_settings, name, None) is not None},
            },
            origin=dict(origin or {"surface": "research_api", "issuer": issuer, "subject": subject}),
        )
        fingerprint = _fingerprint(question, mode.value, admitted_budget.as_dict(), seeds, domain_values)
        stored = await self.repository.admit(task, idempotency_key=idempotency_key, fingerprint=fingerprint)
        return ResearchAdmission(stored.id, stored.task_ref, stored.mode, "admitted", stored.admitted_budget, stored.version, stored.created_at)

    async def get(self, issuer: str, subject: str, research_id: UUID) -> ResearchTask:
        return await self.repository.get(_owner(issuer, subject), research_id)

    async def citation_lifecycle(
        self, issuer: str, subject: str, research_id: UUID
    ) -> ResearchContentState:
        task = await self.get(issuer, subject, research_id)
        return task.content_state

    async def delivery_snapshot(
        self,
        issuer: str,
        subject: str,
        research_id: UUID,
        *,
        expected_version: int,
        expected_origin: Mapping[str, object] | None = None,
    ) -> ResearchTerminalDeliverySnapshot:
        """Authorize a terminal delivery against the source-owned version fence."""
        method = getattr(self.repository, "delivery_snapshot", None)
        if not callable(method):
            task = await self.get(issuer, subject, research_id)
            if task.version != expected_version or not task.terminal:
                raise ResearchConflict("research terminal version is stale")
            if task.content_state is not ResearchContentState.ACTIVE:
                raise ResearchConflict("research content is no longer deliverable")
            if expected_origin is not None and dict(task.origin) != dict(expected_origin):
                raise ResearchConflict("research origin fence failed")
            task_snapshot = ResearchTerminalDeliverySnapshot(
                task.id, task.issuer, task.subject, task.version,
                cast(ResearchOutcome, task.outcome), task.content_state,
                task.failure_class, task.synthesized_answer or "Research completed without a persisted answer.",
                tuple(task.citations[:32]), tuple(task.artifact_refs[:8]), dict(task.origin), task.mode,
            )
        else:
            task_snapshot = await cast(Any, method)(
                _owner(issuer, subject), research_id,
                expected_version=expected_version, expected_origin=expected_origin,
            )
        await self._authorize_delivery_artifacts(
            (issuer, subject), research_id, task_snapshot.artifact_refs
        )
        return task_snapshot

    async def _authorize_delivery_artifacts(
        self,
        owner: tuple[str, str],
        research_id: UUID,
        artifact_refs: Sequence[Mapping[str, object]],
    ) -> None:
        """Reauthorize every report reference through the artifact boundary."""

        if len(artifact_refs) > 8:
            raise ResearchConflict("research artifact references exceed the delivery bound")
        if not artifact_refs:
            return
        if self.artifact_service is None:
            raise ResearchConflict("research artifact authorization is unavailable")
        service = cast(Any, self.artifact_service)
        for reference in artifact_refs:
            try:
                artifact_id = UUID(str(reference["artifactId"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchConflict("research artifact reference is invalid") from exc
            try:
                artifact = await service.get(*owner, artifact_id)
            except Exception as exc:
                raise ResearchConflict("research artifact reference is not authorized") from exc
            lifecycle = getattr(getattr(artifact, "lifecycle", None), "value", getattr(artifact, "lifecycle", None))
            lineage = getattr(artifact, "lineage", None)
            if (
                getattr(artifact, "issuer", None) != owner[0]
                or getattr(artifact, "subject", None) != owner[1]
                or lifecycle != "active"
                or getattr(lineage, "task_id", None) != research_id
            ):
                raise ResearchConflict("research artifact reference is not authorized")

    async def claim_delivery(
        self,
        issuer: str,
        subject: str,
        research_id: UUID,
        *,
        expected_version: int,
        phase: str = "delivery",
        lease_seconds: float = 60.0,
    ) -> ResearchDeliveryClaim:
        method = getattr(self.repository, "claim_delivery", None)
        if not callable(method):
            raise ResearchConflict("research delivery claim boundary is unavailable")
        return await cast(Any, method)(
            _owner(issuer, subject), research_id,
            expected_version=expected_version,
            phase=phase,
            lease_seconds=lease_seconds,
        )

    async def settle_delivery_claim(
        self, issuer: str, subject: str, claim: ResearchDeliveryClaim
    ) -> None:
        method = getattr(self.repository, "settle_delivery_claim", None)
        if not callable(method):
            raise ResearchConflict("research delivery claim boundary is unavailable")
        await cast(Any, method)(_owner(issuer, subject), claim)

    async def admit_deletion(
        self,
        issuer: str,
        subject: str,
        research_id: UUID,
        *,
        expected_version: int,
    ) -> ResearchTask:
        method = getattr(self.repository, "admit_deletion", None)
        if not callable(method):
            raise ResearchConflict("research deletion admission boundary is unavailable")
        current = await self.get(issuer, subject, research_id)
        return await cast(Any, method)(current, expected_version=expected_version)

    async def list(self, issuer: str, subject: str, *, mode: ResearchMode | None = None, outcome: ResearchOutcome | None = None, limit: int = 25, offset: int = 0) -> list[ResearchTask]:
        if not 1 <= limit <= 100:
            raise ResearchValidationError("invalid page size")
        return await self.repository.list(_owner(issuer, subject), mode=mode, outcome=outcome, limit=limit, offset=offset)

    async def evidence(self, issuer: str, subject: str, research_id: UUID, *, limit: int = 100, offset: int = 0) -> list[ResearchEvidence]:
        return await self.repository.evidence(_owner(issuer, subject), research_id, limit=limit, offset=offset)

    async def _transition(self, task: ResearchTask, *, stage: ResearchStage | None = None, outcome: ResearchOutcome | None = None, failure_class: str | None = None, consumed: ResearchBudgetConsumption | None = None, evidence: tuple[ResearchEvidence, ...] | None = None, citations: tuple[ResearchCitation, ...] | None = None, artifact_refs: tuple[Mapping[str, object], ...] | None = None, synthesized_answer: str | None = None, cancellation_requested: bool | None = None, content_state: ResearchContentState | None = None, operation: str = "checkpoint", idempotency_key: str | None = None, idempotency_fingerprint: str | None = None, idempotency_command: str | None = None) -> ResearchTask:
        if task.terminal or task.content_state is not ResearchContentState.ACTIVE:
            raise ResearchConflict("research task is no longer mutable")
        effective_consumed = consumed or task.consumed_budget
        if not effective_consumed.within(task.admitted_budget):
            raise ResearchValidationError("research consumption exceeds admitted budget")
        effective_evidence = evidence if evidence is not None else task.evidence
        effective_citations = citations if citations is not None else task.citations
        evidence_by_id = {item.source_id: item for item in effective_evidence}
        if len(evidence_by_id) != len(effective_evidence):
            raise ResearchValidationError("duplicate research evidence")
        for item in effective_evidence:
            if item.task_id != task.id:
                raise ResearchValidationError("evidence belongs to another research task")
            if not item.excerpts or sum(len(excerpt.encode("utf-8")) for excerpt in item.excerpts) > MAX_EVIDENCE_EXCERPT:
                raise ResearchValidationError("research evidence excerpt is too large")
            _canonical_url(item.url)
        seen_citations: set[UUID] = set()
        for citation in effective_citations:
            source = evidence_by_id.get(citation.source_id)
            if source is None or source.task_id != task.id:
                raise ResearchValidationError("citation does not resolve to task evidence")
            if citation.source_id in seen_citations:
                raise ResearchValidationError("duplicate research citation")
            if citation.url != source.url or citation.label != source.label:
                raise ResearchValidationError("citation differs from admitted evidence")
            seen_citations.add(citation.source_id)
        now = _utc(self.clock())
        requested_failure = failure_class if failure_class is not None else task.failure_class
        effective_failure = _public_failure_class(requested_failure)
        if requested_failure is not None and effective_failure is None:
            raise ResearchValidationError("research failure class is not in the contract vocabulary")
        next_stage = stage or task.stage
        next_outcome = outcome if outcome is not None else task.outcome
        checkpoint = ResearchCheckpoint(uuid4(), task.id, task.version + 1, next_stage, now, operation)
        next_cancellation_requested = task.cancellation_requested if cancellation_requested is None else cancellation_requested
        effective_answer = synthesized_answer if synthesized_answer is not None else task.synthesized_answer
        if next_outcome is not None and effective_answer is None:
            effective_answer = f"Research {next_outcome.value}."
        updated = replace(
            task,
            stage=next_stage,
            outcome=next_outcome,
            failure_class=effective_failure,
            consumed_budget=effective_consumed,
            evidence=effective_evidence,
            citations=effective_citations,
            artifact_refs=artifact_refs if artifact_refs is not None else task.artifact_refs,
            synthesized_answer=effective_answer,
            cancellation_requested=next_cancellation_requested,
            cancellation_requested_at=(now if next_cancellation_requested and not task.cancellation_requested_at else task.cancellation_requested_at),
            content_state=content_state or task.content_state,
            version=task.version + 1,
            updated_at=now,
            finished_at=now if next_outcome is not None else task.finished_at,
            checkpoints=task.checkpoints + (checkpoint,),
        )
        stored = await self.repository.save(
            updated,
            expected_version=task.version,
            idempotency_key=idempotency_key,
            fingerprint=idempotency_fingerprint,
            command=idempotency_command,
            result={"sourceVersion": updated.version, "accepted": True, "unknown": False}
            if idempotency_key
            else None,
        )
        checkpoint_operation = {
            "plan": "research.plan",
            "evidence_validate": "research.evidence_validate",
            "synthesize": "research.synthesize",
            "cancel_requested": "research.cancel_command",
            "cleanup_retry": "research.delete",
            "cleanup_reconcile": "research.delete",
            "settle": "research.coordinate",
        }.get(operation, "research.coordinate")
        _emit_research_metric(
            self.metrics,
            RESEARCH_COMPONENT,
            "research_checkpoint_count",
            operation=checkpoint_operation,
            outcome="success",
            dependency=_RESEARCH_DEPENDENCY,
            status=next_stage.value,
        )
        if next_outcome is not None:
            task_outcome = _RESEARCH_OUTCOME_BY_TASK[next_outcome]
            _emit_research_metric(
                self.metrics,
                RESEARCH_COMPONENT,
                "research_coordinate_outcome_count",
                operation="research.coordinate",
                outcome=task_outcome,
                dependency=_RESEARCH_DEPENDENCY,
                status=next_outcome.value,
                error_class=_telemetry_failure_class(requested_failure, outcome=next_outcome),
            )
            _emit_research_budget_metrics(self.metrics, stored)
            if next_outcome is ResearchOutcome.CANCELED and stored.cancellation_requested_at is not None:
                lag_ms = max(0.0, (stored.finished_at - stored.cancellation_requested_at).total_seconds() * 1000) if stored.finished_at else None
                if lag_ms is not None:
                    _emit_research_metric(
                        self.metrics,
                        "aura.research.cancellation@1",
                        "research_cancel_settlement_lag_ms",
                        lag_ms,
                        operation="research.cancel_settlement",
                        outcome="success",
                        dependency=_RESEARCH_DEPENDENCY,
                        status=stored.stage.value,
                    )
        return stored

    async def checkpoint(
        self,
        task: ResearchTask,
        stage: ResearchStage,
        *,
        operation: str = "checkpoint",
        consumed: ResearchBudgetConsumption | None = None,
        evidence: tuple[ResearchEvidence, ...] | None = None,
    ) -> ResearchTask:
        return await self._transition(task, stage=stage, operation=operation, consumed=consumed, evidence=evidence)

    async def settle(self, task: ResearchTask, outcome: ResearchOutcome, *, failure_class: str | None = None, consumed: ResearchBudgetConsumption | None = None, evidence: tuple[ResearchEvidence, ...] | None = None, citations: tuple[ResearchCitation, ...] | None = None, artifact_refs: tuple[Mapping[str, object], ...] | None = None, synthesized_answer: str | None = None) -> ResearchTask:
        return await self._transition(task, stage=ResearchStage.TERMINAL, outcome=outcome, failure_class=failure_class, consumed=consumed, evidence=evidence, citations=citations, artifact_refs=artifact_refs, synthesized_answer=synthesized_answer, operation="settle")

    async def _record_cleanup(self, task: ResearchTask, *, delete_linked_artifacts: bool, evidence_status: str, artifact_status: str, status: str, last_error: str | None = None) -> None:
        method = getattr(self.repository, "record_cleanup", None)
        if callable(method) and task.deletion_audit_id is not None:
            await cast(Any, method)(task.id, task.deletion_audit_id, delete_linked_artifacts=delete_linked_artifacts, evidence_status=evidence_status, artifact_status=artifact_status, status=status, last_error=last_error)

    async def _cleanup_proof(self, task: ResearchTask) -> Mapping[str, object] | None:
        method = getattr(self.repository, "get_cleanup", None)
        return cast(Mapping[str, object] | None, await cast(Any, method)(task.id)) if callable(method) else None

    async def cancel(self, issuer: str, subject: str, research_id: UUID, *, expected_version: int, idempotency_key: str | None = None, _idempotency_fingerprint: str | None = None) -> ResearchTask:
        started = monotonic()
        outcome = "success"
        error_class: str | None = None
        task = await self.get(issuer, subject, research_id)
        owner = task.owner
        fingerprint = _fingerprint("cancel", research_id, expected_version)
        if idempotency_key:
            durable = await self.repository.get_command_receipt(owner, idempotency_key)
            if durable is not None:
                if durable[0] != fingerprint or durable[1] != "cancel":
                    raise ResearchConflict("idempotency key was reused")
                return await self.repository.get_unscoped(durable[2])
        replay = self._replayed_command(owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchTask, replay)
        try:
            if task.version != expected_version:
                if idempotency_key and task.cancellation_requested:
                    return task
                outcome, error_class = "rejected", "conflict"
                raise ResearchConflict("research task version conflict")
            if task.terminal:
                outcome, error_class = "rejected", "conflict"
                return task
            result = await self._transition(
                task,
                stage=ResearchStage.DRAINING,
                cancellation_requested=True,
                operation="cancel_requested",
                idempotency_key=idempotency_key,
                idempotency_fingerprint=_idempotency_fingerprint or fingerprint,
                idempotency_command="cancel",
            )
            clients = self.provider_clients
            active = await self.repository.active_operations(task.id)
            active_by_provider: dict[str, list[UUID]] = {"search": [], "static_fetch": [], "browser_fetch": []}
            for operation, request_id in active:
                provider_name = {"search": "search", "fetch": "static_fetch", "browser_fetch": "browser_fetch"}.get(operation)
                if provider_name is not None:
                    active_by_provider[provider_name].append(request_id)
            if not any(active_by_provider.values()):
                active_by_provider["search"].append(uuid5(RESEARCH_ID_NAMESPACE, f"operation:{task.id}:search"))
                active_by_provider["static_fetch"].extend(uuid5(RESEARCH_ID_NAMESPACE, f"operation:{task.id}:fetch:{url}") for url in task.seed_urls)
            for provider_name in ("search", "static_fetch", "browser_fetch"):
                provider = getattr(clients, provider_name, None)
                cancel = getattr(provider, "cancel", None)
                if callable(cancel):
                    for cancel_id in active_by_provider[provider_name]:
                        try:
                            await cast(Callable[..., Any], cancel)(cancel_id, reason="owner_cancel")
                        except Exception:
                            # Cancellation is fenced by the durable intent; a provider
                            # outage is reconciled by the worker after the call returns.
                            pass
            self._remember_command(owner, idempotency_key, fingerprint, result)
            return result
        except ResearchConflict:
            raise
        finally:
            _emit_research_metric(
                self.metrics,
                "aura.research.cancellation@1",
                "research_cancel_command_outcome_count",
                operation="research.cancel_command",
                outcome=outcome,
                dependency=_RESEARCH_DEPENDENCY,
                status=ResearchStage.DRAINING.value if outcome == "success" else task.stage.value,
                error_class=error_class,
            )
            _emit_research_metric(
                self.metrics,
                "aura.research.cancellation@1",
                "research_cancel_command_latency_ms",
                (monotonic() - started) * 1000,
                operation="research.cancel_command",
                outcome=outcome,
                dependency=_RESEARCH_DEPENDENCY,
                status=ResearchStage.DRAINING.value if outcome == "success" else task.stage.value,
                error_class=error_class,
            )
            _emit_research_span(
                self.metrics,
                "aura.research.cancellation@1",
                "research.cancel_command",
                (monotonic() - started) * 1000,
                outcome=outcome,
                dependency=_RESEARCH_DEPENDENCY,
                error_class=error_class,
                trace_id=uuid4().hex,
            )

    async def extend(self, issuer: str, subject: str, research_id: UUID, *, expected_version: int, cumulative_budget: ResearchBudget | Mapping[str, object], idempotency_key: str | None = None) -> ResearchTask:
        task = await self.get(issuer, subject, research_id)
        fingerprint = _fingerprint("extend", research_id, expected_version, cumulative_budget)
        if idempotency_key:
            durable = await self.repository.get_command_receipt(task.owner, idempotency_key)
            if durable is not None:
                if durable[0] != fingerprint or durable[1] != "extend":
                    raise ResearchConflict("idempotency key was reused")
                return await self.repository.get_unscoped(durable[2])
        replay = self._replayed_command(task.owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchTask, replay)
        if task.version != expected_version or task.terminal or task.extension_accepted:
            raise ResearchConflict("research extension is not available")
        desired = _budget_from(cumulative_budget, task.mode, await self.get_settings(*task.owner))
        if not task.admitted_budget.within(desired) or desired == task.admitted_budget:
            raise ResearchValidationError("extension must increase within the operator envelope")
        # The extension is an additional owner-confirmed admission from the
        # current point in time.  Keep the absolute deadline consistent with
        # the budget the owner just admitted, while `_budget_from` has already
        # enforced the immutable operator envelope.
        updated = replace(task, admitted_budget=desired, deadline_at=task.created_at + timedelta(seconds=desired.wall_time_seconds), extension_accepted=True, version=task.version + 1, updated_at=_utc(self.clock()))
        result = await self.repository.save(
            updated,
            expected_version=task.version,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint if idempotency_key else None,
            command="extend" if idempotency_key else None,
            result={"sourceVersion": updated.version} if idempotency_key else None,
        )
        self._remember_command(task.owner, idempotency_key, fingerprint, result)
        return result

    async def delete(self, issuer: str, subject: str, research_id: UUID, *, expected_version: int, delete_linked_artifacts: bool = False, idempotency_key: str | None = None) -> ResearchTask:
        task = await self.get(issuer, subject, research_id)
        fingerprint = _fingerprint("delete", research_id, expected_version, delete_linked_artifacts)
        if idempotency_key:
            durable = await self.repository.get_command_receipt(task.owner, idempotency_key)
            if durable is not None:
                if durable[0] != fingerprint or durable[1] != "delete":
                    raise ResearchConflict("idempotency key was reused")
                return await self.repository.get_unscoped(durable[2])
        replay = self._replayed_command(task.owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchTask, replay)
        if task.version != expected_version or not task.terminal:
            raise ResearchConflict("research content is not deletable")
        if task.content_state is not ResearchContentState.ACTIVE:
            return task
        started = monotonic()
        try:
            result = await self.admit_deletion(
                issuer,
                subject,
                research_id,
                expected_version=task.version,
            )
            await self._record_cleanup(result, delete_linked_artifacts=delete_linked_artifacts, evidence_status="pending", artifact_status="pending" if delete_linked_artifacts and task.artifact_refs else "not_requested", status="pending")
            if delete_linked_artifacts and task.artifact_refs:
                if self.artifact_service is None:
                    raise ResearchConflict("linked artifact cleanup is unavailable")
                artifact_lifecycles: list[str] = []
                for reference in task.artifact_refs:
                    artifact_id = UUID(str(reference.get("artifactId")))
                    artifact_service = cast(Any, self.artifact_service)
                    artifact = await artifact_service.get(*task.owner, artifact_id)
                    deleted = await artifact_service.admit_delete(*task.owner, artifact_id, expected_version=artifact.version, idempotency_key=f"research-delete:{task.id}:{artifact_id}")
                    artifact_lifecycles.append(str(getattr(deleted, "lifecycle", "unknown")))
                if any(lifecycle != "deleted" for lifecycle in artifact_lifecycles):
                    await self._record_cleanup(result, delete_linked_artifacts=True, evidence_status="pending", artifact_status="pending", status="pending")
                    _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_outcome_count", operation="research.delete", outcome="partial", dependency=_RESEARCH_DEPENDENCY, status=ResearchContentState.DELETION_PENDING.value, error_class="cleanup_unknown")
                    _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_latency_ms", (monotonic() - started) * 1000, operation="research.delete", outcome="partial", dependency=_RESEARCH_DEPENDENCY, status=ResearchContentState.DELETION_PENDING.value, error_class="cleanup_unknown")
                    _emit_research_span(self.metrics, "aura.research.deletion@1", "research.delete", (monotonic() - started) * 1000, outcome="partial", dependency=_RESEARCH_DEPENDENCY, error_class="cleanup_unknown", trace_id=uuid4().hex)
                    return result
            finalize = getattr(self.repository, "finalize_deletion", None)
            if not callable(finalize):
                raise ResearchConflict("atomic deletion boundary is unavailable")
            retained_artifacts = task.artifact_refs if not delete_linked_artifacts else ()
            result = await cast(Any, finalize)(result, expected_version=result.version, artifact_refs=retained_artifacts, delete_linked_artifacts=delete_linked_artifacts, evidence_status="deleted", artifact_status="not_requested" if not delete_linked_artifacts else "deleted", status="deleted", idempotency_key=idempotency_key, fingerprint=fingerprint if idempotency_key else None, command="delete" if idempotency_key else None, result={"sourceVersion": result.version + 1} if idempotency_key else None)
            self._remember_command(task.owner, idempotency_key, fingerprint, result)
            _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_outcome_count", operation="research.delete", outcome="success", dependency=_RESEARCH_DEPENDENCY, status=result.content_state.value)
            _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_latency_ms", (monotonic() - started) * 1000, operation="research.delete", outcome="success", dependency=_RESEARCH_DEPENDENCY, status=result.content_state.value)
            _emit_research_span(self.metrics, "aura.research.deletion@1", "research.delete", (monotonic() - started) * 1000, outcome="success", dependency=_RESEARCH_DEPENDENCY, trace_id=uuid4().hex)
            return result
        except Exception:
            try:
                pending = await self.repository.get_unscoped(task.id)
                if pending.content_state is ResearchContentState.DELETION_PENDING:
                    unknown = await self.repository.save(replace(pending, content_state=ResearchContentState.CLEANUP_UNKNOWN, version=pending.version + 1, updated_at=_utc(self.clock())), expected_version=pending.version)
                    await self._record_cleanup(unknown, delete_linked_artifacts=delete_linked_artifacts, evidence_status="unknown", artifact_status="unknown" if delete_linked_artifacts else "not_requested", status="unknown", last_error="cleanup proof unavailable")
            except Exception:
                pass
            _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_outcome_count", operation="research.delete", outcome="failed", dependency=_RESEARCH_DEPENDENCY, status=task.content_state.value, error_class="deletion_failed")
            _emit_research_metric(self.metrics, "aura.research.deletion@1", "research_deletion_latency_ms", (monotonic() - started) * 1000, operation="research.delete", outcome="failed", dependency=_RESEARCH_DEPENDENCY, status=task.content_state.value, error_class="deletion_failed")
            _emit_research_span(self.metrics, "aura.research.deletion@1", "research.delete", (monotonic() - started) * 1000, outcome="failed", dependency=_RESEARCH_DEPENDENCY, error_class="deletion_failed", trace_id=uuid4().hex)
            raise

    async def retry_cleanup(self, issuer: str, subject: str, research_id: UUID, *, expected_version: int, idempotency_key: str | None = None) -> ResearchTask:
        task = await self.get(issuer, subject, research_id)
        fingerprint = _fingerprint("cleanup_retry", research_id, expected_version)
        if idempotency_key:
            durable = await self.repository.get_command_receipt(task.owner, idempotency_key)
            if durable is not None:
                if durable[0] != fingerprint or durable[1] != "cleanup_retry":
                    raise ResearchConflict("idempotency key was reused")
                return await self.repository.get_unscoped(durable[2])
        replay = self._replayed_command(task.owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchTask, replay)
        if task.version != expected_version or task.content_state is not ResearchContentState.DELETION_FAILED:
            raise ResearchConflict("cleanup retry is not available")
        proof = await self._cleanup_proof(task)
        if proof is None or proof.get("status") != "failed" or proof.get("evidenceStatus") != "deleted" or proof.get("artifactStatus") not in {"deleted", "not_requested"}:
            raise ResearchConflict("cleanup retry lacks durable proof")
        finalize = getattr(self.repository, "finalize_deletion", None)
        if not callable(finalize):
            raise ResearchConflict("atomic deletion boundary is unavailable")
        result = await cast(Any, finalize)(task, expected_version=task.version, artifact_refs=task.artifact_refs, delete_linked_artifacts=bool(proof.get("deleteLinkedArtifacts", False)), evidence_status="deleted", artifact_status=str(proof.get("artifactStatus", "not_requested")), status="deleted", idempotency_key=idempotency_key, fingerprint=fingerprint if idempotency_key else None, command="cleanup_retry" if idempotency_key else None, result={"sourceVersion": task.version + 1} if idempotency_key else None)
        self._remember_command(task.owner, idempotency_key, fingerprint, result)
        return result

    async def reconcile_cleanup(self, issuer: str, subject: str, research_id: UUID, *, expected_version: int, idempotency_key: str | None = None) -> ResearchTask:
        task = await self.get(issuer, subject, research_id)
        fingerprint = _fingerprint("cleanup_reconcile", research_id, expected_version)
        if idempotency_key:
            durable = await self.repository.get_command_receipt(task.owner, idempotency_key)
            if durable is not None:
                if durable[0] != fingerprint or durable[1] != "cleanup_reconcile":
                    raise ResearchConflict("idempotency key was reused")
                return await self.repository.get_unscoped(durable[2])
        replay = self._replayed_command(task.owner, idempotency_key, fingerprint)
        if replay is not None:
            return cast(ResearchTask, replay)
        if task.version != expected_version or task.content_state is not ResearchContentState.CLEANUP_UNKNOWN:
            raise ResearchConflict("cleanup reconciliation is not available")
        proof = await self._cleanup_proof(task)
        if proof is None or proof.get("evidenceStatus") != "deleted":
            raise ResearchConflict("cleanup reconciliation lacks evidence purge proof")
        if task.artifact_refs and self.artifact_service is not None:
            for reference in task.artifact_refs:
                artifact = await cast(Any, self.artifact_service).get(*task.owner, UUID(str(reference.get("artifactId"))))
                if str(getattr(artifact, "lifecycle", "")) != "deleted":
                    raise ResearchConflict("linked artifact cleanup is not proven")
        finalize = getattr(self.repository, "finalize_deletion", None)
        if not callable(finalize):
            raise ResearchConflict("atomic deletion boundary is unavailable")
        result = await cast(Any, finalize)(task, expected_version=task.version, artifact_refs=task.artifact_refs, delete_linked_artifacts=bool(proof.get("deleteLinkedArtifacts", False)), evidence_status="deleted", artifact_status=str(proof.get("artifactStatus", "not_requested")), status="deleted", idempotency_key=idempotency_key, fingerprint=fingerprint if idempotency_key else None, command="cleanup_reconcile" if idempotency_key else None, result={"sourceVersion": task.version + 1} if idempotency_key else None)
        self._remember_command(task.owner, idempotency_key, fingerprint, result)
        return result

    def bind_projection_capability(self, capability: object) -> None:
        if self._projection_capability is not None:
            raise ResearchConflict("projection capability already bound")
        self._projection_capability = capability

    def __bind_capability(self, capability: object) -> None:  # pyright: ignore[reportUnusedFunction]
        """Private composition hook used by the Task Center source adapter."""
        self.bind_projection_capability(capability)

    def __validate_capability(self, capability: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if capability is not self._projection_capability:
            raise PermissionError("task projection capability is invalid")

    async def query_task_projection_snapshots(self, query: TaskProjectionSnapshotQuery, *, capability: object) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
        if capability is not self._projection_capability:
            raise PermissionError("task projection capability is bound to Core composition")
        owner_tasks = await self.repository.projection_tasks()
        if query.mode == "exact":
            task = next((item for item in owner_tasks if item.id == query.source_id), None)
            if task is None or task.version < query.minimum_source_version:
                return None
            return self._snapshot(task)
        values = [snapshot for item in owner_tasks if (snapshot := self._snapshot(item)) is not None]
        values.sort(key=lambda item: (item.updated_at, item.source_id))
        if query.cursor:
            cursor_stamp, cursor_id = decode_cursor(query.cursor)
            values = [item for item in values if (item.updated_at, item.source_id) > (cursor_stamp, cursor_id)]
        page = values[: query.page_size]
        has_more = len(values) > query.page_size
        next_cursor = encode_cursor(page[-1].updated_at, page[-1].source_id) if has_more and page else None
        return TaskProjectionSnapshotPage(tuple(page), next_cursor, not has_more)

    async def command_task(self, source_id: UUID, command: TaskCommand, *, owner_scope_id: UUID, expected_source_version: int, idempotency_key: str) -> Any:
        task = await self.repository.get_unscoped(source_id)
        if task.owner_scope_id != owner_scope_id:
            raise ResearchNotFound("research task not found")
        fingerprint = _fingerprint("task_command", source_id, command.value, expected_source_version)
        durable = await self.repository.get_command_receipt(task.owner, idempotency_key)
        if durable is not None:
            if durable[0] != fingerprint or durable[1] != command.value:
                raise ResearchConflict("idempotency key was reused")
            result = durable[3]
            return type("ResearchCommandReceipt", (), {"source_version": int(cast(Any, result.get("sourceVersion", task.version))), "accepted": bool(result.get("accepted", True)), "unknown": bool(result.get("unknown", False))})()
        replay = self._replayed_command(task.owner, idempotency_key, fingerprint)
        if replay is not None:
            return replay
        if task.version != expected_source_version:
            raise ResearchConflict("research task version conflict")
        if command is TaskCommand.CANCEL:
            updated = await self.cancel(task.issuer, task.subject, task.id, expected_version=task.version, idempotency_key=idempotency_key, _idempotency_fingerprint=fingerprint)
            receipt = type("ResearchCommandReceipt", (), {"source_version": updated.version, "accepted": True, "unknown": False})()
            self._remember_command(task.owner, idempotency_key, fingerprint, receipt)
            return receipt
        raise ResearchConflict("research retry is source-owned and creates a new task")

    async def resolve_task_detail(self, source_id: UUID, *, owner_scope_id: UUID) -> Mapping[str, object]:
        task = await self.repository.get_unscoped(source_id)
        if task.owner_scope_id != owner_scope_id:
            raise ResearchNotFound("research task not found")
        return self.detail(task)

    def _snapshot(self, task: ResearchTask | None) -> TaskProjectionSnapshot | None:
        if task is None:
            return None
        normalized = TaskNormalizedStatus.QUEUED if task.stage is ResearchStage.QUEUED else TaskNormalizedStatus.RUNNING
        if task.outcome is not None:
            normalized = TaskNormalizedStatus.CANCELED if task.outcome is ResearchOutcome.CANCELED else TaskNormalizedStatus.COMPLETED if task.outcome is ResearchOutcome.COMPLETED else TaskNormalizedStatus.FAILED
        return TaskProjectionSnapshot(task.owner_scope_id, TaskSourceKind.RESEARCH, task.id, task.version, normalized, task.stage.value, task.outcome.value if task.outcome else None, task.stage.value, min(1.0, task.consumed_budget.fetched_sources / max(1, task.admitted_budget.fetched_sources)), TaskProjectionCapability(can_cancel=not task.terminal), "research_detail", _public_failure_class(task.failure_class), task.created_at, task.updated_at, task.finished_at, task.content_state is not ResearchContentState.ACTIVE)

    @staticmethod
    def serialize(task: ResearchTask, *, detail: bool) -> dict[str, object]:
        result: dict[str, object] = {"id": str(task.id), "taskRef": task.task_ref, "mode": task.mode.value, "stage": task.stage.value, "outcome": task.outcome.value if task.outcome else None, "contentState": task.content_state.value, "version": task.version, "capabilities": {"canCancel": not task.terminal, "canExtend": not task.terminal and not task.extension_accepted, "canDelete": task.terminal and task.content_state is ResearchContentState.ACTIVE, "canRetryCleanup": task.content_state is ResearchContentState.DELETION_FAILED, "canReconcileCleanup": task.content_state is ResearchContentState.CLEANUP_UNKNOWN}, "createdAt": task.created_at.isoformat(), "updatedAt": task.updated_at.isoformat(), "finishedAt": task.finished_at.isoformat() if task.finished_at else None}
        if not detail:
            return result
        if task.content_state is not ResearchContentState.ACTIVE:
            result["tombstoneLabel"] = "Research source deleted"
            return result
        result.update({"question": task.question, "admittedBudget": task.admitted_budget.as_dict(), "consumedBudget": task.consumed_budget.as_dict(), "extensionAccepted": task.extension_accepted, "sourceCount": len(task.evidence), "failureClass": _public_failure_class(task.failure_class), "artifactRefs": list(task.artifact_refs), "citations": [item.as_dict() for item in task.citations]})
        return result

    def summary(self, task: ResearchTask) -> dict[str, object]:
        return self.serialize(task, detail=False)

    def detail(self, task: ResearchTask) -> dict[str, object]:
        return self.serialize(task, detail=True)

    @staticmethod
    def settings_payload(settings: ResearchSettings) -> dict[str, object]:
        defaults = dict(settings.defaults)
        return {
            "version": settings.version,
            "presets": [
                {"mode": mode.value, "maximum": DEFAULT_BUDGETS[mode].as_dict(), "defaults": defaults[mode].as_dict()}
                for mode in ResearchMode
            ],
            "updatedAt": settings.updated_at.isoformat(),
        }


class ResearchCoordinator:
    """Bounded worker coordinator; provider output is evidence only."""

    def __init__(self, service: ResearchApplicationService, providers: object | None = None, *, artifacts: object | None = None, synthesizer: ResearchSynthesisPort | None = None, entity_resolver: ResearchEntityResolutionPort | None = None, resolver: ResearchEntityResolutionPort | None = None, metrics: ResearchMetrics | None = None, clock: Any = None, trace_id: str | None = None, run_id: str | None = None, conversation_id: str | None = None) -> None:
        self.service = service
        self.providers = providers or service.provider_clients
        self.artifacts = artifacts
        self.synthesizer = synthesizer
        if entity_resolver is not None and resolver is not None and entity_resolver is not resolver:
            raise ResearchValidationError("conflicting entity resolvers")
        self.entity_resolver = entity_resolver or resolver
        self.metrics = metrics
        self._clock_injected = callable(clock)
        if metrics is not None and service.metrics is None:
            service.metrics = metrics
        self.clock: Callable[[], float] = cast(Callable[[], float], clock if callable(clock) else monotonic)
        self.trace_id = trace_id or uuid4().hex
        self.run_id = run_id
        self.conversation_id = conversation_id

    async def _phase_checkpoint(self, task: ResearchTask, stage: ResearchStage, operation: str, component: str, outcome_metric: str, latency_metric: str, dependency: str, action: Callable[[], object] | None = None) -> ResearchTask:
        started = self.clock()
        outcome = "success"
        error_class: str | None = None
        try:
            if action is not None:
                result = action()
                if inspect.isawaitable(result):
                    await result
            # Checkpoint persistence is deliberately outside the phase span:
            # this timing covers only the planner/validator/synthesizer action.
            pass
        except ResearchConflict:
            outcome, error_class = "rejected", "conflict"
            raise
        except Exception:
            outcome, error_class = "failed", "validation_failed"
            raise
        finally:
            elapsed = (self.clock() - started) * 1000
            _emit_research_metric(self.metrics, component, outcome_metric, operation=f"research.{operation}", outcome=outcome, dependency=dependency, status=stage.value, error_class=error_class)
            _emit_research_metric(self.metrics, component, latency_metric, elapsed, operation=f"research.{operation}", outcome=outcome, dependency=dependency, status=stage.value, error_class=error_class)
            _emit_research_span(self.metrics, component, f"research.{operation}", elapsed, outcome=outcome, dependency=dependency, error_class=error_class, trace_id=self.trace_id, run_id=self.run_id, conversation_id=self.conversation_id)
        return await self.service.checkpoint(task, stage, operation=operation)

    async def _authoritative_task(self, task: ResearchTask) -> ResearchTask:
        """Reload before dispatch so owner cancellation fences provider work."""
        return await self.service.repository.get_unscoped(task.id)

    async def _begin_operation(self, task: ResearchTask, operation: str, request_id: UUID) -> bool:
        capability = {"search": "search", "fetch": "staticFetch", "browser_fetch": "browserFetch"}[operation]
        try:
            return await self.service.repository.begin_operation(
                task.id,
                operation,
                request_id,
                now=self.service.clock(),
                deadline_at=task.deadline_at,
                capability_enabled=self.service.capability_enabled(task, capability),
            )
        except TypeError as exc:
            # Keep explicitly isolated legacy test repositories source
            # compatible; production repositories implement the lease-bearing
            # signature above.
            if "unexpected keyword" not in str(exc):
                raise
            return await self.service.repository.begin_operation(
                task.id, operation, request_id, now=self.service.clock()
            )

    async def _settle_operation(self, task: ResearchTask, operation: str, request_id: UUID, status: str, response: Mapping[str, object] | None = None) -> None:
        await self.service.repository.settle_operation(
            task.id,
            operation,
            request_id,
            status=status,
            now=self.service.clock(),
            response=response,
        )

    async def _dispatch_claimed(self, task: ResearchTask, operation: str, request_id: UUID, invoke: Callable[[], Any]) -> tuple[bool, Any | None]:
        dispatch = getattr(self.service.repository, "dispatch_operation", None)
        if callable(dispatch):
            capability = {"search": "search", "fetch": "staticFetch", "browser_fetch": "browserFetch"}[operation]
            def current_capability(current: ResearchTask) -> bool:
                return self.service.capability_enabled(current, capability)

            return cast(tuple[bool, Any | None], await cast(Any, dispatch)(task.id, operation, request_id, now=self.service.clock(), deadline_at=task.deadline_at, capability_check=current_capability, invoke=invoke))
        return True, await invoke()

    def _deadline_expired(self, task: ResearchTask) -> bool:
        now = self.service.clock()
        if task.deadline_at is not None:
            return _utc(now) >= _utc(task.deadline_at)
        return (now - task.created_at).total_seconds() >= task.admitted_budget.wall_time_seconds

    def _remaining_limit(self, task: ResearchTask, ceiling_ms: int) -> _ResearchRequestLimits:
        remaining_seconds = task.admitted_budget.wall_time_seconds - max(0.0, (self.service.clock() - task.created_at).total_seconds())
        if task.deadline_at is not None:
            remaining_seconds = max(0.0, (task.deadline_at - self.service.clock()).total_seconds())
        # Provider ports reject zero/negative timeouts.  The caller has already
        # fenced an expired task, so one millisecond is the safe lower bound.
        return _ResearchRequestLimits(max(1, min(ceiling_ms, int(remaining_seconds * 1000))))

    @staticmethod
    def _evidence_response(item: ResearchEvidence) -> dict[str, object]:
        return {
            "sourceId": str(item.source_id), "taskId": str(item.task_id), "label": item.label,
            "url": item.url, "publisher": item.publisher, "retrievedAt": item.retrieved_at.isoformat(),
            "mediaType": item.media_type, "extractionMethod": item.extraction_method,
            "freshnessAt": item.freshness_at.isoformat() if item.freshness_at else None,
            "excerpts": list(item.excerpts), "contentSha256": item.content_sha256, "locator": item.locator,
        }

    @staticmethod
    def _evidence_from_response(value: Mapping[str, object]) -> ResearchEvidence | None:
        try:
            excerpts = cast(Sequence[object], value.get("excerpts", ()))
            return ResearchEvidence(UUID(str(value["sourceId"])), UUID(str(value["taskId"])), str(value["label"]), str(value["url"]), str(value["publisher"]) if value.get("publisher") is not None else None, _utc(datetime.fromisoformat(str(value["retrievedAt"]))), str(value["mediaType"]), str(value["extractionMethod"]), _utc(datetime.fromisoformat(str(value["freshnessAt"]))) if value.get("freshnessAt") else None, tuple(str(item) for item in excerpts if isinstance(item, str)), str(value["contentSha256"]), str(value["locator"]) if value.get("locator") is not None else None)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _candidates_from_response(value: object) -> list[tuple[str, str | None, str | None]]:
        if not isinstance(value, (list, tuple)):
            return []
        result: list[tuple[str, str | None, str | None]] = []
        for raw_item in cast(Sequence[object], value):
            item = cast(Mapping[str, object], raw_item) if isinstance(raw_item, Mapping) else None
            if item is not None and item.get("url"):
                result.append((str(item["url"]), str(item["label"]) if item.get("label") else None, "search_candidate"))
        return result

    async def _settle(self, task: ResearchTask, outcome: ResearchOutcome, **kwargs: Any) -> ResearchTask:
        """Record terminal settlement latency, never total coordinator runtime."""
        started = self.clock()
        supplied = cast(ResearchBudgetConsumption | None, kwargs.get("consumed"))
        now = self.service.clock()
        deadline_seconds = task.admitted_budget.wall_time_seconds
        if task.deadline_at is not None:
            elapsed_seconds = max(0.0, (now - task.created_at).total_seconds())
        else:
            elapsed_seconds = max(0.0, (now - task.created_at).total_seconds())
        wall_time = min(deadline_seconds, int(elapsed_seconds))
        kwargs["consumed"] = replace(supplied or task.consumed_budget, wall_time_seconds=max((supplied or task.consumed_budget).wall_time_seconds, wall_time))
        try:
            return await self.service.settle(task, outcome, **kwargs)
        finally:
            elapsed = max(0.0, (self.clock() - started) * 1000)
            _emit_research_metric(
                self.metrics,
                RESEARCH_COMPONENT,
                "research_coordinate_latency_ms",
                elapsed,
                operation="research.coordinate",
                outcome=_RESEARCH_OUTCOME_BY_TASK.get(outcome, "unknown"),
                dependency=_RESEARCH_DEPENDENCY,
                status=outcome.value,
                error_class=cast(str | None, kwargs.get("failure_class")),
            )
            _emit_research_span(
                self.metrics,
                RESEARCH_COMPONENT,
                "research.coordinate",
                elapsed,
                outcome=_RESEARCH_OUTCOME_BY_TASK.get(outcome, "unknown"),
                dependency=_RESEARCH_DEPENDENCY,
                trace_id=self.trace_id,
                run_id=self.run_id,
                conversation_id=self.conversation_id,
            )

    async def _emit_static_settled(self, *, fallback_accepted: bool) -> None:
        recorder = getattr(self.service.repository, "record_fallback", None)
        ratio: float | None = None
        if callable(recorder):
            cohort_time = (
                datetime.fromtimestamp(max(0.0, self.clock()), UTC)
                if self._clock_injected
                else self.service.clock()
            )
            try:
                _, _, ratio = cast(tuple[int, int, float], await cast(Any, recorder)(cohort_time, accepted=fallback_accepted))
            except Exception:
                # A telemetry cohort outage cannot alter provider settlement or
                # the research result.  Do not fabricate a zero ratio.
                ratio = None
        else:
            ratio = None
        _emit_research_metric(self.metrics, RESEARCH_COMPONENT, "research_static_fetch_settled_count", operation="research.fetch_static", outcome="success", dependency=None, status=ResearchStage.FETCHING.value, provider="static_fetcher", degradation="none")
        if fallback_accepted:
            _emit_research_metric(self.metrics, RESEARCH_COMPONENT, "research_static_to_browser_fallback_count", operation="research.browser_fallback", outcome="success", dependency=None, status=ResearchStage.FETCHING.value, provider="static_fetcher", degradation="none")
        if ratio is not None:
            _emit_research_metric(self.metrics, RESEARCH_COMPONENT, "research_static_to_browser_fallback_ratio", ratio, operation="research.browser_fallback", outcome="success", dependency=None, status=ResearchStage.FETCHING.value, provider="static_fetcher", degradation="none")

    @staticmethod
    def _plan(question: str) -> ResearchPlan:
        return ResearchPlan(question)

    def _synthesize(self, plan: ResearchPlan, evidence: Sequence[ResearchEvidence]) -> object:
        """Invoke only the injected trusted synthesizer port.

        Keeping this narrow seam explicit also lets isolated evaluation inject
        a deterministic trusted adapter without making lexical heuristics part
        of production behavior.
        """
        if self.synthesizer is None:
            return None
        return self.synthesizer.synthesize(plan, evidence)

    async def _trusted_synthesis(self, plan: ResearchPlan, evidence: Sequence[ResearchEvidence]) -> ResearchSynthesis | None:
        result = self._synthesize(plan, evidence)
        if inspect.isawaitable(result):
            result = await cast(Any, result)
        if result is None or type(result) is not ResearchSynthesis:
            return None
        by_id = {item.source_id: item for item in evidence}
        task_ids = {item.task_id for item in evidence}
        if len(task_ids) > 1:
            raise ResearchValidationError("trusted synthesis received cross-task evidence")
        validated: list[ResearchClaim] = []
        seen: set[UUID] = set()
        for claim in result.claims:
            if type(claim) is not ResearchClaim or claim.source_id in seen:
                raise ResearchValidationError("trusted synthesis returned invalid claim identity")
            source = by_id.get(claim.source_id)
            if source is None or (task_ids and source.task_id != next(iter(task_ids))):
                raise ResearchValidationError("trusted synthesis returned cross-task evidence")
            if not claim.text or len(claim.text.encode("utf-8")) > MAX_EVIDENCE_EXCERPT:
                raise ResearchValidationError("trusted synthesis returned an unbounded claim")
            if not claim.span or len(claim.span.encode("utf-8")) > MAX_EVIDENCE_EXCERPT:
                raise ResearchValidationError("trusted synthesis returned an unbounded evidence span")
            if not any(claim.span in excerpt for excerpt in source.excerpts):
                raise ResearchValidationError("trusted synthesis span is not an exact evidence span")
            relation = _claim_relation_supported(plan.question, claim.text, claim.span)
            if relation is None:
                raise ResearchValidationError("trusted synthesis claim does not support the question")
            parsed_span = _parse_declarative_relation(claim.span)
            if parsed_span is None or self.entity_resolver is None:
                raise ResearchValidationError("entity resolution is unavailable")
            resolved = self.entity_resolver.resolve(
                parsed_span.subject.text, task_id=source.task_id, run_id=self.run_id
            )
            if inspect.isawaitable(resolved):
                resolved = await cast(Any, resolved)
            if not self._entity_resolution_matches(parsed_span.subject, resolved, source.task_id):
                raise ResearchValidationError("entity resolution did not establish the candidate")
            canonical_text, _ = relation
            seen.add(claim.source_id)
            validated.append(replace(claim, text=canonical_text))
        return ResearchSynthesis(tuple(validated), bool(result.sufficient) and bool(validated))

    def _entity_resolution_matches(
        self, candidate: _EntityPhrase, resolved: object, task_id: UUID
    ) -> bool:
        if type(resolved) is not ResearchEntityResolution:
            return False
        if not all(
            type(value) is str
            for value in (
                resolved.candidate,
                resolved.normalized,
                resolved.identity,
                resolved.entity_type,
                resolved.provenance,
            )
        ):
            return False
        if (
            resolved.task_id != task_id
            or resolved.run_id != self.run_id
            or resolved.candidate != candidate.text
            or resolved.normalized != candidate.text
            or not 1 <= len(resolved.identity) <= 256
            or any(ord(character) < 32 for character in resolved.identity)
            or resolved.entity_type not in {item.value for item in ResearchEntityType}
            or resolved.provenance not in {item.value for item in ResearchEntityProvenance}
        ):
            return False
        if candidate.kind == "numeric":
            return resolved.entity_type == ResearchEntityType.NUMERIC.value
        return resolved.entity_type != ResearchEntityType.NUMERIC.value

    async def run_once(self, research_id: UUID, *, owner: tuple[str, str] | None = None) -> ResearchTask:  # pyright: ignore[reportGeneralTypeIssues]
        if isinstance(self.service.repository, MemoryResearchRepository):
            task = self.service.repository.tasks.get(research_id)
            if task is None:
                raise ResearchNotFound("research task not found")
            if owner and task.owner != owner:
                raise ResearchNotFound("research task not found")
        else:
            if owner is None:
                task = await self.service.repository.get_unscoped(research_id)
            else:
                task = await self.service.get(*owner, research_id)
        if task.terminal:
            return task
        evidence = list(task.evidence)
        consumed = task.consumed_budget
        plan: ResearchPlan | None = None
        synthesis_failed_closed = False
        queue_lag_ms = max(0.0, (self.service.clock() - task.created_at).total_seconds() * 1000)
        _emit_research_metric(self.metrics, RESEARCH_COMPONENT, "research_queue_lag_ms", queue_lag_ms, operation="research.queue_claim", outcome="success", dependency=_RESEARCH_DEPENDENCY, status=task.stage.value)
        try:
            task = await self._authoritative_task(task)
            if self._deadline_expired(task):
                return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
            if task.cancellation_requested:
                return await self._settle(task, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
            planned: list[ResearchPlan] = []
            task = await self._phase_checkpoint(task, ResearchStage.PLANNING, "plan", "aura.research.planner@1", "research_plan_outcome_count", "research_plan_latency_ms", "model_gateway", action=lambda: planned.append(self._plan(task.question)))
            plan = planned[0]
            assert plan is not None
            task = await self._authoritative_task(task)
            if self._deadline_expired(task):
                return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
            task = await self.service.checkpoint(task, ResearchStage.SEARCHING, operation="search")
            candidates: list[tuple[str, str | None, str | None]] = [(url, None, "owner_seed") for url in task.seed_urls]
            if not candidates:
                search = getattr(self.providers, "search", None)
                if search is None or not self.service.capability_enabled(task, "search"):
                    return await self._settle(task, ResearchOutcome.FAILED, failure_class="provider_disabled", consumed=replace(consumed, search_requests=min(task.admitted_budget.search_requests, consumed.search_requests + 1)))
                search_request_id = uuid5(RESEARCH_ID_NAMESPACE, f"operation:{task.id}:search")
                admitted = await self._begin_operation(task, "search", search_request_id)
                if not admitted:
                    latest = await self._authoritative_task(task)
                    if latest.cancellation_requested:
                        return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                    operation = await self.service.repository.get_operation(task.id, "search", search_request_id)
                    if operation and operation.get("status") == "settled":
                        response = cast(Mapping[str, object], operation.get("response", {}))
                        candidates = self._candidates_from_response(response.get("candidates", ()))
                        if candidates:
                            consumed = replace(consumed, search_requests=min(task.admitted_budget.search_requests, consumed.search_requests + 1))
                    else:
                        return await self._settle(task, ResearchOutcome.PARTIAL, failure_class="settlement_unknown", consumed=consumed, evidence=tuple(evidence))
                else:
                    if not self.service.capability_enabled(task, "search"):
                        await self._settle_operation(task, "search", search_request_id, "disabled")
                        return await self._settle(task, ResearchOutcome.FAILED, failure_class="provider_disabled", consumed=consumed, evidence=tuple(evidence))
                    if self._deadline_expired(task):
                        return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
                    search_started = self.clock()
                    try:
                        dispatched, provider_task = await self._dispatch_claimed(task, "search", search_request_id, lambda: search.search(_ResearchSearchRequest(query=task.question, domain_allowlist=task.domains, request_id=search_request_id, limits=self._remaining_limit(task, 15_000))))
                        if not dispatched or provider_task is None:
                            latest = await self._authoritative_task(task)
                            if latest.cancellation_requested:
                                return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                            return await self._settle(latest, ResearchOutcome.BUDGET_EXHAUSTED if self._deadline_expired(latest) else ResearchOutcome.FAILED, failure_class="timeout" if self._deadline_expired(latest) else "provider_disabled", consumed=consumed, evidence=tuple(evidence))
                        result = await provider_task
                        await self._settle_operation(task, "search", search_request_id, "settled", {"candidates": [{"url": item.url, "label": item.label} for item in result.candidates]})
                    except Exception:
                        await self._settle_operation(task, "search", search_request_id, "failed")
                        raise
                    finally:
                        _emit_research_span(self.metrics, "aura.research.search@1", "research.search", max(0.0, (self.clock() - search_started) * 1000), outcome="success", dependency="searxng_port", trace_id=self.trace_id, run_id=self.run_id, conversation_id=self.conversation_id)
                    if result.candidates:
                        consumed = replace(consumed, search_requests=min(task.admitted_budget.search_requests, consumed.search_requests + 1))
                    candidates = [(item.url, item.label, "search_candidate") for item in result.candidates]
            task = await self._authoritative_task(task)
            if self._deadline_expired(task):
                return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
            if task.cancellation_requested:
                return await self._settle(task, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
            task = await self.service.checkpoint(task, ResearchStage.FETCHING, operation="fetch", consumed=consumed, evidence=tuple(evidence))
            static = getattr(self.providers, "static_fetch", None)
            browser = getattr(self.providers, "browser_fetch", None)
            for url, label, admission_kind in candidates[: task.admitted_budget.fetched_sources]:
                if task.cancellation_requested:
                    return await self._settle(task, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                if consumed.fetched_sources >= task.admitted_budget.fetched_sources:
                    break
                if any(item.url == url for item in evidence):
                    continue
                if static is None or not self.service.capability_enabled(task, "staticFetch"):
                    break
                url = _validate_public_destination(url)
                task = await self._authoritative_task(task)
                if task.cancellation_requested:
                    return await self._settle(task, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                if self._deadline_expired(task):
                    return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
                fetch_request_id = uuid5(RESEARCH_ID_NAMESPACE, f"operation:{task.id}:fetch:{url}")
                admitted = await self._begin_operation(task, "fetch", fetch_request_id)
                result = None
                if not admitted:
                    latest = await self._authoritative_task(task)
                    if latest.cancellation_requested:
                        return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                    operation = await self.service.repository.get_operation(task.id, "fetch", fetch_request_id)
                    if operation and operation.get("status") == "settled":
                        operation_response = cast(Mapping[str, object], operation.get("response", {}))
                        recovered_value = operation_response.get("evidence", {})
                        recovered = self._evidence_from_response(cast(Mapping[str, object], recovered_value)) if isinstance(recovered_value, Mapping) else None
                        if recovered is not None and all(item.source_id != recovered.source_id for item in evidence):
                            evidence.append(recovered)
                        raw_consumed = operation_response.get("consumed")
                        consumed = ResearchBudgetConsumption(**{key: int(cast(Any, cast(Mapping[str, object], raw_consumed).get(key, getattr(consumed, key)))) for key in ("wall_time_seconds", "fetched_sources", "search_requests", "browser_pages", "model_calls")}) if isinstance(raw_consumed, Mapping) else consumed
                        task = await self._authoritative_task(task)
                        task = await self.service.checkpoint(task, ResearchStage.FETCHING, operation="fetch_recovered", consumed=consumed, evidence=tuple(evidence))
                        continue
                    return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="settlement_unknown", consumed=consumed, evidence=tuple(evidence))
                try:
                    if not self.service.capability_enabled(task, "staticFetch"):
                        await self._settle_operation(task, "fetch", fetch_request_id, "disabled")
                        return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="provider_disabled", consumed=consumed, evidence=tuple(evidence))
                    if self._deadline_expired(task):
                        return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
                    fetch_started = self.clock()
                    dispatched, provider_task = await self._dispatch_claimed(
                        task,
                        "fetch",
                        fetch_request_id,
                        lambda url=url, admission_kind=admission_kind, fetch_request_id=fetch_request_id, task=task: static.fetch(
                            _ResearchStaticFetchRequest(
                                target_url=url,
                                admission_kind=admission_kind or "search_candidate",
                                request_id=fetch_request_id,
                                limits=self._remaining_limit(task, 20_000),
                            )
                        ),
                    )
                    if not dispatched or provider_task is None:
                        latest = await self._authoritative_task(task)
                        if latest.cancellation_requested:
                            return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                        return await self._settle(latest, ResearchOutcome.BUDGET_EXHAUSTED if self._deadline_expired(latest) else ResearchOutcome.PARTIAL, failure_class="timeout" if self._deadline_expired(latest) else "provider_disabled", consumed=consumed, evidence=tuple(evidence))
                    result = await provider_task
                    _emit_research_span(self.metrics, "aura.research.static_fetch@1", "research.fetch_static", max(0.0, (self.clock() - fetch_started) * 1000), outcome="success", dependency="static_fetcher_port", trace_id=self.trace_id, run_id=self.run_id, conversation_id=self.conversation_id)
                except Exception:
                    await self._settle_operation(task, "fetch", fetch_request_id, "failed")
                    raise
                consumed = replace(consumed, fetched_sources=consumed.fetched_sources + 1)
                fallback_accepted = getattr(result, "outcome", None) == "inadequate_extraction" and browser is not None and self.service.capability_enabled(task, "browserFetch") and consumed.browser_pages < task.admitted_budget.browser_pages
                await self._emit_static_settled(fallback_accepted=fallback_accepted)
                if fallback_accepted and browser is not None:
                    if self._deadline_expired(task):
                        await self._settle_operation(task, "fetch", fetch_request_id, "settled", {"consumed": consumed.as_dict()})
                        return await self._settle(task, ResearchOutcome.BUDGET_EXHAUSTED, failure_class="timeout", consumed=consumed, evidence=tuple(evidence))
                    browser_request_id = uuid5(RESEARCH_ID_NAMESPACE, f"operation:{task.id}:browser:{url}")
                    if not await self._begin_operation(task, "browser_fetch", browser_request_id):
                        latest = await self._authoritative_task(task)
                        if latest.cancellation_requested:
                            return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                        browser_operation = await self.service.repository.get_operation(task.id, "browser_fetch", browser_request_id)
                        if browser_operation and browser_operation.get("status") == "settled":
                            continue
                        return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="settlement_unknown", consumed=consumed, evidence=tuple(evidence))
                    if not self.service.capability_enabled(task, "browserFetch"):
                        await self._settle_operation(task, "browser_fetch", browser_request_id, "disabled")
                        return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="provider_disabled", consumed=consumed, evidence=tuple(evidence))
                    try:
                        browser_started = self.clock()
                        dispatched, provider_task = await self._dispatch_claimed(
                            task,
                            "browser_fetch",
                            browser_request_id,
                            lambda url=url, admission_kind=admission_kind, browser_request_id=browser_request_id, task=task: browser.fetch(
                                _ResearchBrowserFetchRequest(
                                    target_url=url,
                                    admission_kind=admission_kind or "search_candidate",
                                    justification="static_inadequate_extraction",
                                    request_id=browser_request_id,
                                    limits=self._remaining_limit(task, 45_000),
                                )
                            ),
                        )
                        if not dispatched or provider_task is None:
                            latest = await self._authoritative_task(task)
                            if latest.cancellation_requested:
                                return await self._settle(latest, ResearchOutcome.CANCELED, consumed=consumed, evidence=tuple(evidence))
                            return await self._settle(latest, ResearchOutcome.BUDGET_EXHAUSTED if self._deadline_expired(latest) else ResearchOutcome.PARTIAL, failure_class="timeout" if self._deadline_expired(latest) else "provider_disabled", consumed=consumed, evidence=tuple(evidence))
                        result = await provider_task
                        _emit_research_span(self.metrics, "aura.research.browser_fetch@1", "research.fetch_browser", max(0.0, (self.clock() - browser_started) * 1000), outcome="success", dependency="browser_fetcher_port", trace_id=self.trace_id, run_id=self.run_id, conversation_id=self.conversation_id)
                        await self._settle_operation(task, "browser_fetch", browser_request_id, "settled", {"outcome": str(getattr(result, "outcome", "unknown"))})
                    except Exception:
                        await self._settle_operation(task, "browser_fetch", browser_request_id, "failed")
                        raise
                    consumed = replace(consumed, browser_pages=consumed.browser_pages + 1)
                if getattr(result, "outcome", None) == "success":
                    extraction_started = self.clock()
                    source = result.evidence
                    final_url = _validate_public_destination(source.final_url)
                    excerpt = source.extracted_text[: min(MAX_EVIDENCE_EXCERPT, 8 * 1024)]
                    source_id = uuid5(RESEARCH_ID_NAMESPACE, f"evidence:{task.id}:{source.final_url}:{source.content_sha256}")
                    evidence_item = ResearchEvidence(source_id, task.id, source.label or label or final_url, final_url, source.publisher, _utc(datetime.fromisoformat(source.retrieved_at.replace("Z", "+00:00"))), source.detected_media_type, "browser" if source.extraction_method == "playwright_dom" else "static", _utc(datetime.fromisoformat(source.freshness_at.replace("Z", "+00:00"))) if source.freshness_at else None, (excerpt,), source.content_sha256)
                    evidence.append(evidence_item)
                    _emit_research_span(self.metrics, "aura.research.extraction@1", "research.extract", max(0.0, (self.clock() - extraction_started) * 1000), outcome="success", dependency="content_policy", trace_id=self.trace_id, run_id=self.run_id, conversation_id=self.conversation_id)
                operation_response = {"evidence": self._evidence_response(evidence[-1]) if evidence and evidence[-1].url == getattr(getattr(result, "evidence", None), "final_url", None) else {}, "consumed": consumed.as_dict()}
                try:
                    await self._settle_operation(task, "fetch", fetch_request_id, "settled", operation_response)
                except Exception:
                    # The provider response is durably settled before this
                    # process-level failure is surfaced.  Leave the task
                    # resumable so the next worker run can recover the bounded
                    # response from the operation ledger.
                    if operation_response.get("evidence"):
                        return await self._authoritative_task(task)
                    raise
                task = await self.service.checkpoint(task, ResearchStage.FETCHING, operation="fetch_evidence", consumed=consumed, evidence=tuple(evidence))
                task = await self.service.checkpoint(task, ResearchStage.FETCHING, operation="fetch_settled", consumed=consumed, evidence=tuple(evidence))
            synthesized: list[ResearchSynthesis | None] = []
            task_evidence = tuple(item for item in evidence if item.task_id == task.id)
            def validate_evidence() -> None:
                if len({item.source_id for item in task_evidence}) != len(task_evidence):
                    raise ResearchValidationError("duplicate research evidence")
                for item in task_evidence:
                    if not item.excerpts or item.task_id != task.id:
                        raise ResearchValidationError("research evidence failed task validation")
                    _canonical_url(item.url)

            task = await self._phase_checkpoint(
                task,
                ResearchStage.VALIDATING_EVIDENCE,
                "evidence_validate",
                "aura.research.evidence_validation@1",
                "research_evidence_validation_outcome_count",
                "research_evidence_validation_latency_ms",
                "content_policy",
                action=validate_evidence,
            )
            task = await self._phase_checkpoint(
                task,
                ResearchStage.VALIDATING_EVIDENCE,
                "citation_validate",
                "aura.research.citation_validation@1",
                "research_citation_validation_outcome_count",
                "research_citation_validation_latency_ms",
                "citation_validator",
                action=lambda: tuple(item.citation() for item in task_evidence),
            )
            async def run_trusted_synthesis() -> None:
                nonlocal synthesis_failed_closed
                synthesis_failed_closed = True
                synthesized.append(await self._trusted_synthesis(plan, task_evidence))

            task = await self._phase_checkpoint(task, ResearchStage.SYNTHESIZING, "synthesize", "aura.research.synthesis@1", "research_synthesis_outcome_count", "research_synthesis_latency_ms", "model_gateway", action=run_trusted_synthesis)
            synthesis = synthesized[0]
            if synthesis is None:
                return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="provider_unavailable", consumed=consumed, evidence=(), citations=(), artifact_refs=())
            citations = tuple(item.citation() for item in evidence if any(claim.source_id == item.source_id for claim in synthesis.claims))
            artifact_refs: tuple[Mapping[str, object], ...] = ()
            if synthesis.sufficient and evidence and self.artifacts is not None:
                report = "# Research report\n\n" + task.question + "\n\n" + "\n".join(f"- {claim.text} [source:{claim.source_id}]" for claim in synthesis.claims)
                create = getattr(self.artifacts, "create_generated_artifact", None)
                if callable(create):
                    artifact = await cast(Any, create)(task.issuer, task.subject, kind="research_report", title="Research report", media_type="text/markdown; charset=utf-8", content=report, creator_component="aura.research.synthesis@1", lineage=GeneratedArtifactLineage(task_id=task.id), idempotency_key=f"research-report:{task.id}")
                    artifact_refs = ({"artifactId": str(artifact.id), "kind": artifact.kind, "revision": artifact.current_revision},)
            compact_answer = "\n".join(
                f"- {claim.text} [source:{claim.source_id}]" for claim in synthesis.claims
            )[:32 * 1024]
            task = await self._settle(task, ResearchOutcome.COMPLETED if synthesis.sufficient else ResearchOutcome.PARTIAL, consumed=consumed, evidence=tuple(item for item in evidence if any(claim.source_id == item.source_id for claim in synthesis.claims)), citations=citations, artifact_refs=artifact_refs, synthesized_answer=compact_answer or "Research completed without a supported claim.")
            return task
        except ResearchConflict:
            # A concurrent owner command may win between the last reload and a
            # checkpoint. Reconcile from the authoritative row instead of
            # leaving the durable task stranded in ``draining``.
            latest = await self._authoritative_task(task)
            if latest.cancellation_requested and not latest.terminal:
                return await self._settle(
                    latest,
                    ResearchOutcome.CANCELED,
                    consumed=latest.consumed_budget,
                    evidence=latest.evidence,
                    citations=latest.citations,
                    artifact_refs=latest.artifact_refs,
                )
            raise
        except ResearchNotFound:
            raise
        except Exception as exc:
            if isinstance(exc, (ResearchConflict, ResearchNotFound)):
                raise
            return await self._settle(task, ResearchOutcome.PARTIAL if evidence else ResearchOutcome.FAILED, failure_class="provider_unavailable", consumed=consumed, evidence=() if synthesis_failed_closed else tuple(evidence), citations=())


__all__ = [
    "DEFAULT_BUDGETS", "MemoryResearchRepository", "ResearchAdmission", "ResearchApplicationService", "ResearchBudget", "ResearchBudgetConsumption", "ResearchCitation", "ResearchClaim", "ResearchConflict", "ResearchContentState", "ResearchCoordinator", "ResearchCitationLifecycleQueryPort", "ResearchDeliveryClaim", "ResearchEntityProvenance", "ResearchEntityResolution", "ResearchEntityResolutionPort", "ResearchEntityType", "ResearchEvidence", "ResearchError", "ResearchFailureClass", "ResearchMode", "ResearchNotFound", "ResearchOutcome", "ResearchPlan", "ResearchSettings", "ResearchStage", "ResearchSynthesis", "ResearchSynthesisPort", "ResearchTask", "ResearchTerminalDeliverySnapshot", "ResearchValidationError",
]
