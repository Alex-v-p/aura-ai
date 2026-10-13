import { expect, test, type Locator, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = '2026-10-10T09:00:00.000Z';
type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'canceled';
type ExecutionStatus = 'not_started' | 'queued' | 'executing' | 'completed' | 'failed' | 'denied';
type ApprovalToolId = 'reminder.create' | 'reminder.series.create' | 'reminder.reschedule' | 'reminder.series.update' | 'reminder.series.pause' | 'reminder.series.resume' | 'reminder.cancel';
type ApprovalSummary = { id: string; proposalId: string; runId: string; conversationId: string; toolCallId: string; toolId: ApprovalToolId; toolVersion: 1; status: ApprovalStatus; executionStatus: ExecutionStatus; version: number; createdAt: string; updatedAt: string; expiresAt: string; decidedAt: string | null };
type ApprovalDetail = ApprovalSummary & { proposal: { id: string; content: string; dueAt: string } | { id: string; operation: 'series.create' | 'reschedule' | 'series.update' | 'series.pause' | 'series.resume' | 'cancel'; summary: string; targetType: 'reminder' | 'series' | 'occurrence' | null; targetId: string | null; expectedVersion: number | null } };

const summary = (id: string, status: ApprovalStatus = 'pending', executionStatus: ExecutionStatus = 'not_started'): ApprovalSummary => ({ id, proposalId: `proposal-${id}`, runId: `run-${id}`, conversationId: `conversation-${id}`, toolCallId: `call-${id}`, toolId: 'reminder.create', toolVersion: 1, status, executionStatus, version: status === 'pending' ? 1 : 2, createdAt: now, updatedAt: now, expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: status === 'pending' ? null : '2026-10-10T09:05:00.000Z' });
const pending = { ...summary('approval-pending'), id: '00000000-0000-4000-8000-000000000001', proposalId: '00000000-0000-4000-8000-000000000002', runId: '00000000-0000-4000-8000-000000000003', conversationId: '00000000-0000-4000-8000-000000000004', toolCallId: '00000000-0000-4000-8000-000000000005' };
const pendingB = summary('approval-pending-b');
const commandPending: ApprovalSummary = { ...summary('command-pending'), id: '00000000-0000-4000-8000-000000000011', proposalId: '00000000-0000-4000-8000-000000000012', runId: '00000000-0000-4000-8000-000000000013', conversationId: '00000000-0000-4000-8000-000000000014', toolCallId: '00000000-0000-4000-8000-000000000015', toolId: 'reminder.series.create' };
const approved = summary('approval-approved', 'approved', 'completed');
const rejected = summary('approval-rejected', 'rejected', 'denied');
const historyThree = summary('approval-history-3', 'approved', 'completed');
const cancelOccurrence = { ...summary('cancel-occurrence'), id: '00000000-0000-4000-8000-000000000021', proposalId: '00000000-0000-4000-8000-000000000022', toolId: 'reminder.cancel' as const };
const cancelOccurrenceDetail: ApprovalDetail = { ...cancelOccurrence, proposal: { id: cancelOccurrence.proposalId, operation: 'cancel', summary: 'Cancel the next occurrence of the recurring reminder.', targetType: 'occurrence', targetId: '00000000-0000-4000-8000-000000000023', expectedVersion: 4 } };
const terminalCommand = { ...commandPending, id: '00000000-0000-4000-8000-000000000031', proposalId: '00000000-0000-4000-8000-000000000032', status: 'approved' as const, executionStatus: 'completed' as const, version: 2, decidedAt: now };
const terminalCommandDetail: ApprovalDetail = { ...terminalCommand, proposal: { id: terminalCommand.proposalId, operation: 'series.update', summary: 'Update future occurrences on the recurring reminder.', targetType: 'series', targetId: '00000000-0000-4000-8000-000000000033', expectedVersion: 1 } };
const detailFor = (approval: ApprovalSummary, content = 'Create a reminder to bring the library books back.'): ApprovalDetail => ({ ...approval, proposal: { id: approval.proposalId, content, dueAt: '2026-10-11T16:30:00.000Z' } });
const commandDetail: ApprovalDetail = { ...commandPending, proposal: { id: commandPending.proposalId, operation: 'series.create', summary: 'Create this recurring reminder on the configured schedule.', targetType: 'series', targetId: null, expectedVersion: null } };

type DecisionResponse = { readonly status?: number; readonly abort?: boolean };
type ApprovalFixtureState = {
  pendingRows: ApprovalSummary[];
  historyRows: ApprovalSummary[];
  pendingNextCursor: string | null;
  historyNextCursor: string | null;
  details: Record<string, ApprovalDetail>;
  detailErrors: Record<string, number | undefined>;
  detailGate?: Promise<void>;
  detailRefreshGate?: Promise<void>;
  decisionGate?: Promise<void>;
  decisionResponses: DecisionResponse[];
  decisionCalls: number;
  decisionIdempotencyKeys: string[];
  listRequests: number;
  detailRequests: number;
  pendingListResponses: number[];
  historyListResponses: number[];
  pendingListGate?: Promise<void>;
  historyListGate?: Promise<void>;
  historyPages: Record<string, { readonly items: ApprovalSummary[]; readonly nextCursor: string | null }>;
  sessionStatus: number | null;
  streamMode: 'heartbeat' | 'invalid-valid' | 'pending-refresh' | 'event-reconcile-fail' | 'command' | '410' | 'cap';
  streamEventGate?: Promise<void>;
  streamRequests: number;
  streamHeaders: string[];
};

async function installFixtures(page: Page): Promise<ApprovalFixtureState> {
  const state: ApprovalFixtureState = {
    pendingRows: [pending],
    historyRows: [approved, rejected],
    pendingNextCursor: null,
    historyNextCursor: null,
    details: { [pending.id]: detailFor(pending), [pendingB.id]: detailFor(pendingB, 'Create a second reminder.'), [approved.id]: detailFor(approved, 'The approved reminder is already complete.'), [rejected.id]: detailFor(rejected, 'The rejected reminder was not created.'), [commandPending.id]: commandDetail, [cancelOccurrence.id]: cancelOccurrenceDetail, [terminalCommand.id]: terminalCommandDetail },
    detailErrors: {},
    decisionResponses: [],
    decisionCalls: 0,
    decisionIdempotencyKeys: [],
    listRequests: 0,
    detailRequests: 0,
    pendingListResponses: [],
    historyListResponses: [],
    historyPages: {},
    sessionStatus: null,
    streamMode: 'heartbeat',
    streamRequests: 0,
    streamHeaders: [],
  };

  await page.route('**/api/v1/auth/session', async (route) => state.sessionStatus ? route.fulfill({ status: state.sessionStatus, json: { detail: 'session unavailable' } }) : route.fulfill({ json: { principal: { issuer: 'https://issuer', subject: 'owner' }, csrfToken: 'csrf', idleExpiresAt: now, absoluteExpiresAt: '2026-10-10T12:00:00.000Z' } }));
  await page.route('**/api/v1/approvals**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith('/stream')) {
      state.streamRequests += 1;
      state.streamHeaders.push(request.headers()['last-event-id'] ?? '');
      if (state.streamMode === '410' && state.streamRequests === 1) return route.fulfill({ status: 410, json: { detail: 'cursor expired' } });
      if (state.streamMode === 'cap') return route.fulfill({ status: 503, json: { detail: 'stream unavailable' } });
      if (state.streamEventGate) await state.streamEventGate;
      if (state.streamMode === 'invalid-valid') {
        state.pendingRows = [];
        const validId = '11111111-1111-4111-8111-111111111111';
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `event: approval.pending\ndata: {"schemaVersion":1,"eventId":"not-a-uuid","eventType":"approval.pending","approvalId":"${pending.id}","proposalId":"${pending.proposalId}","runId":"${pending.runId}","conversationId":"${pending.conversationId}","toolId":"reminder.create","toolVersion":1,"status":"pending","executionStatus":"not_started","version":1,"occurredAt":"${now}"}\n\nid: ${validId}\nevent: approval.approved\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: validId, eventType: 'approval.approved', approvalId: pending.id, proposalId: pending.proposalId, runId: pending.runId, conversationId: pending.conversationId, toolId: 'reminder.create', toolVersion: 1, status: 'approved', executionStatus: 'completed', version: 2, occurredAt: now })}\n\n` });
      }
      if (state.streamMode === 'pending-refresh' || state.streamMode === 'event-reconcile-fail') {
        const mode = state.streamMode;
        state.streamMode = 'heartbeat';
        if (mode === 'pending-refresh') state.pendingRows = [];
        const eventId = mode === 'pending-refresh' ? '22222222-2222-4222-8222-222222222222' : '33333333-3333-4333-8333-333333333333';
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: ${eventId}\nevent: approval.approved\ndata: ${JSON.stringify({ schemaVersion: 1, eventId, eventType: 'approval.approved', approvalId: pending.id, proposalId: pending.proposalId, runId: pending.runId, conversationId: pending.conversationId, toolId: 'reminder.create', toolVersion: 1, status: 'approved', executionStatus: 'completed', version: 2, occurredAt: now })}\n\n` });
      }
      if (state.streamMode === 'command') {
        state.streamMode = 'heartbeat';
        state.pendingRows = [commandPending];
        const eventId = '44444444-4444-4444-8444-444444444444';
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: ${eventId}\nevent: approval.pending\ndata: ${JSON.stringify({ schemaVersion: 1, eventId, eventType: 'approval.pending', approvalId: commandPending.id, proposalId: commandPending.proposalId, runId: commandPending.runId, conversationId: commandPending.conversationId, toolId: commandPending.toolId, toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, occurredAt: now })}\n\n` });
      }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': heartbeat\n\n' });
    }
    const detailMatch = url.pathname.match(/\/approvals\/([^/]+)$/);
    if (detailMatch && request.method() === 'GET') {
      state.detailRequests += 1;
      const id = detailMatch[1];
      if (state.detailGate && id === pending.id) await state.detailGate;
      if (state.detailRefreshGate && id === pending.id && state.detailRequests > 1) await state.detailRefreshGate;
      const errorStatus = state.detailErrors[id];
      if (errorStatus) return route.fulfill({ status: errorStatus, json: { detail: 'detail unavailable' } });
      return route.fulfill({ json: state.details[id] ?? detailFor(summary(id)) });
    }
    const decisionMatch = url.pathname.match(/\/approvals\/([^/]+)\/(approve|reject)$/);
    if (decisionMatch && request.method() === 'POST') {
      state.decisionCalls += 1;
      state.decisionIdempotencyKeys.push(request.headers()['idempotency-key'] ?? '');
      if (state.decisionGate) await state.decisionGate;
      const configured = state.decisionResponses.shift();
      if (configured?.abort) return route.abort('failed');
      if (configured?.status) return route.fulfill({ status: configured.status, json: { detail: 'decision unavailable' } });
      const source = state.details[decisionMatch[1]] ?? detailFor(pending);
      const decided = { ...source, status: decisionMatch[2] === 'approve' ? 'approved' as const : 'rejected' as const, executionStatus: decisionMatch[2] === 'approve' ? 'queued' as const : 'denied' as const, version: source.version + 1, decidedAt: now };
      state.pendingRows = state.pendingRows.filter((item) => item.id !== source.id);
      state.historyRows = [decided, ...state.historyRows.filter((item) => item.id !== source.id)];
      state.details[source.id] = decided;
      return route.fulfill({ json: decided });
    }
    if (url.pathname === '/api/v1/approvals' && request.method() === 'GET') {
      state.listRequests += 1;
      const pendingView = url.searchParams.get('status') === 'pending';
      const gate = pendingView ? state.pendingListGate : state.historyListGate;
      if (gate) await gate;
      const errors = pendingView ? state.pendingListResponses : state.historyListResponses;
      const errorStatus = errors.shift();
      if (errorStatus) return route.fulfill({ status: errorStatus, json: { detail: 'list unavailable' } });
      const cursor = url.searchParams.get('cursor');
      const historyPage = !pendingView && cursor ? state.historyPages[cursor] : undefined;
      const rows = pendingView ? state.pendingRows : historyPage?.items ?? state.historyRows;
      const nextCursor = pendingView ? state.pendingNextCursor : historyPage ? historyPage.nextCursor : state.historyNextCursor;
      return route.fulfill({ json: { items: rows, nextCursor } });
    }
    return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
  });
  return state;
}

async function openApproval(page: Page, id = pending.id): Promise<Locator> {
  await page.locator(`#approval-${id}`).click();
  const detail = page.getByRole('dialog');
  await expect(detail).toBeVisible();
  return detail;
}

test.describe('approval decision inbox', () => {
  test('keeps pending decisions separate, hides decision actions from rows, and manages detail focus', async ({ page }) => {
    const state = await installFixtures(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/approvals');
    await expect(page.getByRole('heading', { name: 'Approvals', exact: true })).toBeVisible();
    const row = page.locator(`#approval-${pending.id}`);
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute('aria-label', 'Review approval: Create a reminder');
    await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    await expect(page.getByText(`Conversation ${pending.conversationId}`)).toBeVisible();
    await expect(page.getByText('reminder.create', { exact: true })).toHaveCount(0);

    const detail = await openApproval(page);
    await expect(detail.getByRole('heading', { name: 'Create a reminder', exact: true })).toBeVisible();
    await expect(detail.getByText('What will happen', { exact: true })).toBeVisible();
    await expect(detail.getByText(detailFor(pending).proposal.content, { exact: true })).toBeVisible();
    await expect(detail.getByText('Origin', { exact: true })).toBeVisible();
    await expect(detail.getByText('Expires', { exact: true })).toBeVisible();
    await expect(detail.getByText('reminder.create v1', { exact: true })).not.toBeVisible();
    await detail.getByText('Technical details', { exact: true }).click();
    await expect(detail.getByText('reminder.create v1', { exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(detail).toBeHidden();
    await expect(row).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const results = await new AxeBuilder({ page }).include('.approvals-page').analyze();
    expect(results.violations).toEqual([]);
    expect(state.decisionCalls).toBe(0);
  });

  test('accepts a recurring reminder lifecycle event and renders its typed command detail', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [];
    state.streamMode = 'command';
    await page.goto('/approvals');
    const row = page.locator(`#approval-${commandPending.id}`);
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute('aria-label', 'Review approval: Create a recurring reminder');
    const detail = await openApproval(page, commandPending.id);
    await expect(detail.getByRole('heading', { name: 'Create a recurring reminder', exact: true })).toBeVisible();
    await expect(detail.getByText(commandDetail.proposal.summary, { exact: true })).toBeVisible();
    const technicalDetails = detail.locator('details.technical-disclosure');
    await expect(detail.getByText('series.create', { exact: true })).toBeHidden();
    await expect(detail.getByText('series', { exact: true })).toBeHidden();
    await technicalDetails.getByText('Technical details', { exact: true }).click();
    await expect(detail.getByText('series.create', { exact: true })).toBeVisible();
    await expect(detail.getByText('series', { exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
    expect(state.streamRequests).toBe(1);
  });

  test('renders cancel scope in plain language and keeps protocol fields in technical details', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [cancelOccurrence];
    await page.goto('/approvals');
    const detail = await openApproval(page, cancelOccurrence.id);
    await expect(detail.getByRole('heading', { name: 'Cancel this reminder occurrence', exact: true })).toBeVisible();
    await expect(detail.locator('.proposal-card').getByText('future recurring occurrences remain scheduled', { exact: false })).toBeVisible();
    await expect(detail.locator('.proposal-card').getByText('cancel', { exact: true })).toHaveCount(0);
    await detail.getByText('Technical details', { exact: true }).click();
    await expect(detail.getByText('cancel', { exact: true })).toBeVisible();
    await expect(detail.getByText('occurrence', { exact: true })).toBeVisible();
  });

  test('uses terminal copy for an approved recurring command', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [terminalCommand];
    await page.goto('/approvals');
    const detail = await openApproval(page, terminalCommand.id);
    await expect(detail.getByText('Approved and completed. This action is no longer awaiting authorization.', { exact: true })).toBeVisible();
    await expect(detail.getByText('Aura is waiting for your authorization before it takes this action.', { exact: true })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  });

  test('does not drop one approval click while SSE reconciliation refreshes the same detail', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'pending-refresh';
    let releaseStreamEvent: (() => void) | undefined;
    state.streamEventGate = new Promise<void>((resolve) => { releaseStreamEvent = resolve; });
    let releaseDetailRefresh: (() => void) | undefined;
    state.detailRefreshGate = new Promise<void>((resolve) => { releaseDetailRefresh = resolve; });
    await page.goto('/approvals');
    const detail = await openApproval(page);
    state.streamEventGate = undefined;
    releaseStreamEvent?.();
    await expect.poll(() => state.detailRequests).toBe(2);
    const approve = detail.getByRole('button', { name: 'Approve', exact: true });
    await expect(approve).toBeVisible();
    await approve.click();
    await expect.poll(() => state.decisionCalls).toBe(1);
    releaseDetailRefresh?.();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  });

  test('sends a UUID idempotency key when the browser has no randomUUID', async ({ page }) => {
    await page.addInitScript(() => {
      const browserCrypto = globalThis.crypto as Crypto & { randomUUID?: () => string };
      try { Object.defineProperty(browserCrypto, 'randomUUID', { configurable: true, value: undefined }); }
      catch { Object.defineProperty(Crypto.prototype, 'randomUUID', { configurable: true, value: undefined }); }
    });
    const state = await installFixtures(page);
    await page.goto('/approvals');
    const detail = await openApproval(page);
    await detail.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect.poll(() => state.decisionCalls).toBe(1);
    expect(state.decisionIdempotencyKeys[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  });

  test('keeps the authoritative pending badge independent of History and pagination', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [pending, pendingB];
    state.pendingNextCursor = 'pending-next';
    await page.goto('/approvals');
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();
    await expect(page.locator('.approval-card').first().getByText('Create a reminder', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();
    await expect(page.getByText('Approved', { exact: true })).toBeVisible();
    await expect(page.getByText('Rejected', { exact: true })).toBeVisible();
    await expect(page.locator(`#approval-${pending.id}`)).toHaveCount(0);
    await expect(page.locator(`#approval-${pendingB.id}`)).toHaveCount(0);
    await expect(page.locator(`#approval-${approved.id}`)).toBeVisible();
    await expect(page.locator(`#approval-${rejected.id}`)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
  });

  test('advances unfiltered history past a pending-only page and preserves continuation', async ({ page }) => {
    const state = await installFixtures(page);
    state.historyRows = [pending];
    state.historyNextCursor = 'history-next';
    state.historyPages['history-next'] = { items: [approved], nextCursor: 'history-final' };
    state.historyPages['history-final'] = { items: [rejected, historyThree], nextCursor: null };
    await page.goto('/approvals');
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByRole('button', { name: 'History', exact: false })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator(`#approval-${pending.id}`)).toHaveCount(0);
    await expect(page.locator(`#approval-${approved.id}`)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Load more approvals', exact: true })).toBeVisible();
    const historyContinuationRequests = state.listRequests;
    await page.getByRole('button', { name: 'Load more approvals', exact: true }).dispatchEvent('click');
    await expect.poll(() => state.listRequests).toBe(historyContinuationRequests + 1);
    await expect(page.locator(`#approval-${rejected.id}`)).toBeVisible();
    await expect(page.locator(`#approval-${historyThree.id}`)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Load more approvals', exact: true })).toHaveCount(0);
  });

  test('requires detail review, updates terminal state, and removes impossible actions', async ({ page }) => {
    const state = await installFixtures(page);
    let releaseDecision: (() => void) | undefined;
    let decisionRequests = 0;
    const decisionReady = new Promise<void>((resolve) => { releaseDecision = resolve; });
    await page.route(`**/api/v1/approvals/${pending.id}/approve`, async (route) => {
      decisionRequests += 1;
      await decisionReady;
      const updated = { ...pending, status: 'approved' as const, executionStatus: 'queued' as const, version: 2, decidedAt: now };
      state.pendingRows = [];
      state.historyRows = [updated, ...state.historyRows];
      state.details[pending.id] = { ...state.details[pending.id], ...updated };
      return route.fulfill({ json: updated });
    });
    await page.goto('/approvals');
    await expect(page.getByRole('button', { name: /Approve|Reject/ })).toHaveCount(0);
    const detail = await openApproval(page);
    const approve = detail.locator('button.primary-button');
    await approve.click();
    await expect.poll(() => decisionRequests).toBe(1);
    await expect(approve).toBeDisabled();
    await expect(approve).toHaveText('Saving…');
    releaseDecision?.();
    await expect(detail.locator('.detail-meta dd').filter({ hasText: 'Approved', exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Pending 0', exact: true })).toBeVisible();
    expect(decisionRequests).toBe(1);
  });

  test('renders terminal deep links without decision controls', async ({ page }) => {
    await installFixtures(page);
    await page.goto(`/approvals/${approved.id}`);
    const detail = page.getByRole('dialog');
    await expect(detail.getByRole('heading', { name: 'Create a reminder', exact: true })).toBeVisible();
    await expect(detail.getByRole('status').filter({ hasText: 'durable' })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    await detail.getByRole('button', { name: 'Close approval details', exact: true }).click();
    await expect(page).toHaveURL(/\/approvals$/);
  });

  test('handles delayed detail loading, not-found, permission, and conflict states', async ({ page }) => {
    const state = await installFixtures(page);
    let releaseDetail: (() => void) | undefined;
    state.detailGate = new Promise<void>((resolve) => { releaseDetail = resolve; });
    await page.goto(`/approvals/${pending.id}`);
    await expect(page.getByRole('status').filter({ hasText: 'Loading approval' })).toBeVisible();
    releaseDetail?.();
    await expect(page.getByRole('dialog').getByRole('heading', { name: 'Create a reminder', exact: true })).toBeVisible();

    state.detailErrors[pending.id] = 404;
    await page.goto(`/approvals/${pending.id}`);
    await expect(page.getByRole('alert').filter({ hasText: 'Approval details are unavailable' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry details', exact: true })).toBeVisible();
    state.detailErrors[pending.id] = 403;
    await page.getByRole('button', { name: 'Retry details', exact: true }).click();
    const permissionAlert = page.getByRole('alert').filter({ hasText: 'permission' });
    await expect(permissionAlert).toHaveCount(1);
    await expect(permissionAlert).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    state.detailErrors[pending.id] = 409;
    await page.goto(`/approvals/${pending.id}`);
    await expect(page.getByRole('alert').filter({ hasText: 'changed elsewhere' })).toBeVisible();
  });

  test('does not present an unknown decision outcome as success or retry automatically', async ({ page }) => {
    const state = await installFixtures(page);
    state.decisionResponses = [{ abort: true }];
    await page.goto('/approvals');
    const detail = await openApproval(page);
    await detail.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'outcome is unknown' })).toBeVisible();
    await expect(detail.getByText('Pending', { exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    await expect(detail.getByRole('status').filter({ hasText: 'outcome is unconfirmed' })).toBeVisible();

    const listRequestsBeforeRetry = state.listRequests;
    const detailRequestsBeforeRetry = state.detailRequests;
    const terminal = { ...pending, status: 'approved' as const, executionStatus: 'completed' as const, version: 2, decidedAt: now };
    state.pendingRows = [];
    state.historyRows = [terminal, ...state.historyRows];
    state.details[pending.id] = { ...state.details[pending.id], ...terminal };
    let releaseDetail: (() => void) | undefined;
    state.detailGate = new Promise<void>((resolve) => { releaseDetail = resolve; });
    await detail.getByRole('alert').filter({ hasText: 'outcome is unknown' }).getByRole('button', { name: 'Retry', exact: true }).click();
    await expect.poll(() => state.listRequests).toBe(listRequestsBeforeRetry + 1);
    await expect.poll(() => state.detailRequests).toBe(detailRequestsBeforeRetry + 1);
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('#approval-detail-panel')))).toBe(true);
    releaseDetail?.();
    state.detailGate = undefined;
    await expect(detail.getByRole('status').filter({ hasText: 'Approved. This decision is durable' })).toBeVisible();
    await expect(detail).toBeFocused();
    await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    expect(state.decisionCalls).toBe(1);
  });

  test('does not expose a second decision after an unknown outcome and detail close', async ({ page }) => {
    const state = await installFixtures(page);
    state.decisionResponses = [{ abort: true }];
    await page.goto('/approvals');
    const detail = await openApproval(page);
    await detail.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'outcome is unknown' })).toBeVisible();
    await detail.getByRole('button', { name: 'Close approval details', exact: true }).click();
    await expect(page).toHaveURL(/\/approvals$/);
    const reopened = await openApproval(page);
    await expect(reopened.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(reopened.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    expect(state.decisionCalls).toBe(1);
  });

  test('ignores a delayed decision failure after navigating to another approval', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [pending, pendingB];
    let releaseDecision: (() => void) | undefined;
    state.decisionGate = new Promise<void>((resolve) => { releaseDecision = resolve; });
    state.decisionResponses = [{ status: 503 }];
    await page.goto('/approvals');
    const first = await openApproval(page, pending.id);
    await first.getByRole('button', { name: 'Approve', exact: true }).click();
    await first.getByRole('button', { name: 'Close approval details', exact: true }).click();
    const second = await openApproval(page, pendingB.id);
    await expect(second.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
    releaseDecision?.();
    await page.waitForTimeout(50);
    await expect(second.getByRole('heading', { name: 'Create a reminder', exact: true })).toBeVisible();
    await expect(second.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
    await expect(second.getByRole('button', { name: 'Saving…', exact: true })).toHaveCount(0);
    await expect(page.getByRole('alert').filter({ hasText: 'could not be approved' })).toHaveCount(0);
  });

  test('does not republish a delayed decision after closing and reopening the same approval', async ({ page }) => {
    const state = await installFixtures(page);
    let releaseDecision: (() => void) | undefined;
    state.decisionGate = new Promise<void>((resolve) => { releaseDecision = resolve; });
    state.decisionResponses = [{ status: 503 }];
    await page.goto('/approvals');
    const first = await openApproval(page, pending.id);
    await first.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect.poll(() => state.decisionCalls).toBe(1);
    await first.getByRole('button', { name: 'Close approval details', exact: true }).click();
    const reopened = await openApproval(page, pending.id);
    await expect(reopened.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(reopened.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    releaseDecision?.();
    state.decisionGate = undefined;
    await expect.poll(() => state.decisionCalls).toBe(1);
    await expect(reopened.getByRole('heading', { name: 'Create a reminder', exact: true })).toBeVisible();
    await expect(reopened.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(reopened.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    await expect(page.getByRole('alert').filter({ hasText: 'could not be approved' })).toHaveCount(0);
  });

  test('keeps two unresolved approvals independently confirmation-gated across navigation', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingRows = [pending, pendingB];
    let releaseDecisions: (() => void) | undefined;
    state.decisionGate = new Promise<void>((resolve) => { releaseDecisions = resolve; });
    state.decisionResponses = [{ status: 503 }, { status: 503 }];
    await page.goto('/approvals');

    const first = await openApproval(page, pending.id);
    await first.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect.poll(() => state.decisionCalls).toBe(1);
    await first.getByRole('button', { name: 'Close approval details', exact: true }).click();

    const second = await openApproval(page, pendingB.id);
    await second.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect.poll(() => state.decisionCalls).toBe(2);
    await second.getByRole('button', { name: 'Close approval details', exact: true }).click();

    const firstAgain = await openApproval(page, pending.id);
    await expect(firstAgain.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(firstAgain.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    expect(state.decisionCalls).toBe(2);

    releaseDecisions?.();
    state.decisionGate = undefined;
    await page.waitForTimeout(100);
    await expect(firstAgain.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
    await expect(firstAgain.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
    expect(state.decisionCalls).toBe(2);
  });

  test('clears owner-scoped approval state after an authorization failure', async ({ page }) => {
    const state = await installFixtures(page);
    state.pendingListResponses = [401];
    await page.goto('/approvals');
    await expect(page.getByRole('alert').filter({ hasText: 'session has expired' })).toBeVisible();
    await expect(page.getByText('Create a reminder', { exact: true })).toHaveCount(0);
    await page.reload();
    await expect(page.locator(`#approval-${pending.id}`)).toBeVisible();
  });

  test('clears owner state when a current-session auth failure settles after detail navigation changes', async ({ page }) => {
    const state = await installFixtures(page);
    let releaseDetail: (() => void) | undefined;
    state.detailGate = new Promise<void>((resolve) => { releaseDetail = resolve; });
    state.detailErrors[pending.id] = 401;
    await page.goto(`/approvals/${pending.id}`);
    await expect(page.getByRole('status').filter({ hasText: 'Loading approval' })).toBeVisible();
    await expect.poll(() => state.streamRequests).toBeGreaterThan(0);
    const streamRequestsBeforeReset = state.streamRequests;
    await page.getByRole('button', { name: 'Close approval details', exact: true }).click();
    await expect(page).toHaveURL(/\/approvals$/);
    state.detailGate = undefined;
    releaseDetail?.();
    await expect(page.getByRole('alert').filter({ hasText: 'session has expired' })).toBeVisible();
    await expect(page.locator(`#approval-${pending.id}`)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.waitForTimeout(100);
    expect(state.streamRequests).toBe(streamRequestsBeforeReset);
    state.detailErrors[pending.id] = undefined;
    await page.reload();
    await expect(page.locator(`#approval-${pending.id}`)).toBeVisible();
  });

  test('clears owner state when a decision settles with authorization failure after detail closes', async ({ page }) => {
    const state = await installFixtures(page);
    let releaseDecision: (() => void) | undefined;
    state.decisionGate = new Promise<void>((resolve) => { releaseDecision = resolve; });
    state.decisionResponses = [{ status: 403 }];
    await page.goto('/approvals');
    const detail = await openApproval(page);
    await detail.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect(detail.locator('button.primary-button')).toBeDisabled();
    await detail.getByRole('button', { name: 'Close approval details', exact: true }).click();
    state.decisionGate = undefined;
    releaseDecision?.();
    await expect(page.getByRole('alert').filter({ hasText: 'permission' })).toBeVisible();
    await expect(page.locator(`#approval-${pending.id}`)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(state.decisionCalls).toBe(1);
    await page.reload();
    await expect(page.locator(`#approval-${pending.id}`)).toBeVisible();
  });

  test('requires successful 410 reconciliation before reconnecting with no expired cursor', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = '410';
    state.pendingListResponses = [0, 503];
    await page.goto('/approvals');
    await expect(page.getByRole('button', { name: 'Retry live updates', exact: true })).toBeVisible();
    await expect.poll(() => state.streamRequests).toBe(1);
    await expect.poll(() => state.listRequests).toBe(2);
    await page.waitForTimeout(50);
    expect(state.streamRequests).toBe(1);
    await page.getByRole('button', { name: 'Retry live updates', exact: true }).click();
    await expect.poll(() => state.streamRequests).toBe(2);
    expect(state.streamHeaders[0]).toBe('');
    expect(state.streamHeaders[1]).toBe('');
  });

  test('does not resume SSE with an event cursor whose authoritative reconciliation failed', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'event-reconcile-fail';
    state.pendingListResponses = [0, 503];
    await page.goto('/approvals');
    await expect.poll(() => state.streamRequests).toBeGreaterThanOrEqual(2);
    await expect.poll(() => state.listRequests).toBeGreaterThanOrEqual(2);
    expect(state.streamHeaders[0]).toBe('');
    expect(state.streamHeaders[1]).toBe('');
    expect(state.streamHeaders).not.toContain('33333333-3333-4333-8333-333333333333');
  });

  test('accepts valid bounded SSE events while ignoring malformed events', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'invalid-valid';
    await page.goto('/approvals');
    await expect(page.getByText('Create a reminder', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Pending 0', exact: true })).toBeVisible();
    expect(state.streamRequests).toBe(1);
  });

  test('keeps the pending badge unknown during reconciliation and exact after recovery', async ({ page }) => {
    const state = await installFixtures(page);
    state.streamMode = 'pending-refresh';
    let releaseStreamEvent: (() => void) | undefined;
    state.streamEventGate = new Promise<void>((resolve) => { releaseStreamEvent = resolve; });
    await page.goto('/approvals');
    await expect(page.getByRole('button', { name: 'Pending 1', exact: true })).toBeVisible();

    let releasePendingList: (() => void) | undefined;
    state.pendingListGate = new Promise<void>((resolve) => { releasePendingList = resolve; });
    state.pendingListResponses = [503];
    state.streamEventGate = undefined;
    releaseStreamEvent?.();
    await expect.poll(() => state.listRequests).toBe(2);
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();
    state.pendingListGate = undefined;
    releasePendingList?.();
    await expect(page.getByRole('alert').filter({ hasText: 'temporarily unavailable' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Pending Several', exact: true })).toBeVisible();

    state.pendingRows = [];
    state.pendingListResponses = [0];
    await page.getByRole('alert').filter({ hasText: 'temporarily unavailable' }).getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Pending 0', exact: true })).toBeVisible();
  });

  test('caps reconnect attempts and exposes manual recovery', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'Timer-heavy stream cap coverage runs once in desktop.');
    const state = await installFixtures(page);
    state.streamMode = 'cap';
    await page.clock.install();
    await page.goto('/approvals');
    await expect.poll(() => state.streamRequests).toBe(1);
    const retryDelays = [500, 1_000, 2_000, 4_000, 8_000, 15_000];
    for (const [index, delay] of retryDelays.entries()) {
      await page.clock.fastForward(delay);
      await page.evaluate(() => undefined);
      await expect.poll(() => state.streamRequests).toBe(index + 2);
    }
    await expect(page.getByRole('alert').filter({ hasText: 'Live approvals stopped after several attempts' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry live updates', exact: true })).toBeVisible();
  });
});
