"""AURA-0091 deployment isolation and bounded-runner checks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _document(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services() -> dict[str, dict[str, Any]]:
    value = _document(ROOT / "compose.yaml").get("services")
    assert isinstance(value, dict)
    services = cast(dict[object, object], value)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in services.items()
    }


def _nats_user_block(script: str, user_variable: str) -> str:
    """Return one generated authorization block without parsing shell secrets."""

    start = script.index(f'user: "{user_variable}"')
    next_user = script.find('\n    {\n      user:', start + 1)
    return script[start:] if next_user == -1 else script[start:next_user]


def _nats_permission_subjects(block: str, permission: str) -> set[str]:
    match = re.search(
        rf"{permission}:\s*\{{\s*allow:\s*\[(.*?)\]",
        block,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing {permission} permission block"
    return {
        value.replace(r"\$", "$")
        for value in re.findall(r'"((?:\\.|[^"\\])*)"', match.group(1))
    }


def test_evaluation_nats_has_closed_subject_acl_and_file_injected_identities() -> None:
    services = _services()
    nats = services["aura-nats"]
    assert nats["entrypoint"] == ["/usr/local/bin/aura-nats-entrypoint"]
    assert set(nats["secrets"]) == {
        "aura-nats-password",
        "aura-nats-bootstrap-password",
        "aura-nats-dispatch-password",
        "aura-nats-runner-password",
        "aura-nats-run-evidence-bootstrap-password",
        "aura-nats-run-evidence-password",
    }
    assert "aura-evaluation-bus" in nats["networks"]
    script = (ROOT / "deploy/docker/nats-entrypoint.sh").read_text(encoding="utf-8")
    assert "aura.evaluation.runner.requested.v1" in script
    assert "aura.evaluation.runner.cancellation.v1" in script
    assert "aura.evaluation.runner.results.v1" in script
    assert 'deny: ["aura.evaluation.>"]' in script
    assert "/run/secrets/aura-nats-dispatch-password" in script
    assert "/run/secrets/aura-nats-runner-password" in script
    assert "/run/secrets/aura-nats-bootstrap-password" in script
    assert '"\\$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER"' in script
    assert '"\\$JS.API.STREAM.CREATE.AURA_EVALUATION_RUNNER"' in script
    assert (
        '"\\$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner"'
        in script
    )
    assert 'allow: ["\\$JS.API.>"]' not in script
    for line in script.splitlines():
        if "publish: { allow:" in line:
            assert '"_INBOX.>"' not in line
    assert "CONSUMER.INFO.AURA_EVALUATION_RUNNER" in script
    assert "printf '%s' \"$value\"" in script
    assert "echo \"$value\"" not in script


def test_core_nats_identity_has_only_the_exact_current_jetstream_matrix() -> None:
    script = (ROOT / "deploy/docker/nats-entrypoint.sh").read_text(encoding="utf-8")
    normal = _nats_user_block(script, "$normal_user")
    normal_publish = _nats_permission_subjects(normal, "publish")
    normal_subscribe = _nats_permission_subjects(normal, "subscribe")

    consumers = (
        ("aura-core-worker", "aura.runs.execute.v1"),
        ("aura-core-memory-worker", "aura.memory.process.v1"),
        ("aura-core-approval-resume-worker", "aura.runs.resume.v1"),
        ("aura-core-document-process-worker", "aura.knowledge.documents.process.v1"),
        ("aura-core-document-delete-worker", "aura.knowledge.documents.delete.v1"),
    )
    expected_jetstream = {
        "$JS.API.INFO",
        "$JS.API.STREAM.LIST",
        "$JS.API.STREAM.INFO.AURA_RUNS",
        "$JS.API.STREAM.CREATE.AURA_RUNS",
        "$JS.API.STREAM.UPDATE.AURA_RUNS",
    }
    for durable, filter_subject in consumers:
        expected_jetstream.update(
            {
                f"$JS.API.CONSUMER.INFO.AURA_RUNS.{durable}",
                f"$JS.API.CONSUMER.CREATE.AURA_RUNS.{durable}.{filter_subject}",
                f"$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.{durable}",
                f"$JS.ACK.AURA_RUNS.{durable}.>",
            }
        )

    actual_jetstream = {
        subject for subject in normal_publish if subject.startswith("$JS.")
    }
    assert actual_jetstream == expected_jetstream
    assert "$JS.API.ACCOUNT.INFO" not in actual_jetstream
    assert not any(
        subject.startswith("$JS.API.") and ">" in subject
        for subject in actual_jetstream
    )
    assert not any(
        subject.startswith("$JS.API.CONSUMER.DURABLE.CREATE")
        for subject in actual_jetstream
    )

    # The ordinary Core topics remain explicit where they are not covered by
    # the existing broad run/approval/notification/artifact permissions.
    assert {
        "aura.memory.process.v1",
        "aura.knowledge.documents.process.v1",
        "aura.knowledge.documents.delete.v1",
    } <= normal_subscribe
    assert "$JS.API.STREAM.DELETE.AURA_RUNS" not in normal_publish
    assert "$JS.API.STREAM.PURGE.AURA_RUNS" not in normal_publish
    assert not any(
        subject.startswith("$JS.API.CONSUMER.DELETE")
        or subject.startswith("$JS.API.CONSUMER.DURABLE.CREATE")
        for subject in normal_publish
    )


def test_non_core_nats_identities_have_no_core_jetstream_grants() -> None:
    script = (ROOT / "deploy/docker/nats-entrypoint.sh").read_text(encoding="utf-8")
    normal = _nats_user_block(script, "$normal_user")
    normal_publish = _nats_permission_subjects(normal, "publish")
    normal_jetstream = {subject for subject in normal_publish if subject.startswith("$JS.")}

    # ACCOUNT.INFO is not needed by any identity; stream/account metadata must
    # not become a shortcut to cross-domain or evaluation access.
    assert '"\\$JS.API.ACCOUNT.INFO"' not in script

    bootstrap = _nats_user_block(script, "$bootstrap_user")
    bootstrap_jetstream = {
        subject
        for subject in _nats_permission_subjects(bootstrap, "publish")
        if subject.startswith("$JS.")
    }
    assert bootstrap_jetstream == {
        "$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER",
        "$JS.API.STREAM.CREATE.AURA_EVALUATION_RUNNER",
        "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch",
        "$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch",
        "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner",
        "$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner",
        "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation",
        "$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation",
    }

    run_evidence_bootstrap = _nats_user_block(script, "$run_evidence_bootstrap_user")
    assert _nats_permission_subjects(run_evidence_bootstrap, "publish") == {
        "$JS.API.STREAM.INFO.AURA_RUN_EVIDENCE",
        "$JS.API.STREAM.CREATE.AURA_RUN_EVIDENCE",
        "$JS.API.CONSUMER.INFO.AURA_RUN_EVIDENCE.aura-observatory-run-evidence",
        "$JS.API.CONSUMER.DURABLE.CREATE.AURA_RUN_EVIDENCE.aura-observatory-run-evidence",
    }

    for user_variable in ("$dispatch_user", "$runner_user"):
        block = _nats_user_block(script, user_variable)
        published = _nats_permission_subjects(block, "publish")
        published_jetstream = {
            subject for subject in published if subject.startswith("$JS.")
        }
        assert not (published_jetstream & normal_jetstream)
        assert "$JS.API.INFO" not in published
        assert "$JS.API.STREAM.LIST" not in published
        assert "$JS.API.ACCOUNT.INFO" not in published
        assert not any(
            subject.startswith("$JS.API.CONSUMER.CREATE.AURA_RUNS")
            or subject.startswith("$JS.API.CONSUMER.DURABLE.CREATE.AURA_RUNS")
            for subject in published
        )


def test_runner_is_not_a_normal_core_or_observatory_service() -> None:
    runner = _services()["aura-evaluation-runner"]
    assert runner["profiles"] == ["observatory"]
    assert "ports" not in runner
    assert set(runner["networks"]) == {
        "aura-core-db",
        "aura-evaluation-bus",
        "observatory-object-storage",
        "observatory-ingest",
    }
    assert "observatory-db" not in runner["networks"]
    assert "observatory-session" not in runner["networks"]
    assert "observatory-ingest" in runner["networks"]
    assert "aura-core-bus" not in runner["networks"]
    assert "aura-core-cache" not in runner["networks"]
    assert runner["read_only"] is True
    assert runner["cpus"] == "1.0"
    assert runner["mem_limit"] == "768m"
    assert runner["pids_limit"] == 128
    assert runner["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=64m"]
    assert "aura-evaluation-runner-garage-access-key" in runner["secrets"]
    assert "observatory-ingest-credential" in runner["secrets"]
    assert "observatory-garage-access-key" not in runner["secrets"]
    assert "aura-core-database-password" not in runner["secrets"]
    environment = runner["environment"]
    assert (
        environment["AURA_TELEMETRY_OTLP_CREDENTIAL_FILE"]
        == "/run/secrets/observatory-ingest-credential"
    )
    assert environment["AURA_TELEMETRY_OTLP_QUEUE_CAPACITY"]
    assert "AURA_OLLAMA_URL" not in environment
    assert (
        environment["AURA_EVALUATION_RUNTIME_FIXTURE_REVISION"]
        == "${AURA_EVALUATION_RUNTIME_FIXTURE_REVISION:-}"
    )
    assert (
        environment["AURA_EVALUATION_RUNTIME_PROMPT_REVISION"]
        == "${AURA_EVALUATION_RUNTIME_PROMPT_REVISION:-prompt-v1}"
    )
    assert (
        environment["AURA_EVALUATION_RUNTIME_POLICY_REVISION"]
        == "${AURA_EVALUATION_RUNTIME_POLICY_REVISION:-policy-v1}"
    )


def test_runner_telemetry_uses_the_existing_private_collector_boundary() -> None:
    services = _services()
    collector = services["observatory-collector"]
    assert collector["profiles"] == ["observatory"]
    assert "ports" not in collector
    assert collector["networks"] == ["observatory-ingest"]
    assert collector["secrets"] == ["observatory-ingest-credential"]
    assert collector["depends_on"]["observatory-api"]["condition"] == "service_healthy"
    runner = services["aura-evaluation-runner"]
    assert "observatory-ingest" in runner["networks"]
    assert runner["environment"]["AURA_TELEMETRY_OTLP_CREDENTIAL_FILE"] == (
        "/run/secrets/observatory-ingest-credential"
    )


def test_runner_database_bootstrap_grants_only_ledger_tables() -> None:
    services = _services()
    bootstrap = services["aura-evaluation-runner-db-bootstrap"]
    assert bootstrap["profiles"] == ["observatory"]
    assert set(bootstrap["secrets"]) == {
        "aura-core-database-password",
        "aura-evaluation-runner-database-password",
    }
    assert (
        "GRANT SELECT, INSERT, UPDATE ON TABLE evaluation_runner_attempts"
        in str(bootstrap["command"])
    )
    assert "GRANT REFERENCES ON TABLE evaluation_runner_attempts" in str(bootstrap["command"])
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public" in str(bootstrap["command"])
    assert (
        "\\getenv runner_password AURA_EVALUATION_RUNNER_DATABASE_PASSWORD"
        in str(bootstrap["command"])
    )
    assert "-v runner_password=" not in str(bootstrap["command"])
    assert 'runner_password="$$(cat /run/secrets/' not in str(bootstrap["command"])
    assert "aura-evaluation-runner-database-password" in repr(services["aura-evaluation-runner"])


def test_runner_garage_bootstrap_is_distinct_and_owner_denied() -> None:
    services = _services()
    bootstrap = services["aura-evaluation-runner-garage-bootstrap"]
    command = str(bootstrap["command"])
    assert "bucket allow --read --write --key" in command
    assert "bucket deny --owner --key" in command
    assert "expected RW" in command
    assert "AURA_GARAGE_BUCKET" in command
    assert "AURA_OBSERVATORY_GARAGE_BUCKET" in command
    assert "aura-evaluation-runner-garage-access-key" in repr(bootstrap)
    assert "observatory-garage-access-key" not in repr(bootstrap)
    assert "extract_authorized_buckets" in command
    assert "key info" in command
    assert "target_bucket_count" in command


def test_jetstream_bootstrap_declares_bounded_stream_and_durables() -> None:
    services = _services()
    bootstrap = services["aura-evaluation-nats-bootstrap"]
    assert bootstrap["profiles"] == ["observatory"]
    assert "aura-evaluation-bus" in bootstrap["networks"]
    script = (ROOT / "deploy/docker/nats-evaluation-bootstrap.sh").read_text(encoding="utf-8")
    assert "AURA_EVALUATION_RUNNER" in script
    assert '"max_age": 604800000000000' in script
    assert '"max_bytes": 67108864' in script
    assert '"max_msg_size": 1048576' in script
    assert '"ack_policy": "explicit"' in script
    assert '"max_deliver": 10' in script
    assert '"max_ack_pending": 128' in script
    for durable in (
        "aura-evaluation-dispatch",
        "aura-core-evaluation-runner",
        "aura-core-evaluation-cancellation",
    ):
        assert durable in script


def test_runner_e2e_is_project_scoped_and_does_not_print_credentials() -> None:
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    assert "docker compose -p \"$project\"" in script
    assert "COMPOSE_PARALLEL_LIMIT=1" in script
    assert "mktemp -d \"$ROOT/.aura-evaluation-runner-e2e." in script


def test_runner_outage_probe_catches_up_exact_target_terminal() -> None:
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    assert "outage_fetch_deadline = asyncio.get_running_loop().time() + 30" in script
    assert "target_attempt_id = payload.get(\"attemptId\")" in script
    assert "if terminal.get(\"attemptId\") != target_attempt_id:" in script
    assert "if terminal.get(\"messageType\") in TERMINAL_TYPES:" in script
    assert "await message.ack()" in script
    assert 'terminal.get("messageType") != "evaluation.runner.completed"' in script
    assert "bounded_terminal_mismatch(terminal, reason=\"target_terminal\")" in script
    assert "runner outage-independence target terminal was invalid" in script
    assert "bounded_terminal_mismatch(terminal, reason=\"unrelated_non_terminal\")" in script
    assert "bounded_terminal_mismatch({}, reason=\"json\")" in script
    assert "failure_code=" in script
    assert "reason_code=" in script
    assert "file=sys.stderr" in script
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert 'grep -Fq "_$network"' in script
    assert "trap cleanup EXIT INT TERM" in script
    assert "down --volumes --remove-orphans" in script
    assert "docker system prune" not in script
    assert "docker volume rm" not in script
    assert "echo \"$value\"" not in script
    assert "AURA-0091 evaluation runner deployment acceptance passed" in script


def test_runner_outage_shell_status_branches_are_not_inverted() -> None:
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    outage_block = script.split('if [ "$telemetry_acceptance" = true ]; then', 1)[1]
    assert 'if "${compose[@]}" run --rm --no-deps -T \\' in outage_block
    assert 'if ! "${compose[@]}" run --rm --no-deps -T \\' not in outage_block

    for status, expected in ((0, "success"), (1, "failure")):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"if (exit {status}); then printf success; else printf failure; fi",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout == expected


def test_production_state_root_bindings_are_preserved_and_e2e_uses_disposable_volumes() -> None:
    production = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    state_root = "${AURA_STATE_ROOT:?encrypted state root is required}"
    for path, target in (
        ("aura-core-postgres", "/var/lib/postgresql/data"),
        ("aura-nats", "/data"),
        ("aura-garage", "/var/lib/garage"),
        ("aura-garage-config", "/run/garage-config"),
        ("aura-observatory-postgres", "/var/lib/postgresql/data"),
        ("aura-observatory-clickhouse", "/var/lib/clickhouse"),
        ("aura-observatory-clickhouse-logs", "/var/log/clickhouse-server"),
        ("aura-observatory-session", "/data"),
    ):
        assert f"{state_root}/{path}:{target}" in production
    assert 'override_file="$fixture_root/compose.e2e.yaml"' in script
    assert '"$ROOT/compose.yaml" -f "$override_file"' in script
    for volume in (
        "e2e_core_postgres_data",
        "e2e_nats_data",
        "e2e_garage_data",
        "e2e_garage_config",
        "e2e_observatory_postgres_data",
        "e2e_observatory_clickhouse_data",
        "e2e_observatory_clickhouse_logs",
        "e2e_observatory_session_data",
    ):
        assert f"{volume}:" in script
    assert "--volumes --remove-orphans" in script
    override = script.split("cat >\"$override_file\" <<'EOF'", 1)[1].split("\nEOF", 1)[0]
    assert "\n  external:" not in override
    assert "\n  name:" not in override


def test_observatory_postgres_healthcheck_renders_shell_escape_without_nul() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "--profile",
            "observatory",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rendered = json.loads(result.stdout)
    healthcheck = rendered["services"]["observatory-postgres"]["healthcheck"]["test"]
    command = healthcheck[1]
    assert healthcheck[0] == "CMD-SHELL"
    assert "\x00" not in command
    assert "tr -d '\\000'" in command


def test_runner_e2e_teardown_is_narrow_and_logs_are_not_forwarded() -> None:
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    assert 'find "$fixture_root" -depth -delete' in script
    assert 'docker compose -p "$project"' in script
    assert "docker compose down" not in script
    assert "docker logs" not in script
    assert "compose[@]} logs" not in script
    assert "AURA_EVALUATION_RUNNER_MAX_QUEUE_DEPTH=4" in script
    assert "AURA_EVALUATION_RUNNER_MAX_CONCURRENCY=1" in script
    assert "aura.evaluation.runner.requested.v1" in script
    assert "aura.evaluation.runner.cancellation.v1" in script
    assert "aura-evaluation-dispatch" in script
    assert "aura-evaluation-bootstrap" in script
    assert "synthetic_end_to_end" in script
    assert '"aura.evaluation.synthetic"' in script
    assert 'execution_kind="module_replay"' in script
    assert 'component_id="aura.knowledge.document_processing"' in script
    assert '"synthetic-run-coordinator-v1"' in script
    assert '"document-library-eval-fixtures-v1"' in script
    assert 'fixture_revision = fixture_revisions[execution_kind]' in script
    assert '"inputArtifactReferences"' in script
    assert 'input_references=[input_reference]' in script
    assert (
        'declared_model_calls = 1 if execution_kind == "synthetic_end_to_end" else 0'
        in script
    )
    assert "declared_artifact_operations = len(input_references)" in script
    assert (
        'declared_artifact_operations += 1' in script
        and 'execution_kind == "module_replay"' in script
    )
    assert '"maxModelCalls": declared_model_calls' in script
    assert '"maxArtifactOperations": declared_artifact_operations' in script
    assert '"fixtureRevision"' in script
    assert '"policyRevision"' in script
    assert "runner_privacy_logs" in script
    assert "nats_privacy_logs" in script
    assert "runner_forbidden_markers" in script
    assert "nats_forbidden_markers" in script
    assert "static upstream" in script
    assert 'http://aura-' in script
    assert "runner Garage key reached the Core bucket" in script
    assert "production replay output artifact integrity failed" in script
    assert "production replay output artifact deletion was not reachable" in script
    assert '"aura.knowledge.document_processing"' in script
    assert "outputArtifactReferences" in script
    assert "EvaluationArtifactAdapter(eval_store" in script
    assert (
        "def validate_fixture_message(payload: dict[str, object], *, fixture_name: str)"
        in script
    )
    assert "validate_message(payload)" in script
    assert "fixture contract invalid before publish" in script
    assert "fixture contract invalid before redelivery publish" in script
    assert "race_projection = race_attempt.request_projection" in script
    assert "race_deadline = datetime.fromisoformat(" in script
    assert "request_id=race_attempt.request_id" in script
    assert "job_id=race_attempt.job_id" in script
    assert "idempotency_key=race_attempt.idempotency_key" in script
    assert "request_digest=race_attempt.request_digest" in script
    assert "deadline_at=race_deadline" in script
    assert 'request_digest=cancel_request["requestDigest"]' in script
    assert script.index("cancel_request = request_payload(") < script.index(
        "cancellation = envelope("
    )
    assert "attempt_id=cancel_attempt" in script
    assert "request_id=cancel_request_id" in script
    assert "job_id=cancel_job_id" in script
    assert "idem=cancel_idem" in script
    assert "async def assert_runner_database_ready()" in script
    assert 'await connection.execute(text("SELECT 1"))' in script
    assert (
        'text("SELECT attempt_id FROM evaluation_runner_attempts LIMIT 1")'
        in script
    )
    assert "await assert_runner_database_ready()" in script
    assert "ledger_query_status={ledger_query}" in script
    assert "poison_metric_present=false" in script
    assert "terminal_fetch_poison_metric_present=%s" in script
    assert "evaluation_runner_nats_poison_count" in script
    for fixture_name in (
        'fixture_name="synthetic request"',
        'fixture_name="module replay request"',
        'fixture_name="owner cancellation"',
        'fixture_name="deadline request"',
    ):
        assert fixture_name in script
    assert "def runner_object_key(reference):" in script
    assert "async def read_runner_object(store, reference):" in script
    assert "async def assert_runner_object_deleted(store, reference, failure_message):" in script
    assert "UUID(str(reference['artifactReferenceId'])).hex" in script
    assert "await read_runner_object(eval_store, output_reference)" in script
    assert "await assert_runner_object_deleted(" in script
    assert "await replay_adapter.read(output_reference)" not in script
    assert "await adapter.read(output_reference)" not in script
    assert "runner structured telemetry assertion failed" in script
    assert "terminal_fetch_health_metrics" in script
    assert "AURA_E2E_ASSERT_RUNNER_TELEMETRY" in script
    assert 'AURA_E2E_COMPOSE_START_TIMEOUT_SECONDS:-600' in script
    assert 'AURA_E2E_COMPOSE_CLEANUP_TIMEOUT_SECONDS:-60' in script
    assert 'AURA_E2E_COMPOSE_DIAGNOSTIC_TIMEOUT_SECONDS:-20' in script
    assert 'timeout --foreground --signal=TERM --kill-after=10s' in script
    assert 'evaluation Compose startup failed or timed out' in script
    assert 'scoped Compose cleanup failed or timed out' in script
    assert 'ps --all' in script
    assert "AURA_TELEMETRY_OTLP_ENABLED=$telemetry_acceptance" in script
    assert "observatory-collector aura-evaluation-runner" in script
    assert "stop observatory-collector observatory-api" in script
    assert "telemetry_deadline=$((SECONDS + 20))" in script
    assert "capture_policy = 'metadata_only'" in script
    assert "runner did not execute while Collector was unavailable" in script
    assert "/run/secrets/observatory-clickhouse-password" in script
    for metric in (
        "evaluation_runner_queue_time_ms",
        "evaluation_runner_fixture_load_duration_ms",
        "evaluation_runner_execution_duration_ms",
        "evaluation_runner_outcome_count",
        "evaluation_runner_cancellation_count",
        "evaluation_runner_resource_limit_count",
        "evaluation_runner_reproducibility_completeness",
        "evaluation_runner_lease_renewal_failure_count",
        "evaluation_runner_lease_lost_count",
        "evaluation_runner_artifact_cleanup_failure_count",
        "evaluation_runner_recovery_scan_failure_count",
        "evaluation_runner_recovery_count",
        "evaluation_runner_publication_recovery_failure_count",
        "evaluation_runner_terminal_accounting_failure_count",
        "evaluation_runner_nats_identity_conflict_count",
    ):
        assert metric in script
    assert "aura-e2e-dispatch" in script
    assert "aura-e2e-runner" in script
    assert "conversations LIMIT 1" in script
    assert "completed" in script
    assert "runner-e2e-roundtrip" in script
    assert "max-deliver" in script
    assert (
        "$JS.API.CONSUMER.MSG.NEXT.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner"
        not in script
    )
    assert 'delivered.get("consumer_seq")' in script
    assert 'delivered.get("stream_seq")' in script
    assert "pub_ack = await connection.jetstream().publish" in script
    assert "pub_ack.seq" in script
    assert "redelivery-publish-state.json" in script
    assert (
        '-v "$probe_root:/run/e2e-probe:rw" \\\n'
        '  -e E2E_DISPATCH_PASSWORD_FILE'
    ) in script
    assert script.count('-v "$probe_root:/run/e2e-probe:rw"') == 3
    assert script.count('-v "$probe_root:/run/e2e-probe:ro"') == 3
    assert "expected_stream_seq" in script
    assert "stream_seq == expected_stream_seq" in script
    assert 'info.get("num_ack_pending")' in script
    assert 'runner.get("num_redelivered")' in script
    assert 'ack_floor.get("consumer_seq")' in script
    assert 'ack_floor.get("stream_seq")' in script
    assert "redelivery-consumer-state.json" in script
    assert "stop_at = loop.time() + 10" in script
    assert "consumer_seq == initial_consumer_seq + 1" in script
    assert "stream_seq >= initial_stream_seq" in script
    assert "ack_floor_consumer_seq == initial_consumer_seq + 1" in script
    assert "ack_floor_stream_seq >= initial_stream_seq" in script
    assert "ack_pending == 0" in script
    assert "production runner did not settle the durable redelivery" in script
    assert "AURA_E2E redelivery_sequence_diagnostic" in script
    assert "import sys\n\n    initial = json.loads(" in script
    assert "file=sys.stderr" in script
    assert "delivered_stream_at_or_after_request" in script
    assert "ack_floor_stream_at_or_after_request" in script
    assert "ack_floor_consumer_seq_delta" in script
    assert "redelivered >= 1" not in script
    assert script.count(
        "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner"
    ) == 4
    assert "durable runner did not retain the initial unacknowledged delivery" in script
    assert "Restart the production runner after the ack-wait interval" in script
    assert "privacy" in script


def test_runner_e2e_request_provenance_uses_deployed_telemetry_revision() -> None:
    script = (ROOT / "deploy/compose/run-evaluation-runner-e2e.sh").read_text(encoding="utf-8")
    assert "AURA_TELEMETRY_DEPLOYMENT_ID=runner-e2e-telemetry" in script
    assert "def telemetry_deployment_revision()" in script
    assert 'os.environ.get("AURA_TELEMETRY_DEPLOYMENT_ID", "local")' in script
    assert '"deploymentRevision": telemetry_deployment_revision()' in script
    assert '"deploymentRevision": "local"' not in script
    assert "len(value) > 128" in script
    assert "contains sensitive metadata" in script


def test_runner_dockerfile_is_separate_and_secret_safe() -> None:
    dockerfile = (ROOT / "deploy/docker/aura-evaluation-runner.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "services/aura-core/" in dockerfile
    assert "aura-core-entrypoint.sh" in dockerfile
    assert "nats-evaluation-bootstrap.sh" in dockerfile
    assert "USER aura" in dockerfile
    assert "EXPOSE" not in dockerfile


def test_modules_register_runner_boundary() -> None:
    modules = _document(ROOT / "tooling/architecture/modules.yaml")["modules"]
    ids = {str(cast(dict[str, Any], item)["id"]) for item in modules}
    assert {
        "aura-core.runtime.replay",
        "aura-core.platform.evaluation-transport",
        "aura-core.entrypoints.evaluation-runner",
    } <= ids
