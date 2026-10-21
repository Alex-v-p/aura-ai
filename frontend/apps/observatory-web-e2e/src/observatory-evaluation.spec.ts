import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page, type Route } from '@playwright/test';

const datasetId = '11111111-1111-4111-8111-111111111111';
const actionId = '22222222-2222-4222-8222-222222222222';
const observedAt = '2026-10-21T00:00:00.000Z';
const csrfToken = 'fixture-evaluation-csrf-token-0123456789abcdef';
const suiteId = '22222222-2222-4222-8222-222222222222';
const evaluatorId = '33333333-3333-4333-8333-333333333333';
const caseId = '44444444-4444-4444-8444-444444444444';
const jobId = '55555555-5555-4555-8555-555555555555';
const resultId = '66666666-6666-4666-8666-666666666666';
const attemptId = '77777777-7777-4777-8777-777777777777';
const scoreId = '88888888-8888-4888-8888-888888888888';
const authRecordId = '99999999-9999-4999-8999-999999999999';
const componentBinding = { componentId: 'aura.core.api', componentVersion: '1.0.0', codeRevision: 'rev-core-1', imageDigest: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', configurationRevision: 'cfg-core-1' };
const limits = { timeoutMs: 30_000, maxConcurrency: 1, maxAttempts: 1, maxCases: 2, maxInputBytes: 0, maxOutputBytes: 0, maxTokens: 0 };
const modelBinding = { modelId: 'local-fixture-model', modelVersion: '1.0.0', modelDigest: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' };
const reproducibility = { seed: 7, clockMode: 'fixed', timeZone: 'UTC', locale: 'en-US', fixtureRevision: 'fixture-1', promptRevision: 'prompt-1', policyRevision: 'policy-1' };
const executionProvenance = { dataset: { resourceId: datasetId, resourceVersion: '1.0.0' }, suite: { resourceId: suiteId, resourceVersion: '1.0.0' }, evaluators: [{ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }], components: [componentBinding], model: modelBinding, deploymentRevision: 'deploy-fixture-1', reproducibility };

const envelope = (items: unknown[] = []) => ({ items, nextCursor: null, snapshotAt: observedAt, partial: false });
type EvaluationFixtureOptions = {
  anonymous?: boolean;
  evaluationSse?: 'reconnect' | 'disconnect';
  providerUnavailable?: boolean;
  rejectDatasets?: boolean;
  seedAllTables?: boolean;
  jobStatus?: 'queued' | 'running' | 'succeeded' | 'cancelled' | 'failed';
  deferJobSubmit?: boolean;
  deferResult?: boolean;
};
const dataset = {
  schemaVersion: 1,
  resourceType: 'dataset',
  datasetId,
  datasetVersion: '1.0.0',
  sourceKind: 'synthetic',
  scopeIds: ['private'],
  captureClassification: 'metadata_only',
  caseCount: 2,
  retentionMode: 'manual_only',
  provenance: { createdByComponent: 'aura.observatory.evaluation_api', createdByVersion: '1.0.0', sourceDigest: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' },
  state: 'active',
  createdAt: observedAt,
};
const caseItem = { schemaVersion: 1, resourceType: 'case', caseId, caseVersion: '1.0.0', dataset: { resourceId: datasetId, resourceVersion: '1.0.0' }, sourceKind: 'synthetic', scopeIds: ['private'], captureClassification: 'metadata_only', artifactIds: [], retentionMode: 'manual_only', provenance: dataset.provenance, createdAt: observedAt };
const suite = { schemaVersion: 1, resourceType: 'suite', suiteId, suiteVersion: '1.0.0', dataset: { resourceId: datasetId, resourceVersion: '1.0.0' }, evaluators: [{ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }], componentIds: ['aura.core.api'], components: [componentBinding], caseBudget: 2, timeoutMs: 30_000, limits, retentionMode: 'manual_only', provenance: dataset.provenance, state: 'active', createdAt: observedAt };
const evaluator = { schemaVersion: 1, resourceType: 'evaluator', evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic', metric: 'quality.pass_rate', componentIds: ['aura.core.api'], rubricVersion: '1.0.0', calibrationVersion: '1.0.0', retentionMode: 'manual_only', provenance: dataset.provenance, createdAt: observedAt };
const job = (status: 'queued' | 'running' | 'succeeded' | 'cancelled' | 'failed') => ({ schemaVersion: 1, resourceType: 'job', jobId, suite: { resourceId: suiteId, resourceVersion: '1.0.0' }, status, captureClassification: 'metadata_only', attemptCount: 1, resultCount: status === 'succeeded' ? 1 : 0, artifactCount: 0, deadlineAt: '2026-10-21T01:00:00.000Z', failureCode: status === 'failed' ? 'runner_failed' : null, provenance: executionProvenance, createdAt: observedAt, updatedAt: observedAt });
const result = { schemaVersion: 1, resourceType: 'result', resultId, jobId, attemptId, status: 'completed', captureClassification: 'metadata_only', scoreCount: 1, artifactCount: 0, partialEvidence: false, reproducible: true, provenance: executionProvenance, createdAt: observedAt, completedAt: observedAt };
const score = { schemaVersion: 1, resourceType: 'score', scoreId, resultId, caseId, evaluator: { evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }, component: componentBinding, metric: 'quality.pass_rate', value: 1, outcome: 'pass', evidenceStatus: 'observed', uncertainty: { method: 'none', lower: 1, upper: 1 }, scoreDisposition: 'deterministic', provenance: executionProvenance, createdAt: observedAt };
const judgeDefinitionId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaae';
const calibrationId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaba';
const calibration = { calibrationId, calibrationVersion: '1.0.0', dataset: { resourceId: datasetId, resourceVersion: '1.0.0' }, suite: { resourceId: suiteId, resourceVersion: '1.0.0' }, evaluator: { evaluatorId, evaluatorVersion: '1.0.0', kind: 'local_judge' }, judgeDefinition: { resourceId: judgeDefinitionId, resourceVersion: '1.0.0' }, sampleCount: 2, agreementRate: 1, meanAbsoluteError: 0, disagreementRate: 0, eligibility: 'eligible', advisory: false, provenance: dataset.provenance, createdAt: observedAt };
const qualification = (state: 'qualified' | 'revoked') => ({ schemaVersion: 1, resourceType: 'judge_qualification', qualificationId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', qualificationVersion: '1.0.0', state, dataset: { resourceId: datasetId, resourceVersion: '1.0.0' }, suite: { resourceId: suiteId, resourceVersion: '1.0.0' }, evaluator: { evaluatorId, evaluatorVersion: '1.0.0', kind: 'local_judge' }, judgeDefinition: { resourceId: judgeDefinitionId, resourceVersion: '1.0.0' }, calibration: { resourceId: calibrationId, resourceVersion: '1.0.0' }, calibrationResultId: resultId, authorizationRecordId: authRecordId, scoreDisposition: state === 'qualified' ? 'calibrated_enabled' : 'advisory_only', provenance: dataset.provenance, createdAt: observedAt });
const reviewItem = { schemaVersion: 1, resourceType: 'human_review', reviewId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab', reviewVersion: '1.0.0', calibration: { resourceId: calibrationId, resourceVersion: '1.0.0' }, caseId, resultId, scoreId, reviewScore: 1, reviewOutcome: 'pass', reviewLabel: 'accepted', disagreement: 'none', authorizationRecordId: authRecordId, commentArtifactId: null, provenance: dataset.provenance, createdAt: observedAt };
const historyItem = { historyId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac', resourceType: 'evaluation_job', resourceId: jobId, resourceVersion: '1.0.0', change: 'created', state: 'queued', occurredAt: observedAt };
const artifact = (artifactId: string, purpose: 'judge_prompt' | 'judge_rubric') => ({ schemaVersion: 1, resourceType: 'artifact', artifactId, artifactVersion: '1.0.0', jobId: null, resultId: null, purpose, mediaClassification: 'evaluation_context', mediaType: 'text/plain', digest: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', byteCount: 32, captureClassification: 'metadata_only', authorizationRecordId: authRecordId, retentionMode: 'manual_only', deletionStatus: 'retained', provenance: executionProvenance, createdAt: observedAt });

async function installEvaluationFixture(page: Page, options: EvaluationFixtureOptions = {}): Promise<{
  deletionRequests: Array<{ body: Record<string, unknown>; headers: Record<string, string> }>;
  commandRequests: Array<{ path: string; body: Record<string, unknown>; headers: Record<string, string> }>;
  evaluationGetRequests: string[];
  completeJob: () => void;
  getEvaluationEventConnections: () => number;
  getEvaluationLastEventIds: () => string[];
  restoreSession: () => void;
  releaseJobSubmit: () => void;
  releaseResult: () => void;
}> {
  const deletionRequests: Array<{ body: Record<string, unknown>; headers: Record<string, string> }> = [];
  const commandRequests: Array<{ path: string; body: Record<string, unknown>; headers: Record<string, string> }> = [];
  const evaluationGetRequests: string[] = [];
  let deletionPolls = 0;
  let jobStatus: 'queued' | 'running' | 'succeeded' | 'cancelled' | 'failed' = options.jobStatus ?? 'queued';
  let activeSuite = suite;
  let qualificationState: 'qualified' | 'revoked' | null = options.seedAllTables ? 'qualified' : null;
  let eventConnections = 0;
  const evaluationLastEventIds: string[] = [];
  let loggedOut = options.anonymous ?? false;
  let jobSubmitResolver: (() => void) | null = null;
  let resultResolver: (() => void) | null = null;
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith('/auth/logout') && request.method() === 'POST') {
      loggedOut = true;
      await route.fulfill({ status: 204 });
      return;
    }
    if (path.endsWith('/auth/me')) {
      if (loggedOut) { await route.fulfill({ status: 401, json: { code: 'session_required' } }); return; }
      await route.fulfill({ json: { csrfToken } });
      return;
    }
    if (path.endsWith('/evaluation/events')) {
      eventConnections += 1;
      evaluationLastEventIds.push(request.headers()['last-event-id'] ?? '');
      if ((options.evaluationSse === 'disconnect' || options.evaluationSse === 'reconnect') && eventConnections === 1) {
        await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' }, body: `retry: 25\nid: aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab\nevent: evaluation.changed\ndata: {"schemaVersion":1,"eventId":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab","eventType":"evaluation.changed","occurredAt":"${observedAt}","resourceType":"evaluation_job","resourceId":"${jobId}","change":"updated","state":"running","capturePolicy":"metadata_only"}\n\n` });
        return;
      }
      await new Promise<void>(() => undefined);
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' }, body: `retry: 60000\nid: 33333333-3333-4333-8333-333333333333\nevent: heartbeat\ndata: {}\n\n` });
      return;
    }
    if (path.endsWith('/overview')) {
      await route.fulfill({ json: { observedAt, systemState: 'healthy', observationCount: 1, runCount: 0, latestObservationId: null, ingest: { state: 'healthy', counts: { accepted: 1, rejected: 0, duplicate: 0, malformed: 0, storageFailure: 0 }, lastAcceptedAt: observedAt, lastFailureAt: null } } });
      return;
    }
    if (path.endsWith('/health/history')) {
      await route.fulfill({ json: envelope() });
      return;
    }
    if (path.endsWith('/runs')) {
      await route.fulfill({ json: { ...envelope(), latestObservationId: null } });
      return;
    }
    if (path.endsWith('/components')) {
      await route.fulfill({ json: { snapshotAt: observedAt, items: [] } });
      return;
    }
    if (path.endsWith('/deployments')) {
      await route.fulfill({ json: envelope() });
      return;
    }
    if (path.endsWith('/analytics/metrics')) {
      await route.fulfill({ json: { snapshotAt: observedAt, capturePolicy: 'metadata_only', limits: { defaultWindowHours: 24, maximumWindowDays: 31, maximumPointsPerSeries: 200, maximumSeries: 12, maximumBreakdowns: 50, maximumScannedRows: 1000, executionTimeoutMs: 5000 }, items: [] } });
      return;
    }
    if (path.includes('/analytics/query')) {
      await route.fulfill({ json: { snapshotAt: observedAt, percentileMethod: 'clickhouse_quantile_exact', capturePolicy: 'metadata_only', query: {}, series: [], breakdowns: [], freshness: { latestObservedAt: observedAt, lagMs: 0, state: 'fresh' }, partial: false, partialReasons: [], unavailableSignals: [], scan: { scannedRows: 0, scanLimit: 1000, executionMs: 1, executionLimitMs: 5000 } } });
      return;
    }
    if (path.startsWith('/api/v1/evaluation/')) {
      if (request.method() === 'GET') evaluationGetRequests.push(path);
      if (path.endsWith('/datasets') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        await route.fulfill({ status: 201, json: dataset });
        return;
      }
      if (path.endsWith('/suites') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        const requestDataset = (body.dataset ?? {}) as Record<string, unknown>;
        const requestLimits = (body.limits ?? {}) as Record<string, unknown>;
        activeSuite = {
          ...suite,
          suiteId: String(body.suiteId),
          suiteVersion: String(body.suiteVersion),
          dataset: { resourceId: String(requestDataset.id), resourceVersion: String(requestDataset.version) },
          evaluators: (body.evaluators ?? suite.evaluators) as typeof suite.evaluators,
          components: (body.components ?? suite.components) as typeof suite.components,
          caseBudget: Number(requestLimits.maxCases),
          timeoutMs: Number(requestLimits.timeoutMs),
          limits: requestLimits as typeof suite.limits,
          provenance: (body.provenance ?? suite.provenance) as typeof suite.provenance,
        };
        await route.fulfill({ status: 201, json: activeSuite });
        return;
      }
      if (path.endsWith('/jobs') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        if (options.providerUnavailable) { await route.fulfill({ status: 503, json: { code: 'provider_unavailable', message: 'local judge provider unavailable', retryable: true } }); return; }
        if (options.deferJobSubmit) await new Promise<void>((resolve) => { jobSubmitResolver = resolve; });
        jobStatus = 'running';
        await route.fulfill({ status: 202, json: job(jobStatus) });
        return;
      }
      if (path.endsWith(`/jobs/${jobId}/cancel`) && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        jobStatus = 'cancelled';
        await route.fulfill({ status: 202, json: job(jobStatus) });
        return;
      }
      if (path.endsWith('/reviews') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        await route.fulfill({ status: 201, json: { schemaVersion: 1, resourceType: 'human_review', reviewId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab', reviewVersion: '1.0.0', calibration: { resourceId: calibrationId, resourceVersion: '1.0.0' }, caseId, resultId, scoreId, reviewScore: 1, reviewOutcome: 'pass', reviewLabel: 'accepted', disagreement: 'none', authorizationRecordId: authRecordId, commentArtifactId: null, provenance: dataset.provenance, createdAt: observedAt } });
        return;
      }
      if (path.endsWith('/qualifications') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        qualificationState = 'qualified';
        await route.fulfill({ status: 201, json: qualification('qualified') });
        return;
      }
      if (path.includes('/qualifications/') && path.endsWith('/revoke') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        commandRequests.push({ path, body, headers: request.headers() });
        qualificationState = 'revoked';
        await route.fulfill({ status: 202, json: qualification('revoked') });
        return;
      }
      if (path.endsWith('/deletion-actions') && request.method() === 'POST') {
        const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
        deletionRequests.push({ body, headers: request.headers() });
        await route.fulfill({ status: 202, json: { actionId, scope: body.kind, resourceId: null, resourceVersion: null, cutoff: body.cutoff ?? null, status: 'queued', deletedArtifactCount: 0, deletedMetadataCount: 0, failureCode: null, createdAt: observedAt, startedAt: null, finishedAt: null } });
        return;
      }
      if (path.endsWith(`/deletion-actions/${actionId}`)) {
        deletionPolls += 1;
        await route.fulfill({ json: { actionId, scope: 'purge_all', resourceId: null, resourceVersion: null, cutoff: null, status: deletionPolls > 1 ? 'succeeded' : 'running', deletedArtifactCount: deletionPolls > 1 ? 1 : 0, deletedMetadataCount: deletionPolls > 1 ? 1 : 0, failureCode: null, createdAt: observedAt, startedAt: observedAt, finishedAt: deletionPolls > 1 ? observedAt : null } });
        return;
      }
      if (path.endsWith('/datasets')) {
        if (options.rejectDatasets) { await route.fulfill({ status: 503, json: { code: 'evaluation_scope_rejected' } }); return; }
        const pageData = url.searchParams.has('cursor') ? { ...dataset, datasetId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaf' } : dataset;
        await route.fulfill({ json: { ...envelope([pageData]), nextCursor: url.searchParams.has('cursor') ? null : 'datasets-next' } });
        return;
      }
      if (path.endsWith('/cases')) {
        await route.fulfill({ json: envelope([caseItem]) });
        return;
      }
      if (path.endsWith('/suites')) {
        await route.fulfill({ json: envelope([activeSuite]) });
        return;
      }
      if (path.endsWith('/evaluators')) {
        await route.fulfill({ json: envelope([evaluator]) });
        return;
      }
      if (path.endsWith('/jobs')) {
        await route.fulfill({ json: envelope([job(jobStatus)]) });
        return;
      }
      if (path.endsWith('/results')) {
        await route.fulfill({ json: envelope([result]) });
        return;
      }
      if (path.includes('/results/') && !path.endsWith('/scores')) {
        if (options.deferResult) await new Promise<void>((resolve) => { resultResolver = resolve; });
        await route.fulfill({ json: result });
        return;
      }
      if (path.endsWith('/scores')) {
        await route.fulfill({ json: envelope([score]) });
        return;
      }
      if (path.endsWith('/calibrations')) {
        await route.fulfill({ json: envelope([calibration]) });
        return;
      }
      if (path.endsWith('/qualifications')) {
        await route.fulfill({ json: envelope(qualificationState ? [qualification(qualificationState)] : []) });
        return;
      }
      if (path.endsWith('/reviews')) {
        await route.fulfill({ json: envelope(options.seedAllTables ? [reviewItem] : []) });
        return;
      }
      if (path.endsWith('/history')) {
        await route.fulfill({ json: envelope(options.seedAllTables ? [historyItem] : []) });
        return;
      }
      if (path.endsWith('/artifacts')) {
        await route.fulfill({ json: envelope([artifact('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac', 'judge_prompt'), artifact('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad', 'judge_rubric')]) });
        return;
      }
      await route.fulfill({ json: envelope() });
      return;
    }
    await route.continue();
  });
  return { deletionRequests, commandRequests, evaluationGetRequests, completeJob: () => { jobStatus = 'succeeded'; }, getEvaluationEventConnections: () => eventConnections, getEvaluationLastEventIds: () => [...evaluationLastEventIds], restoreSession: () => { loggedOut = false; }, releaseJobSubmit: () => { jobSubmitResolver?.(); jobSubmitResolver = null; }, releaseResult: () => { resultResolver?.(); resultResolver = null; } };
}

async function openEvaluationWorkspace(page: Page): Promise<Locator> {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
  await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
  const workspace = page.locator('observatory-evaluation-workspace');
  await expect(workspace.getByRole('heading', { name: 'Datasets and cases', exact: true })).toBeVisible();
  await expect(workspace.getByRole('row', { name: new RegExp(datasetId) })).toBeVisible();
  return workspace;
}
async function selectWorkspace(page: Page, workspace: Locator, tab: string): Promise<void> {
  if ((page.viewportSize()?.width ?? 1280) <= 640) await workspace.getByRole('combobox', { name: 'Evaluation workspace' }).selectOption(tab);
  else await workspace.getByRole('tab', { name: new RegExp(tab === 'datasets' ? 'Datasets' : tab, 'i') }).click();
  const headings: Record<string, string> = { datasets: 'Datasets and cases', suites: 'Suite composition', evaluators: 'Evaluator bindings', jobs: 'Jobs and progress', results: 'Results, scores, coverage, and reproducibility', reviews: 'Human review and calibration', qualification: 'Judge qualification', retention: 'Data retention' };
  const heading = headings[tab];
  if (heading) await expect(workspace.getByRole('heading', { name: heading, exact: true })).toBeVisible();
}
async function selectVersion(workspace: Locator): Promise<void> {
  const button = workspace.getByRole('button', { name: 'Use this version' }).first();
  await button.click();
  await expect(button).toHaveAttribute('aria-pressed', 'true');
}
async function prepareAuthoritativeJob(page: Page, workspace: Locator): Promise<void> {
  const inspect = workspace.getByRole('button', { name: 'Inspect cases' });
  await inspect.click();
  await expect(workspace.getByRole('row', { name: new RegExp(caseId) })).toBeVisible();
  await expect(inspect).toHaveAttribute('aria-pressed', 'true');
  await selectWorkspace(page, workspace, 'suites');
  await selectVersion(workspace);
  await selectWorkspace(page, workspace, 'evaluators');
  await selectVersion(workspace);
  await selectWorkspace(page, workspace, 'jobs');
  for (const [label, value] of [['Evaluation model ID', modelBinding.modelId], ['Evaluation model version', modelBinding.modelVersion], ['Evaluation model digest', modelBinding.modelDigest], ['Evaluation deployment revision', 'deploy-fixture-1'], ['Evaluation reproducibility seed', '7'], ['Evaluation fixture revision', 'fixture-1'], ['Evaluation prompt revision', 'prompt-1'], ['Evaluation policy revision', 'policy-1']] as const) await workspace.getByLabel(label).fill(value);
  const bind = workspace.getByRole('button', { name: 'Bind selected metadata' });
  await expect(bind).toBeEnabled();
  await bind.click();
}

test.describe('Observatory evaluation workspace browser acceptance', () => {
  test('renders bounded metadata, supports keyboard tab navigation, and performs confirmed deletion', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { seedAllTables: true });
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();
    await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
    const workspace = page.locator('observatory-evaluation-workspace');
    await expect(workspace.getByRole('heading', { name: 'Datasets and cases', exact: true })).toBeVisible();
    await expect(workspace.getByRole('row', { name: new RegExp(datasetId) })).toBeVisible();

    const viewportWidth = page.viewportSize()?.width ?? 1280;
    if (viewportWidth <= 640) {
      const selector = workspace.getByRole('combobox', { name: 'Evaluation workspace' });
      await expect(selector).toBeVisible();
      await expect(workspace.getByRole('tablist')).toHaveCount(0);
      const expectedLabels: Record<string, string[][]> = {
        datasets: [['Dataset', 'Version', 'Cases', 'State', 'Capture', 'Action'], ['Case', 'Version', 'Artifacts']],
        suites: [['Suite', 'Dataset', 'Evaluators', 'Budget', 'State', 'Action']],
        evaluators: [['Evaluator', 'Kind', 'Metric', 'Components', 'Version', 'Action']],
        jobs: [['Job', 'Status', 'Attempts', 'Results', 'Recovery', 'Action']],
        results: [['Result', 'Status', 'Scores', 'Evidence', 'Reproducible'], ['Metric', 'Value', 'Outcome', 'Disposition']],
        artifacts: [['Artifact', 'Purpose', 'Media', 'Bytes', 'Retention']],
        reviews: [['Review', 'Label', 'Outcome', 'Disagreement', 'Calibration']],
        qualification: [['Qualification', 'State', 'Disposition', 'Calibration', 'Action']],
        history: [['Resource', 'Change', 'State', 'Occurred']],
      };
      for (const width of [viewportWidth, 320]) {
        await page.setViewportSize({ width, height: page.viewportSize()?.height ?? 844 });
        for (const tab of Object.keys(expectedLabels)) {
          await selector.selectOption(tab);
          if (tab === 'datasets') await workspace.getByRole('button', { name: 'Inspect cases' }).click();
          if (tab === 'results') await workspace.getByRole('button', { name: resultId }).click();
          const tables = workspace.locator('table.data-table');
          await expect(tables).toHaveCount(expectedLabels[tab].length);
          for (const [index, expected] of expectedLabels[tab].entries()) {
            const table = tables.nth(index);
            const headers = (await table.locator('thead th').allTextContents()).map((value) => value.trim());
            expect(headers).toEqual(expected);
            const row = table.locator('tbody tr').filter({ hasNot: table.locator('td.empty') }).first();
            const labels = await row.locator('th,td').evaluateAll((cells) => cells.map((cell) => getComputedStyle(cell, '::before').content.replace(/^"|"$/g, '')));
            expect(labels).toEqual(headers);
          }
        }
      }
      await page.setViewportSize({ width: viewportWidth, height: page.viewportSize()?.height ?? 844 });
      await selector.selectOption('retention');
    } else {
      const tabs = workspace.getByRole('tab');
      await expect(workspace.getByRole('tablist')).toBeVisible();
      await tabs.first().focus();
      await page.keyboard.press('End');
      await expect(tabs.filter({ hasText: 'Retention' })).toBeFocused();
      await page.keyboard.press('Home');
      await expect(tabs.first()).toBeFocused();
      await page.keyboard.press('ArrowRight');
      await expect(tabs.nth(1)).toBeFocused();
      await tabs.filter({ hasText: 'Retention' }).focus();
      await page.keyboard.press('Enter');
    }
    await expect(workspace.getByRole('heading', { name: 'Data retention', exact: true })).toBeVisible();
    const cutoff = workspace.getByRole('textbox', { name: 'Deletion cutoff in local time' });
    await cutoff.fill('2026-10-01T00:00');
    page.once('dialog', (dialog) => void dialog.accept());
    const deleteBefore = workspace.getByRole('button', { name: 'Delete before cutoff', exact: true });
    await deleteBefore.scrollIntoViewIfNeeded();
    await deleteBefore.click();
    await expect.poll(() => fixture.deletionRequests.length).toBe(1);
    expect(fixture.deletionRequests[0]?.headers['x-observatory-csrf']).toBe(csrfToken);
    expect(fixture.deletionRequests[0]?.headers['idempotency-key']).toBeTruthy();
    expect(fixture.deletionRequests[0]?.headers['x-observatory-confirmation']).toBe('DELETE_EVALUATION_ARTIFACTS');
    await expect(workspace.locator('.progress')).toContainText(/Deletion .*succeeded/i);

    const storage = await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage), url: location.href }));
    expect(storage.local.join(' ')).not.toMatch(/dataset|evaluation|api/i);
    expect(storage.session.join(' ')).not.toMatch(/dataset|evaluation|api/i);
    expect(storage.url).not.toMatch(/token|credential|secret/i);
    const axe = await new AxeBuilder({ page }).include('observatory-evaluation-workspace').analyze();
    expect(axe.violations).toEqual([]);
    const originalViewport = page.viewportSize();
    for (const equivalentWidth of [640, 320]) {
      await page.setViewportSize({ width: equivalentWidth, height: originalViewport?.height ?? 900 });
      await expect(workspace.getByRole('heading', { name: 'Data retention', exact: true })).toBeVisible();
      const layout = await page.evaluate(() => { const root = document.documentElement; const action = document.querySelector<HTMLButtonElement>('observatory-evaluation-workspace button.danger'); action?.focus(); const rect = action?.getBoundingClientRect(); return { overflow: root.scrollWidth - root.clientWidth, actionReachable: Boolean(rect && rect.width > 0 && rect.height > 0), focusOutline: action ? getComputedStyle(action).outlineStyle : 'none' }; });
      expect(layout.overflow).toBeLessThanOrEqual(2);
      expect(layout.actionReachable).toBe(true);
      expect(layout.focusOutline).not.toBe('none');
    }
    if (originalViewport) await page.setViewportSize(originalViewport);
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(workspace.getByRole('heading', { name: 'Data retention', exact: true })).toBeVisible();
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--on-danger').trim())).toBe('#24141b');
  });

  test('walks authoritative dataset, case, suite, evaluator, job progress, and terminal result routes', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await expect.poll(() => fixture.getEvaluationEventConnections()).toBeGreaterThan(0);
    await prepareAuthoritativeJob(page, workspace);
    await expect(workspace.getByRole('button', { name: 'Submit evaluation' })).toBeEnabled();
    await workspace.getByRole('button', { name: 'Submit evaluation' }).click();
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/jobs')).length).toBe(1);
    const submission = fixture.commandRequests.find((request) => request.path.endsWith('/jobs'));
    expect(submission?.body).toMatchObject({ requestId: expect.any(String), dataset: { id: datasetId, version: '1.0.0' }, suite: { id: suiteId, version: '1.0.0' }, evaluator: { id: evaluatorId, version: '1.0.0' }, captureClassification: 'metadata_only' });
    expect((submission?.body.requestData as Record<string, unknown>).caseIds).toEqual([caseId]);
    await expect(workspace.getByRole('row', { name: new RegExp(`${jobId}.*running`, 'i') })).toBeVisible();
    fixture.completeJob();
    await workspace.getByRole('button', { name: 'Refresh' }).click();
    await expect(workspace.getByRole('row', { name: new RegExp(`${jobId}.*succeeded`, 'i') })).toBeVisible();
    await selectWorkspace(page, workspace, 'results');
    await workspace.getByRole('button', { name: new RegExp(resultId) }).click();
    await expect(workspace.getByRole('heading', { name: 'Scores for selected result' })).toBeVisible();
  });

  test('composes an authoritative immutable suite with bounded metadata', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await selectWorkspace(page, workspace, 'suites');
    const compose = workspace.getByRole('button', { name: 'Compose immutable suite' });
    await expect(compose).toBeDisabled();
    await workspace.getByLabel('Suite dataset version').selectOption(`${datasetId}|1.0.0`);
    await workspace.getByLabel('Suite evaluator version').selectOption(`${evaluatorId}|1.0.0`);
    for (const [label, value] of [
      ['Suite version', '2.0.0'], ['Suite component ID', 'aura.core.api'], ['Suite component version', '1.0.0'], ['Suite code revision', 'rev-core-1'],
      ['Suite image digest', componentBinding.imageDigest], ['Suite configuration revision', 'cfg-core-1'], ['Suite case budget', '2'], ['Suite timeout milliseconds', '30000'],
      ['Suite max concurrency', '1'], ['Suite max attempts', '1'], ['Suite max cases', '2'], ['Suite max input bytes', '0'], ['Suite max output bytes', '0'],
      ['Suite max tokens', '0'], ['Suite threshold metric', 'quality.pass_rate'], ['Suite threshold value', '0.8'], ['Suite provenance component', 'aura.observatory.evaluation_api'],
      ['Suite provenance version', '1.0.0'], ['Suite provenance source digest', dataset.provenance.sourceDigest],
    ] as const) await workspace.getByLabel(label).fill(value);
    await expect(workspace.getByText(/At least \(≥\) semantics are fixed/)).toBeVisible();
    await expect(compose).toBeEnabled();
    await compose.focus();
    await page.keyboard.press('Enter');
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/suites')).length).toBe(1);
    const request = fixture.commandRequests.find((item) => item.path.endsWith('/suites'));
    expect(request?.body).toMatchObject({ suiteVersion: '2.0.0', dataset: { id: datasetId, version: '1.0.0' }, evaluators: [{ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }], components: [componentBinding], limits, manualThresholds: { 'quality.pass_rate': 0.8 }, retentionMode: 'manual_only', provenance: { createdByComponent: 'aura.observatory.evaluation_api', createdByVersion: '1.0.0', sourceDigest: dataset.provenance.sourceDigest } });
    expect(request?.body.suiteId).not.toBe(suiteId);
    const createdSuiteId = String(request?.body.suiteId);
    await expect(workspace.getByRole('row', { name: new RegExp(createdSuiteId) })).toBeVisible();
    await expect(workspace.getByRole('heading', { name: 'Selected suite created', exact: true })).toBeVisible();
    await expect(workspace.locator('#created-suite-summary')).toBeFocused();
    await expect(workspace.getByText(/minimum sample 1, missing evidence inconclusive/)).toBeVisible();
    if ((page.viewportSize()?.width ?? 0) <= 640) {
      await page.setViewportSize({ width: 320, height: 900 });
      const layout = await page.evaluate(() => {
        const root = document.documentElement;
        const summary = document.querySelector<HTMLElement>('#created-suite-summary');
        const action = document.querySelector<HTMLButtonElement>('.suite-actions button.primary');
        const summaryRect = summary?.getBoundingClientRect();
        const actionRect = action?.getBoundingClientRect();
        return { overflow: root.scrollWidth - root.clientWidth, summaryOverflow: summary ? summary.scrollWidth - summary.clientWidth : 999, summaryHeight: summaryRect?.height ?? 0, actionReachable: Boolean(actionRect && actionRect.width > 0 && actionRect.height > 0) };
      });
      expect(layout.overflow).toBeLessThanOrEqual(2);
      expect(layout.summaryOverflow).toBeLessThanOrEqual(2);
      expect(layout.summaryHeight).toBeGreaterThan(0);
      expect(layout.actionReachable).toBe(true);
    }
    const storage = await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }));
    expect(storage.local.join(' ')).not.toMatch(/suite|dataset|evaluation|api/i);
    expect(storage.session.join(' ')).not.toMatch(/suite|dataset|evaluation|api/i);
  });

  test('keeps invalid suite metadata disabled, focuses the first invalid control, and avoids compact overflow', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await selectWorkspace(page, workspace, 'suites');
    await expect(workspace.getByRole('button', { name: 'Compose immutable suite' })).toBeDisabled();
    await workspace.getByRole('button', { name: 'Check suite readiness' }).click();
    await expect(workspace.getByLabel('Suite dataset version')).toBeFocused();
    await workspace.getByLabel('Suite dataset version').selectOption(`${datasetId}|1.0.0`);
    await workspace.getByLabel('Suite evaluator version').selectOption(`${evaluatorId}|1.0.0`);
    await workspace.getByLabel('Suite version').fill('not-semver');
    await workspace.getByLabel('Suite image digest').fill('not-a-digest');
    await workspace.getByLabel('Suite case budget').fill('0');
    await workspace.getByRole('button', { name: 'Check suite readiness' }).click();
    await expect(workspace.getByRole('button', { name: 'Compose immutable suite' })).toBeDisabled();
    await expect(workspace.getByLabel('Suite version')).toBeFocused();
    await expect(workspace.getByRole('status').filter({ hasText: /Suite is not ready/ })).toBeVisible();
    await page.setViewportSize({ width: 320, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(2);
    const suiteFormBounds = await workspace.locator('.suite-form').evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: rect.width };
    });
    expect(suiteFormBounds.left).toBeGreaterThanOrEqual(0);
    expect(suiteFormBounds.right).toBeLessThanOrEqual(320);
    expect(suiteFormBounds.width).toBeGreaterThan(0);
    expect(await workspace.getByRole('button', { name: 'Check suite readiness' }).evaluate((element) => { const rect = element.getBoundingClientRect(); return { width: rect.width, height: rect.height, reachable: rect.width > 0 && rect.height > 0 }; })).toMatchObject({ reachable: true });
    expect(fixture.commandRequests.filter((request) => request.path.endsWith('/suites'))).toHaveLength(0);
  });

  test('cancels a running job with exact confirmation and stable idempotency', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await workspace.getByRole('button', { name: 'Inspect cases' }).click();
    await expect(workspace.getByRole('row', { name: new RegExp(caseId) })).toBeVisible();
    await selectWorkspace(page, workspace, 'suites');
    await selectVersion(workspace);
    await selectWorkspace(page, workspace, 'evaluators');
    await selectVersion(workspace);
    await selectWorkspace(page, workspace, 'jobs');
    for (const [label, value] of [['Evaluation model ID', modelBinding.modelId], ['Evaluation model version', modelBinding.modelVersion], ['Evaluation model digest', modelBinding.modelDigest], ['Evaluation deployment revision', 'deploy-fixture-1'], ['Evaluation reproducibility seed', '7'], ['Evaluation fixture revision', 'fixture-1'], ['Evaluation prompt revision', 'prompt-1'], ['Evaluation policy revision', 'policy-1']] as const) await workspace.getByLabel(label).fill(value);
    await workspace.getByRole('button', { name: 'Bind selected metadata' }).click();
    await workspace.getByRole('button', { name: 'Submit evaluation' }).click();
    await expect(workspace.getByRole('button', { name: 'Cancel' })).toBeVisible();
    page.once('dialog', (dialog) => void dialog.accept());
    await workspace.getByRole('button', { name: 'Cancel' }).click();
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith(`/jobs/${jobId}/cancel`)).length).toBe(1);
    const cancellation = fixture.commandRequests.find((request) => request.path.endsWith(`/jobs/${jobId}/cancel`));
    expect(cancellation?.headers['x-observatory-confirmation']).toBe('cancel-job');
    expect(cancellation?.body).toEqual({ action: 'cancel', reason: 'OWNER_REQUESTED' });
    await expect(workspace.getByRole('row', { name: new RegExp(`${jobId}.*cancelled`, 'i') })).toBeVisible();
  });

  test('records human review and qualification lifecycle without exposing content', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await workspace.getByRole('button', { name: 'Inspect cases' }).click();
    await expect(workspace.getByRole('row', { name: new RegExp(caseId) })).toBeVisible();
    await selectWorkspace(page, workspace, 'suites');
    await selectVersion(workspace);
    await selectWorkspace(page, workspace, 'evaluators');
    await selectVersion(workspace);
    await selectWorkspace(page, workspace, 'results');
    await workspace.getByRole('button', { name: new RegExp(resultId) }).click();
    await expect(workspace.getByRole('heading', { name: 'Scores for selected result' })).toBeVisible();
    await selectWorkspace(page, workspace, 'reviews');
    await expect(workspace.getByRole('button', { name: 'Record review' })).toBeVisible();
    await workspace.getByLabel('Review score selection').selectOption(scoreId);
    await workspace.getByLabel('Review calibration selection').selectOption(calibrationId);
    await workspace.getByRole('spinbutton', { name: 'Review score' }).fill('1');
    await workspace.getByLabel('Review authorization record ID').fill(authRecordId);
    await workspace.getByRole('button', { name: 'Record review' }).click();
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/reviews')).length).toBe(1);
    await selectWorkspace(page, workspace, 'qualification');
    await workspace.getByLabel('Qualification calibration selection').selectOption(calibrationId);
    await workspace.getByLabel('Qualification prompt artifact selection').selectOption('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac');
    await workspace.getByLabel('Qualification rubric artifact selection').selectOption('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad');
    await workspace.getByLabel('Qualification authorization record ID').fill(authRecordId);
    await workspace.getByLabel('Calibration result ID').fill(resultId);
    await workspace.getByLabel('Judge definition ID').fill(judgeDefinitionId);
    await workspace.getByLabel('Judge definition version').fill('1.0.0');
    await workspace.getByLabel('Local model ID').fill(modelBinding.modelId);
    await workspace.getByLabel('Local model version').fill(modelBinding.modelVersion);
    await workspace.getByLabel('Local model digest').fill(modelBinding.modelDigest);
    let qualificationConfirmation = '';
    page.once('dialog', async (dialog) => { qualificationConfirmation = dialog.message(); await dialog.accept(); });
    await workspace.getByRole('button', { name: 'Qualify local judge' }).click();
    expect(qualificationConfirmation).toContain(`judge ${judgeDefinitionId}@1.0.0`);
    expect(qualificationConfirmation).toContain(`local model ${modelBinding.modelId}@${modelBinding.modelVersion}`);
    expect(qualificationConfirmation).toContain(`calibration result ${resultId}`);
    expect(qualificationConfirmation).toContain(`prompt aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac`);
    expect(qualificationConfirmation).toContain(`rubric aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad`);
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/qualifications')).length).toBe(1);
    page.once('dialog', (dialog) => void dialog.accept());
    await workspace.getByRole('button', { name: 'Revoke' }).click();
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/revoke')).length).toBe(1);
    expect(fixture.commandRequests.find((request) => request.path.endsWith('/revoke'))?.headers['x-observatory-confirmation']).toBe('revoke-qualification');
    expect(await page.locator('body').textContent()).not.toMatch(/prompt content|rubric content|secret|credential/i);
  });

  test('loads the next cursor page and exercises every deletion scope with duplicate-safe commands', async ({ page }) => {
    const fixture = await installEvaluationFixture(page);
    const workspace = await openEvaluationWorkspace(page);
    await expect(workspace.getByRole('button', { name: 'Load more' })).toBeVisible();
    await workspace.getByRole('button', { name: 'Load more' }).click();
    await expect(workspace.getByRole('row', { name: /aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaf/ })).toBeVisible();
    page.on('dialog', (dialog) => void dialog.accept());
    await workspace.getByRole('button', { name: 'Delete' }).first().click();
    await selectWorkspace(page, workspace, 'suites');
    await workspace.getByRole('button', { name: 'Delete' }).first().click();
    await selectWorkspace(page, workspace, 'jobs');
    await workspace.getByRole('button', { name: 'Delete' }).first().click();
    await selectWorkspace(page, workspace, 'retention');
    await workspace.getByRole('button', { name: 'Purge all evaluation data' }).click();
    await expect.poll(() => fixture.deletionRequests.length).toBe(4);
    expect(fixture.deletionRequests.map((request) => request.body.kind)).toEqual(['dataset', 'suite', 'job', 'purge_all']);
    expect(new Set(fixture.deletionRequests.map((request) => request.headers['idempotency-key'])).size).toBe(4);
    expect(fixture.deletionRequests.every((request) => request.headers['x-observatory-confirmation'] === 'DELETE_EVALUATION_ARTIFACTS')).toBe(true);
  });

  test('recovers from an initial signed-out session without exposing evaluation routes', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { anonymous: true });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in to Observatory' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Sign in as owner' })).toHaveAttribute('href', /auth\/login/);
    fixture.restoreSession();
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Operational metadata' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Evaluations', exact: true })).toBeVisible();
  });

  test('reconnects evaluation SSE with Last-Event-ID and renders disconnected recovery', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { evaluationSse: 'reconnect' });
    const workspace = await openEvaluationWorkspace(page);
    await expect.poll(() => fixture.getEvaluationEventConnections()).toBeGreaterThan(1);
    await expect(workspace.locator('.live-banner')).toContainText(/stale|disconnected/i);
    await expect(workspace.getByRole('row', { name: new RegExp(datasetId) })).toBeVisible();
  });

  test('maps provider-unavailable job admission to bounded recovery copy', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { providerUnavailable: true });
    const workspace = await openEvaluationWorkspace(page);
    await prepareAuthoritativeJob(page, workspace);
    await workspace.getByRole('button', { name: 'Submit evaluation' }).click();
    await expect(workspace.locator('.live-banner')).toContainText('local judge provider is unavailable');
    expect(fixture.commandRequests.filter((request) => request.path.endsWith('/jobs')).length).toBe(1);
  });

  test('prevents duplicate job admission while the first request is uncertain', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { deferJobSubmit: true });
    const workspace = await openEvaluationWorkspace(page);
    await prepareAuthoritativeJob(page, workspace);
    const submit = workspace.getByRole('button', { name: 'Submit evaluation' });
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect.poll(() => fixture.commandRequests.filter((request) => request.path.endsWith('/jobs')).length).toBe(1);
    fixture.releaseJobSubmit();
    await expect(workspace.getByRole('row', { name: new RegExp(`${jobId}.*running`, 'i') })).toBeVisible();
  });

  test('clears deferred evaluation responses after sign-out', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { deferResult: true });
    const workspace = await openEvaluationWorkspace(page);
    await selectWorkspace(page, workspace, 'results');
    await workspace.getByRole('button', { name: new RegExp(resultId) }).click();
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'You are signed out of Observatory' })).toBeVisible();
    fixture.releaseResult();
    await expect(page.locator('body')).not.toContainText('Scores for selected result');
  });

  test('shows sanitized failed-job recovery without exposing causes', async ({ page }) => {
    await installEvaluationFixture(page, { jobStatus: 'failed' });
    const workspace = await openEvaluationWorkspace(page);
    await selectWorkspace(page, workspace, 'jobs');
    await expect(workspace.getByText('Cause: Runner failed')).toBeVisible();
    await expect(workspace.getByRole('button', { name: 'Inspect' })).toBeVisible();
    await workspace.getByRole('button', { name: 'Prepare new evaluation' }).click();
    await expect(workspace.getByText(/Prepared a new evaluation from job/i)).toBeVisible();
    expect(await page.locator('body').textContent()).not.toMatch(/stack trace|secret|credential|prompt content/i);
  });

  test('keeps storage 503 generic instead of labeling it a judge outage', async ({ page }) => {
    await installEvaluationFixture(page, { providerUnavailable: false });
    const workspace = await openEvaluationWorkspace(page);
    await expect(workspace.getByRole('heading', { name: 'Datasets and cases', exact: true })).toBeVisible();
    expect(await workspace.locator('.live-banner').textContent()).not.toMatch(/local judge provider/i);
  });

  test('distinguishes rejected scope recovery from a fulfilled partial snapshot', async ({ page }) => {
    const fixture = await installEvaluationFixture(page, { rejectDatasets: true });
    await page.goto('/');
    await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
    const workspace = page.locator('observatory-evaluation-workspace');
    await expect(workspace.locator('.live-banner')).toContainText(/rejected/i);
    await expect(workspace.getByRole('button', { name: 'Retry datasets' })).toBeVisible();
    await expect(workspace.locator('.live-banner')).not.toContainText(/partial snapshot/i);
    const initialDatasetGets = fixture.evaluationGetRequests.filter((path) => path.endsWith('/datasets')).length;
    const initialSuiteGets = fixture.evaluationGetRequests.filter((path) => path.endsWith('/suites')).length;
    await workspace.getByRole('button', { name: 'Retry datasets' }).click();
    await expect.poll(() => fixture.evaluationGetRequests.filter((path) => path.endsWith('/datasets')).length).toBe(initialDatasetGets + 1);
    expect(fixture.evaluationGetRequests.filter((path) => path.endsWith('/suites')).length).toBe(initialSuiteGets);
  });
});
