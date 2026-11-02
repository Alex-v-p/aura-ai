"""Bounded HTTPX/Trafilatura retrieval implementation."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any, Literal

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend  # type: ignore[reportMissingImports]
from pydantic import HttpUrl

from .models import EvidenceResult
from .policy import DestinationBlocked, validate_public_https_url_async

TraceContext = tuple[str, str]
ExtractionObserver = Callable[..., None]

try:  # optional in local contract-only environments
    import trafilatura
except ImportError:  # pragma: no cover - exercised in the minimal dev image
    trafilatura = None  # type: ignore[assignment]


MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_REDIRECTS = 5
DEADLINE_SECONDS = 20.0
MediaType = Literal["text/html", "application/xhtml+xml", "text/plain"]
_ALLOWED: set[str] = {"text/html", "application/xhtml+xml", "text/plain"}
_REDIRECTS = {301, 302, 303, 307, 308}


class LimitRejected(RuntimeError):
    pass


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once in policy, then connect only to that validated address."""

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


def _media(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().lower() if value else None


def _sniff(data: bytes) -> MediaType | None:
    if b"\x00" in data[:8192]:
        return None
    sample = data[:8192].lstrip().lower()
    if sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<!--")):
        return "text/html"
    try:
        decoded = data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not decoded:
        return "text/plain"
    # Reject binary/polyglot data while accepting ordinary UTF-8 text.
    controls = sum(1 for char in decoded if ord(char) < 32 and char not in "\r\n\t")
    if controls > max(1, len(decoded) // 100):
        return None
    return "text/plain"


def _bounded_utf8(value: str, max_bytes: int = MAX_TEXT_BYTES) -> str:
    raw = value.encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


def _notify_extraction(
    observer: ExtractionObserver | None,
    outcome: str,
    started: float,
    trace_context: TraceContext | None = None,
) -> None:
    """Send bounded extraction metadata without affecting retrieval."""
    if observer is None:
        return
    try:
        elapsed_ms = max(0.0, (monotonic() - started) * 1000)
        if trace_context is None:
            observer(outcome, elapsed_ms)
        else:
            observer(outcome, elapsed_ms, *trace_context)
    except Exception:
        return


def _fallback_extract(data: bytes, media_type: str) -> str:
    text = data.decode("utf-8", errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"}:
        text = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>", " ", text)
        text = re.sub(r"(?s)<[^>]*>", " ", text)
        text = html.unescape(text)
    return " ".join(text.split())


async def _body(
    response: httpx.Response,
    max_wire_bytes: int = MAX_BYTES,
    max_decompressed_bytes: int = MAX_BYTES,
) -> AsyncIterator[bytes]:
    """Stream identity-encoded response bytes with independent ceilings.

    Production clients request identity encoding and reject a server that
    ignores it.  Content-Length bounds the wire declaration while this
    iterator bounds the decoded stream before it is buffered.
    """
    wire_total = 0
    decompressed_total = 0
    # Mock transports may hand us an already materialized response; the
    # production stream remains raw until this point.
    stream = response.aiter_raw() if not response.is_stream_consumed else response.aiter_bytes()
    async for chunk in stream:
        wire_total += len(chunk)
        if wire_total > max_wire_bytes:
            raise LimitRejected("response_bytes")
        decompressed_total += len(chunk)
        if decompressed_total > max_decompressed_bytes:
            raise LimitRejected("decompressed_bytes")
        yield chunk


async def _consume_response(
    response: httpx.Response,
    request_id: Any,
    destination: Any,
    redirects: int,
    max_wire_bytes: int,
    max_bytes: int,
    max_text_bytes: int,
    extraction_observer: ExtractionObserver | None = None,
    trace_context: TraceContext | None = None,
) -> EvidenceResult | str:
    """Consume one streamed response, or return its validated redirect target."""
    if response.status_code in _REDIRECTS:
        location = response.headers.get("location")
        if not location:
            return EvidenceResult(
                request_id=request_id,
                outcome="redirect_blocked",
                redirects=redirects,
                error_class="missing_location",
            )
        from urllib.parse import urljoin

        return urljoin(destination.url, location)
    declared = _media(response.headers.get("content-type"))
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_wire_bytes:
                return EvidenceResult(
                    request_id=request_id,
                    outcome="limit_rejected",
                    final_url=HttpUrl(destination.url),
                    declared_media_type=declared,
                    redirects=redirects,
                    error_class="response_bytes",
                )
        except ValueError:
            return EvidenceResult(
                request_id=request_id,
                outcome="limit_rejected",
                final_url=HttpUrl(destination.url),
                declared_media_type=declared,
                redirects=redirects,
                error_class="content_length",
            )
    if declared not in _ALLOWED:
        return EvidenceResult(
            request_id=request_id,
            outcome="unsupported_media_type",
            final_url=HttpUrl(destination.url),
            declared_media_type=declared,
            redirects=redirects,
            error_class="media_type",
        )
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        return EvidenceResult(
            request_id=request_id,
            outcome="unsupported_media_type",
            final_url=HttpUrl(destination.url),
            declared_media_type=declared,
            redirects=redirects,
            error_class="content_encoding",
        )
    try:
        chunks = [
            chunk async for chunk in _body(response, max_wire_bytes, max_bytes)
        ]
    except LimitRejected as exc:
        return EvidenceResult(
            request_id=request_id,
            outcome="limit_rejected",
            final_url=HttpUrl(destination.url),
            declared_media_type=declared,
            redirects=redirects,
            error_class=str(exc),
        )
    data = b"".join(chunks)
    detected = _sniff(data)
    compatible = detected == "text/plain" if declared == "text/plain" else detected == "text/html"
    if not compatible:
        return EvidenceResult(
            request_id=request_id,
            outcome="unsupported_media_type",
            final_url=HttpUrl(destination.url),
            declared_media_type=declared,
            detected_media_type=detected,
            redirects=redirects,
            error_class="mime_mismatch",
        )
    extracted: str | None = None
    extraction_started = monotonic()
    method = "bounded_text"
    if trafilatura is not None and declared in {"text/html", "application/xhtml+xml"}:
        try:
            extracted = await asyncio.to_thread(
                trafilatura.extract, data, include_comments=False, include_tables=False
            )
            method = "trafilatura"
        except Exception:
            extracted = None
    if not extracted:
        extracted = _fallback_extract(data, declared)
    extracted = _bounded_utf8(extracted, max_text_bytes)
    if not extracted:
        _notify_extraction(
            extraction_observer, "rejected", extraction_started, trace_context
        )
        return EvidenceResult(
            request_id=request_id,
            outcome="inadequate_extraction",
            final_url=HttpUrl(destination.url),
            declared_media_type=declared,
            detected_media_type=detected or "text/plain",
            redirects=redirects,
            error_class="empty_text",
        )
    _notify_extraction(extraction_observer, "success", extraction_started, trace_context)
    return EvidenceResult(
        request_id=request_id,
        outcome="completed",
        final_url=HttpUrl(destination.url),
        declared_media_type=declared,
        detected_media_type=detected or "text/plain",
        extraction_method=method,
        text=extracted,
        content_sha256=hashlib.sha256(data).hexdigest(),
        bytes_transferred=len(data),
        redirects=redirects,
    )


class StaticFetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        extraction_observer: ExtractionObserver | None = None,
    ) -> None:
        self._client = client
        self._extraction_observer = extraction_observer
        self._semaphore = asyncio.Semaphore(4)
        self._in_flight: dict[Any, asyncio.Task[EvidenceResult]] = {}

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    @asynccontextmanager
    async def _client_context(self) -> AsyncGenerator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(DEADLINE_SECONDS),
            headers={"Accept": "text/html, application/xhtml+xml, text/plain"},
        ) as client:
            yield client

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
        max_wire_bytes = min(MAX_BYTES, int(limits.get("maxTransferredBytes", MAX_BYTES)))
        max_bytes = min(MAX_BYTES, int(limits.get("maxDecompressedBytes", MAX_BYTES)))
        max_text_bytes = min(
            MAX_TEXT_BYTES, int(limits.get("maxExtractedUtf8Bytes", MAX_TEXT_BYTES))
        )
        max_redirects = min(MAX_REDIRECTS, int(limits.get("maxRedirects", MAX_REDIRECTS)))
        current = url
        redirects = 0
        try:
            async with asyncio.timeout(deadline):
                async with self._client_context() as client:
                    while True:
                        destination = await validate_public_https_url_async(current)
                        try:
                            if self._client is not None:
                                async with client.stream(
                                    "GET",
                                    destination.url,
                                    extensions={"sni_hostname": destination.hostname},
                                    follow_redirects=False,
                                ) as response:
                                    outcome = await _consume_response(
                                        response,
                                        request_id,
                                        destination,
                                        redirects,
                                        max_wire_bytes,
                                        max_bytes,
                                        max_text_bytes,
                                        self._extraction_observer,
                                        trace_context,
                                    )
                            else:
                                async with httpx.AsyncClient(
                                    transport=_PinnedAsyncHTTPTransport(destination.addresses[0]),
                                    timeout=httpx.Timeout(deadline),
                                    headers={"Accept-Encoding": "identity"},
                                ) as pinned_client:
                                    async with pinned_client.stream(
                                        "GET",
                                        destination.url,
                                        follow_redirects=False,
                                        extensions={"sni_hostname": destination.hostname},
                                    ) as response:
                                        outcome = await _consume_response(
                                            response,
                                            request_id,
                                            destination,
                                            redirects,
                                            max_wire_bytes,
                                            max_bytes,
                                            max_text_bytes,
                                            self._extraction_observer,
                                            trace_context,
                                        )
                        except httpx.TimeoutException, TimeoutError:
                            return EvidenceResult(
                                request_id=request_id, outcome="timeout", error_class="timeout"
                            )
                        except httpx.HTTPError:
                            return EvidenceResult(
                                request_id=request_id,
                                outcome="provider_unavailable",
                                error_class="http_error",
                            )
                        if isinstance(outcome, str):
                            if redirects >= max_redirects:
                                return EvidenceResult(
                                    request_id=request_id,
                                    outcome="redirect_blocked",
                                    redirects=redirects,
                                    error_class="redirect_limit",
                                )
                            current = outcome
                            redirects += 1
                            continue
                        return outcome
        except DestinationBlocked as exc:
            reason = str(exc)
            return EvidenceResult(
                request_id=request_id, outcome="destination_blocked", error_class=reason
            )
        except TimeoutError:
            return EvidenceResult(request_id=request_id, outcome="timeout", error_class="timeout")
        except asyncio.CancelledError:
            return EvidenceResult(request_id=request_id, outcome="canceled", error_class="canceled")
