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
# The default/full scope exercises the complete acceptance matrix.  The
# foundation scope is intentionally narrower for environments that are only
# proving the Core-to-Observatory run path: it still performs normal setup,
# pre-browser gates, Scenario 1, privacy/retention evidence, and scoped
# cleanup, but it never enters evaluation Scenarios 2-6 or the outage matrix.
acceptance_scope=${AURA_OBSERVATORY_ACCEPTANCE_SCOPE:-full}
foundation_scope=0
case "$acceptance_scope" in
  default|full)
    acceptance_scope=full
    ;;
  foundation)
    foundation_scope=1
    ;;
  *)
    printf '%s\n' 'AURA_OBSERVATORY_ACCEPTANCE_SCOPE must be default, full, or foundation' >&2
    exit 2
    ;;
esac
# Compose v5 can issue duplicate concurrent create requests for the same fixed
# networks and volumes during a large `up --build`. Serialize Compose's
# resource lifecycle for this disposable, low-memory acceptance stack so a
# partially-created network cannot race its own IPAM allocation.
export COMPOSE_PARALLEL_LIMIT=1
bounded() { timeout --foreground --signal=TERM --kill-after=10s "${1}s" "${@:2}"; }
# shellcheck source=e2e/diagnostic-categories.sh
. "$ROOT/deploy/compose/e2e/diagnostic-categories.sh"

capture_optional_metric_snapshot() {
  local phase=$1 metric=$2 snapshot
  if snapshot=$(capture_observatory_metric_total "$phase" "$metric") && [ -n "$snapshot" ]; then
    printf '%s' "$snapshot"
  else
    # A successful probe with an empty response is still unavailable evidence;
    # normalize it before any post-browser parser sees the value.
    printf '%s' '{"available":false,"reason":"optional-not-sampled"}'
  fi
}

# Keep credential-bearing Compose env/override artifacts in a repository-local
# ignored fixture directory so Docker Desktop can bind-mount the secret source
# files. The exit trap removes the directory and all of its contents.
fixture_root=$(mktemp -d "$ROOT/.aura-e2e.XXXXXX")
suffix=$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')
project="aura-observatory-e2e-$suffix"
browser_artifact_root=$(mktemp -d "$ROOT/.aura-e2e.artifacts.XXXXXX")
chmod 0700 "$browser_artifact_root"
override="$fixture_root/compose.override.yaml"
env_file="$fixture_root/.env"
secret_root="$fixture_root/secrets"
nginx_config="$fixture_root/observatory-web.nginx.conf"
browser_state="$fixture_root/observatory-browser-state.json"
browser_target="$fixture_root/observatory-browser-target.json"
browser_command_output="$fixture_root/browser-command-output.log"
component_manifest="$fixture_root/aura-evaluation-synthetic.yaml"
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

emit_failure_diagnostics() {
  local service container_id
  for service in \
    observatory-postgres observatory-migrate observatory-clickhouse observatory-session \
    observatory-garage-bootstrap observatory-api observatory-worker observatory-collector \
    observatory-web observatory-evaluation-worker observatory-evaluation-scheduler \
    aura-evaluation-runner aura-core-postgres aura-core-migrate aura-core-api \
    aura-core-worker aura-core-scheduler aura-web; do
    container_id=$(docker compose -p "$project" --env-file "$env_file" \
      -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks \
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
  container_id=$(docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
    ps -q aura-e2e-ollama 2>/dev/null | head -n 1 || true)
  if [ -n "$container_id" ]; then
    docker inspect --format "aura-e2e-ollama status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" \
      "$container_id" 2>/dev/null || true
    categories=$(bounded 15 docker logs --tail 100 "$container_id" 2>&1 |
      classify_diagnostic_stream) || categories=categories=none
    printf '%s %s\n' aura-e2e-ollama "$categories"
  fi
  container_id=$(docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
    ps -q aura-e2e-judge 2>/dev/null | head -n 1 || true)
  if [ -n "$container_id" ]; then
    docker inspect --format "aura-e2e-judge status={{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" \
      "$container_id" 2>/dev/null || true
    categories=$(bounded 15 docker logs --tail 100 "$container_id" 2>&1 |
      classify_diagnostic_stream) || categories=categories=none
    printf '%s %s\n' aura-e2e-judge "$categories"
  fi
}

capture_browser_evaluation_service_diagnostics() {
  local destination="$browser_artifact_root/evaluation-service-diagnostic.json"
  local worker_id scheduler_id runner_id worker_state scheduler_state runner_state
  local worker_categories scheduler_categories runner_categories
  worker_id=$(docker compose -p "$project" --env-file "$env_file" \
    -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks \
    ps -q observatory-evaluation-worker 2>/dev/null | head -n 1 || true)
  scheduler_id=$(docker compose -p "$project" --env-file "$env_file" \
    -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks \
    ps -q observatory-evaluation-scheduler 2>/dev/null | head -n 1 || true)
  runner_id=$(docker compose -p "$project" --env-file "$env_file" \
    -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks \
    ps -q aura-evaluation-runner 2>/dev/null | head -n 1 || true)
  worker_state='unavailable|unavailable|unavailable'
  scheduler_state='unavailable|unavailable|unavailable'
  runner_state='unavailable|unavailable|unavailable'
  worker_categories='categories=unavailable'
  scheduler_categories='categories=unavailable'
  runner_categories='categories=unavailable'
  if [ -n "$worker_id" ]; then
    worker_state=$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$worker_id" 2>/dev/null || printf '%s' 'unavailable|unavailable|unavailable')
    worker_categories=$(bounded 15 docker logs --tail 100 "$worker_id" 2>&1 |
      classify_diagnostic_stream) || worker_categories='categories=unavailable'
  fi
  if [ -n "$scheduler_id" ]; then
    scheduler_state=$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$scheduler_id" 2>/dev/null || printf '%s' 'unavailable|unavailable|unavailable')
    scheduler_categories=$(bounded 15 docker logs --tail 100 "$scheduler_id" 2>&1 |
      classify_diagnostic_stream) || scheduler_categories='categories=unavailable'
  fi
  if [ -n "$runner_id" ]; then
    runner_state=$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$runner_id" 2>/dev/null || printf '%s' 'unavailable|unavailable|unavailable')
    runner_categories=$(bounded 15 docker logs --tail 100 "$runner_id" 2>&1 |
      classify_diagnostic_stream) || runner_categories='categories=unavailable'
  fi
  python3 - "$destination" "$worker_state" "$worker_categories" \
    "$scheduler_state" "$scheduler_categories" "$runner_state" "$runner_categories" <<'PY'
import json
import sys
from pathlib import Path


def state(value: str) -> dict[str, object]:
    parts = value.split("|", 2)
    parts.extend(["unavailable"] * (3 - len(parts)))
    try:
        parsed_exit = int(parts[1])
    except ValueError:
        parsed_exit = None
    return {"status": parts[0][:32], "exitCode": parsed_exit, "health": parts[2][:32]}


def categories(value: str) -> dict[str, str]:
    # The classifier output contains category names/counts only; never persist
    # source log lines, identifiers, request bodies, or credentials.
    return {"summary": value[:512]}


Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "schemaVersion": 1,
            "reportKind": "evaluation-service-failure",
            "capturePolicy": "metadata-only",
            "services": {
                "observatory-evaluation-worker": {
                    "state": state(sys.argv[2]),
                    "logs": categories(sys.argv[3]),
                },
                "observatory-evaluation-scheduler": {
                    "state": state(sys.argv[4]),
                    "logs": categories(sys.argv[5]),
                },
                "aura-evaluation-runner": {
                    "state": state(sys.argv[6]),
                    "logs": categories(sys.argv[7]),
                },
            },
        },
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY
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
      emit_failure_diagnostics
    fi
    if [ -d "$browser_artifact_root" ]; then
      printf '%s\n' "Retaining bounded browser failure artifacts at $browser_artifact_root." >&2
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
  if [ "$status" -eq 0 ] && [ -d "$browser_artifact_root" ]; then
    rm -rf "$browser_artifact_root"
  fi
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
: > "$browser_target"
chmod 0600 "$browser_target"

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
random_secret() { openssl rand -hex 32; }
random_access_key() { printf 'GK%s' "$(openssl rand -hex 12)"; }
write_fixture_secret aura-core-database-password "$(random_secret)"
write_fixture_secret aura-oidc-client-secret "$(random_secret)"
write_fixture_secret aura-nats-password "$(random_secret)"
write_fixture_secret aura-nats-bootstrap-password "$(random_secret)"
write_fixture_secret aura-nats-dispatch-password "$(random_secret)"
write_fixture_secret aura-nats-runner-password "$(random_secret)"
write_fixture_secret aura-valkey-password "$(random_secret)"
write_fixture_secret aura-garage-rpc-secret "$(random_secret)"
write_fixture_secret aura-garage-access-key "$(random_access_key)"
write_fixture_secret aura-garage-secret-key "$(random_secret)"
write_fixture_secret observatory-garage-access-key "$(random_access_key)"
write_fixture_secret observatory-garage-secret-key "$(random_secret)"
# The API has its own write-only Garage principal.  Keep this fixture separate
# from the worker/judge credentials so the bootstrap authorization boundary is
# exercised by every composed run.
write_fixture_secret observatory-api-garage-access-key "$(random_access_key)"
write_fixture_secret observatory-api-garage-secret-key "$(random_secret)"
write_fixture_secret observatory-database-password "$(random_secret)"
write_fixture_secret observatory-oidc-client-secret "$(random_secret)"
write_fixture_secret observatory-session-password "$(random_secret)"
write_fixture_secret observatory-ingest-credential "$(random_secret)"
write_fixture_secret observatory-clickhouse-password "$(random_secret)"
write_fixture_secret observatory-judge-garage-access-key "$(random_access_key)"
write_fixture_secret observatory-judge-garage-secret-key "$(random_secret)"
write_fixture_secret aura-evaluation-runner-garage-access-key "$(random_access_key)"
write_fixture_secret aura-evaluation-runner-garage-secret-key "$(random_secret)"
write_fixture_secret aura-evaluation-runner-database-password "$(random_secret)"

# The synthetic runtime is a disposable, metadata-only component. Mount this
# fixture into the service-owned packaged manifest directory for the existing
# sync-manifests CLI; it is never copied into the repository image or normal
# deployment state.
cat >"$component_manifest" <<'EOF'
component:
  id: aura.evaluation.synthetic
  version: 1.0.0
  owner: observatory.evaluation_synthetic
  category: evaluation
input:
  schema: evaluation-synthetic-input-v1
  replayable: true
output:
  schema: evaluation-synthetic-output-v1
  replayable: true
dependencies:
  - evaluation_definition_state
  - core_evaluation_runner
metrics:
  - evaluation_synthetic_operation_count
  - evaluation_synthetic_operation_duration_ms
  - evaluation_synthetic_error_count
metric_semantics:
  evaluation_synthetic_operation_count: Bounded synthetic evaluation operations by fixed operation and outcome.
  evaluation_synthetic_operation_duration_ms: Synthetic evaluation operation latency histogram in milliseconds.
  evaluation_synthetic_error_count: Bounded synthetic evaluation errors by fixed operation and error class.
metric_definitions:
  - name: evaluation_synthetic_operation_count
    type: counter
    unit: operations
    labels: [operation, outcome]
    dependencies: [core_evaluation_runner]
    trace: evaluation_synthetic_operation
    correlation: run_id
    export: prometheus
  - name: evaluation_synthetic_operation_duration_ms
    type: histogram
    unit: milliseconds
    labels: [operation]
    dependencies: [core_evaluation_runner]
    trace: evaluation_synthetic_operation
    correlation: run_id
    export: prometheus
  - name: evaluation_synthetic_error_count
    type: counter
    unit: errors
    labels: [operation, error_class]
    dependencies: [core_evaluation_runner]
    trace: evaluation_synthetic_operation
    correlation: run_id
    export: prometheus
evaluators: [evaluation_e2e_deterministic]
capture_policy: metadata_only
retention: manual_owner_delete_only
cardinality:
  metric_attributes: [operation, outcome, error_class]
  trace_only: [trace_id, span_id, parent_span_id, dependency]
  prohibited: [prompt, response, content, filename, arguments, result, endpoint, token, credential, owner_identity]
EOF
chmod 0644 "$component_manifest"

# Create the external fixture provider first so its disposable network is
# included in the IPAM inventory below. The provider remains outside the Aura
# Compose project and is removed by the exit trap.
docker compose -p aura-e2e-provider -f "$ROOT/deploy/compose/e2e/provider.yaml" \
  up --build --detach --wait

# The normal private-network defaults are intentionally stable for operators,
# but an acceptance run must coexist with an already-running Aura stack. Pick
# one free /24 for the frontend and three free /28s for the other statically
# addressed networks by inspecting Docker's current network IPAM; no existing
# network is modified or removed. Staying within these reserved ranges avoids
# Docker's default-prone 172.30/31 pools while preserving the provider network
# in the inventory above.
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
  python3 - "$fixture_root/docker-networks.json" "$suffix" "${network_names[@]}" <<'PY'
import ipaddress
import json
import sys

network_path, suffix, *network_names = sys.argv[1:]
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

start = int(suffix, 36)
frontend = frontend_free[start % len(frontend_free)]
small_start = start % len(small_free)
selected = [small_free[(small_start + offset) % len(small_free)] for offset in range(len(network_names) - 1)]
print(network_names[0], frontend)
for name, subnet in zip(network_names[1:], selected):
    print(name, subnet)
PY
)
for network_name in "${network_names[@]}"; do
  test -n "${network_subnets[$network_name]:-}"
done
frontend_subnet=${network_subnets[aura-frontend]}
object_storage_subnet=${network_subnets[aura-object-storage]}
document_parser_subnet=${network_subnets[aura-document-parser]}
observatory_object_storage_subnet=${network_subnets[observatory-object-storage]}

cat > "$env_file" <<EOF
AURA_STATE_ROOT=/unused/aura-observatory-e2e-state
AURA_CORE_DATABASE_PASSWORD_FILE=/run/secrets/aura-core-database-password
AURA_OIDC_CLIENT_SECRET_FILE=/run/secrets/aura-oidc-client-secret
AURA_NATS_PASSWORD_FILE=/run/secrets/aura-nats-password
AURA_NATS_BOOTSTRAP_PASSWORD_FILE=/run/secrets/aura-nats-bootstrap-password
AURA_NATS_DISPATCH_PASSWORD_FILE=/run/secrets/aura-nats-dispatch-password
AURA_NATS_RUNNER_PASSWORD_FILE=/run/secrets/aura-nats-runner-password
AURA_VALKEY_PASSWORD_FILE=/run/secrets/aura-valkey-password
AURA_GARAGE_RPC_SECRET_FILE=/run/secrets/aura-garage-rpc-secret
AURA_GARAGE_ACCESS_KEY_FILE=/run/secrets/aura-garage-access-key
AURA_GARAGE_SECRET_KEY_FILE=/run/secrets/aura-garage-secret-key
AURA_OBSERVATORY_GARAGE_BUCKET=aura-observatory-evaluation
AURA_OBSERVATORY_GARAGE_ACCESS_KEY_FILE=/run/secrets/observatory-garage-access-key
AURA_OBSERVATORY_GARAGE_SECRET_KEY_FILE=/run/secrets/observatory-garage-secret-key
AURA_OBSERVATORY_API_GARAGE_ACCESS_KEY_FILE=/run/secrets/observatory-api-garage-access-key
AURA_OBSERVATORY_API_GARAGE_SECRET_KEY_FILE=/run/secrets/observatory-api-garage-secret-key
AURA_E2E_CORE_DATABASE_SECRET_SOURCE_FILE=$secret_root/aura-core-database-password
AURA_E2E_OIDC_CLIENT_SECRET_SOURCE_FILE=$secret_root/aura-oidc-client-secret
AURA_E2E_NATS_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-password
AURA_E2E_NATS_BOOTSTRAP_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-bootstrap-password
AURA_E2E_NATS_DISPATCH_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-dispatch-password
AURA_E2E_NATS_RUNNER_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-nats-runner-password
AURA_E2E_VALKEY_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-valkey-password
AURA_E2E_GARAGE_RPC_SECRET_SOURCE_FILE=$secret_root/aura-garage-rpc-secret
AURA_E2E_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-access-key
AURA_E2E_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/aura-garage-secret-key
AURA_E2E_OBSERVATORY_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-garage-access-key
AURA_E2E_OBSERVATORY_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-garage-secret-key
AURA_E2E_OBSERVATORY_API_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-api-garage-access-key
AURA_E2E_OBSERVATORY_API_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-api-garage-secret-key
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
AURA_E2E_OBSERVATORY_JUDGE_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-judge-garage-access-key
AURA_E2E_OBSERVATORY_JUDGE_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/observatory-judge-garage-secret-key
AURA_E2E_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE=$secret_root/aura-evaluation-runner-garage-access-key
AURA_E2E_EVALUATION_RUNNER_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE=$secret_root/aura-evaluation-runner-garage-secret-key
AURA_E2E_EVALUATION_RUNNER_DATABASE_PASSWORD_SECRET_SOURCE_FILE=$secret_root/aura-evaluation-runner-database-password
AURA_E2E_COMPONENT_MANIFEST_FILE=$component_manifest
AURA_CORE_DATABASE_NAME=aura
AURA_CORE_DATABASE_USER=aura
AURA_EVALUATION_RUNNER_DATABASE_USER=aura_evaluation_runner
AURA_NATS_BOOTSTRAP_USER=aura-evaluation-bootstrap
AURA_NATS_DISPATCH_USER=aura-evaluation-dispatch
AURA_NATS_RUNNER_USER=aura-evaluation-runner
AURA_OBSERVATORY_EVALUATION_WORKER_ID=aura0098-evaluation-worker
AURA_OBSERVATORY_EVALUATION_SCHEDULER_ID=aura0098-evaluation-scheduler
AURA_OBSERVATORY_EVALUATION_POLL_SECONDS=1
AURA_OBSERVATORY_EVALUATION_LEASE_SECONDS=5
AURA_OBSERVATORY_EVALUATION_RETRY_BASE_SECONDS=1
AURA_OBSERVATORY_EVALUATION_RETRY_MAX_SECONDS=4
AURA_EVALUATION_RUNNER_MAX_CONCURRENCY=1
AURA_EVALUATION_RUNNER_MAX_QUEUE_DEPTH=4
AURA_EVALUATION_RUNNER_MAX_TIMEOUT_MS=30000
AURA_EVALUATION_RUNNER_MAX_CASES=1
AURA_EVALUATION_RUNNER_MAX_MODEL_CALLS=1
AURA_EVALUATION_RUNNER_MAX_ARTIFACT_OPERATIONS=1
AURA_EVALUATION_RUNTIME_FIXTURE_REVISION=synthetic-run-coordinator-v1
AURA_EVALUATION_RUNTIME_IMAGE_DIGEST=0000000000000000000000000000000000000000000000000000000000000000
AURA_EVALUATION_RUNTIME_MODEL_ID=fixture-chat-2
AURA_EVALUATION_RUNTIME_MODEL_VERSION=1.0.0
AURA_EVALUATION_RUNTIME_MODEL_DIGEST=0000000000000000000000000000000000000000000000000000000000000000
AURA_EVALUATION_RUNTIME_SEED=98
AURA_EVALUATION_RUNTIME_PROMPT_REVISION=aura0098-AURA0090_PRIVACY_SENTINEL
AURA_EVALUATION_RUNTIME_POLICY_REVISION=aura0098-policy
AURA_TELEMETRY_DEPLOYMENT_ID=aura0098-e2e
AURA_TELEMETRY_GIT_COMMIT=0000000
AURA_TELEMETRY_CONFIGURATION_VERSION=aura0098
AURA_OBSERVATORY_EVALUATION_OBJECT_ENDPOINT=http://aura-garage:3900
AURA_OBSERVATORY_EVALUATION_OBJECT_REGION=garage
AURA_OBSERVATORY_EVALUATION_OBJECT_BUCKET=aura-observatory-evaluation
AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE=/run/secrets/observatory-judge-garage-access-key
AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE=/run/secrets/observatory-judge-garage-secret-key
AURA_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_FILE=/run/secrets/aura-evaluation-runner-garage-access-key
AURA_EVALUATION_RUNNER_GARAGE_SECRET_KEY_FILE=/run/secrets/aura-evaluation-runner-garage-secret-key
AURA_EVALUATION_RUNNER_DATABASE_PASSWORD_FILE=/run/secrets/aura-evaluation-runner-database-password
# The composed browser phase is enabled only after the owner/CSRF public
# admission preflight below has established the governed capture boundary.
AURA_OBSERVATORY_EVALUATION_FULL_STACK=0
AURA_E2E_OBSERVATORY_NGINX_CONFIG_FILE=$nginx_config
AURA_E2E_OBSERVATORY_BROWSER_STATE_FILE=$browser_state
AURA_E2E_BROWSER_TARGET_FILE=$browser_target
AURA_E2E_BROWSER_ARTIFACT_DIR=$browser_artifact_root
AURA_OBSERVATORY_PUBLIC_ORIGIN=http://observatory.localhost:4300
AURA_OBSERVATORY_OIDC_ISSUER=http://aura-e2e-oidc:8081
AURA_OBSERVATORY_OIDC_AUDIENCE=aura-observatory
AURA_OBSERVATORY_OIDC_CLIENT_ID=aura-observatory
AURA_OBSERVATORY_OIDC_REDIRECT_URI=http://observatory.localhost:4300/api/v1/auth/callback
AURA_OBSERVATORY_OWNER_SUBJECT=owner
AURA_OBSERVATORY_SECURE_COOKIES=false
AURA_FRONTEND_SUBNET=$frontend_subnet
AURA_OBJECT_STORAGE_SUBNET=$object_storage_subnet
AURA_DOCUMENT_PARSER_SUBNET=$document_parser_subnet
AURA_OBSERVATORY_OBJECT_STORAGE_SUBNET=$observatory_object_storage_subnet
EOF

# The provider and browser runner are separate Compose concerns.  The temporary
# override bridges only the test containers to the provider and reuses the
# existing Playwright image; it does not alter the tracked production graph.
nats_entrypoint_source="$ROOT/deploy/docker/nats-entrypoint.sh"
garage_template_source="$ROOT/deploy/docker/garage.toml"
cat > "$override" <<'EOF'
services:
  aura-web:
    ports: !override []
  aura-core-api:
    environment:
      AURA_ENVIRONMENT: test
      AURA_SECURE_COOKIES: "false"
      AURA_OLLAMA_URL: http://aura-e2e-ollama:8081
      AURA_TELEMETRY_OTLP_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
    secrets:
      - observatory-ingest-credential
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
      AURA_TELEMETRY_OTLP_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
    secrets:
      - observatory-ingest-credential
    networks:
      - aura-core-db
      - aura-core-bus
      - aura-core-cache
      - aura-object-storage
      - aura-document-parser
      - aura-core-egress
      - observatory-ingest
      - aura-e2e-provider
  aura-core-scheduler:
    environment:
      AURA_ENVIRONMENT: test
      AURA_TELEMETRY_OTLP_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
    secrets:
      - observatory-ingest-credential
    networks:
      - aura-core-db
      - observatory-ingest
  observatory-api:
    volumes:
      - ${AURA_E2E_COMPONENT_MANIFEST_FILE}:/app/services/aura-observatory/resources/component-manifests/aura-evaluation-synthetic.yaml:ro
    networks:
      - observatory-api
      - observatory-db
      - observatory-session
      - observatory-ingest
      - observatory-identity-egress
      - observatory-object-storage
      - aura-e2e-provider
    secrets:
      - observatory-database-password
      - observatory-oidc-client-secret
      - observatory-session-password
      - observatory-ingest-credential
      - observatory-clickhouse-password
      - observatory-api-garage-access-key
      - observatory-api-garage-secret-key
  observatory-evaluation-worker:
    environment:
      AURA_OBSERVATORY_ENVIRONMENT: disposable_local
      AURA_OBSERVATORY_EVALUATION_NATS_USER: aura-evaluation-dispatch
      AURA_OBSERVATORY_EVALUATION_NATS_PASSWORD_FILE: /run/secrets/aura-nats-dispatch-password
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED: "true"
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT: http://observatory-collector:4318/v1/traces
      AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
      AURA_OBSERVATORY_CLICKHOUSE_URL: http://observatory-clickhouse:8123
      AURA_OBSERVATORY_CLICKHOUSE_DATABASE: aura_observatory
      AURA_OBSERVATORY_CLICKHOUSE_USER: observatory
      AURA_OBSERVATORY_CLICKHOUSE_PASSWORD_FILE: /run/secrets/observatory-clickhouse-password
      AURA_OBSERVATORY_EVALUATION_OBJECT_ENDPOINT: http://aura-garage:3900
      AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE: /run/secrets/observatory-judge-garage-access-key
      AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE: /run/secrets/observatory-judge-garage-secret-key
      AURA_OBSERVATORY_LOCAL_JUDGE_ORIGIN: http://aura-e2e-judge:8081
      AURA_OBSERVATORY_LOCAL_JUDGE_TIMEOUT_SECONDS: "30"
      HTTP_PROXY: ""
      HTTPS_PROXY: ""
      ALL_PROXY: ""
      http_proxy: ""
      https_proxy: ""
      all_proxy: ""
      NO_PROXY: "*"
      no_proxy: "*"
    secrets:
      - observatory-database-password
      - observatory-clickhouse-password
      - observatory-ingest-credential
      - aura-nats-dispatch-password
      - observatory-judge-garage-access-key
      - observatory-judge-garage-secret-key
    networks:
      - observatory-db
      - observatory-ingest
      - observatory-object-storage
      - observatory-judge-egress
      - aura-evaluation-bus
      - aura-e2e-provider
  observatory-evaluation-scheduler:
    environment:
      AURA_OBSERVATORY_ENVIRONMENT: disposable_local
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED: "true"
      AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT: http://observatory-collector:4318/v1/traces
      AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
    secrets:
      - observatory-database-password
      - observatory-ingest-credential
    networks:
      - observatory-db
      - observatory-ingest
  aura-evaluation-runner:
    environment:
      AURA_ENVIRONMENT: test
      AURA_OLLAMA_URL: http://aura-e2e-ollama:8081
      AURA_EVALUATION_RUNTIME_FIXTURE_REVISION: synthetic-run-coordinator-v1
      AURA_EVALUATION_RUNTIME_IMAGE_DIGEST: 0000000000000000000000000000000000000000000000000000000000000000
      AURA_EVALUATION_RUNTIME_MODEL_ID: fixture-chat-2
      AURA_EVALUATION_RUNTIME_MODEL_VERSION: 1.0.0
      AURA_EVALUATION_RUNTIME_MODEL_DIGEST: 0000000000000000000000000000000000000000000000000000000000000000
      AURA_EVALUATION_RUNTIME_SEED: "98"
      AURA_EVALUATION_RUNTIME_PROMPT_REVISION: aura0098-AURA0090_PRIVACY_SENTINEL
      AURA_EVALUATION_RUNTIME_POLICY_REVISION: aura0098-policy
      AURA_TELEMETRY_DEPLOYMENT_ID: aura0098-e2e
      AURA_TELEMETRY_GIT_COMMIT: "0000000"
      AURA_TELEMETRY_CONFIGURATION_VERSION: aura0098
      AURA_TELEMETRY_OTLP_ENABLED: "true"
      AURA_TELEMETRY_OTLP_CREDENTIAL_FILE: /run/secrets/observatory-ingest-credential
    secrets:
      - aura-evaluation-runner-database-password
      - aura-nats-runner-password
      - aura-evaluation-runner-garage-access-key
      - aura-evaluation-runner-garage-secret-key
      - observatory-ingest-credential
    networks:
      - aura-core-db
      - aura-evaluation-bus
      - observatory-object-storage
      - observatory-ingest
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
      - __AURA_NATS_ENTRYPOINT_SOURCE__:/usr/local/bin/aura-nats-entrypoint:ro
  aura-garage:
    volumes: !override
      - core_garage_data:/var/lib/garage
      - core_garage_config:/run/garage-config:ro
  aura-garage-config:
    volumes: !override
      - core_garage_config:/run/garage-config
      - __AURA_GARAGE_TEMPLATE_SOURCE__:/etc/garage/garage.toml.template:ro
  aura-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  observatory-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  aura-evaluation-runner-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  observatory-judge-garage-bootstrap:
    volumes: !override
      - core_garage_data:/var/lib/garage:ro
      - core_garage_config:/run/garage-config:ro
  observatory-api-garage-bootstrap:
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
      AURA_OBSERVATORY_EVALUATION_FULL_STACK: "${AURA_OBSERVATORY_EVALUATION_FULL_STACK:-0}"
      AURA_OBSERVATORY_BASE_URL: http://observatory.localhost:4300
      AURA_OBSERVATORY_FULL_STACK_BASE_URL: http://observatory.localhost:4300
      AURA_OBSERVATORY_BROWSER_HOST_RESOLVER_RULES: MAP observatory.localhost observatory-web
      AURA_OBSERVATORY_CORE_BASE_URL: http://aura-web:8080
      AURA_OBSERVATORY_CORE_MODEL: fixture-chat-2
      AURA_OBSERVATORY_RUN_PROMPT: AURA0090_PRIVACY_SENTINEL
      AURA_OBSERVATORY_PRIVACY_SENTINEL: AURA0090_PRIVACY_SENTINEL
      AURA_OBSERVATORY_STORAGE_STATE_PATH: /tmp/observatory-browser-state.json
      AURA_E2E_BROWSER_TARGET_FILE: /tmp/observatory-browser-target.json
      AURA_E2E_BROWSER_ARTIFACT_DIR: /tmp/aura-e2e-artifacts
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
      - ${AURA_E2E_BROWSER_TARGET_FILE}:/tmp/observatory-browser-target.json
      - ${AURA_E2E_BROWSER_ARTIFACT_DIR}:/tmp/aura-e2e-artifacts
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
  aura-nats-bootstrap-password: !override
    file: ${AURA_E2E_NATS_BOOTSTRAP_PASSWORD_SECRET_SOURCE_FILE}
  aura-nats-dispatch-password: !override
    file: ${AURA_E2E_NATS_DISPATCH_PASSWORD_SECRET_SOURCE_FILE}
  aura-nats-runner-password: !override
    file: ${AURA_E2E_NATS_RUNNER_PASSWORD_SECRET_SOURCE_FILE}
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
  observatory-api-garage-access-key: !override
    file: ${AURA_E2E_OBSERVATORY_API_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE}
  observatory-api-garage-secret-key: !override
    file: ${AURA_E2E_OBSERVATORY_API_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE}
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
  observatory-judge-garage-access-key: !override
    file: ${AURA_E2E_OBSERVATORY_JUDGE_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE}
  observatory-judge-garage-secret-key: !override
    file: ${AURA_E2E_OBSERVATORY_JUDGE_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE}
  aura-evaluation-runner-garage-access-key: !override
    file: ${AURA_E2E_EVALUATION_RUNNER_GARAGE_ACCESS_KEY_SECRET_SOURCE_FILE}
  aura-evaluation-runner-garage-secret-key: !override
    file: ${AURA_E2E_EVALUATION_RUNNER_GARAGE_SECRET_KEY_SECRET_SOURCE_FILE}
  aura-evaluation-runner-database-password: !override
    file: ${AURA_E2E_EVALUATION_RUNNER_DATABASE_PASSWORD_SECRET_SOURCE_FILE}
volumes:
  core_postgres_data:
  core_nats_data:
  core_garage_data:
  core_garage_config:
  observatory_postgres_data:
  observatory_clickhouse_data:
  observatory_clickhouse_logs:
  observatory_session_data:
EOF

# The override is generated under the ignored fixture directory, so relative
# host paths would resolve there. Substitute only these two tracked, read-only
# sources after the quoted heredoc while retaining Compose interpolation for
# all other generated values.
sed -i \
  -e "s|__AURA_NATS_ENTRYPOINT_SOURCE__|$nats_entrypoint_source|g" \
  -e "s|__AURA_GARAGE_TEMPLATE_SOURCE__|$garage_template_source|g" \
  "$override"

# Docker otherwise allocates the unbounded internal networks from its global
# default pools. Override every network used by this full-stack fixture so a
# host with many saved Compose projects cannot exhaust those pools. The
# provider attachment remains external and is deliberately not recreated.
{
  printf '%s\n' 'networks:'
  for network_name in "${network_names[@]}"; do
    printf '  %s:\n    ipam: !override\n      config:\n        - subnet: %s\n' \
      "$network_name" "${network_subnets[$network_name]}"
  done
  printf '%s\n' '  aura-e2e-provider:' '    external: true' '    name: aura-e2e-provider'
} >> "$override"

compose=(docker compose -p "$project" --env-file "$env_file" -f "$ROOT/compose.yaml" -f "$override" --profile observatory --profile checks)
start_timeout=${AURA_E2E_COMPOSE_START_TIMEOUT_SECONDS:-900}
step_timeout=${AURA_E2E_COMPOSE_STEP_TIMEOUT_SECONDS:-180}

ensure_fresh_clickhouse_volumes() {
  local volume project_label
  for volume in "${project}_observatory_clickhouse_data" "${project}_observatory_clickhouse_logs"; do
    if ! docker volume inspect "$volume" >/dev/null 2>&1; then
      continue
    fi
    project_label=$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' "$volume" 2>/dev/null || true)
    if [ "$project_label" != "$project" ]; then
      printf 'AURA_E2E refusing to remove unowned ClickHouse volume %s\n' "$volume" >&2
      return 1
    fi
    if ! docker volume rm "$volume" >/dev/null; then
      printf 'AURA_E2E could not remove stale disposable ClickHouse volume %s\n' "$volume" >&2
      return 1
    fi
  done
  printf '%s\n' 'AURA_E2E ClickHouse disposable volumes=fresh'
}

ensure_fresh_clickhouse_volumes

wait_running() {
  local deadline=$((SECONDS + step_timeout))
  local service container_id
  while (( SECONDS < deadline )); do
    for service in "$@"; do
      container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
      if [ -z "$container_id" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null || true)" != true ]; then
        container_id=''
        break
      fi
    done
    if [ -n "${container_id:-}" ]; then
      return 0
    fi
    sleep 1
  done
  printf '%s\n' 'AURA_E2E startup timeout waiting for running services' >&2
  return 1
}

wait_healthy() {
  local deadline=$((SECONDS + step_timeout))
  local service container_id health
  while (( SECONDS < deadline )); do
    for service in "$@"; do
      container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null | head -n 1 || true)
      health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
      if [ -z "$container_id" ] || [ "$health" != healthy ]; then
        container_id=''
        break
      fi
    done
    if [ -n "${container_id:-}" ]; then
      return 0
    fi
    sleep 1
  done
  printf '%s\n' 'AURA_E2E health timeout waiting for services' >&2
  return 1
}

admit_public_capture_authorization() {
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY observatory-api \
    /app/.venv/bin/python - <<'PY'
import asyncio
import hashlib
import json
import httpx
import redis.asyncio
from aura_observatory.auth import ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings

AUTHORIZATION = "00000000-0000-4000-8000-000000009086"
DATASET = "00000000-0000-4000-8000-000000009081"
SUITE = "00000000-0000-4000-8000-000000009083"
EVALUATOR = "84504a1b-189c-5d5b-a046-b5f73dc3ddfe"
AT = "2026-01-01T00:00:00Z"

async def main() -> None:
    settings = Settings()
    store = redis.asyncio.from_url(settings.session_store_url, password=settings.session_store_password, decode_responses=True)
    backend = RedisSessionBackend(store, namespace=settings.session_namespace)
    auth = ObservatoryAuth(settings, backend, backend)
    session_id, session = await auth.create_session(Principal(settings.oidc_issuer, settings.owner_subject, "E2E owner"))
    headers = {"Origin": settings.public_origin, "X-CSRF-Token": session.csrf_token}
    async with httpx.AsyncClient(base_url="http://observatory-api:8000", timeout=30, cookies={settings.effective_session_cookie_name: session_id}) as client:
        async def accepted(response: httpx.Response, label: str, expected: tuple[int, ...] = (201, 409)) -> None:
            if response.status_code not in expected:
                raise SystemExit(f"public {label} admission failed status={response.status_code}")

        response = await client.post("/api/v1/evaluation/capture-authorizations", json={
            "authorizationRecordId": AUTHORIZATION, "requestId": "00000000-0000-4000-8000-000000009087",
            "dataset": {"id": DATASET, "version": "1.0.0"}, "captureClassification": "full_content",
            "authorizationPurpose": "synthetic_benchmark", "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only", "authorizedAt": AT},
            headers={**headers, "Idempotency-Key": "00000000-0000-4000-8000-000000009088"})
        await accepted(response, "capture authorization")

        component = {"componentId":"aura.evaluation.synthetic", "componentVersion":"1.0.0", "codeRevision":"0000000", "imageDigest":"0" * 64, "configurationRevision":"aura0098"}
        def metadata(artifact_id: str, purpose: str, digest: str, byte_count: int) -> dict:
            return {"artifactId": artifact_id, "artifactVersion":"1.0.0", "purpose":purpose, "mediaClassification":"evaluation_input", "mediaType":"text/plain", "byteCount":byte_count, "integrity":{"algorithm":"sha256","digest":digest}, "captureAuthorization":{"captureClassification":"full_content","authorizationPurpose":"synthetic_benchmark","authorizationContext":"controlled_evaluation","retentionMode":"manual_only","authorizationRecordId":AUTHORIZATION,"authorizedAt":AT}, "retentionMode":"manual_only", "provenance":{"dataset":{"resourceId":DATASET,"resourceVersion":"1.0.0"},"suite":{"resourceId":SUITE,"resourceVersion":"1.0.0"},"evaluators":[{"evaluatorId":EVALUATOR,"evaluatorVersion":"1.0.0","kind":"local_judge"}],"components":[component],"model":{"modelId":"fixture","modelVersion":"1.0.0","modelDigest":"a" * 64},"deploymentRevision":"1.0.0","reproducibility":{"seed":1,"clockMode":"fixed","timeZone":"UTC","locale":"en-US","fixtureRevision":"1.0.0","promptRevision":"1.0.0","policyRevision":"1.0.0"}},"createdAt":AT}
        prompt, rubric = b"bounded local judge prompt", b"bounded local judge rubric"
        for artifact_id, purpose, body, key in (("00000000-0000-4000-8000-000000009084", "judge_prompt", prompt, "00000000-0000-4000-8000-000000009090"), ("00000000-0000-4000-8000-000000009085", "judge_rubric", rubric, "00000000-0000-4000-8000-000000009091")):
            digest = hashlib.sha256(body).hexdigest()
            response = await client.post("/api/v1/evaluation/artifacts", files={"metadata": (None, json.dumps(metadata(artifact_id, purpose, digest, len(body))), "application/json"), "content": (artifact_id + ".txt", body, "text/plain")}, headers={**headers, "Idempotency-Key": key})
            await accepted(response, "artifact")
        judge = {"evaluatorId":EVALUATOR,"evaluatorVersion":"1.0.0","metric":"evaluation.local_judge.score","componentIds":["aura.evaluation.synthetic"],"judgeDefinitionId":EVALUATOR,"judgeDefinitionVersion":"1.0.0","model":{"provider":"local_ollama","modelName":"fixture","modelVersion":"1.0.0","modelDigest":"a" * 64},"prompt":{"artifactId":"00000000-0000-4000-8000-000000009084","artifactVersion":"1.0.0","purpose":"judge_prompt","digest":hashlib.sha256(prompt).hexdigest()},"rubric":{"artifactId":"00000000-0000-4000-8000-000000009085","artifactVersion":"1.0.0","purpose":"judge_rubric","digest":hashlib.sha256(rubric).hexdigest()},"calibration":{"calibrationId":EVALUATOR,"calibrationVersion":"1.0.0"},"seed":1,"budget":{"timeoutMs":1000,"maxInputBytes":64,"maxOutputBytes":64,"maxOutputTokens":8,"maxContextTokens":64,"maxConcurrency":1,"temperature":0},"outputContract":"local_judge_score_v1","defaultDisposition":"advisory_only","retentionMode":"manual_only","provenance":{"createdByComponent":"aura.evaluation.synthetic","createdByVersion":"1.0.0","sourceDigest":"a" * 64},"createdAt":AT}
        response = await client.post("/api/v1/evaluation/evaluators", json=judge, headers={**headers, "Idempotency-Key":"00000000-0000-4000-8000-000000009092"})
        await accepted(response, "local-judge")
    await store.close()
    print("AURA_E2E public owner capture authorization, artifact upload, and local-judge admission passed")

asyncio.run(main())
PY
}

seed_evaluation_definitions() {
  # The CLI synchronizes the E2E-only component through the existing catalogue
  # boundary. The typed definition service then admits evaluator, dataset,
  # case, and suite records without opening a SQL connection in the harness.
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY observatory-api \
    /app/.venv/bin/python -m aura_observatory.entrypoints.cli sync-manifests
  for signal in \
    evaluation_synthetic_operation_count \
    evaluation_synthetic_operation_duration_ms \
    evaluation_synthetic_error_count \
    'trace: evaluation_synthetic_operation' \
    'correlation: run_id'; do
    if ! rg -Fq "$signal" "$component_manifest"; then
      printf 'AURA_E2E synthetic component manifest missing bounded signal: %s\n' "$signal" >&2
      return 1
    fi
  done
  printf '%s\n' 'AURA_E2E synthetic component manifest bounded metrics and trace semantics=verified'
  bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY observatory-api \
    /app/.venv/bin/python - <<'PY'
import asyncio

from aura_observatory.composition import (
    build_evaluation_definition_repository,
    build_evaluation_definition_service,
)
from aura_observatory.evaluation_definitions import EvaluatorDefinition
from aura_observatory.settings import Settings

DATASET_ID = "00000000-0000-4000-8000-000000009081"
CASE_ID = "00000000-0000-4000-8000-000000009082"
SUITE_ID = "00000000-0000-4000-8000-000000009083"
EVALUATOR_ID = "84504a1b-189c-5d5b-a046-b5f73dc3ddfe"
VERSION = "1.0.0"
CREATED_AT = "2026-01-01T00:00:00Z"
AUTHORIZATION = {
    "captureClassification": "metadata_only",
    "authorizationPurpose": "synthetic_benchmark",
    "authorizationContext": "controlled_evaluation",
    "retentionMode": "manual_only",
}

PROVENANCE = {
    "createdByComponent": "aura.evaluation.synthetic",
    "createdByVersion": VERSION,
    "sourceDigest": "1111111111111111111111111111111111111111111111111111111111111111",
}
COMPONENT = {
    "componentId": "aura.evaluation.synthetic",
    "componentVersion": VERSION,
    "codeRevision": "0000000",
    "imageDigest": "0000000000000000000000000000000000000000000000000000000000000000",
    "configurationRevision": "aura0098",
}
EVALUATOR = {
    "schemaVersion": 1,
    "resourceType": "evaluator",
    "evaluatorId": EVALUATOR_ID,
    "evaluatorVersion": VERSION,
    "kind": "deterministic",
    "metric": "evaluation.exact.ratio",
    "componentIds": ["aura.evaluation.synthetic"],
    "retentionMode": "manual_only",
    "provenance": {
        **PROVENANCE,
        "sourceDigest": "2222222222222222222222222222222222222222222222222222222222222222",
    },
    "createdAt": CREATED_AT,
}
DATASET = {
    "schemaVersion": 1,
    "resourceType": "dataset",
    "datasetId": DATASET_ID,
    "datasetVersion": VERSION,
    "sourceKind": "synthetic",
    "scopes": ["public_project"],
    "captureAuthorization": AUTHORIZATION,
    "retentionMode": "manual_only",
    "provenance": PROVENANCE,
    "caseIds": [CASE_ID],
    "caseCount": 1,
    "createdAt": CREATED_AT,
}
CASE = {
    "schemaVersion": 1,
    "resourceType": "case",
    "caseId": CASE_ID,
    "caseVersion": VERSION,
    "sourceKind": "synthetic",
    "scopes": ["public_project"],
    "captureAuthorization": AUTHORIZATION,
    "retentionMode": "manual_only",
    "provenance": PROVENANCE,
    "createdAt": CREATED_AT,
    "dataset": {"id": DATASET_ID, "version": VERSION},
    "expectations": {
        "exact": {
            "fields": [
                {
                    "fieldId": "synthetic_result_digest",
                    "expectedDigest": "0000000000000000000000000000000000000000000000000000000000000000",
                    "normalization": "none",
                }
            ]
        }
    },
}
SUITE = {
    "schemaVersion": 1,
    "resourceType": "suite",
    "suiteId": SUITE_ID,
    "suiteVersion": VERSION,
    "dataset": {"id": DATASET_ID, "version": VERSION},
    "evaluators": [{"evaluatorId": EVALUATOR_ID, "evaluatorVersion": VERSION, "kind": "deterministic"}],
    "components": [COMPONENT],
    "limits": {
        "timeoutMs": 30000,
        "maxConcurrency": 1,
        "maxAttempts": 1,
        "maxCases": 1,
        "maxInputBytes": 0,
        "maxOutputBytes": 0,
        "maxTokens": 0,
    },
    "manualThresholds": [],
    "variantBindings": [],
    "retentionMode": "manual_only",
    "provenance": {
        **PROVENANCE,
        "sourceDigest": "3333333333333333333333333333333333333333333333333333333333333333",
    },
    "createdAt": CREATED_AT,
}


async def main() -> None:
    settings = Settings()
    repository = build_evaluation_definition_repository(settings)
    service = build_evaluation_definition_service(settings)
    expected = EvaluatorDefinition.from_payload(EVALUATOR).to_storage()["fingerprint"]
    existing = await repository.get_evaluator(EVALUATOR_ID, VERSION)
    if existing is None:
        await service.admit_evaluator(EVALUATOR)
    elif existing.to_storage()["fingerprint"] != expected:
        raise SystemExit("AURA_E2E evaluator fixture conflicts with existing immutable state")
    await service.admit_dataset(DATASET)
    await service.admit_case(CASE)
    await service.admit_suite(SUITE)
    print("AURA_E2E public application definition seed passed")


asyncio.run(main())
PY
  admit_public_capture_authorization
  if ! sed -i 's/^AURA_OBSERVATORY_EVALUATION_FULL_STACK=0$/AURA_OBSERVATORY_EVALUATION_FULL_STACK=1/' "$env_file"; then
    printf '%s\n' 'AURA_E2E could not enable the evaluation browser phase after seeding' >&2
    return 1
  fi
  grep -Fqx 'AURA_OBSERVATORY_EVALUATION_FULL_STACK=1' "$env_file"
}

verify_evaluation_runner_database_access() {
  # Exercise the actual long-lived runner identity after migrations and role
  # bootstrap have completed.  The transaction is always rolled back, so this
  # proves the required read/write grants without leaving fixture rows behind.
  # Keep this boundary before Playwright: a database permission regression must
  # be reported as a Compose/bootstrap failure, not as a browser timeout.
  if ! bounded "$step_timeout" "${compose[@]}" run --rm --no-deps --no-TTY \
    aura-evaluation-runner /app/.venv/bin/python - <<'PY' >/dev/null 2>/dev/null
import asyncio
from uuid import uuid4

from sqlalchemy import text

from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine


async def main() -> None:
    settings = Settings(role="evaluation-runner")
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                attempt_id = uuid4()
                request_id = uuid4()
                job_id = uuid4()
                idempotency_key = uuid4()
                message_id = uuid4()
                cancellation_id = uuid4()
                await connection.execute(
                    text("SELECT 1 FROM evaluation_runner_attempts LIMIT 1")
                )
                await connection.execute(
                    text("SELECT 1 FROM evaluation_runner_terminal_outbox LIMIT 1")
                )
                await connection.execute(
                    text("SELECT 1 FROM evaluation_runner_cancellations LIMIT 1")
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO evaluation_runner_attempts
                            (attempt_id, request_id, job_id, idempotency_key,
                             request_digest, request_projection, status)
                        VALUES
                            (:attempt_id, :request_id, :job_id, :idempotency_key,
                             :request_digest, CAST(:request_projection AS JSONB), 'queued')
                        """
                    ),
                    {
                        "attempt_id": attempt_id,
                        "request_id": request_id,
                        "job_id": job_id,
                        "idempotency_key": idempotency_key,
                        "request_digest": "0" * 64,
                        "request_projection": "{}",
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE evaluation_runner_attempts
                        SET status = 'leased', lease_owner = 'aura0098-db-gate',
                            lease_generation = :lease_generation,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE attempt_id = :attempt_id
                        """
                    ),
                    {"attempt_id": attempt_id, "lease_generation": uuid4()},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO evaluation_runner_terminal_outbox
                            (attempt_id, message_id, payload, publication_status)
                        VALUES (:attempt_id, :message_id, CAST(:payload AS JSONB), 'pending')
                        """
                    ),
                    {
                        "attempt_id": attempt_id,
                        "message_id": message_id,
                        "payload": "{}",
                    },
                )
                await connection.execute(
                    text(
                        """
                        UPDATE evaluation_runner_terminal_outbox
                        SET publication_status = 'published',
                            published_at = CURRENT_TIMESTAMP
                        WHERE attempt_id = :attempt_id
                        """
                    ),
                    {"attempt_id": attempt_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO evaluation_runner_cancellations
                            (attempt_id, cancellation_id, request_id, job_id,
                             idempotency_key, request_digest, deadline_at, reason)
                        VALUES (:attempt_id, :cancellation_id, :request_id, :job_id,
                                :idempotency_key, :request_digest,
                                CURRENT_TIMESTAMP + INTERVAL '1 minute', 'SHUTDOWN')
                        """
                    ),
                    {
                        "attempt_id": attempt_id,
                        "cancellation_id": cancellation_id,
                        "request_id": request_id,
                        "job_id": job_id,
                        "idempotency_key": idempotency_key,
                        "request_digest": "0" * 64,
                    },
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


asyncio.run(main())
PY
  then
    printf '%s\n' 'AURA_E2E evaluation-runner database identity read/write gate failed closed' >&2
    return 1
  fi
  printf '%s\n' 'AURA_E2E evaluation-runner database identity read/write gate passed'
}

# shellcheck source=deploy/compose/e2e/core-availability.sh
. "$ROOT/deploy/compose/e2e/core-availability.sh"

assert_visibility_probe_runtime() {
  local label container_id
  for label in core observatory; do
    if [ "$label" = core ]; then
      container_id=$core_api_id
      if ! bounded 20 docker exec -i "$container_id" uv run python - <<'PY' >/dev/null 2>&1
import importlib

for module in ("redis.asyncio", "aura_core.platform.auth"):
    importlib.import_module(module)
PY
      then
        printf '%s\n' 'AURA_E2E visibility probe runtime prerequisite unavailable for Core' >&2
        return 1
      fi
    else
      container_id=$observatory_api_id
      if ! bounded 20 docker exec -i "$container_id" uv run python - <<'PY' >/dev/null 2>&1
import importlib

for module in ("redis.asyncio", "httpx", "aura_observatory.auth", "aura_observatory.settings"):
    importlib.import_module(module)
PY
      then
        printf '%s\n' 'AURA_E2E visibility probe runtime prerequisite unavailable for Observatory' >&2
        return 1
      fi
    fi
  done
  printf '%s\n' 'AURA_E2E visibility probe production runtimes and imports=verified'
}

probe_clickhouse_observations_table() {
  local phase=$1 api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  docker exec -i "$api_id" uv run python - "$phase" <<'PY'
import json
import sys

import httpx
from aura_observatory.settings import Settings

phase = sys.argv[1]
settings = Settings()
try:
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            settings.clickhouse_url,
            params={"database": settings.clickhouse_database},
            auth=(settings.clickhouse_user, settings.clickhouse_password),
            content="SELECT count() AS observation_count FROM observations FORMAT JSONEachRow",
        )
except httpx.HTTPError as exc:
    raise SystemExit(f"ClickHouse observations table usability failed phase={phase} transport") from exc
if not response.is_success:
    raise SystemExit(
        f"ClickHouse observations table usability failed phase={phase} status={response.status_code}"
    )
try:
    row = json.loads(next(line for line in response.text.splitlines() if line.strip()))
    count = int(row["observation_count"])
except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(
        f"ClickHouse observations table usability failed phase={phase} response-shape"
    ) from exc
if count < 0:
    raise SystemExit(f"ClickHouse observations table usability failed phase={phase} invalid-count")
print(f"AURA_E2E phase={phase} ClickHouse observations table usability=passed")
PY
}

pre_browser_visibility_run_id=''
run_core_observatory_visibility_probe() {
  local stage=${1:-initial}
  local core_api_id observatory_api_id run_payload run_id
  local export_before export_after ingest_before ingest_after
  core_api_id=$("${compose[@]}" ps -q aura-core-api 2>/dev/null | head -n 1 || true)
  observatory_api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$core_api_id" ] && [ -n "$observatory_api_id" ] || return 1
  assert_visibility_probe_runtime || return 1
  export_before=$(capture_core_telemetry_export_stats) || return 1
  ingest_before=$(capture_observatory_ingest_signal) || return 1

  run_payload=$(docker exec -i "$core_api_id" uv run python - "$stage" <<'PY'
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

import redis.asyncio
from aura_core.domains.governance.identity.models import Principal
from aura_core.platform.auth import RedisSessionBackend, SessionService, Settings


async def main() -> None:
    stage = sys.argv[1]
    settings = Settings(role="api")
    redis_client = redis.asyncio.from_url(settings.valkey_url, decode_responses=True)
    sessions = SessionService(RedisSessionBackend(redis_client), settings)
    session_id, session = await sessions.create(
        Principal(settings.oidc_issuer, settings.owner_subject, "Disposable visibility probe")
    )
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"{settings.session_cookie.name}={session_id}",
        "Origin": settings.public_origin,
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": f"aura0098-core-observatory-visibility-{stage}",
    }
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/conversations",
        data=json.dumps({
            "message": "AURA0098_CORE_OBSERVATORY_VISIBILITY_PROBE",
            "modelId": "fixture-chat-2",
        }).encode(),
        headers=headers,
        method="POST",
    )
    try:
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                status = response.status
                accepted = json.loads(response.read(65536))
        except urllib.error.HTTPError as error:
            raise SystemExit(f"Core visibility admission failed status={error.code}") from None
        if status != 202 or not isinstance(accepted, dict):
            raise SystemExit(f"Core visibility admission failed status={status}")
        conversation = accepted.get("conversation")
        run = accepted.get("run")
        conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
        run_id = run.get("id") if isinstance(run, dict) else None
        if not isinstance(conversation_id, str) or not isinstance(run_id, str):
            raise SystemExit("Core visibility admission omitted run correlation")
        detail_request = urllib.request.Request(
            f"http://127.0.0.1:8000/api/v1/conversations/{conversation_id}",
            headers={"Cookie": f"{settings.session_cookie.name}={session_id}"},
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(detail_request, timeout=5) as response:
                    detail = json.loads(response.read(65536))
            except (OSError, urllib.error.HTTPError, ValueError):
                time.sleep(1)
                continue
            candidates = [
                item for item in detail.get("recentRuns", []) if isinstance(item, dict)
            ] if isinstance(detail, dict) else []
            current = detail.get("currentRun") if isinstance(detail, dict) else None
            if isinstance(current, dict):
                candidates.append(current)
            target = next((item for item in candidates if item.get("id") == run_id), None)
            if isinstance(target, dict) and target.get("status") == "completed":
                print(json.dumps({"conversationId": conversation_id, "runId": run_id}))
                return
            if isinstance(target, dict) and target.get("status") in {
                "failed", "canceled", "cancelled", "interrupted"
            }:
                raise SystemExit("Core visibility probe reached a terminal failure")
            time.sleep(1)
        raise SystemExit("Core visibility probe did not complete before deadline")
    finally:
        await sessions.revoke(session_id)
        await redis_client.aclose()


asyncio.run(main())
PY
  ) || return 1
  if [ -z "$run_payload" ]; then
    printf '%s\n' 'AURA_E2E Core visibility probe returned no correlation payload' >&2
    return 1
  fi
  run_id=$(printf '%s\n' "$run_payload" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["runId"])') || return 1
  pre_browser_visibility_run_id=$run_id

  docker exec -i -e "AURA_E2E_VISIBILITY_RUN_ID=$run_id" "$observatory_api_id" \
    uv run python - <<'PY'
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

import redis.asyncio
from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


async def main() -> None:
    settings = Settings(role="api")
    run_id = os.environ.get("AURA_E2E_VISIBILITY_RUN_ID")
    if not isinstance(run_id, str) or not run_id:
        raise SystemExit("visibility run correlation was not provided")
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E visibility owner")
    )
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:8000/api/v1/runs/{run_id}",
            headers={
                "Cookie": f"{settings.effective_session_cookie_name}={session_id}",
                "Origin": settings.public_origin,
            },
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read(1_000_000))
            except urllib.error.HTTPError as error:
                if error.code not in {404, 503}:
                    raise SystemExit(
                        f"Observatory visibility query failed status={error.code}"
                    ) from None
                time.sleep(1)
                continue
            except (OSError, ValueError):
                time.sleep(1)
                continue
            observations = payload.get("observations") if isinstance(payload, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("runId") == run_id
                and isinstance(observations, list)
                and observations
            ):
                print(json.dumps({"runId": run_id, "observationCount": min(len(observations), 256)}))
                return
            time.sleep(1)
        raise SystemExit("Core run was not visible through Observatory before deadline")
    finally:
        await auth.sessions.delete(session_id)
        await redis_client.aclose()


asyncio.run(main())
PY

  export_after=$(capture_core_telemetry_export_stats) || return 1
  ingest_after=$(capture_observatory_ingest_signal) || return 1
  python3 - "$export_before" "$export_after" "$ingest_before" "$ingest_after" <<'PY'
import json
import math
import sys

export_before, export_after, ingest_before, ingest_after = map(json.loads, sys.argv[1:])
core_names = (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "dropped",
    "rejected",
    "abandoned_batches",
    "export_failures",
)
for name in core_names:
    before = float(export_before[name])
    after = float(export_after[name])
    if not math.isfinite(before) or not math.isfinite(after) or before < 0 or after < before:
        raise SystemExit("Core exporter failure counters were not bounded and monotonic")
for name in ("accepted", "rejected", "duplicate", "malformed", "storageFailure"):
    before = ingest_before.get(name)
    after = ingest_after.get(name)
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        raise SystemExit("Observatory ingest counters were unavailable")
    if not math.isfinite(float(before)) or not math.isfinite(float(after)) or before < 0 or after < before:
        raise SystemExit("Observatory ingest failure counters were not bounded and monotonic")
if float(ingest_after["accepted"]) <= float(ingest_before["accepted"]):
    raise SystemExit("Core run did not advance the Observatory accepted-ingest counter")
print(json.dumps({
    "core_export_deltas": {
        name: float(export_after[name]) - float(export_before[name])
        for name in core_names
    },
    "observatory_ingest_accepted_delta": float(ingest_after["accepted"])
    - float(ingest_before["accepted"]),
    "observatory_ingest_rejected_delta": float(ingest_after["rejected"])
    - float(ingest_before["rejected"]),
    "observatory_ingest_duplicate_delta": float(ingest_after["duplicate"])
    - float(ingest_before["duplicate"]),
    "observatory_ingest_malformed_delta": float(ingest_after["malformed"])
    - float(ingest_before["malformed"]),
    "observatory_ingest_storage_failure_delta": float(ingest_after["storageFailure"])
    - float(ingest_before["storageFailure"]),
}, separators=(",", ":")))
PY
  printf 'AURA_E2E project=%s stage=%s run_id=%s core_export_to_collector_to_observatory=visible\n' \
    "$project" "$stage" "$run_id"
}

read_browser_target() {
  browser_target_run_id=''
  browser_target_conversation_id=''
  browser_target_retention_action_id=''
  browser_target_payload=''
  browser_target_payload=$(python3 - "$browser_target" <<'PY'
import json
import re
import sys
import uuid

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        payload = json.load(stream)
    contract_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    run_value = payload.get("runId", "")
    conversation_value = payload.get("conversationId", "")
    retention_value = payload.get("retentionActionId", "")
    if not isinstance(run_value, str) or not isinstance(conversation_value, str):
        raise ValueError("browser target identifiers are not strings")
    if not isinstance(retention_value, str):
        raise ValueError("browser retention action identifier is not a string")
    if not re.fullmatch(contract_pattern, run_value) or not re.fullmatch(
        contract_pattern, conversation_value
    ):
        raise ValueError("browser target identifier shape is invalid")
    if retention_value and not re.fullmatch(contract_pattern, retention_value):
        raise ValueError("browser retention action identifier shape is invalid")
    run_id = str(uuid.UUID(run_value))
    conversation_id = str(uuid.UUID(conversation_value))
    retention_action_id = str(uuid.UUID(retention_value)) if retention_value else ""
    if run_id != run_value or conversation_id != conversation_value:
        raise ValueError("browser target identifiers are not canonical")
    if retention_action_id and retention_action_id != retention_value:
        raise ValueError("browser retention action identifier is not canonical")
except (OSError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
    raise SystemExit(1)
print(json.dumps({
    "runId": run_id,
    "conversationId": conversation_id,
    "retentionActionId": retention_action_id,
}, separators=(",", ":")))
PY
  ) || return 1
  browser_target_run_id=$(printf '%s\n' "$browser_target_payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["runId"])') || return 1
  browser_target_conversation_id=$(printf '%s\n' "$browser_target_payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["conversationId"])') || return 1
  browser_target_retention_action_id=$(printf '%s\n' "$browser_target_payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("retentionActionId", ""))') || return 1
}

capture_browser_target_diagnostics() {
  local target_run_id=${1:-}
  local sample_watermark_before=${2:-unavailable}
  local sample_watermark_after=${3:-unavailable}
  local collector_before=${4:-'{"available":false}'}
  local collector_after=${5:-'{"available":false}'}
  local retention_action_id=${6:-}
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  docker exec -i \
    -e "AURA_E2E_BROWSER_TARGET_RUN_ID=$target_run_id" \
    -e "AURA_E2E_BROWSER_SAMPLE_WATERMARK_BEFORE=$sample_watermark_before" \
    -e "AURA_E2E_BROWSER_SAMPLE_WATERMARK_AFTER=$sample_watermark_after" \
    -e "AURA_E2E_BROWSER_COLLECTOR_BEFORE=$collector_before" \
    -e "AURA_E2E_BROWSER_COLLECTOR_AFTER=$collector_after" \
    -e "AURA_E2E_BROWSER_RETENTION_ACTION_ID=$retention_action_id" \
    "$api_id" \
    uv run python - <<'PY'
import asyncio
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import httpx
import psycopg
import redis.asyncio

from aura_observatory.auth import MemoryLoginStateBackend, ObservatoryAuth, Principal, RedisSessionBackend
from aura_observatory.settings import Settings


def unavailable(reason: str) -> dict[str, object]:
    return {"available": False, "reason": reason}


def bounded_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 2**63 - 1 else None


def bounded_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 2**63 - 1 else None


def bounded_trace_id(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) else None


def bounded_kind(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", value)
        else None
    )


def bounded_retention_action_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if parsed == value else None


def collector_sample(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"available": False}
    if not isinstance(value, dict) or value.get("available") is not True:
        return {"available": False}
    sample: dict[str, object] = {"available": True}
    for name in (
        "sentSpans",
        "sendFailedSpans",
        "enqueueFailedSpans",
        "retryExhaustedSpans",
        "sentMetricPoints",
        "sendFailedMetricPoints",
        "enqueueFailedMetricPoints",
        "queueSize",
        "queueCapacity",
        "queueUtilization",
    ):
        number = bounded_float(value.get(name))
        if number is not None and (name != "queueUtilization" or number <= 1):
            sample[name] = number
    return sample


async def main() -> None:
    settings = Settings(role="api")
    raw_target = __import__("os").environ.get("AURA_E2E_BROWSER_TARGET_RUN_ID", "")
    try:
        target_run_id = str(uuid.UUID(raw_target))
    except (ValueError, AttributeError):
        print(json.dumps({"available": False, "targetRunId": "unavailable"}, separators=(",", ":")))
        return

    result: dict[str, object] = {
        "available": True,
        "targetRunId": target_run_id,
        "rest": unavailable("not-sampled"),
        "postgres": unavailable("not-sampled"),
        "retention": unavailable("not-sampled"),
        "clickhouse": unavailable("not-sampled"),
        "reconciliation": unavailable("not-sampled"),
        "samples": {
            "watermarkBefore": bounded_int(os.environ.get("AURA_E2E_BROWSER_SAMPLE_WATERMARK_BEFORE")),
            "watermarkAfter": bounded_int(os.environ.get("AURA_E2E_BROWSER_SAMPLE_WATERMARK_AFTER")),
            "collectorBefore": collector_sample(os.environ.get("AURA_E2E_BROWSER_COLLECTOR_BEFORE", "")),
            "collectorAfter": collector_sample(os.environ.get("AURA_E2E_BROWSER_COLLECTOR_AFTER", "")),
        },
    }
    clickhouse_observation_ids: list[str] = []

    # REST remains the public authoritative view. Only statuses, identifiers,
    # bounded counts, and sequence values leave this probe.
    redis_client = redis.asyncio.from_url(
        settings.session_store_url,
        password=settings.session_store_password,
        decode_responses=True,
    )
    auth = ObservatoryAuth(
        settings,
        RedisSessionBackend(redis_client, namespace=settings.session_namespace),
        MemoryLoginStateBackend(),
    )
    session_id, _ = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject, "E2E browser diagnostics")
    )
    try:
        cookie = f"{settings.effective_session_cookie_name}={session_id}"

        def rest_request(path: str) -> tuple[int, object | None]:
            request = urllib.request.Request(
                f"http://127.0.0.1:8000{path}",
                headers={"Cookie": cookie, "Origin": settings.public_origin},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read(1_000_000))
            except urllib.error.HTTPError as error:
                return error.code, None
            except (OSError, ValueError, TypeError):
                return 599, None

        list_status, list_payload = rest_request("/api/v1/runs?limit=50")
        detail_status, detail_payload = rest_request(f"/api/v1/runs/{target_run_id}")
        overview_status, overview_payload = rest_request("/api/v1/overview")
        items = list_payload.get("items", []) if isinstance(list_payload, dict) else []
        if not isinstance(items, list):
            items = []
        list_ids = {
            str(item.get("runId", item.get("id", "")))
            for item in items[:50]
            if isinstance(item, dict)
        }
        detail_observations = detail_payload.get("observations", []) if isinstance(detail_payload, dict) else []
        if not isinstance(detail_observations, list):
            detail_observations = []
        detail_sequences = [
            bounded_int(item.get("sequence"))
            for item in detail_observations[:256]
            if isinstance(item, dict)
        ]
        detail_sequences = [value for value in detail_sequences if value is not None]
        result["rest"] = {
            "listStatus": list_status,
            "listCount": len(items),
            "listContainsTarget": target_run_id in list_ids,
            "detailStatus": detail_status,
            "detailRunId": (
                target_run_id
                if isinstance(detail_payload, dict)
                and detail_payload.get("runId") == target_run_id
                else None
            ),
            "detailTraceId": (
                bounded_trace_id(detail_payload.get("traceId"))
                if isinstance(detail_payload, dict)
                else None
            ),
            "detailObservationCount": len(detail_observations),
            "detailMinSequence": min(detail_sequences) if detail_sequences else None,
            "detailMaxSequence": max(detail_sequences) if detail_sequences else None,
            "detailFirst200MonotonicMs": (
                time.monotonic_ns() // 1_000_000 if detail_status == 200 else None
            ),
        }
        retention_action_id = bounded_retention_action_id(
            os.environ.get("AURA_E2E_BROWSER_RETENTION_ACTION_ID", "")
        )
        result["retention"] = (
            {"available": False, "reason": "action-id-unavailable"}
            if retention_action_id is None
            else {"available": False, "actionId": retention_action_id, "reason": "not-sampled"}
        )
        health = overview_payload.get("health") if isinstance(overview_payload, dict) else None
        result["reconciliation"] = {
            "overviewStatus": overview_status,
            "state": health.get("state") if isinstance(health, dict) else None,
            "visibilityWatermark": (
                bounded_int(health.get("visibilityWatermark")) if isinstance(health, dict) else None
            ),
            "visibilityLagCount": (
                bounded_int(health.get("visibilityLagCount")) if isinstance(health, dict) else None
            ),
            "backlogCount": (
                bounded_int(health.get("backlogCount")) if isinstance(health, dict) else None
            ),
        }

        # The direct store probes use only service-owned metadata tables and
        # ClickHouse aggregate results; no observation payload is selected.
        try:
            async with await psycopg.AsyncConnection.connect(settings.database_url) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT committed_sequence FROM observatory_ingest_watermark "
                        "WHERE watermark_key = TRUE"
                    )
                    watermark = await cursor.fetchone()
                    await cursor.execute(
                        "SELECT count(*) FROM observatory_observation_receipts "
                        "WHERE status = 'pending'"
                    )
                    pending = await cursor.fetchone()
                    await cursor.execute(
                        "SELECT count(*) FROM observatory_ingest_visibility "
                        "WHERE visible = FALSE"
                    )
                    invisible = await cursor.fetchone()
                    if retention_action_id is not None:
                        await cursor.execute(
                            "SELECT action_kind, status, target_observation_count, "
                            "target_receipt_count, target_run_count, deletion_phase "
                            "FROM observatory_retention_actions WHERE action_id = %s",
                            (retention_action_id,),
                        )
                        retention = await cursor.fetchone()
                        if retention is None:
                            result["retention"] = {
                                "available": False,
                                "actionId": retention_action_id,
                                "reason": "action-not-found",
                            }
                        else:
                            result["retention"] = {
                                "available": True,
                                "actionId": retention_action_id,
                                "actionKind": bounded_kind(retention[0]),
                                "status": bounded_kind(retention[1]),
                                "targetObservationCount": bounded_int(retention[2]),
                                "targetReceiptCount": bounded_int(retention[3]),
                                "targetRunCount": bounded_int(retention[4]),
                                "deletionPhase": bounded_kind(retention[5]),
                            }
            result["postgres"] = {
                "receiptStatus": None,
                "receiptSequence": None,
                "committedWatermark": bounded_int(watermark[0]) if watermark else None,
            }
            reconciliation = result.get("reconciliation")
            if isinstance(reconciliation, dict):
                reconciliation.update(
                    {
                        "databaseAvailable": True,
                        "pendingReceiptCount": bounded_int(pending[0]) if pending else None,
                        "invisibleSequenceCount": bounded_int(invisible[0]) if invisible else None,
                    }
                )
        except (OSError, psycopg.Error, TypeError, ValueError):
            result["postgres"] = unavailable("query-failed")
            if retention_action_id is not None:
                result["retention"] = {
                    "available": False,
                    "actionId": retention_action_id,
                    "reason": "query-failed",
                }
            reconciliation = result.get("reconciliation")
            if isinstance(reconciliation, dict):
                reconciliation["databaseAvailable"] = False

        def fallback_clickhouse_stage(stage: str) -> dict[str, object]:
            return {
                "stage": stage,
                "outcome": f"{stage}-status",
                "status": None,
                "elapsedMs": 0,
                "responseBytes": 0,
            }

        try:
            escaped_target = target_run_id.replace("'", "''")
            clickhouse_params = {
                "database": settings.clickhouse_database,
                "max_execution_time": "4",
            }

            def stage_result(stage: str) -> dict[str, object]:
                return fallback_clickhouse_stage(stage) | {"outcome": f"{stage}-shape"}

            async def run_stage(
                client: httpx.AsyncClient,
                stage: str,
                query: str,
                parser,
            ) -> tuple[dict[str, object], object | None]:
                metadata = stage_result(stage)
                started_ns = time.monotonic_ns()
                try:
                    response = await client.post(
                        settings.clickhouse_url,
                        params=clickhouse_params,
                        auth=(settings.clickhouse_user, settings.clickhouse_password),
                        content=query,
                    )
                except httpx.TimeoutException:
                    metadata["outcome"] = f"{stage}-timeout"
                    metadata["elapsedMs"] = min(5_000, max(0, (time.monotonic_ns() - started_ns) // 1_000_000))
                    return metadata, None
                except (OSError, httpx.HTTPError):
                    metadata["outcome"] = f"{stage}-status"
                    metadata["elapsedMs"] = min(5_000, max(0, (time.monotonic_ns() - started_ns) // 1_000_000))
                    return metadata, None
                metadata["status"] = bounded_int(response.status_code)
                metadata["responseBytes"] = min(len(response.content), 1_000_000)
                metadata["elapsedMs"] = min(5_000, max(0, (time.monotonic_ns() - started_ns) // 1_000_000))
                if not response.is_success:
                    metadata["outcome"] = f"{stage}-status"
                    return metadata, None
                lines = [line for line in response.text.splitlines() if line.strip()]
                if not lines:
                    metadata["outcome"] = f"{stage}-empty"
                    return metadata, None
                try:
                    parsed = parser(lines)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if parsed is None:
                    metadata["outcome"] = f"{stage}-shape"
                    return metadata, None
                metadata["outcome"] = f"{stage}-ok"
                return metadata, parsed

            def parse_aggregate(lines: list[str]) -> dict[str, object] | None:
                if len(lines) != 1:
                    return None
                candidate = json.loads(lines[0])
                if not isinstance(candidate, dict):
                    return None
                required = {
                    "row_count",
                    "min_sequence",
                    "max_sequence",
                    "span_row_count",
                    "metric_row_count",
                    "distinct_trace_ids",
                    "unique_span_ids",
                }
                if not required <= set(candidate):
                    return None
                return candidate

            def parse_kind(lines: list[str]) -> dict[str, int] | None:
                if len(lines) > 16:
                    return None
                parsed: dict[str, int] = {}
                for line in lines:
                    candidate = json.loads(line)
                    if not isinstance(candidate, dict):
                        return None
                    kind = bounded_kind(candidate.get("kind"))
                    count = bounded_int(candidate.get("kind_count"))
                    if kind is None or count is None:
                        return None
                    parsed[kind] = count
                return parsed or None

            def parse_trace(lines: list[str]) -> list[dict[str, object]] | None:
                if len(lines) > 8:
                    return None
                parsed: list[dict[str, object]] = []
                for line in lines:
                    candidate = json.loads(line)
                    if not isinstance(candidate, dict):
                        return None
                    trace_id = bounded_trace_id(candidate.get("trace_id"))
                    count = bounded_int(candidate.get("trace_count"))
                    if trace_id is None or count is None:
                        return None
                    parsed.append({"traceId": trace_id, "count": count})
                return parsed or None

            def parse_expected(lines: list[str]) -> int | None:
                if len(lines) != 1:
                    return None
                candidate = json.loads(lines[0])
                return bounded_int(candidate.get("expected_trace_count")) if isinstance(candidate, dict) else None

            def parse_rows(lines: list[str]) -> list[str] | None:
                if len(lines) > 256:
                    return None
                identifiers: list[str] = []
                for line in lines:
                    candidate = json.loads(line)
                    if not isinstance(candidate, dict):
                        return None
                    try:
                        identifiers.append(str(uuid.UUID(str(candidate["observation_id"]))))
                    except (KeyError, ValueError, AttributeError, TypeError):
                        return None
                return identifiers or None

            aggregate_query = (
                "SELECT count() AS row_count, min(ingest_sequence) AS min_sequence, "
                "max(ingest_sequence) AS max_sequence, countIf(kind = 'span') AS span_row_count, "
                "countIf(kind = 'metric') AS metric_row_count, "
                "uniqExactIf(trace_id, trace_id != '') AS distinct_trace_ids, "
                "uniqExactIf(span_id, span_id != '') AS unique_span_ids FROM observations WHERE run_id = "
                + repr(escaped_target)
                + " FORMAT JSONEachRow"
            )
            kind_query = (
                "SELECT kind, count() AS kind_count FROM observations WHERE run_id = "
                + repr(escaped_target)
                + " GROUP BY kind ORDER BY kind_count DESC, kind ASC LIMIT 16 FORMAT JSONEachRow"
            )
            trace_query = (
                "SELECT trace_id, count() AS trace_count FROM observations WHERE run_id = "
                + repr(escaped_target)
                + " AND trace_id != '' GROUP BY trace_id ORDER BY trace_count DESC, trace_id ASC LIMIT 8 FORMAT JSONEachRow"
            )
            expected_trace_id = bounded_trace_id(
                detail_payload.get("traceId") if isinstance(detail_payload, dict) else None
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                aggregate_meta, aggregate_row = await run_stage(client, "aggregate", aggregate_query, parse_aggregate)
                kind_meta, kind_counts_value = await run_stage(client, "kind", kind_query, parse_kind)
                trace_meta, trace_counts_value = await run_stage(client, "trace", trace_query, parse_trace)
                expected_meta = stage_result("trace-expected")
                expected_trace_count = None
                if expected_trace_id is not None:
                    expected_meta, expected_trace_count = await run_stage(
                        client,
                        "trace-expected",
                        "SELECT count() AS expected_trace_count FROM observations WHERE run_id = "
                        + repr(escaped_target)
                        + " AND trace_id = "
                        + repr(expected_trace_id)
                        + " FORMAT JSONEachRow",
                        parse_expected,
                    )
                rows_meta, row_ids = await run_stage(
                    client,
                    "trace-rows",
                    "SELECT observation_id, ingest_sequence FROM observations WHERE run_id = "
                    + repr(escaped_target)
                    + " ORDER BY ingest_sequence LIMIT 256 FORMAT JSONEachRow",
                    parse_rows,
                )
            if isinstance(row_ids, list):
                clickhouse_observation_ids.extend(row_ids[:256])
            row = aggregate_row if isinstance(aggregate_row, dict) else {}
            kind_counts = kind_counts_value if isinstance(kind_counts_value, dict) else {}
            top_trace_counts = trace_counts_value if isinstance(trace_counts_value, list) else []
            result["clickhouse"] = {
                "exactRowPresent": bool(bounded_int(row.get("row_count"))),
                "rowCount": bounded_int(row.get("row_count")),
                "minSequence": bounded_int(row.get("min_sequence")),
                "maxSequence": bounded_int(row.get("max_sequence")),
                "kindCounts": kind_counts,
                "spanRowCount": bounded_int(row.get("span_row_count")),
                "metricRowCount": bounded_int(row.get("metric_row_count")),
                "distinctNonEmptyTraceIds": bounded_int(row.get("distinct_trace_ids")),
                "uniqueNonEmptySpanIds": bounded_int(row.get("unique_span_ids")),
                "topTraceIdCounts": top_trace_counts,
                "expectedTraceId": expected_trace_id,
                "expectedTraceIdCount": expected_trace_count,
                "expectedTraceIdPresent": bool(expected_trace_count),
                "stages": {
                    "aggregate": aggregate_meta,
                    "kind": kind_meta,
                    "trace": trace_meta,
                    "traceExpected": expected_meta,
                    "traceRows": rows_meta,
                },
            }
        except (OSError, httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError):
            result["clickhouse"] = {
                "exactRowPresent": False,
                "rowCount": None,
                "minSequence": None,
                "maxSequence": None,
                "kindCounts": {},
                "spanRowCount": None,
                "metricRowCount": None,
                "distinctNonEmptyTraceIds": None,
                "uniqueNonEmptySpanIds": None,
                "topTraceIdCounts": [],
                "expectedTraceId": bounded_trace_id(
                    detail_payload.get("traceId") if isinstance(detail_payload, dict) else None
                ),
                "expectedTraceIdCount": None,
                "expectedTraceIdPresent": False,
                "stages": {
                    stage: fallback_clickhouse_stage(stage)
                    for stage in ("aggregate", "kind", "trace", "traceExpected", "traceRows")
                },
            }

        try:
            clickhouse = result.get("clickhouse")
            postgres = result.get("postgres")
            row_count = clickhouse.get("rowCount") if isinstance(clickhouse, dict) else None
            target_min = clickhouse.get("minSequence") if isinstance(clickhouse, dict) else None
            target_max = clickhouse.get("maxSequence") if isinstance(clickhouse, dict) else None
            committed = postgres.get("committedWatermark") if isinstance(postgres, dict) else None
            if not isinstance(row_count, int) or row_count <= 0 or not all(
                isinstance(value, int) for value in (target_min, target_max, committed)
            ):
                raise ValueError("target-sequence-range-unavailable")
            async with await psycopg.AsyncConnection.connect(settings.database_url) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT count(*), min(ingest_sequence), max(ingest_sequence) "
                        "FROM observatory_observation_receipts "
                        "WHERE status = 'pending' AND ingest_sequence BETWEEN %s AND %s",
                        (target_min, target_max),
                    )
                    pending_target = await cursor.fetchone()
            if isinstance(postgres, dict):
                pending_count = bounded_int(pending_target[0]) if pending_target else None
                pending_min = bounded_int(pending_target[1]) if pending_target else None
                pending_max = bounded_int(pending_target[2]) if pending_target else None
                postgres["pendingTargetRange"] = {
                    "targetMinSequence": target_min,
                    "targetMaxSequence": target_max,
                    "committedWatermark": committed,
                    "pendingCount": pending_count,
                    "pendingMinSequence": pending_min,
                    "pendingMaxSequence": pending_max,
                    "watermarkCoversTargetRange": committed >= target_max,
                    "pendingAfterWatermark": (
                        pending_min is not None and pending_min > committed
                    ),
                }
        except (OSError, psycopg.Error, TypeError, ValueError):
            postgres = result.get("postgres")
            if isinstance(postgres, dict):
                postgres["pendingTargetRange"] = unavailable("sequence-relation-unavailable")

        if clickhouse_observation_ids:
            try:
                async with await psycopg.AsyncConnection.connect(settings.database_url) as connection:
                    async with connection.cursor() as cursor:
                        placeholders = ",".join("%s" for _ in clickhouse_observation_ids)
                        await cursor.execute(
                            "SELECT status, ingest_sequence, claimed_at, updated_at "
                            "FROM observatory_observation_receipts "
                            f"WHERE observation_id IN ({placeholders})",
                            tuple(clickhouse_observation_ids),
                        )
                        receipt_rows = await cursor.fetchall()
                statuses: dict[str, int] = {}
                sequences = []
                for status, sequence, _claimed_at, _updated_at in receipt_rows[:256]:
                    status_name = str(status)[:32]
                    statuses[status_name] = statuses.get(status_name, 0) + 1
                    value = bounded_int(sequence)
                    if value is not None:
                        sequences.append(value)
                current_postgres = result.get("postgres")
                if isinstance(current_postgres, dict):
                    single_receipt = receipt_rows[0] if len(receipt_rows) == 1 else None
                    current_postgres.update(
                        {
                            "receiptStatus": str(single_receipt[0])[:32] if single_receipt else (
                                "multiple" if len(receipt_rows) > 1 else None
                            ),
                            "receiptSequence": bounded_int(single_receipt[1]) if single_receipt else None,
                            "receiptCount": len(receipt_rows),
                            "receiptStatuses": statuses,
                            "receiptMinSequence": min(sequences) if sequences else None,
                            "receiptMaxSequence": max(sequences) if sequences else None,
                            "claimedAtMin": min(
                                (str(row[2])[:64] for row in receipt_rows if row[2] is not None),
                                default=None,
                            ),
                            "claimedAtMax": max(
                                (str(row[2])[:64] for row in receipt_rows if row[2] is not None),
                                default=None,
                            ),
                            "updatedAtMin": min(
                                (str(row[3])[:64] for row in receipt_rows if row[3] is not None),
                                default=None,
                            ),
                            "updatedAtMax": max(
                                (str(row[3])[:64] for row in receipt_rows if row[3] is not None),
                                default=None,
                            ),
                        }
                    )
            except (OSError, psycopg.Error, TypeError, ValueError):
                result["postgres"] = unavailable("receipt-query-failed")
    finally:
        await auth.sessions.delete(session_id)
        await redis_client.aclose()
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


asyncio.run(main())
PY
}

run_scan_boundary_probe() {
  # Keep the million-row scan-boundary fixture after the real browser run. It
  # exercises the bounded analytics path without changing the ClickHouse
  # visibility state that the browser must inspect. Every mutation is
  # synchronous and removed in finally, including a partially inserted
  # fixture.
  local api_id
  api_id=$("${compose[@]}" ps -q observatory-api 2>/dev/null | head -n 1 || true)
  [ -n "$api_id" ] || return 1
  docker exec -i "$api_id" uv run python - <<'PY'
import asyncio
import json
from datetime import datetime, timezone

import httpx

from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.query import AnalyticsRequest
from aura_observatory.settings import Settings


COMPONENT = "aura.observatory.e2e.scan-boundary"


def unavailable_postgres() -> object:
    raise RuntimeError("PostgreSQL is not part of this ClickHouse scan-boundary probe")


def provider_status(error: BaseException) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return str(status) if isinstance(status, int) else "transport"


settings = Settings()
insert = (
    "INSERT INTO observations (observation_id, ingest_sequence, observed_at, "
    "schema_version, kind, component_id, component_version, operation, "
    "measurement_name, measurement_value, measurement_unit, dimensions, "
    "trace_id, span_id, parent_span_id, linked_span_ids, run_id, "
    "conversation_id, deployment_id, git_commit, environment, host_id, "
    "service_role, capture_policy) SELECT "
    "concat('aura-0087-scan-', toString(number)), number + 2, "
    "toDateTime64('2026-01-01 00:00:00', 3, 'UTC'), 1, 'metric', "
    + repr(COMPONENT)
    + ", 'e2e', 'scan', 'duration_ms', toFloat64(1), 'milliseconds', "
    "map('outcome', 'success'), '', '', '', emptyArrayString(), '', '', "
    "'e2e', '0000000', 'test', 'e2e', 'api', 'metadata_only' "
    "FROM numbers(1000001) SETTINGS max_threads = 1"
)
cleanup = (
    "ALTER TABLE observations DELETE WHERE component_id = "
    + repr(COMPONENT)
    + " SETTINGS mutations_sync=1"
)
failure: str | None = None
try:
    try:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                content=insert,
            )
        if not response.is_success:
            failure = f"ClickHouse scan-boundary fixture insert failed status={response.status_code}"
    except httpx.HTTPError:
        failure = "ClickHouse scan-boundary fixture insert failed: transport"
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
            analytics = asyncio.run(store.operational_analytics_query(
                AnalyticsRequest(
                    metric="operation_duration_ms",
                    unit="milliseconds",
                    from_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    to_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    bucket="one_hour",
                    group_by="component",
                    filters={"component": COMPONENT},
                )
            ))
        except httpx.HTTPError as exc:
            failure = "ClickHouse scan-boundary adapter failed status=" + provider_status(exc)
            analytics = {"partialReasons": []}
        if failure is None and "scan_limit" not in analytics.get("partialReasons", []):
            failure = "ClickHouse scan-boundary query was not partial"
        elif failure is None:
            print("AURA_E2E post-browser ClickHouse scan-boundary query=passed")
finally:
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                settings.clickhouse_url,
                params={"database": settings.clickhouse_database},
                auth=(settings.clickhouse_user, settings.clickhouse_password),
                content=cleanup,
            )
        if not response.is_success and failure is None:
            failure = f"ClickHouse scan-boundary cleanup failed status={response.status_code}"
    except httpx.HTTPError:
        if failure is None:
            failure = "ClickHouse scan-boundary cleanup failed: transport"
if failure is not None:
    raise SystemExit(failure)
print("AURA_E2E post-browser ClickHouse scan-boundary fixture cleaned")
PY
}

# Bring up the Core API first so its startup path can create and validate the
# AURA_RUNS JetStream stream before a worker begins consuming fresh-state
# commands. The API's readiness route is the existing dependency signal; it
# includes NATS JetStream and provider checks without inventing a worker
# health endpoint.
bounded "$start_timeout" "${compose[@]}" up --build --detach --wait aura-core-api
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
bounded "$start_timeout" "${compose[@]}" up --build --detach --wait \
  aura-web observatory-web observatory-api observatory-collector
bounded "$start_timeout" "${compose[@]}" up --build --detach --wait \
  aura-core-worker aura-core-scheduler observatory-worker \
  observatory-evaluation-worker observatory-evaluation-scheduler aura-evaluation-runner
probe_clickhouse_observations_table post-readiness
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
PAYLOAD_BYTES = len(PAYLOAD)
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
            max_bytes=PAYLOAD_BYTES,
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
metric_capability_blocked=0
metric_capability_blockers=()
artifact_operation_before='{"value":0,"samples":0}'
operations_recovery_before='{"value":0,"samples":0}'
operations_full_stack_enabled=${AURA_OBSERVATORY_OPERATIONS_FULL_STACK:-0}
if [ "$foundation_scope" -eq 1 ]; then
  operations_full_stack_enabled=0
fi
record_metric_capability_blocker() {
  local capability=$1
  metric_capability_blocked=1
  metric_capability_blockers+=("$capability")
  printf 'AURA_E2E metric_capability_blocker=%s\n' "$capability" >&2
}
if [ "$foundation_scope" -eq 1 ]; then
  printf '%s\n' 'AURA_E2E foundation scope: evaluation artifact metric gate not applicable' >&2
else
  if ! artifact_operation_before=$(capture_observatory_metric_total \
    artifact_preflight observatory_evaluation_artifact_operation_count); then
    record_metric_capability_blocker \
      observatory_evaluation_artifact_operation_count
    artifact_operation_before='{"value":0,"samples":0}'
  fi
fi
if [ "$foundation_scope" -eq 1 ]; then
  printf '%s\n' 'AURA_E2E foundation scope: operations recovery metric gate not applicable' >&2
elif [ "$operations_full_stack_enabled" = 1 ]; then
  # The optional browser scenario is the only authorized operations recovery
  # action in this work item. Baseline its exact bounded operation/outcome
  # series immediately before that workflow; never accept a historical
  # positive sample as proof.
  if ! operations_recovery_before=$(capture_observatory_metric_total \
    operations_preflight observatory_operations_recovery_count); then
    record_metric_capability_blocker \
      'observatory_operations_recovery_count:public-workflow-unavailable'
    operations_recovery_before='{"value":0,"samples":0}'
  fi
else
  record_metric_capability_blocker \
    'observatory_operations_recovery_count:authorized-browser-workflow-not-enabled'
fi
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
QUANTILE_COMPONENT = "aura.observatory.e2e.quantile-parity"


def provider_status(error: BaseException) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return str(status) if isinstance(status, int) else "transport"


def http_failure_category(status: object) -> str:
    if isinstance(status, int) and 400 <= status <= 499:
        return "client_error"
    if isinstance(status, int) and 500 <= status <= 599:
        return "server_error"
    return "unexpected_status"


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
            failure = (
                "ClickHouse preflight failed "
                f"status={response.status_code} "
                f"category={http_failure_category(response.status_code)}"
            )
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
                f"status={response.status_code} "
                f"category={http_failure_category(response.status_code)}"
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
                        "ClickHouse preflight cleanup failed "
                        f"status={cleanup.status_code} "
                        f"category={http_failure_category(cleanup.status_code)}"
                    )
    except httpx.HTTPError:
        cleanup_failure = cleanup_failure or "ClickHouse preflight cleanup failed: transport error"
    if failure is None and cleanup_failure is not None:
        failure = cleanup_failure

if failure is not None:
    raise SystemExit(failure)
print("Observatory ClickHouse row-shape preflight passed")
PY
seed_evaluation_definitions
"${compose[@]}" build frontend-e2e
probe_clickhouse_observations_table pre-browser
run_core_observatory_visibility_probe pre-browser
verify_evaluation_runner_database_access
browser_export_before=$(capture_core_telemetry_export_stats)
browser_worker_export_before=$(capture_core_telemetry_export_stats aura-core-worker \
  || printf '%s' '{"available":false}')
browser_ingest_before=$(capture_observatory_ingest_signal)
# Capture the Collector baseline before Playwright starts. A post-only sample
# cannot distinguish delivery during the browser phase from pre-existing queue
# state, and aggregate counters alone cannot correlate a missing run.
browser_collector_before=$(capture_collector_queue_signal \
  || printf '%s' '{"available":false}')
# The browser analytics assertion uses the public rendered refresh action. Keep
# these optional public metric samples as bounded supporting evidence for that
# phase; they never substitute for the rendered assertion or target evidence.
if [ "$foundation_scope" -eq 1 ]; then
  # These Prometheus query counters support the full evaluation/operations
  # matrix only.  Scenario 1 has authoritative rendered analytics assertions;
  # keep the optional sampler explicitly unavailable so an empty metrics
  # response cannot become a post-browser JSON parse failure.
  browser_query_outcome_before='{"available":false,"reason":"foundation-not-sampled"}'
  browser_query_duration_before='{"available":false,"reason":"foundation-not-sampled"}'
else
  browser_query_outcome_before=$(capture_optional_metric_snapshot browser_query_before observatory_query_outcome)
  browser_query_duration_before=$(capture_optional_metric_snapshot browser_query_before observatory_query_duration_ms_count)
fi
browser_status=0
browser_phase_started_at=$(date +%s%3N)
set +e
if [ "$foundation_scope" -eq 1 ]; then
  if "${compose[@]}" run --rm --no-deps --no-TTY \
    -e "AURA_OBSERVATORY_OPERATIONS_FULL_STACK=$operations_full_stack_enabled" \
    frontend-e2e sh -c "exec pnpm --dir frontend exec playwright test \
      --config=apps/observatory-web-e2e/full-stack.config.ts \
      --grep 'authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention'" \
    2>&1 | tee "$browser_command_output"; then
    browser_pipeline_status=0
  else
    browser_pipeline_status=${PIPESTATUS[0]}
  fi
elif "${compose[@]}" run --rm --no-deps --no-TTY \
  -e "AURA_OBSERVATORY_OPERATIONS_FULL_STACK=$operations_full_stack_enabled" frontend-e2e \
  2>&1 | tee "$browser_command_output"; then
  browser_pipeline_status=0
else
  browser_pipeline_status=${PIPESTATUS[0]}
fi
set -e
browser_status=${browser_pipeline_status:-1}
browser_phase_finished_at=$(date +%s%3N)
browser_phase_elapsed_ms=$((browser_phase_finished_at - browser_phase_started_at))
printf 'AURA_E2E phase=browser status=%s elapsed_ms=%s\n' \
  "$browser_status" "$browser_phase_elapsed_ms" >&2
printf '{"schemaVersion":1,"phase":"browser","status":%s,"elapsedMs":%s}\n' \
  "$browser_status" "$browser_phase_elapsed_ms" >"$browser_artifact_root/browser-phase.json"
if [ "$browser_status" -ne 0 ]; then
  # Keep only bounded, content-free Playwright process diagnostics. The raw
  # stream is temporary fixture state and is removed during cleanup; the
  # retained artifact contains classifications rather than test output,
  # selectors, URLs, prompts, or response bodies.
  python3 - "$browser_command_output" "$browser_artifact_root/browser-command-failure.json" "$browser_status" <<'PY' || true
import json
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
status = int(sys.argv[3])
line_count = 0
failure_kinds: list[str] = []
if source.is_file():
    for raw_line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        line_count = min(line_count + 1, 4096)
        line = raw_line.strip()
        if "Invalid regular expression" in line:
            failure_kinds.append("syntax-error-invalid-regular-expression")
        elif re.search(r"\bNo tests found\b", line):
            failure_kinds.append("no-tests-found")
        elif line.startswith("Error:"):
            failure_kinds.append("playwright-error")
        elif line.startswith("SyntaxError:"):
            failure_kinds.append("syntax-error")
unique_kinds = list(dict.fromkeys(failure_kinds))[:8]
destination.write_text(
    json.dumps(
        {
            "schemaVersion": 1,
            "reportKind": "playwright-process",
            "status": status,
            "outputLineCount": line_count,
            "failureKinds": unique_kinds,
            "failureReportAvailable": bool(unique_kinds),
        },
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY
fi
if [ "$browser_status" -ne 0 ]; then
  # Capture only bounded service state and classified log categories while the
  # project-scoped evaluation services are still available. Raw log bodies,
  # credentials, request data, and model content never enter the artifact.
  capture_browser_evaluation_service_diagnostics || true
fi
# Always sample the same bounded, content-free boundary after Playwright,
# including when the browser command fails. This preserves the evidence needed
# to distinguish a browser/detail race from Core export or Collector ingest
# loss before returning the browser's original nonzero status.
browser_after_sample_status=0
browser_export_after=''
browser_worker_export_after='{"available":false}'
browser_ingest_after=''
browser_collector_after='{"available":false}'
if ! browser_export_after=$(capture_core_telemetry_export_stats); then
  browser_after_sample_status=1
fi
if ! browser_worker_export_after=$(capture_core_telemetry_export_stats aura-core-worker); then
  browser_worker_export_after='{"available":false}'
fi
if ! browser_ingest_after=$(capture_observatory_ingest_signal); then
  browser_after_sample_status=1
fi
if ! browser_collector_after=$(capture_collector_queue_signal); then
  browser_collector_after='{"available":false}'
fi
if [ "$foundation_scope" -eq 1 ]; then
  browser_query_outcome_after='{"available":false,"reason":"foundation-not-sampled"}'
  browser_query_duration_after='{"available":false,"reason":"foundation-not-sampled"}'
else
  browser_query_outcome_after=$(capture_optional_metric_snapshot browser_query_after observatory_query_outcome)
  browser_query_duration_after=$(capture_optional_metric_snapshot browser_query_after observatory_query_duration_ms_count)
fi
browser_delta_status=0
if [ "$browser_after_sample_status" -eq 0 ]; then
  if ! python3 - "$browser_export_before" "$browser_export_after" \
  "$browser_worker_export_before" "$browser_worker_export_after" \
  "$browser_ingest_before" "$browser_ingest_after" \
  "$browser_collector_before" "$browser_collector_after" \
  <<'PY'
import json
import math
import sys

(
    export_before,
    export_after,
    worker_before,
    worker_after,
    ingest_before,
    ingest_after,
    collector_before,
    collector_after,
) = sys.argv[1:]
before_export = json.loads(export_before)
after_export = json.loads(export_after)
before_worker = json.loads(worker_before)
after_worker = json.loads(worker_after)
before_ingest = json.loads(ingest_before)
after_ingest = json.loads(ingest_after)
core_names = (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "dropped",
    "rejected",
    "abandoned_batches",
    "export_failures",
)
core_gauge_names = ("retained",)


def exporter_transition(before, after, label):
    before_identity = (before.get("containerId"), before.get("startedAt"), before.get("pid"))
    after_identity = (after.get("containerId"), after.get("startedAt"), after.get("pid"))
    if any(value in {None, "", "unavailable"} for value in before_identity + after_identity):
        return {}, {}, {"state": "identity-unavailable", "label": label}
    if before_identity != after_identity:
        return {}, {}, {
            "state": "reset",
            "label": label,
            "beforeContainerId": str(before_identity[0])[:16],
            "afterContainerId": str(after_identity[0])[:16],
        }
    deltas = {}
    gauges = {}
    for name in core_names:
        before_value = float(before[name])
        after_value = float(after[name])
        if (
            not math.isfinite(before_value)
            or not math.isfinite(after_value)
            or before_value < 0
            or after_value < 0
        ):
            raise SystemExit(f"{label} exporter counters were not bounded")
        if after_value < before_value:
            raise SystemExit(f"{label} exporter counters regressed on the same instance")
        deltas[name] = after_value - before_value
    for name in core_gauge_names:
        before_value = float(before[name])
        after_value = float(after[name])
        if (
            not math.isfinite(before_value)
            or not math.isfinite(after_value)
            or before_value < 0
            or after_value < 0
        ):
            raise SystemExit(f"{label} exporter gauges were not bounded")
        gauges[name] = {"before": before_value, "after": after_value}
    return deltas, gauges, {
        "state": "monotonic",
        "label": label,
        "containerId": str(after_identity[0])[:16],
    }


api_delta, api_gauges, api_lifecycle = exporter_transition(
    before_export, after_export, "core-api"
)
for name in ("accepted", "rejected", "duplicate", "malformed", "storageFailure"):
    before = float(before_ingest[name])
    after = float(after_ingest[name])
    if not math.isfinite(before) or not math.isfinite(after) or after < before:
        raise SystemExit("browser stage Observatory ingest counters were not bounded and monotonic")
collector_before = json.loads(collector_before)
collector_after = json.loads(collector_after)
collector_delta = {}
collector_gauges = {}
if collector_before.get("available") and collector_after.get("available"):
    for name in (
        "sentSpans",
        "sendFailedSpans",
        "enqueueFailedSpans",
        "retryExhaustedSpans",
        "sentMetricPoints",
        "sendFailedMetricPoints",
        "enqueueFailedMetricPoints",
    ):
        if name not in collector_before or name not in collector_after:
            continue
        before = float(collector_before[name])
        after = float(collector_after[name])
        if not math.isfinite(before) or not math.isfinite(after) or after < before:
            raise SystemExit("Collector queue counters were not bounded and monotonic")
        collector_delta[name] = after - before
    for name in ("queueSize", "queueCapacity", "queueUtilization"):
        if name not in collector_before or name not in collector_after:
            continue
        before = float(collector_before[name])
        after = float(collector_after[name])
        if (
            not math.isfinite(before)
            or not math.isfinite(after)
            or before < 0
            or after < 0
            or (name == "queueUtilization" and (before > 1 or after > 1))
        ):
            raise SystemExit("Collector queue gauge was not bounded")
        collector_gauges[name] = {"before": before, "after": after}
worker_delta = {}
worker_gauges = {}
worker_lifecycle = {"state": "unavailable", "label": "core-worker"}
if before_worker.get("containerId") and after_worker.get("containerId"):
    worker_delta, worker_gauges, worker_lifecycle = exporter_transition(
        before_worker, after_worker, "core-worker"
    )
print(json.dumps({
    "project": "aura0098",
    "stage": "browser",
    "correlatedRunId": "browser-target-file",
    "coreApiExporterDeltas": api_delta,
    "coreApiExporterGauges": api_gauges,
    "coreApiExporterLifecycle": api_lifecycle,
    "coreWorkerExporterAvailable": bool(worker_delta),
    "coreWorkerExporterDeltas": worker_delta,
    "coreWorkerExporterGauges": worker_gauges,
    "coreWorkerExporterLifecycle": worker_lifecycle,
    "collectorQueueDeltas": collector_delta,
    "collectorQueueGauges": collector_gauges,
    **api_delta,
    **(
        {
            "coreExportRetryDelta": api_delta["telemetry_export_retry_count"],
            "coreExportDropDelta": api_delta["telemetry_export_dropped_count"],
        }
        if api_lifecycle["state"] == "monotonic"
        else {}
    ),
    "observatoryAcceptedDelta": float(after_ingest["accepted"])
    - float(before_ingest["accepted"]),
    "observatoryRejectedDelta": float(after_ingest["rejected"])
    - float(before_ingest["rejected"]),
    "observatoryDuplicateDelta": float(after_ingest["duplicate"])
    - float(before_ingest["duplicate"]),
    "observatoryMalformedDelta": float(after_ingest["malformed"])
    - float(before_ingest["malformed"]),
    "observatoryStorageFailureDelta": float(after_ingest["storageFailure"])
    - float(before_ingest["storageFailure"]),
}, separators=(",", ":")))
PY
  then
    browser_delta_status=1
  fi
else
  browser_delta_status=1
fi
if ! python3 - "$browser_query_outcome_before" "$browser_query_outcome_after" \
  "$browser_query_duration_before" "$browser_query_duration_after" <<'PY'
import json
import math
import sys

outcome_before, outcome_after, duration_before, duration_after = map(json.loads, sys.argv[1:])


def bounded_sample(value):
    if not isinstance(value, dict) or value.get("available") is False:
        return None
    number = value.get("value")
    return number if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and 0 <= number <= 2**63 - 1 else None


def transition(before, after, name):
    before_value = bounded_sample(before)
    after_value = bounded_sample(after)
    if before_value is None or after_value is None:
        return {"state": "unavailable", "metric": name}
    if after_value < before_value:
        raise SystemExit(f"{name} regressed during browser stage")
    return {"state": "available", "metric": name, "before": before_value, "after": after_value, "delta": after_value - before_value}


print(json.dumps({
    "project": "aura0098",
    "stage": "browser",
    "observatoryQueryOutcome": transition(outcome_before, outcome_after, "observatory_query_outcome"),
    "observatoryQueryDurationCount": transition(duration_before, duration_after, "observatory_query_duration_ms_count"),
}, separators=(",", ":")))
PY
then
  browser_delta_status=1
fi
# Resolve the UUID created by the browser itself. The pre-browser probe is a
# barrier only; it is never substituted for a missing browser target.
browser_sample_watermark_before=$(python3 - "$browser_ingest_before" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1]).get("visibilityWatermark")
    print(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "unavailable")
except (TypeError, ValueError, json.JSONDecodeError):
    print("unavailable")
PY
)
browser_sample_watermark_after=$(python3 - "$browser_ingest_after" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1]).get("visibilityWatermark")
    print(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "unavailable")
except (TypeError, ValueError, json.JSONDecodeError):
    print("unavailable")
PY
)
if read_browser_target; then
  browser_target_diagnostics=$(capture_browser_target_diagnostics "$browser_target_run_id" \
    "$browser_sample_watermark_before" "$browser_sample_watermark_after" \
    "$browser_collector_before" "$browser_collector_after" \
    "$browser_target_retention_action_id" \
    || printf '{"available":false,"targetRunId":"%s"}' "$browser_target_run_id")
else
  browser_target_run_id='unavailable'
  browser_target_diagnostics='{"available":false,"targetRunId":"unavailable"}'
fi
printf 'AURA_E2E browser_target_diagnostics=%s\n' "$browser_target_diagnostics" >&2
if [ "$browser_status" -ne 0 ]; then
  printf 'AURA_E2E browser phase failed status=%s; post-browser boundary samples were attempted\n' \
    "$browser_status" >&2
  exit "$browser_status"
fi
if [ "$browser_after_sample_status" -ne 0 ]; then
  printf '%s\n' 'AURA_E2E post-browser boundary samples unavailable' >&2
  exit 1
fi
if [ "$browser_delta_status" -ne 0 ]; then
  printf '%s\n' 'AURA_E2E post-browser boundary deltas were invalid' >&2
  exit 1
fi
test -s "$browser_state"
if [ "$foundation_scope" -eq 1 ]; then
  # The million-row analytics scan-boundary stress fixture belongs to the
  # full matrix, not Scenario 1.  Do not create it in foundation mode; retain
  # an explicit unavailable marker while keeping the required privacy and
  # retention evidence below authoritative.
  printf '%s\n' 'AURA_E2E foundation scope: ClickHouse scan-boundary probe not-sampled' >&2
else
  run_scan_boundary_probe
fi
if [ "$foundation_scope" -eq 0 ]; then
  artifact_operation_after=$(capture_observatory_metric_total \
    operations_workflow observatory_evaluation_artifact_operation_count) || {
    record_metric_capability_blocker observatory_evaluation_artifact_operation_count
    artifact_operation_after='{"value":0,"samples":0}'
  }
  if ! assert_observatory_metric_increment operations_workflow \
    "$artifact_operation_before" "$artifact_operation_after" \
    observatory_evaluation_artifact_operation_count; then
    record_metric_capability_blocker observatory_evaluation_artifact_operation_count
  fi
fi
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
if [ "$foundation_scope" -eq 1 ]; then
  printf '%s\n' 'Observatory foundation acceptance passed; evaluation and outage scenarios were not run.'
  exit 0
fi
if [ "$operations_full_stack_enabled" = 1 ]; then
  operations_recovery_after=$(capture_observatory_metric_total \
    operations_workflow observatory_operations_recovery_count) || {
    record_metric_capability_blocker \
      'observatory_operations_recovery_count:public-workflow-unavailable'
    operations_recovery_after='{"value":0,"samples":0}'
  }
  if ! assert_observatory_metric_increment operations_workflow \
    "$operations_recovery_before" "$operations_recovery_after" \
    observatory_operations_recovery_count; then
    record_metric_capability_blocker \
      'observatory_operations_recovery_count:causal-increment-missing'
  fi
fi

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

# AURA-0098 dependency matrix. Each Observatory-owned dependency is stopped
# individually for a bounded interval and a real authenticated Core run is
# completed while it is unavailable; the exact service is restarted before the
# next phase. Shared aura-nats is Core infrastructure, so its phase proves the
# truthful degraded readiness response while stopped and only runs a normal
# Core probe after full recovery. The fixture provider is a separate
# disposable Compose project and is covered by the Core-independence probe.
# No operator volumes or normal deployment state are touched.
dependency_capability_blocked=0
dependency_capability_blockers=()
record_dependency_capability_blocker() {
  local capability=$1
  dependency_capability_blocked=1
  dependency_capability_blockers+=("$capability")
  printf 'AURA_E2E dependency_capability_blocker=%s\n' "$capability" >&2
}
run_dependency_phase_or_record() {
  local phase=$1 service=$2
  if run_dependency_outage_phase "$@"; then
    return 0
  else
    local phase_status=$?
  fi
  if [ "$phase_status" -eq 2 ]; then
    case "$service" in
      aura-garage)
        record_dependency_capability_blocker \
          'aura-garage:observatory_evaluation_artifact_operation_count'
        ;;
      observatory-evaluation-worker)
        record_dependency_capability_blocker \
          'observatory-evaluation-worker:worker-heartbeat-or-operation-signal'
        ;;
      observatory-evaluation-scheduler)
        record_dependency_capability_blocker \
          'observatory-evaluation-scheduler:observatory_evaluation_scheduler_heartbeat_age_seconds'
        ;;
      *)
        record_dependency_capability_blocker "$service:public-recovery-signal"
        ;;
    esac
    printf 'AURA_E2E dependency_phase=failed public-capability-prerequisite\n' >&2
    return 1
  fi
  return "$phase_status"
}

run_shared_nats_phase_or_record() {
  if run_shared_nats_outage_phase "$@"; then
    return 0
  else
    local phase_status=$?
  fi
  if [ "$phase_status" -eq 2 ]; then
    record_dependency_capability_blocker \
      'aura-nats:evaluation_runner_nats_recovery_count'
    printf 'AURA_E2E dependency_phase=failed public-capability-prerequisite\n' >&2
    return 1
  fi
  return "$phase_status"
}

run_dependency_phase_or_record collector observatory-collector
run_dependency_phase_or_record observatory_postgres observatory-postgres
run_dependency_phase_or_record clickhouse observatory-clickhouse
run_shared_nats_phase_or_record nats aura-nats
run_dependency_phase_or_record garage aura-garage
run_dependency_phase_or_record session observatory-session
run_dependency_phase_or_record evaluation_worker observatory-evaluation-worker
run_dependency_phase_or_record evaluation_scheduler observatory-evaluation-scheduler
run_fixture_ollama_outage_phase fixture_ollama
# The judge phase is a required public-boundary acceptance.  A provider health
# probe is not a substitute; failure is propagated instead of being converted
# into a capability blocker that lets the composed run appear green.
run_fixture_judge_outage_phase fixture_judge
run_observatory_api_outage_phase
run_observatory_web_outage_phase

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
if [ "$dependency_capability_blocked" -ne 0 ] \
  || [ "$metric_capability_blocked" -ne 0 ]; then
  if [ "$dependency_capability_blocked" -ne 0 ]; then
    printf 'AURA_E2E full-stack acceptance failed public dependency evidence: %s\n' \
      "$(IFS=,; printf '%s' "${dependency_capability_blockers[*]}")" >&2
  fi
  if [ "$metric_capability_blocked" -ne 0 ]; then
    printf 'AURA_E2E full-stack acceptance blocked by missing public metric evidence: %s\n' \
      "$(IFS=,; printf '%s' "${metric_capability_blockers[*]}")" >&2
  fi
  exit 1
fi
printf '%s\n' 'Observatory full-stack acceptance passed; Core remained healthy after Observatory stop.'
