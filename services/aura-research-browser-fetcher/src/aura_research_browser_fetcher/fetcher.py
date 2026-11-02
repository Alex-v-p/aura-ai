"""Isolated Playwright retrieval with ephemeral context and bounded output."""

from __future__ import annotations

import asyncio
import hashlib
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, cast

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend  # type: ignore[reportMissingImports]
from pydantic import HttpUrl

from .models import EvidenceResult
from .policy import DestinationBlocked, validate_public_https_url_async

TraceContext = tuple[str, str]
ExtractionObserver = Callable[..., None]

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_REDIRECTS = 5
DEADLINE_SECONDS = 45.0


class _ResponseLimit(Exception):
    """Raised when either wire or decompressed response bytes exceed a cap."""


@dataclass(slots=True)
class _FetchState:
    """Mutable outcome state scoped to one browser fetch operation."""

    redirect_blocked: bool = False
    limit_rejected: bool = False
    limit_error: str = "response_bytes"


async def _stream_bounded_body(response: Any, max_bytes: int) -> tuple[bytes, int, int]:
    """Read a response with independent wire and decompressed byte bounds.

    ``aiter_raw`` is intentionally used so compressed wire bytes are accounted
    for before decompression. The provider only forwards a fully bounded body
    to Chromium; it never asks HTTPX to materialize an unbounded response.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in {"", "identity"}:
        decoder: Any = None
    elif encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise ValueError("unsupported_content_encoding")

    wire_bytes = 0
    decompressed_bytes = 0
    chunks: list[bytes] = []
    async for raw in response.aiter_raw():
        wire_bytes += len(raw)
        if wire_bytes > max_bytes:
            raise _ResponseLimit("wire_bytes")
        remaining = max_bytes - decompressed_bytes
        if remaining <= 0:
            raise _ResponseLimit("decompressed_bytes")
        chunk = (
            decoder.decompress(raw, remaining + 1) if decoder is not None else raw
        )
        decompressed_bytes += len(chunk)
        if decompressed_bytes > max_bytes:
            raise _ResponseLimit("decompressed_bytes")
        if decoder is not None and decoder.unconsumed_tail:
            raise _ResponseLimit("decompressed_bytes")
        chunks.append(chunk)
    if decoder is not None:
        chunk = decoder.flush(max_bytes - decompressed_bytes + 1)
        decompressed_bytes += len(chunk)
        if decompressed_bytes > max_bytes:
            raise _ResponseLimit("decompressed_bytes")
        chunks.append(chunk)
    return b"".join(chunks), wire_bytes, decompressed_bytes


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, address: str) -> None:
        self._address = address
        self._backend: Any = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        del host
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, address: str) -> None:
        super().__init__(trust_env=False, retries=0, limits=httpx.Limits(max_connections=1))
        self._pool = httpcore.AsyncConnectionPool(
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=_PinnedNetworkBackend(address),
        )


def _bounded(value: str) -> str:
    return value.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def _sniff(data: bytes) -> Literal["text/html", "text/plain"] | None:
    if b"\x00" in data[:8192]:
        return None
    sample = data[:8192].lstrip().lower()
    if sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<!--")):
        return "text/html"
    try:
        decoded = data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return None
    controls = sum(1 for char in decoded if ord(char) < 32 and char not in "\r\n\t")
    if controls > max(1, len(decoded) // 100):
        return None
    return "text/plain"


class BrowserFetcher:
    def __init__(self, *, extraction_observer: ExtractionObserver | None = None) -> None:
        self._semaphore = asyncio.Semaphore(2)
        self._in_flight: dict[Any, asyncio.Task[EvidenceResult]] = {}
        self._extraction_observer = extraction_observer

    def _notify_extraction(
        self,
        outcome: str,
        started: float,
        trace_context: TraceContext | None = None,
    ) -> None:
        if self._extraction_observer is None:
            return
        try:
            elapsed_ms = max(0.0, (monotonic() - started) * 1000)
            if trace_context is None:
                self._extraction_observer(outcome, elapsed_ms)
            else:
                self._extraction_observer(outcome, elapsed_ms, *trace_context)
        except Exception:
            return

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    async def fetch(
        self,
        request_id: Any,
        url: str,
        limits: dict[str, Any] | None = None,
        trace_context: TraceContext | None = None,
    ) -> EvidenceResult:
        task = asyncio.current_task()
        if task is None:
            return EvidenceResult(request_id=request_id, outcome="provider_unavailable")
        self._in_flight[request_id] = task
        try:
            async with self._semaphore:
                return await self._fetch(request_id, url, limits, trace_context)
        finally:
            self._in_flight.pop(request_id, None)

    def cancel(self, request_id: Any) -> Literal["accepted", "already_settled", "not_found"]:
        task = self._in_flight.get(request_id)
        if task is None:
            return "not_found"
        if task.done():
            return "already_settled"
        task.cancel()
        return "accepted"

    def cancel_all(self) -> int:
        """Cancel every in-flight operation except the caller's own task."""
        current = asyncio.current_task()
        canceled = 0
        for task in tuple(self._in_flight.values()):
            if task is current or task.done():
                continue
            task.cancel()
            canceled += 1
        return canceled

    async def _fetch(
        self,
        request_id: Any,
        url: str,
        limits: dict[str, Any] | None = None,
        trace_context: TraceContext | None = None,
    ) -> EvidenceResult:
        limits = limits or {}
        deadline = min(
            DEADLINE_SECONDS, int(limits.get("deadlineMs", DEADLINE_SECONDS * 1000)) / 1000
        )
        max_bytes = min(MAX_BYTES, int(limits.get("maxResponseBytes", MAX_BYTES)))
        max_text_bytes = min(
            MAX_TEXT_BYTES, int(limits.get("maxExtractedUtf8Bytes", MAX_TEXT_BYTES))
        )
        max_redirects = min(MAX_REDIRECTS, int(limits.get("maxRedirects", MAX_REDIRECTS)))
        state = _FetchState()
        try:
            destination = await validate_public_https_url_async(url)
        except DestinationBlocked as exc:
            return EvidenceResult(
                request_id=request_id, outcome="destination_blocked", error_class=str(exc)
            )
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError:
            return EvidenceResult(
                request_id=request_id, outcome="provider_unavailable", error_class="browser_runtime"
            )

        try:
            async with asyncio.timeout(deadline):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        chromium_sandbox=True,
                        args=[
                            "--disable-dev-shm-usage",
                            "--disable-background-networking",
                            "--disable-extensions",
                        ],
                    )
                    try:
                        context = await browser.new_context(
                            accept_downloads=False,
                            service_workers="block",
                            permissions=[],
                            java_script_enabled=True,
                            viewport={"width": 1280, "height": 720},
                        )
                        try:
                            page = await context.new_page()

                            async def bounded_route(route: Any) -> None:
                                await self._route_request(
                                    route,
                                    max_bytes=max_bytes,
                                    max_redirects=max_redirects,
                                    state=state,
                                )

                            await page.route("**/*", bounded_route)
                            response = await page.goto(
                                destination.url,
                                wait_until="domcontentloaded",
                                timeout=int(deadline * 1000),
                            )
                            if response is None:
                                if state.limit_rejected:
                                    return EvidenceResult(
                                        request_id=request_id,
                                        outcome="limit_rejected",
                                        error_class=state.limit_error,
                                    )
                                if state.redirect_blocked:
                                    return EvidenceResult(
                                        request_id=request_id,
                                        outcome="redirect_blocked",
                                        redirects=MAX_REDIRECTS,
                                        error_class="redirect_limit",
                                    )
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="provider_unavailable",
                                    error_class="no_response",
                                )
                            if state.limit_rejected:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="limit_rejected",
                                    error_class=state.limit_error,
                                )
                            if state.redirect_blocked:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="redirect_blocked",
                                    redirects=MAX_REDIRECTS,
                                    error_class="redirect_limit",
                                )
                            redirects = 0
                            previous = getattr(response.request, "redirected_from", None)
                            while previous is not None:
                                redirects += 1
                                previous = getattr(previous, "redirected_from", None)
                            if redirects > max_redirects:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="redirect_blocked",
                                    redirects=min(redirects, MAX_REDIRECTS),
                                    error_class="redirect_limit",
                                )
                            headers = await response.all_headers()
                            content_type = (
                                headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            )
                            if content_type not in {
                                "text/html",
                                "application/xhtml+xml",
                                "text/plain",
                            }:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="unsupported_media_type",
                                    final_url=HttpUrl(destination.url),
                                    redirects=redirects,
                                    error_class="media_type",
                                )
                            declared_media_type = cast(
                                Literal["text/html", "application/xhtml+xml", "text/plain"],
                                content_type,
                            )
                            body = await response.body()
                            raw_final_url = getattr(page, "url", destination.url)
                            final_destination = await validate_public_https_url_async(raw_final_url)
                            if len(body) > max_bytes:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="limit_rejected",
                                    final_url=HttpUrl(final_destination.url),
                                    redirects=redirects,
                                    error_class="response_bytes",
                                )
                            detected = _sniff(body)
                            compatible = (
                                detected == "text/plain"
                                if content_type == "text/plain"
                                else detected == "text/html"
                            )
                            if not compatible:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="unsupported_media_type",
                                    final_url=HttpUrl(final_destination.url),
                                    redirects=redirects,
                                    error_class="mime_mismatch",
                                )
                            extraction_started = monotonic()
                            try:
                                text = _bounded(
                                    await page.locator("body").inner_text(timeout=5000)
                                )
                            except Exception:
                                self._notify_extraction(
                                    "rejected", extraction_started, trace_context
                                )
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="inadequate_extraction",
                                    final_url=HttpUrl(final_destination.url),
                                    declared_media_type=declared_media_type,
                                    detected_media_type=detected,
                                    redirects=redirects,
                                    error_class="dom_extraction",
                                )
                            text = text.encode("utf-8")[:max_text_bytes].decode(
                                "utf-8", errors="ignore"
                            )
                            if not text:
                                self._notify_extraction(
                                    "rejected", extraction_started, trace_context
                                )
                                return EvidenceResult(
                                request_id=request_id,
                                outcome="inadequate_extraction",
                                final_url=HttpUrl(final_destination.url),
                                declared_media_type=declared_media_type,
                                detected_media_type=detected,
                                redirects=redirects,
                                error_class="empty_text",
                                )
                            self._notify_extraction("success", extraction_started, trace_context)
                            return EvidenceResult(
                                request_id=request_id,
                                outcome="completed",
                                final_url=HttpUrl(final_destination.url),
                                declared_media_type=declared_media_type,
                                detected_media_type=detected,
                                extraction_method="playwright_text",
                                text=text,
                                content_sha256=hashlib.sha256(body).hexdigest(),
                                bytes_transferred=len(body),
                                redirects=redirects,
                            )
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
        except TimeoutError:
            return EvidenceResult(request_id=request_id, outcome="timeout", error_class="timeout")
        except PlaywrightTimeoutError:
            return EvidenceResult(request_id=request_id, outcome="timeout", error_class="timeout")
        except asyncio.CancelledError:
            return EvidenceResult(request_id=request_id, outcome="canceled", error_class="canceled")
        except Exception:
            if state.redirect_blocked:
                return EvidenceResult(
                    request_id=request_id,
                    outcome="redirect_blocked",
                    redirects=MAX_REDIRECTS,
                    error_class="redirect_limit",
                )
            if state.limit_rejected:
                return EvidenceResult(
                    request_id=request_id,
                    outcome="limit_rejected",
                    error_class=state.limit_error,
                )
            return EvidenceResult(
                request_id=request_id, outcome="provider_unavailable", error_class="browser_error"
            )

    async def _route_request(
        self,
        route: Any,
        *,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        state: _FetchState | None = None,
    ) -> None:
        """Fulfil browser requests through the validated in-process egress."""
        if state is None:
            state = _FetchState()
        request = route.request
        if request.resource_type in {"image", "font", "media", "websocket"}:
            await route.abort()
            return
        redirects = 0
        previous = getattr(request, "redirected_from", None)
        while previous is not None:
            redirects += 1
            previous = getattr(previous, "redirected_from", None)
        if redirects > max_redirects:
            state.redirect_blocked = True
            await route.abort()
            return
        try:
            destination = await validate_public_https_url_async(request.url)
            async with httpx.AsyncClient(
                transport=_PinnedAsyncHTTPTransport(destination.addresses[0]),
                timeout=httpx.Timeout(DEADLINE_SECONDS),
                headers={"Accept-Encoding": "identity"},
            ) as client:
                body: bytes | None = None
                if request.method not in {"GET", "HEAD"}:
                    posted: Any = getattr(request, "post_data_buffer", None)
                    value: Any = posted() if callable(posted) else posted
                    body = await value if hasattr(value, "__await__") else value
                async with client.stream(
                    request.method,
                    destination.url,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() not in {"host", "content-length"}
                    },
                    content=body,
                    follow_redirects=False,
                    extensions={"sni_hostname": destination.hostname},
                ) as response:
                    bounded_body, _wire_bytes, _decompressed_bytes = await _stream_bounded_body(
                        response, max_bytes
                    )
                    response_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower()
                        not in {"content-encoding", "content-length", "transfer-encoding"}
                    }
                    status_code = response.status_code
            await route.fulfill(
                status=status_code,
                headers=response_headers,
                body=bounded_body,
            )
        except _ResponseLimit as exc:
            state.limit_rejected = True
            state.limit_error = str(exc)
            await route.abort()
        except Exception:
            await route.abort()

    @staticmethod
    async def _route(route: Any) -> None:
        request_url = route.request.url.lower()
        if not request_url.startswith("https://"):
            await route.abort()
            return
        try:
            await validate_public_https_url_async(route.request.url)
        except DestinationBlocked:
            await route.abort()
            return
        redirected_from = getattr(route.request, "redirected_from", None)
        redirects = 0
        while redirected_from is not None:
            redirects += 1
            if redirects > MAX_REDIRECTS:
                await route.abort()
                return
            redirected_from = getattr(redirected_from, "redirected_from", None)
        # Images, fonts, media, downloads, and websocket endpoints are not
        # evidence and create unnecessary browser attack surface.
        resource_type = route.request.resource_type
        if resource_type in {"image", "font", "media", "websocket"}:
            await route.abort()
            return
        await route.continue_()
