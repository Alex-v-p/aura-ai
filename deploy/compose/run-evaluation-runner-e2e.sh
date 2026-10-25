#!/usr/bin/env bash
set -Eeuo pipefail

# Project-scoped, low-memory acceptance for the Core evaluation runner. The
# fixture creates opaque credentials and state under an ignored temporary
# directory, never prints secret contents or evaluation payloads, and always
# removes only this Compose project.
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
export COMPOSE_PARALLEL_LIMIT=1
fixture_root=$(mktemp -d "$ROOT/.aura-evaluation-runner-e2e.XXXXXX")
project="aura-evaluation-runner-e2e-$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')"
telemetry_acceptance="${AURA_E2E_ASSERT_RUNNER_TELEMETRY:-false}"
case "$telemetry_acceptance" in
  true|false) ;;
  *) echo 'AURA_E2E_ASSERT_RUNNER_TELEMETRY must be true or false' >&2; exit 2 ;;
esac
compose_start_timeout="${AURA_E2E_COMPOSE_START_TIMEOUT_SECONDS:-600}"
compose_cleanup_timeout="${AURA_E2E_COMPOSE_CLEANUP_TIMEOUT_SECONDS:-60}"
compose_diagnostic_timeout="${AURA_E2E_COMPOSE_DIAGNOSTIC_TIMEOUT_SECONDS:-20}"
validate_timeout() {
  local name=$1 value=$2 minimum=$3 maximum=$4
  case "$value" in
    ''|*[!0-9]*) echo "$name must be an integer number of seconds" >&2; exit 2 ;;
  esac
  if [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
    echo "$name must be between $minimum and $maximum seconds" >&2
    exit 2
  fi
}
validate_timeout AURA_E2E_COMPOSE_START_TIMEOUT_SECONDS "$compose_start_timeout" 30 1800
validate_timeout AURA_E2E_COMPOSE_CLEANUP_TIMEOUT_SECONDS "$compose_cleanup_timeout" 5 300
validate_timeout AURA_E2E_COMPOSE_DIAGNOSTIC_TIMEOUT_SECONDS "$compose_diagnostic_timeout" 5 120

# Compose health waits and daemon calls are external processes. Keep every
# acceptance-stage Compose operation bounded, including failure diagnostics and
# scoped teardown; no command here can wait indefinitely for Docker.
bounded_command() {
  local seconds=$1
  shift
  timeout --foreground --signal=TERM --kill-after=10s "${seconds}s" "$@"
}
env_file="$fixture_root/.env"
override_file="$fixture_root/compose.e2e.yaml"
secret_root="$fixture_root/secrets"
state_root="$fixture_root/state"
probe_root="$fixture_root/probe"
mkdir -p "$secret_root" "$state_root" "$probe_root"
chmod 0700 "$fixture_root" "$secret_root" "$state_root"
chmod 0777 "$probe_root"

write_secret() {
  local name=$1 value=$2
  printf '%s\n' "$value" >"$secret_root/$name"
  chmod 0600 "$secret_root/$name"
}

cleanup() {
  local status=$?
  local -a compose_files=(-f "$ROOT/compose.yaml")
  trap - EXIT INT TERM
  if [ -f "$override_file" ]; then
    compose_files+=(-f "$override_file")
  fi
  if [ "$status" -ne 0 ] && [ "${AURA_E2E_DEBUG:-false}" = true ] && [ -f "$env_file" ]; then
    bounded_command "$compose_diagnostic_timeout" docker compose -p "$project" \
      --env-file "$env_file" "${compose_files[@]}" --profile observatory \
      logs --no-color --tail=80 aura-evaluation-runner-db-bootstrap >&2 || true
    bounded_command "$compose_diagnostic_timeout" docker compose -p "$project" \
      --env-file "$env_file" "${compose_files[@]}" --profile observatory \
      logs --no-color --tail=120 aura-evaluation-runner aura-nats >&2 || true
    bounded_command "$compose_diagnostic_timeout" docker compose -p "$project" \
      --env-file "$env_file" "${compose_files[@]}" --profile observatory \
      exec -T aura-core-postgres sh -ec \
      'export PGPASSWORD="$(cat /run/secrets/aura-core-database-password)"; psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT status,publication_status,(terminal_payload IS NOT NULL) FROM evaluation_runner_attempts ORDER BY created_at DESC LIMIT 3"' >&2 || true
  fi
  if [ -f "$env_file" ]; then
    if bounded_command "$compose_cleanup_timeout" docker compose -p "$project" \
      --env-file "$env_file" "${compose_files[@]}" --profile observatory \
      down --volumes --remove-orphans >/dev/null 2>&1; then
      :
    else
      cleanup_status=$?
      echo "scoped Compose cleanup failed or timed out after ${compose_cleanup_timeout}s" >&2
      bounded_command "$compose_diagnostic_timeout" docker compose -p "$project" \
        --env-file "$env_file" "${compose_files[@]}" --profile observatory \
        ps --all >&2 || true
      # Preserve the original test result while making cleanup failure visible.
      if [ "$status" -eq 0 ]; then
        status="$cleanup_status"
      fi
    fi
  fi
  bounded_command "$compose_cleanup_timeout" find "$fixture_root" -depth -delete 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

# Every value is a fixture marker only. The script intentionally does not
# print Compose logs because a provider or test adapter must never make raw
# content part of acceptance output.
write_secret aura-core-database-password runner-e2e-core-database
write_secret aura-evaluation-runner-database-password runner-e2e-runner-database
write_secret aura-nats-password runner-e2e-normal-nats
write_secret aura-nats-bootstrap-password runner-e2e-bootstrap-nats
write_secret aura-nats-dispatch-password runner-e2e-dispatch-nats
write_secret aura-nats-runner-password runner-e2e-runner-nats
write_secret aura-garage-rpc-secret 0000000000000000000000000000000000000000000000000000000000000000
write_secret aura-garage-access-key GK000000000000000000000000
write_secret aura-garage-secret-key 1111111111111111111111111111111111111111111111111111111111111111
write_secret observatory-garage-access-key GK111111111111111111111111
write_secret observatory-garage-secret-key 2222222222222222222222222222222222222222222222222222222222222222
write_secret observatory-database-password runner-e2e-observatory-database
write_secret observatory-oidc-client-secret runner-e2e-observatory-oidc
write_secret observatory-session-password runner-e2e-observatory-session
write_secret observatory-ingest-credential runner-e2e-observatory-ingest
write_secret observatory-clickhouse-password runner-e2e-observatory-clickhouse
write_secret aura-evaluation-runner-garage-access-key GK222222222222222222222222
write_secret aura-evaluation-runner-garage-secret-key 3333333333333333333333333333333333333333333333333333333333333333

cat >"$env_file" <<EOF
AURA_STATE_ROOT=$state_root
AURA_PUBLIC_ORIGIN=https://runner-e2e.invalid
AURA_OIDC_ISSUER=https://authentik.runner-e2e.invalid/application/o/aura-web/
AURA_OIDC_AUDIENCE=aura-web
AURA_OIDC_CLIENT_ID=aura-web
AURA_OIDC_REDIRECT_URI=https://runner-e2e.invalid/api/v1/auth/callback
AURA_OWNER_SUBJECT=runner-e2e-owner
AURA_CORE_DATABASE_PASSWORD_FILE=$secret_root/aura-core-database-password
AURA_EVALUATION_RUNNER_DATABASE_PASSWORD_FILE=$secret_root/aura-evaluation-runner-database-password
AURA_EVALUATION_RUNNER_DATABASE_USER=aura_evaluation_runner
AURA_NATS_PASSWORD_FILE=$secret_root/aura-nats-password
AURA_NATS_BOOTSTRAP_PASSWORD_FILE=$secret_root/aura-nats-bootstrap-password
AURA_NATS_DISPATCH_PASSWORD_FILE=$secret_root/aura-nats-dispatch-password
AURA_NATS_RUNNER_PASSWORD_FILE=$secret_root/aura-nats-runner-password
AURA_GARAGE_RPC_SECRET_FILE=$secret_root/aura-garage-rpc-secret
AURA_GARAGE_ACCESS_KEY_FILE=$secret_root/aura-garage-access-key
AURA_GARAGE_SECRET_KEY_FILE=$secret_root/aura-garage-secret-key
AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE=$secret_root/observatory-garage-access-key
AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE=$secret_root/observatory-garage-secret-key
AURA_OBSERVATORY_DATABASE_PASSWORD_FILE=$secret_root/observatory-database-password
AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE=$secret_root/observatory-oidc-client-secret
AURA_OBSERVATORY_SESSION_PASSWORD_FILE=$secret_root/observatory-session-password
AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE=$secret_root/observatory-ingest-credential
AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE=$secret_root/observatory-clickhouse-password
AURA_OBSERVATORY_OWNER_SUBJECT=runner-e2e-owner
AURA_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_FILE=$secret_root/aura-evaluation-runner-garage-access-key
AURA_EVALUATION_RUNNER_GARAGE_SECRET_KEY_FILE=$secret_root/aura-evaluation-runner-garage-secret-key
AURA_GARAGE_BUCKET=aura-documents
AURA_OBSERVATORY_GARAGE_BUCKET=aura-observatory-evaluation
AURA_EVALUATION_RUNNER_MAX_CONCURRENCY=1
AURA_EVALUATION_RUNNER_MAX_QUEUE_DEPTH=4
AURA_EVALUATION_RUNNER_MAX_TIMEOUT_MS=30000
AURA_EVALUATION_RUNNER_MAX_CASES=4
AURA_EVALUATION_RUNNER_MAX_INPUT_BYTES=1048576
AURA_EVALUATION_RUNNER_MAX_OUTPUT_BYTES=1048576
AURA_EVALUATION_RUNNER_MAX_TOKENS=10000
AURA_EVALUATION_RUNNER_MAX_MODEL_CALLS=4
AURA_EVALUATION_RUNNER_MAX_ARTIFACT_OPERATIONS=4
AURA_TELEMETRY_OTLP_ENABLED=$telemetry_acceptance
AURA_TELEMETRY_DEPLOYMENT_ID=runner-e2e-telemetry
EOF
chmod 0600 "$env_file"

# Docker Desktop's classic overlay2 can stall high-write bind mounts during
# health checks.  Keep production's encrypted state-root bindings unchanged,
# but use only project-scoped disposable named volumes for this acceptance.
# Compose prefixes these names with "$project"; no external or explicit volume
# name is allowed, so `down --volumes` removes every generated volume.
cat >"$override_file" <<'EOF'
services:
  aura-core-postgres:
    volumes:
      - e2e_core_postgres_data:/var/lib/postgresql/data
  aura-nats:
    volumes:
      - e2e_nats_data:/data
  aura-garage:
    volumes:
      - e2e_garage_data:/var/lib/garage
      - e2e_garage_config:/run/garage-config:ro
  aura-garage-config:
    volumes:
      - e2e_garage_config:/run/garage-config
  aura-garage-bootstrap:
    volumes:
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-garage-bootstrap:
    volumes:
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  aura-evaluation-runner-garage-bootstrap:
    volumes:
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-postgres:
    volumes:
      - e2e_observatory_postgres_data:/var/lib/postgresql/data
  observatory-clickhouse:
    volumes:
      - e2e_observatory_clickhouse_data:/var/lib/clickhouse
      - e2e_observatory_clickhouse_logs:/var/log/clickhouse-server
  observatory-session:
    volumes:
      - e2e_observatory_session_data:/data
volumes:
  e2e_core_postgres_data: {}
  e2e_nats_data: {}
  e2e_garage_data: {}
  e2e_garage_config: {}
  e2e_observatory_postgres_data: {}
  e2e_observatory_clickhouse_data: {}
  e2e_observatory_clickhouse_logs: {}
  e2e_observatory_session_data: {}
EOF

compose=(docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override_file" --profile observatory)
"${compose[@]}" config --quiet
if [ "$telemetry_acceptance" = true ]; then
  # The existing Collector/API graph is opt-in and shares only the private
  # ingest network. No new service or host port is introduced by acceptance.
  if bounded_command "$compose_start_timeout" "${compose[@]}" up --build --detach --wait observatory-collector aura-evaluation-runner; then
    :
  else
    compose_status=$?
    echo "evaluation Compose startup failed or timed out after ${compose_start_timeout}s" >&2
    bounded_command "$compose_diagnostic_timeout" "${compose[@]}" ps --all >&2 || true
    exit "$compose_status"
  fi
else
  if bounded_command "$compose_start_timeout" "${compose[@]}" up --build --detach --wait aura-evaluation-runner; then
    :
  else
    compose_status=$?
    echo "evaluation Compose startup failed or timed out after ${compose_start_timeout}s" >&2
    bounded_command "$compose_diagnostic_timeout" "${compose[@]}" ps --all >&2 || true
    exit "$compose_status"
  fi
fi

# Topology and independence assertions are deliberately metadata-only.
test -z "$("${compose[@]}" port aura-evaluation-runner 8000 2>/dev/null || true)"
runner_networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' \
  "$project-aura-evaluation-runner-1" 2>/dev/null || true)
for network in aura-core-db aura-evaluation-bus observatory-object-storage observatory-ingest; do
  printf '%s\n' "$runner_networks" | grep -Fq "_$network" || {
    echo 'evaluation runner network isolation check failed' >&2
    exit 1
  }
done
for network in aura-core-bus aura-core-cache aura-core-egress aura-frontend; do
  if printf '%s\n' "$runner_networks" | grep -Fq "_$network"; then
    echo 'evaluation runner joined a normal Core network' >&2
    exit 1
  fi
done

# Exercise live identities and synthetic metadata-only envelopes. This probe
# mounts fixture credentials read-only and emits no broker, database, artifact,
# or model payload.
if "${compose[@]}" run --rm --no-deps -T \
  -v "$secret_root:/run/e2e:ro" \
  -e E2E_NORMAL_PASSWORD_FILE=/run/e2e/aura-nats-password \
  -e E2E_BOOTSTRAP_PASSWORD_FILE=/run/e2e/aura-nats-bootstrap-password \
  -e E2E_DISPATCH_PASSWORD_FILE=/run/e2e/aura-nats-dispatch-password \
  -e E2E_RUNNER_PASSWORD_FILE=/run/e2e/aura-nats-runner-password \
  -e E2E_RUNNER_ACCESS_KEY_FILE=/run/secrets/aura-evaluation-runner-garage-access-key \
  -e E2E_RUNNER_SECRET_KEY_FILE=/run/secrets/aura-evaluation-runner-garage-secret-key \
  -e E2E_CORE_BUCKET=aura-documents \
  -v "$probe_root:/run/e2e-probe:rw" \
  --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import nats
from nats.errors import TimeoutError as NatsTimeoutError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from aura_core.platform.evaluation_transport import (
    TransportValidationError,
    canonical_digest,
    validate_message,
)
from aura_core.providers.object_storage.evaluation import EvaluationArtifactAdapter, EvaluationArtifactError
from aura_core.providers.object_storage.garage import GarageS3ObjectStore
from aura_core.runtime.replay.state import AttemptConflict, AttemptRecord, SqlEvaluationRunnerLedger


def secret(name: str) -> str:
    return Path(os.environ[name]).read_text(encoding="utf-8").strip()


def telemetry_deployment_revision() -> str:
    """Use the runner's bounded deployment identity in every fixture request."""
    value = os.environ.get("AURA_TELEMETRY_DEPLOYMENT_ID", "local")
    if (
        not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-"
            for character in value
        )
    ):
        raise ValueError("fixture deployment revision is not a bounded identifier")
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "credential",
            "bearer",
            "owner",
            "identity",
            "private",
            "session",
            "cookie",
            "endpoint",
            "prompt",
            "response",
            "content",
        )
    ):
        raise ValueError("fixture deployment revision contains sensitive metadata")
    return value


async def denied(user: str, password: str, operation) -> None:
    errors = []
    async def error_callback(error) -> None:
        errors.append(error)
    connection = await nats.connect(f"nats://{user}:{password}@aura-nats:4222", error_cb=error_callback)
    try:
        try:
            await asyncio.wait_for(operation(connection), timeout=2)
            await connection.flush(timeout=1)
        except Exception:
            return
        await asyncio.sleep(0.3)
        if errors:
            return
    finally:
        await connection.close()
    raise RuntimeError("ACL denial probe unexpectedly succeeded")


async def allowed(user: str, password: str, operation) -> None:
    errors = []
    async def error_callback(error) -> None:
        errors.append(error)
    connection = await nats.connect(f"nats://{user}:{password}@aura-nats:4222", error_cb=error_callback)
    try:
        await asyncio.wait_for(operation(connection), timeout=3)
        await connection.flush(timeout=1)
        await asyncio.sleep(0.1)
        if errors:
            raise RuntimeError("ACL allow probe failed")
    finally:
        await connection.close()


async def publish_plain(connection, subject: str) -> None:
    await connection.publish(subject, b"probe")


async def subscribe_plain(connection, subject: str) -> None:
    subscription = await connection.subscribe(subject)
    await subscription.unsubscribe()


async def publish_management(connection, subject: str) -> None:
    await connection.publish(subject, b"{}")


def validate_fixture_message(payload: dict[str, object], *, fixture_name: str) -> None:
    """Reject fixture drift before a valid envelope reaches JetStream."""
    try:
        validate_message(payload)
    except TransportValidationError as exc:
        reason = str(exc)
        bounded_reason = reason[:96] if reason else "unknown"
        raise AssertionError(
            f"fixture contract invalid before publish ({fixture_name}): {bounded_reason}"
        ) from None


async def publish(
    connection,
    subject: str,
    payload: dict[str, object],
    *,
    broker_message_id=None,
    fixture_name="valid envelope",
) -> None:
    validate_fixture_message(payload, fixture_name=fixture_name)
    await connection.jetstream().publish(
        subject,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        headers={"Nats-Msg-Id": str(broker_message_id or uuid4())},
    )


async def publish_unvalidated(connection, subject: str, payload: dict[str, object]) -> None:
    """Publish only the intentionally malformed poison probe."""
    await connection.jetstream().publish(
        subject,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        headers={"Nats-Msg-Id": str(uuid4())},
    )


def envelope(*, message_type: str, attempt_id, request_id, job_id, idem, deadline, data, request_digest=None):
    digest = request_digest or hashlib.sha256(b"runner-e2e-fixture").hexdigest()
    return {
        "schemaVersion": 1,
        "messageId": str(uuid4()),
        "messageType": message_type,
        "occurredAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "correlationId": str(request_id),
        "causationId": str(request_id),
        "requestId": str(request_id),
        "jobId": str(job_id),
        "attemptId": str(attempt_id),
        "idempotencyKey": str(idem),
        "requestDigest": digest,
        "data": data,
    }


def explicit_content_capture_authorization(*, purpose="controlled_replay"):
    return {
        "captureClassification": "full_content",
        "authorizationPurpose": purpose,
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": str(uuid4()),
        "authorizedAt": "2026-01-01T00:00:00.000Z",
    }


def request_payload(
    *,
    attempt_id,
    request_id,
    job_id,
    idem,
    deadline,
    component_id="aura.evaluation.synthetic",
    execution_kind="synthetic_end_to_end",
    input_references=None,
    capture_authorization=None,
):
    # The transport contract binds the request digest to the canonicalized
    # request data projection. Use the public transport canonicalizer so the
    # acceptance probe exercises the same wire invariant as production callers.
    fixture_digest = hashlib.sha256(b"runner-e2e-fixture").hexdigest()
    input_references = list(input_references or [])
    declared_input_bytes = 0
    for reference in input_references:
        if not isinstance(reference, dict) or type(reference.get("byteCount")) is not int:
            raise ValueError("fixture artifact byte count is invalid")
        declared_input_bytes += reference["byteCount"]
        if declared_input_bytes > 1_048_576:
            raise ValueError("fixture artifact byte count exceeds the bounded ceiling")
    if input_references:
        capture_authorization = dict(
            capture_authorization or explicit_content_capture_authorization()
        )
    else:
        capture_authorization = {
            "captureClassification": "metadata_only",
            "authorizationPurpose": "controlled_replay",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
        }
    declared_model_calls = 1 if execution_kind == "synthetic_end_to_end" else 0
    fixture_revisions = {
        "synthetic_end_to_end": "synthetic-run-coordinator-v1",
        "module_replay": "document-library-eval-fixtures-v1",
    }
    fixture_revision = fixture_revisions[execution_kind]
    declared_artifact_operations = len(input_references)
    if execution_kind == "module_replay" and input_references:
        # The deployed document replay reads its input and writes one
        # runner-owned output reference through the artifact adapter.
        declared_artifact_operations += 1
    target = {
        "componentId": component_id,
        "componentVersion": "1.0.0",
        "codeRevision": "0000000",
        "imageDigest": fixture_digest,
        "configurationRevision": "local",
    }
    data = {
        "executionKind": execution_kind,
        "requestVersion": "1.0.0",
        "targetComponent": target,
        "captureAuthorization": capture_authorization,
        "provenance": {
            "dataset": {"id": str(uuid4()), "version": "1.0.0"},
            "suite": {"id": str(uuid4()), "version": "1.0.0"},
            "evaluators": [{"evaluatorId": str(uuid4()), "evaluatorVersion": "1.0.0", "kind": "deterministic"}],
            "components": [target],
            "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": fixture_digest},
            "deploymentRevision": telemetry_deployment_revision(),
            "reproducibility": {
                "seed": 7,
                "clockMode": "fixed",
                "timeZone": "UTC",
                "locale": "en-US",
                "fixtureRevision": fixture_revision,
                "promptRevision": "prompt-v1",
                "policyRevision": "policy-v1",
            },
        },
        "limits": {
            "execution": {
                "timeoutMs": 10000,
                "maxConcurrency": 1,
                "maxAttempts": 1,
                "maxCases": 1,
                "maxInputBytes": declared_input_bytes,
                "maxOutputBytes": 1024,
                "maxTokens": 0,
            },
            "maxQueueTimeMs": 10000,
            "maxModelCalls": declared_model_calls,
            "maxArtifactOperations": declared_artifact_operations,
        },
        "deadlineAt": deadline,
        "caseIds": [str(uuid4())],
        "inputArtifactReferences": input_references,
    }
    message = envelope(
        message_type="evaluation.runner.requested",
        attempt_id=attempt_id,
        request_id=request_id,
        job_id=job_id,
        idem=idem,
        deadline=deadline,
        data=data,
        request_digest="0" * 64,
    )
    message["requestDigest"] = canonical_digest(message)
    return message


def artifact_reference(
    *, reference_id, content, output=False, digest=None, capture_authorization=None
):
    return {
        "artifactReferenceId": str(reference_id),
        "mediaClassification": "evaluation_output" if output else "evaluation_input",
        "mediaType": "application/octet-stream",
        "byteCount": len(content),
        "integrity": {
            "algorithm": "sha256",
            "digest": digest or hashlib.sha256(content).hexdigest(),
        },
        "captureAuthorization": dict(
            capture_authorization or explicit_content_capture_authorization(
                purpose="owner_supplied_evaluation"
            )
        ),
    }


def runner_object_key(reference):
    """Derive the runner-owned key without exposing its locator."""
    return f"runner/{UUID(str(reference['artifactReferenceId'])).hex}"


async def read_runner_object(store, reference):
    """Read a runner output through the provider-owned, deterministic key."""
    return b"".join([chunk async for chunk in store.get(runner_object_key(reference))])


async def assert_runner_object_deleted(store, reference, failure_message):
    """Treat provider not-found as deletion and reject any returned object."""
    returned_bytes = False
    try:
        async for _chunk in store.get(runner_object_key(reference)):
            returned_bytes = True
    except Exception:
        if not returned_bytes:
            return
        raise
    raise RuntimeError(failure_message)


async def wait_for_cancellation_tombstone(engine, *, attempt_id, cancellation_id, reason):
    """Wait only for the runner-owned durable cancellation receipt."""
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + 10
    while True:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT cancellation_id, reason "
                        "FROM evaluation_runner_cancellations "
                        "WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
        if row is not None:
            if str(row["cancellation_id"]) != str(cancellation_id) or row["reason"] != reason:
                raise RuntimeError("cancellation tombstone identity failed validation")
            return
        if loop.time() >= stop_at:
            raise RuntimeError("cancellation tombstone was not durably recorded")
        await asyncio.sleep(0.1)


async def assert_runner_database_ready() -> None:
    """Prove the runner credential can reach its service-owned ledger."""
    try:
        database_password = Path(
            "/run/secrets/aura-evaluation-runner-database-password"
        ).read_text(encoding="utf-8").strip()
        database_url = (
            "postgresql+psycopg://aura_evaluation_runner:"
            f"{quote(database_password, safe='')}@aura-core-postgres:5432/aura"
        )
        engine = create_async_engine(
            database_url, connect_args={"connect_timeout": 2}
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                await connection.execute(
                    text("SELECT attempt_id FROM evaluation_runner_attempts LIMIT 1")
                )
        finally:
            await engine.dispose()
    except Exception:
        raise RuntimeError("runner ledger preflight unavailable") from None


def _bounded_count(value, *, nested_key=None):
    if nested_key is not None and isinstance(value, dict):
        value = value.get(nested_key)
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else "na"


async def terminal_fetch_diagnostic(
    *, attempt_id, reason, result_length=0, result_header="none"
) -> None:
    """Emit bounded transport/state classes without payloads or identifiers."""

    def safe_secret(name):
        return Path(os.environ[name]).read_text(encoding="utf-8").strip()

    async def info(connection, subject):
        try:
            response = await connection.request(subject, b"{}", timeout=2)
            value = json.loads(response.data)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    stream_messages = "na"
    runner_delivered = runner_redelivered = runner_ack_pending = runner_pending = "na"
    dispatch_delivered = dispatch_redelivered = dispatch_ack_pending = dispatch_pending = "na"
    bootstrap_password = None
    try:
        bootstrap_password = safe_secret("E2E_BOOTSTRAP_PASSWORD_FILE")
    except (KeyError, OSError, UnicodeError):
        pass
    bootstrap_connection = None
    try:
        if bootstrap_password:
            bootstrap_connection = await nats.connect(
                f"nats://aura-evaluation-bootstrap:{bootstrap_password}@aura-nats:4222",
                connect_timeout=2,
            )
            stream = await info(bootstrap_connection, "$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER")
            stream_state = stream.get("state", {}) if isinstance(stream.get("state", {}), dict) else {}
            stream_messages = _bounded_count(stream_state.get("messages"))
            runner = await info(
                bootstrap_connection,
                "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner",
            )
            dispatch_info = await info(
                bootstrap_connection,
                "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch",
            )
            runner_delivered = _bounded_count(runner.get("delivered"), nested_key="consumer_seq")
            runner_redelivered = _bounded_count(runner.get("num_redelivered"))
            runner_ack_pending = _bounded_count(runner.get("num_ack_pending"))
            runner_pending = _bounded_count(runner.get("num_pending"))
            dispatch_delivered = _bounded_count(dispatch_info.get("delivered"), nested_key="consumer_seq")
            dispatch_redelivered = _bounded_count(dispatch_info.get("num_redelivered"))
            dispatch_ack_pending = _bounded_count(dispatch_info.get("num_ack_pending"))
            dispatch_pending = _bounded_count(dispatch_info.get("num_pending"))
    except Exception:
        stream_messages = "unavailable"
        runner_delivered = runner_redelivered = runner_ack_pending = runner_pending = "unavailable"
        dispatch_delivered = dispatch_redelivered = dispatch_ack_pending = dispatch_pending = "unavailable"
    finally:
        if bootstrap_connection is not None:
            await bootstrap_connection.close()

    ledger_query = "unavailable"
    ledger_status = ledger_publication = ledger_terminal = "absent"
    engine = None
    try:
        database_password = Path("/run/secrets/aura-evaluation-runner-database-password").read_text(encoding="utf-8").strip()
        database_url = f"postgresql+psycopg://aura_evaluation_runner:{quote(database_password, safe='')}@aura-core-postgres:5432/aura"
        engine = create_async_engine(database_url, connect_args={"connect_timeout": 2})
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, publication_status, terminal_payload IS NOT NULL AS terminal_payload_present "
                        "FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"
                    ),
                    {"attempt_id": attempt_id},
                )
            ).mappings().first()
            ledger_query = "ok"
            if row is not None:
                ledger_status = row["status"] if row["status"] in {"queued", "leased", "completed", "cancelled", "failed"} else "other"
                ledger_publication = row["publication_status"] if row["publication_status"] in {"pending", "published"} else "other"
                ledger_terminal = "present" if row["terminal_payload_present"] is True else "absent"
    except Exception:
        ledger_query = "unavailable"
        ledger_status = ledger_publication = ledger_terminal = "unavailable"
    finally:
        if engine is not None:
            await engine.dispose()

    try:
        Path("/run/e2e-probe/terminal-fetch-diagnostic").write_text("1\n", encoding="utf-8")
    except OSError:
        pass
    print(
        "AURA_E2E terminal_fetch_diagnostic "
        f"reason={reason} stream_messages={stream_messages} "
        f"runner_num_delivered={runner_delivered} runner_num_redelivered={runner_redelivered} "
        f"runner_num_ack_pending={runner_ack_pending} runner_num_pending={runner_pending} "
        f"dispatch_num_delivered={dispatch_delivered} dispatch_num_redelivered={dispatch_redelivered} "
        f"dispatch_num_ack_pending={dispatch_ack_pending} dispatch_num_pending={dispatch_pending} "
        f"ledger_query_status={ledger_query} "
        f"ledger_status={ledger_status} ledger_publication_status={ledger_publication} "
        f"ledger_terminal_payload_present={ledger_terminal} result_length={result_length} "
        f"result_header={result_header}",
        file=sys.stderr,
    )


async def fetch_terminal(subscription, *, attempt_id):
    try:
        messages = await subscription.fetch(1, timeout=30)
    except NatsTimeoutError:
        try:
            await terminal_fetch_diagnostic(attempt_id=attempt_id, reason="timeout")
        except Exception:
            print("AURA_E2E terminal_fetch_diagnostic unavailable", file=sys.stderr)
        raise RuntimeError("terminal result fetch timed out") from None
    if len(messages) != 1:
        try:
            await terminal_fetch_diagnostic(attempt_id=attempt_id, reason="unexpected")
        except Exception:
            print("AURA_E2E terminal_fetch_diagnostic unavailable", file=sys.stderr)
        raise RuntimeError("terminal result fetch returned an unexpected count")
    result = messages[0]
    raw = bytes(result.data)
    try:
        terminal = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        stripped = raw.lstrip()
        header = "empty" if not raw else "json_object" if stripped.startswith(b"{") else "non_json"
        try:
            await terminal_fetch_diagnostic(
                attempt_id=attempt_id,
                reason="json",
                result_length=_bounded_count(len(raw)),
                result_header=header,
            )
        except Exception:
            print("AURA_E2E terminal_fetch_diagnostic unavailable", file=sys.stderr)
        raise RuntimeError("terminal result was not valid JSON") from None
    return result, terminal


def terminal_mismatch_diagnostic(terminal, *, reason) -> None:
    """Report only finite terminal shape fields before a contract mismatch."""
    terminal_types = {
        "evaluation.runner.completed",
        "evaluation.runner.cancelled",
        "evaluation.runner.failed",
    }
    failure_codes = {
        "REQUEST_INVALID",
        "DEADLINE_EXCEEDED",
        "RESOURCE_LIMIT_EXCEEDED",
        "DEPENDENCY_UNAVAILABLE",
        "EXECUTION_FAILED",
        "RESULT_VALIDATION_FAILED",
        "STORAGE_FAILED",
        "INTERNAL_FAILURE",
    }
    reason_codes = {"OWNER_REQUESTED", "DEADLINE_EXCEEDED", "SUPERSEDED", "SHUTDOWN"}
    data = terminal.get("data") if isinstance(terminal, dict) else None
    data = data if isinstance(data, dict) else {}
    message_type = terminal.get("messageType") if isinstance(terminal, dict) else None
    failure_code = data.get("failureCode")
    reason_code = data.get("reasonCode")
    print(
        "AURA_E2E terminal_mismatch "
        f"reason={reason} "
        f"message_type={message_type if isinstance(message_type, str) and message_type in terminal_types else 'other'} "
        f"failure_code={failure_code if isinstance(failure_code, str) and failure_code in failure_codes else 'other'} "
        f"reason_code={reason_code if isinstance(reason_code, str) and reason_code in reason_codes else 'other'}",
        file=sys.stderr,
    )


def terminal_data(terminal):
    data = terminal.get("data") if isinstance(terminal, dict) else None
    return data if isinstance(data, dict) else {}


def terminal_matches(terminal, *, attempt_id, message_type, reason_code=None) -> bool:
    if not isinstance(terminal, dict):
        return False
    if terminal.get("attemptId") != str(attempt_id) or terminal.get("messageType") != message_type:
        return False
    return reason_code is None or terminal_data(terminal).get("reasonCode") == reason_code


async def main() -> None:
    normal = ("aura", secret("E2E_NORMAL_PASSWORD_FILE"))
    bootstrap = ("aura-evaluation-bootstrap", secret("E2E_BOOTSTRAP_PASSWORD_FILE"))
    dispatch = ("aura-evaluation-dispatch", secret("E2E_DISPATCH_PASSWORD_FILE"))
    runner = ("aura-evaluation-runner", secret("E2E_RUNNER_PASSWORD_FILE"))
    observed_payloads = []

    # The matrix covers non-retained allow operations and the management APIs
    # that must remain unavailable outside the one-shot admin. The retained
    # dispatch request/cancellation and runner result allow paths are proven
    # below by real contract envelopes so the stream starts clean.
    await allowed(*normal, lambda nc: publish_plain(nc, "aura.runs.execute.v1"))
    await allowed(*normal, lambda nc: subscribe_plain(nc, "aura.runs.execute.v1"))
    await allowed(*bootstrap, lambda nc: nc.request("$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", b"{}"))
    await allowed(*dispatch, lambda nc: subscribe_plain(nc, "aura.evaluation.runner.results.v1"))
    await allowed(*dispatch, lambda nc: publish_management(nc, "$JS.ACK.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch.probe"))
    await allowed(*runner, lambda nc: subscribe_plain(nc, "aura.evaluation.runner.requested.v1"))
    await allowed(*runner, lambda nc: subscribe_plain(nc, "aura.evaluation.runner.cancellation.v1"))
    await allowed(*runner, lambda nc: publish_management(nc, "$JS.ACK.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner.probe"))
    await allowed(*runner, lambda nc: publish_management(nc, "$JS.ACK.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation.probe"))

    await denied(*normal, lambda nc: nc.request("$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", b"{}"))
    await denied(*normal, lambda nc: nc.subscribe("aura.evaluation.runner.requested.v1"))
    await denied(*bootstrap, lambda nc: nc.request("$JS.API.STREAM.INFO.AURA_RUNS", b"{}"))
    await denied(*bootstrap, lambda nc: nc.request("$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-worker", b"{}"))
    await denied(*dispatch, lambda nc: nc.request("$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", b"{}"))
    await denied(*dispatch, lambda nc: nc.request("$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner", b"{}"))
    await denied(*dispatch, lambda nc: nc.subscribe("aura.evaluation.runner.requested.v1"))
    await denied(*dispatch, lambda nc: nc.subscribe("aura.evaluation.runner.cancellation.v1"))
    await denied(*runner, lambda nc: nc.publish("aura.runs.execute.v1", b"probe"))
    await denied(*runner, lambda nc: nc.subscribe("aura.runs.execute.v1"))
    await denied(*runner, lambda nc: nc.subscribe("aura.evaluation.runner.results.v1"))
    await denied(*runner, lambda nc: nc.request("$JS.API.STREAM.INFO.AURA_RUNS", b"{}"))

    admin_connection = await nats.connect(f"nats://{bootstrap[0]}:{bootstrap[1]}@aura-nats:4222")
    try:
        response = await admin_connection.request("$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", b"{}")
        if b"AURA_EVALUATION_RUNNER" not in response.data:
            raise RuntimeError("JetStream admin probe failed")
    finally:
        await admin_connection.close()

    dispatch_connection = await nats.connect(f"nats://{dispatch[0]}:{dispatch[1]}@aura-nats:4222")
    result_subscription = await dispatch_connection.jetstream().pull_subscribe(
        "aura.evaluation.runner.results.v1",
        durable="aura-evaluation-dispatch",
        stream="AURA_EVALUATION_RUNNER",
    )
    try:
        await assert_runner_database_ready()
        # Seed one opaque input through the exact runner bucket and derived
        # locator.  The subsequent module replay reads it only through the
        # runner adapter; no object key crosses NATS or PostgreSQL.
        eval_store = GarageS3ObjectStore(
            "http://aura-garage:3900",
            "aura-observatory-evaluation",
            access_key=secret("E2E_RUNNER_ACCESS_KEY_FILE"),
            secret_key=secret("E2E_RUNNER_SECRET_KEY_FILE"),
        )
        input_id = UUID("00000000-0000-4000-8000-000000009101")
        input_content = b"runner-e2e-fixture"
        replay_capture = explicit_content_capture_authorization(purpose="controlled_replay")
        input_reference = artifact_reference(
            reference_id=input_id,
            content=input_content,
            capture_authorization=replay_capture,
        )
        async def input_stream():
            yield input_content
        await eval_store.put_stream(f"runner/{input_id.hex}", input_stream(), content_type="application/octet-stream")

        attempt_id = uuid4()
        request_id = uuid4()
        job_id = uuid4()
        idem = uuid4()
        deadline = (datetime.now(UTC) + timedelta(seconds=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        request = request_payload(attempt_id=attempt_id, request_id=request_id, job_id=job_id, idem=idem, deadline=deadline)
        request_data = request["data"]
        if not isinstance(request_data, dict) or request_data["limits"]["execution"]["maxInputBytes"] != 0:
            raise RuntimeError("metadata-only synthetic input limit was widened")
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.requested.v1",
            request,
            broker_message_id=request["messageId"],
            fixture_name="synthetic request",
        )
        result, terminal = await fetch_terminal(result_subscription, attempt_id=attempt_id)
        observed_payloads.append(json.dumps(terminal, sort_keys=True))
        if not terminal_matches(
            terminal, attempt_id=attempt_id, message_type="evaluation.runner.completed"
        ):
            terminal_mismatch_diagnostic(terminal, reason="synthetic_terminal")
            raise RuntimeError("synthetic terminal result failed validation")
        runtime_value = terminal_data(terminal).get("runtimeEvidence", {})
        runtime = runtime_value if isinstance(runtime_value, dict) else {}
        if not all(runtime.get(key, {}).get("status") == "verified" for key in ("componentVersion", "fixtureRevision", "policyRevision")):
            terminal_mismatch_diagnostic(terminal, reason="synthetic_runtime_evidence")
            raise RuntimeError("fixture-backed synthetic replay evidence failed validation")
        await result.ack()
        # A logically identical request with a distinct broker message ID is
        # accepted by JetStream but must not create a second side effect.
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.requested.v1",
            request,
            broker_message_id=uuid4(),
            fixture_name="synthetic duplicate request",
        )
        try:
            duplicate = await result_subscription.fetch(1, timeout=2)
        except NatsTimeoutError:
            duplicate = []
        if duplicate:
            raise RuntimeError("logical duplicate produced more than one terminal result")

        # The live registry path is a module replay, not the synthetic
        # scenario.  It proves exact deployed component evidence and reads an
        # explicitly authorized input through the runner-specific adapter.
        replay_attempt = uuid4()
        replay_request_id = uuid4()
        replay_job_id = uuid4()
        replay_idem = uuid4()
        replay_deadline = (datetime.now(UTC) + timedelta(seconds=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        replay_request = request_payload(
            attempt_id=replay_attempt,
            request_id=replay_request_id,
            job_id=replay_job_id,
            idem=replay_idem,
            deadline=replay_deadline,
            component_id="aura.knowledge.document_processing",
            execution_kind="module_replay",
            input_references=[input_reference],
            capture_authorization=replay_capture,
        )
        replay_data = replay_request["data"]
        replay_limits = replay_data["limits"]["execution"] if isinstance(replay_data, dict) else {}
        if not isinstance(replay_limits, dict) or replay_limits.get("maxInputBytes") != len(input_content):
            raise RuntimeError("module replay input limit does not match declared artifact bytes")
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.requested.v1",
            replay_request,
            fixture_name="module replay request",
        )
        replay_result, replay_payload = await fetch_terminal(result_subscription, attempt_id=replay_attempt)
        observed_payloads.append(json.dumps(replay_payload, sort_keys=True))
        if not terminal_matches(
            replay_payload, attempt_id=replay_attempt, message_type="evaluation.runner.completed"
        ):
            terminal_mismatch_diagnostic(replay_payload, reason="module_replay_terminal")
            raise RuntimeError("production module replay did not complete")
        replay_data = terminal_data(replay_payload)
        runtime_value = replay_data.get("runtimeEvidence", {})
        runtime = runtime_value if isinstance(runtime_value, dict) else {}
        if not all(runtime.get(key, {}).get("status") == "verified" for key in ("componentVersion", "fixtureRevision", "policyRevision")):
            terminal_mismatch_diagnostic(replay_payload, reason="module_replay_runtime_evidence")
            raise RuntimeError("production replay runtime evidence failed validation")
        output_references = replay_data.get("outputArtifactReferences")
        if not isinstance(output_references, list):
            terminal_mismatch_diagnostic(replay_payload, reason="module_replay_output_references")
            raise RuntimeError("terminal output references are not bounded")
        if not output_references:
            terminal_mismatch_diagnostic(replay_payload, reason="module_replay_output_missing")
            raise RuntimeError("production replay did not persist its authorized output artifact")
        for reference in output_references:
            if not isinstance(reference, dict) or "locator" in reference or "filename" in reference:
                terminal_mismatch_diagnostic(replay_payload, reason="module_replay_output_reference_shape")
                raise RuntimeError("terminal output exposed a locator")
        # The production replay must return a content-authorized output that
        # is readable through the runner-owned provider, then be reachable
        # for explicit deletion. The terminal carries references only.
        replay_adapter = EvaluationArtifactAdapter(eval_store, max_bytes=1024)
        for output_reference in output_references:
            output_bytes = await read_runner_object(eval_store, output_reference)
            integrity = output_reference.get("integrity") if isinstance(output_reference, dict) else None
            if not isinstance(integrity, dict) or hashlib.sha256(output_bytes).hexdigest() != integrity.get("digest"):
                raise RuntimeError("production replay output artifact integrity failed")
            await replay_adapter.delete(output_reference)
            await assert_runner_object_deleted(
                eval_store,
                output_reference,
                "production replay output artifact deletion was not reachable",
            )
        await replay_result.ack()

        cancel_attempt = uuid4()
        cancel_request_id = uuid4()
        cancel_job_id = uuid4()
        cancel_idem = uuid4()
        cancel_deadline = (datetime.now(UTC) + timedelta(seconds=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cancellation_id = uuid4()
        cancel_request = request_payload(
            attempt_id=cancel_attempt,
            request_id=cancel_request_id,
            job_id=cancel_job_id,
            idem=cancel_idem,
            deadline=cancel_deadline,
        )
        cancellation = envelope(
            message_type="evaluation.runner.cancellation_requested",
            attempt_id=cancel_attempt,
            request_id=cancel_request_id,
            job_id=cancel_job_id,
            idem=cancel_idem,
            deadline=cancel_deadline,
            data={"cancellationId": str(cancellation_id), "reasonCode": "OWNER_REQUESTED", "deadlineAt": cancel_deadline},
            request_digest=cancel_request["requestDigest"],
        )
        # Publish cancellation first and wait for the service-owned durable
        # tombstone. This makes the queued-before-run path deterministic while
        # preserving the strict terminal result assertion below.
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.cancellation.v1",
            cancellation,
            fixture_name="owner cancellation",
        )
        database_password = Path("/run/secrets/aura-evaluation-runner-database-password").read_text(encoding="utf-8").strip()
        database_url = f"postgresql+psycopg://aura_evaluation_runner:{quote(database_password, safe='')}@aura-core-postgres:5432/aura"
        cancellation_engine = create_async_engine(database_url)
        try:
            await wait_for_cancellation_tombstone(
                cancellation_engine,
                attempt_id=cancel_attempt,
                cancellation_id=cancellation_id,
                reason="OWNER_REQUESTED",
            )
        finally:
            await cancellation_engine.dispose()
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.requested.v1",
            cancel_request,
            fixture_name="cancelled request",
        )
        cancelled, cancelled_payload = await fetch_terminal(result_subscription, attempt_id=cancel_attempt)
        observed_payloads.append(json.dumps(cancelled_payload, sort_keys=True))
        if not terminal_matches(
            cancelled_payload,
            attempt_id=cancel_attempt,
            message_type="evaluation.runner.cancelled",
            reason_code="OWNER_REQUESTED",
        ):
            terminal_mismatch_diagnostic(cancelled_payload, reason="cancellation_terminal")
            raise RuntimeError("cancellation terminal result failed validation")
        await cancelled.ack()

        expired_attempt = uuid4()
        expired_request_id = uuid4()
        expired_job_id = uuid4()
        expired_idem = uuid4()
        expired = request_payload(
            attempt_id=expired_attempt,
            request_id=expired_request_id,
            job_id=expired_job_id,
            idem=expired_idem,
            deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        await publish(
            dispatch_connection,
            "aura.evaluation.runner.requested.v1",
            expired,
            fixture_name="deadline request",
        )
        deadline_result, deadline_payload = await fetch_terminal(result_subscription, attempt_id=expired_attempt)
        observed_payloads.append(json.dumps(deadline_payload, sort_keys=True))
        if not terminal_matches(
            deadline_payload,
            attempt_id=expired_attempt,
            message_type="evaluation.runner.cancelled",
            reason_code="DEADLINE_EXCEEDED",
        ):
            terminal_mismatch_diagnostic(deadline_payload, reason="deadline_terminal")
            raise RuntimeError("deadline terminal result failed validation")
        await deadline_result.ack()

        # Keep denied writes to retained evaluation subjects after the first
        # contract flows. If an ACL probe is unexpectedly routed, it cannot
        # pollute the initial durable result consumer or obscure its counts.
        await denied(*normal, lambda nc: nc.publish("aura.evaluation.runner.requested.v1", b"probe"))
        await denied(*normal, lambda nc: nc.publish("aura.evaluation.runner.results.v1", b"probe"))
        await denied(*bootstrap, lambda nc: nc.publish("aura.evaluation.runner.results.v1", b"probe"))
        await denied(*dispatch, lambda nc: nc.publish("aura.evaluation.runner.results.v1", b"probe"))
        await denied(*runner, lambda nc: nc.publish("aura.evaluation.runner.requested.v1", b"probe"))
        await denied(*runner, lambda nc: nc.publish("aura.evaluation.runner.cancellation.v1", b"probe"))
        # Reply inboxes are subscribe-only. Request/reply still works through the
        # bounded JetStream API subjects above, while arbitrary inbox publication
        # is denied for every long-running identity.
        await denied(*normal, lambda nc: nc.publish("_INBOX.aura-e2e-normal", b"probe"))
        await denied(*dispatch, lambda nc: nc.publish("_INBOX.aura-e2e-dispatch", b"probe"))
        await denied(*runner, lambda nc: nc.publish("_INBOX.aura-e2e-runner", b"probe"))

        # Poison is acknowledged without a terminal result and cannot consume
        # retries indefinitely; this exercises max-deliver/poison handling.
        await publish_unvalidated(dispatch_connection, "aura.evaluation.runner.requested.v1", {"poison": True})
        try:
            poison_result = await result_subscription.fetch(1, timeout=2)
        except NatsTimeoutError:
            poison_result = []
        if poison_result:
            raise RuntimeError("poison message produced a terminal result")
    finally:
        await dispatch_connection.close()

    database_password = Path("/run/secrets/aura-evaluation-runner-database-password").read_text(encoding="utf-8").strip()
    database_url = f"postgresql+psycopg://aura_evaluation_runner:{quote(database_password, safe='')}@aura-core-postgres:5432/aura"
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM evaluation_runner_attempts LIMIT 1"))
            rows = (await connection.execute(text("SELECT request_projection::text, terminal_payload::text FROM evaluation_runner_attempts"))).all()
            for row in rows:
                combined = " ".join(str(part or "") for part in row)
                if any(term in combined for term in ("toolArguments", "toolResults", "bearerToken", "rawContent", "private-e2e-content-marker")):
                    raise RuntimeError("runner ledger contains prohibited content fields")
            outbox_rows = (await connection.execute(text("SELECT payload::text FROM evaluation_runner_terminal_outbox"))).all()
            if any(any(term in str(row[0] or "") for term in ("toolArguments", "toolResults", "bearerToken", "rawContent")) for row in outbox_rows):
                raise RuntimeError("runner outbox contains prohibited content fields")
            try:
                await connection.execute(text("SELECT 1 FROM conversations LIMIT 1"))
            except Exception:
                pass
            else:
                raise RuntimeError("runner PostgreSQL role reached an owner table")

        # Run the durable SQL state machine against the migrated project
        # database.  These assertions intentionally use the production
        # adapter and compare-and-set predicates rather than an in-memory
        # substitute.
        sessions = __import__("aura_core.platform.database.engine", fromlist=["session_factory"]).session_factory(engine)
        ledger = SqlEvaluationRunnerLedger(sessions)
        sql_message = request
        sql_data = sql_message["data"]
        assert isinstance(sql_data, dict)
        sql_attempt_id = uuid4()
        sql_idem = uuid4()
        sql_attempt = AttemptRecord(
            sql_attempt_id,
            UUID(sql_message["requestId"]),
            UUID(sql_message["jobId"]),
            sql_idem,
            str(sql_message["requestDigest"]),
            request_projection=sql_message,
        )
        await ledger.receive(sql_attempt)
        assert (await ledger.receive(sql_attempt)).attempt_id == sql_attempt_id
        try:
            await ledger.receive(AttemptRecord(
                sql_attempt_id,
                sql_attempt.request_id,
                sql_attempt.job_id,
                sql_idem,
                "f" * 64,
                request_projection=sql_message,
            ))
        except AttemptConflict:
            pass
        else:
            raise RuntimeError("SQL idempotency conflict was accepted")

        race_id = uuid4()
        race_attempt = AttemptRecord(race_id, sql_attempt.request_id, sql_attempt.job_id, uuid4(), "1" * 64, request_projection=sql_message)
        await ledger.receive(race_attempt)
        race_claims = await asyncio.gather(
            ledger.claim(race_id, "sql-owner-a", timedelta(seconds=30)),
            ledger.claim(race_id, "sql-owner-b", timedelta(seconds=30)),
        )
        race_winners = [claim for claim in race_claims if claim is not None]
        if len(race_winners) != 1:
            raise RuntimeError("SQL concurrent claim was not fenced")
        winner = race_winners[0]
        assert winner.lease_generation is not None
        try:
            await ledger.terminal(
                race_id,
                {"status": "completed", "result_id": uuid4()},
                lease_owner="stale-owner",
                lease_generation=winner.lease_generation,
            )
        except AttemptConflict:
            pass
        else:
            raise RuntimeError("SQL stale owner settled a terminal state")
        race_projection = race_attempt.request_projection
        if not isinstance(race_projection, dict):
            raise RuntimeError("SQL race projection is not available")
        race_data = race_projection.get("data")
        if not isinstance(race_data, dict):
            raise RuntimeError("SQL race projection data is not available")
        race_deadline = datetime.fromisoformat(
            str(race_data["deadlineAt"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        cancelled = await ledger.cancel(
            race_id,
            uuid4(),
            "OWNER_REQUESTED",
            request_id=race_attempt.request_id,
            job_id=race_attempt.job_id,
            idempotency_key=race_attempt.idempotency_key,
            request_digest=race_attempt.request_digest,
            deadline_at=race_deadline,
        )
        if cancelled is None or cancelled.status != "cancelled":
            raise RuntimeError("SQL cancellation did not settle the current lease")

        recovery_id = uuid4()
        recovery_attempt = AttemptRecord(recovery_id, sql_attempt.request_id, sql_attempt.job_id, uuid4(), "2" * 64, request_projection=sql_message)
        await ledger.receive(recovery_attempt)
        old = await ledger.claim(recovery_id, "expired-owner", timedelta(seconds=30))
        if old is None or old.lease_generation is None:
            raise RuntimeError("SQL lease claim failed")
        async with sessions() as session, session.begin():
            await session.execute(text("UPDATE evaluation_runner_attempts SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE attempt_id = :attempt_id"), {"attempt_id": recovery_id})
        recovered = await ledger.claim(recovery_id, "recovered-owner", timedelta(seconds=30))
        if recovered is None or recovered.lease_generation == old.lease_generation:
            raise RuntimeError("SQL expired lease was not recovered with a new generation")
        try:
            await ledger.terminal(recovery_id, {"status": "completed", "result_id": uuid4()}, lease_owner="expired-owner", lease_generation=old.lease_generation)
        except AttemptConflict:
            pass
        else:
            raise RuntimeError("SQL stale lease generation settled a terminal state")

        outbox_id = uuid4()
        outbox_attempt = AttemptRecord(outbox_id, sql_attempt.request_id, sql_attempt.job_id, uuid4(), "3" * 64, request_projection=sql_message)
        await ledger.receive(outbox_attempt)
        outbox_claim = await ledger.claim(outbox_id, "outbox-owner", timedelta(seconds=30))
        if outbox_claim is None:
            raise RuntimeError("SQL outbox claim failed")
        terminal_id = uuid4()
        await ledger.terminal(
            outbox_id,
            {
                "status": "completed",
                "result_id": terminal_id,
                "result_digest": "4" * 64,
                "terminal_message_id": uuid4(),
                "terminal_payload": {"status": "completed", "resultId": str(terminal_id)},
            },
            lease_owner=outbox_claim.lease_owner,
            lease_generation=outbox_claim.lease_generation,
        )
        pending = await ledger.pending_publications()
        if not any(record.attempt_id == outbox_id for record in pending):
            raise RuntimeError("SQL terminal outbox recovery row is missing")
        await ledger.mark_published(outbox_id)
        published = await ledger.get(outbox_id)
        if published is None or published.publication_status != "published":
            raise RuntimeError("SQL outbox publication recovery failed")
    finally:
        await engine.dispose()

    store = GarageS3ObjectStore(
        "http://aura-garage:3900",
        os.environ["E2E_CORE_BUCKET"],
        access_key=secret("E2E_RUNNER_ACCESS_KEY_FILE"),
        secret_key=secret("E2E_RUNNER_SECRET_KEY_FILE"),
    )
    denied_core_bucket = False
    try:
        async for _ in store.get("runner-acl-probe"):
            pass
    except Exception:
        denied_core_bucket = True
    if not denied_core_bucket:
        raise RuntimeError("runner Garage key reached the Core bucket")

    eval_store = GarageS3ObjectStore(
        "http://aura-garage:3900",
        "aura-observatory-evaluation",
        access_key=secret("E2E_RUNNER_ACCESS_KEY_FILE"),
        secret_key=secret("E2E_RUNNER_SECRET_KEY_FILE"),
    )
    async def artifact_stream():
        yield b"runner-e2e-artifact"
    await eval_store.put_stream("runner-e2e-roundtrip", artifact_stream(), content_type="application/octet-stream")
    artifact = b"".join([chunk async for chunk in eval_store.get("runner-e2e-roundtrip")])
    if artifact != b"runner-e2e-artifact":
        raise RuntimeError("evaluation Garage artifact roundtrip failed")
    await eval_store.delete("runner-e2e-roundtrip")

    # Exercise the deployed adapter's authorization, digest, size, opaque
    # locator, write, read, and delete behavior against Garage.  The adapter
    # converts all provider failures to bounded content-free exceptions.
    adapter = EvaluationArtifactAdapter(eval_store, max_bytes=1024)
    assert await adapter.read(input_reference) == input_content
    bad_digest = dict(input_reference)
    bad_digest["integrity"] = {"algorithm": "sha256", "digest": "0" * 64}
    try:
        await adapter.read(bad_digest)
    except EvaluationArtifactError:
        pass
    else:
        raise RuntimeError("Garage digest rejection failed")
    oversized = dict(input_reference)
    oversized["byteCount"] = 2048
    try:
        await adapter.read(oversized)
    except EvaluationArtifactError:
        pass
    else:
        raise RuntimeError("Garage size rejection failed")
    output_content = b"runner-e2e-output"
    output_reference = artifact_reference(
        reference_id=UUID("00000000-0000-4000-8000-000000009102"),
        content=output_content,
        output=True,
    )
    await adapter.write(output_reference, output_content)
    output_bytes = b"".join([chunk async for chunk in eval_store.get(f"runner/{UUID(output_reference['artifactReferenceId']).hex}")])
    if output_bytes != output_content:
        raise RuntimeError("Garage output reference write failed")
    await adapter.delete(output_reference)
    await assert_runner_object_deleted(
        eval_store,
        output_reference,
        "Garage output artifact deletion was not reachable",
    )

    forbidden_markers = (
        '"prompt":', '"response":', '"memoryContent":', '"documentContent":',
        '"filename":', '"toolArguments":', '"toolResults":', '"bearerToken":',
        '"credentials":', '"endpoint":', "http://",
        "https://", "Traceback", "RuntimeError", "private-e2e-content-marker",
        "runner-e2e-fixture", "runner-e2e-output", "runner-e2e-core-database",
        "runner-e2e-runner-database", "runner-e2e-normal-nats", "runner-e2e-bootstrap-nats",
        "runner-e2e-dispatch-nats", "runner-e2e-runner-nats",
    )
    serialized_payloads = " ".join(observed_payloads)
    if any(marker in serialized_payloads for marker in forbidden_markers):
        raise RuntimeError("forbidden evaluation content or credential marker crossed result boundary")

    # Leave one bounded valid request for the shell-controlled redelivery
    # phase.  Its deadline is deliberately long enough for the one allowed
    # JetStream ack-wait interval and the scoped runner restart.
    redelivery = request_payload(
        attempt_id=uuid4(),
        request_id=uuid4(),
        job_id=uuid4(),
        idem=uuid4(),
        deadline=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    Path("/run/e2e-probe/redelivery.json").write_text(
        json.dumps(redelivery, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    outage = request_payload(
        attempt_id=uuid4(),
        request_id=uuid4(),
        job_id=uuid4(),
        idem=uuid4(),
        deadline=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    Path("/run/e2e-probe/outage.json").write_text(
        json.dumps(outage, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


asyncio.run(main())
PY
then
  probe_status=0
else
  probe_status=$?
fi

if [ "$probe_status" -ne 0 ]; then
  if [ -f "$probe_root/terminal-fetch-diagnostic" ]; then
    bounded_runner_logs=$("${compose[@]}" logs --no-color --tail=500 aura-evaluation-runner 2>/dev/null || true)
    metric_total=0
    metric_present=0
    metric_present_names=""
    poison_metric_present=false
    case "$bounded_runner_logs" in
      *evaluation_runner_nats_poison_count*) poison_metric_present=true ;;
    esac
    for metric in \
      evaluation_runner_queue_time_ms \
      evaluation_runner_fixture_load_duration_ms \
      evaluation_runner_execution_duration_ms \
      evaluation_runner_outcome_count \
      evaluation_runner_cancellation_count \
      evaluation_runner_resource_limit_count \
      evaluation_runner_reproducibility_completeness \
      evaluation_runner_queue_accepted \
      evaluation_runner_nats_fetch_failure_count \
      evaluation_runner_nats_publish_failure_count \
      evaluation_runner_nats_drain_failure_count \
      evaluation_runner_nats_recovery_count \
      evaluation_runner_nats_poison_count \
      evaluation_runner_terminal_settlement_failure_count \
      evaluation_runner_lease_renewal_failure_count \
      evaluation_runner_lease_lost_count \
      evaluation_runner_artifact_cleanup_failure_count \
      evaluation_runner_recovery_scan_failure_count \
      evaluation_runner_recovery_count \
      evaluation_runner_publication_recovery_failure_count \
      evaluation_runner_terminal_accounting_failure_count \
      evaluation_runner_nats_identity_conflict_count; do
      metric_total=$((metric_total + 1))
      case "$bounded_runner_logs" in
        *"$metric"*)
          metric_present=$((metric_present + 1))
          if [ -z "$metric_present_names" ]; then
            metric_present_names="$metric"
          else
            metric_present_names="$metric_present_names,$metric"
          fi
          ;;
      esac
    done
    printf 'AURA_E2E terminal_fetch_health_metrics present=%s total=%s names=%s\n' \
      "$metric_present" "$metric_total" "${metric_present_names:-none}" >&2
    printf 'AURA_E2E terminal_fetch_poison_metric_present=%s\n' \
      "$poison_metric_present" >&2
  fi
  exit "$probe_status"
fi

if [ "$telemetry_acceptance" = true ]; then
  # Poll the existing Observatory ClickHouse through its private API network.
  # The query proves that the runner signal crossed Collector and the
  # authenticated ingest boundary; only bounded counts are returned.
  telemetry_deadline=$((SECONDS + 20))
  telemetry_delivered=false
  while [ "$telemetry_delivered" = false ] && [ "$SECONDS" -lt "$telemetry_deadline" ]; do
    if "${compose[@]}" run --rm --no-deps -T \
      --entrypoint /app/.venv/bin/python observatory-api - <<'PY' >/dev/null
from base64 import b64encode
from pathlib import Path
from urllib.request import Request, urlopen

password = Path("/run/secrets/observatory-clickhouse-password").read_text(encoding="utf-8").strip()
query = (
    "SELECT count(), countIf(capture_policy = 'metadata_only') "
    "FROM observations WHERE deployment_id = 'runner-e2e-telemetry' FORMAT TabSeparated"
)
request = Request(
    "http://observatory-clickhouse:8123/?database=aura_observatory",
    data=query.encode("utf-8"),
    headers={
        "Content-Type": "text/plain",
        "Authorization": "Basic " + b64encode(f"observatory:{password}".encode()).decode(),
    },
    method="POST",
)
with urlopen(request, timeout=2) as response:
    values = response.read().decode("ascii").strip().split("\t")
if len(values) != 2 or int(values[0]) < 1 or int(values[0]) != int(values[1]):
    raise SystemExit(1)
PY
    then
      telemetry_delivered=true
    else
      sleep 1
    fi
  done
  if [ "$telemetry_delivered" != true ]; then
    echo 'runner OTLP telemetry was not delivered through Collector and Observatory ingest' >&2
    exit 1
  fi

  # Stop only the existing Collector and ingest API and prove the runner
  # remains alive and can complete another isolated request. Export failures
  # are asynchronous and must not block evaluation execution or readiness.
  "${compose[@]}" stop observatory-collector observatory-api >/dev/null
  runner_state=$(docker inspect -f '{{.State.Running}}' "$project-aura-evaluation-runner-1")
  if [ "$runner_state" != true ]; then
    echo 'runner stopped when Collector became unavailable' >&2
    exit 1
  fi
  if "${compose[@]}" run --rm --no-deps -T \
    -v "$secret_root:/run/e2e:ro" \
    -v "$probe_root:/run/e2e-probe:ro" \
    -e E2E_DISPATCH_PASSWORD_FILE=/run/e2e/aura-nats-dispatch-password \
    --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import json
import sys
from pathlib import Path
import nats
from nats.errors import TimeoutError as NatsTimeoutError

TERMINAL_TYPES = {
    "evaluation.runner.completed",
    "evaluation.runner.cancelled",
    "evaluation.runner.failed",
}
FAILURE_CODES = {
    "REQUEST_INVALID",
    "DEADLINE_EXCEEDED",
    "RESOURCE_LIMIT_EXCEEDED",
    "DEPENDENCY_UNAVAILABLE",
    "EXECUTION_FAILED",
    "RESULT_VALIDATION_FAILED",
    "STORAGE_FAILED",
    "INTERNAL_FAILURE",
}
REASON_CODES = {"OWNER_REQUESTED", "DEADLINE_EXCEEDED", "SUPERSEDED", "SHUTDOWN"}


def bounded_terminal_mismatch(terminal, *, reason) -> None:
    data = terminal.get("data") if isinstance(terminal, dict) else None
    data = data if isinstance(data, dict) else {}
    message_type = terminal.get("messageType") if isinstance(terminal, dict) else None
    failure_code = data.get("failureCode")
    reason_code = data.get("reasonCode")
    print(
        "AURA_E2E outage_terminal_mismatch "
        f"reason={reason} "
        f"message_type={message_type if isinstance(message_type, str) and message_type in TERMINAL_TYPES else 'other'} "
        f"failure_code={failure_code if isinstance(failure_code, str) and failure_code in FAILURE_CODES else 'other'} "
        f"reason_code={reason_code if isinstance(reason_code, str) and reason_code in REASON_CODES else 'other'}",
        file=sys.stderr,
    )

async def main() -> None:
    password = Path("/run/e2e/aura-nats-dispatch-password").read_text(encoding="utf-8").strip()
    payload = json.loads(Path("/run/e2e-probe/outage.json").read_text(encoding="utf-8"))
    connection = await nats.connect(f"nats://aura-evaluation-dispatch:{password}@aura-nats:4222")
    try:
        subscription = await connection.jetstream().pull_subscribe(
            "aura.evaluation.runner.results.v1",
            durable="aura-evaluation-dispatch",
            stream="AURA_EVALUATION_RUNNER",
        )
        await connection.jetstream().publish(
            "aura.evaluation.runner.requested.v1",
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        target_attempt_id = payload.get("attemptId")
        outage_fetch_deadline = asyncio.get_running_loop().time() + 30
        while True:
            remaining = outage_fetch_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                bounded_terminal_mismatch({}, reason="target_not_seen")
                raise RuntimeError("runner did not execute while Collector was unavailable")
            try:
                messages = await subscription.fetch(1, timeout=min(2, remaining))
            except NatsTimeoutError:
                continue
            if len(messages) != 1:
                bounded_terminal_mismatch({}, reason="unexpected_count")
                raise RuntimeError("runner outage-independence result count was invalid")
            message = messages[0]
            try:
                terminal = json.loads(message.data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                bounded_terminal_mismatch({}, reason="json")
                raise RuntimeError("runner outage-independence result was not JSON") from None
            if not isinstance(terminal, dict):
                bounded_terminal_mismatch({}, reason="shape")
                raise RuntimeError("runner outage-independence result shape was invalid")
            if terminal.get("attemptId") != target_attempt_id:
                if terminal.get("messageType") in TERMINAL_TYPES:
                    await message.ack()
                    continue
                bounded_terminal_mismatch(terminal, reason="unrelated_non_terminal")
                raise RuntimeError("runner outage-independence result was not a terminal message")
            if terminal.get("messageType") != "evaluation.runner.completed":
                await message.ack()
                bounded_terminal_mismatch(terminal, reason="target_terminal")
                raise RuntimeError("runner outage-independence target terminal was invalid")
            await message.ack()
            break
    finally:
        await connection.close()

asyncio.run(main())
PY
  then
    echo 'runner execution remained independent of Collector availability'
  else
    echo 'runner execution failed while Collector was unavailable' >&2
    exit 1
  fi
fi

# Post-run privacy scan: inspect only bounded service output for prohibited
# content-shaped fields. The output is never forwarded to the acceptance log.
# Keep URL rejection strict for the runner. NATS may print static upstream
# documentation URLs, so its policy rejects private endpoint hosts and all
# content/credential markers without rejecting every https:// occurrence.
runner_privacy_logs=$("${compose[@]}" logs --no-color --tail=500 aura-evaluation-runner 2>/dev/null || true)
nats_privacy_logs=$("${compose[@]}" logs --no-color --tail=500 aura-nats 2>/dev/null || true)
# Optional acceptance hook for deployments that enable the runner's bounded
# telemetry export. It checks only structured metric names and never forwards
# payloads. The default remains false because this disposable runner does not
# start the full Observatory Collector graph.
if [ "${AURA_E2E_ASSERT_RUNNER_TELEMETRY:-false}" = true ]; then
  case "$runner_privacy_logs" in
    *evaluation_runner_*) ;;
    *) echo 'runner structured telemetry assertion failed' >&2; exit 1 ;;
  esac
fi
runner_forbidden_markers=(
  '"prompt":' '"response":' '"memoryContent":' '"documentContent":'
  '"filename":' '"toolArguments":' '"toolResults":' '"bearerToken":'
  '"credentials":' '"endpoint":' http:// https:// Traceback RuntimeError
  private-e2e-content-marker runner-e2e-fixture runner-e2e-output
)
nats_forbidden_markers=(
  '"prompt":' '"response":' '"memoryContent":' '"documentContent":'
  '"filename":' '"toolArguments":' '"toolResults":' '"bearerToken":'
  '"credentials":' '"endpoint":' Traceback RuntimeError
  private-e2e-content-marker runner-e2e-fixture runner-e2e-output
  runner-e2e.invalid authentik.runner-e2e.invalid aura-nats: aura-core-postgres:
  aura-garage: observatory- http://aura- https://aura- http://observatory- https://observatory-
)
for forbidden in "${runner_forbidden_markers[@]}"; do
  case "$runner_privacy_logs" in
    *"$forbidden"*) echo "runner privacy scan failed: $forbidden" >&2; exit 1 ;;
  esac
done
for forbidden in "${nats_forbidden_markers[@]}"; do
  case "$nats_privacy_logs" in
    *"$forbidden"*) echo "NATS privacy scan failed: $forbidden" >&2; exit 1 ;;
  esac
done
for secret_file in "$secret_root"/*; do
  secret_value=$(cat "$secret_file")
  for service_logs in "$runner_privacy_logs" "$nats_privacy_logs"; do
    case "$service_logs" in
      *"$secret_value"*) echo 'privacy scan found a fixture credential in service output' >&2; exit 1 ;;
    esac
  done
done

# Durable redelivery/restart phase.  The runner is allowed to receive a valid
# request while its Core database is unavailable; the failed durable receipt
# remains unacknowledged.  After the one configured ack-wait interval, the
# broker must report a redelivery.  Restart then settles one terminal row and
# one terminal outbox publication.
"${compose[@]}" stop aura-core-postgres >/dev/null
"${compose[@]}" run --rm --no-deps -T \
  -v "$secret_root:/run/e2e:ro" \
  -v "$probe_root:/run/e2e-probe:rw" \
  -e E2E_DISPATCH_PASSWORD_FILE=/run/e2e/aura-nats-dispatch-password \
  --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import json
from pathlib import Path
import nats
from aura_core.platform.evaluation_transport import (
    TransportValidationError,
    validate_message,
)

async def main() -> None:
    password = Path("/run/e2e/aura-nats-dispatch-password").read_text(encoding="utf-8").strip()
    payload = json.loads(Path("/run/e2e-probe/redelivery.json").read_text(encoding="utf-8"))
    try:
        validate_message(payload)
    except TransportValidationError as exc:
        reason = str(exc)
        raise AssertionError(
            f"fixture contract invalid before redelivery publish: {reason[:96]}"
        ) from None
    connection = await nats.connect(f"nats://aura-evaluation-dispatch:{password}@aura-nats:4222")
    try:
        pub_ack = await connection.jetstream().publish(
            "aura.evaluation.runner.requested.v1",
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        if type(pub_ack.seq) is not int or pub_ack.seq < 1:
            raise RuntimeError("redelivery publish did not return a valid stream sequence")
        Path("/run/e2e-probe/redelivery-publish-state.json").write_text(
            json.dumps({"stream_seq": pub_ack.seq}, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        await connection.flush(timeout=2)
    finally:
        await connection.close()

asyncio.run(main())
PY
sleep 3
"${compose[@]}" run --rm --no-deps -T \
  -v "$secret_root:/run/e2e:ro" \
  -v "$probe_root:/run/e2e-probe:rw" \
  -e E2E_BOOTSTRAP_PASSWORD_FILE=/run/e2e/aura-nats-bootstrap-password \
  --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import json
from pathlib import Path
import nats

async def main() -> None:
    publication = json.loads(
        Path("/run/e2e-probe/redelivery-publish-state.json").read_text(encoding="utf-8")
    )
    expected_stream_seq = publication.get("stream_seq") if isinstance(publication, dict) else None
    if type(expected_stream_seq) is not int or expected_stream_seq < 1:
        raise RuntimeError("redelivery publish sequence state was invalid")
    password = Path("/run/e2e/aura-nats-bootstrap-password").read_text(encoding="utf-8").strip()
    connection = await nats.connect(f"nats://aura-evaluation-bootstrap:{password}@aura-nats:4222")
    try:
        loop = asyncio.get_running_loop()
        stop_at = loop.time() + 10
        while True:
            try:
                response = await connection.request(
                    "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner",
                    b"{}",
                    timeout=2,
                )
                info = json.loads(response.data)
            except Exception:
                info = {}
            delivered = info.get("delivered") if isinstance(info, dict) else None
            delivered = delivered if isinstance(delivered, dict) else {}
            consumer_seq = delivered.get("consumer_seq")
            stream_seq = delivered.get("stream_seq")
            ack_pending = info.get("num_ack_pending") if isinstance(info, dict) else None
            if (
                type(consumer_seq) is int
                and consumer_seq >= 1
                and type(stream_seq) is int
                and stream_seq >= 1
                and stream_seq == expected_stream_seq
                and type(ack_pending) is int
                and ack_pending >= 1
            ):
                Path("/run/e2e-probe/redelivery-consumer-state.json").write_text(
                    json.dumps(
                        {"consumer_seq": consumer_seq, "stream_seq": expected_stream_seq},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                break
            if loop.time() >= stop_at:
                raise RuntimeError(
                    "durable runner did not retain the initial unacknowledged delivery"
                )
            await asyncio.sleep(0.2)
    finally:
        await connection.close()

asyncio.run(main())
PY
"${compose[@]}" stop aura-evaluation-runner >/dev/null
"${compose[@]}" up --detach --wait aura-core-postgres >/dev/null
sleep 32
# Restart the production runner after the ack-wait interval. Its own durable
# consumer must perform the redelivery and settle the request.
"${compose[@]}" up --detach --wait aura-evaluation-runner >/dev/null
"${compose[@]}" run --rm --no-deps -T \
  -v "$secret_root:/run/e2e:ro" \
  -v "$probe_root:/run/e2e-probe:ro" \
  -e E2E_DISPATCH_PASSWORD_FILE=/run/e2e/aura-nats-dispatch-password \
  -e E2E_RUNNER_DATABASE_PASSWORD_FILE=/run/secrets/aura-evaluation-runner-database-password \
  --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import json
from pathlib import Path
from urllib.parse import quote
import nats
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main() -> None:
    payload = json.loads(Path("/run/e2e-probe/redelivery.json").read_text(encoding="utf-8"))
    password = Path("/run/e2e/aura-nats-dispatch-password").read_text(encoding="utf-8").strip()
    connection = await nats.connect(f"nats://aura-evaluation-dispatch:{password}@aura-nats:4222")
    try:
        subscription = await connection.jetstream().pull_subscribe(
            "aura.evaluation.runner.results.v1",
            durable="aura-evaluation-dispatch",
            stream="AURA_EVALUATION_RUNNER",
        )
        messages = await subscription.fetch(1, timeout=30)
        if len(messages) != 1:
            raise RuntimeError("redelivery recovery did not publish one terminal result")
        terminal = json.loads(messages[0].data)
        if terminal.get("attemptId") != payload["attemptId"] or terminal.get("messageType") != "evaluation.runner.completed":
            raise RuntimeError("redelivery recovery terminal identity was not deterministic")
        await messages[0].ack()
    finally:
        await connection.close()
    database_password = Path("/run/secrets/aura-evaluation-runner-database-password").read_text(encoding="utf-8").strip()
    engine = create_async_engine(f"postgresql+psycopg://aura_evaluation_runner:{quote(database_password, safe='')}@aura-core-postgres:5432/aura")
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT status, publication_status, attempt_count FROM evaluation_runner_attempts WHERE attempt_id = :attempt_id"), {"attempt_id": payload["attemptId"]})).mappings().one()
            if row["status"] != "completed" or row["publication_status"] != "published" or int(row["attempt_count"]) != 1:
                raise RuntimeError("redelivery recovery created an unexpected durable state")
            outbox = (await connection.execute(text("SELECT COUNT(*) FROM evaluation_runner_terminal_outbox WHERE attempt_id = :attempt_id"), {"attempt_id": payload["attemptId"]})).scalar_one()
            if int(outbox) != 1:
                raise RuntimeError("redelivery recovery created duplicate terminal outbox rows")
    finally:
        await engine.dispose()

asyncio.run(main())
PY

# Confirm the production runner performed the redelivery that produced the
# terminal result. The query is bounded because JetStream management replies
# can be transient during dependency recovery.
"${compose[@]}" run --rm --no-deps -T \
  -v "$secret_root:/run/e2e:ro" \
  -v "$probe_root:/run/e2e-probe:ro" \
  -e E2E_BOOTSTRAP_PASSWORD_FILE=/run/e2e/aura-nats-bootstrap-password \
  --entrypoint /app/.venv/bin/python aura-evaluation-runner - <<'PY' >/dev/null
import asyncio
import json
from pathlib import Path
import nats

async def main() -> None:
    import sys

    initial = json.loads(
        Path("/run/e2e-probe/redelivery-consumer-state.json").read_text(encoding="utf-8")
    )
    initial_consumer_seq = initial.get("consumer_seq") if isinstance(initial, dict) else None
    initial_stream_seq = initial.get("stream_seq") if isinstance(initial, dict) else None
    if (
        type(initial_consumer_seq) is not int
        or initial_consumer_seq < 1
        or type(initial_stream_seq) is not int
        or initial_stream_seq < 1
    ):
        raise RuntimeError("initial durable consumer sequence state was invalid")

    def bounded_delta(value, baseline):
        if type(value) is int and type(baseline) is int:
            delta = value - baseline
            if -1_000_000_000 <= delta <= 1_000_000_000:
                return delta
        return "na"

    def bounded_count(value):
        return value if type(value) is int and 0 <= value <= 1_000_000_000 else "na"

    password = Path("/run/e2e/aura-nats-bootstrap-password").read_text(encoding="utf-8").strip()
    connection = await nats.connect(f"nats://aura-evaluation-bootstrap:{password}@aura-nats:4222")
    try:
        loop = asyncio.get_running_loop()
        stop_at = loop.time() + 10
        while True:
            try:
                response = await connection.request(
                    "$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner",
                    b"{}",
                    timeout=2,
                )
                info = json.loads(response.data)
            except Exception:
                info = {}
            delivered = info.get("delivered") if isinstance(info, dict) else None
            delivered = delivered if isinstance(delivered, dict) else {}
            consumer_seq = delivered.get("consumer_seq")
            stream_seq = delivered.get("stream_seq")
            ack_floor = info.get("ack_floor") if isinstance(info, dict) else None
            ack_floor = ack_floor if isinstance(ack_floor, dict) else {}
            ack_floor_consumer_seq = ack_floor.get("consumer_seq")
            ack_floor_stream_seq = ack_floor.get("stream_seq")
            ack_pending = info.get("num_ack_pending") if isinstance(info, dict) else None
            if (
                type(consumer_seq) is int
                and consumer_seq == initial_consumer_seq + 1
                and type(stream_seq) is int
                and stream_seq >= initial_stream_seq
                and type(ack_floor_consumer_seq) is int
                and ack_floor_consumer_seq == initial_consumer_seq + 1
                and type(ack_floor_stream_seq) is int
                and ack_floor_stream_seq >= initial_stream_seq
                and type(ack_pending) is int
                and ack_pending == 0
            ):
                break
            if loop.time() >= stop_at:
                delivered_stream_matches = (
                    type(stream_seq) is int
                    and type(initial_stream_seq) is int
                    and stream_seq >= initial_stream_seq
                )
                ack_floor_stream_matches = (
                    type(ack_floor_stream_seq) is int
                    and type(initial_stream_seq) is int
                    and ack_floor_stream_seq >= initial_stream_seq
                )
                print(
                    "AURA_E2E redelivery_sequence_diagnostic "
                    f"consumer_seq_delta={bounded_delta(consumer_seq, initial_consumer_seq)} "
                    f"delivered_stream_at_or_after_request={str(delivered_stream_matches).lower()} "
                    f"ack_floor_consumer_seq_delta={bounded_delta(ack_floor_consumer_seq, initial_consumer_seq)} "
                    f"ack_floor_stream_at_or_after_request={str(ack_floor_stream_matches).lower()} "
                    f"ack_pending={bounded_count(ack_pending)}",
                    file=sys.stderr,
                )
                raise RuntimeError(
                    "production runner did not settle the durable redelivery"
                )
            await asyncio.sleep(0.2)
    finally:
        await connection.close()

asyncio.run(main())
PY

# Re-run the migration job against the already materialized schema.  The
# initial project startup proves the empty-schema path; this proves the
# upgraded/idempotent path without creating a second Compose project.
"${compose[@]}" run --rm aura-core-migrate >/dev/null

# Core independence is a live operation, not only a topology assertion. Keep
# the runner stopped while the normal API, worker, and scheduler are started;
# then execute the public liveness request from inside the API container.
"${compose[@]}" stop aura-evaluation-runner >/dev/null
"${compose[@]}" up --build --detach --wait aura-core-api aura-core-worker aura-core-scheduler >/dev/null
"${compose[@]}" exec -T aura-core-api /app/.venv/bin/python -c \
  'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8000/health/live", timeout=3); assert response.status == 200'
for core_service in aura-core-api aura-core-worker aura-core-scheduler; do
  state=$(docker inspect -f '{{.State.Status}}' "$project-$core_service-1" 2>/dev/null || true)
  test "$state" = running || { echo "normal Core service is not running: $core_service" >&2; exit 1; }
done

# Restart and dependency recovery are scoped to this project.  The runner is
# brought back only after the normal Core independence probe completes.
"${compose[@]}" up --detach --wait aura-evaluation-runner >/dev/null
echo 'AURA-0091 evaluation runner deployment acceptance passed'
