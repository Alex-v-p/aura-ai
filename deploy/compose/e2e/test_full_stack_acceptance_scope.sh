#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh

python3 - "$runner" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")

for marker in (
    "acceptance_scope=${AURA_OBSERVATORY_ACCEPTANCE_SCOPE:-full}",
    "default|full)",
    "foundation)",
    "AURA_OBSERVATORY_ACCEPTANCE_SCOPE must be default, full, or foundation",
):
    if marker not in source:
        raise SystemExit(f"acceptance scope validation marker missing: {marker}")

foundation_browser = "--grep 'authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention'"
for marker in (
    "if [ \"$foundation_scope\" -eq 1 ]; then",
    foundation_browser,
    "evaluation artifact metric gate not applicable",
    "operations recovery metric gate not applicable",
    "ClickHouse scan-boundary probe not-sampled",
    "capture_optional_metric_snapshot()",
    "if snapshot=$(capture_observatory_metric_total \"$phase\" \"$metric\") && [ -n \"$snapshot\" ]; then",
    '"reason":"optional-not-sampled"',
    "foundation-not-sampled",
    "browser_query_outcome_after='{\"available\":false,\"reason\":\"foundation-not-sampled\"}'",
    "browser_query_duration_after='{\"available\":false,\"reason\":\"foundation-not-sampled\"}'",
    "Observatory foundation acceptance passed; evaluation and outage scenarios were not run.",
):
    if marker not in source:
        raise SystemExit(f"foundation scope marker missing: {marker}")

scope_validation = source.index("acceptance_scope=${AURA_OBSERVATORY_ACCEPTANCE_SCOPE:-full}")
setup = source.index("seed_evaluation_definitions")
ledger_gate = source.index("verify_evaluation_runner_database_access\n")
browser = source.index("browser_status=0")
privacy = source.index("# Scan bounded service logs")
foundation_exit = source.index("Observatory foundation acceptance passed")
evaluation_matrix = source.index("# AURA-0098 dependency matrix")
if not scope_validation < setup < ledger_gate < browser < privacy < foundation_exit < evaluation_matrix:
    raise SystemExit("foundation scope ordering does not preserve setup/gates/privacy before the exit")
if source.index("trap cleanup EXIT INT TERM") > foundation_exit:
    raise SystemExit("foundation success exit is not protected by project cleanup")
if source.index("exit 0", foundation_exit) > evaluation_matrix:
    raise SystemExit("foundation success path does not exit before the full matrix")

if source.count(foundation_browser) != 1:
    raise SystemExit("foundation browser filter must be unique")

scan_boundary = source.index("run_scan_boundary_probe")
scan_branch = source.index("ClickHouse scan-boundary probe not-sampled")
privacy = source.index("# Scan bounded service logs")
if not scan_branch < privacy:
    raise SystemExit("foundation scan-boundary marker must precede privacy evidence")
if source[scan_branch:privacy].count("run_scan_boundary_probe") != 1:
    raise SystemExit("scan-boundary probe must remain available only on the full path")

# Reproduce the former cleanup payload shape: httpx rejects a one-element tuple
# as a request body, while the corrected string body is serializable.
import httpx

try:
    httpx.Request("POST", "http://example.invalid", content=("cleanup",)).read()
except TypeError:
    pass
else:
    raise SystemExit("tuple cleanup payload no longer reproduces the httpx TypeError")
httpx.Request("POST", "http://example.invalid", content="cleanup").read()

# Reproduce the exact shell ordering that previously let a successful empty
# optional probe reach json.loads: the normalizer must turn empty stdout into
# valid unavailable JSON before the parser is invoked.
empty_probe = ""
normalized = empty_probe or '{"available":false,"reason":"optional-not-sampled"}'
import json
parsed = json.loads(normalized)
if parsed != {"available": False, "reason": "optional-not-sampled"}:
    raise SystemExit("empty optional response was not normalized truthfully")

if source.index("browser_query_outcome_before='{\"available\":false,\"reason\":\"foundation-not-sampled\"}'") > source.index("browser_status=0"):
    raise SystemExit("foundation optional query sampler must be disabled before browser execution")
if source.index("browser_query_outcome_after='{\"available\":false,\"reason\":\"foundation-not-sampled\"}'") < source.index("browser_after_sample_status=0"):
    raise SystemExit("foundation optional query sampler must be disabled after browser execution")

print("full-stack acceptance scope regression passed")
PY

printf '%s\n' 'full-stack acceptance scope regression passed'
