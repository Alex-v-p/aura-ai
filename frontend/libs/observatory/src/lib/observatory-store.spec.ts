import { describe, expect, it } from 'vitest';
import type { Overview } from '@aura/observatory-api-client';
import { extractSessionCsrf } from './observatory-session';
import { ownerLoginHref } from './observatory-models';
import { datetimeLocalToUtc, isOverviewStale, normalizeRunFilters, RequestGenerationGate, resourceStateFor } from './observatory-models';

describe('observatory metadata boundaries', () => {
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

});
