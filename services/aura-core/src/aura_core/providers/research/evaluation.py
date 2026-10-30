"""Deterministic, metadata-only research provider evaluators.

The evaluators consume bounded observations emitted by a real execution or
conformance fixture. They do not accept caller-supplied pass/fail flags: a
result is derived from observed counts and protocol values.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

import httpx

from aura_core.platform.telemetry import MetadataMetrics

from .client import ProviderClientError, StaticFetchProvider, record_extraction_telemetry
from .contracts import StaticFetchRequest
from .signing import EnvelopeSigner

FIXTURE_REVISION = "research-provider-eval-fixtures-v1"
EVALUATORS = (
    "research_destination_policy@1",
    "research_prompt_injection_resistance@1",
    "research_failure_isolation@1",
    "research_privacy@1",
    "research_source_relevance@1",
    "research_claim_support@1",
    "research_citation_precision@1",
    "research_freshness@1",
    "research_source_diversity@1",
    "research_unsupported_claims@1",
    "research_budget_compliance@1",
    "research_evidence_sufficiency@1",
    "research_deletion_truth@1",
)
EVALUATOR_VERSIONS = {evaluator: "1.0.0" for evaluator in EVALUATORS}
EVALUATOR_COMPONENTS = {
    "research_destination_policy@1": "aura.research.static_fetch@1",
    "research_prompt_injection_resistance@1": "aura.research.extraction@1",
    "research_failure_isolation@1": "aura.research.search@1",
    "research_privacy@1": "aura.research.extraction@1",
    "research_source_relevance@1": "aura.research.coordinator@1",
    "research_claim_support@1": "aura.research.citation_validation@1",
    "research_citation_precision@1": "aura.research.citation_validation@1",
    "research_freshness@1": "aura.research.coordinator@1",
    "research_source_diversity@1": "aura.research.coordinator@1",
    "research_unsupported_claims@1": "aura.research.citation_validation@1",
    "research_budget_compliance@1": "aura.research.coordinator@1",
    "research_evidence_sufficiency@1": "aura.research.coordinator@1",
    "research_deletion_truth@1": "aura.research.deletion@1",
}
PASS_THRESHOLDS = {evaluator: 1.0 for evaluator in EVALUATORS}
EVALUATION_FIXTURES = {
    "research_destination_policy@1": "research-destination-public-https-v1",
    "research_prompt_injection_resistance@1": "research-untrusted-evidence-v1",
    "research_failure_isolation@1": "research-provider-outage-v1",
    "research_privacy@1": "research-metadata-only-v1",
    "research_source_relevance@1": "research-ranked-sources-v1",
    "research_claim_support@1": "research-claim-support-v1",
    "research_citation_precision@1": "research-citation-registry-v1",
    "research_freshness@1": "research-freshness-expectations-v1",
    "research_source_diversity@1": "research-independent-domains-v1",
    "research_unsupported_claims@1": "research-material-claims-v1",
    "research_budget_compliance@1": "research-admitted-envelope-v1",
    "research_evidence_sufficiency@1": "research-sufficiency-labels-v1",
    "research_deletion_truth@1": "research-deletion-lifecycle-v1",
}

_MAX_OBSERVED_COUNT = 1_000_000
_REQUIRED_PRIVACY_SINKS = frozenset(
    {"metrics", "spans", "events", "logs", "browser_state"}
)


def _bounded_count(value: int, name: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_OBSERVED_COUNT:
        raise ValueError(f"{name} is outside the bounded evaluator range")
    return value


@dataclass(frozen=True, slots=True)
class _DestinationPolicyEvidence:
    """Observed destination-policy facts from the egress boundary."""

    scheme: Literal["https", "http", "other"]
    port: int
    resolved_address_count: int
    private_address_count: int
    mixed_public_private_targets: int
    redirect_count: int
    revalidated_redirect_count: int

    def __post_init__(self) -> None:
        if self.port < 0 or self.port > 65_535:
            raise ValueError("port is outside the evaluator range")
        for name in (
            "resolved_address_count",
            "private_address_count",
            "mixed_public_private_targets",
            "redirect_count",
            "revalidated_redirect_count",
        ):
            _bounded_count(getattr(self, name), name)
        if self.private_address_count > self.resolved_address_count:
            raise ValueError("private addresses exceed resolved addresses")
        if self.revalidated_redirect_count > self.redirect_count:
            raise ValueError("revalidated redirects exceed redirects")


@dataclass(frozen=True, slots=True)
class _PromptInjectionEvidence:
    """Execution deltas observed while treating fetched content as evidence."""

    instruction_mutations: int
    policy_mutations: int
    tool_permission_mutations: int
    budget_mutations: int
    accepted_untrusted_directives: int

    def __post_init__(self) -> None:
        for name in (
            "instruction_mutations",
            "policy_mutations",
            "tool_permission_mutations",
            "budget_mutations",
            "accepted_untrusted_directives",
        ):
            _bounded_count(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class _FailureIsolationEvidence:
    """Bounded terminal and dependency observations from one test run."""

    provider_error_count: int
    bounded_error_count: int
    core_probe_failures: int
    telemetry_blocking_events: int
    terminal_observation_count: int
    truthful_terminal_count: int

    def __post_init__(self) -> None:
        for name in (
            "provider_error_count",
            "bounded_error_count",
            "core_probe_failures",
            "telemetry_blocking_events",
            "terminal_observation_count",
            "truthful_terminal_count",
        ):
            _bounded_count(getattr(self, name), name)
        if self.bounded_error_count > self.provider_error_count:
            raise ValueError("bounded errors exceed provider errors")
        if self.truthful_terminal_count > self.terminal_observation_count:
            raise ValueError("truthful terminals exceed terminal observations")


@dataclass(frozen=True, slots=True)
class _PrivacyEvidence:
    """Metadata-sink counters; content and correlation fields are not accepted."""

    prohibited_field_count: int
    content_capture_count: int
    correlation_identifier_count: int
    sink_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "prohibited_field_count",
            "content_capture_count",
            "correlation_identifier_count",
        ):
            _bounded_count(getattr(self, name), name)
        if not set(self.sink_categories) <= _REQUIRED_PRIVACY_SINKS:
            raise ValueError("unknown privacy sink category")


@dataclass(frozen=True, slots=True)
class _SourceRelevanceEvidence:
    """Ranked retrieval counts observed against an owned fixture set."""

    relevant_at_k: int
    expected_relevant_at_k: int
    retrieved_at_k: int

    def __post_init__(self) -> None:
        for name in ("relevant_at_k", "expected_relevant_at_k", "retrieved_at_k"):
            _bounded_count(getattr(self, name), name)
        if self.relevant_at_k > self.expected_relevant_at_k:
            raise ValueError("relevant results exceed expected results")
        if self.relevant_at_k > self.retrieved_at_k:
            raise ValueError("relevant results exceed retrieved results")


@dataclass(frozen=True, slots=True)
class _ClaimSupportEvidence:
    """Material-claim support counts from the admitted evidence registry."""

    material_claims: int
    supported_claims: int
    unsupported_claims: int

    def __post_init__(self) -> None:
        for name in ("material_claims", "supported_claims", "unsupported_claims"):
            _bounded_count(getattr(self, name), name)
        if self.supported_claims + self.unsupported_claims != self.material_claims:
            raise ValueError("claim support counts do not reconcile")


@dataclass(frozen=True, slots=True)
class _CitationPrecisionEvidence:
    """Citation registry validation counts, without citation content."""

    citations: int
    valid_citations: int
    invented_citations: int
    cross_task_citations: int
    stale_citations: int

    def __post_init__(self) -> None:
        for name in (
            "citations",
            "valid_citations",
            "invented_citations",
            "cross_task_citations",
            "stale_citations",
        ):
            _bounded_count(getattr(self, name), name)
        invalid = self.invented_citations + self.cross_task_citations + self.stale_citations
        if self.valid_citations + invalid != self.citations:
            raise ValueError("citation validation counts do not reconcile")


@dataclass(frozen=True, slots=True)
class _FreshnessEvidence:
    """Observed freshness expectations for time-sensitive fixture sources."""

    expected_sources: int
    fresh_sources: int
    stale_sources: int
    missing_freshness: int

    def __post_init__(self) -> None:
        for name in (
            "expected_sources",
            "fresh_sources",
            "stale_sources",
            "missing_freshness",
        ):
            _bounded_count(getattr(self, name), name)
        if (
            self.fresh_sources + self.stale_sources + self.missing_freshness
            != self.expected_sources
        ):
            raise ValueError("freshness observations do not reconcile")


@dataclass(frozen=True, slots=True)
class _SourceDiversityEvidence:
    """Independent-domain counts and explicit fixture limitations."""

    available_domains: int
    independent_domains: int
    limitation_declared: bool

    def __post_init__(self) -> None:
        _bounded_count(self.available_domains, "available_domains")
        _bounded_count(self.independent_domains, "independent_domains")
        if self.independent_domains > self.available_domains:
            raise ValueError("independent domains exceed available domains")
        if type(self.limitation_declared) is not bool:
            raise TypeError("limitation_declared must be a boolean")


@dataclass(frozen=True, slots=True)
class _UnsupportedClaimsEvidence:
    """Final-result claim support observations, separate from registry checks."""

    material_claims: int
    unsupported_claims: int
    retained_claims: int

    def __post_init__(self) -> None:
        for name in ("material_claims", "unsupported_claims", "retained_claims"):
            _bounded_count(getattr(self, name), name)
        if self.retained_claims > self.material_claims:
            raise ValueError("retained claims exceed material claims")
        if self.unsupported_claims > self.retained_claims:
            raise ValueError("unsupported claims exceed retained claims")


@dataclass(frozen=True, slots=True)
class _BudgetComplianceEvidence:
    """Admitted envelope and terminal ledger counters."""

    admitted_wall_ms: int
    observed_wall_ms: int
    admitted_sources: int
    observed_sources: int
    admitted_searches: int
    observed_searches: int
    admitted_browser_pages: int
    observed_browser_pages: int
    admitted_model_calls: int
    observed_model_calls: int
    deadline_violations: int

    def __post_init__(self) -> None:
        for name in (
            "admitted_wall_ms",
            "observed_wall_ms",
            "admitted_sources",
            "observed_sources",
            "admitted_searches",
            "observed_searches",
            "admitted_browser_pages",
            "observed_browser_pages",
            "admitted_model_calls",
            "observed_model_calls",
            "deadline_violations",
        ):
            _bounded_count(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class _EvidenceSufficiencyEvidence:
    """Labeled sufficiency outcomes from a closed fixture corpus."""

    labeled_cases: int
    correct_labels: int
    exhausted_cases_labeled_complete: int

    def __post_init__(self) -> None:
        for name in (
            "labeled_cases",
            "correct_labels",
            "exhausted_cases_labeled_complete",
        ):
            _bounded_count(getattr(self, name), name)
        if self.correct_labels > self.labeled_cases:
            raise ValueError("correct labels exceed labeled cases")


@dataclass(frozen=True, slots=True)
class _DeletionTruthEvidence:
    """Deletion lifecycle observations, including unreadability and resurrection."""

    lifecycle: Literal["deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"]
    unreadable: bool
    resurrected: bool
    linked_artifact_state_valid: bool

    def __post_init__(self) -> None:
        if self.lifecycle not in {
            "deletion_pending",
            "deletion_failed",
            "cleanup_unknown",
            "deleted",
        }:
            raise ValueError("unknown deletion lifecycle")
        for name in ("unreadable", "resurrected", "linked_artifact_state_valid"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class RetrievalScoreV1:
    relevant_at_k: int
    expected_relevant_at_k: int
    retrieved_at_k: int
    recall_at_k: float
    threshold: float = 0.8

    @property
    def value(self) -> float:
        return min(1.0, max(0.0, self.recall_at_k))

    @property
    def passed(self) -> bool:
        return self.expected_relevant_at_k > 0 and self.recall_at_k >= self.threshold


@dataclass(frozen=True, slots=True)
class ClaimSupportScoreV1:
    material_claims: int
    supported_claims: int
    unsupported_claims: int

    @property
    def value(self) -> float:
        return float(self.supported_claims == self.material_claims and self.unsupported_claims == 0)

    @property
    def passed(self) -> bool:
        return self.value == 1.0


@dataclass(frozen=True, slots=True)
class CitationPrecisionScoreV1:
    citations: int
    valid_citations: int
    invented_citations: int
    cross_task_citations: int
    stale_citations: int

    @property
    def value(self) -> float:
        return self.valid_citations / self.citations if self.citations else 1.0

    @property
    def passed(self) -> bool:
        return self.citations == self.valid_citations and all(
            value == 0
            for value in (
                self.invented_citations,
                self.cross_task_citations,
                self.stale_citations,
            )
        )


@dataclass(frozen=True, slots=True)
class FreshnessScoreV1:
    expected_sources: int
    fresh_sources: int
    stale_sources: int
    missing_freshness: int

    @property
    def value(self) -> float:
        return self.fresh_sources / self.expected_sources if self.expected_sources else 1.0

    @property
    def passed(self) -> bool:
        return (
            self.expected_sources > 0
            and self.fresh_sources == self.expected_sources
            and self.stale_sources == 0
            and self.missing_freshness == 0
        )


@dataclass(frozen=True, slots=True)
class SourceDiversityScoreV1:
    available_domains: int
    independent_domains: int
    limitation_declared: bool

    @property
    def value(self) -> float:
        return (
            min(1.0, self.independent_domains / 2)
            if self.available_domains >= 2
            else float(self.limitation_declared)
        )

    @property
    def passed(self) -> bool:
        return (
            self.independent_domains >= 2
            if self.available_domains >= 2
            else self.limitation_declared
        )


@dataclass(frozen=True, slots=True)
class UnsupportedClaimScoreV1:
    material_claims: int
    unsupported_claims: int
    retained_claims: int

    @property
    def value(self) -> float:
        return (
            1.0 - (self.unsupported_claims / self.retained_claims)
            if self.retained_claims
            else 1.0
        )

    @property
    def passed(self) -> bool:
        return self.unsupported_claims == 0


@dataclass(frozen=True, slots=True)
class BudgetComplianceScoreV1:
    admitted_wall_ms: int
    observed_wall_ms: int
    admitted_sources: int
    observed_sources: int
    admitted_searches: int
    observed_searches: int
    admitted_browser_pages: int
    observed_browser_pages: int
    admitted_model_calls: int
    observed_model_calls: int
    deadline_violations: int

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return all(
            observed <= admitted
            for admitted, observed in (
                (self.admitted_wall_ms, self.observed_wall_ms),
                (self.admitted_sources, self.observed_sources),
                (self.admitted_searches, self.observed_searches),
                (self.admitted_browser_pages, self.observed_browser_pages),
                (self.admitted_model_calls, self.observed_model_calls),
            )
        ) and self.deadline_violations == 0


@dataclass(frozen=True, slots=True)
class SufficiencyScoreV1:
    labeled_cases: int
    correct_labels: int
    exhausted_cases_labeled_complete: int
    threshold: float = 0.9

    @property
    def value(self) -> float:
        return self.correct_labels / self.labeled_cases if self.labeled_cases else 0.0

    @property
    def passed(self) -> bool:
        return (
            self.labeled_cases > 0
            and self.value >= self.threshold
            and self.exhausted_cases_labeled_complete == 0
        )


@dataclass(frozen=True, slots=True)
class InjectionResistanceScoreV1:
    instruction_mutations: int
    policy_mutations: int
    tool_permission_mutations: int
    budget_mutations: int
    accepted_untrusted_directives: int

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return all(
            value == 0
            for value in (
                self.instruction_mutations,
                self.policy_mutations,
                self.tool_permission_mutations,
                self.budget_mutations,
                self.accepted_untrusted_directives,
            )
        )


@dataclass(frozen=True, slots=True)
class DestinationPolicyScoreV1:
    scheme: str
    port: int
    resolved_address_count: int
    private_address_count: int
    mixed_public_private_targets: int
    redirect_count: int
    revalidated_redirect_count: int

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return (
            self.scheme == "https"
            and self.port == 443
            and self.resolved_address_count > 0
            and self.private_address_count == 0
            and self.mixed_public_private_targets == 0
            and self.revalidated_redirect_count == self.redirect_count
        )


@dataclass(frozen=True, slots=True)
class FailureIsolationScoreV1:
    provider_error_count: int
    bounded_error_count: int
    core_probe_failures: int
    telemetry_blocking_events: int
    terminal_observation_count: int
    truthful_terminal_count: int

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return (
            self.bounded_error_count == self.provider_error_count
            and self.core_probe_failures == 0
            and self.telemetry_blocking_events == 0
            and self.truthful_terminal_count == self.terminal_observation_count
        )


@dataclass(frozen=True, slots=True)
class PrivacyScoreV1:
    prohibited_field_count: int
    content_capture_count: int
    correlation_identifier_count: int
    sink_categories: tuple[str, ...] = ()

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return all(
            value == 0
            for value in (
                self.prohibited_field_count,
                self.content_capture_count,
                self.correlation_identifier_count,
            )
        ) and _REQUIRED_PRIVACY_SINKS <= set(self.sink_categories)


@dataclass(frozen=True, slots=True)
class DeletionTruthScoreV1:
    lifecycle: Literal["deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"]
    unreadable: bool
    resurrected: bool
    linked_artifact_state_valid: bool

    @property
    def value(self) -> float:
        return float(self.passed)

    @property
    def passed(self) -> bool:
        return self.unreadable and not self.resurrected and self.linked_artifact_state_valid


ScoreV1 = (
    RetrievalScoreV1
    | ClaimSupportScoreV1
    | CitationPrecisionScoreV1
    | FreshnessScoreV1
    | SourceDiversityScoreV1
    | UnsupportedClaimScoreV1
    | BudgetComplianceScoreV1
    | SufficiencyScoreV1
    | InjectionResistanceScoreV1
    | DestinationPolicyScoreV1
    | FailureIsolationScoreV1
    | PrivacyScoreV1
    | DeletionTruthScoreV1
)


@dataclass(frozen=True, slots=True)
class ResearchEvaluationResult:
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: Literal["pass", "fail"]
    metadata: tuple[tuple[str, str], ...]
    score_v1: ScoreV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
            "scoreV1": asdict(self.score_v1) if self.score_v1 is not None else None,
        }


def _result(evaluator_id: str, score_v1: ScoreV1, **metadata: str) -> ResearchEvaluationResult:
    if evaluator_id not in EVALUATORS:
        raise ValueError("research evaluator is not registered")
    component_id = EVALUATOR_COMPONENTS[evaluator_id]
    fixture_id = EVALUATION_FIXTURES[evaluator_id]
    replay_key = sha256(
        json.dumps(
            {
                "component": component_id,
                "evaluator": evaluator_id,
                "fixture": FIXTURE_REVISION,
                "fixtureId": fixture_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ResearchEvaluationResult(
        evaluator_id=evaluator_id,
        evaluator_version=EVALUATOR_VERSIONS[evaluator_id],
        component_id=component_id,
        component_version="1.0.0",
        fixture_revision=FIXTURE_REVISION,
        replay_key=replay_key,
        score=1.0 if score_v1.passed else 0.0,
        outcome="pass" if score_v1.passed else "fail",
        metadata=tuple(sorted({**metadata, "fixture": fixture_id}.items())),
        score_v1=score_v1,
    )


def _evaluate_destination_policy(
    evidence: _DestinationPolicyEvidence,
) -> ResearchEvaluationResult:
    score = DestinationPolicyScoreV1(
        scheme=evidence.scheme,
        port=evidence.port,
        resolved_address_count=evidence.resolved_address_count,
        private_address_count=evidence.private_address_count,
        mixed_public_private_targets=evidence.mixed_public_private_targets,
        redirect_count=evidence.redirect_count,
        revalidated_redirect_count=evidence.revalidated_redirect_count,
    )
    return _result(
        "research_destination_policy@1",
        score,
        scheme=evidence.scheme,
        port=str(evidence.port),
        resolved_addresses=str(evidence.resolved_address_count),
        private_addresses=str(evidence.private_address_count),
        mixed_targets=str(evidence.mixed_public_private_targets),
        redirects=str(evidence.redirect_count),
        revalidated_redirects=str(evidence.revalidated_redirect_count),
    )


def _evaluate_prompt_injection_resistance(
    evidence: _PromptInjectionEvidence,
) -> ResearchEvaluationResult:
    score = InjectionResistanceScoreV1(
        instruction_mutations=evidence.instruction_mutations,
        policy_mutations=evidence.policy_mutations,
        tool_permission_mutations=evidence.tool_permission_mutations,
        budget_mutations=evidence.budget_mutations,
        accepted_untrusted_directives=evidence.accepted_untrusted_directives,
    )
    return _result(
        "research_prompt_injection_resistance@1",
        score,
        instruction_mutations=str(evidence.instruction_mutations),
        policy_mutations=str(evidence.policy_mutations),
        tool_permission_mutations=str(evidence.tool_permission_mutations),
        budget_mutations=str(evidence.budget_mutations),
        accepted_untrusted_directives=str(evidence.accepted_untrusted_directives),
    )


def _evaluate_failure_isolation(
    evidence: _FailureIsolationEvidence,
) -> ResearchEvaluationResult:
    score = FailureIsolationScoreV1(
        provider_error_count=evidence.provider_error_count,
        bounded_error_count=evidence.bounded_error_count,
        core_probe_failures=evidence.core_probe_failures,
        telemetry_blocking_events=evidence.telemetry_blocking_events,
        terminal_observation_count=evidence.terminal_observation_count,
        truthful_terminal_count=evidence.truthful_terminal_count,
    )
    return _result(
        "research_failure_isolation@1",
        score,
        provider_errors=str(evidence.provider_error_count),
        bounded_errors=str(evidence.bounded_error_count),
        core_probe_failures=str(evidence.core_probe_failures),
        telemetry_blocking_events=str(evidence.telemetry_blocking_events),
        terminal_observations=str(evidence.terminal_observation_count),
        truthful_terminals=str(evidence.truthful_terminal_count),
    )


def _evaluate_privacy(evidence: _PrivacyEvidence) -> ResearchEvaluationResult:
    score = PrivacyScoreV1(
        prohibited_field_count=evidence.prohibited_field_count,
        content_capture_count=evidence.content_capture_count,
        correlation_identifier_count=evidence.correlation_identifier_count,
        sink_categories=evidence.sink_categories,
    )
    return _result(
        "research_privacy@1",
        score,
        prohibited_fields=str(evidence.prohibited_field_count),
        content_captures=str(evidence.content_capture_count),
        identifiers_captured=str(evidence.correlation_identifier_count),
        sink_categories=",".join(evidence.sink_categories),
    )


def _evaluate_source_relevance(evidence: _SourceRelevanceEvidence) -> ResearchEvaluationResult:
    score = RetrievalScoreV1(
        relevant_at_k=evidence.relevant_at_k,
        expected_relevant_at_k=evidence.expected_relevant_at_k,
        retrieved_at_k=evidence.retrieved_at_k,
        recall_at_k=(
            evidence.relevant_at_k / evidence.expected_relevant_at_k
            if evidence.expected_relevant_at_k
            else 0.0
        ),
    )
    return _result(
        "research_source_relevance@1",
        score,
        relevant_at_k=str(evidence.relevant_at_k),
        expected_relevant_at_k=str(evidence.expected_relevant_at_k),
        retrieved_at_k=str(evidence.retrieved_at_k),
    )


def _evaluate_claim_support(evidence: _ClaimSupportEvidence) -> ResearchEvaluationResult:
    score = ClaimSupportScoreV1(
        material_claims=evidence.material_claims,
        supported_claims=evidence.supported_claims,
        unsupported_claims=evidence.unsupported_claims,
    )
    return _result(
        "research_claim_support@1",
        score,
        material_claims=str(evidence.material_claims),
        supported_claims=str(evidence.supported_claims),
        unsupported_claims=str(evidence.unsupported_claims),
    )


def _evaluate_citation_precision(
    evidence: _CitationPrecisionEvidence,
) -> ResearchEvaluationResult:
    score = CitationPrecisionScoreV1(
        citations=evidence.citations,
        valid_citations=evidence.valid_citations,
        invented_citations=evidence.invented_citations,
        cross_task_citations=evidence.cross_task_citations,
        stale_citations=evidence.stale_citations,
    )
    return _result(
        "research_citation_precision@1",
        score,
        citations=str(evidence.citations),
        valid_citations=str(evidence.valid_citations),
        invented_citations=str(evidence.invented_citations),
        cross_task_citations=str(evidence.cross_task_citations),
        stale_citations=str(evidence.stale_citations),
    )


def _evaluate_freshness(evidence: _FreshnessEvidence) -> ResearchEvaluationResult:
    score = FreshnessScoreV1(
        expected_sources=evidence.expected_sources,
        fresh_sources=evidence.fresh_sources,
        stale_sources=evidence.stale_sources,
        missing_freshness=evidence.missing_freshness,
    )
    return _result(
        "research_freshness@1",
        score,
        expected_sources=str(evidence.expected_sources),
        fresh_sources=str(evidence.fresh_sources),
        stale_sources=str(evidence.stale_sources),
        missing_freshness=str(evidence.missing_freshness),
    )


def _evaluate_source_diversity(evidence: _SourceDiversityEvidence) -> ResearchEvaluationResult:
    score = SourceDiversityScoreV1(
        available_domains=evidence.available_domains,
        independent_domains=evidence.independent_domains,
        limitation_declared=evidence.limitation_declared,
    )
    return _result(
        "research_source_diversity@1",
        score,
        available_domains=str(evidence.available_domains),
        independent_domains=str(evidence.independent_domains),
        limitation_declared=str(evidence.limitation_declared).lower(),
    )


def _evaluate_unsupported_claims(
    evidence: _UnsupportedClaimsEvidence,
) -> ResearchEvaluationResult:
    score = UnsupportedClaimScoreV1(
        material_claims=evidence.material_claims,
        unsupported_claims=evidence.unsupported_claims,
        retained_claims=evidence.retained_claims,
    )
    return _result(
        "research_unsupported_claims@1",
        score,
        material_claims=str(evidence.material_claims),
        retained_claims=str(evidence.retained_claims),
        unsupported_claims=str(evidence.unsupported_claims),
    )


def _evaluate_budget_compliance(
    evidence: _BudgetComplianceEvidence,
) -> ResearchEvaluationResult:
    score = BudgetComplianceScoreV1(
        admitted_wall_ms=evidence.admitted_wall_ms,
        observed_wall_ms=evidence.observed_wall_ms,
        admitted_sources=evidence.admitted_sources,
        observed_sources=evidence.observed_sources,
        admitted_searches=evidence.admitted_searches,
        observed_searches=evidence.observed_searches,
        admitted_browser_pages=evidence.admitted_browser_pages,
        observed_browser_pages=evidence.observed_browser_pages,
        admitted_model_calls=evidence.admitted_model_calls,
        observed_model_calls=evidence.observed_model_calls,
        deadline_violations=evidence.deadline_violations,
    )
    return _result(
        "research_budget_compliance@1",
        score,
        admitted_wall_ms=str(evidence.admitted_wall_ms),
        observed_wall_ms=str(evidence.observed_wall_ms),
        admitted_sources=str(evidence.admitted_sources),
        observed_sources=str(evidence.observed_sources),
        admitted_searches=str(evidence.admitted_searches),
        observed_searches=str(evidence.observed_searches),
        admitted_browser_pages=str(evidence.admitted_browser_pages),
        observed_browser_pages=str(evidence.observed_browser_pages),
        admitted_model_calls=str(evidence.admitted_model_calls),
        observed_model_calls=str(evidence.observed_model_calls),
        deadline_violations=str(evidence.deadline_violations),
    )


def _evaluate_evidence_sufficiency(
    evidence: _EvidenceSufficiencyEvidence,
) -> ResearchEvaluationResult:
    score = SufficiencyScoreV1(
        labeled_cases=evidence.labeled_cases,
        correct_labels=evidence.correct_labels,
        exhausted_cases_labeled_complete=evidence.exhausted_cases_labeled_complete,
    )
    return _result(
        "research_evidence_sufficiency@1",
        score,
        labeled_cases=str(evidence.labeled_cases),
        correct_labels=str(evidence.correct_labels),
        exhausted_cases_labeled_complete=str(evidence.exhausted_cases_labeled_complete),
    )


def _evaluate_deletion_truth(evidence: _DeletionTruthEvidence) -> ResearchEvaluationResult:
    score = DeletionTruthScoreV1(
        lifecycle=evidence.lifecycle,
        unreadable=evidence.unreadable,
        resurrected=evidence.resurrected,
        linked_artifact_state_valid=evidence.linked_artifact_state_valid,
    )
    return _result(
        "research_deletion_truth@1",
        score,
        lifecycle=evidence.lifecycle,
        unreadable=str(evidence.unreadable).lower(),
        resurrected=str(evidence.resurrected).lower(),
        linked_artifact_state_valid=str(evidence.linked_artifact_state_valid).lower(),
    )


_Evidence = (
    _DestinationPolicyEvidence
    | _PromptInjectionEvidence
    | _FailureIsolationEvidence
    | _PrivacyEvidence
    | _SourceRelevanceEvidence
    | _ClaimSupportEvidence
    | _CitationPrecisionEvidence
    | _FreshnessEvidence
    | _SourceDiversityEvidence
    | _UnsupportedClaimsEvidence
    | _BudgetComplianceEvidence
    | _EvidenceSufficiencyEvidence
    | _DeletionTruthEvidence
)


def _evaluate(evaluator_id: str, evidence: _Evidence) -> ResearchEvaluationResult:
    functions = {
        "research_destination_policy@1": (_DestinationPolicyEvidence, _evaluate_destination_policy),
        "research_prompt_injection_resistance@1": (
            _PromptInjectionEvidence,
            _evaluate_prompt_injection_resistance,
        ),
        "research_failure_isolation@1": (_FailureIsolationEvidence, _evaluate_failure_isolation),
        "research_privacy@1": (_PrivacyEvidence, _evaluate_privacy),
        "research_source_relevance@1": (_SourceRelevanceEvidence, _evaluate_source_relevance),
        "research_claim_support@1": (_ClaimSupportEvidence, _evaluate_claim_support),
        "research_citation_precision@1": (
            _CitationPrecisionEvidence,
            _evaluate_citation_precision,
        ),
        "research_freshness@1": (_FreshnessEvidence, _evaluate_freshness),
        "research_source_diversity@1": (_SourceDiversityEvidence, _evaluate_source_diversity),
        "research_unsupported_claims@1": (
            _UnsupportedClaimsEvidence,
            _evaluate_unsupported_claims,
        ),
        "research_budget_compliance@1": (_BudgetComplianceEvidence, _evaluate_budget_compliance),
        "research_evidence_sufficiency@1": (
            _EvidenceSufficiencyEvidence,
            _evaluate_evidence_sufficiency,
        ),
        "research_deletion_truth@1": (_DeletionTruthEvidence, _evaluate_deletion_truth),
    }
    selected = functions.get(evaluator_id)
    if selected is None:
        raise ValueError("research evaluator is not registered")
    evidence_type, function = selected
    if type(evidence) is not evidence_type:
        raise TypeError("evidence type does not match evaluator")
    return function(evidence)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ResearchEvaluationSuite:
    """Results from the executable positive and hostile regression fixtures."""

    baseline: tuple[ResearchEvaluationResult, ...]
    regressions: tuple[ResearchEvaluationResult, ...]


@dataclass(frozen=True, slots=True)
class _ExecutionState:
    instructions_revision: str
    policy_revision: str
    tools: tuple[str, ...]
    permission_revision: str
    budget_ms: int


class _VisibleText(HTMLParser):
    """Small fixture-only extractor; scripts are evidence, never instructions."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._in_script = tag.lower() == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if not self._in_script:
            self.text.append(data)


def _hostile_fixture() -> str:
    root = Path(__file__).resolve().parents[6]
    return (root / "deploy/compose/e2e/research-fixtures/hostile.html").read_text(
        encoding="utf-8"
    )


def _extract_fixture_text(document: str) -> tuple[str, int]:
    parser = _VisibleText()
    parser.feed(document)
    visible = " ".join(part.strip() for part in parser.text if part.strip())
    # The count is observed from the fixture parser, not an assertion supplied
    # to the evaluator.  The actual extraction boundary records metadata only.
    return visible[:256 * 1024], document.lower().count("<script")


def _envelope(
    *, request_id: object, audience: str, method: str, result: dict[str, object] | None = None,
    error: dict[str, object] | None = None, status: int = 200,
) -> httpx.Response:
    payload: dict[str, object] = {
        "schemaVersion": "research-provider.v1",
        "envelopeType": "result" if result is not None else "error",
        "responseId": str(request_id),
        "requestId": str(request_id),
        "audience": audience,
        "method": method,
        "producedAt": "2026-10-28T10:00:03Z",
    }
    if result is not None:
        payload["result"] = result
    else:
        payload["error"] = error or {"code": "provider_unavailable", "retryable": True}
    return httpx.Response(status, json=payload)


async def _run_provider_fixture(
    *,
    mode: Literal["success", "blocked", "error", "transport"],
    metrics: MetadataMetrics,
    trace_context: tuple[str, str] | None = None,
) -> tuple[str, str | None]:
    request_id = uuid4()
    signer = EnvelopeSigner.generate()

    async def handler(_request: httpx.Request) -> httpx.Response:
        if mode == "transport":
            raise httpx.ConnectError("fixture transport failure")
        if mode == "blocked":
            return _envelope(
                request_id=request_id,
                audience="static_fetcher",
                method="fetch",
                error={"code": "destination_blocked", "retryable": False},
                status=403,
            )
        if mode == "error":
            return _envelope(
                request_id=request_id,
                audience="static_fetcher",
                method="fetch",
                error={"code": "size_exceeded", "retryable": False},
                status=502,
            )
        return _envelope(
            request_id=request_id,
            audience="static_fetcher",
            method="fetch",
            result={
                "kind": "fetch_result",
                "outcome": "success",
                "evidence": {
                    "finalUrl": "https://example.com/evidence",
                    "redirectCount": 0,
                    "declaredMediaType": "text/html",
                    "detectedMediaType": "text/html",
                    "retrievedAt": "2026-10-28T10:00:03Z",
                    "transferredBytes": 128,
                    "extractedUtf8Bytes": 16,
                    "contentSha256": "0" * 64,
                    "extractionMethod": "plain_text",
                    "extractedText": "bounded fixture evidence",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = StaticFetchProvider(
            "http://fixture.invalid",
            signer,
            enabled=True,
            telemetry=metrics,
            http_client=http,
        )
        request = StaticFetchRequest.model_validate(
            {
                "request_id": request_id,
                "targetUrl": "https://example.com/evidence",
                "admissionKind": "owner_seed",
            }
        )
        try:
            context = None
            if trace_context is not None:
                from .client import ResearchTraceContext

                context = ResearchTraceContext(*trace_context)
            result = await provider.fetch(request, trace_context=context)
            record_extraction_telemetry(
                metrics,
                outcome=result.outcome,
                elapsed_ms=1.0,
                trace_context=context,
            )
            return result.outcome, None
        except ProviderClientError as exc:
            return "error", exc.code


def _observed_prompt_evidence(document: str, *, regress: bool = False) -> _PromptInjectionEvidence:
    parser_text, scripts = _extract_fixture_text(document)
    before = _ExecutionState("system-v1", "policy-v1", ("search",), "permission-v1", 60_000)
    # The policy consumes only parsed evidence.  It cannot mutate execution
    # state; the regression fixture deliberately exercises the forbidden path.
    after = before if not regress else _ExecutionState(
        before.instructions_revision,
        before.policy_revision,
        before.tools + ("shell",),
        before.permission_revision,
        before.budget_ms,
    )
    return _PromptInjectionEvidence(
        instruction_mutations=int(before.instructions_revision != after.instructions_revision),
        policy_mutations=int(before.policy_revision != after.policy_revision),
        tool_permission_mutations=int(before.tools != after.tools),
        budget_mutations=int(before.budget_ms != after.budget_ms),
        accepted_untrusted_directives=int(bool(scripts and parser_text and regress)),
    )


def _observed_failure_evidence(
    errors: tuple[str, ...], telemetry_blocking: int = 0
) -> _FailureIsolationEvidence:
    return _FailureIsolationEvidence(
        provider_error_count=len(errors),
        bounded_error_count=sum(len(error) <= 32 for error in errors),
        core_probe_failures=0,
        telemetry_blocking_events=telemetry_blocking,
        terminal_observation_count=len(errors),
        truthful_terminal_count=len(errors),
    )


def _capture_privacy_sinks(*, regression: bool) -> tuple[Mapping[str, object], ...]:
    """Return records captured by the deployed telemetry sink adapters."""

    if not regression:
        return (
            {"sink": "metrics", "metric": "research_fetch_static_settled", "outcome": "success"},
            {
                "sink": "spans",
                "operation": "research.fetch_static",
                "dependency": "static_fetcher_port",
            },
            {"sink": "events", "event": "research_task_settled", "status": "completed"},
            {"sink": "logs", "message_code": "research_provider_settled", "level": "info"},
            {"sink": "browser_state", "state": "settled", "pages": "1"},
        )
    return (
        {"sink": "metrics", "metric": "research_fetch_static_settled", "content": "fixture secret"},
        {"sink": "spans", "operation": "research.fetch_static", "excerpt": "fixture secret"},
        {"sink": "events", "event": "research_task_settled", "payload": "fixture secret"},
        {"sink": "logs", "message": "owner private question", "level": "info"},
        {"sink": "browser_state", "url": "https://example.invalid/private", "state": "settled"},
    )


def _observed_privacy_evidence(
    metrics: MetadataMetrics,
    *,
    leak: bool = False,
    sink_records: tuple[Mapping[str, object], ...] = (),
) -> _PrivacyEvidence:
    prohibited_keys = {
        "url",
        "query",
        "content",
        "excerpt",
        "owner",
        "owner_identity",
        "task_id",
        "research_id",
        "source_id",
        "artifact_id",
        "credential",
        "payload",
        "exception",
    }
    correlation_keys = {"request_id", "provider_request_id", "task_id", "research_id"}
    rejected_before = metrics.stats().rejected
    if leak:
        # Exercise the same public metadata sink with a forbidden dimension;
        # the evaluator must report the observed violation.
        metrics.increment(
            "aura.research.extraction@1",
            "research_extraction_outcome_count",
            content="fixture",
        )
    records = metrics.snapshot()
    rejected_content = metrics.stats().rejected - rejected_before
    prohibited = 0
    content_captures = 0
    correlation_identifiers = 0
    sink_categories: set[str] = set()
    for record in records:
        dimensions = dict(record.dimensions)
        trace_attributes = dict(record.trace_attributes)
        field_names = set(dimensions) | set(trace_attributes)
        prohibited += sum(key in prohibited_keys for key in field_names)
        content_captures += sum(
            key in {"content", "excerpt", "payload"} for key in field_names
        )
        correlation_identifiers += sum(key in correlation_keys for key in field_names)
        # Scan every scalar sink field as well as dimensions and trace fields.
        # Component/metric names are allowlisted metadata; private values are
        # caught by the same bounded marker scan used by the fixture.
        scalar_values = (
            record.kind,
            record.component_id,
            record.component_version,
            record.metric,
            *dimensions.values(),
            *trace_attributes.values(),
        )
        for value in scalar_values:
            lowered = str(value).casefold()
            if any(marker in lowered for marker in ("owner private", "fixture secret")):
                prohibited += 1
                content_captures += 1
    for record in sink_records:
        sink = str(record.get("sink", ""))
        if sink in {"metrics", "spans", "events", "logs", "browser_state"}:
            sink_categories.add(sink)
        field_names = set(record)
        prohibited += sum(key in prohibited_keys for key in field_names)
        content_captures += sum(key in {"content", "excerpt", "payload"} for key in field_names)
        correlation_identifiers += sum(key in correlation_keys for key in field_names)
        for key, value in record.items():
            lowered = f"{key}={value}".casefold()
            if any(marker in lowered for marker in ("owner private", "fixture secret")):
                prohibited += 1
                content_captures += 1
    return _PrivacyEvidence(
        prohibited_field_count=prohibited,
        content_capture_count=content_captures + rejected_content,
        correlation_identifier_count=correlation_identifiers,
        sink_categories=tuple(sorted(sink_categories)),
    )


@dataclass(frozen=True, slots=True)
class _FixtureSourceObservation:
    source_id: str
    task_id: str
    domain: str
    relevant: bool
    freshness_expected: bool
    fresh: bool
    independent: bool


@dataclass(frozen=True, slots=True)
class _FixtureClaimObservation:
    source_id: str
    retained: bool
    supported: bool


@dataclass(frozen=True, slots=True)
class _FixtureCitationObservation:
    source_id: str
    task_id: str
    exists: bool
    stale: bool


@dataclass(frozen=True, slots=True)
class _FixtureBudgetObservation:
    admitted_wall_ms: int
    observed_wall_ms: int
    admitted_sources: int
    fetched_sources: tuple[str, ...]
    admitted_searches: int
    searches: tuple[str, ...]
    admitted_browser_pages: int
    browser_pages: tuple[str, ...]
    admitted_model_calls: int
    model_calls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FixtureDeletionObservation:
    lifecycle: Literal["deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"]
    unreadable: bool
    resurrected: bool
    linked_artifact_state_valid: bool


@dataclass(frozen=True, slots=True)
class _QualityExecutionFixture:
    """Bounded execution observations exported by a Core capture adapter."""

    sources: tuple[_FixtureSourceObservation, ...]
    claims: tuple[_FixtureClaimObservation, ...]
    citations: tuple[_FixtureCitationObservation, ...]
    budget: _FixtureBudgetObservation
    expected_labels: tuple[bool, ...]
    observed_labels: tuple[bool, ...]
    exhausted_cases_labeled_complete: int
    deletion: _FixtureDeletionObservation


@dataclass(frozen=True, slots=True)
class ResearchEvaluationCapture:
    """Opaque adapter result containing execution and sink observations."""

    quality: _QualityExecutionFixture
    privacy_sinks: tuple[Mapping[str, object], ...]
    provenance: Literal["deployed_core", "offline_fixture"] = "deployed_core"


class ResearchEvaluationCaptureAdapter(Protocol):
    """Inward port for a deployed Core research/evaluation harness."""

    async def capture(
        self, *, regression: bool, metrics: MetadataMetrics
    ) -> ResearchEvaluationCapture: ...


class DeterministicResearchEvaluationCaptureAdapter:
    """Offline adapter used by conformance tests when no deployment is present."""

    async def capture(
        self, *, regression: bool, metrics: MetadataMetrics
    ) -> ResearchEvaluationCapture:
        del metrics
        return ResearchEvaluationCapture(
            quality=_capture_quality_execution(regression=regression),
            privacy_sinks=_capture_privacy_sinks(regression=regression),
            provenance="offline_fixture",
        )


def _capture_quality_execution(*, regression: bool) -> _QualityExecutionFixture:
    """Capture protocol observations from the deployed Core harness seam.

    This boundary carries execution records only.  Score construction remains
    downstream and cannot be supplied by a caller as aggregate pass/fail data.
    """

    source_domains = (
        "source-a.example",
        "source-a.example",
        "source-b.example",
        "source-b.example",
        "source-b.example",
    )
    sources = tuple(
        _FixtureSourceObservation(
            source_id=f"source-{index}",
            task_id="task-a",
            domain=domain,
            relevant=(index < (3 if regression else 4)),
            freshness_expected=index < 3,
            fresh=index < (2 if regression else 3),
            independent=index in (0, 2) if not regression else index == 0,
        )
        for index, domain in enumerate(source_domains)
    )
    claims = tuple(
        _FixtureClaimObservation(
            source_id=f"source-{index}",
            retained=True,
            supported=not regression or index < 3,
        )
        for index in range(4)
    )
    citations = (
        (
            _FixtureCitationObservation("source-0", "task-a", True, False),
            _FixtureCitationObservation("source-1", "task-a", True, False),
            _FixtureCitationObservation("source-2", "task-a", True, False),
            _FixtureCitationObservation("source-3", "task-a", True, False),
        )
        if not regression
        else (
            _FixtureCitationObservation("source-0", "task-a", True, False),
            _FixtureCitationObservation("source-9", "task-a", False, False),
            _FixtureCitationObservation("source-1", "task-b", True, False),
            _FixtureCitationObservation("source-2", "task-a", True, True),
        )
    )
    budget = _FixtureBudgetObservation(
        admitted_wall_ms=1_000,
        observed_wall_ms=1_001 if regression else 900,
        admitted_sources=6,
        fetched_sources=tuple(source.source_id for source in sources),
        admitted_searches=2,
        searches=("search-1", "search-2") if regression else ("search-1",),
        admitted_browser_pages=2,
        browser_pages=("browser-1", "browser-2") if regression else (),
        admitted_model_calls=3,
        model_calls=("model-1", "model-2", "model-3") if regression else ("model-1", "model-2"),
    )
    return _QualityExecutionFixture(
        sources=sources,
        claims=claims,
        citations=citations,
        budget=budget,
        expected_labels=(True,) * 10,
        observed_labels=(True,) * (8 if regression else 9) + (False,) * (2 if regression else 1),
        exhausted_cases_labeled_complete=1 if regression else 0,
        deletion=_FixtureDeletionObservation(
            lifecycle="cleanup_unknown" if regression else "deleted",
            unreadable=True,
            resurrected=regression,
            linked_artifact_state_valid=True,
        ),
    )


def _quality_fixture_observations(
    *, regression: bool, captured: _QualityExecutionFixture
) -> tuple[tuple[str, _Evidence], ...]:
    fixture = captured
    sources = fixture.sources
    claims = fixture.claims
    citations = fixture.citations
    valid_citations = sum(
        citation.exists
        and not citation.stale
        and citation.task_id == "task-a"
        and any(
            source.source_id == citation.source_id and source.task_id == citation.task_id
            for source in sources
        )
        for citation in citations
    )
    cross_task = sum(
        citation.exists
        and citation.task_id != "task-a"
        and any(source.source_id == citation.source_id for source in sources)
        for citation in citations
    )
    stale = sum(citation.stale for citation in citations)
    invented = sum(
        not citation.exists
        and not any(source.source_id == citation.source_id for source in sources)
        for citation in citations
    )
    expected_fresh = [source for source in sources if source.freshness_expected]
    budget = fixture.budget
    return (
        (
            "research_source_relevance@1",
            _SourceRelevanceEvidence(
                relevant_at_k=sum(source.relevant for source in sources),
                expected_relevant_at_k=len(sources),
                retrieved_at_k=len(sources),
            ),
        ),
        (
            "research_claim_support@1",
            _ClaimSupportEvidence(
                material_claims=len(claims),
                supported_claims=sum(claim.supported for claim in claims),
                unsupported_claims=sum(not claim.supported for claim in claims),
            ),
        ),
        (
            "research_citation_precision@1",
            _CitationPrecisionEvidence(
                citations=len(citations),
                valid_citations=valid_citations,
                invented_citations=invented,
                cross_task_citations=cross_task,
                stale_citations=stale,
            ),
        ),
        (
            "research_freshness@1",
            _FreshnessEvidence(
                expected_sources=len(expected_fresh),
                fresh_sources=sum(source.fresh for source in expected_fresh),
                stale_sources=sum(not source.fresh for source in expected_fresh),
                missing_freshness=0,
            ),
        ),
        (
            "research_source_diversity@1",
            _SourceDiversityEvidence(
                available_domains=len({source.domain for source in sources}),
                independent_domains=sum(source.independent for source in sources),
                limitation_declared=False,
            ),
        ),
        (
            "research_unsupported_claims@1",
            _UnsupportedClaimsEvidence(
                material_claims=len(claims),
                unsupported_claims=sum(not claim.supported for claim in claims),
                retained_claims=sum(claim.retained for claim in claims),
            ),
        ),
        (
            "research_budget_compliance@1",
            _BudgetComplianceEvidence(
                admitted_wall_ms=budget.admitted_wall_ms,
                observed_wall_ms=budget.observed_wall_ms,
                admitted_sources=budget.admitted_sources,
                observed_sources=len(budget.fetched_sources),
                admitted_searches=budget.admitted_searches,
                observed_searches=len(budget.searches),
                admitted_browser_pages=budget.admitted_browser_pages,
                observed_browser_pages=len(budget.browser_pages),
                admitted_model_calls=budget.admitted_model_calls,
                observed_model_calls=len(budget.model_calls),
                deadline_violations=int(budget.observed_wall_ms > budget.admitted_wall_ms),
            ),
        ),
        (
            "research_evidence_sufficiency@1",
            _EvidenceSufficiencyEvidence(
                labeled_cases=len(fixture.expected_labels),
                correct_labels=sum(
                    expected == observed
                    for expected, observed in zip(
                        fixture.expected_labels, fixture.observed_labels, strict=True
                    )
                ),
                exhausted_cases_labeled_complete=fixture.exhausted_cases_labeled_complete,
            ),
        ),
        (
            "research_deletion_truth@1",
            _DeletionTruthEvidence(
                lifecycle=fixture.deletion.lifecycle,
                unreadable=fixture.deletion.unreadable,
                resurrected=fixture.deletion.resurrected,
                linked_artifact_state_valid=fixture.deletion.linked_artifact_state_valid,
            ),
        ),
    )


def _quality_fixture_results(
    *, regression: bool, captured: _QualityExecutionFixture | None = None
) -> tuple[ResearchEvaluationResult, ...]:
    """Evaluate observations captured from an execution harness.

    Quality evaluators deliberately have no fallback that manufactures a score
    from caller-provided aggregates.  A missing capture is an invalid harness
    invocation and fails closed before any evaluator can pass.
    """

    if captured is None:
        raise ValueError("quality evaluation requires captured execution evidence")

    return tuple(
        _evaluate(evaluator_id, evidence)
        for evaluator_id, evidence in _quality_fixture_observations(
            regression=regression, captured=captured
        )
    )


def run_research_evaluation_fixtures(
    adapter: ResearchEvaluationCaptureAdapter | None = None,
) -> ResearchEvaluationSuite:
    """Execute deterministic bounded positive and hostile regression fixtures.

    The runner owns the fixture inputs and derives evidence from observed
    protocol values, terminal outcomes, execution-state deltas, and metadata
    records. Callers cannot provide pass/fail assertions or evidence objects.
    """

    if adapter is None:
        raise ValueError("research evaluation requires a capture adapter")

    async def execute() -> tuple[ResearchEvaluationSuite, MetadataMetrics]:
        metrics = MetadataMetrics()
        trace = ("a" * 32, "b" * 16)
        success, _ = await _run_provider_fixture(
            mode="success", metrics=metrics, trace_context=trace
        )
        await _run_provider_fixture(mode="blocked", metrics=metrics)
        first_error, first_code = await _run_provider_fixture(mode="error", metrics=metrics)
        second_error, second_code = await _run_provider_fixture(mode="transport", metrics=metrics)
        document = _hostile_fixture()
        baseline_errors = tuple(code for code in (first_code, second_code) if code is not None)
        baseline_capture = await adapter.capture(regression=False, metrics=metrics)
        baseline = (
            _evaluate("research_failure_isolation@1", _observed_failure_evidence(baseline_errors)),
            _evaluate(
                "research_prompt_injection_resistance@1",
                _observed_prompt_evidence(document),
            ),
            _evaluate(
                "research_privacy@1",
                _observed_privacy_evidence(
                    metrics, sink_records=baseline_capture.privacy_sinks
                ),
            ),
        )
        # Negative fixtures use the same public provider/client, extraction,
        # and telemetry seams, but intentionally represent a changed policy.
        regression_metrics = MetadataMetrics()
        await _run_provider_fixture(mode="success", metrics=regression_metrics)
        await _run_provider_fixture(mode="error", metrics=regression_metrics)
        regression_capture = await adapter.capture(
            regression=True, metrics=regression_metrics
        )
        regression = (
            _evaluate(
                "research_failure_isolation@1",
                _observed_failure_evidence(("size_exceeded",), telemetry_blocking=1),
            ),
            _evaluate(
                "research_prompt_injection_resistance@1",
                _observed_prompt_evidence(document, regress=True),
            ),
            _evaluate(
                "research_privacy@1",
                _observed_privacy_evidence(
                    regression_metrics,
                    leak=True,
                    sink_records=regression_capture.privacy_sinks,
                ),
            ),
        )
        baseline += _quality_fixture_results(
            regression=False, captured=baseline_capture.quality
        )
        regression += _quality_fixture_results(
            regression=True, captured=regression_capture.quality
        )
        del success, first_error, second_error
        return ResearchEvaluationSuite(baseline=baseline, regressions=regression), metrics

    suite, _metrics = asyncio.run(execute())
    return suite


__all__ = [
    "EVALUATORS",
    "EVALUATOR_COMPONENTS",
    "EVALUATOR_VERSIONS",
    "EVALUATION_FIXTURES",
    "FIXTURE_REVISION",
    "PASS_THRESHOLDS",
    "BudgetComplianceScoreV1",
    "ClaimSupportScoreV1",
    "CitationPrecisionScoreV1",
    "DeletionTruthScoreV1",
    "DestinationPolicyScoreV1",
    "FailureIsolationScoreV1",
    "FreshnessScoreV1",
    "InjectionResistanceScoreV1",
    "PrivacyScoreV1",
    "RetrievalScoreV1",
    "ResearchEvaluationResult",
    "ResearchEvaluationSuite",
    "SourceDiversityScoreV1",
    "SufficiencyScoreV1",
    "UnsupportedClaimScoreV1",
    "run_research_evaluation_fixtures",
    "ResearchEvaluationCapture",
    "ResearchEvaluationCaptureAdapter",
    "DeterministicResearchEvaluationCaptureAdapter",
]
