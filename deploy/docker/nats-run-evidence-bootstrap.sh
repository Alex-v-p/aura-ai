#!/bin/sh
set -eu

# Provision the dedicated run-evidence JetStream topology with the one-shot
# bootstrap identity. Existing resources are reconciled fail-closed so a
# changed retention, subject, or consumer policy cannot be widened silently.
exec /app/.venv/bin/python - <<'PY'
import asyncio
import json
import os
from pathlib import Path

import nats


class JetStreamApiError(RuntimeError):
    def __init__(self, error: object) -> None:
        self.error = error if isinstance(error, dict) else {}
        raw_code = self.error.get("err_code")
        self.code = int(raw_code) if isinstance(raw_code, (int, str)) and str(raw_code).isdigit() else None
        super().__init__("JetStream API request failed")


async def request(nc, subject: str, body: dict[str, object]) -> dict[str, object]:
    message = await nc.request(subject, json.dumps(body, separators=(",", ":")).encode(), timeout=5)
    try:
        value = json.loads(message.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid JetStream response") from exc
    if not isinstance(value, dict):
        raise RuntimeError("invalid JetStream response")
    if value.get("error"):
        raise JetStreamApiError(value["error"])
    return value


def is_missing(error: JetStreamApiError) -> bool:
    return error.code in {10014, 10059}


def require_config(info: dict[str, object], expected: dict[str, object]) -> None:
    actual = info.get("config")
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
        raise RuntimeError("run-evidence JetStream configuration drift detected")


async def ensure() -> None:
    password = Path("/run/secrets/aura-nats-run-evidence-bootstrap-password").read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("missing bootstrap credential")
    user = os.environ.get("AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_USER", "aura-run-evidence-bootstrap")
    nc = await nats.connect(f"nats://{user}:{password}@aura-nats:4222")
    try:
        stream_config = {
            "name": "AURA_RUN_EVIDENCE",
            "subjects": ["aura.runs.evidence.v1"],
            "retention": "limits",
            "storage": "file",
            "max_age": 604800000000000,
            "max_bytes": 268435456,
            "max_msgs": 250000,
            "max_msg_size": 1500000,
            "discard": "old",
            "num_replicas": 1,
        }
        try:
            stream_info = await request(nc, "$JS.API.STREAM.INFO.AURA_RUN_EVIDENCE", {})
        except JetStreamApiError as error:
            if not is_missing(error):
                raise
            await request(nc, "$JS.API.STREAM.CREATE.AURA_RUN_EVIDENCE", stream_config)
            stream_info = await request(nc, "$JS.API.STREAM.INFO.AURA_RUN_EVIDENCE", {})
        require_config(stream_info, stream_config)

        consumer_config = {
            "durable_name": "aura-observatory-run-evidence",
            "name": "aura-observatory-run-evidence",
            "ack_policy": "explicit",
            "ack_wait": 60000000000,
            "max_deliver": 10,
            "max_ack_pending": 128,
            "deliver_policy": "all",
            "filter_subject": "aura.runs.evidence.v1",
        }
        info_subject = "$JS.API.CONSUMER.INFO.AURA_RUN_EVIDENCE.aura-observatory-run-evidence"
        create_subject = "$JS.API.CONSUMER.DURABLE.CREATE.AURA_RUN_EVIDENCE.aura-observatory-run-evidence"
        payload = {"stream_name": "AURA_RUN_EVIDENCE", "config": consumer_config}
        try:
            consumer_info = await request(nc, info_subject, {})
        except JetStreamApiError as error:
            if not is_missing(error):
                raise
            await request(nc, create_subject, payload)
            consumer_info = await request(nc, info_subject, {})
        require_config(consumer_info, consumer_config)
    finally:
        await nc.drain()


try:
    asyncio.run(ensure())
except Exception:
    print("run-evidence JetStream bootstrap failed", file=__import__("sys").stderr)
    raise SystemExit(78)
PY
