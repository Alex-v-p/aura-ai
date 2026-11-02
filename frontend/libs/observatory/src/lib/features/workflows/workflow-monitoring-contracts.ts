import type { AnalyticsBucket, WorkflowLatencyMetricName, WorkflowOutcomeMetricName } from '@aura/observatory-api-client';

export const WORKFLOW_OUTCOME_METRICS: ReadonlyArray<WorkflowOutcomeMetricName> = ['workflow_transition_outcome', 'workflow_command_outcome', 'workflow_repository_outcome'];
export const WORKFLOW_LATENCY_METRICS: ReadonlyArray<WorkflowLatencyMetricName> = ['workflow_transition_duration_ms', 'workflow_command_duration_ms', 'workflow_repository_duration_ms', 'workflow_definition_validation_duration_ms', 'workflow_agent_run_step_duration_ms', 'workflow_approval_command_duration_ms', 'workflow_notification_step_duration_ms', 'workflow_wait_command_duration_ms', 'workflow_wait_lag_ms', 'workflow_checkpoint_persistence_duration_ms', 'workflow_cancellation_duration_ms', 'workflow_settlement_duration_ms'];
export const WORKFLOW_BUCKETS: ReadonlyArray<AnalyticsBucket> = ['auto', 'one_minute', 'five_minutes', 'fifteen_minutes', 'one_hour', 'six_hours', 'one_day'];

export const isWorkflowOutcomeMetric = (value: unknown): value is WorkflowOutcomeMetricName => typeof value === 'string' && WORKFLOW_OUTCOME_METRICS.includes(value as WorkflowOutcomeMetricName);
export const isWorkflowLatencyMetric = (value: unknown): value is WorkflowLatencyMetricName => typeof value === 'string' && WORKFLOW_LATENCY_METRICS.includes(value as WorkflowLatencyMetricName);
export const isWorkflowBucket = (value: unknown): value is AnalyticsBucket => typeof value === 'string' && WORKFLOW_BUCKETS.includes(value as AnalyticsBucket);
/** Consume a monotonic Observatory revision once; repeated effect passes must not reschedule it. */
export const workflowMonitoringRevisionChanged = (observedRevision: number, currentRevision: number): boolean => currentRevision > observedRevision;
export const validWorkflowUtcWindow = (from: string, to: string): boolean => {
  const start = Date.parse(from); const end = Date.parse(to);
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(from) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(to) && Number.isFinite(start) && Number.isFinite(end) && end > start && end - start <= 31 * 24 * 60 * 60 * 1000;
};
