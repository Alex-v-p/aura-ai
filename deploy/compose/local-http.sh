#!/usr/bin/env bash
set -Eeuo pipefail

# Start Aura with a disposable, loopback-only HTTP Authentik deployment. This
# script owns only the ignored local identity inputs (.env.local-http and one
# password file), plus the optional loopback Ollama relay; Compose owns the
# disposable named volumes. It never edits the tracked .env or emits credentials.

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
ENV_FILE=${AURA_ENV_FILE:-"$ROOT/.env"}
LOCAL_ENV_FILE=${AURA_LOCAL_ENV_FILE:-"$ROOT/.env.local-http"}
COMPOSE=(docker compose --env-file "$ENV_FILE" --env-file "$LOCAL_ENV_FILE" \
  -f "$ROOT/compose.yaml" -f "$ROOT/deploy/compose/local-identity.yaml" \
  -f "$ROOT/deploy/compose/local-http.yaml" --profile local-identity \
  --profile observatory)
COMPOSE_BOOTSTRAP=("${COMPOSE[@]}" --profile local-http-bootstrap)
# Compose gives ambient shell variables precedence over every --env-file.
# Remove only the fixed local-HTTP identity and relay URL values so generated
# .env.local-http remains authoritative for owner/relay settings and optional
# values keep their normal configured precedence.
COMPOSE_ENV_UNSET=(env
  -u AURA_PUBLIC_ORIGIN
  -u AURA_OIDC_ISSUER
  -u AURA_OIDC_AUDIENCE
  -u AURA_OIDC_CLIENT_ID
  -u AURA_OIDC_REDIRECT_URI
  -u AURA_OWNER_SUBJECT
  -u AURA_OBSERVATORY_OWNER_SUBJECT
  -u AURA_SECURE_COOKIES
  -u AUTHENTIK_HOST_BROWSER)
COMPOSE_ENV_UNSET+=( -u AURA_OLLAMA_URL )

OLLAMA_RELAY_UNIT=aura-local-ollama-relay.service
OLLAMA_RELAY_STARTED=0
OLLAMA_RELAY_URL=

die() {
  printf 'local-http: %s\n' "$1" >&2
  exit 1
}

read_env_value() {
  local key=$1 file=$2
  [ -f "$file" ] || return 0
  awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "=" {
      sub("^[[:space:]]*" key "=", "")
      print
      exit
    }
  ' "$file"
}

configured_value() {
  local key=$1 value
  value=${!key-}
  if [ -z "$value" ]; then
    value=$(read_env_value "$key" "$LOCAL_ENV_FILE")
  fi
  if [ -z "$value" ]; then
    value=$(read_env_value "$key" "$ENV_FILE")
  fi
  printf '%s' "$value"
}

absolute_path() {
  case $1 in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$ROOT" "$1" ;;
  esac
}

validate_relay_port() {
  local port=$1
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || die "AURA_LOCAL_OLLAMA_RELAY_PORT must be 1-5 decimal digits"
  (( 10#$port >= 1 && 10#$port <= 65535 )) || die "AURA_LOCAL_OLLAMA_RELAY_PORT must be 1-65535"
}

validate_relay_target() {
  local target=$1 host port octet octet_a octet_b octet_c octet_d
  [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{1,5}$ ]] ||
    die "AURA_LOCAL_OLLAMA_RELAY_TARGET must be an IPv4 address and port"
  host=${target%:*}
  port=${target##*:}
  IFS=. read -r octet_a octet_b octet_c octet_d <<<"$host"
  for octet in "$octet_a" "$octet_b" "$octet_c" "$octet_d"; do
    (( 10#$octet >= 0 && 10#$octet <= 255 )) || die "AURA_LOCAL_OLLAMA_RELAY_TARGET has an invalid IPv4 address"
  done
  validate_relay_port "$port"
  case "$host" in
    0.*|255.255.255.255) die "AURA_LOCAL_OLLAMA_RELAY_TARGET must be a routable host" ;;
  esac
}

stop_ollama_relay() {
  command -v systemctl >/dev/null 2>&1 || return 0
  if systemctl --user show "$OLLAMA_RELAY_UNIT" >/dev/null 2>&1; then
    systemctl --user stop "$OLLAMA_RELAY_UNIT" >/dev/null 2>&1 || true
    systemctl --user reset-failed "$OLLAMA_RELAY_UNIT" >/dev/null 2>&1 || true
  fi
}

cleanup_failed_up() {
  local status=$?
  trap - ERR INT TERM EXIT
  if [ "$status" -ne 0 ] && [ "$OLLAMA_RELAY_STARTED" -eq 1 ]; then
    stop_ollama_relay
    OLLAMA_RELAY_STARTED=0
  fi
  return "$status"
}

cleanup_interrupted_up() {
  local status=$1
  trap - ERR INT TERM EXIT
  if [ "$OLLAMA_RELAY_STARTED" -eq 1 ]; then
    stop_ollama_relay
    OLLAMA_RELAY_STARTED=0
  fi
  exit "$status"
}

start_ollama_relay() {
  local target relay_port target_host target_port target_first_octet
  local -a relay_command
  OLLAMA_RELAY_STARTED=0
  OLLAMA_RELAY_URL=
  target=$(configured_value AURA_LOCAL_OLLAMA_RELAY_TARGET)
  stop_ollama_relay
  [ -n "$target" ] || return 0
  relay_port=$(configured_value AURA_LOCAL_OLLAMA_RELAY_PORT)
  relay_port=${relay_port:-11435}
  validate_relay_target "$target"
  validate_relay_port "$relay_port"
  command -v systemd-run >/dev/null 2>&1 || die "AURA_LOCAL_OLLAMA_RELAY_TARGET requires systemd-run"
  command -v systemctl >/dev/null 2>&1 || die "AURA_LOCAL_OLLAMA_RELAY_TARGET requires systemctl"
  command -v socat >/dev/null 2>&1 || die "AURA_LOCAL_OLLAMA_RELAY_TARGET requires socat"
  target_host=${target%:*}
  target_port=${target##*:}
  target_first_octet=${target_host%%.*}
  if (( 10#$target_first_octet == 127 && 10#$target_port == 10#$relay_port )); then
    die "AURA_LOCAL_OLLAMA_RELAY_TARGET must not self-forward the loopback listener"
  fi
  relay_command=(
    socat
    "TCP-LISTEN:${relay_port},bind=127.0.0.1,reuseaddr,fork"
    "TCP:${target_host}:${target_port}"
  )
  systemd-run --user --quiet --unit="$OLLAMA_RELAY_UNIT" --collect \
    --property=Restart=on-failure --property=RestartSec=2s -- \
    "${relay_command[@]}" ||
    die "could not start the loopback Ollama relay"
  OLLAMA_RELAY_STARTED=1
  trap cleanup_failed_up ERR EXIT
  trap 'cleanup_interrupted_up 130' INT
  trap 'cleanup_interrupted_up 143' TERM
  if ! systemctl --user is-active --quiet "$OLLAMA_RELAY_UNIT"; then
    stop_ollama_relay
    OLLAMA_RELAY_STARTED=0
    die "the loopback Ollama relay did not remain active"
  fi
  OLLAMA_RELAY_URL=http://host.docker.internal:$relay_port
}

ensure_owner_password() {
  local configured path
  configured=$(configured_value AUTHENTIK_OWNER_PASSWORD_FILE)
  path=$(absolute_path "${configured:-./.secrets/authentik-owner-password}")
  umask 077
  mkdir -p "$(dirname -- "$path")"
  python3 - "$path" <<'PY'
import os
import secrets
import stat
import sys

path = sys.argv[1]
if not hasattr(os, "O_NOFOLLOW"):
    raise SystemExit("owner password creation requires no-follow filesystem support")


def validate_existing() -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise SystemExit("owner password path must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("owner password path is not a regular file")
    if metadata.st_mode & 0o077 or not metadata.st_mode & 0o400:
        raise SystemExit("owner password file permissions must be owner-readable only")
    if metadata.st_size == 0:
        raise SystemExit("owner password file is empty")


while True:
    try:
        os.lstat(path)
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            payload = (secrets.token_urlsafe(32) + "\n").encode("utf-8")
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    else:
        validate_existing()
        break
PY
}

write_local_environment() {
  local subject=$1 relay_url=${2:-} owner_username owner_name owner_password_file temp
  case $subject in
    ""|*[!A-Za-z0-9._:-]*) die "invalid generated owner subject" ;;
  esac
  owner_username=$(configured_value AUTHENTIK_OWNER_USERNAME)
  owner_name=$(configured_value AUTHENTIK_OWNER_NAME)
  owner_password_file=$(configured_value AUTHENTIK_OWNER_PASSWORD_FILE)
  owner_username=${owner_username:-aura-owner}
  owner_name=${owner_name:-Aura Owner}
  owner_password_file=${owner_password_file:-./.secrets/authentik-owner-password}
  case "$owner_username$owner_name$owner_password_file" in
    *$'\n'*|*$'\r'*) die "local identity values must be single-line values" ;;
  esac
  temp=$(mktemp "$ROOT/.env.local-http.XXXXXX")
  chmod 600 "$temp"
  cat >"$temp" <<EOF
# Generated local-only development overlay. Do not commit.
AURA_PUBLIC_ORIGIN=http://aura.localhost:4200
AURA_OIDC_ISSUER=http://authentik.localhost:9000/application/o/aura-web/
AURA_OIDC_AUDIENCE=aura-web
AURA_OIDC_CLIENT_ID=aura-web
AURA_OIDC_REDIRECT_URI=http://aura.localhost:4200/api/v1/auth/callback
AURA_OWNER_SUBJECT=$subject
AURA_OBSERVATORY_OWNER_SUBJECT=$subject
AURA_SECURE_COOKIES=false
AUTHENTIK_HOST_BROWSER=http://authentik.localhost:9000
AUTHENTIK_OWNER_USERNAME=$owner_username
AUTHENTIK_OWNER_NAME=$owner_name
AUTHENTIK_OWNER_PASSWORD_FILE=$owner_password_file
EOF
  if [ -n "$relay_url" ]; then
    printf 'AURA_OLLAMA_URL=%s\n' "$relay_url" >>"$temp"
  fi
  mv -f "$temp" "$LOCAL_ENV_FILE"
}

compose() {
  "${COMPOSE_ENV_UNSET[@]}" "${COMPOSE[@]}" "$@"
}

bootstrap_owner() {
  local output subject relay_url=$1
  output=$("${COMPOSE_ENV_UNSET[@]}" "${COMPOSE_BOOTSTRAP[@]}" run --rm authentik-owner-bootstrap 2>&1) || {
    printf '%s\n' "$output" >&2
    die "Authentik owner bootstrap failed"
  }
  subject=$(printf '%s\n' "$output" | awk -F= '/^AURA_OWNER_SUBJECT=/{value=$2} END{print value}')
  [ -n "$subject" ] || die "owner bootstrap did not return a subject"
  write_local_environment "$subject" "$relay_url"
}

up() {
  local relay_url
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; copy .env.example to .env first"
  ensure_owner_password
  start_ollama_relay
  relay_url=$OLLAMA_RELAY_URL
  # Compose interpolates the whole project before selecting services.  A
  # temporary non-empty subject lets us start Authentik before discovering the
  # real hashed subject; it is replaced atomically after bootstrap.
  if [ ! -f "$LOCAL_ENV_FILE" ]; then
    write_local_environment bootstrap-pending "$relay_url"
  fi
  compose up --build --detach --wait authentik-server authentik-worker
  bootstrap_owner "$relay_url"
  compose up --build --detach --wait
  trap - ERR INT TERM EXIT
}

down() {
  stop_ollama_relay
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; copy .env.example to .env first"
  if [ -f "$LOCAL_ENV_FILE" ]; then
    compose down --remove-orphans
  else
    printf 'local-http: no local HTTP environment exists; nothing to stop\n'
  fi
}

reset() {
  stop_ollama_relay
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; copy .env.example to .env first"
  if [ -f "$LOCAL_ENV_FILE" ]; then
    # All durable mounts in the disposable overlay are project-scoped named
    # volumes. This explicit command is the only local launcher operation that
    # removes them; normal `down` preserves the disposable state.
    compose down --volumes --remove-orphans
    rm -f -- "$LOCAL_ENV_FILE"
    printf 'local-http: disposable state reset; owner password file preserved\n'
  else
    printf 'local-http: no local HTTP environment exists; nothing to reset\n'
  fi
}

verify_idempotency() {
  local configured password_path before_hash after_hash before_subject after_subject
  up
  configured=$(configured_value AUTHENTIK_OWNER_PASSWORD_FILE)
  password_path=$(absolute_path "${configured:-./.secrets/authentik-owner-password}")
  before_hash=$(sha256sum "$password_path" | awk '{print $1}')
  before_subject=$(read_env_value AURA_OWNER_SUBJECT "$LOCAL_ENV_FILE")
  up
  after_hash=$(sha256sum "$password_path" | awk '{print $1}')
  after_subject=$(read_env_value AURA_OWNER_SUBJECT "$LOCAL_ENV_FILE")
  [ "$before_hash" = "$after_hash" ] || die "owner password changed during repeated startup"
  [ "$before_subject" = "$after_subject" ] || die "owner subject changed during repeated startup"
  printf 'local-http: idempotency verified\n'
}

case ${1:-up} in
  up) up ;;
  down) down ;;
  reset) reset ;;
  verify-idempotency) verify_idempotency ;;
  *) die "usage: $0 {up|down|reset|verify-idempotency}" ;;
esac
