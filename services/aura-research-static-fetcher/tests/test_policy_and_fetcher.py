from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, "services/aura-research-static-fetcher/src")

from aura_research_static_fetcher import policy  # noqa: E402
from aura_research_static_fetcher.app import limits, live  # noqa: E402
from aura_research_static_fetcher.fetcher import StaticFetcher  # noqa: E402
from aura_research_static_fetcher.models import (
    ProviderRequestEnvelope,  # noqa: E402
    ProviderResultEnvelope,  # noqa: E402
)
from aura_research_static_fetcher.telemetry import (  # noqa: E402
    BoundedMetadataSink,
    ProductionExtractionSink,
)


def test_policy_rejects_userinfo_and_non_https() -> None:
    with pytest.raises(policy.DestinationBlocked):
        policy.validate_public_https_url("http://example.com/")
    with pytest.raises(policy.DestinationBlocked):
        policy.validate_public_https_url("https://user@example.com/")


def test_shared_contract_fixture_parses_static_request() -> None:
    fixture = json.loads(
        Path("contracts/providers/research/v1/conformance-fixtures.json").read_text()
    )
    document = next(
        item["document"]
        for item in fixture["cases"]
        if item["caseId"] == "valid-static-fetch-request"
    )
    request = ProviderRequestEnvelope.model_validate(document)
    assert request.audience == "static_fetcher"
    assert request.method == "fetch"


def test_shared_contract_fixtures_parse_static_results_and_reject_wrong_method() -> None:
    fixture = json.loads(
        Path("contracts/providers/research/v1/conformance-fixtures.json").read_text()
    )
    for case_id in ("valid-fetch-result", "valid-health-result", "valid-limits-result"):
        document = next(item["document"] for item in fixture["cases"] if item["caseId"] == case_id)
        if document["audience"] == "static_fetcher":
            ProviderResultEnvelope.model_validate(document)
    wrong = next(
        item["document"]
        for item in fixture["cases"]
        if item["caseId"] == "valid-static-fetch-request"
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
    assert limits_result.limits.service == "static_fetcher"
    assert not any(
        token in repr(health.model_dump()) for token in ("url", "query", "excerpt", "exception")
    )


@pytest.mark.asyncio
async def test_static_fetch_uses_bounded_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(
        *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(
        policy.socket,
        "getaddrinfo",
        cast(Any, fake_resolver),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"bounded evidence",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await StaticFetcher(client=client).fetch(uuid4(), "https://example.com/evidence")
    assert result.outcome == "completed"
    assert result.text == "bounded evidence"
    assert result.bytes_transferred == len(b"bounded evidence")


@pytest.mark.asyncio
async def test_static_extraction_observer_only_runs_at_extraction_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def public_address(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(policy.socket, "getaddrinfo", cast(Any, public_address))
    observations: list[tuple[str, float]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/unsupported"):
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"not text",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"extract me",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        def record_observation(outcome: str, elapsed: float) -> None:
            observations.append((outcome, elapsed))

        fetcher = StaticFetcher(
            client=client,
            extraction_observer=record_observation,
        )
        await fetcher.fetch(uuid4(), "https://example.com/unsupported")
        result = await fetcher.fetch(uuid4(), "https://example.com/text")
    assert result.outcome == "completed"
    assert observations and observations[-1][0] == "success"
    assert observations[-1][1] >= 0
    assert len(observations) == 1


@pytest.mark.asyncio
async def test_static_full_extraction_path_propagates_trace_to_span_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(
        *_args: object, **_kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(policy.socket, "getaddrinfo", fake_getaddrinfo)
    output = io.StringIO()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"trace-bound evidence",
        )

    sink = ProductionExtractionSink(writer=output, background=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await StaticFetcher(client=client, extraction_observer=sink).fetch(
            uuid4(),
            "https://example.com/trace",
            trace_context=("c" * 32, "d" * 16),
        )
    assert result.outcome == "completed"
    sink.drain()
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all("trace" not in record for record in records if record["kind"] == "metric")
    span = next(record for record in records if record["kind"] == "span")
    assert span["trace"] == {
        "traceId": "c" * 32,
        "parentSpanId": "d" * 16,
        "spanId": span["trace"]["spanId"],
    }


def test_production_extraction_sink_serializes_exact_metadata_observations() -> None:
    output = io.StringIO()
    sink = ProductionExtractionSink(writer=output, capacity=8, background=False)
    sink("success", 12.5, trace_id="a" * 32, parent_span_id="b" * 16)
    assert sink.drain() == 3
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert {record["component"]["id"] for record in records} == {"aura.research.extraction"}
    assert {record["component"]["version"] for record in records} == {"1.0.0"}
    metrics = [record for record in records if record["kind"] == "metric"]
    assert {record["measurement"]["unit"] for record in metrics} == {"count", "milliseconds"}
    assert all("trace" not in record for record in metrics)
    span = next(record for record in records if record["kind"] == "span")
    assert span["trace"]["traceId"] == "a" * 32
    assert span["trace"]["parentSpanId"] == "b" * 16

    bounded = BoundedMetadataSink(writer=io.StringIO(), capacity=1, background=False)
    bounded.emit({"kind": "metric"})
    bounded.emit({"kind": "metric"})
    assert bounded.dropped_count == 1


def test_production_sink_retries_export_three_times_then_counts_failure() -> None:
    attempts = 0

    def fail(_: bytes) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("collector unavailable")

    sink = ProductionExtractionSink(exporter=fail, capacity=8, background=False)
    sink("success", 1.0)
    assert sink.drain() == 2
    assert attempts == 6
    assert sink.stats["export_failures"] == 2
    assert sink.stats["dropped"] == 2
    assert sink.stats["abandoned_batches"] == 0


def test_production_sink_tracks_rejections_and_shutdown_drain() -> None:
    output = io.StringIO()
    sink = ProductionExtractionSink(writer=output, capacity=8, background=False)
    sink("rejected", 1.0)
    sink.close()
    assert sink.stats["rejected"] == 1
    assert sink.stats["abandoned_batches"] == 0
    assert len(output.getvalue().splitlines()) == 3
