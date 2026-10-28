#!/bin/sh
set -eu

# The upstream SearXNG process never binds beyond loopback: the adapter is
# the only listener reachable on the provider network.
secret_file=${SEARXNG_SECRET_KEY_FILE:?missing SearXNG secret file}
[ -r "$secret_file" ] || exit 78
secret_key=$(cat "$secret_file")
[ -n "$secret_key" ] || exit 78
SEARXNG_SECRET_KEY="$secret_key" SEARXNG_PORT=8081 /usr/local/searxng/entrypoint.sh &
searxng_pid=$!
cleanup() {
  kill "$searxng_pid" 2>/dev/null || true
  wait "$searxng_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

exec /usr/local/searxng/.venv/bin/python -m uvicorn searxng_adapter:app \
  --host 0.0.0.0 --port 8080 --no-access-log
