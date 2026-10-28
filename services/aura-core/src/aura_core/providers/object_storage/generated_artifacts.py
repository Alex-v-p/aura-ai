# ruff: noqa: E501

"""Dedicated Garage adapter for the ``aura-artifacts`` bucket.

This adapter intentionally does not accept a bucket from a request.  The
bucket is fixed by composition and the generated-artifact identity has no
permissions on document, evaluation, or Observatory buckets.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import datetime

from aura_core.domains.artifacts.generated.ports import (
    GeneratedArtifactCleanupUnknown,
    GeneratedArtifactObjectStoreError,
)
from aura_core.providers.object_storage.garage import GarageS3ObjectStore

GENERATED_ARTIFACT_BUCKET = "aura-artifacts"


class GeneratedArtifactGarageObjectStore:
    bucket_name = GENERATED_ARTIFACT_BUCKET

    def __init__(
        self,
        endpoint: str,
        *,
        access_key: str,
        secret_key: str,
        region: str = "garage",
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = GarageS3ObjectStore(
            endpoint,
            GENERATED_ARTIFACT_BUCKET,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            timeout_seconds=timeout_seconds,
            clock=clock,
        )

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        try:
            return await self._store.put_stream(key, stream, content_type=content_type)
        except Exception as exc:
            raise GeneratedArtifactObjectStoreError("generated artifact upload failed") from exc

    async def put_if_absent(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        sha256: str,
        byte_count: int,
    ) -> int:
        """Publish only to a digest-qualified key.

        Garage's S3 endpoint has no portable conditional-put primitive in the
        supported local deployment.  A pre-existing key is therefore read and
        compared before publication; the digest-qualified key makes any race
        fail closed rather than overwriting another revision.
        """
        try:
            existing = await self._read_existing(key)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if len(existing) != byte_count or hashlib.sha256(existing).hexdigest() != sha256:
                raise GeneratedArtifactObjectStoreError("immutable artifact key collision")
            return len(existing)
        return await self.put_stream(key, stream, content_type=content_type)

    async def _read_existing(self, key: str) -> bytes:
        chunks: list[bytes] = []
        try:
            async for chunk in self._store.get(key):
                chunks.append(chunk)
        except Exception as exc:
            if _is_not_found(exc):
                raise FileNotFoundError(key) from exc
            raise GeneratedArtifactObjectStoreError("generated artifact read failed") from exc
        return b"".join(chunks)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in self._store.get(key):
                    yield chunk
            except Exception as exc:
                if _is_not_found(exc):
                    raise FileNotFoundError(key) from exc
                raise GeneratedArtifactObjectStoreError("generated artifact download failed") from exc

        return stream()

    async def delete(self, key: str) -> None:
        try:
            await self._store.delete(key)
        except FileNotFoundError:
            return
        except Exception as exc:
            # Garage's network timeout has no reliable delete truth; callers
            # must retain cleanup_unknown until an explicit reconciliation.
            if _is_timeout(exc):
                raise GeneratedArtifactCleanupUnknown("generated artifact cleanup outcome is unknown") from exc
            raise GeneratedArtifactObjectStoreError("generated artifact deletion failed") from exc


class UnavailableGeneratedArtifactObjectStore:
    async def put_if_absent(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        sha256: str,
        byte_count: int,
    ) -> int:
        del key, stream, content_type, sha256, byte_count
        raise GeneratedArtifactObjectStoreError("generated artifact storage is unavailable")
    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        del key, stream, content_type
        raise GeneratedArtifactObjectStoreError("generated artifact storage is unavailable")

    def get(self, key: str) -> AsyncIterator[bytes]:
        del key

        async def fail() -> AsyncIterator[bytes]:
            raise GeneratedArtifactObjectStoreError("generated artifact storage is unavailable")
            yield b""

        return fail()

    async def delete(self, key: str) -> None:
        del key
        raise GeneratedArtifactObjectStoreError("generated artifact storage is unavailable")


def _causes(exc: BaseException):
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_not_found(exc: BaseException) -> bool:
    return any(
        isinstance(item, FileNotFoundError)
        or getattr(getattr(item, "response", None), "status_code", None) == 404
        or "404" in str(item).lower()
        or "not found" in str(item).lower()
        for item in _causes(exc)
    )


def _is_timeout(exc: BaseException) -> bool:
    return any(isinstance(item, TimeoutError) or item.__class__.__name__ in {"ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout"} for item in _causes(exc))
