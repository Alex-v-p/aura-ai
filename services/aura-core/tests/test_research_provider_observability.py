"""Privacy and bounded telemetry tests for research provider clients."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.providers.research.client import (
    ProviderClientError,
    ResearchTraceContext,
    SearchProvider,
    StaticFetchProvider,
    record_extraction_telemetry,
    telemetry_classification,
)
from aura_core.providers.research.contracts import (
    ProviderErrorCode,
    SearchRequest,
    StaticFetchRequest,
)
from aura_core.providers.research.evaluation import (
    EVALUATORS,
    run_research_evaluation_fixtures,
)
from aura_core.providers.research.signing import EnvelopeSigner


class _Metrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, str]]] = []

    def increment(self, component: str, name: str, **attributes: str) -> None:
        self.events.append((component, name, attributes))

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
        del value
        self.events.append((component, name, attributes))


@pytest.mark.asyncio
async def test_provider_telemetry_is_metadata_only() -> None:
    metrics = _Metrics()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schemaVersion": "research-provider.v1",
                "envelopeType": "result",
                "responseId": str(uuid4()),
                "requestId": str(request_id),
                "audience": "searxng",
                "method": "search",
                "producedAt": "2026-10-28T10:00:03Z",
                "result": {
                    "kind": "search_result",
                    "outcome": "success",
                    "candidates": [],
                },
            },
        )

    query = "owner-private-query"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        request_id = uuid4()
        await SearchProvider(
            "http://provider.invalid",
            EnvelopeSigner.generate(),
            enabled=True,
            telemetry=metrics,
            http_client=http,
        ).search(SearchRequest(request_id=request_id, query=query))

    rendered = repr(metrics.events)
    assert query not in rendered
    assert all("url" not in attrs and "domain" not in attrs for _, _, attrs in metrics.events)
    assert {name for _, name, _ in metrics.events} == {
        "research_search_outcome_count",
        "research_search_latency_ms",
        "research_search_candidate_count",
    }


def test_production_metadata_sink_accepts_research_spans_and_closed_dimensions() -> None:
    metrics = MetadataMetrics()
    for component, operation, dependency, provider, prefix in (
        ("aura.research.search@1", "research.search", "searxng_port", "searxng", "research_search"),
        (
            "aura.research.static_fetch@1",
            "research.fetch_static",
            "static_fetcher_port",
            "static_fetcher",
            "research_static_fetch",
        ),
        (
            "aura.research.browser_fetch@1",
            "research.fetch_browser",
            "browser_fetcher_port",
            "browser_fetcher",
            "research_browser_fetch",
        ),
    ):
        attrs = {
            "operation": operation,
            "outcome": "success",
            "dependency": dependency,
            "provider": provider,
            "degradation": "none",
        }
        metrics.increment(component, f"{prefix}_outcome_count", **attrs)
        metrics.observe(component, f"{prefix}_latency_ms", 1.0, **attrs)
        metrics.record_span(
            component,
            operation,
            1.0,
            trace_id=uuid4().hex,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
            dependency=dependency,
            outcome="success",
            provider=provider,
            degradation="none",
        )
    record_extraction_telemetry(metrics, outcome="success", elapsed_ms=1.0)
    record_extraction_telemetry(
        metrics, outcome="rejected", elapsed_ms=2.0, error_class="type_rejected"
    )
    records = metrics.snapshot()
    assert {record.component_id for record in records} == {
        "aura.research.search@1",
        "aura.research.static_fetch@1",
        "aura.research.browser_fetch@1",
        "aura.research.extraction@1",
    }
    assert all(
        key not in dict(record.dimensions)
        for record in records
        for key in ("request_id", "provider_request_id", "url", "query", "content")
    )
    extraction_names = {
        record.metric for record in records if record.component_id == "aura.research.extraction@1"
    }
    assert extraction_names == {
        "research_extraction_outcome_count",
        "research_extraction_latency_ms",
        "research_extraction_rejected_count",
    }


@pytest.mark.asyncio
async def test_correlated_provider_error_survives_http_502_and_records_limit_class() -> None:
    metrics = MetadataMetrics()
    request_id = uuid4()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "schemaVersion": "research-provider.v1",
                "envelopeType": "error",
                "responseId": str(uuid4()),
                "requestId": str(request_id),
                "audience": "static_fetcher",
                "method": "fetch",
                "producedAt": "2026-10-28T10:00:03Z",
                "error": {"code": "size_exceeded", "retryable": False},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = StaticFetchProvider(
            "http://provider.invalid",
            EnvelopeSigner.generate(),
            enabled=True,
            telemetry=metrics,
            http_client=http,
        )
        with pytest.raises(ProviderClientError, match="size_exceeded"):
            await provider.fetch(
                StaticFetchRequest.model_validate(
                    {
                        "request_id": request_id,
                        "targetUrl": "https://example.com",
                        "admissionKind": "owner_seed",
                    }
                )
            )

    records = metrics.snapshot()
    assert any(record.metric == "research_static_fetch_limit_rejection_count" for record in records)
    outcome_records = [
        record for record in records if record.metric == "research_static_fetch_outcome_count"
    ]
    assert dict(outcome_records[0].dimensions)["error_class"] == "size_exceeded"
    assert dict(outcome_records[0].dimensions)["degradation"] == "none"
    assert not any(record.component_id == "aura.research.extraction@1" for record in records)


@pytest.mark.asyncio
async def test_admitted_trace_context_is_propagated_to_provider_span() -> None:
    metrics = MetadataMetrics()
    trace_id = "a" * 32
    parent_span_id = "b" * 16
    request_id = uuid4()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schemaVersion": "research-provider.v1",
                "envelopeType": "result",
                "responseId": str(uuid4()),
                "requestId": str(request_id),
                "audience": "searxng",
                "method": "search",
                "producedAt": "2026-10-28T10:00:03Z",
                "result": {"kind": "search_result", "outcome": "success", "candidates": []},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SearchProvider(
            "http://provider.invalid",
            EnvelopeSigner.generate(),
            enabled=True,
            telemetry=metrics,
            http_client=http,
        )
        await provider.search(
            SearchRequest(request_id=request_id, query="bounded"),
            trace_context=ResearchTraceContext(trace_id, parent_span_id),
        )

    span = next(record for record in metrics.snapshot() if record.kind == "span")
    assert span.trace_id == trace_id
    assert span.parent_span_id == parent_span_id
    assert "correlation" not in dict(span.dimensions)


def test_provider_error_telemetry_mapping_is_exhaustive_and_bounded() -> None:
    for code in ProviderErrorCode:
        outcome, error_class, degradation = telemetry_classification(code.value)
        assert outcome in {"success", "rejected", "partial", "canceled", "timeout", "failed"}
        assert degradation in {"none", "provider_disabled", "provider_unavailable", "draining"}
        assert error_class is None or error_class in {
            "validation_failed",
            "provider_disabled",
            "provider_unavailable",
            "destination_blocked",
            "redirect_blocked",
            "type_rejected",
            "size_exceeded",
            "resource_exhausted",
            "timeout",
        }
    assert telemetry_classification("success") == ("success", None, "none")
    assert telemetry_classification("draining") == (
        "failed",
        "provider_unavailable",
        "draining",
    )
    assert telemetry_classification("audience_mismatch") == (
        "rejected",
        "validation_failed",
        "none",
    )
    assert telemetry_classification("extraction_failed") == (
        "partial",
        "validation_failed",
        "none",
    )


def test_trace_context_requires_canonical_ids() -> None:
    with pytest.raises(ValueError):
        ResearchTraceContext("not-a-trace")
    with pytest.raises(ValueError):
        ResearchTraceContext("a" * 32, "not-a-span")


@pytest.mark.asyncio
async def test_reusable_provider_keeps_trace_context_per_call() -> None:
    metrics = MetadataMetrics()
    request_ids = [uuid4(), uuid4()]

    async def handler(request: httpx.Request) -> httpx.Response:
        request_id = request_ids.pop(0)
        return httpx.Response(
            200,
            json={
                "schemaVersion": "research-provider.v1",
                "envelopeType": "result",
                "responseId": str(uuid4()),
                "requestId": str(request_id),
                "audience": "searxng",
                "method": "search",
                "producedAt": "2026-10-28T10:00:03Z",
                "result": {"kind": "search_result", "outcome": "success", "candidates": []},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SearchProvider(
            "http://provider.invalid",
            EnvelopeSigner.generate(),
            enabled=True,
            telemetry=metrics,
            http_client=http,
        )
        await provider.search(
            SearchRequest(request_id=request_ids[0], query="one"),
            trace_context=ResearchTraceContext("a" * 32, "b" * 16),
        )
        await provider.search(SearchRequest(request_id=request_ids[0], query="two"))

    spans = [record for record in metrics.snapshot() if record.kind == "span"]
    assert len(spans) == 1
    assert spans[0].trace_id == "a" * 32
    assert all(
        "trace_id" not in dict(record.dimensions)
        and "parent_span_id" not in dict(record.dimensions)
        for record in metrics.snapshot()
        if record.kind == "metric"
    )


def test_research_evaluators_are_versioned_and_runner_only() -> None:
    assert set(EVALUATORS) == {
        "research_destination_policy@1",
        "research_prompt_injection_resistance@1",
        "research_failure_isolation@1",
        "research_privacy@1",
    }
    suite = run_research_evaluation_fixtures()
    replay = run_research_evaluation_fixtures()
    assert suite == replay
    for result in suite.baseline:
        assert result.outcome == "pass"
        assert result.evaluator_version == "1.0.0"
        assert result.score == 1.0
        assert all(
            key not in result.payload()
            for key in ("query", "url", "content", "excerpt", "requestId")
        )
    # Evidence constructors and the arbitrary score function are private: a
    # caller can only obtain an authoritative result from the fixture runner.
    import aura_core.providers.research.evaluation as evaluation

    assert not hasattr(evaluation, "evaluate")
    assert not hasattr(evaluation, "DestinationPolicyEvidence")


def test_executable_research_fixtures_derive_positive_and_regression_results() -> None:
    suite = run_research_evaluation_fixtures()
    assert {result.outcome for result in suite.baseline} == {"pass"}
    assert {result.outcome for result in suite.regressions} == {"fail"}
    prompt_regression = suite.regressions[1]
    assert prompt_regression.metadata
    metadata = dict(prompt_regression.metadata)
    assert metadata["instruction_mutations"] == "0"
    assert metadata["tool_permission_mutations"] == "1"
    assert metadata["accepted_untrusted_directives"] == "1"
