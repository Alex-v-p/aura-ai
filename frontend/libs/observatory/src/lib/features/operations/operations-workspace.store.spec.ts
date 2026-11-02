import { describe, expect, it } from 'vitest';
import type { SavedQuery, SavedQueryDefinition } from '@aura/observatory-api-client';
import { dashboardExecutionKey, dashboardOperationForKind, incompatibleDashboardQuery, parseDashboardExecution } from './operations-dashboard-contracts';

const observedAt = '2026-10-21T09:00:00.000Z';
const query = (definition: SavedQueryDefinition, queryId = 'query-1'): SavedQuery => ({ queryId, queryVersion: '1.0.0', name: 'Operations query', definition, createdAt: observedAt });
const runDefinition = (overrides: Record<string, unknown> = {}): SavedQueryDefinition => ({ kind: 'run_search', window: 'twenty_four_hours', statuses: ['running', 'succeeded'], components: ['component-a', 'component-b'], deployments: ['deploy-a', 'deploy-b'], models: ['model-a', 'model-b'], pageSize: 50, ...overrides } as SavedQueryDefinition);
const traceDefinition = (overrides: Record<string, unknown> = {}): SavedQueryDefinition => ({ kind: 'trace_search', window: 'twenty_four_hours', components: ['component-a', 'component-b'], outcomes: ['succeeded', 'failed'], pageSize: 50, ...overrides } as SavedQueryDefinition);
const regressionDefinition = (overrides: Record<string, unknown> = {}): SavedQueryDefinition => ({ kind: 'regression_search', window: 'seven_days', outcomes: ['regressed', 'improved'], experimentReferences: [{ resourceId: 'experiment-a', resourceVersion: '1.0.0' }, { resourceId: 'experiment-b', resourceVersion: '1.0.0' }], pageSize: 50, ...overrides } as SavedQueryDefinition);
const unavailableExecution = (saved: SavedQuery) => ({ kind: saved.definition.kind, query: { queryId: saved.queryId, queryVersion: saved.queryVersion }, state: 'unavailable', partial: false, snapshotAt: observedAt, freshness: { state: 'unavailable', measuredAt: null, lagMs: null }, result: null, ...(saved.definition.kind === 'run_search' || saved.definition.kind === 'trace_search' || saved.definition.kind === 'regression_search' ? { nextCursor: null } : {}) });

describe('operations dashboard execution contracts', () => {
  it('maps every closed query kind to the exact-version generated REST operation', () => {
    expect(['analytics', 'run_search', 'trace_search', 'run_comparison', 'regression_search', 'component_health'].map(dashboardOperationForKind)).toEqual(Array(6).fill('executeSavedQuery'));
  });

  it('keys identical saved queries by panel identity and visualization', () => {
    const saved = query({ kind: 'analytics', metric: 'errors', unit: 'count', aggregation: 'sum', window: 'one_hour', bucket: 'auto', groupBy: 'component', focus: 'all', facets: [], seriesLimit: 12, pointLimit: 200 });
    expect(dashboardExecutionKey(saved, 'panel-a', 'table')).not.toBe(dashboardExecutionKey(saved, 'panel-b', 'comparison'));
  });

  it('keeps panel compatibility separate from exact saved-query execution', () => {
    const analytics = { kind: 'analytics' } as SavedQueryDefinition;
    expect(incompatibleDashboardQuery(analytics, 'comparison')).toContain('incompatible');
    expect(incompatibleDashboardQuery(analytics, 'table')).toBeNull();
  });

  it('accepts maximum bounded multi-filter definitions because filtering is server-side', () => {
    const saved = query(runDefinition(), 'query-run');
    const projection = parseDashboardExecution({ ...unavailableExecution(saved), state: 'complete', freshness: { state: 'fresh', measuredAt: observedAt, lagMs: 0 }, result: { items: [], nextCursor: null, snapshotAt: observedAt, latestObservationId: null, partial: false } }, saved);
    expect(projection.state).toBe('complete');
    expect(projection.result?.kind).toBe('run_search');
    expect((query(traceDefinition(), 'query-trace').definition as Extract<SavedQueryDefinition, { kind: 'trace_search' }>).outcomes).toHaveLength(2);
    expect((query(regressionDefinition(), 'query-regression').definition as Extract<SavedQueryDefinition, { kind: 'regression_search' }>).experimentReferences).toHaveLength(2);
  });

  it('parses a server analytics execution envelope for dashboard rendering', () => {
    const saved = query({ kind: 'analytics', metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', window: 'one_hour', bucket: 'auto', groupBy: 'component', focus: 'all', facets: [], seriesLimit: 12, pointLimit: 200 });
    const snapshot = { snapshotAt: observedAt, percentileMethod: 'clickhouse_quantile_exact', capturePolicy: 'metadata_only', query: { metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', focus: 'all', from: '2026-10-21T08:00:00.000Z', to: observedAt, bucket: 'auto', bucketSeconds: 1800, groupBy: 'component', filters: { component: 'aura.observatory.operations' }, seriesLimit: 12, pointLimit: 200 }, series: [{ seriesKey: 'aura.observatory.operations', facets: { component: 'aura.observatory.operations' }, summary: { sampleCount: 1, minimum: 1, maximum: 1, average: 1, p50: 1, p90: 1, p95: 1, p99: 1, ratePerInterval: null, rateIntervalSeconds: null }, points: [{ startAt: '2026-10-21T08:00:00.000Z', endAt: observedAt, value: 1, sampleCount: 1, availability: 'available', partial: false }], availability: 'available', partial: false }], breakdowns: [], freshness: { latestObservedAt: observedAt, lagMs: 0, state: 'fresh' }, partial: false, partialReasons: [], unavailableSignals: [], scan: { scannedRows: 1, scanLimit: 1000000, executionMs: 1, executionLimitMs: 5000 } };
    const projection = parseDashboardExecution({ kind: 'analytics', query: { queryId: saved.queryId, queryVersion: saved.queryVersion }, state: 'complete', partial: false, snapshotAt: observedAt, freshness: { state: 'fresh', measuredAt: observedAt, lagMs: 0 }, result: snapshot }, saved);
    expect(projection.result?.kind).toBe('analytics');
    expect(projection.snapshot?.series[0]?.points[0]?.value).toBe(1);
  });

  it('consumes every closed result discriminant and preserves unavailable state', () => {
    const definitions: ReadonlyArray<SavedQueryDefinition> = [
      { kind: 'analytics', metric: 'errors', unit: 'count', aggregation: 'sum', window: 'one_hour', bucket: 'auto', groupBy: 'component', focus: 'all', facets: [], seriesLimit: 12, pointLimit: 200 },
      runDefinition(), traceDefinition(),
      { kind: 'run_comparison', baselineRunId: 'run-a', candidateRunId: 'run-b' },
      regressionDefinition(),
      { kind: 'component_health', componentId: 'component-a', componentVersion: '1.0.0', window: 'one_hour', pointLimit: 50 },
    ];
    for (const definition of definitions) {
      const saved = query(definition, `query-${definition.kind}`);
      expect(parseDashboardExecution(unavailableExecution(saved), saved).state).toBe('unavailable');
    }
  });
});
