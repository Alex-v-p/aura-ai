import { describe, expect, it } from 'vitest';
import { isWorkflowLatencyMetric, isWorkflowOutcomeMetric, validWorkflowUtcWindow, workflowMonitoringRevisionChanged } from './workflow-monitoring-contracts';

const outcomeMetrics = ['workflow_transition_outcome', 'workflow_command_outcome', 'workflow_repository_outcome'] as const;
const latencyMetrics = [
  'workflow_transition_duration_ms', 'workflow_command_duration_ms', 'workflow_repository_duration_ms',
  'workflow_definition_validation_duration_ms', 'workflow_agent_run_step_duration_ms',
  'workflow_approval_command_duration_ms', 'workflow_notification_step_duration_ms', 'workflow_wait_command_duration_ms',
  'workflow_wait_lag_ms', 'workflow_checkpoint_persistence_duration_ms', 'workflow_cancellation_duration_ms',
  'workflow_settlement_duration_ms',
] as const;

describe('workflow monitoring frontend contract guards', () => {
  it('keeps all fifteen measurements closed and preserves count versus millisecond units', () => {
    expect(outcomeMetrics).toHaveLength(3);
    expect(latencyMetrics).toHaveLength(12);
    for (const metric of outcomeMetrics) expect(isWorkflowOutcomeMetric(metric)).toBe(true);
    for (const metric of latencyMetrics) expect(isWorkflowLatencyMetric(metric)).toBe(true);
    expect(isWorkflowOutcomeMetric('workflow_scheduler_scan_duration_ms')).toBe(false);
    expect(isWorkflowLatencyMetric('workflow_private_payload_ms')).toBe(false);
  });

  it('keeps the workflow metric catalogue closed and units distinct', () => {
    expect(isWorkflowOutcomeMetric('workflow_transition_outcome')).toBe(true);
    expect(isWorkflowOutcomeMetric('workflow_private_payload')).toBe(false);
    expect(isWorkflowLatencyMetric('workflow_wait_lag_ms')).toBe(true);
    expect(isWorkflowLatencyMetric('workflow_command_outcome')).toBe(false);
  });

  it('accepts bounded UTC windows and rejects malformed or oversized ranges', () => {
    expect(validWorkflowUtcWindow('2026-10-27T10:00:00.000Z', '2026-10-28T10:00:00.000Z')).toBe(true);
    expect(validWorkflowUtcWindow('2026-10-28T10:00:00+01:00', '2026-10-28T11:00:00+01:00')).toBe(false);
    expect(validWorkflowUtcWindow('2026-09-01T10:00:00.000Z', '2026-10-28T10:00:00.000Z')).toBe(false);
  });

  it('consumes a monotonic invalidation revision once instead of rescheduling every effect pass', () => {
    let observedRevision = 0;
    const revision = 3;
    expect(workflowMonitoringRevisionChanged(observedRevision, revision)).toBe(true);
    observedRevision = revision;
    expect(workflowMonitoringRevisionChanged(observedRevision, revision)).toBe(false);
    expect(workflowMonitoringRevisionChanged(observedRevision, 2)).toBe(false);
    expect(workflowMonitoringRevisionChanged(observedRevision, 4)).toBe(true);
  });

});
