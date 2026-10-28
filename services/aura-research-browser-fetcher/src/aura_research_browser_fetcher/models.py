"""Pydantic view of the closed browser-provider v1 transport."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    algorithm: Literal["Ed25519"]
    keyId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    value: str = Field(min_length=86, max_length=86, pattern=r"^[A-Za-z0-9_-]{86}$")


class BrowserFetchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deadlineMs: int = Field(ge=100, le=45_000)
    maxRedirects: int = Field(ge=0, le=5)
    maxResponseBytes: int = Field(ge=1, le=8 * 1024 * 1024)
    maxExtractedUtf8Bytes: int = Field(ge=1, le=262_144)


class BrowserFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["browser_fetch_request"]
    # Preserve the exact URL spelling in the signed payload; network policy
    # performs canonical public-HTTPS validation at the point of connection.
    targetUrl: str = Field(min_length=9, max_length=4096)
    admissionKind: Literal["search_candidate", "owner_seed"]
    justification: Literal["javascript_required", "static_inadequate_extraction"]
    limits: BrowserFetchLimits

    @field_validator("targetUrl")
    @classmethod
    def target_is_public_https(cls, value: str) -> str:
        parsed = HttpUrl(value)
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username:
            raise ValueError("target URL must use public HTTPS")
        return value


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cancel_request"]
    targetRequestId: UUID
    reason: Literal["owner_cancel", "operator_stop", "deadline_expired"]


class LimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["limits_request"]


class HealthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["health_request"]


class ProviderRequestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: Literal["research-provider.v1"]
    envelopeType: Literal["request"]
    requestId: UUID
    replayId: UUID
    audience: Literal["browser_fetcher"]
    method: Literal["fetch", "cancel", "limits", "health"]
    issuedAt: datetime
    expiresAt: datetime
    ttlMs: int = Field(ge=100, le=60_000)
    payload: BrowserFetchRequest | CancelRequest | LimitsRequest | HealthRequest
    signature: Signature

    @model_validator(mode="after")
    def validate_method_payload(self) -> ProviderRequestEnvelope:
        expected = {
            "fetch": BrowserFetchRequest,
            "cancel": CancelRequest,
            "limits": LimitsRequest,
            "health": HealthRequest,
        }[self.method]
        if type(self.payload) is not expected:
            raise ValueError("method_payload_mismatch")
        if (
            self.expiresAt <= self.issuedAt
            or self.expiresAt - self.issuedAt != timedelta(milliseconds=self.ttlMs)
            or self.expiresAt - self.issuedAt > timedelta(seconds=60)
        ):
            raise ValueError("invalid_window")
        return self


class FetchedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    finalUrl: HttpUrl
    redirectCount: int = Field(ge=0, le=5)
    declaredMediaType: Literal["text/html", "application/xhtml+xml", "text/plain"]
    detectedMediaType: Literal["text/html", "application/xhtml+xml", "text/plain"]
    retrievedAt: datetime
    transferredBytes: int = Field(ge=0, le=8 * 1024 * 1024)
    extractedUtf8Bytes: int = Field(ge=1, le=262_144)
    contentSha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    extractionMethod: Literal["playwright_dom"]
    label: str | None = Field(default=None, min_length=1, max_length=512)
    publisher: str | None = Field(default=None, min_length=1, max_length=256)
    freshnessAt: datetime | None = None
    extractedText: str = Field(min_length=1, max_length=262_144)


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["fetch_result"]
    outcome: Literal["success"]
    evidence: FetchedEvidence


class FetchInadequateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["fetch_result"]
    outcome: Literal["inadequate_extraction"]
    finalUrl: HttpUrl
    redirectCount: int = Field(ge=0, le=5)
    declaredMediaType: Literal["text/html", "application/xhtml+xml", "text/plain"]
    detectedMediaType: Literal["text/html", "application/xhtml+xml", "text/plain"]
    transferredBytes: int = Field(ge=0, le=8 * 1024 * 1024)


class CancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cancel_result"]
    outcome: Literal["accepted", "already_settled", "not_found"]


class BrowserProviderLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    service: Literal["browser_fetcher"]
    deadlineMs: int = Field(ge=1, le=45_000)
    maxRedirects: int = Field(ge=0, le=5)
    maxResponseBytes: int = Field(ge=1, le=8 * 1024 * 1024)
    maxExtractedUtf8Bytes: int = Field(ge=1, le=262_144)
    maxConcurrency: int = Field(ge=1, le=2)
    cpuMillicores: int = Field(ge=1, le=4000)
    memoryBytes: int = Field(ge=1, le=2 * 1024 * 1024 * 1024)
    pids: int = Field(ge=1, le=256)
    temporaryStorageBytes: int = Field(ge=1, le=256 * 1024 * 1024)


class LimitsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["limits_result"]
    limits: BrowserProviderLimits


class HealthTransportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["health_result"]
    status: Literal["live", "ready", "degraded", "not_ready"]
    enabled: bool
    accepting: bool
    inFlight: int = Field(ge=0, le=8)
    contractVersion: Literal["research-provider.v1"]
    degradation: (
        Literal[
            "disabled", "draining", "capacity", "dependency_unavailable", "configuration_invalid"
        ]
        | None
    ) = None


class ProviderResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: Literal["research-provider.v1"]
    envelopeType: Literal["result"]
    responseId: UUID
    requestId: UUID
    audience: Literal["browser_fetcher"]
    method: Literal["fetch", "cancel", "limits", "health"]
    producedAt: datetime
    result: (
        FetchResult | FetchInadequateResult | CancelResult | LimitsResult | HealthTransportResult
    )

    @model_validator(mode="after")
    def validate_method_result(self) -> ProviderResultEnvelope:
        expected = {
            "fetch": (FetchResult, FetchInadequateResult),
            "cancel": (CancelResult,),
            "limits": (LimitsResult,),
            "health": (HealthTransportResult,),
        }[self.method]
        if type(self.result) not in expected:
            raise ValueError("method_result_mismatch")
        return self


class ProviderError(BaseModel):
    """Finite, content-free provider failure metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal[
        "contract_invalid",
        "signature_invalid",
        "expired",
        "replayed",
        "audience_mismatch",
        "method_unsupported",
        "disabled",
        "draining",
        "destination_blocked",
        "redirect_blocked",
        "type_rejected",
        "size_exceeded",
        "deadline_exceeded",
        "resource_exhausted",
        "provider_unavailable",
        "canceled",
        "not_found",
        "extraction_failed",
        "protocol_error",
        "internal_error",
    ]
    retryable: bool


class ProviderErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: Literal["research-provider.v1"]
    envelopeType: Literal["error"]
    responseId: UUID
    requestId: UUID
    audience: Literal["browser_fetcher"]
    method: Literal["fetch", "cancel", "limits", "health"]
    producedAt: datetime
    error: ProviderError


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    outcome: Literal[
        "completed",
        "inadequate_extraction",
        "destination_blocked",
        "redirect_blocked",
        "unsupported_media_type",
        "limit_rejected",
        "timeout",
        "provider_unavailable",
        "parser_failed",
        "canceled",
    ]
    final_url: HttpUrl | None = None
    declared_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] | None = None
    detected_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] | None = None
    extraction_method: Literal["playwright_text"] | None = None
    text: str | None = Field(default=None, max_length=262_144)
    content_sha256: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    bytes_transferred: int = Field(default=0, ge=0, le=8 * 1024 * 1024)
    redirects: int = Field(default=0, ge=0, le=5)
    error_class: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")


class HealthResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["health_result"]
    status: Literal["live", "ready", "degraded", "not_ready"]
    enabled: bool
    accepting: bool
    inFlight: int = Field(ge=0, le=8)
    contractVersion: Literal["research-provider.v1"]
    degradation: (
        Literal[
            "disabled", "draining", "capacity", "dependency_unavailable", "configuration_invalid"
        ]
        | None
    ) = None
