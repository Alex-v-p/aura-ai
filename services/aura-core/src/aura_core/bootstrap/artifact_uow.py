# ruff: noqa: E501

"""Composition for the Core generated-artifact public API."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from aura_core.domains.artifacts.generated.public import GeneratedArtifactService
from aura_core.domains.artifacts.generated.repository import (
    MemoryGeneratedArtifactRepository,
    SqlGeneratedArtifactRepository,
)
from aura_core.providers.object_storage.generated_artifacts import (
    GeneratedArtifactGarageObjectStore,
    UnavailableGeneratedArtifactObjectStore,
)


class MemoryGeneratedArtifactObjectStore:
    """Deterministic bounded object store for tests and local composition."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        del content_type
        value = b"".join([piece async for piece in stream])
        self.objects[key] = value
        return len(value)

    async def put_if_absent(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        sha256: str,
        byte_count: int,
    ) -> int:
        import hashlib

        del content_type
        value = b"".join([piece async for piece in stream])
        if len(value) != byte_count or hashlib.sha256(value).hexdigest() != sha256:
            raise ValueError("artifact digest or byte count mismatch")
        existing = self.objects.get(key)
        if existing is not None:
            if existing != value:
                raise ValueError("immutable artifact key collision")
            return len(existing)
        self.objects[key] = value
        return len(value)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            if key not in self.objects:
                raise FileNotFoundError(key)
            yield self.objects[key]

        return stream()

    async def delete(self, key: str) -> None:
        if key not in self.objects:
            raise FileNotFoundError(key)
        del self.objects[key]


def artifact_service(
    sessions: Any = None,
    *,
    settings: Any = None,
    metrics: Any = None,
    testing: bool = False,
    repository: Any = None,
    object_store: Any = None,
) -> GeneratedArtifactService:
    if repository is not None and object_store is not None:
        return GeneratedArtifactService(repository, object_store, metrics=metrics)
    if testing or sessions is None:
        return GeneratedArtifactService(
            MemoryGeneratedArtifactRepository(),
            MemoryGeneratedArtifactObjectStore(),
            metrics=metrics,
        )
    if settings is None:
        raise ValueError("settings are required for SQL artifact composition")
    repository = SqlGeneratedArtifactRepository(sessions)
    access_key = getattr(settings, "artifact_storage_access_key", "")
    secret_key = getattr(settings, "artifact_storage_secret_key", "")
    store = (
        GeneratedArtifactGarageObjectStore(
            getattr(settings, "artifact_storage_endpoint", settings.object_storage_endpoint),
            access_key=access_key,
            secret_key=secret_key,
            region=getattr(settings, "artifact_storage_region", settings.object_storage_region),
        )
        if access_key and secret_key
        else UnavailableGeneratedArtifactObjectStore()
    )
    return GeneratedArtifactService(repository, store, metrics=metrics)
