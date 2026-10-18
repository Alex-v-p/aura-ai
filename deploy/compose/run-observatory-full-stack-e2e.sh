#!/usr/bin/env bash
set -Eeuo pipefail

# Opt-in, disposable acceptance runner for the complete Core -> Collector ->
# Observatory -> browser path.  It creates fixture-only credentials and state,
# and removes them on exit.  It intentionally does not read or modify .env or
# any operator deployment secret.
# Set AURA_OBSERVATORY_E2E_KEEP_ON_FAILURE=1 to retain only a failed,
# disposable project and its mode-0600 fixture directory for bounded
# diagnostics. The default remains cleanup on every exit.

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# Compose v5 can issue duplicate concurrent create requests for the same fixed
# networks and volumes during a large `up --build`. Serialize Compose's
# resource lifecycle for this disposable, low-memory acceptance stack so a
# partially-created network cannot race its own IPAM allocation.
export COMPOSE_PARALLEL_LIMIT=1
# Keep credential-bearing Compose env/override artifacts in a repository-local
# ignored fixture directory so Docker Desktop can bind-mount the secret source
# files. The exit trap removes the directory and all of its contents.
fixture_root=$(mktemp -d "$ROOT/.aura-e2e.XXXXXX")
suffix=$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')
project="aura-observatory-e2e-$suffix"
override="$fixture_root/compose.override.yaml"
env_file="$fixture_root/.env"
secret_root="$fixture_root/secrets"
nginx_config="$fixture_root/observatory-web.nginx.conf"
browser_state="$fixture_root/observatory-browser-state.json"
cleanup_started=0
keep_on_failure=${AURA_OBSERVATORY_E2E_KEEP_ON_FAILURE:-0}

remove_disposable_networks() {
  network_rows=$(docker network ls --filter "label=com.docker.compose.project=$project" \
    --format '{{.ID}} {{.Name}}' 2>/dev/null || true)
  while read -r network_id network_name; do
    [ -n "$network_id" ] || continue
    case "$network_name" in
      "${project}_"*)
        # Compose can leave a project network behind when startup fails while
        # reconciling services. Remove only this disposable project's networks.
        docker network rm "$network_id" >/dev/null 2>&1 || true
        ;;
    esac
  done <<<"$network_rows"
}

cleanup() {
  status=$?
  if [ "$cleanup_started" -eq 1 ]; then
    trap - EXIT INT TERM
    exit "$status"
  fi
  cleanup_started=1
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ]; then
    if [ -f "$env_file" ] && [ -f "$override" ]; then
      docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override" \
        logs --no-color --tail=100 \
        observatory-postgres observatory-migrate observatory-clickhouse observatory-session \
        observatory-garage-bootstrap observatory-api observatory-worker observatory-collector observatory-web \
        aura-core-postgres aura-core-migrate aura-core-api aura-core-worker aura-core-scheduler aura-web \
        2>&1 || true
      # The deterministic Ollama fixture is a separate Compose project and
      # therefore is not included in the main project's service log request.
      # Capture its bounded output before provider teardown so a Core
      # PROVIDER_ERROR distinguishes provider startup/protocol failures from
      # worker-side failures without exposing request bodies or credentials.
      docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
        logs --no-color --tail=100 aura-e2e-ollama 2>&1 || true
    fi
  fi
  if [ "$status" -ne 0 ] && [ "$keep_on_failure" = "1" ]; then
    printf '%s\n' "Keeping failed disposable Observatory project $project and fixture directory $fixture_root (AURA_OBSERVATORY_E2E_KEEP_ON_FAILURE=1)." >&2
    exit "$status"
  fi
  if [ -f "$env_file" ] && [ -f "$override" ]; then
    docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override" \
      --profile observatory --profile checks down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  remove_disposable_networks
  docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
    down --remove-orphans >/dev/null 2>&1 || true
  if [ -d "$fixture_root" ]; then
    rm -rf "$fixture_root"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

umask 077

# Docker Desktop must be allowed to share the repository path, while the
# fixture must never enter Git or a Docker build context. Fail closed before
# writing any secret source file if the repository ignore policy is missing.
assert_fixture_ignored() {
  if ! grep -Fqx '.aura-e2e.*/' "$ROOT/.gitignore"; then
    printf '%s\n' 'missing .aura-e2e.*/ rule in .gitignore' >&2
    exit 1
  fi
  if ! grep -Fqx '**/.aura-e2e.*/**' "$ROOT/.dockerignore"; then
    printf '%s\n' 'missing **/.aura-e2e.*/** rule in .dockerignore' >&2
    exit 1
  fi
}
assert_fixture_ignored

# Reuse the tracked edge configuration and change only its listener port for
# this network-only fixture. The generated file is non-secret and readable by
# the unprivileged nginx container; the source tree remains untouched.
sed 's/^        listen 8080;$/        listen 4300;/' \
  "$ROOT/deploy/docker/observatory-web.nginx.conf" > "$nginx_config"
chmod 0644 "$nginx_config"
test "$(grep -Fxc '        listen 4300;' "$nginx_config")" -eq 1
test "$(grep -Fxc '        listen 8080;' "$nginx_config")" -eq 0
if ! cmp -s \
  <(sed 's/^        listen 4300;$/        listen 8080;/' "$nginx_config") \
  "$ROOT/deploy/docker/observatory-web.nginx.conf"; then
  printf '%s\n' 'generated Observatory nginx fixture differs beyond its listener port' >&2
  exit 1
fi

# The browser lifecycle phases exchange only opaque cookie state through this
# ignored, mode-0600 fixture file. It is created before Compose interpolation
# so the first browser phase can atomically replace the file rather than
# causing Docker to bind-mount an implicit directory.
: > "$browser_state"
chmod 0600 "$browser_state"

# Compose's file-backed secrets are bind-mounted into every consuming
# container. Keep the disposable values in the ignored fixture directory and
# pass only their paths through interpolation; environment-backed Compose
# secrets are not supported for read-only services. The explicit mode also
# protects the files if the host's mktemp defaults change.
mkdir "$secret_root"
write_fixture_secret() {
  local name=$1
  local value=$2
  printf '%s\n' "$value" > "$secret_root/$name"
  chmod 0600 "$secret_root/$name"
}
write_fixture_secret aura-core-database-password fixture-core-database-password
write_fixture_secret aura-oidc-client-secret fixture-oidc-client-secret
write_fixture_secret aura-nats-password fixture-nats-password
write_fixture_secret aura-valkey-password fixture-valkey-password
write_fixture_secret aura-garage-rpc-secret 0000000000000000000000000000000000000000000000000000000000000000
write_fixture_secret aura-garage-access-key GK000000000000000000000000
write_fixture_secret aura-garage-secret-key 1111111111111111111111111111111111111111111111111111111111111111
write_fixture_secret observatory-garage-access-key GK111111111111111111111111
write_fixture_secret observatory-garage-secret-key 2222222222222222222222222222222222222222222222222222222222222222
write_fixture_secret observatory-database-password fixture-observatory-database-password
write_fixture_secret observatory-oidc-client-secret fixture-observatory-oidc-client-secret
write_fixture_secret observatory-session-password fixture-observatory-session-password
write_fixture_secret observatory-ingest-credential fixture-observatory-ingest-credential
write_fixture_secret observatory-clickhouse-password fixture-observatory-clickhouse-password

# Create the external fixture provider first so its disposable network is
# included in the IPAM inventory below. The provider remains outside the Aura
# Compose project and is removed by the exit trap.
docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
  up --build --detach --wait

# The normal private-network defaults are intentionally stable for operators,
# but an acceptance run must coexist with an already-running Aura stack. Pick
# two free /28s from the dedicated E2E pool by inspecting Docker's current
# network IPAM; no existing network is modified or removed. Staying within
# this reserved range avoids Docker's default-prone 172.30/31 pools.
mapfile -t network_id_array < <(docker network ls --quiet)
if [ "${#network_id_array[@]}" -gt 0 ]; then
  docker network inspect "${network_id_array[@]}" > "$fixture_root/docker-networks.json"
else
  printf '%s\n' '[]' > "$fixture_root/docker-networks.json"
fi
read -r object_storage_subnet document_parser_subnet observatory_object_storage_subnet < <(
  python3 - "$fixture_root/docker-networks.json" "$suffix" <<'PY'
import ipaddress
import json
import sys

network_path, suffix = sys.argv[1:]
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

free = []
for candidate in ipaddress.ip_network("10.254.192.0/18").subnets(new_prefix=28):
    if all(not candidate.overlaps(existing) for existing in occupied):
        free.append(candidate)

if len(free) < 3:
    raise SystemExit("no three free /28 subnets remain in the dedicated E2E pool")

start = int(suffix, 36) % len(free)
selected = [free[(start + offset) % len(free)] for offset in range(3)]
print(selected[0], selected[1], selected[2])
PY
)
test -n "$object_storage_subnet"
test -n "$document_parser_subnet"
test -n "$observatory_object_storage_subnet"

cat > "$env_file" <<EOF
AURA_STATE_ROOT=/unused/aura-observatory-e2e-state
AURA_CORE_DATABASE_PASSWORD_FILE=/run/secrets/aura-core-database-password
AURA_OIDC_CLIENT_SECRET_FILE=/run/secrets/aura-oidc-client-secret
AURA_NATS_PASSWORD_FILE=/run/secrets/aura-nats-password
AURA_VALKEY_PASSWORD_FILE=/run/secrets/aura-valkey-password
AURA_GARAGE_RPC_SECRET_FILE=/run/secrets/aura-garage-rpc-secret
AURA_GARAGE_ACCESS_KEY_FILE=/run/secrets/aura-garage-access-key
AURA_GARAGE_SECRET_KEY_FILE=/run/secrets/aura-garage-secret-key
AURA_OBSERVATORY_GARAGE_BUCKET=aura-observatory-evaluation
AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE=/run/secrets/observatory-garage-access-key
AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE=/run/secrets/observatory-garage-secret-key
AURA_E2E_CORE_DATABASE_SECRET_SOURCE_FILE=$secret_root/aura-core-database-password
AURA_E2E_OIDC_CLIENT_SECRET_SOURCE_FILE=$secret_root/aura-oidc-client-secret
AURA_E2E_NATS_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-password
AURA_E2E_VALKEY_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-valkey-password
AURA_E2E_GARAGE_RPC_SECRET_SOURCE_FILE=$secret_root/aura-garage-rpc-secret
AURA_E2E_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-access-key
AURA_E2E_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-secret-key
AURA_E2E_OBSERVATORY_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-garage-access-key
AURA_E2E_OBSERVATORY_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-garage-secret-key
AURA_OIDC_ISSUER=http://aura-e2e-oidc:8081
AURA_OIDC_AUDIENCE=aura-web
AURA_OIDC_CLIENT_ID=aura-web
AURA_OIDC_REDIRECT_URI=http://aura-web:8080/api/v1/auth/callback
AURA_OWNER_SUBJECT=owner
AURA_PUBLIC_ORIGIN=http://aura-web:8080
AURA_TELEMETRY_OTLP_ENABLED=true
AURA_DEFAULT_MODEL=fixture-chat-2
AURA_OBSERVATORY_ENVIRONMENT=disposable_local
AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE=/run/secrets/observatory-oidc-client-secret
AURA_OBSERVATORY_SESSION_PASSWORD_FILE=/run/secrets/observatory-session-password
AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE=/run/secrets/observatory-ingest-credential
AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE=/run/secrets/observatory-clickhouse-password
AURA_OBSERVATORY_DATABASE_PASSWORD_FILE=/run/secrets/observatory-database-password
AURA_OBSERVATORY_EVALUATION_OBJECT_ENDPOINT=http://aura-garage:3900
AURA_OBSERVATORY_EVALUATION_OBJECT_REGION=garage
AURA_OBSERVATORY_EVALUATION_OBJECT_BUCKET=aura-observatory-evaluation
AURA_OBSERVATORY_EVALUATION_OBJECT_ACCESS_KEY_FILE=/run/secrets/observatory-garage-access-key
AURA_OBSERVATORY_EVALUATION_OBJECT_SECRET_KEY_FILE=/run/secrets/observatory-garage-secret-key
AURA_E2E_OBSERVATORY_DATABASE_PASSWORD_SECRET_SOURCE_FILE=$secret_root/observatory-database-password
AURA_E2E_OBSERVATORY_OIDC_CLIENT_SECRET_SOURCE_FILE=$secret_root/observatory-oidc-client-secret
AURA_E2E_OBSERVATORY_SESSION_PASSWORD_SECRET_SOURCE_FILE=$secret_root/observatory-session-password
AURA_E2E_OBSERVATORY_INGEST_CREDENTIAL_SECRET_SOURCE_FILE=$secret_root/observatory-ingest-credential
AURA_E2E_OBSERVATORY_CLICKHOUSE_PASSWORD_SECRET_SOURCE_FILE=$secret_root/observatory-clickhouse-password
AURA_E2E_OBSERVATORY_NGINX_CONFIG_FILE=$nginx_config
AURA_E2E_OBSERVATORY_BROWSER_STATE_FILE=$browser_state
AURA_OBSERVATORY_PUBLIC_ORIGIN=http://observatory.localhost:4300
AURA_OBSERVATORY_OIDC_ISSUER=http://aura-e2e-oidc:8081
AURA_OBSERVATORY_OIDC_AUDIENCE=aura-observatory
AURA_OBSERVATORY_OIDC_CLIENT_ID=aura-observatory
AURA_OBSERVATORY_OIDC_REDIRECT_URI=http://observatory.localhost:4300/api/v1/auth/callback
AURA_OBSERVATORY_OWNER_SUBJECT=owner
AURA_OBSERVATORY_SECURE_COOKIES=false
AURA_OBJECT_STORAGE_SUBNET=$object_storage_subnet
AURA_DOCUMENT_PARSER_SUBNET=$document_parser_subnet
AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET=$observatory_object_storage_subnet
EOF

# The provider and browser runner are separate Compose concerns.  The temporary
# override bridges only the test containers to the provider and reuses the
# existing Playwright image; it does not alter the tracked production graph.
cat > "$override" <<'EOF'
services:
  aura-web:
    ports: !override []
  aura-core-api:
    environment:
      AURA_ENVIRONMENT: test
      AURA_SECURE_COOKIES: "false"
      AURA_OLLAMA_URL: http://aura-e2e-ollama:8081
    networks:
      - aura-frontend
      - aura-core-db
      - aura-core-bus
      - aura-core-cache
      - aura-object-storage
      - aura-core-egress
      - observatory-ingest
      - aura-e2e-provider
  aura-core-worker:
    environment:
      AURA_ENVIRONMENT: test
      AURA_OLLAMA_URL: http://aura-e2e-ollama:8081
    networks:
      - aura-core-db
      - aura-core-bus
      - aura-core-cache
      - aura-object-storage
      - aura-document-parser
      - aura-core-egress
      - observatory-ingest
      - aura-e2e-provider
  observatory-api:
    networks:
      - observatory-api
      - observatory-db
      - observatory-session
      - observatory-ingest
      - observatory-identity-egress
      - aura-e2e-provider
  observatory-web:
    ports: !override []
    volumes:
      - ${AURA_E2E_OBSERVATORY_NGINX_CONFIG_FILE}:/etc/nginx/nginx.conf:ro
    networks:
      observatory-edge:
        aliases:
          - observatory.localhost
      observatory-api: {}
    healthcheck:
      test: [CMD-SHELL, "wget --quiet --output-document=- http://127.0.0.1:4300/healthz | grep -q '^ok$'"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 5s
  aura-core-postgres:
    volumes: !override
      - core_postgres_data:/var/lib/postgresql/data
  aura-nats:
    volumes: !override
      - core_nats_data:/data
  aura-garage:
    volumes: !override
      - core_garage_data:/var/lib/garage
      - core_garage_config:/run/garage-config:ro
  aura-garage-config:
    volumes: !override
      - core_garage_config:/run/garage-config
      - ./deploy/docker/garage.toml:/etc/garage/garage.toml.template:ro
  aura-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  observatory-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  observatory-postgres:
    volumes: !override
      - observatory_postgres_data:/var/lib/postgresql/data
    # The base stack's historical expression is parsed as a literal NUL by
    # Compose. Keep the disposable overlay on the safe escaped command used
    # by the local HTTP deployment.
    healthcheck:
      test: [CMD-SHELL, "test \"$$(tr -d '\\000' </proc/1/cmdline)\" = postgres && pg_isready -U \"$${POSTGRES_USER}\" -d \"$${POSTGRES_DB}\""]
      interval: 5s
      timeout: 5s
      retries: 10
  observatory-clickhouse:
    volumes: !override
      - observatory_clickhouse_data:/var/lib/clickhouse
      - observatory_clickhouse_logs:/var/log/clickhouse-server
  observatory-session:
    volumes: !override
      - observatory_session_data:/data
  frontend-e2e:
    environment:
      BASE_URL: http://aura-web:8080
      AURA_OBSERVATORY_FULL_STACK: "1"
      AURA_OBSERVATORY_BASE_URL: http://observatory.localhost:4300
      AURA_OBSERVATORY_FULL_STACK_BASE_URL: http://observatory.localhost:4300
      AURA_OBSERVATORY_BROWSER_HOST_RESOLVER_RULES: MAP observatory.localhost observatory-web
      AURA_OBSERVATORY_CORE_BASE_URL: http://aura-web:8080
      AURA_OBSERVATORY_CORE_MODEL: fixture-chat-2
      AURA_OBSERVATORY_RUN_PROMPT: AURA0090_PRIVACY_SENTINEL
      AURA_OBSERVATORY_PRIVACY_SENTINEL: AURA0090_PRIVACY_SENTINEL
      AURA_OBSERVATORY_STORAGE_STATE_PATH: /tmp/observatory-browser-state.json
      AURA_OBSERVATORY_IDENTITY_MODE: external-oidc
      AURA_OBSERVATORY_ISSUER: http://aura-e2e-oidc:8081
      AURA_OBSERVATORY_IDENTITY_URL: http://aura-e2e-oidc:8081
      AURA_OBSERVATORY_OWNER_USERNAME: owner
      AURA_OBSERVATORY_OWNER_PASSWORD: fixture-password
      AURA_OBSERVATORY_OWNER_SUBJECT: owner
      AURA_E2E_IDENTITY_MODE: external-oidc
      AURA_E2E_ISSUER: http://aura-e2e-oidc:8081
      AURA_E2E_IDENTITY_URL: http://aura-e2e-oidc:8081
      AURA_E2E_OWNER_USERNAME: owner
      AURA_E2E_OWNER_PASSWORD: fixture-password
      AURA_E2E_OWNER_SUBJECT: owner
    volumes:
      - ${AURA_E2E_OBSERVATORY_BROWSER_STATE_FILE}:/tmp/observatory-browser-state.json
    networks:
      - aura-frontend
      - observatory-edge
      - aura-e2e-provider
    command: [sh, -c, "pnpm --dir frontend exec playwright test --config=apps/observatory-web-e2e/full-stack.config.ts"]
secrets:
  aura-core-database-password: !override
    file: ${AURA_E2E_CORE_DATABASE_SECRET_SOURCE_FILE}
  aura-oidc-client-secret: !override
    file: ${AURA_E2E_OIDC_CLIENT_SECRET_SOURCE_FILE}
  aura-nats-password: !override
    file: ${AURA_E2E_NATS_PASSWORD_SECRET_SOURCE_FILE}
  aura-valkey-password: !override
    file: ${AURA_E2E_VALKEY_PASSWORD_SECRET_SOURCE_FILE}
  aura-garage-rpc-secret: !override
    file: ${AURA_E2E_GARAGE_RPC_SECRET_SOURCE_FILE}
  aura-garage-access-key: !override
    file: ${AURA_E2E_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE}
  aura-garage-secret-key: !override
    file: ${AURA_E2E_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE}
  observatory-garage-access-key: !override
    file: ${AURA_E2E_OBSERVATORY_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE}
  observatory-garage-secret-key: !override
    file: ${AURA_E2E_OBSERVATORY_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE}
  observatory-database-password: !override
    file: ${AURA_E2E_OBSERVATORY_DATABASE_PASSWORD_SECRET_SOURCE_FILE}
  observatory-oidc-client-secret: !override
    file: ${AURA_E2E_OBSERVATORY_OIDC_CLIENT_SECRET_SOURCE_FILE}
  observatory-session-password: !override
    file: ${AURA_E2E_OBSERVATORY_SESSION_PASSWORD_SECRET_SOURCE_FILE}
  observatory-ingest-credential: !override
    file: ${AURA_E2E_OBSERVATORY_INGEST_CREDENTIAL_SECRET_SOURCE_FILE}
  observatory-clickhouse-password: !override
    file: ${AURA_E2E_OBSERVATORY_CLICKHOUSE_PASSWORD_SECRET_SOURCE_FILE}
volumes:
  core_postgres_data:
  core_nats_data:
  core_garage_data:
  core_garage_config:
  observatory_postgres_data:
  observatory_clickhouse_data:
  observatory_clickhouse_logs:
  observatory_session_data:
networks:
  aura-e2e-provider:
    external: true
    name: aura-e2e-provider
EOF

compose=(docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks)
# Bring up the Core API first so its startup path can create and validate the
# AURA_RUNS JetStream stream before a worker begins consuming fresh-state
# commands. The API's readiness route is the existing dependency signal; it
# includes NATS JetStream and provider checks without inventing a worker
# health endpoint.
"${compose[@]}" up --build --detach --wait aura-core-api
"${compose[@]}" exec -T aura-core-api python - <<'PY'
import json
import time
import urllib.error
import urllib.request

deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    try:
        response = urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5)
        payload = json.loads(response.read(65536))
        if response.status == 200 and payload.get("status") == "ready":
            print("Core API readiness preflight passed")
            break
    except (OSError, ValueError, urllib.error.HTTPError):
        pass
    time.sleep(2)
else:
    raise SystemExit("Core API readiness preflight timed out")
PY
# The browser-facing edge and Observatory services can now start against a
# stable Core API. Start the Core worker and scheduler only after that
# readiness barrier has completed, avoiding the fresh-state NATS stream race.
"${compose[@]}" up --build --detach --wait \
  aura-web observatory-web observatory-api observatory-collector
"${compose[@]}" up --build --detach --wait aura-core-worker aura-core-scheduler observatory-worker
# Exercise the worker-only evaluation storage boundary with bounded,
# synthetic fixtures.  Stop the long-running worker first so command creation
# and the explicit selector assertions cannot race its polling loop.  The
# final command is left in an expired, persisted objects phase and is resumed
# only after the worker is brought back up below.
"${compose[@]}" stop observatory-worker
"${compose[@]}" run --rm --no-deps --no-TTY observatory-worker /app/.venv/bin/python - <<'PY'
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from datetime import timedelta
from typing import Any

import httpx
from aura_observatory.adapters.evaluation_storage import (
    ClickHouseEvaluationScoreStore,
    PostgresEvaluationArtifactRepository,
)
from aura_observatory.composition import (
    build_evaluation_artifact_storage,
    build_evaluation_deletion,
    postgres_connection,
)
from aura_observatory.evaluation_storage import (
    ArtifactUpload,
    DeletionCommand,
    DeletionKind,
    DeletionSelector,
    EvaluationScore,
)
from aura_observatory.settings import Settings


RESTART_COMMAND_ID = "00000000-0000-4000-8000-000000009014"
RESTART_COMMAND_KEY = "00000000-0000-4000-8000-000000009015"
PAYLOAD = b"bounded-observatory-e2e-artifact"
FIXTURES: tuple[dict[str, str], ...] = (
    {
        "label": "job-missing-object",
        "authorization_id": "00000000-0000-4000-8000-000000009001",
        "artifact_id": "00000000-0000-4000-8000-000000009002",
        "reference_id": "00000000-0000-4000-8000-000000009003",
        "request_id": "00000000-0000-4000-8000-000000009004",
        "result_id": "00000000-0000-4000-8000-000000009005",
        "idempotency_key": "00000000-0000-4000-8000-000000009006",
        "job_id": "00000000-0000-4000-8000-000000009007",
        "dataset_id": "00000000-0000-4000-8000-000000009008",
        "suite_id": "00000000-0000-4000-8000-000000009009",
        "score_id": "00000000-0000-4000-8000-000000009010",
    },
    {
        "label": "dataset",
        "authorization_id": "00000000-0000-4000-8000-000000009021",
        "artifact_id": "00000000-0000-4000-8000-000000009022",
        "reference_id": "00000000-0000-4000-8000-000000009023",
        "request_id": "00000000-0000-4000-8000-000000009024",
        "result_id": "00000000-0000-4000-8000-000000009025",
        "idempotency_key": "00000000-0000-4000-8000-000000009026",
        "job_id": "00000000-0000-4000-8000-000000009027",
        "dataset_id": "00000000-0000-4000-8000-000000009028",
        "suite_id": "00000000-0000-4000-8000-000000009029",
        "score_id": "00000000-0000-4000-8000-000000009030",
    },
    {
        "label": "suite",
        "authorization_id": "00000000-0000-4000-8000-000000009031",
        "artifact_id": "00000000-0000-4000-8000-000000009032",
        "reference_id": "00000000-0000-4000-8000-000000009033",
        "request_id": "00000000-0000-4000-8000-000000009034",
        "result_id": "00000000-0000-4000-8000-000000009035",
        "idempotency_key": "00000000-0000-4000-8000-000000009036",
        "job_id": "00000000-0000-4000-8000-000000009037",
        "dataset_id": "00000000-0000-4000-8000-000000009038",
        "suite_id": "00000000-0000-4000-8000-000000009039",
        "score_id": "00000000-0000-4000-8000-000000009040",
    },
    {
        "label": "cutoff-old",
        "authorization_id": "00000000-0000-4000-8000-000000009041",
        "artifact_id": "00000000-0000-4000-8000-000000009042",
        "reference_id": "00000000-0000-4000-8000-000000009043",
        "request_id": "00000000-0000-4000-8000-000000009044",
        "result_id": "00000000-0000-4000-8000-000000009045",
        "idempotency_key": "00000000-0000-4000-8000-000000009046",
        "job_id": "00000000-0000-4000-8000-000000009047",
        "dataset_id": "00000000-0000-4000-8000-000000009048",
        "suite_id": "00000000-0000-4000-8000-000000009049",
        "score_id": "00000000-0000-4000-8000-000000009050",
    },
    {
        "label": "cutoff-new",
        "authorization_id": "00000000-0000-4000-8000-000000009051",
        "artifact_id": "00000000-0000-4000-8000-000000009052",
        "reference_id": "00000000-0000-4000-8000-000000009053",
        "request_id": "00000000-0000-4000-8000-000000009054",
        "result_id": "00000000-0000-4000-8000-000000009055",
        "idempotency_key": "00000000-0000-4000-8000-000000009056",
        "job_id": "00000000-0000-4000-8000-000000009057",
        "dataset_id": "00000000-0000-4000-8000-000000009058",
        "suite_id": "00000000-0000-4000-8000-000000009059",
        "score_id": "00000000-0000-4000-8000-000000009060",
    },
    {
        "label": "purge-all",
        "authorization_id": "00000000-0000-4000-8000-000000009061",
        "artifact_id": "00000000-0000-4000-8000-000000009062",
        "reference_id": "00000000-0000-4000-8000-000000009063",
        "request_id": "00000000-0000-4000-8000-000000009064",
        "result_id": "00000000-0000-4000-8000-000000009065",
        "idempotency_key": "00000000-0000-4000-8000-000000009066",
        "job_id": "00000000-0000-4000-8000-000000009067",
        "dataset_id": "00000000-0000-4000-8000-000000009068",
        "suite_id": "00000000-0000-4000-8000-000000009069",
        "score_id": "00000000-0000-4000-8000-000000009070",
    },
)


async def chunks():
    yield PAYLOAD


async def main() -> None:
    settings = Settings()
    repository = PostgresEvaluationArtifactRepository(
        lambda: postgres_connection(settings.database_url)
    )
    storage = build_evaluation_artifact_storage(settings)
    score_store = ClickHouseEvaluationScoreStore(
        settings.clickhouse_url,
        database=settings.clickhouse_database,
        user=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )
    deletion = build_evaluation_deletion(settings)
    now = datetime.now(UTC).replace(microsecond=0)
    cutoff = now - timedelta(days=1)
    times = {
        "job-missing-object": now,
        "dataset": now,
        "suite": now,
        "cutoff-old": cutoff - timedelta(hours=1),
        "cutoff-new": cutoff + timedelta(hours=1),
        "purge-all": now,
    }
    digest = hashlib.sha256(PAYLOAD).hexdigest()

    async def add_fixture(fixture: dict[str, str]) -> None:
        observed_at = times[fixture["label"]]
        async with postgres_connection(settings.database_url) as connection:
            await connection.execute(
                """INSERT INTO observatory_evaluation_authorizations(
                authorization_record_id, request_id, dataset_id, capture_classification,
                authorization_purpose, authorization_context, retention_mode, authorized_at)
                VALUES (%s, %s, %s, 'full_content', 'owner_supplied_evaluation',
                'controlled_evaluation', 'manual_only', %s)""",
                (
                    fixture["authorization_id"],
                    fixture["request_id"],
                    fixture["dataset_id"],
                    now,
                ),
            )
        request = ArtifactUpload(
            artifact_id=fixture["artifact_id"],
            artifact_reference_id=fixture["reference_id"],
            request_id=fixture["request_id"],
            result_id=fixture["result_id"],
            idempotency_key=fixture["idempotency_key"],
            authorization_record_id=fixture["authorization_id"],
            capture_classification="full_content",
            media_classification="evaluation_input",
            media_type="application/octet-stream",
            expected_sha256=digest,
            max_bytes=1024,
            job_id=fixture["job_id"],
            dataset_id=fixture["dataset_id"],
            suite_id=fixture["suite_id"],
            created_at=observed_at,
        )
        await storage.store(request, chunks())
        await score_store.insert_score(
            EvaluationScore(
                score_id=fixture["score_id"],
                job_id=fixture["job_id"],
                result_id=fixture["result_id"],
                dataset_id=fixture["dataset_id"],
                suite_id=fixture["suite_id"],
                evaluator_id="00000000-0000-4000-8000-000000009013",
                evaluator_version="e2e",
                evaluator_kind="deterministic",
                component_id="aura.observatory.e2e.storage",
                component_version="e2e",
                capture_classification="full_content",
                evidence_status="observed",
                value=0.0,
                outcome="pass",
                observed_at=observed_at,
                artifact_reference_id=fixture["reference_id"],
            )
        )

    for fixture in FIXTURES:
        await add_fixture(fixture)

    # Also retain a score with no content-bearing artifact reference.  It is
    # part of the purge fixture and proves nullable outcome/value semantics.
    missing_score_id = "00000000-0000-4000-8000-000000009071"
    purge = next(item for item in FIXTURES if item["label"] == "purge-all")
    await score_store.insert_score(
        EvaluationScore(
            score_id=missing_score_id,
            job_id=purge["job_id"],
            result_id=purge["result_id"],
            dataset_id=purge["dataset_id"],
            suite_id=purge["suite_id"],
            evaluator_id="00000000-0000-4000-8000-000000009013",
            evaluator_version="e2e",
            evaluator_kind="deterministic",
            component_id="aura.observatory.e2e.storage",
            component_version="e2e",
            capture_classification="metadata_only",
            evidence_status="not_observed",
            value=None,
            outcome=None,
            observed_at=now,
            artifact_reference_id=None,
        )
    )

    async def clickhouse_rows(query: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                content=query + " FORMAT JSONEachRow",
            )
            response.raise_for_status()
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    score_ids = [item["score_id"] for item in FIXTURES] + [missing_score_id]
    score_rows = await clickhouse_rows(
        "SELECT score_id, job_id, result_id, dataset_id, suite_id, "
        "score_value, outcome, artifact_reference_id FROM evaluation_scores "
        "WHERE score_id IN (" + ",".join(f"toUUID('{value}')" for value in score_ids) + ")"
    )
    if len(score_rows) != len(score_ids):
        raise SystemExit("ClickHouse score readback did not return every bounded fixture")
    by_score_id = {str(row["score_id"]): row for row in score_rows}
    for fixture in FIXTURES:
        row = by_score_id.get(fixture["score_id"])
        if not row or float(row["score_value"]) != 0.0 or row["outcome"] != "pass":
            raise SystemExit("ClickHouse score readback lost safe zero-score correlation")
        for key in ("job_id", "result_id", "dataset_id", "suite_id"):
            if not row.get(key):
                raise SystemExit("ClickHouse score readback lost a bounded correlation")
    missing_row = by_score_id.get(missing_score_id)
    if not missing_row or missing_row["score_value"] is not None or missing_row["outcome"] is not None:
        raise SystemExit("ClickHouse nullable score readback was not preserved")

    async def assert_deleted(fixture: dict[str, str], *, expect_deleted: bool) -> None:
        async with postgres_connection(settings.database_url) as connection:
            result = await connection.execute(
                "SELECT COUNT(*) FROM observatory_evaluation_artifacts WHERE artifact_reference_id = %s",
                (fixture["reference_id"],),
            )
            row = await result.fetchone()
        if not row or int(row[0]) != (0 if expect_deleted else 1):
            raise SystemExit("evaluation artifact metadata selector result was incorrect")
        rows = await clickhouse_rows(
            "SELECT count() AS count FROM evaluation_scores WHERE score_id = "
            f"toUUID('{fixture['score_id']}')"
        )
        if not rows or int(rows[0]["count"]) != (0 if expect_deleted else 1):
            raise SystemExit("evaluation score selector result was incorrect")

    async def execute_command(command: DeletionCommand, expected_count: int) -> None:
        # Exercise the production use case, including its authenticated
        # ClickHouse HTTP score snapshot.  Passing IDs directly to the
        # repository would bypass the boundary this acceptance is intended to
        # validate.
        await deletion.create_command(command)
        if await deletion.execute(command, page_size=10) != expected_count:
            raise SystemExit("evaluation deletion selector returned an unexpected count")

    job = FIXTURES[0]
    # Simulate provider-side loss before the durable command runs; Garage
    # DELETE remains idempotent while PG/ClickHouse removal is still exact.
    await storage.object_store.delete(job["reference_id"])
    job_command = DeletionCommand(
        command_id="00000000-0000-4000-8000-000000009011",
        idempotency_key="00000000-0000-4000-8000-000000009012",
        selector=DeletionSelector(DeletionKind.JOB, job["job_id"]),
        confirmation="DELETE_EVALUATION_ARTIFACTS",
    )
    await execute_command(job_command, 1)
    if await deletion.execute(job_command, page_size=10) != 0:
        raise SystemExit("missing-object deletion retry was not idempotent")
    await assert_deleted(job, expect_deleted=True)

    dataset = FIXTURES[1]
    await execute_command(
        DeletionCommand(
            command_id="00000000-0000-4000-8000-000000009072",
            idempotency_key="00000000-0000-4000-8000-000000009073",
            selector=DeletionSelector(DeletionKind.DATASET, dataset["dataset_id"]),
            confirmation="DELETE_EVALUATION_ARTIFACTS",
        ),
        1,
    )
    await assert_deleted(dataset, expect_deleted=True)

    suite = FIXTURES[2]
    await execute_command(
        DeletionCommand(
            command_id="00000000-0000-4000-8000-000000009074",
            idempotency_key="00000000-0000-4000-8000-000000009075",
            selector=DeletionSelector(DeletionKind.SUITE, suite["suite_id"]),
            confirmation="DELETE_EVALUATION_ARTIFACTS",
        ),
        1,
    )
    await assert_deleted(suite, expect_deleted=True)

    old_fixture = FIXTURES[3]
    new_fixture = FIXTURES[4]
    await execute_command(
        DeletionCommand(
            command_id="00000000-0000-4000-8000-000000009076",
            idempotency_key="00000000-0000-4000-8000-000000009077",
            selector=DeletionSelector(DeletionKind.CUTOFF, cutoff),
            confirmation="DELETE_EVALUATION_ARTIFACTS",
        ),
        1,
    )
    await assert_deleted(old_fixture, expect_deleted=True)
    await assert_deleted(new_fixture, expect_deleted=False)

    # Persist, claim, and phase the final command while the worker is stopped.
    # Expire only its lease so the restarted production worker must resume the
    # same immutable target rather than taking a fresh selector snapshot.
    restart_command = DeletionCommand(
        command_id=RESTART_COMMAND_ID,
        idempotency_key=RESTART_COMMAND_KEY,
        selector=DeletionSelector(DeletionKind.PURGE_ALL),
        confirmation="DELETE_EVALUATION_ARTIFACTS",
    )
    await deletion.create_command(restart_command)
    lease_token = await repository.claim_deletion_command(RESTART_COMMAND_ID)
    if lease_token is None or not await repository.mark_deletion_phase(
        RESTART_COMMAND_ID, "objects", lease_token=lease_token
    ):
        raise SystemExit("could not persist the restart recovery phase")
    async with postgres_connection(settings.database_url) as connection:
        await connection.execute(
            "UPDATE observatory_evaluation_deletion_commands SET lease_expires_at = "
            "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE command_id = %s",
            (RESTART_COMMAND_ID,),
        )
    print("Observatory evaluation storage fixtures and score readback prepared")


asyncio.run(main())
print("Observatory evaluation storage/deletion preflight passed")
PY
"${compose[@]}" up --detach --wait observatory-worker
# The worker was stopped while the final purge command was claimed in its
# persisted objects phase.  Verify that a fresh worker resumes that exact
# command, removes the object/metadata/score target, and writes one
# content-free audit without selecting new targets.
"${compose[@]}" run --rm --no-deps --no-TTY observatory-worker /app/.venv/bin/python - <<'PY'
import asyncio
import json
import time
from typing import Any

import httpx
from aura_observatory.composition import postgres_connection
from aura_observatory.settings import Settings


COMMAND_ID = "00000000-0000-4000-8000-000000009014"
REFERENCE_IDS = (
    "00000000-0000-4000-8000-000000009053",
    "00000000-0000-4000-8000-000000009063",
)
SCORE_IDS = (
    "00000000-0000-4000-8000-000000009060",
    "00000000-0000-4000-8000-000000009070",
    "00000000-0000-4000-8000-000000009071",
)


async def main() -> None:
    settings = Settings()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        async with postgres_connection(settings.database_url) as connection:
            result = await connection.execute(
                "SELECT status, deletion_phase, deleted_count FROM "
                "observatory_evaluation_deletion_commands WHERE command_id = %s",
                (COMMAND_ID,),
            )
            row = await result.fetchone()
        if row and row[0] == "completed":
            if row[1] != "objects" or int(row[2]) != 2:
                raise SystemExit("restart recovery completed with an unexpected persisted count")
            break
        if row and row[0] not in {"queued", "running"}:
            raise SystemExit("restart recovery entered an invalid terminal state")
        await asyncio.sleep(2)
    else:
        raise SystemExit("restarted Observatory worker did not resume the persisted command")

    async with postgres_connection(settings.database_url) as connection:
        result = await connection.execute(
            "SELECT COUNT(*) FROM observatory_evaluation_artifacts "
            "WHERE artifact_reference_id IN (%s, %s)",
            REFERENCE_IDS,
        )
        artifact_row = await result.fetchone()
        result = await connection.execute(
            "SELECT COUNT(*) FROM observatory_evaluation_deletion_targets "
            "WHERE command_id = %s AND completed_at IS NULL",
            (COMMAND_ID,),
        )
        pending_target_row = await result.fetchone()
        result = await connection.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE outcome = 'completed') "
            "FROM observatory_evaluation_deletion_audits WHERE command_id = %s",
            (COMMAND_ID,),
        )
        audit_row = await result.fetchone()
    if not artifact_row or int(artifact_row[0]) != 0:
        raise SystemExit("restart recovery left artifact metadata")
    if not pending_target_row or int(pending_target_row[0]) != 0:
        raise SystemExit("restart recovery retargeted or left a pending artifact target")
    if not audit_row or tuple(map(int, audit_row)) != (1, 1):
        raise SystemExit("restart recovery did not write one completed content-free audit")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.clickhouse_url,
            params={"database": settings.clickhouse_database},
            auth=(settings.clickhouse_user, settings.clickhouse_password),
            content=(
                "SELECT count() AS count FROM evaluation_scores WHERE score_id IN ("
                + ",".join(f"toUUID('{value}')" for value in SCORE_IDS)
                + ") FORMAT JSONEachRow"
            ),
        )
        response.raise_for_status()
    score_rows: list[dict[str, Any]] = [
        json.loads(line) for line in response.text.splitlines() if line.strip()
    ]
    if not score_rows or int(score_rows[0]["count"]) != 0:
        raise SystemExit("restart recovery left ClickHouse score targets")


asyncio.run(main())
print("Observatory evaluation worker restart recovery passed")
PY
# Verify the actual worker container can resolve and reach the deterministic
# provider before the browser creates a run. This is intentionally a bounded,
# metadata-only GET from the worker network namespace; it catches an
# accidental Settings/Compose override mismatch without changing Core
# startup or provider behavior.
"${compose[@]}" exec -T aura-core-worker python -c \
  "import json, os, urllib.request; endpoint=os.environ.get('AURA_OLLAMA_URL'); model=os.environ.get('AURA_DEFAULT_MODEL'); assert endpoint == 'http://aura-e2e-ollama:8081', 'unexpected worker provider endpoint'; assert model == 'fixture-chat-2', 'unexpected worker default model'; response=urllib.request.urlopen(endpoint + '/api/tags', timeout=5); payload=json.loads(response.read(65536)); assert any(item.get('name') == model for item in payload.get('models', [])), 'fixture-chat-2 is not in provider inventory'; print('Core worker provider preflight passed')"
# Exercise the same Core adapter protocol used by this metadata-only run after
# inventory succeeds. Keep the probe content fixed and harmless, bound the
# entire stream, and report only pass/fail so no provider content is printed.
"${compose[@]}" exec -T aura-core-worker /app/.venv/bin/python - <<'PY'
import asyncio
import os

from aura_core.domains.execution.runs.ports import ChatMessage
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.runtime.models.ports import ModelStreamCompleted, ModelTextDelta


async def probe() -> None:
    endpoint = os.environ.get("AURA_OLLAMA_URL")
    model = os.environ.get("AURA_DEFAULT_MODEL")
    if endpoint != "http://aura-e2e-ollama:8081" or model != "fixture-chat-2":
        raise SystemExit("unexpected worker provider configuration")
    adapter = OllamaAdapter(endpoint, timeout_seconds=5.0)
    saw_text = False
    saw_completed = False
    async with asyncio.timeout(10):
        async for event in adapter.stream_chat_events(
            model,
            [ChatMessage("user", "bounded provider protocol probe")],
        ):
            if isinstance(event, ModelTextDelta) and event.text:
                saw_text = True
            if isinstance(event, ModelStreamCompleted):
                saw_completed = True
    if not saw_text or not saw_completed:
        raise SystemExit("provider adapter stream did not complete")


asyncio.run(probe())
print("Core worker adapter protocol preflight passed")
PY
# Exercise the exact Observatory ClickHouse insert boundary before any browser
# run can create real observations. Build one validated, fixed metadata-only
# payload through the production row helper, use the service's Settings for
# endpoint/database/file-injected credentials, and retain the adapter's
# deduplication parameters. A fixed valid run_id deliberately exercises the
# run_projections materialized view; synchronous mutations in finally remove
# both the observation and derived projection even when the insert reports an
# error.
"${compose[@]}" exec -T observatory-api /app/.venv/bin/python - <<'PY'
import asyncio
import json
from datetime import datetime, timezone

import httpx

from aura_observatory.adapters.clickhouse import clickhouse_row
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.query import AnalyticsRequest, RunFilters
from aura_observatory.settings import Settings


OBSERVATION_ID = "00000000-0000-4000-8000-000000000084"
RUN_ID = "00000000-0000-4000-8000-000000000086"
SCAN_BOUNDARY_COMPONENT = "aura.observatory.e2e.scan-boundary"
QUANTILE_COMPONENT = "aura.observatory.e2e.quantile-parity"


def bounded_error(value: object) -> str:
    return " ".join(str(value).split())[:512]


def provider_status(error: BaseException) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return str(status) if isinstance(status, int) else "transport"


settings = Settings()
observation = MetadataObservation.model_validate(
    {
        "schemaVersion": 1,
        "observationId": OBSERVATION_ID,
        "observedAt": "2026-01-01T00:00:00.000Z",
        "kind": "metric",
        "component": {"id": "aura.observatory", "version": "e2e"},
        "operation": "preflight",
        "measurement": {"name": "latency", "value": 1.0, "unit": "milliseconds"},
        "dimensions": {"provider": "fixture-chat-2", "status": "probe"},
        "correlation": {
            "runId": RUN_ID,
            "conversationId": "00000000-0000-4000-8000-000000000085",
        },
        "deployment": {
            "deploymentId": "e2e",
            "gitCommit": "0000000",
            "environment": "test",
            "hostId": "e2e",
        },
        "serviceRole": "api",
        "capturePolicy": "metadata_only",
    }
)
row = clickhouse_row(observation.to_payload(), ingest_sequence=1)
insert = "INSERT INTO observations FORMAT JSONEachRow\n" + json.dumps(
    row, separators=(",", ":")
)
cleanup_statements = (
    "ALTER TABLE observations DELETE WHERE observation_id = "
    + repr(OBSERVATION_ID)
    + " SETTINGS mutations_sync=1",
    "ALTER TABLE observations DELETE WHERE component_id = "
    + repr(SCAN_BOUNDARY_COMPONENT)
    + " SETTINGS mutations_sync=1",
    "ALTER TABLE observations DELETE WHERE component_id = "
    + repr(QUANTILE_COMPONENT)
    + " SETTINGS mutations_sync=1",
    "ALTER TABLE run_projections DELETE WHERE run_id = "
    + repr(RUN_ID)
    + " SETTINGS mutations_sync=1",
)
params = {
    "database": settings.clickhouse_database,
    "insert_deduplicate": "1",
    "insert_deduplication_token": OBSERVATION_ID,
}
failure = None


def unavailable_postgres() -> object:
    raise RuntimeError("PostgreSQL is not part of this ClickHouse probe")


try:
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                settings.clickhouse_url,
                params=params,
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                content=insert,
            )
        if not response.is_success:
            failure = f"ClickHouse preflight failed: {bounded_error(response.text)}"
    except httpx.HTTPError:
        failure = "ClickHouse preflight failed: transport error"
    if failure is None:
        async def query_probe() -> None:
            store = ProductionObservatoryStore(
                unavailable_postgres,
                settings.clickhouse_url,
                database=settings.clickhouse_database,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password,
            )
            # These are the exact production adapter paths used by the
            # overview and Runs browser resources. The PostgreSQL watermark
            # falls back to an unfenced bounded read in this isolated probe;
            # both ClickHouse statements still use production SQL generation.
            await store._overview_counts()  # pyright: ignore[reportPrivateUsage]
            await store.run_query(RunFilters(), cursor=None, limit=50)

        try:
            with httpx.Client(timeout=30.0) as client:
                version_response = client.post(
                    settings.clickhouse_url,
                    params={"database": settings.clickhouse_database},
                    auth=(settings.clickhouse_user, settings.clickhouse_password),
                    content=(
                        "SELECT version() AS provider_version, "
                        "quantileExact(0.50)(value) AS p50, "
                        "quantileExact(0.90)(value) AS p90, "
                        "quantileExact(0.95)(value) AS p95, "
                        "quantileExact(0.99)(value) AS p99 "
                        "FROM (SELECT arrayJoin([toFloat64(1), toFloat64(2), "
                        "toFloat64(3), toFloat64(100)]) AS value) "
                        "FORMAT JSONEachRow"
                    ),
                )
            if not version_response.is_success:
                failure = "ClickHouse quantileExact preflight failed"
            else:
                quantile_row = json.loads(
                    next(line for line in version_response.text.splitlines() if line.strip())
                )
                provider_version = str(quantile_row["provider_version"])
                if tuple(provider_version.split(".")[:2]) != ("25", "8"):
                    failure = "ClickHouse provider is not pinned to 25.8"
                expected_quantiles = {"p50": 3.0, "p90": 100.0, "p95": 100.0, "p99": 100.0}
                actual_quantiles = {
                    key: float(quantile_row[key]) for key in expected_quantiles
                }
                if actual_quantiles != expected_quantiles:
                    failure = (
                        "ClickHouse quantileExact parity preflight failed: actual="
                        + json.dumps(actual_quantiles, sort_keys=True)
                    )
                else:
                    print(
                        "ClickHouse 25.8 quantileExact parity passed: "
                        + json.dumps(actual_quantiles, sort_keys=True)
                    )
        except (KeyError, StopIteration, TypeError, ValueError, httpx.HTTPError):
            failure = "ClickHouse quantileExact preflight failed"

        if failure is None:
            quantile_rows = []
            for index, value in enumerate((1.0, 2.0, 3.0, 100.0), 1):
                quantile_row = dict(row)
                quantile_row.update(
                    {
                        "observation_id": f"aura-0087-quantile-{index}",
                        "ingest_sequence": 2_000_000 + index,
                        "component_id": QUANTILE_COMPONENT,
                        "measurement_name": "duration_ms",
                        "measurement_value": value,
                    }
                )
                quantile_rows.append(json.dumps(quantile_row, separators=(",", ":")))
            try:
                with httpx.Client(timeout=30.0) as client:
                    quantile_insert_response = client.post(
                        settings.clickhouse_url,
                        params={"database": settings.clickhouse_database},
                        auth=(settings.clickhouse_user, settings.clickhouse_password),
                        content="INSERT INTO observations FORMAT JSONEachRow\n"
                        + "\n".join(quantile_rows),
                    )
                if not quantile_insert_response.is_success:
                    failure = "ClickHouse quantile parity fixture insert failed"
            except httpx.HTTPError:
                failure = "ClickHouse quantile parity fixture insert failed"

        try:
            if failure is None:
                class ScanBoundaryStore(ProductionObservatoryStore):
                    async def _analytics_retention_boundary(  # pyright: ignore[reportPrivateUsage]
                        self, lower: object, upper: object
                    ) -> bool:
                        del lower, upper
                        return False

                store = ScanBoundaryStore(
                    unavailable_postgres,
                    settings.clickhouse_url,
                    database=settings.clickhouse_database,
                    user=settings.clickhouse_user,
                    password=settings.clickhouse_password,
                )
                try:
                    quantile_analytics = asyncio.run(store.operational_analytics_query(
                        AnalyticsRequest(
                            metric="operation_duration_ms",
                            unit="milliseconds",
                            from_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            to_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                            bucket="one_day",
                            group_by="component",
                            filters={"component": QUANTILE_COMPONENT},
                        )
                    ))
                except httpx.HTTPError as exc:
                    failure = (
                        "ClickHouse quantile adapter preflight failed status="
                        + provider_status(exc)
                    )
                    quantile_analytics = {"series": []}
                quantile_series = quantile_analytics.get("series", [])
                if failure is None and (
                    not isinstance(quantile_series, list) or not quantile_series
                ):
                    partial_reasons = quantile_analytics.get("partialReasons", [])
                    unavailable_signals = quantile_analytics.get("unavailableSignals", [])
                    safe_partial_reasons = (
                        [str(value) for value in partial_reasons[:8]]
                        if isinstance(partial_reasons, list)
                        else []
                    )
                    safe_unavailable_signals = (
                        [str(value) for value in unavailable_signals[:8]]
                        if isinstance(unavailable_signals, list)
                        else []
                    )
                    series_count = len(quantile_series) if isinstance(quantile_series, list) else 0
                    failure = (
                        "ProductionObservatoryStore quantile parity query returned no series "
                        f"count={series_count} partialReasons="
                        + json.dumps(safe_partial_reasons, sort_keys=True)
                        + " unavailableSignals="
                        + json.dumps(safe_unavailable_signals, sort_keys=True)
                    )
                elif failure is None:
                    quantile_summary = quantile_series[0].get("summary", {})
                    adapter_quantiles = {
                        key: float(quantile_summary[key]) for key in expected_quantiles
                    }
                    if adapter_quantiles != expected_quantiles:
                        failure = (
                            "ProductionObservatoryStore quantileExact parity failed: actual="
                            + json.dumps(adapter_quantiles, sort_keys=True)
                        )
                    else:
                        print(
                            "ProductionObservatoryStore quantileExact parity passed: "
                            + json.dumps(adapter_quantiles, sort_keys=True)
                        )
                if failure is None:
                    boundary_insert = (
                        "INSERT INTO observations (observation_id, ingest_sequence, observed_at, "
                        "schema_version, kind, component_id, component_version, operation, "
                        "measurement_name, measurement_value, measurement_unit, dimensions, "
                        "trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
                        "conversation_id, deployment_id, git_commit, environment, host_id, "
                        "service_role, capture_policy) SELECT "
                        "concat('aura-0087-scan-', toString(number)), number + 2, "
                        "toDateTime64('2026-01-01 00:00:00', 3, 'UTC'), 1, 'metric', "
                        + repr(SCAN_BOUNDARY_COMPONENT)
                        + ", 'e2e', 'scan', 'duration_ms', toFloat64(1), 'milliseconds', "
                        "map('outcome', 'success'), '', '', '', emptyArrayString(), '', '', "
                        "'e2e', '0000000', 'test', 'e2e', 'api', 'metadata_only' "
                        "FROM numbers(1000001) SETTINGS max_threads = 1"
                    )
                    try:
                        with httpx.Client(timeout=60.0) as client:
                            boundary_response = client.post(
                                settings.clickhouse_url,
                                params={"database": settings.clickhouse_database},
                                auth=(settings.clickhouse_user, settings.clickhouse_password),
                                content=boundary_insert,
                            )
                        if not boundary_response.is_success:
                            failure = "ClickHouse scan-boundary fixture insert failed"
                    except httpx.HTTPError:
                        failure = "ClickHouse scan-boundary fixture insert failed"

                if failure is None:
                    try:
                        analytics = asyncio.run(store.operational_analytics_query(
                            AnalyticsRequest(
                                metric="operation_duration_ms",
                                unit="milliseconds",
                                from_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                to_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                                bucket="one_hour",
                                group_by="component",
                                filters={"component": SCAN_BOUNDARY_COMPONENT},
                            )
                        ))
                    except httpx.HTTPError as exc:
                        failure = (
                            "ClickHouse scan-boundary adapter preflight failed status="
                            + provider_status(exc)
                        )
                        analytics = {"partialReasons": []}
                    if failure is None and "scan_limit" not in analytics.get("partialReasons", []):
                        failure = "ClickHouse scan-boundary query was not partial"
                    elif failure is None:
                        print("ProductionObservatoryStore scan-boundary query passed")
            if failure is None:
                try:
                    asyncio.run(query_probe())
                    print("Observatory ClickHouse overview/run query preflight passed")
                except httpx.HTTPError as exc:
                    failure = (
                        "ClickHouse overview/run adapter preflight failed status="
                        + provider_status(exc)
                    )
        except httpx.HTTPStatusError as exc:
            response = exc.response
            failure = (
                "ClickHouse query preflight failed "
                f"status={response.status_code}: {bounded_error(response.text)}"
            )
        except httpx.HTTPError:
            failure = "ClickHouse query preflight failed: transport error"
        except (KeyError, TypeError, ValueError, OSError):
            failure = "ClickHouse query preflight failed: response shape error"
finally:
    cleanup_failure = None
    try:
        with httpx.Client(timeout=10.0) as client:
            for statement in cleanup_statements:
                try:
                    cleanup = client.post(
                        settings.clickhouse_url,
                        params={"database": settings.clickhouse_database},
                        auth=(settings.clickhouse_user, settings.clickhouse_password),
                        content=statement,
                    )
                except httpx.HTTPError:
                    if cleanup_failure is None:
                        cleanup_failure = "ClickHouse preflight cleanup failed: transport error"
                    continue
                if not cleanup.is_success and cleanup_failure is None:
                    cleanup_failure = (
                        "ClickHouse preflight cleanup failed: "
                        + bounded_error(cleanup.text)
                    )
    except httpx.HTTPError:
        cleanup_failure = cleanup_failure or "ClickHouse preflight cleanup failed: transport error"
    if failure is None and cleanup_failure is not None:
        failure = cleanup_failure

if failure is not None:
    raise SystemExit(failure)
print("Observatory ClickHouse row-shape preflight passed")
PY
"${compose[@]}" build frontend-e2e
"${compose[@]}" run --rm --no-deps --no-TTY frontend-e2e
test -s "$browser_state"
# Scan bounded service logs and the metrics exposition for the same synthetic
# reference used by the browser SSE/URL/storage privacy assertions.  The
# reference is intentionally non-secret, and failures report only a generic
# result so it cannot be echoed as a diagnostic.
privacy_log="$fixture_root/privacy-scan.log"
"${compose[@]}" logs --no-color --tail=500 \
  aura-core-api aura-core-worker aura-core-scheduler observatory-api observatory-worker observatory-collector \
  >"$privacy_log" 2>/dev/null || true
if LC_ALL=C grep -Fq 'AURA0090_PRIVACY_SENTINEL' "$privacy_log"; then
  printf '%s\n' 'privacy scan found synthetic content in a captured service log' >&2
  exit 1
fi
"${compose[@]}" exec -T observatory-api /app/.venv/bin/python - <<'PY'
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5) as response:
        body = response.read(1_000_000)
except OSError as exc:
    raise SystemExit("privacy metrics scan unavailable") from exc
if b"AURA0090_PRIVACY_SENTINEL" in body:
    raise SystemExit("privacy scan found synthetic content in metrics exposition")
print("Observatory bounded privacy scan passed")
PY

# Exercise the same scoped Compose down/up lifecycle used by the shipped
# service, without removing named volumes. This proves the session store's
# production persistence configuration rather than applying a test-only
# command override.
"${compose[@]}" down --remove-orphans \
  observatory-web observatory-api observatory-worker observatory-collector observatory-session
"${compose[@]}" up --detach --wait \
  observatory-session observatory-api observatory-web observatory-worker observatory-collector
"${compose[@]}" run --rm --no-deps --no-TTY \
  -e AURA_OBSERVATORY_LIFECYCLE_PHASE=preserved \
  -e AURA_OBSERVATORY_LOAD_STORAGE_STATE=1 frontend-e2e \
  sh -c "exec pnpm --dir frontend exec playwright test \
    --config=apps/observatory-web-e2e/full-stack.config.ts \
    --grep 'preserves the owner session across a disposable Observatory restart'"

# Reset only this project's dedicated session store. The scoped down removes
# the session container but preserves every named volume until the one exact
# session volume is explicitly removed; all databases, observations, networks,
# and operator state remain.
"${compose[@]}" down --remove-orphans \
  observatory-web observatory-api observatory-worker observatory-collector observatory-session
docker volume rm "${project}_observatory_session_data" >/dev/null
"${compose[@]}" up --detach --wait \
  observatory-session observatory-api observatory-web observatory-worker observatory-collector
"${compose[@]}" run --rm --no-deps --no-TTY \
  -e AURA_OBSERVATORY_LIFECYCLE_PHASE=reset \
  -e AURA_OBSERVATORY_LOAD_STORAGE_STATE=1 frontend-e2e \
  sh -c "exec pnpm --dir frontend exec playwright test \
    --config=apps/observatory-web-e2e/full-stack.config.ts \
    --grep 'rejects the old session after reset and permits owner reauthentication'"

# Observatory is deliberately stopped after the real browser flow.  The
# existing acceptance spec is invoked again with only its outage test selected;
# that test signs into Core and submits a normal request/run while Observatory
# is unavailable.  Core's own probes remain an additional startup/readiness
# check below.
"${compose[@]}" stop observatory-web observatory-api observatory-worker observatory-collector
"${compose[@]}" run --rm --no-deps --no-TTY -e AURA_OBSERVATORY_SERVICES_STOPPED=1 frontend-e2e \
  sh -c "exec pnpm --dir frontend exec playwright test \
    --config=apps/observatory-web-e2e/full-stack.config.ts \
    --grep 'keeps Core usable after the platform stops Observatory services'"
"${compose[@]}" exec -T aura-core-api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5).read()"
"${compose[@]}" exec -T aura-core-api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).read()"
printf '%s\n' 'Observatory full-stack acceptance passed; Core remained healthy after Observatory stop.'
