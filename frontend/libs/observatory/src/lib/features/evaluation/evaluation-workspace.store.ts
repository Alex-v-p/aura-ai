import { Injectable, computed, inject, signal } from '@angular/core';
import type {
  EvaluationArtifactPage,
  EvaluationCalibrationPage,
  EvaluationCasePage,
  EvaluationDataset,
  EvaluationDatasetPage,
  CreateEvaluationDeletionRequest,
  EvaluationDeletionAction,
  EvaluationEvaluatorPage,
  EvaluationHistoryPage,
  EvaluationJob,
  EvaluationJobPage,
  EvaluationQualificationPage,
  EvaluationResultPage,
  EvaluationReviewPage,
  EvaluationScorePage,
  EvaluationSuitePage,
  EvaluationSuite,
  EvaluationResult,
  EvaluationEvaluator,
  EvaluationModelBinding,
  EvaluationReproducibility,
  EvaluationExecutionLimits,
  EvaluationLiveEvent,
  CreateEvaluationDatasetRequest,
  CreateEvaluationSuiteRequest,
  CreateEvaluationReviewRequest,
  QualifyEvaluationJudgeRequest,
  RevokeEvaluationQualificationRequest,
} from '@aura/observatory-api-client';
import { blankInput, OBSERVATORY_GATEWAY, ObservatorySessionRequiredError, type ObservatoryGatewayPort } from '../../observatory-gateway';
import {
  parseEvaluationArtifactPage, parseEvaluationCalibrationPage, parseEvaluationCasePage,
  parseEvaluationDataset, parseEvaluationDatasetPage, parseEvaluationDeletionAction,
  parseEvaluationEvaluatorPage, parseEvaluationHistoryPage, parseEvaluationJob,
  parseEvaluationJobPage, parseEvaluationQualification, parseEvaluationQualificationPage,
  parseEvaluationResult, parseEvaluationResultPage, parseEvaluationReview,
  parseEvaluationReviewPage, parseEvaluationScorePage, parseEvaluationSuite,
  parseEvaluationSuitePage,
} from './evaluation-dto-adapter';

export type EvaluationWorkspaceTab = 'datasets' | 'suites' | 'evaluators' | 'jobs' | 'results' | 'artifacts' | 'reviews' | 'qualification' | 'history' | 'retention';
export type EvaluationWorkspaceState = 'loading' | 'ready' | 'empty' | 'partial' | 'rejected' | 'stale' | 'disconnected' | 'unauthorized' | 'provider-unavailable' | 'error';

export interface EvaluationWorkspaceData {
  readonly datasets: EvaluationDatasetPage | null;
  readonly cases: EvaluationCasePage | null;
  readonly suites: EvaluationSuitePage | null;
  readonly evaluators: EvaluationEvaluatorPage | null;
  readonly jobs: EvaluationJobPage | null;
  readonly results: EvaluationResultPage | null;
  readonly scores: EvaluationScorePage | null;
  readonly artifacts: EvaluationArtifactPage | null;
  readonly reviews: EvaluationReviewPage | null;
  readonly calibrations: EvaluationCalibrationPage | null;
  readonly qualifications: EvaluationQualificationPage | null;
  readonly history: EvaluationHistoryPage | null;
  readonly deletion: EvaluationDeletionAction | null;
}

const emptyData = (): EvaluationWorkspaceData => ({ datasets: null, cases: null, suites: null, evaluators: null, jobs: null, results: null, scores: null, artifacts: null, reviews: null, calibrations: null, qualifications: null, history: null, deletion: null });
let fallbackUuid = 1;
const uuid = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const suffix = String(fallbackUuid++).padStart(12, '0');
  return `00000000-0000-4000-8000-${suffix}`;
};

export interface JobAdmissionBindings {
  readonly model: EvaluationModelBinding;
  readonly deploymentRevision: string;
  readonly reproducibility: EvaluationReproducibility;
  readonly limits: EvaluationExecutionLimits;
}

/** Evaluation application state is intentionally ephemeral and never persisted. */
@Injectable()
export class EvaluationWorkspaceStore {
  private readonly gateway = inject(OBSERVATORY_GATEWAY) as ObservatoryGatewayPort;
  readonly state = signal<EvaluationWorkspaceState>('loading');
  readonly data = signal<EvaluationWorkspaceData>(emptyData());
  readonly error = signal<string | null>(null);
  readonly unavailableScopes = signal<ReadonlyArray<string>>([]);
  readonly rejectedScopes = signal<ReadonlyArray<string>>([]);
  readonly notice = signal<string | null>(null);
  readonly activeJobId = signal<string | null>(null);
  readonly busyAction = signal<string | null>(null);
  readonly lastUpdatedAt = signal<string | null>(null);
  readonly selectedDataset = signal<EvaluationDataset | null>(null);
  readonly selectedSuite = signal<EvaluationSuite | null>(null);
  readonly selectedEvaluator = signal<EvaluationEvaluator | null>(null);
  readonly selectedResult = signal<EvaluationResult | null>(null);
  readonly jobBindings = signal<JobAdmissionBindings | null>(null);
  readonly jobAdmissionReason = computed(() => {
    const dataset = this.selectedDataset(); const suite = this.selectedSuite(); const evaluator = this.selectedEvaluator(); const bindings = this.jobBindings();
    if (!dataset) return 'Select and inspect an authoritative dataset version first.';
    if (!suite) return 'Select an authoritative suite version first.';
    if (!evaluator) return 'Select an authoritative evaluator version first.';
    if (suite.dataset.resourceId !== dataset.datasetId || suite.dataset.resourceVersion !== dataset.datasetVersion) return 'The selected suite is bound to a different dataset version.';
    if (!suite.evaluators.some((binding) => binding.evaluatorId === evaluator.evaluatorId && binding.evaluatorVersion === evaluator.evaluatorVersion)) return 'The selected evaluator is not an immutable member of the selected suite.';
    if (!this.data().cases?.items.length) return 'The selected dataset has no loaded case membership.';
    if (!suite.components?.length) return 'The selected suite has no authoritative component binding.';
    if (!suite.limits) return 'The selected suite has no authoritative execution limits.';
    if (!bindings) return 'Authoritative model, deployment, and reproducibility metadata is unavailable.';
    return null;
  });
  readonly hasData = computed(() => Object.values(this.data()).some((value) => value !== null));
  private generation = 0;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private requestController = new AbortController();
  private readonly actionKeys = new Map<string, string>();
  private loadInFlight = 0;
  private readonly deferredLiveRefreshes = new Set<EvaluationLiveEvent['resourceType']>();
  private liveRefreshInFlight = false;
  private liveRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private liveRefreshEpoch = 0;
  private readonly queuedLiveRefreshes = new Set<EvaluationLiveEvent['resourceType']>();

  private beginRequest(): { readonly generation: number; readonly signal: AbortSignal } {
    this.requestController.abort();
    this.requestController = new AbortController();
    return { generation: ++this.generation, signal: this.requestController.signal };
  }
  private current(generation: number, signal: AbortSignal): boolean { return generation === this.generation && !signal.aborted; }
  private runtimeState(cause: unknown): EvaluationWorkspaceState {
    const message = cause instanceof Error ? cause.message.toLowerCase() : String(cause).toLowerCase();
    if (message.includes('401') || message.includes('owner session')) return 'unauthorized';
    if (message.includes('local_judge') || message.includes('local judge') || message.includes('judge provider')) return 'provider-unavailable';
    if (message.includes('503')) return 'error';
    return 'error';
  }
  private requirePage<T>(value: unknown, parser: (raw: unknown) => T | null, label: string): T {
    const parsed = parser(value);
    if (!parsed) throw new Error(`${label} response was malformed or contained unsupported metadata.`);
    return parsed;
  }
  private reconcileScopeState(): void {
    const rejected = this.rejectedScopes();
    const unavailable = this.unavailableScopes();
    this.state.set(rejected.length ? 'rejected' : unavailable.length ? 'partial' : this.hasRows(this.data()) ? 'ready' : 'empty');
    this.error.set(rejected.length ? `Evaluation views were rejected: ${rejected.join(', ')}.` : unavailable.length ? `Some evaluation views are temporarily unavailable: ${unavailable.join(', ')}.` : null);
  }

  async load(): Promise<void> {
    this.loadInFlight += 1;
    const { generation, signal } = this.beginRequest();
    this.state.set('loading');
    this.error.set(null);
    try {
      await this.gateway.ensureSession();
      const requests = await Promise.allSettled([
        this.gateway.execute('listEvaluationDatasets', { ...blankInput('listEvaluationDatasets'), query: { page_size: 50 } }, signal).then((response) => {
          const page = parseEvaluationDatasetPage(response);
          if (!page) throw new Error('The evaluation dataset response was malformed.');
          return page;
        }),
        this.gateway.execute('listEvaluationSuites', { ...blankInput('listEvaluationSuites'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationSuitePage, 'Evaluation suite')),
        this.gateway.execute('listEvaluationEvaluators', { ...blankInput('listEvaluationEvaluators'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationEvaluatorPage, 'Evaluation evaluator')),
        this.gateway.execute('listEvaluationJobs', { ...blankInput('listEvaluationJobs'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationJobPage, 'Evaluation job')),
        this.gateway.execute('listEvaluationResults', { ...blankInput('listEvaluationResults'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationResultPage, 'Evaluation result')),
        this.gateway.execute('listEvaluationArtifacts', { ...blankInput('listEvaluationArtifacts'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationArtifactPage, 'Evaluation artifact')),
        this.gateway.execute('listEvaluationReviews', { ...blankInput('listEvaluationReviews'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationReviewPage, 'Evaluation review')),
        this.gateway.execute('listEvaluationCalibrations', { ...blankInput('listEvaluationCalibrations'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationCalibrationPage, 'Evaluation calibration')),
        this.gateway.execute('listEvaluationQualifications', { ...blankInput('listEvaluationQualifications'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationQualificationPage, 'Evaluation qualification')),
        this.gateway.execute('listEvaluationHistory', { ...blankInput('listEvaluationHistory'), query: { page_size: 50 } }, signal).then((response) => this.requirePage(response, parseEvaluationHistoryPage, 'Evaluation history')),
      ]);
      if (!this.current(generation, signal)) return;
      if (requests.some((result) => result.status === 'rejected' && result.reason instanceof ObservatorySessionRequiredError)) throw new ObservatorySessionRequiredError();
      const next: EvaluationWorkspaceData = {
        ...this.data(),
        datasets: requests[0].status === 'fulfilled' ? requests[0].value : null,
        suites: requests[1].status === 'fulfilled' ? requests[1].value : null,
        evaluators: requests[2].status === 'fulfilled' ? requests[2].value : null,
        jobs: requests[3].status === 'fulfilled' ? this.retainActiveJob(requests[3].value) : null,
        results: requests[4].status === 'fulfilled' ? requests[4].value : null,
        artifacts: requests[5].status === 'fulfilled' ? requests[5].value : null,
        reviews: requests[6].status === 'fulfilled' ? requests[6].value : null,
        calibrations: requests[7].status === 'fulfilled' ? requests[7].value : null,
        qualifications: requests[8].status === 'fulfilled' ? requests[8].value : null,
        history: requests[9].status === 'fulfilled' ? requests[9].value : null,
      };
      this.data.set(next);
      const scopeNames = ['datasets', 'suites', 'evaluators', 'jobs', 'results', 'artifacts', 'reviews', 'calibrations', 'qualifications', 'history'];
      const rejected = requests.flatMap((result, index) => result.status === 'rejected' ? [scopeNames[index]] : []);
      const unavailable = requests.flatMap((result, index) => result.status === 'fulfilled' && result.value.partial ? [scopeNames[index]] : []);
      this.rejectedScopes.set(rejected);
      this.unavailableScopes.set(unavailable);
      this.state.set(rejected.length ? 'rejected' : unavailable.length ? 'partial' : this.hasRows(next) ? 'ready' : 'empty');
      this.error.set(rejected.length ? `Evaluation views were rejected: ${rejected.join(', ')}.` : unavailable.length ? `Some evaluation views are temporarily unavailable: ${unavailable.join(', ')}.` : null);
      this.lastUpdatedAt.set(new Date().toISOString());
    } catch (cause) {
      if (!this.current(generation, signal)) return;
      this.state.set(cause instanceof ObservatorySessionRequiredError ? 'unauthorized' : this.runtimeState(cause));
      this.error.set(cause instanceof Error ? cause.message : 'Evaluation service is unavailable.');
    } finally {
      this.loadInFlight -= 1;
      if (this.loadInFlight === 0 && this.deferredLiveRefreshes.size) {
        const resources = [...this.deferredLiveRefreshes];
        this.deferredLiveRefreshes.clear();
        queueMicrotask(() => resources.forEach((resource) => void this.refreshResource(resource)));
      }
    }
  }

  async loadCases(dataset: EvaluationDataset): Promise<void> {
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('listEvaluationCases', { ...blankInput('listEvaluationCases'), path: { dataset_id: dataset.datasetId, resource_version: dataset.datasetVersion }, query: { page_size: 100 } }, signal);
      const cases = this.requirePage(response, parseEvaluationCasePage, 'Evaluation case');
      if (!this.current(generation, signal)) return;
      this.selectedDataset.set(dataset);
      this.data.update((value) => ({ ...value, cases }));
    } catch { if (this.current(generation, signal)) this.notice.set('Dataset cases are unavailable. The dataset metadata is still shown.'); }
  }

  async inspectDataset(dataset: EvaluationDataset): Promise<void> { await this.loadCases(dataset); }
  selectSuite(suite: EvaluationSuite): void { this.selectedSuite.set(suite); }
  selectEvaluator(evaluator: EvaluationEvaluator): void { this.selectedEvaluator.set(evaluator); }
  setJobAdmissionBindings(bindings: JobAdmissionBindings | null): void { this.jobBindings.set(bindings); }
  async inspectSuite(suiteId: string, version: string): Promise<void> {
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('getEvaluationSuite', { ...blankInput('getEvaluationSuite'), path: { suite_id: suiteId, resource_version: version }, query: {} }, signal);
      const suite = this.requirePage(response, parseEvaluationSuite, 'Evaluation suite');
      if (this.current(generation, signal)) this.selectedSuite.set(suite);
    } catch { if (this.current(generation, signal)) this.notice.set('Suite details are temporarily unavailable.'); }
  }
  async inspectResult(resultId: string): Promise<void> {
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('getEvaluationResult', { ...blankInput('getEvaluationResult'), path: { result_id: resultId }, query: {} }, signal);
      const result = this.requirePage(response, parseEvaluationResult, 'Evaluation result');
      if (!this.current(generation, signal)) return;
      this.selectedResult.set(result);
      await this.loadScores(resultId);
    } catch { if (this.current(generation, signal)) this.notice.set('Result details are temporarily unavailable.'); }
  }

  async createDataset(request: CreateEvaluationDatasetRequest): Promise<void> {
    if (this.busyAction() === 'create-dataset') return;
    this.busyAction.set('create-dataset');
    const key = this.actionKeys.get(`dataset:${request.datasetId}`) ?? uuid();
    this.actionKeys.set(`dataset:${request.datasetId}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('createEvaluationDataset', { ...blankInput('createEvaluationDataset'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request }, signal);
      const dataset = this.requirePage(response, parseEvaluationDataset, 'Evaluation dataset');
      if (!this.current(generation, signal)) return;
      this.notice.set(`Dataset ${dataset.datasetId} ${dataset.datasetVersion} was created.`); this.busyAction.set(null); void this.load();
    } catch (cause) { if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set('Dataset creation failed. Retry when the service is available.'); this.busyAction.set(null); } }
  }

  async createSuite(request: CreateEvaluationSuiteRequest): Promise<void> {
    if (this.busyAction() === 'create-suite') return;
    this.busyAction.set('create-suite');
    const key = this.actionKeys.get(`suite:${request.suiteId}:${request.suiteVersion}`) ?? uuid();
    this.actionKeys.set(`suite:${request.suiteId}:${request.suiteVersion}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('createEvaluationSuite', { ...blankInput('createEvaluationSuite'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request }, signal);
      const suite = this.requirePage(response, parseEvaluationSuite, 'Evaluation suite');
      if (!this.current(generation, signal)) return;
      this.selectedSuite.set(suite);
      this.notice.set(`Suite ${suite.suiteId} ${suite.suiteVersion} was created.`); this.busyAction.set(null); void this.load();
    } catch (cause) { if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set('Suite creation failed. Retry when the service is available.'); this.busyAction.set(null); } }
  }

  async submitReview(request: CreateEvaluationReviewRequest): Promise<void> {
    if (this.busyAction() === 'review') return;
    this.busyAction.set('review');
    const key = this.actionKeys.get(`review:${request.reviewId}`) ?? uuid();
    this.actionKeys.set(`review:${request.reviewId}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('createEvaluationReview', { ...blankInput('createEvaluationReview'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key }, body: request }, signal);
      const review = this.requirePage(response, parseEvaluationReview, 'Evaluation review');
      if (!this.current(generation, signal)) return;
      this.notice.set(`Review ${review.reviewId} was recorded.`); this.busyAction.set(null); void this.load();
    } catch (cause) { if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set('Review submission failed. Retry when the service is available.'); this.busyAction.set(null); } }
  }

  async qualifyJudge(request: QualifyEvaluationJudgeRequest): Promise<void> {
    if (this.busyAction()) throw new Error('Another evaluation action is already in progress. Retry when it completes.');
    this.busyAction.set('qualify');
    const key = this.actionKeys.get(`qualify:${request.qualificationId}`) ?? uuid();
    this.actionKeys.set(`qualify:${request.qualificationId}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('qualifyEvaluationJudge', { ...blankInput('qualifyEvaluationJudge'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key, 'X-Observatory-Confirmation': 'qualify-judge' }, body: request }, signal);
      const qualification = this.requirePage(response, parseEvaluationQualification, 'Evaluation qualification');
      if (!this.current(generation, signal)) return;
      this.notice.set(`Qualification ${qualification.qualificationId} is ${qualification.state}.`); this.busyAction.set(null); void this.load();
    } catch (cause) {
      const message = 'Qualification failed. Retry when the service is available.';
      if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set(message); this.busyAction.set(null); }
      throw new Error(message);
    }
  }

  async revokeQualification(qualificationId: string, request: RevokeEvaluationQualificationRequest): Promise<void> {
    if (this.busyAction()) throw new Error('Another evaluation action is already in progress. Retry when it completes.');
    this.busyAction.set(`revoke:${qualificationId}`);
    const key = this.actionKeys.get(`revoke:${qualificationId}`) ?? uuid();
    this.actionKeys.set(`revoke:${qualificationId}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('revokeEvaluationQualification', { ...blankInput('revokeEvaluationQualification'), path: { qualification_id: qualificationId }, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key, 'X-Observatory-Confirmation': 'revoke-qualification' }, body: request }, signal);
      const qualification = this.requirePage(response, parseEvaluationQualification, 'Evaluation qualification');
      if (!this.current(generation, signal)) return;
      this.notice.set(`Qualification ${qualification.qualificationId} is ${qualification.state}.`); this.busyAction.set(null); void this.load();
    } catch (cause) {
      const message = 'Qualification revocation failed. Retry when the service is available.';
      if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set(message); this.busyAction.set(null); }
      throw new Error(message);
    }
  }

  async refreshResource(resourceType: EvaluationLiveEvent['resourceType']): Promise<void> {
    if (this.loadInFlight > 0) {
      this.deferredLiveRefreshes.add(resourceType);
      return;
    }
    this.queuedLiveRefreshes.add(resourceType);
    if (this.liveRefreshInFlight || this.liveRefreshTimer !== null) return;
    this.liveRefreshTimer = setTimeout(() => {
      this.liveRefreshTimer = null;
      void this.drainLiveRefreshes();
    }, 50);
  }

  private async drainLiveRefreshes(): Promise<void> {
    if (this.liveRefreshInFlight || this.queuedLiveRefreshes.size === 0) return;
    this.liveRefreshInFlight = true;
    const epoch = this.liveRefreshEpoch;
    const resources = [...this.queuedLiveRefreshes];
    this.queuedLiveRefreshes.clear();
    try {
      for (const resourceType of resources) {
        if (epoch !== this.liveRefreshEpoch) return;
        await this.refreshResourceNow(resourceType);
      }
    } finally {
      this.liveRefreshInFlight = false;
      if (epoch === this.liveRefreshEpoch && this.queuedLiveRefreshes.size && this.liveRefreshTimer === null) {
        this.liveRefreshTimer = setTimeout(() => {
          this.liveRefreshTimer = null;
          void this.drainLiveRefreshes();
        }, 50);
      }
    }
  }

  private async refreshResourceNow(resourceType: EvaluationLiveEvent['resourceType']): Promise<void> {
    const resource = resourceType as string;
    if (resource === 'evaluation_case' || resource === 'evaluation_attempt' || resource === 'evaluation_deletion' || resource === 'evaluation_experiment' || resource === 'evaluation_baseline' || resource === 'evaluation_regression' || resource === 'evaluation_attribution') { await this.load(); return; }
    const pageSize = 50;
    const { generation, signal } = this.beginRequest();
    try {
      if (resourceType === 'evaluation_dataset') { const raw = await this.gateway.execute('listEvaluationDatasets', { ...blankInput('listEvaluationDatasets'), query: { page_size: pageSize } }, signal); const datasets = this.requirePage(raw, parseEvaluationDatasetPage, 'Evaluation dataset'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, datasets })); }
      else if (resourceType === 'evaluation_suite') { const raw = await this.gateway.execute('listEvaluationSuites', { ...blankInput('listEvaluationSuites'), query: { page_size: pageSize } }, signal); const suites = this.requirePage(raw, parseEvaluationSuitePage, 'Evaluation suite'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, suites })); }
      else if (resourceType === 'evaluation_evaluator') { const raw = await this.gateway.execute('listEvaluationEvaluators', { ...blankInput('listEvaluationEvaluators'), query: { page_size: pageSize } }, signal); const evaluators = this.requirePage(raw, parseEvaluationEvaluatorPage, 'Evaluation evaluator'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, evaluators })); }
      else if (resourceType === 'evaluation_job') { const raw = await this.gateway.execute('listEvaluationJobs', { ...blankInput('listEvaluationJobs'), query: { page_size: pageSize } }, signal); const jobs = this.requirePage(raw, parseEvaluationJobPage, 'Evaluation job'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, jobs: this.retainActiveJob(jobs) })); }
      else if (resourceType === 'evaluation_result') { const raw = await this.gateway.execute('listEvaluationResults', { ...blankInput('listEvaluationResults'), query: { page_size: pageSize } }, signal); const results = this.requirePage(raw, parseEvaluationResultPage, 'Evaluation result'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, results })); }
      else if (resourceType === 'evaluation_artifact') { const raw = await this.gateway.execute('listEvaluationArtifacts', { ...blankInput('listEvaluationArtifacts'), query: { page_size: pageSize } }, signal); const artifacts = this.requirePage(raw, parseEvaluationArtifactPage, 'Evaluation artifact'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, artifacts })); }
      else if (resourceType === 'evaluation_review') { const raw = await this.gateway.execute('listEvaluationReviews', { ...blankInput('listEvaluationReviews'), query: { page_size: pageSize } }, signal); const reviews = this.requirePage(raw, parseEvaluationReviewPage, 'Evaluation review'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, reviews })); }
      else if (resourceType === 'evaluation_calibration') { const raw = await this.gateway.execute('listEvaluationCalibrations', { ...blankInput('listEvaluationCalibrations'), query: { page_size: pageSize } }, signal); const calibrations = this.requirePage(raw, parseEvaluationCalibrationPage, 'Evaluation calibration'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, calibrations })); }
      else if (resourceType === 'evaluation_qualification') { const raw = await this.gateway.execute('listEvaluationQualifications', { ...blankInput('listEvaluationQualifications'), query: { page_size: pageSize } }, signal); const qualifications = this.requirePage(raw, parseEvaluationQualificationPage, 'Evaluation qualification'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, qualifications })); }
      else if (resource === 'evaluation_history') { const raw = await this.gateway.execute('listEvaluationHistory', { ...blankInput('listEvaluationHistory'), query: { page_size: pageSize } }, signal); const history = this.requirePage(raw, parseEvaluationHistoryPage, 'Evaluation history'); if (this.current(generation, signal)) this.data.update((value) => ({ ...value, history })); }
      else await this.load();
      if (this.current(generation, signal)) {
        this.lastUpdatedAt.set(new Date().toISOString());
        if (resourceType === 'evaluation_job') {
          const activeJob = this.data().jobs?.items.find((item) => item.jobId === this.activeJobId());
          if (activeJob && (activeJob.status === 'queued' || activeJob.status === 'running')) this.pollJob(activeJob.jobId);
        }
      }
    } catch (cause) { if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause) === 'provider-unavailable' ? 'provider-unavailable' : this.hasData() ? 'stale' : 'error'); this.notice.set('A live evaluation update could not be reconciled. Refresh to retry.'); } }
  }

  async retryScope(scope: string): Promise<void> {
    const allowed = new Set(['datasets', 'suites', 'evaluators', 'jobs', 'results', 'artifacts', 'reviews', 'calibrations', 'qualifications', 'history']);
    if (!allowed.has(scope) || this.busyAction()) return;
    const action = `retry:${scope}`;
    const { generation, signal } = this.beginRequest();
    this.busyAction.set(action);
    this.notice.set(`Retrying ${scope} metadata…`);
    try {
      await this.gateway.ensureSession();
      const query = { page_size: 50 };
      if (scope === 'datasets') { const raw = await this.gateway.execute('listEvaluationDatasets', { ...blankInput('listEvaluationDatasets'), query }, signal); const page = this.requirePage(raw, parseEvaluationDatasetPage, 'Evaluation dataset'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, datasets: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'suites') { const raw = await this.gateway.execute('listEvaluationSuites', { ...blankInput('listEvaluationSuites'), query }, signal); const page = this.requirePage(raw, parseEvaluationSuitePage, 'Evaluation suite'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, suites: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'evaluators') { const raw = await this.gateway.execute('listEvaluationEvaluators', { ...blankInput('listEvaluationEvaluators'), query }, signal); const page = this.requirePage(raw, parseEvaluationEvaluatorPage, 'Evaluation evaluator'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, evaluators: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'jobs') { const raw = await this.gateway.execute('listEvaluationJobs', { ...blankInput('listEvaluationJobs'), query }, signal); const page = this.requirePage(raw, parseEvaluationJobPage, 'Evaluation job'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, jobs: this.retainActiveJob(page) })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'results') { const raw = await this.gateway.execute('listEvaluationResults', { ...blankInput('listEvaluationResults'), query }, signal); const page = this.requirePage(raw, parseEvaluationResultPage, 'Evaluation result'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, results: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'artifacts') { const raw = await this.gateway.execute('listEvaluationArtifacts', { ...blankInput('listEvaluationArtifacts'), query }, signal); const page = this.requirePage(raw, parseEvaluationArtifactPage, 'Evaluation artifact'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, artifacts: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'reviews') { const raw = await this.gateway.execute('listEvaluationReviews', { ...blankInput('listEvaluationReviews'), query }, signal); const page = this.requirePage(raw, parseEvaluationReviewPage, 'Evaluation review'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, reviews: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'calibrations') { const raw = await this.gateway.execute('listEvaluationCalibrations', { ...blankInput('listEvaluationCalibrations'), query }, signal); const page = this.requirePage(raw, parseEvaluationCalibrationPage, 'Evaluation calibration'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, calibrations: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else if (scope === 'qualifications') { const raw = await this.gateway.execute('listEvaluationQualifications', { ...blankInput('listEvaluationQualifications'), query }, signal); const page = this.requirePage(raw, parseEvaluationQualificationPage, 'Evaluation qualification'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, qualifications: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      else { const raw = await this.gateway.execute('listEvaluationHistory', { ...blankInput('listEvaluationHistory'), query }, signal); const page = this.requirePage(raw, parseEvaluationHistoryPage, 'Evaluation history'); if (!this.current(generation, signal)) return; this.data.update((value) => ({ ...value, history: page })); this.unavailableScopes.update((items) => page.partial ? [...new Set([...items, scope])] : items.filter((item) => item !== scope)); }
      if (!this.current(generation, signal)) return;
      this.rejectedScopes.update((items) => items.filter((item) => item !== scope));
      this.reconcileScopeState();
      this.lastUpdatedAt.set(new Date().toISOString());
      this.notice.set(`${scope} metadata refreshed.`);
    } catch (cause) {
      if (this.current(generation, signal)) { this.rejectedScopes.update((items) => items.includes(scope) ? items : [...items, scope]); this.reconcileScopeState(); this.notice.set(cause instanceof Error ? cause.message : `The ${scope} metadata request was rejected.`); }
    } finally {
      if (this.busyAction() === action) this.busyAction.set(null);
    }
  }

  async loadScores(resultId: string): Promise<void> {
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('listEvaluationScores', { ...blankInput('listEvaluationScores'), path: { result_id: resultId }, query: { page_size: 100 } }, signal);
      const scores = this.requirePage(response, parseEvaluationScorePage, 'Evaluation score');
      if (this.current(generation, signal)) this.data.update((value) => ({ ...value, scores }));
    } catch { if (this.current(generation, signal)) this.notice.set('Scores are temporarily unavailable.'); }
  }

  async loadMore(resource: 'datasets' | 'cases' | 'suites' | 'evaluators' | 'jobs' | 'results' | 'scores' | 'artifacts' | 'reviews' | 'calibrations' | 'qualifications' | 'history'): Promise<void> {
    if (this.busyAction() === `load-more:${resource}`) return;
    const current = this.data()[resource];
    if (!current?.nextCursor) return;
    this.busyAction.set(`load-more:${resource}`);
    const { generation, signal } = this.beginRequest();
    try {
      if (resource === 'datasets') {
        const raw = await this.gateway.execute('listEvaluationDatasets', { ...blankInput('listEvaluationDatasets'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationDatasetPage, 'Evaluation dataset');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, datasets: { ...page, items: [...(value.datasets?.items ?? []), ...page.items] } }));
      } else if (resource === 'cases' && this.selectedDataset()) {
        const dataset = this.selectedDataset();
        if (!dataset) return;
        const raw = await this.gateway.execute('listEvaluationCases', { ...blankInput('listEvaluationCases'), path: { dataset_id: dataset.datasetId, resource_version: dataset.datasetVersion }, query: { page_size: 100, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationCasePage, 'Evaluation case');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, cases: { ...page, items: [...(value.cases?.items ?? []), ...page.items] } }));
      } else if (resource === 'suites') {
        const raw = await this.gateway.execute('listEvaluationSuites', { ...blankInput('listEvaluationSuites'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationSuitePage, 'Evaluation suite');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, suites: { ...page, items: [...(value.suites?.items ?? []), ...page.items] } }));
      } else if (resource === 'evaluators') {
        const raw = await this.gateway.execute('listEvaluationEvaluators', { ...blankInput('listEvaluationEvaluators'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationEvaluatorPage, 'Evaluation evaluator');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, evaluators: { ...page, items: [...(value.evaluators?.items ?? []), ...page.items] } }));
      } else if (resource === 'jobs') {
        const raw = await this.gateway.execute('listEvaluationJobs', { ...blankInput('listEvaluationJobs'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationJobPage, 'Evaluation job');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, jobs: { ...page, items: [...(value.jobs?.items ?? []), ...page.items] } }));
      } else if (resource === 'results') {
        const raw = await this.gateway.execute('listEvaluationResults', { ...blankInput('listEvaluationResults'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationResultPage, 'Evaluation result');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, results: { ...page, items: [...(value.results?.items ?? []), ...page.items] } }));
      } else if (resource === 'scores' && this.selectedResult()) {
        const result = this.selectedResult();
        if (!result) return;
        const raw = await this.gateway.execute('listEvaluationScores', { ...blankInput('listEvaluationScores'), path: { result_id: result.resultId }, query: { page_size: 100, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationScorePage, 'Evaluation score');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, scores: { ...page, items: [...(value.scores?.items ?? []), ...page.items] } }));
      } else if (resource === 'artifacts') {
        const raw = await this.gateway.execute('listEvaluationArtifacts', { ...blankInput('listEvaluationArtifacts'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationArtifactPage, 'Evaluation artifact');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, artifacts: { ...page, items: [...(value.artifacts?.items ?? []), ...page.items] } }));
      } else if (resource === 'reviews') {
        const raw = await this.gateway.execute('listEvaluationReviews', { ...blankInput('listEvaluationReviews'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationReviewPage, 'Evaluation review');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, reviews: { ...page, items: [...(value.reviews?.items ?? []), ...page.items] } }));
      } else if (resource === 'calibrations') {
        const raw = await this.gateway.execute('listEvaluationCalibrations', { ...blankInput('listEvaluationCalibrations'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationCalibrationPage, 'Evaluation calibration');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, calibrations: { ...page, items: [...(value.calibrations?.items ?? []), ...page.items] } }));
      } else if (resource === 'qualifications') {
        const raw = await this.gateway.execute('listEvaluationQualifications', { ...blankInput('listEvaluationQualifications'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationQualificationPage, 'Evaluation qualification');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, qualifications: { ...page, items: [...(value.qualifications?.items ?? []), ...page.items] } }));
      } else if (resource === 'history') {
        const raw = await this.gateway.execute('listEvaluationHistory', { ...blankInput('listEvaluationHistory'), query: { page_size: 50, cursor: current.nextCursor } }, signal);
        const page = this.requirePage(raw, parseEvaluationHistoryPage, 'Evaluation history');
        if (this.current(generation, signal)) this.data.update((value) => ({ ...value, history: { ...page, items: [...(value.history?.items ?? []), ...page.items] } }));
      }
    } catch { if (this.current(generation, signal)) this.notice.set(`More ${resource} are temporarily unavailable.`); }
    finally { if (this.current(generation, signal)) this.busyAction.set(null); }
  }

  async submitJob(): Promise<void> {
    if (this.busyAction() === 'submit') return;
    const dataset = this.selectedDataset(); const suite = this.selectedSuite(); const evaluator = this.selectedEvaluator(); const bindings = this.jobBindings();
    const admissionReason = this.jobAdmissionReason();
    if (admissionReason) { this.notice.set(admissionReason); return; }
    if (!dataset || !suite || !evaluator || !bindings || !suite.components || !suite.limits) return;
    const caseIds = this.data().cases?.items.map((item) => item.caseId) ?? [];
    const { generation, signal } = this.beginRequest();
    const requestId = uuid(); const now = new Date(); const deadline = new Date(now.getTime() + 15 * 60_000).toISOString();
    const component = suite.components[0];
    const definition = { id: dataset.datasetId, version: dataset.datasetVersion } as const;
    this.busyAction.set('submit'); this.notice.set(null);
    try {
      const job = await this.gateway.execute('submitEvaluationJob', {
        ...blankInput('submitEvaluationJob'),
        headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': requestId },
        body: { requestId, requestVersion: '1.0', dataset: definition, suite: { id: suite.suiteId, version: suite.suiteVersion }, evaluator: { id: evaluator.evaluatorId, version: evaluator.evaluatorVersion }, captureClassification: 'metadata_only', deadlineAt: deadline, maxAttempts: bindings.limits.maxAttempts, createdAt: now.toISOString(), requestData: { executionKind: 'synthetic_end_to_end', requestVersion: '1.0', targetComponent: component, captureAuthorization: { captureClassification: 'metadata_only', authorizationPurpose: 'synthetic_benchmark', authorizationContext: 'controlled_evaluation', retentionMode: 'manual_only' }, provenance: { dataset: definition, suite: { id: suite.suiteId, version: suite.suiteVersion }, evaluators: [{ evaluatorId: evaluator.evaluatorId, evaluatorVersion: evaluator.evaluatorVersion, kind: evaluator.kind }], components: suite.components, model: bindings.model, deploymentRevision: bindings.deploymentRevision, reproducibility: bindings.reproducibility }, limits: { execution: bindings.limits, maxQueueTimeMs: 30_000, maxModelCalls: 0, maxArtifactOperations: 0 }, deadlineAt: deadline, caseIds, inputArtifactReferences: [] } },
      }, signal);
      const parsedJob = this.requirePage(job, parseEvaluationJob, 'Evaluation job');
      if (!this.current(generation, signal)) return;
      this.activeJobId.set(parsedJob.jobId);
      this.upsertJob(parsedJob);
      this.notice.set(`Evaluation job ${parsedJob.jobId} was queued.`); this.busyAction.set(null); void this.load(); this.pollJob(parsedJob.jobId);
    } catch (cause) { if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set('The evaluation job could not be submitted. Retry when the service is available.'); this.busyAction.set(null); } }
  }

  async cancelJob(job: EvaluationJob): Promise<void> {
    if (this.busyAction()) throw new Error('Another evaluation action is already in progress. Retry when it completes.');
    this.busyAction.set(`cancel:${job.jobId}`);
    const key = this.actionKeys.get(`cancel:${job.jobId}`) ?? uuid();
    this.actionKeys.set(`cancel:${job.jobId}`, key);
    const { generation, signal } = this.beginRequest();
    try {
      const response = await this.gateway.execute('cancelEvaluationJob', { ...blankInput('cancelEvaluationJob'), path: { job_id: job.jobId }, headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': key, 'X-Observatory-Confirmation': 'cancel-job' }, body: { action: 'cancel', reason: 'OWNER_REQUESTED' } }, signal);
      const cancelled = this.requirePage(response, parseEvaluationJob, 'Evaluation job');
      if (!this.current(generation, signal)) return;
      this.notice.set(`Cancellation for ${cancelled.jobId} is ${cancelled.status}.`); this.busyAction.set(null); void this.load();
    } catch (cause) {
      const message = 'Cancellation failed. Retry when the service is available.';
      if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set(message); this.busyAction.set(null); }
      throw new Error(message);
    }
  }

  async delete(scope: 'job' | 'dataset' | 'suite' | 'cutoff' | 'purge_all', resourceId?: string, cutoff?: string): Promise<void> {
    if (scope === 'cutoff' && !cutoff) {
      this.notice.set('Choose a UTC cutoff before requesting deletion.');
      return;
    }
    const exact = 'DELETE_EVALUATION_ARTIFACTS';
    if (scope !== 'purge_all' && scope !== 'cutoff' && !resourceId) { this.notice.set(`Select a ${scope} before requesting deletion.`); return; }
    const actionKey = `delete:${scope}:${resourceId ?? cutoff ?? 'all'}`;
    const busyAction = `delete:${scope}`;
    if (this.busyAction()) throw new Error('Another evaluation action is already in progress. Retry when it completes.');
    this.busyAction.set(busyAction);
    const commandId = this.actionKeys.get(actionKey) ?? uuid();
    this.actionKeys.set(actionKey, commandId);
    const { generation, signal } = this.beginRequest();
    try {
      const requestedAt = new Date().toISOString();
      let body: CreateEvaluationDeletionRequest;
      if (scope === 'purge_all') body = { commandId, requestedAt, kind: 'purge_all' };
      else if (cutoff) body = { commandId, requestedAt, kind: 'cutoff', cutoff };
      else if (scope === 'job' && resourceId) body = { commandId, requestedAt, kind: 'job', jobId: resourceId };
      else if (scope === 'dataset' && resourceId) body = { commandId, requestedAt, kind: 'dataset', datasetId: resourceId };
      else if (scope === 'suite' && resourceId) body = { commandId, requestedAt, kind: 'suite', suiteId: resourceId };
      else { if (this.current(generation, signal)) this.notice.set('A deletion selector is required.'); return; }
      const response = await this.gateway.execute('createEvaluationDeletionAction', { ...blankInput('createEvaluationDeletionAction'), headers: { 'X-Observatory-CSRF': '', 'Idempotency-Key': commandId, 'X-Observatory-Confirmation': exact }, body }, signal);
      const action = this.requirePage(response, parseEvaluationDeletionAction, 'Evaluation deletion');
      if (!this.current(generation, signal)) return;
      this.data.update((value) => ({ ...value, deletion: action })); this.notice.set(`Deletion ${action.actionId} is ${action.status}.`); this.pollDeletion(action.actionId, busyAction);
    } catch (cause) {
      const message = 'Deletion request failed. Retry when the service is available.';
      if (this.current(generation, signal)) { this.state.set(this.runtimeState(cause)); this.notice.set(message); this.busyAction.set(null); }
      throw new Error(message);
    }
  }

  setEvaluationLiveDisconnected(disconnected: boolean): void {
    if (disconnected) this.state.set('disconnected');
    else if (this.state() === 'disconnected') this.state.set(this.hasData() ? 'stale' : 'loading');
  }

  clear(): void { this.requestController.abort(); this.generation += 1; this.liveRefreshEpoch += 1; this.deferredLiveRefreshes.clear(); this.queuedLiveRefreshes.clear(); if (this.liveRefreshTimer) clearTimeout(this.liveRefreshTimer); this.liveRefreshTimer = null; if (this.pollTimer) clearTimeout(this.pollTimer); this.pollTimer = null; this.data.set(emptyData()); this.selectedDataset.set(null); this.selectedSuite.set(null); this.selectedEvaluator.set(null); this.selectedResult.set(null); this.jobBindings.set(null); this.actionKeys.clear(); this.unavailableScopes.set([]); this.rejectedScopes.set([]); this.state.set('loading'); this.error.set(null); this.notice.set(null); this.busyAction.set(null); this.activeJobId.set(null); this.lastUpdatedAt.set(null); }
  private hasRows(data: EvaluationWorkspaceData): boolean { return [data.datasets, data.suites, data.evaluators, data.jobs, data.results, data.artifacts, data.reviews, data.calibrations, data.qualifications, data.history].some((page) => Boolean(page && page.items.length > 0)); }
  private upsertJob(job: EvaluationJob): void {
    this.data.update((value) => {
      const current = value.jobs;
      const items = (current?.items ?? []).filter((item) => item.jobId !== job.jobId);
      return {
        ...value,
        jobs: current
          ? { ...current, items: [job, ...items] }
          : { items: [job], nextCursor: null, snapshotAt: job.updatedAt, partial: false },
      };
    });
  }
  private retainActiveJob(page: EvaluationJobPage): EvaluationJobPage {
    const activeJobId = this.activeJobId();
    if (!activeJobId || page.items.some((item) => item.jobId === activeJobId)) return page;
    const activeJob = this.data().jobs?.items.find((item) => item.jobId === activeJobId);
    return activeJob ? { ...page, items: [activeJob, ...page.items] } : page;
  }
  private pollJob(jobId: string): void {
    const generation = this.generation; const signal = this.requestController.signal;
    this.activeJobId.set(jobId);
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(async () => {
      this.pollTimer = null;
      if (!this.current(generation, signal)) return;
      await this.load();
      if (!this.current(generation + 1, this.requestController.signal)) return;
      const job = this.data().jobs?.items.find((item) => item.jobId === jobId);
      if (job && (job.status === 'queued' || job.status === 'running')) this.pollJob(jobId);
    }, 1500);
  }
  private pollDeletion(actionId: string, busyAction: string): void {
    const generation = this.generation; const signal = this.requestController.signal;
    this.pollTimer = setTimeout(async () => {
      if (!this.current(generation, signal)) return;
      try {
        const response = await this.gateway.execute('getEvaluationDeletionAction', { ...blankInput('getEvaluationDeletionAction'), path: { action_id: actionId }, query: {} });
        const deletion = this.requirePage(response, parseEvaluationDeletionAction, 'Evaluation deletion');
        if (!this.current(generation, signal)) return;
        this.data.update((value) => ({ ...value, deletion }));
        if (deletion.status === 'queued' || deletion.status === 'running') this.pollDeletion(actionId, busyAction); else { this.busyAction.set(null); await this.load(); }
      } catch { if (this.current(generation, signal)) { this.notice.set('Deletion progress is temporarily unavailable.'); this.busyAction.set(null); } }
    }, 1200);
  }
}
