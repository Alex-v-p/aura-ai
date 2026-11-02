import { Injectable, computed, inject, signal } from '@angular/core';
import { type ComponentHealth, type EvaluationLiveEvent, type RunDetail, type RunEvidenceManifest, type RunInspection, type TraceDetail } from '@aura/observatory-api-client';
import { OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort, blankInput } from './observatory-gateway';
import { ComponentDetailRequestGate, DEFAULT_ANALYTICS_FILTERS, hasSessionRequiredRejection, healthHistoryAfterDisconnect, inspectionEvidenceIsCurrent, isApprovedRunEvidenceMedia, isComparisonInvalidationEvent, isCurrentLoadSequence, isOverviewStale, isRunCorrelatedInvalidation, isRunProjectionInvalidation, LiveRefreshCoordinator, matchesSelectedRunInvalidation, needsRunReconciliation, needsSelectedRunReconciliation, parseAnalyticsMetricCatalogue, parseComponentDefinition, parseComponentDependencyTopology, parseOperationalAnalyticsSnapshot, parseRunComparison, RequestGenerationGate, resourceStateFor, SELECTED_RUN_LOAD_TIMEOUT_MS, selectedRunLoadError, shouldPreserveRunEvidenceDeletionState, type AnalyticsFilters, type AnalyticsResource, type ComponentCatalogueSelection, type ObservatoryAuthState, type ObservatoryComparison, type ObservatoryComparisonState, type ObservatoryHealthHistory, type ObservatoryResource, type ObservatoryResourceState, type ObservatorySnapshot, type RetentionRequest, type RetentionState, type RunFilters, type SelectedRun } from './observatory-models';
import { parseEvaluationLiveEvent } from './features/evaluation/evaluation-dto-adapter';

// The run projection can lag Core ingestion well beyond a normal SSE retry.
// Keep recovery active for one minute, but at a cadence that cannot turn an
// unavailable Observatory into a request storm.
const RUN_RECONCILIATION_DELAY_MS = 2_000;
const SELECTED_RUN_TRAILING_REFRESH_DELAY_MS = 500;
const MAX_RUN_RECONCILIATION_ATTEMPTS = 30;
const LIVE_REFRESH_CADENCE_MS = 500;
const HEALTH_HISTORY_REFRESH_CADENCE_MS = 2_000;
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;
const isRunDetailProjection = (value: unknown): value is RunDetail => isRecord(value) && typeof value['runId'] === 'string' && typeof value['status'] === 'string' && typeof value['startedAt'] === 'string' && Array.isArray(value['observations']);
const isRunInspectionProjection = (value: unknown): value is RunInspection => isRecord(value) && Array.isArray(value['steps']) && Array.isArray(value['partialReasons']) && isRecord(value['completeness']) && Array.isArray(value['completeness']['reasons']);
const isTraceProjection = (value: unknown): value is TraceDetail => isRecord(value) && typeof value['traceId'] === 'string' && Array.isArray(value['spans']);

@Injectable({ providedIn: 'root' })
export class ObservatoryStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  readonly snapshot = signal<ObservatorySnapshot>({ overview: null, runs: null, components: null, deployments: null, state: 'loading', lastUpdatedAt: null, lastEventId: null, error: null, partial: false, resourceErrors: [] });
  readonly healthHistory = signal<ObservatoryHealthHistory>({ page: null, state: 'loading', error: null });
  readonly resourceStates = signal<Record<ObservatoryResource, ObservatoryResourceState>>({ overview: 'loading', runs: 'loading', components: 'loading', deployments: 'loading' });
  readonly filters = signal<RunFilters>({});
  readonly selectedRun = signal<SelectedRun>({ detail: null, trace: null, inspection: null });
  readonly selectedRunId = signal<string | null>(null);
  readonly selectedRunState = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');
  readonly selectedRunError = signal<string | null>(null);
  readonly selectedRunInspectionFreshness = signal<'fresh' | 'stale' | 'loading' | 'unavailable'>('loading');
  readonly selectedRunEvidence = signal<{ readonly runId: string; readonly evidenceId: string; readonly manifest: RunEvidenceManifest; readonly blob: Blob } | null>(null);
  readonly selectedRunEvidenceState = signal<'idle' | 'loading' | 'ready' | 'error' | 'deleting' | 'deleted'>('idle');
  readonly selectedRunEvidenceError = signal<string | null>(null);
  readonly retention = signal<RetentionState>({ action: null, running: false, error: null });
  readonly authState = signal<ObservatoryAuthState>('checking');
  readonly sessionRequired = signal(false);
  readonly liveDisconnected = signal(false);
  readonly evaluationLiveDisconnected = signal(false);
  readonly componentHealthById = signal<Record<string, ComponentHealth>>({});
  readonly componentHealthErrors = signal<Record<string, string>>({});
  readonly componentHealthLoading = signal<Record<string, boolean>>({});
  readonly selectedComponent = signal<ComponentCatalogueSelection | null>(null);
  readonly analytics = signal<AnalyticsResource>({ catalogue: null, snapshot: null, state: 'loading', error: null });
  readonly analyticsFilters = signal<AnalyticsFilters>(DEFAULT_ANALYTICS_FILTERS);
  readonly comparison = signal<ObservatoryComparison>({ leftRunId: null, rightRunId: null, result: null, state: 'idle', error: null, updatedAt: null });
  /** Monotonic in-memory invalidation for evaluation workspaces sharing the authenticated SSE. */
  readonly evaluationRevision = signal(0);
  readonly evaluationChange = signal<EvaluationLiveEvent | null>(null);
  /** Monotonic session boundary observed by product feature stores. */
  readonly authGenerationRevision = signal(0);
  /** Generic metadata-only invalidation for aggregate workflow monitoring. */
  readonly workflowMonitoringRevision = signal(0);
  readonly components = computed(() => this.snapshot().components?.items ?? []);
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private runReconciliationTimer: ReturnType<typeof setTimeout> | null = null;
  private runReconciliationAttempts = 0;
  private runsViewActive = false;
  private analyticsViewActive = false;
  private comparisonViewActive = false;
  private selectedRunReconciliationTimer: ReturnType<typeof setTimeout> | null = null;
  private selectedRunReconciliationAttempts = 0;
  private selectedRunViewActive = false;
  /** The active selected-run request owns the next invalidation; do not supersede it. */
  private selectedRunLoadInFlightSequence: number | null = null;
  private selectedRunAbort: AbortController | null = null;
  private selectedRunRefreshPending = false;
  private selectedRunRetryPending = false;
  private selectedRunEvidenceRequest = 0;
  private selectedRunEvidenceAbort: AbortController | null = null;
  private readonly evidenceDeletionKeys = new Map<string, string>();
  private evidenceDeletionPoll: ReturnType<typeof setTimeout> | null = null;
  private loadSequence = 0;
  private openRunSequence = 0;
  private authGeneration = 0;
  private hadAuthenticatedSession = false;
  private pendingRecoveryState: ObservatoryAuthState | null = null;
  private readonly componentHealthRequests = new RequestGenerationGate();
  // Run projections can emit bursts while a request is being materialized.
  // Keep one active refresh and one trailing refresh, with a bounded cadence.
  private readonly liveRefresh = new LiveRefreshCoordinator(LIVE_REFRESH_CADENCE_MS);
  private lastEvaluationEventId: string | null = null;
  private readonly liveEventIds = new Set<string>();
  private readonly healthHistoryRefresh = new LiveRefreshCoordinator(HEALTH_HISTORY_REFRESH_CADENCE_MS);
  private readonly catalogRefresh = new LiveRefreshCoordinator();
  private readonly analyticsRefresh = new LiveRefreshCoordinator(LIVE_REFRESH_CADENCE_MS);
  private readonly comparisonRefresh = new LiveRefreshCoordinator(LIVE_REFRESH_CADENCE_MS);
  private catalogLoadSequence = 0;
  private analyticsLoadSequence = 0;
  /** Last successful catalogue pages survive a later refresh failure. */
  private lastKnownComponents: ObservatorySnapshot['components'] = null;
  private lastKnownDeployments: ObservatorySnapshot['deployments'] = null;
  private readonly componentDetailRequests = new ComponentDetailRequestGate();
  private healthHistorySequence = 0;
  private comparisonLoadSequence = 0;

  async start(initialRecoveryState: ObservatoryAuthState | null = null): Promise<void> {
    await this.runSessionLifecycle(initialRecoveryState);
  }

  private async runSessionLifecycle(initialRecoveryState: ObservatoryAuthState | null = null): Promise<void> {
    if (initialRecoveryState) this.pendingRecoveryState = initialRecoveryState;
    if (this.authState() !== 'authenticated') this.authState.set('checking');
    await this.loadLiveSnapshot();
    if (this.authState() === 'authenticated' && !this.sessionRequired()) {
      await this.loadHealthHistory();
      this.connectLive();
      this.connectEvaluationLive();
    }
  }

  async load(): Promise<void> {
    this.catalogLoadSequence += 1;
    this.catalogRefresh.cancel();
    const sequence = ++this.loadSequence;
    const previous = this.snapshot();
    this.resourceStates.update((states) => ({
      overview: previous.overview ? states.overview : 'loading',
      runs: previous.runs ? states.runs : 'loading',
      components: previous.components ? states.components : 'loading',
      deployments: previous.deployments ? states.deployments : 'loading',
    }));
    this.snapshot.update((value) => ({ ...value, state: value.overview ? value.state : 'loading', error: null }));
    try {
      await this.gateway.ensureSession();
      if (!isCurrentLoadSequence(sequence, this.loadSequence)) return;
      this.sessionRequired.set(false);
      this.authState.set('authenticated');
      this.hadAuthenticatedSession = true;
      this.pendingRecoveryState = null;
      const results = await Promise.allSettled([
        this.gateway.execute('getOverview', blankInput('getOverview')),
        this.gateway.execute('listRuns', { ...blankInput('listRuns'), query: this.filters() }),
        this.gateway.execute('listComponents', blankInput('listComponents')),
        this.gateway.execute('listDeployments', blankInput('listDeployments')),
      ]);
      if (!isCurrentLoadSequence(sequence, this.loadSequence)) return;
      // A parallel resource request can discover that the BFF session expired
      // after ensureSession() succeeded. Promote that rejection through the
      // lifecycle catch before assembling partial data, so an expired session
      // can never be rendered as an authenticated partial snapshot.
      if (hasSessionRequiredRejection(results, (reason) => reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const names = ['overview', 'runs', 'components', 'deployments'];
      const resourceErrors = results.flatMap((result, index) => result.status === 'rejected' ? [names[index]] : []);
      const overview = results[0].status === 'fulfilled' ? results[0].value : previous.overview;
      const runs = results[1].status === 'fulfilled' ? results[1].value : previous.runs;
      const components = results[2].status === 'fulfilled' ? results[2].value : previous.components;
      const deployments = results[3].status === 'fulfilled' ? results[3].value : previous.deployments;
      this.resourceStates.set({
        overview: resourceStateFor('overview', overview, results[0].status === 'rejected', overview?.observedAt),
        runs: resourceStateFor('runs', runs, results[1].status === 'rejected'),
        components: resourceStateFor('components', components, results[2].status === 'rejected'),
        deployments: resourceStateFor('deployments', deployments, results[3].status === 'rejected'),
      });
      if (!overview) throw new Error('Observatory overview is unavailable.');
      const latestEventId = this.snapshot().lastEventId ?? runs?.latestObservationId ?? previous.lastEventId;
      this.snapshot.set({ overview, runs, components, deployments, state: resourceErrors.length ? 'stale' : this.stateFor(overview, runs?.items.length ?? 0), lastUpdatedAt: overview.observedAt, lastEventId: latestEventId, error: resourceErrors.length ? `Some Observatory data is unavailable: ${resourceErrors.join(', ')}.` : null, partial: resourceErrors.length > 0, resourceErrors });
      void this.loadHealthHistory();
    } catch (error) {
      if (!isCurrentLoadSequence(sequence, this.loadSequence)) return;
      if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.');
      else if (!this.hadAuthenticatedSession) this.markSignedOut('Owner Observatory identity is unavailable.', 'identity_unavailable');
      if (!this.snapshot().overview) this.resourceStates.update((states) => ({ ...states, overview: 'error' }));
      this.snapshot.update((value) => ({ ...value, state: value.overview ? 'stale' : 'error', error: error instanceof Error ? error.message : 'Observatory is unavailable.' }));
    }
  }

  setFilters(filters: RunFilters): void { this.filters.set(filters); void this.load(); }
  resourceState(resource: ObservatoryResource): ObservatoryResourceState { return this.resourceStates()[resource]; }
  isComponentHealthLoading(componentId: string): boolean { return this.componentHealthLoading()[componentId] === true; }
  connectLive(): void {
    this.liveDisconnected.set(false);
    this.gateway.connectLive(this.handleLiveOpen, this.handleLiveMessage, this.handleLiveError);
  }
  connectEvaluationLive(): void {
    this.evaluationLiveDisconnected.set(false);
    this.gateway.connectEvaluationLive(this.handleEvaluationLiveOpen, this.handleEvaluationLiveMessage, this.handleEvaluationLiveError);
  }
  reconnectLive(): void { this.liveDisconnected.set(true); this.gateway.reconnectLive(this.handleLiveOpen, this.handleLiveMessage, this.handleLiveError); }
  reconnectEvaluationLive(): void { this.evaluationLiveDisconnected.set(true); this.gateway.reconnectEvaluationLive(this.handleEvaluationLiveOpen, this.handleEvaluationLiveMessage, this.handleEvaluationLiveError); }
  disconnectLive(): void { this.gateway.disconnectLive(); this.gateway.disconnectEvaluationLive(); this.liveDisconnected.set(false); this.evaluationLiveDisconnected.set(false); }
  /** Start a fresh bounded catalogue recovery when the Runs workspace becomes active. */
  activateRunsView(): void {
    if (this.sessionRequired() || this.authState() !== 'authenticated') return;
    this.runsViewActive = true;
    this.resetRunReconciliation();
    this.queueRunReconciliation();
    this.queueLiveRefresh();
  }
  /** Stop a Runs-owned recovery generation when its view is destroyed. */
  deactivateRunsView(): void {
    this.runsViewActive = false;
    this.resetRunReconciliation();
  }
  /** Keep analytics state in memory, but only follow live run events while mounted. */
  activateAnalyticsView(): void { this.analyticsViewActive = true; }
  deactivateAnalyticsView(): void { this.analyticsViewActive = false; this.analyticsLoadSequence += 1; this.analyticsRefresh.cancel(); }
  /** Keep comparison state in memory, but only follow live run events while mounted. */
  activateComparisonView(): void { this.comparisonViewActive = true; }
  deactivateComparisonView(): void { this.comparisonViewActive = false; this.comparisonLoadSequence += 1; this.comparisonRefresh.cancel(); }
  /** Keep selected-run state in the root store, but only refresh it while Trace is mounted. */
  activateSelectedRunView(): void {
    this.selectedRunViewActive = true;
    const runId = this.selectedRunId();
    if (runId && this.selectedRunLoadInFlightSequence === null && this.selectedRunState() !== 'ready') void this.openRun(runId);
    if (runId && this.selectedRunEvidenceState() === 'deleting' && this.evidenceDeletionKeys.has(runId) && !this.evidenceDeletionPoll) this.pollEvidenceDeletion(runId, 0);
  }
  deactivateSelectedRunView(): void {
    this.selectedRunViewActive = false;
    this.openRunSequence += 1;
    this.selectedRunLoadInFlightSequence = null;
    this.selectedRunAbort?.abort();
    this.selectedRunAbort = null;
    if (this.evidenceDeletionPoll) { clearTimeout(this.evidenceDeletionPoll); this.evidenceDeletionPoll = null; }
    this.resetSelectedRunReconciliation();
  }
  async openRun(runId: string): Promise<void> {
    // Selecting a run is the route transition into Trace. Mark the selected
    // view active before the first await so an invalidation delivered in the
    // same turn is retained for the trailing refresh instead of being lost
    // between Runs destruction and Trace creation.
    this.selectedRunViewActive = true;
    this.resetSelectedRunReconciliation();
    this.selectedRunAbort?.abort();
    this.selectedRunAbort = null;
    const previousRun = this.selectedRun();
    const preserveSelectedRun = this.selectedRunId() === runId && Boolean(previousRun.detail || previousRun.trace || previousRun.inspection);
    const preserveDeletionStatus = this.selectedRunId() === runId && this.evidenceDeletionKeys.has(runId) && this.selectedRunEvidenceState() === 'deleting';
    if (this.selectedRunId() !== runId) {
      this.evidenceDeletionKeys.delete(this.selectedRunId() ?? '');
      if (this.evidenceDeletionPoll) { clearTimeout(this.evidenceDeletionPoll); this.evidenceDeletionPoll = null; }
    }
    this.selectedRunId.set(runId);
    this.selectedRunInspectionFreshness.set('loading');
    this.clearRunEvidence();
    if (preserveDeletionStatus) this.selectedRunEvidenceState.set('deleting');
    if (!preserveSelectedRun) this.selectedRun.set({ detail: null, trace: null, inspection: null });
    await this.loadSelectedRun(runId, preserveSelectedRun);
  }
  private async loadSelectedRun(runId: string, preserveSelectedRun: boolean): Promise<void> {
    const sequence = ++this.openRunSequence;
    this.selectedRunLoadInFlightSequence = sequence;
    this.selectedRunState.set('loading');
    this.selectedRunError.set(null);
    this.selectedRunInspectionFreshness.set('loading');
    this.selectedRunAbort?.abort();
    const controller = new AbortController();
    let timedOut = false;
    let inspectionFresh = false;
    this.selectedRunAbort = controller;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, SELECTED_RUN_LOAD_TIMEOUT_MS);
    try {
      const [detailResult, inspectionResult] = await Promise.allSettled([
        this.gateway.execute('getRun', { ...blankInput('getRun'), path: { run_id: runId }, query: {} }, controller.signal),
        this.gateway.execute('getRunInspection', { ...blankInput('getRunInspection'), path: { run_id: runId }, query: {} }, controller.signal),
      ]);
      if (sequence !== this.openRunSequence) return;
      // Both projections share the same owner session. Inspect the settled
      // results before preferring a metadata error so an inspection-only 401
      // still purges the authenticated surface globally.
      if (hasSessionRequiredRejection([detailResult, inspectionResult], (reason) => reason instanceof ObservatorySessionRequiredError)) {
        throw new ObservatorySessionRequiredError();
      }
      const validInspection = inspectionResult.status === 'fulfilled' && isRunInspectionProjection(inspectionResult.value) ? inspectionResult.value : null;
      // Inspection is authoritative for disclosed evidence. Reconcile it as
      // soon as it is available, even when detail or trace is delayed, so a
      // captured-to-deleted transition cannot leave old bytes rendered.
      if (validInspection) {
        inspectionFresh = true;
        this.selectedRunInspectionFreshness.set('fresh');
        const current = this.selectedRun();
        const disclosedEvidence = this.selectedRunEvidence();
        if (disclosedEvidence && !inspectionEvidenceIsCurrent(validInspection, disclosedEvidence.manifest)) this.clearRunEvidence();
        this.selectedRun.set({ ...current, inspection: validInspection });
      } else if (inspectionResult.status === 'fulfilled') {
        this.selectedRunInspectionFreshness.set('unavailable');
      }
      if (detailResult.status === 'rejected') throw detailResult.reason;
      const detail = detailResult.value;
      if (!isRunDetailProjection(detail)) throw new Error('The selected run metadata is unavailable.');
      const inspection = validInspection ?? this.selectedRun().inspection;
      this.selectedRunInspectionFreshness.set(validInspection ? 'fresh' : inspection ? 'stale' : 'unavailable');
      const previousTrace = this.selectedRun().trace;
      this.selectedRun.set({ detail, trace: previousTrace, inspection });
      const traceResponse = detail.traceId ? await this.gateway.execute('getTrace', { ...blankInput('getTrace'), path: { trace_id: detail.traceId }, query: {} }, controller.signal) : previousTrace;
      const trace = traceResponse && isTraceProjection(traceResponse) ? traceResponse : previousTrace;
      if (sequence !== this.openRunSequence) return;
      this.selectedRun.set({ detail, trace, inspection });
      if (inspectionResult.status === 'rejected') this.selectedRunError.set('The governed run inspection is unavailable; metadata remains visible.');
      const retryScheduled = needsSelectedRunReconciliation(detail, trace) && this.queueSelectedRunReconciliation(runId);
      this.selectedRunState.set(retryScheduled ? 'loading' : needsSelectedRunReconciliation(detail, trace) ? 'error' : 'ready');
      // A live invalidation can arrive while either snapshot request is in
      // flight. Preserve that pending bit through a converged response so the
      // finally block can schedule exactly one trailing projection refresh.
      if (!needsSelectedRunReconciliation(detail, trace)) this.resetSelectedRunReconciliation(false);
      if (!retryScheduled && needsSelectedRunReconciliation(detail, trace)) this.selectedRunError.set('The selected run trace is not available yet.');
    } catch (error) {
      if (sequence !== this.openRunSequence) return;
      if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; }
      const message = timedOut ? selectedRunLoadError(true) : error instanceof Error ? error.message : selectedRunLoadError(false);
      const hasSelectedEvidence = preserveSelectedRun || Boolean(this.selectedRun().detail || this.selectedRun().trace || this.selectedRun().inspection);
      this.selectedRunInspectionFreshness.set(inspectionFresh ? 'fresh' : this.selectedRun().inspection ? 'stale' : 'unavailable');
      if (!hasSelectedEvidence) this.selectedRun.set({ detail: null, trace: null, inspection: null });
      this.selectedRunError.set(message);
      this.snapshot.update((value) => ({ ...value, error: message, state: 'stale' }));
      // A previously rendered trace remains actionable with an explicit
      // manual retry. Only an initially uncaptured trace gets automatic
      // projection reconciliation; this preserves true refresh failures.
      const retryNeeded = !timedOut && !this.selectedRun().trace;
      const retryScheduled = retryNeeded && this.queueSelectedRunReconciliation(runId);
      this.selectedRunState.set(retryScheduled ? 'loading' : 'error');
      if (!retryScheduled && retryNeeded) this.selectedRunError.set('The selected run trace is not available yet.');
    } finally {
      clearTimeout(timeout);
      if (this.selectedRunAbort === controller) this.selectedRunAbort = null;
      if (this.selectedRunLoadInFlightSequence === sequence) {
        this.selectedRunLoadInFlightSequence = null;
        if ((this.selectedRunRefreshPending || this.selectedRunRetryPending) && this.selectedRunId() === runId && !this.sessionRequired()) {
          const liveRefreshPending = this.selectedRunRefreshPending;
          this.selectedRunRefreshPending = false;
          this.selectedRunRetryPending = false;
          // A live invalidation is already coalesced while the active request
          // was in flight. Give that one trailing refresh the live cadence;
          // projection-gap retries retain the slower bounded recovery delay.
          this.queueSelectedRunReconciliation(runId, liveRefreshPending);
        }
      }
    }
  }
  /** Fetch one manifest only after the owner explicitly discloses it. */
  async loadRunEvidence(manifest: RunEvidenceManifest): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId || manifest.captureState !== 'captured') return;
    this.selectedRunEvidenceAbort?.abort();
    const controller = new AbortController();
    this.selectedRunEvidenceAbort = controller;
    const request = ++this.selectedRunEvidenceRequest;
    this.selectedRunEvidence.set(null);
    this.selectedRunEvidenceState.set('loading');
    this.selectedRunEvidenceError.set(null);
    try {
      const blob = await this.gateway.execute('getRunEvidence', { ...blankInput('getRunEvidence'), path: { run_id: runId, evidence_id: manifest.evidenceId }, query: {} }, controller.signal);
      if (!(blob instanceof Blob)) throw new Error('Evidence response was unavailable.');
      if (!isApprovedRunEvidenceMedia(manifest, blob.size, blob.type)) throw new Error('Evidence media did not satisfy the governed manifest bounds.');
      if (this.selectedRunId() !== runId || request !== this.selectedRunEvidenceRequest) return;
      this.selectedRunEvidence.set({ runId, evidenceId: manifest.evidenceId, manifest, blob });
      this.selectedRunEvidenceState.set('ready');
    } catch (error) {
      if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; }
      if (this.selectedRunId() !== runId || request !== this.selectedRunEvidenceRequest) return;
      if (controller.signal.aborted) return;
      this.selectedRunEvidenceError.set(error instanceof Error ? error.message : 'Evidence is unavailable.');
      this.selectedRunEvidenceState.set('error');
    }
  }
  clearRunEvidence(): void {
    const runId = this.selectedRunId();
    const preserveDeletionState = shouldPreserveRunEvidenceDeletionState(this.selectedRunEvidenceState(), Boolean(runId && this.evidenceDeletionKeys.has(runId)));
    this.selectedRunEvidenceAbort?.abort();
    this.selectedRunEvidenceAbort = null;
    this.selectedRunEvidenceRequest += 1;
    this.selectedRunEvidence.set(null);
    this.selectedRunEvidenceError.set(null);
    if (!preserveDeletionState) this.selectedRunEvidenceState.set('idle');
  }
  async deleteRunEvidence(): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId || this.selectedRunEvidenceState() === 'deleting' || this.selectedRunEvidenceState() === 'loading') return;
    this.clearRunEvidence();
    this.selectedRunEvidenceState.set('deleting');
    this.selectedRunEvidenceError.set(null);
    try {
      await this.gateway.ensureSession();
      const idempotencyKey = this.evidenceDeletionKeys.get(runId) ?? crypto.randomUUID();
      this.evidenceDeletionKeys.set(runId, idempotencyKey);
      const deletion = await this.gateway.execute('deleteRunEvidence', { ...blankInput('deleteRunEvidence'), path: { run_id: runId }, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': idempotencyKey, 'X-Observatory-Confirmation': 'DELETE_RUN_EVIDENCE' } });
      if (this.selectedRunId() !== runId) return;
      this.selectedRunEvidenceState.set(deletion.status === 'failed' ? 'error' : deletion.status === 'completed' ? 'deleted' : 'deleting');
      if (deletion.status === 'failed') { this.selectedRunEvidenceError.set('Evidence deletion could not be completed.'); this.evidenceDeletionKeys.delete(runId); }
      void this.loadSelectedRun(runId, true);
      if (deletion.status === 'accepted' || deletion.status === 'in_progress') this.pollEvidenceDeletion(runId, 0);
    } catch (error) {
      if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; }
      if (this.selectedRunId() !== runId) return;
      this.selectedRunEvidenceError.set(error instanceof Error ? error.message : 'Evidence deletion failed.');
      this.selectedRunEvidenceState.set('error');
    }
  }
  private pollEvidenceDeletion(runId: string, attempt: number): void {
    if (this.evidenceDeletionPoll) clearTimeout(this.evidenceDeletionPoll);
    if (attempt >= 10) { if (this.selectedRunId() === runId && this.selectedRunEvidenceState() === 'deleting') { this.selectedRunEvidenceState.set('error'); this.selectedRunEvidenceError.set('Deletion status did not converge within the bounded retry window. Retry safely with the same command.'); } return; }
    if (!this.selectedRunViewActive || this.selectedRunId() !== runId || this.sessionRequired()) return;
    this.evidenceDeletionPoll = setTimeout(() => {
      this.evidenceDeletionPoll = null;
      if (!this.selectedRunViewActive || this.selectedRunId() !== runId || this.sessionRequired()) return;
      if (this.selectedRunLoadInFlightSequence !== null) { this.pollEvidenceDeletion(runId, attempt); return; }
      void this.loadSelectedRun(runId, true).then(() => {
        if (!this.selectedRunViewActive || this.selectedRunId() !== runId || this.sessionRequired()) return;
        const inspection = this.selectedRun().inspection;
        if (inspection?.completeness.state === 'deleted') { this.selectedRunEvidenceState.set('deleted'); this.evidenceDeletionKeys.delete(runId); return; }
        this.selectedRunEvidenceState.set('deleting');
        this.pollEvidenceDeletion(runId, attempt + 1);
      });
    }, 500);
  }
  /** Compare exactly two in-memory selections; no route, storage, or URL state is used. */
  async compareRuns(leftRunId: string, rightRunId: string): Promise<void> {
    if (!leftRunId || !rightRunId || leftRunId === rightRunId) {
      this.comparison.set({ leftRunId, rightRunId, result: null, state: 'rejected', error: 'Choose two different runs to compare.', updatedAt: null });
      return;
    }
    const generation = ++this.comparisonLoadSequence;
    this.comparisonRefresh.cancel();
    this.comparison.set({ leftRunId, rightRunId, result: null, state: 'loading', error: null, updatedAt: null });
    await this.loadComparison(generation);
  }
  resetComparison(): void {
    this.comparisonLoadSequence += 1;
    this.comparisonRefresh.cancel();
    this.comparison.set({ leftRunId: null, rightRunId: null, result: null, state: 'idle', error: null, updatedAt: null });
  }
  async componentHealth(componentId: string): Promise<ComponentHealth | null> {
    const existing = this.componentHealthById()[componentId];
    if (existing || this.componentHealthErrors()[componentId]) return existing ?? null;
    const requestToken = this.componentHealthRequests.begin(componentId);
    if (!requestToken) return null;
    const generation = this.authGeneration;
    this.componentHealthLoading.update((value) => ({ ...value, [componentId]: true }));
    try { const health = await this.gateway.execute('getComponentHealth', { ...blankInput('getComponentHealth'), path: { component_id: componentId }, query: {} }); if (generation !== this.authGeneration || this.sessionRequired() || !this.componentHealthRequests.isCurrent(requestToken)) return null; this.componentHealthById.update((value) => ({ ...value, [componentId]: health })); return health; } catch (error) { if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.'); else if (generation === this.authGeneration && !this.sessionRequired() && this.componentHealthRequests.isCurrent(requestToken)) this.componentHealthErrors.update((value) => ({ ...value, [componentId]: error instanceof Error ? error.message : 'Component health is unavailable.' })); return null; } finally { const wasCurrent = this.componentHealthRequests.isCurrent(requestToken); this.componentHealthRequests.finish(requestToken); if (wasCurrent) this.componentHealthLoading.update((value) => { const next = { ...value }; delete next[componentId]; return next; }); }
  }
  async openComponent(componentId: string, componentVersion: string): Promise<void> {
    const request = this.componentDetailRequests.begin(componentId, componentVersion);
    const previous = this.selectedComponent();
    const canPreserve = previous?.componentId === componentId && previous.componentVersion === componentVersion;
    const selection: ComponentCatalogueSelection = { componentId, componentVersion, definition: canPreserve ? previous.definition : null, topology: canPreserve ? previous.topology : null, state: canPreserve ? 'partial' : 'loading', error: canPreserve ? 'Refreshing component details…' : null };
    this.selectedComponent.set(selection);
    const generation = this.authGeneration;
    try {
      await this.gateway.ensureSession();
      const results = await Promise.allSettled([
        this.gateway.execute('getComponentDefinition', { ...blankInput('getComponentDefinition'), path: { component_id: componentId, component_version: componentVersion }, query: {} }),
        this.gateway.execute('getComponentDependencies', { ...blankInput('getComponentDependencies'), path: { component_id: componentId, component_version: componentVersion }, query: { depth: 2, max_nodes: 100 } }),
      ]);
      if (generation !== this.authGeneration || this.sessionRequired() || !this.componentDetailRequests.isCurrent(request)) return;
      if (hasSessionRequiredRejection(results, (reason) => reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const parsedDefinition = results[0].status === 'fulfilled' ? parseComponentDefinition(results[0].value) : null;
      const parsedTopology = results[1].status === 'fulfilled' ? parseComponentDependencyTopology(results[1].value) : null;
      const definition = parsedDefinition ?? (canPreserve ? previous.definition : null);
      const topology = parsedTopology ?? (canPreserve ? previous.topology : null);
      const failed = results.flatMap((result, index) => result.status === 'rejected' || (index === 0 ? parsedDefinition === null : parsedTopology === null) ? [index === 0 ? 'definition' : 'dependencies'] : []);
      this.selectedComponent.set({ ...selection, definition, topology, state: failed.length === 2 ? 'error' : failed.length ? 'partial' : 'ready', error: failed.length ? `Some component details are unavailable: ${failed.join(', ')}.` : null });
    } catch (error) {
      if (!this.componentDetailRequests.isCurrent(request)) return;
      if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; }
      if (generation !== this.authGeneration || this.sessionRequired() || !this.componentDetailRequests.isCurrent(request)) return;
      this.selectedComponent.set({ ...selection, state: 'error', error: error instanceof Error ? error.message : 'Component details are unavailable.' });
    }
  }
  closeComponent(): void { this.componentDetailRequests.invalidate(); this.selectedComponent.set(null); }
  async createRetention(request: RetentionRequest): Promise<void> {
    const generation = this.authGeneration;
    this.retention.set({ action: null, running: true, error: null });
    try {
      await this.gateway.ensureSession();
      if (generation !== this.authGeneration || this.sessionRequired()) return;
      const body = request.action === 'delete_before' ? { action: 'delete_before' as const, cutoff: request.cutoff } : { action: 'purge_all' as const };
      const action = await this.gateway.execute('createRetentionAction', { ...blankInput('createRetentionAction'), body, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': crypto.randomUUID(), 'X-Observatory-Confirmation': request.confirmation } });
      if (generation !== this.authGeneration || this.sessionRequired()) return;
      this.retention.set({ action, running: action.status === 'queued' || action.status === 'running', error: null });
      if (action.status === 'queued' || action.status === 'running') this.pollRetention(action.actionId, generation);
      else await this.refreshAfterRetention();
    } catch (error) { if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.'); if (generation === this.authGeneration && !this.sessionRequired()) this.retention.set({ action: null, running: false, error: error instanceof Error ? error.message : 'Retention request failed.' }); }
  }

  async logout(): Promise<void> {
    this.disconnectLive();
    this.liveEventIds.clear();
    try {
      await this.gateway.logout();
      this.markSignedOut('You are signed out of Observatory.', 'explicit_logout');
    } catch {
      this.markSignedOut('Observatory sign-out could not be confirmed.', 'logout_unconfirmed');
    }
  }

  retryAuthentication(): void { void this.runSessionLifecycle(); }
  /** Propagate an authenticated endpoint rejection discovered by a product feature. */
  requireSession(message = 'Owner Observatory session required.'): void { this.markSignedOut(message); }
  refreshCatalog(): void { this.queueCatalogRefresh(); }
  refreshHealthHistory(): void { this.queueHealthHistory(); }
  setAnalyticsFilters(filters: AnalyticsFilters): void {
    this.analyticsFilters.set(filters);
    this.queueAnalyticsRefresh();
  }
  refreshAnalytics(): void { this.queueAnalyticsRefresh(); }
  private readonly handleLiveMessage = (event: Event): void => {
    const message = event as MessageEvent<string>;
    // A native EventSource may replay the last frame after reconnecting, and
    // the transport also exposes named frames through both addEventListener
    // and onmessage. Stable event IDs make both deliveries idempotent without
    // suppressing synthetic metadata events that have no ID.
    if (message.lastEventId && this.liveEventIds.has(message.lastEventId)) return;
    if (message.lastEventId) {
      this.liveEventIds.add(message.lastEventId);
      if (this.liveEventIds.size > 256) {
        const oldest = this.liveEventIds.values().next().value;
        if (typeof oldest === 'string') this.liveEventIds.delete(oldest);
      }
    }
    let resourceType: string | null = null;
    let resourceId: string | null = null;
    try { const payload = JSON.parse(message.data || '{}') as { readonly resourceType?: unknown; readonly resourceId?: unknown }; resourceType = typeof payload.resourceType === 'string' ? payload.resourceType : null; resourceId = typeof payload.resourceId === 'string' ? payload.resourceId : null; } catch { /* metadata-only invalidation payloads are ignored safely */ }
    this.snapshot.update((value) => ({ ...value, lastEventId: event.type === 'heartbeat' ? value.lastEventId : (message.lastEventId || value.lastEventId), lastUpdatedAt: event.type === 'heartbeat' ? value.lastUpdatedAt : new Date().toISOString() }));
    if (isRunProjectionInvalidation(event.type, resourceType)) this.queueLiveRefresh();
    if (event.type === 'observation.changed' || event.type === 'retention.changed') this.workflowMonitoringRevision.update((value) => value + 1);
    if (this.selectedRunViewActive && matchesSelectedRunInvalidation(event.type, resourceType, resourceId, this.selectedRunId())) {
      const selectedRunId = this.selectedRunId();
      if (selectedRunId) this.queueSelectedRunReconciliation(selectedRunId, true);
    }
    if (event.type === 'health.changed') this.queueHealthHistory();
    if (this.analyticsViewActive && isRunCorrelatedInvalidation(event.type, resourceType) && (this.analytics().catalogue || this.analytics().snapshot)) this.queueAnalyticsRefresh();
    const comparisonInvalidation = isComparisonInvalidationEvent(event.type) && (isRunCorrelatedInvalidation(event.type, resourceType) || event.type === 'retention.changed');
    if (this.comparisonViewActive && comparisonInvalidation && this.comparison().state !== 'loading' && this.comparison().leftRunId && this.comparison().rightRunId) this.queueComparisonRefresh();
  };
  private readonly handleEvaluationLiveOpen = (): void => { this.evaluationLiveDisconnected.set(false); };
  private readonly handleEvaluationLiveError = (): void => { this.evaluationLiveDisconnected.set(true); };
  private readonly handleEvaluationLiveMessage = (event: Event): void => {
    const parsed = parseEvaluationLiveEvent((event as MessageEvent<string>).data);
    if (parsed) {
      if (parsed.eventId === this.lastEvaluationEventId) return;
      this.lastEvaluationEventId = parsed.eventId;
      this.evaluationChange.set(parsed);
      this.evaluationRevision.update((value) => value + 1);
    }
  };
  // An EventSource open is only transport state. Keep a pending run recovery
  // window alive until a snapshot actually converges the independently
  // materialized catalogue; flapping open/error callbacks must not starve it.
  private readonly handleLiveOpen = (): void => { this.liveDisconnected.set(false); this.snapshot.update((value) => ({ ...value, state: value.overview && value.runs ? this.stateFor(value.overview, value.runs.items.length) : 'loading', error: null })); if (this.analyticsViewActive && this.analytics().state === 'disconnected') { this.analytics.update((value) => ({ ...value, state: value.snapshot ? 'stale' : 'loading', error: null })); this.queueAnalyticsRefresh(); } if (this.comparisonViewActive && this.comparison().state === 'disconnected') { this.comparison.update((value) => ({ ...value, state: value.result ? 'refreshing' : 'loading', error: null })); this.queueComparisonRefresh(); } };
  // EventSource owns reconnect and its Last-Event-ID cursor. An error is only
  // transport state; it must not turn the passive Overview workspace into a
  // REST polling loop. Runs performs its own bounded reconciliation when that
  // workspace is active, while the next live snapshot remains authoritative.
  private readonly handleLiveError = (): void => { this.liveDisconnected.set(true); this.healthHistory.update(healthHistoryAfterDisconnect); this.snapshot.update((value) => ({ ...value, state: 'disconnected', error: 'Live updates are disconnected. Reconnecting…' })); if (this.analytics().snapshot) this.analytics.update((value) => ({ ...value, state: 'disconnected', error: 'Live updates are disconnected. Showing the last known analytics snapshot.' })); if (this.comparison().result) this.comparison.update((value) => ({ ...value, state: 'disconnected', error: 'Live updates are disconnected. Showing the last comparison until refresh.' })); };

  private queueLiveRefresh(): void {
    this.liveRefresh.request(() => this.loadLiveSnapshot(), () => this.authState() === 'authenticated' && !this.sessionRequired());
  }

  private queueHealthHistory(): void {
    this.healthHistoryRefresh.request(() => this.loadHealthHistory(), () => this.authState() === 'authenticated' && !this.sessionRequired());
  }

  private queueAnalyticsRefresh(): void {
    this.analyticsRefresh.request(() => this.loadAnalytics(), () => this.analyticsViewActive && this.authState() === 'authenticated' && !this.sessionRequired());
  }

  private queueComparisonRefresh(): void {
    this.comparisonRefresh.request(() => this.loadComparison(this.comparisonLoadSequence), () => this.comparisonViewActive && this.authState() === 'authenticated' && !this.sessionRequired() && Boolean(this.comparison().leftRunId && this.comparison().rightRunId));
  }

  private async loadComparison(sequence: number): Promise<void> {
    const selection = this.comparison();
    if (!selection.leftRunId || !selection.rightRunId) return;
    const generation = this.authGeneration;
    const previous = selection.result;
    if (!previous) this.comparison.update((value) => ({ ...value, state: 'loading', error: null }));
    else this.comparison.update((value) => ({ ...value, state: 'refreshing', error: null }));
    try {
      await this.gateway.ensureSession();
      const raw = await this.gateway.execute('compareRuns', { ...blankInput('compareRuns'), body: { leftRunId: selection.leftRunId, rightRunId: selection.rightRunId } });
      if (sequence !== this.comparisonLoadSequence || generation !== this.authGeneration || this.sessionRequired()) return;
      const result = parseRunComparison(raw);
      if (!result) { this.comparison.update((value) => ({ ...value, result: previous, state: previous ? 'stale' : 'malformed', error: 'The comparison response was malformed or contained unsupported metadata.', updatedAt: previous?.snapshotAt ?? null })); return; }
      const state: ObservatoryComparisonState = this.liveDisconnected() ? 'disconnected' : result.freshness.state === 'unavailable' ? 'unavailable' : result.partial ? 'partial' : 'ready';
      this.comparison.set({ leftRunId: selection.leftRunId, rightRunId: selection.rightRunId, result, state, error: null, updatedAt: result.snapshotAt });
    } catch (error) {
      if (sequence !== this.comparisonLoadSequence || generation !== this.authGeneration) return;
      if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; }
      const message = error instanceof Error ? error.message : 'Run comparison is unavailable.';
      const status = /\(503\)|\(502\)|\(504\)/.test(message) ? 'service_down' : /\(400\)|\(409\)|\(422\)/.test(message) ? 'rejected' : previous ? 'stale' : 'error';
      this.comparison.update((value) => ({ ...value, state: status, error: message }));
    }
  }

  private async loadAnalytics(): Promise<void> {
    const sequence = ++this.analyticsLoadSequence;
    const generation = this.authGeneration;
    const previous = this.analytics();
    this.analytics.update((value) => ({ ...value, state: value.snapshot ? 'stale' : 'loading', error: null }));
    try {
      await this.gateway.ensureSession();
      if (sequence !== this.analyticsLoadSequence || generation !== this.authGeneration || this.sessionRequired()) return;
      const rawCatalogue = previous.catalogue ?? await this.gateway.execute('listAnalyticsMetrics', { ...blankInput('listAnalyticsMetrics'), query: { focus: this.analyticsFilters().focus } });
      const catalogue = parseAnalyticsMetricCatalogue(rawCatalogue);
      if (!catalogue) throw new Error('Operational analytics catalogue is malformed.');
      const requestedFilters = this.analyticsFilters();
      const definition = catalogue.items.find((item) => item.metric === requestedFilters.metric && (requestedFilters.focus === 'all' || item.focusAreas.includes(requestedFilters.focus)));
      const filters = definition && (definition.metric !== requestedFilters.metric || definition.unit !== requestedFilters.unit)
        ? { ...requestedFilters, metric: definition.metric, unit: definition.unit }
        : requestedFilters;
      if (filters !== requestedFilters) this.analyticsFilters.set(filters);
      const rawSnapshot = await this.gateway.execute('queryOperationalAnalytics', {
        ...blankInput('queryOperationalAnalytics'),
        query: {
          metric: filters.metric,
          unit: filters.unit,
          aggregation: filters.aggregation,
          focus: filters.focus,
          bucket: filters.bucket,
          group_by: filters.groupBy,
          ...(filters.from ? { from: filters.from } : {}),
          ...(filters.to ? { to: filters.to } : {}),
          ...(filters.component ? { component: filters.component } : {}),
          ...(filters.deployment ? { deployment: filters.deployment } : {}),
        },
      });
      const snapshot = parseOperationalAnalyticsSnapshot(rawSnapshot);
      if (!snapshot) throw new Error('Operational analytics response is malformed.');
      if (sequence !== this.analyticsLoadSequence || generation !== this.authGeneration || this.sessionRequired()) return;
      this.analytics.set({ catalogue, snapshot, state: snapshot.series.length ? 'ready' : 'empty', error: null });
    } catch (error) {
      if (sequence !== this.analyticsLoadSequence || generation !== this.authGeneration || this.sessionRequired()) return;
      if (error instanceof ObservatorySessionRequiredError) {
        this.markSignedOut('Owner Observatory session required.');
        return;
      }
      this.analytics.set({ catalogue: previous.catalogue, snapshot: previous.snapshot, state: previous.snapshot ? 'stale' : 'error', error: error instanceof Error ? error.message : 'Operational analytics are unavailable.' });
    }
  }

  /** Load only the bounded, content-free operational history page. */
  private async loadHealthHistory(): Promise<void> {
    const sequence = ++this.healthHistorySequence;
    const generation = this.authGeneration;
    const previous = this.healthHistory();
    this.healthHistory.update((value) => ({ ...value, state: value.page ? 'stale' : 'loading', error: null }));
    try {
      await this.gateway.ensureSession();
      const page = await this.gateway.execute('listHealthHistory', { ...blankInput('listHealthHistory'), query: { page_size: 50 } });
      if (sequence !== this.healthHistorySequence || generation !== this.authGeneration || this.sessionRequired()) return;
      this.healthHistory.set({ page, state: page.items.length ? 'ready' : 'empty', error: null });
    } catch (error) {
      if (error instanceof ObservatorySessionRequiredError) {
        this.markSignedOut('Owner Observatory session required.');
        return;
      }
      if (sequence !== this.healthHistorySequence || generation !== this.authGeneration || this.sessionRequired()) return;
      this.healthHistory.set({ page: previous.page, state: previous.page ? 'stale' : 'error', error: previous.page ? 'Health history could not be refreshed.' : 'Health history is unavailable.' });
    }
  }

  /** Reconcile the independently materialized run projection a bounded number of times. */
  private queueRunReconciliation(): void {
    if (this.runReconciliationTimer || this.runReconciliationAttempts >= MAX_RUN_RECONCILIATION_ATTEMPTS || this.sessionRequired()) return;
    this.runReconciliationAttempts += 1;
    this.runReconciliationTimer = setTimeout(() => {
      this.runReconciliationTimer = null;
      // Route retries through the coordinator so a live-event refresh and a
      // reconciliation can never issue duplicate snapshots concurrently.
      this.queueLiveRefresh();
    }, RUN_RECONCILIATION_DELAY_MS);
  }

  private resetRunReconciliation(): void {
    if (this.runReconciliationTimer) clearTimeout(this.runReconciliationTimer);
    this.runReconciliationTimer = null;
    this.runReconciliationAttempts = 0;
  }

  /** Retry an initially unavailable selected-run projection for the same bounded recovery window. */
  private queueSelectedRunReconciliation(runId: string, fromLiveInvalidation = false): boolean {
    // The initial detail/trace load is intentionally still marked loading when
    // it discovers a projection gap. The timer is the concurrency gate; using
    // selectedRunState here would reject the first retry and strand the run in
    // an error state after a single trace request.
    if (this.sessionRequired() || this.selectedRunId() !== runId) return false;
    if (this.selectedRunLoadInFlightSequence !== null) {
      if (fromLiveInvalidation) {
        // A live event is external to the bounded self-reconciliation window.
        // Preserve it even while the final allowed retry is in flight so a
        // successful final snapshot still receives one trailing refresh.
        this.selectedRunRefreshPending = true;
        return true;
      }
      // Projection-gap recovery is the bounded retry itself. It may ask the
      // finally boundary to schedule the next attempt, but it must not bypass
      // the cap or turn the terminal attempt back into loading.
      if (this.selectedRunReconciliationAttempts >= MAX_RUN_RECONCILIATION_ATTEMPTS) return false;
      this.selectedRunRetryPending = true;
      return true;
    }
    if (this.selectedRunReconciliationAttempts >= MAX_RUN_RECONCILIATION_ATTEMPTS) return false;
    if (this.selectedRunReconciliationTimer) return false;
    this.selectedRunReconciliationAttempts += 1;
    const delayMs = fromLiveInvalidation ? SELECTED_RUN_TRAILING_REFRESH_DELAY_MS : RUN_RECONCILIATION_DELAY_MS;
    this.selectedRunReconciliationTimer = setTimeout(() => {
      this.selectedRunReconciliationTimer = null;
      if (this.selectedRunId() !== runId || this.sessionRequired()) return;
      void this.loadSelectedRun(runId, true);
    }, delayMs);
    return true;
  }

  private resetSelectedRunReconciliation(clearPending = true): void {
    if (this.selectedRunReconciliationTimer) clearTimeout(this.selectedRunReconciliationTimer);
    this.selectedRunReconciliationTimer = null;
    this.selectedRunReconciliationAttempts = 0;
    if (clearPending) this.selectedRunRefreshPending = false;
    if (clearPending) this.selectedRunRetryPending = false;
  }

  private queueCatalogRefresh(): void {
    this.catalogRefresh.request(() => this.loadCatalogResources(), () => this.authState() === 'authenticated' && !this.sessionRequired());
  }

  /** Refresh only observation-driven resources; component health can be slow. */
  private async loadLiveSnapshot(): Promise<void> {
    const sequence = ++this.loadSequence;
    const generation = this.authGeneration;
    const previous = this.snapshot();
    try {
      await this.gateway.ensureSession();
      if (!isCurrentLoadSequence(sequence, this.loadSequence) || generation !== this.authGeneration) return;
      this.sessionRequired.set(false);
      this.authState.set('authenticated');
      this.hadAuthenticatedSession = true;
      this.pendingRecoveryState = null;
      const results = await Promise.allSettled([
        this.gateway.execute('getOverview', blankInput('getOverview')),
        this.gateway.execute('listRuns', { ...blankInput('listRuns'), query: this.filters() }),
      ]);
      if (!isCurrentLoadSequence(sequence, this.loadSequence) || generation !== this.authGeneration) return;
      if (hasSessionRequiredRejection(results, (reason) => reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const resourceErrors = results.flatMap((result, index) => result.status === 'rejected' ? [index === 0 ? 'overview' : 'runs'] : []);
      const overview = results[0].status === 'fulfilled' ? results[0].value : previous.overview;
      const runs = results[1].status === 'fulfilled' ? results[1].value : previous.runs;
      this.resourceStates.update((states) => ({
        ...states,
        overview: resourceStateFor('overview', overview, results[0].status === 'rejected', overview?.observedAt),
        runs: resourceStateFor('runs', runs, results[1].status === 'rejected'),
      }));
      if (!overview) throw new Error('Observatory overview is unavailable.');
      const preservedErrors = previous.resourceErrors.filter((resource) => resource === 'components' || resource === 'deployments');
      const allResourceErrors = [...resourceErrors, ...preservedErrors];
      const latestEventId = this.snapshot().lastEventId ?? runs?.latestObservationId ?? previous.lastEventId;
      this.snapshot.update((value) => ({
        ...value,
        overview,
        runs,
        state: this.liveDisconnected() ? 'disconnected' : allResourceErrors.length ? 'stale' : this.stateFor(overview, runs?.items.length ?? 0),
        lastUpdatedAt: overview.observedAt,
        lastEventId: latestEventId,
        error: allResourceErrors.length ? `Some Observatory data is unavailable: ${allResourceErrors.join(', ')}.` : null,
        partial: allResourceErrors.length > 0,
        resourceErrors: allResourceErrors,
      }));
      const runsResourceUnavailable = this.resourceState('runs') === 'error' || this.resourceState('runs') === 'stale';
      // A selected Runs workspace owns a fresh bounded recovery generation.
      // Overview is deliberately passive: a count/identity mismatch or a
      // transient stream error is not permission to poll the projection while
      // the user is not inspecting Runs. The next live snapshot remains the
      // normal convergence path, and Runs activation starts recovery when it
      // is actually needed.
      const shouldReconcile = this.runsViewActive && (this.liveDisconnected() || runsResourceUnavailable || needsRunReconciliation(overview, runs));
      if (shouldReconcile) this.queueRunReconciliation();
      else if (results[1].status === 'fulfilled') this.resetRunReconciliation();
    } catch (error) {
      if (!isCurrentLoadSequence(sequence, this.loadSequence) || generation !== this.authGeneration) return;
      if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.');
      else if (!this.hadAuthenticatedSession) this.markSignedOut('Owner Observatory identity is unavailable.', 'identity_unavailable');
      if (!this.snapshot().overview) this.resourceStates.update((states) => ({ ...states, overview: 'error' }));
      this.snapshot.update((value) => ({ ...value, state: value.overview ? 'stale' : 'error', error: error instanceof Error ? error.message : 'Observatory is unavailable.' }));
    }
  }

  /** Load the slower catalog resources independently after the live surface is ready. */
  private async loadCatalogResources(): Promise<void> {
    const sequence = ++this.catalogLoadSequence;
    const generation = this.authGeneration;
    try {
      await this.gateway.ensureSession();
      if (sequence !== this.catalogLoadSequence || generation !== this.authGeneration) return;
      const results = await Promise.allSettled([
        this.gateway.execute('listComponents', blankInput('listComponents')),
        this.gateway.execute('listDeployments', blankInput('listDeployments')),
      ]);
      if (sequence !== this.catalogLoadSequence || generation !== this.authGeneration) return;
      if (hasSessionRequiredRejection(results, (reason) => reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const resourceErrors = results.flatMap((result, index) => result.status === 'rejected' ? [index === 0 ? 'components' : 'deployments'] : []);
      const current = this.snapshot();
      if (results[0].status === 'fulfilled') this.lastKnownComponents = results[0].value;
      if (results[1].status === 'fulfilled') this.lastKnownDeployments = results[1].value;
      const components = results[0].status === 'fulfilled' ? results[0].value : current.components ?? this.lastKnownComponents;
      const deployments = results[1].status === 'fulfilled' ? results[1].value : current.deployments ?? this.lastKnownDeployments;
      this.resourceStates.update((states) => ({
        ...states,
        components: resourceStateFor('components', components, results[0].status === 'rejected'),
        deployments: resourceStateFor('deployments', deployments, results[1].status === 'rejected'),
      }));
      const preservedErrors = current.resourceErrors.filter((resource) => resource === 'overview' || resource === 'runs');
      const allResourceErrors = [...preservedErrors, ...resourceErrors];
      this.snapshot.update((value) => ({
        ...value,
        components,
        deployments,
        state: this.liveDisconnected() ? 'disconnected' : allResourceErrors.length ? 'stale' : value.overview && value.runs ? this.stateFor(value.overview, value.runs.items.length) : value.state,
        error: allResourceErrors.length ? `Some Observatory data is unavailable: ${allResourceErrors.join(', ')}.` : null,
        partial: allResourceErrors.length > 0,
        resourceErrors: allResourceErrors,
      }));
    } catch (error) {
      if (sequence !== this.catalogLoadSequence || generation !== this.authGeneration) return;
      if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.');
      else {
        this.resourceStates.update((states) => ({ ...states, components: 'error', deployments: 'error' }));
        this.snapshot.update((value) => {
          const resourceErrors = [...value.resourceErrors.filter((resource) => resource === 'overview' || resource === 'runs'), 'components', 'deployments'];
          return { ...value, components: value.components ?? this.lastKnownComponents, deployments: value.deployments ?? this.lastKnownDeployments, state: value.overview ? 'stale' : 'error', error: error instanceof Error ? error.message : 'Observatory catalog is unavailable.', partial: true, resourceErrors };
        });
      }
    }
  }

  private stateFor(overview: ObservatorySnapshot['overview'], runCount: number): ObservatorySnapshot['state'] {
    if (!overview) return 'loading';
    if (this.liveDisconnected()) return 'disconnected';
    if (isOverviewStale(overview.observedAt)) return 'stale';
    return runCount ? 'ready' : 'empty';
  }

  private markSignedOut(message: string, explicitState?: ObservatoryAuthState): void {
    this.selectedRunAbort?.abort();
    this.selectedRunAbort = null;
    if (this.evidenceDeletionPoll) { clearTimeout(this.evidenceDeletionPoll); this.evidenceDeletionPoll = null; }
    this.evidenceDeletionKeys.clear();
    this.disconnectLive();
    this.liveEventIds.clear();
    this.liveRefresh.cancel();
    this.catalogRefresh.cancel();
    this.runsViewActive = false;
    this.analyticsViewActive = false;
    this.comparisonViewActive = false;
    this.selectedRunViewActive = false;
    this.resetRunReconciliation();
    this.resetSelectedRunReconciliation();
    this.analyticsRefresh.cancel();
    this.healthHistoryRefresh.cancel();
    this.catalogLoadSequence += 1;
    this.healthHistorySequence += 1;
    this.loadSequence += 1;
    this.openRunSequence += 1;
    this.authGeneration += 1;
    this.authGenerationRevision.update((value) => value + 1);
    if (this.refreshTimer) { clearTimeout(this.refreshTimer); this.refreshTimer = null; }
    this.sessionRequired.set(true);
    this.authState.set(explicitState ?? this.pendingRecoveryState ?? (this.hadAuthenticatedSession ? 'expired' : 'initial'));
    this.pendingRecoveryState = null;
    this.filters.set({});
    this.resourceStates.set({ overview: 'error', runs: 'error', components: 'error', deployments: 'error' });
    this.snapshot.set({ overview: null, runs: null, components: null, deployments: null, state: 'error', lastUpdatedAt: null, lastEventId: null, error: message, partial: false, resourceErrors: [] });
    this.healthHistory.set({ page: null, state: 'error', error: null });
    this.selectedRun.set({ detail: null, trace: null, inspection: null });
    this.selectedRunInspectionFreshness.set('loading');
    this.selectedRunId.set(null);
    this.selectedRunState.set('idle');
    this.selectedRunError.set(null);
    this.clearRunEvidence();
    this.componentHealthById.set({});
    this.componentHealthErrors.set({});
    this.componentHealthLoading.set({});
    this.componentHealthRequests.invalidate();
    this.componentDetailRequests.invalidate();
    this.lastKnownComponents = null;
    this.lastKnownDeployments = null;
    this.selectedComponent.set(null);
    this.analyticsLoadSequence += 1;
    this.analytics.set({ catalogue: null, snapshot: null, state: 'error', error: null });
    this.analyticsFilters.set(DEFAULT_ANALYTICS_FILTERS);
    this.comparisonLoadSequence += 1;
    this.comparisonRefresh.cancel();
    this.comparison.set({ leftRunId: null, rightRunId: null, result: null, state: 'error', error: null, updatedAt: null });
    this.retention.set({ action: null, running: false, error: null });
  }

  private async refreshAfterRetention(): Promise<void> {
    this.selectedRunAbort?.abort();
    this.selectedRunAbort = null;
    if (this.evidenceDeletionPoll) { clearTimeout(this.evidenceDeletionPoll); this.evidenceDeletionPoll = null; }
    this.evidenceDeletionKeys.clear();
    this.authGeneration += 1;
    // Retention is a destructive boundary for all product-owned, in-memory
    // selections and idempotency material. Feature stores must discard their
    // rows and command keys before taking the authoritative post-delete view.
    this.authGenerationRevision.update((value) => value + 1);
    this.evaluationRevision.update((value) => value + 1);
    this.openRunSequence += 1;
    this.resetSelectedRunReconciliation();
    this.selectedRun.set({ detail: null, trace: null, inspection: null });
    this.selectedRunId.set(null);
    this.selectedRunState.set('idle');
    this.selectedRunError.set(null);
    this.clearRunEvidence();
    this.componentHealthById.set({});
    this.componentHealthErrors.set({});
    this.componentHealthLoading.set({});
    this.componentHealthRequests.invalidate();
    this.componentDetailRequests.invalidate();
    this.lastKnownComponents = null;
    this.lastKnownDeployments = null;
    this.selectedComponent.set(null);
    this.analyticsLoadSequence += 1;
    this.analytics.set({ catalogue: this.analytics().catalogue, snapshot: null, state: 'loading', error: null });
    this.comparisonLoadSequence += 1;
    this.comparisonRefresh.cancel();
    this.comparison.set({ leftRunId: null, rightRunId: null, result: null, state: 'idle', error: null, updatedAt: null });
    await this.load();
    this.queueAnalyticsRefresh();
  }
  private pollRetention(actionId: string, generation: number): void {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(async () => {
      if (generation !== this.authGeneration || this.sessionRequired()) return;
      try {
        const action = await this.gateway.execute('getRetentionAction', { ...blankInput('getRetentionAction'), path: { action_id: actionId }, query: {} });
        if (generation !== this.authGeneration || this.sessionRequired()) return;
        this.retention.set({ action, running: action.status === 'queued' || action.status === 'running', error: action.failureCode });
        if (action.status === 'queued' || action.status === 'running') this.pollRetention(actionId, generation); else await this.refreshAfterRetention();
      } catch (error) { if (error instanceof ObservatorySessionRequiredError) this.markSignedOut('Owner Observatory session required.'); if (generation === this.authGeneration && !this.sessionRequired()) this.retention.set({ action: null, running: false, error: error instanceof Error ? error.message : 'Retention status is unavailable.' }); }
    }, 1200);
  }
}
