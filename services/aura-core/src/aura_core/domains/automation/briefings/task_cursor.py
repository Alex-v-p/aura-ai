"""Opaque immutable cursor owned by the briefing source boundary."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

MAX_LENGTH = 1024


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def encode_task_cursor(fence: datetime, created_at: datetime, source_id: UUID) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "f": _utc(fence).isoformat(),
            "c": _utc(created_at).isoformat(),
            "i": str(source_id),
        },
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_task_cursor(cursor: str) -> tuple[datetime, datetime, UUID]:
    if not cursor or len(cursor) > MAX_LENGTH:
        raise ValueError("invalid briefing source cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = cast(Mapping[str, object], json.loads(base64.urlsafe_b64decode(padded).decode()))
        if raw.get("v") != 1:
            raise ValueError("invalid briefing source cursor")
        return (
            datetime.fromisoformat(str(raw["f"])),
            datetime.fromisoformat(str(raw["c"])),
            UUID(str(raw["i"])),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid briefing source cursor") from exc
