#!/usr/bin/env bash
set -Eeuo pipefail

# Opt-in, disposable acceptance runner for the complete Core -> Collector ->
# Observatory -> browser path.  It creates fixture-only credentials and state,
# and removes them on exit.  It intentionally does not read or modify .env or
# any operator deployment secret.

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# No fixture file is mounted into a container. Keep credential-bearing Compose
# env/override artifacts outside the root build context and remove them on exit.
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/aura-observatory-e2e.XXXXXX")
suffix=$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')
project="aura-observatory-e2e-$suffix"
override="$fixture_root/compose.override.yaml"
env_file="$fixture_root/.env"
cleanup_started=0

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
        logs --no-color --tail=100 observatory-api observatory-worker observatory-collector observatory-web 2>&1 || true
    fi
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

# Create the external fixture provider first so its disposable network is
# included in the IPAM inventory below. The provider remains outside the Aura
# Compose project and is removed by the exit trap.
docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
  up --build --detach --wait

# The normal private-network defaults are intentionally stable for operators,
# but an acceptance run must coexist with an already-running Aura stack. Pick
# two free /28s from bounded private pools by inspecting Docker's current
# network IPAM; no existing network is modified or removed.
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
for pool in (
    ipaddress.ip_network("10.254.192.0/18"),
    ipaddress.ip_network("172.30.0.0/16"),
    ipaddress.ip_network("172.31.0.0/16"),
):
    for candidate in pool.subnets(new_prefix=28):
        if all(not candidate.overlaps(existing) for existing in occupied):
            free.append(candidate)

if len(free) < 2:
    raise SystemExit("no two free /28 subnets remain in the bounded E2E pools")

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
AURA_E2E_CORE_DATABASE_PASSWORD=fixture-core-database-password
AURA_E2E_OIDC_CLIENT_SECRET=fixture-oidc-client-secret
AURA_E2E_NATS_PASSWORD=fixture-nats-password
AURA_E2E_VALKEY_PASSWORD=fixture-valkey-password
AURA_E2E_GARAGE_RPC_SECRET=0000000000000000000000000000000000000000000000000000000000000000
AURA_E2E_GARAGE_ACCESS_KEY=GK000000000000000000000000
AURA_E2E_GARAGE_SECRET_KEY=1111111111111111111111111111111111111111111111111111111111111111
AURA_OIDC_ISSUER=http://aura-e2e-oidc:8081
AURA_OIDC_AUDIENCE=aura-web
AURA_OIDC_CLIENT_ID=aura-web
AURA_OIDC_REDIRECT_URI=http://aura-web:8080/api/v1/auth/callback
AURA_OWNER_SUBJECT=owner
AURA_PUBLIC_ORIGIN=http://aura-web:8080
AURA_TELEMETRY_OTLP_ENABLED=true
AURA_DEFAULT_MODEL=fixture-chat
AURA_OBSERVATORY_DATABASE_PASSWORD_FILE=/run/secrets/observatory-database-password
AURA_OBSERVATORY_OIDC_CLIENT_SECRET_FILE=/run/secrets/observatory-oidc-client-secret
AURA_OBSERVATORY_SESSION_PASSWORD_FILE=/run/secrets/observatory-session-password
AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE=/run/secrets/observatory-ingest-credential
AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE=/run/secrets/observatory-clickhouse-password
AURA_E2E_OBSERVATORY_DATABASE_PASSWORD=fixture-observatory-database-password
AURA_E2E_OBSERVATORY_OIDC_CLIENT_SECRET=fixture-observatory-oidc-client-secret
AURA_E2E_OBSERVATORY_SESSION_PASSWORD=fixture-observatory-session-password
AURA_E2E_OBSERVATORY_INGEST_CREDENTIAL=fixture-observatory-ingest-credential
AURA_E2E_OBSERVATORY_CLICKHOUSE_PASSWORD=fixture-observatory-clickhouse-password
AURA_OBSERVATORY_PUBLIC_ORIGIN=http://observatory-web:8080
AURA_OBSERVATORY_OIDC_ISSUER=http://aura-e2e-oidc:8081
AURA_OBSERVATORY_OIDC_AUDIENCE=aura-observatory
AURA_OBSERVATORY_OIDC_CLIENT_ID=aura-observatory
AURA_OBSERVATORY_OIDC_REDIRECT_URI=http://observatory-web:8080/api/v1/auth/callback
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
      AURA_OBSERVATORY_BASE_URL: http://observatory-web:8080
      AURA_OBSERVATORY_FULL_STACK_BASE_URL: http://observatory-web:8080
      AURA_OBSERVATORY_CORE_BASE_URL: http://aura-web:8080
      AURA_OBSERVATORY_RUN_PROMPT: AURA_OBSERVATORY_E2E_METADATA_RUN
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
    networks:
      - aura-frontend
      - observatory-edge
      - aura-e2e-provider
    command: [sh, -c, "pnpm --dir frontend exec playwright test --config=apps/observatory-web-e2e/full-stack.config.ts"]
secrets:
  aura-core-database-password: !override
    environment: AURA_E2E_CORE_DATABASE_PASSWORD
  aura-oidc-client-secret: !override
    environment: AURA_E2E_OIDC_CLIENT_SECRET
  aura-nats-password: !override
    environment: AURA_E2E_NATS_PASSWORD
  aura-valkey-password: !override
    environment: AURA_E2E_VALKEY_PASSWORD
  aura-garage-rpc-secret: !override
    environment: AURA_E2E_GARAGE_RPC_SECRET
  aura-garage-access-key: !override
    environment: AURA_E2E_GARAGE_ACCESS_KEY
  aura-garage-secret-key: !override
    environment: AURA_E2E_GARAGE_SECRET_KEY
  observatory-database-password: !override
    environment: AURA_E2E_OBSERVATORY_DATABASE_PASSWORD
  observatory-oidc-client-secret: !override
    environment: AURA_E2E_OBSERVATORY_OIDC_CLIENT_SECRET
  observatory-session-password: !override
    environment: AURA_E2E_OBSERVATORY_SESSION_PASSWORD
  observatory-ingest-credential: !override
    environment: AURA_E2E_OBSERVATORY_INGEST_CREDENTIAL
  observatory-clickhouse-password: !override
    environment: AURA_E2E_OBSERVATORY_CLICKHOUSE_PASSWORD
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
"${compose[@]}" up --build --detach --wait \
  aura-web aura-core-api aura-core-worker aura-core-scheduler observatory-web observatory-api observatory-worker observatory-collector
"${compose[@]}" build frontend-e2e
"${compose[@]}" run --rm --no-deps frontend-e2e

# Observatory is deliberately stopped after the real browser flow.  The
# existing acceptance spec is invoked again with only its outage test selected;
# that test signs into Core and submits a normal request/run while Observatory
# is unavailable.  Core's own probes remain an additional startup/readiness
# check below.
"${compose[@]}" stop observatory-web observatory-api observatory-worker observatory-collector
"${compose[@]}" run --rm --no-deps -e AURA_OBSERVATORY_SERVICES_STOPPED=1 frontend-e2e \
  pnpm --dir frontend exec playwright test \
  --config=apps/observatory-web-e2e/full-stack.config.ts \
  --grep 'keeps Core usable after the platform stops Observatory services'
"${compose[@]}" exec -T aura-core-api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=5).read()"
"${compose[@]}" exec -T aura-core-api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).read()"
printf '%s\n' 'Observatory full-stack acceptance passed; Core remained healthy after Observatory stop.'
