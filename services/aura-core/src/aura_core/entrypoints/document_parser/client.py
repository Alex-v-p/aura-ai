"""Narrow client for the no-ambient-access parser subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from aura_core.domains.knowledge.documents.parser import DocumentParseError, ParsedDocument

MAX_PARSER_OUTPUT = 10 * 1024 * 1024 + 1024


class SubprocessDocumentParser:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def parse(self, data: bytes, *, filename: str, media_type: str) -> ParsedDocument:
        environment = {
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "aura_core.entrypoints.document_parser.main",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            close_fds=True,
        )
        try:
            metadata = json.dumps(
                {"filename": filename, "media_type": media_type},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            stdout, _ = await asyncio.wait_for(
                process.communicate(metadata + b"\n\n" + data), self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DocumentParseError("parser timed out") from exc
        if process.returncode != 0 or len(stdout) > MAX_PARSER_OUTPUT:
            raise DocumentParseError("isolated parser failed")
        try:
            payload = json.loads(stdout.decode("utf-8"))
            text = payload["text"]
            parsed_media_type = payload["mediaType"]
            if not isinstance(text, str) or not isinstance(parsed_media_type, str):
                raise ValueError
            return ParsedDocument(text, parsed_media_type)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DocumentParseError("isolated parser returned malformed output") from exc
