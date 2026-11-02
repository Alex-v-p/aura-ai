"""Composition for the owner-private image attachment boundary."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from aura_core.domains.artifacts.attachments.ports import InspectedImage
from aura_core.domains.artifacts.attachments.public import AttachmentService
from aura_core.domains.artifacts.attachments.repository import (
    MemoryAttachmentRepository,
    SqlAttachmentRepository,
)
from aura_core.platform.outbox import SqlOutboxRepository
from aura_core.providers.object_storage.garage import GarageS3ObjectStore, UnavailableObjectStore
from aura_core.providers.object_storage.image_inspection import PillowImageInspector


class MemoryAttachmentObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        del content_type
        value = b"".join([piece async for piece in stream])
        self.objects[key] = value
        return len(value)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            value = self.objects.get(key)
            if value is None:
                raise FileNotFoundError(key)
            yield value

        return stream()

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class _AttachmentImageInspector:
    def __init__(self) -> None:
        self._delegate = PillowImageInspector()

    def inspect(self, data: bytes, *, declared_media_type: str) -> InspectedImage:
        result = self._delegate.inspect(data, declared_media_type=declared_media_type)
        return InspectedImage(
            result.media_type,
            result.width,
            result.height,
            result.derivative_bytes,
            result.animated,
        )


def attachment_service(
    sessions: Any = None,
    *,
    settings: Any = None,
    metrics: Any = None,
    testing: bool = False,
) -> AttachmentService:
    if testing or sessions is None:
        return AttachmentService(
            MemoryAttachmentRepository(),
            MemoryAttachmentObjectStore(),
            inspector=_AttachmentImageInspector(),
            metrics=metrics,
        )
    repository = SqlAttachmentRepository(sessions, SqlOutboxRepository())
    access_key = getattr(settings, "object_storage_access_key", None)
    secret_key = getattr(settings, "object_storage_secret_key", None)
    store = (
        GarageS3ObjectStore(
            settings.object_storage_endpoint,
            settings.object_storage_bucket,
            access_key=access_key,
            secret_key=secret_key,
            region=settings.object_storage_region,
        )
        if access_key and secret_key
        else UnavailableObjectStore()
    )
    return AttachmentService(
        repository,
        store,
        inspector=_AttachmentImageInspector(),
        metrics=metrics,
    )
