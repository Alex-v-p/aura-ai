#!/usr/bin/env bash
set -Eeuo pipefail

# Project-scoped Docker Desktop acceptance for AURA-0092.  The probe creates
# Observatory-owned jobs through the internal repository seam, never through
# a public HTTP endpoint, and verifies the real Core runner round trip.  It
# uses disposable Compose volumes only for this acceptance; production
# AURA_STATE_ROOT bindings are not changed.
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
export COMPOSE_PARALLEL_LIMIT=1
fixture_root=$(mktemp -d "$ROOT/.aura-0092-e2e.XXXXXX")
project="aura-0092-e2e-$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')"
env_file="$fixture_root/.env"
override_file="$fixture_root/compose.e2e.yaml"
secret_root="$fixture_root/secrets"
state_root="$fixture_root/state"
mkdir -p "$secret_root" "$state_root"
chmod 0700 "$fixture_root" "$secret_root" "$state_root"

start_timeout=${AURA_E2E_COMPOSE_START_TIMEOUT_SECONDS:-900}
step_timeout=${AURA_E2E_COMPOSE_STEP_TIMEOUT_SECONDS:-180}
cleanup_timeout=${AURA_E2E_COMPOSE_CLEANUP_TIMEOUT_SECONDS:-90}
bounded() { timeout --foreground --signal=TERM --kill-after=10s "${1}s" "${@:2}"; }
# shellcheck source=e2e/diagnostic-categories.sh
. "$ROOT/deploy/compose/e2e/diagnostic-categories.sh"

remove_disposable_networks() {
  local network_rows network_id network_name
  network_rows=$(docker network ls --filter "label=com.docker.compose.project=$project" \
    --format '{{.ID}} {{.Name}}' 2>/dev/null || true)
  while read -r network_id network_name; do
    [ -n "$network_id" ] || continue
    case "$network_name" in
      "${project}_"*)
        # Compose normally removes these during down. Keep the fallback exact
        # to this generated project so a failed network create cannot leave a
        # future run fighting an orphaned disposable subnet.
        docker network rm "$network_id" >/dev/null 2>&1 || true
        ;;
    esac
  done <<<"$network_rows"
}

write_secret() {
  local name=$1 value=$2
  printf '%s\n' "$value" >"$secret_root/$name"
  chmod 0600 "$secret_root/$name"
}

random_secret() { openssl rand -hex 32; }
random_access_key() { printf 'GK%s' "$(openssl rand -hex 12)"; }

emit_failure_diagnostics() {
  local service container_id
  for service in \
    observatory-migrate observatory-postgres observatory-clickhouse observatory-session \
    observatory-api observatory-collector observatory-evaluation-worker \
    observatory-evaluation-scheduler aura-core-migrate aura-core-postgres \
    aura-evaluation-runner; do
    container_id=$(docker compose -p "$project" --env-file "$env_file" \
      -f "$ROOT/compose.yaml" -f "$override_file" --profile observatory \
      ps -q "$service" 2>/dev/null | head -n 1 || true)
    if [ -z "$container_id" ]; then
      continue
    fi
    docker inspect --format "$service status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" \
      "$container_id" 2>/dev/null || true
    # Never print source log lines or values. Only finite category names/counts
    # may leave this disposable diagnostic boundary.
    categories=$(bounded 15 docker logs --tail 100 "$container_id" 2>&1 |
      classify_diagnostic_stream) || categories=categories=none
    printf '%s %s\n' "$service" "$categories"
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ] && [ -f "$env_file" ] && [ -f "$override_file" ]; then
    emit_failure_diagnostics
  fi
  if [ -f "$env_file" ]; then
    bounded "$cleanup_timeout" docker compose -p "$project" --env-file "$env_file" \
      -f "$ROOT/compose.yaml" -f "$override_file" --profile observatory \
      down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  remove_disposable_networks
  bounded "$cleanup_timeout" find "$fixture_root" -depth -delete 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

# Hold a repository-scoped host lock from IPAM inspection through Compose
# startup. This prevents two disposable harnesses from selecting the same
# currently-free subnet before either has created its network.
root_lock_id=$(printf '%s' "$ROOT" | sha256sum | cut -c1-16)
network_lock="/tmp/aura-e2e-network-${root_lock_id}.lock"
exec 9>"$network_lock"
if ! flock -w "${AURA_E2E_NETWORK_LOCK_TIMEOUT_SECONDS:-30}" 9; then
  printf '%s\n' 'timed out waiting for the disposable E2E network allocation lock' >&2
  exit 1
fi

write_secret aura-core-database-password "$(random_secret)"
write_secret aura-oidc-client-secret "$(random_secret)"
write_secret aura-nats-password "$(random_secret)"
write_secret aura-nats-bootstrap-password "$(random_secret)"
write_secret aura-nats-dispatch-password "$(random_secret)"
write_secret aura-nats-runner-password "$(random_secret)"
write_secret aura-valkey-password "$(random_secret)"
write_secret aura-garage-rpc-secret "$(random_secret)"
write_secret aura-garage-access-key "$(random_access_key)"
write_secret aura-garage-secret-key "$(random_secret)"
write_secret observatory-database-password "$(random_secret)"
write_secret observatory-oidc-client-secret "$(random_secret)"
write_secret observatory-session-password "$(random_secret)"
write_secret observatory-ingest-credential "$(random_secret)"
write_secret observatory-clickhouse-password "$(random_secret)"
write_secret observatory-garage-access-key "$(random_access_key)"
write_secret observatory-garage-secret-key "$(random_secret)"
# The local judge bootstrap is also a disposable consumer of the shared Garage
# layout. Keep its identity fixture-backed so Compose never falls back to the
# repository's operator .secrets directory.
write_secret observatory-judge-garage-access-key "$(random_access_key)"
write_secret observatory-judge-garage-secret-key "$(random_secret)"
write_secret observatory-api-garage-access-key "$(random_access_key)"
write_secret observatory-api-garage-secret-key "$(random_secret)"
write_secret aura-evaluation-runner-garage-access-key "$(random_access_key)"
write_secret aura-evaluation-runner-garage-secret-key "$(random_secret)"
write_secret aura-evaluation-runner-database-password "$(random_secret)"

# Allocate every statically-addressed Compose network from disposable pools
# after inspecting Docker's current IPAM. This keeps the runner compatible
# with an already-running normal Aura stack and with other E2E projects while
# giving the frontend its required /24 and the other networks /28s. The
# provider network is included in the inventory, and COMPOSE_PARALLEL_LIMIT=1
# preserves deterministic lifecycle ordering.
mapfile -t network_id_array < <(docker network ls --quiet)
if [ "${#network_id_array[@]}" -gt 0 ]; then
  docker network inspect "${network_id_array[@]}" > "$fixture_root/docker-networks.json"
else
  printf '%s\n' '[]' > "$fixture_root/docker-networks.json"
fi
network_names=(
  aura-edge aura-frontend aura-core-db aura-core-bus aura-evaluation-bus
  aura-core-cache aura-document-parser aura-object-storage aura-core-egress
  observatory-edge observatory-api observatory-db observatory-session
  observatory-ingest observatory-identity-egress observatory-judge-egress
  observatory-object-storage
)
declare -A network_subnets=()
while read -r network_name network_subnet; do
  [ -n "$network_name" ] || continue
  network_subnets["$network_name"]="$network_subnet"
done < <(
  python3 - "$fixture_root/docker-networks.json" "$project" "${network_names[@]}" <<'PY'
import ipaddress
import json
import sys

network_path, project, *network_names = sys.argv[1:]
with open(network_path, encoding="utf-8") as handle:
    networks = json.load(handle)

occupied = []
for network in networks:
    for config in (network.get("IPAM") or {}).get("Config") or []:
        value = config.get("Subnet")
        if not value:
            continue
        try:
            occupied.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue

frontend_free = [
    candidate
    for candidate in ipaddress.ip_network("10.254.128.0/18").subnets(new_prefix=24)
    if all(not candidate.overlaps(existing) for existing in occupied)
]
small_free = [
    candidate
    for candidate in ipaddress.ip_network("10.254.192.0/18").subnets(new_prefix=28)
    if all(not candidate.overlaps(existing) for existing in occupied)
]
if not frontend_free:
    raise SystemExit("no free /24 frontend subnet remains in the dedicated E2E pool")
if len(small_free) < len(network_names) - 1:
    raise SystemExit("not enough free /28 subnets remain for the disposable E2E networks")

offset = sum(ord(char) for char in project)
frontend = frontend_free[offset % len(frontend_free)]
small_offset = offset % len(small_free)
selected = [small_free[(small_offset + index) % len(small_free)] for index in range(len(network_names) - 1)]
print(network_names[0], frontend)
for name, subnet in zip(network_names[1:], selected):
    print(name, subnet)
PY
)
for network_name in "${network_names[@]}"; do
  test -n "${network_subnets[$network_name]:-}"
done
frontend_subnet=${network_subnets[aura-frontend]}
document_parser_subnet=${network_subnets[aura-document-parser]}
object_storage_subnet=${network_subnets[aura-object-storage]}
observatory_object_storage_subnet=${network_subnets[observatory-object-storage]}

cat >"$env_file" <<EOF
AURA_STATE_ROOT=$state_root
AURA_PUBLIC_ORIGIN=https://aura0092.example.invalid
AURA_OIDC_ISSUER=https://identity.aura0092.example.invalid/application/o/aura-web/
AURA_OIDC_AUDIENCE=aura-web
AURA_OIDC_CLIENT_ID=aura-web
AURA_OIDC_REDIRECT_URI=https://aura0092.example.invalid/api/v1/auth/callback
AURA_OWNER_SUBJECT=aura0092-owner
AURA_CORE_DATABASE_PASSWORD_FILE=$secret_root/aura-core-database-password
AURA_OIDC_CLIENT_SECRET_FILE=$secret_root/aura-oidc-client-secret
AURA_NATS_PASSWORD_FILE=$secret_root/aura-nats-password
AURA_NATS_BOOTSTRAP_PASSWORD_FILE=$secret_root/aura-nats-bootstrap-password
AURA_NATS_DISPATCH_PASSWORD_FILE=$secret_root/aura-nats-dispatch-password
AURA_NATS_RUNNER_PASSWORD_FILE=$secret_root/aura-nats-runner-password
AURA_VALKEY_PASSWORD_FILE=$secret_root/aura-valkey-password
AURA_GARAGE_RPC_SECRET_FILE=$secret_root/aura-garage-rpc-secret
AURA_GARAGE_ACCESS_KEY_FILE=$secret_root/aura-garage-access-key
AURA_GARAGE_SECRET_KEY_FILE=$secret_root/aura-garage-secret-key
AURA_OBSERVATORY_DATABASE_PASSWORD_FILE=$secret_root/observatory-database-password
AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE=$secret_root/observatory-oidc-client-secret
AURA_OBSERVATORY_SESSION_PASSWORD_FILE=$secret_root/observatory-session-password
AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE=$secret_root/observatory-ingest-credential
AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE=$secret_root/observatory-clickhouse-password
AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE=$secret_root/observatory-garage-access-key
AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE=$secret_root/observatory-garage-secret-key
AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE=$secret_root/observatory-judge-garage-access-key
AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE=$secret_root/observatory-judge-garage-secret-key
AURA_OBSERVATORY_API_GARAGE_ACCESS_KEY_FILE=$secret_root/observatory-api-garage-access-key
AURA_OBSERVATORY_API_GARAGE_SECRET_KEY_FILE=$secret_root/observatory-api-garage-secret-key
AURA_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_FILE=$secret_root/aura-evaluation-runner-garage-access-key
AURA_EVALUATION_RUNNER_GARAGE_SECRET_KEY_FILE=$secret_root/aura-evaluation-runner-garage-secret-key
AURA_EVALUATION_RUNNER_DATABASE_PASSWORD_FILE=$secret_root/aura-evaluation-runner-database-password
AURA_CORE_DATABASE_NAME=aura
AURA_OBSERVATORY_DATABASE_NAME=aura_observatory
AURA_EVALUATION_RUNNER_DATABASE_USER=aura_evaluation_runner
AURA_GARAGE_BUCKET=aura-documents
AURA_OBSERVATORY_GARAGE_BUCKET=aura-observatory-evaluation
AURA_OBSERVATORY_ENVIRONMENT=disposable_local
AURA_OBSERVATORY_OWNER_SUBJECT=aura0092-owner
AURA_OBSERVATORY_EVALUATION_WORKER_ID=00000000-0000-4000-8000-000000000092
AURA_OBSERVATORY_EVALUATION_SCHEDULER_ID=00000000-0000-4000-8000-000000000093
AURA_OBSERVATORY_EVALUATION_POLL_SECONDS=1
AURA_OBSERVATORY_EVALUATION_RETRY_BASE_SECONDS=1
AURA_OBSERVATORY_EVALUATION_RETRY_MAX_SECONDS=4
AURA_OBSERVATORY_EVALUATION_LEASE_SECONDS=5
AURA_EVALUATION_RUNNER_MAX_CONCURRENCY=1
AURA_EVALUATION_RUNNER_MAX_QUEUE_DEPTH=4
AURA_EVALUATION_RUNNER_MAX_TIMEOUT_MS=30000
AURA_EVALUATION_RUNNER_MAX_CASES=1
AURA_EVALUATION_RUNNER_MAX_MODEL_CALLS=1
AURA_EVALUATION_RUNNER_MAX_ARTIFACT_OPERATIONS=0
AURA_TELEMETRY_OTLP_ENABLED=false
AURA_TELEMETRY_DEPLOYMENT_ID=aura0092-e2e
AURA_FRONTEND_SUBNET=$frontend_subnet
AURA_DOCUMENT_PARSER_SUBNET=$document_parser_subnet
AURA_OBJECT_STORAGE_SUBNET=$object_storage_subnet
AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET=$observatory_object_storage_subnet
EOF
chmod 0600 "$env_file"

# Named volumes keep Docker Desktop high-write databases off the host bind
# mounts.  They have no external names and are removed by this exact project.
cat >"$override_file" <<EOF
services:
  aura-core-postgres:
    volumes: !override [e2e_core_postgres_data:/var/lib/postgresql/data]
  aura-nats:
    volumes: !override
      - e2e_nats_data:/data
      - ${ROOT}/deploy/docker/nats-entrypoint.sh:/usr/local/bin/aura-nats-entrypoint:ro
  aura-garage:
    volumes: !override
      - e2e_garage_data:/var/lib/garage
      - e2e_garage_config:/run/garage-config:ro
  aura-garage-config:
    volumes: !override
      - e2e_garage_config:/run/garage-config
      - ${ROOT}/deploy/docker/garage.toml:/etc/garage/garage.toml.template:ro
  aura-garage-bootstrap:
    volumes: !override
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-garage-bootstrap:
    volumes: !override
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-judge-garage-bootstrap:
    volumes: !override
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-api-garage-bootstrap:
    volumes: !override
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-api:
    secrets:
      - observatory-database-password
      - observatory-oidc-client-secret
      - observatory-session-password
      - observatory-ingest-credential
      - observatory-clickhouse-password
      - observatory-api-garage-access-key
      - observatory-api-garage-secret-key
    networks:
      - observatory-api
      - observatory-db
      - observatory-session
      - observatory-ingest
      - observatory-identity-egress
      - observatory-object-storage
  aura-evaluation-runner-garage-bootstrap:
    volumes: !override
      - e2e_garage_data:/var/lib/garage:ro
      - e2e_garage_config:/run/garage-config:ro
  observatory-postgres:
    volumes: !override [e2e_observatory_postgres_data:/var/lib/postgresql/data]
  observatory-clickhouse:
    volumes: !override
      - e2e_observatory_clickhouse_data:/var/lib/clickhouse
      - e2e_observatory_clickhouse_logs:/var/log/clickhouse-server
  observatory-session:
    volumes: !override [e2e_observatory_session_data:/data]
  observatory-evaluation-worker:
    environment:
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED: "true"
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT: http://observatory-collector:4318/v1/traces
      AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
      AURA_OBSERVATORY_DEPLOYMENT_ID: aura0092-e2e
      AURA_OBSERVATORY_DEPLOYMENT_COMMIT: "0000000"
      AURA_OBSERVATORY_DEPLOYMENT_HOST: docker-desktop
    # Compose list overrides replace the base list. Repeat every required
    # private attachment and secret so this disposable overlay does not make
    # the worker healthy-but-isolated from its database or evaluation bus.
    secrets: [observatory-database-password, aura-nats-dispatch-password, observatory-ingest-credential]
    networks: [observatory-db, aura-evaluation-bus, observatory-ingest]
    healthcheck:
      test: [CMD-SHELL, "test -r /app/services/aura-observatory/src/aura_observatory/entrypoints/evaluation_worker.py"]
      timeout: 5s
      start_period: 5s
  observatory-evaluation-scheduler:
    environment:
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED: "true"
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT: http://observatory-collector:4318/v1/traces
      AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
      AURA_OBSERVATORY_DEPLOYMENT_ID: aura0092-e2e
      AURA_OBSERVATORY_DEPLOYMENT_COMMIT: "0000000"
      AURA_OBSERVATORY_DEPLOYMENT_HOST: docker-desktop
    secrets: [observatory-database-password, observatory-ingest-credential]
    networks: [observatory-db, observatory-ingest]
    healthcheck:
      test: [CMD-SHELL, "test -r /app/services/aura-observatory/src/aura_observatory/entrypoints/evaluation_scheduler.py"]
      timeout: 5s
      start_period: 5s
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

# Docker otherwise allocates the unbounded internal networks from its global
# default pools. Override every network used by this orchestration fixture so
# a host with many saved Compose projects cannot exhaust those pools.
{
  printf '%s\n' 'networks:'
  for network_name in "${network_names[@]}"; do
    printf '  %s:\n    ipam: !override\n      config:\n        - subnet: %s\n' \
      "$network_name" "${network_subnets[$network_name]}"
  done
} >> "$override_file"

compose=(docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override_file" --profile observatory)
"${compose[@]}" config --quiet
wait_running() {
  local deadline=$((SECONDS + step_timeout))
  local service container_id running container_ids
  while (( SECONDS < deadline )); do
    running=1
    for service in "$@"; do
      container_ids=$("${compose[@]}" ps -q "$service" 2>/dev/null || true)
      if [ -z "$container_ids" ]; then
        running=0
        continue
      fi
      while read -r container_id; do
        if [ -z "$container_id" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null || true)" != true ]; then
          running=0
        fi
      done <<<"$container_ids"
    done
    if (( running == 1 )); then
      return 0
    fi
    sleep 1
  done
  printf 'AURA_E2E startup timeout waiting for running services\n' >&2
  return 1
}
wait_healthy() {
  local deadline=$((SECONDS + step_timeout))
  local service container_id healthy
  while (( SECONDS < deadline )); do
    healthy=1
    for service in "$@"; do
      container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
      if [ -z "$container_id" ] || [ "$(docker inspect -f '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)" != healthy ]; then
        healthy=0
      fi
    done
    if (( healthy == 1 )); then
      return 0
    fi
    sleep 1
  done
  printf 'AURA_E2E health timeout waiting for services\n' >&2
  return 1
}

assert_scheduler_scale_prerequisites() {
  local migration_container migration_status migration_exit

  # The first Compose up above is the mandatory migration barrier. Before
  # scaling an already-running scheduler, verify that barrier and its direct
  # runtime dependency explicitly. This keeps the later scale operation from
  # asking Compose to re-evaluate a one-shot migration service.
  wait_healthy observatory-postgres observatory-evaluation-scheduler
  migration_container=$("${compose[@]}" ps -aq observatory-migrate 2>/dev/null | head -n 1 || true)
  if [ -z "$migration_container" ]; then
    printf '%s\n' 'AURA_E2E scheduler scale prerequisite missing completed observatory migration' >&2
    return 1
  fi
  migration_status=$(docker inspect -f '{{.State.Status}}' "$migration_container" 2>/dev/null || true)
  migration_exit=$(docker inspect -f '{{.State.ExitCode}}' "$migration_container" 2>/dev/null || true)
  if [ "$migration_status" != exited ] || [ "$migration_exit" != 0 ]; then
    printf 'AURA_E2E scheduler scale prerequisite migration status=%s exit=%s\n' \
      "$migration_status" "$migration_exit" >&2
    return 1
  fi
}

assert_runner_remains_available() {
  wait_running aura-evaluation-runner
  printf '%s\n' 'AURA_E2E Core evaluation runner remained available during Observatory evaluation outage'
}

if ! bounded "$start_timeout" "${compose[@]}" up --build --detach observatory-api observatory-collector observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner; then
  # Keep startup failures actionable without emitting credentials, payloads, or
  # provider diagnostics from service logs.
  for service in observatory-collector observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner; do
    container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null || true)
    if [ -n "$container_id" ]; then
      docker inspect --format "$service status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" "$container_id"
      categories=$(docker logs --tail 80 "$container_id" 2>&1 |
        classify_diagnostic_stream) || categories=categories=none
      printf '%s %s\n' "$service" "$categories"
    fi
  done
  exit 1
fi
wait_running observatory-api observatory-collector observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner

# The probe runs inside a service image and uses only the internal PostgreSQL
# repository seam. It deliberately emits status classes, never identifiers or
# message bodies. The request has no input artifacts and is metadata-only.
probe_script="$fixture_root/probe.py"
cat >"$probe_script" <<'PY'
import asyncio
import json
import math
import os
import re
import urllib.request
from base64 import b64encode
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import nats
import psycopg
from aura_observatory.adapters.evaluation_orchestration import PostgresEvaluationOrchestrationRepository
from aura_observatory.evaluation_orchestration import EvaluationAdmission, EvaluationOrchestrationService
from aura_observatory.settings import Settings


async def _wait_for_state(repository, job_id: str, states: set[str], timeout: int = 90):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = await repository.get_job(job_id)
        if current is not None and current.state.value in states:
            return current
        await asyncio.sleep(1)
    raise SystemExit("timed out waiting for durable evaluation state")


async def _clickhouse_rows(settings: Settings, query: str) -> list[dict[str, object]]:
    password = settings.clickhouse_password
    auth = b64encode(f"{settings.clickhouse_user}:{password}".encode()).decode()
    endpoint = settings.clickhouse_url.rstrip("/") + "/?database=" + settings.clickhouse_database

    def request() -> list[dict[str, object]]:
        http_request = urllib.request.Request(
            endpoint,
            data=(query.rstrip().rstrip(";") + " FORMAT JSONEachRow").encode(),
            headers={"Authorization": f"Basic {auth}"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=5) as response:
            lines = response.read().decode().splitlines()
        return [dict(json.loads(line)) for line in lines if line.strip()]

    return await asyncio.to_thread(request)


def _terminal_result(request_envelope: dict[str, object]) -> dict[str, object]:
    request_data = dict(request_envelope["data"])
    occurred = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    runtime_evidence = {
        key: {"status": "missing"}
        for key in (
            "componentVersion", "codeRevision", "imageDigest", "configurationRevision",
            "modelId", "modelVersion", "modelDigest", "deploymentRevision",
            "fixtureRevision", "promptRevision", "policyRevision", "seed",
        )
    }
    result = dict(request_envelope)
    result.update(
        {
            "messageId": str(uuid4()),
            "messageType": "evaluation.runner.completed",
            "occurredAt": occurred,
            "causationId": request_envelope["messageId"],
            "data": {
                "resultId": str(uuid4()),
                "resultDigest": "b" * 64,
                "status": "completed",
                "startedAt": occurred,
                "completedAt": occurred,
                "deadlineAt": request_data["deadlineAt"],
                "captureAuthorization": request_data["captureAuthorization"],
                "effectiveLimits": request_data["limits"],
                "provenance": request_data["provenance"],
                "runtimeEvidence": runtime_evidence,
                "outputArtifactReferences": [],
            },
        }
    )
    return result


async def _exercise_jetstream_redelivery(settings: Settings) -> None:
    """Leave an Observatory result unacknowledged for the real worker."""
    redelivery_job_id = os.environ.get("AURA_E2E_REDELIVERY_JOB_ID", "")
    if not redelivery_job_id:
        raise SystemExit("redelivery job correlation is missing")
    async with _connection(settings.database_url) as connection:
        result = await connection.execute(
            "SELECT envelope_json FROM observatory_evaluation_outbox "
            "WHERE event_type='evaluation.runner.requested' AND published_at IS NULL "
            "AND job_id=%s ORDER BY created_at DESC LIMIT 1",
            (redelivery_job_id,),
        )
        row = await result.fetchone()
    if row is None or not isinstance(row[0], dict):
        raise SystemExit("no pending request available for JetStream redelivery")
    dispatch_password = Path("/run/secrets/aura-nats-dispatch-password").read_text(encoding="utf-8").strip()
    dispatch = await nats.connect(
        settings.evaluation_nats_url, user="aura-evaluation-dispatch", password=dispatch_password
    )
    try:
        await dispatch.jetstream().publish(
            "aura.evaluation.runner.requested.v1",
            json.dumps(dict(row[0]), sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={"Nats-Msg-Id": str(dict(row[0])["messageId"])},
        )
        subscription = await dispatch.jetstream().pull_subscribe(
            "aura.evaluation.runner.results.v1",
            durable="aura-evaluation-dispatch",
            stream="AURA_EVALUATION_RUNNER",
        )
        request_id = str(dict(row[0])["messageId"])
        matching = None
        for _ in range(4):
            candidate = (await subscription.fetch(1, timeout=10))[0]
            try:
                payload = json.loads(candidate.data)
            except (TypeError, ValueError):
                await candidate.ack()
                continue
            if payload.get("causationId") == request_id:
                matching = candidate
                break
            await candidate.ack()
        if matching is None:
            raise SystemExit("no matching Observatory result available for redelivery")
        await asyncio.sleep(31)
    finally:
        await dispatch.drain()


async def main() -> None:
    settings = Settings(role="evaluation_scheduler")
    repository = PostgresEvaluationOrchestrationRepository(
        lambda: _connection(settings.database_url)
    )
    service = EvaluationOrchestrationService(repository)
    phase = os.environ.get("AURA_E2E_PHASE", "round_trip")
    if phase == "health_privacy":
        async with _connection(settings.database_url) as connection:
            health = await connection.execute(
                "SELECT backlog_count, outbox_pending_count, result_lag_count "
                "FROM observatory_evaluation_worker_leases "
                "WHERE component_id='aura.observatory.evaluation_orchestration'"
            )
            snapshot = await health.fetchone()
            if snapshot is None or any(type(value) is not int or value < 0 for value in snapshot):
                raise SystemExit("persisted health snapshot missing or unbounded")
            tables = (
                "observatory_evaluation_jobs", "observatory_evaluation_attempts",
                "observatory_evaluation_schedules", "observatory_evaluation_outbox",
                "observatory_evaluation_result_receipts",
                "observatory_evaluation_lifecycle_audits",
                "observatory_evaluation_worker_leases", "observatory_evaluation_metric_series",
            )
            forbidden_pattern = (
                r'"(prompt|response|content|filename|tool|argument|credentials?|endpoint|'
                r'bearer|owner|identity|secret|password|token|url|uri|traceback|'
                r'exception|stacktrace|diagnostic|provider_error|provider_diagnostic)"\s*:'
            )
            for table in tables:
                forbidden = await connection.execute(
                    f"SELECT count(*) FROM {table} AS row "
                    "WHERE to_jsonb(row)::text ~* %s",
                    (forbidden_pattern,),
                )
                if (await forbidden.fetchone())[0] != 0:
                    raise SystemExit("privacy field found in persisted evaluation state")
        print(json.dumps({"phase": "health_privacy", "state": "verified"}, sort_keys=True))
        return
    if phase == "dependency_metrics":
        # This is the service-owned persistence boundary for evaluation
        # dependency metrics.  Do not scrape an unrelated process metric or
        # print labels/content: assert the exact fixed labels and finite
        # values after the real PostgreSQL, NATS, and result/score paths.
        async with _connection(settings.database_url) as connection:
            result = await connection.execute(
                "SELECT label_operation, label_outcome, value "
                "FROM observatory_evaluation_metric_series "
                "WHERE metric_name='observatory_evaluation_dependency_outcome_count' "
                "ORDER BY label_operation, label_outcome"
            )
            rows = await result.fetchall()
        allowed_operations = {
            "postgresql", "evaluation_nats", "clickhouse", "garage", "ollama",
            "evaluation_definition_state",
        }
        allowed_outcomes = {
            "success", "published", "failed", "failure", "unavailable", "duplicate",
        }
        observed: set[tuple[str, str]] = set()
        for operation, outcome, value in rows:
            operation_text = str(operation)
            outcome_text = str(outcome)
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                raise SystemExit("dependency outcome metric was not numeric")
            if (
                operation_text not in allowed_operations
                or outcome_text not in allowed_outcomes
                or not operation_text.isascii()
                or not outcome_text.isascii()
                or len(operation_text) > 64
                or len(outcome_text) > 64
                or not math.isfinite(numeric_value)
                or numeric_value < 0
                or numeric_value > 1_000_000_000_000
            ):
                raise SystemExit("dependency outcome metric was not finite and bounded")
            observed.add((operation_text, outcome_text))
        required = {("postgresql", "success"), ("evaluation_nats", "published")}
        if not required.issubset(observed):
            raise SystemExit("dependency outcome metric samples missing real PG/NATS paths")
        print(json.dumps({"phase": "dependency_metrics", "state": "verified", "sample_count": len(rows)}, sort_keys=True))
        return
    if phase == "jetstream_redelivery":
        await _exercise_jetstream_redelivery(settings)
        print(json.dumps({"phase": "jetstream_redelivery", "state": "left_pending"}, sort_keys=True))
        return
    if phase == "duplicate":
        async with _connection(settings.database_url) as connection:
            result_row = await connection.execute(
                "SELECT envelope_json FROM observatory_evaluation_result_receipts "
                "ORDER BY received_at DESC LIMIT 1"
            )
            row = await result_row.fetchone()
        if row is None:
            raise SystemExit("no durable result available for redelivery")
        result = dict(row[0])
        first = await service.consume_result(result)
        second = await service.consume_result(result)
        if first != "duplicate" or second != "duplicate":
            raise SystemExit("durable result redelivery was not idempotent")
        print(json.dumps({"phase": "redelivery", "state": "duplicate"}, sort_keys=True))
        return
    if phase in {"redelivery_verify", "deadline_verify", "schedule_verify", "schedule_recovery"}:
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            async with _connection(settings.database_url) as connection:
                if phase == "redelivery_verify":
                    redelivery_job_id = os.environ.get("AURA_E2E_REDELIVERY_JOB_ID", "")
                    if not redelivery_job_id:
                        raise SystemExit("redelivery job correlation is missing")
                    result = await connection.execute(
                        "WITH receipt AS ("
                        "SELECT job_id, attempt_id, count(*) AS receipt_count "
                        "FROM observatory_evaluation_result_receipts WHERE job_id=%s "
                        "GROUP BY job_id, attempt_id) "
                        "SELECT j.state, COALESCE(r.receipt_count, 0), "
                        "(SELECT count(*) FROM observatory_evaluation_lifecycle_audits a "
                        "WHERE a.job_id=j.job_id AND a.attempt_id=r.attempt_id "
                        "AND a.event_type LIKE 'evaluation.%%' "
                        "AND a.event_type NOT IN ('evaluation.retry_scheduled', "
                        "'evaluation.deadline', 'evaluation.schedule_materialized') "
                        "AND a.outcome='accepted') "
                        "FROM observatory_evaluation_jobs j "
                        "LEFT JOIN receipt r ON r.job_id=j.job_id "
                        "WHERE j.job_id=%s",
                        (redelivery_job_id, redelivery_job_id),
                    )
                    row = await result.fetchone()
                    if (
                        row is not None
                        and row[0] == "succeeded"
                        and int(row[1]) == 1
                        and int(row[2]) == 1
                    ):
                        print(json.dumps({"phase": "jetstream_redelivery", "state": "recovered"}, sort_keys=True))
                        return
                elif phase == "deadline_verify":
                    result = await connection.execute(
                        "SELECT state FROM observatory_evaluation_jobs ORDER BY created_at DESC LIMIT 1"
                    )
                    row = await result.fetchone()
                    if row is not None and row[0] == "deadline":
                        print(json.dumps({"phase": "deadline", "state": "deadline"}, sort_keys=True))
                        return
                else:
                    result = await connection.execute(
                        "SELECT j.state, count(a.audit_id) FROM observatory_evaluation_jobs j "
                        "JOIN observatory_evaluation_lifecycle_audits a ON a.job_id=j.job_id "
                        "AND a.event_type='evaluation.schedule_materialized' "
                        "GROUP BY j.job_id ORDER BY j.created_at DESC LIMIT 1"
                    )
                    row = await result.fetchone()
                    if row is not None and row[0] == "succeeded" and int(row[1]) == 1:
                        print(json.dumps({"phase": "scheduled_execution", "state": "succeeded"}, sort_keys=True))
                        return
            await asyncio.sleep(1)
        raise SystemExit("timed out waiting for requested durable recovery state")
    if phase == "postgres_outage":
        try:
            async with _connection(settings.database_url) as connection:
                await connection.execute("SELECT 1")
        except Exception:
            print(json.dumps({"phase": "postgres_outage", "state": "classified"}, sort_keys=True))
            return
        raise SystemExit("PostgreSQL outage probe unexpectedly connected")
    if phase == "nats_outage":
        deadline = asyncio.get_running_loop().time() + 30
        latest_snapshot: dict[str, object] | None = None
        while asyncio.get_running_loop().time() < deadline:
            async with _connection(settings.database_url) as connection:
                latest = await connection.execute(
                    "WITH latest_job AS ("
                    "SELECT job_id FROM observatory_evaluation_jobs "
                    "ORDER BY created_at DESC LIMIT 1), "
                    "latest_attempt AS ("
                    "SELECT a.state FROM observatory_evaluation_attempts a "
                    "JOIN latest_job j ON j.job_id=a.job_id "
                    "ORDER BY a.attempt_number DESC LIMIT 1), "
                    "latest_outbox AS ("
                    "SELECT o.event_type, o.published_at, o.publish_attempts, "
                    "o.last_error_code, o.next_publish_at, o.lease_token, "
                    "o.lease_expires_at FROM observatory_evaluation_outbox o "
                    "JOIN latest_job j ON j.job_id=o.job_id "
                    "ORDER BY o.created_at DESC LIMIT 1) "
                    "SELECT (SELECT state FROM observatory_evaluation_jobs "
                    "ORDER BY created_at DESC LIMIT 1), "
                    "(SELECT state FROM latest_attempt), "
                    "o.event_type, o.published_at IS NULL, o.publish_attempts, "
                    "o.last_error_code, o.next_publish_at <= CURRENT_TIMESTAMP, "
                    "o.lease_token IS NOT NULL, "
                    "o.lease_expires_at IS NOT NULL AND o.lease_expires_at <= CURRENT_TIMESTAMP "
                    "FROM latest_outbox o"
                )
                latest_row = await latest.fetchone()
                counts = await connection.execute(
                    "WITH latest_job AS ("
                    "SELECT job_id FROM observatory_evaluation_jobs "
                    "ORDER BY created_at DESC LIMIT 1) "
                    "SELECT count(*) FILTER (WHERE o.published_at IS NULL), "
                    "count(*) FILTER (WHERE o.published_at IS NULL "
                    "AND o.lease_token IS NOT NULL), "
                    "count(*) FILTER (WHERE o.published_at IS NOT NULL) "
                    "FROM observatory_evaluation_outbox o "
                    "JOIN latest_job j ON j.job_id=o.job_id"
                )
                count_row = await counts.fetchone()
                failed_metric = await connection.execute(
                    "SELECT value FROM observatory_evaluation_metric_series "
                    "WHERE metric_name='observatory_evaluation_outbox_publication_count' "
                    "AND label_outcome='failed' ORDER BY updated_at DESC LIMIT 1"
                )
                metric_row = await failed_metric.fetchone()
                metric_diagnostics = await connection.execute(
                    "SELECT label_failure_code, count(*) FROM observatory_evaluation_metric_series "
                    "WHERE label_failure_code <> '' GROUP BY label_failure_code "
                    "ORDER BY label_failure_code LIMIT 16"
                )
                diagnostic_rows = await metric_diagnostics.fetchall()
            safe_failure_code = None
            if latest_row is not None and latest_row[5] is not None:
                candidate = str(latest_row[5])
                safe_failure_code = candidate if candidate in {"transport_unavailable", "timeout", "database_failure"} else "other"
            diagnostic_codes = {
                (str(code) if str(code) in {"transport_unavailable", "timeout", "database_failure"} else "other"): int(count)
                for code, count in diagnostic_rows
            }
            latest_snapshot = {
                "job_state": latest_row[0] if latest_row is not None else None,
                "attempt_state": latest_row[1] if latest_row is not None else None,
                "outbox_event_type": latest_row[2] if latest_row is not None else None,
                "published_at_null": bool(latest_row[3]) if latest_row is not None else None,
                "publish_attempts": int(latest_row[4]) if latest_row is not None else None,
                "last_error_code": safe_failure_code,
                "next_publish_at_le_db_now": bool(latest_row[6]) if latest_row is not None else None,
                "lease_present": bool(latest_row[7]) if latest_row is not None else None,
                "lease_expired": bool(latest_row[8]) if latest_row is not None else None,
                "matching_pending_rows": int(count_row[0]) if count_row is not None else 0,
                "matching_leased_rows": int(count_row[1]) if count_row is not None else 0,
                "matching_published_rows": int(count_row[2]) if count_row is not None else 0,
                "failed_publication_metric": (
                    math.isfinite(float(metric_row[0])) and float(metric_row[0]) >= 1
                    if metric_row is not None else False
                ),
                "metric_failure_diagnostic_counts": diagnostic_codes,
            }
            if (
                latest_row is not None
                and bool(latest_row[3])
                and int(latest_row[4]) > 0
                and safe_failure_code == "transport_unavailable"
                and metric_row is not None
                and math.isfinite(float(metric_row[0]))
                and float(metric_row[0]) >= 1
            ):
                print(json.dumps({"phase": "nats_outage", "state": "bounded_failure", "snapshot": latest_snapshot}, sort_keys=True))
                return
            await asyncio.sleep(1)
        print(json.dumps({"phase": "nats_outage", "state": "unbounded_failure", "snapshot": latest_snapshot}, sort_keys=True))
        raise SystemExit("NATS outage did not produce a bounded pending failure")
    if phase == "trace_verify":
        deadline = asyncio.get_running_loop().time() + 90
        trace_only = os.environ.get("AURA_E2E_TRACE_ONLY") == "true"
        required_operations = (
            {"dispatch", "publish", "result_apply", "heartbeat"}
            if trace_only
            else {"dispatch", "publish", "result_apply", "recover", "schedule_materialize", "heartbeat"}
        )
        required_dependencies = {"postgresql", "evaluation_nats"}
        required_components = {
            "aura.observatory.evaluation_orchestration",
            "aura.observatory.evaluation_scheduler",
        }
        diagnostics: dict[str, object] = {
            "span_count": 0,
            "component_count": 0,
            "trace_count": 0,
            "operation_count": 0,
            "dependency_count": 0,
            "missing_operations": sorted(required_operations),
            "missing_dependencies": sorted(required_dependencies),
            "missing_components": sorted(required_components),
            "failure_span": False,
            "privacy_ok": False,
            "duplicate_span": False,
        }
        while asyncio.get_running_loop().time() < deadline:
            rows = await _clickhouse_rows(
                settings,
                "SELECT component_id, operation, dimensions, trace_id, span_id, "
                "parent_span_id, deployment_id, service_role, capture_policy "
                "FROM observations WHERE component_id IN "
                "('aura.observatory.evaluation_orchestration', "
                "'aura.observatory.evaluation_scheduler') AND kind='span' "
                # Keep a bounded but sufficiently wide verification window.
                # The Collector recovery phase can append more than 512
                # metadata-only spans, which otherwise evicts the earlier
                # transport-failure span required by this assertion.
                "ORDER BY observed_at DESC LIMIT 4096",
            )
            operations = {str(row.get("operation")) for row in rows}
            dimensions = [row.get("dimensions") if isinstance(row.get("dimensions"), dict) else {} for row in rows]
            dependencies = {str(item.get("dependency")) for item in dimensions if item.get("dependency")}
            component_ids = {str(row.get("component_id")) for row in rows}
            trace_ids = {str(row.get("trace_id")) for row in rows if row.get("trace_id")}
            span_ids = [str(row.get("span_id")) for row in rows if row.get("span_id")]
            privacy_ok = all(
                str(row.get("deployment_id")) == "aura0092-e2e"
                and str(row.get("capture_policy")) == "metadata_only"
                and str(row.get("service_role")) in {"worker", "scheduler"}
                and str(row.get("trace_id"))
                and str(row.get("span_id"))
                and not any(
                    token in json.dumps(row, sort_keys=True).casefold()
                    for token in ("prompt", "response", "content", "password", "token", "endpoint")
                )
                for row in rows
            )
            failure_ok = trace_only or any(
                str(row.get("operation")) == "publish"
                and str(dimensions[index].get("dependency")) == "evaluation_nats"
                and str(dimensions[index].get("outcome")) == "failure"
                and str(dimensions[index].get("errorClass")) == "transport_unavailable"
                for index, row in enumerate(rows)
            )
            diagnostics = {
                "span_count": len(rows),
                "component_count": len(component_ids),
                "trace_count": len(trace_ids),
                "operation_count": len(operations),
                "dependency_count": len(dependencies),
                "missing_operations": sorted(required_operations - operations),
                "missing_dependencies": sorted(required_dependencies - dependencies),
                "missing_components": sorted(required_components - component_ids),
                "failure_span": bool(failure_ok),
                "privacy_ok": bool(privacy_ok),
                "duplicate_span": len(span_ids) != len(set(span_ids)),
            }
            if (
                required_operations.issubset(operations)
                and required_dependencies.issubset(dependencies)
                and required_components.issubset(component_ids)
                and len(trace_ids) >= 2
                and len(span_ids) == len(set(span_ids))
                and privacy_ok
                and failure_ok
            ):
                print(json.dumps({
                    "phase": "trace_verify",
                    "state": "exported_persisted",
                    **diagnostics,
                }, sort_keys=True))
                return
            await asyncio.sleep(1)
        print(json.dumps({
            "phase": "trace_verify",
            "state": "failed",
            "diagnostics": diagnostics,
        }, sort_keys=True))
        raise SystemExit("component spans were not exported and persisted with required coverage")
    if phase in {"collector_outage_metrics", "collector_recovered_metrics"}:
        try:
            with urllib.request.urlopen("http://observatory-collector:8888/metrics", timeout=5) as response:
                metrics_text = response.read().decode("utf-8", "replace")
        except (OSError, urllib.error.URLError):
            raise SystemExit("Collector self-telemetry endpoint unavailable")

        # Scan the complete private self-metrics body before selecting the
        # bounded queue/retry series.  The probe must reject endpoint,
        # credential, content, owner, and lifecycle labels (including
        # server_address/server_port) without printing the matching body.
        sensitive_label_names = {
            "server_address", "server_port", "endpoint", "url", "uri",
            "secret", "secret_file", "token", "password", "filename",
            "file", "content", "prompt", "response", "owner", "identity",
            "lifecycle", "process_command", "service_instance_id",
        }
        sensitive_value = re.compile(
            r"https?://|nats://|postgresql://|/run/secrets/|"
            r"bearer|secret|password|token|prompt|response|content|"
            r"filename|owner|identity|lifecycle",
            re.IGNORECASE,
        )
        for label_name, label_value in re.findall(
            r'([A-Za-z_][A-Za-z0-9_.-]*)="([^"]*)"', metrics_text
        ):
            if label_name.casefold() in sensitive_label_names:
                raise SystemExit("Collector self-telemetry sensitive label name matched")
            if sensitive_value.search(label_value):
                raise SystemExit("Collector self-telemetry sensitive label value matched")
        if re.search(r"https?://|nats://|postgresql://|/run/secrets/", metrics_text, re.IGNORECASE):
            raise SystemExit("Collector self-telemetry URI value matched")

        def metric_value(prefix: str, *required_labels: str) -> float:
            for line in metrics_text.splitlines():
                if not line.startswith(prefix) or any(label not in line for label in required_labels):
                    continue
                try:
                    return float(line.rsplit(" ", 1)[1])
                except (IndexError, ValueError):
                    continue
            raise SystemExit("Collector self-telemetry metric missing")

        queue_size = metric_value(
            "otelcol_exporter_queue_size{",
            'data_type="traces"',
            'exporter="otlphttp/observatory"',
        )
        queue_capacity = metric_value(
            "otelcol_exporter_queue_capacity{",
            'data_type="traces"',
            'exporter="otlphttp/observatory"',
        )
        send_attempt_count = metric_value(
            "otelcol_exporter_queue_batch_send_size_count{",
            'exporter="otlphttp/observatory"',
        )
        if not (0 <= queue_size <= queue_capacity):
            raise SystemExit("Collector retry queue exceeded its configured bound")
        if phase == "collector_outage_metrics" and (queue_size < 1 or send_attempt_count < 1):
            raise SystemExit("Collector outage did not produce bounded retry evidence")
        if phase == "collector_recovered_metrics" and queue_size != 0:
            raise SystemExit("Collector retry queue did not drain after API recovery")
        print(json.dumps({
            "phase": phase,
            "queue_size": queue_size,
            "queue_capacity": queue_capacity,
            "send_attempt_count": send_attempt_count,
            "state": "bounded" if phase == "collector_outage_metrics" else "recovered",
        }, sort_keys=True))
        return
    if phase in {"heartbeat_fresh", "heartbeat_stale", "heartbeat_stale_seed", "heartbeat_recovered"}:
        async with _connection(settings.database_url) as connection:
            if phase == "heartbeat_stale_seed":
                await connection.execute(
                    "UPDATE observatory_evaluation_worker_leases SET heartbeat_at=NOW()-INTERVAL '5 minutes', "
                    "lease_expires_at=NOW()-INTERVAL '1 minute' WHERE component_id=%s",
                    ("aura.observatory.evaluation_orchestration",),
                )
                print(json.dumps({"phase": "heartbeat_stale_seed", "state": "aged"}, sort_keys=True))
                return
            heartbeat = await connection.execute(
                "SELECT heartbeat_at, lease_expires_at FROM observatory_evaluation_worker_leases "
                "WHERE component_id=%s",
                ("aura.observatory.evaluation_orchestration",),
            )
            row = await heartbeat.fetchone()
            if row is None:
                raise SystemExit("evaluation worker heartbeat missing")
            stale = row[1] <= datetime.now(UTC) or row[0] <= datetime.now(UTC) - timedelta(seconds=60)
            if phase in {"heartbeat_fresh", "heartbeat_recovered"} and stale:
                raise SystemExit("evaluation worker heartbeat remains stale")
            if phase == "heartbeat_stale" and not stale:
                raise SystemExit("evaluation worker heartbeat did not age")
            print(json.dumps({"phase": phase, "state": "verified"}, sort_keys=True))
            return
    if phase == "recovery":
        async with _connection(settings.database_url) as connection:
            pending = await connection.execute(
                "SELECT job_id FROM observatory_evaluation_jobs "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = await pending.fetchone()
        if row is None:
            raise SystemExit("no durable outage job available for recovery")
        current = await _wait_for_state(repository, str(row[0]), {"succeeded"})
        print(json.dumps({"phase": "outage_recovery", "state": current.state.value}, sort_keys=True))
        return
    now = datetime.now(UTC)
    deadline_seconds = 3 if phase in {"deadline", "deadline_admit"} else 120
    data = {
        "executionKind": "synthetic_end_to_end",
        "requestVersion": "1.0.0",
        "targetComponent": {
            "componentId": "aura.evaluation.synthetic",
            "componentVersion": "1.0.0",
            "codeRevision": "0000000",
            "imageDigest": "a" * 64,
            "configurationRevision": "local",
        },
        "captureAuthorization": {
            "captureClassification": "metadata_only",
            "authorizationPurpose": "synthetic_benchmark",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
        },
        "provenance": {
            "dataset": {"id": str(uuid4()), "version": "1.0.0"},
            "suite": {"id": str(uuid4()), "version": "1.0.0"},
            "evaluators": [{"evaluatorId": str(uuid4()), "evaluatorVersion": "1.0.0", "kind": "deterministic"}],
            "components": [],
            "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": "a" * 64},
            "deploymentRevision": "aura0092-e2e",
            "reproducibility": {"seed": 7, "clockMode": "fixed", "timeZone": "UTC", "locale": "en-US", "fixtureRevision": "synthetic-run-coordinator-v1", "promptRevision": "prompt-v1", "policyRevision": "policy-v1"},
        },
        "limits": {"execution": {"timeoutMs": 10000, "maxConcurrency": 1, "maxAttempts": 1, "maxCases": 1, "maxInputBytes": 0, "maxOutputBytes": 1024, "maxTokens": 0}, "maxQueueTimeMs": 10000, "maxModelCalls": 1, "maxArtifactOperations": 0},
        "deadlineAt": (now + timedelta(seconds=deadline_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "caseIds": [str(uuid4())],
        "inputArtifactReferences": [],
    }
    data["provenance"]["components"] = [data["targetComponent"]]
    request = EvaluationAdmission(str(uuid4()), str(uuid4()), "1.0.0", str(data["provenance"]["dataset"]["id"]), "1.0.0", str(data["provenance"]["suite"]["id"]), "1.0.0", str(data["provenance"]["evaluators"][0]["evaluatorId"]), "1.0.0", "metadata_only", now + timedelta(seconds=deadline_seconds), 1, {"data": data}, created_at=now)
    job = await service.admit(request, now=now)
    if phase in {"outage_admit", "redelivery_admit", "deadline_admit"}:
        if phase == "redelivery_admit":
            print(json.dumps({"phase": "redelivery_admitted", "state": job.state.value, "jobId": job.job_id}, sort_keys=True))
            return
        if phase == "deadline_admit":
            print(json.dumps({"phase": "deadline_admitted", "state": job.state.value}, sort_keys=True))
            return
        print(json.dumps({"phase": "outage_admitted", "state": job.state.value}, sort_keys=True))
        return
    if phase == "schedule_seed":
        async with _connection(settings.database_url) as connection:
            schedule_id = str(uuid4())
            occurrence_id = str(uuid4())
            request_data = dict(data)
            request_data["limits"] = dict(request_data["limits"])
            request_data["limits"]["execution"] = dict(request_data["limits"]["execution"])
            request_data["limits"]["execution"]["maxAttempts"] = 1
            schedule_data = json.dumps({"data": request_data}, sort_keys=True)
            await connection.execute(
                "INSERT INTO observatory_evaluation_schedules("
                "schedule_id,occurrence_id,dataset_id,dataset_version,suite_id,suite_version,"
                "evaluator_id,evaluator_version,request_envelope_json,next_run_at,cadence_seconds,"
                "deadline_seconds,max_attempts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
                "NOW()+INTERVAL '2 seconds',60,60,1)",
                (
                    schedule_id, occurrence_id, request.dataset_id, request.dataset_version,
                    request.suite_id, request.suite_version, request.evaluator_id,
                    request.evaluator_version, schedule_data,
                ),
            )
        print(json.dumps({"phase": "schedule_seed", "state": "queued"}, sort_keys=True))
        return
    if phase == "schedule_expire":
        async with _connection(settings.database_url) as connection:
            await connection.execute(
                "UPDATE observatory_evaluation_schedules SET next_run_at=NOW(), "
                "lease_token=%s, lease_expires_at=NOW()-INTERVAL '1 second' "
                "WHERE schedule_id=(SELECT schedule_id FROM observatory_evaluation_schedules "
                "ORDER BY updated_at DESC LIMIT 1)",
                (str(uuid4()),),
            )
        print(json.dumps({"phase": "schedule_expire", "state": "aged"}, sort_keys=True))
        return
    if phase == "cancel":
        if not await service.cancel(job.job_id, now=now):
            raise SystemExit("cancellation was not admitted")
        print(json.dumps({"phase": "cancelled", "state": "cancelled"}, sort_keys=True))
        return
    print(json.dumps({"phase": "admitted", "state": job.state.value}, sort_keys=True))
    deadline = asyncio.get_running_loop().time() + (20 if phase == "deadline" else 90)
    while asyncio.get_running_loop().time() < deadline:
        current = await repository.get_job(job.job_id)
        if current is not None and current.state.value in {"succeeded", "failed", "cancelled", "deadline"}:
            expected = {"deadline", "succeeded"} if phase == "deadline" else {"succeeded"}
            if current.state.value not in expected:
                raise SystemExit(f"unexpected terminal state: {current.state.value}")
            print(json.dumps({"phase": "durable_result", "state": current.state.value}, sort_keys=True))
            break
        await asyncio.sleep(1)
    else:
        raise SystemExit("timed out waiting for durable runner result")


@asynccontextmanager
async def _connection(url: str):
    connection = await psycopg.AsyncConnection.connect(url)
    try:
        yield connection
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise
    finally:
        await connection.close()


asyncio.run(main())
PY

run_probe() {
  local phase=$1
  local output status service container_id
  local -a phase_env=(-e "AURA_E2E_PHASE=$phase")
  if [ -n "${AURA_E2E_REDELIVERY_JOB_ID:-}" ]; then
    phase_env+=( -e "AURA_E2E_REDELIVERY_JOB_ID=$AURA_E2E_REDELIVERY_JOB_ID" )
  fi
  if [ "${AURA_E2E_TRACE_ONLY:-false}" = true ]; then
    phase_env+=( -e AURA_E2E_TRACE_ONLY=true )
  fi
  if output=$(bounded "$step_timeout" "${compose[@]}" run --rm --no-deps -T \
    "${phase_env[@]}" \
    -e AURA_OBSERVATORY_CLICKHOUSE_URL=http://observatory-clickhouse:8123 \
    -e AURA_OBSERVATORY_CLICKHOUSE_DATABASE=aura_observatory \
    -e AURA_OBSERVATORY_CLICKHOUSE_USER=observatory \
    -e AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE=/run/secrets/observatory-clickhouse-password \
    --volume "$secret_root/observatory-clickhouse-password:/run/secrets/observatory-clickhouse-password:ro" \
    observatory-evaluation-scheduler \
    /app/.venv/bin/python - <"$probe_script" 2>&1); then
    printf '%s\n' "$output"
    return 0
  else
    status=$?
    bounded_reason=probe_failure
    if printf '%s' "$output" | rg -qi 'Collector retry queue did not drain'; then
      bounded_reason=collector_recovery_timeout
    elif printf '%s' "$output" | rg -qi 'Collector self-telemetry metric missing'; then
      bounded_reason=collector_metric_missing
    elif printf '%s' "$output" | rg -qi 'Collector self-telemetry sensitive label name matched'; then
      bounded_reason=collector_metric_sensitive_label_name
    elif printf '%s' "$output" | rg -qi 'Collector self-telemetry sensitive label value matched'; then
      bounded_reason=collector_metric_sensitive_label_value
    elif printf '%s' "$output" | rg -qi 'Collector self-telemetry URI value matched'; then
      bounded_reason=collector_metric_uri_value
    elif printf '%s' "$output" | rg -qi 'Collector self-telemetry endpoint unavailable'; then
      bounded_reason=collector_metric_unavailable
    elif printf '%s' "$output" | rg -qi 'Collector retry queue exceeded its configured bound'; then
      bounded_reason=collector_queue_bound_exceeded
    elif printf '%s' "$output" | rg -qi 'Collector outage did not produce bounded retry evidence'; then
      bounded_reason=collector_outage_evidence_missing
    fi
    printf 'AURA_E2E phase=%s bounded_failure=%s exit=%s\n' \
      "$phase" "$bounded_reason" "$status" >&2
    printf 'AURA_E2E phase=%s service-state\n' "$phase" >&2
    for service in observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner; do
      container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
      if [ -n "$container_id" ]; then
        docker inspect --format "$service status={{.State.Status}} exit={{.State.ExitCode}} restart={{.RestartCount}} oom={{.State.OOMKilled}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" "$container_id" >&2
        categories=$(docker logs --tail 80 "$container_id" 2>&1 |
          classify_diagnostic_stream) || categories=categories=none
        printf 'AURA_E2E service=%s %s\n' "$service" "$categories" >&2
      fi
    done
    return "$status"
  fi
}
run_dispatch_probe() {
  local phase=$1
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps -T \
    -e "AURA_E2E_PHASE=$phase" \
    -e "AURA_E2E_REDELIVERY_JOB_ID=${AURA_E2E_REDELIVERY_JOB_ID:?}" \
    -e AURA_OBSERVATORY_EVALUATION_NATS_USER=aura-evaluation-dispatch \
    -e AURA_OBSERVATORY_EVALUATION_NATS_PASSWORD_FILE=/run/secrets/aura-nats-dispatch-password \
    --volume "$secret_root/aura-nats-dispatch-password:/run/secrets/aura-nats-dispatch-password:ro" \
    observatory-evaluation-worker /app/.venv/bin/python - <"$probe_script"
}

run_probe round_trip

if [ "${AURA_E2E_TRACE_ONLY:-false}" = true ]; then
  sleep 5
  run_probe trace_verify
  printf '%s\n' '{"phase":"trace_only","state":"passed"}'
  exit 0
fi

# A worker restart must preserve the service-owned database state and resume
# the same real runner boundary.
bounded "$step_timeout" "${compose[@]}" restart observatory-evaluation-worker
bounded "$start_timeout" "${compose[@]}" up --detach \
  observatory-api observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
wait_running observatory-api observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
run_probe restart_recovery
run_probe cancel

# Prove a deadline terminal transition by keeping dispatch services stopped
# beyond the admitted deadline; a fast synthetic runner must not turn this into
# a successful result race.
bounded "$step_timeout" "${compose[@]}" stop observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
run_probe deadline_admit
sleep 5
bounded "$start_timeout" "${compose[@]}" start observatory-evaluation-worker observatory-evaluation-scheduler
wait_running observatory-evaluation-worker observatory-evaluation-scheduler
run_probe deadline_verify
bounded "$start_timeout" "${compose[@]}" start aura-evaluation-runner
wait_running aura-evaluation-runner
run_probe duplicate
run_probe health_privacy

# Two scheduler replicas must materialize one due occurrence.  An explicitly
# expired schedule lease is then recovered after a scheduler restart.
assert_scheduler_scale_prerequisites
bounded "$start_timeout" "${compose[@]}" up --no-deps --detach --scale observatory-evaluation-scheduler=2 \
  observatory-evaluation-scheduler
wait_running observatory-evaluation-scheduler
run_probe schedule_seed
run_probe schedule_verify
bounded "$step_timeout" "${compose[@]}" stop observatory-evaluation-scheduler
assert_runner_remains_available
run_probe schedule_expire
bounded "$start_timeout" "${compose[@]}" start observatory-evaluation-scheduler
wait_running observatory-evaluation-scheduler
assert_runner_remains_available
run_probe schedule_recovery

# Verify persisted heartbeat freshness, age, and recovery without waiting for
# the production two-minute lease horizon.
run_probe heartbeat_fresh
bounded "$step_timeout" "${compose[@]}" stop observatory-evaluation-worker
assert_runner_remains_available
run_probe heartbeat_stale_seed
run_probe heartbeat_stale
bounded "$start_timeout" "${compose[@]}" start observatory-evaluation-worker
wait_running observatory-evaluation-worker
assert_runner_remains_available
run_probe heartbeat_recovered

# Exercise real JetStream ack-failure/redelivery on the Observatory result
# durable. The one-shot dispatch consumer exits without acking; the real
# Observatory worker must receive the redelivery and persist one receipt.
bounded "$step_timeout" "${compose[@]}" stop observatory-evaluation-worker observatory-evaluation-scheduler
redelivery_admit_output=$(run_probe redelivery_admit)
AURA_E2E_REDELIVERY_JOB_ID=$(printf '%s\n' "$redelivery_admit_output" |
  sed -n 's/.*"jobId": *"\([0-9a-f-]\{36\}\)".*/\1/p' | tail -n 1)
if [ -z "$AURA_E2E_REDELIVERY_JOB_ID" ]; then
  printf '%s\n' 'AURA_E2E redelivery admission did not return a bounded job correlation' >&2
  exit 1
fi
export AURA_E2E_REDELIVERY_JOB_ID
printf '%s\n' '{"phase":"redelivery_admitted","state":"queued"}'
run_dispatch_probe jetstream_redelivery
bounded "$start_timeout" "${compose[@]}" start observatory-evaluation-worker observatory-evaluation-scheduler
wait_running observatory-evaluation-worker observatory-evaluation-scheduler
run_probe redelivery_verify

# A broker outage must leave the worker alive with a bounded pending publish and
# finite failure metric. Restart only the broker and recover the queued job.
bounded "$start_timeout" "${compose[@]}" up --detach observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
wait_running observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
bounded "$step_timeout" "${compose[@]}" stop aura-nats
run_probe outage_admit
run_probe nats_outage
for service in observatory-evaluation-worker observatory-evaluation-scheduler; do
  if ! wait_running "$service"; then
    printf 'AURA_E2E %s stopped during NATS outage\n' "$service" >&2
    exit 1
  fi
done
assert_runner_remains_available
bounded "$start_timeout" "${compose[@]}" start aura-nats
bounded "$step_timeout" "${compose[@]}" restart observatory-evaluation-worker
bounded "$start_timeout" "${compose[@]}" up --detach \
  observatory-api observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
wait_running observatory-api observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
run_probe recovery
run_probe dependency_metrics

# PostgreSQL failure is classified at the repository boundary and recovery
# restores the worker lease without touching Core state.
bounded "$step_timeout" "${compose[@]}" stop observatory-postgres
run_probe postgres_outage
for service in observatory-evaluation-worker observatory-evaluation-scheduler; do
  if ! wait_running "$service"; then
    printf 'AURA_E2E %s stopped during PostgreSQL outage\n' "$service" >&2
    exit 1
  fi
  container_id=$("${compose[@]}" ps -q "$service" | head -n 1)
  categories=$(docker logs --tail 100 "$container_id" 2>&1 |
    classify_diagnostic_stream) || categories=categories=none
  if [[ "$categories" == *exception:* ]]; then
    printf 'AURA_E2E bounded exception category found during PostgreSQL outage\n' >&2
    exit 1
  fi
done
assert_runner_remains_available
bounded "$start_timeout" "${compose[@]}" start observatory-postgres
wait_running observatory-postgres
wait_healthy observatory-postgres
bounded "$step_timeout" "${compose[@]}" restart observatory-evaluation-worker
bounded "$start_timeout" "${compose[@]}" up --detach observatory-evaluation-worker observatory-evaluation-scheduler
wait_running observatory-evaluation-worker observatory-evaluation-scheduler
run_probe heartbeat_recovered

# Observatory evaluation services may stop without taking the Core runner down.
bounded "$step_timeout" "${compose[@]}" stop observatory-evaluation-worker observatory-evaluation-scheduler
wait_running aura-evaluation-runner
bounded "$start_timeout" "${compose[@]}" up --detach aura-core-api aura-core-worker
wait_running aura-core-api aura-core-worker
wait_healthy aura-core-api
core_api_container=$("${compose[@]}" ps -q aura-core-api | head -n 1)
docker exec "$core_api_container" python - <<'PY'
import asyncio
import json
import urllib.request

import redis.asyncio
from aura_core.domains.governance.identity.models import Principal
from aura_core.platform.auth import RedisSessionBackend, SessionService, Settings

for path, expected in (("/health/live", 200), ("/health/ready", 200)):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=3) as response:
        if response.status != expected:
            raise SystemExit("Core readiness request failed")


async def authenticated_core_operation() -> None:
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(settings.valkey_url, decode_responses=True)
    sessions = SessionService(RedisSessionBackend(redis_client), settings)
    session_id, _ = await sessions.create(
        Principal(settings.oidc_issuer, settings.owner_subject, "Local owner")
    )
    try:
        cookie = f"{settings.session_cookie.name}={session_id}"
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/auth/session",
            headers={"Cookie": cookie, "Origin": settings.public_origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise SystemExit("Core authenticated session request failed")
        request = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/models",
            headers={"Cookie": cookie, "Origin": settings.public_origin},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200 or "models" not in json.loads(response.read()):
                raise SystemExit("Core authenticated model operation failed")
    finally:
        await sessions.revoke(session_id)
        await redis_client.aclose()


asyncio.run(authenticated_core_operation())
PY
if [ "$(docker inspect -f '{{.State.Running}}' $("${compose[@]}" ps -q aura-core-worker | head -n 1))" != true ]; then
  printf '%s\n' 'Core worker stopped while Observatory was stopped' >&2
  exit 1
fi
bounded "$start_timeout" "${compose[@]}" start observatory-evaluation-worker observatory-evaluation-scheduler

# Verify that the bounded OTLP exporter crossed the Collector boundary and
# the resulting metadata-only spans are queryable from Observatory ClickHouse.
run_probe trace_verify

# Exercise the real Collector-to-API failure path.  The pinned Collector keeps
# actionable warnings/errors while its info diagnostics are reduced at the
# service boundary; this must not turn an API outage into a URL, endpoint, or
# secret-path log sink.  The subsequent trace verification proves that the
# Collector's configured queue/retry path recovered after the API returned.
bounded "$step_timeout" "${compose[@]}" stop observatory-api
run_probe heartbeat_fresh
sleep 3
run_probe collector_outage_metrics
collector_outage_logs=$("${compose[@]}" logs --no-color observatory-collector 2>&1 || true)
if printf '%s' "$collector_outage_logs" | rg -q -i \
  'https?://|nats://|postgresql://|/run/secrets/[A-Za-z0-9_.-]+|"(endpoint|filename|secret|password|token|url|uri)"[[:=]]'; then
  printf '%s\n' 'AURA_E2E Collector outage diagnostics leaked an endpoint or secret path' >&2
  exit 1
fi
bounded "$start_timeout" "${compose[@]}" start observatory-api
wait_running observatory-api
wait_healthy observatory-api
collector_recovery_deadline=$((SECONDS + step_timeout))
collector_recovered=0
while (( SECONDS < collector_recovery_deadline )); do
  if recovery_output=$(run_probe collector_recovered_metrics 2>/dev/null); then
    printf '%s\n' "$recovery_output"
    collector_recovered=1
    break
  fi
  sleep 2
done
if (( collector_recovered == 0 )); then
  printf '%s\n' 'AURA_E2E Collector retry queue did not drain within the bounded recovery window' >&2
  exit 1
fi
run_probe trace_verify

metrics_container=$("${compose[@]}" ps -q observatory-api | head -n 1)
docker exec "$metrics_container" python - <<'PY'
import re
import urllib.request

body = urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5).read().decode()
expected_types = {
    "observatory_evaluation_job_admission_count": "counter",
    "observatory_evaluation_outbox_pending_count": "gauge",
    "observatory_evaluation_worker_heartbeat_age_seconds": "gauge",
    "observatory_evaluation_scheduler_heartbeat_age_seconds": "gauge",
    "observatory_evaluation_scheduler_occurrence_count": "counter",
    "observatory_evaluation_operation_duration_ms": "histogram",
}
for name, metric_type in expected_types.items():
    if f"# TYPE {name} {metric_type}" not in body:
        raise SystemExit("persisted evaluation metric type mismatch")
histogram_count = re.search(
    r"^observatory_evaluation_operation_duration_ms_count(?:\{[^}]*\})? ([0-9]+(?:\.[0-9]+)?)$",
    body,
    re.MULTILINE,
)
if histogram_count is None or float(histogram_count.group(1)) < 1:
    raise SystemExit("persisted histogram count missing")
for bucket in (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000):
    if not re.search(
        rf"^observatory_evaluation_operation_duration_ms_bucket\{{[^}}]*le=\"{bucket}\"[^}}]*\}} ",
        body,
        re.MULTILINE,
    ):
        raise SystemExit("persisted histogram bucket missing")
if any(
    token in body.casefold()
    for token in (
        "prompt", "response", "content", "filename", "credentials", "bearer",
        "endpoint", "owner_identity", "provider_diagnostic", "traceback", "stacktrace",
    )
):
    raise SystemExit("private field found in metrics scrape")
PY

for service in observatory-collector observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner observatory-api; do
  if [ "$service" = "observatory-api" ]; then
    log_output=$("${compose[@]}" logs --no-color --no-log-prefix "$service" 2>&1)
    log_output=$(printf '%s\n' "$log_output" | sed -E \
      '/^INFO:[[:space:]]+Uvicorn running on http:\/\/0\.0\.0\.0:8000 \(Press CTRL\+C to quit\)$/d')
  else
    log_output=$("${compose[@]}" logs --no-color "$service" 2>&1)
  fi
  if printf '%s' "$log_output" | rg -q -i \
    'Traceback|^[[:space:]]*File "|provider[_-]?(diagnostic|error)|https?://|nats://|postgresql://|/run/secrets/[A-Za-z0-9_.-]+|"(prompt|response|content|filename|tool|argument|credentials?|endpoint|bearer|owner|identity|secret|password|token|url|uri)"[[:=]]'; then
    categories=$(printf '%s' "$log_output" | rg -o -i \
      'Traceback|^[[:space:]]*File "|provider[_-]?(diagnostic|error)|https?://|nats://|postgresql://|/run/secrets/[A-Za-z0-9_.-]+|"(prompt|response|content|filename|tool|argument|credentials?|endpoint|bearer|owner|identity|secret|password|token|url|uri)"[[:=]]' \
      | sed -E 's/^[[:space:]]*File "[^"]*".*/file-trace/; s/"[^ ]+"[[:space:]]*[:=].*/field/; s#https?://.*#endpoint#; s#nats://.*#broker-endpoint#; s#postgresql://.*#database-endpoint#; s#/run/secrets/[^[:space:]]*#secret-file#; s/provider.*/provider-diagnostic/; s/Traceback.*/traceback/' \
      | sort -u | paste -sd, -)
    printf 'AURA_E2E privacy or raw-diagnostic scan failed service=%s categories=%s\n' "$service" "${categories:-unknown}" >&2
    exit 1
  fi
done
printf '%s\n' 'AURA_E2E AURA-0092 orchestration reliability and isolation passed'
