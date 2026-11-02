from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "services/aura-research-browser-fetcher/src")

from aura_research_browser_fetcher.app import limits, live  # noqa: E402
from aura_research_browser_fetcher.models import (
    BrowserFetchRequest,  # noqa: E402
    ProviderRequestEnvelope,  # noqa: E402
    ProviderResultEnvelope,  # noqa: E402
)
from aura_research_browser_fetcher.policy import (  # noqa: E402
    DestinationBlocked,
    validate_public_https_url,
)
from aura_research_browser_fetcher.telemetry import ProductionExtractionSink  # noqa: E402


def test_browser_policy_rejects_private_literal_and_non_https() -> None:
    with pytest.raises(DestinationBlocked):
        validate_public_https_url("http://example.com/")


def test_browser_request_is_closed_and_bounded() -> None:
    request = BrowserFetchRequest.model_validate(
        {
            "kind": "browser_fetch_request",
            "targetUrl": "https://example.com/",
            "admissionKind": "owner_seed",
            "justification": "javascript_required",
            "limits": {
                "deadlineMs": 45_000,
                "maxRedirects": 5,
                "maxResponseBytes": 8 * 1024 * 1024,
                "maxExtractedUtf8Bytes": 262_144,
            },
        }
    )
    assert str(request.targetUrl).startswith("https://")
    with pytest.raises(ValueError):
        BrowserFetchRequest.model_validate(
            {
                "kind": "browser_fetch_request",
                "targetUrl": "https://example.com/",
                "admissionKind": "owner_seed",
                "justification": "javascript_required",
                "limits": {
                    "deadlineMs": 45_000,
                    "maxRedirects": 5,
                    "maxResponseBytes": 8 * 1024 * 1024,
                    "maxExtractedUtf8Bytes": 262_144,
                },
                "extra": "blocked",
            }
        )


def test_shared_contract_fixture_parses_browser_request() -> None:
    fixture = json.loads(
        Path("contracts/providers/research/v1/conformance-fixtures.json").read_text()
    )
    document = next(
        item["document"]
        for item in fixture["cases"]
        if item["caseId"] == "valid-browser-fetch-request"
    )
    request = ProviderRequestEnvelope.model_validate(document)
    assert request.audience == "browser_fetcher"
    assert request.method == "fetch"


def test_shared_contract_fixtures_parse_browser_results_and_reject_wrong_method() -> None:
    fixture = json.loads(
        Path("contracts/providers/research/v1/conformance-fixtures.json").read_text()
    )
    for case_id in ("valid-fetch-result", "valid-health-result", "valid-limits-result"):
        document = next(item["document"] for item in fixture["cases"] if item["caseId"] == case_id)
        if document["audience"] == "browser_fetcher":
            ProviderResultEnvelope.model_validate(document)
    wrong = next(
        item["document"]
        for item in fixture["cases"]
        if item["caseId"] == "valid-browser-fetch-request"
    )
    wrong["payload"] = {"kind": "limits_request"}
    with pytest.raises(ValueError, match="method_payload_mismatch"):
        ProviderRequestEnvelope.model_validate(wrong)


@pytest.mark.asyncio
async def test_health_and_limits_are_closed_content_free_contracts() -> None:
    health = await live()
    limits_result = await limits()
    assert health.model_dump() == {
        "kind": "health_result",
        "status": "live",
        "enabled": False,
        "accepting": False,
        "inFlight": 0,
        "contractVersion": "research-provider.v1",
        "degradation": "disabled",
    }
    assert limits_result.limits.service == "browser_fetcher"
    assert not any(
        token in repr(health.model_dump()) for token in ("url", "query", "excerpt", "exception")
    )


def test_browser_production_sink_uses_structured_metadata_only() -> None:
    output = io.StringIO()
    sink = ProductionExtractionSink(writer=output, capacity=8, background=False)
    sink("rejected", 3.0)
    assert sink.drain() == 3
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all(record["component"]["id"] == "aura.research.extraction" for record in records)
    assert all("trace" not in record for record in records if record["kind"] == "metric")
    assert all(record["dimensions"]["dependency"] == "content_policy" for record in records)
    assert not any(
        prohibited in repr(records)
        for prohibited in ("query", "url", "extractedText", "excerpt", "requestId")
    )
