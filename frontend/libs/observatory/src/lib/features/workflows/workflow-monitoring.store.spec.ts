import { describe, expect, it } from 'vitest';
import { parseWorkflowMonitoringSnapshot } from './workflow-monitoring-parser';

const validSnapshot = () => ({
  snapshotAt: '2026-10-28T08:00:00.000Z',
  window: { from: '2026-10-27T08:00:00.000Z', to: '2026-10-28T08:00:00.000Z', bucket: 'one_hour', bucketSeconds: 3600 },
  lifecycleCounts: [{ metric: 'workflow_transition_outcome', operation: 'workflow.transition', outcome: 'succeeded', count: 8, partial: false }],
  latencySummaries: [{ metric: 'workflow_wait_lag_ms', unit: 'milliseconds', summary: { sampleCount: 2, minimum: 10, maximum: 90, average: 44, p50: 40, p90: 75, p95: 80, p99: 90, ratePerInterval: null, rateIntervalSeconds: null }, availability: 'available', partial: false }],
  series: [{ metric: 'workflow_wait_lag_ms', unit: 'milliseconds', summary: { sampleCount: 2, minimum: 10, maximum: 90, average: 44, p50: 40, p90: 75, p95: 80, p99: 90, ratePerInterval: null, rateIntervalSeconds: null }, points: [{ startAt: '2026-10-27T08:00:00.000Z', endAt: '2026-10-27T09:00:00.000Z', value: 35, sampleCount: 2, availability: 'available', partial: false }], availability: 'available', partial: false }],
  failureBreakdowns: [{ component: 'aura.automation.workflow_step.wait_until', operation: 'resume', outcome: 'failed', errorClass: 'timeout', count: 1, partial: false }],
  recentCorrelations: [{ metric: 'workflow_agent_run_step_duration_ms', runId: '6f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a01', traceId: '0123456789abcdef0123456789abcdef', observedAt: '2026-10-28T08:00:00.000Z', component: 'aura.automation.workflow_step.agent_run', operation: 'execute', outcome: 'succeeded', errorClass: null }],
  freshness: { latestObservedAt: '2026-10-28T08:00:00.000Z', lagMs: 0, state: 'fresh' },
  partial: false,
  partialReasons: [],
  unavailableSignals: [],
  limits: { defaultWindowHours: 24, maximumWindowDays: 31, maximumScannedRows: 1000000, scannedRows: 12, maximumSeries: 12, returnedSeries: 1, maximumPointsPerSeries: 200, returnedPoints: 1, maximumFailureBreakdowns: 50, returnedFailureBreakdowns: 1, maximumRecentCorrelations: 25, returnedRecentCorrelations: 1, executionLimitMs: 5000, executionMs: 4, truncatedByExecutionLimit: false, truncatedByStorageLimit: false },
  capturePolicy: 'metadata_only',
});

describe('workflow monitoring DTO boundary', () => {
  it('accepts the exact closed metadata-only snapshot shape', () => {
    expect(parseWorkflowMonitoringSnapshot(validSnapshot())).not.toBeNull();
  });

  it('rejects recursive extras, missing fields, constants, and unsupported bounds', () => {
    const snapshot = validSnapshot();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, window: { ...snapshot.window, ownerId: 'owner:secret' } })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, latencySummaries: [{ ...snapshot.latencySummaries[0], summary: { ...snapshot.latencySummaries[0].summary, content: 'private' } }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, limits: { ...snapshot.limits, maximumSeries: 13 } })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, series: [{ ...snapshot.series[0], points: [{ ...snapshot.series[0].points[0], value: 1e16 }] }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, partialReasons: ['late_data', 'late_data'] })).toBeNull();
  });

  it('rejects malformed identifiers before they can reach navigation state', () => {
    const snapshot = validSnapshot();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, recentCorrelations: [{ ...snapshot.recentCorrelations[0], runId: 'ownerId:secret' }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, recentCorrelations: [{ ...snapshot.recentCorrelations[0], traceId: 'trace-20261028-001' }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, failureBreakdowns: [{ ...snapshot.failureBreakdowns[0], errorClass: null }] })).toBeNull();
  });

  it('accepts production wait-resume failures but rejects values outside generated vocabularies', () => {
    const snapshot = validSnapshot();
    expect(parseWorkflowMonitoringSnapshot(snapshot)).not.toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, lifecycleCounts: [{ ...snapshot.lifecycleCounts[0], operation: 'workflow.advance' }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, failureBreakdowns: [{ ...snapshot.failureBreakdowns[0], operation: 'workflow.private' }] })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, failureBreakdowns: [{ ...snapshot.failureBreakdowns[0], errorClass: 'secret_error' }] })).toBeNull();
  });

  it('rejects impossible UTC dates and windows outside the bounded interval', () => {
    const snapshot = validSnapshot();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, snapshotAt: '2026-02-30T08:00:00.000Z' })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, window: { ...snapshot.window, from: '2026-04-31T08:00:00.000Z' } })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, window: { ...snapshot.window, from: snapshot.window.to } })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, window: { ...snapshot.window, from: '2026-10-28T08:00:00.000Z', to: '2026-10-27T08:00:00.000Z' } })).toBeNull();
    expect(parseWorkflowMonitoringSnapshot({ ...snapshot, window: { ...snapshot.window, from: '2026-01-01T08:00:00.000Z', to: '2026-02-01T08:00:00.001Z' } })).toBeNull();
  });
});
