import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const observedAt = '2026-10-28T08:00:00.000Z';
const workflowFrom = '2026-10-27T08:00:00.000Z';
const workflowTo = '2026-10-28T08:00:00.000Z';
const runId = '6f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a01';
const traceId = '0123456789abcdef0123456789abcdef';

const page = (items: ReadonlyArray<unknown>) => ({ items, nextCursor: null, snapshotAt: observedAt });

const workflowSnapshot = {
  snapshotAt: observedAt,
  window: { from: workflowFrom, to: workflowTo, bucket: 'one_hour', bucketSeconds: 3600 },
  lifecycleCounts: [
    { metric: 'workflow_transition_outcome', operation: 'workflow.transition', outcome: 'succeeded', count: 8, partial: false },
    { metric: 'workflow_command_outcome', operation: 'workflow.command', outcome: 'failed', count: 1, partial: false },
  ],
  latencySummaries: [
    {
      metric: 'workflow_wait_lag_ms',
      unit: 'milliseconds',
      availability: 'available',
      partial: false,
      summary: { sampleCount: 8, minimum: 10, maximum: 90, average: 44, p50: 40, p90: 75, p95: 80, p99: 90, ratePerInterval: null, rateIntervalSeconds: null },
    },
  ],
  series: [
    {
      metric: 'workflow_wait_lag_ms',
      unit: 'milliseconds',
      availability: 'available',
      partial: false,
      summary: { sampleCount: 2, minimum: 35, maximum: 52, average: 43.5, p50: 43.5, p90: 50, p95: 51, p99: 52, ratePerInterval: null, rateIntervalSeconds: null },
      points: [
        { startAt: '2026-10-27T08:00:00.000Z', endAt: '2026-10-27T09:00:00.000Z', value: 35, sampleCount: 4, availability: 'available', partial: false },
        { startAt: '2026-10-28T07:00:00.000Z', endAt: '2026-10-28T08:00:00.000Z', value: 52, sampleCount: 4, availability: 'available', partial: false },
      ],
    },
  ],
  failureBreakdowns: [
    { component: 'aura.automation.workflow_step.wait_until', operation: 'resume', outcome: 'failed', errorClass: 'timeout', count: 1, partial: false },
  ],
  recentCorrelations: [
    { metric: 'workflow_agent_run_step_duration_ms', runId, traceId, observedAt, component: 'aura.automation.workflow_step.agent_run', operation: 'execute', outcome: 'succeeded', errorClass: null },
  ],
  freshness: { state: 'fresh', latestObservedAt: observedAt, lagMs: 0 },
  partial: false,
  partialReasons: [],
  unavailableSignals: [],
  limits: {
    defaultWindowHours: 24,
    maximumWindowDays: 31,
    maximumScannedRows: 1000000,
    scannedRows: 12,
    maximumSeries: 12,
    returnedSeries: 1,
    maximumPointsPerSeries: 200,
    returnedPoints: 2,
    maximumFailureBreakdowns: 50,
    returnedFailureBreakdowns: 1,
    maximumRecentCorrelations: 25,
    returnedRecentCorrelations: 1,
    executionLimitMs: 5000,
    executionMs: 4,
    truncatedByExecutionLimit: false,
    truncatedByStorageLimit: false,
  },
  capturePolicy: 'metadata_only',
};

type MonitoringResponse = { readonly status?: number; readonly body: unknown };
type WorkflowFixtureOptions = {
  readonly monitoring?: MonitoringResponse | ReadonlyArray<MonitoringResponse>;
  readonly authStatus?: number;
};

async function installWorkflowFixture(pageHandle: Page, options: WorkflowFixtureOptions = {}): Promise<{ workflowRequests: string[] }> {
  const workflowRequests: string[] = [];
  let monitoringIndex = 0;
  await pageHandle.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) {
      await route.fulfill({ status: options.authStatus ?? 200, json: { csrfToken: 'fixture-observatory-csrf-token-0123456789abcdef' } });
      return;
    }
    if (path.endsWith('/events') || path.endsWith('/evaluation/events')) {
      // A terminal 204 keeps EventSource from reconnecting forever in a
      // mocked browser run; the dashboard still exercises disconnected-state
      // handling without a retry storm starving Angular navigation.
      await route.fulfill({ status: 204 });
      return;
    }
    if (path.endsWith('/automation/workflows/monitoring')) {
      workflowRequests.push(request.url());
      const configured = options.monitoring;
      const responses = Array.isArray(configured) ? configured : configured ? [configured] : [{ body: workflowSnapshot }];
      const response = responses[Math.min(monitoringIndex++, responses.length - 1)];
      await route.fulfill({ status: response.status ?? 200, json: response.body });
      return;
    }
    if (path.endsWith('/overview')) {
      await route.fulfill({
        json: {
          observedAt,
          systemState: 'healthy',
          observationCount: 0,
          runCount: 0,
          latestObservationId: null,
          ingest: { state: 'healthy', counts: { accepted: 0, rejected: 0, duplicate: 0, malformed: 0, storageFailure: 0 }, lastAcceptedAt: observedAt, lastFailureAt: null },
        },
      });
      return;
    }
    if (path.endsWith('/runs')) {
      await route.fulfill({ json: { items: [], nextCursor: null, snapshotAt: observedAt, latestObservationId: null } });
      return;
    }
    if (path.endsWith('/components') || path.endsWith('/deployments') || path.endsWith('/health/history')) {
      await route.fulfill({ json: page([]) });
      return;
    }
    if (request.method() === 'POST') {
      await route.fulfill({ status: 201, json: { ok: true } });
      return;
    }
    await route.fulfill({ status: 200, json: {} });
  });
  return { workflowRequests };
}

async function openWorkflows(pageHandle: Page): Promise<void> {
  if ((pageHandle.viewportSize()?.width ?? 1280) <= 832) {
    await pageHandle.getByRole('button', { name: 'Open Observatory navigation', exact: true }).click();
    await expect(pageHandle.locator('#observatory-sections')).toBeVisible();
    const workflowNav = pageHandle.locator('#observatory-sections').getByRole('button', { name: 'Workflows', exact: true });
    await workflowNav.focus();
    await pageHandle.keyboard.press('Enter');
    return;
  }
  await pageHandle.getByRole('button', { name: 'Workflows', exact: true }).click({ force: true });
}

async function openWorkflowDashboard(pageHandle: Page, options: WorkflowFixtureOptions = {}): Promise<{ workflowRequests: string[] }> {
  const fixture = await installWorkflowFixture(pageHandle, options);
  await pageHandle.goto('/');
  await expect(pageHandle.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
  await openWorkflows(pageHandle);
  await expect(pageHandle.getByRole('heading', { level: 2, name: 'Workflow monitoring' })).toBeVisible();
  return fixture;
}

test.describe('Observatory workflow monitoring', () => {
  test('opens the bounded workflow dashboard and keeps UTC filters in memory', async ({ page }) => {
    const fixture = await installWorkflowFixture(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    await openWorkflows(page);
    await expect(page.getByRole('heading', { level: 2, name: 'Workflow monitoring' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Workflow lifecycle' })).toBeVisible();
    await expect(page.getByText('execute', { exact: true })).toBeVisible();
    await expect(page.getByText('timeout', { exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: /Accessible table for Wait Lag/ })).toBeVisible();

    const from = page.getByLabel('From (UTC)');
    const to = page.getByLabel('To (UTC)');
    await from.fill('2026-10-26T08:00');
    await to.fill('2026-10-28T08:00');
    await page.getByRole('button', { name: 'Apply window', exact: true }).click();
    await expect.poll(() => fixture.workflowRequests.length).toBeGreaterThan(1);
    expect(fixture.workflowRequests.at(-1)).toContain('from=2026-10-26T08%3A00%3A00.000Z');
    expect(fixture.workflowRequests.at(-1)).toContain('to=2026-10-28T08%3A00%3A00.000Z');
    expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  });

  test('supports keyboard activation of the workflow destination', async ({ page }) => {
    await installWorkflowFixture(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    if ((page.viewportSize()?.width ?? 1280) <= 832) {
      await page.getByRole('button', { name: 'Open Observatory navigation', exact: true }).click();
    }
    const workflows = page.locator('#observatory-sections').getByRole('button', { name: 'Workflows', exact: true });
    await workflows.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { level: 2, name: 'Workflow monitoring' })).toBeVisible();
  });

  test('keeps partial and stale query evidence explicit', async ({ page }) => {
    await openWorkflowDashboard(page, {
      monitoring: {
        body: {
          ...workflowSnapshot,
          partial: true,
          partialReasons: ['late_data'],
          freshness: { ...workflowSnapshot.freshness, state: 'stale', lagMs: 3600000 },
          limits: { ...workflowSnapshot.limits, truncatedByStorageLimit: true },
        },
      },
    });
    await expect(page.getByText(/Partial: late data/)).toBeVisible();
    await expect(page.getByText('Results truncated by query limits', { exact: true })).toBeVisible();
    await expect(page.getByText(/Source observed .*07:00 UTC/)).toBeVisible();
    await expect(page.getByText(/Source lag 3,600,000 ms/)).toBeVisible();
  });

  test('renders an exact empty backend snapshot as empty without numeric zeroes', async ({ page }) => {
    await openWorkflowDashboard(page, {
      monitoring: {
        body: {
          ...workflowSnapshot,
          lifecycleCounts: [],
          latencySummaries: [],
          series: [],
          failureBreakdowns: [],
          recentCorrelations: [],
          freshness: { state: 'unavailable', latestObservedAt: null, lagMs: null },
          unavailableSignals: ['lifecycle_counts', 'latency_summaries', 'time_series', 'failure_breakdowns', 'run_trace_correlation'],
          limits: { ...workflowSnapshot.limits, returnedSeries: 0, returnedPoints: 0, returnedFailureBreakdowns: 0, returnedRecentCorrelations: 0 },
        },
      },
    });
    await expect(page.getByText('No workflow telemetry in this window', { exact: true })).toBeVisible();
    await expect(page.getByText('Source: unavailable data', { exact: true })).toBeVisible();
    await expect(page.getByText('Unavailable signals: lifecycle counts, latency summaries, time series, failure breakdowns, run trace correlation', { exact: true })).toBeVisible();
    await expect(page.locator('.summary-card')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('rejects malformed responses without disclosing payload content', async ({ page }) => {
    await openWorkflowDashboard(page, { monitoring: { body: { unexpected: 'workflow-content-should-not-render' } } });
    await expect(page.getByRole('alert')).toContainText('Response rejected');
    await expect(page.getByText('workflow-content-should-not-render', { exact: true })).toHaveCount(0);
  });

  test('isolates an unavailable Observatory monitoring endpoint', async ({ page }) => {
    await openWorkflowDashboard(page, { monitoring: { status: 503, body: { code: 'unavailable' } } });
    await expect(page.getByRole('alert')).toContainText('Workflow monitoring unavailable');
    await expect(page.getByRole('button', { name: 'Retry snapshot', exact: true })).toBeVisible();
  });

  test('handles an expired owner session and resets the workflow view', async ({ page }) => {
    await openWorkflowDashboard(page, {
      monitoring: [{ body: workflowSnapshot }, { status: 401, body: { code: 'unauthorized' } }],
    });
    await page.getByRole('button', { name: 'Refresh workflow monitoring snapshot', exact: true }).click();
    await expect(page.getByRole('heading', { name: /session expired|signed out|sign-in was not authorized/i })).toBeVisible();
  });

  test('coalesces invalidations and refreshes delayed genuine observations', async ({ page }) => {
    await page.addInitScript(() => {
      type Listener = (event: Event) => void;
      class FixtureEventSource {
        static readonly instances: FixtureEventSource[] = [];
        static eventSequence = 0;
        readonly listeners = new Map<string, Listener[]>();
        onopen: (() => void) | null = null;
        onmessage: Listener | null = null;
        onerror: (() => void) | null = null;
        closed = false;
        constructor(readonly url: string) { FixtureEventSource.instances.push(this); setTimeout(() => this.onopen?.(), 0); }
        addEventListener(type: string, listener: Listener): void { this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]); }
        close(): void { this.closed = true; }
        // The stabilized backend removes only its exact query_api self event;
        // this public fixture represents genuine generic observations.
        emit(type: string): void {
          const event = new MessageEvent(type, { data: JSON.stringify({ resourceType: 'observation' }), lastEventId: `${type}-fixture-${++FixtureEventSource.eventSequence}` });
          for (const listener of this.listeners.get(type) ?? []) listener(event);
          if (type === 'message') this.onmessage?.(event);
        }
      }
      Object.defineProperty(window, 'EventSource', { configurable: true, value: FixtureEventSource });
      Object.defineProperty(window, '__emitWorkflowInvalidations', {
        configurable: true,
        value: (count = 1) => { for (let index = 0; index < count; index += 1) FixtureEventSource.instances[0]?.emit('observation.changed'); },
      });
    });
    const fixture = await openWorkflowDashboard(page);
    const initialRequests = fixture.workflowRequests.length;
    await page.evaluate(() => {
      const emit = (window as Window & { __emitWorkflowInvalidations?: () => void }).__emitWorkflowInvalidations;
      emit?.(3);
    });
    await expect.poll(() => fixture.workflowRequests.length, { timeout: 5000 }).toBe(initialRequests + 1);
    await page.waitForTimeout(2200);
    await page.evaluate(() => {
      const emit = (window as Window & { __emitWorkflowInvalidations?: (count?: number) => void }).__emitWorkflowInvalidations;
      emit?.();
    });
    await expect.poll(() => fixture.workflowRequests.length, { timeout: 5000 }).toBe(initialRequests + 2);
  });

  test('opens the existing run/trace inspector from a bounded correlation', async ({ page }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await openWorkflowDashboard(page);
    await page.getByRole('button', { name: /Inspect run/ }).click();
    await expect(page.getByRole('heading', { level: 2, name: 'Run inspector' })).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('remains accessible and usable with reduced motion and 200/400 percent zoom', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openWorkflowDashboard(page);
    for (const zoom of [2, 4]) {
      await page.evaluate((value) => { document.documentElement.style.zoom = String(value); }, zoom);
      const layout = await page.evaluate(() => ({ bodyWidth: document.body.scrollWidth, viewportWidth: document.documentElement.clientWidth }));
      expect(layout.bodyWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    }
    await page.evaluate(() => { document.documentElement.style.zoom = '1'; });
    const axe = await new AxeBuilder({ page }).include('#observatory-main').analyze();
    expect(axe.violations, JSON.stringify(axe.violations)).toEqual([]);
  });
});
