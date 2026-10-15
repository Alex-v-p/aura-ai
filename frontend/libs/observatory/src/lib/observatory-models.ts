import type {
  ComponentCollection,
  ComponentHealth,
  CreateRetentionActionRequest,
  DeploymentCollection,
  LiveEvent,
  Overview,
  RetentionAction,
  RunDetail,
  RunPage,
  TraceDetail,
} from '@aura/observatory-api-client';
import { OBSERVATORY_API_OPERATIONS } from '@aura/observatory-api-client';

export type ObservatoryViewState = 'loading' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'error';
export type ObservatoryResource = 'overview' | 'runs' | 'components' | 'deployments';
export type ObservatoryResourceState = 'loading' | 'ready' | 'empty' | 'stale' | 'error';
export function ownerLoginHref(baseUrl = ''): string {
  return `${baseUrl}${OBSERVATORY_API_OPERATIONS.startOwnerLogin.pathTemplate}?${new URLSearchParams({ return_to: '/' }).toString()}`;
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
export type RetentionRequest = CreateRetentionActionRequest & { confirmation: 'delete-before' | 'purge-all' };
export type RetentionState = { action: RetentionAction | null; running: boolean; error: string | null };
export type SelectedRun = { detail: RunDetail | null; trace: TraceDetail | null };
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
export type { ComponentCollection, ComponentHealth, DeploymentCollection, LiveEvent, Overview, RetentionAction, RunDetail, RunPage, TraceDetail };
