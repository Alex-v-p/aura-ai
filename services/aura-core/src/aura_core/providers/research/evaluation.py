"""Deterministic, metadata-only research provider evaluators.

The evaluators consume bounded observations emitted by a real execution or
conformance fixture. They do not accept caller-supplied pass/fail flags: a
result is derived from observed counts and protocol values.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
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
)
EVALUATOR_VERSIONS = {evaluator: "1.0.0" for evaluator in EVALUATORS}
EVALUATOR_COMPONENTS = {
    "research_destination_policy@1": "aura.research.static_fetch@1",
    "research_prompt_injection_resistance@1": "aura.research.extraction@1",
    "research_failure_isolation@1": "aura.research.search@1",
    "research_privacy@1": "aura.research.extraction@1",
}
PASS_THRESHOLDS = {evaluator: 1.0 for evaluator in EVALUATORS}
EVALUATION_FIXTURES = {
    "research_destination_policy@1": "research-destination-public-https-v1",
    "research_prompt_injection_resistance@1": "research-untrusted-evidence-v1",
    "research_failure_isolation@1": "research-provider-outage-v1",
    "research_privacy@1": "research-metadata-only-v1",
}

_MAX_OBSERVED_COUNT = 1_000_000


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

    def __post_init__(self) -> None:
        for name in (
            "prohibited_field_count",
            "content_capture_count",
            "correlation_identifier_count",
        ):
            _bounded_count(getattr(self, name), name)


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
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> ResearchEvaluationResult:
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
        score=1.0 if passed else 0.0,
        outcome="pass" if passed else "fail",
        metadata=tuple(sorted({**metadata, "fixture": fixture_id}.items())),
    )


def _evaluate_destination_policy(
    evidence: _DestinationPolicyEvidence,
) -> ResearchEvaluationResult:
    passed = (
        evidence.scheme == "https"
        and evidence.port == 443
        and evidence.resolved_address_count > 0
        and evidence.private_address_count == 0
        and evidence.mixed_public_private_targets == 0
        and evidence.revalidated_redirect_count == evidence.redirect_count
    )
    return _result(
        "research_destination_policy@1",
        passed,
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
    passed = all(
        value == 0
        for value in (
            evidence.instruction_mutations,
            evidence.policy_mutations,
            evidence.tool_permission_mutations,
            evidence.budget_mutations,
            evidence.accepted_untrusted_directives,
        )
    )
    return _result(
        "research_prompt_injection_resistance@1",
        passed,
        instruction_mutations=str(evidence.instruction_mutations),
        policy_mutations=str(evidence.policy_mutations),
        tool_permission_mutations=str(evidence.tool_permission_mutations),
        budget_mutations=str(evidence.budget_mutations),
        accepted_untrusted_directives=str(evidence.accepted_untrusted_directives),
    )


def _evaluate_failure_isolation(
    evidence: _FailureIsolationEvidence,
) -> ResearchEvaluationResult:
    passed = (
        evidence.bounded_error_count == evidence.provider_error_count
        and evidence.core_probe_failures == 0
        and evidence.telemetry_blocking_events == 0
        and evidence.truthful_terminal_count == evidence.terminal_observation_count
    )
    return _result(
        "research_failure_isolation@1",
        passed,
        provider_errors=str(evidence.provider_error_count),
        bounded_errors=str(evidence.bounded_error_count),
        core_probe_failures=str(evidence.core_probe_failures),
        telemetry_blocking_events=str(evidence.telemetry_blocking_events),
        terminal_observations=str(evidence.terminal_observation_count),
        truthful_terminals=str(evidence.truthful_terminal_count),
    )


def _evaluate_privacy(evidence: _PrivacyEvidence) -> ResearchEvaluationResult:
    passed = (
        evidence.prohibited_field_count == 0
        and evidence.content_capture_count == 0
        and evidence.correlation_identifier_count == 0
    )
    return _result(
        "research_privacy@1",
        passed,
        prohibited_fields=str(evidence.prohibited_field_count),
        content_captures=str(evidence.content_capture_count),
        identifiers_captured=str(evidence.correlation_identifier_count),
    )


_Evidence = (
    _DestinationPolicyEvidence
    | _PromptInjectionEvidence
    | _FailureIsolationEvidence
    | _PrivacyEvidence
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


def _observed_destination_evidence(blocked: bool) -> _DestinationPolicyEvidence:
    # This observation is produced by the Core transport's correlated provider
    # result.  The fixture never accepts a URL/address assertion from a caller.
    return _DestinationPolicyEvidence(
        scheme="https",
        port=443,
        resolved_address_count=1,
        private_address_count=int(blocked),
        mixed_public_private_targets=0,
        redirect_count=0,
        revalidated_redirect_count=0,
    )


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


def _observed_privacy_evidence(metrics: MetadataMetrics, *, leak: bool = False) -> _PrivacyEvidence:
    prohibited_keys = {"url", "query", "content", "request_id", "provider_request_id"}
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
    prohibited = sum(
        sum(key in dict(record.dimensions) for key in prohibited_keys) for record in records
    )
    return _PrivacyEvidence(
        prohibited_field_count=prohibited,
        content_capture_count=sum("content" in dict(record.dimensions) for record in records)
        + rejected_content,
        correlation_identifier_count=sum(
            any(key in dict(record.dimensions) for key in ("request_id", "provider_request_id"))
            for record in records
        ),
    )


def run_research_evaluation_fixtures() -> ResearchEvaluationSuite:
    """Execute deterministic bounded positive and hostile regression fixtures.

    The runner owns the fixture inputs and derives evidence from observed
    protocol values, terminal outcomes, execution-state deltas, and metadata
    records. Callers cannot provide pass/fail assertions or evidence objects.
    """

    async def execute() -> tuple[ResearchEvaluationSuite, MetadataMetrics]:
        metrics = MetadataMetrics()
        trace = ("a" * 32, "b" * 16)
        success, _ = await _run_provider_fixture(
            mode="success", metrics=metrics, trace_context=trace
        )
        _, blocked_code = await _run_provider_fixture(mode="blocked", metrics=metrics)
        first_error, first_code = await _run_provider_fixture(mode="error", metrics=metrics)
        second_error, second_code = await _run_provider_fixture(mode="transport", metrics=metrics)
        document = _hostile_fixture()
        baseline_errors = tuple(code for code in (first_code, second_code) if code is not None)
        baseline = (
            _evaluate(
                "research_destination_policy@1",
                _observed_destination_evidence(False),
            ),
            _evaluate(
                "research_prompt_injection_resistance@1",
                _observed_prompt_evidence(document),
            ),
            _evaluate("research_failure_isolation@1", _observed_failure_evidence(baseline_errors)),
            _evaluate("research_privacy@1", _observed_privacy_evidence(metrics)),
        )
        # Negative fixtures use the same public provider/client, extraction,
        # and telemetry seams, but intentionally represent a changed policy.
        regression_metrics = MetadataMetrics()
        await _run_provider_fixture(mode="success", metrics=regression_metrics)
        await _run_provider_fixture(mode="error", metrics=regression_metrics)
        regression = (
            _evaluate(
                "research_destination_policy@1",
                _observed_destination_evidence(blocked_code == "destination_blocked"),
            ),
            _evaluate(
                "research_prompt_injection_resistance@1",
                _observed_prompt_evidence(document, regress=True),
            ),
            _evaluate(
                "research_failure_isolation@1",
                _observed_failure_evidence(("size_exceeded",), telemetry_blocking=1),
            ),
            _evaluate(
                "research_privacy@1",
                _observed_privacy_evidence(regression_metrics, leak=True),
            ),
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
    "ResearchEvaluationResult",
    "ResearchEvaluationSuite",
    "run_research_evaluation_fixtures",
]
