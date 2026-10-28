# ruff: noqa: E501

"""Inward ports used by generated artifacts.

The domain speaks only in bytes and bounded metadata; Garage and SQLAlchemy
remain adapters at the composition edge.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, Protocol
from uuid import UUID


class GeneratedArtifactObjectStoreError(RuntimeError):
    """A content-free storage failure."""


class GeneratedArtifactCleanupUnknown(GeneratedArtifactObjectStoreError):
    """The provider outcome is ambiguous and must be reconciled."""


class GeneratedArtifactObjectStore(Protocol):
    async def put_if_absent(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        sha256: str,
        byte_count: int,
    ) -> int: ...

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int: ...

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...


class GeneratedArtifactRepository(Protocol):
    async def admit_create(
        self,
        artifact: object,
        revision: object,
        *,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def admit_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def publish_create(
        self,
        artifact: object,
        revision: object,
        *,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def publish_append(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def replay_append(self, owner: tuple[str, str], artifact_id: UUID, *, idempotency_key: str, idempotency_fingerprint: str) -> Any: ...

    async def create(
        self,
        artifact: object,
        revision: object,
        *,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def append_revision(
        self,
        artifact_id: UUID,
        revision: object,
        *,
        owner: tuple[str, str],
        expected_version: int,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def get(self, owner: tuple[str, str], artifact_id: UUID) -> Any: ...

    async def list(self, owner: tuple[str, str], *, limit: int = 100, offset: int = 0, media_type: str | None = None) -> list[Any]: ...

    async def revisions(self, owner: tuple[str, str], artifact_id: UUID) -> list[Any]: ...

    async def get_revision(self, owner: tuple[str, str], artifact_id: UUID, revision: int | None = None) -> Any: ...

    async def begin_delete(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        idempotency_fingerprint: str | None = None,
    ) -> Any: ...

    async def admit_cleanup(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        expected_version: int,
        operation: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
    ) -> Any: ...

    async def set_cleanup_state(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        expected_version: int,
        lifecycle: str,
        failure_code: str | None = None,
        idempotency_key: str | None = None,
        operation: str = "delete",
        idempotency_fingerprint: str | None = None,
        claim_token: str | None = None,
    ) -> Any: ...

    async def replay_cleanup(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        operation: str,
        idempotency_key: str,
        idempotency_fingerprint: str,
    ) -> Any: ...

    async def claim_pending_cleanup(self, owner: tuple[str, str], *, limit: int = 20) -> list[Any]: ...

    async def assert_cleanup_claim(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        operation: str,
        claim_token: str,
    ) -> None: ...

    async def renew_cleanup_claim(
        self,
        owner: tuple[str, str],
        artifact_id: UUID,
        *,
        operation: str,
        claim_token: str,
    ) -> None: ...
