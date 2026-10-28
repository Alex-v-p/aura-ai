import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = '2099-10-28T09:00:00.000Z';
const workflowId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const revisionId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const instanceId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const streamEventId = '11111111-1111-4111-8111-111111111111';
const streamEventId2 = '22222222-2222-4222-8222-222222222222';
const streamEventId3 = '33333333-3333-4333-8333-333333333333';

type FixtureState = {
  readonly workflow: Record<string, unknown>;
  readonly revision: Record<string, unknown>;
  instance: Record<string, unknown>;
  draft: Record<string, unknown> | null;
  listRequests: number;
  streamRequests: number;
  streamLastEventIds: string[];
  createRequests: number;
  createKeys: string[];
  createMode: 'success' | 'unknown-once' | 'unavailable';
  deletionScopes: string[];
  mutationRequests: Array<{ readonly pathname: string; readonly body: unknown; readonly idempotencyKey: string }>;
  startRequests: Array<{ readonly body: unknown; readonly idempotencyKey: string }>;
  cursorExpired: boolean;
  cursorStatuses: number[];
  streamMode: 'normal' | 'invalid-eof' | 'overflow' | 'heartbeat' | 'coalesced' | 'unrelated' | 'workflow-only' | 'eof-in-flight';
  streamStatuses: number[];
  listFailureOnce: boolean;
  listFailureConsumed: boolean;
  startMode: 'success' | 'unknown-once';
  draftUpdateRequests: Array<{ readonly pathname: string; readonly body: unknown; readonly idempotencyKey: string }>;
  listFailureAfter: number | null;
  listFailureStatus: number;
  publishDelayMs: number;
  listDelayAfter: number | null;
  listDelayMs: number;
};

const step = (ordinal: number, kind: 'agent_run' | 'owner_approval' | 'owner_notification' | 'wait_until') => ({
  ordinal,
  kind,
  stepExecutionId: `execution-${ordinal}`,
  status: ordinal === 1 ? 'running' : 'pending',
  effectOutcome: 'not_attempted',
  recoveryCapabilities: ordinal === 1 ? ['cancel'] : [],
  replayUsed: false,
  result: null,
  startedAt: ordinal === 1 ? now : null,
  settledAt: null,
});

const instance = (status: 'running' | 'waiting_for_approval' | 'terminal' = 'running') => ({
  id: instanceId,
  workflowId,
  revisionId,
  input: { owner_note: 'private owner input' },
  status,
  terminalOutcome: status === 'terminal' ? 'completed' : null,
  currentStepOrdinal: status === 'terminal' ? null : 1,
  steps: [step(1, 'agent_run'), step(2, 'owner_approval'), step(3, 'owner_notification'), step(4, 'wait_until')],
  completedEffects: [],
  checkpoints: [{ id: 'checkpoint-e2e', stepOrdinal: 1, status: 'current', version: 1, createdAt: now }],
  result: null,
  version: status === 'terminal' ? 3 : 1,
  admittedAt: now,
  deadlineAt: '2099-11-28T09:00:00.000Z',
  updatedAt: now,
  terminalAt: status === 'terminal' ? now : null,
});

function workflowFixture(): FixtureState {
  return {
    workflow: { id: workflowId, displayName: 'Prepare owner update', status: 'active', latestRevisionId: revisionId, version: 3, createdAt: now, updatedAt: now, retiredAt: null },
    revision: { id: revisionId, workflowId, revision: 1, displayName: 'Prepare owner update', inputFields: [{ name: 'owner_note', valueType: 'bounded_text' }], steps: [{ kind: 'owner_approval' }], limits: { maxSteps: 8, maxAgentRuns: 1, agentRunDeadlineSeconds: 300, approvalLifetimeSeconds: 604800, waitHorizonSeconds: 2592000, instanceLifetimeSeconds: 2592000, maxDefinitionBytes: 65536, maxInstanceInputBytes: 32768, maxStepRecordBytes: 32768, maxCombinedValueBytes: 65536, maxCheckpointBytes: 8192, maxCheckpoints: 9, toolsEnabled: false, memoryEnabled: false, delegationEnabled: false, agentRunReplayEnabled: false }, status: 'published', version: 1, publishedAt: now, retiredAt: null },
    instance: instance(),
    draft: null,
    listRequests: 0,
    streamRequests: 0,
    streamLastEventIds: [],
    createRequests: 0,
    createKeys: [],
    createMode: 'success',
    deletionScopes: [],
    mutationRequests: [],
    startRequests: [],
    cursorExpired: false,
  cursorStatuses: [],
    streamMode: 'normal',
    streamStatuses: [],
    listFailureOnce: false,
    listFailureConsumed: false,
    startMode: 'success',
    draftUpdateRequests: [],
    listFailureAfter: null,
    listFailureStatus: 503,
    publishDelayMs: 0,
    listDelayAfter: null,
    listDelayMs: 0,
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', json: body });
}

async function installFixtures(page: Page, state = workflowFixture()): Promise<FixtureState> {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (url.pathname.endsWith('/auth/session')) {
      return json(route, { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-28T10:00:00Z', absoluteExpiresAt: '2099-10-29T09:00:00Z' });
    }
    if (url.pathname.endsWith('/workflows/stream')) {
      state.streamRequests += 1;
      state.streamLastEventIds.push(request.headers()['last-event-id'] ?? '');
      const streamStatus = state.streamStatuses[state.streamRequests - 1];
      if (streamStatus) return json(route, { detail: streamStatus === 401 ? 'session expired' : 'permission denied' }, streamStatus);
      if (state.cursorExpired) return json(route, { detail: 'cursor expired' }, 410);
      const cursorStatus = state.cursorStatuses[state.streamRequests - 2];
      if (cursorStatus) return json(route, { detail: 'cursor expired' }, cursorStatus);
      if (state.streamMode === 'overflow') return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `data: ${'x'.repeat(65_537)}\n\n` });
      const event = { schemaVersion: 1, eventId: streamEventId, eventType: 'workflow.instance.status_changed', workflowId, revisionId, instanceId, instanceStatus: 'waiting_for_approval', terminalOutcome: null, currentStepOrdinal: 2, version: 2, occurredAt: now };
      if (state.streamMode === 'heartbeat') {
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `event: heartbeat\ndata: ${JSON.stringify({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: now })}\n\nevent: heartbeat\ndata: ${JSON.stringify({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: now, privateContent: 'reject' })}\n\n` });
      }
      if (state.streamMode === 'coalesced') {
        const stepEvent = { schemaVersion: 1, eventId: streamEventId2, eventType: 'workflow.step.status_changed', workflowId, revisionId, instanceId, stepExecutionId: '33333333-3333-4333-8333-333333333333', stepOrdinal: 1, stepKind: 'agent_run', stepStatus: 'completed', effectOutcome: 'completed', recoveryCapabilities: [], instanceVersion: 2, occurredAt: now };
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `event: heartbeat\ndata: ${JSON.stringify({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: now })}\n\nid: ${streamEventId}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(event)}\n\nid: ${streamEventId2}\nevent: workflow.step.status_changed\ndata: ${JSON.stringify(stepEvent)}\n\n` });
      }
      if (state.streamMode === 'workflow-only') {
        const workflowEvent = { schemaVersion: 1, eventId: streamEventId3, eventType: 'workflow.revision.published', workflowId, revisionId, version: 2, occurredAt: now };
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: ${streamEventId3}\nevent: workflow.revision.published\ndata: ${JSON.stringify(workflowEvent)}\n\n` });
      }
      if (state.streamMode === 'eof-in-flight') {
        if (state.streamRequests === 1) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: ${streamEventId}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(event)}\n\n` });
        if (state.streamRequests === 2) {
          await new Promise((resolve) => setTimeout(resolve, 2_000));
          return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `event: heartbeat\ndata: ${JSON.stringify({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: now })}\n\n` });
        }
      }
      if (state.streamMode === 'unrelated') {
        const unrelated = { ...event, eventId: streamEventId2, workflowId: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', revisionId: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', instanceId: 'ffffffff-ffff-4fff-8fff-ffffffffffff' };
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: ${streamEventId2}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(unrelated)}\n\n` });
      }
      if (state.streamMode === 'invalid-eof') {
        const invalid = { ...event, privateContent: 'must be rejected' };
        const followUp = { ...event, eventId: streamEventId2, instanceStatus: 'running', version: 3 };
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: forged\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(invalid)}\n\nid: ${streamEventId}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(event)}\n\nid: ${streamEventId2}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(followUp)}\n\n` });
      }
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: ${streamEventId}\nevent: workflow.instance.status_changed\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (url.pathname === '/api/v1/workflows' && method === 'GET') {
      state.listRequests += 1;
      if (state.listFailureOnce && !state.listFailureConsumed && state.listRequests > 1) { state.listFailureConsumed = true; return json(route, { detail: 'authoritative refresh unavailable' }, state.listFailureStatus); }
      if (state.listFailureAfter !== null && state.listRequests >= state.listFailureAfter) return json(route, { detail: 'authoritative refresh unavailable' }, state.listFailureStatus);
      if (state.listDelayAfter !== null && state.listRequests >= state.listDelayAfter && state.listDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, state.listDelayMs));
      return json(route, { items: [state.workflow], nextCursor: null });
    }
    if (url.pathname === '/api/v1/workflows' && method === 'POST') {
      state.createRequests += 1;
      state.createKeys.push(request.headers()['idempotency-key'] ?? '');
      if (state.createMode === 'unknown-once' && state.createRequests === 1) return route.abort('failed');
      if (state.createMode === 'unavailable') return json(route, { detail: 'Workflow admission is unavailable.' }, 503);
      const body = request.postDataJSON() as { readonly displayName?: string; readonly inputFields?: unknown[]; readonly steps?: unknown[] };
      state.draft = { id: 'draft-e2e', workflowId, displayName: body.displayName ?? 'New workflow', inputFields: body.inputFields ?? [], steps: body.steps ?? [], version: 1, createdAt: now, updatedAt: now };
      return json(route, state.draft);
    }
    if (url.pathname === `/api/v1/workflows/${workflowId}/instances` && method === 'POST') {
      state.startRequests.push({ body: request.postDataJSON(), idempotencyKey: request.headers()['idempotency-key'] ?? '' });
      if (state.startMode === 'unknown-once' && state.startRequests.length === 1) return route.abort('failed');
      return json(route, { ...state.instance, status: 'running', terminalOutcome: null, version: 2 });
    }
    if (url.pathname === `/api/v1/workflows/${workflowId}` && method === 'GET') return json(route, state.workflow);
    if (url.pathname === `/api/v1/workflows/${workflowId}/revisions/${revisionId}` && method === 'GET') return json(route, state.revision);
    if (url.pathname === `/api/v1/workflows/${workflowId}/instances` && method === 'GET') return json(route, { items: [state.instance], nextCursor: null });
    if (url.pathname === `/api/v1/workflows/${workflowId}/instances/${instanceId}` && method === 'GET') return json(route, state.instance);
    if (method === 'POST') state.mutationRequests.push({ pathname: url.pathname, body: request.postDataJSON(), idempotencyKey: request.headers()['idempotency-key'] ?? '' });
    if (url.pathname.endsWith('/drafts/draft-e2e') && method === 'PATCH') state.draftUpdateRequests.push({ pathname: url.pathname, body: request.postDataJSON(), idempotencyKey: request.headers()['idempotency-key'] ?? '' });
    if (url.pathname === `/api/v1/workflows/${workflowId}/drafts/draft-e2e` && method === 'GET') return json(route, state.draft ?? { id: 'draft-e2e', workflowId, displayName: 'New workflow', inputFields: [], steps: [], version: 1, createdAt: now, updatedAt: now });
    if (url.pathname.includes('/deletions') || url.pathname.includes('/history-deletions') || url.pathname.includes('/result-deletions') || url.pathname.includes('/checkpoint-deletions')) {
      const body = request.postDataJSON() as { readonly scope?: string };
      state.deletionScopes.push(body.scope ?? 'unknown');
      return json(route, { scope: body.scope, deleted: true, tombstoneRetained: true, deletedAt: now });
    }
    if (url.pathname.includes(`/revisions/${revisionId}/`) && url.pathname.endsWith('/retire') && method === 'POST') {
      Object.assign(state.revision, { status: 'retired', retiredAt: now, version: 2 });
      return json(route, state.revision);
    }
    if (url.pathname.endsWith('/retire') && method === 'POST') return json(route, { ...state.workflow, status: 'retired', retiredAt: now, version: 4 });
    if (url.pathname.endsWith('/publish') && method === 'POST') {
      if (state.publishDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, state.publishDelayMs));
      return json(route, state.revision);
    }
    if (url.pathname.endsWith('/cancel') && method === 'POST') {
      state.instance = { ...instance('terminal'), terminalOutcome: 'canceled' };
      return json(route, state.instance);
    }
    if (url.pathname.endsWith('/resolve') && method === 'POST') return json(route, instance('waiting_for_approval'));
    if (url.pathname.endsWith('/validate') && method === 'POST') return json(route, { valid: true, issues: [], validatedVersion: 1 });
    if (url.pathname.endsWith('/drafts/draft-e2e') && method === 'PATCH') return json(route, state.draft);

    // The shell asks for these resources while the workflow route is loading.
    if (url.pathname.endsWith('/models')) return json(route, { models: [], defaultModelId: null, observedAt: now });
    if (url.pathname.endsWith('/agents')) return json(route, { items: [{ id: 'agent-e2e', status: 'active', currentRevision: { id: 'agent-revision-e2e', displayName: 'Owner update agent', revision: 3 } }], nextCursor: null });
    if (url.pathname.endsWith('/personas') || url.pathname.endsWith('/conversations')) return json(route, { items: [], nextCursor: null });
    return json(route, { detail: 'fixture endpoint not implemented' }, 404);
  });
  return state;
}

async function openNewWorkflow(page: Page): Promise<void> {
  await page.goto('/workflows/new');
  await expect(page.getByRole('heading', { name: 'Workflow studio', exact: true })).toBeVisible();
  await page.locator('header.page-header').getByRole('button', { name: 'New workflow', exact: true }).click();
  await expect(page.getByLabel('Workflow name', { exact: true })).toBeVisible();
}

async function fillMinimumDraft(page: Page): Promise<void> {
  await page.getByLabel('Workflow name', { exact: true }).fill('Prepare owner update');
  await page.getByRole('button', { name: 'Owner approval', exact: true }).click();
}

test.describe('workflow studio authoring and privacy', () => {
  test('authors all four step kinds, enforces eight ordered steps, revalidates mappings, and stays accessible', async ({ page }) => {
    const state = await installFixtures(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openNewWorkflow(page);
    await expect(page.getByRole('heading', { name: 'Your workflows', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Ordered steps', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Add input', exact: true }).click();
    await page.getByRole('button', { name: 'Add input', exact: true }).click();
    await expect(page.getByLabel('Input 1 name', { exact: true })).toHaveAttribute('autocomplete', 'off');
    await expect(page.getByLabel('Input 1 type', { exact: true })).toHaveAttribute('aria-label', 'Input 1 type');
    await page.getByLabel('Input 2 type', { exact: true }).selectOption('instant');
    await page.getByRole('button', { name: 'Agent run', exact: true }).click();
    await page.getByRole('button', { name: 'Owner approval', exact: true }).click();
    await page.getByRole('button', { name: 'Owner notification', exact: true }).click();
    await page.getByRole('button', { name: 'Wait until', exact: true }).click();
    await expect(page.locator('.step-card')).toHaveCount(4);
    const mobileStepNavigation = page.getByRole('navigation', { name: 'Step editor navigation', exact: true });
    if (await mobileStepNavigation.count()) {
      for (let count = 0; count < 3; count += 1) await mobileStepNavigation.getByRole('button', { name: 'Previous step', exact: true }).click();
    }
    await expect(page.locator('.step-card').first().getByLabel('Authorized agent revision').locator('option')).toHaveText(['Choose an authorized revision', 'Owner update agent · 3']);
    await page.locator('.step-card').first().getByLabel('Authorized agent revision').selectOption('agent-revision-e2e');
    await page.locator('.step-card').first().getByLabel('Instructions').fill('Prepare the bounded owner update.');
    await expect(page.locator('.step-card').first().getByLabel('Input or prior-step mapping').locator('option')).toHaveText(['Choose a mapping', 'input1']);
    await page.locator('.step-card').first().getByLabel('Input or prior-step mapping').selectOption('input1');
    if (await mobileStepNavigation.count()) {
      await mobileStepNavigation.getByRole('button', { name: 'Next step', exact: true }).click();
      await mobileStepNavigation.getByRole('button', { name: 'Next step', exact: true }).click();
      await expect.poll(() => page.evaluate(() => document.activeElement?.closest('[data-step-index]')?.getAttribute('data-step-index'))).toBe('2');
    }
    if ((await page.evaluate(() => window.innerWidth)) <= 960) {
      const workflowList = await page.locator('.workflow-list').first().boundingBox();
      const workspace = await page.locator('.workspace').boundingBox();
      expect(workspace?.y ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(workflowList?.y ?? Number.NEGATIVE_INFINITY);
    }
    expect(await page.locator('.step-card').nth(2).getByLabel('Notification content mapping').locator('option').evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value))).toEqual(['', 'input1', 'step:1:output', 'step:2:decision', 'step:2:status']);
    expect(await page.locator('.step-card').nth(3).getByLabel('Wait-until instant mapping').locator('option').evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value))).toEqual(['', 'input2']);
    const notificationMapping = page.locator('.step-card').nth(2).getByLabel('Notification content mapping');
    const waitMapping = page.locator('.step-card').nth(3).getByLabel('Wait-until instant mapping');
    await notificationMapping.scrollIntoViewIfNeeded();
    await notificationMapping.selectOption('input1');
    if (await mobileStepNavigation.count()) await mobileStepNavigation.getByRole('button', { name: 'Next step', exact: true }).click();
    await waitMapping.scrollIntoViewIfNeeded();
    await waitMapping.selectOption('input2');
    if (await mobileStepNavigation.count()) {
      await mobileStepNavigation.getByRole('button', { name: 'Previous step', exact: true }).click();
      await mobileStepNavigation.getByRole('button', { name: 'Previous step', exact: true }).click();
      await mobileStepNavigation.getByRole('button', { name: 'Previous step', exact: true }).click();
    }

    for (let count = 0; count < 4; count += 1) await page.locator('.step-card').first().getByRole('button', { name: 'Clone', exact: true }).click();
    await expect(page.locator('.step-card')).toHaveCount(8);
    await expect(page.getByText('8 / 8', { exact: true })).toBeVisible();
    await expect(page.locator('.step-card').first().getByRole('button', { name: 'Clone', exact: true })).toBeDisabled();
    if (await mobileStepNavigation.count()) {
      for (let count = 0; count < 7; count += 1) await mobileStepNavigation.getByRole('button', { name: 'Next step', exact: true }).click();
    }
    const dialogs: string[] = [];
    page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.accept(); });
    await page.locator('.step-card').last().getByRole('button', { name: /Remove step 8/ }).click();
    await expect(page.locator('.step-card')).toHaveCount(7);
    if (await mobileStepNavigation.count()) {
      for (let count = 0; count < 6; count += 1) await mobileStepNavigation.getByRole('button', { name: 'Previous step', exact: true }).click();
    }
    await page.getByRole('button', { name: 'Remove input input1', exact: true }).click();
    await expect(page.getByText('Choose an input field that exists.', { exact: true })).toHaveCount(6);
    expect(dialogs).toEqual([expect.stringContaining('Remove step 8')]);

    expect(page.url()).not.toContain('Prepare');
    expect(await page.evaluate(() => Object.keys(localStorage).concat(Object.keys(sessionStorage)).join(' '))).not.toMatch(/workflow|owner update|agent-revision/i);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const results = await new AxeBuilder({ page }).include('.workflow-studio').analyze();
    expect(results.violations).toEqual([]);
    expect(state.streamRequests).toBeGreaterThan(0);
  });

  test('opens an accessible publish review with ordered provenance and bounded limits before publishing', async ({ page }) => {
    const state = await installFixtures(page);
    await openNewWorkflow(page);
    await page.getByLabel('Workflow name', { exact: true }).fill('Prepare owner update');
    await page.getByRole('button', { name: 'Add input', exact: true }).click();
    await page.getByLabel('Input 1 name', { exact: true }).fill('owner_note');
    await page.getByRole('button', { name: 'Agent run', exact: true }).click();
    await page.locator('.step-card').first().getByLabel('Authorized agent revision').selectOption('agent-revision-e2e');
    await page.locator('.step-card').first().getByLabel('Instructions').fill('Prepare the bounded owner update.');
    await page.locator('.step-card').first().getByLabel('Input or prior-step mapping').selectOption('owner_note');
    await page.getByRole('button', { name: 'Owner approval', exact: true }).click();
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByText('Draft definition', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Validate and publish', exact: true }).click();
    const review = page.getByRole('dialog', { name: 'Review before publishing', exact: true });
    await expect(review).toBeVisible();
    await expect(review).toBeFocused();
    await expect(review).toContainText('Publishing creates an immutable revision');
    await expect(review).toContainText('Ordered steps');
    await expect(review).toContainText('owner_note');
    await expect(review).toContainText('Owner update agent · 3');
    await expect(review).toContainText('Instance input “owner_note” (bounded text).');
    await expect(review).toContainText('Owner approval');
    await expect(review).toContainText('Maximum steps: 8');
    const backgroundIsInert = await page.locator('.workflow-studio > :not(.publish-review-backdrop)').evaluateAll((elements) => elements.length > 0 && elements.every((element) => (element as HTMLElement).inert));
    expect(backgroundIsInert).toBe(true);
    const dialogAxe = await new AxeBuilder({ page }).include('#publish-review-dialog').analyze();
    expect(dialogAxe.violations).toEqual([]);
    const closeButton = review.getByRole('button', { name: 'Cancel publish review', exact: true });
    const confirmButton = review.getByRole('button', { name: 'Publish immutable revision', exact: true });
    await page.keyboard.press('Tab');
    await expect(closeButton).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(confirmButton).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(closeButton).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.keyboard.press('Escape');
    await expect(review).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Validate and publish', exact: true })).toBeFocused();
    await page.getByRole('button', { name: 'Validate and publish', exact: true }).click();
    state.publishDelayMs = 1_000;
    await page.getByRole('dialog', { name: 'Review before publishing', exact: true }).getByRole('button', { name: 'Publish immutable revision', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Validate and publish', exact: true })).toBeFocused();
    await expect.poll(() => state.mutationRequests.some((request) => request.pathname.endsWith('/publish'))).toBe(true);
    expect(state.mutationRequests.find((request) => request.pathname.endsWith('/publish'))?.body).toEqual({ expectedVersion: 1 });
  });

  test('preserves an unsaved draft and stable idempotency key through an unknown outcome', async ({ page }) => {
    const state = await installFixtures(page);
    state.createMode = 'unknown-once';
    await openNewWorkflow(page);
    await fillMinimumDraft(page);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('unknown outcome');
    await expect(page.getByLabel('Workflow name', { exact: true })).toHaveValue('Prepare owner update');
    state.streamMode = 'unrelated';
    await expect.poll(() => state.streamRequests, { timeout: 10_000 }).toBeGreaterThan(1);
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect.poll(() => state.createRequests).toBe(2);
    expect(state.createKeys[0]).toBeTruthy();
    expect(state.createKeys[0]).toBe(state.createKeys[1]);
    expect(await page.evaluate(() => document.body.innerText)).not.toContain('private owner input');
    state.listFailureAfter = state.listRequests + 1;
    state.listFailureStatus = 401;
    await page.reload();
    await expect(page.getByRole('alert')).toContainText(/session has expired|authorized/i);
    await expect(page.getByLabel('Workflow name', { exact: true })).toHaveValue('');
    state.listFailureStatus = 403;
    state.listFailureAfter = state.listRequests + 1;
    await page.reload();
    await expect(page.getByRole('alert')).toContainText(/permission|authorized/i);
    await expect(page.getByLabel('Workflow name', { exact: true })).toHaveValue('');
  });

  test('adopts the authoritative parent after create and rebinds draft saves to that parent', async ({ page }) => {
    const state = await installFixtures(page);
    await openNewWorkflow(page);
    await page.getByLabel('Workflow name', { exact: true }).fill('Created owner workflow');
    await page.getByRole('button', { name: 'Owner approval', exact: true }).click();
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect.poll(() => state.createRequests).toBe(1);
    await expect(page.getByText('Draft definition', { exact: true })).toBeVisible();
    await page.getByLabel('Workflow name', { exact: true }).fill('Created owner workflow v2');
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect.poll(() => state.draftUpdateRequests.length).toBe(1);
    expect(state.draftUpdateRequests[0].pathname).toBe(`/api/v1/workflows/${workflowId}/drafts/draft-e2e`);
    expect(state.draftUpdateRequests[0].body).toMatchObject({ expectedVersion: 1, displayName: 'Created owner workflow v2' });
    expect(state.draftUpdateRequests[0].idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  });

  test('keeps stale usable state and reconciles metadata-only SSE through authoritative REST', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'invalid-eof';
    state.listFailureAfter = 3;
    await page.goto('/workflows');
    await expect(page.getByText('Prepare owner update', { exact: true })).toBeVisible();
    await expect.poll(() => state.listRequests).toBeGreaterThanOrEqual(2);
    await expect.poll(() => state.streamRequests).toBeGreaterThan(0);
    expect(state.streamLastEventIds[0]).toBe('');
    await expect(page.getByRole('alert')).toContainText(/unavailable|refresh/i);
    await expect(page.getByText('Prepare owner update', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.body.innerText)).not.toContain('private owner input');
    await expect(page.getByRole('status').filter({ hasText: /Live updates|Reconnecting/ }).first()).toBeVisible();
  });

  test('resumes an EOF retry after a delayed authoritative refresh with the committed cursor', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'eof-in-flight';
    state.listDelayAfter = 2;
    state.listDelayMs = 750;
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    await expect.poll(() => state.listRequests).toBeGreaterThanOrEqual(2);
    await expect.poll(() => state.streamRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
    expect(state.streamLastEventIds[0]).toBe('');
    expect(state.streamLastEventIds[1]).toBe(streamEventId);
    await expect(page.getByRole('status').filter({ hasText: 'Reconnecting' })).toHaveCount(0);
  });

  test('coalesces lifecycle projections and commits the cursor only after REST succeeds', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'coalesced';
    state.listFailureOnce = true;
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    const runHeadings = await page.locator('.workspace h2, .workspace h3').allTextContents();
    expect(runHeadings.indexOf(`Run ${instanceId}`)).toBeGreaterThanOrEqual(0);
    expect(runHeadings.indexOf(`Run ${instanceId}`)).toBeLessThan(runHeadings.indexOf('Step execution details'));
    expect(runHeadings.indexOf('Step execution details')).toBeLessThan(runHeadings.indexOf('Run history and evidence'));
    await expect.poll(() => state.streamRequests).toBeGreaterThan(0);
    await expect.poll(() => state.streamLastEventIds.includes(streamEventId2), { timeout: 10_000 }).toBe(true);
    expect(state.streamLastEventIds[0]).toBe('');
    expect(state.streamLastEventIds[1]).toBe(streamEventId2);
    expect(state.streamLastEventIds).toContain(streamEventId2);
    expect(state.listRequests).toBeLessThanOrEqual(4);
  });

  test('accepts the exact heartbeat envelope while ignoring malformed heartbeat fields', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'heartbeat';
    await page.goto('/workflows');
    await expect.poll(() => state.streamRequests).toBeGreaterThan(0);
    await expect(page.getByRole('status').filter({ hasText: /Live updates|Reconnecting/ }).first()).toBeVisible();
    await expect.poll(() => state.listRequests).toBe(1);
    expect(await page.evaluate(() => document.body.innerText)).not.toContain('privateContent');
  });

  test('sends Last-Event-ID on reconnect and surfaces expired persisted cursors without accepting content', async ({ page }) => {
    const state = await installFixtures(page);
    state.cursorStatuses = [404, 410];
    await page.goto('/workflows');
    await expect.poll(() => state.streamRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(4);
    expect(state.streamLastEventIds.slice(0, 4)).toEqual(['', streamEventId, '', '']);
    expect(state.listRequests).toBeGreaterThan(1);
    expect(await page.evaluate(() => document.body.innerText)).not.toContain('private owner input');
  });

  for (const [status, message] of [[401, /session has expired/i], [403, /permission/i]] as const) {
    test(`clears private state and advances the session epoch on stream ${status}`, async ({ page }) => {
      const state = await installFixtures(page);
      state.streamStatuses = [0, status];
      await page.goto(`/workflows/${workflowId}`);
      await expect(page.getByLabel('owner_note (bounded text)', { exact: true })).toBeVisible();
      await page.getByLabel('owner_note (bounded text)', { exact: true }).fill('must clear after reset');
      await expect.poll(() => state.streamRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
      await expect(page.getByRole('alert')).toContainText(message);
      if (status === 401) {
        await expect(page.getByRole('link', { name: 'Sign in again', exact: true })).toHaveAttribute('href', /\/api\/v1\/auth\/login\?return_to=/);
      } else {
        await expect(page.getByText(/Ask the workspace owner for workflow access/i)).toBeVisible();
        await expect(page.getByRole('button', { name: 'Check permission again', exact: true })).toBeVisible();
      }
      await expect(page.getByRole('heading', { name: 'Choose a workflow', exact: true })).toBeVisible();
      await page.locator('header.page-header').getByRole('button', { name: 'New workflow', exact: true }).click();
      await page.getByRole('button', { name: 'Agent run', exact: true }).click();
      await expect(page.getByLabel('Authorized agent revision').locator('option')).toHaveCount(1);
      expect(await page.locator('input[value="must clear after reset"]').count()).toBe(0);
    });
  }

  test('supports direct run deep links with progress, approval handoff, recovery, cancellation, and terminal result', async ({ page }) => {
    const state = await installFixtures(page);
    state.instance = {
      ...state.instance,
      status: 'waiting_for_approval',
      steps: [
        { ...step(1, 'agent_run'), recoveryCapabilities: ['request_replay'], replayUsed: true, startedAt: '2099-10-28T09:01:00.000Z', settledAt: '2099-10-28T09:02:00.000Z', result: { output: 'bounded result' } },
        { ...step(2, 'owner_approval'), status: 'waiting_for_approval', recoveryCapabilities: ['reconcile'], effectOutcome: 'completed', startedAt: '2099-10-28T09:03:00.000Z', settledAt: null, result: { decision: 'pending' } },
        step(3, 'owner_notification'),
        step(4, 'wait_until'),
      ],
    };
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    await expect(page.getByText('Owner approval', { exact: true })).toBeVisible();
    await expect(page.getByText('Waiting For Approval', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Cancel run', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Request Replay step 1', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Reconcile completed step 2', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Reconcile failed step 2', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Settle Failed step 1', exact: true })).toHaveCount(0);
    await expect(page.getByText('2099-10-28T09:01:00.000Z', { exact: true })).toBeVisible();
    await expect(page.getByText('2099-10-28T09:02:00.000Z', { exact: true })).toBeVisible();
    await expect(page.getByText(/bounded result/)).toBeVisible();
    await expect(page.getByRole('link', { name: /Approvals workspace|Open approvals/i })).toHaveAttribute('href', /approvals/);
    page.once('dialog', async (dialog) => { expect(dialog.message()).toBe(`request replay: Apply request_replay to step execution execution-1 in run ${instanceId} at authoritative version 1?`); await dialog.accept(); });
    await page.getByRole('button', { name: 'Request Replay step 1', exact: true }).click();
    const recovery = state.mutationRequests.find((request) => request.pathname.endsWith('/resolve'));
    expect(recovery?.body).toEqual({ action: 'request_replay', stepExecutionId: 'execution-1', expectedVersion: 1 });
    page.once('dialog', async (dialog) => { expect(dialog.message()).toBe(`Cancel workflow run: Cancel action for run ${instanceId} at authoritative version 1?`); await dialog.accept(); });
    await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
    await expect(page.getByText('Canceled', { exact: true })).toBeVisible();
    expect(state.listRequests).toBeGreaterThan(0);
  });
});

test.describe('workflow studio responsive and lifecycle affordances', () => {
  test('keeps an open run selected for same-workflow definition and revision SSE, then clears it on parent navigation', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'workflow-only';
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    await expect.poll(() => state.streamRequests).toBeGreaterThan(0);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    expect(state.streamLastEventIds).toContain('');
    await page.goto(`/workflows/${workflowId}`);
    await expect(page.getByRole('heading', { name: 'Prepare owner update', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toHaveCount(0);
  });

  test('rebinds route selection from an instance to its parent definition without stale run state', async ({ page }) => {
    await installFixtures(page);
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    await page.goto(`/workflows/${workflowId}`);
    await expect(page.getByRole('heading', { name: 'Prepare owner update', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Runs', exact: true })).toBeVisible();
  });

  test('gates an unknown start until reconciliation, then adopts the authoritative run', async ({ page }) => {
    const state = await installFixtures(page);
    state.startMode = 'unknown-once';
    await page.goto(`/workflows/${workflowId}`);
    await page.getByLabel('owner_note (bounded text)', { exact: true }).fill('start once');
    await page.getByRole('button', { name: 'Start manual run', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText(/unknown outcome|reconcile/i);
    await expect.poll(() => state.startRequests.length).toBe(1);
    await page.getByRole('button', { name: 'Start manual run', exact: true }).click();
    await expect.poll(() => state.startRequests.length).toBe(1);
    state.startMode = 'success';
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Start manual run', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Start manual run', exact: true }).click();
    await expect.poll(() => state.startRequests.length).toBe(2);
    await expect(page).toHaveURL(new RegExp(`/workflows/${workflowId}/instances/${instanceId}$`));
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    expect(state.startRequests[0].idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(state.startRequests[1].idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  });

  test('gates published-revision deletion until retirement and uses exact version confirmations', async ({ page }) => {
    const state = await installFixtures(page);
    await page.goto(`/workflows/${workflowId}`);
    await expect(page.getByRole('button', { name: 'Retire revision', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete published revision', exact: true })).toHaveCount(0);
    const confirmations: string[] = [];
    page.on('dialog', async (dialog) => { confirmations.push(dialog.message()); await dialog.accept(); });
    await page.getByRole('button', { name: 'Retire revision', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Delete published revision', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Delete published revision', exact: true }).click();
    const retirement = state.mutationRequests.find((request) => request.pathname.endsWith(`/revisions/${revisionId}/retire`));
    const deletion = state.mutationRequests.find((request) => request.pathname.endsWith(`/revisions/${revisionId}/deletions`));
    expect(retirement?.body).toEqual({ expectedVersion: 1 });
    expect(deletion?.body).toEqual({ scope: 'published_revision', expectedVersion: 2 });
    expect(confirmations).toEqual(expect.arrayContaining([expect.stringContaining(`Retire revision ${revisionId} at authoritative version 1`), expect.stringContaining(`Delete exact scope published_revision for ${revisionId} v2`)]));
  });

  test('retains a content-free tombstone target after terminal state deletion for remaining scopes', async ({ page }) => {
    const state = await installFixtures(page);
    state.instance = { ...instance('terminal'), result: { summary: 'private result must not be in the tombstone' } };
    await page.goto(`/workflows/${workflowId}/instances/${instanceId}`);
    await expect(page.getByRole('heading', { name: `Run ${instanceId}`, exact: true })).toBeVisible();
    await expect(page.getByText('Terminal result', { exact: true })).toBeVisible();
    await expect(page.getByText('private result must not be in the tombstone', { exact: true })).toBeVisible();
    const confirmations: string[] = [];
    page.on('dialog', async (dialog) => { confirmations.push(dialog.message()); await dialog.accept(); });
    await page.getByRole('button', { name: 'Delete terminal instance state', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Remaining terminal instance deletion scopes', exact: true })).toBeVisible();
    await expect(page.getByText(`State was deleted for instance ${instanceId}`, { exact: false })).toBeVisible();
    for (const label of ['Delete terminal instance history', 'Delete terminal instance result', 'Delete terminal instance checkpoints']) {
      await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
    }
    expect(confirmations[0]).toContain(`Delete exact scope terminal_instance_state for ${instanceId} v3`);
    expect(state.deletionScopes).toContain('terminal_instance_state');
    await page.getByRole('button', { name: 'Delete terminal instance result', exact: true }).click();
    expect(confirmations.at(-1)).toContain(`Delete exact scope terminal_instance_result for ${instanceId} v3`);
    expect(state.deletionScopes).toContain('terminal_instance_result');
  });

  test('keeps mobile and tablet authoring usable at 200% zoom and reduced motion', async ({ page }, testInfo) => {
    await installFixtures(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/workflows/new');
    await page.evaluate(() => document.documentElement.style.zoom = '2');
    await page.locator('header.page-header').getByRole('button', { name: 'New workflow', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Workflow studio', exact: true })).toBeVisible();
    await expect(page.getByLabel('Workflow name', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2)).toBe(true);
    const results = await new AxeBuilder({ page }).include('.workflow-studio').analyze();
    expect(results.violations).toEqual([]);
    testInfo.annotations.push({ type: 'responsive-project', description: testInfo.project.name });
  });

  test('keeps focus inside the active mobile step through navigation, clone, move, and remove', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'mobile', 'Mobile focus contract');
    await installFixtures(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openNewWorkflow(page);
    await page.getByRole('button', { name: 'Owner approval', exact: true }).click();
    await page.getByRole('button', { name: 'Agent run', exact: true }).click();
    await page.getByRole('button', { name: 'Owner notification', exact: true }).click();
    const navigation = page.getByRole('navigation', { name: 'Step editor navigation', exact: true });
    const expectFocusedStep = async (index: number): Promise<void> => {
      await expect.poll(() => page.evaluate(() => document.activeElement?.closest('[data-step-index]')?.getAttribute('data-step-index'))).toBe(String(index));
      const card = page.locator(`[data-step-index="${index}"]`);
      await expect(card).toBeVisible();
      await expect(card).toHaveAttribute('aria-current', 'step');
      await expect(card).toHaveClass(/is-active/);
      await expect(card.locator(':focus')).toBeVisible();
      const visibleIndexes = await page.locator('.step-card').evaluateAll((cards) => cards.filter((candidate) => {
        const style = getComputedStyle(candidate);
        return style.display !== 'none' && style.visibility !== 'hidden';
      }).map((candidate) => candidate.getAttribute('data-step-index')));
      expect(visibleIndexes).toEqual(index === 0 ? ['0'] : [String(index - 1), String(index)]);
    };
    await expectFocusedStep(2);
    await navigation.getByRole('button', { name: 'Previous step', exact: true }).click();
    await expectFocusedStep(1);
    await navigation.getByRole('button', { name: 'Previous step', exact: true }).click();
    await expectFocusedStep(0);
    await navigation.getByRole('button', { name: 'Next step', exact: true }).click();
    await expectFocusedStep(1);
    await navigation.getByRole('button', { name: 'Next step', exact: true }).click();
    await expectFocusedStep(2);
    const finalNext = navigation.getByRole('button', { name: 'Next step', exact: true });
    await expect(finalNext).toHaveAttribute('title', 'Already on the final step');
    await finalNext.click();
    await expect(page.locator('.studio-grid p[role="status"]')).toContainText('Step 3 is the final step.');
    await expectFocusedStep(2);
    await page.locator('[data-step-index="2"]').getByRole('button', { name: 'Clone', exact: true }).click();
    await expect(page.locator('.step-card')).toHaveCount(4);
    await expectFocusedStep(3);
    await page.locator('[data-step-index="3"]').getByRole('button', { name: 'Move step 4 earlier', exact: true }).click();
    await expectFocusedStep(2);
    page.once('dialog', async (dialog) => { await dialog.accept(); });
    await page.locator('[data-step-index="2"]').getByRole('button', { name: 'Remove step 3', exact: true }).click();
    await expect(page.locator('.step-card')).toHaveCount(3);
    await expectFocusedStep(2);
    await expect(page.locator('.studio-grid p[role="status"]')).toContainText('Focus returned to step 3');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const results = await new AxeBuilder({ page }).include('.workflow-studio').analyze();
    expect(results.violations).toEqual([]);
  });

  test('exposes publish/start/retire and every explicit deletion scope with version-aware mutation controls', async ({ page }) => {
    const state = await installFixtures(page);
    await page.goto(`/workflows/${workflowId}`);
    await expect(page.getByRole('heading', { name: 'Prepare owner update', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Edit new draft', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retire definition', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /Publish/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Start manual run|Start workflow/ })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete draft', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Delete retired definition', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Delete terminal instance state', exact: true })).toHaveCount(0);
    await expect(page.getByLabel('owner_note (bounded text)', { exact: true })).toHaveAttribute('required', '');
    await page.getByLabel('owner_note (bounded text)', { exact: true }).fill('bounded owner input');
    await page.getByRole('button', { name: 'Start manual run', exact: true }).click();
    await expect.poll(() => state.startRequests.length).toBe(1);
    expect(state.startRequests[0].body).toEqual({ revisionId, input: { owner_note: 'bounded owner input' } });
    expect(state.startRequests[0].idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
    const dialogs: string[] = [];
    page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.accept(); });
    await page.getByRole('button', { name: 'Retire definition', exact: true }).click();
    await expect(page.getByText('Retired', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete retired definition', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Delete retired definition', exact: true }).click();
    expect(dialogs).toEqual([
      `Retire workflow definition: Retire definition ${workflowId} at authoritative version 3? Published revisions remain immutable.`,
      `Delete retired definition: Delete exact scope retired_definition for ${workflowId} v4. The tombstone is retained; revision history survives.`,
    ]);
    const retirement = state.mutationRequests.find((request) => request.pathname.endsWith('/retire'));
    expect(retirement?.body).toEqual({ expectedVersion: 3 });
    expect(retirement?.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
    const deletion = state.mutationRequests.find((request) => request.pathname.endsWith('/deletions'));
    expect(deletion?.body).toEqual({ scope: 'retired_definition', expectedVersion: 4 });
    expect(deletion?.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  });
});
