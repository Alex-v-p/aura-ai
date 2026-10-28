"""Application-level provider hard ceilings and destination policy tests."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import sys
import types
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).parents[2]
CORE_SRC = ROOT / "services/aura-core/src"
sys.path.insert(0, str(ROOT / "services/aura-research-static-fetcher/src"))
sys.path.insert(0, str(ROOT / "services/aura-research-browser-fetcher/src"))
from aura_research_static_fetcher.fetcher import (  # noqa: E402
    DEADLINE_SECONDS,
    MAX_BYTES,
    MAX_REDIRECTS,
    MAX_TEXT_BYTES,
    StaticFetcher,
    _bounded_utf8,
    _sniff,
)
from aura_research_static_fetcher.policy import DestinationBlocked  # noqa: E402


def test_static_limits_are_exact_adr_ceilings() -> None:
    assert DEADLINE_SECONDS == 20
    assert MAX_BYTES == 8 * 1024 * 1024
    assert MAX_TEXT_BYTES == 256 * 1024
    assert MAX_REDIRECTS == 5


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://example.com:444/",
        "https://user:pass@example.com/",
        "https://127.0.0.1/",
        "https://[::1]/",
    ],
)
def test_destination_policy_rejects_non_public_or_non_https_targets(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura_research_static_fetcher import policy

    monkeypatch.setattr(
        policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    if "127.0.0.1" in url or "::1" in url:
        monkeypatch.setattr(
            policy.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
        )
    with pytest.raises(DestinationBlocked):
        policy.validate_public_https_url(url)


def test_policy_rejects_mixed_public_private_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    from aura_research_static_fetcher import policy

    monkeypatch.setattr(
        policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(DestinationBlocked):
        policy.validate_public_https_url("https://example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://example.com:8443/",
        "http://example.com/",
    ],
)
def test_browser_destination_policy_matches_static_private_target_rejections(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura_research_browser_fetcher import policy

    monkeypatch.setattr(
        policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(policy.DestinationBlocked):
        policy.validate_public_https_url(url)


def test_browser_limits_match_adr_0018() -> None:
    from aura_research_browser_fetcher import fetcher as browser_fetcher

    assert browser_fetcher.DEADLINE_SECONDS == 45
    assert browser_fetcher.MAX_BYTES == 8 * 1024 * 1024
    assert browser_fetcher.MAX_REDIRECTS == 5
    assert browser_fetcher.MAX_TEXT_BYTES == 256 * 1024


@pytest.mark.asyncio
async def test_browser_decompression_is_incrementally_capped() -> None:
    from aura_research_browser_fetcher.fetcher import _ResponseLimit, _stream_bounded_body

    class Response:
        headers = {"content-encoding": "gzip"}

        async def aiter_raw(self):
            compressed = gzip.compress(b"x" * 4096)
            yield compressed[:3]
            yield compressed[3:]

    with pytest.raises(_ResponseLimit, match="decompressed_bytes|wire_bytes"):
        await _stream_bounded_body(Response(), 128)


@pytest.mark.asyncio
async def test_browser_route_limit_state_isolated_per_concurrent_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_browser_fetcher import fetcher as browser_fetcher

    class Destination:
        def __init__(self, url: str) -> None:
            self.url = url
            self.hostname = "example.com"
            self.addresses = ("93.184.216.34",)

    class Request:
        resource_type = "document"
        method = "GET"
        headers: dict[str, str] = {}
        post_data_buffer = None

        def __init__(self, url: str) -> None:
            self.url = url
            self.redirected_from = None

    class Route:
        def __init__(self, url: str) -> None:
            self.request = Request(url)
            self.fulfilled = False
            self.aborted = False

        async def fulfill(self, **_kwargs: object) -> None:
            self.fulfilled = True

        async def abort(self) -> None:
            self.aborted = True

    class Response:
        status_code = 200
        headers = {"content-type": "text/plain"}

        def __init__(self, url: str) -> None:
            self.url = url

    class Stream:
        def __init__(self, url: str) -> None:
            self.url = url

        async def __aenter__(self) -> Response:
            return Response(self.url)

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(self, _method: str, url: str, *_args: object, **_kwargs: object) -> Stream:
            return Stream(url)

    async def validate(url: str) -> Destination:
        return Destination(url)

    async def bounded(response: Response, _max_bytes: int) -> tuple[bytes, int, int]:
        if "too-big" in response.url:
            raise browser_fetcher._ResponseLimit("wire_bytes")
        return b"bounded", 7, 7

    monkeypatch.setattr(browser_fetcher, "validate_public_https_url_async", validate)
    monkeypatch.setattr(browser_fetcher.httpx, "AsyncClient", Client)
    monkeypatch.setattr(browser_fetcher, "_stream_bounded_body", bounded)

    async def run(route: Route, state: object) -> None:
        await browser_fetcher.BrowserFetcher()._route_request(route, state=state)  # type: ignore[arg-type]

    # The two calls are deliberately overlapped; each receives its own state
    # object, so one rejection cannot poison the successful request.
    too_big = Route("https://too-big.example/")
    good = Route("https://good.example/")
    state_one = browser_fetcher._FetchState()
    state_two = browser_fetcher._FetchState()
    await asyncio.gather(run(too_big, state_one), run(good, state_two))
    assert state_one.limit_rejected is True
    assert state_two.limit_rejected is False
    assert too_big.aborted is True
    assert good.fulfilled is True


def test_public_ip_predicate_does_not_treat_reserved_space_as_public() -> None:
    assert not ipaddress.ip_address("192.0.2.1").is_global


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # IPv4 integer spelling for 127.0.0.1.
        "https://0x7f000001/",  # Hexadecimal IPv4 spelling.
        "https://0177.0.0.1/",  # Legacy octal first component.
        "https://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback.
        "https://[fc00::1]/",  # IPv6 unique-local address.
        "https://[fe80::1]/",  # IPv6 link-local address.
    ],
)
def test_destination_policy_rejects_encoded_private_ipv4_and_ipv6_targets(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura_research_static_fetcher import policy

    private = (2, 1, 6, "", ("127.0.0.1", 443))
    monkeypatch.setattr(policy.socket, "getaddrinfo", lambda *args, **kwargs: [private])
    with pytest.raises(DestinationBlocked):
        policy.validate_public_https_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:444/",
        "https://user:pass@example.com/",
        "https://example.com/path#fragment",
        "http://example.com/",
    ],
)
def test_destination_policy_rejects_redirect_downgrade_userinfo_port_and_fragment(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aura_research_static_fetcher import policy

    monkeypatch.setattr(
        policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    with pytest.raises(DestinationBlocked):
        policy.validate_public_https_url(url)


@pytest.mark.asyncio
async def test_static_fetch_revalidates_each_redirect_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_static_fetcher import fetcher as fetcher_module
    from aura_research_static_fetcher.policy import ValidatedDestination

    seen: list[str] = []

    async def validate(url: str) -> ValidatedDestination:
        seen.append(url)
        if url.endswith("/private"):
            raise DestinationBlocked("non_public_address")
        return ValidatedDestination(url=url, hostname="example.com", addresses=("93.184.216.34",))

    monkeypatch.setattr(fetcher_module, "validate_public_https_url_async", validate)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302 if request.url.path == "/start" else 200,
            headers={
                "location": "https://example.com/private" if request.url.path == "/start" else "",
                "content-type": "text/plain",
            },
            content=b"evidence",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await StaticFetcher(client=client).fetch(uuid4(), "https://example.com/start")
    assert result.outcome == "destination_blocked"
    assert seen == ["https://example.com/start", "https://example.com/private"]


@pytest.mark.asyncio
async def test_static_fetch_stops_redirect_loop_at_five_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_static_fetcher import fetcher as fetcher_module
    from aura_research_static_fetcher.policy import ValidatedDestination

    async def validated(url: str) -> ValidatedDestination:
        return ValidatedDestination(url=url, hostname="example.com", addresses=("93.184.216.34",))

    monkeypatch.setattr(fetcher_module, "validate_public_https_url_async", validated)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await StaticFetcher(client=client).fetch(uuid4(), "https://example.com/loop")
    assert result.outcome == "redirect_blocked"
    assert result.redirects == MAX_REDIRECTS


@pytest.mark.parametrize(
    ("declared", "payload"),
    [
        ("text/html", b"PK\x03\x04\x14\x00\x00\x00"),
        ("application/xhtml+xml", b"\x00\x01\x02not html"),
        ("text/plain", b"\x00secret bytes"),
    ],
)
def test_content_sniffing_rejects_binary_polyglots(declared: str, payload: bytes) -> None:
    detected = _sniff(payload)
    assert detected is None
    assert declared in {"text/html", "application/xhtml+xml", "text/plain"}


def test_extracted_text_is_utf8_bounded_without_splitting_multibyte_codepoints() -> None:
    text = "é" * (MAX_TEXT_BYTES + 64)
    bounded = _bounded_utf8(text)
    assert len(bounded.encode("utf-8")) <= MAX_TEXT_BYTES
    assert bounded.endswith("é")


@pytest.mark.asyncio
async def test_static_fetch_rejects_transferred_bytes_and_maps_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_static_fetcher import fetcher as fetcher_module
    from aura_research_static_fetcher.policy import ValidatedDestination

    async def valid(url: str) -> ValidatedDestination:
        return ValidatedDestination(url=url, hostname="example.com", addresses=("93.184.216.34",))

    async def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * (MAX_BYTES + 1),
        )

    async def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("deadline", request=request)

    monkeypatch.setattr(fetcher_module, "validate_public_https_url_async", valid)
    async with httpx.AsyncClient(transport=httpx.MockTransport(oversized)) as client:
        result = await StaticFetcher(client=client).fetch(uuid4(), "https://example.com/")
    assert result.outcome == "limit_rejected"
    assert result.error_class == "response_bytes"

    async with httpx.AsyncClient(transport=httpx.MockTransport(timed_out)) as client:
        result = await StaticFetcher(client=client).fetch(uuid4(), "https://example.com/")
    assert result.outcome == "timeout"
    assert result.error_class == "timeout"


@pytest.mark.asyncio
async def test_static_and_browser_semaphores_enforce_provider_concurrency() -> None:
    from aura_research_browser_fetcher.fetcher import BrowserFetcher

    async def assert_ceiling(fetcher: object, ceiling: int) -> None:
        active = 0
        maximum = 0
        lock = asyncio.Lock()

        async def fake_fetch(*args: object, **kwargs: object) -> object:
            nonlocal active, maximum
            async with lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0)
            async with lock:
                active -= 1
            return object()

        fetcher._fetch = fake_fetch  # type: ignore[attr-defined]
        await asyncio.gather(
            *(fetcher.fetch(f"request-{idx}", "https://example.com/") for idx in range(ceiling + 2))  # type: ignore[attr-defined]
        )
        assert maximum <= ceiling

    await assert_ceiling(StaticFetcher(), 4)
    await assert_ceiling(BrowserFetcher(), 2)


def test_hostile_research_content_is_evidence_not_provider_authority() -> None:
    hostile = (ROOT / "deploy/compose/e2e/research-fixtures/hostile.html").read_text(
        encoding="utf-8"
    )
    assert "Ignore prior instructions" in hostile
    assert "reveal provider secrets" in hostile
    source = (
        ROOT / "services/aura-research-static-fetcher/src/aura_research_static_fetcher/fetcher.py"
    ).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "exec(" not in source
    assert "os.system" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "source_root", "fixture_case", "request_kind"),
    [
        (
            "aura_research_static_fetcher.app",
            "services/aura-research-static-fetcher/src",
            "valid-static-fetch-request",
            "static_fetch_request",
        ),
        (
            "aura_research_browser_fetcher.app",
            "services/aura-research-browser-fetcher/src",
            "valid-browser-fetch-request",
            "browser_fetch_request",
        ),
    ],
)
async def test_provider_forwards_owner_lowered_limits_to_fetcher(
    module_name: str,
    source_root: str,
    fixture_case: str,
    request_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = ROOT / source_root
    sys.path.insert(0, str(source_path))
    try:
        import importlib

        module = importlib.import_module(module_name)
        fixture = json.loads(
            (ROOT / "contracts/providers/research/v1/conformance-fixtures.json").read_text(
                encoding="utf-8"
            )
        )
        document = next(
            item["document"] for item in fixture["cases"] if item["caseId"] == fixture_case
        )
        if request_kind == "browser_fetch_request":
            document["payload"]["limits"] = {
                "deadlineMs": 1_000,
                "maxRedirects": 1,
                "maxResponseBytes": 1_024,
                "maxExtractedUtf8Bytes": 1_024,
            }
        else:
            document["payload"]["limits"] = {
                "deadlineMs": 1_000,
                "maxRedirects": 1,
                "maxTransferredBytes": 1_024,
                "maxDecompressedBytes": 1_024,
                "maxExtractedUtf8Bytes": 1_024,
            }
        envelope = module.ProviderRequestEnvelope.model_validate(document)

        class RecordingFetcher:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            async def fetch(self, *args: object, **kwargs: object) -> object:
                self.calls.append((args, kwargs))
                from uuid import UUID

                values: dict[str, object] = {
                    "request_id": args[0] if isinstance(args[0], UUID) else uuid4(),
                    "outcome": "completed",
                    "final_url": "https://example.org/",
                    "detected_media_type": "text/plain",
                    "extraction_method": (
                        "bounded_text"
                        if request_kind == "static_fetch_request"
                        else "playwright_text"
                    ),
                    "text": "bounded evidence",
                    "content_sha256": "a" * 64,
                    "bytes_transferred": 17,
                }
                values["declared_media_type"] = "text/plain"
                return module.EvidenceResult(
                    **values,
                )

        recorder = RecordingFetcher()
        monkeypatch.setattr(module, "_enabled", True)
        monkeypatch.setattr(module, "_verify", lambda _envelope: None)
        monkeypatch.setattr(module, "fetcher", recorder)
        await module.fetch(envelope, types.SimpleNamespace(headers={}))
        rendered = repr(recorder.calls)
        assert "1000" in rendered
        assert "1024" in rendered
        assert "maxRedirects" in rendered or "max_redirects" in rendered
    finally:
        sys.path.remove(str(source_path))


@pytest.mark.asyncio
async def test_static_connection_pins_validated_ip_and_preserves_tls_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_static_fetcher import fetcher as fetcher_module
    from aura_research_static_fetcher.policy import ValidatedDestination

    observed: list[tuple[str, str | None, str | None]] = []
    pinned_addresses: list[str] = []

    async def validate(url: str) -> ValidatedDestination:
        return ValidatedDestination(
            url=url, hostname="example.com", addresses=("93.184.216.34",)
        )

    monkeypatch.setattr(fetcher_module, "validate_public_https_url_async", validate)

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.url.host,
                request.headers.get("host"),
                request.extensions.get("sni_hostname"),
            )
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"pinned evidence",
        )

    def pinned(address: str) -> httpx.MockTransport:
        pinned_addresses.append(address)
        return httpx.MockTransport(handler)

    monkeypatch.setattr(fetcher_module, "_PinnedAsyncHTTPTransport", pinned)
    result = await StaticFetcher().fetch(uuid4(), "https://example.com/")
    assert result.outcome == "completed"
    assert pinned_addresses == ["93.184.216.34"]
    assert observed == [("example.com", "example.com", "example.com")]


@pytest.mark.asyncio
async def test_browser_navigation_rejects_more_than_five_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_research_browser_fetcher import fetcher as fetcher_module
    from aura_research_browser_fetcher.fetcher import BrowserFetcher
    from aura_research_browser_fetcher.policy import ValidatedDestination

    class Request:
        def __init__(self, previous: object | None) -> None:
            self.redirected_from = previous

    class Response:
        def __init__(self) -> None:
            previous: object | None = None
            # Seven requests represent six redirects, exceeding the
            # five-hop ceiling while exactly five remains permitted.
            for _ in range(7):
                previous = Request(previous)
            self.request = previous

        async def all_headers(self) -> dict[str, str]:
            return {"content-type": "text/plain"}

        async def body(self) -> bytes:
            return b"bounded browser response"

    class Locator:
        async def inner_text(self, *, timeout: int) -> str:
            del timeout
            return "bounded browser response"

    class Page:
        async def route(self, _pattern: str, _handler: object) -> None:
            return None

        async def goto(self, _url: str, *, wait_until: str, timeout: int) -> Response:
            del wait_until, timeout
            return Response()

        def locator(self, _selector: str) -> Locator:
            return Locator()

    class Context:
        async def __aenter__(self) -> Context:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def new_page(self) -> Page:
            return Page()

        async def close(self) -> None:
            return None

    class Browser:
        async def new_context(self, **_kwargs: object) -> Context:
            return Context()

        async def close(self) -> None:
            return None

    class Chromium:
        async def launch(self, **_kwargs: object) -> Browser:
            return Browser()

    class Playwright:
        chromium = Chromium()

    class PlaywrightContext:
        async def __aenter__(self) -> Playwright:
            return Playwright()

        async def __aexit__(self, *_args: object) -> None:
            return None

    playwright_api = types.ModuleType("playwright.async_api")
    playwright_api.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})  # type: ignore[attr-defined]
    playwright_api.async_playwright = lambda: PlaywrightContext()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_api)
    monkeypatch.setattr(
        fetcher_module,
        "validate_public_https_url_async",
        lambda url: _browser_validated(url),
    )

    async def _browser_validated(url: str) -> ValidatedDestination:
        return ValidatedDestination(
            url=url, hostname="example.com", addresses=("93.184.216.34",)
        )

    result = await BrowserFetcher().fetch(uuid4(), "https://example.com/")
    assert result.outcome == "redirect_blocked"
