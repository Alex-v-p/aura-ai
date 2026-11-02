"""AURA-0090 deployment and least-privilege object-storage checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _document(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = document.get("services")
    assert isinstance(services, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[Any, Any], services).items()
        if isinstance(definition, dict)
    }


def test_evaluation_bucket_and_key_are_separate_from_core_and_worker_only() -> None:
    document = _document(ROOT / "compose.yaml")
    services = _services(document)
    worker = services["observatory-worker"]
    api = services["observatory-api"]
    web = services["observatory-web"]
    collector = services["observatory-collector"]
    core_api = services["aura-core-api"]
    core_worker = services["aura-core-worker"]

    worker_body = repr(worker)
    assert "observatory-object-storage" in worker_body
    assert "observatory-garage-access-key" in worker_body
    assert "observatory-garage-secret-key" in worker_body
    assert "AURA_OBSERVATORY_EVALUATION_OBJECT_BUCKET" in worker_body
    for service in (api, web, collector, core_api, core_worker):
        body = repr(service)
        assert "observatory-garage-access-key" not in body
        assert "observatory-garage-secret-key" not in body

    assert "observatory-garage-bootstrap" in repr(worker.get("depends_on", {}))


def test_object_storage_network_is_private_and_has_no_host_ports() -> None:
    document = _document(ROOT / "compose.yaml")
    networks = cast(dict[Any, Any], document["networks"])
    object_network = cast(dict[str, Any], networks["observatory-object-storage"])
    assert object_network["internal"] is True
    services = _services(document)
    garage = services["aura-garage"]
    bootstrap = services["observatory-garage-bootstrap"]
    assert "observatory-object-storage" in (garage.get("networks") or [])
    assert "observatory-object-storage" in (bootstrap.get("networks") or [])
    assert "ports" not in garage
    assert "ports" not in bootstrap


def test_evaluation_secrets_are_file_backed_and_state_mounts_use_encrypted_root() -> None:
    document = _document(ROOT / "compose.yaml")
    secrets = cast(dict[Any, Any], document["secrets"])
    for name in ("observatory-garage-access-key", "observatory-garage-secret-key"):
        assert name in secrets
        definition = cast(dict[str, Any], secrets[name])
        assert "file" in definition
        assert "value" not in definition

    services = _services(document)
    for name in ("aura-garage", "observatory-garage-bootstrap"):
        body = repr(services[name])
        assert "AURA_STATE_ROOT" in body
    worker_body = repr(services["observatory-worker"])
    assert "/run/secrets/observatory-garage-access-key" in worker_body
    assert "/run/secrets/observatory-garage-secret-key" in worker_body


def test_observatory_garage_grant_is_write_only_scoped_and_fails_closed() -> None:
    document = _document(ROOT / "compose.yaml")
    services = _services(document)
    bootstrap = services["observatory-garage-bootstrap"]
    command = str(bootstrap["command"])
    bootstrap_text = repr(bootstrap)

    assert "bucket allow --write --key" in command
    assert "bucket allow --read" not in command
    assert "unexpected preexisting bucket grant" in command
    assert "bucket deny --read --key" in command
    assert "bucket deny --owner --key" in command
    assert "Remove a pre-existing read grant" in command
    assert "grant verification failed closed" in command
    assert "target_bucket_count" in command
    assert "has_read_disabled" in command
    assert "has_owner_disabled" in command
    assert "has_write_grant" in command
    assert "read/write/owner permission verification failed closed" in command
    assert "AURA_GARAGE_BUCKET" in command
    assert "AURA_OBSERVATORY_GARAGE_BUCKET" in command
    assert "observatory-garage-access-key" in bootstrap_text
    assert "aura-garage-access-key" not in bootstrap_text
    core_bootstrap_text = repr(services["aura-garage-bootstrap"])
    assert "AURA_GARAGE_BUCKET:-aura-documents" in core_bootstrap_text
    assert "AURA_OBSERVATORY_GARAGE_BUCKET:-aura-observatory-evaluation" in bootstrap_text


def test_local_http_and_full_stack_fixtures_keep_observatory_secrets_scoped() -> None:
    local_text = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    assert "local_http_core_garage_data" in local_text
    assert "local_http_core_garage_config" in local_text
    assert "observatory-garage-bootstrap:" in local_text

    runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(encoding="utf-8")
    assert "write_fixture_secret observatory-garage-access-key" in runner
    assert "write_fixture_secret observatory-garage-secret-key" in runner
    assert "AURA_OBSERVATORY_EVALUATION_OBJECT_BUCKET" in runner
    assert "AURA_OBSERVATORY_EVALUATION_OBJECT_ACCESS_KEY_FILE" in runner
    assert "AURA_OBSERVATORY_EVALUATION_OBJECT_SECRET_KEY_FILE" in runner
    assert "observatory-garage-access-key" in runner
    assert "observatory-garage-secret-key" in runner


def test_full_stack_fixture_uses_distinct_valid_garage_keys_without_printing_secrets() -> None:
    runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
        encoding="utf-8"
    )

    def fixture_key(secret_name: str) -> str:
        match = re.search(
            rf"^write_fixture_secret {re.escape(secret_name)} (GK[A-Za-z0-9]{{24}})$",
            runner,
            flags=re.MULTILINE,
        )
        assert match is not None
        return match.group(1)

    core_key = fixture_key("aura-garage-access-key")
    observatory_key = fixture_key("observatory-garage-access-key")
    assert core_key.startswith("GK")
    assert observatory_key.startswith("GK")
    assert core_key != observatory_key
    assert 'printf \'%s\\n\' "$value" > "$secret_root/$name"' in runner
    assert 'printf \'%s\\n\' "$value"\n' not in runner


def test_full_stack_artifact_seed_matches_authorization_schema_and_contract() -> None:
    runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
        encoding="utf-8"
    )
    seed = re.search(
        r"INSERT INTO observatory_evaluation_authorizations\((.*?)\)\s+"
        r"VALUES \((.*?)\)\"\"\"",
        runner,
        flags=re.DOTALL,
    )
    assert seed is not None
    columns = {item.strip() for item in seed.group(1).split(",")}
    assert {
        "authorization_record_id",
        "request_id",
        "dataset_id",
        "capture_classification",
        "authorization_purpose",
        "authorization_context",
        "retention_mode",
        "authorized_at",
    } <= columns
    values = seed.group(2)
    assert values.count("%s") == 4
    assert "owner_supplied_evaluation" in values
    assert "e2e_storage" not in values

    # The acceptance runner now seeds several isolated fixtures through one
    # typed helper.  Validate the helper's parameter tuple rather than
    # coupling this deployment regression to one fixture's old constant names.
    assert 'fixture["authorization_id"]' in runner
    assert 'fixture["request_id"]' in runner
    assert 'fixture["dataset_id"]' in runner
    assert "fixture[\"authorization_id\"]," in runner
    assert "fixture[\"request_id\"]," in runner
    assert "fixture[\"dataset_id\"]," in runner
    labels = set(re.findall(r'"label": "([a-z-]+)"', runner))
    assert {"job-missing-object", "dataset", "suite", "cutoff-old", "cutoff-new", "purge-all"} <= labels
    fixture_ids = re.findall(
        r'"(?:authorization_id|artifact_id|reference_id|request_id|result_id|'
        r'idempotency_key|job_id|dataset_id|suite_id|score_id)": "([0-9a-f-]{36})"',
        runner,
    )
    assert fixture_ids
    assert len(fixture_ids) == len(set(fixture_ids))


def test_full_stack_runner_hands_off_through_distinct_provider_subnet() -> None:
    runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
        encoding="utf-8"
    )
    for variable in (
        "AURA_OBJECT_STORAGE_SUBNET",
        "AURA_DOCUMENT_PARSER_SUBNET",
        "AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET",
    ):
        assert variable in runner
    assert "AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET=$observatory_object_storage_subnet" in runner
    assert "aura-e2e-provider:" in runner
    assert "external: true" in runner
    assert "name: aura-e2e-provider" in runner
    provider_members = runner.split("services:", 1)[1].split("secrets:", 1)[0]
    assert provider_members.count("aura-e2e-provider") >= 3
    assert "frontend-e2e:" in provider_members


def test_full_stack_runner_serializes_compose_resource_lifecycle() -> None:
    runner = (ROOT / "deploy/compose/run-observatory-full-stack-e2e.sh").read_text(
        encoding="utf-8"
    )
    assert "export COMPOSE_PARALLEL_LIMIT=1" in runner


def test_artifact_manifest_declares_metadata_only_capture_and_bounded_labels() -> None:
    root = ROOT / "services/aura-observatory/resources/component-manifests"
    artifact = _document(root / "evaluation-artifact-storage.yaml")
    deletion = _document(root / "evaluation-deletion.yaml")
    assert artifact["capture_policy"] == "metadata_only"
    assert artifact["retention"] == "manual_owner_delete_only"
    assert deletion["capture_policy"] == "metadata_only"
    assert deletion["retention"] == "manual_owner_delete_only"
    assert set(artifact["cardinality"]["metric_attributes"]) <= {"operation", "outcome"}
    assert set(deletion["cardinality"]["metric_attributes"]) <= {
        "selector",
        "phase",
        "outcome",
        "failure_code",
    }
    for manifest in (artifact, deletion):
        prohibited = set(manifest["cardinality"]["prohibited"])
        assert prohibited == {
            "prompt",
            "response",
            "content",
            "filename",
            "arguments",
            "result",
            "endpoint",
            "token",
            "bearer",
            "credential",
            "password",
            "private",
            "hash",
            "digest",
            "access_token",
            "refresh_token",
            "id_token",
            "authorization",
            "nonce",
            "code",
            "state",
            "issuer",
            "subject",
            "object_key",
            "owner_identity",
        }
