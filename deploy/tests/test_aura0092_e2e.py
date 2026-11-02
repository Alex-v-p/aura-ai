"""Static contract checks for the project-scoped AURA-0092 acceptance."""

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy/compose/run-evaluation-orchestration-e2e.sh"


def test_orchestration_e2e_is_bounded_and_project_scoped() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "export COMPOSE_PARALLEL_LIMIT=1" in script
    assert 'project="aura-0092-e2e-' in script
    assert 'mktemp -d "$ROOT/.aura-0092-e2e.' in script
    assert 'timeout --foreground --signal=TERM --kill-after=10s' in script
    assert 'docker compose -p "$project"' in script
    assert "down --volumes --remove-orphans" in script
    assert "docker system prune" not in script
    assert "docker volume rm" not in script
    assert 'find "$fixture_root" -depth -delete' in script


def test_orchestration_e2e_preserves_production_state_bindings() -> None:
    production = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    assert "${AURA_STATE_ROOT:?encrypted state root is required}" in production
    assert 'override_file="$fixture_root/compose.e2e.yaml"' in script
    assert '"$ROOT/compose.yaml" -f "$override_file"' in script
    assert "e2e_core_postgres_data" in script
    assert "e2e_observatory_postgres_data" in script
    assert "e2e_observatory_clickhouse_data" in script
    assert "e2e_observatory_session_data" in script
    override = script.split('cat >"$override_file" <<EOF', 1)[1].split("\nEOF", 1)[0]
    assert "external:" not in override
    assert "name:" not in override


def test_orchestration_e2e_drives_internal_state_and_real_runner() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "observatory-evaluation-scheduler" in script
    assert "PostgresEvaluationOrchestrationRepository" in script
    assert "EvaluationAdmission" in script
    assert "EvaluationOrchestrationService" in script
    assert "aura-evaluation-runner" in script
    assert '"phase": "durable_result"' in script
    assert "metadata_only" in script
    assert "inputArtifactReferences" in script
    assert "observatory.localhost" not in script
    assert "curl" not in script
    assert "aura0092.example.invalid" in script


def test_observatory_image_packages_all_transitive_runner_schema_documents() -> None:
    """A copied root schema is insufficient when it has external refs."""
    schema_path = ROOT / "contracts/evaluation/v1/evaluation-runner-transport.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    references: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            reference = mapping.get("$ref")
            if isinstance(reference, str) and reference and not reference.startswith("#"):
                references.add(reference.split("#", 1)[0])
            for child in mapping.values():
                collect(child)
        elif isinstance(value, list):
            for child in cast(list[Any], value):
                collect(child)

    collect(schema)
    dockerfile = (ROOT / "deploy/docker/aura-observatory.Dockerfile").read_text(
        encoding="utf-8"
    )
    complete_directory_copy = (
        "COPY contracts/evaluation/v1/ ./contracts/evaluation/v1/" in dockerfile
    )
    for reference in references:
        filename = Path(reference).name
        assert complete_directory_copy or f"COPY contracts/evaluation/v1/{filename}" in dockerfile


def test_orchestration_e2e_does_not_forward_secrets_or_content() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "chmod 0600" in script
    assert "secret_root" in script
    assert 'print(json.dumps({"credentials"' not in script
    assert 'print(json.dumps({"phase": "admitted"' in script
    assert 'print(json.dumps({"phase": "durable_result"' in script
    assert 'print(json.dumps({"prompt"' not in script
    assert 'print(json.dumps({"response"' not in script
    assert 'print(json.dumps({"content"' not in script


def test_orchestration_e2e_covers_recovery_races_and_core_independence() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for phase in (
        "restart_recovery",
        "cancel",
        "deadline",
        "deadline_admit",
        "deadline_verify",
        "duplicate",
        "health_privacy",
        "schedule_seed",
        "schedule_verify",
        "schedule_recovery",
        "heartbeat_stale",
        "postgres_outage",
        "nats_outage",
        "outage_admit",
        "recovery",
    ):
        assert f'run_probe {phase}' in script
    assert "run_dispatch_probe jetstream_redelivery" in script
    assert 'durable="aura-evaluation-dispatch"' in script
    assert 'aura.evaluation.runner.results.v1' in script
    assert 'last_error_code' in script
    assert 'stop aura-nats' in script
    assert 'start aura-nats' in script
    assert 'if ! wait_running "$service"; then' in script
    assert 'wait_running aura-evaluation-runner' in script
    assert 'restart observatory-evaluation-worker' in script
    assert 'stop observatory-evaluation-worker observatory-evaluation-scheduler' in script
    assert (
        'up --build --detach observatory-api observatory-collector '
        'observatory-evaluation-worker'
    ) in script
    assert (
        'wait_running observatory-api observatory-collector '
        'observatory-evaluation-worker'
    ) in script
    assert (
        '--volume "$secret_root/observatory-clickhouse-password:'
        '/run/secrets/observatory-clickhouse-password:ro"'
    ) in script
    assert 'wait_running aura-evaluation-runner' in script
    assert 'observatory_evaluation_worker_leases' in script
    assert 'observatory_evaluation_result_receipts' in script
    assert 'AURA_E2E_REDELIVERY_JOB_ID' in script
    assert 'receipt_count' in script
    redelivery_verify = script.split('if phase == "redelivery_verify":', 1)[1].split(
        'elif phase == "deadline_verify":', 1
    )[0]
    assert "a.attempt_id=r.attempt_id" in redelivery_verify
    assert "a.event_type NOT IN ('evaluation.retry_scheduled'" in redelivery_verify
    assert "WHERE event_type='evaluation.schedule_materialized'" not in redelivery_verify
    assert 'to_jsonb(row)::text' in script
    assert 'observatory_evaluation_operation_duration_ms_count' in script
    assert 'SessionService' in script
    assert 'RedisSessionBackend' in script
    assert 'Principal(settings.oidc_issuer, settings.owner_subject' in script
    assert 'observatory-evaluation-scheduler aura-evaluation-runner observatory-api' in script
    assert 'provider_diagnostic' in script
    assert 'Traceback' in script
    assert 'observatory_evaluation_operation_duration_ms_bucket' in script
    assert 'privacy or raw-diagnostic scan failed' in script
    assert 'AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED: "true"' in script
    assert 'http://observatory-collector:4318/v1/traces' in script
    assert (
        'secrets: [observatory-database-password, aura-nats-dispatch-password, '
        'observatory-ingest-credential]'
    ) in script
    assert 'networks: [observatory-db, aura-evaluation-bus, observatory-ingest]' in script
    assert 'secrets: [observatory-database-password, observatory-ingest-credential]' in script
    assert 'networks: [observatory-db, observatory-ingest]' in script
    assert 'run_probe trace_verify' in script
    assert 'stop observatory-api' in script
    assert 'start observatory-api' in script
    assert 'Collector outage diagnostics leaked an endpoint or secret path' in script
    assert 'collector_outage_logs' in script
    assert 'collector_outage_metrics' in script
    assert 'collector_recovered_metrics' in script
    assert 'sensitive_label_names' in script
    assert 'server_address' in script
    assert 'server_port' in script
    assert 'Collector self-telemetry sensitive label name matched' in script
    assert 'Collector self-telemetry sensitive label value matched' in script
    assert 'Collector self-telemetry URI value matched' in script
    assert 'Collector self-telemetry endpoint unavailable' in script
    assert 'sensitive_value = re.compile' in script
    assert 'otelcol_exporter_queue_size' in script
    assert 'otelcol_exporter_queue_batch_send_size_count' in script
    assert 'collector_recovery_deadline=$((SECONDS + step_timeout))' in script
    assert 'Collector retry queue did not drain within the bounded recovery window' in script
    assert 'bounded_log_categories=' in script
    assert 'printf \'%s\\n\' "$output" >&2' not in script
    assert 'AURA_E2E_TRACE_ONLY' in script
    assert '"phase":"trace_only"' in script
    assert 'exported_persisted' in script
    assert "kind='span'" in script
    assert "aura.observatory.evaluation_orchestration" in script
    assert "aura.observatory.evaluation_scheduler" in script
    assert 'deployment_id' in script
    assert 'capture_policy' in script
    assert (
        'for service in observatory-collector observatory-evaluation-worker '
        'observatory-evaluation-scheduler aura-evaluation-runner observatory-api; do'
    ) in script
    assert (
        'for service in observatory-collector observatory-evaluation-worker '
        'observatory-evaluation-scheduler aura-evaluation-runner; do'
    ) in script


def test_orchestration_e2e_allows_only_the_fixed_observatory_startup_line() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    startup_line = re.compile(
        r"^INFO:\s+Uvicorn running on http://0\.0\.0\.0:8000 "
        r"\(Press CTRL\+C to quit\)$"
    )
    assert startup_line.fullmatch(
        "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)"
    )

    # These are deliberately not covered by the exact exception and must remain
    # visible to the broad privacy/raw-diagnostic scanner.
    for disallowed in (
        "INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)",
        "INFO:     Uvicorn running on https://0.0.0.0:8000 (Press CTRL+C to quit)",
        "INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)",
        "provider error: unavailable",
        "Traceback (most recent call last):",
    ):
        assert startup_line.fullmatch(disallowed) is None

    assert 'if [ "$service" = "observatory-api" ]; then' in script
    assert 'logs --no-color --no-log-prefix "$service"' in script
    assert r"^INFO:[[:space:]]+Uvicorn running on http:\/\/0\.0\.0\.0:8000" in script
    for scanner_marker in (
        "https?://",
        "nats://",
        "postgresql://",
        "/run/secrets/",
        "provider[_-]?(diagnostic|error)",
        "Traceback",
    ):
        assert scanner_marker in script


def test_privacy_scan_rejects_collector_endpoint_and_secret_filename() -> None:
    scanner = re.compile(
        r"Traceback|^[\s]*File \"|provider[_-]?(diagnostic|error)|"
        r"https?://|nats://|postgresql://|/run/secrets/[A-Za-z0-9_.-]+|"
        r"\"(prompt|response|content|filename|tool|argument|credentials?|endpoint|"
        r"bearer|owner|identity|secret|password|token|url|uri)\"[\s]*[:=]",
        re.IGNORECASE,
    )
    assert scanner.search("export failed: http://observatory-api:4318/v1/traces")
    assert scanner.search("reading /run/secrets/observatory-ingest-credential")


def test_trace_verify_failure_diagnostics_are_bounded_aggregates() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    trace_verify = script.split('if phase == "trace_verify":', 1)[1].split(
        'if phase in {"heartbeat_fresh"', 1
    )[0]
    for field in (
        '"span_count"',
        '"component_count"',
        '"trace_count"',
        '"operation_count"',
        '"dependency_count"',
        '"missing_operations"',
        '"missing_dependencies"',
        '"missing_components"',
        '"failure_span"',
        '"privacy_ok"',
        '"duplicate_span"',
    ):
        assert field in trace_verify
    assert '"state": "failed"' in trace_verify
    assert '"diagnostics": diagnostics' in trace_verify
    # Failure output must not serialize raw ClickHouse rows or identifiers.
    assert '"rows": rows' not in trace_verify
    assert '"trace_ids"' not in trace_verify
    assert '"span_ids"' not in trace_verify
