import { Injectable, computed, effect, inject, signal } from '@angular/core';
import type {
  AnalyticsBucket,
  WorkflowFailureBreakdown,
  WorkflowLatencySeries,
  WorkflowLatencySummary,
  WorkflowLifecycleCount,
  WorkflowMonitoringSnapshot,
  WorkflowMonitoringUnavailableSignal,
  WorkflowMonitoringPartialReason,
  WorkflowOutcomeSeries,
  WorkflowRunTraceReference,
} from '@aura/observatory-api-client';
import { blankInput, OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort } from '../../observatory-gateway';
import { ObservatoryStore } from '../../observatory-store';
import { isWorkflowBucket, validWorkflowUtcWindow, workflowMonitoringRevisionChanged } from './workflow-monitoring-contracts';
import { parseWorkflowMonitoringSnapshot } from './workflow-monitoring-parser';

export type WorkflowMonitoringState = 'loading' | 'ready' | 'empty' | 'partial' | 'stale' | 'disconnected' | 'malformed' | 'unauthorized' | 'unavailable' | 'error';
export type WorkflowMonitoringFilters = { readonly from: string; readonly to: string; readonly bucket: AnalyticsBucket };
export type WorkflowMonitoringResource = { readonly snapshot: WorkflowMonitoringSnapshot | null; readonly state: WorkflowMonitoringState; readonly error: string | null; readonly lastFetchedAt: string | null };

export const defaultWorkflowMonitoringFilters = (): WorkflowMonitoringFilters => {
  const to = new Date();
  const from = new Date(to.getTime() - 24 * 60 * 60 * 1000);
  return { from: utcInput(from), to: utcInput(to), bucket: 'auto' };
};

const utcInput = (date: Date): string => date.toISOString().slice(0, 16);
const initialResource = (): WorkflowMonitoringResource => ({ snapshot: null, state: 'loading', error: null, lastFetchedAt: null });
const isUnavailable = (error: unknown): boolean => error instanceof Error && /\b(502|503|504)\b|unavailable|network|failed to fetch|timeout/i.test(error.message);

@Injectable()
export class WorkflowMonitoringStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  private readonly observatoryStore = inject(ObservatoryStore, { optional: true });
  private sequence = 0;
  private inFlight = false;
  private pending = false;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private observedAuthGeneration = -1;
  private observedWorkflowRevision = 0;
  private observedDisconnected = false;
  readonly filters = signal<WorkflowMonitoringFilters>(defaultWorkflowMonitoringFilters());
  readonly resource = signal<WorkflowMonitoringResource>(initialResource());
  readonly state = computed(() => this.resource().state);
  readonly snapshot = computed(() => this.resource().snapshot);
  readonly error = computed(() => this.resource().error);

  constructor() {
    effect(() => {
      const authGeneration = this.observatoryStore?.authGenerationRevision() ?? 0;
      const disconnected = this.observatoryStore?.liveDisconnected() ?? false;
      const invalidation = this.observatoryStore?.workflowMonitoringRevision() ?? 0;
      const revisionChanged = workflowMonitoringRevisionChanged(this.observedWorkflowRevision, invalidation);
      const reconnected = this.observedDisconnected && !disconnected;
      if (this.observedAuthGeneration >= 0 && authGeneration !== this.observedAuthGeneration) this.resetSessionState();
      this.observedAuthGeneration = authGeneration;
      if (revisionChanged) this.observedWorkflowRevision = invalidation;
      this.observedDisconnected = disconnected;
      const resource = this.resource();
      if (disconnected && resource.snapshot && resource.state !== 'disconnected') this.resource.update((value) => ({ ...value, state: 'disconnected', error: 'Live updates are disconnected. Showing the last known workflow snapshot.' }));
      if (!disconnected && (revisionChanged || reconnected) && resource.snapshot) this.scheduleRefresh();
    });
  }

  async load(): Promise<void> {
    if (this.inFlight) { this.pending = true; return; }
    this.inFlight = true;
    const sequence = ++this.sequence;
    const previous = this.resource().snapshot;
    this.resource.update((value) => ({ ...value, state: previous ? 'stale' : 'loading', error: null }));
    try {
      await this.gateway.ensureSession();
      const filters = this.filters();
      const from = toUtc(filters.from); const to = toUtc(filters.to);
      if (!validWorkflowUtcWindow(from, to) || !isWorkflowBucket(filters.bucket)) { this.resource.update((value) => ({ ...value, state: previous ? 'stale' : 'error', error: 'The UTC workflow monitoring window is invalid or exceeds 31 days.' })); return; }
      const response = await this.gateway.execute('getWorkflowMonitoring', { ...blankInput('getWorkflowMonitoring'), query: { from, to, bucket: filters.bucket, series_limit: 12, point_limit: 200 } });
      if (sequence !== this.sequence) return;
      const snapshot = parseWorkflowMonitoringSnapshot(response);
      if (!snapshot) { this.resource.update((value) => ({ ...value, state: previous ? 'stale' : 'malformed', error: 'The workflow monitoring response was malformed or contained unsupported metadata.' })); return; }
      const hasMeasurements = snapshot.lifecycleCounts.length || snapshot.series.length || snapshot.latencySummaries.length || snapshot.failureBreakdowns.length || snapshot.recentCorrelations.length;
      // Query/transport state is separate from source freshness. A successful
      // empty snapshot remains empty even when its source signals are unavailable;
      // freshness is rendered from the accepted snapshot metadata.
      const state: WorkflowMonitoringState = this.observatoryStore?.liveDisconnected() ? 'disconnected' : snapshot.partial ? 'partial' : hasMeasurements ? 'ready' : 'empty';
      this.resource.set({ snapshot, state, error: null, lastFetchedAt: new Date().toISOString() });
    } catch (error) {
      if (sequence !== this.sequence) return;
      if (error instanceof ObservatorySessionRequiredError) { this.resource.update((value) => ({ ...value, state: 'unauthorized', error: 'Your owner session has ended. Sign in again to view workflow monitoring.' })); this.observatoryStore?.requireSession('Your owner session has ended. Sign in again to view workflow monitoring.'); }
      else this.resource.update((value) => ({ ...value, state: isUnavailable(error) ? 'unavailable' : previous ? 'stale' : 'error', error: previous ? 'Refresh failed; the previous workflow snapshot remains visible.' : 'Workflow monitoring is unavailable. Retry when Observatory recovers.' }));
    } finally {
      this.inFlight = false;
      if (this.pending) { this.pending = false; void this.load(); }
    }
  }

  refresh(): void { void this.load(); }
  setFilters(filters: WorkflowMonitoringFilters): void { this.filters.set(filters); void this.load(); }
  resetSessionState(): void { this.sequence += 1; this.pending = false; this.filters.set(defaultWorkflowMonitoringFilters()); this.resource.set(initialResource()); }
  stop(): void { this.sequence += 1; this.pending = false; if (this.refreshTimer) clearTimeout(this.refreshTimer); this.refreshTimer = null; }

  private scheduleRefresh(): void {
    if (this.refreshTimer) return;
    if (this.inFlight) { this.pending = true; return; }
    this.refreshTimer = setTimeout(() => { this.refreshTimer = null; void this.load(); }, 400);
  }
}

const toUtc = (value: string): string => {
  const date = new Date(value.length === 16 ? `${value}:00Z` : value);
  return Number.isFinite(date.getTime()) ? date.toISOString() : value;
};

export type { WorkflowFailureBreakdown, WorkflowLatencySeries, WorkflowLatencySummary, WorkflowLifecycleCount, WorkflowMonitoringSnapshot, WorkflowMonitoringUnavailableSignal, WorkflowMonitoringPartialReason, WorkflowOutcomeSeries, WorkflowRunTraceReference };
