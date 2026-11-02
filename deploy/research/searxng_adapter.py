"""Credential-free Core-facing adapter for the private stock SearXNG service."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

app = FastAPI(title="Aura Research Search Adapter", docs_url=None, redoc_url=None)
_enabled = os.getenv("AURA_RESEARCH_SEARCH_ENABLED", "false").lower() == "true"
_stock_url = os.getenv("AURA_SEARXNG_URL", "http://127.0.0.1:8081").rstrip("/")
_search_semaphore = asyncio.Semaphore(8)
_in_flight = 0
_search_tasks: dict[UUID, asyncio.Task[object]] = {}


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"code": "contract_invalid", "retryable": False})


class SearchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deadlineMs: int = Field(ge=100, le=15_000)
    maxCandidates: int = Field(ge=1, le=20)


class SearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^search_request$")
    query: str = Field(min_length=1, max_length=2048)
    language: str = Field(min_length=2, max_length=35)
    domainAllowlist: list[str] = Field(default_factory=list, max_length=32)
    limits: SearchLimits

    @model_validator(mode="after")
    def validate_domains(self) -> SearchPayload:
        if len(set(self.domainAllowlist)) != len(self.domainAllowlist):
            raise ValueError("duplicate_domain")
        return self


class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    algorithm: str = Field(pattern="^Ed25519$")
    keyId: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=86, max_length=86)


class SearchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: str = Field(pattern="^research-provider\\.v1$")
    envelopeType: str = Field(pattern="^request$")
    requestId: UUID
    replayId: UUID
    audience: Literal["searxng"]
    method: Literal["search"]
    issuedAt: datetime
    expiresAt: datetime
    ttlMs: int = Field(ge=100, le=60_000)
    payload: SearchPayload
    signature: Signature

    @model_validator(mode="after")
    def validate_window(self) -> SearchEnvelope:
        if (
            self.expiresAt <= self.issuedAt
            or self.expiresAt - self.issuedAt != timedelta(milliseconds=self.ttlMs)
            or self.expiresAt - self.issuedAt > timedelta(seconds=60)
        ):
            raise ValueError("invalid_window")
        return self


class ControlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["limits_request", "health_request"]


class ControlEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: Literal["research-provider.v1"]
    envelopeType: Literal["request"]
    requestId: UUID
    replayId: UUID
    audience: Literal["searxng"]
    method: Literal["limits", "health"]
    issuedAt: datetime
    expiresAt: datetime
    ttlMs: int = Field(ge=100, le=60_000)
    payload: ControlPayload
    signature: Signature

    @model_validator(mode="after")
    def validate_method_payload(self) -> ControlEnvelope:
        expected = "limits_request" if self.method == "limits" else "health_request"
        if self.payload.kind != expected:
            raise ValueError("method_payload_mismatch")
        if (
            self.expiresAt <= self.issuedAt
            or self.expiresAt - self.issuedAt != timedelta(milliseconds=self.ttlMs)
            or self.expiresAt - self.issuedAt > timedelta(seconds=60)
        ):
            raise ValueError("invalid_window")
        return self


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str = Field(pattern="^search_result$")
    outcome: str = Field(pattern="^success$")
    candidates: list[dict[str, object]] = Field(max_length=20)


class ProviderError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(
        pattern="^(contract_invalid|signature_invalid|expired|replayed|audience_mismatch|method_unsupported|disabled|draining|provider_unavailable|protocol_error|internal_error)$"
    )
    retryable: bool


class ProviderErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: str = Field(pattern="^research-provider\\.v1$")
    envelopeType: str = Field(pattern="^error$")
    responseId: UUID
    requestId: UUID
    audience: str = Field(pattern="^searxng$")
    method: Literal["search", "limits", "health"]
    producedAt: datetime
    error: ProviderError


_seen: dict[str, float] = {}
_REPLAY_CACHE_CAPACITY = 4096


class RuntimeControl:
    """Authenticated in-process state transition for the adapter runtime."""

    def __init__(self) -> None:
        self.draining = os.getenv("AURA_RESEARCH_SEARCH_DRAINING", "false").lower() == "true"
        self.immediate_stop = (
            os.getenv("AURA_RESEARCH_SEARCH_IMMEDIATE_STOP", "false").lower() == "true"
        )

    def begin_graceful_drain(self) -> None:
        self.draining = True

    def immediate_stop_now(self) -> int:
        self.draining = True
        self.immediate_stop = True
        return self.cancel_in_flight()

    def apply_authenticated(self, *, draining: bool = False, immediate_stop: bool = False) -> int:
        if draining:
            self.begin_graceful_drain()
        if immediate_stop:
            return self.immediate_stop_now()
        return 0

    def cancel_in_flight(self) -> int:
        current = asyncio.current_task()
        canceled = 0
        for task in tuple(_search_tasks.values()):
            if task is not current and not task.done():
                task.cancel()
                canceled += 1
        return canceled


runtime_control = RuntimeControl()


def _draining() -> bool:
    return runtime_control.draining


def _immediate_stop() -> bool:
    return runtime_control.immediate_stop


def _apply_runtime_stop() -> bool:
    if not _immediate_stop():
        return False
    runtime_control.cancel_in_flight()
    return True


def _canonical(envelope: BaseModel) -> bytes:
    value = envelope.model_dump(mode="json")
    value.pop("signature", None)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _verify(envelope: SearchEnvelope | ControlEnvelope) -> None:
    now = time.time()
    for key, expiry in list(_seen.items()):
        if expiry <= now:
            _seen.pop(key, None)
    if (
        envelope.issuedAt.timestamp() > now + 5
        or envelope.expiresAt.timestamp() <= now
        or envelope.expiresAt - envelope.issuedAt != timedelta(milliseconds=envelope.ttlMs)
        or envelope.expiresAt - envelope.issuedAt > timedelta(seconds=60)
    ):
        raise ValueError("expired")
    if str(envelope.replayId) in _seen:
        raise ValueError("replayed")
    key_file = os.getenv("AURA_PROVIDER_PUBLIC_KEY_FILE", "")
    if key_file:
        try:
            from pathlib import Path

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            raw = Path(key_file).read_bytes().strip()
            try:
                raw = base64.urlsafe_b64decode(raw + b"===")
            except Exception:
                raw = base64.b64decode(raw, validate=True)
            signature = base64.urlsafe_b64decode(envelope.signature.value + "==")
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, _canonical(envelope))
        except Exception as exc:
            raise ValueError("signature_invalid") from exc
    else:
        raise ValueError("signature_invalid")
    if len(_seen) >= _REPLAY_CACHE_CAPACITY:
        raise ValueError("provider_unavailable")
    _seen[str(envelope.replayId)] = envelope.expiresAt.timestamp()


def _error(
    envelope: SearchEnvelope | ControlEnvelope, code: str, retryable: bool, http_status: int
) -> JSONResponse:
    payload = ProviderErrorEnvelope(
        schemaVersion="research-provider.v1",
        envelopeType="error",
        responseId=uuid4(),
        requestId=envelope.requestId,
        audience="searxng",
        method=envelope.method,
        producedAt=datetime.now(UTC),
        error=ProviderError(code=code, retryable=retryable),
    )
    return JSONResponse(status_code=http_status, content=payload.model_dump(mode="json"))


def _verification_error(
    envelope: SearchEnvelope | ControlEnvelope, exc: ValueError
) -> JSONResponse:
    """Map replay-capacity failures to retryable provider unavailability."""
    code = str(exc)
    retryable = code == "provider_unavailable"
    if code not in {"expired", "replayed", "signature_invalid", "provider_unavailable"}:
        code = "protocol_error"
    return _error(envelope, code, retryable, 503 if retryable else 401)


def _result(envelope: SearchEnvelope | ControlEnvelope, result: dict[str, object]) -> JSONResponse:
    payload = {
        "schemaVersion": "research-provider.v1",
        "envelopeType": "result",
        "responseId": str(uuid4()),
        "requestId": str(envelope.requestId),
        "audience": "searxng",
        "method": envelope.method,
        "producedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": result,
    }
    return JSONResponse(content=payload)


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


async def _backend_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
            response = await client.get(f"{_stock_url}/healthz")
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def _candidate(value: object, rank: int) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    item = cast(dict[str, object], value)
    raw_url = item.get("url")
    if not isinstance(raw_url, str) or len(raw_url) > 4096:
        return None
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        return None
    label = item.get("title") or item.get("url")
    if not isinstance(label, str) or not label:
        return None
    result: dict[str, object] = {"rank": rank, "url": str(HttpUrl(raw_url)), "label": label[:512]}
    publisher = item.get("engine")
    if isinstance(publisher, str) and publisher:
        result["publisher"] = publisher[:256]
    return result


@app.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    immediate = _apply_runtime_stop()
    draining = _draining() or immediate
    configured = _configured()
    policy = _policy_ready()
    backend = (
        await _backend_ready()
        if _enabled and not draining and configured and policy
        else False
    )
    capacity = _in_flight < 8
    accepting = _enabled and not draining and configured and policy and backend and capacity
    if not accepting:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "kind": "health_result",
        "status": (
            "ready"
            if accepting
            else "degraded"
            if _enabled and configured and policy and backend and (draining or not capacity)
            else "not_ready"
        ),
        "enabled": _enabled,
        "accepting": accepting,
        "inFlight": _in_flight,
        "contractVersion": "research-provider.v1",
        "degradation": (
            None
            if accepting
            else "disabled"
            if not _enabled
            else "draining"
            if draining
            else "configuration_invalid"
            if not configured or not policy
            else "capacity"
            if not capacity
            else "dependency_unavailable"
        ),
    }


@app.get("/health/live")
async def live() -> dict[str, object]:
    immediate = _apply_runtime_stop()
    draining = _draining() or immediate
    accepting = _enabled and not draining and _configured() and _policy_ready()
    return {
        "kind": "health_result",
        "status": "live",
        "enabled": _enabled,
        "accepting": accepting,
        "inFlight": _in_flight,
        "contractVersion": "research-provider.v1",
        "degradation": "disabled" if not _enabled else "draining" if draining else None,
    }


async def limits() -> dict[str, object]:
    return {
        "kind": "limits_result",
        "limits": {
            "service": "searxng",
            "deadlineMs": 15_000,
            "maxCandidates": 20,
            "maxConcurrency": 8,
            "cpuMillicores": 2_000,
            "memoryBytes": 1024 * 1024 * 1024,
            "pids": 128,
            "temporaryStorageBytes": 128 * 1024 * 1024,
        },
    }


@app.post("/internal/v1/limits", response_model=None)
async def signed_limits(envelope: ControlEnvelope) -> JSONResponse:
    if envelope.method != "limits":
        return _error(envelope, "method_unsupported", False, 400)
    try:
        _verify(envelope)
    except ValueError as exc:
        return _verification_error(envelope, exc)
    if not _enabled:
        return _error(envelope, "disabled", False, 503)
    value = await limits()
    return _result(envelope, value)


@app.post("/internal/v1/health", response_model=None)
async def signed_health(envelope: ControlEnvelope) -> JSONResponse:
    if envelope.method != "health":
        return _error(envelope, "method_unsupported", False, 400)
    try:
        _verify(envelope)
    except ValueError as exc:
        return _verification_error(envelope, exc)
    if not _enabled:
        return _error(envelope, "disabled", False, 503)
    response = Response()
    value = await ready(response)
    if response.status_code >= 500:
        return _error(envelope, "provider_unavailable", True, response.status_code)
    return _result(envelope, value)


@app.post("/internal/v1/search")
async def search(envelope: SearchEnvelope) -> JSONResponse:
    try:
        _verify(envelope)
    except ValueError as exc:
        return _verification_error(envelope, exc)
    if not _enabled:
        return _error(envelope, "disabled", False, 503)
    if _apply_runtime_stop() or _draining():
        return _error(envelope, "draining", True, 503)
    try:
        timeout = min(envelope.payload.limits.deadlineMs / 1000, 15.0)
        global _in_flight
        task = asyncio.current_task()
        if task is not None:
            _search_tasks[envelope.requestId] = task
        entered = False
        try:
            async with _search_semaphore:
                _in_flight += 1
                entered = True
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout), follow_redirects=False
                ) as client:
                    response = await client.post(
                        f"{_stock_url}/search",
                        data={
                            "q": envelope.payload.query,
                            "format": "json",
                            "language": envelope.payload.language,
                        },
                        headers={"content-type": "application/x-www-form-urlencoded"},
                    )
                    response.raise_for_status()
                    body = response.json()
        finally:
            if entered:
                _in_flight -= 1
            if task is not None:
                _search_tasks.pop(envelope.requestId, None)
        raw: list[object] = []
        if isinstance(body, dict):
            body_map = cast(dict[str, object], body)
            if isinstance(body_map.get("results"), list):
                raw = cast(list[object], body_map["results"])
        candidates = [
            candidate
            for index, item in enumerate(raw, 1)
            if (candidate := _candidate(item, index)) is not None
        ]
        candidates = candidates[: envelope.payload.limits.maxCandidates]
    except httpx.HTTPError, ValueError, TypeError, TimeoutError:
        return _error(envelope, "provider_unavailable", True, 503)
    return _result(
        envelope,
        {"kind": "search_result", "outcome": "success", "candidates": candidates},
    )
