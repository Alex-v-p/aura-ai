#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-evaluation-orchestration-e2e.sh

# Keep this regression text-only and deterministic: the disposable runner must
# render the judge bootstrap against fixture-backed Garage state/config rather
# than the repository's operator secret directory.
grep -Fqx 'write_secret observatory-judge-garage-access-key "$(random_access_key)"' "$runner"
grep -Fqx 'write_secret observatory-judge-garage-secret-key "$(random_secret)"' "$runner"
grep -Fqx 'AURA_OBSERVATORY_JUDGE_OBJECT_ACCESS_KEY_FILE=$secret_root/observatory-judge-garage-access-key' "$runner"
grep -Fqx 'AURA_OBSERVATORY_JUDGE_OBJECT_SECRET_KEY_FILE=$secret_root/observatory-judge-garage-secret-key' "$runner"
grep -Fq '  observatory-judge-garage-bootstrap:' "$runner"
grep -Fq '      - e2e_garage_data:/var/lib/garage:ro' "$runner"
grep -Fq '      - e2e_garage_config:/run/garage-config:ro' "$runner"
grep -Fq 'ORDER BY observed_at DESC LIMIT 4096' "$runner"
test "$(sed -n '/collector_recovery_deadline=/,$p' "$runner" | grep -Fc 'run_probe trace_verify')" -eq 1

# Scaling an already-started scheduler must not re-run its one-shot migration
# dependency. The initial Compose up remains the mandatory migration barrier;
# this later operation requires an explicit completed-migration/health check
# and uses Compose's public --no-deps scale behavior.
grep -Fq 'assert_scheduler_scale_prerequisites' "$runner"
grep -Fq 'up --no-deps --detach --scale observatory-evaluation-scheduler=2' "$runner"
grep -Fq 'ps -aq observatory-migrate' "$runner"
grep -Fq 'wait_healthy observatory-postgres observatory-evaluation-scheduler' "$runner"
test "$(grep -Fc 'up --detach --scale observatory-evaluation-scheduler=2' "$runner")" -eq 0

printf '%s\n' 'evaluation orchestration judge bootstrap override regression passed'
