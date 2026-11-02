#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh

# The owner-authorized admission runs in a one-off observatory-api container.
# It must reach the running API through the Compose service boundary, while
# retaining the public Origin, cookie, CSRF, and route checks.
python3 - "$runner" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
probe = text.split("admit_public_capture_authorization() {", 1)[1]
probe = probe.split("\n}\n\nseed_evaluation_definitions()", 1)[0]
expected = 'base_url="http://observatory-api:8000"'
if expected not in probe:
    raise SystemExit("capture authorization probe does not use the Compose API boundary")
if 'base_url="http://127.0.0.1:8000"' in probe:
    raise SystemExit("capture authorization probe targets its one-off container loopback")
for required in (
    '"Origin": settings.public_origin',
    '"X-CSRF-Token": session.csrf_token',
    'settings.effective_session_cookie_name: session_id',
    '"/api/v1/evaluation/capture-authorizations"',
    '"/api/v1/evaluation/artifacts"',
    '"/api/v1/evaluation/evaluators"',
):
    if required not in probe:
        raise SystemExit(f"capture authorization probe lost required public boundary field: {required}")
print("full-stack capture boundary regression passed")
PY

printf '%s\n' 'full-stack capture boundary regression passed'
