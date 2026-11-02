"""AWS SigV4 S3-compatible adapter for the private Garage bucket."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote, urlsplit

import httpx

from aura_core.domains.knowledge.documents.ports import DocumentObjectStoreError


class GarageObjectStoreError(DocumentObjectStoreError):
    """Content-free object-store failure."""


class UnavailableObjectStore:
    """Fail at use time when runtime credentials are not configured."""

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del key, stream, content_type
        raise GarageObjectStoreError("object storage is unavailable")

    async def get(self, key: str) -> AsyncIterator[bytes]:
        del key
        raise GarageObjectStoreError("object storage is unavailable")
        yield b""

    async def delete(self, key: str) -> None:
        del key
        raise GarageObjectStoreError("object storage is unavailable")


class GarageS3ObjectStore:
    """Use standard AWS Signature Version 4 request authentication."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        *,
        access_key: str,
        secret_key: str,
        region: str = "garage",
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("object storage endpoint must be an HTTP(S) URL")
        if not bucket or not access_key or not secret_key or not region:
            raise ValueError("object storage bucket, region, and credentials are required")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = httpx.Timeout(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _url(self, key: str) -> str:
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
        canonical_headers = "".join(
            f"{name}:{' '.join(value.strip().split())}\n"
            for name, value in sorted(request.headers.items())
            if name.lower() in signed_names
        )
        signed_headers = ";".join(
            name.lower() for name in sorted(request.headers) if name.lower() in signed_names
        )
        canonical_request = "\n".join(
            [
                method,
                request.url.raw_path.decode("ascii") or "/",
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

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        request = self._signed_request(
            "PUT",
            key,
            headers={"content-type": content_type},
            payload_hash="UNSIGNED-PAYLOAD",
            content=stream,
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.send(request)
                try:
                    response.raise_for_status()
                    value = response.headers.get("content-length")
                    return int(value) if value and value.isdigit() else 0
                finally:
                    await response.aclose()
            except Exception as exc:
                raise GarageObjectStoreError("object upload failed") from exc

    async def get(self, key: str) -> AsyncIterator[bytes]:
        request = self._signed_request(
            "GET", key, payload_hash=hashlib.sha256(b"").hexdigest()
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.send(request, stream=True)
                try:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
                finally:
                    await response.aclose()
            except Exception as exc:
                raise GarageObjectStoreError("object download failed") from exc

    async def delete(self, key: str) -> None:
        request = self._signed_request(
            "DELETE", key, payload_hash=hashlib.sha256(b"").hexdigest()
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response: httpx.Response | None = None
            try:
                response = await client.send(request)
                if response.status_code >= 400 and response.status_code != 404:
                    raise GarageObjectStoreError("object deletion failed")
            except GarageObjectStoreError:
                raise
            except Exception as exc:
                raise GarageObjectStoreError("object deletion failed") from exc
            finally:
                if response is not None:
                    await response.aclose()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"s3", hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()
