"""FastAPI entrypoint for the static research provider."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .fetcher import (
    MAX_BYTES,
    MAX_TEXT_BYTES,
    StaticFetcher,
)
from .models import (
    CancelRequest,
    CancelResult,
    EvidenceResult,
    FetchedEvidence,
    FetchInadequateResult,
    FetchResult,
    HealthRequest,
    HealthResult,
    HealthTransportResult,
    LimitsRequest,
    LimitsResult,
    MediaType,
    ProviderError,
    ProviderErrorEnvelope,
    ProviderRequestEnvelope,
    ProviderResultEnvelope,
    StaticFetchRequest,
    StaticProviderLimits,
)
from .telemetry import production_extraction_sink


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        yield
    finally:
        production_extraction_sink.close()


app = FastAPI(
    title="Aura Research Static Fetcher",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)
fetcher = StaticFetcher(extraction_observer=production_extraction_sink)


_seen: dict[str, float] = {}
_REPLAY_CACHE_CAPACITY = 4096
_enabled = os.getenv("AURA_RESEARCH_STATIC_ENABLED", "false").lower() == "true"
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def _admitted_trace_context(request: Request) -> tuple[str, str] | None:
    """Accept only a canonical Core-admitted W3C traceparent."""
    value = request.headers.get("traceparent", "")
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, parent_span_id, _flags = match.groups()
    if set(trace_id) == {"0"} or set(parent_span_id) == {"0"}:
        return None
    return trace_id, parent_span_id


class RuntimeControl:
    """Authenticated in-process provider state and cancellation transition."""

    def __init__(self) -> None:
        self.draining = os.getenv("AURA_RESEARCH_STATIC_DRAINING", "false").lower() == "true"
        self.immediate_stop = (
            os.getenv("AURA_RESEARCH_STATIC_IMMEDIATE_STOP", "false").lower() == "true"
        )

    def begin_graceful_drain(self) -> None:
        self.draining = True

    def immediate_stop_now(self) -> int:
        self.draining = True
        self.immediate_stop = True
        return fetcher.cancel_all()

    def apply_authenticated(self, *, draining: bool = False, immediate_stop: bool = False) -> int:
        if draining:
            self.begin_graceful_drain()
        if immediate_stop:
            return self.immediate_stop_now()
        return 0


runtime_control = RuntimeControl()


def _draining() -> bool:
    return runtime_control.draining


def _immediate_stop() -> bool:
    return runtime_control.immediate_stop


def _apply_runtime_stop() -> bool:
    if not _immediate_stop():
        return False
    fetcher.cancel_all()
    return True


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"code": "contract_invalid", "retryable": False})


def _canonical(envelope: ProviderRequestEnvelope) -> bytes:
    value = envelope.model_dump(mode="json")
    value.pop("signature", None)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _verify(envelope: ProviderRequestEnvelope) -> None:
    now = time.time()
    for key, expiry in list(_seen.items()):
        if expiry <= now:
            _seen.pop(key, None)
    if (
        envelope.issuedAt.timestamp() > now + 5
        or envelope.expiresAt <= envelope.issuedAt
        or envelope.expiresAt - envelope.issuedAt != timedelta(milliseconds=envelope.ttlMs)
        or envelope.expiresAt - envelope.issuedAt > timedelta(seconds=60)
    ):
        raise HTTPException(status_code=401, detail="invalid_envelope")
    if envelope.expiresAt.timestamp() <= now or envelope.expiresAt.timestamp() > now + 60:
        raise HTTPException(status_code=401, detail="invalid_envelope")
    if str(envelope.replayId) in _seen:
        raise HTTPException(status_code=409, detail="replay_rejected")
    key_file = os.getenv("AURA_PROVIDER_PUBLIC_KEY_FILE", "")
    if key_file:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            raw = Path(key_file).read_bytes().strip()
            try:
                raw = base64.urlsafe_b64decode(raw + b"===")
            except Exception:
                raw = base64.b64decode(raw, validate=True)
            signature = base64.urlsafe_b64decode(envelope.signature.value + "==")
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, _canonical(envelope))
        except Exception as exc:
            raise HTTPException(status_code=401, detail="invalid_envelope") from exc
    else:
        raise HTTPException(status_code=503, detail="provider_not_configured")
    if len(_seen) >= _REPLAY_CACHE_CAPACITY:
        raise HTTPException(status_code=503, detail="replay_cache_full")
    _seen[str(envelope.replayId)] = envelope.expiresAt.timestamp()


def _error(
    request: ProviderRequestEnvelope,
    code: Any,
    *,
    retryable: bool,
    http_status: int,
) -> JSONResponse:
    payload = ProviderErrorEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="error",
        responseId=uuid4(),
        requestId=request.requestId,
        audience="static_fetcher",
        method=request.method,
        producedAt=datetime.now(UTC),
        error=ProviderError(code=code, retryable=retryable),
    )
    return JSONResponse(status_code=http_status, content=payload.model_dump(mode="json"))


def _verification_error(
    envelope: ProviderRequestEnvelope, exc: HTTPException
) -> JSONResponse:
    """Map replay-capacity/configuration failures to retryable unavailability."""
    code = "replayed" if exc.status_code == 409 else "signature_invalid"
    if exc.status_code >= 500:
        code = "provider_unavailable"
    return _error(
        envelope,
        code,
        retryable=exc.status_code >= 500,
        http_status=exc.status_code,
    )


def _configured() -> bool:
    key_file = os.getenv("AURA_PROVIDER_PUBLIC_KEY_FILE", "")
    if not key_file:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        raw = Path(key_file).read_bytes().strip()
        try:
            raw = base64.urlsafe_b64decode(raw + b"===")
        except Exception:
            raw = base64.b64decode(raw, validate=True)
        Ed25519PublicKey.from_public_bytes(raw)
        return True
    except Exception:
        return False


def _policy_ready() -> bool:
    policy_file = os.getenv("AURA_RESEARCH_EGRESS_POLICY_FILE", "/run/provider/egress-policy.yaml")
    probe_file = os.getenv("AURA_RESEARCH_EGRESS_ATTESTATION_PROBE_PATH", "")
    if not probe_file:
        return False
    try:
        Path(policy_file).read_text(encoding="utf-8")
        probe_path = Path(probe_file)
        # The repository checker is diagnostic/test-only; it cannot attest a
        # live provider network and is never accepted as production evidence.
        if not probe_path.is_file() or probe_path.name == "egress_probe.py":
            return False
        result = subprocess.run(
            [sys.executable, probe_file, "--verify-enforcement"],
            check=True,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout.strip() == "aura.research.egress.attested.v1"
    except OSError:
        return False
    except (subprocess.SubprocessError, UnicodeError, ValueError):
        return False


def _transport(request: ProviderRequestEnvelope, result: EvidenceResult) -> JSONResponse:
    if result.outcome == "inadequate_extraction":
        assert (
            result.final_url is not None
            and result.declared_media_type is not None
            and result.detected_media_type is not None
        )
        payload = ProviderResultEnvelope(
            schemaVersion="research-provider.v1",
            envelopeType="result",
            responseId=uuid4(),
            requestId=request.requestId,
            audience="static_fetcher",
            method="fetch",
            producedAt=datetime.now(UTC),
            result=FetchInadequateResult(
                kind="fetch_result",
                outcome="inadequate_extraction",
                finalUrl=result.final_url,
                redirectCount=result.redirects,
                declaredMediaType=cast(MediaType, result.declared_media_type),
                detectedMediaType=result.detected_media_type,
                transferredBytes=result.bytes_transferred,
            ),
        )
        return JSONResponse(content=payload.model_dump(mode="json", exclude_none=True))
    if result.outcome == "completed":
        assert result.final_url is not None and result.declared_media_type is not None
        assert result.detected_media_type is not None and result.text is not None
        assert result.content_sha256 is not None and result.extraction_method is not None
        evidence = FetchedEvidence(
            finalUrl=result.final_url,
            redirectCount=result.redirects,
            declaredMediaType=cast(MediaType, result.declared_media_type),
            detectedMediaType=result.detected_media_type,
            retrievedAt=datetime.now(UTC),
            transferredBytes=result.bytes_transferred,
            extractedUtf8Bytes=len(result.text.encode()),
            contentSha256=result.content_sha256,
            extractionMethod="trafilatura"
            if result.extraction_method == "trafilatura"
            else "plain_text",
            extractedText=result.text,
        )
        fetch_result = FetchResult(kind="fetch_result", outcome="success", evidence=evidence)
    else:
        code = {
            "destination_blocked": "destination_blocked",
            "redirect_blocked": "redirect_blocked",
            "unsupported_media_type": "type_rejected",
            "limit_rejected": "size_exceeded",
            "timeout": "deadline_exceeded",
            "canceled": "canceled",
        }.get(result.outcome, "provider_unavailable")
        error = ProviderErrorEnvelope(
            schemaVersion="research-provider.v1",
            envelopeType="error",
            responseId=uuid4(),
            requestId=request.requestId,
            audience="static_fetcher",
            method="fetch",
            producedAt=datetime.now(UTC),
            error=ProviderError(
                code=cast(Any, code),
                retryable=code in {"deadline_exceeded", "provider_unavailable"},
            ),
        )
        return JSONResponse(status_code=502, content=error.model_dump(mode="json"))
    payload = ProviderResultEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="result",
        responseId=uuid4(),
        requestId=request.requestId,
        audience="static_fetcher",
        method="fetch",
        producedAt=datetime.now(UTC),
        result=fetch_result,
    )
    return JSONResponse(content=payload.model_dump(mode="json", exclude_none=True))


@app.get("/health/live", response_model=HealthResult)
async def live() -> HealthResult:
    immediate = _apply_runtime_stop()
    draining = _draining() or immediate
    accepting = _enabled and not draining and _configured() and _policy_ready()
    return HealthResult(
        kind="health_result",
        status="live",
        enabled=_enabled,
        accepting=accepting,
        inFlight=fetcher.in_flight,
        contractVersion="research-provider.v1",
        degradation=("disabled" if not _enabled else "draining" if draining else None),
    )


@app.get("/health/ready", response_model=HealthResult)
async def ready(response: Response) -> HealthResult:
    configured = _configured()
    policy = _policy_ready()
    capacity = fetcher.in_flight < 4
    immediate = _apply_runtime_stop()
    draining = _draining() or immediate
    if not _enabled or draining or not configured or not policy or not capacity:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResult(
            kind="health_result",
            status=(
                "degraded"
                if _enabled and configured and policy and (draining or not capacity)
                else "not_ready"
            ),
            enabled=_enabled,
            accepting=False,
            inFlight=fetcher.in_flight,
            contractVersion="research-provider.v1",
            degradation=(
                "disabled"
                if not _enabled
                else "draining"
                if draining
                else "configuration_invalid"
                if not configured or not policy
                else "capacity"
            ),
        )
    return HealthResult(
        kind="health_result",
        status="ready",
        enabled=True,
        accepting=True,
        inFlight=fetcher.in_flight,
        contractVersion="research-provider.v1",
    )


async def limits() -> LimitsResult:
    return LimitsResult(
        kind="limits_result",
        limits=StaticProviderLimits(
            service="static_fetcher",
            deadlineMs=20_000,
            maxRedirects=5,
            maxTransferredBytes=MAX_BYTES,
            maxDecompressedBytes=MAX_BYTES,
            maxExtractedUtf8Bytes=MAX_TEXT_BYTES,
            maxConcurrency=4,
            cpuMillicores=2_000,
            memoryBytes=1024 * 1024 * 1024,
            pids=64,
            temporaryStorageBytes=64 * 1024 * 1024,
        ),
    )


@app.post("/internal/v1/fetch", response_model=None)
async def fetch(
    envelope: ProviderRequestEnvelope,
    request: Request,
) -> ProviderResultEnvelope | ProviderErrorEnvelope | JSONResponse:
    if envelope.method != "fetch" or not isinstance(envelope.payload, StaticFetchRequest):
        return _error(envelope, "method_unsupported", retryable=False, http_status=400)
    try:
        _verify(envelope)
        if not _enabled:
            return _error(envelope, "disabled", retryable=False, http_status=503)
        if _apply_runtime_stop() or _draining():
            return _error(envelope, "draining", retryable=True, http_status=503)
        return _transport(
            envelope,
            await fetcher.fetch(
                envelope.requestId,
                str(envelope.payload.targetUrl),
                envelope.payload.limits.model_dump(),
                trace_context=_admitted_trace_context(request),
            ),
        )
    except HTTPException as exc:
        return _verification_error(envelope, exc)


@app.post("/internal/v1/cancel", response_model=None)
async def cancel(
    envelope: ProviderRequestEnvelope,
) -> ProviderResultEnvelope | ProviderErrorEnvelope | JSONResponse:
    if envelope.method != "cancel" or not isinstance(envelope.payload, CancelRequest):
        return _error(envelope, "method_unsupported", retryable=False, http_status=400)
    try:
        _verify(envelope)
    except HTTPException as exc:
        return _verification_error(envelope, exc)
    if envelope.payload.reason == "operator_stop":
        runtime_control.apply_authenticated(immediate_stop=True)
    result = ProviderResultEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="result",
        responseId=uuid4(),
        requestId=envelope.requestId,
        audience="static_fetcher",
        method="cancel",
        producedAt=datetime.now(UTC),
        result=CancelResult(
            kind="cancel_result", outcome=fetcher.cancel(envelope.payload.targetRequestId)
        ),
    )
    return result


@app.post("/internal/v1/limits", response_model=None)
async def signed_limits(
    envelope: ProviderRequestEnvelope,
) -> ProviderResultEnvelope | ProviderErrorEnvelope | JSONResponse:
    if envelope.method != "limits" or not isinstance(envelope.payload, LimitsRequest):
        return _error(envelope, "method_unsupported", retryable=False, http_status=400)
    try:
        _verify(envelope)
    except HTTPException as exc:
        return _verification_error(envelope, exc)
    if not _enabled:
        return _error(envelope, "disabled", retryable=False, http_status=503)
    return ProviderResultEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="result",
        responseId=uuid4(),
        requestId=envelope.requestId,
        audience="static_fetcher",
        method="limits",
        producedAt=datetime.now(UTC),
        result=(await limits()),
    )


@app.post("/internal/v1/health", response_model=None)
async def signed_health(
    envelope: ProviderRequestEnvelope,
) -> ProviderResultEnvelope | ProviderErrorEnvelope | JSONResponse:
    if envelope.method != "health" or not isinstance(envelope.payload, HealthRequest):
        return _error(envelope, "method_unsupported", retryable=False, http_status=400)
    try:
        _verify(envelope)
    except HTTPException as exc:
        return _verification_error(envelope, exc)
    health = await ready(Response())
    return ProviderResultEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="result",
        responseId=uuid4(),
        requestId=envelope.requestId,
        audience="static_fetcher",
        method="health",
        producedAt=datetime.now(UTC),
        result=HealthTransportResult.model_validate(health.model_dump()),
    )
