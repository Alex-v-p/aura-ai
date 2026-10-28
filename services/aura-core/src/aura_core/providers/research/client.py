"""HTTP ports for the isolated governed research providers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel

from .contracts import (
    BrowserFetchRequest,
    CancelResult,
    FetchEvidenceResult,
    FetchInadequateResult,
    HealthResult,
    LimitsResult,
    ProviderErrorCode,
    ProviderErrorEnvelope,
    ProviderResultEnvelope,
    SearchRequest,
    SearchResult,
    StaticFetchRequest,
)
from .signing import EnvelopeSigner


class ProviderClientError(RuntimeError):
    """Bounded provider error; no response body or target is retained."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ResearchProviderUnavailable(ProviderClientError):
    def __init__(self, code: str = "provider_unavailable") -> None:
        super().__init__(code, retryable=True)


class ProviderMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ResearchProviderHealth:
    provider: str
    state: str
    capacity_available: bool
    contract_version: str = "v1"


@dataclass(frozen=True, slots=True)
class ResearchTraceContext:
    """An already-admitted trace context for one provider operation.

    Provider clients are reusable and therefore never retain trace state.  A
    caller may pass this immutable context to an individual search or fetch.
    Invalid context is rejected at the boundary instead of silently creating a
    second, unrelated trace.
    """

    trace_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        if not _valid_trace_id(self.trace_id):
            raise ValueError("invalid trace context")
        if self.parent_span_id is not None and not _valid_span_id(self.parent_span_id):
            raise ValueError("invalid trace context")


class _ProviderClient:
    _provider = "provider"
    _component = "aura.research.provider@1"
    _request_version = "research-provider.v1"
    _audience = "research-provider"
    _metric_prefix = "research_provider"
    _span_operation = ""
    _dependency = "none"

    def __init__(
        self,
        endpoint: str,
        signer: EnvelopeSigner,
        *,
        enabled: bool = False,
        timeout_seconds: float = 45.0,
        telemetry: ProviderMetrics | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._signer = signer
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._telemetry = telemetry
        self._http = http_client

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def health(self) -> ResearchProviderHealth:
        if not self.enabled:
            return ResearchProviderHealth(self._provider, "disabled", False)
        request_id = uuid4()
        try:
            response = await self._request(
                "health",
                "/internal/v1/health",
                {"kind": "health_request"},
                timeout=5.0,
                request_id=request_id,
            )
            payload = cast(
                HealthResult,
                self._parse(response, HealthResult, request_id=request_id, method="health"),
            )
            state = "ready" if payload.enabled and payload.accepting else payload.status
            return ResearchProviderHealth(
                self._provider, state, payload.enabled and payload.accepting
            )
        except ProviderClientError:
            # Readiness is truthful: transport, parse, and contract errors are
            # all unavailable rather than an optimistic "ok" state.
            return ResearchProviderHealth(self._provider, "unavailable", False)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout: float,
        request_id: UUID | None = None,
        trace_context: ResearchTraceContext | None = None,
    ) -> httpx.Response:
        if not self.enabled:
            raise ResearchProviderUnavailable("provider_disabled")
        envelope = None
        if payload is not None:
            envelope = self._signer.sign(
                version=self._request_version,
                request_id=request_id or uuid4(),
                audience=self._audience,
                method=method,
                payload=payload,
                limits={},
                ttl_seconds=min(max(timeout, 0.1), 60.0),
            )
        client = self._http
        owned = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        try:
            # Provider operations are always HTTP POST.  The signed method is
            # the protocol operation and is not overloaded into an HTTP verb.
            headers = {"accept": "application/json", "content-type": "application/json"}
            if trace_context is not None:
                parent_span_id = trace_context.parent_span_id or uuid4().hex[:16]
                headers["traceparent"] = (
                    f"00-{trace_context.trace_id}-{parent_span_id}-01"
                )
            response = await client.post(
                f"{self.endpoint}{path}",
                json=envelope.model_dump(mode="json", by_alias=True) if envelope else None,
                headers=headers,
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
            )
            if response.status_code >= 400 and self._is_correlated_error(
                response, request_id=request_id, method=method
            ):
                return response
            if response.status_code >= 500:
                raise ResearchProviderUnavailable()
            if response.status_code in {401, 403}:
                raise ProviderClientError("signature_invalid")
            if response.status_code == 409:
                raise ProviderClientError("replayed")
            if response.status_code >= 400:
                raise ProviderClientError("protocol_error")
            return response
        except ProviderClientError:
            raise
        except TimeoutError, httpx.TimeoutException:
            raise ProviderClientError("timeout", retryable=True) from None
        except httpx.HTTPError, OSError:
            raise ResearchProviderUnavailable() from None
        finally:
            if owned:
                await client.aclose()

    def _parse(
        self,
        response: httpx.Response,
        model: type[BaseModel] | None,
        *,
        request_id: UUID,
        method: str,
    ) -> Any:
        try:
            content = response.content
            if len(content) > 512 * 1024:
                raise ProviderClientError("size_exceeded")
            decoded: object = json.loads(content)
            if not isinstance(decoded, dict):
                raise ProviderClientError("protocol_error")
            decoded = cast(dict[str, Any], decoded)
            if decoded.get("envelopeType") == "error":
                envelope = ProviderErrorEnvelope.model_validate(decoded)
                self._check_correlation(
                    envelope.request_id, envelope.audience, envelope.method, request_id, method
                )
                raise ProviderClientError(
                    envelope.error.code.value, retryable=envelope.error.retryable
                )
            envelope = ProviderResultEnvelope.model_validate(decoded)
            self._check_correlation(
                envelope.request_id, envelope.audience, envelope.method, request_id, method
            )
            if model is None:
                return envelope.result
            return model.model_validate(envelope.result)
        except ProviderClientError:
            raise
        except ValueError, TypeError:
            raise ProviderClientError("protocol_error") from None

    def _check_correlation(
        self,
        response_request_id: UUID,
        response_audience: str,
        response_method: str,
        request_id: UUID,
        method: str,
    ) -> None:
        if (
            response_request_id != request_id
            or response_audience != self._audience
            or response_method != method
        ):
            raise ProviderClientError("protocol_error")

    def _observe(
        self,
        started: float,
        outcome: str,
        *,
        error_class: str | None = None,
        candidate_count: int | None = None,
        limit_rejected: bool = False,
        trace_context: ResearchTraceContext | None = None,
    ) -> None:
        metrics = self._telemetry
        if metrics is None or not self._span_operation:
            return
        elapsed_ms = max(0.0, (monotonic() - started) * 1000)
        bounded_outcome = _telemetry_outcome(outcome)
        bounded_error = error_class or _telemetry_error_class(outcome)
        attributes: dict[str, str] = {
            "operation": self._span_operation,
            "outcome": bounded_outcome,
            "dependency": self._dependency,
            "provider": self._provider,
            "degradation": _telemetry_degradation(outcome),
        }
        if bounded_error is not None:
            attributes["error_class"] = bounded_error
        try:
            metrics.increment(self._component, self._metric_prefix + "_outcome_count", **attributes)
            metrics.observe(
                self._component, self._metric_prefix + "_latency_ms", elapsed_ms, **attributes
            )
            if candidate_count is not None and self._provider == "searxng":
                metrics.observe(
                    self._component,
                    "research_search_candidate_count",
                    float(max(0, min(candidate_count, 20))),
                    **attributes,
                )
            if limit_rejected and self._provider in {"static_fetcher", "browser_fetcher"}:
                metrics.increment(
                    self._component, self._metric_prefix + "_limit_rejection_count", **attributes
                )
            record_span = getattr(metrics, "record_span", None)
            # A provider operation without an admitted parent context is a
            # valid metadata observation, but must not invent an isolated
            # trace.  Only emit lineage when the caller supplied a context.
            if callable(record_span) and trace_context is not None:
                record_span(
                    self._component,
                    self._span_operation,
                    elapsed_ms,
                    trace_id=trace_context.trace_id,
                    span_id=uuid4().hex[:16],
                    parent_span_id=trace_context.parent_span_id,
                    dependency=self._dependency,
                    outcome=bounded_outcome,
                    error_class=bounded_error,
                    provider=self._provider,
                    degradation=attributes["degradation"],
                )
        except Exception:
            # Telemetry is never on the provider critical path.
            return

    def _is_correlated_error(
        self, response: httpx.Response, *, request_id: UUID | None, method: str
    ) -> bool:
        if request_id is None or len(response.content) > 512 * 1024:
            return False
        try:
            decoded = json.loads(response.content)
            if not isinstance(decoded, dict):
                return False
            decoded = cast(dict[str, Any], decoded)
            if decoded.get("envelopeType") != "error":
                return False
            envelope = ProviderErrorEnvelope.model_validate(decoded)
            self._check_correlation(
                envelope.request_id, envelope.audience, envelope.method, request_id, method
            )
            return True
        except ValueError, TypeError, ProviderClientError:
            return False


class SearchProvider(_ProviderClient):
    _provider = "searxng"
    _component = "aura.research.search@1"
    _audience = "searxng"
    _metric_prefix = "research_search"
    _span_operation = "research.search"
    _dependency = "searxng_port"
    _path = "/internal/v1/search"

    async def search(
        self,
        request: SearchRequest,
        *,
        trace_context: ResearchTraceContext | None = None,
    ) -> SearchResult:
        _validate_trace_context(trace_context)
        started = monotonic()
        request_id = request.request_id or uuid4()
        try:
            response = await self._request(
                "search",
                self._path,
                request.model_dump(mode="json", by_alias=True),
                timeout=min(self.timeout_seconds, request.limits.deadline_ms / 1000),
                request_id=request_id,
                trace_context=trace_context,
            )
            result = cast(
                SearchResult,
                self._parse(response, SearchResult, request_id=request_id, method="search"),
            )
            self._observe(
                started,
                result.outcome,
                candidate_count=len(result.candidates),
                trace_context=trace_context,
            )
            return result
        except ProviderClientError as exc:
            self._observe(
                started,
                exc.code,
                error_class=_telemetry_error_class(exc.code),
                trace_context=trace_context,
            )
            raise

    async def limits(self) -> LimitsResult:
        request_id = uuid4()
        response = await self._request(
            "limits",
            "/internal/v1/limits",
            {"kind": "limits_request"},
            timeout=5.0,
            request_id=request_id,
        )
        return cast(
            LimitsResult,
            self._parse(response, LimitsResult, request_id=request_id, method="limits"),
        )


class _FetchProvider(_ProviderClient):
    _path = "/internal/v1/fetch"

    async def fetch(
        self,
        request: BrowserFetchRequest | StaticFetchRequest,
        *,
        trace_context: ResearchTraceContext | None = None,
    ) -> FetchEvidenceResult | FetchInadequateResult:
        _validate_trace_context(trace_context)
        started = monotonic()
        request_id = request.request_id or uuid4()
        try:
            response = await self._request(
                "fetch",
                self._path,
                request.model_dump(mode="json", by_alias=True),
                timeout=min(self.timeout_seconds, request.limits.deadline_ms / 1000),
                request_id=request_id,
                trace_context=trace_context,
            )
            result = cast(
                FetchEvidenceResult | FetchInadequateResult,
                self._parse(response, None, request_id=request_id, method="fetch"),
            )
            self._observe(started, result.outcome, trace_context=trace_context)
            return result
        except ProviderClientError as exc:
            self._observe(
                started,
                exc.code,
                error_class=_telemetry_error_class(exc.code),
                limit_rejected=exc.code in {"size_exceeded", "resource_exhausted"},
                trace_context=trace_context,
            )
            raise

    async def cancel(
        self,
        request_id: UUID,
        *,
        reason: Literal["owner_cancel", "operator_stop", "deadline_expired"] = "owner_cancel",
    ) -> bool:
        if reason not in {"owner_cancel", "operator_stop", "deadline_expired"}:
            raise ValueError("unsupported cancellation reason")
        try:
            response = await self._request(
                "cancel",
                "/internal/v1/cancel",
                {
                    "kind": "cancel_request",
                    "targetRequestId": str(request_id),
                    "reason": reason,
                },
                timeout=5.0,
                request_id=request_id,
            )
            result = cast(
                CancelResult,
                self._parse(response, CancelResult, request_id=request_id, method="cancel"),
            )
            return result.outcome != "not_found"
        except ProviderClientError:
            return False

    async def limits(self) -> LimitsResult:
        request_id = uuid4()
        response = await self._request(
            "limits",
            "/internal/v1/limits",
            {"kind": "limits_request"},
            timeout=5.0,
            request_id=request_id,
        )
        return cast(
            LimitsResult,
            self._parse(response, LimitsResult, request_id=request_id, method="limits"),
        )


class StaticFetchProvider(_FetchProvider):
    _provider = "static_fetcher"
    _component = "aura.research.static_fetch@1"
    _audience = "static_fetcher"
    _metric_prefix = "research_static_fetch"
    _span_operation = "research.fetch_static"
    _dependency = "static_fetcher_port"


class BrowserFetchProvider(_FetchProvider):
    _provider = "browser_fetcher"
    _component = "aura.research.browser_fetch@1"
    _audience = "browser_fetcher"
    _metric_prefix = "research_browser_fetch"
    _span_operation = "research.fetch_browser"
    _dependency = "browser_fetcher_port"


SearchProviderClient = SearchProvider
StaticFetchProviderClient = StaticFetchProvider
BrowserFetchProviderClient = BrowserFetchProvider


TelemetryClassification = tuple[str, str | None, str]


# Keep this table exhaustive over the closed transport enum.  The transport
# error code is provider-facing; the three values here are the bounded Core
# observability vocabulary (outcome, error class, degradation).
_TELEMETRY_CLASSIFICATIONS: dict[str, TelemetryClassification] = {
    "contract_invalid": ("rejected", "validation_failed", "none"),
    "signature_invalid": ("rejected", "validation_failed", "none"),
    "replayed": ("rejected", "validation_failed", "none"),
    "expired": ("rejected", "validation_failed", "none"),
    "audience_mismatch": ("rejected", "validation_failed", "none"),
    "method_unsupported": ("rejected", "validation_failed", "none"),
    "disabled": ("rejected", "provider_disabled", "provider_disabled"),
    "draining": ("failed", "provider_unavailable", "draining"),
    "destination_blocked": ("rejected", "destination_blocked", "none"),
    "redirect_blocked": ("rejected", "redirect_blocked", "none"),
    "type_rejected": ("rejected", "type_rejected", "none"),
    "size_exceeded": ("rejected", "size_exceeded", "none"),
    "deadline_exceeded": ("timeout", "timeout", "provider_unavailable"),
    "resource_exhausted": ("rejected", "resource_exhausted", "none"),
    "provider_unavailable": ("failed", "provider_unavailable", "provider_unavailable"),
    "canceled": ("canceled", None, "none"),
    "not_found": ("partial", None, "none"),
    "extraction_failed": ("partial", "validation_failed", "none"),
    "protocol_error": ("rejected", "validation_failed", "none"),
    "internal_error": ("failed", "provider_unavailable", "provider_unavailable"),
    # Internal result values retained for Core-owned callers and tests.
    "success": ("success", None, "none"),
    "accepted": ("success", None, "none"),
    "already_settled": ("success", None, "none"),
    "inadequate_extraction": ("partial", None, "none"),
    "timeout": ("timeout", "timeout", "provider_unavailable"),
    "cancelled": ("canceled", None, "none"),
    "provider_disabled": ("rejected", "provider_disabled", "provider_disabled"),
    "malformed_response": ("rejected", "validation_failed", "none"),
    "provider_rejected": ("rejected", "validation_failed", "none"),
}

# A contract change adding a ProviderErrorCode must update this table in the
# same Core change.  Keeping the assertion at import time prevents a newly
# added wire code from silently becoming provider_unavailable telemetry.
_PROVIDER_ERROR_VALUES = {code.value for code in ProviderErrorCode}
if not _PROVIDER_ERROR_VALUES <= _TELEMETRY_CLASSIFICATIONS.keys():  # pragma: no cover - guard
    raise RuntimeError("unmapped research provider error code")


def telemetry_classification(value: str) -> TelemetryClassification:
    return _TELEMETRY_CLASSIFICATIONS.get(
        value, ("failed", "provider_unavailable", "provider_unavailable")
    )


def _telemetry_outcome(value: str) -> str:
    return telemetry_classification(value)[0]


def _telemetry_error_class(value: str) -> str | None:
    return telemetry_classification(value)[1]


def record_extraction_telemetry(
    metrics: ProviderMetrics,
    *,
    outcome: str,
    elapsed_ms: float,
    error_class: str | None = None,
    trace_context: ResearchTraceContext | None = None,
) -> None:
    """Record the content-policy extraction boundary without source data."""

    _validate_trace_context(trace_context)
    bounded_outcome = _telemetry_outcome(outcome)
    bounded_error = error_class or _telemetry_error_class(outcome)
    attrs: dict[str, str] = {
        "operation": "research.extract",
        "outcome": bounded_outcome,
        "dependency": "content_policy",
        "provider": "none",
        "degradation": _telemetry_degradation(outcome),
    }
    if bounded_error is not None:
        attrs["error_class"] = bounded_error
    try:
        metrics.increment(
            "aura.research.extraction@1", "research_extraction_outcome_count", **attrs
        )
        metrics.observe(
            "aura.research.extraction@1",
            "research_extraction_latency_ms",
            max(0.0, elapsed_ms),
            **attrs,
        )
        if bounded_outcome != "success":
            metrics.increment(
                "aura.research.extraction@1", "research_extraction_rejected_count", **attrs
            )
        record_span = getattr(metrics, "record_span", None)
        if callable(record_span) and trace_context is not None:
            record_span(
                "aura.research.extraction@1",
                "research.extract",
                max(0.0, elapsed_ms),
                trace_id=trace_context.trace_id,
                span_id=uuid4().hex[:16],
                parent_span_id=trace_context.parent_span_id,
                dependency="content_policy",
                outcome=bounded_outcome,
                error_class=bounded_error,
                provider="none",
                degradation=attrs["degradation"],
            )
    except Exception:
        return


def _telemetry_degradation(value: str) -> str:
    return telemetry_classification(value)[2]


def _validate_trace_context(context: ResearchTraceContext | None) -> None:
    if context is None:
        return
    if type(context) is not ResearchTraceContext:
        raise ValueError("invalid trace context")
    if not _valid_trace_id(context.trace_id) or (
        context.parent_span_id is not None and not _valid_span_id(context.parent_span_id)
    ):
        raise ValueError("invalid trace context")


def _valid_trace_id(value: str | None) -> bool:
    return (
        value is not None
        and re.fullmatch(r"[0-9a-f]{32}", value) is not None
        and set(value) != {"0"}
    )


def _valid_span_id(value: str | None) -> bool:
    return (
        value is not None
        and re.fullmatch(r"[0-9a-f]{16}", value) is not None
        and set(value) != {"0"}
    )
