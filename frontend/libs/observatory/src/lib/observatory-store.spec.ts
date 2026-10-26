import { describe, expect, it } from 'vitest';
import type { Overview } from '@aura/observatory-api-client';
import { extractSessionCsrf } from './observatory-session';
import { authCopyForState, authStateForRecoveryMarker, boundedReturnPath, DEFAULT_ANALYTICS_FILTERS, needsRunReconciliation, needsSelectedRunReconciliation, ownerLoginHref } from './observatory-models';
import { ComponentDetailRequestGate, componentAvailabilityLabel, componentCatalogueViewFor, datetimeLocalToUtc, dependencyNodeForEdge, hasSessionRequiredRejection, healthHistoryAfterDisconnect, inspectionEvidenceIsCurrent, isApprovedRunEvidenceMedia, isComparisonInvalidationEvent, isCurrentCapturedRunEvidenceManifest, isCurrentLoadSequence, isOverviewStale, LiveRefreshCoordinator, matchesSelectedRunInvalidation, normalizeRunFilters, parseAnalyticsMetricCatalogue, parseComponentDefinition, parseComponentDependencyTopology, parseOperationalAnalyticsSnapshot, RequestGenerationGate, resourceStateFor, SELECTED_RUN_LOAD_TIMEOUT_MS, selectedRunLoadError } from './observatory-models';

describe('observatory metadata boundaries', () => {
  it('fails closed for evidence size, media type, and classification mismatches', () => {
    const manifest = { mediaClassification: 'structured', mediaType: 'application/json', byteCount: 12 } as const;
    expect(isApprovedRunEvidenceMedia(manifest as never, 12, 'application/json')).toBe(true);
    expect(isApprovedRunEvidenceMedia(manifest as never, 13, 'application/json')).toBe(false);
    expect(isApprovedRunEvidenceMedia(manifest as never, 12, 'text/html')).toBe(false);
    expect(isApprovedRunEvidenceMedia({ ...manifest, mediaType: 'image/png' } as never, 12, 'image/png')).toBe(false);
    expect(isApprovedRunEvidenceMedia({ ...manifest, byteCount: 1_048_577 } as never, 1_048_577, 'application/json')).toBe(false);
  });

  it('invalidates disclosed evidence when a live inspection changes captured to deleted', () => {
    const disclosed = { evidenceId: 'evidence-1', captureState: 'captured', mediaClassification: 'structured', mediaType: 'application/json', digest: 'sha256:one', byteCount: 12 } as const;
    expect(isCurrentCapturedRunEvidenceManifest(disclosed as never, disclosed as never)).toBe(true);
    expect(isCurrentCapturedRunEvidenceManifest({ ...disclosed, captureState: 'deleted' } as never, disclosed as never)).toBe(false);
    expect(isCurrentCapturedRunEvidenceManifest({ ...disclosed, digest: 'sha256:two' } as never, disclosed as never)).toBe(false);
    expect(isCurrentCapturedRunEvidenceManifest(null, disclosed as never)).toBe(false);
  });

  it('reconciles a fulfilled deleted inspection even when detail is timed out', () => {
    const disclosed = { evidenceId: 'evidence-1', captureState: 'captured', mediaClassification: 'structured', mediaType: 'application/json', digest: 'sha256:one', byteCount: 12 } as const;
    const detailResult: PromiseSettledResult<unknown> = { status: 'rejected', reason: new Error('timed out') };
    const inspectionResult: PromiseSettledResult<never> = { status: 'fulfilled', value: { steps: [{ evidence: [{ ...disclosed, captureState: 'deleted' }] }] } as never };
    expect(detailResult.status).toBe('rejected');
    expect(inspectionResult.status === 'fulfilled' && inspectionEvidenceIsCurrent(inspectionResult.value, disclosed as never)).toBe(false);
  });

  it('starts analytics with a bounded metadata-only query shape', () => {
    expect(DEFAULT_ANALYTICS_FILTERS).toMatchObject({ focus: 'all', metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', bucket: 'auto', groupBy: 'component' });
    expect(DEFAULT_ANALYTICS_FILTERS).not.toHaveProperty('prompt');
    expect(DEFAULT_ANALYTICS_FILTERS).not.toHaveProperty('response');
  });

  it('rejects malformed analytics snapshots before template access', () => {
    expect(parseOperationalAnalyticsSnapshot({ snapshotAt: '2026-10-17T00:00:00Z', capturePolicy: 'metadata_only', series: [{ seriesKey: 'broken', points: 'not-an-array' }] })).toBeNull();
    expect(parseOperationalAnalyticsSnapshot({ snapshotAt: '2026-10-17T00:00:00Z', capturePolicy: 'metadata_only', series: [], breakdowns: [], query: {}, freshness: {}, partial: false, partialReasons: [], unavailableSignals: [], scan: {} })).toBeNull();
  });

  it('accepts only closed analytics enums and rejects private render-bound markers', () => {
    const snapshot = {
      snapshotAt: '2026-10-17T00:00:00.000Z', percentileMethod: 'clickhouse_quantile_exact', capturePolicy: 'metadata_only',
      query: { metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', focus: 'all', from: '2026-10-16T00:00:00.000Z', to: '2026-10-17T00:00:00.000Z', bucket: 'one_hour', bucketSeconds: 3600, groupBy: 'component', filters: {}, seriesLimit: 12, pointLimit: 200 },
      series: [{ seriesKey: 'operation-api', facets: { component: 'aura.core.api' }, summary: { sampleCount: 1, minimum: 1, maximum: 1, average: 1, p50: 1, p90: 1, p95: 1, p99: 1, ratePerInterval: null, rateIntervalSeconds: null }, points: [{ startAt: '2026-10-16T00:00:00.000Z', endAt: '2026-10-16T01:00:00.000Z', value: 1, sampleCount: 1, availability: 'available', partial: false }], availability: 'available', partial: false }],
      breakdowns: [], freshness: { latestObservedAt: '2026-10-16T01:00:00.000Z', lagMs: 10, state: 'fresh' }, partial: false, partialReasons: [], unavailableSignals: [], scan: { scannedRows: 1, scanLimit: 1000000, executionMs: 1.5, executionLimitMs: 5000 },
    };
    expect(parseOperationalAnalyticsSnapshot(snapshot)).not.toBeNull();
    expect(parseOperationalAnalyticsSnapshot({ ...snapshot, query: { ...snapshot.query, metric: 'arbitrary_metric' } })).toBeNull();
    expect(parseOperationalAnalyticsSnapshot({ ...snapshot, series: [{ ...snapshot.series[0], facets: { provider: 'PRIVATE CONTENT VALUE' } }] })).toBeNull();
  });

  it('validates the bounded metric catalogue before creating controls', () => {
    const catalogue = { snapshotAt: '2026-10-17T00:00:00.000Z', capturePolicy: 'metadata_only', limits: { defaultWindowHours: 24, maximumWindowDays: 31, maximumPointsPerSeries: 200, maximumSeries: 12, maximumBreakdowns: 50, maximumScannedRows: 1000000, executionTimeoutMs: 5000 }, items: [{ metric: 'operation_duration_ms', unit: 'milliseconds', focusAreas: ['all'], supportedAggregations: ['average', 'p95'], supportedGroupings: ['none', 'component'], availability: 'available', unavailableSignals: [] }] };
    expect(parseAnalyticsMetricCatalogue(catalogue)).not.toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: [{ ...catalogue.items[0], metric: 'document_retrieval_duration_ms' }] })).not.toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: [{ ...catalogue.items[0], metric: 'evaluation_runner_nats_recovery_count' }] })).not.toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: Array.from({ length: 128 }, () => catalogue.items[0]) })).not.toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: Array.from({ length: 129 }, () => catalogue.items[0]) })).toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: [{ ...catalogue.items[0], supportedAggregations: ['arbitrary'] }] })).toBeNull();
    expect(parseAnalyticsMetricCatalogue({ ...catalogue, items: [{ ...catalogue.items[0], metric: 'PRIVATE_PROMPT_MARKER' }] })).toBeNull();
  });
  it('promotes a 401 rejection from any parallel resource before partial assembly', () => {
    const parallelResources = ['overview', 'runs', 'components', 'deployments'];
    for (const resource of parallelResources) {
      const results = parallelResources.map((candidate) => candidate === resource
        ? { status: 'rejected' as const, reason: { kind: 'session-required' } }
        : { status: 'fulfilled' as const, value: { resource } });
      expect(hasSessionRequiredRejection(results, (reason) => (reason as { kind?: string }).kind === 'session-required')).toBe(true);
    }
    expect(hasSessionRequiredRejection([{ status: 'rejected', reason: new Error('query failed') }], () => false)).toBe(false);
  });

  it('promotes an inspection-only session expiry to the global purge path', () => {
    const results: ReadonlyArray<PromiseSettledResult<unknown>> = [
      { status: 'fulfilled', value: { traceId: null } },
      { status: 'rejected', reason: { kind: 'session-required' } },
    ];
    expect(hasSessionRequiredRejection(results, (reason) => (reason as { kind?: string }).kind === 'session-required')).toBe(true);
  });

  it('ignores a late session rejection from a stale load sequence', () => {
    expect(isCurrentLoadSequence(3, 4)).toBe(false);
    expect(isCurrentLoadSequence(4, 4)).toBe(true);
  });

  it('coalesces a high-frequency SSE burst and guarantees a trailing snapshot refresh (AURA-0116)', async () => {
    const coordinator = new LiveRefreshCoordinator();
    let serverVersion = 0;
    let visibleVersion = 0;
    let runCount = 0;
    let releaseFirst!: () => void;
    let releaseTrailing!: () => void;
    const first = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const trailing = new Promise<void>((resolve) => { releaseTrailing = resolve; });
    const refresh = async (): Promise<void> => {
      const request = runCount++;
      await (request === 0 ? first : trailing);
      visibleVersion = serverVersion;
    };

    coordinator.request(refresh);
    serverVersion = 1;
    for (let index = 0; index < 100; index += 1) coordinator.request(refresh);
    expect(runCount).toBe(1);
    expect(coordinator.isRunning).toBe(true);
    expect(coordinator.hasPending).toBe(true);

    releaseFirst();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    serverVersion = 2;
    expect(runCount).toBe(2);
    expect(coordinator.isRunning).toBe(true);
    releaseTrailing();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(visibleVersion).toBe(2);
    expect(runCount).toBe(2);
    expect(coordinator.isRunning).toBe(false);
    expect(coordinator.hasPending).toBe(false);
  });

  it('invalidates comparisons only for metadata-bearing live events', () => {
    expect(isComparisonInvalidationEvent('observation.changed')).toBe(true);
    expect(isComparisonInvalidationEvent('retention.changed')).toBe(true);
    expect(isComparisonInvalidationEvent('snapshot')).toBe(false);
    expect(isComparisonInvalidationEvent('health.changed')).toBe(false);
    expect(isComparisonInvalidationEvent('heartbeat')).toBe(false);
  });

  it('refreshes a selected run only for a matching run-scoped invalidation', () => {
    expect(matchesSelectedRunInvalidation('observation.changed', 'run', 'run-1', 'run-1')).toBe(true);
    expect(matchesSelectedRunInvalidation('observation.changed', 'run', 'run-2', 'run-1')).toBe(false);
    expect(matchesSelectedRunInvalidation('snapshot', 'run', 'run-1', 'run-1')).toBe(false);
    expect(matchesSelectedRunInvalidation('retention.changed', 'component', 'run-1', 'run-1')).toBe(false);
  });

  it('bounds selected-run loading with truthful delayed recovery copy', () => {
    expect(SELECTED_RUN_LOAD_TIMEOUT_MS).toBe(10_000);
    expect(selectedRunLoadError(true)).toMatch(/did not respond within 10 seconds.*Retry/);
    expect(selectedRunLoadError(false)).toBe('Run detail is unavailable.');
  });

  it('publishes live runs without waiting for an unresolved components health request', async () => {
    const coordinator = new LiveRefreshCoordinator();
    let visibleRunId: string | null = null;
    let releaseRuns!: () => void;
    const runsReady = new Promise<void>((resolve) => { releaseRuns = resolve; });
    const componentsNeverResolve = new Promise<void>(() => undefined);
    const liveRefresh = async (): Promise<void> => {
      const [overview, runs] = await Promise.all([
        Promise.resolve({ observedAt: '2026-10-16T19:00:00Z' }),
        runsReady.then(() => ({ items: [{ runId: 'run-live-1' }] })),
      ]);
      void overview;
      visibleRunId = runs.items[0]?.runId ?? null;
    };

    coordinator.request(liveRefresh);
    const blockedFullSnapshot = Promise.all([componentsNeverResolve, Promise.resolve({ items: [] })]);
    releaseRuns();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(visibleRunId).toBe('run-live-1');
    expect(coordinator.isRunning).toBe(false);
    void blockedFullSnapshot;
  });

  it('requests bounded run reconciliation when overview leads the catalogue', () => {
    const overview = { runCount: 2 } as Parameters<typeof needsRunReconciliation>[0];
    const catalogue = { items: [{ runId: 'run-1' }] } as Parameters<typeof needsRunReconciliation>[1];
    expect(needsRunReconciliation(overview, catalogue)).toBe(true);
    expect(needsRunReconciliation({ runCount: 1 } as typeof overview, catalogue)).toBe(false);
    expect(needsRunReconciliation(overview, null)).toBe(false);
  });

  it('keeps a successful detail response pending until its trace materializes', () => {
    const detail = { traceId: null } as Parameters<typeof needsSelectedRunReconciliation>[0];
    const trace = { traceId: '0123456789abcdef0123456789abcdef', spans: [] } as Parameters<typeof needsSelectedRunReconciliation>[1];
    expect(needsSelectedRunReconciliation(detail, null)).toBe(true);
    expect(needsSelectedRunReconciliation({ traceId: '0123456789abcdef0123456789abcdef' } as typeof detail, trace)).toBe(false);
    expect(needsSelectedRunReconciliation(null, null)).toBe(false);
  });

  it('connects the live surface when initial catalog loading remains unresolved', async () => {
    const componentsNeverResolve = new Promise<void>(() => undefined);
    let overviewAndRunsReady = false;
    let liveConnected = false;
    const bootstrap = async (): Promise<void> => {
      await Promise.all([Promise.resolve({ observedAt: '2026-10-16T19:00:00Z' }), Promise.resolve({ items: [{ runId: 'run-live-1' }] })]);
      overviewAndRunsReady = true;
      liveConnected = true;
      void componentsNeverResolve;
    };

    await bootstrap();

    expect(overviewAndRunsReady).toBe(true);
    expect(liveConnected).toBe(true);
  });

  it('refreshes the catalog once on Components entry without coupling it to live events', async () => {
    const coordinator = new LiveRefreshCoordinator();
    let catalogRuns = 0;
    let releaseInitial!: () => void;
    let releaseEntry!: () => void;
    const initial = new Promise<void>((resolve) => { releaseInitial = resolve; });
    const entry = new Promise<void>((resolve) => { releaseEntry = resolve; });
    const refreshCatalog = async (): Promise<void> => {
      const run = catalogRuns++;
      await (run === 0 ? initial : entry);
    };

    coordinator.request(refreshCatalog);
    // A burst of observation events does not call this catalog coordinator.
    coordinator.request(refreshCatalog);
    expect(catalogRuns).toBe(1);
    expect(coordinator.hasPending).toBe(true);

    releaseInitial();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(catalogRuns).toBe(2);
    releaseEntry();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(catalogRuns).toBe(2);
    expect(coordinator.isRunning).toBe(false);
  });

  it('models only operational fields in overview fixtures', () => {
    const overview: Overview = { observedAt: '2026-01-01T00:00:00Z', systemState: 'healthy', observationCount: 2, runCount: 1, latestObservationId: 'obs-1', ingest: { state: 'healthy', counts: { accepted: 2, rejected: 0, duplicate: 0, malformed: 0, storageFailure: 0 }, lastAcceptedAt: null, lastFailureAt: null } };
    expect(overview).not.toHaveProperty('prompt');
    expect(overview.ingest.counts.storageFailure).toBe(0);
  });

  it('accepts the server session CSRF token without persisting it', () => {
    const csrfToken = 'fixture-csrf-token-0123456789-abcdefghij';
    const token = extractSessionCsrf({ subject: 'owner', displayName: 'Owner', csrfToken });
    expect(token).toBe(csrfToken);
    expect(extractSessionCsrf({ csrfToken: 'too-short' })).toBeNull();
    expect(extractSessionCsrf({ csrfToken: '' })).toBeNull();
  });

  it('derives the owner login URL from the generated operation', () => {
    expect(ownerLoginHref()).toBe('/api/v1/auth/login?return_to=%2F');
    expect(ownerLoginHref('', '/runs?status=succeeded')).toBe('/api/v1/auth/login?return_to=%2Fruns%3Fstatus%3Dsucceeded');
    expect(ownerLoginHref('', '/runs?access_token=secret&status=succeeded')).toBe('/api/v1/auth/login?return_to=%2Fruns%3Fstatus%3Dsucceeded');
    const sensitiveKeys = ['authorization_endpoint', 'client_id', 'content', 'error_description', 'provider', 'request_uri', 'token_endpoint', 'oauth_state', 'x-secret'];
    const sensitivePath = `/runs?selected=trace&status=succeeded&component=api&model=small&deployment=local&filter=recent&from=2026-01-01&to=2026-01-02&${sensitiveKeys.map((key) => `${key}=PRIVATE_VALUE`).join('&')}#fragment`;
    const loginHref = ownerLoginHref('', sensitivePath);
    expect(loginHref).toContain('return_to=%2Fruns%3Fselected%3Dtrace%26status%3Dsucceeded%26component%3Dapi%26model%3Dsmall%26deployment%3Dlocal%26filter%3Drecent%26from%3D2026-01-01%26to%3D2026-01-02');
    expect(loginHref).not.toContain('PRIVATE_VALUE');
    expect(loginHref).not.toMatch(/authorization|client_id|content|error_description|provider|request_uri|token_endpoint|oauth_state|fragment/i);
    expect(ownerLoginHref('', `/${'a'.repeat(2047)}`)).toContain('return_to=%2F' + 'a'.repeat(2047));
    expect(ownerLoginHref('', `/${'a'.repeat(2048)}`)).toBe('/api/v1/auth/login?return_to=%2F');
    expect(boundedReturnPath('https://unsafe.example/')).toBe('/');
    expect(boundedReturnPath('//unsafe.example/')).toBe('/');
  });

  it('maps only finite callback markers to content-free recovery states', () => {
    expect(authStateForRecoveryMarker('provider_denied')).toBe('callback_denied');
    expect(authStateForRecoveryMarker('state_invalid')).toBe('callback_failed');
    expect(authStateForRecoveryMarker('provider_error_details')).toBeNull();
    expect(authCopyForState('initial').title).toBe('Sign in to Observatory');
    expect(authCopyForState('expired').message).not.toMatch(/cookie|provider|token|subject/i);
    expect(authCopyForState('callback_denied').title).toBe('Owner sign-in was not authorized');
    expect(authCopyForState('identity_unavailable')).toMatchObject({ action: 'Check session again', retryAction: 'session' });
    expect(authCopyForState('logout_unconfirmed')).toMatchObject({ action: 'Try sign-out again', retryAction: 'logout' });
  });

  it('keeps a datetime-local cutoff in UTC instead of shifting it to browser local time', () => {
    expect(datetimeLocalToUtc('2026-10-01T00:00')).toBe('2026-10-01T00:00:00.000Z');
    expect(datetimeLocalToUtc('not-a-date')).toBeNull();
  });

  it('marks an old server snapshot stale even when the request succeeded', () => {
    expect(isOverviewStale('2026-10-15T08:00:00.000Z', Date.parse('2026-10-15T09:00:00.000Z'))).toBe(true);
    expect(isOverviewStale('2026-10-15T08:58:00.000Z', Date.parse('2026-10-15T09:00:00.000Z'))).toBe(false);
  });

  it('normalizes run time filters to UTC API instants', () => {
    expect(normalizeRunFilters({ from: '2026-10-01T00:00', to: '2026-10-01T01:30', model: 'fixture-chat' })).toEqual({ from: '2026-10-01T00:00:00.000Z', to: '2026-10-01T01:30:00.000Z', model: 'fixture-chat' });
  });

  it('distinguishes an independently failed resource from an empty resource', () => {
    expect(resourceStateFor('runs', null, true)).toBe('error');
    expect(resourceStateFor('runs', { items: [] }, false)).toBe('empty');
    expect(resourceStateFor('components', { items: [{ componentId: 'api' }] }, true)).toBe('stale');
  });

  it('keeps cached component data visible while a stale catalogue has recovery copy', () => {
    expect(componentCatalogueViewFor('stale', 1)).toBe('data');
    expect(componentCatalogueViewFor('stale', 0)).toBe('empty');
    expect(componentCatalogueViewFor('ready', 1)).toBe('data');
  });

  it('maps bounded dependency availability enums to readable labels', () => {
    expect(componentAvailabilityLabel('observed')).toBe('Observed');
    expect(componentAvailabilityLabel('unobserved')).toBe('Unobserved');
    expect(componentAvailabilityLabel('unavailable')).toBe('Unavailable');
    expect(componentAvailabilityLabel('unexpected')).toBe('Unavailable');
  });

  it('matches dependency topology nodes by id and exact nullable version', () => {
    const nodes = [
      { dependencyId: 'storage', componentVersion: '1.0.0', availability: 'observed' },
      { dependencyId: 'storage', componentVersion: '2.0.0', availability: 'unavailable' },
      { dependencyId: 'provider', componentVersion: null, availability: 'unobserved' },
    ] as const;
    expect(dependencyNodeForEdge(nodes, 'storage', '1.0.0')?.availability).toBe('observed');
    expect(dependencyNodeForEdge(nodes, 'storage', '2.0.0')?.availability).toBe('unavailable');
    expect(dependencyNodeForEdge(nodes, 'provider', null)?.availability).toBe('unobserved');
    expect(dependencyNodeForEdge(nodes, 'storage', null)).toBeUndefined();
  });

  it('deduplicates health reads and invalidates pre-retention completions', () => {
    const gate = new RequestGenerationGate();
    const first = gate.begin('aura.core.api');
    expect(first).not.toBeNull();
    expect(gate.begin('aura.core.api')).toBeNull();
    gate.invalidate();
    const afterRetention = gate.begin('aura.core.api');
    expect(afterRetention).not.toBeNull();
    expect(gate.isCurrent(first!)).toBe(false);
    gate.finish(first!);
    expect(gate.isCurrent(afterRetention!)).toBe(true);
    gate.finish(afterRetention!);
  });

  it('invalidates superseded component detail requests after close and reset', () => {
    const gate = new ComponentDetailRequestGate();
    const first = gate.begin('component-a', '1.0.0');
    expect(gate.isCurrent(first)).toBe(true);
    const second = gate.begin('component-b', '2.0.0');
    expect(gate.isCurrent(first)).toBe(false);
    expect(gate.isCurrent(second)).toBe(true);
    gate.invalidate();
    expect(gate.isCurrent(second)).toBe(false);
    const afterReset = gate.begin('component-a', '1.0.0');
    expect(gate.isCurrent(afterReset)).toBe(true);
  });

  it('rejects malformed component definitions without exposing arbitrary fields', () => {
    const definition = { componentId: 'component', componentVersion: '1.0.0', owner: null, category: 'api', inputSchemaId: 'in', outputSchemaId: 'out', measurements: ['duration'], evaluators: [] };
    expect(parseComponentDefinition(definition)).toBeNull();
    expect(parseComponentDefinition({ ...definition, owner: 'observatory', measurements: undefined })).toBeNull();

    const validDefinition = { ...definition, owner: 'observatory', dependencies: [] };
    const malformedDefinitions: unknown[] = [
      { ...validDefinition, measurements: ['duration', { name: 'private' }] },
      { ...validDefinition, evaluators: 'not-an-array' },
      { ...validDefinition, dependencies: [{ dependencyId: 'dependency', kind: 'unexpected', requiredVersion: null }] },
      { ...validDefinition, dependencies: [{ dependencyId: 'dependency', kind: 'component', requiredVersion: ['1.0.0'] }] },
    ];
    for (const malformed of malformedDefinitions) expect(parseComponentDefinition(malformed)).toBeNull();
  });

  it('rejects malformed and unbounded dependency topology responses', () => {
    const topology = { rootComponentId: 'component', rootComponentVersion: '1.0.0', partial: false, snapshotAt: '2026-10-16T00:00:00Z', directDependencyIds: [], reverseDependencyIds: [], nodes: [], edges: [] };
    expect(parseComponentDependencyTopology({ ...topology, nodes: Array.from({ length: 101 }, (_, index) => ({ dependencyId: `node-${index}`, kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: [], availability: 'unobserved', health: null, partialHealth: false })) })).toBeNull();
    expect(parseComponentDependencyTopology({ ...topology, directDependencyIds: [null] })).toBeNull();

    const validHealth = { sampleCount: 2, latencyP50Ms: 10, latencyP95Ms: 20, errorRate: 0.5, outcomes: { succeeded: 1, failed: 1, cancelled: 0, unknown: 0 } };
    const validNode = { dependencyId: 'dependency', kind: 'component', componentVersion: '1.0.0', definitionState: 'declared', deployedVersions: ['1.0.0'], availability: 'observed', health: validHealth, partialHealth: false };
    const validEdge = { sourceId: 'component', sourceVersion: '1.0.0', targetId: 'dependency', targetVersion: '1.0.0', targetKind: 'component', resolution: 'resolved' };
    const validTopology = { ...topology, directDependencyIds: ['dependency'], nodes: [validNode], edges: [validEdge] };
    const malformedTopologies: unknown[] = [
      { ...validTopology, nodes: [{ ...validNode, health: 'unavailable' }] },
      { ...validTopology, nodes: [{ ...validNode, health: { ...validHealth, outcomes: [] } }] },
      { ...validTopology, nodes: [{ ...validNode, health: { ...validHealth, sampleCount: '2' } }] },
      { ...validTopology, nodes: [{ ...validNode, definitionState: 'declared-ish' }] },
      { ...validTopology, nodes: [{ ...validNode, deployedVersions: '1.0.0' }] },
      { ...validTopology, edges: [{ ...validEdge, resolution: 'unknown' }] },
      { ...validTopology, edges: [{ ...validEdge, targetKind: ['component'] }] },
    ];
    for (const malformed of malformedTopologies) expect(parseComponentDependencyTopology(malformed)).toBeNull();
  });

  it('cancels a pending live refresh without starting a trailing request', async () => {
    const coordinator = new LiveRefreshCoordinator();
    let runs = 0;
    let release!: () => void;
    const first = new Promise<void>((resolve) => { release = resolve; });
    const refresh = async (): Promise<void> => { runs += 1; await first; };

    coordinator.request(refresh);
    coordinator.request(refresh);
    expect(coordinator.hasPending).toBe(true);
    coordinator.cancel();
    expect(coordinator.hasPending).toBe(false);
    release();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(runs).toBe(1);
    expect(coordinator.isRunning).toBe(false);
  });

  it('keeps an empty health history empty while the live stream is disconnected', () => {
    const empty = { page: { items: [], nextCursor: null, snapshotAt: '2026-10-16T19:00:00Z' }, state: 'empty' as const, error: null };
    expect(healthHistoryAfterDisconnect(empty)).toEqual(empty);
    const populated = { page: { items: [{ snapshotId: 'snapshot-1' }], nextCursor: null, snapshotAt: '2026-10-16T19:00:00Z' }, state: 'ready' as const, error: null };
    expect(healthHistoryAfterDisconnect(populated)).toMatchObject({ state: 'stale', error: 'Live updates are disconnected. Showing the last known health history.' });
  });

});
