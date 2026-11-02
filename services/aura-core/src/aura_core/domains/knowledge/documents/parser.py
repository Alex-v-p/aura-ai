"""Safe, bounded parsers for the v1 document formats."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, cast

from aura_core.domains.knowledge.documents.chunking import MAX_EXTRACTED_TEXT

MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_EXPANSION = 100 * 1024 * 1024
MAX_PDF_PAGES = 10_000


class DocumentParseError(ValueError):
    """Input is unsupported, malformed, or exceeds parser limits."""


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    media_type: str


class LocalDocumentParser:
    """Development/test adapter for the parser port."""

    async def parse(self, data: bytes, *, filename: str, media_type: str) -> ParsedDocument:
        return parse_document(data, filename=filename, media_type=media_type)


def validate_format(filename: str, media_type: str, header: bytes) -> str:
    suffix = PurePath(filename).suffix.casefold()
    if suffix in {".md", ".markdown"}:
        kind = "text/markdown"
    elif suffix == ".txt":
        kind = "text/plain"
    elif suffix == ".pdf":
        kind = "application/pdf"
    elif suffix == ".docx":
        kind = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        raise DocumentParseError("unsupported document format")
    declared = media_type.casefold().split(";", 1)[0].strip()
    allowed = {kind}
    if declared not in allowed:
        raise DocumentParseError("declared media type does not match filename")
    if kind == "application/pdf" and not header.startswith(b"%PDF-"):
        raise DocumentParseError("invalid PDF signature")
    if kind == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        if not header.startswith(b"PK"):
            raise DocumentParseError("invalid DOCX archive signature")
    if kind in {"text/markdown", "text/plain"} and b"\x00" in header:
        raise DocumentParseError("binary content is not text")
    return kind


def parse_document(data: bytes, *, filename: str, media_type: str) -> ParsedDocument:
    kind = validate_format(filename, media_type, data[:16])
    if kind in {"text/plain", "text/markdown"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentParseError("text document is not UTF-8") from exc
    elif kind == "application/pdf":
        text = _parse_pdf(data)
    else:
        text = _parse_docx(data)
    if len(text) > MAX_EXTRACTED_TEXT:
        raise DocumentParseError("extracted text exceeds the configured limit")
    return ParsedDocument(text, kind)


def _parse_pdf(data: bytes) -> str:
    # PDF parsing is deliberately fail-closed: if the bounded parser dependency
    # is unavailable or rejects the bytes, arbitrary data is never treated as text.
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = cast(Any, PdfReader(io.BytesIO(data), strict=True))
        if reader.is_encrypted:
            raise DocumentParseError("encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentParseError("PDF has too many pages")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("PDF parsing failed") from exc


def _parse_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise DocumentParseError("DOCX has too many archive members")
            expanded = sum(item.file_size for item in members)
            if expanded > MAX_ARCHIVE_EXPANSION:
                raise DocumentParseError("DOCX archive expansion exceeds limit")
            if any(item.filename.casefold().endswith((".vbaproject", ".bin")) for item in members):
                raise DocumentParseError("macro-enabled documents are not supported")
            xml = archive.read("word/document.xml")
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("DOCX parsing failed") from exc
    try:
        from xml.etree import ElementTree

        root = ElementTree.fromstring(xml)
        return " ".join(
            item.text or "" for item in root.iter() if item.tag.casefold().endswith("}t")
        )
    except Exception as exc:
        raise DocumentParseError("DOCX XML is malformed") from exc
