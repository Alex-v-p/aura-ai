"""Owner-authenticated document API contract and isolation tests."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentCommandSink,
    DocumentService,
    DocumentStatus,
    DocumentValidationError,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from fastapi import FastAPI

from conftest import owner_client


def _command_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Origin": "https://aura.dev.example"}


class RecordingDeleteRepository(MemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.transitions: list[Document] = []

    async def transition_deleting(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> Document:
        self.transitions.append(document)
        await command_sink.publish(document.id, operation="delete")
        return await super().transition_deleting(
            document,
            expected_version=expected_version,
            command_sink=command_sink,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )


class RecordingCommandSink:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, document_id: object, *, operation: str = "process") -> None:
        self.published.append((document_id, operation))

    async def stage(self, *_: object, **__: object) -> None:
        return None

    async def stage_event(self, *_: object, **__: object) -> None:
        return None


@pytest.mark.asyncio
async def test_document_mutations_require_csrf_and_origin_and_never_cache_private_data(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        missing = await client.post(
            "/api/v1/documents/uploads",
            json={"filename": "notes.txt", "mediaType": "text/plain"},
        )
        assert missing.status_code == 403
        assert missing.headers["cache-control"] == "no-store"

        wrong_origin = await client.post(
            "/api/v1/documents/uploads",
            headers={"X-CSRF-Token": session.csrf_token, "Origin": "https://evil.example"},
            json={"filename": "notes.txt", "mediaType": "text/plain"},
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.headers["cache-control"] == "no-store"

        created = await client.post(
            "/api/v1/documents/uploads",
            headers={**_command_headers(session.csrf_token), "Idempotency-Key": str(uuid4())},
            json={"filename": "notes.txt", "mediaType": "text/plain", "byteCount": 19},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        session_payload = created.json()
        assert set(session_payload) >= {
            "id",
            "documentId",
            "filename",
            "mediaType",
            "byteCount",
            "status",
            "version",
            "createdAt",
            "expiresAt",
        }
        assert session_payload["byteCount"] == 19
        assert session_payload["status"] == "uploading"
        assert session_payload["version"] == 1
        assert session_payload["expiresAt"] > session_payload["createdAt"]
        assert "uploadId" not in session_payload
        assert "objectKey" not in session_payload
        assert "credential" not in repr(created.json()).lower()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_document_upload_process_search_download_and_owner_isolation(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    other, _ = await owner_client(api_app, subject="another-subject")
    try:
        created = await client.post(
            "/api/v1/documents/uploads",
            headers={**_command_headers(session.csrf_token), "Idempotency-Key": str(uuid4())},
            json={"filename": "notes.txt", "mediaType": "text/plain", "byteCount": 21},
        )
        upload_session = created.json()
        upload_id = UUID(upload_session["id"])
        uploaded = await client.put(
            f"/api/v1/documents/uploads/{upload_id}/content",
            headers={
                **_command_headers(session.csrf_token),
                "Content-Type": "text/plain",
                "Content-Length": "21",
                "X-Expected-Version": "1",
                "Idempotency-Key": str(uuid4()),
            },
            content=b"owner private marker!",
        )
        assert uploaded.status_code == 202
        receipt = uploaded.json()
        assert set(receipt) == {"document", "duplicate"}
        assert receipt["duplicate"] is False
        assert "id" not in receipt
        document_id = UUID(receipt["document"]["id"])
        await api_app.state.aura.document_service.process(document_id)

        detail = await client.get(f"/api/v1/documents/{document_id}")
        assert detail.status_code == 200
        assert detail.json()["chunkCount"] == 1

        found = await client.post(
            "/api/v1/documents/search",
            headers=_command_headers(session.csrf_token),
            json={"query": "private marker", "limit": 20},
        )
        assert found.status_code == 200
        result = found.json()["items"][0]
        assert result["documentId"] == str(document_id)
        assert result["displayName"] == "notes.txt"
        assert result["locator"] == {
            "kind": "section",
            "sectionOrdinal": 1,
            "heading": None,
        }
        assert "owner private marker" in found.text

        downloaded = await client.get(f"/api/v1/documents/{document_id}/content")
        assert downloaded.status_code == 200
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"

        duplicate_session = await client.post(
            "/api/v1/documents/uploads",
            headers={**_command_headers(session.csrf_token), "Idempotency-Key": str(uuid4())},
            json={"filename": "renamed.txt", "mediaType": "text/plain", "byteCount": 21},
        )
        duplicate_upload = await client.put(
            f"/api/v1/documents/uploads/{duplicate_session.json()['id']}/content",
            headers={
                **_command_headers(session.csrf_token),
                "Content-Type": "text/plain",
                "Content-Length": "21",
                "X-Expected-Version": "1",
                "Idempotency-Key": str(uuid4()),
            },
            content=b"owner private marker!",
        )
        assert duplicate_upload.status_code == 202
        assert duplicate_upload.json()["duplicate"] is True
        assert duplicate_upload.json()["document"]["id"] == str(document_id)

        forbidden_list = await other.get("/api/v1/documents")
        assert forbidden_list.status_code == 403
        forbidden_detail = await other.get(f"/api/v1/documents/{document_id}")
        assert forbidden_detail.status_code == 403
    finally:
        await client.aclose()
        await other.aclose()


@pytest.mark.asyncio
async def test_document_retry_and_delete_replay_same_idempotency_key_without_duplicate_transition(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        service = api_app.state.aura.document_service
        document = await service.upload(
            session.principal.issuer,
            session.principal.subject,
            filename="replay.txt",
            media_type="text/plain",
            stream=b"replay content",
        )
        failed = await service.repository.save(
            replace(
                document,
                status=DocumentStatus.FAILED,
                version=2,
                failure_code="parse_failed",
            )
        )
        retry_headers = {
            **_command_headers(session.csrf_token),
            "Idempotency-Key": str(uuid4()),
        }
        first_retry = await client.post(
            f"/api/v1/documents/{failed.id}/retry",
            headers=retry_headers,
            json={"expectedVersion": failed.version},
        )
        replay_retry = await client.post(
            f"/api/v1/documents/{failed.id}/retry",
            headers=retry_headers,
            json={"expectedVersion": failed.version},
        )
        assert first_retry.status_code == replay_retry.status_code == 202
        assert first_retry.json()["id"] == replay_retry.json()["id"] == str(failed.id)
        stale_retry = await client.post(
            f"/api/v1/documents/{failed.id}/retry",
            headers={**_command_headers(session.csrf_token), "Idempotency-Key": str(uuid4())},
            json={"expectedVersion": failed.version},
        )
        assert stale_retry.status_code == 409

        delete_document = await service.upload(
            session.principal.issuer,
            session.principal.subject,
            filename="delete-replay.txt",
            media_type="text/plain",
            stream=b"delete replay",
        )
        delete_headers = {
            **_command_headers(session.csrf_token),
            "Idempotency-Key": str(uuid4()),
        }
        first_delete = await client.request(
            "DELETE",
            f"/api/v1/documents/{delete_document.id}",
            headers=delete_headers,
            json={"expectedVersion": delete_document.version},
        )
        replay_delete = await client.request(
            "DELETE",
            f"/api/v1/documents/{delete_document.id}",
            headers=delete_headers,
            json={"expectedVersion": delete_document.version},
        )
        assert first_delete.status_code == replay_delete.status_code == 202
        assert first_delete.json() == replay_delete.json()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_delete_api_enqueues_and_worker_tombstone_redelivery_is_idempotent(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        repository = RecordingDeleteRepository()
        sink = RecordingCommandSink()
        service = DocumentService(repository, MemoryDocumentObjectStore(), commands=sink)
        api_app.state.aura.document_service = service
        document = await service.upload(
            session.principal.issuer,
            session.principal.subject,
            filename="enqueue-delete.txt",
            media_type="text/plain",
            stream=b"enqueue delete",
        )

        headers = {
            **_command_headers(session.csrf_token),
            "Idempotency-Key": str(uuid4()),
        }
        response = await client.request(
            "DELETE",
            f"/api/v1/documents/{document.id}",
            headers=headers,
            json={"expectedVersion": document.version},
        )
        replay = await client.request(
            "DELETE",
            f"/api/v1/documents/{document.id}",
            headers=headers,
            json={"expectedVersion": document.version},
        )
        assert response.status_code == replay.status_code == 202
        assert response.json() == replay.json()
        assert len(repository.transitions) == 1
        assert [operation for _document_id, operation in sink.published] == [
            "process",
            "delete",
        ]
        assert sink.published[-1][0] == document.id

        from aura_core.entrypoints.worker.documents import DocumentWorker

        worker = DocumentWorker(service)
        settled = cast(Document, await worker.delete(document.id))
        assert settled.status is DocumentStatus.DELETING
        store = cast(MemoryDocumentObjectStore, service.object_store)
        assert document.object_key not in store.objects
        replayed = cast(Document, await worker.delete(document.id))
        assert replayed.status is DocumentStatus.DELETING
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_download_filename_is_header_safe_for_quotes_unicode_and_control_characters(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        service = api_app.state.aura.document_service
        document = await service.upload(
            session.principal.issuer,
            session.principal.subject,
            filename='quote";β;semi;.txt',
            media_type="text/plain",
            stream=b"safe filename",
        )
        response = await client.get(f"/api/v1/documents/{document.id}/content")
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert "filename*=UTF-8''" in disposition
        assert response.headers["x-content-type-options"] == "nosniff"

        with pytest.raises(DocumentValidationError):
            await service.create_upload_session(
                session.principal.issuer,
                session.principal.subject,
                filename="bad\r\nname.txt",
                media_type="text/plain",
                byte_count=1,
            )
        with pytest.raises(DocumentValidationError):
            await service.create_upload_session(
                session.principal.issuer,
                session.principal.subject,
                filename="bad\\name.txt",
                media_type="text/plain",
                byte_count=1,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_document_list_pagination_is_stable_and_cursor_bound(api_app: FastAPI) -> None:
    client, session = await owner_client(api_app)
    try:
        service = api_app.state.aura.document_service
        for index in range(3):
            await service.upload(
                session.principal.issuer,
                session.principal.subject,
                filename=f"page-{index}.txt",
                media_type="text/plain",
                stream=f"page {index}".encode(),
            )
        first = await client.get("/api/v1/documents?limit=1")
        assert first.status_code == 200
        assert len(first.json()["items"]) == 1
        cursor = first.json()["nextCursor"]
        assert cursor
        second = await client.get(f"/api/v1/documents?limit=1&cursor={cursor}")
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1
        assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_document_operations_emit_lineage_spans_and_metadata_only_metrics(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        marker = "PRIVATE-OBSERVATION-MARKER"
        before = len(api_app.state.aura.metrics.snapshot())
        created = await client.post(
            "/api/v1/documents/uploads",
            headers={**_command_headers(session.csrf_token), "Idempotency-Key": str(uuid4())},
            json={"filename": "observed.txt", "mediaType": "text/plain", "byteCount": len(marker)},
        )
        upload = await client.put(
            f"/api/v1/documents/uploads/{created.json()['id']}/content",
            headers={
                **_command_headers(session.csrf_token),
                "Content-Type": "text/plain",
                "Content-Length": str(len(marker)),
                "X-Expected-Version": "1",
                "Idempotency-Key": str(uuid4()),
            },
            content=marker.encode(),
        )
        document_id = UUID(upload.json()["document"]["id"])
        await api_app.state.aura.document_service.process(document_id)
        await client.post(
            "/api/v1/documents/search",
            headers=_command_headers(session.csrf_token),
            json={"query": marker, "limit": 8},
        )
        records = api_app.state.aura.metrics.snapshot()[before:]
        components = {record.component_id for record in records}
        assert {
            "aura.knowledge.document_upload",
            "aura.knowledge.document_processing",
            "aura.knowledge.document_retrieval",
        } <= components
        assert all(marker not in repr(record) for record in records)
        assert all("filename" not in repr(record).casefold() for record in records)
        assert all("object_key" not in repr(record).casefold() for record in records)
    finally:
        await client.aclose()
