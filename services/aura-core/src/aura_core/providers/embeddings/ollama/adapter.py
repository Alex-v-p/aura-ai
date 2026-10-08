"""Ollama adapter for the provider-neutral embedding port.

This adapter has no chat or structured-inference authority.  It normalizes a
finite vector and model metadata, and keeps provider failures content-free.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

import httpx

from aura_core.runtime.models.ports import (
    EmbeddingResult,
    ProviderTelemetryPort,
    ProviderTraceContext,
)

MAX_INPUT_CHARS = 256 * 1024
MAX_DIMENSION = 16_384


class OllamaEmbeddingUnavailable(RuntimeError):
    """Raised when Ollama cannot produce a valid embedding."""


class OllamaEmbeddingAdapter:
    """Call Ollama's embedding endpoint through :class:`EmbeddingPort`."""

    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 30.0,
        telemetry: ProviderTelemetryPort | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._telemetry = telemetry

    async def embed(
        self,
        model_id: str,
        text: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> EmbeddingResult:
        started = monotonic()
        requested_model_id = model_id
        if not model_id or len(text) > MAX_INPUT_CHARS:
            model_id = ""
            text = ""
            self._record_telemetry(
                started, context, model_id=requested_model_id,
                outcome="error", error_class="validation"
            )
            raise OllamaEmbeddingUnavailable("embedding request is invalid")
        payload = await self._request_embedding(model_id, text)
        if payload is None:
            model_id = ""
            text = ""
            self._record_telemetry(
                started, context, model_id=requested_model_id,
                outcome="error", error_class="provider"
            )
            raise OllamaEmbeddingUnavailable("embedding unavailable")
        parsed = self._parse_embedding(payload)
        if parsed is None:
            payload = None
            model_id = ""
            text = ""
            self._record_telemetry(
                started, context, model_id=requested_model_id,
                outcome="error", error_class="provider"
            )
            raise OllamaEmbeddingUnavailable("embedding response malformed")
        values, response_revision = parsed
        artifact = await self._request_model_artifact(model_id)
        if artifact is None:
            payload = None
            model_id = ""
            text = ""
            self._record_telemetry(
                started, context, model_id=requested_model_id,
                outcome="error", error_class="provider"
            )
            raise OllamaEmbeddingUnavailable("embedding model artifact unavailable")
        model_digest, artifact_revision = artifact
        model_revision = response_revision or artifact_revision
        digest = hashlib.sha256(
            json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._record_telemetry(started, context, model_id=requested_model_id, outcome="ok")
        return EmbeddingResult(
            vector=values,
            model_id=model_id,
            model_revision=model_revision,
            dimension=len(values),
            model_digest=model_digest,
            digest=digest,
        )

    async def is_ready(self, model_id: str | None = None) -> bool:
        payload = await self._request_inventory()
        if payload is None:
            return False
        if model_id is None:
            return True
        models = payload.get("models")
        if not isinstance(models, list):
            return False
        for item in cast(list[object], models):
            if isinstance(item, dict) and cast(dict[str, object], item).get("name") == model_id:
                return True
        return False

    def _record_telemetry(
        self,
        started: float,
        context: ProviderTraceContext | None,
        *,
        model_id: str,
        outcome: str,
        error_class: str | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        trace_id = uuid4().hex
        if context and _valid_trace_id(context.trace_id):
            trace_id = cast(str, context.trace_id)
        span_id = uuid4().hex[:16]
        parent_span_id = context.span_id if context and _valid_span_id(context.span_id) else None
        attrs = _trace_attributes(context)
        if _safe_model_id(model_id):
            attrs["model_id"] = model_id
        run_id = attrs.pop("run_id", None)
        conversation_id = attrs.pop("conversation_id", None)
        duration_ms = max(0.0, (monotonic() - started) * 1000.0)
        try:
            self._telemetry.record_span(
                "aura.runtime.embedding_gateway",
                "embedding.generate",
                duration_ms,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                dependency="embedding_provider",
                outcome=outcome,
                error_class=error_class,
                provider="ollama",
                run_id=run_id,
                conversation_id=conversation_id,
                **attrs,
            )
            if error_class == "provider":
                self._telemetry.increment(
                    "aura.runtime.embedding_gateway",
                    "provider_errors",
                    trace_id=trace_id,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    dependency="embedding_provider",
                    outcome="error",
                    error_class="provider",
                    provider="ollama",
                    **attrs,
                )
        except Exception:
            return

    async def _request_embedding(self, model_id: str, text: str) -> object | None:
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                response = await client.post(
                    "/api/embed", json={"model": model_id, "input": text}
                )
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    async def _request_inventory(self) -> dict[str, object] | None:
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=self._timeout) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                payload: Any = response.json()
            return cast(dict[str, object], payload) if isinstance(payload, dict) else None
        except Exception:
            return None

    async def _request_model_artifact(self, model_id: str) -> tuple[str, str | None] | None:
        """Read one unambiguous artifact identity from Ollama inventory."""

        payload = await self._request_inventory()
        if payload is None or not isinstance(payload.get("models"), list):
            return None
        matches: list[dict[str, object]] = []
        for item in cast(list[object], payload["models"]):
            if isinstance(item, dict) and cast(dict[str, object], item).get("name") == model_id:
                matches.append(cast(dict[str, object], item))
        # Multiple tags with the same name are ambiguous: fail closed instead
        # of selecting a digest that might not have generated the vector.
        if len(matches) != 1:
            return None
        value: object = matches[0].get("digest")
        if value is None and isinstance(matches[0].get("details"), dict):
            value = cast(dict[str, object], matches[0]["details"]).get("digest")
        if not isinstance(value, str):
            return None
        digest = value.strip().lower()
        if digest.startswith("sha256:"):
            digest = digest[7:]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        revision_value = matches[0].get("modified_at")
        revision = (
            revision_value[:255]
            if isinstance(revision_value, str) and revision_value
            else None
        )
        return digest, revision

    @classmethod
    def _parse_embedding(cls, payload: object) -> tuple[tuple[float, ...], str | None] | None:
        try:
            vector = cls._extract_vector(payload)
            values = tuple(float(cast(Any, value)) for value in vector)
        except (TypeError, ValueError, OverflowError):
            return None
        if not values or len(values) > MAX_DIMENSION or not all(
            math.isfinite(value) for value in values
        ):
            return None
        return values, cls._extract_revision(payload)

    @staticmethod
    def _extract_vector(payload: object) -> list[object]:
        if not isinstance(payload, dict):
            raise ValueError
        payload_map = cast(dict[str, object], payload)
        values: object = payload_map.get("embeddings")
        if isinstance(values, list) and values and isinstance(values[0], list):
            values = cast(list[object], values[0])
        elif values is None:
            values = payload_map.get("embedding")
        if not isinstance(values, list):
            raise ValueError
        return cast(list[object], values)

    @staticmethod
    def _extract_revision(payload: object) -> str | None:
        if not isinstance(payload, dict):
            return None
        payload_map = cast(dict[str, object], payload)
        revision = payload_map.get("model_revision")
        if isinstance(revision, str) and revision:
            return revision[:255]
        details = payload_map.get("details")
        if isinstance(details, dict):
            digest = cast(dict[str, object], details).get("digest")
            if isinstance(digest, str) and digest:
                return digest[:255]
        return None


def _valid_trace_id(value: str | None) -> bool:
    return value is not None and len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_span_id(value: str | None) -> bool:
    return value is not None and len(value) == 16 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_uuid(value: str | None) -> bool:
    if value is None:
        return False
    try:
        UUID(value)
    except (AttributeError, ValueError):
        return False
    return True


def _trace_attributes(context: ProviderTraceContext | None) -> dict[str, str]:
    if context is None:
        return {}
    attributes: dict[str, str] = {}
    for name in (
        "correlation_id",
        "causation_id",
        "command_id",
        "job_id",
        "run_id",
        "conversation_id",
        "generation_id",
    ):
        value = getattr(context, name)
        if _valid_uuid(value):
            attributes[name] = cast(str, value)
    return attributes


def _safe_model_id(value: str) -> bool:
    return bool(value) and len(value) <= 255 and all(
        character.isalnum() or character in ".:_/-" for character in value
    )
