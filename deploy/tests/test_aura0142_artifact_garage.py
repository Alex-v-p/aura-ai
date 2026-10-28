"""AURA-0142 generated-artifact Garage isolation and bootstrap checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
ARTIFACT_SECRETS = {"aura-artifact-access-key", "aura-artifact-secret-key"}


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services() -> dict[str, dict[str, Any]]:
    services = _compose()["services"]
    assert isinstance(services, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[object, object], services).items()
    }


def test_artifact_bucket_bootstrap_is_private_idempotent_and_fail_closed() -> None:
    services = _services()
    bootstrap = services["aura-artifacts-garage-bootstrap"]
    command = str(bootstrap["command"])

    assert "profiles" not in bootstrap
    assert bootstrap["networks"] == ["aura-object-storage"]
    assert "ports" not in bootstrap
    assert "aura-garage" in repr(bootstrap["depends_on"])
    assert "aura-garage-config" in repr(bootstrap["depends_on"])
    assert "aura-garage-bootstrap" in repr(bootstrap["depends_on"])
    assert bootstrap["secrets"] == sorted(ARTIFACT_SECRETS)
    assert "key info" in command
    assert "key import --yes" in command
    assert "bucket info" in command
    assert "bucket create" in command
    assert "bucket allow --read --write --key" in command
    assert "bucket deny --owner --key" in command
    assert "unexpected preexisting bucket grant" in command
    assert "grant verification failed closed" in command
    assert "permission verification failed closed (expected RW)" in command
    assert "target_bucket_count" in command
    assert "unset access_key secret_key" in command
    assert ">/dev/null 2>&1" in command


def test_artifact_identity_is_exactly_scoped_away_from_all_other_buckets() -> None:
    services = _services()
    bootstrap = services["aura-artifacts-garage-bootstrap"]
    environment = cast(dict[str, Any], bootstrap["environment"])

    assert environment["AURA_ARTIFACT_STORAGE_BUCKET"] == (
        "${AURA_ARTIFACT_STORAGE_BUCKET:-aura-artifacts}"
    )
    assert environment["AURA_GARAGE_BUCKET"] == "${AURA_GARAGE_BUCKET:-aura-documents}"
    assert environment["AURA_OBSERVATORY_GARAGE_BUCKET"] == (
        "${AURA_OBSERVATORY_GARAGE_BUCKET:-aura-observatory-evaluation}"
    )
    assert environment["AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET"] == (
        "${AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET:-aura-observatory-run-evidence}"
    )
    command = str(bootstrap["command"])
    assert '"$${AURA_ARTIFACT_STORAGE_BUCKET}" != "aura-artifacts"' in command
    assert '"$${AURA_ARTIFACT_STORAGE_BUCKET}" = "$${AURA_GARAGE_BUCKET}"' in command
    assert '"$${AURA_ARTIFACT_STORAGE_BUCKET}" = "$${AURA_OBSERVATORY_GARAGE_BUCKET}"' in command
    assert (
        '"$${AURA_ARTIFACT_STORAGE_BUCKET}" = '
        '"$${AURA_OBSERVATORY_RUN_EVIDENCE_OBJECT_BUCKET}"'
    ) in command
    assert "bucket allow --read --write --key" in command
    assert "bucket deny --owner --key" in command

    # The bootstrap identity receives no other product's secret, and the
    # production services that do not own generated artifacts receive neither
    # half of this credential pair.
    assert not ARTIFACT_SECRETS & set(map(str, bootstrap["secrets"])) ^ ARTIFACT_SECRETS
    for name, service in services.items():
        if name not in {"aura-core-api", "aura-core-worker", "aura-artifacts-garage-bootstrap"}:
            assert not ARTIFACT_SECRETS & set(map(str, service.get("secrets", [])))


def test_core_artifact_credentials_are_file_backed_and_not_browser_or_provider_visible() -> None:
    document = _compose()
    services = _services()
    secrets = cast(dict[Any, Any], document["secrets"])
    for secret_name in ARTIFACT_SECRETS:
        definition = cast(dict[str, Any], secrets[secret_name])
        assert "file" in definition
        assert "value" not in definition

    for service_name in ("aura-core-api", "aura-core-worker"):
        service = services[service_name]
        body = repr(service)
        assert "aura-artifact-access-key" in body
        assert "aura-artifact-secret-key" in body
        environment = cast(dict[str, Any], service["environment"])
        assert environment["AURA_ARTIFACT_STORAGE_ENDPOINT"] == "http://aura-garage:3900"
        assert environment["AURA_ARTIFACT_STORAGE_ACCESS_KEY_FILE"] == (
            "/run/secrets/aura-artifact-access-key"
        )
        assert environment["AURA_ARTIFACT_STORAGE_SECRET_KEY_FILE"] == (
            "/run/secrets/aura-artifact-secret-key"
        )
        assert "aura-artifacts-garage-bootstrap" in repr(service["depends_on"])

    # Browsers, the scheduler, provider services, and Observatory must not
    # inherit the artifact credentials or the private artifact bucket value.
    for service_name in (
        "aura-web",
        "aura-core-scheduler",
        "aura-document-parser",
        "observatory-worker",
        "observatory-api",
        "observatory-evaluation-worker",
        "observatory-evaluation-scheduler",
    ):
        body = repr(services[service_name])
        assert "aura-artifact-access-key" not in body
        assert "aura-artifact-secret-key" not in body
        assert "AURA_ARTIFACT_STORAGE_BUCKET" not in body


def test_artifact_environment_examples_are_non_secret_and_separate() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "AURA_ARTIFACT_STORAGE_BUCKET",
        "AURA_ARTIFACT_STORAGE_ACCESS_KEY_FILE",
        "AURA_ARTIFACT_STORAGE_SECRET_KEY_FILE",
    ):
        assert name in env
    assert "AURA_ARTIFACT_STORAGE_BUCKET=aura-artifacts" in env
    assert "aura-documents" not in env.split("AURA_ARTIFACT_STORAGE_BUCKET=", 1)[1].splitlines()[0]
    assert "GK" not in env
    assert "secret-key=" not in env


def test_artifact_bootstrap_does_not_print_secret_material() -> None:
    command = str(_services()["aura-artifacts-garage-bootstrap"]["command"])
    for forbidden in (
        "echo \"$${access_key}\"",
        "echo \"$${secret_key}\"",
        "printf '%s\\n' \"$${secret_key}\"",
        "set -x",
    ):
        assert forbidden not in command
    assert "unset access_key secret_key" in command
