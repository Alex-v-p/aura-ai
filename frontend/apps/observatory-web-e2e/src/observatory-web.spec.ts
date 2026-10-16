import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

// Keep the normal fixture inside the freshness window. Stale-state coverage
// opts into the explicitly old timestamp below instead of making every live
// update test depend on the calendar date.
const observedAt = new Date(Date.now() - 30_000).toISOString();
const oldObservedAt = new Date(Date.now() - 10 * 60_000).toISOString();
const runId = '6f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a01';
const traceId = '0123456789abcdef0123456789abcdef';
const actionId = 'fda9d3fb-79c7-472b-bf18-1c76a9ec4f25';
const fixtureCsrfToken = 'fixture-observatory-csrf-token-0123456789abcdef';

type FixtureHealthState = 'healthy' | 'delayed' | 'partial' | 'degraded' | 'unavailable';

const overview = (state: 'healthy' | 'degraded' | 'unavailable' = 'healthy', stale = false, healthState: FixtureHealthState = state === 'healthy' ? 'healthy' : 'degraded') => ({
  observedAt: stale ? oldObservedAt : observedAt,
  systemState: state,
  ingest: {
    state,
    counts: { accepted: 12, rejected: 1, duplicate: 2, malformed: 1, storageFailure: state === 'degraded' ? 1 : 0 },
    lastAcceptedAt: observedAt,
    lastFailureAt: state === 'healthy' ? null : observedAt,
  },
  observationCount: 12,
  runCount: 1,
  latestObservationId: 'b8fb4c57-95a8-46b4-9d8f-5c11fb8c7c22',
  health: {
    snapshotId: '3cbd2a62-8a4e-4596-9e59-9fd2d52a5de5',
    observedAt: stale ? oldObservedAt : observedAt,
    state: healthState,
    intakeLagMs: healthState === 'healthy' ? 120 : 7200,
    visibilityWatermark: 10,
    visibilityLagCount: state === 'healthy' ? 0 : 2,
    rejectionReasons: [{ reason: 'schema_validation', count: 1 }],
    duplicateCount: 2,
    storageLatency: { sampleCount: 12, latestMs: 18, p50Ms: 20, p95Ms: 42 },
    queryLatency: { sampleCount: 6, latestMs: 11, p50Ms: 12, p95Ms: 24 },
    queryFreshnessLagMs: healthState === 'healthy' ? 160 : 7400,
    workerHeartbeatAt: observedAt,
    workerHeartbeatAgeMs: healthState === 'healthy' ? 500 : 80000,
    backlogCount: healthState === 'healthy' ? 0 : 4,
    oldestBacklogAgeMs: healthState === 'healthy' ? null : 120000,
  },
});

const healthHistory = {
  items: [overview('healthy').health, { ...overview('degraded').health, snapshotId: '0baf6fc7-2c63-49e3-8740-0b707aaeb2dd' }],
  nextCursor: null,
  snapshotAt: observedAt,
};

const run = {
  runId,
  status: 'succeeded',
  startedAt: '2026-10-15T08:59:55.000Z',
  finishedAt: observedAt,
  durationMs: 5000,
  model: 'fixture-chat',
  deploymentId: 'dev-local',
  componentIds: ['aura.core.api', 'aura.core.worker'],
  traceId,
};

const componentHealth = {
  sampleCount: 4,
  latencyP50Ms: 21,
  latencyP95Ms: 44,
  errorRate: 0.25,
  outcomes: { succeeded: 3, failed: 1, cancelled: 0, unknown: 0 },
};

function runDetail() {
  return {
    ...run,
    observations: [
      { observationId: 'a3fdc06f-88ad-4a05-970d-f8b7c8cfb1cc', observedAt, componentId: 'aura.core.api', componentVersion: '1.2.0', operation: 'request', measurement: { name: 'duration', value: 21, unit: 'milliseconds' }, outcome: 'succeeded', deploymentId: 'dev-local', serviceRole: 'api', capturePolicy: 'metadata_only' },
      { observationId: 'bb03bb2c-c2d9-4ba9-9e46-79e6fa5c3868', observedAt, componentId: 'aura.core.worker', componentVersion: '1.2.0', operation: 'job', measurement: { name: 'duration', value: 44, unit: 'milliseconds' }, outcome: 'succeeded', deploymentId: 'dev-local', serviceRole: 'worker', capturePolicy: 'metadata_only' },
    ],
    partial: false,
    nextCursor: null,
  };
}

function traceDetail() {
  return {
    traceId,
    runId,
    partial: true,
    nextCursor: null,
    spans: [
      { spanId: 'aaaaaaaaaaaaaaaa', parentSpanId: null, componentId: 'aura.core.api', operation: 'request', startedAt: '2026-10-15T08:59:55.000Z', durationMs: 5000, outcome: 'succeeded', depth: 0, partial: false },
      { spanId: 'bbbbbbbbbbbbbbbb', parentSpanId: 'aaaaaaaaaaaaaaaa', componentId: 'aura.core.worker', operation: 'job', startedAt: '2026-10-15T08:59:56.000Z', durationMs: 3200, outcome: 'succeeded', depth: 1, partial: false },
      { spanId: 'cccccccccccccccc', parentSpanId: 'missing-parent-1', componentId: 'aura.core.scheduler', operation: 'schedule', startedAt: '2026-10-15T08:59:57.000Z', durationMs: 100, outcome: 'unknown', depth: 0, partial: true },
    ],
  };
}

type FixtureOptions = {
  anonymous?: boolean;
  identityUnavailable?: boolean;
  identityFailures?: number;
  logoutStatuses?: number[];
  csrfTokens?: string[];
  expireNextRuns?: boolean;
  stale?: boolean;
  unavailable?: boolean;
  sseDisconnected?: boolean;
  retentionFailure?: boolean;
  partial?: boolean;
  healthUnavailable?: boolean;
  emptyHealthHistory?: boolean;
  healthChanged?: boolean;
  healthState?: FixtureHealthState;
  deferAfterLogout?: boolean;
  deferCatalogue?: boolean;
  componentStale?: boolean;
  componentTopology?: 'declared' | 'multi-version' | 'partial' | 'unavailable' | 'empty';
  detailFailure?: 'definition' | 'dependencies' | 'malformed' | 'malformed-topology';
};

type FixtureHandle = {
  retentionRequests: Array<{ action: string; body: Record<string, unknown>; headers: Record<string, string> }>;
  logoutRequests: Array<{ headers: Record<string, string> }>;
  deferredKinds: Set<string>;
  releaseDeferred: () => void;
  failCatalogueRefresh: () => void;
  restoreIdentity: () => void;
  expireSession: () => void;
};

async function installFixture(page: Page, options: FixtureOptions = {}): Promise<FixtureHandle> {
  const retentionRequests: Array<{ action: string; body: Record<string, unknown>; headers: Record<string, string> }> = [];
  const logoutRequests: Array<{ headers: Record<string, string> }> = [];
  const deferredKinds = new Set<string>();
  const deferredResolvers: Array<() => void> = [];
  let actionPolls = 0;
  let namedObservationReconciled = false;
  let loggedOut = options.anonymous ?? false;
  let identityUnavailable = options.identityUnavailable ?? false;
  let identityAttempts = 0;
  let logoutAttempts = 0;
  let sessionTokenIndex = 0;
  let expireSessionRequested = false;
  let healthChanged = false;
  let detailFailureAttempts = 0;
  let componentRefreshFailure = false;

  const defer = async (kind: string): Promise<void> => {
    if (!options.deferAfterLogout && !(kind === 'catalogue' && options.deferCatalogue)) return;
    deferredKinds.add(kind);
    await new Promise<void>((resolve) => deferredResolvers.push(resolve));
  };

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/logout') && request.method() === 'POST') {
      logoutRequests.push({ headers: request.headers() });
      const status = options.logoutStatuses?.[logoutAttempts++] ?? 204;
      if (status === 204 || status === 401) loggedOut = true;
      await route.fulfill(status === 204 ? { status } : { status, json: { code: status === 401 ? 'session_required' : 'logout_unavailable' } });
      return;
    }
    if (path.endsWith('/auth/me')) {
      if (identityUnavailable || identityAttempts++ < (options.identityFailures ?? 0)) {
        await route.fulfill({ status: 503, json: { code: 'identity_unavailable' } });
        return;
      }
      if (loggedOut) {
        await route.fulfill({ status: 401, json: { code: 'session_required' } });
        return;
      }
      const csrfToken = options.csrfTokens?.[Math.min(sessionTokenIndex++, (options.csrfTokens?.length ?? 1) - 1)] ?? fixtureCsrfToken;
      await route.fulfill({ json: { csrfToken } });
      return;
    }
    if (path.endsWith('/overview')) {
      const currentHealthState = healthChanged ? 'delayed' : options.healthState ?? (options.sseDisconnected ? 'degraded' : 'healthy');
      const currentOverview = overview(options.sseDisconnected ? 'degraded' : 'healthy', options.stale, currentHealthState);
      await route.fulfill({ status: options.unavailable ? 503 : 200, json: options.unavailable ? { code: 'service_unavailable', message: 'temporarily unavailable', retryable: true, traceId } : { ...currentOverview, observationCount: namedObservationReconciled ? 13 : currentOverview.observationCount } });
      return;
    }
    if (path.endsWith('/health/history')) {
      const currentHistory = options.emptyHealthHistory ? { ...healthHistory, items: [] } : healthChanged ? { ...healthHistory, items: [{ ...healthHistory.items[0], state: 'delayed', intakeLagMs: 7200, snapshotId: '9a8b7c6d-5e4f-4321-9abc-def012345678' }, ...healthHistory.items.slice(1)] } : healthHistory;
      await route.fulfill(options.healthUnavailable ? { status: 503, json: { code: 'service_unavailable', message: 'health history unavailable', retryable: true, traceId } } : { json: currentHistory });
      return;
    }
    if (path.endsWith('/runs') && request.method() === 'GET') {
      if (options.expireNextRuns && expireSessionRequested) {
        expireSessionRequested = false;
        loggedOut = true;
        await route.fulfill({ status: 401, json: { code: 'session_required' } });
        return;
      }
      if (options.partial) { await route.abort('failed'); return; }
      await route.fulfill({ json: { items: [run], nextCursor: null, snapshotAt: observedAt, latestObservationId: 'b8fb4c57-95a8-46b4-9d8f-5c11fb8c7c22' } });
      return;
    }
    if (path.endsWith(`/runs/${runId}`)) {
      await defer('run');
      await route.fulfill({ json: runDetail() });
      return;
    }
    if (path.endsWith(`/traces/${traceId}`)) {
      await route.fulfill({ json: traceDetail() });
      return;
    }
    if (path.endsWith('/components') && request.method() === 'GET') {
      if (options.componentStale && componentRefreshFailure) { await route.abort('failed'); return; }
      if (options.partial) { await route.abort('failed'); return; }
      if (options.deferCatalogue) await defer('catalogue');
      if (options.componentTopology === 'unavailable') {
        await route.fulfill({ status: 503, json: { code: 'service_unavailable', message: 'component catalogue unavailable', retryable: true, traceId } });
        return;
      }
      if (options.componentTopology === 'empty') {
        await route.fulfill({ json: { snapshotAt: observedAt, items: [] } });
        return;
      }
      if (options.componentTopology === 'declared' || options.componentTopology === 'multi-version' || options.componentTopology === 'partial') {
        await route.fulfill({ json: { snapshotAt: observedAt, items: [
          { componentId: 'aura.observatory.query_api', versions: ['1.0.0'], firstSeenAt: observedAt, lastSeenAt: observedAt, definitionState: 'declared', definitionVersions: ['1.0.0'], health: componentHealth },
          { componentId: 'aura.core.scheduler', versions: ['9.9.9'], firstSeenAt: observedAt, lastSeenAt: observedAt, definitionVersions: [], health: { ...componentHealth, errorRate: null, outcomes: { succeeded: 0, failed: 0, cancelled: 0, unknown: 4 } } },
          { componentId: `aura.observatory.observed_${'x'.repeat(70)}`, versions: ['9.9.9'], firstSeenAt: observedAt, lastSeenAt: observedAt, definitionVersions: [], health: { ...componentHealth, errorRate: null, outcomes: { succeeded: 0, failed: 0, cancelled: 0, unknown: 4 } } },
        ] } });
        return;
      }
      await route.fulfill({ json: { snapshotAt: observedAt, items: [
        { componentId: 'aura.core.api', versions: ['1.2.0'], firstSeenAt: observedAt, lastSeenAt: observedAt, health: componentHealth },
        { componentId: 'aura.core.worker', versions: ['1.2.0'], firstSeenAt: observedAt, lastSeenAt: observedAt, health: { ...componentHealth, errorRate: 0 } },
      ] } });
      return;
    }
    if (path.endsWith('/components/aura.observatory.query_api/definitions/1.0.0')) {
      if (options.detailFailure === 'definition' && detailFailureAttempts++ === 0) {
        await route.fulfill({ status: 503, json: { code: 'service_unavailable', message: 'definition unavailable', retryable: true, traceId } });
        return;
      }
      if (options.detailFailure === 'malformed' && detailFailureAttempts++ === 0) {
        await route.fulfill({ json: { componentId: 'aura.observatory.query_api', componentVersion: '1.0.0', owner: null } });
        return;
      }
      await route.fulfill({ json: {
        componentId: 'aura.observatory.query_api', componentVersion: '1.0.0', owner: 'observatory', category: 'api',
        inputSchemaId: 'metadata-request-v1', outputSchemaId: 'metadata-response-v1', measurements: ['duration_ms'], evaluators: ['observatory_self_monitoring'],
        dependencies: [
          { dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', requiredVersion: '1.0.0' },
          { dependencyId: 'missing.provider', kind: 'provider', requiredVersion: null },
        ],
      } });
      return;
    }
    if (path.endsWith('/components/aura.observatory.query_api/definitions/1.0.0/dependencies')) {
      if (options.detailFailure === 'dependencies' && detailFailureAttempts++ === 0) {
        await route.fulfill({ status: 503, json: { code: 'service_unavailable', message: 'dependencies unavailable', retryable: true, traceId } });
        return;
      }
      if (options.detailFailure === 'malformed-topology' && detailFailureAttempts++ === 0) {
        await route.fulfill({ json: {
          rootComponentId: 'aura.observatory.query_api', rootComponentVersion: '1.0.0', partial: false, snapshotAt: observedAt,
          directDependencyIds: ['aura.observatory.telemetry_storage', 17], reverseDependencyIds: [],
          nodes: [{ dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: ['1.0.0'], availability: 'observed', health: { sampleCount: 'four', latencyP50Ms: 10, latencyP95Ms: 20, errorRate: 0.25, outcomes: { succeeded: 3, failed: 1, cancelled: 0, unknown: 0 } }, partialHealth: false }],
          edges: [{ sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'aura.observatory.telemetry_storage', targetVersion: '1.0.0', targetKind: 'component', resolution: 'resolved' }],
        } });
        return;
      }
      if (options.componentTopology === 'partial') {
        await route.fulfill({ json: {
          rootComponentId: 'aura.observatory.query_api', rootComponentVersion: '1.0.0', partial: true, snapshotAt: observedAt,
          directDependencyIds: ['aura.observatory.telemetry_storage', 'missing.provider'], reverseDependencyIds: [],
          nodes: [
            { dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: [], availability: 'unobserved', health: null, partialHealth: true },
            { dependencyId: 'missing.provider', kind: 'provider', componentVersion: null, definitionState: 'missing', deployedVersions: [], availability: 'unavailable', health: null, partialHealth: true },
          ],
          edges: [
            { sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'aura.observatory.telemetry_storage', targetVersion: '1.0.0', targetKind: 'component', resolution: 'resolved' },
            { sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'missing.provider', targetVersion: null, targetKind: 'provider', resolution: 'missing_definition' },
          ],
        } });
        return;
      }
      if (options.componentTopology === 'multi-version') {
        await route.fulfill({ json: {
          rootComponentId: 'aura.observatory.query_api', rootComponentVersion: '1.0.0', partial: false, snapshotAt: observedAt,
          directDependencyIds: ['aura.observatory.telemetry_storage'], reverseDependencyIds: [],
          nodes: [
            { dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: ['1.0.0'], availability: 'observed', health: componentHealth, partialHealth: false },
            { dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', componentVersion: '2.0.0', definitionState: 'declared', deployedVersions: ['2.0.0'], availability: 'unavailable', health: { ...componentHealth, sampleCount: 9, latencyP50Ms: 40, latencyP95Ms: 88, errorRate: 0, outcomes: { succeeded: 9, failed: 0, cancelled: 0, unknown: 0 } }, partialHealth: false },
          ],
          edges: [
            { sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'aura.observatory.telemetry_storage', targetVersion: '1.0.0', targetKind: 'component', resolution: 'resolved' },
            { sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'aura.observatory.telemetry_storage', targetVersion: '2.0.0', targetKind: 'component', resolution: 'resolved' },
          ],
        } });
        return;
      }
      await route.fulfill({ json: {
        rootComponentId: 'aura.observatory.query_api', rootComponentVersion: '1.0.0', partial: false, snapshotAt: observedAt,
        directDependencyIds: ['aura.observatory.telemetry_storage', 'missing.provider'], reverseDependencyIds: [],
        nodes: [{ dependencyId: 'aura.observatory.telemetry_storage', kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: ['1.0.0'], availability: 'observed', health: componentHealth, partialHealth: false }, { dependencyId: 'missing.provider', kind: 'provider', componentVersion: null, definitionState: 'missing', deployedVersions: [], availability: 'unavailable', health: null, partialHealth: true }],
        edges: [{ sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'aura.observatory.telemetry_storage', targetVersion: '1.0.0', targetKind: 'component', resolution: 'resolved' }, { sourceId: 'aura.observatory.query_api', sourceVersion: '1.0.0', targetId: 'missing.provider', targetVersion: null, targetKind: 'provider', resolution: 'missing_definition' }],
      } });
      return;
    }
    if (path.endsWith('/components/aura.observatory.query_api/health')) {
      await route.fulfill({ json: { componentId: 'aura.observatory.query_api', summary: componentHealth, points: [{ startAt: oldObservedAt, endAt: observedAt, summary: componentHealth }] } });
      return;
    }
    if (path.endsWith('/components/aura.core.api/health')) {
      await defer('component');
      await route.fulfill({ json: { componentId: 'aura.core.api', summary: componentHealth, points: [{ startAt: oldObservedAt, endAt: observedAt, summary: componentHealth }] } });
      return;
    }
    if (path.endsWith('/deployments')) {
      if (options.partial) { await route.abort('failed'); return; }
      await route.fulfill({ json: { snapshotAt: observedAt, items: [{ deploymentId: 'dev-local', gitCommit: 'abcdef1', environment: 'test', hostId: 'fixture-host', serviceRoles: ['api', 'worker'], firstSeenAt: observedAt, lastSeenAt: observedAt }] } });
      return;
    }
    if (path.endsWith('/events')) {
      if (options.sseDisconnected) {
        await route.abort('failed');
        return;
      }
      namedObservationReconciled = true;
      const event = { schemaVersion: 1, eventId: '1c3b8f28-cf6f-48a1-9c84-5603a8e84c55', eventType: 'observation.changed', occurredAt: observedAt, resourceType: 'run', resourceId: runId, capturePolicy: 'metadata_only' };
      const healthEvent = { schemaVersion: 1, eventId: '2d4e6f80-1234-4567-89ab-cdef01234567', eventType: 'health.changed', occurredAt: observedAt, resourceType: 'system', resourceId: null, capturePolicy: 'metadata_only' };
      if (options.healthChanged) healthChanged = true;
      const healthFrame = options.healthChanged ? `id: ${healthEvent.eventId}\nevent: health.changed\ndata: ${JSON.stringify(healthEvent)}\n\n` : '';
      await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' }, body: `id: ${event.eventId}\nevent: observation.changed\ndata: ${JSON.stringify(event)}\n\n${healthFrame}` });
      return;
    }
    if (path.endsWith('/retention/actions') && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { action?: string };
      retentionRequests.push({ action: body.action ?? '', body, headers: request.headers() });
      await route.fulfill({ status: 202, json: { actionId, action: body.action, cutoff: body.action === 'delete_before' ? '2026-10-01T00:00:00.000Z' : null, status: 'queued', createdAt: observedAt, startedAt: null, finishedAt: null, deletedObservationCount: 0, deletedReceiptCount: 0, deletedRunCount: 0, failureCode: null } });
      return;
    }
    if (path.endsWith(`/retention/actions/${actionId}`)) {
      await defer('retention');
      actionPolls += 1;
      await route.fulfill({ json: { actionId, action: 'delete_before', cutoff: '2026-10-01T00:00:00.000Z', status: options.retentionFailure ? 'failed' : actionPolls > 1 ? 'succeeded' : 'running', createdAt: observedAt, startedAt: observedAt, finishedAt: actionPolls > 1 ? observedAt : null, deletedObservationCount: actionPolls > 1 ? 2 : 0, deletedReceiptCount: 0, deletedRunCount: actionPolls > 1 ? 1 : 0, failureCode: options.retentionFailure ? 'storage_failure' : null } });
      return;
    }
    await route.continue();
  });
  return {
    retentionRequests,
    logoutRequests,
    deferredKinds,
    releaseDeferred: () => { while (deferredResolvers.length) deferredResolvers.shift()?.(); },
    failCatalogueRefresh: () => { componentRefreshFailure = true; },
    restoreIdentity: () => { identityUnavailable = false; loggedOut = false; },
    expireSession: () => { expireSessionRequested = true; },
  };
}

async function openFixture(page: Page, options: FixtureOptions = {}): Promise<FixtureHandle> {
  const fixture = await installFixture(page, options);
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
  await expect(page.getByText('Aura Observatory', { exact: true })).toBeVisible();
  return fixture;
}

async function expectNoA11yViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).include('app-root').analyze();
  expect(results.violations).toEqual([]);
}

test.describe('Observatory Web mocked browser acceptance', () => {
  test('has no axe violations in the healthy shell', async ({ page }) => {
    await openFixture(page);
    await expectNoA11yViolations(page);
  });

  test('has no axe violations in the unavailable failure state', async ({ page }) => {
    await openFixture(page, { unavailable: true });
    await expect(page.getByText(/unavailable|could not load/i).first()).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test('renders an initial anonymous recovery state with a bounded sign-in return path', async ({ page }) => {
    await installFixture(page, { anonymous: true });
    await page.goto('/?status=succeeded');
    await expect(page.getByRole('heading', { name: 'Sign in to Observatory', exact: true })).toBeVisible();
    const signIn = page.getByRole('link', { name: 'Sign in as owner', exact: true });
    await expect(signIn).toHaveAttribute('href', /return_to=%2F%3Fstatus%3Dsucceeded/);
    await expect(page.getByRole('navigation', { name: 'Observatory sections' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Sign in to Observatory', exact: true })).toBeFocused();
    await expect(page.locator('main[aria-labelledby="auth-recovery-title"] [role="status"]')).toContainText('Sign in to Observatory');
  });

  test('consumes callback recovery markers and exposes only static denial copy', async ({ page }) => {
    await installFixture(page, { anonymous: true });
    await page.goto('/?auth_recovery=provider_denied&code=never-rendered&state=never-rendered');
    await expect(page.getByRole('heading', { name: 'Owner sign-in was not authorized', exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.url()).not.toContain('auth_recovery');
    await expect(page.url()).not.toContain('never-rendered');
    await expect(page.locator('body')).not.toContainText(/provider_denied|token|credential|exception/i);
    await expect(page.getByRole('heading', { name: 'Owner sign-in was not authorized', exact: true })).toBeFocused();
    await expect(page.locator('main[aria-labelledby="auth-recovery-title"] [role="status"]')).toContainText('not authorized');
    const storage = await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }));
    expect(storage.local).toEqual([]);
    expect(storage.session).toEqual([]);
  });

  test('distinguishes identity unavailability and offers a keyboard retry', async ({ page }) => {
    await installFixture(page, { identityUnavailable: true });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Owner sign-in is temporarily unavailable', exact: true })).toBeVisible();
    const retry = page.getByRole('button', { name: 'Check session again', exact: true });
    await retry.focus();
    await expect(retry).toBeFocused();
    await expect(page.getByRole('link', { name: 'Sign in as owner', exact: true })).toBeVisible();
    await expect(page.locator('main[aria-labelledby="auth-recovery-title"] [role="status"]')).toContainText('temporarily unavailable');
  });

  test('retries identity recovery into the authenticated shell without a redirect loop', async ({ page }) => {
    await installFixture(page, { identityFailures: 2 });
    await page.goto('/');
    const recoveryHeading = page.getByRole('heading', { name: 'Owner sign-in is temporarily unavailable', exact: true });
    await expect(recoveryHeading).toBeVisible();
    const retry = page.getByRole('button', { name: 'Check session again', exact: true });
    await retry.click();
    await expect(recoveryHeading).toBeFocused();
    await retry.click();
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Observatory sections' })).toBeVisible();
    await expect(page.locator('main#observatory-main')).toBeFocused();
    await expect(page.locator('header').getByRole('status')).toHaveText(/Live updates on|Disconnected|Connecting/i);
    await expect(page.getByRole('heading', { name: 'Owner sign-in is temporarily unavailable', exact: true })).toHaveCount(0);
  });

  test('moves focus and announces an in-session expiry while clearing private navigation', async ({ page }) => {
    const fixture = await openFixture(page, { expireNextRuns: true });
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    fixture.expireSession();
    await page.evaluate(() => {
      const form = document.querySelector('observatory-runs form');
      if (!(form instanceof HTMLFormElement)) throw new Error('Runs form is unavailable');
      form.requestSubmit();
    });
    const heading = page.getByRole('heading', { name: 'Your Observatory session expired', exact: true });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();
    await expect(page.locator('main[aria-labelledby="auth-recovery-title"] [role="status"]')).toContainText('session expired');
    await expect(page.getByRole('navigation', { name: 'Observatory sections' })).toHaveCount(0);
  });

  test('has no axe violations while retention confirmation is open', async ({ page }) => {
    await openFixture(page);
    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await page.getByRole('textbox', { name: 'Delete observations before (UTC)' }).fill('2026-10-01T00:00');
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Confirm destructive retention action' })).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test('shows ingest health, counts, stale, and unavailable states', async ({ page }) => {
    await openFixture(page, { stale: true });
    await expect(page.getByRole('status').filter({ hasText: /stale|disconnected/i }).first()).toBeVisible();
    await expect(page.getByText('Accepted')).toBeVisible();
    const ingest = page.locator('observatory-overview .ingest-card');
    await expect(ingest.getByText('Rejected', { exact: true })).toBeVisible();
    await expect(ingest.getByText('Duplicates', { exact: true })).toBeVisible();
    await expect(ingest.getByText('Malformed', { exact: true })).toBeVisible();
    await expect(ingest.getByText('Storage failures', { exact: true })).toBeVisible();
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await page.route('**/api/v1/overview', async (route) => route.fulfill({ status: 503, json: { code: 'service_unavailable', message: 'temporarily unavailable', retryable: true, traceId } }));
    await page.reload();
    await expect(page.getByText(/unavailable|could not load/i).first()).toBeVisible();
  });

  test('shows bounded self-monitoring health, finite rejection reasons, and an accessible history table', async ({ page }) => {
    await openFixture(page);
    await expect(page.getByRole('heading', { name: 'Ingest quality and worker health', exact: true })).toBeVisible();
    const health = page.locator('observatory-overview .health-section');
    await expect(health.getByText('Intake lag', { exact: true })).toBeVisible();
    await expect(health.getByText('Visibility watermark', { exact: true })).toBeVisible();
    await expect(page.getByText('schema validation', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Recent health snapshots', exact: true })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Recent Observatory health snapshots' })).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test('offers a keyboard retry when bounded health history is unavailable', async ({ page }) => {
    await openFixture(page, { healthUnavailable: true });
    const retry = page.getByRole('button', { name: 'Retry history', exact: true });
    await expect(retry).toBeVisible();
    await retry.focus();
    await expect(retry).toBeFocused();
    await expect(page.getByRole('alert')).toContainText('Health history is unavailable');
  });

  test('keeps an empty health history empty while the live stream is disconnected', async ({ page }) => {
    await openFixture(page, { sseDisconnected: true, emptyHealthHistory: true });
    const history = page.locator('observatory-overview .history-section');
    await expect(history.getByRole('status')).toContainText('No health snapshots');
    await expect(page.locator('header').getByRole('status')).toHaveText(/Disconnected/i);
    await expect(history.getByText(/last known health history/i)).toHaveCount(0);
  });

  test('refreshes overview and bounded history after a health.changed notification', async ({ page }) => {
    await openFixture(page, { healthChanged: true });
    await expect(page.locator('observatory-overview .state-badge').filter({ hasText: 'delayed' })).toBeVisible();
    await expect(page.locator('observatory-overview .health-section').getByText('7.2 s', { exact: true })).toBeVisible();
    await expect(page.locator('observatory-overview .history-section').getByText('7.2 s', { exact: true }).first()).toBeVisible();
  });

  test('distinguishes delayed health with explicit copy', async ({ page }) => {
    await openFixture(page, { healthState: 'delayed' });
    await expect(page.locator('observatory-overview .state-badge').filter({ hasText: 'delayed' })).toBeVisible();
    await expect(page.getByText(/freshness or backlog thresholds need attention/i)).toBeVisible();
  });

  test('distinguishes partial health with explicit copy', async ({ page }) => {
    await openFixture(page, { healthState: 'partial' });
    await expect(page.locator('observatory-overview .state-badge').filter({ hasText: 'partial' })).toBeVisible();
    await expect(page.getByText(/measurements are incomplete/i)).toBeVisible();
  });

  test('reconciles REST snapshot with SSE and marks a disconnected stream', async ({ page }) => {
    await openFixture(page, { sseDisconnected: true });
    await expect(page.locator('header').getByRole('status')).toHaveText(/Disconnected/i);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.locator('observatory-runs').getByRole('status').filter({ hasText: /data may be stale|disconnected|reconnecting/i })).toBeVisible();
    await expect(page.getByText(runId)).toBeVisible();
  });

  test('reconciles a named observation.changed SSE event beyond the initial REST snapshot', async ({ page }) => {
    await openFixture(page);
    const observations = page.locator('observatory-overview section[aria-label="System and ingest health"] article.metric-card').filter({ hasText: 'Observations' }).getByText('13', { exact: true });
    await expect(observations).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('status').filter({ hasText: /live updates|disconnected|reconnecting/i }).first()).toBeVisible();
  });

  test('filters runs and opens a partial trace with ordered parent-child spans', async ({ page }) => {
    await openFixture(page);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    const status = page.getByRole('combobox', { name: 'Status' });
    await status.selectOption('succeeded');
    await page.getByPlaceholder('component id').fill('aura.core.api');
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
    await expect(page.getByText('fixture-chat')).toBeVisible();
    await page.getByRole('button', { name: runId, exact: true }).click();
    await page.getByRole('navigation', { name: 'Observatory sections' }).getByRole('button', { name: 'Trace', exact: true }).click();
    await expect(page.getByText(/trace waterfall/i)).toBeVisible();
    const traceList = page.getByRole('list', { name: 'Ordered trace spans' });
    await expect(traceList).toHaveCount(1);
    await expect(traceList.getByText('request', { exact: true })).toBeVisible();
    await expect(traceList.getByText('job', { exact: true })).toBeVisible();
    await expect(traceList.getByText('partial / missing parent', { exact: true })).toBeVisible();
    await expect(traceList.getByRole('listitem')).toHaveCount(3);
  });

  test('shows component health summaries and a bounded trend', async ({ page }) => {
    await openFixture(page);
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(page.getByText('aura.core.api')).toBeVisible();
    await expect(page.locator('observatory-components').getByText(/p50|latency/i).first()).toBeVisible();
    await expect(page.locator('observatory-components').getByText(/errors?/i).first()).toBeVisible();
    const firstComponent = page.locator('observatory-components').getByRole('article').first();
    await expect(firstComponent.locator('dt').filter({ hasText: 'Samples' }).locator('..').locator('dd')).toHaveText('4');
    const trendValues = page.locator('observatory-components [role="img"][aria-label]');
    await expect(trendValues.first()).toBeVisible({ timeout: 10_000 });
    expect(await trendValues.evaluateAll((items) => items.every((item) => /samples at/i.test(item.getAttribute('aria-label') ?? '')))).toBe(true);
  });

  test('keeps the component catalogue structural loading state while its request is pending', async ({ page }) => {
    const fixture = await installFixture(page, { componentTopology: 'declared', deferCatalogue: true });
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const loading = page.getByRole('status', { name: 'Loading component catalogue' });
    await expect(loading).toBeVisible();
    expect(fixture.deferredKinds.has('catalogue')).toBe(true);
    await fixture.releaseDeferred();
    await expect(page.getByText('aura.observatory.query_api', { exact: true })).toBeVisible();
    await expect(loading).toHaveCount(0);
  });

  test('renders declared and observed-only catalogue entries with an accessible topology table', async ({ page }) => {
    await openFixture(page, { componentTopology: 'declared' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const components = page.locator('observatory-components');
    await expect(components.getByText('aura.observatory.query_api', { exact: true })).toBeVisible();
    const declared = components.getByRole('article', { name: 'aura.observatory.query_api' });
    await expect(declared).toContainText('Declared definition');
    await expect(components.getByText('aura.core.scheduler', { exact: true })).toBeVisible();
    await expect(components.getByText(`aura.observatory.observed_${'x'.repeat(70)}`, { exact: true })).toBeVisible();
    const observedOnly = components.getByRole('article').filter({ hasText: 'aura.core.scheduler' });
    await expect(observedOnly).toContainText('Observed only');
    await expect(observedOnly).toContainText('No approved definition is available for this observed component.');
    await expect(observedOnly).toContainText('No outcome data');
    await expect(observedOnly).not.toContainText(/Healthy|Degraded/);
    const longObservedOnly = components.getByRole('article').filter({ hasText: `aura.observatory.observed_${'x'.repeat(70)}` });
    await expect(longObservedOnly).toContainText('Observed only');

    const open = components.getByRole('button', { name: 'View definition and topology' }).first();
    await expect(open).toHaveAttribute('aria-expanded', 'false');
    await expect(open).toHaveAttribute('aria-controls', 'component-details-panel');
    await open.focus();
    await expect(open).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#component-details-title')).toBeVisible();
    await expect(open).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#component-details-title')).toBeFocused();
    const table = page.getByRole('table', { name: /Complete dependency relationships/ });
    await expect(table).toBeVisible();
    await expect(table.getByText('Missing definition', { exact: true })).toBeVisible();
    await expect(table.getByText('Unavailable', { exact: true })).toBeVisible();
    await expect(table.getByText('4 samples · 25% errors · p95 44 ms · Degraded', { exact: true })).toBeVisible();
    await expect(page.getByText('Direct dependencies', { exact: true })).toBeVisible();
    await expect(page.getByText('Reverse dependencies', { exact: true })).toBeVisible();
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await expectNoA11yViolations(page);
    await page.getByRole('button', { name: 'Close details', exact: true }).click();
    await expect(open).toBeFocused();
  });

  test('exposes partial topology and preserves keyboard-safe responsive layout', async ({ page }) => {
    await openFixture(page, { componentTopology: 'partial' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await page.getByRole('button', { name: 'View definition and topology' }).first().click();
    await expect(page.getByText('Partial health; some observations are unavailable.', { exact: true })).toBeVisible();
    const table = page.getByRole('table', { name: /Complete dependency relationships/ });
    await expect(table).toBeVisible();
    const scrollRegion = page.getByRole('region', { name: 'Scrollable dependency relationship table' });
    await page.setViewportSize({ width: 768, height: 900 });
    const beforeScroll = await scrollRegion.evaluate((element) => ({ left: element.scrollLeft, overflow: element.scrollWidth > element.clientWidth }));
    expect(beforeScroll.overflow).toBe(true);
    await scrollRegion.focus();
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => scrollRegion.evaluate((element) => element.scrollLeft)).toBeGreaterThan(beforeScroll.left);
    await page.setViewportSize({ width: 390, height: 844 });
    const layout = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, viewportWidth: window.innerWidth }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    await expectNoA11yViolations(page);
  });

  test('matches topology availability and health to each target version', async ({ page }) => {
    await openFixture(page, { componentTopology: 'multi-version' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await page.getByRole('button', { name: 'View definition and topology' }).first().click();
    const table = page.getByRole('table', { name: /Complete dependency relationships/ });
    const versionOne = table.locator('tbody tr').filter({ hasText: '2.0.0' }).first();
    const versionZero = table.locator('tbody tr').filter({ hasText: '1.0.0' }).first();
    await expect(versionZero.locator('td').nth(4)).toHaveText('Observed');
    await expect(versionZero.locator('td').nth(5)).toHaveText('4 samples · 25% errors · p95 44 ms · Degraded');
    await expect(versionOne.locator('td').nth(4)).toHaveText('Unavailable');
    await expect(versionOne.locator('td').nth(5)).toHaveText('9 samples · 0% errors · p95 88 ms · Healthy');
  });

  test('retries partial component details with Enter and announces the transition', async ({ page }) => {
    await openFixture(page, { componentTopology: 'declared', detailFailure: 'dependencies' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const opener = page.getByRole('button', { name: 'View definition and topology' }).first();
    await opener.press('Enter');
    await expect(page.getByRole('status').filter({ hasText: 'partially loaded' })).toBeVisible();
    const retry = page.getByRole('button', { name: 'Retry details', exact: true });
    const details = page.locator('#component-details-panel');
    await expect(details.getByText('observatory', { exact: true })).toBeVisible();
    await expect(details.getByText('Partial component details.', { exact: true })).toBeVisible();
    await retry.focus();
    await retry.press('Enter');
    await expect(page.getByRole('table', { name: /Complete dependency relationships/ })).toBeVisible();
    await expect(page.locator('#component-details-title')).toBeFocused();
    await expect(details.getByText('observatory', { exact: true })).toBeVisible();
    await expect(page.locator('.sr-only[role="status"]')).toContainText('loaded');
    await expectNoA11yViolations(page);
  });

  test('keeps malformed definition data in a recoverable detail state', async ({ page }) => {
    await openFixture(page, { componentTopology: 'declared', detailFailure: 'malformed' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await page.getByRole('button', { name: 'View definition and topology' }).first().press('Enter');
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.locator('#component-details-title')).toBeFocused();
    await expect(page.getByRole('button', { name: 'Retry details', exact: true })).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test('treats malformed nested topology values as partial without crashing the detail view', async ({ page }) => {
    await openFixture(page, { componentTopology: 'declared', detailFailure: 'malformed-topology' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const opener = page.getByRole('button', { name: 'View definition and topology' }).first();
    await opener.click();
    const details = page.locator('#component-details-panel');
    await expect(details.locator('#component-details-title')).toBeFocused();
    await expect(details.getByRole('alert')).toContainText('Partial component details');
    await expect(details.getByText('observatory', { exact: true })).toBeVisible();
    await details.getByRole('button', { name: 'Retry details', exact: true }).press('Enter');
    await expect(details.getByRole('table', { name: /Complete dependency relationships/ })).toBeVisible();
    await expectNoA11yViolations(page);
  });

  test('keeps catalogue empty state retryable without private persistence', async ({ page }) => {
    await openFixture(page, { componentTopology: 'empty' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(page.getByText('No component definitions or observed components are available yet.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Refresh catalogue', exact: true })).toBeVisible();

    const storage = await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage), url: location.href }));
    expect(storage.local).toEqual([]);
    expect(storage.session).toEqual([]);
    expect(storage.url).not.toMatch(/token|credential|secret|access_token/i);
  });

  test('shows an unavailable catalogue with a keyboard retry action', async ({ page }) => {
    await openFixture(page, { componentTopology: 'unavailable' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('Component catalogue is unavailable');
    await expect(page.getByRole('button', { name: 'Retry component catalogue', exact: true })).toBeVisible();
  });

  test('distinguishes stale catalogue data from a disconnected live stream', async ({ page }) => {
    const fixture = await openFixture(page, { componentTopology: 'declared', componentStale: true, sseDisconnected: true });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const components = page.locator('observatory-components');
    await expect(components.getByText('aura.observatory.query_api', { exact: true })).toBeVisible();
    fixture.failCatalogueRefresh();
    await components.getByRole('button', { name: 'Refresh catalogue', exact: true }).click();
    await expect(components.getByRole('status').filter({ hasText: /last known component catalogue/i })).toBeVisible();
    await expect(page.locator('header').getByRole('status')).toHaveText(/Disconnected/i);
    await expect(components.getByText('aura.observatory.query_api', { exact: true })).toBeVisible();
  });

  test('exposes partial-data state when one or more resource panels cannot refresh', async ({ page }) => {
    await openFixture(page, { partial: true });
    await expect(page.locator('observatory-overview').getByRole('status').filter({ hasText: /^Partial data:/ })).toBeVisible();
  });

  test('keeps section focus and active-section semantics keyboard accessible', async ({ page }) => {
    await openFixture(page);
    const sections = page.getByRole('navigation', { name: 'Observatory sections' });
    const runs = sections.getByRole('button', { name: 'Runs', exact: true });
    await runs.focus();
    await page.keyboard.press('Enter');
    await expect(runs).toBeFocused();
    await expect(runs).toHaveClass(/active/);
    await expect(runs).toHaveAttribute('aria-current', 'page');
  });

  test('does not overflow or overlap at a narrow responsive viewport', async ({ page }) => {
    await openFixture(page);
    await page.setViewportSize({ width: 390, height: 844 });
    const layout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      headerRight: document.querySelector('header')?.getBoundingClientRect().right ?? 0,
      headerBottom: document.querySelector('header')?.getBoundingClientRect().bottom ?? 0,
      tabsTop: document.querySelector('nav[aria-label="Observatory sections"]')?.getBoundingClientRect().top ?? 0,
    }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.headerRight).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.tabsTop).toBeGreaterThanOrEqual(layout.headerBottom - 1);
  });

  test('reflows long component identifiers at a deterministic 200 percent zoom', async ({ page }) => {
    await openFixture(page, { componentTopology: 'declared' });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await page.setViewportSize({ width: 640, height: 900 });
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    const longIdentifier = page.getByText(`aura.observatory.observed_${'x'.repeat(70)}`, { exact: true });
    await expect(longIdentifier).toBeVisible();
    const layout = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, viewportWidth: window.innerWidth }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
  });

  test('uses semantic dark-theme tokens and reduced-motion browser preferences', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await installFixture(page, { anonymous: true });
    await page.goto('/');
    const preferences = await page.evaluate(() => ({
      dark: matchMedia('(prefers-color-scheme: dark)').matches,
      reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
      canvas: getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim(),
      text: getComputedStyle(document.documentElement).getPropertyValue('--text').trim(),
    }));
    expect(preferences.dark).toBe(true);
    expect(preferences.reduced).toBe(true);
    expect(preferences.canvas).toBe('#191517');
    expect(preferences.text).toBe('#f6eef1');
  });

  test('uses keyboard-accessible retention confirmation, progress, failure, and refreshes after completion', async ({ page }) => {
    const fixture = await openFixture(page);
    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Delete stored observations', exact: true })).toBeVisible();
    const cutoff = page.getByRole('textbox', { name: 'Delete observations before (UTC)' });
    await cutoff.fill('2026-10-01T00:00');
    const deleteButton = page.getByRole('button', { name: 'Delete before cutoff', exact: true });
    await deleteButton.focus();
    await page.keyboard.press('Enter');
    const confirmation = page.getByRole('dialog', { name: 'Confirm destructive retention action' });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('button', { name: 'Confirm delete-before', exact: true }).click();
    await expect.poll(() => fixture.retentionRequests.length).toBe(1);
    expect(fixture.retentionRequests[0]?.action).toBe('delete_before');
    expect(fixture.retentionRequests[0]?.body).toMatchObject({ action: 'delete_before', cutoff: '2026-10-01T00:00:00.000Z' });
    expect(fixture.retentionRequests[0]?.headers['x-observatory-csrf']).toBe(fixtureCsrfToken);
    expect(fixture.retentionRequests[0]?.headers['idempotency-key']).toBeTruthy();
    expect(fixture.retentionRequests[0]?.headers['x-observatory-confirmation']).toBe('delete-before');
    const retentionStatus = page.locator('observatory-retention .result div').filter({ hasText: /Status.*succeeded/i }).getByRole('definition');
    await expect(retentionStatus).toHaveText('succeeded');

    const purge = page.getByRole('button', { name: 'Purge all observations', exact: true });
    await purge.focus();
    await page.keyboard.press('Enter');
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('button', { name: 'Confirm purge-all', exact: true }).click();
    await expect.poll(() => fixture.retentionRequests.length).toBe(2);
    expect(fixture.retentionRequests[1]?.headers['x-observatory-confirmation']).toBe('purge-all');
  });

  test('surfaces a retention failure without leaving destructive controls locked', async ({ page }) => {
    const fixture = await openFixture(page, { retentionFailure: true });
    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await page.getByRole('textbox', { name: 'Delete observations before (UTC)' }).fill('2026-10-01T00:00');
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: 'Confirm destructive retention action' });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('button', { name: 'Confirm delete-before', exact: true }).click();
    await expect.poll(() => fixture.retentionRequests.length).toBe(1);
    await expect(page.locator('observatory-retention .result div').filter({ hasText: /Status.*failed/i }).getByRole('definition')).toHaveText('failed');
    await expect(page.locator('observatory-retention').getByText(/storage_(unavailable|failure)/i)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Purge all observations', exact: true })).toBeEnabled();
  });

  test.describe('UTC retention boundary', () => {
    test.use({ timezoneId: 'America/Los_Angeles' });

    test('serializes a datetime-local cutoff as the exact UTC boundary', async ({ page }) => {
      const fixture = await openFixture(page);
      await page.getByRole('button', { name: 'Retention', exact: true }).click();
      await page.getByRole('textbox', { name: 'Delete observations before (UTC)' }).fill('2026-10-01T12:34');
      await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
      const confirmation = page.getByRole('dialog', { name: 'Confirm destructive retention action' });
      await expect(confirmation).toBeVisible();
      await confirmation.getByRole('button', { name: 'Confirm delete-before', exact: true }).click();
      await expect.poll(() => fixture.retentionRequests.length).toBe(1);
      expect(fixture.retentionRequests[0]?.body).toMatchObject({ action: 'delete_before', cutoff: '2026-10-01T12:34:00.000Z' });
    });
  });

  test('does not persist authenticated API or SSE data in browser storage, caches, or URLs', async ({ page }) => {
    await openFixture(page);
    const storage = await page.evaluate(async () => {
      const cacheKeys = 'caches' in window ? await caches.keys() : [];
      const indexedDbDatabases = 'indexedDB' in window && 'databases' in indexedDB ? await indexedDB.databases() : [];
      return {
        localStorage: Object.keys(localStorage),
        sessionStorage: Object.keys(sessionStorage),
        cacheKeys,
        indexedDbNames: indexedDbDatabases.map((database) => database.name ?? ''),
        url: location.href,
      };
    });
    expect(storage.localStorage.join(' ')).not.toMatch(/run|trace|component|deployment|retention|api|sse/i);
    expect(storage.sessionStorage.join(' ')).not.toMatch(/run|trace|component|deployment|retention|api|sse/i);
    expect(storage.cacheKeys.join(' ')).not.toMatch(/api|observatory|sse/i);
    expect(storage.indexedDbNames.join(' ')).not.toMatch(/api|observatory|sse/i);
    expect(storage.url).not.toMatch(/token|credential|secret|access_token/i);
  });

  test('does not claim logout after a 403 and retries the same logout operation', async ({ page }) => {
    const fixture = await openFixture(page, { logoutStatuses: [403, 204] });
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Sign-out could not be confirmed', exact: true })).toBeVisible();
    await expect(page.locator('main.signed-out > p').filter({ hasText: /session may still be active/i })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Try sign-out again', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Sign in as owner', exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Observatory sections' })).toHaveCount(0);
    await expect.poll(() => fixture.logoutRequests.length).toBe(1);
    await page.getByRole('button', { name: 'Try sign-out again', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'You are signed out of Observatory', exact: true })).toBeVisible();
    await expect.poll(() => fixture.logoutRequests.length).toBe(2);
  });

  test('does not claim logout after a 503 and keeps the retry available', async ({ page }) => {
    const fixture = await openFixture(page, { logoutStatuses: [503] });
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Sign-out could not be confirmed', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Try sign-out again', exact: true })).toBeEnabled();
    await expect.poll(() => fixture.logoutRequests.length).toBe(1);
  });

  test('treats a logout 401 as already absent and offers no retry', async ({ page }) => {
    const fixture = await openFixture(page, { logoutStatuses: [401, 204], csrfTokens: ['fixture-csrf-first-0123456789-abcdefghij', 'fixture-csrf-second-0123456789-abcdefghij'] });
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'You are signed out of Observatory', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /retry|try sign-out again/i })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Sign in as owner', exact: true })).toBeVisible();
    await expect.poll(() => fixture.logoutRequests.length).toBe(1);
    fixture.restoreIdentity();
    await page.reload();
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'You are signed out of Observatory', exact: true })).toBeVisible();
    await expect.poll(() => fixture.logoutRequests.length).toBe(2);
    expect(fixture.logoutRequests[0]?.headers['x-observatory-csrf']).not.toBe(fixture.logoutRequests[1]?.headers['x-observatory-csrf']);
  });

  test('logs out with CSRF and prevents deferred authenticated data from repopulating state', async ({ page }) => {
    const fixture = await openFixture(page, { deferAfterLogout: true });
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await page.getByRole('button', { name: runId, exact: true }).click();
    await expect.poll(() => fixture.deferredKinds.has('run')).toBe(true);

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await page.getByRole('textbox', { name: 'Delete observations before (UTC)' }).fill('2026-10-01T00:00');
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: 'Confirm destructive retention action' });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole('button', { name: 'Confirm delete-before', exact: true }).click();
    await expect.poll(() => fixture.deferredKinds.has('retention')).toBe(true);

    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'You are signed out of Observatory', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Sign in as owner', exact: true })).toBeVisible();
    await expect.poll(() => fixture.logoutRequests.length).toBe(1);
    expect(fixture.logoutRequests[0]?.headers['x-observatory-csrf']).toBe(fixtureCsrfToken);
    await expect(page.getByRole('navigation', { name: 'Observatory sections' })).toHaveCount(0);

    fixture.releaseDeferred();
    await expect(page.getByText(runId)).toHaveCount(0);
    await expect(page.getByText('aura.core.api')).toHaveCount(0);
    await expect(page.getByText(/Deletion is running|Deleted observations/i)).toHaveCount(0);
  });
});
