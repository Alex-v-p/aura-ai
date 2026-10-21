import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const observedAt = '2026-10-21T09:00:00.000Z';
const csrf = 'operations-fixture-csrf-token-0123456789abcdef';
const query = { queryId: 'query-1', queryVersion: '1.0.0', name: 'Latency by component', definition: { kind: 'analytics', metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', window: 'twenty_four_hours', bucket: 'auto', groupBy: 'component', focus: 'all', facets: [], seriesLimit: 12, pointLimit: 200 }, createdAt: observedAt };
const runQuery = { queryId: 'query-run', queryVersion: '1.0.0', name: 'Recent runs', definition: { kind: 'run_search', window: 'twenty_four_hours', statuses: [], components: [], deployments: [], models: [], pageSize: 50 }, createdAt: observedAt };
const dashboard = { dashboardId: 'dashboard-1', dashboardVersion: '1.0.0', name: 'Owner operations', description: 'Bounded metadata', panels: [{ panelId: 'panel-1', title: 'Latency', query: { queryId: 'query-1', queryVersion: '1.0.0' }, visualization: 'table', size: 'full_width', position: 0 }, { panelId: 'panel-2', title: 'Latency trend', query: { queryId: 'query-1', queryVersion: '1.0.0' }, visualization: 'line', size: 'large', position: 1 }, { panelId: 'panel-run', title: 'Run results', query: { queryId: 'query-run', queryVersion: '1.0.0' }, visualization: 'table', size: 'medium', position: 2 }, { panelId: 'panel-incompatible', title: 'Incompatible analytics panel', query: { queryId: 'query-1', queryVersion: '1.0.0' }, visualization: 'comparison', size: 'medium', position: 3 }], createdAt: observedAt };
const traceQuery = { queryId: 'query-trace', queryVersion: '1.0.0', name: 'Trace candidates', definition: { kind: 'trace_search', window: 'twenty_four_hours', components: [], outcomes: [], pageSize: 50 }, createdAt: observedAt };
const comparisonQuery = { queryId: 'query-comparison', queryVersion: '1.0.0', name: 'Compare runs', definition: { kind: 'run_comparison', baselineRunId: 'run-1', candidateRunId: 'run-2' }, createdAt: observedAt };
const regressionQuery = { queryId: 'query-regression', queryVersion: '1.0.0', name: 'Regression decisions', definition: { kind: 'regression_search', window: 'seven_days', outcomes: [], experimentReferences: [], pageSize: 50 }, createdAt: observedAt };
const healthQuery = { queryId: 'query-health', queryVersion: '1.0.0', name: 'Component health', definition: { kind: 'component_health', componentId: 'component-a', componentVersion: '1.0.0', window: 'twenty_four_hours', pointLimit: 50 }, createdAt: observedAt };
const allKindsDashboard = { dashboardId: 'dashboard-all-kinds', dashboardVersion: '1.0.0', name: 'All result kinds', description: 'Bounded result coverage', panels: [{ panelId: 'panel-all-analytics', title: 'Analytics measurements', query: { queryId: 'query-1', queryVersion: '1.0.0' }, visualization: 'line', size: 'medium', position: 0 }, { panelId: 'panel-all-runs', title: 'Run results', query: { queryId: 'query-run', queryVersion: '1.0.0' }, visualization: 'table', size: 'medium', position: 1 }, { panelId: 'panel-all-traces', title: 'Trace results', query: { queryId: 'query-trace', queryVersion: '1.0.0' }, visualization: 'trace', size: 'medium', position: 2 }, { panelId: 'panel-all-comparison', title: 'Run comparison', query: { queryId: 'query-comparison', queryVersion: '1.0.0' }, visualization: 'comparison', size: 'medium', position: 3 }, { panelId: 'panel-all-regression', title: 'Regression decisions', query: { queryId: 'query-regression', queryVersion: '1.0.0' }, visualization: 'regression', size: 'medium', position: 4 }, { panelId: 'panel-all-health', title: 'Component health', query: { queryId: 'query-health', queryVersion: '1.0.0' }, visualization: 'table', size: 'medium', position: 5 }], createdAt: observedAt };
const runPage = { items: [{ runId: 'run-1', status: 'succeeded', startedAt: '2026-10-21T08:30:00.000Z', finishedAt: observedAt, durationMs: 470, model: 'local-model', deploymentId: 'deploy-1', componentIds: ['component-a'], traceId: 'trace-1' }], nextCursor: null, snapshotAt: observedAt, latestObservationId: 'obs-1', partial: false };
const comparisonResult = { snapshotAt: observedAt, left: { role: 'left', runId: '00000000-0000-4000-8000-000000000001', evidence: 'available', status: 'succeeded', outcome: 'succeeded', startedAt: observedAt, finishedAt: observedAt, durationMs: 420, traceId: '0123456789abcdef0123456789abcdef', modelId: { availability: 'available', value: 'model-a' }, modelVersion: { availability: 'available', value: 'v1' }, deploymentId: { availability: 'available', value: 'deploy-a' }, gitCommit: { availability: 'available', value: 'commit-a' }, imageVersion: { availability: 'available', value: 'image-a' }, configurationVersion: { availability: 'available', value: 'config-a' }, agentRevision: { availability: 'available', value: 'agent-a' }, promptBundleRevision: { availability: 'available', value: 'bundle-a' }, policyRevision: { availability: 'available', value: 'policy-a' }, componentVersions: [{ componentId: 'aura.core.api', version: { availability: 'available', value: 'v1' } }], partial: false }, right: { role: 'right', runId: '00000000-0000-4000-8000-000000000002', evidence: 'available', status: 'succeeded', outcome: 'succeeded', startedAt: observedAt, finishedAt: observedAt, durationMs: 470, traceId: '1123456789abcdef0123456789abcdef', modelId: { availability: 'available', value: 'model-b' }, modelVersion: { availability: 'available', value: 'v1' }, deploymentId: { availability: 'available', value: 'deploy-b' }, gitCommit: { availability: 'available', value: 'commit-b' }, imageVersion: { availability: 'available', value: 'image-b' }, configurationVersion: { availability: 'available', value: 'config-b' }, agentRevision: { availability: 'available', value: 'agent-b' }, promptBundleRevision: { availability: 'available', value: 'bundle-b' }, policyRevision: { availability: 'available', value: 'policy-b' }, componentVersions: [{ componentId: 'aura.core.api', version: { availability: 'available', value: 'v2' } }], partial: false }, markerCorrelations: [{ marker: 'deployment_id', state: 'changed', left: { availability: 'available', value: 'deploy-a' }, right: { availability: 'available', value: 'deploy-b' } }], components: [{ componentId: 'aura.core.api', operation: 'request', alignment: 'matched', left: { componentVersion: { availability: 'available', value: 'v1' }, durationMs: 100, outcome: 'succeeded', spanCount: 1, partial: false }, right: { componentVersion: { availability: 'available', value: 'v2' }, durationMs: 120, outcome: 'succeeded', spanCount: 1, partial: false }, measurements: [{ name: 'duration_ms', unit: 'milliseconds', state: 'changed', left: { availability: 'available', value: 100 }, right: { availability: 'available', value: 120 }, absoluteDelta: 20 }] }], trace: { leftTraceId: '0123456789abcdef0123456789abcdef', rightTraceId: '1123456789abcdef0123456789abcdef', leftEvidence: 'available', rightEvidence: 'available', leftIntegrity: ['complete'], rightIntegrity: ['complete'], alignedSpans: [], partial: false }, freshness: { latestObservedAt: observedAt, lagMs: 0, state: 'fresh' }, partial: false, partialReasons: [], unavailableMarkerCount: 0, scan: { scannedRows: 2, scanLimit: 1000000, executionMs: 4, executionLimitMs: 5000 }, limits: { runCount: 2, maximumObservationsPerRun: 10000, maximumSpansPerRun: 5000, maximumMeasurements: 2000, maximumScannedRows: 1000000, executionTimeoutMs: 5000 }, capturePolicy: 'metadata_only' };
const healthResult = { componentId: 'component-a', summary: { sampleCount: 4, latencyP50Ms: 280, latencyP95Ms: 470, errorRate: 0, outcomes: { succeeded: 4, failed: 0, cancelled: 0, unknown: 0 } }, points: [{ startAt: '2026-10-21T08:30:00.000Z', endAt: observedAt, summary: { sampleCount: 4, latencyP50Ms: 280, latencyP95Ms: 470, errorRate: 0, outcomes: { succeeded: 4, failed: 0, cancelled: 0, unknown: 0 } } }] };
const regressionResult = { items: [{ regressionId: 'regression-1', decision: 'unchanged', evidenceState: 'complete', createdAt: observedAt, capturePolicy: 'metadata_only' }], nextCursor: null, snapshotAt: observedAt, partial: false };
const incident = { incidentId: 'incident-1', incidentVersion: '1.0.0', title: 'Elevated latency', state: 'investigating', severity: 'warning', impact: 'degraded', relatedAlerts: [{ resourceType: 'alert_instance', resourceId: 'instance-1', resourceVersion: '1.0.0' }], evidence: [], resolutionNote: null, createdAt: observedAt, updatedAt: observedAt };
const annotation = { annotationId: 'annotation-1', annotationVersion: '1.0.0', target: { targetType: 'run', targetId: 'run-1', targetVersion: '1.0.0' }, text: 'Investigate the bounded latency sample.', createdAt: observedAt, updatedAt: observedAt };
const analyticsSnapshot = { snapshotAt: observedAt, percentileMethod: 'clickhouse_quantile_exact', capturePolicy: 'metadata_only', query: { metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', focus: 'all', from: '2026-10-21T08:00:00.000Z', to: observedAt, bucket: 'auto', bucketSeconds: 1800, groupBy: 'component', filters: { component: 'aura.observatory.operations', }, seriesLimit: 12, pointLimit: 200 }, series: [{ seriesKey: 'aura.observatory.operations', facets: { component: 'aura.observatory.operations' }, summary: { sampleCount: 4, minimum: 120, maximum: 480, average: 280, p50: 280, p90: 450, p95: 470, p99: 480, ratePerInterval: null, rateIntervalSeconds: null }, points: [{ startAt: '2026-10-21T08:00:00.000Z', endAt: '2026-10-21T08:30:00.000Z', value: 420, sampleCount: 2, availability: 'available', partial: false }, { startAt: '2026-10-21T08:30:00.000Z', endAt: '2026-10-21T09:00:00.000Z', value: 470, sampleCount: 2, availability: 'available', partial: false }], availability: 'available', partial: false }], breakdowns: [{ groupBy: 'component', value: 'aura.observatory.operations', count: 4, partial: false }], freshness: { latestObservedAt: observedAt, lagMs: 0, state: 'fresh' }, partial: false, partialReasons: [], unavailableSignals: [], scan: { scannedRows: 4, scanLimit: 1000000, executionMs: 12, executionLimitMs: 5000 } };
const instance = { instanceId: 'instance-1', instanceVersion: '1.0.0', rule: { resourceType: 'alert_rule', resourceId: 'rule-1', resourceVersion: '1.0.0' }, groupingId: 'group-1', groupingFacets: [], severity: 'warning', state: 'firing', evidenceState: 'complete', observedValue: 420, unit: 'milliseconds', sampleSize: 10, evidence: [], firstObservedAt: observedAt, lastObservedAt: observedAt, occurrenceCount: 3, suppressed: false, acknowledged: false };
const page = (items: ReadonlyArray<unknown>) => ({ items, nextCursor: null, snapshotAt: observedAt, partial: false });
const savedQueryExecution = (kind: string, queryId: string, result: unknown) => ({ kind, query: { queryId, queryVersion: '1.0.0' }, state: 'complete', partial: false, snapshotAt: observedAt, freshness: { state: 'fresh', measuredAt: observedAt, lagMs: 0 }, result, ...(kind === 'run_search' || kind === 'trace_search' || kind === 'regression_search' ? { nextCursor: null } : {}) });

async function installOperationsFixture(pageHandle: Page, options: { readonly liveEvent?: boolean; readonly failAfterInitial?: boolean; readonly allKinds?: boolean } = {}): Promise<void> {
  let annotationPresent = true;
  let dashboardRequests = 0;
  await pageHandle.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) { await route.fulfill({ json: { csrfToken: csrf } }); return; }
    if (path.endsWith('/operations/events')) { if (options.liveEvent) await new Promise((resolve) => setTimeout(resolve, 500)); await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' }, body: options.liveEvent ? 'event: operations.changed\ndata: {"resourceType":"dashboard","resourceId":"dashboard-1","resourceVersion":"1.0.0"}\n\n' : ': connected\n\n' }); return; }
    if (path.includes('/evaluation/')) { await route.fulfill({ json: page([]) }); return; }
    if (path.endsWith('/dashboard-presets')) { await route.fulfill({ json: page([{ presetId: 'system-overview', presetVersion: '1.0.0', kind: 'system', name: 'System overview', panels: [], immutable: true }]) }); return; }
    if (path.endsWith('/dashboards')) { dashboardRequests += 1; if (options.failAfterInitial && dashboardRequests > 1) { await route.fulfill({ status: 503, json: { detail: 'dashboard refresh unavailable' } }); return; } await route.fulfill({ json: page([options.allKinds ? allKindsDashboard : dashboard]) }); return; }
    if (path.endsWith('/saved-queries/query-1/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('analytics', 'query-1', analyticsSnapshot) }); return; }
    if (path.endsWith('/saved-queries/query-run/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('run_search', 'query-run', runPage) }); return; }
    if (path.endsWith('/saved-queries/query-trace/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('trace_search', 'query-trace', runPage) }); return; }
    if (path.endsWith('/saved-queries/query-comparison/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('run_comparison', 'query-comparison', comparisonResult) }); return; }
    if (path.endsWith('/saved-queries/query-regression/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('regression_search', 'query-regression', regressionResult) }); return; }
    if (path.endsWith('/saved-queries/query-health/versions/1.0.0/execute')) { await route.fulfill({ json: savedQueryExecution('component_health', 'query-health', healthResult) }); return; }
    if (path.endsWith('/saved-queries')) { await route.fulfill({ json: page(options.allKinds ? [query, runQuery, traceQuery, comparisonQuery, regressionQuery, healthQuery] : [query, runQuery]) }); return; }
    if (path.endsWith('/alert-rules')) { await route.fulfill({ json: page([]) }); return; }
    if (path.endsWith('/alert-instances/instance-1/acknowledgements')) { await route.fulfill({ status: 201, json: { acknowledgementId: 'ack-1', acknowledgementVersion: '1.0.0', instance: { resourceType: 'alert_instance', resourceId: 'instance-1', resourceVersion: '1.0.0' }, reason: 'investigating', acknowledgedAt: observedAt } }); return; }
    if (path.endsWith('/alert-instances')) { await route.fulfill({ json: page([instance]) }); return; }
    if (path.endsWith('/alert-suppressions')) { await route.fulfill({ json: page([]) }); return; }
    if (path.endsWith('/incidents')) { await route.fulfill({ json: page([incident]) }); return; }
    if (path.includes('/annotations/annotation-1/versions/') && path.endsWith('/delete') && request.method() === 'POST') { annotationPresent = false; await route.fulfill({ status: 204 }); return; }
    if (path.endsWith('/annotations')) { await route.fulfill({ json: page(annotationPresent ? [annotation] : []) }); return; }
    if (request.method() === 'POST') { await route.fulfill({ status: 201, json: { ok: true } }); return; }
    if (path.endsWith('/overview')) { await route.fulfill({ json: { observedAt, systemState: 'healthy', observationCount: 0, runCount: 0, latestObservationId: null, ingest: { state: 'healthy', counts: { accepted: 0, rejected: 0, duplicate: 0, malformed: 0, storageFailure: 0 }, lastAcceptedAt: observedAt, lastFailureAt: null } } }); return; }
    if (path.endsWith('/runs')) { await route.fulfill({ json: runPage }); return; }
    if (path.endsWith('/components') || path.endsWith('/deployments') || path.endsWith('/health/history')) { await route.fulfill({ json: page([]) }); return; }
    if (path.endsWith('/analytics/metrics')) { await route.fulfill({ json: { snapshotAt: observedAt, capturePolicy: 'metadata_only', limits: { defaultWindowHours: 24, maximumWindowDays: 31, maximumPointsPerSeries: 200, maximumSeries: 12, maximumBreakdowns: 50, maximumScannedRows: 1000000, executionTimeoutMs: 5000 }, items: [] } }); return; }
    if (path.endsWith('/analytics/query')) { await route.fulfill({ json: analyticsSnapshot }); return; }
    await route.fulfill({ status: 200, json: {} });
  });
}

test.describe('Observatory operations workspaces', () => {
  test('triages grouped alerts and preserves responsive, memory-only operation state', async ({ page }) => {
    await installOperationsFixture(page);
    await page.goto('/');
    await expect(page.getByRole('button', { name: 'Operations', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' })).toBeVisible();
    await page.getByRole('button', { name: /^Alert triage/ }).click();
    await expect(page.getByText('firing', { exact: true })).toBeVisible();
    await page.getByLabel('Acknowledgement reason for instance-1').selectOption('investigating');
    const acknowledge = page.getByRole('button', { name: 'Acknowledge', exact: true });
    await acknowledge.focus();
    await acknowledge.press('Enter');
    await expect(page.getByText('Acknowledge alert accepted.')).toBeVisible();
    await expect(page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length, url: location.href }))).resolves.toMatchObject({ local: 0, session: 0, url: expect.not.stringContaining('Latency') });
    await page.setViewportSize({ width: 360, height: 740 });
    await expect(page.locator('body')).toHaveCSS('overflow-x', 'visible');
  });

  test('supports keyboard workspace navigation and keeps live content out of browser persistence', async ({ page }) => {
    await installOperationsFixture(page);
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' })).toBeVisible();
    const tabs = page.getByRole('navigation', { name: 'Operations workspaces' });
    await expect(tabs).toBeVisible();
    const queriesTab = tabs.getByRole('button', { name: 'Saved queries', exact: true });
    await queriesTab.focus();
    await queriesTab.press('Enter');
    await expect(page.getByRole('heading', { name: 'New bounded saved query' })).toBeVisible();
    await expect(page.getByLabel('Query name')).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole('heading', { name: 'New bounded saved query' })).toBeVisible();
    await expect(page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).resolves.toEqual({ local: 0, session: 0 });
    await expect(page.locator('body')).toHaveCSS('overflow-x', 'visible');
  });

  test('renders bounded dashboard panels and uses an accessible confirmation dialog for destructive alert actions', async ({ page }) => {
    await installOperationsFixture(page);
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'View panels' })).toBeVisible();
    const viewPanels = page.getByRole('button', { name: 'View panels' });
    await viewPanels.focus();
    await viewPanels.press('Enter');
    await expect(page.getByRole('heading', { name: 'Owner operations panel workspace' })).toBeVisible();
    await expect(page.getByRole('table', { name: /Latency.*measured data/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Latency.*measured data/ }).getByText('470', { exact: true })).toBeVisible();
    await expect(page.getByRole('table', { name: /Run results runs/ })).toBeVisible();
    await expect(page.getByText('Incompatible panel', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry panel', exact: true })).toHaveCount(0);
    await expect(page.locator('p.metric-freshness').first()).toContainText(/fresh · observed through/);
    await page.getByRole('button', { name: /^Alert triage/ }).click();
    const suppress = page.getByRole('button', { name: 'Suppress', exact: true });
    await suppress.focus();
    await suppress.press('Enter');
    await expect(page.getByRole('dialog')).toContainText('Suppress alert instance');
    await expect(page.getByRole('dialog')).toContainText('the observation remains available');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await suppress.focus();
    await suppress.press('Enter');
    await page.getByRole('button', { name: 'Confirm', exact: true }).click();
    await expect(page.getByText('Suppress alert accepted.')).toBeVisible();
  });

  test('keeps all result kinds local and focusable at mobile 400 percent zoom', async ({ page }) => {
    await installOperationsFixture(page, { allKinds: true });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    const shellHeading = page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' });
    const beforeActivation = await shellHeading.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, scrollX: window.scrollX, documentWidth: document.documentElement.scrollWidth, viewportWidth: document.documentElement.clientWidth };
    });
    expect(beforeActivation.scrollX).toBe(0);
    expect(beforeActivation.right).toBeLessThanOrEqual(beforeActivation.viewportWidth + 1);
    const viewPanels = page.getByRole('button', { name: 'View panels' }).first();
    await viewPanels.focus();
    await viewPanels.press('Enter');
    await expect(page.getByRole('heading', { name: 'All result kinds panel workspace' })).toBeVisible();
    await page.evaluate(() => { window.scrollTo(500, 0); });
    const selected390 = await page.evaluate(() => ({ rootWidth: document.documentElement.getBoundingClientRect().width, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, scrollX: window.scrollX }));
    expect(selected390.rootWidth).toBeLessThanOrEqual(selected390.clientWidth + 1);
    expect(selected390.scrollWidth).toBeLessThanOrEqual(selected390.clientWidth);
    expect(selected390.scrollX).toBe(0);
    await page.evaluate(() => { document.documentElement.style.zoom = '4'; });
    const resultTableScroller = page.getByRole('region', { name: /Dashboard result table for Run results/ });
    await expect(resultTableScroller).toBeVisible();
    await resultTableScroller.focus();
    await expect(resultTableScroller).toBeFocused();
    const beforeTableScroll = await resultTableScroller.evaluate((element) => ({ left: element.scrollLeft, width: element.scrollWidth, clientWidth: element.clientWidth }));
    expect(beforeTableScroll.width).toBeGreaterThan(beforeTableScroll.clientWidth);
    await resultTableScroller.press('ArrowRight');
    await expect.poll(() => resultTableScroller.evaluate((element) => element.scrollLeft)).toBeGreaterThan(beforeTableScroll.left);
    await expect(page.getByRole('table', { name: /Analytics measurements UTC point measurements/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Run results runs/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Trace results trace candidates/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Run comparison run comparison/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Regression decisions regression decisions/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Component health component health/ })).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return false;
      const rect = active.getBoundingClientRect();
      return rect.left < window.innerWidth && rect.right > 0 && rect.top < window.innerHeight && rect.bottom > 0;
    })).toBeTruthy();
    await page.evaluate(() => { window.scrollTo(500, 0); });
    const rootLayout = await page.evaluate(() => ({ rootWidth: document.documentElement.getBoundingClientRect().width, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, scrollX: window.scrollX }));
    expect(rootLayout.rootWidth).toBeLessThanOrEqual(rootLayout.clientWidth + 1);
    expect(rootLayout.scrollWidth).toBeLessThanOrEqual(rootLayout.clientWidth);
    expect(rootLayout.scrollX).toBe(0);
    const afterActivation = await page.evaluate(() => {
      const heading = document.querySelector<HTMLElement>('#dashboard-preview-heading');
      const workspace = document.querySelector<HTMLElement>('.dashboard-preview');
      const bounds = (element: HTMLElement | null) => { const rect = element?.getBoundingClientRect(); return rect ? { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom } : null; };
      return { heading: bounds(heading), workspace: bounds(workspace), scrollX: window.scrollX, documentWidth: document.documentElement.getBoundingClientRect().width, scrollWidth: document.documentElement.scrollWidth, viewportWidth: document.documentElement.clientWidth };
    });
    expect(afterActivation.scrollX).toBe(0);
    expect(afterActivation.documentWidth).toBeLessThanOrEqual(afterActivation.viewportWidth);
    expect(afterActivation.scrollWidth).toBeLessThanOrEqual(afterActivation.viewportWidth);
    expect(afterActivation.heading?.left).toBeGreaterThanOrEqual(-1);
    expect(afterActivation.heading?.right).toBeLessThanOrEqual(afterActivation.viewportWidth + 1);
    expect(afterActivation.workspace?.left).toBeGreaterThanOrEqual(-1);
    expect(afterActivation.workspace?.right).toBeLessThanOrEqual(afterActivation.viewportWidth + 1);
    await page.evaluate(() => { document.documentElement.style.zoom = ''; });
  });

  test('re-executes the selected dashboard on live refresh, keeps point parity, and clears removed versions', async ({ page }) => {
    await installOperationsFixture(page, { liveEvent: true });
    let analyticsRequests = 0;
    page.on('request', (request) => { if (new URL(request.url()).pathname.endsWith('/saved-queries/query-1/versions/1.0.0/execute')) analyticsRequests += 1; });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await page.getByRole('button', { name: 'View panels' }).focus();
    await page.getByRole('button', { name: 'View panels' }).press('Enter');
    await expect(page.getByRole('table', { name: /Latency trend UTC point measurements/ })).toBeVisible();
    await expect(page.getByRole('table', { name: /Latency trend UTC point measurements/ }).locator('tbody tr').nth(1).getByRole('rowheader')).toContainText('2026-10-21T08:30:00.000Z');
    await expect.poll(() => analyticsRequests).toBeGreaterThan(1);
    await page.route('**/api/v1/dashboards**', async (route) => {
      if (route.request().method() === 'GET') { await route.fulfill({ json: { items: [], nextCursor: null, snapshotAt: observedAt, partial: false } }); return; }
      await route.fallback();
    });
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Owner operations panel workspace' })).toHaveCount(0);
  });

  test('keeps incident transitions valid and avoids page overflow at narrow zoom', async ({ page }) => {
    await installOperationsFixture(page);
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' })).toBeVisible();
    const dashboardView = page.getByRole('button', { name: 'View panels' }).first();
    await dashboardView.focus();
    await dashboardView.press('Enter');
    await expect(page.getByRole('heading', { name: 'Owner operations panel workspace' })).toBeVisible();
    await page.setViewportSize({ width: 320, height: 568 });
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; window.scrollTo(500, 0); });
    const selected320 = await page.evaluate(() => ({ rootWidth: document.documentElement.getBoundingClientRect().width, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, scrollX: window.scrollX }));
    expect(selected320.rootWidth).toBeLessThanOrEqual(selected320.clientWidth + 1);
    expect(selected320.scrollWidth).toBeLessThanOrEqual(selected320.clientWidth);
    expect(selected320.scrollX).toBe(0);
    await page.evaluate(() => { document.documentElement.style.zoom = ''; });
    const operationsTabs = page.getByRole('navigation', { name: 'Operations workspaces' });
    await expect(operationsTabs).toBeVisible();
    await operationsTabs.getByRole('button', { name: /^Incidents/ }).click();
    await expect(page.getByText('investigating', { exact: true })).toBeVisible();
    const stateSelect = page.getByLabel('Next state');
    await expect(stateSelect).toHaveValue('mitigated');
    await expect(stateSelect.locator('option')).toHaveCount(3);
    await expect(stateSelect.locator('option').allTextContents()).resolves.toEqual(['mitigated', 'resolved', 'open']);
    await page.getByRole('button', { name: 'Transition', exact: true }).click();
    await expect(page.getByRole('dialog')).toContainText('Transition incident to mitigated');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: /^Alert triage/ }).click();
    await page.setViewportSize({ width: 320, height: 568 });
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
    await page.evaluate(() => { document.documentElement.style.zoom = ''; });
  });

  test('retains a saved-query draft and shows an inline error when the command is rejected', async ({ page }) => {
    await installOperationsFixture(page);
    await page.route('**/api/v1/saved-queries', async (route) => {
      if (route.request().method() === 'POST') { await route.fulfill({ status: 422, json: { detail: 'saved query rejected by bounded validation' } }); return; }
      await route.continue();
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Dashboards, alerts, and incidents' })).toBeVisible();
    await page.getByRole('button', { name: 'Saved queries', exact: true }).click();
    const name = page.getByLabel('Query name');
    await name.fill('Keep this draft');
    await page.getByRole('button', { name: 'Save bounded query', exact: true }).click();
    await expect(name).toHaveValue('Keep this draft');
    await expect(page.locator('.inline-error')).toContainText('422');
  });

  test('deletes an annotation and restores focus to its collection heading', async ({ page }) => {
    await installOperationsFixture(page);
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await page.getByRole('button', { name: 'Annotations', exact: true }).click();
    const deleteButton = page.getByRole('button', { name: 'Delete annotation', exact: true });
    await expect(deleteButton).toBeVisible();
    await deleteButton.focus();
    await deleteButton.press('Enter');
    await expect(page.getByRole('dialog')).toContainText('Delete annotation');
    await page.getByRole('button', { name: 'Confirm', exact: true }).click();
    await expect(page.getByText('Delete annotation accepted.')).toBeVisible();
    await expect(page.locator('#annotation-heading')).toBeFocused();
    await expect(page.getByRole('button', { name: 'Delete annotation', exact: true })).toHaveCount(0);
  });

  test('retains measured dashboard panels and announces a manual refresh failure', async ({ page }) => {
    await installOperationsFixture(page, { failAfterInitial: true });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await page.getByRole('button', { name: 'View panels' }).focus();
    await page.getByRole('button', { name: 'View panels' }).press('Enter');
    await expect(page.getByRole('table', { name: /Latency.*measured data/ })).toBeVisible();
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Refresh failed' }).first()).toContainText('Refresh failed');
    await expect(page.locator('.metric-freshness').first()).toContainText('stale');
    await expect(page.getByRole('button', { name: 'Retry operation views' })).toBeVisible();
    await expect(page.locator('.dashboard-preview').getByText('470', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('table', { name: /Run results runs/ })).toBeVisible();
  });

  test('retains measured dashboard panels and announces an SSE refresh failure', async ({ page }) => {
    await installOperationsFixture(page, { liveEvent: true, failAfterInitial: true });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await page.getByRole('button', { name: 'View panels' }).focus();
    await page.getByRole('button', { name: 'View panels' }).press('Enter');
    await expect(page.getByRole('table', { name: /Latency.*measured data/ })).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: 'Refresh failed' }).first()).toContainText('Refresh failed');
    await expect(page.locator('.metric-freshness').first()).toContainText('stale');
    await expect(page.locator('.dashboard-preview').getByText('470', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('table', { name: /Run results runs/ })).toBeVisible();
  });

  test('passes axe in ready dark reduced-motion high-contrast mode', async ({ page }) => {
    await installOperationsFixture(page);
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce', forcedColors: 'active' });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    const viewPanels = page.getByRole('button', { name: 'View panels' }).first();
    await viewPanels.focus();
    await viewPanels.press('Enter');
    await expect(page.getByRole('heading', { name: 'Owner operations panel workspace' })).toBeVisible();
    await expect(page.getByRole('region', { name: /Dashboard result table/ }).first()).toBeVisible();
    const result = await new AxeBuilder({ page }).include('observatory-operations-workspace').analyze();
    expect(result.violations).toEqual([]);
  });

  test('passes axe while the operations snapshot is loading', async ({ page }) => {
    await installOperationsFixture(page);
    await page.route('**/api/v1/**', async (route) => { await new Promise((resolve) => setTimeout(resolve, 750)); await route.fallback(); });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByText('Loading owner operation workspaces…')).toBeVisible();
    const result = await new AxeBuilder({ page }).include('observatory-operations-workspace').analyze();
    expect(result.violations).toEqual([]);
  });

  test('passes axe in the rejected operation-data state', async ({ page }) => {
    await installOperationsFixture(page);
    await page.route('**/api/v1/alert-instances*', async (route) => { if (route.request().method() === 'GET') { await route.fulfill({ status: 422, json: { detail: 'bounded fixture rejection' } }); return; } await route.fallback(); });
    await page.goto('/');
    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    await expect(page.getByText(/rejected/i).first()).toBeVisible();
    const viewPanels = page.getByRole('button', { name: 'View panels' }).first();
    await viewPanels.focus();
    await viewPanels.press('Enter');
    await expect(page.getByRole('heading', { name: 'Owner operations panel workspace' })).toBeVisible();
    const result = await new AxeBuilder({ page }).include('observatory-operations-workspace').analyze();
    expect(result.violations).toEqual([]);
  });
});
