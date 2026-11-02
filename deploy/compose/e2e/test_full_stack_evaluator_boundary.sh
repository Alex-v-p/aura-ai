#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
spec=$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh
categories=$ROOT/deploy/compose/e2e/diagnostic-categories.sh

# The evaluator action follows the suite action in the composed browser flow.
# Keep this regression source-only: it proves that a stalled evaluator cannot
# consume the aggregate test budget without leaving bounded diagnostics.
python3 - "$spec" "$runner" "$categories" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
runner = Path(sys.argv[2]).read_text(encoding="utf-8")
category_source = Path(sys.argv[3]).read_text(encoding="utf-8")
if "const evaluationActionTimeoutMs = 15_000;" not in source:
    raise SystemExit("evaluation action timeout budget is not declared")

evaluator_start = source.index("await selectTab('evaluators', 'Evaluator bindings')")
evaluator_end = source.index("await selectTab('jobs', 'Jobs and progress')", evaluator_start)
evaluator = source[evaluator_start:evaluator_end]
for marker in (
    "evaluatorTabActionCompletedMs",
    "evaluatorSelectionCompletedMs",
    "collectEvaluationActionCheckpoint",
    "evaluator-selection-diagnostic.json",
    "evaluatorEndpointStatuses",
    "await drainEvaluationUiDiagnostics()",
    "page.off('response', recordEvaluationUiResponse)",
):
    if marker not in evaluator:
        raise SystemExit(f"evaluator boundary diagnostic marker missing: {marker}")

if not re.search(
    r"getByRole\('button', \{ name: 'Use this version', exact: true \}\)\s*\.click\(\{ timeout: evaluationActionTimeoutMs \}\)",
    evaluator,
):
    raise SystemExit("evaluator selection is not locally bounded")

post_evaluator_end = source.index("let terminalJob:", evaluator_end)
post_evaluator = source[evaluator_end:post_evaluator_end]
for marker in (
    "jobsTabActionCompletedMs",
    "fill(value, { timeout: evaluationActionTimeoutMs })",
    "toBeEnabled({ timeout: evaluationActionTimeoutMs })",
    "bindMetadata.click({ timeout: evaluationActionTimeoutMs })",
    "metadataBoundMs",
    "submissionClickedMs",
):
    if marker not in post_evaluator:
        raise SystemExit(f"critical evaluation action is not locally bounded: {marker}")
if not re.search(
    r"getByRole\('button', \{ name: 'Submit evaluation' \}\)[\s\S]{0,160}\.click\(\{ timeout: evaluationActionTimeoutMs \}\)",
    post_evaluator,
):
    raise SystemExit("evaluation submission is not locally bounded")

terminal_end = source.index("let terminalResult:", post_evaluator_end)
terminal = source[post_evaluator_end:terminal_end]
for marker in (
    "evaluationJobGetDiagnostics",
    "evaluation-terminal-diagnostic.json",
    "writeTerminalPollDiagnostic",
    "terminal-job-poll-timeout",
    "pollTimedOut",
    "lastJob: boundedEvaluationTerminalMetadata(terminalJob)",
    "jobGetAttempts",
    "/attempts?page_size=50",
    "/results?page_size=50&job_id=",
    "terminalJob: boundedEvaluationTerminalMetadata(terminalJob)",
):
    if marker not in terminal:
        raise SystemExit(f"terminal evaluation diagnostic marker missing: {marker}")
for marker in ("failureCode", "workerId", "schedulerId", "createdAt", "updatedAt"):
    if marker not in source:
        raise SystemExit(f"terminal metadata field missing: {marker}")

for marker in (
    "capture_browser_evaluation_service_diagnostics()",
    "evaluation-service-diagnostic.json",
    "capturePolicy",
    "observatory-evaluation-worker",
    "observatory-evaluation-scheduler",
    "aura-evaluation-runner",
    "classify_diagnostic_stream",
):
    if marker not in runner:
        raise SystemExit(f"service failure diagnostic marker missing: {marker}")

for marker in (
    "verify_evaluation_runner_database_access()",
    "evaluation-runner database identity read/write gate failed closed",
    "evaluation-runner database identity read/write gate passed",
    "verify_evaluation_runner_database_access\n",
    "SELECT 1 FROM evaluation_runner_attempts LIMIT 1",
    "INSERT INTO evaluation_runner_terminal_outbox",
    "INSERT INTO evaluation_runner_cancellations",
    "await transaction.rollback()",
):
    if marker not in runner:
        raise SystemExit(f"runner database identity gate marker missing: {marker}")

if runner.index("verify_evaluation_runner_database_access\n") < runner.index('"${compose[@]}" build frontend-e2e'):
    raise SystemExit("runner database identity gate must execute after the frontend image build")
if runner.index("verify_evaluation_runner_database_access\n") > runner.index('browser_status=0'):
    raise SystemExit("runner database identity gate must execute before the browser phase")

if "AURA_EVALUATION_RUNTIME_FIXTURE_REVISION=synthetic-run-coordinator-v1" not in runner:
    raise SystemExit("runner fixture revision is not adapter-owned")
if "AURA_EVALUATION_RUNTIME_FIXTURE_REVISION: synthetic-run-coordinator-v1" not in runner:
    raise SystemExit("container fixture revision is not adapter-owned")
if "AURA_TELEMETRY_DEPLOYMENT_ID=aura0098-e2e" not in runner:
    raise SystemExit("runner deployment revision is not aligned")
if "AURA_TELEMETRY_DEPLOYMENT_ID: aura0098-e2e" not in runner:
    raise SystemExit("container deployment revision is not aligned")
if '"codeRevision":"0000000"' not in runner or '"codeRevision": "0000000"' not in runner:
    raise SystemExit("seeded component code revision is not aligned")
if 'AURA_TELEMETRY_GIT_COMMIT: "0000000"' not in runner:
    raise SystemExit("Compose git revision must remain a quoted string")
if "AURA_TELEMETRY_CONFIGURATION_VERSION=aura0098" not in runner:
    raise SystemExit("runner configuration revision is not aligned")
if "AURA_TELEMETRY_CONFIGURATION_VERSION: aura0098" not in runner:
    raise SystemExit("container configuration revision is not aligned")
if "AURA_EVALUATION_RUNTIME_PROMPT_REVISION=aura0098-AURA0090_PRIVACY_SENTINEL" not in runner:
    raise SystemExit("runner prompt revision is not aligned")
if "AURA_EVALUATION_RUNTIME_POLICY_REVISION=aura0098-policy" not in runner:
    raise SystemExit("runner policy revision is not aligned")
if "AURA_EVALUATION_RUNTIME_PROMPT_REVISION: aura0098-AURA0090_PRIVACY_SENTINEL" not in runner:
    raise SystemExit("container prompt revision is not aligned")
if "AURA_EVALUATION_RUNTIME_POLICY_REVISION: aura0098-policy" not in runner:
    raise SystemExit("container policy revision is not aligned")
for marker in (
    "AURA_EVALUATION_RUNTIME_IMAGE_DIGEST=0000000000000000000000000000000000000000000000000000000000000000",
    "AURA_EVALUATION_RUNTIME_MODEL_ID=fixture-chat-2",
    "AURA_EVALUATION_RUNTIME_MODEL_VERSION=1.0.0",
    "AURA_EVALUATION_RUNTIME_MODEL_DIGEST=0000000000000000000000000000000000000000000000000000000000000000",
    "AURA_EVALUATION_RUNTIME_SEED=98",
    "AURA_EVALUATION_RUNTIME_IMAGE_DIGEST: 0000000000000000000000000000000000000000000000000000000000000000",
    "AURA_EVALUATION_RUNTIME_MODEL_ID: fixture-chat-2",
    "AURA_EVALUATION_RUNTIME_MODEL_VERSION: 1.0.0",
    "AURA_EVALUATION_RUNTIME_MODEL_DIGEST: 0000000000000000000000000000000000000000000000000000000000000000",
    'AURA_EVALUATION_RUNTIME_SEED: "98"',
):
    if marker not in runner:
        raise SystemExit(f"runtime evidence marker is not aligned: {marker}")
if "['Evaluation fixture revision', 'synthetic-run-coordinator-v1']" not in source:
    raise SystemExit("browser fixture revision is not adapter-owned")
if source.count("synthetic-run-coordinator-v1") != 1:
    raise SystemExit("browser fixture revision appears outside the submitted metadata field")
if "aura-evaluation-runner" not in runner:
    raise SystemExit("bounded runner diagnostic is missing")
for marker in ("resource.limit.exceeded", "request_invalid", "terminal_settlement_failure"):
    if marker not in category_source:
        raise SystemExit(f"bounded runner diagnostic signal is missing: {marker}")

if source.count("timeout: 120_000, intervals: [500, 1_000, 2_000, 5_000]") != 2:
    raise SystemExit("terminal evaluation poll timeouts changed unexpectedly")
if "test.setTimeout(300_000);" not in source:
    raise SystemExit("aggregate evaluation test timeout changed unexpectedly")

print("full-stack evaluator action boundary regression passed")
PY
