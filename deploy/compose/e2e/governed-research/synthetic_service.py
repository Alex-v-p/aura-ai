"""Synthetic, no-credential HTTP dependencies for the governed research runner.

This process is intentionally not a research provider implementation.  It is a
small health and fixture boundary used by the disposable Compose topology so
the acceptance harness can prove service separation without enabling live web
egress or replaying production content.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "aura-synthetic-e2e/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        if self.path.rstrip("/") not in {"", "/health/live"}:
            self.send_error(404)
            return
        self._write({"status": "ok", "fixture": "synthetic", "role": _role()})

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        if self.path not in {"/v1/search", "/v1/fetch"}:
            self.send_error(404)
            return
        # Never echo a query, URL, body, or header.  The fixture boundary only
        # needs a deterministic shape and a health signal.
        self._write(
            {
                "schemaVersion": 1,
                "fixture": "synthetic",
                "role": _role(),
                "result": "bounded",
            }
        )

    def log_message(self, format: str, *_args: object) -> None:
        _ = format
        return

    def _write(self, value: dict[str, str | int]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _role() -> str:
    return os.environ.get("AURA_SYNTHETIC_ROLE", "fixture")


def main() -> None:
    port = int(os.environ.get("AURA_SYNTHETIC_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
