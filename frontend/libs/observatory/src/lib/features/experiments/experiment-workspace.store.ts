import { Injectable, inject, signal } from '@angular/core';
import type {
  CreateEvaluationBaselineCandidateRequest,
  CreateEvaluationExperimentRequest,
  CreateEvaluationRegressionRequest,
  DeploymentCollection,
  EvaluationAttribution,
  EvaluationBaseline,
  EvaluationBaselinePage,
  EvaluationCasePage,
  EvaluationEvaluatorPage,
  EvaluationExperimentPage,
  EvaluationHistoryPage,
  EvaluationRegression,
  EvaluationRegressionPage,
  EvaluationResultPage,
  EvaluationSuite,
  EvaluationSuitePage,
  PromoteEvaluationBaselineRequest,
} from '@aura/observatory-api-client';
import { blankInput, OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort } from '../../observatory-gateway';
import { ObservatoryStore } from '../../observatory-store';
import { EXPERIMENT_VARIANT_MAX, EXPERIMENT_VARIANT_MIN, PROMOTION_CONFIRMATION, hasValidExperimentVariants } from './experiment-contract-guards';

export type ExperimentWorkspaceState = 'loading' | 'ready' | 'empty' | 'partial' | 'stale' | 'disconnected' | 'rejected' | 'unauthorized' | 'unavailable' | 'incompatible' | 'insufficient-evidence' | 'inconclusive' | 'error';

export interface ExperimentWorkspaceData {
  readonly experiments: EvaluationExperimentPage | null;
  readonly baselines: EvaluationBaselinePage | null;
  readonly regressions: EvaluationRegressionPage | null;
  readonly history: EvaluationHistoryPage | null;
  readonly suites: EvaluationSuitePage | null;
  readonly evaluators: EvaluationEvaluatorPage | null;
  readonly results: EvaluationResultPage | null;
  readonly cases: EvaluationCasePage | null;
  readonly deployments: DeploymentCollection | null;
  readonly attribution: EvaluationAttribution | null;
}

const emptyData = (): ExperimentWorkspaceData => ({ experiments: null, baselines: null, regressions: null, history: null, suites: null, evaluators: null, results: null, cases: null, deployments: null, attribution: null });
let fallbackId = 1;
const id = (): string => typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `00000000-0000-4000-8000-${String(fallbackId++).padStart(12, '0')}`;

/** Ephemeral experiment state. No browser storage, cache, or URL state is used. */
@Injectable()
export class ExperimentWorkspaceStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  private readonly observatoryStore = inject(ObservatoryStore, { optional: true });
  private readonly actionKeys = new Map<string, string>();
  private readonly promotionCommands = new Map<string, { readonly idempotencyKey: string; readonly body: PromoteEvaluationBaselineRequest }>();
  private requestGeneration = 0;

  readonly state = signal<ExperimentWorkspaceState>('loading');
  readonly data = signal<ExperimentWorkspaceData>(emptyData());
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly selectedRegression = signal<EvaluationRegression | null>(null);
  readonly lastUpdatedAt = signal<string | null>(null);

  async load(): Promise<void> {
    const generation = ++this.requestGeneration;
    this.state.set('loading'); this.error.set(null);
    try {
      await this.gateway.ensureSession();
      const query = { page_size: 100 } as const;
      const results = await Promise.allSettled([
        this.gateway.execute('listExperiments', { ...blankInput('listExperiments'), query }),
        this.gateway.execute('listBaselines', { ...blankInput('listBaselines'), query }),
        this.gateway.execute('listRegressions', { ...blankInput('listRegressions'), query }),
        this.gateway.execute('listEvaluationHistory', { ...blankInput('listEvaluationHistory'), query }),
        this.gateway.execute('listEvaluationSuites', { ...blankInput('listEvaluationSuites'), query }),
        this.gateway.execute('listEvaluationEvaluators', { ...blankInput('listEvaluationEvaluators'), query }),
        this.gateway.execute('listEvaluationResults', { ...blankInput('listEvaluationResults'), query }),
        this.gateway.execute('listDeployments', blankInput('listDeployments')),
      ]);
      if (generation !== this.requestGeneration) return;
      if (results.some((item) => item.status === 'rejected' && item.reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const previous = this.data();
      const value = <T>(index: number, fallback: T | null): T | null => results[index].status === 'fulfilled' ? results[index].value as T : fallback;
      const next: ExperimentWorkspaceData = {
        experiments: value(0, previous.experiments), baselines: value(1, previous.baselines), regressions: value(2, previous.regressions), history: value(3, previous.history),
        suites: value(4, previous.suites), evaluators: value(5, previous.evaluators), results: value(6, previous.results), cases: previous.cases, deployments: value(7, previous.deployments), attribution: previous.attribution,
      };
      this.data.set(next);
      const selected = this.selectedRegression();
      if (selected) this.selectedRegression.set(next.regressions?.items.find((item) => item.regressionId === selected.regressionId && item.regressionVersion === selected.regressionVersion) ?? null);
      this.state.set(this.stateFor(next, results.some((item) => item.status === 'rejected')));
      this.lastUpdatedAt.set(new Date().toISOString());
    } catch (cause) {
      if (generation !== this.requestGeneration) return;
      if (cause instanceof ObservatorySessionRequiredError) this.observatoryStore?.requireSession();
      this.state.set(cause instanceof ObservatorySessionRequiredError ? 'unauthorized' : 'unavailable'); this.error.set(cause instanceof Error ? cause.message : 'Experiment data is unavailable.');
    }
  }

  async loadCases(suite: EvaluationSuite): Promise<void> {
    const generation = this.requestGeneration;
    try {
      const response = await this.gateway.execute('listEvaluationCases', { ...blankInput('listEvaluationCases'), path: { dataset_id: suite.dataset.resourceId, resource_version: suite.dataset.resourceVersion }, query: { page_size: 100 } });
      if (generation === this.requestGeneration) this.data.update((value) => ({ ...value, cases: response }));
    } catch (cause) {
      if (generation === this.requestGeneration) { if (cause instanceof ObservatorySessionRequiredError) this.observatoryStore?.requireSession(); this.state.set(cause instanceof ObservatorySessionRequiredError ? 'unauthorized' : 'unavailable'); this.error.set(cause instanceof Error ? cause.message : 'Suite cases are unavailable.'); }
    }
  }

  async createExperiment(request: CreateEvaluationExperimentRequest): Promise<void> {
    if (this.busyAction()) return;
    if (!hasValidExperimentVariants(request.variants)) { this.state.set('rejected'); this.error.set(`Experiments require ${EXPERIMENT_VARIANT_MIN}–${EXPERIMENT_VARIANT_MAX} unique bounded variants.`); return; }
    this.busyAction.set('create-experiment');
    const key = this.actionKeys.get(`experiment:${request.experimentId}`) ?? id(); this.actionKeys.set(`experiment:${request.experimentId}`, key);
    try {
      const experiment = await this.gateway.execute('createExperiment', { ...blankInput('createExperiment'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request });
      this.notice.set(`Experiment ${experiment.experimentId} ${experiment.experimentVersion} was created.`); await this.load();
    } catch (cause) { this.captureError(cause, 'Experiment creation failed.'); }
    finally { this.busyAction.set(null); }
  }

  async createBaselineCandidate(request: CreateEvaluationBaselineCandidateRequest): Promise<void> {
    if (this.busyAction()) return;
    if (!request.sourceResultIds.length || !request.cohortIds.length || !request.thresholds.length) { this.state.set('rejected'); this.error.set('Select persisted results, cohorts, and an authoritative evaluator threshold.'); return; }
    this.busyAction.set('create-baseline');
    const key = this.actionKeys.get(`baseline:${request.baselineId}`) ?? id(); this.actionKeys.set(`baseline:${request.baselineId}`, key);
    try {
      const baseline = await this.gateway.execute('createBaselineCandidate', { ...blankInput('createBaselineCandidate'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request });
      this.notice.set(`Baseline candidate ${baseline.baselineId} is ready for owner review.`); await this.load();
    } catch (cause) { this.captureError(cause, 'Baseline candidate creation failed.'); }
    finally { this.busyAction.set(null); }
  }

  async promoteBaseline(baseline: EvaluationBaseline, confirmation: string): Promise<void> {
    if (this.busyAction() || baseline.state !== 'candidate' || confirmation !== PROMOTION_CONFIRMATION) return;
    // A retry after an ambiguous transport failure must be evaluated against the
    // new response, rather than the previous error signal.
    this.error.set(null);
    this.busyAction.set(`promote:${baseline.baselineId}`);
    const commandKey = `promote:${baseline.baselineId}:${baseline.baselineVersion}`;
    const command = this.promotionCommands.get(commandKey) ?? (() => {
      const created = { idempotencyKey: this.actionKeys.get(commandKey) ?? id(), body: { expectedCandidateVersion: baseline.baselineVersion, authorizationRecordId: id(), promotedAt: new Date().toISOString() } };
      this.actionKeys.set(commandKey, created.idempotencyKey); this.promotionCommands.set(commandKey, created); return created;
    })();
    try {
      const promoted = await this.gateway.execute('promoteBaseline', { ...blankInput('promoteBaseline'), path: { baseline_id: baseline.baselineId }, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': command.idempotencyKey, 'X-Observatory-Confirmation': confirmation }, body: command.body });
      this.notice.set(`Baseline ${promoted.baselineId} is now immutable and promoted.`); await this.load();
    } catch (cause) { this.captureError(cause, 'Baseline promotion outcome is ambiguous. Retry the same command safely.'); }
    finally { this.busyAction.set(null); }
  }

  async compare(request: CreateEvaluationRegressionRequest): Promise<void> {
    if (this.busyAction()) return;
    const baseline = this.data().baselines?.items.find((item) => item.baselineId === request.baseline.id && item.baselineVersion === request.baseline.version);
    const experiment = this.data().experiments?.items.find((item) => item.experimentId === request.experiment.id && item.experimentVersion === request.experiment.version);
    if (!baseline || baseline.state !== 'promoted' || !experiment || request.variantId === baseline.variantId || !request.cohortIds.every((cohort) => experiment.cohorts.some((item) => item.cohortId === cohort))) { this.state.set('rejected'); this.error.set('Choose a promoted baseline, a distinct declared variant, and compatible cohorts.'); return; }
    this.busyAction.set('compare');
    const key = this.actionKeys.get(`regression:${request.regressionId}`) ?? id(); this.actionKeys.set(`regression:${request.regressionId}`, key);
    try {
      const regression = await this.gateway.execute('createRegressionComparison', { ...blankInput('createRegressionComparison'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request });
      this.selectedRegression.set(regression); this.notice.set(`Comparison ${regression.regressionId} completed: ${regression.decision}.`); await this.load();
    } catch (cause) { this.captureError(cause, 'Regression comparison failed.'); }
    finally { this.busyAction.set(null); }
  }

  openRegression(regression: EvaluationRegression): void { this.selectedRegression.set(regression); this.data.update((value) => ({ ...value, attribution: null })); }

  async inspectAttribution(regressionId: string, version?: string): Promise<void> {
    try {
      const regression = this.data().regressions?.items.find((item) => item.regressionId === regressionId && (!version || item.regressionVersion === version)) ?? this.selectedRegression();
      const exactVersion = regression?.regressionVersion;
      if (!regression || !exactVersion) { this.state.set('incompatible'); this.error.set('Attribution requires an exact persisted regression version.'); return; }
      const attribution = await this.gateway.execute('getRegressionAttribution', { ...blankInput('getRegressionAttribution'), path: { regression_id: regressionId }, query: { version: exactVersion } });
      this.selectedRegression.set(regression); this.data.update((current) => ({ ...current, attribution }));
    } catch (cause) { this.captureError(cause, 'Attribution evidence is unavailable.'); }
  }

  clear(): void { this.requestGeneration += 1; this.data.set(emptyData()); this.selectedRegression.set(null); this.actionKeys.clear(); this.promotionCommands.clear(); this.state.set('loading'); this.error.set(null); this.notice.set(null); this.busyAction.set(null); this.lastUpdatedAt.set(null); }
  async retry(): Promise<void> { await this.load(); }
  setEvaluationLiveDisconnected(disconnected: boolean): void { if (disconnected) this.state.set('disconnected'); else if (this.state() === 'disconnected') this.state.set(this.data().experiments ? 'stale' : 'loading'); }

  private captureError(cause: unknown, fallback: string): void { if (cause instanceof ObservatorySessionRequiredError) { this.observatoryStore?.requireSession(); this.state.set('unauthorized'); } else if (cause instanceof Error && /unavailable/i.test(cause.message)) this.state.set('unavailable'); else this.state.set(this.data().experiments ? 'stale' : 'error'); this.error.set(cause instanceof Error ? cause.message : fallback); }
  private hasRows(data: ExperimentWorkspaceData): boolean { return Boolean(data.experiments?.items.length || data.baselines?.items.length || data.regressions?.items.length || data.history?.items.length); }
  private stateFor(data: ExperimentWorkspaceData, rejected: boolean): ExperimentWorkspaceState { if (rejected) return this.hasRows(data) ? 'partial' : 'unavailable'; if (data.regressions?.items.some((item) => item.evidenceState === 'incompatible')) return 'incompatible'; if (data.regressions?.items.some((item) => item.evidenceState === 'insufficient' || item.evidenceState === 'missing')) return 'insufficient-evidence'; if (data.regressions?.items.some((item) => item.evidenceState === 'late')) return 'stale'; if (data.regressions?.items.some((item) => item.decision === 'inconclusive')) return 'inconclusive'; return this.hasRows(data) ? 'ready' : 'empty'; }
}
