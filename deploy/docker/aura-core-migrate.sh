#!/bin/sh
set -eu

cd /app/services/aura-core
# Runtime images deliberately install only production dependencies. Invoke the
# image venv directly so migration never tries to resolve development tools at
# container startup.
attempt=1
while ! /app/.venv/bin/python -m aura_core.entrypoints.cli migrate; do
  if [ "$attempt" -ge 30 ]; then
    echo "Core migration could not connect to PostgreSQL after 30 attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done
/app/.venv/bin/python -m aura_core.entrypoints.cli seed
