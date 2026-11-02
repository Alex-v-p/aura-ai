#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
# shellcheck source=core-availability.sh
. "$ROOT/deploy/compose/e2e/core-availability.sh"
original_public_surface_definition=$(declare -f assert_observatory_public_surfaces)
eval "${original_public_surface_definition/assert_observatory_public_surfaces/assert_observatory_public_surfaces_real}"

step_timeout=1
compose=(core_availability_test_compose)
core_availability_test_compose() {
  [ "${1:-}" = ps ] || return 1
  printf '%s\n' api-container
}
bounded() { return 0; }
wait_running() { return 0; }
wait_core_ready() { return 0; }
assert_observatory_public_surfaces() { return 0; }
assert_core_available() { return 0; }
assert_core_nats_unavailable() { return 0; }
assert_observatory_component_signal() { return 0; }
run_core_normal_run_phase() { return 41; }

run_and_capture_status() {
  local output_file=$1 expected_status=$2
  shift 2
  local status
  if "$@" >"$output_file" 2>&1; then
    status=0
  else
    status=$?
  fi
  if [ "$status" -ne "$expected_status" ]; then
    printf 'expected injected failure status %s, got %s\n' "$expected_status" "$status" >&2
    return 1
  fi
  if grep -Fq 'recovery=passed' "$output_file"; then
    printf '%s\n' 'intermediate outage failure reached a recovery success line' >&2
    return 1
  fi
}

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

# These calls deliberately execute as `if` conditions. Bash disables errexit
# for a function body reached through a conditional; the phase must therefore
# return the injected intermediate failure explicitly.
run_and_capture_status "$tmp_dir/dependency.out" 41 \
  run_dependency_outage_phase injected_dependency aura-garage
assert_core_available() { return 41; }
run_and_capture_status "$tmp_dir/nats.out" 41 \
  run_shared_nats_outage_phase injected_nats aura-nats
run_and_capture_status "$tmp_dir/judge.out" 41 \
  run_fixture_judge_outage_phase injected_judge

# The session credential is delivered only as stdin to docker exec. The fake
# docker command records argv, so this adversarial check fails if a credential
# is ever put back into process arguments.
docker_args="$tmp_dir/docker-args"
docker() {
  printf '<%s>\n' "$@" >>"$docker_args"
  if [ "${AURA_PUBLIC_FAIL_ONCE:-0}" = 1 ] && [ "${AURA_PUBLIC_FAIL_SEEN:-0}" = 0 ]; then
    AURA_PUBLIC_FAIL_SEEN=1
    return 41
  fi
  if [ "${AURA_SESSION_FAKE_STATUS:-503}" = 200 ]; then
    printf '%s\n' 200
  elif [ "${AURA_SESSION_FAKE_STATUS:-503}" = 401 ]; then
    printf '%s\n' 401
  else
    printf '%s\n' 503
  fi
}
session_sentinel='AURA0098_SESSION_ARG_SENTINEL'
run_nested_public_surface_failure() {
  AURA_PUBLIC_FAIL_ONCE=1
  AURA_PUBLIC_FAIL_SEEN=0
  assert_observatory_public_surfaces_real nested_public_surface
}
if run_nested_public_surface_failure >"$tmp_dir/public-surface.out" 2>&1; then
  printf '%s\n' 'nested public-surface failure was unexpectedly swallowed' >&2
  exit 1
fi
if grep -Fq 'observatory_public_surface=ok' "$tmp_dir/public-surface.out"; then
  printf '%s\n' 'nested public-surface failure reached a success line' >&2
  exit 1
fi
assert_observatory_session_degraded session_degraded "$session_sentinel"
if AURA_SESSION_FAKE_STATUS=401 assert_observatory_session_degraded session_expired "$session_sentinel"; then
  printf '%s\n' 'session probe accepted 401 as a session-store outage' >&2
  exit 1
fi
AURA_SESSION_FAKE_STATUS=200 assert_observatory_session_recovered session_recovered "$session_sentinel"
if grep -Fq "$session_sentinel" "$docker_args"; then
  printf '%s\n' 'session credential appeared in docker exec arguments' >&2
  exit 1
fi
if ! grep -Fq '<-i>' "$docker_args" || [ "$(grep -Fc '<-i>' "$docker_args")" -ne 3 ]; then
  printf '%s\n' 'session probes did not use stdin-enabled docker exec' >&2
  exit 1
fi
if ! grep -Fq 'sys.stdin.read().strip()' "$ROOT/deploy/compose/e2e/core-availability.sh"; then
  printf '%s\n' 'session probes no longer read credentials from stdin' >&2
  exit 1
fi
if grep -Eq "docker exec[^\\n]*python - [\\\"']?\\\$session_id" \
  "$ROOT/deploy/compose/e2e/core-availability.sh"; then
  printf '%s\n' 'session probe source still passes credentials as Python argv' >&2
  exit 1
fi

# The optional operations workflow must be the causal evidence boundary. A
# historical positive operations-recovery counter cannot satisfy the matrix;
# the baseline and post-workflow capture must both remain behind the explicit
# enablement gate, with a precise default blocker.
python3 - "$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh" <<'PY'
import re
import sys

source = open(sys.argv[1], encoding="utf-8").read()
gates = [
    match.start()
    for match in re.finditer(r'if \[ "\$operations_full_stack_enabled" = 1 \]; then', source)
]
before = source.index("operations_recovery_before=$(capture_observatory_metric_total")
after = source.index("operations_recovery_after=$(capture_observatory_metric_total")
if len(gates) < 2 or not (gates[0] < before < gates[1] < after):
    raise SystemExit("operations recovery metric capture escaped its authorized workflow gates")
if "observatory_operations_recovery_count:authorized-browser-workflow-not-enabled" not in source:
    raise SystemExit("operations recovery capability blocker is not explicit")
if "bounded_error" in source or re.search(r"failure[^\n]*\b(?:response|cleanup)\.text", source):
    raise SystemExit("ClickHouse failure diagnostics still expose a raw response body")
if source.count("http_failure_category(") < 3:
    raise SystemExit("ClickHouse failure categories are not applied at every terminal response site")
print("operations recovery causal metric gate passed")
PY

# Core telemetry diagnostics must retain only the finite allowlisted health
# counters, and browser-stage validation must reject a counter regression.
python3 - "$ROOT/deploy/compose/e2e/core-availability.sh" "$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh" <<'PY'
from pathlib import Path
import sys

core = Path(sys.argv[1]).read_text(encoding="utf-8")
runner = Path(sys.argv[2]).read_text(encoding="utf-8")
required = (
    "telemetry_export_accepted_count",
    "telemetry_export_failure_count",
    "telemetry_export_retry_count",
    "telemetry_export_dropped_count",
    "retained",
    "dropped",
    "rejected",
    "abandoned_batches",
    "export_failures",
)
for name in required:
    if core.count(f'"{name}"') < 2:
        raise SystemExit(f"Core health counter is not captured and validated: {name}")
if "math.isfinite(float(value))" not in core or "current < previous" not in core:
    raise SystemExit("Core telemetry health values are not finite and monotonic checked")
for name in (
    "otelcol_exporter_sent_spans",
    "otelcol_exporter_send_failed_spans",
    "otelcol_exporter_enqueue_failed_spans",
    "otelcol_exporter_sent_metric_points",
    "otelcol_exporter_send_failed_metric_points",
):
    if name not in core:
        raise SystemExit(f"Collector allowlisted queue metric missing: {name}")
if "otelcol_exporter_retry_exhausted_spans" not in core:
    raise SystemExit("Collector retry-exhausted metric is not allowlisted")
for name in ("otelcol_exporter_queue_capacity", "queueUtilization"):
    if name not in core:
        raise SystemExit(f"Collector queue gauge missing: {name}")
if "capture_collector_queue_signal" not in runner:
    raise SystemExit("browser stage does not attempt Collector queue evidence")
print("Core and Collector bounded counter regression passed")
PY

printf '%s\n' 'core availability fail-closed and session-credential safety checks passed'
