"""Closed DTOs matching the versioned research-provider.v1 transport."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

MAX_QUERY = 2_048
MAX_URL = 4_096
MAX_EXCERPT = 256 * 1024
MAX_CANDIDATES = 20
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"


class ProviderModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, str_strip_whitespace=False
    )


class ProviderErrorCode(StrEnum):
    CONTRACT_INVALID = "contract_invalid"
    SIGNATURE_INVALID = "signature_invalid"
    REPLAYED = "replayed"
    EXPIRED = "expired"
    AUDIENCE_MISMATCH = "audience_mismatch"
    METHOD_UNSUPPORTED = "method_unsupported"
    DISABLED = "disabled"
    DRAINING = "draining"
    DESTINATION_BLOCKED = "destination_blocked"
    REDIRECT_BLOCKED = "redirect_blocked"
    TYPE_REJECTED = "type_rejected"
    SIZE_EXCEEDED = "size_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CANCELED = "canceled"
    NOT_FOUND = "not_found"
    EXTRACTION_FAILED = "extraction_failed"
    PROTOCOL_ERROR = "protocol_error"
    INTERNAL_ERROR = "internal_error"


class BoundedProviderError(ProviderModel):
    code: ProviderErrorCode
    retryable: bool = False


class Signature(ProviderModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Annotated[
        str,
        Field(
            alias="keyId",
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        ),
    ]
    value: Annotated[
        str,
        Field(
            min_length=86,
            max_length=86,
            pattern=r"^[A-Za-z0-9_-]{86}$",
        ),
    ]


class SearchRequestLimits(ProviderModel):
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=15_000)] = 15_000
    max_candidates: Annotated[int, Field(alias="maxCandidates", ge=1, le=MAX_CANDIDATES)] = 20


class FetchRequestLimits(ProviderModel):
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=45_000)]
    max_redirects: Annotated[int, Field(alias="maxRedirects", ge=0, le=5)] = 5
    max_bytes: Annotated[int, Field(alias="maxTransferredBytes", ge=1, le=8 * 1024 * 1024)] = (
        8 * 1024 * 1024
    )
    max_decompressed_bytes: Annotated[
        int, Field(alias="maxDecompressedBytes", ge=1, le=8 * 1024 * 1024)
    ] = 8 * 1024 * 1024
    max_text_bytes: Annotated[int, Field(alias="maxExtractedUtf8Bytes", ge=1, le=MAX_EXCERPT)] = (
        MAX_EXCERPT
    )


class BrowserFetchRequestLimits(ProviderModel):
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=45_000)] = 45_000
    max_redirects: Annotated[int, Field(alias="maxRedirects", ge=0, le=5)] = 5
    max_bytes: Annotated[int, Field(alias="maxResponseBytes", ge=1, le=8 * 1024 * 1024)] = (
        8 * 1024 * 1024
    )
    max_text_bytes: Annotated[int, Field(alias="maxExtractedUtf8Bytes", ge=1, le=MAX_EXCERPT)] = (
        MAX_EXCERPT
    )


class SearchRequest(ProviderModel):
    kind: Literal["search_request"] = "search_request"
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY)]
    language: Annotated[
        str,
        Field(
            min_length=2,
            max_length=35,
            pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
        ),
    ] = "en"
    domain_allowlist: Annotated[tuple[str, ...], Field(alias="domainAllowlist", max_length=32)] = ()
    limits: SearchRequestLimits = SearchRequestLimits()
    request_id: UUID | None = Field(default=None, exclude=True)

    @field_validator("query")
    @classmethod
    def query_is_text(cls, value: str) -> str:
        if "\x00" in value or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("query contains control characters")
        return value


class SearchCandidate(ProviderModel):
    rank: Annotated[int, Field(ge=1, le=MAX_CANDIDATES)]
    url: Annotated[str, Field(min_length=1, max_length=MAX_URL)]
    label: Annotated[str, Field(min_length=1, max_length=512)]
    publisher: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @field_validator("url")
    @classmethod
    def url_is_public_scheme(cls, value: str) -> str:
        parsed = HttpUrl(value)
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username:
            raise ValueError("candidate URL must use public HTTPS")
        return value


class SearchResult(ProviderModel):
    kind: Literal["search_result"] = "search_result"
    outcome: Literal["success"] = "success"
    candidates: tuple[SearchCandidate, ...] = ()


class StaticFetchRequest(ProviderModel):
    kind: Literal["static_fetch_request"] = "static_fetch_request"
    target_url: Annotated[str, Field(alias="targetUrl", min_length=9, max_length=MAX_URL)]
    admission_kind: Literal["search_candidate", "owner_seed"] = Field(alias="admissionKind")
    limits: FetchRequestLimits = Field(
        default_factory=lambda: FetchRequestLimits(deadline_ms=20_000)
    )
    request_id: UUID | None = Field(default=None, exclude=True)


class BrowserFetchRequest(ProviderModel):
    kind: Literal["browser_fetch_request"] = "browser_fetch_request"
    target_url: Annotated[str, Field(alias="targetUrl", min_length=9, max_length=MAX_URL)]
    admission_kind: Literal["search_candidate", "owner_seed"] = Field(alias="admissionKind")
    justification: Literal["javascript_required", "static_inadequate_extraction"]
    limits: BrowserFetchRequestLimits = Field(default_factory=BrowserFetchRequestLimits)
    request_id: UUID | None = Field(default=None, exclude=True)


class FetchEvidence(ProviderModel):
    final_url: Annotated[str, Field(alias="finalUrl", min_length=9, max_length=MAX_URL)]
    redirect_count: Annotated[int, Field(alias="redirectCount", ge=0, le=5)]
    declared_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] = Field(
        alias="declaredMediaType"
    )
    detected_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] = Field(
        alias="detectedMediaType"
    )
    retrieved_at: Annotated[
        str,
        Field(alias="retrievedAt", pattern=TIMESTAMP_PATTERN),
    ]
    transferred_bytes: Annotated[int, Field(alias="transferredBytes", ge=0, le=8 * 1024 * 1024)]
    extracted_utf8_bytes: Annotated[int, Field(alias="extractedUtf8Bytes", ge=1, le=MAX_EXCERPT)]
    content_sha256: Annotated[str, Field(alias="contentSha256", pattern=r"^[0-9a-f]{64}$")]
    extraction_method: Literal["trafilatura", "plain_text", "playwright_dom"] = Field(
        alias="extractionMethod"
    )
    extracted_text: Annotated[
        str, Field(alias="extractedText", min_length=1, max_length=MAX_EXCERPT)
    ]
    label: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    publisher: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    freshness_at: str | None = Field(default=None, alias="freshnessAt", pattern=TIMESTAMP_PATTERN)


class FetchEvidenceResult(ProviderModel):
    kind: Literal["fetch_result"] = "fetch_result"
    outcome: Literal["success"] = "success"
    evidence: FetchEvidence


class FetchInadequateResult(ProviderModel):
    kind: Literal["fetch_result"] = "fetch_result"
    outcome: Literal["inadequate_extraction"]
    final_url: Annotated[str, Field(alias="finalUrl", min_length=9, max_length=MAX_URL)]
    redirect_count: Annotated[int, Field(alias="redirectCount", ge=0, le=5)]
    declared_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] = Field(
        alias="declaredMediaType"
    )
    detected_media_type: Literal["text/html", "application/xhtml+xml", "text/plain"] = Field(
        alias="detectedMediaType"
    )
    transferred_bytes: Annotated[int, Field(alias="transferredBytes", ge=0, le=8 * 1024 * 1024)]


class CancelResult(ProviderModel):
    kind: Literal["cancel_result"] = "cancel_result"
    outcome: Literal["accepted", "already_settled", "not_found"]


class SearchProviderLimits(ProviderModel):
    service: Literal["searxng"]
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=15_000)]
    max_candidates: Annotated[int, Field(alias="maxCandidates", ge=1, le=20)]
    max_concurrency: Annotated[int, Field(alias="maxConcurrency", ge=1, le=8)]
    cpu_millicores: Annotated[int, Field(alias="cpuMillicores", ge=1, le=2_000)]
    memory_bytes: Annotated[int, Field(alias="memoryBytes", ge=1, le=1_073_741_824)]
    pids: Annotated[int, Field(ge=1, le=128)]
    temporary_storage_bytes: Annotated[
        int, Field(alias="temporaryStorageBytes", ge=1, le=134_217_728)
    ]


class StaticProviderLimits(ProviderModel):
    service: Literal["static_fetcher"]
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=20_000)]
    max_redirects: Annotated[int, Field(alias="maxRedirects", ge=0, le=5)]
    max_transferred_bytes: Annotated[
        int, Field(alias="maxTransferredBytes", ge=1, le=8 * 1024 * 1024)
    ]
    max_decompressed_bytes: Annotated[
        int, Field(alias="maxDecompressedBytes", ge=1, le=8 * 1024 * 1024)
    ]
    max_extracted_utf8_bytes: Annotated[
        int, Field(alias="maxExtractedUtf8Bytes", ge=1, le=MAX_EXCERPT)
    ]
    max_concurrency: Annotated[int, Field(alias="maxConcurrency", ge=1, le=4)]
    cpu_millicores: Annotated[int, Field(alias="cpuMillicores", ge=1, le=2_000)]
    memory_bytes: Annotated[int, Field(alias="memoryBytes", ge=1, le=1_073_741_824)]
    pids: Annotated[int, Field(ge=1, le=64)]
    temporary_storage_bytes: Annotated[
        int, Field(alias="temporaryStorageBytes", ge=1, le=67_108_864)
    ]


class BrowserProviderLimits(ProviderModel):
    service: Literal["browser_fetcher"]
    deadline_ms: Annotated[int, Field(alias="deadlineMs", ge=100, le=45_000)]
    max_redirects: Annotated[int, Field(alias="maxRedirects", ge=0, le=5)]
    max_response_bytes: Annotated[int, Field(alias="maxResponseBytes", ge=1, le=8 * 1024 * 1024)]
    max_extracted_utf8_bytes: Annotated[
        int, Field(alias="maxExtractedUtf8Bytes", ge=1, le=MAX_EXCERPT)
    ]
    max_concurrency: Annotated[int, Field(alias="maxConcurrency", ge=1, le=2)]
    cpu_millicores: Annotated[int, Field(alias="cpuMillicores", ge=1, le=4_000)]
    memory_bytes: Annotated[int, Field(alias="memoryBytes", ge=1, le=2_147_483_648)]
    pids: Annotated[int, Field(ge=1, le=256)]
    temporary_storage_bytes: Annotated[
        int, Field(alias="temporaryStorageBytes", ge=1, le=268_435_456)
    ]


class LimitsResult(ProviderModel):
    kind: Literal["limits_result"] = "limits_result"
    limits: SearchProviderLimits | StaticProviderLimits | BrowserProviderLimits


class HealthResult(ProviderModel):
    kind: Literal["health_result"] = "health_result"
    status: Literal["live", "ready", "degraded", "not_ready"]
    enabled: bool
    accepting: bool
    in_flight: Annotated[int, Field(alias="inFlight", ge=0, le=8)] = 0
    contract_version: Literal["research-provider.v1"] = Field(
        default="research-provider.v1", alias="contractVersion"
    )
    degradation: (
        Literal[
            "disabled",
            "draining",
            "capacity",
            "dependency_unavailable",
            "configuration_invalid",
        ]
        | None
    ) = None


class CancelRequest(ProviderModel):
    kind: Literal["cancel_request"] = "cancel_request"
    target_request_id: Annotated[UUID, Field(alias="targetRequestId")]
    reason: Literal["owner_cancel", "operator_stop", "deadline_expired"]


class LimitsRequest(ProviderModel):
    kind: Literal["limits_request"] = "limits_request"


class HealthRequest(ProviderModel):
    kind: Literal["health_request"] = "health_request"


ProviderResult = (
    SearchResult
    | FetchEvidenceResult
    | FetchInadequateResult
    | CancelResult
    | LimitsResult
    | HealthResult
)

ProviderRequestPayload = (
    SearchRequest
    | StaticFetchRequest
    | BrowserFetchRequest
    | CancelRequest
    | LimitsRequest
    | HealthRequest
)


class ProviderResultEnvelope(ProviderModel):
    schema_version: Literal["research-provider.v1"] = Field(
        default="research-provider.v1", alias="schemaVersion"
    )
    envelope_type: Literal["result"] = Field(default="result", alias="envelopeType")
    response_id: UUID = Field(alias="responseId")
    request_id: UUID = Field(alias="requestId")
    audience: Literal["searxng", "static_fetcher", "browser_fetcher"]
    method: Literal["search", "fetch", "cancel", "limits", "health"]
    produced_at: str = Field(alias="producedAt", pattern=TIMESTAMP_PATTERN)
    result: ProviderResult

    @model_validator(mode="after")
    def validate_method_result_binding(self) -> ProviderResultEnvelope:
        parsed = self
        if parsed.method == "search" and (
            parsed.audience != "searxng" or not isinstance(parsed.result, SearchResult)
        ):
            raise ValueError("search result does not match audience and method")
        if parsed.method == "fetch" and (
            parsed.audience not in {"static_fetcher", "browser_fetcher"}
            or not isinstance(parsed.result, (FetchEvidenceResult, FetchInadequateResult))
        ):
            raise ValueError("fetch result does not match audience and method")
        if parsed.method == "cancel" and (
            parsed.audience not in {"static_fetcher", "browser_fetcher"}
            or not isinstance(parsed.result, CancelResult)
        ):
            raise ValueError("cancel result does not match audience and method")
        if parsed.method == "limits" and not isinstance(parsed.result, LimitsResult):
            raise ValueError("limits result does not match method")
        if parsed.method == "limits":
            expected_service = {
                "searxng": "searxng",
                "static_fetcher": "static_fetcher",
                "browser_fetcher": "browser_fetcher",
            }[parsed.audience]
            if parsed.result.limits.service != expected_service:  # type: ignore[union-attr]
                raise ValueError("limits result does not match audience")
        if parsed.method == "health" and not isinstance(parsed.result, HealthResult):
            raise ValueError("health result does not match method")
        return parsed


class ProviderErrorEnvelope(ProviderModel):
    schema_version: Literal["research-provider.v1"] = Field(
        default="research-provider.v1", alias="schemaVersion"
    )
    envelope_type: Literal["error"] = Field(default="error", alias="envelopeType")
    response_id: UUID = Field(alias="responseId")
    request_id: UUID = Field(alias="requestId")
    audience: Literal["searxng", "static_fetcher", "browser_fetcher"]
    method: Literal["search", "fetch", "cancel", "limits", "health"]
    produced_at: str = Field(alias="producedAt", pattern=TIMESTAMP_PATTERN)
    error: BoundedProviderError

    @model_validator(mode="after")
    def validate_method_audience_binding(self) -> ProviderErrorEnvelope:
        if self.method == "search" and self.audience != "searxng":
            raise ValueError("search error does not match audience")
        if self.method in {"fetch", "cancel"} and self.audience not in {
            "static_fetcher",
            "browser_fetcher",
        }:
            raise ValueError("fetch/cancel error does not match audience")
        return self
