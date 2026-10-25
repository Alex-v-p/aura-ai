import type {
  ComponentHealth,
  DashboardVisualization,
  EvaluationRegressionPage,
  OperationalAnalyticsSnapshot,
  RunComparison,
  RunPage,
  SavedQuery,
  SavedQueryDefinition,
  SavedQueryExecutionFreshness,
  SavedQueryExecutionResult,
  SavedQueryExecutionState,
} from '@aura/observatory-api-client';
import { parseOperationalAnalyticsSnapshot, parseRunComparison } from '../../observatory-models';

export type DashboardExecutionResult =
  | { readonly kind: 'analytics'; readonly value: OperationalAnalyticsSnapshot }
  | { readonly kind: 'run_search' | 'trace_search'; readonly value: RunPage }
  | { readonly kind: 'run_comparison'; readonly value: RunComparison }
  | { readonly kind: 'regression_search'; readonly value: EvaluationRegressionPage }
  | { readonly kind: 'component_health'; readonly value: ComponentHealth };

export type DashboardExecutionProjection = {
  readonly state: SavedQueryExecutionState;
  readonly partial: boolean;
  readonly snapshotAt: string;
  readonly freshness: SavedQueryExecutionFreshness;
  readonly nextCursor: string | null;
  readonly snapshot: OperationalAnalyticsSnapshot | null;
  readonly result: DashboardExecutionResult | null;
  readonly error: string | null;
};

/** Panel identity is part of the key because one saved query may be rendered by multiple visualizations. */
export const dashboardExecutionKey = (query: SavedQuery, panelId?: string, visualization?: DashboardVisualization): string => `${query.queryId}:${query.queryVersion}:${panelId ?? 'query'}:${visualization ?? 'default'}`;

/** Every closed saved-query definition is executed by the exact-version server resource. */
export const dashboardOperationForKind = (): string => 'executeSavedQuery';

export const incompatibleDashboardQuery = (definition: SavedQueryDefinition, visualization: DashboardVisualization): string | null => {
  const supported: Record<SavedQueryDefinition['kind'], ReadonlyArray<DashboardVisualization>> = {
    analytics: ['line', 'area', 'bar', 'stat', 'table'],
    run_search: ['table'],
    trace_search: ['trace', 'table'],
    run_comparison: ['comparison', 'table'],
    regression_search: ['regression', 'table'],
    component_health: ['line', 'area', 'bar', 'stat', 'table'],
  };
  return supported[definition.kind].includes(visualization) ? null : `Visualization “${visualization}” is incompatible with the ${definition.kind} saved query. Choose a supported visualization before running this panel.`;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;
const page = <T>(value: unknown, label: string): { readonly items: ReadonlyArray<T>; readonly nextCursor: string | null; readonly snapshotAt: string; readonly partial: boolean } => {
  if (!isRecord(value) || !Array.isArray(value['items']) || (value['nextCursor'] !== null && typeof value['nextCursor'] !== 'string') || typeof value['snapshotAt'] !== 'string' || typeof value['partial'] !== 'boolean') throw new Error(`${label} response was malformed.`);
  return value as unknown as { readonly items: ReadonlyArray<T>; readonly nextCursor: string | null; readonly snapshotAt: string; readonly partial: boolean };
};
const parseRunPage = (value: unknown, label: string): RunPage => {
  const parsed = page<unknown>(value, label);
  if (parsed.items.length > 100 || !parsed.items.every((item) => isRecord(item) && typeof item['runId'] === 'string' && typeof item['status'] === 'string' && typeof item['startedAt'] === 'string' && typeof item['deploymentId'] === 'string' && Array.isArray(item['componentIds']))) throw new Error(`${label} response contained an invalid run summary.`);
  return parsed as unknown as RunPage;
};
const parseRegressionPage = (value: unknown): EvaluationRegressionPage => {
  const parsed = page<unknown>(value, 'regression search');
  if (parsed.items.length > 100 || !parsed.items.every((item) => isRecord(item) && typeof item['regressionId'] === 'string' && typeof item['decision'] === 'string' && item['capturePolicy'] === 'metadata_only')) throw new Error('Regression search response contained invalid metadata.');
  return parsed as unknown as EvaluationRegressionPage;
};
const parseComponentHealth = (value: unknown): ComponentHealth => {
  if (!isRecord(value) || typeof value['componentId'] !== 'string' || !isRecord(value['summary']) || !Array.isArray(value['points']) || value['points'].length > 200) throw new Error('Component health response was malformed.');
  const summary = value['summary'];
  if (!Number.isSafeInteger(summary['sampleCount']) || (summary['latencyP50Ms'] !== null && typeof summary['latencyP50Ms'] !== 'number') || (summary['latencyP95Ms'] !== null && typeof summary['latencyP95Ms'] !== 'number') || (summary['errorRate'] !== null && typeof summary['errorRate'] !== 'number') || !isRecord(summary['outcomes'])) throw new Error('Component health summary was malformed.');
  if (!value['points'].every((point) => isRecord(point) && typeof point['startAt'] === 'string' && typeof point['endAt'] === 'string' && isRecord(point['summary']))) throw new Error('Component health points were malformed.');
  return value as unknown as ComponentHealth;
};
const validExecutionStates: ReadonlyArray<SavedQueryExecutionState> = ['complete', 'partial', 'unavailable', 'incompatible'];
const validFreshnessStates: ReadonlyArray<SavedQueryExecutionFreshness['state']> = ['fresh', 'stale', 'unavailable'];
const parseFreshness = (value: unknown): SavedQueryExecutionFreshness => {
  if (!isRecord(value) || !validFreshnessStates.includes(value['state'] as SavedQueryExecutionFreshness['state']) || (value['measuredAt'] !== null && typeof value['measuredAt'] !== 'string') || (value['lagMs'] !== null && (typeof value['lagMs'] !== 'number' || value['lagMs'] < 0))) throw new Error('Saved query execution freshness was malformed.');
  return value as unknown as SavedQueryExecutionFreshness;
};

/** Maps the generated closed execution union to the product's typed panel projection. */
export const parseDashboardExecution = (value: unknown, expected: SavedQuery): DashboardExecutionProjection => {
  if (!isRecord(value) || !validExecutionStates.includes(value['state'] as SavedQueryExecutionState) || value['kind'] !== expected.definition.kind || !isRecord(value['query']) || value['query']['queryId'] !== expected.queryId || value['query']['queryVersion'] !== expected.queryVersion || typeof value['snapshotAt'] !== 'string') throw new Error('Saved query execution response was malformed.');
  const raw = value as unknown as SavedQueryExecutionResult;
  const freshness = parseFreshness(value['freshness']);
  const rawResult = value['result'];
  let result: DashboardExecutionResult | null = null;
  if (rawResult !== null) {
    switch (raw.kind) {
      case 'analytics': { const parsed = parseOperationalAnalyticsSnapshot(rawResult); if (!parsed) throw new Error('Saved query returned malformed analytics data.'); result = { kind: raw.kind, value: parsed }; break; }
      case 'run_search': result = { kind: raw.kind, value: parseRunPage(rawResult, 'run search') }; break;
      case 'trace_search': result = { kind: raw.kind, value: parseRunPage(rawResult, 'trace search') }; break;
      case 'run_comparison': { const parsed = parseRunComparison(rawResult); if (!parsed) throw new Error('Saved query returned malformed run comparison data.'); result = { kind: raw.kind, value: parsed }; break; }
      case 'regression_search': result = { kind: raw.kind, value: parseRegressionPage(rawResult) }; break;
      case 'component_health': result = { kind: raw.kind, value: parseComponentHealth(rawResult) }; break;
    }
  }
  const nextCursor = 'nextCursor' in raw && typeof raw.nextCursor === 'string' ? raw.nextCursor : null;
  const error = raw.state === 'incompatible' ? 'The saved query could not be executed for this exact version.' : raw.state === 'unavailable' ? 'The saved query measurement service is unavailable.' : null;
  return { state: raw.state, partial: raw.partial, snapshotAt: raw.snapshotAt, freshness, nextCursor, snapshot: result?.kind === 'analytics' ? result.value : null, result, error };
};
