"""Core-side contract tests for governed research provider ports."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_core.providers.research.client import (
    ProviderClientError,
    ResearchTraceContext,
    SearchProvider,
    StaticFetchProvider,
)
from aura_core.providers.research.contracts import (
    FetchEvidenceResult,
    HealthResult,
    ProviderResultEnvelope,
    SearchRequest,
    Signature,
    StaticFetchRequest,
)
from aura_core.providers.research.signing import (
    EnvelopeSigner,
    EnvelopeVerificationError,
    EnvelopeVerifier,
)


def _write_test_only_egress_attestation_probe(tmp_path: Path) -> Path:
    """Use an explicit test stub; the bundled diagnostic is never readiness evidence."""
    probe_file = tmp_path / "test_only_egress_attestation_probe.py"
    probe_file.write_text(
        "import sys\n"
        "if sys.argv[1:] != ['--verify-enforcement']:\n"
        "    raise SystemExit(2)\n"
        "print('aura.research.egress.attested.v1')\n",
        encoding="utf-8",
    )
    return probe_file


def test_signed_envelope_is_audience_bound_and_replay_protected() -> None:
    signer = EnvelopeSigner.generate()
    verifier = EnvelopeVerifier(signer.public_key, audience="searxng")
    envelope = signer.sign(
        version="research-provider.v1",
        request_id=uuid4(),
        audience="searxng",
        method="search",
        payload={
            "kind": "search_request",
            "query": "bounded",
            "language": "en",
            "domainAllowlist": [],
            "limits": {"deadlineMs": 15_000, "maxCandidates": 20},
        },
    )

    assert (
        verifier.verify(envelope, method="search", version="research-provider.v1")["query"]
        == "bounded"
    )
    with pytest.raises(EnvelopeVerificationError, match="replayed"):
        verifier.verify(envelope, method="search", version="research-provider.v1")


def test_expired_or_wrong_audience_envelopes_fail_closed() -> None:
    signer = EnvelopeSigner.generate()
    verifier = EnvelopeVerifier(signer.public_key, audience="searxng")
    issued = datetime.now(UTC) - timedelta(seconds=10)
    expired = signer.sign(
        version="research-provider.v1",
        audience="searxng",
        method="search",
        payload={
            "kind": "search_request",
            "query": "bounded",
            "language": "en",
            "domainAllowlist": [],
            "limits": {"deadlineMs": 15_000, "maxCandidates": 20},
        },
        limits={},
        ttl_seconds=1,
        now=issued,
    )
    with pytest.raises(EnvelopeVerificationError, match="expired"):
        verifier.verify(expired, method="search", version="research-provider.v1")
    wrong = signer.sign(
        version="research-provider.v1",
        audience="searxng",
        method="search",
        payload={
            "kind": "search_request",
            "query": "bounded",
            "language": "en",
            "domainAllowlist": [],
            "limits": {"deadlineMs": 15_000, "maxCandidates": 20},
        },
        limits={},
    )
    with pytest.raises(EnvelopeVerificationError, match="wrong_audience"):
        EnvelopeVerifier(signer.public_key, audience="other-provider").verify(
            wrong, method="search", version="research-provider.v1"
        )


def test_shared_v1_fixtures_are_consumed_by_core_dtos() -> None:
    fixture_path = (
        Path(__file__).parents[3] / "contracts/providers/research/v1/conformance-fixtures.json"
    )
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        if case["expectation"] != "accept":
            continue
        document = case["document"]
        if case["definition"] == "ProviderRequestEnvelope":
            from aura_core.providers.research.signing import SignedEnvelope

            SignedEnvelope.model_validate(document)
        elif case["definition"] == "ProviderResultEnvelope":
            ProviderResultEnvelope.model_validate(document)


def test_core_dtos_reject_open_or_cross_bound_result_shapes() -> None:
    evidence: dict[str, Any] = {
        "kind": "fetch_result",
        "outcome": "success",
        "evidence": {
            "finalUrl": "https://example.org/research",
            "redirectCount": 0,
            "declaredMediaType": "text/html",
            "detectedMediaType": "text/html",
            "retrievedAt": "2026-10-28T10:00:03Z",
            "transferredBytes": 10,
            "extractedUtf8Bytes": 4,
            "contentSha256": "a" * 64,
            "extractionMethod": "trafilatura",
            "extractedText": "safe",
        },
    }
    with pytest.raises(ValueError):
        FetchEvidenceResult.model_validate({**evidence, "extra": "blocked"})
    with pytest.raises(ValueError):
        FetchEvidenceResult.model_validate(
            {
                **evidence,
                "evidence": {
                    **evidence["evidence"],
                    "declaredMediaType": "application/pdf",
                },
            }
        )
    with pytest.raises(ValueError):
        HealthResult.model_validate(
            {
                "kind": "health_result",
                "status": "degraded",
                "enabled": True,
                "accepting": False,
                "inFlight": 0,
                "contractVersion": "research-provider.v1",
                "degradation": "arbitrary",
            }
        )
    envelope = {
        "schemaVersion": "research-provider.v1",
        "envelopeType": "result",
        "responseId": str(uuid4()),
        "requestId": str(uuid4()),
        "audience": "searxng",
        "method": "search",
        "producedAt": "2026-10-28T10:00:03Z",
        "result": evidence,
    }
    with pytest.raises(ValueError):
        ProviderResultEnvelope.model_validate(envelope)
    with pytest.raises(ValueError):
        Signature.model_validate({"algorithm": "Ed25519", "keyId": "bad key", "value": "0" * 86})
    with pytest.raises(ValueError):
        Signature.model_validate({"algorithm": "Ed25519", "keyId": "core", "value": "+" * 86})


@pytest.mark.asyncio
async def test_search_client_sends_signed_closed_request_and_parses_bounded_result() -> None:
    signer = EnvelopeSigner.generate()
    seen: list[httpx.Request] = []
    request_id = uuid4()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["traceparent"] == f"00-{'a' * 32}-{'b' * 16}-01"
        body = request.read()
        assert b"query" in body
        assert b"bounded" in body
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
                    "candidates": [
                        {
                            "rank": 1,
                            "url": "https://example.com/report",
                            "label": "Report",
                        }
                    ],
                },
            },
        )

    request = SearchRequest(
        request_id=request_id,
        query="bounded",
        limits={"maxCandidates": 1, "deadlineMs": 15_000},  # type: ignore[reportArgumentType]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await SearchProvider(
            "http://provider.invalid", signer, enabled=True, http_client=http
        ).search(request, trace_context=ResearchTraceContext("a" * 32, "b" * 16))

    assert result.outcome == "success"
    assert result.candidates[0].url == "https://example.com/report"
    assert seen[0].method == "POST"


@pytest.mark.asyncio
async def test_disabled_fetcher_returns_truthful_bounded_failure_without_network() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderClientError, match="provider_disabled"):
            await StaticFetchProvider(
                "http://provider.invalid",
                EnvelopeSigner.generate(),
                enabled=False,
                http_client=http,
            ).fetch(
                StaticFetchRequest.model_validate(
                    {
                        "request_id": uuid4(),
                        "targetUrl": "https://example.com",
                        "admissionKind": "owner_seed",
                    }
                )
            )

    assert called is False


@pytest.mark.asyncio
async def test_static_client_round_trips_signed_health_through_provider_asgi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the real Core port against the provider's platform route."""

    signer = EnvelopeSigner.generate()
    key_file = tmp_path / "provider-public.key"
    # Providers consume the exact unpadded base64url public-key wire form.
    key_file.write_text(signer.public_key, encoding="ascii")
    policy_file = tmp_path / "egress-policy.yaml"
    policy_file.write_text(
        "schemaVersion: aura.research.egress.v1\n"
        "mode: operator-provisioned\n"
        "dns:\n  mode: controlled\n"
        "  requireAllAnswersPublic: true\n"
        "  revalidateBeforeConnect: true\n"
        "  revalidateRedirects: true\n"
        "  denyPrivateLoopbackLinkLocalMetadataReserved: true\n"
        "destinations:\n  schemes: [https]\n  ports: [443]\n"
        "  allowUserinfo: false\n",
        encoding="utf-8",
    )
    probe_file = _write_test_only_egress_attestation_probe(tmp_path)
    monkeypatch.setenv("AURA_RESEARCH_STATIC_ENABLED", "true")
    monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH", str(probe_file))
    monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)

    service_src = str(Path(__file__).parents[2] / "aura-research-static-fetcher" / "src")
    sys.path.insert(0, service_src)
    prefix = "aura_research_static_fetcher"
    previous = {name: module for name, module in sys.modules.items() if name.startswith(prefix)}
    try:
        # The provider module reads its capability switch at import time.  Do
        # not inherit a module initialized by another test or test order.
        for name in list(sys.modules):
            if name.startswith(prefix):
                sys.modules.pop(name, None)
        app_module = importlib.import_module(f"{prefix}.app")
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as http:
            health = await StaticFetchProvider(
                "http://provider", signer, enabled=True, http_client=http
            ).health()
        assert health.state == "ready"
        assert health.capacity_available is True
    finally:
        sys.path.remove(service_src)
        for name in list(sys.modules):
            if name.startswith(prefix):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


@pytest.mark.asyncio
async def test_static_client_preserves_typed_inadequate_extraction_from_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signer = EnvelopeSigner.generate()
    key_file = tmp_path / "provider-public.key"
    key_file.write_text(signer.public_key, encoding="ascii")
    policy_file = tmp_path / "egress-policy.yaml"
    policy_file.write_text(
        "schemaVersion: aura.research.egress.v1\n"
        "mode: operator-provisioned\n"
        "dns:\n  mode: controlled\n"
        "  requireAllAnswersPublic: true\n"
        "  revalidateBeforeConnect: true\n"
        "  revalidateRedirects: true\n"
        "  denyPrivateLoopbackLinkLocalMetadataReserved: true\n"
        "destinations:\n  schemes: [https]\n  ports: [443]\n"
        "  allowUserinfo: false\n",
        encoding="utf-8",
    )
    probe_file = _write_test_only_egress_attestation_probe(tmp_path)
    monkeypatch.setenv("AURA_RESEARCH_STATIC_ENABLED", "true")
    monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH", str(probe_file))

    service_src = str(Path(__file__).parents[2] / "aura-research-static-fetcher" / "src")
    sys.path.insert(0, service_src)
    prefix = "aura_research_static_fetcher"
    previous = {name: module for name, module in sys.modules.items() if name.startswith(prefix)}
    try:
        for name in list(sys.modules):
            if name.startswith(prefix):
                sys.modules.pop(name, None)
        app_module = importlib.import_module(f"{prefix}.app")
        captured_trace: dict[str, object] = {}

        async def inadequate(*_args: object, **_kwargs: object) -> Any:
            captured_trace["context"] = _kwargs.get("trace_context")
            return app_module.EvidenceResult(
                request_id=uuid4(),
                outcome="inadequate_extraction",
                final_url="https://example.com/empty",
                declared_media_type="text/html",
                detected_media_type="text/html",
                redirects=0,
                bytes_transferred=128,
                error_class="empty_text",
            )

        monkeypatch.setattr(app_module.fetcher, "fetch", inadequate)
        request_id = uuid4()
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as http:
            result = await StaticFetchProvider(
                "http://provider", signer, enabled=True, http_client=http
            ).fetch(
                StaticFetchRequest.model_validate(
                    {
                        "request_id": request_id,
                        "targetUrl": "https://example.com/empty",
                        "admissionKind": "owner_seed",
                    }
                ),
                trace_context=ResearchTraceContext("e" * 32, "f" * 16),
            )
        assert result.outcome == "inadequate_extraction"
        assert result.final_url == "https://example.com/empty"
        assert captured_trace["context"] == ("e" * 32, "f" * 16)
    finally:
        sys.path.remove(service_src)
        for name in list(sys.modules):
            if name.startswith(prefix):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


@pytest.mark.asyncio
async def test_search_client_round_trips_signed_health_and_limits_through_integrated_searxng(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The single SearXNG deployable owns search and its signed controls."""

    signer = EnvelopeSigner.generate()
    key_file = tmp_path / "provider-public.key"
    key_file.write_text(signer.public_key, encoding="ascii")
    policy_file = tmp_path / "egress-policy.yaml"
    policy_file.write_text(
        "schemaVersion: aura.research.egress.v1\n"
        "mode: operator-provisioned\n"
        "dns:\n  mode: controlled\n"
        "  requireAllAnswersPublic: true\n"
        "  revalidateBeforeConnect: true\n"
        "  revalidateRedirects: true\n"
        "  denyPrivateLoopbackLinkLocalMetadataReserved: true\n"
        "destinations:\n  schemes: [https]\n  ports: [443]\n"
        "  allowUserinfo: false\n",
        encoding="utf-8",
    )
    probe_file = _write_test_only_egress_attestation_probe(tmp_path)
    monkeypatch.setenv("AURA_RESEARCH_SEARCH_ENABLED", "true")
    monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH", str(probe_file))

    service_src = str(Path(__file__).parents[3] / "deploy" / "research")
    sys.path.insert(0, service_src)
    module_name = "searxng_adapter"
    previous = {
        name: module for name, module in sys.modules.items() if name == module_name
    }
    try:
        sys.modules.pop(module_name, None)
        adapter = importlib.import_module(module_name)

        async def backend_ready() -> bool:
            return True

        cast(Any, adapter)._backend_ready = backend_ready
        transport = httpx.ASGITransport(app=adapter.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as http:
            provider = SearchProvider("http://provider", signer, enabled=True, http_client=http)
            health = await provider.health()
            limits = await provider.limits()
        assert health.state == "ready"
        assert health.capacity_available is True
        assert limits.limits.service == "searxng"
    finally:
        sys.path.remove(service_src)
        sys.modules.pop(module_name, None)
        sys.modules.update(previous)
