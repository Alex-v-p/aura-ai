#!/usr/bin/env bash
set -Eeuo pipefail

# AURA-0148's fail-closed aggregate runner.  The default path starts a
# disposable internal synthetic topology and observes each declared dependency
# over its health/protocol boundary before exercising public journeys.  It has
# no live egress, real provider profiles, or publication path.
# --all-upstream-checks additionally executes every required AURA-0142..0147
# check recorded in the integrated work items.  Any unavailable dependency,
# skipped command, or failed check is a failed gate.

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FIXTURE_DIR="$ROOT/deploy/compose/e2e/governed-research"
COMPOSE_FILE="$FIXTURE_DIR/compose.yaml"
if [ -n "${AURA_GOVERNED_RESEARCH_PROJECT:-}" ]; then
  # An explicit project is useful for a deliberate diagnostic session.  Keep
  # it bounded to Docker Compose's project-name grammar and make cleanup
  # ownership unambiguous for that caller.
  RUNNER_PROJECT="$AURA_GOVERNED_RESEARCH_PROJECT"
else
  # Every invocation owns a fresh namespace.  PID plus cryptographic entropy
  # avoids collisions when runners overlap or a shell reuses a process ID.
  RUNNER_SUFFIX=$(python3 -c 'import os,secrets; print(f"{os.getpid()}-{secrets.token_hex(4)}")')
  RUNNER_PROJECT="aura-governed-research-${RUNNER_SUFFIX}"
fi
if ! [[ "$RUNNER_PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  printf 'governed-research: invalid Compose project name: %s\n' "$RUNNER_PROJECT" >&2
  exit 64
fi
printf 'governed-research: project=%s\n' "$RUNNER_PROJECT"
export AURA_GOVERNED_RESEARCH_PROJECT="$RUNNER_PROJECT"
ALL_UPSTREAM=0

usage() {
  cat <<'EOF'
Usage: deploy/compose/run-governed-research-e2e.sh [--synthetic-only|--all-upstream-checks]

The default is --synthetic-only.  --all-upstream-checks runs the exact
AURA-0142..AURA-0147 required-check inventories before the synthetic gate.
EOF
}

case "${1:-}" in
  "") ;;
  --synthetic-only) ;;
  --all-upstream-checks) ALL_UPSTREAM=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 64 ;;
esac

for required in python3 docker uv pnpm; do
  command -v "$required" >/dev/null 2>&1 || {
    printf 'governed-research: mandatory dependency missing: %s\n' "$required" >&2
    exit 70
  }
done

# Keep all generated evidence in a disposable directory outside the repository.
# It is removed by the trap and is never uploaded or copied into the repository;
# keeping it outside the checkout also ensures repository-shape checks observe
# only version-controlled top-level paths.
REPORT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/aura-governed-research.XXXXXX")
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ] && [ -s "$REPORT_ROOT/upstream.json" ]; then
    python3 - "$REPORT_ROOT/upstream.json" <<'PY' >&2 || true
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failed = [
    (item.get("name"), item.get("failureClass", "command_failed"))
    for item in document.get("checks", [])
    if item.get("status") != "passed"
]
for name, failure_class in failed:
    print(f"governed-research upstream failure: {name} [{failure_class}]")
PY
  fi
  docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$REPORT_ROOT"
  exit "$status"
}
trap cleanup EXIT INT TERM
umask 077
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$REPORT_ROOT/pycache"
export AURA_GOVERNED_RESEARCH_REPORT_ROOT="$REPORT_ROOT"

# Reserve disposable loopback ports before Compose creates the stack.  The
# defaults in compose.yaml keep manual diagnostics usable; runner executions
# always receive fresh ports and then verify the resolved mappings below.
read -r AURA_GOVERNED_RESEARCH_POSTGRES_PORT AURA_GOVERNED_RESEARCH_NATS_PORT AURA_GOVERNED_RESEARCH_VALKEY_PORT < <(
  python3 - <<'PY'
import socket

ports = []
for _ in range(3):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ports.append(str(sock.getsockname()[1]))
print(*ports)
PY
)
export AURA_GOVERNED_RESEARCH_POSTGRES_PORT AURA_GOVERNED_RESEARCH_NATS_PORT AURA_GOVERNED_RESEARCH_VALKEY_PORT

# Concurrent disposable validation stacks may reserve the fixture's default
# CIDRs.  Select four adjacent /24s from a private range that do not overlap
# any existing Docker network, while retaining the same topology and provider
# single-homing guarantees.
read -r AURA_GOVERNED_RESEARCH_STATE_SUBNET \
  AURA_GOVERNED_RESEARCH_STATIC_SUBNET \
  AURA_GOVERNED_RESEARCH_BROWSER_SUBNET \
  AURA_GOVERNED_RESEARCH_FRONTEND_SUBNET < <(
  python3 - <<'PY'
import hashlib
import ipaddress
import json
import os
import subprocess
import sys

network_ids = subprocess.check_output(
    ["docker", "network", "ls", "--quiet"], text=True
).split()
existing = []
if network_ids:
    documents = json.loads(
        subprocess.check_output(["docker", "network", "inspect", *network_ids], text=True)
    )
    for document in documents:
        for config in (document.get("IPAM") or {}).get("Config") or []:
            subnet = config.get("Subnet")
            if subnet:
                try:
                    existing.append(ipaddress.ip_network(subnet, strict=False))
                except ValueError:
                    continue

project = os.environ.get("AURA_GOVERNED_RESEARCH_PROJECT", "")
if not project:
    raise SystemExit("Compose project is required for subnet allocation")
# Start from a project-derived slot so overlapping invocations do not all race
# for the first free CIDR group.  The existing-network scan still prevents a
# collision with a topology created before this invocation.
start = int.from_bytes(hashlib.sha256(project.encode()).digest()[:2], "big") % 237
for group_offset in range(237):
    third_octet = (start + group_offset) % 237
    candidates = [
        ipaddress.ip_network(f"10.253.{third_octet + network_offset}.0/24")
        for network_offset in range(4)
    ]
    if all(
        not candidate.overlaps(network)
        for candidate in candidates
        for network in existing
    ):
        print(*candidates)
        break
else:
    print("no non-overlapping disposable Docker subnet group available", file=sys.stderr)
    raise SystemExit(1)
PY
)
export AURA_GOVERNED_RESEARCH_STATE_SUBNET \
  AURA_GOVERNED_RESEARCH_STATIC_SUBNET \
  AURA_GOVERNED_RESEARCH_BROWSER_SUBNET \
  AURA_GOVERNED_RESEARCH_FRONTEND_SUBNET

# Compose must actually start and wait for every dependency.  No image is built
# or published by this runner; the fixture uses only the declared disposable
# images.  A failure in any health or protocol observation is retained as a
# failed observation and reaches the synthetic gate.
docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" config --quiet

docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" up --detach --wait

# The overlap regression can opt into a bounded hold after Compose has
# reported every service healthy.  It is deliberately inert unless both
# synchronization paths are supplied by the test; normal deployments never
# wait on a filesystem rendezvous.
if [ -n "${AURA_GOVERNED_RESEARCH_HOLD_READY_FILE:-}" ] \
  || [ -n "${AURA_GOVERNED_RESEARCH_HOLD_RELEASE_FILE:-}" ]; then
  if [ -z "${AURA_GOVERNED_RESEARCH_HOLD_READY_FILE:-}" ] \
    || [ -z "${AURA_GOVERNED_RESEARCH_HOLD_RELEASE_FILE:-}" ]; then
    printf 'governed-research: hold requires ready and release files\n' >&2
    exit 64
  fi
  case "$AURA_GOVERNED_RESEARCH_HOLD_READY_FILE:$AURA_GOVERNED_RESEARCH_HOLD_RELEASE_FILE" in
    *[[:space:]]*)
      printf 'governed-research: hold paths may not contain whitespace\n' >&2
      exit 64
      ;;
  esac
  HOLD_STARTED=$(date +%s)
  : > "$AURA_GOVERNED_RESEARCH_HOLD_READY_FILE"
  while [ ! -e "$AURA_GOVERNED_RESEARCH_HOLD_RELEASE_FILE" ]; do
    if [ "$(( $(date +%s) - HOLD_STARTED ))" -ge 180 ]; then
      printf 'governed-research: bounded hold expired before release\n' >&2
      exit 75
    fi
    sleep 1
  done
fi

declare -A OBSERVATIONS=(
  [postgres]=false
  [nats]=false
  [valkey]=false
  [garage]=false
  [static_fetcher]=false
  [browser_fetcher]=false
  [frontend]=false
)

service_healthy() {
  local service=$1 container state health
  container=$(docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" ps -q "$service")
  [ -n "$container" ] || return 1
  state=$(docker inspect --format '{{.State.Status}}' "$container")
  health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
  [ "$state" = running ] && [ "$health" = healthy ]
}

observe() {
  local name=$1 service=$2
  shift 2
  if service_healthy "$service" && "$@" >/dev/null 2>&1; then
    OBSERVATIONS["$name"]=true
  fi
}

observe postgres postgres docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T postgres pg_isready -U aura_fixture -d aura_fixture
observe nats nats docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T nats wget --spider --quiet http://127.0.0.1:8222/varz
observe valkey valkey docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T valkey valkey-cli ping
observe garage garage docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T garage /garage --config /fixture/garage.toml status
observe static_fetcher synthetic-static-fetcher docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T synthetic-static-fetcher python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"
observe browser_fetcher synthetic-browser-fetcher docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T synthetic-browser-fetcher python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"
observe frontend synthetic-frontend docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T synthetic-frontend python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"

NETWORK_ISOLATED=true
for provider in synthetic-static-fetcher synthetic-browser-fetcher; do
  for target in postgres nats valkey garage; do
    if docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" exec -T "$provider" \
      python -c "import socket; a=socket.getaddrinfo('$target',None); socket.create_connection((a[0][4][0],1),.2)" >/dev/null 2>&1; then
      NETWORK_ISOLATED=false
    fi
  done
done

if [ -n "${AURA_GOVERNED_RESEARCH_INJECT_FAILURE:-}" ]; then
  OBSERVATIONS["$AURA_GOVERNED_RESEARCH_INJECT_FAILURE"]=false
fi

python3 - "$REPORT_ROOT/observations.json" "${OBSERVATIONS[postgres]}" "${OBSERVATIONS[nats]}" "${OBSERVATIONS[valkey]}" "${OBSERVATIONS[garage]}" "${OBSERVATIONS[static_fetcher]}" "${OBSERVATIONS[browser_fetcher]}" "${OBSERVATIONS[frontend]}" <<'PY'
import json
import sys
from pathlib import Path

names = ("postgres", "nats", "valkey", "garage", "static_fetcher", "browser_fetcher", "frontend")
Path(sys.argv[1]).write_text(
    json.dumps({name: value == "true" for name, value in zip(names, sys.argv[2:], strict=True)}, separators=(",", ":"))
    + "\n",
    encoding="utf-8",
)
PY
printf '{"static_fetcher_to_state":%s,"browser_fetcher_to_state":%s}\n' \
  "$NETWORK_ISOLATED" "$NETWORK_ISOLATED" > "$REPORT_ROOT/network-isolation.json"

python3 - "$REPORT_ROOT" "$FIXTURE_DIR" <<'PY'
import py_compile
import sys
from pathlib import Path

destination = Path(sys.argv[1]) / "pyc"
destination.mkdir()
for source in Path(sys.argv[2]).glob("*.py"):
    py_compile.compile(source, cfile=destination / f"{source.name}.pyc", doraise=True)
PY

host_port() {
  local service=$1 container_port=$2 mapping host hostport
  mapping=$(docker compose -p "$RUNNER_PROJECT" -f "$COMPOSE_FILE" port "$service" "$container_port")
  host=${mapping%:*}
  hostport=${mapping##*:}
  [ "$host" = "127.0.0.1" ] || {
    printf 'governed-research: %s/%s is not loopback-bound: %s\n' "$service" "$container_port" "$mapping" >&2
    return 1
  }
  case "$hostport" in
    (''|*[!0-9]*)
      printf 'governed-research: invalid host port for %s/%s: %s\n' "$service" "$container_port" "$mapping" >&2
      return 1
      ;;
  esac
  printf '%s' "$hostport"
}

# Current-state-compatible upstream checks use disposable loopback-only host
# ports.  The providers remain single-homed and cannot reach these endpoints.
POSTGRES_PORT=$(host_port postgres 5432)
NATS_PORT=$(host_port nats 4222)
VALKEY_PORT=$(host_port valkey 6379)
export AURA_TEST_DATABASE_URL="postgresql+psycopg://aura_fixture:fixture-only@127.0.0.1:${POSTGRES_PORT}/aura_fixture"
export AURA_0143_MIGRATION_DATABASE_URL="$AURA_TEST_DATABASE_URL"
# AURA-0143's archived document SQL command still uses the historical
# AURA_0068 name; point that public test input at the same disposable endpoint
# without changing the recorded command.
export AURA_0068_DATABASE_URL="$AURA_TEST_DATABASE_URL"
export AURA_TEST_NATS_URL="nats://127.0.0.1:${NATS_PORT}"
export AURA_TEST_VALKEY_URL="redis://127.0.0.1:${VALKEY_PORT}/0"
export AURA_TEST_DEPENDENCIES_ISOLATED=1
export AURA_GOVERNED_RESEARCH_COMPOSE_FILE="$COMPOSE_FILE"

# The dedicated research-origin observation regression is exercised by the
# focused Observatory suite, not this 23-ID full-stack acceptance inventory.
# Keep the inventory closed while still running the complete governed journey
# and artifact/task acceptance set here.
PYTHON_ACCEPTANCE_COMMAND="UV_CACHE_DIR=$ROOT/.local-state/uv-cache uv run pytest -p no:cacheprovider services/aura-core/tests/test_aura0148_governed_research.py services/aura-observatory/tests/test_aura0148_evaluation_privacy.py services/aura-core/tests/test_research_budget_evaluation.py::test_wall_clock_deadline_stops_execution_before_provider_dispatch services/aura-core/tests/test_research_budget_evaluation.py::test_wall_clock_expiry_has_exact_terminal_failure_labels -k 'not test_complete_research_origin_observation_cannot_be_satisfied_by_admission_only' --junitxml=$REPORT_ROOT/python-acceptance.xml -q"
set +e
bash -lc "$PYTHON_ACCEPTANCE_COMMAND" >/dev/null 2>&1
PYTHON_ACCEPTANCE_EXIT=$?
set -e
if [ ! -s "$REPORT_ROOT/python-acceptance.xml" ]; then
  printf 'governed-research: Python acceptance did not produce JUnit evidence (exit %s)\n' "$PYTHON_ACCEPTANCE_EXIT" >&2
  exit 1
fi

WEB_PORT=$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)
PLAYWRIGHT_ACCEPTANCE_COMMAND="cd $ROOT/frontend && AURA_WEB_E2E_APP_PORT=$WEB_PORT AURA_WEB_E2E_REUSE_SERVERS=0 AURA_WEB_E2E_OUTPUT_DIR=$REPORT_ROOT/playwright-results PLAYWRIGHT_JUNIT_OUTPUT_FILE=$REPORT_ROOT/playwright-acceptance.xml pnpm exec playwright test --config apps/aura-web-e2e/playwright.config.ts src/governed-research-acceptance.spec.ts src/governed-research-accessibility.spec.ts --reporter=junit"
set +e
bash -lc "$PLAYWRIGHT_ACCEPTANCE_COMMAND" >/dev/null 2>&1
PLAYWRIGHT_ACCEPTANCE_EXIT=$?
set -e
for acceptance_report in "$REPORT_ROOT/python-acceptance.xml" "$REPORT_ROOT/playwright-acceptance.xml"; do
  if [ ! -s "$acceptance_report" ]; then
    printf 'governed-research: acceptance did not produce JUnit evidence: %s\n' "$acceptance_report" >&2
    exit 1
  fi
done

python3 "$FIXTURE_DIR/acceptance_results.py" \
  --python-xml "$REPORT_ROOT/python-acceptance.xml" \
  --playwright-xml "$REPORT_ROOT/playwright-acceptance.xml" \
  --python-command "$PYTHON_ACCEPTANCE_COMMAND" \
  --playwright-command "$PLAYWRIGHT_ACCEPTANCE_COMMAND" \
  --python-exit-code "$PYTHON_ACCEPTANCE_EXIT" \
  --playwright-exit-code "$PLAYWRIGHT_ACCEPTANCE_EXIT" \
  --report "$REPORT_ROOT/observed-acceptance.json"

if [ "$ALL_UPSTREAM" -eq 1 ]; then
  python3 "$FIXTURE_DIR/upstream_checks.py" \
    --root "$ROOT" \
    --report "$REPORT_ROOT/upstream.json"
fi

python3 "$FIXTURE_DIR/fixture.py" \
  --observations "$REPORT_ROOT/observations.json" \
  --acceptance "$REPORT_ROOT/observed-acceptance.json" \
  --report "$REPORT_ROOT/synthetic.json"

PYTHONPATH="$ROOT/services/aura-core/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  UV_CACHE_DIR="$ROOT/.local-state/uv-cache" uv run python "$FIXTURE_DIR/evaluation_adapter.py" \
    --root "$ROOT" \
    --observed "$REPORT_ROOT/observed-acceptance.json" \
    --report "$REPORT_ROOT/evaluation-gate.json"

python3 - "$REPORT_ROOT" "$ALL_UPSTREAM" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
all_upstream = sys.argv[2] == "1"
paths = [
    root / "observations.json",
    root / "network-isolation.json",
    root / "synthetic.json",
    root / "observed-acceptance.json",
    root / "evaluation-gate.json",
]
if all_upstream:
    paths.append(root / "upstream.json")

for path in paths:
    if not path.is_file():
        raise SystemExit(f"mandatory evidence is missing: {path.name}")
    document = json.loads(path.read_text(encoding="utf-8"))
    checks = document.get("checks")
    decisions = document.get("decisions")
    is_observation = path.name == "observations.json"
    is_gate = path.name == "evaluation-gate.json"
    if is_observation:
        if not isinstance(document, dict) or set(document) != {"postgres", "nats", "valkey", "garage", "static_fetcher", "browser_fetcher", "frontend"}:
            raise SystemExit("dependency observations are incomplete")
        if any(value is not True for value in document.values()):
            raise SystemExit("a dependency observation failed")
        continue
    if path.name == "network-isolation.json":
        if document != {"static_fetcher_to_state": True, "browser_fetcher_to_state": True}:
            raise SystemExit("provider network isolation observation failed")
        continue
    if is_gate:
        decisions = document.get("decisions")
        if (
            document.get("capturePolicy") != "metadata_only"
            or document.get("reportRevision") != "aura0148-gate-report-v1"
            or document.get("status") != "passed"
            or document.get("evaluatorCount") != 26
            or document.get("passedCount") != 26
            or document.get("failedCount") != 0
            or not isinstance(decisions, list)
        ):
            raise SystemExit("invalid evaluation release-gate envelope")
        identifiers = [item.get("evaluatorId") for item in decisions if isinstance(item, dict)]
        if (
            len(decisions) != 26
            or len(identifiers) != len(set(identifiers))
            or len(identifiers) != 26
            or any(
                not isinstance(item, dict)
                or set(item) != {"evaluatorId", "score", "threshold", "passed", "reason"}
                or item.get("passed") is not True
                for item in decisions
            )
        ):
            raise SystemExit("evaluation release gate is incomplete or failed")
        continue
    if path.name == "observed-acceptance.json":
        if (
            document.get("capturePolicy") != "metadata_only"
            or not isinstance(document.get("python"), dict)
            or not isinstance(document.get("playwright"), dict)
        ):
            raise SystemExit("invalid observed acceptance envelope")
        for surface in ("python", "playwright"):
            section = document[surface]
            if (
                section.get("exitCode") != 0
                or section.get("discovered", 0) <= 0
                or section.get("passed") != section.get("discovered")
                or section.get("failed") != 0
                or section.get("skipped") != 0
                or len(section.get("tests", [])) != section.get("discovered")
            ):
                raise SystemExit("observed acceptance tests are incomplete or failed")
        continue
    if document.get("capturePolicy") != "metadata-only" or not isinstance(checks, list):
        raise SystemExit(f"invalid mandatory evidence envelope: {path.name}")
    if not checks or any(item.get("status") != "passed" for item in checks):
        raise SystemExit(f"mandatory evidence did not pass: {path.name}")
    if any("skip" in str(item.get("status", "")).lower() for item in checks):
        raise SystemExit(f"skipped evidence cannot be promoted to success: {path.name}")

print("governed research synthetic full-stack acceptance passed")
if all_upstream:
    print("all AURA-0142..AURA-0147 mandatory checks passed")
PY
