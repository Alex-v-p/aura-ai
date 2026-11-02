import type { WorkflowMonitoringSnapshot } from '@aura/observatory-api-client';
import { isWorkflowBucket, isWorkflowLatencyMetric, isWorkflowOutcomeMetric } from './workflow-monitoring-contracts';

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const utcParts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$/;
const parseUtc = (value: unknown): Date | null => {
  if (typeof value !== 'string') return null;
  const match = utcParts.exec(value);
  if (!match) return null;
  const year = Number(match[1]); const month = Number(match[2]); const day = Number(match[3]);
  const hour = Number(match[4]); const minute = Number(match[5]); const second = Number(match[6]);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  const monthEnd = new Date(0); monthEnd.setUTCFullYear(year, month, 0);
  const daysInMonth = monthEnd.getUTCDate();
  if (day < 1 || day > daysInMonth) return null;
  const milliseconds = Number((match[7] ?? '').padEnd(3, '0').slice(0, 3) || '0');
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, milliseconds);
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day && date.getUTCHours() === hour && date.getUTCMinutes() === minute && date.getUTCSeconds() === second && date.getUTCMilliseconds() === milliseconds ? date : null;
};
const isUtc = (value: unknown): value is string => parseUtc(value) !== null;
const isUuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const isTraceId = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const WORKFLOW_COMPONENTS = ['aura.automation.workflow_state', 'aura.automation.workflow_definition', 'aura.automation.workflow_step.agent_run', 'aura.automation.workflow_approval', 'aura.automation.workflow_step.notification', 'aura.automation.workflow_step.wait_until', 'aura.automation.workflow_checkpoint', 'aura.automation.workflow_cancellation', 'aura.automation.workflow_settlement'] as const;
const WORKFLOW_LIFECYCLE_OPERATIONS = ['workflow.transition', 'workflow.command', 'workflow.repository'] as const;
const WORKFLOW_FAILURE_OPERATIONS = ['workflow.transition', 'workflow.command', 'workflow.repository', 'execute', 'validate', 'resume', 'persist', 'cancel', 'settle'] as const;
const WORKFLOW_ERROR_CLASSES = ['unknown', 'authorization', 'database', 'database_programming', 'idempotency', 'internal', 'not_found', 'persistence', 'serialization', 'execution', 'budget', 'capability', 'provider_protocol', 'provider', 'timeout', 'validation', 'conflict'] as const;
const isWorkflowComponent = (value: unknown): boolean => typeof value === 'string' && WORKFLOW_COMPONENTS.includes(value as typeof WORKFLOW_COMPONENTS[number]);
const isWorkflowLifecycleOperation = (value: unknown): boolean => typeof value === 'string' && WORKFLOW_LIFECYCLE_OPERATIONS.includes(value as typeof WORKFLOW_LIFECYCLE_OPERATIONS[number]);
const isWorkflowFailureOperation = (value: unknown): boolean => typeof value === 'string' && WORKFLOW_FAILURE_OPERATIONS.includes(value as typeof WORKFLOW_FAILURE_OPERATIONS[number]);
const isWorkflowErrorClass = (value: unknown): boolean => typeof value === 'string' && WORKFLOW_ERROR_CLASSES.includes(value as typeof WORKFLOW_ERROR_CLASSES[number]);
const isOutcome = (value: unknown): boolean => ['succeeded', 'failed', 'cancelled', 'unknown'].includes(String(value));
const isAvailability = (value: unknown): boolean => ['available', 'no_data', 'unavailable'].includes(String(value));
const hasOnly = (value: Record<string, unknown>, keys: ReadonlyArray<string>): boolean => Object.keys(value).every((key) => keys.includes(key));
const isNonNegativeCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isBoundedFinite = (value: unknown, minimum: number, maximum: number): value is number => isFiniteNumber(value) && value >= minimum && value <= maximum;
const isNullableUtc = (value: unknown): value is string | null => value === null || isUtc(value);
const isNullableFinite = (value: unknown): value is number | null => value === null || isFiniteNumber(value);
const isUnique = (values: ReadonlyArray<unknown>): boolean => new Set(values).size === values.length;

const isWorkflowWindow = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['from', 'to', 'bucket', 'bucketSeconds'])) return false;
  const from = parseUtc(value['from']); const to = parseUtc(value['to']);
  if (!from || !to || to.getTime() <= from.getTime() || to.getTime() - from.getTime() > 31 * 24 * 60 * 60 * 1000) return false;
  return isWorkflowBucket(value['bucket']) && Number.isSafeInteger(value['bucketSeconds']) && (value['bucketSeconds'] as number) >= 60 && (value['bucketSeconds'] as number) <= 86400;
};
const isAnalyticsFreshness = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['latestObservedAt', 'lagMs', 'state'])) return false;
  return isNullableUtc(value['latestObservedAt']) && (value['lagMs'] === null || isBoundedFinite(value['lagMs'], 0, 604800000)) && ['fresh', 'stale', 'unavailable'].includes(String(value['state']));
};
const isAnalyticsSeriesSummary = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['sampleCount', 'minimum', 'maximum', 'average', 'p50', 'p90', 'p95', 'p99', 'ratePerInterval', 'rateIntervalSeconds'])) return false;
  return isNonNegativeCount(value['sampleCount']) && ['minimum', 'maximum', 'average', 'p50', 'p90', 'p95', 'p99'].every((key) => isNullableFinite(value[key])) && (value['ratePerInterval'] === null || isBoundedFinite(value['ratePerInterval'], 0, Number.MAX_VALUE)) && (value['rateIntervalSeconds'] === null || (Number.isSafeInteger(value['rateIntervalSeconds']) && (value['rateIntervalSeconds'] as number) >= 60 && (value['rateIntervalSeconds'] as number) <= 86400));
};
const isAnalyticsPoint = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['startAt', 'endAt', 'value', 'sampleCount', 'availability', 'partial'])) return false;
  return isUtc(value['startAt']) && isUtc(value['endAt']) && (value['value'] === null || isBoundedFinite(value['value'], -1000000000000000, 1000000000000000)) && isNonNegativeCount(value['sampleCount']) && isAvailability(value['availability']) && typeof value['partial'] === 'boolean';
};
const isWorkflowLifecycleCount = (value: unknown): boolean => isRecord(value) && hasOnly(value, ['metric', 'operation', 'outcome', 'count', 'partial']) && isWorkflowOutcomeMetric(value['metric']) && isWorkflowLifecycleOperation(value['operation']) && isOutcome(value['outcome']) && isNonNegativeCount(value['count']) && typeof value['partial'] === 'boolean';
const isWorkflowLatencySummary = (value: unknown): boolean => isRecord(value) && hasOnly(value, ['metric', 'unit', 'summary', 'availability', 'partial']) && isWorkflowLatencyMetric(value['metric']) && value['unit'] === 'milliseconds' && isAnalyticsSeriesSummary(value['summary']) && isAvailability(value['availability']) && typeof value['partial'] === 'boolean';
const isWorkflowSeries = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['metric', 'unit', 'summary', 'points', 'availability', 'partial']) || !isAnalyticsSeriesSummary(value['summary']) || !Array.isArray(value['points']) || value['points'].length > 200 || !value['points'].every(isAnalyticsPoint) || !isAvailability(value['availability']) || typeof value['partial'] !== 'boolean') return false;
  return (isWorkflowOutcomeMetric(value['metric']) && value['unit'] === 'count') || (isWorkflowLatencyMetric(value['metric']) && value['unit'] === 'milliseconds');
};
const isWorkflowFailureBreakdown = (value: unknown): boolean => isRecord(value) && hasOnly(value, ['component', 'operation', 'outcome', 'errorClass', 'count', 'partial']) && isWorkflowComponent(value['component']) && isWorkflowFailureOperation(value['operation']) && value['outcome'] === 'failed' && isWorkflowErrorClass(value['errorClass']) && isNonNegativeCount(value['count']) && typeof value['partial'] === 'boolean';
const isWorkflowCorrelation = (value: unknown): boolean => isRecord(value) && hasOnly(value, ['metric', 'runId', 'traceId', 'observedAt', 'component', 'operation', 'outcome', 'errorClass']) && value['metric'] === 'workflow_agent_run_step_duration_ms' && isUuid(value['runId']) && isTraceId(value['traceId']) && isUtc(value['observedAt']) && value['component'] === 'aura.automation.workflow_step.agent_run' && value['operation'] === 'execute' && isOutcome(value['outcome']) && (value['errorClass'] === null || isWorkflowErrorClass(value['errorClass']));
const isWorkflowLimits = (value: unknown): boolean => {
  if (!isRecord(value) || !hasOnly(value, ['defaultWindowHours', 'maximumWindowDays', 'maximumScannedRows', 'scannedRows', 'maximumSeries', 'returnedSeries', 'maximumPointsPerSeries', 'returnedPoints', 'maximumFailureBreakdowns', 'returnedFailureBreakdowns', 'maximumRecentCorrelations', 'returnedRecentCorrelations', 'executionLimitMs', 'executionMs', 'truncatedByExecutionLimit', 'truncatedByStorageLimit'])) return false;
  return value['defaultWindowHours'] === 24 && value['maximumWindowDays'] === 31 && value['maximumScannedRows'] === 1000000 && isBoundedFinite(value['scannedRows'], 0, 1000000) && Number.isSafeInteger(value['scannedRows']) && value['maximumSeries'] === 12 && isBoundedFinite(value['returnedSeries'], 0, 12) && Number.isSafeInteger(value['returnedSeries']) && value['maximumPointsPerSeries'] === 200 && isBoundedFinite(value['returnedPoints'], 0, 2400) && Number.isSafeInteger(value['returnedPoints']) && value['maximumFailureBreakdowns'] === 50 && isBoundedFinite(value['returnedFailureBreakdowns'], 0, 50) && Number.isSafeInteger(value['returnedFailureBreakdowns']) && value['maximumRecentCorrelations'] === 25 && isBoundedFinite(value['returnedRecentCorrelations'], 0, 25) && Number.isSafeInteger(value['returnedRecentCorrelations']) && value['executionLimitMs'] === 5000 && isBoundedFinite(value['executionMs'], 0, 5000) && typeof value['truncatedByExecutionLimit'] === 'boolean' && typeof value['truncatedByStorageLimit'] === 'boolean';
};

/** Runtime boundary for the generated closed workflow-monitoring DTO. */
export function parseWorkflowMonitoringSnapshot(value: unknown): WorkflowMonitoringSnapshot | null {
  if (!isRecord(value)) return null;
  const item = value;
  if (!hasOnly(item, ['snapshotAt', 'window', 'lifecycleCounts', 'latencySummaries', 'series', 'failureBreakdowns', 'recentCorrelations', 'freshness', 'partial', 'partialReasons', 'unavailableSignals', 'limits', 'capturePolicy'])) return null;
  if (!isUtc(item['snapshotAt']) || !isWorkflowWindow(item['window']) || !isAnalyticsFreshness(item['freshness']) || !isWorkflowLimits(item['limits']) || item['capturePolicy'] !== 'metadata_only' || typeof item['partial'] !== 'boolean') return null;
  const counts = item['lifecycleCounts'];
  if (!Array.isArray(counts) || counts.length > 50 || !counts.every(isWorkflowLifecycleCount)) return null;
  const summaries = item['latencySummaries'];
  if (!Array.isArray(summaries) || summaries.length > 12 || !summaries.every(isWorkflowLatencySummary)) return null;
  const series = item['series'];
  if (!Array.isArray(series) || series.length > 12 || !series.every(isWorkflowSeries)) return null;
  const failures = item['failureBreakdowns'];
  if (!Array.isArray(failures) || failures.length > 50 || !failures.every(isWorkflowFailureBreakdown)) return null;
  const correlations = item['recentCorrelations'];
  if (!Array.isArray(correlations) || correlations.length > 25 || !correlations.every(isWorkflowCorrelation)) return null;
  const partialReasons = item['partialReasons']; const unavailable = item['unavailableSignals'];
  if (!Array.isArray(partialReasons) || partialReasons.length > 8 || !isUnique(partialReasons) || !partialReasons.every((entry) => ['scan_limit', 'execution_limit', 'storage_limit', 'series_limit', 'point_limit', 'correlation_limit', 'late_data', 'retention_boundary'].includes(String(entry))) || !Array.isArray(unavailable) || unavailable.length > 5 || !isUnique(unavailable) || !unavailable.every((entry) => ['lifecycle_counts', 'latency_summaries', 'time_series', 'failure_breakdowns', 'run_trace_correlation'].includes(String(entry)))) return null;
  return value as WorkflowMonitoringSnapshot;
}
