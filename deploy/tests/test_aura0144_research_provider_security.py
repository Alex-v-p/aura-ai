"""Provider sandbox, network, and credential-isolation policy checks."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import yaml

ROOT = Path(__file__).parents[2]
PROVIDER_NAMES = (
    "aura-research-searxng",
    "aura-research-static-fetcher",
    "aura-research-browser-fetcher",
)
PRIVATE_NETWORKS = {
    "aura-research-searxng": "aura-research-searxng-private",
    "aura-research-static-fetcher": "aura-research-static-private",
    "aura-research-browser-fetcher": "aura-research-browser-private",
}
STATIC_SRC = ROOT / "services/aura-research-static-fetcher/src"
BROWSER_SRC = ROOT / "services/aura-research-browser-fetcher/src"
CORE_SRC = ROOT / "services/aura-core/src"


def _services() -> dict[str, dict[str, Any]]:
    value = yaml.safe_load(
        (ROOT / "deploy/compose/research-providers.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    services = cast(dict[Any, Any], value["services"])
    return {str(key): cast(dict[str, Any], item) for key, item in services.items()}


def test_provider_limits_match_adr_0018() -> None:
    services = _services()
    expected = {
        "aura-research-searxng": ("1g", 128, 2.0, "128m"),
        "aura-research-static-fetcher": ("1g", 64, 2.0, "64m"),
        "aura-research-browser-fetcher": ("2g", 256, 4.0, "256m"),
    }
    for name, (memory, pids, cpus, tmp) in expected.items():
        service = services[name]
        assert service["mem_limit"] == memory
        assert service["pids_limit"] == pids
        assert service["cpus"] == cpus
        assert any(tmp in mount for mount in service["tmpfs"])


def test_seccomp_profile_is_restrictive_and_network_is_not_host_network() -> None:
    profile = json.loads(
        (ROOT / "deploy/research/provider-seccomp.json").read_text(encoding="utf-8")
    )
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert "SCMP_ACT_ALLOW" in repr(profile)
    for service in _services().values():
        assert "host" not in service.get("network_mode", "")
        assert "privileged" not in service
        assert "devices" not in service
        assert "pid:host" not in repr(service)


def test_seccomp_allowlist_excludes_namespace_mount_and_kernel_escape_syscalls() -> None:
    profile = json.loads(
        (ROOT / "deploy/research/provider-seccomp.json").read_text(encoding="utf-8")
    )
    allowed = {
        name
        for group in profile["syscalls"]
        if group.get("action") == "SCMP_ACT_ALLOW"
        for name in group.get("names", [])
    }
    assert not allowed.intersection(
        {
            "mount",
            "umount",
            "umount2",
            "pivot_root",
            "setns",
            "unshare",
            "ptrace",
            "reboot",
            "kexec_load",
            "open_by_handle_at",
        }
    )


def test_images_are_immutable_digest_references_and_fixture_has_no_secrets() -> None:
    for service in _services().values():
        image = str(service.get("image", ""))
        if image:
            assert "@sha256:" in image
    env = (ROOT / "deploy/compose/research-providers.env.example").read_text(encoding="utf-8")
    assert "BEGIN PRIVATE KEY" not in env
    assert "AURA_PROVIDER_ALLOW_UNSIGNED=true" not in env


def test_provider_network_and_mount_topology_has_no_escape_hatches() -> None:
    services = _services()
    for _name, service in services.items():
        assert service.get("network_mode") != "host"
        assert service.get("privileged", False) is not True
        assert "devices" not in service
        assert "pid:host" not in repr(service)
        networks = set(cast(dict[str, Any], service.get("networks", {})))
        if _name in PRIVATE_NETWORKS:
            assert PRIVATE_NETWORKS[_name] in networks
            assert not networks.intersection(
                set(PRIVATE_NETWORKS.values()) - {PRIVATE_NETWORKS[_name]}
            )
        assert "aura-core-db" not in networks
        assert "aura-core-bus" not in networks
        assert "aura-core-cache" not in networks
        assert "aura-object-storage" not in networks
        assert "observatory-ingest" not in networks
        for mount in service.get("volumes", []):
            rendered = repr(mount)
            assert "docker.sock" not in rendered
            assert "/var/run" not in rendered
            assert "/run/secrets" not in rendered
            assert "/etc/passwd" not in rendered
            assert "/etc/shadow" not in rendered


def test_all_provider_containers_use_hardened_runtime_defaults() -> None:
    services = _services()
    for name in PROVIDER_NAMES:
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        security_options = set(service["security_opt"])
        assert "no-new-privileges:true" in security_options
        assert any(option.startswith("seccomp=") for option in security_options)
        assert service["pids_limit"] > 0
        assert service["mem_limit"]
        assert service["cpus"] > 0
        assert all(
            "noexec" in mount and "nosuid" in mount and "nodev" in mount
            for mount in service["tmpfs"]
        )


def test_provider_images_drop_root_and_do_not_bake_in_credentials() -> None:
    for relative, uid in (
        ("deploy/docker/research-static-fetcher.Dockerfile", "10001:10001"),
        ("deploy/docker/research-browser-fetcher.Dockerfile", "10002:10002"),
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert f"USER {uid}" in source
        assert "PRIVATE KEY" not in source
        assert "AURA_SESSION" not in source
        assert "DATABASE_URL" not in source
        assert "docker.sock" not in source


def test_stock_searxng_is_loopback_only_and_signed_adapter_is_the_network_listener() -> None:
    settings = (ROOT / "deploy/research/searxng-settings.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/research/searxng_entrypoint.sh").read_text(encoding="utf-8")
    assert "bind_address: 127.0.0.1" in settings
    assert "port: 8081" in settings
    assert "SEARXNG_PORT=8081" in entrypoint
    assert "--host 0.0.0.0 --port 8080" in entrypoint
    assert "--no-access-log" in entrypoint
    assert "bind_address: 0.0.0.0" not in settings
    compose = (ROOT / "deploy/compose/research-providers.yaml").read_text(encoding="utf-8")
    assert "SEARXNG_SECRET_KEY:" not in compose
    assert "SEARXNG_SECRET_KEY_FILE: /run/provider/searxng-secret" in compose


def test_provider_images_use_hash_locked_provider_only_requirements() -> None:
    for relative, requirements in (
        (
            "deploy/docker/research-static-fetcher.Dockerfile",
            "provider-requirements.txt",
        ),
        (
            "deploy/docker/research-browser-fetcher.Dockerfile",
            "browser-provider-requirements.txt",
        ),
        (
            "deploy/docker/research-searxng.Dockerfile",
            "browser-provider-requirements.txt",
        ),
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "--require-hashes" in source
        assert requirements in source
    for relative in (
        "deploy/research/provider-requirements.txt",
        "deploy/research/browser-provider-requirements.txt",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "--hash=sha256:" in source
        assert "==" in source


def test_search_adapter_posts_query_body_without_query_string_or_access_logging() -> None:
    source = (ROOT / "deploy/research/searxng_adapter.py").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/research/searxng_entrypoint.sh").read_text(encoding="utf-8")
    assert "client.post(" in source
    assert "params={" not in source
    assert '"q": envelope.payload.query' in source
    assert "--no-access-log" in entrypoint


def test_browser_backend_streams_and_bounds_before_route_fulfilment() -> None:
    source = (
        ROOT
        / "services/aura-research-browser-fetcher/src/aura_research_browser_fetcher/fetcher.py"
    ).read_text(encoding="utf-8")
    assert "aiter_raw()" in source
    assert "_stream_bounded_body" in source
    assert "response.content" not in source


def test_signed_transport_canonicalization_preserves_unicode_and_no_slash_urls() -> None:
    sys.path.insert(0, str(CORE_SRC))
    sys.path.insert(0, str(STATIC_SRC))
    sys.path.insert(0, str(BROWSER_SRC))
    sys.path.insert(0, str(ROOT / "deploy/research"))
    try:
        from aura_core.providers.research.signing import EnvelopeSigner
        from aura_research_browser_fetcher import app as browser_app
        from aura_research_static_fetcher import app as static_app

        signer = EnvelopeSigner.generate()
        cases = (
            (
                static_app,
                "static_fetcher",
                {
                    "kind": "static_fetch_request",
                    "targetUrl": "https://example.org",
                    "admissionKind": "owner_seed",
                    "limits": {
                        "deadlineMs": 1_000,
                        "maxRedirects": 1,
                        "maxTransferredBytes": 1_024,
                        "maxDecompressedBytes": 1_024,
                        "maxExtractedUtf8Bytes": 1_024,
                    },
                },
            ),
            (
                browser_app,
                "browser_fetcher",
                {
                    "kind": "browser_fetch_request",
                    "targetUrl": "https://example.org",
                    "admissionKind": "owner_seed",
                    "justification": "javascript_required",
                    "limits": {
                        "deadlineMs": 1_000,
                        "maxRedirects": 1,
                        "maxResponseBytes": 1_024,
                        "maxExtractedUtf8Bytes": 1_024,
                    },
                },
            ),
        )
        from aura_core.providers.research.signing import _canonical as core_canonical

        for module, audience, payload in cases:
            signed = signer.sign(
                version="research-provider.v1",
                audience=audience,
                method="fetch",
                payload=payload,
            )
            parsed = module.ProviderRequestEnvelope.model_validate(
                signed.model_dump(mode="json", by_alias=True)
            )
            assert parsed.payload.targetUrl == "https://example.org"
            assert module._canonical(parsed) == core_canonical(signed.unsigned())

        import searxng_adapter

        search = signer.sign(
            version="research-provider.v1",
            audience="searxng",
            method="search",
            payload={
                "kind": "search_request",
                "query": "café 日本語",
                "language": "en",
                "domainAllowlist": [],
                "limits": {"deadlineMs": 1_000, "maxCandidates": 5},
            },
        )
        parsed_search = searxng_adapter.SearchEnvelope.model_validate(
            search.model_dump(mode="json", by_alias=True)
        )
        assert parsed_search.payload.query == "café 日本語"
        assert searxng_adapter._canonical(parsed_search) == core_canonical(search.unsigned())
    finally:
        for path in (
            str(ROOT / "deploy/research"),
            str(BROWSER_SRC),
            str(STATIC_SRC),
            str(CORE_SRC),
        ):
            if path in sys.path:
                sys.path.remove(path)


def test_health_and_environment_surfaces_are_content_free() -> None:
    forbidden = (
        "AURA_SESSION",
        "AURA_OWNER",
        "DATABASE_URL",
        "NATS_URL",
        "VALKEY_URL",
        "GARAGE",
        "OIDC",
        "COOKIE",
        "AUTHORIZATION",
        "PRIVATE_KEY",
    )
    for name in PROVIDER_NAMES:
        service = _services()[name]
        environment = cast(dict[str, Any], service.get("environment", {}))
        assert not any(token in repr(environment).upper() for token in forbidden)
        healthcheck = repr(service.get("healthcheck", {})).lower()
        assert "query" not in healthcheck
        assert "target" not in healthcheck
        assert "content" not in healthcheck

    for relative in (
        "services/aura-research-static-fetcher/src/aura_research_static_fetcher/app.py",
        "services/aura-research-browser-fetcher/src/aura_research_browser_fetcher/app.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "logging" not in source
        assert "print(" not in source
        assert "str(exc)" not in source


def test_provider_readiness_requires_an_executable_enforcement_probe() -> None:
    for relative in (
        "services/aura-research-static-fetcher/src/aura_research_static_fetcher/app.py",
        "services/aura-research-browser-fetcher/src/aura_research_browser_fetcher/app.py",
        "deploy/research/searxng_adapter.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH" in source
        assert "--verify-enforcement" in source
        assert "aura.research.egress.attested.v1" in source
        assert "subprocess.run" in source
    bundled = (ROOT / "deploy/research/egress_probe.py").read_text(encoding="utf-8")
    assert "verify_enforcement" not in bundled
    assert "aura.research.egress.attested.v1" not in bundled
    env = (ROOT / "deploy/compose/research-providers.env.example").read_text(encoding="utf-8")
    assert "AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH=" in env


def test_browser_runtime_forbids_downloads_persistence_and_privileged_browser_hooks() -> None:
    source = (
        ROOT / "services/aura-research-browser-fetcher/src/aura_research_browser_fetcher/fetcher.py"
    ).read_text(encoding="utf-8")
    assert "accept_downloads=False" in source
    assert 'service_workers="block"' in source
    assert "permissions=[]" in source
    assert "chromium_sandbox=True" in source
    assert 'resource_type in {"image", "font", "media", "websocket"}' in source
    for forbidden in (
        "storage_state",
        "user_data_dir",
        "connect_over_cdp",
        "executable_path",
        "downloads_path",
        "clipboard",
        "geolocation",
    ):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_browser_route_blocks_http_subresources_and_network_schemes() -> None:
    sys.path.insert(0, str(BROWSER_SRC))
    try:
        from aura_research_browser_fetcher.fetcher import BrowserFetcher

        class Request:
            def __init__(self, url: str, resource_type: str = "document") -> None:
                self.url = url
                self.resource_type = resource_type

        class Route:
            def __init__(self, request: Request) -> None:
                self.request = request
                self.aborted = False
                self.continued = False

            async def abort(self) -> None:
                self.aborted = True

            async def continue_(self) -> None:
                self.continued = True

        for url in ("http://private.example/", "file:///etc/passwd", "data:text/plain,secret"):
            route = Route(Request(url))
            await BrowserFetcher._route(route)
            assert route.aborted is True
            assert route.continued is False
    finally:
        sys.path.remove(str(BROWSER_SRC))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "source_path", "service"),
    [
        ("aura_research_static_fetcher.app", STATIC_SRC, "static_fetcher"),
        ("aura_research_browser_fetcher.app", BROWSER_SRC, "browser_fetcher"),
    ],
)
async def test_provider_health_routes_return_exact_content_free_v1_metadata(
    module_name: str,
    source_path: Path,
    service: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.path.insert(0, str(source_path))
    try:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "_enabled", False)
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as client:
            for path in ("/health/live", "/health/ready"):
                response = await client.get(path)
                assert response.status_code in {200, 503}
                payload = response.json()
                assert set(payload) == {
                    "kind",
                    "status",
                    "enabled",
                    "accepting",
                    "inFlight",
                    "contractVersion",
                    "degradation",
                }
                assert payload["kind"] == "health_result"
                assert payload["enabled"] is False
                assert payload["accepting"] is False
                assert payload["inFlight"] == 0
                assert payload["contractVersion"] == "research-provider.v1"
                assert payload["degradation"] == "disabled"
                assert service not in repr(payload)
                assert "targetUrl" not in repr(payload)
                assert "extractedText" not in repr(payload)
    finally:
        sys.path.remove(str(source_path))


@pytest.mark.parametrize(
    ("module_name", "source_path"),
    [
        ("aura_research_static_fetcher.app", STATIC_SRC),
        ("aura_research_browser_fetcher.app", BROWSER_SRC),
    ],
)
def test_fetcher_advertises_all_closed_control_routes_and_error_envelope(
    module_name: str, source_path: Path
) -> None:
    sys.path.insert(0, str(source_path))
    try:
        module = importlib.import_module(module_name)
        routes = {
            (method, route.path)
            for route in module.app.routes
            for method in getattr(route, "methods", set())
        }
        for path in (
            "/internal/v1/fetch",
            "/internal/v1/cancel",
            "/internal/v1/limits",
            "/internal/v1/health",
        ):
            assert ("POST", path) in routes
        assert hasattr(module, "ProviderErrorEnvelope")
    finally:
        sys.path.remove(str(source_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "source_path"),
    [
        ("aura_research_static_fetcher.app", STATIC_SRC),
        ("aura_research_browser_fetcher.app", BROWSER_SRC),
    ],
)
async def test_limits_endpoint_is_exact_and_content_free(
    module_name: str, source_path: Path
) -> None:
    sys.path.insert(0, str(source_path))
    try:
        module = importlib.import_module(module_name)
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as client:
            unsigned_get = await client.get("/internal/v1/limits")
            unsigned_post = await client.post("/internal/v1/limits", json={})
        assert unsigned_get.status_code == 405
        assert unsigned_post.status_code == 400
        payload = unsigned_post.json()
        assert payload == {"code": "contract_invalid", "retryable": False}
    finally:
        sys.path.remove(str(source_path))


@pytest.mark.asyncio
async def test_core_health_maps_malformed_provider_health_to_unavailable() -> None:
    sys.path.insert(0, str(CORE_SRC))
    try:
        from aura_core.providers.research.client import StaticFetchProvider
        from aura_core.providers.research.signing import EnvelopeSigner

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"service": "static_fetcher", "state": "ready"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            health = await StaticFetchProvider(
                "http://provider",
                EnvelopeSigner.generate(),
                enabled=True,
                http_client=client,
            ).health()
        assert health.state == "unavailable"
        assert health.capacity_available is False
    finally:
        sys.path.remove(str(CORE_SRC))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "source_path"),
    [
        ("aura_research_static_fetcher.app", STATIC_SRC),
        ("aura_research_browser_fetcher.app", BROWSER_SRC),
    ],
)
async def test_enabled_provider_with_missing_verification_key_is_not_ready(
    module_name: str,
    source_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.path.insert(0, str(source_path))
    try:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "_enabled", True)
        monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", "/missing/provider-key")
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)
        transport = httpx.ASGITransport(app=module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as client:
            response = await client.get("/health/ready")
        payload = response.json()
        assert response.status_code != 200 or payload.get("status") != "ready"
        assert payload.get("degradation") in {"configuration_invalid", "dependency_unavailable"}
    finally:
        sys.path.remove(str(source_path))


def test_provider_accepts_core_urlsafe_public_keys_with_dash_or_underscore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(CORE_SRC))
    sys.path.insert(0, str(STATIC_SRC))
    try:
        from aura_core.providers.research.signing import EnvelopeSigner

        module = importlib.import_module("aura_research_static_fetcher.app")
        signer = EnvelopeSigner.generate()
        while "-" not in signer.public_key and "_" not in signer.public_key:
            signer = EnvelopeSigner.generate()
        signed = signer.sign(
            version="research-provider.v1",
            audience="static_fetcher",
            method="fetch",
            request_id=uuid4(),
            payload={
                "kind": "static_fetch_request",
                "targetUrl": "https://example.org/",
                "admissionKind": "owner_seed",
                "limits": {
                    "deadlineMs": 1_000,
                    "maxRedirects": 1,
                    "maxTransferredBytes": 1_024,
                    "maxDecompressedBytes": 1_024,
                    "maxExtractedUtf8Bytes": 1_024,
                },
            },
        )
        envelope = module.ProviderRequestEnvelope.model_validate(
            signed.model_dump(mode="json", by_alias=True)
        )
        key_file = tmp_path / "provider-public-key"
        key_file.write_text(signer.public_key, encoding="ascii")
        module._seen.clear()
        monkeypatch.setattr(module, "_enabled", True)
        monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)
        module._verify(envelope)
    finally:
        sys.path.remove(str(STATIC_SRC))
        sys.path.remove(str(CORE_SRC))


@pytest.mark.asyncio
async def test_core_static_provider_completes_real_post_asgi_journey_with_correlation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(CORE_SRC))
    sys.path.insert(0, str(STATIC_SRC))
    try:
        from aura_core.providers.research.client import StaticFetchProvider
        from aura_core.providers.research.contracts import StaticFetchRequest
        from aura_core.providers.research.signing import EnvelopeSigner
        from aura_research_static_fetcher import app as provider_module
        from aura_research_static_fetcher.models import EvidenceResult

        async def fake_fetch(
            request_id: object,
            url: str,
            limits: dict[str, int],
            *,
            trace_context: object = None,
        ) -> EvidenceResult:
            del trace_context
            del url, limits
            return EvidenceResult(
                request_id=request_id,
                outcome="completed",
                final_url="https://example.org/",
                declared_media_type="text/plain",
                detected_media_type="text/plain",
                extraction_method="bounded_text",
                text="bounded evidence",
                content_sha256="a" * 64,
                bytes_transferred=17,
            )

        signer = EnvelopeSigner.generate()
        key_file = tmp_path / "provider-public-key"
        key_file.write_text(signer.public_key, encoding="ascii")
        monkeypatch.setattr(provider_module, "_enabled", True)
        monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)
        monkeypatch.setattr(provider_module.fetcher, "fetch", fake_fetch)
        request = StaticFetchRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "targetUrl": "https://example.org/",
                "admissionKind": "owner_seed",
                "limits": {
                    "deadlineMs": 1_000,
                    "maxRedirects": 1,
                    "maxTransferredBytes": 1_024,
                    "maxDecompressedBytes": 1_024,
                    "maxExtractedUtf8Bytes": 1_024,
                },
            }
        )
        transport = httpx.ASGITransport(app=provider_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://provider") as client:
            result = await StaticFetchProvider(
                "http://provider",
                signer,
                enabled=True,
                http_client=client,
            ).fetch(request)
        assert result.outcome == "success"
        assert result.evidence.final_url == "https://example.org/"
    finally:
        sys.path.remove(str(STATIC_SRC))
        sys.path.remove(str(CORE_SRC))


@pytest.mark.asyncio
async def test_core_rejects_provider_response_with_wrong_request_correlation() -> None:
    sys.path.insert(0, str(CORE_SRC))
    try:
        from aura_core.providers.research.client import ProviderClientError, StaticFetchProvider
        from aura_core.providers.research.contracts import StaticFetchRequest
        from aura_core.providers.research.signing import EnvelopeSigner

        fixture = json.loads(
            (ROOT / "contracts/providers/research/v1/conformance-fixtures.json").read_text(
                encoding="utf-8"
            )
        )
        document = next(
            item["document"]
            for item in fixture["cases"]
            if item["caseId"] == "valid-fetch-result"
        )
        request = StaticFetchRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "targetUrl": "https://example.org/",
                "admissionKind": "owner_seed",
            }
        )
        document["requestId"] = str(uuid4())

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=document)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderClientError):
                await StaticFetchProvider(
                    "http://provider",
                    EnvelopeSigner.generate(),
                    enabled=True,
                    http_client=client,
                ).fetch(request)
    finally:
        sys.path.remove(str(CORE_SRC))


@pytest.mark.parametrize(
    ("module_name", "source_path", "fixture_case"),
    [
        (
            "aura_research_static_fetcher.app",
            STATIC_SRC,
            "valid-static-fetch-request",
        ),
        (
            "aura_research_browser_fetcher.app",
            BROWSER_SRC,
            "valid-browser-fetch-request",
        ),
    ],
)
def test_provider_rejects_expired_and_replayed_envelopes(
    module_name: str,
    source_path: Path,
    fixture_case: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(source_path))
    sys.path.insert(0, str(CORE_SRC))
    try:
        module = importlib.import_module(module_name)
        from aura_core.providers.research.signing import EnvelopeSigner
        fixture = json.loads(
            (ROOT / "contracts/providers/research/v1/conformance-fixtures.json").read_text(
                encoding="utf-8"
            )
        )
        document = next(
            item["document"] for item in fixture["cases"] if item["caseId"] == fixture_case
        )
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        signer = EnvelopeSigner.generate()
        signed = signer.sign(
            version="research-provider.v1",
            request_id=UUID(document["requestId"]),
            replay_id=UUID(document["replayId"]),
            audience=document["audience"],
            method=document["method"],
            payload=document["payload"],
            ttl_seconds=20,
            now=now,
        )
        envelope = module.ProviderRequestEnvelope.model_validate(
            signed.model_dump(mode="json", by_alias=True)
        )
        key_file = tmp_path / f"{fixture_case}-provider-public-key"
        key_file.write_text(signer.public_key, encoding="ascii")
        module._seen.clear()
        monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)

        module._verify(envelope)
        with pytest.raises(module.HTTPException) as replay:
            module._verify(envelope)
        assert replay.value.status_code == 409

        expired = envelope.model_copy(
            update={
                "replayId": envelope.requestId,
                "expiresAt": now - timedelta(seconds=1),
            }
        )
        with pytest.raises(module.HTTPException) as expiry:
            module._verify(expired)
        assert expiry.value.status_code == 401
    finally:
        sys.path.remove(str(source_path))
        sys.path.remove(str(CORE_SRC))


def test_provider_requires_signature_verification_when_unsigned_mode_is_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.path.insert(0, str(STATIC_SRC))
    try:
        module = importlib.import_module("aura_research_static_fetcher.app")
        fixture = json.loads(
            (ROOT / "contracts/providers/research/v1/conformance-fixtures.json").read_text(
                encoding="utf-8"
            )
        )
        document = next(
            item["document"]
            for item in fixture["cases"]
            if item["caseId"] == "valid-static-fetch-request"
        )
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        document["issuedAt"] = now.isoformat()
        document["expiresAt"] = (now + timedelta(seconds=20)).isoformat()
        envelope = module.ProviderRequestEnvelope.model_validate(document)
        module._seen.clear()
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)
        monkeypatch.delenv("AURA_PROVIDER_PUBLIC_KEY_FILE", raising=False)
        with pytest.raises(module.HTTPException) as not_configured:
            module._verify(envelope)
        assert not_configured.value.status_code == 503
    finally:
        sys.path.remove(str(STATIC_SRC))


def test_provider_rejects_malformed_public_key_or_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sys.path.insert(0, str(STATIC_SRC))
    try:
        module = importlib.import_module("aura_research_static_fetcher.app")
        fixture = json.loads(
            (ROOT / "contracts/providers/research/v1/conformance-fixtures.json").read_text(
                encoding="utf-8"
            )
        )
        document = next(
            item["document"]
            for item in fixture["cases"]
            if item["caseId"] == "valid-static-fetch-request"
        )
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        document["issuedAt"] = now.isoformat()
        document["expiresAt"] = (now + timedelta(seconds=20)).isoformat()
        envelope = module.ProviderRequestEnvelope.model_validate(document)
        key_file = tmp_path / "public-key"
        key_file.write_bytes(b"not-an-ed25519-key")
        module._seen.clear()
        monkeypatch.setenv("AURA_PROVIDER_PUBLIC_KEY_FILE", str(key_file))
        monkeypatch.delenv("AURA_PROVIDER_ALLOW_UNSIGNED", raising=False)
        with pytest.raises(module.HTTPException) as malformed:
            module._verify(envelope)
        assert malformed.value.status_code == 401
    finally:
        sys.path.remove(str(STATIC_SRC))
