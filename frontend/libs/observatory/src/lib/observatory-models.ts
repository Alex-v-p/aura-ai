import type {
  ComponentCollection,
  ComponentDefinition,
  ComponentDependencyTopology,
  ComponentHealth,
  CreateRetentionActionRequest,
  DeploymentCollection,
  HealthHistoryPage,
  LiveEvent,
  AnalyticsAggregation,
  AnalyticsAvailability,
  AnalyticsBucket,
  AnalyticsFocus,
  AnalyticsGroupBy,
  AnalyticsMetricCatalogue,
  AnalyticsMetricDefinition,
  AnalyticsMetricName,
  AnalyticsPartialReason,
  AnalyticsPercentileMethod,
  AnalyticsUnavailableSignal,
  AnalyticsUnit,
  RunComparison,
  OperationalAnalyticsSnapshot,
  Overview,
  RetentionAction,
  RunDetail,
  RunInspection,
  RunEvidenceManifest,
  RunPage,
  SelfMonitoringSnapshot,
  TraceDetail,
} from '@aura/observatory-api-client';
import { OBSERVATORY_API_OPERATIONS } from '@aura/observatory-api-client';

export type ObservatoryViewState = 'loading' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'error';
export type ObservatoryResource = 'overview' | 'runs' | 'components' | 'deployments';
export type ObservatoryResourceState = 'loading' | 'ready' | 'empty' | 'stale' | 'error';

/** Bounded, memory-only controls for the operational analytics explorer. */
export type AnalyticsFilters = {
  readonly focus: AnalyticsFocus;
  readonly metric: AnalyticsMetricName;
  readonly unit: AnalyticsUnit;
  readonly aggregation: AnalyticsAggregation;
  readonly bucket: AnalyticsBucket;
  readonly groupBy: AnalyticsGroupBy;
  readonly from?: string;
  readonly to?: string;
  readonly component?: string;
  readonly deployment?: string;
};

export type AnalyticsViewState = 'loading' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'error';
export type AnalyticsResource = {
  readonly catalogue: AnalyticsMetricCatalogue | null;
  readonly snapshot: OperationalAnalyticsSnapshot | null;
  readonly state: AnalyticsViewState;
  readonly error: string | null;
};

export const DEFAULT_ANALYTICS_FILTERS: AnalyticsFilters = {
  focus: 'all',
  metric: 'operation_duration_ms',
  unit: 'milliseconds',
  aggregation: 'p95',
  bucket: 'auto',
  groupBy: 'component',
};

const ANALYTICS_FOCUSES = ['all', 'model', 'queue', 'retrieval', 'tool', 'memory', 'automation'] as const satisfies ReadonlyArray<AnalyticsFocus>;
const ANALYTICS_UNITS = ['count', 'milliseconds', 'bytes', 'ratio', 'score'] as const satisfies ReadonlyArray<AnalyticsUnit>;
const ANALYTICS_AGGREGATIONS = ['average', 'sum', 'minimum', 'maximum', 'p50', 'p90', 'p95', 'p99', 'rate', 'count'] as const satisfies ReadonlyArray<AnalyticsAggregation>;
const ANALYTICS_BUCKETS = ['auto', 'one_minute', 'five_minutes', 'fifteen_minutes', 'one_hour', 'six_hours', 'one_day'] as const satisfies ReadonlyArray<AnalyticsBucket>;
const ANALYTICS_GROUPINGS = ['none', 'component', 'component_version', 'operation', 'deployment', 'environment', 'host', 'service_role', 'provider', 'outcome', 'error_class', 'status', 'retrieval_stage'] as const satisfies ReadonlyArray<AnalyticsGroupBy>;
const ANALYTICS_METRICS = ['operation_duration_ms', 'run_duration_ms', 'queue_wait_ms', 'errors', 'retries', 'backlog', 'backlog_depth', 'inference_duration_ms', 'time_to_first_token_ms', 'output_tokens', 'provider_errors', 'model_routing_duration_ms', 'structured_inference_duration_ms', 'tool_selection_duration_ms', 'tool_selection_outcome', 'tool_policy_evaluation_duration_ms', 'tool_policy_decision', 'tool_argument_validation_outcome', 'tool_execution_duration_ms', 'tool_execution_outcome', 'tool_timeout_count', 'memory_extraction_duration_ms', 'memory_candidate_decision', 'memory_reviewable_candidate_count', 'memory_operation_duration_ms', 'memory_operation_outcome', 'memory_retrieval_duration_ms', 'memory_lexical_search_duration_ms', 'memory_vector_search_duration_ms', 'memory_rerank_duration_ms', 'memory_processing_errors', 'memory_retry_count', 'memory_reindex_backlog', 'document_retrieval_duration_ms', 'document_retrieval_outcome', 'document_retrieval_candidate_count', 'reminder_scheduler_scan_duration_ms', 'reminder_due_lag_ms', 'reminder_series_due_lag_ms', 'reminder_fired_count', 'reminder_failure_count', 'notification_failure_count', 'notification_acknowledgement_outcome', 'evaluation_runner_nats_recovery_count', 'workflow_transition_outcome', 'workflow_command_outcome', 'workflow_repository_outcome', 'workflow_transition_duration_ms', 'workflow_command_duration_ms', 'workflow_repository_duration_ms', 'workflow_definition_validation_duration_ms', 'workflow_agent_run_step_duration_ms', 'workflow_approval_command_duration_ms', 'workflow_notification_step_duration_ms', 'workflow_wait_command_duration_ms', 'workflow_wait_lag_ms', 'workflow_checkpoint_persistence_duration_ms', 'workflow_cancellation_duration_ms', 'workflow_settlement_duration_ms'] as const satisfies ReadonlyArray<AnalyticsMetricName>;
const ANALYTICS_AVAILABILITIES = ['available', 'no_data', 'unavailable'] as const satisfies ReadonlyArray<AnalyticsAvailability>;
const ANALYTICS_PARTIAL_REASONS = ['scan_limit', 'execution_limit', 'series_limit', 'point_limit', 'late_data', 'retention_boundary'] as const satisfies ReadonlyArray<AnalyticsPartialReason>;
const ANALYTICS_UNAVAILABLE_SIGNALS = ['model_identity', 'retryability', 'input_cohort', 'gpu', 'selected_evidence', 'side_effect_detail', 'quality', 'saturation', 'measurement', 'unit', 'dimension'] as const satisfies ReadonlyArray<AnalyticsUnavailableSignal>;
const ANALYTICS_PERCENTILE_METHODS = ['clickhouse_quantile_exact'] as const satisfies ReadonlyArray<AnalyticsPercentileMethod>;
const ANALYTICS_FACET_KEYS = ['component', 'componentVersion', 'operation', 'deployment', 'environment', 'host', 'serviceRole', 'provider', 'outcome', 'errorClass', 'status', 'retrievalStage'] as const;
const ANALYTICS_MAX_WINDOW_MS = 31 * 24 * 60 * 60 * 1000;
const ANALYTICS_LIMITS = { defaultWindowHours: 24, maximumWindowDays: 31, maximumPointsPerSeries: 200, maximumSeries: 12, maximumBreakdowns: 50, maximumScannedRows: 1_000_000, executionTimeoutMs: 5_000 } as const;
function enumValue<T extends string>(values: ReadonlyArray<T>, value: unknown): value is T { return typeof value === 'string' && values.includes(value as T); }
function onlyKeys(value: Record<string, unknown>, keys: ReadonlyArray<string>): boolean { return Object.keys(value).every((key) => keys.includes(key)); }
function analyticsUtc(value: unknown): value is string { return safeText(value) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(value) && Number.isFinite(Date.parse(value)); }
/** Reject known content/private markers before any analytics value reaches a template. */
const COMPONENT_ID_PATTERN = /^aura\.[a-z][a-z0-9_.-]{0,90}$/;
const SAFE_LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const OPERATION_PATTERN = /^[a-z][a-z0-9_.-]{0,95}$/;
const ENVIRONMENT_PATTERN = /^[a-z][a-z0-9_.-]{0,31}$/;
const DIMENSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const SERIES_KEY_PATTERN = /^[A-Za-z0-9._:+-]{1,256}$/;
function analyticsPattern(value: unknown, pattern: RegExp): value is string { return typeof value === 'string' && pattern.test(value); }
function analyticsFacetValue(key: string, value: unknown): boolean {
  switch (key) {
    case 'component': return analyticsPattern(value, COMPONENT_ID_PATTERN);
    case 'componentVersion':
    case 'deployment':
    case 'host': return analyticsPattern(value, SAFE_LABEL_PATTERN);
    case 'operation': return analyticsPattern(value, OPERATION_PATTERN);
    case 'environment': return analyticsPattern(value, ENVIRONMENT_PATTERN);
    case 'serviceRole': return enumValue(['api', 'worker', 'scheduler', 'cli'], value);
    case 'outcome': return enumValue(['succeeded', 'failed', 'cancelled', 'unknown'], value);
    case 'provider':
    case 'errorClass':
    case 'status':
    case 'retrievalStage': return analyticsPattern(value, DIMENSION_PATTERN);
    default: return false;
  }
}
function analyticsFacets(value: unknown): boolean {
  const facets = record(value);
  return Boolean(facets && Object.keys(facets).length <= ANALYTICS_FACET_KEYS.length && onlyKeys(facets, ANALYTICS_FACET_KEYS) && Object.keys(facets).every((key) => ANALYTICS_FACET_KEYS.includes(key as typeof ANALYTICS_FACET_KEYS[number]) && analyticsFacetValue(key, facets[key])));
}
function analyticsWindow(from: unknown, to: unknown): boolean {
  if (!analyticsUtc(from) || !analyticsUtc(to)) return false;
  const duration = Date.parse(to) - Date.parse(from);
  return duration > 0 && duration <= ANALYTICS_MAX_WINDOW_MS;
}
function analyticsMetricDefinition(value: unknown): value is AnalyticsMetricDefinition {
  const item = record(value);
  const focusAreas = item?.['focusAreas']; const aggregations = item?.['supportedAggregations']; const groupings = item?.['supportedGroupings']; const unavailable = item?.['unavailableSignals'];
  return Boolean(item && onlyKeys(item, ['metric', 'unit', 'focusAreas', 'supportedAggregations', 'supportedGroupings', 'availability', 'unavailableSignals']) && enumValue(ANALYTICS_METRICS, item['metric']) && enumValue(ANALYTICS_UNITS, item['unit']) && Array.isArray(focusAreas) && focusAreas.length > 0 && focusAreas.length <= ANALYTICS_FOCUSES.length && focusAreas.every((entry) => enumValue(ANALYTICS_FOCUSES, entry)) && Array.isArray(aggregations) && aggregations.length > 0 && aggregations.length <= ANALYTICS_AGGREGATIONS.length && aggregations.every((entry) => enumValue(ANALYTICS_AGGREGATIONS, entry)) && Array.isArray(groupings) && groupings.length > 0 && groupings.length <= ANALYTICS_GROUPINGS.length && groupings.every((entry) => enumValue(ANALYTICS_GROUPINGS, entry)) && enumValue(ANALYTICS_AVAILABILITIES, item['availability']) && Array.isArray(unavailable) && unavailable.length <= ANALYTICS_UNAVAILABLE_SIGNALS.length && unavailable.every((entry) => enumValue(ANALYTICS_UNAVAILABLE_SIGNALS, entry)));
}
/** Validate the closed metric catalogue before it reaches filter controls. */
export function parseAnalyticsMetricCatalogue(value: unknown): AnalyticsMetricCatalogue | null {
  const item = record(value); const limits = record(item?.['limits']); const entries = item?.['items'];
  if (!item || !onlyKeys(item, ['snapshotAt', 'items', 'limits', 'capturePolicy']) || !analyticsUtc(item['snapshotAt']) || item['capturePolicy'] !== 'metadata_only' || !Array.isArray(entries) || entries.length > 128 || !entries.every(analyticsMetricDefinition) || !limits || !onlyKeys(limits, Object.keys(ANALYTICS_LIMITS))) return null;
  if (!Object.entries(ANALYTICS_LIMITS).every(([key, expected]) => limits[key] === expected)) return null;
  return item as unknown as AnalyticsMetricCatalogue;
}

/** Validate bounded analytics JSON before it reaches the template. */
export function parseOperationalAnalyticsSnapshot(value: unknown): OperationalAnalyticsSnapshot | null {
  const item = record(value);
  const query = record(item?.['query']);
  const freshness = record(item?.['freshness']);
  const scan = record(item?.['scan']);
  const series = item?.['series'];
  const breakdowns = item?.['breakdowns'];
  const boundedNumber = (candidate: unknown): candidate is number => typeof candidate === 'number' && Number.isFinite(candidate);
  const boundedNullableNumber = (candidate: unknown): candidate is number | null => candidate === null || boundedNumber(candidate);
  const availability = (candidate: unknown): boolean => candidate === 'available' || candidate === 'no_data' || candidate === 'unavailable';
  const validBreakdown = (candidate: unknown): boolean => { const breakdown = record(candidate); return Boolean(breakdown && onlyKeys(breakdown, ['groupBy', 'value', 'count', 'partial']) && enumValue(ANALYTICS_GROUPINGS, breakdown['groupBy']) && analyticsPattern(breakdown['value'], DIMENSION_PATTERN) && Number.isSafeInteger(breakdown['count']) && (breakdown['count'] as number) >= 0 && typeof breakdown['partial'] === 'boolean'); };
  const validPoint = (candidate: unknown): boolean => {
    const point = record(candidate);
    return Boolean(point && onlyKeys(point, ['startAt', 'endAt', 'value', 'sampleCount', 'availability', 'partial']) && analyticsUtc(point['startAt']) && analyticsUtc(point['endAt']) && boundedNullableNumber(point['value']) && Number.isSafeInteger(point['sampleCount']) && (point['sampleCount'] as number) >= 0 && availability(point['availability']) && typeof point['partial'] === 'boolean');
  };
  const validSeries = (candidate: unknown): boolean => {
    const current = record(candidate);
    const summary = record(current?.['summary']);
    const points = current?.['points'];
    return Boolean(current && onlyKeys(current, ['seriesKey', 'facets', 'summary', 'points', 'availability', 'partial']) && analyticsPattern(current['seriesKey'], SERIES_KEY_PATTERN) && analyticsFacets(current['facets']) && summary && onlyKeys(summary, ['sampleCount', 'minimum', 'maximum', 'average', 'p50', 'p90', 'p95', 'p99', 'ratePerInterval', 'rateIntervalSeconds']) && Array.isArray(points) && points.length <= 200 && points.every(validPoint) && availability(current['availability']) && typeof current['partial'] === 'boolean' && Number.isSafeInteger(summary['sampleCount']) && (summary['sampleCount'] as number) >= 0 && ['minimum', 'maximum', 'average', 'p50', 'p90', 'p95', 'p99', 'ratePerInterval'].every((key) => boundedNullableNumber(summary[key])) && (summary['rateIntervalSeconds'] === null || (Number.isSafeInteger(summary['rateIntervalSeconds']) && (summary['rateIntervalSeconds'] as number) >= 0)));
  };
  const groupingFacet: Record<string, string> = { component: 'component', component_version: 'componentVersion', operation: 'operation', deployment: 'deployment', environment: 'environment', host: 'host', service_role: 'serviceRole', provider: 'provider', outcome: 'outcome', error_class: 'errorClass', status: 'status', retrieval_stage: 'retrievalStage' };
  const groupingIsConsistent = (candidate: unknown): boolean => {
    const facets = record(record(candidate)?.['facets']);
    if (!facets || query?.['groupBy'] === 'none') return Boolean(facets && Object.keys(facets).length === 0);
    const key = groupingFacet[String(query?.['groupBy'])];
    return Boolean(key && Object.prototype.hasOwnProperty.call(facets, key));
  };
  if (!item || !onlyKeys(item, ['snapshotAt', 'query', 'percentileMethod', 'series', 'breakdowns', 'freshness', 'partial', 'partialReasons', 'unavailableSignals', 'scan', 'capturePolicy']) || !query || !onlyKeys(query, ['metric', 'unit', 'aggregation', 'focus', 'from', 'to', 'bucket', 'bucketSeconds', 'groupBy', 'filters', 'seriesLimit', 'pointLimit']) || !freshness || !onlyKeys(freshness, ['latestObservedAt', 'lagMs', 'state']) || !scan || !onlyKeys(scan, ['scannedRows', 'scanLimit', 'executionMs', 'executionLimitMs']) || !Array.isArray(series) || series.length > ANALYTICS_LIMITS.maximumSeries || !series.every(validSeries) || !series.every(groupingIsConsistent) || !Array.isArray(breakdowns) || breakdowns.length > ANALYTICS_LIMITS.maximumBreakdowns || !breakdowns.every(validBreakdown) || !analyticsUtc(item['snapshotAt']) || item['capturePolicy'] !== 'metadata_only' || !enumValue(ANALYTICS_PERCENTILE_METHODS, item['percentileMethod']) || !enumValue(ANALYTICS_METRICS, query['metric']) || !enumValue(ANALYTICS_UNITS, query['unit']) || !enumValue(ANALYTICS_AGGREGATIONS, query['aggregation']) || !enumValue(ANALYTICS_FOCUSES, query['focus']) || !analyticsWindow(query['from'], query['to']) || !enumValue(ANALYTICS_BUCKETS, query['bucket']) || !Number.isSafeInteger(query['bucketSeconds']) || (query['bucketSeconds'] as number) < 60 || (query['bucketSeconds'] as number) > 86400 || !enumValue(ANALYTICS_GROUPINGS, query['groupBy']) || !analyticsFacets(query['filters']) || !Number.isSafeInteger(query['seriesLimit']) || (query['seriesLimit'] as number) < 1 || (query['seriesLimit'] as number) > ANALYTICS_LIMITS.maximumSeries || !Number.isSafeInteger(query['pointLimit']) || (query['pointLimit'] as number) < 1 || (query['pointLimit'] as number) > ANALYTICS_LIMITS.maximumPointsPerSeries || !['fresh', 'stale', 'unavailable'].includes(freshness['state'] as string) || (freshness['latestObservedAt'] !== null && !analyticsUtc(freshness['latestObservedAt'])) || (freshness['lagMs'] !== null && (!boundedNumber(freshness['lagMs']) || freshness['lagMs'] < 0)) || typeof item['partial'] !== 'boolean' || !Array.isArray(item['partialReasons']) || item['partialReasons'].length > 6 || !item['partialReasons'].every((entry) => enumValue(ANALYTICS_PARTIAL_REASONS, entry)) || !Array.isArray(item['unavailableSignals']) || item['unavailableSignals'].length > 10 || !item['unavailableSignals'].every((entry) => enumValue(ANALYTICS_UNAVAILABLE_SIGNALS, entry)) || !Number.isSafeInteger(scan['scannedRows']) || (scan['scannedRows'] as number) < 0 || (scan['scannedRows'] as number) > ANALYTICS_LIMITS.maximumScannedRows || scan['scanLimit'] !== ANALYTICS_LIMITS.maximumScannedRows || !boundedNumber(scan['executionMs']) || scan['executionMs'] < 0 || scan['executionMs'] > ANALYTICS_LIMITS.executionTimeoutMs || scan['executionLimitMs'] !== ANALYTICS_LIMITS.executionTimeoutMs) return null;
  return item as unknown as OperationalAnalyticsSnapshot;
}
export type ObservatoryAuthState = 'checking' | 'authenticated' | 'initial' | 'expired' | 'explicit_logout' | 'logout_unconfirmed' | 'callback_failed' | 'callback_denied' | 'identity_unavailable';
export type ObservatoryAuthRecoveryMarker = 'state_missing' | 'state_invalid' | 'code_missing' | 'provider_denied' | 'callback_failed';

/** Keep an async snapshot from mutating state after a newer load starts. */
export function isCurrentLoadSequence(sequence: number, currentSequence: number): boolean {
  return sequence === currentSequence;
}

/** Identify an authentication loss without class-coupling this state module to a transport. */
export function hasSessionRequiredRejection(
  results: ReadonlyArray<PromiseSettledResult<unknown>>,
  isSessionRequired: (reason: unknown) => boolean,
): boolean {
  return results.some((result) => result.status === 'rejected' && isSessionRequired(result.reason));
}

/**
 * Coalesce bursty live notifications into one active refresh and one trailing
 * refresh. This is deliberately timer-free: the active request provides the
 * backpressure boundary and the trailing request guarantees the latest event
 * is eventually reflected without creating an unbounded queue.
 */
export class LiveRefreshCoordinator {
  private running = false;
  private pending = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastStartedAt: number | null = null;

  constructor(private readonly cadenceMs = 0) {}

  get isRunning(): boolean { return this.running; }
  get hasPending(): boolean { return this.pending; }

  request(run: () => Promise<void>, canRun: () => boolean = () => true): void {
    if (!canRun()) return;
    if (this.running) {
      this.pending = true;
      return;
    }
    const elapsed = this.lastStartedAt === null ? this.cadenceMs : Date.now() - this.lastStartedAt;
    if (this.cadenceMs > 0 && elapsed < this.cadenceMs) {
      this.pending = true;
      if (!this.timer) {
        this.timer = setTimeout(() => {
          this.timer = null;
          if (!this.pending) return;
          this.pending = false;
          this.request(run, canRun);
        }, Math.max(0, this.cadenceMs - elapsed));
      }
      return;
    }
    this.lastStartedAt = Date.now();
    this.running = true;
    void run().then(() => undefined, () => undefined).finally(() => {
      this.running = false;
      if (!this.pending) return;
      this.pending = false;
      if (canRun()) this.request(run, canRun);
    });
  }

  cancel(): void {
    this.pending = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

/** Comparison data changes only; health and keepalive events cannot alter run evidence. */
export function isComparisonInvalidationEvent(eventType: string): boolean {
  return eventType === 'observation.changed' || eventType === 'retention.changed';
}

/**
 * Only run-correlated metadata changes invalidate the live run projection.
 * Observatory's own health observations are deliberately not a reason to
 * read the same projection again: doing so would create a self-observation
 * request loop.
 */
export function isRunProjectionInvalidation(eventType: string, resourceType: string | null): boolean {
  return eventType === 'retention.changed'
    || (eventType === 'observation.changed' && (resourceType === 'run' || resourceType === 'trace'));
}

/** Analytics and comparison snapshots follow the same run-scoped boundary. */
export function isRunCorrelatedInvalidation(eventType: string, resourceType: string | null): boolean {
  return eventType === 'observation.changed' && (resourceType === 'run' || resourceType === 'trace');
}

/** A selected-run refresh requires an explicit metadata-only run target. */
export function matchesSelectedRunInvalidation(eventType: string, resourceType: string | null, resourceId: string | null, selectedRunId: string | null): boolean {
  return isComparisonInvalidationEvent(eventType) && resourceType === 'run' && resourceId !== null && resourceId === selectedRunId;
}

/**
 * Clearing disclosed bytes must not erase an accepted deletion lifecycle.
 * The idempotency key is the in-memory authority needed to resume polling on
 * Trace re-entry; without it, a deleting state cannot safely be continued.
 */
export function shouldPreserveRunEvidenceDeletionState(state: string, hasDeletionKey: boolean): boolean {
  return state === 'deleting' && hasDeletionKey;
}

export const SELECTED_RUN_LOAD_TIMEOUT_MS = 10_000;
export function selectedRunLoadError(timedOut: boolean): string { return timedOut ? 'The selected run inspector did not respond within 10 seconds. Retry when the service is available.' : 'Run detail is unavailable.'; }

/**
 * A disclosed blob remains valid only while its authoritative manifest is the
 * same captured object. Capture-state transitions and any immutable metadata
 * change must revoke the previously disclosed bytes.
 */
export function isCurrentCapturedRunEvidenceManifest(current: RunEvidenceManifest | null | undefined, disclosed: RunEvidenceManifest): boolean {
  return Boolean(current && current.captureState === 'captured' && disclosed.captureState === 'captured'
    && current.evidenceId === disclosed.evidenceId
    && current.mediaClassification === disclosed.mediaClassification
    && current.mediaType === disclosed.mediaType
    && current.digest === disclosed.digest
    && current.byteCount === disclosed.byteCount);
}

/** Reconcile a disclosed item against the latest authoritative inspection. */
export function inspectionEvidenceIsCurrent(inspection: RunInspection | null | undefined, disclosed: RunEvidenceManifest): boolean {
  const current = inspection?.steps.flatMap((step) => step.evidence).find((manifest) => manifest.evidenceId === disclosed.evidenceId);
  return isCurrentCapturedRunEvidenceManifest(current, disclosed);
}

const MAX_RETURN_PATH_LENGTH = 2048;
const SENSITIVE_RETURN_KEYS = new Set([
  'access', 'accesstoken', 'apikey', 'aud', 'authorization', 'authorisation',
  'authorizationcode', 'authorisationcode', 'authorizationendpoint',
  'authorisationendpoint', 'azp', 'bearer', 'bearertoken', 'clientkey',
  'clientid', 'clientassertion', 'clientsecret', 'cookie', 'cookievalue',
  'code', 'credential', 'credentials', 'credentialvalue', 'endpoint',
  'body', 'content', 'document', 'file', 'filename', 'input', 'memory',
  'message', 'output', 'payload', 'tool', 'toolargument', 'toolarguments',
  'toolresult', 'toolresults',
  'audience', 'error', 'errordescription', 'erroruri', 'endsessionendpoint',
  'idtoken', 'issuer', 'iss', 'jwksuri', 'loginhint', 'jwt', 'nonce',
  'password', 'prompt', 'privatekey', 'provider', 'redirecturi',
  'redirecturl', 'request', 'requesturi', 'response', 'responsemode',
  'responsetype', 'refreshtoken', 'secret', 'secretkey', 'session',
  'sessionid', 'scope', 'state', 'sub', 'subject', 'token', 'tokenhint',
  'tokenendpoint', 'userinfoendpoint', 'owner', 'owneridentity',
  'displayname', 'preferredusername', 'authrecovery',
]);
const SENSITIVE_RETURN_SUFFIXES = ['token', 'credential', 'credentials', 'secret', 'password'];
const SENSITIVE_RETURN_PREFIXES = ['error', 'oauthstate', 'loginstate', 'authcode', 'oauthcode'];

function isSensitiveReturnKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '');
  return SENSITIVE_RETURN_KEYS.has(normalized)
    || SENSITIVE_RETURN_PREFIXES.some((prefix) => normalized.startsWith(prefix))
    || SENSITIVE_RETURN_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

/** Keep callback recovery finite and content-free at the browser boundary. */
export function authStateForRecoveryMarker(marker: string | null): ObservatoryAuthState | null {
  switch (marker) {
    case 'provider_denied': return 'callback_denied';
    case 'state_missing':
    case 'state_invalid':
    case 'code_missing':
    case 'callback_failed': return 'callback_failed';
    default: return null;
  }
}

/** Consume the one-shot callback marker without retaining it in browser history. */
export function consumeAuthRecoveryMarker(): ObservatoryAuthState | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  if (!url.searchParams.has('auth_recovery')) return null;
  const state = authStateForRecoveryMarker(url.searchParams.get('auth_recovery'));
  window.history.replaceState(null, '', boundedReturnPath(`${url.pathname}${url.search}`));
  return state;
}

export function boundedReturnPath(returnTo: string): string {
  if (typeof returnTo !== 'string' || returnTo.length > MAX_RETURN_PATH_LENGTH || !returnTo.startsWith('/') || returnTo.startsWith('//')) return '/';
  try {
    const url = new URL(returnTo, 'http://observatory.invalid');
    if (url.origin !== 'http://observatory.invalid' || !url.pathname.startsWith('/')) return '/';
    const query = new URLSearchParams();
    for (const [key, value] of url.searchParams.entries()) if (!isSensitiveReturnKey(key)) query.append(key, value);
    const sanitized = `${url.pathname}${query.toString() ? `?${query.toString()}` : ''}`;
    return sanitized.length <= MAX_RETURN_PATH_LENGTH ? sanitized : '/';
  } catch {
    return '/';
  }
}

export function ownerLoginHref(baseUrl = '', returnTo = '/'): string {
  return `${baseUrl}${OBSERVATORY_API_OPERATIONS.startOwnerLogin.pathTemplate}?${new URLSearchParams({ return_to: boundedReturnPath(returnTo) }).toString()}`;
}

export type ObservatoryAuthCopy = { readonly title: string; readonly message: string; readonly action: string; readonly announcement: string; readonly retryAction: 'session' | 'logout' | null };
export function authCopyForState(state: ObservatoryAuthState): ObservatoryAuthCopy {
  switch (state) {
    case 'expired': return { title: 'Your Observatory session expired', message: 'Sign in again to continue viewing owner-only operational metadata.', action: 'Sign in as owner', announcement: 'Your Observatory session expired. Sign in again to continue.', retryAction: null };
    case 'explicit_logout': return { title: 'You are signed out of Observatory', message: 'Sign in again when you are ready to view owner-only operational metadata.', action: 'Sign in as owner', announcement: 'You are signed out of Observatory.', retryAction: null };
    case 'callback_denied': return { title: 'Owner sign-in was not authorized', message: 'No Observatory session was created. You can try signing in again.', action: 'Try owner sign-in again', announcement: 'Owner sign-in was not authorized. You can try again.', retryAction: null };
    case 'callback_failed': return { title: 'Owner sign-in could not be completed', message: 'No Observatory session was created. Try signing in again.', action: 'Try owner sign-in again', announcement: 'Owner sign-in could not be completed. You can try again.', retryAction: null };
    case 'logout_unconfirmed': return { title: 'Sign-out could not be confirmed', message: 'The Observatory service did not confirm sign-out. Your session may still be active; try again.', action: 'Try sign-out again', announcement: 'Sign-out could not be confirmed. Your session may still be active.', retryAction: 'logout' };
    case 'identity_unavailable': return { title: 'Owner sign-in is temporarily unavailable', message: 'The identity service did not respond. Check your session again, or use sign-in when it is available.', action: 'Check session again', announcement: 'Owner sign-in is temporarily unavailable.', retryAction: 'session' };
    case 'initial': return { title: 'Sign in to Observatory', message: 'This owner-only workspace requires an Observatory session.', action: 'Sign in as owner', announcement: 'Sign in to Observatory to continue.', retryAction: null };
    default: return { title: 'Checking your Observatory session', message: 'Your owner session is being checked.', action: 'Sign in as owner', announcement: '', retryAction: null };
  }
}
export type RequestToken = { readonly key: string; readonly generation: number };
/** Small in-memory gate for deduplicating and invalidating async panel requests. */
export class RequestGenerationGate {
  private generation = 0;
  private readonly active = new Map<string, number>();
  begin(key: string): RequestToken | null {
    if (this.active.has(key)) return null;
    const token = { key, generation: this.generation } as const;
    this.active.set(key, token.generation);
    return token;
  }
  invalidate(): void { this.generation += 1; this.active.clear(); }
  has(key: string): boolean { return this.active.has(key); }
  isCurrent(token: RequestToken): boolean { return token.generation === this.generation && this.active.get(token.key) === token.generation; }
  finish(token: RequestToken): void { if (this.active.get(token.key) === token.generation) this.active.delete(token.key); }
}
export type ComponentDetailRequestToken = { readonly componentId: string; readonly componentVersion: string; readonly generation: number };
/** Only the latest selected component detail request may publish or focus. */
export class ComponentDetailRequestGate {
  private generation = 0;
  private current: ComponentDetailRequestToken | null = null;
  begin(componentId: string, componentVersion: string): ComponentDetailRequestToken {
    const token = { componentId, componentVersion, generation: ++this.generation };
    this.current = token;
    return token;
  }
  invalidate(): void { this.generation += 1; this.current = null; }
  isCurrent(token: ComponentDetailRequestToken): boolean { return this.current?.generation === token.generation && this.current.componentId === token.componentId && this.current.componentVersion === token.componentVersion; }
}
export type RunFilters = { status?: RunPage['items'][number]['status']; component?: string; model?: string; deployment?: string; from?: string; to?: string };
export type ObservatorySnapshot = {
  overview: Overview | null;
  runs: RunPage | null;
  components: ComponentCollection | null;
  deployments: DeploymentCollection | null;
  state: ObservatoryViewState;
  lastUpdatedAt: string | null;
  lastEventId: string | null;
  error: string | null;
  partial: boolean;
  resourceErrors: ReadonlyArray<string>;
};

export type ComponentCatalogueSelection = {
  readonly componentId: string;
  readonly componentVersion: string;
  readonly definition: ComponentDefinition | null;
  readonly topology: ComponentDependencyTopology | null;
  readonly state: 'idle' | 'loading' | 'ready' | 'partial' | 'error';
  readonly error: string | null;
};

const MAX_DEFINITION_TEXT = 256;
const MAX_DEFINITION_ITEMS = 128;
const MAX_TOPOLOGY_NODES = 100;
const MAX_TOPOLOGY_EDGES = 200;
function record(value: unknown): Record<string, unknown> | null { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function safeText(value: unknown): value is string { return typeof value === 'string' && value.length > 0 && value.length <= MAX_DEFINITION_TEXT && !Array.from(value).some((character) => { const code = character.codePointAt(0) ?? 0; return code < 0x20 || code === 0x7f; }); }
function boundedTextList(value: unknown): value is ReadonlyArray<string> { return Array.isArray(value) && value.length <= MAX_DEFINITION_ITEMS && value.every((item) => safeText(item)); }
function boundedNullableText(value: unknown): value is string | null { return value === null || safeText(value); }
function validHealth(value: unknown): value is ComponentHealth {
  const item = record(value); const outcomes = record(item?.['outcomes']);
  return Boolean(item && outcomes && Number.isSafeInteger(item['sampleCount']) && (item['sampleCount'] as number) >= 0 && (item['latencyP50Ms'] === null || (typeof item['latencyP50Ms'] === 'number' && Number.isFinite(item['latencyP50Ms']) && item['latencyP50Ms'] >= 0)) && (item['latencyP95Ms'] === null || (typeof item['latencyP95Ms'] === 'number' && Number.isFinite(item['latencyP95Ms']) && item['latencyP95Ms'] >= 0)) && (item['errorRate'] === null || (typeof item['errorRate'] === 'number' && Number.isFinite(item['errorRate']) && item['errorRate'] >= 0 && item['errorRate'] <= 1)) && ['succeeded', 'failed', 'cancelled', 'unknown'].every((key) => Number.isSafeInteger(outcomes[key]) && (outcomes[key] as number) >= 0));
}
/** Validate closed generated responses before metadata reaches the template. */
export function parseComponentDefinition(value: unknown): ComponentDefinition | null {
  const item = record(value); const dependencies = item?.['dependencies'];
  if (!item || !safeText(item['componentId']) || !safeText(item['componentVersion']) || !safeText(item['owner']) || !safeText(item['category']) || !safeText(item['inputSchemaId']) || !safeText(item['outputSchemaId']) || !boundedTextList(item['measurements']) || !boundedTextList(item['evaluators']) || !Array.isArray(dependencies) || dependencies.length > MAX_DEFINITION_ITEMS) return null;
  if (!dependencies.every((dependency) => { const edge = record(dependency); return Boolean(edge && safeText(edge['dependencyId']) && (edge['kind'] === 'component' || edge['kind'] === 'provider') && boundedNullableText(edge['requiredVersion'])); })) return null;
  return item as unknown as ComponentDefinition;
}
export function parseComponentDependencyTopology(value: unknown): ComponentDependencyTopology | null {
  const item = record(value); const nodes = item?.['nodes']; const edges = item?.['edges'];
  if (!item || !safeText(item['rootComponentId']) || !safeText(item['rootComponentVersion']) || !Array.isArray(nodes) || nodes.length > MAX_TOPOLOGY_NODES || !Array.isArray(edges) || edges.length > MAX_TOPOLOGY_EDGES || !boundedTextList(item['directDependencyIds']) || !boundedTextList(item['reverseDependencyIds']) || typeof item['partial'] !== 'boolean' || !safeText(item['snapshotAt'])) return null;
  if (!nodes.every((node) => { const entry = record(node); return Boolean(entry && safeText(entry['dependencyId']) && (entry['kind'] === 'component' || entry['kind'] === 'provider') && boundedNullableText(entry['componentVersion']) && (entry['definitionState'] === 'declared' || entry['definitionState'] === 'observed_only' || entry['definitionState'] === 'missing') && boundedTextList(entry['deployedVersions']) && (entry['availability'] === 'observed' || entry['availability'] === 'unobserved' || entry['availability'] === 'unavailable') && (entry['health'] === null || validHealth(entry['health'])) && typeof entry['partialHealth'] === 'boolean'); })) return null;
  if (!edges.every((edge) => { const entry = record(edge); return Boolean(entry && safeText(entry['sourceId']) && safeText(entry['sourceVersion']) && safeText(entry['targetId']) && boundedNullableText(entry['targetVersion']) && (entry['targetKind'] === 'component' || entry['targetKind'] === 'provider') && (entry['resolution'] === 'resolved' || entry['resolution'] === 'missing_definition' || entry['resolution'] === 'version_unavailable')); })) return null;
  return item as unknown as ComponentDependencyTopology;
}
export function componentAvailabilityLabel(availability: string): string {
  switch (availability) {
    case 'observed': return 'Observed';
    case 'unobserved': return 'Unobserved';
    case 'unavailable': return 'Unavailable';
    default: return 'Unavailable';
  }
}
export type DependencyNodeIdentity = { readonly dependencyId: string; readonly componentVersion: string | null };
export function dependencyNodeForEdge<T extends DependencyNodeIdentity>(nodes: ReadonlyArray<T>, dependencyId: string, componentVersion: string | null): T | undefined {
  return nodes.find((node) => node.dependencyId === dependencyId && node.componentVersion === componentVersion);
}

const COMPARISON_MARKERS = ['deployment_id', 'git_commit', 'image_version', 'configuration_version', 'model_id', 'model_version', 'agent_revision', 'prompt_bundle_revision', 'policy_revision'] as const;
const COMPARISON_STATES = ['equal', 'changed', 'missing', 'unavailable'] as const;
const COMPARISON_AVAILABILITY = ['available', 'missing', 'unavailable'] as const;
const COMPARISON_ALIGNMENT = ['matched', 'left_only', 'right_only', 'incompatible'] as const;
const TRACE_INTEGRITY = ['complete', 'retained_partial', 'truncated', 'missing_parent', 'cyclic', 'duplicate'] as const;
const COMPARISON_PARTIAL_REASONS = ['observation_limit', 'span_limit', 'measurement_limit', 'scan_limit', 'execution_limit', 'late_data', 'retention_boundary', 'incomplete_run', 'missing_parent', 'cyclic', 'duplicate_span'] as const;
const COMPARISON_RUN_STATUSES = ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'] as const;
const COMPARISON_OUTCOMES = ['succeeded', 'failed', 'cancelled', 'unknown'] as const;
const COMPARISON_SAFE_LABEL = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const COMPARISON_COMPONENT_ID = /^aura\.[a-z][a-z0-9_.-]{0,90}$/;
const COMPARISON_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const COMPARISON_TRACE_ID = /^[0-9a-f]{32}$/;
const COMPARISON_SPAN_ID = /^[0-9a-f]{16}$/;
const COMPARISON_ALIGNMENT_KEY = /^[A-Za-z0-9._:+-]{1,512}$/;
const COMPARISON_PRIVATE_SEGMENT = '(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)';
const COMPARISON_REVISION_IDENTIFIER = new RegExp(`^(?!${COMPARISON_PRIVATE_SEGMENT}(?:$|[._:+-]))(?!.*[._:+-]${COMPARISON_PRIVATE_SEGMENT}(?:$|[._:+-]))[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$`, 'i');
const COMPARISON_MODEL_IDENTIFIER = new RegExp(`^(?!(?:https?|ftp|file|mailto|ssh|ws|wss):)(?!${COMPARISON_PRIVATE_SEGMENT}(?:$|[._:+-]))(?!.*[._:+-]${COMPARISON_PRIVATE_SEGMENT}(?:$|[._:+-]))[a-z0-9][a-z0-9._+-]{0,127}(?::(?=[a-z0-9._+-]{1,64}$)(?=[a-z0-9._+-]*[a-z])[a-z0-9._+-]+)?$`);
const COMPARISON_UNITS = ['count', 'milliseconds', 'bytes', 'ratio', 'score'] as const satisfies ReadonlyArray<AnalyticsUnit>;
const COMPARISON_NUMERIC_LIMIT = 1_000_000_000_000_000;
const COMPARISON_OPERATION = /^[a-z][a-z0-9_.-]{0,95}$/;
function comparisonEnum<T extends string>(value: unknown, values: ReadonlyArray<T>): value is T { return typeof value === 'string' && values.includes(value as T); }
function comparisonNumber(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= COMPARISON_NUMERIC_LIMIT; }
function comparisonNonNegativeNumber(value: unknown): value is number { return comparisonNumber(value) && value >= 0; }
function comparisonAbsoluteDelta(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 2_000_000_000_000_000; }
function comparisonNullableNumber(value: unknown): value is number | null { return value === null || comparisonNumber(value); }
function comparisonNullableNonNegativeNumber(value: unknown): value is number | null { return value === null || comparisonNonNegativeNumber(value); }
function comparisonUtc(value: unknown): value is string { return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(value) && Number.isFinite(Date.parse(value)); }
function comparisonRecord(value: unknown): Record<string, unknown> | null { return record(value); }
function comparisonKeys(value: Record<string, unknown>, keys: ReadonlyArray<string>): boolean { return Object.keys(value).every((key) => keys.includes(key)); }
function comparisonMarker(value: unknown, model = false): boolean { const marker = comparisonRecord(value); return Boolean(marker && comparisonKeys(marker, ['availability', 'value']) && comparisonEnum(marker['availability'], COMPARISON_AVAILABILITY) && (marker['value'] === null || (typeof marker['value'] === 'string' && (model ? COMPARISON_MODEL_IDENTIFIER.test(marker['value']) : COMPARISON_REVISION_IDENTIFIER.test(marker['value']))))); }
function comparisonSide(value: unknown): boolean {
  const side = comparisonRecord(value);
  if (!side || !comparisonKeys(side, ['role', 'runId', 'evidence', 'status', 'outcome', 'startedAt', 'finishedAt', 'durationMs', 'traceId', 'modelId', 'modelVersion', 'deploymentId', 'gitCommit', 'imageVersion', 'configurationVersion', 'agentRevision', 'promptBundleRevision', 'policyRevision', 'componentVersions', 'partial'])) return false;
  const versions = side['componentVersions'];
  return comparisonEnum(side['role'], ['left', 'right']) && typeof side['runId'] === 'string' && COMPARISON_UUID.test(side['runId']) && comparisonEnum(side['evidence'], COMPARISON_AVAILABILITY) && comparisonEnum(side['status'], COMPARISON_RUN_STATUSES) && comparisonEnum(side['outcome'], COMPARISON_OUTCOMES) && (side['startedAt'] === null || comparisonUtc(side['startedAt'])) && (side['finishedAt'] === null || comparisonUtc(side['finishedAt'])) && comparisonNullableNonNegativeNumber(side['durationMs']) && (side['durationMs'] === null || side['durationMs'] <= 604800000) && (side['traceId'] === null || (typeof side['traceId'] === 'string' && COMPARISON_TRACE_ID.test(side['traceId']))) && comparisonMarker(side['modelId'], true) && ['modelVersion', 'deploymentId', 'gitCommit', 'imageVersion', 'configurationVersion', 'agentRevision', 'promptBundleRevision', 'policyRevision'].every((key) => comparisonMarker(side[key])) && Array.isArray(versions) && versions.length <= 256 && versions.every((entry) => { const item = comparisonRecord(entry); return Boolean(item && comparisonKeys(item, ['componentId', 'version']) && typeof item['componentId'] === 'string' && COMPARISON_COMPONENT_ID.test(item['componentId']) && comparisonMarker(item['version'])); }) && typeof side['partial'] === 'boolean';
}
function comparisonNumberPair(value: unknown): boolean { const item = comparisonRecord(value); return Boolean(item && comparisonKeys(item, ['availability', 'value']) && comparisonEnum(item['availability'], COMPARISON_AVAILABILITY) && comparisonNullableNumber(item['value'])); }
function comparisonMeasurement(value: unknown): boolean { const item = comparisonRecord(value); return Boolean(item && comparisonKeys(item, ['name', 'unit', 'state', 'left', 'right', 'absoluteDelta']) && typeof item['name'] === 'string' && COMPARISON_OPERATION.test(item['name']) && comparisonEnum(item['unit'], COMPARISON_UNITS) && comparisonEnum(item['state'], COMPARISON_STATES) && comparisonNumberPair(item['left']) && comparisonNumberPair(item['right']) && (item['absoluteDelta'] === null || comparisonAbsoluteDelta(item['absoluteDelta']))); }
function comparisonComponent(value: unknown): boolean {
  const item = comparisonRecord(value);
  const side = (candidate: unknown): boolean => { const current = comparisonRecord(candidate); return Boolean(current && comparisonKeys(current, ['componentVersion', 'durationMs', 'outcome', 'spanCount', 'partial']) && comparisonMarker(current['componentVersion']) && comparisonNullableNonNegativeNumber(current['durationMs']) && (current['durationMs'] === null || current['durationMs'] <= 604_800_000) && comparisonEnum(current['outcome'], COMPARISON_OUTCOMES) && Number.isSafeInteger(current['spanCount']) && (current['spanCount'] as number) >= 0 && (current['spanCount'] as number) <= 5_000 && typeof current['partial'] === 'boolean'); };
  return Boolean(item && comparisonKeys(item, ['componentId', 'operation', 'alignment', 'left', 'right', 'measurements']) && typeof item['componentId'] === 'string' && COMPARISON_COMPONENT_ID.test(item['componentId']) && typeof item['operation'] === 'string' && COMPARISON_OPERATION.test(item['operation']) && comparisonEnum(item['alignment'], COMPARISON_ALIGNMENT) && (item['left'] === null || side(item['left'])) && (item['right'] === null || side(item['right'])) && Array.isArray(item['measurements']) && item['measurements'].length <= 128 && item['measurements'].every(comparisonMeasurement));
}
function comparisonSpan(value: unknown): boolean { const span = comparisonRecord(value); return Boolean(span && comparisonKeys(span, ['spanId', 'parentSpanId', 'componentId', 'componentVersion', 'operation', 'startedAt', 'durationMs', 'outcome', 'partial']) && typeof span['spanId'] === 'string' && COMPARISON_SPAN_ID.test(span['spanId']) && (span['parentSpanId'] === null || (typeof span['parentSpanId'] === 'string' && COMPARISON_SPAN_ID.test(span['parentSpanId']))) && typeof span['componentId'] === 'string' && COMPARISON_COMPONENT_ID.test(span['componentId']) && typeof span['componentVersion'] === 'string' && COMPARISON_SAFE_LABEL.test(span['componentVersion']) && typeof span['operation'] === 'string' && COMPARISON_OPERATION.test(span['operation']) && comparisonUtc(span['startedAt']) && comparisonNonNegativeNumber(span['durationMs']) && span['durationMs'] <= 604800000 && comparisonEnum(span['outcome'], COMPARISON_OUTCOMES) && typeof span['partial'] === 'boolean'); }
function comparisonIntegrity(value: unknown): boolean { return Array.isArray(value) && value.length >= 1 && value.length <= 6 && value.every((entry) => comparisonEnum(entry, TRACE_INTEGRITY)) && new Set(value).size === value.length; }
function comparisonAlignedSpan(value: unknown): boolean { const item = comparisonRecord(value); return Boolean(item && comparisonKeys(item, ['alignmentKey', 'alignment', 'left', 'right', 'leftIntegrity', 'rightIntegrity']) && typeof item['alignmentKey'] === 'string' && COMPARISON_ALIGNMENT_KEY.test(item['alignmentKey']) && comparisonEnum(item['alignment'], COMPARISON_ALIGNMENT) && (item['left'] === null || comparisonSpan(item['left'])) && (item['right'] === null || comparisonSpan(item['right'])) && comparisonIntegrity(item['leftIntegrity']) && comparisonIntegrity(item['rightIntegrity'])); }
function comparisonTrace(value: unknown): boolean { const item = comparisonRecord(value); return Boolean(item && comparisonKeys(item, ['leftTraceId', 'rightTraceId', 'leftEvidence', 'rightEvidence', 'leftIntegrity', 'rightIntegrity', 'alignedSpans', 'partial']) && (item['leftTraceId'] === null || (typeof item['leftTraceId'] === 'string' && COMPARISON_TRACE_ID.test(item['leftTraceId']))) && (item['rightTraceId'] === null || (typeof item['rightTraceId'] === 'string' && COMPARISON_TRACE_ID.test(item['rightTraceId']))) && comparisonEnum(item['leftEvidence'], COMPARISON_AVAILABILITY) && comparisonEnum(item['rightEvidence'], COMPARISON_AVAILABILITY) && comparisonIntegrity(item['leftIntegrity']) && comparisonIntegrity(item['rightIntegrity']) && Array.isArray(item['alignedSpans']) && item['alignedSpans'].length <= 10000 && item['alignedSpans'].every(comparisonAlignedSpan) && typeof item['partial'] === 'boolean'); }
/** Validate the closed comparison response before any marker or trace reaches a template. */
export function parseRunComparison(value: unknown): RunComparison | null {
  const item = comparisonRecord(value);
  const freshness = comparisonRecord(item?.['freshness']);
  const scan = comparisonRecord(item?.['scan']);
  const limits = comparisonRecord(item?.['limits']);
  const markers = item?.['markerCorrelations'];
  const validMarkerComparison = (entry: unknown): boolean => {
    const marker = comparisonRecord(entry);
    if (!marker || !comparisonKeys(marker, ['marker', 'state', 'left', 'right']) || !comparisonEnum(marker['marker'], COMPARISON_MARKERS) || !comparisonEnum(marker['state'], COMPARISON_STATES)) return false;
    const model = marker['marker'] === 'model_id';
    return comparisonMarker(marker['left'], model) && comparisonMarker(marker['right'], model);
  };
  const validFreshness = Boolean(
    freshness
    && comparisonKeys(freshness, ['latestObservedAt', 'lagMs', 'state'])
    && (freshness['latestObservedAt'] === null || comparisonUtc(freshness['latestObservedAt']))
    && comparisonNullableNonNegativeNumber(freshness['lagMs'])
    && (freshness['lagMs'] === null || freshness['lagMs'] <= 604_800_000)
    && comparisonEnum(freshness['state'], ['fresh', 'stale', 'unavailable']),
  );
  const validScan = Boolean(
    scan
    && comparisonKeys(scan, ['scannedRows', 'scanLimit', 'executionMs', 'executionLimitMs'])
    && Number.isSafeInteger(scan['scannedRows']) && (scan['scannedRows'] as number) >= 0 && (scan['scannedRows'] as number) <= 1_000_000
    && scan['scanLimit'] === 1_000_000
    && comparisonNonNegativeNumber(scan['executionMs']) && (scan['executionMs'] as number) <= 5_000
    && scan['executionLimitMs'] === 5_000,
  );
  const validLimits = Boolean(
    limits
    && comparisonKeys(limits, ['runCount', 'maximumObservationsPerRun', 'maximumSpansPerRun', 'maximumMeasurements', 'maximumScannedRows', 'executionTimeoutMs'])
    && limits['runCount'] === 2 && limits['maximumObservationsPerRun'] === 10_000 && limits['maximumSpansPerRun'] === 5_000
    && limits['maximumMeasurements'] === 2_000 && limits['maximumScannedRows'] === 1_000_000 && limits['executionTimeoutMs'] === 5_000,
  );
  const partialReasons = item?.['partialReasons'];
  const validPartialReasons = Array.isArray(partialReasons)
    && partialReasons.length <= COMPARISON_PARTIAL_REASONS.length
    && partialReasons.every((reason) => comparisonEnum(reason, COMPARISON_PARTIAL_REASONS))
    && new Set(partialReasons).size === partialReasons.length;
  const validUnavailableCount = Number.isSafeInteger(item?.['unavailableMarkerCount'])
    && (item?.['unavailableMarkerCount'] as number) >= 0
    && (item?.['unavailableMarkerCount'] as number) <= 18;
  if (!item
    || !comparisonKeys(item, ['snapshotAt', 'left', 'right', 'markerCorrelations', 'components', 'trace', 'freshness', 'partial', 'partialReasons', 'unavailableMarkerCount', 'scan', 'limits', 'capturePolicy'])
    || !comparisonUtc(item['snapshotAt'])
    || !comparisonSide(item['left']) || !comparisonSide(item['right'])
    || !Array.isArray(markers) || markers.length > COMPARISON_MARKERS.length || !markers.every(validMarkerComparison)
    || !Array.isArray(item['components']) || item['components'].length > 512 || !item['components'].every(comparisonComponent)
    || !comparisonTrace(item['trace']) || !validFreshness || typeof item['partial'] !== 'boolean'
    || !validPartialReasons || !validUnavailableCount || !validScan || !validLimits
    || item['capturePolicy'] !== 'metadata_only') return null;
  return item as unknown as RunComparison;
}

/**
 * The health history surface is deliberately separate from the observation
 * snapshot. It is bounded by the API page size and is never persisted by the
 * web application.
 */
export type ObservatoryHealthHistory = {
  page: HealthHistoryPage | null;
  state: 'loading' | 'ready' | 'empty' | 'stale' | 'error';
  error: string | null;
};

/** A disconnected stream must not turn an already-empty history into stale data. */
export function healthHistoryAfterDisconnect(history: ObservatoryHealthHistory): ObservatoryHealthHistory {
  const hasCachedSnapshots = Boolean(history.page?.items.length);
  return {
    ...history,
    state: hasCachedSnapshots ? 'stale' : history.state,
    error: hasCachedSnapshots ? 'Live updates are disconnected. Showing the last known health history.' : null,
  };
}
export function resourceStateFor(
  resource: ObservatoryResource,
  value: unknown,
  failed: boolean,
  observedAt?: string,
): ObservatoryResourceState {
  if (failed) return value ? 'stale' : 'error';
  if (!value) return 'loading';
  if (resource === 'overview' && observedAt && isOverviewStale(observedAt)) return 'stale';
  if (resource === 'runs' || resource === 'components' || resource === 'deployments') {
    const items = (value as { items?: ReadonlyArray<unknown> }).items;
    return items?.length ? 'ready' : 'empty';
  }
  return 'ready';
}
export type ComponentCatalogueView = 'loading' | 'error' | 'empty' | 'data';
/** A stale catalogue keeps its last successful cards visible; without cards it falls back to empty recovery copy. */
export function componentCatalogueViewFor(state: ObservatoryResourceState, itemCount: number): ComponentCatalogueView {
  if (state === 'loading') return 'loading';
  if (state === 'error') return 'error';
  if (state === 'stale' && itemCount > 0) return 'data';
  if (state === 'ready' && itemCount > 0) return 'data';
  return 'empty';
}
export type RetentionRequest = CreateRetentionActionRequest & { confirmation: 'delete-before' | 'purge-all' };
export type RetentionState = { action: RetentionAction | null; running: boolean; error: string | null };
export type SelectedRun = { detail: RunDetail | null; trace: TraceDetail | null; inspection: RunInspection | null };

export const MAX_RUN_EVIDENCE_BYTES = 1_048_576;
const APPROVED_RUN_EVIDENCE_MEDIA: Readonly<Record<RunEvidenceManifest['mediaClassification'], ReadonlyArray<RunEvidenceManifest['mediaType']>>> = {
  text: ['text/plain', 'text/markdown'],
  structured: ['application/json'],
  image: ['image/png', 'image/jpeg', 'image/webp'],
  document: ['application/pdf'],
};
export function isApprovedRunEvidenceMedia(manifest: RunEvidenceManifest, byteCount: number, mediaType: string): boolean {
  return byteCount >= 0 && byteCount <= MAX_RUN_EVIDENCE_BYTES && byteCount === manifest.byteCount && mediaType === manifest.mediaType && APPROVED_RUN_EVIDENCE_MEDIA[manifest.mediaClassification].includes(manifest.mediaType);
}

/** Format a stable snapshot duration for people while keeping exact timing in the accessible label. */
export function formatDurationMs(durationMs: number | null): string {
  if (durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) return 'Unavailable';
  const milliseconds = Math.round(durationMs);
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) {
    const seconds = milliseconds / 1_000;
    return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
  }
  if (milliseconds < 3_600_000) {
    const minutes = Math.floor(milliseconds / 60_000);
    const seconds = Math.floor((milliseconds % 60_000) / 1_000);
    return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
  return `${hours}h ${String(minutes).padStart(2, '0')}m`;
}

export function durationAriaLabel(durationMs: number | null): string {
  if (durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) return 'Duration unavailable';
  return `${durationMs} milliseconds`;
}

export type ObservatoryComparisonState = 'idle' | 'loading' | 'refreshing' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'partial' | 'malformed' | 'rejected' | 'unavailable' | 'service_down' | 'error';
export type ObservatoryComparison = {
  readonly leftRunId: string | null;
  readonly rightRunId: string | null;
  readonly result: RunComparison | null;
  readonly state: ObservatoryComparisonState;
  readonly error: string | null;
  readonly updatedAt: string | null;
};
export const OBSERVATORY_STALE_AFTER_MS = 5 * 60 * 1000;
export function isOverviewStale(observedAt: string, now = Date.now()): boolean {
  const timestamp = Date.parse(observedAt);
  return !Number.isFinite(timestamp) || now - timestamp > OBSERVATORY_STALE_AFTER_MS;
}
/** Convert the value from a datetime-local control as UTC, never browser-local time. */
export function datetimeLocalToUtc(value: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) return null;
  const iso = `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6] ?? '00'}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
export function normalizeRunFilters(filters: RunFilters): RunFilters {
  const from = filters.from ? datetimeLocalToUtc(filters.from) : undefined;
  const to = filters.to ? datetimeLocalToUtc(filters.to) : undefined;
  return { status: filters.status, component: filters.component, model: filters.model, deployment: filters.deployment, ...(from ? { from } : {}), ...(to ? { to } : {}) };
}
/** The overview and run catalogue are separate bounded projections. */
export function needsRunReconciliation(overview: Overview | null, runs: RunPage | null): boolean {
  // A non-null cursor means this page is only a prefix of the ordered run
  // projection. Its item count cannot be compared with the overview total.
  return Boolean(overview && runs && runs.nextCursor === null && overview.runCount > runs.items.length);
}
/** A successful run detail can predate trace materialization. */
export function needsSelectedRunReconciliation(detail: RunDetail | null, trace: TraceDetail | null): boolean {
  return Boolean(detail && (!detail.traceId || !trace));
}
export type { ComponentCollection, ComponentDefinition, ComponentDependencyTopology, ComponentHealth, DeploymentCollection, HealthHistoryPage, LiveEvent, Overview, RetentionAction, RunDetail, RunInspection, RunPage, SelfMonitoringSnapshot, TraceDetail };
