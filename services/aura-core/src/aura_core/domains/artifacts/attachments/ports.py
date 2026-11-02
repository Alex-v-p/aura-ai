"""Inward ports for image inspection and private object storage.

The attachment domain never imports a storage or imaging SDK.  Adapters own
those concerns and return only bounded metadata and bytes through these ports.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from aura_core.domains.artifacts.attachments.public import (
        Attachment,
        AttachmentLifecycleEvent,
    )


class AttachmentObjectStore(Protocol):
    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int: ...

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class InspectedImage:
    media_type: str
    width: int
    height: int
    derivative: bytes
    animated: bool = False


class ImageInspector(Protocol):
    def inspect(self, data: bytes, *, declared_media_type: str) -> InspectedImage: ...


class AttachmentRepository(Protocol):
    async def save(self, attachment: Attachment) -> Attachment: ...

    async def save_with_event(
        self, attachment: Attachment, event: AttachmentLifecycleEvent
    ) -> Attachment: ...

    async def get(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None: ...

    async def get_by_digest(
        self,
        owner: tuple[str, str],
        digest: str,
        *,
        exclude_attachment_id: UUID | None = None,
    ) -> Attachment | None: ...

    async def list(self, owner: tuple[str, str]) -> list[Attachment]: ...

    async def delete(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None: ...

    async def delete_with_event(
        self,
        owner: tuple[str, str],
        attachment: Attachment,
        event: AttachmentLifecycleEvent,
    ) -> Attachment | None: ...

    async def create_upload_session(self, value: dict[str, object]) -> dict[str, object]: ...

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> dict[str, object] | None: ...

    async def complete_upload_session(
        self,
        owner: tuple[str, str],
        upload_id: UUID,
        expected_version: int = 1,
        attachment_id: UUID | None = None,
    ) -> None: ...

    async def get_delete_request(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> dict[str, object] | None: ...

    async def save_delete_request(self, value: dict[str, object]) -> None: ...


class AttachmentReadPort(Protocol):
    """Owner-scoped metadata/content port consumed by other domains."""

    async def get(self, issuer: str, subject: str, attachment_id: UUID) -> Attachment: ...

    async def preview(self, issuer: str, subject: str, attachment_id: UUID) -> bytes: ...

    async def content(self, issuer: str, subject: str, attachment_id: UUID) -> bytes: ...
