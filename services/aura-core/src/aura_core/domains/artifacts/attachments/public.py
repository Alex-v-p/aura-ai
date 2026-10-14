"""Public attachment value objects and owner-scoped image use cases.

Only image bytes are owned here.  Documents remain in ``knowledge.documents``
and are referenced by opaque IDs from conversation links.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4, uuid5

from aura_core.domains.artifacts.attachments.ports import (
    AttachmentObjectStore,
    AttachmentRepository,
    ImageInspector,
    InspectedImage,
)

MAX_IMAGE_BYTES = 25 * 1_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_DERIVATIVE_DIMENSION = 2048
ATTACHMENT_ID_NAMESPACE = UUID("e32e7e87-5ef5-42ba-9870-0ed6cb7c3401")
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_IMAGES_PER_MESSAGE = 4
SUPPORTED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_OWNER_RE = re.compile(r"^[^\x00\n\r]{1,1024}$")


class AttachmentStatus(StrEnum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    UNAVAILABLE = "unavailable"


class AttachmentError(RuntimeError):
    pass


class AttachmentAdmissionError(AttachmentError, ValueError):
    """An explicit conversation resource set is not authorized and ready."""


class ImageValidationError(AttachmentError, ValueError):
    pass


class AttachmentNotFound(AttachmentError):
    pass


class AttachmentConflict(AttachmentError):
    """Optimistic version or idempotency conflict at an attachment boundary."""


@dataclass(frozen=True, slots=True)
class Attachment:
    id: UUID
    issuer: str
    subject: str
    filename: str
    media_type: str
    byte_count: int
    digest: str
    original_object_key: str
    derivative_object_key: str
    width: int
    height: int
    status: AttachmentStatus
    version: int
    failure_code: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject

    @property
    def available(self) -> bool:
        return self.status is AttachmentStatus.READY

    def deleted(self) -> Attachment:
        return replace(
            self,
            status=AttachmentStatus.UNAVAILABLE,
            version=self.version + 1,
            original_object_key="",
            derivative_object_key="",
            failure_code="deleted",
            updated_at=datetime.now(UTC),
        )

    def deleting(self) -> Attachment:
        return replace(
            self,
            status=AttachmentStatus.DELETING,
            version=self.version + 1,
            updated_at=datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class AttachmentLifecycleEvent:
    """Identifier-only lifecycle event; never contains filenames or bytes."""

    attachment_id: UUID
    status: AttachmentStatus
    version: int
    occurred_at: datetime
    event_type: str
    upload_id: UUID | None = None
    stored_object_count: int = 0
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @property
    def event_id(self) -> UUID:
        return uuid5(
            ATTACHMENT_ID_NAMESPACE,
            f"event:{self.attachment_id}:{self.version}:{self.event_type}",
        )

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "attachmentId": str(self.attachment_id),
            "status": (
                "deleted" if self.status is AttachmentStatus.UNAVAILABLE else self.status.value
            ),
            "attachmentVersion": self.version,
            "storedObjectCount": self.stored_object_count,
            "uploadId": str(self.upload_id) if self.upload_id else None,
            "occurredAt": self.occurred_at.isoformat(),
            "correlationId": str(self.correlation_id or self.event_id),
            "causationId": str(self.causation_id or self.event_id),
        }


def _filename(value: str) -> str:
    if (
        not value
        or len(value) > 255
        or "/" in value
        or "\\" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ImageValidationError("invalid filename")
    return value


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not _OWNER_RE.fullmatch(issuer) or not _OWNER_RE.fullmatch(subject):
        raise ImageValidationError("invalid owner identity")
    return issuer, subject


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ImageValidationError("invalid PNG signature")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ImageValidationError("invalid JPEG signature")
    position = 2
    while position + 3 < len(data):
        while position < len(data) and data[position] != 0xFF:
            position += 1
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD8, 0xD9}:
            continue
        if position + 2 > len(data):
            break
        size = int.from_bytes(data[position : position + 2], "big")
        if size < 2 or position + size > len(data):
            raise ImageValidationError("malformed JPEG")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if size < 7:
                raise ImageValidationError("malformed JPEG frame")
            return int.from_bytes(data[position + 3 : position + 5], "big"), int.from_bytes(
                data[position + 5 : position + 7], "big"
            )
        position += size
    raise ImageValidationError("JPEG dimensions unavailable")


def _webp_dimensions(data: bytes) -> tuple[int, int, bool]:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageValidationError("invalid WebP signature")
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        flags = data[20]
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height, bool(flags & 0x02)
    if kind == b"VP8L" and len(data) >= 25:
        if data[20] != 0x2F:
            raise ImageValidationError("malformed WebP")
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF), False
    if kind == b"VP8 " and len(data) >= 30:
        marker = data.find(b"\x9d\x01\x2a", 20)
        if marker >= 0 and marker + 7 <= len(data):
            return (
                int.from_bytes(data[marker + 3 : marker + 5], "little") & 0x3FFF,
                int.from_bytes(data[marker + 5 : marker + 7], "little") & 0x3FFF,
                False,
            )
    raise ImageValidationError("unsupported or malformed WebP")


class BasicImageInspector:
    """Bounded signature inspector used by tests and local fallback composition.

    Production composition should inject a decoder-backed inspector that
    color-normalizes and resizes the derivative.  This fallback intentionally
    does not parse arbitrary image metadata or trust declared MIME types.
    """

    def inspect(self, data: bytes, *, declared_media_type: str) -> InspectedImage:
        if declared_media_type not in SUPPORTED_MEDIA_TYPES:
            raise ImageValidationError("unsupported image type")
        if declared_media_type == "image/png":
            width, height = _png_dimensions(data)
            animated = False
        elif declared_media_type == "image/jpeg":
            width, height = _jpeg_dimensions(data)
            animated = False
        else:
            width, height, animated = _webp_dimensions(data)
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            raise ImageValidationError("image dimensions exceed safety limit")
        if animated:
            raise ImageValidationError("animated images are not supported")
        return InspectedImage(declared_media_type, width, height, data, animated)


class AttachmentMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        **attributes: str,
    ) -> None: ...


async def _aiter_bytes(
    source: AsyncIterable[bytes] | Iterable[bytes] | bytes,
) -> AsyncIterator[bytes]:
    if isinstance(source, bytes):
        yield source
    elif hasattr(source, "__aiter__"):
        async for piece in source:  # type: ignore[union-attr]
            yield piece
    else:
        for piece in source:  # type: ignore[union-attr]
            yield piece


class AttachmentService:
    """Owner-scoped image upload, preview, download, and deletion commands."""

    def __init__(
        self,
        repository: AttachmentRepository,
        object_store: AttachmentObjectStore,
        *,
        inspector: ImageInspector | None = None,
        metrics: AttachmentMetrics | None = None,
        clock: object | None = None,
        lifecycle_sink: Callable[[AttachmentLifecycleEvent], object] | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.inspector = inspector or BasicImageInspector()
        self.metrics = metrics
        self.lifecycle_sink = lifecycle_sink
        self.lifecycle_events: list[AttachmentLifecycleEvent] = []
        self.clock: Callable[[], datetime] = (
            cast(Callable[[], datetime], clock) if callable(clock) else lambda: datetime.now(UTC)
        )
        self._idempotency: dict[tuple[str, str, str], tuple[str, Attachment]] = {}
        self._delete_idempotency: dict[tuple[str, str, str], tuple[str, Attachment]] = {}
        self._upload_sessions: dict[tuple[str, str, UUID], dict[str, object]] = {}
        self._upload_idempotency: dict[tuple[str, str, str], dict[str, object]] = {}
        self._upload_replays: dict[tuple[str, str, UUID], tuple[str, Attachment]] = {}
        self._lock = asyncio.Lock()

    async def create_upload_session(
        self,
        issuer: str,
        subject: str,
        *,
        filename: str,
        media_type: str,
        byte_count: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        _owner(issuer, subject)
        _filename(filename)
        if media_type not in SUPPORTED_MEDIA_TYPES or not 0 < byte_count <= MAX_IMAGE_BYTES:
            raise ImageValidationError("invalid upload metadata")
        fingerprint = hashlib.sha256(
            f"{filename}\x00{media_type}\x00{byte_count}".encode()
        ).hexdigest()
        prior = self._upload_idempotency.get((issuer, subject, idempotency_key))
        if prior is not None:
            if prior.get("fingerprint") != fingerprint:
                raise AttachmentConflict("upload idempotency conflict")
            return prior
        upload_id, attachment_id = uuid4(), uuid4()
        created = self.clock()
        value: dict[str, object] = {
            "id": upload_id,
            "attachment_id": attachment_id,
            "filename": filename,
            "media_type": media_type,
            "byte_count": byte_count,
            "idempotency_key": idempotency_key,
            "created_at": created,
            "expires_at": created + timedelta(minutes=15),
            "status": "uploading",
            "version": 1,
            "principal_issuer": issuer,
            "principal_subject": subject,
            "fingerprint": fingerprint,
        }
        try:
            value = await self.repository.create_upload_session(value)
        except ValueError as exc:
            raise AttachmentConflict("upload idempotency conflict") from exc
        if value.get("fingerprint") != fingerprint:
            raise AttachmentConflict("upload idempotency conflict")
        self._upload_sessions[(issuer, subject, upload_id)] = value
        self._upload_idempotency[(issuer, subject, idempotency_key)] = value
        return value

    async def complete_upload(
        self,
        issuer: str,
        subject: str,
        upload_id: UUID,
        *,
        stream: AsyncIterable[bytes] | Iterable[bytes] | bytes,
        idempotency_key: str,
        media_type: str | None = None,
        expected_version: int | None = None,
    ) -> Attachment:
        replay = self._upload_replays.get((issuer, subject, upload_id))
        if replay is not None:
            replay_key, replay_attachment = replay
            if idempotency_key != replay_key:
                raise AttachmentConflict("upload idempotency conflict")
            return replay_attachment
        value = await self.repository.get_upload_session((issuer, subject), upload_id)
        if value is None:
            raise AttachmentNotFound
        if value["idempotency_key"] != idempotency_key:
            raise AttachmentConflict("upload idempotency conflict")
        if (
            expected_version is not None
            and int(cast(int, value.get("version", 1))) != expected_version
        ):
            raise AttachmentConflict("upload session version conflict")
        if media_type is not None and value["media_type"] != media_type:
            raise AttachmentConflict("upload media type conflict")
        if value.get("status") == "completed":
            existing = await self.get(issuer, subject, cast(UUID, value["attachment_id"]))
            return existing
        if cast(datetime, value["expires_at"]) <= self.clock():
            raise AttachmentNotFound
        pieces: list[bytes] = []
        total = 0
        async for piece in _aiter_bytes(stream):
            total += len(piece)
            if total > MAX_IMAGE_BYTES:
                raise ImageValidationError("image exceeds 25 MB limit")
            pieces.append(piece)
        if total != int(cast(int, value["byte_count"])):
            raise AttachmentConflict("upload byte count conflict")
        attachment = await self.upload(
            issuer,
            subject,
            filename=cast(str, value["filename"]),
            media_type=cast(str, value["media_type"]),
            stream=b"".join(pieces),
            idempotency_key=idempotency_key,
            attachment_id=cast(UUID, value["attachment_id"]),
            upload_id=upload_id,
        )
        await self.repository.complete_upload_session(
            (issuer, subject),
            upload_id,
            int(cast(int, value.get("version", expected_version or 1))),
        )
        self._upload_sessions.pop((issuer, subject, upload_id), None)
        self._upload_replays[(issuer, subject, upload_id)] = (idempotency_key, attachment)
        return attachment

    async def upload(
        self,
        issuer: str,
        subject: str,
        *,
        filename: str,
        media_type: str,
        stream: AsyncIterable[bytes] | Iterable[bytes] | bytes,
        idempotency_key: str | None = None,
        attachment_id: UUID | None = None,
        upload_id: UUID | None = None,
    ) -> Attachment:
        operation_started = monotonic()
        _owner(issuer, subject)
        _filename(filename)
        if media_type not in SUPPORTED_MEDIA_TYPES:
            raise ImageValidationError("unsupported image type")
        pieces: list[bytes] = []
        total = 0
        async for piece in _aiter_bytes(stream):
            total += len(piece)
            if total > MAX_IMAGE_BYTES:
                raise ImageValidationError("image exceeds 25 MB limit")
            pieces.append(piece)
        data = b"".join(pieces)
        digest = hashlib.sha256(data).hexdigest()
        key = (issuer, subject, idempotency_key) if idempotency_key else None
        fingerprint = f"{filename}:{media_type}:{digest}"
        if key is not None and key in self._idempotency:
            prior_fingerprint, prior = self._idempotency[key]
            if prior_fingerprint != fingerprint:
                raise ImageValidationError("idempotency key was already used")
            return prior
        try:
            inspected = self.inspector.inspect(data, declared_media_type=media_type)
        except Exception as exc:
            # Decoder-specific diagnostics stay behind the inward port.
            del exc
            raise ImageValidationError("image failed safety inspection") from None
        if inspected.animated or inspected.width * inspected.height > MAX_IMAGE_PIXELS:
            raise ImageValidationError("image failed safety inspection")
        identifier = attachment_id or uuid4()
        upload_trace_id = uuid5(ATTACHMENT_ID_NAMESPACE, f"upload:{identifier}").hex
        upload_span_id = uuid4().hex[:16]
        now = self.clock()
        attachment = Attachment(
            identifier,
            issuer,
            subject,
            filename,
            inspected.media_type,
            total,
            digest,
            f"attachments/{issuer}/{subject}/{identifier}/original",
            f"attachments/{issuer}/{subject}/{identifier}/preview",
            inspected.width,
            inspected.height,
            AttachmentStatus.UPLOADING,
            1,
            None,
            now,
            now,
        )
        await self._persist(attachment, "attachment.upload.created", idempotency_key, upload_id)
        attachment = replace(
            attachment, status=AttachmentStatus.PROCESSING, updated_at=self.clock()
        )
        await self._persist(attachment, "attachment.processing", idempotency_key, upload_id)
        try:
            await self.object_store.put_stream(
                attachment.original_object_key, _aiter_bytes(data), content_type=media_type
            )
            await self.object_store.put_stream(
                attachment.derivative_object_key,
                _aiter_bytes(inspected.derivative),
                content_type=media_type,
            )
            attachment = replace(attachment, status=AttachmentStatus.READY, updated_at=self.clock())
            await self._persist(attachment, "attachment.ready", idempotency_key, upload_id)
            self._metric(
                "aura.artifacts.attachment_processing",
                "attachment_processing_outcome",
                "ok",
                duration_ms=(monotonic() - operation_started) * 1000,
                attachment_id=attachment.id,
                trace_id=upload_trace_id,
                parent_span_id=upload_span_id,
            )
        except Exception:
            try:
                await self.object_store.delete(attachment.original_object_key)
                await self.object_store.delete(attachment.derivative_object_key)
            except Exception:
                pass
            attachment = replace(attachment, status=AttachmentStatus.FAILED, failure_code="storage")
            await self._persist(attachment, "attachment.failed", idempotency_key, upload_id)
            self._metric(
                "aura.artifacts.attachment_processing",
                "attachment_processing_outcome",
                "error",
                duration_ms=(monotonic() - operation_started) * 1000,
                attachment_id=attachment.id,
                trace_id=upload_trace_id,
                parent_span_id=upload_span_id,
                error_class="object_storage_failed",
            )
            raise AttachmentError("attachment storage unavailable") from None
        if key is not None:
            self._idempotency[key] = (fingerprint, attachment)
        self._metric(
            "aura.artifacts.attachment_upload",
            "attachment_upload_outcome",
            "ok",
            duration_ms=(monotonic() - operation_started) * 1000,
            attachment_id=attachment.id,
            trace_id=upload_trace_id,
            span_id=upload_span_id,
        )
        return attachment

    async def get(self, issuer: str, subject: str, attachment_id: UUID) -> Attachment:
        value = await self._get_owned(issuer, subject, attachment_id)
        if value.status is AttachmentStatus.UNAVAILABLE:
            raise AttachmentNotFound
        return value

    async def list(self, issuer: str, subject: str) -> list[Attachment]:
        values = await self.repository.list((issuer, subject))
        return [item for item in values if item.status is not AttachmentStatus.UNAVAILABLE]

    async def _get_owned(self, issuer: str, subject: str, attachment_id: UUID) -> Attachment:
        """Read the durable row for internal lifecycle/tombstone workflows."""

        value = await self.repository.get((issuer, subject), attachment_id)
        if value is None:
            raise AttachmentNotFound
        return value

    async def content(self, issuer: str, subject: str, attachment_id: UUID) -> bytes:
        value = await self.get(issuer, subject, attachment_id)
        if not value.available or not value.original_object_key:
            raise AttachmentNotFound
        return await self._read(value.original_object_key)

    async def preview(self, issuer: str, subject: str, attachment_id: UUID) -> bytes:
        value = await self.get(issuer, subject, attachment_id)
        if not value.available or not value.derivative_object_key:
            raise AttachmentNotFound
        return await self._read(value.derivative_object_key)

    async def delete(
        self,
        issuer: str,
        subject: str,
        attachment_id: UUID,
        *,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Attachment:
        operation_started = monotonic()
        delete_fingerprint = f"{attachment_id}:{expected_version}"
        if idempotency_key is not None:
            prior_record = self._delete_idempotency.get((issuer, subject, idempotency_key))
            prior: Attachment | None = None
            if prior_record is not None:
                prior_fingerprint, prior_value = prior_record
                if prior_fingerprint != delete_fingerprint:
                    raise AttachmentConflict("delete idempotency conflict")
                prior = prior_value
            if prior is None:
                request = await self.repository.get_delete_request(
                    (issuer, subject), idempotency_key
                )
                if request is not None:
                    persisted_fingerprint = (
                        f"{request['attachment_id']}:{request.get('expected_version')}"
                    )
                    if persisted_fingerprint != delete_fingerprint:
                        raise AttachmentConflict("delete idempotency conflict")
                    prior = await self._get_owned(
                        issuer, subject, cast(UUID, request["attachment_id"])
                    )
                    self._delete_idempotency[(issuer, subject, idempotency_key)] = (
                        persisted_fingerprint,
                        prior,
                    )
            if prior is not None:
                return prior
        value = await self._get_owned(issuer, subject, attachment_id)
        if expected_version is not None and value.version != expected_version:
            raise AttachmentConflict("attachment version conflict")
        if value.status is AttachmentStatus.UNAVAILABLE:
            return value
        deleting = value.deleting()
        await self._persist(deleting, "attachment.deleting", idempotency_key)
        try:
            if value.original_object_key:
                await self.object_store.delete(value.original_object_key)
            if value.derivative_object_key:
                await self.object_store.delete(value.derivative_object_key)
        except Exception:
            failed = replace(
                deleting,
                status=AttachmentStatus.FAILED,
                failure_code="storage_unavailable",
            )
            await self._persist(failed, "attachment.failed", idempotency_key)
            self._metric(
                "aura.artifacts.attachment_deletion",
                "attachment_deletion_outcome",
                "storage_unavailable",
                duration_ms=(monotonic() - operation_started) * 1000,
                attachment_id=value.id,
                trace_id=uuid5(ATTACHMENT_ID_NAMESPACE, f"delete:{value.id}").hex,
                error_class="object_storage_failed",
            )
            if idempotency_key is not None:
                self._delete_idempotency[(issuer, subject, idempotency_key)] = (
                    delete_fingerprint,
                    failed,
                )
                await self._save_delete_request(
                    issuer,
                    subject,
                    idempotency_key,
                    attachment_id,
                    expected_version,
                    failed,
                )
            return failed
        deleted_candidate = deleting.deleted()
        deleted = await self.repository.delete_with_event(
            (issuer, subject),
            deleted_candidate,
            self._make_event(deleted_candidate, "attachment.deleted", idempotency_key),
        )
        if deleted is None:
            raise AttachmentNotFound
        self._metric(
            "aura.artifacts.attachment_deletion",
            "attachment_deletion_outcome",
            "ok",
            duration_ms=(monotonic() - operation_started) * 1000,
            attachment_id=deleted.id,
            trace_id=uuid5(ATTACHMENT_ID_NAMESPACE, f"delete:{deleted.id}").hex,
        )
        if idempotency_key is not None:
            self._delete_idempotency[(issuer, subject, idempotency_key)] = (
                delete_fingerprint,
                deleted,
            )
            await self._save_delete_request(
                issuer,
                subject,
                idempotency_key,
                attachment_id,
                expected_version,
                deleted,
            )
        self._publish_event(self._make_event(deleted, "attachment.deleted", idempotency_key))
        return deleted

    async def _save_delete_request(
        self,
        issuer: str,
        subject: str,
        idempotency_key: str,
        attachment_id: UUID,
        expected_version: int | None,
        result: Attachment,
    ) -> None:
        value: dict[str, object] = {
            "principal_issuer": issuer,
            "principal_subject": subject,
            "idempotency_key": idempotency_key,
            "attachment_id": attachment_id,
            "expected_version": expected_version,
            "result_version": result.version,
            "result_status": result.status.value,
        }
        await self.repository.save_delete_request(value)

    async def _read(self, key: str) -> bytes:
        parts: list[bytes] = []
        async for piece in self.object_store.get(key):
            parts.append(piece)
        return b"".join(parts)

    def _metric(
        self,
        component: str,
        name: str,
        outcome: str,
        *,
        duration_ms: float = 0.001,
        attachment_id: UUID | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if self.metrics is None:
            return
        try:
            effective_trace_id = trace_id or uuid4().hex
            attributes = {"outcome": outcome}
            if attachment_id is not None:
                attributes["attachment_id"] = str(attachment_id)
            self.metrics.increment(
                component,
                name,
                trace_id=effective_trace_id,
                **attributes,
            )
            operation = {
                "aura.artifacts.attachment_upload": "attachment.upload",
                "aura.artifacts.attachment_processing": "attachment.process",
                "aura.artifacts.attachment_deletion": "attachment.delete",
            }.get(component, "attachment.process")
            self.metrics.record_span(
                component,
                operation,
                max(duration_ms, 0.001),
                trace_id=effective_trace_id,
                span_id=span_id or uuid4().hex[:16],
                parent_span_id=parent_span_id,
                dependency="attachment_store",
                outcome=outcome,
                **({"error_class": error_class} if error_class else {}),
                **({"attachment_id": str(attachment_id)} if attachment_id else {}),
            )
        except Exception:
            pass

    async def _persist(
        self,
        attachment: Attachment,
        event_type: str,
        correlation: str | None,
        upload_id: UUID | None = None,
    ) -> None:
        event = self._make_event(attachment, event_type, correlation, upload_id)
        await self.repository.save_with_event(attachment, event)
        self._publish_event(event)

    def _make_event(
        self,
        attachment: Attachment,
        event_type: str,
        correlation: str | None,
        upload_id: UUID | None = None,
    ) -> AttachmentLifecycleEvent:
        try:
            correlation_id = UUID(correlation) if correlation else None
        except ValueError:
            correlation_id = None
        return AttachmentLifecycleEvent(
            attachment.id,
            attachment.status,
            attachment.version,
            attachment.updated_at,
            event_type,
            upload_id=upload_id,
            correlation_id=correlation_id,
            causation_id=correlation_id,
            stored_object_count=int(bool(attachment.original_object_key))
            + int(bool(attachment.derivative_object_key)),
        )

    def _publish_event(self, event: AttachmentLifecycleEvent) -> None:
        self.lifecycle_events.append(event)
        if self.lifecycle_sink is not None:
            try:
                self.lifecycle_sink(event)
            except Exception:
                pass

    def _emit_event(
        self,
        attachment: Attachment,
        event_type: str,
        correlation: str | None,
        upload_id: UUID | None = None,
    ) -> None:
        self._publish_event(self._make_event(attachment, event_type, correlation, upload_id))
