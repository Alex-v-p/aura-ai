"""Repository adapters for image attachments."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.artifacts.attachments.persistence import (
    AttachmentDeleteRequestRow,
    AttachmentLifecycleEventRow,
    AttachmentRow,
    AttachmentTombstoneRow,
    AttachmentUploadSessionRow,
)
from aura_core.domains.artifacts.attachments.public import (
    Attachment,
    AttachmentLifecycleEvent,
    AttachmentStatus,
)
from aura_core.platform.outbox import (
    ATTACHMENT_EVENT_TOPIC,
    OutboxCommand,
    SqlOutboxRepository,
)


class MemoryAttachmentRepository:
    def __init__(self) -> None:
        self.attachments: dict[UUID, Attachment] = {}
        self.tombstones: dict[UUID, int] = {}
        self.upload_sessions: dict[tuple[str, str, UUID], dict[str, object]] = {}
        self.delete_requests: dict[tuple[str, str, str], dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def save(self, attachment: Attachment) -> Attachment:
        async with self._lock:
            self.attachments[attachment.id] = attachment
        return attachment

    async def save_with_event(
        self, attachment: Attachment, event: AttachmentLifecycleEvent
    ) -> Attachment:
        return await self.save(attachment)

    async def delete_with_event(
        self,
        owner: tuple[str, str],
        attachment: Attachment,
        event: AttachmentLifecycleEvent,
    ) -> Attachment | None:
        del event
        return await self.save(attachment) if await self.get(owner, attachment.id) else None

    async def get(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None:
        value = self.attachments.get(attachment_id)
        return value if value is not None and value.owner == owner else None

    async def get_any(self, attachment_id: UUID) -> Attachment | None:
        return self.attachments.get(attachment_id)

    async def list(self, owner: tuple[str, str]) -> list[Attachment]:
        return sorted(
            (item for item in self.attachments.values() if item.owner == owner),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    async def delete(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None:
        async with self._lock:
            value = await self.get(owner, attachment_id)
            if value is None:
                return None
            if value.status is AttachmentStatus.UNAVAILABLE:
                return value
            deleted = value.deleted()
            self.attachments[attachment_id] = deleted
            self.tombstones[attachment_id] = deleted.version
            return deleted

    async def create_upload_session(self, value: dict[str, object]) -> dict[str, object]:
        key = (
            str(value["principal_issuer"]),
            str(value["principal_subject"]),
            UUID(str(value["id"])),
        )
        owner_key = (key[0], key[1], str(value["idempotency_key"]))
        prior = next(
            (
                item
                for item in self.upload_sessions.values()
                if (item["principal_issuer"], item["principal_subject"], item["idempotency_key"])
                == owner_key
            ),
            None,
        )
        if prior is not None:
            if prior.get("fingerprint") != value.get("fingerprint"):
                from aura_core.domains.artifacts.attachments.public import AttachmentConflict

                raise AttachmentConflict("upload idempotency conflict")
            return dict(prior)
        self.upload_sessions[key] = dict(value)
        return dict(value)

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> dict[str, object] | None:
        value = self.upload_sessions.get((*owner, upload_id))
        return dict(value) if value is not None else None

    async def complete_upload_session(
        self, owner: tuple[str, str], upload_id: UUID, expected_version: int = 1
    ) -> None:
        async with self._lock:
            value = self.upload_sessions.get((*owner, upload_id))
            if value is None:
                return
            if int(cast(int, value.get("version", 1))) != expected_version:
                from aura_core.domains.artifacts.attachments.public import AttachmentConflict

                raise AttachmentConflict("upload session version conflict")
            if value.get("status") == "completed":
                return
            value["status"] = "completed"
            value["version"] = expected_version + 1

    async def get_delete_request(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> dict[str, object] | None:
        value = self.delete_requests.get((*owner, idempotency_key))
        return dict(value) if value is not None else None

    async def save_delete_request(self, value: dict[str, object]) -> None:
        self.delete_requests[
            (
                str(value["principal_issuer"]),
                str(value["principal_subject"]),
                str(value["idempotency_key"]),
            )
        ] = dict(value)


class SqlAttachmentRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        outbox: SqlOutboxRepository | None = None,
    ) -> None:
        self.sessions = sessions
        self.outbox = outbox

    @staticmethod
    def _value(row: AttachmentRow) -> Attachment:
        return Attachment(
            id=row.id,
            issuer=row.principal_issuer,
            subject=row.principal_subject,
            filename=row.filename,
            media_type=row.media_type,
            byte_count=row.byte_count,
            digest=row.digest,
            original_object_key=row.original_object_key,
            derivative_object_key=row.derivative_object_key,
            width=row.width,
            height=row.height,
            status=AttachmentStatus(row.status),
            version=row.version,
            failure_code=row.failure_code,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def save(self, attachment: Attachment) -> Attachment:
        async with self.sessions() as session, session.begin():
            row = await session.get(AttachmentRow, attachment.id, with_for_update=True)
            values = dict(
                id=attachment.id,
                principal_issuer=attachment.issuer,
                principal_subject=attachment.subject,
                filename=attachment.filename,
                media_type=attachment.media_type,
                byte_count=attachment.byte_count,
                digest=attachment.digest,
                original_object_key=attachment.original_object_key,
                derivative_object_key=attachment.derivative_object_key,
                width=attachment.width,
                height=attachment.height,
                status=attachment.status.value,
                version=attachment.version,
                failure_code=attachment.failure_code,
                created_at=attachment.created_at,
                updated_at=attachment.updated_at,
            )
            if row is None:
                session.add(AttachmentRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return attachment

    async def save_with_event(
        self, attachment: Attachment, event: AttachmentLifecycleEvent
    ) -> Attachment:
        async with self.sessions() as session, session.begin():
            row = await session.get(AttachmentRow, attachment.id, with_for_update=True)
            values = dict(
                id=attachment.id,
                principal_issuer=attachment.issuer,
                principal_subject=attachment.subject,
                filename=attachment.filename,
                media_type=attachment.media_type,
                byte_count=attachment.byte_count,
                digest=attachment.digest,
                original_object_key=attachment.original_object_key,
                derivative_object_key=attachment.derivative_object_key,
                width=attachment.width,
                height=attachment.height,
                status=attachment.status.value,
                version=attachment.version,
                failure_code=attachment.failure_code,
                created_at=attachment.created_at,
                updated_at=attachment.updated_at,
            )
            if row is None:
                session.add(AttachmentRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await self._stage_lifecycle(session, event)
        return attachment

    async def delete_with_event(
        self,
        owner: tuple[str, str],
        attachment: Attachment,
        event: AttachmentLifecycleEvent,
    ) -> Attachment | None:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(AttachmentRow).where(
                    AttachmentRow.id == attachment.id,
                    AttachmentRow.principal_issuer == owner[0],
                    AttachmentRow.principal_subject == owner[1],
                )
            )
            if row is None:
                return None
            deleted = attachment
            for key, value in {
                "status": deleted.status.value,
                "version": deleted.version,
                "original_object_key": deleted.original_object_key,
                "derivative_object_key": deleted.derivative_object_key,
                "failure_code": deleted.failure_code,
                "updated_at": deleted.updated_at,
            }.items():
                setattr(row, key, value)
            await self._stage_lifecycle(session, event)
            session.add(
                AttachmentTombstoneRow(attachment_id=deleted.id, version=deleted.version)
            )
            return deleted

    async def _stage_lifecycle(
        self, session: AsyncSession, event: AttachmentLifecycleEvent
    ) -> None:
        existing = await session.get(AttachmentLifecycleEventRow, event.event_id)
        if existing is None:
            session.add(
                AttachmentLifecycleEventRow(
                    event_id=event.event_id,
                    attachment_id=event.attachment_id,
                    upload_id=event.upload_id,
                    status=event.payload()["status"],
                    attachment_version=event.version,
                    stored_object_count=event.stored_object_count,
                    occurred_at=event.occurred_at,
                    correlation_id=event.correlation_id or event.event_id,
                    causation_id=event.causation_id or event.event_id,
                )
            )
        if self.outbox is not None:
            command = OutboxCommand(
                event.event_id,
                ATTACHMENT_EVENT_TOPIC,
                None,
                None,
                event.occurred_at,
                event.correlation_id or event.event_id,
                event.causation_id or event.event_id,
                schema_version=1,
                aggregate_type="artifacts.attachment",
                aggregate_id=event.attachment_id,
                event_payload=event.payload(),
            )
            await self.outbox.stage_command(session, command=command)

    async def get(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(AttachmentRow).where(
                    AttachmentRow.id == attachment_id,
                    AttachmentRow.principal_issuer == owner[0],
                    AttachmentRow.principal_subject == owner[1],
                )
            )
            return self._value(row) if row else None

    async def list(self, owner: tuple[str, str]) -> list[Attachment]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(AttachmentRow)
                    .where(
                        AttachmentRow.principal_issuer == owner[0],
                        AttachmentRow.principal_subject == owner[1],
                    )
                    .order_by(AttachmentRow.created_at.desc(), AttachmentRow.id.desc())
                )
            ).all()
            return [self._value(row) for row in rows]

    async def delete(self, owner: tuple[str, str], attachment_id: UUID) -> Attachment | None:
        value = await self.get(owner, attachment_id)
        if value is None:
            return None
        return await self.save(value.deleted())

    @staticmethod
    def _session_value(row: AttachmentUploadSessionRow) -> dict[str, object]:
        return {
            "id": row.id,
            "attachment_id": row.attachment_id,
            "principal_issuer": row.principal_issuer,
            "principal_subject": row.principal_subject,
            "filename": row.filename,
            "media_type": row.media_type,
            "byte_count": row.byte_count,
            "idempotency_key": row.idempotency_key,
            "fingerprint": row.fingerprint,
            "status": row.status,
            "version": row.version,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
        }

    async def create_upload_session(self, value: dict[str, object]) -> dict[str, object]:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(AttachmentUploadSessionRow).where(
                    AttachmentUploadSessionRow.principal_issuer == value["principal_issuer"],
                    AttachmentUploadSessionRow.principal_subject == value["principal_subject"],
                    AttachmentUploadSessionRow.idempotency_key == value["idempotency_key"],
                )
            )
            if row is None:
                row = AttachmentUploadSessionRow(**value)
                session.add(row)
                await session.flush()
            elif row.fingerprint != value.get("fingerprint"):
                from aura_core.domains.artifacts.attachments.public import AttachmentConflict

                raise AttachmentConflict("upload idempotency conflict")
            return self._session_value(row)

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> dict[str, object] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(AttachmentUploadSessionRow).where(
                    AttachmentUploadSessionRow.id == upload_id,
                    AttachmentUploadSessionRow.principal_issuer == owner[0],
                    AttachmentUploadSessionRow.principal_subject == owner[1],
                )
            )
            return self._session_value(row) if row else None

    async def complete_upload_session(
        self, owner: tuple[str, str], upload_id: UUID, expected_version: int = 1
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(AttachmentUploadSessionRow)
                .where(
                    AttachmentUploadSessionRow.id == upload_id,
                    AttachmentUploadSessionRow.principal_issuer == owner[0],
                    AttachmentUploadSessionRow.principal_subject == owner[1],
                )
                .with_for_update()
            )
            if row is None:
                return
            if row.version != expected_version:
                from aura_core.domains.artifacts.attachments.public import AttachmentConflict

                raise AttachmentConflict("upload session version conflict")
            if row.status == "completed":
                return
            row.status = "completed"
            row.version = expected_version + 1

    async def get_delete_request(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> dict[str, object] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(AttachmentDeleteRequestRow).where(
                    AttachmentDeleteRequestRow.principal_issuer == owner[0],
                    AttachmentDeleteRequestRow.principal_subject == owner[1],
                    AttachmentDeleteRequestRow.idempotency_key == idempotency_key,
                )
            )
            if row is None:
                return None
            return {
                "principal_issuer": row.principal_issuer,
                "principal_subject": row.principal_subject,
                "idempotency_key": row.idempotency_key,
                "attachment_id": row.attachment_id,
                "expected_version": row.expected_version,
                "result_version": row.result_version,
                "result_status": row.result_status,
            }

    async def save_delete_request(self, value: dict[str, object]) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(
                AttachmentDeleteRequestRow,
                (
                    value["principal_issuer"],
                    value["principal_subject"],
                    value["idempotency_key"],
                ),
            )
            if row is None:
                session.add(AttachmentDeleteRequestRow(**value))
            else:
                row.attachment_id = UUID(str(value["attachment_id"]))
                row.expected_version = cast(int | None, value.get("expected_version"))
                row.result_version = int(cast(str | int, value["result_version"]))
                row.result_status = str(value["result_status"])
