#!/usr/bin/env bash
set -Eeuo pipefail

# Start Aura with a disposable, loopback-only HTTP Authentik deployment.  This
# script owns only ignored local state (.env.local-http and one password file);
# it never edits the tracked .env or emits credentials.

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
ENV_FILE=${AURA_ENV_FILE:-"$ROOT/.env"}
LOCAL_ENV_FILE=${AURA_LOCAL_ENV_FILE:-"$ROOT/.env.local-http"}
COMPOSE=(docker compose --env-file "$ENV_FILE" --env-file "$LOCAL_ENV_FILE" \
  -f "$ROOT/compose.yaml" -f "$ROOT/deploy/compose/local-identity.yaml" \
  -f "$ROOT/deploy/compose/local-http.yaml" --profile local-identity)
COMPOSE_BOOTSTRAP=("${COMPOSE[@]}" --profile local-http-bootstrap)
# Compose gives ambient shell variables precedence over every --env-file.
# Remove only the fixed local-HTTP identity values so generated .env.local-http
# remains authoritative for AURA_OWNER_SUBJECT and optional values keep their
# normal configured precedence.
COMPOSE_ENV_UNSET=(env
  -u AURA_PUBLIC_ORIGIN
  -u AURA_OIDC_ISSUER
  -u AURA_OIDC_AUDIENCE
  -u AURA_OIDC_CLIENT_ID
  -u AURA_OIDC_REDIRECT_URI
  -u AURA_OWNER_SUBJECT
  -u AURA_SECURE_COOKIES
  -u AUTHENTIK_HOST_BROWSER)

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
  local subject=$1 owner_username owner_name owner_password_file temp
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
AURA_SECURE_COOKIES=false
AUTHENTIK_HOST_BROWSER=http://authentik.localhost:9000
AUTHENTIK_OWNER_USERNAME=$owner_username
AUTHENTIK_OWNER_NAME=$owner_name
AUTHENTIK_OWNER_PASSWORD_FILE=$owner_password_file
EOF
  mv -f "$temp" "$LOCAL_ENV_FILE"
}

compose() {
  "${COMPOSE_ENV_UNSET[@]}" "${COMPOSE[@]}" "$@"
}

bootstrap_owner() {
  local output subject
  output=$("${COMPOSE_ENV_UNSET[@]}" "${COMPOSE_BOOTSTRAP[@]}" run --rm authentik-owner-bootstrap 2>&1) || {
    printf '%s\n' "$output" >&2
    die "Authentik owner bootstrap failed"
  }
  subject=$(printf '%s\n' "$output" | awk -F= '/^AURA_OWNER_SUBJECT=/{value=$2} END{print value}')
  [ -n "$subject" ] || die "owner bootstrap did not return a subject"
  write_local_environment "$subject"
}

up() {
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; copy .env.example to .env first"
  ensure_owner_password
  # Compose interpolates the whole project before selecting services.  A
  # temporary non-empty subject lets us start Authentik before discovering the
  # real hashed subject; it is replaced atomically after bootstrap.
  if [ ! -f "$LOCAL_ENV_FILE" ]; then
    write_local_environment bootstrap-pending
  fi
  compose up --build --detach --wait authentik-server authentik-worker
  bootstrap_owner
  compose up --build --detach --wait
}

down() {
  [ -f "$ENV_FILE" ] || die "missing $ENV_FILE; copy .env.example to .env first"
  if [ -f "$LOCAL_ENV_FILE" ]; then
    compose down --remove-orphans
  else
    printf 'local-http: no local HTTP environment exists; nothing to stop\n'
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
  verify-idempotency) verify_idempotency ;;
  *) die "usage: $0 {up|down|verify-idempotency}" ;;
esac
