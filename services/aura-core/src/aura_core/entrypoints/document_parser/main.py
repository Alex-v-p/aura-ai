"""Minimal parser subprocess entrypoint with no Core application access."""

from __future__ import annotations

import json
import sys

from aura_core.domains.knowledge.documents.parser import parse_document


def main() -> int:
    if len(sys.argv) != 1:
        return 64
    raw = sys.stdin.buffer.read(10 * 1024 * 1024 + 16 * 1024 + 1)
    header, separator, data = raw.partition(b"\n\n")
    if not separator:
        return 64
    try:
        metadata = json.loads(header.decode("utf-8"))
        filename = metadata["filename"]
        media_type = metadata["media_type"]
        if not isinstance(filename, str) or not isinstance(media_type, str):
            return 64
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return 64
    if len(data) > 10 * 1024 * 1024:
        return 65
    try:
        parsed = parse_document(data, filename=filename, media_type=media_type)
    except Exception:
        return 66
    sys.stdout.write(
        json.dumps(
            {"text": parsed.text, "mediaType": parsed.media_type},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
