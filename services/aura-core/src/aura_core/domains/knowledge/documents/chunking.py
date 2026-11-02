"""Deterministic bounded text chunking."""

from __future__ import annotations

from dataclasses import dataclass

MAX_EXTRACTED_TEXT = 10 * 1024 * 1024
MAX_CHUNKS = 10_000
CHUNK_SIZE = 1_600
CHUNK_OVERLAP = 160


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    ordinal: int
    locator: str


def chunk_text(
    text: str, *, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[TextChunk]:
    """Split text on bounded word boundaries, preserving deterministic offsets."""

    if len(text) > MAX_EXTRACTED_TEXT:
        raise ValueError("extracted text exceeds the configured limit")
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("invalid chunk configuration")
    normalized = " ".join(text.split())
    if not normalized:
        return []
    result: list[TextChunk] = []
    start = 0
    ordinal = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        if end < len(normalized):
            boundary = normalized.rfind(" ", start, end)
            if boundary > start + chunk_size // 2:
                end = boundary
        value = normalized[start:end].strip()
        if value:
            result.append(TextChunk(value, ordinal, f"section:{ordinal + 1}"))
            ordinal += 1
        if len(result) > MAX_CHUNKS:
            raise ValueError("document has too many chunks")
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return result
