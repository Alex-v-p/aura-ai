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
        observatory-api observatory-worker observatory-collector observatory-web \
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
read -r object_storage_subnet document_parser_subnet < <(
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

if len(free) < 2:
    raise SystemExit("no two free /28 subnets remain in the dedicated E2E pool")

start = int(suffix, 36) % len(free)
selected = [free[(start + offset) % len(free)] for offset in range(2)]
print(selected[0], selected[1])
PY
)
test -n "$object_storage_subnet"
test -n "$document_parser_subnet"

cat > "$env_file" <<EOF
AURA_STATE_ROOT=/unused/aura-observatory-e2e-state
AURA_CORE_DATABASE_PASSWORD_FILE=/run/secrets/aura-core-database-password
AURA_OIDC_CLIENT_SECRET_FILE=/run/secrets/aura-oidc-client-secret
AURA_NATS_PASSWORD_FILE=/run/secrets/aura-nats-password
AURA_VALKEY_PASSWORD_FILE=/run/secrets/aura-valkey-password
AURA_GARAGE_RPC_SECRET_FILE=/run/secrets/aura-garage-rpc-secret
AURA_GARAGE_ACCESS_KEY_FILE=/run/secrets/aura-garage-access-key
AURA_GARAGE_SECRET_KEY_FILE=/run/secrets/aura-garage-secret-key
AURA_E2E_CORE_DATABASE_SECRET_SOURCE_FILE=$secret_root/aura-core-database-password
AURA_E2E_OIDC_CLIENT_SECRET_SOURCE_FILE=$secret_root/aura-oidc-client-secret
AURA_E2E_NATS_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-password
AURA_E2E_VALKEY_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-valkey-password
AURA_E2E_GARAGE_RPC_SECRET_SOURCE_FILE=$secret_root/aura-garage-rpc-secret
AURA_E2E_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-access-key
AURA_E2E_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-secret-key
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
      AURA_OBSERVATORY_RUN_PROMPT: AURA_OBSERVATORY_E2E_METADATA_RUN
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

import httpx

from aura_observatory.adapters.clickhouse import clickhouse_row
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.contract import MetadataObservation
from aura_observatory.query import RunFilters
from aura_observatory.settings import Settings


OBSERVATION_ID = "00000000-0000-4000-8000-000000000084"
RUN_ID = "00000000-0000-4000-8000-000000000086"


def bounded_error(value: object) -> str:
    return " ".join(str(value).split())[:512]


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
            def unavailable_postgres() -> object:
                raise RuntimeError("PostgreSQL is not part of this ClickHouse probe")

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
            asyncio.run(query_probe())
            print("Observatory ClickHouse overview/run query preflight passed")
        except httpx.HTTPStatusError as exc:
            response = exc.response
            failure = (
                "ClickHouse query preflight failed "
                f"status={response.status_code}: {bounded_error(response.text)}"
            )
        except httpx.HTTPError:
            failure = "ClickHouse query preflight failed: transport error"
        except (KeyError, TypeError, ValueError):
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
