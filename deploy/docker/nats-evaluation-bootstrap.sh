#!/bin/sh
set -eu

# Provision the bounded evaluation JetStream topology with a one-shot
# administration identity. Existing resources are reconciled fail-closed:
# drift is an operator error, not an invitation to widen retention or delivery.
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
    # NATS 2.10 error codes for stream-not-found and consumer-not-found.
    # No other error is treated as absence; auth and connectivity fail closed.
    return error.code in {10014, 10059}


def require_config(info: dict[str, object], expected: dict[str, object]) -> None:
    actual = info.get("config")
    if not isinstance(actual, dict):
        raise RuntimeError("JetStream resource configuration is unavailable")
    if any(actual.get(key) != value for key, value in expected.items()):
        raise RuntimeError("JetStream resource configuration drift detected")


async def ensure() -> None:
    password = Path("/run/secrets/aura-nats-bootstrap-password").read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("missing bootstrap credential")
    user = os.environ.get("AURA_NATS_BOOTSTRAP_USER", "aura-evaluation-bootstrap")
    nc = await nats.connect(f"nats://{user}:{password}@aura-nats:4222")
    try:
        stream_config = {
            "name": "AURA_EVALUATION_RUNNER",
            "subjects": [
                "aura.evaluation.runner.requested.v1",
                "aura.evaluation.runner.cancellation.v1",
                "aura.evaluation.runner.results.v1",
            ],
            "retention": "limits",
            "storage": "file",
            "max_age": 604800000000000,
            "max_bytes": 67108864,
            "max_msgs": 100000,
            "max_msg_size": 1048576,
            "discard": "old",
            "num_replicas": 1,
        }
        try:
            stream_info = await request(nc, "$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", {})
        except JetStreamApiError as error:
            if not is_missing(error):
                raise
            await request(nc, "$JS.API.STREAM.CREATE.AURA_EVALUATION_RUNNER", stream_config)
            stream_info = await request(nc, "$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", {})
        require_config(stream_info, stream_config)

        base_consumer_config = {
            "ack_policy": "explicit",
            "ack_wait": 30000000000,
            "max_deliver": 10,
            "max_ack_pending": 128,
            "deliver_policy": "all",
        }
        consumers = (
            ("aura-evaluation-dispatch", "aura.evaluation.runner.results.v1"),
            ("aura-core-evaluation-runner", "aura.evaluation.runner.requested.v1"),
            ("aura-core-evaluation-cancellation", "aura.evaluation.runner.cancellation.v1"),
        )
        for durable, subject in consumers:
            expected = {
                **base_consumer_config,
                "durable_name": durable,
                "name": durable,
                "filter_subject": subject,
            }
            info_subject = f"$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.{durable}"
            create_subject = f"$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.{durable}"
            payload = {"stream_name": "AURA_EVALUATION_RUNNER", "config": expected}
            try:
                consumer_info = await request(nc, info_subject, {})
            except JetStreamApiError as error:
                if not is_missing(error):
                    raise
                await request(nc, create_subject, payload)
                consumer_info = await request(nc, info_subject, {})
            require_config(consumer_info, expected)
    finally:
        await nc.drain()


try:
    asyncio.run(ensure())
except Exception:
    print("evaluation JetStream bootstrap failed", file=__import__("sys").stderr)
    raise SystemExit(78)
PY
