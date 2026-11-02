"""Strict private-origin adapter for the pinned local Ollama API.

The adapter has no caller-controlled URL, proxy, redirect, tool, streaming, or
cloud fallback path.  Provider response text is parsed only in memory and is
reduced to the closed :class:`JudgeResponse` projection before it leaves this
module.
"""

# Ollama's JSON response is a provider-defined object; the adapter validates
# and projects it immediately into a closed response record.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from aura_observatory.evaluation_judges import (
    MAX_ARTIFACT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    JudgeError,
    JudgeOutcome,
    JudgeResponse,
)


class LocalOllamaError(JudgeError):
    pass


class LocalOllamaAdapter:
    """One configured Ollama origin with bounded non-streaming generation."""

    # These are deployment-owned relay names, not a general URL allowlist.
    # Keep the set literal and small so a settings mistake cannot turn this
    # adapter into an arbitrary content egress client.
    ALLOWED_ORIGINS = frozenset(
        {
            "http://ollama:11434",
            "http://ollama",
            "http://ollama.internal:11434",
            "http://ollama.internal",
            "http://host.docker.internal:11434",
            "http://host.docker.internal",
            "http://localhost:11434",
            "http://127.0.0.1:11434",
        }
    )
    MAX_RESPONSE_BYTES = MAX_OUTPUT_BYTES
    # The input contract permits 1 MiB of artifact bytes. JSON escaping and
    # the fixed instruction envelope add bounded overhead, so the transport
    # limit is explicit but does not silently reduce the application budget.
    MAX_REQUEST_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        origin: str,
        *,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(origin)
        normalized_origin = origin.rstrip("/")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("local Ollama origin is invalid")
        if parsed.path not in {"", "/"}:
            raise ValueError("local Ollama origin must not include a path")
        if normalized_origin not in self.ALLOWED_ORIGINS:
            raise ValueError("local Ollama origin is not deployment-owned")
        if not 0.1 <= timeout_seconds <= 120.0 or not math.isfinite(timeout_seconds):
            raise ValueError("local Ollama timeout is invalid")
        self.origin = normalized_origin
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def verify_model(
        self, model_name: str, model_digest: str, *, model_version: str = ""
    ) -> None:
        # /api/tags is the pinned local Ollama inventory endpoint.  Do not use
        # /api/show here: it resolves a caller-selected alias and therefore
        # cannot prove that the configured deployment still exposes the exact
        # admitted name and digest.
        inventory = await self._request("GET", "/api/tags", json_body=None)
        if not isinstance(inventory, Mapping):
            raise LocalOllamaError("provider_malformed")

        # The pinned Ollama API exposes an inventory-shaped ``models``
        # projection.  A digest by itself
        # is never sufficient admission evidence; every entry must carry an
        # exact, case-sensitive name (including its tag/version).
        raw_entries = inventory.get("models")
        if isinstance(raw_entries, list):
            entries = [item for item in raw_entries if isinstance(item, Mapping)]
            if len(entries) != len(raw_entries):
                raise LocalOllamaError("provider_malformed")
        else:
            raise LocalOllamaError("provider_malformed")
        if not entries:
            raise LocalOllamaError("model_not_found")

        # Reject malformed or ambiguous inventory before considering a digest
        # match.  In particular, an entry with a digest but no name must not
        # be allowed to match a configured model.
        names: list[str] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise LocalOllamaError("model_not_found")
            alternate_name = entry.get("model")
            if alternate_name is not None and alternate_name != name:
                raise LocalOllamaError("model_not_found")
            names.append(name)

        matches = [entry for entry, name in zip(entries, names, strict=True) if name == model_name]
        if len(matches) != 1:
            # This covers absent names, case/tag aliases, and duplicate model
            # entries without revealing provider payloads.
            raise LocalOllamaError("model_not_found")
        if matches[0].get("digest") != model_digest:
            raise LocalOllamaError("model_digest_drift")
        returned_version = matches[0].get("version")
        details = matches[0].get("details")
        if returned_version is None and isinstance(details, Mapping):
            returned_version = details.get("version")
        # Ollama's tags inventory commonly carries the version in the exact
        # tagged name rather than a separate field.  When a deployment emits a
        # version field, it must match the admitted definition exactly.
        if returned_version is not None and returned_version != model_version:
            raise LocalOllamaError("model_not_found")

    async def generate(
        self,
        *,
        model_name: str,
        prompt: bytes,
        rubric: bytes,
        evidence: tuple[bytes, ...],
        timeout_seconds: float,
        max_output_bytes: int,
        max_output_tokens: int,
        context_tokens: int,
        seed: int = 0,
    ) -> JudgeResponse:
        if max_output_bytes > MAX_OUTPUT_BYTES or max_output_tokens > MAX_OUTPUT_TOKENS:
            raise LocalOllamaError("provider_resource_limit")
        if len(prompt) + len(rubric) + sum(len(item) for item in evidence) > MAX_ARTIFACT_BYTES:
            raise LocalOllamaError("artifact_limit")
        # Delimit untrusted bytes as quoted data. They are never interpreted as
        # request configuration by this adapter.
        prompt_text = prompt.decode("utf-8", errors="strict")
        rubric_text = rubric.decode("utf-8", errors="strict")
        evidence_text = "\n".join(item.decode("utf-8", errors="strict") for item in evidence)
        body = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Evaluate the quoted evidence according to the quoted rubric. "
                        "Return only the requested JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": "RUBRIC_DATA:\n"
                    + rubric_text
                    + "\nPROMPT_DATA:\n"
                    + prompt_text
                    + "\nEVIDENCE_DATA:\n"
                    + evidence_text,
                },
            ],
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "outcome": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
                    "label": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
                },
                "required": ["score", "outcome", "label"],
                "additionalProperties": False,
            },
            "options": {
                "temperature": 0,
                "seed": seed,
                "num_predict": max_output_tokens,
                "num_ctx": context_tokens,
            },
        }
        response = await self._request(
            "POST",
            "/api/chat",
            json_body=body,
            timeout=timeout_seconds,
            max_bytes=max_output_bytes,
        )
        if not isinstance(response, dict) or set(response) - {
            "message",
            "done",
            "done_reason",
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        }:
            raise LocalOllamaError("provider_malformed")
        message = response.get("message")
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise LocalOllamaError("provider_malformed")
        if message.get("role") != "assistant":
            raise LocalOllamaError("provider_malformed")
        value = message.get("content")
        if not isinstance(value, str) or len(value.encode("utf-8")) > max_output_bytes:
            raise LocalOllamaError("provider_oversized")
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise LocalOllamaError("provider_malformed") from exc
        if not isinstance(parsed, dict) or set(parsed) != {"score", "outcome", "label"}:
            raise LocalOllamaError("provider_malformed")
        score = parsed["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
        ):
            raise LocalOllamaError("provider_malformed")
        if parsed["outcome"] not in {item.value for item in JudgeOutcome} or parsed[
            "label"
        ] not in {"pass", "fail", "inconclusive"}:
            raise LocalOllamaError("provider_malformed")
        return JudgeResponse(float(score), JudgeOutcome(parsed["outcome"]), str(parsed["label"]))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None,
        timeout: float | None = None,
        max_bytes: int = MAX_OUTPUT_BYTES,
    ) -> object:
        request_timeout = httpx.Timeout(
            timeout or self.timeout_seconds, connect=min(10.0, timeout or self.timeout_seconds)
        )
        payload = (
            json.dumps(json_body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            if json_body is not None
            else b""
        )
        if len(payload) > self.MAX_REQUEST_BYTES:
            raise LocalOllamaError("provider_resource_limit")
        try:
            async with httpx.AsyncClient(
                base_url=self.origin,
                timeout=request_timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                request_kwargs: dict[str, object] = {}
                if payload:
                    request_kwargs["content"] = payload
                    request_kwargs["headers"] = {"content-type": "application/json"}
                async with client.stream(method, path, **request_kwargs) as response:
                    if response.is_redirect:
                        raise LocalOllamaError("provider_redirect")
                    if response.status_code == 404:
                        raise LocalOllamaError("model_not_found")
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length) > max_bytes
                    ):
                        raise LocalOllamaError("provider_oversized")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise LocalOllamaError("provider_oversized")
                        chunks.append(chunk)
                    try:
                        return json.loads(b"".join(chunks))
                    except (TypeError, ValueError, UnicodeError):
                        raise LocalOllamaError("provider_malformed") from None
        except LocalOllamaError:
            raise
        except httpx.TimeoutException:
            raise LocalOllamaError("provider_timeout") from None
        except httpx.HTTPStatusError:
            raise LocalOllamaError("provider_unavailable") from None
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise LocalOllamaError("provider_unavailable") from None


__all__ = ["LocalOllamaAdapter", "LocalOllamaError"]
