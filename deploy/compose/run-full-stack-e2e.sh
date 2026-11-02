#!/bin/sh
# Test-only full-stack runner. It creates disposable state and fixture-only
# credentials; it never reads or writes operator deployment secrets.
set -eu

mode=${AURA_E2E_MODE:-external}
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
default_garage_image='dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a'
export AURA_GARAGE_IMAGE="${AURA_GARAGE_IMAGE:-$default_garage_image}"
case "$AURA_GARAGE_IMAGE" in
  *@sha256:*) ;;
  *) echo "AURA_GARAGE_IMAGE must be set to an operator-verified digest-qualified Garage image" >&2; exit 64 ;;
esac
# The browser suite covers notification inbox reconciliation, Last-Event-ID
# replay, and reconnect recovery against this disposable stack.
# Docker Desktop only permits bind sources it can see within the shared
# workspace. Keep this disposable, ignored directory beside the project
# rather than using /tmp, which may be private to the host shell.
fixture_root=$(mktemp -d "$root/.aura-e2e.XXXXXX")
fixture_suffix=$(basename "$fixture_root" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')
e2e_project="aura-e2e-$fixture_suffix"
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # Compose's service-list reconciliation can race a completed one-shot
    # config/bootstrap container and emit "No such container". Inspect by
    # project label so diagnostics never turn an already-captured failure
    # into a second, misleading failure.
    docker ps -a --filter "label=com.docker.compose.project=$e2e_project" \
      --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>&1 || true
    docker compose -p "$e2e_project" logs --no-color --tail=80 aura-core-postgres authentik-postgres authentik-server authentik-worker 2>&1 || true
  fi
  if [ "$status" -ne 0 ] && [ "${AURA_E2E_KEEP_ON_FAILURE:-0}" = "1" ]; then
    trap - EXIT INT TERM
    exit "$status"
  fi
  docker compose -p "$e2e_project" -f "$root/compose.yaml" down --remove-orphans >/dev/null 2>&1 || true
  docker compose -p aura-e2e-provider -f "$root/deploy/compose/e2e/provider.yaml" down --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$fixture_root"
  exit "$status"
}
trap cleanup EXIT INT TERM

compose_up_with_one_shot_retry() {
  attempt=1
  while :; do
    error_file="$fixture_root/compose-up-$attempt.stderr"
    if docker compose "$@" 2>"$error_file"; then
      cat "$error_file" >&2
      return 0
    fi
    status=$?
    cat "$error_file" >&2
    if [ "$attempt" -ge 2 ] || ! grep -Fq 'No such container:' "$error_file"; then
      return "$status"
    fi
    echo "Retrying Compose startup after one-shot container race" >&2
    attempt=$((attempt + 1))
  done
}

umask 077
for name in database oidc nats valkey garage-rpc garage-access garage-secret authentik-database authentik-key authentik-bootstrap authentik-valkey; do
  printf 'fixture-%s\n' "$name" > "$fixture_root/$name"
done
printf 'fixture-authentik-admin\n' > "$fixture_root/authentik-bootstrap"
# Deterministic fixture-only Garage credentials; these are never operator
# credentials and are discarded with the temporary state directory.
printf '0000000000000000000000000000000000000000000000000000000000000000\n' > "$fixture_root/garage-rpc"
printf 'GK000000000000000000000000\n' > "$fixture_root/garage-access"
printf '1111111111111111111111111111111111111111111111111111111111111111\n' > "$fixture_root/garage-secret"
mkdir "$fixture_root/state"
# Compose implements local secrets as bind mounts. The container runs as an
# unprivileged UID, so files must be readable there; the 0700 fixture parent
# remains private to this runner on the host.
chmod 0444 "$fixture_root"/database "$fixture_root"/oidc "$fixture_root"/nats "$fixture_root"/valkey \
  "$fixture_root"/garage-rpc "$fixture_root"/garage-access "$fixture_root"/garage-secret \
  "$fixture_root"/authentik-database "$fixture_root"/authentik-key \
  "$fixture_root"/authentik-bootstrap "$fixture_root"/authentik-valkey

for required_fixture in database oidc nats valkey garage-rpc garage-access garage-secret; do
  test -f "$fixture_root/$required_fixture"
done

export AURA_STATE_ROOT="$fixture_root/state"
export AURA_CORE_DATABASE_PASSWORD_FILE="$fixture_root/database"
export AURA_OIDC_CLIENT_SECRET_FILE="$fixture_root/oidc"
export AURA_NATS_PASSWORD_FILE="$fixture_root/nats"
export AURA_VALKEY_PASSWORD_FILE="$fixture_root/valkey"
export AURA_GARAGE_RPC_SECRET_FILE="$fixture_root/garage-rpc"
export AURA_GARAGE_ACCESS_KEY_FILE="$fixture_root/garage-access"
export AURA_GARAGE_SECRET_KEY_FILE="$fixture_root/garage-secret"

# A previous interrupted run can leave one-shot config/bootstrap containers
# with the same Compose project names. Reconcile only these disposable test
# projects before creating the new fixture; real startup failures still fail
# below and are reported by the normal trap.
docker compose -p "$e2e_project" -f "$root/compose.yaml" down --remove-orphans >/dev/null 2>&1 || true
docker compose -p aura-e2e -f "$root/compose.yaml" down --remove-orphans >/dev/null 2>&1 || true
docker compose -p aura-e2e-provider -f "$root/deploy/compose/e2e/provider.yaml" down --remove-orphans >/dev/null 2>&1 || true

case "$mode" in
  external)
    # These base inputs satisfy Compose interpolation. The external override
    # repeats them deliberately, keeping test-only HTTP origins out of the
    # normal deployment file.
    export AURA_OIDC_ISSUER=http://aura-e2e-oidc:8081
    export AURA_OIDC_AUDIENCE=aura-web
    export AURA_OIDC_CLIENT_ID=aura-web
    export AURA_OIDC_REDIRECT_URI=http://aura-web:8080/api/v1/auth/callback
    export AURA_OWNER_SUBJECT=owner
    export AURA_PUBLIC_ORIGIN=http://aura-web:8080
    docker compose --progress quiet -p aura-e2e-provider -f "$root/deploy/compose/e2e/provider.yaml" up --build --detach --wait
    compose_up_with_one_shot_retry --progress quiet -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/e2e/external-aura.yaml" --profile checks up --build --detach --wait aura-web aura-core-api aura-core-worker aura-core-scheduler
    docker compose --progress quiet -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/e2e/external-aura.yaml" --profile checks build frontend-e2e
    docker compose -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/e2e/external-aura.yaml" --profile checks run --rm --no-deps frontend-e2e
    ;;
  local-identity)
    if [ -n "${AURA_E2E_AUTHENTIK_IMAGE:-}" ]; then
      export AUTHENTIK_IMAGE="$AURA_E2E_AUTHENTIK_IMAGE"
    fi
    export AUTHENTIK_HOST_BROWSER=http://authentik-server:9000
    export AURA_OIDC_ISSUER=http://authentik-server:9000/application/o/aura-e2e/
    export AURA_OIDC_AUDIENCE=aura-e2e
    export AURA_OIDC_CLIENT_ID=aura-e2e
    export AURA_OIDC_REDIRECT_URI=http://aura-web:8080/api/v1/auth/callback
    # Authentik 2026.5's deterministic bootstrap user is assigned subject 4
    # in a fresh fixture database; keep this aligned with local-aura.yaml.
    export AURA_OWNER_SUBJECT=4
    export AURA_PUBLIC_ORIGIN=http://aura-web:8080
    export AUTHENTIK_DATABASE_PASSWORD_FILE="$fixture_root/authentik-database"
    export AUTHENTIK_SECRET_KEY_FILE="$fixture_root/authentik-key"
    export AUTHENTIK_BOOTSTRAP_PASSWORD_FILE="$fixture_root/authentik-bootstrap"
    export AUTHENTIK_VALKEY_PASSWORD_FILE="$fixture_root/authentik-valkey"
    compose_up_with_one_shot_retry --progress quiet -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/local-identity.yaml" -f "$root/deploy/compose/e2e/local-aura.yaml" --profile local-identity --profile checks up --build --detach --wait aura-web aura-core-api aura-core-worker aura-core-scheduler aura-e2e-ollama authentik-server authentik-worker
    # Authentik discovers and applies custom blueprints asynchronously after
    # its HTTP readiness endpoint turns green. Wait for the configured OIDC
    # discovery document so the browser test cannot race provider creation.
    oidc_attempt=1
    while ! docker compose -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/local-identity.yaml" -f "$root/deploy/compose/e2e/local-aura.yaml" --profile local-identity exec -T authentik-server /ak-root/.venv/bin/python -c "import urllib.request; urllib.request.urlopen('${AURA_OIDC_ISSUER}.well-known/openid-configuration', timeout=2)" >/dev/null 2>&1; do
      if [ "$oidc_attempt" -ge 60 ]; then
        echo "Authentik OIDC discovery did not become available" >&2
        exit 1
      fi
      oidc_attempt=$((oidc_attempt + 1))
      sleep 2
    done
    docker compose --progress quiet -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/local-identity.yaml" -f "$root/deploy/compose/e2e/local-aura.yaml" --profile local-identity --profile checks build frontend-e2e
    docker compose -p "$e2e_project" -f "$root/compose.yaml" -f "$root/deploy/compose/local-identity.yaml" -f "$root/deploy/compose/e2e/local-aura.yaml" --profile local-identity --profile checks run --rm --no-deps frontend-e2e
    ;;
  *)
    echo "AURA_E2E_MODE must be external or local-identity" >&2
    exit 64
    ;;
esac
