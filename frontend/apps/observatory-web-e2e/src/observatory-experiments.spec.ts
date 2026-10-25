import { createServer, type AddressInfo } from 'node:http';
import { expect, test, type Page, type Route } from '@playwright/test';

const observedAt = '2026-10-21T09:00:00.000Z';
const experimentId = '11111111-1111-4111-8111-111111111111';
const baselineId = '22222222-2222-4222-8222-222222222222';
const suiteId = '33333333-3333-4333-8333-333333333333';
const resultId = '44444444-4444-4444-8444-444444444444';
const candidateResultId = '55555555-5555-4555-8555-555555555555';
const datasetId = '66666666-6666-4666-8666-666666666666';
const evaluatorId = '77777777-7777-4777-8777-777777777777';
const caseId = '88888888-8888-4888-8888-888888888888';
const evaluationCatchupEventId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac';

async function startNativeEvaluationSseFixture(): Promise<{
  url: string;
  lastEventIds: string[];
  isCatchupAccepted: () => boolean;
  releaseInitialEvent: () => void;
  close: () => Promise<void>;
}> {
  const lastEventIds: string[] = [];
  let eventDelivered = false;
  let catchupAccepted = false;
  let releaseInitialEvent: (() => void) | null = null;
  const initialEventReleased = new Promise<void>((resolve) => {
    releaseInitialEvent = resolve;
  });
  const server = createServer(async (request, response) => {
    const lastEventId =
      typeof request.headers['last-event-id'] === 'string' ? request.headers['last-event-id'] : '';
    lastEventIds.push(lastEventId);
    const corsOrigin = request.headers.origin ?? '*';
    const headers = {
      'access-control-allow-credentials': 'true',
      'access-control-allow-origin': corsOrigin,
      'cache-control': 'no-store',
      'content-type': 'text/event-stream',
    };
    if (!eventDelivered) {
      await initialEventReleased;
      eventDelivered = true;
      response.writeHead(200, headers);
      response.end(
        `retry: 25\nid: ${evaluationCatchupEventId}\nevent: evaluation.changed\ndata: {"schemaVersion":1,"eventId":"${evaluationCatchupEventId}","eventType":"evaluation.changed","occurredAt":"${observedAt}","resourceType":"evaluation_result","resourceId":"${candidateResultId}","change":"updated","state":"completed","capturePolicy":"metadata_only"}\n\n`,
      );
      return;
    }
    if (lastEventId !== evaluationCatchupEventId) {
      response.writeHead(409, { ...headers, 'content-type': 'application/json' });
      response.end(JSON.stringify({ code: 'catchup_cursor_required' }));
      return;
    }
    catchupAccepted = true;
    response.writeHead(200, headers);
    response.write('retry: 60000\nevent: heartbeat\ndata: {}\n\n');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${address.port}/evaluation-events`,
    lastEventIds,
    isCatchupAccepted: () => catchupAccepted,
    releaseInitialEvent: () => {
      releaseInitialEvent?.();
      releaseInitialEvent = null;
    },
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.closeAllConnections();
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}

async function startIdleEvaluationSseFixture(): Promise<{
  url: string;
  close: () => Promise<void>;
}> {
  const server = createServer((request, response) => {
    const corsOrigin = request.headers.origin ?? '*';
    response.writeHead(200, {
      'access-control-allow-credentials': 'true',
      'access-control-allow-origin': corsOrigin,
      'cache-control': 'no-store',
      'content-type': 'text/event-stream',
    });
    response.write('retry: 60000\nevent: heartbeat\ndata: {}\n\n');
    const keepAlive = setInterval(() => response.write(': heartbeat\n\n'), 10_000);
    response.on('close', () => clearInterval(keepAlive));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${address.port}/evaluation-events`,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.closeAllConnections();
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}
const component = {
  componentId: 'aura.core.api',
  componentVersion: '1.0.0',
  codeRevision: 'code-1',
  imageDigest: 'a'.repeat(64),
  configurationRevision: 'config-1',
};
const reproducibility = {
  seed: 7,
  clockMode: 'fixed',
  timeZone: 'UTC',
  locale: 'en-US',
  fixtureRevision: 'fixture-1',
  promptRevision: 'prompt-1',
  policyRevision: 'policy-1',
};
const evaluator = {
  schemaVersion: 1,
  resourceType: 'evaluator',
  evaluatorId,
  evaluatorVersion: '1.0.0',
  kind: 'deterministic',
  metric: 'quality.pass_rate',
  componentIds: ['aura.core.api'],
  retentionMode: 'manual_only',
  provenance: {
    createdByComponent: 'aura.observatory.evaluator',
    createdByVersion: '1.0.0',
    sourceDigest: 'e'.repeat(64),
  },
  createdAt: observedAt,
};
const suite = {
  schemaVersion: 1,
  resourceType: 'suite',
  suiteId,
  suiteVersion: '1.0.0',
  dataset: { resourceId: datasetId, resourceVersion: '1.0.0' },
  evaluators: [{ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }],
  componentIds: ['aura.core.api'],
  components: [component],
  caseBudget: 10,
  timeoutMs: 30000,
  limits: {
    timeoutMs: 30000,
    maxConcurrency: 1,
    maxAttempts: 1,
    maxCases: 10,
    maxInputBytes: 0,
    maxOutputBytes: 0,
    maxTokens: 0,
  },
  retentionMode: 'manual_only',
  provenance: {
    createdByComponent: 'aura.observatory.suite',
    createdByVersion: '1.0.0',
    sourceDigest: 's'.repeat(64),
  },
  state: 'active',
  createdAt: observedAt,
};
const result = (id: string, modelId: string, deploymentRevision: string) => ({
  schemaVersion: 1,
  resourceType: 'result',
  resultId: id,
  jobId: '99999999-9999-4999-8999-999999999999',
  attemptId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  status: 'completed',
  captureClassification: 'metadata_only',
  scoreCount: 1,
  artifactCount: 0,
  partialEvidence: false,
  reproducible: true,
  provenance: {
    dataset: { resourceId: datasetId, resourceVersion: '1.0.0' },
    suite: { resourceId: suiteId, resourceVersion: '1.0.0' },
    evaluators: [{ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' }],
    components: [component],
    model: { modelId, modelVersion: '1.0.0', modelDigest: 'b'.repeat(64) },
    deploymentRevision,
    reproducibility,
  },
  createdAt: observedAt,
  completedAt: '2026-10-21T09:30:00.000Z',
});
const controlResult = result(resultId, 'control-model', 'deployment-control');
const candidateResult = result(candidateResultId, 'candidate-model', 'deployment-candidate');
const envelope = (items: unknown[] = []) => ({
  items,
  nextCursor: null,
  snapshotAt: observedAt,
  partial: false,
});
const variant = (variantId: string, source: ReturnType<typeof result>) => ({
  variantId,
  components: source.provenance.components,
  model: source.provenance.model,
  configurationRevision: source.provenance.components[0].configurationRevision,
  promptRevision: source.provenance.reproducibility.promptRevision,
  policyRevision: source.provenance.reproducibility.policyRevision,
  deploymentRevision: source.provenance.deploymentRevision,
});
const experiment = {
  schemaVersion: 1,
  resourceType: 'experiment',
  experimentId,
  experimentVersion: '1.0.0',
  state: 'active',
  suite: { resourceId: suiteId, resourceVersion: '1.0.0' },
  variants: [variant('control', controlResult), variant('candidate', candidateResult)],
  cohorts: [{ cohortId: 'cohort-1', caseIds: [caseId] }],
  captureClassification: 'metadata_only',
  retentionMode: 'manual_only',
  provenance: suite.provenance,
  createdAt: observedAt,
};
const threshold = {
  metric: 'quality.pass_rate',
  metricClass: 'quality',
  evaluator: { evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' },
  unit: 'score',
  aggregation: 'mean',
  direction: 'higher_is_better',
  comparisonMode: 'relative',
  thresholdValue: 0.05,
  minimumSampleSize: 1,
  missingEvidencePolicy: 'inconclusive',
};
const baseline = {
  schemaVersion: 1,
  resourceType: 'baseline',
  baselineId,
  baselineVersion: '1.0.0',
  state: 'candidate',
  experiment: { resourceId: experimentId, resourceVersion: '1.0.0' },
  suite: { resourceId: suiteId, resourceVersion: '1.0.0' },
  variantId: 'control',
  cohortIds: ['cohort-1'],
  sourceResultIds: [resultId],
  sourceResultFrom: observedAt,
  sourceResultTo: '2026-10-21T09:30:00.000Z',
  thresholds: [threshold],
  captureClassification: 'metadata_only',
  reproducibility,
  immutable: true,
  promotionMode: 'manual_owner_action',
  manualPromotionRecordId: null,
  promotedAt: null,
  retentionMode: 'manual_only',
  provenance: experiment.provenance,
  createdAt: observedAt,
};
const regression = {
  schemaVersion: 1,
  resourceType: 'regression_decision',
  regressionId: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
  regressionVersion: '1.0.0',
  experiment: { resourceId: experimentId, resourceVersion: '1.0.0' },
  baseline: { resourceId: baselineId, resourceVersion: '1.0.0' },
  variant: variant('candidate', candidateResult),
  cohortIds: ['cohort-1'],
  baselineResultIds: [resultId],
  candidateResultIds: [candidateResultId],
  comparisons: [
    {
      ...threshold,
      baselineValue: 0.94,
      candidateValue: 0.92,
      difference: -0.02,
      relativeChange: -0.02,
      baselineSampleSize: 1,
      candidateSampleSize: 1,
      outcome: 'unchanged',
    },
  ],
  decision: 'unchanged',
  evidenceState: 'complete',
  evidence: [
    { kind: 'result', id: resultId, digest: 'd'.repeat(64), version: '1.0.0' },
    { kind: 'result', id: candidateResultId, digest: 'f'.repeat(64), version: '1.0.0' },
  ],
  attribution: {
    attributionId: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    attributionVersion: '1.0.0',
    regression: { resourceId: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', resourceVersion: '1.0.0' },
    availability: 'available',
    candidates: [
      {
        rank: 1,
        bindingKind: 'deployment',
        componentId: null,
        beforeRevision: 'deployment-control',
        afterRevision: 'deployment-candidate',
        affectedCohortIds: ['cohort-1'],
        unaffectedCohortIds: [],
        evidence: [
          { kind: 'result', id: candidateResultId, digest: 'f'.repeat(64), version: '1.0.0' },
        ],
      },
    ],
    capturePolicy: 'metadata_only',
    createdAt: observedAt,
  },
  capturePolicy: 'metadata_only',
  createdAt: observedAt,
};

async function installFixture(
  page: Page,
  options: { evaluationEventsUrl?: string } = {},
): Promise<{
  promotionRequests: Array<{ headers: Record<string, string>; body: Record<string, unknown> }>;
  experimentRequests: Record<string, unknown>[];
  candidateRequests: Record<string, unknown>[];
  regressionRequests: Record<string, unknown>[];
  expireExperimentSession: () => void;
  triggerIdentityStoreOutage: () => void;
  recoverIdentityStore: () => void;
  getExperimentRequests: () => number;
  completeRetention: () => void;
}> {
  const idleSse = options.evaluationEventsUrl ? null : await startIdleEvaluationSseFixture();
  page.once('close', () => {
    void idleSse?.close();
  });
  const promotionRequests: Array<{
    headers: Record<string, string>;
    body: Record<string, unknown>;
  }> = [];
  const experimentRequests: Record<string, unknown>[] = [];
  const candidateRequests: Record<string, unknown>[] = [];
  const regressionRequests: Record<string, unknown>[] = [];
  let experimentGetRequests = 0;
  let promotionAttempt = 0;
  let baselinePromoted = false;
  let experimentUnauthorized = false;
  let identityStoreUnavailable = false;
  let experimentServiceUnavailable = false;
  let retentionCompleted = false;
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/auth/me')) {
      if (identityStoreUnavailable) {
        await route.fulfill({ status: 503, json: { code: 'identity_unavailable' } });
        return;
      }
      await route.fulfill({
        json: { csrfToken: 'fixture-evaluation-csrf-token-0123456789abcdef' },
      });
      return;
    }
    if (path.endsWith('/evaluation/events')) {
      if (options.evaluationEventsUrl ?? idleSse?.url) {
        await route.continue({ url: options.evaluationEventsUrl ?? idleSse?.url });
        return;
      }
      await route.abort();
      return;
    }
    if (path.endsWith('/events')) {
      await route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
        body: 'retry: 60000\nevent: heartbeat\ndata: {}\n\n',
      });
      return;
    }
    if (path.endsWith('/retention/actions') && request.method() === 'POST') {
      retentionCompleted = true;
      await route.fulfill({
        status: 202,
        json: {
          actionId: '99999999-9999-4999-8999-999999999999',
          action: 'delete_before',
          cutoff: '2026-10-01T00:00:00.000Z',
          status: 'succeeded',
          createdAt: observedAt,
          startedAt: observedAt,
          finishedAt: observedAt,
          deletedObservationCount: 1,
          deletedReceiptCount: 0,
          deletedRunCount: 1,
          failureCode: null,
        },
      });
      return;
    }
    if (path.includes('/retention/actions/')) {
      await route.fulfill({
        json: {
          actionId: '99999999-9999-4999-8999-999999999999',
          action: 'delete_before',
          cutoff: '2026-10-01T00:00:00.000Z',
          status: 'succeeded',
          createdAt: observedAt,
          startedAt: observedAt,
          finishedAt: observedAt,
          deletedObservationCount: 1,
          deletedReceiptCount: 0,
          deletedRunCount: 1,
          failureCode: null,
        },
      });
      return;
    }
    if (path.endsWith('/overview')) {
      await route.fulfill({
        json: {
          observedAt,
          systemState: 'healthy',
          observationCount: 1,
          runCount: 0,
          latestObservationId: null,
          ingest: {
            state: 'healthy',
            counts: { accepted: 1, rejected: 0, duplicate: 0, malformed: 0, storageFailure: 0 },
            lastAcceptedAt: observedAt,
            lastFailureAt: null,
          },
        },
      });
      return;
    }
    if (
      path.endsWith('/health/history') ||
      path.endsWith('/runs') ||
      path.endsWith('/components') ||
      path.endsWith('/metrics')
    ) {
      await route.fulfill({ json: envelope() });
      return;
    }
    if (path.endsWith('/analytics/query')) {
      await route.fulfill({ status: 503, json: { code: 'unavailable' } });
      return;
    }
    if (path.endsWith('/evaluation/suites') && request.method() === 'GET') {
      await route.fulfill({ json: envelope([suite]) });
      return;
    }
    if (path.endsWith('/evaluation/evaluators') && request.method() === 'GET') {
      await route.fulfill({ json: envelope([evaluator]) });
      return;
    }
    if (path.endsWith('/evaluation/results') && request.method() === 'GET') {
      await route.fulfill({ json: envelope([controlResult, candidateResult]) });
      return;
    }
    if (path.includes('/evaluation/datasets/') && path.includes('/cases')) {
      await route.fulfill({
        json: envelope([
          {
            schemaVersion: 1,
            resourceType: 'case',
            caseId,
            caseVersion: '1.0.0',
            dataset: { resourceId: datasetId, resourceVersion: '1.0.0' },
            sourceKind: 'synthetic',
            scopeIds: ['public_project'],
            captureClassification: 'metadata_only',
            artifactIds: [],
            retentionMode: 'manual_only',
            provenance: suite.provenance,
            createdAt: observedAt,
          },
        ]),
      });
      return;
    }
    if (path.endsWith('/deployments')) {
      await route.fulfill({
        json: {
          items: [
            {
              deploymentId: 'deployment-control',
              gitCommit: 'commit-control',
              environment: 'test',
              hostId: 'host-1',
              serviceRoles: ['api'],
              firstSeenAt: observedAt,
              lastSeenAt: observedAt,
            },
            {
              deploymentId: 'deployment-candidate',
              gitCommit: 'commit-candidate',
              environment: 'test',
              hostId: 'host-1',
              serviceRoles: ['api'],
              firstSeenAt: observedAt,
              lastSeenAt: observedAt,
            },
          ],
          snapshotAt: observedAt,
        },
      });
      return;
    }
    if (path.endsWith('/evaluation/experiments') && request.method() === 'GET') {
      experimentGetRequests += 1;
      if (experimentUnauthorized) {
        await route.fulfill({ status: 401, json: { code: 'session_required' } });
        return;
      }
      if (experimentServiceUnavailable) {
        experimentServiceUnavailable = false;
        await route.fulfill({ status: 503, json: { code: 'temporarily_unavailable' } });
        return;
      }
      await route.fulfill({ json: envelope(retentionCompleted ? [] : [experiment]) });
      return;
    }
    if (path.endsWith('/evaluation/baselines') && request.method() === 'GET') {
      await route.fulfill({
        json: envelope([
          {
            ...baseline,
            state: baselinePromoted ? 'promoted' : 'candidate',
            manualPromotionRecordId: baselinePromoted
              ? 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
              : null,
            promotedAt: baselinePromoted ? observedAt : null,
          },
        ]),
      });
      return;
    }
    if (path.endsWith('/evaluation/regressions') && request.method() === 'GET') {
      await route.fulfill({ json: envelope([regression]) });
      return;
    }
    if (path.endsWith('/evaluation/history')) {
      await route.fulfill({
        json: envelope([
          {
            historyId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
            resourceType: 'regression',
            resourceId: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
            resourceVersion: '1.0.0',
            change: 'comparison_completed',
            state: 'inconclusive',
            occurredAt: observedAt,
          },
        ]),
      });
      return;
    }
    if (path.includes('/attribution')) {
      await route.fulfill({ json: regression.attribution });
      return;
    }
    if (path.endsWith('/evaluation/experiments') && request.method() === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>;
      experimentRequests.push(body);
      await route.fulfill({ status: 201, json: { ...experiment, variants: body['variants'] } });
      return;
    }
    if (path.endsWith('/evaluation/baselines') && request.method() === 'POST') {
      candidateRequests.push(request.postDataJSON() as Record<string, unknown>);
      await route.fulfill({ status: 201, json: baseline });
      return;
    }
    if (path.endsWith('/evaluation/regressions') && request.method() === 'POST') {
      regressionRequests.push(request.postDataJSON() as Record<string, unknown>);
      await route.fulfill({ status: 201, json: regression });
      return;
    }
    if (path.includes('/promotion')) {
      promotionAttempt += 1;
      promotionRequests.push({
        headers: request.headers(),
        body: request.postDataJSON() as Record<string, unknown>,
      });
      if (promotionAttempt === 1) {
        await route.abort('timedout');
        return;
      }
      baselinePromoted = true;
      await route.fulfill({
        json: {
          ...baseline,
          state: 'promoted',
          manualPromotionRecordId: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
          promotedAt: observedAt,
        },
      });
      return;
    }
    await route.fulfill({ json: envelope() });
  });
  return {
    promotionRequests,
    experimentRequests,
    candidateRequests,
    regressionRequests,
    expireExperimentSession: () => {
      experimentUnauthorized = true;
    },
    triggerIdentityStoreOutage: () => {
      identityStoreUnavailable = true;
      experimentServiceUnavailable = true;
    },
    recoverIdentityStore: () => {
      identityStoreUnavailable = false;
    },
    getExperimentRequests: () => experimentGetRequests,
    completeRetention: () => {
      retentionCompleted = true;
    },
  };
}

async function openExperiments(page: Page): Promise<void> {
  const menu = page.getByRole('button', { name: 'Open Observatory navigation', exact: true });
  if ((page.viewportSize()?.width ?? 1280) <= 832) {
    await menu.click();
    await page
      .locator('#observatory-sections')
      .getByRole('button', { name: 'Experiments', exact: true })
      .click();
    return;
  }
  await page.getByRole('button', { name: 'Experiments', exact: true }).click();
}

async function openRetention(page: Page): Promise<void> {
  const menu = page.getByRole('button', { name: 'Open Observatory navigation', exact: true });
  if ((page.viewportSize()?.width ?? 1280) <= 832) {
    await menu.click();
    await page
      .locator('#observatory-sections')
      .getByRole('button', { name: 'Retention', exact: true })
      .click();
    return;
  }
  await page.getByRole('button', { name: 'Retention', exact: true }).click();
}

test.describe('Observatory experiments, baselines, and regressions', () => {
  test('supports responsive navigation and typed irreversible promotion safety', async ({
    page,
  }) => {
    const fixture = await installFixture(page);
    await page.goto('/');
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
    await openExperiments(page);
    const workspace = page.locator('observatory-experiment-workspace');
    await expect(
      workspace.getByRole('heading', { name: 'Compare controlled evaluation evidence' }),
    ).toBeVisible();
    await expect(workspace.getByRole('row', { name: new RegExp(baselineId) })).toBeVisible();
    await workspace.getByText('Open experiment authoring', { exact: false }).click();
    const variants = workspace.getByRole('group', { name: /Variants \(2 of 16; minimum 2\)/ });
    await expect(variants).toBeVisible();
    await workspace.getByRole('button', { name: 'Add variant', exact: true }).click();
    await expect(
      workspace.getByRole('group', { name: /Variants \(3 of 16; minimum 2\)/ }),
    ).toBeVisible();
    await expect(
      workspace.getByRole('button', { name: 'Remove variant', exact: true }),
    ).toHaveCount(3);
    await workspace.getByRole('button', { name: 'Remove variant', exact: true }).last().click();
    await expect(
      workspace.getByText('Remove this variant? The selected binding can be restored.'),
    ).toBeVisible();
    await workspace.getByRole('button', { name: 'Confirm removal' }).click();
    await expect(
      workspace.getByRole('group', { name: /Variants \(2 of 16; minimum 2\)/ }),
    ).toBeVisible();
    await workspace.getByLabel('Experiment ID').fill(experimentId);
    await workspace.getByLabel('Persisted suite').selectOption(`${suiteId}@1.0.0`);
    await workspace.getByLabel('Cohort ID').fill('cohort-1');
    await workspace
      .getByRole('group', { name: 'Persisted suite cases' })
      .getByRole('checkbox')
      .check();
    const variantIds = workspace.locator('input[name^="variant-id-"]');
    await variantIds.nth(0).fill('control');
    await variantIds.nth(1).fill('candidate');
    const sourceResults = workspace.locator('select[name^="variant-result-"]');
    await sourceResults.nth(0).selectOption(resultId);
    await sourceResults.nth(1).selectOption(candidateResultId);
    await workspace.getByRole('button', { name: 'Create experiment', exact: true }).click();
    await expect.poll(() => fixture.experimentRequests.length).toBe(1);
    const experimentBody = fixture.experimentRequests[0];
    expect(Array.isArray(experimentBody['variants'])).toBe(true);
    expect((experimentBody['variants'] as unknown[]).length).toBeGreaterThanOrEqual(2);
    expect((experimentBody['variants'] as unknown[]).length).toBeLessThanOrEqual(16);
    expect(experimentBody['captureAuthorization']).toEqual(
      expect.objectContaining({
        captureClassification: 'metadata_only',
        retentionMode: 'manual_only',
      }),
    );
    expect((experimentBody['variants'] as Array<Record<string, unknown>>)[0]['components']).toEqual(
      [component],
    );
    expect(Object.keys(experimentBody).sort()).toEqual(
      [
        'captureAuthorization',
        'cohorts',
        'createdAt',
        'experimentId',
        'experimentVersion',
        'limits',
        'provenance',
        'retentionMode',
        'suite',
        'variants',
      ].sort(),
    );
    const candidatePanel = workspace.locator('section[aria-labelledby="candidate-title"]');
    await candidatePanel.getByText('Open candidate authoring', { exact: false }).click();
    await candidatePanel.getByLabel('Experiment').selectOption(`${experimentId}@1.0.0`);
    await candidatePanel.getByLabel('Baseline variant').selectOption('control');
    await candidatePanel
      .getByRole('group', { name: 'Candidate cohorts' })
      .getByRole('checkbox', { name: /cohort-1/ })
      .check();
    await candidatePanel
      .getByRole('group', { name: 'Candidate source results' })
      .getByRole('checkbox', { name: new RegExp(resultId) })
      .check();
    await candidatePanel.getByLabel('Authoritative evaluator').selectOption(`${evaluatorId}@1.0.0`);
    await candidatePanel.getByLabel('Threshold value').fill('0.05');
    await candidatePanel.getByRole('button', { name: 'Create candidate', exact: true }).click();
    await expect.poll(() => fixture.candidateRequests.length).toBe(1);
    expect(fixture.candidateRequests[0]['sourceResultIds']).toEqual([resultId]);
    expect(fixture.candidateRequests[0]['cohortIds']).toEqual(['cohort-1']);
    expect(
      (fixture.candidateRequests[0]['thresholds'] as Array<Record<string, unknown>>)[0][
        'evaluator'
      ],
    ).toEqual({ evaluatorId, evaluatorVersion: '1.0.0', kind: 'deterministic' });
    expect(fixture.candidateRequests[0]['reproducibility']).toEqual(reproducibility);
    const promote = workspace.getByRole('button', { name: 'Promote', exact: true });
    const baselinesHeading = workspace.getByRole('heading', {
      name: 'Baseline candidates and promoted baselines',
    });
    await promote.click();
    const dialog = page.getByRole('dialog', { name: 'Promote immutable baseline?' });
    await expect(dialog.getByText(/quality\.pass_rate/)).toBeVisible();
    await expect(dialog.getByText(/evaluator .* 1\.0\.0/)).toBeVisible();
    await expect(
      dialog.getByText(
        /relative · higher_is_better · 0\.05 score · minimum 1 · missing inconclusive/,
      ),
    ).toBeVisible();
    await dialog.getByRole('button', { name: 'Close' }).focus();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(promote).toBeFocused();
    await promote.click();
    await expect(dialog.getByRole('button', { name: 'Promote baseline' })).toBeDisabled();
    await dialog.getByLabel(/Type PROMOTE_IMMUTABLE_BASELINE/).fill('PROMOTE_IMMUTABLE_BASELINE');
    await expect(dialog.getByRole('button', { name: 'Promote baseline' })).toBeEnabled();
    await dialog.getByRole('button', { name: 'Close' }).focus();
    await page.keyboard.press('Tab');
    await expect(
      dialog.getByRole('heading', { name: 'Promote immutable baseline?' }),
    ).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused();
    await dialog.getByRole('button', { name: 'Promote baseline' }).click();
    await expect.poll(() => fixture.promotionRequests.length).toBe(1);
    await expect(
      dialog.getByRole('heading', { name: 'Promote immutable baseline?' }),
    ).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Close' })).toBeEnabled();
    await dialog.getByRole('button', { name: 'Close' }).click();
    await expect(dialog).toBeHidden();
    await promote.click();
    await dialog.getByLabel(/Type PROMOTE_IMMUTABLE_BASELINE/).fill('PROMOTE_IMMUTABLE_BASELINE');
    await dialog.getByRole('button', { name: 'Promote baseline' }).click();
    await expect.poll(() => fixture.promotionRequests.length).toBe(2);
    expect(fixture.promotionRequests[1].headers['x-idempotency-key']).toBe(
      fixture.promotionRequests[0].headers['x-idempotency-key'],
    );
    expect(fixture.promotionRequests[1].headers['x-observatory-confirmation']).toBe(
      'PROMOTE_IMMUTABLE_BASELINE',
    );
    expect(fixture.promotionRequests[1].body).toEqual(fixture.promotionRequests[0].body);
    await expect(page.getByText(/now immutable and promoted/)).toBeVisible();
    await expect(baselinesHeading).toBeFocused();
    const comparePanel = workspace.locator('section[aria-labelledby="compare-title"]');
    await comparePanel.getByLabel('Promoted baseline').selectOption(`${baselineId}@1.0.0`);
    await comparePanel.getByLabel('Candidate variant').selectOption('candidate');
    await comparePanel
      .getByRole('group', { name: 'Candidate result IDs' })
      .getByRole('checkbox')
      .check();
    await comparePanel.getByRole('button', { name: 'Compare evidence', exact: true }).click();
    await expect.poll(() => fixture.regressionRequests.length).toBe(1);
    expect(fixture.regressionRequests[0]['variantId']).toBe('candidate');
    expect(fixture.regressionRequests[0]['baselineResultIds']).toEqual([resultId]);
    expect(fixture.regressionRequests[0]['candidateResultIds']).toEqual([candidateResultId]);
    expect(fixture.regressionRequests[0]['cohortIds']).toEqual(['cohort-1']);
    await workspace.getByRole('button', { name: 'Open detail' }).click();
    await expect(workspace.getByRole('heading', { name: /Regression c{8}-/ })).toBeVisible();
    await expect(workspace.getByText(/Exact persisted regression bindings/)).toBeVisible();
    const bindingDefinitions = workspace.getByRole('definition');
    await expect(bindingDefinitions.filter({ hasText: 'aura.core.api 1.0.0' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: 'candidate-model · 1.0.0' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: 'deployment-candidate' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: 'config-1' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: 'policy-1' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: 'prompt-1' })).toBeVisible();
    await expect(bindingDefinitions.filter({ hasText: `${evaluatorId} 1.0.0` })).toBeVisible();
    await workspace.getByRole('button', { name: 'Load exact-version attribution' }).click();
    await expect(workspace.getByRole('heading', { name: 'Attribution evidence' })).toBeVisible();
    await expect(
      workspace.locator('td[data-label="Affected cohorts"]', { hasText: 'cohort-1' }),
    ).toBeVisible();
    await expect(
      workspace.locator('td[data-label="Unaffected cohorts"]', { hasText: 'None identified' }),
    ).toBeVisible();
    await expect(workspace.getByRole('heading', { name: 'Experiment history' })).toBeVisible();
    await expect(
      workspace.locator('td[data-label="Change"]', { hasText: 'comparison_completed' }),
    ).toBeVisible();
  });

  test('propagates an experiment endpoint 401 into shell sign-in recovery', async ({ page }) => {
    const fixture = await installFixture(page);
    await page.goto('/');
    await openExperiments(page);
    const workspace = page.locator('observatory-experiment-workspace');
    await expect(
      workspace.getByRole('heading', { name: 'Compare controlled evaluation evidence' }),
    ).toBeVisible();
    fixture.expireExperimentSession();
    await workspace.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: 'Your Observatory session expired' }),
    ).toBeVisible();
    await expect(page.getByRole('link', { name: 'Sign in as owner' })).toBeVisible();
    await expect(page.locator('body')).not.toContainText(experimentId);
  });

  test('clears experiment selections and refetches after retention completion', async ({
    page,
  }) => {
    const fixture = await installFixture(page);
    await page.goto('/');
    await openExperiments(page);
    const workspace = page.locator('observatory-experiment-workspace');
    await expect(workspace.getByRole('row', { name: new RegExp(experimentId) })).toBeVisible();
    const initialReads = fixture.getExperimentRequests();
    await openRetention(page);
    await page
      .getByRole('textbox', { name: 'Delete observations before (UTC)' })
      .fill('2026-10-01T00:00');
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: 'Confirm destructive retention action' });
    await confirmation.getByRole('button', { name: 'Confirm delete-before', exact: true }).click();
    await expect(
      page
        .locator('observatory-retention .result div')
        .filter({ hasText: /Status.*succeeded/i })
        .getByRole('definition'),
    ).toHaveText('succeeded');
    await openExperiments(page);
    await expect.poll(() => fixture.getExperimentRequests()).toBeGreaterThan(initialReads);
    await expect(page.locator('observatory-experiment-workspace')).not.toContainText(experimentId);
    await expect(
      page.locator('observatory-experiment-workspace').getByRole('status').first(),
    ).toContainText(/No experiment evidence|Live experiment snapshot|Live updates disconnected/);
  });

  test('keeps the workspace usable with metadata-only evaluation SSE and catches up with Last-Event-ID', async ({
    page,
  }) => {
    const nativeSse = await startNativeEvaluationSseFixture();
    try {
      const fixture = await installFixture(page, { evaluationEventsUrl: nativeSse.url });
      await page.goto('/');
      await openExperiments(page);
      const workspace = page.locator('observatory-experiment-workspace');
      await expect(
        workspace.getByRole('heading', { name: 'Compare controlled evaluation evidence' }),
      ).toBeVisible();
      await expect
        .poll(() => fixture.getExperimentRequests(), { timeout: 10000 })
        .toBeGreaterThan(0);
      await expect(workspace.getByRole('row', { name: new RegExp(experimentId) })).toBeVisible();
      nativeSse.releaseInitialEvent();
      // The native EventSource reconnect carries the persisted cursor; the
      // loopback fixture rejects every reconnect until the exact cursor arrives.
      await expect
        .poll(() => nativeSse.lastEventIds, { timeout: 10000 })
        .toContain(evaluationCatchupEventId);
      await expect.poll(() => nativeSse.isCatchupAccepted(), { timeout: 10000 }).toBe(true);
      await expect(workspace.getByRole('status').first()).toContainText(
        /Live experiment snapshot|Live updates disconnected/,
      );
      const renderedExperimentShell = (await page.locator('main').textContent()) ?? '';
      expect(renderedExperimentShell).not.toContain('PRIVATE CONTENT VALUE');
      expect(renderedExperimentShell).not.toContain('PRIVATE_PROMPT_MARKER');
    } finally {
      await nativeSse.close();
    }
  });

  test('keeps the authenticated snapshot during a transient 5xx identity-store outage and recovers', async ({
    page,
  }) => {
    const fixture = await installFixture(page);
    await page.goto('/');
    await openExperiments(page);
    const workspace = page.locator('observatory-experiment-workspace');
    await expect(workspace.getByRole('row', { name: new RegExp(experimentId) })).toBeVisible();

    fixture.triggerIdentityStoreOutage();
    const identityProbeStatus = await page.evaluate(
      async () => (await fetch('/api/v1/auth/me', { credentials: 'same-origin' })).status,
    );
    expect(identityProbeStatus).toBe(503);
    const outageRefreshes = fixture.getExperimentRequests();
    await workspace.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect.poll(() => fixture.getExperimentRequests()).toBeGreaterThan(outageRefreshes);
    await expect(workspace.locator('.state-banner')).toContainText(
      /Partial snapshot|Live updates disconnected|Stale or late evidence/,
    );
    await expect(workspace.getByRole('row', { name: new RegExp(experimentId) })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
    expect(await page.locator('body').textContent()).not.toMatch(
      /session expired|signed out of Observatory/i,
    );

    fixture.recoverIdentityStore();
    const recoveryRefreshes = fixture.getExperimentRequests();
    await workspace.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect.poll(() => fixture.getExperimentRequests()).toBeGreaterThan(recoveryRefreshes);
    await expect(workspace.getByRole('row', { name: new RegExp(experimentId) })).toBeVisible();
  });

  test('keeps the experiment workspace usable at narrow widths and announces empty recovery', async ({
    page,
  }) => {
    await installFixture(page);
    await page.goto('/');
    await openExperiments(page);
    const workspace = page.locator('observatory-experiment-workspace');
    await expect(
      workspace.getByRole('heading', { name: 'Experiments', exact: true }),
    ).toBeVisible();
    await page.keyboard.press('Tab');
    await expect(page.locator(':focus')).toBeVisible();
    await expect(
      workspace.getByText('Protected repository golden results are not changed.'),
    ).toHaveCount(0);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 320, height: 844 });
    const compactLayout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }));
    expect(compactLayout.documentWidth).toBeLessThanOrEqual(compactLayout.viewportWidth + 1);
    await page.setViewportSize({ width: 640, height: 844 });
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
    });
    const layout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
    }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.reduced).toBe(true);
  });
});
