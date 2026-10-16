import { Injectable, computed, inject, signal } from '@angular/core';
import { type ComponentHealth, type LiveEvent, type ObservatoryOperationInput, type RunStatus } from '@aura/observatory-api-client';
import { OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort, blankInput } from './observatory-gateway';
import { hasSessionRequiredRejection, healthHistoryAfterDisconnect, isCurrentLoadSequence, isOverviewStale, LiveRefreshCoordinator, RequestGenerationGate, resourceStateFor, type ObservatoryAuthState, type ObservatoryHealthHistory, type ObservatoryResource, type ObservatoryResourceState, type ObservatorySnapshot, type RetentionRequest, type RetentionState, type RunFilters, type SelectedRun } from './observatory-models';

@Injectable({ providedIn: 'root' })
export class ObservatoryStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  readonly snapshot = signal<ObservatorySnapshot>({ overview: null, runs: null, components: null, deployments: null, state: 'loading', lastUpdatedAt: null, lastEventId: null, error: null, partial: false, resourceErrors: [] });
  readonly healthHistory = signal<ObservatoryHealthHistory>({ page: null, state: 'loading', error: null });
  readonly resourceStates = signal<Record<ObservatoryResource, ObservatoryResourceState>>({ overview: 'loading', runs: 'loading', components: 'loading', deployments: 'loading' });
  readonly filters = signal<RunFilters>({});
  readonly selectedRun = signal<SelectedRun>({ detail: null, trace: null });
  readonly selectedRunId = signal<string | null>(null);
  readonly selectedRunState = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');
  readonly selectedRunError = signal<string | null>(null);
  readonly retention = signal<RetentionState>({ action: null, running: false, error: null });
  readonly authState = signal<ObservatoryAuthState>('checking');
  readonly sessionRequired = signal(false);
  readonly liveDisconnected = signal(false);
  readonly componentHealthById = signal<Record<string, ComponentHealth>>({});
  readonly componentHealthErrors = signal<Record<string, string>>({});
  readonly componentHealthLoading = signal<Record<string, boolean>>({});
  readonly components = computed(() => this.snapshot().components?.items ?? []);
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private loadSequence = 0;
  private openRunSequence = 0;
  private authGeneration = 0;
  private hadAuthenticatedSession = false;
  private pendingRecoveryState: ObservatoryAuthState | null = null;
  private readonly componentHealthRequests = new RequestGenerationGate();
  private readonly liveRefresh = new LiveRefreshCoordinator();
  private readonly healthHistoryRefresh = new LiveRefreshCoordinator();
  private readonly catalogRefresh = new LiveRefreshCoordinator();
  private catalogLoadSequence = 0;
  private healthHistorySequence = 0;

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
      this.queueCatalogRefresh();
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
  reconnectLive(): void { this.liveDisconnected.set(true); this.gateway.reconnectLive(this.handleLiveOpen, this.handleLiveMessage, this.handleLiveError); }
  disconnectLive(): void { this.gateway.disconnectLive(); this.liveDisconnected.set(false); }
  async openRun(runId: string): Promise<void> {
    const sequence = ++this.openRunSequence;
    this.selectedRunId.set(runId);
    this.selectedRun.set({ detail: null, trace: null });
    this.selectedRunState.set('loading');
    this.selectedRunError.set(null);
    try {
      const detail = await this.gateway.execute('getRun', { ...blankInput('getRun'), path: { run_id: runId }, query: {} });
      const trace = detail.traceId ? await this.gateway.execute('getTrace', { ...blankInput('getTrace'), path: { trace_id: detail.traceId }, query: {} }) : null;
      if (sequence !== this.openRunSequence) return;
      this.selectedRun.set({ detail, trace });
      this.selectedRunState.set('ready');
    } catch (error) { if (sequence !== this.openRunSequence) return; if (error instanceof ObservatorySessionRequiredError) { this.markSignedOut('Owner Observatory session required.'); return; } this.selectedRun.set({ detail: null, trace: null }); this.selectedRunState.set('error'); this.selectedRunError.set(error instanceof Error ? error.message : 'Run detail is unavailable.'); this.snapshot.update((value) => ({ ...value, error: this.selectedRunError(), state: 'stale' })); }
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
    try {
      await this.gateway.logout();
      this.markSignedOut('You are signed out of Observatory.', 'explicit_logout');
    } catch {
      this.markSignedOut('Observatory sign-out could not be confirmed.', 'logout_unconfirmed');
    }
  }

  retryAuthentication(): void { void this.runSessionLifecycle(); }
  refreshCatalog(): void { this.queueCatalogRefresh(); }
  refreshHealthHistory(): void { this.queueHealthHistory(); }

  private readonly handleLiveMessage = (event: Event): void => {
    const message = event as MessageEvent<string>;
    this.snapshot.update((value) => ({ ...value, lastEventId: event.type === 'heartbeat' ? value.lastEventId : (message.lastEventId || value.lastEventId), lastUpdatedAt: new Date().toISOString() }));
    this.queueLiveRefresh();
    if (event.type === 'health.changed') this.queueHealthHistory();
  };
  private readonly handleLiveOpen = (): void => { this.liveDisconnected.set(false); this.snapshot.update((value) => ({ ...value, state: value.overview && value.runs ? this.stateFor(value.overview, value.runs.items.length) : 'loading', error: null })); };
  private readonly handleLiveError = (): void => { this.liveDisconnected.set(true); this.healthHistory.update(healthHistoryAfterDisconnect); this.snapshot.update((value) => ({ ...value, state: 'disconnected', error: 'Live updates are disconnected. Reconnecting…' })); this.queueLiveRefresh(); };

  private queueLiveRefresh(): void {
    this.liveRefresh.request(() => this.loadLiveSnapshot(), () => this.authState() === 'authenticated' && !this.sessionRequired());
  }

  private queueHealthHistory(): void {
    this.healthHistoryRefresh.request(() => this.loadHealthHistory(), () => this.authState() === 'authenticated' && !this.sessionRequired());
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
        state: allResourceErrors.length ? 'stale' : this.stateFor(overview, runs?.items.length ?? 0),
        lastUpdatedAt: overview.observedAt,
        lastEventId: latestEventId,
        error: allResourceErrors.length ? `Some Observatory data is unavailable: ${allResourceErrors.join(', ')}.` : null,
        partial: allResourceErrors.length > 0,
        resourceErrors: allResourceErrors,
      }));
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
      const components = results[0].status === 'fulfilled' ? results[0].value : current.components;
      const deployments = results[1].status === 'fulfilled' ? results[1].value : current.deployments;
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
        state: allResourceErrors.length ? 'stale' : value.overview && value.runs ? this.stateFor(value.overview, value.runs.items.length) : value.state,
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
          return { ...value, state: value.overview ? 'stale' : 'error', error: error instanceof Error ? error.message : 'Observatory catalog is unavailable.', partial: true, resourceErrors };
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
    this.disconnectLive();
    this.liveRefresh.cancel();
    this.catalogRefresh.cancel();
    this.healthHistoryRefresh.cancel();
    this.catalogLoadSequence += 1;
    this.healthHistorySequence += 1;
    this.loadSequence += 1;
    this.openRunSequence += 1;
    this.authGeneration += 1;
    if (this.refreshTimer) { clearTimeout(this.refreshTimer); this.refreshTimer = null; }
    this.sessionRequired.set(true);
    this.authState.set(explicitState ?? this.pendingRecoveryState ?? (this.hadAuthenticatedSession ? 'expired' : 'initial'));
    this.pendingRecoveryState = null;
    this.filters.set({});
    this.resourceStates.set({ overview: 'error', runs: 'error', components: 'error', deployments: 'error' });
    this.snapshot.set({ overview: null, runs: null, components: null, deployments: null, state: 'error', lastUpdatedAt: null, lastEventId: null, error: message, partial: false, resourceErrors: [] });
    this.healthHistory.set({ page: null, state: 'error', error: null });
    this.selectedRun.set({ detail: null, trace: null });
    this.selectedRunId.set(null);
    this.selectedRunState.set('idle');
    this.selectedRunError.set(null);
    this.componentHealthById.set({});
    this.componentHealthErrors.set({});
    this.componentHealthLoading.set({});
    this.componentHealthRequests.invalidate();
    this.retention.set({ action: null, running: false, error: null });
  }

  private async refreshAfterRetention(): Promise<void> {
    this.authGeneration += 1;
    this.openRunSequence += 1;
    this.selectedRun.set({ detail: null, trace: null });
    this.selectedRunId.set(null);
    this.selectedRunState.set('idle');
    this.selectedRunError.set(null);
    this.componentHealthById.set({});
    this.componentHealthErrors.set({});
    this.componentHealthLoading.set({});
    this.componentHealthRequests.invalidate();
    await this.load();
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
