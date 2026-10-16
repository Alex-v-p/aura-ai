import { describe, expect, it } from 'vitest';
import type { Overview } from '@aura/observatory-api-client';
import { extractSessionCsrf } from './observatory-session';
import { authCopyForState, authStateForRecoveryMarker, boundedReturnPath, ownerLoginHref } from './observatory-models';
import { datetimeLocalToUtc, hasSessionRequiredRejection, healthHistoryAfterDisconnect, isCurrentLoadSequence, isOverviewStale, LiveRefreshCoordinator, normalizeRunFilters, RequestGenerationGate, resourceStateFor } from './observatory-models';

describe('observatory metadata boundaries', () => {
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

  it('ignores a late session rejection from a stale load sequence', () => {
    expect(isCurrentLoadSequence(3, 4)).toBe(false);
    expect(isCurrentLoadSequence(4, 4)).toBe(true);
  });

  it('coalesces a high-frequency SSE burst and guarantees a trailing snapshot refresh', async () => {
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

  it('keeps an empty health history empty while the live stream is disconnected', () => {
    const empty = { page: { items: [], nextCursor: null, snapshotAt: '2026-10-16T19:00:00Z' }, state: 'empty' as const, error: null };
    expect(healthHistoryAfterDisconnect(empty)).toEqual(empty);
    const populated = { page: { items: [{ snapshotId: 'snapshot-1' }], nextCursor: null, snapshotAt: '2026-10-16T19:00:00Z' }, state: 'ready' as const, error: null };
    expect(healthHistoryAfterDisconnect(populated)).toMatchObject({ state: 'stale', error: 'Live updates are disconnected. Showing the last known health history.' });
  });

});
