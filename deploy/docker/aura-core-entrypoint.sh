#!/bin/sh
set -eu

if [ -z "${AURA_DATABASE_URL:-}" ] && [ -n "${AURA_DATABASE_PASSWORD_FILE:-}" ]; then
  db_password="$(cat "$AURA_DATABASE_PASSWORD_FILE")"
  db_password_encoded="$(AURA_DB_PASSWORD="$db_password" python -c 'import os; from urllib.parse import quote; print(quote(os.environ["AURA_DB_PASSWORD"], safe=""))')"
  export AURA_DATABASE_URL="postgresql+psycopg://${AURA_DATABASE_USER:-aura}:${db_password_encoded}@${AURA_DATABASE_HOST:-aura-core-postgres}:${AURA_DATABASE_PORT:-5432}/${AURA_DATABASE_NAME:-aura}"
  unset db_password db_password_encoded AURA_DB_PASSWORD
fi

if [ -n "${AURA_NATS_PASSWORD_FILE:-}" ]; then
  nats_password="$(cat "$AURA_NATS_PASSWORD_FILE")"
  nats_password_encoded="$(AURA_NATS_PASSWORD="$nats_password" python -c 'import os; from urllib.parse import quote; print(quote(os.environ["AURA_NATS_PASSWORD"], safe=""))')"
  export AURA_NATS_URL="nats://${AURA_NATS_USER:-aura}:${nats_password_encoded}@aura-nats:4222"
  unset nats_password nats_password_encoded AURA_NATS_PASSWORD
fi

if [ -n "${AURA_VALKEY_PASSWORD_FILE:-}" ]; then
  valkey_password="$(cat "$AURA_VALKEY_PASSWORD_FILE")"
  valkey_password_encoded="$(AURA_VALKEY_PASSWORD="$valkey_password" python -c 'import os; from urllib.parse import quote; print(quote(os.environ["AURA_VALKEY_PASSWORD"], safe=""))')"
  export AURA_VALKEY_URL="redis://:${valkey_password_encoded}@aura-valkey:6379/0"
  unset valkey_password valkey_password_encoded AURA_VALKEY_PASSWORD
fi

if [ -z "${AURA_OIDC_CLIENT_SECRET:-}" ] && [ -n "${AURA_OIDC_CLIENT_SECRET_FILE:-}" ]; then
  export AURA_OIDC_CLIENT_SECRET="$(cat "$AURA_OIDC_CLIENT_SECRET_FILE")"
fi

exec "$@"
