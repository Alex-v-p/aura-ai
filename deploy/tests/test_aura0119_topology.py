"""AURA-0119 isolated run-evidence deployment topology checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services() -> dict[str, dict[str, Any]]:
    value = _compose()["services"]
    assert isinstance(value, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[object, object], value).items()
    }


def test_run_evidence_has_a_dedicated_profile_stream_consumer_and_private_network() -> None:
    services = _services()
    nats = services["aura-nats"]
    bootstrap = services["aura-run-evidence-nats-bootstrap"]
    worker = services["observatory-worker"]
    core_api = services["aura-core-api"]
    core_worker = services["aura-core-worker"]
    network = cast(dict[str, Any], _compose()["networks"]["aura-run-evidence-bus"])

    assert network["internal"] is True
    assert "aura-run-evidence-bus" in nats["networks"]
    assert "aura-run-evidence-bus" in worker["networks"]
    assert "aura-run-evidence-bus" in core_api["networks"]
    assert "aura-run-evidence-bus" in core_worker["networks"]
    assert bootstrap["profiles"] == ["observatory"]
    assert bootstrap["networks"] == ["aura-run-evidence-bus"]
    assert "nats-run-evidence-bootstrap.sh" in repr(bootstrap)
    assert "aura-nats-run-evidence-bootstrap-password" in repr(bootstrap["secrets"])
    assert bootstrap["environment"]["AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_USER"] == (
        "${AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_USER:-aura-run-evidence-bootstrap}"
    )
    evaluation_bootstrap = services["aura-evaluation-nats-bootstrap"]
    assert "aura-nats-bootstrap-password" in repr(evaluation_bootstrap["secrets"])
    assert "aura-nats-run-evidence-bootstrap-password" not in repr(
        evaluation_bootstrap["secrets"]
    )
    assert "aura-run-evidence-nats-bootstrap" in repr(worker["depends_on"])

    nats_text = (ROOT / "deploy/docker/nats-entrypoint.sh").read_text(encoding="utf-8")
    bootstrap_text = (ROOT / "deploy/docker/nats-run-evidence-bootstrap.sh").read_text(
        encoding="utf-8"
    )
    for text in (nats_text, bootstrap_text):
        assert "AURA_RUN_EVIDENCE" in text
        assert "aura.runs.evidence.v1" in text
        assert "aura-observatory-run-evidence" in text
    assert "AURA_EVALUATION_RUNNER" in nats_text
    assert "aura.evaluation.runner.requested.v1" not in bootstrap_text
    assert 'deny: ["aura.evaluation.>", "aura.runs.evidence.v1"]' in nats_text


def test_run_evidence_nats_identity_is_consumer_only_and_bootstrap_is_bounded() -> None:
    nats_text = (ROOT / "deploy/docker/nats-entrypoint.sh").read_text(encoding="utf-8")
    bootstrap_text = (ROOT / "deploy/docker/nats-run-evidence-bootstrap.sh").read_text(
        encoding="utf-8"
    )

    user_start = nats_text.index('user: "$run_evidence_user"')
    user_block = nats_text[user_start : nats_text.index("    }", user_start) + 5]
    assert "aura.runs.evidence.v1" not in user_block
    assert "$JS.API.CONSUMER.INFO.AURA_RUN_EVIDENCE.aura-observatory-run-evidence" in user_block
    assert "$JS.API.CONSUMER.MSG.NEXT.AURA_RUN_EVIDENCE.aura-observatory-run-evidence" in user_block
    assert "$JS.ACK.AURA_RUN_EVIDENCE.aura-observatory-run-evidence.>" in user_block
    assert 'subscribe: { allow: ["_INBOX.>"] }' in user_block

    assert '"max_age": 604800000000000' in bootstrap_text
    assert '"max_bytes": 268435456' in bootstrap_text
    assert '"max_msgs": 250000' in bootstrap_text
    assert '"max_msg_size": 1500000' in bootstrap_text
    assert '"ack_policy": "explicit"' in bootstrap_text
    assert '"max_deliver": 10' in bootstrap_text
    assert "configuration drift detected" in bootstrap_text


def test_run_evidence_garage_has_distinct_worker_write_and_api_read_delete_keys() -> None:
    services = _services()
    worker_bootstrap = services["observatory-run-evidence-garage-bootstrap"]
    api_bootstrap = services["observatory-run-evidence-api-garage-bootstrap"]
    worker = services["observatory-worker"]
    api = services["observatory-api"]
    secrets = cast(dict[Any, Any], _compose()["secrets"])

    worker_body = repr(worker_bootstrap)
    api_body = repr(api_bootstrap)
    worker_service_body = repr(worker)
    api_service_body = repr(api)
    worker_environment = cast(dict[str, Any], worker["environment"])
    api_environment = cast(dict[str, Any], api["environment"])
    assert worker_bootstrap["profiles"] == ["observatory"]
    assert api_bootstrap["profiles"] == ["observatory"]
    assert "observatory-run-evidence-garage-bootstrap" in repr(worker["depends_on"])
    assert "observatory-run-evidence-api-garage-bootstrap" in repr(api["depends_on"])
    assert "observatory-run-evidence-worker-access-key" in worker_body
    assert "observatory-run-evidence-worker-secret-key" in worker_body
    assert "observatory-run-evidence-api-access-key" in api_body
    assert "observatory-run-evidence-api-secret-key" in api_body
    assert "bucket allow --write --key" in str(worker_bootstrap["command"])
    assert "bucket deny --read --key" in str(worker_bootstrap["command"])
    assert "expected W" in str(worker_bootstrap["command"])
    assert "bucket allow --read --write --key" in str(api_bootstrap["command"])
    assert "expected RW" in str(api_bootstrap["command"])
    assert "bucket deny --owner --key" in str(api_bootstrap["command"])
    assert "observatory-run-evidence-api-access-key" in api_service_body
    assert "observatory-run-evidence-api-secret-key" in api_service_body
    assert "observatory-run-evidence-worker-access-key" in worker_service_body
    assert "observatory-run-evidence-worker-secret-key" in worker_service_body
    assert "observatory-run-evidence-api-access-key" not in worker_service_body
    assert "observatory-run-evidence-worker-access-key" not in api_service_body
    assert worker_environment["AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_ACCESS_KEY_FILE"].endswith(
        "observatory-run-evidence-worker-access-key"
    )
    assert worker_environment["AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_SECRET_KEY_FILE"].endswith(
        "observatory-run-evidence-worker-secret-key"
    )
    assert api_environment["AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_ACCESS_KEY_FILE"].endswith(
        "observatory-run-evidence-api-access-key"
    )
    assert api_environment["AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_SECRET_KEY_FILE"].endswith(
        "observatory-run-evidence-api-secret-key"
    )
    assert worker_environment["AURA_OBSERVATORY_RUN_EVIDENCE_NATS_DURABLE"] == (
        "${AURA_OBSERVATORY_RUN_EVIDENCE_NATS_DURABLE:-aura-observatory-run-evidence}"
    )
    assert "AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES" in worker_environment
    assert "AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES" in api_environment
    assert worker_environment["AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES"] == (
        "${AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES:-1500000}"
    )
    assert api_environment["AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES"] == (
        "${AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES:-1500000}"
    )

    for name in (
        "observatory-run-evidence-worker-access-key",
        "observatory-run-evidence-worker-secret-key",
        "observatory-run-evidence-api-access-key",
        "observatory-run-evidence-api-secret-key",
    ):
        assert name in secrets
        assert "file" in cast(dict[str, Any], secrets[name])
        assert "value" not in cast(dict[str, Any], secrets[name])

    assert "aura-observatory-run-evidence" in worker_service_body
    assert "AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET" in api_service_body
    assert "AURA_OBSERVATORY_GARAGE_BUCKET" in worker_body


def test_run_evidence_local_http_and_environment_wiring_is_file_backed() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    launcher = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")
    overlay = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    for name in (
        "AURA_NATS_RUN_EVIDENCE_PASSWORD_FILE",
        "AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_PASSWORD_FILE",
        "AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_USER",
        "AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET",
        "AURA_OBSERVATORY_RUN_EVIDENCE_WORKER_ACCESS_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_WORKER_SECRET_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_API_ACCESS_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_API_SECRET_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_NATS_DURABLE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_MAX_ENVELOPE_BYTES",
        "AURA_RUN_EVIDENCE_MAX_BYTES",
    ):
        assert name in env
    for name in (
        "AURA_NATS_RUN_EVIDENCE_PASSWORD_FILE",
        "AURA_NATS_RUN_EVIDENCE_BOOTSTRAP_PASSWORD_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_WORKER_ACCESS_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_WORKER_SECRET_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_API_ACCESS_KEY_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_API_SECRET_KEY_FILE",
    ):
        assert name in launcher
    assert "observatory-run-evidence-garage-bootstrap:" in overlay
    assert "observatory-run-evidence-api-garage-bootstrap:" in overlay
    assert "local_http_core_garage_data" in overlay
    assert "local_http_core_garage_config" in overlay


def test_core_capture_policy_is_explicit_owner_opt_in_and_disabled_by_default() -> None:
    services = _services()
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    expected_policy = "${AURA_RUN_EVIDENCE_CAPTURE_POLICY:-}"
    expected_revision = "${AURA_RUN_EVIDENCE_CAPTURE_REVISION:-full-content-v1}"
    for service_name in ("aura-core-api", "aura-core-worker", "aura-evaluation-runner"):
        service_environment = cast(dict[str, Any], services[service_name]["environment"])
        assert service_environment["AURA_RUN_EVIDENCE_CAPTURE_POLICY"] == expected_policy
        assert service_environment["AURA_RUN_EVIDENCE_CAPTURE_REVISION"] == expected_revision
        assert service_environment["AURA_RUN_EVIDENCE_MAX_BYTES"] == (
            "${AURA_RUN_EVIDENCE_MAX_BYTES:-1048576}"
        )
    assert "AURA_RUN_EVIDENCE_CAPTURE_POLICY=\n" in env
    assert "AURA_RUN_EVIDENCE_CAPTURE_REVISION=full-content-v1\n" in env
    assert "AURA_RUN_EVIDENCE_CAPTURE_POLICY:-full_content" not in env
    assert "AURA_RUN_EVIDENCE_CAPTURE_POLICY:-full_content" not in (
        (ROOT / "compose.yaml").read_text(encoding="utf-8")
    )
