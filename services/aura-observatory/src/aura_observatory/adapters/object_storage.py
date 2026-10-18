"""Least-privilege S3-compatible adapter for the Observatory Garage bucket.

Only UUID artifact references enter this adapter.  The provider key is derived
internally and is never returned or persisted.  Responses are classified into
bounded failure codes; provider bodies are not logged or propagated.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterable, Callable
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx


class EvaluationObjectStoreError(RuntimeError):
    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


def object_key_for_artifact(artifact_reference_id: str) -> str:
    """Derive a fixed internal key from an opaque UUID, never caller path data."""

    try:
        parsed = UUID(artifact_reference_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError("artifact reference must be a UUID") from exc
    if str(parsed) != artifact_reference_id.lower() or len(artifact_reference_id) != 36:
        raise ValueError("artifact reference must be a canonical UUID")
    return f"artifacts/{parsed}"


class GarageEvaluationObjectStore:
    """AWS SigV4 requests constrained to one configured private bucket."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        *,
        access_key: str,
        secret_key: str,
        region: str = "garage",
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("evaluation object endpoint must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("evaluation object endpoint must not include credentials or query")
        if not bucket or len(bucket) > 63 or "/" in bucket:
            raise ValueError("evaluation object bucket is invalid")
        if not access_key or not secret_key or not region:
            raise ValueError("evaluation object credentials are required")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def put_stream(
        self,
        artifact_reference_id: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> int:
        key = object_key_for_artifact(artifact_reference_id)
        if expected_sha256 is not None and (
            len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise EvaluationObjectStoreError("storage_integrity")
        request = self._signed_request(
            "PUT",
            key,
            headers={"content-type": content_type},
            # Garage validates the SigV4 payload hash against the streamed
            # body.  Never publish an object whose bytes differ from the
            # contract digest.  The unsigned form is retained only for
            # callers that do not provide an integrity contract.
            payload_hash=expected_sha256 or "UNSIGNED-PAYLOAD",
            content=stream,
        )
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            try:
                response = await client.send(request)
                try:
                    if response.is_redirect:
                        raise EvaluationObjectStoreError("storage_integrity")
                    response.raise_for_status()
                    value = response.headers.get("content-length")
                    return int(value) if value and value.isdigit() else 0
                finally:
                    await response.aclose()
            except EvaluationObjectStoreError:
                raise
            except httpx.TimeoutException as exc:
                raise EvaluationObjectStoreError("storage_timeout") from exc
            except httpx.HTTPStatusError as exc:
                code = (
                    "storage_authentication"
                    if exc.response.status_code in {401, 403}
                    else "storage_failure"
                )
                raise EvaluationObjectStoreError(code) from exc
            except Exception as exc:
                source_code = getattr(exc, "failure_code", None)
                if source_code in {"artifact_limit_exceeded", "malformed_stream"}:
                    raise EvaluationObjectStoreError(str(source_code)) from exc
                raise EvaluationObjectStoreError("storage_failure") from exc

    async def delete(self, artifact_reference_id: str) -> None:
        key = object_key_for_artifact(artifact_reference_id)
        request = self._signed_request(
            "DELETE", key, payload_hash=hashlib.sha256(b"").hexdigest()
        )
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response: httpx.Response | None = None
            try:
                response = await client.send(request)
                if response.is_redirect:
                    raise EvaluationObjectStoreError("storage_integrity")
                if response.status_code >= 400 and response.status_code != 404:
                    code = (
                        "storage_authentication"
                        if response.status_code in {401, 403}
                        else "storage_failure"
                    )
                    raise EvaluationObjectStoreError(code)
            except EvaluationObjectStoreError:
                raise
            except httpx.TimeoutException as exc:
                raise EvaluationObjectStoreError("storage_timeout") from exc
            except Exception as exc:
                raise EvaluationObjectStoreError("storage_failure") from exc
            finally:
                if response is not None:
                    await response.aclose()

    def _url(self, key: str) -> str:
        # ``key`` is generated by object_key_for_artifact; quote again to make
        # this helper safe if it is ever called from another internal path.
        return f"{self.endpoint}/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"

    def _signed_request(
        self,
        method: str,
        key: str,
        *,
        headers: dict[str, str] | None = None,
        payload_hash: str,
        content: AsyncIterable[bytes] | None = None,
    ) -> httpx.Request:
        request = httpx.Request(method, self._url(key), headers=headers, content=content)
        now = self._clock().astimezone(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        netloc = cast(bytes | str, request.url.netloc)
        request.headers["host"] = (
            netloc.decode("ascii") if isinstance(netloc, bytes) else netloc
        )
        request.headers["x-amz-date"] = amz_date
        request.headers["x-amz-content-sha256"] = payload_hash
        signed_names = {"content-type", "host", "x-amz-content-sha256", "x-amz-date"}
        canonical_headers, signed_headers = _canonical_headers(
            request.headers, signed_names
        )
        canonical_request = "\n".join(
            [
                method,
                _canonical_uri(request),
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{date}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = _signing_key(self.secret_key, date, self.region)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        request.headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return request


def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"s3", hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


def _canonical_uri(request: httpx.Request) -> str:
    """Return Garage's path-style URI without normalising S3 object paths.

    S3 signs the path exactly as sent.  ``httpx.URL.raw_path`` is already
    percent-encoded and, unlike ``URL.path``, preserves encoded octets.  A
    query is not part of this adapter's URL surface, but split it defensively
    so a future internal caller cannot accidentally sign it as the URI.
    """

    raw_path = request.url.raw_path.decode("ascii")
    return raw_path.partition("?")[0] or "/"


def _canonical_headers(
    headers: httpx.Headers, signed_names: set[str]
) -> tuple[str, str]:
    """Build the AWS canonical header block and its deterministic name list."""

    selected = sorted(
        (name.lower(), " ".join(value.strip().split()))
        for name, value in headers.items()
        if name.lower() in signed_names
    )
    return (
        "".join(f"{name}:{value}\n" for name, value in selected),
        ";".join(name for name, _value in selected),
    )


__all__ = ["EvaluationObjectStoreError", "GarageEvaluationObjectStore", "object_key_for_artifact"]
