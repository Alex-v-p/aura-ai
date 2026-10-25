#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
runner=$ROOT/deploy/compose/run-observatory-full-stack-e2e.sh
core_probe=$ROOT/deploy/compose/e2e/core-availability.sh

# Keep the composed acceptance honest about the real visibility path. The
# disposable ClickHouse volume is label-checked before removal, readiness
# queries the authenticated observations table, and the Core run is correlated
# to an authenticated Observatory run-detail response before browser work.
grep -Fq 'ensure_fresh_clickhouse_volumes()' "$runner"
grep -Fq 'com.docker.compose.project' "$runner"
grep -Fq 'probe_clickhouse_observations_table post-readiness' "$runner"
grep -Fq 'probe_clickhouse_observations_table pre-browser' "$runner"
grep -Fq 'SELECT count() AS observation_count FROM observations FORMAT JSONEachRow' "$runner"
grep -Fq 'run_core_observatory_visibility_probe' "$runner"
grep -Fq 'assert_visibility_probe_runtime' "$runner"
grep -Fq 'visibility probe production runtimes and imports=verified' "$runner"
grep -Fq 'docker exec -i "$core_api_id" uv run python -' "$runner"
grep -Fq 'docker exec -i -e "AURA_E2E_VISIBILITY_RUN_ID=$run_id" "$observatory_api_id"' "$runner"
grep -Fq '    uv run python - <<' "$runner"
grep -Fq 'for module in ("redis.asyncio", "aura_core.platform.auth")' "$runner"
grep -Fq 'for module in ("redis.asyncio", "httpx", "aura_observatory.auth", "aura_observatory.settings")' "$runner"
grep -Fq 'api/v1/conversations' "$runner"
grep -Fq 'api/v1/runs/{run_id}' "$runner"
grep -Fq 'docker exec -i -e "AURA_E2E_VISIBILITY_RUN_ID=$run_id"' "$runner"
grep -Fq 'Core visibility probe returned no correlation payload' "$runner"
grep -Fq 'telemetry_export_retry_count' "$runner"
grep -Fq 'Observatory ingest counters were unavailable' "$runner"
grep -Fq 'project=%s stage=%s run_id=%s core_export_to_collector_to_observatory=visible' "$runner"
grep -Fq 'observatory_ingest_accepted_delta' "$runner"
grep -Fq 'run_core_observatory_visibility_probe pre-browser' "$runner"
grep -Fq 'browser_export_before=$(capture_core_telemetry_export_stats)' "$runner"
grep -Fq 'browser_worker_export_before=$(capture_core_telemetry_export_stats aura-core-worker' "$runner"
grep -Fq 'browser_collector_before=$(capture_collector_queue_signal' "$runner"
grep -Fq 'browser_query_outcome_before=$(capture_optional_metric_snapshot browser_query_before observatory_query_outcome)' "$runner"
grep -Fq 'browser_query_duration_before=$(capture_optional_metric_snapshot browser_query_before observatory_query_duration_ms_count)' "$runner"
grep -Fq 'browser_export_after=$(capture_core_telemetry_export_stats)' "$runner"
grep -Fq 'browser_worker_export_after=$(capture_core_telemetry_export_stats aura-core-worker)' "$runner"
grep -Fq 'browser_query_outcome_after=$(capture_optional_metric_snapshot browser_query_after observatory_query_outcome)' "$runner"
grep -Fq 'browser_query_duration_after=$(capture_optional_metric_snapshot browser_query_after observatory_query_duration_ms_count)' "$runner"
grep -Fq 'browser_status=0' "$runner"
grep -Fq 'browser_after_sample_status=0' "$runner"
grep -Fq 'capture_browser_target_diagnostics' "$runner"
grep -Fq 'observatory_observation_receipts' "$runner"
grep -Fq 'observatory_ingest_watermark' "$runner"
grep -Fq 'FROM observations WHERE run_id' "$runner"
grep -Fq 'claimedAtMin' "$runner"
grep -Fq 'detailFirst200MonotonicMs' "$runner"
grep -Fq 'queueSize' "$core_probe"
grep -Fq 'coreApiExporterLifecycle' "$runner"
grep -Fq 'coreApiExporterGauges' "$runner"
grep -Fq 'collectorQueueGauges' "$runner"
for field in enqueueFailedSpans sentMetricPoints sendFailedMetricPoints queueCapacity queueUtilization; do
  grep -Fq "$field" "$core_probe"
  grep -Fq "$field" "$runner"
done
for field in kindCounts spanRowCount metricRowCount distinctNonEmptyTraceIds uniqueNonEmptySpanIds topTraceIdCounts expectedTraceIdPresent pendingTargetRange; do
  grep -Fq "$field" "$runner"
done
grep -Fq 'publicBoundary' "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts"
grep -Fq 'renderedState' "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts"
grep -Fq 'identity-unavailable' "$runner"
grep -Fq 'regressed on the same instance' "$runner"
grep -Fq 'containerId' "$core_probe"
grep -Fq 'browser phase failed status=%s; post-browser boundary samples were attempted' "$runner"
grep -Fq 'run_scan_boundary_probe' "$runner"
grep -Fq 'AURA_E2E post-browser ClickHouse scan-boundary fixture cleaned' "$runner"

# The overview snapshot remains metadata-only but must expose the persisted
# acceptance/failure counters used by the visibility probe.
for field in accepted rejected duplicate malformed storageFailure; do
  grep -Fq "\"$field\": counts[\"$field\"]" "$core_probe"
done

grep -Fq 'docker exec -i "$api_id" uv run python - <<' "$core_probe"

# Stdin attachment is required for every Python heredoc used by the new
# visibility path. The correlation parser must be guarded before JSON
# decoding so a detached/empty exec cannot become an opaque parser failure.
python3 - "$runner" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
exec_marker = 'run_payload=$(docker exec -i "$core_api_id" uv run python -'
guard_marker = 'if [ -z "$run_payload" ]; then'
parser_marker = 'run_id=$(printf \'%s\\n\' "$run_payload" | python3 -c'
positions = [source.find(marker) for marker in (exec_marker, guard_marker, parser_marker)]
if any(position < 0 for position in positions) or positions != sorted(positions):
    raise SystemExit("visibility probe must attach stdin and reject empty payload before JSON parsing")
print("visibility heredoc stdin and empty-payload regression passed")
PY

# The new probes must use the same uv-managed runtime as the service entrypoint;
# a hard-coded .venv interpreter is not an accepted substitute.

python3 - "$runner" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
for marker in (
    'docker exec -i "$core_api_id" /app/.venv/bin/python',
    'docker exec -i "$api_id" /app/.venv/bin/python',
):
    if marker in source:
        raise SystemExit("visibility probes still bypass the production uv runtime")
observatory_exec = 'docker exec -i -e "AURA_E2E_VISIBILITY_RUN_ID=$run_id" "$observatory_api_id"'
start = source.find(observatory_exec)
if start < 0 or "/app/.venv/bin/python" in source[start : start + 200]:
    raise SystemExit("visibility Observatory probe bypasses the production uv runtime")
print("visibility production-runtime regression passed")
PY

# The million-row scan-boundary mutation must not run before the browser's
# real Core/Observatory scenario. The correlated probe and bounded stage
# snapshots form a barrier immediately before browser launch; the fixture is
# isolated after that scenario and always self-cleans.
python3 - "$runner" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
browser = source.index('"${compose[@]}" run --rm --no-deps --no-TTY \\\n  -e "AURA_OBSERVATORY_OPERATIONS_FULL_STACK=$operations_full_stack_enabled" frontend-e2e')
pre_probe = source.index('run_core_observatory_visibility_probe pre-browser')
before_snapshot = source.index('browser_export_before=$(capture_core_telemetry_export_stats)')
after_snapshot = source.index('browser_export_after=$(capture_core_telemetry_export_stats)')
scan_function = source.index('run_scan_boundary_probe()')
scan_fixture = source.index('FROM numbers(1000001)', scan_function)
scan_call = source.index('run_scan_boundary_probe', browser)
browser_command = browser - 3
after_export = source.index('if ! browser_export_after=$(capture_core_telemetry_export_stats)', browser)
after_ingest = source.index('if ! browser_ingest_after=$(capture_observatory_ingest_signal)', browser)
failure_report = source.index('browser phase failed status=%s; post-browser boundary samples were attempted', browser)
if not pre_probe < before_snapshot < browser:
    raise SystemExit("fresh correlated probe is not an immediate pre-browser barrier")
if not browser_command < after_export < after_ingest < failure_report:
    raise SystemExit("browser failure does not retain both post-browser boundary samples")
if not after_snapshot < scan_call:
    raise SystemExit("scan-boundary fixture is not isolated after browser stage")
if not scan_function < scan_fixture:
    raise SystemExit("scan-boundary fixture is not owned by its isolated phase")
print("full-stack observability ordering and browser barrier regression passed")
PY

# The browser-created UUID must be handed off through the disposable Node-side
# result file and used for diagnostics. A pre-browser probe UUID is a barrier,
# never a fallback target, and the Collector baseline must precede Playwright.
python3 - "$runner" "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts" <<'PY'
from pathlib import Path
import re
import sys

runner = Path(sys.argv[1]).read_text(encoding="utf-8")
frontend = Path(sys.argv[2]).read_text(encoding="utf-8")
browser = runner.index('"${compose[@]}" run --rm --no-deps --no-TTY \\\n  -e "AURA_OBSERVATORY_OPERATIONS_FULL_STACK=$operations_full_stack_enabled" frontend-e2e')
collector_baseline = runner.index('browser_collector_before=$(capture_collector_queue_signal')
query_before = runner.index('browser_query_outcome_before=$(capture_optional_metric_snapshot', collector_baseline)
worker_before = runner.index('browser_worker_export_before=$(capture_core_telemetry_export_stats aura-core-worker')
worker_after = runner.index('browser_worker_export_after=$(capture_core_telemetry_export_stats aura-core-worker)')
query_after = runner.index('browser_query_outcome_after=$(capture_optional_metric_snapshot', browser)
target_read = runner.index('if read_browser_target; then', browser)
target_diag = runner.index('capture_browser_target_diagnostics "$browser_target_run_id"', browser)
failure = runner.index('browser phase failed status=%s; post-browser boundary samples were attempted', browser)
if not collector_baseline < query_before < browser or not worker_before < browser:
    raise SystemExit('Collector/Core worker baselines must precede browser launch')
if not browser < worker_after < query_after < target_read < target_diag < failure:
    raise SystemExit('browser target/worker after-samples are not ordered before failure diagnostics')
if 'last_visibility_run_id' in runner:
    raise SystemExit('stale last_visibility_run_id must not correlate browser diagnostics')
for marker in ('AURA_E2E_BROWSER_TARGET_FILE', 'recordBrowserTarget', 'writeFile'):
    if marker not in frontend:
        raise SystemExit(f'browser target channel marker missing: {marker}')
if frontend.index('recordBrowserTarget') > frontend.index('const latestUserMessage'):
    raise SystemExit('browser target must be recorded before the Core run UI wait')
contract = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
for valid in (
    '74a7df65-5e5b-350c-d997-ea484cbd1d9c',
    '00000000-0000-ffff-0000-ffffffffffff',
):
    if contract.fullmatch(valid) is None:
        raise SystemExit('contract-valid arbitrary UUID nibbles were rejected')
for invalid in (
    '74a7df65-5e5b-350C-d997-ea484cbd1d9c',
    '../74a7df65-5e5b-350c-d997-ea484cbd1d9c',
    '74a7df65-5e5b-350c-d997-ea484cbd1d9',
    'not-a-run-id',
):
    if contract.fullmatch(invalid) is not None:
        raise SystemExit('malformed or path-like browser target was accepted')
if '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' not in frontend:
    raise SystemExit('frontend browser target validator does not use the closed contract pattern')
if 're.fullmatch(contract_pattern' not in runner:
    raise SystemExit('shell browser target validator does not use the closed contract pattern')
print('browser target UUID and baseline ordering regression passed')
PY

# Analytics acceptance must observe the public rendered workflow exactly once:
# listeners are attached before activation, the existing Refresh control is
# the only explicit retry, and diagnostics contain bounded metadata rather
# than query bodies or rendered private content.
python3 - "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding='utf-8')
primary = source.index("test('authenticates owner, creates a real Core run")
analytics_start = source.index('The default bounded 24-hour analytics query', primary)
analytics_end = source.index('const comparisonRun =', analytics_start)
analytics = source[analytics_start:analytics_end]
if "type AnalyticsRequestKind = 'catalogue' | 'query'" not in source:
    raise SystemExit('analytics request kind contract missing')
for marker in (
    'analyticsRequestDiagnostics',
    'transportFailureCategory',
    'parserState',
    'partialReasons',
    'executionLimitMs',
    'seriesCount',
    'breakdownCount',
    'freshnessState',
    'captureAnalyticsRenderedDiagnostics',
    'queryResponsesBeforeRefresh',
    'toBeGreaterThan(queryResponsesBeforeRefresh)',
    'recordPendingAnalyticsRequests',
    'endedAtMs: null',
    "requestfailed', recordAnalyticsFailure",
    "getByRole('button', { name: 'Refresh metrics', exact: true })",
    'timeout: 30_000',
):
    if marker not in analytics:
        raise SystemExit(f'analytics diagnostic marker missing: {marker}')
if analytics.index("page.on('request', recordAnalyticsRequest)") > analytics.index("button', { name: 'Analytics'"):
    raise SystemExit('analytics listeners must precede activation')
if analytics.count('refreshMetrics.click') != 1:
    raise SystemExit('analytics acceptance must perform exactly one explicit Refresh click')
if not analytics.index("Metric explorer") < analytics.index('refreshMetrics.click') < analytics.index('rows scanned'):
    raise SystemExit('analytics activation, refresh, and readiness are out of order')
if 'no run_id filter' not in analytics or 'target-correlated' in analytics:
    raise SystemExit('analytics claim must remain global and non-target-correlated')
for forbidden in ('postData()', 'response.text()', 'response.body()', 'request.postData()'):
    if forbidden in analytics:
        raise SystemExit(f'analytics diagnostics expose raw request/response data: {forbidden}')

def transport(status: int, error: str = '') -> str:
    if status >= 500:
        return 'http_5xx'
    if status >= 400:
        return 'http_4xx'
    lowered = error.lower()
    if 'timeout' in lowered:
        return 'timeout'
    if any(value in lowered for value in ('network', 'disconnect', 'failed')):
        return 'network'
    return 'unknown' if status == 0 else 'none'

def outcome(responses: list[dict[str, object]], rendered: str, timed_out: bool = False) -> bool:
    if timed_out:
        return False
    latest = next((item for item in reversed(responses) if item['kind'] == 'query'), None)
    return rendered == 'ready' and bool(latest and latest.get('parserState') == 'parsed')

if not outcome(
    [
        {'kind': 'query', 'status': 503, 'parserState': None},
        {'kind': 'query', 'status': 200, 'parserState': 'parsed'},
    ],
    'ready',
):
    raise SystemExit('initial analytics failure plus explicit successful retry did not recover')
if outcome([], 'loading', timed_out=True):
    raise SystemExit('pending analytics timeout was not fail-closed')
if transport(503) != 'http_5xx' or transport(0, 'network disconnected') != 'network':
    raise SystemExit('analytics transport failure categories are incorrect')
if outcome([{'kind': 'query', 'status': 200, 'parserState': 'mismatch'}], 'ready'):
    raise SystemExit('200 parser/state mismatch was accepted as ready')
bounded = {
    'scannedRows': min(2_000_000_000, 9_000_000_000),
    'seriesCount': min(10_000, 20_000),
    'partialReasons': ['bounded.reason'] * 16,
}
if bounded['scannedRows'] != 2_000_000_000 or bounded['seriesCount'] != 10_000 or len(bounded['partialReasons']) != 16:
    raise SystemExit('analytics metadata bounds are not enforced')
if any(key in {'prompt', 'response', 'credential', 'bearer'} for key in bounded):
    raise SystemExit('analytics diagnostic metadata contains prohibited content keys')
print('analytics refresh, state, failure, bounds, and privacy regression passed')
PY

# Trace-boundary diagnostics are metadata-only and ordered after the public
# run/trace response and rendered-state observations. ClickHouse diagnostics
# are bounded aggregates; no payload columns or unbounded trace lists may leak.
python3 - "$runner" "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts" <<'PY'
from pathlib import Path
import sys

runner = Path(sys.argv[1]).read_text(encoding='utf-8')
frontend = Path(sys.argv[2]).read_text(encoding='utf-8')
for marker in (
    'getRunStatus',
    'getRunTraceId',
    'getTraceStatus',
    'getTraceRunIdMatch',
    'getTraceSpanCount',
    'getTracePartial',
    'selectedRunIdMatch',
    'selectedRunState',
    'detailPresent',
    'detailTraceIdPresent',
    'tracePresent',
    'traceSpanCount',
    'errorCategory',
    'boundedHttpStatus',
):
    if marker not in frontend:
        raise SystemExit(f'bounded browser trace marker missing: {marker}')
for marker in (
    'kindCounts',
    'spanRowCount',
    'metricRowCount',
    'distinctNonEmptyTraceIds',
    'uniqueNonEmptySpanIds',
    'topTraceIdCounts',
    'expectedTraceIdPresent',
    'pendingTargetRange',
    'LIMIT 8 FORMAT JSONEachRow',
    'LIMIT 16 FORMAT JSONEachRow',
):
    if marker not in runner:
        raise SystemExit(f'bounded trace aggregate marker missing: {marker}')
diagnostic_start = runner.index('capture_browser_target_diagnostics()')
diagnostic_end = runner.index('run_scan_boundary_probe()', diagnostic_start)
diagnostic = runner[diagnostic_start:diagnostic_end]
for forbidden in (
    'SELECT *',
    'measurement_value',
    'dimensions',
    'prompt',
    'response_body',
):
    if forbidden in diagnostic:
        raise SystemExit(f'trace diagnostic contains prohibited payload field: {forbidden}')
if not (
    diagnostic.index('FROM observations WHERE run_id')
    < diagnostic.index('kindCounts')
    < diagnostic.index('pendingTargetRange')
):
    raise SystemExit('trace aggregate and pending-sequence diagnostics are out of order')
print('trace boundary privacy, bounds, and ordering regression passed')
PY

# Target ClickHouse diagnostics must keep every bounded request inside a live
# AsyncClient, cap server execution below the client deadline, and preserve a
# stage-specific metadata outcome instead of collapsing all failures.
python3 - "$runner" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding='utf-8')
start = source.index('capture_browser_target_diagnostics()')
end = source.index('run_scan_boundary_probe()', start)
diagnostic = source[start:end]
for marker in (
    'async with httpx.AsyncClient(timeout=5.0) as client:',
    'max_execution_time',
    'aggregate_meta, aggregate_row = await run_stage(client',
    'kind_meta, kind_counts_value = await run_stage(client',
    'trace_meta, trace_counts_value = await run_stage(client',
    '"stages": {',
    'f"{stage}-timeout"',
    'f"{stage}-status"',
    'f"{stage}-empty"',
    'f"{stage}-shape"',
    'responseBytes',
):
    if marker not in diagnostic:
        raise SystemExit(f'ClickHouse diagnostic marker missing: {marker}')
client_start = diagnostic.index('async with httpx.AsyncClient(timeout=5.0) as client:')
client_close = diagnostic.index('            if isinstance(row_ids, list):', client_start)
for marker in (
    'aggregate_meta, aggregate_row = await run_stage(client',
    'kind_meta, kind_counts_value = await run_stage(client',
    'trace_meta, trace_counts_value = await run_stage(client',
    'expected_meta, expected_trace_count = await run_stage(',
    'rows_meta, row_ids = await run_stage(',
):
    position = diagnostic.index(marker, client_start)
    if position > client_close:
        raise SystemExit(f'ClickHouse request escaped the live client context: {marker}')
if diagnostic.count('async with httpx.AsyncClient(timeout=5.0) as client:') != 1:
    raise SystemExit('ClickHouse target diagnostics opened multiple primary client contexts')
if diagnostic[client_start:client_close].count('run_stage(client') < 3:
    raise SystemExit('ClickHouse target diagnostics did not issue all primary requests through the live client')

def classify(stage: str, *, timeout: bool = False, status: int = 200, lines: list[str] | None = None, shape: bool = True) -> str:
    if timeout:
        return f'{stage}-timeout'
    if status < 200 or status >= 300:
        return f'{stage}-status'
    if not lines:
        return f'{stage}-empty'
    return f'{stage}-ok' if shape else f'{stage}-shape'

successful = [classify(stage, lines=['{}']) for stage in ('aggregate', 'kind', 'trace')]
if successful != ['aggregate-ok', 'kind-ok', 'trace-ok']:
    raise SystemExit('all three ClickHouse stage requests did not classify as successful')
for stage in ('aggregate', 'kind', 'trace'):
    if classify(stage, timeout=True) != f'{stage}-timeout':
        raise SystemExit(f'{stage} timeout was not classified')
    if classify(stage, status=503) != f'{stage}-status':
        raise SystemExit(f'{stage} non-2xx status was not classified')
    if classify(stage, lines=[]) != f'{stage}-empty':
        raise SystemExit(f'{stage} empty response was not classified')
    if classify(stage, lines=['malformed'], shape=False) != f'{stage}-shape':
        raise SystemExit(f'{stage} malformed response was not classified')
if any(value in diagnostic for value in ('response.text.splitlines()', 'response.content.decode')) and 'responseBytes' not in diagnostic:
    raise SystemExit('ClickHouse diagnostics parse response content without bounded byte metadata')
if 'SELECT *' in diagnostic or 'response_body' in diagnostic or 'prompt' in diagnostic:
    raise SystemExit('ClickHouse diagnostics contain prohibited payload access')
print('ClickHouse target client lifecycle, timeout, outcome, and privacy regression passed')
PY

# The primary serial scenario has a finite local budget that covers its
# retained Core, Observatory visibility, trace, analytics, and retention
# windows. Individual phase limits remain explicit and unchanged.
python3 - "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1]).read_text(encoding='utf-8')
match = re.search(r'test\.setTimeout\(([0-9_]+)\)', source)
if match is None:
    raise SystemExit('primary scenario is missing a bounded local timeout')
timeout_ms = int(match.group(1).replace('_', ''))
retained_budget_ms = (
    3 * (90_000 + 120_000)  # three real Core runs
    + 60_000 + 15_000 + 75_000  # visibility reload and trace
    + 3 * 60_000  # retention terminal windows
    + 30_000 + 30_000 + 30_000  # analytics/REST bounded probes
)
if timeout_ms <= retained_budget_ms:
    raise SystemExit('primary scenario timeout does not cover summed phase budgets')
for marker in (
    'timeout: 90_000',
    'timeout: 120_000',
    'timeout: 60_000',
    'timeout: 75_000',
    'timeline.visibilityWaitStartedAtMs',
    'timeline.finalRestProbeDurationMs',
    'timeline.coreTerminalDurationMs',
    'page.setDefaultTimeout(15_000)',
    'corePage.setDefaultTimeout(15_000)',
    'runSelectionDurationMs',
    'traceNavigationDurationMs',
    '.click({\n        timeout: 15_000,',
):
    if marker not in source:
        raise SystemExit(f'phase/timeline marker missing: {marker}')
primary = source.index("test('authenticates owner, creates a real Core run")
admission = source.index('createCoreRun(corePage, core, prompt, timeline)', primary)
terminal = source.index('waitForCoreRun(corePage, core, accepted, timeline)', admission)
visibility = source.index('timeline.visibilityWaitStartedAtMs', terminal)
final_probe = source.index('timeline.finalRestProbeDurationMs', visibility)
failure = source.index('timeline=${JSON.stringify(boundedTimeline(timeline))}', final_probe)
if not admission < terminal < visibility < final_probe < failure:
    raise SystemExit('timeline measurements are not ordered around the visibility failure path')
for marker in (
    'async function ensureRunVisibleWithRecovery(',
    'async function captureRunVisibilityFailureDiagnostics(',
    "result: 'recovered'",
    'visibilityReloadDurationMs',
    'const comparisonVisibility = await ensureRunVisibleWithRecovery',
    'const purgeVisibility = await ensureRunVisibleWithRecovery',
    'Comparison Core run did not render after bounded reload recovery',
    'Purge Core run did not render after bounded reload recovery',
    'visibleRunButtonCount',
    'connectionStatus',
):
    if marker not in source:
        raise SystemExit(f'visibility recovery marker missing: {marker}')

helper = source.index('async function ensureRunVisibleWithRecovery(')
reload = source.index("await page.reload({ waitUntil: 'domcontentloaded' })", helper)
reentry = source.index("await page.getByRole('button', { name: 'Runs', exact: true }).click", reload)
recovered = source.index("result: 'recovered'", reload)
failed = source.index("result: 'failed'", recovered)
comparison = source.index('const comparisonVisibility = await ensureRunVisibleWithRecovery', primary)
purge = source.index('const purgeVisibility = await ensureRunVisibleWithRecovery', comparison)
if not helper < reload < reentry < recovered < failed < comparison < purge:
    raise SystemExit('shared visibility recovery is not ordered across real Core runs')
if 'timeout: 120_000' in source[comparison:purge]:
    raise SystemExit('comparison run retained an unbounded visibility wait')
def visibility_path(initial_visible: bool, reload_visible: bool) -> tuple[str, bool]:
    if initial_visible:
        return 'rendered', True
    if reload_visible:
        return 'recovered', True
    return 'failed', False
if visibility_path(False, True) != ('recovered', True):
    raise SystemExit('stale initial visibility plus successful reload did not continue')
if visibility_path(False, False) != ('failed', False):
    raise SystemExit('failed reload was not kept on the diagnostic failure path')
if visibility_path(False, True) != ('recovered', True):
    raise SystemExit('second-run stale initial visibility plus successful reload did not continue')
print('primary scenario timeout and timeline ordering regression passed')
PY

# Evaluation has two independent 120-second terminal polls. Its aggregate
# budget must cover both polls plus the bounded owner/setup phase, while the
# individual poll and locator budgets remain unchanged. Checkpoint collection
# must also survive a Playwright page/context close so it cannot replace the
# original timeout with a diagnostic failure.
python3 - "$ROOT/frontend/apps/observatory-web-e2e/src/full-stack.spec.ts" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding='utf-8')
evaluation = source.index("test('drives the public evaluation round trip")
next_scenario = source.index("test('exercises seeded owner review", evaluation)
scenario = source[evaluation:next_scenario]
if 'test.setTimeout(300_000);' not in scenario:
    raise SystemExit('evaluation scenario is missing its aggregate timeout')
if 'two independent 120,000 ms terminal polls' not in scenario:
    raise SystemExit('evaluation aggregate timeout rationale is missing')
if scenario.count('timeout: 120_000') < 2:
    raise SystemExit('evaluation terminal polls lost their individual 120-second budgets')
for marker in (
    "click({ timeout: 15_000 })",
    "selectOption(name, { timeout: 15_000 })",
    "new Promise<void>((resolve) => setTimeout(resolve, 1_000))",
    "'signInCompletedMs'",
    "'preflightCollectionsCompletedMs'",
    "'pageLoadSseCompletedMs'",
    "'refreshCompletedMs'",
    "'datasetRenderedMs'",
    "'inspectCasesClickedMs'",
    "'suiteTabActionCompletedMs'",
    "boundedEvaluationMilestones",
    'authSession',
    'shellAuthState',
    'sessionRequired',
    'workspaceHasData',
    'suitesPage',
):
    if marker not in scenario and marker not in source:
        raise SystemExit(f'evaluation diagnostic-boundary marker missing: {marker}')
for marker in (
    "captureStatus: 'captured'",
    "captureStatus: 'page-closed'",
    "unavailableEvaluationSuiteCheckpoint",
    'checkpointCaptureStatus',
    "writeBoundedBrowserArtifact('suite-selection-diagnostic.json'",
):
    if marker not in source:
        raise SystemExit(f'page-close-safe suite checkpoint marker missing: {marker}')
checkpoint = source.index('async function collectEvaluationSuiteCheckpoint(')
selection = source.index('const selectTab =', evaluation)
checkpoint_call = source.index('collectEvaluationSuiteCheckpoint(page, workspace, suiteId)', selection)
artifact = source.index("writeBoundedBrowserArtifact('suite-selection-diagnostic.json'", selection)
failure_throw = source.index('throw cause;', selection)
if not checkpoint < selection < checkpoint_call < artifact < failure_throw:
    raise SystemExit('suite checkpoint is not persisted before the bounded failure is rethrown')
catch_block = source.rfind('} catch (cause) {', selection, checkpoint_call)
checkpoint_in_catch = source.index('const checkpoint = await collectEvaluationSuiteCheckpoint', catch_block)
drain_in_catch = source.index('await drainEvaluationUiDiagnostics()', catch_block)
if not checkpoint_in_catch < drain_in_catch:
    raise SystemExit('suite checkpoint must be captured before diagnostic draining')
if 'throw cause;' not in source[catch_block : failure_throw + len('throw cause;')]:
    raise SystemExit('suite-selection catch must rethrow the original failure')
print('evaluation aggregate timeout and page-close-safe checkpoint regression passed')
PY

# Exporter counters are monotonic only within one process/container instance;
# retained and Collector queue size are drainable gauges. A replacement emits
# a reset marker and no delta; a lower cumulative counter on the same identity
# remains a hard failure rather than being silently accepted.
python3 - "$runner" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding='utf-8')
for marker in (
    'state": "reset"',
    'state": "identity-unavailable"',
    'exporter counters regressed on the same instance',
    'coreApiExporterLifecycle',
    'coreApiExporterGauges',
    'collectorQueueGauges',
    'beforeContainerId',
    'afterContainerId',
):
    if marker not in source:
        raise SystemExit(f'exporter lifecycle marker missing: {marker}')

def transition(
    before: dict[str, object], after: dict[str, object]
) -> tuple[str, dict[str, float], dict[str, dict[str, float]]]:
    identity_before = (before.get('containerId'), before.get('startedAt'), before.get('pid'))
    identity_after = (after.get('containerId'), after.get('startedAt'), after.get('pid'))
    if identity_before != identity_after:
        return 'reset', {}, {}
    deltas: dict[str, float] = {}
    for name in ('dropped', 'rejected'):
        if float(after[name]) < float(before[name]):
            raise ValueError('same-instance regression')
        deltas[name] = float(after[name]) - float(before[name])
    gauges = {
        'retained': {
            'before': float(before['retained']),
            'after': float(after['retained']),
        }
    }
    if any(value < 0 for sample in gauges.values() for value in sample.values()):
        raise ValueError('unbounded gauge')
    return 'monotonic', deltas, gauges

baseline = {
    'containerId': 'api-before',
    'startedAt': 't0',
    'pid': 10,
    'retained': 8,
    'dropped': 2,
    'rejected': 3,
}
replacement = {**baseline, 'containerId': 'api-after', 'retained': 1, 'dropped': 0}
state, deltas, gauges = transition(baseline, replacement)
if state != 'reset' or deltas:
    raise SystemExit('container replacement incorrectly produced a counter delta')
state, deltas, gauges = transition(baseline, {**baseline, 'retained': 1})
if state != 'monotonic' or deltas != {'dropped': 0.0, 'rejected': 0.0}:
    raise SystemExit('same-instance gauge drain was not accepted without a gauge delta')
if gauges['retained'] != {'before': 8.0, 'after': 1.0}:
    raise SystemExit('gauge samples were not reported as before/after values')
collector_gauges = {
    'queueSize': {'before': 8.0, 'after': 1.0},
    'queueCapacity': {'before': 16.0, 'after': 16.0},
    'queueUtilization': {'before': 0.5, 'after': 0.0625},
}
if any(
    value < 0 or (name == 'queueUtilization' and value > 1)
    for name, sample in collector_gauges.items()
    for value in sample.values()
):
    raise SystemExit('Collector gauge bounds were not enforced')
if collector_gauges['queueSize']['after'] >= collector_gauges['queueSize']['before']:
    raise SystemExit('Collector queue drain was not preserved as a gauge sample')
try:
    transition(baseline, {**baseline, 'dropped': 1})
except ValueError:
    pass
else:
    raise SystemExit('same-instance non-monotonic cumulative counters were accepted')
print('Core exporter reset, counter, and gauge regression passed')
PY

# Inject a bounded browser failure into the same conditional status pattern:
# both post-browser samples must still run, and the original nonzero status
# must remain the process result.
if probe_output=$(bash -c '
set -Eeuo pipefail
browser_status=0
if false; then
  :
else
  browser_status=$?
fi
after_samples=0
after_export=$(printf "%s" "core-export-sampled")
after_ingest=$(printf "%s" "collector-ingest-sampled")
printf "%s\\n" "post-browser-boundary-samples-attempted"
printf "%s\\n" "post-browser-core-export=$after_export"
printf "%s\\n" "post-browser-collector-ingest=$after_ingest"
if [ "$browser_status" -ne 0 ]; then
  printf "browser-status=%s\\n" 17
  exit 17
fi
'); then
  probe_status=0
else
  probe_status=$?
fi
[ "$probe_status" -eq 17 ]
grep -Fqx 'post-browser-boundary-samples-attempted' <<<"$probe_output"
grep -Fqx 'post-browser-core-export=core-export-sampled' <<<"$probe_output"
grep -Fqx 'post-browser-collector-ingest=collector-ingest-sampled' <<<"$probe_output"
grep -Fqx 'browser-status=17' <<<"$probe_output"
printf '%s\n' 'browser failure post-sample/status propagation regression passed'

printf '%s\n' 'full-stack observability path regression passed'
