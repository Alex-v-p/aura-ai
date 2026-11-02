"""HTTP adapter for the isolated document parser service."""

from __future__ import annotations

import httpx

from aura_core.domains.knowledge.documents.parser import DocumentParseError, ParsedDocument

MAX_PARSER_OUTPUT = 10 * 1024 * 1024 + 1024


class HttpDocumentParser:
    def __init__(self, endpoint: str, *, timeout_seconds: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def parse(self, data: bytes, *, filename: str, media_type: str) -> ParsedDocument:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.endpoint}/parse",
                    content=data,
                    headers={
                        "content-type": media_type,
                        "x-document-filename": filename,
                    },
                )
                response.raise_for_status()
                if len(response.content) > MAX_PARSER_OUTPUT:
                    raise DocumentParseError("isolated parser output exceeds limit")
                payload = response.json()
        except DocumentParseError:
            raise
        except Exception as exc:
            raise DocumentParseError("isolated parser unavailable") from exc
        text = payload.get("text")
        parsed_media_type = payload.get("media_type")
        if not isinstance(text, str) or not isinstance(parsed_media_type, str):
            raise DocumentParseError("isolated parser returned malformed output")
        return ParsedDocument(text, parsed_media_type)
