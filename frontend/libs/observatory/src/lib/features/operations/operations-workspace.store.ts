import { Injectable, computed, inject, signal } from '@angular/core';
import type {
  AlertAcknowledgementReason,
  AlertInstance,
  AlertRule,
  AlertSuppression,
  Annotation,
  AnnotationTargetType,
  CreateAlertSuppressionRequest,
  Dashboard,
  DashboardPreset,
  Incident,
  IncidentImpact,
  IncidentState,
  OperationalSeverity,
  OperationalAnalyticsSnapshot,
  DashboardVisualization,
  SavedQueryDefinition,
  SavedQuery,
  SavedQueryExecutionFreshness,
} from '@aura/observatory-api-client';
import { blankInput, OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort } from '../../observatory-gateway';
import { ObservatoryStore } from '../../observatory-store';
import { dashboardExecutionKey, incompatibleDashboardQuery, parseDashboardExecution, type DashboardExecutionResult } from './operations-dashboard-contracts';
export { dashboardExecutionKey, dashboardOperationForKind, incompatibleDashboardQuery, parseDashboardExecution, type DashboardExecutionResult } from './operations-dashboard-contracts';

export type OperationsWorkspaceState = 'loading' | 'ready' | 'partial' | 'stale' | 'disconnected' | 'rejected' | 'unavailable' | 'unauthorized' | 'error';
export type OperationsWorkspaceTab = 'dashboards' | 'queries' | 'alerts' | 'incidents' | 'annotations';

type Page<T> = { readonly items: ReadonlyArray<T>; readonly nextCursor: string | null; readonly snapshotAt: string; readonly partial: boolean };
export interface OperationsWorkspaceData {
  readonly presets: Page<DashboardPreset> | null;
  readonly dashboards: Page<Dashboard> | null;
  readonly queries: Page<SavedQuery> | null;
  readonly rules: Page<AlertRule> | null;
  readonly instances: Page<AlertInstance> | null;
  readonly suppressions: Page<AlertSuppression> | null;
  readonly incidents: Page<Incident> | null;
  readonly annotations: Page<Annotation> | null;
}
export type DashboardExecution = { readonly state: 'loading' | 'ready' | 'partial' | 'unavailable' | 'error' | 'incompatible'; readonly kind: SavedQueryDefinition['kind']; readonly snapshot: OperationalAnalyticsSnapshot | null; readonly result: DashboardExecutionResult | null; readonly freshness: SavedQueryExecutionFreshness | null; readonly snapshotAt: string | null; readonly nextCursor: string | null; readonly error: string | null };

/** Panel identity is part of the key because one saved query may be rendered by multiple visualizations. */

const emptyData = (): OperationsWorkspaceData => ({ presets: null, dashboards: null, queries: null, rules: null, instances: null, suppressions: null, incidents: null, annotations: null });
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;
const page = <T>(value: unknown, label: string): Page<T> => {
  if (!isRecord(value) || !Array.isArray(value['items']) || (value['nextCursor'] !== null && typeof value['nextCursor'] !== 'string') || typeof value['snapshotAt'] !== 'string' || typeof value['partial'] !== 'boolean') throw new Error(`${label} response was malformed.`);
  return value as unknown as Page<T>;
};
const idempotency = (): string => typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `operations-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const now = (): string => new Date().toISOString();

/** Ephemeral owner operations state. It intentionally has no storage, cache, or URL synchronization. */
@Injectable()
export class OperationsWorkspaceStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  private readonly observatoryStore = inject(ObservatoryStore, { optional: true });
  private generation = 0;
  private loadInFlight = false;
  private loadPending = false;
  private liveRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private liveOpen = false;
  readonly state = signal<OperationsWorkspaceState>('loading');
  readonly data = signal<OperationsWorkspaceData>(emptyData());
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly lastUpdatedAt = signal<string | null>(null);
  readonly lastFetchedAt = signal<string | null>(null);
  readonly rejectedScopes = signal<ReadonlyArray<string>>([]);
  readonly unavailableScopes = signal<ReadonlyArray<string>>([]);
  readonly activeTab = signal<OperationsWorkspaceTab>('dashboards');
  readonly dashboardExecutions = signal<Record<string, DashboardExecution>>({});
  readonly alertCount = computed(() => this.data().instances?.items.filter((item) => item.state === 'firing' && !item.suppressed).length ?? 0);
  readonly openIncidentCount = computed(() => this.data().incidents?.items.filter((item) => item.state !== 'closed' && item.state !== 'resolved').length ?? 0);

  async load(): Promise<void> {
    if (this.loadInFlight) {
      this.loadPending = true;
      return;
    }
    this.loadInFlight = true;
    try {
      await this.loadSnapshot();
    } finally {
      this.loadInFlight = false;
      if (this.loadPending) {
        this.loadPending = false;
        void this.load();
      }
    }
  }

  private async loadSnapshot(): Promise<void> {
    const generation = ++this.generation;
    this.dashboardExecutions.update((executions) => Object.fromEntries(Object.entries(executions).map(([key, execution]) => [key, execution.snapshot || execution.result ? { ...execution, state: 'loading', error: null } : execution])) as Record<string, DashboardExecution>);
    this.state.set('loading');
    this.error.set(null);
    try {
      await this.gateway.ensureSession();
      const results = await Promise.allSettled([
        this.gateway.execute('listDashboardPresets', { ...blankInput('listDashboardPresets'), query: { page_size: 50 } }),
        this.gateway.execute('listDashboards', { ...blankInput('listDashboards'), query: { page_size: 50 } }),
        this.gateway.execute('listSavedQueries', { ...blankInput('listSavedQueries'), query: { page_size: 50 } }),
        this.gateway.execute('listAlertRules', { ...blankInput('listAlertRules'), query: { page_size: 50 } }),
        this.gateway.execute('listAlertInstances', { ...blankInput('listAlertInstances'), query: { page_size: 50 } }),
        this.gateway.execute('listAlertSuppressions', { ...blankInput('listAlertSuppressions'), query: { page_size: 50 } }),
        this.gateway.execute('listIncidents', { ...blankInput('listIncidents'), query: { page_size: 50 } }),
        this.gateway.execute('listAnnotations', { ...blankInput('listAnnotations'), query: { page_size: 50 } }),
      ]);
      if (generation !== this.generation) return;
      if (results.some((result) => result.status === 'rejected' && isSessionError(result.reason))) throw new ObservatorySessionRequiredError();
      const labels = ['dashboard presets', 'dashboards', 'saved queries', 'alert rules', 'alert instances', 'suppressions', 'incidents', 'annotations'];
      const parsed = results.map((result, index) => result.status === 'fulfilled' ? page<never>(result.value, labels[index]) : null);
      const next: OperationsWorkspaceData = {
        presets: parsed[0] as Page<DashboardPreset> | null,
        dashboards: parsed[1] as Page<Dashboard> | null,
        queries: parsed[2] as Page<SavedQuery> | null,
        rules: parsed[3] as Page<AlertRule> | null,
        instances: parsed[4] as Page<AlertInstance> | null,
        suppressions: parsed[5] as Page<AlertSuppression> | null,
        incidents: parsed[6] as Page<Incident> | null,
        annotations: parsed[7] as Page<Annotation> | null,
      };
      this.data.set(next);
      const rejected = results.flatMap((result, index) => result.status === 'rejected' ? [labels[index]] : []);
      const unavailable = results.flatMap((result, index) => result.status === 'rejected' && isUnavailableError(result.reason) ? [labels[index]] : []);
      const rejectedOnly = rejected.filter((label) => !unavailable.includes(label));
      this.rejectedScopes.set(rejectedOnly);
      this.unavailableScopes.set(unavailable);
      const partial = results.some((result) => result.status === 'fulfilled' && result.value.partial);
      this.state.set(unavailable.length && !this.hasData() ? 'unavailable' : rejectedOnly.length ? 'rejected' : rejected.length || partial ? 'partial' : 'ready');
      this.error.set(unavailable.length || rejectedOnly.length ? `${unavailable.length ? `Unavailable views: ${unavailable.join(', ')}. ` : ''}${rejectedOnly.length ? `Rejected views: ${rejectedOnly.join(', ')}.` : ''}`.trim() : null);
      if (rejected.length) this.markDashboardRefreshFailed('One or more operation views could not be refreshed.');
      const observedAt = Object.values(next).flatMap((value) => value ? [value.snapshotAt] : []).sort().at(-1) ?? null;
      this.lastUpdatedAt.set(observedAt);
      this.lastFetchedAt.set(new Date().toISOString());
      this.connectLive();
    } catch (cause) {
      if (generation !== this.generation) return;
      if (cause instanceof ObservatorySessionRequiredError) {
        this.state.set('unauthorized');
        this.error.set('Your owner session has ended. Sign in again to view Observatory operations.');
        this.observatoryStore?.requireSession(this.error() ?? 'Owner Observatory session required.');
        return;
      }
      this.state.set(isUnavailableError(cause) ? 'unavailable' : this.hasData() ? 'stale' : 'error');
      const message = 'The latest operation snapshot is unavailable. Retry when the service is available.';
      this.error.set(this.hasData() ? `Refresh failed; the previous measured dashboard data remains visible. ${message}` : message);
      this.markDashboardRefreshFailed(message);
    }
  }

  refresh(): void { void this.load(); }
  disconnect(preserveSnapshot = false): void { this.generation += 1; this.loadPending = false; this.gateway.disconnectOperationsLive(); this.liveOpen = false; if (this.liveRefreshTimer) clearTimeout(this.liveRefreshTimer); this.liveRefreshTimer = null; if (!preserveSnapshot) { this.data.set(emptyData()); this.error.set(null); this.notice.set(null); } }

  async createDashboard(name: string, description: string, queryId: string, queryVersion: string): Promise<void> {
    const query = this.data().queries?.items.find((item) => item.queryId === queryId && item.queryVersion === queryVersion);
    if (!query) throw new Error('Select an existing saved query before composing a dashboard.');
    await this.command('create dashboard', 'createDashboard', { name: name.trim(), description: description.trim(), panels: [{ panelId: idempotency(), title: query.name, query: { queryId: query.queryId, queryVersion: query.queryVersion }, visualization: 'table', size: 'full_width', position: 0 }], createdAt: now() });
  }

  async executeDashboardQuery(query: SavedQuery, visualization?: DashboardVisualization, panelId?: string): Promise<void> {
    const key = dashboardExecutionKey(query, panelId, visualization);
    this.dashboardExecutions.update((value) => ({ ...value, [key]: { state: 'loading', kind: query.definition.kind, snapshot: value[key]?.snapshot ?? null, result: value[key]?.result ?? null, freshness: value[key]?.freshness ?? null, snapshotAt: value[key]?.snapshotAt ?? null, nextCursor: value[key]?.nextCursor ?? null, error: null } }));
    try {
      await this.gateway.ensureSession();
      const incompatible = visualization ? incompatibleDashboardQuery(query.definition, visualization) : null;
      if (incompatible) {
        this.dashboardExecutions.update((value) => ({ ...value, [key]: { state: 'incompatible', kind: query.definition.kind, snapshot: value[key]?.snapshot ?? null, result: value[key]?.result ?? null, freshness: value[key]?.freshness ?? null, snapshotAt: value[key]?.snapshotAt ?? null, nextCursor: value[key]?.nextCursor ?? null, error: incompatible } }));
        return;
      }
      const response = await this.gateway.execute('executeSavedQuery', { ...blankInput('executeSavedQuery'), path: { query_id: query.queryId, resource_version: query.queryVersion }, query: {} } as never);
      const projection = parseDashboardExecution(response, query);
      const state = projection.state === 'incompatible' ? 'incompatible' : projection.state === 'unavailable' || projection.freshness.state === 'unavailable' ? 'unavailable' : projection.state === 'partial' ? 'partial' : 'ready';
      this.dashboardExecutions.update((value) => ({ ...value, [key]: { state, kind: query.definition.kind, snapshot: projection.snapshot ?? value[key]?.snapshot ?? null, result: projection.result?.kind === 'analytics' ? null : projection.result ?? value[key]?.result ?? null, freshness: projection.freshness, snapshotAt: projection.snapshotAt, nextCursor: projection.nextCursor, error: projection.error } }));
    } catch (cause) {
      if (cause instanceof ObservatorySessionRequiredError) { this.state.set('unauthorized'); this.error.set('Your owner session has ended. Sign in again to continue.'); this.observatoryStore?.requireSession(this.error() ?? 'Owner Observatory session required.'); }
      this.dashboardExecutions.update((value) => ({ ...value, [key]: { state: 'error', kind: query.definition.kind, snapshot: value[key]?.snapshot ?? null, result: value[key]?.result ?? null, freshness: value[key]?.freshness ?? null, snapshotAt: value[key]?.snapshotAt ?? null, nextCursor: value[key]?.nextCursor ?? null, error: cause instanceof Error ? cause.message : 'Saved query execution failed.' } }));
    }
  }

  private markDashboardRefreshFailed(cause: string): void {
    const message = `Refresh failed; the previous measured panel snapshot is stale. ${cause}`;
    this.dashboardExecutions.update((executions) => Object.fromEntries(Object.entries(executions).map(([key, execution]) => [key, execution.snapshot || execution.result ? { ...execution, state: 'error', error: message } : execution])) as Record<string, DashboardExecution>);
  }

  async createSavedQuery(name: string, definition: Record<string, unknown>): Promise<void> {
    await this.command('create saved query', 'createSavedQuery', { name: name.trim(), definition: definition as never, createdAt: now() });
  }

  async acknowledge(instance: AlertInstance, reason: AlertAcknowledgementReason): Promise<void> {
    await this.command('acknowledge alert', 'acknowledgeAlertInstance', { instanceVersion: instance.instanceVersion, reason, acknowledgedAt: now() }, { instance_id: instance.instanceId });
  }

  async suppress(instance: AlertInstance): Promise<void> {
    const body: CreateAlertSuppressionRequest = { scope: 'instance', target: { resourceType: 'alert_instance', resourceId: instance.instanceId, resourceVersion: instance.instanceVersion }, startsAt: now(), endsAt: null, reason: 'noise_reduction' };
    await this.command('suppress alert', 'createAlertSuppression', body);
  }

  async clearSuppression(suppression: AlertSuppression): Promise<void> {
    await this.command('clear suppression', 'clearAlertSuppression', { expectedVersion: suppression.suppressionVersion, clearedAt: now() }, { suppression_id: suppression.suppressionId }, 'clear-suppression');
  }

  async createIncident(title: string, severity: OperationalSeverity, impact: IncidentImpact, selectedAlertIds: ReadonlySet<string> = new Set()): Promise<void> {
    const instances = this.data().instances?.items ?? [];
    const relatedAlerts = instances.filter((instance) => instance.state === 'firing' && (selectedAlertIds.size === 0 || selectedAlertIds.has(instance.instanceId))).map((instance) => ({ resourceType: 'alert_instance' as const, resourceId: instance.instanceId, resourceVersion: instance.instanceVersion }));
    if (!relatedAlerts.length) throw new Error('Select or retain at least one firing alert before creating an incident.');
    await this.command('create incident', 'createIncident', { title: title.trim(), severity, impact, relatedAlerts, evidence: [], createdAt: now() });
  }

  async transitionIncident(incident: Incident, targetState: IncidentState, impact: IncidentImpact, resolutionNote: string | null): Promise<void> {
    await this.command('transition incident', 'transitionIncident', { expectedVersion: incident.incidentVersion, targetState, impact, resolutionNote: resolutionNote?.trim() || null, transitionedAt: now() }, { incident_id: incident.incidentId }, 'transition-incident');
  }

  async createAnnotation(targetType: AnnotationTargetType, targetId: string, targetVersion: string, text: string): Promise<void> {
    const bounded = text.trim();
    if (!bounded || bounded.length > 2000) throw new Error('Annotation text must be between 1 and 2,000 characters.');
    await this.command('create annotation', 'createAnnotation', { target: { targetType, targetId: targetId.trim(), targetVersion: targetVersion.trim() }, text: bounded, createdAt: now() });
  }

  async deleteAnnotation(annotation: Annotation): Promise<void> {
    await this.command('delete annotation', 'deleteAnnotation', { expectedVersion: annotation.annotationVersion, requestedAt: now() }, { annotation_id: annotation.annotationId, resource_version: annotation.annotationVersion }, 'delete-annotation');
  }

  private async command<K extends 'createDashboard' | 'createSavedQuery' | 'acknowledgeAlertInstance' | 'createAlertSuppression' | 'clearAlertSuppression' | 'createIncident' | 'transitionIncident' | 'createAnnotation' | 'deleteAnnotation'>(label: string, operation: K, body: unknown, path: Record<string, string> = {}, confirmation?: string): Promise<void> {
    if (this.busyAction()) return;
    this.busyAction.set(label);
    this.notice.set(null);
    try {
      await this.gateway.ensureSession();
      await this.gateway.execute(operation, { ...blankInput(operation), path, body, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': idempotency(), ...(confirmation ? { 'X-Observatory-Confirmation': confirmation } : {}) } } as never);
      this.notice.set(`${label[0].toUpperCase()}${label.slice(1)} accepted. Refreshing operation state.`);
      await this.load();
    } catch (cause) {
      if (cause instanceof ObservatorySessionRequiredError) { this.state.set('unauthorized'); this.error.set('Your owner session has ended. Sign in again to continue.'); this.observatoryStore?.requireSession(this.error() ?? 'Owner Observatory session required.'); }
      else this.error.set(`${label[0].toUpperCase()}${label.slice(1)} failed. Your draft is preserved; retry when the service is available.`);
      throw cause;
    } finally { this.busyAction.set(null); }
  }

  private hasData(): boolean { return Object.values(this.data()).some((value) => value !== null); }
  private connectLive(): void {
    if (this.liveOpen) return;
    this.gateway.connectOperationsLive(() => { this.liveOpen = true; if (this.state() === 'disconnected') this.state.set('ready'); }, () => this.scheduleLiveRefresh(), () => { this.liveOpen = false; this.state.set('disconnected'); });
  }
  private scheduleLiveRefresh(): void {
    if (this.liveRefreshTimer) return;
    this.liveRefreshTimer = setTimeout(() => { this.liveRefreshTimer = null; void this.load(); }, 500);
  }
}

const isSessionError = (cause: unknown): boolean => cause instanceof ObservatorySessionRequiredError || (cause instanceof Error && /session|unauthori[sz]ed|401/i.test(cause.message));
const isUnavailableError = (cause: unknown): boolean => cause instanceof Error && /\b(502|503|504)\b|unavailable|network|failed to fetch/i.test(cause.message);
