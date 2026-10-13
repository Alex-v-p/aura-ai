"""AURA-0068 document-format, upload, lifecycle, and retrieval evaluations.

These tests use only the public knowledge.documents boundary and deliberately
keep object-store and embedding dependencies in memory.  They assert behavior
and privacy contracts, rather than SQL or provider implementation details.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, fields
from typing import cast

import pytest
from aura_core.domains.knowledge.documents.parser import (
    DocumentParseError,
    parse_document,
    validate_format,
)
from aura_core.domains.knowledge.documents.public import (
    MAX_UPLOAD_BYTES,
    DocumentLifecycleEvent,
    DocumentNotFound,
    DocumentService,
    DocumentStatus,
    DocumentValidationError,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.runtime.models.ports import EmbeddingResult, ProviderTraceContext

OWNER = ("https://issuer.example", "owner-a")
OTHER_OWNER = ("https://issuer.example", "owner-b")


class CountingStore(MemoryDocumentObjectStore):
    """Consumes streams without retaining a huge test body."""

    def __init__(self) -> None:
        super().__init__()
        self.bytes_seen = 0
        self.keys: list[str] = []

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        self.keys.append(key)
        total = 0
        async for piece in stream:
            total += len(piece)
        self.bytes_seen += total
        return total


class RecordingEmbedder:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, ProviderTraceContext | None]] = []
        self.error = error

    async def embed(
        self, model_id: str, text: str, *, context: ProviderTraceContext | None = None
    ) -> EmbeddingResult:
        self.calls.append((model_id, text, context))
        if self.error is not None:
            raise self.error
        return EmbeddingResult(
            vector=(0.1, 0.2, 0.3),
            model_id=model_id,
            model_revision="rev-1",
            dimension=3,
            model_digest="a" * 64,
            digest="b" * 64,
        )


@dataclass
class RecordingEvents:
    records: list[tuple[str, object]]

    def __init__(self) -> None:
        self.records = []

    async def emit(self, event: str, *, document: object) -> None:
        self.records.append((event, document))


def _docx(*, members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


def _valid_docx(text: str = "bounded document text") -> bytes:
    return _docx(
        members={
            "[Content_Types].xml": b"<Types xmlns='x'/>",
            "word/document.xml": (
                "<document xmlns='urn:test'><p><t>" + text + "</t></p></document>"
            ).encode(),
        }
    )


def _service(
    *,
    store: MemoryDocumentObjectStore | None = None,
    embedder: RecordingEmbedder | None = None,
    events: RecordingEvents | None = None,
) -> DocumentService:
    return DocumentService(
        MemoryDocumentRepository(),
        store or MemoryDocumentObjectStore(),
        embedder=embedder,
        events=events,
    )


@pytest.mark.parametrize(
    ("filename", "media_type", "header"),
    [
        ("notes.md", "text/markdown", b"# title"),
        ("notes.txt", "text/plain", b"plain text"),
        ("notes.pdf", "application/pdf", b"%PDF-1.7"),
        (
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04",
        ),
    ],
)
def test_supported_formats_require_matching_extension_media_type_and_signature(
    filename: str, media_type: str, header: bytes
) -> None:
    assert validate_format(filename, media_type, header)


@pytest.mark.parametrize(
    ("filename", "media_type", "header"),
    [
        ("notes.txt", "application/octet-stream", b"plain text"),
        ("notes.md", "application/octet-stream", b"# heading"),
        ("notes.pdf", "application/octet-stream", b"%PDF-1.7"),
        (
            "notes.docx",
            "application/octet-stream",
            b"PK\x03\x04",
        ),
    ],
)
def test_supported_extensions_require_their_exact_declared_media_type(
    filename: str, media_type: str, header: bytes
) -> None:
    with pytest.raises(DocumentParseError):
        validate_format(filename, media_type, header)


@pytest.mark.parametrize(
    ("filename", "media_type", "body"),
    [
        ("notes.pdf", "text/plain", b"%PDF-1.7"),
        ("notes.pdf", "application/pdf", b"not a pdf"),
        (
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"not zip",
        ),
        ("notes.exe", "application/octet-stream", b"MZ"),
        ("notes.txt", "text/plain", b"\x00binary"),
    ],
)
def test_format_spoofing_and_unsupported_inputs_are_rejected(
    filename: str, media_type: str, body: bytes
) -> None:
    with pytest.raises(DocumentParseError):
        parse_document(body, filename=filename, media_type=media_type)


def test_malformed_and_encrypted_pdf_are_rejected() -> None:
    with pytest.raises(DocumentParseError):
        parse_document(
            b"%PDF-1.7\nnot a valid xref", filename="bad.pdf", media_type="application/pdf"
        )

    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(DocumentParseError, match="encrypted"):
        parse_document(output.getvalue(), filename="secret.pdf", media_type="application/pdf")


def test_docx_macro_archive_and_expansion_limits_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    macro = _valid_docx()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", b"<document/>")
        archive.writestr("word/vbaProject.bin", b"macro")
    with pytest.raises(DocumentParseError, match="macro"):
        parse_document(
            output.getvalue(),
            filename="macro.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    monkeypatch.setattr(
        "aura_core.domains.knowledge.documents.parser.MAX_ARCHIVE_EXPANSION", 8
    )
    with pytest.raises(DocumentParseError, match="expansion"):
        parse_document(
            macro,
            filename="large.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_malformed_docx_xml_is_rejected() -> None:
    malformed = _docx(members={"word/document.xml": b"<document><broken"})
    with pytest.raises(DocumentParseError, match="malformed"):
        parse_document(
            malformed,
            filename="broken.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


@pytest.mark.asyncio
async def test_exact_upload_boundary_is_streamed_and_one_byte_over_limit_fails() -> None:
    store = CountingStore()
    service = _service(store=store)

    async def body(size: int) -> AsyncIterator[bytes]:
        first = b"header\n"
        yield first
        remaining = size - len(first)
        chunk = b"x" * (1024 * 1024)
        while remaining:
            piece = chunk if remaining >= len(chunk) else chunk[:remaining]
            yield piece
            remaining -= len(piece)

    accepted = await service.upload(
        *OWNER,
        filename="boundary.txt",
        media_type="text/plain",
        stream=body(MAX_UPLOAD_BYTES),
    )
    assert accepted.status is DocumentStatus.QUEUED
    assert accepted.byte_count == MAX_UPLOAD_BYTES
    assert store.bytes_seen == MAX_UPLOAD_BYTES

    with pytest.raises(DocumentValidationError, match="100000000"):
        await service.upload(
            *OWNER,
            filename="over.txt",
            media_type="text/plain",
            stream=body(MAX_UPLOAD_BYTES + 1),
        )


@pytest.mark.asyncio
async def test_duplicate_and_idempotent_uploads_retain_one_object_and_document() -> None:
    store = CountingStore()
    service = _service(store=store)
    payload = b"same content\n"
    first = await service.upload(
        *OWNER,
        filename="one.txt",
        media_type="text/plain",
        stream=payload,
        idempotency_key="same-command",
    )
    replay = await service.upload(
        *OWNER,
        filename="one.txt",
        media_type="text/plain",
        stream=payload,
        idempotency_key="same-command",
    )
    duplicate = await service.upload(
        *OWNER,
        filename="renamed.txt",
        media_type="text/plain",
        stream=payload,
    )
    assert replay.id == duplicate.id == first.id
    assert len(await service.list(*OWNER)) == 1
    assert len(store.keys) == 3  # duplicate bytes are streamed then discarded

    with pytest.raises(DocumentValidationError, match="already used"):
        await service.upload(
            *OWNER,
            filename="different.txt",
            media_type="text/plain",
            stream=b"different",
            idempotency_key="same-command",
        )


@pytest.mark.asyncio
async def test_owner_isolation_applies_to_list_detail_search_and_upload_sessions() -> None:
    service = _service()
    document = await service.upload(
        *OWNER,
        filename="private.txt",
        media_type="text/plain",
        stream=b"owner a secret marker",
    )
    await service.process(document.id)
    assert await service.list(*OTHER_OWNER) == []
    assert await service.search(*OTHER_OWNER, "secret") == []
    with pytest.raises(DocumentNotFound):
        await service.get(*OTHER_OWNER, document.id)

    session = await service.create_upload_session(
        *OWNER, filename="session.txt", media_type="text/plain"
    )
    with pytest.raises(DocumentNotFound):
        await service.complete_upload(
            session.id,
            b"private",
            issuer=OTHER_OWNER[0],
            subject=OTHER_OWNER[1],
        )


@pytest.mark.asyncio
async def test_processing_is_atomic_for_search_and_persists_provenance_and_embedding_identity(
) -> None:
    embedder = RecordingEmbedder()
    service = _service(embedder=embedder)
    document = await service.upload(
        *OWNER,
        filename="knowledge.txt",
        media_type="text/plain",
        stream=b"alpha beta provenance marker",
    )
    assert await service.search(*OWNER, "alpha") == []
    ready = await service.process(document.id)
    assert ready.status is DocumentStatus.READY
    hits = await service.search(*OWNER, "alpha")
    assert hits and hits[0].document_id == document.id
    assert hits[0].filename == "knowledge.txt"
    assert hits[0].locator.startswith("section:")
    assert hits[0].evidence_id
    process_calls = [
        call
        for call in embedder.calls
        if call[2] is not None and call[2].job_id == str(document.id)
    ]
    assert process_calls
    assert len(await service.repository.chunks(document.id)) == len(process_calls)


@pytest.mark.asyncio
async def test_failed_processing_can_retry_and_duplicate_delivery_is_idempotent() -> None:
    failing = RecordingEmbedder(error=TimeoutError("embedding timeout"))
    service = _service(embedder=failing)
    document = await service.upload(
        *OWNER,
        filename="retry.txt",
        media_type="text/plain",
        stream=b"retry me",
    )
    failed = await service.process(document.id)
    assert failed.status is DocumentStatus.FAILED
    assert failed.failure_code == "timeout"
    assert await service.search(*OWNER, "retry") == []
    queued = await service.retry(*OWNER, document.id, expected_version=failed.version)
    assert queued.status is DocumentStatus.QUEUED
    assert queued.version == failed.version + 1


@pytest.mark.asyncio
async def test_delete_excludes_document_immediately_and_retains_retryable_state_on_store_failure(
) -> None:
    class FailingDeleteStore(MemoryDocumentObjectStore):
        async def delete(self, key: str) -> None:
            del key
            raise RuntimeError("object store unavailable")

    service = _service(store=FailingDeleteStore())
    document = await service.upload(
        *OWNER,
        filename="delete.txt",
        media_type="text/plain",
        stream=b"delete me",
    )
    await service.process(document.id)
    deleting = await service.delete(*OWNER, document.id, expected_version=3)
    assert deleting.status is DocumentStatus.DELETING
    assert await service.search(*OWNER, "delete") == []
    assert await service.get(*OWNER, document.id)


@pytest.mark.asyncio
async def test_document_events_must_be_metadata_only() -> None:
    events = RecordingEvents()
    service = _service(events=events)
    await service.upload(
        *OWNER,
        filename="private.txt",
        media_type="text/plain",
        stream=b"PRIVATE CONTENT",
    )
    assert events
    event_payload = cast(DocumentLifecycleEvent, events.records[0][1])
    payload = {field.name for field in fields(event_payload)}
    prohibited = {"filename", "digest", "object_key", "issuer", "subject"}
    assert not prohibited & payload


@pytest.mark.asyncio
async def test_lifecycle_events_are_identifier_only_ordered_and_replayable() -> None:
    events = RecordingEvents()
    service = _service(events=events)
    document = await service.upload(
        *OWNER,
        filename="lineage.txt",
        media_type="text/plain",
        stream=b"lineage",
    )
    ready = await service.process(document.id)
    assert [event for event, _payload in events.records] == ["document.queued", "document.ready"]
    queued = cast(DocumentLifecycleEvent, events.records[0][1])
    completed = cast(DocumentLifecycleEvent, events.records[1][1])
    assert queued.document_id == completed.document_id == ready.id
    assert queued.version < completed.version
    assert queued.occurred_at <= completed.occurred_at
    assert {field.name for field in fields(queued)} == {
        "document_id",
        "status",
        "version",
        "occurred_at",
    }
