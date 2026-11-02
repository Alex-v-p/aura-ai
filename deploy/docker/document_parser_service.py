#!/usr/bin/env python3
"""Small, bounded HTTP boundary for hostile document parsing.

The service deliberately has no application credentials or persistence
adapters. Compose supplies its only network attachment: the private parser
network shared with the Core worker.
"""

from __future__ import annotations

import json
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer

from aura_core.domains.knowledge.documents.parser import (
    MAX_EXTRACTED_TEXT,
    DocumentParseError,
    parse_document,
)

MAX_BODY_BYTES = 100_000_000
PARSE_TIMEOUT_SECONDS = 30


class _ParseTimeout(Exception):
    pass


def _timeout(_signum: int, _frame: object) -> None:
    raise _ParseTimeout


class ParserHandler(BaseHTTPRequestHandler):
    server_version = "AuraDocumentParser/1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(PARSE_TIMEOUT_SECONDS)

    def log_message(self, _format: str, *_args: object) -> None:
        # Document metadata and parse failures must not enter ordinary logs.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/parse":
            self.send_error(404)
            return
        raw_length = self.headers.get("content-length")
        try:
            length = int(raw_length or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "document body exceeds parser limit"})
            return
        filename = self.headers.get("x-document-filename", "")
        media_type = self.headers.get("content-type", "")
        try:
            signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(PARSE_TIMEOUT_SECONDS)
            parsed = parse_document(
                self.rfile.read(length), filename=filename, media_type=media_type
            )
        except (_ParseTimeout, DocumentParseError, ValueError):
            self._send_json(422, {"error": "document parsing failed"})
            return
        finally:
            signal.alarm(0)
        if len(parsed.text) > MAX_EXTRACTED_TEXT:
            self._send_json(422, {"error": "extracted text exceeds parser limit"})
            return
        self._send_json(200, {"media_type": parsed.media_type, "text": parsed.text})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    HTTPServer.allow_reuse_address = False
    server = HTTPServer(("0.0.0.0", 8080), ParserHandler)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
